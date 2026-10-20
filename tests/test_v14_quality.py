"""v14 C1 优质五问/C5 档位(docs/v14-protocol.md)边界与档位测试。

钉死协议口径:Q1 恰 0.35/0.65 边界 inclusive、缺带 fail-closed;Q2 恰 1.5R、
对面缘缺失=空间无界=过、risk_px 退化拒绝;Q5 F1 ≥0.8·ATR(距离不足/无 F1
即拒);Q3 k>k_* 成熟、k=k_* 不成熟、无 zone 不成熟;Q4 顺风定义;档 A=1.0 /
B=0.75 / 拒=0.0;evaluate_quality 确定性(同输入两遍逐字段等)。
"""

from __future__ import annotations

import pytest

from market_operator.quality import (
    QualityParams,
    V14Params,
    evaluate_quality,
    opposite_edge_R,
    swing_band_pos,
)
from market_operator.structure import (
    Pivot,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)

P = QualityParams()
ATR = 2.0


def _pivots(hi=110.0, lo=100.0):
    """最近 LOW=lo、HIGH=hi(按名单序,最新在后)。"""
    return (Pivot(price=hi, forming_index=0, known_index=1,
                  kind=PivotKind.HIGH),
            Pivot(price=lo, forming_index=2, known_index=3,
                  kind=PivotKind.LOW))


def _zones_resistance(edge=130.0, state=ZoneState.ACTIVE, touches=3):
    return (Zone(lo=edge, hi=edge + 2.0, role=ZoneRole.RESISTANCE,
                 state=state, touches=touches),)


def _zones_support(edge=80.0, state=ZoneState.ACTIVE, touches=3):
    return (Zone(lo=edge - 2.0, hi=edge, role=ZoneRole.SUPPORT,
                 state=state, touches=touches),)


def _eval(**kw):
    arg = dict(direction=-1, entry_ref=104.0, invalid=104.0 + 2 * ATR,
               atr=ATR, zones=_zones_support(), pivots=_pivots(),
               regime=Regime.TREND_DOWN, zone_touches=3)
    arg.update(kw)
    return evaluate_quality(P, **arg)


# ---------------------------------------------------------------------------
# 摆动带位置 / Q1(pos 恰 0.35 / 0.65;缺带 fail-closed)
# ---------------------------------------------------------------------------


def test_swing_band_pos_and_boundary_short_at_035_passes():
    # 带 [100,110];pos = 0.35 ⇒ entry=103.5;空 pos≥0.35 恰过
    pos = swing_band_pos(_pivots(), 103.5)
    assert pos == pytest.approx(0.35)
    v = _eval(entry_ref=103.5, invalid=103.5 + 2 * ATR)
    assert v.q1_pos is True and v.tradable
    # pos 恰低于 0.35 ⇒ 拒(错半边不交易)
    v2 = _eval(entry_ref=103.499, invalid=103.499 + 2 * ATR)
    assert v2.q1_pos is False and not v2.tradable
    assert "q1_pos" in v2.rejects and v2.size_mult == 0.0


def test_boundary_long_at_065_passes_and_above_rejects():
    v = _eval(direction=1, entry_ref=106.5, invalid=106.5 - 2 * ATR,
              zones=_zones_resistance(), regime=Regime.TREND_UP)
    assert v.q1_pos is True and v.tradable
    v2 = _eval(direction=1, entry_ref=106.501, invalid=106.501 - 2 * ATR,
               zones=_zones_resistance(), regime=Regime.TREND_UP)
    assert v2.q1_pos is False and not v2.tradable


def test_q1_missing_band_side_fail_closed():
    only_high = (Pivot(price=110.0, forming_index=0, known_index=1,
                       kind=PivotKind.HIGH),)
    v = _eval(pivots=only_high)
    assert v.pos is None and v.q1_pos is False and not v.tradable
    degenerate = _pivots(hi=100.0, lo=100.0)
    assert swing_band_pos(degenerate, 101.0) is None
    assert _eval(pivots=degenerate).q1_pos is False


# ---------------------------------------------------------------------------
# Q2 对面缘距离(恰 1.5R / 缘缺失=无界=过 / 距离不足)
# ---------------------------------------------------------------------------


