"""同向预算覆盖在仓和活跃入场意图,且超限必须整笔拒绝。"""

from types import SimpleNamespace

import pytest

from market_operator import capacity
from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind, RestingLimitOrder
from market_operator.campaign import _submit_decisions

H = 3_600_000
SYMS = ("BTCUSDT@2h", "ETHUSDT@4h", "SOLUSDT@12h")


def broker(mode, cap=.015, fee=0, **kwargs):
    return Broker(equity=1000, risk_frac=.01, leverage_cap=20,
                  taker_fee=fee, slippage_per_side=0, funding_per_8h=0,
                  contracts={s: ContractSpec(s, .001, .001, 3, 2, 0) for s in SYMS},
                  directional_stop_risk_frac=cap, capacity_mode=mode, **kwargs)


def submit(b, symbol, kind="market", direction=1, t=0, **meta):
    stop, target = (90, 130) if direction == 1 else (110, 70)
    if kind == "market":
        b.submit(OrderIntent(OrderKind.OPEN, symbol, None, "TEST", 100, t), t,
                 direction=direction, invalid=stop, target_1=target, meta=meta)
    else:
        if kind == "watch":
            meta = {"watch_entry": True, "near_price": 101 if direction == 1 else 99,
                    **meta}
        b.submit_limit(RestingLimitOrder(direction, 100, t, t + 2 * H,
                                        symbol=symbol, invalid=stop, target_1=target,
                                        meta=meta))


@pytest.mark.parametrize("mode", capacity.MODES)
@pytest.mark.parametrize("first", ["market", "limit", "watch"])
@pytest.mark.parametrize("second", ["market", "limit", "watch"])
def test_direct_submit_counts_other_active_entry_intents(mode, first, second):
    b = broker(mode)
    submit(b, SYMS[0], first)
    submit(b, SYMS[1], second)
    assert b.n_skips.get("DIRECTIONAL_CAP") == 1
    assert not b.pending.get(SYMS[1]) and not b.resting.get(SYMS[1])
    assert b.ledger[-1]["reason_code"] == "DIRECTIONAL_CAP"


@pytest.mark.parametrize("mode", capacity.MODES)
@pytest.mark.parametrize("kind", ["market", "limit", "watch"])
def test_campaign_admission_counts_pending_without_sizing_down(mode, kind):
    b = broker(mode)
    submit(b, SYMS[0], kind)
    hyp = SimpleNamespace(direction=1, invalid=90, target_1=130, event_id="TEST",
                          regime=SimpleNamespace(value="range"), r_mult=3)
    intent = OrderIntent(OrderKind.OPEN, "ETHUSDT", None, "TEST", 100, 0)
    decision = SimpleNamespace(kind="market", intent=intent, hyp=hyp, route="TEST", meta={})
    counters = {"portfolio_cap_skips": 0, "open_intents": 0}
    skips = []
    _submit_decisions(SimpleNamespace(frame=SimpleNamespace(key=SYMS[1])), b,
                      [decision], 0, 0, 0, {}, counters, skips, 2, H)
    assert skips and skips[0]["reason_code"] == "DIRECTIONAL_CAP"
    assert counters == {"portfolio_cap_skips": 1, "open_intents": 0}
    assert not b.pending.get(SYMS[1])


@pytest.mark.parametrize("mode", capacity.MODES)
@pytest.mark.parametrize("kind", ["market", "limit", "watch"])
def test_opposite_direction_has_independent_budget(mode, kind):
    b = broker(mode)
    submit(b, SYMS[0], kind)
    submit(b, SYMS[1], kind, direction=-1)
    assert not b.n_skips
    assert b.directional_usage(1, moment=capacity.DECISION) == pytest.approx(10)
    assert b.directional_usage(-1, moment=capacity.DECISION) == pytest.approx(10)


@pytest.mark.parametrize("mode", capacity.MODES)
@pytest.mark.parametrize("release", ["cancel", "expire"])
def test_resting_release_is_derived_not_reserved(mode, release):
    b = broker(mode)
    submit(b, SYMS[0], "watch")
    if release == "cancel":
        b.cancel_resting(SYMS[0], H, reason_code="TEST", detail="test")
    else:
        b.on_15m_bar(SYMS[0], 3 * H, 110, 111, 109, 110)
    submit(b, SYMS[1])
    assert not b.n_skips
    assert b.directional_usage(1, moment=capacity.DECISION) == pytest.approx(10)


