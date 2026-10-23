"""Data layer: fetch/validate/hash Bitget USDT perpetual OHLCV(v4 起支持任意 granularity)。

Methodology §1: Bitget mix/market/history-candles, BTCUSDT/ETHUSDT/SOLUSDT,
gap + duplicate + OHLC legality checks, SHA-256 archived in data/manifest.json.

v4(docs/v4-protocol.md §1,执行路径细化):granularity 任意化(15m 执行帧);
文件名 = {symbol}_{label}.csv(label 由 granularity 推导:1H→1h,15m→15m);
validate 按该 granularity 的 step;manifest 按文件名键追加(sha256)。
细粒度帧相对 1h 帧的对齐硬约束见 check_frame_alignment(15m 首末 ts 必须
覆盖 1h 首末 —— 最后一根 1h bar 的全部子 bar 都在场;每根 1h bar 恰好
ratio 根子 bar)。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
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


def _granularity_label(granularity: str) -> str:
    """文件名标签(与 v1 既有 {symbol}_1h.csv 命名恒等;v4:1H→1h,15m→15m。"""
    units = {"m": "m", "H": "h", "D": "d", "W": "w"}
    return f"{int(granularity[:-1])}{units[granularity[-1]]}"


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
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> pd.DataFrame:
    """Page backwards through Bitget history-candles from the last closed bar.

    start_ts/end_ts(v4 显式区间,毫秒 bar 标签,含端点):给定则替代
    years/now 推导 —— 窗口与既有 1h 帧显式一致,重跑不受 fetch 时刻漂移。
    """
    step = _granularity_ms(granularity)
    now_ms = int(time.time() * 1000)
    last_closed = now_ms - (now_ms % step) - step if end_ts is None else int(end_ts)
    start_bound = now_ms - int(years * 365 * 86_400_000) if start_ts is None else int(start_ts)

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
        df = df[df["ts"] >= start_bound]          # 显式区间才裁下界(1h 历史行为不变)
    df = df.reset_index(drop=True)
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


def save(df: pd.DataFrame, symbol: str, granularity: str = "1H") -> Path:
    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    label = _granularity_label(granularity)
    path = raw_dir / f"{symbol}_{label}.csv"
    # 换行符显式写 LF:pandas 在 Windows 上默认 os.linesep(CRLF),而
    # `.gitattributes` 规定 data/raw/*.csv 的仓库字节是 LF(aggregate._csv_bytes
    # 同一口径)。若按平台写 CRLF,manifest 里记的 sha256 就只对那台机器成立——
    # 换一台机器 checkout 出 LF 后哈希立刻对不上(重构期间确实踩到过)。
    buf = StringIO()
    df.to_csv(buf, index=False, columns=COLUMNS, lineterminator="\n")
    path.write_bytes(buf.getvalue().encode("utf-8"))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    issues = validate(df, step_ms=_granularity_ms(granularity))
    ts = df.sort_values("ts")["ts"]
    record = {
        "path": str(path.relative_to(DATA_DIR.parent)),
        "sha256": sha,
        "rows": int(len(df)),
        "ts_first": int(ts.iloc[0]),
        "ts_last": int(ts.iloc[-1]),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "span_days": round(float(ts.iloc[-1] - ts.iloc[0]) / 86_400_000, 2),
        "granularity": granularity,
        "step_ms": _granularity_ms(granularity),
        "validation_issues": issues,
    }

    manifest: dict = {"files": {}}
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
    """v4 对齐硬约束(docs/v4-protocol.md §1):细粒度帧必须完整覆盖粗粒度帧。

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="market_operator.data")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = sub.add_parser("fetch")
    fetch_cmd.add_argument("--symbols", required=True, help="comma separated, e.g. BTCUSDT,ETHUSDT")
    fetch_cmd.add_argument("--years", type=float, default=2)
    fetch_cmd.add_argument("--product-type", default="USDT-FUTURES")
    fetch_cmd.add_argument("--granularity", default="1H")
    fetch_cmd.add_argument("--start-ts", type=int, default=None,
                           help="v4 显式区间起点(毫秒 bar 标签,含);缺省按 years/now 推导")
    fetch_cmd.add_argument("--end-ts", type=int, default=None,
                           help="v4 显式区间末点(毫秒 bar 标签,含)")
    fetch_cmd.add_argument("--align-with", default=None,
                           help="对齐断言基准 granularity(如 1H);细粒度帧必须覆盖该帧")
    args = parser.parse_args()

    if args.command == "fetch":
        for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            df = fetch_klines(symbol, product_type=args.product_type, granularity=args.granularity,
                              years=args.years, start_ts=args.start_ts, end_ts=args.end_ts)
            issues = validate(df, step_ms=_granularity_ms(args.granularity))
            path = save(df, symbol, granularity=args.granularity)
            first, last = df["ts"].iloc[0], df["ts"].iloc[-1]
            span = (last - first) / 86_400_000
            print(f"{symbol}: {len(df)} bars, {span:.1f} days, {len(issues)} validation issues -> {path}")
            for issue in issues:
                print(f"  {issue}")
            if args.align_with is not None:
                df_coarse = load(symbol, granularity=args.align_with)
                align = check_frame_alignment(df, df_coarse,
                                              step_fine_ms=_granularity_ms(args.granularity))
                if align:
                    for a in align:
                        print(f"  ALIGNMENT ISSUE: {a}")
                    raise RuntimeError(f"{symbol} {args.granularity} vs {args.align_with} alignment failed")
                print(f"  alignment vs {args.align_with}: OK (covers {len(df_coarse)} coarse bars)")


if __name__ == "__main__":
    main()
