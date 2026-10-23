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

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.bitget_live import BitgetLiveClient                  # noqa: E402
from market_operator.secrets_loader import SecretsError, load_secrets    # noqa: E402

MS_DAY = 86_400_000
MS_H = 3_600_000
OUT_DIR = ROOT / "reports" / "live_cost_audit_20261023"


def cfg_costs() -> dict:
    cfg = json.loads((ROOT / "config" / "strategy.json").read_text(encoding="utf-8"))
    return {k: cfg.get(k) for k in ("taker_fee", "maker_fee",
                                    "slippage_per_side",
                                    "funding_per_8h_per_side")}


def _page_bills(cli: BitgetLiveClient, start: int, end: int, btype: str | None,
                cap: int = 1000) -> list[dict]:
    out: list[dict] = []
    cursor = end
    while len(out) < cap:
        params = {"productType": "USDT-FUTURES", "startTime": start,
                  "endTime": cursor, "limit": 100}
        if btype:
            params["businessType"] = btype
        data = cli._request("GET", "/api/v2/mix/account/bill", signed=True,
                            params=params)
        rows = (data or {}).get("bills") or [] if isinstance(data, dict) else []
        if not rows:
            break
        out.extend(rows)
        oldest = min(int(r.get("cTime") or 0) for r in rows)
        if oldest <= start or oldest >= cursor:
            break
        cursor = oldest - 1
    return out


def _parse_fills(raw) -> list[dict]:
    rows = raw.get("fillList") if isinstance(raw, dict) else raw
    out = []
    for f in rows or []:
        try:
            px = float(f.get("price") or 0)
            qty = float(f.get("baseVolume") or 0)
        except (TypeError, ValueError):
            continue
        qv = float(f.get("quoteVolume") or 0) or px * qty
        fee = 0.0
        for d in (f.get("feeDetail") or []):
            try:
                fee += abs(float(d.get("totalFee") or 0))
            except (TypeError, ValueError):
                pass
        ts = int(f.get("cTime") or 0)
        out.append(dict(ts=ts, symbol=f.get("symbol"), qty=qty, price=px,
                        quote=qv, fee=fee,
                        scope=(f.get("tradeScope") or "?").lower(),
                        side=(f.get("side") or "?").lower(),
                        trade_side=(f.get("tradeSide") or "?").lower(),
                        profit=float(f.get("profit") or 0) or 0.0))
    out.sort(key=lambda r: r["ts"])
    return out


def _market_funding(cli: BitgetLiveClient, symbols: list[str],
                    days: int) -> dict:
    """交易所公开的资金费率历史(每 8h 一次),用于检验 1bp/8h 的假设。"""
    out: dict[str, list[float]] = {}
    cutoff = int(time.time() * 1000) - days * MS_DAY
    for sym in symbols:
        rates: list[float] = []
        try:
            data = cli._request("GET", "/api/v2/mix/market/history-fund-rate",
                                params={"symbol": sym, "productType": "USDT-FUTURES",
                                        "pageSize": 100, "pageNo": 1})
        except Exception:                                      # noqa: BLE001
            continue
        for r in data or []:
            try:
                ts = int(r.get("fundingTime") or 0)
                if ts < cutoff:
                    continue
                rates.append(float(r.get("fundingRate") or 0))
            except (TypeError, ValueError):
                pass
        if rates:
            out[sym] = rates
    return out


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _ledger_sensitivity(path: Path) -> dict:
    """在 a0 账本上重算:资金费若按实测(≈0)计,各周期净 E[R] 变化多少。"""
    import csv as _csv
    per: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for r in _csv.DictReader(fh):
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
            d = per.setdefault(tf, dict(n=0, gross=0.0, fee=0.0, fund=0.0,
                                        slip=0.0, net=0.0, nh=0.0, nhr=0.0, npr=0.0))
            d["n"] += 1
            d["gross"] += float(r["gross_pnl"]) / risk
            d["fee"] += float(r["fees"]) / risk
            d["fund"] += float(r["funding"]) / risk
            d["slip"] += float(r["slippage_cost"]) / risk
            d["net"] += float(r["net_pnl"]) / risk
            d["nh"] += notional * (hold / 8.0)
            d["nhr"] += (notional / risk) * (hold / 8.0)
            d["npr"] += notional / risk
    out = {}
    for tf, d in per.items():
        n = d["n"] or 1
        out[tf] = dict(n=d["n"],
                       gross=round(d["gross"] / n, 4),
                       fee=round(d["fee"] / n, 4),
                       fund=round(d["fund"] / n, 4),
                       slip=round(d["slip"] / n, 4),
                       net=round(d["net"] / n, 4),
                       net_no_fund=round((d["net"] - d["fund"]) / n, 4),
                       notional_per_R=round(d["npr"] / n, 2),
                       implied_bps_8h=(round(d["fund"] / d["nhr"] * 1e4, 4)
                                       if d["nhr"] else None))
    return out

    """按 symbol 重建单仓位开→平区间(策略一 symbol 一仓)。"""
    per: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        per[f["symbol"]].append(f)
    rts = []
    for sym, fs in per.items():
        open_ts = None
        open_notional = 0.0
        acc_qty = 0.0
        for f in fs:
            if f["trade_side"] == "open" or (open_ts is None and acc_qty == 0):
                if acc_qty == 0:
                    open_ts = f["ts"]
                    open_notional = 0.0
                acc_qty += f["qty"]
                open_notional += f["quote"]
                continue
            acc_qty = max(0.0, acc_qty - f["qty"])
            if acc_qty <= 1e-12 and open_ts is not None:
                hrs = max(0.0, (f["ts"] - open_ts) / MS_H)
                rts.append(dict(symbol=sym, open_ts=open_ts, close_ts=f["ts"],
                                notional=open_notional, hours=hrs))
                open_ts = None
                open_notional = 0.0
        if open_ts is not None:
            hrs = max(0.0, (time.time() * 1000 - open_ts) / MS_H)
            rts.append(dict(symbol=sym, open_ts=open_ts, close_ts=None,
                            notional=open_notional, hours=hrs))
    rts.sort(key=lambda r: r["open_ts"])
    return rts



