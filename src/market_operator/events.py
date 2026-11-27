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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .level import Level, LevelKind, LevelRole
from .structure import MarketStructure, Params, _bar_parts

BREAKOUT_ACCEPTED = "BREAKOUT_ACCEPTED"
LEVEL_REJECTED = "LEVEL_REJECTED"
STRUCTURE_BROKEN = "STRUCTURE_BROKEN"

ACCEPT = BREAKOUT_ACCEPTED
REJECT = LEVEL_REJECTED
CONSUMED = "CONSUMED"

EVENT_ALIASES: Mapping[str, str] = MappingProxyType({
    "ACCEPT": BREAKOUT_ACCEPTED,
    "REJECT": LEVEL_REJECTED,
    "BREAKOUT_ACCEPTED": BREAKOUT_ACCEPTED,
    "LEVEL_REJECTED": LEVEL_REJECTED,
    "STRUCTURE_BROKEN": STRUCTURE_BROKEN,
    "CONSUMED": CONSUMED,
})

EVENT_IDENTIFIERS = (
    BREAKOUT_ACCEPTED,
    LEVEL_REJECTED,
    STRUCTURE_BROKEN,
)

_MAX_REJECTION_EPISODE_BARS = 12
_OUTSIDE_CLOSE_INVALIDATION_COUNT = 2


def canonical_event_id(event_id: object) -> str:
    text = str(event_id).strip()
    return EVENT_ALIASES.get(text, text)


@dataclass(frozen=True)
class MarketEvent:
    id: str
    direction: int
    ts: int
    known_index: int
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return canonical_event_id(self.id)

    @property
    def level(self) -> Level | None:
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


