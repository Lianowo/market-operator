"""Risk budget is shared across BTC/ETH/SOL, not per-symbol.

Opt-in to preserve frozen v4 research results. This caps theoretical stop
loss from current marked equity, not guaranteed fills through a gap.
"""
import pytest
from market_operator.broker import Broker, ContractSpec
from market_operator.broker import OrderKind, OrderIntent

T0 = 1_700_000_000_000
H = 3_600_000


def broker():
    spec = {s: ContractSpec(symbol=s, size_min=0.01, size_step=0.01,
            size_place=2, price_place=2, min_trade_usdt=5.0)
            for s in ("BTC", "ETH")}
    return Broker(equity=300.0, risk_frac=0.0075, portfolio_stop_risk_frac=0.01,
                  leverage_cap=5.0, taker_fee=0.0006,
                  slippage_per_side=0.0002, funding_per_8h=0.0001,
                  contracts=spec)


def place(b: Broker, symbol: str, ts: int, invalid: float = 99.0):
    it = OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                     reason="E7|valid", ref_price=100.0, ts=ts)
    b.submit(it, ts, direction=1, invalid=invalid,
             event_id="E7", target_1=110.0)
    return b.on_bar_open(symbol, ts + H, 100.0)[0]


def test_combined_prospective_stops_are_bounded():
    b = broker()
    a = place(b, "BTC", T0)
    c = place(b, "ETH", T0)
    assert a["kind"] == "OPEN" and c["kind"] == "OPEN"
    assert c["qty"] < a["qty"]  # second instrument sees already-used risk
    stop_risk = sum(pos.qty * max(0, pos.direction * (
        b.last_mark[sym] - pos.invalid)) for sym, pos in b.positions.items())
    assert stop_risk <= b.equity_mtm() * .01 + 1e-8


def test_stop_tightening_updates_budget_but_loosen_is_rejected():
    b = broker()
    place(b, "BTC", T0)
    prev = b._available_stop_risk(b.equity_mtm())
    b.update_protective_invalid("BTC", 99.8)
    assert b._available_stop_risk(b.equity_mtm()) > prev
    with pytest.raises(ValueError, match="loosened"):
        b.update_protective_invalid("BTC", 99.7)


def test_add_also_consumes_total_stop_risk():
    b = broker()
    place(b, "BTC", T0)
    it = OrderIntent(kind=OrderKind.ADD, symbol="BTC", qty_frac=1.0,
                     reason="§8.4|add", ref_price=100.0, ts=T0 + H)
    original = b.position_of("BTC").qty
    b.submit(it, T0 + H)
    row = b.on_bar_open("BTC", T0 + 2*H, 100.0)[0]
    assert row["kind"] in ("ADD", "SKIP")
    if row["kind"] == "ADD":
        assert row["qty"] < original
    assert b._available_stop_risk(b.equity_mtm()) >= -1e-8


def test_legacy_frozen_backtest_default_is_unchanged():
    b = Broker(equity=300, risk_frac=.0075, leverage_cap=5,
               taker_fee=.0006, slippage_per_side=.0002,
               funding_per_8h=.0001, contracts={
                   "BTC": ContractSpec(symbol="BTC", size_min=.01,
                     size_step=.01, size_place=2, price_place=2, min_trade_usdt=5)})
    assert b.portfolio_stop_risk_frac is None
    assert b._available_stop_risk(300) == float("inf")
