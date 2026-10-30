"""P0/P1/P2 增量口径的单元钉死(A1/A2/A5/B1/B2/C1)。

这些函数都是**纯函数**(给定输入唯一输出),所以用最小构造物钉住语义,而不是
再跑一遍回放:回放只有成本,没有分辨率去区分"资金费符号错了"和"多算了 8h"。

A1 的关键不是"能不能算出数",而是 **符号**:常数口径下资金费恒为成本;真实
口径下空头在正费率时**收钱**。若符号错,净收益反而会变好,这种错误在净值曲线
上看起来像"策略变强了"。
"""
import sys

sys.path.insert(0, "src")

import pytest

from market_operator import capacity
from market_operator.broker import (
    Broker, ContractSpec, OrderIntent, OrderKind, _normalize_funding_table,
)
from market_operator.events import MarketEvent
from market_operator.hypothesis import Hypothesis
from market_operator.replay import annotate_excursions, counterfactual_summary
from market_operator.routing import LEVEL_REJECTED, MarketRouter, RouterParams
from market_operator.structure import Regime

T0 = 1_700_000_000_000
H = 3_600_000


def _specs(*symbols):
    return {s: ContractSpec(symbol=s, size_min=0.001, size_step=0.001,
                            size_place=3, price_place=2, min_trade_usdt=5.0)
            for s in symbols}


def _broker(**kwargs):
    return Broker(equity=300.0, risk_frac=0.0075, leverage_cap=20.0,
                  taker_fee=0.0006, slippage_per_side=0.0002,
                  funding_per_8h=0.0001,
                  contracts=_specs("BTCUSDT@2h", "ETHUSDT@2h"),
                  **kwargs)


def _open(broker, symbol="BTCUSDT@2h", price=100.0, direction=1):
    invalid = price - 5.0 if direction > 0 else price + 5.0
    target = price + 10.0 if direction > 0 else price - 10.0
    broker.submit(OrderIntent(kind=OrderKind.OPEN, symbol=symbol, qty_frac=None,
                              reason="LEVEL_REJECTED|valid", ref_price=price,
                              ts=T0),
                  T0, direction=direction, invalid=invalid,
                  event_id="LEVEL_REJECTED", target_1=target)
    row = broker.on_bar_open(symbol, T0 + H, price)[0]
    assert row["kind"] == "OPEN", row
    return broker.positions[symbol]


# ---------------------------------------------------------------------------
# A1 资金费:真实结算表
# ---------------------------------------------------------------------------

def test_normalize_funding_table_sorts_and_dedups_nothing():
    table = _normalize_funding_table({"BTCUSDT": [(T0 + 8 * H, 0.001),
                                                 (T0, -0.002)]})
    times, rates = table["BTCUSDT"]
    assert times == (T0, T0 + 8 * H)
    assert rates == (-0.002, 0.001)
    assert _normalize_funding_table(None) is None
    assert _normalize_funding_table({}) is None


def test_funding_signed_and_only_settlement_points_count():
    """多头 × 正费率 = 付出;负费率 = 收取(空头相反)。"""
    table = {"BTCUSDT": [(T0 + 4 * H, 0.001), (T0 + 8 * H, -0.002)]}
    b = _broker(funding_rates=table)
    pos = _open(b)
    notional = pos.qty * 100.0

    b._accrue_funding(pos, 100.0, T0 + 5 * H)      # 只跨过 +0.001 那一点
    assert pos.funding_acc == pytest.approx(notional * 0.001)
    assert pos.last_funding_ts == T0 + 5 * H

    b._accrue_funding(pos, 100.0, T0 + 9 * H)      # 再跨过 -0.002
    assert pos.funding_acc == pytest.approx(notional * (0.001 - 0.002))

    # 无结算点:累计不动,但游标必须推进(否则下一次会重复扫描)
    before = pos.funding_acc
    b._accrue_funding(pos, 100.0, T0 + 12 * H)
    assert pos.funding_acc == pytest.approx(before)
    assert pos.last_funding_ts == T0 + 12 * H

    # 同一位点重复计提 = 无操作(dt<=0)
    b._accrue_funding(pos, 100.0, T0 + 9 * H)
    assert pos.funding_acc == pytest.approx(notional * (0.001 - 0.002))


