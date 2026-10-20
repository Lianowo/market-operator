"""tape 特征层(v10 §B,只读注解,不参与判定——积累 ≥60 区域接触前禁生效)。

数据:data/tape/{SYM}/{YYYYMMDD}.jsonl(tape_recorder 产出,行含 ts/price/size/side)。
side="buy"=主动买。特征统一为窗口聚合,供决策日志注解与后续测量使用。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TAPE_DIR = ROOT / "data" / "tape"


def iter_tape(symbol, t0_ms=None, t1_ms=None, day=None):
    days = [day] if day else sorted(p.stem for p in (TAPE_DIR / symbol).glob("*.jsonl"))
    for d in days:
        p = TAPE_DIR / symbol / f"{d}.jsonl"
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                t = json.loads(line)
                ts = int(t["ts"])
                if (t0_ms is None or ts >= t0_ms) and (t1_ms is None or ts <= t1_ms):
                    yield ts, float(t["price"]), float(t["size"]), t["side"]


def window_stats(symbol, t0_ms, t1_ms, day=None):
    """窗口聚合:delta(主动买-卖量)、vol、n、大单占比、价格效率|move|/vol、cvd 端点。"""
    vol_b = vol_s = 0.0
    n = 0
    cvd = 0.0
    big_thr_share_num = 0.0
    first_p = last_p = None
    sizes = []
    for ts, price, size, side in iter_tape(symbol, t0_ms, t1_ms, day):
        v = price * size
        if side == "buy":
            vol_b += v
            cvd += v
        else:
            vol_s += v
            cvd -= v
        sizes.append(v)
        n += 1
        first_p = first_p or price
        last_p = price
    vol = vol_b + vol_s
    if n == 0:
        return {"n": 0}
    big_thr = _q90(sizes)
    big_vol = sum(v for v in sizes if v >= big_thr)
    move = abs((last_p - first_p)) if first_p else 0.0
    ref = first_p or 1.0
    return {
        "n": n, "vol": vol, "delta": vol_b - vol_s, "delta_share": (vol_b - vol_s) / vol if vol else 0.0,
        "cvd_end": cvd, "big_share": big_vol / vol if vol else 0.0,
        "move_pct": move / ref * 100, "eff_per_million": move / (vol / 1e6) if vol else 0.0,
    }


def _q90(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[int(len(s) * 0.9)] if len(s) >= 10 else s[-1]
