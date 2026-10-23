"""v21 极简平衡箱(BalanceDetector) + 雙原语(ACCEPT/REJECT) + 高阶降档混合触发测试。"""

from __future__ import annotations

import json
from pathlib import Path

from market_operator.events import (
    ACCEPT, E1, E3, REJECT, EventDetector, MarketEvent, canonical_event_id,
)
from market_operator.hypothesis import StructureView
from market_operator.strategy_v6 import (
    ROUTE_ACCEPT, ROUTE_E1, ROUTE_E3, ROUTE_REJECT, WHITELIST_BALANCE,
    V6Params, V6Router,
)
from market_operator.structure import (
    MarketStructure, Params, Regime, Zone, ZoneRole, ZoneState,
)


def test_accept_reject_aliases_and_routing():
    assert canonical_event_id(ACCEPT) == E1
    assert canonical_event_id(REJECT) == E3
    assert canonical_event_id("E1") == E1
    assert canonical_event_id("E3") == E3
    assert ROUTE_ACCEPT == ROUTE_E1
    assert ROUTE_REJECT == ROUTE_E3

    wl = frozenset({(ACCEPT, 1), (ACCEPT, -1), (REJECT, 1), (REJECT, -1)})
    router = V6Router(V6Params(whitelist=wl))
    z = Zone(lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
             touches=3, origin_known=1, last_change_known=5,
             box_opposite=90.0, box_quality=0.55, box_width_atr=5.0, box_rotations=3)
    view = StructureView(regime=Regime.RANGE, zones=(z,), atr=2.0)

    # 1. ACCEPT (+1) 事件触发市价开多
    ev_acc = MarketEvent(id=E1, direction=1, ts=1000, known_index=10,
                         attrs={"zone": z, "ref": 100.0, "invalid": 97.8})
    decs = router.on_bar("BTCUSDT", (1000, 100.0, 101.5, 99.5, 101.0, 1.0),
                         10, view, 2.0, [ev_acc],
                         position_open=False, entry_resting=False)
    assert len(decs) == 1
    assert decs[0].route == ROUTE_ACCEPT
    assert decs[0].hyp.direction == 1

    # 2. REJECT (-1) 事件触发市价开空
    ev_rej = MarketEvent(id=E3, direction=-1, ts=2000, known_index=11,
                         attrs={"zone": z, "ref": 100.0, "invalid": 100.2})
    decs2 = router.on_bar("BTCUSDT", (2000, 100.0, 100.5, 98.5, 99.0, 1.0),
                          11, view, 2.0, [ev_rej],
                          position_open=False, entry_resting=False)
    assert len(decs2) == 1
    assert decs2[0].route == ROUTE_REJECT
    assert decs2[0].hyp.direction == -1


def test_restraint_gates_rotations_and_quality():
    wl = WHITELIST_BALANCE
    router = V6Router(V6Params(whitelist=wl, min_box_rotations=3, min_box_quality=0.50))
    z_weak = Zone(lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
                  touches=2, origin_known=1, last_change_known=5,
                  box_opposite=92.0, box_quality=0.45, box_width_atr=3.0, box_rotations=2)
    z_strong = Zone(lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
                    touches=4, origin_known=1, last_change_known=5,
                    box_opposite=92.0, box_quality=0.60, box_width_atr=4.0, box_rotations=3)
    view = StructureView(regime=Regime.RANGE, zones=(z_weak, z_strong), atr=2.0)

    ev1 = MarketEvent(id=E3, direction=-1, ts=1000, known_index=10,
                      attrs={"zone": z_weak, "ref": 100.0, "invalid": 100.2})
    assert router.on_bar("BTCUSDT", (1000, 100.0, 100.5, 98.5, 99.0, 1.0),
                         10, view, 2.0, [ev1],
                         position_open=False, entry_resting=False) == []

    ev2 = MarketEvent(id=E3, direction=-1, ts=2000, known_index=11,
                      attrs={"zone": z_strong, "ref": 100.0, "invalid": 100.2})
    decs = router.on_bar("BTCUSDT", (2000, 100.0, 100.5, 98.5, 99.0, 1.0),
                         11, view, 2.0, [ev2],
                         position_open=False, entry_resting=False)
    assert len(decs) == 1


