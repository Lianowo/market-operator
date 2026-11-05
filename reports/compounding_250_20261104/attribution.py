"""只读归因：为什么 250U 复利回放里"其余 90%"合计为负。

口径全部来自 full_1x 的逐笔账本；不重跑、不改配置。
"""
from __future__ import annotations

import csv
import json
import math
import statistics as st
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
report = json.loads((HERE / "report.json").read_text(encoding="utf-8"))
sc = report["scenarios"]["full_1x"]

with (HERE / "full_1x.trades.csv").open(encoding="utf-8-sig", newline="") as fh:
    rows = [r for r in csv.DictReader(fh) if r["in_segment_open"] == "True"]
closed = [r for r in rows if r["closed"] == "True"]

with (HERE / "full_1x.equity.csv").open(encoding="utf-8-sig", newline="") as fh:
    eq = [(int(r["ts"]), float(r["equity"])) for r in csv.DictReader(fh)]
eq_ts = [t for t, _ in eq]


def equity_on(ts: int) -> float:
    lo, hi = 0, len(eq_ts) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if eq_ts[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return eq[lo][1]


def enrich(r: dict) -> dict:
    net = float(r["net_pnl"])
    risk = float(r["risk_usdt"])
    return {
        "id": r["trade_id"], "sym": r["symbol"], "dir": int(r["direction"]),
        "tf": r["tf"], "event": r["event_id"], "mode": r["entry_mode"],
        "exit": r["exit_reason_code"], "open": int(r["open_ts"]), "exit_ts": int(r["exit_ts"]),
        "net": net, "gross": float(r["gross_pnl"]), "fee": float(r["fees"]),
        "fund": float(r["funding"]), "risk": risk, "notional": float(r["notional"]),
        "r": net / risk if risk else 0.0, "mfe": float(r["mfe_r"]), "mae": float(r["mae_r"]),
        "hold": float(r["hold_hours"]), "eq": equity_on(int(r["open_ts"])),
        "wait": float(r["fill_wait_bars"] or 0),
    }


trades = [enrich(r) for r in closed]
ordered = sorted(trades, key=lambda t: t["net"], reverse=True)
k = math.ceil(len(ordered) * 0.1)
head, rest = ordered[:k], ordered[k:]


def stats(g: list) -> dict:
    if not g:
        return {"n": 0}
    wins = [t["net"] for t in g if t["net"] > 0]
    losses = [t["net"] for t in g if t["net"] < 0]
    return {
        "n": len(g),
        "gross": round(sum(t["gross"] for t in g), 2),
        "fees": round(sum(t["fee"] for t in g), 2),
        "funding": round(sum(t["fund"] for t in g), 2),
        "net": round(sum(t["net"] for t in g), 2),
        "wins_n": len(wins), "wins_sum": round(sum(wins), 2), "avg_win": round(st.mean(wins), 2) if wins else None,
        "loss_n": len(losses), "loss_sum": round(sum(losses), 2), "avg_loss": round(st.mean(losses), 2) if losses else None,
        "mean_r": round(st.mean(t["r"] for t in g), 4),
        "median_r": round(st.median(t["r"] for t in g), 4),
        "sum_r": round(sum(t["r"] for t in g), 2),
        "avg_notional": round(st.mean(t["notional"] for t in g), 2),
        "avg_risk": round(st.mean(t["risk"] for t in g), 2),
        "avg_eq_at_entry": round(st.mean(t["eq"] for t in g), 2),
        "avg_hold_h": round(st.mean(t["hold"] for t in g), 1),
        "first_open": min(t["open"] for t in g), "last_open": max(t["open"] for t in g),
    }


def flat_stats(g: list) -> dict:
    """把每笔按固定 12.5U 风险（250U × 5%）折算，回答"同样的交易在定仓下是多少"。"""
    return {
        "n": len(g),
        "net_at_const_250": round(sum(12.5 * t["r"] for t in g), 2),
    }


def by(key: str, g: list) -> dict:
    groups = defaultdict(list)
    for t in g:
        groups[t[key]].append(t)
    return {str(kk): stats(v) for kk, v in sorted(groups.items(), key=lambda kv: str(kv[0]))}


out = {
    "head": stats(head), "rest": stats(rest),
    "head_flat": flat_stats(head), "rest_flat": flat_stats(rest),
    "rest_buckets": {
        "year": by("year", [dict(t, year=str(__import__("datetime").datetime.utcfromtimestamp(t["open"] / 1000).year)) for t in rest]),
        "tf": by("tf", rest), "symbol": by("sym", rest), "event": by("event", rest),
        "direction": by("dir", rest), "entry_mode": by("mode", rest), "exit": by("exit", rest),
    },
    "head_exit_mix": by("exit", head),
}
head_eq = round(st.mean(t["eq"] for t in head), 2)
rest_eq = round(st.mean(t["eq"] for t in rest), 2)
out["trade_size_timing"] = {
    "head_avg_eq_at_entry": head_eq,
    "rest_avg_eq_at_entry": rest_eq,
    "equity_multiple_head_vs_rest": round(head_eq / rest_eq, 2),
}
# --- 追加：分布形状 / 时间分段 / 定仓反事实 ---
def gross_win_loss(g: list) -> dict:
    gw = [t["gross"] for t in g if t["gross"] > 0]
    gl = [t["gross"] for t in g if t["gross"] < 0]
    return {"gross_wins_n": len(gw), "gross_wins_sum": round(sum(gw), 2),
            "gross_loss_n": len(gl), "gross_loss_sum": round(sum(gl), 2),
            "avg_gross_win": round(st.mean(gw), 2) if gw else None,
            "avg_gross_loss": round(st.mean(gl), 2) if gl else None,
            "breakeven_winrate": round((st.mean([-x for x in gl]) /
                                        (st.mean([-x for x in gl]) + st.mean(gw))), 4)
            if gw and gl else None}


def mfe_buckets(g: list) -> dict:
    buckets = {"mfe<0.25R": 0, "0.25-0.5R": 0, "0.5-1R": 0, ">=1R": 0}
    for t in g:
        m = t["mfe"]
        key = "mfe<0.25R" if m < 0.25 else "0.25-0.5R" if m < 0.5 else "0.5-1R" if m < 1 else ">=1R"
        buckets[key] += 1
    return {k: {"n": v, "share": round(v / len(g), 4)} for k, v in buckets.items()}


def year_of(t: dict) -> str:
    from datetime import datetime, timezone
    return str(datetime.fromtimestamp(t["open"] / 1000, timezone.utc).year)


rest_years = defaultdict(list)
for t in rest:
    rest_years[year_of(t)].append(t)
rest_by_year = {}
for y, g in sorted(rest_years.items()):
    s = stats(g)
    gw = gross_win_loss(g)
    s.update(gw)
    s["stop_share"] = round(sum(1 for t in g if t["exit"] == "PROTECTIVE_STOP") / len(g), 4)
    s["mfe_ge_1r_share"] = round(sum(1 for t in g if t["mfe"] >= 1) / len(g), 4)
    s["net_const_250"] = round(sum(12.5 * t["r"] for t in g), 2)
    rest_by_year[y] = s

split = len(ordered) // 2
by_r = sorted(trades, key=lambda t: t["r"], reverse=True)
kk = math.ceil(len(by_r) * 0.1)
time_half = sorted(trades, key=lambda t: t["open"])

out["rest_detail"] = {
    "gross_win_loss": gross_win_loss(rest),
    "mfe_buckets": mfe_buckets(rest),
    "by_year": rest_by_year,
    "stop_vs_nonstop": {
        "stop": stats([t for t in rest if t["exit"] == "PROTECTIVE_STOP"]),
        "non_stop": stats([t for t in rest if t["exit"] != "PROTECTIVE_STOP"]),
    },
    "head_size": {"min_net": round(min(t["net"] for t in head), 2),
                  "max_net": round(max(t["net"] for t in head), 2)},
}
out["split_by_r_decile"] = {"top10pct": stats(by_r[:kk]), "rest90pct": stats(by_r[kk:])}
out["half_by_time"] = {"first_half": stats(time_half[:split]), "second_half": stats(time_half[split:])}

out["tail_shape"] = {
    "rest_worst10_sum": round(sum(t["net"] for t in sorted(rest, key=lambda x: x["net"])[:10]), 2),
    "rest_lt_minus1r": {
        "n": sum(1 for t in rest if t["r"] < -1),
        "net": round(sum(t["net"] for t in rest if t["r"] < -1), 2),
    },
    "rest_gt_2r": {
        "n": sum(1 for t in rest if t["r"] >= 2),
        "net": round(sum(t["net"] for t in rest if t["r"] >= 2), 2),
    },
    "months": {},
}

from datetime import datetime, timezone

for label, group in (("head", head), ("rest", rest)):
    buckets = defaultdict(list)
    for t in group:
        buckets[datetime.fromtimestamp(t["open"] / 1000, timezone.utc).strftime("%Y-%m")].append(t)
    out["tail_shape"]["months"][label] = {
        m: {"n": len(g), "net": round(sum(x["net"] for x in g), 2),
            "mean_r": round(st.mean(x["r"] for x in g), 3)}
        for m, g in sorted(buckets.items())
    }

print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
(HERE / "attribution.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
