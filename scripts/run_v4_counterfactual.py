"""v4 反事实(counterfactual)测量:过期/武装限价单的"如果市价化会怎样"。

问题(v4-protocol 研究后续,reports/backtest_v4.json 已冻结):E7 三闸门信号
武装的入场限价单里,(a) 超时 12×1h 未穿过 zone 前缘被撤(EXPIRED)的单子,
是不是错过的好交易?(b) 已成交单子若改市价次开盘入场,限价相对市价的
价格改善与结局改变各多少钱(adverse selection 量化)?

做法(事后测量,不改任何冻结文件/config/既有模块):
- 用 config/v4.json 原配置重跑全部 9 次 run_backtest_v4 回放,经
  RecordingBroker(Broker 的运行时子类补丁,仅本脚本进程内有效,不改源码)
  原样捕获每次武装的入场 RestingLimitOrder 全精度字段(invalid/target_1/
  limit_price/armed_at_ts)与武装瞬间的权益(市价化 sizing 基准)。
- 复核对不上就 raise 停止:armed/fills/expired/skips/cancels/go_events/
  open_intents 逐一等于 reports/backtest_v4.json 冻结 counters;逐笔净额
  合计对账 ≤1e-6;每个武装单在账本中恰映射一个结局(LIMIT_FILL/EXPIRED/
  SKIP/CANCEL/数据末仍挂单)。
- 反事实模拟 = 单发(single-shot)v2 语义市价化重放:信号 bar i 决策,bar
  i+1 开盘市价入场(taker+滑点,同 config 0.06%/0.02%),持仓走 §8 六条
  (PositionManager 同 params、同认知缓存 atr/pivots/events),市价减仓/
  退出(下一根 1h 开盘,taker+滑点),资金费同公式按 1h 计提(dt 比例口径
  不变;v4 为 15m 记价,口径差异声明),Broker/PositionManager 均为全新
  实例 ⇒ 每条信号独立于其他持仓(每品种一仓冲突如实声明为单发反事实,
  合计不视为组合收益);sizing 近似 = 真实 v4(1.0x)回放武装瞬间权益 ×
  risk_frac、leverage_cap 同配置。

产物:reports/v4_counterfactual.json(机器可读全量逐信号)与
reports/v4_counterfactual.md(汇总表,只有数字,不给叙述性结论;test 段
过期单 n=4 属 INSUFFICIENT_DATA 如实标注)。

用法: python scripts/run_v4_counterfactual.py
"""

from __future__ import annotations

import hashlib
from typing import Optional
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import aggregate as agg                       # noqa: E402
from market_operator import data as mo_data                        # noqa: E402
from market_operator.backtest import prepare_cognition             # noqa: E402
from market_operator import backtest_v4 as bt4                     # noqa: E402
from market_operator.backtest_v4 import (                          # noqa: E402
    alignment_issues_ci, build_exec_frame, run_backtest_v4)
from market_operator.broker import (                               # noqa: E402
    CONTRACTS_PATH, Broker, ContractSpec, _AcctPosition)
from market_operator.position import PositionManager               # noqa: E402
from market_operator.strategy import (                             # noqa: E402
    OrderIntent, OrderKind, Strategy, StrategyParams)
from market_operator.structure import Params                       # noqa: E402

CONFIG_V4 = ROOT / "config" / "v4.json"
BT_V2_PATH = ROOT / "reports" / "backtest_v2.json"
BT_V4_FROZEN = ROOT / "reports" / "backtest_v4.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev", "test")
COST_MULTS = (1.0, 1.5, 2.0)
MS_1H = 3_600_000

COUNTER_KEYS = ["go_events", "open_intents", "opens_filled", "min_size_skips",
                "liquidations", "limits_armed", "limit_fills_entry",
                "limit_expired", "limit_min_size_skips", "reduce_limits_armed",
                "reduce_limit_fills", "reduce_timeout_taker", "resting_canceled"]


def _iso(ts) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(
        timespec="seconds")


# ---------------------------------------------------------------------------
# 武装单捕获(运行时子类补丁;冻结源码不动)
# ---------------------------------------------------------------------------


class RecordingBroker(Broker):
    """Broker 子类:武装入场限价时快照全精度字段与武装瞬间权益。"""

    instances: list = []

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.recorded_entry_limits: list[dict] = []
        RecordingBroker.instances.append(self)

    def submit_limit(self, order) -> None:
        if order.role == "entry":
            self.recorded_entry_limits.append({
                "symbol": order.symbol, "direction": int(order.direction),
                "limit_price": float(order.limit_price),
                "armed_at_ts": int(order.armed_at_ts),
                "expiry_ts": int(order.expiry_ts),
                "invalid": float(order.invalid),
                "target_1": (None if order.target_1 is None
                             else float(order.target_1)),
                "event_id": order.event_id,
                "regime": order.meta.get("regime"),
                "r_mult": order.meta.get("r_mult"),
                "reason": order.reason,
                "equity_at_arm": float(self.equity_mtm()),
                "cash_at_arm": float(self.cash),
            })
        super().submit_limit(order)


bt4.Broker = RecordingBroker          # 进程内补丁(frozen 文件不修改)


# ---------------------------------------------------------------------------
# 结局推断(账本 → 每武装单一个结局)
# ---------------------------------------------------------------------------


