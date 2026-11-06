"""Replay production campaigns with live risk settings; never sends orders."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.campaign import run_campaigns
from market_operator.replay import compute_metrics
from market_operator.scenario import ScenarioRunner


def trade_stats(trades: list) -> dict:
    values = [t["net_pnl"] for t in trades]
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    gain, loss = sum(wins), -sum(losses)
    return {
        "n": len(values), "wins": len(wins), "losses": len(losses),
        "breakeven": sum(v == 0 for v in values), "net": sum(values),
        "winrate": len(wins) / len(values) if values else None,
        "profit_factor": gain / loss if loss else None,
        "average_win": statistics.mean(wins) if wins else None,
        "average_loss": statistics.mean(losses) if losses else None,
        "payoff_ratio": statistics.mean(wins) / -statistics.mean(losses)
        if wins and losses else None,
        "expectancy": statistics.mean(values) if values else None,
        "median_net": statistics.median(values) if values else None,
        "best": max(values) if values else None,
        "worst": min(values) if values else None,
    }


def concentration(trades: list) -> dict:
    ordered = sorted(trades, key=lambda t: t["net_pnl"], reverse=True)
    count = math.ceil(len(ordered) * 0.1)
    total = sum(t["net_pnl"] for t in ordered)
    top, rest = trade_stats(ordered[:count]), trade_stats(ordered[count:])
    for group in (top, rest):
        group["share_of_net"] = group["net"] / total if total else None
    return {"top_10pct": top, "remaining_90pct": rest,
            "without_top_10_trades": trade_stats(ordered[10:])}


def streaks(trades: list) -> dict:
    longest_win = longest_loss = win = loss = 0
    for t in sorted(trades, key=lambda t: (t["exit_ts"], t["trade_id"])):
        win = win + 1 if t["net_pnl"] > 0 else 0
        loss = loss + 1 if t["net_pnl"] < 0 else 0
        longest_win, longest_loss = max(win, longest_win), max(loss, longest_loss)
    return {"longest_win": longest_win, "longest_loss": longest_loss}


def write_csv(path: Path, rows: list) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader()
        writer.writerows(rows)


def utc(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()


def select_symbols(requested: str | None, allowed: tuple) -> tuple[tuple[str, ...], bool]:
    """解析 --symbols；返回 (本次回放品种, 是否发生过滤)。

    只允许配置宇宙的子集：换品种集等于换一个场景，不能让拼写错误静默变成
    "只跑了一部分标的还当成全宇宙结果"。
    """
    if requested is None:
        return tuple(allowed), False
    chosen = tuple(s.strip().upper() for s in requested.split(",") if s.strip())
    if not chosen:
        raise ValueError("--symbols 解析为空集")
    unknown = [s for s in chosen if s not in allowed]
    if unknown:
        raise ValueError(f"--symbols 必须是 {', '.join(allowed)} 的子集; 未知: {', '.join(unknown)}")
    if len(set(chosen)) != len(chosen):
        raise ValueError("--symbols 含重复品种")
    return chosen, chosen != tuple(allowed)


def build_runner(symbols: tuple[str, ...], strategy_path: Path, *, progress: bool):
    """按本次实验配置装配场景，禁止报告快照与实际执行配置分叉。"""
    return ScenarioRunner(symbols, config_path=strategy_path, progress=progress)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--equity", type=float, default=300.0)
    parser.add_argument("--symbols", default=None,
                        help="逗号分隔的品种子集(默认用 live.json 的 symbols_core);"
                             "仅过滤本次回放,不改配置")
    parser.add_argument("--config", type=Path, default=None,
                        help="策略配置路径(默认 config/strategy.json);用于不改生产配置的实验")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.equity) or args.equity <= 0:
        parser.error("--equity must be finite and positive")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    strategy_path = ((args.config or (ROOT / "config/strategy.json")).resolve())
    live_path = ROOT / "config/live.json"
    live = json.loads(live_path.read_text(encoding="utf-8"))
    risk = live["risk"]
    if live.get("symbols_core"):
        allowed = tuple(live["symbols_core"])
    else:
        contracts_doc = json.loads((ROOT / "data/contracts.json").read_text(encoding="utf-8"))
        avail_1h = {p.name[:-7] for p in (ROOT / "data/raw").glob("*_1h.csv")}
        allowed = tuple(sorted(s for s in contracts_doc.get("entries", {}) if s in avail_1h))
    try:
        chosen, filtered = select_symbols(args.symbols, allowed)
    except ValueError as exc:
        parser.error(str(exc))
    runner = build_runner(chosen, strategy_path, progress=True)
    settings = runner.settings
    assert risk["dynamic_risk_tiers"] == settings.raw["small_account_compounding"]["dynamic_risk_tiers"]
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "equity0": args.equity, "symbols": list(runner.market.symbols),
        "symbols_config": list(allowed), "symbols_filtered": filtered,
        "risk": risk, "scenarios": {},
        "hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in (strategy_path, live_path, ROOT / "data/manifest.json", ROOT / "data/contracts.json")},
        "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / "src/market_operator").glob("*.py"))},
        "working_tree_status": subprocess.check_output(
            ["git", "status", "--short"], cwd=ROOT, text=True),
        "limitations": [
            "Production canonical campaigns, not a tick-by-tick execution of live_runner.",
            (f"Symbol subset {list(chosen)} replayed, but capacity/portfolio risk is no longer "
             f"shared with the configured universe {list(allowed)}; this is a standalone "
             f"single-portfolio run, NOT a per-symbol decomposition of the four-symbol run."
             if filtered else
             "Fixed four-symbol universe; no historical dynamic universe, outages or API failures."),
            "Live daily-loss/order-rate circuit breakers are not simulated by run_campaigns.",
            *(["Historical funding rates are loaded for the whole configured universe; "
               "only the traded subset consumes them."] if filtered else []),
            "Slippage is embedded in fill prices; do not deduct slippage_cost twice.",
            "Tail removal is attribution of original trades, not a counterfactual recompounding replay.",
            f"dev/test independently start at {args.equity:g} USDT; their PnL is not additive to full.",
        ],
    }
    (out / "strategy.snapshot.json").write_bytes(strategy_path.read_bytes())
    (out / "live.snapshot.json").write_bytes(live_path.read_bytes())
    for segment, multiplier in (("full", 1.0), ("dev", 1.0), ("test", 1.0), ("full", 1.5)):
        key = f"{segment}_{multiplier:g}x"
        print(f"Running {key}", flush=True)
        costs = settings.costs
        # Broker applies the multiplier to fees/slippage/constant funding once.
        # Historical funding is scaled explicitly, matching ExecutionCosts.scaled.
        funding = costs.scaled(multiplier).funding_rates
        result = run_campaigns(
            runner.frames, segment=segment, dev_frac=settings.dev_frac,
            equity0=args.equity, risk_frac=risk["risk_frac"],
            leverage_cap=risk["leverage_cap"], taker_fee=risk["taker_fee"],
            maker_fee=risk["maker_fee"], slippage_per_side=risk["slippage_per_side"],
            funding_per_8h=costs.funding_per_8h, cost_multiplier=multiplier,
            funding_rates=funding, slippage_by_symbol=costs.slippage_by_symbol,
            contracts=runner.contracts, params=settings.structure, n_fill=settings.n_fill,
            portfolio_cap_frac=risk["portfolio_cap_frac"],
            tf_cap_by_tf=risk["tf_cap_by_tf"], dynamic_risk_tiers=risk["dynamic_risk_tiers"],
            capacity_mode=settings.capacity_mode, arm=settings.arm,
            router_params=settings.router, router_params_by_tf=dict(settings.router_by_timeframe),
            enable_mic=True,
        )
        trades = [t for t in result.trades if t["in_segment_open"]]
        closed = [t for t in trades if t["closed"]]
        rows = result.equity_rows
        final = rows[-1]
        peak, max_dd, max_dd_usdt = args.equity, 0.0, 0.0
        for row in rows:
            peak = max(peak, row[1])
            max_dd = max(max_dd, (peak - row[1]) / peak)
            max_dd_usdt = max(max_dd_usdt, peak - row[1])
        cash_pnl = sum(t["net_pnl"] for t in result.trades)
        assert math.isclose(final[2] - args.equity, cash_pnl, abs_tol=1e-6)
        assert math.isclose(final[1], final[2] + final[3] - final[4], abs_tol=1e-6)
        for trade in closed:
            trade["exit_reason_code"] = (trade.get("reason_exit") or "").split("|", 1)[0]
        buckets = {}
        for field in ("tf", "symbol", "event_id", "direction", "entry_mode", "exit_reason_code"):
            buckets[field] = {str(v): trade_stats([t for t in closed if t.get(field) == v])
                              for v in sorted({t.get(field) for t in closed}, key=str)}
        month_ends = {}
        for row in rows:
            month_ends[utc(row[0])[:7]] = row[1]
        monthly, previous = [], args.equity
        for month, equity in month_ends.items():
            monthly.append({"month": month, "equity_end": equity,
                            "pnl": equity - previous,
                            "return_frac": equity / previous - 1})
            previous = equity
        summary = {
            "start_utc": utc(rows[0][0]), "end_utc": utc(final[0]),
            "equity_end": final[1], "cash_end": final[2], "unrealized_end": final[3],
            "unsettled_funding_end": final[4],
            "equity_profit": final[1] - args.equity,
            "return_frac": final[1] / args.equity - 1,
            "multiple": final[1] / args.equity,
            "max_drawdown_frac": max_dd, "max_drawdown_usdt": max_dd_usdt,
            "min_equity": min(r[1] for r in rows), "max_equity": max(r[1] for r in rows),
            "n_opened": len(trades), "n_open_end": len(trades) - len(closed),
            "closed": trade_stats(closed), "concentration": concentration(closed),
            "streaks": streaks(closed), "buckets": buckets,
            "fees": sum(t["fees"] for t in trades),
            "funding": sum(t["funding"] for t in trades),
            "slippage_embedded": sum(t["slippage_cost"] for t in trades),
            "holding_hours_mean": statistics.mean(t["hold_hours"] for t in closed) if closed else None,
            "holding_hours_median": statistics.median(t["hold_hours"] for t in closed) if closed else None,
            "capacity_skips": len(result.cap_skips), "counters": result.counters,
            "monthly_full_execution_window": monthly,
            "segment_bounds": result.seg_bounds,
            "standard_metrics": compute_metrics(result),
        }
        report["scenarios"][key] = summary
        write_csv(out / f"{key}.trades.csv", result.trades)
        write_csv(out / f"{key}.equity.csv", [dict(zip(
            ("ts", "equity", "cash", "unrealized", "funding", "open_positions"), r)) for r in rows])
        write_csv(out / f"{key}.cap_skips.csv", result.cap_skips)
        write_csv(out / f"{key}.claim_events.csv", result.claim_events)
        write_csv(out / f"{key}.top10pct.csv", sorted(closed, key=lambda t: t["net_pnl"], reverse=True)[:math.ceil(len(closed) * 0.1)])
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{key}: equity={final[1]:.6f}, closed={len(closed)}, dd={max_dd:.4%}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())