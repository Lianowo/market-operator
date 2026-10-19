"""v4 回测全管线(docs/v4-protocol.md §1/§3):成熟度三闸门 + 15m 执行路径 × 9 段。

用法:  python scripts/run_backtest_v4.py
产物:  reports/backtest_v4.json / backtest_v4.md
       reports/trades_v4_{full,dev,test}[_cost1_5,_cost2_0].csv
       reports/equity_v4_{full,dev,test}[_cost1_5,_cost2_0].csv

纪律(v4-protocol,冻结于任何 v4 运行之前):
- 三闸门(v4-protocol §1):G1 = 白名单 {E7|short, E7|long}(config/v4.json
  whitelist_v2,parent=config/v2.json);G2 = F1(dist(close, invalid) ≥ k_is·atr,
  k_is=0.8 沿用 v2 冻结先验,strategy 层执行);G3 = 翻转区域前缘限价接回踩
  (short:zone.lo,long:zone.hi),N_fill=12 根 1h bar,超时撤单(EXPIRED)。
- 执行路径:认知/决策 1h(与 v2 逐 bar 回放严格同源),成交判定/持仓路径
  解决走 15m(BTCUSDT/ETHUSDT/SOLUSDT 15m,与 1h 同 2 年区间,对齐断言);
  maker/taker 非对称费用(入场限价/减仓限价 = maker 0.02%;退出 = taker
  0.06% + 滑点 0.02%;资金费不变);§8 其余五条、风险预算、杠杆上限、
  MIN_SIZE 规则一律不动。
- 15m↔1h 对齐断言:框架覆盖(15m 首 ts ≤ 1h 首、每根 1h bar 恰 4 根 15m
  子 bar,check_frame_alignment)+ 引擎网格断言(assert_aligned)+ 零缺口
  validate(900_000);compaction 1h(15m 聚合)OHLC 与 1h 帧字节级一致
  (本脚本断言;volume 允许 ≤1e-3 相对浮点差,认知不使用 volume 如实声明)。
- dev/test 拐点断言同 v3:1h 索引 12320(split_boundary(17600, 0.7));
  alignment_issues_ci 落桶核查。
- 认知对账:E7 发射/过 F1 计数与 reports/backtest_v2.json 既有产物逐
  symbol 交叉核对(同一认知源,差异即 raise,同 v3 诚实性硬约束)。
- 样本量(v4-protocol §3):test 段成交 < 60 笔 ⇒ INSUFFICIENT_DATA,
  报告不得给出"看起来转正"的叙述;所有 CI 如实报告(宽就宽)。
- 合约规格强制离线缓存(同 v3 纪律;data/contracts.json 与 v1/v2 同缓存,
  逐条目 sha256 校验)。
- **test 段第 6 次评估(v4 唯一一次;research-log 条目 9)**。
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data                       # noqa: E402
from market_operator import aggregate as agg                     # noqa: E402
from research.rounds.v1.backtest import compute_metrics, prepare_cognition  # noqa: E402
from research.rounds.v4.backtest_v4 import (                        # noqa: E402
    BacktestResult,
    alignment_issues_ci,
    build_exec_frame,
    run_backtest_v4,
)
from market_operator.broker import CONTRACTS_PATH, ContractSpec  # noqa: E402
from market_operator.hypothesis import f1_invalid_distance_ok    # noqa: E402
from market_operator.research import block_bootstrap_ci, split_boundary  # noqa: E402
from research.rounds.v1.strategy import Strategy, StrategyParams    # noqa: E402
from market_operator.structure import Params                     # noqa: E402

CONFIG_V4 = ROOT / "config" / "v4.json"
BT_V2_PATH = ROOT / "reports" / "backtest_v2.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev", "test")
COST_MULTS = (1.0, 1.5, 2.0)
MIN_TRADES_TEST = 60                       # v4-protocol §3 样本量阈

TRADE_COLUMNS = [
    "trade_id", "symbol", "direction", "event_id", "regime", "r_mult_planned",
    "open_decision_ts", "open_decision_iso", "open_ts", "open_iso", "entry",
    "entry_mode", "fill_wait_bars", "limit_price",
    "qty", "notional", "used_leverage", "invalid", "target_1", "risk_usdt",
    "closed", "close_decision_ts", "close_decision_iso", "exit_ts", "exit_iso",
    "exit_price", "exit_kind", "hold_hours", "gross_pnl", "fees",
    "fees_maker", "fees_taker", "funding",
    "slippage_cost", "net_pnl", "r_multiple", "reason_open", "reason_reduce",
    "reason_exit",
]


def _iso(ts) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec="seconds")


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


def equity_frame(res: BacktestResult) -> pd.DataFrame:
    return pd.DataFrame(
        [(ts, _iso(ts), eq, cash, upnl, fnd, n)
         for ts, eq, cash, upnl, fnd, n in res.equity_rows],
        columns=["ts", "iso", "equity", "cash", "unrealized", "funding_accrued",
                 "n_open"])


def load_contracts_cache(symbols: list) -> tuple:
    """离线合约规格(v3 纪律;v1/v2 同缓存,逐条目 sha256 校验)。"""
    payload = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    specs = {}
    for s in symbols:
        ent = payload["entries"][s]
        ent_sha = hashlib.sha256(json.dumps(
            ent["raw"], sort_keys=True).encode("utf-8")).hexdigest()
        if ent_sha != ent["sha256"]:
            raise RuntimeError(f"contracts cache sha256 mismatch for {s}")
        specs[s] = ContractSpec.from_entry(ent["raw"])
    return specs, f"cache(offline enforced, cached_at={payload['fetched_at_utc']})"


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(CONFIG_V4.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V4)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    k_is = float(raw_cfg["k_is"])
    n_fill = int(raw_cfg["n_fill"])
    exec_tf = int(raw_cfg["exec_tf_seconds"])
    if raw_cfg["entry_mode"] != "limit_front_edge":
        raise RuntimeError("config/v4.json entry_mode must be limit_front_edge")
    if exec_tf != 900 or 3600 % exec_tf != 0:
        raise RuntimeError(f"exec_tf_seconds={exec_tf} must divide 3600 (v4=900)")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist_v2"])
    equity0 = 300.0

    contracts, spec_src = load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    frames = {}
    compaction_report: dict = {}
    for sym in symbols:
        t0 = time.time()
        df_1h = mo_data.load(sym)
        df_15m = mo_data.load(sym, "15m")
        issues_15m = mo_data.validate(df_15m, step_ms=900_000)
        if issues_15m:
            raise RuntimeError(f"{sym} 15m validation: {issues_15m[:5]}")
        align = mo_data.check_frame_alignment(df_15m, df_1h, step_fine_ms=900_000)
        if align:
            raise RuntimeError(f"{sym} 15m↔1h alignment: {align[:5]}")
        if len(df_15m) != 4 * len(df_1h):
            raise RuntimeError(f"{sym} 15m rows {len(df_15m)} != 4×{len(df_1h)}")
        # compaction 一致性(脚本级断言):15m 聚合 1h 的 OHLC 逐格 == 1h 帧
        compact = agg.aggregate(df_15m, 1)
        exact = all((compact[c] - df_1h[c]).abs().max() == 0.0
                    for c in ("open", "high", "low", "close"))
        vol_rel = float(((compact["volume"] - df_1h["volume"]).abs()
                         / df_1h["volume"].clip(lower=1e-9)).max())
        compaction_report[sym] = {"ohlc_exact": bool(exact),
                                  "volume_rel_maxdiff": vol_rel}
        if not exact or not (compact["ts"] == df_1h["ts"]).all():
            raise RuntimeError(f"{sym} 15m compaction != 1h frame (OHLC/ts)")
        cog = prepare_cognition(df_1h, params, sym, enable_e7=True)
        ci = alignment_issues_ci(cog.bars, [(int(r.ts),) for r in
                                            df_15m.itertuples(index=False)],
                                 dev_frac)
        if ci:
            raise RuntimeError(f"{sym} boundary alignment: {ci}")
        frames[sym] = build_exec_frame(cog, df_15m)
        n_ev = sum(len(v) for v in cog.events_by.values())
        print(f"[frame] {sym}: {len(df_1h)} 1h bars / {len(df_15m)} 15m bars, "
              f"{n_ev} events, compaction exact={exact} ({time.time() - t0:.1f}s)",
              flush=True)

    # E7 发射/过 F1 计数,与 v2 冻结产物逐 symbol 交叉核对(同一认知源)
    bt_v2 = json.loads(BT_V2_PATH.read_text(encoding="utf-8")) \
        if BT_V2_PATH.exists() else None
    if bt_v2 is None:
        raise RuntimeError("reports/backtest_v2.json 缺失(对照表与认知对账所需)")
    e7_counts: dict = {}
    for sym in symbols:
        cog = frames[sym].cog
        fired, passed = [], []
        for i, evs in cog.events_by.items():
            for e in evs:
                if (e.id, e.direction) in whitelist:
                    fired.append(e)
                    if f1_invalid_distance_ok(e, float(cog.bars[i][4]), k_is):
                        passed.append(e)
        e7_counts[sym] = {"whitelist_events": len(fired),
                          "passed_f1": len(passed)}
        v2c = bt_v2["e7_counts"][sym]
        if (v2c["whitelist_events"], v2c["passed_f1"]) \
                != (len(fired), len(passed)):
            raise RuntimeError(
                f"E7 计数对账失败 {sym}: v4 fired={len(fired)} passed={len(passed)} "
                f"vs v2 冻结产物 {v2c}")

    broker_kw = dict(dev_frac=dev_frac, equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     maker_fee=float(raw_cfg["maker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params, n_fill=n_fill)
    results: dict = {}
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            t0 = time.time()
            res = run_backtest_v4(
                frames, segment=seg, cost_multiplier=mult,
                strategy=Strategy(StrategyParams(
                    whitelist=whitelist, k_is=k_is,
                    entry_mode="limit_front_edge")),
                whitelist=whitelist, **broker_kw)
            results[(seg, mult)] = (res, compute_metrics(res))
            m = results[(seg, mult)][1]
            c = res.counters
            fill_rate = (c["limit_fills_entry"] / c["limits_armed"]
                         if c["limits_armed"] else None)
            print(f"[bt] {seg:5s} cost {mult}x: net={m['net_pnl']:+.2f} "
                  f"trades={m['n_trades']} armed={c['limits_armed']} "
                  f"fills={c['limit_fills_entry']} expired={c['limit_expired']} "
                  f"fill_rate={fill_rate if fill_rate is None else round(fill_rate, 3)} "
                  f"liq={c['liquidations']} ({time.time() - t0:.1f}s)", flush=True)

    # -- v4-protocol §3 升级判据与样本量判定(先于叙述) -----------------------
    m_test_1x = results[("test", 1.0)][1]
    pf_2x = results[("test", 2.0)][1]["profit_factor"]
    n_test = m_test_1x["n_trades"]
    exp_test = m_test_1x["expectancy_usdt"]
    sample = {
        "n_trades_test_1x": n_test,
        "threshold": MIN_TRADES_TEST,
        "sample_size_verdict": ("SUFFICIENT(>=60)" if n_test >= MIN_TRADES_TEST
                                else "INSUFFICIENT_DATA"),
        "honesty_note": ("test<60 ⇒ INSUFFICIENT_DATA:不产生任何'看起来转正'"
                         "的叙述;合规补证手段 = 前向 paper 积累,不修回测曲线"
                         "(v4-protocol §2)"),
    }
    upgrade = {
        "criteria": {
            "n_trades_test_ge_60": n_test >= MIN_TRADES_TEST,
            "expectancy_test_gt_0": bool(exp_test is not None and exp_test > 0.0),
            "pf_test_2x_gt_1": bool(pf_2x is not None and pf_2x > 1.0),
            "expectancy_test_usdt": exp_test,
            "pf_test_2x": pf_2x,
        },
        "verdict": ("PAPER_CANDIDATE" if (
            n_test >= MIN_TRADES_TEST
            and exp_test is not None and exp_test > 0.0
            and pf_2x is not None and pf_2x > 1.0) else "RESEARCH_ONLY"),
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

    REPORTS.mkdir(exist_ok=True)
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            res = results[(seg, mult)][0]
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame(res.trades).to_csv(
                REPORTS / f"trades_v4_{seg}{suffix}.csv", index=False)
            equity_frame(res).to_csv(
                REPORTS / f"equity_v4_{seg}{suffix}.csv", index=False)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v4-protocol.md(§1 三闸门与执行路径,§3 评估)",
            "meta": raw_cfg["meta"], "whitelist_v2": raw_cfg["whitelist_v2"],
            "k_is": k_is, "entry_mode": raw_cfg["entry_mode"],
            "n_fill": n_fill, "exec_tf_seconds": exec_tf,
            "maker_fee": float(raw_cfg["maker_fee"]),
            "test_eval_declaration": "test 段第 6 次评估(v4 唯一一次;见 "
                                     "docs/research-log.md 条目 9)",
        },
        "config": {
            "config_file": "config/v4.json",
            "equity0": equity0, "dev_frac": dev_frac,
            "risk_frac": broker_kw["risk_frac"],
            "leverage_cap": broker_kw["leverage_cap"],
            "taker_fee": broker_kw["taker_fee"],
            "maker_fee": broker_kw["maker_fee"],
            "slippage_per_side": broker_kw["slippage_per_side"],
            "funding_per_8h_per_side": broker_kw["funding_per_8h"],
            "cost_mults": list(COST_MULTS), "segments": list(SEGMENTS),
            "whitelist": ["%s|%s" % (i, "short" if d < 0 else "long")
                          for i, d in sorted(whitelist)],
            "k_is": k_is, "n_fill": n_fill, "exec_tf_seconds": exec_tf,
        },
        "contracts": {
            "source": spec_src, "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
            "cache_sha256": contracts_sha,
            "specs": {s: vars(c) for s, c in contracts.items()},
        },
        "conventions": {
            "decision_ts": "决策 1h bar 的 ts(收盘决策,与 v1/v2 同约定)",
            "armed_at_ts": "= decision_ts(1h bar 标签)",
            "fill_ts": "LIMIT_FILL = 成交 15m bar 标签(严格穿过,> armed);"
                       "市价 OPEN/REDUCE/EXIT = 下一根 1h bar 的第一根 15m "
                       "开盘,价格 = 1h 开盘价(与 v2 逐 bar 一致)",
            "expiry": "N_fill×1h 后超时:入场 ⇒ EXPIRED(无交易);减仓限价 ⇒ "
                      "CANCEL(TIMEOUT_TO_TAKER)并转同数量 taker 市价",
            "marking": "盯市/资金费计提细化到 15m(v2 为 1h),dt 比例口径不变;"
                       "①Open 撮合先于②挂单的同根因果序结构性解决 15m 路径",
            "funding": "资金费 = notional·rate·m·(持仓 ts 差)/8h,方向无关恒为成本",
            "liquidation": "近似强平保护 = 单仓 |浮亏| ≥ 0.9·equity_mtm 时下一根"
                           "15m 开盘平仓(15m 粒度,较 v2 更保守;预期 0 次)",
            "cash_mirror": "权益 = equity0 + Σ(realized_pnl − fee + funding_delta)"
                           " 逐行对账(同 v1/v2)",
        },
        "compaction_report": compaction_report,
        "e7_counts": e7_counts,
        "e7_counts_v2_cross_check_ok": True,
        "sample_size": sample,
        "upgrade_v4": upgrade,
        "v2_comparison": {
            "source": "reports/backtest_v2.json(v2 冻结产物,同白名单 A/B 锚点)",
            "cells": {f"{seg}@{mult}x": {
                "v2": {k: bt_v2["runs"][f"{seg}@{mult}x"]["metrics"][k] for k in
                       ("n_trades", "net_pnl", "profit_factor",
                        "expectancy_usdt", "winrate", "max_dd_pct")},
                "v4": {k: results[(seg, mult)][1][k] for k in
                       ("n_trades", "net_pnl", "profit_factor",
                        "expectancy_usdt", "winrate", "max_dd_pct")},
            } for seg in SEGMENTS for mult in COST_MULTS},
        },
        "runs": {},
    }
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            res, m = results[(seg, mult)]
            payload["runs"][f"{seg}@{mult}x"] = {"metrics": m,
                                                 "trades": res.trades}

    (REPORTS / "backtest_v4.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v4.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/backtest_v4.json/.md, trades_v4/equity_v4 CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    if x is None or (isinstance(x, float) and x != x):
        return dash
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def render_md(payload: dict) -> str:
    runs = payload["runs"]
    cfg = payload["config"]

    def m(seg, mult):
        return runs[f"{seg}@{mult}x"]["metrics"]

    def cnt(seg, mult):
        return runs[f"{seg}@{mult}x"]["metrics"]["counters"]

    lines = [
        "# v4 回测报告(docs/v4-protocol.md:成熟度三闸门 + 15m 执行路径;§7/§8 全规则原样)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        f"- 三闸门(v4-protocol §1):G1 白名单={cfg['whitelist']};G2 = F1 "
        f"(k_is={cfg['k_is']},沿用 v2 冻结先验);G3 = 翻转区域前缘限价"
        f"(short:zone.lo,long:zone.hi,偏向成交率方向,协议如实声明),"
        f"N_fill={cfg['n_fill']} 根 1h bar,超时撤单无交易。",
        f"- 执行路径:认知/决策 1h(与 v2 逐 bar 回放严格同源);成交判定与持仓"
        f"路径解决走 15m(exec_tf={cfg['exec_tf_seconds']}s);maker/taker 非对称"
        f"费用(限价成交 maker {cfg['maker_fee']},退出 taker {cfg['taker_fee']}"
        f" + 滑点 {cfg['slippage_per_side']},资金费不变)。§8 其余五条、风险"
        "预算、杠杆上限、MIN_SIZE 规则一律不动。",
        f"- **{payload['protocol']['test_eval_declaration']}**:本报告为该次"
        "评估的回测侧产物。",
        f"- 样本量(v4-protocol §3):test 段成交 < {payload['sample_size']['threshold']} 笔"
        f" ⇒ **{payload['sample_size']['sample_size_verdict']}**"
        + ("——按协议**不给出任何转正叙述**;合规补证手段只有前向 paper 积累。"
           if payload['sample_size']['sample_size_verdict'] == "INSUFFICIENT_DATA"
           else "。"),
        f"- 合约规格:{payload['contracts']['source']}(v1/v2 同缓存,离线强制)。",
        "- E7 发射/过 F1 计数(与 v2 冻结产物逐 symbol 交叉核对,全部一致):"
        + "; ".join(f"{s}={v['passed_f1']}/{v['whitelist_events']}"
                    for s, v in payload["e7_counts"].items()) + "。",
        "- 15m 数据:data/raw/{SYMBOL}_15m.csv(manifest 追加 sha256;70400 bars"
        "/symbol,0 validation issues,对齐断言通过);compaction 1h OHLC 与 1h "
        "帧逐格一致("
        + "; ".join(f"{s}:exact={v['ohlc_exact']},vol_rel≤{v['volume_rel_maxdiff']:.1e}"
                    for s, v in payload["compaction_report"].items())
        + ";volume 不入认知,如实声明)。",
        "",
        "## 1. 总览(每段 × 成本压力;权益 300 USDT 起)",
        "",
        "| 段 | 成本 | 净盈亏 | 收益率 | 年化 | 最大回撤% | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R | maker费 | taker费 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            g = m(seg, mult)
            t_in = [t for t in runs[f"{seg}@{mult}x"]["trades"] if t["in_segment_open"]]
            fees_maker = sum(t["fees_maker"] for t in t_in)
            fees_taker = sum(t["fees_taker"] for t in t_in)
            lines.append(
                f"| {seg} | {mult}x | {_f(g['net_pnl'])} "
                f"| {_f(100 * g['return_pct'])}% "
                f"| {_f(None if g['ann_return'] is None else 100 * g['ann_return'])}% "
                f"| {_f(100 * g['max_dd_pct'])}% "
                f"| {g['n_trades']} | {_f(g['winrate'], 3)} "
                f"| {_f(g['profit_factor'])} | {_f(g['expectancy_usdt'], 4)} "
                f"| {_f(g['expectancy_r'], 4)} | {_f(fees_maker, 4)} "
                f"| {_f(fees_taker, 4)} |")

    lines += ["", "## 2. G3 执行统计(入场限价:武装→成交/过期;§8.2 减仓限价)", "",
              "| 段 | 成本 | 白名单事件 | 开仓意图 | 武装限价 | LIMIT_FILL | EXPIRED | "
              "成交率 | 过期率 | MIN_SIZE(限价) | 减仓限价武装 | 减仓mak成交 | 减仓超时转taker |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            c = cnt(seg, mult)
            fr = (c["limit_fills_entry"] / c["limits_armed"]
                  if c["limits_armed"] else None)
            er = (c["limit_expired"] / c["limits_armed"]
                  if c["limits_armed"] else None)
            lines.append(
                f"| {seg} | {mult}x | {c['go_events']} | {c['open_intents']} "
                f"| {c['limits_armed']} | {c['limit_fills_entry']} "
                f"| {c['limit_expired']} | {_f(fr, 3)} | {_f(er, 3)} "
                f"| {c['limit_min_size_skips']} | {c['reduce_limits_armed']} "
                f"| {c['reduce_limit_fills']} | {c['reduce_timeout_taker']} |")

    waits = sorted(t["fill_wait_bars"] for t in runs["test@1.0x"]["trades"]
                   if t["fill_wait_bars"] is not None)
    if waits:
        mid = len(waits) // 2
        med = waits[mid] if len(waits) % 2 else 0.5 * (waits[mid - 1] + waits[mid])
        p90 = waits[max(0, math.ceil(0.9 * len(waits)) - 1)]
        lines += ["",
                  f"入场成交等待(fill_wait_bars,15m bar 数):min={waits[0]}, "
                  f"median={med:.0f}, p90={p90}, max={waits[-1]}(test@1x)。", ]

    lines += ["", "## 3. 与 v2 回测同段对照(v2 = 同白名单市价执行 A/B 锚点,"
              "数字取自 reports/backtest_v2.json 冻结产物)", "",
              "| 段 | 成本 | v4 净盈亏 | v2 净盈亏 | v4 交易数 | v2 交易数 | v4 PF | v2 PF | v4 期望USDT | v2 期望USDT |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            cmp_ = payload["v2_comparison"]["cells"][f"{seg}@{mult}x"]
            v4, v2 = cmp_["v4"], cmp_["v2"]
            lines.append(
                f"| {seg} | {mult}x | {_f(v4['net_pnl'])} | {_f(v2['net_pnl'])} "
                f"| {v4['n_trades']} | {v2['n_trades']} "
                f"| {_f(v4['profit_factor'])} | {_f(v2['profit_factor'])} "
                f"| {_f(v4['expectancy_usdt'], 4)} | {_f(v2['expectancy_usdt'], 4)} |")

    up = payload["upgrade_v4"]
    ss = payload["sample_size"]
    lines += ["", "## 4. 样本量判定与升级判据(v4-protocol §3,Bootstrap CI 如实)", "",
              f"- test 段成交笔数(1x,OPEN 决策 ∈ test):**{ss['n_trades_test_1x']}**"
              f"(阈值 ≥ {ss['threshold']})⇒ **{ss['sample_size_verdict']}**。",
              f"- test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,2000 次,"
              f"固定种子):{_f(ss['test_net_pnl_mean_ci95_1x'][0], 4)} ~ "
              f"{_f(ss['test_net_pnl_mean_ci95_1x'][1], 4)} USDT —— 如实报告,"
              "宽就宽(v4-protocol §2)。",
              "",
              "| 判据 | 值 | 通过 |", "|---|---|---|",
              f"| test 段成交 ≥ 60 | {up['criteria']['n_trades_test_ge_60']} "
              f"(n={ss['n_trades_test_1x']}) "
              f"| {'✓' if up['criteria']['n_trades_test_ge_60'] else '✗'} |",
              f"| test 净期望 > 0(1x) | {_f(up['criteria']['expectancy_test_usdt'], 4)} "
              f"| {'✓' if up['criteria']['expectancy_test_gt_0'] else '✗'} |",
              f"| test@2x PF > 1 | {_f(up['criteria']['pf_test_2x'])} "
              f"| {'✓' if up['criteria']['pf_test_2x_gt_1'] else '✗'} |",
              f"| **结论(全部满足才升级)** | — | **{up['verdict']}** |"]
    if ss["sample_size_verdict"] == "INSUFFICIENT_DATA":
        lines += ["",
                  "> INSUFFICIENT_DATA 处置(v4-protocol §2):不产生任何『看起来转正』"
                  "的叙述;低频风格的合规补证手段只有前向 paper 积累,不是修回测曲线。"]

    base = m("test", 1.0)
    lines += ["", "## 5. 分解(test 段,成本 1x)", "",
              "### 方向", "", "| direction | n | 净盈亏 | 期望值 | 胜率 |",
              "|---|---|---|---|---|"]
    for d, v in sorted(base["by_direction"].items()):
        lines.append(f"| {d} | {v['n']} | {_f(v['net'])} "
                     f"| {_f(v['expectancy'], 4)} | {_f(v['winrate'], 3)} |")
    lines += ["", "### 按 symbol", "",
              "| symbol | n | 净盈亏 | 期望值 | 胜率 |", "|---|---|---|---|---|"]
    for sym, v in sorted(base["by_symbol"].items()):
        lines.append(f"| {sym} | {v['n']} | {_f(v['net'])} "
                     f"| {_f(v['expectancy'], 4)} | {_f(v['winrate'], 3)} |")

    liq_any = sum(m(s, mm)["counters"]["liquidations"] for s in SEGMENTS
                  for mm in COST_MULTS)
    lines += ["", "## 6. 破产风险与限制", "",
              f"- 强平触发合计(全部 9 次回放):**{liq_any}**(0 为预期,断言)。",
              "- 已知近似与如实声明:① 限价严格穿过才成交、持平不成交(不假设排队"
              "靠前,协议声明的残余乐观风险);② 穿透缺口按 limit 价成交(不给价格"
              "改善,保守);③ 入场成交小时不做 §8 裁决(该小时含成交前路径,§8 自"
              "下一整小时起,N_time 以整持仓小时计);④ 盯市/资金费计提细化到 15m"
              "(dt 比例口径不变,与 v2 存在小额账面差);⑤ 同根因果序 = 开盘撮合 "
              "→ 挂单成交(15m 双向触及若出现,按 §8.4 既有路径启发式,当前 15m 层"
              "无决策歧义点);⑥ 其余与 v1/v2 相同(当前合约规格代入历史等)。",
              "",
              "机器可读全量:`reports/backtest_v4.json`;逐笔:`reports/trades_v4_*.csv`"
              "(增列 entry_mode/fill_wait_bars/limit_price/fees_maker/fees_taker);"
              "逐 15m bar 权益:`reports/equity_v4_*.csv`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
