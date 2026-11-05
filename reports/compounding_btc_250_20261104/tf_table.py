"""按周期的综合表：R、止损宽度、成本占 R、MFE 捕获、箱体剩余空间。"""
import csv, json, statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
raw = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
       if r["in_segment_open"] == "True" and r["closed"] == "True"]
lines = ["| 周期 | 笔数 | 净额 | mean R | 止损宽(中位) | 费用/风险 | 计划剩余空间(中位) | MFE 捕获 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|"]
data = {}
for tf in ("2h", "4h", "12h", "1d"):
    g = [r for r in raw if r["tf"] == tf]
    risk_pct = st.median(abs(float(r["entry"]) - float(r["invalid"])) / float(r["entry"]) for r in g) * 100
    cost_ratio = sum(float(r["fees"]) for r in g) / sum(float(r["risk_usdt"]) for r in g)
    room = [float(r["r_mult_planned"]) for r in g if r["r_mult_planned"]]
    cap = sum(float(r["r_multiple"]) for r in g) / sum(float(r["mfe_r"]) for r in g)
    row = {"n": len(g), "net": round(sum(float(r["net_pnl"]) for r in g), 2),
           "mean_r": round(st.mean(float(r["r_multiple"]) for r in g), 3),
           "stop_pct": round(risk_pct, 2), "cost_over_risk": round(cost_ratio, 3),
           "room_median": round(st.median(room), 2) if room else None,
           "capture": round(cap, 3)}
    data[tf] = row
    lines.append(f"| {tf} | {row['n']} | {row['net']:+.2f} | {row['mean_r']:+.3f} | {row['stop_pct']:.2f}% | "
                 f"{row['cost_over_risk']:.1%} | {row['room_median']} | {row['capture']:.1%} |")
(HERE / "tf_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
(HERE / "tf_table.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n".join(lines))
