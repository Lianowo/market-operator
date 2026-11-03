"""市场认知核心:ATR、确认波段、区域生命周期与市场状态(regime)。

因果知时纪律:任何结构事实只在 ``known_index <= t`` 时参与认知与决策。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .true_balance import TrueBalanceDetector, TrueBalanceParams


def _key_id(key: str) -> str:
    """配置键的归一形式:忽略大小写与下划线(``N_a`` ≡ ``n_a``)。"""
    return key.replace("_", "").lower()



@dataclass(frozen=True)
class Params:
    atr_n: int = 14
    k_rev: float = 1.0
    k_zw: float = 0.5
    k_bm: float = 0.1
    k_rj: float = 0.3
    k_span: float = 3.0
    k_room: float = 2.0
    tol_atr: float = 0.1
    n_a: int = 3
    n_f: int = 3
    n_time: int = 24
    e4_depth_atr: tuple[float, float] = (0.5, 2.5)
    e4_recovery: float = 0.5
    range_max_pivots: int = 8
    e5_edge_atr: float = 0.75
    merge_max_atr: float = 0.0
    use_balance_detector: bool = False
    balance_quality_min: float = 0.25
    box_min_rotations: int = 1
    box_keep_broken_bars: int = 12
    box_keep_active_bars: int = 24
    accept_max_ext_atr: float = 1.50
    box_reentry_enabled: bool = False
    box_reentry_max_bars: int = 96

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "Params":
        """从 JSON 配置文件加载结构参数;键名大小写与下划线归一化绑定。"""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        keys_by_id: dict[str, list[str]] = {}
        for key in raw:
            keys_by_id.setdefault(_key_id(key), []).append(key)
        wanted: dict = {}
        for name in cls.__dataclass_fields__:
            hits = keys_by_id.get(_key_id(name), [])
            if not hits:
                continue
            if len(hits) > 1:
                raise ValueError(
                    f"{path}: 键 {sorted(hits)} 都指向字段 {name!r},只能保留一个"
                )
            wanted[name] = raw[hits[0]]
        if "e4_depth_atr" in wanted:
            wanted["e4_depth_atr"] = tuple(wanted["e4_depth_atr"])
        for k in (
            "atr_n", "n_a", "n_f", "n_time", "range_max_pivots",
            "box_min_rotations", "box_keep_broken_bars",
            "box_keep_active_bars", "box_reentry_max_bars",
        ):
            if k in wanted:
                wanted[k] = int(wanted[k])
        return cls(**wanted)



# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


class PivotKind(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class Pivot:
    """确认的波段端点(§3)。forming_index 为极值发生 bar,known_index 为确认 bar。"""

    price: float
    forming_index: int
    known_index: int
    kind: PivotKind
    close_extreme: float = 0.0   # v21:极值 bar 的收盘(失败延伸判据用)


class ZoneRole(str, Enum):
    SUPPORT = "support"
    RESISTANCE = "resistance"


class ZoneState(str, Enum):
    ACTIVE = "ACTIVE"
    BROKEN = "BROKEN"
    RETESTED = "RETESTED"
    FLIPPED = "FLIPPED"
    DEAD = "DEAD"


@dataclass
class Zone:
    """关键价格区域(§4)。属性为事实记录(§4.3:不设评分)。"""

    lo: float
    hi: float
    role: ZoneRole
    state: ZoneState = ZoneState.ACTIVE
    touches: int = 0
    rejects: int = 0
    origin_known: int = 0        # origin_known_index:最早来源 pivot 的知时(§4.1)
    last_change_known: int = 0   # 最近一次状态/属性变化的知时
    merged: int = 1              # 合并来源数(§4.1)
    _retest_extreme: Optional[float] = None  # BROKEN→RETESTED 后向正面最深回踩极值(§4.2)
    box_opposite: Optional[float] = None     # v21.4:平衡箱对侧边界(REJECT 均值回归目标锚)
    box_quality: float = 0.0                 # v21.5:平衡箱质量分
    box_width_atr: float = 0.0               # v21.5:平衡箱宽度(ATR 倍数)
    box_rotations: int = 0                   # v21.5:平衡箱来回旋转次数
    source_kind: str = "BOX_EDGE"            # "BOX_EDGE" | "FLIPPED_BOX_EDGE"

    def clone(self) -> "Zone":
        return Zone(
            lo=self.lo, hi=self.hi, role=self.role, state=self.state,
            touches=self.touches, rejects=self.rejects,
            origin_known=self.origin_known,
            last_change_known=self.last_change_known, merged=self.merged,
            _retest_extreme=self._retest_extreme,
            box_opposite=self.box_opposite,
            box_quality=self.box_quality,
            box_width_atr=self.box_width_atr,
            box_rotations=self.box_rotations,
            source_kind=self.source_kind,
        )


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"


@dataclass(frozen=True)
class ZoneFlip:
    """一只 zone 在本 bar 转为 FLIPPED 的事实记录(§4.2;v2 扩展,v2-protocol §1)。

    `zone` 为翻转生成的新角色区域的快照(`_insert_merged` 合并前):边界即旧区
    边界、`role` 为角色互换后的新角色(role_new),touches/rejects/origin/merged
    继承自旧区。旧区的 face 语义以 `from_role` 保留(结构移除后仍可追溯,A10)。
    """

    index: int              # 转移知时 bar(= on_bar 的 index)
    from_role: ZoneRole     # 旧区角色
    zone: Zone              # 新角色区快照(role = role_new,合并前边界 = 旧区边界)


Bar = Union[tuple, list, dict, pd.Series]


def _bar_parts(bar: Bar) -> tuple:
    """只读提取 (ts, open, high, low, close, volume);元组序即契约列序。"""
    if isinstance(bar, pd.Series):
        return (bar["ts"], float(bar["open"]), float(bar["high"]),
                float(bar["low"]), float(bar["close"]), float(bar["volume"]))
    if isinstance(bar, dict):
        return (bar["ts"], float(bar["open"]), float(bar["high"]),
                float(bar["low"]), float(bar["close"]), float(bar["volume"]))
    t, o, h, l, c, v = bar
    return t, float(o), float(h), float(l), float(c), float(v)


# ---------------------------------------------------------------------------
# ATR(§2,Wilder 1978)
# ---------------------------------------------------------------------------


class ATR:
    """Wilder 平滑 ATR。前 n 根不可用(值 0.0);第 n 根为 tr 均值,其后递推。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self._value: float = 0.0
        self._seed_sum: float = 0.0
        self._bars: int = 0
        self._prev_close: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close),
                     abs(low - self._prev_close))
        self._bars += 1
        if self._bars <= self.n:
            self._seed_sum += tr
            self._value = self._seed_sum / self.n if self._bars == self.n else 0.0
        else:
            self._value = (self._value * (self.n - 1) + tr) / self.n
        self._prev_close = close
        return self._value

    @property
    def value(self) -> float:
        return self._value


