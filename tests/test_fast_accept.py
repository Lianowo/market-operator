"""Conditional fast ACCEPT: only a decisive first close may skip n_a."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_operator.events import BREAKOUT_ACCEPTED, EventDetector
from market_operator.structure import Params, ZoneRole, ZoneState
from test_v22_pipeline import _make_box_bars


def _detector(fast_atr):
    return EventDetector(Params(
        use_balance_detector=True, balance_quality_min=0.35,
        n_a=3, n_f=3, k_bm=0.1, k_rj=0.3,
        accept_max_ext_atr=1.5, fast_accept_atr=fast_atr))


def _resistance(det):
    zones = [z for z in det.structure.zones
             if z.role is ZoneRole.RESISTANCE and z.state is ZoneState.ACTIVE]
    assert len(zones) == 1
    return zones[0]


def _feed_box(det):
    for b in _make_box_bars(cycles=4):
        det.update(b)
    return len(_make_box_bars(cycles=4)), det.structure.atr()


def test_strong_first_close_emits_fast_accept():
    det = _detector(0.50)
    n, atr = _feed_box(det)
    rz = _resistance(det)
    last_ts = _make_box_bars(cycles=4)[-1][0]
    step = 3_600_000

    c1 = rz.hi + 0.70 * atr
    o1 = rz.hi + 0.05 * atr
    h1 = c1 + 0.10 * atr
    l1 = rz.lo - 0.10 * atr
    ev = det.update((last_ts + step, o1, h1, l1, c1, 200.0))
    accepts = [e for e in ev if e.id == BREAKOUT_ACCEPTED]
    assert len(accepts) == 1
    assert accepts[0].attrs["fast"] is True
    assert accepts[0].attrs["sub_kind"] == "A3_FAST"
    assert accepts[0].known_index == n


def test_weak_first_close_waits_for_three():
    det = _detector(0.50)
    n, atr = _feed_box(det)
    rz = _resistance(det)
    last_ts = _make_box_bars(cycles=4)[-1][0]
    step = 3_600_000

    c1 = rz.hi + 0.20 * atr
    o1 = rz.hi + 0.05 * atr
    h1 = c1 + 0.05 * atr
    l1 = rz.lo - 0.05 * atr
    ev1 = det.update((last_ts + step, o1, h1, l1, c1, 120.0))
    assert not any(e.id == BREAKOUT_ACCEPTED for e in ev1)

    c2, o2 = c1 + 0.05 * atr, c1
    ev2 = det.update((last_ts + 2 * step, o2, c2 + 0.05 * atr,
                      o2 - 0.05 * atr, c2, 120.0))
    assert not any(e.id == BREAKOUT_ACCEPTED for e in ev2)

    c3, o3 = c2 + 0.05 * atr, c2
    ev3 = det.update((last_ts + 3 * step, o3, c3 + 0.05 * atr,
                      o3 - 0.05 * atr, c3, 120.0))
    accepts = [e for e in ev3 if e.id == BREAKOUT_ACCEPTED]
    assert len(accepts) == 1
    assert accepts[0].attrs["fast"] is False
    assert accepts[0].known_index == n + 2
