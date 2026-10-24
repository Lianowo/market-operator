"""逐周期出入场策略挖掘(只读诊断: 不改 src/、不改 config/)。

两件事, 都建立在已冻结的 a0 成交账本上:

  A 入场因子分桶 —— 严格因果(TF 帧 <= 决策 bar 收盘)的上下文特征, 输出
    每桶的 N / 胜率 / 盈亏比 / 毛 E[R] / 净 E[R], 分 dev 与 test 两列。
  B 出场规则反事实 —— 在每笔自己的 1h 路径上重放 +0.5R/+0.75R/+1R 止盈、
    半仓止盈+保本、+1R 后 0.5R 棘轮、时间帽等规则, 输出每周期的 ΔE[R]。

口径声明:
- 毛 R = gross_pnl / risk_usdt;净 R = r_multiple(CSV 列, = net_pnl/risk_usdt)。
- MFE/MAE 用 1h bar 极值重建, 风险价 = risk_usdt * entry / notional。
- 反事实出场按 1h 粒度成交;同一根 bar 内同时触到止盈与止损时**假设先触
  止损**(对止盈规则偏保守)。成本沿用该笔实际 cost_R(缩短持仓本会少付资金
  费, 故对止盈规则同样偏保守)。
- 分桶边界一律用 dev 分位数确定, 再原样套到 test(不回流)。

用法:
    python scripts/_diag_v21_factor_mine.py --source abcd --out reports/factor_mine_20261023
    python scripts/_diag_v21_factor_mine.py --source solo --workspace explore --out reports/factor_mine_20261023_solo
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import aggregate as agg          # noqa: E402

HTF = ("2h", "4h", "12h", "1d")
NEXT_TF = {"2h": "4h", "4h": "12h", "12h": "1d", "1d": None}
MS_H = 3_600_000.0
ABCD_DIR = ROOT / "reports" / "tf_trigger_exp" / "abcd_20261022"
SOLO_DIR = ROOT / "reports" / "tf_solo_20261023"


# ---------------------------------------------------------------------------
# 帧特征(因果: 第 i 根 bar 的特征只用 <= i 的数据)
# ---------------------------------------------------------------------------

def frame_features(df: pd.DataFrame) -> pd.DataFrame:
    """TF 帧上的因果上下文特征。df 列: ts/open/high/low/close/volume。"""
    d = df.reset_index(drop=True).copy()
    c, h, l = d["close"], d["high"], d["low"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()
    d["atr_pct"] = d["atr"] / c
    d["atr_ratio"] = d["atr"] / d["atr"].rolling(200).mean()
    for n in (20, 55):
        hi = h.rolling(n).max().shift(1)
        lo = l.rolling(n).min().shift(1)
        d[f"pos{n}"] = (c - lo) / (hi - lo)
        d[f"width{n}_atr"] = (hi - lo) / d["atr"]
        d[f"ret{n}_atr"] = (c - c.shift(n)) / d["atr"]
        d[f"touch{n}"] = (h >= hi * 0.999).rolling(n).sum()
        d[f"bar_hi{n}"] = h.rolling(n).apply(
            lambda x: float(len(x) - 1 - int(np.argmax(x))), raw=True)
    d["bar_lo55"] = l.rolling(55).apply(
        lambda x: float(len(x) - 1 - int(np.argmin(x))), raw=True)
    for n in (20, 50):
        num = (c - c.shift(n)).abs()
        den = c.diff().abs().rolling(n).sum()
        d[f"er{n}"] = num / den
    d["ema200_atr"] = (c - d["ema200"]) / d["atr"]
    d["ema50_atr"] = (c - d["ema50"]) / d["atr"]
    step = np.sign(c.diff().fillna(0.0).to_numpy())
    streak = np.zeros(len(step), dtype=int)
    for i in range(1, len(step)):
        if step[i] > 0:
            streak[i] = streak[i - 1] + 1 if streak[i - 1] > 0 else 1
        elif step[i] < 0:
            streak[i] = streak[i - 1] - 1 if streak[i - 1] < 0 else -1
    d["up_streak"] = streak
    return d


def pick_row(d: pd.DataFrame, limit_ts: int) -> int:
    """最后一根**已收盘** bar 的索引: bar.ts + tf_ms <= 决策时刻。

    调用方传入 limit_ts = decision_ts - tf_ms(本数据 ts 为桶起点, 决策发生在
    桶收盘时;decision_ts 恰为收盘/下一桶起点)。未找到返回 -1。
    """
    arr = d["ts"].to_numpy(np.int64)
    return int(np.searchsorted(arr, int(limit_ts), side="right")) - 1


FEATS = ("atr_pct", "atr_ratio", "er20", "er50", "pos20", "pos55",
         "width20_atr", "width55_atr", "touch20", "ret20_atr", "ret55_atr",
         "ema200_atr", "ema50_atr", "up_streak", "bar_hi20", "bar_lo55")


def ctx_of(frames: dict, sym: str, tf: str, ts: int) -> dict:
    d = frames[(sym, tf)]
    i = pick_row(d, ts - agg.TF_HOURS[tf] * 3_600_000)
    out: dict = {}
    if i < 0:
        return out
    r = d.iloc[i]
    for k in FEATS:
        v = r.get(k)
        out[k] = None if v is None or not np.isfinite(float(v)) else float(v)
    nxt = NEXT_TF.get(tf)
    if nxt and (sym, nxt) in frames:
        dx = frames[(sym, nxt)]
        j = pick_row(dx, ts - agg.TF_HOURS[nxt] * 3_600_000)
        if j >= 0:
            rx = dx.iloc[j]
            for k, col in (("x_pos20", "pos20"), ("x_er50", "er50"),
                           ("x_ema200_atr", "ema200_atr")):
                v = rx.get(col)
                out[k] = None if v is None or not np.isfinite(float(v)) else float(v)
    return out

# ---------------------------------------------------------------------------
# 持仓路径 + 出场规则反事实(1h 粒度)
# ---------------------------------------------------------------------------

def load_1h(sym: str) -> pd.DataFrame:
    d = pd.read_csv(ROOT / "data" / "raw" / f"{sym}_1h.csv")
    return d.sort_values("ts").reset_index(drop=True)


def path_slice(df1h: pd.DataFrame, open_ts: int, exit_ts: int):
    m = (df1h["ts"] >= int(open_ts)) & (df1h["ts"] <= int(exit_ts))
    sub = df1h.loc[m]
    if sub.empty:
        return None
    return (sub["high"].to_numpy(float), sub["low"].to_numpy(float),
            sub["close"].to_numpy(float), sub["ts"].to_numpy(np.int64))


def path_features(hi, lo, ts, entry: float, rp: float, d: int) -> dict:
    fav = d * (hi - entry) / rp
    adv = d * (lo - entry) / rp
    j = int(np.argmax(fav))
    t_mfe_h = (int(ts[j]) - int(ts[0])) / MS_H
    first = "none"
    for k in range(len(fav)):
        if adv[k] <= -1.0:
            first = "mae1"
            break
        if fav[k] >= 1.0:
            first = "mfe1"
            break
    return {"mfe_r": float(fav.max()), "mae_r": float(adv.min()),
            "t_mfe_h": t_mfe_h, "mfe_first": first}


TP_LEVELS = (0.5, 0.75, 1.0, 1.5, 2.0)
HALF_LEVELS = (0.75, 1.0, 1.5)
TIME_BARS = (3, 6, 12, 24)
RULE_NAMES = tuple(
    [f"tp{x:g}_full" for x in TP_LEVELS]
    + [f"tp{x:g}_half_be" for x in HALF_LEVELS]
    + ["be1_full", "trail05_after1"]
    + [f"tc{k}" for k in TIME_BARS]
    + [f"tl{k}" for k in TIME_BARS if k != 24])


def simulate_rules(hi, lo, cl, entry: float, rp: float, d: int,
                   actual_gross_r: float, tf_hours: int) -> dict:
    """在同一 1h 路径上重放出场规则;返回 {规则: 毛 R}(None = 未触发)。"""
    fav = d * (hi - entry) / rp
    adv = d * (lo - entry) / rp
    n = len(fav)
    out: dict = {}
    for lvl in TP_LEVELS:
        hit = np.flatnonzero(fav >= lvl)
        out[f"tp{lvl:g}_full"] = float(lvl) if len(hit) else None
    for lvl in HALF_LEVELS:
        hit = np.flatnonzero(fav >= lvl)
        if not len(hit):
            out[f"tp{lvl:g}_half_be"] = None
            continue
        j = int(hit[0])
        if adv[j] <= 0.0:                     # 同 bar 回到入场价: 保守记 0
            rest = 0.0
        else:
            rest = 0.0 if np.any(adv[j + 1:] <= 0.0) else actual_gross_r
        out[f"tp{lvl:g}_half_be"] = 0.5 * lvl + 0.5 * rest
    hit = np.flatnonzero(fav >= 1.0)
    if not len(hit):
        out["be1_full"] = None
    else:
        j = int(hit[0])
        out["be1_full"] = (0.0 if (adv[j] <= 0.0 or np.any(adv[j + 1:] <= 0.0))
                           else actual_gross_r)
    mfe_run = np.maximum.accumulate(fav)
    stop = -np.inf
    res = actual_gross_r
    armed = bool(np.any(mfe_run >= 1.0))
    for i in range(1, n):
        if mfe_run[i - 1] >= 1.0:
            stop = max(stop, float(mfe_run[i - 1]) - 0.5)
        if stop > -np.inf and adv[i] <= stop:
            res = float(stop)
            break
    out["trail05_after1"] = res if armed else None
    for k in TIME_BARS:
        idx = k * tf_hours
        if idx >= n - 1:
            out[f"tc{k}"] = None
            if k != 24:
                out[f"tl{k}"] = None
            continue
        out[f"tc{k}"] = float(d * (cl[idx] - entry) / rp)
        if k != 24:
            out[f"tl{k}"] = (float(d * (cl[idx] - entry) / rp)
                             if mfe_run[idx] < 0.5 else actual_gross_r)
    return out

# ---------------------------------------------------------------------------
# 账本装配
# ---------------------------------------------------------------------------

def trade_files(source: str, workspace: str) -> list:
    out = []
    if source == "abcd":
        for seg in ("dev", "test"):
            p = ABCD_DIR / f"trades_a0__{seg}__1.csv"
            if p.exists():
                out.append((seg, p))
    else:
        for tf in HTF:
            for seg in ("dev", "test"):
                p = SOLO_DIR / workspace / f"trades_tfsolo_{tf}__{seg}__1.csv"
                if p.exists():
                    out.append((seg, p))
    return out


def load_trades(files) -> list:
    rows = []
    for seg, path in files:
        with path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("closed") not in ("True", "true"):
                    continue
                if r.get("in_segment_open") not in ("True", "true"):
                    continue
                if not r.get("exit_ts"):
                    continue
                risk = float(r["risk_usdt"] or 0.0)
                notional = float(r["notional"] or 0.0)
                entry = float(r["entry"])
                if risk <= 0 or notional <= 0:
                    continue
                rows.append({
                    "trade_id": r["trade_id"], "symbol": r["symbol"],
                    "tf": r["tf"], "seg": seg, "event_id": r["event_id"],
                    "regime": r["regime"], "direction": int(r["direction"]),
                    "entry_mode": r["entry_mode"], "risk_usdt": risk,
                    "open_decision_ts": int(float(r["open_decision_ts"])),
                    "open_ts": int(float(r["open_ts"])),
                    "exit_ts": int(float(r["exit_ts"])), "entry": entry,
                    "risk_px": risk * entry / notional,
                    "gross_r": float(r["gross_pnl"]) / risk,
                    "net_r": float(r["r_multiple"]),
                    "net_usd": float(r["net_pnl"]),
                    "cost_r": (float(r["gross_pnl"]) - float(r["net_pnl"])) / risk,
                    "hold_h": float(r["hold_hours"] or 0.0),
                    "reason": (r["reason_exit"] or "").split("|")[0],
                })
    return rows


def enrich(rows: list, frames: dict, h1: dict) -> list:
    keep = []
    for r in rows:
        feats = ctx_of(frames, r["symbol"], r["tf"], r["open_decision_ts"])
        if not feats:
            continue
        r.update(feats)
        r["hour"] = (r["open_decision_ts"] // 3_600_000) % 24
        ap = r.get("atr_pct")
        r["risk_atr"] = ((r["risk_px"] / r["entry"]) / ap
                         if ap and ap > 0 else None)
        sl = path_slice(h1[r["symbol"]], r["open_ts"], r["exit_ts"])
        if sl is not None:
            hi, lo, cl, ts = sl
            r.update(path_features(hi, lo, ts, r["entry"], r["risk_px"],
                                   r["direction"]))
            r["sims"] = simulate_rules(hi, lo, cl, r["entry"], r["risk_px"],
                                       r["direction"], r["gross_r"],
                                       agg.TF_HOURS[r["tf"]])
        keep.append(r)
    return keep


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _boot_ci(x: np.ndarray, reps: int = 2000, seed: int = 7):
    if len(x) < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    m = rng.choice(x, size=(reps, len(x)), replace=True).mean(axis=1)
    return (float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5)))


def stats(rows: list, with_ci: bool = False) -> dict:
    if not rows:
        return {"n": 0}
    g = np.array([r["gross_r"] for r in rows], float)
    nu = np.array([r["net_r"] for r in rows], float)
    win = g > 0
    gw = float(g[win].sum()) if win.any() else 0.0
    gl = float(-g[~win].sum()) if (~win).any() else 0.0
    out = {"n": len(rows),
           "wr": round(float(win.mean()), 4),
           "avg_win": round(float(g[win].mean()), 3) if win.any() else None,
           "avg_loss": round(float(g[~win].mean()), 3) if (~win).any() else None,
           "payoff": round(gw / gl, 3) if gl > 0 else None,
           "e_gross": round(float(g.mean()), 4),
           "e_net": round(float(nu.mean()), 4),
           "net_usd": round(sum(r["net_usd"] for r in rows), 3)}
    if with_ci:
        lo, hi = _boot_ci(nu)
        out["e_net_ci"] = [round(lo, 4), round(hi, 4)]
    return out

# ---------------------------------------------------------------------------
# 分桶与报告
# ---------------------------------------------------------------------------

CONT_FEATS = ("er20", "er50", "pos20", "pos55", "width20_atr", "atr_ratio",
              "atr_pct", "ret20_atr", "ema200_atr", "touch20", "up_streak",
              "bar_hi20", "x_pos20", "x_er50", "x_ema200_atr", "risk_atr",
              "hold_h", "mfe_r", "t_mfe_h")
DISC_FEATS = ("event_id", "regime", "entry_mode", "direction", "symbol",
              "mfe_first", "hour")
POST_HOC = ("hold_h", "mfe_r", "t_mfe_h", "mfe_first")
ENTRY_FEATS = tuple(f for f in CONT_FEATS if f not in POST_HOC)


def tail_stats(rows: list) -> dict:
    if not rows:
        return {"n": 0}
    nu = np.sort(np.array([r["net_r"] for r in rows], float))[::-1]
    tot = float(nu.sum())
    k = max(1, int(round(len(nu) * 0.05)))
    top = float(nu[:k].sum())
    return {"n": len(nu), "top5_n": k, "top5_R": round(top, 2),
            "top5_share": round(top / tot, 3) if tot else None,
            "n_ge3R": int((nu >= 3.0).sum()),
            "sum_ge3R": round(float(nu[nu >= 3.0].sum()), 2),
            "n_le_m1R": int((nu <= -1.0).sum()),
            "median_R": round(float(np.median(nu)), 3)}


def perm_threshold(dev: list, feats, reps: int = 300, seed: int = 11):
    """置换检验: 同一套分桶下 max|Δ净R| 的零分布 95 分位(FWER≈5%)。"""
    nu = np.array([r["net_r"] for r in dev], float)
    if len(nu) < 60:
        return {"thr": None, "hits": [], "tested": 0}
    base = float(nu.mean())
    cells = []
    for f in feats:
        if f in DISC_FEATS:
            keys = sorted({str(r.get(f)) for r in dev if r.get(f) is not None})
            groups = {k: np.array([i for i, r in enumerate(dev)
                                   if str(r.get(f)) == k]) for k in keys}
        else:
            vals = np.array([r[f] if r.get(f) is not None else np.nan
                             for r in dev], float)
            ok = ~np.isnan(vals)
            if ok.sum() < 60:
                continue
            e = _edges(vals[ok])
            groups = {f"q{i+1}": np.flatnonzero(ok & (np.searchsorted(e, vals, "left") == i))
                      for i in range(5)}
        for name, idx in groups.items():
            if len(idx) >= 20:
                cells.append((f, name, idx))
    if not cells:
        return {"thr": None, "hits": [], "tested": 0}
    obs = np.array([nu[idx].mean() - base for _, _, idx in cells])
    rng = np.random.default_rng(seed)
    null = np.empty(reps)
    for k in range(reps):
        p = rng.permutation(nu)
        null[k] = np.max(np.abs([p[idx].mean() - base for _, _, idx in cells]))
    thr = float(np.percentile(null, 95))
    hits = [{"feat": f, "bucket": name, "d_e_net": round(float(o), 4),
             "n": int(len(idx))}
            for (f, name, idx), o in zip(cells, obs) if abs(o) >= thr]
    hits.sort(key=lambda x: -abs(x["d_e_net"]))
    return {"thr": round(thr, 4), "hits": hits, "tested": len(cells)}



def _edges(vals: np.ndarray) -> list:
    return [float(x) for x in np.quantile(vals, [0.2, 0.4, 0.6, 0.8])]


def bucketize(dev: list, test: list, feats) -> dict:
    """dev 定边界 → 同一套边界/取值套 test;返回 {feat: {bucket: {dev,test}}}。"""
    res: dict = {}
    for f in feats:
        groups: dict = {}
        if f in DISC_FEATS:
            keys = sorted({str(r.get(f)) for r in dev + test
                           if r.get(f) is not None})
            for k in keys:
                def sel(r, f=f, k=k):
                    return str(r.get(f)) == k
                groups[k] = sel
        else:
            dv = np.array([r[f] for r in dev if r.get(f) is not None], float)
            if len(dv) < 60:
                continue
            e = _edges(dv)
            for idx, name in enumerate(("q1", "q2", "q3", "q4", "q5")):
                def sel(r, f=f, e=e, idx=idx):
                    v = r.get(f)
                    if v is None:
                        return False
                    return sum(1 for x in e if v > x) == idx
                groups[name] = sel
        block = {}
        for name, sel in groups.items():
            dsub = [r for r in dev if sel(r)]
            if len(dsub) < 20:
                continue
            tsub = [r for r in test if sel(r)]
            block[name] = {"dev": stats(dsub), "test": stats(tsub)}
        if block:
            res[f] = block
    return res


def sim_table(rows: list) -> dict:
    out = {}
    base = np.array([r["net_r"] for r in rows], float)
    cost = np.array([r["cost_r"] for r in rows], float)
    for rule in RULE_NAMES:
        vals, trig = [], 0
        for r in rows:
            v = (r.get("sims") or {}).get(rule)
            if v is None:
                vals.append(r["gross_r"])
            else:
                trig += 1
                vals.append(float(v))
        g = np.array(vals, float)
        nu = g - cost
        win = g > 0
        gw = float(g[win].sum()) if win.any() else 0.0
        gl = float(-g[~win].sum()) if (~win).any() else 0.0
        out[rule] = {"n": len(rows), "triggered": trig,
                     "wr": round(float(win.mean()), 3),
                     "payoff": round(gw / gl, 3) if gl > 0 else None,
                     "e_gross": round(float(g.mean()), 4),
                     "e_net": round(float(nu.mean()), 4),
                     "d_e_net": round(float(nu.mean() - base.mean()), 4)}
    return out


def align_key(r) -> "str | None":
    """入场方向 × 更粗一档周期已收盘 EMA200 方向(1d 无更粗周期)。"""
    v = r.get("x_ema200_atr")
    if v is None:
        return None
    return f"align{int(r['direction']) * (1 if v > 0 else -1)}"


def group_table(dev: list, test: list, keyfn=align_key) -> dict:
    out = {}
    for seg, rows in (("dev", dev), ("test", test)):
        groups: dict = {}
        for r in rows:
            k = keyfn(r)
            if k:
                groups.setdefault(k, []).append(r)
        out[seg] = {k: stats(v, True) for k, v in sorted(groups.items())}
    return out


def perm_spread(rows: list, keyfn=align_key, reps: int = 5000, seed: int = 5):
    """低自由度分组:组间均值极差的置换 p 值(双侧)。"""
    vals, labs = [], []
    for r in rows:
        k = keyfn(r)
        if k:
            vals.append(r["net_r"])
            labs.append(k)
    if len(vals) < 40 or len(set(labs)) < 2:
        return {"spread": None, "p": None}
    v, lab = np.array(vals, float), np.array(labs)
    groups = sorted(set(labs))
    if min(int((lab == g).sum()) for g in groups) < 8:
        return {"spread": None, "p": None}
    obs = float(max(v[lab == g].mean() for g in groups)
                - min(v[lab == g].mean() for g in groups))
    rng = np.random.default_rng(seed)
    cnt = 0
    for _ in range(reps):
        p = rng.permutation(v)
        d = float(max(p[lab == g].mean() for g in groups)
                  - min(p[lab == g].mean() for g in groups))
        if d >= obs:
            cnt += 1
    return {"spread": round(obs, 4), "p": round((cnt + 1) / (reps + 1), 4)}


def write_md(path: Path, source: str, base_by_tf: dict, buckets: dict,
             sims: dict, n_scanned: dict, perm: dict, tail: dict,
             groups: dict) -> None:
    L = [f"# 逐周期出入场因子挖掘 — {source}", "",
         "只读诊断。Δ 均相对本周期基线;分桶边界由 dev 分位定,test 原样套用。",
         "毛 R = gross_pnl/risk_usdt;净 R = net_pnl/risk_usdt;"
         "盈亏比 = Σ盈利毛R / |Σ亏损毛R|。",
         "hold_h / mfe_r / t_mfe_h 属持仓期特征(用于设计出场, 不能当入场过滤)。",
         ""]
    for tf in HTF:
        b = base_by_tf.get(tf)
        if not b or not b.get("dev", {}).get("n"):
            continue
        L += [f"## {tf}", "",
              "| 分段 | N | 胜率 | 均盈(R) | 均亏(R) | 盈亏比 | 毛E[R] | 净E[R] | 净$ |",
              "|---|---|---|---|---|---|---|---|---|"]
        for seg in ("dev", "test"):
            s = b.get(seg) or {}
            if not s.get("n"):
                continue
            L.append(f"| {seg} | {s['n']} | {s['wr']:.1%} | {s.get('avg_win')} | "
                     f"{s.get('avg_loss')} | {s.get('payoff')} | {s['e_gross']} | "
                     f"{s['e_net']} | {s['net_usd']} |")
        L.append("")
        p = perm.get(tf) or {}
        t = tail.get(tf) or {}
        if t:
            L += ["尾部分布(净R):", "",
                  "| 分段 | N | 中位R | 前5%笔贡献R | 前5%占净额 | ≥3R 笔数 | ≥3R 合计R | ≤-1R 笔数 |",
                  "|---|---|---|---|---|---|---|---|"]
            for seg in ("dev", "test"):
                s = t.get(seg) or {}
                if not s.get("n"):
                    continue
                L.append(f"| {seg} | {s['n']} | {s['median_R']} | {s['top5_R']} | "
                         f"{s['top5_share']} | {s['n_ge3R']} | {s['sum_ge3R']} | "
                         f"{s['n_le_m1R']} |")
            L.append("")
        if p.get("thr") is not None:
            L.append(f"因果因子置换检验(dev): 单元数={p['tested']}, "
                     f"FWER≈5% 阈值 |Δ净R| ≥ {p['thr']}; 通过单元 {len(p['hits'])} 个。")
            for h in p["hits"][:10]:
                L.append(f"- `{h['feat']}` {h['bucket']}  Δ净R={h['d_e_net']:+.3f} (N={h['n']})")
            L.append("")
        g = groups.get(tf) or {}
        if g.get("dev") or g.get("test"):
            L += ["低自由度分组 align(方向 × 更粗周期 EMA200):", "",
                  "| 段 | 组 | N | 胜率 | 盈亏比 | 净E[R] | 95%CI |",
                  "|---|---|---|---|---|---|---|"]
            for seg in ("dev", "test"):
                for k, s in (g.get(seg) or {}).items():
                    if not s.get("n"):
                        continue
                    L.append(f"| {seg} | {k} | {s['n']} | {s['wr']:.1%} | {s.get('payoff')} | "
                             f"{s['e_net']} | {s.get('e_net_ci')} |")
            L.append(f"置换检验: dev spread={g.get('p_dev',{}).get('spread')} "
                     f"p={g.get('p_dev',{}).get('p')} | test spread={g.get('p_test',{}).get('spread')} "
                     f"p={g.get('p_test',{}).get('p')}")
            L.append("")
        rows = []
        bd, bt = b.get("dev", {}), b.get("test", {})
        for f, block in buckets.get(tf, {}).items():
            for name, v in block.items():
                d, t = v["dev"], v.get("test") or {}
                if d["n"] < 25 or t.get("n", 0) < 10:
                    continue
                dd = d["e_net"] - bd.get("e_net", 0.0)
                dt = t["e_net"] - bt.get("e_net", 0.0)
                rows.append((f, name, d, t, dd, dt))
        rows.sort(key=lambda x: -x[4])
        L += ["| 因子 | 桶 | devN | dev胜率 | dev盈亏比 | devΔ净R | testN | test胜率 | testΔ净R | 同号 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for f, name, d, t, dd, dt in rows[:16]:
            L.append(f"| {f} | {name} | {d['n']} | {d['wr']:.1%} | {d.get('payoff')} | "
                     f"{dd:+.3f} | {t['n']} | {t['wr']:.1%} | {dt:+.3f} | "
                     f"{'Y' if dd * dt > 0 else 'n'} |")
        L += ["", f"扫描桶数={n_scanned.get(tf, 0)};表按 dev Δ净R 排序,"
                  "同号=dev/test 同向(最低稳健门槛)。", ""]
        s = sims.get(tf) or {}
        if s:
            L += ["| 出场规则 | 触发 | dev胜率 | dev盈亏比 | devΔ净R | test胜率 | test盈亏比 | testΔ净R |",
                  "|---|---|---|---|---|---|---|---|"]
            for rule in RULE_NAMES:
                dv, tv = (s.get("dev") or {}).get(rule), (s.get("test") or {}).get(rule)
                if not dv or not tv:
                    continue
                L.append(f"| {rule} | {dv['triggered']}/{dv['n']} | {dv['wr']:.1%} | "
                         f"{dv.get('payoff')} | {dv['d_e_net']:+.3f} | {tv['wr']:.1%} | "
                         f"{tv.get('payoff')} | {tv['d_e_net']:+.3f} |")
            L.append("")
    path.write_text("\n".join(L), encoding="utf-8")

def dump_features(path: Path, rows: list) -> None:
    cols = [c for c in rows[0].keys() if c != "sims"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols + [f"sim_{k}" for k in RULE_NAMES],
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in cols}
            s = r.get("sims") or {}
            row.update({f"sim_{k}": s.get(k) for k in RULE_NAMES})
            w.writerow(row)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("abcd", "solo"), default="abcd")
    ap.add_argument("--workspace", default="explore")
    ap.add_argument("--out", default="reports/factor_mine_20261023")
    a = ap.parse_args()

    files = trade_files(a.source, a.workspace)
    if not files:
        raise SystemExit(f"no trade csv for source={a.source}")
    rows = load_trades(files)
    syms = sorted({r["symbol"] for r in rows})
    print(f"[mine] trades={len(rows)} symbols={syms} files={len(files)}")

    frames = {}
    for sym in syms:
        for tf in HTF:
            frames[(sym, tf)] = frame_features(agg.load_tf(sym, tf))
        print(f"[mine] frames {sym} ok")
    h1 = {s: load_1h(s) for s in syms}

    rows = enrich(rows, frames, h1)
    print(f"[mine] enriched={len(rows)}")

    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    dump_features(out / "features.csv", rows)

    base_by_tf, buckets, sims, n_scanned = {}, {}, {}, {}
    perm, tail, groups = {}, {}, {}
    for tf in HTF:
        dev = [r for r in rows if r["tf"] == tf and r["seg"] == "dev"]
        test = [r for r in rows if r["tf"] == tf and r["seg"] == "test"]
        if not dev:
            continue
        base_by_tf[tf] = {"dev": stats(dev, True), "test": stats(test, True)}
        buckets[tf] = bucketize(dev, test, CONT_FEATS + DISC_FEATS)
        n_scanned[tf] = sum(len(v) for v in buckets[tf].values())
        sims[tf] = {"dev": sim_table(dev), "test": sim_table(test)}
        perm[tf] = perm_threshold(dev, ENTRY_FEATS + ("regime", "event_id",
                                                      "direction", "symbol"))
        tail[tf] = {"dev": tail_stats(dev), "test": tail_stats(test)}
        groups[tf] = group_table(dev, test)
        groups[tf]["p_dev"] = perm_spread(dev)
        groups[tf]["p_test"] = perm_spread(test)
        print(f"[mine] {tf}: dev={len(dev)} test={len(test)} "
              f"buckets={n_scanned[tf]} perm_hits={len(perm[tf]['hits'])}")

    (out / "factors.json").write_text(json.dumps(
        {"source": a.source, "workspace": a.workspace, "base": base_by_tf,
         "buckets": buckets, "sims": sims, "scanned": n_scanned,
         "perm": perm, "tail": tail, "groups": groups},
        ensure_ascii=False, indent=1), encoding="utf-8")
    write_md(out / "FACTORS.md",
             a.source if a.source == "abcd" else f"solo/{a.workspace}",
             base_by_tf, buckets, sims, n_scanned, perm, tail, groups)
    print(f"[mine] -> {out}")


if __name__ == "__main__":
    main()




