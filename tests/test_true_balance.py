"""真平衡箱与现价近邻夹板引擎单元测试(合成结构数据)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from market_operator.true_balance import TrueBalanceDetector, TrueBalanceParams


def _feed_bars(bars, atr=10.0, params=None):
    det = TrueBalanceDetector(params or TrueBalanceParams())
    last_bracket = None
    for b in bars:
        last_bracket = det.push(b, atr)
    return det, last_bracket


def _make_horizontal_box_bars(cycles=3, lo=100.0, hi=130.0):
    """生成清晰的水平震荡箱体:在 [100, 130] 内往返多次,实体对齐在 [103, 127]。"""
    bars = []
    ts = 1_700_000_000_000
    step = 3_600_000
    pts = [115.0]
    for _ in range(cycles):
        pts += [122.0, 127.0, 121.0, 114.0, 107.0, 103.0, 109.0, 115.0]
    prev = pts[0]
    for i, c in enumerate(pts):
        o = prev
        h = max(o, c) + (3.0 if c >= 126.0 else 1.5)
        l = min(o, c) - (3.0 if c <= 104.0 else 1.5)
        vol = 200.0 if 108.0 <= c <= 122.0 else 90.0
        bars.append((ts + i * step, o, h, l, c, vol))
        prev = c
    return bars


def test_horizontal_box_detected_with_dual_boundaries_and_bracket():
    bars = _make_horizontal_box_bars(cycles=3)
    det, br = _feed_bars(bars, atr=10.0)
    assert det.active_box is not None, "标准水平往返震荡应识别出活跃真平衡箱"
    box = det.active_box
    assert 100.0 <= box.val <= 108.0
    assert 122.0 <= box.vah <= 130.0
    assert box.lo_ext < box.val and box.hi_ext > box.vah
    assert 0.25 <= box.poc_pos <= 0.75
    assert box.slope_atr_per_10 <= 0.30

    assert br is not None and br.mode == "IN_BOX"
    assert br.support is not None and br.resistance is not None
    assert br.support.role == "SUPPORT" and br.support.opposite == pytest.approx(box.vah)
    assert br.resistance.role == "RESISTANCE" and br.resistance.opposite == pytest.approx(box.val)


def test_sloping_downtrend_channel_is_rejected():
    """45度震荡阴跌通道(每轮反弹高点与低点持续下移):绝不能误判成水平平衡箱接飞刀!"""
    bars = []
    ts = 1_700_000_000_000
    base = 200.0
    prev = base
    for i in range(40):
        # 既有来回重叠折返,但整体每 10 根下跌 12 点(atr=10 -> 斜率 1.2 ATR/10 bars)
        wave = 6.0 if (i % 4 in (1, 2)) else -6.0
        c = base - i * 1.2 + wave
        o = prev
        bars.append((ts + i * 3_600_000, o, max(o, c) + 2.0, min(o, c) - 2.0, c, 100.0))
        prev = c
    det, br = _feed_bars(bars, atr=10.0)
    assert det.active_box is None, "持续下移的阴跌斜通道必须被水平度硬约束拦截"
    assert len(det.history_boxes) == 0
    assert br is not None and br.mode == "MIGRATION"


def test_single_extreme_wick_spike_does_not_ruin_inner_val_vah():
    """箱体中途出现单根极端插针(high=220):内沿 VAH 保持在真实实体顶部,外沿受 wick_cap 保护。"""
    bars = list(_make_horizontal_box_bars(cycles=3))
    mid = len(bars) // 2
    t, o, _h, l, c, v = bars[mid]
    bars[mid] = (t, o, 220.0, l, c, v)
    det, _ = _feed_bars(bars, atr=10.0)
    assert det.active_box is not None
    assert det.active_box.vah < 132.0, f"单根长针不应把内沿 VAH 拉飞: {det.active_box.vah}"
    assert det.active_box.hi_ext <= det.active_box.vah + 10.0


def test_breakout_transitions_to_migration_with_flipped_support():
    """箱体向上连续有效突破后,老箱体退役,原箱顶自动转为现价下方唯一的 S/R Flip 支撑!"""
    bars = list(_make_horizontal_box_bars(cycles=3))
    last_ts = bars[-1][0]
    # 追加连续 3 根突破站稳在箱顶 130 上方的阳线
    for k, close_px in enumerate((136.0, 139.0, 141.0), start=1):
        o = 132.0 if k == 1 else close_px - 2.0
        bars.append((last_ts + k * 3_600_000, o, close_px + 1.5, o - 1.0, close_px, 150.0))
    det, br = _feed_bars(bars, atr=10.0)
    assert det.active_box is None
    assert len(det.history_boxes) == 1
    assert det.history_boxes[0].state == "BROKEN_UP"
    assert br is not None and br.mode == "MIGRATION"
    assert br.support is not None
    assert br.support.source_kind == "FLIPPED_BOX_EDGE"
    assert 122.0 <= br.support.outer <= br.support.inner <= 138.0
