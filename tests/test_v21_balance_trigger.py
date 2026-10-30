"""v21 极简平衡箱(BalanceDetector) + 雙原语(ACCEPT/REJECT) + 高阶降档混合触发测试。"""

from __future__ import annotations

import json
from pathlib import Path

from market_operator.events import (
    ACCEPT, BREAKOUT_ACCEPTED, LEVEL_REJECTED, REJECT, EventDetector, MarketEvent, canonical_event_id,
)
from market_operator.hypothesis import StructureView
from market_operator.routing import (
    ROUTE_BREAKOUT_ACCEPTED, ROUTE_LEVEL_REJECTED, WHITELIST_BALANCE,
    RouterParams, MarketRouter,
)
from market_operator.structure import (
    Params, Regime, Zone, ZoneRole, ZoneState,
)


def _prod_lock_frac() -> float:
    """生产配置里的锁利 `frac`。

    这些用例断言的是**语义**(峰值锚定、预算 = (1−|frac|)·span、可多次棘轮),
    不是某个标定值 —— 标定会随数据更新(v21.7 从 −0.50 改到 −0.25),所以一律从
    `config/strategy.json` 取值,免得"调个参数就要改一堆断言"。
    """
    payload = json.loads((Path(__file__).resolve().parents[1]
                          / "config" / "strategy.json").read_text("utf-8"))
    return float(payload["arm_config"]["reject_opp_lock_frac"])


def _prod_giveback(entry: float, target: float) -> float:
    """生产 `frac` 下允许回吐的行程(价格单位)= (1−|frac|)·|target−entry|。"""
    return (1.0 - abs(_prod_lock_frac())) * abs(float(target) - float(entry))


def _legacy_lock_px(entry: float, target: float, frac: float) -> float | None:
    """**已退役**的绝对价位口径:entry→target 行程的 |frac| 处。

    v21.7 前生产用这个式子算锁利价;它只看建仓时的行程,不看峰值,所以
    决策时现价早已越过该价(钉在身后 = 永不触发)。函数已从 `position.py`
    删除 —— 这里只作为**对照量**保留,说明新口径在 peak==target 时与之逐位
    重合、且真正触发时回吐恰为预算。None 语义同旧实现(未启用 ⇒ None)。
    """
    span = float(target) - float(entry)
    frac_abs = abs(float(frac))
    if frac_abs <= 0.0 or abs(span) < 1e-12:
        return None
    return float(entry) + frac_abs * span



def test_accept_reject_aliases_and_routing():
    assert canonical_event_id(ACCEPT) == BREAKOUT_ACCEPTED
    assert canonical_event_id(REJECT) == LEVEL_REJECTED
    assert canonical_event_id("BREAKOUT_ACCEPTED") == BREAKOUT_ACCEPTED
    assert canonical_event_id("LEVEL_REJECTED") == LEVEL_REJECTED
    assert ROUTE_BREAKOUT_ACCEPTED == ROUTE_BREAKOUT_ACCEPTED
    assert ROUTE_LEVEL_REJECTED == ROUTE_LEVEL_REJECTED

    wl = frozenset({(ACCEPT, 1), (ACCEPT, -1), (REJECT, 1), (REJECT, -1)})
    router = MarketRouter(RouterParams(whitelist=wl))
    z = Zone(lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
             touches=3, origin_known=1, last_change_known=5,
             box_opposite=90.0, box_quality=0.55, box_width_atr=5.0, box_rotations=3)
    view = StructureView(regime=Regime.RANGE, zones=(z,), atr=2.0)

    # 1. ACCEPT (+1) 事件触发市价开多
    ev_acc = MarketEvent(id=BREAKOUT_ACCEPTED, direction=1, ts=1000, known_index=10,
                         attrs={"zone": z, "ref": 100.0, "invalid": 97.8})
    decs = router.on_bar("BTCUSDT", (1000, 100.0, 101.5, 99.5, 101.0, 1.0),
                         10, view, 2.0, [ev_acc],
                         position_open=False, entry_resting=False)
    assert len(decs) == 1
    assert decs[0].route == ROUTE_BREAKOUT_ACCEPTED
    assert decs[0].hyp.direction == 1

    # 2. REJECT (-1) 事件触发市价开空
    ev_rej = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=2000, known_index=11,
                         attrs={"zone": z, "ref": 100.0, "invalid": 100.2})
    decs2 = router.on_bar("BTCUSDT", (2000, 100.0, 100.5, 98.5, 99.0, 1.0),
                          11, view, 2.0, [ev_rej],
                          position_open=False, entry_resting=False)
    assert len(decs2) == 1
    assert decs2[0].route == ROUTE_LEVEL_REJECTED
    assert decs2[0].hyp.direction == -1


