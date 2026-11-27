"""自动判定非加密(RWA)合约的底层真实交易时段(零写死标的、零写死日期)。

为什么必须自动化且带价格交叉核验:
Bitget 的合约接口**没有底层交易所/交易时段字段**(实测只有 ``isRwa=YES`` /
``fundInterval`` / ``maxLever``)。同一个 RWA 宇宙里混着美股现货、港股/韩股/
日股/A股/欧股、COMEX 金属、NYMEX 能源、即期外汇、24×7 代币化黄金以及未上市
合成合约。单靠 K 线成交分布会因 Bitget 盘前盘后合成交易而把美股与商品混在一起;
单靠 ticker 名称查外部接口又会产生撞名(例:原油合约与同代码消费股)。

本脚本用三层可证伪、零硬编码机制判定每只 RWA 合约的真实日历:
1. **结构标准层**: ``*HKDUSDT`` → ``hk_equity``; ISO-4217 6 字母外汇货币对 → ``fx``;
   ISO-4217 贵金属代码(XAU/XAG/XPT/XPD) → ``metal_futures``。
2. **权威交易所元数据 + 美元/港币价格交叉核验层**:
   查询公开行情元数据(Yahoo Finance v8 chart / v1 search,含对 ``(Derivatives)`` /
   ``GDS`` 包装标的的一次底层名称解包),按实时外汇汇率把标的价格换算为合约计价币种
   (``*HKDUSDT`` 用 HKD,其余 ``*USDT`` 严格用 USD),**必须满足 ``|Bitget价 - 权威价| / 权威价 <= 8%``**
   才采信其官方交易所代码(NMS/NYQ/PCX/BTS → ``us_equity``; CMX → ``metal_futures``;
   NYM/ICE → ``energy_futures``; CME/CBT → ``us_index_futures``; HKG → ``hk_equity``;
   KSC/KOE → ``kr_equity``; JPX/TYO → ``jp_equity``; TAI/TWO → ``tw_equity``;
   SHH/SHZ → ``cn_equity``; NSI/BSE → ``in_equity``; LSE/GER/FRA/PAR/AMS/SWX → ``eu_equity``;
   原生 24×7 非衍生品加密资产 ``CCC`` 且价差 <= 5% → ``crypto``)。
   价格交叉核验从根上杜绝同名撞车。
3. **DST 感知密度比兜底层**(针对未上市 Pre-IPO 或交易所专属合成合约,或 ``--no-external``):
   用合约自身工作日 1H K 线的『核心时段中位振幅 / 非核心时段中位振幅』(自动跟随 IANA 夏令时),
   并叠加 CME 17:00-18:00 ET 维护塌陷校验;仅在显著集中(>= 1.5x 且领先另一时区 >= 1.25x)时
   分配 ``us_equity`` / ``asia_equity``,其余保守落回 ``weekday_only``(再由运行时
   ``quiet_window`` 与 ``quote_guard`` 双重兜底)。

产物 ``data/calendars/assignments.json`` 由 ``market_operator.sessions`` 自动按 mtime
热加载,优先级: 运维覆盖(overrides.json) > 结构命名族(patterns) > 自动分派(assignments.json) > 兜底。

用法:
  python scripts/learn_sessions.py                         # 全量 isRwa=YES → assignments.json
  python scripts/learn_sessions.py --days 21 --workers 8
  python scripts/learn_sessions.py --symbols AUSDT,BUSDT --dry-run
  python scripts/learn_sessions.py --no-external           # 离线仅用 K 线密度兜底
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
import sys
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import heartbeat as hb                                # noqa: E402
from market_operator import paths                                          # noqa: E402
from market_operator import sessions                                       # noqa: E402
from market_operator.bitget_live import BitgetLiveClient                   # noqa: E402
from market_operator.timeframes import MS_PER_DAY                          # noqa: E402

OUT_PATH = paths.data_dir() / "calendars" / "assignments.json"
ET = ZoneInfo("America/New_York")
MS_DAY = MS_PER_DAY
MS_HALF_HOUR = 1_800_000
CME_MAINT_HOUR_ET = 17
_MISS_DIFF = 999.0
_DIRECT_INSTRUMENT_TYPES = frozenset({"EQUITY", "ETF", "INDEX", "FUTURE", "CURRENCY"})
_SEARCH_QUOTE_TYPES = frozenset({"EQUITY", "ETF", "FUTURE", "CURRENCY"})

# ISO-4217 贵金属标准代码(国际货币标准,非个股代码)
ISO_METALS = frozenset({"XAU", "XAG", "XPT", "XPD"})

# 纯交易所代码 → config/sessions.json 日历分类(零个股代码)
EXCHANGE_TO_CAL: Mapping[str, str] = {
    # 美国现货股市(NYSE / Nasdaq / Arca / BATS / AMEX)
    "NMS": "us_equity", "NYQ": "us_equity", "NGM": "us_equity",
    "NCM": "us_equity", "ASE": "us_equity", "PCX": "us_equity",
    "BTS": "us_equity", "BATS": "us_equity", "NYS": "us_equity", "NAS": "us_equity",
    # 亚太现货股市
    "HKG": "hk_equity",
    "KSC": "kr_equity", "KOE": "kr_equity",
    "JPX": "jp_equity", "TYO": "jp_equity", "OSA": "jp_equity",
    "TAI": "tw_equity", "TWO": "tw_equity",
    "SHH": "cn_equity", "SHZ": "cn_equity",
    "NSI": "in_equity", "BSE": "in_equity",
    # 欧洲现货股市
    "LSE": "eu_equity", "GER": "eu_equity", "FRA": "eu_equity",
    "PAR": "eu_equity", "AMS": "eu_equity", "SWX": "eu_equity",
    "MIL": "eu_equity", "STO": "eu_equity", "CPH": "eu_equity", "HEL": "eu_equity",
    # 商品 / 指数期货与即期外汇
    "CMX": "metal_futures",
    "NYM": "energy_futures",
    "ICE": "energy_futures",
    "CME": "us_index_futures",
    "CBT": "us_index_futures",
    "CCY": "fx",
}

_DERIV_WRAPPER_MARKS = (
    "derivative", "tokenized", "xstock", "perpetual", "perp", "synthetic", "gds",
)
_STRIP_WRAPPER_RE = re.compile(
    r"\((?:derivatives|xstock|tokenized[^)]*)\)|\b(?:usd|gds|co\.,?\s*ltd\.?|inc\.?|jsc\s*ltd\.?|corp\.?|holdings?)\b",
    re.IGNORECASE,
)
_FX_CURRENCIES = (
    "HKD", "KRW", "JPY", "TWD", "CNY", "EUR", "GBP",
    "INR", "CHF", "SEK", "DKK", "CAD", "AUD", "SGD",
)


def rwa_symbols(
    explicit: str | None,
    cli: BitgetLiveClient,
    *,
    specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    if explicit:
        return sorted({s.strip().upper() for s in explicit.split(",") if s.strip()})
    rows = specs.values() if specs is not None else cli.contracts()
    return sorted({
        str(r.get("symbol"))
        for r in rows
        if r.get("symbol") and str(r.get("isRwa") or "").upper() == "YES"
    })


def fetch_bars(
    cli: BitgetLiveClient,
    symbol: str,
    days: int,
    granularity: str = "1H",
    page: int = 200,
) -> list[tuple[int, float, float, float, float, float]]:
    """按 endTime 往回翻,拿到 days 天的已收盘 K 线(升序去重)。"""
    end = int(time.time() * 1000)
    start = end - int(days) * MS_DAY
    got: dict[int, tuple[int, float, float, float, float, float]] = {}
    cursor = end
    while cursor > start:
        rows = cli.history_candles(symbol, granularity, end_ms=cursor, limit=page)
        if not rows:
            break
        for bar in rows:
            got[int(bar[0])] = bar
        oldest = min(int(bar[0]) for bar in rows)
        if oldest <= start or oldest >= cursor:
            break
        cursor = oldest
    return [got[k] for k in sorted(got)]


class Masks:
    """日历窗口掩码((日历, ts) 线程安全缓存,避免逐标的重复时区换算)。"""

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.cfg = cfg
        self._cache: dict[tuple[str, int], bool] = {}
        self._lock = threading.Lock()

    def of(self, calendar: str, ts: int) -> bool:
        key = (calendar, int(ts))
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        computed = sessions.in_session(calendar, int(ts), self.cfg)
        with self._lock:
            self._cache[key] = computed
        return computed


def features(bars: Sequence[tuple[Any, ...]], masks: Masks) -> dict[str, Any]:
    """DST 感知活动密度特征(range 比 volume 更真实反映价格发现)。"""
    wd_rows: list[tuple[int, float]] = []
    all_rng: list[float] = []
    for b in bars:
        ts = int(b[0])
        close = max(float(b[4]), 1e-12)
        rng = max(0.0, float(b[2]) - float(b[3])) / close
        all_rng.append(rng)
        if masks.of("weekday_only", ts):
            wd_rows.append((ts, rng))
    total = sum(all_rng) or 1.0
    wd_sum = sum(r for _, r in wd_rows) or 1.0

    def density_ratio(cal: str) -> float:
        ins = [r for ts, r in wd_rows if masks.of(cal, ts + MS_HALF_HOUR)]
        outs = [r for ts, r in wd_rows if not masks.of(cal, ts + MS_HALF_HOUR)]
        if len(ins) < 10 or len(outs) < 10:
            return 1.0
        om = statistics.median(outs)
        return (statistics.median(ins) / om) if om > 0.0 else 1.0

    def share(cal: str) -> float:
        return sum(r for ts, r in wd_rows if masks.of(cal, ts + MS_HALF_HOUR)) / wd_sum

    brk: list[float] = []
    rest: list[float] = []
    for ts, r in wd_rows:
        if datetime.fromtimestamp(ts / 1000, ET).hour == CME_MAINT_HOUR_ET:
            brk.append(r)
        else:
            rest.append(r)
    med = statistics.median(rest) if rest else 0.0
    brk_ratio = (statistics.median(brk) / med) if brk and med > 0.0 else 9.9
    return {
        "us_density": round(density_ratio("us_equity"), 3),
        "asia_density": round(density_ratio("asia_equity"), 3),
        "rth": round(share("us_equity"), 4),
        "asia": round(share("asia_equity"), 4),
        "brk": round(brk_ratio, 3),
        "weekend_share": round((total - wd_sum) / total, 4),
        "bars": len(bars),
    }


def classify(
    feats: Mapping[str, Any],
    *,
    min_bars: int = 60,
    density_min: float = 1.50,
    lead_mult: float = 1.25,
    brk_min: float = 0.50,
) -> tuple[str, str]:
    """K 线密度兜底判定(仅在权威交易所查无结果或离线模式下使用)。"""
    if int(feats["bars"]) < min_bars:
        return "weekday_only", "insufficient_bars"
    us_d = float(feats.get("us_density", 1.0))
    as_d = float(feats.get("asia_density", 1.0))
    brk = float(feats.get("brk", 9.9))
    if us_d >= density_min and us_d >= as_d * lead_mult and brk >= brk_min:
        return "us_equity", f"density_us={us_d:.2f}x"
    if as_d >= density_min and as_d >= us_d * lead_mult:
        return "asia_equity", f"density_asia={as_d:.2f}x"
    return "weekday_only", f"density_flat(us={us_d:.2f},as={as_d:.2f})"


def _yahoo_chart_meta(
    sess: requests.Session,
    ticker: str,
    timeout: float = 6.0,
) -> dict[str, Any] | None:
    try:
        r = sess.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1d",
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result") or []
        return (res[0].get("meta") or None) if res else None
    except (requests.RequestException, ValueError):
        return None


def _yahoo_search(
    sess: requests.Session,
    query: str,
    count: int = 8,
    timeout: float = 6.0,
) -> list[dict[str, Any]]:
    try:
        r = sess.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": query, "quotesCount": count, "newsCount": 0},
            timeout=timeout,
        )
        if r.status_code != 200:
            return []
        return list((r.json() or {}).get("quotes") or [])
    except (requests.RequestException, ValueError):
        return []


def fetch_fx_to_usd(timeout: float = 5.0) -> dict[str, float]:
    """获取主要法币兑 USD 即期汇率,用于跨币种股票价格交叉核验。"""
    rates: dict[str, float] = {"USD": 1.0, "USX": 0.01}
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
    for ccy in _FX_CURRENCIES:
        meta = _yahoo_chart_meta(sess, f"{ccy}USD=X", timeout=timeout)
        if not meta:
            continue
        px = float(meta.get("regularMarketPrice") or 0.0)
        if px > 0.0:
            rates[ccy] = px
    if "GBP" in rates:
        rates["GBp"] = rates["GBP"] * 0.01
    return rates


def price_matches_in_quote_ccy(
    symbol: str,
    bitget_px: float,
    meta: Mapping[str, object],
    fx_rates: Mapping[str, float],
    tol: float = 0.08,
) -> tuple[bool, float, float]:
    """按合约计价币种校验 Bitget 现价与外部权威价是否一致。

    返回 ``(是否匹配, 相对价差, 换算后的参考价)``。
    * ``*HKDUSDT`` 合约以 HKD 计价,仅匹配 HKD 标的价格;
    * 其余 ``*USDT`` 合约严格把外部价格按实时汇率换算为 USD 后比对,绝不用原始外币数额硬凑。
    """
    if bitget_px <= 0.0:
        return False, _MISS_DIFF, 0.0
    y_px = float(meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or 0.0)
    if y_px <= 0.0:
        return False, _MISS_DIFF, 0.0
    ccy = str(meta.get("currency") or "USD")
    if symbol.endswith("HKDUSDT"):
        if ccy != "HKD":
            return False, _MISS_DIFF, 0.0
        ref_px = y_px
    else:
        rate = fx_rates.get(ccy)
        if rate is None or rate <= 0.0:
            return False, _MISS_DIFF, 0.0
        ref_px = y_px * float(rate)
    diff = abs(bitget_px - ref_px) / max(ref_px, 1e-12)
    return (diff <= tol), diff, ref_px


def _is_derivative_wrapper(meta_or_quote: Mapping[str, object]) -> bool:
    txt = " ".join(
        str(meta_or_quote.get(k) or "")
        for k in ("shortName", "longName", "shortname", "longname")
    ).lower()
    return any(m in txt for m in _DERIV_WRAPPER_MARKS)


def _unwrap_entity_name(raw_name: str) -> str:
    cleaned = _STRIP_WRAPPER_RE.sub(" ", str(raw_name or ""))
    cleaned = re.sub(r"[^A-Za-z0-9\s\-&]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _name_compatible(base: str, quote: Mapping[str, object]) -> bool:
    """确认 search 返回的标的名称或代码与待查前缀具备合理关联,防纯价格巧合。"""
    b = re.sub(r"[^A-Z0-9]", "", base.upper())
    if len(b) < 2:
        return False
    qsym = re.sub(r"[^A-Z0-9]", "", str(quote.get("symbol") or "").upper())
    qname = re.sub(
        r"[^A-Z0-9]",
        "",
        f"{quote.get('shortname') or ''}{quote.get('longname') or ''}".upper(),
    )
    return qsym.startswith(b) or b in qname or (len(b) >= 4 and b[:4] in qname)


def _verified_hit(
    *,
    cal: str,
    reason: str,
    ticker: str,
    exchange: str,
    instrument_type: str,
    bitget_px: float,
    ref_px: float,
    diff: float,
) -> dict[str, Any]:
    return {
        "calendar": cal,
        "reason": reason,
        "matched_ticker": ticker,
        "exchange": exchange,
        "instrument_type": instrument_type,
        "bitget_px": round(bitget_px, 6),
        "ref_px": round(ref_px, 6),
        "price_diff_pct": round(diff * 100.0, 2),
    }


def _resolve_direct_candidates(
    symbol: str,
    candidates: Sequence[str],
    bitget_px: float,
    fx_rates: Mapping[str, float],
    cals: Mapping[str, object],
    sess: requests.Session,
    tol: float,
) -> dict[str, Any] | None:
    for ticker in candidates:
        meta = _yahoo_chart_meta(sess, ticker)
        if not meta:
            continue
        exch = str(meta.get("exchangeName") or "").upper()
        itype = str(meta.get("instrumentType") or "").upper()
        cal = EXCHANGE_TO_CAL.get(exch)
        if not cal or cal not in cals or itype not in _DIRECT_INSTRUMENT_TYPES:
            continue
        ok, diff, ref_px = price_matches_in_quote_ccy(symbol, bitget_px, meta, fx_rates, tol=tol)
        if ok:
            return _verified_hit(
                cal=cal,
                reason="yahoo_direct_verified",
                ticker=ticker,
                exchange=exch,
                instrument_type=itype,
                bitget_px=bitget_px,
                ref_px=ref_px,
                diff=diff,
            )
    return None


def _match_from_quotes(
    symbol: str,
    quotes: Sequence[Mapping[str, Any]],
    *,
    check_name: bool,
    query_key: str,
    bitget_px: float,
    fx_rates: Mapping[str, float],
    cals: Mapping[str, object],
    sess: requests.Session,
    tol: float,
) -> dict[str, Any] | None:
    for q in quotes:
        exch = str(q.get("exchange") or "").upper()
        qtype = str(q.get("quoteType") or "").upper()
        qsym = str(q.get("symbol") or "")
        cal = EXCHANGE_TO_CAL.get(exch)
        if not qsym or not cal or cal not in cals or qtype not in _SEARCH_QUOTE_TYPES:
            continue
        if check_name and not _name_compatible(query_key, q):
            continue
        meta = _yahoo_chart_meta(sess, qsym)
        if not meta:
            continue
        ok, diff, ref_px = price_matches_in_quote_ccy(symbol, bitget_px, meta, fx_rates, tol=tol)
        if ok:
            return _verified_hit(
                cal=cal,
                reason="yahoo_search_verified",
                ticker=qsym,
                exchange=exch,
                instrument_type=qtype,
                bitget_px=bitget_px,
                ref_px=ref_px,
                diff=diff,
            )
    return None


def resolve_via_yahoo(
    symbol: str,
    spec: Mapping[str, object],
    bitget_px: float,
    fx_rates: Mapping[str, float],
    cals: Mapping[str, object],
    sess: requests.Session,
    tol: float = 0.08,
) -> dict[str, Any] | None:
    """权威交易所元数据 + 价格交叉核验解析。未通过价格核验一律返回 None。"""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    if base in ISO_METALS and "metal_futures" in cals:
        return {
            "calendar": "metal_futures",
            "reason": "iso4217_metal",
            "matched_ticker": base,
            "exchange": "CMX",
            "instrument_type": "FUTURE",
        }
    if bitget_px <= 0.0:
        return None

    bases = [base]
    if base.endswith("STOCK") and len(base) > 5:
        bases.append(base[:-5])
    prefer_fut = (
        float(spec.get("maxLever") or 0.0) >= 50.0
        or int(spec.get("fundInterval") or 8) == 4
    )
    direct_cands = [
        cand
        for b in bases
        for cand in ([f"{b}=F", b] if prefer_fut else [b, f"{b}=F"])
    ]
    hit = _resolve_direct_candidates(symbol, direct_cands, bitget_px, fx_rates, cals, sess, tol)
    if hit is not None:
        return hit

    # 2b. 检查是否为原生 24×7 代币化加密资产(例 PAXG-USD / XAUT-USD;严格排除 Derivatives/xStock 包装)
    if "crypto" in cals:
        meta_c = _yahoo_chart_meta(sess, f"{base}-USD")
        if (
            meta_c
            and str(meta_c.get("exchangeName") or "").upper() == "CCC"
            and str(meta_c.get("instrumentType") or "").upper() == "CRYPTOCURRENCY"
            and not _is_derivative_wrapper(meta_c)
        ):
            ok, diff, ref_px = price_matches_in_quote_ccy(
                symbol, bitget_px, meta_c, fx_rates, tol=0.05
            )
            if ok:
                return _verified_hit(
                    cal="crypto",
                    reason="yahoo_native_24x7_token",
                    ticker=f"{base}-USD",
                    exchange="CCC",
                    instrument_type="CRYPTOCURRENCY",
                    bitget_px=bitget_px,
                    ref_px=ref_px,
                    diff=diff,
                )

    # 2c. Yahoo Search + 对 (Derivatives)/GDS 包装自动解包一次二跳查询
    quotes = _yahoo_search(sess, base)
    hit = _match_from_quotes(
        symbol, quotes, check_name=True, query_key=base,
        bitget_px=bitget_px, fx_rates=fx_rates, cals=cals, sess=sess, tol=tol,
    )
    if hit is not None:
        return hit

    unwrapped_names: list[str] = []
    for q in quotes:
        if not _is_derivative_wrapper(q):
            continue
        ent = _unwrap_entity_name(str(q.get("shortname") or q.get("longname") or ""))
        if ent and len(ent) >= 3 and ent.upper() != base.upper() and ent not in unwrapped_names:
            unwrapped_names.append(ent)
    for ent in unwrapped_names[:2]:
        hit2 = _match_from_quotes(
            symbol, _yahoo_search(sess, ent), check_name=False, query_key=ent,
            bitget_px=bitget_px, fx_rates=fx_rates, cals=cals, sess=sess, tol=tol,
        )
        if hit2 is not None:
            hit2["reason"] = f"yahoo_unwrapped_verified:{ent}"
            return hit2
    return None


def _write_assignments(
    out: Path,
    results: Mapping[str, Mapping[str, Any]],
    args: argparse.Namespace,
) -> None:
    assignments = {s: r["calendar"] for s, r in results.items() if r.get("calendar")}
    previous_doc: dict[str, Any] = {}
    if out.exists():
        try:
            previous_doc = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous_doc = {}
    diag = dict(previous_doc.get("diagnostics") or {}) if args.merge else {}
    if args.merge:
        assignments = {**(previous_doc.get("assignments") or {}), **assignments}
    diag.update(results)

    payload = {
        "schema": 2,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "price_verified_exchange_taxonomy+dst_density_fallback",
        "params": {
            "days": args.days,
            "granularity": args.granularity,
            "external": not args.no_external,
        },
        "assignments": assignments,
        "diagnostics": diag,
    }
    if args.dry_run:
        print("[learn] dry-run, not written")
        return
    if (previous_doc.get("assignments") or {}) == assignments and previous_doc.get("schema") == 2:
        print("[learn] assignments unchanged, not written")
        return
    hb.write_atomic(out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"[learn] wrote {out}")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="逗号分隔;缺省取全部 isRwa=YES 合约")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--granularity", default="1H")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--merge",
        action="store_true",
        help="只更新本次给出的标的,其余沿用已有 assignments",
    )
    ap.add_argument(
        "--no-external",
        action="store_true",
        help="不查询外部权威交易所元数据,仅用 K 线密度兜底",
    )
    args = ap.parse_args(argv)

    cli = BitgetLiveClient(None)
    specs = {str(c["symbol"]): c for c in cli.contracts() if c.get("symbol")}
    tickers = {str(t["symbol"]): t for t in cli.tickers() if t.get("symbol")}
    syms = rwa_symbols(args.symbols, cli, specs=specs)
    cfg = sessions.load_config()
    cals = cfg.get("calendars") or {}
    masks = Masks(cfg)
    overrides = sessions.load_overrides()      # 运维显式覆盖优先,不再重复推断
    fx_rates = {} if args.no_external else fetch_fx_to_usd()
    if not args.no_external:
        print(f"[learn] fx_rates loaded={len(fx_rates)} symbols={len(syms)}", flush=True)

    tls = threading.local()

    def _http() -> requests.Session:
        s = getattr(tls, "sess", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = "Mozilla/5.0"
            tls.sess = s
        return s

    def _bitget() -> BitgetLiveClient:
        client = getattr(tls, "bitget", None)
        if client is None:
            client = BitgetLiveClient(None)
            tls.bitget = client
        return client

    def work(sym: str) -> tuple[str, dict[str, Any]]:
        resolved, source = sessions.resolve_calendar(sym, cfg, overrides, {})
        if source in ("override", "pattern"):
            return sym, {"skipped": source, "calendar": resolved, "reason": f"rule_{source}"}
        sp = specs.get(sym) or {}
        tk = tickers.get(sym) or {}
        bitget_px = float(tk.get("lastPr") or tk.get("markPrice") or 0.0)
        if not args.no_external:
            hit = resolve_via_yahoo(sym, sp, bitget_px, fx_rates, cals, _http())
            if hit is not None:
                return sym, {
                    **hit,
                    "fund_interval": sp.get("fundInterval"),
                    "max_lever": sp.get("maxLever"),
                }
        try:
            bars = fetch_bars(_bitget(), sym, args.days, args.granularity)
        except Exception as exc:                 # 单标的失败不拖全局
            return sym, {
                "skipped": f"fetch_error:{type(exc).__name__}",
                "calendar": "weekday_only",
            }
        feats = features(bars, masks)
        cal, why = classify(feats)
        return sym, {
            "calendar": cal,
            "reason": why,
            "fund_interval": sp.get("fundInterval"),
            "max_lever": sp.get("maxLever"),
            **feats,
        }

    done = 0
    lock = threading.Lock()

    def work_tracked(sym: str) -> tuple[str, dict[str, Any]]:
        nonlocal done
        out = work(sym)
        with lock:
            done += 1
            if done % 25 == 0 or done == len(syms):
                print(f"[learn] resolved {done}/{len(syms)}", flush=True)
        return out

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        results = dict(ex.map(work_tracked, syms))

    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["calendar"]] = counts.get(r["calendar"], 0) + 1
    print(
        "[learn] symbols="
        + str(len(results))
        + " "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    fallback = [s for s, r in results.items() if r["calendar"] == "weekday_only"]
    if fallback:
        print(f"[learn] weekday_only={len(fallback)} e.g. {fallback[:10]}")
    _write_assignments(Path(args.out), results, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