def test_v21_config_valid_and_loads_balance_detector():
    root = Path(__file__).resolve().parents[1]
    cfg_path = root / "config" / "v21.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    p = Params.from_json(cfg_path)
    assert p.use_balance_detector is True
    assert p.balance_quality_min == 0.35
    assert raw["tf_list"] == ["2h", "4h", "12h", "1d"]
    # 4h 认知已下沉为 2h 触发(结构与 ATR 仍绑定 4h 认知帧),与 12h 同为 2h 触发
    assert raw["trigger_tf_by_htf"] == {"2h": None, "4h": 7200,
                                        "12h": 7200, "1d": 14400}
    ed = EventDetector(p)
    assert ed.structure._zones._bal_det is not None


def test_v21_per_tf_capacity_and_dynamic_risk_tiers():
    from market_operator.broker import Broker, ContractSpec
    from market_operator.position import PositionManager

    spec = ContractSpec(symbol="BTCUSDT", size_min=0.001, size_step=0.001,
                        size_place=3, price_place=1, min_trade_usdt=5.0)
    tiers = [(250.0, 0.04), (600.0, 0.03), (1500.0, 0.02), (1e9, 0.01)]
    broker = Broker(
        equity=104.0,
        risk_frac=0.0075,
        leverage_cap=20.0,
        taker_fee=0.0006,
        slippage_per_side=0.0002,
        funding_per_8h=0.0001,
        contracts={"BTCUSDT@2h": spec, "BTCUSDT@12h": spec},
        tf_stop_risk_frac_by_tf={"2h": 0.12, "12h": 0.12},
        dynamic_risk_tiers=tiers,
    )
    assert broker.current_risk_frac() == 0.04
    broker.cash = 300.0
    assert broker.current_risk_frac() == 0.03
    broker.cash = 800.0
    assert broker.current_risk_frac() == 0.02
    broker.cash = 2000.0
    assert broker.current_risk_frac() == 0.01

    # 验证 REJECT 对侧突破锁利 50% (reject_opp_lock_frac = -0.50)
    pm = PositionManager(
        Params(),
        allow_add=False,
        breakeven_on_reduce=True,
        time_stop=False,
        reject_opp_lock_frac=-0.50,
    )
    pos = pm.open("BTCUSDT", -1, 100.0, 105.0, 90.0, E3, 1000, 0)
    # 1. 仅极值触及 90.0 但收盘 92.0 未突破对侧 -> 不锁利、不减仓,原止损 105.0 不动
    intents1 = pm.on_bar("BTCUSDT", (2000, 98.0, 99.0, 89.5, 92.0, 1.0), 2.0)
    assert intents1 == []
    assert pos.invalid == 105.0
    assert pos.reduced is False
    # 2. 收盘 89.0 真正突破对侧 90.0 -> 不减仓,但止损直接锁利 50% 箱体跨度至 95.0
    intents2 = pm.on_bar("BTCUSDT", (3000, 92.0, 93.0, 88.5, 89.0, 1.0), 2.0)
    assert intents2 == []
    assert pos.invalid == 95.0
    assert pos.reduced is True


