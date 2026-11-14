"""Benchmark fingerprints exclude only allocator identity, never market facts."""

import importlib.util
import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_operator.structure import Zone, ZoneRole


@pytest.fixture
def benchmark():
    path = Path(__file__).resolve().parents[1] / "tools" / "benchmark_cognition.py"
    spec = importlib.util.spec_from_file_location("benchmark_cognition", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cognition_fingerprint_preserves_structural_identity_and_geometry(benchmark):
    from market_operator.level import LevelKind, LevelState
    module = benchmark
    zone = Zone(
        uid=1,
        role=ZoneRole.SUPPORT,
        kind=LevelKind.SWING,
        extreme=99.0,
        neck=100.0,
        state=LevelState.FRESH,
        origin_index=3,
        known_index=6,
        origin_ts=3600000,
        last_change_known=6,
    )
    same = zone.clone()
    same.uid += 1
    assert module._plain(zone) == module._plain(same)
    same.geometry_version += 1
    assert module._plain(zone) != module._plain(same)
    same = zone.clone()
    same.neck += 1e-10
    assert module._plain(zone) != module._plain(same)


def test_fingerprint_is_exact_for_values_and_sequence(benchmark):
    assert benchmark._fingerprint({"price": 100.}) != benchmark._fingerprint({"price": 100. + 1e-10})
    assert benchmark._fingerprint([1, 2]) != benchmark._fingerprint([2, 1])
    assert benchmark._fingerprint({"ts": 1000}) != benchmark._fingerprint({"ts": 1001})


@pytest.mark.parametrize("mode,traced,calls", [("timing", False, 4), ("memory", True, 2)])
def test_measure_separates_timing_and_tracing(benchmark, monkeypatch, mode, traced, calls):
    states = []

    def prepare():
        states.append(benchmark.tracemalloc.is_tracing())
        return SimpleNamespace(pivots_by={"x": [1, 2]})

    def fingerprint(outcome):
        assert not benchmark.tracemalloc.is_tracing()
        return "exact"

    monkeypatch.setattr(benchmark, "_fingerprint", fingerprint)
    result = benchmark._measure(prepare, mode, repeats=3, warmups=1)
    assert states == [False] + [traced] * (calls - 1)
    assert len(result["samples_seconds"]) == (3 if mode == "timing" else 0)
    assert len(result["samples_cpu_seconds"]) == (3 if mode == "timing" else 0)
    assert len(result["peaks_traced_bytes"]) == (1 if mode == "memory" else 0)
    assert len(result["fingerprints"]) == calls - 1
    assert not benchmark.tracemalloc.is_tracing()


def test_memory_trace_stops_on_failure(benchmark):
    def fail():
        raise ValueError("failure")

    with pytest.raises(ValueError, match="failure"):
        benchmark._measure(fail, "memory", 1, 0)
    assert not benchmark.tracemalloc.is_tracing()


@pytest.mark.parametrize("reverse", [False, True])
def test_schedule_fresh_workers_alternate_and_memory_is_last(benchmark, monkeypatch, tmp_path, reverse):
    args = argparse.Namespace(baseline_source=benchmark.ROOT / "src", sizes=[8], rounds=4,
                              repeats=2, warmups=1, reverse_order=reverse,
                              output=tmp_path / "benchmark.json")
    calls = []

    def run(args, source, label, mode):
        calls.append((source, label, mode))
        return {"environment": {"python": "fake"}, "result": {
            "samples_seconds": [1., 2.] if mode == "timing" else [],
            "samples_cpu_seconds": [1., 2.] if mode == "timing" else [],
            "peaks_traced_bytes": [123] if mode == "memory" else [],
            "fingerprints": ["exact"] * (2 if mode == "timing" else 1),
            "pivots": 0, "bars": 8}}

    monkeypatch.setattr(benchmark, "_run_worker", run)
    result = benchmark._benchmark(args)
    records = result["records"]
    assert len(calls) == 30  # 3 cases * (4 timing rounds + 1 memory round) * 2 sides
    assert [r["mode"] for r in records] == ["timing"] * 24 + ["memory"] * 6
    for round_index in range(4):
        expected = ["baseline", "current"]
        if bool(round_index % 2) != reverse:
            expected.reverse()
        round_records = records[round_index * 6:(round_index + 1) * 6]
        assert [r["side"] for r in round_records] == expected * 3
    assert len(args.output.with_suffix(".samples.jsonl").read_text().splitlines()) == 30
    for summary in result["comparison"].values():
        assert summary["equal"]
        assert summary["baseline"]["seconds"]["n"] == 8
        assert summary["current_over_baseline"] == 1
        assert summary["paired_ratio_stats"]["n"] == 4

    # A mismatch in ANY measured outcome, including memory, is not normalized away.
    def mismatch(*args):
        value = run(*args)
        if args[-1] == "memory":
            value["result"]["fingerprints"] = [str(len(calls))]
        return value

    monkeypatch.setattr(benchmark, "_run_worker", mismatch)
    result = benchmark._benchmark(args)
    assert not any(summary["equal"] for summary in result["comparison"].values())


def test_default_cases_and_stats(benchmark):
    assert benchmark._case_labels([8000, 32000]) == [
        "dense_pivots_8000", "no_pivots_8000", "dense_pivots_32000", "no_pivots_32000",
        "BTCUSDT_1h_level"]
    assert benchmark._stats([1., 2., 3.]) == {"median": 2., "min": 1., "max": 3., "mad": 1., "n": 3}


def test_subprocess_uses_requested_source_and_fixed_hash_seed(benchmark, monkeypatch):
    args = argparse.Namespace(repeats=3, warmups=1)
    captured = []

    def check_output(command, **kwargs):
        captured.append((command, kwargs))
        return '{"result": {}}'

    monkeypatch.setattr(benchmark.subprocess, "check_output", check_output)
    source = benchmark.ROOT / "src"
    for mode in ("timing", "memory"):
        assert benchmark._run_worker(args, source, "no_pivots_32000", mode) == {"result": {}}
    assert len(captured) == 2
    for (command, kwargs), mode in zip(captured, ("timing", "memory")):
        assert command[0] == benchmark.sys.executable
        assert Path(command[1]).is_absolute()
        assert command[command.index("--source") + 1] == str(source)
        assert command[command.index("--mode") + 1] == mode
        assert kwargs["env"]["PYTHONHASHSEED"] == "0"


@pytest.mark.parametrize("flag,value", [("--rounds", "1"), ("--repeats", "0"),
                                       ("--warmups", "-1"), ("--sizes", "0")])
def test_cli_rejects_invalid_protocol(benchmark, monkeypatch, flag, value):
    monkeypatch.setattr(benchmark.sys, "argv", ["benchmark_cognition", flag, value])
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2