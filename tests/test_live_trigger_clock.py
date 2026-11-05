"""Incremental adapter maps old cognition anchors exactly as warmup does."""

from types import SimpleNamespace

import pytest

from market_operator.structure import Params
from tests.test_live_runtime import _FakeClient, _SignalTG, _runner


@pytest.mark.parametrize("scale,trigger_hours", [(4, 2), (6, 2), (6, 4)])
@pytest.mark.parametrize("prefix_hours", [0, 48])
def test_incremental_sync_maps_historical_spans_not_current_bucket(tmp_path, scale, trigger_hours, prefix_hours):
    runner = _runner(tmp_path, _FakeClient(), _SignalTG(), mode="paper")
    # First cognition bucket and (when 4h triggers) first trigger bucket partial.
    obs_hours = trigger_hours * scale
    raw = [(h * 3_600_000, 100, 101, 99, 100, 1) for h in range(2, prefix_hours + obs_hours * 3, 2)]
    cognition = [(h * 3_600_000, 100, 101, 99, 100, 1)
                 for h in range(prefix_hours, prefix_hours + obs_hours * 3, obs_hours)]
    import live_runner as lr
    series = [b for b in raw if b[0] >= cognition[0][0]]
    if trigger_hours > 2:
        series = lr.aggregate_tf(series, trigger_hours * 3600)
    spans = {}
    for k, bar in enumerate(series):
        parent = (bar[0] - cognition[0][0]) // (obs_hours * 3_600_000)
        spans.setdefault(parent, [k, k])[1] = k

    class Capture:
        def sync(self, i, det, start, end):
            assert i == 2
            assert {p: [start(p), end(p)] for p in range(3)} == spans
            assert end(100) == spans[2][1]

    structure = SimpleNamespace(pivots=(), atr=lambda: 1.0)
    cs = SimpleNamespace(
        detector=SimpleNamespace(structure=structure, update=lambda bar: []),
        tracker=Capture(), cog_bars=cognition[:2], trig_bars=raw,
        obs_tf_seconds=obs_hours * 3600, trigger_tf_seconds=trigger_hours * 3600,
        symbol="OFFLINE", pm=SimpleNamespace(positions={}),
    )
    runner.params = Params()
    runner._close_cognition(cs, cognition[2])
    origin = lr.trigger_origin_ms(cs)
    assert [(bar[0] - origin) // (trigger_hours * 3_600_000) for bar in series] == list(range(len(series)))


@pytest.mark.parametrize("trigger_hours,obs_hours", [(2, 12), (4, 24)])
def test_advance_continues_cropped_warmup_trigger_indices(tmp_path, trigger_hours, obs_hours):
    import live_runner as lr

    runner = _runner(tmp_path, _FakeClient(), _SignalTG(), mode="paper")
    runner._on_sub_bar = lambda *args: None
    runner._close_cognition = lambda *args: None
    base = 3_600_000
    raw = [(h * base, 100, 101, 99, 100, 1) for h in range(2, 96, 2)]
    first_cog = 48 * base
    cog = [(h * base, 100, 101, 99, 100, 1) for h in range(48, 96, obs_hours)]
    replay_raw = [b for b in raw if b[0] >= first_cog]
    series = replay_raw if trigger_hours == 2 else lr.aggregate_tf(replay_raw, trigger_hours * 3600)
    captured = []

    class Capture:
        def on_trigger_bar(self, index, *args):
            captured.append(index)
            return []

    cs = SimpleNamespace(symbol="OFFLINE", tf_label=f"{obs_hours}h", cog_bars=cog,
                         trig_bars=list(raw), tracker=Capture(),
                         trigger_tf_seconds=trigger_hours * 3600, obs_tf_seconds=obs_hours * 3600)
    new = [(h * base, 100, 101, 99, 100, 1) for h in range(96, 96 + trigger_hours, 2)]
    runner._advance(cs, (100 + trigger_hours) * base, new_bars=new, run_paper_fills=False)
    assert captured == [len(series)]