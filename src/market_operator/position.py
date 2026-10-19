"""持仓管理状态机(docs/methodology.md §8 六条,全部事件驱动、逐 bar 调用)。

每 bar 对持仓评估一次,规则优先级 §8.1 > §8.5 > §8.2 > §8.3 > §8.6
(出场类规则先到先判,同一 bar 不重复出场;§8.2 减仓与 §8.3 移动防线可
同 bar 并存,§8.2 在前):

1. §8.1 破位退出:收盘越过 invalid ⇒ EXIT 剩余全部。
2. §8.5 反向 E6:新事件中存在方向与持仓相反的 E6 ⇒ EXIT 剩余全部(CHoCH)。
3. §8.2 target_1:bar 极值触及目标 ⇒ REDUCE 当前仓 50%(仅一次,§9.1 的
   target 为结构区域近缘),余仓 invalid 移至 entry(保本,只收紧不放松)。
4. §8.3 移动防线:每新确认的逆向 pivot(空仓=pivot high,多仓=pivot low)
   ⇒ invalid 收紧至 pivot 外侧 ± k_bm·atr(只许向有利方向移动,永不放松;
   防线调整无订单,记录于 Position.log)。
5. §8.4 加仓:v1 关闭(PositionManager(allow_add=False),阶段 3 E4
   NO-GO ⇒ 策略层不出现加仓输入)。try_add 完整实现:同向新 E4 且浮盈
   ≥ 1R 时允许一次,合并后最坏情景损失不得劣于加仓前(风险中性约束),
   由独立测试验证。
6. §8.6 时间止损:bars_held 达到 n_time(§7 先验 N_time=24)时
   MFE < 0.5R ⇒ EXIT(MFE 以 bar 极值度量,R = |entry − 初始 invalid|)。

OrderIntent.qty_frac 约定:以该仓 OPEN 名义为基准 1.0;REDUCE=减仓部分
(0.5),EXIT=剩余全部(未减仓 1.0 / 减仓后 0.5),ADD=新增部分;账本可对账
(OPEN 1.0 − REDUCE 0.5 − EXIT 0.5 = 0)。权益占比换算属 sizing/记账层。

v6 增量(docs/v6-protocol.md §2-S2,加性开关,默认参数行为逐位不变):
- breakeven_on_reduce=False(臂 A "Target-Clamp"):§8.2 减仓成交后**不再**
  把 invalid 移至 entry(反模式实证禁令:保本移动),防线只随 §8.3 pivot
  收紧;默认 True ⇒ v1/v4/v5 逐位旧行为。
- time_stop=False(对照臂 B "Turtle"):§8.6 时间止损整条关闭;默认 True
  ⇒ v1/v4/v5 逐位旧行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .events import E4, E6, MarketEvent
from .hypothesis import Hypothesis
from .strategy import OrderIntent, OrderKind
from .structure import Params, Pivot, PivotKind, _bar_parts

MFE_MIN_R = 0.5          # §7 expiry 先验:N_time 根内 MFE < 0.5R
ADD_MIN_R = 1.0          # §8.4 先验:浮盈 ≥ 1R 才允许加仓


@dataclass
class Position:
    """持仓状态(§8 状态机的事实记录)。initial_risk 恒为开盘口径 R 基准,
    加仓不改变 R 度量。"""

    symbol: str
    direction: int                 # +1 多 / -1 空
    entry: float                   # 加权均价(加仓后更新)
    invalid: float                 # 当前结构失效价(只许向有利方向移动)
    target_1: Optional[float]
    event_id: str                  # 开仓机制(E1/E6)
    opened_ts: object
    opened_index: int
    initial_risk: float            # |entry_open − invalid_open|(R 基准,>0)
    size_frac: float = 1.0         # 剩余名义(以 OPEN 为 1.0)
    reduced: bool = False          # §8.2 仅在实际成交后为 True
    pending_reduce: bool = False   # v4:限价减仓待成交;不提前减量或移止损
    pending_add: bool = False      # future engine:加仓委托待成交,不提前滚仓
    pending_add_invalid: Optional[float] = None
    added: bool = False            # §8.4 加仓仅一次
    bars_held: int = 0             # 已收盘 bar 数(入场 bar 记第 1 根)
    _best: Optional[float] = None  # 入场以来最有利极值(空=min low,多=max high)
    log: list = field(default_factory=list)   # 无订单的决策记录(§8.3 防线等)

    @property
    def mfe_r(self) -> float:
        """最大有利行程,以初始 R 为单位(bar 极值口径,entry 取开盘口径)。"""
        return self.direction * (self._best - self._entry0) / self.initial_risk

    def __post_init__(self) -> None:
        if self.initial_risk <= 0.0:
            raise ValueError("initial_risk must be positive")
        if self._best is None:
            self._best = self.entry
        self._entry0 = self.entry   # 开盘口径均价(加仓不改变 R 度量基准)


class PositionManager:
    """§8 六条规则状态机;每 symbol 一个持仓,逐 bar 裁决。"""

    def __init__(self, params: Optional[Params] = None,
                 allow_add: bool = False,
                 breakeven_on_reduce: bool = True,
                 time_stop: bool = True) -> None:
        self.p = params or Params()
        self.allow_add = allow_add
        self.breakeven_on_reduce = breakeven_on_reduce  # v6 臂 A:False=禁保本
        self.time_stop = time_stop                      # v6 臂 B:False=关 §8.6
        self.positions: dict[str, Position] = {}
        self.last: list[str] = []            # 最近 on_bar 判定记录(解释性)

    # -- 建仓 ------------------------------------------------------------------

    def open(self, symbol: str, direction: int, entry: float, invalid: float,
             target_1: Optional[float], event_id: str, ts: object,
             index: int) -> Position:
        risk = direction * (entry - invalid)
        if risk <= 0.0:
            raise ValueError(f"invalid not protective: entry={entry} "
                             f"invalid={invalid} direction={direction}")
        pos = Position(symbol=symbol, direction=direction, entry=float(entry),
                       invalid=float(invalid), target_1=target_1,
                       event_id=event_id, opened_ts=ts, opened_index=index,
                       initial_risk=float(risk))
        self.positions[symbol] = pos
        return pos

    def open_from_hypothesis(self, symbol: str, hyp: Hypothesis, entry: float,
                             ts: object, index: int) -> Position:
        """由 §7 假设建仓(entry 为下一 bar 开盘成交价)。"""
        return self.open(symbol, hyp.direction, entry, hyp.invalid,
                         hyp.target_1, hyp.event_id, ts, index)

    # -- 逐 bar 裁决 -----------------------------------------------------------

    def on_bar(self, symbol: str, bar, atr: float,
               new_pivots: Sequence[Pivot] = (),
               new_events: Sequence[MarketEvent] = (),
               defer_reduce: bool = False,
               defer_add: bool = False) -> list[OrderIntent]:
        """按优先级 §8.1>§8.5>§8.2>§8.3>§8.6 评估;返回订单意图列表。"""
        pos = self.positions.get(symbol)
        if pos is None:
            return []
        self.last = []
        ts, o, h, l, c, _v = _bar_parts(bar)
        pos.bars_held += 1
        pos._best = min(pos._best, l) if pos.direction < 0 else max(pos._best, h)

        # 1. §8.1 破位退出(结构失效,收盘口径)
        if (c > pos.invalid) if pos.direction < 0 else (c < pos.invalid):
            return [self._exit(pos, ts, c,
                               f"§8.1|invalidation close close={c:.8g} "
                               f"invalid={pos.invalid:.8g} exit all")]

        # 2. §8.5 反向 E6(CHoCH 出场)
        for ev in new_events:
            if ev.id == E6 and ev.direction == -pos.direction:
                return [self._exit(pos, ts, c,
                                   f"§8.5|E6 reverse CHoCH event_ts={ev.ts} "
                                   f"exit all")]

        intents: list[OrderIntent] = []

        # 3. §8.2 target_1 触及:减仓 50%(仅一次),余仓 invalid 移至 entry
        if pos.target_1 is not None and not pos.reduced and not pos.pending_reduce:
            hit = (l <= pos.target_1) if pos.direction < 0 \
                else (h >= pos.target_1)
            if hit:
                if defer_reduce:
                    intents.append(self._defer_reduce(pos, ts, c, l if pos.direction < 0 else h))
                else:
                    intents.append(self._reduce(pos, ts, c, l if pos.direction < 0 else h))

        # 4. §8.3 移动防线:新确认逆向 pivot ⇒ invalid 收紧(永不放松)
        self._trail(pos, new_pivots, atr)

        # 5. §8.4 加仓(v1 关闭;逻辑保留并独立测试)
        if self.allow_add and not pos.added and not pos.pending_add:
            for ev in new_events:
                if ev.id == E4 and ev.direction == pos.direction:
                    intent = self.try_add(pos, ev, c, add_frac=pos.size_frac,
                                          ts=ts, defer=defer_add)
                    if intent is not None:
                        intents.append(intent)

        # 6. §8.6 时间止损:N_time 根内 MFE < 0.5R(v6 臂 B 整条关闭)
        if self.time_stop and pos.bars_held >= self.p.n_time \
                and pos.mfe_r < MFE_MIN_R:
            intents.append(self._exit(
                pos, ts, c,
                f"§8.6|time stop bars_held={pos.bars_held} >= "
                f"n_time={self.p.n_time} mfe={pos.mfe_r:.3f}R "
                f"< {MFE_MIN_R}R exit all"))
        return intents

    # -- 单条规则 ----------------------------------------------------------------

    def _exit(self, pos: Position, ts: object, ref: float,
              reason: str) -> OrderIntent:
        qty = pos.size_frac
        pos.size_frac = 0.0
        self.positions.pop(pos.symbol, None)
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.EXIT, symbol=pos.symbol,
                           qty_frac=qty, reason=reason, ref_price=float(ref),
                           ts=ts)

    def _defer_reduce(self, pos: Position, ts: object, ref: float,
                      extreme: float) -> OrderIntent:
        """限价委托只锁定一次减仓意图,未成交前仓位和失效位保持真实。"""
        if pos.pending_reduce or pos.reduced:
            raise AssertionError("duplicate deferred reduce")
        pos.pending_reduce = True
        qty = pos.size_frac * 0.5
        reason = (f"§8.2|target_1 hit reduce 50% target={pos.target_1:.8g} "
                  f"extreme={extreme:.8g} awaiting fill before breakeven")
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.REDUCE, symbol=pos.symbol,
                           qty_frac=qty, reason=reason, ref_price=float(ref), ts=ts)

    def confirm_reduce(self, symbol: str, actual_qty_frac: float) -> None:
        """经纪商实际成交后才减少仓位并收紧止损。数量以初始 OPEN 仓位为基准。"""
        pos = self.positions.get(symbol)
        if pos is None or not pos.pending_reduce:
            raise AssertionError(f"unexpected reduce fill without pending order: {symbol}")
        if not 0.0 < actual_qty_frac <= pos.size_frac:
            raise ValueError(f"invalid confirmed reduce fraction {actual_qty_frac}")
        pos.size_frac -= actual_qty_frac
        pos.pending_reduce = False
        pos.reduced = True
        old = pos.invalid
        if self.breakeven_on_reduce:
            pos.invalid = min(pos.invalid, pos.entry) if pos.direction < 0 \
                else max(pos.invalid, pos.entry)
        pos.log.append(f"§8.2|reduce FILLED qty_frac={actual_qty_frac:.8g} "
                       f"invalid {old:.8g}->{pos.invalid:.8g}")

    def cancel_reduce(self, symbol: str) -> None:
        """下单失败时撤销待成交标记;不凭未成交指令修改真实仓位。"""
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.pending_reduce = False

    def confirm_add(self, symbol: str, actual_qty_frac: float,
                    fill_price: float) -> None:
        """加仓成交确认:金额和实际成交价来自 broker,而不是决策时 close。"""
        pos = self.positions.get(symbol)
        if pos is None or not pos.pending_add:
            raise AssertionError(f"unexpected add fill without pending order: {symbol}")
        if actual_qty_frac <= 0.0 or fill_price <= 0.0:
            raise ValueError("invalid add fill")
        new_size = pos.size_frac + actual_qty_frac
        pos.entry = ((pos.size_frac * pos.entry +
                      actual_qty_frac * fill_price) / new_size)
        pos.size_frac = new_size
        proposed = pos.pending_add_invalid
        if proposed is not None:
            pos.invalid = (min(pos.invalid, proposed) if pos.direction < 0
                           else max(pos.invalid, proposed))
        pos.added = True
        pos.pending_add = False
        pos.pending_add_invalid = None
        pos.log.append(
            f"§8.4|ADD FILLED qty_frac={actual_qty_frac:.8g} "
            f"actual_price={fill_price:.8g} avg={pos.entry:.8g}")

    def cancel_add(self, symbol: str) -> None:
        """拒单/撤单不得创造一个已经加仓的持仓。"""
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.pending_add = False
            pos.pending_add_invalid = None

    def _reduce(self, pos: Position, ts: object, ref: float,
                extreme: float) -> OrderIntent:
        qty = pos.size_frac * 0.5
        pos.size_frac -= qty
        pos.reduced = True
        old = pos.invalid
        # 保本(§8.2 后半):invalid 移至 entry;只收紧不放松(已 trailing 更优则不动)。
        # v6 臂 A 禁令:breakeven_on_reduce=False ⇒ 保本移动整条关闭。
        if self.breakeven_on_reduce:
            if pos.direction < 0:
                pos.invalid = min(pos.invalid, pos.entry)
            else:
                pos.invalid = max(pos.invalid, pos.entry)
        reason = (f"§8.2|target_1 hit reduce 50% target={pos.target_1:.8g} "
                  f"extreme={extreme:.8g} breakeven invalid {old:.8g}"
                  f"->{pos.invalid:.8g}")
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.REDUCE, symbol=pos.symbol,
                           qty_frac=qty, reason=reason,
                           ref_price=float(ref), ts=ts)

    def _trail(self, pos: Position, new_pivots: Sequence[Pivot],
               atr: float) -> None:
        """§8.3:逆向 pivot(空=pivot high / 多=pivot low)外侧 ±k_bm·atr。"""
        if atr <= 0.0:
            return
        buf = self.p.k_bm * atr
        for pv in new_pivots:
            if pos.direction < 0 and pv.kind is PivotKind.HIGH:
                cand = pv.price + buf
                if cand < pos.invalid:                 # 只收紧
                    pos.log.append(
                        f"§8.3|trail new pivot high={pv.price:.8g} "
                        f"invalid {pos.invalid:.8g}->{cand:.8g}")
                    pos.invalid = cand
            elif pos.direction > 0 and pv.kind is PivotKind.LOW:
                cand = pv.price - buf
                if cand > pos.invalid:
                    pos.log.append(
                        f"§8.3|trail new pivot low={pv.price:.8g} "
                        f"invalid {pos.invalid:.8g}->{cand:.8g}")
                    pos.invalid = cand

    def try_add(self, pos: Position, event: MarketEvent, price: float,
                add_frac: float, ts: object = None,
                defer: bool = False) -> Optional[OrderIntent]:
        """§8.4 风险中性加仓(v1 由 allow_add 门控)。

        条件:同向 E4、浮盈 ≥ 1R、加仓仅一次。合并口径:合并仓位的失效价取
        min/max(现 invalid, E4 建议 invalid)(只收紧),合并均价按名义加权;
        风险中性约束 = 合并后最坏情景损失 ≤ 加仓前,不满足则拒绝(记录理由)。
        """
        if (event.id != E4 or event.direction != pos.direction or
                pos.added or pos.pending_add):
            return None
        if pos.mfe_r < ADD_MIN_R:
            self.last.append(
                f"§8.4|add rejected: mfe={pos.mfe_r:.3f}R < {ADD_MIN_R}R")
            return None
        d = pos.direction
        size = pos.size_frac
        cand = event.attrs.get("invalid")
        new_inv = pos.invalid
        if cand is not None:
            new_inv = min(pos.invalid, float(cand)) if d < 0 \
                else max(pos.invalid, float(cand))
        merged_size = size + add_frac
        avg = (size * pos.entry + add_frac * price) / merged_size
        worst_before = d * (pos.entry - pos.invalid) * size
        worst_after = d * (avg - new_inv) * merged_size
        if worst_after > worst_before + 1e-9:
            self.last.append(
                f"§8.4|add rejected: risk-neutral violated worst "
                f"{worst_before:.8g}->{worst_after:.8g}")
            return None
        if defer:
            pos.pending_add = True
            pos.pending_add_invalid = new_inv
        else:
            pos.entry = avg
            pos.size_frac = merged_size
            pos.invalid = new_inv
            pos.added = True
        reason = (f"§8.4|E4 add risk-neutral avg_entry={avg:.8g} "
                  f"invalid={new_inv:.8g} worst {worst_before:.8g}"
                  f"->{worst_after:.8g}")
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.ADD, symbol=pos.symbol,
                           qty_frac=add_frac, reason=reason,
                           ref_price=float(price), ts=ts)
