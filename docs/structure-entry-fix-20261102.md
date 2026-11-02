# Structure-entry repair — 2026-11-02

> Historical archive: the dedicated study scripts, experimental APIs, candidate configuration and raw generated artifacts described below were removed on 2026-11-02. Commands below are historical, NOT current runnable instructions. Read `session-cleanup-20261102.md` for retained code and `research-results-archive-20261102.md` for archived result tables. Production defaults were not changed by cleanup.


> Historical first-pass report. The implicit Watch lower-bound gate and reentry
> target-management coupling described below have subsequently been removed.
> Current contract and replay results: `structure-entry-decoupled-20261102.md`.

## Status: implemented, NOT production-return accepted

No live process was restarted and no orders were submitted by this repair.
The production strategy configuration is unchanged. Loading the modified code
still changes REJECT admission semantics; do not treat this as a parity refactor.

## Contracts

- Targeted REJECT cannot enter after its recorded opposite edge has passed or
  without a valid forward target. With the existing deadzone Watch enabled,
  its configured lower bound also applies to admitted entries: lower-R market
  entries no longer bypass the Watch interval.
- Reentry structural stop and box target remain explicit configuration choices.
  When enabled, Watch preserves the event structural stop. Remaining space uses
  entry-to-target distance, not full box width. Generic A2 limit conversion must
  not discard the reentry target.
- Bounded entry metadata survives routing, replay fills and live Watch execution.
  Actual fill/reference prices are checked again for protective invalidation,
  forward target and configured minimum remaining R. Live market orders require
  an available bid/ask quote for this check; quotes may be cached up to 15 seconds.
  Market gaps after submission cannot be eliminated by a pre-submit check.
- Final contract rejection restores zone locks; rejected ideas do not consume
  the next valid opportunity.
- Signals distinguish event boundary, process extreme, close confirmation,
  Watch reference and sampled last price. Missing quote is explicit. Snapshot
  timestamps are sampling times, not exchange tick timestamps or fill receipts.
  Displayed confirmation time is the bar close, while order identity retains the
  existing bar-label timestamp. Live expiry starts at confirmation close.
- `signal_structure` journals the decision context even when execution is blocked.

## Candidate

`config/strategy.structure-entry-candidate.json` is a full standalone research
configuration. It enables structural reentry stop/target and an explicit 1R
bounded-entry floor. This floor is a research candidate, not an optimized value.
The 1.2 ATR far-confirm filter remains disabled; no optimal threshold was inferred.

## Offline replay comparison

BTC/ETH/SOL/DOGE, 2h/4h/12h/1d, stored 1h data labels from
2024-10-15 20:00 to 2026-10-19 03:00 UTC. 300 USDT starting equity,
production replay risk/cost/capacity settings; NOT live 5% compounding.
Baseline router was loaded from Git HEAD in memory; all runs shared the same
cognition frames and execution engine. Baseline orders carry no new contract
metadata, so new execution checks are inactive for baseline trades.

| Segment/cost | Baseline net / closed | Fixed, current config | Candidate |
|---|---:|---:|---:|
| dev / 1x | 340.1540 / 443 | 254.4223 / 268 | 168.5541 / 220 |
| test / 1x | 163.4918 / 226 | 120.2780 / 129 | 110.4878 / 118 |
| full / 1x | 680.7830 / 669 | 488.5698 / 399 | 331.2276 / 337 |
| full / 1.5x | 354.9018 / 665 | 330.0232 / 395 | 220.8394 / 334 |

All variants remain positive in these scenarios, but both modifications lose
absolute net return versus baseline. They FAIL the repository's return-floor
acceptance principle. No claim of profitability improvement or deployment
readiness is made. Full/dev/test are independent runs, not additive totals.