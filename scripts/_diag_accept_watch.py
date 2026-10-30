"""突破过伸系统价格关注 (Breakout WatchEntry) 全量 A/B 验证。"""
from __future__ import annotations

import json
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
    return {
        "n": n, "net": sum(sp),
        "ex10": sum(sp[10:]) if n > 10 else 0.0,
        "wr": len(w) / n * 100.0,
        "pf": sum(t["net_pnl"] for t in w) / max(1e-9, abs(sum(t["net_pnl"] for t in lo))),
    }


def _accept_split(res) -> dict:
    opens = {(r["symbol"], r["ts"]): r for r in res.ledger if r["kind"] in ("OPEN", "LIMIT_FILL")}
    out = {1: [0, 0.0, 0], -1: [0, 0.0, 0]}
    watch_fills = 0
    for t in res.trades:
        if not t.get("closed"):
            continue
        op = opens.get((t["campaign"], t["open_ts"]))
        if not op:
            continue
        m = op.get("meta") or {}
        if m.get("event_id") != "BREAKOUT_ACCEPTED":
            continue
        d = int(t["direction"])
        out[d][0] += 1
        out[d][1] += float(t["net_pnl"])
        out[d][2] += 1 if float(t["net_pnl"]) > 0 else 0
        if m.get("watch_entry"):
            watch_fills += 1
    c = res.counters
    return {
        "l_n": out[1][0], "l_net": out[1][1],
        "s_n": out[-1][0], "s_net": out[-1][1],
        "w_fills": watch_fills,
        "w_armed": c.get("watch_entries_armed", 0),
        "w_played": c.get("watch_canceled_played_out", 0),
    }


def _with_overrides(st, *, struct_kw: dict | None = None, router_kw: dict | None = None):
    s_new = dc(st.structure, **(struct_kw or {}))
    r_kw = router_kw or {}
    r_new = dc(st.router, **r_kw)
    r_tf = {tf: dc(rp, **r_kw) for tf, rp in st.router_by_timeframe.items()}
    return dc(st, structure=s_new, router=r_new, router_by_timeframe=r_tf)


def _run(frames, runner, st, seg: str, cm: float = 1.0):
    costs = st.costs.scaled(cm)
    return run_campaigns(
        frames, segment=seg, dev_frac=st.dev_frac, equity0=300.0,
        risk_frac=st.risk_frac, leverage_cap=st.leverage_cap,
        taker_fee=costs.taker_fee, maker_fee=costs.maker_fee,
        slippage_per_side=costs.slippage_per_side,
        funding_per_8h=costs.funding_per_8h, cost_multiplier=cm,
        funding_rates=costs.funding_rates,
        slippage_by_symbol=costs.slippage_by_symbol,
        contracts=runner.contracts, params=st.structure, n_fill=st.n_fill,
        portfolio_cap_frac=st.portfolio_cap_frac, directional_cap_frac=None,
        tf_cap_by_tf=dict(st.tf_cap_by_timeframe),
        capacity_mode=st.capacity_mode, arm=st.arm, router_params=st.router,
        router_params_by_tf=dict(st.router_by_timeframe), enable_mic=True,
    )


VARIANTS = (
    ("R0 无Watch旧基线 (REJECT/ACCEPT均市价)",
     {"accept_max_ext_atr": 1.50},
     {"deadzone_watch_enabled": False, "accept_watch_enabled": False}),
    ("B0 现行基线 (仅REJECT死区Watch开, >1.5ATR丢弃)",
     {"accept_max_ext_atr": 1.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": False}),
    ("V1 过伸>0.8ATR转Watch (max=1.5, 4h, run=1.5)",
     {"accept_max_ext_atr": 1.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 0.80, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 4}),
    ("V2 过伸>0.8ATR转Watch + 救回2.5ATR (4h, run=1.5)",
     {"accept_max_ext_atr": 2.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 0.80, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 4}),
    ("V3 过伸>0.8ATR转Watch + 救回2.5ATR (8h, run=1.5)",
     {"accept_max_ext_atr": 2.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 0.80, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 8}),
    ("V4 过伸>1.0ATR转Watch + 救回2.5ATR (6h, run=1.5)",
     {"accept_max_ext_atr": 2.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.00, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 6}),
    ("V5 过伸>1.2ATR转Watch + 救回2.5ATR (6h, run=1.5)",
     {"accept_max_ext_atr": 2.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.20, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 6}),
    ("V6 仅救回>1.5ATR转Watch (max=2.5, 4h, run=1.5)",
     {"accept_max_ext_atr": 2.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.50, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 4}),
    ("V7 仅救回>1.5ATR转Watch (max=2.5, 8h, run=1.5)",
     {"accept_max_ext_atr": 2.50},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.50, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 8}),
    ("V10 取消丢弃(>1.5ATR全转Watch, max=99, 4h, run=1.5)",
     {"accept_max_ext_atr": 99.0},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.50, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 4}),
    ("V11 取消丢弃(>1.5ATR全转Watch, max=99, 8h, run=1.5)",
     {"accept_max_ext_atr": 99.0},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.50, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 8}),
    ("V12 取消丢弃(>1.5ATR全转Watch, max=99, 12h, run=1.5)",
     {"accept_max_ext_atr": 99.0},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.50, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 12}),
    ("V13 取消丢弃(>1.5ATR全转Watch, max=3.0, 6h, run=1.5)",
     {"accept_max_ext_atr": 3.00},
     {"deadzone_watch_enabled": True, "accept_watch_enabled": True,
      "accept_watch_ext_atr": 1.50, "accept_watch_max_run_atr": 1.50,
      "accept_watch_wait_hours": 6}),
)


def main() -> None:
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    runner = sc.ScenarioRunner(tuple(raw["symbols"]), sc.PRODUCTION_CONFIG, progress=False)
    base_st = runner.settings

    frames_cache: dict[float, object] = {}
    print("=" * 168)
    print(f"{'变体':46s} | {'dev':>8s} | {'test':>8s} | "
          f"{'full@1.0x (N, WR, PF)':>27s} | {'去Top10':>8s} | {'1.5x':>8s} | "
          f"{'ACC多(N/净)':>14s} | {'ACC空(N/净)':>14s} | {'Watch(成/武/废)':>15s}")
    print("-" * 168)

    for label, s_kw, r_kw in VARIANTS:
        st = _with_overrides(base_st, struct_kw=s_kw, router_kw=r_kw)
        max_ext = float(st.structure.accept_max_ext_atr)
        if max_ext not in frames_cache:
            frames_cache[max_ext] = sc.build_frames(runner.market, st, runner.timeframes)
        frames = frames_cache[max_ext]

        d = _summ(_run(frames, runner, st, "dev"))
        t = _summ(_run(frames, runner, st, "test"))
        f_res = _run(frames, runner, st, "full")
        f = _summ(f_res)
        x = _summ(_run(frames, runner, st, "full", 1.5))
        ac = _accept_split(f_res)

        print(f"{label:46s} | {d['net']:+8.2f} | {t['net']:+8.2f} | "
              f"{f['net']:+8.2f}({f['n']:3d},{f['wr']:4.1f}%,{f['pf']:4.2f}) | "
              f"{f['ex10']:+8.2f} | {x['net']:+8.2f} | "
              f"{ac['l_n']:2d}/{ac['l_net']:+7.2f}U   | "
              f"{ac['s_n']:2d}/{ac['s_net']:+7.2f}U   | "
              f"{ac['w_fills']:2d}/{ac['w_armed']:3d}/{ac['w_played']:3d}")


if __name__ == "__main__":
    main()

