"""Execution-only protection: exchange targets and quote-driven stop amendments."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_realtime_protection import _attach_15m
from test_bitget_live import _Session, _client
from market_operator.bitget_live import BitgetError


def _realtime(tmp_path):
    r, client, p, now = _attach_15m(tmp_path, price_clock="realtime")
    r.positions["BTCUSDT"].pop("price_source", None)
    r.positions["BTCUSDT"]["qty"] = 10.0
    r.positions["BTCUSDT"]["entry"] = 100.0
    r.positions["BTCUSDT"]["event_id"] = p.event_id
    r.campaigns["BTCUSDT"].pm.reject_opp_lock_frac = 0.25
    r.campaigns["BTCUSDT"].pm.partial_exit_enabled = True
    return r, client, p, now


def _quote(client, now, last, mark=None):
    client.tickers = lambda: [{"symbol": "BTCUSDT", "lastPr": str(last),
                               "markPrice": str(last if mark is None else mark),
                               "ts": str(now)}]


def test_entry_order_carries_original_stop():
    for kind in ("market", "limit"):
        session = _Session(data={"orderId": "entry"})
        kw = dict(side="buy", size=10, client_oid="entry", trade_side="open",
                  preset_stop_loss=95.0)
        if kind == "limit":
            _client(session).place_limit_order("BTCUSDT", price=100, **kw)
        else:
            _client(session).place_market_order("BTCUSDT", **kw)
        body = json.loads(session.calls[0][2])
        assert body["presetStopLossPrice"] == "95.0"
        assert body["presetStopLossExecutePrice"] == "0"
        assert "presetStopSurplusPrice" not in body  # Do not silently close 100%.


def test_native_half_target_contract():
    session = _Session(data={"orderId": "tp"})
    _client(session).place_partial_take_profit(
        "BTCUSDT", trigger_price=110, size=5, hold_side="long", client_oid="tp")
    body = json.loads(session.calls[0][2])
    assert body["planType"] == "profit_plan"
    assert body["size"] == "5"
    assert body["triggerType"] == "fill_price"
    assert body["executePrice"] == "0"
    assert "side" not in body and "tradeSide" not in body


@pytest.mark.parametrize("payload", [{"entrustedList": None}, {}, {"entrustedList": []}])
def test_empty_exchange_history_is_always_a_list(payload):
    session = _Session(data=payload)
    assert _client(session).tpsl_order_history("BTCUSDT", "tp") == []


def test_modify_stop_does_not_cancel():
    session = _Session(data={"orderId": "sl"})
    _client(session).modify_stop_loss("BTCUSDT", order_id="sl", trigger_price=105)
    assert len(session.calls) == 1
    assert session.calls[0][1].endswith("/modify-tpsl-order")
    body = json.loads(session.calls[0][2])
    assert body["size"] == "" and body["triggerPrice"] == "105"


def test_target_rests_once_without_consuming_partial_exit(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    r._manage_targets(now + 5000)
    assert len(client.plan_placed) == 1
    assert client.plan_placed[0]["size"] == 5
    assert p.pending_reduce and not p.reduced
    assert p.size_frac == 1 and p.invalid == 95


@pytest.mark.parametrize("direction", [1, -1])
def test_touch_arms_and_ratchets_without_bar_or_fill_receipt(tmp_path, monkeypatch, direction):
    r, client, p, now = _realtime(tmp_path)
    if direction < 0:
        p.direction, p.invalid, p.target_1 = -1, 105.0, 90.0
        r.positions["BTCUSDT"].update(direction=-1, invalid=105, target_1=90)
    r._manage_targets(now)
    monkeypatch.setattr("live_runner.time.time", lambda: now / 1000)
    _quote(client, now, 112 if direction > 0 else 88)
    r._poll_position_prices(now)
    assert p.armed and not p.reduced and p.pending_reduce
    assert p.invalid == pytest.approx(104.5 if direction > 0 else 95.5)
    assert client.market_orders == []


def test_target_fill_consumed_once_and_moves_stop_to_breakeven(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    oid = r.positions["BTCUSDT"]["target_order"]["order_id"]
    client.tpsl_order_history = lambda *a: [
        {"orderId": oid, "executeOrderId": "executed", "planStatus": "executed"}]
    client.recent_fills = lambda *a, **k: [
        {"symbol": "BTCUSDT", "orderId": "executed", "price": "110",
         "size": "5", "cTime": str(now)}]
    r._sync_target_fills(now)
    r._sync_target_fills(now + 5000)
    assert r.positions["BTCUSDT"]["qty"] == 5
    assert p.reduced and p.armed and not p.pending_reduce
    assert p.size_frac == 0.5 and p.invalid == 100
    assert r.counters["reduce_fills"] == 1
    assert client.market_orders == []


def test_split_target_fills_are_incremental(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    oid = r.positions["BTCUSDT"]["target_order"]["order_id"]
    client.tpsl_order_history = lambda *a: [
        {"orderId": oid, "executeOrderId": "executed", "planStatus": "executed"}]
    fills = [{"symbol": "BTCUSDT", "orderId": "executed", "price": "110",
              "size": "2", "cTime": str(now)}]
    client.recent_fills = lambda *a, **k: fills
    r._sync_target_fills(now)
    assert r.positions["BTCUSDT"]["qty"] == 8 and p.pending_reduce
    fills.append({**fills[0], "size": "3", "cTime": str(now + 1)})
    r._sync_target_fills(now + 1000)
    assert r.positions["BTCUSDT"]["qty"] == 5
    assert p.size_frac == pytest.approx(0.5) and not p.pending_reduce


def test_amend_failure_keeps_old_stop(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r.stops["BTCUSDT"] = {"trigger": 95, "qty": 10, "order_id": "sl"}
    client._pending = {"entrustedList": [{"orderId": "sl", "symbol": "BTCUSDT"}]}
    p.invalid = r.positions["BTCUSDT"]["invalid"] = 98
    def reject(*a, **kw):
        raise BitgetError("temporary amendment failure")
    client.modify_stop_loss = reject
    r._manage_stops(now)
    assert r.stops["BTCUSDT"]["trigger"] == 95
    assert client.plan_canceled == [] and client.plan_placed == []


def test_target_not_deleted_by_stop_deduplication(tmp_path):
    r, client, _, now = _realtime(tmp_path)
    r.stops["BTCUSDT"] = {"trigger": 95, "qty": 10, "order_id": "sl"}
    r._stops_live_rows = [
        {"orderId": "tp", "symbol": "BTCUSDT", "clientOid": "mo21tp",
         "planType": "profit_plan"}]
    r._dedupe_stops(now)
    assert client.plan_canceled == []


def test_registration_installs_both_protections_before_notification(tmp_path):
    r, client, _, now = _realtime(tmp_path)
    r.positions.clear()
    r.campaigns["BTCUSDT"].pm.positions.clear()
    observed = []
    r.tg.send_fill = lambda *a, **kw: observed.append(len(client.plan_placed))
    r._register_position("BTCUSDT", {
        "oid": "entry", "tf_label": "2h", "direction": 1, "qty": 10,
        "invalid": 95.0, "target_1": 110.0, "event_id": "LEVEL_REJECTED",
        "route": "level_rejected_market", "fill_price_source": "ref"}, 100, now)
    assert observed == [2]
    assert client.plan_placed[0]["trigger_price"] == 95
    assert client.plan_placed[1]["trigger_price"] == 110
    assert client.plan_placed[1]["size"] == 5
    assert r.positions["BTCUSDT"]["price_source"] == "ref"


def test_restart_recovers_target_by_exact_client_id(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    order = r.positions["BTCUSDT"].pop("target_order")
    client._pending = {"entrustedList": [{
        "symbol": "BTCUSDT", "orderId": order["order_id"],
        "clientOid": order["client_oid"], "planType": "profit_plan"}]}
    r._stops_live_ids = None
    p.pending_reduce = False
    r._manage_targets(now + 5000)
    assert len(client.plan_placed) == 1
    assert r.positions["BTCUSDT"]["target_order"]["order_id"] == order["order_id"]


def test_cancelled_unexecuted_target_can_rearm(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    old = r.positions["BTCUSDT"]["target_order"]
    client.tpsl_order_history = lambda *a: [{
        "orderId": old["order_id"], "planStatus": "cancelled"}]
    r._sync_target_fills(now)
    assert not p.pending_reduce
    r._manage_targets(now + 5000)
    assert len(client.plan_placed) == 2
    assert r.positions["BTCUSDT"]["target_order"]["client_oid"] != old["client_oid"]


def test_history_without_fill_is_not_a_reduction(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    oid = r.positions["BTCUSDT"]["target_order"]["order_id"]
    client.tpsl_order_history = lambda *a: [{
        "orderId": oid, "executeOrderId": "pending-fill", "planStatus": "executed"}]
    r._sync_target_fills(now)
    assert not p.reduced and p.pending_reduce
    assert r.positions["BTCUSDT"]["qty"] == 10


def test_peak_ratchet_inside_same_15m_bar(tmp_path, monkeypatch):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    monkeypatch.setattr("live_runner.time.time", lambda: now / 1000)
    _quote(client, now, 112)
    r._poll_position_prices(now)
    assert p.invalid == 104.5
    _quote(client, now + 5000, 115)
    r._poll_position_prices(now + 5000)
    assert p.invalid == 107.5
    _quote(client, now + 10000, 114)
    r._poll_position_prices(now + 10000)
    assert p.invalid == 107.5 and not p.reduced


def test_target_submit_failure_uses_immediate_touch_fallback(tmp_path, monkeypatch):
    r, client, p, now = _realtime(tmp_path)
    def reject(*a, **kw):
        raise BitgetError("target rejected")
    client.place_partial_take_profit = reject
    r._manage_targets(now)
    monkeypatch.setattr("live_runner.time.time", lambda: now / 1000)
    _quote(client, now, 112)
    r._poll_position_prices(now)
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["size"] == 5
    assert p.reduced and p.armed


def test_existing_preset_stop_is_reused(tmp_path):
    r, client, _, now = _realtime(tmp_path)
    client._pending = {"entrustedList": [{
        "symbol": "BTCUSDT", "orderId": "preset", "planType": "pos_loss",
        "holdSide": "long", "triggerPrice": "95", "clientOid": "mo21entry"}]}
    r._manage_stops(now)
    assert client.plan_placed == []
    assert r.stops["BTCUSDT"]["order_id"] == "preset"


def test_target_fill_does_not_double_subtract_reconciled_quantity(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r._manage_targets(now)
    order = r.positions["BTCUSDT"]["target_order"]
    # The ordinary position reconciler can observe the exchange reduction
    # before its execution report becomes available.
    r.positions["BTCUSDT"]["qty"] = 5
    client.tpsl_order_history = lambda *a: [{
        "orderId": order["order_id"], "executeOrderId": "done",
        "planStatus": "executed"}]
    client.recent_fills = lambda *a, **kw: [{
        "symbol": "BTCUSDT", "orderId": "done", "price": "110",
        "size": "5", "cTime": str(now)}]
    r._sync_target_fills(now)
    assert r.positions["BTCUSDT"]["qty"] == 5
    assert p.size_frac == 0.5 and p.reduced