"""v19-orderflow aggTrades 日档抓取(docs/v19-protocol-orderflow.md §1,冻结)。

来源 = data.binance.vision 公开日度压缩包(venue = Binance USDⓈ-M 永续,
fapi um;日档逐天下载,月档不用 —— 协议明文 + 所有者凭据已验日档可取):
  /data/futures/um/daily/aggTrades/{SYM}/{SYM}-aggTrades-YYYY-MM-DD.zip
  旁挂 .CHECKSUM(sha256)在可得时核对(zip 层),不可得如实记录。

列契约(落地只存必需列,协议 §1 "解开后仅按需柱效率抽使用"):
  ts_ms           = transact_time(毫秒;防御性归一微秒档案)
  price, qty      = 成交价/量(基币)
  is_buyer_maker  = 买方是否 maker(true ⇒ 主动卖;taker 方向 = not is_buyer_maker)
原始 zip 只算 sha256 后即弃(分层摘要:zip sha + 行数进 manifest,仅落地
4 列 csv.gz)。

用法:
  python scripts/fetch_aggtrades.py [--symbols BTCUSDT,ETHUSDT,SOLUSDT]
      [--days 90] [--end-date YYYY-MM-DD] [--sleep 0.25]

窗口:--end-date 缺省 = UTC 昨天,若该日档未发布(404)自动回退(≤5 天,
实际终点如实打印并写 manifest);窗口 = [end−(days−1), end] 共 days 天;
窗口内部任何一天 404 = 数据缺口,直接报错(不静默跳过)。
幂等:manifest 中已有同 key 且落地文件 sha256 与记录一致 ⇒ 跳过(分阶段
重跑安全)。

落地: data/aggtrades/{SYM}/{YYYY-MM-DD}.csv.gz;validate(长度>0/ts 非降/
price,qty 无负非零/ts 落在当日 UTC 域)+ sha256 追加进 data/manifest.json
(key = aggtrades/{SYM}/{date}.csv.gz,分层含 raw_zip_sha256 与行数)。

离线可测: 纯函数 _csv_bytes_to_frame(bytes)->DataFrame / _zip_to_frame(bytes)
-> DataFrame / validate(df, day_start_ms)->[issue] / parse_checksum(text)->sha
不触网络;网络只在 fetch_* 内。
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

import requests  # noqa: E402

VISION_DAILY_AGG = ("https://data.binance.vision/data/futures/um/daily/"
                    "aggTrades/{sym}/{sym}-aggTrades-{yyyy}-{mm}-{dd}.zip")

RETRIES = 5
RETRY_BACKOFF_S = 2.0
SLEEP_S = 0.25
END_PROBE_BACK_DAYS = 5         # 最近端未发布时允许的回退天数

DATA_DIR = ROOT / "data"
AGG_DIR = DATA_DIR / "aggtrades"
MANIFEST_PATH = DATA_DIR / "manifest.json"

MS_PER_DAY = 86_400_000
COLUMNS = ["ts_ms", "price", "qty", "is_buyer_maker"]
_VISION_COLS = ["agg_trade_id", "price", "quantity", "first_trade_id",
                "last_trade_id", "transact_time", "is_buyer_maker"]

VENUE_DECLARATION = ("Binance USDⓈ-M 永续(data.binance.vision, futures/um "
                     "daily aggTrades);与 Bitget 非同一场座,v19-orderflow "
                     "检验接触点 tape 特征增量,不复刻 Bitget")


# ---------------------------------------------------------------------------
# 纯解析/校验(离线可测)
# ---------------------------------------------------------------------------


def _csv_bytes_to_frame(content: bytes) -> pd.DataFrame:
    """解析单个 vision aggTrades csv 字节(含/不含表头两种形态都接)→ 4 列契约。"""
    raw = pd.read_csv(io.BytesIO(content), header=None, dtype=str)
    if str(raw.iloc[0, 0]).strip().lower() == "agg_trade_id":
        raw = raw.iloc[1:].reset_index(drop=True)
    raw.columns = _VISION_COLS[: len(raw.columns)]
    df = raw[["transact_time", "price", "quantity", "is_buyer_maker"]].copy()
    df["transact_time"] = df["transact_time"].astype("int64")
    df["price"] = df["price"].astype("float64")
    df["quantity"] = df["quantity"].astype("float64")
    df["is_buyer_maker"] = (df["is_buyer_maker"].str.strip().str.lower()
                            .map({"true": True, "false": False}).astype(bool))
    # 防御:远期档案若切换微秒时间戳(参考 fetch_binance_klines 同规归一)。
    if int(df["transact_time"].abs().max()) >= 10**14:
        df["transact_time"] = df["transact_time"] // 1000
    df = df.rename(columns={"transact_time": "ts_ms", "quantity": "qty"})
    return df[COLUMNS]


def _zip_to_frame(content: bytes) -> pd.DataFrame:
    """vision aggTrades zip 字节 → (ts_ms, price, qty, is_buyer_maker)。"""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"vision aggTrades zip csv count != 1: {names}")
        return _csv_bytes_to_frame(zf.read(names[0]))


def validate(df: pd.DataFrame, day_start_ms: int) -> list[str]:
    """日档校验:长度 > 0;ts_ms 非降;price/qty 无负非零;ts 落当日 UTC 域。"""
    issues: list[str] = []
    n = len(df)
    if n == 0:
        return ["empty frame"]
    ts = df["ts_ms"].to_numpy()
    n_decr = int((ts[1:] < ts[:-1]).sum())
    if n_decr:
        issues.append(f"ts_ms decreasing at {n_decr} positions")
    n_bad_price = int((df["price"] <= 0.0).sum())
    n_bad_qty = int((df["qty"] <= 0.0).sum())
    if n_bad_price:
        issues.append(f"price <= 0 rows: {n_bad_price}")
    if n_bad_qty:
        issues.append(f"qty <= 0 rows: {n_bad_qty}")
    lo, hi = day_start_ms, day_start_ms + MS_PER_DAY
    n_out = int(((ts < lo) | (ts >= hi)).sum())
    if n_out:
        issues.append(f"ts_ms outside [{lo}, {hi}): {n_out} rows")
    n_nan = int(df[COLUMNS].isna().any(axis=1).sum())
    if n_nan:
        issues.append(f"NaN rows: {n_nan}")
    return issues


def parse_checksum(text: str) -> str:
    """vision .CHECKSUM 文本("<sha256>  <filename>")→ sha256。"""
    tokens = text.strip().split()
    if not tokens:
        raise RuntimeError("empty CHECKSUM payload")
    token = tokens[0].lower()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise RuntimeError(f"bad CHECKSUM payload: {text[:80]!r}")
    return token


# ---------------------------------------------------------------------------
# 网络(唯一触网区)
# ---------------------------------------------------------------------------


def _get(url: str) -> bytes:
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.content
            last_err = RuntimeError(f"{url} -> HTTP {resp.status_code}")
            if resp.status_code == 404:
                raise last_err          # 404 = 该日档未发布,重试无意义
        except requests.RequestException as e:
            last_err = e
        time.sleep(RETRY_BACKOFF_S * 2**attempt)
    raise RuntimeError(f"fetch failed: {last_err}")


def fetch_day(symbol: str, day: datetime) -> tuple[pd.DataFrame, str, str]:
    """下载一日 zip,核对 .CHECKSUM(可得时),返回 (df, zip_sha256, 核对状态)。"""
    url = VISION_DAILY_AGG.format(sym=symbol, yyyy=day.year,
                                  mm=f"{day.month:02d}", dd=f"{day.day:02d}")
    content = _get(url)
    zip_sha = hashlib.sha256(content).hexdigest()
    checksum_status = "unavailable"
    try:
        expected = parse_checksum(_get(url + ".CHECKSUM").decode("utf-8", "replace"))
        if expected != zip_sha:
            raise RuntimeError(f"{symbol} {day:%Y-%m-%d} CHECKSUM mismatch: "
                               f"{expected} != {zip_sha}")
        checksum_status = "verified"
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    return _zip_to_frame(content), zip_sha, checksum_status


# ---------------------------------------------------------------------------
# 落地与 manifest(追加;data/manifest.json 惯例)
# ---------------------------------------------------------------------------


def landed_path(symbol: str, day: datetime) -> Path:
    return AGG_DIR / symbol / f"{day:%Y-%m-%d}.csv.gz"


def save(df: pd.DataFrame, symbol: str, day: datetime, zip_sha: str,
         checksum_status: str) -> tuple[Path, list[str]]:
    path = landed_path(symbol, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, columns=COLUMNS,
              compression={"method": "gzip", "compresslevel": 6})
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    day_start_ms = int(datetime(day.year, day.month, day.day,
                                tzinfo=timezone.utc).timestamp() * 1000)
    issues = validate(df, day_start_ms)
    record = {
        "path": str(path.relative_to(DATA_DIR.parent)),
        "sha256": sha,
        "rows": int(len(df)),
        "ts_first": int(df["ts_ms"].iloc[0]),
        "ts_last": int(df["ts_ms"].iloc[-1]),
        "raw_zip_sha256": zip_sha,
        "zip_checksum": checksum_status,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "binance-vision futures/um daily aggTrades",
        "venue_declaration": VENUE_DECLARATION,
        "granularity": "trade",
        "columns": COLUMNS,
        "validation_issues": issues,
    }
    manifest: dict = {"files": {}}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[f"aggtrades/{symbol}/{day:%Y-%m-%d}.csv.gz"] = record
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return path, issues


def already_landed(symbol: str, day: datetime) -> bool:
    """幂等:文件在且 manifest sha 与文件一致 ⇒ 跳过(分阶段重跑安全)。"""
    path = landed_path(symbol, day)
    if not path.exists() or not MANIFEST_PATH.exists():
        return False
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rec = manifest.get("files", {}).get(
        f"aggtrades/{symbol}/{day:%Y-%m-%d}.csv.gz")
    if not rec:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == rec.get("sha256")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog="fetch_aggtrades")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--end-date", default=None,
                        help="窗口最后一天 YYYY-MM-DD(UTC);缺省 = UTC 昨天,"
                             "未发布自动回退(≤%d 天)" % END_PROBE_BACK_DAYS)
    parser.add_argument("--sleep", type=float, default=SLEEP_S)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.end_date:
        end = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end = (now - timedelta(days=1)).replace(hour=0, minute=0,
                                               second=0, microsecond=0)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 探测最近已发布日(用第一个品种探;日档发布时间全场一致)
    probe = end
    for _ in range(END_PROBE_BACK_DAYS):
        try:
            content_probe = _get(VISION_DAILY_AGG.format(
                sym=symbols[0], yyyy=probe.year, mm=f"{probe.month:02d}",
                dd=f"{probe.day:02d}") + ".CHECKSUM")
            parse_checksum(content_probe.decode("utf-8", "replace"))
            break
        except RuntimeError as e:
            if "404" not in str(e):
                raise
            probe -= timedelta(days=1)
    else:
        raise RuntimeError(f"no published aggTrades day in "
                           f"[{end - timedelta(days=END_PROBE_BACK_DAYS):%Y-%m-%d}, "
                           f"{end:%Y-%m-%d}]")
    if probe != end:
        print(f"end-date {end:%Y-%m-%d} not published; rolled back to "
              f"{probe:%Y-%m-%d}", flush=True)
    end = probe

    days = [end - timedelta(days=i) for i in range(args.days - 1, -1, -1)]
    print(f"window: {days[0]:%Y-%m-%d} .. {days[-1]:%Y-%m-%d} "
          f"({len(days)} days) x {len(symbols)} symbols", flush=True)

    for symbol in symbols:
        t0 = time.time()
        total_rows = 0
        n_skip = 0
        for day in days:
            if already_landed(symbol, day):
                n_skip += 1
                continue
            df, zip_sha, checksum_status = fetch_day(symbol, day)
            path, issues = save(df, symbol, day, zip_sha, checksum_status)
            total_rows += len(df)
            flag = f" checksum={checksum_status}" if checksum_status != "verified" else ""
            print(f"[{symbol}] {day:%Y-%m-%d}: {len(df):>8,} rows -> "
                  f"{path.name}{flag}; issues={issues or '[]'}", flush=True)
            if issues:
                raise RuntimeError(f"{symbol} {day:%Y-%m-%d} validation failed: "
                                   f"{issues}")
            time.sleep(args.sleep)
        print(f"[{symbol}] done: {args.days - n_skip} fetched ({total_rows:,} rows), "
              f"{n_skip} skipped, {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
