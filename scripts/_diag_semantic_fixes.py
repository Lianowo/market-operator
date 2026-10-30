"""语义修复的逐项判决(只读诊断;每项独立开关,同口径对比)。

变体:
  OLD  旧口径:A5 关(target_from_pivot=False) + A4 关(allow_pullback_limit=True)
       + A3 关(max_cost_frac=None) + A11 关(结构性撤单 no-op)
  F5   只开 A5(目标取"最近结构障碍")
  F4   F5 + A4(确认离带太远 ⇒ 丢弃,不挂回抽限价)
  F3   F4 + A3(成本闸改用 往返成本/风险 ≤ x)   ← x 扫描
  F11  F4 + A11(结构性撤单)
  ALL  F5+F4+A3(取最优 x)+A11 + quality_min=0
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import market_operator.campaign as camp_mod                 # noqa: E402
from market_operator import scenario as sc                  # noqa: E402
from market_operator.campaign import run_campaigns          # noqa: E402

ORIG_CANCEL = camp_mod._cancel_structurally_dead_entries


def _summ(res, eq0: float = 300.0) -> dict:
    cl = [t for t in res.trades if t.get("closed")]
    n = len(cl)
    if not n:
        return {"n": 0, "net": 0.0, "ex10": 0.0, "wr": 0.0, "pf": 0.0}
    sp = sorted([float(t["net_pnl"]) for t in cl], reverse=True)
    w = [t for t in cl if t["net_pnl"] > 0]
    lo = [t for t in cl if t["net_pnl"] <= 0]
    return {"n": n, "net": sum(sp), "ex10": sum(sp[10:]) if n > 10 else 0.0,
            "wr": len(w) / n * 100.0,
            "pf": sum(t["net_pnl"] for t in w) / max(1e-9, abs(sum(t["net_pnl"] for t in lo)))}


def evaluate(label: str, runner, rp, rpm, arm) -> dict:
    st = runner.settings

    def _r(seg, cm):
        costs = st.costs.scaled(cm)
        return _summ(run_campaigns(
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
            capacity_mode=st.capacity_mode, arm=arm, router_params=rp,
            router_params_by_tf=rpm, enable_mic=True))

    d, t, f, x = _r("dev", 1.0), _r("test", 1.0), _r("full", 1.0), _r("full", 1.5)
    print(f"{label:36s} | dev:{d['net']:+7.2f} | test:{t['net']:+7.2f} | "
          f"full:{f['net']:+7.2f}(N={f['n']:3d},WR={f['wr']:4.1f}%,PF={f['pf']:4.2f}) | "
          f"去Top10:{f['ex10']:+7.2f} | 1.5x:{x['net']:+7.2f}")
    return {"dev": d, "test": t, "full": f, "x15": x}


def main() -> None:
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    runner = sc.ScenarioRunner(tuple(raw["symbols"]), sc.PRODUCTION_CONFIG,
                               progress=False)
    st = runner.settings

    def var(**over):
        rp = replace(st.router, **over)
        rpm = {k: replace(v, **over) for k, v in st.router_by_timeframe.items()}
        return rp, rpm

    print("=" * 130)
    print(f"{'变体':36s} | {'dev净利':>10s} | {'test净利':>10s} | "
          f"{'full@1.0x (N, WR, PF)':>28s} | {'去Top10':>10s} | {'1.5x':>10s}")
    print("-" * 130)

    old = dict(target_from_pivot=False, allow_pullback_limit=True,
               max_cost_frac=None, far_confirm_skip=False)
    f4a = dict(old, allow_pullback_limit=False)
    f4b = dict(old, far_confirm_skip=True)

    camp_mod._cancel_structurally_dead_entries = lambda *a, **k: None
    try:
        evaluate("OLD 旧口径(全部关闭)", runner, *var(**old), st.arm)
        evaluate("A4a 关回抽限价(只禁 limit)", runner, *var(**f4a), st.arm)
        evaluate("A4b 确认离带太远即丢弃", runner, *var(**f4b), st.arm)
        for cf in (0.09, 0.10, 0.11, 0.12, 0.13):
            evaluate(f"A3 成本闸 cost/risk<={cf:.2f}", runner,
                     *var(**dict(old, max_cost_frac=cf)), st.arm)
        evaluate("A5' 目标取最近结构障碍", runner,
                 *var(**dict(old, target_from_pivot=True)), st.arm)
    finally:
        camp_mod._cancel_structurally_dead_entries = ORIG_CANCEL

    evaluate("A11 结构性撤单(旧底座)", runner, *var(**old), st.arm)



if __name__ == "__main__":
    main()
