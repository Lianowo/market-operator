"""Real-time protection clock vs arming evidence (clean production chain)."""
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
import live_runner as lr


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


def test_initial_price_clock_ts_skips_entry_history():
    """Restart anchors at the last closed 15m bar, not at the entry bar."""
    step = 15 * 60 * 1000
    now = 1_800_000_000_000
    old_entry = now - 96 * step
    assert lr.initial_price_clock_ts(now, step, old_entry) == \
        (now // step) * step - step

    # A position opened inside the current bar must not look before its entry.
    entry = (now // step) * step
    assert lr.initial_price_clock_ts(now, step, entry) == entry


def test_price_clock_resyncs_after_long_gap(tmp_path):
    """A stalled process must not crawl the missed 15m history."""
    step = 15 * 60 * 1000
    r, _client, _p, now = _attach_15m(tmp_path)
    r._price_last["BTCUSDT"] = now - 20 * step
    r._advance_price_clock(now)
    assert r._price_last["BTCUSDT"] >= (now // step) * step - step


@pytest.mark.parametrize("age_ms", [30_001, -1])
def test_stale_or_future_quote_does_not_advance(tmp_path, monkeypatch, age_ms):
    r, client, p, now = _attach_15m(tmp_path)
    r._advance_price_clock(now)
    before = (p.invalid, p._best)
    monkeypatch.setattr(lr.time, "time", lambda: now / 1000)
    client.tickers = lambda: [{
        "symbol": "BTCUSDT", "lastPr": "115", "markPrice": "115",
        "ts": str(now - age_ms)}]
    r._poll_position_prices(now)
    assert (p.invalid, p._best) == before
    assert "BTCUSDT" not in r._price_seen


def test_duplicate_quote_does_not_advance_again(tmp_path, monkeypatch):
    r, client, p, now = _attach_15m(tmp_path)
    r._advance_price_clock(now)
    monkeypatch.setattr(lr.time, "time", lambda: now / 1000)
    quote = {"symbol": "BTCUSDT", "lastPr": "115", "markPrice": "115",
             "ts": str(now)}
    client.tickers = lambda: [quote]
    r._poll_position_prices(now)
    assert p.invalid == pytest.approx(107.5)
    before = p._best
    quote.update(lastPr="125", markPrice="125")
    r._poll_position_prices(now)
    assert p.invalid == pytest.approx(107.5)
    assert p._best == before


def test_unconfirmed_entry_does_not_arm_or_ratchet(tmp_path, monkeypatch):
    r, client, p, now = _attach_15m(tmp_path)
    r.positions["BTCUSDT"]["price_source"] = "ref"
    monkeypatch.setattr(lr.time, "time", lambda: now / 1000)
    client.tickers = lambda: [{
        "symbol": "BTCUSDT", "lastPr": "115", "markPrice": "115",
        "ts": str(now)}]
    r._advance_price_clock(now)
    r._poll_position_prices(now)
    assert not p.armed
    assert p.invalid == pytest.approx(95.0)


def test_unclosed_15m_bar_cannot_arm(tmp_path):
    r, client, p, now = _attach_15m(tmp_path)
    client.history_candles = lambda *a, **k: [
        (now, 101.0, 112.0, 100.5, 111.0, 15.0)]
    r._advance_price_clock(now)
    assert not p.armed
    assert p.invalid == pytest.approx(95.0)


def test_new_position_clears_previous_clock_anchors(tmp_path):
    r, _, _, now = _attach_15m(tmp_path)
    r._price_last["BTCUSDT"] = now - 100_000_000
    r._price_seen["BTCUSDT"] = now - 1
    intent = {
        "oid": "new-position", "tf_label": "2h", "direction": 1,
        "qty": 1.0, "invalid": 95.0, "target_1": 110.0,
        "event_id": LEVEL_REJECTED, "route": "e3_market"}
    r._register_position("BTCUSDT", intent, 100.0, now)
    assert "BTCUSDT" not in r._price_last
    assert "BTCUSDT" not in r._price_seen


def test_closed_position_clears_clock_anchors(tmp_path):
    r, _, _, now = _attach_15m(tmp_path)
    r._price_seen["BTCUSDT"] = now - 1
    r._reconcile_closed("BTCUSDT", r.positions["BTCUSDT"])
    assert "BTCUSDT" not in r.positions
    assert "BTCUSDT" not in r._price_last
    assert "BTCUSDT" not in r._price_seen


def test_production_protection_clock_configuration():
    import json

    path = Path(__file__).resolve().parents[1] / "config" / "live.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["loop"]["poll_seconds"] == 5
    assert raw["protection"]["price_clock"] == "15m"
    assert raw["protection"]["max_quote_age_seconds"] == 30
