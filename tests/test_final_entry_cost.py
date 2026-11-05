"""成本约束必须应用于最终入场几何,而不只是确认收盘价。"""

import pytest

from market_operator.events import (
    BREAKOUT_ACCEPTED, FLIP_RETESTED, LEVEL_REJECTED, STRUCTURE_BROKEN, MarketEvent,
)
from market_operator.hypothesis import StructureView
from market_operator.routing import MarketRouter, RouterParams, WHITELIST_BALANCE
from market_operator.structure import Regime, Zone, ZoneRole


def _market_accept(direction, max_cost_frac, **overrides):
    zone = (Zone(99, 100, ZoneRole.RESISTANCE) if direction > 0
            else Zone(100, 101, ZoneRole.SUPPORT))
    close, invalid = (102, 99) if direction > 0 else (98, 101)
    event = MarketEvent(BREAKOUT_ACCEPTED, direction, 1000, 1,
                        {"zone": zone, "ref": 100, "invalid": invalid})
    params = dict(accept_use_struct_stop=True,
                  max_cost_frac=max_cost_frac, cost_per_side=.0008)
    params.update(overrides)
    router = MarketRouter(RouterParams(**params))
    decisions = router.on_bar("X", (1000, close, close + 1, close - 1, close, 1),
                              1, StructureView(Regime.RANGE, (zone,), 1), 1,
                              [event], position_open=False, entry_resting=False)
    return router, decisions


@pytest.mark.parametrize("direction", [-1, 1])
def test_market_cost_rejects_when_exceeding_max_cost_frac(direction):
    router, decisions = _market_accept(direction, .04, zone_departure_reset=True)
    assert decisions == []
    assert router.counters["f1_cost_reject_breakout_accepted_market"] == 1
    assert router._zone_lock == {}  # a rejected plan must not consume the zone


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("threshold", [None, 0, -1, .16, .17])
def test_disabled_cost_gate_and_inclusive_boundary_preserve_market(direction, threshold):
    router, decisions = _market_accept(direction, threshold)
    decision, = decisions
    assert decision.kind == "market"
    assert not any(key.startswith("f1_cost_reject") for key in router.counters)


@pytest.mark.parametrize("direction", [-1, 1])
def test_tight_market_keeps_existing_confirmation_cost_gate(direction):
    router, decisions = _market_accept(direction, .1)
    decision, = decisions
    assert decision.kind == "market"
    assert not any(key.startswith("f1_cost_reject") for key in router.counters)


@pytest.mark.parametrize("direction", [-1, 1])
def test_rejection_market_checks_final_cost(direction):
    zone = (Zone(99, 100, ZoneRole.SUPPORT, box_opposite=106) if direction > 0
            else Zone(100, 101, ZoneRole.RESISTANCE, box_opposite=94))
    close, invalid = (102, 99) if direction > 0 else (98, 101)
    event = MarketEvent(LEVEL_REJECTED, direction, 1000, 1,
                        {"zone": zone, "ref": 100, "invalid": invalid})
    router = MarketRouter(RouterParams(
        whitelist=WHITELIST_BALANCE, reject_use_struct_stop=True,
        reject_target_mode="opposite", max_cost_frac=.04,
    ))
    decisions = router.on_bar("X", (1000, close, close + 1, close - 1, close, 1),
                              1, StructureView(Regime.RANGE, (zone,), 1), 1,
                              [event], position_open=False, entry_resting=False)
    assert decisions == []
    assert router.counters["f1_cost_reject_level_rejected_market"] == 1


@pytest.mark.parametrize("direction", [-1, 1])
def test_weak_structure_flip_limit_checks_final_cost(direction):
    zone = (Zone(100.5, 101, ZoneRole.SUPPORT) if direction > 0
            else Zone(99, 99.5, ZoneRole.RESISTANCE))
    close = 100 + direction * 2
    view = StructureView(Regime.RANGE, (zone,), 1)
    router = MarketRouter(RouterParams(max_cost_frac=.1, zone_departure_reset=True))
    weak = MarketEvent(STRUCTURE_BROKEN, direction, 0, 0, {"ref": 100})
    assert router.on_bar("X", (0, 100, 100.3, 99.7, 100, 1), 0, view, 1,
                         [weak], position_open=False, entry_resting=False) == []
    flip = MarketEvent(FLIP_RETESTED, direction, 1000, 1, {"zone": zone, "ref": 100})
    decisions = router.on_bar("X", (1000, close, close + 1, close - 1, close, 1),
                              1, view, 1, [flip], position_open=False, entry_resting=False)
    assert decisions == []
    assert router.counters["f1_cost_reject_structure_broken_weak_limit"] == 1
    assert router._zone_lock == {}


def test_disabled_cost_gate_preserves_insufficient_remaining_rr_counter():
    zone = Zone(99, 100, ZoneRole.SUPPORT, box_opposite=106)
    event = MarketEvent(LEVEL_REJECTED, 1, 1000, 1,
                        {"zone": zone, "ref": 100, "invalid": 99})
    router = MarketRouter(RouterParams(
        whitelist=WHITELIST_BALANCE, reject_use_struct_stop=True,
        reject_target_mode="opposite", min_box_rr=.1, bounded_min_rr=10,
    ))
    assert router.on_bar("X", (1000, 102, 103, 101, 102, 1), 1,
                         StructureView(Regime.RANGE, (zone,), 1), 1, [event],
                         position_open=False, entry_resting=False) == []
    assert router.counters["insufficient_remaining_rr"] == 1