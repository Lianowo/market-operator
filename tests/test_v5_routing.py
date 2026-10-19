"""v5 路由策略单元测试(docs/v5-protocol.md §1;strategy_v5.V5Router)。

覆盖:STRONG/WEAK 质量判定边界(恰 k_dq=0.5 / k_imp=1.5 两侧,含恰等取
STRONG 的 ≥ 语义);F1(k_is=0.8)对 E1/E6 生效(v1 中会通过的老紧止损在 v5
被拒);E1 取消未成交挂单优先级(决策序 cancel→market;持仓中不撤);WEAK-E6
→ 同 episode E7(ref == l_star)挂前缘限价、ref 不匹配/无登记不交易、每
episode 消费一次、最新 weak-E6 覆盖旧登记、r_min 拒绝;同 bar 一信号纪律。
"""

from __future__ import annotations

import pytest

from market_operator.events import E1, E6, E7, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.strategy_v5 import (
    ROUTE_E1,
    ROUTE_STRONG_E6,
    ROUTE_WEAK_E7,
    V5Params,
    V5Router,
)
from market_operator.structure import Regime, Zone, ZoneRole, ZoneState

T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC
H = 3_600_000
ATR = 2.0
SYM = "TEST"


def _view():
    """结构视图:ACTIVE 支撑区 [4.0, 5.0](short 目标搜索用),atr=2.0。"""
    return StructureView(
        regime=Regime.TREND_DOWN,
        zones=(Zone(lo=4.0, hi=5.0, role=ZoneRole.SUPPORT,
                    state=ZoneState.ACTIVE),),
        atr=ATR)


def _bar(c, h_amp=None, o=None, hour=0):
    """(ts,o,h,l,c,v);h_amp 给定时按 (h−l)=h_amp 构造二元精确振幅。"""
    o = c if o is None else o
    if h_amp is None:
        return (T0 + hour * H, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 1.0)
    half = h_amp / 2.0
    return (T0 + hour * H, o, c + half, c - half, c, 1.0)


def _e6(l_star, invalid, hour=0):
    return MarketEvent(id=E6, direction=-1, ts=T0 + hour * H, known_index=hour,
                       attrs={"ref": l_star, "invalid": invalid,
                              "pivot_forming": 0,
                              "regime_before": Regime.TREND_UP.value})


def _e1(invalid, ref, hour=0):
    return MarketEvent(id=E1, direction=-1, ts=T0 + hour * H, known_index=hour,
                       attrs={"ref": ref, "invalid": invalid, "started": 0})


def _e7(zone_lo, zone_hi, invalid, hour=0):
    return MarketEvent(
        id=E7, direction=-1, ts=T0 + hour * H, known_index=hour,
        attrs={"zone": Zone(lo=zone_lo, hi=zone_hi,
                            role=ZoneRole.RESISTANCE, state=ZoneState.FLIPPED),
               "ref": zone_lo, "invalid": invalid,
               "from_role": ZoneRole.SUPPORT.value, "atr": ATR})


def _router():
    return V5Router(V5Params())


# ---------------------------------------------------------------------------
# STRONG/WEAK 质量边界(恰 0.5/1.5 两侧;≥ 语义:恰等取 STRONG)
# ---------------------------------------------------------------------------


def test_strong_boundary_exact_margin_and_impulse():
    """margin 恰 0.5 且 impulse 恰 1.5(全部二元精确)⇒ STRONG 市价决策。"""
    r = _router()
    # l_star=10, close=9 ⇒ margin=(10−9)/2=0.5;amp=3.0/2=1.5
    d = r.on_bar(SYM, _bar(9.0, h_amp=3.0), 0, _view(), ATR,
                 [_e6(10.0, 11.0)], position_open=False, entry_resting=False)
    assert len(d) == 1 and d[0].kind == "market" and d[0].route == ROUTE_STRONG_E6
    assert r.counters.get("e6_strong_quality") == 1
    assert r.pending_of(SYM) is None


