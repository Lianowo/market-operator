"""临时诊断:v21.2 平衡箱模型 vs 历史单带区 的 数量/间距/年龄 对照。"""
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from market_operator import data as mo_data
from market_operator.structure import Params, ATR, SwingTracker, Pivot, PivotKind
from market_operator.balance import BalanceBook


def run_balance(params, label):
    df = mo_data.load("BTCUSDT")
    atr = ATR(params.atr_n)
    swing = SwingTracker(params.k_rev)
    bb = BalanceBook(params)
    bars = []
    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        ts, o, h, l, c, v = row.ts, row.open, row.high, row.low, row.close, row.volume
        index = len(bars)
        atr_val = atr.update(h, l, c)
        bars.append((ts, o, h, l, c, v))
        pivot = swing.update(index, o, h, l, c, atr_val)
        if pivot is not None:
            pivot = Pivot(pivot.price, pivot.forming_index, pivot.known_index,
                          pivot.kind, close_extreme=bars[pivot.forming_index][4])
            bb.on_pivot(pivot, pivot.known_index, atr_val)
        prev_close = bars[-2][4] if len(bars) > 1 else None
        bb.on_bar(index, o, h, l, c, prev_close, atr_val)
    boxes = sorted(bb.active(), key=lambda x: x.lo)
    n = len(boxes)
    gaps = np.array([(b.lo - a.hi) / (atr._value or 1) for a, b in zip(boxes, boxes[1:])])
    width = np.array([(x.hi - x.lo) / (atr._value or 1) for x in boxes])
    age = np.array([len(bars) - 1 - x.last_change_known for x in boxes])
    print(f"=== {label} ===")
    print(f"boxes={n} pivots_swing={len(bars)}")
    if n:
        print(f"  间距中位 {np.median(gaps):+.2f} ATR | 重叠(<=0) {sum(1 for g in gaps if g<=0)}/{len(gaps)} | >0.5ATR {sum(1 for g in gaps if g>0.5)}")
        print(f"  箱宽中位 {np.median(width):.2f} ATR | 宽>3ATR {sum(1 for w in width if w>3)}")
        print(f"  年龄中位 {np.median(age):.0f} | max {int(age.max())} | >200 {sum(1 for a in age if a>200)}")


def main():
    base = Params.from_json(ROOT / "config" / "canonical.json")
    d = dict(vars(base)); d["stale_after_bars"] = 200
    run_balance(Params(**d), "v21.2 平衡箱 stale=200")
    d["stale_after_bars"] = 100
    run_balance(Params(**d), "v21.2 平衡箱 stale=100")


if __name__ == "__main__":
    main()
