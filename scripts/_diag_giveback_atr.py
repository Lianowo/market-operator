"""B1:A/B「回吐预算 = (1−|frac|)·span」 vs 「回吐预算 = k·ATR」。

判据:去Top10、1.5x、`R>=1.5` 桶净额（71% 利润来源）。
底座 = 当前生产配置（funding_source=history 已生效）。
"""
from __future__ import annotations

import json
import statistics
import sys
from dataclasses import replace as dc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import scenario as sc                    # noqa: E402
from market_operator.campaign import run_campaigns            # noqa: E402


def _summ(res) -> dict:
    cl = [t for t in res.trades if t.get("closed")]
    n = len(cl)
    if not n:
        return {"n": 0, "net": 0.0, "ex10": 0.0, "wr": 0.0, "pf": 0.0}
    sp = sorted([float(t["net_pnl"]) for t in cl], reverse=True)
    w = [t for t in cl if t["net_pnl"] > 0]
    lo = [t for t in cl if t["net_pnl"] <= 0]
    return {"n": n, "net": sum(sp), "ex10": sum(sp[10:]) if n > 10 else 0.0,
            "wr": len(w) / n * 100.0,
            "pf": sum(t["net_pnl"] for t in w)
            / max(1e-9, abs(sum(t["net_pnl"] for t in lo)))}


def _run(runner, st, arm, seg: str, cm: float = 1.0):
    costs = st.costs.scaled(cm)
    return run_campaigns(
        runner.frames, segment=seg, dev_frac=st.dev_frac, equity0=300.0,
        risk_frac=st.risk_frac, leverage_cap=st.leverage_cap,
        taker_fee=costs.taker_fee, maker_fee=costs.maker_fee,
        slippage_per_side=costs.slippage_per_side,
        funding_per_8h=costs.funding_per_8h, cost_multiplier=cm,
        funding_rates=costs.funding_rates,
        slippage_by_symbol=costs.slippage_by_symbol,
        contracts=runner.contracts, params=st.structure, n_fill=st.n_fill,
        portfolio_cap_frac=st.portfolio_cap_frac, directional_cap_frac=None,
        tf_cap_by_tf=dict(st.tf_cap_by_timeframe),
        capacity_mode=st.capacity_mode, arm=arm, router_params=st.router,
        router_params_by_tf=dict(st.router_by_timeframe), enable_mic=True)


def _top_bucket(res) -> str:
    rm = {}
    for r in res.ledger:
        if r["kind"] in ("OPEN", "LIMIT_FILL"):
            m = r.get("meta") or {}
            if m.get("r_mult") is not None:
                rm[(r["symbol"], r["ts"])] = float(m["r_mult"])
    hi, n_hi, mid, n_mid = 0.0, 0, 0.0, 0
    for t in res.trades:
        if not t.get("closed"):
            continue
        r = rm.get((t["campaign"], t["open_ts"]))
        if r is None:
            continue
        pnl = float(t["net_pnl"])
        if r >= 1.5:
            hi += pnl
            n_hi += 1
        elif 1.0 <= r < 1.5:
            mid += pnl
            n_mid += 1
    return f"R>=1.5: n={n_hi:3d} net={hi:+7.2f}U | 1.0-1.5: n={n_mid:3d} net={mid:+7.2f}U"


def main() -> None:
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    runner = sc.ScenarioRunner(tuple(raw["symbols"]), sc.PRODUCTION_CONFIG,
                               progress=False)
    st = runner.settings
    print("=" * 150)
    print(f"{'回吐预算口径':34s} | {'dev':>8s} | {'test':>8s} | "
          f"{'full(N,WR,PF)':>26s} | {'去Top10':>9s} | {'1.5x':>8s}")
    print("-" * 150)
    arms = (("span 比例(生产现状 (1-|frac|)·span)", st.arm),) + tuple(
        (f"ATR 单位 k={k:.2f}·ATR", dc(st.arm, giveback_atr_k=k))
        for k in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0))
    for label, arm in arms:
        d = _summ(_run(runner, st, arm, "dev"))
        t = _summ(_run(runner, st, arm, "test"))
        fr = _run(runner, st, arm, "full")
        f = _summ(fr)
        x = _summ(_run(runner, st, arm, "full", 1.5))
        print(f"{label:34s} | {d['net']:+8.2f} | {t['net']:+8.2f} | "
              f"{f['net']:+8.2f}({f['n']:3d},{f['wr']:4.1f}%,{f['pf']:4.2f}) | "
              f"{f['ex10']:+9.2f} | {x['net']:+8.2f}")
        print(f"    └ {_top_bucket(fr)}")


if __name__ == "__main__":
    main()
