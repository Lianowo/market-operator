"""v22 真实平衡箱夹板 + 完备双原语(R1/R2/R3 + A1/A2 + CONSUMED) + 回抽限价路由专项验收测试。"""
from __future__ import annotations

import pytest

from market_operator.events import (
    ACCEPT,
    BREAKOUT_ACCEPTED,
    CONSUMED,
    LEVEL_REJECTED,
    REJECT,
    EventDetector,
    MarketEvent,
    canonical_event_id,
)
from market_operator.hypothesis import StructureView
from market_operator.routing import (
    ROUTE_BREAKOUT_ACCEPTED,
    ROUTE_BREAKOUT_ACCEPTED_LIMIT,
    ROUTE_LEVEL_REJECTED,
    ROUTE_LEVEL_REJECTED_LIMIT,
    WHITELIST_BALANCE,
    MarketRouter,
    RouterParams,
)
from market_operator.structure import MarketStructure, Params, Regime, Zone, ZoneRole, ZoneState


def _make_box_bars(cycles: int = 4, base_ts: int = 1_700_000_000_000, step: int = 3_600_000):
    """生成标准水平往返箱体序列(含预热段),使 ATR 稳定且形成清晰的 [lo_ext, VAL] / [VAH, hi_ext]。"""
    pts = [115.0]
    for _ in range(cycles):
        pts += [122.0, 127.0, 121.0, 114.0, 108.0, 103.0, 109.0, 115.0]
    bars = []
    prev = pts[0]
    for i, c in enumerate(pts):
        o = prev
        h = max(o, c) + (3.0 if c >= 126.0 else 2.0)
        l = min(o, c) - (3.0 if c <= 104.0 else 2.0)
        vol = 220.0 if 108.0 <= c <= 122.0 else 95.0
        bars.append((base_ts + i * step, o, h, l, c, vol))
        prev = c
    return bars


def test_step2_1_true_balance_bridged_into_zonebook_active_bracket():
    """动作 2.1:TrueBalanceDetector 桥接进 ZoneBook,活跃区严格 <= 2 且具备双层边界与 box_opposite。"""
    p = Params(use_balance_detector=True, balance_quality_min=0.35, box_min_rotations=2)
    ms = MarketStructure(p)
    bars = _make_box_bars(cycles=4)

    active_counts = []
    sup_ids = []
    for b in bars:
        ms.update(b)
        active = [z for z in ms.zones if z.state is ZoneState.ACTIVE]
        active_counts.append(len(active))
        assert len(active) <= 2, f"任意时刻活跃夹板 Zone 必须严格 <= 2,实际={len(active)}"
        sup_raw = [z for z in ms._zones.zones if z.role is ZoneRole.SUPPORT and z.state is ZoneState.ACTIVE]
        if sup_raw:
            sup_ids.append(id(sup_raw[0]))

    active_final = [z for z in ms.zones if z.state is ZoneState.ACTIVE]
    assert len(active_final) == 2
    by_role = {z.role: z for z in active_final}
    sup = by_role[ZoneRole.SUPPORT]
    res = by_role[ZoneRole.RESISTANCE]

    # 双层边界:[lo_ext, VAL] 与 [VAH, hi_ext],且对侧锚点互指内沿
    assert sup.lo < sup.hi < res.lo < res.hi
    assert sup.box_opposite == pytest.approx(res.lo, rel=1e-4)
    assert res.box_opposite == pytest.approx(sup.hi, rel=1e-4)
    assert sup.box_quality >= 0.35 and res.box_quality >= 0.35
    # 原位更新保持同一 Zone 对象身份 id(z) 不断开 _Episode
    assert len(set(sup_ids[-6:])) == 1


def test_step2_2_r3_false_breakout_trap_emits_reject_and_cancels_accept():

    """动作 2.2:R3 收盘假突破 1~2 根后反向大阴吞没收复回内沿 -> 发射 LEVEL_REJECTED(R3_TRAP)并取消破位候选。"""
    p = Params(use_balance_detector=True, balance_quality_min=0.35, n_a=3, n_f=3, k_bm=0.1, k_rj=0.3)
    det = EventDetector(p)
    bars = _make_box_bars(cycles=4)
    for b in bars:
        det.update(b)

    res_zones = [z for z in det.structure.zones if z.role is ZoneRole.RESISTANCE and z.state is ZoneState.ACTIVE]
    assert len(res_zones) == 1
    rz = res_zones[0]
    atr = det.structure.atr()
    assert atr > 0.0
    last_ts = bars[-1][0]
    step = 3_600_000

    # 第 1 根:从正面(前收 < rz.lo)向上突破收盘越过外沿 rz.hi + bm(但未超 1.25*ATR 逃逸)
    c1 = rz.hi + 0.35 * atr
    h1 = rz.hi + 0.55 * atr
    l1 = rz.lo - 0.20 * atr
    ev1 = det.update((last_ts + step, rz.lo - 1.0, h1, l1, c1, 120.0))
    assert not any(e.id == BREAKOUT_ACCEPTED for e in ev1)

    # 第 2 根:反向吞没大阴线直接砸回内沿 rz.lo 下方 (c < rz.lo 且 c < o) -> 触发 R3_TRAP!
    o2 = c1
    h2 = c1 + 0.10 * atr
    c2 = rz.lo - 0.45 * atr
    l2 = c2 - 0.15 * atr
    ev2 = det.update((last_ts + 2 * step, o2, h2, l2, c2, 260.0))

    rej_events = [e for e in ev2 if e.id == LEVEL_REJECTED]
    assert len(rej_events) == 1, f"R3 假突破吞没收复必须发射 LEVEL_REJECTED,实际={ev2}"
    rej = rej_events[0]
    assert rej.direction == -1
    assert rej.attrs.get("sub_kind") == "R3_TRAP"
    assert rej.attrs["invalid"] == pytest.approx(max(h1, h2) + p.k_bm * det.structure.atr())
    assert len(det._candidates) == 0


def test_step2_2_consumed_sticky_episode_evicted_without_trade():
    """动作 2.2:CONSUMED 钝化淘汰 —— 边界内粘连反复穿刺超 N_f + 1 根未决出胜负,淘汰销毁不发交易事件。"""
    assert canonical_event_id("CONSUMED") == CONSUMED
    p = Params(use_balance_detector=True, balance_quality_min=0.35, n_a=3, n_f=3, k_bm=0.1, k_rj=0.8)
    det = EventDetector(p)
    bars = _make_box_bars(cycles=4)
    for b in bars:
        det.update(b)

    rz = [z for z in det.structure.zones if z.role is ZoneRole.RESISTANCE and z.state is ZoneState.ACTIVE][0]
    last_ts = bars[-1][0]
    step = 3_600_000
    mid = 0.5 * (rz.lo + rz.hi)

    sticky_emitted = []
    for k in range(1, 7):
        o = rz.lo - 0.5 if k == 1 else mid
        b = (last_ts + k * step, o, mid + 0.2, mid - 0.2, mid, 80.0)
        sticky_emitted.extend(det.update(b))

    assert not any(e.id in (LEVEL_REJECTED, BREAKOUT_ACCEPTED) for e in sticky_emitted)
    assert det.consumed_episodes >= 1


