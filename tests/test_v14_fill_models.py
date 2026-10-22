"""v14 broker 新成交语义(docs/v14-protocol.md C2/C3/C4/C5)机械测试。

钉死:virtual 价达(含持平)触发、次细 bar 开盘 taker+滑点成交与超时;
stop-entry 严格穿过才触发(持平不触发)、超时作废(EXPIRED,漏单);
exchange 真挂单严格穿过、持平不成交(与 virtual 同价触发的对照差);
maker/taker 费用分账;分层 3 份合并均价/净费用/同 trade_id/异组 DUP;
size_mult 定仓缩放;TRIGGER/EXPIRED 账本行 causal(decision<fill)。

时间口径:sell limit 的 touch=high==L(穿过=h>L);buy 侧镜像;挂单可成
交窗口 = 所属 1h bar 标签 ∈ (fill_after, expiry](与 canonical LIMIT_FILL
同比),armed=0、fill_after=0 ⇒ 首个可成交 bar 为 ts≥3_600_000。
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

SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
H = 3_600_000


def _broker(**kw):
    arg = dict(equity=300.0, risk_frac=0.0075, leverage_cap=5.0,
               taker_fee=0.0006, maker_fee=0.0002, slippage_per_side=0.0002,
               funding_per_8h=0.0001, contracts={"TEST": SPEC})
    arg.update(kw)
    return Broker(**arg)


def _order(**kw):
    arg = dict(direction=-1, limit_price=100.0, armed_at_ts=0,
               expiry_ts=12 * H, fill_after_ts=0, symbol="TEST",
               role="entry", invalid=104.0, target_1=None, event_id="BREAKOUT_ACCEPTED",
               reason="T|v14", meta={"direction": -1})
    arg.update(kw)
    return RestingLimitOrder(**arg)


def _bar(b, ts, o, h, l, c):
    """on_bar_open + on_15m_bar + mark 顺序推进(campaign 的 ①②④)。"""
    rows = b.on_bar_open("TEST", ts, o)
    rows += b.on_15m_bar("TEST", ts, o, h, l, c)
    b.mark("TEST", ts, c)
    return rows


# ---------------------------------------------------------------------------
# C2 virtual_limit:价达(含持平)触发 ⇒ 次细 bar 开盘 taker 成交
# ---------------------------------------------------------------------------


def test_virtual_touch_at_equality_triggers_and_fills_next_open_taker():
    b = _broker()
    b.submit_limit(_order(fill_model="virtual"))
    # bar1(ts=1h):high 恰 = 100(sell 侧 touch/持平)⇒ virtual 触发
    rows = _bar(b, 1 * H, 99.0, 100.0, 98.5, 99.5)
    trig = [r for r in rows if r["kind"] == "TRIGGER"]
    assert len(trig) == 1 and trig[0]["reason_code"] == "VIRTUAL_TRIGGER"
    assert not any(r["kind"] == "LIMIT_FILL" for r in rows)
    # bar2(ts=2h):次细 bar 开盘 99.8 ⇒ taker+滑点(sell:price=o·(1−slip))
    rows = _bar(b, 2 * H, 99.8, 100.4, 99.4, 100.0)
    opens = [r for r in rows if r["kind"] == "OPEN"]
    assert len(opens) == 1
    o = opens[0]
    assert o["price"] == pytest.approx(99.8 * (1 - 0.0002))
    assert o["meta"]["entry_mode"] == "virtual_limit"
    assert o["meta"]["trigger_ts"] == 1 * H and o["meta"]["limit_price"] == 100.0
    assert o["fee"] == pytest.approx(o["qty"] * o["price"] * 0.0006)   # taker
    assert o["decision_ts"] == 1 * H < o["ts"] == 2 * H                # 因果
    assert b.position_of("TEST") is not None


def test_exchange_limit_strict_cross_vs_virtual_contrast():
    be, bv = _broker(), _broker()
    be.submit_limit(_order())                        # exchange(缺省 fill_model)
    bv.submit_limit(_order(fill_model="virtual"))
    # 持平(high 恰 = 100):exchange 严格穿过不成交,virtual 价达触发
    re = _bar(be, 1 * H, 99.0, 100.0, 98.5, 99.5)
    rv = _bar(bv, 1 * H, 99.0, 100.0, 98.5, 99.5)
    assert not any(r["kind"] in ("LIMIT_FILL", "TRIGGER") for r in re)
    assert any(r["kind"] == "TRIGGER" for r in rv)
    assert be.has_entry_limit("TEST") and not bv.has_entry_limit("TEST")
    # 穿过(high=100.1 > 100)⇒ exchange 于本 bar 限价成交 maker
    re = _bar(be, 2 * H, 99.5, 100.1, 99.2, 100.0)
    lf = [r for r in re if r["kind"] == "LIMIT_FILL"]
    assert len(lf) == 1 and lf[0]["price"] == 100.0
    assert lf[0]["fee"] == pytest.approx(lf[0]["qty"] * 100.0 * 0.0002)  # maker
    assert lf[0]["meta"]["entry_mode"] == "limit"
    assert lf[0]["decision_ts"] == 0 < lf[0]["ts"]                       # 因果


def test_virtual_expires_untouched():
    b = _broker()
    b.submit_limit(_order(fill_model="virtual"))
    rows = []
    for k in range(1, 15):                           # 12h 窗口内价不到(high 恒 <100)
        rows += _bar(b, k * H, 99.0, 99.8, 98.2, 99.0)
    exp = [r for r in rows if r["kind"] == "EXPIRED"]
    assert len(exp) == 1 and exp[0]["ts"] == 13 * H  # 首个越 expiry 的 bar
    assert b.position_of("TEST") is None


# ---------------------------------------------------------------------------
# C3 stop-entry:严格穿过触发(持平不)、次细 bar 开盘 taker、超时作废
# ---------------------------------------------------------------------------


def test_stop_entry_cross_triggers_equality_does_not():
    b = _broker()
    b.submit_limit(_order(direction=+1, limit_price=100.0, invalid=96.0,
                          is_stop=True, meta={"direction": 1}))
    rows = _bar(b, 1 * H, 99.0, 100.0, 98.5, 99.5)    # buy stop:high 恰=100 不触发
    assert not any(r["kind"] == "TRIGGER" for r in rows)
    rows = _bar(b, 2 * H, 99.5, 100.1, 99.2, 100.0)   # 穿过 ⇒ STOP_TRIGGER
    trig = [r for r in rows if r["kind"] == "TRIGGER"]
    assert len(trig) == 1 and trig[0]["reason_code"] == "STOP_TRIGGER"
    rows = _bar(b, 3 * H, 100.0, 100.5, 99.8, 100.2)  # 次细 bar 开盘成交
    o = [r for r in rows if r["kind"] == "OPEN"]
    assert len(o) == 1
    assert o[0]["price"] == pytest.approx(100.0 * (1 + 0.0002))    # buy taker 滑点
    assert o[0]["meta"]["entry_mode"] == "stop_entry"
    assert o[0]["decision_ts"] == 2 * H < o[0]["ts"] == 3 * H      # 因果
    assert b.position_of("TEST") is not None


def test_stop_entry_expires_after_window():
    b = _broker()
    b.submit_limit(_order(direction=+1, limit_price=100.0, invalid=96.0,
                          is_stop=True, expiry_ts=4 * H, fill_after_ts=0,
                          meta={"direction": 1}))
    rows = []
    for k in range(1, 7):                            # 窗口 hour_label ∈ (0, 4h]
        rows += _bar(b, k * H, 99.0, 99.5, 98.5, 99.0)
    exp = [r for r in rows if r["kind"] == "EXPIRED"]
    assert len(exp) == 1 and exp[0]["ts"] == 5 * H   # 漏单=超时作废
    assert b.position_of("TEST") is None


# ---------------------------------------------------------------------------
# C4 分层:3 份合并均价/净费用/同 trade_id;异组 DUP;未成交份过期
# ---------------------------------------------------------------------------


def _ladder_group(b, group="TEST#0#LEVEL_REJECTED"):
    """sell 分层三份:前缘 100 / 中 101 / 内缘 102,invalid 108,各 1/3 风险份。"""
    for j, px in enumerate((100.0, 101.0, 102.0)):
        b.submit_limit(_order(limit_price=px, invalid=108.0,
                              ladder_group=group, event_id="LEVEL_REJECTED",
                              meta={"direction": -1, "size_mult": 1.0 / 3.0,
                                    "order_type": "ladder_entry",
                                    "ladder_slice": j}))


def test_ladder_two_slices_merge_weighted_entry_same_trade():
    b = _broker()
    _ladder_group(b)
    # 穿过前缘(100)与中(101):两份同 bar 成交;内缘 102 未达
    rows = _bar(b, 1 * H, 99.0, 101.5, 98.5, 99.5)
    lf = [r for r in rows if r["kind"] == "LIMIT_FILL"]
    assert len(lf) == 2
    assert len({r["trade_id"] for r in lf}) == 1               # 同一笔交易
    add = [r for r in lf if r["meta"].get("ladder_add")]
    assert len(add) == 1 and add[0]["meta"]["ladder_group"] == "TEST#0#LEVEL_REJECTED"
    pos = b.position_of("TEST")
    assert pos is not None
    # 各份 qty = floor_step(risk_frac/3·equity/|px−invalid|, 0.01):0.09375→0.09、
    # ≈0.1071→0.10(equity 微变不影响取整格)
    q1, q2 = 0.09, 0.10
    assert pos.qty == pytest.approx(q1 + q2)
    assert pos.entry == pytest.approx((q1 * 100.0 + q2 * 101.0) / (q1 + q2))
    fees = sum(r["fee"] for r in lf)
    assert fees == pytest.approx((q1 * 100.0 + q2 * 101.0) * 0.0002)  # maker 净费
    for r in rows:
        assert r["ts"] > r["decision_ts"]                      # causal 全行


def test_ladder_unfilled_slice_expires():
    b = _broker()
    _ladder_group(b)
    _bar(b, 1 * H, 99.0, 101.5, 98.5, 99.5)                    # 前/中两份成
    assert b.position_of("TEST") is not None
    rows = []
    for k in range(2, 16):                                     # 价不回头
        rows += _bar(b, k * H, 99.0, 99.8, 98.2, 99.0)
    exp = [r for r in rows if r["kind"] == "EXPIRED"]
    assert len(exp) == 1 and exp[0]["meta"]["limit_price"] == 102.0
    assert exp[0]["meta"]["ladder_group"] == "TEST#0#LEVEL_REJECTED"


def test_ladder_foreign_group_dup_guard():
    b = _broker()
    _ladder_group(b)
    _bar(b, 1 * H, 99.0, 100.5, 98.5, 99.5)                    # 首份成
    assert b.position_of("TEST") is not None
    b.submit_limit(_order(limit_price=99.5, ladder_group="OTHER#1#LEVEL_REJECTED",
                          meta={"direction": -1}))
    rows = _bar(b, 2 * H, 99.0, 100.0, 98.0, 99.0)
    skip = [r for r in rows if r["kind"] == "SKIP"]
    assert len(skip) == 1 and skip[0]["reason_code"] == "DUP_POSITION"


# ---------------------------------------------------------------------------
# C5 size_mult(quality tier × risk_frac;杠杆/容量不动)
# ---------------------------------------------------------------------------


def test_size_mult_scales_qty_not_leverage_or_cap():
    b1, b2 = _broker(), _broker()
    intent = OrderIntent(kind=OrderKind.OPEN, symbol="TEST", qty_frac=None,
                         reason="T|size", ref_price=100.0, ts=0)
    b1.submit(intent, decision_ts=0, direction=+1, invalid=96.0, meta={})
    b2.submit(intent, decision_ts=0, direction=+1, invalid=96.0,
              meta={"size_mult": 0.75})
    f1 = _bar(b1, 1 * H, 100.0, 100.5, 99.5, 100.0)
    f2 = _bar(b2, 1 * H, 100.0, 100.5, 99.5, 100.0)
    q1 = [r for r in f1 if r["kind"] == "OPEN"][0]["qty"]
    q2 = [r for r in f2 if r["kind"] == "OPEN"][0]["qty"]
    # 期望 = floor_step(size_mult × 未乘系数 qty):0.5597→0.55、0.4198→0.41
    unf = 0.0075 * 300.0 / abs(100.0 * 1.0002 - 96.0)
    assert q1 == pytest.approx(0.55) and q2 == pytest.approx(0.41)
    b3 = _broker()
    b3.submit(intent, decision_ts=0, direction=+1, invalid=96.0,
              meta={"size_mult": 0.5})
    f3 = _bar(b3, 1 * H, 100.0, 100.5, 99.5, 100.0)
    q3 = [r for r in f3 if r["kind"] == "OPEN"][0]["qty"]
    assert q3 == pytest.approx(0.27)                  # floor(0.5×0.5597)→0.27
    assert unf == pytest.approx(0.5597, abs=1e-3)


def test_size_mult_flows_into_triggered_limit_fills():
    bv = _broker()
    bv.submit_limit(_order(fill_model="virtual", direction=+1, invalid=96.0,
                           meta={"direction": 1, "size_mult": 0.5}))
    _bar(bv, 1 * H, 99.0, 100.5, 100.0, 99.5)     # low 恰=100 ⇒ buy touch 触发
    rows = _bar(bv, 2 * H, 100.0, 100.5, 99.5, 100.0)
    o = [r for r in rows if r["kind"] == "OPEN"]
    assert len(o) == 1
    assert o[0]["qty"] == pytest.approx(0.27)         # 同上 0.5× 全量取整


def test_arm_defaults_bitwise_no_new_fields():
    # 缺省 RestingLimitOrder:fill_model/is_stop/ladder_group 默认 ⇒ exchange
    od = _order()
    assert od.fill_model == "exchange" and od.is_stop is False
    assert od.ladder_group is None
