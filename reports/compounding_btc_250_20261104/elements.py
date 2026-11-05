"""四要素量化：环境 / 箱体位置 / 进场迟滞 / 退出迟滞（只读，BTC 150 笔）。

- 环境：`regime` 字段 + 持仓期间实际价格方向
- 箱体位置：REJECT 的 `target_1`(对侧沿) 与 `invalid`(止损) 给出箱体内位置与剩余空间
- 进场迟滞：信号 bar 到成交的时间、成交价距前 12 根极值的 R 距离
- 退出迟滞：是否触及计划目标、MFE 后的衰减 bar 数、实现 vs 计划
"""
from __future__ import annotations

import bisect
import csv
import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ROOT_DATA = ROOT / "data" / "raw"
TZ = timezone.utc
TF_MS = {"2h": 2 * 3600_000, "4h": 4 * 3600_000, "12h": 12 * 3600_000, "1d": 24 * 3600_000}


def load_bars(tf: str):
    rows = []
    with (ROOT_DATA / f"BTCUSDT_{tf}.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append((int(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]),
                         float(r["close"])))
    rows.sort()
    return rows


BARS = {tf: load_bars(tf) for tf in TF_MS}
IDX = {tf: [b[0] for b in BARS[tf]] for tf in TF_MS}

raw = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
       if r["in_segment_open"] == "True" and r["closed"] == "True"]

trades = []
for r in raw:
    tf = r["tf"]
    d = int(r["direction"])
    entry = float(r["entry"])
    invalid = float(r["invalid"])
    risk_px = abs(entry - invalid)
    target = float(r["target_1"]) if r["target_1"] else None
    rmult = float(r["r_mult_planned"]) if r["r_mult_planned"] else None
    signal_ts = int(r["open_decision_ts"])
    fill_ts = int(r["open_ts"])
    i = bisect.bisect_right(IDX[tf], signal_ts) - 1
    lo_i = max(0, i - 11)
    window = BARS[tf][lo_i:i + 1]
    if d == 1:
        low = min(b[3] for b in window)
        ext_i = lo_i + next(j for j, b in enumerate(window) if b[3] == low)
        dist_r = (entry - low) / risk_px
    else:
        high = max(b[2] for b in window)
        ext_i = lo_i + next(j for j, b in enumerate(window) if b[2] == high)
        dist_r = (high - entry) / risk_px
    net, risk = float(r["net_pnl"]), float(r["risk_usdt"])
    trades.append({
        "id": r["trade_id"], "dir": d, "tf": tf, "event": r["event_id"],
        "regime": r["regime"], "route": r["route"],
        "entry": entry, "invalid": invalid, "target": target, "rmult": rmult,
        "risk_px": risk_px, "risk_pct": risk_px / entry,
        "net": net, "r": net / risk if risk else 0.0,
        "mfe": float(r["mfe_r"]), "mae": float(r["mae_r"]),
        "hold_h": float(r["hold_hours"]),
        "hold_bars": float(r["hold_bars"]), "excursion_bars": float(r["excursion_bars"]),
        "entry_delay_h": (fill_ts - signal_ts) / 3_600_000,
        "exit_delay_h": (int(r["exit_ts"]) - int(r["close_decision_ts"])) / 3_600_000,
        "entry_delay_bars": (fill_ts - signal_ts) / TF_MS[tf],
        "dist_from_ext_r": dist_r,
        "bars_since_ext": i - ext_i,
        "open_dt": datetime.fromtimestamp(fill_ts / 1000, TZ).strftime("%Y-%m-%d %H:%M"),
    })

for t in trades:
    t["exit_delay_h"] = t["exit_delay_h"]
    t["reached_target"] = (t["rmult"] is not None and t["mfe"] >= t["rmult"] - 1e-9)
    t["counter_trend"] = ((t["regime"] == "TREND_UP" and t["dir"] == -1) or
                          (t["regime"] == "TREND_DOWN" and t["dir"] == 1))


def agg(g):
    if not g:
        return {"n": 0}
    return {"n": len(g), "net": round(sum(x["net"] for x in g), 2),
            "mean_r": round(st.mean(x["r"] for x in g), 3),
            "winrate": round(sum(1 for x in g if x["net"] > 0) / len(g), 3),
            "mean_mfe": round(st.mean(x["mfe"] for x in g), 2),
            "median_hold_h": round(st.median(x["hold_h"] for x in g), 1)}


def group(key):
    d = defaultdict(list)
    for t in trades:
        d[str(t[key])].append(t)
    return {k: agg(v) for k, v in sorted(d.items(), key=lambda kv: str(kv[0]))}


# 1) 环境
env = {
    "by_regime": group("regime"),
    "by_regime_x_dir": {},
    "counter_trend": agg([t for t in trades if t["counter_trend"]]),
    "with_trend": agg([t for t in trades if not t["counter_trend"]]),
}
for reg in ("TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION"):
    for d in (1, -1):
        g = [t for t in trades if t["regime"] == reg and t["dir"] == d]
        if g:
            env["by_regime_x_dir"][f"{reg}|{'long' if d == 1 else 'short'}"] = agg(g)

