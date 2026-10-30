"""Controlled risk-only comparison using the unchanged production campaign core."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
from market_operator.scenario import ScenarioRunner
from market_operator.campaign import run_campaigns
from replay_compounding import trade_stats, concentration

runner = ScenarioRunner(("BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT"))
s = runner.settings
live = json.loads((ROOT / "config/live.json").read_text(encoding="utf-8"))["risk"]
old = [[250, .04], [600, .03], [1500, .02], [1e9, .01]]
variants = [
    ("standard_0.75pct", s.risk_frac, s.portfolio_cap_frac, dict(s.tf_cap_by_timeframe), None),
    ("old_dynamic_tiers", live["risk_frac"], live["portfolio_cap_frac"], live["tf_cap_by_tf"], old),
    ("new_dynamic_tiers", live["risk_frac"], live["portfolio_cap_frac"], live["tf_cap_by_tf"], live["dynamic_risk_tiers"]),
]
report = {}
for name, rf, cap, tfcap, tiers in variants:
    print("Running", name, flush=True)
    c = s.costs
    result = run_campaigns(
        runner.frames, segment="full", dev_frac=s.dev_frac, equity0=300,
        risk_frac=rf, leverage_cap=s.leverage_cap, taker_fee=c.taker_fee,
        maker_fee=c.maker_fee, slippage_per_side=c.slippage_per_side,
        funding_per_8h=c.funding_per_8h, cost_multiplier=1,
        funding_rates=c.funding_rates, slippage_by_symbol=c.slippage_by_symbol,
        contracts=runner.contracts, params=s.structure, n_fill=s.n_fill,
        portfolio_cap_frac=cap, tf_cap_by_tf=tfcap, dynamic_risk_tiers=tiers,
        capacity_mode=s.capacity_mode, arm=s.arm, router_params=s.router,
        router_params_by_tf=dict(s.router_by_timeframe), enable_mic=True)
    closed = [t for t in result.trades if t["closed"] and t["in_segment_open"]]
    peak, dd, dd_peak_ts, peak_ts, dd_trough_ts = 300, 0, None, None, None
    for row in result.equity_rows:
        if row[1] > peak:
            peak, peak_ts = row[1], row[0]
        drawdown = (peak - row[1]) / peak
        if drawdown > dd:
            dd, dd_peak_ts, dd_trough_ts = drawdown, peak_ts, row[0]
    report[name] = {
        "risk_frac": rf, "portfolio_cap": cap, "tf_cap": tfcap, "tiers": tiers,
        "equity_end": result.equity_rows[-1][1], "max_drawdown": dd,
        "dd_peak_ts": dd_peak_ts, "dd_trough_ts": dd_trough_ts,
        "closed": trade_stats(closed), "concentration": concentration(closed),
        "cap_skips": len(result.cap_skips),
    }
    print(json.dumps(report[name]), flush=True)
    Path(__file__).with_name("risk_comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")