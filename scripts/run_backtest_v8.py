"""v8 研究轮回测全管线:EDGE_RECLAIMED EDGE_RECLAIM(docs/v8-protocol.md,冻结于任何 v8
运行之前)—— campaign 引擎 × v8 路由(次 1h 市价/结构失效/带缘目标/room 门)。

用法:  python scripts/run_backtest_v8.py
配置:  config/v8.json(parent=config/canonical.json,不改动;执行/持仓/容量
       先验逐项照抄;差异仅 v8 协议声明的机制面)
产物:  reports/backtest_v8.json / backtest_v8.md
       reports/trades_v8_{seg}[_cost1_5,_cost2_0].csv
       reports/equity_v8_{seg}[_cost1_5,_cost2_0].csv
(canonical 冻结产物 reports/backtest_v6_turtle.* 与 trades_v6_turtle_* 一律只读,
对照桶直接读它们拼表,不重跑 —— 协议§2"A/B 位置语义对照"的操作化。)

语义地位(v8 研究轮,非规范链转正):
- test 段曝光纪律:v8-protocol §2/research-log #14 —— test 为第 11 次曝光,
  一次性评估;此后任何 v8 迭代只许前向。本脚本即该一次。test 合并战役成交
  笔数 < 60 ⇒ INSUFFICIENT_DATA,不产生任何"看起来转正"的叙述(协议§2)。
- 机制:EDGE_RECLAIMED 事件(events.py,enable_e8,pos_edge=0.20 冻结先验,regime 无关,
  confirmed band 语境 + 收复确认;伴随 BREAKOUT_FAILED 发射,BREAKOUT_ACCEPTED-FLIP_RETESTED 检测零改动 —— 本脚本
  仍以冻结产物对账硬约束守护:FLIP_RETESTED 1h 计数 == reports/backtest_v2.json;FLIP_RETESTED 多
  TF == reports/event_study_v3.json;BREAKOUT_ACCEPTED+STRUCTURE_BROKEN|short == reports/backtest.json
  go_events(v1 parity);任一漂移即 raise)。
- 路由 = strategy_v8(白名单独含 (EDGE_RECLAIMED,±1);协议§1 单一执行风格 = 次 1h 开盘
  市价,与 v6 桶可比;失效 = 事件 attrs invalid(刺穿极值∓k_bm·atr);F1
  k_is=0.8 闸;目标 = 对侧结构边缘 band H*/L*;room < r_min·risk 拒);
  出场 = §8 全套沿用 canonical(PositionManager(breakeven_on_reduce=True,
  time_stop=False) 接线,turtle 收割语义;§8.2 于带缘目标做 50% 减仓)。
- 战役/容量/账目:3 symbol × 5 TF = 15 战役,隔离互不知晓;共享 equity +
  组合容量 3%(最坏情景 FIFO 整笔跳过);fill_ts > decision_ts 恒等(Broker
  行内断言 + 模块级 assert_fill_after_decision);15m↔1h、1h↔TF compaction
  对账违即 raise;合约规格离线缓存强制(逐条 sha256)。
- 判别量(协议§2,写作在先):① EDGE_RECLAIMED 桶入场 pos 分布(事件 pos 构造过门 +
  成交 pos 应集中边缘带;构造校验)与胜率/平均R/净额按 (TF×方向);
  ② 同期 canonical STRUCTURE_BROKEN/FLIP_RETESTED 对照桶(读 trades_v6_turtle_*.csv,同段同成本);
  ③ dev 位置带敏感性 {0.15, 0.20, 0.25}(仅 dev,不择优);
  ④ 升级判定:test≥60 且 1.5x 净期望>0 且 2x PF>1。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))                    # 复用 v6 规范入口的纯函数
sys.path.insert(0, str(ROOT / "src"))

from run_backtest_v6 import (                                # noqa: E402
    COST_MULTS,
    SEGMENTS,
    _iso,
    _median,
    bucket_tables,
    cross_checks,
    load_contracts_cache,
)
from market_operator import aggregate as agg                # noqa: E402
from market_operator import data as mo_data                 # noqa: E402
from market_operator.backtest_v6 import (                   # noqa: E402
    alignment_issues_ci,
    compute_metrics,
    prepare_cognition,
)
from market_operator.broker import CONTRACTS_PATH           # noqa: E402
from market_operator.campaign import (                      # noqa: E402
    build_campaign_frame,
    run_campaigns,
)
from market_operator.research import (                      # noqa: E402
    block_bootstrap_ci,
    split_boundary,
)
from market_operator.strategy_v8 import (                   # noqa: E402
    ROUTE_EDGE_RECLAIMED,
    V8Params,
    V8Router,
)
from market_operator.structure import Params                # noqa: E402

CONFIG_V8 = ROOT / "config" / "v8.json"
REPORTS = ROOT / "reports"
SEMANTICS = "v8"
MIN_TRADES_TEST = 60                       # v8-protocol §2 升级线样本量阈(不变)
MECHANISMS = ("EDGE_RECLAIMED",)
CANON_EVENTS = ("STRUCTURE_BROKEN", "FLIP_RETESTED")                # 协议§2 对照桶(位置语义 A/B)

TRADE_COLUMNS = [
    "trade_id", "campaign", "symbol", "tf", "direction", "arm", "route",
    "event_id", "regime", "r_mult_planned",
    "open_decision_ts", "open_decision_iso", "open_ts", "open_iso", "entry",
    "entry_mode", "fill_wait_bars", "limit_price",
    "qty", "notional", "used_leverage", "invalid", "target_1", "risk_usdt",
    "closed", "close_decision_ts", "close_decision_iso", "exit_ts", "exit_iso",
    "exit_price", "exit_kind", "hold_hours", "gross_pnl", "fees",
    "fees_maker", "fees_taker", "funding",
    "slippage_cost", "net_pnl", "r_multiple", "mfe_r", "giveback_r",
    "pos_event", "pos_entry", "band_lo", "band_hi",
    "reason_open", "reason_reduce", "reason_exit",
]

E1E7_IDS = ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED", "STRUCTURE_BROKEN", "FLIP_RETESTED")


# ---------------------------------------------------------------------------
# 数据装配(一次装载/校验;认知缓存按 pos_edge 重放 —— 仅 EDGE_RECLAIMED 目录受 gate 影响)
# ---------------------------------------------------------------------------


def load_symbol_data(raw_cfg: dict, tf_list: list) -> tuple:
    """装载 + 全部对齐/compaction 对账(违即 raise);返回 (sym_data, report)。"""
    sym_data: dict = {}
    compaction_report: dict = {}
    for sym in raw_cfg["symbols"]:
        df_1h = mo_data.load(sym)
        df_15m = mo_data.load(sym, "15m")
        issues = mo_data.validate(df_15m, step_ms=900_000)
        if issues:
            raise RuntimeError(f"{sym} 15m validation: {issues[:5]}")
        align = mo_data.check_frame_alignment(df_15m, df_1h, step_fine_ms=900_000)
        if align:
            raise RuntimeError(f"{sym} 15m↔1h alignment: {align[:5]}")
        compact = agg.aggregate(df_15m, 1)
        exact_15m = all((compact[c] - df_1h[c]).abs().max() == 0.0
                        for c in ("open", "high", "low", "close"))
        if not exact_15m or not (compact["ts"] == df_1h["ts"]).all():
            raise RuntimeError(f"{sym} 15m compaction != 1h frame (OHLC/ts)")
        ci = alignment_issues_ci(
            [(int(r.ts),) for r in df_1h.itertuples(index=False)],
            [(int(r.ts),) for r in df_15m.itertuples(index=False)],
            float(raw_cfg["dev_frac"]))
        if ci:
            raise RuntimeError(f"{sym} boundary alignment: {ci}")
        compaction_report[sym] = {"15m_vs_1h_exact": bool(exact_15m)}
        per_tf: dict = {}
        for tf in tf_list:
            if tf == "1h":
                df_cog, df_exec = df_1h, df_15m
            else:
                df_cog = agg.load_tf(sym, tf)
                df_exec = df_1h
                step = agg.TF_HOURS[tf] * 3_600_000
                issues_tf = mo_data.validate(df_cog, step_ms=step)
                if issues_tf:
                    raise RuntimeError(f"{sym} {tf} validation: {issues_tf[:5]}")
                expected = agg.aggregate(df_1h, agg.TF_HOURS[tf])
                ok = all((expected[c] - df_cog[c]).abs().max() == 0.0
                         for c in ("open", "high", "low", "close"))
                if not ok or not (expected["ts"] == df_cog["ts"]).all():
                    raise RuntimeError(f"{sym} {tf} 盘上帧 != 1h 聚合重算(漂移)")
                compaction_report[sym][f"1h_vs_{tf}_exact"] = bool(ok)
            per_tf[tf] = (df_cog, df_exec)
        sym_data[sym] = per_tf
    return sym_data, compaction_report


def build_frames(sym_data: dict, params: Params, pos_edge: float) -> dict:
    """按 pos_edge 重放认知(enable_e7/enable_e8)并装配战役帧。"""
    frames: dict = {}
    for sym, per_tf in sym_data.items():
        for tf, (df_cog, df_exec) in per_tf.items():
            t0 = time.time()
            cog = prepare_cognition(df_cog, params, sym, enable_e7=True,
                                    enable_e8=True, e8_pos_edge=pos_edge)
            f = build_campaign_frame(cog, df_exec, tf)
            frames[f.key] = f
            n_e8 = sum(1 for evs in cog.events_by.values()
                       for e in evs if e.id == "EDGE_RECLAIMED")
            n_ev = sum(len(v) for v in cog.events_by.values())
            print(f"[frame] {sym}@{tf}: {len(df_cog)} 认知 bar, {n_ev} events "
                  f"(EDGE_RECLAIMED={n_e8}, pos_edge={pos_edge}) ({time.time() - t0:.1f}s)",
                  flush=True)
    return frames


# ---------------------------------------------------------------------------
# 逐笔增广:MFE_R / 回吐 R / EDGE_RECLAIMED 入场 pos(构造校验核心量)
# ---------------------------------------------------------------------------


def _e8_lookup(frames: dict) -> dict:
    """战役 → (direction, decision_ts) → [EDGE_RECLAIMED attrs](同 bar 多 zone episode 并存)。"""
    lut: dict = {}
    for key, f in frames.items():
        m: dict = {}
        for _i, evs in f.cog.events_by.items():
            for e in evs:
                if e.id == "EDGE_RECLAIMED":
                    m.setdefault((e.direction, int(e.ts)), []).append(e.attrs)
        lut[key] = m
    return lut


def augment_trades_v8(trades: list, frames: dict) -> list:
    lut = _e8_lookup(frames)
    out = []
    for t in trades:
        f = frames[t["campaign"]]
        ms_tf = f.tf_hours * 3_600_000
        label_of = {int(b[0]): i for i, b in enumerate(f.cog.bars)}
        e_i = label_of.get(t["open_ts"] - (t["open_ts"] % ms_tf))
        x_i = e_i if t["exit_ts"] is None else \
            label_of.get(t["exit_ts"] - (t["exit_ts"] % ms_tf))
        mfe_r = None
        if e_i is not None and x_i is not None and t["risk_usdt"] and \
                t["entry"] and t["invalid"]:
            d = t["direction"]
            risk_px = abs(t["entry"] - t["invalid"])
            highs = max(b[2] for b in f.cog.bars[e_i:x_i + 1])
            lows = min(b[3] for b in f.cog.bars[e_i:x_i + 1])
            mfe_px = (highs - t["entry"]) if d > 0 else (t["entry"] - lows)
            mfe_r = mfe_px / risk_px if risk_px > 0 else None
        # EDGE_RECLAIMED 入场 pos(构造校验):事件知时 band + 事件 pos(检测器写的门值,
        # 构造 ≤0.20/≥0.80)与成交 pos(实际成交价,可因滑点/隔夜微出带)。
        pos_event = pos_entry = band_lo = band_hi = None
        cands = lut.get(t["campaign"], {}).get(
            (t["direction"], int(t["open_decision_ts"])), [])
        if cands:
            a = min(cands, key=lambda x: abs(float(x["invalid"]) - t["invalid"]))
            band_lo, band_hi = float(a["band"][0]), float(a["band"][1])
            pos_event = float(a["pos"])
            if band_hi > band_lo:
                pos_entry = (t["entry"] - band_lo) / (band_hi - band_lo)
        out.append({**t, "arm": SEMANTICS, "mfe_r": mfe_r,
                    "giveback_r": (mfe_r - t["r_multiple"]
                                   if mfe_r is not None
                                   and t["r_multiple"] is not None else None),
                    "pos_event": pos_event, "pos_entry": pos_entry,
                    "band_lo": band_lo, "band_hi": band_hi})
    return out


def giveback_summary(trades: list) -> dict:
    gbs = [t["giveback_r"] for t in trades if t["closed"]
           and t["giveback_r"] is not None and t["in_segment_open"]]
    mfes = [t["mfe_r"] for t in trades if t["closed"]
            and t["mfe_r"] is not None and t["in_segment_open"]]
    return {"n": len(gbs), "giveback_median_r": _median(gbs),
            "mfe_median_r": _median(mfes)}


# ---------------------------------------------------------------------------
# ① 判别量:EDGE_RECLAIMED 入场 pos 构造校验(事件 pos ⊆ 门 + 成交 pos 分布)
# ---------------------------------------------------------------------------


def pos_construction(trades: list) -> dict:
    out: dict = {}
    for t in trades:
        if t["event_id"] != "EDGE_RECLAIMED" or not t["in_segment_open"]:
            continue
        side = "long" if t["direction"] > 0 else "short"
        g = out.setdefault(side, {"n": 0, "pos_event": [], "pos_entry": []})
        g["n"] += 1
        if t["pos_event"] is not None:
            g["pos_event"].append(t["pos_event"])
        if t["pos_entry"] is not None:
            g["pos_entry"].append(t["pos_entry"])
    res: dict = {}
    for side, g in sorted(out.items()):
        rec: dict = {"n": g["n"]}
        for k in ("pos_event", "pos_entry"):
            xs = sorted(g[k])
            if not xs:
                rec[k] = {"n": 0}
                continue
            n = len(xs)
            thr = 0.20 if side == "long" else 0.80
            in_edge = sum(1 for x in xs
                          if (x <= thr if side == "long" else x >= thr))
            rec[k] = {
                "n": n, "mean": sum(xs) / n, "median": _median(xs),
                "min": xs[0], "max": xs[-1],
                "in_event_band_edge_share": in_edge / n,
            }
        res[side] = rec
    return res


# ---------------------------------------------------------------------------
# ② canonical STRUCTURE_BROKEN/FLIP_RETESTED 对照桶(读 v6 冻结产物拼表,不重跑)
# ---------------------------------------------------------------------------


def _cost_suffix(mult: float) -> str:
    return "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"


def canonical_buckets(seg: str, mult: float) -> dict:
    """同期 canonical STRUCTURE_BROKEN/FLIP_RETESTED 机制桶(n/胜率/平均R/净额;与 bucket_tables 同口径,
    winrate/avg_r 以 closed 笔计;segment 原生 CSV,段口径与 v6 metrics 一致)。"""
    path = REPORTS / f"trades_v6_turtle_{seg}{_cost_suffix(mult)}.csv"
    if not path.exists():
        return {"error": f"missing canonical product {path.name}"}
    df = pd.read_csv(path)
    df = df[df["event_id"].isin(CANON_EVENTS)].copy()
    df["side"] = np.where(df["direction"] > 0, "long", "short")
    out: dict = {}
    for (ev, tf, side), g in df.groupby(["event_id", "tf", "side"], sort=True):
        closed = g[g["closed"].astype(str) == "True"]
        rs = pd.to_numeric(closed["r_multiple"], errors="coerce").dropna()
        n_closed = len(closed)
        wins = int((pd.to_numeric(closed["net_pnl"],
                                  errors="coerce") > 0).sum())
        out.setdefault(ev, {}).setdefault(str(tf), {})[side] = {
            "n_trades": int(len(g)),
            "winrate": (wins / n_closed) if n_closed else None,
            "avg_r": (float(rs.mean()) if len(rs) else None),
            "net_pnl": float(pd.to_numeric(g["net_pnl"],
                                           errors="coerce").sum())}
    return out


# ---------------------------------------------------------------------------
# EDGE_RECLAIMED 事件计数(认知缓存直数,段 = 每战役 TF 帧 split_boundary,不含路由门)
# ---------------------------------------------------------------------------


def e8_event_counts(frames: dict, dev_frac: float) -> dict:
    from collections import Counter
    cnt = {"dev": Counter(), "test": Counter()}
    for key, f in sorted(frames.items()):
        b = split_boundary(len(f.cog.bars), dev_frac)
        for i, evs in f.cog.events_by.items():
            seg = "dev" if 0 <= i < b else "test"
            for e in evs:
                if e.id == "EDGE_RECLAIMED":
                    cnt[seg][f"{f.tf_label}×"
                             f"{'long' if e.direction > 0 else 'short'}"] += 1
    return {seg: dict(sorted(c.items())) for seg, c in cnt.items()}


def e1_e7_parity_counts(frames: dict) -> dict:
    """BREAKOUT_ACCEPTED-FLIP_RETESTED 认知总数(enable_e8=True 下;应与 canonical 产物口径一致 ——
    交叉强证由 cross_checks 的冻结锚点对账承担,此处仅计数留档)。"""
    per: dict = {}
    for key, f in sorted(frames.items()):
        from collections import Counter
        c = Counter()
        for evs in f.cog.events_by.values():
            for e in evs:
                if e.id in E1E7_IDS:
                    c[e.id] += 1
        per[key] = dict(sorted(c.items()))
    return per


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def trades_frame(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": _iso(t["open_decision_ts"]),
                     "open_iso": _iso(t["open_ts"]),
                     "close_decision_iso": _iso(t["close_decision_ts"]),
                     "exit_iso": _iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def equity_frame(res) -> pd.DataFrame:
    return pd.DataFrame(
        [(ts, _iso(ts), eq, cash, upnl, fnd, n)
         for ts, eq, cash, upnl, fnd, n in res.equity_rows],
        columns=["ts", "iso", "equity", "cash", "unrealized",
                 "funding_accrued", "n_open"])


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(CONFIG_V8.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V8)
    dev_frac = float(raw_cfg["dev_frac"])
    tf_list = list(raw_cfg["tf_list"])
    if tf_list != ["1h", "2h", "4h", "12h", "1d"]:
        raise RuntimeError(f"config/v8.json tf_list 冻结为 "
                           f"['1h','2h','4h','12h','1d']: {tf_list}")
    if raw_cfg["entry_mode"] != "edge_reclaimed_market":
        raise RuntimeError("config/v8.json entry_mode must be e8_market "
                           "(v8 协议§1 单执行风格)")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V8Params(k_is=float(raw_cfg["k_is"]),
                       r_min=float(raw_cfg["r_min"]), whitelist=whitelist)
    cap_frac = float(raw_cfg["portfolio_cap_frac"])
    n_fill = int(raw_cfg["n_fill"])
    if int(raw_cfg["exec_tf_seconds"]) != 900:
        raise RuntimeError("exec_tf_seconds 冻结为 900(15m)")
    pos_edge = float(raw_cfg["pos_edge"])
    sens_edges = [float(x) for x in raw_cfg["pos_edge_sensitivity"]]
    equity0 = 300.0

    contracts, spec_src = load_contracts_cache(list(raw_cfg["symbols"]))
    print(f"[contracts] source={spec_src}", flush=True)

    sym_data, compaction_report = load_symbol_data(raw_cfg, tf_list)
    frames = build_frames(sym_data, params, pos_edge)
    xcheck = cross_checks(frames, float(raw_cfg["k_is"]))

    broker_kw = dict(dev_frac=dev_frac, equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     maker_fee=float(raw_cfg["maker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params, n_fill=n_fill,
                     portfolio_cap_frac=cap_frac)

    def run_one(seg: str, mult: float, fr) -> tuple:
        res = run_campaigns(fr, segment=seg, cost_multiplier=mult,
                            router_params=rp_base, router_factory=V8Router,
                            **broker_kw)
        res.trades = augment_trades_v8(res.trades, fr)
        return res, compute_metrics(res)

    # -- 主网格(full/dev/test × 1x/1.5x/2x;test 为第 11 次曝光,一次性) ---------
    results: dict = {}
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            t0 = time.time()
            res, m = run_one(seg, mult, frames)
            results[(seg, mult)] = (res, m)
            c = res.counters
            print(f"[bt] v8 {seg:4s} cost {mult}x: net={m['net_pnl']:+.2f} "
                  f"trades={m['n_trades']} fills={c['opens_filled']} "
                  f"cap_skips={c['portfolio_cap_skips']} "
                  f"({time.time() - t0:.1f}s)", flush=True)
    liq = sum(results[(s, mm)][0].counters["liquidations"]
              for s in SEGMENTS for mm in COST_MULTS)
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")

    # -- ③ dev 位置带敏感性(仅 dev,不择优;共享仓位/容量语义,仅 EDGE_RECLAIMED 门变化) ------
    sens_runs: dict = {}
    if sens_edges:
        for edge in sens_edges:
            fr = build_frames(sym_data, params, edge)
            for mult in COST_MULTS:
                t0 = time.time()
                res, m = run_one("dev", mult, fr)
                sens_runs[(edge, mult)] = (res, m)
                print(f"[sens] v8 dev pos_edge={edge} cost {mult}x: "
                      f"net={m['net_pnl']:+.2f} trades={m['n_trades']} "
                      f"({time.time() - t0:.1f}s)", flush=True)

    # -- 样本量与升级线测量(v8-protocol §2 同一冻结口径:test≥60/1.5x>0/2x PF>1) --
    m_test_1x = results[("test", 1.0)][1]
    exp_test_15x = results[("test", 1.5)][1]["expectancy_usdt"]
    pf_2x = results[("test", 2.0)][1]["profit_factor"]
    n_test = m_test_1x["n_trades"]
    insufficient = n_test < MIN_TRADES_TEST
    sample = {
        "n_trades_test_1x": n_test, "threshold": MIN_TRADES_TEST,
        "sample_size_verdict": ("SUFFICIENT(>=60)" if not insufficient
                                else "INSUFFICIENT_DATA"),
        "honesty_note": ("test<60 ⇒ INSUFFICIENT_DATA:不产生任何'看起来转正'"
                         "的叙述;合规补证手段 = 前向 paper 积累,不修回测曲线"),
    }
    upgrade = {
        "principal_unit": "合并战役(全部 symbol×TF 合并,与协议一致)",
        "criteria": {
            "n_trades_test_ge_60": not insufficient,
            "expectancy_test_1_5x_gt_0": bool(exp_test_15x is not None
                                              and exp_test_15x > 0.0),
            "pf_test_2x_gt_1": bool(pf_2x is not None and pf_2x > 1.0),
            "expectancy_test_usdt_1_5x": exp_test_15x,
            "pf_test_2x": pf_2x,
        },
        "verdict": ("PAPER_CANDIDATE" if (
            not insufficient
            and exp_test_15x is not None and exp_test_15x > 0.0
            and pf_2x is not None and pf_2x > 1.0) else "RESEARCH_ONLY"),
        "note": ("v8 唯一一次 test 评估(第 11 次曝光,v8-protocol §2/"
                 "research-log #14 预注册);此后任何 v8 迭代只许前向。"),
    }
    trades_test_1x = [t for t in results[("test", 1.0)][0].trades
                      if t["in_segment_open"]]
    if trades_test_1x:
        ci_lo, ci_hi = block_bootstrap_ci(
            np.asarray([t["net_pnl"] for t in trades_test_1x], dtype=np.float64),
            np.asarray([t["open_ts"] for t in trades_test_1x], dtype=np.int64))
    else:
        ci_lo = ci_hi = float("nan")
    sample["test_net_pnl_mean_ci95_1x"] = [ci_lo, ci_hi]

    # -- ① 判别量(pos 构造校验)+ ② 对照桶 + 分桶/容量/回吐 ------------------------
    pos_check = {f"{seg}@1x": pos_construction(results[(seg, 1.0)][0].trades)
                 for seg in ("dev", "test")}
    buckets: dict = {}
    canon: dict = {}
    givebacks: dict = {}
    cap_stats: dict = {}
    for (seg, mult), (res, m) in sorted(results.items()):
        tag = f"{SEMANTICS}|{seg}@{mult}x"
        buckets[tag] = bucket_tables(res.trades)
        givebacks[tag] = giveback_summary(res.trades)
        cap_stats[tag] = {
            "portfolio_cap_skips": res.counters.get("portfolio_cap_skips", 0),
            "cap_skips": res.cap_skips,
            "limits_armed": res.counters["limits_armed"],
            "limit_fills_entry": res.counters["limit_fills_entry"],
            "limit_expired": res.counters["limit_expired"],
            "opens_filled": res.counters["opens_filled"],
            "e1_priority_cancels": res.counters["e1_priority_cancels"],
            "min_size_skips": res.counters["min_size_skips"],
            "e8_counters": res.counters.get("campaigns"),
        }
        if seg in ("dev", "test"):
            canon[f"turtle|{seg}@{mult}x"] = canonical_buckets(seg, mult)

    sens_sens: dict = {}
    for (edge, mult), (res, m) in sorted(sens_runs.items()):
        sens_sens[f"pos_edge={edge}|dev@{mult}x"] = {
            "overview": {k: m[k] for k in (
                "net_pnl", "n_trades", "n_closed", "winrate", "profit_factor",
                "expectancy_usdt", "expectancy_r")},
            "buckets": bucket_tables(res.trades),
        }

    REPORTS.mkdir(exist_ok=True)
    for (seg, mult), (res, m) in sorted(results.items()):
        suffix = _cost_suffix(mult)
        trades_frame(res.trades).to_csv(
            REPORTS / f"trades_v8_{seg}{suffix}.csv", index=False)
        equity_frame(res).to_csv(
            REPORTS / f"equity_v8_{seg}{suffix}.csv", index=False)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "semantics": SEMANTICS,
        "protocol": {
            "doc": "docs/v8-protocol.md(EDGE_RECLAIMED EDGE_RECLAIM,冻结于任何 v8 运行之前)",
            "meta": raw_cfg["meta"],
            "mechanism": ("EDGE_RECLAIMED:confirmed band 语境 pos 门(底带 ≤0.20/顶带 ≥0.80,"
                          "regime 无关)+ 区域刺穿 ≤N_f=3 根收复确认;伴随 BREAKOUT_FAILED 发射,"
                          "BREAKOUT_ACCEPTED-FLIP_RETESTED 检测零改动(cross_checks 冻结锚点守护)"),
            "entry_geometry": ("次 1h 开盘市价(单执行风格,与 v6 桶可比);"
                               "失效=刺穿极值∓k_bm·atr;F1 k_is=0.8;目标=对侧"
                               "结构边缘 band H*/L*;room<r_min=1.5 拒;"
                               "出场=§8 全套沿用 canonical(turtle 接线)"),
            "test_eval_declaration": ("test 段曝光纪律:第 11 次曝光(research-log"
                                      " #14 预注册),v8 唯一一次评估;此后任何 v8"
                                      " 迭代只许前向。"),
            "comparison_policy": ("canonical 对照桶直接读 reports/trades_v6_turtle_"
                                  "*.csv(同段同成本,不重跑);STRUCTURE_BROKEN/FLIP_RETESTED 为协议§2 指定"
                                  "的位置语义 A/B 对照。"),
        },
        "config": {
            "config_file": "config/v8.json",
            "equity0": equity0, "dev_frac": dev_frac,
            "risk_frac": broker_kw["risk_frac"],
            "leverage_cap": broker_kw["leverage_cap"],
            "taker_fee": broker_kw["taker_fee"],
            "maker_fee": broker_kw["maker_fee"],
            "slippage_per_side": broker_kw["slippage_per_side"],
            "funding_per_8h_per_side": broker_kw["funding_per_8h"],
            "cost_mults": list(COST_MULTS),
            "whitelist": [f"{i}|{'long' if d > 0 else 'short'}"
                          for i, d in sorted(whitelist)],
            "tf_list": tf_list, "k_is": rp_base.k_is, "r_min": rp_base.r_min,
            "pos_edge": pos_edge, "pos_edge_sensitivity": sens_edges,
            "portfolio_cap_frac": cap_frac, "n_fill": n_fill,
            "exec_tf_seconds": 900,
        },
        "contracts": {"source": spec_src},
        "compaction_report": compaction_report,
        "cross_checks": xcheck,
        "e8_event_counts": e8_event_counts(frames, dev_frac),
        "e1_e7_counts_with_e8_enabled": e1_e7_parity_counts(frames),
        "pos_construction_check": pos_check,               # ① 判别量
        "canonical_comparison": canon,                     # ② 对照桶(读产物)
        "dev_pos_sensitivity": {"edges": [pos_edge] + sens_edges,
                                "dev_only": True,
                                "runs": sens_sens},        # ③ dev 敏感性
        "sample_size": sample,
        "upgrade_v8": upgrade,                             # ④ 升级判定
        "bucket_tables": buckets,
        "giveback": givebacks,
        "portfolio_capacity": cap_stats,
        "runs": {},
    }
    for (seg, mult), (res, m) in sorted(results.items()):
        payload["runs"][f"{SEMANTICS}|{seg}@{mult}x"] = {"metrics": m,
                                                         "trades": res.trades}

    (REPORTS / "backtest_v8.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v8.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/backtest_v8.json/.md, trades_v8/equity_v8 CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    if x is None or (isinstance(x, float) and x != x):
        return dash
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def _bucket_lines(bt: dict, event_ids, tfs=("1h", "2h", "4h", "12h", "1d")):
    lines = []
    for mech in event_ids:
        for tf in tfs:
            for side in ("short", "long"):
                cell = bt.get(mech, {}).get(tf, {}).get(side)
                if cell is None:
                    continue
                lines.append(f"| {mech} | {tf} | {side} | {cell['n_trades']} "
                             f"| {_f(cell['winrate'], 3)} "
                             f"| {_f(cell['avg_r'], 3)} "
                             f"| {_f(cell['net_pnl'])} |")
    return lines


def render_md(payload: dict) -> str:
    runs = payload["runs"]
    cfg = payload["config"]

    def m(tag):
        return runs[tag]["metrics"]

    insufficient = payload["sample_size"]["sample_size_verdict"] \
        == "INSUFFICIENT_DATA"

    lines = [
        "# v8 回测报告(EDGE_RECLAIMED EDGE_RECLAIM 边缘收复;协议 docs/v8-protocol.md)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明",
        "",
        "- 机制:EDGE_RECLAIMED 摆动带边缘收复(confirmed band `B=[L*,H*]`,多 pos ≤ "
        f"{cfg['pos_edge']}/空 pos ≥ {1.0 - cfg['pos_edge']:.2f},regime 无关;"
        "区域刺穿 + ≤N_f=3 根收复确认;伴随 BREAKOUT_FAILED 发射,A3 episode 去重继承;"
        "BREAKOUT_ACCEPTED-FLIP_RETESTED 检测零改动)。",
        "- 入场几何(协议§1):次 1h 开盘市价(单执行风格,与 v6 桶可比);失效 = "
        "刺穿极值 ∓ k_bm·atr(结构锚);F1 k_is=0.8 闸;目标 = 对侧结构边缘 "
        f"band H*/L*;room < r_min={cfg['r_min']}·risk 拒;出场 = §8 全套沿用 "
        "canonical(turtle 收割接线,breakeven_on_reduce=True, time_stop=False)。",
        "- 战役:3 symbol × TF ∈ {1h,2h,4h,12h,1d} = 15 战役,隔离互不知晓;"
        f"共享权益 + 组合容量 {cfg['portfolio_cap_frac']}(最坏情景 FIFO 跳过)。",
        f"- **{payload['protocol']['test_eval_declaration']}**",
        "- 样本量(协议§2 口径,如实测量):test 合并战役成交 < "
        f"{payload['sample_size']['threshold']} ⇒ "
        f"**{payload['sample_size']['sample_size_verdict']}**"
        + ("——本报告不给出任何转正叙述。" if insufficient else "。"),
        "- 认知对账(全部硬约束一致 ⇒ EDGE_RECLAIMED 检测未扰动 BREAKOUT_ACCEPTED-FLIP_RETESTED):FLIP_RETESTED 1h 计数 == "
        "v2 冻结产物("
        + "; ".join(f"{s}={v['passed_f1']}/{v['fired']}"
                    for s, v in payload["cross_checks"]["e7_counts_1h"].items())
        + f");FLIP_RETESTED 多TF计数 == event_study_v3 冻结产物;BREAKOUT_ACCEPTED+STRUCTURE_BROKEN|short="
        f"{payload['cross_checks']['breakout_structure_short_vs_v1']['v6']} == v1 go_events="
        f"{payload['cross_checks']['breakout_structure_short_vs_v1']['v1']}(v1 parity,未破)。",
        f"- 合约规格:{payload['contracts']['source']}(离线强制)。",
        "",]

    lines += ["## 1. 总览(段 × 成本;权益 300 USDT 起)", "",
              "| 段 | 成本 | 净盈亏 | 收益率 | 最大回撤% | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            g = m(f"{SEMANTICS}|{seg}@{mult}x")
            lines.append(
                f"| {seg} | {mult}x | {_f(g['net_pnl'])} "
                f"| {_f(100 * g['return_pct'])}% "
                f"| {_f(100 * g['max_dd_pct'])}% "
                f"| {g['n_trades']} | {_f(g['winrate'], 3)} "
                f"| {_f(g['profit_factor'])} | {_f(g['expectancy_usdt'], 4)} "
                f"| {_f(g['expectancy_r'], 4)} |")

    # -- ① 判别量:pos 构造校验 ---------------------------------------------------
    lines += ["", "## 2. 判别量①:EDGE_RECLAIMED 入场 pos 构造校验(协议§2,写作在先)", "",
              "事件 pos(检测门值,构造 ⊆[0,0.20]/[0.80,1])与成交 pos(实际成交价"
              "落带内位置,可因滑点/隔夜微出带;选言:c 为无效段记 0 笔)。", ""]
    for seg in ("dev", "test"):
        pc = payload["pos_construction_check"].get(f"{seg}@1x", {})
        lines += [f"### {seg}@1x", "",
                  "| 方向 | n | 源 | mean | median | min | max | 边缘带占比 |",
                  "|---|---|---|---|---|---|---|---|"]
        for side in ("long", "short"):
            rec = pc.get(side)
            if rec is None:
                lines.append(f"| {side} | 0 | — | — | — | — | — | — |")
                continue
            for src in ("pos_event", "pos_entry"):
                s = rec[src]
                if not s.get("n"):
                    lines.append(f"| {side} | {rec['n']} | {src} | — | — "
                                 f"| — | — | — |")
                    continue
                lines.append(
                    f"| {side} | {rec['n']} | {src} | {_f(s['mean'], 3)} "
                    f"| {_f(s['median'], 3)} | {_f(s['min'], 3)} "
                    f"| {_f(s['max'], 3)} | {_f(100 * s['in_event_band_edge_share'], 1)}% |")
        lines.append("")

    lines += ["### EDGE_RECLAIMED 机制桶(v8,TF × 方向;笔数/胜率/平均R/净额)", ""]
    for seg, mult in (("dev", 1.0), ("test", 1.0)):
        tag = f"{SEMANTICS}|{seg}@{mult}x"
        lines += [f"#### {seg}@{mult}x", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        rows = _bucket_lines(payload["bucket_tables"][tag], MECHANISMS)
        lines += rows if rows else ["| — | — | — | 0 | — | — | — |"]
        lines.append("")

    ec = payload["e8_event_counts"]
    lines += ["### EDGE_RECLAIMED 事件计数(认知直数,路由门前;dev/test × 方向)", "",
              "| TF×方向 | dev | test |", "|---|---|---|"]
    keys = sorted(set(ec.get("dev", {})) | set(ec.get("test", {})))
    for k in keys:
        lines.append(f"| {k} | {ec.get('dev', {}).get(k, 0)} "
                     f"| {ec.get('test', {}).get(k, 0)} |")

    # -- ② canonical 对照 ---------------------------------------------------------
    lines += ["", "## 3. 判别量②:canonical STRUCTURE_BROKEN/FLIP_RETESTED 对照桶(读 trades_v6_turtle_* "
              "拼表,同段同成本,不重跑)", "",
              "协议§2 判别(写作在先):位置假说成立 ⇒ EDGE_RECLAIMED 桶胜率显著高于同尺度 "
              "STRUCTURE_BROKEN/FLIP_RETESTED 桶;此处只把两表并置如实报告。", ""]
    for seg in ("dev", "test"):
        tag = f"turtle|{seg}@1.0x"
        lines += [f"### canonical {seg}@1.0x(STRUCTURE_BROKEN/FLIP_RETESTED)", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        cb = payload["canonical_comparison"].get(tag, {})
        rows = _bucket_lines(cb, CANON_EVENTS)
        lines += rows if rows else ["| — | — | — | 0 | — | — | — |"]
        lines.append("")

    # -- ③ dev 敏感性 -------------------------------------------------------------
    sens = payload["dev_pos_sensitivity"]
    lines += ["", "## 4. 判别量③:dev 位置带敏感性(仅 dev,不择优,不上 test)", ""]
    for (seg, mult) in (("dev", 1.0), ("dev", 1.5), ("dev", 2.0)):
        del seg
        lines += [f"### dev@{mult}x", "",
                  "| pos_edge | 笔数 | 胜率 | PF | 期望USDT | 期望R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        row20 = payload["runs"][f"{SEMANTICS}|dev@{mult}x"]["metrics"]
        lines.append(f"| 0.20 | {row20['n_trades']} | {_f(row20['winrate'], 3)} "
                     f"| {_f(row20['profit_factor'])} "
                     f"| {_f(row20['expectancy_usdt'], 4)} "
                     f"| {_f(row20['expectancy_r'], 4)} | {_f(row20['net_pnl'])} |")
        for edge in sens["edges"][1:]:
            r = sens["runs"].get(f"pos_edge={edge}|dev@{mult}x")
            if r is None:
                continue
            g = r["overview"]
            lines.append(f"| {edge} | {g['n_trades']} | {_f(g['winrate'], 3)} "
                         f"| {_f(g['profit_factor'])} "
                         f"| {_f(g['expectancy_usdt'], 4)} "
                         f"| {_f(g['expectancy_r'], 4)} | {_f(g['net_pnl'])} |")
        lines.append("")

    # -- ④ 升级判定 ---------------------------------------------------------------
    up = payload["upgrade_v8"]
    ss = payload["sample_size"]
    lines += ["", "## 5. 判别量④:样本量与升级线测量(协议§2 同一冻结口径)", "",
              f"- test 合并战役成交笔数(v8,1x,OPEN 决策 ∈ test):**"
              f"{ss['n_trades_test_1x']}**(阈值 ≥ {ss['threshold']})⇒ "
              f"**{ss['sample_size_verdict']}**。",
              f"- test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,2000 次,"
              f"固定种子):{_f(ss['test_net_pnl_mean_ci95_1x'][0], 4)} ~ "
              f"{_f(ss['test_net_pnl_mean_ci95_1x'][1], 4)} USDT —— 如实报告。",
              f"- {up['note']}", "",
              "| 判据 | 值 | 通过 |", "|---|---|---|",
              f"| test 成交 ≥ 60(合并战役) | {up['criteria']['n_trades_test_ge_60']}"
              f" (n={ss['n_trades_test_1x']}) "
              f"| {'✓' if up['criteria']['n_trades_test_ge_60'] else '✗'} |",
              f"| test 净期望 > 0(1.5x) | "
              f"{_f(up['criteria']['expectancy_test_usdt_1_5x'], 4)} "
              f"| {'✓' if up['criteria']['expectancy_test_1_5x_gt_0'] else '✗'} |",
              f"| test@2x PF > 1 | {_f(up['criteria']['pf_test_2x'])} "
              f"| {'✓' if up['criteria']['pf_test_2x_gt_1'] else '✗'} |",
              f"| **测量结论(同一口径)** | — | **{up['verdict']}** |"]
    if insufficient:
        lines += ["",
                  "> INSUFFICIENT_DATA 处置(v8-protocol §2):不产生任何『看起来转"
                  "正』的叙述;样本量按格如实报告;此后 v8 迭代只许前向 paper。"]

    lines += ["", "## 6. 已知近似与如实声明", "",
              "① EDGE_RECLAIMED 事件伴随 BREAKOUT_FAILED 发射(A3 episode 去重继承:BREAKOUT_FAILED 成立的 episode 至多"
              "一枚 EDGE_RECLAIMED;多区并存 = 逐 zone episode 独立判定,同 BREAKOUT_FAILED 语义);② pos "
              "门以收复 bar 收盘与彼时 confirmed band 度量(因果,含同 bar 新确认"
              " pivot 对 band 的更新);③ 失效 = 刺穿行程极值(episode ex ∪ 收复"
              " bar 极值)∓ k_bm·atr —— 锚在被扫流动性最深价外侧;④ 组合容量按决"
              "策时刻参考价估算(不取整,略高估=保守),成交时刻 PR#9 broker 口径"
              "第二层钳制;⑤ gap 市价单实际成交价越过结构失效时 Broker 记 "
              "BAD_INVALID 跳过(如实,无这笔交易);⑥ 敏感性仅 dev 重放认知+回放,"
              "{0.15, 0.25} 门变化只影响 EDGE_RECLAIMED 目录;⑦ 对照桶读 v6 冻结产物(segm"
              "ent 原生 CSV,段口径与 v6 metrics 一致),不重跑 canonical。", "",
              "机器可读全量:`reports/backtest_v8.json`;逐笔:"
              "`reports/trades_v8_*.csv`(增列 pos_event/pos_entry/band_lo/band_hi);"
              "逐执行细 bar 权益:`reports/equity_v8_*.csv`。",
              "canonical 对照池:`reports/trades_v6_turtle_*.csv`(只读)。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
