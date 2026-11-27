"""资金费率抓取:交易所结算表 → data/raw/funding/{SYM}.csv。

为什么需要:引擎里 `funding_per_8h` 是**常数**(默认 1bp/8h,方向无关、恒为成本)。
实测该假设与交易所真实结算差 5.4 倍且符号经常相反(见
reports/live_cost_audit_20261023/FUNDING_REPLAY.md)。要让回放口径可审计,
必须把真实结算表落盘,而不是每次跑都打网络。

两个来源(同一落盘口径,同一回放读取路径):
- ``binance``(缺省):Binance USDT-M ``fapi/v1/fundingRate``,原生加密;
- ``bitget``:Bitget USDT 永续 ``/api/v2/mix/market/history-fund-rate``,
  覆盖 RWA(美股/商品/指数/外汇)合约 —— 这些标的的结算间隔是 **4h 或 8h**
  混合(见 contracts 的 ``fundInterval``),常数先验必然失真,必须用真实表。

口径(与 broker `_accrue_funding` 的符号约定一致):
- 结算点 `fundingTime`(毫秒 UTC),费率 `fundingRate`(有符号小数);
- 持仓在 (last_ts, ts] 内跨过的结算点逐个计提:成本 = notional · rate · direction
  (多 +1 / 空 −1),即 rate>0 时多头付费、空头收费;
- 落盘列 `funding_time,funding_rate`,升序去重。

用法:
  python scripts/fetch_funding.py [--source binance|bitget]
      --symbols BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT,NVDAUSDT,XAUUSDT
      [--start-ts MS --end-ts MS] [--days N] [--out data/raw/funding]

窗口优先级:显式 --start-ts/--end-ts > 本地 data/raw/{SYM}_1h.csv 首末 ts(±2 天)
> --days 指定的最近 N 天。离线可测:纯函数 `normalize(rows)` 不触网络。
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import heartbeat as hb                                # noqa: E402
from market_operator import paths                                          # noqa: E402
from market_operator.bitget_live import BitgetLiveClient                   # noqa: E402
from market_operator.timeframes import MS_PER_DAY, MS_PER_HOUR             # noqa: E402

DATA_DIR = paths.data_dir()
RAW_DIR = DATA_DIR / "raw"
MANIFEST_PATH = DATA_DIR / "manifest.json"
FAPI = "https://fapi.binance.com/fapi/v1/fundingRate"
BITGET_FUNDING = "https://api.bitget.com/api/v2/mix/market/history-fund-rate"
PRODUCT_TYPE = "USDT-FUTURES"
LIVE_CONFIG = ROOT / "config" / "live.json"

PAGE_LIMIT = 1000
BITGET_PAGE_LIMIT = 100          # 交易所上限:pageSize 最大 100(实测)
BITGET_MAX_PAGES = 400
SOURCES = ("binance", "bitget")
SLEEP_S = 0.15
RETRIES = 4
RETRY_BACKOFF_S = 2.0
HTTP_TIMEOUT_S = 20.0
DEFAULT_RWA_MIN_VOL = 3_000_000.0
FRAME_PAD_DAYS = 2
MS_DAY = MS_PER_DAY


def _get_json_with_retry(
    url: str,
    params: Mapping[str, Any],
    *,
    retries: int = RETRIES,
    timeout: float = HTTP_TIMEOUT_S,
) -> Any:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=dict(params), timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
    raise RuntimeError("unreachable retry loop")


def frame_span(symbol: str, tf: str = "1h") -> tuple[int, int]:
    """本地行情首/末 ts(毫秒);缺失时抛错,避免抓到与回放无关的窗口。"""
    path = RAW_DIR / f"{symbol}_{tf}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    first: int | None = None
    last: int | None = None
    with path.open(encoding="utf-8") as fh:
        next(fh, None)
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


def normalize(rows: Sequence[Mapping[str, Any]]) -> list[tuple[int, float]]:
    """交易所原始行 → 升序去重的 (fundingTime, fundingRate)。"""
    seen: dict[int, float] = {}
    for row in rows:
        ts = int(row["fundingTime"])
        seen[ts] = float(row["fundingRate"])
    return sorted(seen.items())


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur = start_ms
    while cur < end_ms:
        rows = _get_json_with_retry(
            FAPI,
            {
                "symbol": symbol,
                "startTime": cur,
                "endTime": end_ms,
                "limit": PAGE_LIMIT,
            },
        )
        if not rows:
            break
        out.extend(rows)
        nxt = int(rows[-1]["fundingTime"]) + 1
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(SLEEP_S)
    return out


def fetch_funding_bitget(
    symbol: str,
    start_ms: int,
    end_ms: int,
    product_type: str = PRODUCT_TYPE,
) -> list[dict[str, Any]]:
    """Bitget 公开资金费历史(pageNo 翻页;pageSize 上限 100,实测)。

    与 Binance 分支的区别:交易所不给 startTime 过滤,只能按页往回翻到
    窗口左界;返回前按窗口裁剪,口径与 `fetch_funding` 完全一致。
    """
    out: list[dict[str, Any]] = []
    page = 1
    lo, hi = min(start_ms, end_ms), max(start_ms, end_ms)
    while page <= BITGET_MAX_PAGES:
        body = _get_json_with_retry(
            BITGET_FUNDING,
            {
                "symbol": symbol,
                "productType": product_type,
                "pageSize": BITGET_PAGE_LIMIT,
                "pageNo": page,
            },
        )
        if str(body.get("code") or "") not in ("", "00000"):
            raise RuntimeError(f"bitget {body.get('code')} {body.get('msg')}")
        rows = body.get("data") or []
        if not rows:
            break
        out.extend(rows)
        oldest = min(int(x["fundingTime"]) for x in rows)
        if oldest <= lo:
            break
        page += 1
        time.sleep(SLEEP_S)
    return [x for x in out if lo <= int(x["fundingTime"]) <= hi]


def rwa_universe(min_volume: float | None = None) -> list[str]:
    """按 live.json 的 RWA 成交额门槛,返回当前在宇宙里的 RWA 合约(公开接口)。"""
    if min_volume is None:
        try:
            uni = json.loads(LIVE_CONFIG.read_text(encoding="utf-8")).get("universe") or {}
            min_volume = float((uni.get("rwa") or {}).get("min_usdt_volume_24h") or 0.0)
        except (OSError, ValueError):
            min_volume = DEFAULT_RWA_MIN_VOL
    client = BitgetLiveClient(None, product_type=PRODUCT_TYPE)
    rwa = set(client.rwa_symbols())
    tickers = client.tickers()
    threshold = float(min_volume)
    return sorted(
        str(t["symbol"])
        for t in tickers
        if t.get("symbol") in rwa and float(t.get("usdtVolume") or 0.0) > threshold
    )


def existing_series(path: Path) -> list[tuple[int, float]]:
    if not path.exists():
        return []
    rows: list[tuple[int, float]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if not line.strip():
            continue
        ts, rate = line.split(",", 1)
        rows.append((int(ts), float(rate)))
    return rows


def validate(series: Sequence[tuple[int, float]]) -> list[str]:
    """连续性检查:升序、无重复、间隔为整小时(交易所偶发 4h/8h/1h 结算)。"""
    issues: list[str] = []
    for i in range(1, len(series)):
        gap = series[i][0] - series[i - 1][0]
        if gap <= 0:
            issues.append(f"non-monotonic at {series[i][0]}")
        elif gap % MS_PER_HOUR:
            issues.append(f"sub-hour gap {gap}ms at {series[i][0]}")
    return issues


def write_symbol(
    symbol: str,
    series: Sequence[tuple[int, float]],
    out_dir: Path,
    *,
    source: str = "binance",
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}.csv"
    body = ["funding_time,funding_rate", *(f"{ts},{rate:.10g}" for ts, rate in series)]
    hb.write_atomic(path, "\n".join(body) + "\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:                                   # out 目录在仓库之外
        rel = path.as_posix()
    src = (
        "binance_fapi/fapi/v1/fundingRate"
        if source == "binance"
        else "bitget_mix/market/history-fund-rate"
    )
    note = (
        "settlements every 8h (occasionally 4h/1h); signed rate"
        if source == "binance"
        else "settlements every 4h or 8h (exchange fundInterval); signed rate"
    )
    return {
        "path": rel,
        "sha256": digest,
        "rows": len(series),
        "ts_first": series[0][0] if series else None,
        "ts_last": series[-1][0] if series else None,
        "source": src,
        "note": note,
    }


def update_manifest(
    records: Mapping[str, Mapping[str, Any]],
    *,
    source: str = "binance",
) -> None:
    manifest: dict[str, Any] = {"files": {}}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    files = manifest.setdefault("files", {})
    prefix = "binance_fapi_funding" if source == "binance" else "bitget_mix_funding"
    for symbol, rec in records.items():
        files[f"{prefix}/{symbol}.csv"] = dict(rec)
    hb.write_atomic(
        MANIFEST_PATH,
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
    )


def _window(symbol: str, args: argparse.Namespace) -> tuple[int, int]:
    """窗口优先级:显式 start/end > 本地 1h 帧(±2 天)> --days 最近 N 天。"""
    have_start = args.start_ts is not None
    have_end = args.end_ts is not None
    if have_start and have_end:
        return int(args.start_ts), int(args.end_ts)
    try:
        lo, hi = frame_span(symbol)
    except FileNotFoundError:
        if args.days is None:
            raise FileNotFoundError(
                f"{RAW_DIR / (symbol + '_1h.csv')} 不存在:请给 --start-ts/--end-ts "
                "或 --days 指定窗口(拉 RWA 标的时通常没有本地 1h 帧)"
            ) from None
        hi = int(time.time() * 1000)
        lo = hi - int(args.days) * MS_DAY
    start = int(args.start_ts) if have_start else lo - FRAME_PAD_DAYS * MS_DAY
    end = int(args.end_ts) if have_end else hi + FRAME_PAD_DAYS * MS_DAY
    return start, end


def _sync_symbol(
    symbol: str,
    args: argparse.Namespace,
    out_dir: Path,
) -> dict[str, Any] | None:
    prev = existing_series(out_dir / f"{symbol}.csv") if args.incremental else []
    start, end = _window(symbol, args)
    if prev:
        start = max(start, prev[-1][0] + 1)
        if start >= end:
            return None
    rows = (
        fetch_funding_bitget(symbol, start, end, args.product_type)
        if args.source == "bitget"
        else fetch_funding(symbol, start, end)
    )
    series = normalize(rows)
    if prev:
        merged = dict(prev)
        merged.update(dict(series))
        series = sorted(merged.items())
    if not series:
        print(f"[{symbol}] 无结算记录(window {start}..{end})")
        return None
    issues = validate(series)
    rec = write_symbol(symbol, series, out_dir, source=args.source)
    rec["window"] = {"start": start, "end": end}
    rec["issues"] = issues
    pos = sum(1 for _, r in series if r > 0) / max(len(series), 1)
    gaps = sorted({
        (series[i][0] - series[i - 1][0]) // MS_PER_HOUR
        for i in range(1, len(series))
    })
    print(
        f"[{symbol}] rows={len(series)} "
        f"{time.strftime('%Y-%m-%d', time.gmtime(series[0][0] / 1000))}.."
        f"{time.strftime('%Y-%m-%d', time.gmtime(series[-1][0] / 1000))} "
        f"interval_h={gaps} pos_share={pos:.1%} issues={len(issues)}"
    )
    for msg in issues[:3]:
        print(f"  ! {msg}")
    return rec


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT")
    ap.add_argument("--source", choices=SOURCES, default="binance")
    ap.add_argument("--product-type", default=PRODUCT_TYPE)
    ap.add_argument(
        "--all-rwa",
        action="store_true",
        help="从公开接口取当前宇宙内的 RWA 合约(按 live.json 成交额门槛)",
    )
    ap.add_argument(
        "--min-volume",
        type=float,
        default=None,
        help="配合 --all-rwa 的 24h USDT 名义量门槛;缺省读 config/live.json",
    )
    ap.add_argument(
        "--incremental",
        action="store_true",
        help="从已有 CSV 最后一个结算点继续,只补新数据",
    )
    ap.add_argument("--start-ts", type=int, default=None)
    ap.add_argument("--end-ts", type=int, default=None)
    ap.add_argument(
        "--days",
        type=int,
        default=None,
        help="本地无 1h 帧时的回看天数(如 RWA 标的)",
    )
    ap.add_argument("--out", default=str(RAW_DIR / "funding"))
    ap.add_argument("--no-manifest", action="store_true")
    args = ap.parse_args(argv)

    out_dir = Path(args.out).resolve()
    records: dict[str, dict[str, Any]] = {}
    syms = (
        rwa_universe(args.min_volume)
        if args.all_rwa
        else [s.strip() for s in args.symbols.split(",") if s.strip()]
    )
    print(f"[symbols] n={len(syms)} source={args.source} incremental={args.incremental}")
    for symbol in syms:
        rec = _sync_symbol(symbol, args, out_dir)
        if rec is not None:
            records[symbol] = rec

    if not args.no_manifest:
        update_manifest(records, source=args.source)
        print(f"[manifest] {MANIFEST_PATH}")
    print(f"[out] {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
