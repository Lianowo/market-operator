"""非加密(RWA)合约的交易时段日历:按底层市场的**规则**判定"能不能新开仓"。

问题:Bitget 的 ``isRwa`` 只标出"非加密",**不区分股票/商品/指数/外汇**。同一个
非加密宇宙里,美股现货(NYSE/Nasdaq)、CME 期货(Gold/CL/ES)、即期外汇、港股、
日股、韩股、台股的开收盘与节假日完全不同。改动前的实现把全部非加密合约塞进
一条硬编码的"周五 21:00 UTC ~ 周日 22:00 UTC"周末窗口:

* 没有夏令时修正 —— 美东 16:00 收盘在夏令时是 20:00 UTC,旧窗口 20:00-21:00 UTC
  这段死水仍放行开仓;
* 没有节假日/半日市 —— 感恩节、圣诞前夜 13:00 ET 之后仍是工作日,旧窗口放行;
* 商品/指数/外汇被错误地当成美股现货关闭 —— 它们真实交易约 23 小时/日,
  于是系统在大部分可交易时段拒绝开仓("不能正常交易")。

设计纪律(与仓库既有口径一致):

* **代码里没有固定标的、没有固定年月日**。节假日由交易所规则推导
  (:func:`us_equity_dates`):第 n 个星期几、复活节推算法、观察日平移、
  半日市规则;公元纪年可无限延伸,不会因为"写死了 2026-2028"而过期。
* **标的 → 日历** 只按"命名族"规则解析(外汇货币对形状、``HKD`` 后缀、
  ``TW`` 前缀),另外允许一份**运维覆盖文件**逐标的登记,便于新上架合约
  不用改代码也不用改仓库配置。
* 日历**形状**(窗口/时区/跨午夜会话)写在 ``config/sessions.json``,
  那里只有结构常量,没有日期、没有 ticker。
* 未登记的 RWA 标的一律走 ``weekday_only``(仅周末关闭),并在
  :func:`describe` 里标出 ``source="default"``,供运维补齐;绝不静默假装
  自己知道该标的属于哪个市场。

本模块只做执行闸门(禁新开仓),**不参与任何策略语义**:
- 只对 ``is_rwa=True`` 生效,原生加密保持 24x7;
- 既有仓位的交易所原生止损/止盈照常全天候守护(调用方保证,本模块不碰);
- 返回码沿用既有契约 ``RWA_MARKET_CLOSED`` / ``RWA_INACTIVE_SESSION``,
  因此通知文案、计数器与历史账本口径不变。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time as _time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "sessions.json"
OVERRIDES_PATH = ROOT / "data" / "calendars" / "overrides.json"

CLOSED = "RWA_MARKET_CLOSED"
INACTIVE = "RWA_INACTIVE_SESSION"
LEGACY = "legacy_weekend"
DEFAULT_RWA = "weekday_only"
US_EQUITY_RULES = "us_equity"

DAY_S = 86_400
_CONFIG_CACHE: tuple[Any, Optional[Mapping[str, Any]]] = (None, None)
_OVER_CACHE: tuple[Any, Optional[Mapping[str, Any]]] = (None, None)

_LEGACY_FALLBACK: Mapping[str, Any] = {
    "calendars": {LEGACY: {"legacy_weekend": True}},
    "rwa_default_calendar": LEGACY,
    "patterns": [],
}


def _hms(text: str) -> int:
    """``"09:30"`` → 34200;``"24:00"`` → 86400(仅收盘时间可等于日终)。"""
    parts = str(text).split(":")
    if len(parts) != 2:
        raise ValueError(f"时间必须为 HH:MM,收到 {text!r}")
    total = int(parts[0]) * 3600 + int(parts[1]) * 60
    if not 0 <= total <= DAY_S:
        raise ValueError(f"时间越界: {text!r}")
    return total


# ---------------------------------------------------------------------------
# 节假日:交易所规则推导(不写任何具体日期)
# ---------------------------------------------------------------------------


def _observed(d: date, *, saturday_skips: bool = False) -> Optional[date]:
    """观察日平移:周六 → 周五(或按交易所规则不补),周日 → 周一。"""
    if d.weekday() == 5:
        return None if saturday_skips else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """某年某月的第 n 个星期 weekday(0=周一)。"""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """某年某月最后一个星期 weekday。"""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """复活节(匿名格里高利算法,Meeus/Jones/Butcher):Good Friday = 减 2 天。"""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month = (h + lam - 7 * m + 114) // 31
    day = ((h + lam - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=8)
def us_equity_dates(year: int) -> tuple[frozenset[date], frozenset[date]]:
    """美股现货(NYSE/Nasdaq)第 ``year`` 年的 (全天休市, 半日市)。

    规则(与交易所公开日历同构,但**不出现日期字面量**):
    元旦(周六不补/周日顺延周一)、MLK(1 月第 3 个周一)、总统日(2 月第 3 个周一)、
    耶稣受难日(复活节前 2 天)、阵亡将士(5 月最后一个周一)、六月节(6/19 观察日)、
    独立日(7/4 观察日)、劳动节(9 月第 1 个周一)、感恩节(11 月第 4 个周四)、
    圣诞(12/25 观察日)。
    半日市(13:00 ET):感恩节次日、12/24(周一至周四)、7/3(当 7/4 落在工作日)。
    """
    closed: set[date] = set()
    for d in (
        _observed(date(year, 1, 1), saturday_skips=True),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    ):
        if d is not None and d.year == year:      # 跨年观察日只记在本年
            closed.add(d)
    early: set[date] = {_nth_weekday(year, 11, 3, 4) + timedelta(days=1)}
    if date(year, 12, 24).weekday() <= 3:         # 周一…周四
        early.add(date(year, 12, 24))
    if date(year, 7, 4).weekday() <= 4:           # 7/4 是工作日 ⇒ 7/3 提前收
        early.add(date(year, 7, 3))
    return frozenset(closed), frozenset(early)


@lru_cache(maxsize=64)
def _rule_dates(rules: str, year: int) -> tuple[frozenset[date], frozenset[date]]:
    if rules == US_EQUITY_RULES:
        return us_equity_dates(year)
    return frozenset(), frozenset()


def _load_overrides(path: str | Path | None = None) -> Mapping[str, Any]:
    """运维覆盖文件(可缺省):``data/calendars/overrides.json``。

    结构:``{"assignments": {symbol: calendar}, "closed": {calendar: [YYYY-MM-DD]},
    "early_closes": {calendar: {YYYY-MM-DD: "HH:MM"}}}``。
    它属于**运行数据**,不写进仓库配置,新上架合约/临时休市不用改代码。
    """
    global _OVER_CACHE
    p = Path(path) if path is not None else OVERRIDES_PATH
    if not p.exists():
        return {}
    stat = p.stat()
    key = (str(p), stat.st_mtime_ns, stat.st_size)
    if _OVER_CACHE[0] == key and _OVER_CACHE[1] is not None:
        return _OVER_CACHE[1]
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("overrides.json: 顶层必须是对象")
    if not isinstance(data.get("assignments") or {}, Mapping):
        raise ValueError("overrides.json: assignments 必须是 {symbol: calendar}")
    for cal_name, days in (data.get("closed") or {}).items():
        for day in days:
            date.fromisoformat(str(day))                # 非法日期在加载时就失败
    for cal_name, table in (data.get("early_closes") or {}).items():
        if not isinstance(table, Mapping):
            raise ValueError(f"overrides.json: early_closes[{cal_name}] 必须是 {date: HH:MM}")
        for day, hhmm in table.items():
            date.fromisoformat(str(day))
            _hms(hhmm)
    _OVER_CACHE = (key, data)
    return data



# ---------------------------------------------------------------------------
# 配置(只有结构:窗口/时区/假日规则族;没有日期与 ticker)
# ---------------------------------------------------------------------------


def _validate(cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    cals = cfg.get("calendars")
    if not isinstance(cals, Mapping) or not cals:
        raise ValueError("sessions.json: calendars 缺失或为空")
    for name, cal in cals.items():
        if not isinstance(cal, Mapping):
            raise ValueError(f"sessions.json: 日历 {name!r} 不是对象")
        forms = [bool(cal.get("always_open")), bool(cal.get("legacy_weekend")),
                 bool(cal.get("weekday_only")),
                 cal.get("daily_session") is not None,
                 cal.get("windows") is not None]
        if sum(1 for x in forms if x) != 1:
            raise ValueError(
                f"sessions.json: 日历 {name!r} 必须且只能选择一种时段形式"
                "(always_open / legacy_weekend / weekday_only / daily_session / windows)")
        if cal.get("always_open") or cal.get("legacy_weekend"):
            continue
        tz = str(cal.get("tz") or "")
        if not tz:
            raise ValueError(f"sessions.json: 日历 {name!r} 缺少 tz")
        try:
            ZoneInfo(tz)                                # 未知时区立即失败(fail-closed)
        except Exception as exc:                        # ZoneInfoNotFoundError 等
            raise ValueError(
                f"sessions.json: 日历 {name!r} 的 tz={tz!r} 不可用: {exc}") from exc
        rules = str(cal.get("holiday_rules") or "none")
        if rules not in ("none", US_EQUITY_RULES):
            raise ValueError(f"sessions.json: 日历 {name!r} holiday_rules={rules!r} 不受支持")
        ds = cal.get("daily_session")
        if ds is not None:
            if not ds.get("start_weekdays"):
                raise ValueError(f"sessions.json: 日历 {name!r} daily_session 缺少 start_weekdays")
            _hms(ds["open"])
            _hms(ds["close"])
        for w in cal.get("windows") or ():
            if not w.get("weekdays"):
                raise ValueError(f"sessions.json: 日历 {name!r} window 缺少 weekdays")
            _hms(w["open"])
            _hms(w["close"])
        _hms(cal.get("early_close_time") or "13:00")
    default = cfg.get("rwa_default_calendar")
    if default is not None and default not in cals:
        raise ValueError(f"sessions.json: rwa_default_calendar={default!r} 未在 calendars 中定义")
    for rule in cfg.get("patterns") or ():
        cal_name = rule.get("calendar")
        if cal_name not in cals:
            raise ValueError(f"sessions.json: pattern 指向未定义日历 {cal_name!r}")
        re.compile(str(rule.get("pattern") or ""))
    return cfg


def load_config(path: str | Path | None = None) -> Mapping[str, Any]:
    """读取并校验日历形状配置(按 mtime 缓存);文件缺失 → 旧周末口径兜底。"""
    global _CONFIG_CACHE
    p = Path(path) if path is not None else CONFIG_PATH
    if not p.exists():
        return _LEGACY_FALLBACK
    stat = p.stat()
    key = (str(p), stat.st_mtime_ns, stat.st_size)
    if _CONFIG_CACHE[0] == key and _CONFIG_CACHE[1] is not None:
        return _CONFIG_CACHE[1]
    cfg = _validate(json.loads(p.read_text(encoding="utf-8")))
    _CONFIG_CACHE = (key, cfg)
    return cfg


def root_symbol(symbol: Optional[str]) -> Optional[str]:
    """``SYM@4h`` → ``SYM``(去掉执行周期后缀;无后缀原样返回)。"""
    if not symbol:
        return None
    return str(symbol).split("@", 1)[0]


def _resolve(symbol: Optional[str], cfg: Mapping[str, Any],
             overrides: Mapping[str, Any]) -> tuple[str, str]:
    """(日历名, 来源)。来源 ∈ override / pattern / default,供运维补齐未登记标的。"""
    cals = cfg.get("calendars") or {}
    root = root_symbol(symbol)
    if root:
        named = (overrides.get("assignments") or {}).get(root)
        if named in cals:
            return str(named), "override"
        for rule in cfg.get("patterns") or ():
            pattern, cal = rule.get("pattern"), rule.get("calendar")
            if pattern and cal in cals and re.fullmatch(str(pattern), root):
                return str(cal), "pattern"
    fallback = cfg.get("rwa_default_calendar") or DEFAULT_RWA
    return (str(fallback), "default") if fallback in cals else (LEGACY, "default")


def calendar_for(symbol: Optional[str],
                 cfg: Mapping[str, Any] | None = None, *,
                 overrides: Mapping[str, Any] | None = None) -> str:
    c = cfg or load_config()
    return _resolve(symbol, c, overrides if overrides is not None
                    else _load_overrides())[0]


def _calendar_dates(cal_name: str, cal: Mapping[str, Any], year: int,
                    overrides: Mapping[str, Any]) -> tuple[set[date], dict[str, str]]:
    """该日历第 year 年的 (全天休市, {日期: 收盘时间}) = 规则推导 ∪ 运维覆盖。"""
    closed, early = _rule_dates(str(cal.get("holiday_rules") or "none"), year)
    early_time = str(cal.get("early_close_time") or "13:00")
    early_map = {d.isoformat(): early_time for d in early if d.year == year}
    for day in ((overrides.get("closed") or {}).get(cal_name) or ()):
        closed = closed | {date.fromisoformat(str(day))}
    for day, hhmm in ((overrides.get("early_closes") or {}).get(cal_name) or {}).items():
        early_map[str(day)] = str(hhmm)
    return set(closed), early_map


def _legacy_closed(dt_utc: datetime) -> bool:
    """改动前的周末口径:周五 21:00 UTC 后 ~ 周日 22:00 UTC 前(逐位保持)。"""
    wd = dt_utc.weekday()                              # 0=周一 … 5=周六
    return wd == 5 or (wd == 4 and dt_utc.hour >= 21) or (wd == 6 and dt_utc.hour < 22)



def _in_intraday(cal: Mapping[str, Any], local: datetime, closed: set[date],
                 early: Mapping[str, str]) -> bool:
    """日内窗口(现货):每个工作日若干 [open, close) 段,支持半日市与午休。"""
    day = local.date()
    if day in closed:
        return False
    sec = local.hour * 3600 + local.minute * 60 + local.second
    week = local.isoweekday()
    for w in cal.get("windows") or ():
        if week not in {int(x) for x in w["weekdays"]}:
            continue
        open_s = _hms(w["open"])
        close_s = _hms(w["close"])
        if day.isoformat() in early:
            close_s = min(close_s, _hms(early[day.isoformat()]))   # 半日市只提前
        if open_s <= sec < close_s:
            return True
    return False


def _in_daily(cal: Mapping[str, Any], local: datetime, closed: set[date],
              early: Mapping[str, str]) -> bool:
    """跨午夜会话(期货/外汇):工作日 open 起、次日 close 止,天然含每日维护休市。"""
    ds = cal["daily_session"]
    starts = {int(x) for x in ds["start_weekdays"]}
    open_s = _hms(ds["open"])
    naive = local.replace(tzinfo=None)
    for back in (0, 1):                                  # 会话最长 24h:只看今天/昨天起
        start_day = local.date() - timedelta(days=back)
        if start_day.isoweekday() not in starts:
            continue
        if start_day in closed:                          # 节日当天不重开
            continue
        end_day = start_day + timedelta(days=1)
        if end_day in closed:                            # 节日当天无收盘:整段关闭
            continue
        close_s = _hms(early.get(end_day.isoformat(), ds["close"]))
        start_dt = datetime.combine(
            start_day, _time(open_s // 3600, (open_s % 3600) // 60))
        end_dt = (datetime.combine(end_day + timedelta(days=1), _time(0, 0))
                  if close_s >= DAY_S else
                  datetime.combine(end_day, _time(close_s // 3600,
                                                  (close_s % 3600) // 60)))
        if start_dt <= naive < end_dt:
            return True
    return False


def in_session(calendar: str, ts_ms: int,
               cfg: Mapping[str, Any] | None = None, *,
               overrides: Mapping[str, Any] | None = None) -> bool:
    """给定日历名与毫秒时间戳,是否处于可新开仓的时段(未知日历 → 旧周末口径)。"""
    c = cfg or load_config()
    ov = overrides if overrides is not None else _load_overrides()
    cals = c.get("calendars") or {}
    cal = cals.get(calendar)
    known = cal is not None
    if cal is None:
        cal = cals.get(LEGACY) or {"legacy_weekend": True}
    if cal.get("always_open"):
        return True
    if cal.get("legacy_weekend"):
        dt_utc = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
        return not _legacy_closed(dt_utc)
    local = datetime.fromtimestamp(int(ts_ms) / 1000.0, ZoneInfo(str(cal["tz"])))
    closed, early = _calendar_dates(calendar if known else LEGACY, cal,
                                    local.year, ov)
    if cal.get("weekday_only"):
        return local.isoweekday() <= 5 and local.date() not in closed
    if cal.get("daily_session") is not None:
        return _in_daily(cal, local, closed, early)
    return _in_intraday(cal, local, closed, early)


def block_reason(is_rwa: bool, symbol: Optional[str], ts_ms: int,
                 bar: Optional[Sequence[float]] = None, *,
                 cfg: Mapping[str, Any] | None = None,
                 overrides: Mapping[str, Any] | None = None) -> Optional[str]:
    """执行闸门:返回 ``RWA_MARKET_CLOSED`` / ``RWA_INACTIVE_SESSION`` / ``None``。

    判定顺序与原实现一致:先看确认棒是否零成交/一字静止(死水盘),再看日历。
    原生加密(is_rwa=False)恒放行 — 既有仓位的交易所保护单不受本函数影响。
    """
    if not is_rwa:
        return None
    if bar is not None and len(bar) >= 6:
        if float(bar[5]) <= 0.0 or float(bar[2]) <= float(bar[3]):
            return INACTIVE
    c = cfg or load_config()
    ov = overrides if overrides is not None else _load_overrides()
    cal = _resolve(symbol, c, ov)[0]
    return None if in_session(cal, int(ts_ms), c, overrides=ov) else CLOSED


def describe(symbol: Optional[str], ts_ms: Optional[int] = None, *,
             is_rwa: Optional[bool] = None,
             cfg: Mapping[str, Any] | None = None,
             overrides: Mapping[str, Any] | None = None) -> dict:
    """运维诊断:标的解析到的日历、来源、当前是否在时段内(不改变任何判定)。"""
    import time as _time_mod
    ts = int(ts_ms if ts_ms is not None else _time_mod.time() * 1000)
    if is_rwa is False:
        return {"symbol": root_symbol(symbol), "calendar": "crypto",
                "source": "crypto", "in_session": True, "ts": ts}
    c = cfg or load_config()
    ov = overrides if overrides is not None else _load_overrides()
    cal, source = _resolve(symbol, c, ov)
    return {"symbol": root_symbol(symbol), "calendar": cal, "source": source,
            "in_session": in_session(cal, ts, c, overrides=ov), "ts": ts}

