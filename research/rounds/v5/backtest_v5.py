"""v5 回测引擎(docs/v5-protocol.md §1,冻结):破位质量路由 + 15m 执行路径。

与 v4 的关系(A/B):主循环、时间约定、对齐断言、15m 路径、§8 持仓管理、
maker/taker 非对称费用与 v4 完全一致 —— 本模块复用 backtest_v4 的帧契约
(ExecFrame/build_exec_frame/assert_aligned/alignment_issues_ci)、
_arm_reduce_limit(§8.2 目标前缘限价减仓)与 reconstruct_trades_v4(账本→逐笔),
增量只有入场侧路由(V5Router,strategy_v5.py):

- E6|short STRONG(margin ≥ k_dq ∧ impulse ≥ k_imp)⇒ 市价次 1h 开盘
  (Broker.submit,成交 = 下一根 1h bar 首根 15m 开盘价,与 v1/v4 市价同径);
  WEAK ⇒ 不入场,登记 episode(路由器内部状态,每品种至多一个);
- 其后同 episode E7|short(attrs["ref"] == 登记 l_star,浮点同源容差 1e-9)
  ⇒ v4 G3 前缘限价(Rest-ingLimitOrder,armed = 决策 bar 标签,expiry =
  armed + n_fill×1h,15m 严格穿过成交,maker),超时 = 没有这笔交易;
- E1|short ⇒ 永远市价次开盘;信号出现时若该品种有未成交入场挂单 ⇒ 先撤单
  (账本 CANCEL/E1_PRIORITY,E1 优先级最高)并可同 bar 市价入场;持仓中 E1
  按 v1 dup 语义拒绝,不触及 §8 减仓挂单;
- 全入口 F1(k_is=0.8):E1/E6 以事件 bar ATR 度量 dist ≥ k_is·atr(策略层,
  与 E7 的 v4 语义同一知时刻度);目标/r_min 与 v1/v4 同构(决策以收盘近似
  entry);§8 出场、15m 执行路径、maker/taker 费用与 v4 一致 —— §8.2 减仓
  对全部路由仓位统一走目标前缘限价(maker,超时转 taker)。
- 计数:引擎计数(go_events/open_intents/限价统计/撤单)与路由器计数
  (质量判定/episode 追溯/各路由闸门拒绝)合并进 BacktestResult.counters。

因果与断言:与 v4 完全一致(LIMIT_FILL/EXPIRED/CANCEL ts > armed;市价成交 >
决策 ts;模块末 assert_fill_after_decision;每品种一仓一挂单由槽位状态 + dup
拒绝维持——E1 撤单是唯一的挂单解除新路径,其 CANCEL ts = 决策 bar 标签严格
晚于 armed(同 bar 时 E1 先于 E7 评估占有槽位,不可能撤同 bar 新挂的单))。
dev/test 拐点断言同 v4(1h 索引 12320,alignment_issues_ci 复用)。
"""

from __future__ import annotations

from typing import Optional

from research.rounds.v1.backtest import BacktestResult, assert_fill_after_decision
from research.rounds.v4.backtest_v4 import (
    MS_1H,
    N_FINE_PER_COARSE,
    _arm_reduce_limit,
    alignment_issues_ci,  # noqa: F401  (脚本复用锚点)
    assert_aligned,       # noqa: F401
    build_exec_frame,     # noqa: F401
    reconstruct_trades_v4,
)
from market_operator.broker import Broker, RestingLimitOrder  # noqa: F401
from market_operator.position import PositionManager
from market_operator.research import split_boundary
from research.rounds.v1.strategy import OrderKind
from research.rounds.v5.strategy_v5 import ROUTE_E1, ROUTE_WEAK_E7, V5Router
from market_operator.structure import Params


# ---------------------------------------------------------------------------
# v5 引擎:主循环走 15m bar 序列(与 v4 同构;仅入场侧路由不同)
# ---------------------------------------------------------------------------


