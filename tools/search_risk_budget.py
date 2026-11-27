"""Bounded offline risk search; shortlist on dev before inspecting validation."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.campaign import run_campaigns                         # noqa: E402
from market_operator.replay import BacktestResult                          # noqa: E402
from market_operator.scenario import ScenarioRunner                        # noqa: E402
from replay_compounding import (                                           # noqa: E402
    EQUITY_CSV_FIELDS,
    concentration,
    trade_stats,
    write_csv,
)

EQUITY0 = 300.0
VALIDATION_SCENARIOS = ("dev", "test", "full", "stress")
GRID_TFS = ("2h", "4h", "12h", "1d")
BUDGETS = (0.15, 0.20, 0.30)


def candidates() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for risk in (0.005, 0.0075, 0.01, 0.015, 0.02):
        for portfolio in (0.06, 0.09, 0.12):
            for slots in (2, 3):
                result.append({
                    "id": f"r{risk:g}_p{portfolio:g}_s{slots}",
                    "risk_frac": risk,
                    "portfolio_cap_frac": portfolio,
                    "tf_cap_by_tf": {tf: risk * slots for tf in GRID_TFS},
                    "dynamic_risk_tiers": None,
                })
    result.append({
        "id": "old_dynamic",
        "risk_frac": 0.04,
        "portfolio_cap_frac": 0.5,
        "tf_cap_by_tf": {tf: 0.12 for tf in GRID_TFS},
        "dynamic_risk_tiers": [[250, 0.04], [600, 0.03], [1500, 0.02], [1e9, 0.01]],
    })
    return result


def shortlist(
    results: Mapping[str, Mapping[str, Any]],
    budget: float,
    limit: int = 3,
) -> list[str]:
    eligible = [
        (key, value["dev"])
        for key, value in results.items()
        if value["dev"]["max_drawdown"] <= budget
        and value["dev"]["equity_end"] > EQUITY0
    ]
    return [
        key
        for key, _ in sorted(
            eligible, key=lambda pair: (-pair[1]["equity_end"], pair[0])
        )[:limit]
    ]


def passes(scenarios: Mapping[str, Mapping[str, Any]], budget: float) -> bool:
    return all(
        key in scenarios
        and scenarios[key]["max_drawdown"] <= budget
        and scenarios[key]["equity_end"] > EQUITY0
        for key in VALIDATION_SCENARIOS
    )


def run(
    runner: ScenarioRunner,
    config: Mapping[str, Any],
    segment: str,
    cost: float,
) -> BacktestResult:
    s, c = runner.settings, runner.settings.costs
    return run_campaigns(
        runner.frames,
        segment=segment,
        dev_frac=s.dev_frac,
        equity0=EQUITY0,
        risk_frac=config["risk_frac"],
        leverage_cap=s.leverage_cap,
        taker_fee=c.taker_fee,
        maker_fee=c.maker_fee,
        slippage_per_side=c.slippage_per_side,
        funding_per_8h=c.funding_per_8h,
        cost_multiplier=cost,
        funding_rates=c.scaled(cost).funding_rates,
        slippage_by_symbol=c.slippage_by_symbol,
        contracts=runner.contracts,
        params=s.structure,
        n_fill=s.n_fill,
        portfolio_cap_frac=config["portfolio_cap_frac"],
        tf_cap_by_tf=config["tf_cap_by_tf"],
        dynamic_risk_tiers=config["dynamic_risk_tiers"],
        capacity_mode=s.capacity_mode,
        arm=s.arm,
        router_params=s.router,
        router_params_by_tf=dict(s.router_by_timeframe),
        enable_mic=True,
    )


def summarize(result: BacktestResult) -> dict[str, Any]:
    rows = result.equity_rows
    peak, dd = EQUITY0, 0.0
    for row in rows:
        peak = max(peak, row[1])
        dd = max(dd, (peak - row[1]) / peak)
    final = rows[-1]
    if not math.isclose(
        final[2] - EQUITY0, sum(t["net_pnl"] for t in result.trades), abs_tol=1e-6
    ):
        raise AssertionError("cash PnL accounting mismatch")
    if not math.isclose(final[1], final[2] + final[3] - final[4], abs_tol=1e-6):
        raise AssertionError("equity identity mismatch")
    closed = [t for t in result.trades if t["closed"] and t["in_segment_open"]]
    return {
        "equity_end": final[1],
        "cash_end": final[2],
        "max_drawdown": dd,
        "closed": trade_stats(closed),
        "concentration": concentration(closed),
        "cap_skips": len(result.cap_skips),
        "open_end": sum(not t["closed"] for t in result.trades),
        "by_tf": {
            tf: trade_stats([t for t in closed if t["tf"] == tf]) for tf in GRID_TFS
        },
    }


def _validate_candidate(
    runner: ScenarioRunner,
    results: dict[str, dict[str, Any]],
    key: str,
    out: Path,
) -> None:
    for name, segment, cost in (("test", "test", 1.0), ("full", "full", 1.0), ("stress", "full", 1.5)):
        if name in results[key]:
            continue
        result = run(runner, results[key]["config"], segment, cost)
        results[key][name] = summarize(result)
        write_csv(out / f"{key}.{name}.trades.csv", result.trades)
        write_csv(
            out / f"{key}.{name}.equity.csv",
            [dict(zip(EQUITY_CSV_FIELDS, row)) for row in result.equity_rows],
        )
        (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(
            f"VALIDATE {key} {name} equity={results[key][name]['equity_end']:.2f} "
            f"dd={results[key][name]['max_drawdown']:.2%}",
            flush=True,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    configs = candidates()
    plan = {
        "equity0": int(EQUITY0),
        "budgets": list(BUDGETS),
        "candidates": configs,
        "selection": "Top 3 dev terminal equities within each budget, frozen before validation.",
        "validation": "dev/test/full and full@1.5x all profitable with drawdown <= budget.",
        "ranking": (
            "Highest full equity among passing top-3; if none pass, dev-ranked backups "
            "until first passing. Not global optimum."
        ),
        "limitations": (
            "Reused historical test; not virgin OOS. No live daily breaker, "
            "dynamic universe or API failures."
        ),
        "strategy_sha256": hashlib.sha256(
            (ROOT / "config/strategy.json").read_bytes()
        ).hexdigest(),
    }
    (out / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    runner = ScenarioRunner(("BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT"))
    results_path = out / "results.json"
    results: dict[str, dict[str, Any]] = (
        json.loads(results_path.read_text(encoding="utf-8")) if args.resume else {}
    )

    for i, config in enumerate(configs, 1):
        cid = str(config["id"])
        if cid in results:
            if results[cid]["config"] != config:
                raise AssertionError(f"resumed config mismatch for {cid}")
            continue
        result = run(runner, config, "dev", 1.0)
        results[cid] = {"config": config, "dev": summarize(result)}
        results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(
            f"DEV {i}/{len(configs)} {cid} equity={results[cid]['dev']['equity_end']:.2f}",
            flush=True,
        )

    selected = {str(b): shortlist(results, b) for b in BUDGETS}
    backups = {str(b): shortlist(results, b, len(configs))[3:] for b in BUDGETS}
    (out / "shortlist.json").write_text(json.dumps(selected, indent=2), encoding="utf-8")
    (out / "backup_order.json").write_text(json.dumps(backups, indent=2), encoding="utf-8")

    for key in sorted({key for group in selected.values() for key in group}):
        _validate_candidate(runner, results, key, out)

    winners: dict[str, str | None] = {}
    for b in BUDGETS:
        good = [key for key in selected[str(b)] if passes(results[key], b)]
        if not good:
            for key in backups[str(b)]:
                _validate_candidate(runner, results, key, out)
                if passes(results[key], b):
                    good = [key]
                    break
        winners[str(b)] = (
            max(good, key=lambda key: results[key]["full"]["equity_end"]) if good else None
        )
    (out / "winners.json").write_text(json.dumps(winners, indent=2), encoding="utf-8")
    print("COMPLETE", json.dumps(winners), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
