"""资金费率抓取:Binance USDT-M 永续 `fapi/v1/fundingRate` → data/raw/funding/{SYM}.csv。

为什么需要:引擎里 `funding_per_8h` 是**常数**(默认 1bp/8h,方向无关、恒为成本)。
实测该假设与交易所真实结算差 5.4 倍且符号经常相反(见
reports/live_cost_audit_20261023/FUNDING_REPLAY.md)。要让回放口径可审计,
必须把真实结算表落盘,而不是每次跑都打网络。

口径(与 broker `_accrue_funding` 的符号约定一致):
- 结算点 `fundingTime`(毫秒 UTC),费率 `fundingRate`(有符号小数);
- 持仓在 (last_ts, ts] 内跨过的结算点逐个计提:成本 = notional · rate · direction
  (多 +1 / 空 −1),即 rate>0 时多头付费、空头收费;
- 落盘列 `funding_time,funding_rate`,升序去重。

用法:
  python scripts/fetch_funding.py [--symbols BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT]
      [--start-ts MS --end-ts MS] [--out data/raw/funding]

缺省窗口取自 `data/raw/{SYM}_1h.csv` 的首/末 ts(±2 天),与回放行情同窗。
离线可测:纯函数 `normalize(rows)` 不触网络。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
MANIFEST_PATH = DATA_DIR / "manifest.json"
FAPI = "https://fapi.binance.com/fapi/v1/fundingRate"

PAGE_LIMIT = 1000
SLEEP_S = 0.15
RETRIES = 4
RETRY_BACKOFF_S = 2.0
MS_DAY = 86_400_000


def frame_span(symbol: str, tf: str = "1h") -> tuple[int, int]:
    """本地行情首/末 ts(毫秒);缺失时抛错,避免抓到与回放无关的窗口。"""
    path = RAW_DIR / f"{symbol}_{tf}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    first = last = None
    with path.open(encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            if not line.strip():
                continue
            ts = int(line.split(",", 1)[0])
            if first is None:
                first = ts
            last = ts
    if first is None or last is None:
        raise ValueError(f"empty frame {path}")
    return first, last


def normalize(rows: list[dict]) -> list[tuple[int, float]]:
    """交易所原始行 → 升序去重的 (fundingTime, fundingRate)。"""
    seen: dict[int, float] = {}
    for x in rows:
        ts = int(x["fundingTime"])
        seen[ts] = float(x["fundingRate"])
    return sorted(seen.items())


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    out: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        for attempt in range(RETRIES):
            try:
                r = requests.get(FAPI, params={
                    "symbol": symbol, "startTime": cur,
                    "endTime": end_ms, "limit": PAGE_LIMIT}, timeout=20)
                r.raise_for_status()
                break
            except Exception:                    # noqa: BLE001 - 重试后仍失败则上抛
                if attempt == RETRIES - 1:
                    raise
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        rows = r.json()
        if not rows:
            break
        out.extend(rows)
        nxt = int(rows[-1]["fundingTime"]) + 1
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(SLEEP_S)
    return out


def validate(series: list[tuple[int, float]]) -> list[str]:
    """连续性检查:升序、无重复、间隔为整小时(交易所偶发 4h/1h 结算)。"""
    issues: list[str] = []
    for i in range(1, len(series)):
        gap = series[i][0] - series[i - 1][0]
        if gap <= 0:
            issues.append(f"non-monotonic at {series[i][0]}")
        elif gap % 3_600_000:
            issues.append(f"sub-hour gap {gap}ms at {series[i][0]}")
    return issues


def write_symbol(symbol: str, series: list[tuple[int, float]],
                 out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}.csv"
    body = ["funding_time,funding_rate"]
    body += [f"{ts},{rate:.10g}" for ts, rate in series]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "sha256": digest,
        "rows": len(series),
        "ts_first": series[0][0] if series else None,
        "ts_last": series[-1][0] if series else None,
        "source": "binance_fapi/fapi/v1/fundingRate",
        "note": "settlements every 8h (occasionally 4h/1h); signed rate",
    }


def update_manifest(records: dict) -> None:
    manifest: dict = {"files": {}}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    files = manifest.setdefault("files", {})
    for symbol, rec in records.items():
        files[f"binance_fapi_funding/{symbol}.csv"] = rec
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT")
    ap.add_argument("--start-ts", type=int, default=None)
    ap.add_argument("--end-ts", type=int, default=None)
    ap.add_argument("--out", default=str(RAW_DIR / "funding"))
    ap.add_argument("--no-manifest", action="store_true")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    records: dict = {}
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        lo, hi = frame_span(symbol)
        start = args.start_ts if args.start_ts is not None else lo - 2 * MS_DAY
        end = args.end_ts if args.end_ts is not None else hi + 2 * MS_DAY
        rows = fetch_funding(symbol, start, end)
        series = normalize(rows)
        issues = validate(series)
        rec = write_symbol(symbol, series, out_dir)
        rec["window"] = {"start": start, "end": end}
        rec["issues"] = issues
        records[symbol] = rec
        pos = sum(1 for _, r in series if r > 0) / max(len(series), 1)
        print(f"[{symbol}] rows={len(series)} "
              f"{time.strftime('%Y-%m-%d', time.gmtime(series[0][0] / 1000))}.."
              f"{time.strftime('%Y-%m-%d', time.gmtime(series[-1][0] / 1000))} "
              f"pos_share={pos:.1%} issues={len(issues)}")
        for msg in issues[:3]:
            print(f"  ! {msg}")

    if not args.no_manifest:
        update_manifest(records)
        print(f"[manifest] {MANIFEST_PATH}")
    print(f"[out] {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
