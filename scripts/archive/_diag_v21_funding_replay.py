"""只读回放:用**真实历史资金费率**替换 `funding_per_8h_per_side` 恒定额,看各周期净 E[R]。

- 交易来自 a0 账本(reports/tf_trigger_exp/abcd_20261022/trades_a0__full__1.csv)
- 费率来自 Binance USDT-M 公开历史(每 8h 一次,按 fundingTime 落在持仓区间内计提);
  账本标的只有 BTC/ETH/SOL,三个都在 Binance 永续有完整历史。
- 符号约定:多头(方向 +1)在 rate>0 时付费,空头反之。
  成本(USDT) = notional × Σ_t rate_t · sign(direction)   (rate>0 且做多 ⇒ 正成本)

用法: python scripts/_diag_v21_funding_replay.py [--out reports/live_cost_audit_20261023]
"""
from __future__ import annotations

import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "reports" / "tf_trigger_exp" / "abcd_20261022" / "trades_a0__full__1.csv"
MS_H = 3_600_000
FAPI = "https://fapi.binance.com/fapi/v1/fundingRate"


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get(FAPI, params={"symbol": symbol, "startTime": cur,
                                       "endTime": end_ms, "limit": 1000}, timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for x in rows:
            out.append((int(x["fundingTime"]), float(x["fundingRate"])))
        nxt = rows[-1]["fundingTime"] + 1
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    return out


def main() -> int:
    out_dir = ROOT / "reports" / "live_cost_audit_20261023"
    if "--out" in sys.argv:
        out_dir = Path(sys.argv[sys.argv.index("--out") + 1])
    out_dir.mkdir(parents=True, exist_ok=True)
    if not LEDGER.exists():
        print(f"[ledger] missing {LEDGER}")
        return 2

    rows = []
    with LEDGER.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("closed") not in ("True", "1"):
                continue
            if r.get("in_segment_open") not in ("True", "1"):
                continue
            rows.append(r)
    print(f"[ledger] n={len(rows)}")

    t0 = min(int(r["open_ts"]) for r in rows)
    t1 = max(int(r["exit_ts"]) for r in rows)
    syms = sorted({r["symbol"] for r in rows})
    print(f"[span] {time.strftime('%Y-%m-%d', time.gmtime(t0 / 1000))} .. "
          f"{time.strftime('%Y-%m-%d', time.gmtime(t1 / 1000))} symbols={syms}")

    rates: dict[str, list[tuple[int, float]]] = {}
    for s in syms:
        rates[s] = fetch_funding(s, t0 - MS_H, t1 + MS_H)
        print(f"[rates] {s} n={len(rates[s])}")

    per: dict[str, dict] = defaultdict(lambda: dict(
        n=0, model=0.0, actual=0.0, net=0.0, adverse=0, income=0, zero=0))
    detail = []
    for r in rows:
        sym, tf = r["symbol"], r["tf"]
        risk = float(r["risk_usdt"])
        notional = float(r["notional"])
        hold = float(r["hold_hours"])
        d = 1.0 if float(r["direction"]) > 0 else -1.0
        o, x = int(r["open_ts"]), int(r["exit_ts"])
        s = 0.0
        for ts, rate in rates.get(sym, []):
            if o < ts <= x:
                s += rate
        actual_usd = notional * s * d
        model_usd = notional * 1e-4 * (hold / 8.0)
        acc = per[tf]
        acc["n"] += 1
        acc["model"] += model_usd / risk
        acc["actual"] += actual_usd / risk
        acc["net"] += float(r["net_pnl"]) / risk
        if actual_usd > 1e-9:
            acc["adverse"] += 1
        elif actual_usd < -1e-9:
            acc["income"] += 1
        else:
            acc["zero"] += 1
        detail.append(dict(open_ts=o, tf=tf, symbol=sym, direction=int(d),
                           hold=hold, rate_sum=s, model_R=model_usd / risk,
                           actual_R=actual_usd / risk,
                           net_R=float(r["net_pnl"]) / risk))

    L = ["# 真实资金费率回放(a0 账本)", "",
         f"- 账本:{len(rows)} 笔(closed & 在段内开仓)",
         f"- 区间:{time.strftime('%Y-%m-%d', time.gmtime(t0 / 1000))} .. "
         f"{time.strftime('%Y-%m-%d', time.gmtime(t1 / 1000))};标的 {', '.join(syms)}",
         "- 费率:Binance USDT-M `fapi/v1/fundingRate`(每 8h);按方向计符号", "",
         "## 各周期:模型恒定额 vs 真实费率", "",
         "| TF | N | 模型 资金费(R) | 真实 资金费(R) | 净E[R] | 真实费率下 净E[R] | "
         "付费笔 | 收费笔 | 为零笔 |",
         "|---|---|---|---|---|---|---|---|---|"]
    out_json = {}
    for tf in ("2h", "4h", "12h", "1d"):
        a = per.get(tf)
        if not a:
            continue
        n = a["n"]
        m, act, net = a["model"] / n, a["actual"] / n, a["net"] / n
        adj = net + m - act
        L.append(f"| {tf} | {n} | {m:.4f} | {act:+.4f} | {net:+.4f} | {adj:+.4f} | "
                 f"{a['adverse']} | {a['income']} | {a['zero']} |")
        out_json[tf] = dict(n=n, model_fund_R=round(m, 4),
                            actual_fund_R=round(act, 4), net_R=round(net, 4),
                            net_R_actual_funding=round(adj, 4),
                            adverse=a["adverse"], income=a["income"], zero=a["zero"])

    all_rates = [rt for v in rates.values() for _, rt in v]
    if all_rates:
        pos = sum(1 for x in all_rates if x > 0) / len(all_rates)
        mean = sum(all_rates) / len(all_rates)
        L += ["", "## 费率分布(Binance,全区间)", "",
              f"- 结算点 {len(all_rates)} 个;有符号均值 **{mean * 1e4:+.3f} bps/8h**;"
              f"正值占比 {pos:.1%}",
              f"- 配置假设 {1.0:.2f} bps/8h 恒为成本"]

    (out_dir / "FUNDING_REPLAY.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out_dir / "funding_replay.json").write_text(
        json.dumps(out_json, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(out_json, ensure_ascii=False, indent=1))
    print(f"[out] {out_dir / 'FUNDING_REPLAY.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