def infer_fates(orders: list[dict], ledger: list[dict], broker: RecordingBroker
                ) -> dict:
    """(symbol, armed_at_ts) → fate dict;附带完整性断言。"""
    fates: dict = {}
    for row in ledger:
        sym = row["symbol"]
        key = (sym, int(row["decision_ts"]))
        if row["kind"] == "EXPIRED":                        # 仅入场挂单有 EXPIRED
            fates[key] = {"fate": "expired", "fate_ts": int(row["ts"]),
                          "wait_bars": row["meta"].get("fill_wait_bars")}
        elif row["kind"] == "LIMIT_FILL" \
                and row["meta"].get("role", "entry") == "entry":
            fates[key] = {"fate": "filled", "fate_ts": int(row["ts"]),
                          "wait_bars": row["meta"].get("fill_wait_bars"),
                          "trade_id": row["trade_id"],
                          "qty": float(row["qty"]),
                          "risk_usdt": float(row["meta"].get("risk_usdt") or 0.0)}
        elif row["kind"] == "SKIP" and "armed_at_ts" in row["meta"] \
                and row["meta"].get("role") == "entry":
            fates[key] = {"fate": "skip_" + row["reason_code"].lower(),
                          "fate_ts": int(row["ts"])}
        elif row["kind"] == "CANCEL" and row["meta"].get("role") == "entry":
            fates[key] = {"fate": "canceled_position_closed",
                          "fate_ts": int(row["ts"])}
    for od_l in broker.resting.values():
        for od in od_l:
            if od.role == "entry":
                fates[(od.symbol, int(od.armed_at_ts))] = {
                    "fate": "resting_at_end", "fate_ts": None,
                    "wait_bars": od.wait_bars}
    order_keys = {(o["symbol"], o["armed_at_ts"]) for o in orders}
    if set(fates) != order_keys:
        missing = order_keys - set(fates)
        extra = set(fates) - order_keys
        raise RuntimeError(f"fate mapping mismatch: missing={sorted(missing)} "
                           f"extra={sorted(extra)}")
    return fates


# ---------------------------------------------------------------------------
# 反事实:单发 v2 语义市价化重放
# ---------------------------------------------------------------------------


def _forced_open(broker, contracts, sym, d, invalid, target_1, order,
                 qty, ts_k, open_px, eq0, reason) -> Optional[dict]:
    """same-qty 变体的手工市价开仓(taker+滑点,与 Broker._fill_open 逐字段同
    口径,但 qty 强制为真实 v4 成交 qty;BAD_INVALID/MIN_SIZE 拒绝 ⇒ None)。

    仅在本脚本进程内重放研究(frozen 源码不动);账本行字段与 _row 一致,
    供对账/汇总读取。
    """
    price = (open_px * (1.0 + broker.slippage) if d > 0
             else open_px * (1.0 - broker.slippage))
    if d * (price - invalid) <= 0.0:
        return None
    spec = contracts[sym]
    notional = qty * price
    if qty < spec.size_min or notional < spec.min_trade_usdt:
        return None
    risk_per_unit = abs(price - invalid)
    fee = notional * broker.taker_fee
    broker.cash -= fee
    broker._seq += 1
    trade_id = f"{sym}#{broker._seq}"
    pos = _AcctPosition(symbol=sym, direction=d, qty=qty, open_qty=qty,
                        entry=price, trade_id=trade_id, opened_ts=ts_k,
                        last_funding_ts=ts_k)
    broker.positions[sym] = pos
    broker.last_mark[sym] = price
    meta = {"direction": d, "regime": order.get("regime"),
            "r_mult": order.get("r_mult"), "invalid": float(invalid),
            "target_1": target_1, "event_id": order["event_id"],
            "risk_usdt": qty * risk_per_unit, "forced_qty": True}
    row = {"seq": broker._next_seq_row(), "ts": int(ts_k), "symbol": sym,
           "side": "buy" if d > 0 else "sell", "kind": "OPEN", "qty": qty,
           "price": price, "open_price": open_px, "fee": fee,
           "funding_delta": 0.0, "realized_pnl": 0.0,
           "decision_ts": int(order["armed_at_ts"]),
           "reason_code": reason.split("|", 1)[0], "reason": reason,
           "trade_id": trade_id, "used_leverage": notional / eq0,
           "slippage_cost": qty * abs(price - open_px),
           "cash_after": broker.cash, "meta": meta}
    broker.ledger.append(row)
    return row


