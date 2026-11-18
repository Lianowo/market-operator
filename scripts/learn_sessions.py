"""自动判定非加密合约的底层市场时段(只在高置信时给精确日历,其余工作日兜底)。

为什么必须自动:Bitget 的合约接口**没有交易时段字段**(实测只有
``symbolType=perpetual`` / ``symbolStatus=normal`` / ``maintainTime=''`` /
``offTime=-1`` / ``limitOpenTime=-1``,以及 ``fundInterval`` 4 或 8 /
``maxLever``),交易所不告诉"这只底层是美股/COMEX/港交所"。

为什么不能只靠成交分布:实测这些 RWA 永续在**工作日几乎 24 小时都有真实活动**
(例:黄金、美股个股在亚洲时段一样有成交与波动),所以"按成交额选窗口"会把
美股和商品混在一起。这里只保留**可复现且方向安全**的三条判据:

1. ``rth_share``  —— 美股现货 09:30-16:00 ET 的活动占比;
2. ``asia_share`` —— 亚洲共同交易区 00:00-10:00 UTC 的活动占比;
3. ``break_ratio``—— CME 每日 17:00-18:00 ET 维护小时的活动中位数/其余小时
   中位数(期货类会显著塌陷,现货不会)。

判定(保守优先,宁可落回 ``weekday_only`` 也不错误地关掉一个正常市场):
* ``rth >= 0.26`` 且 ``asia <= 0.32`` 且 ``break_ratio >= 0.55`` ⇒ ``us_equity``
* 否则 ``asia >= 0.42`` 且 ``rth <= 0.18`` ⇒ ``asia_equity``
* 其余 ⇒ ``weekday_only``(工作日可开、周末关闭;假日/半日市/每日维护由
  运行时 ``quiet_window`` 用同一时段的历史活动中位数自动拦住)

产物 ``data/calendars/assignments.json`` 由 ``market_operator.sessions`` 读取,
优先级:运维覆盖 > 命名族规则 > 自动推断 > 兜底。想强制某只合约仍在
``data/calendars/overrides.json`` 显式覆盖。

用法:
  python scripts/learn_sessions.py                       # 全量 RWA → assignments.json
  python scripts/learn_sessions.py --days 28 --workers 6
  python scripts/learn_sessions.py --symbols AUSDT,BUSDT --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from market_operator import sessions
from market_operator.bitget_live import BitgetLiveClient

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "calendars" / "assignments.json"
ET = ZoneInfo("America/New_York")
MS_DAY = 86_400_000


def rwa_symbols(explicit: str | None, cli: BitgetLiveClient) -> list[str]:
    if explicit:
        return sorted({s.strip().upper() for s in explicit.split(",") if s.strip()})
    rows = cli.contracts()
    return sorted({str(r.get("symbol")) for r in rows
                   if str(r.get("isRwa") or "").upper() == "YES"})


def fetch_bars(cli: BitgetLiveClient, symbol: str, days: int,
               granularity: str = "1H", page: int = 200) -> list[tuple]:
    """按 endTime 往回翻,拿到 days 天的已收盘 K 线(升序去重)。"""
    end = int(time.time() * 1000)
    start = end - int(days) * MS_DAY
    got: dict[int, tuple] = {}
    cursor = end
    while cursor > start:
        rows = cli.history_candles(symbol, granularity, end_ms=cursor, limit=page)
        if not rows:
            break
        for b in rows:
            got[int(b[0])] = b
        oldest = min(int(b[0]) for b in rows)
        if oldest <= start or oldest >= cursor:
            break
        cursor = oldest
    return [got[k] for k in sorted(got)]


class Masks:
    """日历窗口掩码((日历, ts) 缓存,避免逐标的重复时区换算)。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self._cache: dict[tuple[str, int], bool] = {}

    def of(self, calendar: str, ts: int) -> bool:
        key = (calendar, int(ts))
        hit = self._cache.get(key)
        if hit is None:
            hit = sessions.in_session(calendar, int(ts), self.cfg)
            self._cache[key] = hit
        return hit


