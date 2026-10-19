"""阶段 5 模拟经纪商测试:费用/资金费/滑点精确数值、rounding 向下、
MIN_SIZE 拒绝、leverage cap 缩量、强平保护、权益盯市手算路径一致性。

统一合约规格:step=0.01(sizePlace=2)、sizeMin=0.01、minTradeUSDT=5;
成本先验取自 config/defaults.json(taker 0.0006/slip 0.0002/funding 0.0001 每 8h),
cost_multiplier 默认 1.0。bar ts 为分钟级递增毫秒值,决策 bar t ⇒ 成交 bar t+1。
"""

from __future__ import annotations

import pytest

from market_operator.broker import (
    ContractSpec,
    Broker,
    _floor_step,
    fetch_contract_specs,
)
from market_operator.strategy import OrderIntent, OrderKind

H = 3_600_000
T0 = 1_700_000_000_000


def _spec(step=0.01, place=2, size_min=0.01, min_usdt=5.0) -> ContractSpec:
    return ContractSpec(symbol="TEST", size_min=size_min, size_step=step,
                        size_place=place, price_place=2, min_trade_usdt=min_usdt)


def _broker(equity=300.0, cost_multiplier=1.0, spec=None, **kw) -> Broker:
    cfg = dict(risk_frac=0.0075, leverage_cap=5.0, taker_fee=0.0006,
               slippage_per_side=0.0002, funding_per_8h=0.0001)
    cfg.update(kw)
    return Broker(equity, contracts={"TEST": spec or _spec()},
                  cost_multiplier=cost_multiplier, **cfg)


def _intent(kind, frac=None, reason="§X|test intent", ref=100.0, ts=0):
    return OrderIntent(kind=kind, symbol="TEST", qty_frac=frac,
                       reason=reason, ref_price=ref, ts=ts)


def _open_short(b: Broker, invalid=101.0, decision_ts=T0, meta=None):
    m = {"direction": -1}
    m.update(meta or {})
    b.submit(_intent(OrderKind.OPEN, None, "E1|short GO test", 100.0, decision_ts),
             decision_ts=decision_ts, direction=-1, invalid=invalid,
             target_1=94.0, event_id="E1", meta=m)


# ---------------------------------------------------------------------------
# OPEN:sizing / 滑点 / 费用 精确值
# ---------------------------------------------------------------------------


def test_open_short_sizing_fee_slippage_exact():
    b = _broker()
    _open_short(b, invalid=101.0)
    rows = b.on_bar_open("TEST", T0 + H, 100.0)
    assert len(rows) == 1
    r = rows[0]
    # 成交价 = open·(1 − 0.0002)(卖出方向不利)
    assert r["price"] == pytest.approx(100.0 * (1 - 0.0002), rel=0, abs=1e-12)
    price = 99.98
    # qty = 0.0075·300/|99.98−101| = 2.205882…→ 向下取整 2.20(非最近取整 2.21)
    assert r["qty"] == 2.20
    notional = 2.20 * price                                   # 219.956
    assert r["fee"] == pytest.approx(notional * 0.0006, rel=1e-12)
    assert r["side"] == "sell" and r["kind"] == "OPEN"
    assert r["used_leverage"] == pytest.approx(notional / 300.0, rel=1e-12)
    assert r["used_leverage"] <= 5.0
    assert b.cash == pytest.approx(300.0 - notional * 0.0006, rel=1e-12)
    assert r["decision_ts"] == T0 and r["ts"] == T0 + H
    assert r["reason_code"] == "E1"


def test_open_long_slippage_adverse_buy():
    b = _broker()
    b.submit(_intent(OrderKind.OPEN, None, "E6|long GO test", 100.0, T0),
             decision_ts=T0, direction=+1, invalid=99.0, target_1=104.0,
             event_id="E6", meta={"direction": +1})
    rows = b.on_bar_open("TEST", T0 + H, 100.0)
    assert rows[0]["price"] == pytest.approx(100.0 * (1 + 0.0002), abs=1e-12)
    assert rows[0]["side"] == "buy"


def test_leverage_cap_shrinks_qty():
    b = _broker()
    _open_short(b, invalid=100.1)          # risk/unit=0.12 ⇒ qty_risk=18.75
    rows = b.on_bar_open("TEST", T0 + H, 100.0)
    # cap: qty = 5·300/99.98 = 15.003…→ 15.00(向下取整),而非风险量 18.75
    assert rows[0]["qty"] == 15.00
    assert rows[0]["used_leverage"] == pytest.approx(15.00 * 99.98 / 300.0,
                                                     rel=1e-12)
    assert rows[0]["used_leverage"] <= 5.0


