"""v7 对照实验 quattro 复刻的逐规则合成断言(tests/ 纪律:离线、合成、精确)。

覆盖(v7-protocol §1 / 上游 README §2 逐条):
- Donchian 20/10 通道 shift(1) 正确性(不含当前 bar);
- ATR(14) Wilder ewm(alpha=1/14) 播种/递推;
- 日 EMA200 过滤的日界对齐:日帧由 4h 聚合,EMA 只用已完成日,不偷看
  当日收盘;上升判定 = ema[d] > ema[d−20];
- 入场 = 次 4H 开盘(滑点后),qty 按 2%·acct/2N 向 step 下取整;
- 加梯三层级 +0.5N/+1.0N/+1.5N,wick 触及 ⇒ 次开盘加,至多 4 单元,
  单元等仓(qty 相等);
- 棘轮只在加仓时上移,不随 close 连续上移,从不下移;
- 5% 硬强平(同 bar 贯穿两级 ⇒ hard 标签,upstream 评估顺序);
- Donchian 出场 = close < 前 10 根 low ⇒ 次开盘全平;
- MIN_SIZE 拒单如实记、序列不成立;确定性双跑逐字段一致。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from market_operator import quattro
from market_operator.broker import Broker, ContractSpec

H4 = quattro.STEP_4H_MS
DAY = quattro.DAY_MS
T0 = 1_700_006_400_000          # 2023-11-15T00:00:00Z(4h/日网格对齐)
SLIP = 0.0002


def _spec() -> ContractSpec:
    return ContractSpec(symbol="TEST", size_min=0.0001, size_step=0.0001,
                        size_place=4, price_place=1, min_trade_usdt=5.0)


def _broker(equity: float = 300.0) -> Broker:
    return Broker(equity, risk_frac=quattro.RISK_PCT, leverage_cap=5.0,
                  taker_fee=0.0006, slippage_per_side=SLIP,
                  funding_per_8h=0.0001, contracts={"TEST": _spec()})


def _df(rows: list, start: int = T0) -> pd.DataFrame:
    ts = [start + k * H4 for k in range(len(rows))]
    return pd.DataFrame(
        {"ts": ts,
         "open": [r[0] for r in rows],
         "high": [r[1] for r in rows],
         "low": [r[2] for r in rows],
         "close": [r[3] for r in rows],
         "volume": [1.0] * len(rows)})


def _drift(n: int, c0: float = 100.0, step: float = 0.01) -> list:
    """n 根缓慢上移 bar(日 EMA 上升 ⇒ 过滤恒真;通道随漂移,永不突破)。"""
    rows = []
    prev = c0
    for k in range(n):
        c = c0 + step * k
        rows.append((prev, c + 0.5, prev - 0.5, c))
        prev = c
    return rows


def _atr_at(rows: list, i: int) -> float:
    return float(quattro.compute_indicators(_df(rows))["atr14"].iloc[i])


# ---------------------------------------------------------------------------
# 指标层:Donchian shift(1) / ATR Wilder / 日界对齐
# ---------------------------------------------------------------------------


def test_donchian_shift_excludes_current_bar():
    rows = [(float(k), float(k), float(k), float(k)) for k in range(45)]
    df = quattro.compute_indicators(_df(rows))
    dh, dl = df["donch_high_20"].to_numpy(), df["donch_low_10"].to_numpy()
    assert np.isnan(dh[:20]).all() and np.isnan(dl[:10]).all()
    assert dh[20] == 19.0                    # max(high[0..19]),不含 bar20
    assert dl[10] == 0.0                     # min(low[0..9])
    assert dh[39] == 38.0
    # bar39 自身 high 抬高不影响其通道(shift1);bar40 才纳入
    rows[39] = (39.0, 1000.0, 39.0, 39.0)
    df2 = quattro.compute_indicators(_df(rows))
    assert df2["donch_high_20"].iloc[39] == 38.0
    hi = [r[1] for r in rows]
    assert df2["donch_high_20"].iloc[40] == max(hi[20:40]) == 1000.0


def test_atr_wilder_seed_and_recursion():
    rows = [(10.0, 11.0, 9.0, 10.0),          # TR0 = 11−9 = 2(无 prev close)
            (10.0, 12.0, 10.0, 11.0),         # TR1 = max(2, 2, 0) = 2
            (11.0, 12.0, 8.0, 9.0),           # TR2 = max(4, 1, 3) = 4
            (9.0, 9.5, 8.5, 9.0)]             # TR3 = max(1, 0.5, 0.5) = 1
    atr = quattro.compute_indicators(_df(rows))["atr14"].to_numpy()
    a14 = 1.0 / 14
    assert atr[0] == pytest.approx(2.0)
    assert atr[1] == pytest.approx(atr[0] * (1 - a14) + 2.0 * a14)
    assert atr[2] == pytest.approx(atr[1] * (1 - a14) + 4.0 * a14)
    assert atr[3] == pytest.approx(atr[2] * (1 - a14) + 1.0 * a14)


def test_daily_aggregation_utc_buckets():
    rows = [(100 + k, 100.5 + k, 99.5 + k, 100.2 + k) for k in range(13)]
    daily = quattro.aggregate_daily(_df(rows))
    assert list(daily["ts"]) == [T0, T0 + DAY, T0 + 2 * DAY]
    assert daily["close"].iloc[0] == pytest.approx(100.2 + 5)   # 末日最后一根
    assert daily["open"].iloc[0] == pytest.approx(100.0)        # 日首根 open
    assert daily["volume"].sum() == pytest.approx(13.0)
    assert all(t % DAY == 0 for t in daily["ts"])


def test_daily_ema_visibility_never_peeks_current_day():
    """日 0..20 收 100(平),日 21 收 200:当日 bar 不可见当日收盘。"""
    rows = []
    for d in range(23):
        close = 200.0 if d >= 21 else 100.0
        for k in range(6):
            o = close
            rows.append((o, o + 1.0, o - 1.0,
                         close if k == 5 else o))
    ema_now, ema_ref = quattro.daily_ema_visibility(_df(rows))
    # 日 21 全部 6 根 bar:可见最近完成日 = 日 20 ⇒ ema 仍是 100(未升)
    day21 = np.arange(21 * 6, 22 * 6)
    assert np.allclose(ema_now[day21], 100.0)
    assert np.allclose(ema_ref[day21], 100.0)
    assert not np.any(ema_now[day21] > ema_ref[day21])
    # 日 22 起:日 21 收盘已纳入 ⇒ ema[21] > ema[1] ⇒ 上升成立
    day22 = np.arange(22 * 6, 23 * 6)
    assert np.all(ema_now[day22] > ema_ref[day22])
    assert ema_now[22 * 6] == pytest.approx(100.0 + 100.0 * (2 / 201))
    # 首个完成日之前(日 0 的 bar)无可见日 ⇒ NaN
    assert np.isnan(ema_now[:6]).all()


# ---------------------------------------------------------------------------
# 引擎层:入场/加梯/棘轮/5%硬平/Donchian 出场/MIN_SIZE/确定性
# ---------------------------------------------------------------------------


def _breakout_frame() -> tuple:
    """260 根漂移 + bar260 突破(收 106)⇒ bar261 开盘入场。"""
    rows = _drift(260)
    prev_c = rows[-1][3]                              # 102.59
    rows.append((prev_c, 106.5, prev_c - 0.01, 106.0))   # bar260: close 突破
    return rows, 260


def test_entry_fills_at_next_open_with_slippage_and_risk_sizing():
    rows, s = _breakout_frame()
    n_atr = _atr_at(rows, s)
    e1 = 106.0 * (1 + SLIP)
    stop0 = e1 - 2 * n_atr
    rows.append((106.0, 106.4, 105.0, 106.3))         # bar261 入场 bar
    rows.extend([(106.3, 106.5, 104.0, 104.5)] * 3)   # 低 > stop0,持仓
    rows.append((104.5, 104.6, stop0 - 0.001, 104.0)) # wick 破 stop0 ⇒ trail
    rows.append((104.0, 104.2, 103.9, 104.0))         # 末棒
    res = quattro.run(_df(rows), _broker(), "TEST")
    assert len(res["sequences"]) == 1
    seq = res["sequences"][0]
    assert seq["first_entry_price"] == pytest.approx(e1)
    assert seq["original_atr"] == pytest.approx(n_atr)
    assert seq["acct_at_entry"] == pytest.approx(300.0)
    # qty = floor(0.02·300 / (2N) → step):风险 ≈ 6 USDT,尺寸距离 = 2N
    expected_q = math.floor((0.02 * 300.0 / (2 * n_atr)) / 0.0001 + 1e-9) * 0.0001
    assert seq["total_size_btc"] == pytest.approx(expected_q)
    assert seq["exit_reason"] == quattro.EXIT_TRAIL
    assert seq["exit_price"] == pytest.approx(stop0 * (1 - SLIP))


def _pyramid_frame() -> tuple:
    """突破后逐级触及三层加梯,第 4 单元满仓后单 bar 崩穿 5% 硬平。"""
    rows, s = _breakout_frame()
    n_atr = _atr_at(rows, s)
    e1 = 106.0 * (1 + SLIP)
    t2 = e1 + 0.5 * n_atr
    t3 = e1 + 1.0 * n_atr
    t4 = e1 + 1.5 * n_atr
    rows.append((106.0, 106.4, 105.0, 106.3))          # 261 入场;低 > stop
    rows.append((106.3, t2 + 0.2, 105.5, 106.6))       # 262 wick 触 t2
    rows.append((106.7, t3 - 0.2, 106.2, 106.8))       # 263 unit2 成交于其开盘
    rows.append((106.8, t3 + 0.2, 106.3, 107.3))       # 264 wick 触 t3
    rows.append((107.3, t4 - 0.2, 106.8, 107.5))       # 265 unit3 成交
    rows.append((107.5, t4 + 0.2, 107.0, 107.9))       # 266 wick 触 t4
    rows.append((107.9, 108.2, 107.5, 108.0))          # 267 unit4 成交
    rows.append((107.0, 107.2, 100.0, 101.0))          # 268 崩穿 5% 层级
    rows.append((101.0, 101.5, 100.5, 101.2))          # 269 末棒
    return rows, (e1, n_atr)


def test_pyramid_ladder_next_open_equal_units_max4():
    rows, (e1, n_atr) = _pyramid_frame()
    res = quattro.run(_df(rows), _broker(), "TEST")
    assert len(res["sequences"]) == 1
    seq = res["sequences"][0]
    assert seq["num_units"] == 4                       # 至多 4 单元
    assert seq["first_entry_price"] == pytest.approx(e1)
    # 等仓:各单元 qty 与首单元完全相等(equal-unit 语义)
    assert len(set(seq["unit_qtys"])) == 1
    assert seq["total_size_btc"] == pytest.approx(4 * seq["unit_qtys"][0])
    # 单元入场 = 触发 bar 的**次**开盘(滑点后),非 trigger 价
    assert seq["unit_entries"][1] == pytest.approx(106.7 * (1 + SLIP))
    assert seq["unit_entries"][2] == pytest.approx(107.3 * (1 + SLIP))
    assert seq["unit_entries"][3] == pytest.approx(107.9 * (1 + SLIP))


def test_pyramid_unit_fills_and_avg_entry():
    rows, (e1, n_atr) = _pyramid_frame()
    res = quattro.run(_df(rows), _broker(), "TEST")
    seq = res["sequences"][0]
    q = seq["total_size_btc"] / 4
    e2 = 106.7 * (1 + SLIP)
    e3 = 107.3 * (1 + SLIP)
    e4 = 107.9 * (1 + SLIP)
    avg = (e1 + e2 + e3 + e4) * q / (4 * q)
    assert seq["avg_entry_price"] == pytest.approx(avg)
    assert e2 != e1 + 0.5 * n_atr                      # 不在 trigger 价成交
    assert seq["exit_reason"] == quattro.EXIT_HARD     # 两级同穿 ⇒ hard 标签
    # 4 单元硬平 ≈ −2.5R 毛损,成本加深(费用/滑点/资金费)
    assert -3.6 < seq["R_total"] < -2.3
    assert seq["pnl_usd"] < -0.045 * 300.0


def test_ratchet_moves_only_on_add_and_never_down():
    """(1) 未加仓:close 连升但 high 未触 t2 ⇒ 止损钉在 e1−2N;
    (2) 加仓后:止损 = 新单元入场−2N;其间 close 升亦不再上移。"""
    rows, s = _breakout_frame()
    n_atr = _atr_at(rows, s)
    e1 = 106.0 * (1 + SLIP)
    stop0 = e1 - 2 * n_atr
    rows.append((106.0, 106.4, 105.0, 106.3))          # 入场
    for k in range(8):                                  # 262..269 连升不破 t2
        c = 106.3 + 0.02 * k
        assert c + 0.1 < e1 + 0.5 * n_atr
        rows.append((c - 0.05, c + 0.1, c - 0.3, c))
    rows.append((106.4, 106.4, stop0 + 0.001, 106.0))   # 270 未破 stop0
    rows.append((106.0, 106.0, stop0, 105.0))           # 271 wick 触 stop0
    rows.append((105.0, 105.2, 104.8, 105.0))           # 272 末棒
    res = quattro.run(_df(rows), _broker(), "TEST")
    assert len(res["sequences"]) == 1
    seq = res["sequences"][0]
    assert seq["exit_reason"] == quattro.EXIT_TRAIL
    assert seq["num_units"] == 1
    # 成交恰在钉死的 stop0(滑点后)⇒ 证明未随 close 上移
    assert seq["exit_price"] == pytest.approx(stop0 * (1 - SLIP))

    # —— 加仓后棘轮抬至 e2−2N;回落破新位 ⇒ 出场(旧位上方 ⇒ 旧止损不会平)
    rows2, s2 = _breakout_frame()
    n2 = _atr_at(rows2, s2)
    e1b = 106.0 * (1 + SLIP)
    t2 = e1b + 0.5 * n2
    rows2.append((106.0, 106.4, 105.0, 106.3))          # 入场
    rows2.append((106.3, t2 + 0.2, 105.5, 106.6))       # wick 触 t2
    rows2.append((106.7, 106.9, 106.2, 106.8))          # unit2 成交于其开盘
    e2 = 106.7 * (1 + SLIP)
    stop1 = e2 - 2 * n2
    for k in range(4):                                  # 连升不触 t3
        c = 106.8 + 0.02 * k
        rows2.append((c - 0.05, c + 0.08, c - 0.2, c))
    rows2.append((106.85, 106.85, stop1 - 0.002, 106.5))  # 破新棘轮(旧位上方)
    rows2.append((106.5, 106.6, 106.4, 106.5))
    res2 = quattro.run(_df(rows2), _broker(), "TEST")
    seq2 = res2["sequences"][0]
    assert seq2["num_units"] == 2
    assert seq2["exit_reason"] == quattro.EXIT_TRAIL
    assert stop0 < stop1                                # 只上移
    assert seq2["exit_price"] == pytest.approx(stop1 * (1 - SLIP))


def test_hard_stop_label_vs_trailing_single_unit():
    """1 单元:同 bar 贯穿 trail/hard 两级 ⇒ hard(upstream 顺序);
    仅触及 trail ⇒ trailing。"""
    rows, s = _breakout_frame()
    n_atr = _atr_at(rows, s)
    e1 = 106.0 * (1 + SLIP)
    rows.append((106.0, 106.4, 105.0, 106.3))           # 入场
    q = math.floor((6.0 / (2 * n_atr)) / 0.0001 + 1e-9) * 0.0001
    hard_lvl = e1 - 0.05 * 300.0 / q                    # ≈ e1 − 5N(更深)
    trail_lvl = e1 - 2 * n_atr
    assert hard_lvl < trail_lvl
    rows.append((106.0, 106.2, hard_lvl - 1.0, 105.0))  # 贯穿两级
    rows.append((105.0, 105.2, 104.8, 105.0))
    res = quattro.run(_df(rows), _broker(), "TEST")
    seq = res["sequences"][0]
    assert seq["exit_reason"] == quattro.EXIT_HARD
    # 站立位 = max(trail, hard) = trail:本复刻按站立位成交(差异已声明)
    assert seq["exit_price"] == pytest.approx(trail_lvl * (1 - SLIP))

    rows2, s2 = _breakout_frame()
    n2 = _atr_at(rows2, s2)
    e1b = 106.0 * (1 + SLIP)
    rows2.append((106.0, 106.4, 105.0, 106.3))
    rows2.append((106.0, 106.2, e1b - 3 * n2, 105.5))   # 仅破 trail(>hard)
    rows2.append((105.5, 105.6, 105.4, 105.5))
    res2 = quattro.run(_df(rows2), _broker(), "TEST")
    assert res2["sequences"][0]["exit_reason"] == quattro.EXIT_TRAIL


def test_donchian_exit_next_open_full_close():
    rows, s = _breakout_frame()
    rows.append((106.0, 106.4, 105.0, 106.3))           # 入场
    for k in range(10):                                 # 262..271 低=105 → donch10=105
        c = 105.5 + 0.05 * k
        rows.append((c, c + 0.2, 105.0, c + 0.1))
    rows.append((105.9, 106.0, 104.9, 104.8))           # 272 close<105 ⇒ 出场
    rows.append((104.8, 104.9, 104.5, 104.7))           # 273 次开盘全平
    rows.append((104.7, 104.8, 104.6, 104.7))           # 274 末棒
    res = quattro.run(_df(rows), _broker(), "TEST")
    assert len(res["sequences"]) == 1
    seq = res["sequences"][0]
    assert seq["exit_reason"] == quattro.EXIT_DONCHIAN
    exit_ts = T0 + 273 * H4
    assert seq["exit_ts"] == exit_ts                    # 次 bar 开盘成交
    assert seq["exit_price"] == pytest.approx(104.8 * (1 - SLIP))


def test_entry_requires_breakout_and_rising_ema():
    # close == donch_high(非严格 >)⇒ 无信号
    rows = _drift(260)
    prev_c = rows[-1][3]
    donch = prev_c + 0.5
    rows.append((prev_c, donch + 0.3, prev_c, donch))   # close == donch
    rows.extend([(donch, donch + 0.2, donch - 0.5, donch)] * 12)
    res = quattro.run(_df(rows), _broker(), "TEST")
    assert res["sequences"] == [] and res["counters"]["entry_signals"] == 0

    # 日 EMA 走平(恒等)⇒ 上升过滤否决,突破真实也不入场
    rows2 = [(100.0, 100.5, 99.5, 100.0)] * 260
    rows2.append((100.0, 103.5, 99.5, 103.0))
    rows2.extend([(103.0, 103.5, 102.0, 103.0)] * 12)
    res2 = quattro.run(_df(rows2), _broker(), "TEST")
    assert res2["sequences"] == [] and res2["counters"]["entry_signals"] == 0


def test_min_size_reject_recorded_no_sequence():
    # 天价品种 + 巨 ATR ⇒ qty < size_min ⇒ SKIP(MIN_SIZE),序列不成立
    base = 1_000_000.0
    rows = []
    prev = base
    for k in range(260):
        c = base + 100 * k
        rows.append((prev, c + 5000.0, prev - 5000.0, c))
        prev = c
    rows.append((prev, prev + 2_100_000.0, prev, prev + 2_000_000.0))  # 巨 ATR 突破
    n_atr = _atr_at(rows, 260)
    assert 6.0 / (2 * n_atr) < 0.0001                    # 预期 qty 下取整 < size_min
    rows.append((prev + 2_000_000.0, prev + 2_001_000.0,
                 prev + 1_999_000.0, prev + 2_000_500.0))
    rows.append((prev + 2_000_500.0, prev + 2_001_000.0,
                 prev + 2_000_000.0, prev + 2_000_500.0))
    b = _broker()
    res = quattro.run(_df(rows), b, "TEST")
    assert res["sequences"] == []
    assert res["counters"]["entry_min_size_skips"] == 1
    assert b.n_skips.get("MIN_SIZE") == 1


def test_deterministic_double_run_identical_sequences():
    rows, _ = _pyramid_frame()
    a = quattro.run(_df(rows), _broker(), "TEST")
    b = quattro.run(_df(rows), _broker(), "TEST")
    assert json.dumps(a["sequences"], sort_keys=True) \
        == json.dumps(b["sequences"], sort_keys=True)
    assert a["equity_rows"] == b["equity_rows"]


def test_end_bar_force_close_labeled_still_open():
    rows, s = _breakout_frame()
    rows.append((106.0, 106.4, 105.0, 106.3))           # 入场(261)= 决策末棒
    rows.append((106.3, 106.4, 106.0, 106.2))           # 262 = 数据末棒(开盘平)
    res = quattro.run(_df(rows), _broker(), "TEST")
    assert len(res["sequences"]) == 1
    assert res["sequences"][0]["exit_reason"] == quattro.EXIT_END
    assert res["sequences"][0]["exit_ts"] == T0 + 262 * H4
