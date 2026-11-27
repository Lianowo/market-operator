"""二维 (t, P) 空间层析原子水平位系统、因果无未来函数、拍卖触发与实盘一致性测试。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

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
    b0_absorb = (9500, 99.8, 100.6, 99.7, 100.4, 10.0)
    assert eng_ok.step(bar_index=5, bar=b0_absorb, prev_bar=b0, recent_bars=[b0], active_levels=[res], atr=2.0, k_bm=0.1) == []
    ev_ok = eng_ok.step(bar_index=6, bar=b1, prev_bar=b0_absorb, recent_bars=[b0, b0_absorb], active_levels=[res], atr=2.0, k_bm=0.1)
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


def test_crypto_vs_rwa_independent_directional_buckets_and_session_gate(tmp_path):
    """跨资产(Crypto vs RWA 非加密传统合约)独立同向风险分桶 + RWA 休市禁开新仓测试:
    1. Crypto 同向池打满时阻塞新增 Crypto 同向单,但放行零相关的 RWA(如 NVDAUSDT/XAUUSDT)同向单;
    2. 全账户总风险上限 portfolio_cap_frac 依然统一硬封顶跨资产总敞口;
    3. RWA 合约的时段闸门按"标的→日历"判定:未登记标的走 weekday_only 兜底(周末关闭),命名族规则(外汇/HKD/TW)与运维覆盖文件见 tests/test_sessions_calendar.py;Crypto 24x7 不受限。
    """
    from datetime import datetime, timezone
    from market_operator import capacity
    from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind
    from market_operator.engine import rwa_session_entry_block
    from tests.test_live_runtime import _FakeClient, _SignalTG, _pos, _runner

    specs = {
        "BTCUSDT@4h": ContractSpec("BTCUSDT@4h", 0.001, 0.001, 3, 2, 0.0, is_rwa=False),
        "ETHUSDT@4h": ContractSpec("ETHUSDT@4h", 0.001, 0.001, 3, 2, 0.0, is_rwa=False),
        "SOLUSDT@4h": ContractSpec("SOLUSDT@4h", 0.001, 0.001, 3, 2, 0.0, is_rwa=False),
        "NVDAUSDT@4h": ContractSpec("NVDAUSDT@4h", 0.01, 0.01, 2, 2, 0.0, is_rwa=True),
        "XAUUSDT@4h": ContractSpec("XAUUSDT@4h", 0.01, 0.01, 2, 2, 0.0, is_rwa=True),
    }
    assert ContractSpec.from_entry({
        "symbol": "NVDAUSDT", "minTradeNum": "0.01", "sizeMultiplier": "0.01",
        "volumePlace": "2", "pricePlace": "2", "minTradeUSDT": "5", "isRwa": "YES",
    }).is_rwa is True

    # Total portfolio cap = 20% (200U), per-asset-class directional cap = 11% (110U), per-trade = 5% (50U)
    b = Broker(
        equity=1000.0, risk_frac=0.05, leverage_cap=20.0,
        taker_fee=0.0, slippage_per_side=0.0, funding_per_8h=0.0,
        contracts=specs,
        portfolio_stop_risk_frac=0.20,
        directional_stop_risk_frac=0.11,
        capacity_mode=capacity.CASH_NET_ADMISSION,
    )
    b.submit(OrderIntent(OrderKind.OPEN, "BTCUSDT@4h", None, "T", 100.0, 0), 0, direction=1, invalid=90.0, target_1=130.0)
    b.submit(OrderIntent(OrderKind.OPEN, "ETHUSDT@4h", None, "T", 100.0, 0), 0, direction=1, invalid=90.0, target_1=130.0)
    b.on_bar_open("BTCUSDT@4h", 3600_000, 100.0)
    b.on_bar_open("ETHUSDT@4h", 3600_000, 100.0)

    # Crypto Long bucket = 100U -> 3rd Crypto Long (SOL, +50U => 150U > 110U) MUST be blocked:
    assert b.directional_blocked(1, 100.0, 90.0, "SOLUSDT@4h", moment=capacity.DECISION)
    # Uncorrelated RWA Long bucket = 0U -> NVDAUSDT Long (+50U <= 110U, total 150U <= 200U) MUST be allowed!
    assert not b.directional_blocked(1, 100.0, 90.0, "NVDAUSDT@4h", moment=capacity.DECISION)

    # LiveRunner parity for Crypto vs RWA directional bucket:
    runner = _runner(tmp_path, _FakeClient(), _SignalTG())
    runner.capacity_mode = capacity.CASH_NET_ADMISSION
    runner.equity = 1000.0
    runner.unrealized_pl = 0.0
    runner.directional_cap_frac = 0.11
    runner.contracts["NVDAUSDT"] = ContractSpec("NVDAUSDT", 0.01, 0.01, 2, 2, 5.0, is_rwa=True)
    runner.positions["BTCUSDT"] = {**_pos(qty=5.0), "entry": 100.0, "mark": 100.0, "invalid": 90.0, "direction": 1}
    runner.positions["ETHUSDT"] = {**_pos(qty=5.0), "entry": 100.0, "mark": 100.0, "invalid": 90.0, "direction": 1}
    assert runner._directional_entry_blocked(1, 5.0, 100.0, 90.0, symbol="SOLUSDT")
    assert not runner._directional_entry_blocked(1, 5.0, 100.0, 90.0, symbol="NVDAUSDT")

    # Session gate checks:未登记标的 = weekday_only 兜底(周五夜间放行、周末关闭);
    # 各资产类别的真实日历(美股 RTH/DST/假日/半日市、CME、FX、港日韩台印)在
    # tests/test_sessions_calendar.py 里用显式 cfg/overrides 逐条锁定。
    sat_noon = int(datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)   # Saturday 12:00 UTC
    fri_late = int(datetime(2026, 11, 13, 21, 30, tzinfo=timezone.utc).timestamp() * 1000)  # Friday 21:30 UTC
    sun_open = int(datetime(2026, 11, 15, 22, 30, tzinfo=timezone.utc).timestamp() * 1000)  # Sunday 22:30 UTC
    wed_us = int(datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)     # Wednesday 15:00 UTC
    active_bar = (wed_us, 160.0, 162.0, 159.5, 161.5, 12000.0)
    flat_bar = (wed_us, 160.0, 160.0, 160.0, 160.0, 0.0)
    unassigned = "ZZZUSDT"                     # 没有命名族/覆盖记录 ⇒ weekday_only

    legacy = {}                            # 显式关闭运维 24x7 覆盖,锁定日历口径
    assert rwa_session_entry_block(False, sat_noon, flat_bar, overrides=legacy) is None
    assert rwa_session_entry_block(
        True, sat_noon, active_bar, symbol=unassigned,
        overrides=legacy) == "RWA_MARKET_CLOSED"
    assert rwa_session_entry_block(
        True, fri_late, active_bar, symbol=unassigned,
        overrides=legacy) is None
    assert rwa_session_entry_block(
        True, sun_open, active_bar, symbol=unassigned,
        overrides=legacy) == "RWA_MARKET_CLOSED"
    assert rwa_session_entry_block(
        True, wed_us, active_bar, symbol=unassigned,
        overrides=legacy) is None
    assert rwa_session_entry_block(
        True, wed_us, flat_bar, symbol=unassigned,
        overrides=legacy) == "RWA_INACTIVE_SESSION"


def test_universe_crypto_3m_and_rwa_unfiltered():
    """双轨宇宙筛选规则验证:
    1. Crypto(isRwa != 'YES')严格要求 24h 成交额 > 3,000,000 USDT;
    2. 非 Crypto(isRwa == 'YES' 且 symbolStatus == 'normal')不做成交量过滤(即使周末 24h 成交额为 0 亦保留),停盘下架合约除外。
    """
    from market_operator.bitget_live import BitgetLiveClient

    cli = BitgetLiveClient(None)
    contracts = [
        {"symbol": "BTCUSDT", "isRwa": "NO", "symbolStatus": "normal"},
        {"symbol": "LOWCOINUSDT", "isRwa": "NO", "symbolStatus": "normal"},
        {"symbol": "EXACT3MUSDT", "isRwa": "NO", "symbolStatus": "normal"},
        {"symbol": "AMDUSDT", "isRwa": "YES", "symbolStatus": "normal"},
        {"symbol": "NVDAUSDT", "isRwa": "YES", "symbolStatus": "normal"},
        {"symbol": "DELISTRWAUSDT", "isRwa": "YES", "symbolStatus": "off"},
    ]
    tickers = [
        {"symbol": "BTCUSDT", "usdtVolume": "50000000"},
        {"symbol": "LOWCOINUSDT", "usdtVolume": "1200000"},
        {"symbol": "EXACT3MUSDT", "usdtVolume": "3000000"},
        {"symbol": "AMDUSDT", "usdtVolume": "0"},
        {"symbol": "NVDAUSDT", "usdtVolume": "800000"},
        {"symbol": "DELISTRWAUSDT", "usdtVolume": "9000000"},
    ]
    picked = cli.universe(
        3_000_000.0,
        tickers=tickers,
        rwa_no_volume_filter=True,
        contracts=contracts,
    )
    assert picked == ["AMDUSDT", "BTCUSDT", "NVDAUSDT"]


def test_live_config_rwa_volume_floor_3m():
    """实盘配置口径:Crypto 24h 成交额门槛 5M(剔除极薄山寨),RWA 3M(严格大于),且运维日历处于 calendar 模式。"""
    from market_operator.bitget_live import BitgetLiveClient

    root = Path(__file__).resolve().parents[1]
    cfg = json.loads((root / "config" / "live.json").read_text(encoding="utf-8"))
    uni_cfg = cfg["universe"]
    assert uni_cfg["crypto"]["min_usdt_volume_24h"] == 5_000_000.0
    assert uni_cfg["rwa"]["min_usdt_volume_24h"] == 3_000_000.0
    assert uni_cfg["rwa_no_volume_filter"] is False

    ovr = json.loads((root / "data" / "calendars" / "overrides.json").read_text(encoding="utf-8"))
    assert ovr["mode"] == "calendar"

    cli = BitgetLiveClient(None)
    contracts = [
        {"symbol": "BTCUSDT", "isRwa": "NO", "symbolStatus": "normal"},
        {"symbol": "THINCOINUSDT", "isRwa": "NO", "symbolStatus": "normal"},
        {"symbol": "AMDUSDT", "isRwa": "YES", "symbolStatus": "normal"},
        {"symbol": "NVDAUSDT", "isRwa": "YES", "symbolStatus": "normal"},
    ]
    tickers = [
        {"symbol": "BTCUSDT", "usdtVolume": "50000000"},
        {"symbol": "THINCOINUSDT", "usdtVolume": "4200000"},  # Crypto <= 5M ⇒ 不入选
        {"symbol": "AMDUSDT", "usdtVolume": "3000000"},       # RWA 恰 3M ⇒ 不入选
        {"symbol": "NVDAUSDT", "usdtVolume": "3200000"},      # RWA > 3M ⇒ 入选
    ]
    assert cli.universe(uni_cfg, tickers=tickers,
                        contracts=contracts) == ["BTCUSDT", "NVDAUSDT"]


def test_canvas2d_band_width_bounded_by_intrinsic_p90_range():
    """画布内生振幅分位(Q75/P90)约束:单根极端巨幅长影线/阴线与宽横盘簇不得把水平带拉穿画布 P90 单棒振幅。"""
    from market_operator.level import Canvas2DLevelScanner

    bars = []
    px = 100.0
    for i in range(120):
        ts = i * 14_400_000
        # 前半段构造清晰的高低波段,末段停在区间中枢(100)确保上下两侧存在未被击穿的活跃水平位
        if i < 90:
            amp = 6.0 if i < 60 else 3.0
            o = px + amp * math.sin(i / 5.0)
            c = px + amp * math.sin((i + 1) / 5.0)
        else:
            o = px + 0.8 * math.sin(i / 3.0)
            c = px + 0.8 * math.sin((i + 1) / 3.0)
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        if i == 70:
            # 注入极端巨幅冲高长阴线(模仿 PROMUSDT/极端插针),高点 118,实体收在 102(原始影线/实体跨度 16 >> P90)
            o, h, l, c = 114.0, 118.0, 101.0, 102.0
        bars.append((ts, o, h, l, c, 1000.0))

    sc = Canvas2DLevelScanner(canvas_bars=160, max_active_per_side=8)
    sc.update_canvas(bars)
    levels = sc.active_levels
    assert len(levels) > 0
    assert sc._p90_range > 0.0
    assert 16.0 > 3.0 * sc._p90_range
    for lv in levels:
        bw = lv.band_hi - lv.band_lo
        assert bw <= sc._p90_range + 1e-9, f"水平带过宽: bw={bw} > p90_range={sc._p90_range}"
        assert lv.band_lo <= lv.probe_lo <= lv.probe_hi <= lv.band_hi


def test_production_configs_align_1d_accept_and_quote_guard():
    """锁定已验证落盘的生产配置:1d->4h ACCEPT、因子并发上限与 RWA quote_guard 已双端对齐。"""
    root = Path(__file__).resolve().parents[1]
    strat = json.loads((root / "config" / "strategy.json").read_text(encoding="utf-8"))
    live = json.loads((root / "config" / "live.json").read_text(encoding="utf-8"))
    assert strat["trigger_tf_by_htf"]["1d"] == 14400
    assert live["cognition"]["trigger_tf_by_htf"]["1d"] == 14400
    assert sorted(strat["router_params_by_tf"]["1d"]["whitelist"]) == [["ACCEPT", -1], ["ACCEPT", 1]]
    assert live["execution"]["quote_guard"]["enabled"] is True
    assert strat["factor_burst_cap_1h"] == 1
    assert strat["factor_burst_cap_4h"] == 2
    assert strat["router_params"]["conviction_sizing"] is True
    assert live["risk"]["factor_burst_cap_1h"] == 1
    assert live["risk"]["factor_burst_cap_4h"] == 2


def test_factor_burst_cap_broker_and_live_runner_exact_parity(tmp_path):
    """单棒因子并发上限(1h=1, 4h=2)在 Broker 与 LiveRunner 中按资产类×方向独立计数、严格同构。"""
    from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind
    from tests.test_live_runtime import _FakeClient, _SignalTG, _runner

    specs = {
        "BTCUSDT@4h": ContractSpec("BTCUSDT", 0.01, 0.01, 2, 2, 5.0, is_rwa=False),
        "ETHUSDT@4h": ContractSpec("ETHUSDT", 0.01, 0.01, 2, 2, 5.0, is_rwa=False),
        "SOLUSDT@4h": ContractSpec("SOLUSDT", 0.01, 0.01, 2, 2, 5.0, is_rwa=False),
        "XRPUSDT@4h": ContractSpec("XRPUSDT", 0.01, 0.01, 2, 2, 5.0, is_rwa=False),
        "NVDAUSDT@4h": ContractSpec("NVDAUSDT", 0.01, 0.01, 2, 2, 5.0, is_rwa=True),
    }
    b = Broker(
        equity=1000.0, risk_frac=0.05, leverage_cap=20.0, taker_fee=0.0,
        slippage_per_side=0.0, funding_per_8h=0.0, contracts=specs,
        portfolio_stop_risk_frac=0.50, directional_stop_risk_frac=0.50,
        factor_burst_cap_1h=1, factor_burst_cap_4h=2,
    )
    t0 = 1_700_006_400_000  # 整 4h 桶起点 (能被 14_400_000 整除)
    assert t0 % 14_400_000 == 0
    # 同 1h 内第 1 个加密多头成交,第 2 个被 FACTOR_BURST_CAP_1H 拦截;不同资产类 RWA 多头独立放行
    b.submit(OrderIntent(OrderKind.OPEN, "BTCUSDT@4h", None, "T", 100.0, t0 - 1), t0 - 1, direction=1, invalid=90.0, target_1=130.0)
    b.submit(OrderIntent(OrderKind.OPEN, "ETHUSDT@4h", None, "T", 100.0, t0 - 1), t0 - 1, direction=1, invalid=90.0, target_1=130.0)
    b.submit(OrderIntent(OrderKind.OPEN, "NVDAUSDT@4h", None, "T", 100.0, t0 - 1), t0 - 1, direction=1, invalid=90.0, target_1=130.0)
    r1 = b.on_bar_open("BTCUSDT@4h", t0, 100.0)
    r2 = b.on_bar_open("ETHUSDT@4h", t0, 100.0)
    r3 = b.on_bar_open("NVDAUSDT@4h", t0, 100.0)
    assert r1[0]["kind"] == "OPEN"
    assert r2[0]["kind"] == "SKIP" and r2[0]["reason_code"] == "FACTOR_BURST_CAP_1H"
    assert r3[0]["kind"] == "OPEN"

    # 下一小时(仍在同一 4h 桶)允许第 2 个加密多头成交;再下一小时(仍同一 4h 桶)第 3 个被 FACTOR_BURST_CAP_4H 拦截
    t1 = t0 + 3_600_000
    t2 = t0 + 7_200_000
    b.submit(OrderIntent(OrderKind.OPEN, "SOLUSDT@4h", None, "T", 100.0, t1 - 1), t1 - 1, direction=1, invalid=90.0, target_1=130.0)
    assert b.on_bar_open("SOLUSDT@4h", t1, 100.0)[0]["kind"] == "OPEN"
    b.submit(OrderIntent(OrderKind.OPEN, "XRPUSDT@4h", None, "T", 100.0, t2 - 1), t2 - 1, direction=1, invalid=90.0, target_1=130.0)
    r4 = b.on_bar_open("XRPUSDT@4h", t2, 100.0)
    assert r4[0]["kind"] == "SKIP" and r4[0]["reason_code"] == "FACTOR_BURST_CAP_4H"

    # LiveRunner 同构核验
    runner = _runner(tmp_path, _FakeClient(), _SignalTG())
    runner.risk["factor_burst_cap_1h"] = 1
    runner.risk["factor_burst_cap_4h"] = 2
    runner.contracts["NVDAUSDT"] = ContractSpec("NVDAUSDT", 0.01, 0.01, 2, 2, 5.0, is_rwa=True)
    runner.positions["BTCUSDT"] = {"symbol": "BTCUSDT", "direction": 1, "opened_ts": t0}
    assert runner._factor_burst_blocked("ETHUSDT", 1, t0) == "FACTOR_BURST_CAP_1H"
    assert runner._factor_burst_blocked("ETHUSDT", -1, t0) is None
    assert runner._factor_burst_blocked("NVDAUSDT", 1, t0) is None
    runner.positions["SOLUSDT"] = {"symbol": "SOLUSDT", "direction": 1, "opened_ts": t1}
    assert runner._factor_burst_blocked("XRPUSDT", 1, t2) == "FACTOR_BURST_CAP_4H"
    assert runner._factor_burst_blocked("XRPUSDT", 1, t0 + 14_400_000) is None


def test_two_scale_brownian_diffusion_and_peer_excess_returns():
    """纯 K 线双尺度 Lo-MacKinlay 方差比 VR(4) 与无状态横截面超额收益严格正确性测试。"""
    import math
    from market_operator.structure import variance_ratio
    from market_operator.engine import compute_peer_excess_returns, peer_lookback_bars
    from market_operator.trigger import MicTracker

    # 1) 超扩散持续趋势流 (正自相关) => VR(4) > 1.0; 亚扩散交替锯齿流 (负自相关) => VR(4) < 1.0
    trend = [100.0]
    for i in range(60):
        step = 0.015 if (i // 4) % 2 == 0 else -0.012
        trend.append(trend[-1] * math.exp(step))
    chop = [100.0]
    for i in range(60):
        step = 0.015 if i % 2 == 0 else -0.015
        chop.append(chop[-1] * math.exp(step))
    assert variance_ratio(trend, q=4) > 1.0
    assert variance_ratio(chop, q=4) < 1.0
    assert variance_ratio([100.0] * 10, q=4) == 1.0
    assert variance_ratio([100.0] * 50, q=4) == 1.0

    # 2) 无状态跨品种超额收益: 纯棒数空间(与 tf_hours 无关), 同伴 >= 4 时返回本标的 - 中位数, 不足 4 个时退回 0.0
    assert peer_lookback_bars(4) == (10, 32)
    assert peer_lookback_bars(24) == (10, 32)
    assert peer_lookback_bars(1) == (10, 32)
    ex48, ex120 = compute_peer_excess_returns(0.10, -0.05, [0.01, 0.02, 0.03, 0.04], [0.0, 0.01, 0.02, 0.03])
    assert ex48 == pytest.approx(0.10 - 0.025)
    assert ex120 == pytest.approx(-0.05 - 0.015)
    assert compute_peer_excess_returns(0.10, -0.05, [0.01, 0.02, 0.03], [0.0, 0.01, 0.02]) == (0.0, 0.0)

    # 3) MicTracker 触发量能比在无成交量数据源(volume=0, 如即期外汇)下保持中性 (1.0, 1.0)
    trk = MicTracker(scale=4)
    for k in range(25):
        trk.update((k * 3600_000, 1.10, 1.11, 1.09, 1.105, 0.0))
    assert trk.trig_volume_ratios(20) == (1.0, 1.0)
    trk_v = MicTracker(scale=4)
    for k in range(24):
        trk_v.update((k * 3600_000, 100.0, 101.0, 99.0, 100.5, 10.0))
    trk_v.update((24 * 3600_000, 100.0, 101.0, 99.0, 100.5, 25.0))
    v_rel, p3_rel = trk_v.trig_volume_ratios(20)
    assert v_rel == pytest.approx(2.5)
    assert p3_rel == pytest.approx(1.0)


def test_router_kelly_conviction_sizing_monotonic_and_zero_rejection():
    """100% 零成交量纯 OHLC 七维闭式凯利信念分级定仓(Phase 4: 多倍频程无尺度 + Q75 + 2*pts/7):
    - 0 分全负状态(亚扩散 + 追涨衰竭 + 相对弱势)被 rejected_zero_conviction 拦截;
    - 死水晚追突破(cog_park_ratio < 1.0 且 er_dir > 1.0)被 rejected_dead_vol_exhausted 拦截;
    - 7 分满格共振获精确闭式 2*7/7 = 2.0x 与最高龙头优先级,且完全不依赖成交量(vol=0 与 vol=99 逐位一致);
    - 拒绝事件采用严格纯 OHLC 影线三分对称(wick>=1/3, wick>0) + 内生 Q75 振幅(>=1.0),并放开严格时间分离的 FLIPPED;
    - 动态场所摩擦成本 resolve_cost_per_side 按 taker_fee + slippage_by_symbol[sym] 自动派生;
    - 默认 conviction_sizing=False 保持既有行为零侵入。
    """
    import math
    from market_operator.engine import markowitz_corr_multiplier, resolve_cost_per_side
    from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
    from market_operator.hypothesis import StructureView
    from market_operator.level import Level, LevelKind, LevelRole, LevelState
    from market_operator.routing import MarketRouter, RouterParams
    from market_operator.structure import (
        Regime,
        multioctave_brownian_er,
        multioctave_parkinson_ratio,
        parkinson_vol_ratio,
        path_efficiency,
    )

    res_lv = Level(
        uid=1, role=LevelRole.RESISTANCE, kind=LevelKind.ORIGIN,
        extreme=100.0, neck=99.5, state=LevelState.FRESH,
        origin_index=10, known_index=12, origin_ts=0, last_change_known=12,
        prominence_2d=5.0,
    )
    ev = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=1, ts=100_000, known_index=20,
        attrs={"level": res_lv, "zone": res_lv, "invalid": 98.0, "ex": 102.0, "ref": 100.0, "sub_kind": "LEVEL_ACCEPT_ORIGIN"},
    )

    # 1) 0 分死盘突破(cog_vr4<1, trig_vr4>=1, body_eff=0.90>2/3, excess<0) => 直接拦截
    vw_bad = StructureView(
        regime=Regime.TREND_UP, zones=(res_lv,), atr=2.0, tf_hours=4,
        imbalance_strength=1.5, cog_vr4=0.75, trig_vr4=1.35, vol_rel=0.0, pre3_vol_rel=0.0,
        cog_park_ratio=1.10, cog_er_norm=0.20,
    )
    climax_bar = (100_000, 100.1, 102.1, 100.0, 102.0, 0.0)  # body_eff = 1.9/2.1 = 0.905 > 2/3
    r_conv = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert r_conv.on_bar("LAGGARD", climax_bar, 20, vw_bad, 2.0, [ev], universe_size=10, excess_ret_48h=-0.05) == []
    assert r_conv.counters.get("rejected_zero_conviction") == 1

    # 1b) 宏观多倍频程波动萎缩(<1.0) 且布朗归一路径效率透支(ER_norm > 1.0) 的死水晚追突破 => rejected_dead_vol_exhausted
    vw_dead_exh = StructureView(
        regime=Regime.TREND_UP, zones=(res_lv,), atr=2.0, tf_hours=4,
        imbalance_strength=1.5, cog_vr4=1.35, trig_vr4=0.80,
        cog_park_ratio=0.75, cog_er_norm=1.50,
    )
    coiled_bar = (100_000, 100.5, 102.0, 100.0, 101.5, 0.0)  # body_eff = 1.0/2.0 = 0.50 <= 2/3, close > mid(101.0)
    r_dead = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert r_dead.on_bar("EXHAUSTED", coiled_bar, 20, vw_dead_exh, 2.0, [ev], universe_size=10, excess_ret_48h=0.08) == []
    assert r_dead.counters.get("rejected_dead_vol_exhausted") == 1

    # 2) 6 分满格等权相变龙头(零成交量 volume=0 与巨量 volume=99 逐位一致) => 精确单位均值线性映射 0.5 + (6-1)/5 = 1.5x
    vw_ace_novol = StructureView(
        regime=Regime.TREND_UP, zones=(res_lv,), atr=2.0, tf_hours=4,
        imbalance_strength=1.5, cog_vr4=1.35, trig_vr4=0.80, vol_rel=0.0, pre3_vol_rel=0.0,
        cog_park_ratio=1.25, cog_er_norm=0.45,
    )
    vw_ace_hivol = StructureView(
        regime=Regime.TREND_UP, zones=(res_lv,), atr=2.0, tf_hours=4,
        imbalance_strength=1.5, cog_vr4=1.35, trig_vr4=0.80, vol_rel=99.0, pre3_vol_rel=99.0,
        cog_park_ratio=1.25, cog_er_norm=0.45,
    )
    r_ace = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    r_ace2 = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    decs_ace = r_ace.on_bar("LEADER", coiled_bar, 20, vw_ace_novol, 2.0, [ev], universe_size=10, excess_ret_48h=0.08)
    decs_ace2 = r_ace2.on_bar("LEADER", coiled_bar, 20, vw_ace_hivol, 2.0, [ev], universe_size=10, excess_ret_48h=0.08)
    assert len(decs_ace) == 1 and len(decs_ace2) == 1
    assert decs_ace[0].meta["conviction_score"] == 6
    assert decs_ace[0].meta["size_mult"] == pytest.approx(1.5)
    assert decs_ace[0].meta["size_mult"] == decs_ace2[0].meta["size_mult"]
    assert decs_ace[0].meta["priority_score"] == decs_ace2[0].meta["priority_score"]

    # 3) 2 分中低格(cog_vr4=1.10 等权得 1 分 + 波动觉醒 1 分, 线性透支 er_norm=1.40 无新基地分) => 0.5 + (2-1)/5 = 0.70x
    vw_mid = StructureView(
        regime=Regime.TREND_UP, zones=(res_lv,), atr=2.0, tf_hours=4,
        imbalance_strength=1.5, cog_vr4=1.10, trig_vr4=1.20,
        cog_park_ratio=1.15, cog_er_norm=1.40,
    )
    r_mid = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    decs_mid = r_mid.on_bar("MID", climax_bar, 20, vw_mid, 2.0, [ev], universe_size=10, excess_ret_48h=-0.02)
    assert len(decs_mid) == 1
    assert decs_mid[0].meta["conviction_score"] == 2
    assert decs_mid[0].meta["size_mult"] == pytest.approx(0.7)
    assert decs_ace[0].meta["priority_score"] > decs_mid[0].meta["priority_score"]

    # 3b) 100% 多空对称拒绝: ORIGIN/强 SWEEP_TRAP 拒绝获得 6 分满格 1.5x; lv.flipped 回踩在多空双向对称拦截
    orig_sup = Level(
        uid=2, role=LevelRole.SUPPORT, kind=LevelKind.ORIGIN,
        extreme=100.0, neck=100.5, state=LevelState.FRESH,
        origin_index=8, known_index=15, origin_ts=0, last_change_known=15,
        prominence_2d=5.0, flipped=False,
    )
    flip_sup = Level(
        uid=22, role=LevelRole.SUPPORT, kind=LevelKind.FLIPPED,
        extreme=100.0, neck=100.5, state=LevelState.FLIPPED,
        origin_index=8, known_index=15, origin_ts=0, last_change_known=15,
        prominence_2d=5.0, flipped=True,
    )
    minor_sup = Level(
        uid=3, role=LevelRole.SUPPORT, kind=LevelKind.SWING,
        extreme=92.0, neck=92.5, state=LevelState.FRESH,
        origin_index=4, known_index=10, origin_ts=0, last_change_known=10,
        prominence_2d=1.0,
    )
    far_res = Level(
        uid=4, role=LevelRole.RESISTANCE, kind=LevelKind.SWING,
        extreme=120.0, neck=119.5, state=LevelState.FRESH,
        origin_index=2, known_index=8, origin_ts=0, last_change_known=8,
        prominence_2d=0.5,
    )
    ev_rej = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=100_000, known_index=20,
        attrs={"level": orig_sup, "zone": orig_sup, "invalid": 99.0, "ex": 99.5, "ref": 100.0, "swept": True, "sub_kind": "SWEEP_TRAP"},
    )
    ev_flip = MarketEvent(
        id=LEVEL_REJECTED, direction=1, ts=100_000, known_index=20,
        attrs={"level": flip_sup, "zone": flip_sup, "invalid": 99.0, "ex": 99.5, "ref": 100.0, "sub_kind": "FLIP_RETEST"},
    )
    vw_rej = StructureView(
        regime=Regime.TREND_UP, zones=(orig_sup, minor_sup, far_res), atr=2.0, tf_hours=4,
        imbalance_strength=1.5, cog_vr4=1.30, trig_park_ratio=1.20, bar_range_q75_rel=1.15,
    )
    # 下影线 = (101.2 - 100.0)/2.0 = 0.60 >= 1/3, 实体 = 0.6/2.0 = 0.30 <= 2/3
    hammer_bar = (100_000, 101.2, 102.0, 100.0, 101.8, 0.0)
    r_rej = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    decs_rej = r_rej.on_bar("SWEEP_OK", hammer_bar, 20, vw_rej, 2.0, [ev_rej], universe_size=10)
    assert len(decs_rej) == 1
    assert decs_rej[0].meta["conviction_score"] == 6
    assert decs_rej[0].meta["size_mult"] == pytest.approx(1.5)
    r_flip = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert r_flip.on_bar("FLIP_BLK", hammer_bar, 20, vw_rej, 2.0, [ev_flip], universe_size=10) == []

    # 4) 默认关闭 conviction_sizing=False 保持原样(无 conviction_score, 不拦截)
    r_off = MarketRouter(RouterParams(conviction_sizing=False, cost_per_side=0.0002))
    decs_off = r_off.on_bar("LAGGARD", climax_bar, 20, vw_bad, 2.0, [ev], universe_size=10, excess_ret_48h=-0.05)
    assert len(decs_off) == 1
    assert "conviction_score" not in decs_off[0].meta

    # 5) 多倍频程 Parkinson 波动觉醒比、布朗归一路径效率比、Markowitz 协方差与动态场所摩擦成本恒等式
    quiet = [(k * 1000, 100.0, 100.5, 99.5, 100.0, 0.0) for k in range(56)]
    woke = [(k * 1000, 100.0, 104.0, 96.0, 100.0, 0.0) for k in range(56, 64)]
    assert parkinson_vol_ratio(quiet[26:] + woke, 8, 38) > 1.5
    assert multioctave_parkinson_ratio(quiet + woke, canvas_bars=160) > 1.3
    assert parkinson_vol_ratio(quiet[:10], 10, 40) == pytest.approx(1.0)
    assert path_efficiency([100.0 + k for k in range(21)]) == pytest.approx(1.0)
    assert path_efficiency([100.0 + (1.0 if k % 2 else 0.0) for k in range(21)]) == pytest.approx(0.0)
    # 严格线性上涨 33 棒 => mean(sqrt(16), sqrt(20), sqrt(32)) > 4.0 >> 1.0
    assert multioctave_brownian_er([100.0 + k for k in range(33)], canvas_bars=160) > 4.0
    assert multioctave_brownian_er([100.0 + (1.0 if k % 2 else 0.0) for k in range(33)], canvas_bars=160) == pytest.approx(0.0)
    rets_a = [0.01 * ((k % 5) - 2) for k in range(40)]
    assert markowitz_corr_multiplier(1, rets_a[:5], []) == pytest.approx(1.0)
    assert markowitz_corr_multiplier(1, rets_a, []) == pytest.approx(math.sqrt(2.0))
    assert markowitz_corr_multiplier(1, rets_a, [(1, rets_a)]) == pytest.approx(1.0)
    assert markowitz_corr_multiplier(1, rets_a, [(-1, rets_a)]) == pytest.approx(math.sqrt(2.0))
    # 动态场所摩擦派生: cost_per_side=None 按 taker_fee + slippage_by_symbol 自动派生; 显式覆盖保持原值
    rp_auto = RouterParams(cost_per_side=None)
    assert resolve_cost_per_side(rp_auto, "BTCUSDT", taker_fee=0.0006, slippage_per_side=0.0002).cost_per_side == pytest.approx(0.0008)
    assert resolve_cost_per_side(rp_auto, "AAPL", taker_fee=0.0001, slippage_per_side=0.0001).cost_per_side == pytest.approx(0.0002)
    assert resolve_cost_per_side(rp_auto, "MEMEUSDT", taker_fee=0.0006, slippage_per_side=0.0002, slippage_by_symbol={"MEMEUSDT": 0.0009}).cost_per_side == pytest.approx(0.0015)
    assert resolve_cost_per_side(RouterParams(cost_per_side=0.0005), "AAPL", taker_fee=0.0001, slippage_per_side=0.0001).cost_per_side == pytest.approx(0.0005)


def test_directional_breadth_parity_and_strong_sweep_trap_unlock():
    """第一性原理方向对齐市场宽度豁免(F2_DIR) + 纯 OHLC 强扫损反转解锁(F1e_PURE_STRONG_SWEEP)。"""
    from market_operator.engine import blocked_by_market_breadth
    from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
    from market_operator.hypothesis import StructureView
    from market_operator.level import Level, LevelKind, LevelRole, LevelState
    from market_operator.routing import MarketRouter, RouterParams
    from market_operator.structure import Regime

    # 1) 方向对齐市场宽度豁免:
    #    - 全市场暴跌(breadth=-0.7)时: 仅自身处于 TREND_UP 的独立多头龙头豁免;
    #      RANGE 滞后标的与 TREND_DOWN 跌势标的抄底一律拦截!
    assert blocked_by_market_breadth(+1, Regime.TREND_UP, -0.7, 10) is False
    assert blocked_by_market_breadth(+1, Regime.RANGE, -0.7, 10) is True
    assert blocked_by_market_breadth(+1, Regime.TREND_DOWN, -0.7, 10) is True
    assert blocked_by_market_breadth(-1, Regime.TREND_DOWN, +0.7, 10) is False
    assert blocked_by_market_breadth(-1, Regime.TREND_UP, +0.7, 10) is True

    # 路由器端端到端验证: 独立领涨 TREND_UP 多头突破在 breadth=-0.7 时不被误杀
    res_lv = Level(
        uid=10, role=LevelRole.RESISTANCE, kind=LevelKind.ORIGIN,
        extreme=100.0, neck=99.0, state=LevelState.FRESH,
        origin_index=5, known_index=10, origin_ts=0, last_change_known=10,
        prominence_2d=5.0,
    )
    ev_bo = MarketEvent(
        id=BREAKOUT_ACCEPTED, direction=+1, ts=100_000, known_index=20,
        attrs={"level": res_lv, "zone": res_lv, "invalid": 98.0, "ex": 102.0, "ref": 100.0, "sub_kind": "ACCEPT_ORIGIN"},
    )
    vw_up = StructureView(
        regime=Regime.TREND_UP, zones=(res_lv,), atr=2.0, tf_hours=4,
        imbalance_strength=1.5, cog_vr4=1.30, trig_vr4=0.80, cog_park_ratio=1.20, cog_er_norm=0.50,
    )
    bo_bar = (100_000, 100.5, 102.0, 100.0, 101.5, 0.0)
    r_br = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert len(r_br.on_bar("LEADER", bo_bar, 20, vw_up, 2.0, [ev_bo], market_breadth=-0.7, universe_size=10)) == 1

    # 2) 纯 OHLC 强扫损反转 SWEEP_TRAP 解锁:
    #    双底 EQUAL 流动性池在 TREND_DOWN 下被刺穿扫损 ->
    #    - 强反杀 K 线(wick>=1/3, bar_range_q75_rel>=1.0, trig_park_ratio>=1.0)且非高周期裸逆势 => 放行!
    #    - 弱小影线/无 Q75 振幅/无波动觉醒 => 仍拦截!
    #    - 全市场暴跌风暴(breadth=-0.7)下的 TREND_DOWN 抄底 => 仍被方向对齐宽度门拦截!
    eq_sup = Level(
        uid=20, role=LevelRole.SUPPORT, kind=LevelKind.EQUAL,
        extreme=100.0, neck=100.5, state=LevelState.TESTED,
        origin_index=5, known_index=12, origin_ts=0, last_change_known=12,
        prominence_2d=1.0,
    )
    hi_lv = Level(
        uid=21, role=LevelRole.RESISTANCE, kind=LevelKind.SWING,
        extreme=120.0, neck=119.5, state=LevelState.FRESH,
        origin_index=2, known_index=8, origin_ts=0, last_change_known=8,
        prominence_2d=3.0,
    )
    ev_sw = MarketEvent(
        id=LEVEL_REJECTED, direction=+1, ts=200_000, known_index=25,
        attrs={"level": eq_sup, "zone": eq_sup, "invalid": 98.5, "ex": 99.0, "ref": 100.0, "swept": True, "sub_kind": "SWEEP_TRAP"},
    )
    vw_sw_strong = StructureView(
        regime=Regime.TREND_DOWN, zones=(eq_sup, hi_lv), atr=2.0, tf_hours=4,
        imbalance_strength=-1.2, cog_vr4=1.15, trig_park_ratio=1.25, bar_range_q75_rel=1.30,
    )
    vw_sw_weak = StructureView(
        regime=Regime.TREND_DOWN, zones=(eq_sup, hi_lv), atr=2.0, tf_hours=4,
        imbalance_strength=-1.2, cog_vr4=1.15, trig_park_ratio=0.80, bar_range_q75_rel=0.70,
    )
    # 强下影锤头线: low=99.0, open=100.5, close=101.2, high=101.5 -> wick_def = 1.5/2.5 = 0.60 >= 1/3
    strong_hammer = (200_000, 100.5, 101.5, 99.0, 101.2, 0.0)
    # 无下影软棒: low=100.0, open=100.1, close=101.2, high=101.5 -> wick_def = 0.1/1.5 = 0.067 < 1/3
    weak_bar = (200_000, 100.1, 101.5, 100.0, 101.2, 0.0)

    r_ok = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert len(r_ok.on_bar("SWEEP_OK", strong_hammer, 25, vw_sw_strong, 2.0, [ev_sw], market_breadth=-0.1, universe_size=10)) == 1

    r_w1 = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert r_w1.on_bar("SWEEP_W1", weak_bar, 25, vw_sw_strong, 2.0, [ev_sw], market_breadth=-0.1, universe_size=10) == []

    r_w2 = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert r_w2.on_bar("SWEEP_W2", strong_hammer, 25, vw_sw_weak, 2.0, [ev_sw], market_breadth=-0.1, universe_size=10) == []

    # 全市场暴跌风暴(breadth=-0.7)下 TREND_DOWN 扫损抄底必须被拦截!
    r_storm = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))
    assert r_storm.on_bar("SWEEP_STORM", strong_hammer, 25, vw_sw_strong, 2.0, [ev_sw], market_breadth=-0.7, universe_size=10) == []
    assert r_storm.counters.get("rejected_market_breadth") == 1


def test_consumed_claims_deduplication_uses_origin_index():
    """P1 回归测试: _release_departed_claims 必须按 (origin_index, flip_count) 匹配活跃水平位,
    在价格未实质脱离 3*band_width 前禁止同一水平位连续重复放行(不再因 uid != origin_index 被下一棒误清空)。"""
    from market_operator.events import LEVEL_REJECTED, MarketEvent
    from market_operator.hypothesis import StructureView
    from market_operator.level import Level, LevelKind, LevelRole, LevelState
    from market_operator.routing import MarketRouter, RouterParams
    from market_operator.structure import Regime

    # 故意让 uid(999) != origin_index(12),复现原键错配 Bug!
    sup = Level(
        uid=999, role=LevelRole.SUPPORT, kind=LevelKind.ORIGIN,
        extreme=100.0, neck=101.0, state=LevelState.FRESH,
        origin_index=12, known_index=15, origin_ts=0, last_change_known=15,
        prominence_2d=5.0,
    )
    far_res = Level(
        uid=1000, role=LevelRole.RESISTANCE, kind=LevelKind.SWING,
        extreme=120.0, neck=119.0, state=LevelState.FRESH,
        origin_index=5, known_index=8, origin_ts=0, last_change_known=8,
        prominence_2d=1.0,
    )
    vw = StructureView(Regime.RANGE, (sup, far_res), atr=2.0, cog_vr4=1.2, trig_park_ratio=1.2, bar_range_q75_rel=1.2)
    ev = MarketEvent(LEVEL_REJECTED, +1, 1000, 20, {"level": sup, "zone": sup, "invalid": 99.0, "ex": 100.2})
    bar_near = (1000, 101.2, 102.0, 100.5, 101.8, 0.0)
    router = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0002))

    # 第 1 次放行并登记 _consumed_claims
    assert len(router.on_bar("X", bar_near, 20, vw, 2.0, [ev])) == 1
    # 下一棒价格仍在水平位附近(l=100.5 <= band_hi + 3*w = 104.0): 必须被 skipped_duplicate_claim 拦截!
    assert router.on_bar("X", bar_near, 21, vw, 2.0, [ev]) == []
    assert router.counters.get("skipped_duplicate_claim") == 1
    # 价格彻底远离 (l=106.0 > 104.0) 后释放去重锁,再次回踩允许重新交易
    bar_far = (2000, 106.5, 108.0, 106.0, 107.0, 0.0)
    assert router.on_bar("X", bar_far, 22, vw, 2.0, []) == []
    assert len(router.on_bar("X", bar_near, 23, vw, 2.0, [ev])) == 1


def test_p3_single_trade_risk_cap_and_config_parity():
    """P3 单笔有效风险硬封顶契约 + 回放/实盘配置一致性测试。"""
    import json
    import math
    from pathlib import Path
    from market_operator.broker import Broker, ContractSpec
    from market_operator.capacity import cap_effective_size_multiplier
    from market_operator.engine import cap_effective_size_multiplier as engine_cap
    from market_operator.scenario import PRODUCTION_CONFIG, load_settings

    # 1) 共享纯函数: 防止 conviction(1.5x) * markowitz(sqrt(2)=1.414x) = 2.12x 越过单笔封顶
    assert engine_cap is cap_effective_size_multiplier
    assert cap_effective_size_multiplier(1.5, math.sqrt(2.0), 1.5) == pytest.approx(1.5)
    assert cap_effective_size_multiplier(0.9, math.sqrt(2.0), 1.5) == pytest.approx(0.9 * math.sqrt(2.0))
    assert cap_effective_size_multiplier(1.5, 0.5, 1.5) == pytest.approx(0.75)
    assert cap_effective_size_multiplier(2.0, math.sqrt(2.0), None) == pytest.approx(2.0 * math.sqrt(2.0))

    # 2) Broker.qty_for_entry 严格遵守 max_eff_size_mult 硬封顶
    spec = ContractSpec(symbol="BTCUSDT", size_min=0.001, size_place=4, size_step=0.001, price_place=2, min_trade_usdt=5.0)
    brk_cap = Broker(
        equity=1000.0, risk_frac=0.04, leverage_cap=20.0,
        taker_fee=0.0006, slippage_per_side=0.0002, funding_per_8h=0.0,
        contracts={"BTCUSDT@4h": spec},
        corr_size_fn=lambda _b, _s, _d: math.sqrt(2.0),
        max_eff_size_mult=1.5,
    )
    brk_uncap = Broker(
        equity=1000.0, risk_frac=0.04, leverage_cap=20.0,
        taker_fee=0.0006, slippage_per_side=0.0002, funding_per_8h=0.0,
        contracts={"BTCUSDT@4h": spec},
        corr_size_fn=lambda _b, _s, _d: math.sqrt(2.0),
        max_eff_size_mult=None,
    )
    # entry=100, stop=90 => risk_per_unit=10; capital=1000, risk_frac=0.04 => base_risk=40
    # capped: min(1.5 * sqrt(2), 1.5) * 40 / 10 = 6.0; uncapped: 1.5 * sqrt(2) * 40 / 10 = 8.485
    q_cap = brk_cap.qty_for_entry(1000.0, 100.0, 90.0, "BTCUSDT@4h", size_mult=1.5, use_budget=False, direction=+1)
    q_uncap = brk_uncap.qty_for_entry(1000.0, 100.0, 90.0, "BTCUSDT@4h", size_mult=1.5, use_budget=False, direction=+1)
    assert q_cap == pytest.approx(6.0)
    assert q_uncap == pytest.approx(6.0 * math.sqrt(2.0))

    # 3) 生产配置 strategy.json 与 live.json 均显式落盘 max_eff_size_mult=1.5 且一致
    settings = load_settings(PRODUCTION_CONFIG)
    live_cfg = json.loads((Path(PRODUCTION_CONFIG).parent / "live.json").read_text(encoding="utf-8"))
    assert settings.max_eff_size_mult == pytest.approx(1.5)
    assert float(settings.raw["small_account_compounding"]["max_eff_size_mult"]) == pytest.approx(1.5)
    assert float(live_cfg["risk"]["max_eff_size_mult"]) == pytest.approx(1.5)
    # 4) 六维单位均值恒等式: mean({0.5 + (pts - 1)/5 | pts in 1..6}) == 1.0
    mults = [0.5 + (pts - 1.0) / 5.0 for pts in range(1, 7)]
    assert sum(mults) / len(mults) == pytest.approx(1.0)








