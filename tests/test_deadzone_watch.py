"""死区系统价格关注 (WatchEntry: F1 钳制内沿 + 差 25% 企稳补票 + 走完 65% 作废 + 4h 短窗) 单元与实盘轮询测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_runner as lr
from market_operator.broker import Broker, ContractSpec, RestingLimitOrder
from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.replay import MS_1H
from market_operator.routing import (
    ROUTE_BREAKOUT_ACCEPTED,
    ROUTE_BREAKOUT_ACCEPTED_LIMIT,
    ROUTE_LEVEL_REJECTED,
    ROUTE_LEVEL_REJECTED_LIMIT,
    WHITELIST_BALANCE,
    MarketRouter,
    RouterParams,
)
from market_operator.structure import Regime, Zone, ZoneRole, ZoneState

T0 = 1_750_000_000_000 - (1_750_000_000_000 % 86_400_000)


def _specs(*symbols):
    return {
        s: ContractSpec(symbol=s, size_min=0.001, size_step=0.001,
                        size_place=3, price_place=2, min_trade_usdt=5.0)
        for s in symbols
    }


def _broker(**kwargs):
    return Broker(
        equity=1000.0, risk_frac=0.01, leverage_cap=20.0,
        taker_fee=0.0006, maker_fee=0.0002, slippage_per_side=0.0002,
        funding_per_8h=0.0, contracts=_specs("BTCUSDT@2h"),
        **kwargs,
    )


def test_router_market_only_no_deadzone_watch():
    """路由层物理移除 WatchEntry 后,平衡箱拒绝统一走 market 入场(无 deadzone_watch 挂单)。"""
    z = Zone(
        lo=99.0, hi=99.6, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=3, origin_known=1, last_change_known=5,
        box_opposite=105.0, box_quality=0.6, box_width_atr=3.0, box_rotations=3,
    )
    view = StructureView(regime=Regime.RANGE, zones=(z,), atr=2.0)
    ev = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=T0, known_index=10,
        attrs={"zone": z, "ref": 99.6, "invalid": 98.8},
    )

    r = MarketRouter(RouterParams(
        whitelist=WHITELIST_BALANCE, reject_use_struct_stop=True,
        reject_target_mode="opposite",
    ))
    d = r.on_bar("BTCUSDT", (T0, 100.0, 102.0, 99.2, 101.6, 10.0),
                 10, view, 2.0, [ev], position_open=False, entry_resting=False)
    assert len(d) == 1 and d[0].kind == "market" and d[0].route == ROUTE_LEVEL_REJECTED
    assert not d[0].meta.get("watch_entry")

    d_high_r = r.on_bar("BTCUSDT", (T0 + MS_1H, 100.0, 101.0, 99.2, 100.8, 10.0),
                        11, view, 2.0, [ev], position_open=False, entry_resting=False)
    assert len(d_high_r) == 1 and d_high_r[0].kind == "market" and d_high_r[0].route == ROUTE_LEVEL_REJECTED


def test_broker_watch_entry_near_bounce_played_out_and_4h_timeout():
    """验证 Broker 细帧上的三项 WatchEntry 算子:① 差25%企稳补票;② 走完65%作废;③ 4h超时。"""
    def _arm(b: Broker):
        b.submit_limit(RestingLimitOrder(
            direction=1, limit_price=100.0, armed_at_ts=T0,
            expiry_ts=T0 + 4 * MS_1H, fill_after_ts=T0,
            symbol="BTCUSDT@2h", role="entry", invalid=98.0, target_1=110.0,
            event_id=LEVEL_REJECTED, reason="WATCH_ENTRY",
            meta={
                "direction": 1, "watch_entry": True, "watch_price": 100.0,
                "near_price": 100.5, "confirm_close": 102.0,
                "max_prog_frac": 0.65, "max_wait_hours": 4,
            },
        ))

    b1 = _broker()
    _arm(b1)
    rows_trig = b1.on_15m_bar("BTCUSDT@2h", T0 + MS_1H, 100.4, 101.2, 100.2, 101.0)
    assert len(rows_trig) == 1 and rows_trig[0]["kind"] == "TRIGGER"
    rows_open = b1.on_bar_open("BTCUSDT@2h", T0 + 2 * MS_1H, 101.0)
    assert len(rows_open) == 1 and rows_open[0]["kind"] == "OPEN"
    assert "BTCUSDT@2h" in b1.positions

    b2 = _broker()
    _arm(b2)
    rows_cancel = b2.on_15m_bar("BTCUSDT@2h", T0 + MS_1H, 102.0, 107.5, 101.8, 107.0)
    assert len(rows_cancel) == 1
    assert rows_cancel[0]["kind"] == "CANCEL" and rows_cancel[0]["reason_code"] == "MOVE_ALREADY_PLAYED_OUT"
    assert not b2.resting.get("BTCUSDT@2h")
    assert b2.on_15m_bar("BTCUSDT@2h", T0 + 2 * MS_1H, 107.0, 107.0, 99.0, 99.5) == []

    b3 = _broker()
    _arm(b3)
    for k in range(1, 5):
        assert b3.on_15m_bar("BTCUSDT@2h", T0 + k * MS_1H, 102.0, 103.0, 101.0, 102.2) == []
    rows_exp = b3.on_15m_bar("BTCUSDT@2h", T0 + 5 * MS_1H, 102.0, 102.5, 99.5, 100.0)
    assert len(rows_exp) == 1 and rows_exp[0]["kind"] == "EXPIRED"



def test_live_runner_15s_watch_polling_bounce_and_played_out(tmp_path):
    """验证实盘 LiveRunner 15s 轮询:不挂交易所限价单、近沿反抽补票触发市价单、走完65%立即注销。"""
    cfg = json.loads((ROOT / "config" / "live.json").read_text("utf-8"))
    step2 = 7_200_000
    bars = [(T0 + k * step2, 100.0, 102.0, 99.0, 101.0, 10.0) for k in range(396)]

    class WatchMockClient:
        def __init__(self):
            self.ticker_px = 102.0
            self.limit_orders = []
            self.market_orders = []
            self.tpsl_orders = []

        def history_candles(self, symbol, granularity="2H", *, start_ms=None, end_ms=None, limit=200):
            if start_ms is not None:
                return [b for b in bars if b[0] >= start_ms][:limit]
            out = [b for b in bars if end_ms is None or b[0] < end_ms]
            return out[-limit:]

        def universe(self, min_vol):
            return ["BTCUSDT"]

        def balance(self):
            return {"equity": 1000.0, "available": 1000.0, "unrealized_pl": 0.0}

        def set_leverage(self, symbol, lev, hold_side=None):
            return {}

        def tickers(self):
            return [{"symbol": "BTCUSDT", "lastPr": str(self.ticker_px),
                     "bidPr": str(self.ticker_px), "askPr": str(self.ticker_px)}]

        def place_limit_order(self, *a, **kw):
            self.limit_orders.append((a, kw))
            return {"orderId": "lim1"}

        def place_market_order(self, symbol, *, side, size, client_oid, trade_side=None):
            self.market_orders.append((symbol, side, size, client_oid, trade_side))
            return {"orderId": f"mkt_{len(self.market_orders)}"}

        def recent_fills(self, symbol=None, limit=50):
            return []

        def tpsl_orders_pending(self, symbol=None):
            return self.tpsl_orders

        def place_stop_loss(self, symbol, *, trigger_price, hold_side, client_oid, trigger_type="mark_price"):
            oid = f"sl_{len(self.tpsl_orders) + 1}"
            self.tpsl_orders.append({"orderId": oid, "symbol": symbol,
                                     "clientOid": client_oid, "triggerPrice": str(trigger_price)})
            return {"orderId": oid}

        def positions(self):
            return []

    class DummyTg:
        def __getattr__(self, name):
            return lambda *a, **k: True

    client = WatchMockClient()
    runner = lr.LiveRunner(
        "live", cfg, client, DummyTg(),
        equity0=1000.0,
        state_path=tmp_path / "state.json",
        journal_dir=tmp_path / "logs",
        symbols=("BTCUSDT",),
        universe_enabled=False,
    )
    t_now = bars[-1][0] + 1000
    runner.step(t_now)

    runner.open_intents["mo21watch1"] = {
        "oid": "mo21watch1", "symbol": "BTCUSDT", "tf_label": "2h",
        "role": "entry", "kind": "limit", "direction": 1, "qty": 5.0,
        "limit_price": 100.0, "ref_px": 100.0, "invalid": 98.0, "target_1": 110.0,
        "event_id": LEVEL_REJECTED, "route": ROUTE_LEVEL_REJECTED_LIMIT,
        "watch_entry": True, "watch_price": 100.0, "near_price": 100.5,
        "confirm_close": 102.0, "max_prog_frac": 0.65, "max_wait_hours": 4,
        "near_touched": False, "near_extreme": None, "risk_usdt_cap": 10.0,
        "decision_ts": t_now, "mic": False, "expiry_ts": t_now + 4 * MS_1H,
        "status": "armed", "exchange_order_id": None,
    }
    assert client.limit_orders == []

    # 第 1 拍 (15s 后): 回踩至 100.3 (进入 near_price=100.5 近沿带, 尚未反抽) ⇒ 只置位 near_touched
    client.ticker_px = 100.3
    runner._poll_watch_entries(t_now + 15_000)
    assert runner.open_intents["mo21watch1"]["near_touched"] is True
    assert client.market_orders == []

    # 第 2 拍 (30s 后): 反抽至 100.6 (> 100.5, 差一点没摸到 100.0 但已企稳掉头) ⇒ 立即市价补票开仓!
    client.ticker_px = 100.6
    runner._poll_watch_entries(t_now + 30_000)
    assert len(client.market_orders) == 1
    assert runner.open_intents["mo21watch1"]["status"] == "filled"
    assert runner.open_intents["mo21watch1"]["watch_trigger"] == "WATCH_NEAR_BOUNCE"
    assert "BTCUSDT" in runner.positions
    assert runner.positions["BTCUSDT"]["qty"] <= 10.0 / 2.6 + 1e-6

    # 场景 2: 价格先冲到 107.5 (>= 107.2 走完 65% 行程) ⇒ 立即作废, 之后跌回 99.9 绝不下单!
    runner.positions.clear()
    runner.open_intents["mo21watch2"] = {
        **runner.open_intents["mo21watch1"],
        "oid": "mo21watch2", "kind": "limit", "qty": 5.0,
        "near_touched": False, "near_extreme": None, "status": "armed",
    }
    client.ticker_px = 107.5
    runner._poll_watch_entries(t_now + 45_000)
    assert runner.open_intents["mo21watch2"]["status"] == "canceled"
    assert runner.counters.get("watch_canceled_played_out") == 1
    client.ticker_px = 99.9
    runner._poll_watch_entries(t_now + 60_000)
    assert len(client.market_orders) == 1


def test_router_accept_tight_market_and_overextended_restraint():
    """紧致突破直接市价;过伸突破(A2_OVEREXTENDED)直接放弃,不再挂 accept_watch 限价单。"""
    z_res = Zone(
        lo=104.0, hi=105.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
        touches=3, origin_known=1, last_change_known=5,
        box_opposite=99.0, box_quality=0.6, box_width_atr=3.0, box_rotations=3,
    )
    z_sup = Zone(
        lo=99.0, hi=100.0, role=ZoneRole.SUPPORT, state=ZoneState.ACTIVE,
        touches=3, origin_known=1, last_change_known=5,
        box_opposite=105.0, box_quality=0.6, box_width_atr=3.0, box_rotations=3,
    )
    view = StructureView(regime=Regime.RANGE, zones=(z_res, z_sup), atr=2.0)
    r = MarketRouter(RouterParams(whitelist=WHITELIST_BALANCE))

    ev_tight = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=T0, known_index=10,
        attrs={"zone": z_res, "ref": 105.0, "invalid": 103.8, "sub_kind": "A1_TIGHT"},
    )
    d_tight = r.on_bar("BTCUSDT", (T0, 105.0, 106.4, 104.8, 106.2, 10.0),
                       10, view, 2.0, [ev_tight], position_open=False, entry_resting=False)
    assert len(d_tight) == 1 and d_tight[0].kind == "market" and d_tight[0].route == ROUTE_BREAKOUT_ACCEPTED

    ev_up = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=T0 + MS_1H, known_index=11,
        attrs={"zone": z_res, "ref": 105.0, "invalid": 103.8, "sub_kind": "A2_OVEREXTENDED"},
    )
    d_up = r.on_bar("BTCUSDT", (T0 + MS_1H, 105.5, 107.2, 105.4, 107.0, 10.0),
                    11, view, 2.0, [ev_up], position_open=False, entry_resting=False)
    assert d_up == []
    assert r.counters["restraint_a2_overextended_reject"] == 1

    ev_dn = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=-1, ts=T0 + 2 * MS_1H, known_index=12,
        attrs={"zone": z_sup, "ref": 99.0, "invalid": 100.2, "sub_kind": "A2_OVEREXTENDED"},
    )
    d_dn = r.on_bar("BTCUSDT", (T0 + 2 * MS_1H, 98.5, 98.6, 96.8, 97.0, 10.0),
                    12, view, 2.0, [ev_dn], position_open=False, entry_resting=False)
    assert d_dn == []
    assert r.counters["restraint_a2_overextended_reject"] == 2


def test_broker_accept_watch_without_target1_prog_price_and_short_bounce():
    """无 target_1 的突破 WatchEntry(空头跌破下沿):回抽近沿掉头补票 vs 直接下杀越过 prog_price 作废。"""
    def _arm_short(b: Broker):
        b.submit_limit(RestingLimitOrder(
            direction=-1, limit_price=99.0, armed_at_ts=T0,
            expiry_ts=T0 + 4 * MS_1H, fill_after_ts=T0,
            symbol="BTCUSDT@2h", role="entry", invalid=103.0, target_1=None,
            event_id=BREAKOUT_ACCEPTED, reason="WATCH_ENTRY_SHORT",
            meta={
                "direction": -1, "watch_entry": True, "watch_price": 99.0,
                "near_price": 98.5, "prog_price": 94.0, "confirm_close": 97.0,
                "max_prog_frac": None, "max_wait_hours": 4,
            },
        ))

    # 空头回抽至 98.7 (>=98.5, <99.0) 且阴线收在 98.2 (<=98.5) ⇒ 触发补票做空
    b1 = _broker()
    _arm_short(b1)
    rows_trig = b1.on_15m_bar("BTCUSDT@2h", T0 + MS_1H, 98.6, 98.8, 98.0, 98.2)
    assert len(rows_trig) == 1 and rows_trig[0]["kind"] == "TRIGGER"
    rows_open = b1.on_bar_open("BTCUSDT@2h", T0 + 2 * MS_1H, 98.2)
    assert len(rows_open) == 1 and rows_open[0]["kind"] == "OPEN"

    # 空头未回抽先下杀至 93.8 (<= prog_price 94.0) ⇒ 判 MOVE_ALREADY_PLAYED_OUT 作废
    b2 = _broker()
    _arm_short(b2)
    rows_cancel = b2.on_15m_bar("BTCUSDT@2h", T0 + MS_1H, 97.0, 97.2, 93.8, 94.2)
    assert len(rows_cancel) == 1
    assert rows_cancel[0]["kind"] == "CANCEL" and rows_cancel[0]["reason_code"] == "MOVE_ALREADY_PLAYED_OUT"


def test_box_reentry_allows_flipped_edge_accept_and_blocks_flipped_reject():
    """突破进入平衡箱(box_reentry_enabled):FLIPPED_BOX_EDGE 允许触发 BREAKOUT_ACCEPTED 重返箱内,但不发迁移期 LEVEL_REJECTED;且 live.json 已移除 0.35 权重补丁。"""
    from market_operator.events import EventDetector
    from market_operator.structure import Params

    p = Params(use_balance_detector=True, box_reentry_enabled=True, n_a=2, n_f=3, k_bm=0.1, k_rj=0.3)
    det = EventDetector(p)
    # 推进若干基础 bar 使 ATR 就绪
    for k in range(15):
        det.update((T0 + k * MS_1H, 100.0, 101.0, 99.0, 100.0, 10.0))

    # 手工植入一条来自 BROKEN_DOWN 下沿翻转成的 RESISTANCE (FLIPPED_BOX_EDGE): [99.0, 100.0], 对侧上沿 106.0
    det.structure._bal_det = None
    z_flip = Zone(
        lo=99.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
        touches=3, origin_known=5, last_change_known=14,
        box_opposite=106.0, box_quality=0.6, box_width_atr=3.5, box_rotations=3,
        source_kind="FLIPPED_BOX_EDGE",
    )
    det.structure._zones.zones = [z_flip]

    # 1) 在箱外下方触碰翻转沿 [99.0, 100.0] 并回落 ⇒ 因 source_kind == FLIPPED_BOX_EDGE, 不得发射迁移期 LEVEL_REJECTED
    evs_touch = det.update((T0 + 15 * MS_1H, 98.0, 99.8, 97.5, 98.2, 10.0))
    assert not any(e.id == LEVEL_REJECTED for e in evs_touch)

    # 2) 从下方连续 n_a=2 根强阳突破翻转沿重返箱内 ⇒ 正常发射 BREAKOUT_ACCEPTED (突破进入平衡箱做多)!
    det.update((T0 + 16 * MS_1H, 98.5, 100.6, 98.4, 100.4, 10.0))
    evs_reenter = det.update((T0 + 17 * MS_1H, 100.3, 101.2, 100.2, 101.0, 10.0))
    acc = [e for e in evs_reenter if e.id == BREAKOUT_ACCEPTED]
    assert len(acc) == 1 and acc[0].direction == 1

    # 3) 校验生产 live.json 已彻底清空 0.35 方向掩蔽权重
    live_cfg = json.loads((ROOT / "config" / "live.json").read_text("utf-8"))
    assert live_cfg["risk"]["edge_size_by_event_direction"] == {}