# ---------------------------------------------------------------------------
# SwingTracker(§3,确认驱动波段)
# ---------------------------------------------------------------------------


class SwingTracker:
    """事件驱动状态机。方向由首次达到 k_rev*atr 的推进决定(§3 初始化)。"""

    def __init__(self, k_rev: float) -> None:
        self.k_rev = k_rev
        self.direction: Optional[int] = None  # +1 up / -1 down(当前腿)
        self.ext_price: float = 0.0
        self.ext_index: int = 0
        self._anchor_price: float = 0.0  # 未定向时的参考价(bar0 close)

    def update(self, index: int, o: float, h: float, l: float, c: float,
               atr_val: float) -> Optional[Pivot]:
        if index == 0:
            self._anchor_price = c
        if atr_val <= 0.0:
            return None

        if self.direction is None:
            # §3 初始化:第一腿方向 = 先走出 k_rev*atr 推进的方向;
            # 同一根双向同时满足时按 §8.4 路径启发式(close < open → 先高,取 up)。
            up_push = h - self._anchor_price >= self.k_rev * atr_val
            down_push = self._anchor_price - l >= self.k_rev * atr_val
            if up_push and (not down_push or c < o):
                self.direction = +1
                self.ext_price, self.ext_index = h, index
            elif down_push:
                self.direction = -1
                self.ext_price, self.ext_index = l, index
            return None

        rev = self.k_rev * atr_val
        if self.direction > 0:
            # 确认只对本 bar 之前的极值判定:本 bar 不做"先推进极值再确认"的同根
            # 处理(见模块 docstring §3/§8.4 条款注),保证 known_index > forming_index。
            if self.ext_price - l >= rev:
                pivot = Pivot(self.ext_price, self.ext_index, index, PivotKind.HIGH)
                self.direction = -1
                self.ext_price, self.ext_index = l, index
                return pivot
            if h > self.ext_price:
                self.ext_price, self.ext_index = h, index
        else:
            if h - self.ext_price >= rev:
                pivot = Pivot(self.ext_price, self.ext_index, index, PivotKind.LOW)
                self.direction = +1
                self.ext_price, self.ext_index = h, index
                return pivot
            if l < self.ext_price:
                self.ext_price, self.ext_index = l, index
        return None