def features(bars: list[tuple], masks: Masks) -> dict:
    """活动分布特征(range 比 volume 更能反映"价格发现是否在发生")。"""
    w = [max(0.0, float(b[2]) - float(b[3])) for b in bars]
    ts = [int(b[0]) for b in bars]
    total = sum(w) or 1.0
    base = [masks.of("weekday_only", t) for t in ts]
    base_vol = sum(x for x, m in zip(w, base) if m) or 1.0

    def share(calendar: str) -> float:
        inside = sum(x for x, t, m in zip(w, ts, base)
                     if m and masks.of(calendar, t))
        return inside / base_vol

    brk = [x for x, t in zip(w, ts)
           if datetime.fromtimestamp(t / 1000, ET).hour == 17]
    rest = [x for x, t in zip(w, ts)
            if datetime.fromtimestamp(t / 1000, ET).hour != 17]
    med = statistics.median(rest) if rest else 0.0
    brk_ratio = (statistics.median(brk) / med) if brk and med > 0 else 9.9
    return {"rth": share("us_equity"), "asia": share("asia_equity"),
            "brk": brk_ratio, "weekend_share": (total - base_vol) / total,
            "bars": len(bars)}


def classify(feats: dict, *, min_bars: int = 60,
             us_rth: float = 0.40, us_asia_max: float = 0.20,
             brk_min: float = 0.60, asia_min: float = 0.42,
             asia_rth_max: float = 0.18) -> tuple[str, str]:
    """保守判定:只有**显著**证据才收紧窗口,否则交给 weekday_only + 静默窗口。"""
    if feats["bars"] < min_bars:
        return "weekday_only", "insufficient_bars"
    if (feats["rth"] >= us_rth and feats["asia"] <= us_asia_max
            and feats["brk"] >= brk_min):
        return "us_equity", "us_rth_concentration"
    if feats["asia"] >= asia_min and feats["rth"] <= asia_rth_max:
        return "asia_equity", "asia_session_concentration"
    return "weekday_only", "no_confident_session"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None,
                    help="逗号分隔;缺省取全部 isRwa=YES 合约")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--granularity", default="1H")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cli = BitgetLiveClient(None)
    specs = {c["symbol"]: c for c in cli.contracts()}
    syms = rwa_symbols(args.symbols, cli)
    cfg = sessions.load_config()
    masks = Masks(cfg)
    overrides = sessions._load_overrides()      # 运维显式覆盖优先,不再重复推断

    def work(sym: str) -> tuple[str, dict]:
        resolved, source = sessions._resolve(sym, cfg, overrides, {})
        if source in ("override", "pattern"):
            return sym, {"skipped": source, "calendar": resolved}
        try:
            bars = fetch_bars(cli, sym, args.days, args.granularity)
        except Exception as exc:                 # noqa: BLE001 - 单标的失败不拖全局
            return sym, {"skipped": f"fetch_error:{type(exc).__name__}",
                         "calendar": "weekday_only"}
        feats = features(bars, masks)
        cal, why = classify(feats)
        sp = specs.get(sym) or {}
        return sym, {"calendar": cal, "reason": why,
                     "fund_interval": sp.get("fundInterval"),
                     "max_lever": sp.get("maxLever"), **feats}

    done = {"n": 0}
    lock = threading.Lock()

    def work_tracked(sym: str) -> tuple[str, dict]:
        out = work(sym)
        with lock:
            done["n"] += 1
            if done["n"] % 25 == 0 or done["n"] == len(syms):
                print(f"[learn] fetched {done['n']}/{len(syms)}", flush=True)
        return out

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        results = dict(ex.map(work_tracked, syms))

    assignments = {s: r["calendar"] for s, r in results.items()
                   if r.get("calendar")}
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["calendar"]] = counts.get(r["calendar"], 0) + 1
    print("[learn] symbols=" + str(len(results)) + " " +
          ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    fallback = [s for s, r in results.items() if r["calendar"] == "weekday_only"]
    if fallback:
        print(f"[learn] weekday_only={len(fallback)} e.g. {fallback[:8]}")

    payload = {
        "schema": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "rth+asia+break_ratio",
        "params": {"days": args.days, "granularity": args.granularity},
        "assignments": assignments,
        "diagnostics": results,
    }
    if args.dry_run:
        print("[learn] dry-run, not written")
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, out)
    print(f"[learn] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

