"""v3 回测全管线(docs/v3-protocol.md §1/§3):逐 TF × 9 段(3 段 × 3 成本)E7+F1。

用法:  python scripts/run_backtest_v3.py
产物:  reports/backtest_v3.json / backtest_v3.md
       reports/trades_v3_{tf}_{full,dev,test}[_cost1_5,_cost2_0].csv
       reports/equity_v3_{tf}_{full,dev,test}[_cost1_5,_cost2_0].csv

纪律(v3-protocol,冻结):
- 唯一变更 = 观察尺度;机制白名单 = config/v2.json whitelist_v2({E7|short,
  E7|long}) + F1(k_is=0.8,strategy 层执行);r_min/room 同 §7;出场 = §8 六条
  原样;每 symbol 一仓;风险/成本模型不变(300 USDT、risk_frac、leverage_cap、
  taker+slippage+funding 同 v1/v2)。
- 尺度语义:H/N_time 以根数计(bars 语义不变,v3-protocol §1);持仓时长
  funding 由 broker 按 ts 差精确计提(dt/8h,与 TF 无关,自动如实计入长尺度
  持仓,N_time=24 根 1d 仓 = 24 天 funding)——无上游改动。
- 段分界 = research.split_boundary 每品种各自帧;dev/test 拐点与事件研究
  同源(v3-protocol §2;tests/test_aggregate.py 硬编码核算)。
- 合约规格强制离线缓存(v3 "不重抓 API" 纪律扩及合约接口;data/contracts.json
  为 v1/v2 同缓存,逐条目 sha256 校验)。
- E7 发射/过 F1 计数与 event_study_v3 逐 (symbol, TF) 交叉核对,不等即
  raise(诚实性硬约束,同认知源 EventDetector 双路径对账)。
- 升级判据(v3-protocol §3,与 v2 §3-4 逐字相同):某 (TF×方向) 满足全部 ⇒
  Paper 候选:① 事件研究 test 绝对净期望>0(H24@1x);② CI 下界>0;③ 回测
  test@2x PF>1。n<60 的格子按 §2 为 INSUFFICIENT_DATA,§2 优先于 §3:
  不足数据的格子不参与升级(不做叙述性补救)。任一格子不足 ⇒ 该格子
  Research Only;本轮结束即停止(反过拟合纪律,§3/§4)。
- **test 段第 5 次评估(v3 唯一一次;research-log 条目 7)**。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import aggregate as agg                     # noqa: E402
from research.rounds.v1.backtest import (                           # noqa: E402
    BacktestResult,
    compute_metrics,
    prepare_cognition,
    run_backtest,
)
from market_operator.broker import CONTRACTS_PATH, ContractSpec  # noqa: E402
from market_operator.hypothesis import f1_invalid_distance_ok    # noqa: E402
from market_operator.research import split_boundary              # noqa: E402
from research.rounds.v1.strategy import Strategy, StrategyParams    # noqa: E402
from market_operator.structure import Params                     # noqa: E402

CONFIG_V2 = ROOT / "config" / "v2.json"
ES_V3_PATH = ROOT / "reports" / "event_study_v3.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev", "test")
COST_MULTS = (1.0, 1.5, 2.0)
TF_LABELS = ("2h", "4h", "12h", "1d")

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


def load_contracts_cache(symbols: list) -> tuple:
    """离线合约规格(v3 不重抓 API;v1/v2 同缓存,逐条目 sha256 校验)。"""
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
    raw_cfg = json.loads(CONFIG_V2.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V2)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    k_is = float(raw_cfg["k_is"])
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist_v2"])
    equity0 = 300.0

    contracts, spec_src = load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    es = json.loads(ES_V3_PATH.read_text(encoding="utf-8")) \
        if ES_V3_PATH.exists() else None
    if es is None:
        raise RuntimeError("reports/event_study_v3.json 缺失:先运行 "
                           "run_event_study_v3.py(升级判据依赖其 test 数字)")

    agg.ensure_aggregated(symbols, TF_LABELS)

    broker_kw = dict(dev_frac=dev_frac, equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params)

    results: dict = {}
    e7_counts: dict = {}
    cross_ok = True
    for tf in TF_LABELS:
        cogs = {}
        for sym in symbols:
            t0 = time.time()
            df = agg.load_tf(sym, tf)
            cogs[sym] = prepare_cognition(df, params, sym, enable_e7=True)
            print(f"[cognition {tf}/{sym}] {len(df)} bars, "
                  f"{sum(len(v) for v in cogs[sym].events_by.values())} events, "
                  f"{time.time() - t0:.1f}s", flush=True)

        # E7 发射/过 F1 计数(与 event_study_v3 逐 (symbol, TF) 交叉核对)
        es_syms = (es["timeframes"][tf]["symbols"])
        for sym in symbols:
            cog = cogs[sym]
            fired, passed = [], []
            for i, evs in cog.events_by.items():
                for e in evs:
                    if (e.id, e.direction) in whitelist:
                        fired.append(e)
                        if f1_invalid_distance_ok(e, float(cog.bars[i][4]), k_is):
                            passed.append(e)
            e7_counts[f"{tf}/{sym}"] = {"whitelist_events": len(fired),
                                        "passed_f1": len(passed)}
            ok = (len(fired) == es_syms[sym]["n_events_fired"]
                  and len(passed) == es_syms[sym]["n_passed_f1"])
            cross_ok = cross_ok and ok
            if not ok:
                raise RuntimeError(
                    f"E7 计数对账失败 {tf}/{sym}: backtest fired={len(fired)} "
                    f"passed={len(passed)} vs event_study "
                    f"{es_syms[sym]['n_events_fired']}/{es_syms[sym]['n_passed_f1']}")

        for seg in SEGMENTS:
            for mult in COST_MULTS:
                t0 = time.time()
                res = run_backtest(
                    cogs, segment=seg, cost_multiplier=mult,
                    strategy=Strategy(
                        StrategyParams(whitelist=whitelist, k_is=k_is)),
                    whitelist=whitelist, **broker_kw)
                results[(tf, seg, mult)] = (res, compute_metrics(res))
                m = results[(tf, seg, mult)][1]
                print(f"[bt {tf}] {seg:5s} cost {mult}x: net={m['net_pnl']:+.2f} "
                      f"trades={m['n_trades']} DD={m['max_dd_pct']:.2%} "
                      f"liq={m['counters']['liquidations']} "
                      f"({time.time() - t0:.1f}s)", flush=True)

    # -- v3-protocol §3 升级判据(§2 INSUFFICIENT_DATA 优先;逐 TF×方向格子) -----
    upgrades: dict = {}
    for tf in TF_LABELS:
        es_checks = {}
        for mech in ("E7|short", "E7|long"):
            tt = (es["timeframes"][tf]["mechanisms"].get(mech, {})
                  .get("test", {}) or {})
            h24 = (tt.get("horizons", {}) or {}).get("24", {}) or {}
            c10 = (h24.get("cost", {}) or {}).get("1_0", {})
            n_test = tt.get("n_events", 0) or 0
            es_checks[mech] = {
                "test_n_events": n_test,
                "n_ge_60": bool(n_test >= 60),
                "event_net_atr_1x_mean": c10.get("event_net_atr_mean"),
                "event_net_atr_ci95": c10.get("event_net_atr_ci95"),
                "abs_net_positive": bool(c10.get("event_net_atr_mean") is not None
                                         and c10["event_net_atr_mean"] > 0.0),
                "ci_lower_gt_0": bool(c10.get("event_net_atr_ci95") is not None
                                      and c10["event_net_atr_ci95"][0] > 0.0),
                "go_no_go_verdict": (tt.get("go_no_go", {}) or {}).get("verdict"),
            }
        pf_2x = results[(tf, "test", 2.0)][1]["profit_factor"]
        cells = {}
        for mech, v in es_checks.items():
            cells[mech] = ("PAPER_CANDIDATE" if (
                v["n_ge_60"] and v["abs_net_positive"] and v["ci_lower_gt_0"]
                and pf_2x is not None and pf_2x > 1.0) else "RESEARCH_ONLY")
        upgrades[tf] = {
            "criteria": es_checks,
            "backtest_test_2x_profit_factor": pf_2x,
            "backtest_test_2x_pf_gt_1": bool(pf_2x is not None and pf_2x > 1.0),
            "cells_verdict": cells,
            "tf_verdict": ("PAPER_CANDIDATE" if "PAPER_CANDIDATE" in cells.values()
                           else "RESEARCH_ONLY"),
        }
    overall = ("PAPER_CANDIDATE" if any(u["tf_verdict"] == "PAPER_CANDIDATE"
                                        for u in upgrades.values())
               else "RESEARCH_ONLY")

    REPORTS.mkdir(exist_ok=True)
    for tf in TF_LABELS:
        for seg in SEGMENTS:
            for mult in COST_MULTS:
                res = results[(tf, seg, mult)][0]
                suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
                trades_frame(res.trades).to_csv(
                    REPORTS / f"trades_v3_{tf}_{seg}{suffix}.csv", index=False)
                equity_frame(res).to_csv(
                    REPORTS / f"equity_v3_{tf}_{seg}{suffix}.csv", index=False)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v3-protocol.md(§1 机制 whitelist config/v2.json,"
                      "§3 判据与 v2 §3-4 逐字相同)",
            "meta": raw_cfg["meta"], "whitelist_v2": raw_cfg["whitelist_v2"],
            "k_is": k_is, "timeframes": {tf: agg.TF_HOURS[tf] for tf in TF_LABELS},
            "bar_semantics": "H/N_time 以根数计(bars 语义不变);funding 由 "
                             "broker 按 ts 差计提(dt/8h,TF 无关,长尺度持仓自动"
                             "如实计入)——回测侧无上游改动",
            "insufficient_data_precedence": "n<60 ⇒ §2 INSUFFICIENT_DATA,优先于 "
                                            "§3 升级判据,该格子不参与升级",
            "test_eval_declaration": "test 段第 5 次评估(v3 唯一一次;"
                                     "research-log 条目 7)",
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
            "funding": "资金费 = notional·rate·m·(持仓 ts 差)/8h,方向无关恒为成本",
            "liquidation": "近似强平保护 = 单仓 |浮亏| ≥ 0.9·equity_mtm 时下一根"
                           "开盘平仓",
        },
        "e7_counts": e7_counts,
        "event_study_v3_cross_check_ok": cross_ok,
        "upgrade_v3": {**upgrades, "overall_verdict": overall},
        "runs": {},
    }
    for tf in TF_LABELS:
        for seg in SEGMENTS:
            for mult in COST_MULTS:
                res, m = results[(tf, seg, mult)]
                payload["runs"][f"{tf}/{seg}@{mult}x"] = {"metrics": m,
                                                          "trades": res.trades}

    (REPORTS / "backtest_v3.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v3.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/backtest_v3.json/.md, trades_v3/equity_v3 CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    return dash if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render_md(payload: dict) -> str:
    runs = payload["runs"]
    cfg = payload["config"]

    def m(tf, seg, mult):
        return runs[f"{tf}/{seg}@{mult}x"]["metrics"]

    lines = [
        "# v3 回测报告(docs/v3-protocol.md:多尺度 × E7 白名单 + F1;§7/§8 全规则原样)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        f"- 机制(v3-protocol §1):whitelist={cfg['whitelist']},k_is="
        f"{cfg['k_is']}(F1 失效距离下限);r_min=1.5、结构目标、每 symbol 一仓、"
        "§8 六条出场、风险/成本模型全部同 v1/v2,先验零改动。",
        f"- 尺度语义:{payload['protocol']['bar_semantics']}。",
        f"- **{payload['protocol']['test_eval_declaration']}**:本报告为该次"
        "评估的回测侧产物。",
        f"- 资金模型:初始权益 {cfg['equity0']:.0f} USDT,risk_frac="
        f"{cfg['risk_frac']},leverage_cap={cfg['leverage_cap']}x;成本压力 "
        f"{cfg['cost_mults']};合约规格 {payload['contracts']['source']}(v1/v2 "
        "同缓存,离线强制,sha 校验)。",
        "- E7 发射/过 F1 计数与 event_study_v3 逐 (symbol, TF) 交叉核对:"
        f"{'全部一致' if payload['event_study_v3_cross_check_ok'] else '不一致(见 JSON)'}。",
        "",
        "## 1. v3-protocol §3 升级判据(§2 INSUFFICIENT_DATA 优先;八格完整公布)",
        "",
        "| TF | 方向 | n(test) | ① 绝对净>0 | ② CI下界>0 | ③ test@2x PF>1 | 格子结论 |",
        "|---|---|---|---|---|---|---|"]
    up = payload["upgrade_v3"]
    pf_shown: dict = {}
    for tf, u in up.items():
        if tf == "overall_verdict":
            continue
        pf = u["backtest_test_2x_profit_factor"]
        pf_shown[tf] = pf
        for mech, v in sorted(u["criteria"].items()):
            lines.append(
                f"| {tf} | {mech} | {v['test_n_events']} "
                f"| {'✓' if v['abs_net_positive'] else '✗'}"
                f"({_f(v['event_net_atr_1x_mean'], 4)}) "
                f"| {'✓' if v['ci_lower_gt_0'] else '✗'} "
                f"| {'✓' if u['backtest_test_2x_pf_gt_1'] else '✗'}"
                f"(PF={_f(pf)}) | **{u['cells_verdict'][mech]}**"
                f"{'(INSUFFICIENT: n<60)' if not v['n_ge_60'] else ''} |")
    lines.append(f"| **总体** | — | — | — | — | — | **{up['overall_verdict']}** |")

    for tf in TF_LABELS:
        lines += ["", f"## 2-{tf}. {tf} 段 × 成本压力总览(权益 300 USDT 起)", "",
                  "| 段 | 成本 | 净盈亏 | 收益率 | 年化 | 最大回撤% | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R | 资金费合计 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for seg in SEGMENTS:
            for mult in COST_MULTS:
                g = m(tf, seg, mult)
                lines.append(
                    f"| {seg} | {mult}x | {_f(g['net_pnl'])} "
                    f"| {_f(100 * g['return_pct'])}% "
                    f"| {_f(None if g['ann_return'] is None else 100 * g['ann_return'])}% "
                    f"| {_f(100 * g['max_dd_pct'])}% "
                    f"| {g['n_trades']} | {_f(g['winrate'], 3)} "
                    f"| {_f(g['profit_factor'])} | {_f(g['expectancy_usdt'], 4)} "
                    f"| {_f(g['expectancy_r'], 4)} | {_f(g['funding_total'], 4)} |")
        base = m(tf, "test", 1.0)
        lines += ["", f"### {tf} test@1x 分解", "",
                  "| direction | n | 净盈亏 | 期望值 | 胜率 | 持仓中位h |",
                  "|---|---|---|---|---|---|"]
        med = base["holding"]["median_h"]
        for d, v in sorted(base["by_direction"].items()):
            lines.append(f"| {d} | {v['n']} | {_f(v['net'])} "
                         f"| {_f(v['expectancy'], 4)} | {_f(v['winrate'], 3)} "
                         f"| {_f(med, 1)} |")

    liq_any = sum(m(tf, s, mm)["counters"]["liquidations"] for tf in TF_LABELS
                  for s in SEGMENTS for mm in COST_MULTS)
    lines += ["", "## 3. 破产风险与限制", "",
              f"- 强平触发合计(全部 {len(TF_LABELS)}×9 次回放):**{liq_any}**"
              "(0 为预期)。",
              "- 其余已知近似与 v1/v2 回测相同(当前合约规格代入历史、逐 bar 计提"
              "funding、近似强平;见 reports/backtest.md §8)。",
              "",
              "机器可读全量:`reports/backtest_v3.json`;逐笔:"
              "`reports/trades_v3_{tf}_*.csv`;逐 bar 权益:"
              "`reports/equity_v3_{tf}_*.csv`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
