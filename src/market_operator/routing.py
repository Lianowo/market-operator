"""路由策略:市场事件与结构假设 → 交易计划(RouteDecision)。

职责:
- 接收认知层或触发层产出的结构行为事件(BREAKOUT_ACCEPTED / LEVEL_REJECTED 等);
- 执行 F1 最小失效距离闸门、结构失衡确认(struct_impulse_confirm)与箱沿离开复位(zone_departure_reset);
- 对死区拒绝([r_lo, r_hi))与过伸突破(>accept_watch_ext_atr)生成系统价格关注(WatchEntry)计划;
- 产出标准化的市价/限价/撤单 ``RouteDecision``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .broker import OrderIntent, OrderKind
from .events import (
    BREAKOUT_ACCEPTED,
    BREAKOUT_FAILED,
    FLIP_RETESTED,
    LEVEL_REJECTED,
    RANGE_ROTATED,
    STRUCTURE_BROKEN,
    TREND_RESUMED,
    MarketEvent,
    canonical_event_id,
)
from .hypothesis import Hypothesis, StructureView
from .structure import PivotKind, _bar_parts

_REF_REL_TOL = 1e-9


def _event_provenance(ev: MarketEvent) -> dict:
    """提取事件结构溯源元数据(ref / invalid / zone 边界与对侧锚点)。"""
    prov: dict = {
        "ev_ref": (
            float(ev.attrs["ref"]) if ev.attrs.get("ref") is not None else None
        ),
        "ev_invalid": (
            float(ev.attrs["invalid"])
            if ev.attrs.get("invalid") is not None
            else None
        ),
    }
    zone = ev.attrs.get("zone")
    if zone is not None:
        prov.update({
            "zone_lo": float(zone.lo),
            "zone_hi": float(zone.hi),
            "zone_touches": int(zone.touches),
        })
        if getattr(zone, "box_opposite", None) is not None:
            prov["box_opposite"] = float(zone.box_opposite)
    return prov


ROUTE_BREAKOUT_ACCEPTED = "breakout_accepted_market"
ROUTE_LEVEL_REJECTED = "level_rejected_market"
ROUTE_BREAKOUT_ACCEPTED_LIMIT = "breakout_accepted_limit"
ROUTE_LEVEL_REJECTED_LIMIT = "level_rejected_limit"
ROUTE_BREAKOUT_FAILED = "breakout_failed_market"
ROUTE_TREND_RESUMED = "trend_resumed_market"
ROUTE_RANGE_ROTATED = "range_rotated_market"
ROUTE_STRUCTURE_BROKEN_STRONG = "structure_broken_strong_market"
ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT = "structure_broken_weak_limit"

MARKET_ROUTES = {
    BREAKOUT_FAILED: (ROUTE_BREAKOUT_FAILED, "失败突破确认", "breakout_failed_signals"),
    TREND_RESUMED: (ROUTE_TREND_RESUMED, "趋势恢复", "trend_resumed_signals"),
    RANGE_ROTATED: (ROUTE_RANGE_ROTATED, "区间回归", "range_rotated_signals"),
}

SIGNAL_COUNTERS = {
    BREAKOUT_ACCEPTED: "breakout_accepted_signals",
    LEVEL_REJECTED: "level_rejected_signals",
    STRUCTURE_BROKEN: "structure_broken_signals",
    FLIP_RETESTED: "flip_retested_signals",
    **{k: v[2] for k, v in MARKET_ROUTES.items()},
}

ENTRY_WHITELIST = frozenset({
    (BREAKOUT_ACCEPTED, -1),
    (BREAKOUT_ACCEPTED, +1),
    (STRUCTURE_BROKEN, -1),
    (STRUCTURE_BROKEN, +1),
    (FLIP_RETESTED, -1),
    (FLIP_RETESTED, +1),
})

WHITELIST_BALANCE = frozenset({
    (BREAKOUT_ACCEPTED, -1),
    (BREAKOUT_ACCEPTED, +1),
    (LEVEL_REJECTED, -1),
    (LEVEL_REJECTED, +1),
})



@dataclass(frozen=True)
class RouterParams:
    """路由策略配置参数。"""

    k_dq: float = 0.5
    k_imp: float = 1.5
    k_is: float = 0.8
    turtle_stop_atr: float = 2.0
    k_bm: float = 0.1
    whitelist: frozenset = ENTRY_WHITELIST
    reject_target_mode: str = "none"         # "none" | "opposite" | "mid"
    reject_use_struct_stop: bool = False
    accept_use_struct_stop: bool = False
    max_target_r: Optional[float] = None
    deadzone_watch_enabled: bool = False
    deadzone_watch_r_lo: float = 1.0
    deadzone_watch_r_hi: float = 1.5
    deadzone_watch_near_frac: float = 0.75
    deadzone_watch_max_prog_frac: float = 0.65
    deadzone_watch_wait_hours: int = 4
    accept_watch_enabled: bool = False
    accept_watch_ext_atr: float = 0.80
    accept_watch_near_frac: float = 0.75
    accept_watch_max_run_atr: float = 1.50
    accept_watch_wait_hours: int = 4
    struct_impulse_confirm: bool = False
    zone_departure_reset: bool = False
    min_box_touches: int = 0         # >0 时要求 zone.touches >= min_box_touches
    min_box_quality: float = 0.0     # >0 时要求 zone.box_quality >= min_box_quality
    min_box_rotations: int = 0       # >0 时要求 zone.box_rotations >= min_box_rotations
    min_box_width_atr: float = 0.0   # >0 时要求 zone.box_width_atr >= min_box_width_atr
    min_box_rr: float = 0.0          # >0 时要求 |box_opposite - close| / stop_dist >= min_box_rr
    max_confirm_stop_atr: float = 1.2     # v22:紧凑市价确认的最大止损宽度(ATR 倍数;超出转 50%/内沿回抽 Limit)
    allow_pullback_limit: bool = True     # True 时过伸确认转回抽 Limit;False 时严格要求当根入场满足 min_box_rr 且具备对侧目标
    max_cost_frac: Optional[float] = None
    cost_per_side: float = 0.0008         # 单边成本比例(taker+滑点),仅用于 max_cost_frac
    target_from_pivot: bool = False
    far_confirm_skip: bool = False
    accept_watch_keep_confirm_stop: bool = False
    accept_target_box_mult: Optional[float] = None
    reentry_use_box_target: bool = False
    reentry_use_struct_stop: bool = False
    _normalized_whitelist: frozenset = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        norm = frozenset(
            (canonical_event_id(eid), int(d)) for eid, d in self.whitelist
        )
        object.__setattr__(self, "_normalized_whitelist", norm)





@dataclass(frozen=True)
class RouteDecision:
    """路由器输出(执行语义由引擎承担:intent/hyp/限价与路由标签)。"""

    kind: str                        # "market" | "limit" | "cancel_entry"
    route: str                       # ROUTE_*(cancel_entry 亦归属触发路由 BREAKOUT_ACCEPTED)
    intent: Optional[OrderIntent] = None
    hyp: Optional[Hypothesis] = None
    limit_price: Optional[float] = None     # kind="limit":前缘限价
    meta: dict = None                # 附加记录字段(如 weak_episode 追溯属性)

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})


@dataclass
class _WeakEpisode:
    """WEAK-STRUCTURE_BROKEN 登记:破位未入场(弱),等待同 episode 的 FLIP_RETESTED(同向)。"""

    l_star: float                    # STRUCTURE_BROKEN attrs["ref"](被破 pivot 价)
    direction: int                   # 登记方向(同 episode FLIP_RETESTED 必须同向)
    bar_index: int                   # STRUCTURE_BROKEN 事件 bar 索引
    margin: float                    # 登记时的质量量(记录字段)
    impulse: float


class MarketRouter:
    """市场事件路由器。"""

    def __init__(self, params: Optional[RouterParams] = None) -> None:
        self.params = params or RouterParams()
        self._pending: dict[str, _WeakEpisode] = {}
        self._notes: list[str] = []
        self.counters: dict[str, int] = {}
        self._zone_lock: dict[tuple, tuple[int, int]] = {}

    def on_bar(
        self,
        symbol: str,
        bar,
        bar_index: int,
        view: StructureView,
        atr: float,
        new_events: Sequence[MarketEvent],
        *,
        position_open: bool,
        entry_resting: bool,
    ) -> list[RouteDecision]:
        """处理当前 bar 的新增事件,返回路由决策序列。"""
        _ts, _o, h, l, c, _v = _bar_parts(bar)
        self._notes = []
        decisions: list[RouteDecision] = []
        atr = float(atr)

        wl = self.params._normalized_whitelist
        e1_fires = any(
            canonical_event_id(e.id) == BREAKOUT_ACCEPTED
            and (BREAKOUT_ACCEPTED, e.direction) in wl
            for e in new_events
        )
        slot_freed = False
        if e1_fires:
            if symbol in self._pending:
                ep = self._pending.pop(symbol)
                self._notes.append(
                    f"e1_priority|{symbol} weak-STRUCTURE_BROKEN episode "
                    f"(dir={ep.direction}) dropped on BREAKOUT_ACCEPTED signal"
                )
                self._bump("e1_drops_pending")
            if entry_resting and not position_open:
                decisions.append(
                    RouteDecision(
                        kind="cancel_entry",
                        route=ROUTE_BREAKOUT_ACCEPTED,
                        meta={
                            "reason": (
                                "BREAKOUT_ACCEPTED signal cancels unfilled entry limit "
                                "(BREAKOUT_ACCEPTED priority)"
                            )
                        },
                    )
                )
                slot_freed = True
                self._bump("e1_cancel_orders")

        holding = position_open or (entry_resting and not slot_freed)
        armed = False
        for ev in new_events:
            side = "long" if ev.direction > 0 else "short"
            cid = canonical_event_id(ev.id)
            if (cid, ev.direction) not in wl:
                self._notes.append(f"nogo|{symbol} {ev.id}|{side} not in whitelist")
                continue
            if cid in (STRUCTURE_BROKEN, BREAKOUT_ACCEPTED, FLIP_RETESTED):
                self._bump(SIGNAL_COUNTERS[cid])
            if holding or armed:
                self._notes.append(
                    f"dup|{symbol} {ev.id}|{side} slot busy (position or "
                    "resting entry; one position/one order per campaign)"
                )
                self._bump("dup")
                continue
            if cid == STRUCTURE_BROKEN:
                decision = self._route_e6(symbol, ev, view, h, l, c, atr, bar_index)
            elif cid == BREAKOUT_ACCEPTED:
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_BREAKOUT_ACCEPTED, "突破接受,永远市价次开盘"
                )
            elif cid == LEVEL_REJECTED:
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_LEVEL_REJECTED, "拒绝确认,市价次开盘"
                )
                self._bump(SIGNAL_COUNTERS[LEVEL_REJECTED])
            elif cid == FLIP_RETESTED:
                decision = self._route_e7(symbol, ev, view, c, atr, bar_index)
            elif cid in MARKET_ROUTES:
                route, label, counter = MARKET_ROUTES[cid]
                decision = self._route_market(
                    symbol, ev, view, c, atr, route, label + ",市价次开盘"
                )
                self._bump(counter)
            else:
                self._notes.append(f"unknown_event|{symbol} {ev.id}")
                decision = None
            if decision is not None:
                decisions.append(decision)
                armed = True
        if not decisions and not new_events:
            self._notes.append(f"flat|{symbol} no new event")
        return decisions


    # -- STRUCTURE_BROKEN:质量路由(双向;margin 镜像) -----------------------------------------

    def _route_e6(self, symbol: str, ev: MarketEvent, view: StructureView,
                  h: float, l: float, c: float, atr: float,
                  bar_index: int) -> Optional[RouteDecision]:
        ref = float(ev.attrs["ref"])
        margin = ev.direction * (c - ref) / atr if atr > 0.0 else float("nan")
        impulse = (h - l) / atr if atr > 0.0 else float("nan")
        strong = margin >= self.params.k_dq and impulse >= self.params.k_imp
        if not strong:
            if symbol in self._pending:
                self._bump("e6_replaced_pending")
            self._pending[symbol] = _WeakEpisode(
                l_star=ref, direction=ev.direction, bar_index=bar_index,
                margin=margin, impulse=impulse)
            self._bump("e6_weak_registered")
            self._notes.append(
                f"weak_e6|{symbol} margin={margin:.4g} < k_dq={self.params.k_dq}"
                f" or impulse={impulse:.4g} < k_imp={self.params.k_imp} ⇒ "
                f"不入场,登记 episode(ref={ref:.8g}),待同 episode FLIP_RETESTED")
            return None
        self._bump("e6_strong_quality")
        self._notes.append(
            f"strong_e6|{symbol} margin={margin:.4g} >= {self.params.k_dq}, "
            f"impulse={impulse:.4g} >= {self.params.k_imp} ⇒ 市价次开盘")
        return self._route_market(
            symbol, ev, view, c, atr, ROUTE_STRUCTURE_BROKEN_STRONG,
            "STRONG 破位,市价次开盘")

    def _route_market(
        self,
        symbol: str,
        ev: MarketEvent,
        view: StructureView,
        close: float,
        atr: float,
        route: str,
        tag: str,
    ) -> Optional[RouteDecision]:
        side = "long" if ev.direction > 0 else "short"
        p = self.params
        cid = canonical_event_id(ev.id)
        zone = ev.attrs.get("zone")

        # v22 轨道 C: A2 过伸突破转翻转边界前缘 Limit 挂单(当显式传入 A2_OVEREXTENDED 且开启回抽限价时)
        if (cid == BREAKOUT_ACCEPTED
                and not p.accept_watch_enabled
                and p.allow_pullback_limit
                and ev.attrs.get("sub_kind") == "A2_OVEREXTENDED"
                and (p.accept_use_struct_stop or p.min_box_rr > 0.0)
                and zone is not None):
            limit_px = float(zone.hi if ev.direction > 0 else zone.lo)
            ev_inv = ev.attrs.get("invalid")
            invalid = float(ev_inv) if (p.accept_use_struct_stop and ev_inv is not None) else (
                limit_px - ev.direction * p.turtle_stop_atr * atr
            )
            dist_lim = ev.direction * (limit_px - invalid)
            if dist_lim >= p.k_is * atr:
                hyp = Hypothesis(
                    event_id=ev.id, direction=ev.direction, ts=ev.ts,
                    known_index=ev.known_index, entry=limit_px,
                    invalid=invalid, target_1=None, r_mult=None, regime=view.regime,
                )
                reason = (f"{ev.id}|{side} {ROUTE_BREAKOUT_ACCEPTED_LIMIT} "
                          f"A2过伸突破回踩前缘限价 limit_px={limit_px:.8g} invalid={invalid:.8g}")
                self._bump("a2_retest_limit_armed")
                return RouteDecision(
                    kind="limit", route=ROUTE_BREAKOUT_ACCEPTED_LIMIT,
                    intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                                       reason=reason, ref_price=float(close), ts=ev.ts),
                    hyp=hyp, limit_price=limit_px,
                    meta={"a2_retest_limit": True, **_event_provenance(ev)},
                )

        hyp = self._gates(symbol, ev, view, close, atr, route)
        if hyp is None:
            return None

        if cid == BREAKOUT_ACCEPTED and p.accept_watch_enabled and zone is not None and atr > 0.0:
            watch_dec = self._build_accept_watch_decision(
                symbol, ev, view, hyp, zone, close, atr, side
            )
            if watch_dec is not None:
                return watch_dec


        # v22 轨道 B:当配置了 min_box_rr > 0 且允许回抽挂单时,确认 K 线过长自动转为 50%/内沿 RestingLimitOrder
        if (cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
                and p.allow_pullback_limit
                and p.reject_use_struct_stop and p.min_box_rr > 0.0
                and zone is not None and atr > 0.0):
            stop_dist = abs(float(close) - float(hyp.invalid))
            opp = getattr(zone, "box_opposite", None)
            room = ev.direction * (float(opp) - float(close)) if opp is not None else None
            rr_thresh = p.min_box_rr
            need_pullback = (
                (p.max_confirm_stop_atr > 0.0 and stop_dist > p.max_confirm_stop_atr * atr)
                or (room is not None and stop_dist > 0.0 and (room / stop_dist) < rr_thresh)
            )
            if need_pullback:
                inner_edge = float(zone.lo if ev.direction < 0 else zone.hi)
                half_pb = 0.5 * (float(close) + inner_edge)
                limit_px = half_pb
                dist_lim = ev.direction * (limit_px - float(hyp.invalid))
                if opp is not None and dist_lim > 0.0:
                    room_lim = ev.direction * (float(opp) - limit_px)
                    if (room_lim / dist_lim) < rr_thresh or (
                        p.max_confirm_stop_atr > 0.0 and dist_lim > p.max_confirm_stop_atr * atr
                    ):
                        if ev.direction * (float(close) - inner_edge) > 0.0:
                            limit_px = inner_edge
                            dist_lim = ev.direction * (limit_px - float(hyp.invalid))
                if dist_lim >= p.k_is * atr and ev.direction * (float(close) - limit_px) > 0.0:
                    target_1 = hyp.target_1
                    r_mult = (ev.direction * (float(target_1) - limit_px) / dist_lim
                              if target_1 is not None and dist_lim > 0.0 else hyp.r_mult)
                    hyp_lim = Hypothesis(
                        event_id=ev.id, direction=ev.direction, ts=ev.ts,
                        known_index=ev.known_index, entry=float(limit_px),
                        invalid=float(hyp.invalid), target_1=target_1,
                        r_mult=r_mult, regime=view.regime,
                    )
                    reason = (f"{ev.id}|{side} {ROUTE_LEVEL_REJECTED_LIMIT} "
                              f"确认K线过长转50%/内沿回抽限价 entry_ref={close:.8g} "
                              f"limit_px={limit_px:.8g} invalid={hyp.invalid:.8g} "
                              f"target_1={target_1} r_mult={r_mult}")
                    self._bump("pullback_limit_armed")
                    return RouteDecision(
                        kind="limit", route=ROUTE_LEVEL_REJECTED_LIMIT,
                        intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                                           reason=reason, ref_price=float(close), ts=ev.ts),
                        hyp=hyp_lim, limit_price=float(limit_px),
                        meta={"pullback_limit": True, **_event_provenance(ev)},
                    )

        if (
            cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
            and p.deadzone_watch_enabled
            and zone is not None
            and atr > 0.0
            and hyp.r_mult is not None
            and float(p.deadzone_watch_r_lo) <= float(hyp.r_mult) < float(p.deadzone_watch_r_hi)
        ):
            watch_dec = self._build_deadzone_watch_decision(
                symbol, ev, view, hyp, zone, close, atr, side
            )
            if watch_dec is not None:
                return watch_dec

        reason = (
            f"{ev.id}|{side} {route} {tag} entry_ref={close:.8g} "
            f"invalid={hyp.invalid:.8g} target_1={hyp.target_1} "
            f"r_mult={hyp.r_mult}"
        )
        return RouteDecision(
            kind="market",
            route=route,
            intent=OrderIntent(
                kind=OrderKind.OPEN,
                symbol=symbol,
                qty_frac=None,
                reason=reason,
                ref_price=float(close),
                ts=ev.ts,
            ),
            hyp=hyp,
            meta=_event_provenance(ev),
        )


    def _build_accept_watch_decision(
        self,
        symbol: str,
        ev: MarketEvent,
        view: StructureView,
        hyp: Hypothesis,
        zone: object,
        close: float,
        atr: float,
        side: str,
    ) -> Optional[RouteDecision]:
        p = self.params
        direction = int(ev.direction)
        edge_px = float(
            ev.attrs["ref"]
            if ev.attrs.get("ref") is not None
            else (zone.hi if direction > 0 else zone.lo)
        )
        close_px = float(close)
        ext_dist = direction * (close_px - edge_px)
        if ext_dist <= float(p.accept_watch_ext_atr) * atr + 1e-9:
            return None

        min_dist = float(p.k_is) * atr
        ev_inv = ev.attrs.get("invalid")
        if p.accept_use_struct_stop and ev_inv is not None:
            inv_px = float(ev_inv)
        elif p.accept_watch_keep_confirm_stop:
            inv_px = float(hyp.invalid)
        else:
            inv_px = edge_px - direction * float(p.turtle_stop_atr) * atr
        f1_floor_px = inv_px + direction * min_dist
        watch_px = max(edge_px, f1_floor_px) if direction > 0 else min(edge_px, f1_floor_px)
        dist_w = direction * (watch_px - inv_px)
        if direction * (close_px - watch_px) <= 1e-9 or dist_w < min_dist - 1e-9:
            return None

        near_frac = float(p.accept_watch_near_frac or 0.0)
        near_px = (
            close_px - near_frac * (close_px - watch_px) if near_frac > 0.0 else None
        )
        max_run = float(p.accept_watch_max_run_atr or 0.0)
        prog_px = (
            close_px + direction * max_run * atr if max_run > 0.0 else None
        )
        target_w = hyp.target_1
        r_w = (
            direction * (float(target_w) - watch_px) / dist_w
            if target_w is not None and dist_w > 0.0
            else None
        )
        hyp_w = Hypothesis(
            event_id=ev.id,
            direction=direction,
            ts=ev.ts,
            known_index=ev.known_index,
            entry=float(watch_px),
            invalid=inv_px,
            target_1=target_w,
            r_mult=r_w,
            regime=view.regime,
        )
        reason = (
            f"{ev.id}|{side} {ROUTE_BREAKOUT_ACCEPTED_LIMIT} "
            f"WATCH_ENTRY 突破过伸({ext_dist / atr:.2f}ATR>{p.accept_watch_ext_atr:.2g}ATR)"
            f"系统价格关注 confirm_close={close_px:.8g} "
            f"watch_px={watch_px:.8g} near_px={near_px} "
            f"prog_px={prog_px} invalid={inv_px:.8g}"
        )
        self._bump("accept_watch_armed")
        return RouteDecision(
            kind="limit",
            route=ROUTE_BREAKOUT_ACCEPTED_LIMIT,
            intent=OrderIntent(
                kind=OrderKind.OPEN,
                symbol=symbol,
                qty_frac=None,
                reason=reason,
                ref_price=float(watch_px),
                ts=ev.ts,
            ),
            hyp=hyp_w,
            limit_price=float(watch_px),
            meta={
                "watch_entry": True,
                "watch_price": float(watch_px),
                "near_price": float(near_px) if near_px is not None else None,
                "prog_price": float(prog_px) if prog_px is not None else None,
                "confirm_close": close_px,
                "max_prog_frac": None,
                "max_wait_hours": int(p.accept_watch_wait_hours),
                **_event_provenance(ev),
            },
        )


    def _build_deadzone_watch_decision(
        self,
        symbol: str,
        ev: MarketEvent,
        view: StructureView,
        hyp: Hypothesis,
        zone: object,
        close: float,
        atr: float,
        side: str,
    ) -> Optional[RouteDecision]:
        p = self.params
        direction = int(ev.direction)
        inner_edge = float(zone.hi if direction > 0 else zone.lo)
        close_px = float(close)
        inv_px = float(hyp.invalid)
        min_dist = float(p.k_is) * atr
        f1_floor_px = inv_px + direction * min_dist
        watch_px = max(inner_edge, f1_floor_px) if direction > 0 else min(inner_edge, f1_floor_px)
        dist_w = direction * (watch_px - inv_px)
        if direction * (close_px - watch_px) <= 1e-9 or dist_w < min_dist - 1e-9:
            return None

        target_1 = hyp.target_1
        new_r = (
            direction * (float(target_1) - watch_px) / dist_w
            if target_1 is not None and dist_w > 0.0
            else hyp.r_mult
        )
        near_frac = float(p.deadzone_watch_near_frac or 0.0)
        near_px = (
            close_px - near_frac * (close_px - watch_px) if near_frac > 0.0 else None
        )
        hyp_w = Hypothesis(
            event_id=ev.id,
            direction=direction,
            ts=ev.ts,
            known_index=ev.known_index,
            entry=float(watch_px),
            invalid=inv_px,
            target_1=target_1,
            r_mult=new_r,
            regime=view.regime,
        )
        reason = (
            f"{ev.id}|{side} {ROUTE_LEVEL_REJECTED_LIMIT} "
            f"WATCH_ENTRY 死区[{p.deadzone_watch_r_lo:.2g},{p.deadzone_watch_r_hi:.2g}R)"
            f"系统价格关注 confirm_close={close_px:.8g} "
            f"watch_px={watch_px:.8g} near_px={near_px} "
            f"invalid={inv_px:.8g} target_1={target_1} r_mult={new_r}"
        )
        self._bump("deadzone_watch_armed")
        return RouteDecision(
            kind="limit",
            route=ROUTE_LEVEL_REJECTED_LIMIT,
            intent=OrderIntent(
                kind=OrderKind.OPEN,
                symbol=symbol,
                qty_frac=None,
                reason=reason,
                ref_price=float(watch_px),
                ts=ev.ts,
            ),
            hyp=hyp_w,
            limit_price=float(watch_px),
            meta={
                "watch_entry": True,
                "watch_price": float(watch_px),
                "near_price": float(near_px) if near_px is not None else None,
                "confirm_close": close_px,
                "max_prog_frac": float(p.deadzone_watch_max_prog_frac),
                "max_wait_hours": int(p.deadzone_watch_wait_hours),
                **_event_provenance(ev),
            },
        )



    # -- WEAK-STRUCTURE_BROKEN → FLIP_RETESTED(同 episode 前缘限价,v4 G3 / v5 §1 语义,双向) -------------

    def _route_e7(self, symbol: str, ev: MarketEvent, view: StructureView,
                  close: float, atr: float, bar_index: int
                  ) -> Optional[RouteDecision]:
        pend = self._pending.get(symbol)
        if pend is None or pend.direction != ev.direction:
            self._bump("e7_no_episode")
            self._notes.append(
                f"e7_no_episode|{symbol} FLIP_RETESTED without registered same-direction "
                "weak-STRUCTURE_BROKEN episode ⇒ 不交易(v6:FLIP_RETESTED 仅作弱破位回踩延续入场)")
            return None
        ref = float(ev.attrs["ref"])
        if abs(ref - pend.l_star) > _REF_REL_TOL * max(1.0, abs(pend.l_star)):
            self._bump("e7_ref_mismatch")
            self._notes.append(
                f"e7_ref_mismatch|{symbol} FLIP_RETESTED ref={ref:.8g} != pending ref="
                f"{pend.l_star:.8g} ⇒ 非同 episode,登记保留")
            return None
        del self._pending[symbol]                     # 消费一次(zone 至多翻转一次)
        self._bump("e7_matched_episode")
        weak_bars = bar_index - pend.bar_index
        side = "long" if ev.direction > 0 else "short"
        hyp = self._gates(symbol, ev, view, close, atr, ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT)
        if hyp is None:
            return None
        zone = ev.attrs.get("zone")
        if zone is None:
            self._bump(f"no_zone_{ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT}")
            self._notes.append(
                f"no_zone|{symbol} FLIP_RETESTED|{side} limit requires zone front edge (G3)")
            return None
        limit = float(zone.lo if ev.direction < 0 else zone.hi)
        reason = (f"FLIP_RETESTED|{side} {ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT} weak-STRUCTURE_BROKEN 同 episode 回踩拒绝入场 "
                  f"entry_ref={close:.8g} invalid={hyp.invalid:.8g} "
                  f"target_1={hyp.target_1} r_mult={hyp.r_mult} "
                  f"limit_front_edge={limit:.8g} l_star={pend.l_star:.8g} "
                  f"weak_bars={weak_bars}")
        return RouteDecision(
            kind="limit", route=ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT,
            intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                               reason=reason, ref_price=float(close), ts=ev.ts),
            hyp=hyp, limit_price=limit,
            meta={"l_star": pend.l_star, "weak_bars": weak_bars,
                  "e6_margin": pend.margin, "e6_impulse": pend.impulse,
                  **_event_provenance(ev)})

    # -- 闸门(turtle 唯一语义:F1 + 2·atr 防线;无目标,不做 room/r_min) ---------

    def _gates(self, symbol: str, ev: MarketEvent, view: StructureView,
               close: float, atr: float, route: str) -> Optional[Hypothesis]:
        side = "long" if ev.direction > 0 else "short"
        p = self.params
        cid = canonical_event_id(ev.id)
        invalid = close - ev.direction * p.turtle_stop_atr * atr
        ev_inv = ev.attrs.get("invalid")
        if cid in (BREAKOUT_FAILED, LEVEL_REJECTED) and p.reject_use_struct_stop and ev_inv is not None:
            invalid = float(ev_inv)
        elif cid == BREAKOUT_ACCEPTED and ev_inv is not None and (
            p.accept_use_struct_stop
            or (p.reentry_use_struct_stop
                and getattr(ev.attrs.get("zone"), "source_kind", "BOX_EDGE") == "FLIPPED_BOX_EDGE")
        ):
            invalid = float(ev_inv)
        # F1(全入口,闸):天然距离不足即笔不存在。
        # A3:优先用**钱的占比**(往返成本/风险)表达同一件事;`k_is` 仅作旧口径回退。
        dist = ev.direction * (close - invalid)
        if p.max_cost_frac is not None and p.max_cost_frac > 0.0:
            cost_r = ((2.0 * float(p.cost_per_side) * float(close)) / dist
                      if dist > 0.0 else float("inf"))
            if cost_r > float(p.max_cost_frac):
                self._bump(f"f1_cost_reject_{route}")
                self._notes.append(
                    f"f1_cost|{symbol} {ev.id}|{side} cost/risk={cost_r:.4f} > "
                    f"max_cost_frac={float(p.max_cost_frac):.4f} ⇒ 无交易(成本地板)")
                return None
        elif dist < p.k_is * atr:
            self._bump(f"f1_reject_{route}")
            self._notes.append(
                f"f1|{symbol} {ev.id}|{side} dist={dist:.4g} < k_is·atr="
                f"{p.k_is * atr:.4g} ⇒ 无交易(F1 全入口下限)")
            return None

        zone = ev.attrs.get("zone")
        # A4:确认位置的语义。拒绝的证据必须发生在**带缘附近**:收盘已深入箱体
        # (离内沿 > max_confirm_stop_atr·ATR)意味着那条"拒绝"已被行情走完,
        # 此刻再进等于追高/追低。旧实现把这件事转成"挂 50% 回抽限价",而回抽
        # 只在反向失败时才成交(adverse selection,实测更差)。正确定义是**丢弃
        # 这一笔**,等同一箱沿的下一次 A2 触碰。
        if (p.far_confirm_skip and p.max_confirm_stop_atr > 0.0
                and zone is not None
                and cid in (BREAKOUT_FAILED, LEVEL_REJECTED)):
            inner_edge = float(zone.hi if ev.direction > 0 else zone.lo)
            if ev.direction * (close - inner_edge) > p.max_confirm_stop_atr * atr:
                self._bump("restraint_far_confirm")
                self._notes.append(
                    f"far_confirm|{symbol} {ev.id}|{side} close 距内沿 "
                    f"{ev.direction * (close - inner_edge):.4g} > "
                    f"{p.max_confirm_stop_atr:.2f}·ATR ⇒ 拒绝证据已过期,不入场")
                return None
        if p.min_box_touches > 0 and zone is not None:
            if int(zone.touches) < p.min_box_touches:
                self._bump("restraint_touches_reject")
                return None
        if p.min_box_quality > 0.0 and zone is not None:
            if float(getattr(zone, "box_quality", 0.0)) < p.min_box_quality:
                self._bump("restraint_quality_reject")
                return None
        if p.min_box_rotations > 0 and zone is not None:
            if int(getattr(zone, "box_rotations", 0)) < p.min_box_rotations:
                self._bump("restraint_rotations_reject")
                return None
        if p.min_box_width_atr > 0.0 and zone is not None:
            if float(getattr(zone, "box_width_atr", 0.0)) < p.min_box_width_atr:
                self._bump("restraint_width_reject")
                return None

        if p.struct_impulse_confirm:
            eval_bars = ev.attrs.get("eval_bars") or ()
            if len(eval_bars) >= 5:
                _, _bo, bh, bl, bc = eval_bars[-1]
                b1, b2, b3, b4 = eval_bars[-2], eval_bars[-3], eval_bars[-4], eval_bars[-5]
                d = int(ev.direction)
                brk3 = (bc < min(b1[3], b2[3], b3[3])) if d < 0 else (bc > max(b1[2], b2[2], b3[2]))
                out1 = ((bh - bl) > (b1[2] - b1[3])) and (
                    (bc < min(b1[1], b1[4])) if d < 0 else (bc > max(b1[1], b1[4]))
                )
                mid_ok = (bc < 0.5 * (bh + bl)) if d < 0 else (bc > 0.5 * (bh + bl))
                rmax4 = (bh - bl) > max(b1[2] - b1[3], b2[2] - b2[3], b3[2] - b3[3], b4[2] - b4[3])
                if not ((brk3 or (rmax4 and out1)) and mid_ok):
                    self._bump("restraint_struct_impulse_reject")
                    return None
        if p.min_box_rr > 0.0 and zone is not None:
            opp = getattr(zone, "box_opposite", None)
            if opp is None and not p.allow_pullback_limit and cid in (BREAKOUT_FAILED, LEVEL_REJECTED):
                self._bump("restraint_rr_reject")
                return None
            if opp is not None and dist > 0.0:
                if cid in (BREAKOUT_FAILED, LEVEL_REJECTED):
                    room = ev.direction * (float(opp) - close)
                elif p.allow_pullback_limit:
                    box_span = abs(float(opp) - (zone.hi if ev.direction > 0 else zone.lo))
                    room = box_span
                else:
                    room = None
                if room is not None and (room / dist) < p.min_box_rr:
                    can_pullback = False
                    if p.allow_pullback_limit and cid in (BREAKOUT_FAILED, LEVEL_REJECTED) and p.reject_use_struct_stop:
                        inner_edge = float(zone.lo if ev.direction < 0 else zone.hi)
                        for cand_px in (0.5 * (close + inner_edge), inner_edge):
                            d_cand = ev.direction * (cand_px - invalid)
                            r_cand = ev.direction * (float(opp) - cand_px)
                            if (ev.direction * (close - cand_px) > 0.0
                                    and d_cand >= p.k_is * atr
                                    and (r_cand / d_cand) >= p.min_box_rr):
                                can_pullback = True
                                break
                    if not can_pullback:
                        self._bump("restraint_rr_reject")
                        return None

        if p.zone_departure_reset and zone is not None:
            zkey = (symbol, round(float(zone.lo), 6), round(float(zone.hi), 6), ev.direction)
            cts = int(ev.attrs.get("close_ts", ev.ts))
            step_ms = int(ev.attrs.get("reset_step_ms", 0))
            prev_lock = self._zone_lock.get(zkey)
            if prev_lock is not None:
                ta, ta_step = prev_lock
                if ta_step > 0 and step_ms > ta_step and (cts - ta) <= 3 * step_ms:
                    self._bump("restraint_coarse_echo_reject")
                    return None
                rb = ev.attrs.get("reset_bars") or ()
                mid = [b for b in rb if ta < (int(b[0]) + step_ms) < cts]
                if not mid:
                    self._bump("restraint_departure_reset_reject")
                    return None
                d = int(ev.direction)
                inner = float(zone.hi) if d > 0 else float(zone.lo)
                outer = float(zone.lo) if d > 0 else float(zone.hi)
                inv = outer - d * p.k_bm * atr
                ready = False
                for b in mid:
                    _, _bo, bh, bl, bc = b
                    in_zone = (bl <= inner) if d > 0 else (bh >= inner)
                    breached = (bl <= inv) if d > 0 else (bh >= inv)
                    if breached or in_zone:
                        ready = False
                        continue
                    if ((bc - inner) if d > 0 else (inner - bc)) >= 0.3 * atr:
                        ready = True
                if not ready:
                    self._bump("restraint_departure_reset_reject")
                    return None
            self._zone_lock[zkey] = (cts, step_ms)
        target_1: Optional[float] = None
        r_mult: Optional[float] = None
        if cid in (BREAKOUT_FAILED, LEVEL_REJECTED) and p.reject_target_mode != "none" and zone is not None:
            opp = getattr(zone, "box_opposite", None)
            if opp is not None:
                opp_f = float(opp)
                if p.reject_target_mode == "opposite":
                    cand_t = opp_f
                elif p.reject_target_mode == "mid":
                    near_edge = float(zone.hi if ev.direction > 0 else zone.lo)
                    cand_t = 0.5 * (near_edge + opp_f)
                else:
                    cand_t = opp_f
                if ev.direction * (cand_t - close) > 0.0:
                    target_1 = cand_t
                    r_mult = ev.direction * (cand_t - close) / dist if dist > 0 else None
                    # A5:目标 = 方向上**最近的结构障碍**。对侧内沿只是一个候选;
                    # 若其间已有确认 pivot(前高/前低),它才是第一个会挡住的价位。
                    if p.target_from_pivot and view.pivots:
                        want = (PivotKind.HIGH if ev.direction > 0 else PivotKind.LOW)
                        obstacles = [
                            float(pv.price) for pv in view.pivots
                            if pv.kind is want
                            and ev.direction * (float(pv.price) - close) > 0.0]
                        if obstacles:
                            near = (min(obstacles) if ev.direction > 0
                                    else max(obstacles))
                            if ev.direction * (near - close) < ev.direction * (target_1 - close):
                                target_1 = near
                                r_mult = (ev.direction * (target_1 - close) / dist
                                          if dist > 0 else None)
                                self._bump("target_from_pivot")
                    # C1:目标 R 上限。截断只改目标锚点(部分减仓/对面收割),不改
                    # 止损与入场,故风险分母恒定,左右尾可比。缺口来源:2h 的
                    # 箱体对面目标常在 6–12R,回放里从未触及 ⇒ 该目标对盈亏无贡献,
                    # 只污染 r_mult 的分布。
                    cap_r = p.max_target_r
                    if (cap_r is not None and r_mult is not None
                            and r_mult > float(cap_r) and dist > 0.0):
                        target_1 = float(close) + ev.direction * float(cap_r) * dist
                        r_mult = float(cap_r)
                        self._bump("target_r_clamped")
        elif (cid == BREAKOUT_ACCEPTED
              and p.reentry_use_box_target
              and zone is not None
              and getattr(zone, "source_kind", "BOX_EDGE") == "FLIPPED_BOX_EDGE"):
            opp = getattr(zone, "box_opposite", None)
            if opp is not None and ev.direction * (float(opp) - close) > 0.0:
                target_1 = float(opp)
                r_mult = ev.direction * (target_1 - close) / dist if dist > 0 else None
        elif (cid == BREAKOUT_ACCEPTED
              and p.accept_target_box_mult is not None
              and float(p.accept_target_box_mult) > 0.0
              and zone is not None):
            opp = getattr(zone, "box_opposite", None)
            if opp is not None:
                ref_edge = float(ev.attrs.get("ref")
                                 if ev.attrs.get("ref") is not None
                                 else (zone.hi if ev.direction > 0 else zone.lo))
                box_span = abs(ref_edge - float(opp))
                if box_span > 0.0:
                    cand_t = ref_edge + ev.direction * float(p.accept_target_box_mult) * box_span
                    if ev.direction * (cand_t - close) > 0.0:
                        target_1 = cand_t
                        r_mult = ev.direction * (cand_t - close) / dist if dist > 0 else None

        hyp = Hypothesis(event_id=ev.id, direction=ev.direction, ts=ev.ts,
                         known_index=ev.known_index, entry=float(close),
                         invalid=float(invalid), target_1=target_1,
                         r_mult=r_mult, regime=view.regime)
        return hyp

    def _bump(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def explain(self) -> str:
        if not self._notes:
            return "flat|no evaluation yet"
        return "\n".join(self._notes)

    def pending_of(self, symbol: str) -> Optional[_WeakEpisode]:
        return self._pending.get(symbol)


__all__ = [
    "RouterParams",
    "MarketRouter",
    "RouteDecision",
    "ENTRY_WHITELIST",
    "WHITELIST_BALANCE",
    "ROUTE_BREAKOUT_ACCEPTED",
    "ROUTE_LEVEL_REJECTED",
    "ROUTE_BREAKOUT_ACCEPTED_LIMIT",
    "ROUTE_LEVEL_REJECTED_LIMIT",
    "ROUTE_STRUCTURE_BROKEN_STRONG",
    "ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT",
    "ROUTE_BREAKOUT_FAILED",
    "ROUTE_TREND_RESUMED",
    "ROUTE_RANGE_ROTATED",
]