def test_restraint_gates_rotations_and_quality():
    wl = WHITELIST_BALANCE
    router = MarketRouter(RouterParams(whitelist=wl, min_box_rotations=3, min_box_quality=0.50))
    z_weak = Zone(lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
                  touches=2, origin_known=1, last_change_known=5,
                  box_opposite=92.0, box_quality=0.45, box_width_atr=3.0, box_rotations=2)
    z_strong = Zone(lo=98.0, hi=100.0, role=ZoneRole.RESISTANCE, state=ZoneState.ACTIVE,
                    touches=4, origin_known=1, last_change_known=5,
                    box_opposite=92.0, box_quality=0.60, box_width_atr=4.0, box_rotations=3)
    view = StructureView(regime=Regime.RANGE, zones=(z_weak, z_strong), atr=2.0)

    ev1 = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=1000, known_index=10,
                      attrs={"zone": z_weak, "ref": 100.0, "invalid": 100.2})
    assert router.on_bar("BTCUSDT", (1000, 100.0, 100.5, 98.5, 99.0, 1.0),
                         10, view, 2.0, [ev1],
                         position_open=False, entry_resting=False) == []

    ev2 = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=2000, known_index=11,
                      attrs={"zone": z_strong, "ref": 100.0, "invalid": 100.2})
    decs = router.on_bar("BTCUSDT", (2000, 100.0, 100.5, 98.5, 99.0, 1.0),
                         11, view, 2.0, [ev2],
                         position_open=False, entry_resting=False)
    assert len(decs) == 1


def test_v21_config_valid_and_loads_balance_detector():
    root = Path(__file__).resolve().parents[1]
    cfg_path = root / "config" / "strategy.json"
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
    pos = pm.open("BTCUSDT", -1, 100.0, 105.0, 90.0, LEVEL_REJECTED, 1000, 0)
    # 1. 仅极值触及 90.0 但收盘 92.0 未突破对侧 -> 不锁利、不减仓,原止损 105.0 不动
    intents1 = pm.on_bar("BTCUSDT", (2000, 98.0, 99.0, 89.5, 92.0, 1.0), 2.0)
    assert intents1 == []
    assert pos.invalid == 105.0
    assert pos.reduced is False
    # 2. 收盘 89.0 真正突破对侧 90.0 ⇒ arming;峰值 88.5 / 预算 ½·10 = 5
    #    ⇒ 防线 93.5(= peak + budget),不减仓(reduced 保持 False)
    intents2 = pm.on_bar("BTCUSDT", (3000, 92.0, 93.0, 88.5, 89.0, 1.0), 2.0)
    assert intents2 == []
    assert pos.invalid == 93.5
    assert pos.armed is True
    assert pos.reduced is False


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
        "BTCUSDT", "LEVEL_REJECTED", "空", "level_rejected_market", 111.5, 114.0,
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
        "BTCUSDT", "开仓(live)", 0.5, 111.5, note="LEVEL_REJECTED e3_market",
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


def test_opp_lock_budget_and_peak_definition():
    """v21.7 锁利 = **回吐预算**(价格单位),不是钉在某个绝对价位。

        budget = (1 − |frac|)·|target − entry| ; stop = peak ∓ budget

    端点语义:``|frac|→0`` 不锁(预算 = 整段计划行程);``|frac|=1`` 全锁
    (预算 = 0 ⇒ 触到目标即离场)。``frac`` 的符号只决定 arming 口径
    (<0 收盘突破 / >0 盘中触及),不再是"两把尺子"里的另一把。
    """
    from market_operator.position import (
        opp_lock_budget, opp_lock_peak_px, stop_protect_dist,
    )

    assert abs(opp_lock_budget(100.0, 90.0, 0.5) - 5.0) < 1e-12
    assert abs(opp_lock_budget(324.02, 322.66, 0.5) - 0.68) < 1e-12
    assert opp_lock_budget(100.0, 90.0, 1.0) == 0.0        # 全锁 ⇒ 触价即平
    assert opp_lock_budget(100.0, 90.0, 2.0) == 0.0        # 越界按全锁
    assert opp_lock_budget(100.0, 90.0, 0.0) is None       # 未启用
    assert opp_lock_budget(100.0, 100.0, 0.5) is None      # 行程退化

    # 峰值恰等于 target 时,新口径与参考价 `entry + frac·span` 逐位重合:
    # 说明 |frac| 仍是"锁住计划行程的多少",只是改由回吐预算来表达。
    assert abs(opp_lock_peak_px(-1, 322.66, 0.68)
               - _legacy_lock_px(324.02, 322.66, 0.5)) < 1e-12
    assert abs(opp_lock_peak_px(1, 110.0, 5.0) - 105.0) < 1e-12

    # 恒等式:保护侧距离 ≡ 预算 − 自峰值回撤
    # ⇒ "落在保护侧"与"回吐未超预算"同真同假,构造上不会算出非法价位。
    peak, budget, ref = 320.93, 0.68, 321.33
    stop = opp_lock_peak_px(-1, peak, budget)
    assert abs(stop_protect_dist(-1, stop, ref)
               - (budget - (ref - peak))) < 1e-12