def simulate_market(cog, i_decision: int, order: dict, *, risk_frac: float,
                    leverage_cap: float, taker_fee: float, slippage: float,
                    funding_8h: float, contracts: dict, params: Params,
                    qty_override: Optional[float] = None) -> dict:
    """信号 bar i_decision 收盘决策 ⇒ bar i+1 开盘市价入场,§8 走到退出。

    qty_override:filled 组 A/B 的同量(same-qty)变体 —— 强制按真实 v4 成交
    qty 开仓(绕过 sizing),隔离"执行路径/费用类别/时点"纯效应;其余字段与
    正常路径一致(本脚本进程内手工建仓,冻结源码不动)。

    返回 outcome ∈ traded/open_end/reject_*(BAD_INVALID/MIN_SIZE)/no_future_bar。
    """
    sym = order["symbol"]
    d = int(order["direction"])
    invalid = float(order["invalid"])
    target_1 = order["target_1"]
    eq0 = float(order["equity_at_arm"])
    bars = cog.bars
    n = len(bars)
    j = i_decision + 1
    out = {"open_next_px_raw": (float(bars[j][1]) if j < n else None)}
    if j >= n:
        return {**out, "outcome": "no_future_bar"}
    broker = Broker(equity=eq0, risk_frac=risk_frac, leverage_cap=leverage_cap,
                    taker_fee=taker_fee, slippage_per_side=slippage,
                    funding_per_8h=funding_8h, contracts=contracts,
                    cost_multiplier=1.0)
    pm = PositionManager(params, allow_add=False)
    reason = (f"CF|{order['event_id']} market-next-open (armed "
              f"{bars[i_decision][0]}, limit_front_edge={order['limit_price']:.8g})")
    if qty_override is None:
        broker.submit(OrderIntent(kind=OrderKind.OPEN, symbol=sym, qty_frac=None,
                                  reason=reason,
                                  ref_price=float(bars[i_decision][4]),
                                  ts=bars[i_decision][0]),
                      decision_ts=int(bars[i_decision][0]), direction=d,
                      invalid=invalid, target_1=target_1,
                      event_id=order["event_id"],
                      meta={"direction": d, "regime": order.get("regime"),
                            "r_mult": order.get("r_mult")})
    open_row = None
    open_k = None
    exit_ts = None
    skips: list = []
    for k in range(j, n):
        ts_k = int(bars[k][0])
        rows = broker.on_bar_open(sym, ts_k, float(bars[k][1]))
        if qty_override is not None and k == j:
            forced = _forced_open(broker, contracts, sym, d, invalid, target_1,
                                  order, qty_override, ts_k, float(bars[k][1]),
                                  eq0, reason)
            if forced is None:
                price = (float(bars[k][1]) * (1.0 + broker.slippage) if d > 0
                         else float(bars[k][1]) * (1.0 - broker.slippage))
                code = ("reject_bad_invalid" if d * (price - invalid) <= 0.0
                        else "reject_min_size")
                return {**out, "outcome": code, "skips": []}
            open_row, open_k = forced, k
            pm.open(sym, d, float(open_row["price"]),
                    float(open_row["meta"]["invalid"]),
                    open_row["meta"].get("target_1"),
                    str(open_row["meta"].get("event_id")), ts_k, k)
        for row in rows:
            if row["kind"] == "OPEN":
                m = row["meta"]
                pm.open(sym, d, row["price"], float(m["invalid"]),
                        m.get("target_1"), str(m.get("event_id")), ts_k, k)
                open_row = row
                open_k = k
            elif row["kind"] in ("EXIT", "LIQUIDATION"):
                pm.positions.pop(sym, None)
                exit_ts = ts_k
            elif row["kind"] == "SKIP":
                skips.append({"ts": int(row["ts"]),
                              "code": row["reason_code"],
                              "reason": row["reason"]})
        if open_row is None:                # 首个 bar 未能开仓(拒绝路径)
            code = skips[-1]["code"] if skips else "UNKNOWN"
            return {**out, "outcome": "reject_" + code.lower(), "skips": skips}
        if exit_ts is not None and not broker.positions \
                and not broker.pending.get(sym):
            break
        broker.mark(sym, ts_k, float(bars[k][4]))
        mgmt = pm.on_bar(sym, bars[k], cog.atrs[k],
                         cog.pivots_by.get(k, ()), cog.events_by.get(k, ()))
        for it in mgmt:
            broker.submit(it, decision_ts=ts_k)

    outcome = "traded" if not broker.positions else "open_end"
    net = broker.equity_mtm() - eq0
    legs = [r for r in broker.ledger]
    entry_px = float(open_row["price"])
    qty = float(open_row["qty"])
    risk_usdt = float(open_row["meta"].get("risk_usdt") or 0.0)
    reduces = [r for r in legs if r["kind"] == "REDUCE"]
    first_reduce_ts = min((int(r["decision_ts"]) for r in reduces), default=None)
    inv_exits = [r for r in legs
                 if r["kind"] in ("EXIT", "LIQUIDATION")
                 and str(r["reason"]).startswith("§8.1")]
    first_inv_ts = min((int(r["decision_ts"]) for r in inv_exits), default=None)
    t1_first = None
    if first_reduce_ts is not None:
        t1_first = bool(first_inv_ts is None or first_reduce_ts < first_inv_ts)
    final_exits = [r for r in legs if r["kind"] in ("EXIT", "LIQUIDATION")]
    final_exit = ({"kind": final_exits[-1]["kind"],
                   "decision_ts": int(final_exits[-1]["decision_ts"]),
                   "ts": int(final_exits[-1]["ts"]),
                   "reason": final_exits[-1]["reason"]}
                  if final_exits else None)
    hold_hours = ((exit_ts - int(bars[open_k][0])) / MS_1H
                  if exit_ts is not None else
                  (int(bars[-1][0]) - int(bars[open_k][0])) / MS_1H)
    fees = sum(float(r["fee"]) for r in legs)
    funding = -sum(float(r["funding_delta"]) for r in legs)
    realized = sum(float(r["realized_pnl"]) for r in legs)
    slip = sum(float(r["slippage_cost"]) for r in legs)
    if outcome == "traded":                 # 平仓后权益恒等式对账
        settled = realized - fees - funding
        if abs(settled - net) > 1e-6:
            raise RuntimeError(f"{sym} CF 权益对账失败: legs={settled} "
                               f"vs equity_mtm-eq0={net}")
    return {**out,
            "outcome": outcome,
            "open_ts": int(bars[open_k][0]), "open_iso": _iso(bars[open_k][0]),
            "entry_px": entry_px, "qty": qty, "notional": qty * entry_px,
            "risk_usdt": risk_usdt,
            "net": net, "r": (net / risk_usdt if risk_usdt > 0 else None),
            "t1_before_invalid": t1_first,
            "invalid_first": bool(first_inv_ts is not None
                                  and (first_reduce_ts is None
                                       or first_inv_ts < first_reduce_ts)),
            "n_reduce": len(reduces),
            "first_reduce_decision_ts": first_reduce_ts,
            "final_exit": final_exit, "skips": skips,
            "hold_hours": hold_hours,
            "fees": fees, "funding": funding, "realized_gross": realized,
            "slippage_cost": slip}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _stats(vals: list) -> dict:
    if not vals:
        return {"n": 0, "sum": 0.0, "mean": None,
                "min": None, "max": None}
    return {"n": len(vals), "sum": sum(vals), "mean": sum(vals) / len(vals),
            "min": min(vals), "max": max(vals)}


