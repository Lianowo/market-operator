"""主张身份/生命周期契约(语义测试,不是收益测试)。

四条被固化的语义:

1. 预算主体是"标的×方向"的市场判断,周期只是观察尺度 → 跨周期同向共用一份预算。
2. 同一标的不能同时持两个相反主张 → 后来者被拒,并写明原因。
3. 主张到达对侧 = 履约;没有新的迁移事实就必须平仓,不能默认续期。
4. 只有同向"外侧接受"(ACCEPT)才能把已完成的主张换签为迁移主张。
"""

from __future__ import annotations

import pytest

from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind
from market_operator.capacity import ADMISSION_FULL_COST
from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
from market_operator.position import PositionManager
from market_operator.structure import Params

H = 3_600_000
T0 = 1_700_000_000_000


def _spec() -> ContractSpec:
    return ContractSpec(symbol="X", size_min=0.01, size_step=0.01,
                        size_place=2, price_place=2, min_trade_usdt=5.0)


def _broker(**kw) -> Broker:
    cfg = dict(risk_frac=0.1, leverage_cap=20.0, taker_fee=0.0006,
               slippage_per_side=0.0002, funding_per_8h=0.0001,
               portfolio_stop_risk_frac=0.5,
               tf_stop_risk_frac_by_tf={"2h": 0.1, "4h": 0.1},
               capacity_mode=ADMISSION_FULL_COST)
    cfg.update(kw)
    return Broker(1000.0, contracts={"A@2h": _spec(), "A@4h": _spec()}, **cfg)


def _submit_short(b: Broker, key: str, ts: int, meta: dict | None = None) -> None:
    b.submit(OrderIntent(kind=OrderKind.OPEN, symbol=key, qty_frac=None,
                         reason="LEVEL_REJECTED|short", ref_price=100.0, ts=ts),
             decision_ts=ts, direction=-1, invalid=101.0, target_1=94.0,
             event_id=LEVEL_REJECTED,
             meta={"direction": -1, **(meta or {})})


def test_timeframes_share_one_claim_budget():
    b = _broker()
    _submit_short(b, "A@2h", T0)
    row = b.on_bar_open("A@2h", T0 + H, 100.0)[0]
    assert row["kind"] == "OPEN"
    used = row["meta"]["risk_usdt"]
    assert used == pytest.approx(0.1 * 1000, rel=0.05)

    headroom, pool, frac = b.claim_headroom("A@4h", -1, b.risk_capital(),
                                           moment="fill")
    assert pool == "claim:A:-1" and frac == pytest.approx(0.1)
    # 同一判断已经用掉这份预算 → 另一个周期的"再确认"没有新额度
    assert headroom < 0.05 * 1000
    assert b.qty_for_entry(b.risk_capital(), 100.0, 101.0, "A@4h",
                           direction=-1) < 1.0


def test_opposite_claim_is_refused_with_reason():
    b = _broker()
    _submit_short(b, "A@2h", T0)
    assert b.on_bar_open("A@2h", T0 + H, 100.0)[0]["kind"] == "OPEN"

    assert b.opposite_claim_open("A@4h", +1) == "A@2h"
    b.submit(OrderIntent(kind=OrderKind.OPEN, symbol="A@4h", qty_frac=None,
                         reason="BREAKOUT_ACCEPTED|long", ref_price=100.0,
                         ts=T0 + H),
             decision_ts=T0 + H, direction=+1, invalid=99.0, target_1=None,
             event_id=BREAKOUT_ACCEPTED, meta={"direction": 1})
    rows = b.on_bar_open("A@4h", T0 + 2 * H, 100.0)
    assert rows[0]["kind"] == "SKIP"
    assert rows[0]["reason_code"] == "OPPOSITE_CLAIM"
    assert b.positions.keys() == {"A@2h"}


def _pm() -> PositionManager:
    return PositionManager(Params(), allow_add=False, time_stop=False,
                           reject_opp_lock_frac=-0.25)


def _short_position(pm: PositionManager, target=94.0, invalid=101.0):
    return pm.open("A@2h", -1, 100.0, invalid, target, LEVEL_REJECTED,
                   T0, 0)


def test_claim_completion_is_recorded_but_does_not_rebase_risk():
    """到达对侧 = 完成(事实);但风险重定基必须等"接受"(收盘越过)。

    归因实验:触价即重定基会把趋势右尾剪掉(净 −19.4k、回撤 +12pp),所以
    完成只记账:claim_completed_ts 被写入,防线保持出发时的结构止损。
    """
    pm = _pm()
    pos = _short_position(pm)          # entry 100, invalid 101, target 94
    bar = (T0 + H, 99.0, 99.5, 93.0, 95.0, 1.0)      # low 93 穿过 target,收盘 95 未接受
    intents = pm.on_bar("A@2h", bar, 1.0, (), ())
    assert intents == []
    assert pos.claim_completed_ts == T0 + H
    assert pos.armed is False                        # 触价不 arming
    assert pos.invalid == pytest.approx(101.0)       # 仍用出发时的结构止损


def test_acceptance_beyond_target_arms_the_giveback_budget():
    """收盘越过目标 = 接受 ⇒ 才把风险基准换成回吐预算。"""
    pm = _pm()
    pos = _short_position(pm)
    bar = (T0 + H, 96.0, 96.5, 92.0, 92.5, 1.0)      # 收盘 92.5 < 94
    intents = pm.on_bar("A@2h", bar, 1.0, (), ())
    assert intents == []
    assert pos.claim_completed_ts == T0 + H
    assert pos.armed is True
    assert pos.invalid < pos.entry


def test_completed_claim_with_same_direction_acceptance_is_re_underwritten():
    pm = _pm()
    pos = _short_position(pm)
    acceptance = MarketEvent(id=BREAKOUT_ACCEPTED, direction=-1,
                             ts=T0 + H, known_index=1)
    bar = (T0 + H, 96.0, 96.5, 92.0, 92.5, 1.0)      # 收盘 92.5 < 94 → 外侧接受
    intents = pm.on_bar("A@2h", bar, 1.0, (), (acceptance,))
    assert intents == []
    assert pos.claim_migrated_ts == T0 + H
    assert pos.claim_completed_ts is None
    assert pos.target_1 is None            # 换签为迁移主张:不再是"回箱内"目标


def test_legacy_target_mode_is_not_silently_rewritten():
    """frac=0(传统 §8.2 减仓)不套用主张锁利 arming:两种合同不混用。"""
    pm = PositionManager(Params(), allow_add=False, time_stop=False,
                         reject_opp_lock_frac=0.0)
    pos = _short_position(pm)
    bar = (T0 + H, 99.0, 99.5, 93.0, 95.0, 1.0)
    pm.on_bar("A@2h", bar, 1.0, (), ())
    assert pos.armed is False
    assert pos.claim_completed_ts == T0 + H     # 事实仍被记录


def test_accept_entry_without_target_is_unaffected():
    pm = _pm()
    pos = pm.open("A@2h", -1, 100.0, 101.0, None, BREAKOUT_ACCEPTED, T0, 0)
    bar = (T0 + H, 99.0, 99.5, 90.0, 95.0, 1.0)
    intents = pm.on_bar("A@2h", bar, 1.0, (), ())
    assert intents == []
    assert pos.claim_completed_ts is None
