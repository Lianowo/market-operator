"""市场结构认知核心:二维 (t, P) 空间层析水平位地图 + 零参数拓扑波段状态(Regime)。

核心纪律:
1. 严格因果知时:每根 K 线收盘时仅将已知历史序列 ``bars[:t+1]`` 投射到二维画布 ``Canvas2DLevelScanner``,
   绝不读取任何未来 K 线。
2. 零魔法参数、零特定 K 线拟合:
   - 【原子水平位 Level】由二维留白统治面积 + 跨时间波峰/波谷水平射线共线共振 + 近期水平视线反 POC 层析给出;
   - 波段移动止损端点(``Pivot``)与市场道氏状态(``Regime``)由零参数一阶/二阶拓扑摆动给出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Union

from .level import (
    Canvas2DLevelScanner,
    Level,
    LevelKind,
    LevelRole,
    LevelState,
    nearest_opposing_level,
)

Zone = Level
ZoneRole = LevelRole
ZoneState = LevelState


def _key_id(key: str) -> str:
    return key.replace("_", "").lower()


def _bar_parts(bar: Sequence[float]) -> tuple[int, float, float, float, float, float]:
    return (
        int(bar[0]),
        float(bar[1]),
        float(bar[2]),
        float(bar[3]),
        float(bar[4]),
        float(bar[5]) if len(bar) > 5 else 0.0,
    )


@dataclass(frozen=True)
class Params:
    """客观结构配置(二维画布视窗宽度、每侧最大保留层级与 Wilder 波动度量周期)。"""

    atr_n: int = 14
    canvas_bars: int = 160
    swing_wing: int = 3
    max_active_levels_per_side: int = 8
    range_max_pivots: int = 8
    k_bm: float = 0.10

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "Params":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        keys_by_id: dict[str, list[str]] = {}
        for key in raw:
            keys_by_id.setdefault(_key_id(key), []).append(key)
        wanted: dict = {}
        for name in cls.__dataclass_fields__:
            hits = keys_by_id.get(_key_id(name), [])
            if not hits:
                continue
            wanted[name] = raw[hits[0]]
        for k in ("atr_n", "canvas_bars", "swing_wing", "max_active_levels_per_side", "range_max_pivots"):
            if k in wanted:
                wanted[k] = int(wanted[k])
        if "k_bm" in wanted:
            wanted["k_bm"] = float(wanted["k_bm"])
        return cls(**wanted)


class PivotKind(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class Pivot:
    price: float
    forming_index: int
    known_index: int
    kind: PivotKind
    neck_price: float = 0.0
    close_extreme: float = 0.0
    origin_ts: int = 0


@dataclass
class _MinorSwing:
    forming_index: int
    price: float
    neck: float
    close_extreme: float
    origin_ts: int
    promoted: bool = False


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    TRANSITION = "transition"


class MarketStructure:
    """逐收盘 K 线因果推进的二维 (t, P) 空间层析结构地图。"""

    def __init__(self, params: Optional[Params] = None) -> None:
        self.params = params or Params()
        self.bars: list[tuple[int, float, float, float, float, float]] = []
        self._pivots: list[Pivot] = []
        self._scanner = Canvas2DLevelScanner(
            canvas_bars=self.params.canvas_bars,
            max_active_per_side=self.params.max_active_levels_per_side,
        )
        self._atr_val: float = 0.0
        self._tr_sum: float = 0.0
        self._dv_ema: float = 0.0
        self._dir: int = 0
        self._ext_idx: int = 0
        self._ext_h: float = 0.0
        self._ext_l: float = 0.0
        self._minor_highs: list[_MinorSwing] = []
        self._minor_lows: list[_MinorSwing] = []
        self.last_transitions: list[tuple[str, Level, Optional[Level]]] = []
        self.last_new_pivots: list[Pivot] = []

    @property
    def bars_seen(self) -> int:
        return len(self.bars)

    def atr(self) -> float:
        return self._atr_val

    @property
    def pivots(self) -> tuple[Pivot, ...]:
        return tuple(self._pivots)

    @property
    def pivot_count(self) -> int:
        return len(self._pivots)

    def pivot_slice(self, start: int) -> tuple[Pivot, ...]:
        return tuple(self._pivots[start:])

    @property
    def levels(self) -> tuple[Level, ...]:
        return self._scanner.active_levels

    @property
    def zones(self) -> tuple[Level, ...]:
        return self._scanner.active_levels

    def _record_pivot(self, ms: _MinorSwing, kind: PivotKind, known_index: int) -> None:
        if ms.promoted:
            return
        ms.promoted = True
        piv = Pivot(
            price=ms.price,
            forming_index=ms.forming_index,
            known_index=known_index,
            kind=kind,
            neck_price=ms.neck,
            close_extreme=ms.close_extreme,
            origin_ts=ms.origin_ts,
        )
        self._pivots.append(piv)
        self.last_new_pivots.append(piv)

    def update(self, bar: Sequence[float]) -> None:
        b = _bar_parts(bar)
        ts, o, h, l, c, v = b
        t = len(self.bars)
        prev_close = self.bars[-1][4] if t > 0 else o
        self.bars.append(b)

        dv = max(0.0, c * v)
        n = max(1, self.params.atr_n)
        self._dv_ema = dv if t == 0 else ((self._dv_ema * (n - 1)) + dv) / n

        tr = (h - l) if t == 0 else max(h - l, abs(h - prev_close), abs(l - prev_close))
        if t < n:
            self._tr_sum += tr
            self._atr_val = self._tr_sum / (t + 1)
        else:
            self._atr_val = ((self._atr_val * (n - 1)) + tr) / n

        self.last_transitions = self._scanner.update_canvas(self.bars, atr=self._atr_val)
        self.last_new_pivots = []

        if t == 0:
            self._ext_idx, self._ext_h, self._ext_l = 0, h, l
            return

        if self._dir == 0:
            eb = self.bars[self._ext_idx]
            if h > self._ext_h and c > eb[2]:
                self._dir, self._ext_idx, self._ext_h = 1, t, h
            elif l < self._ext_l and c < eb[3]:
                self._dir, self._ext_idx, self._ext_l = -1, t, l
            else:
                self._ext_h = max(self._ext_h, h)
                self._ext_l = min(self._ext_l, l)
            return

        if self._dir == 1:
            if h >= self._ext_h:
                self._ext_idx, self._ext_h = t, h
            eb = self.bars[self._ext_idx]
            eb_body_lo = min(eb[1], eb[4])
            broke_prev_ml = bool(self._minor_lows) and self._ext_idx > self._minor_lows[-1].forming_index and c < self._minor_lows[-1].price
            if (l < eb[3] and c < eb_body_lo) or broke_prev_ml:
                mh = _MinorSwing(self._ext_idx, eb[2], max(eb[1], eb[4]), eb[4], eb[0])
                self._minor_highs.append(mh)
                if len(self._minor_highs) >= 3:
                    mid = self._minor_highs[-2]
                    if mid.price >= self._minor_highs[-3].price and mid.price > self._minor_highs[-1].price:
                        self._record_pivot(mid, PivotKind.HIGH, t)
                self._dir, self._ext_idx, self._ext_l = -1, t, l
        else:
            if l <= self._ext_l:
                self._ext_idx, self._ext_l = t, l
            eb = self.bars[self._ext_idx]
            eb_body_hi = max(eb[1], eb[4])
            broke_prev_mh = bool(self._minor_highs) and self._ext_idx > self._minor_highs[-1].forming_index and c > self._minor_highs[-1].price
            if (h > eb[2] and c > eb_body_hi) or broke_prev_mh:
                ml = _MinorSwing(self._ext_idx, eb[3], min(eb[1], eb[4]), eb[4], eb[0])
                self._minor_lows.append(ml)
                if len(self._minor_lows) >= 3:
                    mid = self._minor_lows[-2]
                    if mid.price <= self._minor_lows[-3].price and mid.price < self._minor_lows[-1].price:
                        self._record_pivot(mid, PivotKind.LOW, t)
                self._dir, self._ext_idx, self._ext_h = 1, t, h

        if self._minor_highs and self._minor_lows:
            last_mh = self._minor_highs[-1]
            last_ml = self._minor_lows[-1]
            if last_mh.forming_index > last_ml.forming_index and c < last_ml.price:
                self._record_pivot(last_mh, PivotKind.HIGH, t)
            elif last_ml.forming_index > last_mh.forming_index and c > last_mh.price:
                self._record_pivot(last_ml, PivotKind.LOW, t)

    def regime(self) -> Regime:
        """直接返回二维时空画布直读的市场状态(零滞后取代旧一维道氏两高两低计数)。"""
        if not self.bars:
            return Regime.RANGE
        r2d = self._scanner.regime_2d
        if r2d == "trend_up":
            return Regime.TREND_UP
        if r2d == "trend_down":
            return Regime.TREND_DOWN
        if r2d == "transition":
            return Regime.TRANSITION
        return Regime.RANGE

    def macro_imbalance(self, _unused_room: float = 0.0) -> int:
        reg = self.regime()
        if reg is Regime.TREND_UP:
            return 1
        if reg is Regime.TREND_DOWN:
            return -1
        return 0

    def imbalance_strength(self) -> float:
        """返回纯 K 线内生导出的二维横截面相对强弱得分(正=多头领涨强度,负=空头领跌强度)。"""
        return float(self._scanner.imbalance_strength)

    def dollar_volume(self) -> float:
        """返回纯 K 线流(close * volume)因果滚动成交额,供多尺度流动性自适应准入使用。"""
        return float(self._dv_ema)

    def nearest_opposing(self, direction: int, ref_price: float,
                         exclude_uid: Optional[int] = None) -> Optional[Level]:
        return nearest_opposing_level(self._scanner.active_levels, direction,
                                      ref_price, exclude_uid=exclude_uid)


