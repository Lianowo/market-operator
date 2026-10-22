"""v6 路由策略单元测试(规范 turtle 唯一语义,docs/v6-protocol.md §2-S2 臂 B)。

覆盖:turtle 失效(初始防线 = close ∓ 2.0·atr,无目标 target_1=None,
r_mult=None,不做 room/r_min 门);F1 闸;双向 episode 关联(WEAK-STRUCTURE_BROKEN 登记
⇒ 同 episode 同向 FLIP_RETESTED 前缘限价,方向错位不匹配,消费一次)。
历史 A/B 对照:臂 A clamp2R(S1 区缘失效/room 门/2R 钳制/未知臂拒)与
完整双臂测试套件保留于 research/rounds/v6/tests/test_v6_strategy.py,对
归档双臂实现(research/rounds/v6/routing.py)运行。
"""

from __future__ import annotations

from market_operator.events import STRUCTURE_BROKEN, FLIP_RETESTED, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.routing import (
    ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT,
    RouterParams,
    MarketRouter,
)
from market_operator.structure import Regime, Zone, ZoneRole, ZoneState

T0 = 1_704_067_200_000
H = 3_600_000
ATR = 2.0
K_BM = 0.1


def _view(*zones):
    return StructureView(regime=Regime.RANGE, zones=tuple(zones), atr=ATR)


def _e6(direction, hour=5, ref=99.5, bar_close=97.0):
    inv = ref + 0.2 if direction < 0 else ref - 0.2
    return MarketEvent(id=STRUCTURE_BROKEN, direction=direction, ts=T0 + hour * H,
                       known_index=hour,
                       attrs={"ref": ref, "invalid": inv, "pivot_forming": 0,
                              "regime_before": (Regime.TREND_UP.value
                                                if direction < 0 else
                                                Regime.TREND_DOWN.value)})


def _e7(direction, hour=6, lo=99.5, hi=99.8, invalid=None):
    # 检测器冻结口径:FLIP_RETESTED invalid = 翻转区缘 ± k_bm·atr
    if invalid is None:
        invalid = (hi + K_BM * ATR) if direction < 0 else (lo - K_BM * ATR)
    return MarketEvent(id=FLIP_RETESTED, direction=direction, ts=T0 + hour * H,
                       known_index=hour,
                       attrs={"zone": Zone(lo=lo, hi=hi, role=(
                           ZoneRole.RESISTANCE if direction < 0 else
                           ZoneRole.SUPPORT), state=ZoneState.FLIPPED),
                           "ref": lo if direction < 0 else hi,
                           "invalid": invalid,
                           "from_role": (ZoneRole.SUPPORT.value
                                         if direction < 0 else
                                         ZoneRole.RESISTANCE.value),
                           "atr": ATR})


def _bar(close, hour=5, hi_pad=3.0, lo_pad=3.0, o=None):
    o = close if o is None else o
    return (T0 + hour * H, o, close + hi_pad, close - lo_pad, close, 1.0)


def _router(**over):
    return MarketRouter(RouterParams(**over))


# ---------------------------------------------------------------------------
# turtle(无目标,2·atr 防线,不做 room/r_min)
# ---------------------------------------------------------------------------


def test_turtle_stop_and_no_target():
    r = _router()
    zone = Zone(lo=99.5, hi=100.4, role=ZoneRole.SUPPORT,
                state=ZoneState.BROKEN)
    dec = r.on_bar("X", _bar(97.0), 5, _view(zone), ATR,
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    hyp = dec[0].hyp
    assert hyp.invalid == 97.0 + 2.0 * ATR        # close − d·2·atr(short:上方)
    assert hyp.target_1 is None and hyp.r_mult is None


def test_no_room_gate():
    """即使完全无结构目标也交易(turtle 无目标语义,如实声明)。"""
    r = _router()
    dec = r.on_bar("X", _bar(97.0), 5, _view(), ATR,  # 空视图
                   [_e6(-1, ref=99.5)], position_open=False,
                   entry_resting=False)
    assert len(dec) == 1


def test_long_mirror():
    r = _router()
    dec = r.on_bar("X", _bar(103.0), 5, _view(), ATR,
                   [_e6(+1, ref=100.5)], position_open=False,
                   entry_resting=False)
    assert dec[0].hyp.invalid == 103.0 - 2.0 * ATR


# ---------------------------------------------------------------------------
# 双向路由与 episode 关联
# ---------------------------------------------------------------------------


def test_weak_episode_direction_isolated():
    """weak-STRUCTURE_BROKEN|short 登记不消费 FLIP_RETESTED|long(方向错位 episode 不匹配)。"""
    r = _router()
    r.on_bar("X", _bar(97.0, hi_pad=0.5, lo_pad=0.5), 5, _view(), ATR,
             [_e6(-1, ref=99.5)], position_open=False, entry_resting=False)
    assert r.pending_of("X") is not None         # 弱破位已登记(margin 0.25)
    dec = r.on_bar("X", _bar(99.0, hour=6), 6, _view(), ATR,
                   [_e7(+1, hour=6)], position_open=False, entry_resting=False)
    assert dec == []
    assert r.counters.get("e7_no_episode") == 1
    assert r.pending_of("X") is not None         # 登记保留


def test_weak_episode_consumed_once_by_matching_e7_long():
    r = _router()
    zone = Zone(lo=99.6, hi=100.5, role=ZoneRole.RESISTANCE,
                state=ZoneState.BROKEN)
    tgt = Zone(lo=111.0, hi=112.0, role=ZoneRole.RESISTANCE,
               state=ZoneState.ACTIVE)
    r.on_bar("X", _bar(103.0, hi_pad=0.2, lo_pad=0.2), 5, _view(zone, tgt),
             ATR, [_e6(+1, ref=100.5)], position_open=False,
             entry_resting=False)
    assert r.pending_of("X") is not None         # impulse 0.2 ⇒ WEAK 登记
    dec = r.on_bar("X", _bar(102.0, hour=6), 6, _view(zone, tgt), ATR,
                   [_e7(+1, hour=6, lo=100.2, hi=100.5)], position_open=False,
                   entry_resting=False)
    assert len(dec) == 1 and dec[0].kind == "limit"
    assert dec[0].route == ROUTE_STRUCTURE_BROKEN_WEAK_LIMIT
    assert dec[0].limit_price == 100.5           # long 前缘 = zone.hi
    assert r.pending_of("X") is None             # 消费一次
