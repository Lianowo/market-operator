"""Final price checks are shared by routing, simulation and live adapters (offline)."""

from dataclasses import replace

import pytest

from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind, RestingLimitOrder
from market_operator.events import BREAKOUT_ACCEPTED, MarketEvent
from market_operator.hypothesis import StructureView, entry_contract_error, entry_cost_error
from market_operator.routing import MarketRouter, RouterParams
from market_operator.structure import Regime
from tests.test_live_runtime import (
    NOW, _FakeClient, _FillClient, _SignalTG, _campaign, _decision, _entry_rec,
    _pos, _runner,
)

META = {"max_cost_frac": .1, "cost_per_side": .0008}


@pytest.mark.parametrize("direction", [-1, 1])
def test_cost_check_covers_unbounded_entries_and_exact_boundary(direction):
    invalid = 100 - direction
    assert entry_contract_error(direction, 100, invalid, None, META) == "ENTRY_COST"
    assert entry_cost_error(direction, 100, invalid, {**META, "max_cost_frac": .16}) is None
    assert entry_cost_error(direction, 100, invalid, {}) is None
    assert entry_cost_error(direction, float("nan"), invalid, META) == "INVALID_COST_CONSTRAINT"


def _broker():
    return Broker(1000, risk_frac=.01, leverage_cap=20, taker_fee=.0006,
                  slippage_per_side=.0002, funding_per_8h=0,
                  contracts={"X": ContractSpec("X", .001, .001, 3, 2, 5)})


def test_router_propagates_cost_contract_only_when_enabled():
    from market_operator.level import Level, LevelKind, LevelRole, LevelState
    lv = Level(1, LevelRole.RESISTANCE, LevelKind.EQUAL, 99.5, 99.0, LevelState.TESTED, 0, 1, 0, 1, touches=2)
    opp = Level(2, LevelRole.RESISTANCE, LevelKind.SWING, 112.0, 110.0, LevelState.FRESH, 0, 1, 0, 1)
    event = MarketEvent(BREAKOUT_ACCEPTED, 1, 0, 1, {"level": lv, "zone": lv, "invalid": 97.0, "ref": 99.5})
    args = ("X", (0, 99.5, 101, 99, 100, 1), 1, StructureView(Regime.TREND_UP, (opp,), 1), 1, [event])
    params = RouterParams(max_cost_frac=.1, cost_per_side=.0008)
    enabled, = MarketRouter(params).on_bar(*args, position_open=False, entry_resting=False)
    disabled, = MarketRouter(replace(params, max_cost_frac=None)).on_bar(
        *args, position_open=False, entry_resting=False)
    assert {key: enabled.meta[key] for key in META} == META
    assert "max_cost_frac" not in disabled.meta


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("kind", ["market", "limit", "watch"])
def test_broker_rechecks_cost_at_fill_without_charging_fee(direction, kind):
    broker = _broker()
    invalid = 100 - direction
    if kind == "market":
        broker.submit(OrderIntent(OrderKind.OPEN, "X", None, "cost", 100, 0), 0,
                      direction=direction, invalid=invalid, meta=META)
        row, = broker.on_bar_open("X", 3_600_000, 100)
    else:
        broker.submit_limit(RestingLimitOrder(
            direction, 100, 0, 10 * 3_600_000, symbol="X", invalid=invalid,
            meta={**META, **({"watch_entry": True, "near_price": 100 + direction} if kind == "watch" else {})}))
        if kind == "watch":
            trigger_bar = ((100.5, 101.5, 100.5, 101) if direction > 0
                           else (99.5, 99.5, 98.5, 99))
            trigger, = broker.on_15m_bar("X", 3_600_000, *trigger_bar)
            assert trigger["kind"] == "TRIGGER"
            row, = broker.on_bar_open("X", 2 * 3_600_000, 100)
        else:
            row, = broker.on_15m_bar("X", 3_600_000, 100, 101, 99, 100)
    assert row["reason_code"] == "ENTRY_COST"
    assert broker.cash == 1000 and broker.positions == {}


@pytest.mark.parametrize("direction", [-1, 1])
def test_live_fitted_limit_is_checked_before_submission(tmp_path, direction):
    client, tg = _FakeClient(), _SignalTG()
    submitted = []
    client.place_limit_order = lambda *args, **kw: submitted.append(kw) or {"orderId": "L1"}
    runner = _runner(tmp_path, client, tg)
    runner.equity = 104
    runner.contracts["BTCUSDT"] = ContractSpec("BTCUSDT", .001, .001, 3, 2, 5)
    # Raw candidate cost < .1; precision fitting shrinks the risk below the ceiling.
    ref = 100.009 if direction > 0 else 99.991
    invalid = 98.4 if direction > 0 else 101.6
    decision = _decision(kind="limit", direction=direction, ref=ref, invalid=invalid)
    decision.limit_price = ref
    decision.meta.update(max_cost_frac=.0996, cost_per_side=.0008)
    assert entry_cost_error(direction, ref, invalid, decision.meta) is None
    runner._submit_open(_campaign(), decision, NOW, mic=False)
    assert submitted == [] and runner.open_intents == {}
    assert "ENTRY_COST" in tg.signals[0]["exec_status"]


@pytest.mark.parametrize("quote", [None, 95.5])
def test_live_cost_limited_market_requires_valid_execution_quote(tmp_path, quote):
    client, tg = _FakeClient(), _SignalTG()
    if quote is not None:
        client.tickers = lambda: [{"symbol": "BTCUSDT", "lastPr": "100", "askPr": str(quote)}]
    runner = _runner(tmp_path, client, tg)
    runner.equity = 104
    decision = _decision(invalid=95)
    decision.meta.update(META)
    runner._submit_open(_campaign(), decision, NOW, mic=False)
    assert client.market_orders == []
    assert ("EXECUTION_QUOTE_UNAVAILABLE" if quote is None else "ENTRY_COST") in tg.signals[0]["exec_status"]