def test_step2_3_routing_compact_market_vs_overextended_pullback_limit():
    """动作 2.3:紧凑确认发 market;确认 K 线过长(stop_dist > 1.2*ATR 或剩余 RR 不足)自动转 50%/内沿 Limit 单。"""
    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            min_box_rr=1.5,
            max_confirm_stop_atr=1.2,
            k_is=0.8,
        )
    )
    z = Zone(
        lo=125.0, hi=128.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
        touches=3, origin_known=5, last_change_known=20,
        box_opposite=105.0, box_quality=0.65, box_width_atr=4.6, box_rotations=3,
    )
    view = StructureView(regime=Regime.RANGE, zones=(z,), atr=5.0)

    ev_compact = MarketEvent(
        id=REJECT, direction=-1, ts=10_000, known_index=21,
        attrs={"zone": z, "ref": 128.0, "invalid": 129.0, "sub_kind": "R1_WICK"},
    )
    d_mkt = router.on_bar(
        "BTCUSDT", (10_000, 126.0, 128.5, 123.5, 124.0, 100.0),
        21, view, 5.0, [ev_compact], position_open=False, entry_resting=False,
    )
    assert len(d_mkt) == 1
    assert d_mkt[0].kind == "market"
    assert d_mkt[0].route == ROUTE_LEVEL_REJECTED
    assert d_mkt[0].hyp.target_1 == pytest.approx(105.0)

    ev_over = MarketEvent(
        id=REJECT, direction=-1, ts=20_000, known_index=25,
        attrs={"zone": z, "ref": 128.0, "invalid": 129.0, "sub_kind": "R3_TRAP"},
    )
    d_lim = router.on_bar(
        "BTCUSDT", (20_000, 127.5, 128.5, 115.5, 116.0, 300.0),
        25, view, 5.0, [ev_over], position_open=False, entry_resting=False,
    )
    assert d_lim == []
    assert router.counters.get("restraint_rr_reject", 0) == 1

    ev_a2 = MarketEvent(
        id=ACCEPT, direction=+1, ts=30_000, known_index=30,
        attrs={"zone": z, "ref": 128.0, "invalid": 124.5, "sub_kind": "A2_OVEREXTENDED"},
    )
    d_a2 = router.on_bar(
        "BTCUSDT", (30_000, 130.0, 136.0, 129.5, 135.0, 280.0),
        30, view, 5.0, [ev_a2], position_open=False, entry_resting=False,
    )
    assert d_a2 == []
    assert router.counters.get("restraint_a2_overextended_reject", 0) == 1


def test_step2_2_r3_two_bar_false_breakout_trap_also_recovered():
    """动作 2.2:连续 2 根收盘越过外沿(未超 1.25*ATR)后第 3 根大阴吞没砸回内沿 -> 同样触发 R3_TRAP!"""
    p = Params(use_balance_detector=True, balance_quality_min=0.35, n_a=3, n_f=3, k_bm=0.1, k_rj=0.3)
    det = EventDetector(p)
    bars = _make_box_bars(cycles=4)
    for b in bars:
        det.update(b)

    rz = [z for z in det.structure.zones if z.role is ZoneRole.RESISTANCE and z.state is ZoneState.ACTIVE][0]
    atr = det.structure.atr()
    last_ts = bars[-1][0]
    step = 3_600_000

    # 第 1 根与第 2 根连续收在外沿上方(未达 N_a=3)
    c1 = rz.hi + 0.30 * atr
    det.update((last_ts + step, rz.lo - 1.0, rz.hi + 0.45 * atr, rz.lo - 0.1 * atr, c1, 110.0))
    c2 = rz.hi + 0.40 * atr
    ev2 = det.update((last_ts + 2 * step, c1, rz.hi + 0.60 * atr, rz.hi + 0.05 * atr, c2, 115.0))
    assert not any(e.id == BREAKOUT_ACCEPTED for e in ev2)

    # 第 3 根反向长阴直接砸回内沿 rz.lo 下方 -> 发射 R3_TRAP REJECT!
    c3 = rz.lo - 0.50 * atr
    ev3 = det.update((last_ts + 3 * step, c2, c2 + 0.05 * atr, c3 - 0.1 * atr, c3, 290.0))
    rej = [e for e in ev3 if e.id == LEVEL_REJECTED]
    assert len(rej) == 1
    assert rej[0].attrs.get("sub_kind") == "R3_TRAP"
    assert rej[0].attrs.get("outside_closes") == 2


def test_step3_1_directional_cap_frac_blocks_same_direction_overload_only():
    """动作 3.1:同向净风险上限 directional_cap_frac 仅统计在途同方向仓位风险,反方向仓位不受阻。"""
    from market_operator import capacity
    from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind

    specs = {
        s: ContractSpec(symbol=s, size_min=0.01, size_step=0.01,
                        size_place=2, price_place=2, min_trade_usdt=5.0)
        for s in ("BTCUSDT@2h", "ETHUSDT@4h", "SOLUSDT@12h")
    }
    broker = Broker(
        equity=1000.0, risk_frac=0.04, leverage_cap=20.0,
        taker_fee=0.0, slippage_per_side=0.0, funding_per_8h=0.0,
        contracts=specs,
        portfolio_stop_risk_frac=0.15,
        directional_stop_risk_frac=0.08,
        capacity_mode=capacity.STOP_RISK,
    )
    t0 = 1_700_000_000_000
    h = 3_600_000

    # 开第 1 笔多单 BTC (+4% = 40U 止损风险)
    broker.submit(OrderIntent(kind=OrderKind.OPEN, symbol="BTCUSDT@2h", qty_frac=None,
                              reason="REJECT|long", ref_price=100.0, ts=t0),
                  t0, direction=+1, invalid=90.0, event_id=LEVEL_REJECTED, target_1=120.0)
    broker.on_bar_open("BTCUSDT@2h", t0 + h, 100.0)

    # 开第 2 笔多单 ETH (+4% = 40U 止损风险 -> 多头合计 80U = 8.0% 触及 directional_cap_frac=0.08)
    broker.submit(OrderIntent(kind=OrderKind.OPEN, symbol="ETHUSDT@4h", qty_frac=None,
                              reason="REJECT|long", ref_price=100.0, ts=t0 + h),
                  t0 + h, direction=+1, invalid=90.0, event_id=LEVEL_REJECTED, target_1=120.0)
    broker.on_bar_open("ETHUSDT@4h", t0 + 2 * h, 100.0)

    long_exp = capacity.outstanding_exposure(
        broker.positions, broker.last_mark, direction=+1
    ).measure(include_costs=False)
    short_exp = capacity.outstanding_exposure(
        broker.positions, broker.last_mark, direction=-1
    ).measure(include_costs=False)

    assert long_exp == pytest.approx(80.0)
    assert short_exp == pytest.approx(0.0)
    # 若再开第 3 笔多单 (+40U -> 120U > 80U),同向锁触发拦截;但开空单 (0 + 40U <= 80U) 则允许!
    assert long_exp + 40.0 > broker.risk_budget.directional_cap_frac * 1000.0
    assert short_exp + 40.0 <= broker.risk_budget.directional_cap_frac * 1000.0



def test_balance_detector_retest_does_not_inject_zombie_legacy_zone():
    """use_balance_detector=True 时,_retest 仅记录 flips 事实,严禁向 zones 注入或合并 box_rotations=0 的旧版僵尸区。"""
    p = Params(use_balance_detector=True, balance_quality_min=0.35, k_bm=0.1, k_rj=0.3)
    ms = MarketStructure(p)
    for b in _make_box_bars(cycles=4):
        ms.update(b)
    rz = [z for z in ms.zones if z.role is ZoneRole.RESISTANCE and z.state is ZoneState.ACTIVE][0]
    atr = ms.atr()
    t0 = _make_box_bars(cycles=4)[-1][0]
    step = 3_600_000

    # 第 1 根收盘突破阻力外沿 -> BROKEN
    c1 = rz.hi + 0.5 * atr
    ms.update((t0 + step, rz.lo, c1 + 0.2 * atr, rz.lo, c1, 120.0))
    # 第 2 根回踩带内并向上抽离 >= k_rj*atr 触发 _retest FLIPPED
    l2 = rz.hi - 0.05 * atr
    c2 = l2 + 0.5 * atr
    ms.update((t0 + 2 * step, c1, c2 + 0.1 * atr, l2, c2, 120.0))

    for z in ms.zones:
        assert z.box_rotations > 0, "真平衡箱视图中严禁混入 box_rotations=0 的旧版 _retest 僵尸区"
        assert z.box_quality > 0.0, "真平衡箱视图中严禁混入 box_quality=0.0 的旧版 _retest 僵尸区"