@pytest.mark.parametrize("mode", capacity.MODES)
@pytest.mark.parametrize("kind", ["market", "limit", "watch"])
def test_fill_does_not_count_current_order_twice(mode, kind):
    b = broker(mode, cap=.011)
    submit(b, SYMS[0], kind)
    if kind == "market":
        row, = b.on_bar_open(SYMS[0], H, 100)
    elif kind == "limit":
        row, = b.on_15m_bar(SYMS[0], H, 100, 101, 99, 100)
    else:
        trigger, = b.on_15m_bar(SYMS[0], H, 101, 103, 100.5, 102)
        assert trigger["kind"] == "TRIGGER"
        row, = b.on_bar_open(SYMS[0], H + 900_000, 102)
    assert row["kind"] in ("OPEN", "LIMIT_FILL")
    assert len(b.positions) == 1


@pytest.mark.parametrize("mode", capacity.MODES)
@pytest.mark.parametrize("kind", ["market", "limit"])
def test_fill_rechecks_capital_and_other_pending_whole_order(mode, kind):
    b = broker(mode, cap=.021)
    submit(b, SYMS[0], kind)
    submit(b, SYMS[1])
    # Dynamic tier raises the current full order, but not the older small intent.
    b.pending[SYMS[1]][0].meta["size_mult"] = .5
    b.risk_frac = .02
    before = b.cash
    if kind == "market":
        row, = b.on_bar_open(SYMS[0], H, 100)
    else:
        row, = b.on_15m_bar(SYMS[0], H, 100, 101, 99, 100)
    assert row["kind"] == "SKIP" and row["reason_code"] == "DIRECTIONAL_CAP"
    assert not b.positions and b.cash == before


@pytest.mark.parametrize("mode", capacity.MODES)
def test_usage_revalues_capital_size_multiplier_and_cost_mode(mode):
    b = broker(mode, cap=.1, fee=.001)
    submit(b, SYMS[0], "market", size_mult=.5)
    submit(b, SYMS[1], "limit")
    b.cash = 800
    for moment in capacity.MOMENTS:
        expected = 12 + (.24 if b.risk_budget.includes_costs(moment) else 0)
        assert b.directional_usage(1, moment=moment) == pytest.approx(expected)


def test_ordinary_budget_still_clips_but_directional_does_not():
    b = broker(capacity.STOP_RISK, cap=.015, portfolio_stop_risk_frac=.005)
    submit(b, SYMS[0])
    row, = b.on_bar_open(SYMS[0], H, 100)
    assert row["kind"] == "OPEN" and row["qty"] == .5


@pytest.mark.parametrize("mode", capacity.MODES)
def test_none_cap_preserves_uncapped_ledger(mode):
    a = broker(mode, cap=None)
    submit(a, SYMS[0], "market")
    submit(a, SYMS[1], "limit")
    a.on_bar_open(SYMS[0], H, 101)
    a.on_15m_bar(SYMS[1], H, 100, 101, 99, 100)
    assert [(r["kind"], r["qty"], r["price"], r["cash_after"])
            for r in a.ledger] == [("OPEN", .909, 101, 1000),
                                    ("LIMIT_FILL", 1, 100, 1000)]
    assert not a.n_skips


@pytest.mark.parametrize("kind", ["market", "limit"])
def test_same_symbol_later_unprocessed_order_remains_visible(kind):
    b = broker(capacity.STOP_RISK, cap=.021)
    submit(b, SYMS[0], kind)
    submit(b, SYMS[0], kind)
    b.risk_frac = .02
    if kind == "market":
        rows = b.on_bar_open(SYMS[0], H, 100)
    else:
        rows = b.on_15m_bar(SYMS[0], H, 100, 101, 99, 100)
    assert rows[0]["reason_code"] == "DIRECTIONAL_CAP"
    assert rows[1]["kind"] in ("OPEN", "LIMIT_FILL")


