"""局部 pivot 查询不构造完整快照,公开历史仍保持不可变契约。"""

import pytest

from market_operator.events import EventDetector
from market_operator.replay import prepare_cognition
from market_operator.structure import MarketStructure, Params
from tests.test_structure import make_random_df


def test_pivot_snapshot_is_lazy_cached_and_stable_after_updates():
    structure = MarketStructure(Params())
    bars = list(make_random_df().itertuples(index=False, name=None))
    snapshots = []
    for bar in bars:
        count = structure.pivot_count
        structure.update(bar)
        if structure.pivot_count > count:
            assert structure._pivots_tuple is None
        snapshot = structure.pivots
        assert isinstance(snapshot, tuple)
        assert structure.pivots is snapshot
        assert snapshot == tuple(structure._pivots)
        snapshots.append(snapshot)

    assert len(structure.pivots) > 3
    for snapshot in snapshots:
        assert snapshot == structure.pivots[:len(snapshot)]


@pytest.mark.parametrize("start,stop", [
    (None, None), (0, None), (-8, None), (0, 0), (3, 6),
    (-1000, 1000), (1000, None), (-3, -1), (6, 3),
])
def test_pivot_slice_matches_tuple_slicing_without_full_snapshot(start, stop):
    structure = MarketStructure(Params())
    for bar in make_random_df().itertuples(index=False, name=None):
        structure.update(bar)
    assert structure._pivots_tuple is None
    expected = tuple(structure._pivots)[start:stop]
    assert structure.pivot_slice(start, stop) == expected
    assert structure.pivot_count == len(structure._pivots)
    assert structure._pivots_tuple is None


@pytest.mark.parametrize("balance", [False, True])
@pytest.mark.parametrize("window", [0, 8])
def test_cognition_matches_full_history_reference_without_reading_snapshot(monkeypatch, balance, window):
    params = Params(use_balance_detector=balance, range_max_pivots=window)
    df = make_random_df(n=800)
    detector = EventDetector(params, enable_e7=True)
    expected_pivots, expected_events, expected_views = {}, {}, {}
    for index, bar in enumerate(df.itertuples(index=False, name=None)):
        previous = len(detector.structure.pivots)
        events = detector.update(bar)
        structure = detector.structure
        added = list(structure.pivots[previous:])
        if added:
            expected_pivots[index] = added
        if events:
            expected_events[index] = events
            expected_views[index] = (structure.regime(), structure.zones,
                                     structure.atr(), structure.pivots[-window:])

    def unexpected_snapshot(self):
        raise AssertionError("cognition must not materialize complete pivot history")

    monkeypatch.setattr(MarketStructure, "pivots", property(unexpected_snapshot))
    actual = prepare_cognition(df, params, "X", enable_e7=True)
    assert actual.pivots_by == expected_pivots
    assert actual.events_by == expected_events
    assert {i: (view.regime, view.zones, view.atr, view.pivots)
            for i, view in actual.views.items()} == expected_views


def test_cognition_reads_pivot_count_once_per_bar(monkeypatch):
    df = make_random_df(n=100)
    getter = MarketStructure.pivot_count.fget
    reads = []

    def counted(structure):
        reads.append(1)
        return getter(structure)

    monkeypatch.setattr(MarketStructure, "pivot_count", property(counted))
    prepare_cognition(df, Params(), "X")
    assert len(reads) == len(df)
