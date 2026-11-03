"""A rejection decided at close must include every already-known trial extreme."""
import pytest

from market_operator.events import EventDetector, LEVEL_REJECTED, _Episode
from market_operator.hypothesis import StructureView
from market_operator.routing import MarketRouter, RouterParams, WHITELIST_BALANCE
from market_operator.structure import Params, Regime, Zone, ZoneRole
from market_operator.trigger import MicTracker, _Context, _MicEpisode


def _mirror_bar(bar):
    ts, o, h, low, c, v = bar
    return ts, 200 - o, 200 - low, 200 - h, 200 - c, v


@pytest.mark.parametrize("layer", ["cognition", "trigger"])
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("confirmation_open", [99.0, 101.0])
def test_reject_uses_complete_trial_extreme_at_decision_close(layer, direction, confirmation_open):
    p = Params(use_balance_detector=True)
    zone = Zone(101, 103, ZoneRole.RESISTANCE, box_opposite=90)
    bar = (3_600_000, confirmation_open, 110, 98, 100.5, 1)
    extreme = 102
    if direction > 0:
        zone = Zone(97, 99, ZoneRole.SUPPORT, box_opposite=110)
        bar = _mirror_bar(bar)
        extreme = 98

    if layer == "cognition":
        detector = EventDetector(p)
        detector.structure._zones.zones = [zone]
        detector.structure._regime = Regime.RANGE
        detector._episodes = [_Episode(zone, 0, extreme, 0)]
        events = detector._zone_events(1, bar[0], *bar[1:5], 100, 1)
    else:
        tracker = MicTracker(p, scale=1, trigger_tf_seconds=3600, obs_tf_seconds=3600)
        tracker._ctx = _Context(1, .1, .3, Regime.RANGE, (zone,), (),
                                None, None, None, None, None)
        tracker._episodes = [_MicEpisode(zone, 0, extreme, 0)]
        tracker._prev_trig_close = 100
        events = tracker.on_trigger_bar(1, bar[0], *bar[1:5])

    rejected = [event for event in events if event.id == LEVEL_REJECTED]
    assert len(rejected) == 1
    event = rejected[0]
    assert event.direction == direction
    assert event.known_index == 1
    assert event.attrs["ex"] == pytest.approx(110 if direction < 0 else 90)
    assert event.attrs["ex_index"] == 1
    assert event.attrs["invalid"] == pytest.approx(110.1 if direction < 0 else 89.9)

    # The same evidence must reach sizing; the stop cannot be inside the trial.
    router = MarketRouter(RouterParams(whitelist=WHITELIST_BALANCE,
                                      reject_use_struct_stop=True,
                                      reject_target_mode="opposite"))
    decision, = router.on_bar("X", bar, 1, StructureView(Regime.RANGE, (zone,), 1),
                              1, [event], position_open=False, entry_resting=False)
    assert decision.hyp.invalid == event.attrs["invalid"]


def test_reject_complete_extreme_through_natural_structure_updates():
    # No injected zones/episodes: let the production structure discover the box.
    from tests.test_v22_pipeline import _make_box_bars

    detector = EventDetector(Params(use_balance_detector=True))
    bars = _make_box_bars(cycles=4)
    for bar in bars:
        detector.update(bar)
    ts = bars[-1][0]
    detector.update((ts + 3_600_000, 115, 130.5, 114, 126, 1))
    events = detector.update((ts + 7_200_000, 126, 140, 125, 126.5, 1))
    event, = [event for event in events if event.id == LEVEL_REJECTED]
    assert event.attrs["ex"] == 140
    assert event.attrs["ex_index"] == len(bars) + 1
    assert event.attrs["invalid"] == pytest.approx(140 + .1 * detector.structure.atr())


@pytest.mark.parametrize("direction", [-1, 1])
def test_trigger_layer_r3_uses_the_same_outside_close_evidence(direction):
    """同一原语在两层必须覆盖同一事实:触发层也要有 R3 失败突破回收。"""
    p = Params(use_balance_detector=True)
    zone = Zone(101, 103, ZoneRole.RESISTANCE, box_opposite=90)
    bar = (3_600_000, 100, 104, 99, 100.5, 1)
    extreme = 104
    if direction > 0:
        zone = Zone(97, 99, ZoneRole.SUPPORT, box_opposite=110)
        bar = _mirror_bar(bar)
        extreme = 96
    tracker = MicTracker(p, scale=1, trigger_tf_seconds=3600, obs_tf_seconds=3600)
    tracker._ctx = _Context(1, .1, .3, Regime.RANGE, (zone,), (),
                            None, None, None, None, None)
    tracker._episodes = [_MicEpisode(zone, 0, extreme, 0,
                                     puncture_index=0, outside_closes=1)]
    events = tracker.on_trigger_bar(1, bar[0], *bar[1:5])
    rejected = [event for event in events if event.id == LEVEL_REJECTED]
    assert len(rejected) == 1
    event = rejected[0]
    assert event.attrs["sub_kind"] == "R3_TRAP"
    assert event.attrs["outside_closes"] == 1
    assert event.attrs["ex"] == pytest.approx(104 if direction < 0 else 96)
    assert event.attrs["invalid"] == pytest.approx(
        (104 + .1) if direction < 0 else (96 - .1))


@pytest.mark.parametrize("direction", [-1, 1])
def test_r3_recovery_keeps_the_confirmation_bars_trial_extreme(direction):
    p = Params(use_balance_detector=True)
    zone = Zone(101, 103, ZoneRole.RESISTANCE, box_opposite=90)
    bar = (3_600_000, 99, 110, 98, 100.5, 1)
    extreme = 104
    if direction > 0:
        zone = Zone(97, 99, ZoneRole.SUPPORT, box_opposite=110)
        bar = _mirror_bar(bar)
        extreme = 96
    detector = EventDetector(p)
    detector.structure._zones.zones = [zone]
    detector.structure._regime = Regime.RANGE
    detector._episodes = [_Episode(zone, 0, extreme, 0, puncture_index=0, outside_closes=1)]
    events = detector._zone_events(1, bar[0], *bar[1:5], 104 if direction < 0 else 96, 1)
    event, = [event for event in events if event.id == LEVEL_REJECTED]
    assert event.attrs["sub_kind"] == "R3_TRAP"
    assert event.attrs["ex"] == pytest.approx(110 if direction < 0 else 90)
    assert event.attrs["invalid"] == pytest.approx(110.1 if direction < 0 else 89.9)