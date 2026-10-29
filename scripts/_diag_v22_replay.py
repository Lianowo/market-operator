"""v22 两年全量四标的(BTC/ETH/SOL/DOGE)多周期回放与 104U 阶梯复利对比诊断。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import scenario as sc
from market_operator.campaign import run_campaigns


def _summarize(trades: list[dict], equity_rows: list[tuple], equity0: float) -> dict:
    closed = [t for t in trades if t.get("closed")]
    n = len(closed)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "payoff": 0.0, "gr": 0.0,
                "net_pnl": 0.0, "final_eq": equity0, "max_dd_pct": 0.0}
    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] <= 0]
    gw = sum(t["net_pnl"] for t in wins)
    gl = abs(sum(t["net_pnl"] for t in losses))
    pf = (gw / gl) if gl > 1e-9 else float("inf")
    aw = sum(t["gross_pnl"] / t["risk_usdt"] for t in wins if t.get("risk_usdt")) / len(wins) if wins else 0.0
    al = abs(sum(t["gross_pnl"] / t["risk_usdt"] for t in losses if t.get("risk_usdt")) / len(losses)) if losses else 0.0
    payoff = (aw / al) if al > 1e-9 else float("inf")
    grs = [t["gross_pnl"] / t["risk_usdt"] for t in closed if t.get("risk_usdt")]
    gr = sum(grs) / len(grs) if grs else 0.0
    sp = sorted([float(t["net_pnl"]) for t in closed], reverse=True)
    net = sum(sp)
    ex10 = sum(sp[10:]) if n > 10 else 0.0
    peak, mdd = equity0, 0.0
    for row in equity_rows:
        eq = float(row[1])
        peak = max(peak, eq)
        if peak > 0:
            mdd = max(mdd, (peak - eq) / peak)
    feq = float(equity_rows[-1][1]) if equity_rows else (equity0 + net)
    return {"n": n, "wr": len(wins) / n * 100.0, "pf": pf, "payoff": payoff,
            "gr": gr, "net_pnl": net, "ex10": ex10, "final_eq": feq, "max_dd_pct": mdd * 100.0}



def main() -> None:
    t0 = time.perf_counter()
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    symbols = tuple(raw["symbols"])
    runner = sc.ScenarioRunner(symbols, sc.PRODUCTION_CONFIG, progress=True)
    st = runner.settings

    print("\n" + "=" * 108)
    print("v22 真平衡箱夹板 + 完备双原语(R1/R2/R3 + A1/A2 + CONSUMED) + 回抽限价 + 同向风控 两年全量回放报告")
    print("=" * 108)
    print(f"标的集合: {', '.join(symbols)} | 认知周期: {', '.join(runner.timeframes)}")

    print("\n[一] 300U 标准账户分段与成本压力验证 (portfolio_cap=12%, directional_cap=8%):")
    print("-" * 108)
    for seg, cm in (("dev", 1.0), ("test", 1.0), ("full", 1.0), ("full", 1.5)):
        out = runner.run(segment=seg, cost_multiplier=cm, equity0=300.0)
        s = _summarize(out.trades, out.equity_rows, 300.0)
        c = out.counters
        print(f"  {seg:4s} @ {cm:.1f}x | 笔数 N={s['n']:4d} | WR={s['wr']:5.1f}% | "
              f"盈亏比={s['payoff']:4.2f} | PF={s['pf']:4.2f} | E[gR]={s['gr']:+.3f}R | "
              f"净利={s['net_pnl']:+7.2f}U | 去Top10={s['ex10']:+7.2f}U | 终值={s['final_eq']:7.2f}U | MaxDD={s['max_dd_pct']:5.2f}%")

    out_full = runner.run(segment="full", cost_multiplier=1.0, equity0=300.0)
    print("\n[二] Full @ 1.0x 各认知周期明细:")
    print("-" * 108)
    for tf in runner.timeframes:
        sub = [t for t in out_full.trades if t.get("closed") and str(t.get("campaign", "")).endswith(f"@{tf}")]
        s_tf = _summarize(sub, [], 300.0)
        print(f"  {tf:4s} | 笔数 N={s_tf['n']:4d} | WR={s_tf['wr']:5.1f}% | "
              f"盈亏比={s_tf['payoff']:4.2f} | PF={s_tf['pf']:4.2f} | "
              f"E[gR]={s_tf['gr']:+.3f}R | 净贡献={s_tf['net_pnl']:+7.2f}U")

    sac = raw.get("small_account_compounding", {})
    eq0 = float(sac.get("equity0", 104.0))
    tiers = sac.get("dynamic_risk_tiers", [[350.0, 0.05], [1000.0, 0.04], [2500.0, 0.03], [1e9, 0.02]])
    tf_caps = dict(sac.get("tf_cap_by_tf", {"2h": 0.12, "4h": 0.12, "12h": 0.12, "1d": 0.12}))
    p_cap = float(sac.get("portfolio_cap_frac", 0.50))
    raw_dc = sac.get("directional_cap_frac")
    d_cap = float(raw_dc) if raw_dc is not None else 0.08

    print("\n[三] 104 USDT 小资金阶梯复利模式 (20x 杠杆, 动态风险阶梯):")
    print("-" * 108)
    for label, dc in (("生产配置 (无同向锁限制)", None), ("对照: 开启同向锁 (directional_cap=0.08)", d_cap)):
        res = run_campaigns(
            runner.frames, segment="full", dev_frac=st.dev_frac, equity0=eq0,
            risk_frac=float(tiers[0][1]), leverage_cap=float(sac.get("leverage_cap", 20.0)),
            taker_fee=st.costs.taker_fee, maker_fee=st.costs.maker_fee,
            slippage_per_side=st.costs.slippage_per_side, funding_per_8h=st.costs.funding_per_8h,
            cost_multiplier=1.0, funding_rates=st.costs.funding_rates,
            slippage_by_symbol=st.costs.slippage_by_symbol, contracts=runner.contracts,
            params=st.structure, n_fill=st.n_fill, portfolio_cap_frac=p_cap,
            directional_cap_frac=dc, tf_cap_by_tf=tf_caps, dynamic_risk_tiers=tiers,
            capacity_mode=st.capacity_mode, arm=st.arm, router_params=st.router,
            router_params_by_tf=dict(st.router_by_timeframe), enable_mic=True,
        )
        s2 = _summarize(res.trades, res.equity_rows, eq0)
        d_skips = sum(1 for sk in res.cap_skips if sk.get("reason_code") == "DIRECTIONAL_CAP")
        print(f"  {label:38s} | N={s2['n']:4d} | WR={s2['wr']:5.1f}% | PF={s2['pf']:4.2f} | "
              f"104U->终值={s2['final_eq']:8.2f}U ({s2['final_eq']/eq0:4.1f}x, {(s2['final_eq']/eq0 - 1)*100:+.1f}%) | "
              f"去Top10={s2['ex10']:+7.2f}U | MaxDD={s2['max_dd_pct']:5.2f}%")


    print(f"\n总耗时: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
