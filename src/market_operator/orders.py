"""Durable fill-first order tracker. Only Python standard library.

Independent, minimal rewrite of the order lifecycle ideas reviewed in
Hummingbot's Apache-2.0 ClientOrderTracker/InFlightOrder. No Hummingbot code
is copied and no trading framework is imported.

Safety invariant: an order request and even an exchange status "FILLED" are
NOT proof of position change; only unique exchange executions change quantity.
A restarted process cannot dispatch new risk before reconciling orders and
account positions with the venue. No exchange/network calls occur here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
from typing import Mapping


class ReconciliationError(RuntimeError):
    pass


def _d(value: str | int | float | Decimal, *, positive: bool = False) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal: {value}") from exc
    if not d.is_finite() or (positive and d <= 0):
        raise ValueError(f"invalid numeric amount: {value}")
    return d


@dataclass(frozen=True)
class Order:
    client_id: str
    symbol: str
    side: str
    qty: Decimal
    role: str
    state: str
    exchange_id: str | None
    verified: bool
    filled_qty: Decimal


ACTIVE = frozenset({"NEW", "SUBMITTED", "PARTIAL", "CANCEL_PENDING", "UNKNOWN"})
TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED"})


class OrderLedger:
    """SQLite-backed order/fill facts; requires one writer per database.

    Submit carries a deterministic client ID; retrying the same client ID does
    not create another order. Any unknown/stale exchange response is resolved
    by polling the venue, never by resubmitting blindly.
    """

    def __init__(self, path: str | Path) -> None:
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
              client_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
              side TEXT NOT NULL, qty TEXT NOT NULL, role TEXT NOT NULL,
              state TEXT NOT NULL, exchange_id TEXT, verified INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS fills (
              client_id TEXT NOT NULL, fill_id TEXT NOT NULL,
              qty TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
              PRIMARY KEY (client_id, fill_id),
              FOREIGN KEY (client_id) REFERENCES orders(client_id)
            );
        """)
        self.db.execute("PRAGMA foreign_keys=ON")
        # A restart loses knowledge of whether nonterminal orders continued to
        # fill, cancel, or change state while the process was offline.
        self.db.execute("UPDATE orders SET verified=0 WHERE state IN "
                        "('NEW','SUBMITTED','PARTIAL','CANCEL_PENDING','UNKNOWN')")
        self._position_verified = False

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> OrderLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _row(self, client_id: str):
        row = self.db.execute(
            "SELECT client_id,symbol,side,qty,role,state,exchange_id,verified "
            "FROM orders WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            raise KeyError(client_id)
        return row

    def _filled(self, client_id: str) -> Decimal:
        return sum((Decimal(row[0]) for row in self.db.execute(
            "SELECT qty FROM fills WHERE client_id=?", (client_id,))), Decimal(0))

    def order(self, client_id: str) -> Order:
        r = self._row(client_id)
        return Order(r[0], r[1], r[2], Decimal(r[3]), r[4], r[5],
                     r[6], bool(r[7]), self._filled(client_id))

    def submit(self, client_id: str, symbol: str, side: str,
               qty: str | Decimal, role: str) -> Order:
        """Persist intent before any network send; requires stable client ID."""
        if not client_id or not symbol or not role or side not in ("BUY", "SELL"):
            raise ValueError("invalid order identifiers, side or role")
        amount = _d(qty, positive=True)
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO orders "
                "(client_id,symbol,side,qty,role,state) VALUES (?,?,?,?,?,?)",
                (client_id, symbol, side, str(amount), role, "NEW"))
            found = self.order(client_id)
            if (found.symbol, found.side, found.qty, found.role) != (
                    symbol, side, amount, role):
                raise ValueError(f"client ID collision with different order: {client_id}")
        self._position_verified = False
        return found

    def dispatch_allowed(self) -> bool:
        """No unknown orders and account exposure reconciled after restart."""
        if not self._position_verified:
            return False
        return not self.unverified()

    def mark_submitted(self, client_id: str) -> None:
        """Called after network dispatch, not before; no automatic retries."""
        if not self.dispatch_allowed():
            raise ReconciliationError("order or account has not been reconciled")
        order = self.order(client_id)
        if order.state != "NEW":
            raise ReconciliationError("already sent/unknown order: query exchange")
        with self.db:
            self.db.execute(
                "UPDATE orders SET state='SUBMITTED', verified=0 WHERE client_id=?",
                (client_id,))

    def unverified(self) -> tuple[str, ...]:
        return tuple(row[0] for row in self.db.execute(
            "SELECT client_id FROM orders WHERE verified=0 AND state IN "
            "('NEW','SUBMITTED','PARTIAL','CANCEL_PENDING','UNKNOWN') "
            "ORDER BY client_id"))

    def cancel_requested(self, client_id: str) -> None:
        order = self.order(client_id)
        if order.state in TERMINAL:
            return
        with self.db:
            self.db.execute(
                "UPDATE orders SET state='CANCEL_PENDING', verified=0 WHERE client_id=?",
                (client_id,))

    def record_fill(self, client_id: str, fill_id: str,
                    qty: str | Decimal, price: str | Decimal,
                    fee: str | Decimal = "0") -> bool:
        """Accept unique exchange executions exactly once, including late fills.

        Returns True only if a new execution changed the order and portfolio.
        Status/cancel acknowledgements alone never manufacture fills.
        """
        if not fill_id:
            raise ValueError("exchange fill_id required")
        q, p, f = _d(qty, positive=True), _d(price, positive=True), _d(fee)
        if f < 0:
            raise ValueError("negative fee unsupported; rebates need explicit convention")
        with self.db:
            order = self.order(client_id)
            prior = self.db.execute(
                "SELECT qty,price,fee FROM fills WHERE client_id=? AND fill_id=?",
                (client_id, fill_id)).fetchone()
            if prior is not None:
                if (Decimal(prior[0]), Decimal(prior[1]), Decimal(prior[2])) != (q, p, f):
                    raise ReconciliationError(f"fill ID collision: {fill_id}")
                return False
            if order.state == "REJECTED":
                raise ReconciliationError("exchange reports fill of rejected order")
            if order.filled_qty + q > order.qty:
                raise ReconciliationError("fill would exceed submitted order quantity")
            self.db.execute(
                "INSERT INTO fills(client_id,fill_id,qty,price,fee) "
                "VALUES (?,?,?,?,?)", (client_id, fill_id, str(q), str(p), str(f)))
            if order.filled_qty + q == order.qty:
                state = "FILLED"
            elif order.state == "CANCELED":
                state = "CANCELED"  # late partial fill, residual is still canceled
            else:
                state = "PARTIAL"
            self.db.execute(
                "UPDATE orders SET state=?,verified=0 WHERE client_id=?",
                (state, client_id))
        self._position_verified = False
        return True

    def reconcile_order(self, client_id: str, exchange_state: str,
                        exchange_id: str | None = None) -> Order:
        """Apply venue truth after fetching order status AND its execution list.

        A venue-reported FILLED with missing fill details is UNKNOWN, not a
        synthetic fill. Caller must fetch missing executions and retry.
        """
        if exchange_state not in {"OPEN", "PARTIAL", "FILLED", "CANCELED", "REJECTED"}:
            raise ValueError("unsupported exchange order state")
        with self.db:
            order = self.order(client_id)
            if order.exchange_id and exchange_id and order.exchange_id != exchange_id:
                raise ReconciliationError("exchange order ID changed")
            if exchange_state == "FILLED" and order.filled_qty != order.qty:
                state, verified = "UNKNOWN", 0
            elif exchange_state == "OPEN":
                state, verified = ("PARTIAL" if order.filled_qty else "SUBMITTED"), 1
            elif exchange_state == "PARTIAL":
                state, verified = ("PARTIAL" if order.filled_qty else "UNKNOWN"), int(bool(order.filled_qty))
            elif exchange_state == "REJECTED" and order.filled_qty:
                state, verified = "UNKNOWN", 0
            else:
                state, verified = exchange_state, 1
            self.db.execute(
                "UPDATE orders SET state=?,exchange_id=COALESCE(?,exchange_id),"
                "verified=? WHERE client_id=?",
                (state, exchange_id, verified, client_id))
        return self.order(client_id)

    def net_positions(self) -> dict[str, Decimal]:
        """Signed base asset quantity from actual executions only."""
        positions: dict[str, Decimal] = {}
        for symbol, side, qty in self.db.execute(
                "SELECT o.symbol,o.side,f.qty FROM fills f "
                "JOIN orders o ON o.client_id=f.client_id"):
            sign = 1 if side == "BUY" else -1
            positions[symbol] = positions.get(symbol, Decimal(0)) + sign * Decimal(qty)
        return {s: q for s, q in positions.items() if q}

    def verify_exchange_positions(
            self, exchange_positions: Mapping[str, str | Decimal]) -> None:
        """Explicit exchange-position check required before more order risk."""
        local = self.net_positions()
        remote = {s: _d(q) for s, q in exchange_positions.items() if _d(q)}
        if local != remote:
            self._position_verified = False
            raise ReconciliationError(f"position drift: executions={local}; venue={remote}")
        self._position_verified = True

    def execution_totals(self, client_id: str) -> tuple[Decimal, Decimal, Decimal]:
        """Filled base qty, weighted average price, paid fee in quote currency."""
        rows = [(Decimal(q), Decimal(p), Decimal(f)) for q,p,f in self.db.execute(
            "SELECT qty,price,fee FROM fills WHERE client_id=?", (client_id,))]
        qty = sum((r[0] for r in rows), Decimal(0))
        gross = sum((r[0] * r[1] for r in rows), Decimal(0))
        fees = sum((r[2] for r in rows), Decimal(0))
        return qty, gross / qty if qty else Decimal(0), fees