class LevelAuctionEngine:
    """零魔法阈值、纯结构破坏与价值接受驱动的原子水平位拍卖引擎(认知层与触发层完全同构共用)。"""

    _STALE_PRUNE_BARS = 320

    def __init__(self) -> None:
        self.episodes: dict[tuple[int, int], RejectionEpisode] = {}
        self.fired_episode_keys: set[tuple[int, int, int]] = set()
        self.fired_breakout_keys: set[tuple[int, int]] = set()
        self.broken_role_keys: set[tuple[int, int]] = set()
        self.retested_flip_keys: set[tuple[int, int]] = set()
        self.states: dict[tuple[int, int], LevelAuctionState] = {}
        self._last_seen_bar: dict[tuple[int, int], int] = {}

    def _try_emit_breakout(
        self,
        lkey: tuple[int, int],
        lv: Level,
        state: LevelAuctionState,
        *,
        bar_index: int,
        k_idx: int,
        bar: tuple[int, float, float, float, float, float],
        prev_bar: tuple[int, float, float, float, float, float],
        pad: float,
    ) -> MarketEvent | None:
        """当根夺权突破确认:须先完成墙前吸收,且当前棒实体破界吞没前一根压制极值。"""
        if not state.absorbed_wall or lkey in self.fired_breakout_keys:
            return None
        ts, o, h, lo, c, _ = bar
        _, _, ph, pl, pc, _ = prev_bar
        is_res = lv.role is LevelRole.RESISTANCE

        if is_res and pc <= lv.band_hi and c > lv.band_hi and c > o and c > ph:
            direction = 1
            inv = min(lv.band_lo, lo) - pad
            probe_inv = min(lv.probe_lo, lo) - pad
            ex_px = float(h)
        elif (not is_res) and pc >= lv.band_lo and c < lv.band_lo and c < o and c < pl:
            direction = -1
            inv = max(lv.band_hi, h) + pad
            probe_inv = max(lv.probe_hi, h) + pad
            ex_px = float(lo)
        else:
            return None

        self.fired_breakout_keys.add(lkey)
        return MarketEvent(
            id=BREAKOUT_ACCEPTED,
            direction=direction,
            ts=ts,
            known_index=k_idx,
            attrs={
                "zone": lv.clone(),
                "level": lv.clone(),
                "ref": float(lv.extreme),
                "price": float(c),
                "ex": ex_px,
                "invalid": float(inv),
                "probe_invalid": float(probe_inv),
                "started": bar_index,
                "sub_kind": f"LEVEL_ACCEPT_{lv.kind.value}",
            },
        )

    @staticmethod
    def _update_wall_absorption(
        state: LevelAuctionState, lv: Level, h: float, lo: float, c: float
    ) -> None:
        """更新蓄势吸收因果链(先探入墙带,再在墙前区间收盘吸收)。"""
        is_res = lv.role is LevelRole.RESISTANCE
        if (is_res and h >= lv.neck and lo <= lv.band_hi and c <= lv.band_hi) or (
            (not is_res) and lo <= lv.neck and h >= lv.band_lo and c >= lv.band_lo
        ):
            state.probed_wall = True
        if not state.probed_wall:
            return
        if is_res and lv.probe_lo <= c <= lv.band_hi:
            state.absorbed_wall = True
        elif (not is_res) and lv.band_lo <= c <= lv.probe_hi:
            state.absorbed_wall = True

    def _step_rejection_episode(
        self,
        lkey: tuple[int, int],
        lv: Level,
        *,
        bar_index: int,
        bar: tuple[int, float, float, float, float, float],
        prev_bar: tuple[int, float, float, float, float, float],
    ) -> RejectionEpisode | None:
        """Open or update the active rejection auction episode on ``lv``."""
        _, o, h, lo, c, _ = bar
        _, _, ph, pl, pc, _ = prev_bar
        is_sup = lv.role is LevelRole.SUPPORT
        ep = self.episodes.get(lkey)

        if ep is None:
            if (is_sup and pc < lv.extreme) or ((not is_sup) and pc > lv.extreme):
                self.broken_role_keys.add(lkey)
                return None
            approached = (
                (lo <= lv.neck and pc > lv.neck and pl >= lv.extreme)
                if is_sup
                else (h >= lv.neck and pc < lv.neck and ph <= lv.extreme)
            )
            fast_sweep = (
                not lv.flipped
                and lv.kind is not LevelKind.FLIPPED
                and lv.departed
                and (
                    (is_sup and pc >= lv.extreme and lo < lv.extreme and c > lv.neck and c > o and c > 0.5 * (h + lo))
                    or ((not is_sup) and pc <= lv.extreme and h > lv.extreme and c < lv.neck and c < o and c < 0.5 * (h + lo))
                )
            )
            if not (lv.departed and (approached or fast_sweep)):
                return None
            swept_now = (lo < lv.extreme) if is_sup else (h > lv.extreme)
            if fast_sweep:
                anchor_init = 0.5 * (h + lo)
            elif is_sup:
                anchor_init = ph if (swept_now and c > ph) else h
            else:
                anchor_init = pl if (swept_now and c < pl) else lo
            ep = RejectionEpisode(
                level=lv.clone(),
                opened_index=bar_index,
                extreme_px=lo if is_sup else h,
                extreme_index=bar_index,
                anchor_px=anchor_init,
                swept=swept_now,
            )
            self.episodes[lkey] = ep
            return ep

        if is_sup:
            if lo <= ep.extreme_px:
                ep.extreme_px, ep.extreme_index = lo, bar_index
                is_fast = (
                    not lv.flipped
                    and lv.kind is not LevelKind.FLIPPED
                    and pc >= lv.extreme
                    and lo < lv.extreme
                    and c > lv.neck
                    and c > o
                    and c > 0.5 * (h + lo)
                )
                ep.anchor_px = 0.5 * (h + lo) if is_fast else h
            elif c <= o and h < ep.anchor_px:
                ep.anchor_px = h
            if lo < lv.extreme:
                ep.swept = True
        else:
            if h >= ep.extreme_px:
                ep.extreme_px, ep.extreme_index = h, bar_index
                is_fast = (
                    not lv.flipped
                    and lv.kind is not LevelKind.FLIPPED
                    and pc <= lv.extreme
                    and h > lv.extreme
                    and c < lv.neck
                    and c < o
                    and c < 0.5 * (h + lo)
                )
                ep.anchor_px = 0.5 * (h + lo) if is_fast else lo
            elif c >= o and lo > ep.anchor_px:
                ep.anchor_px = lo
            if h > lv.extreme:
                ep.swept = True
        return ep

    def _try_emit_rejection(
        self,
        lkey: tuple[int, int],
        lv: Level,
        ep: RejectionEpisode,
        *,
        bar_index: int,
        k_idx: int,
        bar: tuple[int, float, float, float, float, float],
        pad: float,
    ) -> MarketEvent | None:
        ts, o, _h, _lo, c, _ = bar
        is_sup = lv.role is LevelRole.SUPPORT

        out_close = (c < lv.extreme) if is_sup else (c > lv.extreme)
        ep.outside_closes = ep.outside_closes + 1 if out_close else 0
        if ep.outside_closes >= _OUTSIDE_CLOSE_INVALIDATION_COUNT:
            self.broken_role_keys.add(lkey)
            self.episodes.pop(lkey, None)
            return None
        if (bar_index - ep.opened_index) > _MAX_REJECTION_EPISODE_BARS:
            self.episodes.pop(lkey, None)
            return None

        fire_key = (lv.uid, lv.flip_count, lv.geometry_version)
        shifted = (
            (c > o and c > lv.neck and c > ep.anchor_px)
            if is_sup
            else (c < o and c < lv.neck and c < ep.anchor_px)
        )
        if fire_key in self.fired_episode_keys or not shifted:
            return None

        raw_stop = min(lv.extreme, ep.extreme_px) if is_sup else max(lv.extreme, ep.extreme_px)
        inv = (raw_stop - pad) if is_sup else (raw_stop + pad)
        if (is_sup and c <= inv) or ((not is_sup) and c >= inv):
            return None

        self.fired_episode_keys.add(fire_key)
        if lv.flipped:
            lv.flip_retested = True
            self.retested_flip_keys.add(lkey)
        if lv.kind is LevelKind.FLIPPED or lv.flipped:
            sub_kind = "FLIP_RETEST"
        elif ep.swept:
            sub_kind = "SWEEP_TRAP"
        else:
            sub_kind = f"LEVEL_REJECT_{lv.kind.value}"

        self.episodes.pop(lkey, None)
        return MarketEvent(
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
        )

    def step(
        self,
        *,
        bar_index: int,
        bar: tuple[int, float, float, float, float, float],
        prev_bar: tuple[int, float, float, float, float, float] | None,
        recent_bars: Sequence[tuple[int, float, float, float, float, float]],
        active_levels: Sequence[Level],
        atr: float,
        k_bm: float,
        known_index_override: int | None = None,
    ) -> list[MarketEvent]:
        _ = recent_bars  # Retained for caller signature compatibility
        _, _o, h, lo, c, _ = bar
        k_idx = bar_index if known_index_override is None else int(known_index_override)
        pad = max(0.0, float(k_bm) * float(atr))
        emitted: list[MarketEvent] = []
        if prev_bar is None:
            return emitted
        active_by_key = {(lv.uid, lv.flip_count): lv for lv in active_levels if lv.is_active}

        for lkey, lv in active_by_key.items():
            if lv.flipped and (lv.flip_retested or lkey in self.retested_flip_keys):
                lv.flip_retested = True
                continue
            is_sup = lv.role is LevelRole.SUPPORT
            if lv.flipped and ((is_sup and c < lv.band_lo) or ((not is_sup) and c > lv.band_hi)):
                lv.flip_retested = True
                self.retested_flip_keys.add(lkey)
                self.episodes.pop(lkey, None)
                continue

            state = self.states.setdefault(lkey, LevelAuctionState())
            brk_ev = self._try_emit_breakout(
                lkey, lv, state, bar_index=bar_index, k_idx=k_idx, bar=bar, prev_bar=prev_bar, pad=pad
            )
            if brk_ev is not None:
                emitted.append(brk_ev)

            self._update_wall_absorption(state, lv, h, lo, c)

            if lkey in self.broken_role_keys:
                continue
            ep = self._step_rejection_episode(lkey, lv, bar_index=bar_index, bar=bar, prev_bar=prev_bar)
            if ep is None:
                continue
            rej_ev = self._try_emit_rejection(
                lkey, lv, ep, bar_index=bar_index, k_idx=k_idx, bar=bar, pad=pad
            )
            if rej_ev is not None:
                emitted.append(rej_ev)

        self._cleanup(active_by_key, h, lo, c, bar_index)
        return emitted

    def _cleanup(
        self,
        active_by_key: dict[tuple[int, int], Level],
        h: float,
        lo: float,
        c: float,
        bar_index: int = 0,
    ) -> None:
        for lkey in active_by_key:
            self._last_seen_bar[lkey] = bar_index
        for lkey in list(self.episodes):
            if lkey not in active_by_key:
                self.episodes.pop(lkey, None)
        for fkey in list(self.fired_episode_keys):
            lv_now = active_by_key.get((fkey[0], fkey[1]))
            if lv_now is None:
                self.fired_episode_keys.discard(fkey)
            elif lv_now.role is LevelRole.SUPPORT and lo > lv_now.band_hi and c > lv_now.band_hi:
                self.fired_episode_keys.discard(fkey)
            elif lv_now.role is LevelRole.RESISTANCE and h < lv_now.band_lo and c < lv_now.band_lo:
                self.fired_episode_keys.discard(fkey)
        # 24/7 常驻内存上限治理: 超出滑动画布久未出现的历史水平位键永久不可重现,安全裁剪
        cutoff = bar_index - self._STALE_PRUNE_BARS
        if cutoff > 0:
            stale = [
                k for k, seen in self._last_seen_bar.items()
                if k not in active_by_key and seen < cutoff
            ]
            for k in stale:
                self._last_seen_bar.pop(k, None)
                self.states.pop(k, None)
                self.fired_breakout_keys.discard(k)
                self.broken_role_keys.discard(k)
                self.retested_flip_keys.discard(k)


