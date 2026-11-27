"""ORIGIN CHoCH 公共判定的语义契约(4H 认知收盘与增量状态维护共用唯一入口)。"""

from __future__ import annotations

from market_operator.events import (
    STRUCTURE_BROKEN,
    EventDetector,
    MarketEvent,
    origin_choch_events,
)
from market_operator.level import Level, LevelKind, LevelRole, LevelState
from market_operator.structure import MarketStructure, Params


def _level(*, role: LevelRole, kind: LevelKind, extreme: float, uid: int = 1) -> Level:
    return Level(
        uid=uid,
        role=role,
        kind=kind,
        extreme=extreme,
        neck=extreme,
        state=LevelState.FRESH,
        origin_index=0,
        known_index=0,
        origin_ts=0,
        last_change_known=0,
    )


def test_pure_function_filters_non_origin_and_non_flipped() -> None:
    origin = _level(role=LevelRole.RESISTANCE, kind=LevelKind.ORIGIN, extreme=100.0)
    swing = _level(role=LevelRole.SUPPORT, kind=LevelKind.SWING, extreme=90.0, uid=2)

    events = origin_choch_events(
        [
            ("FLIPPED", swing.clone(), swing.clone()),   # 非 ORIGIN 转移
            ("TESTED", origin.clone(), origin.clone()),  # 非 FLIPPED 转移
            ("FLIPPED", origin.clone(), origin.clone()),  # 唯一命中
        ],
        ts=123,
        known_index=7,
        high=105.0,
        low=95.0,
    )

    assert len(events) == 1
    ev = events[0]
    assert ev.id == STRUCTURE_BROKEN
    assert ev.direction == 1
    assert ev.ts == 123
    assert ev.known_index == 7
    assert ev.attrs["ref"] == 100.0
    assert ev.attrs["ex"] == 105.0
    assert ev.attrs["sub_kind"] == "ORIGIN_CHOCH"
    assert ev.attrs["zone"].uid == origin.uid
    assert ev.attrs["level"].uid == origin.uid
    assert ev.attrs["zone"] is not ev.attrs["level"]


def test_pure_function_direction_and_ex_follow_pre_transition_role() -> None:
    resistance = _level(role=LevelRole.RESISTANCE, kind=LevelKind.ORIGIN, extreme=100.0)
    support = _level(role=LevelRole.SUPPORT, kind=LevelKind.ORIGIN, extreme=80.0, uid=2)

    events = origin_choch_events(
        [
            ("FLIPPED", resistance.clone(), resistance.clone()),
            ("FLIPPED", support.clone(), support.clone()),
        ],
        ts=0,
        known_index=0,
        high=110.0,
        low=70.0,
    )

    assert [ev.direction for ev in events] == [1, -1]
    assert [ev.attrs["ex"] for ev in events] == [110.0, 70.0]
    assert [ev.attrs["ref"] for ev in events] == [100.0, 80.0]
    assert all(isinstance(ev, MarketEvent) for ev in events)


def test_detector_update_still_emits_identical_choch_contract(monkeypatch) -> None:
    """回归:认知收盘经公共函数发出的事件与抽取前逐字段一致。"""
    origin = _level(role=LevelRole.SUPPORT, kind=LevelKind.ORIGIN, extreme=80.0)
    detector = EventDetector()
    structure = detector.structure

    monkeypatch.setattr(structure, "last_transitions", [
        ("FLIPPED", origin.clone(), origin.clone()),
    ])
    monkeypatch.setattr(
        structure._scanner,
        "update_canvas",
        lambda bars, atr: [("FLIPPED", origin.clone(), origin.clone())],
    )

    bar = (1000, 90.0, 110.0, 70.0, 100.0, 1.0)
    events = detector.update(bar)
    choch = [ev for ev in events if ev.id == STRUCTURE_BROKEN]

    assert len(choch) == 1
    ev = choch[0]
    assert ev.direction == -1
    assert ev.known_index == 0
    assert ev.ts == 1000
    assert ev.attrs["ex"] == 70.0
    assert ev.attrs["ref"] == 80.0
    assert ev.attrs["sub_kind"] == "ORIGIN_CHOCH"


