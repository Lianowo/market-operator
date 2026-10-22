"""§9 事件研究计算核心测试:合成价格路径上的已知结局断言。

覆盖:MFE/MAE/毛收益数值、target/invalid 命中判定(含同 bar 双命中保守
invalid 优先)、taker 成本扣减数值、对照组排除事件后 24 bar 窗口、
§9.1-3 方向映射硬核对、GO/NO-GO 判据逻辑(§9.3 增量口径)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from market_operator.events import BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, TREND_RESUMED, RANGE_ROTATED, STRUCTURE_BROKEN, MarketEvent
from market_operator.research import (
    COST_MULTS,
    CostModel,
    block_bootstrap_ci,
    control_pool,
    exclusion_mask,
    expected_direction,
    go_no_go,
    measure_events,
    measure_path,
    StructureTimeline,
)
from market_operator.hypothesis import StructureView
from market_operator.structure import Regime, Zone, ZoneRole

COST = CostModel(taker_fee=0.0006, slippage_per_side=0.0002,
                 funding_per_8h_per_side=0.0001)


def _arrays(bars):
    a = np.array(bars, dtype=np.float64)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]      # o, h, l, c


# ---------------------------------------------------------------------------
# measure_path:已知结局
# ---------------------------------------------------------------------------


def test_measure_path_known_outcome_long():
    o, h, l, c = _arrays([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.4, 99.6, 100.2),   # 入场 bar(entry_i=1, open=100)
        (100.2, 101.2, 98.9, 100.8),
    ])
    mfe, mae, gross, hit = measure_path(o, h, l, c, entry_i=1, horizon=2,
                                        direction=+1, invalid=99.0,
                                        target=101.0)
    assert mfe == pytest.approx(1.2)                # max high 101.2 − 100
    assert mae == pytest.approx(1.1)                # 100 − min low 98.9
    assert gross == pytest.approx(0.8)              # close 100.8 − 100
    assert hit == "invalid"                         # bar2 双命中 ⇒ 保守 invalid


def test_measure_path_target_first():
    o, h, l, c = _arrays([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 101.2, 99.3, 100.8),
    ])
    _, _, _, hit = measure_path(o, h, l, c, entry_i=1, horizon=1,
                                direction=+1, invalid=99.0, target=101.0)
    assert hit == "target"


def test_measure_path_same_bar_double_hit_is_invalid():
    # 同 bar 同时触及 target 与 invalid ⇒ 保守记 invalid
    o, h, l, c = _arrays([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 101.2, 98.8, 100.5),
    ])
    _, _, _, hit = measure_path(o, h, l, c, entry_i=1, horizon=1,
                                direction=+1, invalid=99.0, target=101.0)
    assert hit == "invalid"


def test_measure_path_short_mirror():
    o, h, l, c = _arrays([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.7, 98.9, 99.2),
    ])
    mfe, mae, gross, hit = measure_path(o, h, l, c, entry_i=1, horizon=1,
                                        direction=-1, invalid=101.0,
                                        target=99.0)
    assert mfe == pytest.approx(1.1)                # 100 − min low 98.9
    assert mae == pytest.approx(0.7)                # max high 100.7 − 100
    assert gross == pytest.approx(0.8)              # 100 − close 99.2
    assert hit == "target"                          # low 98.9 ≤ 99.0


def test_measure_path_no_hit():
    o, h, l, c = _arrays([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.3, 99.7, 100.1),
    ])
    _, _, _, hit = measure_path(o, h, l, c, entry_i=1, horizon=1,
                                direction=+1, invalid=99.0, target=101.0)
    assert hit is None


# ---------------------------------------------------------------------------
# 成本扣减数值(§9.2:taker 0.06% + 滑点 0.02% 每边,funding 双边按持仓)
# ---------------------------------------------------------------------------


def test_net_deducts_round_trip_cost():
    assert COST.round_trip_frac(8, 1.0) == pytest.approx(0.0018)   # 12+4+2 bp
    assert COST.round_trip_frac(8, 1.5) == pytest.approx(0.0027)
    assert COST.round_trip_frac(24, 1.0) == pytest.approx(0.0022)  # +3×8h funding

    rows = _measured_synthetic()
    g = rows[rows["H"] == 6].iloc[0]
    frac = 0.0012 + 0.0004 + 0.0002 * (6 / 8)         # H=6 小时 funding 双边
    assert g["gross_pct"] == pytest.approx(0.0)       # 平直路径,毛收益 0
    assert g["net_pct_1_0"] == pytest.approx(-frac)
    assert g["net_atr_1_0"] == pytest.approx(-frac * 100.0 / 1.0)  # ÷atr(=1)
    assert g["net_pct_2_0"] == pytest.approx(-2 * frac)


# ---------------------------------------------------------------------------
# measure_events 端到端(合成 df + 手工事件/时间线)
# ---------------------------------------------------------------------------


def _measured_synthetic():
    n = 80
    df = pd.DataFrame({
        "ts": [1_700_000_000_000 + 3_600_000 * i for i in range(n)],
        "open": [100.0] * n, "high": [100.0] * n,
        "low": [100.0] * n, "close": [100.0] * n, "volume": [1.0] * n,
    })
    zone = Zone(lo=100.0, hi=101.0, role=ZoneRole.RESISTANCE)
    ev = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=df["ts"][9], known_index=9,
                     attrs={"invalid": 101.1, "ref": 101.0, "zone": zone})
    views = {9: StructureView(regime=Regime.TRANSITION, zones=(), atr=1.0)}
    timeline = StructureTimeline(
        regime=np.array([Regime.TRANSITION.value] * n, dtype=object),
        atr=np.ones(n), zones=[()] * n, views=views)
    return measure_events(df, [ev], timeline, COST)


def test_measure_events_rows_and_fields():
    rows = _measured_synthetic()
    assert sorted(rows["H"].unique()) == [6, 12, 24, 48]
    r = rows[rows["H"] == 6].iloc[0]
    assert r["entry"] == 100.0                      # 下一根 bar 开盘价
    assert r["invalid"] == 101.1
    assert r["target_1"] is None or np.isnan(r["target_1"])
    assert r["r_mult"] is None or np.isnan(r["r_mult"])
    assert r["atr"] == 1.0
    for m in COST_MULTS:
        tag = str(m).replace(".", "_")
        assert f"net_atr_{tag}" in rows.columns


def test_measure_events_skips_tail_without_forward_window():
    rows = _measured_synthetic()
    assert len(rows) == 4                           # known_index=9 前向窗足够
    n = 40                                          # 尾部事件:9+1+48-1 ≥ 40
    df = pd.DataFrame({
        "ts": [1_700_000_000_000 + 3_600_000 * i for i in range(n)],
        "open": [100.0] * n, "high": [100.0] * n,
        "low": [100.0] * n, "close": [100.0] * n, "volume": [1.0] * n,
    })
    zone = Zone(lo=100.0, hi=101.0, role=ZoneRole.RESISTANCE)
    ev = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=df["ts"][9], known_index=9,
                     attrs={"invalid": 101.1, "ref": 101.0, "zone": zone})
    views = {9: StructureView(regime=Regime.TRANSITION, zones=(), atr=1.0)}
    timeline = StructureTimeline(
        regime=np.array([Regime.TRANSITION.value] * n, dtype=object),
        atr=np.ones(n), zones=[()] * n, views=views)
    assert len(measure_events(df, [ev], timeline, COST)) == 0


# ---------------------------------------------------------------------------
# 对照组:排除事件后 24 bar 窗口(§9.1-5)
# ---------------------------------------------------------------------------


def test_control_pool_excludes_event_windows():
    n = 120
    ev = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=0, known_index=50, attrs={})
    mask = exclusion_mask(n, [ev])
    assert mask[50:75].all()                        # [known, known+24] 全排除
    assert not mask[49] and not mask[75]

    timeline = StructureTimeline(
        regime=np.array(["RANGE"] * n, dtype=object),
        atr=np.ones(n), zones=[()] * n, views={})
    pool = control_pool(timeline, 0, n, "RANGE", mask, max_h=48)
    assert pool.min() >= 0 and pool.max() <= n - 1 - 49
    assert not ((pool >= 50) & (pool <= 74)).any()  # 事件窗口不在池中


def test_control_pool_matches_regime():
    n = 120
    regime = np.array(["RANGE"] * 60 + ["TREND_UP"] * 60, dtype=object)
    timeline = StructureTimeline(regime=regime, atr=np.ones(n),
                                 zones=[()] * n, views={})
    pool = control_pool(timeline, 0, n, "RANGE",
                        exclusion_mask(n, []), max_h=48)
    assert (pool < 60).all()


# ---------------------------------------------------------------------------
# 统计与判据
# ---------------------------------------------------------------------------


def test_block_bootstrap_constant_values():
    lo, hi = block_bootstrap_ci(np.full(50, 0.5),
                                np.arange(50) * 86_400_000, reps=200)
    assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)


def _zone(role):
    return Zone(lo=1.0, hi=2.0, role=role)


def test_expected_direction_mapping():
    res, sup = ZoneRole.RESISTANCE, ZoneRole.SUPPORT
    cases = [
        (MarketEvent(BREAKOUT_ACCEPTED, 0, 0, 0, {"zone": _zone(res)}), +1),
        (MarketEvent(BREAKOUT_ACCEPTED, 0, 0, 0, {"zone": _zone(sup)}), -1),
        (MarketEvent(BREAKOUT_FAILED, 0, 0, 0, {"zone": _zone(res)}), -1),
        (MarketEvent(BREAKOUT_FAILED, 0, 0, 0, {"zone": _zone(sup)}), +1),
        (MarketEvent(LEVEL_REJECTED, 0, 0, 0, {"zone": _zone(res)}), -1),
        (MarketEvent(LEVEL_REJECTED, 0, 0, 0, {"zone": _zone(sup)}), +1),
        (MarketEvent(TREND_RESUMED, 0, 0, 0, {"leg_start": 110.0, "pb_price": 105.0}), +1),
        (MarketEvent(TREND_RESUMED, 0, 0, 0, {"leg_start": 95.0, "pb_price": 100.0}), -1),
        (MarketEvent(RANGE_ROTATED, 0, 0, 0, {"zone": _zone(res)}), -1),
        (MarketEvent(STRUCTURE_BROKEN, 0, 0, 0, {"regime_before": "TREND_UP"}), -1),
        (MarketEvent(STRUCTURE_BROKEN, 0, 0, 0, {"regime_before": "TREND_DOWN"}), +1),
    ]
    for ev, want in cases:
        assert expected_direction(ev) == want, ev.id


def _analysis(n, incr15_atr, incr15_pct, ci_lo, by_symbol):
    return {
        "n_events": n,
        "horizons": {"24": {
            "cost": {
                "1_0": {"incr_atr_ci95": [ci_lo, 1.0]},
                "1_5": {"incr_atr_mean": incr15_atr,
                        "incr_pct_mean": incr15_pct},
            },
            "by_symbol": by_symbol,
        }},
    }


def test_go_no_go_all_criteria_required():
    sym = {"BTC": {"n": 30, "mean": 0.1}, "ETH": {"n": 25, "mean": 0.2},
           "SOL": {"n": 15, "mean": -0.1}}
    a = _analysis(70, 0.05, 0.001, 0.01, sym)
    assert go_no_go(a, {"BTC": 30, "ETH": 25, "SOL": 15})["verdict"] == "GO"

    assert go_no_go(_analysis(59, 0.05, 0.001, 0.01, sym),
                    {})["verdict"] == "INSUFFICIENT_DATA"        # 判据 1
    assert go_no_go(_analysis(70, -0.05, 0.001, 0.01, sym),
                    {})["verdict"] == "NO-GO"                    # 判据 2
    assert go_no_go(_analysis(70, 0.05, 0.001, -0.01, sym),
                    {})["verdict"] == "NO-GO"                    # 判据 3
    sym_one_pos = {"BTC": {"n": 30, "mean": 0.1},
                   "ETH": {"n": 25, "mean": -0.2},
                   "SOL": {"n": 15, "mean": -0.1}}
    assert go_no_go(_analysis(70, 0.05, 0.001, 0.01, sym_one_pos),
                    {})["verdict"] == "NO-GO"                    # 判据 4
