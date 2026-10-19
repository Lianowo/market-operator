"""§8 持仓管理六条规则的合成场景测试(含同 bar 优先级与边界)。

合成 bar:(ts, o, h, l, c, v)。空仓基准:entry=99, invalid=101, R=2,
target_1=94;n_time=3(合成, Params 直接构造,非改 config)。
"""

from __future__ import annotations

from market_operator.events import E4, E6, MarketEvent
from market_operator.position import PositionManager
from market_operator.strategy import OrderKind
from market_operator.structure import Params, Pivot, PivotKind

P = Params(k_bm=0.1, n_time=3)


def _mgr(allow_add: bool = False) -> PositionManager:
    return PositionManager(P, allow_add=allow_add)


def _short(mgr: PositionManager, invalid: float = 101.0,
           target: float = 94.0):
    return mgr.open("BTCUSDT", -1, entry=99.0, invalid=invalid,
                    target_1=target, event_id="E1", ts=0, index=0)


def _bar(ts: int, h: float, l: float, c: float, o: float = 99.0):
    return (ts, o, h, l, c, 1.0)


def _pivot_high(price: float, known: int = 1) -> Pivot:
    return Pivot(price=price, forming_index=known - 1, known_index=known,
                 kind=PivotKind.HIGH)


def _pivot_low(price: float, known: int = 1) -> Pivot:
    return Pivot(price=price, forming_index=known - 1, known_index=known,
                 kind=PivotKind.LOW)


def _e6(direction: int) -> MarketEvent:
    return MarketEvent(id=E6, direction=direction, ts=1, known_index=1,
                       attrs={"ref": 100.0, "invalid": 100.0,
                              "regime_before": "TREND_DOWN" if direction > 0
                              else "TREND_UP"})


ATR = 1.0


# ---------------------------------------------------------------------------
# §8.1 破位退出
# ---------------------------------------------------------------------------


def test_81_invalidation_close_exits_all():
    m = _mgr()
    _short(m)
    intents = m.on_bar("BTCUSDT", _bar(1, 102.5, 98.0, 102.0), ATR)
    assert len(intents) == 1
    it = intents[0]
    assert it.kind is OrderKind.EXIT and it.qty_frac == 1.0
    assert it.reason.startswith("§8.1|") and "invalid=101" in it.reason
    assert "BTCUSDT" not in m.positions


def test_81_close_near_but_not_beyond_survives():
    m = _mgr()
    _short(m)
    intents = m.on_bar("BTCUSDT", _bar(1, 101.5, 98.0, 100.9), ATR)
    assert intents == []                            # 影线刺穿不算,收盘口径
    assert "BTCUSDT" in m.positions


# ---------------------------------------------------------------------------
# §8.5 反向 E6 / 优先级 1>5
# ---------------------------------------------------------------------------


def test_85_reverse_e6_exits_all():
    m = _mgr()
    _short(m)
    intents = m.on_bar("BTCUSDT", _bar(1, 99.5, 98.0, 99.0), ATR,
                       new_events=[_e6(+1)])
    assert len(intents) == 1
    assert intents[0].kind is OrderKind.EXIT
    assert intents[0].reason.startswith("§8.5|E6 reverse CHoCH")


def test_85_same_direction_e6_ignored():
    m = _mgr()
    _short(m)
    intents = m.on_bar("BTCUSDT", _bar(1, 99.5, 98.0, 99.0), ATR,
                       new_events=[_e6(-1)])        # 同向 E6 不出场
    assert intents == []


def test_81_beats_85_same_bar():
    m = _mgr()
    _short(m)
    intents = m.on_bar("BTCUSDT", _bar(1, 102.5, 98.0, 102.0), ATR,
                       new_events=[_e6(+1)])
    assert len(intents) == 1
    assert intents[0].reason.startswith("§8.1|")    # 优先级 1>5


# ---------------------------------------------------------------------------
# §8.2 target_1:减仓一次 + 保本移动;/与 §8.1 同 bar 优先级
# ---------------------------------------------------------------------------


def test_82_target_hit_reduces_once_and_breakevens():
    m = _mgr()
    pos = _short(m)
    intents = m.on_bar("BTCUSDT", _bar(1, 99.5, 93.5, 98.0), ATR)
    assert len(intents) == 1
    it = intents[0]
    assert it.kind is OrderKind.REDUCE and it.qty_frac == 0.5
    assert it.reason.startswith("§8.2|target_1 hit reduce 50%")
    assert pos.size_frac == 0.5 and pos.reduced
    assert pos.invalid == 99.0                      # 保本:invalid 移至 entry
    # 再次触及:不重复减仓
    assert m.on_bar("BTCUSDT", _bar(2, 99.5, 93.0, 98.0), ATR) == []
    assert pos.size_frac == 0.5


