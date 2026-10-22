"""诊断:赢家 vs 输家 全生命周期对照(当前最优配置:止损修复,无fade收割)。"""
import json
import sys
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import run_backtest_v6 as rb
from market_operator.campaign import run_campaigns
from market_operator.structure import Params
from market_operator.strategy_v6 import V6Params


def pct(x, n):
    return f"{100*x/n:.1f}%" if n else "-"


def main():
    raw = json.loads((ROOT / "config" / "canonical.json").read_text(encoding="utf-8"))
    params = Params.from_json(ROOT / "config" / "canonical.json")
    syms = list(raw["symbols"]); tfs = list(raw["tf_list"])
    wl = frozenset((str(i), int(d)) for i, d in raw["whitelist"])
    rp = V6Params(k_dq=float(raw["k_dq"]), k_imp=float(raw["k_imp"]),
                  k_is=float(raw["k_is"]), turtle_stop_atr=float(raw["turtle_stop_atr"]),
                  k_bm=float(raw["k_bm"]), whitelist=wl)
    contracts, _ = rb.load_contracts_cache(syms)
    kw = dict(dev_frac=float(raw["dev_frac"]), equity0=300.0,
              risk_frac=float(raw["risk_frac"]), leverage_cap=float(raw["leverage_cap"]),
              taker_fee=float(raw["taker_fee"]), maker_fee=float(raw["maker_fee"]),
              slippage_per_side=float(raw["slippage_per_side"]),
              funding_per_8h=float(raw["funding_per_8h_per_side"]),
              contracts=contracts, params=params, n_fill=int(raw["n_fill"]),
              portfolio_cap_frac=float(raw["portfolio_cap_frac"]))
    frames, _ = rb.prepare_frames(raw, params, tfs)
    res = run_campaigns(frames, segment="full", cost_multiplier=1.0, router_params=rp, **kw)
    res.trades = rb.augment_trades(res.trades, frames)
    df = pd.DataFrame(res.trades)
    c = df[df.closed == True].copy()
    c["gR"] = c.gross_pnl / c.risk_usdt
    W = c[c.gR > 0].copy(); L = c[c.gR <= 0].copy()
    print(f"总{c.shape[0]} 赢{len(W)} 亏{len(L)} | 毛期望R {c.gR.mean():+.3f}")

    print("\n[A] 事件×方向 (赢/亏 净额):")
    for side in (1, -1):
        sub = c[c.direction == side]
        print(f"  {'long' if side>0 else 'short'}: 赢{len(sub[sub.gR>0])} 亏{len(sub[sub.gR<=0])} 净{sub.net_pnl.sum():+.0f}")
    g = c.groupby(["event_id", "direction"]).agg(n=("net_pnl", "size"), net=("net_pnl", "sum"),
                                                 wr=("gR", lambda s: (s > 0).mean())).sort_values("net")
    for (ev, dr), r in g.iterrows():
        print(f"  {ev} {'long' if dr>0 else 'short':5s} n={int(r['n']):4d} wr={r['wr']:.2f} 净{r['net']:+.1f}")

    print("\n[B] 环境 regime (赢/亏):")
    for rg in ["TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION"]:
        s = c[c.regime == rg]
        if len(s):
            print(f"  {rg:12s} n={len(s):4d} 胜率{100*len(s[s.gR>0])/len(s):.1f}% 净{s.net_pnl.sum():+.1f}")

    print("\n[C] 价格行为:赢家 MFE vs 输家 MFE")
    print(f"  赢家 MFE 中位 {W.mfe_r.median():.2f}R | 输家 MFE 中位 {L.mfe_r.median():.2f}R")
    print(f"  输家曾盈利>0.3R: {pct(sum(L.mfe_r>0.3),len(L))} | >0.5R: {pct(sum(L.mfe_r>0.5),len(L))} | >1R: {pct(sum(L.mfe_r>1.0),len(L))}")
    print(f"  输家从未盈利(MFE<=0): {pct(sum(L.mfe_r<=0),len(L))}")

    print("\n[D] 持仓时长(h)")
    print(f"  赢家 中位{W.hold_hours.median():.1f} 均{W.hold_hours.mean():.1f}")
    print(f"  输家 中位{L.hold_hours.median():.1f} 均{L.hold_hours.mean():.1f}")

    print("\n[E] 退出原因 (赢/亏)")
    def bucket(r):
        r = str(r or "")
        if r.startswith("PROTECTIVE_STOP"): return "protective_stop"
        if "CHoCH" in r: return "choch"
        if "time" in r: return "time"
        return "other"
    c["eb"] = c.reason_exit.map(bucket)
    for k in ["protective_stop", "choch", "time", "other"]:
        s = c[c.eb == k]
        if len(s):
            print(f"  {k:16s} n={len(s):4d} 胜率{100*len(s[s.gR>0])/len(s):.1f}% 净{s.net_pnl.sum():+.1f}")

    print("\n[F] 计划盈亏比 r_mult_planned (赢 vs 输)")
    print(f"  赢家 中位{W.r_mult_planned.median():.2f} | 输家 中位{L.r_mult_planned.median():.2f}")

    print("\n[G] 入场路由 (赢/亏)")
    for rt in ["breakout_accepted_market", "structure_broken_strong_market", "structure_broken_weak_limit"]:
        s = c[c.route == rt]
        if len(s):
            print(f"  {rt:15s} n={len(s):4d} 胜率{100*len(s[s.gR>0])/len(s):.1f}% 净{s.net_pnl.sum():+.1f}")


if __name__ == "__main__":
    main()