def test_opp_lock_step_three_state():
    """三态穷尽:未 arming 保持结构防线 / 保护侧推进(可多次)/ 越过即离场。"""
    from market_operator.position import opp_lock_step

    kw = dict(direction=1, entry=100.0, target=110.0, frac=-0.5)

    # ① 未 arming(收盘 108 < 110)⇒ hold:不预设任何锁利价
    d0 = opp_lock_step(peak=109.0, high=109.0, low=99.0, close=108.0, **kw)
    assert (d0.action, d0.armed) == ("hold", False)
    assert d0.budget == 5.0 and d0.stop_px is None

    # ② 收盘突破 ⇒ arming + 推进到 peak−budget = 112−5 = 107
    d1 = opp_lock_step(peak=112.0, high=112.0, low=107.0, close=111.0, **kw)
    assert (d1.action, d1.armed) == ("tighten", True)
    assert abs(d1.stop_px - 107.0) < 1e-12
    assert abs(d1.drawdown - 1.0) < 1e-12

    # ③ 新峰值 ⇒ **再次**棘轮(不是一次性置位):115−5 = 110
    d2 = opp_lock_step(peak=115.0, high=115.0, low=110.0, close=114.5,
                       armed=True, **kw)
    assert (d2.action, d2.armed) == ("tighten", True)
    assert abs(d2.stop_px - 110.0) < 1e-12

    # ④ 回撤 ≥ 预算(115−109.5 = 5.5 ≥ 5)⇒ 市价离场;
    #    不存在"闸门拒单后原地等下一根"的第四态。
    d3 = opp_lock_step(peak=115.0, high=115.0, low=109.5, close=109.5,
                       armed=True, **kw)
    assert d3.action == "exit"
    assert d3.drawdown >= d3.budget

    # ⑤ 空头镜像 + 触及 arming(frac>0):峰值 320.93 − 预算 0.68 ⇒ 321.61
    s = opp_lock_step(direction=-1, entry=324.02, target=322.66, frac=0.5,
                      peak=320.93, high=324.22, low=320.93, close=321.33)
    assert (s.action, s.armed) == ("tighten", True)
    assert abs(s.stop_px - 321.61) < 1e-12

    # ⑥ 参数域不成立(目标落在 entry 反侧)⇒ off:不锁利、不减仓
    bad = opp_lock_step(direction=1, entry=112.0, target=110.0, frac=-0.5,
                        peak=111.2, high=111.2, low=110.3, close=110.8)
    assert bad.action == "off" and bad.reason == "degenerate_span"


def test_opp_lock_on_bar_ratchet_exit_and_degenerate_domain():
    """回放 on_bar:arming 后逐 bar 棘轮 / 越过预算即市价离场 / 退化行程不锁利。"""
    from market_operator.position import PositionManager

    # ① 同一根 bar 内"冲高又跌回":收盘突破 arming,peak 118 / 预算 5 ⇒ 113,
    #    而收盘 111 已在 113 之下 ⇒ 不挂一张必然立刻成交的单,直接市价离场。
    pm = PositionManager(Params(), allow_add=False, time_stop=False,
                         reject_opp_lock_frac=-0.50)
    pos = pm.open("XUSDT", 1, 100.0, 94.0, 110.0, LEVEL_REJECTED, 1000, 0)
    intents = pm.on_bar("XUSDT", (2000, 111.0, 118.0, 109.0, 111.0, 1.0), 2.0)
    assert len(intents) == 1 and intents[0].kind.name == "EXIT"
    assert "lock_giveback_exhausted" in intents[0].reason
    assert "XUSDT" not in pm.positions
    assert any("lock_giveback_exhausted" in s for s in pos.log)

    # ② 正常形态:arming 推进 107,再随新峰棘轮到 110;全程不减仓
    pm2 = PositionManager(Params(), allow_add=False, time_stop=False,
                          reject_opp_lock_frac=-0.50)
    pos2 = pm2.open("XUSDT", 1, 100.0, 94.0, 110.0, LEVEL_REJECTED, 1000, 0)
    assert pm2.on_bar("XUSDT", (2000, 110.0, 112.0, 110.0, 111.5, 1.0), 2.0) == []
    assert abs(pos2.invalid - 107.0) < 1e-12 and pos2.armed is True
    assert pm2.on_bar("XUSDT", (3000, 111.0, 115.0, 111.0, 114.5, 1.0), 2.0) == []
    assert abs(pos2.invalid - 110.0) < 1e-12        # 115 − 5(棘轮可多次)
    assert pos2.reduced is False                    # 锁利不消耗减仓额度
    assert any("opp_lock_armed" in s for s in pos2.log)

    # ③ 参数域不成立(多头 target 110 低于 entry 112)⇒ off:防线不动、不减仓,
    #    且只记一次(不是逐 bar 刷台账)
    pm3 = PositionManager(Params(), allow_add=False, time_stop=False,
                          reject_opp_lock_frac=-0.50)
    pos3 = pm3.open("XUSDT", 1, 112.0, 106.0, 110.0, LEVEL_REJECTED, 1000, 0)
    for k in (2000, 3000):
        assert pm3.on_bar("XUSDT", (k, 111.0, 111.2, 110.3, 110.8, 1.0), 2.0) == []
    assert pos3.invalid == 106.0 and pos3.reduced is False and pos3.armed is False
    assert sum("opp_lock_off" in s for s in pos3.log) == 1

    # ④ frac=0 ⇒ 传统 §8.2 减仓 50%(两个参数域互斥,不共用 reduced)
    pm4 = PositionManager(Params(), allow_add=False, time_stop=False)
    pm4.open("XUSDT", 1, 100.0, 94.0, 110.0, LEVEL_REJECTED, 1000, 0)
    intents4 = pm4.on_bar("XUSDT", (2000, 109.5, 110.5, 109.0, 110.2, 1.0), 2.0)
    assert intents4 and intents4[0].kind.name == "REDUCE"


