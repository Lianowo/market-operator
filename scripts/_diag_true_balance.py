"""实测对比诊断:旧版滑动窗口 BalanceDetector vs 新版波段定框 TrueBalanceDetector。"""
from __future__ import annotations

from datetime import datetime, timezone
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data
from market_operator.balance_detect import BalanceDetector, BalanceParams
from market_operator.structure import ATR, MarketStructure, Params
from market_operator.true_balance import TrueBalanceDetector, TrueBalanceParams


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _slope10(bars: list[tuple], start: int, end: int, atr: float) -> float:
    n = end - start + 1
    if n < 4 or atr <= 0:
        return 0.0
    cs = [bars[i][4] for i in range(start, end + 1)]
    m = n // 2
    return abs(sum(cs[m:]) / max(n - m, 1) - sum(cs[:m]) / max(m, 1)) / max(n / 2.0, 1.0) * 10.0 / atr


def evaluate(symbol: str, gran: str) -> dict:
    df = mo_data.load(symbol, gran)
    rows = list(df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False, name=None))

    p_old = Params(use_balance_detector=True, balance_quality_min=0.35,
                   box_min_rotations=2, box_keep_broken_bars=12, box_keep_active_bars=24)
    ms_old = MarketStructure(p_old)
    old_det = BalanceDetector(BalanceParams(quality_min=0.35))
    atr1 = ATR(14)

    t0 = time.perf_counter()
    old_zcnt: list[int] = []
    old_drift, old_has = 0, 0
    cache: list[tuple] = []
    for idx, r in enumerate(rows):
        cache.append(r)
        av = atr1.update(r[2], r[3], r[4])
        ms_old.update(r)
        old_det.push(r, av)
        old_zcnt.append(len(ms_old.zones))
        if av > 0 and idx >= 20:
            bxs = [b for b in old_det.score() if b.rotations >= 2]
            if bxs:
                old_has += 1
                if _slope10(cache, bxs[0].start_index, bxs[0].end_index, av) > 0.35:
                    old_drift += 1
    old_ms = (time.perf_counter() - t0) * 1000.0

    atr2 = ATR(14)
    new_det = TrueBalanceDetector(TrueBalanceParams())
    t1 = time.perf_counter()
    in_box, max_br, touches, revs = 0, 0, 0, 0
    for idx, r in enumerate(rows):
        av = atr2.update(r[2], r[3], r[4])
        br = new_det.push(r, av)
        if br is None or av <= 0:
            continue
        max_br = max(max_br, int(br.support is not None) + int(br.resistance is not None))
        if br.mode == "IN_BOX" and br.active_box is not None:
            in_box += 1
            b = br.active_box
            if idx + 6 < len(rows):
                if r[3] <= b.val and r[4] >= b.lo_ext:
                    touches += 1
                    fhi = max(rows[k][2] for k in range(idx + 1, idx + 7))
                    if fhi - b.val >= 0.6 * av or fhi >= b.poc:
                        revs += 1
                elif r[2] >= b.vah and r[4] <= b.hi_ext:
                    touches += 1
                    flo = min(rows[k][3] for k in range(idx + 1, idx + 7))
                    if b.vah - flo >= 0.6 * av or flo <= b.poc:
                        revs += 1
    new_ms = (time.perf_counter() - t1) * 1000.0

    boxes = list(new_det.history_boxes) + ([new_det.active_box] if new_det.active_box else [])
    samples = [
        {
            "id": b.box_id,
            "span": f"{_iso(rows[b.start_index][0])}->{_iso(rows[b.end_index][0])}",
            "bars": b.end_index - b.start_index + 1,
            "sup": f"[{b.lo_ext:.2f}, {b.val:.2f}]",
            "res": f"[{b.vah:.2f}, {b.hi_ext:.2f}]",
            "poc": round(b.poc, 2),
            "w": round(b.width_atr, 2),
            "slp": round(b.slope_atr_per_10, 3),
            "dw": round(b.dwell, 2),
            "st": b.state,
        }
        for b in boxes[-4:]
    ]
    return {
        "sym": symbol, "tf": gran, "n": len(rows),
        "old_ms": round(old_ms, 1), "new_ms": round(new_ms, 1),
        "old_avg_z": round(sum(old_zcnt) / max(len(old_zcnt), 1), 2),
        "old_max_z": max(old_zcnt) if old_zcnt else 0,
        "old_drift": round(old_drift / max(old_has, 1) * 100.0, 1),
        "new_cnt": len(boxes),
        "in_box": round(in_box / max(len(rows), 1) * 100.0, 1),
        "max_br": max_br,
        "new_drift": round(sum(1 for b in boxes if b.slope_atr_per_10 > 0.35) / max(len(boxes), 1) * 100.0, 1),
        "rev6": round(revs / max(touches, 1) * 100.0, 1),
        "touches": touches,
        "samples": samples,
    }


def main() -> None:
    print("=" * 114)
    print("旧版滑动窗口 BalanceDetector vs 新版波段定框 TrueBalanceDetector + ActiveBracket 实测对比")
    print("=" * 114)
    res = []
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for gran in ("1H", "2H", "4H"):
            r = evaluate(sym, gran)
            res.append(r)
            print(
                f"{r['sym']:8s} {r['tf']:3s}(n={r['n']:5d}) | "
                f"耗时 旧:{r['old_ms']:6.1f}ms 新:{r['new_ms']:5.1f}ms | "
                f"活跃区 旧(均/峰):{r['old_avg_z']:4.1f}/{r['old_max_z']:2d} 新夹板上限:{r['max_br']} | "
                f"斜通道误报 旧:{r['old_drift']:4.1f}% 新:{r['new_drift']:4.1f}% | "
                f"真箱:{r['new_cnt']:3d}(占比{r['in_box']:4.1f}%) | "
                f"内沿触碰6根回归率:{r['rev6']:4.1f}%(n={r['touches']})"
            )
    print("\n" + "-" * 114)
    print("4H 周期最近识别出的真实平衡箱样例明细:")
    print("-" * 114)
    for r in res:
        if r["tf"] == "4H":
            print(f"\n[{r['sym']} 4H]")
            for s in r["samples"]:
                print(
                    f"  Box#{s['id']:02d} | {s['span']} ({s['bars']:2d}根) | "
                    f"支撑带 {s['sup']} <-> POC {s['poc']} <-> 阻力带 {s['res']} | "
                    f"宽={s['w']}ATR 斜率={s['slp']} 驻留={s['dw']} 状态={s['st']}"
                )


if __name__ == "__main__":
    main()