def test_dual_bot_telegram_and_bw_kline_chart():
    from market_operator.tg import TelegramNotifier, render_bw_kline_png

    bars = [
        (1700000000000 + i * 7200000, 100.0 + i * 0.5, 102.0 + i * 0.5,
         99.0 + i * 0.5, 101.0 + i * 0.5, 10.0)
        for i in range(30)
    ]
    png = render_bw_kline_png(
        bars, symbol="BTCUSDT", tf_label="12h", title_tag="SIGNAL REJECT SHORT",
        box_lo=98.0, box_hi=112.0, entry_price=111.5, stop_price=114.0,
        target_price=98.0, direction=-1
    )
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
    png_lin = render_bw_kline_png(
        bars, symbol="BTCUSDT", tf_label="12h", title_tag="SIGNAL REJECT SHORT",
        box_lo=98.0, box_hi=112.0, entry_price=111.5, stop_price=114.0,
        target_price=98.0, direction=-1, log_scale=False
    )
    assert png_lin is not None and png_lin[:8] == b"\x89PNG\r\n\x1a\n"
    assert png != png_lin

    calls = []

    class DummyResp:
        status_code = 200
        def json(self):
            return {"ok": True}

    class DummySession:
        proxies = {}
        def post(self, url, data=None, files=None, timeout=None):
            calls.append((url, data, files))
            return DummyResp()

    tg = TelegramNotifier({
        "TELEGRAM_BOT_TOKEN": "SIG_TOK",
        "TELEGRAM_CHAT_ID": "SIG_CHAT",
        "TELEGRAM_TRADE_BOT_TOKEN": "TRD_TOK",
        "TELEGRAM_TRADE_CHAT_ID": "TRD_CHAT",
    }, session=DummySession(), async_mode=False)

    ok_sig = tg.send_signal(
        "BTCUSDT", "E3", "空", "e3_market", 111.5, 114.0,
        size_note="规模 0.5", regime="range", tf_label="12h",
        bars=bars, box_lo=98.0, box_hi=112.0, target_1=98.0
    )
    assert ok_sig is True
    assert len(calls) == 1
    assert "SIG_TOK/sendPhoto" in calls[0][0]
    assert calls[0][1]["chat_id"] == "SIG_CHAT"
    assert "photo" in calls[0][2]
    assert "REJECT (刺穿拒绝)" in calls[0][1]["caption"]
    assert "REJECT 市价" in calls[0][1]["caption"]
    # 每行一条关键信息 + Emoji 行首
    cap_lines = [ln for ln in calls[0][1]["caption"].splitlines() if not ln.startswith("【")]
    assert all(len(ln) > 0 for ln in cap_lines)

    ok_fill = tg.send_fill(
        "BTCUSDT", "开仓(live)", 0.5, 111.5, note="E3 e3_market",
        tf_label="12h", direction=-1, stop_price=114.0, target_price=98.0,
        box_lo=98.0, box_hi=112.0, bars=bars
    )
    assert ok_fill is True
    assert len(calls) == 2
    assert "TRD_TOK/sendPhoto" in calls[1][0]
    assert calls[1][1]["chat_id"] == "TRD_CHAT"
    assert "REJECT (刺穿拒绝) | 市价" in calls[1][1]["caption"]


def test_live_runner_v21_multi_tf_campaigns(tmp_path):
    """验证 LiveRunner 在 v21 引擎下自动装配 2h/4h/12h/1d 四周期并发实例及周期独立容量池。"""
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import live_runner as lr

    cfg = json.loads((root / "config" / "live.json").read_text("utf-8"))
    step2 = 7_200_000
    t0 = 1_790_000_000_000 - (1_790_000_000_000 % 86_400_000)
    want = int(cfg["cognition"]["warmup_trigger_bars"])
    bars = [(t0 + k * step2, 100.0, 102.0, 99.0, 101.0, 10.0) for k in range(want)]

    class DummyClient:
        def history_candles(self, symbol, granularity="2H", *, start_ms=None, end_ms=None, limit=200):
            if start_ms is not None:
                return [b for b in bars if b[0] >= start_ms][:limit]
            out = [b for b in bars if end_ms is None or b[0] < end_ms]
            return out[-limit:]

        def universe(self, min_vol):
            return ["BTCUSDT"]

    class DummyTg:
        def __getattr__(self, name):
            return lambda *a, **k: True

    runner = lr.LiveRunner("paper", cfg, DummyClient(), DummyTg(),
                           equity0=104.0,
                           state_path=tmp_path / "state.json",
                           journal_dir=tmp_path / "logs",
                           symbols=("BTCUSDT",),
                           universe_enabled=False)
    runner.step(bars[-1][0] + 1)
    assert "BTCUSDT" in runner.campaigns
    assert runner.campaigns["BTCUSDT"].tf_label == "12h"
    assert ("BTCUSDT", "2h") in runner.extra_campaigns
    assert ("BTCUSDT", "4h") in runner.extra_campaigns
    assert ("BTCUSDT", "1d") in runner.extra_campaigns
    assert runner.current_risk_frac(104.0) == 0.04
    assert runner.current_risk_frac(300.0) == 0.03
    assert runner.current_risk_frac(800.0) == 0.02
    assert runner.current_risk_frac(2000.0) == 0.01