def test_opp_lock_property_random_paths_keep_invariants():
    """随机路径上的三条不变量:只紧不松 / 恒在保护侧 / 要么推进要么离场。"""
    import random

    from market_operator.position import opp_lock_step, stop_protect_dist

    rng = random.Random(20261024)
    for _ in range(200):
        direction = rng.choice((1, -1))
        entry = 100.0
        span = rng.uniform(1.0, 20.0)
        target = entry + direction * span
        frac = -rng.choice((0.25, 0.5, 0.75, 1.0))
        peak = entry
        armed = False
        prev_stop = None
        for _ in range(40):
            ref = entry + direction * rng.uniform(-0.2 * span, 1.5 * span)
            hi = max(entry, ref) + rng.uniform(0.0, 0.6 * span)
            lo = min(entry, ref) - rng.uniform(0.0, 0.6 * span)
            peak = max(peak, hi) if direction > 0 else min(peak, lo)
            dec = opp_lock_step(
                direction=direction, entry=entry, target=target, frac=frac,
                peak=peak, high=hi, low=lo, close=ref, ref_px=ref,
                armed=armed)
            assert dec.action in ("hold", "tighten", "exit")
            armed = dec.armed
            if dec.action == "tighten":
                # 在保护侧 + 在 entry 盈利侧(结构性质,不靠闸门兜)
                assert stop_protect_dist(direction, dec.stop_px, ref) > 0.0
                assert direction * (dec.stop_px - entry) > 0.0
                # 只紧不松
                if prev_stop is not None:
                    assert direction * (dec.stop_px - prev_stop) >= -1e-12
                prev_stop = dec.stop_px
            elif dec.action == "exit":
                # 离场 ⇔ 回撤已达预算(不会"没超预算却离场")
                assert dec.drawdown >= dec.budget - 1e-9
            else:
                assert dec.stop_px is None


def test_opp_lock_frequency_invariance():
    """同一价格路径,1×/2×/4× 采样得到**同一防线水平**(因果且频率无关)。

    子周期只是"更早看到同一事实" —— 防线水平不随裁决频率漂移;若允许漂移,
    实盘(2h 子帧)与回放(12h 认知帧)就永远不可比。
    """
    from market_operator.position import PositionManager

    sub = [
        (110.0, 112.0, 110.0, 111.5),
        (111.0, 114.0, 111.0, 113.5),
        (113.0, 116.0, 113.0, 115.5),
        (115.0, 118.0, 115.0, 117.5),
    ]

    def agg(bars, n):
        out = []
        for i in range(0, len(bars), n):
            chunk = bars[i:i + n]
            out.append((chunk[0][0], max(b[1] for b in chunk),
                        min(b[2] for b in chunk), chunk[-1][3]))
        return out

    def run(bars):
        pm = PositionManager(Params(), allow_add=False, time_stop=False,
                             reject_opp_lock_frac=-0.50)
        pos = pm.open("XUSDT", 1, 100.0, 94.0, 110.0, LEVEL_REJECTED, 1000, 0)
        for k, (o, h, l, c) in enumerate(bars):
            pm.on_bar("XUSDT", (2000 + k, o, h, l, c, 1.0), 2.0,
                      price_only=True)
        assert pm.positions.get("XUSDT") is pos      # 路径内未触发离场
        return pos

    p1, p2, p4 = run(agg(sub, 1)), run(agg(sub, 2)), run(agg(sub, 4))
    assert p1.armed and p2.armed and p4.armed
    assert abs(p1.invalid - p2.invalid) < 1e-12
    assert abs(p1.invalid - p4.invalid) < 1e-12
    assert abs(p1.invalid - 113.0) < 1e-12        # 118 − ½·10
    assert p1.reduced is False                    # 锁利不消耗减仓额度



