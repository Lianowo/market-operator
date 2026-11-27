"""Data layer: fetch, validate, and hash Bitget USDT perpetual OHLCV frames.

Supports arbitrary candle granularities (``1H``, ``15m``, ``30m``, etc.) with
strict gap, duplicate, OHLC legality, and fine-to-coarse grid alignment checks,
archiving SHA-256 checksums in ``data/manifest.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from .exceptions import DataIntegrityError, ExchangeError
from .timeframes import MS_PER_DAY, MS_PER_HOUR, normalize_tf_label, tf_to_ms

BASE_URL = "https://api.bitget.com/api/v2/mix/market/history-candles"
PAGE_LIMIT = 200
PAGE_SLEEP_S = 0.15
RETRIES = 5
RETRY_BACKOFF_S = 2.0
HTTP_TIMEOUT_S = 20
DAYS_PER_YEAR = 365

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"

STEP_MS = MS_PER_HOUR
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def _granularity_ms(granularity: str) -> int:
    return tf_to_ms(granularity)


def _granularity_label(granularity: str) -> str:
    """Canonical filename label (e.g. ``1H`` -> ``1h``, ``15m`` -> ``15m``)."""
    return normalize_tf_label(granularity)


def csv_bytes(df: pd.DataFrame) -> bytes:
    """Encode OHLCV DataFrame as deterministic UTF-8 LF-terminated CSV bytes."""
    buf = StringIO()
    df.to_csv(buf, index=False, columns=COLUMNS, lineterminator="\n")
    return buf.getvalue().encode("utf-8")


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
            resp = requests.get(BASE_URL, params=params, timeout=HTTP_TIMEOUT_S)
            body = resp.json()
            if resp.status_code == 200 and body.get("code") == "00000":
                return body.get("data") or []
            last_err = ExchangeError(f"bitget {resp.status_code} {body.get('code')}: {body.get('msg')}")
        except (requests.RequestException, ValueError) as e:
            last_err = e
        time.sleep(RETRY_BACKOFF_S * 2**attempt)
    raise ExchangeError(f"fetch failed for {symbol} endTime={end_time_ms}: {last_err}")


def fetch_klines(
    symbol: str,
    product_type: str = "USDT-FUTURES",
    granularity: str = "1H",
    years: float = 2,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> pd.DataFrame:
    """Page backwards through Bitget history-candles from the last closed bar.

    When ``start_ts`` / ``end_ts`` (inclusive bar open timestamps in ms) are
    provided, they define an explicit fetch window instead of deriving bounds
    from ``years`` and wall-clock time.
    """
    step = _granularity_ms(granularity)
    now_ms = int(time.time() * 1000)
    last_closed = now_ms - (now_ms % step) - step if end_ts is None else int(end_ts)
    start_bound = now_ms - int(years * DAYS_PER_YEAR * MS_PER_DAY) if start_ts is None else int(start_ts)

    raw: list[list[str]] = []
    end_time = last_closed + step
    while end_time >= start_bound:
        page = _request_page(symbol, product_type, granularity, end_time)
        if not page:
            break
        raw.extend(page)
        page.sort(key=lambda r: int(r[0]))
        oldest = int(page[0][0])
        # 终止条件只看「是否已翻到区间下界」与「页是否还在推进」,不用
        # len(page) < PAGE_LIMIT:Bitget 单页上限随粒度收窄(1D=90/12H=180,
        # 均 < PAGE_LIMIT=200),按长度判会在一页后就收工,把长历史静默截断。
        if oldest <= start_bound or oldest >= end_time:
            break
        end_time = oldest  # endTime is exclusive: response ends strictly below it
        time.sleep(PAGE_SLEEP_S)

    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume", "_quote"])
    df = df.drop(columns="_quote").astype(
        {"ts": "int64", "open": "float64", "high": "float64", "low": "float64", "close": "float64", "volume": "float64"}
    )
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df = df[df["ts"] <= last_closed]
    if start_ts is not None:
        df = df[df["ts"] >= start_bound]
    df = df.reset_index(drop=True)
    return df[COLUMNS]


def validate(df: pd.DataFrame, step_ms: int = STEP_MS) -> list[str]:
    issues: list[str] = []
    d = df.sort_values("ts").reset_index(drop=True)

    dup = d[d["ts"].duplicated(keep=False)]["ts"].unique()
    for ts_val in dup:
        issues.append(f"duplicate ts: {int(ts_val)}")

    ts = d["ts"].to_numpy()
    diffs = ts[1:] - ts[:-1]
    bad = (diffs != step_ms) & (diffs > 0)
    for i in bad.nonzero()[0]:
        missing = int(diffs[i] // step_ms) - 1
        issues.append(f"gap: {int(ts[i])} -> {int(ts[i + 1])} ({missing} bars missing)")

    op = d["open"].to_numpy()
    hi = d["high"].to_numpy()
    lo = d["low"].to_numpy()
    cl = d["close"].to_numpy()
    bad_ohlc_mask = (lo > op) | (op > hi) | (lo > cl) | (cl > hi)
    for ts_val in ts[bad_ohlc_mask]:
        issues.append(f"bad OHLC at {int(ts_val)}")

    bad_vol_mask = d["volume"].to_numpy() <= 0
    for ts_val in ts[bad_vol_mask]:
        issues.append(f"non-positive volume at {int(ts_val)}")

    return issues


def save(df: pd.DataFrame, symbol: str, granularity: str = "1H") -> Path:
    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    label = _granularity_label(granularity)
    path = raw_dir / f"{symbol}_{label}.csv"
    # 换行符显式写 LF:pandas 在 Windows 上默认 os.linesep(CRLF),而
    # `.gitattributes` 规定 data/raw/*.csv 的仓库字节是 LF(与 aggregate.csv_bytes
    # 同一口径),避免不同平台 checkout 后 sha256 不一致。
    payload = csv_bytes(df)
    path.write_bytes(payload)
    sha = hashlib.sha256(payload).hexdigest()

    step_ms = _granularity_ms(granularity)
    issues = validate(df, step_ms=step_ms)
    ts = df.sort_values("ts")["ts"]
    record: dict[str, Any] = {
        "path": str(path.relative_to(DATA_DIR.parent)),
        "sha256": sha,
        "rows": int(len(df)),
        "ts_first": int(ts.iloc[0]),
        "ts_last": int(ts.iloc[-1]),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "span_days": round(float(ts.iloc[-1] - ts.iloc[0]) / MS_PER_DAY, 2),
        "granularity": granularity,
        "step_ms": step_ms,
        "validation_issues": issues,
    }

    manifest: dict[str, Any] = {"files": {}}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[f"{symbol}_{label}.csv"] = record
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load(symbol: str, granularity: str = "1H") -> pd.DataFrame:
    label = _granularity_label(granularity)
    df = pd.read_csv(DATA_DIR / "raw" / f"{symbol}_{label}.csv", dtype={"ts": "int64"})
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def check_frame_alignment(df_fine: pd.DataFrame, df_coarse: pd.DataFrame,
                          step_fine_ms: int, step_coarse_ms: int = STEP_MS) -> list[str]:
    """对齐硬约束:细粒度帧必须完整覆盖粗粒度帧。

    - 覆盖:fine 首 ts ≤ coarse 首 ts;fine 末 ts ≥ coarse 末 ts + (ratio−1)·step_fine
      (最后一根 coarse bar 的全部 ratio 根子 bar 都在场);
    - 网格:每根 coarse bar 恰好 ratio 根 fine 子 bar,标签为 coarse_ts + k·step_fine。
    返回问题列表(空 = 对齐成立)。
    """
    issues: list[str] = []
    if step_coarse_ms % step_fine_ms != 0:
        raise ValueError(f"step_coarse {step_coarse_ms} not divisible by step_fine {step_fine_ms}")
    ratio = step_coarse_ms // step_fine_ms
    fc = df_fine.sort_values("ts").reset_index(drop=True)
    co = df_coarse.sort_values("ts").reset_index(drop=True)
    if len(fc) == 0 or len(co) == 0:
        return ["empty frame"]
    ts_f, ts_c = fc["ts"].to_numpy(), co["ts"].to_numpy()
    if ts_f[0] > ts_c[0]:
        issues.append(f"coverage: fine first ts {ts_f[0]} > coarse first ts {ts_c[0]}")
    if ts_f[-1] < ts_c[-1] + (ratio - 1) * step_fine_ms:
        issues.append(f"coverage: fine last ts {ts_f[-1]} < coarse last ts "
                      f"{ts_c[-1]} + {(ratio - 1) * step_fine_ms}")
    fine_in = fc[(fc["ts"] >= ts_c[0]) & (fc["ts"] <= ts_c[-1] + (ratio - 1) * step_fine_ms)]
    if len(fine_in) != ratio * len(co):
        issues.append(f"grid: {len(fine_in)} fine bars in coarse window != "
                      f"{ratio}*{len(co)} expected")
    else:
        expected = (co["ts"].repeat(ratio).to_numpy()
                    + (np.arange(len(co) * ratio) % ratio) * step_fine_ms)
        got = fine_in["ts"].to_numpy()
        if not (expected == got).all():
            bad = int((expected != got).nonzero()[0][0])
            issues.append(f"grid: fine sub-bar label mismatch at offset {bad}: "
                          f"expected {expected[bad]} got {got[bad]}")
    return issues


def _fetch_and_verify_symbol(symbol: str, args: argparse.Namespace) -> None:
    """Fetch, validate, persist, and optionally alignment-check one symbol."""
    df = fetch_klines(
        symbol,
        product_type=args.product_type,
        granularity=args.granularity,
        years=args.years,
        start_ts=args.start_ts,
        end_ts=args.end_ts,
    )
    step_ms = _granularity_ms(args.granularity)
    issues = validate(df, step_ms=step_ms)
    path = save(df, symbol, granularity=args.granularity)
    first, last = df["ts"].iloc[0], df["ts"].iloc[-1]
    span = (last - first) / MS_PER_DAY
    print(f"{symbol}: {len(df)} bars, {span:.1f} days, {len(issues)} validation issues -> {path}")
    for issue in issues:
        print(f"  {issue}")
    if args.align_with is None:
        return
    df_coarse = load(symbol, granularity=args.align_with)
    align = check_frame_alignment(df, df_coarse, step_fine_ms=step_ms)
    if align:
        for item in align:
            print(f"  ALIGNMENT ISSUE: {item}")
        raise DataIntegrityError(f"{symbol} {args.granularity} vs {args.align_with} alignment failed")
    print(f"  alignment vs {args.align_with}: OK (covers {len(df_coarse)} coarse bars)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="market_operator.data")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = sub.add_parser("fetch")
    fetch_cmd.add_argument("--symbols", required=True, help="comma separated, e.g. BTCUSDT,ETHUSDT")
    fetch_cmd.add_argument("--years", type=float, default=2)
    fetch_cmd.add_argument("--product-type", default="USDT-FUTURES")
    fetch_cmd.add_argument("--granularity", default="1H")
    fetch_cmd.add_argument("--start-ts", type=int, default=None,
                           help="显式区间起点(毫秒 bar 标签,含);缺省按 years/now 推导")
    fetch_cmd.add_argument("--end-ts", type=int, default=None,
                           help="显式区间末点(毫秒 bar 标签,含)")
    fetch_cmd.add_argument("--align-with", default=None,
                           help="对齐断言基准 granularity(如 1H);细粒度帧必须覆盖该帧")
    args = parser.parse_args()

    if args.command == "fetch":
        for symbol in (s.strip() for s in args.symbols.split(",") if s.strip()):
            _fetch_and_verify_symbol(symbol, args)


if __name__ == "__main__":
    main()
