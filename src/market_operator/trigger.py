"""降档细周期触发跟踪器(事件知时细化层)。

结构/区域/pivot/regime 在最近已收盘认知 bar 处冻结;
触发层在细周期 bar 收盘上实时推进确认条件(突破连续站稳、刺穿拒绝与趋势破坏)。
"""


from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .events import (
    BREAKOUT_ACCEPTED,
    BREAKOUT_FAILED,
    FLIP_RETESTED,
    LEVEL_REJECTED,
    RANGE_ROTATED,
    STRUCTURE_BROKEN,
    TREND_RESUMED,
    EventDetector,
    MarketEvent,
)
from .hypothesis import StructureView
from .structure import (
    Params,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
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
    """触发层接近过程跟踪。"""

    zone: Zone
    opened: int
    ex: float
    ex_index: int
    puncture_index: Optional[int] = None


@dataclass
class _MicCandidate:
    """突破接受候选的触发层连续站稳计数。"""

    zone: Zone
    lo: float
    hi: float
    role: ZoneRole
    count: int
    started: int
    start_close: float = 0.0



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
    """细周期确认跟踪器;认知层结构对象只读。"""

    def __init__(
        self,
        params: Params,
        *,
        scale: int,
        trigger_tf_seconds: int,
        obs_tf_seconds: int,
        enable_e7: bool = True,
    ) -> None:
        if scale < 1:
            raise ValueError(f"scale must be >= 1, got {scale}")
        self.p = params
        self.scale = int(scale)
        self.trigger_tf_seconds = int(trigger_tf_seconds)
        self.obs_tf_seconds = int(obs_tf_seconds)
        self.enable_e7 = bool(enable_e7)
        self.n_a_mic = params.n_a * self.scale
        self.n_f_mic = params.n_f * self.scale
        self._ctx: Optional[_Context] = None
        self._episodes: list[_MicEpisode] = []
        self._candidates: list[_MicCandidate] = []
        self._retests: list[_MicRetest] = []
        self._e4: list[_MicE4] = []
        self._adopted_eps: set = set()
        self._adopted_retests: set = set()
        self._adopted_e4: set = set()
        self._broken: set = set()
        self._e6_fired: Optional[tuple] = None
        self._prev_trig_close: Optional[float] = None
        self._recent_trig_bars: deque[tuple[int, float, float, float, float]] = deque(maxlen=96)

    def sync(self, i: int, det: EventDetector, span_start_of, span_end_of) -> None:
        """在认知 bar 收盘后校准触发层状态并刷新冻结语境。"""
        s = det.structure
        ctx = self._make_context(s)
        self._ctx = ctx
        live_ids = {id(z) for z in ctx.zones}
        self._broken = {zid for zid in self._broken if zid in live_ids}
        self._broken |= {
            id(z) for z in ctx.zones
            if z.state in (ZoneState.BROKEN, ZoneState.RETESTED)
        }
        self._e6_fired = None

        live = {id(z): z for z in ctx.zones}
        cog_eps = {(id(ep.zone), ep.opened) for ep in det._episodes}
        kept: list[_MicEpisode] = []
        for ep in self._episodes:
            z = live.get(id(ep.zone))
            if z is not None:
                if z.state is ZoneState.ACTIVE and (id(ep.zone), ep.opened) in cog_eps:
                    kept.append(ep)
                continue
            for z2 in ctx.zones:
                if (
                    z2.role is ep.zone.role
                    and z2.state is ZoneState.ACTIVE
                    and z2.lo <= ep.zone.lo
                    and z2.hi >= ep.zone.hi
                    and (id(z2), ep.opened) in cog_eps
                ):
                    ep.zone = z2
                    kept.append(ep)
                    break
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

        live_states = {id(z): z.state for z in ctx.zones}
        self._candidates = [
            cd for cd in self._candidates
            if live_states.get(id(cd.zone)) in (ZoneState.BROKEN, ZoneState.RETESTED, None)
        ]

        if not self.p.use_balance_detector:
            flippable = (ZoneState.BROKEN, ZoneState.RETESTED)
            self._retests = [rt for rt in self._retests if live.get(id(rt.zone)) is not None]
            for z in ctx.zones:
                if z.state in flippable and id(z) not in self._adopted_retests:
                    self._adopted_retests.add(id(z))
                    self._retests.append(
                        _MicRetest(
                            zone=z,
                            armed=(z.state is ZoneState.RETESTED),
                            extreme=z._retest_extreme if z.state is ZoneState.RETESTED else None,
                        )
                    )

            cog_e4 = {
                (cd.activated, cd.pb_price, cd.leg_start, cd.direction)
                for cd in det._trend_candidates
            }
            self._e4 = [
                cd for cd in self._e4
                if (cd.activated, cd.pb_price, cd.leg_start, cd.direction) in cog_e4
            ]
            for cd in det._trend_candidates:
                k = (cd.activated, cd.pb_price, cd.leg_start, cd.direction)
                if k in self._adopted_e4:
                    continue
                self._adopted_e4.add(k)
                self._e4.append(
                    _MicE4(
                        direction=cd.direction,
                        leg_start=cd.leg_start,
                        pb_price=cd.pb_price,
                        depth=cd.depth,
                        activated=cd.activated,
                        deadline=span_end_of(cd.deadline),
                    )
                )

    def on_trigger_bar(
        self, a: int, ts: object, o: float, h: float, l: float, c: float
    ) -> list[MarketEvent]:
        ctx = self._ctx
        prev_c = self._prev_trig_close
        self._prev_trig_close = c
        self._recent_trig_bars.append((int(ts), float(o), float(h), float(l), float(c)))
        if ctx is None or ctx.atr <= 0.0:
            return []
        p = self.p
        scale_sqrt = max(1.0, self.scale ** 0.5)
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
            self._candidates.append(
                _MicCandidate(
                    zone=z, lo=z.lo, hi=z.hi, role=z.role,
                    count=1, started=a, start_close=c,
                )
            )
            if not p.use_balance_detector and id(z) not in self._adopted_retests:
                self._adopted_retests.add(id(z))
                self._retests.append(_MicRetest(zone=z, armed=False, extreme=None, started=a))

        kept_cd: list[_MicCandidate] = []
        req_count = p.n_a if p.use_balance_detector else self.n_a_mic
        for cd in self._candidates:
            hold = (
                ((c > cd.hi + bm) if cd.role is res_role else (c < cd.lo - bm))
                if p.use_balance_detector
                else ((c > cd.hi) if cd.role is res_role else (c < cd.lo))
            )
            if a > cd.started:
                if not hold:
                    continue
                cd.count += 1
            if cd.count >= req_count:
                direction = +1 if cd.role is res_role else -1
                ref = cd.hi if cd.role is res_role else cd.lo
                if p.use_balance_detector:
                    ext_dist = (c - cd.hi) if direction > 0 else (cd.lo - c)
                    not_counter = (
                        (ctx.regime is not Regime.TREND_DOWN)
                        if direction > 0
                        else (ctx.regime is not Regime.TREND_UP)
                    )
                    close_pos = (c - l) / rng if direction > 0 else (h - c) / rng
                    progressing = (
                        (c >= cd.start_close and c > o and close_pos >= 0.50)
                        if direction > 0
                        else (c <= cd.start_close and c < o and close_pos >= 0.50)
                    )
                    max_ext = float(p.accept_max_ext_atr or 1.50)
                    if not_counter and progressing and ext_dist <= max_ext * ctx.atr:
                        sub_kind = "A2_OVEREXTENDED" if ext_dist > 0.80 * ctx.atr else "A1_TIGHT"
                        events.append(
                            self._mk(
                                BREAKOUT_ACCEPTED, direction, ts, a,
                                {
                                    "zone": cd.zone.clone(),
                                    "ref": ref,
                                    "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                                    "started": cd.started,
                                    "sub_kind": sub_kind,
                                    "ext_atr": (ext_dist / ctx.atr) if ctx.atr > 0.0 else 0.0,
                                },
                            )
                        )
                else:
                    events.append(
                        self._mk(
                            BREAKOUT_ACCEPTED, direction, ts, a,
                            {
                                "zone": cd.zone.clone(),
                                "ref": ref,
                                "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                                "started": cd.started,
                            },
                        )
                    )
                continue
            kept_cd.append(cd)
        self._candidates = kept_cd




        survivors: list[_MicEpisode] = []
        new_rejections: list[MarketEvent] = []
        for ep in self._episodes:
            z = ep.zone
            if getattr(z, "source_kind", "BOX_EDGE") != "BOX_EDGE" or z.state is not ZoneState.ACTIVE:
                continue
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
                    against_trend = (
                        (ctx.regime is Regime.TREND_UP) if res else (ctx.regime is Regime.TREND_DOWN)
                    )
                    bar_dir_ok = ((c < o) if res else (c > o)) if against_trend else (close_pos >= 0.45)
                    if real_retreat >= rj_trig and back_inside and close_pos >= 0.45 and bar_dir_ok:
                        direction = -1 if res else +1
                        inv_rej = (max(z.hi, ep.ex) + bm) if res else (min(z.lo, ep.ex) - bm)
                        fired = self._mk(
                            LEVEL_REJECTED, direction, ts, a,
                            {
                                "zone": z.clone(),
                                "ref": edge,
                                "invalid": inv_rej,
                                "ex": ep.ex,
                                "ex_index": ep.ex_index,
                                "sub_kind": "R1_MIC",
                            },
                        )
                        new_rejections.append(fired)

            else:
                if ep.puncture_index is not None and a - ep.puncture_index < self.n_f_mic:
                    if (c < z.hi) if res else (c > z.lo):
                        direction = -1 if res else +1
                        fired = self._reject(BREAKOUT_FAILED, direction, ep, z, ts, a, bm, edge)
                if fired is None and (extreme_first or ep.ex_index < a) and a - ep.ex_index <= self.n_f_mic:
                    retreat = (ep.ex - min(l, c)) if res else (max(h, c) - ep.ex)
                    if retreat >= rj:
                        direction = -1 if res else +1
                        fired = self._reject(LEVEL_REJECTED, direction, ep, z, ts, a, bm, edge)
                        new_rejections.append(fired)

            if fired is not None:
                events.append(fired)
            else:
                if not extreme_first:
                    new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                    if new_ex != ep.ex:
                        ep.ex, ep.ex_index = new_ex, a
                if not p.use_balance_detector or (a - ep.opened) <= (p.n_f + 1):
                    survivors.append(ep)
        self._episodes = survivors

        if not p.use_balance_detector:
            events.extend(self._range_rotation(ts, a, c, ctx, new_rejections))

            kept_e4: list[_MicE4] = []
            for cd in self._e4:
                if a > cd.deadline:
                    continue
                level = cd.pb_price + cd.direction * p.e4_recovery * cd.depth
                if (c > level) if cd.direction > 0 else (c < level):
                    events.append(
                        self._mk(
                            TREND_RESUMED, cd.direction, ts, a,
                            {
                                "leg_start": cd.leg_start,
                                "pb_price": cd.pb_price,
                                "depth": cd.depth,
                                "ref": level,
                                "invalid": cd.pb_price - cd.direction * p.k_bm * ctx.atr,
                                "regime": ctx.regime.value,
                            },
                        )
                    )
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

        if self.enable_e7 and not p.use_balance_detector:
            kept_rt: list[_MicRetest] = []
            for rt in self._retests:
                z = rt.zone
                res = z.role is res_role
                if rt.started >= 0 and a <= rt.started:
                    kept_rt.append(rt)
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
                rej = ((h - ex >= rj) or (c - ex >= rj)) if res else ((ex - l >= rj) or (ex - c >= rj))
                if rej:
                    direction = +1 if res else -1
                    ref = z.lo if direction < 0 else z.hi
                    invalid = (z.hi + bm) if direction < 0 else (z.lo - bm)
                    events.append(
                        self._mk(
                            FLIP_RETESTED, direction, ts, a,
                            {
                                "zone": z.clone(),
                                "ref": ref,
                                "invalid": invalid,
                                "from_role": z.role.value,
                                "atr": ctx.atr,
                            },
                        )
                    )
                    continue
                kept_rt.append(rt)
            self._retests = kept_rt
        return events

    def _mk(self, eid: str, direction: int, ts: object, a: int, attrs: dict) -> MarketEvent:
        attrs = dict(attrs)
        attrs["mic"] = True
        attrs["trigger_tf"] = self.trigger_tf_seconds
        step_ms = int(self.trigger_tf_seconds) * 1000
        reset_tail = tuple(self._recent_trig_bars)
        attrs["eval_bars"] = reset_tail[-6:]
        attrs["reset_bars"] = reset_tail
        attrs["reset_step_ms"] = step_ms
        attrs["close_ts"] = int(ts) + step_ms
        return MarketEvent(id=eid, direction=direction, ts=ts, known_index=a, attrs=attrs)


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
        last_low = s.last_low_pivot
        last_high = s.last_high_pivot
        return _Context(
            atr=atr,
            bm=self.p.k_bm * atr,
            rj=self.p.k_rj * atr,
            regime=s.regime(),
            zones=tuple(s._zones.zones),
            pivots=pivots,
            band=None if self.p.use_balance_detector else self._band_of(pivots),
            l_star=last_low.price if last_low is not None else None,
            h_star=last_high.price if last_high is not None else None,
            l_star_forming=last_low.forming_index if last_low is not None else None,
            h_star_forming=last_high.forming_index if last_high is not None else None,
        )

    def _range_rotation(
        self, ts: object, a: int, c: float, ctx: _Context, rejected_events: list
    ) -> list[MarketEvent]:
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
            out.append(
                self._mk(
                    RANGE_ROTATED, rejection.direction, ts, a,
                    {
                        "zone": z.clone(),
                        "ref": rejection.attrs["ref"],
                        "invalid": rejection.attrs["invalid"],
                        "band": (band_lo, band_hi),
                        "room": room,
                        "via_known": rejection.known_index,
                    },
                )
            )
        return out



# ---------------------------------------------------------------------------
# 构建入口(脚本/测试共用)
# ---------------------------------------------------------------------------


def prepare_mic_cognition(
    df_cog,
    df_trig,
    params: Params,
    symbol: str,
    *,
    obs_tf_seconds: int,
    trigger_tf_seconds: int = 900,
    enable_e7: bool = True,
) -> MicCognition:

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

    detector = EventDetector(params, enable_e7=enable_e7)
    tracker = MicTracker(
        params,
        scale=scale,
        trigger_tf_seconds=trigger_tf_seconds,
        obs_tf_seconds=obs_tf_seconds,
        enable_e7=enable_e7,
    )


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
