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
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from .events import E1, E6, MarketEvent
from .hypothesis import Hypothesis, StructureView, build
from .structure import _bar_parts

GO_MECHANISMS = frozenset({(E1, -1), (E6, -1)})


class OrderKind(str, Enum):
    OPEN = "OPEN"
    REDUCE = "REDUCE"
    ADD = "ADD"
    EXIT = "EXIT"


@dataclass(frozen=True)
class OrderIntent:
    """下单意图(与 broker 对接,字段固定)。qty_frac 为仓位名义额占权益的
    比例建议;OPEN 时为 None,由 sizing 模块决定。reason 为 "code|detail"。
    ref_price 为决策时点参考价(事件 bar 收盘;实际成交在下一 bar 开盘)。"""

    kind: OrderKind
    symbol: str
    qty_frac: Optional[float]
    reason: str
    ref_price: float
    ts: object


@dataclass(frozen=True)
class StrategyParams:
    r_min: float = 1.5                          # §7 行动门槛先验
    allow_add: bool = False                     # §8.4 v1 关闭(E4 NO-GO)
    max_open_positions: Optional[int] = None    # 风险预算上限(None=不封顶)


class Strategy:
    """事件驱动的 GO 白名单策略;状态 = _open(每 symbol 最多 1 仓)。"""

    def __init__(self, params: Optional[StrategyParams] = None) -> None:
        self.params = params or StrategyParams()
        self._open: set[str] = set()
        self._taken: dict[str, Hypothesis] = {}   # symbol → 已采纳假设(记录)
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
        if (ev.id, ev.direction) not in GO_MECHANISMS:
            if ev.direction >= 0:
                self._notes.append(
                    f"dir|{symbol} {ev.id}|long not in GO set (short-only v1)")
            else:
                self._notes.append(
                    f"nogo|{symbol} {ev.id}|short mechanism not GO "
                    f"(whitelist: E1|short, E6|short)")
            return None
        if holding:
            self._notes.append(
                f"dup|{symbol} {ev.id}|short GO but position already open, "
                f"v1 no add (allow_add={self.params.allow_add})")
            return None
        if (self.params.max_open_positions is not None
                and len(self._open) >= self.params.max_open_positions):
            self._notes.append(
                f"risk_budget|{symbol} {ev.id}|short open slots "
                f"{len(self._open)}/{self.params.max_open_positions} occupied")
            return None

        hyp = build(ev, view, None)
        if hyp is None:
            self._notes.append(
                f"no_hypo|{symbol} {ev.id}|short attrs['invalid'] missing")
            return None
        hyp = hyp.with_entry(close)       # §7 entry 以决策 bar 收盘近似
        if hyp.target_1 is None:
            self._notes.append(
                f"no_target|{symbol} {ev.id}|short no structural target "
                f"(no fixed-R fabrication, §7)")
            return None
        if hyp.r_mult is None or hyp.r_mult < self.params.r_min:
            r = "None" if hyp.r_mult is None else f"{hyp.r_mult:.3f}"
            self._notes.append(
                f"r_min|{symbol} {ev.id}|short r_mult={r} < "
                f"r_min={self.params.r_min}")
            return None

        self._taken[symbol] = hyp
        reason = (f"{ev.id}|short GO entry_ref={close:.8g} "
                  f"invalid={hyp.invalid:.8g} target_1={hyp.target_1:.8g} "
                  f"r_mult={hyp.r_mult:.3f}")
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
