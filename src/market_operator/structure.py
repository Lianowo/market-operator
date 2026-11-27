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
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .level import (
    Canvas2DLevelScanner,
    Level,
    LevelRole,
    LevelState,
    nearest_opposing_level,
)

Zone = Level
ZoneRole = LevelRole
ZoneState = LevelState


def variance_ratio(closes: Sequence[float], q: int = 4) -> float:
    """严格因果 Lo-MacKinlay 方差比 VR(q) = Var(r_q) / (q * Var(r_1))。

    零魔法阈值纯布朗扩散物理量:
    - VR(q) >= 1.0 ⇒ 超扩散 / 持续趋势流(突破与拒绝摆动均可延续);
    - VR(q) <  1.0 ⇒ 亚扩散 / 均值回归收敛(窄幅蓄势或随机拉锯);
    样本不足或零波动时安全回退中性值 1.0,跨加密/美股/商品/外汇完全通用。
    """
    n = len(closes)
    if q < 2 or n < q * 4 + 1:
        return 1.0
    logs: list[float] = []
    for px in closes:
        v = float(px)
        if v <= 0.0 or not math.isfinite(v):
            return 1.0
        logs.append(math.log(v))
    r1 = [logs[i] - logs[i - 1] for i in range(1, n)]
    m1 = len(r1)
    if m1 < 2:
        return 1.0
    mu1 = sum(r1) / m1
    var1 = sum((x - mu1) * (x - mu1) for x in r1) / (m1 - 1)
    if var1 <= 1e-18:
        return 1.0
    rq = [logs[i] - logs[i - q] for i in range(q, n)]
    mq = len(rq)
    if mq < 2:
        return 1.0
    muq = sum(rq) / mq
    varq = sum((x - muq) * (x - muq) for x in rq) / (mq - 1)
    return float(varq / (q * var1))


def parkinson_vol(bars: Sequence[Sequence[float]]) -> float:
    """严格因果 Parkinson 振幅波动率估计 σ_P = sqrt(mean(ln(H/L)^2) / (4 ln 2))。

    零参数纯 OHLC 物理量(不依赖成交量,跨加密/美股/商品/外汇通用)。
    """
    sq_sum = 0.0
    cnt = 0
    for b in bars:
        h = float(b[2])
        lo = float(b[3])
        if h > 0.0 and lo > 0.0 and h >= lo and math.isfinite(h) and math.isfinite(lo):
            lr = math.log(h / lo)
            sq_sum += lr * lr
            cnt += 1
    if cnt <= 0:
        return 0.0
    return math.sqrt(sq_sum / (4.0 * float(cnt) * math.log(2.0)))


def parkinson_vol_ratio(
    bars: Sequence[Sequence[float]], short_n: int = 10, long_n: int = 40
) -> float:
    """纯 OHLC 波动觉醒比 σ_P(short_n) / σ_P(long_n),精确中性常数 1.0。

    - >= 1.0: 活跃波动扩张态(突破具备能量觉醒 / 拒绝伴随微观止损清扫放量);
    - <  1.0: 波动萎缩死水态。
    样本不足或分母为零时中性返回 1.0。
    """
    sn = int(short_n)
    ln = int(long_n)
    if sn <= 0 or ln <= sn or len(bars) < ln:
        return 1.0
    v_long = parkinson_vol(bars[-ln:])
    if v_long <= 1e-12:
        return 1.0
    v_short = parkinson_vol(bars[-sn:])
    return float(v_short / v_long)


