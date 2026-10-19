"""v6 路由策略单元测试(docs/v6-protocol.md §2/§3)。

覆盖:S1 失效锚定结构区边界(E6 短/长 ⇒ 区缘 ± k_bm·atr;无区拒;
E1/E7 沿用检测器区缘口径);F1 闸(天然距离不足即笔不存在);臂 A
clamp2R(target = close ± 2.0R 按实际 entry-invalid 计;room < 1.5R 拒);
臂 B turtle(初始防线 = 2.0·atr,无目标,不做 room/r_min 门);双向路由
(E6|long 质量镜像/同向 episode 关联);E1 优先级语义沿用。
"""

from __future__ import annotations

from market_operator.events import E1, E6, E7, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.strategy_v6 import (
    ARM_CLAMP2R,
    ARM_TURTLE,
    ROUTE_E1,
    ROUTE_STRONG_E6,
    ROUTE_WEAK_E7,
    V6Params,
    V6Router,
)
from market_operator.structure import Regime, Zone, ZoneRole, ZoneState

T0 = 1_704_067_200_000
H = 3_600_000
ATR = 2.0
K_BM = 0.1


def _view(*zones):
    return StructureView(regime=Regime.RANGE, zones=tuple(zones), atr=ATR)


def _e6(direction, hour=5, ref=99.5, bar_close=97.0):
    inv = ref + 0.2 if direction < 0 else ref - 0.2
    return MarketEvent(id=E6, direction=direction, ts=T0 + hour * H,
                       known_index=hour,
                       attrs={"ref": ref, "invalid": inv, "pivot_forming": 0,
                              "regime_before": (Regime.TREND_UP.value
                                                if direction < 0 else
                                                Regime.TREND_DOWN.value)})


def _e1(direction, hour=5, ref=99.5, invalid=None):
    invalid = invalid if invalid is not None else (ref + 0.2)
    return MarketEvent(id=E1, direction=direction, ts=T0 + hour * H,
                       known_index=hour,
                       attrs={"ref": ref, "invalid": invalid, "started": 0})


def _e7(direction, hour=6, lo=99.5, hi=99.8, invalid=None):
    # 检测器冻结口径:E7 invalid = 翻转区缘 ± k_bm·atr
    if invalid is None:
        invalid = (hi + K_BM * ATR) if direction < 0 else (lo - K_BM * ATR)
    return MarketEvent(id=E7, direction=direction, ts=T0 + hour * H,
                       known_index=hour,
                       attrs={"zone": Zone(lo=lo, hi=hi, role=(
                           ZoneRole.RESISTANCE if direction < 0 else
                           ZoneRole.SUPPORT), state=ZoneState.FLIPPED),
                           "ref": lo if direction < 0 else hi,
                           "invalid": invalid,
                           "from_role": (ZoneRole.SUPPORT.value
                                         if direction < 0 else
                                         ZoneRole.RESISTANCE.value),
                           "atr": ATR})


def _bar(close, hour=5, hi_pad=3.0, lo_pad=3.0, o=None):
    o = close if o is None else o
    return (T0 + hour * H, o, close + hi_pad, close - lo_pad, close, 1.0)


def _router(arm=ARM_CLAMP2R, **over):
    return V6Router(V6Params(arm=arm, **over))


# ---------------------------------------------------------------------------
# S1:失效锚定结构区边界
# ---------------------------------------------------------------------------


def test_s1_e6_short_invalid_is_zone_hi_plus_buf():
    """E6|short:invalid = 含 ref 的区 hi + k_bm·atr(替代微观极值 ref+0.2)。"""
    r = _router()
    zone = Zone(lo=99.5, hi=100.4, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=90.0, hi=91.0, role=ZoneRole.SUPPORT,
               state=ZoneState.ACTIVE)
    bar = _bar(97.0)          # margin=(99.5-97)/2=1.25 ≥0.5, impulse=6/2=3 ≥1.5
    dec = r.on_bar("X", bar, 5, _view(zone, tgt), ATR, [_e6(-1, ref=99.5)],
                   position_open=False, entry_resting=False)
    assert len(dec) == 1 and dec[0].kind == "market"
    assert dec[0].route == ROUTE_STRONG_E6
    assert dec[0].hyp.invalid == 100.4 + K_BM * ATR          # 区 hi + buf