def test_live_runner_v21_adaptive_warmup_for_new_listings(tmp_path):
    """验证新上市币种无需 3600 根 2H:满 60 根(5天)开启 2h,满 125 根开启 4h,不足 60 根只告警一次不重复刷屏。"""
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import live_runner as lr

    cfg = json.loads((root / "config" / "live.json").read_text("utf-8"))
    step2 = 7_200_000
    t0 = 1_790_000_000_000 - (1_790_000_000_000 % 86_400_000)
    # NEW1: 130 根 2H (~10.8 天) -> 足够开启 2h(>=60) 和 4h(>=120),暂不足 12h(需360) 与 1d(需720)
    bars_new1 = [(t0 + k * step2, 100.0, 102.0, 99.0, 101.0, 10.0) for k in range(130)]
    # NEW2: 20 根 2H (~1.6 天) -> 连 2h 最低门槛(60根)都不够,应仅告警一次
    bars_new2 = [(t0 + k * step2, 50.0, 52.0, 49.0, 51.0, 10.0) for k in range(20)]
    by_sym = {"NEW1USDT": bars_new1, "NEW2USDT": bars_new2}

    class DummyClient:
        def history_candles(self, symbol, granularity="2H", *, start_ms=None, end_ms=None, limit=200):
            seq = by_sym.get(symbol, [])
            if start_ms is not None:
                return [b for b in seq if b[0] >= start_ms][:limit]
            out = [b for b in seq if end_ms is None or b[0] < end_ms]
            return out[-limit:]

        def universe(self, min_vol):
            return ["NEW1USDT", "NEW2USDT"]

    alerts = []

    class DummyTg:
        def send_alert(self, msg):
            alerts.append(msg)
            return True

        def __getattr__(self, name):
            return lambda *a, **k: True

    runner = lr.LiveRunner("paper", cfg, DummyClient(), DummyTg(),
                           equity0=104.0,
                           state_path=tmp_path / "state.json",
                           journal_dir=tmp_path / "logs",
                           symbols=("NEW1USDT", "NEW2USDT"),
                           universe_enabled=False)
    runner.step(bars_new1[-1][0] + 1)

    # NEW1USDT 成功准入:主实例自动降级为 2h,extra_campaigns 挂载 4h,不挂载 12h/1d
    assert "NEW1USDT" in runner.campaigns
    assert runner.campaigns["NEW1USDT"].tf_label == "2h"
    assert ("NEW1USDT", "4h") in runner.extra_campaigns
    assert ("NEW1USDT", "12h") not in runner.extra_campaigns
    assert ("NEW1USDT", "1d") not in runner.extra_campaigns

    # NEW2USDT 不足 60 根暂不入场,且重复加入 pending 再次 step 不会重复推送告警
    assert "NEW2USDT" not in runner.campaigns
    assert len([a for a in alerts if "NEW2USDT" in a]) == 1
    runner.symbols_pending.append("NEW2USDT")
    runner.step(bars_new1[-1][0] + 2)
    assert len([a for a in alerts if "NEW2USDT" in a]) == 1


def test_opp_lock_plan_vs_market_definition():
    """v21 锁利口径:计划口径(entry→target 行程) + 行情口径(现价 ∓giveback·R)。"""
    from market_operator.position import (
        fmt_px, lock_is_profitable, lock_is_protective, opp_lock_live_px,
        opp_lock_px, stop_protect_dist, tighten_lock,
    )

    # 计划口径与旧内联 `entry + frac*(target-entry)` 逐位等价
    assert opp_lock_px(100.0, 90.0, 0.50) == 95.0
    assert abs(opp_lock_px(324.02, 322.66, 0.50) - 323.34) < 1e-9
    assert opp_lock_px(100.0, 100.0, 0.50) is None      # 行程退化
    assert opp_lock_px(100.0, 90.0, 0.0) is None        # 关闭

    # 有向距离:> 0 才是保护侧(多=止损在现价下方 / 空=在上方)
    assert stop_protect_dist(-1, 323.34, 321.33) > 0
    assert stop_protect_dist(-1, 320.0, 321.33) < 0
    assert stop_protect_dist(1, 99.0, 100.0) > 0
    assert stop_protect_dist(1, 101.0, 100.0) < 0
    assert lock_is_protective(-1, 323.34, 321.33) is True
    assert lock_is_protective(-1, 320.0, 321.33) is False
    assert lock_is_protective(-1, None, 321.33) is False
    assert lock_is_profitable(-1, 99.0, 100.0) is True
    assert lock_is_profitable(-1, 101.0, 100.0) is False

    # 行情口径:锚在现价,只允许回吐 0.25R(GOOGLUSDT 实盘样本)
    risk = 326.88 - 324.02
    live = opp_lock_live_px(-1, 321.33, 0.25, risk)
    assert abs(live - 322.045) < 1e-9
    assert opp_lock_live_px(-1, 321.33, 0.0, risk) is None   # 关闭 ⇒ 纯计划口径

    # 合并:可用候选里取更贴近现价者
    assert abs(tighten_lock(-1, 323.34, live, 321.33, 324.02) - live) < 1e-9
    # 只给计划口径(行情口径关闭)⇒ 计划口径,逐位旧行为
    assert tighten_lock(-1, 323.34, None, 321.33, 324.02) == 323.34
    # 计划口径非法(现价盈利侧),行情口径可用 ⇒ 用行情口径,不一起丢掉
    # 多头 entry=100 / 现价 103 / 计划 99 在现价上方(非法);行情 103−0.5 = 102.5
    assert abs(tighten_lock(1, 99.0, 102.5, 103.0, 100.0) - 102.5) < 1e-9
    # 行情口径若落到 entry 亏损侧(97.7 < 100,只收紧亏损止损)不算锁利
    assert tighten_lock(1, 99.0, 97.7, 99.2, 100.0) is None
    # 两个都不可用 ⇒ None(调用方不推进)
    assert tighten_lock(1, 101.0, None, 99.2, 100.0) is None
    assert fmt_px(None) == "n/a"


