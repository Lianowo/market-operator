"""P0/P1 判定台:A1 真实资金费 · A2 右尾量度 · A5 同集反事实 · B1 标的池 · B2 慢周期保留 · C1 目标R上限 · C3 12h 结构臂。

用法:
  python scripts/_diag_v21_p012.py [--out reports/p012_20261023]

设计原则:
- 全部臂共用同一份装配(Ctx)与同一段时间轴(segment),只改一个开关;
- A1 的 ``const`` 臂必须与 a0 基线逐位一致(验收由 tools/replay_acceptance.py 另跑);
- 每个臂输出 ``net / E[R] / 每周期`` + ``反事实成本倍数``;A2 的右尾量度只做
  描述统计(不参与判定),避免用新指标反过来挑参数。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator.campaign import run_campaigns                  # noqa: E402
from market_operator.replay import counterfactual_summary           # noqa: E402
from market_operator.scenario import load_funding_rates             # noqa: E402
from _diag_v21_abcd import Ctx, HTF, metrics                        # noqa: E402


def e_r(trades: list) -> float | None:
    risk = sum(float(t.get("risk_usdt") or 0.0) for t in trades)
    if risk <= 0:
        return None
    return sum(float(t["net_pnl"]) for t in trades) / risk


def tail_stats(trades: list) -> dict:
    """A2:右尾描述统计(全部以 R 为单位,分母与 r_multiple 相同)。"""
    mfe = [t["mfe_r"] for t in trades if t.get("mfe_r") is not None]
    mae = [t["mae_r"] for t in trades if t.get("mae_r") is not None]
    gap = [t["mfe_r"] - t["r_multiple"] for t in trades
           if t.get("mfe_r") is not None and t.get("r_multiple") is not None]
    holds = [t["hold_bars"] for t in trades if t.get("hold_bars") is not None]
    ok = sum(1 for t in trades if t.get("mfe_r") is not None
             and t.get("r_multiple") is not None
             and t["mfe_r"] + 1e-9 >= t["r_multiple"])
    nets = sorted((float(t["net_pnl"]) for t in trades), reverse=True)
    total = sum(nets)
    return {
        "mfe_r_mean": round(sum(mfe) / len(mfe), 4) if mfe else None,
        "mae_r_mean": round(sum(mae) / len(mae), 4) if mae else None,
        # 留在桌上 = MFE − 实际 R:出场类改动的右尾成本直接读数
        "give_back_r_mean": round(sum(gap) / len(gap), 4) if gap else None,
        "mfe_ge_real_share": round(ok / len(gap), 4) if gap else None,
        "top5_share": round(sum(nets[:5]) / total, 4) if total > 0 else None,
        "hold_bars_mean": round(sum(holds) / len(holds), 3) if holds else None,
        "partial_legs": sum(int(t.get("n_partial_legs") or 0) for t in trades),
    }


def summarize(res) -> dict:
    m = metrics(res)
    trades = m.pop("trades")
    closed = [t for t in trades if t.get("closed")]
    by_tf = {}
    for tf in HTF:
        sub = [t for t in closed if t.get("tf") == tf]
        if not sub:
            continue
        by_tf[tf] = {"n": len(sub),
                     "net": round(sum(t["net_pnl"] for t in sub), 2),
                     "e_r": round(e_r(sub) or 0.0, 4),
                     "funding": round(sum(t["funding"] for t in sub), 2)}
    out = {k: v for k, v in m.items() if k in
           ("n", "gross", "cost", "net", "eq", "wr", "pf", "dd_pct")}
    out["e_r"] = round(e_r(closed) or 0.0, 4)
    out["by_tf"] = by_tf
    out["funding_total"] = round(sum(t["funding"] for t in closed), 2)
    out["fees_total"] = round(sum(t["fees"] for t in closed), 2)
    out["tail"] = tail_stats(closed)
    out["counterfactual"] = counterfactual_summary(closed)
    out["counters"] = {k: v for k, v in (m.get("counters") or {}).items()
                       if k in ("target_r_clamped", "portfolio_cap_skips")}
    return out


def build(ctx: Ctx, *, frames=None, funding="const", symbol_cap=None,
          reserve=None, max_target_r=None):
    cfg = ctx.cfg
    frames = ctx.frames if frames is None else frames
    rp_btf = {tf: (dc_replace(p, max_target_r=max_target_r)
                   if max_target_r else p)
              for tf, p in ctx.rp_by_tf.items()}
    rates = load_funding_rates(ctx.syms) if funding == "history" else None
    keys = tuple(frames)
    caps = {tf: v for tf, v in cfg["tf_cap_by_tf"].items()
            if any(f.endswith(f"@{tf}") for f in keys)}
    return run_campaigns(
        frames, segment="full", dev_frac=float(cfg["dev_frac"]),
        equity0=300.0, risk_frac=float(cfg["risk_frac"]),
        leverage_cap=float(cfg["leverage_cap"]),
        taker_fee=float(cfg["taker_fee"]), maker_fee=float(cfg["maker_fee"]),
        slippage_per_side=float(cfg["slippage_per_side"]),
        funding_per_8h=float(cfg["funding_per_8h_per_side"]),
        cost_multiplier=1.0, contracts=ctx.contracts, params=ctx.params,
        n_fill=int(cfg["n_fill"]),
        portfolio_cap_frac=float(cfg["portfolio_cap_frac"]),
        tf_cap_by_tf=caps, arm=ctx.arm, router_params=ctx.base_rp,
        enable_mic=True, router_params_by_tf=rp_btf,
        funding_rates=rates, symbol_stop_risk_frac=symbol_cap,
        reserve_by_tf=reserve)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "p012_20261023"))
    ap.add_argument("--only", default=None, help="逗号分隔的臂名子集")
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    ctx = Ctx()
    print(f"[prep] {time.time() - t0:.1f}s keys={len(ctx.frames)}", flush=True)

    frames_no12h = {k: v for k, v in ctx.frames.items()
                    if not k.endswith("@12h")}
    arms = {
        "base_const": dict(),
        "base_hist": dict(funding="history"),
        "B1_sym3": dict(funding="history",
                        symbol_cap={s: 0.03 for s in ctx.syms}),
        "B1_sym45": dict(funding="history",
                         symbol_cap={s: 0.045 for s in ctx.syms}),
        "B2_reserve_slow": dict(funding="history",
                                reserve={"12h": 0.0225, "1d": 0.0225}),
        "B2_reserve_15": dict(funding="history",
                              reserve={"12h": 0.015, "1d": 0.015}),
        "B2_reserve_075": dict(funding="history",
                               reserve={"12h": 0.0075, "1d": 0.0075}),
        "C1_target4r": dict(funding="history", max_target_r=4.0),
        "C1_target8r": dict(funding="history", max_target_r=8.0),
        "C3_no12h_const": dict(frames=frames_no12h),
        "C3_no12h_hist": dict(frames=frames_no12h, funding="history"),
    }
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        arms = {k: v for k, v in arms.items() if k in want}

    out: dict = {"segment": "full", "symbols": list(ctx.syms),
                 "prep_s": round(time.time() - t0, 1), "arms": {}}
    for name, kw in arms.items():
        t1 = time.time()
        res = build(ctx, **kw)
        s = summarize(res)
        s["run_s"] = round(time.time() - t1, 1)
        out["arms"][name] = s
        print(f"[{name}] n={s['n']} net={s['net']:+.2f} E[R]={s['e_r']:+.4f} "
              f"fund={s['funding_total']:+.2f} ({s['run_s']}s)", flush=True)
        if name.startswith("base"):
            # F4:逐笔账本落盘(块自助法的输入;只存判定所需字段)
            cols = ("campaign", "tf", "symbol", "direction", "open_ts", "exit_ts",
                    "gross_pnl", "fees", "funding", "net_pnl", "risk_usdt",
                    "r_multiple", "mfe_r", "mae_r", "hold_bars")
            path = out_dir / f"trades_{name}.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for t in res.trades:
                    if t.get("closed") and t.get("in_segment_open"):
                        w.writerow({k: t.get(k) for k in cols})
            print(f"  [trades] {path}")

    (out_dir / "p012.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[out] {out_dir / 'p012.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
