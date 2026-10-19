"""Causal area-centric market opportunities, opt-in to keep frozen v5 intact.

The decision chain is *place -> approach/test -> break or reject -> acceptance
or failure -> trade hypothesis -> fill*. E1/E6 are observations, not orders.
E7 is tradable only as the continuation of a KNOWN break of the SAME AREA.
E4/E5 are trend/range continuation opportunities anchored to existing swing/
band facts; ordinary local E2/E3 rejections are never independent trades.

No additional indicators, R gates, arbitrary time windows, or backtest-tuned
thresholds. A second confirmed candle solves ambiguous same-candle OHLC
ordering but does not claim profitable alpha. Full trade costs/stop execution
belong to Broker; this module owns only hypothesis state.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional, Sequence

from .events import E1, E2, E3, E4, E5, E6, E7, MarketEvent
from .hypothesis import StructureView, build
from .market_areas import AreaBook, AreaEpisode
from .strategy import OrderIntent, OrderKind
from .strategy_v5 import RouteDecision, V5Params, V5Router
from .structure import PivotKind, Regime, _bar_parts


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    direction: int
    zone_ref: float
    invalid: float
    observed_index: int
    source_event: MarketEvent
    snapshot: StructureView
    stage: str
    reject_close: Optional[float] = None
    # Causal fingerprint of a SINGLE defended pivot, range or broken region.
    identity: tuple = ()
    break_episode: Optional[AreaEpisode] = None
    scale_bars: Optional[int] = None  # actual formation span; unknown != 0


class OpportunityRouter(V5Router):
    """One live thesis per symbol; area book remembers related market facts."""

    def __init__(self) -> None:
        super().__init__(V5Params(whitelist=frozenset(
            (eid, side) for eid in (E1, E2, E3, E4, E5, E6, E7)
            for side in (-1, 1))))
        self.opportunities: dict[str, Opportunity] = {}
        self.areas = AreaBook()
        self.consumed: set[tuple] = set()

    @staticmethod
    def _identity(symbol: str, ev: MarketEvent,
                  linked_break: Optional[AreaEpisode]) -> Optional[tuple]:
        if ev.id == E7:
            if linked_break is None:
                return None
            a = linked_break.area
            return (symbol, "FLIP", a.source, a.origin_known,
                    a.break_known, a.ref, a.direction)
        if ev.id == E4:
            # Same confirmed pullback cannot be traded repeatedly as fresh
            # price noise; a NEW pivot makes a NEW structural campaign.
            if not all(k in ev.attrs for k in ("pb_price", "leg_start", "regime")):
                return None
            return (symbol, "TREND", ev.direction,
                    float(ev.attrs["pb_price"]), float(ev.attrs["leg_start"]))
        if ev.id == E5:
            zone, band = ev.attrs.get("zone"), ev.attrs.get("band")
            if zone is None or band is None:
                return None
            return (symbol, "RANGE", ev.direction, zone.origin_known,
                    float(band[0]), float(band[1]))
        return None

    @staticmethod
    def _context_consistent(ev: MarketEvent, view: StructureView) -> bool:
        # A contextual definition, not a new score/filter:
        # E4 must actually be a trend recovery, E5 an established range.
        if ev.id == E4:
            expected = (Regime.TREND_UP if ev.direction > 0
                        else Regime.TREND_DOWN)
            return (ev.attrs.get("regime") == expected.value and
                    view is not None and view.regime == expected)
        if ev.id == E5:
            band = ev.attrs.get("band")
            return (view is not None and view.regime == Regime.RANGE
                    and band is not None and float(band[0]) < float(band[1]))
        return True

    @staticmethod
    def _observed_scale(ev: MarketEvent, view: StructureView,
                        bar_index: int,
                        linked: Optional[AreaEpisode]) -> Optional[int]:
        """Measure actual known pivot/area span, never select on its value."""
        if linked is not None:
            return bar_index - linked.area.origin_known
        if ev.id == E5:
            return bar_index - int(ev.attrs["zone"].origin_known)
        if ev.id != E4 or view is None:
            return None
        pb_price = float(ev.attrs["pb_price"])
        opposite = PivotKind.HIGH if ev.direction > 0 else PivotKind.LOW
        pb_kind = PivotKind.LOW if ev.direction > 0 else PivotKind.HIGH
        pivots = tuple(view.pivots)
        # The pivot whose formation ended this pullback is already confirmed
        # by the E4 event. Span is the elapsed market time between actual
        # turning points, not a fixed number of K-lines.
        for index in range(len(pivots) - 1, -1, -1):
            pivot = pivots[index]
            if (pivot.kind is pb_kind
                    and math.isclose(pivot.price, pb_price, abs_tol=1e-10)):
                for earlier in reversed(pivots[:index]):
                    if earlier.kind is opposite:
                        return pivot.forming_index - earlier.forming_index
        return None

    def on_bar(self, symbol: str, bar, bar_index: int,
               view: Optional[StructureView], atr: float,
               new_events: Sequence[MarketEvent], *,
               position_open: bool, entry_resting: bool) -> list[RouteDecision]:
        ts, o, h, l, close, _ = _bar_parts(bar)
        self._notes = []
        self.areas.on_bar(symbol, bar_index, h, l, close)
        previous = self.opportunities.get(symbol)

        if position_open or entry_resting:
            # Trade cycle is owned by actual Broker fills and PositionManager.
            self.opportunities.pop(symbol, None)
            return []

        # Resolve a PENDING trade thesis using only a DIFFERENT subsequent bar.
        # An opposite structural break is competing evidence, never ignored.
        if previous and previous.stage in ("RETEST_REJECTED", "REACTION_PENDING"):
            d = previous.direction
            adverse = any(ev.id in (E1, E6) and ev.direction == -d
                          for ev in new_events)
            still_linked = (previous.break_episode is None or
                            previous.break_episode in
                            self.areas.active.get(symbol, []))
            if (adverse or not still_linked or
                    d * (close - previous.invalid) <= 0 or
                    (previous.source_event.id == E7 and
                     d * (close - previous.zone_ref) <= 0)):
                self.opportunities.pop(symbol, None)
                self._bump("thesis_failed")
                self._notes.append("FAILED|reclaimed level or adverse structure")
            elif (bar_index > previous.observed_index and
                  previous.reject_close is not None and
                  d * (close - previous.reject_close) > 0):
                ev = previous.source_event
                # Event-time frozen structural snapshot, never future zones.
                hyp = build(ev, previous.snapshot, None, entry=float(close))
                if hyp is not None and d * (close - hyp.invalid) > 0:
                    identity = previous.identity
                    if identity in self.consumed:
                        raise AssertionError("same structural trade already consumed")
                    if previous.break_episode is not None:
                        self.areas.consume(symbol, previous.break_episode, bar_index)
                    self.consumed.add(identity)
                    self.opportunities.pop(symbol, None)
                    self._bump("confirmed_entry")
                    reason = (f"{ev.id}|{'long' if d > 0 else 'short'} "
                              f"AREA_CONFIRMED key={identity!r} "
                              f"observed={previous.observed_index} "
                              f"confirmed={bar_index} "
                              f"structure_span={previous.scale_bars}bars "
                              f"invalid={hyp.invalid:.8g} target={hyp.target_1}")
                    return [RouteDecision(
                        kind="market", route="area_confirmed",
                        intent=OrderIntent(OrderKind.OPEN, symbol, None,
                                           reason, float(close), ts),
                        hyp=hyp,
                        meta={"source_event": ev.id, "area_key": repr(identity),
                              "reaction_known": previous.observed_index,
                              "confirmation_index": bar_index,
                              "structure_span_bars": previous.scale_bars})]
                self.opportunities.pop(symbol, None)
                self._bump("invalid_hypothesis")
            else:
                self._notes.append("WAIT|area still unresolved")

        # Record every observed break EVEN IF we are not ready to trade it.
        # E1 and E6 cannot silently claim a directional market order.
        for ev in new_events:
            if ev.id not in (E1, E6):
                continue
            if "ref" not in ev.attrs or "invalid" not in ev.attrs:
                continue
            zones = () if view is None else view.zones
            episode = self.areas.observe_break(symbol, ev, zones)
            self._bump("break_observed")
            self._notes.append(
                f"BROKEN|{ev.id} {episode.area.lo:.8g}..{episode.area.hi:.8g}"
                " no entry")

        # E2/E3 are pure market observations (local rejection/test).
        for ev in new_events:
            if ev.id in (E2, E3):
                self._bump(f"local_rejection_observed_{ev.id}")

        candidates = [e for e in new_events if e.id in (E4, E5, E7)]
        # Competing unrelated locations or sides on one bar cannot be
        # transformed into a single magical "best" trade.
        if len({(e.id, e.direction, e.attrs.get("ref")) for e in candidates}) > 1:
            self.opportunities.pop(symbol, None)
            self._bump("competing_areas")
            return []

        for ev in candidates:
            ref, invalid = ev.attrs.get("ref"), ev.attrs.get("invalid")
            if ref is None or invalid is None:
                continue
            if ev.direction * (close - float(invalid)) <= 0:
                continue
            if not self._context_consistent(ev, view):
                self._bump("inconsistent_market_context")
                continue
            linked = self.areas.matched_retest(symbol, ev) if ev.id == E7 else None
            if ev.id == E7 and linked is None:
                self._bump("unanchored_flip")  # a local zone flip, not a campaign
                continue
            identity = self._identity(symbol, ev, linked)
            if identity is None:
                self._bump("incomplete_area_evidence")
                continue
            if identity in self.consumed:
                self._bump("previously_consumed_area")
                continue
            if view is None:
                raise AssertionError("an emitted event needs a known-time view")
            # If a prior phase is unresolved, a new unrelated area supersedes
            # it explicitly; there is no hidden score/priority arbitration.
            if symbol in self.opportunities:
                self._bump("area_superseded")
            scale = self._observed_scale(ev, view, bar_index, linked)
            self.opportunities[symbol] = Opportunity(
                symbol, ev.direction, float(ref), float(invalid),
                bar_index, ev, view, "RETEST_REJECTED" if ev.id == E7
                else "REACTION_PENDING", float(close),
                identity, linked, scale)
            self._bump(f"area_reaction_observed_{ev.id}")
            self._notes.append(
                f"REACTION|{ev.id} area={identity!r} "
                "requires causal follow-through")
        return []
