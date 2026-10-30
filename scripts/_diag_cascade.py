"""级联入场研究 + quality_min 冗余性 + A1 归因(只读诊断)。

问题:语义修好之后,"让 2h 战役用 30m 触发"这类更小级别级联,是否能提高入场
速度并带来收益?同时验证 `balance_quality_min` 是否冗余(A6/A7/A8 已在
`_score_window` 里以硬谓词生效,quality 只是其上的二次打分)。

口径:dev / test / full@1.0x / full@1.5x / 去Top10。级联变体共用同一份已装载
行情与认知帧,只重建**触发帧**(`build_frames`),因此比较的是"触发粒度"。
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import scenario as sc                     # noqa: E402
from market_operator.campaign import run_campaigns             # noqa: E402

TMP = ROOT / "var" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)


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


def _run(frames, runner, settings, seg: str, cm: float):
    costs = settings.costs.scaled(cm)
    return run_campaigns(
        frames, segment=seg, dev_frac=settings.dev_frac, equity0=300.0,
        risk_frac=settings.risk_frac, leverage_cap=settings.leverage_cap,
        taker_fee=costs.taker_fee, maker_fee=costs.maker_fee,
        slippage_per_side=costs.slippage_per_side,
        funding_per_8h=costs.funding_per_8h, cost_multiplier=cm,
        funding_rates=costs.funding_rates,
        slippage_by_symbol=costs.slippage_by_symbol,
        contracts=runner.contracts, params=settings.structure,
        n_fill=settings.n_fill, portfolio_cap_frac=settings.portfolio_cap_frac,
        directional_cap_frac=None,
        tf_cap_by_tf=dict(settings.tf_cap_by_timeframe),
        capacity_mode=settings.capacity_mode, arm=settings.arm,
        router_params=settings.router,
        router_params_by_tf=dict(settings.router_by_timeframe),
        enable_mic=True)


def evaluate(label: str, frames, runner, settings) -> dict:
    d = _summ(_run(frames, runner, settings, "dev", 1.0))
    t = _summ(_run(frames, runner, settings, "test", 1.0))
    f = _summ(_run(frames, runner, settings, "full", 1.0))
    x = _summ(_run(frames, runner, settings, "full", 1.5))
    print(f"{label:40s} | dev:{d['net']:+7.2f} | test:{t['net']:+7.2f} | "
          f"full:{f['net']:+7.2f}(N={f['n']:3d},WR={f['wr']:4.1f}%,PF={f['pf']:4.2f}) | "
          f"去Top10:{f['ex10']:+7.2f} | 1.5x:{x['net']:+7.2f}")
    return f


def main() -> None:
    base_raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    syms = tuple(base_raw["symbols"])
    runner = sc.ScenarioRunner(syms, sc.PRODUCTION_CONFIG, progress=False)
    st = runner.settings
    print("=" * 132)
    print(f"{'变体':40s} | {'dev净利':>10s} | {'test净利':>10s} | "
          f"{'full@1.0x (N, WR, PF)':>28s} | {'去Top10':>10s} | {'1.5x':>10s}")
    print("-" * 132)

    evaluate("0 基线(生产 config 现状)", runner.frames, runner, st)

    # --- A1 归因:ACCEPT|LONG 在修好语义后到底是什么贡献 ---
    res = _run(runner.frames, runner, st, "full", 1.0)
    closed = [t for t in res.trades if t.get("closed")]
    acc_l = [t for t in closed if "ACCEPT" in str(t.get("event_id"))
             and int(t.get("direction", 0)) > 0]
    if acc_l:
        net = sum(float(t["net_pnl"]) for t in acc_l)
        wins = sum(1 for t in acc_l if t["net_pnl"] > 0)
        print(f"\n[A1 归因] ACCEPT|LONG N={len(acc_l)} "
              f"WR={wins / len(acc_l) * 100:.1f}% net={net:+.2f}U | "
              f"若仍用 0.35 权重 ≈ {net * 0.35:+.2f}U(被砍掉 {net * 0.65:+.2f}U)\n")

    # --- A6/A7/A8:quality_min 是否冗余(结构参数变化 ⇒ 必须重建认知帧) ---
    for q in (0.0, 0.20):
        raw = dict(base_raw)
        raw["balance_quality_min"] = q
        p = TMP / f"_cfg_q{str(q).replace('.', '_')}.json"
        p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        r2 = sc.ScenarioRunner(syms, p, progress=False)
        evaluate(f"A6 quality_min={q:.2f} (谓词已生效时是否还需要)",
                 r2.frames, r2, r2.settings)

    # --- 级联:只重建触发帧,行情/认知帧复用 ---
    for label, trig in (
        ("C1 2h->30m", {"2h": 1800, "4h": 7200, "12h": 7200, "1d": 14400}),
        ("C2 2h->1h", {"2h": 3600, "4h": 7200, "12h": 7200, "1d": 14400}),
        ("C4 2h->30m + 4h->1h", {"2h": 1800, "4h": 3600, "12h": 7200,
                                 "1d": 14400}),
        ("C5 2h->30m + 12h->1h", {"2h": 1800, "4h": 7200, "12h": 3600,
                                  "1d": 14400}),
        ("C6 2h->30m + 12h->1h + 1d->2h", {"2h": 1800, "4h": 7200, "12h": 3600,
                                           "1d": 7200}),
    ):
        st2 = dc_replace(st, trigger_seconds_by_timeframe=trig)
        try:
            frames = sc.build_frames(runner.market, st2, runner.timeframes)
        except Exception as e:                    # noqa: BLE001
            print(f"{label:40s} | 跳过: {type(e).__name__}: {e}")
            continue
        evaluate(label, frames, runner, st2)


if __name__ == "__main__":
    main()

