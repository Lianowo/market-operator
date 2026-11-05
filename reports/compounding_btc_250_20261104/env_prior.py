"""环境补充：进场前 5/10 天的实际价格方向 vs 交易方向。"""
import bisect, csv, json, statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
bars = []
with (ROOT / "data/raw/BTCUSDT_1d.csv").open(encoding="utf-8") as fh:
    for r in csv.DictReader(fh):
        bars.append((int(r["ts"]), float(r["close"])))
bars.sort()
tsv = [b[0] for b in bars]
close = [b[1] for b in bars]

rows = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
        if r["in_segment_open"] == "True" and r["closed"] == "True"]
res = {}
for look in (5, 10):
    aligned, against = [], []
    for r in rows:
        ts = int(r["open_ts"])
        i = bisect.bisect_right(tsv, ts) - 1
        if i - look < 0:
            continue
        prior = close[i] / close[i - look] - 1
        d = int(r["direction"])
        net = float(r["net_pnl"]); risk = float(r["risk_usdt"])
        item = {"r": net / risk, "net": net, "ret": prior}
        (aligned if (prior > 0) == (d > 0) else against).append(item)
    def s(g):
        return {"n": len(g), "net": round(sum(x["net"] for x in g), 2),
                "mean_r": round(st.mean(x["r"] for x in g), 3),
                "winrate": round(sum(1 for x in g if x["net"] > 0) / len(g), 3)}
    res[f"prior_{look}d"] = {"aligned": s(aligned), "against": s(against)}
(HERE / "env_prior.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(res, ensure_ascii=False, indent=2))
