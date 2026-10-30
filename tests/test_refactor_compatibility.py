"""Compatibility contracts for routing options and reconstructed trade output."""

import pytest

from market_operator.events import BREAKOUT_ACCEPTED, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.replay import reconstruct_trades
from market_operator.routing import MarketRouter, RouterParams
from market_operator.structure import Regime


def test_cost_gate_replaces_atr_floor_when_configured():
    router = MarketRouter(RouterParams(max_cost_frac=0.5, cost_per_side=0.0008))
    event = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=1000, known_index=1, attrs={}
    )
    view = StructureView(regime=Regime.RANGE, zones=(), atr=1.0)
    decisions = router.on_bar(
        "BTCUSDT", (1000, 1000.0, 1001.0, 999.0, 1000.0, 1.0),
        1, view, 1.0, [event], position_open=False, entry_resting=False,
    )
    assert decisions == []
    assert router.counters["f1_cost_reject_breakout_accepted_market"] == 1


def test_reconstructed_trade_preserves_accounting_and_provenance():
    provenance = {
        "route": "structure_broken_weak_limit", "l_star": 99.0,
        "weak_bars": 3, "e6_margin": 0.4, "e6_impulse": 1.2,
    }
    opening = {
        "trade_id": 1, "kind": "LIMIT_FILL", "symbol": "BTCUSDT",
        "side": "buy", "meta": {**provenance, "risk_usdt": 10.0},
        "decision_ts": 0, "ts": 1000, "price": 100.0, "qty": 2.0,
        "used_leverage": 2.0, "realized_pnl": 0.0, "fee": 0.1,
        "funding_delta": 0.0, "slippage_cost": 0.0, "reason": "entry",
    }
    closing = {
        **opening, "kind": "EXIT", "meta": {}, "decision_ts": 2000,
        "ts": 3000, "price": 110.0, "realized_pnl": 20.0,
        "fee": 0.2, "funding_delta": -0.3, "reason": "exit",
    }
    trades = reconstruct_trades(
        [opening, closing], {"BTCUSDT": {"ts_lo": 0, "ts_hi": 5000}}
    )
    assert len(trades) == 1
    trade = trades[0]
    assert {key: trade[key] for key in provenance} == provenance
    assert trade["closed"] is True
    assert trade["entry_mode"] == "limit"
    assert trade["exit_price"] == 110.0
    assert trade["fees_maker"] == pytest.approx(0.1)
    assert trade["fees_taker"] == pytest.approx(0.2)
    assert trade["funding"] == pytest.approx(0.3)
    assert trade["net_pnl"] == pytest.approx(19.4)
    assert trade["r_multiple"] == pytest.approx(1.94)