# ---------------------------------------------------------------------------
# ZoneBook(§4:生成、合并、生命周期状态机)
# ---------------------------------------------------------------------------


class ZoneBook:
    """区域集合与生命周期管理。

    同一 bar 的因果序:先生成/合并新 pivot 区域,再按本 bar 遍历状态机
    (触碰、破位、远侧回踩、回踩拒绝翻转/穿回消亡)。
    终止态(FLIPPED/DEAD)在该 bar 处理结束后移出 ``zones`` 视图。
    """

    def __init__(self, params: Params) -> None:
        self.p = params
        self.zones: list[Zone] = []
        self.flips: list[ZoneFlip] = []
        self._bal_det: Optional[TrueBalanceDetector] = None
        if params.use_balance_detector:
            self._bal_det = TrueBalanceDetector(
                TrueBalanceParams(
                    quality_min=params.balance_quality_min,
                    keep_flipped_bars=params.box_reentry_max_bars,
                    reentry_enabled=params.box_reentry_enabled,
                )
            )

    def on_pivot(self, pivot: Pivot, known: int, atr_val: float) -> None:
        """确认 pivot 生成区域:阻力=[P-k_zw*atr, P],支撑=[P, P+k_zw*atr]。"""
        if self.p.use_balance_detector:
            return
        w = self.p.k_zw * atr_val
        if pivot.kind is PivotKind.HIGH:
            zone = Zone(
                lo=pivot.price - w, hi=pivot.price, role=ZoneRole.RESISTANCE,
                touches=1, origin_known=known, last_change_known=known,
            )
        else:
            zone = Zone(
                lo=pivot.price, hi=pivot.price + w, role=ZoneRole.SUPPORT,
                touches=1, origin_known=known, last_change_known=known,
            )
        self._insert_merged(zone, known, atr_val)


    def _insert_merged(self, new: Zone, known: int, atr_val: float = 0.0) -> None:
        """与现存同向区域合并:并集边界、touches 相加、origin 取最早。"""
        cap = (
            self.p.merge_max_atr * atr_val
            if (self.p.merge_max_atr > 0.0 and atr_val > 0.0)
            else 0.0
        )
        changed = True
        while changed:
            changed = False
            for i, z in enumerate(self.zones):
                if z.role != new.role or not (z.lo <= new.hi and new.lo <= z.hi):
                    continue
                union_lo, union_hi = min(z.lo, new.lo), max(z.hi, new.hi)
                if cap > 0.0 and (union_hi - union_lo) > cap:
                    continue
                new = Zone(
                    lo=union_lo, hi=union_hi, role=new.role,
                    state=ZoneState.ACTIVE,
                    touches=z.touches + new.touches,
                    rejects=z.rejects + new.rejects,
                    origin_known=min(z.origin_known, new.origin_known),
                    last_change_known=known,
                    merged=z.merged + new.merged,
                )
                self.zones.pop(i)
                changed = True
                break
        self.zones.append(new)


    # -- §4.2 状态机 ---------------------------------------------------------

    def _sync_balance_boxes(
        self, index: int, ts: object, o: float, h: float, l: float, c: float,
        v: float, atr_val: float
    ) -> None:
        """将 TrueBalanceDetector 识别出的平衡箱上下沿同步桥接为支撑/阻力区域。"""
        bracket = self._bal_det.push((ts, o, h, l, c, v), atr_val)
        if atr_val <= 0.0 or bracket is None:
            return
        tol = max(self.p.tol_atr * atr_val, 0.5 * atr_val)
        alive_ids: set[int] = set()
        for role, lvl in (
            (ZoneRole.SUPPORT, bracket.support),
            (ZoneRole.RESISTANCE, bracket.resistance),
        ):
            if lvl is None or int(lvl.rotations) < self.p.box_min_rotations:
                continue
            sk = getattr(lvl, "source_kind", "BOX_EDGE")
            if sk != "BOX_EDGE" and not (self.p.box_reentry_enabled and sk == "FLIPPED_BOX_EDGE"):
                continue

            zlo = float(min(lvl.outer, lvl.inner))
            zhi = float(max(lvl.outer, lvl.inner))
            opp = float(lvl.opposite) if lvl.opposite is not None else None
            qual = float(lvl.quality)
            w_atr = float(lvl.width_atr)
            rot = int(lvl.rotations)
            matched = None
            for z in self.zones:
                if z.role is not role:
                    continue
                if z.state not in (ZoneState.ACTIVE, ZoneState.BROKEN, ZoneState.RETESTED):
                    continue
                ref_edge = z.hi if role is ZoneRole.RESISTANCE else z.lo
                new_edge = zhi if role is ZoneRole.RESISTANCE else zlo
                in_edge = z.lo if role is ZoneRole.RESISTANCE else z.hi
                new_in = zlo if role is ZoneRole.RESISTANCE else zhi
                if abs(ref_edge - new_edge) <= tol or abs(in_edge - new_in) <= tol:
                    matched = z
                    break
            if matched is not None:
                matched.lo = zlo
                matched.hi = zhi
                bm_now = self.p.k_bm * atr_val
                still_out = (
                    (c > zhi + bm_now) if role is ZoneRole.RESISTANCE else (c < zlo - bm_now)
                )
                if bracket.mode == "IN_BOX" and not still_out and matched.state is not ZoneState.ACTIVE:
                    matched.state = ZoneState.ACTIVE
                    matched._retest_extreme = None
                matched.last_change_known = index
                matched.source_kind = sk
                if opp is not None:
                    matched.box_opposite = opp
                matched.box_quality = max(matched.box_quality, qual)
                if w_atr > 0.0:
                    matched.box_width_atr = w_atr
                matched.box_rotations = max(matched.box_rotations, rot)
                alive_ids.add(id(matched))
            else:
                nz = Zone(
                    lo=zlo, hi=zhi, role=role,
                    touches=max(1, rot),
                    origin_known=int(getattr(lvl, "origin_index", index)),
                    last_change_known=index,
                    box_opposite=opp,
                    box_quality=qual,
                    box_width_atr=w_atr,
                    box_rotations=rot,
                    source_kind=sk,
                )
                self.zones.append(nz)
                alive_ids.add(id(nz))

        active_by_role: dict[ZoneRole, Zone] = {}
        other_zones: list[Zone] = []
        for z in self.zones:
            if z.state is ZoneState.ACTIVE:
                if id(z) in alive_ids or (index - z.last_change_known <= self.p.box_keep_active_bars):
                    prev_z = active_by_role.get(z.role)
                    if (
                        prev_z is None
                        or (id(z) in alive_ids and id(prev_z) not in alive_ids)
                        or z.last_change_known >= prev_z.last_change_known
                    ):
                        active_by_role[z.role] = z
            elif index - z.last_change_known <= self.p.box_keep_broken_bars:
                other_zones.append(z)
        self.zones = list(active_by_role.values()) + other_zones

    def on_bar(
        self, index: int, o: float, h: float, l: float, c: float,
        prev_close: Optional[float], atr_val: float,
        ts: object = 0, v: float = 0.0
    ) -> None:
        if self._bal_det is not None:
            self._sync_balance_boxes(index, ts, o, h, l, c, v, atr_val)
        if atr_val <= 0.0:
            return
        self.flips = []
        for z in list(self.zones):
            self._advance(z, index, h, l, c, prev_close, atr_val)
        self.zones = [
            z for z in self.zones
            if z.state not in (ZoneState.FLIPPED, ZoneState.DEAD)
        ]


    def _advance(self, z: Zone, index: int, h: float, l: float, c: float,
                 prev_close: Optional[float], atr_val: float) -> None:
        up = z.role is ZoneRole.RESISTANCE   # A1:阻力正面=下侧,远侧=上侧;支撑镜像
        bm = self.p.k_bm * atr_val

        if z.state is ZoneState.ACTIVE:
            # A2 触碰:bar [low,high] 与带相交,且前一根收盘在正面之外
            if prev_close is not None and h >= z.lo and l <= z.hi:
                outside = (prev_close < z.lo) if up else (prev_close > z.hi)
                if outside:
                    z.touches += 1
                    z.last_change_known = index
            # acceptance 破位(§4.2):收盘越过外缘 + k_bm*atr → BROKEN
            if (c > z.hi + bm) if up else (c < z.lo - bm):
                z.state = ZoneState.BROKEN
                z._retest_extreme = None
                z.last_change_known = index
            return

        if z.state is ZoneState.BROKEN:
            # 首次从远侧返回带内(§4.2;A1 远侧:阻力=上,支撑=下)→ RETESTED
            back = (l <= z.hi) if up else (h >= z.lo)
            if back:
                z.state = ZoneState.RETESTED
                z._retest_extreme = l if up else h
                z.last_change_known = index
                self._retest(z, index, h, l, c, atr_val)  # 同根 bar 继续判回踩结果
            return

        if z.state is ZoneState.RETESTED:
            self._retest(z, index, h, l, c, atr_val)
            return

        # FLIPPED / DEAD:终止态,由 on_bar 收尾移除(模块 docstring,A10)

    def _retest(self, z: Zone, index: int, h: float, l: float, c: float,
                atr_val: float) -> None:
        up = z.role is ZoneRole.RESISTANCE
        bm = self.p.k_bm * atr_val
        rj = self.p.k_rj * atr_val
        ex = z._retest_extreme
        ex = min(ex, l) if up else max(ex, h)
        z._retest_extreme = ex
        # v22:当开启真平衡箱检测时,若当根直接反向砸穿回内沿以内,属于假突破收复而非回踩拒绝翻转
        if self.p.use_balance_detector and ((c < z.lo) if up else (c > z.hi)):
            if index - z.last_change_known > 2:
                z.state = ZoneState.DEAD
                z.last_change_known = index
            return
        # 回踩被拒绝(§4.2,回退度量同 A6):自回踩极值向远侧回退 ≥ k_rj*atr

        # → FLIPPED:旧区 DEAD,同边界反向新区继承 touches/rejects/origin
        if ((h - ex >= rj) or (c - ex >= rj)) if up else ((ex - l >= rj) or (ex - c >= rj)):
            flipped = Zone(
                lo=z.lo, hi=z.hi,
                role=ZoneRole.SUPPORT if up else ZoneRole.RESISTANCE,
                state=ZoneState.ACTIVE,
                touches=z.touches, rejects=z.rejects,
                origin_known=z.origin_known, last_change_known=index,
                merged=z.merged,
            )
            z.state = ZoneState.FLIPPED
            z.last_change_known = index
            self.flips.append(ZoneFlip(index=index, from_role=z.role,
                                       zone=flipped.clone()))
            if not self.p.use_balance_detector:
                self._insert_merged(flipped, index, atr_val)
            return
        # 回踩后反向重新 acceptance 穿回(§4.2)→ DEAD(不生成新区)
        if (c < z.lo - bm) if up else (c > z.hi + bm):
            z.state = ZoneState.DEAD
            z.last_change_known = index