def test_q2_exactly_1p5R_passes_and_below_rejects():
    # 空,entry=104,risk=4(2·atr=4),恰 1.5R ⇒ 对面支撑缘 hi = 104 − 6 = 98
    v = _eval(zones=_zones_support(edge=98.0))
    assert v.room_R == pytest.approx(1.5) and v.q2_room is True
    v2 = _eval(zones=_zones_support(edge=98.01))
    assert v2.q2_room is False and not v2.tradable
    assert "q2_room" in v2.rejects


def test_q2_no_opposite_edge_is_unbounded_pass():
    v = _eval(zones=())
    assert v.room_R is None and v.q2_room is True and v.tradable
    # 状态过滤:BROKEN 区不算对面缘(§7 TARGET_STATES 口径)
    v2 = _eval(zones=_zones_support(state=ZoneState.BROKEN))
    assert v2.room_R is None and v2.q2_room is True
    # 同向不越 entry_ref 的区不算
    v3 = _eval(zones=_zones_support(edge=105.0))
    assert v3.room_R is None and v3.q2_room is True


# ---------------------------------------------------------------------------
# Q5 失效定结构 + F1 ≥ 0.8·ATR(无 F1 / 距离不足即拒)
# ---------------------------------------------------------------------------


def test_q5_f1_distance_shortfall_rejects():
    # 空,entry=104,invalid 仅 0.5·atr 之外 ⇒ dist=1.0 < 0.8·2=1.6 拒
    v = _eval(invalid=104.0 + 0.5 * ATR, zones=_zones_support(edge=90.0))
    assert v.q5_f1 is False and not v.tradable and "q5_f1" in v.rejects
    # 恰 0.8·ATR(atr=0.5 位精确)⇒ 过(与 canonical F1 同算术同阈)
    v2 = _eval(invalid=104.0 + 0.4, atr=0.5, zones=_zones_support(edge=90.0))
    assert v2.q5_f1 is True and v2.tradable


def test_q5_no_f1_possible_rejects():
    # atr=0 ⇒ F1 不可判 ⇒ fail-closed;invalid 穿透方向 ⇒ dist≤0 拒
    v = _eval(atr=0.0)
    assert v.q5_f1 is False and not v.tradable
    v2 = _eval(invalid=90.0)          # 空单失效在下方 ⇒ dist<0
    assert v2.q5_f1 is False and not v2.tradable


# ---------------------------------------------------------------------------
# Q3 k 成熟(档注)/ Q4 顺风 / 档位 A=1.0 B=0.75
# ---------------------------------------------------------------------------


def test_tier_a_requires_q3_and_q4_else_b():
    # 全过 + 顺风 + k 成熟(3 > k_*=2) ⇒ A=1.0
    v = _eval()
    assert v.tradable and v.tier == "A" and v.size_mult == 1.0
    # 不顺风(RANGE) ⇒ B=0.75
    v2 = _eval(regime=Regime.RANGE)
    assert v2.tradable and v2.tier == "B" and v2.size_mult == 0.75
    # k=2(=k_*,不成熟) ⇒ B;无 zone(E6 类) ⇒ B;逆 regime ⇒ B
    assert _eval(zone_touches=2).tier == "B"
    assert _eval(zone_touches=None).tier == "B"
    v3 = _eval(regime=Regime.TREND_UP)     # 空单逆风
    assert v3.q4_tailwind is False and v3.tier == "B"
    # 顺风 + 不成熟 ⇒ B(Q3∧Q4 才算全过档)
    v4 = _eval(zone_touches=1)
    assert v4.q4_tailwind is True and v4.q3_mature is False \
        and v4.tier == "B" and v4.size_mult == 0.75


def test_evaluate_quality_is_deterministic():
    a = _eval()
    b = _eval()
    assert a == b
    x = opposite_edge_R(_zones_resistance(), 1, 106.5, 4.0)
    y = opposite_edge_R(_zones_resistance(), 1, 106.5, 4.0)
    assert x == y == pytest.approx((130.0 - 106.5) / 4.0)


def test_v14params_quality_none_keeps_v6_defaults():
    p = V14Params()
    assert p.quality is None and p.k_dq == 0.5 and p.turtle_stop_atr == 2.0