def test_82_exit_qty_after_reduce_is_remaining():
    m = _mgr()
    _short(m)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 93.5, 98.0), ATR)      # REDUCE 0.5
    intents = m.on_bar("BTCUSDT", _bar(2, 102.5, 97.0, 102.0), ATR)  # §8.1
    assert intents[0].qty_frac == 0.5               # 退出剩余全部


def test_81_beats_82_same_bar():
    m = _mgr()
    _short(m)
    intents = m.on_bar("BTCUSDT", _bar(1, 102.5, 93.0, 102.0), ATR)
    assert len(intents) == 1
    assert intents[0].kind is OrderKind.EXIT        # 破位优先,不减仓


def test_82_breakeven_does_not_loosen_trailed_invalid():
    m = _mgr()
    pos = _short(m)                                  # invalid=101
    m.on_bar("BTCUSDT", _bar(1, 99.5, 98.0, 99.0), ATR,
             new_pivots=[_pivot_high(98.5)])        # §8.3 先收紧到 98.6
    assert pos.invalid == 98.6
    m.on_bar("BTCUSDT", _bar(2, 99.0, 93.5, 98.0), ATR)      # 触 target
    assert pos.invalid == 98.6                      # 保本 99 更松 ⇒ 不动


# ---------------------------------------------------------------------------
# §8.3 移动防线:收紧、单向性、方向匹配
# ---------------------------------------------------------------------------


def test_83_trail_tightens_and_never_loosens():
    m = _mgr()
    pos = _short(m, target=None)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 98.0, 99.0), ATR,
             new_pivots=[_pivot_high(100.0)])
    assert pos.invalid == 100.1                     # 100 + 0.1*atr
    assert any("§8.3|trail new pivot high=100" in s for s in pos.log)
    m.on_bar("BTCUSDT", _bar(2, 99.5, 98.0, 99.0), ATR,
             new_pivots=[_pivot_high(100.5)])       # 更松 ⇒ 不动
    assert pos.invalid == 100.1
    m.on_bar("BTCUSDT", _bar(3, 99.5, 98.0, 99.0), ATR,
             new_pivots=[_pivot_high(99.5)])        # 更紧 ⇒ 99.6
    assert pos.invalid == 99.6


def test_83_ignores_favorable_kind_pivots():
    m = _mgr()
    pos = _short(m, target=None)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 98.0, 99.0), ATR,
             new_pivots=[_pivot_low(96.0)])         # 空仓只跟 pivot high
    assert pos.invalid == 101.0
    assert pos.log == []


def test_83_long_mirror():
    m = _mgr()
    pos = m.open("ETHUSDT", +1, entry=100.0, invalid=98.0, target_1=None,
                 event_id="E6", ts=0, index=0)
    m.on_bar("ETHUSDT", _bar(1, 101.0, 99.0, 100.5), ATR,
             new_pivots=[_pivot_low(99.0)])
    assert pos.invalid == 98.9                      # 99 - 0.1*atr(收紧)
    m.on_bar("ETHUSDT", _bar(2, 101.0, 98.5, 100.0), ATR,
             new_pivots=[_pivot_low(98.5)])         # 98.4 更松 ⇒ 不动
    assert pos.invalid == 98.9


# ---------------------------------------------------------------------------
# §8.6 时间止损:边界(恰好 n_time、MFE 恰好 0.5R 两侧)
# ---------------------------------------------------------------------------


def test_86_no_exit_before_n_time_even_if_mfe_low():
    m = _mgr()
    _short(m, target=None)
    assert m.on_bar("BTCUSDT", _bar(1, 99.5, 98.9, 99.0), ATR) == []
    assert m.on_bar("BTCUSDT", _bar(2, 99.5, 98.9, 99.0), ATR) == []  # held=2<3


def test_86_exit_at_n_time_when_mfe_below_half_r():
    m = _mgr()
    pos = _short(m, target=None)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 98.9, 99.0), ATR)
    m.on_bar("BTCUSDT", _bar(2, 99.5, 98.9, 99.0), ATR)
    # bar3:low=98.2 ⇒ mfe=(99-98.2)/2=0.4R < 0.5R ⇒ 时间止损
    intents = m.on_bar("BTCUSDT", _bar(3, 99.5, 98.2, 99.0), ATR)
    assert len(intents) == 1
    assert intents[0].reason.startswith("§8.6|time stop")
    assert "BTCUSDT" not in m.positions


def test_86_mfe_exactly_half_r_survives():
    m = _mgr()
    pos = _short(m, target=None)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 98.9, 99.0), ATR)
    m.on_bar("BTCUSDT", _bar(2, 99.5, 98.9, 99.0), ATR)
    # low=98.0 ⇒ mfe=(99-98)/2=0.5R,恰好 ⇒ 不出场(严格 <)
    assert m.on_bar("BTCUSDT", _bar(3, 99.5, 98.0, 99.0), ATR) == []
    # 窗口过后(≥0.5R 曾经达到),MFE 单调 ⇒ 第 4 根也不出场
    assert m.on_bar("BTCUSDT", _bar(4, 99.4, 98.9, 99.0), ATR) == []


