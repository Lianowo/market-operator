"""框子识别模块的独立单元测试(合成数据)。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from market_operator.balance_detect import BalanceDetector, BalanceParams


def _feed(closes, highs=None, lows=None, opens=None, unit=10.0):
    det = BalanceDetector(BalanceParams())
    n = len(closes)
    highs = highs or [c + 2 for c in closes]
    lows = lows or [c - 2 for c in closes]
    opens = opens or closes
    for i in range(n):
        det.push((i, opens[i], highs[i], lows[i], closes[i], 1.0), unit)
    return det


def _range_closes(n_cycles=4):
    """缓慢震荡 100~110(宽度≈unit=10),相邻 bar 重叠,贴近真实区间。"""
    out = []
    for _ in range(n_cycles):
        out += list(range(100, 111)) + list(range(110, 99, -1))
    return out


def test_range_is_detected_as_balance():
    """震荡区间 → 高质量平衡箱,箱宽覆盖震荡范围。"""
    closes = _range_closes()
    det = _feed(closes, unit=10.0)
    boxes = det.score()
    assert boxes, "震荡区间应识别出平衡箱"
    best = max(boxes, key=lambda b: b.quality)
    assert best.quality > 0.4, f"质量分过低 {best.quality}"
    assert best.lo < 102 and best.hi > 108  # 箱覆盖震荡范围
    assert best.efficiency < 0.4            # 方向效率低 = 平衡


def test_trend_is_not_a_balance():
    """单调趋势 → 方向效率高,不应识别为高质量平衡箱。"""
    closes = [100 + i * 2 for i in range(60)]  # 单调上升(慢,但持续)
    det = _feed(closes, unit=10.0)
    boxes = det.score()
    if boxes:
        assert all(b.quality < 0.4 for b in boxes), "趋势不应产生高质量平衡箱"


def test_wick_spike_does_not_stretch_box():
    """单根影线尖刺不拉宽箱体(稳健 12%/88% 边界)。"""
    closes = _range_closes()
    highs = [c + 2 for c in closes]
    lows = [c - 2 for c in closes]
    highs[5] = 200  # 一根尖刺
    det = _feed(closes, highs=highs, lows=lows, unit=10.0)
    boxes = det.score()
    assert boxes
    best = max(boxes, key=lambda b: b.quality)
    assert best.hi < 120, f"尖刺不应把上沿拉到 {best.hi}"


def test_empty_history_gives_no_box():
    det = BalanceDetector(BalanceParams())
    assert det.score() == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
