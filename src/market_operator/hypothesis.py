"""交易假设构建(docs/methodology.md §7)。

每个事件转化为一个可证伪结构 Hypothesis:direction 取自事件;invalid 取自
`event.attrs["invalid"]`(events.py 已按 §7 口径写入结构失效价的建议值);
entry = 知时(known_index)下一根 bar 开盘价——构建时刻不可知,由成交装配侧事后代入(`with_entry`),构建时可为 None。

target_1 = 结构目标(§7:"最近的对侧区域边缘 / RANGE 对侧边缘"):
- 一般事件:沿事件方向、状态 ∈ {ACTIVE, RETESTED, FLIPPED} 的最近对侧区域近缘。
  多头方向取阻力区(resistance)的下缘 lo,空头取支撑区(support)的上缘 hi。
  搜索锚点取 `event.attrs["ref"]`(事件触发处已知的结构价位,因果):
  只接受严格沿方向越过 ref 的区域(多头 z.lo > ref;空头 z.hi < ref),
  与事件同侧或骑跨 ref 的区域不算"前方目标"。
  (structure.py 的 ZoneState 命名为 ACTIVE/BROKEN/RETESTED/FLIPPED/DEAD;
  活视图中 FLIPPED/DEAD 已被移除,此处仍按 §4.2 状态名集合过滤。)
- RANGE_ROTATED(RANGE_ROTATION):对侧边缘取 attrs["band"] 合成带边缘(多头 band_hi,
  空头 band_lo),不做区域搜索。
- 若无结构目标 ⇒ target_1 = None(不编造固定 R 目标,§7 纪律)。

r_mult = direction·(target_1 − entry) / (direction·(entry − invalid)),
仅当 entry 与 target_1 均存在且方向风险为正时计算;否则为 None。
行动门槛(r_mult ≥ 1.5,§7 先验)属建仓决策,本模块只度量,不过滤。

v2 增量(docs/v2-protocol.md §1):F1 失效距离下限 `f1_invalid_distance_ok`,
dist(close(t), invalid) ≥ k_is·atr(t);由 v2 入口调用,不改变 §7 构建口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .events import RANGE_ROTATED, MarketEvent
from .structure import Params, Regime, ZoneRole, ZoneState

TARGET_STATES = (ZoneState.ACTIVE, ZoneState.RETESTED, ZoneState.FLIPPED)


@dataclass(frozen=True)
class StructureView:
    """事件知时(t = known_index 收盘)的结构快照:只含当时已知的事实(A10)。"""

    regime: Regime
    zones: tuple  # tuple[Zone],只取状态仍在视图中的区域
    atr: float
    # The minimum causally confirmed pivot history used by the current
    # structure model. Optional to preserve all frozen v1-v5 call sites.
    pivots: tuple = ()


@dataclass(frozen=True)
class Hypothesis:
    """§7 可证伪交易假设。entry 缺省 None(研究器代入下一根开盘价)。"""

    event_id: str
    direction: int            # +1 多 / -1 空
    ts: object
    known_index: int
    entry: Optional[float]
    invalid: float
    target_1: Optional[float]
    r_mult: Optional[float]
    regime: Regime

    def with_entry(self, entry: float) -> "Hypothesis":
        """研究器代入知时下一根 bar 开盘价,并重算 r_mult(§7)。"""
        return Hypothesis(
            event_id=self.event_id, direction=self.direction, ts=self.ts,
            known_index=self.known_index, entry=float(entry),
            invalid=self.invalid, target_1=self.target_1,
            r_mult=_r_mult(self.direction, entry, self.invalid, self.target_1),
            regime=self.regime,
        )


def _structural_target(event: MarketEvent, view: StructureView) -> Optional[float]:
    """沿事件方向最近的对侧区域近缘;RANGE_ROTATED 取合成带对侧边缘。"""
    direction = event.direction
    if event.id == RANGE_ROTATED and "band" in event.attrs:
        band_lo, band_hi = event.attrs["band"]
        return float(band_hi) if direction > 0 else float(band_lo)

    ref = event.attrs.get("ref")
    if ref is None:  # 全部冻结事件均携带 ref;缺省时退回 invalid 作锚(保守)
        ref = event.attrs["invalid"]
    if direction > 0:
        cands = [z.lo for z in view.zones
                 if z.role is ZoneRole.RESISTANCE and z.state in TARGET_STATES
                 and z.lo > ref]
        return min(cands) if cands else None
    cands = [z.hi for z in view.zones
             if z.role is ZoneRole.SUPPORT and z.state in TARGET_STATES
             and z.hi < ref]
    return max(cands) if cands else None


def _r_mult(direction: int, entry: Optional[float], invalid: float,
            target: Optional[float]) -> Optional[float]:
    if entry is None or target is None:
        return None
    risk = direction * (entry - invalid)      # 沿方向每单位本金的最坏结构亏损
    if risk <= 0.0:                            # 入场已在失效价之外:假设无法成立
        return None
    return direction * (target - entry) / risk


def build(event: MarketEvent, structure_view: StructureView,
          params: Optional[Params], entry: Optional[float] = None
          ) -> Optional[Hypothesis]:
    """§7 假设构建。event.attrs 缺 "invalid" ⇒ None(不构成可证伪假设)。

    params 保留在签名中(契约:与 §7 先验同来源);当前实现全部阈值已由
    events.py 写入 attrs,本函数不再引用数值先验。
    """
    invalid = event.attrs.get("invalid")
    if invalid is None:
        return None
    target = _structural_target(event, structure_view)
    return Hypothesis(
        event_id=event.id, direction=event.direction, ts=event.ts,
        known_index=event.known_index, entry=entry, invalid=float(invalid),
        target_1=target,
        r_mult=_r_mult(event.direction, entry, float(invalid), target),
        regime=structure_view.regime,
    )


# ---------------------------------------------------------------------------
# F1 失效距离下限(docs/v2-protocol.md §1;v2 增量,不进入 v1 白名单路径)
# ---------------------------------------------------------------------------


def f1_invalid_distance_ok(event: MarketEvent, close: float, k_is: float) -> bool:
    """F1 门:dist(close(t), invalid) ≥ k_is·atr(t) 才允许 FLIP_RETESTED 入场。

    距离取事件 bar(t)收盘至 invalid 的沿方向距离;atr 用事件 attrs["atr"]
    (发射 bar 时写入,与 invalid 同一知时刻度)。attrs 缺 "invalid"/"atr" 或
    atr 非正 ⇒ 判 False(无法验证不满足)。
    """
    invalid = event.attrs.get("invalid")
    atr = event.attrs.get("atr")
    if invalid is None or atr is None or float(atr) <= 0.0:
        return False
    dist = event.direction * (float(close) - float(invalid))
    return dist >= float(k_is) * float(atr)