def origin_choch_events(
    transitions: Sequence[tuple[str, Level | None, Level | None]],
    *,
    ts: int,
    known_index: int,
    high: float,
    low: float,
) -> list[MarketEvent]:
    """从 ``FLIPPED`` 结构转移中派生 ORIGIN CHoCH 事件(唯一判定入口)。"""
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
    """触发细周期收盘反向击穿冻结高周期主导水平位外沿时派生级联 CHoCH 事件(唯一判定入口)。"""
    active = [lv for lv in active_levels if lv.is_active]
    if not active:
        return []
    proms = sorted(float(lv.prominence_2d) for lv in active)
    med_prom = proms[len(proms) // 2]
    ts, _o, h, lo, c, _v = bar
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
                "ex": float(h if direction > 0 else lo),
                "sub_kind": "ORIGIN_CHOCH" if lv.kind is LevelKind.ORIGIN else "DOMINANT_CHOCH",
            },
        ))
    return emitted


class EventDetector:
    """认知周期价格拍卖事件检测器。"""

    def __init__(self, params: Params | None = None, **_ignored_legacy: Any) -> None:
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
                self.structure.mark_level_flip_retested(klv.origin_index, klv.role)

        emitted.extend(origin_choch_events(
            self.structure.last_transitions,
            ts=b[0],
            known_index=t,
            high=b[2],
            low=b[3],
        ))
        return emitted



