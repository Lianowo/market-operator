"""v4 挂单经纪商测试(docs/v4-protocol.md §1-G3,执行路径细化)。

统一约定:合约 step=0.01/sizeMin=0.01/minTradeUSDT=5(同 test_broker.py);
隐藏成本先验 taker 0.0006/maker 0.0002/slip 0.0002/funding 0.0001 每 8h;
1h = 3_600_000 ms,15m = 900_000 ms;T0 = 2024-01-01 00:00 UTC(整小时对齐,
15m 落桶 parent = 标签小时)。覆盖:严格穿过成交/持平不成交/超时 EXPIRED
三分支(入场)、maker/taker 费率精确值、EP→成交严格晚于 armed 的因果、
减仓限价成交、减仓超时转 taker、cancel_resting、MIN_SIZE/DUP/BAD_INVALID
拒绝、cost_multiplier 对 maker 费同缩放。
"""

from __future__ import annotations

import pytest

from market_operator.broker import Broker, ContractSpec, RestingLimitOrder
from research.rounds.v1.strategy import OrderIntent, OrderKind

H = 3_600_000
Q = 900_000
T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC(整小时网格)
N_FILL = 12
EXPIRY = T0 + N_FILL * H


def _spec(step=0.01, place=2, size_min=0.01, min_usdt=5.0) -> ContractSpec:
    return ContractSpec(symbol="TEST", size_min=size_min, size_step=step,
                        size_place=place, price_place=2, min_trade_usdt=min_usdt)


def _broker(equity=300.0, cost_multiplier=1.0, **kw) -> Broker:
    cfg = dict(risk_frac=0.0075, leverage_cap=5.0, taker_fee=0.0006,
               maker_fee=0.0002, slippage_per_side=0.0002,
               funding_per_8h=0.0001)
    cfg.update(kw)
    return Broker(equity, contracts={"TEST": _spec()},
                  cost_multiplier=cost_multiplier, **cfg)


def _sell_entry(limit=100.0, invalid=101.0, armed=T0, n_fill=N_FILL,
                reason="E7|short whitelist entry_ref=98 invalid=101 target_1=95 "
                       "r_mult=2.000 limit_front_edge=100"):
    return RestingLimitOrder(
        direction=-1, limit_price=limit, armed_at_ts=armed,
        expiry_ts=armed + n_fill * H, symbol="TEST", role="entry",
        invalid=invalid, target_1=95.0, event_id="E7", reason=reason,
        meta={"direction": -1, "regime": "RANGE", "r_mult": 2.0})


def _fill_short(b: Broker, limit=100.0, invalid=101.0, cross_k=1):
    """武装卖限价并于 T0+cross_k 小时首根 15m 穿过成交,返回账本行。"""
    b.submit_limit(_sell_entry(limit, invalid))
    rows = b.on_15m_bar("TEST", T0 + cross_k * H, 99.0, 100.5, 98.5, 99.5)
    assert len(rows) == 1 and rows[0]["kind"] == "LIMIT_FILL"
    return rows[0]


# ---------------------------------------------------------------------------
# 三分支:严格穿过成交 / 持平不成交 / 超时 EXPIRED
# ---------------------------------------------------------------------------


def test_strict_cross_fill_sell_limit():
    b = _broker()
    row = _fill_short(b)
    assert row["side"] == "sell"
    assert row["price"] == 100.0                        # limit 精确成交,无滑点
    assert row["slippage_cost"] == 0.0
    assert row["ts"] == T0 + H and row["decision_ts"] == T0
    assert row["ts"] > row["decision_ts"]               # 严格晚于 armed(因果)
    assert row["reason_code"] == "E7"
    # sizing:qty = 0.0075·300/|100−101| = 2.25;notional = 225
    assert row["qty"] == 2.25
    assert row["fee"] == pytest.approx(2.25 * 100.0 * 0.0002, rel=1e-12)
    assert row["used_leverage"] == pytest.approx(225.0 / 300.0, rel=1e-12)
    assert row["meta"]["entry_mode"] == "limit"
    assert row["meta"]["fill_wait_bars"] == 1
    assert b.cash == pytest.approx(300.0 - 0.045, rel=1e-12)
    pos = b.position_of("TEST")
    assert pos is not None and pos.qty == 2.25 and pos.entry == 100.0


