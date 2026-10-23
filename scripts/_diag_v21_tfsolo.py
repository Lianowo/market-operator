"""单时间尺度隔离回放(诊断,非生产链路)。

目的:回答"每个 TF 单独成链时的净收益/胜率/PF/DD 是多少",从而把
**组合链路里的时间尺度交互**(共享 4 槽、共享 300U 复利、组合容量闸)从
**单尺度本身的边缘**中分离出来。

做法:复用 `_diag_v21_abcd.Ctx`(与 a0 逐位相同的帧构建 / 参数装配),
只把 `frames` 过滤成单个 `tf_label` 后再 `run_campaigns` —— 除"参与回放的
时间尺度集合"外,其余(diag 数据、RouterParams 按 TF 覆盖、arm、成本档、
segment 切分)全部与 a0 相同。

指标口径:同时给出 **毛 / 净** 两套 WR·PF(此前 `_diag_v21_abcd.metrics`
只给毛口径,见本轮 §1 的口径缺口)。

用法:
    python scripts/_diag_v21_tfsolo.py --parity          # 先验证器:a0 全链复现
    python scripts/_diag_v21_tfsolo.py --tfs 2h --segments full --cm 1.0
    python scripts/_diag_v21_tfsolo.py                   # 全部 TF × dev/full × 1.0/1.5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _diag_v21_abcd import HTF, Ctx, dump_trades                    # noqa: E402
from market_operator.campaign import run_campaigns                  # noqa: E402

OUTDIR = ROOT / "reports" / "tf_solo_20261023"
REF = ROOT / "reports" / "tf_trigger_exp" / "closure_20261023"
BOOKS = {"explore": ("BTCUSDT", "ETHUSDT", "SOLUSDT"), "book4": None}


def solo_metrics(res, equity0: float = 300.0) -> dict:
    """毛/净双口径指标(逐笔口径与 `_diag_v21_abcd.metrics` 一致,只是拆开)。"""
    c = [t for t in res.trades if t.get("closed") and t.get("in_segment_open")]
    if not c:
        return {"n": 0}
    gross = sum(t["gross_pnl"] for t in c)
    net = sum(t["net_pnl"] for t in c)
    cost = sum(t["fees"] + t["funding"] + t["slippage_cost"] for t in c)
    risk = sum(t["risk_usdt"] for t in c)

    def _wr_pf(key):
        w = [t[key] for t in c if t[key] > 0]
        l = [t[key] for t in c if t[key] <= 0]
        pf = (sum(w) / -sum(l)) if l and sum(l) < 0 else None
        payoff = (sum(w) / len(w)) / (-sum(l) / len(l)) if w and l else None
        return len(w) / len(c), pf, payoff

    wr_n, pf_n, pay_n = _wr_pf("net_pnl")
    wr_g, pf_g, pay_g = _wr_pf("gross_pnl")
    peak, dd = -1e18, 0.0
    max_pos = 0
    for row in res.equity_rows:
        e = float(row[1])
        peak = max(peak, e)
        if peak > 0:
            dd = max(dd, (peak - e) / peak)
        max_pos = max(max_pos, int(row[5]))

    by = {}
    for t in c:
        for dim, key in (("event", t.get("event_id")),
                         ("regime", t.get("regime")), ("symbol", t.get("symbol"))):
            b = by.setdefault(dim, {}).setdefault(str(key), {"n": 0, "net": 0.0})
            b["n"] += 1
            b["net"] += t["net_pnl"]
    for b in by.values():
        for v in b.values():
            v["net"] = round(v["net"], 4)
    return {
        "n": len(c), "gross": round(gross, 4), "cost": round(cost, 4),
        "net": round(net, 4), "eq": round(equity0 + net, 4),
        "wr_net": round(wr_n, 4), "pf_net": None if pf_n is None else round(pf_n, 4),
        "payoff_net": None if pay_n is None else round(pay_n, 4),
        "wr_gross": round(wr_g, 4),
        "pf_gross": None if pf_g is None else round(pf_g, 4),
        "payoff_gross": None if pay_g is None else round(pay_g, 4),
        "er_net": round(net / risk, 4) if risk else None,      # 净 R 期望
        "er_gross": round(gross / risk, 4) if risk else None,
        "cost_R": round(cost / risk, 4) if risk else None,     # 每笔成本(R 计)
        "dd_pct": round(dd * 100.0, 4),
        "max_concurrent": max_pos,
        "avg_hold_h": round(sum(t["hold_hours"] for t in c) / len(c), 2),
        "avg_risk_usdt": round(risk / len(c), 4),
        "limit_entries": sum(1 for t in c if t.get("entry_mode") == "limit"),
        "counters": {k: v for k, v in getattr(res, "counters", {}).items()
                     if isinstance(v, int) and v},
        "breakdown": by,
        "trades": c,
    }


def run_frames(ctx: Ctx, frames: dict, *, segment: str, cm: float):
    """a0 语义的 run_campaigns 调用(只换 frames;其余照抄 Ctx.run 的 a0 分支)。"""
    cfg = ctx.cfg
    return run_campaigns(
        frames, segment=segment, dev_frac=float(cfg["dev_frac"]),
        equity0=300.0, risk_frac=float(cfg["risk_frac"]),
        leverage_cap=float(cfg["leverage_cap"]),
        taker_fee=float(cfg["taker_fee"]), maker_fee=float(cfg["maker_fee"]),
        slippage_per_side=float(cfg["slippage_per_side"]),
        funding_per_8h=float(cfg["funding_per_8h_per_side"]),
        cost_multiplier=cm, contracts=ctx.contracts,
        params=ctx.params, n_fill=int(cfg["n_fill"]),
        portfolio_cap_frac=float(cfg["portfolio_cap_frac"]),
        tf_cap_by_tf=dict(cfg["tf_cap_by_tf"]), arm=ctx.arm,
        router_params=ctx.base_rp, enable_mic=True,
        router_params_by_tf=ctx.rp_by_tf)


def _header() -> None:
    print(f"{'book:tf':6s} {'seg':5s} {'x':>4s} | {'N':>4s} {'net':>8s} "
          f"{'gross':>8s} {'cost':>7s} | {'WRnet':>6s} {'PFnet':>5s} {'pay':>5s} "
          f"{'ER':>6s} {'costR':>6s} {'DD%':>6s} {'maxPos':>6s}", flush=True)


def _row(tag: str, seg: str, cm: float, m: dict, secs: float) -> None:
    def _f(x, w=5, p=2):
        return f"{x:{w}.{p}f}" if isinstance(x, (int, float)) else f"{'--':>{w}}"
    print(f"{tag:6s} {seg:5s} {cm:4.1f} | {m['n']:4d} {m['net']:+8.1f} "
          f"{m['gross']:+8.1f} {m['cost']:7.1f} | {_f(m['wr_net'], 6, 3)} "
          f"{_f(m['pf_net'], 5)} {_f(m['payoff_net'], 5)} {_f(m['er_net'], 6, 3)} "
          f"{_f(m['cost_R'], 6, 3)} {_f(m['dd_pct'], 6, 2)} "
          f"{m['max_concurrent']:6d} ({secs:.0f}s)", flush=True)


def _sel_frames(ctx: Ctx, book: str, tf: str | None) -> dict:
    syms = BOOKS[book]
    return {k: v for k, v in ctx.frames.items()
            if (tf is None or v.tf_label == tf)
            and (syms is None or v.symbol in syms)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--books", default="explore,book4")
    ap.add_argument("--tfs", default=",".join(HTF))
    ap.add_argument("--segments", default="dev,full")
    ap.add_argument("--cm", default="1.0,1.5")
    ap.add_argument("--out", default=str(OUTDIR))
    ap.add_argument("--parity", action="store_true",
                    help="先跑一次全链,与 closure 产物的 n/net/gross 对账(逐 book)")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ctx = Ctx(only=None)
    print(f"[prep] 帧就绪 ({time.time() - t0:.0f}s, {len(ctx.frames)} 战役)",
          flush=True)

    books = [b.strip() for b in args.books.split(",") if b.strip()]
    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    cms = [float(x) for x in args.cm.split(",") if x.strip()]
    tfs = [t.strip() for t in args.tfs.split(",") if t.strip()]

    bad = []
    for book in books:
        if args.parity:
            for seg in segs:
                for cm in cms:
                    t1 = time.time()
                    res = run_frames(ctx, _sel_frames(ctx, book, None),
                                     segment=seg, cm=cm)
                    m = solo_metrics(res)
                    _header()
                    _row(f"{book[:4]}", seg, cm, m, time.time() - t1)
                    ref_p = REF / book / f"metrics_a0__{seg}__{cm:g}.json"
                    if not ref_p.exists():
                        print(f"[parity] 缺参考 {ref_p}", flush=True)
                        continue
                    ref = json.loads(ref_p.read_text(encoding="utf-8"))
                    ok = (ref["n"] == m["n"]
                          and abs(ref["net"] - m["net"]) < 1e-6
                          and abs(ref["gross"] - m["gross"]) < 1e-6)
                    print(f"[parity] {book} {seg}@{cm:g} 参考 n={ref['n']} "
                          f"net={ref['net']:+.4f} gross={ref['gross']:+.4f} "
                          f"WR/PF(毛)={ref['wr']:.4f}/{ref['pf']:.4f} -> "
                          f"{'一致' if ok else '不一致!'}", flush=True)
                    if not ok:
                        bad.append(f"{book}/{seg}/{cm:g}")
            continue

        _header()
        for tf in tfs:
            frames = _sel_frames(ctx, book, tf)
            if not frames:
                print(f"[warn] {book} 无 {tf} 战役", flush=True)
                continue
            for seg in segs:
                for cm in cms:
                    t1 = time.time()
                    res = run_frames(ctx, frames, segment=seg, cm=cm)
                    m = solo_metrics(res)
                    _row(f"{'B4' if book == 'book4' else 'ex'}:{tf}",
                         seg, cm, m, time.time() - t1)
                    bdir = outdir / book
                    bdir.mkdir(parents=True, exist_ok=True)
                    stem = f"tfsolo_{tf}__{seg}__{cm:g}"
                    payload = {k: v for k, v in m.items() if k != "trades"}
                    (bdir / f"metrics_{stem}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    dump_trades(bdir / f"trades_{stem}.csv", m["trades"])
    if bad:
        raise SystemExit(f"parity 失败: {bad}")
    print(f"\n[out] {outdir}", flush=True)


if __name__ == "__main__":
    main()

