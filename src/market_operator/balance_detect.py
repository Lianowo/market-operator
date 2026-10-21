"""框子识别(拍卖市场理论):平衡区(区间)与趋势(迁移)的识别。

市场是叠框子的运动:平衡区(框子)= 拍卖失衡失败的价格区间(来回、收盘重叠、
方向效率低);趋势(迁移)= 价格从一个框子移到下一个框子。框子上下沿即支撑/阻力。

本模块对每个回看窗口按六维打分,分数高=平衡(框子)。独立可测,无交易/事件接线。

相对 edge-engine candidates.py 的优化:
- 前缀和 O(1) 路径/位移/中心稳定性(均值),免逐窗排序;
- 稳健分位数边界(12%/88%),单根影线尖刺不拉宽箱体;
- 砍掉 lineage/incubation/beam 状态机(约 1500 行),只留 打分 + 去重抑制;
- 边界只对窗口 high/low 排序一次,其余指标全 O(length) 或 O(1)。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Sequence


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _quantile(vals: Sequence[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (pos - lo)) + s[hi] * (pos - lo)


@dataclass(frozen=True)
class BalanceParams:
    ladder: tuple = (8, 13, 21, 34, 55, 89, 144)
    eff_max: float = 0.55
    width_min_atr: float = 0.8
    width_max_atr: float = 12.0
    band_in_atr: float = 0.35
    quality_min: float = 0.25
    swing_near_atr: float = 0.75
    w_efficiency: float = 0.18
    w_occupancy: float = 0.16
    w_overlap: float = 0.12
    w_traversal: float = 0.16
    w_balance: float = 0.14
    w_edge: float = 0.14
    w_stability: float = 0.10


@dataclass
class BalanceBox:
    lo: float
    hi: float
    quality: float
    start_index: int
    end_index: int
    width_atr: float = 0.0
    efficiency: float = 0.0
    dwell: float = 0.0
    rotations: int = 0
    components: dict = field(default_factory=dict)

    @property
    def center(self) -> float:
        return (self.hi + self.lo) / 2.0

    @property
    def height(self) -> float:
        return self.hi - self.lo


class BalanceDetector:
    """逐 bar 增量识别平衡箱(区间)。"""

    def __init__(self, params: Optional[BalanceParams] = None) -> None:
        self.p = params or BalanceParams()
        self.bars: list = []
        self.units: list = []
        self.path_prefix: list = [0.0]
        self.close_prefix: list = [0.0]

    def push(self, bar, unit: float) -> None:
        ts, o, h, l, c, v = bar
        idx = len(self.bars)
        self.bars.append((ts, o, h, l, c, v))
        self.units.append(unit)
        prev_close = self.bars[-2][4] if idx else o
        self.path_prefix.append(self.path_prefix[-1] + abs(c - prev_close))
        self.close_prefix.append(self.close_prefix[-1] + c)

    def score(self) -> list[BalanceBox]:
        n = len(self.bars)
        out: list[BalanceBox] = []
        for length in self.p.ladder:
            if n < length:
                break
            box = self._score_window(n - length, length, self.units[-1])
            if box is not None and box.quality >= self.p.quality_min:
                out.append(box)
        return self._suppress(out)

    def _score_window(self, first: int, length: int, unit: float) -> Optional[BalanceBox]:
        if unit <= 0:
            return None
        bars = self.bars[first:first + length]
        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]

        upper = _quantile(highs, 0.88)      # 稳健边界:影线尖刺不拉宽
        lower = _quantile(lows, 0.12)
        width = upper - lower
        width_atr = width / unit
        if width < self.p.width_min_atr * unit or width_atr > self.p.width_max_atr:
            return None

        path = self.path_prefix[first + length] - self.path_prefix[first]
        displacement = abs(closes[-1] - closes[0])
        efficiency = displacement / path if path > 0 else 1.0

        eps = unit * 1e-10
        dwell = sum(1 for c in closes if lower - eps <= c <= upper + eps) / length

        occ = 0.0
        for b in bars:
            bl, bh = sorted((b[1], b[4]))
            body = bh - bl
            if body <= 0:
                occ += float(lower - eps <= b[4] <= upper + eps)
            else:
                occ += _clamp(max(0.0, min(bh, upper) - max(bl, lower)) / body)
        occupancy = occ / length

        adj = 0.0
        for a, b in zip(bars, bars[1:]):
            ov = max(0.0, min(a[2], b[2]) - max(a[3], b[3]))
            den = max(min(a[2] - a[3], b[2] - b[3]), 1e-12)
            adj += _clamp(ov / den)
        adjacent_overlap = adj / (length - 1) if length > 1 else 0.0

        mid_i = first + length // 2
        h1 = self.close_prefix[mid_i] - self.close_prefix[first]
        h2 = self.close_prefix[first + length] - self.close_prefix[mid_i]
        n1, n2 = mid_i - first, first + length - mid_i
        stability_move = abs(h1 / n1 - h2 / n2) if n1 and n2 else 0.0

        edge_depth = max(self.p.band_in_atr * unit, 0.10 * width)
        touches: list[float] = []
        seq: list[str] = []
        for b in bars:
            tt = abs(b[2] - upper) <= edge_depth
            lt = abs(b[3] - lower) <= edge_depth
            if tt:
                touches.append(abs(b[2] - upper))
            if lt:
                touches.append(abs(b[3] - lower))
            if tt and (not seq or seq[-1] != "T"):
                seq.append("T")
            if lt and (not seq or seq[-1] != "B"):
                seq.append("B")
        edge_mad = _quantile(touches, 0.50) if touches else width
        rotations = sum(1 for a, b in zip(seq, seq[1:]) if a != b) // 2
        touch_balance = _clamp(rotations) if rotations > 0 else 0.0

        comps = {
            "efficiency": _clamp(1.0 - efficiency / self.p.eff_max),
            "occupancy": _clamp((occupancy - 0.50) / 0.35),
            "overlap": _clamp((adjacent_overlap - 0.30) / 0.50),
            "traversal": _clamp(path / max(2.0 * width, 1e-12)),
            "balance": touch_balance,
            "edge": math.exp(-edge_mad / max(edge_depth, 1e-12)),
            "stability": math.exp(-stability_move / max(unit, 1e-12)),
        }
        w = {
            "efficiency": self.p.w_efficiency, "occupancy": self.p.w_occupancy,
            "overlap": self.p.w_overlap, "traversal": self.p.w_traversal,
            "balance": self.p.w_balance, "edge": self.p.w_edge,
            "stability": self.p.w_stability,
        }
        geometric = math.exp(sum(w[k] * math.log(max(v, 1e-6)) for k, v in comps.items()))
        p_escape = _clamp((1.0 - dwell) / 0.30)
        p_trend = _clamp((efficiency - 0.40) / 0.20)
        width_dist = max(0.0, 1.2 - width_atr, width_atr - 10.0)
        p_width = _clamp(width_dist / 4.0)
        quality = geometric * (1.0 - p_escape) * (1.0 - 0.5 * p_trend) * (1.0 - 0.5 * p_width)

        return BalanceBox(
            lo=lower, hi=upper, quality=round(quality, 6),
            start_index=first, end_index=first + length - 1,
            width_atr=round(width_atr, 4), efficiency=round(efficiency, 4),
            dwell=round(dwell, 4), rotations=rotations,
            components={k: round(v, 4) for k, v in comps.items()},
        )

    def _suppress(self, boxes: list[BalanceBox]) -> list[BalanceBox]:
        if not boxes:
            return []
        kept: list[BalanceBox] = []
        for b in sorted(boxes, key=lambda x: -x.quality):
            dup = False
            for k in kept:
                shared = min(b.hi, k.hi) - max(b.lo, k.lo)
                span = max(b.hi, k.hi) - min(b.lo, k.lo)
                similar = (min(b.height, k.height) > 0.8 * max(b.height, k.height)
                           and (k.lo <= b.lo and b.hi <= k.hi
                                or b.lo <= k.lo and k.hi <= b.hi))
                if span > 0 and (shared > 0.8 * span or similar):
                    dup = True
                    break
            if not dup:
                kept.append(b)
        return kept
