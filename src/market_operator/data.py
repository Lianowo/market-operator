"""Data layer: fetch/validate/hash Bitget USDT perpetual 1h OHLCV.

Methodology §1: Bitget mix/market/history-candles, BTCUSDT/ETHUSDT/SOLUSDT,
gap + duplicate + OHLC legality checks, SHA-256 archived in data/manifest.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://api.bitget.com/api/v2/mix/market/history-candles"
PAGE_LIMIT = 200
PAGE_SLEEP_S = 0.15
RETRIES = 5
RETRY_BACKOFF_S = 2.0

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"

STEP_MS = 3_600_000
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def _granularity_ms(granularity: str) -> int:
    units = {"m": 60_000, "H": 3_600_000, "D": 86_400_000, "W": 604_800_000}
    return int(granularity[:-1]) * units[granularity[-1]]


def _request_page(symbol: str, product_type: str, granularity: str, end_time_ms: int) -> list[list[str]]:
    params = {
        "symbol": symbol,
        "productType": product_type,
        "granularity": granularity,
        "limit": PAGE_LIMIT,
        "endTime": end_time_ms,
    }
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=20)
            body = resp.json()
            if resp.status_code == 200 and body.get("code") == "00000":
                return body.get("data") or []
            last_err = RuntimeError(f"bitget {resp.status_code} {body.get('code')}: {body.get('msg')}")
        except (requests.RequestException, ValueError) as e:
            last_err = e
        time.sleep(RETRY_BACKOFF_S * 2**attempt)
    raise RuntimeError(f"fetch failed for {symbol} endTime={end_time_ms}: {last_err}")


def fetch_klines(
    symbol: str,
    product_type: str = "USDT-FUTURES",
    granularity: str = "1H",
    years: float = 2,
) -> pd.DataFrame:
    """Page backwards through Bitget history-candles from the last closed bar."""
    step = _granularity_ms(granularity)
    now_ms = int(time.time() * 1000)
    last_closed = now_ms - (now_ms % step) - step
    start_bound = now_ms - int(years * 365 * 86_400_000)

    raw: list[list[str]] = []
    end_time = last_closed + step
    while end_time >= start_bound:
        page = _request_page(symbol, product_type, granularity, end_time)
        if not page:
            break
        raw.extend(page)
        page.sort(key=lambda r: int(r[0]))
        oldest = int(page[0][0])
        if oldest <= start_bound or len(page) < PAGE_LIMIT:
            break
        end_time = oldest  # endTime is exclusive: response ends strictly below it
        time.sleep(PAGE_SLEEP_S)

    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume", "_quote"])
    df = df.drop(columns="_quote").astype(
        {"ts": "int64", "open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "float64"}
    )
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df = df[df["ts"] <= last_closed].reset_index(drop=True)
    return df[COLUMNS]


def validate(df: pd.DataFrame, step_ms: int = STEP_MS) -> list[str]:
    issues: list[str] = []
    d = df.sort_values("ts").reset_index(drop=True)

    dup = d[d["ts"].duplicated(keep=False)]["ts"].unique()
    for ts in dup:
        issues.append(f"duplicate ts: {int(ts)}")

    ts = d["ts"].to_numpy()
    diffs = ts[1:] - ts[:-1]
    bad = (diffs != step_ms) & (diffs > 0)
    for i in bad.nonzero()[0]:
        missing = int(diffs[i] // step_ms) - 1
        issues.append(f"gap: {int(ts[i])} -> {int(ts[i + 1])} ({missing} bars missing)")

    bad_ohlc = d[(d["low"] > d["open"]) | (d["open"] > d["high"]) | (d["low"] > d["close"]) | (d["close"] > d["high"])]
    for _, row in bad_ohlc.iterrows():
        issues.append(f"bad OHLC at {int(row['ts'])}")

    bad_vol = d[d["volume"] <= 0]
    for _, row in bad_vol.iterrows():
        issues.append(f"non-positive volume at {int(row['ts'])}")

    return issues


def save(df: pd.DataFrame, symbol: str) -> Path:
    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{symbol}_1h.csv"
    df.to_csv(path, index=False, columns=COLUMNS)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    issues = validate(df)
    ts = df.sort_values("ts")["ts"]
    record = {
        "path": str(path.relative_to(DATA_DIR.parent)),
        "sha256": sha,
        "rows": int(len(df)),
        "ts_first": int(ts.iloc[0]),
        "ts_last": int(ts.iloc[-1]),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "span_days": round(float(ts.iloc[-1] - ts.iloc[0]) / 86_400_000, 2),
        "validation_issues": issues,
    }

    manifest: dict = {"files": {}}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[f"{symbol}_1h.csv"] = record
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / "raw" / f"{symbol}_1h.csv", dtype={"ts": "int64"})
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="market_operator.data")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = sub.add_parser("fetch")
    fetch_cmd.add_argument("--symbols", required=True, help="comma separated, e.g. BTCUSDT,ETHUSDT")
    fetch_cmd.add_argument("--years", type=float, default=2)
    fetch_cmd.add_argument("--product-type", default="USDT-FUTURES")
    fetch_cmd.add_argument("--granularity", default="1H")
    args = parser.parse_args()

    if args.command == "fetch":
        for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            df = fetch_klines(symbol, product_type=args.product_type, granularity=args.granularity, years=args.years)
            issues = validate(df)
            path = save(df, symbol)
            first, last = df["ts"].iloc[0], df["ts"].iloc[-1]
            span = (last - first) / 86_400_000
            print(f"{symbol}: {len(df)} bars, {span:.1f} days, {len(issues)} validation issues -> {path}")
            for issue in issues:
                print(f"  {issue}")


if __name__ == "__main__":
    main()
