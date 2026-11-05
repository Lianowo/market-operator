"""Read-only analytics over the 250 USDT compounding replay artifacts."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
report = json.loads((HERE / "report.json").read_text(encoding="utf-8"))
scenario = report["scenarios"]["full_1x"]
equity0 = report["equity0"]


def utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


with (HERE / "full_1x.equity.csv").open(encoding="utf-8-sig", newline="") as fh:
    equity_rows = [(int(r["ts"]), float(r["equity"])) for r in csv.DictReader(fh)]

with (HERE / "full_1x.trades.csv").open(encoding="utf-8-sig", newline="") as fh:
    trades = [r for r in csv.DictReader(fh) if r["in_segment_open"] == "True"]
closed = [r for r in trades if r["closed"] == "True"]

# --- milestones -------------------------------------------------------------
milestones = []
for level in (250, 500, 1000, 2500, 5000, 10000, 20000, 25000):
    hit = next((ts for ts, eq in equity_rows if eq >= level), None)
    milestones.append({
        "level": level,
        "first_reached": utc(hit) if hit else None,
        "trading_days": round((hit - equity_rows[0][0]) / 86_400_000, 1) if hit else None,
        "return_from_start": round(level / equity0 - 1, 4) if hit else None,
    })

# --- drawdown path ----------------------------------------------------------
peak_ts, peak_eq = equity_rows[0][0], equity_rows[0][1]
worst = {"dd": 0.0}
max_dd_usdt = 0.0
underwater_samples = 0
longest_underwater = (0, None, None)
uw_start = None
for ts, eq in equity_rows:
    if eq >= peak_eq:
        if uw_start is not None:
            if ts - uw_start > longest_underwater[0]:
                longest_underwater = (ts - uw_start, uw_start, ts)
            uw_start = None
        peak_ts, peak_eq = ts, eq
    else:
        underwater_samples += 1
        if uw_start is None:
            uw_start = ts
        dd = (peak_eq - eq) / peak_eq
        if dd > worst["dd"]:
            worst = {"dd": dd, "peak_ts": peak_ts, "peak_eq": peak_eq, "trough_ts": ts, "trough_eq": eq}
        max_dd_usdt = max(max_dd_usdt, peak_eq - eq)
if uw_start is not None and equity_rows[-1][0] - uw_start > longest_underwater[0]:
    longest_underwater = (equity_rows[-1][0] - uw_start, uw_start, equity_rows[-1][0])

recovery = next((ts for ts, eq in equity_rows if ts > worst["trough_ts"] and eq >= worst["peak_eq"]), None)

# --- sizing / leverage over time -------------------------------------------
dated = sorted(trades, key=lambda r: (int(r["open_ts"]), r["trade_id"]))
quarter = max(1, len(dated) // 4)
quarters = []
for i in range(4):
    chunk = dated[i * quarter:(i + 1) * quarter] if i < 3 else dated[3 * quarter:]
    if not chunk:
        continue
    quarters.append({
        "quartile": i + 1,
        "window": f"{utc(int(chunk[0]['open_ts']))} -> {utc(int(chunk[-1]['open_ts']))}",
        "n": len(chunk),
        "avg_risk_usdt": round(mean(float(r["risk_usdt"]) for r in chunk), 2),
        "avg_notional": round(mean(float(r["notional"]) for r in chunk), 2),
        "avg_leverage": round(mean(float(r["used_leverage"]) for r in chunk), 2),
        "avg_net": round(mean(float(r["net_pnl"]) for r in chunk), 2),
    })

# --- cost / gross decomposition (closed trades only) -----------------------
gross = sum(float(r["gross_pnl"]) for r in closed)
fees = sum(float(r["fees"]) for r in closed)
funding = sum(float(r["funding"]) for r in closed)
net = sum(float(r["net_pnl"]) for r in closed)
rs = [float(r["r_multiple"]) for r in closed]
risk_deployed = sum(float(r["risk_usdt"]) for r in trades) / equity0

# risk deployed measured against the equity in force when the trade opened
equity_at = {}
for ts, eq in equity_rows:
    equity_at[ts] = eq
eq_index = [ts for ts, _ in equity_rows]


def equity_on(ts: int) -> float:
    lo, hi = 0, len(eq_index) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if eq_index[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return equity_at[eq_index[lo]]


risk_deployed_live = sum(float(r["risk_usdt"]) / equity_on(int(r["open_ts"])) for r in trades)

# --- share of time per dynamic risk tier ------------------------------------
tiers = [(1000.0, 0.05), (5000.0, 0.04), (10000.0, 0.03), (50000.0, 0.02), (1e9, 0.01)]
tier_samples = {f"<= {int(bound):,} U ({frac:.0%})": 0 for bound, frac in tiers}
for _, eq in equity_rows:
    for bound, frac in tiers:
        if eq <= bound:
            tier_samples[f"<= {int(bound):,} U ({frac:.0%})"] += 1
            break
total_samples = len(equity_rows)

# --- monthly aggregation ----------------------------------------------------
monthly = scenario["monthly_full_execution_window"]
positive = sum(1 for m in monthly if m["return_frac"] > 0)
compounded = 1.0
for m in monthly:
    compounded *= 1 + m["return_frac"]

summary = {
    "equity_start": equity0,
    "equity_end": scenario["equity_end"],
    "multiple": scenario["multiple"],
    "calendar_days": round((equity_rows[-1][0] - equity_rows[0][0]) / 86_400_000, 1),
    "cagr_like": round(
        (scenario["equity_end"] / equity0) **
        (365 / ((equity_rows[-1][0] - equity_rows[0][0]) / 86_400_000)) - 1, 4),
    "milestones": milestones,
    "worst_drawdown": {
        "frac": round(worst["dd"], 6),
        "peak": f"{utc(worst['peak_ts'])} = {worst['peak_eq']:.2f}",
        "trough": f"{utc(worst['trough_ts'])} = {worst['trough_eq']:.2f}",
        "usdt": round(max_dd_usdt, 2),
        "recovery": utc(recovery) if recovery else None,
        "recovery_days": round((recovery - worst["trough_ts"]) / 86_400_000, 1) if recovery else None,
    },
    "underwater": {
        "sample_share": round(underwater_samples / len(equity_rows), 4),
        "longest_days": round(longest_underwater[0] / 86_400_000, 1),
        "longest_window": f"{utc(longest_underwater[1])} -> {utc(longest_underwater[2])}" if longest_underwater[1] else None,
    },
    "costs": {
        "gross_pnl": round(gross, 2),
        "fees": round(fees, 2),
        "funding": round(funding, 2),
        "net_closed": round(net, 2),
        "cost_of_gross": round((fees + funding) / gross, 4) if gross else None,
        "fees_over_equity_end": round(fees / scenario["equity_end"], 4),
    },
    "sizing_by_quartile": quarters,
    "monthly": {
        "n_months": len(monthly),
        "positive_months": positive,
        "negative_months": len(monthly) - positive,
        "compounded_product": round(compounded, 4),
        "best": max(monthly, key=lambda m: m["return_frac"]),
        "worst": min(monthly, key=lambda m: m["return_frac"]),
        "median_return": round(median(m["return_frac"] for m in monthly), 4),
    },
    "quality": {
        "mean_r": round(mean(rs), 4),
        "median_r": round(median(rs), 4),
        "risk_deployed_x_equity0": round(risk_deployed, 1),
        "risk_deployed_x_live_equity": round(risk_deployed_live, 1),
        "tier_time_share": {k: round(v / total_samples, 4) for k, v in tier_samples.items()},
    },
    "reconciliation": {
        "closed_net": round(net, 2),
        "open_trades": {
            "n": len(trades) - len(closed),
            "fees": round(sum(float(r["fees"]) for r in trades if r not in closed), 2),
            "realized_net": round(sum(float(r["net_pnl"]) for r in trades if r not in closed), 2),
        },
        "unrealized_end": round(scenario["unrealized_end"], 2),
        "unsettled_funding_end": round(scenario["unsettled_funding_end"], 2),
    },
    "n_unrealized_note": scenario["n_open_end"],
    "capacity_skips": scenario["capacity_skips"],
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
(HERE / "analysis.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

lines = ["<!-- generated by analyze.py -->", "", "| 里程碑 | 首次到达 (UTC) | 距起点天数 | 累计收益 |", "|---|---|---:|---:|"]
for m in milestones:
    if m["first_reached"] is None:
        lines.append(f"| {m['level']:,} U | 未达到 | – | – |")
        continue
    lines.append(f"| {m['level']:,} U | {m['first_reached']} | {m['trading_days']:.1f} | {m['return_from_start']:+.0%} |")
lines += ["", "| 月份 | 期末权益 | 当月盈亏 | 当月收益率 |", "|---|---:|---:|---:|"]
for m in monthly:
    lines.append(f"| {m['month']} | {m['equity_end']:.2f} | {m['pnl']:+.2f} | {m['return_frac']:+.2%} |")
(HERE / "tables.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