def test_s1_e6_long_invalid_is_zone_lo_minus_buf():
    """E6|long 镜像:invalid = 含 ref 的区 lo − k_bm·atr(区宽天然缩放)。"""
    r = _router()
    zone = Zone(lo=99.6, hi=100.5, role=ZoneRole.RESISTANCE,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=111.0, hi=112.0, role=ZoneRole.RESISTANCE,
               state=ZoneState.ACTIVE)
    bar = _bar(103.0)         # margin = 103-100.5 = 2.5/2 = 1.25; impulse 3
    dec = r.on_bar("X", bar, 5, _view(zone, tgt), ATR, [_e6(+1, ref=100.5)],
                   position_open=False, entry_resting=False)
    assert len(dec) == 1
    assert dec[0].hyp.invalid == 99.6 - K_BM * ATR


def test_s1_e6_zone_width_scaling_is_natural():
    """同一 ref,更宽的区 ⇒ 更远的失效(缩放来自区宽,不是参数)。"""
    narrow = Zone(lo=99.5, hi=100.0, role=ZoneRole.SUPPORT,
                  state=ZoneState.BROKEN)
    wide = Zone(lo=99.4, hi=101.0, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=88.0, hi=89.0, role=ZoneRole.SUPPORT,
               state=ZoneState.ACTIVE)
    bar = _bar(97.0)
    d1 = _router().on_bar("X", bar, 5, _view(narrow, tgt), ATR,
                          [_e6(-1, ref=99.5)], position_open=False,
                          entry_resting=False)[0]
    d2 = _router().on_bar("X", bar, 5, _view(wide, tgt), ATR,
                          [_e6(-1, ref=99.5)], position_open=False,
                          entry_resting=False)[0]
    assert d1.hyp.invalid < d2.hyp.invalid


def test_s1_e6_no_zone_rejects():
    """含 ref 的结构区不存在(已消亡/移除)⇒ no_zone 拒,不编造宽度。"""
    r = _router()
    away = Zone(lo=80.0, hi=81.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE)
    dec = r.on_bar("X", _bar(97.0), 5, _view(away), ATR, [_e6(-1, ref=99.5)],
                   position_open=False, entry_resting=False)
    assert dec == []
    assert r.counters.get("no_zone_" + ROUTE_STRONG_E6) == 1


def test_s1_e1_e7_keep_detector_zone_boundary():
    """E1/E7:invalid 沿用检测器 attrs(冻结口径即区缘 ± buf),策略不改动。"""
    r = _router()
    tgt = Zone(lo=90.0, hi=91.0, role=ZoneRole.SUPPORT,
               state=ZoneState.ACTIVE)
    dec = r.on_bar("X", _bar(97.0), 5, _view(tgt), ATR,
                   [_e1(-1, ref=99.0, invalid=99.4)],
                   position_open=False, entry_resting=False)
    assert len(dec) == 1 and dec[0].route == ROUTE_E1
    assert dec[0].hyp.invalid == 99.4

    r2 = _router()
    r2.on_bar("X", _bar(97.0, hi_pad=0.2, lo_pad=0.2), 5, _view(tgt), ATR,
              [_e6(-1, hour=5, ref=99.5)], position_open=False,
              entry_resting=False)
    assert r2.pending_of("X") is not None        # impulse 0.2 < 1.5 ⇒ WEAK 登记
    dec2 = r2.on_bar("X", _bar(98.0, hour=6), 6, _view(tgt), ATR,
                     [_e7(-1, hour=6)], position_open=False,
                     entry_resting=False)
    assert len(dec2) == 1 and dec2[0].kind == "limit"
    assert dec2[0].hyp.invalid == 99.8 + K_BM * ATR   # E7 检测器口径原样


