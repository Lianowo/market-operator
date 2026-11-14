"""基于【原子水平位 Level】与同周期波段结构的持仓管理状态机(回放与实盘唯一共享实现)。

四大纯粹结构持仓法则(零魔法时间止损、零生造比例补丁):
1. 初始结构证伪保护(Structural Invalidation Stop):
   收盘或保护防线越过 ``invalid`` ⇒ 立即全平离场(假设证伪)。
2. 突破失败快速离场(Failed Breakout Re-entry Exit):
   对于 ``BREAKOUT_ACCEPTED`` 开立的突破单,若收盘重新跌回被突破原子水平位背侧边界(``reenter_invalid_px``)之内,
   说明突破接受失败演化为诱多/诱空陷阱 ⇒ 不必死等最底端止损,当即离场。
3. 宏观关键起点反扑离场(Origin CHoCH Exit):
   当同周期出现反向 ``STRUCTURE_BROKEN``(发起本段趋势的 ``ORIGIN`` 水平位被反向实体击穿翻转) ⇒ 全平离场。
4. 对手方水平位兑现与结构移动防线(Opposing Level Partial + Breakeven + Swing Trail):
   - 盘中触及顺势前方对手方原子水平位 ``target_1`` 时:立即市价兑现 50% 仓位落袋(``PARTIAL_TARGET``),
     并将剩余 50% 仓位的保护防线移至保本价 ``entry``(实现零风险持盈);
   - 全程随同周期新因果确认的顺势波段拐点(多头 Higher Low / 空头 Lower High)将 ``invalid`` 单调推向
     新拐点极值外侧(只紧不松),让剩余仓位顺应真实市场结构吃尽大波段右尾。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .broker import OrderIntent, OrderKind
from .events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent, STRUCTURE_BROKEN
from .structure import Params, Pivot, PivotKind, _bar_parts

OPP_LOCK_EPS = 1e-12


def stop_protect_dist(direction: int, stop_px: float, ref_px: float) -> float:
    """止损相对现价的有向保护距离(> 0 表示止损位于现价的正确保护侧)。"""
    return -float(direction) * (float(stop_px) - float(ref_px))


@dataclass(frozen=True)
class LockStepResult:
    """对侧目标触及后的保护状态裁决结果(供实盘实时报价守护与回放统一调用)。"""

    action: str                     # "off" | "hold" | "tighten" | "exit"
    armed: bool
    stop_px: Optional[float] = None
    budget: float = 0.0
    drawdown: float = 0.0
    reason: str = ""


def opp_lock_step(
    *,
    direction: int,
    entry: float,
    target: float,
    frac: float,
    peak: Optional[float],
    high: float,
    low: float,
    close: float,
    ref_px: float,
    armed: bool,
    budget_override: Optional[float] = None,
) -> LockStepResult:
    """纯几何峰值回撤棘轮(仅当配置显式启用 frac != 0 时生效;frac == 0 时走纯保本+波段跟踪)。"""
    d = int(direction)
    span = d * (float(target) - float(entry))
    f = abs(float(frac))
    if f <= 0.0 or span <= 0.0 or not math.isfinite(span):
        return LockStepResult(action="off", armed=armed, reason="lock_disabled_or_degenerate")

    cur_ext = float(high) if d > 0 else float(low)
    best = cur_ext if peak is None else (max(float(peak), cur_ext) if d > 0 else min(float(peak), cur_ext))

    now_armed = bool(armed)
    if not now_armed:
        if frac < 0.0:
            now_armed = (float(close) >= float(target)) if d > 0 else (float(close) <= float(target))
        else:
            now_armed = (float(high) >= float(target)) if d > 0 else (float(low) <= float(target))
    if not now_armed:
        return LockStepResult(action="hold", armed=False, reason="not_armed")

    budget = (
        min(span, max(0.0, float(budget_override)))
        if budget_override is not None
        else max(0.0, (1.0 - f) * span)
    )
    drawdown = max(0.0, d * (best - float(ref_px)))
    stop_px = best - d * budget
    if stop_protect_dist(d, stop_px, ref_px) <= OPP_LOCK_EPS:
        return LockStepResult(
            action="exit", armed=True, stop_px=stop_px, budget=budget, drawdown=drawdown, reason="budget_exhausted"
        )
    return LockStepResult(
        action="tighten", armed=True, stop_px=stop_px, budget=budget, drawdown=drawdown, reason="ratchet"
    )


@dataclass
class Position:
    """单战役活跃持仓状态(回放与实盘持久化完全同构)。"""

    symbol: str
    direction: int
    entry: float
    invalid: float
    target_1: Optional[float]
    event_id: str
    opened_ts: int
    opened_index: int
    reduced: bool = False
    pending_reduce: bool = False
    size_frac: float = 1.0
    qty: float = 0.0
    armed: bool = False
    lock_off_logged: bool = False
    bars_held: int = 0
    reenter_invalid_px: Optional[float] = None
    claim_id: Optional[tuple] = None
    claim_completed_ts: Optional[int] = None
    claim_migrated_ts: Optional[int] = None
    log: list[str] = field(default_factory=list)
    _best: Optional[float] = None
    _entry0: float = 0.0
    _init_risk: float = 0.0


class PositionManager:
    """事件与结构驱动的持仓状态机(回放与实盘共享单一实现)。"""

    def __init__(
        self,
        params: Optional[Params] = None,
        *,
        allow_add: bool = False,
        time_stop: bool = False,
        breakeven_on_reduce: bool = True,
        partial_exit_enabled: bool = False,
        accept_reenter_level_exit: bool = True,
        accept_reenter_box_exit: Optional[bool] = None,
        reject_opp_lock_frac: float = 0.0,
    ) -> None:
        self.p = params or Params()
        self.allow_add = bool(allow_add)
        self.time_stop = bool(time_stop)
        self.breakeven_on_reduce = bool(breakeven_on_reduce)
        self.partial_exit_enabled = bool(partial_exit_enabled)
        self.accept_reenter_level_exit = (
            bool(accept_reenter_box_exit)
            if accept_reenter_box_exit is not None
            else bool(accept_reenter_level_exit)
        )
        self.reject_opp_lock_frac = float(reject_opp_lock_frac)
        self.positions: dict[str, Position] = {}

    def confirm_reduce(self, symbol: str, filled_frac: float) -> None:
        """确认交易所侧部分止盈成交回执:增量扣减剩余比例,达半仓后置 reduced 并移保本。"""
        pos = self.positions.get(symbol)
        if pos is None or filled_frac <= 0.0:
            return
        pos.size_frac = max(0.0, float(pos.size_frac) - float(filled_frac))
        if pos.size_frac <= 0.5 + 1e-9:
            pos.reduced = True
            pos.pending_reduce = False
            pos.armed = True
            if self.breakeven_on_reduce:
                pos.invalid = max(pos.invalid, pos.entry) if pos.direction > 0 else min(pos.invalid, pos.entry)

    def _resolve_lock_frac(self, _pos: Position, _atr: float = 0.0) -> float:
        return float(self.reject_opp_lock_frac)

    def open(
        self,
        symbol: str,
        direction: int,
        entry: float,
        invalid: float,
        target_1: Optional[float],
        event_id: str,
        opened_ts: int,
        opened_index: int,
        *,
        reenter_invalid_px: Optional[float] = None,
        claim_id: Optional[tuple] = None,
    ) -> Position:
        pos = Position(
            symbol=symbol,
            direction=int(direction),
            entry=float(entry),
            invalid=float(invalid),
            target_1=float(target_1) if target_1 is not None else None,
            event_id=str(event_id),
            opened_ts=int(opened_ts),
            opened_index=int(opened_index),
            reenter_invalid_px=float(reenter_invalid_px) if reenter_invalid_px is not None else None,
            claim_id=claim_id,
            _best=float(entry),
            _entry0=float(entry),
            _init_risk=abs(float(entry) - float(invalid)),
        )
        self.positions[symbol] = pos
        return pos

    def _tighten(self, pos: Position, cand_stop: float, ref_px: float, tag: str) -> None:
        d = pos.direction
        better = (cand_stop > pos.invalid) if d > 0 else (cand_stop < pos.invalid)
        if better and stop_protect_dist(d, cand_stop, ref_px) > OPP_LOCK_EPS:
            old = pos.invalid
            pos.invalid = float(cand_stop)
            pos.log.append(f"{tag}|{old:.8g}->{pos.invalid:.8g}")

    def _reduce(self, pos: Position, ts: int, ref_price: float, detail_val: float = 0.0) -> OrderIntent:
        pos.reduced = True
        pos.pending_reduce = False
        pos.size_frac = 0.5
        pos.armed = True
        if self.breakeven_on_reduce:
            self._tighten(pos, pos.entry, ref_price, "BREAKEVEN_ON_TARGET")
        return OrderIntent(
            kind=OrderKind.REDUCE,
            symbol=pos.symbol,
            qty_frac=0.5,
            reason=f"partial_exit|target={pos.target_1} ref={detail_val:.8g}",
            ref_price=float(ref_price),
            ts=int(ts),
        )

    def _exit(self, pos: Position, ts: int, ref_price: float, code: str, detail: str) -> OrderIntent:
        rem = 0.5 if pos.reduced else 1.0
        return OrderIntent(
            kind=OrderKind.EXIT,
            symbol=pos.symbol,
            qty_frac=rem,
            reason=f"{code}|{detail}",
            ref_price=float(ref_price),
            ts=int(ts),
        )

    def on_bar(
        self,
        symbol: str,
        bar: Sequence[float],
        atr: float,
        new_pivots: Sequence[Pivot] = (),
        new_events: Sequence[MarketEvent] = (),
        *,
        price_only: bool = False,
        skip_trail: bool = False,
    ) -> list[OrderIntent]:
        pos = self.positions.get(symbol)
        if pos is None:
            return []
        ts, _o, h, l, c, _v = _bar_parts(bar)
        d = pos.direction

        if pos._best is None:
            pos._best = h if d > 0 else l
        else:
            pos._best = max(pos._best, h) if d > 0 else min(pos._best, l)

        if not price_only:
            pos.bars_held += 1

        # 1. 结构失效位破位离场
        if (d > 0 and c <= pos.invalid) or (d < 0 and c >= pos.invalid):
            pos.log.append(f"EXIT_INVALID|close={c:.8g} vs invalid={pos.invalid:.8g}")
            return [self._exit(pos, ts, c, "INVALID_BREACHED", f"close={c:.8g} invalid={pos.invalid:.8g}")]

        if not price_only:
            # 2. 突破单重新跌回/升回被突破原子水平位背侧 -> 假突破快速离场
            if (
                self.accept_reenter_level_exit
                and pos.event_id == BREAKOUT_ACCEPTED
                and pos.reenter_invalid_px is not None
            ):
                re_px = pos.reenter_invalid_px
                if (d > 0 and c < re_px) or (d < 0 and c > re_px):
                    pos.log.append(f"EXIT_FAILED_BREAKOUT|close={c:.8g} reentered={re_px:.8g}")
                    return [self._exit(pos, ts, c, "FAILED_BREAKOUT_REENTRY", f"close={c:.8g} level_back={re_px:.8g}")]

            # 3. 盘中市场控制权反向夺权离场(宏观 ORIGIN 破坏 CHoCH 或对手方确认反向 LEVEL_REJECTED,绝不死扛初始止损!)
            for ev in new_events:
                if int(ev.direction) == -d:
                    if ev.id == STRUCTURE_BROKEN:
                        pos.log.append(f"EXIT_CHOCH|origin broken dir={ev.direction}")
                        return [self._exit(pos, ts, c, "CHOCH_ORIGIN_BROKEN", f"ts={ts}")]
                    if ev.id == LEVEL_REJECTED:
                        pos.log.append(f"EXIT_OPPOSING_AUCTION|dir={ev.direction} close={c:.8g}")
                        return [self._exit(pos, ts, c, "OPPOSING_AUCTION_EXIT", f"ts={ts}")]

        intents: list[OrderIntent] = []

        # 4. 触及第一目标 target_1(对手方原子水平位或 2.5R 首段融资位): 兑现 50% 仓位 + 余仓移至保本
        if pos.target_1 is not None and d * (pos.target_1 - pos.entry) > 0.0:
            tgt = pos.target_1
            reached = (h >= tgt) if d > 0 else (l <= tgt)
            accepted_beyond = (c > tgt) if d > 0 else (c < tgt)
            if reached and pos.claim_completed_ts is None:
                pos.claim_completed_ts = ts
            if accepted_beyond and pos.claim_migrated_ts is None:
                pos.claim_migrated_ts = ts

            frac_cfg = self._resolve_lock_frac(pos, atr)
            if reached and not pos.reduced and self.partial_exit_enabled:
                intents.append(self._reduce(pos, ts, c, h if d > 0 else l))

            if frac_cfg != 0.0:
                ld = opp_lock_step(
                    direction=d,
                    entry=pos.entry,
                    target=tgt,
                    frac=frac_cfg,
                    peak=pos._best,
                    high=h,
                    low=l,
                    close=c,
                    ref_px=c,
                    armed=pos.armed,
                )
                if ld.armed and not pos.armed:
                    pos.armed = True
                    if not pos.reduced and self.partial_exit_enabled:
                        intents.append(self._reduce(pos, ts, c, pos._best or c))
                if ld.action == "exit":
                    pos.armed = True
                    pos.log.append(f"EXIT_LOCK_GIVEBACK|peak={pos._best:.8g} close={c:.8g}")
                    return [self._exit(pos, ts, c, "LOCK_EXIT", f"drawdown={ld.drawdown:.8g}")]
                if ld.action == "tighten" and ld.stop_px is not None:
                    self._tighten(pos, ld.stop_px, c, "OPP_LOCK_TRAIL")

        # 5. 同周期因果波段拐点移动止损(Higher Low / Lower High Structural Trail)
        if not skip_trail and new_pivots:
            pad = max(0.0, float(self.p.k_bm) * float(atr))
            for piv in new_pivots:
                if d > 0 and piv.kind is PivotKind.LOW:
                    self._tighten(pos, piv.price - pad, c, f"SWING_TRAIL_HL@{piv.forming_index}")
                elif d < 0 and piv.kind is PivotKind.HIGH:
                    self._tighten(pos, piv.price + pad, c, f"SWING_TRAIL_LH@{piv.forming_index}")

        return intents

    def on_price(
        self,
        symbol: str,
        *,
        ts: int,
        price: float,
        ref_price: Optional[float] = None,
        atr: float = 0.0,
    ) -> list[OrderIntent]:
        """实盘新鲜报价保护推进(与 on_bar 同源语义:触及对手方水平位减半保本 + 可选锁利棘轮)。"""
        pos = self.positions.get(symbol)
        if pos is None:
            return []
        px = float(price)
        ref = px if ref_price is None else float(ref_price)
        d = pos.direction
        pos._best = px if pos._best is None else (max(pos._best, px) if d > 0 else min(pos._best, px))

        if pos.target_1 is None or d * (pos.target_1 - pos.entry) <= 0.0:
            return []
        tgt = pos.target_1
        reached = (px >= tgt) if d > 0 else (px <= tgt)
        if reached and pos.claim_completed_ts is None:
            pos.claim_completed_ts = int(ts)

        intents: list[OrderIntent] = []
        if reached and not pos.reduced and not pos.pending_reduce and self.partial_exit_enabled:
            intents.append(self._reduce(pos, int(ts), ref, px))
        elif pos.reduced and self.breakeven_on_reduce:
            self._tighten(pos, pos.entry, ref, "BREAKEVEN_ON_TARGET")

        frac_cfg = self._resolve_lock_frac(pos, atr)
        if frac_cfg > 0.0 or (frac_cfg < 0.0 and pos.armed):
            ld = opp_lock_step(
                direction=d,
                entry=pos.entry,
                target=tgt,
                frac=frac_cfg,
                peak=pos._best,
                high=px,
                low=px,
                close=px,
                ref_px=ref,
                armed=pos.armed,
            )
            if ld.armed and not pos.armed:
                pos.armed = True
            if ld.action == "exit":
                pos.armed = True
                return [self._exit(pos, int(ts), ref, "LOCK_EXIT", f"drawdown={ld.drawdown:.8g}")]
            if ld.action == "tighten" and ld.stop_px is not None:
                self._tighten(pos, ld.stop_px, ref, "OPP_LOCK_TRAIL")
        return intents



