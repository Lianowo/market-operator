"""市场结构行为事件检测器。

检测器与 ``MarketStructure`` 同步逐 bar 推进,所有事件均在 ``known_index = t`` 的
bar 收盘时刻因果判出,仅依赖 ``[0, t]`` 已知历史。
"""


from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .structure import (
    MarketStructure,
    Params,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)

BREAKOUT_ACCEPTED = "BREAKOUT_ACCEPTED"
BREAKOUT_FAILED = "BREAKOUT_FAILED"
LEVEL_REJECTED = "LEVEL_REJECTED"
TREND_RESUMED = "TREND_RESUMED"
RANGE_ROTATED = "RANGE_ROTATED"
STRUCTURE_BROKEN = "STRUCTURE_BROKEN"
FLIP_RETESTED = "FLIP_RETESTED"

ACCEPT = BREAKOUT_ACCEPTED
REJECT = LEVEL_REJECTED
CONSUMED = "CONSUMED"

LEGACY_EVENT_CODES = {
    "E1": BREAKOUT_ACCEPTED,
    "E2": BREAKOUT_FAILED,
    "E3": LEVEL_REJECTED,
    "E4": TREND_RESUMED,
    "E5": RANGE_ROTATED,
    "E6": STRUCTURE_BROKEN,
    "E7": FLIP_RETESTED,
    "ACCEPT": BREAKOUT_ACCEPTED,
    "REJECT": LEVEL_REJECTED,
    "CONSUMED": CONSUMED,
}

EVENT_IDENTIFIERS = (
    BREAKOUT_ACCEPTED,
    BREAKOUT_FAILED,
    LEVEL_REJECTED,
    TREND_RESUMED,
    RANGE_ROTATED,
    STRUCTURE_BROKEN,
    FLIP_RETESTED,
)



def canonical_event_id(event_id: object) -> str:
    """把历史编号或极简原语归一为语义标识;未知标识原样返回(便于暴露拼写错误)。

    归一化只有这一处:配置解析、路由白名单、计数口径都调它,因此不会出现
   "路由认别名而计数不认"的分裂。
    """
    text = str(event_id)
    return LEGACY_EVENT_CODES.get(text, text)


@dataclass(frozen=True)
class MarketEvent:
    """§6 事件。`ts` 为知时 bar 的时间戳,`known_index` 为知时 bar 序号(A10)。"""

    id: str
    direction: int            # +1 多 / -1 空
    ts: object
    known_index: int
    attrs: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 内部追踪状态
# ---------------------------------------------------------------------------


@dataclass
class _Episode:
    """区域接近过程(approach episode):自首次正面触碰起追踪,至多产出一枚主事件。"""

    zone: Zone
    opened: int
    ex: float
    ex_index: int
    puncture_index: Optional[int] = None
    outside_closes: int = 0


@dataclass
class _AcceptCandidate:
    """突破接受候选:区域收盘破位起,连续 N_a 根收在外侧确认接受。"""

    zone: Zone
    lo: float
    hi: float
    role: ZoneRole
    count: int
    started: int
    start_close: float = 0.0


@dataclass
class _TrendResumptionCandidate:
    """趋势恢复候选:回调终点 pivot 确认后激活,等待收盘回收回调腿。"""

    direction: int
    leg_start: float
    pb_price: float
    depth: float
    activated: int
    deadline: int



def _reject(eid: str, direction: int, z: Zone, t: int, ts: object, bm: float,
            extra: dict, edge: float) -> MarketEvent:
    """BREAKOUT_FAILED/LEVEL_REJECTED(拒绝类)发射:先记 §4.3 事实(rejects+1、知时),再快照 zone 进 attrs。"""
    z.rejects += 1
    z.last_change_known = t
    attrs = {"zone": z.clone(), "ref": edge, "invalid": edge - direction * bm}
    attrs.update(extra)
    return MarketEvent(id=eid, direction=direction, ts=ts, known_index=t, attrs=attrs)


# ---------------------------------------------------------------------------
# EventDetector
# ---------------------------------------------------------------------------


