"""Offline A/B for real-time profit protection on the clean production chain.

No live client, no orders, no production writes. The baseline is the checked-in
`config/strategy.json` (HEAD). Each variant only overrides position-management
fields; entries, routing, costs and capacity are unchanged.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.scenario import ScenarioRunner  # noqa: E402


def metrics(out):
    trades = out.closed_trades
    peak = 104.0
    dd = 0.0
    dd_pct = 0.0
    for _, equity, *_ in out.equity_rows:
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        dd_pct = max(dd_pct, (peak - equity) / peak if peak > 0 else 0.0)
    touched = [t for t in trades
               if t.get("target_1") is not None
               and t.get("mfe_r") is not None
               and abs(t["entry"] - t["invalid"]) > 0
               and t["mfe_r"] >= abs(t["target_1"] - t["entry"])
               / abs(t["entry"] - t["invalid"])]
    return {
        "closed": len(trades),
        "pnl_net": sum(t["net_pnl"] for t in trades),
        "ending_equity": out.equity_rows[-1][1],
        "max_dd_usdt": dd,
        "max_dd_pct": dd_pct,
        "target_reached_proxy_n": len(touched),
        "target_reached_then_net_loss_proxy_n": sum(t["net_pnl"] < 0 for t in touched),
        "exit_reasons": dict(Counter(str(t["reason_exit"]).split("|")[0]
                                     for t in trades)),
        "equity_start_utc": datetime.fromtimestamp(
            out.equity_rows[0][0] / 1000, timezone.utc).isoformat(),
        "equity_end_utc": datetime.fromtimestamp(
            out.equity_rows[-1][0] / 1000, timezone.utc).isoformat(),
    }


def variants(base):
    return {
        # HEAD: 2H close arming, 2H clock.
        "baseline": base,
        # 15m execution grid, close arming: proxy for the recommended
        # "15m closed-bar arming + quote ratchet" clock. Not pure execution:
        # the negative frac changes its arming close from 2H to 15m.
        "cadence_close_15m": replace(
            base, exit_exec=True, exit_exec_trail=False),
        # 15m grid, touch arming: the more aggressive alternative; rejected.
        "cadence_touch_15m": replace(
            base, exit_exec=True, exit_exec_trail=False,
            reject_opp_lock_frac=0.25),
        # Q2 candidate: only exit when an ACCEPT premise is falsified by a
        # close back inside the broken box edge (no blanket reverse exit).
        "accept_reenter": replace(base, accept_reenter_box_exit=True),
        "accept_reenter_15m": replace(
            base, accept_reenter_box_exit=True,
            exit_exec=True, exit_exec_trail=False),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", default="dev")
    ap.add_argument("--costs", default="1.0")
    ap.add_argument("--variants", default="")
    ap.add_argument("--output", default=str(
        ROOT / "reports" / "profit_protection_20261103" / "summary.json"))
    args = ap.parse_args()

    runner = ScenarioRunner(symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"),
                            progress=False)
    all_variants = variants(runner.settings.arm)
    wanted = ([v.strip() for v in args.variants.split(",") if v.strip()]
              or list(all_variants))
    segments = [s.strip() for s in args.segments.split(",") if s.strip()]
    costs = [float(c) for c in args.costs.split(",") if c.strip()]

    rows = []
    for name in wanted:
        arm = all_variants[name]
        for segment in segments:
            for cost in costs:
                out = runner.run(segment=segment, cost_multiplier=cost,
                                 equity0=104.0, arm=arm)
                row = {"variant": name, "segment": segment, "cost": cost,
                       **metrics(out)}
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "note": ("Clean HEAD baseline. 15m OHLC execution; target/stop "
                 "proxies use whole execution bars, not tick-level causality. "
                 "cadence_* variants change the arming close to 15m and are "
                 "not the same as live negative-frac + quote."),
        "rows": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
