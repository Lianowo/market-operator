"""Structural invalidation is a stop-market execution, not merely a 1h signal."""
import pytest

from market_operator.broker import Broker, ContractSpec
from market_operator.strategy import OrderIntent, OrderKind

T0=1700000000000
H=3600000


def broker():
    return Broker(equity=300,risk_frac=.0075,leverage_cap=5.0,
                  taker_fee=.0006,slippage_per_side=.0002,
                  funding_per_8h=.0001,
                  contracts={"BTC":ContractSpec(
                      symbol="BTC",size_min=.01,size_step=.01,
                      size_place=2,price_place=2,min_trade_usdt=5)})


def enter(b,side,invalid):
    b.submit(OrderIntent(OrderKind.OPEN,"BTC",None,"E7|test",100,T0),T0,
             direction=side,invalid=invalid,event_id="E7",target_1=None)
    assert b.on_bar_open("BTC",T0+H,100)[0]["kind"]=="OPEN"


def test_short_stop_at_15m_wick_before_hour_close():
    b=broker();enter(b,-1,101)
    row=b.execute_protective_stop("BTC",T0+H,100,101.1,98)
    assert row["kind"]=="EXIT"
    assert row["reason_code"]=="PROTECTIVE_STOP"
    assert row["meta"]["stop_level"]==101
    assert row["price"]==pytest.approx(101*1.0002)
    assert b.position_of("BTC") is None
    assert b.execute_protective_stop("BTC",T0+H,100,104,90) is None


def test_gapped_open_uses_worse_actual_open_not_fictional_stop():
    b=broker();enter(b,1,99)
    row=b.execute_protective_stop("BTC",T0+H+900000,95,101,94)
    assert row["price"]==pytest.approx(95*(1-.0002))
    assert row["meta"]["gap_at_open"] is True


def test_long_stop_wick_trigger_and_no_trigger():
    b=broker();enter(b,1,99)
    assert b.execute_protective_stop("BTC",T0+H,100,102,99.5) is None
    assert b.position_of("BTC") is not None
    row=b.execute_protective_stop("BTC",T0+H+900000,100,102,98.9)
    assert row["price"]==pytest.approx(99*(1-.0002))
    assert row["kind"]=="EXIT"


def test_confirmed_trailing_stop_is_executable():
    b=broker();enter(b,-1,101)
    b.update_protective_invalid("BTC",99)
    assert b.execute_protective_stop("BTC",T0+H+900000,98.8,99.1,97.9)
    assert b.position_of("BTC") is None
