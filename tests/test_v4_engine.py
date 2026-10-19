"""v4 回测引擎端到端测试:真实 EventDetector/PositionManager/Broker + 脚本化
策略(注入 E7 白名单 OPEN 意图与 G3 前缘限价),1h bar 由 4 根 15m 子 bar
聚合合成(compaction 与认知输入逐格一致)。

统一合约与成本先验同 test_v4_broker_limit.py;T0 整小时对齐;单品种 TEST。
覆盖:主场景(限价入场→目标减仓限价→§8.1 退出,逐腿精确数值与 ts 因果)、
超时 EXPIRED(再挂第二单证明解锁)、armed 小时子 bar 不成文、MIN_SIZE 拒绝、
减仓超时转 taker、段门控、确定性双跑;另锁策略层 entry_mode 前缘限价取值。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from market_operator.backtest import prepare_cognition
from market_operator.backtest_v4 import build_exec_frame, run_backtest_v4
from market_operator.broker import ContractSpec
from market_operator.events import E7, MarketEvent
from market_operator.hypothesis import Hypothesis, StructureView
from market_operator.strategy import OrderIntent, OrderKind, Strategy, StrategyParams
from market_operator.structure import Params, Regime, Zone, ZoneRole, ZoneState

H = 3_600_000
Q = 900_000
T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC
PARAMS = Params(atr_n=2)
SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(dev_frac=0.7, risk_frac=0.0075, leverage_cap=5.0,
          taker_fee=0.0006, maker_fee=0.0002, slippage_per_side=0.0002,
          funding_per_8h=0.0001, cost_multiplier=1.0,
          contracts={"TEST": SPEC}, params=PARAMS, n_fill=12)
BP = 98.0                                # 决策参考价(desision bar 收盘)


# ---------------------------------------------------------------------------
# 合成帧:每小时 4 根 15m 子 bar;1h bar = 聚合(compaction 逐格一致)
# ---------------------------------------------------------------------------


def _build(subs_per_hour: list) -> tuple:
    """subs_per_hour[i] = [(o,h,l,c) × 4] ⇒ (df_1h, df_15m)。"""
    rows_1h, rows_15m = [], []
    for i, subs in enumerate(subs_per_hour):
        assert len(subs) == 4
        o = subs[0][0]
        h = max(s[1] for s in subs)
        l = min(s[2] for s in subs)
        c = subs[3][3]
        rows_1h.append((T0 + i * H, o, h, l, c, 4.0))
        for k, (so, sh, sl, sc) in enumerate(subs):
            rows_15m.append((T0 + i * H + k * Q, so, sh, sl, sc, 1.0))
    cols = ["ts", "open", "high", "low", "close", "volume"]
    return (pd.DataFrame(rows_1h, columns=cols),
            pd.DataFrame(rows_15m, columns=cols))


def calm(*closes, hi_pad=0.4, lo_pad=0.4):
    """一小时平静子 bar 组(线性过渡到收盘,极值留 pad)。"""
    out, prev = [], None
    for c in closes:
        o = prev if prev is not None else c
        out.append((o, max(o, c) + hi_pad, min(o, c) - lo_pad, c))
        prev = c
    return out


def spike_sub(o, h, l, c):
    return (o, h, l, c)


class _Scripted:
    """脚本化策略:按 1h bar ts 注入白名单 OPEN 意图(与 Strategy 接口同形)。"""

    def __init__(self, script: dict, entry_mode="limit_front_edge"):
        self.params = StrategyParams(
            whitelist=frozenset({(E7, -1), (E7, 1)}), entry_mode=entry_mode)
        self._script = script
        self._taken: dict = {}
        self._taken_limit: dict = {}

    def on_bar(self, symbol, bar, view, new_events, open_position_state=None):
        rec = self._script.get(int(bar[0]))
        if rec is None:
            return []
        intent, hyp, limit = rec
        self._taken[symbol] = hyp
        if limit is not None:
            self._taken_limit[symbol] = limit
        return [intent]


def _open_script(hour: int, limit=99.5, invalid=101.0, target=95.0):
    ts = T0 + hour * H
    hyp = Hypothesis(event_id=E7, direction=-1, ts=ts, known_index=hour,
                     entry=None, invalid=invalid, target_1=target,
                     r_mult=2.0, regime=Regime.RANGE)
    intent = OrderIntent(
        kind=OrderKind.OPEN, symbol="TEST", qty_frac=None,
        reason=(f"E7|short whitelist entry_ref={BP} invalid={invalid} "
                f"target_1={target} r_mult=2.000 limit_front_edge={limit}"),
        ref_price=BP, ts=ts)
    return {ts: (intent, hyp, limit)}


def _frame(hours):
    df_1h, df_15m = _build(hours)
    cog = prepare_cognition(df_1h, PARAMS, "TEST", enable_e7=True)
    return build_exec_frame(cog, df_15m)


def _run(hours, script, segment="full", **over):
    kw = dict(KW)
    equity0 = over.pop("equity0", 300.0)
    kw.update(over)
    return run_backtest_v4({"TEST": _frame(hours)}, segment=segment,
                           equity0=equity0,
                           whitelist=frozenset({(E7, -1), (E7, 1)}),
                           strategy=_Scripted(script), **kw)


# ---------------------------------------------------------------------------
# 主场景 hours 蓝图(40 小时)
# ---------------------------------------------------------------------------


def main_hours():
    hours = []
    for _ in range(8):                                  # 0..7 平静 100
        hours.append(calm(100.0, 100.0, 100.0, 100.0, hi_pad=0.05, lo_pad=0.05))
    hours.append(calm(98.0, 98.0, 98.0, 98.0))          # 8 跌至 98
    hours.append(calm(98.0, 98.3, 97.9, 98.0))          # 9
    # 10 决策小时(子 bar 2 冲高 99.6 > 限价 —— armed 小时不得成交;1h 反映 spike)
    hours.append([(98.0, 98.4, 97.8, 98.1),
                  (98.1, 98.5, 97.9, 98.2),
                  spike_sub(98.2, 99.6, 97.8, 98.3),
                  (98.3, 98.4, 98.0, 98.0)])
    # 11 未达 99.5(严格子 bar 手工,最高 99.3)
    hours.append([(98.2, 98.7, 97.9, 98.4), (98.4, 98.9, 98.1, 98.6),
                  (98.6, 99.1, 98.3, 98.9), (98.9, 99.3, 98.5, 98.9)])
    # 12 子 bar 1 恰触 99.5 持平(不成交),全小时最高 99.5
    hours.append([(98.9, 99.2, 98.6, 99.0), spike_sub(99.0, 99.5, 98.7, 99.1),
                  (99.1, 99.4, 98.8, 99.2), (99.2, 99.4, 98.8, 99.2)])
    # 13 穿越小时:q1 持平 99.5,q2 严格穿过 99.6 ⇒ 成交于 ts=13H+2Q
    hours.append([(99.2, 99.4, 99.0, 99.2),
                  spike_sub(99.2, 99.5, 99.0, 99.3),
                  spike_sub(99.3, 99.6, 99.1, 99.0),
                  (99.0, 99.2, 98.8, 98.9)])
    hours.append(calm(98.9, 98.4, 97.9, 97.8))          # 14 回落
    hours.append(calm(97.8, 97.2, 96.8, 96.5))          # 15
    # 16 §8.2 触发(1h low 94.99 ≤ 目标 95.0);决策收盘武装减仓限价
    hours.append([(96.5, 96.8, 96.0, 96.2),
                  spike_sub(96.2, 96.3, 94.99, 95.4),
                  (95.4, 95.6, 95.3, 95.4),
                  (95.4, 95.5, 95.2, 95.4)])
    # 17 减仓成交:q0 low==95.0 持平不成交;q1 严格穿过 94.9 ⇒ buy limit @95.0
    hours.append([(95.4, 95.5, 95.0, 95.2),
                  spike_sub(95.2, 95.4, 94.9, 95.1),
                  (95.1, 95.3, 95.05, 95.2),
                  (95.2, 95.3, 95.05, 95.3)])
    hours.append(calm(95.3, 96.4, 97.9, 98.4))          # 18 反弹
    hours.append(calm(98.4, 98.8, 99.0, 98.6))          # 19(收盘 98.6 < 保本 99.5)
    hours.append(calm(98.6, 100.5, 101.8, 102.0))       # 20 收盘 102 > 99.5 ⇒ §8.1
    hours.append(calm(102.0, 102.2, 101.6, 101.5))      # 21 退出成交 = 1h 开盘 102.0
    for _ in range(18):                                 # 22..39 平静
        hours.append(calm(101.5, 101.5, 101.5, 101.5,
                          hi_pad=0.05, lo_pad=0.05))
    return hours


# ---------------------------------------------------------------------------
# 主场景:限价入场 → 减仓限价(maker)→ §8.1 退出(taker)逐腿精确
# ---------------------------------------------------------------------------


def test_main_scenario_leg_by_leg_exact():
    res = _run(main_hours(), _open_script(10))
    c = res.counters
    assert (c["limits_armed"], c["limit_fills_entry"], c["limit_expired"],
            c["reduce_limits_armed"], c["reduce_limit_fills"],
            c["reduce_timeout_taker"], c["min_size_skips"], c["liquidations"]) == \
        (1, 1, 0, 1, 1, 0, 0, 0)
    kinds = [r["kind"] for r in res.ledger]
    assert kinds == ["LIMIT_FILL", "LIMIT_FILL", "EXIT"]

    ent, red, ex = res.ledger
    # 入场:决策 hour10 ⇒ 成交 hour13 子 bar 2(穿过 99.6 > 限价 99.5)
    assert ent["decision_ts"] == T0 + 10 * H
    assert ent["ts"] == T0 + 13 * H + 2 * Q
    assert ent["price"] == 99.5 and ent["slippage_cost"] == 0.0
    assert ent["qty"] == 1.5                            # 2.25/1.5 向下取整
    assert ent["fee"] == pytest.approx(1.5 * 99.5 * 0.0002, rel=1e-12)  # maker
    assert ent["meta"]["fill_wait_bars"] == 11          # 4+4+q0,q1,q2
    assert ent["reason_code"] == "E7"
    # 减仓(§8.2 → maker 限价:决策 hour16 ⇒ 成交 hour17 子 bar 1)
    assert red["decision_ts"] == T0 + 16 * H
    assert red["ts"] == T0 + 17 * H + 1 * Q
    assert red["side"] == "buy" and red["price"] == 95.0
    assert red["qty"] == 0.75                           # floor(0.5×1.5)
    assert red["realized_pnl"] == pytest.approx((99.5 - 95.0) * 0.75, rel=1e-12)
    assert red["fee"] == pytest.approx(0.75 * 95.0 * 0.0002, rel=1e-12)
    assert red["meta"]["fill_wait_bars"] == 2           # q0 持平,q1 穿过
    # 退出(§8.1:决策 hour20 收盘 102 > 保本 99.5 ⇒ hour21 首根 15m 开盘 taker)
    assert ex["decision_ts"] == T0 + 20 * H
    assert ex["ts"] == T0 + 21 * H                      # = 1h bar21 开盘(v2 语义)
    assert ex["price"] == pytest.approx(102.0 * 1.0002, abs=1e-12)
    assert ex["qty"] == 0.75
    assert ex["fee"] == pytest.approx(0.75 * ex["price"] * 0.0006, rel=1e-9)
    assert ex["funding_delta"] < 0.0                    # 全部资金费结清
    assert 0.0 < -ex["funding_delta"] < 0.02
    # 因果硬约束:全部行 ts > decision_ts
    for r in res.ledger:
        assert r["ts"] > r["decision_ts"]

    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["entry_mode"] == "limit" and t["fill_wait_bars"] == 11
    assert t["limit_price"] == 99.5 and t["entry"] == 99.5
    assert t["closed"] and t["exit_kind"] == "EXIT"
    assert t["hold_hours"] == pytest.approx(7.5, rel=1e-12)    # 13:30→21:00
    gross = (99.5 - 95.0) * 0.75 + (99.5 - 102.0204) * 0.75
    assert t["gross_pnl"] == pytest.approx(gross, rel=1e-12)
    assert t["net_pnl"] == pytest.approx(
        gross - (1.5 * 99.5 * 0.0002 + 0.75 * 95.0 * 0.0002
                 + 0.75 * 102.0204 * 0.0006) - (-ex["funding_delta"]), rel=1e-9)
    assert res.equity_rows[-1][1] == pytest.approx(300.0 + t["net_pnl"], rel=1e-9)
    # 账本现金对账(同 v1/v2 口径)
    recon = 300.0 + sum(x["realized_pnl"] - x["fee"] + x["funding_delta"]
                        for x in res.ledger)
    assert recon == pytest.approx(res.equity_rows[-1][2], rel=1e-9)


def test_armed_hour_subbars_never_fill_causality():
    """决策(hour10)小时子 bar 2 已有 spike 99.6 > 限价;成交必须晚到 hour13。"""
    res = _run(main_hours(), _open_script(10))
    ent = res.ledger[0]
    assert ent["ts"] >= T0 + 11 * H                     # 最早 hour11 首根
    assert ent["ts"] == T0 + 13 * H + 2 * Q > T0 + 10 * H


# ---------------------------------------------------------------------------
# 超时:EXPIRED(无交易)→ 再挂第二单正常成交(解锁)
# ---------------------------------------------------------------------------


def no_cross_hours():
    hours = main_hours()
    # 抹掉 hour13/16/17 的穿越:高位压回 99.2 之下,低位不破 94.99
    for i in (10, 11, 12, 13):
        hours[i] = calm(98.5, 98.8, 98.6, 98.6, hi_pad=0.4, lo_pad=0.3)
    for i in range(14, 40):                 # 余程平静(不移防线不触破位)
        hours[i] = calm(97.5, 97.9, 97.8, 97.8, hi_pad=0.3, lo_pad=0.3)
    return hours


def test_expiry_then_rearm_second_episode():
    script = _open_script(10) | _open_script(26, limit=99.5)
    hours = no_cross_hours()
    # 第二单成交穿越:hour27 子 bar 1 spike 99.6
    hours[27] = [(98.8, 99.0, 98.5, 98.9),
                 spike_sub(98.9, 99.6, 98.6, 98.7),
                 (98.7, 99.0, 98.5, 98.8),
                 (98.8, 99.0, 98.6, 98.9)]
    res = _run(hours, script)
    c = res.counters
    assert c["limits_armed"] == 2 and c["limit_expired"] == 1
    assert c["limit_fills_entry"] == 1
    exp = [r for r in res.ledger if r["kind"] == "EXPIRED"]
    assert len(exp) == 1
    assert exp[0]["ts"] == T0 + 23 * H                    # armed 10H ⇒ 窗口 11..22H
    assert exp[0]["decision_ts"] == T0 + 10 * H
    fills = [r for r in res.ledger if r["kind"] == "LIMIT_FILL"
             and r["meta"].get("role") != "reduce"]
    assert fills[0]["ts"] == T0 + 27 * H + 1 * Q
    assert fills[0]["decision_ts"] == T0 + 26 * H
    assert fills[0]["meta"]["fill_wait_bars"] == 2         # q0+q1
    assert len(res.trades) == 1 and not res.trades[0]["closed"]  # 持仓至数据末


# ---------------------------------------------------------------------------
# MIN_SIZE 拒绝路径(挂单成交刻;订单被消费,无这笔交易)
# ---------------------------------------------------------------------------


def test_min_size_reject_at_fill_consumes_order():
    res = _run(main_hours(), _open_script(10), equity0=5.0)
    c = res.counters
    assert c["limits_armed"] == 1 and c["limit_fills_entry"] == 0
    assert c["limit_min_size_skips"] == 1 and c["min_size_skips"] == 1
    skips = [r for r in res.ledger if r["kind"] == "SKIP"]
    assert len(skips) == 1 and skips[0]["reason_code"] == "MIN_SIZE"
    assert skips[0]["ts"] == T0 + 13 * H + 2 * Q            # 穿过时刻拒绝
    assert len(res.trades) == 0
    assert res.equity_rows[-1][1] == pytest.approx(5.0, rel=1e-12)


# ---------------------------------------------------------------------------
# 减仓超时:限价撤 CANCEL(TIMEOUT_TO_TAKER) + 同数量 taker 市价
# ---------------------------------------------------------------------------


def test_reduce_timeout_converts_to_taker_market():
    hours = main_hours()
    hours[17] = calm(95.4, 95.3, 95.2, 95.3, hi_pad=0.3, lo_pad=0.0)  # 不穿过
    hours[17][0] = (95.4, 95.6, 95.0, 95.3)               # 恰触 95.0 持平
    for i in range(18, 29):
        hours[i] = calm(96.0, 96.2, 96.1, 96.1, hi_pad=0.3, lo_pad=0.3)
    # hour29 sub0 检出超时(parent 29H > expiry 28H);sub1 市价成交
    hours[29] = [(96.1, 96.3, 95.9, 96.0),
                 (96.0, 96.2, 95.8, 95.9),
                 (95.9, 96.1, 95.7, 96.0),
                 (96.0, 96.2, 95.9, 96.1)]
    res = _run(hours, _open_script(10))
    c = res.counters
    assert c["reduce_limits_armed"] == 1 and c["reduce_limit_fills"] == 0
    assert c["reduce_timeout_taker"] == 1
    kinds = [r["kind"] for r in res.ledger
             if r["kind"] in ("LIMIT_FILL", "CANCEL", "REDUCE", "EXIT")]
    # LIMIT_FILL(入场)在 hour13…其后无减仓 LIMIT_FILL
    cancel = [r for r in res.ledger if r["kind"] == "CANCEL"][0]
    assert cancel["reason_code"] == "TIMEOUT_TO_TAKER"
    assert cancel["ts"] == T0 + 29 * H
    assert cancel["decision_ts"] == T0 + 16 * H
    red = [r for r in res.ledger if r["kind"] == "REDUCE"][0]
    assert red["ts"] == T0 + 29 * H + 1 * Q > cancel["ts"]
    assert red["side"] == "buy" and red["qty"] == 0.75
    assert red["price"] == pytest.approx(96.0 * 1.0002, abs=1e-12)     # taker+滑点
    assert red["fee"] == pytest.approx(0.75 * red["price"] * 0.0006, rel=1e-9)
    assert red["slippage_cost"] == pytest.approx(0.75 * 0.0192, rel=1e-9)
    assert "CANCEL" in kinds and "REDUCE" in kinds


# ---------------------------------------------------------------------------
# 段门控(dev/test 不同段结果)与确定性双跑
# ---------------------------------------------------------------------------


def test_segment_gating_same_script():
    hours = main_hours()
    script = _open_script(10)                              # 决策 hour10 ∈ dev
    test = _run(hours, script, segment="test")             # boundary=int(40×.7)=28
    assert test.counters["limits_armed"] == 0 and test.trades == []
    assert all(abs(eq - 300.0) < 1e-12 for _ts, eq, *_ in test.equity_rows)
    dev = _run(hours, script, segment="dev")
    assert dev.counters["limits_armed"] == 1 and len(dev.trades) == 1
    assert dev.trades[0]["in_segment_open"]


def test_replay_determinism_double_run():
    a = _run(main_hours(), _open_script(10))
    b = _run(main_hours(), _open_script(10))
    assert json.dumps(a.ledger, sort_keys=True, default=str) == \
        json.dumps(b.ledger, sort_keys=True, default=str)
    assert a.equity_rows == b.equity_rows
    assert json.dumps(a.trades, sort_keys=True, default=str) == \
        json.dumps(b.trades, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# 策略层:v4 entry_mode 前缘限价取值(G3:short ⇒ zone.lo,long ⇒ zone.hi)
# ---------------------------------------------------------------------------


def _e7_with_zone(direction, lo=99.0, hi=100.0):
    return MarketEvent(
        id=E7, direction=direction, ts=T0, known_index=0,
        attrs={"zone": Zone(lo=lo, hi=hi,
                            role=ZoneRole.RESISTANCE if direction < 0
                            else ZoneRole.SUPPORT,
                            state=ZoneState.ACTIVE),
               "ref": lo if direction < 0 else hi,
               "invalid": hi + 5.0 if direction < 0 else lo - 5.0,
               "from_role": (ZoneRole.SUPPORT.value if direction < 0
                             else ZoneRole.RESISTANCE.value),
               "atr": 5.0})


def _view(direction):
    zones = ((Zone(lo=91.0, hi=92.0, role=ZoneRole.SUPPORT,
                   state=ZoneState.ACTIVE),) if direction < 0 else
             (Zone(lo=110.0, hi=111.0, role=ZoneRole.RESISTANCE,
                   state=ZoneState.ACTIVE),))
    return StructureView(regime=Regime.RANGE, zones=zones, atr=5.0)


def test_strategy_limit_front_edge_price_for_short_and_long():
    bar = (T0, 100.0, 100.2, 99.8, 100.0, 1.0)
    for direction, expect in ((-1, 99.0), (+1, 100.0)):
        s = Strategy(StrategyParams(
            whitelist=frozenset({(E7, direction)}),
            entry_mode="limit_front_edge"))
        intents = s.on_bar("XYZ", bar, _view(direction), [_e7_with_zone(direction)])
        assert len(intents) == 1
        assert s._taken_limit["XYZ"] == pytest.approx(expect)
        assert f"limit_front_edge={expect:.8g}" in intents[0].reason


def test_strategy_default_mode_no_limit_recorded():
    bar = (T0, 100.0, 100.2, 99.8, 100.0, 1.0)
    s = Strategy(StrategyParams(whitelist=frozenset({(E7, -1)})))
    intents = s.on_bar("XYZ", bar, _view(-1), [_e7_with_zone(-1)])
    assert len(intents) == 1
    assert s._taken_limit == {}                              # 默认 = v1/v2 市价路径
    assert "limit_front_edge=" not in intents[0].reason


def test_unfilled_reduce_limit_must_not_trigger_phantom_breakeven_exit():
    hours = main_hours()
    # hour16 触及 target,只武装减仓限价;hour17 严格不穿过 target。
    # 收盘100处于初始止损101下方但原代码提前生效的保本99.5上方。
    hours[17] = calm(95.4, 96.0, 98.0, 100.0,
                     hi_pad=0.1, lo_pad=0.0)
    # hour18 原始测试路径曾穿过95.0,导致减仓限价真实成交;此处必须始终在95之上。
    hours[18] = calm(95.3, 96.4, 97.9, 98.4, lo_pad=0.0)
    # 隔离其他结构事件(Pivot trail / reverse E6),只验证 maker 意图与实际成交。
    frame = _frame(hours)
    frame.cog.events_by.clear()
    frame.cog.pivots_by.clear()
    res = run_backtest_v4(
        {"TEST": frame}, segment="full", equity0=300.0,
        whitelist=frozenset({(E7, -1), (E7, 1)}),
        strategy=_Scripted(_open_script(10)), **KW)
    assert res.counters["reduce_limits_armed"] == 1
    assert res.counters["reduce_limit_fills"] == 0
    exits = [r for r in res.ledger if r["kind"] == "EXIT"]
    assert len(exits) == 1
    # 应等到 hour20 真正穿越原始结构失效位101,下一小时开盘全仓退出。
    assert exits[0]["decision_ts"] == T0 + 20 * H
    assert exits[0]["ts"] == T0 + 21 * H
    assert exits[0]["qty"] == pytest.approx(1.5)
    assert len([r for r in res.ledger if r["kind"] == "CANCEL"]) == 1
    assert len([r for r in res.ledger if r["kind"] == "REDUCE"]) == 0


def test_execution_ledger_replays_only_broker_confirmed_fills(tmp_path):
    """E7 intent and resting orders have no qty until actual maker/taker fills."""
    from market_operator.orders import OrderLedger

    with OrderLedger(tmp_path / "actual-executions.db") as journal:
        kw = {"execution_ledger": journal, "ledger_run_id": "maker-reduce-exit"}
        result = _run(main_hours(), _open_script(10), **kw)
        fills = [r for r in result.ledger if r["kind"] in
                 ("OPEN", "LIMIT_FILL", "REDUCE", "EXIT", "ADD", "LIQUIDATION")]
        assert len(fills) == 3
        assert journal.net_positions() == {}
        assert journal.dispatch_allowed()
        # Replaying the identical run must never double-book the same fills.
        _run(main_hours(), _open_script(10), **kw)
        assert journal.net_positions() == {}
        assert journal.dispatch_allowed()
        total = sum(journal.execution_totals(
            f"replay:maker-reduce-exit:{r['symbol']}:{r['seq']}")[0] for r in fills)
        assert float(total) == pytest.approx(sum(r["qty"] for r in fills))
