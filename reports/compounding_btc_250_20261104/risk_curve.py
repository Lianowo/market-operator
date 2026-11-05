"""不同风险率下的几何增长（同一批 R，只做算术复利）。"""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
R = [float(r["net"]) / float(r["risk"])
     for r in csv.DictReader((HERE / "trade_ledger.csv").open(encoding="utf-8-sig"))]
for w in (0.02, 0.03, 0.04, 0.05, 0.075, 0.10, 0.15, 0.20):
    G = 1.0
    for x in R:
        G *= 1 + w * x
    print(f"risk={w:.0%}  multiple={G:.2f}x  per_trade={(G ** (1 / len(R)) - 1):.3%}")
