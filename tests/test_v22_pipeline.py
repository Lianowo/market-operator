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
    assert len(d_lim) == 1
    assert d_lim[0].kind == "limit"
    assert d_lim[0].route == ROUTE_LEVEL_REJECTED_LIMIT
    assert 120.0 <= d_lim[0].limit_price <= 125.0
    assert d_lim[0].meta.get("pullback_limit") is True

    ev_a2 = MarketEvent(
        id=ACCEPT, direction=+1, ts=30_000, known_index=30,
        attrs={"zone": z, "ref": 128.0, "invalid": 124.5, "sub_kind": "A2_OVEREXTENDED"},
    )
    d_a2 = router.on_bar(
        "BTCUSDT", (30_000, 130.0, 136.0, 129.5, 135.0, 280.0),
        30, view, 5.0, [ev_a2], position_open=False, entry_resting=False,
    )
    assert len(d_a2) == 1
    assert d_a2[0].kind == "limit"
    assert d_a2[0].route == ROUTE_BREAKOUT_ACCEPTED_LIMIT
    assert d_a2[0].limit_price == pytest.approx(128.0)


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


def test_mfe_protect_locks_waist_profit_only_after_structural_expansion():
    """在浮盈未达 1.25R 前绝不推保本制造蚊子肉;达到 >= 1.25R 后将保护位推至 +0.50R 腰部利润区。"""
    from market_operator.position import PositionManager

    pm = PositionManager(
        Params(), time_stop=False, reject_opp_lock_frac=-0.25,
        mfe_protect_r=1.25, mfe_protect_lock_r=0.50,
    )
    pos = pm.open("BTCUSDT", 1, entry=100.0, invalid=95.0, target_1=110.0,
                  event_id=LEVEL_REJECTED, ts=1000, index=0)
    # 1. 仅走出 +0.8R (high=104.0 < 106.25): 保持原始结构止损 95.0, 绝不推 +0.10R 制造蚊子肉
    pm.on_bar("BTCUSDT", (2000, 100.0, 104.0, 99.5, 103.0, 50.0), atr=2.0)
    assert pos.invalid == pytest.approx(95.0)

    # 2. 走出 >= 1.25R (high=108.0, MFE=1.6R >= 1.25R): 止损推进至 102.5 (+0.50R 腰部利润)
    pm.on_bar("BTCUSDT", (3000, 103.0, 108.0, 102.8, 106.0, 50.0), atr=2.0)
    assert pos.invalid == pytest.approx(102.5)



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

