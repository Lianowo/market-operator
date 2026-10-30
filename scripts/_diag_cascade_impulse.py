"""级联 × 冲动闸:验证「细尺度早确认被 impulse 闸挡住」这一假设。

背景疑惑(用户):"现在不就是因为没级联到小级别去确认接受/拒绝么?确认早了
价格还在区域远端,所以位置好;现在确认晚了,价格已经走了一部分。"

已知读数:单纯把观察尺度调细(2h→15m/30m 等)不但没改善,还把唯一赚钱的
`R>=1.5` 桶打薄了。一个尚未排除的混淆项是:

    `min_bar_impulse_atr = 1.2`(2h/4h)
    —— 它要求**触发 K 线自身振幅 ≥ 1.2·ATR** 才放行。

细尺度的「早确认」恰恰发生在**小振幅**的确认 K 线上(刚回到带内、幅度很小),
所以它会被这道闸**系统性挡掉**;最终成交的仍是"走了一大段之后的大 K 线"。
若假设成立,则「级联 + 释放冲动闸」应当同时出现:
  ① 入场 R_available 中位数上移;② `R>=1.5` 桶笔数增加;③ 净利不下降。

复现:python scripts/_diag_cascade_impulse.py
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


def _rmap(res) -> dict:
    out = {}
    for r in res.ledger:
        if r["kind"] in ("OPEN", "LIMIT_FILL"):
            m = r.get("meta") or {}
            if m.get("r_mult") is not None:
                out[(r["symbol"], r["ts"])] = float(m["r_mult"])
    return out


def _r_stats(res) -> tuple:
    rs = sorted(_rmap(res).values())
    if not rs:
        return (0.0, 0.0, 0.0, 0)
    return (statistics.median(rs),
            sum(1 for x in rs if x < 1.0) / len(rs) * 100.0,
            sum(1 for x in rs if x < 1.5) / len(rs) * 100.0, len(rs))


def _family(res) -> str:
    cl = [t for t in res.trades if t.get("closed")]
    parts = []
    for fam, ids in (("ACCEPT", ("ACCEPT",)), ("REJECT", ("REJECT",))):
        arr = [t for t in cl if any(i in str(t.get("event_id")) for i in ids)]
        if arr:
            w = sum(1 for x in arr if x["net_pnl"] > 0)
            parts.append(f"{fam} N={len(arr):3d} WR={w / len(arr) * 100:4.1f}% "
                         f"net={sum(float(x['net_pnl']) for x in arr):+7.2f}U")
    return " | ".join(parts)


def _buckets(res) -> str:
    rm = _rmap(res)
    b = {"<0.7": [], "0.7-1.0": [], "1.0-1.5": [], ">=1.5": []}
    for t in res.trades:
        if not t.get("closed"):
            continue
        r = rm.get((t["campaign"], t["open_ts"]))
        if r is None:
            continue
        k = ("<0.7" if r < 0.7 else "0.7-1.0" if r < 1.0
             else "1.0-1.5" if r < 1.5 else ">=1.5")
        b[k].append(float(t["net_pnl"]))
    return " | ".join(f"{k}: n={len(v):3d} net={sum(v):+7.2f}U"
                      for k, v in b.items())


VARIANTS = (
    ("V0 基线(2h自身/4h→2h/12h→2h/1d→4h)", 
     {"2h": None, "4h": 7200, "12h": 7200, "1d": 14400}, {}),
    ("A1 冻结表 U1(2h→15m/4h→30m/12h→1h/1d→2h)",
     {"2h": 900, "4h": 1800, "12h": 3600, "1d": 7200}, {}),
    ("A2 U1 + 释放冲动闸(imp=0)",
     {"2h": 900, "4h": 1800, "12h": 3600, "1d": 7200},
     {"min_bar_impulse_atr": 0.0}),
    ("A3 U2 + 释放冲动闸(imp=0)",
     {"2h": 1800, "4h": 3600, "12h": 7200, "1d": 14400},
     {"min_bar_impulse_atr": 0.0}),
    ("A4 冻结表 + imp=0 + 同步冷却(cd=0)",
     {"2h": 900, "4h": 1800, "12h": 3600, "1d": 7200},
     {"min_bar_impulse_atr": 0.0, "zone_cooldown_bars": 0}),
)


def main() -> None:
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    syms = tuple(raw["symbols"])
    runner = sc.ScenarioRunner(syms, sc.PRODUCTION_CONFIG, progress=False)
    st = runner.settings
    show_b = bool(int(__import__("os").environ.get("SHOW_BUCKETS", "0")))
    print("=" * 150)
    print(f"{'变体':44s} | {'dev':>8s} | {'test':>8s} | "
          f"{'full(N,WR,PF)':>26s} | {'去Top10':>9s} | {'1.5x':>8s} | R中位/<1R")
    print("-" * 150)
    for label, trig, over in VARIANTS:
        rp = dc(st.router, **over)
        rpm = {k: dc(v, **over) for k, v in st.router_by_timeframe.items()}
        st2 = dc(st, trigger_seconds_by_timeframe=trig, router=rp,
                 router_by_timeframe=rpm)
        try:
            frames = sc.build_frames(runner.market, st2, runner.timeframes)
        except Exception as e:                      # noqa: BLE001
            print(f"{label:44s} | 跳过 {type(e).__name__}: {e}")
            continue
        d = _summ(_run(frames, runner, st2, "dev"))
        t = _summ(_run(frames, runner, st2, "test"))
        fr = _run(frames, runner, st2, "full")
        f = _summ(fr)
        x = _summ(_run(frames, runner, st2, "full", 1.5))
        med, lt1, _lt15, n_r = _r_stats(fr)
        print(f"{label:44s} | {d['net']:+8.2f} | {t['net']:+8.2f} | "
              f"{f['net']:+8.2f}({f['n']:3d},{f['wr']:4.1f}%,{f['pf']:4.2f}) | "
              f"{f['ex10']:+9.2f} | {x['net']:+8.2f} | {med:4.2f}/{lt1:4.1f}%")
        print(f"    └ {_family(fr)}")
        if show_b:
            print(f"    └ {_buckets(fr)}")


if __name__ == "__main__":
    main()