def test_opp_lock_gate_and_market_anchor_in_on_bar():
    """§8.2 两条锁利分支:落位闸门拒绝错侧;行情锚定锁更多利润。"""
    from market_operator.position import PositionManager

    # ① 计划口径落到现价盈利侧(多头 target 在 entry 之下 ⇒ 行程反向)
    #    ⇒ 闸门拒绝推进,原结构止损不动、不置 reduced
    pm = PositionManager(Params(), allow_add=False, time_stop=False,
                         reject_opp_lock_frac=-0.50)
    pos = pm.open("XUSDT", 1, 112.0, 106.0, 110.0, E3, 1000, 0)
    assert pm.on_bar("XUSDT", (2000, 111.0, 111.2, 110.3, 110.8, 1.0), 2.0) == []
    assert pos.invalid == 106.0
    assert pos.reduced is False
    assert any("opp_break_lock_reject" in s for s in pos.log)

    # ② 行情锚定:空头 entry=324.02 invalid=326.88(R=2.86) target=322.66,
    #    2h 收盘 321.33 ⇒ 计划 323.34 落在现价身后 0.65R;
    #    giveback=0.25R ⇒ 锁利 321.33+0.715 = 322.045(实锁 0.69R)
    pm2 = PositionManager(Params(), allow_add=False, time_stop=False,
                          reject_opp_lock_frac=-0.50,
                          opp_lock_max_giveback_r=0.25)
    pos2 = pm2.open("XUSDT", -1, 324.02, 326.88, 322.66, E3, 1000, 0)
    assert pm2.on_bar("XUSDT", (2000, 323.5, 323.9, 321.0, 321.33, 1.0), 2.0) == []
    assert abs(pos2.invalid - 322.045) < 1e-9
    assert pos2.reduced is True
    assert any("行情=322.045" in s for s in pos2.log)

    # ③ giveback=0 ⇒ 纯计划口径(逐位旧行为):323.34
    pm3 = PositionManager(Params(), allow_add=False, time_stop=False,
                          reject_opp_lock_frac=-0.50,
                          opp_lock_max_giveback_r=0.0)
    pos3 = pm3.open("XUSDT", -1, 324.02, 326.88, 322.66, E3, 1000, 0)
    pm3.on_bar("XUSDT", (2000, 323.5, 323.9, 321.0, 321.33, 1.0), 2.0)
    assert abs(pos3.invalid - 323.34) < 1e-9

    # ④ 仅影线触及分支(frac>0):极值刺到目标后收盘反抽回 entry 附近,
    #    10% 计划锁利价落到多头现价**上方**(挂上即成交)⇒ 闸门拒绝
    pm4 = PositionManager(Params(), allow_add=False, time_stop=False,
                          reject_opp_lock_frac=0.10)
    pos4 = pm4.open("XUSDT", 1, 100.0, 94.0, 110.0, E3, 1000, 0)
    pm4.on_bar("XUSDT", (2000, 101.0, 110.5, 99.0, 99.2, 1.0), 2.0)
    assert pos4.invalid == 94.0
    assert pos4.reduced is False
    assert any("opp_lock_reject" in s for s in pos4.log)


