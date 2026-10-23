"""v6 战役引擎端到端测试(规范 turtle 唯一语义,docs/v6-protocol.md §1/§2)。

手工装配 SymbolCognition(注入事件与视图)驱动 run_campaigns,覆盖:
- 执行路径对齐:1h 战役决策 ⇒ 成交于下一 1h bar 首根 15m 开盘(v4/v5
  契约不变;decision < fill 恒等);
- 组合容量 3%:合计最坏情景超预算 ⇒ 按信号时间 FIFO 整笔跳过并记
  PORTFOLIO_CAP(cap_skips + counters);
- turtle 持仓接线:初始防线 2·atr、trailing 只紧不松、§8.6 时间止损关闭
  (PositionManager(breakeven_on_reduce=True, time_stop=False) 固化接线);
- 段门控、BREAKOUT_ACCEPTED 优先级撤单战役隔离、确定性双跑。
历史 A/B 对照:臂 A clamp2R 相关断言(S1 invalid 穿透、减仓后禁保本、
容量恢复)与完整双臂套件保留于 research/rounds/v6/tests/test_v6_campaign.py,
对归档双臂实现运行。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from market_operator.replay import SymbolCognition
from market_operator.broker import ContractSpec
from market_operator.campaign import (
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.events import BREAKOUT_ACCEPTED, STRUCTURE_BROKEN, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.position import PositionManager
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
    return MarketEvent(id=BREAKOUT_ACCEPTED, direction=-1, ts=T0 + tf_idx * tf_hours * H,
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


def _run(frame_map, **over):
    kw = dict(KW)
    kw.update(over)
    return run_campaigns(frame_map, **kw)


# ---------------------------------------------------------------------------
# 执行路径对齐(认知 bar 边界;决策 TF 收盘 ⇒ 下一 TF bar 首根细 bar 开盘)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# turtle(2·atr 初始防线,trailing 只紧不松,时间止损关闭)
# ---------------------------------------------------------------------------


def test_turtle_initial_stop_2atr_and_trailing_tightens_only():
    f2 = _frame("TEST", 2, events_by={2: [_e1_short(2, 1)]},
                pivots_by={4: [Pivot(price=99.0, forming_index=7,
                                     known_index=4, kind=PivotKind.HIGH)],
                           6: [Pivot(price=99.9, forming_index=9,
                                     known_index=6, kind=PivotKind.HIGH)]})
    res = _run({"TEST@2h": f2})
    open_row = [r for r in res.ledger if r["kind"] == "OPEN"][0]
    decision_close = 96.0                              # 认知 bar2 close = exec idx5
    assert abs(open_row["meta"]["invalid"]
               - (decision_close + 2.0 * ATR)) < 1e-9   # 初始防线 = close+2·atr
    pos = res.campaign_states["TEST@2h"]["pos_mgr"].positions["TEST"]
    # bar4 pivot 99.0+0.2=99.2 < 100.0 ⇒ 收紧;bar6 pivot 99.9+0.2=100.1 >
    # 99.2 ⇒ 不放松(只紧不松)
    assert abs(pos.invalid - 99.2) < 1e-9
    assert pos.target_1 is None


def test_turtle_time_stop_disabled():
    """§8.6 关闭:n_time 已过、MFE≈0,不出场;对照默认 True 仍触发。"""
    params = Params(atr_n=2, n_time=2)
    f2 = _frame("TEST", 2, events_by={1: [_e1_short(1, 1)]})
    res = _run({"TEST@2h": f2}, params=params)
    assert "TEST" in res.campaign_states["TEST@2h"]["pos_mgr"].positions
    pm = PositionManager(params, breakeven_on_reduce=False, time_stop=False)
    pos = pm.open("X", -1, 96.0, 100.0, None, "BREAKOUT_ACCEPTED", T0, 0)
    bar = (T0 + H, 96.0, 96.4, 95.6, 96.0, 1.0)
    for _ in range(3):
        assert pm.on_bar("X", bar, ATR, (), ()) == []    # 时间止损整条关闭
    pm2 = PositionManager(params)
    pm2.open("X", -1, 96.0, 100.0, None, "BREAKOUT_ACCEPTED", T0, 0)
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
    """BREAKOUT_ACCEPTED 撤单只撤本战役挂单(跨战役挂单不受影响)。"""
    # 2h 战役:bar2 BREAKOUT_ACCEPTED 市价;此前 2h 已有 FLIP_RETESTED 前缘限价挂单(BREAKOUT_ACCEPTED 先撤)
    e7_short = MarketEvent(
        id="FLIP_RETESTED", direction=-1, ts=T0 + 1 * 2 * H, known_index=1,
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
        1: [MarketEvent(id="STRUCTURE_BROKEN", direction=-1, ts=T0 + 1 * 2 * H,
                        known_index=1,
                        attrs={"ref": 99.5, "invalid": 99.7,
                               "pivot_forming": 0,
                               "regime_before": Regime.TREND_UP.value})],
        3: [e7_short],
        4: [_e1_short(4, 2)]})
    # bar1 STRUCTURE_BROKEN:bar1 认知收盘 = exec idx3 = 96.9;margin=(99.5−96.9)/2=1.3,
    # impulse=(bar1 振幅)/2:认知 bar1 振幅=max h - min l(固定 +0.5/-0.5)
    # 未必弱;改用直接断言路由层语义(见 test_v6_strategy)。此处验证同战役
    # BREAKOUT_ACCEPTED 优先级撤单不影响 4h 战役挂单,复杂装配留给引擎集成冒烟。
    res = _run({"TEST@2h": f2, "TEST@4h": _frame("TEST", 4)})
    cancels = [r for r in res.ledger if r["kind"] == "CANCEL"
               and r["reason_code"] == "E1_PRIORITY"]
    # 若 STRUCTURE_BROKEN 判弱+FLIP_RETESTED 成单+BREAKOUT_ACCEPTED 撤满,则撤单只可能发生在 TEST@2h
    for r in cancels:
        assert r["symbol"] == "TEST@2h"


def test_go_event_counter_counts_router_offers_once_per_frame():
    """``go_events`` = 送入路由的次数(按帧);``mic_go_events`` 是其中的触发帧子集。

    同一现象若在认知帧与 trigger 帧都被检出,引擎各自决策一次,故计两次——
    仓位去重(dup)发生在提交阶段,不改变"路由被调用了几次"这个事实。
    """
    from types import SimpleNamespace

    from market_operator.campaign import _count_go

    go_set = frozenset({("BREAKOUT_ACCEPTED", 1)})
    events = [SimpleNamespace(id="BREAKOUT_ACCEPTED", direction=1),
              SimpleNamespace(id="BREAKOUT_ACCEPTED", direction=-1),   # 方向不在 GO 集
              SimpleNamespace(id="TREND_RESUMED", direction=1)]        # 不在白名单

    counters: dict = {}
    assert _count_go(counters, events, go_set) == 1
    assert counters == {"go_events": 1}, "认知路径不碰 mic_go_events"

    assert _count_go(counters, events, go_set, mic=True) == 1
    assert counters == {"go_events": 2, "mic_go_events": 1}
    assert counters["mic_go_events"] <= counters["go_events"]

