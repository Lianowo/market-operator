"""v21.2 平衡箱模型(照抄 edge-engine zones.py 的平衡区哲学,保留最小接口)。

与单带 S/R 区(每个 pivot 造一个阻力/支撑带)的本质区别:
1. 只在"失败延伸"(未收盘突破前一个同类极值)时识别一个**箱** [下缘, 上缘];
2. 箱的角色由价格位置动态派生(下缘=支撑/上缘=阻力),不写死;
3. 生命周期:ACTIVE → BREACHED_UP/DOWN → LEFT(被新箱超越取代),外加年龄过期;
4. 取代(supersede)而非几何合并 —— 价格离开后旧箱退役,新箱另起,故箱间天然有 gap。

本模块只交付"箱的识别/生命周期/取代/过期"的可测实现,不含事件/假设层接线。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .structure import Params, Pivot, PivotKind


@dataclass
class BalanceBox:
    """一个平衡箱:失败延伸勾勒出的暂停区间,含上下两条边缘。"""

    lo: float                  # 下缘
    hi: float                  # 上缘
    state: str = "ACTIVE"      # ACTIVE / BREACHED_UP / BREACHED_DOWN / LEFT
    origin_known: int = 0
    last_change_known: int = 0
    touches: int = 0

    @property
    def height(self) -> float:
        return self.hi - self.lo

    @property
    def center(self) -> float:
        return (self.hi + self.lo) / 2.0


def same_balance(a: BalanceBox, b: BalanceBox, tol: float) -> bool:
    """edge-engine zones.py same_balance:共享覆盖>80%跨度 或 高度相近且包含。"""
    shared = min(a.hi, b.hi) - max(a.lo, b.lo)
    span = max(a.hi, b.hi) - min(a.lo, b.lo)
    if shared <= 0 or span <= 0:
        return False
    similar = min(a.height, b.height) > 0.8 * max(a.height, b.height)
    inside = ((b.lo - tol <= a.lo and a.hi <= b.hi + tol)
              or (a.lo - tol <= b.lo and b.hi <= a.hi + tol))
    return shared > 0.8 * span or (similar and inside)


class BalanceBook:
    """失败延伸识别 + 取代 + 年龄过期(逐 pivot 增量,known_index ≤ t)。"""

    def __init__(self, params: Params) -> None:
        self.p = params
        self.boxes: list[BalanceBox] = []
        self._last_high: Optional[Pivot] = None
        self._last_low: Optional[Pivot] = None

    def on_pivot(self, pivot: Pivot, known: int, atr_val: float) -> None:
        if atr_val <= 0:
            return
        tol = self.p.tol_atr * atr_val
        if pivot.kind is PivotKind.HIGH:
            failed = (self._last_high is not None
                      and pivot.close_extreme <= self._last_high.price + tol)
            if failed and self._last_low is not None:
                self._place(BalanceBox(lo=self._last_low.close_extreme, hi=pivot.close_extreme,
                                       origin_known=known, last_change_known=known),
                            known, tol)
            self._last_high = pivot
        else:
            failed = (self._last_low is not None
                      and pivot.close_extreme >= self._last_low.price - tol)
            if failed and self._last_high is not None:
                self._place(BalanceBox(lo=pivot.close_extreme, hi=self._last_high.close_extreme,
                                       origin_known=known, last_change_known=known),
                            known, tol)
            self._last_low = pivot

    def _place(self, new: BalanceBox, known: int, tol: float) -> None:
        # 1. 去重:同一平衡被再次读出
        for b in self.boxes:
            if b.state != "LEFT" and abs(b.hi - new.hi) <= tol and abs(b.lo - new.lo) <= tol:
                return
        # 2. 合并(grow):重叠且 ACTIVE/包含/强重叠 → 并集(edge-engine _place 同构)
        for b in self.boxes:
            if b.state == "LEFT":
                continue
            overlap = min(b.hi, new.hi) - max(b.lo, new.lo)
            if overlap <= 0:
                continue
            contained = ((new.lo - tol <= b.lo and b.hi <= new.hi + tol)
                         or (b.lo - tol <= new.lo and new.hi <= b.hi + tol))
            similar = min(b.height, new.height) > 0.8 * max(b.height, new.height)
            if contained and not similar:
                continue  # 嵌套(父/子),读时推导,不并
            if contained or b.state == "ACTIVE" or overlap >= 0.5 * min(b.height, new.height):
                b.lo = min(b.lo, new.lo)
                b.hi = max(b.hi, new.hi)
                b.last_change_known = known
                return
        # 3. 取代:被破位的旧箱,若新箱中心越过了它的破位侧,则退役(LEFT)
        for b in self.boxes:
            if b.state == "BREACHED_UP" and new.center > b.hi:
                b.state = "LEFT"
            elif b.state == "BREACHED_DOWN" and new.center < b.lo:
                b.state = "LEFT"
        # 4. 新建
        self.boxes.append(new)

    def on_bar(self, index: int, o: float, h: float, l: float, c: float,
               prev_close: Optional[float], atr_val: float) -> None:
        if atr_val <= 0:
            return
        bm = self.p.k_bm * atr_val
        for b in self.boxes:
            if b.state == "ACTIVE":
                if c > b.hi + bm:
                    b.state = "BREACHED_UP"
                    b.last_change_known = index
                elif c < b.lo - bm:
                    b.state = "BREACHED_DOWN"
                    b.last_change_known = index
        # 年龄过期:长时间未被触碰/变化的箱移出(久远无价值)
        if self.p.stale_after_bars > 0:
            self.boxes = [b for b in self.boxes
                          if index - b.last_change_known < self.p.stale_after_bars]

    def active(self) -> list[BalanceBox]:
        """未退役(非 LEFT)的箱 = 当前结构阶梯。"""
        return [b for b in self.boxes if b.state != "LEFT"]
