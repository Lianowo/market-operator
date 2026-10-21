"""诊断:框子识别模块 在 1h BTC 上的输出。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from market_operator import data as mo_data
from market_operator.structure import ATR, Params
from market_operator.balance_detect import BalanceDetector, BalanceParams


def main():
    df = mo_data.load("BTCUSDT")
    base = Params.from_json(ROOT / "config" / "canonical.json")
    atr = ATR(base.atr_n)
    det = BalanceDetector(BalanceParams())
    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        av = atr.update(row.high, row.low, row.close)
        det.push((row.ts, row.open, row.high, row.low, row.close, row.volume), av)

    boxes = det.score()
    print(f"ATR={atr._value:.0f}  当前 bar 识别出 {len(boxes)} 个平衡箱(去重后):")
    for b in sorted(boxes, key=lambda x: x.lo):
        print(f"  [{b.lo:.0f}, {b.hi:.0f}] w={(b.hi-b.lo)/atr._value:.1f}ATR "
              f"q={b.quality:.3f} eff={b.efficiency:.2f} dwell={b.dwell:.2f} rot={b.rotations}")


if __name__ == "__main__":
    main()
