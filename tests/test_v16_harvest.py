"""v16 收割婚配 + 三闸集成(docs/v16-protocol.md)边界与婚配测试。

钉死协议口径:位置恰 0.35/0.65 inclusive、空间恰 1.5R(对面缘缺失=无界=过)、
F1 恰 0.8·ATR(inclusive,结构失效锚);顺风档 A=1.0/其余 B=0.75(k 履历只作
档注不参与定档);fade 前缘改单(空=zone.lo/多=zone.hi)与对面缘=收割目标;
时间帽 k_cap×TF 恰界 MFE=0.5R inclusive 持有(<0.5 才出);fade 收割全平+
禁 trail、续涨保留 turtle;不加仓;arm0 逐位回归;确定性;config/v16.json
parent=canonical 硬对账。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from market_operator.campaign import ArmConfig
from market_operator.events import E2, E4, E6, E8, MarketEvent
from market_operator.harvest import (
    HarvestParams,
    V16Params,
    V16Router,
    evaluate_v16,
    fade_front_edge,
    opposite_edge_price,
)
from market_operator.hypothesis import StructureView
from market_operator.position import PositionManager
from market_operator.strategy_v6 import V6Params, V6Router
from market_operator.structure import (
    Pivot,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)

P = HarvestParams()
ATR = 2.0


def _pivots(hi=110.0, lo=90.0):
    """最近 LOW=lo、HIGH=hi(按名单序,最新在后)。"""
    return (Pivot(price=hi, forming_index=0, known_index=1,
                  kind=PivotKind.HIGH),
            Pivot(price=lo, forming_index=2, known_index=3,
                  kind=PivotKind.LOW))


def _res(lo=100.0, hi=102.0, state=ZoneState.ACTIVE, touches=3):
    return Zone(lo=lo, hi=hi, role=ZoneRole.RESISTANCE, state=state,
                touches=touches)


def _sup(lo, hi, state=ZoneState.ACTIVE, touches=3):
    return Zone(lo=lo, hi=hi, role=ZoneRole.SUPPORT, state=state,
                touches=touches)


def _eval(**kw):
    arg = dict(direction=-1, entry_ref=100.0, invalid=102.2, atr=ATR,
               zones=(_sup(95.0, 96.4),), pivots=_pivots(),
               regime=Regime.RANGE, zone_touches=3)
    arg.update(kw)
    return evaluate_v16(P, **arg)


# ---------------------------------------------------------------------------
# 闸①位置(恰 0.35 / 0.65 inclusive;缺带 fail-closed)
# ---------------------------------------------------------------------------


def test_q1_boundary_short_035_pass_below_rejects():
    # 带 [90,110]:pos=0.5 过;pos 恰 0.35 过(inclusive);0.349 拒
    assert _eval().q1_pos is True and _eval().tradable
    v = _eval(entry_ref=97.0, invalid=97.0 + 1.1 * ATR,
              zones=(_sup(90.0, 93.0),))
    pos = v.pos
    assert pos == pytest.approx(0.35) and v.q1_pos is True and v.tradable
    v2 = _eval(entry_ref=96.979, invalid=96.979 + 1.1 * ATR,
               zones=(_sup(90.0, 93.0),))
    assert v2.q1_pos is False and not v2.tradable
    assert "q1_pos" in v2.rejects and v2.size_mult == 0.0


def test_q1_boundary_long_065_pass_above_rejects():
    v = _eval(direction=1, entry_ref=103.0, invalid=103.0 - 1.1 * ATR,
              zones=(_res(106.6, 108.0),), regime=Regime.TREND_UP)
    assert v.pos == pytest.approx(0.65) and v.q1_pos is True and v.tradable
    v2 = _eval(direction=1, entry_ref=103.001, invalid=103.001 - 1.1 * ATR,
               zones=(_res(106.6, 108.0),), regime=Regime.TREND_UP)
    assert v2.q1_pos is False and not v2.tradable


def test_q1_missing_band_fail_closed():
    only_high = (Pivot(price=110.0, forming_index=0, known_index=1,
                       kind=PivotKind.HIGH),)
    v = _eval(pivots=only_high)
    assert v.pos is None and v.q1_pos is False and not v.tradable
    assert "q1_pos" in v.rejects


# ---------------------------------------------------------------------------
# 闸②空间(恰 1.5R inclusive;对面缘缺失=无界=过;risk 退化拒)
# ---------------------------------------------------------------------------


def test_q2_exact_1p5R_pass_below_rejects():
    # 空:entry=100,risk=2.0 ⇒ 恰 1.5R:对面支撑 hi = 100 − 3.0 = 97.0(整除钉界)
    v = _eval(invalid=102.0, zones=(_sup(95.0, 97.0),))
    assert v.room_R == 1.5 and v.q2_room is True and v.tradable
    assert v.target == 97.0
    v2 = _eval(invalid=102.0, zones=(_sup(95.0, 97.01),))
    assert v2.q2_room is False and not v2.tradable and "q2_room" in v2.rejects


def test_q2_no_opposite_edge_is_unbounded_pass():
    v = _eval(zones=())
    assert v.room_R is None and v.target is None and v.q2_room is True
    # BROKEN 状态不算对面缘(TARGET_STATES 口径)
    v2 = _eval(zones=(_sup(95.0, 96.4, state=ZoneState.BROKEN),))
    assert v2.room_R is None and v2.q2_room is True
    # 反向区(未越过 entry_ref)不算
    v3 = _eval(zones=(_sup(95.0, 101.0),))
    assert v3.room_R is None and v3.q2_room is True


def test_opposite_edge_price_picks_nearest_bilateral():
    zones = (_sup(80.0, 90.0), _sup(94.0, 96.4), _sup(96.5, 96.9))
    assert opposite_edge_price(zones, -1, 100.0) == 96.9      # 最近的支撑 hi 最大者
    assert opposite_edge_price(zones, 1, 90.0) is None        # 多向无阻力 ⇒ 无界
    zones2 = (_res(101.0, 103.0), _res(105.0, 107.0))
    assert opposite_edge_price(zones2, 1, 100.0) == 101.0     # 最近阻力 lo 最小者


# ---------------------------------------------------------------------------
# 闸③ F1(结构失效锚;恰 0.8·ATR inclusive;非保护/无 ATR 拒)
# ---------------------------------------------------------------------------


def test_f1_exact_0p8atr_pass_below_rejects():
    # dist = 102.2 − 100.6 = 1.6 = 0.8·ATR 恰过;invalid 再近一点即拒
    v = _eval(entry_ref=100.6, invalid=102.2)
    assert v.f1 is True and v.tradable
    v2 = _eval(entry_ref=100.61, invalid=102.2)
    assert v2.f1 is False and not v2.tradable and "f1" in v2.rejects


def test_f1_non_protective_invalid_rejects():
    # 空:invalid 低于 entry ⇒ dist < 0 ⇒ 拒(fail-closed;不人工加宽)
    v = _eval(invalid=99.0)
    assert v.f1 is False and not v.tradable
    v2 = _eval(atr=0.0)
    assert v2.f1 is False and not v2.tradable


# ---------------------------------------------------------------------------
# 档(顺风=A×1.0、其余=B×0.75;k 履历只作档注不参与定档)
# ---------------------------------------------------------------------------


def test_tier_tailwind_only_q3_is_annotation():
    v = _eval(regime=Regime.TREND_DOWN)                        # 空+下跌趋势=顺风
    assert v.tier == "A" and v.size_mult == 1.0 and v.q4_tailwind
    v2 = _eval(regime=Regime.RANGE, zone_touches=10)           # 成熟但无顺风 ⇒ B
    assert v2.tier == "B" and v2.size_mult == 0.75
    assert v2.q3_mature is True and v2.q4_tailwind is False
    v3 = _eval(regime=Regime.RANGE, zone_touches=1)            # 不成熟 ⇒ 仍 B
    assert v3.tier == "B" and v3.q3_mature is False


# ---------------------------------------------------------------------------
# 路由:fade 前缘改单 + 结构锚 + 对面缘=收割目标;续涨保留 turtle 语义
# ---------------------------------------------------------------------------

_WL = frozenset({("E2", -1), ("E2", 1), ("E6", -1), ("E6", 1), ("E8", -1)})


def _bar(close, ts=1_700_000_000_000, o=100.0, h=103.0, l=98.0):
    return (ts, o, h, l, close, 1.0)


def _view(regime, zones, pivots=None):
    return StructureView(regime=regime, zones=tuple(zones), atr=ATR,
                         pivots=tuple(pivots or _pivots()))


def _fade_router():
    return V16Router(V16Params(whitelist=_WL, v16=P))


def _e2_short(zone=None, **extra):
    zone = zone or _res()
    attrs = {"zone": zone, "ref": zone.hi, "invalid": zone.hi + 0.1 * ATR,
             "puncture_index": 1}
    attrs.update(extra)
    return MarketEvent(id=E2, direction=-1, ts=1_700_000_000_000,
                       known_index=10, attrs=attrs)


def test_fade_market_decision_rewritten_to_front_edge_limit():
    r = _fade_router()
    zones = [_res(), _sup(95.0, 96.4)]
    ds = r.on_bar("T", _bar(101.0), 10, _view(Regime.RANGE, zones), ATR,
                  [_e2_short()], position_open=False, entry_resting=False)
    assert len(ds) == 1
    d = ds[0]
    assert d.kind == "limit"
    assert d.limit_price == 100.0                       # 空 fade 前缘=zone.lo
    assert d.meta["limit_from"] == "v16_fade_front_edge"
    # 结构锚:invalid = attrs 结构失效(非 turtle 2·ATR),target=对面缘 96.4
    assert d.hyp.invalid == pytest.approx(102.2)
    assert d.hyp.target_1 == pytest.approx(96.4)
    assert d.hyp.entry == pytest.approx(100.0)
    assert d.hyp.r_mult == pytest.approx((100.0 - 96.4) / 2.2)
    assert d.meta["harvest_target"] == pytest.approx(96.4)
    assert d.meta["struct_invalid"] == pytest.approx(102.2)
    # 对面缘=收割目标与空间闸同源:room_R = 3.6/2.2 ≥ 1.5
    assert d.meta["v16_room_R"] == pytest.approx(3.6 / 2.2)
    # long 镜像:前缘 = zone.hi(对面缘 95.6 ⇒ room=3.6/2.2>1.5)
    r2 = _fade_router()
    zsup = _sup(90.0, 92.0)
    ev = MarketEvent(id=E2, direction=1, ts=1_700_000_000_000, known_index=10,
                     attrs={"zone": zsup, "ref": zsup.lo,
                            "invalid": zsup.lo - 0.1 * ATR})
    zones2 = [zsup, _res(95.6, 97.0)]
    ds2 = r2.on_bar("T", _bar(91.0), 10, _view(Regime.TREND_UP, zones2), ATR,
                    [ev], position_open=False, entry_resting=False)
    assert len(ds2) == 1 and ds2[0].limit_price == 92.0  # 多 fade 前缘=zone.hi
    assert ds2[0].hyp.invalid == pytest.approx(89.8)
    assert ds2[0].hyp.target_1 == pytest.approx(95.6)


def test_fade_front_edge_helper_bilateral():
    assert fade_front_edge(10.0, 12.0, -1) == 10.0
    assert fade_front_edge(10.0, 12.0, 1) == 12.0


def test_e8_fade_uses_structural_sweep_invalid():
    r = _fade_router()
    ev = MarketEvent(id=E8, direction=-1, ts=1_700_000_000_000, known_index=10,
                     attrs={"zone": _res(), "ref": 102.0,
                            "invalid": 103.6})          # 刺穿极值外侧(非区缘)
    zones = [_res(), _sup(90.0, 93.0)]
    ds = r.on_bar("T", _bar(101.0), 10, _view(Regime.RANGE, zones), ATR, [ev],
                  position_open=False, entry_resting=False)
    assert len(ds) == 1
    assert ds[0].hyp.invalid == pytest.approx(103.6)    # E8 结构锚=刺穿极值
    assert ds[0].limit_price == 100.0                   # 前缘仍=zone.lo


def test_continuation_keeps_turtle_no_target_structural_invalid():
    r = _fade_router()
    ev = MarketEvent(id=E6, direction=-1, ts=1_700_000_000_000, known_index=10,
                     attrs={"ref": 100.0, "invalid": 100.2})
    zones = [_sup(94.2, 95.2)]
    ds = r.on_bar("T", _bar(98.2), 10, _view(Regime.TREND_DOWN, zones), ATR,
                  [ev], position_open=False, entry_resting=False)
    assert len(ds) == 1
    d = ds[0]
    assert d.kind == "market"                           # 续涨不改单(提交侧 C3)
    assert d.hyp.target_1 is None                       # 续涨保留 turtle:无目标
    assert d.hyp.invalid == pytest.approx(100.2)        # 结构锚替换 turtle
    assert d.meta["v16_tier"] == "A"                    # 空+下跌趋势=顺风 A
    assert d.meta["size_mult"] == 1.0


# ---------------------------------------------------------------------------
# 路由:三闸拒单计数 / fail-closed / arm0 回归 / 确定性
# ---------------------------------------------------------------------------


def test_gate_reject_counters_and_drop():
    # Q1 拒:带 [98,99](窄带),fade 前缘 100 ⇒ pos>1 空侧过?? 用窄带顶拒:
    # pos=(100−98)/(99−98)=2 ⇒ short 需 ≥0.35 ⇒ 过;构造拒:带 [100,130]:
    # pos=0 ⇒ <0.35 ⇒ 拒
    r = _fade_router()
    view = _view(Regime.RANGE, [_res(), _sup(95.0, 96.4)],
                 pivots=_pivots(hi=130.0, lo=100.0))
    ds = r.on_bar("T", _bar(101.0), 10, view, ATR, [_e2_short()],
                  position_open=False, entry_resting=False)
    assert ds == [] and r.counters.get("v16_gate_reject_q1_pos") == 1
    assert r.counters.get("v16_gate_evaluated") == 1
    # Q2 拒:对面缘 97.0 ⇒ room=(100−97)/2.2 <1.5
    r = _fade_router()
    ds = r.on_bar("T", _bar(101.0), 10,
                  _view(Regime.RANGE, [_res(), _sup(95.0, 97.0)]), ATR,
                  [_e2_short()], position_open=False, entry_resting=False)
    assert ds == [] and r.counters.get("v16_gate_reject_q2_room") == 1
    # F1 拒:invalid 紧贴前缘(dist=0.1·ATR < 0.8·ATR)
    r = _fade_router()
    ds = r.on_bar("T", _bar(101.0), 10,
                  _view(Regime.RANGE, [_res(), _sup(95.0, 96.4)]), ATR,
                  [_e2_short(invalid=100.2)], position_open=False,
                  entry_resting=False)
    assert ds == [] and r.counters.get("v16_gate_reject_f1") == 1


def test_fade_fail_closed_no_zone_and_no_invalid():
    r = _fade_router()
    ev = MarketEvent(id=E2, direction=-1, ts=1_700_000_000_000, known_index=10,
                     attrs={"ref": 102.0, "invalid": 102.2})   # 缺 zone
    ds = r.on_bar("T", _bar(101.0), 10,
                  _view(Regime.RANGE, [_sup(95.0, 96.4)]), ATR, [ev],
                  position_open=False, entry_resting=False)
    assert ds == [] and r.counters.get("v16_reject_no_zone") == 1
    r = _fade_router()
    ev2 = MarketEvent(id=E6, direction=-1, ts=1_700_000_000_000,
                      known_index=10, attrs={"ref": 100.0})     # 缺 invalid
    ds2 = r.on_bar("T", _bar(98.2), 10,
                   _view(Regime.RANGE, [_sup(94.2, 95.2)]), ATR, [ev2],
                   position_open=False, entry_resting=False)
    assert ds2 == [] and r.counters.get("v16_reject_no_invalid") == 1


def test_arm0_v16_none_passthrough_identical_to_v6():
    evs = [_e2_short(),
           MarketEvent(id=E6, direction=-1, ts=1_700_000_000_000,
                       known_index=10, attrs={"ref": 100.0, "invalid": 100.2})]
    zones = [_res(), _sup(94.2, 96.4)]
    kw = dict(position_open=False, entry_resting=False)
    a = V6Router(V6Params(whitelist=_WL)).on_bar(
        "T", _bar(101.0), 10, _view(Regime.RANGE, zones), ATR, evs, **kw)
    b = V16Router(V16Params(whitelist=_WL, v16=None)).on_bar(
        "T", _bar(101.0), 10, _view(Regime.RANGE, zones), ATR, evs, **kw)
    assert len(a) == len(b)
    for da, db in zip(a, b):
        assert da.kind == db.kind and da.route == db.route
        assert da.limit_price == db.limit_price and da.hyp == db.hyp


def test_router_determinism():
    evs = [_e2_short()]
    zones = [_res(), _sup(95.0, 96.4)]
    got = []
    for _ in range(2):
        r = _fade_router()
        got.append(r.on_bar("T", _bar(101.0), 10, _view(Regime.RANGE, zones),
                            ATR, evs, position_open=False, entry_resting=False))
    assert got[0] == got[1]
    v1 = _eval()
    v2 = _eval()
    assert v1 == v2


# ---------------------------------------------------------------------------
# 持仓:v16 收割全平 + 禁 trail;续涨 turtle 保留;时间帽恰界;不加仓;分桶
# ---------------------------------------------------------------------------


def _pm():
    return PositionManager(allow_add=False, breakeven_on_reduce=True,
                           time_stop=False, harvest_family=("E2", "E3", "E8"),
                           time_cap_bars=48, cap_mfe_min=0.5)


def _open_fade(pm, direction=-1, entry=100.0, invalid=102.2, target=96.4):
    return pm.open("T", direction, entry, invalid, target, "E2", 0, 0)


def test_harvest_target_full_exit_no_reduce():
    pm = _pm()
    pos = _open_fade(pm)
    # 触及对面缘 ⇒ 一次性 EXIT 全平(非 §8.2 50% 减仓)
    intents = pm.on_bar("T", (1, 99.0, 99.5, 95.0, 96.0, 1.0), ATR)
    assert len(intents) == 1
    assert intents[0].kind.name == "EXIT"
    assert intents[0].qty_frac == 1.0
    assert intents[0].reason.startswith("v16|target|")
    assert "T" not in pm.positions and pos.size_frac == 0.0 and not pos.reduced


def test_harvest_disables_trail_continuation_keeps_turtle():
    pm = _pm()
    fade = _open_fade(pm)
    pv = (Pivot(price=101.0, forming_index=1, known_index=1,
                kind=PivotKind.HIGH),)
    pm.on_bar("T", (1, 100.0, 101.5, 99.5, 100.5, 1.0), ATR, new_pivots=pv)
    assert fade.invalid == pytest.approx(102.2)          # fade:trail 禁用,不动
    pm2 = _pm()
    cont = pm2.open("T", -1, 100.0, 102.2, None, "E6", 0, 0)
    pm2.on_bar("T", (1, 100.0, 101.5, 99.5, 100.5, 1.0), ATR, new_pivots=pv)
    assert cont.invalid == pytest.approx(101.0 + 0.1 * ATR)  # 续涨:trail 收紧


def test_exit_bucket_tags_invalid_vs_trail_vs_choch():
    # 失效桶:防线未动的原始结构失效出场
    pm = _pm()
    _open_fade(pm)
    intents = pm.on_bar("T", (1, 100.0, 103.0, 99.9, 102.5, 1.0), ATR)
    assert intents[0].reason.startswith("v16|invalid|")
    # trail 桶:续涨防线收紧后破位
    pm = _pm()
    pos = pm.open("T", -1, 100.0, 102.2, None, "E6", 0, 0)
    pv = (Pivot(price=100.5, forming_index=1, known_index=1,
                kind=PivotKind.HIGH),)
    pm.on_bar("T", (1, 100.0, 100.9, 99.6, 100.0, 1.0), ATR, new_pivots=pv)
    assert pos.invalid == pytest.approx(100.7)
    intents = pm.on_bar("T", (2, 100.0, 101.0, 99.8, 100.75, 1.0), ATR)
    assert intents[0].reason.startswith("v16|trail|")
    # CHoCH 桶
    pm = _pm()
    _open_fade(pm)
    ch = MarketEvent(id="E6", direction=1, ts=1, known_index=1,
                     attrs={"ref": 95.0, "invalid": 95.2})
    intents = pm.on_bar("T", (1, 100.0, 100.5, 99.5, 100.0, 1.0), ATR,
                        new_events=[ch])
    assert intents[0].reason.startswith("v16|choch|")


def test_time_cap_boundary_mfe_exactly_0p5_holds():
    pm = PositionManager(allow_add=False, breakeven_on_reduce=True,
                         time_stop=False, harvest_family=("E2",),
                         time_cap_bars=2, cap_mfe_min=0.5)
    pos = pm.open("T", 1, 100.0, 98.0, None, "E6", 0, 0)   # risk=2
    # bar1:MFE 恰 0.5R(high=101);bar2:bars_held=2 到期 ⇒ 不放逐
    pm.on_bar("T", (1, 100.0, 101.0, 99.5, 100.2, 1.0), ATR)
    assert pos.mfe_r == pytest.approx(0.5)
    intents = pm.on_bar("T", (2, 100.2, 100.6, 99.6, 100.0, 1.0), ATR)
    assert intents == [] and "T" in pm.positions           # MFE≥0.5R 不放逐
    # MFE < 0.5R ⇒ 出
    pm = PositionManager(allow_add=False, breakeven_on_reduce=True,
                         time_stop=False, harvest_family=("E2",),
                         time_cap_bars=2, cap_mfe_min=0.5)
    pm.open("T", 1, 100.0, 98.0, None, "E6", 0, 0)
    pm.on_bar("T", (1, 100.0, 100.9, 99.5, 100.2, 1.0), ATR)   # mfe=0.45
    intents = pm.on_bar("T", (2, 100.2, 100.6, 99.6, 100.0, 1.0), ATR)
    assert len(intents) == 1 and intents[0].kind.name == "EXIT"
    assert intents[0].reason.startswith("v16|time_cap|")
    # 未到期(bars_held<cap)即使 MFE<0.5 也不出
    pm = PositionManager(allow_add=False, breakeven_on_reduce=True,
                         time_stop=False, time_cap_bars=3, cap_mfe_min=0.5)
    pm.open("T", 1, 100.0, 98.0, None, "E6", 0, 0)
    pm.on_bar("T", (1, 100.0, 100.4, 99.5, 100.0, 1.0), ATR)
    assert pm.on_bar("T", (2, 100.0, 100.3, 99.6, 100.0, 1.0), ATR) == []


def test_no_adds_fade_and_continuation_v16():
    pm = _pm()
    pos = pm.open("T", 1, 100.0, 96.0, 110.0, "E2", 0, 0)  # fade 多
    e4 = MarketEvent(id=E4, direction=1, ts=1, known_index=1,
                     attrs={"ref": 101.0, "invalid": 99.5})
    # 浮盈 ≥1R(MFE=5/4=1.25R)同向 E4:fade 不加仓(协议;allow_add=False 全局)
    intents = pm.on_bar("T", (1, 100.0, 105.0, 99.9, 104.0, 1.0), ATR,
                        new_events=[e4])
    assert all(i.kind.name != "ADD" for i in intents)
    assert pos.size_frac == 1.0


def test_canonical_position_passthrough_untouched():
    # harvest_family/time_cap 全 None ⇒ 逐位 canonical(§8.2 减仓/§8.3/语义无桶签)
    pm = PositionManager(allow_add=False, breakeven_on_reduce=True,
                         time_stop=False)
    pos = pm.open("T", -1, 100.0, 102.2, 96.4, "E2", 0, 0)
    pv = (Pivot(price=101.0, forming_index=1, known_index=1,
                kind=PivotKind.HIGH),)
    intents = pm.on_bar("T", (1, 100.0, 101.5, 99.5, 100.5, 1.0), ATR,
                        new_pivots=pv)
    assert pos.invalid == pytest.approx(101.2)             # trail 照常
    assert intents == []
    intents = pm.on_bar("T", (2, 100.0, 100.5, 95.0, 96.0, 1.0), ATR)
    assert intents[0].kind.name == "REDUCE"                # §8.2 50% 减仓照旧
    assert "v16|" not in intents[0].reason


def test_armconfig_v16_fields_default_canonical():
    a = ArmConfig()
    assert a.harvest_family is None and a.time_cap_bars is None
    assert a.cap_mfe_min == 0.5
    a2 = ArmConfig(harvest_family=("E2", "E3", "E8"), time_cap_bars=48)
    assert a2.harvest_family == ("E2", "E3", "E8") and a2.time_cap_bars == 48


# ---------------------------------------------------------------------------
# config/v16.json:parent=canonical 共享键硬对账 + 冻结先验
# ---------------------------------------------------------------------------


def test_config_v16_parent_canonical_and_frozen_priors():
    root = pathlib.Path(__file__).resolve().parents[1]
    v16 = json.loads((root / "config" / "v16.json").read_text(encoding="utf-8"))
    canon = json.loads((root / "config" / "canonical.json").read_text(
        encoding="utf-8"))
    for k, v in canon.items():
        if k in ("version", "meta", "note"):
            continue
        assert v16.get(k) == v, f"config/v16.json 与 canonical 共享键漂移 {k}"
    p = v16["v16_params"]
    assert p["pos_short_min"] == 0.35 and p["pos_long_max"] == 0.65
    assert p["k_room_R"] == 1.5 and p["k_is"] == 0.8
    assert p["tier_a_mult"] == 1.0 and p["tier_b_mult"] == 0.75
    assert p["k_cap_hours"] == 48 and p["cap_mfe_min"] == 0.5
    assert p["stop_wait_tf"] == 12 and p["k_bm"] == 0.1 and p["k_star"] == 2
    assert p["fade_family"] == ["E2", "E3", "E8"]
    assert p["stop_family"] == ["E1", "E6", "E7"]
    # HarvestParams 默认值与 config 冻结先验逐项一致(双钉)
    hp = HarvestParams()
    assert (hp.pos_short_min, hp.pos_long_max, hp.k_room_R, hp.k_is,
            hp.k_star, hp.tier_a_mult, hp.tier_b_mult, hp.k_cap_hours,
            hp.cap_mfe_min) == (0.35, 0.65, 1.5, 0.8, 2, 1.0, 0.75, 48, 0.5)
    assert hp.fade_family == ("E2", "E3", "E8")
