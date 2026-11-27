"""策略内核四大数学不变性门禁测试(防市场先验与隐式拟合的永久 CI 锁)。

策略内核(structure / level / events / hypothesis / routing / position / trigger)
必须只依赖纯 K 线 (O, H, L, C) 与外部显式传入的摩擦成本 cost_per_side,严格满足:
1. 价格尺度不变性(Scale Invariance): P -> 1000*P / 0.0001*P 信号、信念分与 R 倍数逐位恒等;
2. 零成交量不变性(Zero-Volume Invariance): V=real / V=0 / V=随机噪音 全链路信号逐位恒等;
3. 多空镜像对称性(Mirror / Long-Short Symmetry):
   - 图表翻转 P -> C - P 时,认知状态、事件流、路由决策(含强扫损、突破、宽度保护、信念分)
     与持仓管理严格 LONG <-> SHORT 镜像互换,绝不允许 TREND_DOWN != TREND_UP 单边硬编码;
4. 交易时钟无关性(Clock-Agnostic Bar-Space Invariance):
   - 同一段 OHLC 序列配 24x7 加密时间戳 vs 6.5h/天 + 周末休市美股时间戳,以及不同 tf_hours,
     策略内核产出的视图、方差比、横截面回看窗口与路由决策完全逐位一致。
"""

from __future__ import annotations

import math
import random

import pytest

from market_operator.engine import blocked_by_market_breadth, peer_lookback_bars
from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, EventDetector, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.level import Level, LevelKind, LevelRole, LevelState
from market_operator.position import PositionManager
from market_operator.routing import MarketRouter, RouterParams
from market_operator.structure import Params, Regime
from market_operator.trigger import MicTracker


def _synthetic_ohlcv(n: int = 1200, *, seed: int = 42) -> list[tuple[int, float, float, float, float, float]]:
    """生成含清晰箱体双顶双底、假破反杀与多轮向上/向下真突破的多态纯 K 线序列(确定性可复现)。"""
    rng = random.Random(seed)
    bars: list[tuple[int, float, float, float, float, float]] = []
    # 构造 4 段清晰价格中枢(箱体振荡 -> 突破迁移 -> 新箱体 -> 反向跌破),产生丰富且对称的多空信号
    centers = [200.0, 255.0, 195.0, 260.0, 205.0]
    px = 200.0
    for i in range(n):
        ts = 1_700_000_000_000 + i * 3_600_000
        seg = min(len(centers) - 1, i // 240)
        phase = i % 240
        base_c = centers[seg]
        next_c = centers[min(len(centers) - 1, seg + 1)]
        if phase < 160:
            # 清晰箱体波段往复(产生真实 SWING / EQUAL 水平位与拒收反弹)
            target = base_c + 14.0 * math.sin(phase * 2.0 * math.pi / 48.0)
        else:
            # 干净单边失衡推进(产生真实 BREAKOUT_ACCEPTED 与 TREND_UP/TREND_DOWN)
            frac = (phase - 160) / 80.0
            target = base_c + (next_c - base_c) * frac + 3.0 * math.sin(phase * 0.4)
        o = px
        c = max(30.0, o + 0.55 * (target - o) + rng.uniform(-1.2, 1.2))
        h = max(o, c) + rng.uniform(0.3, 1.8)
        l = min(o, c) - rng.uniform(0.3, 1.8)
        v = rng.uniform(100.0, 50_000.0)
        bars.append((ts, o, h, l, c, v))
        px = c
    return bars


def _run_core_pipeline(
    bars_1h: list[tuple[int, float, float, float, float, float]],
    *,
    tf_hours: int = 4,
    scale: int = 4,
    cost_per_side: float = 0.0002,
    market_breadth: float = 0.0,
    excess_ret_48h: float = 0.02,
    excess_ret_120h: float = 0.03,
) -> list[dict]:
    """沿 EventDetector(4h) -> MicTracker(1h) -> MarketRouter 跑完完整纯策略内核。"""
    params = Params(canvas_bars=160)
    det = EventDetector(params)
    mic = MicTracker(params, scale=scale)
    router = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=cost_per_side))

    out: list[dict] = []
    for i, b in enumerate(bars_1h):
        if (i + 1) % scale == 0:
            chunk = bars_1h[i + 1 - scale : i + 1]
            cog_bar = (
                chunk[0][0],
                chunk[0][1],
                max(x[2] for x in chunk),
                min(x[3] for x in chunk),
                chunk[-1][4],
                sum(x[5] for x in chunk),
            )
            det.update(cog_bar)
            mic.sync_from_structure(det.structure)
        evs = mic.update(b)
        if not evs or det.structure.bars_seen < 20:
            continue
        vw = mic.current_view(tf_hours=tf_hours)
        decs = router.on_bar(
            "TESTSYM",
            b,
            i,
            vw,
            mic.frozen_atr,
            evs,
            market_breadth=market_breadth,
            universe_size=10,
            median_dollar_volume=1e9,  # 故意设极大值,验证策略层零成交额依赖
            excess_ret_48h=excess_ret_48h,
            excess_ret_120h=excess_ret_120h,
        )
        for dec in decs:
            if dec.kind != "market" or dec.hyp is None:
                continue
            out.append({
                "idx": i,
                "event_id": dec.hyp.event_id,
                "direction": int(dec.hyp.direction),
                "regime": dec.meta["regime"],
                "sub_kind": dec.meta.get("sub_kind"),
                "r_mult": round(float(dec.hyp.r_mult), 6),
                "conviction": int(dec.meta.get("conviction_score", 0)),
                "size_mult": round(float(dec.meta.get("size_mult", 1.0)), 6),
                "priority": round(float(dec.meta.get("priority_score", 0.0)), 5),
                "risk_frac_px": round(abs(b[4] - float(dec.hyp.invalid)) / max(b[4], 1e-12), 6),
            })
    return out