# ---------------------------------------------------------------------------
# Regime(§5,查询式,由当前 confirmed pivots 推出)
# ---------------------------------------------------------------------------


def _regime_from(pivots: list[Pivot], atr_val: float, p: Params,
                 close: Optional[float]) -> Regime:
    if atr_val <= 0.0:
        return Regime.TRANSITION
    recent = pivots[-p.range_max_pivots:]           # §6A-A8 窗口
    if len(recent) < 4:
        return Regime.TRANSITION
    highs = [pv.price for pv in recent if pv.kind is PivotKind.HIGH]
    lows = [pv.price for pv in recent if pv.kind is PivotKind.LOW]
    if len(highs) < 2 or len(lows) < 2:
        return Regime.TRANSITION
    tol = p.tol_atr * atr_val
    bm = p.k_bm * atr_val
    up_struct = highs[-1] > highs[-2] + tol and lows[-1] > lows[-2] + tol
    dn_struct = highs[-1] < highs[-2] - tol and lows[-1] < lows[-2] - tol
    # §5:"且价格未破坏最近抬升低点(见 STRUCTURE_BROKEN)"(以当前收盘判定,镜像同理)
    if up_struct and (close is None or close >= lows[-1] - bm):
        return Regime.TREND_UP
    if dn_struct and (close is None or close <= highs[-1] + bm):
        return Regime.TREND_DOWN
    # §5 RANGE:无递升/递降结构且总跨度 ≤ k_span*atr(A8 合成带口径)
    if not up_struct and not dn_struct:
        span = max(highs) - min(lows)
        if span <= p.k_span * atr_val:
            return Regime.RANGE
    return Regime.TRANSITION


