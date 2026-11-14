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
from dataclasses import dataclass, field
from typing import Optional, Sequence

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
from .level import Level, LevelKind, LevelRole, nearest_opposing_level
from .structure import Regime, _bar_parts

ROUTE_BREAKOUT_ACCEPTED = "breakout_accepted_market"
ROUTE_LEVEL_REJECTED = "level_rejected_market"
ROUTE_BREAKOUT_ACCEPTED_LIMIT = "breakout_accepted_limit"
ROUTE_LEVEL_REJECTED_LIMIT = "level_rejected_limit"

WHITELIST_DEFAULT = frozenset({
    (BREAKOUT_ACCEPTED, -1),
    (BREAKOUT_ACCEPTED, +1),
    (LEVEL_REJECTED, -1),
    (LEVEL_REJECTED, +1),
})

SIGNAL_COUNTERS = {
    BREAKOUT_ACCEPTED: "breakout_accepted_signals",
    LEVEL_REJECTED: "level_rejected_signals",
    STRUCTURE_BROKEN: "structure_broken_signals",
}


@dataclass(frozen=True)
class RouterParams:
    """无魔法拟合参数的纯粹结构路由配置(区间外三分之一不对称赔率铁律 min_rr=2.0)。"""

    k_bm: float = 0.15
    min_rr: float = 2.0
    htf_wall_check: bool = True
    cost_per_side: float = 0.0008
    max_cost_frac: Optional[float] = 0.12
    whitelist: frozenset = WHITELIST_DEFAULT

    @property
    def _normalized_whitelist(self) -> frozenset:
        return frozenset((canonical_event_id(eid), int(d)) for eid, d in self.whitelist)


@dataclass(frozen=True)
class RouteDecision:
    """标准路由输出契约(回放 Broker 与实盘 LiveRunner 统一消费)。"""

    kind: str                       # "market" | "limit" | "cancel_entry"
    route: str = ""
    intent: Optional[OrderIntent] = None
    hyp: Optional[Hypothesis] = None
    limit_price: Optional[float] = None
    meta: dict = field(default_factory=dict)


