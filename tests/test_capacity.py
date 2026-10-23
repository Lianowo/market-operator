"""组合容量口径:一处公式,三种预算单位,旧行为被钉住。

这里刻意把 2026-10 之前的三份实现**逐字**复制成 oracle。它们不是"待清理的
脏代码",而是策略的一部分:实测两个对称口径都让净收益变差(explore|full:
含成本 +271.16、只算止损风险 +272.40,现行 +281.00),所以一旦有人把不对称
"顺手抹平",本文件的 oracle 断言会立刻失败,而不是悄悄改掉策略。
"""
import math

import pytest

from market_operator import capacity
from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind

T0 = 1_700_000_000_000
H = 3_600_000


def _specs(*symbols):
    return {s: ContractSpec(symbol=s, size_min=0.001, size_step=0.001,
                            size_place=3, price_place=2, min_trade_usdt=5.0)
            for s in symbols}


def _broker(mode=capacity.ADMISSION_FULL_COST, *, portfolio=0.01, tf=None):
    return Broker(equity=300.0, risk_frac=0.0075, leverage_cap=20.0,
                  taker_fee=0.0006, slippage_per_side=0.0002,
                  funding_per_8h=0.0001,
                  contracts=_specs("BTCUSDT@2h", "ETHUSDT@2h", "BTCUSDT@12h"),
                  portfolio_stop_risk_frac=portfolio,
                  tf_stop_risk_frac_by_tf=tf, capacity_mode=mode)


def _open(broker, symbol, invalid=95.0, price=100.0):
    broker.submit(OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                              reason="LEVEL_REJECTED|valid", ref_price=price,
                              ts=T0),
                  T0, direction=1, invalid=invalid,
                  event_id="LEVEL_REJECTED", target_1=110.0)
    row = broker.on_bar_open(symbol, T0 + H, price)[0]
    assert row["kind"] == "OPEN", row
    return row


# ---------------------------------------------------------------------------
# oracle:2026-10 之前的三份实现(逐字)
# ---------------------------------------------------------------------------


def _frozen_available_stop_risk(broker, equity, symbol=None):
    if broker.tf_stop_risk_frac_by_tf and symbol and "@" in symbol:
        tf_lbl = symbol.rsplit("@", 1)[1]
        tf_cap = broker.tf_stop_risk_frac_by_tf.get(tf_lbl)
        if tf_cap is not None:
            outstanding_tf = 0.0
            suffix = f"@{tf_lbl}"
            for sym, pos in broker.positions.items():
                if not sym.endswith(suffix):
                    continue
                if pos.invalid is None:
                    return 0.0
                mark = broker.last_mark.get(sym, pos.entry)
                outstanding_tf += pos.qty * max(
                    0.0, pos.direction * (mark - pos.invalid))
            avail_tf = max(0.0, equity * tf_cap - outstanding_tf)
            if broker.portfolio_stop_risk_frac is None:
                return avail_tf
            outstanding_all = 0.0
            for sym, pos in broker.positions.items():
                if pos.invalid is None:
                    return 0.0
                mark = broker.last_mark.get(sym, pos.entry)
                outstanding_all += pos.qty * max(
                    0.0, pos.direction * (mark - pos.invalid))
            avail_all = max(0.0, equity * broker.portfolio_stop_risk_frac
                            - outstanding_all)
            return min(avail_tf, avail_all)
    if broker.portfolio_stop_risk_frac is None:
        return float("inf")
    outstanding = 0.0
    for sym, pos in broker.positions.items():
        if pos.invalid is None:
            return 0.0
        mark = broker.last_mark.get(sym, pos.entry)
        outstanding += pos.qty * max(0.0,
                                     pos.direction * (mark - pos.invalid))
    return max(0.0, equity * broker.portfolio_stop_risk_frac - outstanding)


