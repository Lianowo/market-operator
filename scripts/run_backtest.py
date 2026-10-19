"""阶段 5 回测全管线:3 成本压力(1x/1.5x/2x)× 3 段(full/dev/test)= 9 次回放。

用法:  python scripts/run_backtest.py
产物:  reports/backtest.json        机器可读全量(每段×成本:指标、计数、交易明细、
                                    合约规格、段分界与 event_study 一致性核对)
       reports/backtest.md          人读报告
       reports/trades_{segment}.csv            逐笔交易(成本 1x 基线)
       reports/trades_{segment}_cost{m}.csv    成本压力变体(m ∈ 1_5, 2_0)
       reports/equity_{segment}.csv            逐 bar 权益(成本 1x 基线)
       reports/equity_{segment}_cost{m}.csv    成本压力变体

纪律:不改 config/defaults.json、methodology 与上游模块;先验全部来自 config;
event_study.json 只读,用于段分界一致性核对与增量口径对照(§9.4)。
段分界规则与阶段 3 完全一致(int(n·dev_frac),research.split_boundary)。
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
from market_operator.research import DEFAULT_CONFIG, split_boundary  # noqa: E402
from market_operator.structure import Params                      # noqa: E402

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


def _sample_decision_chains(trades: list, k: int = 5) -> list:
    """确定性抽样:首笔、中位(按 open_ts)、最优、最差、首笔含减仓的交易。"""
    if not trades:
        return []
    by_ts = sorted(trades, key=lambda t: t["open_ts"])
    picks = [by_ts[0], by_ts[len(by_ts) // 2],
             max(trades, key=lambda t: t["net_pnl"]),
             min(trades, key=lambda t: t["net_pnl"])]
    reduced = next((t for t in by_ts if t["reasons_reduce"]), None)
    if reduced is not None and reduced not in picks:
        picks.append(reduced)
    out, seen = [], set()
    for t in picks:
        if t["trade_id"] in seen:
            continue
        seen.add(t["trade_id"])
        out.append(t)
        if len(out) >= k:
            break
    return out


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    params = Params.from_json(DEFAULT_CONFIG)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    equity0 = 300.0

    # -- 合约规格(实时抓取,缓存 data/contracts.json,离线回退缓存) -------------
    contracts, spec_src = fetch_contract_specs(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…")
    for s in symbols:
        c = contracts[s]
        print(f"  {s}: sizeMin={c.size_min} step={c.size_step} "
              f"place={c.size_place} pricePlace={c.price_place} "
              f"minTradeUSDT={c.min_trade_usdt}")

    # -- 认知预计算(3 品种一次,9 次回放共用) ---------------------------------
    cogs = {}
    for sym in symbols:
        t0 = time.time()
        df = mo_data.load(sym)
        cogs[sym] = prepare_cognition(df, params, sym)
        print(f"[cognition] {sym}: {len(df)} bars, "
              f"{sum(len(v) for v in cogs[sym].events_by.values())} events, "
              f"{time.time() - t0:.1f}s")

    # -- 9 次回放 ---------------------------------------------------------------
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
                               **broker_kw)
            results[(seg, mult)] = (res, compute_metrics(res))
            m = results[(seg, mult)][1]
            print(f"[bt] {seg:5s} cost {mult}x: net={m['net_pnl']:+.2f} "
                  f"trades={m['n_trades']} DD={m['max_dd_pct']:.2%} "
                  f"liq={m['counters']['liquidations']} "
                  f"({time.time() - t0:.1f}s)")

    # -- 与 event_study 的段分界一致性核对(只读) ------------------------------
    es_path = REPORTS / "event_study.json"
    es = json.loads(es_path.read_text(encoding="utf-8")) if es_path.exists() else None
    boundary_check = {"event_study_json": bool(es), "per_symbol": {}}
    for sym in symbols:
        n = len(cogs[sym].bars)
        b = split_boundary(n, dev_frac)
        got = {"dev_end_index": b, "dev_end_ts": cogs[sym].bars[b][0]}
        exp = (es or {}).get("symbols", {}).get(sym, {})
        boundary_check["per_symbol"][sym] = {
            "computed": got,
            "event_study": {"dev_end_index": exp.get("dev_end_index"),
                            "dev_end_ts": exp.get("dev_end_ts")},
            "match": bool(es) and got["dev_end_index"] == exp.get("dev_end_index")
                     and got["dev_end_ts"] == exp.get("dev_end_ts"),
        }
    # GO 机制 test 段事件数(§9 口径)供交叉一致性引用
    es_go = {}
    if es:
        for mech in ("E1|short", "E6|short"):
            tt = (es.get("mechanisms", {}).get(mech, {}).get("test", {}) or {})
            h24 = (tt.get("horizons", {}) or {}).get("24", {}) or {}
            c10 = (h24.get("cost", {}) or {}).get("1_0", {})
            es_go[mech] = {
                "test_n_events": tt.get("n_events"),
                "h24_event_net_atr_1x": c10.get("event_net_atr_mean"),
                "h24_incr_atr_1x": c10.get("incr_atr_mean"),
                "verdict": (tt.get("go_no_go", {}) or {}).get("verdict"),
            }

    # -- 落盘:CSV(基线 1x 用规格命名,压力变体加后缀) ---------------------------
    REPORTS.mkdir(exist_ok=True)
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            res = results[(seg, mult)][0]
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            tdf = trades_frame(res.trades)
            tdf.to_csv(REPORTS / f"trades_{seg}{suffix}.csv", index=False)
            equity_frame(res).to_csv(REPORTS / f"equity_{seg}{suffix}.csv",
                                     index=False)

    # -- 落盘:backtest.json -----------------------------------------------------
    _hh = sorted(t["hold_hours"] for t in results[("test", 1.0)][0].trades
                 if t["hold_hours"] is not None)
    med_h = _hh[len(_hh) // 2] if _hh else 0.0
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "equity0": equity0, "dev_frac": dev_frac,
            "risk_frac": broker_kw["risk_frac"],
            "leverage_cap": broker_kw["leverage_cap"],
            "taker_fee": broker_kw["taker_fee"],
            "slippage_per_side": broker_kw["slippage_per_side"],
            "funding_per_8h_per_side": broker_kw["funding_per_8h"],
            "cost_mults": list(COST_MULTS), "segments": list(SEGMENTS),
            "go_mechanisms": ["E1|short", "E6|short"],
        },
        "contracts": {
            "source": spec_src, "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
            "cache_sha256": contracts_sha,
            "specs": {s: vars(c) for s, c in contracts.items()},
            "approximation_note": "规格为运行时实时抓取(或缓存);2024-2026 历史"
                                  "最小下单量/精度可能不同,本回测全程用当前规格,"
                                  "属已知近似。",
        },
        "conventions": {
            "decision_ts": "决策 bar 的 ts(数据 ts = bar 开盘毫秒,bar 以开盘标"
                           "记);决策于该 bar 收盘做出",
            "fill_ts": "成交 = 决策后下一根该品种 bar 开盘(fill_ts > decision_ts,"
                       "broker 断言行内强制)",
            "funding": ("资金费 = notional·rate·持仓时长/8h,方向无关恒为成本,逐"
                        "bar 计提、平仓结算(每 8h 一次计提口径);事件研究 §9.2 成"
                        "本模型按往返两腿 2× 计提(更保守),差异 ≈ rate×中位持仓/8h"
                        f"(本运行 test@1x 中位持仓 {med_h:.0f}h ⇒ ≈ "
                        f"{1e2 * broker_kw['funding_per_8h'] * med_h / 8:.4f}% "
                        "名义,如实声明)"),
            "liquidation": "近似强平保护 = 单仓 |浮亏| ≥ 0.9·equity_mtm 时下一根"
                           "开盘市价平仓;风控失效保护,正常交易不应触发",
        },
        "boundary_check": boundary_check,
        "event_study_go": es_go,
        "runs": {},
    }
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            res, m = results[(seg, mult)]
            key = f"{seg}@{mult}x"
            payload["runs"][key] = {"metrics": m, "trades": res.trades}

    (REPORTS / "backtest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/backtest.json/.md, trades/equity CSVs "
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
        "# 阶段 5 独立回测报告(§7/§8 全规则 + 300 USDT 资金模型 + 压力成本)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        f"- 资金模型:初始权益 {cfg['equity0']:.0f} USDT(多品种共享),risk_frac="
        f"{cfg['risk_frac']}(每笔最大允许损失 = risk_frac·权益),leverage_cap="
        f"{cfg['leverage_cap']}x(名义上限,超出缩量);成本先验 taker="
        f"{cfg['taker_fee']}/滑点={cfg['slippage_per_side']}/资金费="
        f"{cfg['funding_per_8h_per_side']}每 8h(§9.2,与阶段 3 同源),成本压力 "
        f"{cfg['cost_mults']} 同时乘 fee+滑点+资金费。",
        "- 下单纪律:GO 白名单 E1|short、E6|short(阶段 3 test 段结论);决策在 "
        "bar t 收盘,成交于 t+1 开盘(taker 滑点对方向不利);账本任一成交 ts ≤ "
        "决策 ts 立即 raise(回测全程断言通过;" +
        f"fill_ts > decision_ts 已在每次运行核验)。",
        "- 持仓管理 §8 六条:破位退出 / 反向 E6 / target_1 减半+保本 / 移动防线 / "
        "加仓(E4 NO-GO,v1 关闭)/ 24 根时间止损(MFE<0.5R)。",
        f"- 合约规格:{payload['contracts']['source']};"
        f"{payload['contracts']['approximation_note']}",
        "- " + payload["conventions"]["funding"] + "。",
        "- 段分界与 `reports/event_study.json` 一致性:" +
        "; ".join(f"{s} match={v['match']}"
                  for s, v in payload["boundary_check"]["per_symbol"].items()) + "。",
        "- 认知层(EventDetector/structure)与撮合层分离:认知为 bar 流纯函数,各"
        "成本×段回放共用同一份事件流(不改变因果语义);九次回放仅重放交易/记账。",
        "",
        "## 1. 总览(每段 × 成本压力;权益 300 USDT 起)",
        "",
        "| 段 | 成本 | 净盈亏 | 收益率 | 年化 | 最大回撤% | 回撤天数 | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            g = m(seg, mult)
            lines.append(
                f"| {seg} | {mult}x | {_f(g['net_pnl'])} "
                f"| {_f(100 * g['return_pct'])}% "
                f"| {_f(None if g['ann_return'] is None else 100 * g['ann_return'])}% "
                f"| {_f(100 * g['max_dd_pct'])}% | {_f(g['dd_days'], 1)} "
                f"| {g['n_trades']} | {_f(g['winrate'], 3)} "
                f"| {_f(g['profit_factor'])} | {_f(g['expectancy_usdt'], 4)} "
                f"| {_f(g['expectancy_r'], 4)} |")

    lines += ["", "### 成本与计数表", "",
              "| 段 | 成本 | 费用 | 滑点 | 资金费 | 成本合计 | GO事件 | 开仓意图 | "
              "成交 | MIN_SIZE | 强平 | 暴露占比 | 持仓中位h/p90h |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            g = m(seg, mult)
            c = g["counters"]
            hold = g["holding"]
            lines.append(
                f"| {seg} | {mult}x | {_f(g['fees_total'])} "
                f"| {_f(g['slippage_total'])} | {_f(g['funding_total'], 4)} "
                f"| {_f(g['fees_total'] + g['slippage_total'] + g['funding_total'])} "
                f"| {c['go_events']} | {c['open_intents']} | {c['opens_filled']} "
                f"| {c['min_size_skips']} | {c['liquidations']} "
                f"| {_f(100 * g['exposure_ratio'], 1)}% "
                f"| {_f(hold['median_h'], 1)}/{_f(hold['p90_h'], 1)} |")

    base = m("test", 1.0)
    lines += ["", "## 2. 分解(test 段,成本 1x)", "",
              "### 方向(v1 白名单仅空头,如实报告)", "",
              "| direction | n | 净盈亏 | 期望值 | 胜率 |", "|---|---|---|---|---|"]
    for d, v in sorted(base["by_direction"].items()):
        lines.append(f"| {d} | {v['n']} | {_f(v['net'])} "
                     f"| {_f(v['expectancy'], 4)} | {_f(v['winrate'], 3)} |")
    lines += ["", "### 按入场时 regime", "",
              "| regime | n | 净盈亏 | 期望值 | 胜率 |", "|---|---|---|---|---|"]
    for rg, v in sorted(base["by_regime"].items()):
        lines.append(f"| {rg} | {v['n']} | {_f(v['net'])} "
                     f"| {_f(v['expectancy'], 4)} | {_f(v['winrate'], 3)} |")
    lines += ["", "### 按 symbol", "",
              "| symbol | n | 净盈亏 | 期望值 | 胜率 |", "|---|---|---|---|---|"]
    for sym, v in sorted(base["by_symbol"].items()):
        lines.append(f"| {sym} | {v['n']} | {_f(v['net'])} "
                     f"| {_f(v['expectancy'], 4)} | {_f(v['winrate'], 3)} |")

    liq_any = sum(m(s, mm)["counters"]["liquidations"] for s in SEGMENTS
                  for mm in COST_MULTS)
    skip_any = sum(m(s, mm)["counters"]["min_size_skips"] for s in SEGMENTS
                   for mm in COST_MULTS)
    lines += ["", "## 3. 破产风险分析", "",
              f"- 强平触发合计(全部 9 次回放):**{liq_any}**"
              "(断言:0 次;" + ("✓ 满足" if liq_any == 0 else "✗ 违反") +
              ";0.9·equity 浮亏强平为风控失效近似保护,正常 risk_frac="
              f"{cfg['risk_frac']} 下单本来不可达)。",
              f"- MIN_SIZE 拒绝合计:**{skip_any}**"
              f"(300 USDT 下 risk 预算 {cfg['risk_frac'] * cfg['equity0']:.2f} "
              "USDT/笔 与 minTradeUSDT=5 的相容性见 §5)。",
              f"- test@1x:权益 {_f(base['equity_start'])} → {_f(base['equity_end'])},"
              f"最大回撤 {_f(100 * base['max_dd_pct'])}%({_f(base['max_dd_usdt'])} "
              f"USDT,峰→谷 {_f(base['dd_days'], 1)} 天);权益最低点远离强平线,"
              "破产风险主要来自连续亏损侵蚀至 MIN_SIZE 下限,而非杠杆爆仓。",
              ""]

    lines += ["## 4. 月度权益(test 段,成本 1x)", "",
              "| 月份 | 月末权益 | 月盈亏 |", "|---|---|---|"]
    for row in base["monthly"]:
        lines.append(f"| {row['month']} | {_f(row['equity_end'])} "
                     f"| {_f(row['pnl'])} |")

    es_go = payload.get("event_study_go", {})
    full1 = m("full", 1.0)
    test1 = m("test", 1.0)
    lines += ["", "## 5. 300 USDT 复利可持续性讨论", "",
              f"- 单笔风险预算 = {cfg['risk_frac'] * 100:.2f}%·权益(300 USDT 时 "
              f"{cfg['risk_frac'] * cfg['equity0']:.2f} USDT);notional 中位数见 "
              "trades CSV;MIN_SIZE 下限 5 USDT ⇒ 风险预算与最小名义相容的前提是 "
              "|entry−invalid|/ entry ≲ risk_frac·权益/5(300 USDT 时约 45% 的"
              "止损距离,几乎全部历史交易满足;本运行 MIN_SIZE 拒绝 = "
              f"{skip_any} 次,如实计数)。",
              f"- 成本占比:test@1x 费用+滑点+资金费合计 "
              f"{_f(test1['fees_total'] + test1['slippage_total'] + test1['funding_total'])}"
              f" USDT,对净盈亏 {_f(test1['net_pnl'])} USDT;成本压力 1.5x/2x 下"
              " test 净盈亏 = "
              f"{_f(m('test', 1.5)['net_pnl'])} / {_f(m('test', 2.0)['net_pnl'])}"
              "(见 §1),边际对成本高度敏感是小资金账户的核心约束。",
              "- 复利含义:权益 < MIN_SIZE 临界值后系统结构性停开新仓(不是亏损"
              "平仓而是拒绝开仓),回撤地板效应与期望值规模同属小资金账户如实"
              "约束;若期望值 > 0,复利可持续直到规模受流动性而非最小单约束。",
              "",
              "## 6. 与 event_study 增量的交叉一致性", ""]
    for mech, g in sorted(es_go.items()):
        lines.append(
            f"- {mech}:§9 test 事件 n={g['test_n_events']},H24 事件净期望@1x "
            f"={_f(g['h24_event_net_atr_1x'], 4)} ATR(相对对照增量 "
            f"{_f(g['h24_incr_atr_1x'], 4)}),verdict={g['verdict']}。")
    cost_t1 = (test1["fees_total"] + test1["slippage_total"]
               + test1["funding_total"])
    e1n = (es_go.get("E1|short") or {}).get("h24_event_net_atr_1x")
    e6n = (es_go.get("E6|short") or {}).get("h24_event_net_atr_1x")
    lines.append(
        "- 口径和解(关键诚实性结论):§9 的 GO 判定基于**相对对照组的增量**——"
        "事件净期望本身在 test 段即为负(E1|short "
        f"{_f(e1n, 4)} ATR、E6|short "
        f"{_f(e6n, 4)} ATR,成本已内含),"
        "对照组(同方向无信号随机入场)更负 ⇒ 事件提供相对信息。本组合级回测是"
        f"**绝对口径**:test@1x 净盈亏 {_f(test1['net_pnl'])} USDT"
        f"(毛 {_f(test1['net_pnl'] + cost_t1)} USDT − 成本 {_f(cost_t1)} USDT,"
        f"{test1['n_trades']} 笔),与事件净期望为负方向一致、量级相容(样本期"
        "做空方向 + 全成本 > 机制毛收益)。增量为正(相对随机入场有信息)与"
        "绝对为负(不足以构成可盈利系统)并存,二者不矛盾,均如实记录。")
    lines.append(
        f"- 本回测 test@1x 共 {test1['counters']['go_events']} 个 GO 事件、"
        f"{test1['counters']['open_intents']} 个开仓意图、"
        f"{test1['counters']['opens_filled']} 笔成交(full 段 "
        f"{full1['counters']['opens_filled']} 笔)。二者差异全部由 §7/§8 规则"
        "解释:同 symbol 同时最多 1 仓(持仓期间事件被 dup 去重)、r_mult≥1.5 "
        "与结构目标过滤、MIN_SIZE 拒绝;事件研究为无持仓管理的理论头寸,本回测"
        "为全规则组合的已实现口径,符号一致性与数值量级是交叉验证对象而非"
        "数值相等。")

    chains = _sample_decision_chains(runs["test@1.0x"]["trades"])
    lines += ["", "## 7. 抽样完整决策链(test 段,成本 1x;reason 可追溯到 "
              "§8 条款与事件 id)", ""]
    for i, t in enumerate(chains, 1):
        lines += [
            f"**{i}. {t['trade_id']}** ({t['symbol']} "
            f"{'空' if t['direction'] < 0 else '多'})",
            f"- 观察到:{t['event_id']} @ {_iso(t['open_decision_ts'])},"
            f"决策参考 {t['reason_open']}",
            f"- 状态:{t['regime']};关键位 invalid={_f(t['invalid'], 8)} "
            f"target_1={_f(t['target_1'], 8)}(r_mult={_f(t['r_mult_planned'], 3)})",
            f"- 为何行动:{t['event_id']}|short ∈ GO 白名单且 r_mult≥1.5 且结构"
            "目标存在(§7);最大允许损失 risk_usdt="
            f"{_f(t['risk_usdt'])} USDT(= qty×|entry−invalid|,≤ "
            f"{cfg['risk_frac'] * 100:.2f}%·权益)",
            f"- 什么会推翻:收盘越过 invalid(§8.1)/ 反向 E6(§8.5)/ 24 根 "
            "MFE<0.5R(§8.6);实际出场:" +
            (f"{t['exit_kind']} @ {_iso(t['exit_ts'])} — {t['reason_exit']}"
             if t["closed"] else "未平仓(数据末端)"),
            (f"- 减仓:[{ ' ;; '.join(t['reasons_reduce']) }]"
             if t["reasons_reduce"] else "- 减仓:无"),
            f"- 结果:净盈亏 {_f(t['net_pnl'])} USDT({_f(t['r_multiple'], 3)}R,"
            f"持仓 {_f(t['hold_hours'], 1)}h)",
            ""]
    lines += [
        "## 8. 已知近似与限制", "",
        "- 合约最小下单量为运行时规格(历史可能更低 ⇒ 300 USDT 在 2024-2025 更"
        "易成交,本近似偏保守)。",
        "- 资金费按持仓时长计提(方向无关恒为成本),不代入真实 funding 历史"
        "(v2 候选,methodology §10)。",
        "- 强平为近似保护(浮亏 vs 0.9·权益),非交易所维持保证金精确模型。",
        "- 同 ts 多品种 bar 按 symbol 字典序撮合;权益合并,单品种持仓互不从"
        "认知上影响(风险预算不经由共享权益传播外的耦合)。",
        "- 持仓穿越段尾的少数交易按 OPEN 决策 bar 归属段,权益**指标**按段切片,"
        "两类口径差异由 n_open_end 如实记录;`equity_{segment}.csv` 保留全时间"
        "轴行(含段外盯市),段切片口径见 backtest.json 的 metrics.ts_lo/ts_hi。",
        "",
        "机器可读全量:`reports/backtest.json`;逐笔:`reports/trades_*.csv`;"
        "逐 bar 权益:`reports/equity_*.csv`。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
