"""L2 算术差：1–2R 桶若捕获 50% MFE（不是重跑）。"""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [r for r in csv.DictReader((HERE / "trade_ledger.csv").open(encoding="utf-8-sig"))]
g = [r for r in rows if 1 <= float(r["mfe"]) < 2]
delta = sum((0.5 * float(r["mfe"]) - float(r["r"])) * float(r["risk"]) for r in g)
print("n", len(g), "actual_net", round(sum(float(r["net"]) for r in g), 2),
      "if_50pct_mfe", round(sum(0.5 * float(r["mfe"]) * float(r["risk"]) for r in g), 2),
      "delta", round(delta, 2))