def test_invariance_1_price_scale() -> None:
    """门禁 1: 价格尺度不变性 P -> 1000*P 与 P -> 0.0001*P 信号、信念与 R 倍数完全一致。"""
    base = _synthetic_ohlcv(1800, seed=7)
    res_base = _run_core_pipeline(base)
    assert len(res_base) >= 5, "合成样本应产出足够多路由信号以覆盖门禁"

    for mult in (1000.0, 0.0001):
        scaled = [(b[0], b[1] * mult, b[2] * mult, b[3] * mult, b[4] * mult, b[5]) for b in base]
        res_s = _run_core_pipeline(scaled)
        assert res_s == res_base


def test_invariance_2_zero_volume() -> None:
    """门禁 2: 零成交量不变性 — 真实成交量 / 全 0 成交量(外汇/CFD) / 随机巨量噪音 逐位恒等。"""
    base = _synthetic_ohlcv(1800, seed=7)
    zero_v = [(b[0], b[1], b[2], b[3], b[4], 0.0) for b in base]
    rng = random.Random(999)
    noisy_v = [(b[0], b[1], b[2], b[3], b[4], rng.uniform(0.0, 1e11)) for b in base]

    res_base = _run_core_pipeline(base, tf_hours=1)
    res_zero = _run_core_pipeline(zero_v, tf_hours=1)
    res_noisy = _run_core_pipeline(noisy_v, tf_hours=1)
    assert len(res_base) >= 5
    assert res_zero == res_base
    assert res_noisy == res_base