# ---------------------------------------------------------------------------
# MarketStructure:增量式认知核心
# ---------------------------------------------------------------------------


class MarketStructure:
    """逐 bar 增量市场结构认知核心。"""

    def __init__(self, params: Params) -> None:
        self.params = params
        self._atr = ATR(params.atr_n)
        self._swing = SwingTracker(params.k_rev)
        self._zones = ZoneBook(params)
        self._pivots: list[Pivot] = []
        self._pivots_tuple: tuple[Pivot, ...] = ()
        self.last_high_pivot: Optional[Pivot] = None
        self.last_low_pivot: Optional[Pivot] = None
        self._bars: list[tuple] = []
        self._regime = Regime.TRANSITION

    def update(self, bar: Bar) -> None:
        ts, o, h, l, c, v = _bar_parts(bar)
        index = len(self._bars)
        prev_close = self._bars[-1][4] if self._bars else None
        atr_val = self._atr.update(h, l, c)
        self._bars.append((ts, o, h, l, c, v))

        pivot = self._swing.update(index, o, h, l, c, atr_val)
        if pivot is not None:
            pivot = Pivot(
                price=pivot.price,
                forming_index=pivot.forming_index,
                known_index=pivot.known_index,
                kind=pivot.kind,
                close_extreme=float(self._bars[pivot.forming_index][4]),
            )
            self._pivots.append(pivot)
            self._pivots_tuple = tuple(self._pivots)
            if pivot.kind is PivotKind.HIGH:
                self.last_high_pivot = pivot
            else:
                self.last_low_pivot = pivot
            self._zones.on_pivot(pivot, pivot.known_index, atr_val)
        self._zones.on_bar(index, o, h, l, c, prev_close, atr_val, ts=ts, v=v)
        self._regime = _regime_from(self._pivots, atr_val, self.params, c)

    @property
    def pivots(self) -> tuple[Pivot, ...]:
        return self._pivots_tuple

    @property
    def zones(self) -> tuple[Zone, ...]:
        return tuple(z.clone() for z in self._zones.zones)

    def atr(self) -> float:
        return self._atr.value

    def regime(self) -> Regime:
        return self._regime

    @property
    def bars_seen(self) -> int:
        return len(self._bars)


def replay(df: pd.DataFrame, params: Optional[Params] = None):
    """逐 bar 回放结构与事件检测,返回 (structure, events_list)。"""
    from .events import EventDetector

    if params is None:
        params = Params()
    detector = EventDetector(params)
    events: list = []
    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        bar = (row.ts, row.open, row.high, row.low, row.close, row.volume)
        events.extend(detector.update(bar))
    return detector.structure, events
