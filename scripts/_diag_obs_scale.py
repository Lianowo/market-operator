"""观察尺度研究:区域尺度固定,观察尺度可变;并量化「信号成立时还剩多少 R」。

回答的问题不是「4h 用 15m 还是 30m 胜率更高」,而是:
**一个区域机会,在什么观察尺度上/在什么 R_available 区间,扣费后才是正期望?**

口径:
- 区域(认知)尺度固定 = 2h/4h/12h/1d(不可被观察尺度改写);
- 观察(触发)尺度由 `trigger_tf_by_htf` 决定,只改分辨率,不改区域定义;
- `r_mult` = 信号成立时「入场→最近有效目标」的距离 ÷ 「入场→真实失效位」的距离,
  即账本 meta 里的计划盈亏比 ⇒ 它就是信号时刻的 R_available(未扣成本)。
- 逐变体报告:dev/test/full/去Top10/1.5x + 入场 R_available 的中位数与低空间占比。
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


def _r_stats(res) -> tuple[float, float, float, int]:
    """入场时的 R_available(计划 r_mult)分布:中位、<1.0 占比、<1.5 占比、样本数。"""
    opens = [r for r in res.ledger if r["kind"] in ("OPEN", "LIMIT_FILL")]
    rs = [float(r["meta"]["r_mult"]) for r in opens
          if (r.get("meta") or {}).get("r_mult") is not None]
    if not rs:
        return (0.0, 0.0, 0.0, 0)
    rs.sort()
    return (statistics.median(rs),
            sum(1 for x in rs if x < 1.0) / len(rs) * 100.0,
            sum(1 for x in rs if x < 1.5) / len(rs) * 100.0, len(rs))


def _r_buckets(res) -> list[tuple]:
    """按入场 R_available 分桶的**真实结果**:检验「R 低 ⇒ 不该做」是否成立。"""
    r_by_key = {}
    for r in res.ledger:
        if r["kind"] in ("OPEN", "LIMIT_FILL"):
            meta = r.get("meta") or {}
            if meta.get("r_mult") is not None:
                r_by_key[(r["symbol"], r["ts"])] = float(meta["r_mult"])
    buckets = {"<0.7": [], "0.7-1.0": [], "1.0-1.5": [], ">=1.5": []}
    for t in res.trades:
        if not t.get("closed"):
            continue
        rm = r_by_key.get((t["campaign"], t["open_ts"]))
        if rm is None:
            continue
        key = ("<0.7" if rm < 0.7 else "0.7-1.0" if rm < 1.0
               else "1.0-1.5" if rm < 1.5 else ">=1.5")
        buckets[key].append(t)
    out = []
    for k, arr in buckets.items():
        if not arr:
            out.append((k, 0, 0.0, 0.0, 0.0))
            continue
        net = sum(float(x["net_pnl"]) for x in arr)
        wr = sum(1 for x in arr if x["net_pnl"] > 0) / len(arr) * 100.0
        exp = statistics.mean([float(x["net_pnl"]) / float(x["risk_usdt"])
                               for x in arr if x.get("risk_usdt")])
        out.append((k, len(arr), net, wr, exp))
    return out



def evaluate(label: str, frames, runner, st) -> None:
    d = _summ(_run(frames, runner, st, "dev"))
    t = _summ(_run(frames, runner, st, "test"))
    f_res = _run(frames, runner, st, "full")
    f = _summ(f_res)
    x = _summ(_run(frames, runner, st, "full", 1.5))
    med, lt1, lt15, n_r = _r_stats(f_res)
    print(f"{label:34s} | dev:{d['net']:+7.2f} | test:{t['net']:+7.2f} | "
          f"full:{f['net']:+7.2f}(N={f['n']:3d},WR={f['wr']:4.1f}%,PF={f['pf']:4.2f}) | "
          f"去Top10:{f['ex10']:+7.2f} | 1.5x:{x['net']:+7.2f} | "
          f"R中位:{med:4.2f} <1R:{lt1:4.1f}% <1.5R:{lt15:4.1f}%(n={n_r})")
    if SHOW_BUCKETS:
        for k, n, net, wr, exp in _r_buckets(f_res):
            print(f"      R_available {k:8s} | N={n:3d} | WR={wr:5.1f}% | "
                  f"净利={net:+7.2f}U | 均净={exp:+.3f}R")


VARIANTS = (
    ("V0 基线 2h自身/4h→2h/12h→2h/1d→4h",
     {"2h": None, "4h": 7200, "12h": 7200, "1d": 14400}),
    ("U1 用户冻结表 2h→15m/4h→30m/12h→1h/1d→2h",
     {"2h": 900, "4h": 1800, "12h": 3600, "1d": 7200}),
    ("U2 用户辅助列 2h→30m/4h→1h/12h→2h/1d→4h",
     {"2h": 1800, "4h": 3600, "12h": 7200, "1d": 14400}),
    ("C6 仅高阶加速 2h→30m/4h→2h/12h→1h/1d→2h",
     {"2h": 1800, "4h": 7200, "12h": 3600, "1d": 7200}),
    ("C7 只加速高阶(2h/4h不动) 12h→1h/1d→2h",
     {"2h": None, "4h": 7200, "12h": 3600, "1d": 7200}),
)

SHOW_BUCKETS = bool(int(__import__("os").environ.get("SHOW_BUCKETS", "0")))



def main() -> None:
    raw = json.loads(sc.PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    syms = tuple(raw["symbols"])
    runner = sc.ScenarioRunner(syms, sc.PRODUCTION_CONFIG, progress=False)
    st = runner.settings
    print("=" * 152)
    print(f"{'变体':34s} | {'dev净利':>10s} | {'test净利':>10s} | "
          f"{'full@1.0x (N, WR, PF)':>28s} | {'去Top10':>10s} | {'1.5x':>10s} | "
          f"入场 R_available")
    print("-" * 152)
    for label, trig in VARIANTS:
        st2 = dc(st, trigger_seconds_by_timeframe=trig)
        try:
            frames = sc.build_frames(runner.market, st2, runner.timeframes)
        except Exception as e:                     # noqa: BLE001
            print(f"{label:34s} | 跳过 {type(e).__name__}: {e}")
            continue
        evaluate(label, frames, runner, st2)


if __name__ == "__main__":
    main()
