"""R 形状与止损穿透：定位"其余 90%"亏损的物理来源（只读算术反事实）。"""
from __future__ import annotations

import csv
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
report = json.loads((HERE / "report.json").read_text(encoding="utf-8"))
with (HERE / "full_1x.trades.csv").open(encoding="utf-8-sig", newline="") as fh:
    rows = [r for r in csv.DictReader(fh)
            if r["in_segment_open"] == "True" and r["closed"] == "True"]

trades = [{
    "net": float(r["net_pnl"]), "risk": float(r["risk_usdt"]), "gross": float(r["gross_pnl"]),
    "r": float(r["net_pnl"]) / float(r["risk_usdt"]) if float(r["risk_usdt"]) else 0.0,
    "mae": float(r["mae_r"]), "mfe": float(r["mfe_r"]), "exit": r["exit_reason_code"],
    "tf": r["tf"], "sym": r["symbol"], "hold": float(r["hold_hours"]),
    "open": int(r["open_ts"]),
} for r in rows]

bins = [(-99, -2), (-2, -1.5), (-1.5, -1), (-1, -0.75), (-0.75, -0.5), (-0.5, -0.25),
        (-0.25, 0), (0, 0.25), (0.25, 0.5), (0.5, 1), (1, 2), (2, 3), (3, 99)]
shape = []
for lo, hi in bins:
    g = [t for t in trades if lo <= t["r"] < hi]
    if not g:
        continue
    shape.append({"bin": f"[{lo},{hi})", "n": len(g),
                  "net": round(sum(t["net"] for t in g), 2),
                  "share_of_trades": round(len(g) / len(trades), 4),
                  "avg_hold_h": round(st.mean(t["hold"] for t in g), 1)})

over = [t for t in trades if t["r"] < -1]
over_loss = sum(t["net"] for t in over)
beyond_1r = sum(t["net"] + t["risk"] for t in over)   # 超出计划风险的部分（负值）
under_1r = [t for t in trades if t["r"] >= -1]
mfe1_lost = [t for t in trades if t["mfe"] >= 1 and t["net"] < 0]

res = {
    "n": len(trades),
    "net_total": round(sum(t["net"] for t in trades), 2),
    "shape": shape,
    "overshoot": {
        "n_r_below_minus1": len(over),
        "share": round(len(over) / len(trades), 4),
        "net": round(over_loss, 2),
        "loss_beyond_1r": round(beyond_1r, 2),
        "avg_r": round(st.mean(t["r"] for t in over), 3),
        "median_r": round(st.median(t["r"] for t in over), 3),
        "worst_r": round(min(t["r"] for t in over), 3),
        "avg_mae_r": round(st.mean(t["mae"] for t in over), 3),
        "avg_hold_h": round(st.mean(t["hold"] for t in over), 1),
        "exit_mix": dict(sorted(
            ((k, sum(1 for t in over if t["exit"] == k)) for k in {t["exit"] for t in over}),
            key=lambda kv: -kv[1])),
        "tf_mix": dict(sorted(
            ((k, sum(1 for t in over if t["tf"] == k)) for k in {t["tf"] for t in over}),
            key=lambda kv: -kv[1])),
    },
    "everything_else": {
        "n_r_ge_minus1": len(under_1r),
        "net": round(sum(t["net"] for t in under_1r), 2),
        "r_ge_minus1_net_without_beyond": round(beyond_1r + sum(t["net"] for t in under_1r), 2),
    },
    "mfe_ge_1r_but_negative": {
        "n": len(mfe1_lost),
        "net": round(sum(t["net"] for t in mfe1_lost), 2),
        "share_of_trades": round(len(mfe1_lost) / len(trades), 4),
        "avg_mfe_r": round(st.mean(t["mfe"] for t in mfe1_lost), 2),
        "hold_h_median": round(st.median(t["hold"] for t in mfe1_lost), 1),
    },
    "counterfactual_cap_1r": {
        "note": "把每笔亏损截断在 -1R（不改仓位/不改路径）的算术上界，不是重跑结果",
        "net_if_capped": round(sum(max(t["net"], -t["risk"]) for t in trades), 2),
        "delta": round(sum(max(t["net"], -t["risk"]) for t in trades) - sum(t["net"] for t in trades), 2),
    },
}
# --- rest 视图（与 report 的 concentration 同定义） ---
import math
ordered = sorted(trades, key=lambda t: t["net"], reverse=True)
k = math.ceil(len(ordered) * 0.1)
for t in trades:
    t["g"] = "head" if id(t) in {id(x) for x in ordered[:k]} else "rest"
