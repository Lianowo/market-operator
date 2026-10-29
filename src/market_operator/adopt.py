"""交易所既有仓位的接管(扩展层,不参与策略链路)。

## 为什么需要

2026-10-29 实盘对账报出 `exchange_only BEUSDT dir=1 qty=0.05`:交易所上有一个
不是本系统开的仓位。旧实现对这类仓位一律**只告警、绝不动作**(`reconcile_positions`
的注释写着"异己仓,单列,绝不动作"),结果是它既没有交易所侧保护止损,也不进入
§8 持仓管理 —— 风险留在账本之外,人只能靠盯 TG 发现。

接管要求的是"用**同一套结构语义**管理它",而不是"用某个百分比兜底"。所以本模块
只做一件事:把**已有的结构事实**(平衡箱上下沿 + ATR)翻译成一个与策略同源的
`invalid`(结构失效价)与可选 `target_1`(对侧内沿)。事件、路由、持仓状态机一律
不参与 —— 本模块不 import `events` / `routing` / `position` / `campaign`,
策略链路的代码零改动。

## 语义(与 `TrueBalance` 的 ZoneBook 桥接同口径)

- `SUPPORT` 带 = `[lo_ext, lo_ext + w]` ⇒ 内沿(VAL)= `hi`,外沿 = `lo`
- `RESISTANCE` 带 = `[hi_ext - w, hi_ext]` ⇒ 内沿(VAH)= `lo`,外沿 = `hi`

多头:
- 失效价 = 最近支撑**外沿** − `k_bm·ATR`(被拒绝的极值之外才是证伪位);
- 若没有支撑带,或距离 `< k_is·ATR`(止损太近 ⇒ 成本占 R 过高,与 F1 同口径),
  退回 `entry − turtle_stop_atr·ATR`(Turtle 初始防线,`stop_source=atr_fallback`);
- 目标 = 上方最近阻力**内沿**(VAH);没有就不设目标(`target_source=none`)。

空头镜像。所有判断只用传入的数值与标记价,不读全局、不读写文件、不联网。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ZoneEdge:
    """一条活跃结构带(与 `structure.Zone` 的字段同名,便于装配侧直接映射)。"""

    role: str                 # "SUPPORT" | "RESISTANCE"
    inner: float              # 内沿:支撑=VAL / 阻力=VAH(价格行为最先触碰的一侧)
    outer: float              # 外沿:极值防线(结构失效锚点)


@dataclass(frozen=True)
class AdoptionStructure:
    """接管所需的全部结构事实(纯数值,便于离线单测)。"""

    atr: float
    zones: tuple = ()
    k_bm: float = 0.1
    k_is: float = 0.8
    turtle_stop_atr: float = 2.0

    def nearest_support(self, mark: float) -> Optional[ZoneEdge]:
        """现价下方最近的支撑(按内沿取最近;内沿必须严格在现价下方)。"""
        cands = [z for z in self.zones
                 if z.role == "SUPPORT" and z.inner < mark]
        return max(cands, key=lambda z: z.inner) if cands else None

    def nearest_resistance(self, mark: float) -> Optional[ZoneEdge]:
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
    target_1: Optional[float]
    stop_source: str            # "structure" | "atr_fallback"
    target_source: str          # "structure" | "none"
    risk_atr: float             # 止损距离 / ATR(如实记录,不做门槛)
    already_breached: bool      # 接手时现价已越过失效价(接管后按 §8.1 处置)

    @property
    def risk(self) -> float:
        """每单位风险(价格单位),恒 > 0。"""
        return abs(self.entry - self.invalid)


def propose(*, symbol: str, direction: int, entry: float, mark: float,
            structure: AdoptionStructure) -> Optional[Adoption]:
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

    target: Optional[float] = None
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
    # (与策略 F1/k_is 同一口径 —— 止损距离不足时该笔不存在,接管同样不挂废止损)。
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


def structure_from_zones(*, atr: float, zones, k_bm: float = 0.1,
                         k_is: float = 0.8,
                         turtle_stop_atr: float = 2.0) -> AdoptionStructure:
    """把 `structure.Zone` 序列规约成接管用的边界(只读;剔除非 ACTIVE)。"""
    edges: list[ZoneEdge] = []
    for z in zones:
        if str(getattr(z, "state", "ACTIVE")) not in ("ACTIVE", "ZoneState.ACTIVE"):
            continue
        role = str(getattr(getattr(z, "role", ""), "value", getattr(z, "role", "")))
        if role == "SUPPORT":
            edges.append(ZoneEdge("SUPPORT", float(z.hi), float(z.lo)))
        elif role == "RESISTANCE":
            edges.append(ZoneEdge("RESISTANCE", float(z.lo), float(z.hi)))
    return AdoptionStructure(atr=float(atr), zones=tuple(edges), k_bm=k_bm,
                             k_is=k_is, turtle_stop_atr=turtle_stop_atr)


__all__ = ["Adoption", "AdoptionStructure", "ZoneEdge", "propose",
           "structure_from_zones"]
