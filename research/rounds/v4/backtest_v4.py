"""v4 回测引擎(docs/v4-protocol.md §1,冻结):成熟度三闸门 + 15m 执行路径。

与 v2(backtest.run_backtest)的关系(A/B):
- 认知/事件/决策仍走 1h 且同源同路:SymbolCognition 由 prepare_cognition
  在同一批带哈希 1h 字节上预计算(与 v2 回测逐 bar 严格一致;compaction
  一致性测试:tests/test_v4_consistency.py,15m 聚合 1h ⇒ 决策流 == v2)。
- 执行走 15m:订单产生后成交判定在 15m 真实数据路径(Broker.on_15m_bar);
  15m 粒度上只做成交/持仓路径解决,不做任何 15m 认知/结构/决策。

时间约定(v1/v2 同构,正确性硬约束的实现基础):
- 1h 决策 bar(标签 t = bar 开盘毫秒)收盘决策 ⇒ decision_ts = t;
- 15m bar 标签 = 其开盘刻;限价成交/超时检出 ts = 所在 15m bar 标签;
- 因果:LIMIT_FILL/EXPIRED/CANCEL ts > armed_at_ts;市价成交 ts > decision_ts
  (Broker 行内 + 本模块末尾 assert_fill_after_decision 双重断言);
- 每根 15m bar 的处理因果序(合并多 symbol,同 ts 按 symbol 字典序):
  ① 15m 开盘撮合(Broker.on_bar_open):1h 决策的 OPEN/REDUCE/EXIT/强平落在
    下一根 1h bar 的第一根 15m 开盘 —— 价格恰为 1h 开盘价,与 v2 逐 bar
    严格一致;15m 内产生的挂单(强平/超时转市价)落在下一根 15m 开盘;
  ② 挂单处理(Broker.on_15m_bar):严格穿过 ⇒ LIMIT_FILL(maker);超时 ⇒
    EXPIRED(入场)/ CANCEL+转市价(减仓);
  ③ 持仓路径解决(15m):当前 15m 层不存在同根双向触及的决策歧义点 ——
    stop/CHoCH/时间退出均为 1h 收盘口径(§8 原样),15m 上唯一的市场事实是
    挂单成交(无条件的市场穿越),先后由同根因果序(©①先于②)结构性解决。
    同 15m bar 若将来出现双向触及歧义,按既有 §8.4 路径启发式裁决
    (first_extreme_of:close < open ⇒ 先见 high 后见 low)——协议要求挂载,
    此处如实声明为一处无调用的冻结工具。
  ④ mark-to-market(15m 收盘计提资金费/权益/近似强平 —— v2 为 1h 收盘,
    v4 细化到 15m,dt 比例口径不变,更保守;强平预期 0,报告断言);
  ⑤ 仅当本 15m bar 是某 1h bar 的第 4 根子 bar(该 1h bar 收盘刻):取 1h
    认知缓存 → §8 持仓管理裁决(PositionManager,输入 1h bar,与 v2 同源同
    输入)→ §7 策略决策(Strategy;段内才启用 OPEN)——1h 序列严格保持在
    1h 边界推进,决策流与 v2 逐 bar 回放严格一致(同一认知缓存)。

G3 限价接回踩(v4-protocol §1):策略 entry_mode="limit_front_edge" ⇒
OPEN 意图转为 RestingLimitOrder(入场):short 挂 zone.lo,long 挂 zone.hi
(偏向成交率的方向,协议如实声明),armed = 决策 bar 标签,expiry =
armed + n_fill×1h;F1/白名单/r_min/room 各门序与 v2 相同(决策时以收盘
近似 entry,与 v2 一致);每 symbol 一单:持仓中或挂单中,新事件不再触发
(_RESTING_SENTINEL 与持仓同纪律)——每 flip episode 至多一单。
§8.2 目标减仓(v4 执行细化):决策时挂目标前缘(= target_1)限价减仓
(maker),≤ n_fill 根 1h 未成交 ⇒ 撤限价转 taker 市价(数量快照不变);
其余 §8 五条原样(市价、taker+滑点)。仓位入场成交小时不做 §8 裁决
(该小时 bar 含成交前路径):§8 自下一整小时起(N_time 以整持仓小时计,
如实声明)。dev/test 拐点断言 = 1h 索引 12320(v3/v4 协议 §2,15m 落桶
核查 alignment_issues_ci)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import pandas as pd

from research.rounds.v1.backtest import (
    MS_PER_HOUR,
    BacktestResult,
    SymbolCognition,
    assert_fill_after_decision,
)
from market_operator.broker import Broker, RestingLimitOrder, _floor_step
from market_operator.events import MarketEvent  # noqa: F401  (文档锚点)
from market_operator.position import PositionManager
from market_operator.research import split_boundary
from research.rounds.v1.strategy import GO_MECHANISMS, OrderKind, Strategy
from market_operator.structure import Params

MS_15M = 900_000
MS_1H = 3_600_000
N_FINE_PER_COARSE = 4                      # 1h = 4 × 15m(exec_tf = 900s)
BOUNDARY_1H_INDEX_FROZEN = 12320           # dev/test 拐点(v3/v4 协议 §2)

_RESTING_SENTINEL = object()   # 挂单中的"伪持仓"哨兵(与持仓同纪律锁 symbol)


# ---------------------------------------------------------------------------
# 15m 帧契约与同根路径启发式
# ---------------------------------------------------------------------------


def frame_15m(df: pd.DataFrame) -> pd.DataFrame:
    """读取 15m 帧并断言契约:ts 严格等距 900_000 且不重复。"""
    out = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    ts = out["ts"].to_numpy()
    if len(ts) >= 2 and not ((ts[1:] - ts[:-1]) == MS_15M).all():
        raise AssertionError("15m frame not contiguous at 900_000 ms step")
    if len(ts) and (ts % MS_15M).any():
        raise AssertionError("15m frame not aligned to 900_000 ms grid")
    return out


def first_extreme_of(o: float, h: float, l: float, c: float) -> str:
    """既有路径启发式(§8.4/A6 同款):close < open ⇒ 先见 high 后见 low。"""
    return "high" if c < o else "low"


# ---------------------------------------------------------------------------
# 对齐断言(1h ⟷ 15m;执行路径正确性硬约束)
# ---------------------------------------------------------------------------


def assert_aligned(bars_1h: Sequence, bars_15m: Sequence) -> None:
    """15m 帧必须完整覆盖 1h 帧:首 ts ≤ 1h 首;每根 1h bar 恰好 4 根 15m
    子 bar(ts 网格严格,1h_ts + k·900_000)。"""
    if len(bars_1h) == 0 or len(bars_15m) == 0:
        raise AssertionError("empty frame")
    by_ts = {int(b[0]): b for b in bars_15m}
    if len(by_ts) != len(bars_15m):
        raise AssertionError("duplicate ts in 15m frame")
    first_1h, last_1h = int(bars_1h[0][0]), int(bars_1h[-1][0])
    ts_15m_min = min(by_ts)
    ts_15m_max = max(by_ts)
    if ts_15m_min > first_1h:
        raise AssertionError(f"15m frame starts after 1h: {ts_15m_min} > {first_1h}")
    if ts_15m_max < last_1h + (N_FINE_PER_COARSE - 1) * MS_15M:
        raise AssertionError(
            f"15m frame misses sub-bars of last 1h bar {last_1h}")
    for b in bars_1h:
        t = int(b[0])
        for k in range(N_FINE_PER_COARSE):
            if t + k * MS_15M not in by_ts:
                raise AssertionError(f"15m sub-bar missing for 1h ts={t} k={k}")


def alignment_issues_ci(bars_1h: Sequence, bars_15m: Sequence, dev_frac: float,
                        boundary_1h_index: int = BOUNDARY_1H_INDEX_FROZEN
                        ) -> list[str]:
    """dev/test 拐点一致性(v4-protocol §3 同 v3 §2):split_boundary(n, frac)
    必须等于冻结 1h 索引 12320;其 ts 的 4 根 15m 落桶必须全部在场、首根
    标签恰为该 ts。"""
    issues: list[str] = []
    n = len(bars_1h)
    b = split_boundary(n, dev_frac)
    if b != boundary_1h_index:
        issues.append(f"boundary drift: n={n} boundary={b} "
                      f"(frozen expectation {boundary_1h_index})")
    ts_landmark = int(bars_1h[b][0])
    labels = {int(x[0]) for x in bars_15m}
    for k in range(N_FINE_PER_COARSE):
        if ts_landmark + k * MS_15M not in labels:
            issues.append(f"boundary 1h ts {ts_landmark} sub-bar k={k} missing")
    return issues


# ---------------------------------------------------------------------------
# 15m ⟷ 1h 联合帧(认知侧 1h 逐 bar;执行侧 15m)
# ---------------------------------------------------------------------------


@dataclass
class ExecFrame:
    """单品种的 1h 认知缓存 + 15m 执行帧(已对齐断言)。

    quarters[i] = 1h bar i 的 4 根 15m 子 bar((ts,o,h,l,c,v),升序,严格
    网格);hour_index[ts] = 1h bar 标签 → index。认知产物与 v2 逐 bar 回放
    同源(同一 SymbolCognition)。
    """

    cog: SymbolCognition
    quarters: dict                       # 1h bar index → [15m bar ×4]
    hour_index: dict                     # 1h bar ts → index


def build_exec_frame(cog: SymbolCognition, df_15m: pd.DataFrame) -> ExecFrame:
    bars_15m = [(int(r.ts), float(r.open), float(r.high), float(r.low),
                 float(r.close), float(r.volume))
                for r in frame_15m(df_15m).itertuples(index=False)]
    assert_aligned(cog.bars, bars_15m)
    by_ts = {b[0]: b for b in bars_15m}
    quarters: dict = {}
    hour_index: dict = {}
    for i, b in enumerate(cog.bars):
        t = int(b[0])
        hour_index[t] = i
        quarters[i] = [by_ts[t + k * MS_15M] for k in range(N_FINE_PER_COARSE)]
    return ExecFrame(cog=cog, quarters=quarters, hour_index=hour_index)


# ---------------------------------------------------------------------------
# v4 引擎:主循环走 15m bar 序列;1h 认知/事件/决策在 1h 边界推进
# ---------------------------------------------------------------------------


def run_backtest_v4(frames: dict, *, segment: str, dev_frac: float,
                    equity0: float, risk_frac: float, leverage_cap: float,
                    taker_fee: float, slippage_per_side: float,
                    funding_per_8h: float, cost_multiplier: float,
                    contracts: dict, params: Params, n_fill: int,
                    maker_fee: Optional[float] = None,
                    strategy: Optional[Strategy] = None,
                    whitelist: Optional[frozenset] = None,
                    execution_ledger=None,
                    ledger_run_id: Optional[str] = None) -> BacktestResult:
    """v4 多品种合并 15m 时间序回放;主循环与缝见模块 docstring。"""
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

    strategy = strategy if strategy is not None else Strategy()
    entry_mode = strategy.params.entry_mode
    go_set = (whitelist if whitelist is not None
              else (strategy.params.whitelist
                    if strategy.params.whitelist is not None else GO_MECHANISMS))

    broker = Broker(equity=equity0, risk_frac=risk_frac, leverage_cap=leverage_cap,
                    taker_fee=taker_fee, slippage_per_side=slippage_per_side,
                    funding_per_8h=funding_per_8h, contracts=contracts,
                    maker_fee=maker_fee, cost_multiplier=cost_multiplier)
    pos_mgr = PositionManager(params, allow_add=False)   # E4 NO-GO ⇒ 加仓关闭
    equity_rows: list = []
    counters = {"go_events": 0, "open_intents": 0, "opens_filled": 0,
                "min_size_skips": 0, "liquidations": 0,
                "limits_armed": 0, "limit_fills_entry": 0, "limit_expired": 0,
                "limit_min_size_skips": 0,
                "reduce_limits_armed": 0, "reduce_limit_fills": 0,
                "reduce_timeout_taker": 0, "resting_canceled": 0}

    # 15m 合并时间轴(对齐断言 ⇒ 各 symbol 15m 网格恰为 1h 网格的细分)
    fine_ts = sorted({q[0] for sym in symbols for qs in frames[sym].quarters.values()
                      for q in qs})
    quarter_of = {sym: {q[0]: q for qs in frames[sym].quarters.values() for q in qs}
                  for sym in symbols}            # symbol → 15m ts → bar
    hour_edge: dict = {}                         # 15m(第 4 子 bar)ts → [(sym, 1h idx)]
    for sym in symbols:
        for i, qs in frames[sym].quarters.items():
            hour_edge.setdefault(qs[N_FINE_PER_COARSE - 1][0], []).append((sym, i))

    for ts in fine_ts:
        for sym in symbols:                      # 同 ts 按 symbol 字典序(同 v2)
            q = quarter_of[sym].get(ts)
            if q is None:
                continue
            _ts, _o, _h, _l, c, _v = q
            # ① 15m 开盘撮合(v2 语义未决订单:1h 决策 ⇒ 下一根 1h 首根 15m
            #   开盘,价格 = 1h 开盘价,与 v2 一致)
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
                elif k == "ADD":
                    acct = broker.position_of(sym)
                    if acct is None:
                        raise AssertionError("add fill must leave a live position")
                    pos_mgr.confirm_add(sym, float(row["qty"]) / acct.open_qty,
                                        float(row["price"]))
                    broker.update_protective_invalid(sym,
                                                     pos_mgr.positions[sym].invalid)
                elif k == "REDUCE":
                    acct = broker.position_of(sym)
                    if acct is None:
                        raise AssertionError("reduce fill must leave a live residual position")
                    pos_mgr.confirm_reduce(sym, float(row["qty"]) / acct.open_qty)
                    broker.update_protective_invalid(sym, pos_mgr.positions[sym].invalid)
                elif k in ("EXIT", "LIQUIDATION"):
                    pos_mgr.positions.pop(sym, None)
                    counters["resting_canceled"] += len(broker.cancel_resting(
                        sym, ts, reason_code="POSITION_CLOSED",
                        detail="position closed by EXIT/LIQUIDATION; resting "
                               "limit canceled"))
                elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
                    counters["min_size_skips"] += 1
                if k == "SKIP" and row["meta"].get("role") == "deferred_reduce":
                    pos_mgr.cancel_reduce(sym)
                if k == "SKIP" and row["meta"].get("role") == "deferred_add":
                    pos_mgr.cancel_add(sym)
            # ② 挂单处理(严格穿过成交;超时撤单/转市价)
            for row in broker.on_15m_bar(sym, ts, _o, _h, _l, c):
                k = row["kind"]
                if k == "LIMIT_FILL":
                    if row["meta"].get("role") == "reduce":
                        acct = broker.position_of(sym)
                        if acct is None:
                            raise AssertionError("reduce limit fill must leave a residual position")
                        pos_mgr.confirm_reduce(sym, float(row["qty"]) / acct.open_qty)
                        broker.update_protective_invalid(sym, pos_mgr.positions[sym].invalid)
                        counters["reduce_limit_fills"] += 1
                    else:
                        counters["limit_fills_entry"] += 1
                        m = row["meta"]
                        pos_mgr.open(sym, int(m["direction"]), row["price"],
                                     float(m["invalid"]), m.get("target_1"),
                                     str(m.get("event_id")), ts,
                                     frames[sym].hour_index[
                                         ts - (ts % MS_1H)])
                elif k == "EXPIRED":
                    counters["limit_expired"] += 1
                elif k == "CANCEL" and row["reason_code"] == "TIMEOUT_TO_TAKER":
                    counters["reduce_timeout_taker"] += 1
                elif k == "SKIP":
                    if row["meta"].get("role") == "reduce":
                        pos_mgr.cancel_reduce(sym)
                    if row["reason_code"] == "MIN_SIZE":
                        counters["min_size_skips"] += 1
                        counters["limit_min_size_skips"] += 1
            # ③ 持仓路径解决:结构性解决(同根因果序 ①先于②),见模块 docstring
            # ④ mark-to-market(15m 收盘)
            broker.mark(sym, ts, c)
            counters["liquidations"] = broker.n_liquidations
        # ⑤ 1h 边界推进(仅 1h bar 收盘刻;认知缓存 → §8 裁决 → §7 策略)
        if ts in hour_edge:
            for sym, i in sorted(hour_edge[ts]):
                frame = frames[sym]
                cog = frame.cog
                bar = cog.bars[i]
                new_events = cog.events_by.get(i, [])
                new_pivots = cog.pivots_by.get(i, [])
                # §8 六条(与 v2 同源同输入;§8.2 减仓在 limit 模式下挂限价。
                # 入场成交小时不做裁决:该小时 1h bar 含成交前价格路径
                # (15m 限价成交刻 opened_ts 严格晚于本 bar 开盘);市价/整点
                # 成交(opened_ts == 本 bar 开盘)与 v2 语义一致,照常裁决。
                pos_pm = pos_mgr.positions.get(sym)
                if pos_pm is not None and pos_pm.opened_ts > bar[0]:
                    mgmt = []
                else:
                    mgmt = pos_mgr.on_bar(sym, bar, cog.atrs[i],
                                          new_pivots, new_events,
                                          defer_reduce=(entry_mode == "limit_front_edge"),
                                          defer_add=True)
                # 已确认 pivot/成交后,同步当前有效结构防线至 broker 风险账本。
                if sym in pos_mgr.positions and broker.position_of(sym) is not None:
                    broker.update_protective_invalid(sym, pos_mgr.positions[sym].invalid)
                for intent in mgmt:
                    armed = (intent.kind is OrderKind.REDUCE
                             and entry_mode == "limit_front_edge"
                             and _arm_reduce_limit(broker, pos_mgr, sym,
                                                   intent, cog.bars[i][0],
                                                   n_fill))
                    if armed:
                        counters["reduce_limits_armed"] += 1
                    else:
                        broker.submit(
                            intent, decision_ts=cog.bars[i][0],
                            meta=({"role": "deferred_reduce"}
                                  if intent.kind is OrderKind.REDUCE
                                  and entry_mode == "limit_front_edge" else
                                  {"role": "deferred_add",
                                   "add_invalid": pos_mgr.positions[sym].pending_add_invalid}
                                  if intent.kind is OrderKind.ADD else None))
                # §7 策略(仅段内启用 OPEN;GO 白名单由 Strategy 内部裁决)
                lo, hi = seg_bounds[sym]["lo"], seg_bounds[sym]["hi"]
                if lo <= i < hi:
                    counters["go_events"] += sum(
                        1 for e in new_events
                        if (e.id, e.direction) in go_set)
                    state = broker.position_of(sym)
                    # 持仓中或挂单中:每 symbol 一单(每 flip episode 至多一单)
                    if state is None and broker.has_entry_limit(sym):
                        state = _RESTING_SENTINEL
                    for intent in strategy.on_bar(
                            sym, bar, cog.views.get(i), new_events,
                            open_position_state=state):
                        if intent.kind is not OrderKind.OPEN:
                            broker.submit(intent, decision_ts=cog.bars[i][0])
                            continue
                        hyp = strategy._taken.get(sym)    # 已采纳假设(记录字段)
                        counters["open_intents"] += 1
                        if entry_mode == "limit_front_edge":
                            limit = strategy._taken_limit.get(sym)
                            if limit is None:
                                raise RuntimeError(
                                    f"{sym} entry_mode=limit_front_edge but no "
                                    f"limit recorded (bar {i})")
                            broker.submit_limit(RestingLimitOrder(
                                direction=int(hyp.direction),
                                limit_price=float(limit),
                                armed_at_ts=int(cog.bars[i][0]),
                                expiry_ts=int(cog.bars[i][0]) + int(n_fill) * MS_1H,
                                symbol=sym, role="entry",
                                invalid=float(hyp.invalid),
                                target_1=hyp.target_1,
                                event_id=hyp.event_id, reason=intent.reason,
                                meta={"direction": int(hyp.direction),
                                      "regime": hyp.regime.value,
                                      "r_mult": hyp.r_mult}))
                            counters["limits_armed"] += 1
                        else:
                            broker.submit(
                                intent, decision_ts=cog.bars[i][0],
                                direction=hyp.direction, invalid=hyp.invalid,
                                target_1=hyp.target_1, event_id=hyp.event_id,
                                meta={"direction": hyp.direction,
                                      "regime": hyp.regime.value,
                                      "r_mult": hyp.r_mult})
        equity_rows.append((ts, broker.equity_mtm(), broker.cash,
                            broker.unrealized(), broker.funding_accrued(),
                            len(broker.positions)))

    res = BacktestResult(segment=segment, cost_multiplier=cost_multiplier,
                         equity0=equity0, ledger=list(broker.ledger),
                         equity_rows=equity_rows,
                         trades=reconstruct_trades_v4(broker.ledger, seg_bounds),
                         counters=counters, seg_bounds=seg_bounds,
                         contracts={s: vars(c) for s, c in contracts.items()})
    assert_fill_after_decision(res.ledger)
    if execution_ledger is not None:
        # Optional full-path ledger audit. Broker is still the simulator, and
        # we import only actual execution rows, never signals or attempted orders.
        if not ledger_run_id:
            raise ValueError("ledger_run_id required when execution_ledger is set")
        from decimal import Decimal
        for fill_row in broker.ledger:
            execution_ledger.record_backtest_fill(fill_row, run_id=ledger_run_id)
        expected = {
            sym: Decimal(str(pos.qty)) * pos.direction
            for sym, pos in broker.positions.items() if pos.qty > 0
        }
        # Block promotion if journal and simulated real positions disagree.
        execution_ledger.verify_exchange_positions(expected)
    return res


def _arm_reduce_limit(broker: Broker, pos_mgr: PositionManager, symbol: str,
                      intent, decision_ts: int, n_fill: int) -> bool:
    """§8.2 减仓 → 目标前缘(target_1)限价挂单(maker)。返回 True = 已武装。

    数量快照 = v2 市价路径在成交刻会算出的同一数量(qty_frac × open_qty 向
    size_step 下取整);无仓/无目标/快照取整为 0 ⇒ 返回 False,交回市价
    路径(v2 SKIP 语义保留)。
    """
    pos_pm = pos_mgr.positions.get(symbol)
    pos_acct = broker.position_of(symbol)
    target = pos_pm.target_1 if pos_pm is not None else None
    if pos_acct is None or target is None:
        return False                            # 无仓/无目标:交回市价路径处理
    spec = broker.contracts[symbol]
    qty = _floor_step(float(intent.qty_frac or 0.0) * pos_acct.open_qty,
                      spec.size_step, spec.size_place)
    if qty <= 0.0:
        return False                            # v2 SKIP("close qty rounds to 0")
    direction = -1 if pos_acct.direction > 0 else +1   # 平仓方向(多卖/空买)
    broker.submit_limit(RestingLimitOrder(
        direction=direction, limit_price=float(target),
        armed_at_ts=int(decision_ts),
        expiry_ts=int(decision_ts) + int(n_fill) * MS_1H,
        symbol=symbol, role="reduce", reason=intent.reason,
        reduce_qty=qty, qty_frac_snap=qty / pos_acct.open_qty,
        meta={"direction": pos_acct.direction}))
    return True


# ---------------------------------------------------------------------------
# 交易重构(账本 → 逐笔;v4:入场可为 LIMIT_FILL;账本对账语义同 v2)
# ---------------------------------------------------------------------------


def reconstruct_trades_v4(ledger: list, seg_bounds: dict) -> list:
    by_tid: dict = {}
    for row in ledger:
        if row["kind"] == "SKIP" or row["trade_id"] is None:
            continue
        by_tid.setdefault(row["trade_id"], []).append(row)

    trades: list = []
    for tid, rows in by_tid.items():
        open_row = rows[0]
        if open_row["kind"] not in ("OPEN", "LIMIT_FILL"):
            continue
        m = open_row["meta"]
        sym = open_row["symbol"]
        direction = -1 if open_row["side"] == "sell" else +1
        closes = [r for r in rows
                  if r["kind"] in ("REDUCE", "EXIT", "LIQUIDATION")
                  or (r["kind"] == "LIMIT_FILL"
                      and r["meta"].get("role") == "reduce")]
        closed = bool(closes) and closes[-1]["kind"] in ("EXIT", "LIQUIDATION")
        final = closes[-1] if closes else None
        qty_closed = sum(r["qty"] for r in closes)
        exit_px = (sum(r["qty"] * r["price"] for r in closes) / qty_closed
                   if qty_closed > 0 else None)
        gross = sum(r["realized_pnl"] for r in rows)
        fees = sum(r["fee"] for r in rows)
        fees_maker = sum(r["fee"] for r in rows if r["kind"] == "LIMIT_FILL")
        funding = -sum(r["funding_delta"] for r in rows)   # funding_delta ≤ 0
        slip = sum(r["slippage_cost"] for r in rows)
        net = gross - fees - funding
        risk = float(m.get("risk_usdt") or 0.0)
        b = seg_bounds[sym]
        trades.append({
            "trade_id": tid, "symbol": sym, "direction": direction,
            "event_id": m.get("event_id"), "regime": m.get("regime"),
            "r_mult_planned": m.get("r_mult"),
            "invalid": m.get("invalid"), "target_1": m.get("target_1"),
            "risk_usdt": risk,
            "open_decision_ts": open_row["decision_ts"],
            "open_ts": open_row["ts"], "entry": open_row["price"],
            "entry_mode": ("limit" if open_row["kind"] == "LIMIT_FILL"
                           else "market"),
            "fill_wait_bars": open_row["meta"].get("fill_wait_bars"),
            "limit_price": open_row["meta"].get("limit_price"),
            "qty": open_row["qty"],
            "notional": open_row["qty"] * open_row["price"],
            "used_leverage": open_row["used_leverage"],
            "closed": closed,
            "close_decision_ts": final["decision_ts"] if final else None,
            "exit_ts": final["ts"] if final else None,
            "exit_price": exit_px, "exit_kind": final["kind"] if final else None,
            "n_reduce_limit": sum(1 for r in closes
                                  if r["kind"] == "LIMIT_FILL"),
            "gross_pnl": gross, "fees": fees,
            "fees_maker": fees_maker, "fees_taker": fees - fees_maker,
            "funding": funding,
            "slippage_cost": slip, "net_pnl": net,
            "r_multiple": (net / risk) if risk > 0 else None,
            "hold_hours": ((final["ts"] - open_row["ts"]) / MS_PER_HOUR
                           if final else None),
            "reason_open": open_row["reason"],
            "reason_exit": final["reason"] if final else None,
            "reasons_reduce": [r["reason"] for r in closes
                               if r["kind"] in ("REDUCE", "LIMIT_FILL")],
            "in_segment_open": bool(
                b["ts_lo"] is not None
                and b["ts_lo"] <= open_row["decision_ts"] <= b["ts_hi"]),
        })
    return trades


__all__ = [
    "MS_15M", "MS_1H", "N_FINE_PER_COARSE", "BOUNDARY_1H_INDEX_FROZEN",
    "frame_15m", "first_extreme_of", "assert_aligned", "alignment_issues_ci",
    "ExecFrame", "build_exec_frame", "run_backtest_v4", "reconstruct_trades_v4",
    "BacktestResult", "SymbolCognition", "Broker", "RestingLimitOrder",
]
