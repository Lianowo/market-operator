"""路由策略:市场事件与结构假设 → 交易计划(RouteDecision)。

职责:
- 接收认知层或触发层产出的结构行为事件(BREAKOUT_ACCEPTED / LEVEL_REJECTED 等);
- 执行 F1 最小失效距离闸门、结构失衡确认(struct_impulse_confirm)与箱沿离开复位(zone_departure_reset);
- 产出标准化的市价/撤单 ``RouteDecision``。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from .hypothesis import Hypothesis, StructureView, entry_contract_error, entry_cost_error
from .structure import PivotKind, Regime, ZoneRole, ZoneState, _bar_parts

_REF_REL_TOL = 1e-9


def _event_provenance(ev: MarketEvent) -> dict:
    """提取事件结构溯源元数据(ref / invalid / zone 边界与对侧锚点)。"""
    prov: dict = {
        "event_extreme": ev.attrs.get("ex"),
        "event_sub_kind": ev.attrs.get("sub_kind"),
        "evidence_started_index": (
            ev.attrs.get("started")
            if ev.attrs.get("started") is not None
            else ev.attrs.get("ex_index")
        ),
        "ev_ref": (
            float(ev.attrs["ref"]) if ev.attrs.get("ref") is not None else None
        ),
        "ev_invalid": (
            float(ev.attrs["invalid"])
            if ev.attrs.get("invalid") is not None
            else None
        ),
        "sub_kind": ev.attrs.get("sub_kind"),
    }
    zone = ev.attrs.get("zone")
    if zone is not None:
        prov.update({
            "source_kind": getattr(zone, "source_kind", "BOX_EDGE"),
            "zone_lo": float(zone.lo),
            "zone_hi": float(zone.hi),
            "zone_touches": int(zone.touches),
            "zone_origin_known": int(zone.origin_known),
            "zone_last_change_known": int(zone.last_change_known),
        })
        if getattr(zone, "box_opposite", None) is not None:
            prov["box_opposite"] = float(zone.box_opposite)
        # 主张身份:价格相同不等于同一机会,对象重建也不等于换机会。
        prov["claim_id"] = (tuple(zone.claim_id[0]), zone.claim_id[1],
                            int(zone.claim_id[2]))
        prov["box_id"] = getattr(zone, "box_id", None)
        prov["geometry_version"] = int(getattr(zone, "geometry_version", 0))
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
    struct_impulse_confirm: bool = False
    zone_departure_reset: bool = False
    min_box_touches: int = 0         # >0 时要求 zone.touches >= min_box_touches
    min_box_quality: float = 0.0     # >0 时要求 zone.box_quality >= min_box_quality
    min_box_rotations: int = 0       # >0 时要求 zone.box_rotations >= min_box_rotations
    min_box_width_atr: float = 0.0   # >0 时要求 zone.box_width_atr >= min_box_width_atr
    min_box_rr: float = 0.0          # >0 时要求 |box_opposite - close| / stop_dist >= min_box_rr
    max_confirm_stop_atr: float = 1.2
    max_cost_frac: Optional[float] = None
    cost_per_side: float = 0.0008         # 单边成本比例(taker+滑点),仅用于 max_cost_frac
    target_from_pivot: bool = False
    far_confirm_skip: bool = False
    accept_target_box_mult: Optional[float] = None
    reentry_use_box_target: bool = False
    reentry_use_struct_stop: bool = False
    reentry_target_management: bool = False  # Opt in to lock/reduce on reentry target.
    bounded_min_rr: float = 0.0     # Explicit candidate threshold; not a full-box-span proxy.
    max_box_progress_frac: Optional[float] = None
    max_accept_ext_atr: float = 1.5
    htf_wall_filter: bool = False
    htf_min_room_r: float = 1.0
    balance_auction_alignment: bool = False
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
        htf_views: Sequence[StructureView] = (),
    ) -> list[RouteDecision]:
        """处理当前 bar 的新增事件,返回路由决策序列。"""
        _ts, _o, h, l, c, _v = _bar_parts(bar)
        self._notes = []
        decisions: list[RouteDecision] = []
        atr = float(atr)

        wl = self.params._normalized_whitelist
        # 拍卖市场理论多尺度职责边界:
        # 降档细周期(mic=True)仅负责"向既有大周期价值区内部回归"的触发(BOX_EDGE 拒绝回归 + FLIPPED_BOX_EDGE 重返入箱);
        # 向箱外发散建立新价值(BOX_EDGE 外向 BREAKOUT_ACCEPTED 与 FLIPPED_BOX_EDGE 外向回踩拒绝)必须由大周期自身认知收盘确认。
        filtered_events: list[MarketEvent] = []
        for e in new_events:
            if e.attrs.get("mic") and e.attrs.get("zone") is not None:
                z_sk = getattr(e.attrs.get("zone"), "source_kind", "BOX_EDGE")
                e_cid = canonical_event_id(e.id)
                if e_cid == BREAKOUT_ACCEPTED and z_sk == "BOX_EDGE":
                    self._bump("restraint_mic_outward_accept_reject")
                    self._notes.append(
                        f"mic_outward_accept|{symbol} {e.id} BOX_EDGE outward breakout "
                        "deferred to HTF cognition close"
                    )
                    continue
                if e_cid == LEVEL_REJECTED and z_sk == "FLIPPED_BOX_EDGE":
                    self._bump("restraint_mic_outward_retest_reject")
                    self._notes.append(
                        f"mic_outward_retest|{symbol} {e.id} FLIPPED_BOX_EDGE outward retest "
                        "deferred to HTF cognition close"
                    )
                    continue
            filtered_events.append(e)
        new_events = filtered_events

        # 趋势破位防御护栏(STRUCTURE_BROKEN):
        # 1) 当根出现任意方向 STRUCTURE_BROKEN 时,若存在未成交入场挂单则立即撤单;
        # 2) 当根出现某方向 STRUCTURE_BROKEN(如 -1 多头结构破位)时,严禁在同根反向(-ev.direction, 如 +1)开仓接刀。
        e6_dirs = {
            int(e.direction)
            for e in new_events
            if canonical_event_id(e.id) == STRUCTURE_BROKEN
        }
        e1_fires = any(
            canonical_event_id(e.id) == BREAKOUT_ACCEPTED
            and (BREAKOUT_ACCEPTED, e.direction) in wl
            for e in new_events
        )
        slot_freed = False
        if e6_dirs and entry_resting and not position_open:
            decisions.append(
                RouteDecision(
                    kind="cancel_entry",
                    route=ROUTE_BREAKOUT_ACCEPTED,
                    meta={
                        "reason": (
                            "STRUCTURE_BROKEN signal cancels unfilled entry limit "
                            "(trend-break defense)"
                        )
                    },
                )
            )
            slot_freed = True
            self._bump("e6_cancel_orders")
        if e1_fires:
            if symbol in self._pending:
                ep = self._pending.pop(symbol)
                self._notes.append(
                    f"e1_priority|{symbol} weak-STRUCTURE_BROKEN episode "
                    f"(dir={ep.direction}) dropped on BREAKOUT_ACCEPTED signal"
                )
                self._bump("e1_drops_pending")
            if entry_resting and not position_open and not slot_freed:
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
            if cid != STRUCTURE_BROKEN and (-int(ev.direction)) in e6_dirs:
                self._bump("restraint_e6_reverse_defense_reject")
                self._notes.append(
                    f"e6_defense|{symbol} {ev.id}|{side} blocked by same-bar reverse STRUCTURE_BROKEN"
                )
                continue
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
                decision = self._route_e6(symbol, ev, view, h, l, c, atr, bar_index, htf_views=htf_views)
            elif cid == BREAKOUT_ACCEPTED:
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_BREAKOUT_ACCEPTED, "突破接受,永远市价次开盘",
                    htf_views=htf_views,
                )
            elif cid == LEVEL_REJECTED:
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_LEVEL_REJECTED, "拒绝确认,市价次开盘",
                    htf_views=htf_views,
                )
                self._bump(SIGNAL_COUNTERS[LEVEL_REJECTED])
            elif cid == FLIP_RETESTED:
                decision = self._route_e7(symbol, ev, view, c, atr, bar_index)
            elif cid in MARKET_ROUTES:
                route, label, counter = MARKET_ROUTES[cid]
                decision = self._route_market(
                    symbol, ev, view, c, atr, route, label + ",市价次开盘",
                    htf_views=htf_views,
                )
                self._bump(counter)
            else:
                self._notes.append(f"unknown_event|{symbol} {ev.id}")
                decision = None
            if decision is not None:
                if self.params.max_cost_frac is not None and self.params.max_cost_frac > 0.0:
                    decision = replace(decision, meta={
                        **decision.meta, "max_cost_frac": self.params.max_cost_frac,
                        "cost_per_side": self.params.cost_per_side})
                decisions.append(decision)
                armed = True
        if not decisions and not new_events:
            self._notes.append(f"flat|{symbol} no new event")
        return decisions


    # -- STRUCTURE_BROKEN:质量路由(双向;margin 镜像) -----------------------------------------

    def _route_e6(self, symbol: str, ev: MarketEvent, view: StructureView,
                  h: float, l: float, c: float, atr: float,
                  bar_index: int, *,
                  htf_views: Sequence[StructureView] = ()) -> Optional[RouteDecision]:
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
            "STRONG 破位,市价次开盘", htf_views=htf_views)

    def _apply_htf_wall_guard(
        self,
        cid: str,
        direction: int,
        close: float,
        atr: float,
        dec: RouteDecision,
        htf_views: Sequence[StructureView],
    ) -> Optional[RouteDecision]:
        """跨周期上下级拍卖结构感知(Top-Down HTF Imbalance & Wall Guard):
        1. 分形失衡对齐:日线宏观主结构单边失衡(tf_hours>=24, macro_imbalance == -direction)时禁止下级周期逆大势接刀/追单;
           低周期(struct_impulse_confirm=True)的箱沿拒绝(LEVEL_REJECTED)同样禁止逆上级中枢失衡(如 12H)接刀;
        2. 严禁小周期迎头撞进未突破的大周期活跃平衡箱反向墙内沿/带内追单;
        3. 前方存在大周期反向箱沿且净空间 < htf_min_room_r·R 时直接放弃;
        4. 前方大周期反向箱沿空间充足但挡在原 target_1 之前时,收缩 target_1 至大周期墙前缘。
        """
        hyp = dec.hyp
        if hyp is None:
            return dec
        is_rej = cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
        for hv in htf_views:
            if hv is None:
                continue
            m_imb = int(getattr(hv, "macro_imbalance", 0) or 0)
            tf_h = int(getattr(hv, "tf_hours", 0) or 0)
            if m_imb == -direction:
                if tf_h >= 24 or (self.params.struct_impulse_confirm and is_rej):
                    self._bump("restraint_htf_imbalance_reject")
                    return None
        if self.params.balance_auction_alignment:
            return dec
        risk = direction * (close - hyp.invalid)
        capped_target = hyp.target_1
        for hv in htf_views:
            if hv is None:
                continue
            htf_atr = float(getattr(hv, "atr", 0.0) or 0.0)
            bm_htf = self.params.k_bm * htf_atr
            for hz in getattr(hv, "zones", ()):
                if hz.state is not ZoneState.ACTIVE:
                    continue
                if getattr(hz, "source_kind", "BOX_EDGE") != "BOX_EDGE":
                    continue
                if direction > 0 and hz.role is ZoneRole.RESISTANCE:
                    wall_in, wall_out = float(hz.lo), float(hz.hi)
                    if wall_in - 0.1 * atr <= close <= wall_out + bm_htf:
                        self._bump("restraint_htf_wall_reject")
                        return None
                    if close < wall_in:
                        room_htf = wall_in - close
                        if risk > 0.0 and room_htf < self.params.htf_min_room_r * risk:
                            self._bump("restraint_htf_room_reject")
                            return None
                        if capped_target is not None and wall_in < capped_target:
                            capped_target = wall_in
                            self._bump("htf_target_capped")
                elif direction < 0 and hz.role is ZoneRole.SUPPORT:
                    wall_out, wall_in = float(hz.lo), float(hz.hi)
                    if wall_out - bm_htf <= close <= wall_in + 0.1 * atr:
                        self._bump("restraint_htf_wall_reject")
                        return None
                    if close > wall_in:
                        room_htf = close - wall_in
                        if risk > 0.0 and room_htf < self.params.htf_min_room_r * risk:
                            self._bump("restraint_htf_room_reject")
                            return None
                        if capped_target is not None and wall_in > capped_target:
                            capped_target = wall_in
                            self._bump("htf_target_capped")
        if capped_target != hyp.target_1 and capped_target is not None:
            new_rm = (direction * (capped_target - close) / risk) if risk > 0.0 else hyp.r_mult
            new_hyp = replace(hyp, target_1=float(capped_target), r_mult=new_rm)
            return replace(dec, hyp=new_hyp)
        return dec

    def _route_market(
        self, symbol: str, ev: MarketEvent, view: StructureView,
        close: float, atr: float, route: str, tag: str, *,
        htf_views: Sequence[StructureView] = (),
    ) -> Optional[RouteDecision]:
        zone = ev.attrs.get("zone")
        cid = canonical_event_id(ev.id)
        sk = getattr(zone, "source_kind", "BOX_EDGE") if zone is not None else "BOX_EDGE"
        reentry = (cid == BREAKOUT_ACCEPTED and zone is not None
                   and sk == "FLIPPED_BOX_EDGE")
        flip_reject = (cid in (BREAKOUT_FAILED, LEVEL_REJECTED) and zone is not None
                       and sk == "FLIPPED_BOX_EDGE")
        bounded = ((cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
                    and not flip_reject
                    and self.params.reject_target_mode != "none")
                   or (reentry and self.params.reentry_use_box_target))
        opp = getattr(zone, "box_opposite", None)
        if bounded and (opp is None or ev.direction * (float(opp) - close) <= 0.0):
            self._bump("structure_target_unavailable")
            return None
        prior_locks = dict(self._zone_lock)
        dec = self._route_market_impl(symbol, ev, view, close, atr, route, tag)
        if dec is None:
            self._zone_lock = prior_locks
            return None
        if self.params.htf_wall_filter and htf_views:
            dec = self._apply_htf_wall_guard(cid, ev.direction, close, atr, dec, htf_views)
            if dec is None:
                self._zone_lock = prior_locks
                return None
        floor = max(self.params.min_box_rr, self.params.bounded_min_rr) if bounded else 0.0
        meta = {**dec.meta, "bounded_entry": bounded, "entry_min_rr": floor,
                "structural_target": dec.hyp.target_1,
                "execution_target": (None if reentry and not self.params.reentry_target_management
                                     else dec.hyp.target_1),
                "confirm_close": float(close), "confirm_ts": ev.attrs.get("close_ts"),
                "trade_context": (
                    "重新入箱" if reentry
                    else ("回踩确认" if flip_reject
                          else ("箱沿拒绝" if cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
                                else "箱外突破"))
                )}
        if zone is not None and atr > 0.0:
            inner = float(zone.hi if ev.direction > 0 else zone.lo)
            meta["inner_edge"] = inner
            if self.params.max_box_progress_frac is not None and bounded:
                meta["max_box_progress_frac"] = float(self.params.max_box_progress_frac)
            meta["confirm_distance_atr"] = ev.direction * (close - inner) / atr
            meta["remaining_room_atr"] = ev.direction * (float(opp) - close) / atr if opp is not None and bounded else None
        error = entry_contract_error(ev.direction, dec.hyp.entry, dec.hyp.invalid, dec.hyp.target_1, meta)
        if error:
            self._zone_lock = prior_locks
            self._bump(error.lower())
            return None
        return replace(dec, meta=meta)

    def _route_market_impl(
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
        if p.balance_auction_alignment and zone is not None:
            if (
                (view.regime is Regime.TREND_DOWN and ev.direction > 0)
                or (view.regime is Regime.TREND_UP and ev.direction < 0)
            ):
                self._bump("restraint_counter_trend_reject")
                return None
            sub_k_align = str(ev.attrs.get("sub_kind", ""))
            sk_align = getattr(zone, "source_kind", "BOX_EDGE")
            is_mic_align = bool(ev.attrs.get("mic", False))
            if cid in (BREAKOUT_FAILED, LEVEL_REJECTED):
                if sk_align == "FLIPPED_BOX_EDGE":
                    # 角色互换回踩拒绝(Break-and-Retest /二次起爆):
                    # 1) 必须发生在大周期(>=4h)认知收盘且具备 R1_WICK 扫损引线确认;
                    # 2) 严禁逆当前趋势(已在上方拦截)。
                    step_ms_align = int(ev.attrs.get("reset_step_ms", 0) or 0)
                    if is_mic_align or step_ms_align < 4 * 3600 * 1000:
                        self._bump("restraint_mic_outward_retest_reject")
                        return None
                    if sub_k_align != "R1_WICK":
                        self._bump("restraint_unbacked_stall_reject")
                        return None
                else:
                    if view.regime in (Regime.TREND_UP, Regime.TREND_DOWN):
                        self._bump("restraint_trend_mean_reversion_reject")
                        return None
                    if sub_k_align != "R3_TRAP":
                        self._bump("restraint_unbacked_stall_reject")
                        return None
            elif cid == BREAKOUT_ACCEPTED:
                if view.regime is Regime.TRANSITION and is_mic_align:
                    allow_tr_reentry = False
                    if sk_align == "FLIPPED_BOX_EDGE":
                        eb_t = ev.attrs.get("eval_bars") or ()
                        if len(eb_t) >= 4:
                            _, bo_t, bh_t, bl_t, bc_t = eb_t[-1]
                            b1_t, b2_t, b3_t = eb_t[-2], eb_t[-3], eb_t[-4]
                            dt = int(ev.direction)
                            dir_t = (bc_t < bo_t) if dt < 0 else (bc_t > bo_t)
                            eng_t = (bc_t < min(b1_t[1], b1_t[4])) if dt < 0 else (bc_t > max(b1_t[1], b1_t[4]))
                            out_t = ((bh_t - bl_t) > (b1_t[2] - b1_t[3])) and eng_t
                            bexp_t = abs(bc_t - bo_t) > abs(b1_t[4] - b1_t[1])
                            brk_t = (bc_t < min(b1_t[3], b2_t[3], b3_t[3])) if dt < 0 else (bc_t > max(b1_t[2], b2_t[2], b3_t[2]))
                            allow_tr_reentry = dir_t and out_t and bexp_t and brk_t
                    if not allow_tr_reentry:
                        self._bump("restraint_transition_regime_reject")
                        return None
                if sk_align == "BOX_EDGE" and view.regime is Regime.RANGE:
                    step_ms_brk = int(ev.attrs.get("reset_step_ms", 0) or 0)
                    if is_mic_align or step_ms_brk < 4 * 3600 * 1000:
                        self._bump("restraint_range_outward_breakout_reject")
                        return None
                if sk_align == "BOX_EDGE" and not is_mic_align and sub_k_align == "A1_TIGHT":
                    self._bump("restraint_range_outward_breakout_reject")
                    return None
        if cid == BREAKOUT_ACCEPTED:
            ext_atr = ev.attrs.get("ext_atr")
            if ext_atr is not None:
                overextended = p.max_accept_ext_atr > 0.0 and float(ext_atr) > p.max_accept_ext_atr
            else:
                overextended = ev.attrs.get("sub_kind") == "A2_OVEREXTENDED"
            if overextended:
                self._bump("restraint_a2_overextended_reject")
                return None

        hyp = self._gates(symbol, ev, view, close, atr, route)
        if hyp is None:
            return None

        reason = (
            f"{ev.id}|{side} {route} {tag} entry_ref={close:.8g} "
            f"invalid={hyp.invalid:.8g} target_1={hyp.target_1} "
            f"r_mult={hyp.r_mult}"
        )
        meta_out = _event_provenance(ev)
        if p.balance_auction_alignment and zone is not None:
            sk_align = getattr(zone, "source_kind", "BOX_EDGE")
            sub_k_align = str(ev.attrs.get("sub_kind", ""))
            is_mic_align = bool(ev.attrs.get("mic", False))
            step_ms_align = int(ev.attrs.get("reset_step_ms", 0) or 0)
            if (
                cid == BREAKOUT_ACCEPTED
                and sk_align == "FLIPPED_BOX_EDGE"
                and sub_k_align == "A1_TIGHT"
                and (
                    (not is_mic_align and 0 < step_ms_align < 4 * 3600 * 1000)
                    or (is_mic_align and 0 < step_ms_align <= 1800 * 1000)
                )
            ):
                meta_out["size_mult"] = 0.5
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
            meta=meta_out,
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
        prior_locks = dict(self._zone_lock)
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
        if not self._cost_ok(symbol, ev, limit, hyp.invalid, ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT):
            self._zone_lock = prior_locks
            return None
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

    def _cost_ok(self, symbol: str, ev: MarketEvent, entry: float,
                 invalid: float, route: str) -> bool:
        """显式成本上限按候选入场价与防线计算,确认价和最终限价共用。"""
        p = self.params
        if p.max_cost_frac is None or p.max_cost_frac <= 0.0:
            return True
        dist = ev.direction * (entry - invalid)
        cost_r = ((2.0 * float(p.cost_per_side) * float(entry)) / dist
                  if dist > 0.0 else float("inf"))
        if entry_cost_error(ev.direction, entry, invalid,
                            {"max_cost_frac": p.max_cost_frac,
                             "cost_per_side": p.cost_per_side}):
            side = "long" if ev.direction > 0 else "short"
            self._bump(f"f1_cost_reject_{route}")
            self._notes.append(
                f"f1_cost|{symbol} {ev.id}|{side} cost/risk={cost_r:.4f} > "
                f"max_cost_frac={float(p.max_cost_frac):.4f} ⇒ 无交易(成本地板)")
            return False
        return True

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
            if not self._cost_ok(symbol, ev, close, invalid, route):
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

        # "无扫损无拒绝(No Sweep, No Rejection)"公理:
        # 1) 无细周期触发背书(mic=False)时全周期统一执行;
        # 2) 中短波段周期(struct_impulse_confirm=True,如 2h->30m, 4h->2h)上,边界外侧止损流动性紧贴箱沿,
        #    未刺穿外沿的 R2_STALL 停滞未完成流动性清扫,抢跑入场会成为随后真扫损的燃料,故严格要求 R1_WICK/R3_TRAP 扫损确认。
        is_mic = bool(ev.attrs.get("mic", False))
        sub_k = str(ev.attrs.get("sub_kind", ""))
        if (
            (not is_mic or p.struct_impulse_confirm)
            and cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
            and sub_k == "R2_STALL"
        ):
            self._bump("restraint_unbacked_stall_reject")
            return None

        if p.struct_impulse_confirm:
            eval_bars = ev.attrs.get("eval_bars") or ()
            if len(eval_bars) >= 5:
                _, bo, bh, bl, bc = eval_bars[-1]
                b1, b2, b3, b4 = eval_bars[-2], eval_bars[-3], eval_bars[-4], eval_bars[-5]
                d = int(ev.direction)
                mid_ok = (bc < 0.5 * (bh + bl)) if d < 0 else (bc > 0.5 * (bh + bl))
                dir_bar = (bc < bo) if d < 0 else (bc > bo)
                eng_body = (bc < min(b1[1], b1[4])) if d < 0 else (bc > max(b1[1], b1[4]))
                body_exp = abs(bc - bo) > abs(b1[4] - b1[1])
                brk3 = (bc < min(b1[3], b2[3], b3[3])) if d < 0 else (bc > max(b1[2], b2[2], b3[2]))
                out1 = ((bh - bl) > (b1[2] - b1[3])) and eng_body
                rmax4 = (bh - bl) > max(b1[2] - b1[3], b2[2] - b2[3], b3[2] - b3[3], b4[2] - b4[3])
                # 零参数纯几何高潮腿拦截(Climax Guard):
                # 若前一根 b1 的逆向实体长度严格大于前 3 根(b2..b4)任意一根的总波幅,
                # 本根必须至少收复 b1 实体中点 b1_mid,严禁在瀑布/暴拉加速腿首根微弱停顿接刀。
                b1_adv_body = (b1[4] - b1[1]) if d < 0 else (b1[1] - b1[4])
                b1_mid = 0.5 * (b1[1] + b1[4])
                climax_blocked = (
                    b1_adv_body > max(b2[2] - b2[3], b3[2] - b3[3], b4[2] - b4[3])
                    and ((bc > b1_mid) if d < 0 else (bc < b1_mid))
                )
                if is_mic and cid in (BREAKOUT_FAILED, LEVEL_REJECTED):
                    # 零参数纯 K 线相对几何扫损反转(Sweep Reversal):
                    # 当根同向实体且收盘完全吞没前一根实体(dir_bar and eng_body),
                    # 且收盘越过当根与前根合并区间中点 merged_mid 及前两根 b2 中点 b2_mid。
                    merged_mid = 0.5 * (max(bh, b1[2]) + min(bl, b1[3]))
                    b2_mid = 0.5 * (b2[2] + b2[3])
                    sweep_rev = (
                        sub_k in ("R1_WICK", "R3_TRAP")
                        and dir_bar
                        and eng_body
                        and ((bc < merged_mid and bc < b2_mid) if d < 0 else (bc > merged_mid and bc > b2_mid))
                    )
                    confirmed = (
                        not climax_blocked
                        and (brk3 or (dir_bar and eng_body and body_exp) or sweep_rev)
                        and mid_ok
                    )
                else:
                    htf_rej_imp = (
                        cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
                        and sub_k in ("R1_WICK", "R3_TRAP")
                        and dir_bar
                        and out1
                        and body_exp
                    )
                    confirmed = (
                        not climax_blocked
                        and (brk3 or (rmax4 and out1) or htf_rej_imp)
                        and mid_ok
                    )
                if not confirmed:
                    self._bump("restraint_struct_impulse_reject")
                    return None
        sk_rr = getattr(zone, "source_kind", "BOX_EDGE") if zone is not None else "BOX_EDGE"
        if p.min_box_rr > 0.0 and zone is not None:
            opp = getattr(zone, "box_opposite", None)
            if opp is None and cid in (BREAKOUT_FAILED, LEVEL_REJECTED) and sk_rr != "FLIPPED_BOX_EDGE":
                self._bump("restraint_rr_reject")
                return None
            if opp is not None and dist > 0.0:
                if cid in (BREAKOUT_FAILED, LEVEL_REJECTED) and sk_rr != "FLIPPED_BOX_EDGE":
                    room = ev.direction * (float(opp) - close)
                elif (cid == BREAKOUT_ACCEPTED and p.reentry_use_box_target
                      and sk_rr == "FLIPPED_BOX_EDGE"):
                    room = ev.direction * (float(opp) - close)
                else:
                    room = None
                if room is not None and (room / dist) < p.min_box_rr:
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
        sk_gate = getattr(zone, "source_kind", "BOX_EDGE") if zone is not None else "BOX_EDGE"
        if (cid in (BREAKOUT_FAILED, LEVEL_REJECTED)
                and sk_gate != "FLIPPED_BOX_EDGE"
                and p.reject_target_mode != "none"
                and zone is not None):
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
        elif (
            (cid == BREAKOUT_ACCEPTED
             or (cid in (BREAKOUT_FAILED, LEVEL_REJECTED) and sk_gate == "FLIPPED_BOX_EDGE"))
            and p.accept_target_box_mult is not None
            and float(p.accept_target_box_mult) > 0.0
            and zone is not None
        ):
            opp = getattr(zone, "box_opposite", None)
            if opp is not None:
                ref_edge = float(ev.attrs.get("ref")
                                 if ev.attrs.get("ref") is not None
                                 else (zone.hi if ev.direction > 0 else zone.lo))
                opp_f = float(opp)
                mult = float(p.accept_target_box_mult)
                box_span = abs(ref_edge - opp_f)
                if box_span > 0.0:
                    cand_t = ref_edge + ev.direction * mult * box_span
                    if ev.direction < 0 and cand_t <= 0.0 and ref_edge > 0.0 and opp_f > ref_edge:
                        # 物理非负下界:当宽幅箱体做空导致算术等幅减法跌穿0(<=0)时,
                        # 采用对数(几何)等幅投影 ref_edge * (ref_edge / opp_f)^mult,确保目标价恒为正数且零魔法阈值
                        cand_t = ref_edge * ((ref_edge / opp_f) ** mult)
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
