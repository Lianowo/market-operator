"""多周期级联触发器(HTF 原子水平位冻结锚定 + LTF 触发周期价格拍卖确认)。

第一性原理(大小周期共振联动: 2h->30m / 4h->1h / 1d->4h):
1. 大周期(HTF)负责回答【在哪里交易(Where)】:
   仅使用当前时刻之前**已完整收盘确认**的大周期【原子水平位 Level】、波段极值与宏观拍卖状态,绝不偷看未收盘大周期 K 线。
2. 小周期(LTF Trigger)负责回答【何时确认扣扳机(When)】:
   当价格进入大周期原子水平位 ``[extreme, neck]`` 时,在小周期收盘上运行完全同构的
   ``LevelAuctionEngine``(清扫流动性后夺回突破前高/前低 `LEVEL_REJECTED`,或突破后站稳跟进 `BREAKOUT_ACCEPTED`)。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import pandas as pd

from .events import LevelAuctionEngine, MarketEvent, dominant_choch_events
from .exceptions import ConfigurationError
from .hypothesis import StructureView
from .level import Level
from .structure import MarketStructure, Params, Pivot, Regime, _bar_parts, parkinson_vol_ratio, variance_ratio

TRIGGER_TF_MAP: Mapping[int, int] = MappingProxyType({
    3600: 900,
    7200: 1800,
    14400: 3600,
    43200: 7200,
    86400: 14400,
})


def resolve_trigger_tf(
    obs_tf_seconds: int,
    *,
    tf_map: Mapping[int, int] | None = None,
    available: tuple[int, ...] = (900, 1800, 3600, 7200, 14400),
) -> tuple[int, str | None]:
    """解析认知周期对应的下级触发周期(秒)。"""
    obs = int(obs_tf_seconds)
    mapping = TRIGGER_TF_MAP if tf_map is None else {int(k): int(v) for k, v in dict(tf_map).items()}
    avail = tuple(sorted(int(a) for a in available))
    target = mapping.get(obs, obs // 4)
    if obs in mapping and mapping[obs] in avail and obs % mapping[obs] == 0:
        return mapping[obs], None
    cands = [a for a in avail if a < obs and obs % a == 0]
    if not cands:
        raise ConfigurationError(f"obs_tf {obs}s 无可用 trigger 帧(available={avail})")

    def _dist(f: int) -> tuple[float, int]:
        d = abs(math.log2(f / target)) if target > 0 else float("inf")
        return (d, -f)

    best = min(cands, key=_dist)
    why = "映射帧不可用" if obs in mapping else "映射缺失"
    return best, f"obs_tf={obs}s {why}(目标 {target}s),取最近可用 {best}s(scale={obs // best})"


class MicTracker:
    """状态化细周期触发跟踪器(离线回放与实盘轮询共用同一实例逻辑)。"""

    def __init__(self, params: Params | None = None, *, scale: int = 4) -> None:
        self.params = params or Params()
        self.scale = max(1, int(scale))
        self._auction = LevelAuctionEngine()
        self.trig_bars: list[tuple[int, float, float, float, float, float]] = []
        self._trig_atr: float = 0.0
        self._trig_tr_sum: float = 0.0
        self.frozen_levels: tuple[Level, ...] = ()
        self.frozen_regime: Regime = Regime.RANGE
        self.frozen_atr: float = 0.0
        self.frozen_pivots: tuple[Pivot, ...] = ()
        self.frozen_macro_imbalance: int = 0
        self.frozen_imbalance_strength: float = 0.0
        self.frozen_dollar_volume: float = 0.0
        self.frozen_cog_vr4: float = 1.0
        self.frozen_cog_park_ratio: float = 1.0
        self.frozen_cog_er20: float = 0.0
        self.frozen_cog_er_norm: float | None = None
        self.frozen_cog_index: int = -1

    def sync_from_snapshot(
        self,
        *,
        levels: Sequence[Level],
        regime: Regime,
        atr: float,
        pivots: Sequence[Pivot],
        macro_imbalance: int = 0,
        imbalance_strength: float = 0.0,
        dollar_volume: float = 0.0,
        cog_vr4: float = 1.0,
        cog_park_ratio: float = 1.0,
        cog_er20: float = 0.0,
        cog_er_norm: float | None = None,
        cog_index: int = -1,
    ) -> None:
        """在最近一根认知 K 线收盘时冻结高周期原子水平位地图。"""
        self.frozen_levels = tuple(lv.clone() for lv in levels if lv.is_active)
        self.frozen_regime = regime
        self.frozen_atr = float(atr)
        self.frozen_pivots = tuple(pivots)
        self.frozen_macro_imbalance = int(macro_imbalance)
        self.frozen_imbalance_strength = float(imbalance_strength)
        self.frozen_dollar_volume = float(dollar_volume)
        self.frozen_cog_vr4 = float(cog_vr4)
        self.frozen_cog_park_ratio = float(cog_park_ratio)
        self.frozen_cog_er20 = float(cog_er20)
        self.frozen_cog_er_norm = float(cog_er_norm) if cog_er_norm is not None else None
        self.frozen_cog_index = int(cog_index)

    def sync_from_structure(self, structure: MarketStructure) -> None:
        """直接从已收盘推进后的 MarketStructure 同步冻结快照。"""
        if structure.bars_seen <= 0:
            return
        self.sync_from_snapshot(
            levels=structure.levels,
            regime=structure.regime(),
            atr=structure.atr(),
            pivots=structure.pivot_slice(-self.params.range_max_pivots),
            macro_imbalance=structure.macro_imbalance(),
            imbalance_strength=structure.imbalance_strength(),
            dollar_volume=structure.dollar_volume(),
            cog_vr4=structure.variance_ratio(4),
            cog_park_ratio=structure.multioctave_parkinson_ratio(),
            cog_er20=structure.path_efficiency(20),
            cog_er_norm=structure.multioctave_brownian_er(),
            cog_index=structure.bars_seen - 1,
        )

    adopt_cognition = sync_from_structure

    def trig_variance_ratio(self, *, tf_hours: int = 0, q: int = 4) -> float:
        """触发细周期无量纲棒数空间(3/4 画布宽度,默认 120 棒)的严格因果方差比 VR(q)。

        彻底脱离 24h 墙钟假设: 无论加密(24h/天)、美股(6.5h/天)还是外汇(24h/5天),均严格按棒数空间计算。
        """
        _ = tf_hours  # 保留历史签名兼容,不再参与窗口计算
        win = max(24, (3 * max(64, int(self.params.canvas_bars))) // 4)
        if len(self.trig_bars) < q * 4 + 1:
            return 1.0
        span = self.trig_bars[-(win + 1):]
        return variance_ratio([b[4] for b in span], q=q)

    def trig_volume_ratios(self, window: int = 20) -> tuple[float, float]:
        """返回 (触发棒量/过去20棒中位量, 触发前3棒均量/过去20棒中位量); 无量馈源中性返回 (1.0, 1.0)。"""
        n = len(self.trig_bars)
        if n < window + 1:
            return 1.0, 1.0
        hist = sorted(float(b[5]) for b in self.trig_bars[-(window + 1):-1] if float(b[5]) >= 0.0)
        if len(hist) < window // 2:
            return 1.0, 1.0
        mid = len(hist) // 2
        med_v = hist[mid] if len(hist) % 2 else 0.5 * (hist[mid - 1] + hist[mid])
        if med_v <= 0.0:
            return 1.0, 1.0
        cur_v = float(self.trig_bars[-1][5])
        pre3 = [float(b[5]) for b in self.trig_bars[-4:-1]]
        pre3_mean = (sum(pre3) / len(pre3)) if pre3 else cur_v
        return cur_v / med_v, pre3_mean / med_v

    def trig_parkinson_and_range(
        self, short_n: int = 6, long_n: int = 30, range_win: int = 20
    ) -> tuple[float, float]:
        """返回触发前微观 Parkinson 波动觉醒比 σ_P(6)/σ_P(30) 与触发棒相对振幅 (H-L)/med20。

        仅使用严格位于当前触发棒之前的历史棒作为参照基线,零成交量依赖(外汇/股票/加密全通用)。
        """
        n = len(self.trig_bars)
        tp_ratio = 1.0
        if n >= long_n + 1:
            tp_ratio = parkinson_vol_ratio(self.trig_bars[-(long_n + 1):-1], short_n=short_n, long_n=long_n)
        bar_rel = 1.0
        if n >= range_win + 1:
            rngs = sorted(max(0.0, float(b[2]) - float(b[3])) for b in self.trig_bars[-(range_win + 1):-1])
            med_r = rngs[len(rngs) // 2]
            if med_r > 1e-12:
                cur_r = max(0.0, float(self.trig_bars[-1][2]) - float(self.trig_bars[-1][3]))
                bar_rel = cur_r / med_r
        return float(tp_ratio), float(bar_rel)

    def trig_range_q75_ratio(self, window: int = 20) -> float:
        """返回触发棒振幅 / 过去 ``window`` 棒振幅上四分位 Q75(>=1.0 标志内生上四分位动能爆发,零魔法常数)。"""
        n = len(self.trig_bars)
        if n < window + 1:
            return 0.0
        rngs = sorted(max(0.0, float(b[2]) - float(b[3])) for b in self.trig_bars[-(window + 1):-1])
        q75_r = rngs[(3 * len(rngs)) // 4]
        if q75_r <= 1e-12:
            return 0.0
        cur_r = max(0.0, float(self.trig_bars[-1][2]) - float(self.trig_bars[-1][3]))
        return float(cur_r / q75_r)

    def current_view(self, tf_hours: int = 0) -> StructureView:
        trig_park_ratio, bar_range_rel = self.trig_parkinson_and_range(6, 30, 20)
        bar_range_q75_rel = self.trig_range_q75_ratio(20)
        return StructureView(
            regime=self.frozen_regime,
            zones=self.frozen_levels,
            atr=self.frozen_atr,
            pivots=self.frozen_pivots,
            macro_imbalance=self.frozen_macro_imbalance,
            tf_hours=int(tf_hours),
            imbalance_strength=self.frozen_imbalance_strength,
            dollar_volume=self.frozen_dollar_volume,
            cog_vr4=self.frozen_cog_vr4,
            trig_vr4=self.trig_variance_ratio(tf_hours=tf_hours),
            cog_park_ratio=self.frozen_cog_park_ratio,
            cog_er20=self.frozen_cog_er20,
            cog_er_norm=self.frozen_cog_er_norm,
            trig_park_ratio=trig_park_ratio,
            bar_range_rel=bar_range_rel,
            bar_range_q75_rel=bar_range_q75_rel,
        )

    def update(self, trig_bar: Sequence[float]) -> list[MarketEvent]:
        """推进一根已收盘的触发细周期 K 线,使用触发周期自身的微观波动标尺锚定止损缓冲。"""
        b = _bar_parts(trig_bar)
        idx = len(self.trig_bars)
        prev_bar = self.trig_bars[-1] if idx > 0 else None
        recent_bars = tuple(self.trig_bars[-4:])
        _ts, o, h, l, _c, _v = b
        prev_c = prev_bar[4] if prev_bar is not None else o
        tr = (h - l) if idx == 0 else max(h - l, abs(h - prev_c), abs(l - prev_c))
        n = max(1, self.params.atr_n)
        if idx < n:
            self._trig_tr_sum += tr
            self._trig_atr = self._trig_tr_sum / (idx + 1)
        else:
            self._trig_atr = ((self._trig_atr * (n - 1)) + tr) / n

        self.trig_bars.append(b)
        if not self.frozen_levels or self.frozen_cog_index < 0:
            return []
        # 水平位本身属于高周期结构:其极值外侧防扫损呼吸垫必须匹配该水平位所属认知周期的波动量级!
        emitted = self._auction.step(
            bar_index=idx,
            bar=b,
            prev_bar=prev_bar,
            recent_bars=recent_bars,
            active_levels=self.frozen_levels,
            atr=self.frozen_atr,
            k_bm=self.params.k_bm,
            known_index_override=self.frozen_cog_index,
        )
        if prev_bar is not None:
            emitted.extend(dominant_choch_events(
                self.frozen_levels,
                prev_close=prev_bar[4],
                bar=b,
                known_index=self.frozen_cog_index,
            ))
        return emitted


@dataclass
class MicCognition:
    """单战役细周期触发缓存(由 MicTracker 严格按因果收盘时间轴生成)。"""

    symbol: str
    obs_tf_seconds: int
    trigger_tf_seconds: int
    scale: int
    bars: list[tuple[int, float, float, float, float, float]]
    cog_index_of: list[int]
    atrs: list[float]
    events_by: dict[int, list[MarketEvent]] = field(default_factory=dict)
    views: dict[int, StructureView] = field(default_factory=dict)


def _sync_tracker_from_cog(
    tracker: MicTracker, cog: Any, known_cog: int, pivots: Sequence[Pivot]
) -> None:
    """Synchronize MicTracker with precomputed cognition arrays at ``known_cog``."""
    def _series_val(attr: str, default: float) -> float:
        seq = getattr(cog, attr, None)
        return float(seq[known_cog]) if seq and known_cog < len(seq) else default

    tracker.sync_from_snapshot(
        levels=cog.active_zones[known_cog],
        regime=cog.regimes[known_cog],
        atr=cog.atrs[known_cog],
        pivots=pivots,
        macro_imbalance=cog.macro_imbalances[known_cog] if cog.macro_imbalances else 0,
        imbalance_strength=_series_val("imbalance_strengths", 0.0),
        dollar_volume=_series_val("dollar_volumes", 0.0),
        cog_vr4=_series_val("variance_ratios", 1.0),
        cog_park_ratio=_series_val("park_ratios", 1.0),
        cog_er_norm=_series_val("path_efficiencies", 0.0),
        cog_index=known_cog,
    )


def prepare_mic_cognition(
    cog_or_df: Any,
    df_trig: pd.DataFrame,
    params: Params,
    symbol: str | None = None,
    *,
    obs_tf_seconds: int,
    trigger_tf_seconds: int,
    **_ignored_legacy: Any,
) -> MicCognition:
    """严格因果对齐认知帧与触发帧,逐根调用 MicTracker 产出触发事件缓存。"""
    if isinstance(cog_or_df, pd.DataFrame):
        from .replay import prepare_cognition
        cog = prepare_cognition(cog_or_df, params, symbol or "UNKNOWN")
    else:
        cog = cog_or_df
    obs_ms = int(obs_tf_seconds) * 1000
    trig_ms = int(trigger_tf_seconds) * 1000
    if obs_ms <= trig_ms or obs_ms % trig_ms != 0:
        raise ValueError(f"trigger_tf {trigger_tf_seconds}s 必须整除且小于 obs_tf {obs_tf_seconds}s")
    if not cog.bars:
        raise ValueError(f"{cog.symbol}: empty cognition bars")

    cog_opens = [int(b[0]) for b in cog.bars]
    cog_closes = [t + obs_ms for t in cog_opens]
    first_open = cog_opens[0]
    last_end = cog_opens[-1] + obs_ms

    raw_rows = [
        (int(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
        for r in df_trig[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False)
        if first_open <= int(r.ts) < last_end
    ]

    tracker = MicTracker(params, scale=obs_ms // trig_ms)
    bars: list[tuple[int, float, float, float, float, float]] = []
    cog_index_of: list[int] = []
    atrs: list[float] = []
    events_by: dict[int, list[MarketEvent]] = {}
    views: dict[int, StructureView] = {}
    tf_hours = int(obs_tf_seconds // 3600)
    synced_cog_idx = -1

    # Incremental O(N) cursor over sorted cognition pivot indices (avoids O(N^2) rescan).
    pivot_keys = sorted(cog.pivots_by)
    pivot_cursor = 0
    collected_pivots: list[Pivot] = []
    pivot_limit = params.range_max_pivots

    for tb in raw_rows:
        ts_t = tb[0]
        bucket_idx = bisect_right(cog_opens, ts_t) - 1
        if bucket_idx < 0:
            continue
        close_moment = ts_t + trig_ms
        known_cog = bisect_right(cog_closes, close_moment) - 1
        if known_cog >= 0 and known_cog != synced_cog_idx:
            while pivot_cursor < len(pivot_keys) and pivot_keys[pivot_cursor] <= known_cog:
                collected_pivots.extend(cog.pivots_by[pivot_keys[pivot_cursor]])
                pivot_cursor += 1
            p_slice = tuple(collected_pivots[-pivot_limit:]) if pivot_limit > 0 else tuple(collected_pivots)
            _sync_tracker_from_cog(tracker, cog, known_cog, p_slice)
            synced_cog_idx = known_cog

        out_idx = len(bars)
        bars.append(tb)
        cog_index_of.append(bucket_idx)
        atrs.append(tracker.frozen_atr)
        emitted = tracker.update(tb)
        if emitted:
            events_by[out_idx] = emitted
            views[out_idx] = tracker.current_view(tf_hours=tf_hours)

    return MicCognition(
        symbol=cog.symbol,
        obs_tf_seconds=int(obs_tf_seconds),
        trigger_tf_seconds=int(trigger_tf_seconds),
        scale=obs_ms // trig_ms,
        bars=bars,
        cog_index_of=cog_index_of,
        atrs=atrs,
        events_by=events_by,
        views=views,
    )


def _pivots_up_to(cog: Any, cog_idx: int, limit: int) -> tuple[Pivot, ...]:
    collected: list[Pivot] = []
    for t in sorted(cog.pivots_by):
        if t > cog_idx:
            break
        collected.extend(cog.pivots_by[t])
    return tuple(collected[-limit:]) if limit > 0 else tuple(collected)

