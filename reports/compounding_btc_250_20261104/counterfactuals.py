"""语义反事实（只读，非重跑）：
A. REJECT 若在"拒绝边沿"挂单成交（主张在穿刺时锚定边沿），而不是确认后市价追。
B. 退出若"到对侧沿即平"（主张完成即结束）vs 现有移动防线。
C. ACCEPT 若"到 +1R 后防线移到成本"（保本）vs 现有移动防线。
都用战役周期 bar 近似执行、同一出场价，明确标注为算术反事实。
"""
from __future__ import annotations

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
acc = [r for r in rows if not r["target_1"]]

# ---------------------------------------------------------------- A 边沿挂单
CLAIM_BARS = 4          # n_f + 1：主张观察窗（语义量，不是过滤参数）
edge_rows = []
for r in rej:
    tf = r["tf"]; d = int(r["direction"])
    bars, idx = BARS[tf]
    i = bisect.bisect_right(idx, int(r["open_decision_ts"])) - 1
    if i < 1:
        continue
    edge = min(bars[i][3], bars[i - 1][3]) if d > 0 else max(bars[i][2], bars[i - 1][2])
    invalid = float(r["invalid"])
    if (d > 0 and edge <= invalid) or (d < 0 and edge >= invalid):
        continue
    risk_edge = abs(edge - invalid)
    r_edge = d * (float(r["exit_price"]) - edge) / risk_edge
    filled_at = None
    for j in range(i + 1, min(i + 1 + CLAIM_BARS, len(bars))):
        if (bars[j][3] <= edge) if d > 0 else (bars[j][2] >= edge):
            filled_at = j
            break
    edge_rows.append({"tf": tf, "r_actual": float(r["r_multiple"]), "r_edge": r_edge,
                      "filled": filled_at is not None, "rmult": float(r["r_mult_planned"])})

filled = [x for x in edge_rows if x["filled"]]
A = {"n": len(edge_rows), "fill_rate": round(len(filled) / len(edge_rows), 3),
     "actual_sum_r_all": round(sum(x["r_actual"] for x in edge_rows), 2),
     "edge_sum_r_if_filled": round(sum(x["r_edge"] for x in filled), 2),
     "edge_mean_r_if_filled": round(st.mean(x["r_edge"] for x in filled), 3),
     "actual_mean_r_same_subset": round(st.mean(x["r_actual"] for x in filled), 3),
     "by_tf": {}}
for tf in ("2h", "4h", "12h", "1d"):
    g = [x for x in filled if x["tf"] == tf]
    all_tf = [x for x in edge_rows if x["tf"] == tf]
    if g:
        A["by_tf"][tf] = {"n_all": len(all_tf), "filled": len(g),
                          "fill_rate": round(len(g) / len(all_tf), 3),
                          "mean_r_actual": round(st.mean(x["r_actual"] for x in g), 3),
                          "mean_r_edge": round(st.mean(x["r_edge"] for x in g), 3)}

# ---------------------------------------------------------------- B 到对侧沿即平
touched = []
for r in rej:
    tf = r["tf"]; d = int(r["direction"])
    bars, idx = BARS[tf]
    i = bisect.bisect_left(idx, int(r["open_ts"]))
    entry, risk_px = float(r["entry"]), abs(float(r["entry"]) - float(r["invalid"]))
    rmult = float(r["r_mult_planned"])
    while i < len(bars) and bars[i][0] <= int(r["exit_ts"]):
        fav = (bars[i][2] - entry) if d > 0 else (entry - bars[i][3])
        if fav / risk_px >= rmult:
            touched.append({"rmult": rmult, "r": float(r["r_multiple"])})
            break
        i += 1
B = {"n": len(touched),
     "sum_r_actual": round(sum(x["r"] for x in touched), 2),
     "sum_r_hard_target": round(sum(x["rmult"] for x in touched), 2),
     "delta_r": round(sum(x["rmult"] - x["r"] for x in touched), 2),
     "actual_over_target": round(sum(x["r"] for x in touched) / sum(x["rmult"] for x in touched), 2)}

# ---------------------------------------------------------------- C ACCEPT 保本
be = []
for r in acc:
    tf = r["tf"]; d = int(r["direction"])
    bars, idx = BARS[tf]
    i = bisect.bisect_left(idx, int(r["open_ts"]))
    entry, risk_px = float(r["entry"]), abs(float(r["entry"]) - float(r["invalid"]))
    exit_ts = int(r["exit_ts"])
    hit_i = None
    while i < len(bars) and bars[i][0] <= exit_ts:
        fav = (bars[i][2] - entry) if d > 0 else (entry - bars[i][3])
        if fav / risk_px >= 1.0:
            hit_i = i
            break
        i += 1
    if hit_i is None:
        continue
    stopped = False
    for j in range(hit_i + 1, len(bars)):
        if bars[j][0] > exit_ts:
            break
        if (bars[j][3] <= entry) if d > 0 else (bars[j][2] >= entry):
            stopped = True
            break
    be.append({"r_actual": float(r["r_multiple"]), "r_be": 0.0 if stopped else float(r["r_multiple"]),
               "stopped": stopped})
C = {"n_touched_1r": len(be), "be_triggered": sum(1 for x in be if x["stopped"]),
     "be_trigger_rate": round(sum(1 for x in be if x["stopped"]) / len(be), 3) if be else None,
     "sum_r_actual": round(sum(x["r_actual"] for x in be), 2),
     "sum_r_with_be": round(sum(x["r_be"] for x in be), 2),
     "delta_r": round(sum(x["r_be"] - x["r_actual"] for x in be), 2)}

out = {"A_edge_entry": A, "B_hard_target": B, "C_accept_breakeven": C,
       "notes": ["A: 边沿 = 信号 bar 与上一根的极值（长取最低/短取最高），主张窗 n_f+1=4 根；"
                 "成交后仍沿用原出场价，属同出场价算术反事实",
                 "B: 只对触及对侧沿的 REJECT；硬目标 = r_mult_planned",
                 "C: 只对触及 +1R 的 ACCEPT；保本 = 回到成本即平（用后续 bar 的 low/high 判定）"]}
(HERE / "counterfactuals.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok", A["n"], A["fill_rate"], B["delta_r"], C["delta_r"])