def _frozen_decision_worst_case(broker, entry_ref, invalid, equity,
                                tf_filter=None):
    """准入时的最坏情景 = Σ(止损亏损 + 平仓成本) + 新仓(止损 + 双边成本)。"""
    total = 0.0
    suffix = f"@{tf_filter}" if tf_filter else None
    for sym, pos in broker.positions.items():
        if suffix and not sym.endswith(suffix):
            continue
        mark = broker.last_mark.get(sym, pos.entry)
        total += pos.qty * max(0.0, pos.direction * (mark - pos.invalid))
        total += pos.qty * mark * (broker.taker_fee + broker.slippage)
    risk_per_unit = abs(entry_ref - invalid)
    qty = min(broker.current_risk_frac() * equity / risk_per_unit,
              broker._available_entry_notional(equity) / entry_ref)
    return total + qty * risk_per_unit + qty * entry_ref * 2.0 * (
        broker.taker_fee + broker.slippage)


# ---------------------------------------------------------------------------
# 纯函数:最坏情景的算术
# ---------------------------------------------------------------------------


def test_exposure_splits_stop_loss_and_exit_cost():
    exp = capacity.position_exposure(
        2.0, 1, 100.0, 95.0, taker_fee=0.0006, slippage=0.0002)
    assert exp.stop_loss == pytest.approx(10.0)
    assert exp.exit_cost == pytest.approx(200.0 * 0.0008)
    assert exp.total == pytest.approx(10.0 + 0.16)
    assert exp.measure(include_costs=False) == pytest.approx(10.0)
    assert (exp + exp).total == pytest.approx(2 * exp.total)


def test_missing_stop_yields_infinite_exposure_not_crash():
    exp = capacity.position_exposure(1.0, 1, 100.0, None,
                                     taker_fee=0.0, slippage=0.0)
    assert math.isinf(exp.stop_loss)


def test_short_side_exposure_uses_direction():
    exp = capacity.position_exposure(1.0, -1, 100.0, 105.0,
                                     taker_fee=0.0, slippage=0.0)
    assert exp.stop_loss == pytest.approx(5.0)


def test_qty_for_budget_is_inverse_of_delta_exposure():
    qty = 3.7
    delta = capacity.delta_exposure(qty, 100.0, 2.0, taker_fee=0.0006,
                                    slippage=0.0002, include_costs=True)
    back = capacity.qty_for_budget(delta, 100.0, 2.0, taker_fee=0.0006,
                                   slippage=0.0002, include_costs=True)
    assert back == pytest.approx(qty)


# ---------------------------------------------------------------------------
# 池与预算单位
# ---------------------------------------------------------------------------


def test_admission_mode_checks_only_timeframe_pool_at_decision():
    budget = capacity.RiskBudget(portfolio_frac=0.12,
                                 per_timeframe_frac={"2h": 0.0225},
                                 mode=capacity.ADMISSION_FULL_COST)
    pools = budget.pools("2h", moment=capacity.DECISION)
    assert [p[0] for p in pools] == ["tf"]
    assert pools[0][1] == pytest.approx(0.0225)
    # 成交时刻才把账户池并进来(取更紧者)
    assert [p[0] for p in budget.pools("2h", moment=capacity.FILL)] \
        == ["tf", "portfolio"]


def test_account_pool_is_actually_enforced_in_symmetric_modes():
    """v21 的 4×2.25% < 12%,账户池只有对称口径下才真正参与。"""
    for mode in (capacity.FULL_COST, capacity.STOP_RISK):
        budget = capacity.RiskBudget(portfolio_frac=0.02,
                                     per_timeframe_frac={"2h": 0.5},
                                     mode=mode)
        pools = budget.pools("2h", moment=capacity.DECISION)
        assert [p[0] for p in pools] == ["tf", "portfolio"]


def test_budget_unit_differs_by_moment_only_for_admission_mode():
    adm = capacity.RiskBudget(mode=capacity.ADMISSION_FULL_COST)
    assert adm.includes_costs(capacity.DECISION) is True
    assert adm.includes_costs(capacity.FILL) is False
    assert capacity.RiskBudget(mode=capacity.FULL_COST).includes_costs(
        capacity.FILL) is True
    assert capacity.RiskBudget(mode=capacity.STOP_RISK).includes_costs(
        capacity.DECISION) is False


def test_default_mode_is_production_behaviour():
    """沉默不得改变数字:不写 capacity_mode 时必须是现行口径。"""
    assert capacity.RiskBudget().mode == capacity.ADMISSION_FULL_COST


