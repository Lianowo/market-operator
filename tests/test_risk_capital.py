"""风险资本口径:浮盈能撑保证金,但不是可亏本金。

这两个量在过去被同一个 `equity` 变量同时承担 —— 档位按现金选、金额按含浮盈
权益算,于是"账户看起来最有钱"的那一刻也是"下一笔下得最大"的那一刻。这里
锁定修正后的定义,防止以后又被合并回一个变量。
"""

from __future__ import annotations

import pytest

from market_operator.broker import (
    Broker,
    ContractSpec,
    OrderIntent,
    OrderKind,
    RestingLimitOrder,
)

H = 3_600_000
T0 = 1_700_000_000_000


def _spec() -> ContractSpec:
    return ContractSpec(symbol="X", size_min=0.01, size_step=0.01,
                        size_place=2, price_place=2, min_trade_usdt=5.0)


def _broker(equity: float = 1000.0, **kw) -> Broker:
    cfg = dict(risk_frac=0.05, leverage_cap=20.0, taker_fee=0.0006,
               slippage_per_side=0.0002, funding_per_8h=0.0001)
    cfg.update(kw)
    return Broker(equity, contracts={"A": _spec(), "B": _spec()}, **cfg)


def _open_short(b: Broker, sym: str, ts: int, price: float = 100.0,
                invalid: float = 101.0) -> dict:
    b.submit(OrderIntent(kind=OrderKind.OPEN, symbol=sym, qty_frac=None,
                         reason="LEVEL_REJECTED|short", ref_price=price, ts=ts),
             decision_ts=ts, direction=-1, invalid=invalid, target_1=94.0,
             event_id="LEVEL_REJECTED", meta={"direction": -1})
    rows = b.on_bar_open(sym, ts + H, price)
    return rows[0]


def _inflate_floating_profit(b: Broker) -> None:
    """A 空单从 100 跌到 50:权益暴涨,但现金只多了开仓手续费。"""
    b.mark("A", T0 + 2 * H, 50.0)


def _capital(b: Broker) -> float:
    """风险资本 = 现金 − 已计提未结资金费;不含浮盈。"""
    return b.cash - b.funding_accrued()


def test_floating_profit_is_solvency_not_risk_capital():
    b = _broker()
    row = _open_short(b, "A", T0)
    _inflate_floating_profit(b)
    assert b.equity_mtm() > 3.0 * b.cash          # 权益被浮盈撑大了 3 倍以上
    assert b.risk_capital() == pytest.approx(_capital(b), rel=1e-12)
    assert b.risk_capital() < 0.35 * b.equity_mtm()
    # 成交价 99.98、止损 101 ⇒ 每单位风险 1.02;5% × 1000 / 1.02,向下取整
    assert row["qty"] == pytest.approx(0.05 * 1000 / 1.02, abs=0.01)


def test_next_entry_sizes_on_banked_cash_not_marked_equity():
    b = _broker()
    _open_short(b, "A", T0)
    _inflate_floating_profit(b)
    capital, equity = _capital(b), b.equity_mtm()

    old_basis_qty = b.qty_for_entry(equity, 100.0, 101.0, "B",
                                    notional_equity=equity)
    new_basis_qty = b.qty_for_entry(capital, 100.0, 101.0, "B",
                                    notional_equity=equity)
    assert new_basis_qty == pytest.approx(0.05 * capital, rel=1e-9)
    assert old_basis_qty > 2.5 * new_basis_qty      # 旧口径会下 3 倍以上的仓

    row = _open_short(b, "B", T0 + 3 * H)
    assert row["kind"] == "OPEN"
    # 成交价含滑点 99.98,故每单位风险 1.02
    assert row["qty"] == pytest.approx(0.05 * capital / 1.02, abs=0.01)
    # 名义上限仍按盯市权益(偿付能力),不因现金口径而被压成零
    assert 0.0 < row["used_leverage"] <= 20.0


def test_limit_entry_uses_same_risk_capital_basis():
    b = _broker()
    _open_short(b, "A", T0)
    _inflate_floating_profit(b)
    capital = _capital(b)
    b.submit_limit(RestingLimitOrder(
        direction=-1, limit_price=100.0, armed_at_ts=T0 + 3 * H,
        expiry_ts=T0 + 15 * H, symbol="B", invalid=101.0, target_1=94.0,
        event_id="LEVEL_REJECTED", meta={"direction": -1}))
    rows = b.on_15m_bar("B", T0 + 4 * H, 101.0, 101.5, 99.0, 100.0)
    fill = next(r for r in rows if r["kind"] == "LIMIT_FILL")
    assert fill["qty"] == pytest.approx(0.05 * capital, abs=0.01)


def test_default_argument_keeps_legacy_single_basis():
    b = _broker()
    # 不传 notional_equity ⇒ 旧的单一 equity 口径,逐位不变
    assert b.qty_for_entry(1000.0, 100.0, 101.0) == pytest.approx(50.0)


def test_realized_loss_shrinks_next_risk_but_floating_loss_does_not():
    b = _broker()
    _open_short(b, "A", T0)
    b.mark("A", T0 + 2 * H, 110.0)            # 浮亏 −500,现金未变
    assert b.risk_capital() == pytest.approx(_capital(b), rel=1e-12)
    assert b.equity_mtm() < b.cash
    # 现金口径下,浮亏不改变"亏得起多少";它只压低杠杆上限(偿付能力)
    assert b.qty_for_entry(b.risk_capital(), 100.0, 101.0, "B",
                           notional_equity=b.equity_mtm()) > 0.0

