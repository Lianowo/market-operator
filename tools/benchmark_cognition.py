"""Offline cognition benchmark against a source snapshot (not a Git HEAD assumption).

Uses identical inputs, fresh interpreters per case/side/round, alternating AB/BA,
exact output fingerprints and separate timing/memory runs. Run without competing
tests/replays. Ratios are descriptive, not a statistical non-regression verdict.
Never starts live execution or rewrites fixtures.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _plain(value):
    if dataclasses.is_dataclass(value):
        # Process-local zone allocation is not a market fact. All structural
        # fields, order, prices and timestamps remain in the fingerprint.
        return {f.name: _plain(getattr(value, f.name))
                for f in dataclasses.fields(value) if f.name != "zone_uid"}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _case_labels(sizes):
    return [label for n in sizes for label in (f"dense_pivots_{n}", f"no_pivots_{n}")] + [
        f"BTCUSDT_1h_balance_{balance}" for balance in (False, True)]


def _fingerprint(outcome):
    payload = json.dumps(_plain(outcome), sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _measure(prepare, mode, repeats, warmups):
    """Only prepare is timed/traced; serialization and GC are outside both."""
    if tracemalloc.is_tracing():
        raise RuntimeError("worker must start without tracemalloc")
    for _ in range(warmups):
        outcome = prepare()
        del outcome
    samples, cpu_samples, peaks, fingerprints = [], [], [], []
    for _ in range(repeats if mode == "timing" else 1):
        gc.collect()
        if mode == "timing":
            cpu_start = time.process_time()
            start = time.perf_counter()
            outcome = prepare()
            elapsed = time.perf_counter() - start
            cpu_elapsed = time.process_time() - cpu_start
            samples.append(elapsed)
            cpu_samples.append(cpu_elapsed)
        else:
            tracemalloc.start()
            try:
                outcome = prepare()
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            peaks.append(peak)
        fingerprints.append(_fingerprint(outcome))
        pivot_count = sum(map(len, outcome.pivots_by.values()))
        del outcome
    return {"samples_seconds": samples, "samples_cpu_seconds": cpu_samples,
            "peaks_traced_bytes": peaks, "fingerprints": fingerprints,
            "pivots": pivot_count}


def _worker(args):
    sys.path.insert(0, str(args.source))
    import numpy as np
    import pandas as pd
    import market_operator.replay as replay
    from market_operator.replay import prepare_cognition
    from market_operator.structure import Params

    if not Path(replay.__file__).resolve().is_relative_to(args.source.resolve()):
        raise RuntimeError("worker imported replay outside requested source")
    label = args.case
    if label.startswith(("dense_pivots_", "no_pivots_")):
        n = int(label.rsplit("_", 1)[1])
        df = pd.DataFrame({"ts": np.arange(n, dtype="int64") * 3_600_000,
                           "open": 100., "high": 102., "low": 98.,
                           "close": 100., "volume": 1.})
        params = Params(k_rev=.5) if label.startswith("dense_pivots_") else Params()
    else:
        df = pd.read_csv(ROOT / "data" / "raw" / "BTCUSDT_1h.csv")
        params = Params(use_balance_detector=label.endswith("_True"))
    result = _measure(lambda: prepare_cognition(df, params, "BTCUSDT", enable_e7=True),
                      args.mode, args.repeats, args.warmups)
    if label.startswith("dense_pivots_") and result["pivots"] < len(df) // 2:
        raise AssertionError("dense-pivot benchmark failed to exercise pivot updates")
    if label.startswith("no_pivots_") and result["pivots"] != 0:
        raise AssertionError("zero-pivot benchmark unexpectedly has pivots")
    result["bars"] = len(df)
    return {"environment": {"python": platform.python_version(),
                            "platform": platform.platform(),
                            "numpy": np.__version__, "pandas": pd.__version__,
                            "executable": sys.executable, "pid": os.getpid(),
                            "replay_file": str(Path(replay.__file__).resolve())},
            "result": result}


def _stats(samples):
    median = statistics.median(samples)
    return {"median": median, "min": min(samples), "max": max(samples),
            "mad": statistics.median(abs(x - median) for x in samples), "n": len(samples)}


def _source_digest(source):
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*.py")):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_worker(args, source, label, mode):
    command = [sys.executable, str(Path(__file__).resolve()), "--source", str(source),
               "--case", label, "--mode", mode, "--repeats", str(args.repeats),
               "--warmups", str(args.warmups)]
    return json.loads(subprocess.check_output(command, cwd=ROOT, text=True,
                                             env={**os.environ, "PYTHONHASHSEED": "0"}))


def _benchmark(args):
    sources = {"baseline": args.baseline_source.resolve(), "current": ROOT / "src"}
    before = {side: _source_digest(source) for side, source in sources.items()}
    records = []
    labels = _case_labels(args.sizes)
    # Every case has fresh interpreters in AB, BA, AB, BA order. No traced
    # allocation run can precede/interfere with a timing worker in this run.
    journal = args.output.with_suffix(".samples.jsonl") if args.output else None
    if journal:
        journal.write_text("", encoding="utf-8")
    for mode, rounds in (("timing", args.rounds), ("memory", 1)):
        for round_index in range(rounds):
            order = ["baseline", "current"]
            if bool(round_index % 2) != args.reverse_order:
                order.reverse()
            for label in labels:
                for position, side in enumerate(order):
                    worker = _run_worker(args, sources[side], label, mode)
                    record = {"mode": mode, "round": round_index + 1, "case": label,
                              "side": side, "position": position + 1,
                              "order": "/".join(order), **worker}
                    records.append(record)
                    if journal:
                        with journal.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(record) + "\n")
                    print(f"{mode} round={round_index + 1} {label} {side}",
                          file=sys.stderr, flush=True)
    after = {side: _source_digest(source) for side, source in sources.items()}
    results = {"schema_version": 3, "protocol": {
        "rounds": args.rounds, "repeats_per_worker": args.repeats,
        "warmups_per_worker": args.warmups, "reverse_order": args.reverse_order,
        "timing": "fresh interpreter per case/side/round; alternating AB/BA; wall and CPU",
        "memory": "separate fresh interpreters after ALL timing; one traced sample per side/case",
        "fingerprint": "exact JSON SHA256 on every measured outcome; excludes only zone_uid",
        "ratio_definition": "current / baseline elapsed seconds; >1 means current slower",
        "statistical_scope": "descriptive samples/median/MAD/range; no automatic performance pass/fail",
        "sources": {side: str(source) for side, source in sources.items()},
        "source_sha256_before": before, "source_sha256_after": after,
        "sources_unchanged": before == after,
        "fixture_sha256": hashlib.sha256(
            (ROOT / "data" / "raw" / "BTCUSDT_1h.csv").read_bytes()).hexdigest(),
    }, "records": records, "comparison": {}}
    for label in labels:
        selected = [r for r in records if r["case"] == label]
        summary = {}
        for side in sources:
            timing = [r for r in selected if r["side"] == side and r["mode"] == "timing"]
            memory = [r for r in selected if r["side"] == side and r["mode"] == "memory"]
            samples = [x for r in timing for x in r["result"]["samples_seconds"]]
            summary[side] = {"samples_seconds": samples, "seconds": _stats(samples),
                             "peak_traced_bytes": memory[0]["result"]["peaks_traced_bytes"][0]}
        paired = []
        for round_index in range(1, args.rounds + 1):
            pair = {r["side"]: r for r in selected
                    if r["mode"] == "timing" and r["round"] == round_index}
            paired.append({"round": round_index, "order": pair["baseline"]["order"],
                           "current_over_baseline": statistics.median(
                               pair["current"]["result"]["samples_seconds"]) / statistics.median(
                               pair["baseline"]["result"]["samples_seconds"])})
        hashes = {x for r in selected for x in r["result"]["fingerprints"]}
        summary.update(equal=len(hashes) == 1, fingerprints=sorted(hashes),
                       paired_round_ratios=paired,
                       paired_ratio_stats=_stats([p["current_over_baseline"] for p in paired]),
                       current_over_baseline=summary["current"]["seconds"]["median"] /
                       summary["baseline"]["seconds"]["median"],
                       peak_memory_ratio=summary["current"]["peak_traced_bytes"] /
                       summary["baseline"]["peak_traced_bytes"])
        results["comparison"][label] = summary
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("timing", "memory"), default="timing",
                        help=argparse.SUPPRESS)
    parser.add_argument("--rounds", type=int, default=4, help="AB/BA rounds (at least 2)")
    parser.add_argument("--repeats", type=int, default=3, help="timed samples per fresh worker")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--sizes", type=int, nargs="+", default=[8000, 32000])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reverse-order", action="store_true")
    args = parser.parse_args()
    if args.rounds < 2 or args.repeats < 1 or args.warmups < 0 or any(size < 1 for size in args.sizes):
        parser.error("rounds must be >= 2; repeats/sizes positive; warmups nonnegative")
    if args.source is not None:
        args.source = args.source.resolve()
        if args.case not in _case_labels(args.sizes):
            # Custom synthetic sizes are encoded in --case by the parent.
            prefix, _, size = (args.case or "").rpartition("_")
            if prefix not in ("dense_pivots", "no_pivots") or not size.isdigit() or int(size) < 1:
                parser.error("invalid worker case")
        print(json.dumps(_worker(args)))
        return 0
    if args.baseline_source is None:
        parser.error("--baseline-source is required")
    if not (args.baseline_source.resolve() / "market_operator").is_dir():
        parser.error("baseline source must contain market_operator")
    if args.output:
        args.output = args.output.resolve()
    results = _benchmark(args)
    payload = json.dumps(results, indent=2)
    if args.output is not None:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return int(not results["protocol"]["sources_unchanged"] or
               not all(case["equal"] for case in results["comparison"].values()))


if __name__ == "__main__":
    raise SystemExit(main())