def test_dominant_choch_and_fast_sweep_and_trigger_defense_and_pyramid() -> None:
    """验证本次合入的四大机制契约(级联主导位破坏、极速清扫收回、短线对侧快出、风控金字塔加仓)。"""
    from types import SimpleNamespace
    from market_operator import campaign
    from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind
    from market_operator.events import (
        BREAKOUT_ACCEPTED,
        LEVEL_REJECTED,
        LevelAuctionEngine,
        dominant_choch_events,
    )
    from market_operator.hypothesis import StructureView
    from market_operator.position import PositionManager
    from market_operator.routing import MarketRouter, RouterParams
    from market_operator.structure import Regime

    dom_sup = Level(1, LevelRole.SUPPORT, LevelKind.EQUAL, 98.0, 99.0, LevelState.TESTED, 0, 1, 0, 1, touches=2, prominence_2d=0.4)
    weak_sup = Level(2, LevelRole.SUPPORT, LevelKind.SWING, 97.0, 97.5, LevelState.FRESH, 0, 1, 0, 1, touches=1, prominence_2d=0.05)
    other_res = Level(3, LevelRole.RESISTANCE, LevelKind.ORIGIN, 110.0, 109.0, LevelState.FRESH, 0, 1, 0, 1, touches=1, prominence_2d=0.6)
    evs = dominant_choch_events(
        (dom_sup, weak_sup, other_res), prev_close=98.5,
        bar=(2000, 98.5, 98.6, 96.5, 96.8, 10.0), known_index=5,
    )
    assert len(evs) == 1 and evs[0].id == STRUCTURE_BROKEN and evs[0].direction == -1
    assert evs[0].attrs["sub_kind"] == "DOMINANT_CHOCH"

    eng = LevelAuctionEngine()
    sup = Level(10, LevelRole.SUPPORT, LevelKind.ORIGIN, 100.0, 100.5, LevelState.FRESH, 0, 1, 0, 1, departed=True, prominence_2d=0.8)
    prev_b = (1000, 102.0, 103.5, 100.2, 100.3, 10.0)
    assert eng.step(bar_index=2, bar=(2000, 100.3, 102.0, 98.0, 99.8, 10.0), prev_bar=prev_b, recent_bars=[prev_b], active_levels=[sup], atr=1.0, k_bm=0.1) == []
    fired = LevelAuctionEngine().step(bar_index=2, bar=(2000, 100.3, 102.0, 98.0, 101.2, 10.0), prev_bar=prev_b, recent_bars=[prev_b], active_levels=[sup], atr=1.0, k_bm=0.1)
    assert len(fired) == 1 and fired[0].id == LEVEL_REJECTED and fired[0].attrs["sub_kind"] == "SWEEP_TRAP"

    pm = PositionManager()
    pm.open("BTCUSDT", 1, 100.0, 95.0, 115.0, LEVEL_REJECTED, 1000, 0)
    opp_rej = MarketEvent(LEVEL_REJECTED, -1, 2000, 0, {})
    assert pm.on_trigger_bar("BTCUSDT", (2000, 102.0, 103.0, 101.0, 101.5, 1.0), [opp_rej], tf_label="4h") == []
    assert pm.on_trigger_bar("BTCUSDT", (2000, 102.0, 103.0, 101.0, 101.5, 1.0), [opp_rej], tf_label="1h") == []
    pm_fast = PositionManager(ltf_opposing_reject_exit=True)
    pm_fast.open("BTCUSDT", 1, 100.0, 95.0, 115.0, LEVEL_REJECTED, 1000, 0)
    ex_fast = pm_fast.on_trigger_bar("BTCUSDT", (2000, 102.0, 103.0, 101.0, 101.5, 1.0), [opp_rej])
    assert len(ex_fast) == 1 and "LTF_OPPOSING_REJECT" in ex_fast[0].reason

    # 策略路由层彻底脱离成交额(零成交量不变性): 1h 上 dollar_volume=0 与 1e9 产出完全一致
    router_1h = MarketRouter(RouterParams(whitelist=frozenset({(LEVEL_REJECTED, 1)})))
    vw_zero_dv = StructureView(Regime.RANGE, (sup, other_res), atr=1.0, tf_hours=1, dollar_volume=0.0)
    ev_long = MarketEvent(LEVEL_REJECTED, 1, 2000, 1, {"level": sup, "zone": sup, "invalid": 98.0, "ex": 98.5, "swept": True})
    assert len(router_1h.on_bar("FXPAIR", (2000, 100.0, 101.5, 98.5, 101.0, 0.0), 1, vw_zero_dv, 1.0, [ev_long], universe_size=10, median_dollar_volume=1000.0)) == 1
    assert router_1h.counters.get("rejected_low_liquidity", 0) == 0

    specs = {"BTCUSDT@4h": ContractSpec("BTCUSDT@4h", 0.001, 0.001, 3, 2, 5.0)}
    b = Broker(1000.0, risk_frac=0.02, leverage_cap=20.0, taker_fee=0.0, slippage_per_side=0.0, funding_per_8h=0.0, contracts=specs, portfolio_stop_risk_frac=0.20)
    b.submit(OrderIntent(OrderKind.OPEN, "BTCUSDT@4h", None, "base", 100.0, 0), 0, direction=1, invalid=90.0, target_1=140.0)
    b.on_bar_open("BTCUSDT@4h", 3_600_000, 100.0)
    base_qty = b.position_of("BTCUSDT@4h").qty
    pm4 = PositionManager(allow_add=True)
    pm4.open("BTCUSDT", 1, 100.0, 90.0, 140.0, BREAKOUT_ACCEPTED, 3_600_000, 0)
    st = SimpleNamespace(frame=SimpleNamespace(key="BTCUSDT@4h", symbol="BTCUSDT", tf_hours=4), pos_mgr=pm4)
    counters: dict = {}

    bad_dec = SimpleNamespace(kind="market", intent=OrderIntent(OrderKind.OPEN, "BTCUSDT", None, "add_bad", 108.0, 7_200_000), hyp=SimpleNamespace(direction=1, invalid=98.0, target_1=145.0, event_id=BREAKOUT_ACCEPTED), meta={})
    campaign._submit_decisions(st, b, [bad_dec], 7_200_000, 7_200_000, 7_200_000, {}, counters, [], 12, cancel_moment=10_800_000)
    assert counters.get("pyramid_gate_skips") == 1

    ok_dec = SimpleNamespace(kind="market", intent=OrderIntent(OrderKind.OPEN, "BTCUSDT", None, "add_ok", 110.0, 7_200_000), hyp=SimpleNamespace(direction=1, invalid=101.0, target_1=145.0, event_id=BREAKOUT_ACCEPTED), meta={})
    campaign._submit_decisions(st, b, [ok_dec], 7_200_000, 7_200_000, 7_200_000, {}, counters, [], 12, cancel_moment=10_800_000)
    assert counters.get("pyramid_add_intents") == 1
    rows = b.on_bar_open("BTCUSDT@4h", 10_800_000, 110.0)
    assert rows[0]["kind"] == "ADD" and 0 < rows[0]["qty"] <= 0.5 * base_qty + 1e-9
    assert b.position_of("BTCUSDT@4h").adds_done == 1
    assert campaign._symbol_position_open(b, "BTCUSDT@4h", allow_add=True) is True

