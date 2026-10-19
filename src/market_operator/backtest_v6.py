"""规范(canonical)回放核心:v6 语义链唯一回放基础设施。

规范地位:本模块是唯一规范回放核心(canonical replay core)。语义合并
(refactor/semantics-unify)后,规范链的认知缓存、回放结果、成交-决策
因果断言、逐笔重构与段×成本指标统一由本模块提供;战役引擎(campaign)
与规范回放入口(scripts/run_backtest_v6.py)只从这里取。

内容来源(原样提升,语义零改动;历史轮次原文件冻结于 research/rounds/):
- research/rounds/v1/backtest.py:MS_PER_HOUR/MS_PER_DAY、SymbolCognition、
  prepare_cognition、BacktestResult、assert_fill_after_decision、
  compute_metrics(及其 _max_drawdown/_holding_stats/_breakdown);
- research/rounds/v4/backtest_v4.py:MS_15M/MS_1H/N_FINE_PER_COARSE/
  BOUNDARY_1H_INDEX_FROZEN、assert_aligned(1h⟷15m 完整覆盖契约)、
  alignment_issues_ci(dev/test 拐点 12320 落桶核查)、reconstruct_trades_v4
  (LIMIT_FILL 时代的账本重构:entry_mode/fill_wait/maker-taker 费用拆分);
- research/rounds/v5/backtest_v5.py:reconstruct_trades_v5(补 route/l_star/
  weak_bars/e6 质量量追溯字段)。

时间约定与因果纪律与归档轮次完全一致:决策 = 认知 bar 收盘(ledger
decision_ts = bar 标签),任何成交严格晚于决策(assert_fill_after_decision);
15m⟷1h 帧契约(assert_aligned:每根 1h 恰 4 根 900_000ms 子 bar,完整覆盖)。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

import pandas as pd

from .events import EventDetector
from .hypothesis import StructureView
from .research import split_boundary
from .structure import Params

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000
MS_15M = 900_000
MS_1H = 3_600_000
N_FINE_PER_COARSE = 4                      # 1h = 4 × 15m(exec_tf = 900s)
BOUNDARY_1H_INDEX_FROZEN = 12320           # dev/test 拐点(v3/v4 协议 §2)


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
    cap_skips: list = field(default_factory=list)       # v6:组合容量 FIFO 跳过明细
    campaign_states: dict = field(default_factory=dict)  # v6:白盒内省(各战役状态)


def assert_fill_after_decision(ledger: list) -> None:
    """正确性硬约束:任何成交时间严格晚于决策时间(bar 标签递增)。"""
    for row in ledger:
        if row["ts"] <= row["decision_ts"]:
            raise AssertionError(
                f"fill ts {row['ts']} <= decision ts {row['decision_ts']} "
                f"(row seq={row['seq']})")


# ---------------------------------------------------------------------------
# 交易重构(共享账本 → 逐笔;LIMIT_FILL 时代口径 + 路由追溯字段)
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


__all__ = [
    "MS_PER_HOUR", "MS_PER_DAY", "MS_15M", "MS_1H", "N_FINE_PER_COARSE",
    "BOUNDARY_1H_INDEX_FROZEN",
    "SymbolCognition", "prepare_cognition", "BacktestResult",
    "assert_fill_after_decision", "assert_aligned", "alignment_issues_ci",
    "reconstruct_trades_v4", "reconstruct_trades_v5", "compute_metrics",
]
