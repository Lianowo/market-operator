# Area-centric market operator: no score, no indicator, no extra entry gates

**Research-only / Live NO-GO.** This is the continuation of #9 after that PR
was merged into main. Re-implemented on a new branch from the updated main
rather than resurrecting an obsolete parent SHA or overwriting integration
changes.

## Market definition, causal facts and what changed

### Area identity

`market_areas.py` identifies a market place from an actual confirmed defended
pivot or a source-known zone (origin_known, lo, hi, ref and trade direction).
It preserves a region's identity through a resistance↔support role flip,
instead of allowing each E7 to act as an unconnected standalone buy/sell
signal. A break episode transitions `BROKEN -> PROBED -> RECLAIMED` or
`CONSUMED` using ONLY already-seen price bars and the existing structure
invalidation price. There is no fixed expiration after N bars; the observed
duration is recorded in `span_bars` and never used as a numeric threshold.

### Markets vs trades

- E1 / E6: a known structural break; a hypothesis *context* not an
  unconditional reverse-direction order.
- E2 / E3: wick/rejection facts. Not independent range-trades.
- E7: only a rejection/flip of an area *already observed breaking* in the
  same direction. Needs a distinct later closed-bar continuation after E7.
  Different isolated flips cannot borrow a break from another price level.
- E4: trend recovery is not just the latest local two pivots. It has to
  resume a directional *expansion* observed after an earlier defended
  structural level was broken in the same direction. The completed leg
  itself must have advanced beyond that old defended price. This is an
  intrinsic market narrative, not a fixed 0.5/1.5 ATR gate, a moving average
  trend filter or a retrospective gain-based condition.
- E5: range rotation is tied to the causally known interval and region, not
  a free-standing generic rejection.
- A completed trade consumes its structural key. Another identical local
  event does not magically reset the campaign. A new confirmed swing or
  new structural break creates a new auditable key.

### Structural scale

For E7 the recorded span is from the known area origin to the current E7;
for E5 it is from the range area's known origin; for E4 it uses **actual
confirmed pivot forming timestamps** for the pullback's start and end.
Unknown span is represented as `None` rather than pretending it is 0.
An existing `range_max_pivots` cap bounds snapshot memory; it does
not operate as a minimum/maximum trading scale.

## Strict separation from economics and optimization

The freeze of v5 remains intact. `OpportunityRouter` runs only as a
separately selected research route. Ledger, stop-market, gross leverage and
costs are preserved from #9. No new trading library, scoring system, ATR
acceptance threshold, target-R filter or best-parameter search.

Historical BTC/ETH/SOL market data and old test have been used repeatedly
for v1-v5 and the previous #9 replay. All replays are **diagnostic**, not
untouched out-of-sample proof. No mechanism is promoted on a profitable bucket
from a few historical trades.

The *pre-expansion-anchoring* area binding diagnostic (run 39257300675)
found:
- full@1x 1,803 trades, -252.11 U from 300 U, PF 0.6495, max DD ~84%;
- 1,418 E4 trades, -175.41 U; 349 E7 trades, -97.69 U;
- only 36 E5 trades, +20.99 U, economically fragile and insufficient;
- test@1x 562 trades, -145.69 U; test@2x 563 trades, -212.72 U.

These bad outcomes do **not** justify adding threshold filters. They
demonstrate the economic mistake of interpreting local swing recovery
as a mature trend. The new E4 link to an observed structural expansion is
based on that trade definition; it remains to be evaluated without selecting
its parameters by outcome.

## Known limits

1. A single 1h confirmed-pivot model still cannot automatically classify
   every larger opportunity scale. Time spans are now factual and auditable;
   *hierarchical multiple-scale context* is still not implemented.
2. No independently established crypto directional alpha. Even executable
   states and contextual meaning are not profit guarantees.
3. The exchange-native protective-stop orchestration is not certified in
   live markets. The 15m OHLC execution simulation cannot know tick ordering.
4. Multiple overlapping active price areas can be ambiguous; fail closed,
   do not rank areas by prior return.
5. Requires new, future, never-used market data after specification freeze
   to validate economic advantage.

See `tests/test_market_areas.py` and `tests/test_opportunity.py` for causal
invariants and `scripts/diagnose_trade_lifecycle.py` for attribution.