def run_backtest_v5(frames: dict, *, segment: str, dev_frac: float,
                    equity0: float, risk_frac: float, leverage_cap: float,
                    taker_fee: float, slippage_per_side: float,
                    funding_per_8h: float, cost_multiplier: float,
                    contracts: dict, params: Params, n_fill: int,
                    maker_fee: Optional[float] = None,
                    router: Optional[V5Router] = None,
                    protective_execution: bool = False,
                    portfolio_stop_risk_frac: Optional[float] = None) -> BacktestResult:
    """v5 多品种合并 15m 时间序回放;主循环与缝见模块 docstring 与 v4 同源。"""
    if segment not in ("full", "dev", "test"):
        raise ValueError(f"unknown segment {segment}")
    symbols = sorted(frames)
    seg_bounds: dict = {}
    for sym in symbols:
        cog = frames[sym].cog
        n = len(cog.bars)
        b = split_boundary(n, dev_frac)
        lo, hi = (0, n) if segment == "full" else ((0, b) if segment == "dev"
                                                   else (b, n))
        seg_bounds[sym] = {"lo": lo, "hi": hi, "n": n,
                           "ts_lo": cog.bars[lo][0] if lo < n else None,
                           "ts_hi": cog.bars[hi - 1][0] if hi > 0 else None}

    router = router if router is not None else V5Router()
    go_set = router.params.whitelist

    broker = Broker(equity=equity0, risk_frac=risk_frac, leverage_cap=leverage_cap,
                    taker_fee=taker_fee, slippage_per_side=slippage_per_side,
                    funding_per_8h=funding_per_8h, contracts=contracts,
                    maker_fee=maker_fee, cost_multiplier=cost_multiplier,
                    portfolio_stop_risk_frac=portfolio_stop_risk_frac)
    pos_mgr = PositionManager(params, allow_add=False)
    equity_rows: list = []
    counters = {"go_events": 0, "open_intents": 0, "opens_filled": 0,
                "min_size_skips": 0, "liquidations": 0,
                "limits_armed": 0, "limit_fills_entry": 0, "limit_expired": 0,
                "limit_min_size_skips": 0,
                "reduce_limits_armed": 0, "reduce_limit_fills": 0,
                "reduce_timeout_taker": 0, "resting_canceled": 0,
                "e1_priority_cancels": 0, "protective_exits": 0}

    fine_ts = sorted({q[0] for sym in symbols for qs in frames[sym].quarters.values()
                      for q in qs})
    quarter_of = {sym: {q[0]: q for qs in frames[sym].quarters.values() for q in qs}
                  for sym in symbols}
    hour_edge: dict = {}
    for sym in symbols:
        for i, qs in frames[sym].quarters.items():
            hour_edge.setdefault(qs[N_FINE_PER_COARSE - 1][0], []).append((sym, i))

    for ts in fine_ts:
        for sym in symbols:                      # 同 ts 按 symbol 字典序(同 v2/v4)
            q = quarter_of[sym].get(ts)
            if q is None:
                continue
            _ts, _o, _h, _l, c, _v = q
            # ① 15m 开盘撮合(1h 决策 ⇒ 下一根 1h 首根 15m 开盘,价格 = 1h 开盘)
            for row in broker.on_bar_open(sym, ts, _o):
                k = row["kind"]
                if k == "OPEN":
                    m = row["meta"]
                    pos_mgr.open(sym, int(m["direction"]), row["price"],
                                 float(m["invalid"]), m.get("target_1"),
                                 str(m.get("event_id")), ts,
                                 frames[sym].hour_index[
                                     ts - (ts % MS_1H)])
                    counters["opens_filled"] += 1
                elif k == "REDUCE" and protective_execution:
                    acct = broker.position_of(sym)
                    if acct is None:
                        raise AssertionError("REDUCE must leave open residual position")
                    pos_mgr.confirm_reduce(sym, float(row["qty"]) / acct.open_qty)
                    broker.update_protective_invalid(sym, pos_mgr.positions[sym].invalid)
                elif k in ("EXIT", "LIQUIDATION"):
                    pos_mgr.positions.pop(sym, None)
                    counters["resting_canceled"] += len(broker.cancel_resting(
                        sym, ts, reason_code="POSITION_CLOSED",
                        detail="position closed by EXIT/LIQUIDATION; resting "
                               "limit canceled"))
                elif k == "SKIP":
                    if protective_execution and row["meta"].get("role") == "deferred_reduce":
                        pos_mgr.cancel_reduce(sym)
                    if row["reason_code"] == "MIN_SIZE":
                        counters["min_size_skips"] += 1
            if protective_execution:
                # Stop before pending passive reduction: ambiguous intrabar
                # order prefers loss prevention to fictional maker profit.
                stop_row = broker.execute_protective_stop(sym, ts, _o, _h, _l)
                if stop_row is not None:
                    pos_mgr.positions.pop(sym, None)
                    counters["protective_exits"] += 1
                    counters["resting_canceled"] += len(broker.cancel_resting(
                        sym, ts, reason_code="PROTECTIVE_STOP",
                        detail="stop filled before later maker operations"))
            # ② 挂单处理(严格穿过成交;超时撤单/转市价)
            for row in broker.on_15m_bar(sym, ts, _o, _h, _l, c):
                k = row["kind"]
                if k == "LIMIT_FILL":
                    if row["meta"].get("role") == "reduce":
                        if protective_execution:
                            acct = broker.position_of(sym)
                            if acct is None:
                                raise AssertionError("reduce limit must leave residual")
                            pos_mgr.confirm_reduce(sym, float(row["qty"]) / acct.open_qty)
                            broker.update_protective_invalid(
                                sym, pos_mgr.positions[sym].invalid)
                        counters["reduce_limit_fills"] += 1
                    else:
                        counters["limit_fills_entry"] += 1
                        m = row["meta"]
                        pos_mgr.open(sym, int(m["direction"]), row["price"],
                                     float(m["invalid"]), m.get("target_1"),
                                     str(m.get("event_id")), ts,
                                     frames[sym].hour_index[
                                         ts - (ts % MS_1H)])
                        if protective_execution:
                            # A stop and a passive entry may both occur inside
                            # the same 15m OHLC bar. Model stop as touched
                            # after entry (conservative; sequence unknowable).
                            new_stop = broker.execute_protective_stop(
                                sym, ts, _o, _h, _l)
                            if new_stop is not None:
                                pos_mgr.positions.pop(sym, None)
                                counters["protective_exits"] += 1
                                counters["resting_canceled"] += len(
                                    broker.cancel_resting(
                                        sym, ts, reason_code="PROTECTIVE_STOP",
                                        detail="entry candle stop"))
                elif k == "EXPIRED":
                    counters["limit_expired"] += 1
                elif k == "CANCEL" and row["reason_code"] == "TIMEOUT_TO_TAKER":
                    counters["reduce_timeout_taker"] += 1
                elif k == "SKIP":
                    if protective_execution and row["meta"].get("role") == "reduce":
                        pos_mgr.cancel_reduce(sym)
                    if row["reason_code"] == "MIN_SIZE":
                        counters["min_size_skips"] += 1
                        counters["limit_min_size_skips"] += 1
            # ③ 持仓路径解决:同 v4(同根因果序 ①先于② 结构性解决)
            # ④ mark-to-market(15m 收盘)
            broker.mark(sym, ts, c)
            counters["liquidations"] = broker.n_liquidations
        # ⑤ 1h 边界推进(仅 1h bar 收盘刻;认知缓存 → §8 裁决 → v5 路由决策)
        if ts in hour_edge:
            for sym, i in sorted(hour_edge[ts]):
                frame = frames[sym]
                cog = frame.cog
                bar = cog.bars[i]
                new_events = cog.events_by.get(i, [])
                new_pivots = cog.pivots_by.get(i, [])
                # §8 六条(与 v4 同源同输入;入场成交小时不做裁决,同 v4 声明)
                pos_pm = pos_mgr.positions.get(sym)
                if pos_pm is not None and pos_pm.opened_ts > bar[0]:
                    mgmt = []
                else:
                    mgmt = pos_mgr.on_bar(sym, bar, cog.atrs[i],
                                          new_pivots, new_events,
                                          defer_reduce=protective_execution)
                if protective_execution and sym in pos_mgr.positions and broker.position_of(sym):
                    broker.update_protective_invalid(sym,
                                                      pos_mgr.positions[sym].invalid)
                for intent in mgmt:
                    armed_r = (intent.kind is OrderKind.REDUCE
                               and _arm_reduce_limit(broker, pos_mgr, sym, intent,
                                                     cog.bars[i][0], n_fill))
                    if armed_r:
                        counters["reduce_limits_armed"] += 1
                    else:
                        broker.submit(
                            intent, decision_ts=cog.bars[i][0],
                            meta=({"role": "deferred_reduce"}
                                  if protective_execution and
                                  intent.kind is OrderKind.REDUCE else None))
                # v5 路由(仅段内启用;白名单与闸门在 V5Router 内部)
                lo, hi = seg_bounds[sym]["lo"], seg_bounds[sym]["hi"]
                if lo <= i < hi:
                    counters["go_events"] += sum(
                        1 for e in new_events
                        if (e.id, e.direction) in go_set)
                    decisions = router.on_bar(
                        sym, bar, i, cog.views.get(i), cog.atrs[i], new_events,
                        position_open=broker.position_of(sym) is not None,
                        entry_resting=broker.has_entry_limit(sym))
                    for d in decisions:
                        if d.kind == "cancel_entry":
                            # E1 优先级:撤该品种未成交入场挂单(持仓不与入场
                            # 挂单共存 ⇒ 此时 resting 中不会有 §8 减仓单;
                            # armed < 决策 ts 由同 bar 槽位序保证)
                            for od in broker.resting.get(sym, ()):
                                if od.armed_at_ts >= cog.bars[i][0]:
                                    raise RuntimeError(
                                        f"E1 cancel ts {cog.bars[i][0]} <= armed "
                                        f"{od.armed_at_ts} ({sym} bar {i})")
                            counters["e1_priority_cancels"] += len(
                                broker.cancel_resting(
                                    sym, cog.bars[i][0],
                                    reason_code="E1_PRIORITY",
                                    detail=str(d.meta.get("reason"))))
                        elif d.kind == "market":
                            counters["open_intents"] += 1
                            broker.submit(
                                d.intent, decision_ts=cog.bars[i][0],
                                direction=d.hyp.direction, invalid=d.hyp.invalid,
                                target_1=d.hyp.target_1, event_id=d.hyp.event_id,
                                meta={"direction": d.hyp.direction,
                                      "regime": d.hyp.regime.value,
                                      "r_mult": d.hyp.r_mult,
                                      "route": d.route, **d.meta})
                        elif d.kind == "limit":
                            counters["open_intents"] += 1
                            broker.submit_limit(RestingLimitOrder(
                                direction=int(d.hyp.direction),
                                limit_price=float(d.limit_price),
                                armed_at_ts=int(cog.bars[i][0]),
                                expiry_ts=int(cog.bars[i][0])
                                           + int(n_fill) * MS_1H,
                                symbol=sym, role="entry",
                                invalid=float(d.hyp.invalid),
                                target_1=d.hyp.target_1,
                                event_id=d.hyp.event_id, reason=d.intent.reason,
                                meta={"direction": int(d.hyp.direction),
                                      "regime": d.hyp.regime.value,
                                      "r_mult": d.hyp.r_mult,
                                      "route": d.route, **d.meta}))
                            counters["limits_armed"] += 1
                        else:
                            raise RuntimeError(f"unknown route decision {d.kind}")
        equity_rows.append((ts, broker.equity_mtm(), broker.cash,
                            broker.unrealized(), broker.funding_accrued(),
                            len(broker.positions)))

    for k, v in router.counters.items():
        counters[k] = counters.get(k, 0) + v
    res = BacktestResult(segment=segment, cost_multiplier=cost_multiplier,
                         equity0=equity0, ledger=list(broker.ledger),
                         equity_rows=equity_rows,
                         trades=reconstruct_trades_v5(broker.ledger, seg_bounds),
                         counters=counters, seg_bounds=seg_bounds,
                         contracts={s: vars(c) for s, c in contracts.items()})
    if protective_execution:
        # Every actual exchange/simulator fill must be included in the
        # journal. Intent logs must never substitute for trade executions.
        reconstructed_cash = equity0 + sum(
            float(r["realized_pnl"]) - float(r["fee"])
            + float(r["funding_delta"]) for r in broker.ledger)
        if abs(reconstructed_cash - broker.cash) > 1e-6:
            raise AssertionError(
                f"cash != fill ledger: {broker.cash} != {reconstructed_cash}")
        signed = {}
        for row in broker.ledger:
            if row["kind"] not in ("OPEN","ADD","REDUCE","EXIT",
                                   "LIMIT_FILL","LIQUIDATION"):
                continue
            d = 1 if row["side"] == "buy" else -1
            signed[row["symbol"]] = (signed.get(row["symbol"], 0.0)
                                     + d * float(row["qty"]))
        for sym in symbols:
            acct = broker.position_of(sym)
            actual = acct.qty * acct.direction if acct else 0.0
            if abs(signed.get(sym, 0.0) - actual) > 1e-6:
                raise AssertionError(
                    f"{sym} open quantity != journal: "
                    f"{actual} != {signed.get(sym, 0.0)}")
        # An entry and its installed stop may be crossed in the *same 15m
        # OHLC bar*: no tick ordering is available. Our deliberate pessimistic
        # convention is fill-entry-then-stop, both with the same bar label.
        # All other rows retain the strict decision < fill invariant.
        for row in res.ledger:
            if (row["meta"].get("role") == "protective_stop"
                    and row["ts"] == row["decision_ts"]):
                if row["meta"].get("stop_install_ts") != row["ts"]:
                    raise AssertionError("same-candle stop lacks entry provenance")
        assert_fill_after_decision([
            r for r in res.ledger if not (
                r["meta"].get("role") == "protective_stop"
                and r["ts"] == r["decision_ts"])])
    else:
        assert_fill_after_decision(res.ledger)
    return res


# ---------------------------------------------------------------------------
# 交易重构(v4 逐笔语义 + v5 路由标签/episode 追溯字段)
# ---------------------------------------------------------------------------


def reconstruct_trades_v5(ledger: list, seg_bounds: dict) -> list:
    """reconstruct_trades_v4 输出 + route / l_star / weak_bars(open 行 meta)。"""
    trades = reconstruct_trades_v4(ledger, seg_bounds)
    route_of: dict = {}
    for row in ledger:
        if row["trade_id"] is None or row["trade_id"] in route_of:
            continue
        if row["kind"] in ("OPEN", "LIMIT_FILL"):
            m = row["meta"]
            route_of[row["trade_id"]] = {
                "route": m.get("route"),
                "l_star": m.get("l_star"),
                "weak_bars": m.get("weak_bars"),
                "e6_margin": m.get("e6_margin"),
                "e6_impulse": m.get("e6_impulse"),
            }
    for t in trades:
        t.update(route_of.get(t["trade_id"], {"route": None}))
    return trades


__all__ = ["run_backtest_v5", "reconstruct_trades_v5", "ROUTE_E1",
           "ROUTE_WEAK_E7", "BacktestResult"]
