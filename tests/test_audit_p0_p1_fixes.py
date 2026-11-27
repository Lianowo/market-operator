"""Targeted regression tests for the full P0/P1/P2 codebase audit fixes."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from market_operator.bitget_live import BitgetError, _fmt_decimal
from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind, RestingLimitOrder
from market_operator.campaign import _submit_decisions
from market_operator.events import BREAKOUT_ACCEPTED, LevelAuctionEngine
from market_operator.heartbeat import HeartbeatGuard
from market_operator.hypothesis import Hypothesis
from market_operator.level import Canvas2DLevelScanner, Level, LevelKind, LevelRole, LevelState
from market_operator.position import PositionManager
from market_operator.replay import _max_drawdown
from market_operator.routing import RouteDecision
from market_operator.sessions import in_session
from market_operator.structure import Regime
from market_operator import ops as ops_mod
from live_runner import LiveRunner
from live_supervisor import Supervisor
from test_bitget_live import _Session, _client
from test_exchange_target_protection import _quote, _realtime


def _spec(sym: str) -> ContractSpec:
    return ContractSpec(
        symbol=sym, size_min=0.001, size_step=0.001,
        size_place=3, price_place=2, min_trade_usdt=5.0,
    )


# ---------------------------------------------------------------------------
# P0-3: Anti-scientific-notation fixed-point formatting in bitget_live.py
# ---------------------------------------------------------------------------

def test_fmt_decimal_no_scientific_notation():
    assert _fmt_decimal(0.0000852) == "0.0000852"
    assert _fmt_decimal(1e-5) == "0.00001"
    assert _fmt_decimal(1e-7) == "0.0000001"
    assert _fmt_decimal(100.0) == "100.0"
    assert _fmt_decimal(5) == "5"
    assert _fmt_decimal(0) == "0"
    for v in (1e-8, 8.52e-5, 1.23e-6, 0.0000045):
        s = _fmt_decimal(v)
        assert "e" not in s and "E" not in s


def test_bitget_order_payloads_never_emit_scientific_notation():
    session = _Session(data={"orderId": "ord1"})
    cli = _client(session)
    cli.place_limit_order(
        "PEPEUSDT", side="buy", size=1e-5, price=8.52e-5,
        client_oid="cid1", trade_side="open", preset_stop_loss=7.9e-5,
    )
    cli.place_stop_loss("PEPEUSDT", trigger_price=7.9e-5, hold_side="long", client_oid="cid2")
    cli.modify_stop_loss("PEPEUSDT", order_id="ord1", trigger_price=8.1e-5)
    cli.place_partial_take_profit("PEPEUSDT", trigger_price=9.5e-5, size=5e-5, hold_side="long", client_oid="cid3")
    assert len(session.calls) == 4
    for _method, _url, raw_body in session.calls:
        body = json.loads(raw_body)
        for k in ("size", "price", "triggerPrice", "presetStopLossPrice"):
            if k in body and body[k] != "":
                assert "e" not in body[k] and "E" not in body[k], f"{k}={body[k]}"


# ---------------------------------------------------------------------------
# P0-1: reenter_invalid_px wired into live PositionManager & triggers reentry
# ---------------------------------------------------------------------------

def test_register_position_wires_reenter_invalid_px_and_triggers_reentry(tmp_path):
    r, _client_mock, _p, now = _realtime(tmp_path)
    cs = r.campaigns["BTCUSDT"]
    r.positions.clear()
    cs.pm.positions.clear()
    cs.pm.reentry_confirm_bars = 1
    it = {
        "oid": "oid-re1", "symbol": "BTCUSDT", "tf_label": cs.tf_label,
        "role": "entry", "kind": "market", "direction": 1, "qty": 1.0,
        "invalid": 95.0, "target_1": 120.0, "event_id": BREAKOUT_ACCEPTED,
        "route": "BREAKOUT", "box_lo": 98.0, "box_hi": 100.0,
        "reenter_invalid_px": 98.0, "fill_price_source": "fills",
    }
    r._register_position("BTCUSDT", it, 102.0, now)
    pm_pos = cs.pm.positions["BTCUSDT"]
    assert pm_pos.reenter_invalid_px == pytest.approx(98.0)
    assert r.positions["BTCUSDT"]["reenter_invalid_px"] == pytest.approx(98.0)
    # Trigger bar closes back inside zone_lo (97.5 < 98.0) -> FAILED_BREAKOUT_REENTRY
    trig_bar = (now + 3_600_000, 101.0, 101.5, 97.0, 97.5, 10.0)
    intents = cs.pm.on_trigger_bar("BTCUSDT", trig_bar, ())
    assert len(intents) == 1
    assert intents[0].kind.value == "EXIT"
    assert "FAILED_BREAKOUT_REENTRY" in intents[0].reason


# ---------------------------------------------------------------------------
# P0-2: LiveRunner HWM peak_equity ratchet on realized capital + persistence
# ---------------------------------------------------------------------------

def test_live_runner_hwm_ratchet_uses_realized_capital_and_never_steps_up():
    live = json.loads((ROOT / "config/live.json").read_text(encoding="utf-8"))
    runner = LiveRunner.__new__(LiveRunner)
    runner.risk = live["risk"]
    runner.equity = 120_000.0
    runner.unrealized_pl = 10_000.0  # realized = 110,000 -> tier <= 300k -> 0.028
    runner.peak_equity = None
    assert runner.current_risk_frac() == pytest.approx(0.028)
    assert runner.peak_equity == pytest.approx(110_000.0)
    # Floating profit spike must NOT prematurely bump HWM above realized capital
    runner.equity = 350_000.0
    runner.unrealized_pl = 240_000.0  # realized still 110,000
    assert runner.current_risk_frac() == pytest.approx(0.028)
    assert runner.peak_equity == pytest.approx(110_000.0)
    # Drawdown back down to 5,000 must NEVER step risk rate back up to 0.04
    runner.equity = 5_000.0
    runner.unrealized_pl = 0.0
    assert runner.current_risk_frac() == pytest.approx(0.028)
    assert runner.peak_equity == pytest.approx(110_000.0)


# ---------------------------------------------------------------------------
# P0-4: SCALE_PROMOTED must NOT close lower-TF position if higher-TF is blocked
# ---------------------------------------------------------------------------

def test_campaign_scale_promoted_keeps_lower_tf_when_higher_tf_blocked():
    contracts = {"BTCUSDT@4h": _spec("BTCUSDT@4h"), "BTCUSDT@1d": _spec("BTCUSDT@1d")}
    broker = Broker(
        equity=500.0, risk_frac=0.04, leverage_cap=20.0,
        taker_fee=0.0005, slippage_per_side=0.0002, funding_per_8h=0.0,
        contracts=contracts,
        portfolio_stop_risk_frac=0.10, directional_stop_risk_frac=0.06,
        tf_stop_risk_frac_by_tf={"4h": 0.08, "1d": 0.005},  # 1d cap too tiny!
    )
    open_it = OrderIntent(OrderKind.OPEN, "BTCUSDT@4h", None, "r", 100.0, 0)
    broker.submit(open_it, 0, direction=1, invalid=95.0, target_1=120.0, event_id="EV", meta={"size_mult": 1.0, "tf_label": "4h"})
    broker.on_bar_open("BTCUSDT@4h", 3_600_000, 100.0)
    assert "BTCUSDT@4h" in broker.positions

    st_1d = SimpleNamespace(
        frame=SimpleNamespace(key="BTCUSDT@1d", symbol="BTCUSDT", tf_label="1d", tf_hours=24),
        pos_mgr=PositionManager(),
    )
    hyp = Hypothesis(BREAKOUT_ACCEPTED, 1, 95.0, 140.0, Regime.TREND_UP, 3_600_000, 1, entry=105.0)
    dec = RouteDecision(
        kind="market",
        route="BREAKOUT",
        intent=OrderIntent(OrderKind.OPEN, "BTCUSDT@1d", None, "r", 105.0, 7_200_000),
        hyp=hyp,
        meta={"size_mult": 1.0, "tf_label": "1d"},
    )
    counters: dict[str, int] = {
        "open_intents": 0, "routed_market": 0, "portfolio_cap_skips": 0,
        "symbol_slot_skips": 0, "e1_priority_cancels": 0,
    }
    cap_skips: list = []
    _submit_decisions(
        st_1d, broker, [dec], 7_200_000, 7_200_000, 7_200_000,
        {}, counters, cap_skips, 6, 7_200_000 + 86_400_000,
    )
    # 1d was blocked by TF_CAP: 4h position MUST NOT have a pending SCALE_PROMOTED EXIT!
    assert counters.get("scale_promotions", 0) == 0
    assert counters.get("portfolio_cap_skips", 0) == 1
    assert not any(
        po.intent.kind is OrderKind.EXIT
        for po in broker.pending.get("BTCUSDT@4h", [])
    )


def test_live_scale_promoted_keeps_lower_tf_when_higher_tf_blocked(tmp_path):
    r, _client_mock, _p, now = _realtime(tmp_path)
    r.equity = 10_000.0
    r.unrealized_pl = 0.0
    r.positions["BTCUSDT"]["tf_label"] = "4h"
    r.extra_campaigns[("BTCUSDT", "4h")] = r.campaigns["BTCUSDT"]
    r.campaigns["BTCUSDT"].tf_label = "4h"
    r.campaigns["BTCUSDT"].obs_tf_seconds = 14400
    cs_1d = SimpleNamespace(
        symbol="BTCUSDT", tf_label="1d", obs_tf_seconds=86400, trigger_tf_seconds=14400,
        cog_bars=r.campaigns["BTCUSDT"].cog_bars, trig_bars=r.campaigns["BTCUSDT"].trig_bars,
        pm=PositionManager(), detector=r.campaigns["BTCUSDT"].detector,
    )
    r.risk["tf_cap_by_tf"] = {"4h": 0.50, "1d": 0.001}
    hyp = Hypothesis(BREAKOUT_ACCEPTED, 1, 95.0, 140.0, Regime.TREND_UP, now, 1, entry=105.0)
    dec = RouteDecision(
        kind="market",
        route="BREAKOUT",
        intent=OrderIntent(OrderKind.OPEN, "BTCUSDT", None, "r", 105.0, now),
        hyp=hyp,
        meta={"size_mult": 1.0, "zone_lo": 99.0, "zone_hi": 100.0},
    )
    bar = (now, 104.0, 106.0, 103.0, 105.0, 10.0)
    r._dispatch_route_decision(dec, cs_1d, now, True, None, bar)
    assert "BTCUSDT" in r.positions
    assert r.positions["BTCUSDT"]["tf_label"] == "4h"
    assert r.counters.get("scale_promotions", 0) == 0


# ---------------------------------------------------------------------------
# P0-5: Full-TP profit_plan race guard + 22002 recovery in _live_market_exit
# ---------------------------------------------------------------------------

def test_full_tp_resting_does_not_fire_competing_local_market_exit(tmp_path):
    r, client, p, now = _realtime(tmp_path)
    r.campaigns["BTCUSDT"].pm.partial_exit_enabled = False
    r.campaigns["BTCUSDT"].pm.reject_opp_lock_frac = 1.0
    p.target_1 = 110.0
    p.pending_reduce = True
    r.positions["BTCUSDT"]["target_1"] = 110.0
    r.positions["BTCUSDT"]["target_order"] = {"order_id": "TP_FULL", "qty": 10.0}
    _quote(client, now, last=111.0, mark=111.0)
    called = []
    r._live_market_exit = lambda *a, **kw: called.append(1) or True
    r._poll_position_prices(now)
    assert called == []
    assert p.armed is True
    assert r._reconcile_at == 0.0


def test_live_market_exit_reconciles_cleanly_on_22002(tmp_path):
    r, client, _p, now = _realtime(tmp_path)
    client.place_market_order = lambda *a, **kw: (_ for _ in ()).throw(
        BitgetError("Bitget 400 code=22002: No position to close")
    )
    client.positions = lambda: []
    client.recent_fills = lambda *a, **kw: []
    ok = r._live_market_exit(
        r.positions["BTCUSDT"], ts=now, why="lock_exit", route="LOCK", ref_px=110.0
    )
    assert ok is True
    assert "BTCUSDT" not in r.positions
    assert "BTCUSDT" not in r._exit_retry
    assert r.counters.get("close_no_pos", 0) == 1


# ---------------------------------------------------------------------------
# P0-6: HeartbeatGuard ignores profit_plan & wrong holdSide
# ---------------------------------------------------------------------------

def test_heartbeat_guard_ensure_protection_ignores_profit_plan_and_wrong_side(tmp_path):
    placed = []

    class FakeClient:
        def positions(self):
            return [{"symbol": "BTCUSDT", "direction": 1, "qty": 1.0, "mark_price": 100.0}]

        def tpsl_orders_pending(self):
            return {"entrustedList": [
                {"symbol": "BTCUSDT", "orderId": "tp1", "clientOid": "mo21tp", "planType": "profit_plan", "holdSide": "long"},
                {"symbol": "BTCUSDT", "orderId": "sl_s", "clientOid": "mo21sl_short", "planType": "pos_loss", "holdSide": "short"},
            ]}

        def place_stop_loss(self, sym, *, trigger_price, hold_side, client_oid):
            placed.append((sym, trigger_price, hold_side, client_oid))
            return {"orderId": "sl-guard"}

    runner = SimpleNamespace(
        mode="live",
        OID_PREFIX="mo21",
        positions={"BTCUSDT": {"symbol": "BTCUSDT", "direction": 1, "invalid": 95.0, "event_id": "EV1"}},
        stops={},
        fit_price=lambda sym, px, d: px,
        stop_hold_side=lambda d: "long" if d > 0 else "short",
        tg=SimpleNamespace(send_alert=lambda *a, **kw: None),
        heartbeat_doc=lambda *a, **kw: {},
    )
    guard = HeartbeatGuard(runner, path=tmp_path / "hb.json", client_factory=lambda: FakeClient())
    res = guard.ensure_protection()
    assert res["placed"] == 1
    assert len(placed) == 1
    assert placed[0][0] == "BTCUSDT" and placed[0][2] == "long"


# ---------------------------------------------------------------------------
# P0-7: Memory pruning bounds (_registry, LevelAuctionEngine.states, runner)
# ---------------------------------------------------------------------------

def test_memory_pruning_bounds_scanner_auction_and_runner(tmp_path):
    # 1. Canvas2DLevelScanner._registry pruned to sliding window
    scanner = Canvas2DLevelScanner(canvas_bars=60)
    bars = []
    for i in range(300):
        px = 100.0 + 10.0 * ((i % 20) - 10) / 10.0
        bars.append((i * 3_600_000, px, px + 2.0, px - 2.0, px + 0.5, 10.0))
        scanner.update_canvas(bars, atr=2.0)
    assert all(k[0] >= 300 - 60 for k in scanner._registry)

    # 2. LevelAuctionEngine.states pruned after > 320 unseen bars
    eng = LevelAuctionEngine()
    lv = Level(
        uid=1, role=LevelRole.RESISTANCE, kind=LevelKind.SWING,
        extreme=100.0, neck=99.0, state=LevelState.FRESH,
        origin_index=0, known_index=0, origin_ts=0, last_change_known=0,
    )
    b0 = (0, 98.0, 99.5, 97.5, 99.0, 1.0)
    b1 = (3_600_000, 99.0, 99.8, 98.5, 99.2, 1.0)
    eng.step(bar_index=1, bar=b1, prev_bar=b0, recent_bars=[b0, b1], active_levels=[lv], atr=1.0, k_bm=0.15)
    assert len(eng.states) == 1
    b400 = (400 * 3_600_000, 98.0, 99.5, 97.5, 99.0, 1.0)
    eng.step(bar_index=400, bar=b400, prev_bar=b1, recent_bars=[b1, b400], active_levels=[], atr=1.0, k_bm=0.15)
    assert len(eng.states) == 0

    # 3. LiveRunner._prune_runtime_state drops old terminal intents, keeps armed & recent
    r, _c, _p, now = _realtime(tmp_path)
    r.open_intents = {
        "old_filled": {"status": "filled", "fill_ts": now - 100_000_000},
        "recent_filled": {"status": "filled", "fill_ts": now - 1_000},
        "old_armed": {"status": "armed", "decision_ts": now - 200_000_000},
    }
    r._alert_last = {"old": now / 1000.0 - 100_000.0, "new": now / 1000.0 - 10.0}
    r._prune_runtime_state(now)
    assert "old_filled" not in r.open_intents
    assert "recent_filled" in r.open_intents
    assert "old_armed" in r.open_intents
    assert "old" not in r._alert_last and "new" in r._alert_last


# ---------------------------------------------------------------------------
# P1-5: Cross-year holiday boundary in sessions.in_session
# ---------------------------------------------------------------------------

def test_sessions_cross_year_new_year_eve_holiday_blocked():
    # Dec 31 2026 20:00 America/New_York = Jan 1 2027 01:00 UTC -> local.year=2026,
    # belongs to the 2027-01-01 daily session (New Year's Day holiday derived from year 2027!).
    ts_ny_eve_evening = int(datetime(2027, 1, 1, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    custom_cfg = {
        "calendars": {
            "us_fut_hol": {
                "tz": "America/New_York",
                "holiday_rules": "us_equity",
                "daily_session": {"start_weekdays": [7, 1, 2, 3, 4], "open": "18:00", "close": "17:00"},
            }
        }
    }
    assert in_session("us_fut_hol", ts_ny_eve_evening, cfg=custom_cfg, overrides={}) is False
    # Same cross-year check via explicit override for 2027-01-01
    assert in_session(
        "us_index_futures", ts_ny_eve_evening,
        overrides={"closed": {"us_index_futures": ["2027-01-01"]}},
    ) is False


# ---------------------------------------------------------------------------
# P1-6: Independent max_dd_pct vs max_dd_usdt in replay._max_drawdown
# ---------------------------------------------------------------------------

def test_replay_max_drawdown_tracks_true_max_pct_independently_of_usdt():
    rows = [
        (0, 500.0),
        (1 * 86_400_000, 1000.0),
        (2 * 86_400_000, 450.0),       # 55% DD (-550 U)
        (3 * 86_400_000, 100_000.0),
        (4 * 86_400_000, 90_000.0),    # 10% DD (-10,000 U)
    ]
    dd = _max_drawdown(rows)
    assert dd["max_dd_pct"] == pytest.approx(0.55)
    assert dd["max_dd_usdt"] == pytest.approx(10_000.0)
    assert dd["peak_ts"] == 1 * 86_400_000
    assert dd["trough_ts"] == 2 * 86_400_000


# ---------------------------------------------------------------------------
# P1-4: Broker._fill_open_limit enforces factor_burst_cap_1h
# ---------------------------------------------------------------------------

def test_broker_fill_open_limit_enforces_factor_burst_cap():
    contracts = {"BTCUSDT@4h": _spec("BTCUSDT@4h"), "ETHUSDT@4h": _spec("ETHUSDT@4h")}
    broker = Broker(
        equity=1000.0, risk_frac=0.02, leverage_cap=10.0,
        taker_fee=0.0005, slippage_per_side=0.0002, funding_per_8h=0.0,
        contracts=contracts, portfolio_stop_risk_frac=0.50,
        factor_burst_cap_1h=1,
    )
    ts_dec = 9 * 3_600_000
    ts_fill = 10 * 3_600_000
    # First symbol fills via market order in the 10h bucket
    it1 = OrderIntent(OrderKind.OPEN, "BTCUSDT@4h", None, "r", 100.0, ts_dec)
    broker.submit(it1, ts_dec, direction=1, invalid=95.0, target_1=120.0, event_id="E1", meta={"tf_label": "4h"})
    broker.on_bar_open("BTCUSDT@4h", ts_fill, 100.0)
    assert "BTCUSDT@4h" in broker.positions

    # Second symbol has a resting limit order that gets crossed in the SAME 10h bucket
    lim = RestingLimitOrder(
        direction=1, limit_price=100.0, armed_at_ts=ts_dec,
        expiry_ts=ts_dec + 6 * 3_600_000, symbol="ETHUSDT@4h", role="entry",
        invalid=95.0, target_1=120.0, event_id="E2", meta={"tf_label": "4h"},
    )
    broker.submit_limit(lim)
    broker.on_15m_bar("ETHUSDT@4h", ts_fill, 101.0, 102.0, 98.0, 101.0)
    assert "ETHUSDT@4h" not in broker.positions
    assert broker.n_skips.get("FACTOR_BURST_CAP_1H", 0) == 1


# ---------------------------------------------------------------------------
# P1-7: LiveSupervisor._alert throttles repeated give_up alerts
# ---------------------------------------------------------------------------

def test_supervisor_alert_throttles_give_up():
    sent = []
    sup = Supervisor.__new__(Supervisor)
    sup.tg = SimpleNamespace(send_alert=lambda msg: sent.append(msg))
    sup._alert_last = {}
    sup._alert("give up", key="give_up", gap_s=1800.0)
    sup._alert("give up again", key="give_up", gap_s=1800.0)
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# P1-7: ops.warmup writes 2H bars to cache even when tf_list starts with 1h
# ---------------------------------------------------------------------------

def test_ops_warmup_uses_base_granularity_for_cache(tmp_path, monkeypatch):
    import market_operator.bitget_live as bl_mod

    cfg = {
        "cognition": {
            "granularity": "2H",
            "tf_list": ["1h", "4h", "1d"],
            "warmup_trigger_bars": 20,
        },
        "symbols_core": ["BTCUSDT"],
        "universe": {"enabled": False},
    }
    fetched_grans = []
    cached_written = []

    class FakeClient:
        def __init__(self, *_a, **_kw):
            pass

        def history_candles(self, sym, gran, *, end_ms=None, limit=200):
            fetched_grans.append(gran)
            step = 7_200_000 if gran.upper() == "2H" else 3_600_000
            return [[(i + 1) * step, 100.0, 101.0, 99.0, 100.5, 10.0] for i in range(limit)]

    fake_runner_mod = SimpleNamespace(
        load_strategy_params=lambda *_a, **_kw: ({"tf_list": ["1h", "4h"], "trigger_tf_by_htf": {}}, None, None, None),
        load_contracts_live=lambda *_a, **_kw: ({"BTCUSDT": _spec("BTCUSDT")}, {}),
        aggregate_tf=lambda bars, obs: bars[::max(1, obs // 7200)],
        replay_cognition=lambda *a, **kw: None,
    )
    monkeypatch.setattr(bl_mod, "BitgetLiveClient", FakeClient)
    monkeypatch.setattr(ops_mod, "load_secrets", lambda *_a, **_kw: {})
    monkeypatch.setattr(ops_mod, "load_runner_module", lambda: fake_runner_mod)
    monkeypatch.setattr(ops_mod, "_runner_pid", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        ops_mod, "_write_candle_cache",
        lambda _cfg, sym, bars: cached_written.append((sym, bars[1][0] - bars[0][0])) or (tmp_path / f"{sym}_2H.json.gz"),
    )
    res = ops_mod.warmup(cfg, symbols=["BTCUSDT"], timeframes=["1h", "4h"], bars=60, write_cache=True, check_account=False)
    assert res["ok"] is True
    assert "2H" in fetched_grans
    assert cached_written == [("BTCUSDT", 7_200_000)]


# ---------------------------------------------------------------------------
# P1-1: Global phase barrier in LiveRunner._step_inner ranks HTF / priority globally
# ---------------------------------------------------------------------------

def test_live_runner_phase_barrier_ranks_higher_tf_and_priority_globally(tmp_path):
    r, _client_mock, _p, now = _realtime(tmp_path)
    r.mode = "paper"
    r.universe_enabled = False
    r.positions.clear()
    r.open_intents.clear()
    r.extra_campaigns.clear()
    r.campaigns.clear()
    r.risk["factor_burst_cap_1h"] = 1
    r.risk["portfolio_cap_frac"] = 0.50
    r.risk["directional_cap_frac"] = 0.50
    r.risk["tf_cap_by_tf"] = {"4h": 0.50, "1d": 0.50}
    r.equity = 10_000.0
    r.unrealized_pl = 0.0

    t0 = 100 * 7_200_000
    t_next = t0 + 7_200_000
    now_step = t_next + 2 * 7_200_000

    class DummyStruct:
        def variance_ratio(self, _k=4):
            return 1.0
        def imbalance_strength(self):
            return 0.0

    def _make_cs(sym: str, tf: str, obs_s: int, pri: float):
        hyp = Hypothesis(BREAKOUT_ACCEPTED, 1, 95.0, 120.0, Regime.TREND_UP, t_next, 1, entry=100.0)
        dec = RouteDecision(
            kind="market",
            route="BREAKOUT",
            intent=OrderIntent(OrderKind.OPEN, sym, None, "r", 100.0, t_next),
            hyp=hyp,
            meta={"priority_score": pri, "size_mult": 1.0, "confirm_ts": t_next + 7_200_000},
        )
        return SimpleNamespace(
            symbol=sym, tf_label=tf, obs_tf_seconds=obs_s, trigger_tf_seconds=7200,
            trig_bars=[(t0 - 7_200_000, 99.0, 100.0, 98.0, 99.5, 1.0), (t0, 99.5, 100.5, 99.0, 100.0, 1.0)],
            cog_bars=[(t0, 99.5, 100.5, 99.0, 100.0, 1.0)],
            detector=SimpleNamespace(structure=DummyStruct()),
            pm=PositionManager(),
            router=SimpleNamespace(),
            tracker=None,
            _dec=dec,
        )

    cs_a = _make_cs("AAAUSDT", "4h", 14400, 1.0)
    cs_z = _make_cs("ZZZUSDT", "1d", 86400, 5.0)
    r.campaigns = {"AAAUSDT": cs_a, "ZZZUSDT": cs_z}
    r.client.history_candles = lambda sym, gran, start_ms=0, limit=100: [
        (t_next, 100.0, 101.0, 99.0, 100.5, 1.0)
    ]

    def fake_advance(cs, now_ms, new_bars=None, run_paper_fills=True, defer_routes=None):
        assert defer_routes is not None
        defer_routes.append((cs, 1.0, [SimpleNamespace(event_id=BREAKOUT_ACCEPTED)], None, False, new_bars[-1], 1))

    r._advance = fake_advance
    r._evaluate_route = lambda cs, atr, evs, view, *, mic, trig_bar=None, bar_index=None: [
        (cs._dec, cs, t_next, mic, view, trig_bar)
    ]
    r._step_inner(now_step)
    armed = [it["symbol"] for it in r.open_intents.values() if it.get("status") == "armed"]
    assert armed == ["ZZZUSDT"]
