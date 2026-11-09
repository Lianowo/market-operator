"""Bounded entry semantics and notification price provenance, fully offline."""
import pytest

from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
from market_operator.hypothesis import StructureView, entry_contract_error, execution_target
from market_operator.position import PositionManager
from dataclasses import replace
from market_operator.routing import MarketRouter, RouterParams, WHITELIST_BALANCE
from market_operator.structure import Regime, Zone, ZoneRole
from market_operator.tg import TelegramNotifier
from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind


@pytest.mark.parametrize("direction", [1, -1])
def test_reject_cannot_outlive_opposite_target(direction):
    z = Zone(lo=99, hi=100, role=ZoneRole.SUPPORT if direction > 0 else ZoneRole.RESISTANCE,
             box_opposite=110 if direction > 0 else 89)
    close = 111 if direction > 0 else 88
    ev = MarketEvent(LEVEL_REJECTED, direction, 0, 1,
                     {"zone": z, "ref": 100, "invalid": 98 if direction > 0 else 101})
    router = MarketRouter(RouterParams(whitelist=WHITELIST_BALANCE,
                          reject_target_mode="opposite", reject_use_struct_stop=True))
    assert router.on_bar("X", (0, close, close+1, close-1, close, 1), 1,
                         StructureView(Regime.RANGE, (z,), 1), 1, [ev],
                         position_open=False, entry_resting=False) == []


@pytest.mark.parametrize("direction", [1, -1])
def test_reentry_preserves_structural_stop_and_target(direction):
    z = Zone(lo=99, hi=100, role=ZoneRole.RESISTANCE if direction > 0 else ZoneRole.SUPPORT,
             box_opposite=110 if direction > 0 else 89, source_kind="FLIPPED_BOX_EDGE")
    edge = 100 if direction > 0 else 99
    invalid = 98.8 if direction > 0 else 100.2
    close = edge + direction * 2
    ev = MarketEvent(BREAKOUT_ACCEPTED, direction, 0, 1,
                     {"zone": z, "ref": edge, "invalid": invalid, "close_ts": 7200000})
    p = RouterParams(whitelist=WHITELIST_BALANCE,
                     reentry_use_struct_stop=True, reentry_use_box_target=True)
    decisions = MarketRouter(p).on_bar("X", (0, close, close+1, close-1, close, 1), 1,
                  StructureView(Regime.RANGE, (z,), 2), 2, [ev],
                  position_open=False, entry_resting=False)
    assert len(decisions) == 1
    dec = decisions[0]
    assert dec.kind == "market" and dec.meta["bounded_entry"]
    assert dec.hyp.invalid == invalid
    assert dec.hyp.target_1 == z.box_opposite
    assert dec.meta["structural_target"] == z.box_opposite
    assert execution_target(dec.meta, dec.hyp.target_1) is None
    managed = MarketRouter(replace(p, reentry_target_management=True)).on_bar(
        "X", (0, close, close+1, close-1, close, 1), 1,
        StructureView(Regime.RANGE, (z,), 2), 2, [ev],
        position_open=False, entry_resting=False)[0]
    assert execution_target(managed.meta, managed.hyp.target_1) == z.box_opposite
    assert dec.meta["confirm_close"] == close
    assert dec.meta["confirm_ts"] == 7200000


def test_fill_contract_rechecks_actual_price_not_watch_price():
    meta = {"bounded_entry": True, "entry_min_rr": 1.0}
    assert entry_contract_error(1, 100, 98, 110, meta) is None
    assert entry_contract_error(1, 105, 98, 110, meta) == "INSUFFICIENT_REMAINING_RR"
    assert entry_contract_error(-1, 89, 101, 90, meta) == "TARGET_ALREADY_PASSED"
    assert entry_contract_error(1, 100, 98, None, meta) == "MISSING_STRUCTURE_TARGET"
    assert entry_contract_error(1, float("nan"), 98, 110, meta) == "MISSING_STRUCTURE_TARGET"


