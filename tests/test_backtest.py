"""阶段 5 回测引擎端到端测试(真实 EventDetector + Strategy + PositionManager
+ Broker 全链路,手工可算合成路径)与重放确定性。

合成路径(25 根构造 bar,ATR 常数种子后):
- i16 确认 pivot low 90 ⇒ 支撑区 [90, 91.435951](E1 空头的结构目标);
- i19 确认 pivot low 100 ⇒ 支撑区 [100, 101.596565];i20 E3|short(非 GO,拒绝);
- i21 收盘 99.2 < 100 − k_bm·atr 破位 + 连续 3 根收在 100 之下 ⇒ i23 发射
  E1|short(invalid=101.884704,目标=91.435951,r_mult≈1.622 ≥ 1.5 ⇒ 开仓);
- i24 开盘 97.7 成交(滑点后 97.68046,qty 向下取整 0.53);
- i25 低点 90.9 ≤ target ⇒ §8.2 决策;i26 开盘 92.0 减仓 0.26(92.0184),
  invalid 移至 entry(保本);
- i27 收盘 98.7 > invalid ⇒ §8.1 决策;i28 开盘 98.9 全平(98.91978, 0.27)。
账本形状恰为 OPEN → REDUCE → EXIT 三行,§8.2/§8.1 各触发一次。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from market_operator.backtest import (
    compute_metrics,
    prepare_cognition,
    run_backtest,
)
from market_operator.broker import ContractSpec
from market_operator.structure import Params

H = 3_600_000
T0 = 1_700_000_000_000

ROWS = [(T0 + i * H, 100.0, 101.0, 99.0, 100.0, 1.0) for i in range(14)] + [
    (T0 + 14 * H, 100.0, 100.6, 97.3, 97.5, 1.0),
    (T0 + 15 * H, 97.5, 97.8, 90.0, 90.5, 1.0),
    (T0 + 16 * H, 90.5, 98.0, 90.3, 97.6, 1.0),
    (T0 + 17 * H, 97.6, 104.0, 97.4, 103.6, 1.0),
    (T0 + 18 * H, 103.6, 103.9, 100.0, 100.6, 1.0),
    (T0 + 19 * H, 100.6, 103.4, 100.2, 103.0, 1.0),
    (T0 + 20 * H, 103.0, 103.3, 100.6, 101.1, 1.0),
    (T0 + 21 * H, 101.1, 101.4, 98.9, 99.2, 1.0),
    (T0 + 22 * H, 99.2, 99.6, 98.0, 98.4, 1.0),
    (T0 + 23 * H, 98.4, 98.8, 97.5, 97.9, 1.0),
    (T0 + 24 * H, 97.7, 98.3, 95.6, 96.0, 1.0),
    (T0 + 25 * H, 96.0, 96.6, 90.9, 91.8, 1.0),
    (T0 + 26 * H, 92.0, 97.5, 91.5, 96.9, 1.0),
    (T0 + 27 * H, 96.9, 99.0, 96.5, 98.7, 1.0),
    (T0 + 28 * H, 98.9, 99.4, 98.0, 98.6, 1.0),
]

SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)

KW = dict(dev_frac=0.7, equity0=300.0, risk_frac=0.0075, leverage_cap=5.0,
          taker_fee=0.0006, slippage_per_side=0.0002, funding_per_8h=0.0001,
          cost_multiplier=1.0, contracts={"TEST": SPEC}, params=Params())


def _frame() -> pd.DataFrame:
    return pd.DataFrame(ROWS, columns=["ts", "open", "high", "low",
                                       "close", "volume"])


def _run(segment="full"):
    cog = prepare_cognition(_frame(), Params(), "TEST")
    return run_backtest({"TEST": cog}, segment=segment, **KW)


# ---------------------------------------------------------------------------
# 端到端:账本形状、严格成交时序、精确数值
# ---------------------------------------------------------------------------


def test_end_to_end_ledger_shape_and_values():
    res = _run()
    assert res.counters == {"go_events": 1, "open_intents": 1, "opens_filled": 1,
                            "min_size_skips": 0, "liquidations": 0}
    kinds = [r["kind"] for r in res.ledger]
    assert kinds == ["OPEN", "REDUCE", "EXIT"]          # §8.2/§8.1 各触发一次

    op, red, ex = res.ledger
    # 决策 i23 ⇒ 成交 i24;决策 i25 ⇒ 成交 i26;决策 i27 ⇒ 成交 i28
    assert (op["decision_ts"], op["ts"]) == (T0 + 23 * H, T0 + 24 * H)
    assert (red["decision_ts"], red["ts"]) == (T0 + 25 * H, T0 + 26 * H)
    assert (ex["decision_ts"], ex["ts"]) == (T0 + 27 * H, T0 + 28 * H)
    # 正确性硬约束:任何成交时间严格晚于决策时间
    for r in res.ledger:
        assert r["ts"] > r["decision_ts"]

    # OPEN:97.7·0.9998 = 97.68046;qty = 2.25/|97.68046−101.884704| 向下取整 0.53
    assert op["side"] == "sell" and op["price"] == pytest.approx(97.68046)
    assert op["qty"] == 0.53
    assert op["fee"] == pytest.approx(0.53 * 97.68046 * 0.0006)
    assert op["reason"].startswith("E1|short GO")
    # REDUCE §8.2:0.5·0.53=0.265 向下取整 0.26;buy 92·1.0002 = 92.0184
    assert red["reason_code"] == "§8.2"
    assert red["qty"] == 0.26 and red["price"] == pytest.approx(92.0184)
    assert red["realized_pnl"] == pytest.approx((97.68046 - 92.0184) * 0.26)
    # EXIT §8.1(保本后收盘 98.7 > invalid 97.68046):余仓 0.27;buy 98.9·1.0002
    assert ex["reason_code"] == "§8.1"
    assert ex["qty"] == 0.27 and ex["price"] == pytest.approx(98.91978)
    assert ex["funding_delta"] == pytest.approx(-0.00188476, abs=1e-7)

    t = res.trades[0]
    assert t["event_id"] == "E1" and t["direction"] == -1
    assert t["closed"] and t["exit_kind"] == "EXIT"
    assert t["invalid"] == pytest.approx(101.884704, abs=1e-6)
    assert t["target_1"] == pytest.approx(91.435951, abs=1e-6)
    assert t["r_mult_planned"] == pytest.approx(1.622215, abs=1e-6)
    assert t["gross_pnl"] == pytest.approx(1.1375192000000052)
    assert t["net_pnl"] == pytest.approx(1.1375192 - 0.06144226104
                                         - 0.0018847636575)
    assert t["risk_usdt"] == pytest.approx(0.53 * (101.88470418868205 - 97.68046))
    assert t["r_multiple"] == pytest.approx(
        t["net_pnl"] / t["risk_usdt"], rel=1e-12)
    assert t["hold_hours"] == 4.0
    # 权益终值 = 300 + 净盈亏(账本逐行对账)
    assert res.equity_rows[-1][1] == pytest.approx(300.0 + t["net_pnl"])
    # 段内唯一 GO 事件 ⇒ 交易归属 full 段
    assert t["in_segment_open"]


def test_metrics_consistency():
    m = compute_metrics(_run())
    assert m["n_trades"] == 1 and m["n_closed"] == 1
    assert m["net_pnl"] == pytest.approx(1.0741921753025052)
    assert m["winrate"] == 1.0 and m["profit_factor"] is None
    assert m["expectancy_usdt"] == pytest.approx(m["net_pnl"])
    assert m["max_dd_usdt"] >= 0.0
    assert m["by_symbol"]["TEST"]["n"] == 1
    assert m["by_direction"]["-1"]["n"] == 1           # v1 预期全空
    assert m["counters"]["liquidations"] == 0


def test_segment_gating_dev_blocks_open_test_allows():
    # 分界 = int(29·0.7) = 20;E1 决策在 i23:段 dev([0,20))⇒ 0 交易;test ⇒ 1
    dev = _run("dev")
    assert dev.counters["open_intents"] == 0 and dev.trades == []
    assert all(eq == pytest.approx(300.0) for _ts, eq, *_ in dev.equity_rows)
    test = _run("test")
    assert len(test.trades) == 1 and test.trades[0]["in_segment_open"]


def test_replay_determinism():
    a, b = _run(), _run()
    assert json.dumps(a.ledger, sort_keys=True, default=str) == \
        json.dumps(b.ledger, sort_keys=True, default=str)
    assert a.equity_rows == b.equity_rows
    assert json.dumps(a.trades, sort_keys=True, default=str) == \
        json.dumps(b.trades, sort_keys=True, default=str)


def test_min_size_blocks_trade_when_equity_tiny():
    cog = prepare_cognition(_frame(), Params(), "TEST")
    kw = {**KW, "equity0": 5.0}                        # 5 USDT ⇒ notional < 5
    res = run_backtest({"TEST": cog}, segment="full", **kw)
    assert res.counters["open_intents"] == 1
    assert res.counters["opens_filled"] == 0
    assert res.counters["min_size_skips"] == 1
    assert res.trades == []
    skip = res.ledger[0]
    assert skip["kind"] == "SKIP" and skip["reason_code"] == "MIN_SIZE"
