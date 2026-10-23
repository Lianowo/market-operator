"""每个时间尺度(2h/4h/12h/1d)的最优路径搜索 + 四尺度组合(只读诊断)。

目标(用户口径):在每个尺度**自己的**时间尺度上,把
  (a) 胜率 WR  (b) 盈亏比 payoff  (c) 净期望 E[R] = Σnet/Σrisk
同时推到该尺度能到达的最好水平;四尺度各自最优之后,再求最优组合。

为什么必须"先单尺度、后组合":
  `reports/tf_solo_20261023/RESULTS.md` §4/§9 曾把它归因于"组合链里共享 4 槽 + 300U 复利 +
  `portfolio_cap_frac`";`--stage crowd` 实测**推翻了"抢槽"**:全部 cap_skips 都是 `TF_CAP`,
  `PORTFOLIO_CAP` 恒为 0,四尺度并发峰值 13、每尺度 4 个(与独占账户完全相同)。真实耦合是
  **共享权益**:同一笔交易在两条权益路径下 `risk_usdt` 差 27%~50%,而 E[R] = Σnet/Σrisk 是
  按风险加权的。单尺度搜索(该 TF 独占账户)给出"格子在单一权益路径下的边缘",组合阶段再
  单独度量"进入共享账户后变成多少"——那是加权/粒度效应,不是槽位竞争。

搜索空间(全部是 config 里**已存在**的字段,不需要改 src/):
  入场闸(per-TF `router_params_by_tf`):
      min_box_rotations / min_box_quality / min_bar_impulse_atr / zone_cooldown_bars
  出口几何(同样 per-TF 可配):
      k_is(失效距离下限) / turtle_stop_atr(初始防线) /
      reject_target_mode(none|mid|opposite = 无目标/箱体中点/对侧缘) /
      reject_use_struct_stop(结构止损 vs 2·ATR)
  ⇒ 因此本脚本选出的"最优路径"**可以直接写进 config**,组合阶段无需任何全局开关。

搜索策略(不是无脑 argmax,控制选择性过拟合):
  R1 粗网格 4×3×2×2 = 48 格(dev)
  R2 在 R1 最优格上逐维细化(dev)
  R3 在 R2 最优格上扫出口几何 3×3×3×2 = 36 格(dev)
  选择准则:dev 上最大化 net E[R],约束 N ≥ 50% 基线 N(否则"只做 5 笔"也能赢);
  **test 段只用于报告,不参与选择**;另报 top-10 dev 格在 test 上的中位数 = 选择乐观度。

用法:
    python scripts/_diag_v21_tf_opt.py --stage entry          # 单尺度搜索(慢,~5min)
    python scripts/_diag_v21_tf_opt.py --stage combo          # 组合(需要上一步产物)
    python scripts/_diag_v21_tf_opt.py --stage crowd          # 跨尺度是否抢槽(78s)
    python scripts/_diag_v21_tf_opt.py --stage all
    python scripts/_diag_v21_tf_opt.py --stage entry --tf 12h --quick

地位:只读诊断(不改 config/、不改 src/、不落生产产物)。
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from dataclasses import replace as dc_replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from _diag_v21_abcd import HTF, Ctx                        # noqa: E402
from _diag_v21_tfsolo import run_frames, solo_metrics       # noqa: E402
from market_operator.campaign import run_campaigns          # noqa: E402

OUT = ROOT / "reports" / "tf_opt_20261023"
BOOK = "book4"                       # 生产账本(BTC/ETH/SOL/DOGE),单尺度=独占账户
BOOK_SYMS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT")
N_FLOOR = 0.5                        # 选择约束:N ≥ 50% 基线 N
ENTRY_KNOBS = ("min_box_rotations", "min_box_quality",
               "min_bar_impulse_atr", "zone_cooldown_bars")
EXIT_KNOBS = ("k_is", "turtle_stop_atr", "reject_target_mode",
              "reject_use_struct_stop")

# 粗网格 / 细化网格 / 出口网格
R1 = {"min_box_rotations": (0, 1, 2, 3),
      "min_box_quality": (0.35, 0.45, 0.55),
      "min_bar_impulse_atr": (0.0, 1.2),
      "zone_cooldown_bars": (0, 3)}
R2 = {"min_box_rotations": (0, 1, 2, 3, 4),
      "min_box_quality": (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60),
      "min_bar_impulse_atr": (0.0, 0.8, 1.2, 1.6),
      "zone_cooldown_bars": (0, 2, 3, 4, 6)}
R3 = {"k_is": (0.5, 0.8, 1.2),
      "turtle_stop_atr": (1.5, 2.0, 2.5),
      "reject_target_mode": ("none", "mid", "opposite"),
      "reject_use_struct_stop": (True, False)}


# ---------------------------------------------------------------------------
# 回放与选择
# ---------------------------------------------------------------------------

def rp_of(ctx: Ctx, tf: str, knobs: dict):
    """该 TF 当前 per-TF 参数 + 覆盖项(未覆盖的字段保持 config 现值)。"""
    cur = ctx.rp_by_tf.get(tf, ctx.base_rp)
    return dc_replace(cur, **knobs)


def sel_frames(ctx: Ctx, tf: str) -> dict:
    """该 TF 的全部战役(生产账本四币;单尺度 = 独占 300U 账户与全部风险预算)。"""
    return {k: v for k, v in ctx.frames.items()
            if v.tf_label == tf and v.symbol in BOOK_SYMS}


def run_tf(ctx: Ctx, tf: str, knobs: dict, *, segment: str, cm: float = 1.0) -> dict:
    """单尺度独占账户回放(a0 语义只换该 TF 的 router 参数)。"""
    saved = ctx.rp_by_tf
    ctx.rp_by_tf = dict(saved)
    ctx.rp_by_tf[tf] = rp_of(ctx, tf, knobs)
    try:
        res = run_frames(ctx, sel_frames(ctx, tf), segment=segment, cm=cm)
        m = solo_metrics(res)
    finally:
        ctx.rp_by_tf = saved
    m = {k: v for k, v in m.items() if k != "trades"}
    m["knobs"] = dict(knobs)
    return m


def score(m: dict, n_floor: float) -> float:
    """选择分数:净 E[R];低于 N 地板的一律压到 −9(不参与择优)。"""
    er = m.get("er_net")
    if m.get("n", 0) < n_floor or er is None:
        return -9.0
    return float(er)


def _grid(cells: dict) -> list:
    keys = list(cells)
    return [dict(zip(keys, vals)) for vals in itertools.product(*cells.values())]


def refine(coarse_best: dict, space: dict) -> list:
    """以 coarse_best 为不动点,逐维替换成 space 里的每个值(坐标搜索)。"""
    out = []
    for k, vals in space.items():
        for v in vals:
            cell = dict(coarse_best)
            cell[k] = v
            out.append(cell)
    # 去重(保持顺序)
    seen, uniq = set(), []
    for c in out:
        t = tuple(sorted(c.items()))
        if t not in seen:
            seen.add(t)
            uniq.append(c)
    return uniq


def trace(ctx: Ctx, tf: str, base_n: int, *, segment: str = "dev",
          cm: float = 1.0, quick: bool = False) -> tuple:
    """三段搜索,返回 (best_cell, 全部评估记录)。"""
    rows: list = []

    def _eval(cells: list, tag: str) -> dict:
        best, best_s = None, -9.0
        for i, cell in enumerate(cells, 1):
            m = run_tf(ctx, tf, cell, segment=segment, cm=cm)
            s = score(m, N_FLOOR * base_n)
            m["stage"] = tag
            m["score"] = round(s, 4)
            rows.append(m)
            if s > best_s:
                best, best_s = cell, s
            if i % 20 == 0:
                print(f"[{tf}] {tag} {i}/{len(cells)} best={best_s:+.3f}",
                      flush=True)
        print(f"[{tf}] {tag} done best={best_s:+.3f} ({best})", flush=True)
        return best

    g1 = _grid(R1)
    b1 = _eval(g1, "R1")
    if quick:
        return b1, rows
    g2 = [c for c in refine(b1, R2) if c != b1]
    b2 = _eval(g2, "R2")
    # R3:出口几何全因子(3×3×3×2 = 36),入场闸固定在 b2 的最优档。
    entry_part = {k: b2[k] for k in ENTRY_KNOBS if k in b2}
    g3 = [{**entry_part, **c} for c in _grid(R3)]
    b3 = _eval(g3, "R3")
    return b3, rows

# ---------------------------------------------------------------------------
# 终评:dev 选、test 验、1.5x 成本压力
# ---------------------------------------------------------------------------

def top_cells(rows: list, k: int = 10) -> list:
    """dev 分数前 k 的互不重复格子(用于度量"选择乐观度")。"""
    ok = [r for r in rows if r["score"] > -8]
    ok.sort(key=lambda r: -r["score"])
    out, seen = [], set()
    for r in ok:
        t = tuple(sorted(r["knobs"].items()))
        if t in seen:
            continue
        seen.add(t)
        out.append(r)
        if len(out) == k:
            break
    return out


def evaluate(ctx: Ctx, tf: str, knobs: dict) -> dict:
    """dev / test / full × cost 1.0/1.5 的终评矩阵。"""
    out = {}
    for seg in ("dev", "test", "full"):
        for cm in (1.0, 1.5):
            m = run_tf(ctx, tf, knobs, segment=seg, cm=cm)
            out[f"{seg}@{cm:g}"] = m
    return out


def stage_entry(ctx: Ctx, tfs: list, quick: bool) -> dict:
    result = {}
    OUT.mkdir(parents=True, exist_ok=True)
    for tf in tfs:
        t0 = time.time()
        base = run_tf(ctx, tf, {}, segment="dev")
        base_n = base["n"]
        print(f"[{tf}] base dev N={base_n} E[R]={base['er_net']} "
              f"WR={base['wr_net']} pay={base['payoff_net']}", flush=True)
        best, rows = trace(ctx, tf, base_n, quick=quick)
        ev = evaluate(ctx, tf, best)
        tops = top_cells(rows, 10)
        top_test = []
        for r in tops:
            m = run_tf(ctx, tf, r["knobs"], segment="test")
            top_test.append({"knobs": r["knobs"], "dev_er": r["er_net"],
                             "dev_n": r["n"], "test_er": m.get("er_net"),
                             "test_n": m.get("n")})
        result[tf] = {
            "base": {k: v for k, v in base.items() if k != "knobs"},
            "base_full": {k: v for k, v in run_tf(ctx, tf, {}, segment="full").items()
                          if k != "knobs"},
            "best_knobs": best,
            "best_eval": ev,
            "n_cells": len(rows),
            "top_dev_cells": top_test,
            "grid": rows,
        }
        e = ev
        print(f"[{tf}] best {best} -> dev={e['dev@1']['er_net']:+.3f} "
              f"test={e['test@1']['er_net']:+.3f} full={e['full@1']['er_net']:+.3f} "
              f"({len(rows)} cells, {time.time()-t0:.0f}s)", flush=True)
        (OUT / f"entry_{tf}.json").write_text(
            json.dumps(result[tf], ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "entry_all.json").write_text(
        json.dumps({tf: {k: v for k, v in r.items() if k != "grid"}
                    for tf, r in result.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# 出口叠加屏(features.csv 的 sim_* :同一批交易、只换出场规则)
# ---------------------------------------------------------------------------

SIM_KEYS = ("sim_tp0.5_full", "sim_tp0.75_full", "sim_tp1_full",
            "sim_tp1.5_full", "sim_tp2_full", "sim_tp0.75_half_be",
            "sim_tp1_half_be", "sim_tp1.5_half_be", "sim_be1_full",
            "sim_trail05_after1", "sim_tc3", "sim_tc6", "sim_tc12",
            "sim_tc24", "sim_tl3", "sim_tl6", "sim_tl12")


def _wr_payoff(x):
    w = [v for v in x if v > 0]
    l = [v for v in x if v <= 0]
    if not w or not l:
        return None, None
    return len(w) / len(x), (sum(w) / len(w)) / (-sum(l) / len(l))


def stage_exit(path: Path) -> dict:
    """对每个 TF×段,比较"实际出场"与 17 条叠加规则(净 = 规则毛 R − 该笔实际成本 R)。

    注意:固定成交集合 ⇒ 度量的是"出场几何"的纯效应,不含换出场后重新入场/复利差异。
    """
    import numpy as np
    import pandas as pd
    d = pd.read_csv(path)
    d["cost_r"] = d["gross_r"] - d["net_r"]
    out = {}
    for tf, g in d.groupby("tf"):
        out[tf] = {}
        for seg, h in g.groupby("seg"):
            row = {"n": int(len(h))}
            base = h["net_r"].to_numpy()
            wr, pay = _wr_payoff(list(base))
            row["actual"] = {"er": round(float(base.mean()), 4),
                             "wr": None if wr is None else round(wr, 4),
                             "payoff": None if pay is None else round(pay, 4)}
            for k in SIM_KEYS:
                if k not in h:
                    continue
                s = h[k]
                cov = float(s.notna().mean())
                if cov < 0.10:
                    continue
                # None = 规则未触发 ⇒ 该笔仍按实际出场结算(不是剔除)。
                net = (s.fillna(h["gross_r"]) - h["cost_r"]).to_numpy()
                wr, pay = _wr_payoff(list(net))
                row[k] = {"er": round(float(net.mean()), 4),
                          "wr": None if wr is None else round(wr, 4),
                          "payoff": None if pay is None else round(pay, 4),
                          "trigger": round(cov, 3)}
            out[tf][seg] = row
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "exit_screen.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for tf in ("2h", "4h", "12h", "1d"):
        if tf not in out:
            continue
        for seg in ("dev", "test"):
            r = out[tf].get(seg)
            if not r:
                continue
            best = max(((k, v) for k, v in r.items()
                        if isinstance(v, dict) and "er" in v),
                       key=lambda kv: kv[1]["er"])
            print(f"[exit] {tf:4s} {seg:4s} n={r['n']:4d} actual ER="
                  f"{r['actual']['er']:+.3f} -> best {best[0]} "
                  f"ER={best[1]['er']:+.3f} WR={best[1]['wr']} "
                  f"pay={best[1]['payoff']}", flush=True)
    return out


# ---------------------------------------------------------------------------
# 组合阶段
# ---------------------------------------------------------------------------

def run_chain(ctx: Ctx, frames: dict, *, segment: str, cm: float,
              tfcap: dict | None = None, keep_trades: bool = False,
              pcap: float | None = None, keep_meta: bool = False) -> dict:
    """全链回放(= Ctx.run 的 a0 分支,但允许换 frames / 预算)。"""
    cfg = ctx.cfg
    res = run_campaigns(
        frames, segment=segment, dev_frac=float(cfg["dev_frac"]),
        equity0=300.0, risk_frac=float(cfg["risk_frac"]),
        leverage_cap=float(cfg["leverage_cap"]),
        taker_fee=float(cfg["taker_fee"]), maker_fee=float(cfg["maker_fee"]),
        slippage_per_side=float(cfg["slippage_per_side"]),
        funding_per_8h=float(cfg["funding_per_8h_per_side"]),
        cost_multiplier=cm, contracts=ctx.contracts,
        params=ctx.params, n_fill=int(cfg["n_fill"]),
        portfolio_cap_frac=float(cfg["portfolio_cap_frac"] if pcap is None
                                 else pcap),
        tf_cap_by_tf=dict(tfcap or cfg["tf_cap_by_tf"]), arm=ctx.arm,
        router_params=ctx.base_rp, enable_mic=True,
        router_params_by_tf=ctx.rp_by_tf)
    m = {k: v for k, v in solo_metrics(res).items() if k != "trades"}
    if keep_trades:
        m["_trades"] = [t for t in res.trades
                        if t.get("closed") and t.get("in_segment_open")]
    if keep_meta:
        m["_cap_skips"] = list(getattr(res, "cap_skips", ()) or ())
        m["_counters"] = {k: v for k, v in (res.counters or {}).items()
                          if not isinstance(v, dict)}
    by: dict = {}
    for t in res.trades:
        if not (t.get("closed") and t.get("in_segment_open")):
            continue
        b = by.setdefault(t.get("tf"), {"n": 0, "net": 0.0, "risk": 0.0})
        b["n"] += 1
        b["net"] += t["net_pnl"]
        b["risk"] += t["risk_usdt"]
    for v in by.values():
        v["net"] = round(v["net"], 4)
        v["er"] = round(v["net"] / v["risk"], 4) if v["risk"] else None
    m["by_tf"] = {k: v for k, v in sorted(by.items())}
    return m


def stage_combo(ctx: Ctx, picks: dict) -> dict:
    """组合:逐变体跑 dev/test/full × 1.0/1.5。"""
    def with_picks(sub: dict) -> dict:
        cur = dict(ctx.rp_by_tf)
        for tf, knobs in sub.items():
            cur[tf] = rp_of(ctx, tf, knobs)
        return cur

    allp = {tf: picks[tf] for tf in HTF if tf in picks}
    variants = {
        "a0": ({}, None, None),
        "best_all": (allp, None, None),
        "best_2h4h": ({tf: allp[tf] for tf in ("2h", "4h") if tf in allp},
                      None, None),
        "best_all_no12h": (allp, None, "12h"),
        "best_all_cap_B": (allp, {"2h": 0.0225, "4h": 0.0375,
                                  "12h": 0.0075, "1d": 0.0225}, None),
    }
    out = {}
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (sub, tfcap, drop) in variants.items():
        saved = ctx.rp_by_tf
        ctx.rp_by_tf = with_picks(sub)
        frames = ctx.frames if drop is None else {
            k: v for k, v in ctx.frames.items() if v.tf_label != drop}
        try:
            res = {}
            for seg in ("dev", "test", "full"):
                for cm in (1.0, 1.5):
                    res[f"{seg}@{cm:g}"] = run_chain(
                        ctx, frames, segment=seg, cm=cm, tfcap=tfcap)
        finally:
            ctx.rp_by_tf = saved
        out[name] = res
        r = res["full@1"]
        per = " ".join(f"{tf}={v['er'] if v['er'] is None else round(v['er'],3)}"
                       for tf, v in r["by_tf"].items())
        print(f"[combo] {name:16s} full@1 n={r['n']:4d} net={r['net']:+8.1f} "
              f"ER={r['er_net']:+.3f} WR={r['wr_net']:.3f} pay={r['payoff_net']} "
              f"DD={r['dd_pct']:.1f}% | {per}", flush=True)
    (OUT / "combo.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 共存阶段:跨尺度到底"抢"了什么
# ---------------------------------------------------------------------------

MS_H = 3_600_000.0


def _ids(trades: list) -> set:
    """交易身份:(symbol, 决策时刻, 方向)。同参数下应逐笔可比。"""
    return {(t.get("symbol"), t.get("open_decision_ts"), t.get("direction"))
            for t in trades if t.get("open_decision_ts") is not None}


def _max_concurrent(trades: list, key=None) -> dict:
    """同时持仓峰值(按 key 分组;key None ⇒ 全账户)。"""
    ev = []
    for t in trades:
        if t.get("exit_ts") is None:
            continue
        k = key(t) if key is not None else "ALL"
        ev.append((int(t["open_ts"]), 1, k))
        ev.append((int(t["exit_ts"]), -1, k))
    ev.sort(key=lambda x: (x[0], x[1]))           # 同时刻先平后开
    cur: dict = {}
    peak: dict = {}
    for _ts, d, k in ev:
        cur[k] = cur.get(k, 0) + d
        peak[k] = max(peak.get(k, 0), cur[k])
    return peak


def _cross_tf_overlap(trades: list) -> dict:
    """同币、不同尺度、同时持仓的小时数(风险相关性的直接度量)。"""
    by: dict = {}
    for t in trades:
        if t.get("exit_ts") is not None:
            by.setdefault(t.get("symbol"), []).append(t)
    hours = ov = 0.0
    pairs = 0
    for _sym, ts in by.items():
        for t in ts:
            hours += (int(t["exit_ts"]) - int(t["open_ts"])) / MS_H
        for a, b in itertools.combinations(ts, 2):
            if a.get("tf") == b.get("tf"):
                continue
            lo = max(int(a["open_ts"]), int(b["open_ts"]))
            hi = min(int(a["exit_ts"]), int(b["exit_ts"]))
            if hi > lo:
                ov += (hi - lo) / MS_H
                pairs += 1
    return {"overlap_h": round(ov, 1), "trade_hours": round(hours, 1),
            "overlap_share": round(ov / hours, 3) if hours else None,
            "pairs": pairs}


def _cap_counts(m: dict) -> dict:
    c: dict = {}
    for s in m.get("_cap_skips", ()) or ():
        c[s.get("reason_code")] = c.get(s.get("reason_code"), 0) + 1
    return c


def _brief(m: dict) -> dict:
    net = m.get("net")
    return {"n": m.get("n"), "er": m.get("er_net"),
            "net": None if net is None else round(net, 2),
            "gross": m.get("gross"), "cost": m.get("cost"),
            "cap_skips": _cap_counts(m),
            "portfolio_cap_skips": (m.get("_counters") or {}).get(
                "portfolio_cap_skips"),
            "min_size_skips": (m.get("_counters") or {}).get("min_size_skips"),
            "by_tf": m.get("by_tf")}


def stage_crowd(ctx: Ctx, picks: dict) -> dict:
    """跨尺度共存到底抢了什么:**同一批参数,只改"别的尺度是否在场"**。

    三个臂(a0 参数):
      solo     该 TF 独占账户(frames 只留该 TF) —— 基准
      coexist  四尺度同时在场(生产形态)
      loose    四尺度在场 + 预算全放开(周期池=账户池=100%)
    若 coexist 与 solo 逐笔相同 ⇒ **不存在跨尺度抢占**;若 loose == coexist
    ⇒ 预算闸根本没被触发,差异只能来自共享权益(尺寸/复利/最小手数)。
    另测:同币跨尺度重叠小时数(BTC 在 2h/4h/12h 同时持仓 = 同一注下三份)。
    """
    tfs = list(HTF)
    frames_all = {k: v for k, v in ctx.frames.items() if v.symbol in BOOK_SYMS}
    coexist = run_chain(ctx, frames_all, segment="full", cm=1.0,
                        keep_trades=True, keep_meta=True)
    loose = run_chain(ctx, frames_all, segment="full", cm=1.0,
                      tfcap={tf: 1.0 for tf in tfs}, pcap=1.0,
                      keep_trades=True, keep_meta=True)
    out: dict = {"coexist": _brief(coexist), "loose": _brief(loose),
                 "single": {}, "delta": {}, "concurrency": {}, "overlap": {}}

    co_tr = coexist["_trades"]
    out["concurrency"]["coexist_all"] = _max_concurrent(co_tr)
    out["concurrency"]["coexist_by_tf"] = _max_concurrent(
        co_tr, key=lambda t: t.get("tf"))
    out["concurrency"]["loose_all"] = _max_concurrent(loose["_trades"])
    out["overlap"]["coexist"] = _cross_tf_overlap(co_tr)
    out["overlap"]["loose"] = _cross_tf_overlap(loose["_trades"])

    for tf in tfs:
        one = {k: v for k, v in frames_all.items() if v.tf_label == tf}
        if not one:
            continue
        s = run_chain(ctx, one, segment="full", cm=1.0,
                      keep_trades=True, keep_meta=True)
        out["single"][tf] = _brief(s)
        out["concurrency"].setdefault("single_all", {})[tf] = _max_concurrent(
            s["_trades"])
        sub = [t for t in co_tr if t.get("tf") == tf]
        a_ids, b_ids = _ids(s["_trades"]), _ids(sub)
        common = a_ids & b_ids
        amap = {(t.get("symbol"), t.get("open_decision_ts"),
                 t.get("direction")): t for t in s["_trades"]}
        bmap = {(t.get("symbol"), t.get("open_decision_ts"),
                 t.get("direction")): t for t in sub}
        dq = []
        for k in common:
            ra = float(amap[k].get("risk_usdt") or 0.0)
            rb = float(bmap[k].get("risk_usdt") or 0.0)
            if ra > 0:
                dq.append(abs(rb - ra) / ra)
        out["delta"][tf] = {
            "n_solo": len(s["_trades"]), "n_coexist": len(sub),
            "only_solo": len(a_ids - b_ids), "only_coexist": len(b_ids - a_ids),
            "common": len(common),
            "risk_dev_pct": round(100.0 * sum(dq) / len(dq), 2) if dq else None,
            "er_solo": s.get("er_net"),
            "er_coexist": (round(sum(t["net_pnl"] for t in sub) /
                                 (sum(t["risk_usdt"] for t in sub) or 1.0), 4)
                           if sub else None),
            "same_ids": a_ids == b_ids,
        }
        d = out["delta"][tf]
        print(f"[crowd] {tf:4s} solo n={d['n_solo']:4d} coexist n={d['n_coexist']:4d} "
              f"only_solo={d['only_solo']:3d} only_coexist={d['only_coexist']:3d} "
              f"risk_dev={d['risk_dev_pct']}% ER {d['er_solo']:+.4f} -> "
              f"{d['er_coexist']:+.4f}", flush=True)

    if picks:
        allp = {tf: picks[tf] for tf in tfs if tf in picks}
        if allp:
            saved = ctx.rp_by_tf
            cur = dict(saved)
            for tf, knobs in allp.items():
                cur[tf] = rp_of(ctx, tf, knobs)
            ctx.rp_by_tf = cur
            try:
                out["best_coexist"] = _brief(run_chain(
                    ctx, frames_all, segment="full", cm=1.0, keep_meta=True))
                out["best_coexist_loose"] = _brief(run_chain(
                    ctx, frames_all, segment="full", cm=1.0,
                    tfcap={tf: 1.0 for tf in tfs}, pcap=1.0, keep_meta=True))
            finally:
                ctx.rp_by_tf = saved
            print(f"[crowd] best_all cap_skips "
                  f"{out['best_coexist']['cap_skips']} | loose "
                  f"{out['best_coexist_loose']['cap_skips']}", flush=True)

    print(f"[crowd] coexist cap_skips={out['coexist']['cap_skips']} "
          f"loose={out['loose']['cap_skips']} | 峰值持仓 "
          f"{out['concurrency']['coexist_all']} (by TF "
          f"{out['concurrency']['coexist_by_tf']}) | 同币跨尺度重叠 "
          f"{out['overlap']['coexist']}", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "crowd.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out



def stage_decomp(ctx: Ctx, tfs: list) -> dict:
    """成本结构分解 + 同集反事实 + 组合可加性(回答"成本鲁棒化"与"为何叠加最差")。

    三块:
      A cost      : 每 TF × {base,best} 的 gross / fees / funding / slip / 名义÷风险
      B same_set  : 同一交易集在 m=1.5 的反事实 net,以及盈亏平衡成本乘数 m*
      C combo_add : 组合(test@1.0)逐尺度 ER/风险权重,检验 ΔER 是否等于 Σ w·ΔER
    """
    out = {"cost": {}, "same_set": {}, "combo_add": {}}
    picks = {}
    for tf in tfs:
        p = OUT / f"entry_{tf}.json"
        if p.exists():
            picks[tf] = json.loads(p.read_text(encoding="utf-8"))["best_knobs"]
    bev = OUT / "base_eval.json"
    obs15 = json.loads(bev.read_text(encoding="utf-8")) if bev.exists() else {}

    for tf in picks:
        # A. 成本逐项(按 R 归一)
        out["cost"][tf] = {}
        stats = {}
        for tag, knobs in (("base", {}), ("best", picks[tf])):
            tr = _trades_of(ctx, tf, knobs, "full", 1.0)
            risk = sum(t["risk_usdt"] for t in tr) or 1.0
            g = sum(t.get("gross_pnl", 0.0) for t in tr)
            net = sum(t["net_pnl"] for t in tr)
            d = dict(
                n=len(tr), gross_er=round(g / risk, 4),
                cost_er=round((g - net) / risk, 4), net_er=round(net / risk, 4),
                fees_er=round(sum(t.get("fees", 0.0) for t in tr) / risk, 4),
                funding_er=round(sum(t.get("funding", 0.0) for t in tr) / risk, 4),
                slip_er=round(sum(t.get("slippage_cost", 0.0) for t in tr) / risk, 4),
                notional_over_risk=round(
                    sum(t.get("notional", 0.0) for t in tr) / risk, 2),
                hold_h=round(sum(t.get("hold_hours", 0.0) for t in tr) / max(len(tr), 1), 1),
                # 成本吃掉毛利的比例:越高越"吃成本"
                cost_share=round((g - net) / g, 4) if g else None)
            out["cost"][tf][tag] = d
            stats[tag] = dict(g=g / risk, c=(g - net) / risk, n=len(tr))

        # B. 同集反事实 + 交易集重叠
        out["same_set"][tf] = {}
        for tag, knobs in (("base", {}), ("best", picks[tf])):
            a = {(t["symbol"], str(t["open_ts"]), t.get("direction")): t
                 for t in _trades_of(ctx, tf, knobs, "full", 1.0)}
            b = {(t["symbol"], str(t["open_ts"]), t.get("direction")): t
                 for t in _trades_of(ctx, tf, knobs, "full", 1.5)}
            common = set(a) & set(b)
            rc = sum(a[k]["risk_usdt"] for k in common) or 1.0
            out["same_set"][tf][tag] = dict(
                n_1=len(a), n_15=len(b), n_common=len(common),
                overlap_pct=round(100.0 * len(common) / max(len(a), 1), 1),
                gross_er_common=round(
                    sum(a[k]["gross_pnl"] for k in common) / rc, 4))
        b, x = stats["base"], stats["best"]
        dg, dc = x["g"] - b["g"], b["c"] - x["c"]
        m_star = round(-dg / dc, 3) if dc else None
        out["same_set"][tf]["delta"] = dict(
            d_gross=round(dg, 4), d_cost=round(x["c"] - b["c"], 4),
            m_star=m_star,
            same_set_1x=round((x["g"] - x["c"]) - (b["g"] - b["c"]), 4),
            same_set_15x=round((x["g"] - 1.5 * x["c"]) - (b["g"] - 1.5 * b["c"]), 4),
            observed_15x=(round(obs15[tf]["best"]["full@1.5"]["er_net"]
                                - obs15[tf]["base"]["full@1.5"]["er_net"], 4)
                          if tf in obs15 else None))
        print(f"[decomp] {tf:4s} cost_er {b['c']:.4f}->{x['c']:.4f} "
              f"gross {b['g']:.4f}->{x['g']:.4f} m*={m_star} "
              f"同集差@1.5={out['same_set'][tf]['delta']['same_set_15x']:+.4f} "
              f"实测={out['same_set'][tf]['delta']['observed_15x']:+.4f}", flush=True)

    # C. 组合可加性(test@1.0)
    for name, sub in (("a0", {}),
                      ("best_all", picks),
                      ("best_2h4h", {k: picks[k] for k in ("2h", "4h")
                                     if k in picks})):
        saved = ctx.rp_by_tf
        cur = dict(saved)
        for tf, knobs in sub.items():
            cur[tf] = rp_of(ctx, tf, knobs)
        ctx.rp_by_tf = cur
        try:
            r = run_chain(ctx, ctx.frames, segment="test", cm=1.0)
        finally:
            ctx.rp_by_tf = saved
        tot = sum(v["risk"] for v in r["by_tf"].values()) or 1.0
        out["combo_add"][name] = {
            "er": r["er_net"], "n": r["n"],
            "by_tf": {tf: dict(n=v["n"], net=round(v["net"], 2),
                               risk=round(v["risk"], 2),
                               weight=round(v["risk"] / tot, 4),
                               er=v["er"]) for tf, v in r["by_tf"].items()}}
        print(f"[decomp] combo {name:10s} ER={r['er_net']:+.4f} n={r['n']} | "
              + " ".join(f"{tf}={v['er']:+.3f}(w{v['risk']/tot:.0%})"
                         for tf, v in r["by_tf"].items()), flush=True)

    # 用 a0 的权重把逐尺度 ΔER 可加地预测 best_all / best_2h4h
    if "a0" in out["combo_add"] and "best_all" in out["combo_add"]:
        a0 = out["combo_add"]["a0"]["by_tf"]
        for name in ("best_all", "best_2h4h"):
            if name not in out["combo_add"]:
                continue
            bb = out["combo_add"][name]["by_tf"]
            pred = sum(a0[tf]["weight"] * (bb[tf]["er"] - a0[tf]["er"])
                       for tf in a0 if tf in bb)
            out["combo_add"][name]["predicted_delta"] = round(pred, 4)
            out["combo_add"][name]["actual_delta"] = round(
                out["combo_add"][name]["er"] - out["combo_add"]["a0"]["er"], 4)
            out["combo_add"][name]["contrib"] = {
                tf: round(a0[tf]["weight"] * (bb[tf]["er"] - a0[tf]["er"]), 4)
                for tf in a0 if tf in bb}
            print(f"[decomp] {name}: 可加预测 Δ={pred:+.4f} vs 实测 "
                  f"{out['combo_add'][name]['actual_delta']:+.4f}", flush=True)

    (OUT / "decomp.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 置信区间:逐笔 bootstrap(按笔重采样,Σnet/Σrisk)
# ---------------------------------------------------------------------------
def _er_ci(trades: list, n_boot: int = 2000, seed: int = 7) -> dict:
    import numpy as np
    net = np.array([t["net_pnl"] for t in trades], dtype=float)
    risk = np.array([t["risk_usdt"] for t in trades], dtype=float)
    if len(net) < 5 or risk.sum() <= 0:
        return {"n": int(len(net)), "er": None, "lo": None, "hi": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(net), size=(n_boot, len(net)))
    vals = net[idx].sum(1) / risk[idx].sum(1)
    return {"n": int(len(net)), "er": round(float(net.sum() / risk.sum()), 4),
            "lo": round(float(np.percentile(vals, 2.5)), 4),
            "hi": round(float(np.percentile(vals, 97.5)), 4)}


def _trades_of(ctx: Ctx, tf: str, knobs: dict, segment: str, cm: float = 1.0) -> list:
    saved = ctx.rp_by_tf
    ctx.rp_by_tf = dict(saved)
    ctx.rp_by_tf[tf] = rp_of(ctx, tf, knobs)
    try:
        res = run_frames(ctx, sel_frames(ctx, tf), segment=segment, cm=cm)
    finally:
        ctx.rp_by_tf = saved
    return [t for t in res.trades
            if t.get("closed") and t.get("in_segment_open")]


def stage_ci(ctx: Ctx, tfs: list) -> dict:
    """每 TF(base/best)×段 + 组合各变体(test 段)的 E[R] 95%CI。"""
    out = {"tf": {}, "combo": {}}
    for tf in tfs:
        p = OUT / f"entry_{tf}.json"
        if not p.exists():
            continue
        best = json.loads(p.read_text(encoding="utf-8"))["best_knobs"]
        out["tf"][tf] = {}
        for tag, knobs in (("base", {}), ("best", best)):
            for seg in ("dev", "test"):
                c = _er_ci(_trades_of(ctx, tf, knobs, seg))
                out["tf"][tf][f"{tag}@{seg}"] = c
                print(f"[ci] {tf:4s} {tag:5s} {seg:4s} n={c['n']:4d} "
                      f"ER={c['er']:+.3f} [{c['lo']:+.3f},{c['hi']:+.3f}]",
                      flush=True)
    picks = {}
    for tf in HTF:
        p = OUT / f"entry_{tf}.json"
        if p.exists():
            picks[tf] = json.loads(p.read_text(encoding="utf-8"))["best_knobs"]
    variants = {"a0": {}, "best_all": picks,
                "best_2h4h": {tf: picks[tf] for tf in ("2h", "4h") if tf in picks}}
    for name, sub in variants.items():
        saved = ctx.rp_by_tf
        cur = dict(saved)
        for tf, knobs in sub.items():
            cur[tf] = rp_of(ctx, tf, knobs)
        ctx.rp_by_tf = cur
        try:
            res = run_chain(ctx, ctx.frames, segment="test", cm=1.0,
                            keep_trades=True)
            tr = res.get("_trades") or []
        finally:
            ctx.rp_by_tf = saved
        out["combo"][name] = _er_ci(tr or [])
        print(f"[ci] combo {name:10s} test n={out['combo'][name]['n']:4d} "
              f"ER={out['combo'][name]['er']}", flush=True)
    (OUT / "ci.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def stage_base(ctx: Ctx, tfs: list) -> dict:
    """基线 vs 最优 的完整终评矩阵(dev/test/full × 1.0/1.5),读已有 best_knobs。"""
    out = {}
    for tf in tfs:
        p = OUT / f"entry_{tf}.json"
        if not p.exists():
            print(f"[base] 缺 {p}，跳过 {tf}", flush=True)
            continue
        best = json.loads(p.read_text(encoding="utf-8"))["best_knobs"]
        out[tf] = {"best_knobs": best, "base": evaluate(ctx, tf, {}),
                   "best": evaluate(ctx, tf, best)}
        for tag in ("base", "best"):
            r = out[tf][tag]
            print(f"[base] {tf:4s} {tag:5s} " + " ".join(
                f"{k}={r[k]['er_net']:+.3f}" for k in r), flush=True)
    (OUT / "base_eval.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    choices=("entry", "exit", "base", "ci", "combo", "crowd",
                             "decomp", "all"))
    ap.add_argument("--tf", default="", help="逗号分隔;缺省 = 四个尺度全跑")
    ap.add_argument("--quick", action="store_true", help="只跑 R1 粗网格")
    ap.add_argument("--features", default=str(
        ROOT / "reports" / "factor_mine_20261023" / "features.csv"))
    ap.add_argument("--picks", default="",
                    help="combo 阶段用现成 picks json(缺省读 entry_<tf>.json)")
    args = ap.parse_args()

    tfs = [t.strip() for t in args.tf.split(",") if t.strip()] or list(HTF)

    if args.stage == "exit":
        t0 = time.time()
        stage_exit(Path(args.features))
        print(f"\n[out] {OUT}  ({time.time()-t0:.0f}s)", flush=True)
        return

    t0 = time.time()
    ctx = Ctx(only=None)
    print(f"[prep] frames ready ({time.time()-t0:.0f}s, {len(ctx.frames)} campaigns)",
          flush=True)

    if args.stage in ("entry", "all"):
        stage_entry(ctx, tfs, args.quick)

    if args.stage in ("base", "ci"):
        stage_base(ctx, tfs)

    if args.stage == "ci":
        stage_ci(ctx, tfs)

    if args.stage in ("decomp", "all"):
        stage_decomp(ctx, tfs)

    if args.stage in ("combo", "all"):
        if args.picks:
            picks = json.loads(Path(args.picks).read_text(encoding="utf-8"))
        else:
            picks = {}
            for tf in HTF:
                p = OUT / f"entry_{tf}.json"
                if p.exists():
                    picks[tf] = json.loads(p.read_text(encoding="utf-8"))["best_knobs"]
        print(f"[combo] picks = {json.dumps(picks, ensure_ascii=False)}", flush=True)
        stage_combo(ctx, picks)

    if args.stage == "crowd":
        picks = {}
        for tf in HTF:
            p = OUT / f"entry_{tf}.json"
            if p.exists():
                picks[tf] = json.loads(
                    p.read_text(encoding="utf-8"))["best_knobs"]
        stage_crowd(ctx, picks)

    print(f"\n[out] {OUT}  ({time.time()-t0:.0f}s total)", flush=True)


if __name__ == "__main__":
    main()