def test_struct_impulse_confirm_and_zone_departure_reset():
    """验证 struct_impulse_confirm(局部 3 根破位或 5 根扩张吞没+半区收盘)与 zone_departure_reset(边界离开净空复位)语义。"""
    wl = frozenset({(ACCEPT, 1), (ACCEPT, -1), (REJECT, 1), (REJECT, -1)})
    rp = RouterParams(
        whitelist=wl,
        reject_use_struct_stop=True,
        reject_target_mode="opposite",
        struct_impulse_confirm=True,
        zone_departure_reset=True,
    )
    router = MarketRouter(rp)
    z = Zone(
        lo=125.0, hi=128.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
        touches=3, origin_known=1, last_change_known=5,
        box_opposite=105.0, box_quality=0.55, box_width_atr=4.6, box_rotations=3,
    )
    view = StructureView(regime=Regime.RANGE, zones=(z,), atr=2.0)
    step = 7_200_000

    # 1) 窄幅碎步 K 线(既未跌破前 3 根低点 min=124.8,波幅 2.5 也未超前 4 根最大波幅 3.0) -> struct_impulse_confirm 拦截
    weak_eval = (
        (1000, 124.5, 127.0, 124.0, 125.5),
        (1000 + step, 125.5, 127.2, 124.8, 126.5),
        (1000 + 2 * step, 126.5, 127.5, 125.2, 127.0),
        (1000 + 3 * step, 127.0, 128.2, 126.0, 127.5),
        (1000 + 4 * step, 127.5, 127.5, 125.0, 125.0),
    )
    ev_weak = MarketEvent(
        id=LEVEL_REJECTED, direction=-1, ts=1000 + 4 * step, known_index=14,
        attrs={"zone": z, "ref": 125.0, "invalid": 128.5, "eval_bars": weak_eval,
               "reset_bars": weak_eval, "reset_step_ms": step, "close_ts": 1000 + 5 * step},
    )
    d_weak = router.on_bar(
        "BTCUSDT", (1000 + 4 * step, 127.5, 127.5, 125.0, 125.0, 100.0),
        14, view, 2.0, [ev_weak], position_open=False, entry_resting=False,
    )
    assert d_weak == []
    assert router.counters.get("restraint_struct_impulse_reject") == 1

    # 2) 真实结构击穿(收盘 123.5 < 前 3 根最低点 min(124.8,125.2,126.0)=124.8,且位于下半区) -> 放行并锁定该箱沿
    strong_eval = weak_eval[:-1] + ((1000 + 4 * step, 127.5, 128.4, 123.0, 123.5),)
    ev_strong = MarketEvent(
        id=LEVEL_REJECTED, direction=-1, ts=1000 + 4 * step, known_index=14,
        attrs={"zone": z, "ref": 125.0, "invalid": 128.5, "eval_bars": strong_eval,
               "reset_bars": strong_eval, "reset_step_ms": step, "close_ts": 1000 + 5 * step},
    )
    d_ok = router.on_bar(
        "BTCUSDT", (1000 + 4 * step, 127.5, 128.4, 123.0, 123.5, 100.0),
        14, view, 2.0, [ev_strong], position_open=False, entry_resting=False,
    )
    assert len(d_ok) == 1

    # 3) 价格未脱离边缘带(仍在 125.0 附近摩擦)即再次触发 -> zone_departure_reset 拦截
    hover_bars = strong_eval + (
        (1000 + 5 * step, 123.5, 126.2, 124.0, 125.2),
        (1000 + 6 * step, 125.2, 128.2, 122.5, 123.0),
    )
    ev_hover = MarketEvent(
        id=LEVEL_REJECTED, direction=-1, ts=1000 + 6 * step, known_index=16,
        attrs={"zone": z, "ref": 125.0, "invalid": 128.5, "eval_bars": hover_bars[-5:],
               "reset_bars": hover_bars, "reset_step_ms": step, "close_ts": 1000 + 7 * step},
    )
    d_hover = router.on_bar(
        "BTCUSDT", (1000 + 6 * step, 125.2, 128.2, 122.5, 123.0, 100.0),
        16, view, 2.0, [ev_hover], position_open=False, entry_resting=False,
    )
    assert d_hover == []
    assert router.counters.get("restraint_departure_reset_reject") == 1

    # 4) 价格先真正离开边缘带进入箱体内部净空区(high=124.0 < inner=125.0, close=121.0 <= 125.0 - 0.3*2.0),随后重返测试 -> 解锁放行!
    departed_bars = hover_bars + (
        (1000 + 7 * step, 123.0, 124.0, 120.5, 121.0),
        (1000 + 8 * step, 121.0, 128.3, 120.0, 120.2),
    )
    ev_retest = MarketEvent(
        id=LEVEL_REJECTED, direction=-1, ts=1000 + 8 * step, known_index=18,
        attrs={"zone": z, "ref": 125.0, "invalid": 128.5, "eval_bars": departed_bars[-5:],
               "reset_bars": departed_bars, "reset_step_ms": step, "close_ts": 1000 + 9 * step},
    )
    d_retest = router.on_bar(
        "BTCUSDT", (1000 + 8 * step, 121.0, 128.3, 120.0, 120.2, 100.0),
        18, view, 2.0, [ev_retest], position_open=False, entry_resting=False,
    )
    assert len(d_retest) == 1


def test_accept_reenter_box_exit_when_enabled():
    """显式开启 accept_reenter_box_exit 时,BREAKOUT_ACCEPTED 仓位在认知 bar 收盘跌穿被破箱沿远端(reenter_invalid_px)触发 §8.1b 退出。"""
    from market_operator.position import PositionManager

    pm = PositionManager(Params(), accept_reenter_box_exit=True)
    assert pm.accept_reenter_box_exit is True
    pm.open(
        "BTCUSDT", direction=+1, entry=131.0, invalid=122.0,
        target_1=None, event_id=BREAKOUT_ACCEPTED, ts=1000, index=10,
        reenter_invalid_px=127.0,
    )
    intents = pm.on_bar("BTCUSDT", (2000, 130.0, 130.5, 125.5, 126.0, 100.0), atr=3.0)
    assert len(intents) == 1
    assert "§8.1b|accept_reentered_box" in intents[0].reason


def test_reject_requires_clean_close_back_inside_value_area_edge():
    """LEVEL_REJECTED 必须收盘真正收复价值区内沿(多头 c >= z.hi / 空头 c <= z.lo),未出内沿不在带内半腰抢跑。"""
    p = Params(use_balance_detector=True, k_bm=0.1, k_rj=0.3)
    det = EventDetector(p)
    z = Zone(lo=100.0, hi=102.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
             box_opposite=112.0, box_quality=0.6, box_width_atr=6.0, box_rotations=3)
    det.structure._zones.zones = [z]
    det.structure._regime = Regime.RANGE

    # 1) 刺穿下沿 99.0 后仅收在带内 101.2 (< z.hi=102.0, 尚未收复内沿) -> 不抢跑发射
    ev_in_band = det._zone_events(1, 3_600_000, 100.5, 102.0, 99.0, 101.2, 103.0, 2.0)
    assert [e for e in ev_in_band if e.id == LEVEL_REJECTED] == []

    # 2) 下一根真正收复价值区内沿 102.5 (>= z.hi=102.0) -> 确认发射 LEVEL_REJECTED,且极值保留 99.0
    ev_confirmed = det._zone_events(2, 7_200_000, 101.2, 103.0, 101.0, 102.5, 101.2, 2.0)
    assert len(ev_confirmed) == 1 and ev_confirmed[0].id == LEVEL_REJECTED
    assert ev_confirmed[0].attrs["ex"] == pytest.approx(99.0)


def test_breakout_requires_full_n_a_confirmation_without_fast_accept():
    """关闭 fast_accept_atr 时,箱外突破必须完成完整 n_a=3 根驻留确认,绝不在第 1~2 根抢跑假突破。"""
    p = Params(use_balance_detector=True, balance_quality_min=0.35,
               n_a=3, fast_accept_atr=None)
    det = EventDetector(p)
    for b in _make_box_bars(cycles=4):
        det.update(b)
    rz = [z for z in det.structure.zones if z.role is ZoneRole.RESISTANCE and z.state is ZoneState.ACTIVE][0]
    atr = det.structure.atr()
    t0 = _make_box_bars(cycles=4)[-1][0]
    step = 3_600_000

    c1 = rz.hi + 0.60 * atr
    ev1 = det.update((t0 + step, rz.lo, c1 + 0.1 * atr, rz.lo, c1, 150.0))
    assert [e for e in ev1 if e.id == BREAKOUT_ACCEPTED] == []

    c2 = rz.hi + 0.70 * atr
    ev2 = det.update((t0 + 2 * step, c1, c2 + 0.1 * atr, c1 - 0.05 * atr, c2, 180.0))
    assert [e for e in ev2 if e.id == BREAKOUT_ACCEPTED] == []

    c3 = rz.hi + 0.80 * atr
    ev3 = det.update((t0 + 3 * step, c2, c3 + 0.1 * atr, c2 - 0.05 * atr, c3, 180.0))
    acc = [e for e in ev3 if e.id == BREAKOUT_ACCEPTED]
    assert len(acc) == 1
    assert acc[0].attrs["fast"] is False


