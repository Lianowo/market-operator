"""市场认知核心:ATR、确认波段、区域生命周期、状态(regime)。

实现 docs/methodology.md §2(ATR)、§3(SwingTracker)、§4(Zone)、§5(Regime),
知时纪律遵循 §0.3 与 §6A-A10:任何事实只在 `known_index <= t` 时参与认知。

解释性决定(方法论文字未完全展开处,取最符合因果语义的读法,标注条款):
- [§2/§3] ATR 在 bar t 报告用 `tr(t)` 更新后的平滑值(§3 确认判据即在 bar t 使用
  `atr(t)`);前 `atr_n` 根 ATR 不可用(返回 0.0),期间不确认 pivot(确认需 `k_rev*atr`
  阈值,阈值为正才能判断"达到")。
- [§3/§8.4] 超宽 bar 的处理:SwingTracker 先对本 bar 之前的极值判确认,再考虑以
  本 bar 极值推进——bar 内路径不可知,而 §0.3/A10 要求 forming_index 指向严格的历史
  位置,此读法保证 `known_index > forming_index` 恒成立(测试断言);代价是同根新高
  不会被记为上一腿的极值(保守、宁可延迟成形也不提前知时)。§8.4 的路径启发式
  (close < open 视为先高后低)仍用于事件检测层 LEVEL_REJECTED 的同根"ex 建立→回退"次序
  (见 events.py)与 SwingTracker 未定向时的首腿定向。
- [§4.1] "origin 保留最早者"取最早 `origin_known_index`;新 pivot 生成的区域自带
  touches=1(波段端点处的市场反应即该区域的第一次触碰,§4 开头"市场曾经在那里给出
  过明确反应")。
- [§4.1] 新区域与现存同向区域重叠时以并集收敛(可链式搭接);被并入的非 ACTIVE 区域
  重新激活为 ACTIVE(并集区间取代旧生命周期,即 A3"zone 返回 ACTIVE 开始新 episode")。
- [§4.2] 状态机按冻结图实现:BROKEN 后"首次从远侧返回带内"= 之后任一根 bar 的
  [low,high] 与带相交(破位收盘本身已接受于远侧,其后相交必来自远侧,A1);
  "回踩被拒绝"以 A6 同语义度量:自回踩最深极值向远侧回退 ≥ k_rj*atr(收盘或极值
  先到),成立则 FLIPPED:旧区 DEAD,同边界反向新区继承 touches/rejects/origin;
  "回踩后反向重新 acceptance 穿回"(收盘越过正缘 k_bm*atr)则旧区 DEAD,不生成新区。
  终止态(FLIPPED/DEAD)在发生 bar 处理完毕后即从 `zones` 视图移除,
  其继承事实由新生反向区携带(A10:知时即刻生效)。
- [§5] "价格未破坏最近抬升低点(见 STRUCTURE_BROKEN)"纳入 regime 谓词,以当前 bar 收盘判定:
  收盘已破坏最近 HL/LH 则不报告趋势。因此 STRUCTURE_BROKEN 恰好发射于 regime 退出趋势的那一根
  (STRUCTURE_BROKEN 检测器以 `regime(t-1)` 为"TREND_UP/…中"之判定,见 events.py)。
- [§5/§6A-A8] 趋势与区间结构统一由最近至多 `range_max_pivots` 个 confirmed pivots 判定
  (A8 明示 RANGE 窗口;趋势取同一窗口内的最近两个高/低点序列,即"最近 4+ 个交错
  pivot"的冻结读法)。
- [v2] ZoneBook.flips:每根 bar 的 FLIPPED 角色互换事实记录(见 ZoneFlip)。只读追加,
  不改变任何生命周期判断与视图移除时序(v1 行为不变);供 v2 事件 FLIP_RETESTED 使用,机制
  定义见 docs/v2-protocol.md §1。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union

import pandas as pd


# ---------------------------------------------------------------------------
# 参数(§2-§6 先验,来自配置文件,冻结)
# ---------------------------------------------------------------------------


def _key_id(key: str) -> str:
    """配置键的归一形式:大小写与下划线都不参与匹配(``N_a`` ≡ ``n_a``)。"""
    return key.replace("_", "").lower()


@dataclass(frozen=True)
class Params:
    atr_n: int = 14                      # §2 ATR(14, Wilder)
    k_rev: float = 1.0                   # §3 反转确认幅度(ATR 倍数)
    k_zw: float = 0.5                    # §4.1 区域半宽(ATR 倍数)
    k_bm: float = 0.1                    # §4.2 acceptance 破位余量(ATR 倍数)
    k_rj: float = 0.3                    # §6 LEVEL_REJECTED / §4.2 回踩拒绝回退幅度(ATR 倍数)
    k_span: float = 3.0                  # §5 RANGE 总跨度上限(ATR 倍数)
    k_room: float = 2.0                  # §6 RANGE_ROTATED 对侧空间下限(ATR 倍数)
    tol_atr: float = 0.1                 # §5 pivot 相等容差(ATR 倍数)
    n_a: int = 3                         # §6 BREAKOUT_ACCEPTED 连续接受根数 N_a
    n_f: int = 3                         # §6 BREAKOUT_FAILED/LEVEL_REJECTED/TREND_RESUMED 窗口 N_f
    n_time: int = 24                     # §7 时间失效(认知层不用,契约保留)
    e4_depth_atr: tuple = (0.5, 2.5)     # §6A-A7 TREND_RESUMED 回调深度区间
    e4_recovery: float = 0.5             # §6A-A7 TREND_RESUMED 回收比例
    range_max_pivots: int = 8            # §6A-A8 结构判定的最多 pivot 数
    e5_edge_atr: float = 0.75            # §6A-A8 带边缘容差(ATR 倍数)
    merge_max_atr: float = 0.0           # 区域并集宽度上限(ATR 倍数;0=无界,历史默认)
    stale_after_bars: int = 0            # v21:区域年龄过期(距上次变化 bar 数;0=关,历史)
    min_amplitude_atr: float = 0.0       # v21:仅腿位移幅度≥阈值的 pivot 造区(ATR;0=关)
    use_same_balance: bool = False       # v21:合并用 same_balance 去重(替代 重叠+封顶)
    use_balance_recognition: bool = False  # v21:仅"失败延伸"pivot 造区(替代 每个pivot)
    use_balance_detector: bool = False   # v21.3:用 BalanceDetector 箱上下沿替换 pivot 造区
    balance_quality_min: float = 0.25    # v21.3:BalanceDetector 质量门槛
    box_min_rotations: int = 1           # 箱沿采纳下限(顶底来回次数;1=至少一次反转)
    box_keep_broken_bars: int = 12       # 非 ACTIVE 边缘保留根数(破位后供回踩确认)
    box_keep_active_bars: int = 24       # ACTIVE 边缘保留根数(未破位但久未变化)
    # 结构确认(替代"振幅/连续根数"这套代理指标):
    # REJECT 只有在「试探极值之外先形成一个反向局部极值,且收盘重新夺回该极值」时
    # 才成立 —— 支撑拒绝=夺回试探后的局部**高**点;阻力拒绝=跌破试探后的局部**低**点。
    # 这是"试探失败、原方向重新取得主动权"的最小可观测结构事实。
    struct_confirm_reject: bool = False
    # 突破接受(ACCEPT)第 N_a 根确认收盘距箱沿的最大允许透支上限(ATR 倍数,默认 1.50)。
    # 当开启突破系统价格关注(accept_watch_enabled)时,可适度放宽该上限(如 2.2~2.5),
    # 把"看见突破晚了(>1.5 ATR)"的候选接回系统 Watch 等回踩箱沿,而非一刀切永久丢弃。
    accept_max_ext_atr: float = 1.50
    # 降档触发层(trigger.py)同步 R3_TRAP(1~2 根假突破未超 1.25·ATR 逃逸后反向吞没重返箱内)
    mic_r3_trap: bool = False
    # 80% 法则:允许价格从箱外重新突破进入近期已破平衡箱(恢复箱体并桥接翻转边界)
    box_reentry_enabled: bool = False
    box_reentry_max_bars: int = 96
    # ACCEPT 破位过程完整性:首根破位 bar 须顺向强收盘,且确认期间影线不跌穿被破箱沿内侧
    accept_strict_progression: bool = False


    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "Params":
        """从 JSON 加载结构先验;键名风格不参与匹配。

        键按 ``去掉下划线 + 忽略大小写`` 归一,所以 ``N_a``/``n_a``/``NA`` 等价。
        历史上配置写的是大写 ``N_a``/``N_f``/``N_time``,而字段是小写,于是这三个
        键被静默丢弃、从未生效(20 份配置全部如此,只因取值恰好等于默认值才没
        暴露)。现在两种写法都绑定;若同一字段被多个键指向,直接报错而不是替
        调用方挑一个。

        不认识任何字段的键忽略:同一份 JSON 还供路由/经纪商/场景门面读取。
        """
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        keys_by_id: dict = {}
        for key in raw:
            keys_by_id.setdefault(_key_id(key), []).append(key)
        wanted: dict = {}
        for name in cls.__dataclass_fields__:
            hits = keys_by_id.get(_key_id(name), [])
            if not hits:
                continue
            if len(hits) > 1:
                raise ValueError(
                    f"{path}: 键 {sorted(hits)} 都指向字段 {name!r},只能保留一个")
            wanted[name] = raw[hits[0]]
        if "e4_depth_atr" in wanted:
            wanted["e4_depth_atr"] = tuple(wanted["e4_depth_atr"])
        for k in ("atr_n", "n_a", "n_f", "n_time", "range_max_pivots",
                  "box_min_rotations", "box_keep_broken_bars",
                  "box_keep_active_bars", "box_reentry_max_bars"):
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
    """区域集合与生命周期,全部以已知时生效(A10)。

    同一 bar 的因果序:先生成/合并新 pivot 区域(§4.1),再按本 bar 遍历状态机
    (A2 触碰、§4.2 acceptance 破位、远侧回踩、回踩拒绝翻转/穿回消亡)。
    终止态(FLIPPED/DEAD)在该 bar 处理结束后移出 `zones` 视图(见模块 docstring)。
    """

    def __init__(self, params: Params) -> None:
        self.p = params
        self.zones: list[Zone] = []
        self.flips: list[ZoneFlip] = []   # 最近一根 bar 的 FLIPPED 事实(v2 只读记录)
        self._last_high_price: Optional[float] = None   # v21 失败延伸判据(影线价)
        self._last_low_price: Optional[float] = None
        self._last_high_close: Optional[float] = None   # v21 边缘锚定(收盘 core)
        self._last_low_close: Optional[float] = None
        self._bal_det = None
        if params.use_balance_detector:
            from .true_balance import TrueBalanceDetector, TrueBalanceParams
            self._bal_det = TrueBalanceDetector(
                TrueBalanceParams(quality_min=params.balance_quality_min,
                                  keep_flipped_bars=params.box_reentry_max_bars,
                                  reentry_enabled=params.box_reentry_enabled))


    # -- §4.1 生成与合并 ----------------------------------------------------

    def on_pivot(self, pivot: Pivot, known: int, atr_val: float,
                 amplitude_atr: Optional[float] = None) -> None:
        """confirmed pivot 生成区域:阻力=[P-k_zw*atr, P],支撑=[P, P+k_zw*atr](知时 atr)。

        v21 显著性过滤:min_amplitude_atr>0 时,腿位移幅度不足的 pivot 不造区
        (噪声摆动不产生结构;edge-engine sig_amplitude_atr 同构)。
        """
        if self.p.use_balance_detector:
            return
        if (self.p.min_amplitude_atr > 0 and amplitude_atr is not None
                and amplitude_atr < self.p.min_amplitude_atr):
            return
        # v21 失败延伸识别(平衡箱 → 两个单带区):仅"未收盘突破前一个同类极值"的
        # pivot 造箱,箱 = [下缘, 上缘] → 支撑区(下缘) + 阻力区(上缘),映射回单带
        # 接口供 events/hypothesis 消费(角色翻转交由既有 ACTIVE→FLIPPED 生命周期)。
        if self.p.use_balance_recognition:
            tol = self.p.tol_atr * atr_val
            w = self.p.k_zw * atr_val
            if pivot.kind is PivotKind.HIGH:
                failed = (self._last_high_price is not None
                          and pivot.close_extreme <= self._last_high_price + tol)
                if failed and self._last_low_close is not None:
                    ce = pivot.close_extreme  # 边缘锚在收盘(core),影线不参与
                    self._insert_merged(Zone(lo=ce - w, hi=ce,
                                             role=ZoneRole.RESISTANCE, touches=1,
                                             origin_known=known, last_change_known=known),
                                        known, atr_val)
                    self._insert_merged(Zone(lo=self._last_low_close,
                                             hi=self._last_low_close + w,
                                             role=ZoneRole.SUPPORT, touches=1,
                                             origin_known=known, last_change_known=known),
                                        known, atr_val)
                self._last_high_price = pivot.price
                self._last_high_close = pivot.close_extreme
                return
            else:
                failed = (self._last_low_price is not None
                          and pivot.close_extreme >= self._last_low_price - tol)
                if failed and self._last_high_close is not None:
                    ce = pivot.close_extreme
                    self._insert_merged(Zone(lo=ce, hi=ce + w,
                                             role=ZoneRole.SUPPORT, touches=1,
                                             origin_known=known, last_change_known=known),
                                        known, atr_val)
                    self._insert_merged(Zone(lo=self._last_high_close - w,
                                             hi=self._last_high_close,
                                             role=ZoneRole.RESISTANCE, touches=1,
                                             origin_known=known, last_change_known=known),
                                        known, atr_val)
                self._last_low_price = pivot.price
                self._last_low_close = pivot.close_extreme
                return
        w = self.p.k_zw * atr_val
        if pivot.kind is PivotKind.HIGH:
            zone = Zone(lo=pivot.price - w, hi=pivot.price, role=ZoneRole.RESISTANCE,
                        touches=1, origin_known=known, last_change_known=known)
        else:
            zone = Zone(lo=pivot.price, hi=pivot.price + w, role=ZoneRole.SUPPORT,
                        touches=1, origin_known=known, last_change_known=known)
        self._insert_merged(zone, known, atr_val)

    def _same_balance(self, a: Zone, b: Zone, tol: float) -> bool:
        """edge-engine 去重判据(zones.py same_balance):共享覆盖 > 80% 合并跨度,
        或高度相近且一方含另一方 —— "交易者只会画一次的箱",相邻/重叠的重复箱合并。
        """
        shared = min(a.hi, b.hi) - max(a.lo, b.lo)
        span = max(a.hi, b.hi) - min(a.lo, b.lo)
        if shared <= 0 or span <= 0:
            return False
        ha, hb = a.hi - a.lo, b.hi - b.lo
        similar = min(ha, hb) > 0.8 * max(ha, hb)
        inside = ((b.lo - tol <= a.lo and a.hi <= b.hi + tol)
                  or (a.lo - tol <= b.lo and b.hi <= a.hi + tol))
        return shared > 0.8 * span or (similar and inside)

    def _insert_merged(self, new: Zone, known: int, atr_val: float = 0.0) -> None:
        """与现存同向区域合并:并集边界、touches 相加、origin 取最早(§4.1)。

        并集可能搭接到下一个同向区域,以不动点方式收敛(保持前缀不变性)。

        合并判据(二选一):
        - 历史(use_same_balance=False):几何重叠 + merge_max_atr 宽度封顶
          (research-log #15,防巨区吞细结构);
        - v21(use_same_balance=True):same_balance 去重(共享>80%跨度 或 高度相近
          且包含),不设宽度封顶 —— 去重判据本身即防链式并集,相邻不同水平不并。
        """
        cap = self.p.merge_max_atr * atr_val if (self.p.merge_max_atr > 0 and atr_val > 0) else 0.0
        tol = self.p.tol_atr * atr_val if atr_val > 0 else 0.0
        changed = True
        while changed:
            changed = False
            for i, z in enumerate(self.zones):
                if z.role != new.role:
                    continue
                if self.p.use_same_balance:
                    if not self._same_balance(z, new, tol):
                        continue
                elif not (z.lo <= new.hi and new.lo <= z.hi):
                    continue
                union_lo, union_hi = min(z.lo, new.lo), max(z.hi, new.hi)
                if (cap > 0 and (union_hi - union_lo) > cap
                        and not self.p.use_same_balance):
                    continue  # 并集会超上限:留下原区,不吞并细结构
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

    def _sync_balance_boxes(self, index: int, ts: object, o: float, h: float,
                            l: float, c: float, v: float, atr_val: float) -> None:
        """v21.3:将 BalanceDetector 识别出的平衡箱上下沿映射为 R/S 区域。

        采纳下限与保留窗口**全部取自 ``Params``**——不在注释里写死数字。写死的
        数字曾经与代码漂移(注释说 rotations≥2 / 60·120 bars,代码是 1 / 12·24),
        于是任何"为什么这个箱被采纳"的推理都建立在错的前提上:

        - ``box_min_rotations``:顶底来回次数下限,用于排除单次反转的假箱;
        - ``box_keep_broken_bars``:已破位边缘保留根数(供 BREAKOUT_ACCEPTED
          破位接受确认与回踩);
        - ``box_keep_active_bars``:未破位但久未变化的 ACTIVE 边缘保留根数
          (市场由平衡转入趋势时,刚离开的箱沿仍是最近的结构阶梯)。

        映射:上沿 ``[hi_ext - w, hi_ext]`` → RESISTANCE,
        下沿 ``[lo_ext, lo_ext + w]`` → SUPPORT;若与场内已有 ACTIVE 区匹配
        (距离 ≤ tol)则原位保留(不打断存活 episode)。
        """
        bracket = self._bal_det.push((ts, o, h, l, c, v), atr_val)
        if atr_val <= 0.0:
            return
        w = self.p.k_zw * atr_val
        tol = max(self.p.tol_atr * atr_val, 0.5 * atr_val)
        if bracket is not None and hasattr(bracket, "mode"):
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
                    still_out = (c > zhi + bm_now) if role is ZoneRole.RESISTANCE else (c < zlo - bm_now)
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
                        if (prev_z is None
                                or (id(z) in alive_ids and id(prev_z) not in alive_ids)
                                or z.last_change_known >= prev_z.last_change_known):
                            active_by_role[z.role] = z
                elif index - z.last_change_known <= self.p.box_keep_broken_bars:
                    other_zones.append(z)
            self.zones = list(active_by_role.values()) + other_zones
            return

        boxes = [b for b in self._bal_det.score()
                 if b.rotations >= self.p.box_min_rotations] if hasattr(self._bal_det, "score") else []

        if boxes:
            alive_ids: set[int] = set()
            for box in boxes:
                win = self._bal_det.bars[box.start_index:box.end_index + 1]
                hi_ext = max(b[2] for b in win)
                lo_ext = min(b[3] for b in win)
                for role, zlo, zhi, opp in (
                    (ZoneRole.RESISTANCE, hi_ext - w, hi_ext, lo_ext),
                    (ZoneRole.SUPPORT, lo_ext, lo_ext + w, hi_ext),
                ):
                    matched = None
                    for z in self.zones:
                        ref_edge = z.hi if z.role is ZoneRole.RESISTANCE else z.lo
                        new_edge = zhi if role is ZoneRole.RESISTANCE else zlo
                        if abs(ref_edge - new_edge) <= tol:
                            matched = z
                            break
                    if matched is not None:
                        if matched.role is role and matched.state is ZoneState.ACTIVE:
                            matched.last_change_known = index
                            matched.box_opposite = opp
                            matched.box_quality = max(matched.box_quality, float(box.quality))
                            matched.box_width_atr = float((hi_ext - lo_ext) / atr_val)
                            matched.box_rotations = max(matched.box_rotations, int(box.rotations))
                        alive_ids.add(id(matched))
                    else:
                        nz = Zone(lo=zlo, hi=zhi, role=role,
                                  touches=max(1, box.rotations),
                                  origin_known=index, last_change_known=index,
                                  box_opposite=opp,
                                  box_quality=float(box.quality),
                                  box_width_atr=float((hi_ext - lo_ext) / atr_val),
                                  box_rotations=int(box.rotations))
                        self.zones.append(nz)
                        alive_ids.add(id(nz))
            self.zones = [
                z for z in self.zones
                if id(z) in alive_ids
                or (z.state is not ZoneState.ACTIVE
                    and index - z.last_change_known <= self.p.box_keep_broken_bars)
                or (z.state is ZoneState.ACTIVE
                    and index - z.last_change_known <= self.p.box_keep_active_bars)
            ]





    def on_bar(self, index: int, o: float, h: float, l: float, c: float,
               prev_close: Optional[float], atr_val: float,
               ts: object = 0, v: float = 0.0) -> None:
        if self._bal_det is not None:
            self._sync_balance_boxes(index, ts, o, h, l, c, v, atr_val)
        if atr_val <= 0.0:
            return
        self.flips = []
        for z in list(self.zones):
            self._advance(z, index, h, l, c, prev_close, atr_val)
        self.zones = [z for z in self.zones
                      if z.state not in (ZoneState.FLIPPED, ZoneState.DEAD)]
        # v21 年龄过期:长时间未被触碰/变化的区域视为过期,移出视图(久远无价值;
        # 0=关,历史 parity 逐位不变)
        if self.p.stale_after_bars > 0:
            self.zones = [z for z in self.zones
                          if index - z.last_change_known < self.p.stale_after_bars]

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
    """逐 bar 增量认知(§0.3:bar t 的认知只用 [0,t])。

    每根 bar 的因果推进顺序:ATR → 波段确认(§3)→ 区域生成/合并(§4.1)
    → 区域状态机(§4.2)→ regime 快照(§5)。全部以当前 bar 收盘为知时(A10)。
    """

    def __init__(self, params: Params) -> None:
        self.params = params
        self._atr = ATR(params.atr_n)
        self._swing = SwingTracker(params.k_rev)
        self._zones = ZoneBook(params)
        self._pivots: list[Pivot] = []
        self._bars: list[tuple] = []  # (ts, o, h, l, c, v)
        self._regime = Regime.TRANSITION

    # -- 输入 ----------------------------------------------------------------

    def update(self, bar: Bar) -> None:
        ts, o, h, l, c, v = _bar_parts(bar)
        index = len(self._bars)
        prev_close = self._bars[-1][4] if self._bars else None
        atr_val = self._atr.update(h, l, c)
        self._bars.append((ts, o, h, l, c, v))

        pivot = self._swing.update(index, o, h, l, c, atr_val)
        if pivot is not None:
            pivot = Pivot(price=pivot.price, forming_index=pivot.forming_index,
                          known_index=pivot.known_index, kind=pivot.kind,
                          close_extreme=float(self._bars[pivot.forming_index][4]))
            self._pivots.append(pivot)
            amplitude = None
            if len(self._pivots) >= 2:
                prev = self._pivots[-2]
                amplitude = (abs(pivot.price - prev.price) / atr_val
                             if atr_val > 0 else None)
            self._zones.on_pivot(pivot, pivot.known_index, atr_val,
                                 amplitude_atr=amplitude)
        self._zones.on_bar(index, o, h, l, c, prev_close, atr_val, ts=ts, v=v)
        self._regime = _regime_from(self._pivots, atr_val, self.params, c)

    # -- 只读视图 ------------------------------------------------------------

    @property
    def pivots(self) -> tuple[Pivot, ...]:
        return tuple(self._pivots)

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

    # -- 批量便捷 ------------------------------------------------------------


def replay(df: pd.DataFrame, params: Optional[Params] = None):
    """逐 bar 回放(与逐根 update 同一条路径)。返回 (structure, events_list)。

    events 由 market_operator.events.EventDetector 在同样的 bar 流上同步检出(§6/§6A)。
    """
    from .events import EventDetector  # 延迟导入避免环

    if params is None:
        params = Params.from_json(_default_config_path())
    detector = EventDetector(params)
    events: list = []
    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        bar = (row.ts, row.open, row.high, row.low, row.close, row.volume)
        events.extend(detector.update(bar))
    return detector.structure, events


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "defaults.json"
