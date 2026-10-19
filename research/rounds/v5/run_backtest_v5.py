"""v5 回测全管线(docs/v5-protocol.md §1/§2):破位质量路由 × 9 段 + dev 敏感性。

用法:  python scripts/run_backtest_v5.py
产物:  reports/backtest_v5.json / backtest_v5.md
       reports/trades_v5_{full,dev,test}[_cost1_5,_cost2_0].csv
       reports/equity_v5_{full,dev,test}[_cost1_5,_cost2_0].csv

纪律(v5-protocol,冻结于任何 v5 运行之前;最终形态,最后一轮):
- 白名单 short-only:{E1|short, E6|short, E7|short}(config/v5.json whitelist,
  parent=config/v4.json,先验逐项照抄不引入新调参)。
- 路由(docs/v5-protocol.md §1):E6 质量 STRONG(margin ≥ k_dq=0.5 ∧
  impulse ≥ k_imp=1.5)⇒ 市价次 1h 开盘;WEAK ⇒ 不入场,登记 episode,其后同
  episode E7|short(attrs["ref"] == l_star)⇒ v4 G3 前缘限价(N_fill=12,
  超时 = 没有这笔交易);E1 ⇒ 永远市价次开盘,信号出现先撤该品种未成交入场挂
  单(优先级最高)。全入口 F1(k_is=0.8,v2 冻结先验);r_min/结构目标门与
  v1/v4 同构;§8 出场、15m 执行路径、maker/taker 非对称费用与 v4 一致。
- 对账硬约束(与 v3/v4 同构,任一不符即 raise):15m↔1h 对齐 +
  compaction OHLC 逐格一致;dev/test 拐点 1h 索引 12320;E7 发射/过 F1 计数
  与 reports/backtest_v2.json 冻结产物逐 symbol 一致;E1+E6|short 全历史计数
  == reports/backtest.json(v1)full@1.0x go_events(v1 parity 锚点);
  每个 E7 事件 attrs["atr"] == 认知缓存同 bar ATR(F1 两口径等价证据)。
- 敏感性(仅 dev,不改先验):k_dq ∈ {0.35,0.5,0.75} × k_imp ∈ {1.25,1.5,2.0}
  净额;不许上 test 择优。
- 样本量(v5-protocol §2):test 段成交 < 60 笔 ⇒ INSUFFICIENT_DATA,报告
  不得给出任何"看起来转正"的叙述。
- 合约规格强制离线缓存(同 v3/v4 纪律;data/contracts.json 逐条目 sha256 校验)。
- **test 段第 7 次且最终评估(v5 唯一一次;research-log 条目 10)**。
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

from market_operator import aggregate as agg                     # noqa: E402
from market_operator import data as mo_data                      # noqa: E402
from research.rounds.v1.backtest import compute_metrics, prepare_cognition  # noqa: E402
from research.rounds.v4.backtest_v4 import (                        # noqa: E402
    alignment_issues_ci,
    build_exec_frame,
)
from research.rounds.v5.backtest_v5 import (                        # noqa: E402
    BacktestResult,
    run_backtest_v5,
)
from market_operator.broker import CONTRACTS_PATH, ContractSpec  # noqa: E402
from market_operator.hypothesis import f1_invalid_distance_ok    # noqa: E402
from market_operator.research import block_bootstrap_ci          # noqa: E402
from research.rounds.v5.strategy_v5 import (                        # noqa: E402
    ROUTE_E1,
    ROUTE_STRONG_E6,
    ROUTE_WEAK_E7,
    V5Params,
    V5Router,
)
from market_operator.structure import Params                     # noqa: E402

CONFIG_V5 = ROOT / "config" / "v5.json"
BT_V1_PATH = ROOT / "reports" / "backtest.json"
BT_V2_PATH = ROOT / "reports" / "backtest_v2.json"
BT_V4_PATH = ROOT / "reports" / "backtest_v4.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev", "test")
COST_MULTS = (1.0, 1.5, 2.0)
MIN_TRADES_TEST = 60                       # v5-protocol §2 样本量阈
ROUTES = (ROUTE_STRONG_E6, ROUTE_WEAK_E7, ROUTE_E1)
ROUTE_LABEL = {ROUTE_STRONG_E6: "strong-E6(市价)",
               ROUTE_WEAK_E7: "weak-E7(前缘限价)",
               ROUTE_E1: "E1(市价)"}
SENS_K_DQ = (0.35, 0.5, 0.75)              # v5-protocol §1/§2:dev 敏感性,不改先验
SENS_K_IMP = (1.25, 1.5, 2.0)

TRADE_COLUMNS = [
    "trade_id", "symbol", "direction", "route", "event_id", "regime",
    "r_mult_planned", "l_star", "weak_bars", "e6_margin", "e6_impulse",
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
    """离线合约规格(v3/v4 纪律;v1/v2/v4 同缓存,逐条目 sha256 校验)。"""
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


def route_bucket(trades: list) -> dict:
    """单路由桶:笔数/胜率/平均R/净额/期望(in_segment_open 口径,与指标一致)。"""
    closed = [t for t in trades if t["closed"]]
    wins = [t for t in closed if t["net_pnl"] > 0]
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    net = sum(t["net_pnl"] for t in trades)
    return {"n_trades": len(trades), "n_closed": len(closed),
            "winrate": (len(wins) / len(closed)) if closed else None,
            "avg_r": (sum(rs) / len(rs)) if rs else None,
            "net_pnl": net,
            "expectancy_usdt": (net / len(trades)) if trades else None}


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(CONFIG_V5.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V5)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    k_is = float(raw_cfg["k_is"])
    k_dq = float(raw_cfg["k_dq"])
    k_imp = float(raw_cfg["k_imp"])
    n_fill = int(raw_cfg["n_fill"])
    exec_tf = int(raw_cfg["exec_tf_seconds"])
    if raw_cfg["entry_mode"] != "quality_routed":
        raise RuntimeError("config/v5.json entry_mode must be quality_routed")
    if exec_tf != 900 or 3600 % exec_tf != 0:
        raise RuntimeError(f"exec_tf_seconds={exec_tf} must divide 3600 (v5=900)")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    if whitelist != frozenset({("E1", -1), ("E6", -1), ("E7", -1)}):
        raise RuntimeError(f"config/v5.json whitelist must be short-only "
                           f"{{E1,E6,E7}}: {sorted(whitelist)}")
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

    # -- 冻结产物对账(任一不符即停止) -----------------------------------------
    bt_v2 = json.loads(BT_V2_PATH.read_text(encoding="utf-8")) \
        if BT_V2_PATH.exists() else None
    if bt_v2 is None:
        raise RuntimeError("reports/backtest_v2.json 缺失(E7 对账锚点)")
    bt_v1 = json.loads(BT_V1_PATH.read_text(encoding="utf-8")) \
        if BT_V1_PATH.exists() else None
    if bt_v1 is None:
        raise RuntimeError("reports/backtest.json 缺失(v1 parity 锚点)")
    event_counts: dict = {}
    e7_counts: dict = {}
    atr_equal = True
    v1_wl = frozenset({("E1", -1), ("E6", -1)})
    for sym in symbols:
        cog = frames[sym].cog
        e1s = e6s = 0
        fired, passed = [], []
        for i, evs in cog.events_by.items():
            for e in evs:
                if e.id == "E7" and e.attrs.get("atr") != cog.atrs[i]:
                    atr_equal = False
                if (e.id, e.direction) in v1_wl:
                    if e.id == "E1":
                        e1s += 1
                    else:
                        e6s += 1
                if e.id == "E7":                                 # v2 白名单 ±1
                    fired.append(e)
                    if f1_invalid_distance_ok(e, float(cog.bars[i][4]), k_is):
                        passed.append(e)
        event_counts[sym] = {"E1_short": e1s, "E6_short": e6s}
        e7_counts[sym] = {"whitelist_events": len(fired),
                          "passed_f1": len(passed)}
        v2c = bt_v2["e7_counts"][sym]
        if (v2c["whitelist_events"], v2c["passed_f1"]) \
                != (len(fired), len(passed)):
            raise RuntimeError(
                f"E7 计数对账失败 {sym}: v5 fired={len(fired)} passed={len(passed)} "
                f"vs v2 冻结产物 {v2c}")
    if not atr_equal:
        raise RuntimeError("E7 attrs['atr'] != 认知缓存同 bar ATR(F1 口径漂移)")
    e1e6_total = sum(v["E1_short"] + v["E6_short"] for v in event_counts.values())
    v1_go = bt_v1["runs"]["full@1.0x"]["metrics"]["counters"]["go_events"]
    if e1e6_total != v1_go:
        raise RuntimeError(f"E1+E6|short 计数对账失败: v5 认知={e1e6_total} vs "
                           f"v1 冻结产物 go_events={v1_go}")
    print(f"[xcheck] E7 双向计数 == v2 冻结产物(逐 symbol); "
          f"E1+E6|short={e1e6_total} == v1 go_events={v1_go}; "
          f"E7 atr 口径一致={atr_equal}", flush=True)

    base_router_kw = dict(k_dq=k_dq, k_imp=k_imp, k_is=k_is,
                          r_min=float(raw_cfg["r_min"]), whitelist=whitelist)
    broker_kw = dict(dev_frac=dev_frac, equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     maker_fee=float(raw_cfg["maker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params, n_fill=n_fill)

    def run_one(seg: str, mult: float, rk: dict) -> tuple:
        res = run_backtest_v5(
            frames, segment=seg, cost_multiplier=mult,
            router=V5Router(V5Params(**rk)), **broker_kw)
        return res, compute_metrics(res)

    results: dict = {}
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            t0 = time.time()
            res, m = run_one(seg, mult, base_router_kw)
            results[(seg, mult)] = (res, m)
            c = res.counters
            print(f"[bt] {seg:5s} cost {mult}x: net={m['net_pnl']:+.2f} "
                  f"trades={m['n_trades']} armed={c['limits_armed']} "
                  f"fills={c['limit_fills_entry']} expired={c['limit_expired']} "
                  f"mkt_fills={c['opens_filled']} e1_cancel={c['e1_priority_cancels']} "
                  f"liq={c['liquidations']} ({time.time() - t0:.1f}s)", flush=True)
    liq_any = sum(results[(s, mm)][0].counters["liquidations"]
                  for s in SEGMENTS for mm in COST_MULTS)
    if liq_any != 0:
        raise RuntimeError(f"强平非预期触发: {liq_any}")

    # -- dev 敏感性(仅观察稳定性,不改先验;不许上 test 择优) ------------------
    sensitivity: dict = {}
    for kd in SENS_K_DQ:
        for ki in SENS_K_IMP:
            cells = {}
            for mult in COST_MULTS:
                _r, m = run_one("dev", mult,
                                {**base_router_kw, "k_dq": kd, "k_imp": ki})
                cells[f"{mult}x"] = {"net_pnl": m["net_pnl"],
                                     "n_trades": m["n_trades"],
                                     "profit_factor": m["profit_factor"]}
            sensitivity[f"{kd}|{ki}"] = cells
            print(f"[sens] k_dq={kd} k_imp={ki}: dev net "
                  + " / ".join(f"{mm}x={cells[f'{mm}x']['net_pnl']:+.2f}"
                               for mm in COST_MULTS), flush=True)

    # -- v5-protocol §2 升级判据与样本量判定(先于叙述) -------------------------
    m_test_1x = results[("test", 1.0)][1]
    exp_test_15x = results[("test", 1.5)][1]["expectancy_usdt"]
    pf_2x = results[("test", 2.0)][1]["profit_factor"]
    n_test = m_test_1x["n_trades"]
    sample = {
        "n_trades_test_1x": n_test,
        "threshold": MIN_TRADES_TEST,
        "sample_size_verdict": ("SUFFICIENT(>=60)" if n_test >= MIN_TRADES_TEST
                                else "INSUFFICIENT_DATA"),
        "honesty_note": ("test<60 ⇒ INSUFFICIENT_DATA:不产生任何'看起来转正'"
                         "的叙述;合规补证手段 = 前向 paper 积累,不修回测曲线"
                         "(v4-protocol §2 沿用,v5-protocol §2)"),
    }
    upgrade = {
        "criteria": {
            "n_trades_test_ge_60": n_test >= MIN_TRADES_TEST,
            "expectancy_test_1_5x_gt_0": bool(exp_test_15x is not None
                                              and exp_test_15x > 0.0),
            "pf_test_2x_gt_1": bool(pf_2x is not None and pf_2x > 1.0),
            "expectancy_test_usdt_1_5x": exp_test_15x,
            "pf_test_2x": pf_2x,
        },
        "verdict": ("PAPER_CANDIDATE" if (
            n_test >= MIN_TRADES_TEST
            and exp_test_15x is not None and exp_test_15x > 0.0
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
                REPORTS / f"trades_v5_{seg}{suffix}.csv", index=False)
            equity_frame(res).to_csv(
                REPORTS / f"equity_v5_{seg}{suffix}.csv", index=False)

    bt_v4 = json.loads(BT_V4_PATH.read_text(encoding="utf-8")) \
        if BT_V4_PATH.exists() else None
    route_cells = {}
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            t_in = [t for t in results[(seg, mult)][0].trades
                    if t["in_segment_open"]]
            route_cells[f"{seg}@{mult}x"] = {
                r: route_bucket([t for t in t_in if t["route"] == r])
                for r in ROUTES}

    def cmp_key(src_body, seg, mult):
        return {k: src_body["runs"][f"{seg}@{mult}x"]["metrics"][k]
                for k in ("n_trades", "net_pnl", "profit_factor",
                          "expectancy_usdt", "winrate")}

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v5-protocol.md(§1 路由状态机,§2 评估;最终形态)",
            "meta": raw_cfg["meta"], "whitelist": raw_cfg["whitelist"],
            "entry_mode": raw_cfg["entry_mode"],
            "k_dq": k_dq, "k_imp": k_imp, "k_is": k_is,
            "n_fill": n_fill, "exec_tf_seconds": exec_tf,
            "maker_fee": float(raw_cfg["maker_fee"]),
            "test_eval_declaration": "test 段第 7 次且最终评估(v5 唯一一次;见 "
                                     "docs/research-log.md 条目 10)",
        },
        "config": {
            "config_file": "config/v5.json",
            "equity0": equity0, "dev_frac": dev_frac,
            "risk_frac": broker_kw["risk_frac"],
            "leverage_cap": broker_kw["leverage_cap"],
            "taker_fee": broker_kw["taker_fee"],
            "maker_fee": broker_kw["maker_fee"],
            "slippage_per_side": broker_kw["slippage_per_side"],
            "funding_per_8h_per_side": broker_kw["funding_per_8h"],
            "cost_mults": list(COST_MULTS), "segments": list(SEGMENTS),
            "whitelist": ["%s|short" % i for i, _d in sorted(whitelist)],
            "k_is": k_is, "k_dq": k_dq, "k_imp": k_imp,
            "n_fill": n_fill, "exec_tf_seconds": exec_tf,
        },
        "contracts": {
            "source": spec_src, "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
            "cache_sha256": contracts_sha,
            "specs": {s: vars(c) for s, c in contracts.items()},
        },
        "conventions": {
            "decision_ts": "决策 1h bar 的 ts(收盘决策,与 v1/v2/v4 同约定)",
            "armed_at_ts": "= decision_ts(1h bar 标签;E7 前缘限价 armed+n_fill×1h 超时)",
            "fill_ts": "LIMIT_FILL = 成交 15m bar 标签(严格穿过,> armed);市价"
                       " OPEN/REDUCE/EXIT = 下一根 1h bar 首根 15m 开盘,价格 = 1h "
                       "开盘价(与 v2/v4 一致)",
            "routing": "E6 质量(margin/impulse,事件 bar 已知量)路由:STRONG ⇒ 市价"
                       "次开盘;WEAK ⇒ 不入场,登记 episode(l_star);同 episode E7"
                       "(attrs ref == l_star,浮点同源容差 1e-9)⇒ 前缘限价 G3;"
                       "E1 ⇒ 永远市价次开盘,先撤未成交入场挂单(E1 优先级最高,"
                       "账本 CANCEL/E1_PRIORITY)",
            "f1": "全入口 F1:k_is=0.8;E1/E6 以事件 bar ATR 度量(检测器零改动,"
                  "StrategyView.atr = 认知缓存逐 bar ATR);E7 同 v4(attrs atr,"
                  "已断言同一数值)",
            "marking": "盯市/资金费计提细化到 15m(同 v4);①Open 撮合先于②挂单的"
                       "同根因果序结构性解决 15m 路径",
            "funding": "资金费 = notional·rate·m·(持仓 ts 差)/8h,方向无关恒为成本",
            "liquidation": "近似强平保护 = 单仓 |浮亏| ≥ 0.9·equity_mtm 时下一根"
                           "15m 开盘平仓(预期 0 次,已断言)",
            "cash_mirror": "权益 = equity0 + Σ(realized_pnl − fee + funding_delta)"
                           " 逐行对账(同 v1/v2/v4)",
        },
        "compaction_report": compaction_report,
        "event_counts": event_counts,
        "e7_counts": e7_counts,
        "cross_checks": {
            "e7_counts_v2_frozen_match": True,
            "e1e6_short_total_vs_v1_go_events": {"v5": e1e6_total, "v1": v1_go},
            "e7_attrs_atr_equals_cognition_atr": atr_equal,
        },
        "sample_size": sample,
        "upgrade_v5": upgrade,
        "route_buckets": route_cells,
        "dev_sensitivity": {
            "note": "dev 段,净额/笔数/PF 按成本 1x/1.5x/2x;仅观察稳定性,"
                    "不改先验,不许上 test 择优(v5-protocol §1/§2)",
            "grid": {"k_dq": list(SENS_K_DQ), "k_imp": list(SENS_K_IMP)},
            "cells": sensitivity,
        },
        "v2_v4_comparison": {
            "source": ("reports/backtest_v2.json(v2 冻结产物)与 "
                       "reports/backtest_v4.json(v4 冻结产物)同段对照"),
            "cells": {f"{seg}@{mult}x": {
                "v2": cmp_key(bt_v2, seg, mult),
                "v4": (cmp_key(bt_v4, seg, mult) if bt_v4 else None),
                "v5": {k: results[(seg, mult)][1][k]
                       for k in ("n_trades", "net_pnl", "profit_factor",
                                 "expectancy_usdt", "winrate")},
            } for seg in SEGMENTS for mult in COST_MULTS},
        },
        "runs": {},
    }
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            res, m = results[(seg, mult)]
            payload["runs"][f"{seg}@{mult}x"] = {"metrics": m,
                                                 "trades": res.trades}

    (REPORTS / "backtest_v5.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v5.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/backtest_v5.json/.md, trades_v5/equity_v5 CSVs "
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

    insufficient = payload["sample_size"]["sample_size_verdict"] \
        == "INSUFFICIENT_DATA"

    lines = [
        "# v5 回测报告(docs/v5-protocol.md:破位质量路由执行——最终形态,最后一轮)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        f"- 白名单 short-only:{cfg['whitelist']}(long 侧各轮增量期望均为负,"
        "无信息规则剔除,协议如实声明)。",
        f"- 路由(v5-protocol §1):E6|short 质量 margin ≥ k_dq={cfg['k_dq']} 且 "
        f"impulse ≥ k_imp={cfg['k_imp']} ⇒ STRONG 市价次 1h 开盘;否则 WEAK 不入"
        "场、登记 episode(l_star),其后同 episode E7|short(attrs ref == l_star)"
        f"按 v4 G3 挂区域前缘限价(N_fill={cfg['n_fill']},超时 = 没有这笔交易);"
        "E1|short 永远市价次开盘,信号出现先撤该品种未成交入场挂单(E1 优先级最"
        "高,账本 CANCEL/E1_PRIORITY)。",
        f"- 全入口 F1:k_is={cfg['k_is']}(v2 冻结先验);E1/E6 以事件 bar ATR 度"
        "量(检测器 E1-E7 零改动);r_min/结构目标门与 v1/v4 同构;§8 出场、15m "
        f"执行路径(exec_tf={cfg['exec_tf_seconds']}s)、maker/taker 非对称费用"
        f"(限价 maker {cfg['maker_fee']},退出 taker {cfg['taker_fee']} + 滑点 "
        f"{cfg['slippage_per_side']},资金费不变)与 v4 一致。",
        f"- **{payload['protocol']['test_eval_declaration']}**:本报告为该次评估的"
        "回测侧产物。",
        f"- 样本量(v5-protocol §2):test 段成交 < {payload['sample_size']['threshold']} 笔"
        f" ⇒ **{payload['sample_size']['sample_size_verdict']}**"
        + ("——按协议**本报告不给出任何转正叙述**。" if insufficient else "。"),
        f"- 合约规格:{payload['contracts']['source']}(v1/v2/v4 同缓存,离线强制)。",
        "- 认知对账(全部一致,硬约束):E7 发射/过 F1 计数 == v2 冻结产物(逐 "
        "symbol:"
        + "; ".join(f"{s}={v['passed_f1']}/{v['whitelist_events']}"
                    for s, v in payload["e7_counts"].items())
        + f");E1+E6|short 全历史 == v1 go_events "
        f"({payload['cross_checks']['e1e6_short_total_vs_v1_go_events']['v5']} == "
        f"{payload['cross_checks']['e1e6_short_total_vs_v1_go_events']['v1']},"
        "v1 parity 锚点);E7 attrs atr == 认知缓存 ATR。",
        "- 15m 数据与 compaction 1h OHLC 逐格一致("
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

    lines += ["", "## 2. 路由与执行统计(质量判定 → 路由 → 成交)", "",
              "| 段 | 成本 | E6信号 | STRONG | WEAK登记 | WEAK覆盖 | E7信号 | 无episode | ref不匹配 | 匹配 | 市价成交 | 限价武装 | LIMIT_FILL | EXPIRED | E1撤单 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            c = cnt(seg, mult)
            lines.append(
                f"| {seg} | {mult}x | {c['e6_signals']} "
                f"| {c.get('e6_strong_quality', 0)} | {c.get('e6_weak_registered', 0)} "
                f"| {c.get('e6_replaced_pending', 0)} | {c['e7_signals']} "
                f"| {c.get('e7_no_episode', 0)} | {c.get('e7_ref_mismatch', 0)} "
                f"| {c.get('e7_matched_episode', 0)} | {c['opens_filled']} "
                f"| {c['limits_armed']} | {c['limit_fills_entry']} "
                f"| {c['limit_expired']} | {c['e1_priority_cancels']} |")

    lines += ["", "## 3. 按路由分桶(v5 核心判别证据:桶间差异 × 段 × 成本;"
              "笔数/胜率/平均R/净额/期望)", "",
              "| 段 | 成本 | 路由 | 笔数 | 胜率 | 平均R | 净额 | 期望USDT |",
              "|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            for r in ROUTES:
                b = payload["route_buckets"][f"{seg}@{mult}x"][r]
                lines.append(
                    f"| {seg} | {mult}x | {ROUTE_LABEL[r]} | {b['n_trades']} "
                    f"| {_f(b['winrate'], 3)} | {_f(b['avg_r'], 3)} "
                    f"| {_f(b['net_pnl'])} | {_f(b['expectancy_usdt'], 4)} |")

    lines += ["", "## 4. 与 v2/v4 冻结产物同段对照(数字取自冻结产物)", "",
              "| 段 | 成本 | v2 净 | v4 净 | v5 净 | v2 笔数 | v4 笔数 | v5 笔数 | v2 PF | v4 PF | v5 PF | v5 期望USDT |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            cmp_ = payload["v2_v4_comparison"]["cells"][f"{seg}@{mult}x"]
            v2, v4, v5 = cmp_["v2"], cmp_["v4"], cmp_["v5"]
            lines.append(
                f"| {seg} | {mult}x | {_f(v2['net_pnl'])} "
                f"| {_f(None if v4 is None else v4['net_pnl'])} | {_f(v5['net_pnl'])} "
                f"| {v2['n_trades']} | {('—' if v4 is None else v4['n_trades'])} "
                f"| {v5['n_trades']} | {_f(v2['profit_factor'])} "
                f"| {_f(None if v4 is None else v4['profit_factor'])} "
                f"| {_f(v5['profit_factor'])} | {_f(v5['expectancy_usdt'], 4)} |")

    sens = payload["dev_sensitivity"]
    lines += ["", "## 5. dev 敏感性(k_dq × k_imp,仅 dev;仅观察稳定性,不改先验)", "",
              "净额(dev);baseline = k_dq 0.5 × k_imp 1.5。",
              "", "| k_dq \\ k_imp | " + " | ".join(f"{x}" for x in SENS_K_IMP) + " |",
              "|---|" + "---|" * len(SENS_K_IMP)]
    for mult in COST_MULTS:
        lines.append(f"| **{mult}x** | | | |")
        for kd in SENS_K_DQ:
            row = f"| {kd} "
            for ki in SENS_K_IMP:
                cell = sens["cells"][f"{kd}|{ki}"][f"{mult}x"]
                row += f"| {_f(cell['net_pnl'])}({cell['n_trades']}) "
            lines.append(row + "|")

    up = payload["upgrade_v5"]
    ss = payload["sample_size"]
    lines += ["", "## 6. 样本量判定与升级判据(v5-protocol §2,先于叙述)", "",
              f"- test 段成交笔数(1x,OPEN 决策 ∈ test):**{ss['n_trades_test_1x']}**"
              f"(阈值 ≥ {ss['threshold']})⇒ **{ss['sample_size_verdict']}**。",
              f"- test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,2000 次,固定"
              f"种子):{_f(ss['test_net_pnl_mean_ci95_1x'][0], 4)} ~ "
              f"{_f(ss['test_net_pnl_mean_ci95_1x'][1], 4)} USDT —— 如实报告。",
              "",
              "| 判据 | 值 | 通过 |", "|---|---|---|",
              f"| test 段成交 ≥ 60 | {up['criteria']['n_trades_test_ge_60']} "
              f"(n={ss['n_trades_test_1x']}) "
              f"| {'✓' if up['criteria']['n_trades_test_ge_60'] else '✗'} |",
              f"| test 净期望 > 0(1.5x) | {_f(up['criteria']['expectancy_test_usdt_1_5x'], 4)} "
              f"| {'✓' if up['criteria']['expectancy_test_1_5x_gt_0'] else '✗'} |",
              f"| test@2x PF > 1 | {_f(up['criteria']['pf_test_2x'])} "
              f"| {'✓' if up['criteria']['pf_test_2x_gt_1'] else '✗'} |",
              f"| **结论(全部满足才升级)** | — | **{up['verdict']}** |"]
    if insufficient:
        lines += ["",
                  "> INSUFFICIENT_DATA 处置(v5-protocol §2/v4 §2):不产生任何『看起来转"
                  "正』的叙述;低频风格的合规补证手段只有前向 paper 积累,不是修回测曲"
                  "线。两种结局都关闭协议级优化循环(v5 为最终形态)。"]

    base = m("test", 1.0)
    lines += ["", "## 7. 分解(test 段,成本 1x)", "",
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

    liq_any = sum(cnt(s, mm)["liquidations"] for s in SEGMENTS for mm in COST_MULTS)
    lines += ["", "## 8. 破产风险与限制", "",
              f"- 强平触发合计(全部 9 次回放):**{liq_any}**(0 为预期,已断言)。",
              "- 已知近似与如实声明:① E6→E7『同 episode』在零检测器改动下以 E7 "
              "attrs ref == 登记 l_star 操作化(zone 破位前缘价 == 被破 HL pivot 价;"
              "浮点同源容差 1e-9);② 每品种至多保留最新一个 weak-E6 登记(新破位为"
              "活体,旧登记退役);③ 限价严格穿过才成交、持平不成交(v4 声明沿用);"
              "④ 穿透缺口按 limit 价成交(不给价格改善,保守);⑤ 入场成交小时不做 "
              "§8 裁决(同 v4);⑥ 盯市/资金费 15m 化(同 v4);⑦ E1 撤单只针对未成交"
              "入场挂单,不动 §8.2 减仓限价(持仓不与入场挂单共存);⑧ 其余与 "
              "v1/v2/v4 相同(当前合约规格代入历史等)。",
              "",
              "机器可读全量:`reports/backtest_v5.json`;逐笔:`reports/trades_v5_*.csv`"
              "(增列 route/l_star/weak_bars/e6_margin/e6_impulse);逐 15m bar 权益:"
              "`reports/equity_v5_*.csv`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
