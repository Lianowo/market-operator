# Minimal open-source core extraction (2026-10-19)

## Decision

**No Freqtrade, Jesse, Hummingbot, NautilusTrader, multi-service runtime or
additional third-party dependency.** This branch only adds self-contained Python
standard-library functionality and corrects the current project's broker.

A framework is not the alpha. The previously frozen v4 E7 strategy remains
**NO-GO**: full@1x -56.00%, test@1x -25.13%, n=32. Nothing in this change
selects different signals or promotes paper/live trading.

## Source-code review and exact extraction boundary

| Reviewed source | License | Valuable source-level mechanism | How implemented here |
|---|---|---|---|
| [Hummingbot `hummingbot/connector/client_order_tracker.py`](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/connector/client_order_tracker.py) and [`hummingbot/core/data_type/in_flight_order.py`](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/core/data_type/in_flight_order.py) | Apache-2.0 | client/exchange order IDs, granular fill updates, canceled orders with pending fills, restore tracking state | Independently rewritten as `market_operator/orders.py` with SQLite WAL, unique exchange execution IDs, fail-closed UNKNOWN status and restart reconciliation. No upstream code copied. |
| [Jesse `jesse/strategies/Strategy.py`](https://github.com/jesse-ai/jesse/blob/master/jesse/strategies/Strategy.py) | MIT | position changes based on executed order quantity (open/increase/reduce/close) | Keep our already-implemented fill-confirmed `PositionManager` callbacks in `backtest_v4.py`; do not install Jesse. |
| [stolgo `lib/stolgo/trade/bracket.py`](https://github.com/stockalgo/stolgo/blob/master/lib/stolgo/trade/bracket.py) | MIT | stop-based position sizing, structural trade plan | Use existing `broker.py` plus independent account-level prospective stop-risk cap; deliberately avoid stolgo's OHLC-only stop-fill assumptions. No code copied. |
| [pymarket-structure](https://github.com/fortunato/pymarket-structure) | MIT | incremental confirmed pivots and zones | Existing `structure.py` and `events.py` already implement causal incremental pivots, zone state machine, and event known-index; reuse rather than duplicate. |
| [PersonalAIOnMobile V25](https://github.com/skyliner2008/PersonalAIOnMobile) | License not confirmed | approach/test/accept/reject/flip behavioral state transitions | Refer only to general architecture, no source-code copying. Existing E1–E7 events form the event vocabulary. |
| [geektrade-strategies](https://github.com/grinay/geektrade-strategies) | MIT | partial exit / trend runner concept | Existing `PositionManager` contains this path; add nothing to avoid unproven signal changes. |

## Exact code delivered

- `src/market_operator/orders.py`: ~single-module SQLite order/fill state ledger,
  deterministic order ID, deduped fills, partial-fills including cancel races,
  execution VWAP/fees, restart fail-closed order reconciliation, venue-position
  verification. **Only real fills change actual position quantity.**
- `src/market_operator/broker.py`: optional `portfolio_stop_risk_frac`
  parameter; across all symbols, size entry/add by both gross notional leverage
  and prospective aggregate loss from marked price to latest structural
  invalidation. No separate risk service or new package.
- `src/market_operator/backtest_v4.py`: synchronize only confirmed structural
  invalidation changes to broker; do not treat unfilled limit orders as fills.
  Optional `execution_ledger` projects actual historical Broker execution rows
  into the same durable journal and checks reconstructed final positions against
  the Broker; replay is idempotent by explicit `ledger_run_id`.
- `src/market_operator/position.py` + `broker.py`: trend ADD now also supports
  a deferred execution path. Intent does not change quantity/entry/stop;
  confirmed ADD applies exact fill qty and VWAP, while rejected ADD clears the
  pending flag and leaves original position unchanged. The Broker recomputes
  the risk-neutral constraint at *actual fill price* before allowing addition.
  Frozen v4 still has `allow_add=False`; this is a correctness mechanism, **not**
  evidence of profitable pyramiding.
- `tests/test_orders.py` and `tests/test_portfolio_stop_risk.py`: regression
  cases for idempotency, late fills, restart, position mismatch, portfolio risk,
  stop tightening, and frozen v4 compatibility.

**Freeze:** To preserve the archived v4 protocol results,
`portfolio_stop_risk_frac` defaults to `None` in existing v4 calls. A future
*separately preregistered* strategy/replay version must explicitly enable it
(e.g., 0.015), and must then be measured as a distinct version. Do not
reinterpret historical v4 metrics as if the risk cap had been active.

## Deliberate safety boundary

The implementation includes the durable **core** but **not** any live exchange
credentials, live HTTP order sender, or automatic trading action. To send orders,
an independently tested venue-specific adapter must: persist a deterministic
client order ID, call `mark_submitted` BEFORE network transmission, send once,
poll exchange order + execution details on ambiguous timeouts, call
`reconcile_order` and `record_fill`, verify the real exchange position,
then unblock further risk. Caller must also maintain protective stop orders on
the exchange and handle partial-fill protection. Neither paper nor live is
authorized merely by this module.

Portfolio stop risk is a *planned price-loss budget*, not a hard guarantee:
exchange gaps, liquidation rules, slippage and adverse fills can exceed it.

No direct upstream code was copied, so the project retains its MIT original
code license; respect upstream license notices if future copy/paste is adopted.

## Validation criteria

1. Whole test suite passes on GitHub Actions for this precise head SHA.
2. No new `pyproject.toml` runtime dependencies.
3. Frozen v4 diagnostic results remain matched within report precision.
4. No paper/live readiness claim; no automatic strategy promotion.