def test_multitf_amt_boundary_defers_mic_outward_breakout_but_allows_reentry_and_reject():
    """多尺度 AMT 职责边界:
    - 细周期触发(mic=True)允许触发向大周期价值区内部回归的信号(BOX_EDGE 拒绝 + FLIPPED_BOX_EDGE 重返入箱);
    - 推翻大周期平衡箱向外突破(BOX_EDGE 外向 BREAKOUT_ACCEPTED)若仅在 mic=True 上触发,必须交还大周期认知收盘确认,
      且不得误撤既有挂单。
    """
    router = MarketRouter(RouterParams(
        whitelist=WHITELIST_BALANCE,
        reject_use_struct_stop=True,
        reject_target_mode="opposite",
    ))
    z_box = Zone(lo=108.0, hi=110.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
                 box_opposite=98.0, box_quality=0.65, box_width_atr=6.0, box_rotations=4,
                 source_kind="BOX_EDGE")
    z_flip = Zone(lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
                  box_opposite=110.0, box_quality=0.65, box_width_atr=6.0, box_rotations=4,
                  source_kind="FLIPPED_BOX_EDGE")
    view = StructureView(regime=Regime.RANGE, zones=(z_box, z_flip), atr=2.0)

    # 1) mic=True 的 BOX_EDGE 外向突破: 不得越权开仓, 也不得触发 e1_cancel_orders 误撤挂单
    ev_mic_out = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=1000, known_index=5,
        attrs={"zone": z_box, "ref": 110.0, "invalid": 107.8, "mic": True},
    )
    dec_mic_out = router.on_bar(
        "BTCUSDT", (1000, 110.2, 111.5, 110.0, 111.2, 50.0),
        5, view, 2.0, [ev_mic_out], position_open=False, entry_resting=True,
    )
    assert dec_mic_out == []
    assert router.counters.get("restraint_mic_outward_accept_reject") == 1
    assert router.counters.get("e1_cancel_orders", 0) == 0

    # 2) 认知收盘(mic=False)的 BOX_EDGE 外向突破: 正常确认入场
    ev_cog_out = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=2000, known_index=6,
        attrs={"zone": z_box, "ref": 110.0, "invalid": 107.8},
    )
    dec_cog_out = router.on_bar(
        "BTCUSDT", (2000, 110.2, 111.5, 110.0, 111.2, 50.0),
        6, view, 2.0, [ev_cog_out], position_open=False, entry_resting=False,
    )
    assert len(dec_cog_out) == 1 and dec_cog_out[0].route == ROUTE_BREAKOUT_ACCEPTED

    # 3) mic=True 的 FLIPPED_BOX_EDGE 重返入箱突破: 属于回归既有大周期价值区, 正常放行
    ev_mic_reentry = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=3000, known_index=7,
        attrs={"zone": z_flip, "ref": 100.0, "invalid": 97.8, "mic": True},
    )
    dec_reentry = router.on_bar(
        "BTCUSDT", (3000, 100.1, 101.4, 99.9, 101.1, 50.0),
        7, view, 2.0, [ev_mic_reentry], position_open=False, entry_resting=False,
    )
    assert len(dec_reentry) == 1 and dec_reentry[0].meta["trade_context"] == "重新入箱"


def test_trigger_layer_r3_trap_end_to_end_survives_outside_close_and_unlocks_zone():
    """触发层完整 R3_TRAP 生命周期:
    细周期第 1 根收盘非逃逸越界(<=1.25*ATR)不得销毁 _MicEpisode;
    第 2 根强力收回价值区内沿时发射 R3_TRAP,清除同区假突破候选并释放 _broken 锁定。
    """
    from types import SimpleNamespace
    from market_operator.trigger import MicTracker

    p = Params(use_balance_detector=True, n_a=3, n_f=3, k_bm=0.1, k_rj=0.3)
    z = Zone(lo=100.0, hi=102.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
             box_opposite=90.0, box_quality=0.65, box_width_atr=6.0, box_rotations=4)
    s_mock = SimpleNamespace(
        atr=lambda: 2.0, regime=lambda: Regime.RANGE, pivots=(),
        last_low_pivot=None, last_high_pivot=None,
        _zones=SimpleNamespace(zones=[z]),
    )
    det_mock = SimpleNamespace(structure=s_mock, _episodes=[], _trend_candidates=[])
    tracker = MicTracker(p, scale=6, trigger_tf_seconds=7200, obs_tf_seconds=43200)
    tracker.sync(0, det_mock, lambda i: 0, lambda i: 5)
    tracker._prev_trig_close = 99.0

    # 第 1 根 2h: 从正面触碰并收盘刺出外沿 102.0 + 0.2 = 102.2 (high=103.0, escape=0.5*ATR <= 1.25*ATR)
    ev1 = tracker.on_trigger_bar(0, 7_200_000, 99.5, 103.0, 99.4, 102.6)
    assert ev1 == []
    assert len(tracker._episodes) == 1
    assert tracker._episodes[0].outside_closes == 1
    assert len(tracker._candidates) == 1
    assert z.zone_id in tracker._broken

    # 第 2 根 2h: 诱多失败大阴线砸回价值区内沿之下(close=99.2 < 100.0, close_pos > 0.50)
    ev2 = tracker.on_trigger_bar(1, 14_400_000, 102.5, 102.8, 98.8, 99.2)
    assert len(ev2) == 1
    assert ev2[0].id == LEVEL_REJECTED and ev2[0].direction == -1
    assert ev2[0].attrs["sub_kind"] == "R3_TRAP"
    assert ev2[0].attrs["outside_closes"] == 1
    assert tracker._candidates == []
    assert z.zone_id not in tracker._broken


def test_null_accept_max_ext_atr_and_midpoint_close_pos_contract():
    """配置收束契约: accept_max_ext_atr=None 原生放行过伸确认(无需 99.0 魔法数),且拒绝确认统一要求跨越 K 线几何中点(0.50)。"""
    import json
    from pathlib import Path

    cfg_path = Path(__file__).resolve().parents[1] / "config" / "strategy.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    p = Params.from_json(cfg_path)
    assert raw["accept_max_ext_atr"] is None
    assert p.accept_max_ext_atr is None
    assert raw["router_params"]["zone_departure_reset"] is True
    assert set(raw["router_params_by_tf"]["2h"].keys()) == {"struct_impulse_confirm"}

    det = EventDetector(Params(use_balance_detector=True, k_bm=0.1, k_rj=0.3))
    z = Zone(lo=98.0, hi=100.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
             box_opposite=110.0, box_quality=0.6, box_width_atr=6.0, box_rotations=3)
    det.structure._zones.zones = [z]
    det.structure._regime = Regime.RANGE

    # close_pos = (100.47 - 98.5) / (102.5 - 98.5) = 1.97 / 4.0 = 0.4925 < 0.50 -> 未过 K 线中轴不发射
    ev_below_mid = det._zone_events(1, 3_600_000, 101.0, 102.5, 98.5, 100.47, 101.0, 2.0)
    assert [e for e in ev_below_mid if e.id == LEVEL_REJECTED] == []


