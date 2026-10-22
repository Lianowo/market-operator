"""BREAKOUT_FAILED/LEVEL_REJECTED/TREND_RESUMED 退役家族解剖台(研究层,不改变执行白名单)。
理论先行的维度(regime对齐/位置带/空间),对 H=24 事件级产物做切片。
用法: python -X utf8 scripts/autopsy_e234.py
"""
import pandas as pd, numpy as np, sys
sys.path.insert(0, "src")
from market_operator.structure import MarketStructure, Params

P = Params.from_json("config/defaults.json")
df = pd.concat([pd.read_csv("reports/event_study_events_dev.csv"),
                pd.read_csv("reports/event_study_events_test.csv")])
q = df[(df.H == 24) & (df.event_id.isin(["BREAKOUT_FAILED","LEVEL_REJECTED","TREND_RESUMED"]))].copy()
bands = {}
for s in q.symbol.unique():
    d = pd.read_csv(f"data/raw/{s}_1h.csv"); ms = MarketStructure(P); snap = []
    for b in d.itertuples():
        ms.update(dict(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume))
        hs = [p for p in ms.pivots if p.kind.value=="high"]; ls = [p for p in ms.pivots if p.kind.value=="low"]
        snap.append((hs[-1].price if hs else np.nan, ls[-1].price if ls else np.nan))
    bands[s] = pd.Series(snap)
q["pos"] = q.apply(lambda r: ((r.entry - bands[r.symbol].iloc[int(r.known_index)][1]) /
    (bands[r.symbol].iloc[int(r.known_index)][0] - bands[r.symbol].iloc[int(r.known_index)][1]))
    if bands[r.symbol].iloc[int(r.known_index)][0] > bands[r.symbol].iloc[int(r.known_index)][1] else np.nan, axis=1)
align = {"TREND_DOWN": -1, "TREND_UP": 1}
q["aligned"] = q.apply(lambda r: align.get(r.regime) == r.direction, axis=1)
def stat(g):
    return pd.Series(dict(n=len(g), net=g.net_atr_1_0.mean(), incr=g.incr_atr_1_0.mean())).round(3)
for ev in ["BREAKOUT_FAILED","LEVEL_REJECTED","TREND_RESUMED"]:
    e = q[q.event_id == ev]
    print(f"== {ev} n={len(e)} 全体 net={e.net_atr_1_0.mean():.3f} incr={e.incr_atr_1_0.mean():.3f}")
    print(e.groupby(["direction","aligned"]).apply(stat, include_groups=False).unstack())
