"""路由策略层:原子水平位拍卖事件 → 结构自洽检验 → 标准化交易决策(RouteDecision)。

第一性原理(顶尖传统图表 + 价格行为 + AMT 交易员扣扳机前的四大自然自洽律):
1. 拍卖状态对齐(Auction & Polarity Alignment):
   - 顺大势或均衡/结构转换期:所有有效原子水平位(SWING / EQUAL / FLIPPED / ORIGIN)均可交易;
   - 逆强单边趋势(如在明确 LH+LL 空头趋势中试图摸底做多):绝不在首次触碰普通单点拐点时盲目逆势接飞刀,
     仅当价格打入多次验证的齐平流动性池(EQUAL,双底/双顶)或完成极性互换(FLIPPED)/关键起点(ORIGIN)时才允许出手。
2. 同周期对手方水平位净空空间(Structural Room >= Risk):
   若顺势前方存在未消耗的反向原子水平位(target_1),净空利润空间必须 >= 本笔结构止损风险(R:R >= 1:1),
   绝不迎面顶着近在咫尺的反向阻力/支撑墙开仓。
3. 更高周期原子阻碍墙规避(HTF Level Wall):
   若更高周期(如日线对 2h/4h)正前方不足 1R 处横亘着反向活跃原子水平位,放弃撞墙追单。
4. 物理交易摩擦可行性(Friction Viability):
   结构止损幅度必须足以承载交易所双边手续费与滑点摩擦,过滤极窄微观噪音单。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .broker import OrderIntent, OrderKind
from .events import (
    BREAKOUT_ACCEPTED,
    LEVEL_REJECTED,
    STRUCTURE_BROKEN,
    MarketEvent,
    canonical_event_id,
)
from .hypothesis import (
    Hypothesis,
    StructureView,
    build_hypothesis,
    entry_contract_error,
)
from .level import Level, LevelKind, LevelRole, LevelSide
from .structure import BarrierTopology, Regime, _bar_parts


def infer_2d_barrier_topology(
    cur_price: float,
    view: StructureView,
    probe_price: float | None = None,
) -> tuple[BarrierTopology, Level | None, Level | None]:
    """零参数二维势垒包络与失衡状态机。"""
    dominant_levels = [
        lv for lv in view.active_levels
        if lv.kind in (LevelKind.EQUAL, LevelKind.ORIGIN)
        or lv.prominence_2d > view.med_prominence_2d
    ]

    supports = [
        lv for lv in dominant_levels
        if lv.side == LevelSide.SUPPORT and lv.band_lo <= cur_price
    ]
    resistances = [
        lv for lv in dominant_levels
        if lv.side == LevelSide.RESISTANCE and lv.band_hi >= cur_price
    ]

    nearest_sup = max(supports, key=lambda x: x.band_hi) if supports else None
    nearest_res = min(resistances, key=lambda x: x.band_lo) if resistances else None

    if probe_price is not None:
        px_lo, px_hi = min(cur_price, probe_price), max(cur_price, probe_price)
        for lv in dominant_levels:
            if lv.band_lo <= px_hi and px_lo <= lv.band_hi:
                return BarrierTopology.BARRIER_CONTEST, nearest_sup, nearest_res

    if view.regime is Regime.TREND_UP:
        return BarrierTopology.IMBALANCE_UP, nearest_sup, nearest_res
    if view.regime is Regime.TREND_DOWN:
        return BarrierTopology.IMBALANCE_DOWN, nearest_sup, nearest_res

    for lv in dominant_levels:
        if lv.band_lo <= cur_price <= lv.band_hi:
            return BarrierTopology.BARRIER_CONTEST, nearest_sup, nearest_res

    return BarrierTopology.ENCLOSED_BALANCE, nearest_sup, nearest_res


def blocked_by_market_breadth(
    direction: int, own_regime: Regime, breadth: float, n_universe: int
) -> bool:
    """系统性风暴保护(纯粹过半数原则 Majority > 50%,零小数魔法常数)。"""
    if n_universe < 4:
        return False
    if direction > 0 and own_regime is Regime.TREND_UP:
        return False
    if direction < 0 and own_regime is Regime.TREND_DOWN:
        return False
    if direction > 0 and breadth < -0.5:
        return True
    if direction < 0 and breadth > 0.5:
        return True
    return False


ROUTE_BREAKOUT_ACCEPTED = "breakout_accepted_market"
ROUTE_LEVEL_REJECTED = "level_rejected_market"
ROUTE_BREAKOUT_ACCEPTED_LIMIT = "breakout_accepted_limit"
ROUTE_LEVEL_REJECTED_LIMIT = "level_rejected_limit"

WHITELIST_DEFAULT: frozenset[tuple[str, int]] = frozenset({
    (BREAKOUT_ACCEPTED, -1),
    (BREAKOUT_ACCEPTED, +1),
    (LEVEL_REJECTED, -1),
    (LEVEL_REJECTED, +1),
})

SIGNAL_COUNTERS: Mapping[str, str] = MappingProxyType({
    BREAKOUT_ACCEPTED: "breakout_accepted_signals",
    LEVEL_REJECTED: "level_rejected_signals",
    STRUCTURE_BROKEN: "structure_broken_signals",
})

_THIRD = 1.0 / 3.0
_TWO_THIRDS = 2.0 / 3.0
_MIN_BREADTH_UNIVERSE = 4
_CONVICTION_MAX_PTS = 6


@dataclass(frozen=True)
class RouterParams:
    """无魔法拟合参数的纯粹结构路由配置(区间外三分之一不对称赔率铁律 min_rr=2.0)。"""

    k_bm: float = 0.15
    min_rr: float = 2.0
    htf_wall_check: bool = True
    cost_per_side: float | None = 0.0
    max_cost_frac: float | None = 0.12
    conviction_sizing: bool = False
    whitelist: frozenset[tuple[str, int]] = WHITELIST_DEFAULT

    @property
    def _normalized_whitelist(self) -> frozenset[tuple[str, int]]:
        return frozenset((canonical_event_id(eid), int(d)) for eid, d in self.whitelist)


@dataclass(frozen=True)
class RouteDecision:
    """标准路由输出契约(回放 Broker 与实盘 LiveRunner 统一消费)。"""

    kind: str                       # "market" | "limit" | "cancel_entry"
    route: str = ""
    intent: OrderIntent | None = None
    hyp: Hypothesis | None = None
    limit_price: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _level_provenance(ev: MarketEvent, view: StructureView) -> dict[str, Any]:
    lv: Level | None = ev.attrs.get("level") or ev.attrs.get("zone")
    prov: dict[str, Any] = {
        "event_extreme": ev.attrs.get("ex"),
        "event_sub_kind": ev.attrs.get("sub_kind"),
        "sub_kind": ev.attrs.get("sub_kind"),
        "evidence_started_index": ev.attrs.get("started", ev.attrs.get("ex_index")),
        "ev_ref": float(ev.attrs["ref"]) if ev.attrs.get("ref") is not None else None,
        "ev_invalid": float(ev.attrs["invalid"]) if ev.attrs.get("invalid") is not None else None,
        "claim_confirm_regime": view.regime.value,
    }
    if lv is not None:
        prov.update({
            "source_kind": lv.kind.value,
            "level_uid": int(lv.uid),
            "level_kind": lv.kind.value,
            "level_role": lv.role.value,
            "level_extreme": float(lv.extreme),
            "level_neck": float(lv.neck),
            "zone_lo": float(lv.band_lo),
            "zone_hi": float(lv.band_hi),
            "zone_touches": int(lv.touches),
            "zone_origin_known": int(lv.known_index),
            "zone_last_change_known": int(lv.last_change_known),
            "level_origin_ts": int(lv.origin_ts),
            "claim_id": lv.claim_id,
            "box_id": int(lv.uid),
            "geometry_version": int(lv.geometry_version),
            "claim_created_ts": int(lv.origin_ts),
            "claim_created_price": float(lv.extreme),
        })
    return prov


def _is_backed_by_htf_wall(
    hv: StructureView, direction: int, px: float, ev_ex: float
) -> bool:
    want_role = LevelSide.RESISTANCE if direction < 0 else LevelSide.SUPPORT
    for hlv in hv.active_levels:
        if hlv.side is not want_role:
            continue
        if direction < 0 and ev_ex >= hlv.band_lo and px <= hlv.band_hi:
            return True
        if direction > 0 and ev_ex <= hlv.band_hi and px >= hlv.band_lo:
            return True
    return False


def _is_htf_naked_against(
    eid: str,
    direction: int,
    px: float,
    ev_ex: float,
    htf_views: Sequence[StructureView] | None,
) -> bool:
    """检查高周期是否处于无墙保护的反向单边失衡真空中(防逆大势接飞刀)。"""
    if not htf_views:
        return False
    for hv in htf_views:
        backed = eid == LEVEL_REJECTED and _is_backed_by_htf_wall(hv, direction, px, ev_ex)
        if backed:
            continue
        h_topo, _, _ = infer_2d_barrier_topology(px, hv)
        if direction > 0 and h_topo == BarrierTopology.IMBALANCE_DOWN:
            return True
        if direction < 0 and h_topo == BarrierTopology.IMBALANCE_UP:
            return True
    return False


def _is_strong_sweep_bar(
    direction: int, o: float, h: float, lo: float, c: float, view: StructureView
) -> bool:
    rng_bar = max(h - lo, 1e-12)
    wick_def_bar = ((min(o, c) - lo) / rng_bar) if direction > 0 else ((h - max(o, c)) / rng_bar)
    q75_raw_bar = getattr(view, "bar_range_q75_rel", None)
    trig_park_bar = float(getattr(view, "trig_park_ratio", 1.0))
    return bool(
        wick_def_bar >= _THIRD
        and q75_raw_bar is not None
        and float(q75_raw_bar) >= 1.0
        and trig_park_bar >= 1.0
    )


class MarketRouter:
    """基于【原子水平位 Level】的价格拍卖路由器。"""

    def __init__(self, params: RouterParams | None = None) -> None:
        self.params = params or RouterParams()
        self.counters: dict[str, int] = {}
        self._consumed_claims: dict[tuple[Any, ...], Level] = {}
        self._normalized_whitelist = frozenset(
            (canonical_event_id(eid), int(d)) for eid, d in self.params.whitelist
        )

    def _inc(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def _release_departed_claims(self, h: float, lo: float, active_levels: Sequence[Level]) -> None:
        active_anchors = {(lv.origin_index, lv.flip_count) for lv in active_levels if lv.is_active}
        for cid, lv in list(self._consumed_claims.items()):
            akey = tuple(cid[0])
            if akey not in active_anchors:
                self._consumed_claims.pop(cid, None)
                continue
            w = max(lv.band_hi - lv.band_lo, abs(lv.extreme) * 1e-5)
            if lv.role is LevelRole.SUPPORT and lo > lv.band_hi + 3.0 * w:
                self._consumed_claims.pop(cid, None)
            elif lv.role is LevelRole.RESISTANCE and h < lv.band_lo - 3.0 * w:
                self._consumed_claims.pop(cid, None)

    def _auction_aligned(
        self,
        ev: MarketEvent,
        view: StructureView,
        htf_views: Sequence[StructureView] | None = None,
        cur_price: float | None = None,
        *,
        strong_sweep_bar: bool = False,
    ) -> bool:
        lv: Level | None = ev.level
        if lv is None:
            return False
        eid = canonical_event_id(ev.id)
        d = int(ev.direction)
        px = float(cur_price) if cur_price is not None else float(ev.price)

        is_dominant_2d = (
            lv.kind in (LevelKind.EQUAL, LevelKind.ORIGIN)
            or lv.prominence_2d > view.med_prominence_2d
        )
        is_dominant_rej = (
            lv.kind is LevelKind.ORIGIN
            or lv.prominence_2d > view.med_prominence_2d
        )
        ev_ex = float(ev.attrs.get("ex") or px)
        topo, _, _ = infer_2d_barrier_topology(px, view)
        htf_naked_against = _is_htf_naked_against(eid, d, px, ev_ex, htf_views)

        if eid == LEVEL_REJECTED:
            swept = bool(ev.attrs.get("swept"))
            if swept and is_dominant_2d and not lv.flipped and strong_sweep_bar and not htf_naked_against:
                return True
            if (swept and not is_dominant_rej) or htf_naked_against or lv.flipped:
                return False
            if (d > 0 and topo == BarrierTopology.IMBALANCE_DOWN) or (
                d < 0 and topo == BarrierTopology.IMBALANCE_UP
            ):
                return False
            if topo in (BarrierTopology.IMBALANCE_UP, BarrierTopology.IMBALANCE_DOWN) or view.regime in (
                Regime.TREND_UP,
                Regime.TREND_DOWN,
            ):
                return is_dominant_rej
            return True

        if eid == BREAKOUT_ACCEPTED:
            if lv.kind is LevelKind.FLIPPED or lv.flipped:
                return False
            if not is_dominant_2d or htf_naked_against:
                return False
            if d > 0 and topo != BarrierTopology.IMBALANCE_UP:
                return False
            if d < 0 and topo != BarrierTopology.IMBALANCE_DOWN:
                return False
            return True

        return False

    def _htf_view_blocks_wall(
        self, hv: StructureView, direction: int, entry_ref: float, risk: float
    ) -> bool:
        h_topo, _, _ = infer_2d_barrier_topology(entry_ref, hv)
        with_htf_imbalance = (
            (direction > 0 and (h_topo == BarrierTopology.IMBALANCE_UP or hv.regime == Regime.TREND_UP))
            or (direction < 0 and (h_topo == BarrierTopology.IMBALANCE_DOWN or hv.regime == Regime.TREND_DOWN))
        )
        req = risk if with_htf_imbalance else (self.params.min_rr * risk)
        for h_lv in hv.active_levels:
            if direction > 0 and h_lv.side == LevelSide.RESISTANCE and h_lv.band_lo > entry_ref:
                if (h_lv.band_lo - entry_ref) < req:
                    return True
            elif direction < 0 and h_lv.side == LevelSide.SUPPORT and h_lv.band_hi < entry_ref:
                if (entry_ref - h_lv.band_hi) < req:
                    return True
        return False

    def _blocked_by_htf_macro(
        self,
        view: StructureView,
        ev: MarketEvent,
        direction: int,
        entry_ref: float,
        risk: float,
        htf_views: Sequence[StructureView],
    ) -> str | None:
        _ = (view, ev)
        if not self.params.htf_wall_check or not htf_views or risk <= 0.0:
            return None
        for hv in htf_views:
            if self._htf_view_blocks_wall(hv, direction, entry_ref, risk):
                return "htf_wall"
        return None

    def _evaluate_conviction(
        self,
        *,
        eid: str,
        direction: int,
        o: float,
        h: float,
        lo: float,
        c: float,
        view: StructureView,
        ev: MarketEvent,
        universe_size: int,
        excess_ret_48h: float,
        excess_ret_120h: float,
    ) -> tuple[float, int | None, float | None, str | None]:
        """Compute priority score and optional 6-point pure-OHLC conviction multiplier."""
        rs_priority = float(direction) * float(getattr(view, "imbalance_strength", 0.0))
        if not self.params.conviction_sizing:
            return rs_priority, None, None, None

        cog_vr4 = float(getattr(view, "cog_vr4", 1.0))
        trig_vr4 = float(getattr(view, "trig_vr4", 1.0))
        cog_park_ratio = float(getattr(view, "cog_park_ratio", 1.0))
        cog_er_norm_raw = getattr(view, "cog_er_norm", None)
        cog_er_norm = (
            float(cog_er_norm_raw)
            if cog_er_norm_raw is not None
            else float(getattr(view, "cog_er20", 0.0)) * math.sqrt(20.0)
        )
        trig_park_ratio = float(getattr(view, "trig_park_ratio", 1.0))
        bar_range_rel = float(getattr(view, "bar_range_rel", 1.0))
        bar_range_q75_raw = getattr(view, "bar_range_q75_rel", None)

        rng = max(h - lo, 1e-12)
        body_eff = abs(c - o) / rng
        wick_def = ((min(o, c) - lo) / rng) if direction > 0 else ((h - max(o, c)) / rng)
        sub_kind = str(ev.attrs.get("sub_kind") or "")
        r48_dir = direction * float(excess_ret_48h)
        r120_dir = direction * float(excess_ret_120h)
        er_dir = direction * cog_er_norm

        if eid == BREAKOUT_ACCEPTED and cog_park_ratio < 1.0 and er_dir > 1.0:
            return rs_priority, None, None, "rejected_dead_vol_exhausted"

        vol_wake = (cog_park_ratio >= 1.0) if eid == BREAKOUT_ACCEPTED else (trig_park_ratio >= 1.0)
        bar_q75_hit = (
            (float(bar_range_q75_raw) >= 1.0)
            if bar_range_q75_raw is not None
            else (bar_range_rel >= 1.5)
        )
        fresh_phase = (er_dir <= 1.0) if eid == BREAKOUT_ACCEPTED else bar_q75_hit
        phase_pts = int(vol_wake) + int(fresh_phase)

        if eid == BREAKOUT_ACCEPTED:
            rs_priority = (cog_vr4 - 1.0) + (1.0 - trig_vr4) + 2.0 * r48_dir + 0.8 * phase_pts
        else:
            rs_priority = (
                (cog_vr4 - 1.0)
                - 2.0 * r48_dir
                + (1.0 if sub_kind in ("SWEEP_TRAP", "FLIP_RETEST") else 0.0)
                + 0.8 * phase_pts
            )

        base_pts = 1 if cog_vr4 >= 1.0 else 0
        if eid == BREAKOUT_ACCEPTED:
            base_pts += int(trig_vr4 < 1.0)
            base_pts += int(body_eff <= _TWO_THIRDS)
            base_pts += int(universe_size < _MIN_BREADTH_UNIVERSE or r48_dir > 0.0)
        else:
            base_pts += int(wick_def >= _THIRD)
            base_pts += int(body_eff <= _TWO_THIRDS and wick_def > 0.0)
            base_pts += int(
                (universe_size >= _MIN_BREADTH_UNIVERSE and r120_dir < 0.0)
                or sub_kind in ("SWEEP_TRAP", "FLIP_RETEST")
            )

        if base_pts == 0:
            return rs_priority, None, None, "rejected_zero_conviction"
        pts = min(_CONVICTION_MAX_PTS, max(1, base_pts + phase_pts))
        conviction_mult = 0.5 + (float(pts) - 1.0) / 5.0
        return rs_priority, pts, conviction_mult, None

    def _build_decision_meta(
        self,
        *,
        ev: MarketEvent,
        eid: str,
        hyp: Hypothesis,
        view: StructureView,
        ts: int,
        c: float,
        rs_priority: float,
        conviction_pts: int | None,
        conviction_mult: float | None,
    ) -> dict[str, Any]:
        raw_target = float(hyp.target_1)
        eff_cost = float(self.params.cost_per_side if self.params.cost_per_side is not None else 0.0)
        meta: dict[str, Any] = {
            **_level_provenance(ev, view),
            "event_id": eid,
            "direction": int(hyp.direction),
            "regime": view.regime.value,
            "invalid": float(hyp.invalid),
            "target_1": raw_target,
            "structural_target": raw_target,
            "execution_target": raw_target,
            "target_source": hyp.target_source,
            "r_mult": hyp.r_mult,
            "priority_score": rs_priority,
            "imbalance_strength": float(getattr(view, "imbalance_strength", 0.0)),
            "cog_vr4": float(getattr(view, "cog_vr4", 1.0)),
            "trig_vr4": float(getattr(view, "trig_vr4", 1.0)),
            "bounded_entry": True,
            "entry_min_rr": float(self.params.min_rr),
            "cost_per_side": eff_cost,
            "claim_confirmed_ts": int(ts),
            "claim_confirmed_price": float(c),
        }
        if conviction_pts is not None and conviction_mult is not None:
            meta["conviction_score"] = conviction_pts
            meta["size_mult"] = conviction_mult
        if self.params.max_cost_frac is not None:
            meta["max_cost_frac"] = float(self.params.max_cost_frac)
        return meta

    def on_bar(
        self,
        symbol: str,
        bar: Sequence[float],
        bar_index: int,
        view: StructureView | None,
        atr: float,
        events: Sequence[MarketEvent],
        *,
        position_open: bool = False,
        entry_resting: bool = False,
        htf_views: Sequence[StructureView] = (),
        market_breadth: float = 0.0,
        universe_size: int = 1,
        median_dollar_volume: float = 0.0,
        excess_ret_48h: float = 0.0,
        excess_ret_120h: float = 0.0,
    ) -> list[RouteDecision]:
        _ = (bar_index, atr, median_dollar_volume)
        if view is None:
            return []
        ts, o, h, lo, c, _v = _bar_parts(bar)
        self._release_departed_claims(h, lo, view.zones)
        decisions: list[RouteDecision] = []

        for ev in events:
            eid = canonical_event_id(ev.id)
            sig_counter = SIGNAL_COUNTERS.get(eid)
            if sig_counter:
                self._inc(sig_counter)
            if (eid, int(ev.direction)) not in self._normalized_whitelist:
                continue
            if position_open:
                self._inc("skipped_position_open")
                continue

            lv: Level | None = ev.attrs.get("level") or ev.attrs.get("zone")
            if lv is not None and lv.claim_id in self._consumed_claims:
                self._inc("skipped_duplicate_claim")
                continue

            strong_sweep = _is_strong_sweep_bar(int(ev.direction), o, h, lo, c, view)
            if not self._auction_aligned(ev, view, htf_views=htf_views, cur_price=c, strong_sweep_bar=strong_sweep):
                self._inc("rejected_counter_trend")
                continue

            if eid == BREAKOUT_ACCEPTED:
                bar_mid = 0.5 * (h + lo)
                if (int(ev.direction) > 0 and c < bar_mid) or (int(ev.direction) < 0 and c > bar_mid):
                    self._inc("rejected_weak_breakout_close")
                    continue

            if universe_size >= _MIN_BREADTH_UNIVERSE and blocked_by_market_breadth(
                int(ev.direction), view.regime, market_breadth, universe_size
            ):
                self._inc("rejected_market_breadth")
                continue

            hyp = build_hypothesis(ev, view, ref_price=c, htf_views=htf_views, min_rr=self.params.min_rr)
            if hyp is None:
                self._inc("rejected_bad_hypothesis")
                continue

            risk = hyp.direction * (c - hyp.invalid)
            htf_block = self._blocked_by_htf_macro(view, ev, hyp.direction, c, risk, htf_views)
            if htf_block is not None:
                self._inc(f"rejected_{htf_block}")
                continue

            rs_priority, conv_pts, conv_mult, conv_err = self._evaluate_conviction(
                eid=eid,
                direction=int(hyp.direction),
                o=o,
                h=h,
                lo=lo,
                c=c,
                view=view,
                ev=ev,
                universe_size=universe_size,
                excess_ret_48h=excess_ret_48h,
                excess_ret_120h=excess_ret_120h,
            )
            if conv_err is not None:
                self._inc(conv_err)
                continue

            meta = self._build_decision_meta(
                ev=ev,
                eid=eid,
                hyp=hyp,
                view=view,
                ts=ts,
                c=c,
                rs_priority=rs_priority,
                conviction_pts=conv_pts,
                conviction_mult=conv_mult,
            )
            err = entry_contract_error(hyp.direction, c, hyp.invalid, float(hyp.target_1), meta)
            if err is not None:
                self._inc(f"rejected_{err.lower()}")
                continue

            if entry_resting:
                if eid != BREAKOUT_ACCEPTED:
                    self._inc("skipped_entry_resting")
                    continue
                decisions.append(RouteDecision(
                    kind="cancel_entry",
                    meta={"reason": "breakout_accepted_supersedes_resting"},
                ))
                entry_resting = False

            route_name = ROUTE_BREAKOUT_ACCEPTED if eid == BREAKOUT_ACCEPTED else ROUTE_LEVEL_REJECTED
            meta["route"] = route_name
            meta["entry_mode"] = "market"
            intent = OrderIntent(
                kind=OrderKind.OPEN,
                symbol=symbol,
                qty_frac=None,
                reason=f"{eid}|{route_name}",
                ref_price=float(c),
                ts=int(ts),
            )
            decisions.append(RouteDecision(
                kind="market",
                route=route_name,
                intent=intent,
                hyp=hyp,
                meta=meta,
            ))
            if lv is not None:
                self._consumed_claims[lv.claim_id] = lv.clone()
            self._inc("routed_market")
            break

        return decisions