def test_strict_cross_fill_buy_limit_mirror():
    b = _broker()
    b.submit_limit(RestingLimitOrder(
        direction=+1, limit_price=100.0, armed_at_ts=T0, expiry_ts=EXPIRY,
        symbol="TEST", role="entry", invalid=99.0, target_1=105.0,
        event_id="E7", reason="E7|long whitelist ...", meta={"direction": +1}))
    rows = b.on_15m_bar("TEST", T0 + H, 100.5, 101.0, 99.5, 100.5)
    r = rows[0]
    assert r["kind"] == "LIMIT_FILL" and r["side"] == "buy"
    assert r["price"] == 100.0
    assert r["qty"] == 2.25                             # risk/unit = 1.0 同上
    assert r["fee"] == pytest.approx(2.25 * 100.0 * 0.0002, rel=1e-12)


def test_touch_equal_does_not_fill():
    """持平不成交:bar 极值恰等于限价(不假设排队靠前)。"""
    b = _broker()
    b.submit_limit(_sell_entry())
    rows = b.on_15m_bar("TEST", T0 + H, 99.0, 100.0, 98.0, 99.0)   # high == limit
    assert rows == [], "high == limit 持平不得成交"
    # 买侧镜像:low == limit 亦不成交
    b2 = _broker()
    b2.submit_limit(RestingLimitOrder(
        direction=+1, limit_price=100.0, armed_at_ts=T0, expiry_ts=EXPIRY,
        symbol="TEST", role="entry", invalid=99.0, target_1=105.0,
        event_id="E7", reason="E7|long ...", meta={"direction": +1}))
    assert b2.on_15m_bar("TEST", T0 + H, 100.5, 101.0, 100.0, 100.5) == []
    # 严格穿过仍成交(对照):wait_bars 已累计 2 root eligible(含持平那根)
    rows2 = b.on_15m_bar("TEST", T0 + H + Q, 99.0, 100.5, 98.0, 99.0)
    assert rows2[0]["kind"] == "LIMIT_FILL"
    assert rows2[0]["meta"]["fill_wait_bars"] == 2


def test_armed_hour_subbars_cannot_fill_causality():
    """armed 小时自身的 15m 子 bar 即使穿过也不成交(因果硬约束)。"""
    b = _broker()
    b.submit_limit(_sell_entry())
    for k in (0, 1, 2, 3):
        rows = b.on_15m_bar("TEST", T0 + k * Q, 99.0, 100.5, 98.0, 99.5)
        assert rows == [], f"armed 小时子 bar k={k} 不得成交"
    rows = b.on_15m_bar("TEST", T0 + H, 99.0, 100.5, 98.0, 99.5)
    assert rows[0]["kind"] == "LIMIT_FILL" and rows[0]["ts"] == T0 + H


def test_expiry_window_last_hour_fills_then_expired():
    """等待窗 = (armed, armed+12·1h] 的 1h 落桶;超时 ⇒ EXPIRED,无这笔交易。"""
    b = _broker()
    b.submit_limit(_sell_entry())
    consumed = 0
    # 小时 1..12 全部 eligible(48 根 15m),均不穿过
    for hour in range(1, N_FILL + 1):
        for k in range(4):
            rows = b.on_15m_bar("TEST", T0 + hour * H + k * Q,
                                99.0, 99.9, 98.0, 99.0)
            assert rows == []
            consumed += 1
    assert consumed == 48
    assert b.resting["TEST"][0].wait_bars == 48
    # 小时 13 首根 15m:parent > expiry ⇒ EXPIRED
    rows = b.on_15m_bar("TEST", T0 + 13 * H, 99.0, 100.5, 98.0, 99.5)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "EXPIRED" and r["reason_code"] == "EXPIRED"
    assert r["ts"] == T0 + 13 * H > T0                      # ts > armed
    assert r["decision_ts"] == T0
    assert r["qty"] == 0.0 and r["fee"] == 0.0
    assert b.resting.get("TEST") is None
    assert b.positions == {}
    assert b.equity_mtm() == pytest.approx(300.0)          # 没有这笔交易


