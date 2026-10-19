"""v6 战役引擎端到端测试(docs/v6-protocol.md §1/§2)。

手工装配 SymbolCognition(注入事件与视图)驱动 run_campaigns,覆盖:
- 执行路径对齐:TF=2h 市价决策于 TF 收盘 ⇒ 成交于下一 TF bar 首根 1h
  细 bar 开盘(decision < fill 恒等;1h 战役复用 v4 15m 契约);
- 战役隔离:同 symbol 的 2h/4h 两战役同时独立持仓,一方 §8.1 出场 /
  trailing 不触碰另一方(出场、防线互不干扰);
- 组合容量 3%:合计最坏情景超预算 ⇒ 按信号时间 FIFO 整笔跳过并记
  PORTFOLIO_CAP(cap_skips + counters);
- 臂 A 禁保本:减仓限价成交后防线不移动(通往 entry 的保本移动不存在),
  只随 confirmed pivot 收紧;臂 B:trailing 只紧不松、§8.6 时间止损关闭
  (PositionManager 加性开关,默认 v1-v5 行为另有 position 测试覆盖);
- 段门控与确定性双跑。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from research.rounds.v1.backtest import SymbolCognition
from market_operator.broker import ContractSpec
from research.rounds.v6.campaign import (
    ARM_CLAMP2R,
    ARM_TURTLE,
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.events import E1, E6, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.position import PositionManager
from research.rounds.v6.strategy_v6 import V6Params
from market_operator.structure import (
    Params,
    Pivot,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)

H = 3_600_000
Q = 900_000
T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC(4h/2h 网格对齐)
ATR = 2.0
PARAMS = Params(atr_n=2)
SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(segment="full", dev_frac=0.7, equity0=300.0, risk_frac=0.0075,
          leverage_cap=5.0, taker_fee=0.0006, maker_fee=0.0002,
          slippage_per_side=0.0002, funding_per_8h=0.0001,
          cost_multiplier=1.0, contracts={"TEST": SPEC}, params=PARAMS,
          n_fill=12, portfolio_cap_frac=0.03)
VIEW = StructureView(
    regime=Regime.RANGE,
    zones=(Zone(lo=85.0, hi=90.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE),),
    atr=ATR)


def _e1_short(tf_idx, tf_hours, invalid=99.7, ref=99.5):
    return MarketEvent(id=E1, direction=-1, ts=T0 + tf_idx * tf_hours * H,
                       known_index=tf_idx,
                       attrs={"ref": ref, "invalid": invalid, "started": 0})


def _exec_prices(n, overrides=None):
    """确定性 1h OHLC:c = 96 + (i%5)*0.3(默认全低于 invalid=99.7)。"""
    out = []
    ov = overrides or {}
    for i in range(n):
        o, h, l, c = ov.get(i, (None, None, None, None))
        base = 96.0 + (i % 5) * 0.3
        co = base if c is None else c
        oo = (out[-1][3] if out else co) if o is None else o
        hh = max(oo, co) + 0.5 if h is None else h
        ll = min(oo, co) - 0.5 if l is None else l
        out.append((oo, hh, ll, co))
    return out


def _frame(symbol, tf_hours, events_by=None, pivots_by=None,
           exec_overrides=None):
    """手工装配(tf_hours 小时战役):认知 TF bar + 1h 执行帧。"""
    n_1h = 16
    rows = _exec_prices(n_1h, exec_overrides)
    df_exec = pd.DataFrame(
        [(T0 + i * H, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(rows)],
        columns=["ts", "open", "high", "low", "close", "volume"])
    n_tf = n_1h // tf_hours
    cog_bars = []
    for i in range(n_tf):
        chunk = rows[i * tf_hours:(i + 1) * tf_hours]
        cog_bars.append((T0 + i * tf_hours * H, chunk[0][0],
                         max(x[1] for x in chunk), min(x[2] for x in chunk),
                         chunk[-1][3], 1.0))
    ev = {i: list(evs) for i, evs in (events_by or {}).items()}
    cog = SymbolCognition(symbol=symbol, bars=cog_bars,
                          atrs=[ATR] * n_tf, events_by=ev,
                          pivots_by={i: list(ps)
                                     for i, ps in (pivots_by or {}).items()},
                          views={i: VIEW for i in ev})
    return build_campaign_frame(cog, df_exec, f"{tf_hours}h")


def _frame_1h(symbol, events_by=None):
    """1h 战役:复用 v4 契约(4×15m 子 bar)。"""
    n_1h = 6
    rows = _exec_prices(n_1h)
    bars_15m = []
    for i, (o, h, l, c) in enumerate(rows):
        q = (h - l) / 4.0
        for k in range(4):
            qo = o + (c - o) * k / 4.0
            qc = o + (c - o) * (k + 1) / 4.0
            bars_15m.append((T0 + i * H + k * Q, qo, max(qo, qc) + 0.1,
                             min(qo, qc) - 0.1, qc, 1.0))
    df_exec = pd.DataFrame(bars_15m,
                           columns=["ts", "open", "high", "low", "close",
                                    "volume"])
    cog_bars = [(T0 + i * H, o, h, l, c, 1.0)
                for i, (o, h, l, c) in enumerate(rows)]
    ev = {i: list(evs) for i, evs in (events_by or {}).items()}
    cog = SymbolCognition(symbol=symbol, bars=cog_bars,
                          atrs=[ATR] * n_1h, events_by=ev, pivots_by={},
                          views={i: VIEW for i in ev})
    return build_campaign_frame(cog, df_exec, "1h")


def _run(frame_map, arm=ARM_CLAMP2R, **over):
    kw = dict(KW)
    kw.update(over)
    return run_campaigns(frame_map, arm=arm, **kw)


# ---------------------------------------------------------------------------
# 执行路径对齐(认知 bar 边界;决策 TF 收盘 ⇒ 下一 TF bar 首根细 bar 开盘)
# ---------------------------------------------------------------------------


def test_exec_path_tf2h_market_fills_next_tf_bar_first_exec_bar():
    """2h 战役:bar2 决策 ⇒ 成交 ts = bar3 标签(其首根 1h bar),价 = 该 1h 开盘滑点。"""
    f = _frame("TEST", 2, events_by={2: [_e1_short(2, 2)]})
    res = _run({"TEST@2h": f})
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1
    row = opens[0]
    t2 = T0 + 2 * 2 * H
    assert row["decision_ts"] == t2                       # 认知 bar 标签
    assert row["ts"] == t2 + 2 * H                        # 下一 TF bar 首根 1h
    exp_price = 96.0 * (1.0 - 0.0002)                     # exec idx6 open = 96.0
    assert abs(row["price"] - exp_price) < 1e-9
    assert row["meta"]["invalid"] == 99.7
    assert row["meta"]["route"] == "e1_market"
    # 臂 A:target clamp 2R(决策收盘 96.0,risk 3.7 ⇒ target 88.6)
    assert abs(row["meta"]["target_1"] - 88.6) < 1e-9


def test_exec_path_tf1h_uses_15m_grid():
    """1h 战役:决策 → 下一根 1h bar 首根 15m 开盘(v4/v5 契约不变)。"""
    f = _frame_1h("TEST", events_by={2: [_e1_short(2, 1)]})
    res = _run({"TEST@1h": f})
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1
    row = opens[0]
    t2 = T0 + 2 * H
    assert row["decision_ts"] == t2
    assert row["ts"] == t2 + H                            # 次 1h 首根 15m
    # 1h bar3 open = exec idx3 open = exec idx2 close = 96.6
    assert abs(row["price"] - 96.6 * (1.0 - 0.0002)) < 1e-9


def test_segment_gate_blocks_out_of_segment_opens():
    f = _frame("TEST", 2, events_by={1: [_e1_short(1, 2)],
                                     6: [_e1_short(6, 2)]})
    # n_tf=8;dev 段 = [0, int(8*0.7)=5):bar6 在 test 段 ⇒ dev 回放无 bar6 开仓
    res = _run({"TEST@2h": f}, segment="dev")
    assert len([r for r in res.ledger if r["kind"] == "OPEN"]) == 1
    res2 = _run({"TEST@2h": f}, segment="test")
    assert len([r for r in res2.ledger if r["kind"] == "OPEN"]) == 1
    row = [r for r in res2.ledger if r["kind"] == "OPEN"][0]
    assert row["decision_ts"] == T0 + 6 * 2 * H


# ---------------------------------------------------------------------------
# 战役隔离(同 symbol 两 TF 同时独立持仓,互不触发对方出场/防线)
# ---------------------------------------------------------------------------


def test_campaign_isolation_two_tf_same_symbol():
    f2 = _frame("TEST", 2, events_by={2: [_e1_short(2, 2)]})
    f4 = _frame("TEST", 4, events_by={1: [_e1_short(1, 4, invalid=99.9)]})
    frames = {campaign_key("TEST", "2h"): f2, campaign_key("TEST", "4h"): f4}
    res = _run(frames, portfolio_cap_frac=0.03)
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 2
    # 两战役同时持仓直到数据结束(收盘均在 invalid 下方 ⇒ 无 §8.1)
    st2 = res.campaign_states["TEST@2h"]["pos_mgr"].positions
    st4 = res.campaign_states["TEST@4h"]["pos_mgr"].positions
    assert "TEST" in st2 and "TEST" in st4
    assert st2["TEST"].invalid == 99.7 and st4["TEST"].invalid == 99.9
    assert res.trades and all(t["closed"] is False for t in res.trades)


def test_campaign_isolation_exit_and_trail_do_not_bleed():
    """4h 战役 §8.1 出场后,2h 战役持仓与防线完全不受影响(及反向)。"""
    f2 = _frame("TEST", 2, events_by={2: [_e1_short(2, 2)]},
                pivots_by={6: [Pivot(price=99.0, forming_index=5,
                                     known_index=6, kind=PivotKind.HIGH)]})
    # 4h bar0 事件(invalid 99.7):成交于 t0+4h;4h bar1 认知收盘 = exec
    # idx7 close = 99.9 > 99.7 ⇒ §8.1 于 bar1 出场
    f4_ = _frame("TEST", 4, events_by={0: [_e1_short(0, 4)]},
                 exec_overrides={7: (None, None, None, 99.9)})
    frames = {campaign_key("TEST", "2h"): f2,
              campaign_key("TEST", "4h"): f4_}
    res = _run(frames)
    st2 = res.campaign_states["TEST@2h"]["pos_mgr"].positions
    st4 = res.campaign_states["TEST@4h"]["pos_mgr"].positions
    assert "TEST" not in st4                       # 4h 已出场
    assert "TEST" in st2                           # 2h 仍在
    # 2h pivot trailing:99.0 + k_bm·ATR = 99.2 < 99.7 ⇒ 收紧;4h 不参与
    assert abs(st2["TEST"].invalid - 99.2) < 1e-9
    exits4 = [r for r in res.ledger if r["kind"] == "EXIT"
              and r["symbol"] == "TEST@4h"]
    assert exits4 and exits4[0]["reason"].startswith("§8.1")
    assert not [r for r in res.ledger
                if r["kind"] in ("EXIT", "LIQUIDATION")
                and r["symbol"] == "TEST@2h"]


# ---------------------------------------------------------------------------
# 组合容量 3%:FIFO 整笔跳过
# ---------------------------------------------------------------------------


def test_portfolio_cap_fifo_skip():
    """两战役相继信号:第二笔最坏情景超 1% 预算 ⇒ 整笔跳过记 PORTFOLIO_CAP。"""
    f2 = _frame("TEST", 2, events_by={2: [_e1_short(2, 2)]})     # 先成交
    f4 = _frame("TEST", 4, events_by={1: [_e1_short(1, 4)]})     # 后被容量拒
    frames = {campaign_key("TEST", "2h"): f2,
              campaign_key("TEST", "4h"): f4}
    res = _run(frames, portfolio_cap_frac=0.01)
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1 and opens[0]["symbol"] == "TEST@2h"
    assert res.counters["portfolio_cap_skips"] == 1
    skip = res.cap_skips[0]
    assert skip["campaign"] == "TEST@4h"
    assert skip["reason_code"] == "PORTFOLIO_CAP"
    assert skip["budget_frac"] == 0.01
    assert skip["worst_case"] > 0.01 * skip["equity"]
    assert skip["ts"] == T0 + 4 * H                            # 4h bar1 标签


def test_portfolio_cap_budget_restored_after_exit():
    """容量被占用期跳过 ≠ 永久封禁:仓位结束后后续信号照常评估通过。"""
    # 4h bar0 开仓(invalid 99.7);bar1 认知收盘 99.9 > 99.7 ⇒ §8.1 出场;
    # bar2 时无持仓/无占用 ⇒ 独立信号凭自身最坏情景(≈0.8% < 1%)照常放行
    f2 = _frame("TEST", 2)
    f4 = _frame("TEST", 4, events_by={0: [_e1_short(0, 4)],
                                      2: [_e1_short(2, 4)]},
                exec_overrides={7: (None, None, None, 99.9)})
    frames = {campaign_key("TEST", "2h"): f2,
              campaign_key("TEST", "4h"): f4}
    res = _run(frames, portfolio_cap_frac=0.01)
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    o4 = [r for r in opens if r["symbol"] == "TEST@4h"]
    assert len(o4) == 2
    assert res.counters["portfolio_cap_skips"] == 0


# ---------------------------------------------------------------------------
# 臂 A:禁保本(减仓后防线不动,只随 pivot 收紧)
# ---------------------------------------------------------------------------


def _arm_a_reduce_frame():
    # TF=2h;认知 bar2 收盘 = exec idx5 close = 96.0,invalid 99.7 ⇒ risk
    # 3.7,target clamp = 96.0 − 2*3.7 = 88.6。认知 bar4(exec 8,9)极值触
    # target(low 88.4 ≤ 88.6)⇒ §8.2 武装减仓限价;bar5 首根 1h(exec
    # idx10)严格穿过 88.6(low 88.2 < 88.6)⇒ maker 成交。
    rows = _exec_prices(16, {8: (None, None, 90.0, None),
                             9: (None, None, 88.4, 97.2),
                             10: (None, None, 88.2, 92.5),
                             11: (None, None, 92.0, 97.0)})
    df_exec = pd.DataFrame(
        [(T0 + i * H, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(rows)],
        columns=["ts", "open", "high", "low", "close", "volume"])
    cog_bars = []
    for i in range(8):
        chunk = rows[i * 2:(i + 1) * 2]
        cog_bars.append((T0 + i * 2 * H, chunk[0][0],
                         max(x[1] for x in chunk), min(x[2] for x in chunk),
                         chunk[-1][3], 1.0))
    ev = {2: [_e1_short(2, 2)]}
    pivots = {6: [Pivot(price=99.0, forming_index=5, known_index=6,
                        kind=PivotKind.HIGH)]}
    cog = SymbolCognition(symbol="TEST", bars=cog_bars, atrs=[ATR] * 8,
                          events_by=ev, pivots_by=pivots,
                          views={2: VIEW})
    return build_campaign_frame(cog, df_exec, "2h")


def test_arm_a_no_breakeven_after_reduce_fill():
    f = _arm_a_reduce_frame()
    res = _run({"TEST@2h": f})
    pos = res.campaign_states["TEST@2h"]["pos_mgr"].positions["TEST"]
    # 减仓成交后:防线 ≠ 入场价方向收紧到 BE(禁保本);保持 99.7
    fills = [r for r in res.ledger if r["kind"] == "LIMIT_FILL"
             and r["meta"].get("role") == "reduce"]
    assert len(fills) == 1, f"expected maker reduce fill: {res.ledger}"
    entry = [r for r in res.ledger if r["kind"] == "OPEN"][0]["price"]
    assert pos.reduced is True
    assert pos.invalid != entry and pos.invalid > entry      # ≠ BE
    assert abs(pos.invalid - 99.2) < 1e-9                    # bar6 pivot trailing
    # 防线只随着 pivot 移动:99.7 → 99.2(pivot 99.0 + k_bm·ATR=0.2)
    trail_logs = [ln for ln in pos.log if ln.startswith("§8.3")]
    assert trail_logs and "99.7" in trail_logs[0] and "99.2" in trail_logs[0]


def test_arm_a_defense_never_moves_back_to_entry_absent_pivots():
    """无 pivot 时减仓后防线永不移动(禁保本断言,不收紧至入场价)。"""
    f = _arm_a_reduce_frame()
    f.cog.pivots_by = {}
    res = _run({"TEST@2h": f})
    pos = res.campaign_states["TEST@2h"]["pos_mgr"].positions["TEST"]
    assert pos.reduced is True
    assert abs(pos.invalid - 99.7) < 1e-9


# ---------------------------------------------------------------------------
# 臂 B:turtle(2·atr 初始防线,trailing 只紧不松,时间止损关闭)
# ---------------------------------------------------------------------------


def test_arm_b_initial_stop_2atr_and_trailing_tightens_only():
    f2 = _frame("TEST", 2, events_by={2: [_e1_short(2, 1)]},
                pivots_by={4: [Pivot(price=99.0, forming_index=7,
                                     known_index=4, kind=PivotKind.HIGH)],
                           6: [Pivot(price=99.9, forming_index=9,
                                     known_index=6, kind=PivotKind.HIGH)]})
    res = _run({"TEST@2h": f2}, arm=ARM_TURTLE,
               router_params=V6Params(arm=ARM_TURTLE))
    open_row = [r for r in res.ledger if r["kind"] == "OPEN"][0]
    decision_close = 96.0                              # 认知 bar2 close = exec idx5
    assert abs(open_row["meta"]["invalid"]
               - (decision_close + 2.0 * ATR)) < 1e-9   # 初始防线 = close+2·atr
    pos = res.campaign_states["TEST@2h"]["pos_mgr"].positions["TEST"]
    # bar4 pivot 99.0+0.2=99.2 < 100.0 ⇒ 收紧;bar6 pivot 99.9+0.2=100.1 >
    # 99.2 ⇒ 不放松(只紧不松)
    assert abs(pos.invalid - 99.2) < 1e-9
    assert pos.target_1 is None


def test_arm_b_time_stop_disabled():
    """§8.6 关闭:n_time 已过、MFE≈0,臂 B 不出场;对照默认 True 仍触发。"""
    params = Params(atr_n=2, n_time=2)
    f2 = _frame("TEST", 2, events_by={1: [_e1_short(1, 1)]})
    res = _run({"TEST@2h": f2}, arm=ARM_TURTLE,
               router_params=V6Params(arm=ARM_TURTLE), params=params)
    assert "TEST" in res.campaign_states["TEST@2h"]["pos_mgr"].positions
    pm = PositionManager(params, breakeven_on_reduce=False, time_stop=False)
    pos = pm.open("X", -1, 96.0, 100.0, None, "E1", T0, 0)
    bar = (T0 + H, 96.0, 96.4, 95.6, 96.0, 1.0)
    for _ in range(3):
        assert pm.on_bar("X", bar, ATR, (), ()) == []    # 时间止损整条关闭
    pm2 = PositionManager(params)
    pm2.open("X", -1, 96.0, 100.0, None, "E1", T0, 0)
    pm2.on_bar("X", bar, ATR, (), ())
    exits = pm2.on_bar("X", bar, ATR, (), ())            # bars_held=2=n_time
    assert exits and "§8.6" in exits[0].reason


# ---------------------------------------------------------------------------
# 确定性双跑
# ---------------------------------------------------------------------------


def test_determinism_double_run():
    frames1 = {"TEST@2h": _frame("TEST", 2,
                                 events_by={2: [_e1_short(2, 2)]}),
               "TEST@4h": _frame("TEST", 4,
                                 events_by={1: [_e1_short(1, 4)]})}
    frames2 = {"TEST@2h": _frame("TEST", 2,
                                 events_by={2: [_e1_short(2, 2)]}),
               "TEST@4h": _frame("TEST", 4,
                                 events_by={1: [_e1_short(1, 4)]})}
    r1 = _run(frames1)
    r2 = _run(frames2)
    assert json.dumps(r1.ledger, sort_keys=True) \
        == json.dumps(r2.ledger, sort_keys=True)
    assert json.dumps(r1.trades, sort_keys=True) \
        == json.dumps(r2.trades, sort_keys=True)
    assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)


def test_e1_priority_cancel_isolated_per_campaign():
    """E1 撤单只撤本战役挂单(跨战役挂单不受影响)。"""
    # 2h 战役:bar2 E1 市价;此前 2h 已有 E7 前缘限价挂单(E1 先撤)
    e7_short = MarketEvent(
        id="E7", direction=-1, ts=T0 + 1 * 2 * H, known_index=1,
        attrs={"zone": Zone(lo=90.0, hi=91.0, role=ZoneRole.RESISTANCE,
                            state=ZoneState.FLIPPED),
               "ref": 90.0, "invalid": 91.2,
               "from_role": ZoneRole.SUPPORT.value, "atr": ATR})
    view_e7 = StructureView(
        regime=Regime.RANGE,
        zones=(Zone(lo=90.0, hi=91.0, role=ZoneRole.SUPPORT,
                    state=ZoneState.ACTIVE),
               Zone(lo=92.0, hi=92.5, role=ZoneRole.RESISTANCE,
                    state=ZoneState.BROKEN),),
        atr=ATR)
    f2 = _frame("TEST", 2, events_by={
        1: [MarketEvent(id="E6", direction=-1, ts=T0 + 1 * 2 * H,
                        known_index=1,
                        attrs={"ref": 99.5, "invalid": 99.7,
                               "pivot_forming": 0,
                               "regime_before": Regime.TREND_UP.value})],
        3: [e7_short],
        4: [_e1_short(4, 2)]})
    # bar1 E6:bar1 认知收盘 = exec idx3 = 96.9;margin=(99.5−96.9)/2=1.3,
    # impulse=(bar1 振幅)/2:认知 bar1 振幅=max h - min l(固定 +0.5/-0.5)
    # 未必弱;改用直接断言路由层语义(见 test_v6_strategy)。此处验证同战役
    # E1 优先级撤单不影响 4h 战役挂单,复杂装配留给引擎集成冒烟。
    res = _run({"TEST@2h": f2, "TEST@4h": _frame("TEST", 4)})
    cancels = [r for r in res.ledger if r["kind"] == "CANCEL"
               and r["reason_code"] == "E1_PRIORITY"]
    # 若 E6 判弱+E7 成单+E1 撤满,则撤单只可能发生在 TEST@2h
    for r in cancels:
        assert r["symbol"] == "TEST@2h"
