"""Real-time protection clock vs arming evidence (clean production chain)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_operator.broker import OrderKind
from market_operator.events import LEVEL_REJECTED
from market_operator.position import PositionManager
from market_operator.structure import Params
from test_live_runtime import _cfg_min, _FakeClient, _FakeTG, _pos, _runner, NOW


def _pm(frac):
    pm = PositionManager(Params(), reject_opp_lock_frac=frac, time_stop=False)
    p = pm.open("BTCUSDT", 1, 100.0, 95.0, 110.0, LEVEL_REJECTED,
                NOW - 1000, 0)
    return pm, p


def test_quote_never_arms_close_mode():
    pm, p = _pm(-0.25)
    assert pm.on_price("BTCUSDT", ts=NOW, price=111.0) == []
    assert not p.armed and p.invalid == pytest.approx(95.0)


def test_armed_close_mode_ratchets_on_quote():
    pm, p = _pm(-0.25)
    p.armed = True
    pm.on_price("BTCUSDT", ts=NOW, price=115.0)
    assert p.invalid == pytest.approx(107.5)


def test_quote_giveback_exhausted_exits():
    pm, p = _pm(0.25)
    p.armed = True
    p._best = 120.0
    out = pm.on_price("BTCUSDT", ts=NOW, price=112.0)
    assert [i.kind for i in out] == [OrderKind.EXIT]


def _attach_15m(tmp_path, *, price_clock="15m"):
    step = 15 * 60 * 1000
    opened = NOW - 4 * step
    bar_ts = NOW - step
    bars = [(bar_ts, 101.0, 112.0, 100.5, 111.0, 15.0)]

    client = _FakeClient()
    client.history_candles = lambda *a, **k: list(bars)
    cfg = _cfg_min()
    cfg.setdefault("protection", {})["price_clock"] = price_clock
    r = _runner(tmp_path, client, _FakeTG(), cfg=cfg)
    pm = PositionManager(Params(), reject_opp_lock_frac=-0.25, time_stop=False)
    p = pm.open("BTCUSDT", 1, 100.0, 95.0, 110.0, LEVEL_REJECTED,
                opened, 0)
    cs = SimpleNamespace(
        symbol="BTCUSDT", tf_label="2h", obs_tf_seconds=7200,
        trigger_tf_seconds=7200, pm=pm, cog_bars=[], trig_bars=[],
        detector=SimpleNamespace(structure=SimpleNamespace(atr=lambda: 2.0)))
    r.campaigns["BTCUSDT"] = cs
    r.positions["BTCUSDT"] = {
        **_pos(direction=1, invalid=95.0, tf="2h"),
        "target_1": 110.0, "opened_ts": opened, "invalid0": 95.0,
        "price_source": "fills"}
    r._price_last["BTCUSDT"] = bar_ts - step
    return r, client, p, bar_ts + step


def test_15m_clock_arms_and_replaces_stop_without_2h(tmp_path):
    r, client, p, now = _attach_15m(tmp_path)
    r._advance_price_clock(now)
    assert p.armed
    assert p.invalid == pytest.approx(104.5)
    r._manage_stops(now)
    assert len(client.plan_placed) == 1
    assert client.plan_placed[0]["trigger_price"] == pytest.approx(104.5)


def test_quote_ratchets_after_15m_arm(tmp_path):
    r, client, p, now = _attach_15m(tmp_path)
    r._advance_price_clock(now)
    assert p.armed and p.invalid == pytest.approx(104.5)
    client.tickers = lambda: [{
        "symbol": "BTCUSDT", "lastPr": "115", "markPrice": "115",
        "ts": str(now + 1000)}]
    r._poll_position_prices(now + 1000)
    assert p.invalid == pytest.approx(107.5)


def test_bar_clock_skips_15m_path(tmp_path):
    r, _, p, now = _attach_15m(tmp_path, price_clock="bar")
    r._advance_price_clock(now)
    assert not p.armed and p.invalid == pytest.approx(95.0)


def test_invalid_price_clock_fails_closed(tmp_path):
    cfg = _cfg_min()
    cfg.setdefault("protection", {})["price_clock"] = "tick"
    with pytest.raises(RuntimeError, match="price_clock"):
        _runner(tmp_path, _FakeClient(), _FakeTG(), cfg=cfg)
