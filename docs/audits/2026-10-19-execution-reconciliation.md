# 2026-10-19 — v4 maker reduction execution-state audit

## Scope and verdict

**ENGINE NO-GO pending regression and independent re-run; STRATEGY NO-GO / Research Only unchanged.**

This is a correctness intervention, **not a strategy optimization**. The original reports
`reports/backtest_v4.*`, `reports/v4_counterfactual.*`, and trade/equity CSVs
were generated **before** this change. Treat them as frozen historical, pre-fix evidence;
**do not** represent them as execution results of this branch.

No entry conditions, GO whitelist, economic assumptions, maker/taker fees, historical
dataset, selection split, or v4 parameter values were changed. The previously reused
test period is **not** a fresh out-of-sample validation set; an execution-correctness
re-run would be a diagnostic, not new independent strategy evidence.

## Confirmed high-severity issue: unfilled reduction counted as executed in position state

### Reproduction from code

1. `PositionManager.on_bar` sees a 1h candle touch `target_1`.
2. Previously `PositionManager._reduce` immediately set `reduced=True`, halved
   `size_frac` and tightened `invalid` to entry (break-even).
3. `run_backtest_v4` then **only armed a maker reduce limit** for up to
   `n_fill = 12` coarse bars. The simulated broker still held the entire
   original quantity until a later real fill (or timeout-to-taker fallback).
4. If the price reversed during that waiting interval, the strategy could exit
   early against a *phantom* tightened stop or report a half-size residual
   before any actual reduction occurred. This is a broken strategy/broker
   state invariant, not a parameter-tuning opportunity.

### Fix

- V4 may **defer** `REDUCE`: `pending_reduce=True` prevents duplicate
  reduction orders, while the actual quantity and structural stop stay unchanged.
- On `LIMIT_FILL(role=reduce)` or a confirmed taker `REDUCE` fill,
  `confirm_reduce` debits the **actual filled quantity / original broker OPEN
  quantity** and then tightens the stop. It never treats an order request as a fill.
- On a rejected fallback order, clear the pending flag; on a protective `EXIT`,
  the manager exits the **entire truly open** quantity, and the broker cancels
  residual resting limits after executing the exit.
- The direct (v1/v2) `PositionManager.on_bar` default behavior stays unchanged
  to avoid silently changing archived protocol results.

### Regression cases

- Pending maker reduction: quantity and invalidation stay unchanged.
- Reversal to a price between original stop and prospective break-even:
  no phantom exit; no duplicate maker reduction.
- Confirmed partial fill: reduction and break-even only then take effect.
- Full invalidation before reduce fill: full remaining quantity exits.
- Canceled/rejected pending reduction: no phantom balance mutation.
- Existing v4 maker/taker accounting and entire regression suite in CI.

## Additional audit blockers NOT solved in this change

1. **No independent net-positive edge**: current v4 test PF 0.29, net -25.13%
   according to the original report; n=32, below the declared threshold.
2. **Shared portfolio leverage/risk limits** need explicit proof. The broker's
   `_fill_open` and `_fill_open_limit` cap each new position by
   `leverage_cap * equity` without deducting *existing positions' notional*.
   This is a per-entry cap and does not, by itself, prove a shared-account
   gross-leverage cap or simultaneous-loss budget. It must be corrected or
   explicitly named a per-position limit before live use.
3. **Intrabar and exchange realism**: 15m OHLCV cannot prove the ordering of
   all events inside the candle, nor actual passive queue fills. The current
   simulated maker fill rule is an assumption, not a verified exchange fill.
4. **Broker/strategy state reconciliation**: the complete Live recovery and
   exchange-driven fill/cancel semantics are not implemented. No auto-trading.
5. **Insufficient fresh OOS**: repeated strategy iterations have reused the
   historical test slice. Reserve a future untouched sample; do not tweak
   v4 thresholds in response to its observed negative outcomes.
6. **Frozen artifacts require an explicit diagnostic regeneration plan**:
   never overwrite the existing reports without versioned paths, engine
   commit SHA, data hashes and detailed old-versus-new trade reconciliation.

## Decision

**No Paper promotion, no Live execution, no automated real orders.**

The work only repairs and tests the invariant **intent != fill** on the
v4 maker partial-reduction path. A full architecture/strategy certification
would require separate follow-up work and independently verified data.