def test_weak_when_margin_just_below_k_dq():
    """margin = 0.5 − ε(impulse 足)⇒ WEAK:无决策,登记 episode。"""
    r = _router()
    d = r.on_bar(SYM, _bar(9.000000001, h_amp=3.0), 0, _view(), ATR,
                 [_e6(10.0, 11.0)], position_open=False, entry_resting=False)
    assert d == []
    p = r.pending_of(SYM)
    assert p is not None and p.l_star == 10.0 and p.bar_index == 0
    assert p.margin < 0.5 and p.impulse >= 1.5
    assert r.counters.get("e6_weak_registered") == 1


def test_weak_when_impulse_just_below_k_imp():
    """impulse = 1.5 − ε(margin 足)⇒ WEAK。"""
    r = _router()
    d = r.on_bar(SYM, _bar(9.0, h_amp=3.0 - 1e-9), 0, _view(), ATR,
                 [_e6(10.0, 11.0)], position_open=False, entry_resting=False)
    assert d == []
    p = r.pending_of(SYM)
    assert p is not None and p.margin >= 0.5 and p.impulse < 1.5


def test_weak_when_both_below():
    r = _router()
    d = r.on_bar(SYM, _bar(9.6, h_amp=2.0), 0, _view(), ATR,
                 [_e6(10.0, 11.0)], position_open=False, entry_resting=False)
    assert d == [] and r.pending_of(SYM) is not None


# ---------------------------------------------------------------------------
# F1(k_is=0.8)对 E1/E6 生效(v1 通过的老紧止损在 v5 被拒)
# ---------------------------------------------------------------------------


def test_f1_e1_boundary_both_sides():
    """dist 恰 0.8·atr 通过(≥ 语义);小一档拒绝(v1 无 F1,本单 v1 会成交)。"""
    # atr=2 ⇒ k_is·atr=1.6;close=95:risk = invalid−95
    ok = _router().on_bar(SYM, _bar(95.0), 0, _view(), ATR,
                          [_e1(96.625, 96.5)], position_open=False,
                          entry_resting=False)
    assert len(ok) == 1 and ok[0].route == ROUTE_E1
    # invalid=96.6:96.6−95 = 1.5999…(最近双精度)<1.6 ⇒ 恰线下拒绝
    r2 = _router()
    no = r2.on_bar(SYM, _bar(95.0), 0, _view(), ATR,
                   [_e1(96.6, 96.5)], position_open=False,
                   entry_resting=False)
    assert no == [] and r2.counters.get(f"f1_reject_{ROUTE_E1}") == 1


def test_f1_e6_clear_reject_and_pass():
    """STRONG 质量 + dist 不足 ⇒ 拒(v1 的紧止损路径被封);dist 足 ⇒ 市价。"""
    # close=9,atr=2:需 invalid−9 ≥ 1.6;两侧取 1.5 / 2.0(二元精确)
    r = _router()
    no = r.on_bar(SYM, _bar(9.0, h_amp=3.0), 0, _view(), ATR,
                  [_e6(10.0, 10.5)], position_open=False, entry_resting=False)
    assert no == [] and r.counters.get(f"f1_reject_{ROUTE_STRONG_E6}") == 1
    assert r.pending_of(SYM) is None            # STRONG 路由拒绝不回落 WEAK
    r2 = _router()
    ok = r2.on_bar(SYM, _bar(9.0, h_amp=3.0), 0, _view(), ATR,
                   [_e6(10.0, 11.0)], position_open=False, entry_resting=False)
    assert len(ok) == 1 and ok[0].kind == "market"


# ---------------------------------------------------------------------------
# E1 优先级:取消未成交入场挂单(决策序 cancel→market);持仓中不撤
# ---------------------------------------------------------------------------


def test_e1_cancels_resting_then_market():
    r = _router()
    d = r.on_bar(SYM, _bar(95.0), 0, _view(), ATR, [_e1(96.625, 96.5)],
                 position_open=False, entry_resting=True)
    assert [x.kind for x in d] == ["cancel_entry", "market"]
    assert d[0].route == ROUTE_E1 and d[1].route == ROUTE_E1
    assert "priority" in str(d[0].meta.get("reason"))
    assert r.counters.get("e1_cancel_orders") == 1


