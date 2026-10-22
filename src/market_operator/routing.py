"""v6 路由策略 —— 规范(canonical)策略语义:turtle 出口几何固化为唯一语义。

规范地位:本模块是规范语义链中"假设 → 交易计划"的唯一策略实现。语义
合并(refactor/semantics-unify)时,docs/v6-protocol.md §2-S2 的 A/B
二臂判选已经终局(reports/replay.json/.md 冻结产物:两臂 dev@1.5x
净期望均 ≤ 0,按冻结规则取 t 统计较好的臂 B,如实声明非转正证据),
**臂 B(turtle)固化为唯一策略语义**;臂切换(arm 参数)与臂 A
clamp2R(S1 区缘失效/room≥r_min 门/target 钳 2R)的实现自此移除。
历史 A/B 记录与协议文字保留于 docs/v6-protocol.md(未改动);双臂实现
与其测试冻结归档于 research/rounds/v6/(routing.py/campaign.py/
run_backtest_v6.py 及 tests/,对归档实现运行,可复现 A/B 判定)。

当前唯一语义(turtle,docs/v6-protocol.md §2-S2 臂 B 口径):
- 与 v5(docs/v5-protocol.md)的关系:BREAKOUT_ACCEPTED-FLIP_RETESTED 检测零改动(events.py/
  structure.py 不动);v5 破位质量路由骨架全保留(BREAKOUT_ACCEPTED 市价优先 / STRUCTURE_BROKEN 质量
  路由 STRONG 市价、WEAK 登记 episode 待同 episode FLIP_RETESTED 前缘限价)。
- **双向**(v6-protocol §3):白名单默认 {(BREAKOUT_ACCEPTED,STRUCTURE_BROKEN,FLIP_RETESTED) × {short,long}},方向
  仅作机制桶标签不作挑选;槽位/episode 关联逻辑完全对称(STRUCTURE_BROKEN|long margin
  = close − ref 镜像)。
- 失效:invalid = close − direction·turtle_stop_atr·atr(初始防线
  2.0·atr,S2 冻结口径);**无固定目标**(target_1=None,不做 room/r_min
  门 —— 无结构目标语义,如实声明);F1(k_is=0.8)统一为**闸**:dist =
  direction·(close − invalid) ≥ k_is·atr(事件 bar ATR;天然距离不足即
  笔不存在,禁止人工加宽)。
- 持仓侧(v6-protocol §2-S2 臂 B 声明,由 campaign 接线承担):§8.3
  confirmed pivot 后 trailing = 只收紧(实现即既有 _trail);出场仅
  §8.1/§8.5;§8.6 时间止损关闭(PositionManager(time_stop=False));
  breakeven_on_reduce=True(臂 A 的"减仓后禁保本"约束随臂 A 一并退役)。

explain() 与 counters 语义同 v5;weaker 登记簿/BREAKOUT_ACCEPTED 优先级语义同 v5。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .broker import OrderIntent, OrderKind
from .events import (
    ACCEPT, BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, TREND_RESUMED, RANGE_ROTATED, STRUCTURE_BROKEN, FLIP_RETESTED, EDGE_RECLAIMED, REJECT, MarketEvent,
    canonical_event_id,
)
from .hypothesis import Hypothesis, StructureView, _structural_target
from .structure import _bar_parts

# 同 episode 匹配容差:两侧价格同源(破位 pivot 价 / zone 前缘价,浮点原值
# 拷贝),容差只覆盖二进制表示差;非调参先验。
_REF_REL_TOL = 1e-9


def _event_provenance(ev: MarketEvent) -> dict:
    """v14 加性溯源(docs/v14-protocol.md 施工挂钩;纯记录,零行为改变)。

    事件结构事实(attrs ref / zone 边界与 touches)随路由决策 meta 携带,供
    quality 五问(C1)、stop-entry 参考价(C3)与分层纵深(C4)在不重查事件
    表的前提下精确取数。meta 不上账本数值路径,canonical 数值逐位不变。

    v16 加性扩展(docs/v16-protocol.md;ev_invalid = attrs["invalid"] 原值拷
    贝 = §7 结构失效建议价:BREAKOUT_ACCEPTED 正缘外侧 / BREAKOUT_FAILED/LEVEL_REJECTED 外缘外侧 / EDGE_RECLAIMED 刺穿极值外侧
    / STRUCTURE_BROKEN 结构 pivot±buf / FLIP_RETESTED 远端边缘,全部 = 结构∓k_bm·ATR 口径):供 v16
    收割婚配路由以结构锚替换 turtle 2·ATR 失效;纯 meta 记录,canonical
    路由/账本数值路径逐位不变(canonical parity 对账钉死)。
    """
    prov: dict = {"ev_ref": (float(ev.attrs["ref"])
                             if ev.attrs.get("ref") is not None else None),
                  "ev_invalid": (float(ev.attrs["invalid"])
                                 if ev.attrs.get("invalid") is not None
                                 else None)}
    zone = ev.attrs.get("zone")
    if zone is not None:
        prov.update({"zone_lo": float(zone.lo), "zone_hi": float(zone.hi),
                     "zone_touches": int(zone.touches)})
        if getattr(zone, "box_opposite", None) is not None:
            prov["box_opposite"] = float(zone.box_opposite)
    return prov

ROUTE_BREAKOUT_ACCEPTED = "breakout_accepted_market"
ROUTE_LEVEL_REJECTED = "level_rejected_market"
ROUTE_BREAKOUT_FAILED = "breakout_failed_market"
ROUTE_TREND_RESUMED = "trend_resumed_market"
ROUTE_RANGE_ROTATED = "range_rotated_market"
ROUTE_EDGE_RECLAIMED = "edge_reclaimed_market"
ROUTE_STRUCTURE_BROKEN_STRONG = "structure_broken_strong_market"
ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT = "structure_broken_weak_limit"

#: 事件 → (路由名, 人话标签, 计数键)。
#:
#: 这张表取代旧实现里"由事件标识切片拼路由名/计数键"的写法
#: (``"e%s_market" % event_id[1]``)。切片把**命名**与**字符串位置**绑死,
#: 一旦事件标识换成语义名,路由名立刻退化为 ``eR_market`` 这类噪声 ——
#: 显式表让"哪个事件走哪条通道、记到哪个桶"可被静态检查。
MARKET_ROUTES = {
    BREAKOUT_FAILED: (ROUTE_BREAKOUT_FAILED, "失败突破确认",
                      "breakout_failed_signals"),
    TREND_RESUMED: (ROUTE_TREND_RESUMED, "趋势恢复", "trend_resumed_signals"),
    RANGE_ROTATED: (ROUTE_RANGE_ROTATED, "区间回归", "range_rotated_signals"),
    EDGE_RECLAIMED: (ROUTE_EDGE_RECLAIMED, "边缘收复", "edge_reclaimed_signals"),
}

#: 各事件族的信号计数键(与路由解耦:同一事件可能因质量分层走不同路由)。
SIGNAL_COUNTERS = {
    BREAKOUT_ACCEPTED: "breakout_accepted_signals",
    LEVEL_REJECTED: "level_rejected_signals",
    STRUCTURE_BROKEN: "structure_broken_signals",
    FLIP_RETESTED: "flip_retested_signals",
}
SIGNAL_COUNTERS.update({k: v[2] for k, v in MARKET_ROUTES.items()})

ENTRY_WHITELIST = frozenset(
    {(BREAKOUT_ACCEPTED, -1), (BREAKOUT_ACCEPTED, +1), (STRUCTURE_BROKEN, -1), (STRUCTURE_BROKEN, +1), (FLIP_RETESTED, -1), (FLIP_RETESTED, +1)})
#: 平衡箱双原语白名单(ACCEPT/REJECT 即突破接受/关键位拒绝,去重后恰 4 项)。
WHITELIST_BALANCE = frozenset(
    {(BREAKOUT_ACCEPTED, -1), (BREAKOUT_ACCEPTED, +1),
     (LEVEL_REJECTED, -1), (LEVEL_REJECTED, +1)})


@dataclass(frozen=True)
class RouterParams:
    """v6 路由参数(v6-protocol §2/§3;k_dq/k_imp/k_is 照抄 v5 冻结先验,
    不许上 test 择优;turtle_stop_atr = S2 臂 B 冻结口径,已固化为唯一语义)。"""

    k_dq: float = 0.5                # STRONG 破位位移下限(ATR 倍数,v5 冻结)
    k_imp: float = 1.5               # STRONG 破位冲动下限(破位bar振幅/ATR,v5 冻结)
    k_is: float = 0.8                # F1 失效距离下限(全入口,v2 冻结先验)
    turtle_stop_atr: float = 2.0     # 初始防线(ATR 倍数,S2 臂 B 冻结,固化)
    k_bm: float = 0.1                # 区缘外余量(= Params.k_bm,冻结先验)
    whitelist: frozenset = ENTRY_WHITELIST
    # v21.5:按周期差异化的谨慎克制过滤(用于 2h/30m 降噪:要求更高箱体质量分、
    # 旋转次数、触发 bar 冲动振幅或冷却期)
    min_box_touches: int = 0         # >0 时要求 zone.touches >= min_box_touches
    min_box_quality: float = 0.0     # >0 时要求 zone.box_quality >= min_box_quality
    min_box_rotations: int = 0       # >0 时要求 zone.box_rotations >= min_box_rotations
    min_box_width_atr: float = 0.0   # >0 时要求 zone.box_width_atr >= min_box_width_atr
    min_box_rr: float = 0.0          # >0 时要求 |box_opposite - close| / stop_dist >= min_box_rr
    min_bar_impulse_atr: float = 0.0 # >0 时要求触发 bar 振幅 (h-l)/atr >= min_bar_impulse_atr
    zone_cooldown_bars: int = 0      # >0 时同一条箱沿触发后冷却 N 根 bar 禁止重复刷单
    # v21.6:区分 ACCEPT(突破趋势 Turtle) 与 REJECT(箱体折返 结构收割/止损)
    reject_target_mode: str = "none" # "none" | "opposite" | "mid" (REJECT 的 target_1 锚点)
    reject_use_struct_stop: bool = False  # True 时 REJECT 使用箱体外缘结构止损(替代 2.0*ATR)
    accept_use_struct_stop: bool = False  # True 时 ACCEPT 使用箱体正缘结构止损(替代 2.0*ATR)


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
    """事件 → 路由决策;状态 = _pending(每战役至多一个未武装 weak episode)。"""

    def __init__(self, params: Optional[RouterParams] = None) -> None:
        self.params = params or RouterParams()
        self._pending: dict[str, _WeakEpisode] = {}
        self._notes: list[str] = []
        self.counters: dict[str, int] = {}
        self._zone_last_bar: dict[tuple, int] = {}
        self._cur_bar_range: float = 0.0
        self._cur_bar_index: int = 0

    # -- 主入口 ---------------------------------------------------------------

    def on_bar(self, symbol: str, bar, bar_index: int, view: StructureView,
               atr: float, new_events: Sequence[MarketEvent], *,
               position_open: bool, entry_resting: bool) -> list[RouteDecision]:
        """返回本 bar 的路由决策序列(无动作为空列表;explain() 读理由)。"""
        ts, _o, h, l, c, _v = _bar_parts(bar)
        self._notes = []
        self._cur_bar_range = float(h - l)
        self._cur_bar_index = int(bar_index)
        decisions: list[RouteDecision] = []
        atr = float(atr)

        wl = frozenset((canonical_event_id(eid), int(d)) for eid, d in self.params.whitelist)
        e1_fires = any(canonical_event_id(e.id) == BREAKOUT_ACCEPTED and (canonical_event_id(e.id), e.direction) in wl
                       for e in new_events)
        slot_freed = False
        if e1_fires:
            dropped = [k for k, ep in self._pending.items()
                       if k == symbol]
            for k in dropped:
                ep = self._pending.pop(k)
                self._notes.append(
                    f"e1_priority|{symbol} weak-STRUCTURE_BROKEN episode "
                    f"(dir={ep.direction}) dropped on BREAKOUT_ACCEPTED signal")
                self._bump("e1_drops_pending")
            if entry_resting and not position_open:
                decisions.append(RouteDecision(
                    kind="cancel_entry", route=ROUTE_BREAKOUT_ACCEPTED,
                    meta={"reason": "BREAKOUT_ACCEPTED signal cancels unfilled entry limit "
                                    "(BREAKOUT_ACCEPTED priority, v6§3 沿用 v5§1)"}))
                slot_freed = True
                self._bump("e1_cancel_orders")

        holding = (position_open or (entry_resting and not slot_freed))
        armed = False
        for ev in new_events:
            side = "long" if ev.direction > 0 else "short"
            cid = canonical_event_id(ev.id)
            if (cid, ev.direction) not in wl:
                self._notes.append(
                    f"nogo|{symbol} {ev.id}|{side} not in v6 whitelist")
                continue
            if cid in (STRUCTURE_BROKEN, BREAKOUT_ACCEPTED, FLIP_RETESTED):
                self._bump(SIGNAL_COUNTERS[cid])
            if holding or armed:
                self._notes.append(
                    f"dup|{symbol} {ev.id}|{side} slot busy (position or "
                    "resting entry; one position/one order per campaign)")
                self._bump("dup")
                continue
            if cid == STRUCTURE_BROKEN:
                decision = self._route_e6(symbol, ev, view, h, l, c, atr,
                                          bar_index)
            elif cid == BREAKOUT_ACCEPTED:
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_BREAKOUT_ACCEPTED,
                    "突破接受,永远市价次开盘")
            elif cid == LEVEL_REJECTED:
                # 关键位拒绝入场(2026-10-20 所有者决议开放):拒绝成因已明确
                # (区域刺穿与回退均已确认),市价次开盘;同一失效距离/空间闸。
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_LEVEL_REJECTED,
                    "拒绝确认,市价次开盘")
                self._bump(SIGNAL_COUNTERS[LEVEL_REJECTED])
            elif cid == FLIP_RETESTED:
                decision = self._route_e7(symbol, ev, view, c, atr, bar_index)
            elif cid in MARKET_ROUTES:
                # 语义路由表(2026-10-20):机制按各自修正语义上市价路由;
                # 失效价/目标价取事件属性,失效距离与空间几何闸不变。
                route, label, counter = MARKET_ROUTES[cid]
                decision = self._route_market(symbol, ev, view, c, atr,
                                              route, label + ",市价次开盘")
                self._bump(counter)
            else:
                self._notes.append(f"unknown_event|{symbol} {ev.id}")
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

    # -- 市价入口(BREAKOUT_ACCEPTED / STRONG-STRUCTURE_BROKEN) ----------------------------------------------

    def _route_market(self, symbol: str, ev: MarketEvent, view: StructureView,
                      close: float, atr: float, route: str,
                      tag: str) -> Optional[RouteDecision]:
        side = "long" if ev.direction > 0 else "short"
        hyp = self._gates(symbol, ev, view, close, atr, route)
        if hyp is None:
            return None
        reason = (f"{ev.id}|{side} {route} {tag} entry_ref={close:.8g} "
                  f"invalid={hyp.invalid:.8g} target_1={hyp.target_1} "
                  f"r_mult={hyp.r_mult}")
        return RouteDecision(
            kind="market", route=route,
            intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                               reason=reason, ref_price=float(close), ts=ev.ts),
            hyp=hyp, meta=_event_provenance(ev))

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
        if cid in (BREAKOUT_FAILED, LEVEL_REJECTED, EDGE_RECLAIMED) and p.reject_use_struct_stop and ev_inv is not None:
            invalid = float(ev_inv)
        elif cid == BREAKOUT_ACCEPTED and p.accept_use_struct_stop and ev_inv is not None:
            invalid = float(ev_inv)
        # F1(全入口,闸):天然距离不足即笔不存在
        dist = ev.direction * (close - invalid)
        if dist < p.k_is * atr:
            self._bump(f"f1_reject_{route}")
            self._notes.append(
                f"f1|{symbol} {ev.id}|{side} dist={dist:.4g} < k_is·atr="
                f"{p.k_is * atr:.4g} ⇒ 无交易(F1 全入口下限)")
            return None
        zone = ev.attrs.get("zone")
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
        if p.min_bar_impulse_atr > 0.0 and atr > 0.0:
            if (self._cur_bar_range / atr) < p.min_bar_impulse_atr:
                self._bump("restraint_impulse_reject")
                return None
        if p.min_box_rr > 0.0 and zone is not None:
            opp = getattr(zone, "box_opposite", None)
            if opp is not None and dist > 0.0:
                if cid in (BREAKOUT_FAILED, LEVEL_REJECTED, EDGE_RECLAIMED):
                    room = ev.direction * (float(opp) - close)
                else:
                    box_span = abs(float(opp) - (zone.hi if ev.direction > 0 else zone.lo))
                    room = box_span
                if (room / dist) < p.min_box_rr:
                    self._bump("restraint_rr_reject")
                    return None
        if p.zone_cooldown_bars > 0 and zone is not None:
            zkey = (symbol, round(float(zone.lo), 6), round(float(zone.hi), 6), ev.direction)
            last_b = self._zone_last_bar.get(zkey)
            if last_b is not None and (self._cur_bar_index - last_b) < p.zone_cooldown_bars:
                self._bump("restraint_cooldown_reject")
                return None
            self._zone_last_bar[zkey] = self._cur_bar_index
        target_1: Optional[float] = None
        r_mult: Optional[float] = None
        if cid in (BREAKOUT_FAILED, LEVEL_REJECTED, EDGE_RECLAIMED) and p.reject_target_mode != "none" and zone is not None:
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
        hyp = Hypothesis(event_id=ev.id, direction=ev.direction, ts=ev.ts,
                         known_index=ev.known_index, entry=float(close),
                         invalid=float(invalid), target_1=target_1,
                         r_mult=r_mult, regime=view.regime)
        return hyp

    # -- 内部 ------------------------------------------------------------------

    def _bump(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def explain(self) -> str:
        if not self._notes:
            return "flat|no evaluation yet"
        return "\n".join(self._notes)

    def pending_of(self, symbol: str) -> Optional[_WeakEpisode]:
        return self._pending.get(symbol)


__all__ = [
    "RouterParams", "MarketRouter", "RouteDecision", "ENTRY_WHITELIST", "WHITELIST_BALANCE",
    "ROUTE_BREAKOUT_ACCEPTED", "ROUTE_LEVEL_REJECTED", "ROUTE_BREAKOUT_ACCEPTED", "ROUTE_LEVEL_REJECTED",
    "ROUTE_STRUCTURE_BROKEN_STRONG", "ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT",
    "ROUTE_BREAKOUT_FAILED", "ROUTE_TREND_RESUMED", "ROUTE_RANGE_ROTATED", "ROUTE_EDGE_RECLAIMED",
]
