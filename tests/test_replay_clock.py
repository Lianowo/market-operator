"""Regression contracts for real close times and portfolio-wide phase barriers."""

from types import SimpleNamespace

import pandas as pd
import pytest

from market_operator import campaign
from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind, RestingLimitOrder
from market_operator.replay import prepare_cognition
from market_operator.structure import Params

HOUR = 3_600_000


def frame(symbol, closes=(150, 160, 170, 180), trigger_seconds=1800):
    columns = ["ts", "open", "high", "low", "close", "volume"]
    hourly = pd.DataFrame([
        (i * HOUR, 100, max(100, c) + 1, 90, c, 1)
        for i, c in enumerate(closes)
    ], columns=columns)
    coarse = pd.DataFrame([
        (i * HOUR, 100, max(closes[i:i + 2]) + 1, 90, closes[i + 1], 2)
        for i in range(0, len(closes), 2)
    ], columns=columns)
    cog = prepare_cognition(coarse, Params(), symbol)
    step = trigger_seconds * 1000
    mic = SimpleNamespace(
        bars=[(t, 100, 102, 99, 101, 1) for t in range(0, len(closes) * HOUR, step)],
        cog_index_of=[t // (2 * HOUR) for t in range(0, len(closes) * HOUR, step)],
        trigger_tf_seconds=trigger_seconds,
    )
    execution_seconds = min(trigger_seconds, 3600)
    if execution_seconds == 1800:
        # Native synthetic half-hours: the hourly close is not present early.
        hourly = pd.DataFrame([
            (i * HOUR + j * HOUR // 2, 100, max(100, c if j else 101) + 1,
             90, c if j else 101, .5)
            for i, c in enumerate(closes) for j in range(2)
        ], columns=columns)
    return campaign.build_campaign_frame(cog, hourly, "2h", mic_cog=mic,
                                         execution_seconds=execution_seconds)


def run(frames):
    specs = {f.symbol: ContractSpec(f.symbol, .01, .01, 2, 2, 1) for f in frames.values()}
    return campaign.run_campaigns(
        frames, segment="full", dev_frac=.7, equity0=500, risk_frac=.0075,
        leverage_cap=5, taker_fee=0, slippage_per_side=0, funding_per_8h=0,
        contracts=specs, params=Params(), enable_mic=True, cost_multiplier=1, n_fill=12,
    )


def test_half_hour_cannot_see_hourly_future_or_early_cognition(monkeypatch):
    seen = []
    cognition = []

    def inspect(st, broker, index, ts, *args, **kwargs):
        seen.append((index, dict(broker.last_mark)))

    monkeypatch.setattr(campaign, "_mic_trigger_step", inspect)
    monkeypatch.setattr(campaign, "_cognition_step",
                        lambda st, broker, i, *a, **kw: cognition.append((i, dict(broker.last_mark))))
    f = frame("X")
    result = run({f.key: f})
    assert seen[0] == (0, {"X@2h": 101})  # 00:30: half-hour close, not hourly future
    assert seen[1] == (1, {"X@2h": 150})  # 01:00 close, before next open
    assert cognition[0] == (0, {"X@2h": 160})  # 2h cognition really waits for 02:00
    assert result.equity_rows[-1][0] == 4 * HOUR  # final close is not dropped


@pytest.mark.parametrize("reverse", [False, True])
def test_all_markets_close_before_any_decision(monkeypatch, reverse):
    a, z = frame("A"), frame("Z", (250, 260, 270, 280))
    seen = []
    monkeypatch.setattr(campaign, "_cognition_step", lambda *a, **kw: None)

    def inspect(st, broker, index, ts, *args, **kwargs):
        if index == 1:
            seen.append((st.frame.key, dict(broker.last_mark)))

    monkeypatch.setattr(campaign, "_mic_trigger_step", inspect)
    ordered = [z, a] if reverse else [a, z]
    run({f.key: f for f in ordered})
    assert [key for key, _ in seen] == ["A@2h", "Z@2h"]
    assert all(marks == {"A@2h": 150, "Z@2h": 250} for _, marks in seen)


@pytest.mark.parametrize("kind", ["market", "limit"])
def test_half_hour_order_waits_for_next_execution_bar(monkeypatch, kind):
    monkeypatch.setattr(campaign, "_cognition_step", lambda *a, **kw: None)

    def submit(st, broker, index, ts, *args, **kwargs):
        if index != 0:
            return
        meta = {"mic": True, "execution_step_ms": HOUR // 2,
                "execution_not_before": HOUR // 2, "direction": 1}
        if kind == "market":
            broker.submit(OrderIntent(OrderKind.OPEN, st.frame.key, None, "clock", 100, 0),
                          0, direction=1, invalid=80, meta=meta)
        else:
            broker.submit_limit(RestingLimitOrder(
                direction=1, limit_price=100, armed_at_ts=0, expiry_ts=10 * HOUR,
                fill_after_ts=-HOUR, symbol=st.frame.key, invalid=80, meta=meta))

    monkeypatch.setattr(campaign, "_mic_trigger_step", submit)
    f = frame("X")
    result = run({f.key: f})
    fills = [r for r in result.ledger if r["kind"] in ("OPEN", "LIMIT_FILL")]
    assert len(fills) == 1
    assert fills[0]["ts"] == HOUR // 2


def test_future_hour_mutation_does_not_change_half_hour_state(monkeypatch):
    monkeypatch.setattr(campaign, "_cognition_step", lambda *a, **kw: None)
    observations = []

    def inspect(st, broker, index, ts, *args, **kwargs):
        if index == 0:
            observations.append((dict(broker.last_mark), broker.equity_mtm(), broker.cash))

    monkeypatch.setattr(campaign, "_mic_trigger_step", inspect)
    for closes in [(150, 160, 170, 180), (900, 800, 700, 600)]:
        f = frame("X", closes)
        run({f.key: f})
    assert observations[0] == observations[1]


def test_opening_quotes_are_batched_before_any_fill(monkeypatch):
    monkeypatch.setattr(campaign, "_cognition_step", lambda *a, **kw: None)
    monkeypatch.setattr(campaign, "_mic_trigger_step", lambda *a, **kw: None)
    original = Broker.on_bar_open
    observed = []

    def inspect(broker, symbol, ts, price, **kwargs):
        if ts == HOUR and not kwargs.get("mic_only"):
            observed.append(dict(broker.last_mark))
        return original(broker, symbol, ts, price, **kwargs)

    monkeypatch.setattr(Broker, "on_bar_open", inspect)
    a, z = frame("A"), frame("Z")
    run({a.key: a, z.key: z})
    assert len(observed) == 2
    assert all(marks == {"A@2h": 100, "Z@2h": 100} for marks in observed)


def test_coarse_trigger_waits_for_its_own_close(monkeypatch):
    monkeypatch.setattr(campaign, "_cognition_step", lambda *a, **kw: None)
    observed = []

    def inspect(st, broker, index, ts, *args, **kwargs):
        observed.append((index, dict(broker.last_mark)))
        assert kwargs["execution_only"] is True

    monkeypatch.setattr(campaign, "_mic_trigger_step", inspect)
    f = frame("X", trigger_seconds=7200)
    run({f.key: f})
    assert observed == [(0, {"X@2h": 160}), (1, {"X@2h": 180})]


def test_partial_final_cognition_never_reveals_full_bucket(monkeypatch):
    calls = []
    monkeypatch.setattr(campaign, "_cognition_step", lambda *a, **kw: calls.append(a[2]))
    monkeypatch.setattr(campaign, "_mic_trigger_step", lambda *a, **kw: None)
    f = frame("X", trigger_seconds=3600)
    # Keep only the first hour of the second 2h bucket. Its cached full
    # cognition cannot be published at that incomplete interval's end.
    f.exec_bars.pop(3 * HOUR)
    f.exec_labels = f.exec_labels[:-1]
    f.close_edge = {HOUR: [0], 2 * HOUR: [1]}
    run({f.key: f})
    assert calls == [0]


def test_coarse_execution_rejects_faster_trigger():
    f = frame("X", trigger_seconds=3600)
    f.mic.trigger_tf_seconds = 1800
    with pytest.raises(ValueError, match="execution step must divide trigger"):
        run({f.key: f})