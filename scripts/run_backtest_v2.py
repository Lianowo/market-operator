"""v2 回测全管线(docs/v2-protocol.md §1/§3):E7 白名单 + F1,9 段(3 段×3 成本压力)。

用法:  python scripts/run_backtest_v2.py
产物:  reports/backtest_v2.json / backtest_v2.md
       reports/trades_v2_{full,dev,test}[_cost1_5,_cost2_0].csv
       reports/equity_v2_{full,dev,test}[_cost1_5,_cost2_0].csv

纪律(冻结,先于本脚本首次运行):
- 白名单 = config/v2.json whitelist_v2({E7|short, E7|long});F1 = dist(close,
  invalid) ≥ k_is·atr,k_is=0.8 先验(strategy 层执行);r_min/room 同 §7。
- 出场 = methodology §8 六条原样;每 symbol 一仓;风险/成本模型不变(v2
  协议 §1)。"E6-as-entry 被归因否定退役",v2 不换白名单。
- 认知预计算与 v1 同源(EventDetector 同一路径);段分界 = research.split_boundary。
- E7 发射/过 F1 计数与 event_study_v2 交叉核对(诚实性,写进报告)。
- 升级判据(v2-protocol §3-4):E7 绝对净期望>0 且 CI 下限>0(event study,
  test)且回测 test@2x PF>1,全部满足 ⇒ Paper 候选;否则 Research Only
  (acceptance 更新由维护者执行)。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data                       # noqa: E402
from market_operator.backtest import (                            # noqa: E402
    BacktestResult,
    compute_metrics,
    prepare_cognition,
    run_backtest,
)
from market_operator.broker import CONTRACTS_PATH, fetch_contract_specs  # noqa: E402
from market_operator.hypothesis import f1_invalid_distance_ok      # noqa: E402
from market_operator.research import split_boundary                # noqa: E402
from market_operator.strategy import Strategy, StrategyParams      # noqa: E402
from market_operator.structure import Params                       # noqa: E402

CONFIG_V2 = ROOT / "config" / "v2.json"
ES_V2_PATH = ROOT / "reports" / "event_study_v2.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev", "test")
COST_MULTS = (1.0, 1.5, 2.0)

TRADE_COLUMNS = [
    "trade_id", "symbol", "direction", "event_id", "regime", "r_mult_planned",
    "open_decision_ts", "open_decision_iso", "open_ts", "open_iso", "entry",
    "qty", "notional", "used_leverage", "invalid", "target_1", "risk_usdt",
    "closed", "close_decision_ts", "close_decision_iso", "exit_ts", "exit_iso",
    "exit_price", "exit_kind", "hold_hours", "gross_pnl", "fees", "funding",
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


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(CONFIG_V2.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V2)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    k_is = float(raw_cfg["k_is"])
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist_v2"])
    equity0 = 300.0

    contracts, spec_src = fetch_contract_specs(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…")

    cogs = {}
    for sym in symbols:
        t0 = time.time()
        df = mo_data.load(sym)
        cogs[sym] = prepare_cognition(df, params, sym, enable_e7=True)
        print(f"[cognition] {sym}: {len(df)} bars, "
              f"{sum(len(v) for v in cogs[sym].events_by.values())} events, "
              f"{time.time() - t0:.1f}s")

    # E7 发射 / 过 F1 计数(与 event_study_v2 交叉核对;knows close 即决策收盘)
    e7_counts: dict = {}
    for sym in symbols:
        cog = cogs[sym]
        fired, passed = [], []
        for i, evs in cog.events_by.items():
            for e in evs:
                if (e.id, e.direction) in whitelist:
                    fired.append(e)
                    if f1_invalid_distance_ok(e, float(cog.bars[i][4]), k_is):
                        passed.append(e)
        e7_counts[sym] = {"whitelist_events": len(fired),
                          "passed_f1": len(passed)}

    broker_kw = dict(dev_frac=dev_frac, equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params)
    results: dict = {}
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            t0 = time.time()
            res = run_backtest(cogs, segment=seg, cost_multiplier=mult,
                               strategy=Strategy(
                                   StrategyParams(whitelist=whitelist, k_is=k_is)),
                               whitelist=whitelist, **broker_kw)
            results[(seg, mult)] = (res, compute_metrics(res))
            m = results[(seg, mult)][1]
            print(f"[bt] {seg:5s} cost {mult}x: net={m['net_pnl']:+.2f} "
                  f"trades={m['n_trades']} DD={m['max_dd_pct']:.2%} "
                  f"liq={m['counters']['liquidations']} "
                  f"({time.time() - t0:.1f}s)")

    # -- v2-protocol §3-4 升级判据(事件研究只读 + 本回测 test@2x PF) -----------
    es = json.loads(ES_V2_PATH.read_text(encoding="utf-8")) \
        if ES_V2_PATH.exists() else None
    es_checks = {}
    for mech in ("E7|short", "E7|long"):
        tt = ((es or {}).get("mechanisms", {}).get(mech, {}).get("test", {}) or {})
        h24 = (tt.get("horizons", {}) or {}).get("24", {}) or {}
        c10 = (h24.get("cost", {}) or {}).get("1_0", {})
        es_checks[mech] = {
            "test_n_events": tt.get("n_events"),
            "event_net_atr_1x_mean": c10.get("event_net_atr_mean"),
            "event_net_atr_ci95": c10.get("event_net_atr_ci95"),
            "abs_net_positive": bool(c10.get("event_net_atr_mean") is not None
                                     and c10["event_net_atr_mean"] > 0.0),
            "ci_lower_gt_0": bool(c10.get("event_net_atr_ci95") is not None
                                  and c10["event_net_atr_ci95"][0] > 0.0),
            "verdict": (tt.get("go_no_go", {}) or {}).get("verdict"),
        }
    pf_2x = results[("test", 2.0)][1]["profit_factor"]
    upgrade = {
        "criteria": es_checks,
        "backtest_test_2x_profit_factor": pf_2x,
        "backtest_test_2x_pf_gt_1": bool(pf_2x is not None and pf_2x > 1.0),
        "verdict": ("PAPER_CANDIDATE" if (
            all(v["abs_net_positive"] and v["ci_lower_gt_0"]
                for v in es_checks.values())
            and pf_2x is not None and pf_2x > 1.0)
            else "RESEARCH_ONLY"),
    }

    REPORTS.mkdir(exist_ok=True)
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            res = results[(seg, mult)][0]
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame(res.trades).to_csv(
                REPORTS / f"trades_v2_{seg}{suffix}.csv", index=False)
            equity_frame(res).to_csv(
                REPORTS / f"equity_v2_{seg}{suffix}.csv", index=False)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v2-protocol.md(§1 机制,§3 评估;§7/§8 规则原样)",
            "meta": raw_cfg["meta"], "whitelist_v2": raw_cfg["whitelist_v2"],
            "k_is": k_is,
            "test_eval_declaration": "v2 唯一一次 test 评估(第 4 次;见 "
                                     "docs/research-log.md 条目 6)",
        },
        "config": {
            "config_file": "config/v2.json",
            "equity0": equity0, "dev_frac": dev_frac,
            "risk_frac": broker_kw["risk_frac"],
            "leverage_cap": broker_kw["leverage_cap"],
            "taker_fee": broker_kw["taker_fee"],
            "slippage_per_side": broker_kw["slippage_per_side"],
            "funding_per_8h_per_side": broker_kw["funding_per_8h"],
            "cost_mults": list(COST_MULTS), "segments": list(SEGMENTS),
            "whitelist": ["%s|%s" % (i, "short" if d < 0 else "long")
                          for i, d in sorted(whitelist)],
            "k_is": k_is,
        },
        "contracts": {
            "source": spec_src, "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
            "cache_sha256": contracts_sha,
            "specs": {s: vars(c) for s, c in contracts.items()},
        },
        "conventions": {
            "decision_ts": "决策 bar 的 ts;决策于该 bar 收盘做出",
            "fill_ts": "成交 = 决策后下一根该品种 bar 开盘(broker 断言行内强制)",
            "funding": "资金费 = notional·rate·持仓时长/8h,方向无关恒为成本(同 v1)",
            "liquidation": "近似强平保护 = 单仓 |浮亏| ≥ 0.9·equity_mtm 时下一根开盘平仓",
        },
        "e7_counts": e7_counts,
        "event_study_v2_upgrade_check": upgrade,
        "runs": {},
    }
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            res, m = results[(seg, mult)]
            payload["runs"][f"{seg}@{mult}x"] = {"metrics": m,
                                                 "trades": res.trades}

    (REPORTS / "backtest_v2.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v2.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/backtest_v2.json/.md, trades_v2/equity_v2 CSVs "
          f"(total {time.time() - t_start:.0f}s)")


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    return dash if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render_md(payload: dict) -> str:
    runs = payload["runs"]
    cfg = payload["config"]

    def m(seg, mult):
        return runs[f"{seg}@{mult}x"]["metrics"]

    lines = [
        "# v2 回测报告(docs/v2-protocol.md:E7 白名单 + F1;§7/§8 全规则原样)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        f"- 机制(v2-protocol §1):whitelist={cfg['whitelist']},k_is="
        f"{cfg['k_is']}(F1 失效距离下限,v2-protocol §1 先验);r_min=1.5、"
        "结构目标、每 symbol 一仓、§8 六条出场、风险/成本模型全部同 v1。",
        "- E6-as-entry 已被 v1 归因否定并退役(v2-protocol §0/§1);v2 白名单"
        "仅 E7,不换回任何 v1 机制。",
        f"- **{payload['protocol']['test_eval_declaration']}**:本报告为该次"
        "评估的回测侧产物。",
        f"- 资金模型:初始权益 {cfg['equity0']:.0f} USDT,risk_frac="
        f"{cfg['risk_frac']},leverage_cap={cfg['leverage_cap']}x;成本先验同 v1,"
        f"成本压力 {cfg['cost_mults']}。",
        f"- 合约规格:{payload['contracts']['source']}(与 v1 同缓存)。",
        "- E7 发射/过 F1 计数(回测认知层,与 event_study_v2 同源核对):"
        + "; ".join(f"{s}={v['passed_f1']}/{v['whitelist_events']}"
                    for s, v in payload["e7_counts"].items()) + "。",
        "",
        "## 1. 总览(每段 × 成本压力;权益 300 USDT 起)",
        "",
        "| 段 | 成本 | 净盈亏 | 收益率 | 年化 | 最大回撤% | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R |",
        "|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            g = m(seg, mult)
            lines.append(
                f"| {seg} | {mult}x | {_f(g['net_pnl'])} "
                f"| {_f(100 * g['return_pct'])}% "
                f"| {_f(None if g['ann_return'] is None else 100 * g['ann_return'])}% "
                f"| {_f(100 * g['max_dd_pct'])}% "
                f"| {g['n_trades']} | {_f(g['winrate'], 3)} "
                f"| {_f(g['profit_factor'])} | {_f(g['expectancy_usdt'], 4)} "
                f"| {_f(g['expectancy_r'], 4)} |")

    lines += ["", "### 成本与计数表", "",
              "| 段 | 成本 | 费用 | 滑点 | 资金费 | 成本合计 | 白名单事件 | 开仓意图 | "
              "成交 | MIN_SIZE | 强平 | 持仓中位h |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            g = m(seg, mult)
            c = g["counters"]
            lines.append(
                f"| {seg} | {mult}x | {_f(g['fees_total'])} "
                f"| {_f(g['slippage_total'])} | {_f(g['funding_total'], 4)} "
                f"| {_f(g['fees_total'] + g['slippage_total'] + g['funding_total'])} "
                f"| {c['go_events']} | {c['open_intents']} | {c['opens_filled']} "
                f"| {c['min_size_skips']} | {c['liquidations']} "
                f"| {_f(g['holding']['median_h'], 1)} |")

    up = payload["event_study_v2_upgrade_check"]
    lines += ["", "## 2. v2-protocol §3-4 升级判据", "",
              "| 判据 | 值 | 通过 |", "|---|---|---|"]
    for mech, v in sorted(up["criteria"].items()):
        lines.append(
            f"| {mech} 事件净期望>0(test,H24@1x,CI 下限>0) "
            f"| n={v['test_n_events']},净={_f(v['event_net_atr_1x_mean'], 4)},"
            f"CI={v['event_net_atr_ci95']},verdict={v['verdict']} "
            f"| {'✓' if v['abs_net_positive'] and v['ci_lower_gt_0'] else '✗'} |")
    lines.append(
        f"| 回测 test@2x PF>1 | PF={_f(up['backtest_test_2x_profit_factor'])} "
        f"| {'✓' if up['backtest_test_2x_pf_gt_1'] else '✗'} |")
    lines.append(
        f"| **结论(全部满足才升级)** | — | **{up['verdict']}** |")

    base = m("test", 1.0)
    lines += ["", "## 3. 分解(test 段,成本 1x)", "",
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
    lines += ["", "## 4. 破产风险与限制", "",
              f"- 强平触发合计(全部 9 次回放):**{liq_any}**(0 为预期)。",
              "- 其余已知近似与 v1 回测相同(当前合约规格代入历史、逐 bar 计提"
              "funding、近似强平;见 reports/backtest.md §8)。",
              "",
              "机器可读全量:`reports/backtest_v2.json`;逐笔:`reports/trades_v2_*.csv`;"
              "逐 bar 权益:`reports/equity_v2_*.csv`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