def _level_provenance(ev: MarketEvent, view: StructureView) -> dict:
    lv: Optional[Level] = ev.attrs.get("level") or ev.attrs.get("zone")
    prov: dict = {
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


class MarketRouter:
    """基于【原子水平位 Level】的价格拍卖路由器。"""

    def __init__(self, params: Optional[RouterParams] = None) -> None:
        self.params = params or RouterParams()
        self.counters: dict[str, int] = {}
        self._consumed_claims: dict[tuple, Level] = {}
        self._normalized_whitelist = frozenset(
            (canonical_event_id(eid), int(d)) for eid, d in self.params.whitelist
        )

    def _inc(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def _release_departed_claims(self, h: float, l: float, active_levels: Sequence[Level]) -> None:
        active_uids = {(lv.uid, lv.flip_count) for lv in active_levels if lv.is_active}
        for cid, lv in list(self._consumed_claims.items()):
            ukey = tuple(cid[0])
            if ukey not in active_uids:
                self._consumed_claims.pop(cid, None)
                continue
            w = max(lv.band_hi - lv.band_lo, abs(lv.extreme) * 1e-5)
            if lv.role is LevelRole.SUPPORT and l > lv.band_hi + 3.0 * w:
                self._consumed_claims.pop(cid, None)
            elif lv.role is LevelRole.RESISTANCE and h < lv.band_lo - 3.0 * w:
                self._consumed_claims.pop(cid, None)

    def _auction_aligned(self, ev: MarketEvent, view: StructureView) -> bool:
        """统一二维 (t, P) 时空场对齐公理(零魔法常数、零人工触碰次数歧视、跨任何市场普适):
        1. 二维势垒显著度(is_dominant_2d):
           不靠死板的一维触碰计数歧视 1-touch 大顶底,直接读取该水平势垒在二维画布上的【留白统治面积 prominence_2d】:
           凡跨时间共线势垒(EQUAL / ORIGIN)或二维留白面积高于画布当前中位数的显著势垒,即使仅触碰 1 次亦具备真实流动性池,允许清扫反转(SFP/2B);
           唯有二维留白面积低于等于中位数的局部小褶皱被刺穿(swept)时,属顺势推平而非清扫陷阱。
        2. 二维场相空间匹配:
           - 背景价值盒均衡与极值折返域(RANGE / TRANSITION):只做边界拒绝回归(LEVEL_REJECTED);
           - 净空失衡域(TREND_UP / TREND_DOWN):顺应失衡流向执行显著二维台阶拒绝或蓄势突破。
        """
        lv: Optional[Level] = ev.attrs.get("level") or ev.attrs.get("zone")
        if lv is None or lv.kind is LevelKind.FLIPPED:
            return False
        d = int(ev.direction)
        reg = view.regime
        eid = canonical_event_id(ev.id)

        proms = sorted(float(z.prominence_2d) for z in view.zones) if view.zones else []
        med_prom = proms[len(proms) // 2] if proms else 0.0
        is_dominant_2d = (
            lv.kind in (LevelKind.EQUAL, LevelKind.ORIGIN)
            or float(lv.prominence_2d) > med_prom
        )

        if eid == LEVEL_REJECTED and bool(ev.attrs.get("swept")) and not is_dominant_2d:
            return False

        if reg in (Regime.RANGE, Regime.TRANSITION):
            return eid == LEVEL_REJECTED

        with_trend = (reg is Regime.TREND_UP and d > 0) or (reg is Regime.TREND_DOWN and d < 0)
        if not with_trend:
            return False
        if eid == BREAKOUT_ACCEPTED:
            return lv.kind in (LevelKind.EQUAL, LevelKind.ORIGIN)
        return is_dominant_2d

    def _blocked_by_htf_macro(
        self,
        view: StructureView,
        ev: MarketEvent,
        direction: int,
        entry_ref: float,
        risk: float,
        htf_views: Sequence[StructureView],
    ) -> Optional[str]:
        """自顶向下(Top-Down)二维空间势垒共振治理(既不半空螳臂当车,也不错失日线天花板摸顶与地板抄底):
        1. 高周期趋势共振与顶底反转解锁:
           - 当高周期(如 1D)处于单边趋势且价格悬在半空无遮挡区时,严禁逆势开仓;
           - 当价格直接打入或清扫了高周期自身活跃的反向大水平势垒 [band_lo, band_hi](脚踏日线天花板/地板共振区)时,
             自动解锁 LEVEL_REJECTED 极值摸顶/抄底!
        2. 高周期极值折返期禁追突破:高周期处于折返期(TRANSITION)时,禁止追高杀跌做突破(BREAKOUT_ACCEPTED)。
        3. 高周期前方阻碍墙:顺势正前方不足 min_rr * Risk 处横亘高周期反向势垒时,拒绝多在天花板、空在地板!
        """
        if not htf_views:
            return None
        eid = canonical_event_id(ev.id)
        ev_ex = float(ev.attrs.get("ex") or entry_ref)
        for hv in htf_views:
            against_htf = (hv.regime is Regime.TREND_UP and direction < 0) or (
                hv.regime is Regime.TREND_DOWN and direction > 0
            )
            if against_htf:
                backed_by_htf_wall = False
                if eid == LEVEL_REJECTED:
                    want_role = LevelRole.RESISTANCE if direction < 0 else LevelRole.SUPPORT
                    for hlv in hv.zones:
                        if hlv.is_active and hlv.role is want_role:
                            if (direction < 0 and ev_ex >= hlv.band_lo) or (
                                direction > 0 and ev_ex <= hlv.band_hi
                            ):
                                backed_by_htf_wall = True
                                break
                if not backed_by_htf_wall:
                    return "htf_counter_trend"
            if hv.regime is Regime.TRANSITION and eid == BREAKOUT_ACCEPTED:
                return "htf_transition_breakout"
            if self.params.htf_wall_check and risk > 0.0:
                opp = nearest_opposing_level(hv.zones, direction, entry_ref)
                if opp is not None:
                    room = direction * (opp.neck - entry_ref)
                    if room < self.params.min_rr * risk:
                        return "htf_wall"
        return None

    def on_bar(
        self,
        symbol: str,
        bar: Sequence[float],
        bar_index: int,
        view: Optional[StructureView],
        atr: float,
        events: Sequence[MarketEvent],
        *,
        position_open: bool = False,
        entry_resting: bool = False,
        htf_views: Sequence[StructureView] = (),
        market_breadth: float = 0.0,
        universe_size: int = 1,
        median_dollar_volume: float = 0.0,
    ) -> list[RouteDecision]:
        if view is None:
            return []
        ts, _o, h, l, c, _v = _bar_parts(bar)
        self._release_departed_claims(h, l, view.zones)
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

            lv: Optional[Level] = ev.attrs.get("level") or ev.attrs.get("zone")
            if lv is not None and lv.claim_id in self._consumed_claims:
                self._inc("skipped_duplicate_claim")
                continue

            if not self._auction_aligned(ev, view):
                self._inc("rejected_counter_trend")
                continue

            # 突破确认棒二分控场公理(零魔法常数):多头确认收盘须站上本棒几何中点 0.5*(h+l),空头须收于中点之下,拒绝冲高回落/探底回升衰竭棒!
            if eid == BREAKOUT_ACCEPTED:
                bar_mid = 0.5 * (h + l)
                if (int(ev.direction) > 0 and c < bar_mid) or (int(ev.direction) < 0 and c > bar_mid):
                    self._inc("rejected_weak_breakout_close")
                    continue

            if universe_size >= 4:
                if (int(ev.direction) > 0 and market_breadth < -0.5) or (
                    int(ev.direction) < 0 and market_breadth > 0.5
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

            raw_target = float(hyp.target_1)
            exec_tgt = raw_target
            rs_priority = float(getattr(view, "dollar_volume", 0.0))

            meta = {
                **_level_provenance(ev, view),
                "event_id": eid,
                "direction": int(hyp.direction),
                "regime": view.regime.value,
                "invalid": float(hyp.invalid),
                "target_1": exec_tgt,
                "structural_target": raw_target,
                "execution_target": exec_tgt,
                "target_source": hyp.target_source,
                "r_mult": hyp.r_mult,
                "priority_score": rs_priority,
                "imbalance_strength": float(getattr(view, "imbalance_strength", 0.0)),
                "bounded_entry": True,
                "entry_min_rr": float(self.params.min_rr),
                "cost_per_side": float(self.params.cost_per_side),
                "claim_confirmed_ts": int(ts),
                "claim_confirmed_price": float(c),
            }
            if self.params.max_cost_frac is not None:
                meta["max_cost_frac"] = float(self.params.max_cost_frac)

            err = entry_contract_error(hyp.direction, c, hyp.invalid, raw_target, meta)
            if err is not None:
                self._inc(f"rejected_{err.lower()}")
                continue

            if entry_resting:
                if eid == BREAKOUT_ACCEPTED:
                    decisions.append(RouteDecision(
                        kind="cancel_entry",
                        meta={"reason": "breakout_accepted_supersedes_resting"},
                    ))
                    entry_resting = False
                else:
                    self._inc("skipped_entry_resting")
                    continue

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


