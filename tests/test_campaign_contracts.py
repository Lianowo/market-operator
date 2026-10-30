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
    assert market.cognition_frame[("BTCUSDT", "2h")] is market.trigger_frame["BTCUSDT"][7200]
    assert market.cognition_frame[("BTCUSDT", "4h")] is market.trigger_frame["BTCUSDT"][14400]
    assert all(count == 1 for count in loads.values())
    assert ("BTCUSDT", "2h") not in loads
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