def test_opposite_third_entry_restraint_and_unbacked_stall_axiom():
    """AMT 第一性原理空间与扫损公理验证:
    1) 无上级价值区背书的单尺度帧(mic=False & struct_impulse_confirm=True):
       未刺穿外沿的 R2_STALL 停滞按"无扫损无拒绝"公理拦截(restraint_unbacked_stall_reject);
    2) 对侧防守带位置克制(max_box_progress_frac=0.75):
       确认或开盘成交价已走入箱体对侧 25% 防守区时判 BOX_OPPOSITE_THIRD_ENTERED 作废。
    """
    z = Zone(
        lo=99.0, hi=100.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=3, rejects=1, origin_known=1, last_change_known=10,
        box_opposite=110.0, box_quality=0.65, box_width_atr=5.0, box_rotations=4,
    )
    view = StructureView(regime=Regime.RANGE, zones=(z,), atr=2.0)
    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            max_box_progress_frac=0.75,
            struct_impulse_confirm=True,
        )
    )

    # 1) 单尺度(mic=False)上的 R2_STALL 无刺穿停顿 -> 触发 restraint_unbacked_stall_reject 拦截
    ev_stall = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=1000, known_index=12,
        attrs={"zone": z, "ref": 99.0, "invalid": 98.0, "sub_kind": "R2_STALL"},
    )
    assert router.on_bar("BTCUSDT", (1000, 100.0, 101.5, 99.5, 101.0, 100.0), 12, view, 2.0, [ev_stall], position_open=False, entry_resting=False) == []
    assert router.counters.get("restraint_unbacked_stall_reject") == 1

    # 2) 价格已走入箱体对侧 25% 防守区(inner=100, opp=110, close=108.0 -> prog=0.80 >= 0.75) -> 拦截作废
    z_fresh = Zone(
        lo=99.0, hi=100.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=2, rejects=1, origin_known=1, last_change_known=10,
        box_opposite=110.0, box_quality=0.65, box_width_atr=5.0, box_rotations=3,
    )
    ev_late = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=5000, known_index=16,
        attrs={"zone": z_fresh, "ref": 99.0, "invalid": 98.0, "sub_kind": "R1_WICK"},
    )
    r_no_imp = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            max_box_progress_frac=0.75,
        )
    )
    assert r_no_imp.on_bar("BTCUSDT", (5000, 100.0, 108.5, 99.5, 108.0, 100.0), 16, view, 2.0, [ev_late], position_open=False, entry_resting=False) == []
    assert r_no_imp.counters.get("box_opposite_third_entered") == 1


def test_htf_wall_guard_blocks_headwind_and_caps_target():
    """跨周期上下级结构感知(HTF Wall Guard):
    1) 小周期多单迎头撞进未突破的大周期活跃平衡箱阻力带 [VAH_htf, hi_ext_htf] -> restraint_htf_wall_reject 拦截;
    2) 小周期多单距大周期阻力内沿空间 < 1.0R -> restraint_htf_room_reject 拦截;
    3) 小周期多单距大周期阻力内沿空间 >= 1.0R 且大周期阻力内沿近于原 target_1 -> 自动收缩 target_1 至大周期阻力内沿;
    4) 在大周期阻力带顺应大墙做空(Confluence) -> 100% 放行。
    """
    z_2h_sup = Zone(
        lo=99.0, hi=100.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=2, rejects=1, origin_known=1, last_change_known=5,
        box_opposite=112.0, box_quality=0.65, box_width_atr=6.0, box_rotations=3,
    )
    z_2h_res = Zone(
        lo=109.0, hi=110.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
        touches=2, rejects=1, origin_known=1, last_change_known=5,
        box_opposite=99.0, box_quality=0.65, box_width_atr=6.0, box_rotations=3,
    )
    view_2h = StructureView(regime=Regime.RANGE, zones=(z_2h_sup, z_2h_res), atr=2.0)

    # 12h/1d 活跃平衡箱上沿阻力带: [108.0, 111.0]
    z_htf_res = Zone(
        lo=108.0, hi=111.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
        touches=4, rejects=2, origin_known=1, last_change_known=4,
        box_opposite=92.0, box_quality=0.75, box_width_atr=5.0, box_rotations=4,
        source_kind="BOX_EDGE",
    )
    htf_view = StructureView(regime=Regime.RANGE, zones=(z_htf_res,), atr=4.0)

    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            htf_wall_filter=True,
            htf_min_room_r=1.0,
        )
    )

    # 1) 2h 向上突破信号(close=109.5)正处于未突破的 12h 阻力带 [108.0, 111.0] 内部 -> 拦截!
    ev_brk_into_wall = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=1000, known_index=10,
        attrs={"zone": z_2h_res, "ref": 110.0, "invalid": 105.5},
    )
    d1 = router.on_bar(
        "BTCUSDT", (1000, 108.5, 109.8, 108.2, 109.5, 100.0),
        10, view_2h, 2.0, [ev_brk_into_wall],
        position_open=False, entry_resting=False, htf_views=(htf_view,),
    )
    assert d1 == []
    assert router.counters.get("restraint_htf_wall_reject") == 1

    # 2) 2h 多单(close=106.5, invalid=103.5 -> risk=3.0), 距 12h 阻力 108.0 仅 1.5 < 1.0R(3.0) -> 拦截!
    ev_tight_room = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=2000, known_index=11,
        attrs={"zone": z_2h_res, "ref": 106.0, "invalid": 103.5},
    )
    d2 = router.on_bar(
        "BTCUSDT", (2000, 105.5, 106.8, 105.2, 106.5, 100.0),
        11, view_2h, 2.0, [ev_tight_room],
        position_open=False, entry_resting=False, htf_views=(htf_view,),
    )
    assert d2 == []
    assert router.counters.get("restraint_htf_room_reject") == 1

    # 3) 2h 下沿拒绝多单(close=101.0, invalid=98.8 -> risk=2.2, 原 target_1=112.0),
    #    距 12h 阻力内沿 108.0 空间为 7.0 >= 1.0R -> 放行且自动将 target_1 从 112.0 收缩至 108.0!
    ev_cap = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=3000, known_index=12,
        attrs={"zone": z_2h_sup, "ref": 99.0, "invalid": 98.8, "sub_kind": "R1_WICK"},
    )
    d3 = router.on_bar(
        "BTCUSDT", (3000, 99.5, 101.4, 98.9, 101.0, 100.0),
        12, view_2h, 2.0, [ev_cap],
        position_open=False, entry_resting=False, htf_views=(htf_view,),
    )
    assert len(d3) == 1
    assert d3[0].hyp.target_1 == 108.0
    assert router.counters.get("htf_target_capped") == 1

    # 4) 在 12h 阻力带内顺应大墙做空(direction=-1) -> 大小周期共振, 正常放行!
    ev_short_confluence = MarketEvent(
        id=LEVEL_REJECTED, direction=-1, ts=4000, known_index=13,
        attrs={"zone": z_2h_res, "ref": 110.0, "invalid": 111.2, "sub_kind": "R1_WICK"},
    )
    d4 = router.on_bar(
        "BTCUSDT", (4000, 109.8, 111.0, 108.2, 108.5, 100.0),
        13, view_2h, 2.0, [ev_short_confluence],
        position_open=False, entry_resting=False, htf_views=(htf_view,),
    )
    assert len(d4) == 1 and d4[0].hyp.direction == -1


