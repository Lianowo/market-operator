"""Fine execution uses actual decision closes and preserves default contracts."""
from collections import Counter
from types import SimpleNamespace

import pandas as pd
import pytest

from market_operator import campaign
from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind, RestingLimitOrder
from market_operator.replay import prepare_cognition
from market_operator.structure import Params

STEP = 900_000


def broker():
    spec = ContractSpec(symbol="X", size_min=.01, size_step=.01, size_place=2,
                        price_place=2, min_trade_usdt=1)
    return Broker(300, risk_frac=.0075, leverage_cap=5, contracts={"X": spec},
                  taker_fee=.0006, slippage_per_side=.0002, funding_per_8h=0)


def test_pending_cannot_fill_before_actual_signal_close():
    b = broker()
    intent = OrderIntent(OrderKind.OPEN, "X", None, "ACCEPT|test", 100, 0)
    b.submit(intent, decision_ts=0, direction=1, invalid=95,
             meta={"execution_not_before": 8 * STEP})
    assert b.on_bar_open("X", STEP, 100) == []
    assert b.on_bar_open("X", 7 * STEP, 100) == []
    assert b.on_bar_open("X", 8 * STEP, 100)[0]["kind"] == "OPEN"


def test_limit_can_fill_next_quarter_without_hour_rounding():
    b = broker()
    b.submit_limit(RestingLimitOrder(direction=1, limit_price=100,
                                    armed_at_ts=0, expiry_ts=10 * STEP,
                                    fill_after_ts=STEP, symbol="X", invalid=95,
                                    meta={"execution_step_ms": STEP}))
    assert b.on_15m_bar("X", STEP, 101, 102, 99, 100) == []
    assert b.on_15m_bar("X", 2 * STEP, 101, 102, 99, 100)[0]["kind"] == "LIMIT_FILL"


def test_watch_wait_remains_hours_not_quarter_bar_count():
    b = broker()
    b.submit_limit(RestingLimitOrder(direction=1, limit_price=90,
                                    armed_at_ts=0, expiry_ts=4 * 3_600_000,
                                    fill_after_ts=0, symbol="X", invalid=85,
                                    meta={"execution_step_ms": STEP, "watch_entry": True,
                                          "max_wait_hours": 4}))
    for k in range(1, 17):
        assert not b.on_15m_bar("X", k * STEP, 100, 101, 99, 100)
    assert b.on_15m_bar("X", 17 * STEP, 100, 101, 99, 100)[0]["kind"] == "EXPIRED"


def test_fine_frame_is_explicit_and_has_correct_close_edges():
    cols = ["ts", "open", "high", "low", "close", "volume"]
    coarse = pd.DataFrame([(0, 100, 102, 98, 101, 1),
                           (8 * STEP, 101, 103, 99, 102, 1)], columns=cols)
    fine = pd.DataFrame([(k * STEP, 100, 102, 98, 101, 1) for k in range(16)], columns=cols)
    cog = prepare_cognition(coarse, Params(), "X")
    with pytest.raises(AssertionError, match="exec grid gap"):
        campaign.build_campaign_frame(cog, fine, "2h")
    frame = campaign.build_campaign_frame(cog, fine, "2h", execution_seconds=900)
    assert frame.ms_exec == STEP
    assert frame.close_edge == {7 * STEP: [0], 15 * STEP: [1]}


def test_mic_decision_uses_close_but_never_replays_coarse_ohlc_as_execution(monkeypatch):
    submitted = []
    monkeypatch.setattr(campaign, "_submit_decisions", lambda *a, **kw: submitted.append((a, kw)))
    event = SimpleNamespace(id="LEVEL_REJECTED", direction=1, attrs={})
    routed = []
    router = SimpleNamespace(on_bar=lambda *a, **kw: routed.append(a) or [])
    mic = SimpleNamespace(bars=[(0, 100, 101, 98, 99, 1)], cog_index_of=[0],
                          events_by={0: [event]}, views={0: None}, atrs=[2],
                          trigger_tf_seconds=7200)
    st = SimpleNamespace(frame=SimpleNamespace(key="X@2h", symbol="X", mic=mic, ms_exec=STEP), router=router)
    b = SimpleNamespace(position_of=lambda key: None, has_entry_limit=lambda key: False)
    campaign._mic_trigger_step(st, b, 0, 7 * STEP, Counter(), [], {"lo": 0, "hi": 1},
                               frozenset({("LEVEL_REJECTED", 1)}), 12, execution_only=True)
    args, kw = submitted[0]
    assert args[5] == 8 * STEP  # expiry base = actual close
    assert args[6]["execution_not_before"] == 8 * STEP
    assert kw["cancel_moment"] == 8 * STEP


def test_fine_mode_default_off_parity():
    # Explicit old step and omitted step must assemble the same default frame.
    cols = ["ts", "open", "high", "low", "close", "volume"]
    cog = prepare_cognition(pd.DataFrame([(0, 100, 102, 98, 101, 1)], columns=cols), Params(), "X")
    hourly = pd.DataFrame([(0, 100, 101, 99, 100, 1), (4 * STEP, 100, 102, 98, 101, 1)], columns=cols)
    a = campaign.build_campaign_frame(cog, hourly, "2h")
    b = campaign.build_campaign_frame(cog, hourly, "2h", execution_seconds=3600)
    assert a == b


def test_preserved_event_routes_original_coarse_bar(monkeypatch):
    monkeypatch.setattr(campaign, "_submit_decisions", lambda *a, **kw: None)
    original = (0, 100, 105, 98, 104, 8)
    event = SimpleNamespace(id="BREAKOUT_ACCEPTED", direction=1,
                            attrs={"routing_bar": original, "routing_index": 12})
    calls = []
    mic = SimpleNamespace(bars=[(7 * STEP, 103, 104, 102, 104, 1)], cog_index_of=[0],
                          events_by={0: [event]}, views={0: None}, atrs=[2], trigger_tf_seconds=900)
    st = SimpleNamespace(frame=SimpleNamespace(key="X@4h", symbol="X", mic=mic, ms_exec=STEP),
                         router=SimpleNamespace(on_bar=lambda *a, **kw: calls.append(a) or []))
    b = SimpleNamespace(position_of=lambda key: None, has_entry_limit=lambda key: False)
    campaign._mic_trigger_step(st, b, 0, 7 * STEP, Counter(), [], {"lo": 0, "hi": 1},
                               frozenset(), 12, execution_only=True)
    assert calls[0][1] == original
    assert calls[0][2] == 12