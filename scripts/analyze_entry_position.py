"""入场位置结构尺诊断： 用系统自身确认摆动结构(pivot)作尺子, 量化每笔交易入场
在结构带中的位置 pos=(entry-last_pivot_low)/(last_pivot_high-last_pivot_low)。
0=结构底, 1=结构顶; <0=底带之下(穿越), >1=顶带之上(穿越)。
用法: python -X utf8 scripts/analyze_entry_position.py reports/trades_v6_turtle_test.csv
"""
import sys
import pandas as pd
import numpy as np

sys.path.insert(0, "src")
from market_operator.structure import MarketStructure, Params


def main(path):
    P = Params.from_json("config/defaults.json")
    t = pd.read_csv(path)
    piv = {}
    for (s, tf) in set(zip(t.symbol, t.tf)):
        df = pd.read_csv(f"data/raw/{s}_{tf}.csv")
        ms = MarketStructure(P)
        for b in df.itertuples():
            ms.update(dict(ts=b.ts, open=b.open, high=b.high, low=b.low,
                           close=b.close, volume=b.volume))
        piv[(s, tf)] = (df, ms.pivots)
    out = []
    for tr in t.itertuples():
        df, ps = piv[(tr.symbol, tr.tf)]
        di = np.searchsorted(df.ts.to_numpy(), tr.open_decision_ts, "right") - 1
        hs = [p for p in ps if p.kind.value == "high" and p.known_index <= di]
        ls = [p for p in ps if p.kind.value == "low" and p.known_index <= di]
        if not hs or not ls:
            continue
        hi, lo = hs[-1].price, ls[-1].price
        rg = hi - lo
        if rg <= 0:
            continue
        out.append(dict(pos=(tr.entry - lo) / rg, dir=tr.direction,
                        win=tr.net_pnl > 0, ev=tr.event_id, tf=tr.tf))
    q = pd.DataFrame(out)
    print("n=", len(q))
    print(q.groupby("dir").pos.describe()[["mean", "50%", "25%", "75%"]].round(3))
    sq, lq = q[q.dir == -1], q[q.dir == 1]
    print("shorts 底带%.0f%% 顶带%.0f%% | longs 底带%.0f%% 顶带%.0f%%" % (
        (sq.pos < .35).mean() * 100, (sq.pos > .65).mean() * 100,
        (lq.pos < .35).mean() * 100, (lq.pos > .65).mean() * 100))
    q["b"] = pd.cut(q.pos, [-9, .35, .65, 9], labels=["底带", "中段", "顶带"])
    print(q.groupby(["b", "dir"], observed=True).agg(
        n=("win", "size"), win=("win", "mean")).round(3))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "reports/trades_v6_turtle_test.csv")