def test_live_runner_sub_bar_lock_market_anchor(tmp_path):
    """_on_sub_bar:峰值锚定(同一 opp_lock_step)+ 图 ENTRY 用建仓均价。"""
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
    # 生产配置:锁利只有 frac 一个参数(第二把 R 单位尺子已删除)
    assert cs.pm.reject_opp_lock_frac == _prod_lock_frac()
    assert not hasattr(cs.pm, "opp_lock_max_giveback_r")

    it = {"oid": "mo21anch1", "symbol": "BTCUSDT", "tf_label": "12h",
          "direction": 1, "qty": 1.0, "invalid": 95.0, "target_1": 110.0,
          "event_id": "E3", "route": "reject_market",
          "box_lo": 96.0, "box_hi": 110.0}
    runner._register_position("BTCUSDT", it, 100.0, bars[-1][0])

    # 子周期收盘 111.0 突破 target_1 ⇒ 峰值取本根 high 112,预算 (1−|frac|)·10
    # ⇒ 防线 112 − 预算(而不是把防线钉在 entry→target 的中点 105 上)
    t_sub1 = t0 + 396 * step2
    runner._advance(cs, t_sub1 + step2,
                    new_bars=[(t_sub1, 101.0, 112.0, 100.5, 111.0, 15.0)],
                    run_paper_fills=True)
    exp1 = 112.0 - _prod_giveback(100.0, 110.0)
    assert abs(runner.positions["BTCUSDT"]["invalid"] - exp1) < 1e-9
    assert abs(cs.pm.positions["BTCUSDT"].invalid - exp1) < 1e-9
    lock_fill = [f for f in fills if "锁利推进" in f[1]][-1]
    assert abs(lock_fill[4]["stop_price"] - exp1) < 1e-9
    # 图 ENTRY = 建仓均价(不是本次事件价 111.0,修前会把相对关系画错)
    assert lock_fill[4]["entry_price"] == 100.0
    assert lock_fill[3] == 111.0

    # 同一根子周期内"冲高又跌回"且已超预算 ⇒ 立刻市价离场:
    # 触及模式(frac>0),峰值 112 / 预算 5 ⇒ 止损 107,而收盘 105.5 已在其下
    # ⇒ 不挂"永不触发"的止损单,也不拒单后裸奔到认知 bar 收盘。
    cs.pm.positions.pop("BTCUSDT", None)
    runner.positions.pop("BTCUSDT", None)
    it2 = dict(it, oid="mo21anch2", event_id="E4", invalid=95.0)
    runner._register_position("BTCUSDT", it2, 100.0, t_sub1)
    cs.pm.reject_opp_lock_frac = 0.50
    t_sub2 = t_sub1 + step2
    runner._advance(cs, t_sub2 + step2,
                    new_bars=[(t_sub2, 105.0, 112.0, 104.0, 105.5, 20.0)],
                    run_paper_fills=True)
    assert "BTCUSDT" not in runner.positions
    assert "BTCUSDT" not in cs.pm.positions
    assert any("平仓(paper,exit)" in f[1] and abs(f[3] - 105.5) < 1e-9
               for f in fills)
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert logs and any('"kind": "lock_exhausted"' in p.read_text(encoding="utf-8")
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
        "event_id": "LEVEL_REJECTED",
        "route": "reject_market",
        "box_lo": 96.0,
        "box_hi": 110.0,
    }
    runner._register_position("BTCUSDT", it, 100.0, open_ts)
    assert runner.positions["BTCUSDT"]["invalid"] == 95.0

    # 下一根 2H bar(k=396,非 12h 收盘时刻):收盘 111.0 突破 target_1(110.0)
    # 应由 _on_sub_bar 立即把 invalid 推进到 峰值 − 预算(预算 = (1−|frac|)·span)
    t_sub1 = t0 + 396 * step2
    assert (t_sub1 + step2) % (43200 * 1000) != 0  # 确认非 12h 收盘点
    runner._advance(cs_12h, t_sub1 + step2,
                    new_bars=[(t_sub1, 101.0, 112.0, 100.5, 111.0, 15.0)],
                    run_paper_fills=True)
    exp_stop = 112.0 - _prod_giveback(100.0, 110.0)
    assert runner.positions["BTCUSDT"]["invalid"] == exp_stop
    assert cs_12h.pm.positions["BTCUSDT"].invalid == exp_stop
    # 通知的**语义标题**必须是"锁利推进"(防线已进盈利侧),不是"止损推进"
    assert any("锁利推进" in f[1] for f in fills)

    # 再下一根 2H bar(k=397,仍非 12h 收盘时刻):低点 104.0 跌破新止损
    # 应由 _on_sub_bar 立即按止损价触发 STOP 平仓,无需等待 12h 收盘
    t_sub2 = t0 + 397 * step2
    assert (t_sub2 + step2) % (43200 * 1000) != 0
    runner._advance(cs_12h, t_sub2 + step2,
                    new_bars=[(t_sub2, 108.0, 108.5, 104.0, 104.5, 20.0)],
                    run_paper_fills=True)
    assert "BTCUSDT" not in runner.positions
    assert "BTCUSDT" not in cs_12h.pm.positions
    assert any("平仓(paper,exit)" in f[1] and abs(f[3] - exp_stop) < 1e-9
               for f in fills)


