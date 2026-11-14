"""多周期级联触发器(HTF 原子水平位冻结锚定 + LTF 触发周期价格拍卖确认)。

第一性原理(大小周期共振联动: 2h->30m / 4h->1h / 1d->4h):
1. 大周期(HTF)负责回答【在哪里交易(Where)】:
   仅使用当前时刻之前**已完整收盘确认**的大周期【原子水平位 Level】、波段极值与宏观拍卖状态,绝不偷看未收盘大周期 K 线。
2. 小周期(LTF Trigger)负责回答【何时确认扣扳机(When)】:
   当价格进入大周期原子水平位 ``[extreme, neck]`` 时,在小周期收盘上运行完全同构的
   ``LevelAuctionEngine``(清扫流动性后夺回突破前高/前低 `LEVEL_REJECTED`,或突破后站稳跟进 `BREAKOUT_ACCEPTED`)。
"""

from __future__ import annotations

from bisect import bisect_right
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

import pandas as pd

from .events import LevelAuctionEngine, MarketEvent
from .hypothesis import StructureView
from .level import Level
from .structure import MarketStructure, Params, Pivot, Regime, _bar_parts

TRIGGER_TF_MAP: dict[int, int] = {
    3600: 900,
    7200: 1800,
    14400: 3600,
    43200: 7200,
    86400: 14400,
}


def resolve_trigger_tf(
    obs_tf_seconds: int,
    *,
    tf_map: Optional[dict] = None,
    available: tuple = (900, 1800, 3600, 7200, 14400),
) -> tuple[int, Optional[str]]:
    """解析认知周期对应的下级触发周期(秒)。"""
    obs = int(obs_tf_seconds)
    mapping = TRIGGER_TF_MAP if tf_map is None else {int(k): int(v) for k, v in dict(tf_map).items()}
    avail = tuple(sorted(int(a) for a in available))
    target = mapping.get(obs, obs // 4)
    if obs in mapping and mapping[obs] in avail and obs % mapping[obs] == 0:
        return mapping[obs], None
    cands = [a for a in avail if a < obs and obs % a == 0]
    if not cands:
        raise RuntimeError(f"obs_tf {obs}s 无可用 trigger 帧(available={avail})")

    def _dist(f: int) -> tuple[float, int]:
        d = abs(math.log2(f / target)) if target > 0 else float("inf")
        return (d, -f)

    best = min(cands, key=_dist)
    why = "映射帧不可用" if obs in mapping else "映射缺失"
    return best, f"obs_tf={obs}s {why}(目标 {target}s),取最近可用 {best}s(scale={obs // best})"


class MicTracker:
    """状态化细周期触发跟踪器(离线回放与实盘轮询共用同一实例逻辑)。"""

    def __init__(self, params: Optional[Params] = None, *, scale: int = 4) -> None:
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
            cog_index=structure.bars_seen - 1,
        )

    adopt_cognition = sync_from_structure

    def current_view(self, tf_hours: int = 0) -> StructureView:
        return StructureView(
            regime=self.frozen_regime,
            zones=self.frozen_levels,
            atr=self.frozen_atr,
            pivots=self.frozen_pivots,
            macro_imbalance=self.frozen_macro_imbalance,
            tf_hours=int(tf_hours),
            imbalance_strength=self.frozen_imbalance_strength,
            dollar_volume=self.frozen_dollar_volume,
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
        return self._auction.step(
            bar_index=idx,
            bar=b,
            prev_bar=prev_bar,
            recent_bars=recent_bars,
            active_levels=self.frozen_levels,
            atr=self.frozen_atr,
            k_bm=self.params.k_bm,
            known_index_override=self.frozen_cog_index,
        )


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


def prepare_mic_cognition(
    cog_or_df,
    df_trig: pd.DataFrame,
    params: Params,
    symbol: Optional[str] = None,
    *,
    obs_tf_seconds: int,
    trigger_tf_seconds: int,
    **_ignored_legacy,
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

    for tb in raw_rows:
        ts_t = tb[0]
        bucket_idx = bisect_right(cog_opens, ts_t) - 1
        if bucket_idx < 0:
            continue
        close_moment = ts_t + trig_ms
        known_cog = bisect_right(cog_closes, close_moment) - 1
        if known_cog >= 0 and known_cog != synced_cog_idx:
            p_slice = _pivots_up_to(cog, known_cog, params.range_max_pivots)
            tracker.sync_from_snapshot(
                levels=cog.active_zones[known_cog],
                regime=cog.regimes[known_cog],
                atr=cog.atrs[known_cog],
                pivots=p_slice,
                macro_imbalance=cog.macro_imbalances[known_cog] if cog.macro_imbalances else 0,
                imbalance_strength=(
                    float(cog.imbalance_strengths[known_cog])
                    if getattr(cog, "imbalance_strengths", None) and known_cog < len(cog.imbalance_strengths)
                    else 0.0
                ),
                dollar_volume=(
                    float(cog.dollar_volumes[known_cog])
                    if getattr(cog, "dollar_volumes", None) and known_cog < len(cog.dollar_volumes)
                    else 0.0
                ),
                cog_index=known_cog,
            )
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


def _pivots_up_to(cog, cog_idx: int, limit: int) -> tuple[Pivot, ...]:
    collected: list[Pivot] = []
    for t in sorted(cog.pivots_by):
        if t > cog_idx:
            break
        collected.extend(cog.pivots_by[t])
    return tuple(collected[-limit:]) if limit > 0 else tuple(collected)

