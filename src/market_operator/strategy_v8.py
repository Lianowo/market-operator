"""v8 路由策略 —— EDGE_RECLAIMED EDGE_RECLAIM 边缘收复入场(docs/v8-protocol.md §1,冻结)。

规范地位:v8 研究轮(第 11 次 test 曝光,research-log #14 预注册)的策略实现,
在战役引擎上以 `router_factory=V8Router` 接插(campaign.run_campaigns 缝,
默认行为不变)。canonical 语义链(routing/position/broker)只读复用:
不 subclass、不改参数;RouteDecision/Hypothesis/计数器/explain 与 v6 同构接。

v8-protocol §1 入场几何(与 canonical turtle 的唯一差异,如实声明):
- **入场** = 次 1h 开盘市价(单执行风格唯一:market 次开盘,与 v6 桶可比;
  无限价/无质量路由/无 BREAKOUT_ACCEPTED 优先级 —— 白名单只含 EDGE_RECLAIMED,这些路径不可达)。
- **失效** = 事件 attrs["invalid"]:刺穿极值 ∓ k_bm·atr(结构锚,非 turtle
  2·atr;由 events.py 检测器于收复 bar 收盘知时写入,因果)。
- **F1 闸**(k_is=0.8,全入口,自 v2/v5/v6 同先验):dist =
  direction·(close − invalid) ≥ k_is·atr(取事件 bar ATR,attrs["atr"];与
  invalid 同一知时刻度)。天然距离不足即笔不存在,禁止人工加宽。
- **目标** = 对侧结构边缘:多 ⇒ band H*,空 ⇒ band L*(attrs["band"] =
  收复 bar 知时的 confirmed band,因果);room < r_min·risk 拒(r_min=1.5,
  §7 冻结先验经 v6.json 链沿用,非新调参)。
- **出场** = §8 全套沿用 canonical(持仓侧 PositionManager(breakeven_on_reduce
  =True, time_stop=False) 接线不变;turtle 收割语义同 canonical):§8.1 破位、
  §8.2 目标 50% 减仓(带缘前缘限价+超时转市价)、§8.3 pivot trailing 只紧不松、
  §8.5 反向 CHoCH;§8.4 加仓关闭、§8.6 时间止损关闭。
- 战役/槽位/组合容量/budget 与 canonical 完全一致(引擎承担,本模块只按
  "每战役一仓一挂单"槽位纪律判 gated-once)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .broker import OrderIntent, OrderKind
from .events import EDGE_RECLAIMED, MarketEvent
from .hypothesis import Hypothesis, StructureView
from .routing import RouteDecision, RouterParams
from .structure import _bar_parts

ROUTE_EDGE_RECLAIMED = "edge_reclaimed_market"               # EDGE_RECLAIMED 市价次 1h 开盘(协议§1 单执行风格)

WHITELIST_V8 = frozenset({(EDGE_RECLAIMED, -1), (EDGE_RECLAIMED, +1)})


@dataclass(frozen=True)
class V8Params(RouterParams):
    """v8 路由参数(docs/v8-protocol.md §1;k_is/r_min 冻结先验;whitelist 只含
    (EDGE_RECLAIMED,±1)。k_dq/k_imp/turtle_stop_atr 沿父类默认值但在 v8 路径不可达
    (无质量路由、无 turtle 防线 —— 失效取事件结构锚),如实声明。"""

    r_min: float = 1.5               # room ≥ r_min·risk(§7 先验,v8 §1 沿用)
    whitelist: frozenset = WHITELIST_V8


class V8Router:
    """事件 → 路由决策(白名单唯一分支 EDGE_RECLAIMED;接口与 MarketRouter 同构,供
    campaign.run_campaigns(router_factory=...) 接插)。"""

    def __init__(self, params: Optional[V8Params] = None) -> None:
        self.params = params or V8Params()
        self._notes: list[str] = []
        self.counters: dict[str, int] = {}

    # -- 主入口(v6 同构:whitelist → 槽位 → 闸门) -------------------------------

    def on_bar(self, symbol: str, bar, bar_index: int, view: StructureView,
               atr: float, new_events: Sequence[MarketEvent], *,
               position_open: bool, entry_resting: bool) -> list[RouteDecision]:
        """返回本 bar 的路由决策序列(无动作为空列表;explain() 读理由)。"""
        ts, _o, _h, _l, c, _v = _bar_parts(bar)
        self._notes = []
        decisions: list[RouteDecision] = []

        wl = self.params.whitelist
        holding = position_open or entry_resting
        armed = False
        for ev in new_events:
            side = "long" if ev.direction > 0 else "short"
            if (ev.id, ev.direction) not in wl:
                self._notes.append(
                    f"nogo|{symbol} {ev.id}|{side} not in v8 whitelist")
                continue
            self._bump("e8_signals")
            if holding or armed:
                self._notes.append(
                    f"dup|{symbol} EDGE_RECLAIMED|{side} slot busy (position or "
                    "resting entry; one position/one order per campaign)")
                self._bump("dup")
                continue
            decision = self._route_e8(symbol, ev, view, c)
            if decision is not None:
                decisions.append(decision)
                armed = True
        if not decisions and not new_events:
            self._notes.append(f"flat|{symbol} no new event")
        return decisions

    # -- EDGE_RECLAIMED:结构失效 + 带缘目标 + room/F1 闸(协议§1) ---------------------------

    def _route_e8(self, symbol: str, ev: MarketEvent, view: StructureView,
                  close: float) -> Optional[RouteDecision]:
        p = self.params
        side = "long" if ev.direction > 0 else "short"
        a = ev.attrs
        if view is None:
            raise RuntimeError(f"EDGE_RECLAIMED|{side} missing StructureView at "
                               f"{symbol} (event bar {ev.known_index})")
        invalid = a.get("invalid")
        band = a.get("band")
        atr = a.get("atr")
        if invalid is None or band is None or atr is None or float(atr) <= 0.0:
            self._bump("e8_bad_attrs")
            self._notes.append(
                f"e8_bad_attrs|{symbol} EDGE_RECLAIMED|{side} 缺 invalid/band/atr "
                "⇒ 无交易(检测器契约)")
            return None
        invalid = float(invalid)
        l_star, h_star = float(band[0]), float(band[1])
        risk = ev.direction * (close - invalid)
        if risk <= 0.0:
            self._bump("e8_bad_invalid")
            self._notes.append(
                f"e8_bad_invalid|{symbol} EDGE_RECLAIMED|{side} close={close:.8g} 已在 "
                f"失效外侧 invalid={invalid:.8g} ⇒ 无交易(结构矛盾)")
            return None
        # F1(闸,协议§1 沿用 k_is=0.8):天然距离不足即笔不存在
        if risk < p.k_is * float(atr):
            self._bump("f1_reject_e8_market")
            self._notes.append(
                f"f1|{symbol} EDGE_RECLAIMED|{side} dist={risk:.4g} < k_is·atr="
                f"{p.k_is * float(atr):.4g} ⇒ 无交易(F1 全入口下限)")
            return None
        target = h_star if ev.direction > 0 else l_star    # §1 对侧结构边缘
        room = ev.direction * (target - close)
        r_mult = room / risk
        if r_mult < p.r_min:
            self._bump("room_reject_e8_market")
            self._notes.append(
                f"room|{symbol} EDGE_RECLAIMED|{side} room={room:.4g} risk={risk:.4g} "
                f"r={r_mult:.4g} < r_min={p.r_min} ⇒ 无交易(对侧带缘空间不足)")
            return None
        self._bump("e8_entries")
        hyp = Hypothesis(event_id=ev.id, direction=ev.direction, ts=ev.ts,
                         known_index=ev.known_index, entry=float(close),
                         invalid=invalid, target_1=target, r_mult=r_mult,
                         regime=view.regime)
        reason = (f"EDGE_RECLAIMED|{side} {ROUTE_EDGE_RECLAIMED} 收复入场(刺穿极值失效/对侧带缘目标) "
                  f"entry_ref={close:.8g} invalid={invalid:.8g} "
                  f"target_1={target:.8g} r_mult={r_mult:.4g} "
                  f"pos={float(a.get('pos', float('nan'))):.4g} "
                  f"band=({l_star:.8g},{h_star:.8g})")
        return RouteDecision(
            kind="market", route=ROUTE_EDGE_RECLAIMED,
            intent=OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                               reason=reason, ref_price=float(close), ts=ev.ts),
            hyp=hyp,
            meta={"pos": a.get("pos"), "band": [l_star, h_star],
                  "puncture_index": a.get("puncture_index"), "ex": a.get("ex")})

    # -- 内部 ------------------------------------------------------------------

    def _bump(self, key: str) -> None:
        self.counters[key] = self.counters.get(key, 0) + 1

    def explain(self) -> str:
        if not self._notes:
            return "flat|no evaluation yet"
        return "\n".join(self._notes)


__all__ = ["V8Params", "V8Router", "ROUTE_EDGE_RECLAIMED", "WHITELIST_V8"]