def test_live_runner_sub_bar_lock_market_anchor(tmp_path):
    """_on_sub_bar:行情锚定 + 落位闸门 + 图 ENTRY 用建仓均价。"""
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import live_runner as lr

    cfg = json.loads((root / "config" / "live.json").read_text("utf-8"))
    step2 = 7_200_000
    t0 = 1_790_000_000_000 - (1_790_000_000_000 % 86_400_000)
    bars = [(t0 + k * step2, 100.0, 102.0, 99.0, 101.0, 10.0) for k in range(396)]

    class DummyClient:
        def history_candles(self, symbol, granularity="2H", *, start_ms=None,
                            end_ms=None, limit=200):
            if start_ms is not None:
                return [b for b in bars if b[0] >= start_ms][:limit]
            out = [b for b in bars if end_ms is None or b[0] < end_ms]
            return out[-limit:]

        def universe(self, min_vol):
            return ["BTCUSDT"]

    fills = []

    class DummyTg:
        def send_fill(self, symbol, action, qty, price, **kw):
            fills.append((symbol, action, qty, price, kw))
            return True

        def __getattr__(self, name):
            return lambda *a, **k: True

    runner = lr.LiveRunner("paper", cfg, DummyClient(), DummyTg(),
                           equity0=1000.0,
                           state_path=tmp_path / "state.json",
                           journal_dir=tmp_path / "logs",
                           symbols=("BTCUSDT",),
                           universe_enabled=False)
    runner.step(bars[-1][0] + 1)
    cs = runner.campaigns["BTCUSDT"]
    # 生产配置(config/v21.json arm_config)已接线行情锚定
    assert cs.pm.opp_lock_max_giveback_r == 0.25
    assert cs.pm.reject_opp_lock_frac == -0.50

    it = {"oid": "mo21anch1", "symbol": "BTCUSDT", "tf_label": "12h",
          "direction": 1, "qty": 1.0, "invalid": 95.0, "target_1": 110.0,
          "event_id": "E3", "route": "reject_market",
          "box_lo": 96.0, "box_hi": 110.0}
    runner._register_position("BTCUSDT", it, 100.0, bars[-1][0])

    # 子周期收盘 111.0 突破 target_1 ⇒ 计划 105.0 / 行情 111−0.25·5 = 109.75
    # ⇒ 取更贴近现价者 109.75(而不是把防线留在现价身后 1.15R 的位置)
    t_sub1 = t0 + 396 * step2
    runner._advance(cs, t_sub1 + step2,
                    new_bars=[(t_sub1, 101.0, 112.0, 100.5, 111.0, 15.0)],
                    run_paper_fills=True)
    assert abs(runner.positions["BTCUSDT"]["invalid"] - 109.75) < 1e-9
    assert abs(cs.pm.positions["BTCUSDT"].invalid - 109.75) < 1e-9
    lock_fill = [f for f in fills if "止损推进" in f[1]][-1]
    assert abs(lock_fill[4]["stop_price"] - 109.75) < 1e-9
    # 图 ENTRY = 建仓均价(不是本次事件价 111.0,修前会把相对关系画错)
    assert lock_fill[4]["entry_price"] == 100.0
    assert lock_fill[3] == 111.0

    # 落位闸门:同一持仓改走"仅触及"分支(frac>0.10),子周期影线刺到
    # target 后收盘反抽回 96.0 ⇒ 计划 101.0 在现价上方、行情锚 94.75 在
    # entry 亏损侧 ⇒ 两个候选都不可用 ⇒ 不推进、不置 reduced
    pm = cs.pm
    pos = pm.positions["BTCUSDT"]
    pos.reduced = False
    pos.invalid = 95.0
    runner.positions["BTCUSDT"]["invalid"] = 95.0
    pm.reject_opp_lock_frac = 0.10
    t_sub2 = t0 + 397 * step2
    runner._advance(cs, t_sub2 + step2,
                    new_bars=[(t_sub2, 100.0, 110.4, 96.0, 96.0, 20.0)],
                    run_paper_fills=True)
    pos = pm.positions["BTCUSDT"]
    assert abs(pos.invalid - 95.0) < 1e-9
    assert pos.reduced is False
    assert any("sub_bar_opp_lock_reject" in s for s in pos.log)
    # 闸门拒绝要留审计痕迹(journal 有 lock_reject 记录)
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert logs and any("lock_reject" in p.read_text(encoding="utf-8")
                        for p in logs)


