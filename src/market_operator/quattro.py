"""v7 对照实验:quattro(Donchian 20-bar 突破 + 4 单元加梯)逐字复刻。

地位:**对照实验,不是 market-operator 的候选语义**(docs/v7-protocol.md
冻结原文;research-log #13 先行登记)。准据优先级:v7-protocol §1 > 上游
README §2(upstream/crypto_systematic_research/2_strategies/01_quattro_
donchian/,MIT)> 上游 backtest.py(语义澄清器;只复刻语义,不复制代码)。
不重置、不调参、不优化 —— 照抄就是照抄。

规则(冻结协议 §1 逐字):
- 标的 BTCUSDT 永续 4H,long-only。
- 入场:close > 前 20 根 high(rolling(20).max().shift(1),不含当前 bar)
  且日 EMA200 上升(EMA200[d] > EMA200[d-20],日帧由 4h UTC 聚合,EMA 只用
  已完成的日 —— 4h bar 可见的最近日 EMA 为其 UTC 日之前最后一个完成日)
  ⇒ 次 4H 开盘市价。N = ATR(14)(Wilder,ewm(alpha=1/14, adjust=False))
  在信号 bar 取值,整序列固定。
- 加梯:unit2@入场+0.5N、unit3@+1.0N、unit4@+1.5N;wick(high)触及 ⇒
  次开盘加(协议对 upstream bar 内 trigger 价成交的 venue 映射);每单元
  风险 2% 账户(信号时快照),止损 = 单元入场 − 2N;合计最坏情景 ≥ 5% 账
  户 ⇒ 硬强平(wick)。
- 出场(同上上游评估顺序):(a) 合计浮亏触及 5% 账户 ⇒ 硬强平(wick);
  (b) 单元棘轮止损 = 最新单元入场 − 2N,只在加仓时上移(wick);(c)
  close < 前 10 根 low ⇒ 全平次开盘。无目标。
- 成本/撮合/记账:沿用系统 broker(taker 0.06%/边 + 滑点 0.02%/边 + 资金
  费按持仓毫秒比例;Bitget 合约规格 step/min;杠杆 cap = 系统 5.0),300
  USDT 初始;拒单(step/min/leverage)如实盘账并如实记。

venue 执行映射声明(协议允许的 venue sizing 落地,非规则改动):
1. upstream backtest.py 无成本、无滑点、加梯在 trigger 价 bar 内成交;
   本复刻按冻结协议:加梯 wick 触及 ⇒ 次 4H 开盘市价成交(taker+滑点)。
   单元入场价 = 实际成交(滑点后),棘轮/硬平层级按实际成交价与数量重算。
2. wick 止损经 broker.execute_protective_stop(stop-market)当 bar 成交:
   成交价 = min(open, stop)·(1−滑点)(缺口保守成交;upstream 恒按 stop 价
   成交,缺口处略乐观,如实声明差异)。站立止损位 = max(棘轮, 硬平层级)。
3. 硬平标签顺序照抄 upstream:bar low 同时触及棘轮与 5% 层级 ⇒ 记
   hard_stop_5pct(upstream 先检硬平;这也是其 40% 桶的主要来源)。唯一
   价格差异:hard 层级 < 棘轮层级且同 bar 一并触及时,upstream 按更深的
   hard 层级成交,本复刻按站立位(棘轮)成交 —— 罕见(单 bar 贯穿
   0.75N~2N),如实声明;4 单元时两层级重合,完全一致。
4. 杠杆 cap = 系统 5.0(upstream MAX_LEVERAGE=20);300 USDT 下 min_size
   拒单如实记;ADD 被 cap 缩量(部分成交)时如实入账(upstream 决策侧
   超 cap 即整单元不加 —— cap 区行为差异,声明)。
5. upstream 数据末棒仍在场 ⇒ 按末棒收盘强平(无成本)记 still_open_at_
   end;本复刻于末棒开盘强平(含成本),标签一致,价格差异声明。
6. 日 EMA200 由本地 4h 聚合帧聚合(窗口起点播种);本地数据窗 2 年
   (2024-10→2026-10),upstream 4.1 年且 EMA 预热自窗口前 1 年 —— 预热
   成熟度差异属数据窗差异,报告如实声明,不主张复现 upstream +1107%。
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .broker import Broker, OrderIntent, OrderKind

# ---------------------------------------------------------------------------
# 冻结规则常量(v7-protocol §1 / 上游 README §2 / backtest.py 逐字)
# ---------------------------------------------------------------------------

SYMBOL = "BTCUSDT"
DONCH_ENTRY = 20            # 入场 Donchian 窗口(前 20 根 high,shift(1))
DONCH_EXIT = 10             # 出场 Donchian 窗口(前 10 根 low,shift(1))
ATR_N = 14                  # N = ATR(14),Wilder
EMA_SPAN = 200              # 日 EMA200
EMA_RISE_DAYS = 20          # EMA200[d] > EMA200[d-20]
RISK_PCT = 0.02             # 每单元风险 2% 账户
HARD_STOP_PCT = 0.05        # 合计最坏情景 5% 账户硬强平
ATR_MULT_STOP = 2.0         # 单元止损距离 2N
PYRAMID_STEP = 0.5          # 加梯步长 0.5N
MAX_UNITS = 4               # 最大 4 单元
WARMUP = 250                # upstream backtest.py 回放起点(4h bar 数)

STEP_4H_MS = 4 * 3_600_000
DAY_MS = 86_400_000

EXIT_HARD = "hard_stop_5pct"
EXIT_TRAIL = "atr_stop_trailing"
EXIT_DONCHIAN = "donchian_exit"
EXIT_END = "still_open_at_end"
EXIT_REASONS_3 = (EXIT_HARD, EXIT_DONCHIAN, EXIT_TRAIL)  # 上游三桶


# ---------------------------------------------------------------------------
# 指标(公式与 upstream 逐字一致;独立实现)
# ---------------------------------------------------------------------------


def compute_indicators(df4h: pd.DataFrame) -> pd.DataFrame:
    """Donchian(20, shift1) high / (10, shift1) low 与 ATR(14, Wilder)。

    TR 首 bar 无 prev close:与 upstream 相同,pandas 横向 max(skipna)使
    TR[0] = high−low;ATR = TR.ewm(alpha=1/14, adjust=False).mean()。
    """
    df = df4h.copy()
    df["donch_high_20"] = df["high"].rolling(DONCH_ENTRY).max().shift(1)
    df["donch_low_10"] = df["low"].rolling(DONCH_EXIT).min().shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1.0 / ATR_N, adjust=False).mean()
    return df


def aggregate_daily(df4h: pd.DataFrame) -> pd.DataFrame:
    """4h → UTC 日帧(open=first/high=max/low=min/close=last/volume=sum)。

    ts 标签 = 日起点毫秒(UTC 对齐桶);首尾边界可为不完整日,如实保留。
    """
    day = (df4h["ts"].to_numpy() // DAY_MS) * DAY_MS
    g = df4h.assign(day=day).groupby("day", sort=True, as_index=False)
    out = g.agg(open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum"))
    return out.rename(columns={"day": "ts"})


def daily_ema_visibility(df4h: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """对齐到 4h 行的 (ema_now, ema_ref):只用已完成的日,不偷看当日收盘。

    4h bar(UTC 日 D)可见的最近日 EMA = 日 D−1(最后一个完成日)的 EMA200;
    ema_ref = 再往前 EMA_RISE_DAYS 个完成日的 EMA200。日序列不足 ⇒ NaN。
    等价 upstream 的 reindex(+1 日, ffill) 可见性约定。
    """
    daily = aggregate_daily(df4h)
    ema = daily["close"].ewm(span=EMA_SPAN, adjust=False).mean().to_numpy()
    day_labels = daily["ts"].to_numpy()
    ts = df4h["ts"].to_numpy()
    bar_day = (ts // DAY_MS) * DAY_MS
    pos = np.searchsorted(day_labels, bar_day) - 1      # 最后一个完成日的位置
    ema_now = np.full(len(ts), np.nan)
    ema_ref = np.full(len(ts), np.nan)
    ok = pos >= 0
    ema_now[ok] = ema[pos[ok]]
    ok_ref = pos >= EMA_RISE_DAYS
    ema_ref[ok_ref] = ema[pos[ok_ref] - EMA_RISE_DAYS]
    return ema_now, ema_ref


# ---------------------------------------------------------------------------
# 回放引擎(逐 bar;决策 = bar 收盘,市价 = 次 bar 开盘;wick 止损当 bar)
# ---------------------------------------------------------------------------


def run(df4h: pd.DataFrame, broker: Broker, symbol: str = SYMBOL) -> dict:
    """在系统 broker 上复刻 quattro。返回 {sequences, equity_rows, counters}。

    sequences:逐金字塔序列明细(上游 trades 口径 + 成本拆分);equity_rows:
    (ts, equity_mtm, cash, unrealized, funding_accrued) 每 4h bar 一行(自
    WARMUP 起,回放窗口内逐 bar 盯市)。
    """
    df = compute_indicators(df4h)
    ema_now, ema_ref = daily_ema_visibility(df4h)
    ts_a = df["ts"].to_numpy(dtype=np.int64)
    o_a = df["open"].to_numpy()
    h_a = df["high"].to_numpy()
    l_a = df["low"].to_numpy()
    c_a = df["close"].to_numpy()
    dh_a = df["donch_high_20"].to_numpy()
    dl_a = df["donch_low_10"].to_numpy()
    atr_a = df["atr14"].to_numpy()
    n = len(df)
    if n <= WARMUP + 1:
        raise ValueError(f"frame too short: {n} bars <= WARMUP+1={WARMUP + 1}")

    sequences: list[dict] = []
    equity_rows: list[tuple] = []
    counters = {
        "entry_signals": 0,
        "entry_min_size_skips": 0,
        "add_min_size_skips": 0,
        "add_clipped_fills": 0,
    }
    seq: dict | None = None          # 进行中的金字塔序列状态
    seq_id = 0

    def _new_pending(signal_i: int) -> dict:
        nonlocal seq_id
        seq_id += 1
        return {
            "seq_id": seq_id, "trade_id": None, "open_filled": False,
            "signal_i": signal_i, "signal_ts": int(ts_a[signal_i]),
            "N": float(atr_a[signal_i]),
            "acct_at_entry": broker.equity_mtm(),   # 信号时刻账户(平持仓 ⇒ cash)
            "units": [],                            # fill 行:price/qty/ts
            "original_entry": None,
            "base_qty": None,
            "common_stop": None,                    # 棘轮:最新单元入场 − 2N
            "hard_level": None,                     # 合计亏 5% acct 的价格层级
            "used_leverage_max": None,
            "add_skips": 0, "clipped_adds": 0,
        }

    def _hard_level(units: list, acct: float) -> float:
        """合计浮亏 = HARD_STOP_PCT·acct 的价格层级(upstream 同式重算)。"""
        tsize = sum(u["qty"] for u in units)
        wsum = sum(u["price"] * u["qty"] for u in units)
        return (wsum - HARD_STOP_PCT * acct) / tsize

    def _close_seq(row: dict, reason: str) -> None:
        """序列平仓记账(行来自 broker 成交价,含费用/资金费结算)。"""
        nonlocal seq
        rows = [r for r in broker.ledger if r.get("trade_id") == seq["trade_id"]]
        gross = sum(float(r["realized_pnl"]) for r in rows)
        fees = sum(float(r["fee"]) for r in rows)
        funding = -sum(float(r["funding_delta"]) for r in rows)
        net = gross - fees - funding
        units = seq["units"]
        tsize = sum(u["qty"] for u in units)
        avg_entry = sum(u["price"] * u["qty"] for u in units) / tsize
        acct = seq["acct_at_entry"]
        sequences.append({
            "seq_id": seq["seq_id"],
            "trade_id": seq["trade_id"],
            "signal_ts": seq["signal_ts"],
            "entry_ts": int(units[0]["ts"]),
            "exit_ts": int(row["ts"]),
            "first_entry_price": units[0]["price"],
            "avg_entry_price": avg_entry,
            "exit_price": float(row["price"]),
            "num_units": len(units),
            "total_size_btc": tsize,
            "unit_entries": [u["price"] for u in units],
            "unit_qtys": [u["qty"] for u in units],
            "unit_ts": [u["ts"] for u in units],
            "original_atr": seq["N"],
            "acct_at_entry": acct,
            "first_unit_risk": acct * RISK_PCT,
            "total_risk": len(units) * acct * RISK_PCT,
            "gross_pnl": gross,
            "fees": fees,
            "funding": funding,
            "pnl_usd": net,
            "pnl_pct": net / acct * 100.0,
            "R_total": net / (acct * RISK_PCT),
            "duration_h": (int(row["ts"]) - int(units[0]["ts"])) / 3_600_000,
            "exit_reason": reason,
            "account": float(row["cash_after"]),
            "used_leverage_max": seq["used_leverage_max"],
            "add_min_size_skips": seq["add_skips"],
            "add_clipped_fills": seq["clipped_adds"],
        })
        seq = None

    def _on_fill_rows(rows: list) -> None:
        """on_bar_open 返回行分类:序列开/加/平/拒单记账。"""
        nonlocal seq
        for row in rows:
            kind = row["kind"]
            meta = row.get("meta") or {}
            if kind == "SKIP":
                if meta.get("quattro_role") == "entry":
                    counters["entry_min_size_skips"] += 1
                    seq = None                     # 入场拒单:序列未成立
                elif meta.get("quattro_role") == "pyramid":
                    if seq is not None:
                        seq["add_skips"] += 1
                    counters["add_min_size_skips"] += 1
                continue
            if kind == "OPEN":
                if seq is None or not math.isfinite(seq["N"]):
                    raise AssertionError("OPEN filled without pending sequence")
                price = float(row["price"])
                qty = float(row["qty"])
                if row["used_leverage"] is not None:
                    seq["used_leverage_max"] = float(row["used_leverage"])
                seq.update(trade_id=row["trade_id"], open_filled=True,
                           original_entry=price, base_qty=qty,
                           common_stop=price - ATR_MULT_STOP * seq["N"])
                seq["units"].append({"price": price, "qty": qty,
                                     "ts": int(row["ts"])})
                seq["hard_level"] = _hard_level(seq["units"],
                                                seq["acct_at_entry"])
            elif kind == "ADD":
                if seq is None:
                    raise AssertionError("ADD filled without open sequence")
                price = float(row["price"])
                qty = float(row["qty"])
                if qty < seq["base_qty"] - 1e-12:
                    seq["clipped_adds"] += 1
                    counters["add_clipped_fills"] += 1
                seq["units"].append({"price": price, "qty": qty,
                                     "ts": int(row["ts"])})
                # 棘轮只在加仓时上移;硬平层级按实际成交重算;站立位取两者高
                new_stop = price - ATR_MULT_STOP * seq["N"]
                if new_stop > seq["common_stop"]:
                    seq["common_stop"] = new_stop
                seq["hard_level"] = _hard_level(seq["units"],
                                                seq["acct_at_entry"])
                broker.update_protective_invalid(
                    symbol, max(seq["common_stop"], seq["hard_level"]))
            elif kind in ("EXIT", "LIQUIDATION"):
                if seq is not None:
                    _close_seq(row, meta.get("quattro_exit", kind))
            else:
                raise AssertionError(f"unexpected ledger kind in quattro: {kind}")

    for i in range(WARMUP, n - 1):
        ts_i, o_i, h_i, l_i, c_i = (int(ts_a[i]), float(o_a[i]), float(h_a[i]),
                                    float(l_a[i]), float(c_a[i]))
        # 1) 次开盘撮合(pending:入场/加梯/Donchian 全平;先成交先算)
        _on_fill_rows(broker.on_bar_open(symbol, ts_i, o_i))

        pos = broker.position_of(symbol)
        if seq is not None and pos is not None:
            # 2) wick 止损:upstream 评估顺序 = 5% 硬平 → 单元棘轮(同 bar
            #    贯穿两级时记 hard_stop_5pct;站立位 = max(棘轮,硬平))
            stop = pos.invalid
            if stop is not None and l_i <= stop:
                reason = EXIT_HARD if l_i <= seq["hard_level"] else EXIT_TRAIL
                row = broker.execute_protective_stop(symbol, ts_i, o_i, h_i, l_i)
                _close_seq(row, reason)
            else:
                # 3) 加梯:wick 触及 trigger ⇒ 次开盘加(donchian 判定之前,
                #    与 upstream 同 bar 顺序一致)
                k = len(seq["units"])
                while k < MAX_UNITS and h_i >= (
                        seq["original_entry"]
                        + PYRAMID_STEP * k * seq["N"]):
                    intent = OrderIntent(
                        kind=OrderKind.ADD, symbol=symbol, qty_frac=1.0,
                        reason=(f"QUATTRO_PYRAMID|unit{k + 1} trigger "
                                f"{seq['original_entry'] + PYRAMID_STEP * k * seq['N']:.8g} "
                                f"touched; add at next open"),
                        ref_price=h_i, ts=ts_i)
                    broker.submit(intent, decision_ts=ts_i, meta={
                        "quattro_seq": seq["seq_id"], "quattro_role": "pyramid",
                        "unit": k + 1})
                    k += 1
                # 4) Donchian 出场:close < 前 10 根 low ⇒ 次开盘全平
                if i < n - 2 and c_i < dl_a[i]:
                    intent = OrderIntent(
                        kind=OrderKind.EXIT, symbol=symbol, qty_frac=None,
                        reason=("QUATTRO_DONCHIAN|close below prior "
                                f"{DONCH_EXIT}-bar low; exit all at next open"),
                        ref_price=c_i, ts=ts_i)
                    broker.submit(intent, decision_ts=ts_i, meta={
                        "quattro_seq": seq["seq_id"], "quattro_role": "exit",
                        "quattro_exit": EXIT_DONCHIAN})
        elif seq is None and pos is None:
            # 5) 入场信号:close > 前 20 根 high 且日 EMA200 上升 ⇒ 次开盘
            warm = not (math.isnan(dh_a[i]) or math.isnan(atr_a[i])
                        or math.isnan(ema_now[i]) or math.isnan(ema_ref[i]))
            if warm and c_i > dh_a[i] and ema_now[i] > ema_ref[i]:
                n_fixed = float(atr_a[i])                # N 固定整序列
                fill_pred = float(o_a[i + 1]) * (1.0 + broker.slippage)
                stop0 = fill_pred - ATR_MULT_STOP * n_fixed
                if stop0 > 0.0:
                    counters["entry_signals"] += 1
                    seq = _new_pending(i)
                    intent = OrderIntent(
                        kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                        reason=(f"QUATTRO_ENTRY|close {c_i:.8g} > prior "
                                f"{DONCH_ENTRY}-bar high {dh_a[i]:.8g}; "
                                f"daily EMA{EMA_SPAN} rising "
                                f"({ema_now[i]:.8g}>{ema_ref[i]:.8g})"),
                        ref_price=c_i, ts=ts_i)
                    broker.submit(intent, decision_ts=ts_i, direction=+1,
                                  invalid=stop0,
                                  meta={"quattro_seq": seq["seq_id"],
                                        "quattro_role": "entry",
                                        "N": n_fixed})
        broker.mark(symbol, ts_i, c_i)
        equity_rows.append((ts_i, broker.equity_mtm(), broker.cash,
                            broker.unrealized(), broker.funding_accrued()))

    # 数据末棒:pending 入场/加梯于末棒开盘成交;仍在场 ⇒ 末棒开盘强平
    i = n - 1
    _on_fill_rows(broker.on_bar_open(symbol, int(ts_a[i]), float(o_a[i])))
    pos = broker.position_of(symbol)
    if seq is not None and pos is not None:
        intent = OrderIntent(
            kind=OrderKind.EXIT, symbol=symbol, qty_frac=None,
            reason="QUATTRO_END_OF_DATA|position open at last bar; close "
                   "at last bar open (upstream: last close, cost-free)",
            ref_price=float(o_a[i]), ts=int(ts_a[i - 1]))
        broker.submit(intent, decision_ts=int(ts_a[i - 1]),
                      meta={"quattro_seq": seq["seq_id"],
                            "quattro_role": "exit", "quattro_exit": EXIT_END})
        _on_fill_rows(broker.on_bar_open(symbol, int(ts_a[i]), float(o_a[i])))
    broker.mark(symbol, int(ts_a[i]), float(c_a[i]))
    equity_rows.append((int(ts_a[i]), broker.equity_mtm(), broker.cash,
                        broker.unrealized(), broker.funding_accrued()))

    counters["sequences"] = len(sequences)
    counters["unit_fills"] = sum(s["num_units"] for s in sequences)
    counters["liquidations"] = broker.n_liquidations
    counters["broker_skips"] = dict(broker.n_skips)
    return {"sequences": sequences, "equity_rows": equity_rows,
            "counters": counters}


# ---------------------------------------------------------------------------
# 序列级绩效(对照口径;全部净额,含费用/资金费/滑点)
# ---------------------------------------------------------------------------


def summarize(result: dict, equity0: float) -> dict:
    seqs = result["sequences"]
    eq_rows = result["equity_rows"]
    final_eq = eq_rows[-1][1] if eq_rows else equity0
    nets = [s["pnl_usd"] for s in seqs]
    wins = [s for s in seqs if s["pnl_usd"] > 0]
    losses = [s for s in seqs if s["pnl_usd"] <= 0]
    gross_w = sum(s["pnl_usd"] for s in wins)
    gross_l = -sum(s["pnl_usd"] for s in losses)

    # 最大回撤:逐 bar 盯市(主口径)+ 逐序列平仓权益(upstream 口径)
    def _max_dd(curve: list) -> float:
        peak, dd = equity0, 0.0
        for v in curve:
            peak = max(peak, v)
            dd = min(dd, (v - peak) / peak)
        return dd

    mtm_dd = _max_dd([r[1] for r in eq_rows])
    closed_curve = [equity0]
    for s in seqs:
        closed_curve.append(closed_curve[-1] + s["pnl_usd"])
    closed_dd = _max_dd(closed_curve)

    exit_counts = {r: 0 for r in EXIT_REASONS_3}
    n_other = 0
    for s in seqs:
        if s["exit_reason"] in exit_counts:
            exit_counts[s["exit_reason"]] += 1
        else:
            n_other += 1
    n_seq = len(seqs)
    exit_share = {r: (c / n_seq if n_seq else 0.0)
                  for r, c in exit_counts.items()}

    rs = [s["R_total"] for s in seqs]
    win_r = [s["R_total"] for s in wins]
    loss_r = [s["R_total"] for s in losses]
    return {
        "n_sequences": n_seq,
        "n_unit_fills": sum(s["num_units"] for s in seqs),
        "avg_units": (sum(s["num_units"] for s in seqs) / n_seq
                      if n_seq else 0.0),
        "final_equity": final_eq,
        "net_pnl": sum(nets),
        "return_pct": (final_eq / equity0 - 1.0),
        "max_dd_mtm": mtm_dd,
        "max_dd_closed": closed_dd,
        "winrate": (len(wins) / n_seq if n_seq else 0.0),
        "profit_factor": (gross_w / gross_l if gross_l > 0
                          else (math.inf if gross_w > 0 else 0.0)),
        "expectancy_r": (sum(rs) / n_seq if n_seq else 0.0),
        "avg_win_r": (sum(win_r) / len(win_r) if win_r else None),
        "avg_loss_r": (sum(loss_r) / len(loss_r) if loss_r else None),
        "exit_counts": exit_counts,
        "exit_share": exit_share,
        "exit_other": n_other,
        "fees_total": sum(s["fees"] for s in seqs),
        "funding_total": sum(s["funding"] for s in seqs),
        "median_duration_h": (float(np.median([s["duration_h"] for s in seqs]))
                              if n_seq else None),
    }
