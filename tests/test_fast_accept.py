"""Only matching trends may ACCEPT the first decisive closed bar."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_operator.events import BREAKOUT_ACCEPTED, EventDetector, fast_accept_ok
from market_operator.structure import Params, Regime, ZoneRole, ZoneState
from market_operator.trigger import MicTracker
from test_v22_pipeline import _make_box_bars


def _setup(monkeypatch, *, direction=1, regime=Regime.TREND_UP, fast_atr=0.5):
    params = Params(
        use_balance_detector=True, balance_quality_min=0.35,
        n_a=3, n_f=3, k_bm=0.1, k_rj=0.3,
        accept_max_ext_atr=1.5, fast_accept_atr=fast_atr)
    detector = EventDetector(params)
    bars = _make_box_bars(cycles=4)
    for bar in bars:
        detector.update(bar)
    role = ZoneRole.RESISTANCE if direction > 0 else ZoneRole.SUPPORT
    zones = [z for z in detector.structure.zones
             if z.role is role and z.state is ZoneState.ACTIVE]
    assert len(zones) == 1
    monkeypatch.setattr(detector.structure, "regime", lambda: regime)
    return detector, zones[0], detector.structure.atr(), bars


def _breakout(ts, zone, atr, direction, ext_atr):
    edge = zone.hi if direction > 0 else zone.lo
    close = edge + direction * ext_atr * atr
    opened = edge + direction * 0.05 * atr
    high = close + 0.1 * atr if direction > 0 else zone.hi + 0.1 * atr
    low = zone.lo - 0.1 * atr if direction > 0 else close - 0.1 * atr
    return (ts, opened, high, low, close, 200.0)


@pytest.mark.parametrize("direction,regime", [
    (1, Regime.TREND_UP), (-1, Regime.TREND_DOWN)])
@pytest.mark.parametrize("layer", ["event", "mic"])
def test_matching_trend_emits_on_first_close(monkeypatch, direction, regime, layer):
    detector, zone, atr, bars = _setup(monkeypatch, direction=direction, regime=regime)
    bar = _breakout(bars[-1][0] + 3_600_000, zone, atr, direction, 0.9)
    if layer == "event":
        emitted = detector.update(bar)
    else:
        tracker = MicTracker(detector.params, scale=6,
                             trigger_tf_seconds=7200, obs_tf_seconds=43200)
        tracker._ctx = tracker._make_context(detector.structure)
        tracker._prev_trig_close = bars[-1][4]
        emitted = tracker.on_trigger_bar(0, *bar[:5])
        assert tracker._candidates == []
    accepts = [e for e in emitted if e.id == BREAKOUT_ACCEPTED]
    assert len(accepts) == 1
    event = accepts[0]
    assert event.direction == direction
    assert event.attrs["fast"] is True
    assert event.attrs["fast_bars"] == 1
    assert event.attrs["sub_kind"] == "A3_FAST"
    assert event.attrs["ref"] == (zone.hi if direction > 0 else zone.lo)


@pytest.mark.parametrize("regime,fast_atr,ext_atr", [
    (Regime.RANGE, 0.5, 0.9),
    (Regime.TRANSITION, 0.5, 0.9),
    (Regime.TREND_UP, 0.5, 0.3),
    (Regime.TREND_UP, None, 0.9),
])
@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("layer", ["event", "mic"])
def test_nontrend_weak_or_disabled_waits_for_three(
        monkeypatch, regime, fast_atr, ext_atr, direction, layer):
    if direction < 0 and regime is Regime.TREND_UP:
        regime = Regime.TREND_DOWN
    detector, zone, atr, bars = _setup(
        monkeypatch, direction=direction, regime=regime, fast_atr=fast_atr)
    tracker = MicTracker(detector.params, scale=6,
                         trigger_tf_seconds=7200, obs_tf_seconds=43200)
    tracker._ctx = tracker._make_context(detector.structure)
    tracker._prev_trig_close = bars[-1][4]
    emitted = []
    for i in range(3):
        bar = _breakout(bars[-1][0] + (i + 1) * 3_600_000,
                        zone, atr, direction, ext_atr + i * 0.01)
        events = (detector.update(bar) if layer == "event" else
                  tracker.on_trigger_bar(i, *bar[:5]))
        accepts = [e for e in events if e.id == BREAKOUT_ACCEPTED]
        if i < 2:
            assert accepts == []
        emitted.extend(accepts)
    assert len(emitted) == 1
    assert emitted[0].direction == direction
    assert emitted[0].attrs["fast"] is False
    assert emitted[0].attrs["fast_bars"] == 0


@pytest.mark.parametrize("direction,regime", [
    (1, Regime.TREND_DOWN), (-1, Regime.TREND_UP)])
def test_countertrend_never_uses_fast_path(direction, regime):
    params = Params(use_balance_detector=True, fast_accept_atr=0.5)
    candidate = SimpleNamespace(count=1, start_close=101.0 if direction > 0 else 98.0,
                                hi=100.0, lo=99.0)
    bar = (dict(o=100.2, h=101.5, l=100.0, c=101.4) if direction > 0 else
           dict(o=98.8, h=99.0, l=97.5, c=97.6))
    assert fast_accept_ok(params, candidate, direction=direction, atr=1.0,
                          regime=regime, **bar) == (False, 0)


@pytest.mark.parametrize("overrides,count,atr", [
    ({"h": 104.0}, 1, 1.0),
    ({"o": 101.45}, 1, 1.0),
    ({"h": 102.0, "c": 101.9}, 1, 1.0),
    ({}, 2, 1.0),
    ({}, 1, 0.0),
])
def test_nondecisive_overextended_or_late_close_is_not_fast(overrides, count, atr):
    params = Params(use_balance_detector=True, fast_accept_atr=0.5)
    candidate = SimpleNamespace(count=count, start_close=101.0, hi=100.0, lo=99.0)
    bar = {"o": 100.2, "h": 101.5, "l": 100.0, "c": 101.4, **overrides}
    assert fast_accept_ok(params, candidate, direction=1, atr=atr,
                          regime=Regime.TREND_UP, **bar) == (False, 0)


def test_production_enables_only_first_close_trend_fast_accept():
    path = Path(__file__).resolve().parents[1] / "config" / "strategy.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    params = Params.from_json(path)
    assert params.fast_accept_atr == 0.5
    assert params.n_a == 3
    assert {tuple(item) for item in raw["whitelist"]} == {
        ("ACCEPT", 1), ("ACCEPT", -1), ("REJECT", 1), ("REJECT", -1)}