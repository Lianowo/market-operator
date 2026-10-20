"""v12 数据抓取(docs/v12-protocol.md §1):Binance U 永续(fapi um)1h K线。

来源 = data.binance.vision 公开压缩包(venue = Binance USDⓈ-M 永续,与本地
Bitget 非同一场座,v12-protocol §1 已声明):
  月度 zip: /data/futures/um/monthly/klines/{SYM}/1h/{SYM}-1h-YYYY-MM.zip
  日度 zip: /data/futures/um/daily/klines/{SYM}/1h/{SYM}-1h-YYYY-MM-DD.zip
  (仅用于补当前未满月的尾段;窗差在 manifest 记录中如实声明)
列契约: ts, open, high, low, close, volume, taker_buy_base_volume
(ts = open_time 毫秒;主动买量字段即 vision csv 的 taker_buy_volume,基币单位)。

用法:
  python scripts/fetch_binance_klines.py [--symbols BTCUSDT,ETHUSDT,SOLUSDT]
      [--start-ts MS --end-ts MS] [--align-bitget]

--align-bitget:起点/终点取自 data/raw/{SYM}_1h.csv 的 ts_first/ts_last(Bitget
同窗);终点钳到币安已发布的最近完整 1h bar,实际窗差写入 manifest
window_delta 与 reports/flow_study.*。

备选路径(vision 抓取失败时):--api-fallback 走 u data API /fapi/v1/klines
分页(1500/页,含 takerBuyBaseAssetVolume 字段,同列契约)。

落地: data/raw_binance/{SYM}_1h.csv;validate(连续性/合法性/主动买量域)+
sha256 进 data/manifest.json(key = binance_vision/{SYM}_1h.csv,source 标注)。

离线可测: 纯函数 _zip_to_frame(bytes)->DataFrame / validate(df)->[issue]
不触网络;网络只在 fetch_* 函数内。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data  # noqa: E402

import requests  # noqa: E402

VISION_MONTHLY = ("https://data.binance.vision/data/futures/um/monthly/klines/"
                  "{sym}/1h/{sym}-1h-{yyyy}-{mm}.zip")
VISION_DAILY = ("https://data.binance.vision/data/futures/um/daily/klines/"
                "{sym}/1h/{sym}-1h-{yyyy}-{mm}-{dd}.zip")
FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
API_PAGE_LIMIT = 1500

RETRIES = 5
RETRY_BACKOFF_S = 2.0
SLEEP_S = 0.25

DATA_DIR = ROOT / "data"
RAW_BINANCE_DIR = DATA_DIR / "raw_binance"
MANIFEST_PATH = DATA_DIR / "manifest.json"

STEP_MS = mo_data.STEP_MS
COLUMNS = ["ts", "open", "high", "low", "close", "volume", "taker_buy_base_volume"]
_VISION_COLS = ["open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "count",
                "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


# ---------------------------------------------------------------------------
# 纯解析(离线可测)
# ---------------------------------------------------------------------------


def _csv_bytes_to_frame(content: bytes) -> pd.DataFrame:
    """解析单个 vision kline csv 字节(含/不含表头两种形态都接)。"""
    raw = pd.read_csv(io.BytesIO(content), header=None, dtype=str)
    if str(raw.iloc[0, 0]).strip().lower() == "open_time":
        raw = raw.iloc[1:].reset_index(drop=True)
    raw.columns = _VISION_COLS[: len(raw.columns)]
    df = raw[["open_time", "open", "high", "low", "close", "volume",
              "taker_buy_volume"]].astype(
        {"open_time": "int64", "open": "float64", "high": "float64",
         "low": "float64", "close": "float64", "volume": "float64",
         "taker_buy_volume": "float64"})
    # 早期 spot 档案出现过微秒 open_time;fapi um 应为毫秒,防御性归一。
    if int(df["open_time"].abs().max()) >= 10**14:
        df["open_time"] = df["open_time"] // 1000
    return df


def _zip_to_frame(content: bytes) -> pd.DataFrame:
    """vision zip 字节 → (ts, open..volume, taker_buy_base_volume) DataFrame。"""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"vision zip csv count != 1: {names}")
        df = _csv_bytes_to_frame(zf.read(names[0]))
    df = df.rename(columns={"open_time": "ts",
                            "taker_buy_volume": "taker_buy_base_volume"})
    return df[COLUMNS]


def validate(df: pd.DataFrame, step_ms: int = STEP_MS) -> list[str]:
    """连续性/合法性 = mo_data.validate 同规;再加主动买量域检查。"""
    issues = mo_data.validate(df, step_ms=step_ms)
    tb = df["taker_buy_base_volume"]
    bad = df[(tb < 0.0) | (tb > df["volume"])]
    for _, row in bad.iterrows():
        issues.append(f"taker_buy out of [0, volume] at {int(row['ts'])}")
    nan_tb = df[tb.isna()]
    for _, row in nan_tb.iterrows():
        issues.append(f"taker_buy NaN at {int(row['ts'])}")
    return issues


# ---------------------------------------------------------------------------
# 网络(唯一触网区)
# ---------------------------------------------------------------------------


def _get(url: str) -> bytes:
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.content
            last_err = RuntimeError(f"{url} -> HTTP {resp.status_code}")
            if resp.status_code == 404:
                raise last_err          # 404 = 该文件根本未发布,重试无意义
        except requests.RequestException as e:
            last_err = e
        time.sleep(RETRY_BACKOFF_S * 2**attempt)
    raise RuntimeError(f"fetch failed: {last_err}")


def _month_range(start_dt: datetime, end_dt: datetime) -> list[tuple[int, int]]:
    cur = datetime(start_dt.year, start_dt.month, 1, tzinfo=timezone.utc)
    out = []
    while cur <= end_dt:
        out.append((cur.year, cur.month))
        cur = (cur.replace(day=28) + timedelta(days=7)).replace(day=1)
    return out


def fetch_vision(symbol: str, start_ts: int, end_ts: int) -> tuple[pd.DataFrame, list[str]]:
    """月度 zip 为主;未满月尾段(当月)用日度 zip 补齐到已发布的最近天。

    返回 (df, parts)。parts 记录实际使用的文件名清单(进 manifest)。
    end_ts 之后未有数据的天(如日度包未发布)如实截断 —— 窗差由调用方声明。
    """
    start_dt = datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    frames, parts = [], []

    months = _month_range(start_dt, end_dt)
    for (yy, mm) in months:
        first_of_month = datetime(yy, mm, 1, tzinfo=timezone.utc)
        if first_of_month.month == now.month and first_of_month.year == now.year:
            break                            # 当月:转日度 zip
        try:
            content = _get(VISION_MONTHLY.format(sym=symbol, yyyy=yy, mm=f"{mm:02d}"))
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            break                            # 未发布的月(时差)也转日度
        frames.append(_zip_to_frame(content))
        parts.append(f"{symbol}-1h-{yy}-{mm:02d}.zip")
        time.sleep(SLEEP_S)

    last_covered = (max((int(f["ts"].max()) for f in frames))
                    if frames else start_ts - STEP_MS)
    day = datetime.fromtimestamp(
        max(start_ts, last_covered + STEP_MS) / 1000, tz=timezone.utc)
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end_dt and day.date() < now.date():
        url = VISION_DAILY.format(sym=symbol, yyyy=day.year,
                                  mm=f"{day.month:02d}", dd=f"{day.day:02d}")
        try:
            content = _get(url)
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            break                            # 最近的日度包尚未发布:截断
        frames.append(_zip_to_frame(content))
        parts.append(f"{symbol}-1h-{day:%Y-%m-%d}.zip")
        time.sleep(SLEEP_S)
        day += timedelta(days=1)

    if not frames:
        raise RuntimeError(f"{symbol}: no vision data in [{start_ts}, {end_ts}]")
    df = (pd.concat(frames, ignore_index=True)
          .drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True))
    df = df[(df["ts"] >= start_ts) & (df["ts"] <= end_ts)].reset_index(drop=True)
    return df, parts


def fetch_api_fallback(symbol: str, start_ts: int, end_ts: int) -> tuple[pd.DataFrame, list[str]]:
    """备选:u data API /fapi/v1/klines 分页(含 takerBuyBaseAssetVolume)。"""
    frames: list[pd.DataFrame] = []
    cur = start_ts
    while cur <= end_ts:
        params = {"symbol": symbol, "interval": "1h", "startTime": cur,
                  "endTime": end_ts, "limit": API_PAGE_LIMIT}
        last_err: Exception | None = None
        for attempt in range(RETRIES):
            try:
                resp = requests.get(FAPI_KLINES, params=params, timeout=20)
                if resp.status_code == 200:
                    last_err = None
                    break
                last_err = RuntimeError(f"fapi HTTP {resp.status_code}: {resp.text[:200]}")
            except requests.RequestException as e:
                last_err = e
            time.sleep(RETRY_BACKOFF_S * 2**attempt)
        if last_err is not None:
            raise RuntimeError(f"fapi fetch failed for {symbol}@{cur}: {last_err}")
        raw = resp.json()
        if not raw:
            break
        # API 末行可能是未收盘 bar:裁到 < 当前已收盘
        now_ms = int(time.time() * 1000)
        keep = [r for r in raw if int(r[6]) < now_ms and int(r[0]) <= end_ts]
        if not keep:
            break
        f = pd.DataFrame(
            {"ts": [int(r[0]) for r in keep],
             "open": [float(r[1]) for r in keep], "high": [float(r[2]) for r in keep],
             "low": [float(r[3]) for r in keep], "close": [float(r[4]) for r in keep],
             "volume": [float(r[5]) for r in keep],
             "taker_buy_base_volume": [float(r[9]) for r in keep]})
        frames.append(f)
        nxt = int(f["ts"].max()) + STEP_MS
        if nxt <= cur:                       # 防御分页死循环
            break
        cur = nxt
        if len(raw) < API_PAGE_LIMIT:
            break
        time.sleep(SLEEP_S)
    if not frames:
        raise RuntimeError(f"{symbol}: no fapi data in [{start_ts}, {end_ts}]")
    df = (pd.concat(frames, ignore_index=True)
          .drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True))
    return df, ["fapi/v1/klines paged (api-fallback)"]


# ---------------------------------------------------------------------------
# 落地与 manifest
# ---------------------------------------------------------------------------


def save(df: pd.DataFrame, symbol: str, parts: list[str],
         source: str, window_ref: dict | None = None) -> Path:
    RAW_BINANCE_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_BINANCE_DIR / f"{symbol}_1h.csv"
    df.to_csv(path, index=False, columns=COLUMNS)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    issues = validate(df, step_ms=STEP_MS)
    ts = df.sort_values("ts")["ts"]
    record: dict = {
        "path": str(path.relative_to(DATA_DIR.parent)),
        "sha256": sha,
        "rows": int(len(df)),
        "ts_first": int(ts.iloc[0]),
        "ts_last": int(ts.iloc[-1]),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "span_days": round(float(ts.iloc[-1] - ts.iloc[0]) / 86_400_000, 2),
        "source": source,
        "venue_declaration": "Binance USDⓈ-M 永续(data.binance.vision,fapi um);"
                             "与 Bitget USDT 永续 microstructure 不同构,"
                             "v12 检验'bar 层流特征值不值钱',不复刻 Bitget",
        "granularity": "1h",
        "step_ms": STEP_MS,
        "parts": parts,
        "validation_issues": issues,
    }
    if window_ref is not None:
        record["window_ref"] = window_ref

    manifest: dict = {"files": {}}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[f"binance_vision/{symbol}_1h.csv"] = record
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(prog="fetch_binance_klines")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--start-ts", type=int, default=None,
                        help="窗口起点(open_time 毫秒,含);缺省 = Bitget 同窗 ts_first")
    parser.add_argument("--end-ts", type=int, default=None,
                        help="窗口终点(open_time 毫秒,含);缺省 = Bitget 同窗 ts_last")
    parser.add_argument("--align-bitget", action="store_true",
                        help="显式声明以 data/raw/{SYM}_1h.csv 首末 ts 为同窗基准(缺省同)")
    parser.add_argument("--api-fallback", action="store_true",
                        help="跳过 vision,直接走 /fapi/v1/klines 分页备选")
    args = parser.parse_args()

    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        bg = mo_data.load(symbol)            # 同窗基准 = 本地 Bitget 1h 帧
        start_ts = args.start_ts if args.start_ts is not None else int(bg["ts"].iloc[0])
        end_ts = args.end_ts if args.end_ts is not None else int(bg["ts"].iloc[-1])

        if args.api_fallback:
            df, parts = fetch_api_fallback(symbol, start_ts, end_ts)
            source = "binance-fapi-api"
        else:
            df, parts = fetch_vision(symbol, start_ts, end_ts)
            source = "binance-vision"
        issues = validate(df, step_ms=STEP_MS)
        window_ref = {
            "ref": "data/raw/{}_1h.csv (Bitget, manifest ts_first/ts_last)".format(symbol),
            "ref_ts_first": int(bg["ts"].iloc[0]),
            "ref_ts_last": int(bg["ts"].iloc[-1]),
            "delta_bars_head": int((df["ts"].iloc[0] - int(bg["ts"].iloc[0])) // STEP_MS),
            "delta_bars_tail": int((int(bg["ts"].iloc[-1]) - df["ts"].iloc[-1]) // STEP_MS),
        }
        path = save(df, symbol, parts, source, window_ref=window_ref)
        d0 = datetime.fromtimestamp(int(df["ts"].iloc[0]) / 1000, tz=timezone.utc)
        d1 = datetime.fromtimestamp(int(df["ts"].iloc[-1]) / 1000, tz=timezone.utc)
        print(f"{symbol}: {len(df)} bars [{d0:%Y-%m-%dT%H:%MZ} .. {d1:%Y-%m-%dT%H:%MZ}], "
              f"source={source}, parts={len(parts)}, {len(issues)} issues -> {path}")
        for issue in issues:
            print(f"  {issue}")
        print(f"  窗差(Bitget 同窗基准): head={window_ref['delta_bars_head']}, "
              f"tail={window_ref['delta_bars_tail']} bars")


if __name__ == "__main__":
    main()
