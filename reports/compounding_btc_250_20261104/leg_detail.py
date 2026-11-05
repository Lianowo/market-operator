"""段内方向/捕获明细（只读）。"""
import csv, json, statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
legs = json.loads((HERE / "btc_path.json").read_text(encoding="utf-8"))["legs"]
rows = [r for r in csv.DictReader((HERE / "trade_ledger.csv").open(encoding="utf-8-sig"))]
for r in rows:
    r["r"] = float(r["net"]) / float(r["risk"])
    r["mfe"] = float(r["mfe"])

for leg in legs:
    inside = [r for r in rows if leg["from"] <= int(r["open_ts"]) <= leg["to"]]
    longs = [r for r in inside if r["dir"] == "1"]
    shorts = [r for r in inside if r["dir"] == "-1"]
    def s(g):
        return (len(g), round(sum(x["r"] for x in g), 2), round(st.mean(x["mfe"] for x in g), 2)) if g else (0, 0, 0)
    print(f"{leg['from_dt']}->{leg['to_dt']} {leg['move']:+.1%} n={len(inside)} "
          f"longs={s(longs)} shorts={s(shorts)} net={leg['net']:+.2f} "
          f"stops={sum(1 for x in inside if x['exit']=='PROTECTIVE_STOP')}")

up = [l for l in legs if l["dir"] == 1]
dn = [l for l in legs if l["dir"] == -1]
print("UP legs net", round(sum(l["net"] for l in up), 2), "n", sum(l["n"] for l in up),
      "down legs net", round(sum(l["net"] for l in dn), 2), "n", sum(l["n"] for l in dn))

