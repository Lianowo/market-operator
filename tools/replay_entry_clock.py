"""Offline A/B for ACCEPT/REJECT confirmation clocks (clean HEAD chain).

No live client, no orders. Baseline = checked-in config/strategy.json.
Variants only change `n_a` (ACCEPT closes) or the trigger frame for 2h.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.scenario import (  # noqa: E402
    ScenarioRunner, build_frames)


def metrics(out):
    trades = out.closed_trades
    peak = 104.0
    dd = dd_pct = 0.0
    for _, equity, *_ in out.equity_rows:
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        dd_pct = max(dd_pct, (peak - equity) / peak if peak > 0 else 0.0)
    return {
        "closed": len(trades),
        "pnl_net": sum(t["net_pnl"] for t in trades),
        "ending_equity": out.equity_rows[-1][1],
        "max_dd_usdt": dd,
        "max_dd_pct": dd_pct,
        "exit_reasons": dict(Counter(str(t["reason_exit"]).split("|")[0]
                                     for t in trades)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", default="dev")
    ap.add_argument("--costs", default="1.0,1.5")
    ap.add_argument("--variants", default="")
    ap.add_argument("--output", default=str(
        ROOT / "reports" / "entry_clock_20261103" / "dev.json"))
    args = ap.parse_args()

    runner = ScenarioRunner(symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"),
                            progress=False)
    base = runner.settings
    base_trig = dict(base.trigger_seconds_by_timeframe)
    variants = {
        "baseline": (base.structure, base_trig),
        "n_a_2": (replace(base.structure, n_a=2), base_trig),
        "n_a_1": (replace(base.structure, n_a=1), base_trig),
        "trigger_2h_30m": (base.structure, {**base_trig, "2h": 1800}),
        "trigger_2h_30m_n_a_2": (
            replace(base.structure, n_a=2), {**base_trig, "2h": 1800}),
        "trigger_2h_30m_n_a_1": (
            replace(base.structure, n_a=1), {**base_trig, "2h": 1800}),
        # Conditional fast ACCEPT: weak first bars still wait for n_a=3.
        "fast_accept_050": (
            replace(base.structure, fast_accept_atr=0.50), base_trig),
        "fast_accept_075": (
            replace(base.structure, fast_accept_atr=0.75), base_trig),
        "fast_accept_100": (
            replace(base.structure, fast_accept_atr=1.00), base_trig),
    }
    segments = [s.strip() for s in args.segments.split(",") if s.strip()]
    costs = [float(c) for c in args.costs.split(",") if c.strip()]
    wanted = ([v.strip() for v in args.variants.split(",") if v.strip()]
              or list(variants))

    rows = []
    for name in wanted:
        structure, trig = variants[name]
        settings = replace(base, structure=structure,
                           trigger_seconds_by_timeframe=trig)
        runner.settings = settings
        runner.frames = build_frames(runner.market, settings, runner.timeframes)
        for segment in segments:
            for cost in costs:
                out = runner.run(segment=segment, cost_multiplier=cost,
                                 equity0=104.0)
                row = {"variant": name, "segment": segment, "cost": cost,
                       **metrics(out)}
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "note": ("Clean HEAD baseline. n_a is the ACCEPT consecutive-close "
                 "count; trigger_2h_30m observes 2h structure on 30m closes. "
                 "No limit-order fallback."),
        "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
