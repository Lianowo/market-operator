"""A/B:振幅闸(min_bar_impulse_atr) vs 结构确认(struct_confirm_reject)。

底座固定为「早确认」:冻结表 U1 触发 + imp=0。
判据(按用户口径):`R>=1.5` 桶净额/笔数、`<0.7R` 桶是否为正、1.5x 是否转正。
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

TMP = ROOT / "var" / "tmp"
TMP.mkdir(parents=True, exist_ok=True)
U1 = {"2h": 900, "4h": 1800, "12h": 3600, "1d": 7200}


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
        router_params_by_tf=dict(st.router_by_timeframe), enable_mic=True)


def _rstats(res) -> tuple:
    rm = {}
    for r in res.ledger:
        if r["kind"] in ("OPEN", "LIMIT_FILL"):
            m = r.get("meta") or {}
            if m.get("r_mult") is not None:
                rm[(r["symbol"], r["ts"])] = float(m["r_mult"])
    rs = sorted(rm.values())
    if not rs:
        return 0.0, 0.0, {}
    b = {"<0.7": [], "0.7-1.0": [], "1.0-1.5": [], ">=1.5": []}
    for t in res.trades:
        if not t.get("closed"):
            continue
        r = rm.get((t["campaign"], t["open_ts"]))
        if r is None:
            continue
        k = ("<0.7" if r < 0.7 else "0.7-1.0" if r < 1.0
             else "1.0-1.5" if r < 1.5 else ">=1.5")
        b[k].append(t)
    out = {}
    for k, arr in b.items():
        net = sum(float(x["net_pnl"]) for x in arr)
        out[k] = (len(arr), net)
    return (statistics.median(rs), sum(1 for x in rs if x < 1.0) / len(rs) * 100.0, out)


def _family(res) -> str:
    cl = [t for t in res.trades if t.get("closed")]
    parts = []
    for fam in ("ACCEPT", "REJECT"):
        arr = [t for t in cl if fam in str(t.get("event_id"))]
        if arr:
            w = sum(1 for x in arr if x["net_pnl"] > 0)
            parts.append(f"{fam} N={len(arr):3d} WR={w/len(arr)*100:4.1f}% "
                         f"net={sum(float(x['net_pnl']) for x in arr):+7.2f}U")
    return " | ".join(parts)


def _runner_with(struct_on: bool):
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    raw["struct_confirm_reject"] = struct_on
    p = TMP / f"_cfg_struct_{int(struct_on)}.json"
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return sc.ScenarioRunner(tuple(raw["symbols"]), p, progress=False)


def _variant(runner, label: str) -> None:
    st = runner.settings
    rp = dc(st.router, min_bar_impulse_atr=0.0)
    rpm = {k: dc(v, min_bar_impulse_atr=0.0)
           for k, v in st.router_by_timeframe.items()}
    st2 = dc(st, trigger_seconds_by_timeframe=U1, router=rp, router_by_timeframe=rpm)
    frames = sc.build_frames(runner.market, st2, runner.timeframes)
    d = _summ(_run(frames, runner, st2, "dev"))
    t = _summ(_run(frames, runner, st2, "test"))
    fr = _run(frames, runner, st2, "full")
    f = _summ(fr)
    x = _summ(_run(frames, runner, st2, "full", 1.5))
    med, lt1, buckets = _rstats(fr)
    print(f"{label:30s} | dev:{d['net']:+8.2f} | test:{t['net']:+8.2f} | "
          f"full:{f['net']:+8.2f}({f['n']:4d},{f['wr']:4.1f}%,{f['pf']:4.2f}) | "
          f"去Top10:{f['ex10']:+8.2f} | 1.5x:{x['net']:+8.2f} | R中位 {med:4.2f}/{lt1:4.1f}%")
    print(f"    └ {_family(fr)}")
    print("    └ " + " | ".join(f"{k}: n={v[0]:3d} net={v[1]:+7.2f}U"
                               for k, v in buckets.items()))


def main() -> None:
    print("=" * 150)
    print("底座 = 早确认(冻结表 U1 触发 + imp=0)")
    print("-" * 150)
    _variant(_runner_with(False), "B1 振幅闸全放开(imp=0)")
    _variant(_runner_with(True), "B2 结构确认(struct=on)")


if __name__ == "__main__":
    main()
