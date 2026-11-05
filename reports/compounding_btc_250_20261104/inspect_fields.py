"""字段结构检查：target/invalid/entry 与 plan 文本。"""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
rows = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
        if r["in_segment_open"] == "True"]
for r in rows[:8]:
    print({k: r[k] for k in ("trade_id", "direction", "tf", "event_id", "regime", "entry",
                             "invalid", "target_1", "r_mult_planned", "route", "l_star",
                             "weak_bars", "e6_margin", "e6_impulse")})
    print("  open:", r["reason_open"][:220])
    print("  exit:", r["reason_exit"][:160])
missing = [r["trade_id"] for r in rows if not r["r_mult_planned"]]
print("empty r_mult_planned:", len(missing), missing[:10])
print("empty invalid:", sum(1 for r in rows if not r["invalid"]),
      "empty target:", sum(1 for r in rows if not r["target_1"]))
