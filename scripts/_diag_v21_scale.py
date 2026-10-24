"""v21.6 时间尺度级联 + 出场推进粒度判定台。

三问(全部单变量、同一装配、同一段时间轴):
  S1 `1h` 观察周期本身值不值得交易(solo,const/history 两口径);
  S2 `2h` 战役的进出场往小尺度级联(触发 = 1h / 30m / 无)是否有提升;
  S3 出场推进粒度(认知收盘 vs 每根已收盘执行细 bar)值多少
     —— 对所有观察周期通用,不只 2h。

臂:
  solo_2h        2h,触发无(T0 的 2h 腿)
  solo_2h_t1h    2h,触发 1h
  solo_2h_t30m   2h,触发 30m
  solo_1h        1h,触发无
  solo_1h_t30m   1h,触发 30m
  port_2h_1h     2h(无触发)+ 1h(无触发)
  port_2h_t1h_1h 2h(触发 1h)+ 1h(无触发)
每个臂再乘 `--xexec`(arm.exit_exec=True)得到 `<臂>_xexec` 对照。

用法:
    python scripts/_diag_v21_scale.py --segments dev --funding const
    python scripts/_diag_v21_scale.py --arms solo_1h,solo_1h_t30m --funding history
    python scripts/_diag_v21_scale.py --arms solo_2h --xexec --segments dev,test

输出:`reports/scale_20261024/scale.json` + 每臂 `trades_<臂>.csv` + 终端表。
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

from market_operator.campaign import run_campaigns                 # noqa: E402
from market_operator.scenario import load_funding_rates            # noqa: E402
from _diag_v21_abcd import Ctx, HTF, TF_ORDER, metrics             # noqa: E402
from _diag_v21_p012 import tail_stats, e_r                         # noqa: E402

OUTDIR = ROOT / "reports" / "scale_20261024"

# 臂 = (观察周期集合, 触发映射);触发映射只对出现的周期生效
ARMS: dict = {
    "solo_2h":        (("2h",), {}),
    "solo_2h_t1h":    (("2h",), {"2h": 3600}),
    "solo_2h_t30m":   (("2h",), {"2h": 1800}),
    "solo_1h":        (("1h",), {}),
    "solo_1h_t30m":   (("1h",), {"1h": 1800}),
    "port_2h_1h":     (("1h", "2h"), {}),
    "port_2h_t1h_1h": (("1h", "2h"), {"2h": 3600}),
}


def build(ctx: Ctx, frames: dict, *, segment: str, funding: str,
          exit_exec: bool, exit_exec_trail: bool = True, cm: float = 1.0):
    """一次回放。caps / rp_by_tf 对 `1h` 的取值口径见下(与 2h 同档,显式声明)。"""
    cfg = ctx.cfg
    caps = dict(cfg["tf_cap_by_tf"])
    for tf in TF_ORDER:
        if tf not in caps:
            # 新增观察周期沿用 2h 的单周期风险池(0.0225);
            # 不改 config/strategy.json —— 线上跑的是既定周期集合。
            caps[tf] = caps["2h"]
    rp_btf = dict(ctx.rp_by_tf)
    if "1h" not in rp_btf:
        # 1h 无专属阈值:按 2h 的 router_params_by_tf 照抄(唯一诚实假设:
        # 同一 balance 箱识别器在更细尺度上尚未单独校准,先用 2h 的门槛)。
        rp_btf["1h"] = dc_replace(ctx.base_rp,
                                  **cfg["router_params_by_tf"]["2h"])
    arm = ctx.arm
    if exit_exec:
        arm = dc_replace(arm, exit_exec=True,
                         exit_exec_trail=exit_exec_trail)
    rates = load_funding_rates(ctx.syms) if funding == "history" else None
    return run_campaigns(
        frames, segment=segment, dev_frac=float(cfg["dev_frac"]),
        equity0=300.0, risk_frac=float(cfg["risk_frac"]),
        leverage_cap=float(cfg["leverage_cap"]),
        taker_fee=float(cfg["taker_fee"]), maker_fee=float(cfg["maker_fee"]),
        slippage_per_side=float(cfg["slippage_per_side"]),
        funding_per_8h=float(cfg["funding_per_8h_per_side"]),
        cost_multiplier=cm, contracts=ctx.contracts, params=ctx.params,
        n_fill=int(cfg["n_fill"]),
        portfolio_cap_frac=float(cfg["portfolio_cap_frac"]),
        tf_cap_by_tf=caps, arm=arm, router_params=ctx.base_rp,
        enable_mic=True, router_params_by_tf=rp_btf,
        funding_rates=rates)


def summarize(res) -> dict:
    m = metrics(res)
    trades = m.pop("trades")
    closed = [t for t in trades if t.get("closed")]
    by_tf = {}
    for tf in TF_ORDER:
        sub = [t for t in closed if t.get("tf") == tf]
        if not sub:
            continue
        ts_sub = tail_stats(sub)
        by_tf[tf] = {"n": len(sub),
                     "net": round(sum(t["net_pnl"] for t in sub), 2),
                     "e_r": round(e_r(sub) or 0.0, 4),
                     "funding": round(sum(t["funding"] for t in sub), 2),
                     "give_back_r": ts_sub["give_back_r_mean"],
                     "hold_bars": ts_sub["hold_bars_mean"]}
    out = {k: v for k, v in m.items()
           if k in ("n", "gross", "cost", "net", "eq", "wr", "pf", "dd_pct")}
    out["e_r"] = round(e_r(closed) or 0.0, 4)
    out["by_tf"] = by_tf
    out["funding_total"] = round(sum(t["funding"] for t in closed), 2)
    out["tail"] = tail_stats(closed)
    out["exit_exec_ticks"] = int((m.get("counters") or {}).get(
        "exit_exec_ticks", 0))
    out["trades"] = closed
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--segments", default="dev")
    ap.add_argument("--funding", default="const,history")
    ap.add_argument("--cm", default="1.0")
    ap.add_argument("--xexec", action="store_true",
                    help="每个臂再跑一份 exit_exec=True 对照")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--out", default=str(OUTDIR))
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    only = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    names = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in names if a not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arms {unknown}; known={list(ARMS)}")
    tfs = sorted({tf for n in names for tf in ARMS[n][0]})

    t0 = time.time()
    ctx = Ctx(only=only, tfs=tfs, trig_map={})
    print(f"[prep] {time.time() - t0:.1f}s syms={ctx.syms} tfs={list(tfs)}",
          flush=True)

    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    fundings = [s.strip() for s in args.funding.split(",") if s.strip()]
    cms = [float(x) for x in args.cm.split(",") if x.strip()]
    variants = [(False, True, "")]
    if args.xexec:
        # _xexec = 目标/锁利/移动防线全部搬到执行细 bar;
        # _xlock = 只提前目标与锁利,trail 仍留认知收盘(拆来源用)
        variants += [(True, True, "_xexec"), (True, False, "_xlock")]

    payload: dict = {"note": "1h 观察周期执行帧 = 15m;2h/4h 执行帧 = 1h",
                     "arms": {}}
    print(f"{'arm':22s} {'seg':5s} {'fund':7s} {'cm':>4s} | {'N':>4s} "
          f"{'net':>9s} {'E[R]':>8s} {'fund$':>8s} {'DD%':>6s} | "
          f"{'giveBack':>9s} {'ticks':>6s} | per-TF net", flush=True)
    for name in names:
        arm_tfs, trig_map = ARMS[name]
        frames = ctx.frames_for(arm_tfs, trig_map)
        for seg in segs:
            for funding in fundings:
                for cm in cms:
                    for xexec, xtrail, suffix in variants:
                        t1 = time.time()
                        res = build(ctx, frames, segment=seg, funding=funding,
                                    exit_exec=xexec, exit_exec_trail=xtrail,
                                    cm=cm)
                        s = summarize(res)
                        trades = s.pop("trades")
                        s["run_s"] = round(time.time() - t1, 1)
                        key = f"{name}{suffix}__{seg}__{funding}__{cm:g}"
                        payload["arms"][key] = s
                        tf_txt = " ".join(f"{tf}={v['net']:+.1f}"
                                          for tf, v in s["by_tf"].items())
                        gb = s["tail"]["give_back_r_mean"]
                        gb_txt = f"{gb:9.3f}" if gb is not None else "      n/a"
                        print(f"{name + suffix:22s} {seg:5s} {funding:7s} "
                              f"{cm:4.1f} | {s['n']:4d} {s['net']:+9.1f} "
                              f"{s['e_r']:+8.4f} {s['funding_total']:+8.1f} "
                              f"{s['dd_pct']:6.2f} | {gb_txt} "
                              f"{s['exit_exec_ticks']:6d} | {tf_txt} "
                              f"({s['run_s']}s)", flush=True)
                        if not xexec:
                            cols = ("campaign", "tf", "symbol", "direction",
                                    "open_ts", "exit_ts", "gross_pnl", "fees",
                                    "funding", "net_pnl", "risk_usdt",
                                    "r_multiple", "mfe_r", "mae_r",
                                    "hold_bars", "n_partial_legs")
                            path = outdir / f"trades_{key}.csv"
                            with path.open("w", newline="",
                                           encoding="utf-8") as fh:
                                w = csv.DictWriter(fh, fieldnames=cols,
                                                   extrasaction="ignore")
                                w.writeheader()
                                for t in trades:
                                    if (t.get("closed")
                                            and t.get("in_segment_open")):
                                        w.writerow({k: t.get(k) for k in cols})

    (outdir / "scale.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[out] {outdir / 'scale.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

