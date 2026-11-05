"""到目标之后有没有及时止盈（只读，用战役周期 bar 近似执行）。

对 REJECT：目标 = 箱体对侧沿（target_1）。找出第一根触及目标的 bar，
再看最终平仓价相对目标的差、以及触及后到平仓还花了多久。
对 ACCEPT：没有固定目标，改看第一次到 1R / 2R / 3R 之后是否守住。
"""
from __future__ import annotations

import bisect
import csv
import json
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TF_MS = {"2h": 2 * 3600_000, "4h": 4 * 3600_000, "12h": 12 * 3600_000, "1d": 24 * 3600_000}


def load_bars(tf):
    rows = []
    with (ROOT / "data" / "raw" / f"BTCUSDT_{tf}.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append((int(r["ts"]), float(r["high"]), float(r["low"])))
    rows.sort()
    return rows, [b[0] for b in rows]


BARS = {tf: load_bars(tf) for tf in TF_MS}
rows = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
        if r["in_segment_open"] == "True" and r["closed"] == "True"]


def first_touch(tf, d, entry, risk_px, level_r, start_ts, end_ts):
    bars, idx = BARS[tf]
    i = bisect.bisect_left(idx, start_ts)
    while i < len(bars) and bars[i][0] <= end_ts:
        hi, lo = bars[i][1], bars[i][2]
        fav = (hi - entry) if d > 0 else (entry - lo)
        if fav / risk_px >= level_r:
            return i, bars[i][0]
        i += 1
    return None, None


out = {"reject": {}, "accept": {}}
rej = [r for r in rows if r["target_1"]]
acc = [r for r in rows if not r["target_1"]]

touched, gave_back, kept = [], [], []
for r in rej:
    tf = r["tf"]; d = int(r["direction"])
    entry, inv = float(r["entry"]), float(r["invalid"])
    risk_px = abs(entry - inv)
    rmult = float(r["r_mult_planned"])
    touch_i, touch_ts = first_touch(tf, d, entry, risk_px, rmult,
                                    int(r["open_ts"]), int(r["exit_ts"]))
    exit_r = d * (float(r["exit_price"]) - entry) / risk_px
    if touch_i is None:
        continue
    bars, idx = BARS[tf]
    touched.append({"r": exit_r, "rmult": rmult, "tf": tf,
                    "bars_to_touch": touch_i - bisect.bisect_left(idx, int(r["open_ts"])),
                    "hours_touch_to_exit": (int(r["exit_ts"]) - touch_ts) / 3_600_000})
    (kept if exit_r >= rmult - 0.05 else gave_back).append(
        {"exit_r": exit_r, "rmult": rmult, "risk": float(r["risk_usdt"])})

out["reject"] = {
    "n": len(rej), "touched_target": len(touched),
    "touched_share": round(len(touched) / len(rej), 3),
    "bars_to_touch_median": round(st.median(x["bars_to_touch"] for x in touched), 1),
    "hours_touch_to_exit_median": round(st.median(x["hours_touch_to_exit"] for x in touched), 1),
    "bars_touch_to_exit_median": round(st.median(
        x["hours_touch_to_exit"] / {"2h": 2, "4h": 4, "12h": 12, "1d": 24}[x["tf"]]
        for x in touched), 1),
    "exit_ge_target": len(kept), "exit_below_target": len(gave_back),
    "exit_below_target_share": round(len(gave_back) / len(touched), 3),
    "gave_back_mean_exit_r": round(st.mean(x["exit_r"] for x in gave_back), 3) if gave_back else None,
    "gave_back_shortfall_usdt": round(sum((x["rmult"] - x["exit_r"]) * x["risk"]
                                          for x in gave_back), 2) if gave_back else None,
}

# ACCEPT：看 1R/2R/3R 的"触及后守住"
acc_stats = {"n": len(acc)}
for level in (1, 2, 3):
    hit, hold, shortfall = [], [], 0.0
    for r in acc:
        tf = r["tf"]; d = int(r["direction"])
        entry, inv = float(r["entry"]), float(r["invalid"])
        risk_px = abs(entry - inv)
        i, _ = first_touch(tf, d, entry, risk_px, level, int(r["open_ts"]), int(r["exit_ts"]))
        if i is None:
            continue
        exit_r = d * (float(r["exit_price"]) - entry) / risk_px
        hit.append(exit_r)
        if exit_r >= level:
            hold.append(exit_r)
        else:
            shortfall += (level - exit_r) * float(r["risk_usdt"])
    acc_stats[f"touched_{level}R"] = len(hit)
    acc_stats[f"held_{level}R"] = len(hold)
    acc_stats[f"held_{level}R_share"] = round(len(hold) / len(hit), 3) if hit else None
    acc_stats[f"mean_exit_r_after_{level}R"] = round(st.mean(hit), 2) if hit else None
    acc_stats[f"shortfall_usdt_vs_{level}R"] = round(shortfall, 2)
out["accept"] = acc_stats

out["notes"] = ["触及判定用战役周期 bar 的 high/low，比执行层 15m/1h 粗，"
                "所以『触及目标』是保守下界",
                "exit_ge_target 用 0.05R 容差"]
(HERE / "exit_path.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok", out["reject"]["touched_target"], out["accept"].get("touched_2R"))
