"""只读对账:Bitget 实盘的真实手续费 / 资金费 vs `config/strategy.json` 的成本假设。

只读端点,不发任何下单/撤单:
  - /api/v2/mix/market/contracts  (公开:合约 taker/makerFeeRate)
  - /api/v2/mix/order/fills       (签名:逐笔成交,feeDetail[].totalFee,tradeScope=taker/maker)
  - /api/v2/mix/account/bill      (签名:businessType=contract_settle_fee 为资金费)

口径:
  - fee bps = |ΣfeeDetail.totalFee| / quoteVolume × 1e4(每边)
  - 资金费 bps/8h = Σ funding(USDT) / (Σ notional·holding_hours / 8) × 1e4
    (notional·hours 由 fill 重建的开→平区间得到;正 funding = 收取)

用法: python scripts/live_cost_audit.py [--days 30]
输出: stdout 摘要 + reports/live_cost_audit_20261023/AUDIT.md + audit.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping, Sequence
import csv
import json
import os
from pathlib import Path
import random
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import paths                                        # noqa: E402
from market_operator.bitget_live import BitgetError, BitgetLiveClient    # noqa: E402
from market_operator.secrets_loader import SecretsError, load_secrets    # noqa: E402
from market_operator.timeframes import MS_PER_DAY, MS_PER_HOUR           # noqa: E402

MS_DAY = MS_PER_DAY
MS_H = MS_PER_HOUR
BPS_SCALE = 10_000.0
FUNDING_HOURS_PER_WINDOW = 8.0
QTY_EPS = 1e-12
# 审计产物是"报告",归运行期类别:`reports/` 在 portable 布局下落到 var/reports/
OUT_DIR = paths.resolve("reports") / "live_cost_audit_20261023"
_REQUIRED_BITGET_KEYS = ("BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE")


def cfg_costs() -> dict[str, Any]:
    cfg = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    return {
        k: cfg.get(k)
        for k in (
            "taker_fee",
            "maker_fee",
            "slippage_per_side",
            "funding_per_8h_per_side",
        )
    }


def _page_bills(
    cli: BitgetLiveClient,
    start: int,
    end: int,
    btype: str | None,
    cap: int = 1000,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = end
    while len(out) < cap:
        data = cli.account_bills(
            start_ms=start,
            end_ms=cursor,
            business_type=btype,
            limit=100,
        )
        rows = data.get("bills") or []
        if not rows:
            break
        out.extend(rows)
        oldest = min(int(r.get("cTime") or 0) for r in rows)
        if oldest <= start or oldest >= cursor:
            break
        cursor = oldest - 1
    return out


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_fills(raw: Any) -> list[dict[str, Any]]:
    rows = raw.get("fillList") if isinstance(raw, Mapping) else raw
    out: list[dict[str, Any]] = []
    for f in rows or []:
        try:
            px = float(f.get("price") or 0)
            qty = float(f.get("baseVolume") or 0)
        except (TypeError, ValueError):
            continue
        qv = _safe_float(f.get("quoteVolume")) or px * qty
        fee = sum(abs(_safe_float(d.get("totalFee"))) for d in (f.get("feeDetail") or []))
        ts = int(f.get("cTime") or 0)
        out.append({
            "ts": ts,
            "symbol": f.get("symbol"),
            "qty": qty,
            "price": px,
            "quote": qv,
            "fee": fee,
            "scope": str(f.get("tradeScope") or "?").lower(),
            "side": str(f.get("side") or "?").lower(),
            "trade_side": str(f.get("tradeSide") or "?").lower(),
            "profit": _safe_float(f.get("profit")),
        })
    out.sort(key=lambda r: r["ts"])
    return out


def _market_funding(
    cli: BitgetLiveClient,
    symbols: Sequence[str],
    days: int,
) -> dict[str, list[float]]:
    """交易所公开的资金费率历史(每 8h 一次),用于检验 1bp/8h 的假设。"""
    out: dict[str, list[float]] = {}
    cutoff = int(time.time() * 1000) - days * MS_DAY
    for sym in symbols:
        try:
            data = cli.funding_rate_history(sym, page_size=100, page_no=1)
        except BitgetError as exc:
            print(f"[market funding] skip {sym}: {exc}")
            continue
        rates: list[float] = []
        for row in data:
            ts = int(_safe_float(row.get("fundingTime")))
            if ts < cutoff:
                continue
            rates.append(_safe_float(row.get("fundingRate")))
        if rates:
            out[sym] = rates
    return out


def _pct(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def _ledger_sensitivity(path: Path) -> dict[str, dict[str, Any]]:
    """在 a0 账本上重算:资金费若按实测(≈0)计,各周期净 E[R] 变化多少。"""
    per: dict[str, dict[str, float]] = {}
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("closed") not in ("True", "1") or r.get("in_segment_open") not in ("True", "1"):
                continue
            try:
                risk = float(r["risk_usdt"])
                notional = float(r["notional"])
                hold = float(r["hold_hours"])
            except (KeyError, TypeError, ValueError):
                continue
            if risk <= 0:
                continue
            tf = r["tf"]
            d = per.setdefault(
                tf,
                {
                    "n": 0,
                    "gross": 0.0,
                    "fee": 0.0,
                    "fund": 0.0,
                    "slip": 0.0,
                    "net": 0.0,
                    "nh": 0.0,
                    "nhr": 0.0,
                    "npr": 0.0,
                },
            )
            d["n"] += 1
            d["gross"] += float(r["gross_pnl"]) / risk
            d["fee"] += float(r["fees"]) / risk
            d["fund"] += float(r["funding"]) / risk
            d["slip"] += float(r["slippage_cost"]) / risk
            d["net"] += float(r["net_pnl"]) / risk
            d["nh"] += notional * (hold / FUNDING_HOURS_PER_WINDOW)
            d["nhr"] += (notional / risk) * (hold / FUNDING_HOURS_PER_WINDOW)
            d["npr"] += notional / risk
    out: dict[str, dict[str, Any]] = {}
    for tf, d in per.items():
        n = int(d["n"]) or 1
        out[tf] = {
            "n": int(d["n"]),
            "gross": round(d["gross"] / n, 4),
            "fee": round(d["fee"] / n, 4),
            "fund": round(d["fund"] / n, 4),
            "slip": round(d["slip"] / n, 4),
            "net": round(d["net"] / n, 4),
            "net_no_fund": round((d["net"] - d["fund"]) / n, 4),
            "notional_per_R": round(d["npr"] / n, 2),
            "implied_bps_8h": (
                round(d["fund"] / d["nhr"] * BPS_SCALE, 4) if d["nhr"] else None
            ),
        }
    return out


def _entry_drift_live(cli: BitgetLiveClient) -> list[tuple[str, float]]:
    """实盘市价入场的执行漂移:成交 VWAP vs 成交所在 1m bar 的开盘(direction 调整后)。"""
    mk: dict[str, dict[str, Any]] = {}
    fl: dict[str, list[dict[str, Any]]] = {}
    log_dir = paths.resolve_existing("logs")
    for file_path in sorted(log_dir.glob("live_2026*.jsonl")):
        with file_path.open(encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                d = json.loads(ln)
                if d.get("kind") == "market" and d.get("role") == "entry":
                    mk[d["oid"]] = d
                elif d.get("kind") == "fill" and d.get("role") == "entry":
                    fl.setdefault(d["oid"], []).append(d)
    out: list[tuple[str, float]] = []
    for oid, m in mk.items():
        fs = fl.get(oid)
        if not fs:
            continue
        q = sum(float(x["qty"]) for x in fs)
        if q <= 0:
            continue
        px = sum(float(x["qty"]) * float(x["price"]) for x in fs) / q
        fts = max(int(x["ts"]) for x in fs)
        try:
            bars = cli.history_candles(m["symbol"], "1m", end_ms=fts + 60_000, limit=3)
        except BitgetError as exc:
            print(f"[drift live] skip {m['symbol']}: {exc}")
            continue
        b = [x for x in bars if x[0] <= fts]
        if not b or b[-1][1] <= 0:
            continue
        out.append((m["symbol"], (px - b[-1][1]) / b[-1][1] * BPS_SCALE * float(m["direction"])))
    return out


def _entry_drift_ledger(
    cli: BitgetLiveClient,
    path: Path,
    n: int = 60,
) -> list[tuple[str, float]]:
    """回测标的的同一口径:入场后第 1 分钟的不利漂移(1m bar close vs open)。"""
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if (
                r.get("closed") in ("True", "true")
                and r.get("in_segment_open") in ("True", "true")
            ):
                rows.append(r)
    random.Random(7).shuffle(rows)
    out: list[tuple[str, float]] = []
    for r in rows[:n]:
        open_ms = int(float(r["open_ts"]))
        direction = 1.0 if float(r["direction"]) > 0 else -1.0
        try:
            bars = cli.history_candles(r["symbol"], "1m", end_ms=open_ms + 300_000, limit=6)
        except BitgetError as exc:
            print(f"[drift ledger] skip {r['symbol']}: {exc}")
            continue
        b = [x for x in bars if open_ms <= x[0] < open_ms + 120_000]
        if not b or b[0][1] <= 0:
            continue
        out.append((r["tf"], (b[0][4] - b[0][1]) / b[0][1] * BPS_SCALE * direction))
    return out


def _round_trips(fills: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 symbol 重建单仓位开→平区间(策略一 symbol 一仓)。"""
    per: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for f in fills:
        per[str(f["symbol"])].append(f)
    rts: list[dict[str, Any]] = []
    now_ms = time.time() * 1000.0
    for sym, fs in per.items():
        open_ts: int | None = None
        open_notional = 0.0
        acc_qty = 0.0
        for f in fs:
            if f["trade_side"] == "open" or (open_ts is None and acc_qty == 0):
                if acc_qty == 0:
                    open_ts = int(f["ts"])
                    open_notional = 0.0
                acc_qty += float(f["qty"])
                open_notional += float(f["quote"])
                continue
            acc_qty = max(0.0, acc_qty - float(f["qty"]))
            if acc_qty <= QTY_EPS and open_ts is not None:
                hrs = max(0.0, (int(f["ts"]) - open_ts) / MS_H)
                rts.append({
                    "symbol": sym,
                    "open_ts": open_ts,
                    "close_ts": int(f["ts"]),
                    "notional": open_notional,
                    "hours": hrs,
                })
                open_ts = None
                open_notional = 0.0
        if open_ts is not None:
            hrs = max(0.0, (now_ms - open_ts) / MS_H)
            rts.append({
                "symbol": sym,
                "open_ts": open_ts,
                "close_ts": None,
                "notional": open_notional,
                "hours": hrs,
            })
    rts.sort(key=lambda r: r["open_ts"])
    return rts


