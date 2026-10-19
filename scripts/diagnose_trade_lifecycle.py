"""Diagnose causal opportunity lifecycle, *without* selecting or tuning on test.

This is an explicitly NEW research variant, NOT a replacement for frozen v5
artifacts. Writes JSON only under diagnostics/, never reports/backtest_v5.*.
Historical test was reused in v1-v5 and is not independent OOS.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data
from market_operator.backtest import compute_metrics, prepare_cognition
from market_operator.backtest_v4 import build_exec_frame, alignment_issues_ci
from market_operator.backtest_v5 import run_backtest_v5
from market_operator.opportunity import OpportunityRouter
from market_operator.structure import Params
from scripts.run_backtest_v5 import load_contracts_cache


def main() -> None:
    cfg_path = ROOT / "config" / "v5.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    params = Params.from_json(cfg_path)
    symbols = cfg["symbols"]
    specs, source = load_contracts_cache(symbols)
    frames = {}
    for sym in symbols:
        df_1h, df_15m = data.load(sym), data.load(sym, "15m")
        if data.validate(df_15m, step_ms=900000):
            raise AssertionError(f"bad input: {sym}")
        cog = prepare_cognition(df_1h, params, sym, enable_e7=True)
        frame = build_exec_frame(cog, df_15m)
        assert not alignment_issues_ci(
            cog.bars, [(int(x.ts),) for x in df_15m.itertuples(index=False)],
            cfg["dev_frac"]), f"boundary mismatch {sym}"
        frames[sym] = frame
        print(f"COGNITION {sym}: {len(cog.bars)} hours", flush=True)

    outcome = {
        "variant": "observed_break -> E7 candidate -> distinct-close confirmation",
        "baseline_reference": "frozen main v5 (do not overwrite)",
        "sample_note": "ALL historical test rows previously exposed >=7 times; diagnostic only",
        "execution": "15m structural stop-market, gap-open adverse, stop before maker",
        "extra_entry_filters": "none; no F1, fixed R minimum or strength thresholds",
        "runs": {}
    }
    for segment, cost in [("full", 1.0), ("test", 1.0), ("test", 2.0)]:
        router = OpportunityRouter()
        res = run_backtest_v5(
            frames, segment=segment, dev_frac=float(cfg["dev_frac"]),
            equity0=300.0, risk_frac=float(cfg["risk_frac"]),
            leverage_cap=float(cfg["leverage_cap"]),
            taker_fee=float(cfg["taker_fee"]),
            slippage_per_side=float(cfg["slippage_per_side"]),
            funding_per_8h=float(cfg["funding_per_8h_per_side"]),
            cost_multiplier=cost, contracts=specs, params=params,
            n_fill=int(cfg["n_fill"]), maker_fee=float(cfg["maker_fee"]),
            router=router, protective_execution=True,
            # No new arbitrary portfolio filter in strategy attribution;
            # broker still enforces shared gross leverage.
            portfolio_stop_risk_frac=None,
        )
        metrics = compute_metrics(res)
        key = f"{segment}@{cost}x"
        # Economic attribution by pre-defined MARKET THESIS, not fitted gates.
        raw_trades = [t for t in res.trades if t["in_segment_open"]]
        by_thesis = {}
        for source in sorted({str(t.get("event_id")) for t in raw_trades}):
            group = [t for t in raw_trades if str(t.get("event_id")) == source]
            closed = [t for t in group if t["closed"]]
            net = sum(float(t["net_pnl"]) for t in group)
            by_thesis[source] = {
                "trades": len(group), "closed": len(closed),
                "net": net, "net_per_trade": net / len(group),
                "fees": sum(float(t["fees"]) for t in group),
                "funding": sum(float(t["funding"]) for t in group),
                "winrate": (sum(t["net_pnl"] > 0 for t in closed)
                            / len(closed)) if closed else None,
                "protective_exits": sum(
                    str(t.get("reason_exit") or "").startswith("PROTECTIVE_STOP|")
                    for t in closed),
            }
        outcome["runs"][key] = {
            "metrics": {name:metrics.get(name) for name in (
                "net_pnl","return_pct","n_trades","winrate",
                "profit_factor","expectancy_usdt","expectancy_r",
                "max_dd_pct")},
            "counters": res.counters,
            "by_symbol": metrics.get("by_symbol"),
            "n_closed": sum(1 for x in res.trades if x["closed"]),
            "by_trade_thesis": by_thesis,
            "n_rejected_hypotheses": router.counters.get("thesis_failed", 0),
            "n_unanchored_local_flips": router.counters.get("unanchored_flip", 0),
        }
        print("REPLAY", key, json.dumps(outcome["runs"][key], ensure_ascii=False),
              flush=True)
    out = ROOT / "diagnostics" / "trade-lifecycle-20261019.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(outcome,ensure_ascii=False,indent=2),encoding="utf-8")
    print("DIAGNOSTIC ONLY:", out, flush=True)


if __name__ == "__main__":
    main()
