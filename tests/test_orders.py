"""End-to-end risk-critical order-tracker tests (no exchange/network/deps)."""
from decimal import Decimal

import pytest

from market_operator.orders import OrderLedger, ReconciliationError


def test_order_intent_is_idempotent_and_conflicting_ids_fail(tmp_path):
    with OrderLedger(tmp_path / "exec.sqlite") as l:
        a = l.submit("hyp7:BTC:entry", "BTCUSDT", "BUY", "0.002", "entry")
        assert a.state == "NEW" and a.qty == Decimal("0.002")
        assert l.submit("hyp7:BTC:entry", "BTCUSDT", "BUY", "0.002", "entry") == a
        with pytest.raises(ValueError, match="collision"):
            l.submit("hyp7:BTC:entry", "BTCUSDT", "BUY", "0.004", "entry")
        assert l.order("hyp7:BTC:entry").qty == Decimal("0.002")


def test_partial_fills_idempotent_vwap_and_cancel_race(tmp_path):
    with OrderLedger(tmp_path / "fills.db") as l:
        l.verify_exchange_positions({})
        l.submit("in-1", "BTCUSDT", "BUY", "0.1", "entry")
        l.mark_submitted("in-1")
        assert not l.dispatch_allowed()
        assert l.record_fill("in-1", "trade-01", "0.04", "100", "0.004")
        assert not l.record_fill("in-1", "trade-01", "0.04", "100", "0.004")
        with pytest.raises(ReconciliationError, match="collision"):
            l.record_fill("in-1", "trade-01", "0.05", "100", "0.005")
        assert l.order("in-1").state == "PARTIAL"
        l.cancel_requested("in-1")
        assert l.order("in-1").state == "CANCEL_PENDING"
        l.reconcile_order("in-1", "CANCELED", "venue-order-1")
        # Late execution after cancel acknowledgement is still part of position.
        assert l.record_fill("in-1", "trade-02", "0.03", "101", "0.003")
        o = l.order("in-1")
        assert o.state == "CANCELED" and o.filled_qty == Decimal("0.07")
        assert l.net_positions() == {"BTCUSDT": Decimal("0.07")}
        q, vwap, fee = l.execution_totals("in-1")
        assert q == Decimal("0.07")
        assert vwap == (Decimal("4") + Decimal("3.03")) / Decimal("0.07")
        assert fee == Decimal("0.007")
        with pytest.raises(ReconciliationError, match="position drift"):
            l.verify_exchange_positions({"BTCUSDT": "0.06"})
        l.verify_exchange_positions({"BTCUSDT": "0.07"})
        assert l.dispatch_allowed()


def test_recovery_requires_order_and_position_reconciliation(tmp_path):
    path = tmp_path / "journal.db"
    with OrderLedger(path) as l:
        l.verify_exchange_positions({})
        l.submit("x", "SOLUSDT", "SELL", "1.2", "entry")
        l.mark_submitted("x")
        l.reconcile_order("x", "OPEN", "exchange-1")
        assert l.order("x").verified
        l.verify_exchange_positions({})
        assert l.dispatch_allowed()
    with OrderLedger(path) as l:
        assert not l.dispatch_allowed()
        assert l.unverified() == ("x",)
        with pytest.raises(ReconciliationError, match="reconciled"):
            l.mark_submitted("x")
        l.reconcile_order("x", "OPEN", "exchange-1")
        assert not l.dispatch_allowed()  # current venue POSITION verification still required
        l.verify_exchange_positions({})
        assert l.dispatch_allowed()


def test_unknown_filled_status_cannot_invent_executions(tmp_path):
    with OrderLedger(tmp_path / "unknown.db") as l:
        l.submit("s", "ETHUSDT", "SELL", "0.3", "entry")
        report = l.reconcile_order("s", "FILLED", "e1")
        assert report.state == "UNKNOWN" and not report.verified
        assert l.net_positions() == {}
        assert not l.dispatch_allowed()
        l.record_fill("s", "real-fill-1", "0.3", "4000", "0.72")
        assert l.order("s").state == "FILLED"
        l.reconcile_order("s", "FILLED", "e1")
        l.verify_exchange_positions({"ETHUSDT": "-0.3"})
        assert l.dispatch_allowed()


def test_rejected_order_does_not_create_position(tmp_path):
    with OrderLedger(tmp_path / "rejected.db") as l:
        l.submit("r", "BTCUSDT", "BUY", "0.01", "entry")
        l.reconcile_order("r", "REJECTED")
        assert l.order("r").state == "REJECTED"
        assert l.net_positions() == {}
        with pytest.raises(ReconciliationError, match="rejected"):
            l.record_fill("r", "late", "0.01", "80000")
        l.verify_exchange_positions({})
        assert l.dispatch_allowed()


def test_overfills_and_invalid_numbers_fail_closed_without_partial_db_write(tmp_path):
    with OrderLedger(tmp_path / "safety.db") as l:
        l.submit("id", "ETHUSDT", "BUY", "0.5", "entry")
        with pytest.raises(ValueError):
            l.record_fill("id", "nan", "NaN", "3000")
        with pytest.raises(ReconciliationError, match="exceed"):
            l.record_fill("id", "excess", "0.6", "3000")
        assert l.order("id").filled_qty == Decimal(0)
        assert l.net_positions() == {}


def test_entry_and_exit_are_reconstructed_only_from_unique_fills(tmp_path):
    with OrderLedger(tmp_path / "life.db") as l:
        l.submit("t:open", "BTCUSDT", "BUY", "0.06", "entry")
        l.record_fill("t:open", "open-a", "0.04", "100", "0.004")
        l.record_fill("t:open", "open-b", "0.02", "100", "0.002")
        l.submit("t:reduce", "BTCUSDT", "SELL", "0.03", "reduce")
        assert l.net_positions() == {"BTCUSDT": Decimal("0.06")}
        l.record_fill("t:reduce", "reduce-a", "0.03", "110", "0.0033")
        assert l.net_positions() == {"BTCUSDT": Decimal("0.03")}
        l.submit("t:exit", "BTCUSDT", "SELL", "0.03", "exit")
        l.record_fill("t:exit", "exit-a", "0.03", "90", "0.0027")
        assert l.net_positions() == {}
