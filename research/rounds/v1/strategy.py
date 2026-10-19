"""阶段 4 信号策略:事件 → §7 假设 → OrderIntent(FLAT 是一等输出)。

下单纪律(阶段 3 冻结结论,reports/event_study.md §1):GO 机制只有
E1|short 与 E6|short;全部做多机制与 E2/E3/E4 为 NO-GO,E5
INSUFFICIENT_DATA(§9.3:NO-GO 机制只保留研究记录,不进入下单策略)。
故 v1 只允许做空,入场白名单 GO_MECHANISMS = {(E1,-1), (E6,-1)};
§8.4 加仓机制(E4)NO-GO ⇒ 加仓规则 v1 实质禁用(allow_add=False,
逻辑保留在 position.PositionManager 并由独立测试验证)。

入场条件(全部满足,§7):
1. (event.id, event.direction) ∈ GO_MECHANISMS;
2. hypothesis.build 成功(attrs["invalid"] 存在)且 target_1 非 None
   (无结构目标不编造固定 R 目标,不交易);
3. r_mult ≥ r_min(§7 先验 1.5,与 config/defaults.json r_min 一致);
   决策在事件 bar 收盘做出,entry 以该收盘近似——§7 定义的"下一根 bar
   开盘价"在决策时点不可知,ref_price 记录该决策参考价;
4. 该 symbol 无持仓(每 symbol 同时最多 1 仓,记录在 _open;同 symbol
   新 GO 事件只记录去重,v1 无加仓);不同 symbol 相互独立;
5. 风险预算:已开仓位数 < max_open_positions(None = 不封顶,v1 默认)。

explain():每次 on_bar 后可读,逐条 "<code>|<detail>" 记录本 bar 判定:
flat(无事件且无仓)/hold(持仓监控中)/nogo(机制非 GO)/dir(做多方向)/
no_hypo(假设无法构建)/no_target(结构目标缺失)/r_min(r_mult 不足)/
dup(同 symbol 已有持仓去重)/risk_budget(风险预算占用)。

持仓时长管理(§8 六条)不在本模块:OPEN 之后由 position.PositionManager
逐 bar 裁决(破位/反向 E6/减仓/移动防线/加仓/时间止损)。

open_position_state 契约:调用方(回测/执行层)每 bar 传入该 symbol 当前
持仓对象(或 None);本模块只做 None 判定。传入 None 即视为该 symbol 已
平仓,解除 _open 锁(假定上一 bar 的 OPEN 意图已在下一 bar 成交)。

v2 增量(docs/v2-protocol.md §1,冻结于实现前):StrategyParams.whitelist
参数化入场白名单(None = v1 GO_MECHANISMS,v1 路径不变);k_is > 0 时启用
F1 失效距离下限(dist(close(t), invalid) ≥ k_is·atr(t),白名单内事件适用)。
v2 回测 whitelist = {(E7,-1),(E7,1)},k_is = 0.8(先验,不做择优)。

v4 增量(docs/v4-protocol.md §1,冻结于实现前):StrategyParams.entry_mode
= "limit_front_edge" 时启用 G3 限价接回踩 —— 采纳的假设直线限价取事件
attrs["zone"] 前缘(short:zone.lo;long:zone.hi,偏向成交率的方向,协议
如实声明),记录于 _taken_limit 供执行层挂单;默认 "market_next_open"
(v1/v2 路径逐 bar 行为不变)。G1 = 白名单,G2 = k_is F1(同款复用),
gates 顺序不变;每 symbol 一单纪律(开仓/挂单中拒绝新单)由执行层维持。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

# OrderIntent/OrderKind 在 v1 由本模块首定义;语义合并后规范家园为
# market_operator.broker(执行边界统一词表)。此处自规范家园导入并再导出,
# 保持本归档模块的历史 API(strategy.OrderIntent/OrderKind)与单一类型身份。
from market_operator.broker import OrderIntent, OrderKind  # noqa: F401
from market_operator.events import E1, E6, MarketEvent
from market_operator.hypothesis import Hypothesis, StructureView, build, f1_invalid_distance_ok
from market_operator.structure import _bar_parts

GO_MECHANISMS = frozenset({(E1, -1), (E6, -1)})   # v1 白名单(冻结,不改)


@dataclass(frozen=True)
class StrategyParams:
    r_min: float = 1.5                          # §7 行动门槛先验
    allow_add: bool = False                     # §8.4 v1 关闭(E4 NO-GO)
    max_open_positions: Optional[int] = None    # 风险预算上限(None=不封顶)
    # v2(docs/v2-protocol.md §1):whitelist = None ⇒ v1 GO_MECHANISMS(行为不变);
    # 显式集合则替代白名单(v2 回测 = {(E7,-1),(E7,1)},来自 config/v2.json)。
    whitelist: Optional[frozenset] = None
    k_is: float = 0.0                           # F1 失效距离下限先验倍率;0 = 不应用
    # v4(docs/v4-protocol.md §1-G3):"limit_front_edge" ⇒ 采纳时按事件 zone
    # 前缘记限价(short:zone.lo,long:zone.hi);默认 = v1/v2 次开盘市价,行为不变。
    entry_mode: str = "market_next_open"


class Strategy:
    """事件驱动的 GO 白名单策略;状态 = _open(每 symbol 最多 1 仓)。"""

    def __init__(self, params: Optional[StrategyParams] = None) -> None:
        self.params = params or StrategyParams()
        self._open: set[str] = set()
        self._taken: dict[str, Hypothesis] = {}      # symbol → 已采纳假设(记录)
        self._taken_limit: dict[str, float] = {}     # v4:symbol → G3 前缘限价
        self._notes: list[str] = []

    # -- 主入口 ---------------------------------------------------------------

    def on_bar(self, symbol: str, bar, view: StructureView,
               new_events: Sequence[MarketEvent],
               open_position_state: object = None) -> list[OrderIntent]:
        """返回本 bar 的下单意图(无动作为空列表;读 explain() 得理由)。"""
        ts, o, h, l, c, _v = _bar_parts(bar)
        self._notes = []

        if open_position_state is None and symbol in self._open:
            self._open.discard(symbol)
            self._taken.pop(symbol, None)
            self._taken_limit.pop(symbol, None)
            self._notes.append(f"flat|{symbol} prior position closed")

        holding = open_position_state is not None or symbol in self._open
        intents: list[OrderIntent] = []
        for ev in new_events:
            intent = self._evaluate(symbol, ev, view, c, ts, holding)
            if intent is not None:
                intents.append(intent)
                self._open.add(symbol)
                holding = True

        if not intents:
            if not new_events and not holding:
                self._notes.append(f"flat|{symbol} no new event, no position")
            elif holding and not new_events:
                self._notes.append(f"hold|{symbol} position open, monitoring")
        return intents

    def _evaluate(self, symbol: str, ev: MarketEvent, view: StructureView,
                  close: float, ts: object, holding: bool
                  ) -> Optional[OrderIntent]:
        side = "long" if ev.direction > 0 else "short"
        if self.params.whitelist is None:       # v1 路径(GO_MECHANISMS,冻结不变)
            if (ev.id, ev.direction) not in GO_MECHANISMS:
                if ev.direction >= 0:
                    self._notes.append(
                        f"dir|{symbol} {ev.id}|long not in GO set (short-only v1)")
                else:
                    self._notes.append(
                        f"nogo|{symbol} {ev.id}|short mechanism not GO "
                        f"(whitelist: E1|short, E6|short)")
                return None
        elif (ev.id, ev.direction) not in self.params.whitelist:
            self._notes.append(
                f"nogo|{symbol} {ev.id}|{side} not in configured whitelist "
                f"(v2 entry gate)")
            return None
        if holding:
            if self.params.whitelist is None:
                self._notes.append(
                    f"dup|{symbol} {ev.id}|short GO but position already open, "
                    f"v1 no add (allow_add={self.params.allow_add})")
            else:
                self._notes.append(
                    f"dup|{symbol} {ev.id}|{side} whitelisted but position "
                    f"already open, no add (allow_add={self.params.allow_add})")
            return None
        if (self.params.max_open_positions is not None
                and len(self._open) >= self.params.max_open_positions):
            self._notes.append(
                f"risk_budget|{symbol} {ev.id}|{side} open slots "
                f"{len(self._open)}/{self.params.max_open_positions} occupied")
            return None
        # F1 失效距离下限(docs/v2-protocol.md §1;k_is>0 时启用,v2 入场门)
        if self.params.k_is > 0.0 \
                and not f1_invalid_distance_ok(ev, close, self.params.k_is):
            inv0 = float(ev.attrs.get("invalid", close))
            atr0 = float(ev.attrs.get("atr", 0.0))
            dist = ev.direction * (close - inv0)
            self._notes.append(
                f"f1|{symbol} {ev.id}|{side} dist={dist:.4g} < "
                f"k_is={self.params.k_is}·atr={self.params.k_is * atr0:.4g} "
                f"(F1 invalid-distance floor, v2-protocol §1)")
            return None

        hyp = build(ev, view, None)
        if hyp is None:
            self._notes.append(
                f"no_hypo|{symbol} {ev.id}|{side} attrs['invalid'] missing")
            return None
        hyp = hyp.with_entry(close)       # §7 entry 以决策 bar 收盘近似
        if hyp.target_1 is None:
            self._notes.append(
                f"no_target|{symbol} {ev.id}|{side} no structural target "
                f"(no fixed-R fabrication, §7)")
            return None
        if hyp.r_mult is None or hyp.r_mult < self.params.r_min:
            r = "None" if hyp.r_mult is None else f"{hyp.r_mult:.3f}"
            self._notes.append(
                f"r_min|{symbol} {ev.id}|{side} r_mult={r} < "
                f"r_min={self.params.r_min}")
            return None

        self._taken[symbol] = hyp
        tag = "GO" if self.params.whitelist is None else "whitelist"
        reason = (f"{ev.id}|{side} {tag} entry_ref={close:.8g} "
                  f"invalid={hyp.invalid:.8g} target_1={hyp.target_1:.8g} "
                  f"r_mult={hyp.r_mult:.3f}")
        # v4 G3(docs/v4-protocol.md §1):限价接回踩取翻转区域前缘
        # (short ⇒ zone.lo,long ⇒ zone.hi);前缘缺失 = 信号不成熟 = 拒绝。
        if self.params.entry_mode == "limit_front_edge":
            zone = ev.attrs.get("zone")
            if zone is None:
                self._taken.pop(symbol, None)
                self._notes.append(
                    f"no_zone|{symbol} {ev.id}|{side} limit_front_edge requires "
                    f"zone front edge (G3)")
                return None
            limit = float(zone.lo if ev.direction < 0 else zone.hi)
            self._taken_limit[symbol] = limit
            reason += f" limit_front_edge={limit:.8g}"
        return OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                           reason=reason, ref_price=float(close), ts=ts)

    # -- 解释 -----------------------------------------------------------------

    def explain(self) -> str:
        """上一次 on_bar 的逐条判定记录(FLAT 及原因是一等输出)。"""
        if not self._notes:
            return "flat|no evaluation yet"
        return "\n".join(self._notes)

    @property
    def open_symbols(self) -> tuple:
        return tuple(sorted(self._open))