def test_broker_rechecks_gapped_market_fill():
    b = Broker(equity=1000, risk_frac=0.01, leverage_cap=20,
               taker_fee=0.0006, slippage_per_side=0.0002, funding_per_8h=0,
               contracts={"X": ContractSpec(
        symbol="X", size_min=0.001, size_step=0.001, size_place=3,
        price_place=2, min_trade_usdt=5)})
    b.submit(OrderIntent(OrderKind.OPEN, "X", None, "audit", 100, 0), 0,
             direction=1, invalid=98, target_1=110, event_id=LEVEL_REJECTED,
             meta={"bounded_entry": True, "entry_min_rr": 1.0})
    rows = b.on_bar_open("X", 3_600_000, 111)
    assert "X" not in b.positions
    assert rows[0]["reason_code"] == "TARGET_ALREADY_PASSED"


def test_notification_distinguishes_prices_and_missing_snapshot():
    n = TelegramNotifier({"TELEGRAM_BOT_TOKEN": "SIG", "TELEGRAM_CHAT_ID": "PUBLIC"})
    bodies = []
    n._post_text_sync = lambda *args: bodies.append(args[-1]) or True
    n.send_signal("X", BREAKOUT_ACCEPTED, "buy", "breakout_accepted_limit", 100, 98,
                  box_lo=99, box_hi=110, decision_ts=7200000,
                  watch_details={"watch_price": 100, "max_wait_hours": 4},
                  price_context={"ev_ref": 99, "event_extreme": 98.5,
                                 "confirm_close": 104, "trade_context": "重新入箱"})
    text = bodies[0]
    assert "关注参考价: 100" in text and "候选触发价: 100" in text
    assert "观察窗口: 4 小时" in text and "信号时间:" in text
    assert "重新入箱" not in text and "事件过程极值" not in text


def test_execution_target_legacy_and_explicit_none():
    assert execution_target({}, 110) == 110
    assert execution_target({"execution_target": None}, 110) is None
    assert execution_target({"execution_target": 108}, 110) == 108


@pytest.mark.parametrize("resting", [False, True])
@pytest.mark.parametrize("managed", [False, True])
def test_campaign_passes_only_execution_target_to_manager(resting, managed):
    from types import SimpleNamespace
    from market_operator.campaign import _apply_open_rows, _apply_resting_rows
    pm = PositionManager(reject_opp_lock_frac=-0.25)
    st = SimpleNamespace(frame=SimpleNamespace(symbol="X", tf_hours=2, cog=SimpleNamespace(
        bars=[(0, 100, 101, 99, 100, 1)])), pos_mgr=pm)
    row = {"kind": "LIMIT_FILL" if resting else "OPEN", "price": 100,
           "meta": {"direction": 1, "invalid": 98, "target_1": 110,
                    "execution_target": 110 if managed else None,
                    "event_id": BREAKOUT_ACCEPTED}}
    counters = {"opens_filled": 0, "limit_fills_entry": 0}
    apply = _apply_resting_rows if resting else _apply_open_rows
    apply(st, None, "X", 0, [row], counters)
    assert pm.positions["X"].target_1 == (110 if managed else None)
    # Move beyond the structural target; observation alone must not arm locking.
    pm.on_bar("X", (7200000, 110, 112, 110, 111, 1), 1)
    assert pm.positions["X"].armed is managed


def test_explicit_rr_floor_blocks_insufficient_remaining_rr():
    z = Zone(lo=99, hi=100, role=ZoneRole.SUPPORT, box_opposite=110)
    ev = MarketEvent(LEVEL_REJECTED, 1, 0, 1, {"zone": z, "ref": 100, "invalid": 98})
    p = RouterParams(whitelist=WHITELIST_BALANCE, reject_use_struct_stop=True,
                     reject_target_mode="opposite")
    args = ("X", (0, 104, 106, 103, 105, 1), 1,
            StructureView(Regime.RANGE, (z,), 1), 1, [ev])
    assert len(MarketRouter(p).on_bar(*args, position_open=False, entry_resting=False)) == 1
    assert MarketRouter(replace(p, bounded_min_rr=1)).on_bar(
        *args, position_open=False, entry_resting=False) == []