def test_e1_cancel_even_if_e1_itself_fails_gates():
    """撤单不回滚:E1 被 F1 拒绝时挂单仍撤(E1 优先级最高,该信号本身无交易)。"""
    r = _router()
    d = r.on_bar(SYM, _bar(95.0), 0, _view(), ATR, [_e1(96.1, 96.0)],
                 position_open=False, entry_resting=True)
    assert [x.kind for x in d] == ["cancel_entry"]
    assert r.counters.get(f"f1_reject_{ROUTE_E1}") == 1


def test_e1_drops_unarmed_weak_episode():
    r = _router()
    r.on_bar(SYM, _bar(9.6, h_amp=2.0), 0, _view(), ATR, [_e6(10.0, 11.0)],
             position_open=False, entry_resting=False)          # weak,登记
    assert r.pending_of(SYM) is not None
    d = r.on_bar(SYM, _bar(95.0), 1, _view(), ATR, [_e1(96.625, 96.5)],
                 position_open=False, entry_resting=False)
    assert [x.kind for x in d] == ["market"]                    # 无单可撤
    assert r.pending_of(SYM) is None                            # 登记被 E1 清掉
    assert r.counters.get("e1_drops_pending") == 1


def test_no_cancel_when_position_open():
    """持仓中 E1 按 v1 dup 语义拒绝,不动任何挂单(§8 减仓限价不受影响)。"""
    r = _router()
    d = r.on_bar(SYM, _bar(95.0), 0, _view(), ATR, [_e1(96.625, 96.5)],
                 position_open=True, entry_resting=False)
    assert d == [] and r.counters.get("dup") == 1
    assert r.counters.get("e1_cancel_orders") is None


# ---------------------------------------------------------------------------
# WEAK-E6 → 同 episode E7 前缘限价(ref == l_star 追溯)
# ---------------------------------------------------------------------------


def test_weak_e6_then_matching_e7_arms_front_edge_limit():
    r = _router()
    r.on_bar(SYM, _bar(9.6, h_amp=2.0), 10, _view(), ATR, [_e6(10.0, 11.0)],
             position_open=False, entry_resting=False)          # weak,登记
    d = r.on_bar(SYM, _bar(8.9), 13, _view(), ATR,
                 [_e7(10.0, 10.4, 10.6)], position_open=False,
                 entry_resting=False)
    assert len(d) == 1 and d[0].kind == "limit"
    assert d[0].route == ROUTE_WEAK_E7
    assert d[0].limit_price == pytest.approx(10.0)              # short ⇒ zone.lo
    assert d[0].meta["l_star"] == 10.0 and d[0].meta["weak_bars"] == 3
    assert r.pending_of(SYM) is None                            # 消费一次
    assert r.counters.get("e7_matched_episode") == 1


def test_episode_consumed_once():
    r = _router()
    r.on_bar(SYM, _bar(9.6, h_amp=2.0), 10, _view(), ATR, [_e6(10.0, 11.0)],
             position_open=False, entry_resting=False)
    r.on_bar(SYM, _bar(8.9), 13, _view(), ATR, [_e7(10.0, 10.4, 10.6)],
             position_open=False, entry_resting=False)          # 消费
    d = r.on_bar(SYM, _bar(8.8), 16, _view(), ATR,
                 [_e7(10.0, 10.4, 10.6)], position_open=False,
                 entry_resting=False)
    assert d == [] and r.counters.get("e7_no_episode") == 1


def test_e7_without_registered_episode_no_trade():
    r = _router()
    d = r.on_bar(SYM, _bar(8.9), 5, _view(), ATR, [_e7(10.0, 10.4, 10.6)],
                 position_open=False, entry_resting=False)
    assert d == [] and r.counters.get("e7_no_episode") == 1


def test_e7_ref_mismatch_keeps_pending():
    r = _router()
    r.on_bar(SYM, _bar(9.6, h_amp=2.0), 10, _view(), ATR, [_e6(10.0, 11.0)],
             position_open=False, entry_resting=False)
    d = r.on_bar(SYM, _bar(8.9), 12, _view(), ATR, [_e7(10.5, 10.9, 11.1)],
                 position_open=False, entry_resting=False)
    assert d == [] and r.counters.get("e7_ref_mismatch") == 1
    assert r.pending_of(SYM) is not None                        # 非同 episode,登记保留