def _entry_drift_live(cli: BitgetLiveClient) -> list[tuple[str, float]]:
    """实盘市价入场的执行漂移:成交 VWAP vs 成交所在 1m bar 的开盘(direction 调整后)。"""
    import json as _json
    import glob as _glob
    mk, fl = {}, {}
    for f in sorted(_glob.glob(str(ROOT / "logs" / "live_2026*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                d = _json.loads(ln)
                if d.get("kind") == "market" and d.get("role") == "entry":
                    mk[d["oid"]] = d
                elif d.get("kind") == "fill" and d.get("role") == "entry":
                    fl.setdefault(d["oid"], []).append(d)
    out = []
    for oid, m in mk.items():
        fs = fl.get(oid)
        if not fs:
            continue
        q = sum(x["qty"] for x in fs)
        if q <= 0:
            continue
        px = sum(x["qty"] * x["price"] for x in fs) / q
        fts = max(x["ts"] for x in fs)
        try:
            bars = cli.history_candles(m["symbol"], "1m", end_ms=fts + 60_000, limit=3)
        except Exception:                                        # noqa: BLE001
            continue
        b = [x for x in bars if x[0] <= fts]
        if not b or b[-1][1] <= 0:
            continue
        out.append((m["symbol"], (px - b[-1][1]) / b[-1][1] * 1e4 * m["direction"]))
    return out


def _entry_drift_ledger(cli: BitgetLiveClient, path: Path,
                        n: int = 60) -> list[tuple[str, float]]:
    """回测标的的同一口径:入场后第 1 分钟的不利漂移(1m bar close vs open)。"""
    import csv as _csv
    import random
    rows = []
    with path.open(encoding="utf-8") as fh:
        for r in _csv.DictReader(fh):
            if (r.get("closed") in ("True", "true")
                    and r.get("in_segment_open") in ("True", "true")):
                rows.append(r)
    random.Random(7).shuffle(rows)
    out = []
    for r in rows[:n]:
        o = int(float(r["open_ts"]))
        d = 1.0 if float(r["direction"]) > 0 else -1.0
        try:
            bars = cli.history_candles(r["symbol"], "1m", end_ms=o + 300_000, limit=6)
        except Exception:                                        # noqa: BLE001
            continue
        b = [x for x in bars if o <= x[0] < o + 120_000]
        if not b or b[0][1] <= 0:
            continue
        out.append((r["tf"], (b[0][4] - b[0][1]) / b[0][1] * 1e4 * d))
    return out


def _round_trips(fills: list[dict]) -> list[dict]:
    """按 symbol 重建单仓位开→平区间(策略一 symbol 一仓)。"""
    per: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        per[f["symbol"]].append(f)
    rts = []
    for sym, fs in per.items():
        open_ts = None
        open_notional = 0.0
        acc_qty = 0.0
        for f in fs:
            if f["trade_side"] == "open" or (open_ts is None and acc_qty == 0):
                if acc_qty == 0:
                    open_ts = f["ts"]
                    open_notional = 0.0
                acc_qty += f["qty"]
                open_notional += f["quote"]
                continue
            acc_qty = max(0.0, acc_qty - f["qty"])
            if acc_qty <= 1e-12 and open_ts is not None:
                hrs = max(0.0, (f["ts"] - open_ts) / MS_H)
                rts.append(dict(symbol=sym, open_ts=open_ts, close_ts=f["ts"],
                                notional=open_notional, hours=hrs))
                open_ts = None
                open_notional = 0.0
        if open_ts is not None:
            hrs = max(0.0, (time.time() * 1000 - open_ts) / MS_H)
            rts.append(dict(symbol=sym, open_ts=open_ts, close_ts=None,
                            notional=open_notional, hours=hrs))
    rts.sort(key=lambda r: r["open_ts"])
    return rts


def main() -> int:
    days = 30
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--days" and i + 1 < len(args):
            days = int(args[i + 1])

    costs = cfg_costs()
    print("[cfg] " + json.dumps(costs))

    try:
        secrets = load_secrets(required=())
    except SecretsError as e:                                  # noqa: F841
        print(f"[secrets] FAILED {e}")
        return 2
    has_key = all(secrets.get(k) for k in ("BITGET_API_KEY", "BITGET_SECRET_KEY",
                                           "BITGET_PASSPHRASE"))
    print(f"[secrets] bitget keys present={has_key}")
    if not has_key:
        return 2

    cli = BitgetLiveClient(secrets)
    now = int(time.time() * 1000)
    start = now - days * MS_DAY

    fee_sched: dict[str, tuple] = {}
    try:
        for c in cli.contracts():
            if c.get("symbol"):
                fee_sched[c["symbol"]] = (c.get("takerFeeRate"),
                                          c.get("makerFeeRate"))
    except Exception as e:                                     # noqa: BLE001
        print(f"[contracts] skipped: {type(e).__name__} {e}")

    try:
        fills = _parse_fills(cli.recent_fills(limit=100))
    except Exception as e:                                     # noqa: BLE001
        print(f"[fills] FAILED {type(e).__name__} {e}")
        fills = []
    if fills:
        print(f"[fills] n={len(fills)} span="
              f"{(fills[-1]['ts'] - fills[0]['ts']) / MS_H:.1f}h")
    else:
        print("[fills] none")

    bills = _page_bills(cli, start, now, None)
    funding_bills = _page_bills(cli, start, now, "contract_settle_fee")
    print(f"[bill] n={len(bills)} funding_n={len(funding_bills)}")

    # ---- 手续费 ----
    by_scope: dict[str, list] = defaultdict(list)
    tot_fee = tot_q = 0.0
    for f in fills:
        if f["quote"] <= 0:
            continue
        by_scope[f["scope"]].append(f["fee"] / f["quote"] * 10_000)
        tot_fee += f["fee"]
        tot_q += f["quote"]
    sched_taker = sorted({round(float(v[0]) * 10_000, 2) for v in fee_sched.values()
                          if v[0] is not None})
    sched_maker = sorted({round(float(v[1]) * 10_000, 2) for v in fee_sched.values()
                          if v[1] is not None})
    bill_fee = sum(-float(b.get("fee") or 0) for b in bills)

    # ---- 资金费 / notional-hours ----
    rts = _round_trips(fills)
    hours_notional = sum(r["notional"] * r["hours"] for r in rts)
    fund_by_sym: dict[str, float] = defaultdict(float)
    for b in funding_bills:
        try:
            fund_by_sym[b.get("symbol") or "?"] += float(b.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    funding_sum = sum(fund_by_sym.values())
    fund_bps_8h = (funding_sum / (hours_notional / 8) * 10_000
                   if hours_notional > 0 else None)

    model_fee = (costs["taker_fee"] + costs["slippage_per_side"]) * tot_q
    model_fund = costs["funding_per_8h_per_side"] * (hours_notional / 8)

    w_bps = tot_fee / tot_q * 10_000 if tot_q else 0.0
    print(f"[fees] notional={tot_q:.2f} fee={tot_fee:.6f} weighted={w_bps:.2f}bps/side "
          f"sched_taker={sched_taker} sched_maker={sched_maker} bill_fee={bill_fee:.6f}")
    fb = "n/a" if fund_bps_8h is None else f"{fund_bps_8h:+.3f}"
    print(f"[fund] sum={funding_sum:+.6f} USDT notional_hours={hours_notional:.2f} "
          f"-> {fb} bps/8h")
    print(f"[model] fee+slip={model_fee:.4f} USDT, funding={model_fund:.6f} USDT")

    # ---- 市场资金费率(交易所公开历史)+ 账本敏感性 ----
    ledger_path = (ROOT / "reports" / "tf_trigger_exp" / "abcd_20261022"
                   / "trades_a0__full__1.csv")
    ledger = _ledger_sensitivity(ledger_path) if ledger_path.exists() else {}
    if ledger:
        print("[ledger] " + json.dumps(ledger, ensure_ascii=False))

    syms: list[str] = []
    if ledger_path.exists():
        import csv as _csv
        with ledger_path.open(encoding="utf-8") as fh:
            seen: set[str] = set()
            for r in _csv.DictReader(fh):
                s = r.get("symbol")
                if s and s not in seen:
                    seen.add(s)
                    syms.append(s)
    mk = _market_funding(cli, syms[:40], days=min(days, 30))
    all_rates = [x for v in mk.values() for x in v]
    mk_stats = None
    if all_rates:
        mk_stats = dict(n_symbols=len(mk), n_rates=len(all_rates),
                        mean=sum(all_rates) / len(all_rates),
                        mean_abs=sum(abs(x) for x in all_rates) / len(all_rates),
                        share_pos=(sum(1 for x in all_rates if x > 0)
                                   / len(all_rates)),
                        p10=_pct(all_rates, 0.10), p50=_pct(all_rates, 0.50),
                        p90=_pct(all_rates, 0.90))
        print("[market funding] symbols={n_symbols} n={n_rates} "
              "mean={mean:+.2f} bps abs={mean_abs:.2f} bps pos={share_pos:.1%} "
              "p10={p10:+.2f} p50={p50:+.2f} p90={p90:+.2f}".format(
                  **{k: (v * 1e4 if k in ("mean", "mean_abs", "p10", "p50", "p90")
                         else v) for k, v in mk_stats.items()}))

    # ---- 执行滑点:实盘山寨 vs 回测标的 ----
    drift_live = _entry_drift_live(cli) if has_key else []
    drift_bt = (_entry_drift_ledger(cli, ledger_path, n=60)
                if ledger_path.exists() else [])
    def _drift_stats(xs):
        if not xs:
            return None
        v = sorted(x for _, x in xs)
        return dict(n=len(v), mean=sum(v) / len(v), median=v[len(v) // 2],
                    p10=_pct(v, 0.10), p90=_pct(v, 0.90))
    ds_live, ds_bt = _drift_stats(drift_live), _drift_stats(drift_bt)
    for tag, ds in (("live", ds_live), ("ledger", ds_bt)):
        if ds:
            print(f"[drift {tag}] n={ds['n']} mean={ds['mean']:+.2f} "
                  f"median={ds['median']:+.2f} p10={ds['p10']:+.2f} "
                  f"p90={ds['p90']:+.2f} bps")


    # ---------------- 报告 ----------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    L = ["# 实盘成本对账(Bitget,只读)", "",
         f"- 窗口:最近 {days} 天;成交 {len(fills)} 笔;流水 {len(bills)} 条"
         f"(其中资金费 {len(funding_bills)} 条)",
         f"- 配置假设:`{json.dumps(costs, ensure_ascii=False)}`", "",
         "## 结论", ""]
    L.append(f"- 实测综合手续费 **{w_bps:.2f} bps/边**"
             f"(配置 taker {costs['taker_fee'] * 1e4:.2f} + slip "
             f"{costs['slippage_per_side'] * 1e4:.2f} = "
             f"{(costs['taker_fee'] + costs['slippage_per_side']) * 1e4:.2f} bps)")
    if sched_taker:
        L.append(f"- 交易所费率档(contracts 端点):taker {sched_taker} bps / "
                 f"maker {sched_maker} bps")
    L.append(f"- 实测资金费合计 **{funding_sum:+.6f} USDT**;折合 "
             f"**{fb} bps/8h**(配置 {costs['funding_per_8h_per_side'] * 1e4:.2f} bps/8h,"
             f"恒为成本)")
    if ds_live and ds_bt:
        L.append(f"- 执行漂移:实盘当前宇宙中位 **{ds_live['median']:+.2f} bps**;"
                 f"回测标的抽样中位 {ds_bt['median']:+.2f} bps"
                 f"(配置滑点 {costs['slippage_per_side'] * 1e4:.2f} bps/边)")
    L.append(f"- 按同一成交额,模型要收 fee+slip {model_fee:.4f} USDT、资金费 "
             f"{model_fund:+.6f} USDT;实付 fee {tot_fee:.4f}、资金费 "
             f"{funding_sum:+.4f} USDT")
    L += ["", "## 手续费(按 tradeScope)", "",
          "| scope | n | 中位 bps | 最小 | 最大 |", "|---|---|---|---|---|"]
    for k, v in sorted(by_scope.items()):
        s = sorted(v)
        L.append(f"| {k} | {len(s)} | {s[len(s) // 2]:.2f} | {s[0]:.2f} | {s[-1]:.2f} |")

    L += ["", "## 资金费率(交易所公开历史,近 %d 天)" % min(days, 30), ""]
    if mk_stats:
        L += ["| 项 | 值 |", "|---|---|",
              f"| 覆盖 | {mk_stats['n_symbols']} 个合约 / {mk_stats['n_rates']} 个 8h 结算点 |",
              f"| 均值(有符号) | {mk_stats['mean'] * 1e4:+.2f} bps/8h |",
              f"| 均值(绝对值) | {mk_stats['mean_abs'] * 1e4:.2f} bps/8h |",
              f"| 正值占比(多头付费) | {mk_stats['share_pos']:.1%} |",
              f"| p10 / p50 / p90 | {mk_stats['p10'] * 1e4:+.2f} / "
              f"{mk_stats['p50'] * 1e4:+.2f} / {mk_stats['p90'] * 1e4:+.2f} bps |",
              "",
              f"配置假设 {costs['funding_per_8h_per_side'] * 1e4:.2f} bps/8h **恒为成本**;",
              "实际:多头在正费率时付费、负费率时收费,空头反之。"]
    else:
        L.append("- 未能取到公开费率历史")

    L += ["", "## 执行漂移(入场后第 1 分钟,按方向调整;正 = 不利)", "",
          "| 组 | n | 均值 bps | 中位 bps | p10 | p90 |", "|---|---|---|---|---|---|"]
    for tag, ds in (("实盘当前宇宙(山寨)", ds_live),
                    ("回测标的 BTC/ETH/SOL/DOGE", ds_bt)):
        if ds:
            L.append(f"| {tag} | {ds['n']} | {ds['mean']:+.2f} | {ds['median']:+.2f} | "
                     f"{ds['p10']:+.2f} | {ds['p90']:+.2f} |")
    L.append("")
    L.append("> 实盘组 = 市价成交 VWAP vs 成交所在 1m bar 开盘(下单在 bar 边界后约 10s);"
             "回测组 = 入场 1m bar 的 close vs open 抽样 60 笔。配置假设 2.00 bps/边。")

    if ledger:
        L += ["", "## 账本敏感性:资金费若按实测/0 计,净 E[R] 变化", "",
              "| TF | N | 毛E[R] | 费+滑 | 资金费 | 净E[R] | 扣掉资金费后 | 名义/R | 隐含 bps/8h |",
              "|---|---|---|---|---|---|---|---|---|"]
        for tf in ("2h", "4h", "12h", "1d"):
            d = ledger.get(tf)
            if not d:
                continue
            L.append(f"| {tf} | {d['n']} | {d['gross']:+.4f} | "
                     f"{-(d['fee'] + d['slip']):+.4f} | {-d['fund']:+.4f} | "
                     f"{d['net']:+.4f} | {d['net_no_fund']:+.4f} | "
                     f"{d['notional_per_R']:.1f}× | "
                     f"{'n/a' if d['implied_bps_8h'] is None else format(d['implied_bps_8h'], '.3f')} |")
        L.append("")
        L.append("> 隐含 bps/8h = Σ(资金费/R) ÷ Σ(名义/R × 持仓h/8);等于 1.000 说明账本"
                 "完全按配置的恒定额计提。")

    L += ["", "## 实盘资金费(按合约,窗口内实测)", "",
          "| symbol | 资金费(USDT) | 仓位名义(USDT) | 持仓h | bps/8h |",
          "|---|---|---|---|---|"]
    by_notional: dict[str, float] = defaultdict(float)
    by_hours: dict[str, float] = defaultdict(float)
    for r in rts:
        by_notional[r["symbol"]] += r["notional"]
        by_hours[r["symbol"]] += r["hours"]
    for sym in sorted(set(list(fund_by_sym) + list(by_notional))):
        f_usd = fund_by_sym.get(sym, 0.0)
        nh = by_notional.get(sym, 0.0) * by_hours.get(sym, 0.0)
        bps8 = f_usd / (nh / 8) * 10_000 if nh > 0 else None
        L.append(f"| {sym} | {f_usd:+.6f} | {by_notional.get(sym, 0):.2f} | "
                 f"{by_hours.get(sym, 0):.2f} | "
                 f"{'n/a' if bps8 is None else format(bps8, '+.2f')} |")

    L += ["", "## 逐笔成交", "",
          "| cTime | symbol | scope | side/tradeSide | 名义(USDT) | fee | bps |",
          "|---|---|---|---|---|---|---|"]
    for f in fills:
        bps = f["fee"] / f["quote"] * 10_000 if f["quote"] else 0.0
        L.append(f"| {time.strftime('%m-%d %H:%M', time.gmtime(f['ts'] / 1000))}Z | "
                 f"{f['symbol']} | {f['scope']} | {f['side']}/{f['trade_side']} | "
                 f"{f['quote']:.2f} | {f['fee']:.6f} | {bps:.2f} |")

    (OUT_DIR / "AUDIT.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (OUT_DIR / "audit.json").write_text(json.dumps(dict(
        days=days, cfg=costs, n_fills=len(fills),
        fee_bps_weighted=round(w_bps, 4),
        fee_bps_by_scope={k: round(sum(v) / len(v), 4) for k, v in by_scope.items()},
        sched_taker_bps=sched_taker, sched_maker_bps=sched_maker,
        fee_usdt=round(tot_fee, 6), notional_usdt=round(tot_q, 2),
        bill_fee_usdt=round(bill_fee, 6),
        funding_usdt=round(funding_sum, 6),
        funding_bps_8h=None if fund_bps_8h is None else round(fund_bps_8h, 4),
        model_fee_usdt=round(model_fee, 4), model_funding_usdt=round(model_fund, 6),
        round_trips=[{k: (v if not isinstance(v, float) else round(v, 4))
                      for k, v in r.items()} for r in rts]),
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[out] {OUT_DIR / 'AUDIT.md'}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())

    print(f"[model] fee+slip={model_fee:.4f} USDT, funding={model_fund:.6f} USDT")

    # ---- 市场资金费率(公开历史)与账本敏感性 ----
    ledger_path = ROOT / "reports" / "tf_trigger_exp" / "abcd_20261022" / "trades_a0__full__1.csv"
    ledger = _ledger_sensitivity(ledger_path) if ledger_path.exists() else {}
    if ledger:
        print("[ledger] " + json.dumps(ledger, ensure_ascii=False))

    syms: list[str] = []
    if ledger_path.exists():
        import csv as _csv
        with ledger_path.open(encoding="utf-8") as fh:
            seen = set()
            for r in _csv.DictReader(fh):
                s = r.get("symbol")
                if s and s not in seen:
                    seen.add(s)
                    syms.append(s)
    mk = _market_funding(cli, syms[:40], days=min(days, 30))
    all_rates = [x for v in mk.values() for x in v]
    mk_stats = None
    if all_rates:
        mk_stats = dict(n_symbols=len(mk), n_rates=len(all_rates),
                        mean=sum(all_rates) / len(all_rates),
                        mean_abs=sum(abs(x) for x in all_rates) / len(all_rates),
                        share_pos=sum(1 for x in all_rates if x > 0) / len(all_rates),
                        p10=_pct(all_rates, 0.10), p50=_pct(all_rates, 0.50),
                        p90=_pct(all_rates, 0.90))
        print("[market funding] symbols={n_symbols} n={n_rates} "
              "mean={mean:+.2f}bps abs={mean_abs:.2f}bps pos={share_pos:.1%} "
              "p10={p10:+.2f} p50={p50:+.2f} p90={p90:+.2f}".format(
                  **{k: (v * 1e4 if k in ("mean", "mean_abs", "p10", "p50", "p90") else v)
                     for k, v in mk_stats.items()}))