def test_live_watch_rechecks_cost_before_market_order(tmp_path):
    client, tg = _FakeClient(), _SignalTG()
    runner = _runner(tmp_path, client, tg)
    it = {"symbol": "BTCUSDT", "direction": 1, "invalid": 99,
          "status": "armed", **META}
    runner._trigger_watch_entry_live("w", it, 100, NOW, "WATCH_INNER_EDGE_HIT")
    assert it["status"] == "canceled" and client.market_orders == []


def test_live_and_paper_intents_retain_cost_contract(tmp_path):
    runner = _runner(tmp_path, _FakeClient(), _SignalTG(), mode="paper")
    runner.equity = 104
    decision = _decision()
    decision.meta.update(META)
    runner._submit_open(_campaign(), decision, NOW, mic=False)
    intent, = runner.open_intents.values()
    assert {key: intent[key] for key in META} == META
    runner._paper_entry_fill("BTCUSDT", intent["oid"], intent, 95.5, NOW + 1)
    assert intent["status"] == "void" and runner.positions == {}
    assert runner.equity == 104


def test_actual_fill_cost_breach_is_audited_without_erasing_position(tmp_path):
    client, tg = _FillClient(), _SignalTG()
    runner = _runner(tmp_path, client, tg)
    runner.positions["BTCUSDT"] = _pos(entry=100, invalid=95)
    runner._entry_settle["BTCUSDT"] = _entry_rec(deadline_ms=NOW + 1)
    intent = {"oid": "E1", "symbol": "BTCUSDT", "direction": 1,
              "invalid": 95, "status": "filled", **META}
    runner.open_intents["E1"] = intent
    client.fills = [{"symbol": "BTCUSDT", "orderId": "M1", "price": "95.5",
                     "size": "1", "cTime": str(NOW)}]
    runner._settle_entries(NOW)
    assert runner.positions["BTCUSDT"]["entry"] == 95.5
    assert intent["status"] == "filled" and intent["entry_cost_breach"] == "ENTRY_COST"
    assert runner.counters["entry_cost_breaches"] == 1
    assert client.market_orders == []  # no unapproved auto-liquidation policy
    runner._audit_entry_cost(intent, 95.5)
    assert runner.counters["entry_cost_breaches"] == 1
    assert any("成交回执超出入场成本约束" in alert for alert in tg.alerts)


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("bad_meta", [
    {"max_cost_frac": "bad"}, {"max_cost_frac": float("nan")},
    {"max_cost_frac": .1}, {"max_cost_frac": .1, "cost_per_side": "bad"},
])
def test_invalid_explicit_cost_contract_fails_closed_without_exception(tmp_path, mode, bad_meta):
    client, tg = _FakeClient(), _SignalTG()
    runner = _runner(tmp_path, client, tg, mode=mode)
    runner.equity = 104
    decision = _decision()
    decision.meta.update(bad_meta)
    runner._submit_open(_campaign(), decision, NOW, mic=False)
    assert client.market_orders == [] and runner.open_intents == {}
    assert "INVALID_COST_CONSTRAINT" in tg.signals[0]["exec_status"]


def test_market_execution_absorbs_minor_slippage_without_cliff_rejection():
    """决策时刻刚好满足 2.0R 的市价单,下一拍开盘多涨 1 分钱或 2bps 滑点变成 1.99R 时绝不误杀,但极端跳空(<1.5R)仍可靠拦截。"""
    meta = {
        "bounded_entry": True,
        "entry_min_rr": 2.0,
        "structural_target": 110.0,
        "target_source": "opposing_level",
        "cost_per_side": 0.0008,
        "max_cost_frac": 0.12,
    }
    # Decision gate is strict at 2.0R:
    assert entry_contract_error(1, 100.0, 95.0, 110.0, meta, at_execution=False) is None
    assert entry_contract_error(1, 100.02, 95.0, 110.0, meta, at_execution=False) == "INSUFFICIENT_REMAINING_RR"
    # Execution gate absorbs minor 2bps~5bps spread/slippage (1.99R) cleanly:
    assert entry_contract_error(1, 100.02, 95.0, 110.0, meta, at_execution=True) is None
    # Extreme adverse gap (< 1.5R remaining) is still blocked:
    assert entry_contract_error(1, 101.5, 95.0, 110.0, meta, at_execution=True) == "INSUFFICIENT_REMAINING_RR"


def test_mid_trade_opposing_auction_exits_before_hard_stop():
    """持仓途中若市场发生突发转向并确认反向 LEVEL_REJECTED,当即离场而不必死扛到底部止损。"""
    from market_operator.events import LEVEL_REJECTED
    from market_operator.position import PositionManager

    pm = PositionManager()
    pm.open("BTCUSDT", 1, 100.0, 94.0, 112.0, "LEVEL_REJECTED", 0, 0)
    rev_ev = MarketEvent(LEVEL_REJECTED, -1, 3_600_000, 1, {})
    intents = pm.on_bar("BTCUSDT", (3_600_000, 103.0, 104.0, 98.5, 99.0, 10.0), 2.0, new_events=[rev_ev])
    assert len(intents) == 1
    assert intents[0].kind is OrderKind.EXIT
    assert "OPPOSING_AUCTION_EXIT" in intents[0].reason

