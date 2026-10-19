"""阶段 4 信号策略测试:GO 白名单入场、拒绝分支与 FLAT 解释。

合成事件/结构视图(同 test_hypothesis.py 惯例)。价格构造:
空入场 ref=100,invalid=101.1 ⇒ R=2.1;bar 收盘 99 近似 entry。
"""

from __future__ import annotations

from market_operator.events import E1, E2, E3, E4, E6, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.position import Position
from research.rounds.v1.strategy import (
    GO_MECHANISMS,
    OrderKind,
    Strategy,
    StrategyParams,
)
from market_operator.structure import Regime, Zone, ZoneRole, ZoneState

BAR = (0, 99.0, 99.5, 98.5, 99.0, 1.0)     # 决策 bar:收盘 99


def _ev(eid: str, direction: int, ref: float = 100.0,
        invalid: float = 101.1) -> MarketEvent:
    role = ZoneRole.SUPPORT if direction < 0 else ZoneRole.RESISTANCE
    return MarketEvent(id=eid, direction=direction, ts=0, known_index=9,
                       attrs={"ref": ref, "invalid": invalid,
                              "zone": Zone(lo=99.0, hi=100.0, role=role)})


def _view(*zones: Zone) -> StructureView:
    return StructureView(regime=Regime.TREND_DOWN, zones=tuple(zones), atr=1.0)


FAR_SUPPORT = Zone(lo=93.0, hi=94.0, role=ZoneRole.SUPPORT,
                   state=ZoneState.ACTIVE)     # target=94 ⇒ r=(99-94)/2.1≈2.38


def _pos(symbol: str = "BTCUSDT") -> Position:
    return Position(symbol=symbol, direction=-1, entry=99.0, invalid=101.1,
                    target_1=94.0, event_id=E1, opened_ts=0, opened_index=0,
                    initial_risk=2.1)


# ---------------------------------------------------------------------------
# GO 事件开仓
# ---------------------------------------------------------------------------


def test_go_e1_short_opens():
    s = Strategy()
    intents = s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(E1, -1)])
    assert len(intents) == 1
    it = intents[0]
    assert it.kind is OrderKind.OPEN
    assert it.symbol == "BTCUSDT"
    assert it.qty_frac is None                    # 由 sizing 决定
    assert it.ref_price == 99.0                   # 决策 bar 收盘近似
    assert "E1" in it.reason and "GO" in it.reason and "short" in it.reason
    assert "invalid=101.1" in it.reason and "target_1=94" in it.reason
    assert s.open_symbols == ("BTCUSDT",)


def test_go_e6_short_opens():
    s = Strategy()
    ev = _ev(E6, -1, ref=100.0, invalid=100.0)    # E6 invalid=被破 pivot(无 buf)
    intents = s.on_bar("ETHUSDT", BAR, _view(FAR_SUPPORT), [ev])
    assert len(intents) == 1
    assert "E6" in intents[0].reason
    assert s.open_symbols == ("ETHUSDT",)


# ---------------------------------------------------------------------------
# 拒绝分支(不交易是一等输出)
# ---------------------------------------------------------------------------


def test_long_event_rejected_with_dir_reason():
    s = Strategy()
    intents = s.on_bar("BTCUSDT", BAR, _view(), [_ev(E1, +1)])
    assert intents == []
    assert "dir|E1|long" in s.explain() or "dir|BTCUSDT E1|long" in s.explain()


def test_nogo_mechanisms_rejected_with_explain():
    for eid in (E2, E3, E4, "E5"):
        s = Strategy()
        intents = s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(eid, -1)])
        assert intents == [], eid
        assert f"{eid}|short" in s.explain()
        assert "nogo" in s.explain()


def test_no_structural_target_rejected():
    s = Strategy()
    intents = s.on_bar("BTCUSDT", BAR, _view(), [_ev(E1, -1)])
    assert intents == []
    assert "no_target" in s.explain()


def test_r_min_rejected():
    near = Zone(lo=97.5, hi=98.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE)           # target=98 ⇒ r=(99-98)/2.1≈0.48
    s = Strategy()
    intents = s.on_bar("BTCUSDT", BAR, _view(near), [_ev(E1, -1)])
    assert intents == []
    assert "r_min" in s.explain() and "r_mult=0.476" in s.explain()


def test_missing_invalid_rejected_no_hypo():
    s = Strategy()
    ev = MarketEvent(id=E1, direction=-1, ts=0, known_index=9, attrs={})
    assert s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [ev]) == []
    assert "no_hypo" in s.explain()


# ---------------------------------------------------------------------------
# 去重:同 symbol 已有持仓 / 同 bar 多事件;不同 symbol 独立
# ---------------------------------------------------------------------------


def test_dedup_while_position_open():
    s = Strategy()
    intents = s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT),
                       [_ev(E1, -1)], open_position_state=_pos())
    assert intents == []
    assert "dup" in s.explain()


def test_dedup_same_bar_two_go_events():
    s = Strategy()
    intents = s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT),
                       [_ev(E1, -1), _ev(E6, -1)])
    assert len(intents) == 1                      # 第一个开仓,第二个去重
    assert "dup" in s.explain()
    assert s.open_symbols == ("BTCUSDT",)


def test_dedup_next_bar_after_open():
    s = Strategy()
    s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(E1, -1)])
    intents = s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(E6, -1)],
                       open_position_state=_pos())  # 契约:成交后次 bar 起传入
    assert intents == []                          # 已有持仓 ⇒ 去重
    assert "dup" in s.explain()


def test_release_lock_when_flat_reported():
    s = Strategy()
    s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(E1, -1)])
    s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [],
             open_position_state=None)            # 调用方报告已平仓 ⇒ 解锁
    intents = s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(E6, -1)])
    assert len(intents) == 1


def test_symbols_independent():
    s = Strategy()
    i1 = s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(E1, -1)])
    i2 = s.on_bar("ETHUSDT", BAR, _view(FAR_SUPPORT), [_ev(E6, -1)])
    assert len(i1) == 1 and len(i2) == 1
    assert s.open_symbols == ("BTCUSDT", "ETHUSDT")


def test_risk_budget_cap():
    s = Strategy(StrategyParams(max_open_positions=1))
    s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), [_ev(E1, -1)])
    intents = s.on_bar("ETHUSDT", BAR, _view(FAR_SUPPORT), [_ev(E6, -1)])
    assert intents == []
    assert "risk_budget" in s.explain()


# ---------------------------------------------------------------------------
# FLAT / hold 解释分支
# ---------------------------------------------------------------------------


def test_flat_explain_no_event_no_position():
    s = Strategy()
    assert s.on_bar("BTCUSDT", BAR, _view(FAR_SUPPORT), []) == []
    assert s.explain().startswith("flat|BTCUSDT no new event")


def test_hold_explain_with_position():
    s = Strategy()
    assert s.on_bar("BTCUSDT", BAR, _view(), [],
                    open_position_state=_pos()) == []
    assert s.explain().startswith("hold|BTCUSDT")


def test_explain_before_any_bar():
    assert Strategy().explain() == "flat|no evaluation yet"


def test_go_whitelist_exact():
    assert (E1, -1) in GO_MECHANISMS and (E6, -1) in GO_MECHANISMS
    assert all(d < 0 for _e, d in GO_MECHANISMS)
    assert len(GO_MECHANISMS) == 2
