"""BTC 价格路径 vs 策略逐单：为什么没有把趋势滚成资金（只读）。

输入：data/raw/BTCUSDT_1d.csv + reports/compounding_btc_250_20261104/full_1x.trades.csv
输出：btc_path.json / trade_ledger.csv / 控制台摘要
"""
from __future__ import annotations

import csv
import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TZ = timezone.utc


def fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, TZ).strftime("%Y-%m-%d %H:%M")


def month_of(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, TZ).strftime("%Y-%m")


# ---------------------------------------------------------------- 价格路径
candles = []
with (ROOT / "data/raw/BTCUSDT_1d.csv").open(encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        ts = int(row["ts"])
        if ts < 1728950400000:      # 2024-10-15 00:00 UTC，与回放窗口对齐
            continue
        candles.append({"ts": ts, "o": float(row["open"]), "h": float(row["high"]),
                        "l": float(row["low"]), "c": float(row["close"])})
candles.sort(key=lambda x: x["ts"])
start, end = candles[0], candles[-1]

monthly = {}
for c in candles:
    m = monthly.setdefault(month_of(c["ts"]),
                           {"open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"]})
    m["high"] = max(m["high"], c["h"])
    m["low"] = min(m["low"], c["l"])
    m["close"] = c["c"]
months = [{"month": k, **v, "ret": round(v["close"] / v["open"] - 1, 4)}
          for k, v in monthly.items()]

# zigzag 波段（阈值 12%，收盘价口径，仅作描述）
THRESH = 0.12
legs = []
anchor_i, anchor_p = 0, candles[0]["c"]
direction = 1
ext_i, ext_p = 0, candles[0]["c"]
for i, c in enumerate(candles):
    p = c["c"]
    if direction == 1:
        if p > ext_p:
            ext_i, ext_p = i, p
        elif p <= ext_p * (1 - THRESH):
            legs.append({"from": candles[anchor_i]["ts"], "to": candles[ext_i]["ts"],
                         "from_px": anchor_p, "to_px": ext_p, "dir": 1,
                         "move": round(ext_p / anchor_p - 1, 4)})
            anchor_i, anchor_p = ext_i, ext_p
            direction, ext_i, ext_p = -1, i, p
    else:
        if p < ext_p:
            ext_i, ext_p = i, p
        elif p >= ext_p * (1 + THRESH):
            legs.append({"from": candles[anchor_i]["ts"], "to": candles[ext_i]["ts"],
                         "from_px": anchor_p, "to_px": ext_p, "dir": -1,
                         "move": round(ext_p / anchor_p - 1, 4)})
            anchor_i, anchor_p = ext_i, ext_p
            direction, ext_i, ext_p = 1, i, p
legs.append({"from": candles[anchor_i]["ts"], "to": candles[ext_i]["ts"], "from_px": anchor_p,
             "to_px": ext_p, "dir": direction,
             "move": round(ext_p / anchor_p - 1, 4)})
for leg in legs:
    leg["from_dt"] = fmt(leg["from"])[:10]
    leg["to_dt"] = fmt(leg["to"])[:10]

# ---------------------------------------------------------------- 逐单
raw = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
       if r["in_segment_open"] == "True" and r["closed"] == "True"]

trades = []
for r in raw:
    d = int(r["direction"])
    entry, exit_px = float(r["entry"]), float(r["exit_price"])
    net, risk = float(r["net_pnl"]), float(r["risk_usdt"])
    trades.append({
        "id": r["trade_id"], "dir": d, "tf": r["tf"], "event": r["event_id"],
        "mode": r["entry_mode"], "exit": r["exit_reason_code"],
        "open_ts": int(r["open_ts"]), "close_ts": int(r["exit_ts"]),
        "open_dt": fmt(int(r["open_ts"])), "close_dt": fmt(int(r["exit_ts"])),
        "entry": entry, "exit_px": exit_px, "net": round(net, 2), "risk": risk,
        "r": net / risk if risk else 0.0, "gross": float(r["gross_pnl"]),
        "fee": float(r["fees"]), "fund": float(r["funding"]),
        "mfe": float(r["mfe_r"]), "mae": float(r["mae_r"]),
        "hold": float(r["hold_hours"]), "notional": float(r["notional"]),
        "px_move": round(d * (exit_px / entry - 1), 6),
    })
trades.sort(key=lambda t: t["open_ts"])

for leg in legs:
    inside = [t for t in trades if leg["from"] <= t["open_ts"] <= leg["to"]]
    leg.update({
        "n": len(inside), "net": round(sum(t["net"] for t in inside), 2),
        "longs": sum(1 for t in inside if t["dir"] == 1),
        "shorts": sum(1 for t in inside if t["dir"] == -1),
        "aligned": sum(1 for t in inside if t["dir"] == leg["dir"]),
        "mean_r": round(st.mean(t["r"] for t in inside), 3) if inside else None,
    })

up_months = {m["month"] for m in months if m["ret"] > 0}
trades_up = [t for t in trades if month_of(t["open_ts"]) in up_months]
total_hours = (end["ts"] - start["ts"]) / 3_600_000
long_h = sum(t["hold"] for t in trades if t["dir"] == 1)
short_h = sum(t["hold"] for t in trades if t["dir"] == -1)
sum_mfe = sum(t["mfe"] for t in trades)
sum_r = sum(t["r"] for t in trades)
left = [t for t in trades if t["mfe"] >= 2 and t["r"] < 1]
given_back = sum((t["mfe"] - max(t["r"], 0)) * t["risk"] for t in trades if t["mfe"] > max(t["r"], 0))

