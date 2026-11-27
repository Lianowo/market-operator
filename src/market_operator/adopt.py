"""交易所既有仓位的接管(扩展层,不参与策略链路)。

把已有的原子水平位结构事实(支撑/阻力带 + ATR)映射成与策略同源的
``invalid``(结构失效价)与可选 ``target_1``(对侧内沿)。本模块不依赖
``events`` / ``routing`` / ``position`` / ``campaign``,所有判定为纯数值计算。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ZoneEdge:
    """一条活跃结构带(与活跃水平位字段同口径,便于装配侧直接映射)。"""

    role: str                 # "SUPPORT" | "RESISTANCE"
    inner: float              # 内沿:支撑=VAL / 阻力=VAH(价格行为最先触碰的一侧)
    outer: float              # 外沿:极值防线(结构失效锚点)


@dataclass(frozen=True)
class AdoptionStructure:
    """接管所需的全部结构事实(纯数值,便于离线单测)。"""

    atr: float
    zones: tuple[ZoneEdge, ...] = ()
    k_bm: float = 0.1
    k_is: float = 0.8
    turtle_stop_atr: float = 2.0

    def nearest_support(self, mark: float) -> ZoneEdge | None:
        """现价下方最近的支撑(按内沿取最近;内沿必须严格在现价下方)。"""
        cands = [z for z in self.zones
                 if z.role == "SUPPORT" and z.inner < mark]
        return max(cands, key=lambda z: z.inner) if cands else None

    def nearest_resistance(self, mark: float) -> ZoneEdge | None:
        """现价上方最近的阻力(按内沿取最近;内沿必须严格在现价上方)。"""
        cands = [z for z in self.zones
                 if z.role == "RESISTANCE" and z.inner > mark]
        return min(cands, key=lambda z: z.inner) if cands else None


@dataclass(frozen=True)
class Adoption:
    """一次接管的结论(纯事实记录,调用方据此建仓/挂保护)。"""

    symbol: str
    direction: int
    entry: float
    invalid: float
    target_1: float | None
    stop_source: str            # "structure" | "atr_fallback"
    target_source: str          # "structure" | "none"
    risk_atr: float             # 止损距离 / ATR(如实记录,不做门槛)
    already_breached: bool      # 接手时现价已越过失效价

    @property
    def risk(self) -> float:
        """每单位风险(价格单位),恒 > 0。"""
        return abs(self.entry - self.invalid)


def propose(*, symbol: str, direction: int, entry: float, mark: float,
            structure: AdoptionStructure) -> Adoption | None:
    """给出接管参数;`direction` 必须是 ±1,`atr` 必须 > 0(否则返回 None)。

    `entry` 用交易所的持仓均价(不是标记价);`mark` 只用于挑选"现价下方/上方"
    的结构带与判断是否已经越过防线。
    """
    if direction not in (1, -1):
        raise ValueError(f"direction 必须是 ±1,收到 {direction!r}")
    atr = float(structure.atr)
    if not (atr > 0.0) or not (entry > 0.0):
        return None
    bm = float(structure.k_bm) * atr
    floor = float(structure.k_is) * atr

    target: float | None = None
    target_source = "none"
    if direction > 0:
        sup = structure.nearest_support(mark)
        if sup is not None:
            invalid = float(sup.outer) - bm
            stop_source = "structure"
        else:
            invalid = entry - float(structure.turtle_stop_atr) * atr
            stop_source = "atr_fallback"
        res = structure.nearest_resistance(mark)
        if res is not None:
            target, target_source = float(res.inner), "structure"
    else:
        res = structure.nearest_resistance(mark)
        if res is not None:
            invalid = float(res.outer) + bm
            stop_source = "structure"
        else:
            invalid = entry + float(structure.turtle_stop_atr) * atr
            stop_source = "atr_fallback"
        sup = structure.nearest_support(mark)
        if sup is not None:
            target, target_source = float(sup.inner), "structure"

    # 结构锚点必须给出"正确一侧且有意义的距离":否则退回 Turtle 初始防线
    dist = direction * (entry - invalid)
    if dist < floor:
        invalid = entry - direction * float(structure.turtle_stop_atr) * atr
        stop_source = "atr_fallback"
        dist = direction * (entry - invalid)
    if dist <= 0.0:
        return None

    # 目标若落在错误一侧(结构带被穿越后仍标 ACTIVE),视为没有目标。
    if target is not None and direction * (target - entry) <= 0.0:
        target, target_source = None, "none"

    breached = direction * (mark - invalid) <= 0.0
    return Adoption(symbol=symbol, direction=direction, entry=float(entry),
                    invalid=float(invalid), target_1=target,
                    stop_source=stop_source, target_source=target_source,
                    risk_atr=dist / atr, already_breached=bool(breached))


def structure_from_zones(*, atr: float, zones: Sequence[Any], k_bm: float = 0.05,
                         k_is: float = 0.5,
                         turtle_stop_atr: float = 2.0) -> AdoptionStructure:
    """将活跃【原子水平位 Level】序列规约为外部持仓接管用的支撑/阻力边界。"""
    edges: list[ZoneEdge] = []
    for z in zones:
        is_act = getattr(z, "is_active", None)
        if is_act is False:
            continue
        if is_act is None and str(getattr(z, "state", "")).lower() in ("consumed", "dead", "broken"):
            continue
        role = str(getattr(getattr(z, "role", ""), "value", getattr(z, "role", ""))).upper()
        inner_px = float(getattr(z, "inner", z.hi if role == "SUPPORT" else z.lo))
        outer_px = float(getattr(z, "outer", z.lo if role == "SUPPORT" else z.hi))
        if role == "SUPPORT":
            edges.append(ZoneEdge("SUPPORT", inner_px, outer_px))
        elif role == "RESISTANCE":
            edges.append(ZoneEdge("RESISTANCE", inner_px, outer_px))
    return AdoptionStructure(atr=float(atr), zones=tuple(edges), k_bm=k_bm,
                             k_is=k_is, turtle_stop_atr=turtle_stop_atr)


__all__ = ["Adoption", "AdoptionStructure", "ZoneEdge", "propose",
           "structure_from_zones"]