def _drift_stats(xs: Sequence[tuple[str, float]]) -> dict[str, Any] | None:
    if not xs:
        return None
    v = sorted(x for _, x in xs)
    return {
        "n": len(v),
        "mean": sum(v) / len(v),
        "median": v[len(v) // 2],
        "p10": _pct(v, 0.10),
        "p90": _pct(v, 0.90),
    }


def _load_contract_meta(
    cli: BitgetLiveClient,
) -> tuple[dict[str, tuple[Any, Any]], dict[str, str]]:
    fee_sched: dict[str, tuple[Any, Any]] = {}
    asset_class: dict[str, str] = {}
    try:
        for c in cli.contracts():
            sym_c = c.get("symbol")
            if sym_c:
                fee_sched[str(sym_c)] = (c.get("takerFeeRate"), c.get("makerFeeRate"))
                asset_class[str(sym_c)] = (
                    "rwa" if str(c.get("isRwa") or "").upper() == "YES" else "crypto"
                )
    except BitgetError as exc:
        print(f"[contracts] skipped: {type(exc).__name__} {exc}")
    return fee_sched, asset_class


def _collect_fee_summary(
    fills: Sequence[Mapping[str, Any]],
    bills: Sequence[Mapping[str, Any]],
    fee_sched: Mapping[str, tuple[Any, Any]],
    asset_class: Mapping[str, str],
) -> dict[str, Any]:
    by_scope: dict[str, list[float]] = defaultdict(list)
    fee_cls: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    tot_fee = tot_q = 0.0
    for f in fills:
        if float(f["quote"]) <= 0:
            continue
        by_scope[str(f["scope"])].append(float(f["fee"]) / float(f["quote"]) * BPS_SCALE)
        tot_fee += float(f["fee"])
        tot_q += float(f["quote"])
        bucket = fee_cls[asset_class.get(str(f.get("symbol") or ""), "unknown")]
        bucket[0] += float(f["fee"])
        bucket[1] += float(f["quote"])
    sched_taker = sorted({
        round(float(v[0]) * BPS_SCALE, 2)
        for v in fee_sched.values()
        if v[0] is not None
    })
    sched_maker = sorted({
        round(float(v[1]) * BPS_SCALE, 2)
        for v in fee_sched.values()
        if v[1] is not None
    })
    bill_fee = sum(-_safe_float(b.get("fee")) for b in bills)
    w_bps = tot_fee / tot_q * BPS_SCALE if tot_q else 0.0
    return {
        "by_scope": by_scope,
        "fee_cls": fee_cls,
        "tot_fee": tot_fee,
        "tot_q": tot_q,
        "w_bps": w_bps,
        "sched_taker": sched_taker,
        "sched_maker": sched_maker,
        "bill_fee": bill_fee,
    }


def _collect_funding_summary(
    rts: Sequence[Mapping[str, Any]],
    funding_bills: Sequence[Mapping[str, Any]],
    asset_class: Mapping[str, str],
) -> dict[str, Any]:
    hours_notional = sum(float(r["notional"]) * float(r["hours"]) for r in rts)
    fund_by_sym: dict[str, float] = defaultdict(float)
    fund_cls: dict[str, float] = defaultdict(float)
    for b in funding_bills:
        amt = _safe_float(b.get("amount"))
        sym = str(b.get("symbol") or "?")
        fund_by_sym[sym] += amt
        fund_cls[asset_class.get(sym, "unknown")] += amt
    hn_cls: dict[str, float] = defaultdict(float)
    for r in rts:
        sym = str(r.get("symbol") or "")
        hn_cls[asset_class.get(sym, "unknown")] += float(r["notional"]) * float(r["hours"])
    funding_sum = sum(fund_by_sym.values())
    fund_bps_8h = (
        funding_sum / (hours_notional / FUNDING_HOURS_PER_WINDOW) * BPS_SCALE
        if hours_notional > 0
        else None
    )
    return {
        "hours_notional": hours_notional,
        "fund_by_sym": fund_by_sym,
        "fund_cls": fund_cls,
        "hn_cls": hn_cls,
        "funding_sum": funding_sum,
        "fund_bps_8h": fund_bps_8h,
    }


def _collect_market_funding_stats(
    cli: BitgetLiveClient,
    fills: Sequence[Mapping[str, Any]],
    ledger_path: Path,
    days: int,
) -> dict[str, Any] | None:
    syms = sorted({str(f["symbol"]) for f in fills if f.get("symbol")})
    if not syms and ledger_path.exists():
        seen: set[str] = set()
        with ledger_path.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                s = r.get("symbol")
                if s and s not in seen:
                    seen.add(s)
                    syms.append(s)
    mk = _market_funding(cli, syms[:40], days=min(days, 30))
    all_rates = [x for v in mk.values() for x in v]
    if not all_rates:
        return None
    return {
        "n_symbols": len(mk),
        "n_rates": len(all_rates),
        "mean": sum(all_rates) / len(all_rates),
        "mean_abs": sum(abs(x) for x in all_rates) / len(all_rates),
        "share_pos": sum(1 for x in all_rates if x > 0) / len(all_rates),
        "p10": _pct(all_rates, 0.10),
        "p50": _pct(all_rates, 0.50),
        "p90": _pct(all_rates, 0.90),
    }


def _render_audit_markdown(ctx: Mapping[str, Any]) -> str:
    days: int = ctx["days"]
    costs: Mapping[str, Any] = ctx["costs"]
    fills: Sequence[Mapping[str, Any]] = ctx["fills"]
    bills: Sequence[Mapping[str, Any]] = ctx["bills"]
    funding_bills: Sequence[Mapping[str, Any]] = ctx["funding_bills"]
    fee_info: Mapping[str, Any] = ctx["fee_info"]
    fund_info: Mapping[str, Any] = ctx["fund_info"]
    rts: Sequence[Mapping[str, Any]] = ctx["rts"]
    mk_stats: Mapping[str, Any] | None = ctx["mk_stats"]
    ds_live: Mapping[str, Any] | None = ctx["ds_live"]
    ds_bt: Mapping[str, Any] | None = ctx["ds_bt"]
    ledger: Mapping[str, Mapping[str, Any]] = ctx["ledger"]
    model_fee: float = ctx["model_fee"]
    model_fund: float = ctx["model_fund"]

    fb = "n/a" if fund_info["fund_bps_8h"] is None else f"{fund_info['fund_bps_8h']:+.3f}"
    lines = [
        "# 实盘成本对账(Bitget,只读)",
        "",
        f"- 窗口:最近 {days} 天;成交 {len(fills)} 笔;流水 {len(bills)} 条"
        f"(其中资金费 {len(funding_bills)} 条)",
        f"- 配置假设:`{json.dumps(dict(costs), ensure_ascii=False)}`",
        "",
        "## 结论",
        "",
        f"- 实测综合手续费 **{fee_info['w_bps']:.2f} bps/边**"
        f"(配置 taker {costs['taker_fee'] * BPS_SCALE:.2f} + slip "
        f"{costs['slippage_per_side'] * BPS_SCALE:.2f} = "
        f"{(costs['taker_fee'] + costs['slippage_per_side']) * BPS_SCALE:.2f} bps)",
    ]
    if fee_info["sched_taker"]:
        lines.append(
            f"- 交易所费率档(contracts 端点):taker {fee_info['sched_taker']} bps / "
            f"maker {fee_info['sched_maker']} bps"
        )
    lines.append(
        f"- 实测资金费合计 **{fund_info['funding_sum']:+.6f} USDT**;折合 "
        f"**{fb} bps/8h**(配置 {costs['funding_per_8h_per_side'] * BPS_SCALE:.2f} bps/8h,"
        "恒为成本)"
    )
    if ds_live and ds_bt:
        lines.append(
            f"- 执行漂移:实盘当前宇宙中位 **{ds_live['median']:+.2f} bps**;"
            f"回测标的抽样中位 {ds_bt['median']:+.2f} bps"
            f"(配置滑点 {costs['slippage_per_side'] * BPS_SCALE:.2f} bps/边)"
        )
    lines.append(
        f"- 按同一成交额,模型要收 fee+slip {model_fee:.4f} USDT、资金费 "
        f"{model_fund:+.6f} USDT;实付 fee {fee_info['tot_fee']:.4f}、资金费 "
        f"{fund_info['funding_sum']:+.4f} USDT"
    )
    lines += [
        "",
        "## 手续费(按 tradeScope)",
        "",
        "| scope | n | 中位 bps | 最小 | 最大 |",
        "|---|---|---|---|---|",
    ]
    for k, v in sorted(fee_info["by_scope"].items()):
        s = sorted(v)
        lines.append(f"| {k} | {len(s)} | {s[len(s) // 2]:.2f} | {s[0]:.2f} | {s[-1]:.2f} |")

    lines += ["", f"## 资金费率(交易所公开历史,近 {min(days, 30)} 天)", ""]
    if mk_stats:
        lines += [
            "| 项 | 值 |",
            "|---|---|",
            f"| 覆盖 | {mk_stats['n_symbols']} 个合约 / {mk_stats['n_rates']} 个 8h 结算点 |",
            f"| 均值(有符号) | {mk_stats['mean'] * BPS_SCALE:+.2f} bps/8h |",
            f"| 均值(绝对值) | {mk_stats['mean_abs'] * BPS_SCALE:.2f} bps/8h |",
            f"| 正值占比(多头付费) | {mk_stats['share_pos']:.1%} |",
            f"| p10 / p50 / p90 | {mk_stats['p10'] * BPS_SCALE:+.2f} / "
            f"{mk_stats['p50'] * BPS_SCALE:+.2f} / {mk_stats['p90'] * BPS_SCALE:+.2f} bps |",
            "",
            f"配置假设 {costs['funding_per_8h_per_side'] * BPS_SCALE:.2f} bps/8h **恒为成本**;",
            "实际:多头在正费率时付费、负费率时收费,空头反之。",
        ]
    else:
        lines.append("- 未能取到公开费率历史")

    lines += [
        "",
        "## 执行漂移(入场后第 1 分钟,按方向调整;正 = 不利)",
        "",
        "| 组 | n | 均值 bps | 中位 bps | p10 | p90 |",
        "|---|---|---|---|---|---|",
    ]
    for tag, ds in (("实盘当前宇宙(山寨)", ds_live), ("回测标的 BTC/ETH/SOL/DOGE", ds_bt)):
        if ds:
            lines.append(
                f"| {tag} | {ds['n']} | {ds['mean']:+.2f} | {ds['median']:+.2f} | "
                f"{ds['p10']:+.2f} | {ds['p90']:+.2f} |"
            )
    lines += [
        "",
        "> 实盘组 = 市价成交 VWAP vs 成交所在 1m bar 开盘(下单在 bar 边界后约 10s);"
        "回测组 = 入场 1m bar 的 close vs open 抽样 60 笔。配置假设 2.00 bps/边。",
    ]

    if ledger:
        lines += [
            "",
            "## 账本敏感性:资金费若按实测/0 计,净 E[R] 变化",
            "",
            "| TF | N | 毛E[R] | 费+滑 | 资金费 | 净E[R] | 扣掉资金费后 | 名义/R | 隐含 bps/8h |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for tf in ("1h", "2h", "4h", "12h", "1d"):
            d = ledger.get(tf)
            if not d:
                continue
            imp = "n/a" if d["implied_bps_8h"] is None else format(d["implied_bps_8h"], ".3f")
            lines.append(
                f"| {tf} | {d['n']} | {d['gross']:+.4f} | "
                f"{-(d['fee'] + d['slip']):+.4f} | {-d['fund']:+.4f} | "
                f"{d['net']:+.4f} | {d['net_no_fund']:+.4f} | "
                f"{d['notional_per_R']:.1f}× | {imp} |"
            )
        lines += [
            "",
            "> 隐含 bps/8h = Σ(资金费/R) ÷ Σ(名义/R × 持仓h/8);等于 1.000 说明账本"
            "完全按配置的恒定额计提。",
        ]

    lines += [
        "",
        "## 实盘资金费(按合约,窗口内实测)",
        "",
        "| symbol | 资金费(USDT) | 仓位名义(USDT) | 持仓h | bps/8h |",
        "|---|---|---|---|---|",
    ]
    by_notional: dict[str, float] = defaultdict(float)
    by_hours: dict[str, float] = defaultdict(float)
    by_nh: dict[str, float] = defaultdict(float)
    for r in rts:
        sym = str(r["symbol"])
        by_notional[sym] += float(r["notional"])
        by_hours[sym] += float(r["hours"])
        by_nh[sym] += float(r["notional"]) * float(r["hours"])
    fund_by_sym: Mapping[str, float] = fund_info["fund_by_sym"]
    for sym in sorted(set(fund_by_sym) | set(by_notional)):
        f_usd = fund_by_sym.get(sym, 0.0)
        nh = by_nh.get(sym, 0.0)
        bps8 = f_usd / (nh / FUNDING_HOURS_PER_WINDOW) * BPS_SCALE if nh > 0 else None
        lines.append(
            f"| {sym} | {f_usd:+.6f} | {by_notional.get(sym, 0):.2f} | "
            f"{by_hours.get(sym, 0):.2f} | "
            f"{'n/a' if bps8 is None else format(bps8, '+.2f')} |"
        )

    lines += [
        "",
        "## 逐笔成交",
        "",
        "| cTime | symbol | scope | side/tradeSide | 名义(USDT) | fee | bps |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in fills:
        bps = float(f["fee"]) / float(f["quote"]) * BPS_SCALE if float(f["quote"]) else 0.0
        lines.append(
            f"| {time.strftime('%m-%d %H:%M', time.gmtime(int(f['ts']) / 1000))}Z | "
            f"{f['symbol']} | {f['scope']} | {f['side']}/{f['trade_side']} | "
            f"{f['quote']:.2f} | {f['fee']:.6f} | {bps:.2f} |"
        )
    return "\n".join(lines) + "\n"


def _print_console_summary(
    fee_info: Mapping[str, Any],
    fund_info: Mapping[str, Any],
    fee_sched: Mapping[str, tuple[Any, Any]],
    asset_class: Mapping[str, str],
    model_fee: float,
    model_fund: float,
) -> None:
    print(
        f"[fees] notional={fee_info['tot_q']:.2f} fee={fee_info['tot_fee']:.6f} "
        f"weighted={fee_info['w_bps']:.2f}bps/side "
        f"sched_taker={fee_info['sched_taker']} sched_maker={fee_info['sched_maker']} "
        f"bill_fee={fee_info['bill_fee']:.6f}"
    )
    fb = "n/a" if fund_info["fund_bps_8h"] is None else f"{fund_info['fund_bps_8h']:+.3f}"
    print(
        f"[fund] sum={fund_info['funding_sum']:+.6f} USDT "
        f"notional_hours={fund_info['hours_notional']:.2f} -> {fb} bps/8h"
    )
    print(f"[model] fee+slip={model_fee:.4f} USDT, funding={model_fund:.6f} USDT")
    for cls_name, (fee_k, q_k) in sorted(fee_info["fee_cls"].items()):
        bps_k = fee_k / q_k * BPS_SCALE if q_k else 0.0
        sched_k = sorted({
            round(float(v[0]) * BPS_SCALE, 2)
            for s, v in fee_sched.items()
            if asset_class.get(s, "unknown") == cls_name and v and v[0] is not None
        })
        print(
            f"[fees:{cls_name}] notional={q_k:.2f} fee={fee_k:.6f} "
            f"weighted={bps_k:.2f}bps/side sched_taker={sched_k}"
        )
    hn_cls = fund_info["hn_cls"]
    fund_cls = fund_info["fund_cls"]
    for cls_name in sorted(set(hn_cls) | set(fund_cls)):
        hn_k = hn_cls.get(cls_name, 0.0)
        fund_k = fund_cls.get(cls_name, 0.0)
        bps_k = fund_k / (hn_k / FUNDING_HOURS_PER_WINDOW) * BPS_SCALE if hn_k > 0 else 0.0
        print(f"[fund:{cls_name}] sum={fund_k:+.6f} notional_hours={hn_k:.2f} -> {bps_k:+.3f} bps/8h")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bitget 只读实盘成本对账")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)
    days = int(args.days)

    costs = cfg_costs()
    print("[cfg] " + json.dumps(costs))

    try:
        secrets = load_secrets(required=())
    except SecretsError as exc:
        print(f"[secrets] FAILED {exc}")
        return 2
    has_key = all(secrets.get(k) for k in _REQUIRED_BITGET_KEYS)
    print(f"[secrets] bitget keys present={has_key}")
    if not has_key:
        return 2

    cli = BitgetLiveClient(secrets)
    now = int(time.time() * 1000)
    start = now - days * MS_DAY

    fee_sched, asset_class = _load_contract_meta(cli)
    try:
        fills = _parse_fills(cli.recent_fills(limit=100))
    except BitgetError as exc:
        print(f"[fills] FAILED {type(exc).__name__} {exc}")
        fills = []
    if fills:
        print(f"[fills] n={len(fills)} span={(fills[-1]['ts'] - fills[0]['ts']) / MS_H:.1f}h")
    else:
        print("[fills] none")

    bills = _page_bills(cli, start, now, None)
    funding_bills = _page_bills(cli, start, now, "contract_settle_fee")
    print(f"[bill] n={len(bills)} funding_n={len(funding_bills)}")

    fee_info = _collect_fee_summary(fills, bills, fee_sched, asset_class)
    rts = _round_trips(fills)
    fund_info = _collect_funding_summary(rts, funding_bills, asset_class)

    model_fee = (costs["taker_fee"] + costs["slippage_per_side"]) * fee_info["tot_q"]
    model_fund = (
        costs["funding_per_8h_per_side"]
        * (fund_info["hours_notional"] / FUNDING_HOURS_PER_WINDOW)
    )
    _print_console_summary(fee_info, fund_info, fee_sched, asset_class, model_fee, model_fund)

    ledger_path = (
        paths.resolve_existing("reports")
        / "tf_trigger_exp"
        / "abcd_20261022"
        / "trades_a0__full__1.csv"
    )
    ledger = _ledger_sensitivity(ledger_path) if ledger_path.exists() else {}
    if ledger:
        print("[ledger] " + json.dumps(ledger, ensure_ascii=False))

    mk_stats = _collect_market_funding_stats(cli, fills, ledger_path, days)
    if mk_stats:
        scaled = {
            k: (v * BPS_SCALE if k in ("mean", "mean_abs", "p10", "p50", "p90") else v)
            for k, v in mk_stats.items()
        }
        print(
            "[market funding] symbols={n_symbols} n={n_rates} "
            "mean={mean:+.2f} bps abs={mean_abs:.2f} bps pos={share_pos:.1%} "
            "p10={p10:+.2f} p50={p50:+.2f} p90={p90:+.2f}".format(**scaled)
        )

    ds_live = _drift_stats(_entry_drift_live(cli)) if has_key else None
    ds_bt = _drift_stats(_entry_drift_ledger(cli, ledger_path, n=60)) if ledger_path.exists() else None
    for tag, ds in (("live", ds_live), ("ledger", ds_bt)):
        if ds:
            print(
                f"[drift {tag}] n={ds['n']} mean={ds['mean']:+.2f} "
                f"median={ds['median']:+.2f} p10={ds['p10']:+.2f} "
                f"p90={ds['p90']:+.2f} bps"
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md = _render_audit_markdown({
        "days": days,
        "costs": costs,
        "fills": fills,
        "bills": bills,
        "funding_bills": funding_bills,
        "fee_info": fee_info,
        "fund_info": fund_info,
        "rts": rts,
        "mk_stats": mk_stats,
        "ds_live": ds_live,
        "ds_bt": ds_bt,
        "ledger": ledger,
        "model_fee": model_fee,
        "model_fund": model_fund,
    })
    (OUT_DIR / "AUDIT.md").write_text(md, encoding="utf-8")
    summary_json = {
        "days": days,
        "cfg": costs,
        "n_fills": len(fills),
        "fee_bps_weighted": round(fee_info["w_bps"], 4),
        "fee_bps_by_scope": {
            k: round(sum(v) / len(v), 4) for k, v in fee_info["by_scope"].items()
        },
        "sched_taker_bps": fee_info["sched_taker"],
        "sched_maker_bps": fee_info["sched_maker"],
        "fee_usdt": round(fee_info["tot_fee"], 6),
        "notional_usdt": round(fee_info["tot_q"], 2),
        "bill_fee_usdt": round(fee_info["bill_fee"], 6),
        "funding_usdt": round(fund_info["funding_sum"], 6),
        "funding_bps_8h": (
            None if fund_info["fund_bps_8h"] is None else round(fund_info["fund_bps_8h"], 4)
        ),
        "model_fee_usdt": round(model_fee, 4),
        "model_funding_usdt": round(model_fund, 6),
        "round_trips": [
            {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()}
            for r in rts
        ],
    }
    (OUT_DIR / "audit.json").write_text(
        json.dumps(summary_json, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"[out] {OUT_DIR / 'AUDIT.md'}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
