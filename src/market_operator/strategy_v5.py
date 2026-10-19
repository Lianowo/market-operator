"""v5 破位质量路由策略(docs/v5-protocol.md §1,冻结于 2026-10-19)。

与 v1 Strategy 的关系:认知/事件 E1-E7 零改动(events.py 不动);G2/F1(k_is)
与 §7/r_min 门序同构复用 hypothesis.build / f1_invalid_distance_ok;变更的唯一
内容是**入场执行按破位质量路由**:

- 白名单 short-only:{(E1,-1),(E6,-1),(E7,-1)}(long 侧各轮增量期望均为负,
  无信息规则剔除,协议如实声明)。
- **E6|short(破位当下)**:质量量(仅入场时刻已知,取自事件 attrs 与事件 bar):
  margin = (l_star − 收盘)/atr(l_star = attrs["ref"]),impulse = 破位bar振幅/atr
  (= (high − low)/atr);STRONG ⇔ margin ≥ k_dq ∧ impulse ≥ k_imp ⇒ 市价次 1h 开盘;
  否则 WEAK ⇒ 不入场、不入场即不撤语义 —— 登记该 episode(l_star),让区域生命周期
  继续。
- **WEAK → E7 同 episode 关联(无检测器状态,策略侧按 zone 参考价追溯)**:其后
  E7|short 且 attrs["ref"] == 登记 l_star(浮点同一价格来源,1e-9 相对容差包住
  表示误差)⇒ 同 episode ⇒ 按 v4 G3 挂区域前缘限价(short ⇒ zone.lo,N_fill=12,
  引擎侧),超时 = 没有这笔交易;ref 不匹配/无登记 ⇒ 不交易(该 E7 不属于已观察
  弱破位,如实声明为一处近似:协议"同 episode"在零检测器改动下的可操作化,
  匹配事实 = 被破的最近 HL pivot 价恰为完成角色互换区域的破位前缘价)。
  每个已登记 episode 至多消费一次(zone PR 翻转至多一次,闸门拒绝同样消费)。
- **E1|short(突破接受)**:永远市价次 1h 开盘;E1 信号出现时该品种若有未成交
  入场挂单 ⇒ 先撤单(E1 优先级最高,引擎落 CANCEL/E1_PRIORITY),并清空未武装
  的 weak-E6 登记;E1 自身仍过全部闸门(F1/r_min/目标),闸门拒绝 = 无交易(撤单
  不回滚)。持仓中 E1 按 v1 dup 语义拒绝,不动 §8 减仓挂单(§8 与 v4 一致)。
- **全入口 F1(k_is)**:E1/E6 同样须 dist = direction·(close − invalid) ≥
  k_is·atr(atr 取事件 bar 的 ATR,= StructureView.atr,即认知缓存逐 bar ATR;
  E1/E6 attrs 不带 atr 属检测器既有输出,零改动);E7 的 F1 语义与 v4 完全一致
  (f1_invalid_distance_ok,attrs["atr"] 同一知时刻度)。闸门序:白名单 → dup/槽位
  → F1 → 假设构建 → 目标 → r_min,与 v1/v2/v4 同构。
- 槽位纪律(每品种一仓一挂单):持仓中或入场挂单中不再武装新信号(满仓/挂单中
  E6/E7 一律 dup 拒绝;只有 E1 撤挂单后可同 bar 入场)。weaker 登记簿按"最新
  weak-E6 覆盖旧登记"(新破位=最新活体结构信息,旧 episode 退役,如实声明)。

explain():与 v1 Strategy 同款逐条 "<code>|<detail>";counters 上报引擎。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .events import E1, E6, E7, MarketEvent
from .hypothesis import Hypothesis, StructureView, build, f1_invalid_distance_ok
from .strategy import OrderIntent, OrderKind
from .structure import _bar_parts

# 同 episode 匹配容差:两侧价格同源(破位 pivot 价 / zone 前缘价,浮点原值
# 拷贝),容差只覆盖二进制表示差;非调参先验。
_REF_REL_TOL = 1e-9

ROUTE_E1 = "e1_market"               # E1 市价次开盘
ROUTE_STRONG_E6 = "strong_e6"        # STRONG E6 市价次开盘
ROUTE_WEAK_E7 = "weak_e7_limit"      # WEAK E6 → 同 episode E7 前缘限价

WHITELIST_V5 = frozenset({(E1, -1), (E6, -1), (E7, -1)})


@dataclass(frozen=True)
class V5Params:
    """v5 路由参数(docs/v5-protocol.md §1:先验 k_dq=0.5、k_imp=1.5;k_is/r_min
    沿用冻结先验,不许上 test 择优)。"""

    k_dq: float = 0.5                # STRONG 破位位移下限(ATR 倍数)
    k_imp: float = 1.5               # STRONG 破位冲动下限(破位bar振幅/ATR)
    k_is: float = 0.8                # F1 失效距离下限(全入口,v2 冻结先验)
    r_min: float = 1.5               # §7 行动门槛(冻结先验)
    whitelist: frozenset = WHITELIST_V5


@dataclass(frozen=True)
class RouteDecision:
    """路由器输出(执行语义由引擎承担:intent/hyp/限价与路由标签)。"""

    kind: str                        # "market" | "limit" | "cancel_entry"
    route: str                       # ROUTE_*(cancel_entry 亦归属触发路由 E1)
    intent: Optional[OrderIntent] = None
    hyp: Optional[Hypothesis] = None
    limit_price: Optional[float] = None     # kind="limit":前缘限价(short=zone.lo)
    meta: dict = None                # 附加记录字段(如 weak_episode 追溯属性)

    def __post_init__(self):
        if self.meta is None:
            object.__setattr__(self, "meta", {})


@dataclass
class _WeakEpisode:
    """WEAK-E6 登记:破位未入场(弱),等待同 episode 的 E7|short。"""

    l_star: float                    # E6 attrs["ref"](被破的最近 HL pivot 价)
    bar_index: int                   # E6 事件 bar 索引
    margin: float                    # 登记时的质量量(记录字段)
    impulse: float


class V5Router:
    """事件 → 路由决策;状态 = _pending(每品种至多一个未武装 weak episode)。"""

    def __init__(self, params: Optional[V5Params] = None) -> None:
        self.params = params or V5Params()
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

        # E1 优先级:信号出现时该品种有未成交入场挂单 ⇒ 先撤单(持仓恒不与入场
        # 挂单共存,position_open 时无入场挂单可撤;§8 减仓限价不受影响——引擎
        # 只在 flat+挂单时收到本决策)。
        wl = self.params.whitelist
        e1_fires = any((e.id, e.direction) in wl and e.id == E1
                       for e in new_events)
        slot_freed = False
        if e1_fires:
            if self._pending.pop(symbol, None) is not None:
                self._notes.append(
                    f"e1_priority|{symbol} weak-E6 episode dropped on E1 signal")
                self._bump("e1_drops_pending")
            if entry_resting and not position_open:
                decisions.append(RouteDecision(
                    kind="cancel_entry", route=ROUTE_E1,
                    meta={"reason": "E1|short signal cancels unfilled entry "
                                    "limit (E1 priority, v5-protocol §1)"}))
                slot_freed = True
                self._bump("e1_cancel_orders")

        holding = (position_open or (entry_resting and not slot_freed))
        armed = False
        for ev in new_events:
            side = "long" if ev.direction > 0 else "short"
            if (ev.id, ev.direction) not in wl:
                self._notes.append(
                    f"nogo|{symbol} {ev.id}|{side} not in v5 whitelist "
                    "(short-only: E1, E6, E7)")
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
                    "resting entry; one position/one order per symbol)")
                self._bump("dup")
                continue
            if ev.id == E6:
                decision = self._route_e6(symbol, ev, view, h, l, c, atr,
                                          bar_index)
            elif ev.id == E1:
                decision = self._route_market(
                    symbol, ev, view, c, atr, ROUTE_E1, "explicit_atr",
                    "突破接受,永远市价次开盘")
            else:  # E7
                decision = self._route_e7(symbol, ev, view, c, atr, bar_index)
            if decision is not None:
                decisions.append(decision)
                armed = True
        if not decisions and not new_events:
            self._notes.append(f"flat|{symbol} no new event")
        return decisions

    # -- E6:质量路由 -----------------------------------------------------------

    def _route_e6(self, symbol: str, ev: MarketEvent, view: StructureView,
                  h: float, l: float, c: float, atr: float,
                  bar_index: int) -> Optional[RouteDecision]:
        l_star = float(ev.attrs["ref"])
        margin = (l_star - c) / atr if atr > 0.0 else float("nan")
        impulse = (h - l) / atr if atr > 0.0 else float("nan")
        strong = margin >= self.params.k_dq and impulse >= self.params.k_imp
        if not strong:
            if symbol in self._pending:
                self._bump("e6_replaced_pending")
            self._pending[symbol] = _WeakEpisode(
                l_star=l_star, bar_index=bar_index, margin=margin, impulse=impulse)
            self._bump("e6_weak_registered")
            self._notes.append(
                f"weak_e6|{symbol} margin={margin:.4g} < k_dq={self.params.k_dq}"
                f" or impulse={impulse:.4g} < k_imp={self.params.k_imp} ⇒ "
                f"不入场,登记 episode(l_star={l_star:.8g}),待同 episode E7")
            return None
        self._bump("e6_strong_quality")
        self._notes.append(
            f"strong_e6|{symbol} margin={margin:.4g} >= {self.params.k_dq}, "
            f"impulse={impulse:.4g} >= {self.params.k_imp} ⇒ 市价次 1h 开盘")
        return self._route_market(
            symbol, ev, view, c, atr, ROUTE_STRONG_E6,
            "explicit_atr", "STRONG 破位,市价次 1h 开盘")

    # -- 市价入口(E1 / STRONG-E6):全入口 F1 + §7 门 ---------------------------

    def _route_market(self, symbol: str, ev: MarketEvent, view: StructureView,
                      close: float, atr: float, route: str, f1_mode: str,
                      tag: str) -> Optional[RouteDecision]:
        side = "long" if ev.direction > 0 else "short"
        if not self._f1_ok(ev, close, atr, f1_mode):
            self._bump(f"f1_reject_{route}")
            self._notes.append(
                f"f1|{symbol} {ev.id}|{side} dist invalid < k_is="
                f"{self.params.k_is}·atr ⇒ 无交易(F1 全入口下限)")
            return None
        hyp = self._build_gates(symbol, ev, view, close, route)
        if hyp is None:
            return None
        reason = (f"{ev.id}|{side} {route} {tag} entry_ref={close:.8g} "
                  f"invalid={hyp.invalid:.8g} target_1={hyp.target_1:.8g} "
                  f"r_mult={hyp.r_mult:.3f}")
        return RouteDecision(
            kind="market", route=route,
            intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                               reason=reason, ref_price=float(close), ts=ev.ts),
            hyp=hyp)

    # -- WEAK-E6 → E7(同 episode 前缘限价,v4 G3 语义) ------------------------

    def _route_e7(self, symbol: str, ev: MarketEvent, view: StructureView,
                  close: float, atr: float, bar_index: int
                  ) -> Optional[RouteDecision]:
        pend = self._pending.get(symbol)
        if pend is None:
            self._bump("e7_no_episode")
            self._notes.append(
                f"e7_no_episode|{symbol} E7|short without registered weak-E6 "
                "episode ⇒ 不交易(v5:E7 仅作弱破位回踩延续入场)")
            return None
        ref = float(ev.attrs["ref"])
        if abs(ref - pend.l_star) > _REF_REL_TOL * max(1.0, abs(pend.l_star)):
            self._bump("e7_ref_mismatch")
            self._notes.append(
                f"e7_ref_mismatch|{symbol} E7 ref={ref:.8g} != pending l_star="
                f"{pend.l_star:.8g} ⇒ 非同 episode,登记保留")
            return None
        del self._pending[symbol]                     # 消费一次(zone 至多翻转一次)
        self._bump("e7_matched_episode")
        weak_bars = bar_index - pend.bar_index
        side = "long" if ev.direction > 0 else "short"
        if not f1_invalid_distance_ok(ev, close, self.params.k_is):
            self._bump(f"f1_reject_{ROUTE_WEAK_E7}")
            self._notes.append(
                f"f1|{symbol} E7|{side} matched episode rejected by F1(同 v4)")
            return None
        hyp = self._build_gates(symbol, ev, view, close, ROUTE_WEAK_E7)
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
                  f"target_1={hyp.target_1:.8g} r_mult={hyp.r_mult:.3f} "
                  f"limit_front_edge={limit:.8g} l_star={pend.l_star:.8g} "
                  f"weak_bars={weak_bars}")
        return RouteDecision(
            kind="limit", route=ROUTE_WEAK_E7,
            intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                               reason=reason, ref_price=float(close), ts=ev.ts),
            hyp=hyp, limit_price=limit,
            meta={"l_star": pend.l_star, "weak_bars": weak_bars,
                  "e6_margin": pend.margin, "e6_impulse": pend.impulse})

    # -- 闸门(F1 全入口 / §7 构建-目标-r_min) ----------------------------------

    def _f1_ok(self, ev: MarketEvent, close: float, atr: float, mode: str) -> bool:
        """F1:E1/E6 ⇒ attrs 无 atr,显式用事件 bar ATR(与 v4 E7 的 attrs atr
        同一知时刻度);mode="v4" 仅备一致性对照(等价但走冻结实现)。"""
        if mode == "v4":
            return bool(f1_invalid_distance_ok(ev, close, self.params.k_is))
        invalid = ev.attrs.get("invalid")
        if invalid is None or atr <= 0.0:
            return False
        dist = ev.direction * (float(close) - float(invalid))
        return dist >= self.params.k_is * float(atr)

    def _build_gates(self, symbol: str, ev: MarketEvent, view: StructureView,
                     close: float, route: str) -> Optional[Hypothesis]:
        side = "long" if ev.direction > 0 else "short"
        hyp = build(ev, view, None)
        if hyp is None:
            self._bump(f"no_hypo_{route}")
            self._notes.append(
                f"no_hypo|{symbol} {ev.id}|{side} attrs['invalid'] missing")
            return None
        hyp = hyp.with_entry(close)       # §7 entry 以决策 bar 收盘近似(同 v1/v4)
        if hyp.target_1 is None:
            self._bump(f"no_target_{route}")
            self._notes.append(
                f"no_target|{symbol} {ev.id}|{side} 无结构目标,不交易")
            return None
        if hyp.r_mult is None or hyp.r_mult < self.params.r_min:
            r = "None" if hyp.r_mult is None else f"{hyp.r_mult:.3f}"
            self._bump(f"rmin_reject_{route}")
            self._notes.append(
                f"r_min|{symbol} {ev.id}|{side} r_mult={r} < "
                f"r_min={self.params.r_min}")
            return None
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
    "V5Params", "V5Router", "RouteDecision", "WHITELIST_V5",
    "ROUTE_E1", "ROUTE_STRONG_E6", "ROUTE_WEAK_E7",
]
