"""临时诊断:对比区域模型参数(年龄过期/显著性过滤)对 数量/间距/年龄 的影响。"""
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from market_operator import data as mo_data
from market_operator.structure import Params, MarketStructure, ZoneRole


def measure(params, label):
    df = mo_data.load("BTCUSDT")
    ms = MarketStructure(params)
    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        ms.update((row.ts, row.open, row.high, row.low, row.close, row.volume))
    n_bars = ms.bars_seen
    zones = ms.zones
    atr = ms.atr()
    print(f"\n=== {label} ===")
    print(f"bars={n_bars} pivots={len(ms.pivots)} active_zones={len(zones)} ATR={atr:.1f}")
    for role in (ZoneRole.RESISTANCE, ZoneRole.SUPPORT):
        zs = sorted([z for z in zones if z.role is role], key=lambda z: z.lo)
        gaps = np.array([(b.lo - a.hi) / atr for a, b in zip(zs, zs[1:])]) if len(zs) > 1 else np.array([])
        age = np.array([n_bars - 1 - z.last_change_known for z in zs]) if zs else np.array([])
        if len(gaps):
            print(f"[{role.value}] n={len(zs)} 间距中位{np.median(gaps):+.2f}ATR 重叠{sum(1 for g in gaps if g<=0)} 0~0.5{sum(1 for g in gaps if 0<g<=0.5)} >1{sum(1 for g in gaps if g>1)}")
        if len(age):
            print(f"          年龄中位{np.median(age):.0f} p90{np.percentile(age,90):.0f} max{int(age.max())} >200{sum(1 for a in age if a>200)}")


def main():
    base = Params.from_json(ROOT / "config" / "canonical.json")
    measure(base, "历史 (stale=0, amp=0)")
    d = dict(vars(base))
    d["stale_after_bars"] = 200
    measure(Params(**d), "v21 年龄过期 (stale=200)")
    d["min_amplitude_atr"] = 0.5
    measure(Params(**d), "v21 年龄+显著性 (stale=200, amp>=0.5)")
    d["stale_after_bars"] = 100
    measure(Params(**d), "v21 年龄+显著性 (stale=100, amp>=0.5)")


if __name__ == "__main__":
    main()
