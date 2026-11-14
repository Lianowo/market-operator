"""Contracts protecting assembly reuse, causal lookup and active watch accounting."""

from collections import Counter
from dataclasses import fields
from types import SimpleNamespace

import pandas as pd
import pytest

from market_operator import campaign, scenario
from market_operator.broker import RestingLimitOrder
from market_operator.position import Position, PositionManager
from market_operator.routing import RouterParams
from market_operator.structure import Params


def test_market_data_reuses_execution_and_trigger_frames(monkeypatch):
    loads = Counter()

    def trigger(symbol, seconds):
        loads[(symbol, seconds)] += 1
        return pd.DataFrame({"ts": [seconds]})

    def aggregate(symbol, timeframe):
        loads[(symbol, timeframe)] += 1
        return pd.DataFrame({"ts": [timeframe]})

    monkeypatch.setattr(scenario, "_trigger_frame_for", trigger)
    monkeypatch.setattr(scenario.aggregate, "load_tf", aggregate)
    monkeypatch.setattr(scenario, "prepare_cognition", lambda frame, *a, **kw: frame)
    market = scenario.load_market_data(("BTCUSDT",), Params(), progress=False)
    execution = market.trigger_frame["BTCUSDT"][3600]
    assert all(frame is execution for frame in market.execution_frame.values())
    assert market.cognition_frame[("BTCUSDT", "4h")] is market.trigger_frame["BTCUSDT"][14400]
    assert all(count == 1 for count in loads.values())
    assert ("BTCUSDT", "4h") not in loads


@pytest.mark.parametrize("index", [0, 1, 2048, 4095])
def test_cognition_lookup_is_logarithmic_and_preserves_bucket(index):
    class CountingBars(list):
        reads = 0

        def __getitem__(self, item):
            self.reads += 1
            return super().__getitem__(item)

    step = 4 * campaign.MS_1H
    bars = CountingBars((i * step,) for i in range(4096))
    frame = SimpleNamespace(tf_hours=4, key="BTCUSDT@4h", cog=SimpleNamespace(bars=bars))
    assert campaign._cog_index_of(frame, index * step + step - 1) == index
    assert bars.reads <= 15


def test_cognition_lookup_rejects_missing_bucket():
    frame = SimpleNamespace(tf_hours=4, key="BTCUSDT@4h", cog=SimpleNamespace(bars=[]))
    with pytest.raises(AssertionError, match="outside cognition"):
        campaign._cog_index_of(frame, 0)


def test_watch_trigger_keeps_existing_counter_contract():
    state = SimpleNamespace(frame=SimpleNamespace(symbol="BTCUSDT"))
    counters = {}
    campaign._apply_resting_rows(
        state, None, "BTCUSDT@4h", 1000,
        [{"kind": "TRIGGER", "reason_code": "VIRTUAL_TRIGGER"}], counters)
    assert counters == {"virtual_entry_triggers": 1}


def test_retired_experiments_have_no_configuration_or_position_state():
    assert not ({"min_bar_impulse_atr", "zone_cooldown_bars"}
                & {field.name for field in fields(RouterParams)})
    assert not ({"stop_entry", "fill_model", "ladder_fade", "harvest_family",
                 "harvest_ladder3", "early_exit", "trail_arm_gate", "harvest_fade_2leg"}
                & {field.name for field in fields(campaign.ExecutionConfig)})
    assert not ({"fill_model", "is_stop", "ladder_group"}
                & {field.name for field in fields(RestingLimitOrder)})
    assert not ({"harvest", "r4_stage", "consec_adverse", "trail_armed", "s2_banked"}
                & {field.name for field in fields(Position)})
    with pytest.raises(TypeError):
        PositionManager(harvest_family=("LEVEL_REJECTED",))


def test_scale_promotion_allows_higher_tf_and_closes_lower_tf():
    from types import SimpleNamespace
    from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind

    specs = {k: ContractSpec(k, 0.001, 0.001, 3, 2, 5) for k in ("BTCUSDT@1h", "BTCUSDT@4h", "BTCUSDT@1d")}
    b = Broker(1000.0, risk_frac=0.02, leverage_cap=20.0, taker_fee=0.0006, slippage_per_side=0.0002, funding_per_8h=0.0, contracts=specs)
    # Open 1h position first
    b.submit(OrderIntent(OrderKind.OPEN, "BTCUSDT@1h", None, "init_1h", 100.0, 0), 0, direction=1, invalid=95.0)
    b.on_bar_open("BTCUSDT@1h", 3_600_000, 100.0)
    assert "BTCUSDT@1h" in b.positions

    # Same 1h is blocked, higher 4h is NOT blocked (eligible for promotion!)
    assert campaign._symbol_position_open(b, "BTCUSDT@1h") is True
    assert campaign._symbol_position_open(b, "BTCUSDT@4h") is False

    # Submitting 4h decision queues SCALE_PROMOTED exit on 1h and opens 4h
    hyp = SimpleNamespace(direction=1, invalid=90.0, target_1=130.0, event_id="LEVEL_REJECTED", regime=SimpleNamespace(value="range"), r_mult=3.0, ts=3_600_000)
    dec = SimpleNamespace(kind="market", intent=OrderIntent(OrderKind.OPEN, "BTCUSDT", None, "4h_open", 102.0, 3_600_000), hyp=hyp, route="level_rejected_market", meta={})
    counters = {"portfolio_cap_skips": 0, "open_intents": 0}
    campaign._submit_decisions(SimpleNamespace(frame=SimpleNamespace(key="BTCUSDT@4h", tf_hours=4)), b, [dec], 3_600_000, 3_600_000, 3_600_000, {}, counters, [], 12, 7_200_000)
    assert counters.get("scale_promotions") == 1
    assert any(po.intent.kind is OrderKind.EXIT and "SCALE_PROMOTED" in po.intent.reason for po in b.pending.get("BTCUSDT@1h", ()))