# ---------------------------------------------------------------------------
# F1 闸(天然距离不足即笔不存在)
# ---------------------------------------------------------------------------


def test_f1_gate_rejects_short_natural_distance():
    """dist = close − invalid(S1) < k_is·atr ⇒ 无交易;不许人工加宽。"""
    r = _router()
    zone = Zone(lo=99.5, hi=97.4 - 0.1, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=80.0, hi=81.0, role=ZoneRole.SUPPORT,
               state=ZoneState.ACTIVE)
    # invalid = 97.3 + 0.2 = 97.5;dist = 97 − 97.4+... 设计使 dist < 1.6
    zone = Zone(lo=99.5, hi=97.2, role=ZoneRole.SUPPORT, state=ZoneState.BROKEN)
    # invalid = 97.2+0.2 = 97.4; dist = 97 − 97.4 < 0 ⇒ 不保护 ⇒ 拒
    dec = r.on_bar("X", _bar(97.0), 5, _view(zone, tgt), ATR,
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    assert dec == []


# ---------------------------------------------------------------------------
# 臂 A:target clamp 2R 与 room 门
# ---------------------------------------------------------------------------


def test_arm_a_target_clamped_to_2R_actual_risk():
    """target_1 = close + direction·2.0·(实际 entry-invalid),非结构目标。"""
    r = _router()
    zone = Zone(lo=99.5, hi=100.4, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=80.0, hi=81.0, role=ZoneRole.SUPPORT,
               state=ZoneState.ACTIVE)      # 结构目标很远
    dec = r.on_bar("X", _bar(97.0), 5, _view(zone, tgt), ATR,
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    hyp = dec[0].hyp
    risk = hyp.invalid - 97.0                      # short:invalid 在上方,risk>0
    assert hyp.target_1 == 97.0 - 2.0 * risk
    assert hyp.target_1 != tgt.hi                              # 已钳制


def test_arm_a_room_below_1_5R_rejects():
    """对侧结构空间 room < 1.5R ⇒ 无交易(沿用 r_min 门,S1 口径 risk)。"""
    r = _router()
    zone = Zone(lo=99.5, hi=100.4, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    # risk = 97 − 100.6 = 3.6;room 需 ≥ 5.4 ⇒ tgt.hi ≤ 91.6;取 92.5 不足
    tgt = Zone(lo=91.5, hi=92.5, role=ZoneRole.SUPPORT,
               state=ZoneState.ACTIVE)
    dec = r.on_bar("X", _bar(97.0), 5, _view(zone, tgt), ATR,
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    assert dec == []
    assert r.counters.get("room_reject_" + ROUTE_STRONG_E6) == 1


def test_arm_a_room_1_5R_boundary_semantics():
    r = _router()
    zone = Zone(lo=99.5, hi=100.4, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=90.5, hi=91.5, role=ZoneRole.SUPPORT,
               state=ZoneState.ACTIVE)      # room = 5.5 > 1.5×3.6 ⇒ 过
    dec = r.on_bar("X", _bar(97.0), 5, _view(zone, tgt), ATR,
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    assert len(dec) == 1
    assert 1.5 < dec[0].hyp.r_mult < 1.6
    # 恰 1.5R 边界:浮点内恰不足 1.5 ⇒ 拒(不容忍带,如实声明)
    r2 = _router()
    tgt2 = Zone(lo=90.5, hi=91.6, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE)
    dec2 = r2.on_bar("X", _bar(97.0), 5, _view(zone, tgt2), ATR,
                     [_e6(-1, ref=99.5)], position_open=False,
                     entry_resting=False)
    assert dec2 == []
    assert r2.counters.get("room_reject_" + ROUTE_STRONG_E6) == 1


# ---------------------------------------------------------------------------
# 臂 B:turtle(无目标,2·atr 防线,不做 room/r_min)
# ---------------------------------------------------------------------------


def test_arm_b_turtle_stop_and_no_target():
    r = _router(arm=ARM_TURTLE)
    zone = Zone(lo=99.5, hi=100.4, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    dec = r.on_bar("X", _bar(97.0), 5, _view(zone), ATR,
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    hyp = dec[0].hyp
    assert hyp.invalid == 97.0 + 2.0 * ATR        # close − d·2·atr(short:上方)
    assert hyp.target_1 is None and hyp.r_mult is None


def test_arm_b_no_room_gate():
    """臂 B:即使完全无结构目标也交易(无目标语义,如实声明)。"""
    r = _router(arm=ARM_TURTLE)
    dec = r.on_bar("X", _bar(97.0), 5, _view(), ATR,  # 空视图
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    assert len(dec) == 1


def test_arm_b_long_mirror():
    r = _router(arm=ARM_TURTLE)
    dec = r.on_bar("X", _bar(103.0), 5, _view(), ATR,
                   [_e6(+1, ref=100.5)], position_open=False,
                   entry_resting=False)
    assert dec[0].hyp.invalid == 103.0 - 2.0 * ATR


# ---------------------------------------------------------------------------
# 双向路由与 episode 关联
# ---------------------------------------------------------------------------


def test_bidirectional_whitelist_and_long_margin():
    """v6 双向:E6|long STRONG 同语义镜像路由(margin = close − ref)。"""
    r = _router()                                # 默认白名单 = ±1 全集
    zone = Zone(lo=99.6, hi=100.5, role=ZoneRole.RESISTANCE,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=111.0, hi=112.0, role=ZoneRole.RESISTANCE,
               state=ZoneState.ACTIVE)
    dec = r.on_bar("X", _bar(103.0), 5, _view(zone, tgt), ATR,
                   [_e6(+1, ref=100.5)], position_open=False,
                   entry_resting=False)
    assert len(dec) == 1 and dec[0].route == ROUTE_STRONG_E6
    assert dec[0].hyp.direction == +1
    assert dec[0].hyp.invalid == 99.6 - K_BM * ATR


def test_weak_episode_direction_isolated():
    """weak-E6|short 登记不消费 E7|long(方向错位 episode 不匹配)。"""
    r = _router()
    r.on_bar("X", _bar(97.0, hi_pad=0.5, lo_pad=0.5), 5, _view(), ATR,
             [_e6(-1, ref=99.5)], position_open=False, entry_resting=False)
    assert r.pending_of("X") is not None         # 弱破位已登记(margin 0.25)
    dec = r.on_bar("X", _bar(99.0, hour=6), 6, _view(), ATR,
                   [_e7(+1, hour=6)], position_open=False, entry_resting=False)
    assert dec == []
    assert r.counters.get("e7_no_episode") == 1
    assert r.pending_of("X") is not None         # 登记保留


def test_weak_episode_consumed_once_by_matching_e7_long():
    r = _router()
    zone = Zone(lo=99.6, hi=100.5, role=ZoneRole.RESISTANCE,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=111.0, hi=112.0, role=ZoneRole.RESISTANCE,
               state=ZoneState.ACTIVE)
    r.on_bar("X", _bar(103.0, hi_pad=0.2, lo_pad=0.2), 5, _view(zone, tgt),
             ATR, [_e6(+1, ref=100.5)], position_open=False,
             entry_resting=False)
    assert r.pending_of("X") is not None         # impulse 0.2 ⇒ WEAK 登记
    dec = r.on_bar("X", _bar(102.0, hour=6), 6, _view(zone, tgt), ATR,
                   [_e7(+1, hour=6, lo=100.2, hi=100.5)], position_open=False,
                   entry_resting=False)
    assert len(dec) == 1 and dec[0].kind == "limit"
    assert dec[0].route == ROUTE_WEAK_E7
    assert dec[0].limit_price == 100.5           # long 前缘 = zone.hi
    assert r.pending_of("X") is None             # 消费一次


def test_unknown_arm_rejected():
    import pytest

    with pytest.raises(ValueError):
        V6Router(V6Params(arm="yolo"))