def test_invalid_mode_and_frac_are_rejected():
    with pytest.raises(ValueError, match="mode"):
        capacity.RiskBudget(mode="v21")
    with pytest.raises(ValueError, match="portfolio_frac"):
        capacity.RiskBudget(portfolio_frac=1.5)
    with pytest.raises(ValueError, match="per_timeframe_frac"):
        capacity.RiskBudget(per_timeframe_frac={"2h": 0.0})


# ---------------------------------------------------------------------------
# 接线:旧数字被逐位复现
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tf", [None, {"2h": 0.02}, {"2h": 0.02, "12h": 0.5}])
def test_admission_mode_reproduces_frozen_headroom(tf):
    broker = _broker(portfolio=0.01, tf=tf)
    _open(broker, "BTCUSDT@2h")
    _open(broker, "ETHUSDT@2h", invalid=98.0)
    equity = broker.equity_mtm()
    for symbol in ("BTCUSDT@2h", "BTCUSDT@12h", None):
        assert broker.available_risk_budget(equity, symbol) == pytest.approx(
            _frozen_available_stop_risk(broker, equity, symbol))


def test_admission_mode_reproduces_frozen_decision_gate():
    broker = _broker(portfolio=0.12, tf={"2h": 0.0225})
    _open(broker, "BTCUSDT@2h")
    equity = broker.equity_mtm()
    qty = broker.qty_for_entry(equity, 100.0, 95.0, "BTCUSDT@2h",
                               use_budget=False)
    delta = capacity.delta_exposure(
        qty, 100.0, 5.0, taker_fee=broker.taker_fee,
        slippage=broker.slippage, include_costs=True)
    used = broker.capacity_usage(equity, "BTCUSDT@2h",
                                 moment=capacity.DECISION)[1]
    assert used + delta == pytest.approx(
        _frozen_decision_worst_case(broker, 100.0, 95.0, equity, "2h"))


def test_symmetric_modes_shave_headroom_by_exit_costs():
    """对称口径只会更保守:平仓成本从额度里被扣掉。"""
    adm = _broker(portfolio=0.01)
    _open(adm, "BTCUSDT@2h")
    equity = adm.equity_mtm()
    full = _broker(portfolio=0.01, mode=capacity.FULL_COST)
    _open(full, "BTCUSDT@2h")
    assert full.available_risk_budget(equity) < \
        adm.available_risk_budget(equity)


def test_budget_clamps_entry_qty_but_unbudgeted_sizing_does_not():
    broker = _broker(portfolio=0.0005)
    equity = broker.equity_mtm()
    budgeted = broker.qty_for_entry(equity, 100.0, 99.0, "BTCUSDT@2h")
    full = broker.qty_for_entry(equity, 100.0, 99.0, "BTCUSDT@2h",
                                use_budget=False)
    assert budgeted < full
    assert full == pytest.approx(0.0075 * equity / 1.0)


def test_decision_gate_and_fill_clamp_agree():
    """对称口径下"过了门却被砍量"必须不可能:两者同解。"""
    broker = _broker(portfolio=0.03, mode=capacity.FULL_COST)
    _open(broker, "BTCUSDT@2h")
    equity = broker.equity_mtm()
    qty_full = broker.qty_for_entry(equity, 100.0, 99.0, "BTCUSDT@2h",
                                    use_budget=False)
    allowed = broker.qty_within_risk_budget(equity, 100.0, 1.0, "BTCUSDT@2h")
    headroom = broker._budget_headroom(equity, "BTCUSDT@2h",
                                       moment=capacity.DECISION)[0]
    delta = capacity.delta_exposure(
        qty_full, 100.0, 1.0, taker_fee=broker.taker_fee,
        slippage=broker.slippage, include_costs=True)
    assert (delta <= headroom + 1e-9) == (allowed >= qty_full - 1e-9)


def test_no_budget_configured_means_unlimited():
    broker = Broker(equity=300.0, risk_frac=0.0075, leverage_cap=5.0,
                    taker_fee=0.0006, slippage_per_side=0.0002,
                    funding_per_8h=0.0001, contracts=_specs("BTCUSDT"))
    assert broker.available_risk_budget(300.0) == float("inf")
    assert broker.capacity_usage(300.0) == (0.0, 0.0, float("inf"), "")


