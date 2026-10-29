"""真实市场平衡箱与现价近邻夹板引擎 (True Balance & Active Bracket Engine)。

核心原则(告别固定窗口盲扫与满屏陈旧画线):
1. 波段定框生长 (Swing-Anchored Formation & Growth):
   - 由显著推进腿停止延伸后,多空在 [L, H] 内完成至少 3 段折返(>=2 顶 + >=2 底独立拐点)
     锚定左起点 ``start_index``,未有效破位前随 K 线向右增量生长。
2. 双层物理边界 (Dual-Layer Boundaries):
   - 内沿 (VAL / VAH):拐点实体边与实体高密分位簇拟合(用于触发观察与前缘 Limit 挂单);
   - 外沿 (lo_ext / hi_ext):边界拐点影线极值 + 异常单针截断保护(用于结构止损掩体)。
3. 三道硬性防伪门:
   - 水平度硬约束:每 10 根 K 线重心与边界漂移 <= max_slope_atr_per_10(剔除 45 度斜通道);
   - 中枢居中与驻留率:箱内收盘驻留率 >= dwell_min,且成交量×实体中枢 POC 落在 [0.25, 0.75];
   - 可交易净跨度:总跨度 >= width_min_atr * ATR。
4. 现价近邻夹板算子 (ActiveBracket):
   - 任意时刻围绕现价最多只激活 [1 个当前支撑, 1 个当前阻力]。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Optional, Sequence


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _quantile(vals: Sequence[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    pos = _clamp(q) * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1.0 - (pos - lo)) + s[hi] * (pos - lo)


@dataclass(frozen=True)
class TrueBalanceParams:
    pivot_rev_atr: float = 0.90          # 波段拐点确认回退阈值(ATR 倍数)
    width_min_atr: float = 1.80          # 可交易箱体最小总跨度(ATR 倍数)
    width_max_atr: float = 12.0          # 箱体最大跨度上限(ATR 倍数)
    min_bars: int = 10                   # 箱体最短持续 K 线根数
    min_swings: int = 4                  # 至少 4 个交替拐点(= 3 段折返:2 顶 + 2 底)
    edge_cluster_atr: float = 0.65       # 拐点归属上下沿簇的容差(ATR 倍数)
    wick_cap_atr: float = 0.85           # 单根极端插针超出内沿的最大外扩保护(ATR 倍数)
    break_margin_atr: float = 0.15       # 收盘有效破位外沿的余量(ATR 倍数)
    break_confirm_bars: int = 2          # 连续收盘在外沿外侧几根判箱体破位转迁移
    max_slope_atr_per_10: float = 0.30   # 每 10 根 K 线最大重心/边界斜率漂移(ATR 倍数)
    dwell_min: float = 0.68              # 收盘落在箱体内的最低驻留率
    poc_zone_lo: float = 0.25            # 价值中枢 POC 相对箱体高度下界
    poc_zone_hi: float = 0.75            # 价值中枢 POC 相对箱体高度上界
    quality_min: float = 0.35            # 综合真实度最低门槛
    keep_flipped_bars: int = 96          # 破位后翻转边缘作为 S/R Flip 保留的最大 bar 数


@dataclass(frozen=True)
class SwingPoint:
    index: int
    ts: int
    kind: str          # "HIGH" | "LOW"
    price: float       # 影线极值
    body_edge: float   # 实体极值:HIGH 取 max(o,c),LOW 取 min(o,c)
    close: float
    atr: float


@dataclass
class TrueBalanceBox:
    box_id: int
    start_index: int
    end_index: int
    val: float                 # 支撑内沿 (Value Area Low)
    vah: float                 # 阻力内沿 (Value Area High)
    lo_ext: float              # 支撑外沿 (极值防线)
    hi_ext: float              # 阻力外沿 (极值防线)
    poc: float                 # 成交量×实体价值中枢
    quality: float
    width_atr: float
    rotations: int
    top_touches: int
    bot_touches: int
    dwell: float
    slope_atr_per_10: float
    efficiency: float
    state: str = "ACTIVE"      # "ACTIVE" | "BROKEN_UP" | "BROKEN_DOWN"
    broken_index: Optional[int] = None
    outside_up_streak: int = 0
    outside_dn_streak: int = 0
    components: dict = field(default_factory=dict)

    @property
    def lo(self) -> float:
        return self.val

    @property
    def hi(self) -> float:
        return self.vah

    @property
    def center(self) -> float:
        return 0.5 * (self.vah + self.val)

    @property
    def height(self) -> float:
        return self.vah - self.val

    @property
    def total_span(self) -> float:
        return self.hi_ext - self.lo_ext

    @property
    def poc_pos(self) -> float:
        return (self.poc - self.val) / max(self.vah - self.val, 1e-12)


@dataclass(frozen=True)
class BracketLevel:
    role: str                  # "SUPPORT" | "RESISTANCE"
    source_kind: str           # "BOX_EDGE" | "FLIPPED_BOX_EDGE" | "MAJOR_SWING"
    inner: float               # 内沿(触碰观察 / Limit 挂单参考价)
    outer: float               # 外沿(极值防线 / 结构止损掩体)
    opposite: Optional[float]  # 均值回归对侧目标
    poc: Optional[float]
    quality: float
    rotations: int
    width_atr: float
    origin_index: int
    box_id: Optional[int] = None

    @property
    def lo(self) -> float:
        return min(self.inner, self.outer)

    @property
    def hi(self) -> float:
        return max(self.inner, self.outer)


@dataclass(frozen=True)
class ActiveBracket:
    """围绕现价 P_t 的唯一活跃夹板:[最多 1 个当前支撑, 最多 1 个当前阻力]。"""

    index: int
    close: float
    atr: float
    mode: str                              # "IN_BOX" | "MIGRATION"
    active_box: Optional[TrueBalanceBox]
    support: Optional[BracketLevel]
    resistance: Optional[BracketLevel]


class TrueBalanceDetector:
    """事件定框 + 双层物理边界 + 水平/中枢硬校验 + 现价夹板引擎。"""

    def __init__(self, params: Optional[TrueBalanceParams] = None) -> None:
        self.p = params or TrueBalanceParams()
        self.bars: list[tuple[int, float, float, float, float, float]] = []
        self.units: list[float] = []
        self.path_prefix: list[float] = [0.0]
        self.swings: list[SwingPoint] = []
        self._swing_indices: list[int] = []
        self.active_box: Optional[TrueBalanceBox] = None
        self.history_boxes: list[TrueBalanceBox] = []
        self._next_box_id: int = 1
        self._last_swing_bar: int = -999

        self._sw_dir: Optional[int] = None
        self._sw_ext_price: float = 0.0
        self._sw_ext_body: float = 0.0
        self._sw_ext_close: float = 0.0
        self._sw_ext_idx: int = 0
        self._sw_ext_ts: int = 0
        self._sw_ext_atr: float = 0.0

    def push(self, bar: tuple, unit: float) -> Optional[ActiveBracket]:
        ts, o, h, l, c, v = (
            int(bar[0]), float(bar[1]), float(bar[2]),
            float(bar[3]), float(bar[4]), float(bar[5]),
        )
        idx = len(self.bars)
        self.bars.append((ts, o, h, l, c, v))
        atr = float(unit) if unit and unit > 0.0 else max(h - l, 1e-8)
        self.units.append(atr)
        prev_c = self.bars[-2][4] if idx > 0 else o
        self.path_prefix.append(self.path_prefix[-1] + abs(c - prev_c))

        new_swing = self._update_swing(idx, ts, o, h, l, c, atr)
        if new_swing is not None:
            self.swings.append(new_swing)
            self._swing_indices.append(new_swing.index)
            self._last_swing_bar = idx

        if self.active_box is not None:
            self._advance_active_box(idx, o, h, l, c, v, atr, has_new_swing=(new_swing is not None))

        if (
            self.active_box is None
            and len(self.swings) >= self.p.min_swings
            and (new_swing is not None or idx - self._last_swing_bar <= 3)
        ):
            cand = self._try_spawn_box(idx, atr)
            if cand is not None:
                self.active_box = cand

        return self.bracket()

    def _update_swing(self, idx: int, ts: int, o: float, h: float, l: float,
                      c: float, atr: float) -> Optional[SwingPoint]:
        body_hi = max(o, c)
        body_lo = min(o, c)
        rev = self.p.pivot_rev_atr * atr

        if idx == 0:
            self._sw_ext_price = c
            self._sw_ext_body = c
            self._sw_ext_close = c
            self._sw_ext_idx = 0
            self._sw_ext_ts = ts
            self._sw_ext_atr = atr
            return None

        if self._sw_dir is None:
            up_move = h - self._sw_ext_price
            dn_move = self._sw_ext_price - l
            if up_move >= rev and up_move >= dn_move:
                self._sw_dir = 1
                self._sw_ext_price, self._sw_ext_body = h, body_hi
                self._sw_ext_close, self._sw_ext_idx = c, idx
                self._sw_ext_ts, self._sw_ext_atr = ts, atr
            elif dn_move >= rev:
                self._sw_dir = -1
                self._sw_ext_price, self._sw_ext_body = l, body_lo
                self._sw_ext_close, self._sw_ext_idx = c, idx
                self._sw_ext_ts, self._sw_ext_atr = ts, atr
            return None

        if self._sw_dir == 1:
            if h >= self._sw_ext_price:
                self._sw_ext_price = h
                self._sw_ext_body = max(self._sw_ext_body, body_hi)
                self._sw_ext_close = c
                self._sw_ext_idx = idx
                self._sw_ext_ts = ts
                self._sw_ext_atr = atr
                return None
            if self._sw_ext_price - l >= rev:
                confirmed = SwingPoint(
                    index=self._sw_ext_idx, ts=self._sw_ext_ts, kind="HIGH",
                    price=self._sw_ext_price, body_edge=self._sw_ext_body,
                    close=self._sw_ext_close, atr=self._sw_ext_atr,
                )
                self._sw_dir = -1
                self._sw_ext_price, self._sw_ext_body = l, body_lo
                self._sw_ext_close, self._sw_ext_idx = c, idx
                self._sw_ext_ts, self._sw_ext_atr = ts, atr
                return confirmed
        else:
            if l <= self._sw_ext_price:
                self._sw_ext_price = l
                self._sw_ext_body = min(self._sw_ext_body, body_lo)
                self._sw_ext_close = c
                self._sw_ext_idx = idx
                self._sw_ext_ts = ts
                self._sw_ext_atr = atr
                return None
            if h - self._sw_ext_price >= rev:
                confirmed = SwingPoint(
                    index=self._sw_ext_idx, ts=self._sw_ext_ts, kind="LOW",
                    price=self._sw_ext_price, body_edge=self._sw_ext_body,
                    close=self._sw_ext_close, atr=self._sw_ext_atr,
                )
                self._sw_dir = 1
                self._sw_ext_price, self._sw_ext_body = h, body_hi
                self._sw_ext_close, self._sw_ext_idx = c, idx
                self._sw_ext_ts, self._sw_ext_atr = ts, atr
                return confirmed
        return None

    def _advance_active_box(self, idx: int, o: float, h: float, l: float,
                            c: float, v: float, atr: float, *,
                            has_new_swing: bool = False) -> None:
        box = self.active_box
        assert box is not None
        bm = self.p.break_margin_atr * atr
        runaway = 1.25 * atr

        if c > box.hi_ext + bm:
            box.outside_up_streak += 1
            box.outside_dn_streak = 0
        elif c < box.lo_ext - bm:
            box.outside_dn_streak += 1
            box.outside_up_streak = 0
        else:
            box.outside_up_streak = 0
            box.outside_dn_streak = 0

        if box.outside_up_streak >= self.p.break_confirm_bars or c > box.hi_ext + runaway:
            box.state = "BROKEN_UP"
            box.broken_index = idx
            self.history_boxes.append(box)
            self.active_box = None
            return
        if box.outside_dn_streak >= self.p.break_confirm_bars or c < box.lo_ext - runaway:
            box.state = "BROKEN_DOWN"
            box.broken_index = idx
            self.history_boxes.append(box)
            self.active_box = None
            return

        # 仍在箱体有效生命期内:价格探顶/探底或单根假突破收回均维持箱体存活!
        box.end_index = idx
        if has_new_swing and idx - box.start_index + 1 <= 180:
            rebuilt = self._evaluate_span(box.start_index, idx, atr, box_id=box.box_id)
            if rebuilt is not None:
                rebuilt.outside_up_streak = box.outside_up_streak
                rebuilt.outside_dn_streak = box.outside_dn_streak
                self.active_box = rebuilt

    def _try_spawn_box(self, idx: int, atr: float) -> Optional[TrueBalanceBox]:
        min_start = 0
        if self.history_boxes:
            last_broken = self.history_boxes[-1]
            if last_broken.broken_index is not None:
                min_start = max(0, last_broken.broken_index - 2)

        lo_pos = bisect_left(self._swing_indices, min_start)
        recent_swings = self.swings[max(lo_pos, len(self.swings) - 10):]
        if len(recent_swings) < self.p.min_swings:
            return None

        best: Optional[TrueBalanceBox] = None
        max_k = min(len(recent_swings), 10)
        for k in range(self.p.min_swings, max_k + 1):
            sub = recent_swings[-k:]
            start_idx = sub[0].index
            if idx - start_idx + 1 < self.p.min_bars or idx - start_idx + 1 > 180:
                continue
            cand = self._evaluate_span(start_idx, idx, atr, box_id=self._next_box_id)
            if cand is not None:
                if best is None or cand.quality > best.quality:
                    best = cand

        if best is not None:
            self._next_box_id += 1
        return best

    def _evaluate_span(self, start_idx: int, end_idx: int, atr: float,
                       *, box_id: int) -> Optional[TrueBalanceBox]:
        length = end_idx - start_idx + 1
        if length < self.p.min_bars or atr <= 0.0:
            return None

        s_lo = bisect_left(self._swing_indices, start_idx)
        s_hi = bisect_right(self._swing_indices, end_idx)
        span_swings = self.swings[s_lo:s_hi]
        high_swings = [s for s in span_swings if s.kind == "HIGH"]
        low_swings = [s for s in span_swings if s.kind == "LOW"]
        if len(high_swings) < 2 or len(low_swings) < 2:
            return None

        bars = self.bars[start_idx:end_idx + 1]
        body_highs = [max(b[1], b[4]) for b in bars]
        body_lows = [min(b[1], b[4]) for b in bars]
        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]

        top_anchors = sorted([s.body_edge for s in high_swings], reverse=True)
        bot_anchors = sorted([s.body_edge for s in low_swings])
        vah_swing = _quantile(top_anchors[:max(2, len(top_anchors) // 2 + 1)], 0.50)
        val_swing = _quantile(bot_anchors[:max(2, len(bot_anchors) // 2 + 1)], 0.50)
        vah_body = _quantile(body_highs, 0.86)
        val_body = _quantile(body_lows, 0.14)
        vah = 0.60 * vah_swing + 0.40 * vah_body
        val = 0.60 * val_swing + 0.40 * val_body

        inner_width = vah - val
        if inner_width <= 0.0:
            return None

        cluster_tol = self.p.edge_cluster_atr * atr
        top_cluster = [s for s in high_swings if abs(s.body_edge - vah) <= cluster_tol or abs(s.price - vah) <= cluster_tol]
        bot_cluster = [s for s in low_swings if abs(s.body_edge - val) <= cluster_tol or abs(s.price - val) <= cluster_tol]
        if len(top_cluster) < 2 or len(bot_cluster) < 2:
            return None

        raw_hi_ext = max(max(s.price for s in top_cluster), _quantile(highs, 0.94))
        raw_lo_ext = min(min(s.price for s in bot_cluster), _quantile(lows, 0.06))
        hi_ext = max(vah + 0.15 * atr, min(raw_hi_ext, vah + self.p.wick_cap_atr * atr))
        lo_ext = min(val - 0.15 * atr, max(raw_lo_ext, val - self.p.wick_cap_atr * atr))
        # 确保内沿 VAL/VAH 紧贴外沿极值防线(带宽 <= 0.50*ATR),避免在箱体半山腰过早触发
        vah = max(vah, hi_ext - 0.50 * atr)
        val = min(val, lo_ext + 0.50 * atr)

        total_width_atr = (hi_ext - lo_ext) / atr
        if total_width_atr < self.p.width_min_atr or total_width_atr > self.p.width_max_atr:
            return None

        seq: list[str] = []
        top_zone_lo = vah - 0.35 * inner_width
        bot_zone_hi = val + 0.35 * inner_width
        for s in span_swings:
            if s.kind == "HIGH" and s.price >= top_zone_lo:
                if not seq or seq[-1] != "T":
                    seq.append("T")
            elif s.kind == "LOW" and s.price <= bot_zone_hi:
                if not seq or seq[-1] != "B":
                    seq.append("B")
        alternations = max(0, len(seq) - 1)
        if alternations < 3:
            return None
        rotations = alternations


        mid = length // 2
        mean_c1 = sum(closes[:mid]) / max(mid, 1)
        mean_c2 = sum(closes[mid:]) / max(length - mid, 1)
        half_drift_per_10 = abs(mean_c2 - mean_c1) / max(length / 2.0, 1.0) * 10.0 / atr

        top_slope_per_10 = (
            abs(top_cluster[-1].body_edge - top_cluster[0].body_edge)
            / max(top_cluster[-1].index - top_cluster[0].index, 5) * 10.0 / atr
        )
        bot_slope_per_10 = (
            abs(bot_cluster[-1].body_edge - bot_cluster[0].body_edge)
            / max(bot_cluster[-1].index - bot_cluster[0].index, 5) * 10.0 / atr
        )
        slope_atr_per_10 = max(half_drift_per_10, 0.5 * (top_slope_per_10 + bot_slope_per_10))
        if slope_atr_per_10 > self.p.max_slope_atr_per_10:
            return None

        dwell_tol = 0.15 * atr
        inside_count = sum(1 for c in closes if (lo_ext - dwell_tol) <= c <= (hi_ext + dwell_tol))
        dwell = inside_count / length
        if dwell < self.p.dwell_min:
            return None

        poc = self._compute_poc(bars, val, vah)
        poc_pos = (poc - val) / max(inner_width, 1e-12)
        if not (self.p.poc_zone_lo <= poc_pos <= self.p.poc_zone_hi):
            return None

        path = self.path_prefix[end_idx + 1] - self.path_prefix[start_idx]
        disp = abs(closes[-1] - closes[0])
        efficiency = disp / path if path > 0.0 else 1.0
        if efficiency > 0.48:
            return None

        c_horizontal = _clamp(1.0 - slope_atr_per_10 / max(self.p.max_slope_atr_per_10, 1e-6))
        c_dwell = _clamp((dwell - 0.60) / 0.35)
        c_poc = _clamp(1.0 - abs(poc_pos - 0.50) / 0.30)
        c_rot = _clamp(alternations / 5.0)
        c_eff = _clamp(1.0 - efficiency / 0.50)
        quality = (
            0.25 * c_horizontal
            + 0.22 * c_dwell
            + 0.18 * c_poc
            + 0.20 * c_rot
            + 0.15 * c_eff
        )
        if quality < self.p.quality_min:
            return None

        return TrueBalanceBox(
            box_id=box_id, start_index=start_idx, end_index=end_idx,
            val=val, vah=vah, lo_ext=lo_ext, hi_ext=hi_ext, poc=poc,
            quality=quality, width_atr=total_width_atr,
            rotations=max(1, rotations),
            top_touches=len(top_cluster), bot_touches=len(bot_cluster),
            dwell=dwell, slope_atr_per_10=slope_atr_per_10,
            efficiency=efficiency,
            components={
                "horizontal": round(c_horizontal, 3),
                "dwell": round(c_dwell, 3),
                "poc": round(c_poc, 3),
                "rotation": round(c_rot, 3),
                "efficiency": round(c_eff, 3),
            },
        )


    @staticmethod
    def _compute_poc(bars: Sequence[tuple], val: float, vah: float, bins: int = 12) -> float:
        span = vah - val
        if span <= 1e-12 or not bars:
            return 0.5 * (val + vah)
        step = span / bins
        weights = [0.0] * bins
        for b in bars:
            o, h, l, c, v = b[1], b[2], b[3], b[4], max(float(b[5]), 1.0)
            b_lo, b_hi = min(o, c), max(o, c)
            for i in range(bins):
                bin_lo = val + i * step
                bin_hi = bin_lo + step
                body_ov = max(0.0, min(b_hi, bin_hi) - max(b_lo, bin_lo))
                wick_ov = max(0.0, min(h, bin_hi) - max(l, bin_lo))
                if body_ov > 0.0 or (b_hi == b_lo and bin_lo <= c <= bin_hi):
                    weights[i] += v * (0.75 * (body_ov / max(b_hi - b_lo, step * 0.25)) + 0.25)
                elif wick_ov > 0.0:
                    weights[i] += v * 0.20 * (wick_ov / max(h - l, step))
        best_i = max(range(bins), key=weights.__getitem__)
        return val + (best_i + 0.5) * step

    def bracket(self) -> Optional[ActiveBracket]:
        if not self.bars:
            return None
        idx = len(self.bars) - 1
        c = self.bars[-1][4]
        atr = self.units[-1]

        if self.active_box is not None:
            b = self.active_box
            sup = BracketLevel(
                role="SUPPORT", source_kind="BOX_EDGE",
                inner=b.val, outer=b.lo_ext, opposite=b.vah, poc=b.poc,
                quality=b.quality, rotations=b.rotations, width_atr=b.width_atr,
                origin_index=b.start_index, box_id=b.box_id,
            )
            res = BracketLevel(
                role="RESISTANCE", source_kind="BOX_EDGE",
                inner=b.vah, outer=b.hi_ext, opposite=b.val, poc=b.poc,
                quality=b.quality, rotations=b.rotations, width_atr=b.width_atr,
                origin_index=b.start_index, box_id=b.box_id,
            )
            return ActiveBracket(
                index=idx, close=c, atr=atr, mode="IN_BOX",
                active_box=b, support=sup, resistance=res,
            )

        candidates_sup: list[BracketLevel] = []
        candidates_res: list[BracketLevel] = []

        for hb in reversed(self.history_boxes[-6:]):
            if hb.broken_index is not None and idx - hb.broken_index > self.p.keep_flipped_bars:
                continue
            if hb.state == "BROKEN_UP" and c >= hb.val:
                candidates_sup.append(BracketLevel(
                    role="SUPPORT", source_kind="FLIPPED_BOX_EDGE",
                    inner=hb.hi_ext, outer=hb.vah, opposite=None, poc=hb.poc,
                    quality=hb.quality, rotations=hb.rotations, width_atr=hb.width_atr,
                    origin_index=hb.broken_index or hb.end_index, box_id=hb.box_id,
                ))
            elif hb.state == "BROKEN_DOWN" and c <= hb.vah:
                candidates_res.append(BracketLevel(
                    role="RESISTANCE", source_kind="FLIPPED_BOX_EDGE",
                    inner=hb.lo_ext, outer=hb.val, opposite=None, poc=hb.poc,
                    quality=hb.quality, rotations=hb.rotations, width_atr=hb.width_atr,
                    origin_index=hb.broken_index or hb.end_index, box_id=hb.box_id,
                ))

        half_w = 0.35 * atr
        for sw in reversed(self.swings[-12:]):
            if sw.kind == "LOW" and sw.price < c:
                candidates_sup.append(BracketLevel(
                    role="SUPPORT", source_kind="MAJOR_SWING",
                    inner=max(sw.body_edge, sw.price + half_w), outer=sw.price,
                    opposite=None, poc=None, quality=0.50, rotations=1,
                    width_atr=0.0, origin_index=sw.index,
                ))
            elif sw.kind == "HIGH" and sw.price > c:
                candidates_res.append(BracketLevel(
                    role="RESISTANCE", source_kind="MAJOR_SWING",
                    inner=min(sw.body_edge, sw.price - half_w), outer=sw.price,
                    opposite=None, poc=None, quality=0.50, rotations=1,
                    width_atr=0.0, origin_index=sw.index,
                ))

        nearest_sup = max(
            (s for s in candidates_sup if s.outer <= c + 0.2 * atr),
            key=lambda s: (1 if s.source_kind == "FLIPPED_BOX_EDGE" else 0, s.inner),
            default=None,
        )
        nearest_res = min(
            (r for r in candidates_res if r.outer >= c - 0.2 * atr),
            key=lambda r: (0 if r.source_kind == "FLIPPED_BOX_EDGE" else 1, r.inner),
            default=None,
        )
        return ActiveBracket(
            index=idx, close=c, atr=atr, mode="MIGRATION",
            active_box=None, support=nearest_sup, resistance=nearest_res,
        )

