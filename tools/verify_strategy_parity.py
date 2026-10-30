"""Compare offline strategy outputs against a Git commit without replacing fixtures.

Run from any directory: python tools/verify_strategy_parity.py --ref HEAD
Both implementations use the same local market data and committed strategy config.
No exchange, live runtime, or frozen acceptance baseline is modified.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def _fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_worker(args: argparse.Namespace) -> int:
    # Import only after selecting the source tree; the two versions never share
    # an interpreter or imported module state. Only data locations are rebound.
    sys.path.insert(0, str(args.source))
    from market_operator import data

    data.ROOT = ROOT
    data.DATA_DIR = ROOT / "data"
    data.MANIFEST_PATH = data.DATA_DIR / "manifest.json"

    from market_operator import scenario

    scenario.load_funding_rates = functools.partial(
        scenario.load_funding_rates, directory=data.DATA_DIR / "raw" / "funding")
    scenario.load_contract_specs = functools.partial(
        scenario.load_contract_specs, path=data.DATA_DIR / "contracts.json")
    runner = scenario.ScenarioRunner(
        tuple(args.symbols.split(",")), config_path=args.config, progress=False)
    results = {}
    for segment in args.segments.split(","):
        for cost in map(float, args.costs.split(",")):
            outcome = runner.run(segment=segment, cost_multiplier=cost)
            key = f"{segment}@{cost:g}"
            results[key] = {
                "trade_count": len(outcome.trades),
                "trades": _fingerprint(outcome.trades),
                "equity": _fingerprint(outcome.equity_rows),
                "counters": _fingerprint(outcome.counters),
            }
            print(f"[parity] {args.source}: {key} complete", flush=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--symbols", default="BTCUSDT,DOGEUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--segments", default="dev,test,full")
    parser.add_argument("--costs", default="1.0,1.5")
    parser.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.source is not None:
        return _run_worker(args)

    commit = _git("rev-parse", "--verify", f"{args.ref}^{{commit}}").decode().strip()
    with tempfile.TemporaryDirectory(prefix="market-operator-parity-") as directory:
        baseline = Path(directory)
        paths = _git("ls-tree", "-r", "--name-only", commit,
                     "src/market_operator", "config/strategy.json").decode().splitlines()
        for name in paths:
            target = baseline / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_git("show", f"{commit}:{name}"))
        config = baseline / "config" / "strategy.json"
        current_config = ROOT / "config" / "strategy.json"
        if json.loads(config.read_text(encoding="utf-8")) != json.loads(
                current_config.read_text(encoding="utf-8")):
            raise ValueError("Strategy configuration differs from the reference commit")
        outputs = []
        for label, source in (("baseline", baseline / "src"), ("current", ROOT / "src")):
            output = baseline / f"{label}.json"
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve()),
                 "--source", str(source), "--config", str(config),
                 "--output", str(output), "--symbols", args.symbols,
                 "--segments", args.segments, "--costs", args.costs],
                cwd=ROOT, check=True)
            outputs.append(json.loads(output.read_text(encoding="utf-8")))
        reference, current = outputs
        differences = {
            key: {field: {"reference": reference[key][field], "current": current[key][field]}
                  for field in reference[key] if reference[key][field] != current[key][field]}
            for key in reference if reference[key] != current[key]
        }
        print(json.dumps({"commit": commit, "differences": differences,
                          "scenarios": current}, indent=2))
        print("[FAIL] Strategy outputs differ" if differences else
              "[PASS] Trades, equity and counters exactly match the reference commit")
        return int(bool(differences))


if __name__ == "__main__":
    raise SystemExit(main())