@pytest.mark.parametrize("kind", ["market", "limit"])
def test_fill_rechecks_cash_after_prior_entry_fee(kind):
    b = broker(capacity.STOP_RISK, cap=.02, fee=.001)
    submit(b, SYMS[0])
    submit(b, SYMS[1], kind)
    b.on_bar_open(SYMS[0], H, 100)
    assert b.cash == pytest.approx(999.9)
    # Smaller contract step prevents rounding from hiding the fee-induced overrun.
    b.contracts[SYMS[1]] = ContractSpec(SYMS[1], .000001, .000001, 6, 2, 0)
    if kind == "market":
        row, = b.on_bar_open(SYMS[1], H, 100)
    else:
        row, = b.on_15m_bar(SYMS[1], H, 100, 101, 99, 100)
    assert row["reason_code"] == "DIRECTIONAL_CAP" and row["kind"] == "SKIP"
    assert len(b.positions) == 1


def test_real_fill_geometry_changes_full_cost_budget():
    b = broker(capacity.FULL_COST, cap=.012, fee=.001)
    submit(b, SYMS[0])  # reference risk 10 + cost .2 fits in 12
    row, = b.on_bar_open(SYMS[0], H, 90.5)  # full qty 20; risk10 + cost3.62
    assert row["reason_code"] == "DIRECTIONAL_CAP"
    assert not b.positions and b.cash == 1000


def test_watch_trigger_replaces_resting_intent_without_double_reserve():
    b = broker(capacity.STOP_RISK, cap=.021)
    submit(b, SYMS[0], "watch")
    submit(b, SYMS[1])
    trigger, = b.on_15m_bar(SYMS[0], H, 101, 103, 100.5, 102)
    assert trigger["kind"] == "TRIGGER"
    assert not b.resting.get(SYMS[0])
    assert b.directional_usage(1, moment=capacity.FILL) == pytest.approx(20)


def test_floating_profit_is_not_pending_risk_capital():
    b = broker(capacity.STOP_RISK, cap=.5)
    submit(b, SYMS[0])
    b.on_bar_open(SYMS[0], H, 100)
    b.last_mark[SYMS[0]] = 120
    submit(b, SYMS[1])
    assert b.equity_mtm() == 1020
    # In-position risk is now 30, pending remains 10 (not 10.2 from floating equity).
    assert b.directional_usage(1, moment=capacity.DECISION) == pytest.approx(40)


@pytest.mark.parametrize("mode", capacity.MODES)
@pytest.mark.parametrize("kind", ["market", "limit", "watch"])
def test_single_oversized_entry_is_rejected_not_clipped(mode, kind):
    b = broker(mode, cap=.005)
    submit(b, SYMS[0], kind)
    assert b.ledger[-1]["reason_code"] == "DIRECTIONAL_CAP"
    assert not b.pending and not b.resting and not b.positions
    assert b.cash == 1000


@pytest.mark.parametrize("mic", [False, True])
def test_other_execution_clock_intents_remain_visible(mic):
    b = broker(capacity.STOP_RISK, cap=.021)
    submit(b, SYMS[0], mic=not mic)
    submit(b, SYMS[0], mic=mic)
    b.risk_frac = .02
    row, = b.on_bar_open(SYMS[0], H, 100, mic_only=mic)
    assert row["reason_code"] == "DIRECTIONAL_CAP"
    assert len(b.pending[SYMS[0]]) == 1


def test_unfilled_resting_entry_still_counts_during_same_symbol_matching():
    b = broker(capacity.STOP_RISK, cap=.021)
    submit(b, SYMS[0], "limit")
    submit(b, SYMS[0], "limit")
    b.resting[SYMS[0]][0].limit_price = 95
    b.risk_frac = .02
    row, = b.on_15m_bar(SYMS[0], H, 100, 101, 99, 100)
    assert row["reason_code"] == "DIRECTIONAL_CAP"
    assert b.resting[SYMS[0]][0].limit_price == 95


def test_reduce_intents_do_not_reserve_entry_directional_risk():
    b = broker(capacity.STOP_RISK)
    b.submit(OrderIntent(OrderKind.REDUCE, SYMS[0], .5, "TEST", 100, 0), 0)
    b.submit_limit(RestingLimitOrder(-1, 110, 0, 2 * H, symbol=SYMS[0],
                                    role="reduce", reduce_qty=.5))
    submit(b, SYMS[1])
    assert not b.n_skips
    assert b.directional_usage(1, moment=capacity.DECISION) == pytest.approx(10)
    assert b.directional_usage(-1, moment=capacity.DECISION) == 0