class EventDetector:
    """与 ``MarketStructure`` 同步逐 bar 推进的事件检测器。"""

    def __init__(self, params: Params, *, enable_e7: bool = False) -> None:
        self.params = params
        self.enable_e7 = bool(enable_e7)
        self.structure = MarketStructure(params)
        self.events: list[MarketEvent] = []
        self._episodes: list[_Episode] = []
        self._candidates: list[_AcceptCandidate] = []
        self._trend_candidates: list[_TrendResumptionCandidate] = []
        self._prev_regime: Regime = Regime.TRANSITION
        self.consumed_episodes: int = 0
        self._recent_bars: deque[tuple[int, float, float, float, float]] = deque(maxlen=96)

    def update(self, bar) -> list[MarketEvent]:
        self.structure.update(bar)
        s = self.structure
        t = s.bars_seen - 1
        atr = s.atr()
        ts_cur, o_cur, h_cur, l_cur, c_cur, _ = s._bars[t]
        step_ms = (int(ts_cur) - self._recent_bars[-1][0]) if self._recent_bars else 0
        self._recent_bars.append(
            (int(ts_cur), float(o_cur), float(h_cur), float(l_cur), float(c_cur))
        )
        emitted: list[MarketEvent] = []
        if t > 0 and atr > 0.0:
            prev_close = s._bars[t - 1][4]
            emitted.extend(
                self._zone_events(t, ts_cur, o_cur, h_cur, l_cur, c_cur, prev_close, atr)
            )
            if not self.params.use_balance_detector:
                emitted.extend(self._trend_resumption(t, ts_cur, c_cur, atr))
            emitted.extend(self._structure_break(t, ts_cur, c_cur, atr))
            if self.enable_e7 and not self.params.use_balance_detector:
                emitted.extend(self._flip_retest(t, ts_cur, atr))
        if emitted:
            reset_tail = tuple(self._recent_bars)
            eval_tail = reset_tail[-6:]
            close_ts = int(ts_cur) + int(step_ms)
            for ev in emitted:
                ev.attrs["eval_bars"] = eval_tail
                ev.attrs["reset_bars"] = reset_tail
                ev.attrs["reset_step_ms"] = int(step_ms)
                ev.attrs["close_ts"] = close_ts
        self._prev_regime = s.regime()
        self.events.extend(emitted)
        return emitted


    # -- BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED/RANGE_ROTATED:区域事件(A1-A6, A8) ---------------------------------------

    def _zone_events(self, t: int, ts: object, o: float, h: float, l: float,
                     c: float, prev_close: float, atr: float) -> list[MarketEvent]:
        p = self.params
        s = self.structure
        bm, rj = p.k_bm * atr, p.k_rj * atr
        n_a, n_f = p.n_a, p.n_f
        res_role = ZoneRole.RESISTANCE

        # A3 收尸:zone 离开 ACTIVE(破位/翻转/消亡)即 episode 终结;
        # v22 R3 扩展:当 use_balance_detector=True 时,允许 1~2 根收盘假突破
        # (只要未发生 > 1.25*ATR 单边逃逸且在 <= n_f 窗口内)暂保 _Episode 观察吞没收复!
        live = {id(z): z for z in s._zones.zones}
        kept_eps: list[_Episode] = []
        for ep in self._episodes:
            z = live.get(id(ep.zone))
            if z is not None:
                if z.state is ZoneState.ACTIVE:
                    kept_eps.append(ep)
                elif (p.use_balance_detector
                      and z.state in (ZoneState.BROKEN, ZoneState.RETESTED)
                      and ep.outside_closes <= 2
                      and (t - (ep.puncture_index if ep.puncture_index is not None else ep.opened)) <= n_f):
                    res = z.role is res_role
                    escape = (ep.ex - z.hi) if res else (z.lo - ep.ex)
                    if escape <= 1.25 * atr:
                        kept_eps.append(ep)
                continue
            for z2 in s._zones.zones:
                if z2.role is ep.zone.role and z2.state is ZoneState.ACTIVE \
                        and z2.lo <= ep.zone.lo and z2.hi >= ep.zone.hi:
                    ep.zone = z2
                    kept_eps.append(ep)
                    break
        self._episodes = kept_eps

        # A2 新触碰开 episode(前收在正面之外,且本 bar [low,high] 与带相交)。
        # v22:若当根同时触碰并直接收盘破位(z 在当根刚转为 BROKEN),同样开启 _Episode 供 R3 假突破观察。
        for z in s._zones.zones:
            just_broken = (
                p.use_balance_detector
                and z.state is ZoneState.BROKEN
                and z.last_change_known == t
            )
            if z.state is not ZoneState.ACTIVE and not just_broken:
                continue
            if any(ep.zone is z for ep in self._episodes):
                continue
            res = z.role is res_role
            front_out = (prev_close < z.lo) if res else (prev_close > z.hi)
            if front_out and h >= z.lo and l <= z.hi:
                ex = h if res else l
                ep = _Episode(zone=z, opened=t, ex=ex, ex_index=t)
                if (h > z.hi + bm) if res else (l < z.lo - bm):
                    ep.puncture_index = t
                self._episodes.append(ep)

        for z in s._zones.zones:
            if z.state is ZoneState.BROKEN and z.last_change_known == t:
                if p.use_balance_detector:
                    res = z.role is res_role
                    escape = (h - z.hi) if res else (z.lo - l)
                    if escape > 1.25 * atr:
                        self._episodes = [ep for ep in self._episodes if ep.zone is not z]
                else:
                    self._episodes = [ep for ep in self._episodes if ep.zone is not z]
                self._candidates = [cd for cd in self._candidates if cd.zone is not z]
                self._candidates.append(
                    _AcceptCandidate(
                        zone=z, lo=z.lo, hi=z.hi, role=z.role,
                        count=1, started=t, start_close=c,
                    )
                )

        events: list[MarketEvent] = []
        rng = max(h - l, 1e-12)
        cur_regime = s.regime()

        kept: list[_AcceptCandidate] = []
        for cd in self._candidates:
            hold = (c > cd.hi) if cd.role is res_role else (c < cd.lo)
            if t > cd.started:
                if not hold:
                    continue
                cd.count += 1
            if cd.count >= n_a:
                direction = +1 if cd.role is res_role else -1
                ref = cd.hi if cd.role is res_role else cd.lo
                ext_dist = (c - cd.hi) if direction > 0 else (cd.lo - c)
                if p.use_balance_detector:
                    not_counter = (
                        (cur_regime is not Regime.TREND_DOWN)
                        if direction > 0
                        else (cur_regime is not Regime.TREND_UP)
                    )
                    close_pos = (c - l) / rng if direction > 0 else (h - c) / rng
                    progressing = (
                        (c >= cd.start_close and c > o and close_pos >= 0.50)
                        if direction > 0
                        else (c <= cd.start_close and c < o and close_pos >= 0.50)
                    )
                    max_ext = float(p.accept_max_ext_atr or 1.50)
                    if not_counter and progressing and ext_dist <= max_ext * atr:
                        sub_kind = "A2_OVEREXTENDED" if ext_dist > 0.80 * atr else "A1_TIGHT"
                        events.append(MarketEvent(
                            id=BREAKOUT_ACCEPTED, direction=direction, ts=ts, known_index=t,
                            attrs={
                                "zone": cd.zone.clone(),
                                "ref": ref,
                                "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                                "started": cd.started,
                                "sub_kind": sub_kind,
                                "ext_atr": (ext_dist / atr) if atr > 0.0 else 0.0,
                            },
                        ))
                else:
                    events.append(MarketEvent(
                        id=BREAKOUT_ACCEPTED, direction=direction, ts=ts, known_index=t,
                        attrs={
                            "zone": cd.zone.clone(),
                            "ref": ref,
                            "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                            "started": cd.started,
                        },
                    ))
                continue
            kept.append(cd)
        self._candidates = kept




        survivors: list[_Episode] = []
        for ep in self._episodes:
            z = ep.zone
            if getattr(z, "source_kind", "BOX_EDGE") != "BOX_EDGE":
                continue
            res = z.role is res_role
            fired: Optional[MarketEvent] = None
            edge = z.hi if res else z.lo
            extreme_first = (c < o) if res else (c >= o)

            if extreme_first:
                new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                if new_ex != ep.ex:
                    ep.ex, ep.ex_index = new_ex, t

            if ep.puncture_index is None:
                if (h > z.hi + bm) if res else (l < z.lo - bm):
                    ep.puncture_index = t

            closed_outside = (c > z.hi + bm) if res else (c < z.lo - bm)
            close_pos = (h - c) / rng if res else (c - l) / rng

            if (
                p.use_balance_detector
                and 1 <= ep.outside_closes <= 2
                and ((c < z.lo and close_pos >= 0.50) if res else (c > z.hi and close_pos >= 0.50))
            ):
                direction = -1 if res else +1
                inv_r3 = (max(z.hi, ep.ex) + bm) if res else (min(z.lo, ep.ex) - bm)
                fired = _reject(
                    LEVEL_REJECTED, direction, z, t, ts, bm,
                    {
                        "ex": ep.ex,
                        "ex_index": ep.ex_index,
                        "puncture_index": ep.puncture_index,
                        "outside_closes": ep.outside_closes,
                        "sub_kind": "R3_TRAP",
                        "invalid": inv_r3,
                    },
                    edge,
                )
                z.state = ZoneState.ACTIVE
                z.last_change_known = t
                self._candidates = [cd for cd in self._candidates if cd.zone is not z]


            if (
                fired is None
                and not p.use_balance_detector
                and ep.puncture_index is not None
                and t - ep.puncture_index < n_f
            ):
                if (c < z.hi) if res else (c > z.lo):
                    direction = -1 if res else +1
                    fired = _reject(
                        BREAKOUT_FAILED, direction, z, t, ts, bm,
                        {"puncture_index": ep.puncture_index}, edge,
                    )

            if (
                fired is None
                and ep.outside_closes == 0
                and z.state is ZoneState.ACTIVE
                and (extreme_first or ep.ex_index < t)
                and t - ep.ex_index <= n_f
            ):
                if p.use_balance_detector:
                    real_retreat = (ep.ex - c) if res else (c - ep.ex)
                    back_inside = (c <= z.lo) if res else (c >= z.hi)
                    against_trend = (
                        (cur_regime is Regime.TREND_UP) if res else (cur_regime is Regime.TREND_DOWN)
                    )
                    bar_dir_ok = ((c < o) if res else (c > o)) if against_trend else (close_pos >= 0.45)
                    if real_retreat >= rj and back_inside and close_pos >= 0.45 and bar_dir_ok:
                        direction = -1 if res else +1
                        extra_rej = {
                            "ex": ep.ex,
                            "ex_index": ep.ex_index,
                            "invalid": (max(z.hi, ep.ex) + bm) if res else (min(z.lo, ep.ex) - bm),
                            "sub_kind": "R1_WICK" if ep.puncture_index is not None else "R2_STALL",
                        }
                        fired = _reject(LEVEL_REJECTED, direction, z, t, ts, bm, extra_rej, edge)
                else:
                    retreat = (ep.ex - min(l, c)) if res else (max(h, c) - ep.ex)
                    if retreat >= rj:
                        direction = -1 if res else +1
                        fired = _reject(
                            LEVEL_REJECTED, direction, z, t, ts, bm,
                            {"ex": ep.ex, "ex_index": ep.ex_index}, edge,
                        )

            if fired is not None:
                events.append(fired)
            else:
                if not extreme_first:
                    new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                    if new_ex != ep.ex:
                        ep.ex, ep.ex_index = new_ex, t
                if closed_outside:
                    ep.outside_closes += 1
                    if p.use_balance_detector and ep.outside_closes > 2:
                        continue
                if p.use_balance_detector and (t - ep.opened) > (n_f + 1):
                    self.consumed_episodes += 1
                    continue
                survivors.append(ep)

        self._episodes = survivors

        if not p.use_balance_detector:
            events.extend(
                self._range_rotation(
                    t, ts, c, atr, [e for e in events if e.id == LEVEL_REJECTED]
                )
            )
        return events

    def _range_rotation(

        self, t: int, ts: object, c: float, atr: float,
        rejected_events: list[MarketEvent]
    ) -> list[MarketEvent]:
        if not rejected_events or self.structure.regime() is not Regime.RANGE:
            return []
        band = self._band()
        if band is None:
            return []
        band_lo, band_hi = band
        edge_tol = self.params.e5_edge_atr * atr
        room_min = self.params.k_room * atr
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
            out.append(MarketEvent(
                id=RANGE_ROTATED, direction=rejection.direction, ts=ts, known_index=t,
                attrs={
                    "zone": z.clone(),
                    "ref": rejection.attrs["ref"],
                    "invalid": rejection.attrs["invalid"],
                    "band": (band_lo, band_hi),
                    "room": room,
                    "via_known": rejection.known_index,
                },
            ))
        return out


    def _band(self) -> Optional[tuple]:
        """A8 合成带:最近至多 range_max_pivots 个 confirmed pivots。"""
        recent = self.structure.pivots[-self.params.range_max_pivots:]
        if len(recent) < 4:
            return None
        highs = [pv.price for pv in recent if pv.kind is PivotKind.HIGH]
        lows = [pv.price for pv in recent if pv.kind is PivotKind.LOW]
        if not highs or not lows:
            return None
        return min(lows), max(highs)

    def _trend_resumption(
        self, t: int, ts: object, c: float, atr: float
    ) -> list[MarketEvent]:
        p = self.params
        regime = self.structure.regime()
        pivots = self.structure._pivots
        lo_d, hi_d = p.e4_depth_atr

        if pivots and pivots[-1].known_index == t:
            pv = pivots[-1]
            if pv.kind is PivotKind.LOW and regime is Regime.TREND_UP:
                highs = [x for x in pivots[-p.range_max_pivots:] if x.kind is PivotKind.HIGH]
                lows = [x for x in pivots[-p.range_max_pivots:] if x.kind is PivotKind.LOW]
                if len(highs) >= 2 and len(lows) >= 2:
                    leg_start = highs[-1].price
                    hl_prev = lows[-2].price
                    d = leg_start - pv.price
                    if pv.price > hl_prev and lo_d * atr <= d <= hi_d * atr:
                        self._trend_candidates.append(
                            _TrendResumptionCandidate(
                                direction=+1, leg_start=leg_start, pb_price=pv.price,
                                depth=d, activated=t, deadline=t + p.n_f,
                            )
                        )
            elif pv.kind is PivotKind.HIGH and regime is Regime.TREND_DOWN:
                highs = [x for x in pivots[-p.range_max_pivots:] if x.kind is PivotKind.HIGH]
                lows = [x for x in pivots[-p.range_max_pivots:] if x.kind is PivotKind.LOW]
                if len(highs) >= 2 and len(lows) >= 2:
                    leg_start = lows[-1].price
                    lh_prev = highs[-2].price
                    d = pv.price - leg_start
                    if pv.price < lh_prev and lo_d * atr <= d <= hi_d * atr:
                        self._trend_candidates.append(
                            _TrendResumptionCandidate(
                                direction=-1, leg_start=leg_start, pb_price=pv.price,
                                depth=d, activated=t, deadline=t + p.n_f,
                            )
                        )

        events: list[MarketEvent] = []
        kept: list[_TrendResumptionCandidate] = []
        for cd in self._trend_candidates:
            if t <= cd.activated:
                kept.append(cd)
                continue
            if t > cd.deadline:
                continue
            level = cd.pb_price + cd.direction * p.e4_recovery * cd.depth
            if (c > level) if cd.direction > 0 else (c < level):
                events.append(MarketEvent(
                    id=TREND_RESUMED, direction=cd.direction, ts=ts, known_index=t,
                    attrs={
                        "leg_start": cd.leg_start,
                        "pb_price": cd.pb_price,
                        "depth": cd.depth,
                        "ref": level,
                        "invalid": cd.pb_price - cd.direction * p.k_bm * atr,
                        "regime": regime.value,
                    },
                ))
                continue
            kept.append(cd)
        self._trend_candidates = kept
        return events

    def _structure_break(
        self, t: int, ts: object, c: float, atr: float
    ) -> list[MarketEvent]:
        bm = self.params.k_bm * atr
        if self._prev_regime is Regime.TREND_UP:
            last_low = self.structure.last_low_pivot
            if last_low is not None and c < last_low.price - bm:
                return [MarketEvent(
                    id=STRUCTURE_BROKEN, direction=-1, ts=ts, known_index=t,
                    attrs={
                        "ref": last_low.price,
                        "invalid": last_low.price + bm,
                        "pivot_forming": last_low.forming_index,
                        "regime_before": Regime.TREND_UP.value,
                    },
                )]
        elif self._prev_regime is Regime.TREND_DOWN:
            last_high = self.structure.last_high_pivot
            if last_high is not None and c > last_high.price + bm:
                return [MarketEvent(
                    id=STRUCTURE_BROKEN, direction=+1, ts=ts, known_index=t,
                    attrs={
                        "ref": last_high.price,
                        "invalid": last_high.price - bm,
                        "pivot_forming": last_high.forming_index,
                        "regime_before": Regime.TREND_DOWN.value,
                    },
                )]
        return []


    # -- FLIP_RETESTED:FLIP_RETEST(docs/v2-protocol.md §1,v2 增量) -----------------------

    def _flip_retest(self, t: int, ts: object, atr: float) -> list[MarketEvent]:
        """本 bar 发生的全部 FLIPPED 角色互换 → FLIP_RETESTED(破位→回踩→被拒绝→控制权易手)。

        role_new=resistance(旧支撑破位)⇒ -1;role_new=support ⇒ +1(v2-protocol
        §1,对称测、不做方向挑选)。invalid = zone 远端边缘 ± k_bm·atr;ref = 破位
        边缘(short=lo,long=hi);atr = 转移 bar 的 ATR(供 F1 查验)。
        """
        bm = self.params.k_bm * atr
        events: list[MarketEvent] = []
        for flip in self.structure._zones.flips:
            if flip.index != t:
                continue
            z = flip.zone                       # 新角色区快照(边界 = 旧区边界)
            direction = -1 if z.role is ZoneRole.RESISTANCE else +1
            ref = z.lo if direction < 0 else z.hi
            invalid = (z.hi + bm) if direction < 0 else (z.lo - bm)
            events.append(MarketEvent(
                id=FLIP_RETESTED, direction=direction, ts=ts, known_index=t,
                attrs={"zone": z.clone(), "ref": ref, "invalid": invalid,
                       "from_role": flip.from_role.value, "atr": atr},
            ))
        return events
