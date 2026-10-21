"""诊断:平衡箱/单带区的边缘 是否锚定在真实 pivot 极值上(准确性)。"""
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from market_operator import data as mo_data
from market_operator.structure import Params, ATR, SwingTracker, Pivot, PivotKind
from market_operator.balance import BalanceBook


def main():
    params = Params(**{**vars(Params.from_json(ROOT / "config" / "canonical.json")),
                       "stale_after_bars": 100})
    df = mo_data.load("BTCUSDT")
    atr = ATR(params.atr_n)
    swing = SwingTracker(params.k_rev)
    bb = BalanceBook(params)
    bars = []
    pivots = []
    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        ts, o, h, l, c, v = row.ts, row.open, row.high, row.low, row.close, row.volume
        idx = len(bars)
        av = atr.update(h, l, c)
        bars.append((ts, o, h, l, c, v))
        pv = swing.update(idx, o, h, l, c, av)
        if pv is not None:
            pv = Pivot(pv.price, pv.forming_index, pv.known_index, pv.kind,
                       close_extreme=bars[pv.forming_index][4])
            pivots.append(pv)
            bb.on_pivot(pv, pv.known_index, av)
        bb.on_bar(idx, o, h, l, c, bars[-2][4] if len(bars) > 1 else None, av)

    a = atr._value
    boxes = sorted(bb.active(), key=lambda x: x.lo)
    print(f"ATR={a:.0f}  平衡箱 {len(boxes)} 个:")
    for b in boxes:
        # 每条边最近的 pivot 距离(以 ATR 计),衡量"是否锚定在真实极值"
        dlo = min(abs(b.lo - p.price) / a for p in pivots)
        dhi = min(abs(b.hi - p.price) / a for p in pivots)
        print(f"  [{b.lo:.0f}, {b.hi:.0f}] w={(b.hi-b.lo)/a:.1f}ATR | 下缘最近pivot {dlo:.2f}ATR 上缘 {dhi:.2f}ATR | {b.state}")

    print(f"\n最近 12 个 pivot(真实摆动极值):")
    for p in pivots[-12:]:
        print(f"  {'HIGH' if p.kind is PivotKind.HIGH else 'LOW '} price={p.price:.0f} close={p.close_extreme:.0f}")


if __name__ == "__main__":
    main()