def test_last_eligible_hour_subbars_all_fill_eligible():
    """expiry(第 12 小时)的 4 根子 bar 全部可成交;13 小时首根已过期。"""
    b = _broker()
    b.submit_limit(_sell_entry())
    for hour in range(1, N_FILL):
        for k in range(4):
            b.on_15m_bar("TEST", T0 + hour * H + k * Q, 99.0, 99.9, 98.0, 99.0)
    rows = b.on_15m_bar("TEST", T0 + N_FILL * H + 3 * Q, 99.0, 100.5, 98.0, 99.5)
    assert rows[0]["kind"] == "LIMIT_FILL"


# ---------------------------------------------------------------------------
# maker/taker 费率与精确数值(含减仓与退出)
# ---------------------------------------------------------------------------


def _reduce_arm(b: Broker, target=95.0, armed=None):
    """对当前空仓武装 §8.2 减仓限价(buy,目标前缘 95.0)。"""
    pos = b.position_of("TEST")
    qty = round(0.5 * pos.open_qty, 2)
    armed = pos.opened_ts + H if armed is None else armed
    b.submit_limit(RestingLimitOrder(
        direction=+1, limit_price=target, armed_at_ts=armed,
        expiry_ts=armed + N_FILL * H, symbol="TEST", role="reduce",
        reason="§8.2|target_1 hit reduce 50% target=95 breakeven invalid "
               "101->99.9999",
        reduce_qty=qty, qty_frac_snap=qty / pos.open_qty,
        meta={"direction": pos.direction}))
    return armed


def test_reduce_limit_fill_maker_exact():
    b = _broker()
    entry = _fill_short(b)                                 # short 2.25 @ 100
    b.mark("TEST", T0 + 2 * H, 99.0)
    armed = _reduce_arm(b, armed=T0 + 2 * H)
    rows = b.on_15m_bar("TEST", armed + H, 99.0, 99.5, 94.5, 95.5)
    r = rows[0]
    assert r["kind"] == "LIMIT_FILL" and r["side"] == "buy"
    assert r["price"] == 95.0 and r["qty"] == 1.12         # floor(0.5×2.25)=1.12
    assert r["realized_pnl"] == pytest.approx((100.0 - 95.0) * 1.12, rel=1e-12)
    assert r["fee"] == pytest.approx(1.12 * 95.0 * 0.0002, rel=1e-12)  # maker
    assert r["slippage_cost"] == 0.0
    assert r["funding_delta"] == 0.0                       # 部分平仓不结算
    assert r["reason_code"] == "§8.2"
    assert r["ts"] > r["decision_ts"]
    pos = b.position_of("TEST")
    assert pos.qty == pytest.approx(2.25 - 1.12)
    # 余仓最终 EXIT(taker + 滑点):唯一结算资金费
    b.mark("TEST", armed + 2 * H, 97.0)
    b.submit(OrderIntent(kind=OrderKind.EXIT, symbol="TEST", qty_frac=None,
                         reason="§8.1|invalidation close", ref_price=97.0,
                         ts=armed + 2 * H), decision_ts=armed + 2 * H)
    rows2 = b.on_bar_open("TEST", armed + 3 * H, 97.0)
    e = rows2[0]
    assert e["kind"] == "EXIT"
    assert e["price"] == pytest.approx(97.0 * 1.0002, abs=1e-12)
    assert e["qty"] == pytest.approx(2.25 - 1.12)
    assert e["fee"] == pytest.approx(e["qty"] * e["price"] * 0.0006, rel=1e-9)
    assert e["funding_delta"] < 0.0                        # 累计资金费结清
    # 同一笔交易三腿费率对比:入场/减仓 maker 0.0002,退出 taker 0.0006
    fees = [r["fee"] / (r["qty"] * r["price"]) for r in b.ledger if r["qty"]]
    assert fees[0] == pytest.approx(0.0002, rel=1e-9)
    assert fees[1] == pytest.approx(0.0002, rel=1e-9)
    assert fees[2] == pytest.approx(0.0006, rel=1e-9)


