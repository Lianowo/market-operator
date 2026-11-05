"""Regression of acceptance evidence reset, fully synthetic/offline."""

import pytest

from market_operator.events import EventDetector, _AcceptCandidate, BREAKOUT_ACCEPTED
from market_operator.structure import Params, Zone, ZoneRole, ZoneState, Regime


@pytest.mark.parametrize("direction", [1, -1])
def test_refresh_is_not_a_second_break_and_must_not_restart_evidence(direction):
    p = Params(use_balance_detector=True, n_a=3, accept_max_ext_atr=99)
    role = ZoneRole.RESISTANCE if direction > 0 else ZoneRole.SUPPORT

    def setup():
        det = EventDetector(p)
        z = Zone(lo=99, hi=100, role=role, state=ZoneState.BROKEN, last_change_known=2)
        det.structure._zones.zones = [z]
        det.structure._regime = Regime.TRANSITION
        det._candidates = [_AcceptCandidate(z, 99, 100, role, 2, 0, 101 if direction > 0 else 98)]
        det._accept_refresh_only = {z.zone_id}
        return det

    args = (2, 200, 101 if direction > 0 else 98,
            103 if direction > 0 else 98.5, 100.5 if direction > 0 else 96,
            102 if direction > 0 else 97, 101 if direction > 0 else 98, 1)
    candidate = setup()
    emitted = candidate._zone_events(*args)
    assert any(ev.id == BREAKOUT_ACCEPTED and ev.attrs["started"] == 0 for ev in emitted)
    assert candidate.structure._zones.zones[0].last_change_known == 2


@pytest.mark.parametrize("old_state,changed,expected", [
    (ZoneState.ACTIVE, False, False), (ZoneState.BROKEN, False, True),
    (ZoneState.RETESTED, False, True), (ZoneState.BROKEN, True, False)])
def test_refresh_classification_is_enabled_by_default(monkeypatch, old_state, changed, expected):
    det = EventDetector(Params(use_balance_detector=True))
    z = Zone(99, 100, ZoneRole.RESISTANCE, state=old_state)
    det.structure._zones.zones = [z]
    def update(bar):
        z.state = ZoneState.BROKEN
        if changed:
            z.hi = 101
        det.structure._bars.append(bar)
    monkeypatch.setattr(det.structure, "update", update)
    det.update((0, 102, 103, 101, 102, 1))
    assert (z.zone_id in det._accept_refresh_only) == expected


def test_refreshed_consumed_break_does_not_rearm():
    det = EventDetector(Params(use_balance_detector=True))
    z = Zone(99, 100, ZoneRole.RESISTANCE, state=ZoneState.BROKEN, last_change_known=2)
    det.structure._zones.zones = [z]
    det._accept_refresh_only = {z.zone_id}
    assert det._zone_events(2, 200, 101, 103, 100.5, 102, 101, 1) == []
    assert det._candidates == []


def test_real_break_starts_new_acceptance_candidate():
    det = EventDetector(Params(use_balance_detector=True))
    z = Zone(99, 100, ZoneRole.RESISTANCE, state=ZoneState.BROKEN, last_change_known=2)
    det.structure._zones.zones = [z]
    det._zone_events(2, 200, 101, 103, 100.5, 102, 101, 1)
    assert len(det._candidates) == 1
    assert det._candidates[0].started == 2
