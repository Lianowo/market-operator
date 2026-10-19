"""阶段 5 独立回测引擎:多品种合并时间序回放 + 指标(docs/methodology.md §7/§8
交易规则,§9.2 成本先验 × cost_multiplier,阶段 5 资金与交易所约束规格)。

与事件研究(阶段 3)的分工:research.py 测"事件本身的信号"(理论头寸、无持仓
管理);本引擎测"§7+§8 全部规则落地的组合":事件 → Strategy(GO 白名单
E1|short/E6|short)→ PositionManager(§8 六条)→ Broker(t+1 开盘撮合、
费用/资金费/滑点、300 USDT 权益、名义上限、最小下单量、强平保护)。

正确性硬约束:决策在 bar t 收盘(账本 decision_ts = 决策 bar 标签),成交在
该品种下一根 bar 开盘(fill_ts = 成交 bar 标签),Broker._fill 断言行内
fill_ts > decision_ts,违者立即 raise;引擎另有全账本断言(见
assert_fill_after_decision)。

认知预计算(prepare_cognition):EventDetector 是 bar 流的纯函数,与撮合/成本
无关;对每品种跑一次,缓存逐 bar 的 atr、已知事件(known_index 索引)、已知
pivot、事件 bar 的 StructureView,9 次回放(3 成本 × 3 段)共用——不改变任何
因果语义(策略/持仓不反哺认知),只是避免重复同一确定性计算。回测中的事件流
与 research.collect_events 同源同路(EventDetector.update 逐 bar)。

段(§9.1-2 同一规则,research.split_boundary):每品种前 dev_frac(0.7)为
dev。OPEN 决策仅当决策 bar 索引落在段内才启用;持仓管理(§8)对已有仓位在
任意 bar 继续(持仓生命周期完整);交易按"OPEN 决策 bar ∈ 段"归属;权益曲线
全程记录、指标按段切片(穿越段尾的持仓如实报告 n_open_end)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .broker import Broker
from .events import EventDetector
from .hypothesis import StructureView
from .position import PositionManager
from .research import split_boundary
from .strategy import GO_MECHANISMS, OrderKind, Strategy
from .structure import Params

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000


# ---------------------------------------------------------------------------
# 认知预计算(bar 流的纯函数,成本无关,9 次回放共用)
# ---------------------------------------------------------------------------


@dataclass
class SymbolCognition:
    """单品种逐 bar 认知缓存(EventDetector 同源回放)。"""
    symbol: str
    bars: list                       # (ts, o, h, l, c, v) 严格按时间升序
    atrs: list                       # 每 bar atr(t)
    events_by: dict                  # bar index → [MarketEvent](known_index=该 bar)
    pivots_by: dict                  # bar index → [Pivot](known_index=该 bar)
    views: dict                      # bar index → StructureView(仅事件 bar)


def prepare_cognition(df: pd.DataFrame, params: Params, symbol: str,
                      enable_e7: bool = False) -> SymbolCognition:
    """严格时间序回放 EventDetector,缓存逐 bar 认知产物。

    与 research.collect_events 同一检测路径;本缓存额外保留逐 bar atr 与
    每 bar 新确认 pivot(§8.3 移动防线输入)。`enable_e7` 打开 v2 E7 发射
    (docs/v2-protocol.md;v1 默认关,行为不变)。
    """
    detector = EventDetector(params, enable_e7=enable_e7)
    bars: list = []
    atrs: list = []
    events_by: dict = {}
    pivots_by: dict = {}
    views: dict = {}
    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        bar = (int(row.ts), float(row.open), float(row.high), float(row.low),
               float(row.close), float(row.volume))
        n_pivots_before = len(detector.structure.pivots)
        emitted = detector.update(bar)
        s = detector.structure
        t = s.bars_seen - 1
        bars.append(bar)
        atrs.append(s.atr())
        new_pivots = list(s.pivots[n_pivots_before:])
        if new_pivots:
            pivots_by[t] = new_pivots
        if emitted:
            events_by[t] = list(emitted)
            views[t] = StructureView(
                regime=s.regime(), zones=s.zones, atr=s.atr(),
                pivots=s.pivots[-params.range_max_pivots:])
    return SymbolCognition(symbol=symbol, bars=bars, atrs=atrs,
                           events_by=events_by, pivots_by=pivots_by, views=views)


# ---------------------------------------------------------------------------
# 回放结果
# ---------------------------------------------------------------------------


@dataclass
class BacktestResult:
    segment: str
    cost_multiplier: float
    equity0: float
    ledger: list = field(default_factory=list)          # broker 账本(SKIP 含)
    equity_rows: list = field(default_factory=list)     # (ts, equity, cash, upnl, funding, n_open)
    trades: list = field(default_factory=list)          # reconstruct_trades 输出
    counters: dict = field(default_factory=dict)
    seg_bounds: dict = field(default_factory=dict)      # symbol → {lo, hi, n}
    contracts: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


def run_backtest(cogs: dict, *, segment: str, dev_frac: float, equity0: float,
                 risk_frac: float, leverage_cap: float, taker_fee: float,
                 slippage_per_side: float, funding_per_8h: float,
                 cost_multiplier: float, contracts: dict,
                 params: Params, strategy: Optional[Strategy] = None,
                 whitelist: Optional[frozenset] = None) -> BacktestResult:
    """合并三品种 bar 为统一时间序列逐 ts 推进(同 ts 按 symbol 字典序)。

    每根 bar 的因果序:① t+1 开盘撮合(broker.on_bar_open,含上一 bar 决策的
    OPEN/REDUCE/EXIT/强平)→ ② 认知(EventDetector 等价缓存到该 bar 收盘)
    → ③ mark-to-market(资金费计提/权益/近似强平)→ ④ §8 持仓管理裁决
    (PositionManager)→ ⑤ §7/GO 策略决策(Strategy;段内才启用 OPEN)。
    决策 decision_ts = 本 bar 标签;成交必在其后(正确性硬约束,Broker 断言)。
    """
    if segment not in ("full", "dev", "test"):
        raise ValueError(f"unknown segment {segment}")
    symbols = sorted(cogs)
    seg_bounds: dict = {}
    ts_index: dict = {}                                  # symbol → ts → bar index
    for sym in symbols:
        bars = cogs[sym].bars
        n = len(bars)
        b = split_boundary(n, dev_frac)
        lo, hi = (0, n) if segment == "full" else ((0, b) if segment == "dev"
                                                   else (b, n))
        seg_bounds[sym] = {"lo": lo, "hi": hi, "n": n,
                           "ts_lo": bars[lo][0] if lo < n else None,
                           "ts_hi": bars[hi - 1][0] if hi > 0 else None}
        ts_index[sym] = {bar[0]: i for i, bar in enumerate(bars)}

    broker = Broker(equity=equity0, risk_frac=risk_frac, leverage_cap=leverage_cap,
                    taker_fee=taker_fee, slippage_per_side=slippage_per_side,
                    funding_per_8h=funding_per_8h, contracts=contracts,
                    cost_multiplier=cost_multiplier)
    # strategy/whitelist 为 v2 入口(docs/v2-protocol.md):None ⇒ v1 行为
    # (GO 白名单 Strategy,r_min=1.5);go_events 计数集与策略白名单保持一致。
    go_set = GO_MECHANISMS if whitelist is None else whitelist
    strategy = strategy if strategy is not None else Strategy()
    pos_mgr = PositionManager(params, allow_add=False)   # E4 NO-GO ⇒ 加仓关闭
    equity_rows: list = []
    counters = {"go_events": 0, "open_intents": 0, "opens_filled": 0,
                "min_size_skips": 0, "liquidations": 0}

    all_ts = sorted({ts for sym in symbols for ts in ts_index[sym]})
    for ts in all_ts:
        for sym in symbols:                              # 同 ts 按 symbol 字典序
            i = ts_index[sym].get(ts)
            if i is None:
                continue
            cog = cogs[sym]
            bar = cog.bars[i]
            # ① t+1 开盘撮合
            fills = broker.on_bar_open(sym, ts, bar[1])
            for row in fills:
                if row["kind"] == "OPEN":
                    m = row["meta"]
                    pos_mgr.open(sym, int(m["direction"]), row["price"],
                                 float(m["invalid"]), m.get("target_1"),
                                 str(m.get("event_id")), ts, i)
                    counters["opens_filled"] += 1
                elif row["kind"] in ("EXIT", "LIQUIDATION"):
                    pos_mgr.positions.pop(sym, None)     # 强平情形同步认知层
                elif row["kind"] == "SKIP" and row["reason_code"] == "MIN_SIZE":
                    counters["min_size_skips"] += 1
            # ② 认知(确定性缓存,等价于 EventDetector 实时推进到本 bar 收盘)
            new_events = cog.events_by.get(i, [])
            new_pivots = cog.pivots_by.get(i, [])
            # ③ 盯市(计提资金费、权益、近似强平入队)
            broker.mark(sym, ts, bar[4])
            counters["liquidations"] = broker.n_liquidations
            # ④ §8 持仓管理(破位/反向E6/减仓/移动防线/时间止损)
            mgmt = pos_mgr.on_bar(sym, bar, cog.atrs[i], new_pivots, new_events)
            for intent in mgmt:
                broker.submit(intent, decision_ts=ts)
            # ⑤ §7 策略(仅段内启用 OPEN;GO 白名单由 Strategy 内部裁决)
            lo, hi = seg_bounds[sym]["lo"], seg_bounds[sym]["hi"]
            if lo <= i < hi:
                counters["go_events"] += sum(
                    1 for e in new_events
                    if (e.id, e.direction) in go_set)
                state = broker.position_of(sym)
                for intent in strategy.on_bar(sym, bar, cog.views.get(i),
                                              new_events,
                                              open_position_state=state):
                    if intent.kind is OrderKind.OPEN:
                        hyp = strategy._taken.get(sym)   # 已采纳假设(记录字段)
                        counters["open_intents"] += 1
                        broker.submit(
                            intent, decision_ts=ts,
                            direction=hyp.direction, invalid=hyp.invalid,
                            target_1=hyp.target_1, event_id=hyp.event_id,
                            meta={"direction": hyp.direction,
                                  "regime": hyp.regime.value,
                                  "r_mult": hyp.r_mult})
                    else:                                # 策略层 v1 只产 OPEN
                        broker.submit(intent, decision_ts=ts)
        equity_rows.append((ts, broker.equity_mtm(), broker.cash,
                            broker.unrealized(), broker.funding_accrued(),
                            len(broker.positions)))

    res = BacktestResult(segment=segment, cost_multiplier=cost_multiplier,
                         equity0=equity0, ledger=list(broker.ledger),
                         equity_rows=equity_rows,
                         trades=reconstruct_trades(broker.ledger, seg_bounds),
                         counters=counters, seg_bounds=seg_bounds,
                         contracts={s: vars(c) for s, c in contracts.items()})
    assert_fill_after_decision(res.ledger)
    return res


def assert_fill_after_decision(ledger: list) -> None:
    """正确性硬约束:任何成交时间严格晚于决策时间(bar 标签递增)。"""
    for row in ledger:
        if row["ts"] <= row["decision_ts"]:
            raise AssertionError(
                f"fill ts {row['ts']} <= decision ts {row['decision_ts']} "
                f"(row seq={row['seq']})")


# ---------------------------------------------------------------------------
# 交易重构(账本 → 逐笔交易;qty_frac 账本对账 OPEN 1.0 − REDUCE 0.5 − EXIT 0.5)
# ---------------------------------------------------------------------------


def reconstruct_trades(ledger: list, seg_bounds: dict) -> list:
    by_tid: dict = {}
    for row in ledger:
        if row["kind"] == "SKIP" or row["trade_id"] is None:
            continue
        by_tid.setdefault(row["trade_id"], []).append(row)

    trades: list = []
    for tid, rows in by_tid.items():
        open_row = rows[0]
        assert open_row["kind"] == "OPEN", f"{tid} first row {open_row['kind']}"
        m = open_row["meta"]
        sym = open_row["symbol"]
        direction = -1 if open_row["side"] == "sell" else +1
        closes = [r for r in rows if r["kind"] in ("REDUCE", "EXIT", "LIQUIDATION")]
        closed = bool(closes) and closes[-1]["kind"] in ("EXIT", "LIQUIDATION")
        final = closes[-1] if closes else None
        qty_closed = sum(r["qty"] for r in closes)
        exit_px = (sum(r["qty"] * r["price"] for r in closes) / qty_closed
                   if qty_closed > 0 else None)
        gross = sum(r["realized_pnl"] for r in rows)
        fees = sum(r["fee"] for r in rows)
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
            "qty": open_row["qty"],
            "notional": open_row["qty"] * open_row["price"],
            "used_leverage": open_row["used_leverage"],
            "closed": closed,
            "close_decision_ts": final["decision_ts"] if final else None,
            "exit_ts": final["ts"] if final else None,
            "exit_price": exit_px, "exit_kind": final["kind"] if final else None,
            "gross_pnl": gross, "fees": fees, "funding": funding,
            "slippage_cost": slip, "net_pnl": net,
            "r_multiple": (net / risk) if risk > 0 else None,
            "hold_hours": ((final["ts"] - open_row["ts"]) / MS_PER_HOUR
                           if final else None),
            "reason_open": open_row["reason"],
            "reason_exit": final["reason"] if final else None,
            "reasons_reduce": [r["reason"] for r in closes
                               if r["kind"] == "REDUCE"],
            "in_segment_open": bool(
                b["ts_lo"] is not None
                and b["ts_lo"] <= open_row["decision_ts"] <= b["ts_hi"]),
        })
    return trades


# ---------------------------------------------------------------------------
# 指标(每段 × 成本压力分别)
# ---------------------------------------------------------------------------


def _max_drawdown(equity: list) -> dict:
    """峰值回撤:幅度(USDT 与 %)、峰→谷 ts 与持续天数。"""
    peak, peak_ts = None, None
    best = {"max_dd_usdt": 0.0, "max_dd_pct": 0.0,
            "peak_ts": None, "trough_ts": None, "dd_days": 0.0}
    for ts, eq in equity:
        if peak is None or eq > peak:
            peak, peak_ts = eq, ts
        dd = peak - eq
        if dd > best["max_dd_usdt"]:
            best = {"max_dd_usdt": dd,
                    "max_dd_pct": (dd / peak) if peak > 0 else 0.0,
                    "peak_ts": peak_ts, "trough_ts": ts,
                    "dd_days": (ts - peak_ts) / MS_PER_DAY}
    return best


def _holding_stats(trades: list) -> dict:
    hs = sorted(t["hold_hours"] for t in trades if t["hold_hours"] is not None)
    if not hs:
        return {"median_h": None, "p90_h": None}
    mid = len(hs) // 2
    med = hs[mid] if len(hs) % 2 else 0.5 * (hs[mid - 1] + hs[mid])
    p90 = hs[max(0, math.ceil(0.9 * len(hs)) - 1)]     # 最近秩口径
    return {"median_h": med, "p90_h": p90}


def _breakdown(trades: list, key: str) -> dict:
    out: dict = {}
    for t in trades:
        k = str(t[key])
        g = out.setdefault(k, {"n": 0, "net": 0.0, "wins": 0})
        g["n"] += 1
        g["net"] += t["net_pnl"]
        g["wins"] += 1 if t["net_pnl"] > 0 else 0
    for g in out.values():
        g["expectancy"] = g["net"] / g["n"]
        g["winrate"] = g["wins"] / g["n"]
        del g["wins"]
    return out


def compute_metrics(res: BacktestResult) -> dict:
    """段内指标:权益切片按段 ts 范围;交易按 OPEN 决策 bar ∈ 段归属。"""
    syms = sorted(res.seg_bounds)
    ts_lo = min(b["ts_lo"] for b in res.seg_bounds.values() if b["ts_lo"])
    ts_hi = max(b["ts_hi"] for b in res.seg_bounds.values() if b["ts_hi"])
    eq_rows = [r for r in res.equity_rows if ts_lo <= r[0] <= ts_hi]
    # 交易归属:OPEN 决策 bar ∈ 段(每品种各自 [lo,hi) bar 索引区间)
    trades = [t for t in res.trades if t["in_segment_open"]]
    closed = [t for t in trades if t["closed"]]

    eq_series = [(r[0], r[1]) for r in eq_rows]
    eq_start = res.equity0 if not eq_series else eq_series[0][1]
    eq_end = res.equity0 if not eq_series else eq_series[-1][1]
    span_days = ((ts_hi - ts_lo) / MS_PER_DAY) if ts_hi > ts_lo else 0.0
    net_pnl = sum(t["net_pnl"] for t in trades)

    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] <= 0]
    gross_w = sum(t["net_pnl"] for t in wins)
    gross_l = -sum(t["net_pnl"] for t in losses)
    rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]

    dd = _max_drawdown(eq_series)
    ann = None
    if span_days > 0 and eq_start > 0:
        ann = (max(eq_end, 0.0) / eq_start) ** (365.25 / span_days) - 1.0

    monthly: list = []
    cur_key, cur_end, prev_end = None, None, res.equity0
    for ts, eq, *_ in eq_rows:
        key = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")
        if key != cur_key:
            if cur_key is not None:
                monthly.append({"month": cur_key, "equity_end": cur_end,
                                "pnl": cur_end - prev_end})
                prev_end = cur_end
            cur_key = key
        cur_end = eq
    if cur_key is not None:
        monthly.append({"month": cur_key, "equity_end": cur_end,
                        "pnl": cur_end - prev_end})

    exposure = (sum(1 for r in eq_rows if r[5] > 0) / len(eq_rows)
                if eq_rows else 0.0)

    return {
        "segment": res.segment, "cost_multiplier": res.cost_multiplier,
        "ts_lo": ts_lo, "ts_hi": ts_hi, "span_days": span_days,
        "equity_start": eq_start, "equity_end": eq_end,
        "net_pnl": net_pnl, "return_pct": net_pnl / res.equity0,
        "ann_return": ann, **dd,
        "n_trades": len(trades), "n_closed": len(closed),
        "n_open_end": len(trades) - len(closed),
        "winrate": (len(wins) / len(closed)) if closed else None,
        "profit_factor": (gross_w / gross_l) if gross_l > 0 else None,
        "payoff_ratio": ((gross_w / len(wins)) / (gross_l / len(losses)))
        if wins and losses else None,
        "expectancy_usdt": (net_pnl / len(trades)) if trades else None,
        "expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "holding": _holding_stats(trades),
        "exposure_ratio": exposure,
        "fees_total": sum(t["fees"] for t in trades),
        "slippage_total": sum(t["slippage_cost"] for t in trades),
        "funding_total": sum(t["funding"] for t in trades),
        "by_direction": _breakdown(trades, "direction"),
        "by_regime": _breakdown(trades, "regime"),
        "by_symbol": _breakdown(trades, "symbol"),
        "monthly": monthly,
        "counters": dict(res.counters),
    }
