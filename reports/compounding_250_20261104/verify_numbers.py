"""核对 WHY_RESIDUAL.md 引用的关键数字（只读断言）。"""
import csv, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
        if r["in_segment_open"] == "True" and r["closed"] == "True"]
t = [{"net": float(r["net_pnl"]), "risk": float(r["risk_usdt"]), "gross": float(r["gross_pnl"])} for r in rows]
for x in t:
    x["r"] = x["net"] / x["risk"]
ordered = sorted(t, key=lambda x: x["net"], reverse=True)
k = __import__("math").ceil(len(ordered) * 0.1)
head, rest = ordered[:k], ordered[k:]

def net(g): return round(sum(x["net"] for x in g), 2)
def gross(g): return round(sum(x["gross"] for x in g), 2)

assert net(head) == 51653.54 and net(rest) == -29270.66
assert gross(rest) == -24387.74
full_stops = [x for x in t if x["r"] < -1]
assert len(full_stops) == 163 and round(sum(x["net"] for x in full_stops), 2) == -30144.62
assert round(sum(min(x["net"] + x["risk"], 0.0) for x in full_stops), 2) == -1697.01
loss_sum = sum(x["net"] for x in t if x["net"] < 0)
assert round(full_stops and sum(x["net"] for x in full_stops) / loss_sum, 4) == 0.5959
assert round(sum(x["risk"] for x in rest), 2) == 99094.13
assert round(sum(x["risk"] * x["r"] for x in rest) / sum(x["risk"] for x in rest), 4) == -0.2954
assert round(sum(12.5 * x["r"] for x in rest), 2) == -627.53
by_r = sorted(t, key=lambda x: x["r"], reverse=True)
rest_r = by_r[k:]
assert net(rest_r) == -21686.35
print("all assertions passed:",
      {"head_net": net(head), "rest_net": net(rest), "rest_gross": gross(rest),
       "full_stops": len(full_stops), "rest_risk": round(sum(x["risk"] for x in rest), 2),
       "rest_r_wtd": round(sum(x["risk"] * x["r"] for x in rest) / sum(x["risk"] for x in rest), 4)})
