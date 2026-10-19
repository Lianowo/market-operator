"""v5 回测引擎端到端测试(docs/v5-protocol.md §1)。

手工装配 SymbolCognition(注入 E6/E7/E1 事件与视图,1h bar 由 4 根 15m 子
bar 聚合)驱动 run_backtest_v5,覆盖:WEAK-E6 不入场 → 同 episode E7 前缘
限价(15m 严格穿过成交、maker)→ §8.2 减仓限价 → §8.1 退出逐腿精确;同
episode 流中 E1 优先级撤挂单(CANCEL/E1_PRIORITY)→ 市价次开盘;STRONG-E6
市价次开盘(恰 0.5/1.5 边界);WEAK→E7 挂单超时 EXPIRED = 没有这笔交易;
段门控;确定性双跑。统一合约与成本先验同 test_v4_engine.py。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from research.rounds.v1.backtest import SymbolCognition
from research.rounds.v4.backtest_v4 import build_exec_frame
from research.rounds.v5.backtest_v5 import run_backtest_v5
from market_operator.broker import ContractSpec
from market_operator.events import E1, E6, E7, MarketEvent
from market_operator.hypothesis import StructureView
from research.rounds.v5.strategy_v5 import V5Params, V5Router
from market_operator.structure import Params, Regime, Zone, ZoneRole, ZoneState

H = 3_600_000
Q = 900_000
T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC
ATR = 2.0
PARAMS = Params(atr_n=2)
SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(dev_frac=0.7, risk_frac=0.0075, leverage_cap=5.0,
          taker_fee=0.0006, maker_fee=0.0002, slippage_per_side=0.0002,
          funding_per_8h=0.0001, cost_multiplier=1.0,
          contracts={"TEST": SPEC}, params=PARAMS, n_fill=12)
VIEW = StructureView(
    regime=Regime.RANGE,
    zones=(Zone(lo=90.0, hi=95.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE),),
    atr=ATR)


# ---------------------------------------------------------------------------
# 合成帧与手工认知装配
# ---------------------------------------------------------------------------


def calm(*closes, hi_pad=0.4, lo_pad=0.4):
    out, prev = [], None
    for c in closes:
        o = prev if prev is not None else c
        out.append((o, max(o, c) + hi_pad, min(o, c) - lo_pad, c))
        prev = c
    return out


def _e7_short(hour, lo=99.5, hi=99.8, invalid=100.0):
    return MarketEvent(
        id=E7, direction=-1, ts=T0 + hour * H, known_index=hour,
        attrs={"zone": Zone(lo=lo, hi=hi, role=ZoneRole.RESISTANCE,
                            state=ZoneState.FLIPPED),
               "ref": lo, "invalid": invalid,
               "from_role": ZoneRole.SUPPORT.value, "atr": ATR})


def _e6_short(hour, l_star=99.5):
    return MarketEvent(id=E6, direction=-1, ts=T0 + hour * H, known_index=hour,
                       attrs={"ref": l_star, "invalid": l_star + 0.2,
                              "pivot_forming": 0,
                              "regime_before": Regime.TREND_UP.value})


def _e1_short(hour, invalid=100.0, ref=99.5):
    return MarketEvent(id=E1, direction=-1, ts=T0 + hour * H, known_index=hour,
                       attrs={"ref": ref, "invalid": invalid, "started": 0})


def _frame(hours, events_by):
    """hours[i] = [(o,h,l,c) × 4];events_by[bar_index] = [MarketEvent]。"""
    rows_1h, rows_15m = [], []
    for i, subs in enumerate(hours):
        assert len(subs) == 4
        o = subs[0][0]
        h = max(s[1] for s in subs)
        l = min(s[2] for s in subs)
        c = subs[3][3]
        rows_1h.append((T0 + i * H, o, h, l, c, 4.0))
        for k, (so, sh, sl, sc) in enumerate(subs):
            rows_15m.append((T0 + i * H + k * Q, so, sh, sl, sc, 1.0))
    cols = ["ts", "open", "high", "low", "close", "volume"]
    df_1h = pd.DataFrame(rows_1h, columns=cols)
    df_15m = pd.DataFrame(rows_15m, columns=cols)
    bars = [(int(r.ts), float(r.open), float(r.high), float(r.low),
             float(r.close), float(r.volume))
            for r in df_1h.itertuples(index=False)]
    cog = SymbolCognition(symbol="TEST", bars=bars, atrs=[ATR] * len(bars),
                          events_by={i: list(evs) for i, evs in events_by.items()},
                          pivots_by={},
                          views={i: VIEW for i in events_by})
    return build_exec_frame(cog, df_15m)


def _run(hours, events_by, segment="full", **over):
    kw = dict(KW)
    equity0 = over.pop("equity0", 300.0)
    kw.update(over)
    return run_backtest_v5({"TEST": _frame(hours, events_by)}, segment=segment,
                           equity0=equity0, router=V5Router(V5Params()), **kw)


# ---------------------------------------------------------------------------
# 主场景 hours 蓝图:WEAK-E6@10 → E7@12 → 限价成交 → 减仓 → §8.1
# ---------------------------------------------------------------------------


def main_hours():
    hours = []
    for _ in range(8):
        hours.append(calm(100.0, 100.0, 100.0, 100.0, hi_pad=0.05, lo_pad=0.05))
    hours.append(calm(98.0, 98.0, 98.0, 98.0))          # 8
    hours.append(calm(98.0, 98.3, 97.9, 98.0))          # 9
    # 10 WEAK-E6 决策小时(收盘 99.2:margin=(99.5−99.2)/2=0.15,impulse=0.75)
    hours.append([(98.0, 99.0, 97.9, 98.5), (98.5, 99.2, 98.4, 99.0),
                  (99.0, 99.4, 98.8, 99.1), (99.1, 99.3, 98.9, 99.2)])
    hours.append(calm(99.2, 99.3, 98.9, 99.0, hi_pad=0.1, lo_pad=0.2))  # 11
    # 12 E7 决策小时(收盘 98.0;全小时最高 99.2 < 限价 99.5)
    hours.append([(99.0, 99.2, 97.8, 98.2), (98.2, 98.3, 97.6, 97.9),
                  (97.9, 98.2, 97.7, 98.0), (98.0, 98.1, 97.8, 98.0)])
    # 13 穿越小时:q0 恰触 99.5 持平(不成交),q1 99.6 严格穿过 ⇒ 成交
    hours.append([(98.0, 99.5, 97.9, 99.4), (99.4, 99.6, 99.0, 99.1),
                  (99.1, 99.3, 98.8, 99.0), (99.0, 99.2, 98.6, 98.8)])
    hours.append(calm(98.8, 98.4, 97.9, 97.8))          # 14
    hours.append(calm(97.8, 97.2, 96.8, 96.5))          # 15
    # 16 §8.2 触发(1h low 94.99 ≤ 目标 95.0)
    hours.append([(96.5, 96.8, 96.0, 96.2), (96.2, 96.3, 94.99, 95.4),
                  (95.4, 95.6, 95.3, 95.4), (95.4, 95.5, 95.2, 95.4)])
    # 17 减仓成交:q0 low==95.0 持平;q1 94.9 严格穿过 ⇒ buy limit @95.0
    hours.append([(95.4, 95.5, 95.0, 95.2), (95.2, 95.4, 94.9, 95.1),
                  (95.1, 95.3, 95.05, 95.2), (95.2, 95.3, 95.05, 95.3)])
    hours.append(calm(95.3, 96.4, 97.9, 98.4))          # 18
    hours.append(calm(98.4, 98.8, 99.0, 98.6))          # 19
    hours.append(calm(98.6, 100.2, 100.4, 100.5))       # 20 收盘 100.5 > 99.5 保本
    hours.append(calm(100.5, 100.5, 100.5, 100.5))      # 21 退出成交 = 1h 开盘 100.5
    for _ in range(18):
        hours.append(calm(100.5, 100.5, 100.5, 100.5, hi_pad=0.05, lo_pad=0.05))
    return hours


MAIN_EVENTS = {10: [_e6_short(10)], 12: [_e7_short(12)]}


def test_weak_e6_to_e7_limit_flow_leg_by_leg_exact():
    res = _run(main_hours(), MAIN_EVENTS)
    c = res.counters
    for key, want in (("go_events", 2), ("open_intents", 1), ("opens_filled", 0),
                      ("limits_armed", 1), ("limit_fills_entry", 1),
                      ("limit_expired", 0), ("reduce_limits_armed", 1),
                      ("reduce_limit_fills", 1), ("reduce_timeout_taker", 0),
                      ("e1_priority_cancels", 0), ("liquidations", 0),
                      ("e6_weak_registered", 1), ("e7_matched_episode", 1),
                      ("e6_strong_quality", None)):
        if want is None:
            assert c.get(key) is None, (key, c.get(key))
        else:
            assert c.get(key) == want, (key, c.get(key), want)
    kinds = [r["kind"] for r in res.ledger]
    assert kinds == ["LIMIT_FILL", "LIMIT_FILL", "EXIT"]

    ent, red, ex = res.ledger
    # 入场:E7 决策 hour12 ⇒ 成交 hour13 子 bar 1(严格穿过 99.6 > 99.5)
    assert ent["decision_ts"] == T0 + 12 * H
    assert ent["ts"] == T0 + 13 * H + 1 * Q
    assert ent["price"] == 99.5 and ent["slippage_cost"] == 0.0
    assert ent["qty"] == 4.5                             # 2.25/0.5 向下取整
    assert ent["fee"] == pytest.approx(4.5 * 99.5 * 0.0002, rel=1e-12)  # maker
    assert ent["meta"]["fill_wait_bars"] == 2            # q0 持平,q1 穿过
    assert ent["reason_code"] == "E7"
    assert ent["meta"]["route"] == "weak_e7_limit"
    assert ent["meta"]["l_star"] == 99.5 and ent["meta"]["weak_bars"] == 2
    assert ent["meta"]["e6_margin"] == pytest.approx(0.15, rel=1e-12)
    assert ent["meta"]["e6_impulse"] == pytest.approx(0.75, rel=1e-12)
    # 减仓(§8.2:决策 hour16 ⇒ 成交 hour17 子 bar 1,目标前缘 95.0,maker)
    assert red["decision_ts"] == T0 + 16 * H
    assert red["ts"] == T0 + 17 * H + 1 * Q
    assert red["side"] == "buy" and red["price"] == 95.0
    assert red["qty"] == 2.25                            # floor(0.5×4.5)
    assert red["realized_pnl"] == pytest.approx((99.5 - 95.0) * 2.25, rel=1e-12)
    assert red["fee"] == pytest.approx(2.25 * 95.0 * 0.0002, rel=1e-12)
    # 退出(§8.1 保本:决策 hour20 收盘 100.5 > 99.5 ⇒ hour21 首根 15m 开盘)
    assert ex["decision_ts"] == T0 + 20 * H
    assert ex["ts"] == T0 + 21 * H
    assert ex["price"] == pytest.approx(100.5 * 1.0002, abs=1e-12)
    assert ex["qty"] == 2.25
    assert ex["fee"] == pytest.approx(2.25 * ex["price"] * 0.0006, rel=1e-9)
    assert ex["funding_delta"] < 0.0                     # 全部资金费结清
    for r in res.ledger:                                 # 因果硬约束
        assert r["ts"] > r["decision_ts"]

    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["route"] == "weak_e7_limit" and t["entry_mode"] == "limit"
    assert t["limit_price"] == 99.5 and t["entry"] == 99.5
    assert t["weak_bars"] == 2 and t["l_star"] == 99.5
    assert t["closed"] and t["exit_kind"] == "EXIT"
    assert t["hold_hours"] == pytest.approx(7.75, rel=1e-12)   # 13:15→21:00
    gross = (99.5 - 95.0) * 2.25 + (99.5 - 100.5201) * 2.25
    assert t["gross_pnl"] == pytest.approx(gross, rel=1e-9)
    fees = (4.5 * 99.5 * 0.0002 + 2.25 * 95.0 * 0.0002
            + 2.25 * 100.5201 * 0.0006)
    assert t["net_pnl"] == pytest.approx(gross - fees - (-ex["funding_delta"]),
                                         rel=1e-9)
    assert res.equity_rows[-1][1] == pytest.approx(300.0 + t["net_pnl"], rel=1e-9)
    recon = 300.0 + sum(x["realized_pnl"] - x["fee"] + x["funding_delta"]
                        for x in res.ledger)
    assert recon == pytest.approx(res.equity_rows[-1][2], rel=1e-9)   # 账本对账


# ---------------------------------------------------------------------------
# E1 优先级:撤未成交挂单(CANCEL/E1_PRIORITY)⇒ 市价次 1h 开盘成交
# ---------------------------------------------------------------------------


def e1_hours():
    hours = main_hours()
    # hour13/14 不穿越(最高 99.2 < 99.5),挂单存活至 hour15
    hours[13] = calm(98.8, 99.0, 98.9, 98.9, hi_pad=0.3, lo_pad=0.3)
    hours[14] = calm(98.9, 98.8, 98.7, 98.8, hi_pad=0.3, lo_pad=0.3)
    hours[15] = [(98.8, 99.0, 97.9, 98.1), (98.1, 98.3, 97.7, 98.0),
                 (98.0, 98.2, 97.7, 98.0), (98.0, 98.1, 97.8, 98.0)]  # E1 决策 c=98
    hours[16] = calm(97.9, 97.9, 97.6, 97.6)           # E1 市价成交 = q0 开盘 97.9
    for i in range(17, 20):
        hours[i] = calm(97.6, 97.4, 97.5, 97.5, hi_pad=0.3, lo_pad=0.3)
    hours[20] = calm(97.5, 101.0, 100.8, 100.6)        # 收盘 100.6 > invalid 100
    hours[21] = calm(100.6, 100.6, 100.6, 100.6)       # §8.1 退出成交 = 1h 开盘
    for i in range(22, 40):
        hours[i] = calm(100.6, 100.6, 100.6, 100.6, hi_pad=0.05, lo_pad=0.05)
    return hours


def test_e1_priority_cancels_resting_entry_limit_then_market_fills():
    events = {10: [_e6_short(10)], 12: [_e7_short(12)], 15: [_e1_short(15)]}
    res = _run(e1_hours(), events)
    kinds = [r["kind"] for r in res.ledger]
    assert kinds == ["CANCEL", "OPEN", "EXIT"]
    cancel, op, ex = res.ledger
    assert cancel["reason_code"] == "E1_PRIORITY"
    assert cancel["ts"] == T0 + 15 * H > cancel["decision_ts"] == T0 + 12 * H
    assert cancel["price"] == 99.5
    assert op["decision_ts"] == T0 + 15 * H
    assert op["ts"] == T0 + 16 * H                       # 次 1h 首根 15m 开盘
    assert op["price"] == pytest.approx(97.9 * 0.9998, abs=1e-12)
    assert op["meta"]["route"] == "e1_market"
    assert ex["kind"] == "EXIT"
    c = res.counters
    assert c["e1_priority_cancels"] == 1 and c["limit_fills_entry"] == 0
    assert c["opens_filled"] == 1 and c["limit_expired"] == 0
    assert c["e1_cancel_orders"] == 1
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t["route"] == "e1_market" and t["entry_mode"] == "market"
    assert t["entry"] == pytest.approx(97.9 * 0.9998, abs=1e-12)
    for r in res.ledger:
        assert r["ts"] > r["decision_ts"]


# ---------------------------------------------------------------------------
# STRONG-E6(恰 margin=0.5 / impulse=1.5 边界)⇒ 市价次 1h 开盘
# ---------------------------------------------------------------------------


def strong_hours():
    hours = []
    for _ in range(10):
        hours.append(calm(100.0, 100.0, 100.0, 100.0, hi_pad=0.05, lo_pad=0.05))
    # 10 E6 决策小时:收盘 98.0 ⇒ margin=(99−98)/2=0.5;振幅 100.5−97.5=3 ⇒ 1.5
    hours.append([(99.0, 100.5, 97.5, 98.0), (98.0, 98.6, 97.7, 98.2),
                  (98.2, 98.4, 97.8, 98.1), (98.1, 98.2, 97.9, 98.0)])
    hours.append(calm(98.0, 98.0, 97.6, 97.6))           # 11 市价成交 = q0 开盘 98.0
    for i in range(12, 20):
        hours.append(calm(97.6, 97.5, 97.4, 97.5, hi_pad=0.3, lo_pad=0.3))
    hours.append(calm(97.5, 101.0, 100.8, 100.6))         # 20 收盘 100.6 > invalid
    hours.append(calm(100.6, 100.6, 100.6, 100.6))        # 21 §8.1 退出成交
    for i in range(22, 40):
        hours.append(calm(100.6, 100.6, 100.6, 100.6, hi_pad=0.05, lo_pad=0.05))
    return hours


def test_strong_e6_at_boundary_market_next_open():
    events = {10: [MarketEvent(id=E6, direction=-1, ts=T0 + 10 * H,
                               known_index=10,
                               attrs={"ref": 99.0, "invalid": 100.0,
                                      "pivot_forming": 0,
                                      "regime_before": Regime.TREND_UP.value})]}
    res = _run(strong_hours(), events)
    kinds = [r["kind"] for r in res.ledger]
    assert kinds == ["OPEN", "EXIT"]
    op, _ex = res.ledger
    assert op["decision_ts"] == T0 + 10 * H and op["ts"] == T0 + 11 * H
    assert op["price"] == pytest.approx(98.0 * 0.9998, abs=1e-12)
    assert op["meta"]["route"] == "strong_e6"
    c = res.counters
    assert c["e6_strong_quality"] == 1 and c.get("e6_weak_registered") is None
    assert c["opens_filled"] == 1 and c["limits_armed"] == 0
    assert len(res.trades) == 1 and res.trades[0]["route"] == "strong_e6"


# ---------------------------------------------------------------------------
# 超时 = 没有这笔交易(WEAK→E7 挂单 12×1h 未穿越 ⇒ EXPIRED)
# ---------------------------------------------------------------------------


def test_limit_expiry_no_trade():
    hours = main_hours()
    # hour13 起全部压回 99.2 之下(12×1h 窗口 = hour13..24 永不穿越)
    hours[13] = calm(98.8, 99.0, 98.9, 98.9, hi_pad=0.3, lo_pad=0.3)
    for i in range(14, 40):
        hours[i] = calm(98.5, 98.8, 98.4, 98.4, hi_pad=0.4, lo_pad=0.3)
    res = _run(hours, MAIN_EVENTS)
    c = res.counters
    assert c["limits_armed"] == 1 and c["limit_fills_entry"] == 0
    assert c["limit_expired"] == 1
    exp = [r for r in res.ledger if r["kind"] == "EXPIRED"][0]
    assert exp["ts"] == T0 + 25 * H                    # armed 12H ⇒ 窗口 13..24H
    assert exp["decision_ts"] == T0 + 12 * H
    assert exp["meta"]["fill_wait_bars"] == 48
    assert res.trades == [] and c["min_size_skips"] == 0


# ---------------------------------------------------------------------------
# 段门控与确定性双跑
# ---------------------------------------------------------------------------


def test_segment_gating():
    test = _run(main_hours(), MAIN_EVENTS, segment="test")   # 拐点 int(40×.7)=28
    assert test.counters["open_intents"] == 0 and test.trades == []
    assert all(abs(eq - 300.0) < 1e-12 for _ts, eq, *_ in test.equity_rows)
    dev = _run(main_hours(), MAIN_EVENTS, segment="dev")
    assert dev.counters["open_intents"] == 1 and len(dev.trades) == 1
    assert dev.trades[0]["in_segment_open"]


def test_replay_determinism_double_run():
    a = _run(main_hours(), MAIN_EVENTS)
    b = _run(main_hours(), MAIN_EVENTS)
    assert json.dumps(a.ledger, sort_keys=True, default=str) == \
        json.dumps(b.ledger, sort_keys=True, default=str)
    assert a.equity_rows == b.equity_rows
    assert json.dumps(a.trades, sort_keys=True, default=str) == \
        json.dumps(b.trades, sort_keys=True, default=str)
    assert a.counters == b.counters
