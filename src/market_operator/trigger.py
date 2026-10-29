"""触发麦克风:事件知时细化层(出处 docs/v10-protocol.md §A)。

规范地位:本模块是 v10 §A 的唯一实现。**认知尺度上的检测器、结构、收割、
执行、费用与组合容量语义零改动**(events.py/structure.py/position.py/broker
成本模型不动;bar 事件流由本模块自跑的同一 EventDetector 复放产出,脚本层
对账 == prepare_cognition,逐位一致是硬约束)。mic 层是纯增量:同族模式的
收盘确认条件(BREAKOUT_ACCEPTED 连续站稳 A4 / BREAKOUT_FAILED 收复 A5 / LEVEL_REJECTED 回退 A6 / TREND_RESUMED 回收 A7 /
STRUCTURE_BROKEN 破坏 A9 / FLIP_RETESTED 回踩拒绝翻转 §4.2)允许在 trigger_tf 收盘上先满足,事件
知时 = 该 trigger bar 收盘,入场 = 次一根 trigger bar 开盘(1h→15m 最多
早 45 分钟)。

解释性决定(协议文字未完全展开处,取最符合因果语义的读法,标注条款):
- [§A 结构锁定] 结构/区域/pivot/regime 不在 mic 层重判:同步点认知 bar t
  收盘后,区域集合(角色/边界/状态)、ATR、pivots、regime 冻结为"最近已收
  认知 bar t 的事实";trigger bar 只推进确认量(episode 极值/刺穿、candidate
  连续站稳计数、回踩极值),不产生新 pivot、新区域、新 episode、新 regime。
  即 episode 生命周期(open=认知 bar 的 A2 触碰、zone 生灭=认知帧状态机)照
  旧在认知帧;"开盘/收盘"类确认判定的知时钟细化到 trigger_tf。
- [§A 阈值] 一切 ATR 阈值(bm/rj/N 窗口外的距离量、invalid 缓冲、RANGE_ROTATED/EDGE_RECLAIMED 带
  量)用最近已收认知 bar 的 ATR(atr(t),事件知时因果量;trigger bar 自身
  的 TR 不入阈)。计数闸门按尺度比换算:N_a_mic = N_a×scale、N_f_mic =
  N_f×scale(scale = obs_tf/trigger_tf;1h→15m ⇒ 12,BREAKOUT_FAILED/LEVEL_REJECTED/TREND_RESUMED 窗口 BREAKOUT_ACCEPTED 计数
  同一换算,协议 §A 原式对全部数量闸门适用)。
- [§A-锚] trigger 极值/收盘与认知帧的对应关系由数据 compaction 守恒保证:
  trigger 高位 ⊂ 认知 bar 高位 ⇒ 刺穿/极值类条件在认知帧与 trigger 帧判到
  同一现象(时间细化、无事实分叉);收盘类条件(破位/站稳/回收)以 trigger
  收盘为准时与认知收盘可以分叉(这正是细化语义:先满足先知,收盘点 cancel
  者不成交 —— 但有真正成交差异,如实记录)。
- [同步收尸] 每认知收盘同步:episode/candidate/retest/TREND_RESUMED 以其认知层对应物
  为准校验 —— episode 对应认知 episode 消亡或 zone 离开 ACTIVE(A3 续绑语
  义同 events.py)⇒ mic 侧终结;mic 破位候选在认知收盘时 zone 未到破位态
  (收盘未破位 = 认知帧 A4 不成立)⇒ 作废;TREND_RESUMED 候选已被认知层消费(认知
  收盘发 TREND_RESUMED)⇒ mic 侧丢弃。反向(认知层存而 mic 侧无)只在 ATR 抖动边
  界发生(trigger 收盘未越 bm(atr(t-1)) 而认知收盘越过 bm(atr(t))),
  如实接受:该路径 bar 事件照常发射,mic 不发射。
- [刺穿 anchored] episode 采用自认知层时,认知 puncture_index(帧单位)映
  射为该认知 bar 的 **首根** trigger bar(可用的最早刺穿时刻 ⇒ BREAKOUT_FAILED 窗口最早
  关闭,保守);ex_index 映射为其认知 bar 的 **末根** trigger bar 之前的
  …同首根口径(窗口最早关闭,保守,与刺穿一致)。
- [§4.3 事实记账] 拒绝类事实(rejects+1)只在认知层记账;mic 发射不改任
  何认知 zone 对象(只读),“事件语义纯增量”。
- [FLIP_RETESTED 细化] 回踩跟踪(破位→首次返回带内→回退 ≥ k_rj·atr_ctx)逐 trigger
  bar 进行(结构 §4.2 的确认类判定;认知层 FLIPPED 仍是认知帧事实,bar
  FLIP_RETESTED 在认知收盘照常发射,mic FLIP_RETESTED 是其提前知时版本)。
- [发射纪律] 同一 trigger bar 的因果序镜像 events.py:破位登记(触发的
  episode 终结 + candidate 注册 + 回踩跟踪武装)→ BREAKOUT_ACCEPTED 推进 → episode 判定
  (BREAKOUT_FAILED 先于 LEVEL_REJECTED,伴随 EDGE_RECLAIMED)→ RANGE_ROTATED 合成(LEVEL_REJECTED 携带)→ TREND_RESUMED → STRUCTURE_BROKEN → FLIP_RETESTED。知时 =
  trigger bar 标签(与全仓 ts=bar 开盘标签约定一致;收盘时刻 = 标签+step)。
- [去重] 每个 mic episode/candidate 至多产出一枚主事件(A3 同语义,伴随
  RANGE_ROTATED/EDGE_RECLAIMED 不在去重清单);STRUCTURE_BROKEN 无 once-only(认知层本来就每个合格 bar 发射,
  mic 同语义逐合格 trigger bar 发射);FLIP_RETESTED 每回踩过程至多一次。
- [PROCESS_ACCEPTED/PROCESS_REJECTED] 过程量语义未在 trigger 尺度定义,本层不发射(v9 目录照旧仅在
  认知帧,由 bar 检测器承担)。

MicCognition = 单 campaign 的 trigger 帧认知缓存;prepare_mic_cognition
为其唯一构建入口(脚本与测试共用,口径同源)。

v13 增量(docs/v13-protocol.md,冻结 2026-10-20,加性,上述 v10 语义逐位
不变):级联触发尺度 —— trigger_tf 不再是单一全局档(900),而按战役认知
帧映射(TRIGGER_TF_MAP,用户指令 1h→15m/2h→30m/4h→1h/12h→2h/1d→4h,
research-log #26 预注册);协议 §1"触发帧 = obs_tf/4(不支持则取最近的现
有帧,声明)" ⇒ 12h/1d 的 obs/4(3h/6h)无现帧,按指令取 2h/4h 并在报告
如实声明。resolve_trigger_tf 为映射解析的唯一实现;MicTracker /
prepare_mic_cognition 本体对任意 trigger_tf 已通用(数量闸门
N_a_mic/N_f_mic = N×scale,scale = obs/trigger 派生),不新增任何先验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .events import (
    BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, TREND_RESUMED, RANGE_ROTATED, STRUCTURE_BROKEN, FLIP_RETESTED, EDGE_RECLAIMED, EventDetector, MarketEvent,
)
from .hypothesis import StructureView
from .structure import (
    Params, PivotKind, Regime, Zone, ZoneRole, ZoneState,
)

# ---------------------------------------------------------------------------
# v13 级联触发尺度映射(docs/v13-protocol.md;12h→2h、1d→4h 为协议 §1
# "obs/4 不支持则取最近现有帧"的落地:3h/6h 无现帧 ⇒ 2h/4h,脚本如实声明)
# ---------------------------------------------------------------------------

TRIGGER_TF_MAP: dict = {3600: 900, 7200: 1800, 14400: 3600,
                        43200: 7200, 86400: 14400}


def resolve_trigger_tf(obs_tf_seconds: int, *, tf_map: dict = None,
                       available: tuple = (900, 1800, 3600, 7200, 14400)
                       ) -> tuple:
    """obs_tf → trigger_tf 解析(v13 级联;返回值与声明由脚本/报告如实采用)。

    返回 (trigger_tf_seconds, declaration):
    - 映射命中且帧可用:declaration = None(精确档);
    - 映射缺失或映射帧不可用:取"严格小于 obs 且整除 obs"的可用帧中与目标
      (映射值,无映射则 obs//4)对数距离最近者(并列取更粗 —— 保守,提前
      量更小),declaration = 如实声明字符串;无候选 ⇒ raise(该认知帧无法
      承载 mic 语义,不得静默退回 bar)。
    数量闸门 scale = obs // trigger 由 prepare_mic_cognition 派生。
    """
    obs = int(obs_tf_seconds)
    tf_map = TRIGGER_TF_MAP if tf_map is None else {int(k): int(v) for k, v
                                                    in dict(tf_map).items()}
    avail = tuple(sorted(int(a) for a in available))
    target = tf_map.get(obs, obs // 4)
    if obs in tf_map and tf_map[obs] in avail and obs % tf_map[obs] == 0:
        return tf_map[obs], None
    cands = [a for a in avail if a < obs and obs % a == 0]
    if not cands:
        raise RuntimeError(f"obs_tf {obs}s 无可用 trigger 帧(候选需整除且小于 "
                           f"obs;available={avail})")
    import math
    def _dist(f):
        d = abs(math.log2(f / target)) if target > 0 else float("inf")
        return (d, -f)                          # 并列取更粗(f 大者)
    best = min(cands, key=_dist)
    why = ("映射帧不可用" if obs in tf_map else "映射缺失")
    return best, (f"obs_tf={obs}s {why}(目标 {target}s),取最近可用 "
                  f"{best}s(scale={obs // best};协议 §1 声明)")

# ---------------------------------------------------------------------------
# 追踪状态(mic 层私有;认知层对象只读)
# ---------------------------------------------------------------------------


@dataclass
class _MicEpisode:
    """A2/A3 已开 episode 的 trigger 级确认跟踪(采用自认知层,逐 trigger 推进)。"""

    zone: Zone                      # 认知层活引用(只读;生命周期由认知帧裁决)
    opened: int                     # 认知帧触碰 bar 索引(信息字段)
    ex: float                       # episode 极值(§8.4 次序启发式,trigger 级推进)
    ex_index: int                   # ex 建立 trigger bar(窗口判据)
    puncture_index: Optional[int] = None  # 刺穿 trigger bar(窗口判据)


@dataclass
class _MicCandidate:
    """A4 破位候选的 trigger 级连续站稳计数(N_a_mic = N_a×scale)。"""

    zone: Zone                      # 破位瞬间快照来源(trackers 只读克隆用)
    lo: float
    hi: float
    role: ZoneRole
    count: int                      # 已连续收在外侧的 trigger 根数(破位 bar=第 1 根)
    started: int                    # 破位 trigger bar 索引


@dataclass
class _MicRetest:
    """§4.2 回踩跟踪(破位→返回带内→回退拒绝)的 trigger 级推进(FLIP_RETESTED 细化)。"""

    zone: Zone                      # 认知层活引用(只读)
    armed: bool                     # 已"首次从远侧返回带内"(RETESTED 等价)
    extreme: Optional[float]        # 回踩最深极值(认知采用时取 _retest_extreme 快照)
    started: int = -1               # trigger 级破位 bar(该 bar 不判回踩,镜像 §4.2)


@dataclass
class _MicE4:
    """A7 TREND_RESUMED 候选(认知层激活;pivot 确认只发生在认知收盘,故只采用不新建)。"""

    direction: int
    leg_start: float
    pb_price: float
    depth: float
    activated: int                  # 认知帧激活 bar 索引
    deadline: int                   # 最后一根允许回收的 trigger bar 索引(inclusive)


@dataclass(frozen=True)
class _Context:
    """认知 bar t 收盘后冻结的语境([§A 结构锁定]/[§A 阈值] 的物质承担者)。"""

    atr: float
    bm: float
    rj: float
    regime: Regime
    zones: tuple                    # tuple[Zone] 认知层活引用(只读)
    pivots: tuple                   # tuple[Pivot] 全部 confirmed
    band: Optional[tuple]           # A8 合成带 (lo, hi)(RANGE_ROTATED 用;不足则 None)
    l_star: Optional[float]         # pivots 全名单最后 low(STRUCTURE_BROKEN/EDGE_RECLAIMED 语境)
    h_star: Optional[float]         # 镜像:最后 high
    l_star_forming: Optional[int]   # STRUCTURE_BROKEN attrs 溯源(认知帧 forming 索引)
    h_star_forming: Optional[int]


# ---------------------------------------------------------------------------
# MicCognition(单战役 trigger 帧缓存)
# ---------------------------------------------------------------------------


@dataclass
class MicCognition:
    """单 (symbol, TF) 的 trigger 帧缓存(bar 流纯函数,成本无关)。

    bars/cog_index_of/atrs 逐 trigger bar 对齐;events_by/views 仅在发射 bar
    有键。bar_events_by = 本模块自跑认知检测器的 bar 事件流(与
    prepare_cognition 的产品逐位对账用,脚本层硬断言)。
    """

    symbol: str
    obs_tf_seconds: int
    trigger_tf_seconds: int
    scale: int                      # obs // trigger(N_a_mic/N_f_mic 的换算基数)
    bars: list                      # (ts, o, h, l, c, v) trigger,严格升序
    cog_index_of: list              # trigger bar → 认知 bar 索引
    atrs: list                      # trigger bar → 语境 ATR(最近已收认知 bar)
    events_by: dict = field(default_factory=dict)   # trigger index → [MarketEvent]
    views: dict = field(default_factory=dict)       # trigger index → StructureView
    bar_events_by: dict = field(default_factory=dict)  # cog index → [MarketEvent]
    counters: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MicTracker(逐 trigger bar 状态机;同步点 = 认知收盘)
# ---------------------------------------------------------------------------


class MicTracker:
    """trigger 级确认跟踪;认知层对象(det/zone)一律只读。"""

    def __init__(self, params: Params, *, scale: int,
                 trigger_tf_seconds: int, obs_tf_seconds: int,
                 enable_e7: bool = True,
                 enable_e8: bool = False, e8_pos_edge: float = 0.20) -> None:
        if scale < 1:
            raise ValueError(f"scale must be >= 1, got {scale}")
        self.p = params
        self.scale = int(scale)
        self.trigger_tf_seconds = int(trigger_tf_seconds)
        self.obs_tf_seconds = int(obs_tf_seconds)
        self.enable_e7 = bool(enable_e7)
        self.enable_e8 = bool(enable_e8)
        self.e8_pos_edge = float(e8_pos_edge)
        self.n_a_mic = params.n_a * self.scale     # 数量闸门尺度换算(§A)
        self.n_f_mic = params.n_f * self.scale
        self._ctx: Optional[_Context] = None       # 最近已收认知 bar 的冻结语境
        self._episodes: list[_MicEpisode] = []
        self._candidates: list[_MicCandidate] = []
        self._retests: list[_MicRetest] = []
        self._e4: list[_MicE4] = []
        # 采用记账(认知层对象身份 → 已采用标记;防重复采用)
        self._adopted_eps: set = set()             # (id(zone), opened)
        self._adopted_retests: set = set()         # id(zone)(破位跟踪一 zone 一枚)
        self._adopted_e4: set = set()              # (activated, pb_price, leg_start, direction)
        # 已破位 zone 身份(trigger 级 + 采用级合并;A4 转移只发生一次 ——
        # 防同一 zone 在后续 trigger 收盘上"重复破位"重置连续计数)
        self._broken: set = set()                  # id(zone)
        # STRUCTURE_BROKEN 同现象去重(ref, regime):每个认知覆盖区间至多一发(镜像认知层
        # "每个合格认知收盘一发"的频率形状;emit 后同段内重复收盘不再发)
        self._e6_fired: Optional[tuple] = None

    # -- 同步:认知 bar i 收盘后调用(det 已完成 update) ------------------------

    def sync(self, i: int, det: EventDetector, span_start_of,
             span_end_of) -> None:
        """以认知层为准校准 mic 状态([同步收尸]);刷新冻结语境。

        span_start_of/span_end_of:认知帧索引 → 其 trigger 覆盖区间首/末根
        (由构建方提供;首/末不完整聚合桶如实,不做 scale 倍乘假设)。
        """
        s = det.structure
        ctx = self._make_context(s)
        self._ctx = ctx
        # 破位身份集:采用层(BROKEN/RETESTED)∪ trigger 级(存活者);离场即忘
        live_ids = {id(z) for z in ctx.zones}
        self._broken = {zid for zid in self._broken if zid in live_ids}
        self._broken |= {id(z) for z in ctx.zones
                         if z.state in (ZoneState.BROKEN, ZoneState.RETESTED)}
        self._e6_fired = None                      # 新覆盖区间重新允许一发

        # episode 收尸/续绑(A3 同语义;zone 离开 ACTIVE ⇒ 终结)
        live = {id(z): z for z in ctx.zones}
        cog_eps = {(id(ep.zone), ep.opened) for ep in det._episodes}
        kept: list[_MicEpisode] = []
        for ep in self._episodes:
            z = live.get(id(ep.zone))
            if z is not None:
                if z.state is ZoneState.ACTIVE \
                        and (id(ep.zone), ep.opened) in cog_eps:
                    kept.append(ep)
                continue
            rebound = False
            for z2 in ctx.zones:
                if z2.role is ep.zone.role and z2.state is ZoneState.ACTIVE \
                        and z2.lo <= ep.zone.lo and z2.hi >= ep.zone.hi \
                        and (id(z2), ep.opened) in cog_eps:
                    ep.zone = z2
                    kept.append(ep)
                    rebound = True
                    break
            if not rebound:
                pass                            # 终结:认知 episode 已消亡
        self._episodes = kept
        # 采用认知 episode(open=A2 触碰,只发生在认知收盘);窗口锚点取该认知
        # bar 覆盖的**首根** trigger bar([刺穿 anchored]:窗口最早关闭,保守)
        for ep in det._episodes:
            k = (id(ep.zone), ep.opened)
            if k in self._adopted_eps:
                continue
            self._adopted_eps.add(k)
            puncture = (span_start_of(ep.puncture_index)
                        if ep.puncture_index is not None else None)
            self._episodes.append(_MicEpisode(
                zone=ep.zone, opened=ep.opened, ex=ep.ex,
                ex_index=span_start_of(ep.ex_index),
                puncture_index=puncture))

        # 破位候选收尸:认知收盘时 zone 未到破位态 ⇒ 认知 A4 不成立 ⇒ 作废
        live_states = {id(z): z.state for z in ctx.zones}
        self._candidates = [cd for cd in self._candidates
                            if live_states.get(id(cd.zone)) in
                            (ZoneState.BROKEN, ZoneState.RETESTED, None)]

        # 回踩跟踪:采用 BROKEN/RETESTED 区(破位只发生在收盘类的判定上,
        # 认知实例此处已含本 bar 破位),zone 离开 book ⇒ 认知侧已终结 ⇒ 弃
        flippable = (ZoneState.BROKEN, ZoneState.RETESTED)
        self._retests = [rt for rt in self._retests
                         if live.get(id(rt.zone)) is not None]
        for z in ctx.zones:
            if z.state in flippable and id(z) not in self._adopted_retests:
                self._adopted_retests.add(id(z))
                self._retests.append(_MicRetest(
                    zone=z, armed=(z.state is ZoneState.RETESTED),
                    extreme=z._retest_extreme
                    if z.state is ZoneState.RETESTED else None))

        # TREND_RESUMED 候选:认知层已消费(发射/过期) ⇒ 弃;新激活 ⇒ 采用,窗口映射为
        # 帧 (activated, activated+n_f] 的 trigger 覆盖([§A-锚] 同一现象)
        cog_e4 = {(cd.activated, cd.pb_price, cd.leg_start, cd.direction)
                  for cd in det._e4}
        self._e4 = [cd for cd in self._e4
                    if (cd.activated, cd.pb_price, cd.leg_start, cd.direction)
                    in cog_e4]
        for cd in det._e4:
            k = (cd.activated, cd.pb_price, cd.leg_start, cd.direction)
            if k in self._adopted_e4:
                continue
            self._adopted_e4.add(k)
            # deadline 是未来帧索引(activated+n_f):由调用方负责把帧索引
            # 换算到 trigger 序号(含尾段超帧钳制);此处不得用当前已知 bar
            # 数截断 —— 那会把窗口永远钳死在采用 bar(曾致 TREND_RESUMED 零发射)
            deadline = span_end_of(cd.deadline)
            self._e4.append(_MicE4(
                direction=cd.direction, leg_start=cd.leg_start,
                pb_price=cd.pb_price, depth=cd.depth,
                activated=cd.activated, deadline=deadline))

        # STRUCTURE_BROKEN 一 bar 一发,无 mic 态;RANGE_ROTATED/EDGE_RECLAIMED 语境在 ctx。

    # -- 推进:一根已收盘 trigger bar ------------------------------------------

    def on_trigger_bar(self, a: int, ts: object, o: float, h: float,
                       l: float, c: float) -> list[MarketEvent]:
        ctx = self._ctx
        prev_c = getattr(self, "_prev_trig_close", None)
        self._prev_trig_close = c
        if ctx is None or ctx.atr <= 0.0:
            return []
        p = self.p
        scale_sqrt = max(1.0, (self.scale ** 0.5))
        bm, rj = ctx.bm, ctx.rj
        rj_trig = rj / scale_sqrt

        res_role = ZoneRole.RESISTANCE
        rng = max(h - l, 1e-12)
        events: list[MarketEvent] = []

        # 细周期 A2 实时触碰开 _MicEpisode (仅在平衡箱模式下启用, 消除等大周期收盘的 10~20h 入场迟缓!)
        if p.use_balance_detector and prev_c is not None:
            for z in ctx.zones:
                if z.state is not ZoneState.ACTIVE or id(z) in self._broken:
                    continue
                if any(ep.zone is z for ep in self._episodes):
                    continue
                res = z.role is res_role
                front_out = (prev_c < z.lo) if res else (prev_c > z.hi)
                if front_out and h >= z.lo and l <= z.hi:
                    ex = h if res else l
                    ep = _MicEpisode(zone=z, opened=a, ex=ex, ex_index=a)
                    if (h > z.hi + bm) if res else (l < z.lo - bm):
                        ep.puncture_index = a
                    self._episodes.append(ep)

        # ① 破位登记
        for z in ctx.zones:
            if z.state is not ZoneState.ACTIVE or id(z) in self._broken:
                continue
            res = z.role is res_role
            broke = (c > z.hi + bm) if res else (c < z.lo - bm)
            if not broke:
                continue
            self._broken.add(id(z))
            self._episodes = [ep for ep in self._episodes if ep.zone is not z]
            self._candidates = [cd for cd in self._candidates if cd.zone is not z]
            cd_new = _MicCandidate(zone=z, lo=z.lo, hi=z.hi, role=z.role, count=1, started=a)
            cd_new._start_close = c
            self._candidates.append(cd_new)
            if not p.use_balance_detector and id(z) not in self._adopted_retests:
                self._adopted_retests.add(id(z))
                self._retests.append(_MicRetest(zone=z, armed=False,
                                                extreme=None, started=a))

        # ② BREAKOUT_ACCEPTED 推进
        kept_cd: list[_MicCandidate] = []
        req_count = p.n_a if p.use_balance_detector else self.n_a_mic
        for cd in self._candidates:
            hold = ((c > cd.hi + bm) if cd.role is res_role else (c < cd.lo - bm)) if p.use_balance_detector else ((c > cd.hi) if cd.role is res_role else (c < cd.lo))
            if a > cd.started:
                if not hold:
                    continue
                cd.count += 1
            if cd.count >= req_count:
                direction = +1 if cd.role is res_role else -1
                ref = cd.hi if cd.role is res_role else cd.lo
                if p.use_balance_detector:
                    ext_dist = (c - cd.hi) if direction > 0 else (cd.lo - c)
                    start_c = getattr(cd, "_start_close", c)
                    not_counter = (ctx.regime is not Regime.TREND_DOWN) if direction > 0 else (ctx.regime is not Regime.TREND_UP)
                    close_pos = (c - l) / rng if direction > 0 else (h - c) / rng
                    progressing = (c >= start_c and c > o and close_pos >= 0.50) if direction > 0 else (c <= start_c and c < o and close_pos >= 0.50)
                    not_climax = ext_dist <= 1.50 * ctx.atr
                    if not_counter and progressing and not_climax:
                        events.append(self._mk(BREAKOUT_ACCEPTED, direction, ts, a, {
                            "zone": cd.zone.clone(), "ref": ref,
                            "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                            "started": cd.started, "sub_kind": "A1_REAL"}))
                else:
                    events.append(self._mk(BREAKOUT_ACCEPTED, direction, ts, a, {
                        "zone": cd.zone.clone(), "ref": ref,
                        "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                        "started": cd.started}))
                continue
            kept_cd.append(cd)
        self._candidates = kept_cd



        # ③ episode 判定(BREAKOUT_FAILED 先于 LEVEL_REJECTED;§8.4 次序启发式镜像 events.py)
        survivors: list[_MicEpisode] = []
        new_e3: list[MarketEvent] = []
        for ep in self._episodes:
            z = ep.zone
            if z.state is not ZoneState.ACTIVE:
                continue                        # ① 已砍(防御;理论不可达)
            res = z.role is res_role
            edge = z.hi if res else z.lo
            extreme_first = (c < o) if res else (c >= o)
            if extreme_first:
                new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                if new_ex != ep.ex:
                    ep.ex, ep.ex_index = new_ex, a
            if ep.puncture_index is None:
                if (h > z.hi + bm) if res else (l < z.lo - bm):
                    ep.puncture_index = a
            fired: Optional[MarketEvent] = None
            close_pos = (h - c) / rng if res else (c - l) / rng
            if p.use_balance_detector:
                if (extreme_first or ep.ex_index < a) and a - ep.ex_index <= self.n_f_mic:
                    real_retreat = (ep.ex - c) if res else (c - ep.ex)
                    back_inside = (c <= z.lo) if res else (c >= z.hi)
                    against_trend = (ctx.regime is Regime.TREND_UP) if res else (ctx.regime is Regime.TREND_DOWN)
                    bar_dir_ok = ((c < o) if res else (c > o)) if against_trend else (close_pos >= 0.45)
                    if real_retreat >= rj_trig and back_inside and close_pos >= 0.45 and bar_dir_ok:
                        direction = -1 if res else +1
                        inv_rej = (max(z.hi, ep.ex) + bm) if res else (min(z.lo, ep.ex) - bm)
                        fired = self._mk(LEVEL_REJECTED, direction, ts, a, {
                            "zone": z.clone(), "ref": edge, "invalid": inv_rej,
                            "ex": ep.ex, "ex_index": ep.ex_index, "sub_kind": "R1_MIC"})
                        new_e3.append(fired)
            else:
                if ep.puncture_index is not None and a - ep.puncture_index < self.n_f_mic:
                    returned = (c < z.hi) if res else (c > z.lo)
                    if returned:
                        direction = -1 if res else +1
                        fired = self._reject(BREAKOUT_FAILED, direction, ep, z, ts, a, bm, edge)
                if fired is None and (extreme_first or ep.ex_index < a) \
                        and a - ep.ex_index <= self.n_f_mic:
                    retreat = (ep.ex - min(l, c)) if res else (max(h, c) - ep.ex)
                    if retreat >= rj:
                        direction = -1 if res else +1
                        fired = self._reject(LEVEL_REJECTED, direction, ep, z, ts, a, bm, edge)
                        new_e3.append(fired)
            if fired is not None:
                events.append(fired)
                # v8 EDGE_RECLAIMED:伴随 BREAKOUT_FAILED 发射(与 events.py 同序:BREAKOUT_FAILED 紧随其伴随 EDGE_RECLAIMED)
                if self.enable_e8 and fired.id == BREAKOUT_FAILED:
                    reclaimed = self._edge_reclaim(ep, z, res, ts, a, c, h, l, ctx)
                    if reclaimed is not None:
                        events.append(reclaimed)
            else:
                if not extreme_first:
                    new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                    if new_ex != ep.ex:
                        ep.ex, ep.ex_index = new_ex, a
                if not p.use_balance_detector or (a - ep.opened) <= (p.n_f + 1):
                    survivors.append(ep)
        self._episodes = survivors


        # ④ RANGE_ROTATED 合成(A8;regime/band 取冻结语境)
        events.extend(self._range_rotation(ts, a, c, ctx, new_e3))

        # ⑤ TREND_RESUMED 触发:(activated, activated+n_f] 帧窗的 trigger 覆盖内回收
        kept_e4: list[_MicE4] = []
        for cd in self._e4:
            if a > cd.deadline:
                continue
            level = cd.pb_price + cd.direction * p.e4_recovery * cd.depth
            if (c > level) if cd.direction > 0 else (c < level):
                events.append(self._mk(TREND_RESUMED, cd.direction, ts, a, {
                    "leg_start": cd.leg_start, "pb_price": cd.pb_price,
                    "depth": cd.depth, "ref": level,
                    "invalid": cd.pb_price - cd.direction * p.k_bm * ctx.atr,
                    "regime": ctx.regime.value}))
                continue
            kept_e4.append(cd)
        self._e4 = kept_e4

        # ⑥ STRUCTURE_BROKEN:冻结语境 regime 曾为趋势 ⇒ 收盘破坏最近 HL/LH(A9 镜像);
        # 同现象每覆盖区间至多一发(镜像认知层"每合格认知收盘一发"的频率)
        e6_key = None
        if ctx.regime is Regime.TREND_UP and ctx.l_star is not None \
                and c < ctx.l_star - bm:
            e6_key = (ctx.l_star, Regime.TREND_UP.value)
            if self._e6_fired != e6_key:
                events.append(self._mk(STRUCTURE_BROKEN, -1, ts, a, {
                    "ref": ctx.l_star, "invalid": ctx.l_star + bm,
                    "pivot_forming": ctx.l_star_forming,
                    "regime_before": Regime.TREND_UP.value}))
        elif ctx.regime is Regime.TREND_DOWN and ctx.h_star is not None \
                and c > ctx.h_star + bm:
            e6_key = (ctx.h_star, Regime.TREND_DOWN.value)
            if self._e6_fired != e6_key:
                events.append(self._mk(STRUCTURE_BROKEN, +1, ts, a, {
                    "ref": ctx.h_star, "invalid": ctx.h_star - bm,
                    "pivot_forming": ctx.h_star_forming,
                    "regime_before": Regime.TREND_DOWN.value}))
        if e6_key is not None:
            self._e6_fired = e6_key

        # ⑦ FLIP_RETESTED:回踩跟踪逐 trigger bar 推进(§4.2 镜像;破位 bar 自身不判
        # 回踩 —— 结构的 BROKEN 首根无 _retest 同语义;armed 后同 bar 即判)
        if self.enable_e7:
            kept_rt: list[_MicRetest] = []
            for rt in self._retests:
                z = rt.zone
                res = z.role is res_role
                if rt.started >= 0 and a <= rt.started:
                    kept_rt.append(rt)              # trigger 级破位 bar 不判
                    continue
                if not rt.armed:
                    back = (l <= z.hi) if res else (h >= z.lo)
                    if not back:
                        kept_rt.append(rt)
                        continue
                    rt.armed = True
                    rt.extreme = l if res else h
                ex = min(rt.extreme, l) if res else max(rt.extreme, h)
                rt.extreme = ex
                rej = ((h - ex >= rj) or (c - ex >= rj)) if res \
                    else ((ex - l >= rj) or (ex - c >= rj))
                if rej:
                    # 角色互换:from_role=resistance ⇒ role_new=support ⇒ +1
                    # (镜像 events._flip_retest 的 role_new 方向映射)
                    direction = +1 if res else -1
                    ref = z.lo if direction < 0 else z.hi
                    invalid = (z.hi + bm) if direction < 0 else (z.lo - bm)
                    events.append(self._mk(FLIP_RETESTED, direction, ts, a, {
                        "zone": z.clone(), "ref": ref, "invalid": invalid,
                        "from_role": z.role.value, "atr": ctx.atr}))
                    continue                        # 一回踩过程至多一次
                kept_rt.append(rt)
            self._retests = kept_rt
        return events

    # -- 内部 ------------------------------------------------------------------

    def _mk(self, eid: str, direction: int, ts: object, a: int,
            attrs: dict) -> MarketEvent:
        attrs = dict(attrs)
        attrs["mic"] = True                        # [发射纪律] 纯增量标注
        attrs["trigger_tf"] = self.trigger_tf_seconds
        return MarketEvent(id=eid, direction=direction, ts=ts,
                           known_index=a, attrs=attrs)

    def _reject(self, eid: str, direction: int, ep: _MicEpisode, z: Zone,
                ts: object, a: int, bm: float, edge: float) -> MarketEvent:
        """BREAKOUT_FAILED/LEVEL_REJECTED 发射(镜像 events._reject 的 attrs 契约;不碰认知 zone 计数)。"""
        if eid == BREAKOUT_FAILED:
            extra = {"puncture_index": ep.puncture_index}
        else:
            extra = {"ex": ep.ex, "ex_index": ep.ex_index}
        return self._mk(eid, direction, ts, a, {
            "zone": z.clone(), "ref": edge, "invalid": edge - direction * bm,
            **extra})

    def _band_of(self, pivots: tuple) -> Optional[tuple]:
        recent = pivots[-self.p.range_max_pivots:]
        if len(recent) < 4:
            return None
        highs = [pv.price for pv in recent if pv.kind is PivotKind.HIGH]
        lows = [pv.price for pv in recent if pv.kind is PivotKind.LOW]
        if not highs or not lows:
            return None
        return min(lows), max(highs)

    def _make_context(self, s) -> _Context:
        atr = float(s.atr())
        pivots = s.pivots
        l_star = h_star = None
        l_form = h_form = None
        for pv in pivots:                            # 全名单,末位 = 最近知时
            if pv.kind is PivotKind.LOW:
                l_star, l_form = pv.price, pv.forming_index
            else:
                h_star, h_form = pv.price, pv.forming_index
        return _Context(
            atr=atr, bm=self.p.k_bm * atr, rj=self.p.k_rj * atr,
            regime=s.regime(), zones=tuple(s._zones.zones), pivots=pivots,
            band=self._band_of(pivots),
            l_star=l_star, h_star=h_star,
            l_star_forming=l_form, h_star_forming=h_form)

    def _range_rotation(self, ts: object, a: int, c: float, ctx: _Context,
                        rejected_events: list) -> list[MarketEvent]:
        if not rejected_events or ctx.regime is not Regime.RANGE or ctx.band is None:
            return []
        band_lo, band_hi = ctx.band
        edge_tol = self.p.e5_edge_atr * ctx.atr
        room_min = self.p.k_room * ctx.atr
        out: list[MarketEvent] = []
        for rejection in rejected_events:
            z: Zone = rejection.attrs["zone"]
            zc = 0.5 * (z.lo + z.hi)
            near_top = abs(zc - band_hi) <= edge_tol
            near_bot = abs(zc - band_lo) <= edge_tol
            if not (near_top or near_bot):
                continue
            room = (c - band_lo) if near_top else (band_hi - c)
            if room < room_min:
                continue
            out.append(self._mk(RANGE_ROTATED, rejection.direction, ts, a, {
                "zone": z.clone(), "ref": rejection.attrs["ref"],
                "invalid": rejection.attrs["invalid"],
                "band": (band_lo, band_hi), "room": room,
                "via_known": rejection.known_index}))
        return out

    def _edge_reclaim(self, ep: _MicEpisode, z: Zone, res: bool, ts: object,
                      a: int, c: float, h: float, l: float, ctx: _Context
                      ) -> Optional[MarketEvent]:
        """EDGE_RECLAIMED 伴随 BREAKOUT_FAILED(冻结语境 band L*/H* + trigger 收盘 pos 门;镜像 events)。"""
        l_star, h_star = ctx.l_star, ctx.h_star
        if l_star is None or h_star is None:
            return None
        rg = h_star - l_star
        if rg <= 0.0:
            return None
        pos = (c - l_star) / rg
        direction = -1 if res else +1
        edge_pos = self.e8_pos_edge
        if direction > 0:
            if pos > edge_pos:
                return None
        elif pos < 1.0 - edge_pos:
            return None
        ex = max(ep.ex, h) if res else min(ep.ex, l)
        return self._mk(EDGE_RECLAIMED, direction, ts, a, {
            "zone": z.clone(), "ref": (z.hi if res else z.lo),
            "invalid": (ex + ctx.bm) if res else (ex - ctx.bm),
            "pos": pos, "band": (l_star, h_star), "atr": ctx.atr,
            "puncture_index": ep.puncture_index, "ex": ex})


# ---------------------------------------------------------------------------
# 构建入口(脚本/测试共用)
# ---------------------------------------------------------------------------


def prepare_mic_cognition(df_cog, df_trig, params: Params, symbol: str, *,
                          obs_tf_seconds: int, trigger_tf_seconds: int = 900,
                          enable_e7: bool = True, enable_e8: bool = False,
                          e8_pos_edge: float = 0.20) -> MicCognition:
    """认知帧(结构锁定)× trigger 帧(知时细化)双流回放 → MicCognition。

    df_cog/df_trig 均为 (ts, o, h, l, c, v) 网格;trigger 帧步长必须整除认知
    步长,且每个认知 bar 的 trigger 覆盖区间内无洞(首/末认知桶可为不完整
    聚合桶 —— 与 build_campaign_frame 同一容忍度)。bar 事件流(本模块自跑
    的 EventDetector 产品)存于 bar_events_by 供逐位对账;使用前须断言与
    prepare_cognition 一致(脚本层;[认知尺度零改动] 的硬约束)。
    """
    import pandas as pd  # 局部导入:模块本体不依赖 pandas 输入(测试用 tuple)

    trig_step = int(trigger_tf_seconds) * 1000
    obs_step = int(obs_tf_seconds) * 1000
    if obs_step % trig_step != 0:
        raise ValueError(f"obs_tf {obs_tf_seconds}s 不被 trigger_tf "
                         f"{trigger_tf_seconds}s 整除")
    scale = obs_step // trig_step

    def rows_of(df) -> list:
        if isinstance(df, pd.DataFrame):
            return [(int(r.ts), float(r.open), float(r.high), float(r.low),
                     float(r.close), float(r.volume))
                    for r in df[["ts", "open", "high", "low", "close",
                                 "volume"]].itertuples(index=False)]
        return [(int(b[0]), float(b[1]), float(b[2]), float(b[3]),
                 float(b[4]), float(b[5])) for b in df]

    cog = rows_of(df_cog)
    trig = rows_of(df_trig)
    if not cog or not trig:
        raise ValueError("empty frame")
    trig_index = {b[0]: k for k, b in enumerate(trig)}
    if len(trig_index) != len(trig):
        raise AssertionError(f"{symbol} duplicate trigger ts")

    # trigger → 认知 bucket(认知 grid 对齐断言同 build_campaign_frame)
    cog_of_trig: list[int] = []
    keep: list[int] = []
    cog_labels = {int(b[0]): i for i, b in enumerate(cog)}
    for k, b in enumerate(trig):
        bucket = b[0] - (b[0] % obs_step)
        i = cog_labels.get(bucket)
        if i is None:
            continue                        # 认知覆盖范围外的 trigger bar(头尾)
        keep.append(k)
        cog_of_trig.append(i)
    if not keep:
        raise AssertionError(f"{symbol} trigger frame has no cognition coverage")
    trig = [trig[k] for k in keep]
    # 区间内无洞:同一认知 bar 的在场 trigger bar 连续成网格
    last_i, spans = None, {}
    for k, i in enumerate(cog_of_trig):
        if i != last_i:
            spans[i] = [k, k]
            last_i = i
        else:
            prev = spans[i][1]
            if trig[k][0] - trig[prev][0] != trig_step:
                raise AssertionError(
                    f"{symbol} cognition bar {i}: trigger hole "
                    f"{trig[prev][0]} -> {trig[k][0]}")
            spans[i][1] = k
    for i in cog_labels.values():
        if i not in spans:
            raise AssertionError(f"{symbol} cognition bar {i} has no trigger bars")

    detector = EventDetector(params, enable_e7=enable_e7, enable_e8=enable_e8,
                             e8_pos_edge=e8_pos_edge)
    tracker = MicTracker(params, scale=scale, trigger_tf_seconds=trigger_tf_seconds,
                         obs_tf_seconds=obs_tf_seconds, enable_e7=enable_e7,
                         enable_e8=enable_e8, e8_pos_edge=e8_pos_edge)

    events_by: dict = {}
    views: dict = {}
    atrs: list = [0.0] * len(trig)
    bar_events_by: dict = {}
    counters: dict = {}

    k = 0
    for i, bar in enumerate(cog):
        # 本认知 bar 覆盖区间:以上一认知收盘冻结的语境逐 trigger bar 推进
        lo, hi = spans[i]
        while k <= hi:
            ts, o, h, l, c, _ = trig[k]
            emitted = tracker.on_trigger_bar(k, ts, o, h, l, c)
            atrs[k] = tracker._ctx.atr if tracker._ctx is not None else 0.0
            if emitted:
                events_by[k] = emitted
                views[k] = StructureView(
                    regime=tracker._ctx.regime,
                    zones=tuple(z.clone() for z in tracker._ctx.zones),
                    atr=tracker._ctx.atr,
                    pivots=tracker._ctx.pivots[-params.range_max_pivots:])
                for e in emitted:
                    counters[e.id] = counters.get(e.id, 0) + 1
            k += 1
        # 认知收盘:bar 事件流(对账)+ 同步
        bar_emitted = detector.update(bar)
        if bar_emitted:
            bar_events_by[i] = list(bar_emitted)
        n_cog = len(cog)
        tracker.sync(i, detector,
                     lambda p, _sp=spans: _sp[p][0],
                     lambda p, _sp=spans, _n=n_cog: _sp[min(p, _n - 1)][1])

    return MicCognition(
        symbol=symbol, obs_tf_seconds=int(obs_tf_seconds),
        trigger_tf_seconds=int(trigger_tf_seconds), scale=scale,
        bars=trig, cog_index_of=cog_of_trig, atrs=atrs,
        events_by=events_by, views=views, bar_events_by=bar_events_by,
        counters=counters)


__all__ = [
    "MicCognition", "MicTracker", "prepare_mic_cognition",
    "TRIGGER_TF_MAP", "resolve_trigger_tf",
]