def test_funding_short_receives_under_positive_rate():
    b = _broker(funding_rates={"BTCUSDT": [(T0 + 4 * H, 0.001)]})
    pos = _open(b, direction=-1)
    b._accrue_funding(pos, 100.0, T0 + 5 * H)
    assert pos.funding_acc < 0.0           # 空头收费方向相反 ⇒ 累计为负 = 收入


def test_funding_falls_back_to_const_when_symbol_missing():
    """表里没有该标的 ⇒ 常数近似(不静默把口径改成 0)。"""
    b = _broker(funding_rates={"ETHUSDT": [(T0 + 4 * H, 0.001)]})
    pos = _open(b)                          # BTCUSDT 不在表里
    b._accrue_funding(pos, 100.0, T0 + 3 * H)
    expected = pos.qty * 100.0 * b.funding_8h * (2 * H / (8 * H))
    assert pos.funding_acc == pytest.approx(expected)


# ---------------------------------------------------------------------------
# A2 右尾量度:MFE / MAE / hold_bars / 分批腿数
# ---------------------------------------------------------------------------

class _Frame:
    """执行细 bar 的最小替身:``exec_bars[ts]`` 的 [2]/[3] 是 high/low。"""

    tf_hours = 2.0

    def __init__(self, bars):
        self.exec_bars = bars
        self.exec_labels = sorted(bars)


def _trade(**kw):
    t = {"campaign": "BTCUSDT@2h", "symbol": "BTCUSDT@2h", "direction": 1,
         "entry": 100.0, "qty": 1.0, "risk_usdt": 10.0, "open_ts": T0,
         "exit_ts": T0 + 4 * H, "hold_hours": 4.0, "r_multiple": 0.3,
         "reasons_reduce": ["LOCK"], "closed": True, "in_segment_open": True,
         "gross_pnl": 3.0, "fees": 0.4, "funding": 0.1}
    t.update(kw)
    return t


def test_excursions_use_high_low_and_respect_the_identity():
    frame = _Frame({T0 + H: (0, 0, 105.0, 99.0, 104.0, 1),
                    T0 + 2 * H: (0, 0, 103.0, 98.0, 102.0, 1),
                    T0 + 5 * H: (0, 0, 120.0, 90.0, 118.0, 1)})   # 平仓之后
    trade = _trade()
    annotate_excursions([trade], {"BTCUSDT@2h": frame})
    assert trade["mfe_r"] == pytest.approx(0.5)      # (105-100)·1/10
    assert trade["mae_r"] == pytest.approx(-0.2)     # (98-100)·1/10
    assert trade["excursion_bars"] == 2              # 平仓 bar 之后不计
    assert trade["hold_bars"] == pytest.approx(2.0)  # 4h / 2h 周期
    assert trade["n_partial_legs"] == 1
    assert trade["mfe_r"] >= trade["r_multiple"]


def test_excursions_skip_unknown_frame_and_are_idempotent():
    trade = _trade()
    annotate_excursions([trade], {})
    assert "mfe_r" not in trade                       # 无帧 ⇒ 不写半真半假的字段
    frame = _Frame({T0 + H: (0, 0, 101.0, 99.0, 100.5, 1)})
    annotate_excursions([trade], {"BTCUSDT@2h": frame})
    first = dict(trade)
    annotate_excursions([trade], {"BTCUSDT@2h": frame})
    assert trade == first


# ---------------------------------------------------------------------------
# A5 成本弹性:net(m) = gross − m·cost
# ---------------------------------------------------------------------------