rest = [t for t in trades if t["g"] == "rest"]
res["rest_shape"] = [
    {"bin": f"[{lo},{hi})", "n": len(g), "net": round(sum(t["net"] for t in g), 2)}
    for lo, hi in bins
    if (g := [t for t in rest if lo <= t["r"] < hi])
]
res["rest_totals"] = {
    "n": len(rest), "net": round(sum(t["net"] for t in rest), 2),
    "mean_r": round(st.mean(t["r"] for t in rest), 4),
    "sum_risk": round(sum(t["risk"] for t in rest), 2),
    "sum_risk_times_r": round(sum(t["risk"] * t["r"] for t in rest), 2),
    "mean_risk_times_mean_r": round(st.mean(t["risk"] for t in rest) * st.mean(t["r"] for t in rest) * len(rest), 2),
}
for key in ("tf", "sym"):
    res.setdefault("rest_2026_by_" + key, {})
    vals = {t[key] for t in rest if t["open"] >= 1767225600000}
    for v in sorted(vals):
        g = [t for t in rest if t["open"] >= 1767225600000 and t[key] == v]
        res["rest_2026_by_" + key][v] = {
            "n": len(g), "net": round(sum(t["net"] for t in g), 2),
            "mean_r": round(st.mean(t["r"] for t in g), 3),
            "winrate": round(sum(1 for t in g if t["net"] > 0) / len(g), 3),
            "avg_risk": round(st.mean(t["risk"] for t in g), 1),
        }

# 用"按 R 排序"的十分位再切一次，检验上面的年度恶化是否是绝对 U 排序的选择性偏差
by_r = sorted(trades, key=lambda t: t["r"], reverse=True)
head_r = by_r[:k]
rest_r = by_r[k:]
from collections import defaultdict as _dd
from datetime import datetime as _dt, timezone as _tz
year_buckets = _dd(list)
for t in rest_r:
    year_buckets[str(_dt.fromtimestamp(t["open"] / 1000, _tz.utc).year)].append(t)
res["rest_by_r_decile_by_year"] = {
    y: {"n": len(g), "net": round(sum(x["net"] for x in g), 2),
        "mean_r": round(st.mean(x["r"] for x in g), 3),
        "avg_risk": round(st.mean(x["risk"] for x in g), 1),
        "winrate": round(sum(1 for x in g if x["net"] > 0) / len(g), 3)}
    for y, g in sorted(year_buckets.items())
}
res["head_by_r_decile"] = {
    "n": len(head_r), "net": round(sum(x["net"] for x in head_r), 2),
    "mean_r": round(st.mean(x["r"] for x in head_r), 3),
    "min_r": round(min(x["r"] for x in head_r), 3),
    "by_year": {
        y: {"n": len(g), "net": round(sum(x["net"] for x in g), 2)}
        for y, g in sorted({yy: [t for t in head_r if str(_dt.fromtimestamp(t["open"] / 1000, _tz.utc).year) == yy]
                            for yy in {str(_dt.fromtimestamp(t["open"] / 1000, _tz.utc).year) for t in head_r}}.items())
    },
}

print(json.dumps(res, ensure_ascii=False, indent=2))
(HERE / "rshape.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

(HERE / "rshape.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
