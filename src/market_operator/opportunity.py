"""Price-behaviour opportunity lifecycle (opt-in, independent of frozen v5).

Price facts and trade rights are different:
  BREAK_OBSERVED: E1 accepted closes or E6 invalidated prior structure.
  RETEST_REJECTED: E7 zone role flip is a candidate, not instant confirmation.
  CONFIRMED: a later *distinct closed bar* demonstrates continuation away from
             the retested zone before structural invalidation. Only then can
             a directional OPEN intent be created.

No additional indicator, k-score, timeframe switch, F1 or R-multiple gate.
The wait is the actual causal sequence needed to resolve the E7 same-bar
high/low ambiguity. Target is a structural observation, not a minimum-R
permission to trade. Unresolved hypotheses are FLAT, not market orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .events import E1, E2, E3, E6, E7, MarketEvent
from .hypothesis import StructureView, build
from .strategy import OrderIntent, OrderKind
from .strategy_v5 import RouteDecision, V5Params, V5Router
from .structure import _bar_parts


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    direction: int
    zone_ref: float
    invalid: float
    observed_index: int
    source_event: MarketEvent
    stage: str              # BREAK_OBSERVED or RETEST_REJECTED
    reject_close: Optional[float] = None


class OpportunityRouter(V5Router):
    """Per-symbol causal opportunity state, no numerical profit filters."""

    def __init__(self) -> None:
        # Whitelist here describes OBSERVABLE facts, not trade-entry approval.
        super().__init__(V5Params(whitelist=frozenset(
            (eid, side) for eid in (E1, E6, E7) for side in (-1, 1))))
        self.opportunities: dict[str, Opportunity] = {}

    def on_bar(self, symbol: str, bar, bar_index: int, view: StructureView,
               atr: float, new_events: Sequence[MarketEvent], *,
               position_open: bool, entry_resting: bool) -> list[RouteDecision]:
        ts, o, h, l, close, _ = _bar_parts(bar)
        self._notes = []
        results: list[RouteDecision] = []
        previous = self.opportunities.get(symbol)

        if position_open or entry_resting:
            # A thesis is consumed by an actual position/order. Existing
            # protective exits and trailing are managed by PositionManager.
            self.opportunities.pop(symbol, None)
            return results

        # Advance only a previously known E7 hypothesis; never confirm E7
        # using the very candle that created it.
        if previous and previous.stage == "RETEST_REJECTED":
            d = previous.direction
            if d * (close - previous.invalid) <= 0:
                self.opportunities.pop(symbol, None)
                self._bump("retest_failed")
                self._notes.append("RETEST_FAILED|structural invalidation crossed")
            elif d * (close - previous.zone_ref) <= 0:
                self.opportunities.pop(symbol, None)
                self._bump("retest_reentered")
                self._notes.append("RETEST_FAILED|price returned through zone edge")
            elif (bar_index > previous.observed_index and
                  d * (close - previous.reject_close) > 0):
                # Confirm continuation on a distinct later close; no fixed
                # amplitude threshold and no lookahead.
                ev = previous.source_event
                hyp = build(ev, view, None, entry=float(close))
                if hyp is not None and d * (close - hyp.invalid) > 0:
                    reason = (f"{ev.id}|{'long' if d>0 else 'short'} "
                              f"RETEST_CONFIRMED reject_known={previous.observed_index} "
                              f"confirmed={bar_index} zone_ref={previous.zone_ref:.8g} "
                              f"invalid={hyp.invalid:.8g} "
                              f"target_1={hyp.target_1} (no R gate)")
                    results.append(RouteDecision(
                        kind="market", route="retest_confirmed",
                        intent=OrderIntent(OrderKind.OPEN, symbol, None, reason,
                                           float(close), ts),
                        hyp=hyp,
                        meta={"source_event": ev.id,
                              "retest_known": previous.observed_index,
                              "confirmation_index": bar_index}))
                    self.opportunities.pop(symbol, None)
                    self._bump("confirmed_entry")
                    return results
                self.opportunities.pop(symbol, None)
                self._bump("invalid_hypothesis")
            else:
                self._notes.append("WAIT|retest has not yet continued")
                return results

        # New E1/E6 is a market fact, never an immediate order.
        for ev in new_events:
            if ev.id not in (E1, E6):
                continue
            ref, invalid = ev.attrs.get("ref"), ev.attrs.get("invalid")
            if ref is None or invalid is None:
                continue
            self.opportunities[symbol] = Opportunity(
                symbol, ev.direction, float(ref), float(invalid),
                bar_index, ev, "BREAK_OBSERVED")
            self._bump("break_observed")
            self._notes.append(f"OBSERVE|{ev.id} structural change, no order")

        # A FLIPPED/RETESTED zone is merely a candidate. Need subsequent
        # market evidence (different closed candle) before risk is committed.
        for ev in new_events:
            if ev.id != E7:
                continue
            ref, invalid = ev.attrs.get("ref"), ev.attrs.get("invalid")
            if ref is None or invalid is None:
                continue
            if ev.direction * (float(close) - float(invalid)) <= 0:
                continue
            # An E7 may represent a different zone from an E6 pivot. The E7
            # itself carries a causal zone-role-flip fact; use ITS own zone
            # and invalidation, never fabricate same-episode equality.
            self.opportunities[symbol] = Opportunity(
                symbol, ev.direction, float(ref), float(invalid),
                bar_index, ev, "RETEST_REJECTED", float(close))
            self._bump("retest_observed")
            self._notes.append("OBSERVE|E7 rejected retest; waiting a later close")
        return results
