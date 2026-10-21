"""深度解剖:死法分类/一致性迟滞/级联噪声对比。
核心问题(用户 Q2):被打止损前,反向移动到底走了多远?是否真打到了对面结构
= 我们真的被市场合法合法证伪(合法死),还是在噪声里被晃死(冤死)?
约定:对空单,对面结构 = entry 下方最近的 ACTIVE 支撑区缘;
合法死 = MAE ≥ 对面缘(打到了对侧); 冤死 = 没打到却被出,且随后按 TF 窗走 ≥1R。
"""
import sys
import pandas as pd
import numpy as np

sys.path.insert(0, "src")
from market_operator.structure import MarketStructure, Params

P = Params.from_json("config/defaults.json")
TFH = {"1h": 1, "2h": 2, "4h": 4, "12h": 12, "1d": 24}
HOR_MULT = 6  # 正当窗口 = 6×TF小时(1h→6h, 12h→72h)


def build(symbol, tf):
    df = pd.read_csv(f"data/raw/{symbol}_{tf}.csv")
    ms = MarketStructure(P)
    snaps = []
    for b in df.itertuples():
        ms.update(dict(ts=b.ts, open=b.open, high=b.high, low=b.low,
                       close=b.close, volume=b.volume))
        sup = [z for z in ms.zones if z.role.value == "support"]
        res = [z for z in ms.zones if z.role.value == "resistance"]
        snaps.append((sup, res))
    return df, snaps


def main(path, label):
    t = pd.read_csv(path)
    frames = {(s, tf): build(s, tf) for (s, tf) in set(zip(t.symbol, t.tf))}
    rows = []
    for tr in t.itertuples():
        df, snaps = frames[(tr.symbol, tr.tf)]
        ts = df.ts.to_numpy()
        ei = np.searchsorted(ts, tr.open_decision_ts, "right") - 1
        fi = np.searchsorted(ts, tr.open_ts, "right") - 1
        xi = np.searchsorted(ts, tr.exit_ts, "right") - 1
        if ei < 50 or fi < 0 or xi < 0:
            continue
        seg = df.iloc[fi:xi + 1]
        d = tr.direction; R = abs(tr.entry - tr.invalid)
        mfe = (tr.entry - seg.low.min()) if d == -1 else (seg.high.max() - tr.entry)
        mae = (seg.high.max() - tr.entry) if d == -1 else (tr.entry - seg.low.min())
        # 对面结构缘(知时至决策 bar)
        sup, res = snaps[ei]
        atr_est = seg.high.max() - seg.low.min()
        if R <= 0:
            continue
        if d == -1:
            cand = [z.hi for z in sup if z.hi <= tr.entry]
            opp = max(cand) if cand else np.nan   # 对面底缘(下方最近支撑带上缘)
        else:
            cand = [z.lo for z in res if z.lo >= tr.entry]
            opp = min(cand) if cand else np.nan   # 对面顶缘(上方最近阻力带下缘)
        # 正当窗口 = HOR_MULT×TF 小时的 bar 数(容忍 1h 帧)
        w = int(HOR_MULT * TFH[tr.tf])
        after = df.iloc[xi + 1: xi + 1 + w]
        cont = np.nan
        if len(after):
            cont = ((tr.entry - after.low.min()) / R) if d == -1 else ((after.high.max() - tr.entry) / R)
        # 决策时该腿已走的几何(相对结构 ref)
        ref = None
        try:
            import re
            m = re.search(r"entry_ref=([\d.eE+-]+)", tr.reason_open)
            ref = float(m.group(1)) if m else None
        except Exception:
            pass
        lag = ((ref - tr.entry) * d / ref * 100) if ref else np.nan
        rows.append(dict(tf=tr.tf, ev=tr.event_id, dir=d, loss=tr.net_pnl <= 0,
                         mfe_R=mfe / R, mae_R=mae / R,
                         opp=opp, opp_dist_R=(((tr.entry - opp) if d == -1 else (opp - tr.entry)) / R) if opp == opp else np.nan,
                         hit_opp=((mae >= 0.9 * ((tr.entry - opp) if d == -1 else (opp - tr.entry))) if opp == opp else False),
                         cont_R=cont, regret1R=(cont is not np.nan and cont >= 1.0),
                         lag_pct=lag, net=tr.net_pnl))
    q = pd.DataFrame(rows)
    L = q[q.loss]
    print(f"\n===== {label}  总 {len(q)} 亏 {len(L)} =====")
    print("== 亏损单死法分类 ==")
    legit = L[L.hit_opp == True]; noise = L[(L.hit_opp == False) & (L.regret1R == True)]
    dead = L[(L.hit_opp == False) & (L.regret1R == False)]
    print(f"  合法死(打到了对面结构缘才出): {len(legit)} ({len(legit)/len(L)*100:.1f}%)")
    print(f"  冤死(没到对面且按正当窗后来走≥1R): {len(noise)} ({len(noise)/len(L)*100:.1f}%)")
    print(f"  真错(没到对面,之后也没走出): {len(dead)} ({len(dead)/len(L)*100:.1f}%)")
    print(f"  对面缘距离中位: {L.opp_dist_R.median():.2f}R | MAE中位 {L.mae_R.median():.2f}R | 冤死者的 MFE_R中位 {noise.mfe_R.median() if len(noise) else 0:.2f}")
    print("== 家族×尺度(全部笔,净均值/胜率) ==")
    print(q.assign(win=~q.loss).groupby(["tf", "ev"]).agg(n=("win", "size"),
          net=("net", "sum"), win=("win", "mean")).round(3).to_string())
    print("== 早/晚(决策→成交滞后 lag%,正:已顺走一段) × 胜率 ==")
    q["lagb"] = pd.cut(q.lag_pct, [-50, 0.05, 0.3, 99])
    print(q.assign(win=~q.loss).groupby("lagb", observed=True).agg(n=("win", "size"),
          win=("win", "mean")).round(3))
    return q


q6 = main("reports/trades_v6_turtle_test.csv", "canonical(E3 开放, v6 turtle)")
if __name__ == "__main__" and len(sys.argv) > 1:
    main("reports/trades_v13_cascade_cascade__test.csv", "v13 级联臂")