# MFE 捕获桶
buckets = [(-99, 0), (0, 0.5), (0.5, 1), (1, 2), (2, 3), (3, 99)]
mfe_capture = []
for lo, hi in buckets:
    g = [t for t in trades if lo <= t["mfe"] < hi]
    if g:
        mfe_capture.append({"mfe_bin": f"[{lo},{hi})", "n": len(g),
                            "realised_mean_r": round(st.mean(t["r"] for t in g), 3),
                            "capture": round(st.mean(t["r"] for t in g) / st.mean(t["mfe"] for t in g), 3)
                            if st.mean(t["mfe"] for t in g) else None,
                            "net": round(sum(t["net"] for t in g), 2)})

# 每月：BTC 收益 vs 策略净额 / 方向
monthly_strategy = {}
for m in months:
    g = [t for t in trades if month_of(t["open_ts"]) == m["month"]]
    monthly_strategy[m["month"]] = {
        "btc_ret": m["ret"], "btc_close": m["close"],
        "n": len(g), "net": round(sum(t["net"] for t in g), 2),
        "longs": sum(1 for t in g if t["dir"] == 1), "shorts": sum(1 for t in g if t["dir"] == -1),
        "mean_r": round(st.mean(t["r"] for t in g), 3) if g else None,
    }

by_exit = {}
for t in trades:
    by_exit.setdefault(t["exit"], []).append(t)
by_exit = {k: {"n": len(v), "net": round(sum(x["net"] for x in v), 2),
               "mean_r": round(st.mean(x["r"] for x in v), 3),
               "mean_mfe": round(st.mean(x["mfe"] for x in v), 2),
               "mean_hold_h": round(st.mean(x["hold"] for x in v), 1)}
           for k, v in by_exit.items()}

ranked = sorted(trades, key=lambda t: t["net"], reverse=True)
extremes = {
    "top10": [{k: t[k] for k in ("id", "open_dt", "close_dt", "dir", "tf", "entry", "exit_px",
                                 "net", "r", "mfe", "hold", "exit")} for t in ranked[:10]],
    "bottom10": [{k: t[k] for k in ("id", "open_dt", "close_dt", "dir", "tf", "entry", "exit_px",
                                    "net", "r", "mfe", "hold", "exit")} for t in ranked[-10:]],
}


out = {
    "price": {
        "from": fmt(start["ts"]), "to": fmt(end["ts"]),
        "open": start["o"], "close": end["c"],
        "buy_hold": round(end["c"] / start["o"] - 1, 4),
        "high": max(c["h"] for c in candles), "low": min(c["l"] for c in candles),
        "high_dt": fmt(max(candles, key=lambda c: c["h"])["ts"]),
        "low_dt": fmt(min(candles, key=lambda c: c["l"])["ts"]),
        "days": len(candles),
        "up_months": sum(1 for m in months if m["ret"] > 0),
        "down_months": sum(1 for m in months if m["ret"] < 0),
    },
    "exposure": {
        "total_hours": round(total_hours, 1),
        "long_hours": round(long_h, 1), "short_hours": round(short_h, 1),
        "in_market_frac": round((long_h + short_h) / total_hours, 4),
        "long_frac": round(long_h / total_hours, 4),
        "short_frac": round(short_h / total_hours, 4),
        "trades": len(trades),
        "trades_per_month": round(len(trades) / (len(candles) / 30.4), 2),
    },
    "capture": {
        "sum_r": round(sum_r, 1), "sum_mfe_r": round(sum_mfe, 1),
        "realised_over_mfe": round(sum_r / sum_mfe, 3) if sum_mfe else None,
        "trades_mfe_ge2_but_r_lt1": len(left),
        "their_net": round(sum(t["net"] for t in left), 2),
        "value_given_back_usdt": round(given_back, 2),
    },
    "direction": {
        "long": {"n": sum(1 for t in trades if t["dir"] == 1),
                 "net": round(sum(t["net"] for t in trades if t["dir"] == 1), 2),
                 "mean_r": round(st.mean(t["r"] for t in trades if t["dir"] == 1), 3),
                 "winrate": round(sum(1 for t in trades if t["dir"] == 1 and t["net"] > 0) /
                                  max(1, sum(1 for t in trades if t["dir"] == 1)), 3)},
        "short": {"n": sum(1 for t in trades if t["dir"] == -1),
                  "net": round(sum(t["net"] for t in trades if t["dir"] == -1), 2),
                  "mean_r": round(st.mean(t["r"] for t in trades if t["dir"] == -1), 3),
                  "winrate": round(sum(1 for t in trades if t["dir"] == -1 and t["net"] > 0) /
                                   max(1, sum(1 for t in trades if t["dir"] == -1)), 3)},
        "in_up_months": {"n": len(trades_up),
                         "net": round(sum(t["net"] for t in trades_up), 2),
                         "long_n": sum(1 for t in trades_up if t["dir"] == 1),
                         "short_n": sum(1 for t in trades_up if t["dir"] == -1)},
    },
    "legs": legs,
    "months": months,
    "monthly_strategy": monthly_strategy,
    "mfe_capture": mfe_capture,
    "by_exit": by_exit,
    "extremes": extremes,
}
print(json.dumps(out, ensure_ascii=False, indent=2))
(HERE / "btc_path.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

with (HERE / "trade_ledger.csv").open("w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(trades[0]))
    w.writeheader()
    w.writerows(trades)

