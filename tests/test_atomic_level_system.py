"""二维 (t, P) 空间层析原子水平位系统、因果无未来函数、拍卖触发与实盘一致性测试。"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from market_operator.engine import load_strategy_bundle
from market_operator.events import (
    BREAKOUT_ACCEPTED,
    LEVEL_REJECTED,
    LevelAuctionEngine,
    MarketEvent,
)
from market_operator.hypothesis import StructureView
from market_operator.level import (
    Canvas2DLevelScanner,
    Level,
    LevelKind,
    LevelRole,
    LevelState,
)
from market_operator.replay import prepare_cognition
from market_operator.routing import MarketRouter
from market_operator.structure import MarketStructure, Params, Regime
from market_operator.trigger import MicTracker, prepare_mic_cognition


def _synthetic_wave_df(n: int = 180, step_ms: int = 14_400_000) -> pd.DataFrame:
    rows: list[tuple[int, float, float, float, float, float]] = []
    px = 1000.0
    t0 = 1_700_044_800_000  # 整除 4h
    for i in range(n):
        wave = 35.0 * math.sin(i * 0.24) + 12.0 * math.cos(i * 0.09)
        drift = 0.20 * i if i < 90 else 18.0 - 0.25 * (i - 90)
        c = 1000.0 + wave + drift
        o = px
        wick = 4.0 + (i % 4) * 1.2
        h = max(o, c) + wick
        l = min(o, c) - wick
        rows.append((t0 + i * step_ms, o, h, l, c, 100.0))
        px = c
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])


def test_2d_canvas_collinear_peaks_and_peak_trough_sr_flip() -> None:
    """二维画布应将双顶共线识别为 EQUAL,并在突破后同高度波谷折返时识别为跨极性 FLIPPED 支撑颈线。"""
    scanner = Canvas2DLevelScanner(canvas_bars=120, max_active_per_side=8)
    bars: list[tuple[int, float, float, float, float, float]] = []
    # 构建前半段在 [100, 108] 活动、两次上探 110.0 迅速受阻折返的自然 K 线画布
    closes = [
        103, 105, 103, 104, 106, 110.0, 105, 103, 104, 102, 104, 103, 105, 104,
        106, 110.0, 105, 103, 104, 102, 104, 103, 105, 104,
    ]
    for i, c in enumerate(closes):
        o = float(closes[i - 1]) if i > 0 else float(c)
        h = 110.2 if c == 110.0 else max(o, c) + 1.2
        l = min(o, c) - 1.2
        bars.append((i * 3_600_000, o, h, l, float(c), 10.0))

    scanner.update_canvas(bars)
    res_eq = [lv for lv in scanner.active_levels if lv.role is LevelRole.RESISTANCE and abs(lv.extreme - 110.2) < 1.5]
    assert len(res_eq) == 1
    assert res_eq[0].kind is LevelKind.EQUAL
    assert res_eq[0].touches >= 2

    # 随后果断突破进入 [114, 120] 新价值区活动,期间单次回踩 110.2 形成波谷支撑并迅速脱离 -> 左顶右底 FLIPPED!
    ext_closes = [
        108, 115, 117, 116, 118, 116, 115, 110.4, 115, 117, 116, 118,
        117, 119, 117, 118, 116, 118, 119, 117, 118, 119,
    ]
    base_n = len(bars)
    for j, c in enumerate(ext_closes):
        o = bars[-1][4]
        h = max(o, c) + 1.2
        l = 110.1 if abs(c - 110.4) < 0.01 else max(112.5, min(o, c) - 1.2)
        bars.append(((base_n + j) * 3_600_000, o, h, l, float(c), 10.0))

    scanner.update_canvas(bars)
    sup_flip = [lv for lv in scanner.active_levels if lv.role is LevelRole.SUPPORT and abs(lv.neck - 110.2) < 2.5]
    assert len(sup_flip) == 1
    assert sup_flip[0].kind is LevelKind.FLIPPED


def test_2d_sightline_eliminates_chopped_midrange_poc_lines() -> None:
    """被上下反复贯穿腰斩的区间中轴腰线(POC)应在二维视线检测中被淘汰,仅保留干净的上下外边界。"""
    scanner = Canvas2DLevelScanner(canvas_bars=120, max_active_per_side=8)
    bars: list[tuple[int, float, float, float, float, float]] = []
    # 价格主体长期围绕中轴 100 密集粘连穿梭(POC),仅偶发冲顶 110 与探底 90 即刻折返
    seq = [
        99, 101, 100, 90.5, 98, 101, 99, 100, 102, 109.5, 101, 99, 101, 100,
        98, 101, 99, 90.5, 98, 100, 101, 99, 102, 109.5, 101, 99, 100, 101, 99, 100,
    ]
    for i, c in enumerate(seq):
        o = float(seq[i - 1]) if i > 0 else float(c)
        h = 110.2 if c == 109.5 else min(104.0, max(o, c) + 1.2)
        l = 89.8 if c == 90.5 else max(96.0, min(o, c) - 1.2)
        bars.append((i * 3_600_000, o, h, l, float(c), 10.0))

    scanner.update_canvas(bars)
    assert not any(98.5 <= lv.neck <= 101.5 for lv in scanner.active_levels)
    assert any(lv.role is LevelRole.RESISTANCE and lv.extreme >= 109.5 for lv in scanner.active_levels)
    assert any(lv.role is LevelRole.SUPPORT and lv.extreme <= 90.5 for lv in scanner.active_levels)


def test_strict_prefix_causality_invariance() -> None:
    """严格因果不变性:截断喂入 df[:k] 与喂入全量 df 在第 k-1 拍的二维水平位、事件完全一致。"""
    df = _synthetic_wave_df(160)
    params = Params()
    full_cog = prepare_cognition(df, params, "BTCUSDT")

    for cut in (40, 85, 130):
        sub_cog = prepare_cognition(df.iloc[:cut].copy(), params, "BTCUSDT")
        t = cut - 1
        assert sub_cog.regimes[t] == full_cog.regimes[t]
        assert math.isclose(sub_cog.atrs[t], full_cog.atrs[t], rel_tol=1e-12)
        sub_lvs = [(z.role, z.kind, round(z.extreme, 8), round(z.neck, 8), z.touches) for z in sub_cog.active_zones[t]]
        full_lvs = [(z.role, z.kind, round(z.extreme, 8), round(z.neck, 8), z.touches) for z in full_cog.active_zones[t]]
        assert sub_lvs == full_lvs
        assert len(sub_cog.events_by.get(t, ())) == len(full_cog.events_by.get(t, ()))


def test_breakout_requires_tight_buildup_and_two_step_hold() -> None:
    """突破必须来自紧凑蓄势基底并经次棒维持确认,拒绝单棒假突破与长途力竭追顶。"""
    res = Level(
        uid=1, role=LevelRole.RESISTANCE, kind=LevelKind.EQUAL,
        extreme=101.0, neck=100.0, state=LevelState.TESTED,
        origin_index=2, known_index=4, origin_ts=2000, last_change_known=4, touches=2,
    )
    b0 = (9000, 99.6, 100.2, 99.4, 99.8, 10.0)
    b1 = (10000, 99.8, 101.6, 99.7, 101.4, 10.0)
    b2_fail = (11000, 101.4, 101.5, 99.2, 99.5, 10.0)

    eng_trap = LevelAuctionEngine()
    assert eng_trap.step(bar_index=6, bar=b1, prev_bar=b0, recent_bars=[b0], active_levels=[res], atr=2.0, k_bm=0.1) == []
    ev_trap = eng_trap.step(bar_index=7, bar=b2_fail, prev_bar=b1, recent_bars=[b0, b1], active_levels=[res], atr=2.0, k_bm=0.1)
    assert not any(e.id == BREAKOUT_ACCEPTED for e in ev_trap)
    assert len(ev_trap) == 1 and ev_trap[0].id == LEVEL_REJECTED and ev_trap[0].direction == -1

    eng_ok = LevelAuctionEngine()
    b2_hold = (11000, 101.3, 102.2, 101.1, 102.0, 10.0)
    eng_ok.step(bar_index=6, bar=b1, prev_bar=b0, recent_bars=[b0], active_levels=[res], atr=2.0, k_bm=0.1)
    ev_ok = eng_ok.step(bar_index=7, bar=b2_hold, prev_bar=b1, recent_bars=[b0, b1], active_levels=[res], atr=2.0, k_bm=0.1)
    assert len(ev_ok) == 1
    assert ev_ok[0].id == BREAKOUT_ACCEPTED


def test_support_rejection_micro_bos_and_ascending_triangle_protection() -> None:
    """支撑位拒绝需微观结构向上突破确认,且上升趋势中严禁逆势做空上方 EQUAL 水平平顶(上升三角形)。"""
    sup = Level(
        uid=11, role=LevelRole.SUPPORT, kind=LevelKind.EQUAL,
        extreme=100.0, neck=101.0, state=LevelState.TESTED,
        origin_index=3, known_index=5, origin_ts=3000, last_change_known=5,
        touches=2, departed=True,
    )
    prev_b = (9000, 103.5, 103.8, 102.0, 102.2, 10.0)
    touch_b = (10000, 102.2, 102.2, 99.8, 100.4, 10.0)
    shift_b = (11000, 100.4, 101.4, 100.3, 100.35, 10.0)
    shift_b2 = (12000, 100.4, 101.9, 100.35, 101.75, 10.0)

    eng = LevelAuctionEngine()
    assert eng.step(bar_index=8, bar=touch_b, prev_bar=prev_b, recent_bars=[prev_b], active_levels=[sup], atr=3.0, k_bm=0.05) == []
    eng.step(bar_index=9, bar=shift_b, prev_bar=touch_b, recent_bars=[prev_b, touch_b], active_levels=[sup], atr=3.0, k_bm=0.05)
    ev = eng.step(bar_index=10, bar=shift_b2, prev_bar=shift_b, recent_bars=[touch_b, shift_b], active_levels=[sup], atr=3.0, k_bm=0.05)
    assert len(ev) == 1
    assert ev[0].id == LEVEL_REJECTED

    router = MarketRouter()
    res_eq = Level(
        uid=22, role=LevelRole.RESISTANCE, kind=LevelKind.EQUAL,
        extreme=115.0, neck=114.0, state=LevelState.TESTED,
        origin_index=4, known_index=6, origin_ts=4000, last_change_known=6, touches=3,
    )
    short_ev = MarketEvent(
        id=LEVEL_REJECTED, direction=-1, ts=13000, known_index=10,
        attrs={"zone": res_eq, "level": res_eq, "ref": 114.0, "ex": 115.0, "invalid": 115.3, "sub_kind": "LEVEL_REJECT_EQUAL"},
    )
    view = StructureView(regime=Regime.TREND_UP, zones=(sup, res_eq), atr=2.0, pivots=(), macro_imbalance=1, tf_hours=4)
    decs = router.on_bar("BTCUSDT", (13000, 114.2, 115.0, 113.4, 113.5, 10.0), 10, view, 2.0, [short_ev])
    assert len(decs) == 0
    assert router.counters.get("rejected_counter_trend", 0) == 1


def test_live_incremental_vs_batch_replay_exact_parity() -> None:
    """单路径铁律:逐棒增量推进(模拟实盘)与批量预计算回放产出的触发事件完全一致。"""
    df_4h = _synthetic_wave_df(90, step_ms=14_400_000)
    ltf_rows: list[tuple[int, float, float, float, float, float]] = []
    step_1h = 3_600_000
    for r in df_4h.itertuples(index=False):
        ts, o, h, l, c, v = int(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)
        mid = 0.5 * (o + c)
        ltf_rows.extend([
            (ts, o, max(o, mid) + 1.0, min(o, mid) - 1.0, mid, v * 0.25),
            (ts + step_1h, mid, h, min(mid, c) - 0.5, 0.5 * (mid + h), v * 0.25),
            (ts + 2 * step_1h, 0.5 * (mid + h), max(mid, c) + 0.5, l, 0.5 * (l + c), v * 0.25),
            (ts + 3 * step_1h, 0.5 * (l + c), max(c, 0.5 * (l + c)) + 0.4, min(c, 0.5 * (l + c)) - 0.4, c, v * 0.25),
        ])
    df_1h = pd.DataFrame(ltf_rows, columns=["ts", "open", "high", "low", "close", "volume"])

    bundle = load_strategy_bundle(Path(__file__).resolve().parents[1] / "config" / "strategy.json")
    cog_batch = prepare_cognition(df_4h, bundle.params, "BTCUSDT")
    mic_batch = prepare_mic_cognition(
        cog_batch, df_1h, bundle.params, obs_tf_seconds=14400, trigger_tf_seconds=3600
    )

    live_ms = MarketStructure(bundle.params)
    live_mic = MicTracker(bundle.params, scale=4)
    htf_bars = [(int(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)) for r in df_4h.itertuples(index=False)]

    batch_evs: list[tuple[int, str, int, float]] = []
    live_evs: list[tuple[int, str, int, float]] = []
    htf_idx = 0

    for j, lb in enumerate(ltf_rows):
        for ev in mic_batch.events_by.get(j, ()):
            batch_evs.append((j, ev.id, ev.direction, round(float(ev.attrs["invalid"]), 8)))

        # 实盘顺序:若该 1H 收盘正值一根 4H 结束边缘,先更新 4H MarketStructure 再同步 MicTracker!
        if (j + 1) % 4 == 0 and htf_idx < len(htf_bars):
            live_ms.update(htf_bars[htf_idx])
            live_mic.sync_from_structure(live_ms)
            htf_idx += 1

        for ev in live_mic.update(lb):
            live_evs.append((j, ev.id, ev.direction, round(float(ev.attrs["invalid"]), 8)))

    assert batch_evs == live_evs


def test_2d_prominence_allows_major_single_touch_sweep_and_htf_wall_unlocks_top_pick():
    """封版二维几何闭环测试:
    1. 单次触碰(touches=1)但二维留白面积显著(> median)的大顶底允许流动性清扫反转;微小局部凸起(<= median)被刺穿则拒绝。
    2. 1D 单边趋势半空中禁止逆势摸顶,但当价格直接触及 1D 活跃大阻力势垒共振带时自动解锁摸顶做空!
    """
    from market_operator.events import LEVEL_REJECTED, MarketEvent
    from market_operator.hypothesis import StructureView
    from market_operator.level import Level, LevelKind, LevelRole, LevelState
    from market_operator.routing import MarketRouter
    from market_operator.structure import Regime

    major_top = Level(1, LevelRole.RESISTANCE, LevelKind.SWING, 105.0, 104.0, LevelState.FRESH, 0, 1, 0, 1, touches=1, prominence_2d=0.45)
    minor_bump = Level(2, LevelRole.RESISTANCE, LevelKind.SWING, 102.0, 101.5, LevelState.FRESH, 0, 1, 0, 1, touches=1, prominence_2d=0.05)
    sup_floor = Level(3, LevelRole.SUPPORT, LevelKind.EQUAL, 88.0, 90.0, LevelState.TESTED, 0, 1, 0, 1, touches=2, prominence_2d=0.20)
    view_4h = StructureView(Regime.RANGE, (major_top, minor_bump, sup_floor), atr=1.0)

    router = MarketRouter()
    ev_major = MarketEvent(LEVEL_REJECTED, -1, 1000, 1, {"level": major_top, "zone": major_top, "invalid": 106.0, "ex": 105.5, "swept": True})
    ev_minor = MarketEvent(LEVEL_REJECTED, -1, 1000, 1, {"level": minor_bump, "zone": minor_bump, "invalid": 103.0, "ex": 102.5, "swept": True})

    # 1) 2D white-space prominence distinguishes major 1-touch SFP from minor noise bump without touch-counting bias:
    assert len(router.on_bar("X", (1000, 104.5, 105.5, 102.5, 103.0, 10.0), 1, view_4h, 1.0, [ev_minor])) == 0
    assert len(router.on_bar("X", (1000, 104.5, 105.5, 102.5, 103.0, 10.0), 1, view_4h, 1.0, [ev_major])) == 1

    # 2) Against 1D TREND_UP: mid-air short is blocked, but short anchored at 1D Resistance wall [104.2, 106.0] is unlocked!
    htf_midair = (StructureView(Regime.TREND_UP, (Level(9, LevelRole.RESISTANCE, LevelKind.ORIGIN, 120.0, 118.0, LevelState.FRESH, 0, 1, 0, 1),), atr=3.0),)
    htf_at_ceiling = (StructureView(Regime.TREND_UP, (Level(9, LevelRole.RESISTANCE, LevelKind.ORIGIN, 106.0, 104.2, LevelState.FRESH, 0, 1, 0, 1),), atr=3.0),)

    r2 = MarketRouter()
    assert len(r2.on_bar("Y", (2000, 104.5, 105.5, 102.5, 103.0, 10.0), 2, view_4h, 1.0, [ev_major], htf_views=htf_midair)) == 0
    assert len(r2.on_bar("Y", (2000, 104.5, 105.5, 102.5, 103.0, 10.0), 2, view_4h, 1.0, [ev_major], htf_views=htf_at_ceiling)) == 1


def test_cash_net_admission_frees_floating_profit_and_hedges_filled_beta(tmp_path):
    """安全管理与资金周转闭环测试(回放 Broker 与实盘 LiveRunner 同源一致):
    1. 本金风险对齐:已持仓标的产生浮盈(mark > entry)时,不倒扣已实现现金风险额度,不误杀后续同向信号;
    2. 已成交反向 Beta 对冲抵扣:已成交反向持仓抵扣系统性同向崩盘敞口,而未成交挂单绝不抵扣,总组合上限依然严守。
    """
    import pytest
    from market_operator import capacity
    from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind
    from tests.test_live_runtime import _FakeClient, _SignalTG, _pos, _runner

    syms = ("BTCUSDT@4h", "ETHUSDT@4h", "SOLUSDT@4h")
    specs = {s: ContractSpec(s, 0.001, 0.001, 3, 2, 0.0) for s in syms}
    b = Broker(
        equity=1000.0,
        risk_frac=0.05,
        leverage_cap=20.0,
        taker_fee=0.0,
        slippage_per_side=0.0,
        funding_per_8h=0.0,
        contracts=specs,
        portfolio_stop_risk_frac=0.18,
        directional_stop_risk_frac=0.11,
        capacity_mode=capacity.CASH_NET_ADMISSION,
    )
    # 1) Open BTC Long @ 100, stop 90 (principal cash risk = 50 = 5% of 1000)
    b.submit(OrderIntent(OrderKind.OPEN, "BTCUSDT@4h", None, "T", 100.0, 0), 0, direction=1, invalid=90.0, target_1=130.0)
    b.on_bar_open("BTCUSDT@4h", 3600_000, 100.0)
    # BTC surges to 125 (+2.5R unrealized floating gain!). Under cash_net_admission, principal cash risk stays 50 (not 175!), so ETH Long (50) is admitted!
    b.last_mark["BTCUSDT@4h"] = 125.0
    assert b.directional_usage(1, moment=capacity.DECISION) == pytest.approx(50.0)
    b.submit(OrderIntent(OrderKind.OPEN, "ETHUSDT@4h", None, "T", 100.0, 3600_000), 3600_000, direction=1, invalid=90.0, target_1=130.0)
    b.on_bar_open("ETHUSDT@4h", 7200_000, 100.0)
    assert len(b.positions) == 2

    # 2) Now Long usage = 100 (10% vs 11% cap), 3rd Long (50) would exceed 110 -> blocked without hedge;
    #    Once a FILLED Short (SOL Short, risk 50) opens, net Long Beta drops to 50 -> 3rd Long is unlocked (while Gross 150 <= 180 portfolio cap)!
    assert b.directional_blocked(1, 100.0, 90.0, "SOLUSDT@4h", moment=capacity.DECISION)
    b.submit(OrderIntent(OrderKind.OPEN, "SOLUSDT@4h", None, "T", 100.0, 7200_000), 7200_000, direction=-1, invalid=110.0, target_1=70.0)
    # Pending (unfilled) Short must NOT offset Long directional usage yet:
    assert b.directional_usage(1, moment=capacity.DECISION) == pytest.approx(100.0)
    b.on_bar_open("SOLUSDT@4h", 10800_000, 100.0)
    # Once filled, Short offsets systemic crash risk -> net Long usage drops to 50!
    assert b.directional_usage(1, moment=capacity.DECISION) == pytest.approx(50.0)

    # 3) LiveRunner parity check:
    runner = _runner(tmp_path, _FakeClient(), _SignalTG())
    runner.capacity_mode = capacity.CASH_NET_ADMISSION
    runner.equity = 1250.0
    runner.unrealized_pl = 250.0  # realized capital = 1000.0
    runner.directional_cap_frac = 0.11
    runner.positions["BTCUSDT"] = {**_pos(qty=5.0), "entry": 100.0, "mark": 150.0, "invalid": 90.0, "direction": 1}
    # Floating profit (mark=150 > entry=100) does NOT block 2nd Long of qty=5 (stop risk 50):
    assert not runner._directional_entry_blocked(1, 5.0, 100.0, 90.0)