def test_opp_lock_giveback_identity_on_googl_repro():
    """2026-10-23 GOOGLUSDT 空头复现:回吐预算口径锁住的是**事实**,不是承诺。

    实盘事实:空头 entry 324.02 / 结构止损 326.88 / 结构目标 322.66;2h 收盘
    321.33 突破对侧缘。当日最低 320.93。

    旧口径把防线钉在 entry→target 行程中点 323.34(距现价 2.01,不回踩就
    等于没锁;回踩触发时已把 1.08R 回吐到 0.24R,实际成交 324.01)。
    新口径:峰值 320.93 − ½span(0.68) ⇒ 321.61 ——
      - 距现价 0.28,仍在保护侧(空头止损须高于现价);
      - 若真被触发,回吐恰为 0.68 = ½span(恒等式,不随行情漂移);
      - 锁住 (324.02−321.61)/2.86 = 0.84R(旧口径 0.24R,实盘 −0.00R)。
    """
    from market_operator.position import (
        PositionManager, opp_lock_budget, stop_protect_dist,
    )

    entry, invalid0, target, low, close = 324.02, 326.88, 322.66, 320.93, 321.33
    span = entry - target                       # 1.36
    assert abs(span - 1.36) < 1e-12
    budget = opp_lock_budget(entry, target, 0.5)
    assert abs(budget - 0.68) < 1e-12
    peak_stop = low + budget                    # 空头:peak + budget
    assert abs(peak_stop - 321.61) < 1e-12

    # 旧"绝对价位"口径:323.34,距现价 2.01(行情不回踩 ⇒ 这张单永不触发)
    plan = _legacy_lock_px(entry, target, 0.5)
    assert abs(plan - 323.34) < 1e-9
    assert abs((plan - close) - 2.01) < 1e-9

    # 新口径严格更贴近现价(空头更小),且结构性质仍成立:
    # 在现价保护侧 + 在 entry 盈利侧 —— 这是**性质**,不需要闸门去兜。
    assert peak_stop < plan
    assert stop_protect_dist(-1, peak_stop, close) > 0.0
    assert peak_stop < entry                    # 在 entry 盈利侧(空头防线低于入场价)
    # 触发时的回吐恰为预算(这就是"承诺成立"的定义)
    assert abs((peak_stop - low) - budget) < 1e-12
    # 锁住的 R:(324.02 − 321.61)/2.86 ≈ 0.84R
    assert abs((entry - peak_stop) / (invalid0 - entry) - 0.84) < 0.01

    # canonical §8.2 路径:326.88 → 321.61,不减仓、不满仓离场
    pm = PositionManager(Params(), allow_add=False, breakeven_on_reduce=True,
                         time_stop=False, reject_opp_lock_frac=-0.50)
    pos = pm.open("GOOGLUSDT", -1, entry, invalid0, target, LEVEL_REJECTED,
                  1792742400000, 0)
    intents = pm.on_bar("GOOGLUSDT",
                        (1792749600000, 323.98, 324.22, low, close, 1.0), 2.0)
    assert intents == []
    assert abs(pos.invalid - peak_stop) < 1e-12
    assert pos.armed is True
    assert pos.reduced is False
    assert any("opp_lock_armed" in s for s in pos.log)
    assert any("opp_lock|" in s for s in pos.log)


def test_opp_lock_touch_arming_exhausted_in_same_bar_exits():
    """触及 arming(frac>0)在同一根 bar 内即超预算 ⇒ 市价离场。

    旧实现遇到"影线刺穿目标 + 收盘反抽回建仓价附近"时,算出的绝对价位落到
    现价错误一侧 ⇒ 闸门拒单 ⇒ 防线原地不动、仓位继续裸奔。新口径下这不是
    非法输入:峰值 322.0 / 预算 0.68 ⇒ 止损 322.68 已被现价 325.0 越过,
    第三态(离场)就是唯一正确处置。
    """
    from market_operator.position import PositionManager

    pm = PositionManager(Params(), allow_add=False, breakeven_on_reduce=True,
                         time_stop=False, reject_opp_lock_frac=0.50)
    pos = pm.open("GOOGLUSDT", -1, 324.02, 326.88, 322.66, LEVEL_REJECTED, 1000, 0)
    intents = pm.on_bar("GOOGLUSDT", (2000, 324.0, 325.5, 322.0, 325.0, 1.0), 2.0)
    assert len(intents) == 1 and intents[0].kind.name == "EXIT"
    assert "lock_giveback_exhausted" in intents[0].reason
    assert "GOOGLUSDT" not in pm.positions
    assert any("lock_giveback_exhausted" in s for s in pos.log)