def test_reduce_limit_timeout_converts_to_taker_same_qty():
    b = _broker()
    _fill_short(b)                                         # short 2.25 @ 100
    b.mark("TEST", T0 + 2 * H, 98.0)
    armed = _reduce_arm(b, armed=T0 + 2 * H)
    # 12 小时窗口内均不穿过(keep low ≥ 95)
    for hour in range(3, 3 + N_FILL):
        for k in range(4):
            rows = b.on_15m_bar("TEST", T0 + hour * H + k * Q,
                                98.0, 98.5, 95.0, 98.0)    # low == target 持平
            assert rows == []
    rows = b.on_15m_bar("TEST", T0 + (2 + N_FILL + 1) * H, 98.0, 98.5, 96.0, 98.0)
    assert len(rows) == 1
    c = rows[0]
    assert c["kind"] == "CANCEL" and c["reason_code"] == "TIMEOUT_TO_TAKER"
    assert c["ts"] > c["decision_ts"]
    # 转市价:下一根 15m 开盘 taker 成交(数量快照不变)
    rows2 = b.on_bar_open("TEST", T0 + (2 + N_FILL + 1) * H + Q, 96.0)
    r = rows2[0]
    assert r["kind"] == "REDUCE" and r["reason_code"] == "TIMEOUT_TO_TAKER"
    assert r["qty"] == 1.12
    assert r["price"] == pytest.approx(96.0 * 1.0002, abs=1e-12)     # buy 滑点
    assert r["fee"] == pytest.approx(1.12 * 96.0192 * 0.0006, rel=1e-9)  # taker
    assert r["ts"] > c["ts"]                                         # 市价在其后


def test_cancel_resting_on_position_close():
    b = _broker()
    _fill_short(b)
    b.mark("TEST", T0 + 2 * H, 98.0)
    armed = _reduce_arm(b, armed=T0 + 2 * H)
    b.submit(OrderIntent(kind=OrderKind.EXIT, symbol="TEST", qty_frac=None,
                         reason="§8.1|invalidation close", ref_price=98.0,
                         ts=T0 + 2 * H), decision_ts=T0 + 2 * H)
    rows = b.on_bar_open("TEST", T0 + 3 * H, 98.0)
    assert rows[0]["kind"] == "EXIT"
    rows2 = b.cancel_resting("TEST", T0 + 3 * H, reason_code="POSITION_CLOSED",
                             detail="position closed by EXIT; resting canceled")
    assert len(rows2) == 1
    c = rows2[0]
    assert c["kind"] == "CANCEL" and c["reason_code"] == "POSITION_CLOSED"
    assert c["ts"] > c["decision_ts"]                       # > armed(= T0+2H)
    assert b.resting.get("TEST") is None
    assert b.ledger[-1]["kind"] == "CANCEL"
    assert armed is not None


# ---------------------------------------------------------------------------
# 拒绝路径:MIN_SIZE / MIN_NOTIONAL / DUP / BAD_INVALID(挂单成交刻)
# ---------------------------------------------------------------------------


