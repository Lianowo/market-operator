"""复利速率分解：算术期望 vs 几何增长（波动拖累）。只读。"""
import csv, json, math, statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [r for r in csv.DictReader((HERE / "trade_ledger.csv").open(encoding="utf-8-sig"))]
R = [float(r["r"]) for r in rows]
risk_frac = [float(r["risk"]) for r in rows]

# 单标的全程都在 <=1000 档：risk_frac = 5%
w = 0.05
trade_ret = [w * x for x in R]
G = 1.0
for x in trade_ret:
    G *= 1 + x
mu = st.mean(trade_ret)
sd = st.stdev(trade_ret)
geo = G ** (1 / len(R)) - 1
print(json.dumps({
    "n": len(R),
    "mean_R": round(st.mean(R), 4),
    "sd_R": round(st.stdev(R), 3),
    "mean_trade_ret": round(mu, 6),
    "sd_trade_ret": round(sd, 6),
    "arith_multiple_150": round((1 + mu) ** len(R), 3),
    "geo_multiple_150": round(G, 3),
    "geo_per_trade": round(geo, 6),
    "drag_ratio": round(geo / mu, 3) if mu else None,
    "approx_drag_term": round(0.5 * sd * sd / (1 + mu), 6),
    "actual_multiple": round(445.87785638857184 / 250, 4),
    "win_rate": round(sum(1 for x in R if x > 0) / len(R), 3),
    "p95_R": round(sorted(R)[int(len(R) * 0.95)], 2),
    "max_R": round(max(R), 2),
    "min_R": round(min(R), 2),
    "sum_R_positive": round(sum(x for x in R if x > 0), 1),
    "sum_R_negative": round(sum(x for x in R if x < 0), 1),
}, indent=2))
