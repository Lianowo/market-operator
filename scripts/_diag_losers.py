"""临时诊断:重跑修复后 full@1x,保存逐笔,深剖亏损仓位全生命周期。"""
import json
import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import run_backtest_v6 as rb
from market_operator.campaign import run_campaigns
from market_operator.structure import Params
from market_operator.routing import RouterParams

CONFIG = ROOT / "config" / "canonical.json"


def pct(x, n):
    return f"{100*x/n:.1f}%" if n else "-"


def main():
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG)
    syms = list(raw["symbols"]); tfs = list(raw["tf_list"])
    wl = frozenset((str(i), int(d)) for i, d in raw["whitelist"])
    rp = RouterParams(k_dq=float(raw["k_dq"]), k_imp=float(raw["k_imp"]),
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
    res = run_campaigns(frames, segment="full", cost_multiplier=1.0,
                        router_params=rp, **kw)
    res.trades = rb.augment_trades(res.trades, frames)
    df = pd.DataFrame(res.trades)
    df.to_csv(ROOT / "reports" / "trades_stopfix_full.csv", index=False)
    c = df[df.closed == True].copy()
    c["gR"] = c.gross_pnl / c.risk_usdt
    L = c[c.gR <= 0].copy(); W = c[c.gR > 0].copy(); n = len(c)
    print("=" * 70)
    print(f"全生命周期剖析 (full@1x 修复后): 总{n} 亏{len(L)}({pct(len(L),n)}) 盈{len(W)}({pct(len(W),n)})")
    print("=" * 70)

    print("\n[A] 亏损按 事件×环境×方向 (net / n / avgR)")
    g = L.groupby(["event_id", "regime", "direction"]).agg(
        n=("net_pnl", "size"), net=("net_pnl", "sum"), ar=("gR", "mean")).sort_values("net")
    for (ev, rg, dr), r in g.iterrows():
        print(f"  {ev:3s} {rg:10s} {'long' if dr>0 else 'short':5s} | {r['net']:+8.1f} | n={int(r['n']):4d} | {r['ar']:+.2f}")

    print("\n[B] 入场路由 亏损/该路由总 与胜率")
    for rt in ["breakout_accepted_market", "structure_broken_strong_market", "structure_broken_weak_limit"]:
        allr = c[c.route == rt]; sub = L[L.route == rt]
        if len(allr):
            print(f"  {rt:15s} | 亏{len(sub):4d}/{len(allr):4d} | 该路由胜率{100*(1-len(sub)/len(allr)):.1f}%")

    print("\n[C] 价格行为(亏损笔) MFE")
    m = L.mfe_r
    for thr in [0, 0.3, 0.5, 1.0, 2.0]:
        print(f"  曾盈利>{thr}R后回吐 : {int(sum(m>thr)):4d} ({pct(sum(m>thr), len(L))})")
    gb = L.giveback_r.dropna()
    print(f"  回吐中位 {gb.median():.2f}R")

    print("\n[D] 环境 regime 表现")
    for rg in ["TREND_UP", "TREND_DOWN", "RANGE", "TRANSITION"]:
        s = c[c.regime == rg]
        if len(s):
            print(f"  {rg:12s} | n={len(s):4d} | 胜率{100*len(s[s.gR>0])/len(s):.1f}% | avgR {s.gR.mean():+.3f} | 净{s.net_pnl.sum():+8.1f}")

    print("\n[F] 亏损退出原因")
    def bucket(r):
        r = str(r or "")
        if r.startswith("PROTECTIVE_STOP"): return "protective_stop"
        if "CHoCH" in r: return "choch"
        if "time" in r: return "time"
        if "target" in r: return "target"
        return "other"
    L["eb"] = L.reason_exit.map(bucket)
    for k, v in L.eb.value_counts().items():
        s = L[L.eb == k]
        print(f"  {k:16s} | n={len(s):4d} ({pct(len(s),len(L))}) | avgR {s.gR.mean():+.2f} | 净{s.net_pnl.sum():+8.1f}")

    print("\n[G] 回吐型亏损(曾>0.5R却亏) 事件×环境")
    give = L[L.mfe_r > 0.5].groupby(["event_id", "regime"]).agg(
        n=("net_pnl", "size"), net=("net_pnl", "sum")).sort_values("n", ascending=False)
    for (ev, rg), r in give.head(8).iterrows():
        print(f"  {ev:3s} {rg:10s} | n={int(r['n']):4d} | 净{r['net']:+8.1f}")

    print("\n[H] 从未盈利型(MFE<=0) 事件×环境×方向")
    nv = L[L.mfe_r <= 0].groupby(["event_id", "regime", "direction"]).agg(
        n=("net_pnl", "size"), net=("net_pnl", "sum")).sort_values("n", ascending=False)
    for (ev, rg, dr), r in nv.head(8).iterrows():
        print(f"  {ev:3s} {rg:10s} {'long' if dr>0 else 'short':5s} | n={int(r['n']):4d} | 净{r['net']:+8.1f}")

    print(f"\n保护性止损成交 {res.counters.get('protective_stop_fills')} 笔")


if __name__ == "__main__":
    main()
