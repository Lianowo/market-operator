"""BTC full/1x claim 生命周期只读汇总；不产生任何交易判定。"""
from __future__ import annotations

import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOUR = 3_600_000


def number(row, key):
    value = row.get(key)
    return None if value in (None, "") else float(value)


def quantiles(values):
    xs = sorted(values)
    if not xs:
        return {"n": 0, "median": None, "p90": None, "max": None}
    return {
        "n": len(xs),
        "median": statistics.median(xs),
        "p90": xs[max(0, int((len(xs) - 1) * 0.9))],
        "max": xs[-1],
    }


trades = list(csv.DictReader((HERE / "full_1x.trades.csv").open(encoding="utf-8-sig")))
events = list(csv.DictReader((HERE / "full_1x.claim_events.csv").open(encoding="utf-8-sig")))
by_trade = defaultdict(dict)
for event in events:
    by_trade[event["trade_id"]][event["event"]] = event

rows = []
for trade in trades:
    life = by_trade[trade["trade_id"]]
    created = int(life["CREATED"]["ts"])
    confirmed = int(life["CONFIRMED"]["ts"])
    ordered = int(life["ORDERED"]["ts"])
    filled = int(life["FILLED"]["ts"])
    evidence = number(trade, "claim_evidence_started_ts")
    status = ("MIGRATED" if trade.get("claim_migrated_ts") else
              "COMPLETED" if trade.get("claim_completed_ts") else "UNCOMPLETED")
    rows.append({
        "trade_id": trade["trade_id"], "tf": trade["tf"],
        "event_id": trade["event_id"], "status": status,
        "birth_regime": trade.get("claim_birth_regime"),
        "confirm_regime": trade.get("claim_confirm_regime"),
        "environment_migrated": (trade.get("claim_birth_regime")
                                 != trade.get("claim_confirm_regime")),
        "create_to_confirm_h": (confirmed - created) / HOUR,
        "evidence_to_confirm_h": (
            (confirmed - evidence) / HOUR if evidence is not None else None),
        "confirm_to_order_h": (ordered - confirmed) / HOUR,
        "order_to_fill_h": (filled - ordered) / HOUR,
        "confirm_to_fill_h": (filled - confirmed) / HOUR,
        "entry": number(trade, "entry"), "net_pnl": number(trade, "net_pnl"),
        "r_multiple": number(trade, "r_multiple"),
    })

summary = {
    "n_trades": len(rows),
    "event_coverage": dict(Counter(e["event"] for e in events)),
    "status": dict(Counter(r["status"] for r in rows)),
    "environment_birth_to_confirm": {
        "migrated": sum(r["environment_migrated"] for r in rows),
        "same": sum(not r["environment_migrated"] for r in rows),
        "pairs": {f"{a}->{b}": n for (a, b), n in Counter(
            (r["birth_regime"], r["confirm_regime"]) for r in rows
        ).items()},
    },
    "latency_hours": {
        key: quantiles([r[key] for r in rows if r[key] is not None])
        for key in ("create_to_confirm_h", "evidence_to_confirm_h",
                    "confirm_to_order_h", "order_to_fill_h", "confirm_to_fill_h")
    },
    "by_tf": {},
}
for tf in sorted({r["tf"] for r in rows}):
    group = [r for r in rows if r["tf"] == tf]
    summary["by_tf"][tf] = {
        "n": len(group), "status": dict(Counter(r["status"] for r in group)),
        "environment_migrated": sum(r["environment_migrated"] for r in group),
        "create_to_confirm_h": quantiles([r["create_to_confirm_h"] for r in group]),
        "evidence_to_confirm_h": quantiles(
            [r["evidence_to_confirm_h"] for r in group
             if r["evidence_to_confirm_h"] is not None]),
        "confirm_to_fill_h": quantiles([r["confirm_to_fill_h"] for r in group]),
    }

(HERE / "lifecycle_summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
with (HERE / "lifecycle_trades.csv").open("w", newline="", encoding="utf-8-sig") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(json.dumps(summary, ensure_ascii=False, indent=2))