def test_min_size_reject_notional_and_qty():
    # notional 不足:invalid 远 ⇒ qty 0.01,notional ≈ 1 < 5 ⇒ SKIP(MIN_SIZE)
    b = _broker()
    _open_short(b, invalid=300.0)
    rows = b.on_bar_open("TEST", T0 + H, 100.0)
    r = rows[0]
    assert r["kind"] == "SKIP" and r["reason_code"] == "MIN_SIZE"
    assert b.positions == {} and b.n_skips.get("MIN_SIZE") == 1
    assert b.equity_mtm() == pytest.approx(300.0)              # 无成交无费用

    # qty 取整后为 0 ⇒ SKIP(MIN_SIZE)
    b2 = _broker()
    _open_short(b2, invalid=1000.0)
    rows2 = b2.on_bar_open("TEST", T0 + H, 100.0)
    assert rows2[0]["kind"] == "SKIP" and rows2[0]["reason_code"] == "MIN_SIZE"
    assert b2.positions == {}


def test_dup_position_rejected():
    b = _broker()
    _open_short(b)
    b.on_bar_open("TEST", T0 + H, 100.0)
    _open_short(b, decision_ts=T0 + H)
    rows = b.on_bar_open("TEST", T0 + 2 * H, 100.0)
    assert rows[0]["kind"] == "SKIP" and rows[0]["reason_code"] == "DUP_POSITION"
    assert b.position_of("TEST").qty == 2.20                   # 原仓不受影响


def test_floor_step_rounds_down():
    assert _floor_step(2.209999, 0.01, 2) == 2.20
    assert _floor_step(0.00015, 0.0001, 4) == 0.0001
    assert _floor_step(0.000099, 0.0001, 4) == 0.0


def test_fill_after_decision_assertion():
    b = _broker()
    _open_short(b, decision_ts=T0 + H)
    with pytest.raises(AssertionError):
        b.on_bar_open("TEST", T0 + H, 100.0)                   # 同 bar 成交禁止
    _open_short(b, decision_ts=T0 + H)                         # 重报(上笔已弹队列)
    with pytest.raises(AssertionError):
        b.on_bar_open("TEST", T0, 100.0)                       # 早于决策亦禁止


# ---------------------------------------------------------------------------
# 资金费计提与平仓结算(精确到小时比例)
# ---------------------------------------------------------------------------


def _open_and_hold(b: Broker, marks: int) -> None:
    """bar1 开盘开空(价格 99.98, qty 2.20),随后 marks 根 bar 以 close=100 盯市。"""
    _open_short(b)
    b.on_bar_open("TEST", T0 + H, 100.0)
    for k in range(1, marks + 1):
        b.mark("TEST", T0 + (1 + k) * H, 100.0)


def test_funding_accrual_and_settlement_exact():
    b = _broker()
    # 开空 @ bar1 开盘;mark bar2..bar8(7h);bar9 开盘平仓(再 1h) ⇒ 共 8h
    _open_and_hold(b, 7)
    pos = b.position_of("TEST")
    assert pos.funding_acc == pytest.approx(
        7 * 2.20 * 100.0 * 0.0001 / 8.0, rel=1e-12)            # 0.01925
    acc_before = pos.funding_acc

    b.submit(_intent(OrderKind.EXIT, 1.0, "§8.1|test exit", 100.0, T0 + 8 * H),
             decision_ts=T0 + 8 * H)
    rows = b.on_bar_open("TEST", T0 + 9 * H, 100.0)
    r = rows[0]
    buy_price = 100.0 * (1 + 0.0002)                           # 100.02
    acc_total = acc_before + 2.20 * buy_price * 0.0001 / 8.0   # +平仓前 1h
    assert r["price"] == pytest.approx(buy_price, abs=1e-12)
    assert r["funding_delta"] == pytest.approx(-acc_total, rel=1e-12)
    assert r["realized_pnl"] == pytest.approx((99.98 - buy_price) * 2.20,
                                              rel=1e-12)       # −0.088
    exit_fee = 2.20 * buy_price * 0.0006
    assert b.cash == pytest.approx(
        300.0 - 2.20 * 99.98 * 0.0006                 # 开仓费
        - exit_fee                                     # 平仓费
        + (99.98 - buy_price) * 2.20                   # 已实现盈亏
        - acc_total, rel=1e-12)                        # 资金费结算
    assert b.positions == {} and b.equity_mtm() == b.cash