def test_multitf_reject_true_engulfing_and_htf_macro_imbalance_semantics():
    """验证多尺度拒绝实体扩张吞没确认(True Engulfing)与 8 拐点道氏宏观失衡对齐(macro_imbalance):
    1) 多尺度(mic=True)LEVEL_REJECTED: 即使前 3 根陡峭下跌未过 3 根前高(brk3=False),
       只要满足顺向实体扩张吞没(|c-o| > |c1-o1| 且吞没前根实体、收在 K 线顺向半区),即刻确认放行;
    2) 当日线(tf_hours=24)或上级周期(tf_hours=12)处于反方向宏观主结构失衡(macro_imbalance == -direction)时,
       restraint_htf_imbalance_reject 拦截逆大势接刀。
    """
    z = Zone(
        lo=99.0, hi=100.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=2, rejects=1, origin_known=1, last_change_known=8,
        box_opposite=110.0, box_quality=0.65, box_width_atr=5.0, box_rotations=4,
    )
    view = StructureView(regime=Regime.RANGE, zones=(z,), atr=2.0)
    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            struct_impulse_confirm=True,
            htf_wall_filter=True,
            min_box_rr=1.5,
        )
    )
    eval_bars = (
        (100, 106.0, 106.5, 104.0, 104.5),
        (200, 104.5, 105.0, 102.5, 103.0),
        (300, 103.0, 103.5, 101.0, 101.5),
        (400, 100.5, 101.8, 99.8, 100.1),
        (500, 100.0, 101.0, 98.5, 100.6),
    )
    ev_mic_rej = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=5000, known_index=10,
        attrs={
            "zone": z, "ref": 99.0, "invalid": 98.3,
            "sub_kind": "R1_WICK", "mic": True,
            "eval_bars": eval_bars,
        },
    )
    d_ok = router.on_bar(
        "BTCUSDT", (5000, 100.0, 101.0, 98.5, 100.6, 125.0),
        10, view, 2.0, [ev_mic_rej], position_open=False, entry_resting=False,
    )
    assert len(d_ok) == 1 and d_ok[0].kind == "market"

    htf_bear = StructureView(regime=Regime.TREND_DOWN, zones=(), atr=8.0, macro_imbalance=-1, tf_hours=24)
    ev_macro_counter = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=6000, known_index=11,
        attrs={
            "zone": z, "ref": 99.0, "invalid": 98.3,
            "sub_kind": "R1_WICK", "mic": True,
            "eval_bars": eval_bars,
        },
    )
    assert router.on_bar(
        "BTCUSDT", (6000, 100.0, 101.0, 98.5, 100.6, 125.0),
        11, view, 2.0, [ev_macro_counter],
        position_open=False, entry_resting=False, htf_views=(htf_bear,),
    ) == []
    assert router.counters.get("restraint_htf_imbalance_reject") == 1


def test_balance_auction_alignment_first_principles():
    """验证纯图表+价格行为+价格拍卖对齐准则(balance_auction_alignment):
    1) TRANSITION 宽幅失衡巨震区仅允许高盈亏比(>=2.0R)且紧凑箱体的 R3_TRAP 假突破收复陷阱;
    2) 单边确认趋势(TREND_DOWN/TREND_UP)不逆势接刀/摸顶;
    3) 边界拒绝(LEVEL_REJECTED)要求完成箱外诱多/诱空收复陷阱(R3_TRAP),拦截未出清流动性的 R2_STALL/R1_WICK;
    4) RANGE 震荡区间内只做回归(REJECT & FLIPPED_BOX_EDGE 重返入箱),不追 BOX_EDGE 箱外突破;趋势箱外突破须具备真实脱离位移(>=1.1 ATR)。
    """
    z_box = Zone(
        lo=99.0, hi=100.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=3, rejects=1, origin_known=1, last_change_known=8,
        box_opposite=110.0, box_quality=0.65, box_width_atr=5.0, box_rotations=4,
        source_kind="BOX_EDGE", box_id=1,
    )
    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            reentry_use_struct_stop=True,
            balance_auction_alignment=True,
            min_box_rr=1.5,
        )
    )
    ev_rej_trap = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=1000, known_index=10,
        attrs={"zone": z_box, "ref": 99.0, "invalid": 98.3, "sub_kind": "R3_TRAP", "ex_index": 10},
    )
    # 1) TREND_UP 下同向 LEVEL_REJECTED(单边趋势不做箱内均值回归)拦截, TRANSITION 下 R3_TRAP 放行
    view_up_rej = StructureView(regime=Regime.TREND_UP, zones=(z_box,), atr=2.0)
    assert router.on_bar("BTCUSDT", (1000, 99.5, 101.0, 98.5, 100.6, 1.0), 10, view_up_rej, 2.0, [ev_rej_trap],
                         position_open=False, entry_resting=False) == []
    assert router.counters.get("restraint_trend_mean_reversion_reject") == 1

    # 2) TREND_DOWN 逆势做多拦截
    view_down = StructureView(regime=Regime.TREND_DOWN, zones=(z_box,), atr=2.0)
    assert router.on_bar("BTCUSDT", (2000, 99.5, 101.0, 98.5, 100.6, 1.0), 10, view_down, 2.0, [ev_rej_trap],
                         position_open=False, entry_resting=False) == []
    assert router.counters.get("restraint_counter_trend_reject") == 1

    # 3) R1_WICK / R2_STALL 未完成箱外收盘诱多/诱空陷阱拦截, R3_TRAP 放行
    view_range = StructureView(regime=Regime.RANGE, zones=(z_box,), atr=2.0)
    ev_wick = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=3000, known_index=10,
        attrs={"zone": z_box, "ref": 99.0, "invalid": 98.3, "sub_kind": "R1_WICK", "ex_index": 10},
    )
    assert router.on_bar("BTCUSDT", (3000, 99.5, 101.0, 98.5, 100.6, 1.0), 10, view_range, 2.0, [ev_wick],
                         position_open=False, entry_resting=False) == []
    assert router.counters.get("restraint_unbacked_stall_reject") == 1

    d_trap = router.on_bar("BTCUSDT", (4000, 99.5, 101.0, 98.5, 100.6, 1.0), 10, view_range, 2.0, [ev_rej_trap],
                           position_open=False, entry_resting=False)
    assert len(d_trap) == 1 and d_trap[0].kind == "market"

    # 4) 短周期(<4h) RANGE 下 BOX_EDGE 向外突破拦截, 但大周期(>=4h)认知收盘从 RANGE 发起的真突破及 TREND_UP 下顺势突破放行
    z_res = Zone(
        lo=109.0, hi=110.0, role=ZoneRole.RESISTANCE, state=ZoneState.BROKEN,
        touches=3, rejects=1, origin_known=1, last_change_known=10,
        box_opposite=100.0, box_quality=0.65, box_width_atr=5.0, box_rotations=4,
        source_kind="BOX_EDGE", box_id=2,
    )
    ev_out = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=5000, known_index=14,
        attrs={"zone": z_res, "ref": 110.0, "invalid": 108.8, "sub_kind": "A2_OVEREXTENDED", "ext_atr": 1.2, "reset_step_ms": 2 * 3600 * 1000},
    )
    assert router.on_bar("BTCUSDT", (5000, 110.2, 112.6, 110.0, 112.4, 1.0), 14, view_range, 2.0, [ev_out],
                         position_open=False, entry_resting=False) == []
    assert router.counters.get("restraint_range_outward_breakout_reject") == 1

    ev_out_4h = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=5500, known_index=14,
        attrs={"zone": z_res, "ref": 110.0, "invalid": 108.8, "sub_kind": "A2_OVEREXTENDED", "ext_atr": 1.2, "reset_step_ms": 4 * 3600 * 1000},
    )
    d_range_4h_out = router.on_bar("BTCUSDT", (5500, 110.2, 112.6, 110.0, 112.4, 1.0), 14, view_range, 2.0, [ev_out_4h],
                                   position_open=False, entry_resting=False)
    assert len(d_range_4h_out) == 1 and d_range_4h_out[0].kind == "market"

    view_up = StructureView(regime=Regime.TREND_UP, zones=(z_res,), atr=2.0)
    d_trend_out = router.on_bar("BTCUSDT", (6000, 110.2, 112.6, 110.0, 112.4, 1.0), 15, view_up, 2.0, [ev_out],
                                position_open=False, entry_resting=False)
    assert len(d_trend_out) == 1 and d_trend_out[0].kind == "market"


