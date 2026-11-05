"""核对正文引用：MFE≥2R 组合计、三笔决定、上涨段合计。"""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [r for r in csv.DictReader((HERE / "trade_ledger.csv").open(encoding="utf-8-sig"))]
for r in rows:
    r["r"] = float(r["net"]) / float(r["risk"])
big = [r for r in rows if float(r["mfe"]) >= 2]
small = [r for r in rows if float(r["mfe"]) < 2]
print("mfe>=2:", len(big), "sumR", round(sum(r["r"] for r in big), 2), "net", round(sum(float(r["net"]) for r in big), 2))
print("mfe<2 :", len(small), "sumR", round(sum(r["r"] for r in small), 2), "net", round(sum(float(r["net"]) for r in small), 2))
top3 = sorted(rows, key=lambda r: float(r["net"]), reverse=True)[:3]
print("top3", [(r["open_dt"], r["tf"], r["dir"], round(float(r["net"]), 2)) for r in top3],
      "sum", round(sum(float(r["net"]) for r in top3), 2))
print("all net", round(sum(float(r["net"]) for r in rows), 2))
