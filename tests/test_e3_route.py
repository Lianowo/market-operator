"""LEVEL_REJECTED 入场路由测试(2026-10-20 放开决议, research-log #16)。

约束:默认 RouterParams 白名单仍为 v6 原集(不含 LEVEL_REJECTED);canonical 配置放开时,
LEVEL_REJECTED 应经 market 路由(拒绝获因后市价次开盘,F1/room 闸沿用)。
"""
import sys

sys.path.insert(0, "src")

from market_operator.routing import RouterParams, MarketRouter, ROUTE_LEVEL_REJECTED, LEVEL_REJECTED
from market_operator.events import MarketEvent
from market_operator.hypothesis import Hypothesis
from market_operator.structure import Regime


def _ev(direction):
    return MarketEvent(id=LEVEL_REJECTED, direction=direction, ts=0, known_index=10,
                       attrs={"zone": None, "ref": 100.0,
                              "invalid": 101.0 if direction < 0 else 99.0})


def _hyp():
    return Hypothesis(event_id=LEVEL_REJECTED, direction=-1, ts=0, known_index=10,
                      entry=100.0, invalid=101.0, target_1=90.0,
                      r_mult=10.0, regime=Regime.TREND_DOWN)


def test_default_whitelist_excludes_e3():
    assert (LEVEL_REJECTED, -1) not in RouterParams().whitelist
    assert (LEVEL_REJECTED, +1) not in RouterParams().whitelist


def test_e3_routes_to_market_when_whitelisted(monkeypatch):
    wl = frozenset({(LEVEL_REJECTED, -1)})
    r = MarketRouter(RouterParams(whitelist=wl))
    monkeypatch.setattr(MarketRouter, "_gates", lambda self, *a, **k: h if (h := _hyp()) else None)
    d = r.on_bar("BTCUSDT", (0, 99, 101, 98, 100, 1), 10, view=None, atr=1.0,
                 new_events=[_ev(-1)], position_open=False, entry_resting=False)
    assert len(d) == 1 and d[0].route == ROUTE_LEVEL_REJECTED and d[0].kind == "market"


def test_e3_nogo_when_not_whitelisted():
    r = MarketRouter(RouterParams())
    d = r.on_bar("BTCUSDT", (0, 99, 101, 98, 100, 1), 10, view=None, atr=1.0,
                 new_events=[_ev(-1)], position_open=False, entry_resting=False)
    assert d == []
    assert any("nogo" in n for n in r._notes)