def test_86_uses_bar_extremes_not_close():
    m = _mgr()
    _short(m, target=None)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 98.9, 99.0), ATR)
    m.on_bar("BTCUSDT", _bar(2, 99.5, 98.9, 99.0), ATR)
    # close 停在 99 但影线到 97.5 ⇒ mfe=0.75R ≥ 0.5 ⇒ 生存
    assert m.on_bar("BTCUSDT", _bar(3, 99.5, 97.5, 99.0), ATR) == []


# ---------------------------------------------------------------------------
# §8.4 加仓(v1 关闭;函数逻辑独立验证)
# ---------------------------------------------------------------------------


def _e4_short(invalid: float) -> MarketEvent:
    return MarketEvent(id=E4, direction=-1, ts=1, known_index=1,
                       attrs={"leg_start": 103.0, "pb_price": 97.0,
                              "depth": 6.0, "ref": 98.5, "invalid": invalid,
                              "regime": "TREND_DOWN"})


def _trailed_short(m: PositionManager):
    """浮盈 ≥1R 且 invalid 已收紧至 97(锁盈)的空仓。"""
    pos = m.open("BTCUSDT", -1, entry=99.0, invalid=101.0, target_1=None,
                 event_id="E1", ts=0, index=0)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 96.5, 97.0), ATR)   # mfe=(99-96.5)/2=1.25R
    pos.invalid = 97.0                                    # 模拟 §8.3 已收紧
    return pos


def test_84_add_accepted_when_risk_neutral():
    m = _mgr(allow_add=True)
    pos = _trailed_short(m)
    # 加 1.0 @ 95,均价 97;E4 invalid=96 ⇒ 合并 worst=-1*(97-96)*2=-2 ≤ 前-2
    intents = m.on_bar("BTCUSDT", _bar(2, 97.5, 94.5, 95.0), ATR,
                       new_events=[_e4_short(96.0)])
    assert len(intents) == 1
    it = intents[0]
    assert it.kind is OrderKind.ADD and it.qty_frac == 1.0
    assert it.reason.startswith("§8.4|E4 add")
    assert pos.added and pos.size_frac == 2.0
    assert pos.entry == 97.0 and pos.invalid == 96.0


def test_84_add_rejected_when_worst_case_degrades():
    m = _mgr(allow_add=True)
    pos = _trailed_short(m)
    # E4 invalid=98 ⇒ 合并防线仍 97(只收紧),worst=0 > 前-2 ⇒ 拒绝
    intents = m.on_bar("BTCUSDT", _bar(2, 97.5, 94.5, 95.0), ATR,
                       new_events=[_e4_short(98.0)])
    assert intents == []
    assert not pos.added and pos.size_frac == 1.0 and pos.entry == 99.0
    assert any("risk-neutral violated" in s for s in m.last)


def test_84_add_rejected_when_mfe_below_1r():
    m = _mgr(allow_add=True)
    pos = m.open("BTCUSDT", -1, entry=99.0, invalid=101.0, target_1=None,
                 event_id="E1", ts=0, index=0)
    m.on_bar("BTCUSDT", _bar(1, 99.5, 98.5, 99.0), ATR)    # mfe=0.25R
    intents = m.on_bar("BTCUSDT", _bar(2, 99.5, 98.9, 99.0), ATR,
                       new_events=[_e4_short(98.0)])
    assert intents == []
    assert any("mfe=0.250R" in s for s in m.last)


def test_84_disabled_by_default_v1():
    m = _mgr()                                             # allow_add=False
    _trailed_short(m)
    intents = m.on_bar("BTCUSDT", _bar(2, 97.5, 94.5, 95.0), ATR,
                       new_events=[_e4_short(96.0)])
    assert intents == []                                   # 完全无加仓输出


def test_84_add_only_once():
    m = _mgr(allow_add=True)
    pos = _trailed_short(m)
    m.on_bar("BTCUSDT", _bar(2, 97.5, 94.5, 95.0), ATR,
             new_events=[_e4_short(96.0)])
    intents = m.on_bar("BTCUSDT", _bar(3, 97.5, 94.0, 95.5), ATR,
                       new_events=[_e4_short(95.0)])
    assert intents == [] and pos.added


# ---------------------------------------------------------------------------
# 记账可对账:OPEN 1.0 − REDUCE 0.5 − EXIT 0.5 = 0
# ---------------------------------------------------------------------------


def test_full_lifecycle_qty_sums_to_flat():
    m = _mgr()
    _short(m)
    r = m.on_bar("BTCUSDT", _bar(1, 99.5, 93.5, 98.0), ATR)  # REDUCE 0.5
    x = m.on_bar("BTCUSDT", _bar(2, 102.5, 97.0, 102.0), ATR)  # §8.1 EXIT
    assert r[0].qty_frac + x[0].qty_frac == 1.0