def test_reduce_does_not_settle_funding():
    b = _broker()
    _open_and_hold(b, 1)                                       # 1h 计提到 bar2
    b.submit(_intent(OrderKind.REDUCE, 0.5, "§8.2|test reduce", 100.0,
                     T0 + 2 * H), decision_ts=T0 + 2 * H)
    rows = b.on_bar_open("TEST", T0 + 3 * H, 100.0)
    r = rows[0]
    assert r["kind"] == "REDUCE" and r["qty"] == 1.10          # 0.5·2.20
    assert r["funding_delta"] == 0.0                           # 部分平仓不结算
    pos = b.position_of("TEST")
    assert pos is not None and pos.qty == 1.10
    assert pos.funding_acc > 0.0                               # 累计继续挂账


# ---------------------------------------------------------------------------
# 权益 mark-to-market 已知路径手算一致性
# ---------------------------------------------------------------------------


def test_equity_mtm_hand_computed_path():
    b = _broker()
    # ① bar1 开盘开空:price 99.98, qty 2.20, fee1 = 2.20·99.98·0.0006
    _open_short(b)
    b.on_bar_open("TEST", T0 + H, 100.0)
    fee1 = 2.20 * 99.98 * 0.0006
    cash = 300.0 - fee1
    assert b.cash == pytest.approx(cash, rel=1e-12)

    # ② bar2 盯市 close=99(1h 计提)
    b.mark("TEST", T0 + 2 * H, 99.0)
    acc2 = 2.20 * 99.0 * 0.0001 / 8.0
    upnl2 = (99.98 - 99.0) * 2.20
    assert b.equity_mtm() == pytest.approx(cash + upnl2 - acc2, rel=1e-12)

    # ③ bar2 收盘决策 REDUCE 0.5;bar3 开盘 98 成交(buy 98.0196, qty 1.1)
    b.submit(_intent(OrderKind.REDUCE, 0.5, "§8.2|reduce", 98.0, T0 + 2 * H),
             decision_ts=T0 + 2 * H)
    b.on_bar_open("TEST", T0 + 3 * H, 98.0)
    p_reduce = 98.0 * 1.0002
    fee2 = 1.1 * p_reduce * 0.0006
    pnl2 = (99.98 - p_reduce) * 1.1
    acc3 = acc2 + 2.20 * p_reduce * 0.0001 / 8.0     # bar2→bar3 1h(全仓 2.2)
    cash += pnl2 - fee2
    assert b.cash == pytest.approx(cash, rel=1e-12)
    pos = b.position_of("TEST")
    assert pos.qty == 1.1 and pos.funding_acc == pytest.approx(acc3, rel=1e-12)

    # ④ bar3 盯市 close=98(dt=0,只换 mark 价),bar4 盯市 close=99(1h,余仓 1.1)
    b.mark("TEST", T0 + 3 * H, 98.0)
    b.mark("TEST", T0 + 4 * H, 99.0)
    acc4 = acc3 + 1.1 * 99.0 * 0.0001 / 8.0
    upnl4 = (99.98 - 99.0) * 1.1
    assert b.equity_mtm() == pytest.approx(cash + upnl4 - acc4, rel=1e-12)

    # ⑤ bar4 收盘决策 EXIT;bar5 开盘 99.5 成交(buy 99.5199),结算全部资金费
    b.submit(_intent(OrderKind.EXIT, 0.5, "§8.1|exit", 99.0, T0 + 4 * H),
             decision_ts=T0 + 4 * H)
    rows = b.on_bar_open("TEST", T0 + 5 * H, 99.5)
    p_exit = 99.5 * 1.0002
    acc5 = acc4 + 1.1 * p_exit * 0.0001 / 8.0
    fee3 = 1.1 * p_exit * 0.0006
    pnl3 = (99.98 - p_exit) * 1.1
    final_cash = cash + pnl3 - fee3 - acc5
    r = rows[0]
    assert r["qty"] == 1.1                                     # EXIT=剩余全部
    assert r["funding_delta"] == pytest.approx(-acc5, rel=1e-12)
    assert b.cash == pytest.approx(final_cash, rel=1e-12)
    assert b.equity_mtm() == pytest.approx(final_cash, rel=1e-12)
    # 账本对账:equity0 + Σ(pnl − fee + funding_delta) == cash
    recon = 300.0 + sum(x["realized_pnl"] - x["fee"] + x["funding_delta"]
                        for x in b.ledger)
    assert recon == pytest.approx(b.cash, rel=1e-12)