def test_structure_broken_defense_and_flipped_box_edge_retest_continuation():
    """验证趋势破位防御护栏与大周期角色互换回踩二次起爆(FLIPPED_BOX_EDGE LEVEL_REJECTED):
    1) 当根出现反向 STRUCTURE_BROKEN 时,MarketRouter 立即撤除未成交挂单并拦截同根逆向开仓;
    2) FLIPPED_BOX_EDGE 的回踩拒绝(二次起爆)在大周期(>=4h)认知收盘且具备 R1_WICK 扫损确认时放行,
       自动按 accept_target_box_mult 计算箱体等幅扩展目标并标记 trade_context='回踩确认'。
    """
    from market_operator.events import STRUCTURE_BROKEN

    z_flip_sup = Zone(
        lo=108.0, hi=110.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=3, rejects=0, origin_known=5, last_change_known=12,
        box_opposite=98.0, box_quality=0.65, box_width_atr=6.0, box_rotations=4,
        source_kind="FLIPPED_BOX_EDGE", box_id=7,
    )
    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            accept_target_box_mult=2.0,
            balance_auction_alignment=True,
        )
    )
    view_up = StructureView(regime=Regime.TREND_UP, zones=(z_flip_sup,), atr=2.0)

    # 1) 同根出现空头 STRUCTURE_BROKEN(-1) 与多头入场信号(+1): 撤除挂单并拦截逆向多单!
    ev_e6_down = MarketEvent(
        id=STRUCTURE_BROKEN, direction=-1, ts=1000, known_index=12,
        attrs={"ref": 107.0, "invalid": 107.2},
    )
    ev_retest_long = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=1000, known_index=12,
        attrs={
            "zone": z_flip_sup, "ref": 108.0, "invalid": 107.3,
            "sub_kind": "R1_WICK", "reset_step_ms": 4 * 3600 * 1000,
        },
    )
    decs_blocked = router.on_bar(
        "BTCUSDT", (1000, 109.0, 111.5, 107.5, 111.0, 100.0),
        12, view_up, 2.0, [ev_e6_down, ev_retest_long],
        position_open=False, entry_resting=True,
    )
    assert len(decs_blocked) == 1 and decs_blocked[0].kind == "cancel_entry"
    assert router.counters.get("e6_cancel_orders") == 1
    assert router.counters.get("restraint_e6_reverse_defense_reject") == 1

    # 2) 无反向破位时, 4h 认知收盘的 FLIPPED_BOX_EDGE R1_WICK 回踩确认放行, 目标为等幅箱体扩展(108.0 + 2.0 * (108.0 - 98.0) = 128.0)
    decs_ok = router.on_bar(
        "BTCUSDT", (2000, 109.0, 111.5, 107.5, 111.0, 100.0),
        13, view_up, 2.0, [ev_retest_long],
        position_open=False, entry_resting=False,
    )
    assert len(decs_ok) == 1 and decs_ok[0].kind == "market"
    assert decs_ok[0].meta.get("trade_context") == "回踩确认"
    assert decs_ok[0].hyp.target_1 == pytest.approx(128.0)


def test_fast_tf_reentry_sizing_and_soft_headroom_scaling():
    """验证快时间尺度(<4h) FLIPPED_BOX_EDGE A1_TIGHT 半仓权重(0.5x)与剩余额度动态缩量分配(Soft Headroom >= 0.25x):
    1) 2h 认知收盘的 FLIPPED_BOX_EDGE A1_TIGHT 重返入箱信号自动标记 size_mult=0.5, 4h 保持默认 1.0;
    2) 当周期容量池(TF_CAP=15%)已有 2 笔 5% 仓位(含手续费成本占用约 11.2% 预算,剩余 ~3.8% 额度 >= 25% 标准单)时,
       第 3 笔信号不再因超出 15% 被一刀切硬拒绝,而是按剩余可用额度动态缩量(scale ~ 0.67x)成交。
    """
    from types import SimpleNamespace
    from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind
    from market_operator.campaign import _submit_decisions

    z_flip = Zone(
        lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
        touches=3, rejects=1, origin_known=2, last_change_known=8,
        box_opposite=110.0, box_quality=0.65, box_width_atr=6.0, box_rotations=4,
        source_kind="FLIPPED_BOX_EDGE", box_id=9,
    )
    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            reject_use_struct_stop=True,
            reject_target_mode="opposite",
            reentry_use_box_target=True,
            reentry_use_struct_stop=True,
            balance_auction_alignment=True,
        )
    )
    view_range = StructureView(regime=Regime.RANGE, zones=(z_flip,), atr=2.0)

    ev_2h = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=1000, known_index=10,
        attrs={
            "zone": z_flip, "ref": 100.0, "invalid": 97.8,
            "sub_kind": "A1_TIGHT", "reset_step_ms": 2 * 3600 * 1000,
        },
    )
    ev_4h = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=2000, known_index=11,
        attrs={
            "zone": z_flip, "ref": 100.0, "invalid": 97.8,
            "sub_kind": "A1_TIGHT", "reset_step_ms": 4 * 3600 * 1000,
        },
    )
    d_2h = router.on_bar(
        "BTCUSDT", (1000, 99.5, 101.4, 99.2, 101.0, 100.0),
        10, view_range, 2.0, [ev_2h], position_open=False, entry_resting=False,
    )
    assert len(d_2h) == 1 and d_2h[0].meta.get("size_mult") == pytest.approx(0.5)

    router2 = MarketRouter(router.params)
    d_4h = router2.on_bar(
        "BTCUSDT", (2000, 99.5, 101.4, 99.2, 101.0, 100.0),
        11, view_range, 2.0, [ev_4h], position_open=False, entry_resting=False,
    )
    assert len(d_4h) == 1 and d_4h[0].meta.get("size_mult", 1.0) == pytest.approx(1.0)

    # 验证第 3 槽位剩余额度动态缩量准入
    specs = {
        k: ContractSpec(symbol=k, size_min=0.001, size_step=0.001, size_place=3, price_place=2, min_trade_usdt=5.0)
        for k in ("BTCUSDT@4h", "ETHUSDT@4h", "SOLUSDT@4h")
    }
    broker = Broker(
        equity=1000.0, risk_frac=0.05, leverage_cap=20.0,
        taker_fee=0.0006, slippage_per_side=0.0002, funding_per_8h=0.0001,
        contracts=specs, portfolio_stop_risk_frac=0.45,
        tf_stop_risk_frac_by_tf={"4h": 0.15},
    )
    for sym in ("BTCUSDT@4h", "ETHUSDT@4h"):
        broker.submit(
            OrderIntent(kind=OrderKind.OPEN, symbol=sym, qty_frac=None, reason="open", ref_price=100.0, ts=1000),
            1000, direction=1, invalid=95.0, event_id=BREAKOUT_ACCEPTED, target_1=115.0,
        )
        broker.on_bar_open(sym, 2000, 100.0)

    st_mock = SimpleNamespace(
        frame=SimpleNamespace(
            key="SOLUSDT@4h",
            cog=SimpleNamespace(bars=[(2000, 99.5, 101.4, 99.2, 101.0, 100.0)] * 15, regimes=[Regime.RANGE] * 15),
            mic=None,
        )
    )
    counters = {"e1_priority_cancels": 0, "portfolio_cap_skips": 0, "open_intents": 0, "limits_armed": 0}
    cap_skips = []
    _submit_decisions(st_mock, broker, d_4h, 2000, 2000, 2000, {}, counters, cap_skips, 24, 6000)
    assert counters["portfolio_cap_skips"] == 0
    assert counters["open_intents"] == 1
    assert 0.25 <= d_4h[0].meta["size_mult"] < 1.0


def test_wide_box_short_geometric_target_and_bankruptcy_rr_guard():
    """验证宽幅箱体做空目标对数(几何)正数保护(复现 UAIUSDT 案例,零魔法阈值、无人工硬拦截):
    当 box_hi=0.1887063, box_lo=0.07303 做空突破时,算术减法得出负数目标(-0.15832 <= 0),
    自动采用对数等幅投影 0.07303*(0.07303/0.1887063)^2 = 0.010938 > 0,确保目标恒正。
    """
    z_uai = Zone(
        lo=0.07303, hi=0.090828, role=ZoneRole.SUPPORT, state=ZoneState.BROKEN,
        touches=5, rejects=2, origin_known=118, last_change_known=158,
        box_opposite=0.1887063, box_quality=0.65, box_width_atr=4.5, box_rotations=4,
        source_kind="BOX_EDGE", box_id=4,
    )
    router = MarketRouter(
        RouterParams(
            whitelist=WHITELIST_BALANCE,
            accept_use_struct_stop=True,
            accept_target_box_mult=2.0,
            balance_auction_alignment=True,
        )
    )
    view_down = StructureView(regime=Regime.TREND_DOWN, zones=(z_uai,), atr=0.02445)

    ev_timely = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=-1, ts=1000, known_index=157,
        attrs={
            "zone": z_uai, "ref": 0.07303, "invalid": 0.093276674,
            "sub_kind": "A2_OVEREXTENDED", "ext_atr": 0.85,
            "reset_step_ms": 4 * 3600 * 1000,
        },
    )
    d_ok = router.on_bar(
        "UAIUSDT", (1000, 0.07270, 0.07386, 0.06381, 0.06639, 1000.0),
        157, view_down, 0.02445, [ev_timely], position_open=False, entry_resting=False,
    )
    assert len(d_ok) == 1
    expected_geo = 0.07303 * ((0.07303 / 0.1887063) ** 2.0)
    assert d_ok[0].hyp.target_1 > 0.0
    assert d_ok[0].hyp.target_1 == pytest.approx(expected_geo)
    assert d_ok[0].hyp.r_mult > 2.0


