"""BTC 单标的 vs 四标的中的 BTC：同一边际、不同注码（只读）。"""
import csv, json, math, statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
four = [r for r in csv.DictReader((ROOT / "reports/compounding_250_20261104/full_1x.trades.csv").open(encoding="utf-8-sig"))
        if r["in_segment_open"] == "True" and r["closed"] == "True"]
solo = [r for r in csv.DictReader((ROOT / "reports/compounding_btc_250_20261104/full_1x.trades.csv").open(encoding="utf-8-sig"))
        if r["in_segment_open"] == "True" and r["closed"] == "True"]


def describe(rows, label):
    net = [float(r["net_pnl"]) for r in rows]
    risk = [float(r["risk_usdt"]) for r in rows]
    notional = [float(r["notional"]) for r in rows]
    r = [n / k for n, k in zip(net, risk)]
    t = st.mean(r) / (st.stdev(r) / math.sqrt(len(r))) if len(r) > 1 else None
    return {
        "label": label, "n": len(rows),
        "net": round(sum(net), 2), "mean_net": round(st.mean(net), 2),
        "mean_r": round(st.mean(r), 4), "median_r": round(st.median(r), 4),
        "r_stdev": round(st.stdev(r), 3), "r_t_stat": round(t, 2) if t else None,
        "avg_risk": round(st.mean(risk), 1), "median_risk": round(st.median(risk), 1),
        "avg_notional": round(st.mean(notional), 0),
        "winrate": round(sum(1 for n in net if n > 0) / len(net), 3),
        "first": min(int(r["open_ts"]) for r in rows), "last": max(int(r["open_ts"]) for r in rows),
    }


btc_in_four = [r for r in four if r["symbol"] == "BTCUSDT"]
out = {
    "btc_solo_250": describe(solo, "BTC only, equity pathway from 250"),
    "btc_inside_four": describe(btc_in_four, "BTC trades inside the four-symbol portfolio"),
    "other_three": describe([r for r in four if r["symbol"] != "BTCUSDT"], "ETH+SOL+DOGE"),
}
print(json.dumps(out, ensure_ascii=False, indent=2))
(ROOT / "reports/compounding_btc_250_20261104/vs_four.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