def test_live_runner_on_sub_bar_stop_and_tp1_lock(tmp_path):
    """验证 _on_sub_bar 在高周期(12h)收盘前:
    1) 子周期(2h)突破 target_1 时立即推进防线锁利 50%,无需等待 12h 收盘;
    2) 子周期(2h)触及 STOP(invalid)时立即平仓结算,消除大周期收盘延迟。
    """
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import live_runner as lr

    cfg = json.loads((root / "config" / "live.json").read_text("utf-8"))
    step2 = 7_200_000
    t0 = 1_790_000_000_000 - (1_790_000_000_000 % 86_400_000)
    # 396 根 2H (刚好 66 根完整 12h 结束:396 % 6 == 0)
    bars = [(t0 + k * step2, 100.0, 102.0, 99.0, 101.0, 10.0) for k in range(396)]

    class DummyClient:
        def history_candles(self, symbol, granularity="2H", *, start_ms=None, end_ms=None, limit=200):
            if start_ms is not None:
                return [b for b in bars if b[0] >= start_ms][:limit]
            out = [b for b in bars if end_ms is None or b[0] < end_ms]
            return out[-limit:]

        def universe(self, min_vol):
            return ["BTCUSDT"]

    fills = []

    class DummyTg:
        def send_fill(self, symbol, action, qty, price, **kw):
            fills.append((symbol, action, qty, price, kw))
            return True

        def __getattr__(self, name):
            return lambda *a, **k: True

    runner = lr.LiveRunner("paper", cfg, DummyClient(), DummyTg(),
                           equity0=1000.0,
                           state_path=tmp_path / "state.json",
                           journal_dir=tmp_path / "logs",
                           symbols=("BTCUSDT",),
                           universe_enabled=False)
    runner.step(bars[-1][0] + 1)
    cs_12h = runner.campaigns["BTCUSDT"]
    assert cs_12h.tf_label == "12h"
    # 本用例钉的是**旧口径 parity**(纯计划口径):行情锚定关闭后必须逐位复现
    # 105.0;行情锚定/落位闸门另见 test_live_runner_sub_bar_lock_market_anchor。
    cs_12h.pm.opp_lock_max_giveback_r = 0.0

    # 注册一笔 12h 多头 REJECT 持仓:entry=100, invalid=95, target_1=110
    open_ts = bars[-1][0]
    it = {
        "oid": "mo21testopen1",
        "symbol": "BTCUSDT",
        "tf_label": "12h",
        "direction": 1,
        "qty": 1.0,
        "invalid": 95.0,
        "target_1": 110.0,
        "event_id": "E3",
        "route": "reject_market",
        "box_lo": 96.0,
        "box_hi": 110.0,
    }
    runner._register_position("BTCUSDT", it, 100.0, open_ts)
    assert runner.positions["BTCUSDT"]["invalid"] == 95.0

    # 下一根 2H bar(k=396,非 12h 收盘时刻):收盘 111.0 突破 target_1(110.0)
    # 应由 _on_sub_bar 立即将 invalid 推进至 100 + 0.5*(110-100) = 105.0
    t_sub1 = t0 + 396 * step2
    assert (t_sub1 + step2) % (43200 * 1000) != 0  # 确认非 12h 收盘点
    runner._advance(cs_12h, t_sub1 + step2,
                    new_bars=[(t_sub1, 101.0, 112.0, 100.5, 111.0, 15.0)],
                    run_paper_fills=True)
    assert runner.positions["BTCUSDT"]["invalid"] == 105.0
    assert cs_12h.pm.positions["BTCUSDT"].invalid == 105.0
    assert any("止损推进" in f[1] for f in fills)

    # 再下一根 2H bar(k=397,仍非 12h 收盘时刻):低点 104.0 跌破新止损 105.0
    # 应由 _on_sub_bar 立即按 105.0 触发 STOP 平仓,无需等待 12h 收盘
    t_sub2 = t0 + 397 * step2
    assert (t_sub2 + step2) % (43200 * 1000) != 0
    runner._advance(cs_12h, t_sub2 + step2,
                    new_bars=[(t_sub2, 108.0, 108.5, 104.0, 104.5, 20.0)],
                    run_paper_fills=True)
    assert "BTCUSDT" not in runner.positions
    assert "BTCUSDT" not in cs_12h.pm.positions
    assert any("平仓(paper,exit)" in f[1] and abs(f[3] - 105.0) < 1e-9 for f in fills)




