import pytest

from market_operator.position import (
    PositionManager, opp_lock_arm_hit, opp_lock_protection_step, opp_lock_step,
)
from market_operator.structure import Params

STEP = 900_000


def test_negative_lock_arming_is_not_sampling_invariant():
    # Touching target then returning inside by 2h close is materially different
    # from a 15m close beyond target, even though both saw the same peak.
    assert opp_lock_arm_hit(1, 110, -.25, 112, 108, 111)
    assert not opp_lock_arm_hit(1, 110, -.25, 112, 108, 109)


def test_price_only_exit_does_not_advance_cognition_time_or_tighten_pivots():
    pm = PositionManager(Params(), time_stop=False, reject_opp_lock_frac=-.25)
    pos = pm.open("X", 1, 100, 95, 110, "LEVEL_REJECTED", 0, 0)
    pm.on_bar("X", (STEP, 109, 112, 108, 111, 1), 2,
              price_only=True, skip_trail=True)
    assert pos.bars_held == 0
    assert pos.armed
    assert pos.invalid == pytest.approx(104.5)


@pytest.mark.parametrize("direction,peak,ref", [(1, 112, 111), (-1, 88, 89)])
def test_protection_cannot_grant_qualification(direction, peak, ref):
    result = opp_lock_protection_step(direction=direction, peak=peak,
                                      budget=7.5, ref_px=ref, armed=False)
    assert result.action == "hold"
    assert not result.armed and result.stop_px is None


@pytest.mark.parametrize("direction,peak,ref,action", [
    (1, 112, 109, "tighten"), (1, 112, 104.5, "exit"),
    (1, 112, 103, "exit"), (-1, 88, 91, "tighten"),
    (-1, 88, 95.5, "exit"), (-1, 88, 97, "exit"),
])
def test_qualified_protection_needs_no_new_target_close(direction, peak, ref, action):
    result = opp_lock_protection_step(direction=direction, peak=peak,
                                      budget=7.5, ref_px=ref, armed=True)
    assert result.action == action and result.armed
    assert result.stop_px == pytest.approx(peak - direction * 7.5)
    assert result.drawdown == pytest.approx(direction * (peak - ref))
    adapter = opp_lock_step(direction=direction, entry=100,
                            target=100 + direction * 10, frac=-.25,
                            peak=peak, high=112, low=88, close=100,
                            ref_px=ref, armed=True)
    assert adapter == result


def test_reference_update_executes_fixed_protection_without_new_peak():
    first = opp_lock_protection_step(direction=1, peak=112, budget=7.5,
                                     ref_px=109, armed=True)
    later = opp_lock_protection_step(direction=1, peak=112, budget=7.5,
                                     ref_px=104, armed=True)
    assert first.action == "tighten" and later.action == "exit"
    assert first.stop_px == later.stop_px == 104.5


def test_armed_lock_updates_without_requiring_target_close_again():
    pm = PositionManager(Params(), time_stop=False, reject_opp_lock_frac=-.25)
    pos = pm.open("X", 1, 100, 95, 110, "LEVEL_REJECTED", 0, 0)
    pm.on_bar("X", (STEP, 109, 112, 108, 111, 1), 2,
              price_only=True, skip_trail=True)
    before = pos.invalid
    pm.on_bar("X", (2 * STEP, 111, 113, 108, 109, 1), 2,
              price_only=True, skip_trail=True)
    assert pos.armed and pos.invalid > before
    assert pos.bars_held == 0