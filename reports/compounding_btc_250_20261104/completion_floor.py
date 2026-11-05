"""反事实：主张完成（触及对侧沿）后，把防线抬到"完成价"，而不是继续用回吐预算。"""
import bisect
import csv
import json
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TF_H = {"2h": 2, "4h": 4, "12h": 12, "1d": 24}


def load(tf):
    rows = []
    with (ROOT / "data" / "raw" / f"BTCUSDT_{tf}.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append((int(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]),
                         float(r["close"])))
    rows.sort()
    return rows, [b[0] for b in rows]


BARS = {tf: load(tf) for tf in TF_H}
rows = [r for r in csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig"))
        if r["in_segment_open"] == "True" and r["closed"] == "True"]
rej = [r for r in rows if r["target_1"]]

sim = []
for r in rej:
    tf = r["tf"]; d = int(r["direction"])
    bars, idx = BARS[tf]
    i = bisect.bisect_left(idx, int(r["open_ts"]))
    entry = float(r["entry"]); invalid = float(r["invalid"])
    risk_px = abs(entry - invalid)
    rmult = float(r["r_mult_planned"])
    exit_ts = int(r["exit_ts"])
    touch = None
    while i < len(bars) and bars[i][0] <= exit_ts:
        fav = (bars[i][2] - entry) if d > 0 else (entry - bars[i][3])
        if fav / risk_px >= rmult:
            touch = i
            break
        i += 1
    if touch is None:
        continue
    # 完成价 = entry + d*rmult*risk（目标价），完成后的防线 = 该价
    level = entry + d * rmult * risk_px
    stopped = False
    for j in range(touch + 1, len(bars)):
        if bars[j][0] > exit_ts:
            break
        back = bars[j][3] <= level if d > 0 else bars[j][2] >= level
        if back:
            stopped = True
            break
    r_actual = float(r["r_multiple"])
    sim.append({"r_actual": r_actual, "r_floor": rmult if stopped else r_actual,
                "stopped": stopped, "rmult": rmult})

out = {
    "n_touched": len(sim),
    "floor_triggered": sum(1 for x in sim if x["stopped"]),
    "sum_r_actual": round(sum(x["r_actual"] for x in sim), 2),
    "sum_r_with_floor": round(sum(x["r_floor"] for x in sim), 2),
    "delta_r": round(sum(x["r_floor"] - x["r_actual"] for x in sim), 2),
    "winners_kept": sum(1 for x in sim if not x["stopped"]),
    "winners_kept_sum_r": round(sum(x["r_actual"] for x in sim if not x["stopped"]), 2),
}
print(json.dumps(out, ensure_ascii=False, indent=2))
(HERE / "claim_completion_floor.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
