"""Cognition identities are not trigger-clock window anchors (offline only)."""

from types import SimpleNamespace

import pytest

from market_operator.events import BREAKOUT_FAILED, LEVEL_REJECTED, EventDetector, _Episode
from market_operator.structure import Params, Regime, Zone, ZoneRole, ZoneState
from market_operator.trigger import MicTracker, prepare_mic_cognition


SCALES = (1, 4, 6)


def _setup(scale, direction, *, balance=True):
    p = Params(use_balance_detector=balance)
    z = Zone(100, 102, ZoneRole.RESISTANCE if direction < 0 else ZoneRole.SUPPORT)
    structure = SimpleNamespace(
        atr=lambda: 1.0, regime=lambda: Regime.RANGE, pivots=(),
        last_low_pivot=None, last_high_pivot=None,
        _zones=SimpleNamespace(zones=[z]),
    )
    det = SimpleNamespace(structure=structure, _episodes=[], _trend_candidates=[])
    tracker = MicTracker(p, scale=scale, trigger_tf_seconds=3600,
                         obs_tf_seconds=3600 * scale)
    # Nonzero grid origin: cognition index multiplication is not the mapping API.
    start = lambda i: 17 + i * scale
    end = lambda i: start(i) + scale - 1
    return tracker, det, z, start, end


def _price(direction, price):
    return price if direction < 0 else 202 - price