def test_opp_lock_single_ruler_no_second_scale():
    """锁利只有一把尺子:回吐预算 =(1−|frac|)·span。

    修前这里还有第二把以 R 为单位的尺子(`opp_lock_max_giveback_r`):两把
    量纲不同的尺子取"更紧者"互相遮盖 —— 0.25R 在赢单 MFE 中位 2.07R /
    p90 5.71R 的分布里约等于只允许回吐 ~12% 行程,必被噪声扫
    (`reports/opp_lock_ab.md`:dev/test 净利 −12.83/−7.38)。而且"更紧"
    并不等于更安全:止损被推到现价身后,就成了挂上即成交的止盈单。

    现在:仓位参数里不存在该开关,锁利价由 peak 与单一 frac 唯一决定;
    预算随 span 自适应 ⇒ 与周期、品种、ATR 管线全部无关。
    """
    from market_operator.position import PositionManager, opp_lock_budget

    pm = PositionManager(Params(), allow_add=False, time_stop=False,
                         reject_opp_lock_frac=-0.50)
    assert not hasattr(pm, "opp_lock_max_giveback_r")
    try:
        PositionManager(Params(), opp_lock_max_giveback_r=0.25)
    except TypeError:
        pass
    else:
        raise AssertionError("第二把尺子必须从构造参数中消失")

    # 预算 = ½span,与品种/周期无关(价格单位,随 span 自动适配)
    assert opp_lock_budget(100.0, 110.0, 0.5) == 5.0
    assert abs(opp_lock_budget(324.02, 322.66, 0.5) - 0.68) < 1e-12

    # 生产配置只剩 frac 一个锁利键(回放与实盘共用同一口径)
    strat = json.loads((Path(__file__).resolve().parents[1]
                        / "config" / "strategy.json").read_text("utf-8"))
    assert "opp_lock_max_giveback_r" not in strat["arm_config"]
    assert strat["arm_config"]["reject_opp_lock_frac"] == _prod_lock_frac()



def test_live_sub_bar_lock_peak_anchor_and_ratchet(tmp_path):
    """实盘 `_on_sub_bar`:峰值锚定 + 多次棘轮(与回放同一个 `opp_lock_step`)。

    子周期收盘 321.33 那根的最低价 320.93 ⇒ 防线 = 320.93 + ½·1.36 = 321.61
    (旧口径会把防线钉在 323.34,距现价 2.01,不回踩等于没锁);下一根创新低
    318.5 ⇒ 棘轮到 319.18 —— 只紧不松,不依赖"回踩到某个旧价位"。
    """
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import live_runner as lr

    # 配置接线:生产 strategy.json 只给 frac,第二把 R 单位尺子已删除
    strat = json.loads((root / "config" / "strategy.json").read_text("utf-8"))
    pm_prod = lr.new_position_manager(Params(), strat)
    assert pm_prod.reject_opp_lock_frac == _prod_lock_frac()
    assert not hasattr(pm_prod, "opp_lock_max_giveback_r")

    # 缺键不兜底:写死的默认值会让下次标定后实盘与回放悄悄分叉
    try:
        lr.new_position_manager(Params(), {"use_balance_detector": True})
    except ValueError:
        pass
    else:
        raise AssertionError("缺 arm_config.reject_opp_lock_frac 时必须报错")

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

    class DummyTg:
        def send_fill(self, symbol, action, qty, price, **kw):
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
    it = {
        "oid": "mo21googlshort2", "symbol": "BTCUSDT", "tf_label": "12h",
        "direction": -1, "qty": 1.0, "invalid": 326.88, "target_1": 322.66,
        "event_id": "LEVEL_REJECTED", "route": "e3_market",
        "box_lo": 322.66, "box_hi": 326.67,
    }
    runner._register_position("BTCUSDT", it, 324.02, bars[-1][0])

    t1 = t0 + 396 * step2
    runner._advance(cs, t1 + step2,
                    new_bars=[(t1, 323.98, 324.22, 320.93, 321.33, 15.0)],
                    run_paper_fills=True)
    exp1 = 320.93 + _prod_giveback(324.02, 322.66)   # 峰值 + 预算(½span 时代为 321.61)
    assert abs(cs.pm.positions["BTCUSDT"].invalid - exp1) < 1e-9
    assert abs(runner.positions["BTCUSDT"]["invalid"] - exp1) < 1e-9
    assert cs.pm.positions["BTCUSDT"].invalid > 321.33     # 仍在保护侧
    assert cs.pm.positions["BTCUSDT"].armed is True

    # 棘轮:新低 318.5 ⇒ 峰值 − 预算(第二次推进,不是一次性置位)
    t2 = t1 + step2
    runner._advance(cs, t2 + step2,
                    new_bars=[(t2, 321.0, 321.2, 318.5, 319.0, 20.0)],
                    run_paper_fills=True)
    exp2 = 318.5 + _prod_giveback(324.02, 322.66)
    assert abs(cs.pm.positions["BTCUSDT"].invalid - exp2) < 1e-9
    assert exp2 < exp1                                    # 空头:只许向下紧


