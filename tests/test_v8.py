"""v8 测试(docs/v8-protocol.md §1,冻结):EDGE_RECLAIMED EDGE_RECLAIM 边缘收复。

覆盖(场景为手工价格路径,结构物理见各函数注释;检测场景用冻结
config(atr_n=14)保证阈值余量,同 test_events.py RANGE_ROTATED 惯例):
- 检测:底带多触发(BREAKOUT_FAILED 伴随、pos 门、attrs 契约:zone/ref/invalid/pos/band/
  atr/puncture_index/ex)、顶带空镜像(数值取多头场景的 202−p 镜像,
  检测器支撑/阻力对称性的直接证据)、F1 侧位置门边界(pos 恰 0.20 inclusive
  通过 / 恰 0.80 inclusive 通过,及两侧失败对照:BREAKOUT_FAILED 照常发射、EDGE_RECLAIMED 仅被门挡下)、
  收复超时(刺穿起 >3 根才收复 ⇒ BREAKOUT_FAILED 窗口已闭 ⇒ 无 EDGE_RECLAIMED;episode 由 LEVEL_REJECTED 收场,
  如实记录替代分支)、enable_e8=False 零 EDGE_RECLAIMED、enable_e8 开关不改 BREAKOUT_ACCEPTED-FLIP_RETESTED parity;
- 路由:V8Router 正向(结构失效/带缘目标/r_mult/路线标签)、F1 拒单、
  room 拒单、槽位纪律、白名单外事件;
- 引擎:EDGE_RECLAIMED 注入合成战役 ⇒ broker/账目接线(次 1h 市价成交、invalid/target
  传递、route=e8_market)、确定性双跑;
- 基线:canonical 全套由仓库标准 pytest 守护(本文件并入 tests/,全仓运行)。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from market_operator.backtest_v6 import SymbolCognition
from market_operator.broker import ContractSpec
from market_operator.campaign import (
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.events import (
    BREAKOUT_FAILED,
    LEVEL_REJECTED,
    EDGE_RECLAIMED,
    EventDetector,
    MarketEvent,
)
from market_operator.hypothesis import StructureView
from market_operator.strategy_v8 import ROUTE_EDGE_RECLAIMED, V8Params, V8Router, WHITELIST_V8
from market_operator.structure import Params, Regime, Zone, ZoneRole, ZoneState

BASE_TS = 1_700_000_000_000
H = 3_600_000
Q = 900_000
T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC(对齐网格)
ATR = 2.0
CFG = Params.from_json("config/defaults.json")


def _mk(bars, o, h, l, c):
    bars.append((BASE_TS + H * len(bars), o, h, l, c, 1.0))


def calm(bars, px=100.0, w=0.05, n=1):
    for _ in range(n):
        _mk(bars, px, px + w, px - w, px)


def bar(bars, o, h, l, c):
    _mk(bars, o, h, l, c)


def _mirror(bars, shift=202.0):
    """价格镜像 o/h/l/c → shift−o/shift−l/shift−h/shift−c(支撑↔阻力对称)。"""
    return [(ts, shift - o, shift - l, shift - h, shift - c, v)
            for ts, o, h, l, c, v in bars]


def run(bars, enable=True, edge=0.20):
    det = EventDetector(CFG, enable_e8=enable, e8_pos_edge=edge)
    for b in bars:
        det.update(b)
    return det


def by_id(events, eid):
    return [e for e in events if e.id == eid]


# ---------------------------------------------------------------------------
# 场景一:底带多(支撑区刺穿→收复,pos ≤ 0.2)
# 结构物理:先冲高形成 H*(101.5@16 确认),下行腿 97.5 见底确认 L1@21
# (支撑区 [97.5, 97.728]);hover 极值 98.9 于刺穿 bar(23)被回撤确认为
# H2 ⇒ band=[97.5, 98.9];bar24 收复 97.65 > zone.lo ⇒ BREAKOUT_FAILED(+1);pos=0.107 过门
# ⇒ 同 bar EDGE_RECLAIMED(+1)。针刺穿(影线过 97.5−bm)、收盘未破位(>97.5−bm)。
# ---------------------------------------------------------------------------


def bars_long_primary():
    bars = []
    calm(bars, 100.0, 0.05, 14)
    bar(bars, 100.0, 100.7, 99.95, 100.6)
    bar(bars, 100.6, 101.5, 100.55, 101.4)
    bar(bars, 101.4, 101.45, 100.5, 100.6)
    bar(bars, 100.6, 100.65, 99.5, 99.7)
    bar(bars, 99.7, 99.75, 98.5, 98.6)
    bar(bars, 98.6, 98.65, 97.6, 97.8)
    bar(bars, 97.8, 97.85, 97.5, 97.65)
    bar(bars, 97.65, 98.35, 97.55, 98.3)
    bar(bars, 98.3, 98.9, 98.25, 98.85)
    bar(bars, 98.85, 98.95, 97.4, 97.48)     # 23:H2@23 + 刺穿 97.4
    bar(bars, 97.48, 97.7, 97.45, 97.65)     # 24:收复 → BREAKOUT_FAILED/EDGE_RECLAIMED(+1)
    return bars


def test_e8_long_bottom_band_positive():
    det = run(bars_long_primary())
    e8s = by_id(det.events, EDGE_RECLAIMED)
    assert len(e8s) == 1
    e = e8s[0]
    assert e.direction == +1
    assert e.known_index == 24
    assert e.ts == bars_long_primary()[24][0]
    z = e.attrs["zone"]
    assert z.role.value == "support"
    assert e.attrs["ref"] == pytest.approx(z.lo)        # 收复边缘(多:zone.lo)
    assert e.attrs["puncture_index"] == 23
    # pos 门(构造):band=[L*,H*] 与 pos 均为因果量(收复 bar 收盘知时)
    l_star, h_star = e.attrs["band"]
    assert (l_star, h_star) == pytest.approx((97.5, 98.9))
    assert e.attrs["pos"] == pytest.approx((97.65 - 97.5) / (98.9 - 97.5))
    assert e.attrs["pos"] <= 0.20
    # 失效 = 刺穿极值 ∓ k_bm·atr(多:ex − bm,ex=episode 极值含本 bar)
    atr = e.attrs["atr"]
    assert e.attrs["ex"] == pytest.approx(97.4)
    assert e.attrs["invalid"] == pytest.approx(97.4 - CFG.k_bm * atr)
    # 伴随语义:同 bar 的 BREAKOUT_FAILED(+1) 是 EDGE_RECLAIMED 的载体(A3 episode 去重继承)
    e2s = [x for x in by_id(det.events, BREAKOUT_FAILED)
           if x.known_index == 24 and x.direction == +1]
    assert len(e2s) == 1
    assert [x.id for x in det.events if x.known_index == 24] == ["BREAKOUT_FAILED", "EDGE_RECLAIMED"]


def test_e8_short_top_band_mirror():
    """顶带空 = 底带多的 202−p 镜像(检测器角色对称性的直接证据)。"""
    det = run(_mirror(bars_long_primary()))
    e8s = by_id(det.events, EDGE_RECLAIMED)
    assert len(e8s) == 1
    e = e8s[0]
    assert e.direction == -1
    assert e.known_index == 24
    z = e.attrs["zone"]
    assert z.role.value == "resistance"
    assert e.attrs["ref"] == pytest.approx(z.hi)        # 收复边缘(空:zone.hi)
    l_star, h_star = e.attrs["band"]
    assert (l_star, h_star) == pytest.approx((202.0 - 98.9, 202.0 - 97.5))
    assert e.attrs["pos"] == pytest.approx(
        ((202.0 - 97.65) - l_star) / (h_star - l_star))
    assert e.attrs["pos"] >= 0.80
    atr = e.attrs["atr"]
    assert e.attrs["invalid"] == pytest.approx(e.attrs["ex"] + CFG.k_bm * atr)
    assert e.attrs["ex"] == pytest.approx(202.0 - 97.4)  # 刺穿极值(空:上方)


# ---------------------------------------------------------------------------
# 场景二:位置门边界(恰界 inclusive)
# 刺穿谷底(96.0)在收复 bar(22)被弹回确认 L2 ⇒ L*=96.0;hover 极值 98.5
# 于刺穿 bar(21)确认 H2 ⇒ band=[96.0, 98.5](rg=2.5);收复收盘 96.5 ⇒
# pos=(96.5−96.0)/2.5=0.2 恰界(inclusive 通过);96.6 ⇒ 0.24 拒(BREAKOUT_FAILED 照常)。
# 谷底新低 pivot 还生成 [96.0,·] 支撑区并与旧区 [96.2,·] 按 §4.1 合并为
# [96.0, 96.444],episode 按 A3 合并语义续绑 —— 触发区 = 合并后 zone。
# ---------------------------------------------------------------------------


def bars_long_boundary(c_final, h_final):
    bars = []
    calm(bars, 100.0, 0.05, 14)
    bar(bars, 100.0, 100.8, 99.95, 100.7)
    bar(bars, 100.7, 100.75, 100.1, 100.2)
    bar(bars, 100.2, 100.25, 96.2, 96.4)
    bar(bars, 96.4, 97.1, 96.3, 97.05)
    bar(bars, 97.05, 97.8, 96.9, 97.7)
    bar(bars, 97.7, 98.5, 97.6, 98.4)
    bar(bars, 98.4, 98.5, 98.2, 98.3)
    bar(bars, 98.3, 98.35, 96.0, 96.17)
    bar(bars, 96.17, h_final, 96.05, c_final)
    return bars


def test_e8_pos_gate_exactly_0_20_passes_inclusive():
    det = run(bars_long_boundary(96.5, 96.75))
    e8s = by_id(det.events, EDGE_RECLAIMED)
    assert len(e8s) == 1
    e = e8s[0]
    assert e.direction == +1 and e.known_index == 22
    assert e.attrs["band"] == pytest.approx((96.0, 98.5))
    assert e.attrs["pos"] == pytest.approx(0.20)        # 恰界,协议 ≤ inclusive
    assert len(by_id(det.events, BREAKOUT_FAILED)) >= 1


def test_e8_pos_gate_above_0_20_rejects():
    det = run(bars_long_boundary(96.6, 96.76))
    assert by_id(det.events, EDGE_RECLAIMED) == [], "pos=0.24 > 0.20 ⇒ 仅门挡,EDGE_RECLAIMED 不得发射"
    e2s = [e for e in by_id(det.events, BREAKOUT_FAILED) if e.known_index == 22]
    assert len(e2s) == 1, "同一收复行为仍发 BREAKOUT_FAILED(EDGE_RECLAIMED 是纯增量门,不动 BREAKOUT_FAILED)"


def test_e8_pos_gate_mirror_exactly_0_80_and_below():
    det = run(_mirror(bars_long_boundary(96.5, 96.75)))
    e8s = by_id(det.events, EDGE_RECLAIMED)
    assert len(e8s) == 1
    e = e8s[0]
    assert e.direction == -1
    assert e.attrs["band"] == pytest.approx((103.5, 106.0))
    assert e.attrs["pos"] == pytest.approx(0.80)        # 恰界,协议 ≥ inclusive
    det_fail = run(_mirror(bars_long_boundary(96.6, 96.76)))
    assert by_id(det_fail.events, EDGE_RECLAIMED) == [], "pos=0.76 < 0.80 ⇒ 拒"
    assert len([e for e in by_id(det_fail.events, BREAKOUT_FAILED)
                if e.known_index == 22]) == 1


# ---------------------------------------------------------------------------
# 场景三:收复超时(刺穿起 >3 根才收复 ⇒ BREAKOUT_FAILED/EDGE_RECLAIMED 窗口已闭)
# 与场景一同前缀;bar23 刺穿后 bar24/25 悬于缓冲带(收 < zone.lo、回退不足
# LEVEL_REJECTED),bar26(offset=3)才收复 ⇒ BREAKOUT_FAILED 窗口(t−puncture < N_f)已闭,无 BREAKOUT_FAILED/EDGE_RECLAIMED;
# episode 由同 bar 的 LEVEL_REJECTED(+1,回退 ≥ k_rj·atr)收场(替代分支,如实断言)。
# ---------------------------------------------------------------------------


def bars_long_timeout():
    bars = []
    calm(bars, 100.0, 0.05, 14)
    bar(bars, 100.0, 100.7, 99.95, 100.6)
    bar(bars, 100.6, 101.5, 100.55, 101.4)
    bar(bars, 101.4, 101.45, 100.5, 100.6)
    bar(bars, 100.6, 100.65, 99.5, 99.7)
    bar(bars, 99.7, 99.75, 98.5, 98.6)
    bar(bars, 98.6, 98.65, 97.6, 97.8)
    bar(bars, 97.8, 97.85, 97.5, 97.65)
    bar(bars, 97.65, 98.35, 97.55, 98.3)
    bar(bars, 98.3, 98.9, 98.25, 98.85)
    bar(bars, 98.85, 98.95, 97.4, 97.48)     # 23:刺穿
    bar(bars, 97.48, 97.5, 97.38, 97.47)     # 24:悬缓冲带
    bar(bars, 97.47, 97.51, 97.36, 97.46)    # 25
    bar(bars, 97.46, 97.9, 97.38, 97.85)     # 26:offset=3 收复(已超时)
    return bars


def test_e8_reclaim_timeout_no_event():
    det = run(bars_long_timeout())
    assert by_id(det.events, EDGE_RECLAIMED) == [], "收复超过 N_f=3 根 ⇒ 无 EDGE_RECLAIMED"
    assert by_id(det.events, BREAKOUT_FAILED) == [], "同窗口外 ⇒ 亦无 BREAKOUT_FAILED"
    e3s = [e for e in by_id(det.events, LEVEL_REJECTED) if e.known_index == 26]
    assert len(e3s) == 1, "迟到的收复触发 LEVEL_REJECTED(替代分支),episode 终结于其"


# ---------------------------------------------------------------------------
# 开关与 parity
# ---------------------------------------------------------------------------


def test_e8_disabled_by_default_and_parity_e1_e7():
    bars = bars_long_primary() + [
        (ts + 25 * H, *rest) for ts, *rest in [tuple(b) for b in bars_long_timeout()[14:]]]
    det_off = run(bars, enable=False)
    det_on = run(bars, enable=True)
    ids = lambda evs: [(e.id, e.known_index, e.direction) for e in evs]
    assert by_id(det_off.events, EDGE_RECLAIMED) == [], "默认关闭 ⇒ 零 EDGE_RECLAIMED"
    assert len(by_id(det_on.events, EDGE_RECLAIMED)) >= 1
    assert ids([e for e in det_off.events if e.id != EDGE_RECLAIMED]) \
        == ids([e for e in det_on.events if e.id != EDGE_RECLAIMED]), \
        "enable_e8 不得改变 BREAKOUT_ACCEPTED-FLIP_RETESTED 任何输出(parity)"


# ---------------------------------------------------------------------------
# V8Router 单元(闸门/槽位/白名单)
# ---------------------------------------------------------------------------


def _view(atr=ATR):
    return StructureView(regime=Regime.TRANSITION, zones=(), atr=atr)


def _e8(direction, close_ref, band=(95.0, 104.0), invalid=None, pos=0.1,
        atr=ATR, hour=5):
    if invalid is None:
        invalid = 94.8 if direction > 0 else 105.2
    return MarketEvent(id=EDGE_RECLAIMED, direction=direction, ts=T0 + hour * H,
                       known_index=hour,
                       attrs={"ref": 95.0 if direction > 0 else 104.0,
                              "invalid": invalid, "pos": pos, "band": band,
                              "atr": atr, "puncture_index": 4,
                              "ex": 94.9 if direction > 0 else 105.1})


def _bar(close, hour=5):
    return (T0 + hour * H, close, close + 0.05, close - 0.05, close, 1.0)


def test_v8_router_market_entry_structural_geometry():
    r = V8Router(V8Params())
    ev = _e8(+1, 96.6)
    dec = r.on_bar("X", _bar(96.6), 5, _view(), ATR, [ev],
                   position_open=False, entry_resting=False)
    assert len(dec) == 1
    d = dec[0]
    assert d.kind == "market" and d.route == ROUTE_EDGE_RECLAIMED
    hyp = d.hyp
    assert hyp.invalid == pytest.approx(94.8)           # 事件结构锚(非 2·atr)
    assert hyp.target_1 == pytest.approx(104.0)         # 对侧带缘 H*
    assert hyp.r_mult == pytest.approx((104.0 - 96.6) / (96.6 - 94.8))
    assert d.meta["pos"] == pytest.approx(0.1)
    assert r.counters["e8_entries"] == 1


def test_v8_router_f1_reject():
    """dist = close − invalid < k_is·atr ⇒ 无交易(天然距离不足,禁加宽)。"""
    r = V8Router(V8Params())
    ev = _e8(+1, 96.3, invalid=94.8)                    # dist=1.5 < 0.8·2=1.6
    dec = r.on_bar("X", _bar(96.3), 5, _view(), ATR, [ev],
                   position_open=False, entry_resting=False)
    assert dec == []
    assert r.counters["f1_reject_e8_market"] == 1


def test_v8_router_room_reject():
    """room/risk < r_min ⇒ 无交易(对侧带缘空间不足)。"""
    r = V8Router(V8Params())
    ev = _e8(+1, 96.6, band=(95.0, 98.0), invalid=94.6)  # r=(1.4)/(2.0)=0.7
    dec = r.on_bar("X", _bar(96.6), 5, _view(), ATR, [ev],
                   position_open=False, entry_resting=False)
    assert dec == []
    assert r.counters["room_reject_e8_market"] == 1


def test_v8_router_slot_busy_and_whitelist():
    r = V8Router(V8Params())
    ev = _e8(+1, 96.6)
    dec = r.on_bar("X", _bar(96.6), 5, _view(), ATR, [ev],
                   position_open=True, entry_resting=False)
    assert dec == [] and r.counters["dup"] == 1
    other = MarketEvent(id="STRUCTURE_BROKEN", direction=-1, ts=T0 + 6 * H, known_index=6,
                        attrs={"ref": 99.5, "invalid": 99.7})
    dec2 = r.on_bar("X", _bar(96.6, hour=6), 6, _view(), ATR, [other],
                    position_open=False, entry_resting=False)
    assert dec2 == []
    assert r.counters["e8_signals"] == 1               # 白名单外事件不进 EDGE_RECLAIMED 路由


def test_v8_router_short_mirror_geometry():
    r = V8Router(V8Params())
    ev = _e8(-1, 105.4, invalid=107.1)                 # dist=1.7 > k_is·atr=1.6
    dec = r.on_bar("X", _bar(105.4), 5, _view(), ATR, [ev],
                   position_open=False, entry_resting=False)
    assert len(dec) == 1
    hyp = dec[0].hyp
    assert hyp.target_1 == pytest.approx(95.0)          # 对侧带缘 L*
    assert hyp.r_mult == pytest.approx((105.4 - 95.0) / (107.1 - 105.4))


# ---------------------------------------------------------------------------
# 引擎接线:EDGE_RECLAIMED 注入合成战役 ⇒ 成交/账目/确定性(test_v6_campaign 同构装配)
# ---------------------------------------------------------------------------

SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(segment="full", dev_frac=0.7, equity0=300.0, risk_frac=0.0075,
          leverage_cap=5.0, taker_fee=0.0006, maker_fee=0.0002,
          slippage_per_side=0.0002, funding_per_8h=0.0001,
          cost_multiplier=1.0, contracts={"TEST": SPEC},
          params=Params(atr_n=2), n_fill=12, portfolio_cap_frac=0.03)
VIEW2H = StructureView(regime=Regime.TRANSITION, zones=(), atr=ATR)


def _exec_prices(n, overrides=None):
    """确定性 1h OHLC(与 test_v6_campaign 同源):c = 96 + (i%5)*0.3。"""
    out = []
    ov = overrides or {}
    for i in range(n):
        o, h, l, c = ov.get(i, (None, None, None, None))
        base = 96.0 + (i % 5) * 0.3
        co = base if c is None else c
        oo = (out[-1][3] if out else co) if o is None else o
        out.append((oo, max(oo, co) + 0.5 if h is None else h,
                    min(oo, co) - 0.5 if l is None else l, co))
    return out


def _frame_2h(symbol, events_by):
    """2h 战役:认知 2h bar × 1h 执行帧(对齐断言由 build_campaign_frame 承担)。"""
    n_1h = 16
    rows = _exec_prices(n_1h)
    df_exec = pd.DataFrame(
        [(T0 + i * H, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(rows)],
        columns=["ts", "open", "high", "low", "close", "volume"])
    cog_bars = []
    for i in range(n_1h // 2):
        chunk = rows[i * 2:(i + 1) * 2]
        cog_bars.append((T0 + i * 2 * H, chunk[0][0],
                         max(x[1] for x in chunk), min(x[2] for x in chunk),
                         chunk[-1][3], 1.0))
    ev = {i: list(evs) for i, evs in events_by.items()}
    cog = SymbolCognition(symbol=symbol, bars=cog_bars, atrs=[ATR] * 8,
                          events_by=ev, pivots_by={},
                          views={i: VIEW2H for i in ev})
    return build_campaign_frame(cog, df_exec, "2h")


def _e8_long_engine(tf_idx):
    # 认知 bar2 close=96.0:dist=1.7 ≥ k_is·atr=1.6 过 F1;r=(104−96)/1.7 过 room
    return MarketEvent(id=EDGE_RECLAIMED, direction=+1, ts=T0 + tf_idx * 2 * H,
                       known_index=tf_idx,
                       attrs={"ref": 95.0, "invalid": 94.3, "pos": 0.12,
                              "band": (95.0, 104.0), "atr": ATR,
                              "puncture_index": tf_idx - 1, "ex": 94.6})


def _run(frame_map, **over):
    kw = dict(KW)
    kw.update(over)
    return run_campaigns(frame_map, router_factory=V8Router,
                         router_params=V8Params(), **kw)


def test_engine_e8_market_fill_and_geometry():
    """EDGE_RECLAIMED 注入 2h 战役:决策 = 认知 bar2 收盘,成交 = 次 1h 开盘(因果);
    invalid/target_1/route 标签按协议几何入账。"""
    f = _frame_2h("TEST", {2: [_e8_long_engine(2)]})
    res = _run({campaign_key("TEST", "2h"): f})
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1
    row = opens[0]
    t2 = T0 + 2 * 2 * H
    assert row["decision_ts"] == t2
    assert row["ts"] == t2 + 2 * H                        # 次 1h 开盘成交
    m = row["meta"]
    assert m["route"] == ROUTE_EDGE_RECLAIMED
    assert m["invalid"] == pytest.approx(94.3)            # 刺穿极值结构锚
    assert m["target_1"] == pytest.approx(104.0)          # 对侧带缘 H*
    pos = res.campaign_states["TEST@2h"]["pos_mgr"].positions["TEST"]
    assert pos.target_1 == pytest.approx(104.0)
    assert pos.invalid == pytest.approx(94.3)
    assert res.counters["e8_entries"] == 1


def test_engine_determinism_double_run():
    frames1 = {campaign_key("TEST", "2h"):
               _frame_2h("TEST", {2: [_e8_long_engine(2)]})}
    frames2 = {campaign_key("TEST", "2h"):
               _frame_2h("TEST", {2: [_e8_long_engine(2)]})}
    r1 = _run(frames1)
    r2 = _run(frames2)
    assert json.dumps(r1.ledger, sort_keys=True) \
        == json.dumps(r2.ledger, sort_keys=True)
    assert json.dumps(r1.trades, sort_keys=True) \
        == json.dumps(r2.trades, sort_keys=True)
    assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)