def _bar(tracker, a, direction, o=101, h=101.1, low=100.9, c=101):
    if direction > 0:
        o, h, low, c = 202 - o, 202 - low, 202 - h, 202 - c
    return tracker.on_trigger_bar(a, a * 3_600_000, o, h, low, c)


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
def test_sync_maps_all_time_anchors_but_keeps_cognition_identity(scale, direction):
    tracker, det, z, start, end = _setup(scale, direction)
    det._episodes = [_Episode(z, 10, _price(direction, 103), 11, 12)]
    tracker.sync(12, det, start, end)
    ep, = tracker._episodes
    assert ep.opened == start(10)
    assert ep.ex_index == start(11)
    assert ep.puncture_index == start(12)
    ep.outside_closes = 2
    tracker.sync(13, det, start, end)
    assert tracker._episodes == [ep]
    assert tracker._episodes[0] is ep
    assert ep.outside_closes == 2

    # A new cognition episode in the same zone is not the old identity.
    det._episodes = [_Episode(z, 14, _price(direction, 101.1), 14)]
    tracker.sync(14, det, start, end)
    replacement, = tracker._episodes
    assert replacement is not ep
    assert replacement.opened == start(14)
    det._episodes = []
    tracker.sync(15, det, start, end)
    assert tracker._episodes == []


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
def test_consumed_window_is_scaled_nf_plus_one_with_inclusive_boundary(scale, direction):
    tracker, det, z, start, end = _setup(scale, direction)
    det._episodes = [_Episode(z, 10, _price(direction, 101.1), 10)]
    tracker.sync(10, det, start, end)
    ep, = tracker._episodes
    deadline = start(10) + (tracker.p.n_f + 1) * scale
    for a in range(end(10) + 1, deadline + 1):
        assert _bar(tracker, a, direction) == []
        assert tracker._episodes == [ep], f"expired early at {a}, deadline={deadline}"
    assert _bar(tracker, deadline + 1, direction) == []
    assert tracker._episodes == []
    tracker.sync(15, det, start, end)
    assert tracker._episodes == []  # consumed identities cannot be re-adopted


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("outside_closes", [1, 2])
def test_adoption_copies_existing_outside_close_evidence(scale, direction, outside_closes):
    tracker, det, z, start, end = _setup(scale, direction)
    cog = _Episode(z, 10, _price(direction, 103), 10, 10, outside_closes)
    det._episodes = [cog]
    tracker.sync(10, det, start, end)
    ep, = tracker._episodes
    assert ep.outside_closes == outside_closes
    event, = _bar(tracker, end(10) + 1, direction, 101, 103, 99, 99.5)
    assert event.id == LEVEL_REJECTED
    assert event.direction == direction
    assert event.attrs["sub_kind"] == "R3_TRAP"
    assert event.attrs["outside_closes"] == outside_closes
    assert cog.outside_closes == outside_closes  # cognition remains read-only
    tracker.sync(11, det, start, end)
    assert tracker._episodes == []


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("finished", [False, True])
def test_trigger_created_episode_survives_sync_without_duplicate_or_resurrection(
        scale, direction, merged, finished):
    tracker, det, z, start, end = _setup(scale, direction)
    tracker.sync(9, det, start, end)
    tracker._prev_trig_close = _price(direction, 99)
    opened = end(10)
    assert _bar(tracker, opened, direction) == []
    ep, = tracker._episodes
    # Preserve the finer-clock evidence rather than replacing with cognition's.
    ep.outside_closes = 1
    ep.ex = _price(direction, 103)
    ep.ex_index = opened
    if finished:
        event, = _bar(tracker, opened + 1, direction, 101, 103, 99, 99.5)
        assert event.attrs["sub_kind"] == "R3_TRAP"
        assert tracker._episodes == []
    if merged:
        z = Zone(99.75, 102.25, z.role)
        det.structure._zones.zones = [z]
    det._episodes = [_Episode(z, 10, _price(direction, 101), 10)]
    tracker.sync(10, det, start, end)
    if finished:
        assert tracker._episodes == []
    else:
        kept, = tracker._episodes
        assert kept is ep
        assert kept.zone is z
        assert kept.opened == opened
        assert kept.ex_index == opened
        assert kept.ex == _price(direction, 103)
        assert kept.outside_closes == 1
    tracker.sync(11, det, start, end)
    assert len(tracker._episodes) == (0 if finished else 1)


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
def test_adopted_episode_rebinds_to_containing_merged_zone_once(scale, direction):
    tracker, det, old, start, end = _setup(scale, direction)
    det._episodes = [_Episode(old, 10, _price(direction, 101.1), 10)]
    tracker.sync(10, det, start, end)
    ep, = tracker._episodes
    ep.outside_closes = 2
    merged = Zone(99.75, 102.25, old.role)
    det.structure._zones.zones = [merged]
    det._episodes = [_Episode(merged, 10, _price(direction, 101), 10)]
    tracker.sync(11, det, start, end)
    kept, = tracker._episodes
    assert kept is ep
    assert kept.zone is merged
    assert kept.opened == start(10)
    assert kept.outside_closes == 2
    tracker.sync(12, det, start, end)
    assert tracker._episodes == [ep]


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("kind", ["puncture", "extreme"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_existing_rejection_window_boundaries_remain_unchanged(scale, direction, kind, offset):
    tracker, det, z, start, end = _setup(scale, direction, balance=False)
    puncture = 10 if kind == "puncture" else None
    ex = 102.2 if kind == "puncture" else 101.1
    det._episodes = [_Episode(z, 10, _price(direction, ex), 10, puncture)]
    tracker.sync(10, det, start, end)
    a = start(10) + tracker.p.n_f * scale + offset
    # Puncture reclaim has no LEVEL_REJECTED fallback (retreat < rj).
    values = (102.1, 102.2, 101.94, 101.96) if puncture else (100.4, 101, 99, 99.5)
    events = _bar(tracker, a, direction, *values)
    expected = offset < 0 if puncture else offset <= 0
    assert bool(events) is expected
    if expected:
        event, = events
        assert event.id == (BREAKOUT_FAILED if puncture else LEVEL_REJECTED)
        assert event.direction == direction
        assert event.known_index == a


@pytest.mark.parametrize("state", [ZoneState.BROKEN, ZoneState.RETESTED, ZoneState.DEAD])
def test_sync_does_not_extend_existing_active_zone_policy(state):
    tracker, det, z, start, end = _setup(4, -1)
    det._episodes = [_Episode(z, 10, 101.1, 10)]
    tracker.sync(10, det, start, end)
    z.state = state
    tracker.sync(11, det, start, end)
    assert tracker._episodes == []


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
def test_unmatched_local_touch_does_not_claim_later_cognition_episode(scale, direction):
    tracker, det, z, start, end = _setup(scale, direction)
    tracker.sync(9, det, start, end)
    tracker._prev_trig_close = _price(direction, 99)
    assert _bar(tracker, end(10), direction) == []
    tracker.sync(10, det, start, end)  # no cognition episode: discard local state
    assert tracker._episodes == []
    det._episodes = [_Episode(z, 11, _price(direction, 101.1), 11)]
    tracker.sync(11, det, start, end)
    ep, = tracker._episodes
    assert ep.opened == start(11)
    assert ep.outside_closes == 0


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("direction", [-1, 1])
def test_natural_preparation_is_causal_deterministic_and_leaves_cognition_unchanged(scale, direction):
    from tests.test_v22_pipeline import _make_box_bars

    step = 3_600_000
    cog = _make_box_bars(cycles=6, base_ts=0, step=step * scale)
    if direction > 0:
        cog = [(ts, 230 - o, 230 - low, 230 - h, 230 - c, v)
               for ts, o, h, low, c, v in cog]
    trig = []
    for ts, o, h, low, c, v in cog:
        for j in range(scale):
            opened = o + (c - o) * j / scale
            close = o + (c - o) * (j + 1) / scale
            # Exact parent OHLC, without reading a later cognition context.
            high = h if j == scale - 1 else max(opened, close)
            bottom = low if j == scale - 1 else min(opened, close)
            trig.append((ts + j * step, opened, high, bottom, close, v / scale))
    p = Params(use_balance_detector=True)

    def prepare(cognition, trigger):
        return prepare_mic_cognition(
            cognition, trigger, p, obs_tf_seconds=3600 * scale,
            trigger_tf_seconds=3600, symbol="OFFLINE")

    full = prepare(cog, trig)
    again = prepare(cog, trig)
    cut = len(cog) - 8
    prefix = prepare(cog[:cut], trig[:cut * scale])
    assert full.events_by  # exercise actual event paths, not an empty equality
    assert full.events_by == again.events_by
    assert prefix.events_by == {a: ev for a, ev in full.events_by.items() if a < cut * scale}
    detector = EventDetector(p)
    expected = {}
    for i, bar in enumerate(cog):
        emitted = detector.update(bar)
        if emitted:
            expected[i] = emitted
    assert full.bar_events_by == expected