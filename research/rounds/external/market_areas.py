"""Market-structure area identity and observed auction episodes.

An area is a historical market *relationship* (pivot defended, then broken,
then retested), not a new indicator or an ATR/volume eligibility filter.
All lifecycle transitions consume closed OHLC at or before the current bar.
Price-area width comes from the existing confirmed zone boundaries. The
episode's duration is observed (known_index - origin_known), NEVER an entry
threshold or a fixed-number lookback.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional, Sequence

from market_operator.events import E1, E6, E7, MarketEvent
from market_operator.structure import Zone


@dataclass(frozen=True)
class StructuralArea:
    lo: float
    hi: float
    origin_known: int
    direction: int             # intended direction after the break
    break_known: int
    ref: float                 # broken boundary / defended pivot
    invalid: float             # hypothesis failed on opposite side
    source: str

    @property
    def span_bars(self) -> int:
        """Observed development time, never a score or entry gate."""
        return self.break_known - self.origin_known

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi

    def overlaps(self, other: "StructuralArea") -> bool:
        return max(self.lo, other.lo) <= min(self.hi, other.hi)


@dataclass
class AreaEpisode:
    area: StructuralArea
    stage: str                 # BROKEN -> PROBED -> RECLAIMED or REJECTED
    tested_known: Optional[int] = None
    last_known: Optional[int] = None


class AreaBook:
    """Compact per-symbol record of still-live structural breaks."""

    def __init__(self) -> None:
        self.active: dict[str, list[AreaEpisode]] = {}
        self.history: dict[str, list[tuple[int, str, StructuralArea]]] = {}

    def _log(self, symbol: str, t: int, stage: str, area: StructuralArea) -> None:
        self.history.setdefault(symbol, []).append((t, stage, area))

    @staticmethod
    def _event_area(ev: MarketEvent, zones: Sequence[Zone] = ()) -> StructuralArea:
        if ev.id not in (E1, E6):
            raise ValueError("a structural break requires E1 or E6")
        direction = ev.direction
        ref, invalid = float(ev.attrs["ref"]), float(ev.attrs["invalid"])
        zone = ev.attrs.get("zone")
        if zone is None:
            # E6 is a defended pivot. Prefer its causally known zone when
            # available, but a confirmed pivot itself remains a real level.
            candidates = [z for z in zones if z.lo <= ref <= z.hi]
            zone = min(candidates,
                       key=lambda z: (z.hi - z.lo, z.origin_known),
                       default=None)
        if zone is None:
            lo = hi = ref
            origin = int(ev.attrs.get("pivot_forming", ev.known_index))
        else:
            lo, hi, origin = float(zone.lo), float(zone.hi), zone.origin_known
        if not (math.isfinite(lo) and math.isfinite(hi) and lo <= hi):
            raise ValueError("invalid structural area bounds")
        if direction * (ref - invalid) <= 0:
            # The structural pivot can coincide with the stop reference;
            # invalid must remain on the opposite side of the proposed move.
            raise ValueError("non-protective structural break invalid")
        return StructuralArea(lo, hi, int(origin), direction,
                              ev.known_index, ref, invalid, ev.id)

    def observe_break(self, symbol: str, ev: MarketEvent,
                      zones: Sequence[Zone] = ()) -> AreaEpisode:
        area = self._event_area(ev, zones)
        episodes = self.active.setdefault(symbol, [])
        # A fresh break of the *same actual region* supersedes prior history.
        episodes[:] = [e for e in episodes
                       if not (e.area.direction == area.direction and
                               (e.area.overlaps(area)
                                or e.area.origin_known == area.origin_known))]
        episode = AreaEpisode(area, "BROKEN", last_known=ev.known_index)
        episodes.append(episode)
        self._log(symbol, ev.known_index, "BROKEN", area)
        return episode

    def on_bar(self, symbol: str, t: int, high: float,
               low: float, close: float) -> None:
        kept = []
        for episode in self.active.get(symbol, []):
            a = episode.area
            if t <= a.break_known:
                kept.append(episode)
                continue
            # An opposing close through the actual structural invalidation
            # means the old directional thesis is no longer alive.
            if a.direction * (close - a.invalid) <= 0:
                self._log(symbol, t, "RECLAIMED", a)
                continue
            if high >= a.lo and low <= a.hi:
                if episode.tested_known is None:
                    episode.tested_known = t
                    episode.stage = "PROBED"
                    self._log(symbol, t, "PROBED", a)
            episode.last_known = t
            kept.append(episode)
        self.active[symbol] = kept

    def matched_retest(self, symbol: str, ev: MarketEvent) -> Optional[AreaEpisode]:
        """Only a SAME-AREA retest after a known break can close its auction."""
        if ev.id != E7:
            raise ValueError("expected a zone-flip retest event")
        zone = ev.attrs.get("zone")
        if zone is None:
            return None
        matches = []
        for episode in self.active.get(symbol, []):
            a = episode.area
            if (a.direction != ev.direction
                    or a.break_known >= ev.known_index):
                continue
            # E6 anchor is a defended pivot; E1 is a full zone. Different
            # role labels are expected on a flip and must not be used to
            # discard the real location.
            same = (zone.lo <= a.ref <= zone.hi
                    or (a.lo <= zone.hi and zone.lo <= a.hi))
            if same:
                matches.append(episode)
        # No arbitrary "best" matching area when multiple structures overlap.
        if len(matches) != 1:
            return None
        return matches[0]

    def last_expansion_for_trend(
            self, symbol: str, direction: int,
            leg_extreme: float, asof: int) -> Optional[AreaEpisode]:
        """Find the actual structural expansion to which a pullback belongs.

        A "trend recovery" must recover a directional swing that previously
        advanced beyond a defended structural level. We use strictly
        *observed* E1/E6 breaks, not a minimum-bar-age/ATR score. The latest
        causally established break owns the active leg. If no break has been
        observed since startup, the trend exists as a market observation but
        its trade thesis is not established in this study.
        """
        candidates = [
            episode for episode in self.active.get(symbol, [])
            if episode.area.direction == direction
            and episode.area.break_known < asof
            and direction * (leg_extreme - episode.area.ref) > 0
        ]
        if not candidates:
            return None
        newest = max(x.area.break_known for x in candidates)
        owners = [x for x in candidates if x.area.break_known == newest]
        return owners[0] if len(owners) == 1 else None

    def consume(self, symbol: str, episode: AreaEpisode, at: int) -> None:
        self.active[symbol] = [e for e in self.active.get(symbol, [])
                               if e is not episode]
        self._log(symbol, at, "CONSUMED", episode.area)
