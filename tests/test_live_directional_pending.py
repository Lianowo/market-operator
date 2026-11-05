"""Offline live/paper adapter checks for same-side in-flight risk."""

import pytest

from tests.test_live_runtime import NOW, _FakeClient, _SignalTG, _campaign, _decision, _pos, _runner


def _watch(runner, symbol, direction=1):
    decision = _decision(kind="limit", direction=direction,
                         invalid=95 if direction > 0 else 105)
    decision.limit_price = 100
    decision.meta.update(watch_entry=True)
    runner._submit_open(_campaign(symbol), decision, NOW, mic=False)


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_pending_same_side_blocks_second_but_opposite_side_is_independent(tmp_path, mode):
    runner = _runner(tmp_path, _FakeClient(), _SignalTG(), mode=mode)
    runner.equity = 104
    runner.directional_cap_frac = .08
    _watch(runner, "BTCUSDT")
    _watch(runner, "ETHUSDT")
    assert len(runner.open_intents) == 1
    assert "同向风险额度" in runner.tg.signals[-1]["exec_status"]
    _watch(runner, "SOLUSDT", -1)
    assert len(runner.open_intents) == 2


@pytest.mark.parametrize("status", ["canceled", "expired", "error", "void"])
def test_terminal_pending_releases_directional_budget(tmp_path, status):
    runner = _runner(tmp_path, _FakeClient(), _SignalTG(), mode="paper")
    runner.equity = 104
    runner.directional_cap_frac = .08
    _watch(runner, "BTCUSDT")
    first, = runner.open_intents.values()
    first["status"] = status
    _watch(runner, "ETHUSDT")
    assert len(runner.open_intents) == 2


def test_watch_rechecks_budget_after_equity_drop_and_does_not_double_count_self(tmp_path):
    client = _FakeClient()
    runner = _runner(tmp_path, client, _SignalTG())
    runner.equity = 104
    runner.directional_cap_frac = .08
    _watch(runner, "BTCUSDT")
    oid, it = next(iter(runner.open_intents.items()))
    assert not runner._directional_entry_blocked(1, it["qty"], 100, 95, exclude_oid=oid)
    runner.equity = 50
    runner._trigger_watch_entry_live(oid, it, 100, NOW + 1, "WATCH_INNER_EDGE_HIT")
    assert it["status"] == "canceled" and client.market_orders == []


def test_paper_fill_rechecks_directional_budget(tmp_path):
    runner = _runner(tmp_path, _FakeClient(), _SignalTG(), mode="paper")
    runner.equity = 104
    runner.directional_cap_frac = .08
    _watch(runner, "BTCUSDT")
    oid, it = next(iter(runner.open_intents.items()))
    runner.equity = 50
    runner._paper_entry_fill("BTCUSDT", oid, it, 100, NOW + 1)
    assert it["status"] == "void" and runner.positions == {} and runner.equity == 50


def test_filled_exposure_breach_is_audit_only_and_deduplicated(tmp_path):
    client, tg = _FakeClient(), _SignalTG()
    runner = _runner(tmp_path, client, tg)
    runner.equity = 104
    runner.directional_cap_frac = .08
    runner.positions["BTCUSDT"] = _pos(qty=2)
    it = {"oid": "fill", "symbol": "BTCUSDT", "direction": 1, "status": "filled"}
    runner._audit_directional_fill(it)
    runner._audit_directional_fill(it)
    assert it["directional_cap_breach"] is True
    assert runner.counters["directional_cap_breaches"] == 1
    assert runner.positions["BTCUSDT"]["qty"] == 2 and client.market_orders == []


def test_disabled_directional_cap_preserves_parallel_intents(tmp_path):
    runner = _runner(tmp_path, _FakeClient(), _SignalTG(), mode="paper")
    runner.equity = 104
    runner.directional_cap_frac = None
    _watch(runner, "BTCUSDT")
    _watch(runner, "ETHUSDT")
    assert len(runner.open_intents) == 2