# 2) 箱体位置（REJECT 才有 target）
rej = [t for t in trades if t["rmult"] is not None]
for t in rej:
    t["box_frac"] = 1.0 / (1.0 + t["rmult"])      # 进场点距止损边在箱体内的比例
box = {
    "n": len(rej),
    "rmult_mean": round(st.mean(t["rmult"] for t in rej), 2),
    "rmult_median": round(st.median(t["rmult"] for t in rej), 2),
    "share_rmult_lt1": round(sum(1 for t in rej if t["rmult"] < 1) / len(rej), 3),
    "share_rmult_lt0p5": round(sum(1 for t in rej if t["rmult"] < 0.5) / len(rej), 3),
    "box_frac_median": round(st.median(t["box_frac"] for t in rej), 3),
    "by_rmult": {}, "by_tf": {},
}
for lo, hi in ((0, 0.5), (0.5, 1), (1, 1.5), (1.5, 2), (2, 9)):
    g = [t for t in rej if lo <= t["rmult"] < hi]
    if g:
        box["by_rmult"][f"{lo}-{hi}R"] = agg(g)
for tf in ("2h", "4h", "12h", "1d"):
    g = [t for t in rej if t["tf"] == tf]
    if g:
        box["by_tf"][tf] = {**agg(g), "rmult_median": round(st.median(t["rmult"] for t in g), 2),
                            "share_rmult_lt1": round(sum(1 for t in g if t["rmult"] < 1) / len(g), 3)}

# 3) 进场迟滞
lag = {"by_task_tf": {}, "by_dist": {}, "by_dist_x_tf": {}}
for tf in ("2h", "4h", "12h", "1d"):
    g = [t for t in trades if t["tf"] == tf]
    lag["by_task_tf"][tf] = {
        **agg(g),
        "entry_delay_h_median": round(st.median(t["entry_delay_h"] for t in g), 2),
        "dist_from_ext_r_median": round(st.median(t["dist_from_ext_r"] for t in g), 2),
        "bars_since_ext_median": round(st.median(t["bars_since_ext"] for t in g), 1),
    }
for lo, hi in ((0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 99)):
    g = [t for t in trades if lo <= t["dist_from_ext_r"] < hi]
    if g:
        lag["by_dist"][f"{lo}-{hi}R"] = agg(g)
for tf in ("2h", "4h"):
    for lo, hi in ((0, 0.5), (0.5, 1.0), (1.0, 99)):
        g = [t for t in trades if t["tf"] == tf and lo <= t["dist_from_ext_r"] < hi]
        if g:
            lag["by_dist_x_tf"][f"{tf}|{lo}-{hi}R"] = agg(g)

# 4) 退出迟滞
reached = [t for t in trades if t["reached_target"]]
exit_an = {
    "n_reject_with_target": len(rej),
    "reached_target_n": len(reached),
    "reached_target_share": round(len(reached) / len(rej), 3) if rej else None,
    "reached_target": agg(reached),
    "reached_but_nonpositive_n": sum(1 for t in reached if t["net"] <= 0),
    "reached_but_nonpositive": agg([t for t in reached if t["net"] <= 0]),
    "realised_minus_rmult_r": round(st.mean(t["r"] - t["rmult"] for t in reached), 3) if reached else None,
    "not_reached": agg([t for t in rej if not t["reached_target"]]),
    "exit_delay_h_median": round(st.median(t["exit_delay_h"] for t in trades), 2),
    "exit_delay_h_mean": round(st.mean(t["exit_delay_h"] for t in trades), 2),
    "by_event": {},
}
for ev in ("LEVEL_REJECTED", "BREAKOUT_ACCEPTED"):
    g = [t for t in trades if t["event"] == ev]
    if g:
        exit_an["by_event"][ev] = {**agg(g),
                                   "realised_over_mfe": round(sum(x["r"] for x in g) /
                                                              sum(x["mfe"] for x in g), 3),
                                   "share_mfe_ge2": round(sum(1 for x in g if x["mfe"] >= 2) / len(g), 3)}

# 5) 环境 × 箱体
cross = {}
for reg in ("TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION"):
    g = [t for t in rej if t["regime"] == reg]
    if g:
        cross[reg] = {"n": len(g), "rmult_median": round(st.median(t["rmult"] for t in g), 2),
                      "reached_share": round(sum(1 for t in g if t["reached_target"]) / len(g), 3),
                      **{k: v for k, v in agg(g).items() if k in ("net", "mean_r")}}

out = {"environment": env, "box": box, "entry_lag": lag, "exit_lag": exit_an,
       "regime_x_box": cross,
       "notes": ["dist_from_ext_r = 成交价距信号 bar 之前 12 根同周期 K 线极值的距离（R 计）",
                 "box_frac = 1/(1+r_mult)，进场点在箱体内的位置（0=止损边，1=对侧沿）",
                 "entry_delay_h = 信号 bar 收盘 → 成交；exit_delay_h = 平仓决策 → 成交",
                 "注意：excursion_bars 是执行 1h bar 计数，hold_bars 是战役周期 bar，单位不同，"
                 "本文件不使用两者之差"]}
(HERE / "elements.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("written elements.json", len(trades), "trades,", len(rej), "with target")

