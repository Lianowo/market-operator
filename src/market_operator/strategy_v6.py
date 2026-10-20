"""v6 路由策略 —— 规范(canonical)策略语义:turtle 出口几何固化为唯一语义。

规范地位:本模块是规范语义链中"假设 → 交易计划"的唯一策略实现。语义
合并(refactor/semantics-unify)时,docs/v6-protocol.md §2-S2 的 A/B
二臂判选已经终局(reports/backtest_v6.json/.md 冻结产物:两臂 dev@1.5x
净期望均 ≤ 0,按冻结规则取 t 统计较好的臂 B,如实声明非转正证据),
**臂 B(turtle)固化为唯一策略语义**;臂切换(arm 参数)与臂 A
clamp2R(S1 区缘失效/room≥r_min 门/target 钳 2R)的实现自此移除。
历史 A/B 记录与协议文字保留于 docs/v6-protocol.md(未改动);双臂实现
与其测试冻结归档于 research/rounds/v6/(strategy_v6.py/campaign.py/
run_backtest_v6.py 及 tests/,对归档实现运行,可复现 A/B 判定)。

当前唯一语义(turtle,docs/v6-protocol.md §2-S2 臂 B 口径):
- 与 v5(docs/v5-protocol.md)的关系:E1-E7 检测零改动(events.py/
  structure.py 不动);v5 破位质量路由骨架全保留(E1 市价优先 / E6 质量
  路由 STRONG 市价、WEAK 登记 episode 待同 episode E7 前缘限价)。
- **双向**(v6-protocol §3):白名单默认 {(E1,E6,E7) × {short,long}},方向
  仅作机制桶标签不作挑选;槽位/episode 关联逻辑完全对称(E6|long margin
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

explain() 与 counters 语义同 v5;weaker 登记簿/E1 优先级语义同 v5。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .broker import OrderIntent, OrderKind
from .events import E1, E2, E3, E4, E5, E6, E7, E8, MarketEvent
from .hypothesis import Hypothesis, StructureView
from .structure import _bar_parts

# 同 episode 匹配容差:两侧价格同源(破位 pivot 价 / zone 前缘价,浮点原值
# 拷贝),容差只覆盖二进制表示差;非调参先验。
_REF_REL_TOL = 1e-9

ROUTE_E1 = "e1_market"               # E1 市价次开盘
ROUTE_E3 = "e3_market"               # E3 拒绝获因后市价次开盘(2026-10-20 放开决议,log#16)
ROUTE_E2 = "e2_market"
ROUTE_E4 = "e4_market"
ROUTE_E5 = "e5_market"
ROUTE_E8 = "e8_market"
ROUTE_STRONG_E6 = "strong_e6"        # STRONG E6 市价次开盘
ROUTE_WEAK_E7 = "weak_e7_limit"      # WEAK E6 → 同 episode E7 前缘限价

WHITELIST_V6 = frozenset(
    {(E1, -1), (E1, +1), (E6, -1), (E6, +1), (E7, -1), (E7, +1)})


@dataclass(frozen=True)
class V6Params:
    """v6 路由参数(v6-protocol §2/§3;k_dq/k_imp/k_is 照抄 v5 冻结先验,
    不许上 test 择优;turtle_stop_atr = S2 臂 B 冻结口径,已固化为唯一语义)。"""

    k_dq: float = 0.5                # STRONG 破位位移下限(ATR 倍数,v5 冻结)
    k_imp: float = 1.5               # STRONG 破位冲动下限(破位bar振幅/ATR,v5 冻结)
    k_is: float = 0.8                # F1 失效距离下限(全入口,v2 冻结先验)
    turtle_stop_atr: float = 2.0     # 初始防线(ATR 倍数,S2 臂 B 冻结,固化)
    k_bm: float = 0.1                # 区缘外余量(= Params.k_bm,冻结先验)
    whitelist: frozenset = WHITELIST_V6


@dataclass(frozen=True)
class RouteDecision:
    """路由器输出(执行语义由引擎承担:intent/hyp/限价与路由标签)。"""

    kind: str                        # "market" | "limit" | "cancel_entry"
    route: str                       # ROUTE_*(cancel_entry 亦归属触发路由 E1)
    intent: Optional[OrderIntent] = None
    hyp: Optional[Hypothesis] = None
    limit_price: Optional[float] = None     # kind="limit":前缘限价
    meta: dict = None                # 附加记录字段(如 weak_episode 追溯属性)

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})


@dataclass
class _WeakEpisode:
    """WEAK-E6 登记:破位未入场(弱),等待同 episode 的 E7(同向)。"""

    l_star: float                    # E6 attrs["ref"](被破 pivot 价)
    direction: int                   # 登记方向(同 episode E7 必须同向)
    bar_index: int                   # E6 事件 bar 索引
    margin: float                    # 登记时的质量量(记录字段)
    impulse: float


class V6Router:
    """事件 → 路由决策;状态 = _pending(每战役至多一个未武装 weak episode)。"""

    def __init__(self, params: Optional[V6Params] = None) -> None:
        self.params = params or V6Params()
        self._pending: dict[str, _WeakEpisode] = {}
        self._notes: list[str] = []
        self.counters: dict[str, int] = {}

    # -- 主入口 ---------------------------------------------------------------

    def on_bar(self, symbol: str, bar, bar_index: int, view: StructureView,
               atr: float, new_events: Sequence[MarketEvent], *,
               position_open: bool, entry_resting: bool) -> list[RouteDecision]:
        """返回本 bar 的路由决策序列(无动作为空列表;explain() 读理由)。"""
        ts, _o, h, l, c, _v = _bar_parts(bar)
        self._notes = []
        decisions: list[RouteDecision] = []
        atr = float(atr)

        wl = self.params.whitelist
        e1_fires = any(e.id == E1 and (e.id, e.direction) in wl
                       for e in new_events)
        slot_freed = False
        if e1_fires:
            dropped = [k for k, ep in self._pending.items()
                       if k == symbol]
            for k in dropped:
                ep = self._pending.pop(k)
                self._notes.append(
                    f"e1_priority|{symbol} weak-E6 episode "
                    f"(dir={ep.direction}) dropped on E1 signal")
                self._bump("e1_drops_pending")
            if entry_resting and not position_open:
                decisions.append(RouteDecision(
                    kind="cancel_entry", route=ROUTE_E1,
                    meta={"reason": "E1 signal cancels unfilled entry limit "
                                    "(E1 priority, v6§3 沿用 v5§1)"}))
                slot_freed = True
                self._bump("e1_cancel_orders")

        holding = (position_open or (entry_resting and not slot_freed))
        armed = False
        for ev in new_events:
            side = "long" if ev.direction > 0 else "short"
            if (ev.id, ev.direction) not in wl:
                self._notes.append(
                    f"nogo|{symbol} {ev.id}|{side} not in v6 whitelist")
                continue
            if ev.id == E6:
                self._bump("e6_signals")
            elif ev.id == E1:
                self._bump("e1_signals")
            elif ev.id == E7:
                self._bump("e7_signals")
            if holding or armed:
                self._notes.append(
                    f"dup|{symbol} {ev.id}|{side} slot busy (position or "
                    "resting entry; one position/one order per campaign)")
                self._bump("dup")
                continue
            if ev.id == E6:
                decision = self._route_e6(symbol, ev, view, h, l, c, atr,
                                          bar_index)
            elif ev.id == E1:
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_E1, "突破接受,永远市价次开盘")
            elif ev.id == E3:
                # E3 入场于 2026-10-20 所有者放开决议开放(research-log #16):
                # 拒绝获因明确(区域刺穿/回退都已确认),市价次开盘;同一 F1/room 闸。
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_E3, "拒绝确认,市价次开盘")
                self._bump("e3_signals")
            elif ev.id == E7:
                decision = self._route_e7(symbol, ev, view, c, atr, bar_index)
            elif ev.id in (E2, E4, E5, E8):
                # 语义路由表(2026-10-20, log#21):无黑白名单,机制按各自
                # 修正语义上市价路由;失效/target 取 attrs,F1/room 几何闸不变。
                tag = {E2: "失败突破确认", E4: "趋势恢复", E5: "区间回归", E8: "边缘收复"}[ev.id]
                decision = self._route_market(symbol, ev, view, c, atr,
                                              "e%s_market" % ev.id[1], tag + ",市价次开盘")
                self._bump("e%s_signals" % ev.id[1].lower())
            else:
                self._notes.append(f"unknown_event|{symbol} {ev.id}")
            if decision is not None:
                decisions.append(decision)
                armed = True
        if not decisions and not new_events:
            self._notes.append(f"flat|{symbol} no new event")
        return decisions

    # -- E6:质量路由(双向;margin 镜像) -----------------------------------------

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
                f"不入场,登记 episode(ref={ref:.8g}),待同 episode E7")
            return None
        self._bump("e6_strong_quality")
        self._notes.append(
            f"strong_e6|{symbol} margin={margin:.4g} >= {self.params.k_dq}, "
            f"impulse={impulse:.4g} >= {self.params.k_imp} ⇒ 市价次开盘")
        return self._route_market(
            symbol, ev, view, c, atr, ROUTE_STRONG_E6,
            "STRONG 破位,市价次开盘")

    # -- 市价入口(E1 / STRONG-E6) ----------------------------------------------

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
            hyp=hyp)

    # -- WEAK-E6 → E7(同 episode 前缘限价,v4 G3 / v5 §1 语义,双向) -------------

    def _route_e7(self, symbol: str, ev: MarketEvent, view: StructureView,
                  close: float, atr: float, bar_index: int
                  ) -> Optional[RouteDecision]:
        pend = self._pending.get(symbol)
        if pend is None or pend.direction != ev.direction:
            self._bump("e7_no_episode")
            self._notes.append(
                f"e7_no_episode|{symbol} E7 without registered same-direction "
                "weak-E6 episode ⇒ 不交易(v6:E7 仅作弱破位回踩延续入场)")
            return None
        ref = float(ev.attrs["ref"])
        if abs(ref - pend.l_star) > _REF_REL_TOL * max(1.0, abs(pend.l_star)):
            self._bump("e7_ref_mismatch")
            self._notes.append(
                f"e7_ref_mismatch|{symbol} E7 ref={ref:.8g} != pending ref="
                f"{pend.l_star:.8g} ⇒ 非同 episode,登记保留")
            return None
        del self._pending[symbol]                     # 消费一次(zone 至多翻转一次)
        self._bump("e7_matched_episode")
        weak_bars = bar_index - pend.bar_index
        side = "long" if ev.direction > 0 else "short"
        hyp = self._gates(symbol, ev, view, close, atr, ROUTE_WEAK_E7)
        if hyp is None:
            return None
        zone = ev.attrs.get("zone")
        if zone is None:
            self._bump(f"no_zone_{ROUTE_WEAK_E7}")
            self._notes.append(
                f"no_zone|{symbol} E7|{side} limit requires zone front edge (G3)")
            return None
        limit = float(zone.lo if ev.direction < 0 else zone.hi)
        reason = (f"E7|{side} {ROUTE_WEAK_E7} weak-E6 同 episode 回踩拒绝入场 "
                  f"entry_ref={close:.8g} invalid={hyp.invalid:.8g} "
                  f"target_1={hyp.target_1} r_mult={hyp.r_mult} "
                  f"limit_front_edge={limit:.8g} l_star={pend.l_star:.8g} "
                  f"weak_bars={weak_bars}")
        return RouteDecision(
            kind="limit", route=ROUTE_WEAK_E7,
            intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                               reason=reason, ref_price=float(close), ts=ev.ts),
            hyp=hyp, limit_price=limit,
            meta={"l_star": pend.l_star, "weak_bars": weak_bars,
                  "e6_margin": pend.margin, "e6_impulse": pend.impulse})

    # -- 闸门(turtle 唯一语义:F1 + 2·atr 防线;无目标,不做 room/r_min) ---------

    def _gates(self, symbol: str, ev: MarketEvent, view: StructureView,
               close: float, atr: float, route: str) -> Optional[Hypothesis]:
        side = "long" if ev.direction > 0 else "short"
        p = self.params
        invalid = close - ev.direction * p.turtle_stop_atr * atr
        # F1(全入口,闸):天然距离不足即笔不存在
        dist = ev.direction * (close - invalid)
        if dist < p.k_is * atr:
            self._bump(f"f1_reject_{route}")
            self._notes.append(
                f"f1|{symbol} {ev.id}|{side} dist={dist:.4g} < k_is·atr="
                f"{p.k_is * atr:.4g} ⇒ 无交易(F1 全入口下限)")
            return None
        hyp = Hypothesis(event_id=ev.id, direction=ev.direction, ts=ev.ts,
                         known_index=ev.known_index, entry=float(close),
                         invalid=float(invalid), target_1=None,
                         r_mult=None, regime=view.regime)
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
    "V6Params", "V6Router", "RouteDecision", "WHITELIST_V6",
    "ROUTE_E1", "ROUTE_E3", "ROUTE_STRONG_E6", "ROUTE_WEAK_E7",
    "ROUTE_E2", "ROUTE_E4", "ROUTE_E5", "ROUTE_E8",
]
