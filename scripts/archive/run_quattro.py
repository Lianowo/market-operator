"""v7 对照实验运行管线:quattro 逐字复刻(docs/v7-protocol.md,冻结)。

用法:  python scripts/run_quattro.py
地位:  **对照实验,非候选语义**(research-log #13;v7-protocol §2:不重置、
       不调参、不优化;结果只裁决"同主场同周期照抄规则的正/负")。
配置:  成本/杠杆/合约规格沿用 config/canonical.json 先验(taker 0.06%/
       滑点 0.02%/资金费 0.01%每8h/cap 5.0,methodology §9.2);风险分位按
       quattro 规则 = 2%/单元;不加组合停(quattro 自带 5% 硬强平)。
产物:  reports/quattro.json / quattro.md
       reports/quattro_trades.csv(逐金字塔序列)
       reports/quattro_equity.csv(逐 4h bar 盯市)
纪律:  合约规格离线缓存强制(同 v6);4h 帧 sha256 对账 manifest;确定性
       双跑逐字段一致才落盘;broker 强平计数必须为 0。
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import aggregate as agg                     # noqa: E402
from market_operator import data as mo_data                      # noqa: E402
from market_operator import quattro                              # noqa: E402
from market_operator.broker import CONTRACTS_PATH, Broker, ContractSpec  # noqa: E402

CONFIG_CANONICAL = ROOT / "config" / "canonical.json"
REPORTS = ROOT / "reports"
SYMBOL = quattro.SYMBOL
EQUITY0 = 300.0

# 上游 README §3 自报参照(仅对照展示,非复现目标;数据窗 4.1 年 ≠ 本地 2 年)
UPSTREAM_REF = {
    "window": "2022-01→2026-05 (4.1 年;本地 2 年,2024-10→2026-10)",
    "total_return": "+1107%", "apy": "+83%", "max_dd": "-37.5%",
    "trades": 94, "winrate": "26.6%",
    "avg_win_r": "+13.4R", "avg_loss_r": "-2.0R",
    "exit_split": {"hard_stop_5pct": 0.40, "donchian_exit": 0.36,
                   "atr_stop_trailing": 0.23},
}

MAPPINGS = [
    "加梯:wick 触及 trigger ⇒ 次 4H 开盘市价成交(协议 §1;upstream 为 "
    "bar 内 trigger 价成交且无成本)",
    "wick 止损 = stop-market 当 bar 成交:min(open, stop)·(1−滑点);"
    "upstream 恒按 stop 价(缺口处略乐观)且无滑点/费用",
    "站立止损位 = max(单元棘轮, 5% 硬平层级);同 bar 贯穿两级记 "
    "hard_stop_5pct(upstream 同评估顺序);仅当 hard 层级更深且同 bar "
    "触及时成交价两层级不同(本复刻取较高站立位),4 单元时重合无差异",
    "杠杆 cap = 系统 5.0(upstream 20);cap 区 ADD 由 broker 缩量成交"
    "(upstream 决策侧整单元不加);300 USDT min_size 拒单如实记",
    "每日 EMA200 由本地 4h 聚合帧 UTC 聚合,只用已完成日;本地窗口起点"
    "播种(无窗口前历史;upstream 预热自起始前 1 年),属数据窗差异",
    "数据末棒仍在场 ⇒ 末棒开盘强平记 still_open_at_end(含成本;"
    "upstream 末棒收盘强平、无成本)",
]


def load_contracts_cache(symbols: list) -> tuple:
    payload = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    specs = {}
    for s in symbols:
        ent = payload["entries"][s]
        ent_sha = hashlib.sha256(json.dumps(
            ent["raw"], sort_keys=True).encode("utf-8")).hexdigest()
        if ent_sha != ent["sha256"]:
            raise RuntimeError(f"contracts cache sha256 mismatch for {s}")
        specs[s] = ContractSpec.from_entry(ent["raw"])
    return specs, (f"cache(offline enforced, "
                   f"cached_at={payload['fetched_at_utc']})")


def _iso(ts) -> str:
    return datetime.fromtimestamp(int(ts) / 1000,
                                  tz=timezone.utc).isoformat(timespec="seconds")


def make_broker(cfg: dict, contracts: dict) -> Broker:
    return Broker(
        equity=EQUITY0,
        risk_frac=quattro.RISK_PCT,                      # quattro 规则:2%/单元
        leverage_cap=float(cfg["leverage_cap"]),         # 沿用系统 cap(5.0)
        taker_fee=float(cfg["taker_fee"]),
        slippage_per_side=float(cfg["slippage_per_side"]),
        funding_per_8h=float(cfg["funding_per_8h_per_side"]),
        contracts=contracts,
        cost_multiplier=1.0,
    )


TRADE_COLUMNS = [
    "seq_id", "trade_id", "signal_ts", "signal_iso", "entry_ts", "entry_iso",
    "exit_ts", "exit_iso", "first_entry_price", "avg_entry_price",
    "exit_price", "num_units", "total_size_btc", "original_atr",
    "acct_at_entry", "first_unit_risk", "total_risk", "gross_pnl", "fees",
    "funding", "pnl_usd", "pnl_pct", "R_total", "duration_h", "exit_reason",
    "account", "used_leverage_max", "add_min_size_skips", "add_clipped_fills",
]


def trades_frame(sequences: list) -> pd.DataFrame:
    rows = [{**s, "signal_iso": _iso(s["signal_ts"]),
             "entry_iso": _iso(s["entry_ts"]), "exit_iso": _iso(s["exit_ts"])}
            for s in sequences]
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def equity_frame(equity_rows: list) -> pd.DataFrame:
    return pd.DataFrame(
        [(ts, _iso(ts), eq, cash, upnl, fnd)
         for ts, eq, cash, upnl, fnd in equity_rows],
        columns=["ts", "iso", "equity", "cash", "unrealized",
                 "funding_accrued"])


def main() -> None:
    cfg = json.loads(CONFIG_CANONICAL.read_text(encoding="utf-8"))
    contracts, spec_src = load_contracts_cache([SYMBOL])
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()

    df4h = mo_data.load(SYMBOL, "4H")
    manifest = json.loads(
        (ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    mrec = manifest["files"][f"{SYMBOL}_4h.csv"]
    file_sha = hashlib.sha256(
        (ROOT / "data" / "raw" / f"{SYMBOL}_4h.csv").read_bytes()).hexdigest()
    # 完整性链:锚定带哈希 1h 帧 → 重聚合 4h → ts/OHLC 逐位相等(本仓聚合帧
    # manifest 字节 sha 因 volume 浮点求和序跨环境漂移全部失配 —— 既有既有
    # 现象,OHLC 语义无损;v6 运行脚本的对账同样只核 OHLC)。volume 差阈
    # 1e-6 absolute 之上即视为真损坏。
    m1 = manifest["files"][f"{SYMBOL}_1h.csv"]
    sha_1h = hashlib.sha256(
        (ROOT / "data" / "raw" / f"{SYMBOL}_1h.csv").read_bytes()).hexdigest()
    if sha_1h != m1["sha256"]:
        raise RuntimeError(f"1h anchor frame sha256 != manifest: {sha_1h}")
    re4 = agg.aggregate(mo_data.load(SYMBOL, "1H"), 4)
    if not (re4["ts"].to_numpy() == df4h["ts"].to_numpy()).all():
        raise RuntimeError("4h frame ts != re-aggregation of hashed 1h")
    for col in ("open", "high", "low", "close"):
        if not (re4[col].to_numpy() == df4h[col].to_numpy()).all():
            raise RuntimeError(f"4h frame {col} != re-aggregation of hashed 1h")
    vol_max_abs = float(
        abs(re4["volume"].to_numpy() - df4h["volume"].to_numpy()).max())
    if vol_max_abs > 1e-6:
        raise RuntimeError(f"4h volume drift beyond float noise: {vol_max_abs}")
    sha_matches_manifest = file_sha == mrec["sha256"]
    integrity = {
        "file_sha256": file_sha,
        "manifest_sha256": mrec["sha256"],
        "sha_matches_manifest_bytes": sha_matches_manifest,
        "anchor": "BTCUSDT_1h.csv bytes == manifest sha256(已核验)",
        "semantic_check": "4h ts/OHLC 与锚定 1h 重聚合逐位一致;"
                          "volume 最大绝对差 = "
                          f"{vol_max_abs:.3g}(浮点求和序噪声,聚合帧 manifest "
                          "字节 sha 失配为全仓既有现象,如实声明)",
    }
    issues = mo_data.validate(df4h, step_ms=quattro.STEP_4H_MS)
    win = {"ts_first": int(df4h["ts"].iloc[0]),
           "ts_last": int(df4h["ts"].iloc[-1]),
           "iso_first": _iso(df4h["ts"].iloc[0]),
           "iso_last": _iso(df4h["ts"].iloc[-1]),
           "rows": int(len(df4h))}

    def _run() -> dict:
        return quattro.run(df4h, make_broker(cfg, contracts), SYMBOL)

    res_a = _run()
    res_b = _run()
    dump_a = json.dumps(res_a["sequences"], sort_keys=True)
    dump_b = json.dumps(res_b["sequences"], sort_keys=True)
    deterministic = dump_a == dump_b and res_a["equity_rows"] == res_b["equity_rows"]
    if not deterministic:
        raise RuntimeError("quattro 双跑不一致(非确定性)")
    broker = make_broker(cfg, contracts)          # 计数器以正式运行为准
    res = quattro.run(df4h, broker, SYMBOL)
    if json.dumps(res["sequences"], sort_keys=True) != dump_a:
        raise RuntimeError("quattro 三跑不一致(非确定性)")
    if broker.n_liquidations != 0:
        raise RuntimeError(f"broker 强平非预期触发: {broker.n_liquidations}")

    m = quattro.summarize(res, EQUITY0)
    counters = res["counters"]
    print(f"[quattro] sequences={m['n_sequences']} units={m['n_unit_fills']} "
          f"final={m['final_equity']:.2f} ret={m['return_pct']:+.1%} "
          f"dd_mtm={m['max_dd_mtm']:.1%} pf={m['profit_factor']:.2f} "
          f"wr={m['winrate']:.1%}")
    print(f"[exits] {counters['sequences']} seq: "
          + " ".join(f"{k}={v}" for k, v in m["exit_counts"].items())
          + f" other={m['exit_other']} | skips={counters['broker_skips']}")

    REPORTS.mkdir(exist_ok=True)
    trades_frame(res["sequences"]).to_csv(REPORTS / "quattro_trades.csv",
                                          index=False)
    equity_frame(res["equity_rows"]).to_csv(REPORTS / "quattro_equity.csv",
                                            index=False)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "对照实验(docs/v7-protocol.md):quattro 逐字复刻,"
                  "非 market-operator 候选语义;不主张复现 upstream +1107%",
        "protocol": {
            "doc": "docs/v7-protocol.md(冻结;research-log #13 先行登记)",
            "rule_source": "upstream/crypto_systematic_research/2_strategies/"
                           "01_quattro_donchian README §2(MIT;backtest.py "
                           "为语义澄清器,未复制其代码)",
            "verdict_scope": "同 venue/成本/数据窗(2 年)照抄规则之正负;"
                             "test/dev 曝光纪律不适用(对照实验),不追调",
        },
        "data": {
            "frame": "data/raw/BTCUSDT_4h.csv(1h 锚定聚合,manifest 记录)",
            "integrity": integrity,
            "validation_issues": issues, "window": win,
            "window_declaration": "本地 2 年(2024-10→2026-10)vs upstream "
                                  "4.1 年(2022-01→2026-05);EMA200 自窗口起"
                                  "点播种(upstream 预热含窗口前 1 年)— 覆"
                                  "盖/预热成熟度差异如实声明",
        },
        "config": {
            "equity0": EQUITY0,
            "risk_frac_per_unit": quattro.RISK_PCT,
            "hard_stop_pct": quattro.HARD_STOP_PCT,
            "leverage_cap": float(cfg["leverage_cap"]),
            "taker_fee": float(cfg["taker_fee"]),
            "slippage_per_side": float(cfg["slippage_per_side"]),
            "funding_per_8h_per_side": float(cfg["funding_per_8h_per_side"]),
            "cost_multiplier": 1.0,
            "cost_source": "config/canonical.json(methodology §9.2 先验,"
                           "系统同模型)",
        },
        "contracts": {
            "source": spec_src, "cache_sha256": contracts_sha,
            "spec": vars(contracts[SYMBOL]),
        },
        "execution_mappings": MAPPINGS,
        "upstream_reference": UPSTREAM_REF,
        "results": m,
        "counters": counters,
        "determinism": {"double_run_identical": True,
                        "sequences_sha256": hashlib.sha256(
                            dump_a.encode()).hexdigest()},
        "sequences": res["sequences"],
    }
    (REPORTS / "quattro.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "quattro.md").write_text(render_md(payload), encoding="utf-8")
    print("-> reports/quattro.json/.md, quattro_trades.csv, "
          "quattro_equity.csv", flush=True)


def _f(x, nd=2, dash="—"):
    if x is None or (isinstance(x, float) and x != x):
        return dash
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def render_md(p: dict) -> str:
    m = p["results"]
    c = p["counters"]
    win = p["data"]["window"]
    ref = p["upstream_reference"]
    lines = [
        "# v7 对照实验报告:quattro 逐字复刻(BTCUSDT 4H)",
        "",
        f"生成时间(UTC):{p['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明",
        "",
        f"- **{p['status']}**(research-log #13;protocol: {p['protocol']['doc']}).",
        f"- 规则准据:{p['protocol']['rule_source']};零改动、零调参、零优化。",
        f"- 判定口径:{p['protocol']['verdict_scope']}。",
        f"- 数据窗:**{win['iso_first']} → {win['iso_last']}({win['rows']} "
        f"根 4h,本地 2 年)**;{p['data']['window_declaration']}",
        "- 成本:系统同模型(taker 0.06%/边 + 滑点 0.02%/边 + 资金费 "
        "0.01%/8h 按持仓时长;upstream backtest.py 零成本)— 成本口径"
        "不同,结果不可直接对比其 +1107%。",
        "",
        "## 1. 执行映射(venue 落地,非规则改动)",
        "",
    ]
    for i, mp in enumerate(p["execution_mappings"], 1):
        lines.append(f"{i}. {mp}。")

    lines += [
        "",
        "## 2. 总览(300 USDT 起;净额)与 upstream 自报参照",
        "",
        "| 指标 | 本复刻(2 年) | upstream 自报(4.1 年) |",
        "|---|---|---|",
        f"| 序列数 | {m['n_sequences']} | {ref['trades']} |",
        f"| 单元成交数(均/序列) | {m['n_unit_fills']}({_f(m['avg_units'])}) | — |",
        f"| 期末权益 | {_f(m['final_equity'])} USDT | $22,196(自 $1,839) |",
        f"| 总收益率 | {m['return_pct']:+.1%} | {ref['total_return']} |",
        f"| 最大回撤(逐 bar 盯市) | {m['max_dd_mtm']:+.1%} | — |",
        f"| 最大回撤(逐序列平仓,upstream 口径) | {m['max_dd_closed']:+.1%} "
        f"| {ref['max_dd']} |",
        f"| 胜率 | {m['winrate']:.1%} | {ref['winrate']} |",
        f"| 盈亏因子 PF | {_f(m['profit_factor'])} | — |",
        f"| 期望 R / 平均胜 R / 平均负 R | {_f(m['expectancy_r'], 3)} / "
        f"{_f(m['avg_win_r'])} / {_f(m['avg_loss_r'])} | — / "
        f"{ref['avg_win_r']} / {ref['avg_loss_r']} |",
        f"| 持仓时长中位(h) | {_f(m['median_duration_h'], 1)} | 3.2 天(均) |",
        f"| 费用合计 / 资金费合计 | {_f(m['fees_total'])} / "
        f"{_f(m['funding_total'])} USDT | 0(无成本) |",
        "",
        "> 数据窗/成本/venue 三重差异:**不主张复现 upstream +1107%**;"
        "本表只回答『同主场同周期,照抄规则是正/是负』。",
        "",
        "## 3. Exit 三桶(vs upstream 40/36/23)",
        "",
        "| 出场原因 | 本复刻笔数 | 占比 | upstream 占比 |",
        "|---|---|---|---|",
    ]
    for r in ("hard_stop_5pct", "donchian_exit", "atr_stop_trailing"):
        lines.append(f"| {r} | {m['exit_counts'][r]} "
                     f"| {m['exit_share'][r]:.1%} "
                     f"| {ref['exit_split'][r]:.0%} |")
    if m["exit_other"]:
        lines.append(f"| still_open_at_end | {m['exit_other']} | — | — |")
    lines += [
        "",
        "## 4. 逐序列明细",
        "",
        f"全量机器可读:reports/quattro_trades.csv({m['n_sequences']} 行);"
        "逐 bar 盯市:reports/quattro_equity.csv。",
        "",
        "| # | 入场(UTC) | 出场(UTC) | 单元 | N(ATR14) | 均价 | 出场价 "
        "| 净盈亏 | R | 出场原因 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in p["sequences"]:
        lines.append(
            f"| {s['seq_id']} | {_iso(s['entry_ts'])} | {_iso(s['exit_ts'])} "
            f"| {s['num_units']} | {_f(s['original_atr'], 1)} "
            f"| {_f(s['avg_entry_price'], 1)} | {_f(s['exit_price'], 1)} "
            f"| {s['pnl_usd']:+.2f} | {s['R_total']:+.2f} "
            f"| {s['exit_reason']} |")

    lines += [
        "",
        "## 5. Sizing / 拒单 / 杠杆(Bitget 规格,300 USDT)",
        "",
        f"- 合约规格:{p['contracts']['source']};"
        f"size_min={p['contracts']['spec']['size_min']} "
        f"step={p['contracts']['spec']['size_step']} "
        f"min_notional={p['contracts']['spec']['min_trade_usdt']} USDT。",
        f"- 入场信号 {c['entry_signals']} 次;入场 MIN_SIZE 拒单 "
        f"{c['entry_min_size_skips']};加梯 MIN_SIZE 拒单 "
        f"{c['add_min_size_skips']};加梯被杠杆 cap 缩量 "
        f"{c['add_clipped_fills']};broker 强平 {c['liquidations']}"
        "(必须为 0)。",
        f"- broker 拒单明细:{json.dumps(c['broker_skips'], ensure_ascii=False)}。",
        "",
        "## 6. 确定性与对账",
        "",
        f"- 确定性三跑逐字段一致:sequences sha256 = "
        f"{p['determinism']['sequences_sha256'][:16]}…。",
        f"- 完整性链:{p['data']['integrity']['anchor']};"
        f"{p['data']['integrity']['semantic_check']};"
        f"validate 问题 {len(p['data']['validation_issues'])} 条。",
        "- 合约规格离线缓存 sha256 逐条目校验通过(同 v6 纪律)。",
        "",
        "## 7. 已知近似与残余差异",
        "",
        "① EMA200 可见性 = 最近完成日(不偷看当日收盘);窗口起点播种,"
        "约首 200 个完成日内 EMA 未熟(upstream 预热自窗口前 1 年),属数"
        "据窗差异;② 首/末 UTC 日为不完整聚合桶(如实保留);③ wick 止损"
        "缺口成交 = min(open, stop)(保守)+滑点,upstream 恒 stop 价;④ "
        "hard<棘轮且同 bar 贯穿时本复刻按棘轮位成交(upstream 按 hard "
        "位),标签口径一致;⑤ 杠杆 cap 5.0(系统)≠20(上"
        "游),cap 命中如实入账;⑥ 末棒开盘强平(含成本)代 upstre"
        "am 末棒收盘(无成本)。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
