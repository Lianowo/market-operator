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
BITGET_FUNDING = "https://api.bitget.com/api/v2/mix/market/history-fund-rate"
PRODUCT_TYPE = "USDT-FUTURES"

PAGE_LIMIT = 1000
BITGET_PAGE_LIMIT = 100          # 交易所上限:pageSize 最大 100(实测)
BITGET_MAX_PAGES = 400
SOURCES = ("binance", "bitget")
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


def fetch_funding_bitget(symbol: str, start_ms: int, end_ms: int,
                         product_type: str = PRODUCT_TYPE) -> list[dict]:
    """Bitget 公开资金费历史(pageNo 翻页;pageSize 上限 100,实测)。

    与 Binance 分支的区别:交易所不给 startTime 过滤,只能按页往回翻到
    窗口左界;返回前按窗口裁剪,口径与 `fetch_funding` 完全一致。
    """
    out: list[dict] = []
    page = 1
    body: dict = {}
    while page <= BITGET_MAX_PAGES:
        for attempt in range(RETRIES):
            try:
                r = requests.get(BITGET_FUNDING, params={
                    "symbol": symbol, "productType": product_type,
                    "pageSize": BITGET_PAGE_LIMIT, "pageNo": page}, timeout=20)
                r.raise_for_status()
                body = r.json()
                break
            except Exception:                    # noqa: BLE001 - 重试后仍失败则上抛
                if attempt == RETRIES - 1:
                    raise
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        if str(body.get("code") or "") not in ("", "00000"):
            raise RuntimeError(f"bitget {body.get('code')} {body.get('msg')}")
        rows = body.get("data") or []
        if not rows:
            break
        out.extend(rows)
        oldest = min(int(x["fundingTime"]) for x in rows)
        if oldest <= min(start_ms, end_ms):
            break
        page += 1
        time.sleep(SLEEP_S)
    lo, hi = min(start_ms, end_ms), max(start_ms, end_ms)
    return [x for x in out if lo <= int(x["fundingTime"]) <= hi]


def validate(series: list[tuple[int, float]]) -> list[str]:
    """连续性检查:升序、无重复、间隔为整小时(交易所偶发 4h/8h/1h 结算)。"""
    issues: list[str] = []
    for i in range(1, len(series)):
        gap = series[i][0] - series[i - 1][0]
        if gap <= 0:
            issues.append(f"non-monotonic at {series[i][0]}")
        elif gap % 3_600_000:
            issues.append(f"sub-hour gap {gap}ms at {series[i][0]}")
    return issues


def write_symbol(symbol: str, series: list[tuple[int, float]],
                 out_dir: Path, *, source: str = "binance") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}.csv"
    body = ["funding_time,funding_rate"]
    body += [f"{ts},{rate:.10g}" for ts, rate in series]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:                                   # out 目录在仓库之外
        rel = path.as_posix()
    src = ("binance_fapi/fapi/v1/fundingRate" if source == "binance"
           else "bitget_mix/market/history-fund-rate")
    note = ("settlements every 8h (occasionally 4h/1h); signed rate"
            if source == "binance" else
            "settlements every 4h or 8h (exchange fundInterval); signed rate")
    return {
        "path": rel,
        "sha256": digest,
        "rows": len(series),
        "ts_first": series[0][0] if series else None,
        "ts_last": series[-1][0] if series else None,
        "source": src,
        "note": note,
    }


def update_manifest(records: dict, *, source: str = "binance") -> None:
    manifest: dict = {"files": {}}
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    files = manifest.setdefault("files", {})
    prefix = "binance_fapi_funding" if source == "binance" else "bitget_mix_funding"
    for symbol, rec in records.items():
        files[f"{prefix}/{symbol}.csv"] = rec
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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
                "或 --days 指定窗口(拉 RWA 标的时通常没有本地 1h 帧)") from None
        hi = int(time.time() * 1000)
        lo = hi - int(args.days) * MS_DAY
    start = int(args.start_ts) if have_start else lo - 2 * MS_DAY
    end = int(args.end_ts) if have_end else hi + 2 * MS_DAY
    return start, end


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT")
    ap.add_argument("--source", choices=SOURCES, default="binance")
    ap.add_argument("--product-type", default=PRODUCT_TYPE)
    ap.add_argument("--start-ts", type=int, default=None)
    ap.add_argument("--end-ts", type=int, default=None)
    ap.add_argument("--days", type=int, default=None,
                    help="本地无 1h 帧时的回看天数(如 RWA 标的)")
    ap.add_argument("--out", default=str(RAW_DIR / "funding"))
    ap.add_argument("--no-manifest", action="store_true")
    args = ap.parse_args(argv)

    out_dir = Path(args.out).resolve()
    records: dict = {}
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        start, end = _window(symbol, args)
        if args.source == "bitget":
            rows = fetch_funding_bitget(symbol, start, end, args.product_type)
        else:
            rows = fetch_funding(symbol, start, end)
        series = normalize(rows)
        if not series:
            print(f"[{symbol}] 无结算记录(window {start}..{end})")
            continue
        issues = validate(series)
        rec = write_symbol(symbol, series, out_dir, source=args.source)
        rec["window"] = {"start": start, "end": end}
        rec["issues"] = issues
        records[symbol] = rec
        pos = sum(1 for _, r in series if r > 0) / max(len(series), 1)
        gaps = sorted({(series[i][0] - series[i - 1][0]) // 3_600_000
                       for i in range(1, len(series))})
        print(f"[{symbol}] rows={len(series)} "
              f"{time.strftime('%Y-%m-%d', time.gmtime(series[0][0] / 1000))}.."
              f"{time.strftime('%Y-%m-%d', time.gmtime(series[-1][0] / 1000))} "
              f"interval_h={gaps} pos_share={pos:.1%} issues={len(issues)}")
        for msg in issues[:3]:
            print(f"  ! {msg}")

    if not args.no_manifest:
        update_manifest(records, source=args.source)
        print(f"[manifest] {MANIFEST_PATH}")
    print(f"[out] {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