def multioctave_parkinson_ratio(
    bars: Sequence[Sequence[float]], canvas_bars: int = 160
) -> float:
    """无尺度多倍频程纯 OHLC Parkinson 波动觉醒比(锚定画布宽度 W,消除单窗口魔法常数)。

    在画布主尺度 W 的三个自然倍频程 (W/20, W/16, W/10) 对各自 4 倍母窗口 (W/5, W/4, 2W/5)
    上求均值(W=160 时为 8/32、10/40、16/64); 精确中性常数 1.0, 零成交量依赖。
    """
    w = max(64, int(canvas_bars))
    short_ns = (max(4, w // 20), max(5, w // 16), max(8, w // 10))
    max_long = max(4 * s for s in short_ns)
    if len(bars) < max_long:
        return parkinson_vol_ratio(bars, short_ns[1], 4 * short_ns[1])
    return sum(parkinson_vol_ratio(bars, s, 4 * s) for s in short_ns) / float(len(short_ns))


def path_efficiency(closes: Sequence[float]) -> float:
    """有符号路径效率比 ER = (C_t - C_0) / Σ|ΔC| ∈ [-1, +1]。

    配合精确布朗随机游走尺度边界 1 / sqrt(N) 区分「刚脱离基地的新相变突破(|ER|<=1/sqrt(N))」
    与「已沿单向线性透支多日的晚追衰竭(|ER|>1/sqrt(N))」。样本不足时中性返回 0.0。
    """
    n = len(closes)
    if n < 2:
        return 0.0
    c0 = float(closes[0])
    c1 = float(closes[-1])
    if not (math.isfinite(c0) and math.isfinite(c1)):
        return 0.0
    tot = 0.0
    prev = c0
    for i in range(1, n):
        cur = float(closes[i])
        if not math.isfinite(cur):
            return 0.0
        tot += abs(cur - prev)
        prev = cur
    if tot <= 1e-12:
        return 0.0
    return float((c1 - c0) / tot)


def multioctave_brownian_er(
    closes: Sequence[float], canvas_bars: int = 160
) -> float:
    """无尺度多倍频程布朗归一路径效率比 mean_k[ ER(N_k) * sqrt(N_k) ]。

    在画布宽度 W 派生的三个尺度 (W/10, W/8, W/5) 上各自除以布朗随机游走基准 1/sqrt(N_k) 后取均值
    (W=160 时为 16/20/32 棒),统一无量纲临界阈值恒为精确常数 1.0(<=1.0 新基地相变, >1.0 线性透支)。
    """
    w = max(64, int(canvas_bars))
    ns = (max(8, w // 10), max(10, w // 8), max(16, w // 5))
    max_n = max(ns)
    if len(closes) <= max_n:
        mid_n = ns[1]
        if len(closes) <= mid_n:
            return 0.0
        return path_efficiency(closes[-(mid_n + 1):]) * math.sqrt(float(mid_n))
    return sum(
        path_efficiency(closes[-(n + 1):]) * math.sqrt(float(n))
        for n in ns
    ) / float(len(ns))


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
    def from_json(cls, path: str | Path) -> Params:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        keys_by_id: dict[str, list[str]] = {}
        for key in raw:
            keys_by_id.setdefault(_key_id(key), []).append(key)
        wanted: dict[str, Any] = {}
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


class BarrierTopology(str, Enum):
    ENCLOSED_BALANCE = "ENCLOSED_BALANCE"
    IMBALANCE_UP = "IMBALANCE_UP"
    IMBALANCE_DOWN = "IMBALANCE_DOWN"
    BARRIER_CONTEST = "BARRIER_CONTEST"


class MarketStructure:
    """逐收盘 K 线因果推进的二维 (t, P) 空间层析结构地图。"""

    def __init__(self, params: Params | None = None) -> None:
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
        self.last_transitions: list[tuple[str, Level, Level | None]] = []
        self.last_new_pivots: list[Pivot] = []

    def mark_level_flip_retested(self, origin_index: int, role: LevelRole) -> None:
        """Retire a flipped level after its retest without exposing scanner internals."""
        self._scanner.mark_flip_retested(origin_index, role)

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

    def variance_ratio(self, q: int = 4, window: int | None = None) -> float:
        """返回本认知周期最近 ``window``(缺省锚定画布宽度 W//4,W=160 时为 40)根收盘 K 线的 Lo-MacKinlay 方差比 VR(q)。"""
        win = (
            max(q * 4, max(64, int(self.params.canvas_bars)) // 4)
            if window is None
            else int(window)
        )
        if len(self.bars) < q * 4 + 1:
            return 1.0
        span = self.bars[-(win + 1):]
        return variance_ratio([b[4] for b in span], q=q)

    def parkinson_ratio(self, short_n: int = 10, long_n: int = 40) -> float:
        """返回本认知周期最近 ``short_n / long_n`` 根收盘 K 线的 Parkinson 波动觉醒比。"""
        return parkinson_vol_ratio(self.bars, short_n=short_n, long_n=long_n)

    def multioctave_parkinson_ratio(self) -> float:
        """返回锚定当前画布宽度 ``canvas_bars`` 的无尺度多倍频程 Parkinson 波动觉醒比。"""
        return multioctave_parkinson_ratio(self.bars, canvas_bars=self.params.canvas_bars)

    def path_efficiency(self, window: int = 20) -> float:
        """返回本认知周期最近 ``window`` 根收盘 K 线的有符号路径效率比 ER。"""
        w = int(window)
        if w <= 0 or len(self.bars) <= w:
            return 0.0
        span = self.bars[-(w + 1):]
        return path_efficiency([b[4] for b in span])

    def multioctave_brownian_er(self) -> float:
        """返回锚定当前画布宽度 ``canvas_bars`` 的无尺度多倍频程布朗归一路径效率比(阈值=1.0)。"""
        w = max(64, int(self.params.canvas_bars))
        need = max(16, w // 5) + 1
        span = self.bars[-need:]
        return multioctave_brownian_er([b[4] for b in span], canvas_bars=self.params.canvas_bars)

    def recent_log_returns(self, max_bars: int = 60) -> tuple[float, ...]:
        """返回最近 ``max_bars`` 根已收盘 K 线的严格因果对数收益率序列(供无状态相关性定仓使用)。"""
        n = len(self.bars)
        if n < 2 or max_bars <= 0:
            return ()
        span = self.bars[-(int(max_bars) + 1):]
        out: list[float] = []
        for i in range(1, len(span)):
            c0 = float(span[i - 1][4])
            c1 = float(span[i][4])
            if c0 > 0.0 and c1 > 0.0 and math.isfinite(c0) and math.isfinite(c1):
                out.append(math.log(c1 / c0))
            else:
                out.append(0.0)
        return tuple(out)

    def log_return(self, lookback_bars: int) -> float | None:
        """返回最近 ``lookback_bars`` 根收盘 K 线的严格因果对数收益率。"""
        lb = int(lookback_bars)
        if lb <= 0 or len(self.bars) <= lb:
            return None
        c_now = float(self.bars[-1][4])
        c_prev = float(self.bars[-1 - lb][4])
        if c_now <= 0.0 or c_prev <= 0.0:
            return None
        return math.log(c_now / c_prev)

    def nearest_opposing(self, direction: int, ref_price: float,
                         exclude_uid: int | None = None) -> Level | None:
        return nearest_opposing_level(self._scanner.active_levels, direction,
                                      ref_price, exclude_uid=exclude_uid)