# ---------------------------------------------------------------------------
# 强平保护(风控失效保护;触发路径精确可算)
# ---------------------------------------------------------------------------


def test_liquidation_triggers_and_fills_next_open():
    b = _broker()
    _open_short(b, invalid=100.1)                              # cap ⇒ qty 15.00
    b.on_bar_open("TEST", T0 + H, 100.0)
    assert b.position_of("TEST").qty == 15.00
    # close=110:浮亏 = 15·(110−99.98) = 150.3 ≥ 0.9·equity(≈149.6) ⇒ 触发
    b.mark("TEST", T0 + 2 * H, 110.0)
    assert b.n_liquidations == 1
    # 强平挂单下一根 bar 开盘成交,账本 LIQUIDATION,结算累计资金费
    rows = b.on_bar_open("TEST", T0 + 3 * H, 110.0)
    r = rows[0]
    assert r["kind"] == "LIQUIDATION" and r["reason_code"] == "LIQUIDATION"
    assert r["side"] == "buy"
    assert r["price"] == pytest.approx(110.0 * 1.0002, abs=1e-9)
    assert r["qty"] == 15.00
    assert r["funding_delta"] < 0.0
    assert b.positions == {}
    assert b.cash < 300.0 - 100.0                              # 大额已实现亏损


def test_no_liquidation_under_normal_risk():
    b = _broker()
    _open_short(b, invalid=101.0)
    b.on_bar_open("TEST", T0 + H, 100.0)
    for k in range(2, 12):                                     # 温和逆行
        b.mark("TEST", T0 + k * H, 101.0 + k * 0.01)
    assert b.n_liquidations == 0


# ---------------------------------------------------------------------------
# cost_multiplier 同时放大 fee/slippage/funding
# ---------------------------------------------------------------------------


def test_cost_multiplier_scales_all_costs():
    b = _broker(cost_multiplier=2.0)
    _open_short(b)
    rows = b.on_bar_open("TEST", T0 + H, 100.0)
    r = rows[0]
    price = 100.0 * (1 - 0.0004)                               # 滑点 ×2
    assert r["price"] == pytest.approx(price, abs=1e-12)
    # qty = 2.25/|99.96−101| = 2.1634…→ 2.16
    assert r["qty"] == 2.16
    assert r["fee"] == pytest.approx(2.16 * price * 0.0012, rel=1e-12)  # 费 ×2
    b.mark("TEST", T0 + 2 * H, 100.0)
    pos = b.position_of("TEST")
    assert pos.funding_acc == pytest.approx(
        2.16 * 100.0 * 0.0002 / 8.0, rel=1e-12)                # 资金费 ×2


# ---------------------------------------------------------------------------
# 合约规格解析(实际响应字段映射)
# ---------------------------------------------------------------------------


def test_contract_spec_from_real_entry_shape():
    entry = {"symbol": "BTCUSDT", "minTradeNum": "0.0001",
             "volumePlace": "4", "pricePlace": "1",
             "sizeMultiplier": "0.0001", "minTradeUSDT": "5",
             "takerFeeRate": "0.0006"}
    spec = ContractSpec.from_entry(entry)
    assert spec.size_min == 0.0001 and spec.size_step == 0.0001
    assert spec.size_place == 4 and spec.min_trade_usdt == 5.0


def test_fetch_contract_specs_cache_roundtrip(tmp_path, monkeypatch):
    import market_operator.broker as br

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"code": "00000", "data": [
                {"symbol": "AAA", "minTradeNum": "0.01", "volumePlace": "2",
                 "pricePlace": "2", "sizeMultiplier": "0.01",
                 "minTradeUSDT": "5"}]}

    cache = tmp_path / "contracts.json"
    monkeypatch.setattr(br.requests, "get", lambda *a, **k: _Resp())
    specs, src = fetch_contract_specs(["AAA"], cache)
    assert src == "live" and specs["AAA"].size_min == 0.01 and cache.exists()

    def _boom(*a, **k):
        raise br.requests.RequestException("offline")

    monkeypatch.setattr(br.requests, "get", _boom)
    specs2, src2 = fetch_contract_specs(["AAA"], cache)
    assert src2.startswith("cache(") and specs2["AAA"].size_place == 2
