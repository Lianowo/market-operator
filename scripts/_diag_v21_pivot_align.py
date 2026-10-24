"""v21.6 前置研究:细尺度 pivot 能不能代理粗尺度 pivot(§8.3 移动防线)。

背景:上一轮证伪了「出场推进粒度」——`exit_exec` 与 `exit_exec_trail=False` 的
84 组读数逐位相同,因为 `_trail()` 只消费 `new_pivots`,而 pivot 只在认知收盘
确认。要让移动防线在 2h 内部动起来,唯一合法路径是**让细尺度(1h)先确认 pivot**。
但那等于换掉 §8.3 的结构输入 —— 动手前必须先证明两个尺度的 pivot 是**同一个
对象**,否则就是换策略而不是修时钟。

本脚本只读、不改策略、不跑回放。度量(按 kind 分开):

  C1 覆盖  粗 pivot 中能在细尺度找到同族对应者的比例。对应判据(kind 相同 +
           两个硬门):|Δprice| ≤ tol_atr·ATR_coarse(确认 bar 的冻结 atr)、
           |极值时间差| ≤ 1 个粗 bar。
  C2 提前量 粗确认时刻 − 细确认时刻(细 bar 根数);> 0 表示细尺度先知道。
  C3 噪声   细尺度独有的 pivot 占比 —— 提前量的代价。**必须按密度下限归一**:
           细/粗 pivot 数量比恒为 ~2(1h:2h)或 ~3(4h:12h),一对一时「细独有」
           的结构下限 = 1 − 粗/细,所以原始占比 0.50 / 0.67 本身不代表噪声。
           报 `noise_excess = noise / noise_floor`(1.0 = 恰在下限)。
  C4 偏差   |Δprice| / ATR_coarse 的分位(是不是同一个价位)。
  C5 可用   匹配对里细价位落在**收紧侧**的比例(`_trail` 只收紧,平局/更松无效)。

判据(先验,写死,不许事后改):
  覆盖 ≥ 0.80 且 提前≥1 细 bar 的比例 ≥ 0.50 且 noise_excess ≤ 1.10
  ⇒ 允许做 `pivot_exec`(细尺度 pivot 提前推进防线)实验;
  否则结论是「pivot 家族不同,不改」,与 exit_exec 的否决同一条逻辑。
  (首版把 C3 写成原始占比 ≤ 0.50,对 2:1 密度是**结构性不可达**的门,已按上述
   下限归一改正;这是口径纠错,不是看着结果放宽阈值 —— 原判据在 4h:12h 上同样
   把 0.676 与下限 0.668 读成「噪声超标」。)

参考对(2h:4h)给「合格匹配」定标:若 2h→4h 也只有 0.5 覆盖,说明度量本身够严,
1h→2h 的低覆盖不能单方面当否定证据。

用法:
    python scripts/_diag_v21_pivot_align.py
    python scripts/_diag_v21_pivot_align.py --symbols BTCUSDT --pairs 1h:2h
    python scripts/_diag_v21_pivot_align.py --tol-atr 0.5 --pairs 1h:2h,2h:4h

输出:`reports/scale_20261024/pivot_align.json` + 终端表。
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

import _diag_v21_backtest as diag                                  # noqa: E402
from market_operator import aggregate as agg                       # noqa: E402
from market_operator.structure import Params, PivotKind            # noqa: E402

CONFIG = ROOT / "config" / "strategy.json"
OUTDIR = ROOT / "reports" / "scale_20261024"


def _pivot_rows(cog) -> list:
    """[(kind, forming_ts, known_ts, price, atr)] 按极值时间升序。"""
    rows = []
    for i in sorted(cog.pivots_by):
        bar = cog.bars[i]
        atr = float(cog.atrs[i]) if i < len(cog.atrs) else 0.0
        for pv in cog.pivots_by[i]:
            fi = int(pv.forming_index)
            fts = int(cog.bars[fi][0]) if 0 <= fi < len(cog.bars) else int(bar[0])
            rows.append((pv.kind, fts, int(bar[0]), float(pv.price), atr))
    rows.sort(key=lambda r: (r[1], r[2]))
    return rows


def _match(coarse: list, fine: list, *, tol_atr: float, span_ms: int) -> dict:
    """粗→细一对一贪心匹配(先按极值时间近、再按价位近)。

    已用索引必须**按 kind 分开记**:high/low 两个列表的下标空间不同,共用一个
    set 会把「已用于 high」的下标误判成「已用于 low」,凭空制造未匹配(首版
    因此把覆盖 0.97 读成 0.69)。
    """
    by_kind: dict = {}
    for row in fine:
        by_kind.setdefault(row[0], []).append(row)
    used: dict = {kind: set() for kind in by_kind}
    matched = []
    unmatched = []
    for c in coarse:
        kind, cfts, _ckts, cprice, catr = c
        best, best_key = None, None
        for j, f in enumerate(by_kind.get(kind, ())):
            if j in used[kind]:
                continue
            _fk, ffts, _fkts, fprice, _fatr = f
            dt = abs(ffts - cfts)
            if dt > span_ms:
                continue
            dp = abs(fprice - cprice)
            if catr > 0 and dp > tol_atr * catr:
                continue
            key = (dt, dp)
            if best_key is None or key < best_key:
                best, best_key = j, key
        if best is None:
            unmatched.append(c)
        else:
            used[kind].add(best)
            matched.append((c, by_kind[kind][best]))
    fine_only = [f for kind, rows in by_kind.items()
                 for j, f in enumerate(rows) if j not in used[kind]]
    return {"matched": matched, "coarse_only": unmatched, "fine_only": fine_only}


def _quant(vals: list, q: float):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def _stats(pair: str, coarse: list, fine: list, *, tol_atr: float,
           span_ms: int, fine_ms: int) -> dict:
    m = _match(coarse, fine, tol_atr=tol_atr, span_ms=span_ms)
    leads = [(c[2] - f[2]) / fine_ms for c, f in m["matched"]]
    dp_atr = [abs(f[3] - c[3]) / c[4] for c, f in m["matched"] if c[4] > 0]
    lead_med = _quant(leads, 0.5)
    noise = round(len(m["fine_only"]) / len(fine), 4) if fine else None
    # 一对一的「细独有」结构下限:细 pivot 比粗多出来的那部分本来就无对应
    noise_floor = (round(1.0 - len(coarse) / len(fine), 4) if fine else None)
    out = {
        "pair": pair,
        "coarse_n": len(coarse), "fine_n": len(fine),
        "matched_n": len(m["matched"]),
        "coverage": round(len(m["matched"]) / len(coarse), 4) if coarse else None,
        "noise": noise,
        "noise_floor": noise_floor,
        "noise_excess": (round(noise / noise_floor, 4)
                         if noise and noise_floor else None),
        "coarse_only_n": len(m["coarse_only"]),
        "fine_only_n": len(m["fine_only"]),
        "lead_bars_med": lead_med,
        "lead_bars_p25": _quant(leads, 0.25),
        "lead_bars_p75": _quant(leads, 0.75),
        "lead_ge1_share": (round(sum(1 for x in leads if x >= 1.0) / len(leads), 4)
                           if leads else None),
        "lead_hours_med": (round(lead_med * fine_ms / 3_600_000, 3)
                           if lead_med is not None else None),
        "dp_atr_med": _quant(dp_atr, 0.5),
        "dp_atr_p90": _quant(dp_atr, 0.9),
        "by_kind": {},
    }
    for kind in (PivotKind.HIGH, PivotKind.LOW):
        cs = [c for c in coarse if c[0] is kind]
        fs = [f for f in fine if f[0] is kind]
        sub = _match(cs, fs, tol_atr=tol_atr, span_ms=span_ms)
        ls = [(c[2] - f[2]) / fine_ms for c, f in sub["matched"]]
        # 收紧方向:HIGH(空头防线在上)细价位更低 ⇒ 更紧;LOW 细价位更高 ⇒ 更紧。
        # `_trail` 只收紧(`cand < pos.invalid` / `> pos.invalid`),平局与更松
        # 都不会改变防线 ⇒ 这一列才是「提前量里有多少真能变成更紧的止损」。
        tight = sum(1 for c, f in sub["matched"]
                    if ((kind is PivotKind.HIGH and f[3] < c[3])
                        or (kind is PivotKind.LOW and f[3] > c[3])))
        out["by_kind"][kind.value] = {
            "coarse_n": len(cs), "fine_n": len(fs),
            "coverage": (round(len(sub["matched"]) / len(cs), 4) if cs else None),
            "noise": (round(len(sub["fine_only"]) / len(fs), 4) if fs else None),
            "lead_bars_med": _quant(ls, 0.5),
            "lead_ge1_share": (round(sum(1 for x in ls if x >= 1.0) / len(ls), 4)
                               if ls else None),
            "tighten_share": (round(tight / len(sub["matched"]), 4)
                              if sub["matched"] else None),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--pairs", default="1h:2h,2h:4h,4h:12h",
                    help="细:粗,逗号分隔")
    ap.add_argument("--tol-atr", type=float, default=0.35,
                    help="价位对应门(× ATR_coarse);0.35 与 k_bm 同量级")
    ap.add_argument("--out", default=str(OUTDIR))
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    pairs = [tuple(p.split(":")) for p in args.pairs.split(",") if p.strip()]
    tfs: list = []
    for fine_tf, coarse_tf in pairs:
        for tf in (fine_tf, coarse_tf):
            if tf not in tfs:
                tfs.append(tf)
    want = ([s.strip() for s in args.symbols.split(",") if s.strip()]
            or list(cfg["symbols"]))
    syms = [s for s in want if (ROOT / "data" / "raw" / f"{s}_1h.csv").exists()]
    if not syms:
        raise SystemExit("no symbol data available")

    base = Params.from_json(CONFIG)
    params = Params(**{**vars(base), "use_balance_detector": True,
                       "balance_quality_min": 0.35})
    t0 = time.time()
    cog_by, _df_cog, _df_exec, _df_trig = diag.load_all_data(
        syms, params, tf_list=tuple(tfs))
    print(f"[prep] {syms} tfs={tfs} ({time.time() - t0:.1f}s)", flush=True)

    payload: dict = {"tol_atr": args.tol_atr,
                     "criteria": {"coverage_min": 0.80, "lead_ge1_share_min": 0.50,
                                  "noise_excess_max": 1.10},
                     "symbols": {}, "agg": {}}
    hdr = (f"{'pair':10s} {'sym':10s} | {'cN':>4s} {'fN':>5s} | {'cover':>6s} "
           f"{'noise':>6s} {'xfloor':>6s} | {'lead':>6s} {'>=1bar':>7s} | "
           f"{'dp/atr':>7s} {'p90':>6s} | {'tight':>5s} | H/L cover")
    print(hdr, flush=True)
    for fine_tf, coarse_tf in pairs:
        pair = f"{fine_tf}:{coarse_tf}"
        fine_ms = agg.TF_HOURS[fine_tf] * 3_600_000
        span_ms = agg.TF_HOURS[coarse_tf] * 3_600_000
        payload["symbols"][pair] = {}
        acc = []
        for sym in syms:
            coarse = _pivot_rows(cog_by[(sym, coarse_tf)])
            fine = _pivot_rows(cog_by[(sym, fine_tf)])
            st = _stats(pair, coarse, fine, tol_atr=args.tol_atr,
                        span_ms=span_ms, fine_ms=fine_ms)
            st["symbol"] = sym
            payload["symbols"][pair][sym] = st
            acc.append((coarse, fine))
            bk = st["by_kind"]
            tight = max((v["tighten_share"] or 0) for v in bk.values())
            print(f"{pair:10s} {sym:10s} | {st['coarse_n']:4d} {st['fine_n']:5d} "
                  f"| {st['coverage']:6.3f} {st['noise']:6.3f} "
                  f"{st['noise_excess']:6.3f} | "
                  f"{(st['lead_bars_med'] or 0):6.2f} "
                  f"{(st['lead_ge1_share'] or 0):7.3f} | "
                  f"{(st['dp_atr_med'] or 0):7.3f} {(st['dp_atr_p90'] or 0):6.3f} | "
                  f"{tight:5.3f} | "
                  f"H={bk['high']['coverage']} L={bk['low']['coverage']}", flush=True)
        coarse_all = [r for c, _ in acc for r in c]
        fine_all = [r for _, f in acc for r in f]
        st = _stats(pair, coarse_all, fine_all, tol_atr=args.tol_atr,
                    span_ms=span_ms, fine_ms=fine_ms)
        ok = (st["coverage"] is not None and st["coverage"] >= 0.80
              and (st["lead_ge1_share"] or 0) >= 0.50
              and st["noise_excess"] is not None and st["noise_excess"] <= 1.10)
        st["passes"] = ok
        payload["agg"][pair] = st
        bk = st["by_kind"]
        tight = max((v["tighten_share"] or 0) for v in bk.values())
        print(f"{pair:10s} {'ALL':10s} | {st['coarse_n']:4d} {st['fine_n']:5d} "
              f"| {st['coverage']:6.3f} {st['noise']:6.3f} "
              f"{st['noise_excess']:6.3f} | "
              f"{(st['lead_bars_med'] or 0):6.2f} "
              f"{(st['lead_ge1_share'] or 0):7.3f} | "
              f"{(st['dp_atr_med'] or 0):7.3f} {(st['dp_atr_p90'] or 0):6.3f} | "
              f"{tight:5.3f} | "
              f"H={bk['high']['coverage']} L={bk['low']['coverage']} "
              f"=> {'PASS' if ok else 'FAIL'}", flush=True)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "pivot_align.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[out] {outdir / 'pivot_align.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

