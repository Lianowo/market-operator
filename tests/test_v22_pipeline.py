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


