"""原子水平位价格拍卖事件检测器(零特定蜡烛图拟合、纯价格拍卖与微观结构破坏第一性原理)。

第一性原理公理(彻底废除一切影线比例、实体占比等特定单根 K 线长相拟合):
在任何活跃【原子水平位 Level】上,多空双方的价格拍卖只存在两种可证伪的客观状态转移:

1. ``LEVEL_REJECTED``(边界拍卖拒绝与控制权反转 — Failed Probe & Micro-BOS):
   - 接近前提: 价格从水平位优势侧接近并触入 ``[extreme, neck]``(支撑自上而下 ``low <= neck``,
     阻力自下而上 ``high >= neck``),且未在 ``extreme`` 外侧形成连续收盘价值接受。
   - 微观压制锚点(``anchor_px``): 在测试水平位的下探/上探过程中,每当价格创出测试新极值或继续向水平位施压时,
     记录施压 K 线的反向极值作为进攻方最后一道微观压制/支撑锚点(做多为施压 K 线的 ``high``,做空为 ``low``)。
   - 拍卖反转确认: 当后续 K 线收盘站回 ``neck`` 优势侧,**且收盘实质性击穿 ``anchor_px``**(做多 ``close > anchor_px and close > neck``,
     做空 ``close < anchor_px and close < neck``),证明进攻方耗尽、防守方已在微观结构上完成反攻夺权!

2. ``BREAKOUT_ACCEPTED``(边界突破与价值迁移接受 — Breakout & Value Acceptance):
   - 第一步(越界): 价格从水平位内侧出发,顺势收盘越过绝对外沿 ``extreme``(多头 ``close > extreme and close > open``,
     空头 ``close < extreme and close < open``)。
   - 第二步(驻留延续): 紧随确认 K 线拒绝跌回水平位背侧(多头 ``low >= band_lo``,空头 ``high <= band_hi``),
     且继续收盘站稳在 ``extreme`` 外侧并超越突破棒收盘价(证明新价值区获得持续买/卖盘承接,排除单根诱多/诱空假突破)。

3. ``STRUCTURE_BROKEN``(关键起点防线 ``ORIGIN`` 被击穿翻转 — Macro CHoCH):
   供持仓管理实时感知宏观结构反转。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .level import Level, LevelKind, LevelRole
from .structure import MarketStructure, Params, _bar_parts

BREAKOUT_ACCEPTED = "BREAKOUT_ACCEPTED"
LEVEL_REJECTED = "LEVEL_REJECTED"
STRUCTURE_BROKEN = "STRUCTURE_BROKEN"

ACCEPT = BREAKOUT_ACCEPTED
REJECT = LEVEL_REJECTED
CONSUMED = "CONSUMED"

EVENT_ALIASES = {
    "ACCEPT": BREAKOUT_ACCEPTED,
    "REJECT": LEVEL_REJECTED,
    "BREAKOUT_ACCEPTED": BREAKOUT_ACCEPTED,
    "LEVEL_REJECTED": LEVEL_REJECTED,
    "STRUCTURE_BROKEN": STRUCTURE_BROKEN,
    "CONSUMED": CONSUMED,
}

EVENT_IDENTIFIERS = (
    BREAKOUT_ACCEPTED,
    LEVEL_REJECTED,
    STRUCTURE_BROKEN,
)


def canonical_event_id(event_id: object) -> str:
    text = str(event_id).strip()
    return EVENT_ALIASES.get(text, text)


@dataclass(frozen=True)
class MarketEvent:
    id: str
    direction: int
    ts: int
    known_index: int
    attrs: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return canonical_event_id(self.id)

    @property
    def level(self) -> Optional[Level]:
        return self.attrs.get("level") or self.attrs.get("zone")

    @property
    def price(self) -> float:
        px = self.attrs.get("price")
        if px is not None:
            return float(px)
        ref = self.attrs.get("ref")
        return float(ref) if ref is not None else 0.0


AuctionEvent = MarketEvent


@dataclass
class LevelAuctionState:
    probed_wall: bool = False
    absorbed_wall: bool = False


@dataclass
class RejectionEpisode:
    """水平位测试拍卖回合(纯追踪测试极值与最近施压棒的微观结构锚点)。"""

    level: Level
    opened_index: int
    extreme_px: float
    extreme_index: int
    anchor_px: float
    swept: bool = False
    outside_closes: int = 0


@dataclass
class BreakoutCandidate:
    """水平位突破后的价值驻留确认观察态。"""

    level_before: Level
    direction: int
    break_index: int
    break_low: float
    break_high: float
    break_close: float
    waited: int = 0


class LevelAuctionEngine:
    """零魔法阈值、纯结构破坏与价值接受驱动的原子水平位拍卖引擎(认知层与触发层完全同构共用)。"""

    def __init__(self) -> None:
        self.episodes: dict[tuple[int, int], RejectionEpisode] = {}
        self.breakouts: dict[tuple[int, int], BreakoutCandidate] = {}
        self.fired_episode_keys: set[tuple[int, int, int]] = set()
        self.fired_breakout_keys: set[tuple[int, int]] = set()
        self.broken_role_keys: set[tuple[int, int]] = set()
        self.retested_flip_keys: set[tuple[int, int]] = set()
        self.states: dict[tuple[int, int], LevelAuctionState] = {}

    def _step_breakouts(
        self, h: float, l: float, c: float, o: float, ts: int, k_idx: int, pad: float
    ) -> list[MarketEvent]:
        out: list[MarketEvent] = []
        for bkey, cand in list(self.breakouts.items()):
            lv = cand.level_before
            d = cand.direction
            cand.waited += 1
            if d > 0:
                if c <= lv.extreme or l < lv.band_lo:
                    self.breakouts.pop(bkey, None)
                    continue
                accepted = c > o and c > lv.extreme and c > cand.break_close and l >= lv.band_lo
                if accepted and bkey not in self.fired_breakout_keys:
                    self.fired_breakout_keys.add(bkey)
                    self.breakouts.pop(bkey, None)
                    inv = min(lv.band_lo, cand.break_low, l) - pad
                    out.append(MarketEvent(
                        id=BREAKOUT_ACCEPTED,
                        direction=1,
                        ts=ts,
                        known_index=k_idx,
                        attrs={
                            "zone": lv.clone(),
                            "level": lv.clone(),
                            "ref": float(lv.extreme),
                            "ex": float(max(cand.break_high, h)),
                            "invalid": float(inv),
                            "started": cand.break_index,
                            "sub_kind": f"LEVEL_ACCEPT_{lv.kind.value}",
                        },
                    ))
                    continue
            else:
                if c >= lv.extreme or h > lv.band_hi:
                    self.breakouts.pop(bkey, None)
                    continue
                accepted = c < o and c < lv.extreme and c < cand.break_close and h <= lv.band_hi
                if accepted and bkey not in self.fired_breakout_keys:
                    self.fired_breakout_keys.add(bkey)
                    self.breakouts.pop(bkey, None)
                    inv = max(lv.band_hi, cand.break_high, h) + pad
                    out.append(MarketEvent(
                        id=BREAKOUT_ACCEPTED,
                        direction=-1,
                        ts=ts,
                        known_index=k_idx,
                        attrs={
                            "zone": lv.clone(),
                            "level": lv.clone(),
                            "ref": float(lv.extreme),
                            "ex": float(min(cand.break_low, l)),
                            "invalid": float(inv),
                            "started": cand.break_index,
                            "sub_kind": f"LEVEL_ACCEPT_{lv.kind.value}",
                        },
                    ))
                    continue
            if cand.waited >= 2:
                self.breakouts.pop(bkey, None)
        return out

    def step(
        self,
        *,
        bar_index: int,
        bar: tuple[int, float, float, float, float, float],
        prev_bar: Optional[tuple[int, float, float, float, float, float]],
        recent_bars: Sequence[tuple[int, float, float, float, float, float]],
        active_levels: Sequence[Level],
        atr: float,
        k_bm: float,
        known_index_override: Optional[int] = None,
    ) -> list[MarketEvent]:
        ts, o, h, l, c, _ = bar
        k_idx = bar_index if known_index_override is None else int(known_index_override)
        pad = max(0.0, float(k_bm) * float(atr))
        emitted: list[MarketEvent] = []
        if prev_bar is None:
            return emitted
        _, po, ph, pl, pc, _ = prev_bar
        active_by_key = {(lv.uid, lv.flip_count): lv for lv in active_levels if lv.is_active}

        for lkey, lv in active_by_key.items():
            if lv.flipped and (lv.flip_retested or lkey in self.retested_flip_keys):
                lv.flip_retested = True
                continue
            is_res = lv.role is LevelRole.RESISTANCE
            is_sup = not is_res
            if lv.flipped and ((is_sup and c < lv.band_lo) or (is_res and c > lv.band_hi)):
                lv.flip_retested = True
                self.retested_flip_keys.add(lkey)
                self.episodes.pop(lkey, None)
                continue

            state = self.states.setdefault(lkey, LevelAuctionState())

            # 当根夺权突破确认（消除两根延迟追高）：
            # 必须先完成墙前吸收 (state.absorbed_wall == True)，且当前棒以强势实体收盘直接破界并吞没前一根反向压制极值：
            if state.absorbed_wall and lkey not in self.fired_breakout_keys:
                if (
                    is_res
                    and pc <= lv.band_hi
                    and c > lv.band_hi
                    and c > o
                    and c > ph
                ):
                    self.fired_breakout_keys.add(lkey)
                    inv = min(lv.band_lo, l) - pad
                    probe_inv = min(lv.probe_lo, l) - pad
                    emitted.append(MarketEvent(
                        id=BREAKOUT_ACCEPTED,
                        direction=1,
                        ts=ts,
                        known_index=k_idx,
                        attrs={
                            "zone": lv.clone(),
                            "level": lv.clone(),
                            "ref": float(lv.extreme),
                            "price": float(c),
                            "ex": float(h),
                            "invalid": float(inv),
                            "probe_invalid": float(probe_inv),
                            "started": bar_index,
                            "sub_kind": f"LEVEL_ACCEPT_{lv.kind.value}",
                        },
                    ))
                elif (
                    is_sup
                    and pc >= lv.band_lo
                    and c < lv.band_lo
                    and c < o
                    and c < pl
                ):
                    self.fired_breakout_keys.add(lkey)
                    inv = max(lv.band_hi, h) + pad
                    probe_inv = max(lv.probe_hi, h) + pad
                    emitted.append(MarketEvent(
                        id=BREAKOUT_ACCEPTED,
                        direction=-1,
                        ts=ts,
                        known_index=k_idx,
                        attrs={
                            "zone": lv.clone(),
                            "level": lv.clone(),
                            "ref": float(lv.extreme),
                            "price": float(c),
                            "ex": float(l),
                            "invalid": float(inv),
                            "probe_invalid": float(probe_inv),
                            "started": bar_index,
                            "sub_kind": f"LEVEL_ACCEPT_{lv.kind.value}",
                        },
                    ))

            # 更新蓄势吸收因果链（无任何 K 线计数或百分比魔法阈值）：
            if (is_res and h >= lv.neck and l <= lv.band_hi and c <= lv.band_hi) or (
                is_sup and l <= lv.neck and h >= lv.band_lo and c >= lv.band_lo
            ):
                state.probed_wall = True
            if state.probed_wall:
                if is_res and lv.probe_lo <= c <= lv.band_hi:
                    state.absorbed_wall = True
                elif is_sup and lv.band_lo <= c <= lv.probe_hi:
                    state.absorbed_wall = True

            if lkey in self.broken_role_keys:
                continue
            ep = self.episodes.get(lkey)
            if ep is None and ((is_sup and pc < lv.extreme) or (is_res and pc > lv.extreme)):
                self.broken_role_keys.add(lkey)
                continue

            if ep is None:
                approached = (
                    (l <= lv.neck and pc > lv.neck and pl >= lv.extreme)
                    if is_sup
                    else (h >= lv.neck and pc < lv.neck and ph <= lv.extreme)
                )
                fast_sweep = (
                    not lv.flipped
                    and lv.kind is not LevelKind.FLIPPED
                    and lv.departed
                    and (
                        (is_sup and pc >= lv.extreme and l < lv.extreme and c > lv.neck and c > o and c > 0.5 * (h + l))
                        or (is_res and pc <= lv.extreme and h > lv.extreme and c < lv.neck and c < o and c < 0.5 * (h + l))
                    )
                )
                if lv.departed and (approached or fast_sweep):
                    swept_now = (l < lv.extreme) if is_sup else (h > lv.extreme)
                    if fast_sweep:
                        anchor_init = 0.5 * (h + l)
                    else:
                        anchor_init = (ph if (swept_now and c > ph) else h) if is_sup else (pl if (swept_now and c < pl) else l)
                    ep = RejectionEpisode(
                        level=lv.clone(),
                        opened_index=bar_index,
                        extreme_px=l if is_sup else h,
                        extreme_index=bar_index,
                        anchor_px=anchor_init,
                        swept=swept_now,
                    )
                    self.episodes[lkey] = ep
            else:
                if is_sup:
                    if l <= ep.extreme_px:
                        ep.extreme_px, ep.extreme_index = l, bar_index
                        ep.anchor_px = (
                            0.5 * (h + l)
                            if (not lv.flipped and lv.kind is not LevelKind.FLIPPED and pc >= lv.extreme and l < lv.extreme and c > lv.neck and c > o and c > 0.5 * (h + l))
                            else h
                        )
                    elif c <= o and h < ep.anchor_px:
                        ep.anchor_px = h
                    if l < lv.extreme:
                        ep.swept = True
                else:
                    if h >= ep.extreme_px:
                        ep.extreme_px, ep.extreme_index = h, bar_index
                        ep.anchor_px = (
                            0.5 * (h + l)
                            if (not lv.flipped and lv.kind is not LevelKind.FLIPPED and pc <= lv.extreme and h > lv.extreme and c < lv.neck and c < o and c < 0.5 * (h + l))
                            else l
                        )
                    elif c >= o and l > ep.anchor_px:
                        ep.anchor_px = l
                    if h > lv.extreme:
                        ep.swept = True

            if ep is None:
                continue

            out_close = (c < lv.extreme) if is_sup else (c > lv.extreme)
            ep.outside_closes = ep.outside_closes + 1 if out_close else 0
            if ep.outside_closes >= 2:
                self.broken_role_keys.add(lkey)
                self.episodes.pop(lkey, None)
                continue

            if (bar_index - ep.opened_index) > 12:
                self.episodes.pop(lkey, None)
                continue

            fire_key = (lv.uid, lv.flip_count, lv.geometry_version)
            shifted = (
                (c > o and c > lv.neck and c > ep.anchor_px)
                if is_sup
                else (c < o and c < lv.neck and c < ep.anchor_px)
            )
            if fire_key not in self.fired_episode_keys and shifted:
                raw_stop = min(lv.extreme, ep.extreme_px) if is_sup else max(lv.extreme, ep.extreme_px)
                inv = (raw_stop - pad) if is_sup else (raw_stop + pad)
                if (is_sup and c <= inv) or ((not is_sup) and c >= inv):
                    continue
                self.fired_episode_keys.add(fire_key)
                if lv.flipped:
                    lv.flip_retested = True
                    self.retested_flip_keys.add(lkey)
                sub_kind = "FLIP_RETEST" if (lv.kind is LevelKind.FLIPPED or lv.flipped) else ("SWEEP_TRAP" if ep.swept else f"LEVEL_REJECT_{lv.kind.value}")
                emitted.append(MarketEvent(
                    id=LEVEL_REJECTED,
                    direction=1 if is_sup else -1,
                    ts=ts,
                    known_index=k_idx,
                    attrs={
                        "zone": lv.clone(),
                        "level": lv.clone(),
                        "ref": float(lv.neck),
                        "ex": float(ep.extreme_px),
                        "ex_index": ep.extreme_index,
                        "invalid": float(inv),
                        "started": ep.opened_index,
                        "swept": ep.swept,
                        "sub_kind": sub_kind,
                    },
                ))
                self.episodes.pop(lkey, None)

        self._cleanup(active_by_key, h, l, c)
        return emitted

    def _cleanup(
        self, active_by_key: dict[tuple[int, int], Level], h: float, l: float, c: float
    ) -> None:
        for lkey in list(self.episodes):
            if lkey not in active_by_key:
                self.episodes.pop(lkey, None)
        for fkey in list(self.fired_episode_keys):
            lv_now = active_by_key.get((fkey[0], fkey[1]))
            if lv_now is None:
                self.fired_episode_keys.discard(fkey)
            else:
                if lv_now.role is LevelRole.SUPPORT and l > lv_now.band_hi and c > lv_now.band_hi:
                    self.fired_episode_keys.discard(fkey)
                elif lv_now.role is LevelRole.RESISTANCE and h < lv_now.band_lo and c < lv_now.band_lo:
                    self.fired_episode_keys.discard(fkey)


def origin_choch_events(
    transitions: Sequence[tuple[str, Optional[Level], Optional[Level]]],
    *,
    ts: int,
    known_index: int,
    high: float,
    low: float,
) -> list[MarketEvent]:
    """从 ``FLIPPED`` 结构转移中派生 ORIGIN CHoCH 事件(唯一判定入口)。

    4H 认知收盘(``EventDetector.update``)与触发周期的增量状态维护共用此函数,
    任一侧都不得另写第二套判定。时钟参数显式传入而非从 bar/structure 推断,
    让调用方自己决定 ``ts``/``known_index`` 的来源,避免把认知周期语义偷渡进
    触发周期。

    语义与抽取前逐字等价:仅当 ``trans == "FLIPPED"`` 且转移前 Level 的 kind
    为 ``ORIGIN`` 才发事件;方向取转移前角色的反侧(原 RESISTANCE ⇒ +1,
    原 SUPPORT ⇒ -1);``ex`` 取当前 bar 对应方向的极值。
    """
    emitted: list[MarketEvent] = []
    for trans, _lv_after, lv_before in transitions:
        if trans != "FLIPPED" or lv_before is None or lv_before.kind is not LevelKind.ORIGIN:
            continue
        direction = 1 if lv_before.role is LevelRole.RESISTANCE else -1
        emitted.append(MarketEvent(
            id=STRUCTURE_BROKEN,
            direction=direction,
            ts=ts,
            known_index=known_index,
            attrs={
                "zone": lv_before.clone(),
                "level": lv_before.clone(),
                "ref": float(lv_before.extreme),
                "ex": float(high if direction > 0 else low),
                "sub_kind": "ORIGIN_CHOCH",
            },
        ))
    return emitted


def dominant_choch_events(
    active_levels: Sequence[Level],
    *,
    prev_close: float,
    bar: tuple[int, float, float, float, float, float],
    known_index: int,
) -> list[MarketEvent]:
    """触发细周期收盘反向击穿冻结高周期主导水平位外沿时派生级联 CHoCH 事件(唯一判定入口)。

    仅对主导二维水平位(``ORIGIN`` / ``EQUAL`` 或 ``prominence_2d > med_prominence_2d``)生效,
    严格要求前一根触发收盘仍在防线优势侧而当根收盘实质越过外沿 ``band_lo`` / ``band_hi``。
    """
    active = [lv for lv in active_levels if lv.is_active]
    if not active:
        return []
    proms = sorted(float(lv.prominence_2d) for lv in active)
    med_prom = proms[len(proms) // 2]
    ts, _o, h, l, c, _v = bar
    pc = float(prev_close)
    emitted: list[MarketEvent] = []
    for lv in active:
        is_dom = lv.kind in (LevelKind.ORIGIN, LevelKind.EQUAL) or float(lv.prominence_2d) > med_prom
        if not is_dom:
            continue
        broke_dn = lv.role is LevelRole.SUPPORT and pc >= lv.band_lo and c < lv.band_lo
        broke_up = lv.role is LevelRole.RESISTANCE and pc <= lv.band_hi and c > lv.band_hi
        if not (broke_dn or broke_up):
            continue
        direction = -1 if broke_dn else 1
        emitted.append(MarketEvent(
            id=STRUCTURE_BROKEN,
            direction=direction,
            ts=int(ts),
            known_index=int(known_index),
            attrs={
                "zone": lv.clone(),
                "level": lv.clone(),
                "ref": float(lv.band_lo if broke_dn else lv.band_hi),
                "ex": float(h if direction > 0 else l),
                "sub_kind": "ORIGIN_CHOCH" if lv.kind is LevelKind.ORIGIN else "DOMINANT_CHOCH",
            },
        ))
    return emitted


class EventDetector:
    """认知周期价格拍卖事件检测器。"""

    def __init__(self, params: Optional[Params] = None, **_ignored_legacy) -> None:
        self.params = params or Params()
        self.structure = MarketStructure(self.params)
        self._auction = LevelAuctionEngine()

    def update(self, bar: Sequence[float]) -> list[MarketEvent]:
        b = _bar_parts(bar)
        t = self.structure.bars_seen
        prev_bar = self.structure.bars[-1] if t > 0 else None
        recent_bars = tuple(self.structure.bars[-4:])
        known_levels = tuple(lv.clone() for lv in self.structure.levels)

        self.structure.update(b)
        atr = self.structure.atr()
        emitted = self._auction.step(
            bar_index=t,
            bar=b,
            prev_bar=prev_bar,
            recent_bars=recent_bars,
            active_levels=known_levels,
            atr=atr,
            k_bm=self.params.k_bm,
        )
        for klv in known_levels:
            if klv.flipped and klv.flip_retested:
                self.structure._scanner._flip_retested_anchors.add(klv.origin_index)
                reg_lv = self.structure._scanner._registry.get((klv.origin_index, klv.role.value))
                if reg_lv is not None:
                    reg_lv.flip_retested = True
                self.structure._scanner._active = [
                    alv for alv in self.structure._scanner._active
                    if not (alv.origin_index == klv.origin_index and alv.role is klv.role)
                ]

        emitted.extend(origin_choch_events(
            self.structure.last_transitions,
            ts=b[0],
            known_index=t,
            high=b[2],
            low=b[3],
        ))
        return emitted



