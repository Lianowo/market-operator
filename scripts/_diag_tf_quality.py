"""逐周期胜率 / 盈亏比归因(只读诊断: 不改 src/、不改 config/)。

问题: 12h / 1d 的胜率与盈亏比显著弱于 2h / 4h(见 FACTORS.md), 而成本不是主因。
本脚本只回答一件事: **差在哪一段**(入场方向 / 止损宽度 / 目标距离 / 持仓回吐)。

四块:
  A 结果分解 —— 把 E[R] 拆成 P(达 +1R)、转化率 P(赢 | 达 +1R)、回吐(mfe − net)、
    亏损解剖(满止损占比), 分 dev / test。
  B 止损宽度 × 目标距离 反事实 —— 在每笔自己的 1h 路径上, 以固定 10×TF 时间窗重放
    "固定止损 k·rp + 固定止盈 T·R"; 同时给出**无止损、持有到期末**的 R, 这是入场
    方向边缘的直接度量(与止损/目标设置无关)。
  C 入场质量代理分桶 —— 目标距离代理、箱宽、趋势度、反向程度, 边界取 dev 分位套 test。
  D 时间集中度 —— 按季度看 12h / 1d 是否只是某一窗口拖累。

口径:
- 毛 R = gross_pnl / risk_usdt; 净 R = r_multiple。风险价 rp = risk_usdt·entry/notional
  (= features.csv 的 risk_px 列, 即开仓时的结构失效距离)。
- 反事实按 1h 粒度成交; 同一根 bar 同时触到止损与止盈时 **假设先触止损**(对止盈
  规则偏保守, 与 _diag_v21_factor_mine.py 一致)。
- 反事实不含成本(纯几何), 成本见 reports/live_cost_audit_20261023/AUDIT.md。

用法:
    python scripts/_diag_tf_quality.py
    python scripts/_diag_tf_quality.py --features reports/factor_mine_20261023/features.csv \
        --out reports/tf_quality_20261023
    python scripts/_diag_tf_quality.py --trades reports/tf_trigger_exp/<arm> \
        --out reports/tf_quality_<arm>          # 其它臂(复用 factor_mine 的因果口径)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
TFS = ("2h", "4h", "12h", "1d")
TF_HOURS = {"2h": 2, "4h": 4, "12h": 12, "1d": 24}
MS_H = 3_600_000
KS = (1.0, 1.25, 1.5, 2.0, 3.0)  # 固定止损宽度(× rp)
TS = (1.0, 2.0, 3.0, None)  # 固定止盈(R); None = 不设止盈
CF_HORIZONS = (10, 20)      # 反事实窗口(× TF 根 1h bar)


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------

def prep(d: pd.DataFrame) -> pd.DataFrame:
    """统一派生列:rp(风险距离)/ 目标距离代理 / 季度。"""
    d = d.dropna(subset=["entry", "risk_px", "net_r", "gross_r", "open_ts"]).copy()
    # features.csv 的 risk_px 是**价格距离**(risk_usdt·entry/notional), 不是价位。
    d["rp"] = d["risk_px"].astype(float)
    d = d[d["rp"] > 0].copy()
    d["hold_h"] = d["hold_h"].fillna(0.0)
    # 目标距离代理: 入场点到箱体对侧缘的距离(R)。非 E3(无对侧目标)不适用。
    edge = np.where(d["direction"] > 0, d["pos55"], 1.0 - d["pos55"])
    d["edge_pos"] = edge
    d["tgt_r"] = (1.0 - edge) * d["width55_atr"] / d["risk_atr"]
    d["quarter"] = (pd.to_datetime(d["open_decision_ts"], unit="ms")
                    .dt.to_period("Q").astype(str))
    return d


def load_features(path: Path) -> pd.DataFrame:
    return prep(pd.read_csv(path))


def features_from_trades(trades_dir: Path, tag: str = "1",
                         arm: str = "a0") -> pd.DataFrame:
    """从成交账本目录重建等价 features(供其它臂复用,如 P1 的 A9 判定)。

    复用 `_diag_v21_factor_mine.py` 的 load_trades / frame_features / ctx_of /
    enrich(同一套因果口径),不重复实现。`tag` 只取一个成本档(默认 1.0×),
    `arm` 只取一个臂前缀(默认 `a0`,同目录还有 A/B/C/D… 的账本)。
    """
    import _diag_v21_factor_mine as fm

    files = []
    for p in sorted(Path(trades_dir).glob(f"trades_{arm}__*__{tag}.csv")):
        seg = ("dev" if "__dev__" in p.name
               else "test" if "__test__" in p.name else None)
        if seg is not None:
            files.append((seg, p))
    if not files:
        raise SystemExit(f"no trades_{arm}__{{dev,test}}__{tag}.csv under {trades_dir}")
    rows = fm.load_trades(files)
    frames, h1 = {}, {}
    for sym in sorted({r["symbol"] for r in rows}):
        for tf in TFS:
            frames[(sym, tf)] = fm.frame_features(fm.agg.load_tf(sym, tf))
        h1[sym] = fm.load_1h(sym)
    rows = fm.enrich(rows, frames, h1)
    d = pd.DataFrame(rows)
    d["risk_atr"] = d["risk_atr"].astype(float)
    return prep(d)


def load_1h(symbols) -> dict:
    out = {}
    for s in sorted(set(symbols)):
        p = ROOT / "data" / "raw" / f"{s}_1h.csv"
        d = pd.read_csv(p).sort_values("ts").reset_index(drop=True)
        out[s] = (d["ts"].to_numpy(np.int64), d["high"].to_numpy(float),
                  d["low"].to_numpy(float), d["close"].to_numpy(float))
    return out


def cf_path(hi, lo, cl, i0: int, entry: float, rp: float, d: int,
            k, t, horizon: int) -> float:
    """固定止损 k·rp + 固定止盈 t·R;同 bar 双触按先触止损(保守)。

    k/t 为 None 表示该侧不设。窗口 = horizon 根 1h bar, 到期按收盘 mark。
    """
    n = min(len(cl) - i0, horizon)
    if n <= 0:
        return float("nan")
    fav = d * (hi[i0:i0 + n] - entry) / rp
    adv = d * (lo[i0:i0 + n] - entry) / rp
    si = (np.flatnonzero(adv <= -k) if k is not None
          else np.empty(0, np.int64))
    ti = (np.flatnonzero(fav >= t) if t is not None
          else np.empty(0, np.int64))
    if len(si) and (not len(ti) or si[0] <= ti[0]):
        return float(-k)
    if len(ti):
        return float(t)
    return float(d * (cl[i0 + n - 1] - entry) / rp)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def summary(r) -> dict:
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    if not len(r):
        return {"n": 0}
    w = r > 0
    gw = float(r[w].sum()) if w.any() else 0.0
    gl = float(-r[~w].sum()) if (~w).any() else 0.0
    return {"n": int(len(r)), "wr": 100.0 * float(w.mean()),
            "avg_win": float(r[w].mean()) if w.any() else None,
            "avg_loss": float(r[~w].mean()) if (~w).any() else None,
            "payoff": gw / gl if gl > 0 else None,
            "e": float(r.mean()),
            "se": float(r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 2 else None}


def boot_ci(r, reps: int = 2000, seed: int = 7):
    """E[R] 的 95% bootstrap 置信区间(按笔重采样)。"""
    r = np.asarray(r, float)
    r = r[np.isfinite(r)]
    if len(r) < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    m = rng.choice(r, size=(reps, len(r)), replace=True).mean(axis=1)
    return (float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5)))


def fmt(v, nd=2):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v:.{nd}f}"


def table(rows: list, cols: list, head: list) -> str:
    def cell(v):
        if v is None:
            return "—"
        if isinstance(v, float):
            return fmt(v)
        return str(v)
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join(["---"] * len(head)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(r.get(c)) for c in cols) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# A 结果分解
# ---------------------------------------------------------------------------

def block_a(d: pd.DataFrame) -> list:
    rows = []
    for tf in TFS:
        for seg in ("dev", "test"):
            a = d[(d["tf"] == tf) & (d["seg"] == seg)]
            if a.empty:
                continue
            g = a["gross_r"].to_numpy(float)
            w = g > 0
            los = a[a["net_r"] <= 0]
            win = a[a["net_r"] > 0]
            mfe1 = a[a["mfe_r"] >= 1.0]
            rows.append({
                "tf": tf, "seg": seg, "n": len(a),
                "wr": 100.0 * float(w.mean()),
                "payoff": float(g[w].sum() / -g[~w].sum()),
                "e_gross": float(g.mean()), "e_net": float(a["net_r"].mean()),
                "p1r": 100.0 * float((a["mfe_r"] >= 1.0).mean()),
                "conv": (100.0 * float((mfe1["net_r"] > 0).mean())
                         if len(mfe1) else None),
                "e_hit1": (float(mfe1["net_r"].mean()) if len(mfe1) else None),
                "cap": float(a["net_r"].sum() / a["mfe_r"].sum()),
                "giveback": float((a["mfe_r"] - a["net_r"]).median()),
                "gb_atr": float(((a["mfe_r"] - a["net_r"]) * a["risk_atr"]).median()),
                "loss_full": 100.0 * float((los["net_r"] <= -0.95).mean()),
                "loss_med": (float(los["net_r"].median()) if len(los) else None),
                "win_med": (float(win["net_r"].median()) if len(win) else None),
                "mfe_med": float(a["mfe_r"].median()),
                "hold_med": float(a["hold_h"].median()),
                "tpeak": float((a["t_mfe_h"] / a["hold_h"].replace(0, np.nan)
                                ).median()),
                "risk_atr": float(a["risk_atr"].median()),
            })
    return rows


# ---------------------------------------------------------------------------
# B 止损宽度 × 目标距离 反事实
# ---------------------------------------------------------------------------

def block_b(d: pd.DataFrame, paths: dict) -> list:
    rows = []
    for tf in TFS:
        a = d[d["tf"] == tf]
        for hbars in CF_HORIZONS:
            horizon = hbars * TF_HOURS[tf]
            curves = {}
            for k in KS:
                for t in TS:
                    vals = []
                    for r in a.itertuples():
                        ts, hi, lo, cl = paths[r.symbol]
                        i0 = int(np.searchsorted(ts, int(r.open_ts), side="left"))
                        vals.append(cf_path(hi, lo, cl, i0, r.entry, r.rp,
                                            int(r.direction), k, t, horizon))
                    curves[(k, t)] = np.asarray(vals, float)
            # 无止损、持有到期末 = 入场方向边缘
            hold = np.asarray([
                cf_path(paths[r.symbol][1], paths[r.symbol][2],
                        paths[r.symbol][3],
                        int(np.searchsorted(paths[r.symbol][0],
                                            int(r.open_ts), side="left")),
                        r.entry, r.rp, int(r.direction), None, None, horizon)
                for r in a.itertuples()], float)
            base = float(np.nanmean(hold))
            for k in KS:
                for t in TS:
                    s = summary(curves[(k, t)])
                    if not s["n"]:
                        continue
                    rows.append({"tf": tf, "h": hbars, "k": float(k),
                                 "t": None if t is None else float(t),
                                 "n": s["n"], "wr": s["wr"],
                                 "payoff": s["payoff"], "e": s["e"],
                                 "e_vs_hold": s["e"] - base})
            s = summary(hold)
            rows.append({"tf": tf, "h": hbars, "k": None, "t": None,
                         "n": s["n"], "wr": s["wr"], "payoff": s["payoff"],
                         "e": s["e"], "e_vs_hold": 0.0})
    return rows


# ---------------------------------------------------------------------------
# C 入场质量代理分桶(边界 dev 分位 → test 原样套用)
# ---------------------------------------------------------------------------

FACTORS = (
    ("tgt_r", "目标距离(R, 代理)"),
    ("width55_atr", "箱宽(ATR)"),
    ("atr_ratio", "ATR/200均值"),
    ("er50", "趋势效率 er50"),
    ("ema200_atr", "距 EMA200(ATR, 带符号)"),
    ("edge_pos", "箱内位置(0=入场缘)"),
)


def block_c(d: pd.DataFrame, tf: str, nq: int = 3) -> list:
    a = d[d["tf"] == tf]
    dev = a[a["seg"] == "dev"]
    rows = []
    for col, label in FACTORS:
        if col not in a or a[col].notna().sum() < 20:
            continue
        edges = np.unique(np.nanquantile(dev[col].dropna(), np.linspace(0, 1, nq + 1)))
        if len(edges) < 3:
            continue
        for j in range(len(edges) - 1):
            lo, hi = edges[j], edges[j + 1]
            m = (a[col] >= lo) & (a[col] <= hi) if j == len(edges) - 2 \
                else (a[col] >= lo) & (a[col] < hi)
            for seg in ("dev", "test"):
                b = a[m & (a["seg"] == seg)]
                if len(b) < 5:
                    continue
                g = b["gross_r"].to_numpy(float)
                w = g > 0
                rows.append({
                    "factor": f"{label} q{j + 1}", "seg": seg, "n": len(b),
                    "lo": float(lo), "hi": float(hi),
                    "wr": 100.0 * float(w.mean()),
                    "payoff": (float(g[w].sum() / -g[~w].sum())
                               if (~w).any() else None),
                    "e_gross": float(g.mean()), "e_net": float(b["net_r"].mean()),
                })
    return rows


# ---------------------------------------------------------------------------
# D 时间集中度
# ---------------------------------------------------------------------------

def block_d(d: pd.DataFrame) -> list:
    rows = []
    for tf in TFS:
        a = d[d["tf"] == tf]
        for q, b in a.groupby("quarter"):
            g = b["gross_r"].to_numpy(float)
            rows.append({"tf": tf, "quarter": str(q), "n": len(b),
                         "wr": 100.0 * float((g > 0).mean()),
                         "e_gross": float(g.mean()),
                         "e_net": float(b["net_r"].mean())})
    return rows


# ---------------------------------------------------------------------------
# E 原始边缘: 无止损持有到期末, 按 TF × 事件 × 窗口
# ---------------------------------------------------------------------------

HORIZONS = (5, 10, 20)


def block_e(d: pd.DataFrame, paths: dict) -> list:
    rows = []
    for tf in TFS:
        a = d[d["tf"] == tf]
        for ev in sorted(a["event_id"].dropna().unique()):
            b = a[a["event_id"] == ev]
            for hbars in HORIZONS:
                vals = []
                for r in b.itertuples():
                    ts, hi, lo, cl = paths[r.symbol]
                    i0 = int(np.searchsorted(ts, int(r.open_ts), side="left"))
                    vals.append(cf_path(hi, lo, cl, i0, r.entry, r.rp,
                                        int(r.direction), None, None,
                                        hbars * TF_HOURS[tf]))
                s = summary(vals)
                if not s["n"]:
                    continue
                lo_ci, hi_ci = boot_ci(vals)
                rows.append({"tf": tf, "event_id": ev, "bars": hbars,
                             "n": s["n"], "wr": s["wr"], "payoff": s["payoff"],
                             "e": s["e"], "ci_lo": lo_ci, "ci_hi": hi_ci})
    return rows


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render(meta: dict, a: list, b: list, c: dict, dq: list, e: list) -> str:
    L = []
    L.append("# 逐周期胜率 / 盈亏比归因 — 12h · 1d\n")
    L.append(f"只读诊断;输入账本 `{meta['features']}` (N={meta['n']})。"
             "毛 R = gross_pnl/risk_usdt;净 R = r_multiple。"
             "反事实为纯几何(不含成本),同 bar 双触按先触止损(保守)。\n")

    L.append("## 1. 结果分解:胜率与盈亏比到底差在哪一段\n")
    L.append(table(a, ["tf", "seg", "n", "wr", "payoff", "e_gross", "e_net",
                       "p1r", "conv", "e_hit1", "cap", "giveback", "gb_atr",
                       "loss_full", "loss_med", "win_med", "mfe_med",
                       "hold_med", "tpeak", "risk_atr"],
                   ["TF", "段", "N", "胜率%", "盈亏比", "毛E[R]", "净E[R]",
                    "P(达+1R)%", "转化%", "E[R]\\|达1R", "捕获率", "回吐中位R",
                    "回吐中位ATR", "满损占比%", "亏中位R", "盈中位R",
                    "MFE中位R(全部)", "持仓中位h", "MFE时点/持仓", "风险(ATR)"]))
    L.append("\n口径:P(达+1R) 用持仓窗口内 1h 极值;转化 = P(净R>0 | MFE≥1R);"
             "捕获率 = Σ净R/ΣMFE;回吐 = median(MFE − 净R),**ATR 口径 = median((MFE−净R)·rp/ATR)**;"
             "满损占比 = 亏损笔中 净R ≤ −0.95 的比例;风险(ATR) = rp/ATR(开仓时结构失效距离)。\n")

    L.append("## 2. 止损宽度 × 目标距离 反事实(纯几何,窗口 = 10× / 20× TF 根 1h)\n")
    by_tf = {}
    for r in b:
        by_tf.setdefault((r["tf"], r["h"]), []).append(r)
    for tf in TFS:
        L.append(f"### {tf}\n")
        for hbars in CF_HORIZONS:
            L.append(f"窗口 {hbars}×TF:\n")
            L.append(table(by_tf.get((tf, hbars), []),
                           ["k", "t", "n", "wr", "payoff", "e", "e_vs_hold"],
                           ["止损(k·rp)", "止盈(R)", "N", "胜率%", "盈亏比",
                            "E[R]", "E−无止损"]))
            L.append("")
    L.append("注:`—` 行 = 无止损、无止盈、到期收盘 mark, 即**入场方向在窗口内的"
             "原始边缘**;其余行与之比较可分离「入场方向」与「止损/目标几何」。\n")

    for tf in ("12h", "1d", "4h"):
        L.append(f"## 3.{tf} 入场质量代理分桶\n")
        L.append(table(c.get(tf, []), ["factor", "seg", "n", "wr", "payoff",
                                       "e_gross", "e_net"],
                       ["因子桶", "段", "N", "胜率%", "盈亏比", "毛E[R]", "净E[R]"]))
        L.append("")
    L.append("注:边界一律取 dev 三分位,test 原样套用(不回流)。仅作归因,不作选参。\n")

    L.append("## 4. 时间集中度(季度)\n")
    L.append(table(dq, ["tf", "quarter", "n", "wr", "e_gross", "e_net"],
                   ["TF", "季度", "N", "胜率%", "毛E[R]", "净E[R]"]))
    L.append("")

    L.append("## 5. 原始边缘:无止损持有到期末(按事件 × 窗口)\n")
    L.append(table(e, ["tf", "event_id", "bars", "n", "wr", "payoff", "e",
                       "ci_lo", "ci_hi"],
                   ["TF", "事件", "窗口(TF根)", "N", "胜率%", "盈亏比", "E[R]",
                    "95%CI 低", "95%CI 高"]))
    L.append("\n注:这是**入场方向本身**在固定窗口的漂移(按 rp 归一), 与止损/止盈/"
             "trail 无关;它给出「这笔信号值不值得做」的上限。\n")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=str(
        ROOT / "reports" / "factor_mine_20261023" / "features.csv"))
    ap.add_argument("--trades", default=None,
                    help="改为从成交账本目录重建 features(用于其它臂,如 P1 的 A9)")
    ap.add_argument("--tag", default="1", help="--trades 模式下的成本档(默认 1.0x)")
    ap.add_argument("--arm", default="a0", help="--trades 模式下的臂前缀(默认 a0)")
    ap.add_argument("--out", default=str(ROOT / "reports" / "tf_quality_20261023"))
    args = ap.parse_args()

    if args.trades:
        d = features_from_trades(Path(args.trades), args.tag, args.arm)
        src = f"trades:{args.trades}@{args.arm}/{args.tag}x"
    else:
        d = load_features(Path(args.features))
        src = Path(args.features).name
    paths = load_1h(d["symbol"].unique())
    # 口径自检: features 的 risk_atr 应 ≈ (rp/entry)/atr_pct
    chk = ((d["rp"] / d["entry"]) / d["atr_pct"])
    err = (chk - d["risk_atr"]).abs().median()
    print(f"[check] risk_atr 口径误差中位 = {err:.4f}(应 ≈ 0)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    a = block_a(d)
    b = block_b(d, paths)
    c = {tf: block_c(d, tf) for tf in TFS}
    dq = block_d(d)
    e = block_e(d, paths)
    md = render({"features": src, "n": len(d)}, a, b, c, dq, e)
    (out / "QUALITY.md").write_text(md, encoding="utf-8")
    (out / "quality.json").write_text(
        json.dumps({"a": a, "b": b, "c": c, "d": dq, "e": e},
                   ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"[out] {out / 'QUALITY.md'}")
    for r in a:
        if r["seg"] == "dev":
            print(f"{r['tf']:>3} dev  N={r['n']:4d} WR={r['wr']:5.1f} "
                  f"payoff={fmt(r['payoff'])} E_g={r['e_gross']:+.3f} "
                  f"P1R={r['p1r']:5.1f} conv={fmt(r['conv'], 1)} "
                  f"giveback={fmt(r['giveback'])} full_loss={r['loss_full']:5.1f} "
                  f"risk_atr={fmt(r['risk_atr'])}")


if __name__ == "__main__":
    main()