def test_min_size_and_min_notional_reject_at_limit_fill():
    # 名义不足:invalid 远 ⇒ qty 0.01,notional ≈ 1 < 5 ⇒ SKIP(MIN_SIZE)
    b = _broker()
    b.submit_limit(_sell_entry(invalid=300.0))
    rows = b.on_15m_bar("TEST", T0 + H, 99.0, 100.5, 98.5, 99.5)
    r = rows[0]
    assert r["kind"] == "SKIP" and r["reason_code"] == "MIN_SIZE"
    assert b.positions == {} and b.n_skips.get("MIN_SIZE") == 1
    assert b.equity_mtm() == pytest.approx(300.0)
    assert b.resting.get("TEST") is None                     # 订单被消费

    # qty 取整为 0
    b2 = _broker()
    b2.submit_limit(_sell_entry(invalid=1000.0))
    rows2 = b2.on_15m_bar("TEST", T0 + H, 99.0, 100.5, 98.5, 99.5)
    assert rows2[0]["kind"] == "SKIP" and rows2[0]["reason_code"] == "MIN_SIZE"


def test_dup_and_bad_invalid_reject():
    b = _broker()
    _fill_short(b)                                           # 已持空仓
    b.submit_limit(_sell_entry())                            # 再挂入场 ⇒ DUP
    rows = b.on_15m_bar("TEST", T0 + 2 * H, 99.0, 100.5, 98.5, 99.5)
    assert rows[0]["kind"] == "SKIP" and rows[0]["reason_code"] == "DUP_POSITION"

    b2 = _broker()
    b2.submit_limit(_sell_entry(limit=100.0, invalid=99.0))  # 卖限价 ≥ invalid
    rows2 = b2.on_15m_bar("TEST", T0 + H, 99.0, 100.5, 98.5, 99.5)
    assert rows2[0]["kind"] == "SKIP" and rows2[0]["reason_code"] == "BAD_INVALID"
    assert b2.positions == {}


# ---------------------------------------------------------------------------
# cost_multiplier 同缩放 maker 费;默认 maker_fee=taker(向后兼容断言)
# ---------------------------------------------------------------------------


def test_cost_multiplier_scales_maker_fee():
    b = _broker(cost_multiplier=2.0)
    row = _fill_short(b)
    assert row["fee"] == pytest.approx(2.25 * 100.0 * 0.0004, rel=1e-12)


def test_default_maker_fee_equals_taker_backcompat():
    cfg = dict(risk_frac=0.0075, leverage_cap=5.0, taker_fee=0.0006,
               slippage_per_side=0.0002, funding_per_8h=0.0001)
    b = Broker(300.0, contracts={"TEST": _spec()}, **cfg)
    assert b.maker_fee == pytest.approx(b.taker_fee, rel=0, abs=0)
    b2 = Broker(300.0, contracts={"TEST": _spec()}, cost_multiplier=2.0,
                maker_fee=0.0002, **cfg)
    assert b2.maker_fee == pytest.approx(0.0004, rel=1e-15)
    assert b2.taker_fee == pytest.approx(0.0012, rel=1e-15)


def test_submit_limit_validation():
    b = _broker()
    with pytest.raises(ValueError):
        b.submit_limit(_sell_entry(armed=T0, n_fill=0))      # expiry ≤ armed
    with pytest.raises(ValueError):
        b.submit_limit(RestingLimitOrder(
            direction=0, limit_price=1.0, armed_at_ts=T0,
            expiry_ts=T0 + H, symbol="TEST", role="entry", invalid=0.5))
    with pytest.raises(ValueError):
        b.submit_limit(RestingLimitOrder(direction=-1, limit_price=100.0,
                                         armed_at_ts=T0, expiry_ts=T0 + H,
                                         symbol="TEST", role="entry"))
    with pytest.raises(ValueError):
        b.submit_limit(RestingLimitOrder(direction=+1, limit_price=95.0,
                                         armed_at_ts=T0, expiry_ts=T0 + H,
                                         symbol="TEST", role="reduce"))