def test_invariance_3_mirror_long_short_symmetry() -> None:
    """门禁 3: 严格多空镜像对称性(绝不允许 TREND_DOWN != TREND_UP 单边硬编码)。"""
    from market_operator.structure import (
        multioctave_brownian_er,
        multioctave_parkinson_ratio,
        variance_ratio,
    )

    # 3a) 端到端几何反射镜像 P -> C - P 与尺度不变 P -> alpha * P:
    #     Canvas2DLevelScanner(含 np.quantile 对称分位) + EventDetector + MicTracker
    #     在 P -> C - P 下状态/失衡强度/水平位/全部事件 100% 镜像互换,在 P -> alpha * P 下 1e-9 尺度不变!
    from market_operator.level import Canvas2DLevelScanner

    base = _synthetic_ohlcv(1200, seed=7)
    base_zv = [(b[0], b[1], b[2], b[3], b[4], 0.0) for b in base]
    c_ref = 2.0 * (max(b[2] for b in base_zv) + min(b[3] for b in base_zv))
    refl = [(b[0], c_ref - b[1], c_ref - b[3], c_ref - b[2], c_ref - b[4], 0.0) for b in base_zv]
    alpha = 137.5
    scaled = [(b[0], alpha * b[1], alpha * b[2], alpha * b[3], alpha * b[4], 0.0) for b in base_zv]

    mirror_reg = {"trend_up": "trend_down", "trend_down": "trend_up", "range": "range", "transition": "transition"}
    mirror_role = {LevelRole.SUPPORT: LevelRole.RESISTANCE, LevelRole.RESISTANCE: LevelRole.SUPPORT}
    sc_o = Canvas2DLevelScanner(canvas_bars=160)
    sc_r = Canvas2DLevelScanner(canvas_bars=160)
    sc_s = Canvas2DLevelScanner(canvas_bars=160)
    cog_o, cog_r, cog_s = [], [], []
    checked_frames = 0
    for idx in range(0, len(base_zv), 4):
        ch_o = base_zv[idx : idx + 4]
        ch_r = refl[idx : idx + 4]
        ch_s = scaled[idx : idx + 4]
        if len(ch_o) < 4:
            break
        cog_o.append((ch_o[0][0], ch_o[0][1], max(x[2] for x in ch_o), min(x[3] for x in ch_o), ch_o[-1][4], 0.0))
        cog_r.append((ch_r[0][0], ch_r[0][1], max(x[2] for x in ch_r), min(x[3] for x in ch_r), ch_r[-1][4], 0.0))
        cog_s.append((ch_s[0][0], ch_s[0][1], max(x[2] for x in ch_s), min(x[3] for x in ch_s), ch_s[-1][4], 0.0))
        sc_o.update_canvas(cog_o)
        sc_r.update_canvas(cog_r)
        sc_s.update_canvas(cog_s)
        if len(cog_o) >= 40:
            checked_frames += 1
            assert mirror_reg[sc_r.regime_2d] == sc_o.regime_2d
            assert sc_r.imbalance_strength == pytest.approx(-sc_o.imbalance_strength, rel=1e-9, abs=1e-9)
            assert sc_s.regime_2d == sc_o.regime_2d
            assert sc_s.imbalance_strength == pytest.approx(sc_o.imbalance_strength, rel=1e-9, abs=1e-9)
            lvs_o = sorted(sc_o.active_levels, key=lambda z: (z.origin_index, z.role.value))
            lvs_r = sorted(sc_r.active_levels, key=lambda z: (z.origin_index, mirror_role[z.role].value))
            lvs_s = sorted(sc_s.active_levels, key=lambda z: (z.origin_index, z.role.value))
            assert len(lvs_o) == len(lvs_r) == len(lvs_s)
            for zo, zr, zs in zip(lvs_o, lvs_r, lvs_s):
                assert zr.origin_index == zo.origin_index and zs.origin_index == zo.origin_index
                assert mirror_role[zr.role] is zo.role and zs.role is zo.role
                assert zr.kind is zo.kind and zs.kind is zo.kind
                assert zr.touches == zo.touches and zs.touches == zo.touches
                assert zr.extreme == pytest.approx(c_ref - zo.extreme, rel=1e-9)
                assert zs.extreme == pytest.approx(alpha * zo.extreme, rel=1e-9)
    assert checked_frames >= 200

    def _collect_events(bars_1h):
        params = Params(canvas_bars=160)
        det = EventDetector(params)
        mic = MicTracker(params, scale=4)
        evs_out = []
        for i, b in enumerate(bars_1h):
            if (i + 1) % 4 == 0:
                ch = bars_1h[i - 3 : i + 1]
                det.update((ch[0][0], ch[0][1], max(x[2] for x in ch), min(x[3] for x in ch), ch[-1][4], 0.0))
                mic.sync_from_structure(det.structure)
            for e in mic.update(b):
                evs_out.append((i, e.id, int(e.direction), det.structure.regime().value, e.attrs.get("sub_kind")))
        return evs_out

    ev_o = _collect_events(base_zv)
    ev_r = _collect_events(refl)
    assert len(ev_o) >= 15
    assert [(i, eid, -d, mirror_reg[reg], sk) for (i, eid, d, reg, sk) in ev_r] == ev_o

    # 3b) 对数空间物理量(VR4 / 多倍频程 Parkinson 波动觉醒)在 P -> K / P 下 1e-10 精度严格恒等!
    inv_bars = [(b[0], 1e5 / b[1], 1e5 / b[3], 1e5 / b[2], 1e5 / b[4], 0.0) for b in base_zv[:240]]
    assert variance_ratio([b[4] for b in inv_bars], q=4) == pytest.approx(
        variance_ratio([b[4] for b in base_zv[:240]], q=4), rel=1e-10, abs=1e-10
    )
    assert multioctave_parkinson_ratio(inv_bars, canvas_bars=160) == pytest.approx(
        multioctave_parkinson_ratio(base_zv[:240], canvas_bars=160), rel=1e-10, abs=1e-10
    )
    # 3c) 路由器在所有 Regime / LevelKind / 宽度风暴 / BREAKOUT+REJECT 组合下的严格镜像恒等
    for reg_up, reg_dn in ((Regime.TREND_UP, Regime.TREND_DOWN), (Regime.RANGE, Regime.RANGE)):
        for kind in (LevelKind.ORIGIN, LevelKind.EQUAL, LevelKind.SWING, LevelKind.FLIPPED):
            for swept in (False, True):
                is_fl = kind is LevelKind.FLIPPED
                sup = Level(1, LevelRole.SUPPORT, kind, 100.0, 100.5, LevelState.FLIPPED if is_fl else LevelState.FRESH, 5, 10, 0, 10, prominence_2d=5.0, flipped=is_fl)
                res = Level(1, LevelRole.RESISTANCE, kind, 200.0, 199.5, LevelState.FLIPPED if is_fl else LevelState.FRESH, 5, 10, 0, 10, prominence_2d=5.0, flipped=is_fl)
                far_res = Level(2, LevelRole.RESISTANCE, LevelKind.SWING, 125.0, 124.5, LevelState.FRESH, 2, 8, 0, 8, prominence_2d=1.0)
                far_sup = Level(2, LevelRole.SUPPORT, LevelKind.SWING, 175.0, 175.5, LevelState.FRESH, 2, 8, 0, 8, prominence_2d=1.0)

                ev_up = MarketEvent(LEVEL_REJECTED, +1, 1000, 20, {"level": sup, "zone": sup, "invalid": 99.0, "ex": 99.5, "swept": swept})
                ev_dn = MarketEvent(LEVEL_REJECTED, -1, 1000, 20, {"level": res, "zone": res, "invalid": 201.0, "ex": 200.5, "swept": swept})
                vw_up = StructureView(reg_up, (sup, far_res), atr=2.0, cog_vr4=1.2, trig_park_ratio=1.2, bar_range_q75_rel=1.2)
                vw_dn = StructureView(reg_dn, (res, far_sup), atr=2.0, cog_vr4=1.2, trig_park_ratio=1.2, bar_range_q75_rel=1.2)
                bar_up = (1000, 101.2, 102.0, 100.0, 101.8, 0.0)
                bar_dn = (1000, 198.8, 200.0, 198.0, 198.2, 0.0)

                d_up = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0)).on_bar("A", bar_up, 20, vw_up, 2.0, [ev_up], market_breadth=0.0, universe_size=10)
                d_dn = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0)).on_bar("B", bar_dn, 20, vw_dn, 2.0, [ev_dn], market_breadth=0.0, universe_size=10)
                assert len(d_up) == len(d_dn), f"Asymmetry at {reg_up}/{kind}/swept={swept}"
                if d_up:
                    assert d_up[0].meta["conviction_score"] == d_dn[0].meta["conviction_score"]
                    assert d_up[0].meta["size_mult"] == pytest.approx(d_dn[0].meta["size_mult"])

        # 同理验证 BREAKOUT_ACCEPTED 在 TREND_UP (+1) vs TREND_DOWN (-1) 严格镜像一致
        bo_res = Level(3, LevelRole.RESISTANCE, LevelKind.ORIGIN, 100.0, 99.0, LevelState.FRESH, 5, 10, 0, 10, prominence_2d=5.0)
        bo_sup = Level(3, LevelRole.SUPPORT, LevelKind.ORIGIN, 200.0, 201.0, LevelState.FRESH, 5, 10, 0, 10, prominence_2d=5.0)
        ev_bo_u = MarketEvent(BREAKOUT_ACCEPTED, +1, 1000, 20, {"level": bo_res, "zone": bo_res, "invalid": 98.0, "ex": 102.0})
        ev_bo_d = MarketEvent(BREAKOUT_ACCEPTED, -1, 1000, 20, {"level": bo_sup, "zone": bo_sup, "invalid": 202.0, "ex": 198.0})
        vw_bu = StructureView(reg_up, (bo_res,), atr=2.0, cog_vr4=1.3, trig_vr4=0.8, cog_park_ratio=1.2, cog_er_norm=0.5)
        vw_bd = StructureView(reg_dn, (bo_sup,), atr=2.0, cog_vr4=1.3, trig_vr4=0.8, cog_park_ratio=1.2, cog_er_norm=-0.5)
        dbu = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0)).on_bar("AU", (1000, 100.5, 102.0, 100.0, 101.5, 0.0), 20, vw_bu, 2.0, [ev_bo_u], market_breadth=0.1, universe_size=10, excess_ret_48h=0.05)
        dbd = MarketRouter(RouterParams(conviction_sizing=True, cost_per_side=0.0)).on_bar("AD", (1000, 199.5, 200.0, 198.0, 198.5, 0.0), 20, vw_bd, 2.0, [ev_bo_d], market_breadth=-0.1, universe_size=10, excess_ret_48h=-0.05)
        assert len(dbu) == len(dbd)
        if dbu:
            assert dbu[0].meta["conviction_score"] == dbd[0].meta["conviction_score"]
            assert dbu[0].meta["size_mult"] == pytest.approx(dbd[0].meta["size_mult"])

    # 宽度风暴保护亦严格多空镜像对称
    assert blocked_by_market_breadth(+1, Regime.TREND_UP, -0.7, 10) == blocked_by_market_breadth(-1, Regime.TREND_DOWN, +0.7, 10)
    assert blocked_by_market_breadth(+1, Regime.TREND_DOWN, -0.7, 10) == blocked_by_market_breadth(-1, Regime.TREND_UP, +0.7, 10)
    assert blocked_by_market_breadth(+1, Regime.RANGE, -0.7, 10) == blocked_by_market_breadth(-1, Regime.RANGE, +0.7, 10)