def test_latest_weak_e6_replaces_pending():
    r = _router()
    r.on_bar(SYM, _bar(9.6, h_amp=2.0), 10, _view(), ATR, [_e6(10.0, 11.0)],
             position_open=False, entry_resting=False)
    r.on_bar(SYM, _bar(8.6, h_amp=2.0), 12, _view(), ATR, [_e6(9.0, 10.0)],
             position_open=False, entry_resting=False)
    assert r.pending_of(SYM).l_star == 9.0
    assert r.counters.get("e6_replaced_pending") == 1
    d = r.on_bar(SYM, _bar(8.9), 13, _view(), ATR, [_e7(10.0, 10.4, 10.6)],
                 position_open=False, entry_resting=False)
    assert d == []                                              # 旧 ref 不再匹配
    d = r.on_bar(SYM, _bar(7.9), 15, _view(), ATR, [_e7(9.0, 9.4, 9.6)],
                 position_open=False, entry_resting=False)
    assert len(d) == 1 and d[0].limit_price == pytest.approx(9.0)


def test_matched_e7_rejected_by_f1_consumes_episode():
    r = _router()
    r.on_bar(SYM, _bar(9.6, h_amp=2.0), 10, _view(), ATR, [_e6(10.0, 11.0)],
             position_open=False, entry_resting=False)
    d = r.on_bar(SYM, _bar(9.2), 13, _view(), ATR, [_e7(10.0, 10.4, 10.6)],
                 position_open=False, entry_resting=False)
    assert d == []                                              # 10.6−9.2=1.4 < 1.6
    assert r.counters.get(f"f1_reject_{ROUTE_WEAK_E7}") == 1
    assert r.pending_of(SYM) is None                            # 消费一次含闸门拒绝


# ---------------------------------------------------------------------------
# 闸门与槽位纪律
# ---------------------------------------------------------------------------


def test_r_min_reject_on_strong_e6():
    """STRONG/F1 通过但结构目标太近(r_mult<1.5)⇒ 无交易。"""
    view = StructureView(
        regime=Regime.TREND_DOWN,
        zones=(Zone(lo=7.0, hi=8.5, role=ZoneRole.SUPPORT,
                    state=ZoneState.ACTIVE),), atr=ATR)
    r = _router()
    # close=9,invalid=11,target=8.5 ⇒ r=(9−8.5)/(11−9)=0.25
    d = r.on_bar(SYM, _bar(9.0, h_amp=3.0), 0, view, ATR, [_e6(10.0, 11.0)],
                 position_open=False, entry_resting=False)
    assert d == [] and r.counters.get(f"rmin_reject_{ROUTE_STRONG_E6}") == 1


def test_one_decision_per_bar_slot_discipline():
    """同 bar E1 + STRONG E6:事件序 E1 先占槽,E6 dup 拒绝(每品种一单)。"""
    r = _router()
    d = r.on_bar(SYM, _bar(95.0), 0, _view(), ATR,
                 [_e1(96.625, 96.5), _e6(96.0, 97.0)],
                 position_open=False, entry_resting=False)
    assert len(d) == 1 and d[0].route == ROUTE_E1
    assert r.counters.get("dup") == 1


def test_dup_when_entry_resting_e7_not_armed():
    """挂单中(非 E1 bar):E7 不再武装新单(每品种一挂单)。"""
    r = _router()
    r.on_bar(SYM, _bar(9.6, h_amp=2.0), 10, _view(), ATR, [_e6(10.0, 11.0)],
             position_open=False, entry_resting=False)
    d = r.on_bar(SYM, _bar(8.9), 13, _view(), ATR, [_e7(10.0, 10.4, 10.6)],
                 position_open=False, entry_resting=True)        # 已有挂单
    assert d == [] and r.counters.get("dup") == 1


def test_long_side_nogo_short_only():
    r = _router()
    e = MarketEvent(id=E6, direction=+1, ts=T0, known_index=0,
                    attrs={"ref": 10.0, "invalid": 9.0})
    d = r.on_bar(SYM, _bar(11.0, h_amp=3.0), 0, _view(), ATR, [e],
                 position_open=False, entry_resting=False)
    assert d == [] and "nogo" in r.explain()
