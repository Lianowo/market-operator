"""生成逐单台账 markdown（只读）。"""
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
path = json.loads((HERE / "btc_path.json").read_text(encoding="utf-8"))
rows = list(csv.DictReader((HERE / "trade_ledger.csv").open(encoding="utf-8-sig")))

L = ["# BTC 逐单台账（150 笔已平仓，250 U 复利回放）", "",
     "列含义：开仓时间(UTC) / 方向 / 周期 / 事件 / 入场→出场 / 持仓h / R / MFE / MAE / 净额U / 出场。", ""]

cur = None
for r in rows:
    month = r["open_dt"][:7]
    if month != cur:
        if cur is not None:
            L.append("")
        L.append(f"## {month}")
        L.append("")
        L.append("| 开仓 | 方向 | 周期 | 事件 | 入场 → 出场 | 持仓h | R | MFE | MAE | 净额 | 出场 |")
        L.append("|---|---|---|---|---|---:|---:|---:|---:|---:|---|")
        cur = month
    L.append("| {open_dt} | {d} | {tf} | {ev} | {e} → {x} | {h:.0f} | {r:+.2f} | {mfe:+.2f} | {mae:+.2f} | {net:+.2f} | {exit} |".format(
        open_dt=r["open_dt"][5:16], d="多" if r["dir"] == "1" else "空", tf=r["tf"],
        ev=r["event"].replace("BREAKOUT_ACCEPTED", "ACCEPT").replace("LEVEL_REJECTED", "REJECT"),
        e=f"{float(r['entry']):.0f}", x=f"{float(r['exit_px']):.0f}",
        h=float(r["hold"]), r=float(r["r"]), mfe=float(r["mfe"]), mae=float(r["mae"]),
        net=float(r["net"]), exit=r["exit"].replace("PROTECTIVE_STOP", "STOP")))

L += ["", "## 段", "", "| 区间 | 方向 | 价格变动 | 笔数 | 做多/做空 | 顺势笔数 | mean R | 净额 |",
      "|---|---|---:|---:|---:|---:|---:|---:|"]
for leg in path["legs"]:
    L.append(f"| {leg['from_dt']} → {leg['to_dt']} | {'涨' if leg['dir']==1 else '跌'} | "
             f"{leg['move']:+.1%} | {leg['n']} | {leg['longs']}/{leg['shorts']} | {leg['aligned']} | "
             f"{leg['mean_r']} | {leg['net']:+.2f} |")

L += ["", "## 月", "", "| 月份 | BTC 月收益 | 笔数 | 多/空 | mean R | 策略净额 |",
      "|---|---:|---:|---:|---:|---:|"]
for m, s in path["monthly_strategy"].items():
    L.append(f"| {m} | {s['btc_ret']:+.2%} | {s['n']} | {s['longs']}/{s['shorts']} | "
             f"{s['mean_r']} | {s['net']:+.2f} |")

(HERE / "TRADE_LEDGER.md").write_text("\n".join(L) + "\n", encoding="utf-8")

C = ["# BTC 逐单速查（150 笔，按开仓时间）", "",
     "| # | 开仓(UTC) | 方向 | 周期 | 事件 | R | 净额 | MFE | 出场 |",
     "|---:|---|---|---|---|---:|---:|---:|---|"]
for i, r in enumerate(rows, 1):
    C.append("| {i} | {t} | {d} | {tf} | {ev} | {r:+.2f} | {net:+.2f} | {mfe:+.2f} | {exit} |".format(
        i=i, t=r["open_dt"][5:16], d="多" if r["dir"] == "1" else "空", tf=r["tf"],
        ev=r["event"].replace("BREAKOUT_ACCEPTED", "ACCEPT").replace("LEVEL_REJECTED", "REJECT"),
        r=float(r["r"]), net=float(r["net"]), mfe=float(r["mfe"]),
        exit=r["exit"].replace("PROTECTIVE_STOP", "STOP")))
(HERE / "TRADES_COMPACT.md").write_text("\n".join(C) + "\n", encoding="utf-8")
print("written", len(L), len(C), "lines")
