"""§7 假设构建测试(docs/methodology.md §7,合成结构视图)。

断言:结构目标(对侧区域近缘 / RANGE_ROTATED 合成带对侧边缘)、invalid 透传、r_mult、
无结构目标时 target_1 = None、attrs 缺 invalid 时 build ⇒ None。
"""

from __future__ import annotations

from market_operator.events import LEVEL_REJECTED, RANGE_ROTATED, MarketEvent
from market_operator.hypothesis import StructureView, build
from market_operator.structure import Regime, Zone, ZoneRole, ZoneState


def _zones(*specs):
    return tuple(Zone(lo=lo, hi=hi, role=role, state=state)
                 for lo, hi, role, state in specs)


def view(regime=Regime.RANGE, zones=(), atr=1.0):
    return StructureView(regime=regime, zones=zones, atr=atr)


# ---------------------------------------------------------------------------
# LEVEL_REJECTED 阻力区拒绝 → 空:target = 沿方向最近的 ACTIVE 支撑区上缘(hi)
# ---------------------------------------------------------------------------


def test_short_target_nearest_support_hi():
    zones = _zones(
        (100.0, 101.0, ZoneRole.RESISTANCE, ZoneState.ACTIVE),   # 事件所涉(同侧)
        (96.0, 97.0, ZoneRole.SUPPORT, ZoneState.ACTIVE),        # 最近对侧
        (90.0, 91.0, ZoneRole.SUPPORT, ZoneState.ACTIVE),        # 更远
        (98.5, 99.5, ZoneRole.SUPPORT, ZoneState.BROKEN),        # 状态不符,排除
    )
    e = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=0, known_index=10,
                    attrs={"invalid": 101.1, "ref": 100.0,
                           "zone": zones[0]})
    h = build(e, view(zones=zones), None, entry=99.0)
    assert h is not None
    assert h.direction == -1
    assert h.invalid == 101.1
    assert h.target_1 == 97.0                       # 最近 ACTIVE 支撑区 hi
    # r_mult = -(97-99) / -(99-101.1) = 2/2.1
    assert abs(h.r_mult - 2.0 / 2.1) < 1e-12


def test_long_target_nearest_resistance_lo():
    zones = _zones(
        (96.0, 97.0, ZoneRole.SUPPORT, ZoneState.ACTIVE),
        (100.0, 101.0, ZoneRole.RESISTANCE, ZoneState.RETESTED),  # RETESTED 可选
        (104.0, 105.0, ZoneRole.RESISTANCE, ZoneState.ACTIVE),
    )
    e = MarketEvent(id=LEVEL_REJECTED, direction=+1, ts=0, known_index=10,
                    attrs={"invalid": 95.9, "ref": 97.0,
                           "zone": zones[0]})
    h = build(e, view(zones=zones), None, entry=98.0)
    assert h.target_1 == 100.0
    # r_mult = (100-98)/(98-95.9) = 2/2.1
    assert abs(h.r_mult - 2.0 / 2.1) < 1e-12


def test_no_structural_target_is_none_not_fabricated():
    e = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=0, known_index=10,
                    attrs={"invalid": 101.1, "ref": 100.0,
                           "zone": Zone(lo=100, hi=101,
                                        role=ZoneRole.RESISTANCE)})
    h = build(e, view(zones=()), None, entry=99.0)
    assert h is not None
    assert h.target_1 is None                       # 不编造固定 R 目标
    assert h.r_mult is None


def test_zone_straddling_ref_is_not_ahead_target():
    # 支撑区 hi 高于 ref(骑跨事件锚点)不算"前方"目标
    zones = _zones((99.0, 100.5, ZoneRole.SUPPORT, ZoneState.ACTIVE))
    e = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=0, known_index=10,
                    attrs={"invalid": 101.1, "ref": 100.0,
                           "zone": Zone(lo=100, hi=101,
                                        role=ZoneRole.RESISTANCE)})
    h = build(e, view(zones=zones), None, entry=99.5)
    assert h.target_1 is None


# ---------------------------------------------------------------------------
# RANGE_ROTATED:合成带对侧边缘
# ---------------------------------------------------------------------------


def test_e5_target_opposite_band_edge():
    e = MarketEvent(id=RANGE_ROTATED, direction=-1, ts=0, known_index=10,
                    attrs={"invalid": 101.1, "ref": 101.0,
                           "zone": Zone(lo=100, hi=101,
                                        role=ZoneRole.RESISTANCE),
                           "band": (95.0, 101.0)})
    h = build(e, view(zones=_zones((92.0, 93.0, ZoneRole.SUPPORT,
                                    ZoneState.ACTIVE))), None)
    assert h.target_1 == 95.0                       # band_lo,不做区域搜索

    e_long = MarketEvent(id=RANGE_ROTATED, direction=+1, ts=0, known_index=10,
                         attrs={"invalid": 94.9, "ref": 95.0,
                                "zone": Zone(lo=95, hi=96,
                                             role=ZoneRole.SUPPORT),
                                "band": (95.0, 101.0)})
    h2 = build(e_long, view(zones=()), None)
    assert h2.target_1 == 101.0                     # band_hi


# ---------------------------------------------------------------------------
# entry 由研究器代入 / 缺 invalid ⇒ None
# ---------------------------------------------------------------------------


def test_entry_filled_later_by_researcher():
    zones = _zones((100.0, 101.0, ZoneRole.RESISTANCE, ZoneState.ACTIVE))
    e = MarketEvent(id=LEVEL_REJECTED, direction=+1, ts=0, known_index=10,
                    attrs={"invalid": 95.9, "ref": 97.0,
                           "zone": Zone(lo=96, hi=97, role=ZoneRole.SUPPORT)})
    h0 = build(e, view(zones=zones), None)
    assert h0.entry is None and h0.r_mult is None
    h1 = h0.with_entry(98.0)
    assert h1.entry == 98.0
    assert abs(h1.r_mult - 2.0 / 2.1) < 1e-12
    assert h0.entry is None                         # frozen,原对象不变


def test_entry_beyond_invalid_gives_none_r_mult():
    zones = _zones((100.0, 101.0, ZoneRole.RESISTANCE, ZoneState.ACTIVE))
    e = MarketEvent(id=LEVEL_REJECTED, direction=+1, ts=0, known_index=10,
                    attrs={"invalid": 95.9, "ref": 97.0,
                           "zone": Zone(lo=96, hi=97, role=ZoneRole.SUPPORT)})
    h = build(e, view(zones=zones), None, entry=95.0)   # 入场已在失效价之下
    assert h.r_mult is None


def test_missing_invalid_returns_none():
    e = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=0, known_index=10, attrs={})
    assert build(e, view(zones=()), None) is None
