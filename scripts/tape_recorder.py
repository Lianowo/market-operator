"""逐笔流(tape)记录器:轮询 Bitget 公开 fills,按 tradeId 去重,逐日写 JSONL。

用途:为"区域上的吸收/主动盘"这条从未被检验的信息域建立前向数据底座。
我们不声称它有效——先接数据,再谈测量。手动启动/停止,不做成自动守护。
用法:
  python scripts/tape_recorder.py BTCUSDT ETHUSDT --minutes 480
记录格式: {"ts":ms,"tradeId":str,"price":f,"size":f,"side":"buy|sell"} (side=主动方)
"""
import argparse
import json
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ("https://api.bitget.com/api/v2/mix/market/fills"
       "?symbol={sym}&productType=USDT-FUTURES&limit=100")
OUT = ROOT / "data" / "tape"


def fetch(sym):
    with urllib.request.urlopen(API.format(sym=sym), timeout=10) as r:
        d = json.load(r)
    if d.get("code") != "00000":
        raise RuntimeError(d)
    return d["data"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+", default=["BTCUSDT"])
    ap.add_argument("--minutes", type=float, default=0.0,
                    help="0=直到 Ctrl+C")
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()

    seen = {s: deque(maxlen=5000) for s in a.symbols}
    seen_set = {s: set() for s in a.symbols}
    end = time.time() + a.minutes * 60 if a.minutes else None
    total = {s: 0 for s in a.symbols}
    try:
        while end is None or time.time() < end:
            for s in a.symbols:
                try:
                    fills = fetch(s)
                except Exception as e:
                    print(f"[{s}] fetch err: {e}", flush=True)
                    continue
                day = time.strftime("%Y%m%d", time.gmtime())
                path = OUT / s / f"{day}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                new = 0
                with open(path, "a", encoding="utf-8") as f:
                    for t in reversed(fills):
                        tid = t["tradeId"]
                        if tid in seen_set[s]:
                            continue
                        seen_set[s].add(tid)
                        seen[s].append(tid)
                        if len(seen_set[s]) > 10000:
                            seen_set[s].discard(seen[s][0])
                        f.write(json.dumps(t, separators=(",", ":")) + "\n")
                        new += 1
                total[s] += new
                if new:
                    print(f"[{s}] +{new} (累计 {total[s]}) 最新 {day}.jsonl", flush=True)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    print("done", total)


if __name__ == "__main__":
    sys.exit(main())