def summarize_expired(rows: list) -> dict:
    traded = [r for r in rows
              if r["cf"]["outcome"] in ("traded", "open_end")]
    rejects = [r for r in rows if r["cf"]["outcome"].startswith("reject_")]
    nets = [r["cf"]["net"] for r in traded]
    rs = [r["cf"]["r"] for r in traded if r["cf"]["r"] is not None]
    winners_t1 = [r for r in traded if r["cf"]["t1_before_invalid"]]
    avoided = [r for r in traded if r["cf"]["net"] <= 0.0]
    won = [r for r in traded if r["cf"]["net"] > 0.0]
    out_by = {}
    for r in traded:
        k = (r["cf"]["final_exit"]["reason"].split("|", 1)[0]
             if r["cf"]["final_exit"] else "open_end")
        g = out_by.setdefault(k, {"n": 0, "net": 0.0})
        g["n"] += 1
        g["net"] += r["cf"]["net"]
    return {
        "n_expired": len(rows),
        "n_traded": len(traded),
        "n_rejected": len(rejects),
        "reject_kinds": sorted({r["cf"]["outcome"] for r in rejects}),
        "winrate": (len(won) / len(traded)) if traded else None,
        "mean_r": (sum(rs) / len(rs)) if rs else None,
        "sum_net": sum(nets),
        "median_hold_h": (sorted(r["cf"]["hold_hours"]
                                 for r in traded)[len(traded) // 2]
                          if traded else None),
        "n_t1_before_invalid_missed_winners": len(winners_t1),
        "winners_t1_net_sum": sum(r["cf"]["net"] for r in winners_t1),
        "n_net_gt0": len(won),
        "net_gt0_sum": sum(r["cf"]["net"] for r in won),
        "n_avoided_loss_net_le0": len(avoided),
        "avoided_loss_net_sum": sum(r["cf"]["net"] for r in avoided),
        "by_final_exit_rule": out_by,
    }


def summarize_filled_ab(rows: list) -> dict:
    traded = [r for r in rows if r["cf"]["outcome"] in ("traded", "open_end")]
    edge_units, edge_usdts, edge_usdt_act, deltas, qty_reldiff = [], [], [], [], []
    deltas_sq, r_delta_sq = [], []
    sq_traded = 0
    for r in traded:
        edge_u = r["direction"] * (r["cf"]["open_next_px_raw"]
                                   - r["limit_price"])
        edge_units.append(edge_u)
        edge_usdts.append(edge_u * r["cf"]["qty"])
        edge_usdt_act.append(edge_u * r["actual"]["qty"])
        deltas.append(r["cf"]["net"] - r["actual"]["net_pnl"])
        qa = r["actual"]["qty"]
        qty_reldiff.append(abs(r["cf"]["qty"] - qa) / qa if qa > 0 else 0.0)
        cfs = r["cf_sameqty"]
        if cfs is not None and cfs["outcome"] in ("traded", "open_end"):
            sq_traded += 1
            deltas_sq.append(cfs["net"] - r["actual"]["net_pnl"])
            if r["actual"]["risk_usdt"] > 0:
                r_delta_sq.append(
                    (cfs["net"] - r["actual"]["net_pnl"])
                    / r["actual"]["risk_usdt"])
    return {
        "n_filled": len(rows), "n_cf_traded": len(traded),
        "n_cf_rejected": len(rows) - len(traded),
        "edge_per_unit": _stats(edge_units),
        "price_improvement_usdt": _stats(edge_usdts),
        "outcome_change_cf_minus_actual_usdt": _stats(deltas),
        "qty_reldiff_median": (sorted(qty_reldiff)[len(qty_reldiff) // 2]
                               if qty_reldiff else None),
        "n_sign_flip": sum(1 for r in traded
                           if (r["cf"]["net"] > 0) != (r["actual"]["net_pnl"] > 0)),
        "sameqty": {
            "n_traded": sq_traded,
            "price_improvement_usdt": _stats(edge_usdt_act),
            "outcome_change_cf_minus_actual_usdt": _stats(deltas_sq),
            "delta_r_over_actual_risk_mean":
                (sum(r_delta_sq) / len(r_delta_sq) if r_delta_sq else None),
            "n_sign_flip": sum(1 for r in traded
                               if r["cf_sameqty"] is not None
                               and r["cf_sameqty"]["outcome"]
                               in ("traded", "open_end")
                               and (r["cf_sameqty"]["net"] > 0)
                               != (r["actual"]["net_pnl"] > 0)),
        },
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(CONFIG_V4.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V4)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    n_fill = int(raw_cfg["n_fill"])
    k_is = float(raw_cfg["k_is"])
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist_v2"])
    frozen = json.loads(BT_V4_FROZEN.read_text(encoding="utf-8"))

    payload = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    contracts = {}
    for s in symbols:
        ent = payload["entries"][s]
        sha = hashlib.sha256(json.dumps(
            ent["raw"], sort_keys=True).encode("utf-8")).hexdigest()
        if sha != ent["sha256"]:
            raise RuntimeError(f"contracts cache sha256 mismatch for {s}")
        contracts[s] = ContractSpec.from_entry(ent["raw"])

    frames = {}
    for sym in symbols:
        t0 = time.time()
        df_1h = mo_data.load(sym)
        df_15m = mo_data.load(sym, "15m")
        issues = mo_data.validate(df_15m, step_ms=900_000)
        if issues:
            raise RuntimeError(f"{sym} 15m validation: {issues[:5]}")
        if mo_data.check_frame_alignment(df_15m, df_1h, step_fine_ms=900_000):
            raise RuntimeError(f"{sym} 15m↔1h alignment failed")
        compact = agg.aggregate(df_15m, 1)
        if not all((compact[c] - df_1h[c]).abs().max() == 0.0
                   for c in ("open", "high", "low", "close")):
            raise RuntimeError(f"{sym} 15m compaction != 1h frame")
        cog = prepare_cognition(df_1h, params, sym, enable_e7=True)
        ci = alignment_issues_ci(
            cog.bars, [(int(r.ts),) for r in df_15m.itertuples(index=False)],
            dev_frac)
        if ci:
            raise RuntimeError(f"{sym} boundary alignment: {ci}")
        frames[sym] = build_exec_frame(cog, df_15m)
        print(f"[frame] {sym}: {len(df_1h)} bars ({time.time() - t0:.1f}s)",
              flush=True)

    broker_kw = dict(dev_frac=dev_frac, equity0=300.0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params, n_fill=n_fill,
                     maker_fee=float(raw_cfg["maker_fee"]))

    # -- 9 次回放 + 复核对账(对不上即停止) ----------------------------------
    verification = {"per_run": {}, "armed_expected": {"full": 162, "dev": 125,
                                                      "test": 37}}
    runs: dict = {}
    for seg in SEGMENTS:
        for mult in COST_MULTS:
            t0 = time.time()
            res = run_backtest_v4(
                frames, segment=seg, cost_multiplier=mult,
                strategy=Strategy(StrategyParams(
                    whitelist=whitelist, k_is=k_is,
                    entry_mode="limit_front_edge")),
                whitelist=whitelist, **broker_kw)
            broker = RecordingBroker.instances[-1]
            key = f"{seg}@{mult}x"
            fc = frozen["runs"][key]["metrics"]["counters"]
            got = {k: res.counters[k] for k in COUNTER_KEYS}
            exp = {k: fc[k] for k in COUNTER_KEYS}
            net_sum = sum(t["net_pnl"] for t in res.trades)
            fnet = frozen["runs"][key]["metrics"]["net_pnl"]
            ok = got == exp and abs(net_sum - fnet) < 1e-6 \
                and len(res.trades) == len(frozen["runs"][key]["trades"])
            verification["per_run"][key] = {
                "counters_match": got == exp, "counters": got,
                "n_trades_match": len(res.trades)
                == len(frozen["runs"][key]["trades"]),
                "net_pnl_sum": net_sum, "frozen_net_pnl": fnet,
                "net_match": abs(net_sum - fnet) < 1e-6}
            if not ok:
                raise RuntimeError(
                    f"[STOP] {key} 复核对不上冻结产物:" 
                    f" counters={got} vs frozen={exp};"
                    f" net_sum={net_sum} vs {fnet}")
            print(f"[bt] {key}: armed={got['limits_armed']} "
                  f"fills={got['limit_fills_entry']} exp={got['limit_expired']} "
                  f"OK match frozen ({time.time() - t0:.1f}s)", flush=True)
            runs[key] = {"res": res, "orders": broker.recorded_entry_limits,
                         "broker": broker}

    # -- 每段(1.0x)结局推断 + 武装集合跨成本一致性 -------------------------
    analysis: dict = {}
    for seg in SEGMENTS:
        keys = [f"{seg}@{m}x" for m in COST_MULTS]
        arm_sets = [sorted((o["symbol"], o["armed_at_ts"])
                           for o in runs[k]["orders"]) for k in keys]
        if any(a != arm_sets[0] for a in arm_sets[1:]):
            raise RuntimeError(f"[STOP] {seg} 武装集合跨成本不一致")
        res = runs[f"{seg}@1.0x"]["res"]
        orders = runs[f"{seg}@1.0x"]["orders"]
        broker = runs[f"{seg}@1.0x"]["broker"]
        if len(orders) != res.counters["limits_armed"]:
            raise RuntimeError(f"[STOP] {seg} 武装记录 "
                               f"{len(orders)} != counters "
                               f"{res.counters['limits_armed']}")
        fates = infer_fates(orders, res.ledger, broker)
        trades_by_key = {(t["symbol"], t["open_decision_ts"]): t
                         for t in res.trades}
        rows = []
        for o in orders:
            f = fates[(o["symbol"], o["armed_at_ts"])]
            actual = None
            if f["fate"] == "filled":
                actual = trades_by_key.get((o["symbol"], o["armed_at_ts"]))
                if actual is None:
                    raise RuntimeError(
                        f"[STOP] {seg} filled 单无对应 trade: "
                        f"{o['symbol']} @{o['armed_at_ts']}")
                if abs(actual["limit_price"] - o["limit_price"]) > 1e-9:
                    raise RuntimeError(f"[STOP] {seg} limit_price 不一致")
            i_dec = frames[o["symbol"]].hour_index[o["armed_at_ts"]]
            cf = simulate_market(
                frames[o["symbol"]].cog, i_dec, o,
                risk_frac=float(raw_cfg["risk_frac"]),
                leverage_cap=float(raw_cfg["leverage_cap"]),
                taker_fee=float(raw_cfg["taker_fee"]),
                slippage=float(raw_cfg["slippage_per_side"]),
                funding_8h=float(raw_cfg["funding_per_8h_per_side"]),
                contracts=contracts, params=params)
            cf_sq = None
            if actual is not None:          # filled 组:same-qty 变体(纯路径)
                cf_sq = simulate_market(
                    frames[o["symbol"]].cog, i_dec, o,
                    risk_frac=float(raw_cfg["risk_frac"]),
                    leverage_cap=float(raw_cfg["leverage_cap"]),
                    taker_fee=float(raw_cfg["taker_fee"]),
                    slippage=float(raw_cfg["slippage_per_side"]),
                    funding_8h=float(raw_cfg["funding_per_8h_per_side"]),
                    contracts=contracts, params=params,
                    qty_override=float(actual["qty"]))
            rows.append({
                "symbol": o["symbol"], "direction": o["direction"],
                "regime": o["regime"], "r_mult_planned": o["r_mult"],
                "armed_at_ts": o["armed_at_ts"], "armed_iso": _iso(o["armed_at_ts"]),
                "limit_price": o["limit_price"], "invalid": o["invalid"],
                "target_1": o["target_1"], "event_id": o["event_id"],
                "equity_at_arm": o["equity_at_arm"],
                **f,
                "actual": (None if actual is None else {
                    "trade_id": actual["trade_id"], "entry": actual["entry"],
                    "qty": actual["qty"], "net_pnl": actual["net_pnl"],
                    "r_multiple": actual["r_multiple"],
                    "risk_usdt": actual["risk_usdt"],
                    "fill_wait_bars": actual["fill_wait_bars"]}),
                "cf": cf,
                "cf_sameqty": (None if cf_sq is None else {
                    "outcome": cf_sq["outcome"],
                    "entry_px": cf_sq.get("entry_px"),
                    "net": cf_sq.get("net"),
                    "r": cf_sq.get("r"),
                    "risk_usdt": cf_sq.get("risk_usdt"),
                    "t1_before_invalid": cf_sq.get("t1_before_invalid"),
                    "hold_hours": cf_sq.get("hold_hours")})})
        counts: dict = {}
        for r in rows:
            counts[r["fate"]] = counts.get(r["fate"], 0) + 1
        exp_counts = {"filled": res.counters["limit_fills_entry"],
                      "expired": res.counters["limit_expired"],
                      "skip_min_size": res.counters["limit_min_size_skips"]}
        for k, v in exp_counts.items():
            if counts.get(k, 0) != v:
                raise RuntimeError(f"[STOP] {seg} fate {k}={counts.get(k, 0)} "
                                   f"!= counters {v}")
        analysis[seg] = {"n_armed": len(rows), "fate_counts": counts,
                         "rows": rows}
        print(f"[fates] {seg}: {counts}", flush=True)

    # -- 汇总报表 -----------------------------------------------------------
    per_segment: dict = {}
    for seg in SEGMENTS:
        rows = analysis[seg]["rows"]
        expired_rows = [r for r in rows if r["fate"] == "expired"]
        filled_rows = [r for r in rows if r["fate"] == "filled"]
        other_rows = [r for r in rows
                      if r["fate"] not in ("expired", "filled")]
        per_segment[seg] = {
            "n_armed": analysis[seg]["n_armed"],
            "fate_counts": analysis[seg]["fate_counts"],
            "expired_cf": summarize_expired(expired_rows),
            "filled_limit_vs_market_ab": summarize_filled_ab(filled_rows),
            "other_fates": [
                {"symbol": r["symbol"], "armed_iso": r["armed_iso"],
                 "fate": r["fate"], "cf_outcome": r["cf"]["outcome"],
                 "cf_net": r["cf"].get("net")} for r in other_rows],
            "signals": rows,
        }

    doc = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "script": "scripts/run_v4_counterfactual.py",
        "question": "限价接回踩超时未成交被撤(E7,N_fill=12×1h 未穿过zone前缘)"
                    "的单子是不是错过的好交易;已成交单 limit vs market A/B。",
        "verification": verification,
        "declarations": [
            "单发反事实:每条信号用全新 Broker/PositionManager 独立重放,与其"
            "他持仓/挂单无组合约束交互(每品种一仓限制不生效);合计净额是逐"
            "单笔反事实之和,不是任何可实际成交组合的 equity 路径。",
            "市价化定义:信号 1h bar 收盘决策 ⇒ 下一根 1h 开盘市价入场"
            "(taker 0.0006 + 滑点 0.0002,同 config/v4.json),§8 六条与真实"
            "回放同 params、同认知缓存,funding 按 1h bar 计提(v4 真实为 15m"
            "记价,dt 比例口径不变,差异声明);§8.2 减仓按 v2 市价口径(真实"
            "v4 为减仓限价 maker)。成本恒为 1.0x。",
            "sizing 近似:qty = risk_frac·equity_at_arm/|entry-invalid|,"
            "cap = leverage_cap·equity_at_arm/entry,向下取整;equity_at_arm"
            " = 真实 v4(1.0x)回放在武装瞬间的 equity_mtm(如实声明近似,"
            "filled 组给出 |qty_cf-qty_actual| 中位相对差作扰动量级,并另给"
            " same-qty 变体(强制真实成交 qty,隔离 sizing 噪声的纯路径 A/B,"
            "脚本进程内手工建仓,冻结源码不动,账本口径与 Broker._fill_open "
            "逐字段一致)。",
            "结局判定用与 v4 相同的 15m 严格穿过规则(引擎原生回放):"
            "sell 限 high>limit,buy 限 low<limit,窗口 12×1h=48×15m。",
            "resting_at_end/skip_min_size 不是过期单(前者数据末段仍挂单,"
            "后者价格穿越但 sizing 拒绝);过期=EXPIRED 才计入过期组。",
            "test 段过期单 n=4 ⇒ INSUFFICIENT_DATA(如实标注,不引伸)。",
        ],
        "config": {"file": "config/v4.json", "whitelist": ["E7|short", "E7|long"],
                   "k_is": k_is, "entry_mode": raw_cfg["entry_mode"],
                   "n_fill": n_fill, "risk_frac": raw_cfg["risk_frac"],
                   "leverage_cap": raw_cfg["leverage_cap"],
                   "taker_fee": raw_cfg["taker_fee"],
                   "maker_fee": raw_cfg["maker_fee"],
                   "slippage_per_side": raw_cfg["slippage_per_side"],
                   "funding_per_8h": raw_cfg["funding_per_8h_per_side"]},
        "per_segment": {s: {k: v for k, v in per_segment[s].items()
                            if k != "signals"} for s in SEGMENTS},
    }
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "v4_counterfactual.json").write_text(
        json.dumps({**doc,
                    "signals": {s: per_segment[s]["signals"] for s in SEGMENTS}},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "v4_counterfactual.md").write_text(
        render_md(doc, per_segment), encoding="utf-8")
    print(f"-> reports/v4_counterfactual.json/.md "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


def _f(x, nd=2):
    if x is None:
        return "—"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def render_md(doc: dict, per_segment: dict) -> str:
    seg_label = {"full": "full(全部 162)", "dev": "dev(125)",
                 "test": "test(37)"}
    lines = [
        "# v4 反事实测量:过期/武装限价单『如果市价化会怎样』",
        "",
        f"生成时间(UTC):{doc['generated_at_utc']};脚本:"
        f" {doc['script']};配置:config/v4.json(E7±1 白名单,k_is=0.8,"
        " limit_front_edge,N_fill=12)。",
        "",
        "## 0. 复核(前置硬闸,对不上即停止)",
        ""]
    v = doc["verification"]
    for key, r in sorted(v["per_run"].items()):
        lines.append(f"- {key}:counters==冻结={r['counters_match']},"
                     f" net_sum={r['net_pnl_sum']:.6f} vs frozen"
                     f" {r['frozen_net_pnl']:.6f}(match={r['net_match']}),"
                     f" trades_match={r['n_trades_match']}")
    e = doc["verification"]["armed_expected"]
    lines.append(f"- armed 复算:full/dev/test = "
                 f"{doc['per_segment']['full']['n_armed']}/"
                 f"{doc['per_segment']['dev']['n_armed']}/"
                 f"{doc['per_segment']['test']['n_armed']}"
                 f"(期望 {e['full']}/{e['dev']}/{e['test']},全部相等)。"
                 "武装集合跨成本 1.0/1.5/2.0 完全一致(断言)。")
    lines += ["", "## 0.1 声明", ""]
    for dline in doc["declarations"]:
        lines.append(f"- {dline}")

    lines += ["", "## 1. 结局分布(每段武装单 → 15m 严格穿过回放结局)", "",
              "| 段 | armed | filled | expired | skip_min_size | resting_at_end"
              " | canceled |", "|---|---|---|---|---|---|---|"]
    for seg in ("full", "dev", "test"):
        c = per_segment[seg]["fate_counts"]
        lines.append(f"| {seg_label[seg]} | {per_segment[seg]['n_armed']} "
                     f"| {c.get('filled', 0)} | {c.get('expired', 0)} "
                     f"| {c.get('skip_min_size', 0)} "
                     f"| {c.get('resting_at_end', 0)} "
                     f"| {c.get('canceled_position_closed', 0)} |")

    lines += ["", "## 2. 过期单反事实(核心问题:超时撤单若次 1h 开盘市价入场)",
              "",
              "| 段 | 过期n | 可交易 | 拒绝 | 胜率 | 平均R | 合计净额 | "
              "中位持仓h | 到T1未先破invalid(真赢家) | 真赢家净额合计 | "
              "躲过亏损(net≤0) | 躲过亏损合计 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in ("full", "dev", "test"):
        g = per_segment[seg]["expired_cf"]
        note = " ⚠INSUFFICIENT_DATA" if seg == "test" else ""
        lines.append(
            f"| {seg}{note} | {g['n_expired']} | {g['n_traded']} "
            f"| {g['n_rejected']} | {_f(g['winrate'], 3)} | {_f(g['mean_r'], 4)} "
            f"| {_f(g['sum_net'])} | {_f(g['median_hold_h'], 1)} "
            f"| {g['n_t1_before_invalid_missed_winners']} "
            f"| {_f(g['winners_t1_net_sum'])} | {g['n_avoided_loss_net_le0']} "
            f"| {_f(g['avoided_loss_net_sum'])} |")
    for seg in ("full", "dev", "test"):
        g = per_segment[seg]["expired_cf"]
        lines.append(f"- {seg} 到期单最终退出规则分布:"
                     + "; ".join(f"{k}:n={v['n']},net={v['net']:.2f}"
                                 for k, v in sorted(
                                     g["by_final_exit_rule"].items()))
                     + f"; 拒绝原因={g['reject_kinds']}")

    lines += ["", "## 3. 已成交单 limit-vs-market 逐单 A/B(同信号市价化)",
              "",
              "变体① qty_cf = 武装瞬间权益 sizing(默认);变体② same-qty = "
              "强制真实成交 qty(隔离 sizing 噪声,纯执行路径/费用类别效应)。",
              "",
              "| 段 | n | Σ价格改善(①qty_cf) | mean | Σ结局改变① (cf−actual) "
              "| mean | Σ价格改善②(实量) | Σ结局改变②(同量) | mean | "
              "ΔR②(实际risk计) | 符号翻转② |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for seg in ("full", "dev", "test"):
        g = per_segment[seg]["filled_limit_vs_market_ab"]
        sq = g["sameqty"]
        lines.append(
            f"| {seg} | {g['n_filled']} "
            f"| {_f(g['price_improvement_usdt']['sum'])} "
            f"| {_f(g['price_improvement_usdt']['mean'], 4)} "
            f"| {_f(g['outcome_change_cf_minus_actual_usdt']['sum'])} "
            f"| {_f(g['outcome_change_cf_minus_actual_usdt']['mean'], 4)} "
            f"| {_f(sq['price_improvement_usdt']['sum'])} "
            f"| {_f(sq['outcome_change_cf_minus_actual_usdt']['sum'])} "
            f"| {_f(sq['outcome_change_cf_minus_actual_usdt']['mean'], 4)} "
            f"| {_f(sq['delta_r_over_actual_risk_mean'], 4)} "
            f"| {sq['n_sign_flip']} |")
    lines += ["",
              "价格改善 = (open_next − limit_price)×方向×qty(正=限价入场价"
              "更优,不含费用类别差:市价入场额外付 taker+滑点、真实限价付"
              " maker 已含在 cf 净额内);结局改变 = cf 净 − 真实 v4 净(同信号"
              "市价次开盘全权走到退出,单发反事实)。(a)+(b) 非精确分解:"
              "§8 路径(entry 不同 ⇒ 保本/MFE/时间止损不同)与 qty 基准在变体①"
              f"中均变(|qty相对差|中位 "
              f"{_f(per_segment['full']['filled_limit_vs_market_ab']['qty_reldiff_median'], 4)}"
              " full),变体② 锁死 qty 只比路径。"]

    lines += ["", "## 4. 其他结局(非过期、非成交,不计入过期组)", ""]
    for seg in ("full", "dev", "test"):
        others = per_segment[seg]["other_fates"]
        if not others:
            lines.append(f"- {seg}:无")
        for o in others:
            lines.append(f"- {seg}:{o['symbol']}@{o['armed_iso']} "
                         f"fate={o['fate']},cf_outcome={o['cf_outcome']},"
                         f"cf_net={o['cf_net']}")

    full = per_segment["full"]["expired_cf"]
    gab = per_segment["full"]["filled_limit_vs_market_ab"]
    lines += ["", "## 5. 问题回答(cheap numbers only)", "",
              f"- 过期单(full,n={full['n_expired']}):市价化胜率"
              f" {_f(full['winrate'], 3)}、平均R {_f(full['mean_r'], 4)}、"
              f"合计净额 {_f(full['sum_net'])} USDT;真赢家"
              f" {full['n_t1_before_invalid_missed_winners']} 笔"
              f"(净额 {_f(full['winners_t1_net_sum'])}),躲过亏损"
              f" {full['n_avoided_loss_net_le0']} 笔"
              f"(净额 {_f(full['avoided_loss_net_sum'])})。",
              f"- 已成交单 A/B(full):限价相对市价的 Σ价格改善"
              f" ①{_f(gab['price_improvement_usdt']['sum'])} /"
              f" ②(同量){_f(gab['sameqty']['price_improvement_usdt']['sum'])}"
              " USDT;Σ结局改变(cf−actual)"
              f" ①{_f(gab['outcome_change_cf_minus_actual_usdt']['sum'])} /"
              f" ②(同量){_f(gab['sameqty']['outcome_change_cf_minus_actual_usdt']['sum'])}"
              " USDT。",
              "", "机器可读逐信号全量:reports/v4_counterfactual.json。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