def test_invariance_4_clock_agnostic_bar_space() -> None:
    """门禁 4: 交易时钟无关性 — 24x7 加密时间戳 vs 6.5h/天+周末休市美股时间戳,以及不同 tf_hours 逐位恒等。"""
    base = _synthetic_ohlcv(1200, seed=7)

    # 构造带日内隔夜缺口(17.5h)与周末缺口(64h)的纯美股 RTH 时间戳,OHLC 序列完全不变
    rth_bars: list[tuple[int, float, float, float, float, float]] = []
    cur_ts = 1_700_050_200_000  # 某周一美东开盘
    for idx, b in enumerate(base):
        rth_bars.append((cur_ts, b[1], b[2], b[3], b[4], b[5]))
        if (idx + 1) % 35 == 0:
            cur_ts += 64 * 3_600_000   # 周末休市跳空
        elif (idx + 1) % 7 == 0:
            cur_ts += 18 * 3_600_000   # 隔夜休市跳空
        else:
            cur_ts += 3_600_000

    res_24x7_4h = _run_core_pipeline(base, tf_hours=4)
    res_rth_4h = _run_core_pipeline(rth_bars, tf_hours=4)
    res_rth_1d = _run_core_pipeline(rth_bars, tf_hours=24)
    res_rth_1h = _run_core_pipeline(rth_bars, tf_hours=1)
    assert len(res_24x7_4h) >= 3
    assert res_rth_4h == res_24x7_4h
    assert res_rth_1d == res_24x7_4h
    assert res_rth_1h == res_24x7_4h

    # peer_lookback_bars 与 PositionManager.on_trigger_bar 同样与 tf_hours/tf_label 无关
    assert peer_lookback_bars(1) == peer_lookback_bars(4) == peer_lookback_bars(24)
    pm = PositionManager()
    pm.open("SYM", 1, 100.0, 95.0, 115.0, BREAKOUT_ACCEPTED, 0, 0)
    opp = MarketEvent(LEVEL_REJECTED, -1, 1000, 1, {})
    assert pm.on_trigger_bar("SYM", (1000, 101.0, 102.0, 100.5, 101.5, 0.0), [opp], tf_label="1h") == []
    assert pm.on_trigger_bar("SYM", (1000, 101.0, 102.0, 100.5, 101.5, 0.0), [opp], tf_label="4h") == []