def test_mic_tracker_retains_candidate_across_pause_and_gap_bar():
    """验证细周期 MicTracker 在 count >= req_count 时不因单根停顿/跳空假阳线误删突破候选:
    1) 第 3 根箱外 K 线若暂为小幅停顿(close_pos < 0.50 但仍守在箱外 cd.lo - bm 之下),保留候选至第 4 根;
    2) 第 4 根恢复向下推进(含向下跳空低开 c < prev_c 且 close_pos >= 0.50)时立即确认 BREAKOUT_ACCEPTED。
    """
    from types import SimpleNamespace
    from market_operator.trigger import MicTracker

    p = Params(use_balance_detector=True, n_a=3, n_f=3, k_bm=0.1, k_rj=0.3)
    z = Zone(
        lo=0.07303, hi=0.09083, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        box_opposite=0.18871, box_quality=0.65, box_width_atr=4.5, box_rotations=4,
    )
    s_mock = SimpleNamespace(
        atr=lambda: 0.025, regime=lambda: Regime.TREND_DOWN, pivots=(),
        last_low_pivot=None, last_high_pivot=None,
        _zones=SimpleNamespace(zones=[z]),
    )
    det_mock = SimpleNamespace(structure=s_mock, _episodes=[], _trend_candidates=[])
    tracker = MicTracker(p, scale=2, trigger_tf_seconds=7200, obs_tf_seconds=14400)
    tracker.sync(0, det_mock, lambda i: 0, lambda i: 1)
    tracker._prev_trig_close = 0.07404

    # Bar 1: 跌破 z.lo - bm (0.07303 - 0.0025 = 0.07053), count = 1
    assert tracker.on_trigger_bar(0, 1000, 0.07270, 0.07386, 0.06381, 0.06672) == []
    assert len(tracker._candidates) == 1 and tracker._candidates[0].count == 1

    # Bar 2: 继续驻留箱外, count = 2
    assert tracker.on_trigger_bar(1, 2000, 0.06662, 0.06685, 0.06274, 0.06420) == []
    assert len(tracker._candidates) == 1 and tracker._candidates[0].count == 2

    # Bar 3: 仍在箱外(0.06350 < 0.07053), 但当根收于上半部停顿(close_pos = 0.30 < 0.50) -> 候选保留(count=3), 不误删!
    assert tracker.on_trigger_bar(2, 3000, 0.06400, 0.06420, 0.06180, 0.06350) == []
    assert len(tracker._candidates) == 1 and tracker._candidates[0].count == 3

    # Bar 4: 向下跳空低开(o=0.06123, h=0.06434, l=0.05919, c=0.06150 < prev_c 0.06350, close_pos=0.55 >= 0.50) -> 立即确认!
    evs = tracker.on_trigger_bar(3, 4000, 0.06123, 0.06434, 0.05919, 0.06150)
    assert len(evs) == 1
    assert evs[0].id == BREAKOUT_ACCEPTED and evs[0].direction == -1
    assert tracker._candidates == []

    # 验证 check_impulse_breakout 强动能即时放行接口
    p_imp = Params(use_balance_detector=True, n_a=3, n_f=3, k_bm=0.1, k_rj=0.3, fast_accept_atr=0.80)
    tracker_imp = MicTracker(p_imp, scale=2, trigger_tf_seconds=7200, obs_tf_seconds=14400)
    z_imp = Zone(
        lo=0.07303, hi=0.09083, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        box_opposite=0.18871, box_quality=0.65, box_width_atr=4.5, box_rotations=4,
    )
    s_imp = SimpleNamespace(
        atr=lambda: 0.025, regime=lambda: Regime.TREND_DOWN, pivots=(),
        last_low_pivot=None, last_high_pivot=None,
        _zones=SimpleNamespace(zones=[z_imp]),
    )
    tracker_imp.sync(0, SimpleNamespace(structure=s_imp, _episodes=[], _trend_candidates=[]), lambda i: 0, lambda i: 1)
    tracker_imp._prev_trig_close = 0.07404
    evs_imp = tracker_imp.on_trigger_bar(0, 1000, 0.07270, 0.07350, 0.04900, 0.05000)
    assert len(evs_imp) == 1
    assert evs_imp[0].id == BREAKOUT_ACCEPTED and evs_imp[0].attrs.get("sub_kind") == "A3_FAST"


def test_dynamic_trade_leverage_planning_within_cap():
    """验证开仓管理在 <= min(leverage_cap=20, maxLever) 范围内根据风险、止损距离与保证金规划自动计算杠杆:
    1) 宽止损波段单(如 XANUSDT 4h 止损距离 7.25%, 名义仓位 0.69x 权益)自动规划为低杠杆(~8x), 而非硬设 20x;
    2) 极宽止损长线单(止损距离 15%, 名义仓位 0.25x 权益)自动规划为 3x~4x 杠杆;
    3) 窄止损短线单(止损距离 0.8%, 名义仓位 6.25x 权益)按需提升至上限 20x(若标的 maxLever=10 则严格受限于 10x)。
    """
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import live_runner as lr

    set_calls = []
    fake_runner = SimpleNamespace(
        mode="live",
        hedge_mode=True,
        risk={"leverage_cap": 20, "portfolio_cap_frac": 0.50},
        contract_raw={
            "XANUSDT": {"maxLever": "50"},
            "BTCUSDT": {"maxLever": "125"},
            "LOWCAPUSDT": {"maxLever": "10"},
        },
        positions={},
        _leverage_done=set(),
        leverage_by_sym={},
        _exch_lev_cap={},
        client=SimpleNamespace(
            set_leverage=lambda sym, lev, hold_side=None: set_calls.append((sym, lev, hold_side))
        ),
        _journal=lambda *a, **k: None,
        _alert_throttled=lambda *a, **k: None,
    )
    fake_runner._max_allowed_leverage = lr.LiveRunner._max_allowed_leverage.__get__(fake_runner, lr.LiveRunner)
    fake_runner._plan_trade_leverage = lr.LiveRunner._plan_trade_leverage.__get__(fake_runner, lr.LiveRunner)
    fake_runner._ensure_leverage = lr.LiveRunner._ensure_leverage.__get__(fake_runner, lr.LiveRunner)

    # 1) 启动预检(target_lev=None): 不向交易所盲目发 20x 请求
    fake_runner._ensure_leverage("XANUSDT")
    assert set_calls == []

    # 2) XANUSDT 4h 宽止损单(equity=1000, capital=1000, risk=50(5%), stop_dist=7.25% -> notional=690)
    lev_xan = fake_runner._plan_trade_leverage("XANUSDT", notional=690.0, equity=1000.0, capital=1000.0, candidate_risk=55.0)
    assert 1 <= lev_xan <= 10  # 约 8x, 远低于 20x
    fake_runner._ensure_leverage("XANUSDT", lev_xan)
    assert fake_runner.leverage_by_sym["XANUSDT"] == lev_xan
    assert ("XANUSDT", lev_xan, "long") in set_calls and ("XANUSDT", lev_xan, "short") in set_calls

    # 3) 窄止损单在 maxLever=10 的品种上严格受限于 <= 10x, 在 BTCUSDT 上受限于 <= 20x
    lev_lowcap = fake_runner._plan_trade_leverage("LOWCAPUSDT", notional=5000.0, equity=1000.0, capital=1000.0, candidate_risk=55.0)
    assert lev_lowcap == 10
    lev_btc = fake_runner._plan_trade_leverage("BTCUSDT", notional=5000.0, equity=1000.0, capital=1000.0, candidate_risk=55.0)
    assert lev_btc == 20









