"""Bounded offline risk search; shortlist on dev before inspecting validation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.campaign import run_campaigns
from market_operator.scenario import ScenarioRunner
from replay_compounding import concentration, trade_stats, write_csv


def candidates() -> list[dict]:
    result = []
    for risk in (.005, .0075, .01, .015, .02):
        for portfolio in (.06, .09, .12):
            for slots in (2, 3):
                result.append({"id": f"r{risk:g}_p{portfolio:g}_s{slots}",
                               "risk_frac": risk, "portfolio_cap_frac": portfolio,
                               "tf_cap_by_tf": {tf: risk * slots for tf in ("2h", "4h", "12h", "1d")},
                               "dynamic_risk_tiers": None})
    result.append({"id": "old_dynamic", "risk_frac": .04,
                   "portfolio_cap_frac": .5,
                   "tf_cap_by_tf": {tf: .12 for tf in ("2h", "4h", "12h", "1d")},
                   "dynamic_risk_tiers": [[250, .04], [600, .03], [1500, .02], [1e9, .01]]})
    return result


def shortlist(results: dict, budget: float, limit: int = 3) -> list[str]:
    eligible = [(key, value["dev"]) for key, value in results.items()
                if value["dev"]["max_drawdown"] <= budget
                and value["dev"]["equity_end"] > 300]
    return [key for key, _ in sorted(eligible,
            key=lambda pair: (-pair[1]["equity_end"], pair[0]))[:limit]]


def passes(scenarios: dict, budget: float) -> bool:
    return all(key in scenarios and scenarios[key]["max_drawdown"] <= budget
               and scenarios[key]["equity_end"] > 300
               for key in ("dev", "test", "full", "stress"))


def run(runner, config: dict, segment: str, cost: float):
    s, c = runner.settings, runner.settings.costs
    return run_campaigns(
        runner.frames, segment=segment, dev_frac=s.dev_frac, equity0=300,
        risk_frac=config["risk_frac"], leverage_cap=s.leverage_cap,
        taker_fee=c.taker_fee, maker_fee=c.maker_fee,
        slippage_per_side=c.slippage_per_side, funding_per_8h=c.funding_per_8h,
        cost_multiplier=cost, funding_rates=c.scaled(cost).funding_rates,
        slippage_by_symbol=c.slippage_by_symbol, contracts=runner.contracts,
        params=s.structure, n_fill=s.n_fill,
        portfolio_cap_frac=config["portfolio_cap_frac"],
        tf_cap_by_tf=config["tf_cap_by_tf"], dynamic_risk_tiers=config["dynamic_risk_tiers"],
        capacity_mode=s.capacity_mode, arm=s.arm, router_params=s.router,
        router_params_by_tf=dict(s.router_by_timeframe), enable_mic=True)


def summarize(result) -> dict:
    rows = result.equity_rows
    peak, dd = 300, 0
    for row in rows:
        peak = max(peak, row[1])
        dd = max(dd, (peak - row[1]) / peak)
    final = rows[-1]
    assert math.isclose(final[2] - 300, sum(t["net_pnl"] for t in result.trades), abs_tol=1e-6)
    assert math.isclose(final[1], final[2] + final[3] - final[4], abs_tol=1e-6)
    closed = [t for t in result.trades if t["closed"] and t["in_segment_open"]]
    return {"equity_end": final[1], "cash_end": final[2],
            "max_drawdown": dd, "closed": trade_stats(closed),
            "concentration": concentration(closed), "cap_skips": len(result.cap_skips),
            "open_end": sum(not t["closed"] for t in result.trades),
            "by_tf": {tf: trade_stats([t for t in closed if t["tf"] == tf])
                      for tf in ("2h", "4h", "12h", "1d")}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    configs = candidates()
    plan = {"equity0": 300, "budgets": [.15, .20, .30], "candidates": configs,
            "selection": "Top 3 dev terminal equities within each budget, frozen before validation.",
            "validation": "dev/test/full and full@1.5x all profitable with drawdown <= budget.",
            "ranking": "Highest full equity among passing top-3; if none pass, dev-ranked backups until first passing. Not global optimum.",
            "limitations": "Reused historical test; not virgin OOS. No live daily breaker, dynamic universe or API failures.",
            "strategy_sha256": hashlib.sha256((ROOT / "config/strategy.json").read_bytes()).hexdigest()}
    (out / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    runner = ScenarioRunner(("BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT"))
    results_path = out / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8")) if args.resume else {}

    def save():
        (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    for i, config in enumerate(configs, 1):
        if config["id"] in results:
            assert results[config["id"]]["config"] == config
            continue
        result = run(runner, config, "dev", 1)
        results[config["id"]] = {"config": config, "dev": summarize(result)}
        save()
        print(f"DEV {i}/{len(configs)} {config['id']} equity={results[config['id']]['dev']['equity_end']:.2f}", flush=True)
    selected = {str(b): shortlist(results, b) for b in plan["budgets"]}
    backups = {str(b): shortlist(results, b, len(configs))[3:] for b in plan["budgets"]}
    (out / "shortlist.json").write_text(json.dumps(selected, indent=2), encoding="utf-8")
    (out / "backup_order.json").write_text(json.dumps(backups, indent=2), encoding="utf-8")

    def validate(key):
        for name, segment, cost in (("test", "test", 1), ("full", "full", 1), ("stress", "full", 1.5)):
            if name in results[key]:
                continue
            result = run(runner, results[key]["config"], segment, cost)
            results[key][name] = summarize(result)
            write_csv(out / f"{key}.{name}.trades.csv", result.trades)
            write_csv(out / f"{key}.{name}.equity.csv", [dict(zip(
                ("ts", "equity", "cash", "unrealized", "funding", "open_positions"), row))
                for row in result.equity_rows])
            save()
            print(f"VALIDATE {key} {name} equity={results[key][name]['equity_end']:.2f} dd={results[key][name]['max_drawdown']:.2%}", flush=True)

    for key in sorted({key for group in selected.values() for key in group}):
        validate(key)
    winners = {}
    for b in plan["budgets"]:
        good = [key for key in selected[str(b)] if passes(results[key], b)]
        if not good:
            for key in backups[str(b)]:
                validate(key)
                if passes(results[key], b):
                    good = [key]
                    break
        winners[str(b)] = max(good, key=lambda key: results[key]["full"]["equity_end"]) if good else None
    (out / "winners.json").write_text(json.dumps(winners, indent=2), encoding="utf-8")
    print("COMPLETE", json.dumps(winners), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())