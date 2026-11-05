"""补充：方向与持仓期实际价格是否一致；迟滞的价格成本。"""
import csv, json, statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [r for r in csv.DictReader((HERE / "trade_ledger.csv").open(encoding="utf-8-sig"))]
for r in rows:
    r["r"] = float(r["net"]) / float(r["risk"])
    r["px"] = float(r["px_move"])
ok = [r for r in rows if r["px"] > 0]
bad = [r for r in rows if r["px"] <= 0]
print("direction matched price move:", len(ok), "/", len(rows),
      "mean R", round(st.mean(r["r"] for r in ok), 3), "net", round(sum(float(r["net"]) for r in ok), 2))
print("direction against price move:", len(bad), "mean R", round(st.mean(r["r"] for r in bad), 3),
      "net", round(sum(float(r["net"]) for r in bad), 2))

el = json.loads((HERE / "elements.json").read_text(encoding="utf-8"))
# 迟滞的价格成本：dist_from_ext_r * risk_pct
trades = []
raw = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
       if r["in_segment_open"] == "True" and r["closed"] == "True"]
cost = {}
for tf in ("2h", "4h", "12h", "1d"):
    g = [r for r in raw if r["tf"] == tf]
    vals = []
    for r in g:
        entry, inv = float(r["entry"]), float(r["invalid"])
        vals.append(abs(entry - inv) / entry)
    cost[tf] = {"n": len(g), "risk_pct_median": round(st.median(vals) * 100, 2)}
print("stop distance % of price:", json.dumps(cost))
(HERE / "elements_extra.json").write_text(json.dumps({
    "px_match": {"n": len(ok), "mean_r": round(st.mean(r["r"] for r in ok), 3),
                 "net": round(sum(float(r["net"]) for r in ok), 2)},
    "px_against": {"n": len(bad), "mean_r": round(st.mean(r["r"] for r in bad), 3),
                   "net": round(sum(float(r["net"]) for r in bad), 2)},
    "stop_distance_pct": cost,
}, ensure_ascii=False, indent=2), encoding="utf-8")
