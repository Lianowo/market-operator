"""基于【原子水平位 Level】的可证伪交易假设(Hypothesis)与寻价目标构建。

核心交易逻辑(传统图表 + AMT 拍卖市场理论):
1. 止损(``invalid``): 交易拍卖假设被彻底证伪的唯一结构极值外侧点。
2. 目标(``target_1``): 市场完成当前水平位拍卖后,沿最小阻力路径单边寻价的终点——
   - 若顺势前方存在未消耗的反向【原子水平位 Level】,取最近一条反向水平位的实体颈线内沿 ``neck``;
   - 若顺势前方处于历史/波段净空区(Price Discovery,无任何反向水平位阻挡),
     采用传统图表经典的【前一波段 1:1 等幅投射(Measured Move, AB = CD)】作为客观波段目标。
3. 撞墙与摩擦自洽检验:
   - 若前方存在反向阻碍水平位且净空空间 ``Room < Risk``(赔率不足 1:1,迎面撞墙),直接否决入场;
   - 若结构止损窄到双边手续费滑点侵蚀过高比例的 1R,作为微观噪音单否决。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .events import MarketEvent
from .level import Level, nearest_opposing_level
from .structure import Params, Pivot, Regime


def execution_target(meta: Mapping[str, Any], structural_target: float | None) -> float | None:
    """返回持仓执行管理采用的目标位(显式置 None 时不触发目标减仓)。"""
    return meta.get("execution_target", structural_target)


def entry_cost_error(
    direction: int, entry: float, invalid: float, meta: Mapping[str, Any]
) -> str | None:
    """检查双边执行摩擦占单笔结构风险(1R)的比例是否超限。"""
    limit = meta.get("max_cost_frac")
    if limit is None:
        return None
    try:
        limit_f = float(limit)
        if limit_f <= 0.0:
            return None
        entry_f, invalid_f = float(entry), float(invalid)
        per_side = float(meta["cost_per_side"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return "INVALID_COST_CONSTRAINT"
    if (
        direction not in (-1, 1)
        or not all(math.isfinite(x) for x in (entry_f, invalid_f, limit_f, per_side))
        or entry_f <= 0.0
        or per_side < 0.0
    ):
        return "INVALID_COST_CONSTRAINT"
    risk = direction * (entry_f - invalid_f)
    if risk <= 0.0 or (2.0 * per_side * entry_f) / risk > limit_f:
        return "ENTRY_COST"
    return None


def entry_contract_error(
    direction: int,
    entry: float,
    invalid: float,
    target: float | None,
    meta: Mapping[str, Any],
    *,
    at_execution: bool = False,
) -> str | None:
    """入场/成交时刻的结构契约自洽校验:
    - 决策时刻(at_execution=False):严格校验真实障碍墙净空 >= min_rr * Risk;
    - 市价成交时刻(at_execution=True):已由确认收盘决策通过,吸收正常盘口价差与微小滑点抖动,
      仅拦截越过止损/目标、成本超标或极端跳空(< 0.75 * min_rr)。
    """
    if not meta.get("bounded_entry"):
        return entry_cost_error(direction, entry, invalid, meta)
    try:
        entry_f, invalid_f = float(entry), float(invalid)
    except (TypeError, ValueError):
        return "BAD_INVALID"
    if direction not in (-1, 1) or not (math.isfinite(entry_f) and math.isfinite(invalid_f)):
        return "BAD_INVALID"
    risk = direction * (entry_f - invalid_f)
    if risk <= 0.0:
        return "BAD_INVALID"
    if meta.get("target_source") == "open_air":
        return entry_cost_error(direction, entry_f, invalid_f, meta)
    wall = meta.get("structural_target", target)
    if wall is None or not math.isfinite(float(wall)):
        return "MISSING_STRUCTURE_TARGET"
    room = direction * (float(wall) - entry_f)
    if room <= 0.0:
        return "TARGET_ALREADY_PASSED"
    min_rr = float(meta.get("entry_min_rr", 0.0))
    eff_min_rr = (0.75 * min_rr) if at_execution else min_rr
    if room + 1e-9 * risk < eff_min_rr * risk:
        return "INSUFFICIENT_REMAINING_RR"
    return entry_cost_error(direction, entry_f, invalid_f, meta)


@dataclass(frozen=True)
class StructureView:
    """事件确认时刻的因果市场结构只读快照。"""

    regime: Regime
    zones: tuple[Level, ...]       # 当前所有活跃原子水平位
    atr: float
    pivots: tuple[Pivot, ...] = ()
    macro_imbalance: int = 0
    tf_hours: int = 0
    imbalance_strength: float = 0.0
    dollar_volume: float = 0.0
    cog_vr4: float = 1.0           # 认知周期 Lo-MacKinlay 方差比 VR(4)
    trig_vr4: float = 1.0          # 触发周期因果方差比 VR(4)
    vol_rel: float = 1.0           # 历史兼容字段(路由层已 100% 零成交量纯 OHLC,不再读取)
    pre3_vol_rel: float = 1.0      # 历史兼容字段(路由层已 100% 零成交量纯 OHLC,不再读取)
    cog_park_ratio: float = 1.0    # 认知周期多倍频程 Parkinson 波动觉醒比(中性=1.0)
    cog_er20: float = 0.0          # 兼容字段: 单窗口 20 棒有符号路径效率比 ER
    cog_er_norm: float | None = None          # 认知周期多倍频程布朗归一路径效率比(临界阈值=1.0)
    trig_park_ratio: float = 1.0   # 触发前微观 Parkinson 波动觉醒比 σ_P(6) / σ_P(30)
    bar_range_rel: float = 1.0     # 触发棒振幅 (H-L) / 过去 20 棒中位振幅
    bar_range_q75_rel: float | None = None    # 触发棒振幅 (H-L) / 过去 20 棒上四分位 Q75(临界=1.0)

    @property
    def levels(self) -> tuple[Level, ...]:
        return self.zones

    @property
    def active_levels(self) -> tuple[Level, ...]:
        return tuple(z for z in self.zones if z.is_active)

    @property
    def med_prominence_2d(self) -> float:
        proms = sorted(float(z.prominence_2d) for z in self.zones if z.is_active)
        return proms[len(proms) // 2] if proms else 0.0


@dataclass(frozen=True)
class Hypothesis:
    """单笔可证伪结构交易假设。"""

    event_id: str
    direction: int
    invalid: float
    target_1: float | None
    regime: Regime
    ts: int
    known_index: int
    entry: float | None = None
    target_source: str = "none"    # "opposing_level" | "measured_move" | "none"

    @property
    def r_mult(self) -> float | None:
        if self.entry is None or self.target_1 is None:
            return None
        risk = self.direction * (self.entry - self.invalid)
        if risk <= 0.0:
            return None
        reward = self.direction * (self.target_1 - self.entry)
        return reward / risk

    def with_entry(self, entry_price: float) -> Hypothesis:
        return replace(self, entry=float(entry_price))


def build_hypothesis(
    ev: MarketEvent,
    view: StructureView,
    ref_price: float,
    _params: Params | None = None,
    *,
    htf_views: Sequence[StructureView] = (),
    min_rr: float = 2.0,
) -> Hypothesis | None:
    """由价格拍卖事件与本周期结构几何构建零魔法截断的可证伪交易假设。"""
    _ = (_params, htf_views)  # Retained for signature compatibility
    inv_raw = ev.attrs.get("invalid")
    if inv_raw is None:
        return None
    invalid = float(inv_raw)
    d = int(ev.direction)
    ref = float(ref_price)
    risk = d * (ref - invalid)
    if risk <= 0.0:
        return None

    ev_lv: Level | None = ev.attrs.get("level") or ev.attrs.get("zone")
    excl_uid = ev_lv.uid if ev_lv is not None else None

    pivs = getattr(view, "pivots", ()) or ()
    legs = (
        sorted(abs(float(pivs[k].price) - float(pivs[k - 1].price)) for k in range(1, len(pivs)))
        if len(pivs) >= 2
        else []
    )
    min_leg = legs[0] if legs else 0.0
    q25_leg = legs[len(legs) // 4] if legs else 0.0

    opp = nearest_opposing_level(view.zones, d, ref, exclude_uid=excl_uid)
    if opp is not None:
        wall_dist = d * (float(opp.neck) - ref)
        eff_dist = (
            min(wall_dist, max(min_leg, float(min_rr) * risk))
            if wall_dist >= float(min_rr) * risk
            else wall_dist
        )
        raw_target = ref + d * eff_dist
        target_source = "opposing_level"
    else:
        eff_dist = max(q25_leg, float(min_rr) * risk)
        raw_target = ref + d * eff_dist
        target_source = "open_air"

    return Hypothesis(
        event_id=ev.id,
        direction=d,
        invalid=invalid,
        target_1=raw_target,
        regime=view.regime,
        ts=int(ev.ts),
        known_index=int(ev.known_index),
        entry=ref,
        target_source=target_source,
    )