def test_live_sub_bar_lock_chart_entry_and_repeat_ratchet(tmp_path):
    """实盘 `_on_sub_bar`:①通知图的 ENTRY 必须是真实建仓均价(修前误用子周期
    收盘价 ⇒ 图上止损/目标相对 ENTRY 全错位);②锁利可多次推进。"""
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
    assert cs.tf_label == "12h"

    # 空头 REJECT:entry 324.02 / invalid 326.88 / target_1 322.66(箱体对侧缘)
    it = {
        "oid": "mo21googlshort", "symbol": "BTCUSDT", "tf_label": "12h",
        "direction": -1, "qty": 1.0, "invalid": 326.88, "target_1": 322.66,
        "event_id": "LEVEL_REJECTED", "route": "e3_market",
        "box_lo": 322.66, "box_hi": 326.67,
    }
    runner._register_position("BTCUSDT", it, 324.02, bars[-1][0])
    assert runner.positions["BTCUSDT"]["invalid"] == 326.88

    # 子周期收盘 321.33 突破对侧缘 ⇒ 峰值 320.93 + 预算(高于现价)
    t1 = t0 + 396 * step2
    assert (t1 + step2) % (43200 * 1000) != 0        # 确认非 12h 收盘点
    runner._advance(cs, t1 + step2,
                    new_bars=[(t1, 323.98, 324.22, 320.93, 321.33, 15.0)],
                    run_paper_fills=True)
    exp1 = 320.93 + _prod_giveback(324.02, 322.66)
    assert abs(runner.positions["BTCUSDT"]["invalid"] - exp1) < 1e-9
    assert abs(cs.pm.positions["BTCUSDT"].invalid - exp1) < 1e-9
    adv = [f for f in fills if "锁利推进" in f[1]]
    assert len(adv) == 1
    assert adv[0][1].startswith("锁利推进(已锁")          # 语义标题(空头锁利)
    assert abs(adv[0][3] - 321.33) < 1e-9                    # 参考价=子周期收盘
    assert abs(adv[0][4]["entry_price"] - 324.02) < 1e-9     # 图 ENTRY=真实建仓价
    assert adv[0][4]["price_label"] == "参考价(子周期收盘)"
    assert abs(adv[0][4]["stop_price"] - exp1) < 1e-9

    # 同一持仓再创新低 ⇒ 第二次推进(棘轮可多次),通知仍带真实 ENTRY;
    # 不是"一次性置位后锁利退出舞台"
    fills.clear()
    t2 = t1 + step2
    runner._advance(cs, t2 + step2,
                    new_bars=[(t2, 321.0, 321.2, 318.5, 319.0, 20.0)],
                    run_paper_fills=True)
    exp2 = 318.5 + _prod_giveback(324.02, 322.66)
    assert abs(runner.positions["BTCUSDT"]["invalid"] - exp2) < 1e-9
    adv2 = [f for f in fills if "锁利推进" in f[1]]
    assert len(adv2) == 1
    assert abs(adv2[0][4]["stop_price"] - exp2) < 1e-9
    assert abs(adv2[0][4]["entry_price"] - 324.02) < 1e-9
    assert exp2 < exp1


def test_pivot_exec_without_fine_pivots_fails_fast():
    """`pivot_exec=True` 必须配到细周期 pivot 流,否则开关静默无效。

    `CampaignFrame.exec_pivots` 必须由调用方显式装配,
    `scenario.build_frames`(验收与生产同源链)不装配 ⇒ 在生产链上打开此开关,
    回放结果与关闭**逐位相同**(2026-10-24 实测 6 段 net 完全一致)。这种"开关
    没接上"极易被读成"机制没价值",所以在装配期直接报错。
    """
    import pytest

    from types import SimpleNamespace

    from market_operator.campaign import require_exec_pivots

    bare = [SimpleNamespace(exec_pivots={}), SimpleNamespace(exec_pivots={})]
    require_exec_pivots(False, bare)                       # 关闭 ⇒ 不校验

    wired = [SimpleNamespace(exec_pivots={}), SimpleNamespace(exec_pivots={0: ()})]
    require_exec_pivots(True, wired)                       # 有数据 ⇒ 放行

    with pytest.raises(ValueError, match="exec_pivots"):
        require_exec_pivots(True, bare)