def test_counterfactual_is_pure_cost_elasticity():
    trades = [_trade(gross_pnl=10.0, fees=1.0, funding=0.5),
              _trade(gross_pnl=-4.0, fees=1.0, funding=0.5)]
    out = counterfactual_summary(trades, multipliers=(1.0, 2.0))
    assert out["gross"] == pytest.approx(6.0)
    assert out["cost"] == pytest.approx(3.0)
    assert out["by_multiplier"]["1x"]["net"] == pytest.approx(3.0)
    assert out["by_multiplier"]["2x"]["net"] == pytest.approx(0.0)
    # 同一组交易的风险分母不变 ⇒ E[R] 只随成本平移
    assert out["by_multiplier"]["1x"]["e_r"] == pytest.approx(3.0 / 20.0)


# ---------------------------------------------------------------------------
# B1 标的级风险池 / B2 慢周期预留
# ---------------------------------------------------------------------------

class _Pos:
    def __init__(self, qty, direction, entry, invalid):
        self.qty, self.direction = qty, direction
        self.entry, self.invalid = entry, invalid


def test_symbol_pool_merges_timeframes_of_one_symbol():
    positions = {"BTCUSDT@2h": _Pos(1.0, 1, 100.0, 95.0),
                 "BTCUSDT@12h": _Pos(2.0, 1, 100.0, 95.0),
                 "ETHUSDT@2h": _Pos(1.0, 1, 100.0, 95.0)}
    marks = {s: 100.0 for s in positions}
    whole = capacity.outstanding_exposure(positions, marks).stop_loss
    btc = capacity.outstanding_exposure(positions, marks,
                                        scope="sym:BTCUSDT").stop_loss
    tf_12h = capacity.outstanding_exposure(positions, marks,
                                           timeframe="12h").stop_loss
    assert whole == pytest.approx(5.0 * 4)      # 4 手 × 5 点
    assert btc == pytest.approx(5.0 * 3)        # 两个 BTC 周期合并
    assert tf_12h == pytest.approx(5.0 * 2)


def test_symbol_root_strips_campaign_suffix():
    assert capacity.symbol_root("BTCUSDT@2h") == "BTCUSDT"
    assert capacity.symbol_root("BTCUSDT") == "BTCUSDT"
    assert capacity.symbol_root(None) is None


def test_reserve_only_bites_fast_timeframes():
    positions = {"BTCUSDT@2h": _Pos(1.0, 1, 100.0, 95.0)}
    marks = {"BTCUSDT@2h": 100.0}
    pools = [("portfolio", 0.10, None)]
    free, name, _ = capacity.budget_headroom(
        pools, 1000.0, positions, marks, taker_fee=0.0, slippage=0.0,
        include_costs=False, reserve_by_tf={"12h": 0.02, "1d": 0.02},
        candidate_timeframe="2h")
    assert free == pytest.approx(55.0)            # 100 − 5 已用 − 40 预留
    assert name == "portfolio+reserve"
    # 慢周期自己入场时不吃别人的预留(但自己那份也不扣)
    slow, _, _ = capacity.budget_headroom(
        pools, 1000.0, positions, marks, taker_fee=0.0, slippage=0.0,
        include_costs=False, reserve_by_tf={"12h": 0.02, "1d": 0.02},
        candidate_timeframe="12h")
    assert slow == pytest.approx(95.0)
    # 预留已被同周期仓位用满 ⇒ 不再重复扣
    used_slow = {"BTCUSDT@12h": _Pos(4.0, 1, 100.0, 95.0)}
    slow2, _, _ = capacity.budget_headroom(
        pools, 1000.0, used_slow, {"BTCUSDT@12h": 100.0},
        taker_fee=0.0, slippage=0.0, include_costs=False,
        reserve_by_tf={"12h": 0.02}, candidate_timeframe="2h")
    assert slow2 == pytest.approx(100.0 - 20.0)   # 20 = 4 手 × 5 点,预留用满


