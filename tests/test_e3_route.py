"""E3 入场路由测试(2026-10-20 放开决议, research-log #16)。

约束:默认 V6Params 白名单仍为 v6 原集(不含 E3);canonical 配置放开时,
E3 应经 market 路由(拒绝获因后市价次开盘,F1/room 闸沿用)。
"""
import sys

sys.path.insert(0, "src")

from market_operator.strategy_v6 import V6Params, V6Router, ROUTE_E3, E3
from market_operator.events import MarketEvent
from market_operator.hypothesis import Hypothesis
from market_operator.structure import Regime


def _ev(direction):
    return MarketEvent(id=E3, direction=direction, ts=0, known_index=10,
                       attrs={"zone": None, "ref": 100.0,
                              "invalid": 101.0 if direction < 0 else 99.0})


def _hyp():
    return Hypothesis(event_id=E3, direction=-1, ts=0, known_index=10,
                      entry=100.0, invalid=101.0, target_1=90.0,
                      r_mult=10.0, regime=Regime.TREND_DOWN)


def test_default_whitelist_excludes_e3():
    assert (E3, -1) not in V6Params().whitelist
    assert (E3, +1) not in V6Params().whitelist


def test_e3_routes_to_market_when_whitelisted(monkeypatch):
    wl = frozenset({(E3, -1)})
    r = V6Router(V6Params(whitelist=wl))
    monkeypatch.setattr(V6Router, "_gates", lambda self, *a, **k: h if (h := _hyp()) else None)
    d = r.on_bar("BTCUSDT", (0, 99, 101, 98, 100, 1), 10, view=None, atr=1.0,
                 new_events=[_ev(-1)], position_open=False, entry_resting=False)
    assert len(d) == 1 and d[0].route == ROUTE_E3 and d[0].kind == "market"


def test_e3_nogo_when_not_whitelisted():
    r = V6Router(V6Params())
    d = r.on_bar("BTCUSDT", (0, 99, 101, 98, 100, 1), 10, view=None, atr=1.0,
                 new_events=[_ev(-1)], position_open=False, entry_resting=False)
    assert d == []
    assert any("nogo" in n for n in r._notes)