# ---------------------------------------------------------------------------
# C1 目标 R 上限(只裁目标,不动止损)
# ---------------------------------------------------------------------------

class _View:
    regime = Regime.RANGE


class _Zone:
    hi = 101.0
    lo = 99.0
    box_opposite = 115.0
    box_quality = 0.9
    touches = 4
    box_rotations = 4
    box_width_atr = 3.0


def _hyp_from(cap):
    ev = MarketEvent(id=LEVEL_REJECTED, direction=1, ts=0, known_index=10,
                     attrs={"zone": _Zone(), "ref": 100.0})
    router = MarketRouter(RouterParams(
        reject_target_mode="opposite", max_target_r=cap,
        whitelist=frozenset({(LEVEL_REJECTED, 1)})))
    return router._gates("BTCUSDT@2h", ev, _View(), 100.0, 1.0, "E3"), router


def test_target_r_cap_clamps_target_and_keeps_stop():
    hyp, plain = _hyp_from(None)
    assert hyp.target_1 == pytest.approx(115.0)
    assert hyp.r_mult == pytest.approx(7.5)       # (115−100)/(100−98)
    assert plain.counters.get("target_r_clamped", 0) == 0

    capped, router = _hyp_from(4.0)
    assert capped.r_mult == pytest.approx(4.0)
    assert capped.target_1 == pytest.approx(108.0)   # 100 + 4·2
    assert capped.invalid == pytest.approx(hyp.invalid)   # 止损不动
    assert router.counters["target_r_clamped"] == 1


def test_target_within_cap_is_left_alone():
    hyp, router = _hyp_from(10.0)
    assert hyp.target_1 == pytest.approx(115.0)
    assert router.counters.get("target_r_clamped", 0) == 0


# ---------------------------------------------------------------------------
# A11 结构性撤单:认知收盘时标必须为 ts + tf_hours * MS_1H (> 同区间 mic armed_at_ts)
# ---------------------------------------------------------------------------

def test_cancel_structurally_dead_entries_uses_cognition_close_timestamp():
    from types import SimpleNamespace
    from market_operator.broker import RestingLimitOrder
    from market_operator.campaign import _cancel_structurally_dead_entries
    from market_operator.replay import MS_1H

    b = _broker()
    key = "BTCUSDT@12h"
    cog_open_ts = T0
    # 模拟 12h 认知区间 [T0, T0+12h) 内首根或中间 2h mic bar (如 T0 或 T0+8h) 武装的入场挂单
    for armed_ts in (cog_open_ts, cog_open_ts + 8 * H):
        b.submit_limit(RestingLimitOrder(
            direction=1, limit_price=99.5, armed_at_ts=armed_ts,
            expiry_ts=armed_ts + 12 * H, fill_after_ts=armed_ts,
            symbol=key, role="entry", invalid=97.0, target_1=110.0,
            event_id=LEVEL_REJECTED, reason="test_limit",
            meta={"direction": 1, "zone_lo": 99.0, "zone_hi": 100.0},
        ))
        st = SimpleNamespace(
            frame=SimpleNamespace(tf_hours=12),
            pos_mgr=SimpleNamespace(p=SimpleNamespace(k_bm=0.1)),
        )
        counters: dict = {}
        # 12h 认知 bar 收盘价 98.0 < zone_lo(99.0) - 0.1*ATR(1.0) = 98.9 ⇒ 触发结构性撤单
        bar = (cog_open_ts, 100.0, 101.0, 97.5, 98.0, 10.0)
        _cancel_structurally_dead_entries(st, b, key, bar, atr=1.0, counters=counters)

        assert counters.get("struct_canceled") == 1
        assert not b.resting.get(key)
        last_row = b.ledger[-1]
        assert last_row["kind"] == "CANCEL"
        assert last_row["reason_code"] == "STRUCTURE_INVALID"
        assert last_row["ts"] == cog_open_ts + 12 * MS_1H
        assert last_row["ts"] > armed_ts

