"""会话日历判定:规则推导 + 命名族解析 + 未登记兜底(不含任何写死的日期/标的)。

测试里出现具体日期是允许的(它是**外部权威表的快照**);生产代码
(`src/market_operator/sessions.py`)与仓库配置(`config/sessions.json`)
则必须保持零日期、零 ticker —— 由本文件最后一个测试守住。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_operator import sessions as S
from market_operator.engine import rwa_session_entry_block

ROOT = Path(__file__).resolve().parents[1]

# NYSE 官方 Holidays & Trading Hours 快照(2026-2028),用来校验规则推导。
OFFICIAL_CLOSED = {
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
           "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
           "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
    2028: ["2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19",
           "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25"],
}
OFFICIAL_EARLY = {
    2026: ["2026-11-27", "2026-12-24"],
    2027: ["2027-11-26"],
    2028: ["2028-07-03", "2028-11-24"],
}

CFG = S.load_config()
OVR = {
    "assignments": {"NVDAUSDT": "us_equity", "XAUUSDT": "metal_futures",
                    "PAXGUSDT": "crypto", "SP500USDT": "us_index_futures",
                    "TOYOTAUSDT": "jp_equity", "SAMSUNGUSDT": "kr_equity"},
    "closed": {},
    "early_closes": {},
}


def _ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def _open(sym: str, ts: int) -> bool:
    return S.in_session(S.calendar_for(sym, CFG, overrides=OVR), ts, CFG, overrides=OVR)


def _in(calendar: str, ts: int) -> bool:
    return S.in_session(calendar, ts, CFG)


@pytest.mark.parametrize("year", [2026, 2027, 2028])
def test_us_equity_rules_match_official_calendar(year):
    closed, early = S.us_equity_dates(year)
    assert sorted(d.isoformat() for d in closed if d.year == year) == OFFICIAL_CLOSED[year]
    assert sorted(d.isoformat() for d in early if d.year == year) == OFFICIAL_EARLY[year]


def test_us_equity_rules_extend_beyond_known_years():
    """没有任何日期字面量 ⇒ 规则对任意年份成立(抽查 2030/2032)。"""
    for year in (2030, 2032):
        closed, _ = S.us_equity_dates(year)
        assert len([d for d in closed if d.year == year]) == 10
        assert S._easter(year).weekday() == 6                 # 复活节永远是周日
        assert S._easter(year) - timedelta(days=2) in closed  # Good Friday


def test_us_equity_dst_boundary():
    # 2026-07-01 夏令时:16:00 ET = 20:00 UTC
    assert _in("us_equity", _ts(2026, 7, 1, 19, 30)) is True
    assert _in("us_equity", _ts(2026, 7, 1, 20, 30)) is False
    # 冬令时:16:00 ET = 21:00 UTC
    assert _in("us_equity", _ts(2026, 11, 18, 20, 30)) is True
    assert _in("us_equity", _ts(2026, 11, 18, 21, 30)) is False


def test_us_equity_holiday_and_half_day():
    assert _in("us_equity", _ts(2026, 11, 26, 15, 0)) is False        # 感恩节
    assert _in("us_equity", _ts(2026, 11, 27, 17, 30)) is True        # 半日市 12:30 ET
    assert _in("us_equity", _ts(2026, 11, 27, 18, 30)) is False       # 13:30 ET 已收
    assert _in("us_equity", _ts(2027, 12, 24, 15, 0)) is False        # 2027 圣诞观察日


def test_futures_globex_skeleton_and_daily_break():
    assert _in("metal_futures", _ts(2026, 11, 22, 22, 30)) is False    # 周日 17:30 ET 未开
    assert _in("metal_futures", _ts(2026, 11, 22, 23, 30)) is True     # 周日 18:30 ET 已开
    assert _in("metal_futures", _ts(2026, 11, 18, 22, 30)) is False    # 每日 17:00-18:00 ET 维护
    assert _in("metal_futures", _ts(2026, 11, 20, 21, 0)) is True      # 周五 16:00 ET
    assert _in("metal_futures", _ts(2026, 11, 20, 22, 30)) is False    # 周五 17:30 ET 已收


def test_fx_is_sunday_open_and_friday_close():
    assert _in("fx", _ts(2026, 11, 22, 22, 30)) is True
    assert _in("fx", _ts(2026, 11, 20, 21, 0)) is True
    assert _in("fx", _ts(2026, 11, 20, 22, 0)) is False


def test_asia_calendar_windows():
    # 港交所(含午休)
    assert _in("hk_equity", _ts(2026, 11, 18, 3, 30)) is True
    assert _in("hk_equity", _ts(2026, 11, 18, 4, 0)) is False
    assert _in("hk_equity", _ts(2026, 11, 18, 5, 0)) is True
    assert _in("hk_equity", _ts(2026, 11, 18, 8, 0)) is False
    # 台交所
    assert _in("tw_equity", _ts(2026, 11, 18, 3, 0)) is True
    assert _in("tw_equity", _ts(2026, 11, 18, 5, 30)) is False
    # 东证(午休) / 韩交所
    assert _in("jp_equity", _ts(2026, 11, 18, 2, 0)) is True
    assert _in("jp_equity", _ts(2026, 11, 18, 3, 0)) is False
    assert _in("kr_equity", _ts(2026, 11, 18, 2, 0)) is True
    assert _in("kr_equity", _ts(2026, 11, 18, 7, 0)) is False


def test_unassigned_symbols_fall_back_to_weekday_only():
    """未登记标的:周末关闭、工作日放行,并在 describe 里标 source=default。"""
    sym = "ZZZUSDT"
    assert S.calendar_for(sym, CFG, overrides=OVR) == "weekday_only"
    assert _open(sym, _ts(2026, 11, 22, 10, 0)) is False
    assert _open(sym, _ts(2026, 11, 18, 3, 0)) is True
    row = S.describe(sym, _ts(2026, 11, 18, 3, 0), cfg=CFG, overrides=OVR)
    assert row["source"] == "default" and row["calendar"] == "weekday_only"


def test_24x7_tokens_and_crypto_are_never_gated():
    assert rwa_session_entry_block(False, _ts(2026, 11, 22, 10, 0),
                                   symbol="BTCUSDT") is None


def test_dead_bar_and_closed_reason_codes():
    ts = _ts(2026, 11, 18, 15, 0)                                    # 周三 10:00 ET
    flat = (ts, 1.0, 1.0, 1.0, 1.0, 0.0)
    active = (ts, 1.0, 1.1, 0.9, 1.0, 10.0)
    assert S.block_reason(True, "NVDAUSDT", ts, flat,
                          cfg=CFG, overrides=OVR) == "RWA_INACTIVE_SESSION"
    assert S.block_reason(True, "NVDAUSDT", ts, active,
                          cfg=CFG, overrides=OVR) is None
    assert S.block_reason(True, "NVDAUSDT", _ts(2026, 11, 22, 10, 0),
                          cfg=CFG, overrides=OVR) == "RWA_MARKET_CLOSED"


def test_overrides_can_close_a_day_without_touching_code():
    ovr = {"assignments": {"NVDAUSDT": "us_equity"},
           "closed": {"us_equity": ["2026-12-28"]}, "early_closes": {}}
    assert S.in_session("us_equity", _ts(2026, 12, 28, 15, 0), CFG, overrides=ovr) is False
    assert _open("NVDAUSDT", _ts(2026, 12, 28, 15, 0)) is True


def test_resolution_priority_override_pattern_learned_default():
    """优先级:运维覆盖 > 命名族 > 自动推断(需显式开启) > 兜底。"""
    learned = {"NVDAUSDT": "us_equity", "EURUSDUSDT": "weekday_only",
               "TENCENTUSDT": "asia_equity"}
    cfg = dict(CFG, learned_assignments=True,
               patterns=[{"pattern": "^[A-Z]{3}[A-Z]{3}USDT$", "calendar": "fx"}])
    ovr_empty = {"assignments": {}}
    assert S.calendar_for("NVDAUSDT", cfg, overrides=ovr_empty,
                          assignments=learned) == "us_equity"
    row = S.describe("NVDAUSDT", cfg=cfg, overrides=ovr_empty,
                     assignments=learned)
    assert row["source"] == "learned"
    # 命名族规则优先于自动推断
    assert S.calendar_for("EURUSDUSDT", cfg, overrides=ovr_empty,
                          assignments=learned) == "fx"
    assert S.describe("EURUSDUSDT", cfg=cfg, overrides=ovr_empty,
                      assignments=learned)["source"] == "pattern"
    # 运维显式覆盖优先于一切
    ovr = {"assignments": {"NVDAUSDT": "asia_equity"}}
    assert S.describe("NVDAUSDT", cfg=cfg, overrides=ovr,
                      assignments=learned)["source"] == "override"
    # 显式关闭 learned_assignments 时 ⇒ 自动推断不生效
    cfg_no_learned = dict(CFG, learned_assignments=False)
    assert S.describe("NVDAUSDT", cfg=cfg_no_learned, overrides=ovr_empty,
                      assignments=learned)["calendar"] == "weekday_only"
    # 默认生产配置已开启 learned_assignments ⇒ 自动推断生效
    assert bool(CFG.get("learned_assignments")) is True
    assert S.describe("NVDAUSDT", cfg=CFG, overrides=ovr_empty,
                      assignments=learned)["calendar"] == "us_equity"


def test_asia_equity_calendar_window():
    assert S.in_session("asia_equity", _ts(2026, 11, 18, 3, 0), CFG) is True
    assert S.in_session("asia_equity", _ts(2026, 11, 18, 11, 0), CFG) is False
    assert S.in_session("asia_equity", _ts(2026, 11, 21, 3, 0), CFG) is False


def test_cn_and_eu_equity_calendar_windows():
    # 沪深 A 股 09:30-11:30 / 13:00-15:00 CST (UTC+8)
    assert _in("cn_equity", _ts(2026, 11, 18, 2, 0)) is True      # 10:00 CST
    assert _in("cn_equity", _ts(2026, 11, 18, 4, 0)) is False     # 12:00 CST 午休
    assert _in("cn_equity", _ts(2026, 11, 18, 5, 30)) is True     # 13:30 CST
    assert _in("cn_equity", _ts(2026, 11, 18, 8, 0)) is False     # 16:00 CST 已收
    # 欧股 09:00-17:30 CET (冬令时 UTC+1)
    assert _in("eu_equity", _ts(2026, 11, 18, 9, 0)) is True      # 10:00 CET
    assert _in("eu_equity", _ts(2026, 11, 18, 17, 0)) is False    # 18:00 CET 已收


def test_rwa_structural_patterns_and_unassigned_fallback():
    """生产口径:命名族(HKD/FX)即刻生效,其余经 learned_assignments 或 weekday_only 兜底。"""
    assert S.calendar_for("TENCENTHKDUSDT", CFG, overrides={}, assignments={}) == "hk_equity"
    assert S.calendar_for("EURUSDUSDT", CFG, overrides={}, assignments={}) == "fx"
    assert S.calendar_for("UNKNOWNRWAUSDT", CFG, overrides={}, assignments={}) == "weekday_only"
    assert _in("weekday_only", _ts(2026, 11, 22, 10, 0)) is False      # 周六
    assert _in("weekday_only", _ts(2026, 11, 18, 3, 0)) is True        # 周三
    assert rwa_session_entry_block(False, _ts(2026, 11, 22, 10, 0),
                                   symbol="BTCUSDT") is None


def test_quiet_window_blocks_collapsed_activity():
    """同一(星期×小时)的近期活动塌陷 ⇒ 死水(自动覆盖假日/半日市/每日维护)。"""
    ts = _ts(2026, 11, 18, 15, 0)                    # 周三 15:00 UTC
    hist = [(ts - k * 7 * 86_400_000, 1.0, 1.1, 0.9, 1.0, 10.0) for k in range(1, 7)]
    quiet = (ts, 1.0, 1.01, 0.99, 1.0, 10.0)           # range 仅为平常的 ~20%
    normal = (ts, 1.0, 1.1, 0.9, 1.0, 10.0)
    assert S.block_reason(True, "ZZZUSDT", ts, quiet, history=hist,
                          cfg=CFG, overrides=OVR) == "RWA_INACTIVE_SESSION"
    assert S.block_reason(True, "ZZZUSDT", ts, normal, history=hist,
                          cfg=CFG, overrides=OVR) is None
    # 样本不足 ⇒ 不猜
    assert S.block_reason(True, "ZZZUSDT", ts, quiet, history=hist[:2],
                          cfg=CFG, overrides=OVR) is None


def test_24x7_override_bypasses_all_rwa_gates():
    """运维覆盖 mode=24x7:周末、零成交、静默窗口一律放行,等同加密。"""
    ts = _ts(2026, 11, 22, 10, 0)                       # 周日
    flat = (ts, 1.0, 1.0, 1.0, 1.0, 0.0)
    hist = [(ts - k * 7 * 86_400_000, 1.0, 1.1, 0.9, 1.0, 10.0) for k in range(1, 7)]
    ovr = {"mode": "24x7"}
    assert S.block_reason(True, "ZZZUSDT", ts, flat,
                          history=hist, cfg=CFG, overrides=ovr) is None
    d = S.describe("ZZZUSDT", ts, is_rwa=True, cfg=CFG, overrides=ovr)
    assert d["calendar"] == "crypto" and d["source"] == "override-24x7"
    assert d["in_session"] is True


def test_config_validation_fails_closed():
    with pytest.raises(ValueError):
        S._validate({"calendars": {"x": {"always_open": True, "weekday_only": True}}})
    with pytest.raises(ValueError):
        S._validate({"calendars": {"x": {"tz": "No/Such_Zone", "windows": []}}})
    with pytest.raises(ValueError):
        S._validate({"calendars": {"x": {"always_open": True}},
                     "patterns": [{"pattern": "x", "calendar": "missing"}]})


def test_production_code_keeps_no_hardcoded_dates_or_symbols():
    """生产代码与仓库配置里不得出现固定日期或具体合约代码(只允许命名族规则)。"""
    src = (ROOT / "src" / "market_operator" / "sessions.py").read_text(encoding="utf-8")
    cfg_text = (ROOT / "config" / "sessions.json").read_text(encoding="utf-8")
    for text in (src, cfg_text):
        assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text), "不得写死年月日"
        for ticker in ("NVDAUSDT", "TSLAUSDT", "AAPLUSDT", "XAUUSDT", "CLUSDT"):
            assert ticker not in text, f"不得写死具体合约 {ticker}"


def test_learn_sessions_price_cross_verification_prevents_ticker_collisions():
    """价格交叉核验必须拦截同名撞车(如油价 84.5 与消费股 95+、或误用未换算外币价)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("learn_sessions", ROOT / "scripts" / "learn_sessions.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    fx = {"USD": 1.0, "HKD": 0.128, "CNY": 0.15}
    # 1. 原油期货 $84.5 命中 CL=F ($84.54),拒绝同名消费股 ($98.0)
    ok_fut, diff_fut, _ = mod.price_matches_in_quote_ccy(
        "CLUSDT", 84.54, {"regularMarketPrice": 84.50, "currency": "USD"}, fx)
    ok_col, _, _ = mod.price_matches_in_quote_ccy(
        "CLUSDT", 84.54, {"regularMarketPrice": 98.0, "currency": "USD"}, fx)
    assert ok_fut is True and diff_fut < 0.01
    assert ok_col is False
    # 2. *USDT 合约严格按 USD 汇率换算,绝不用原始 HKD/CNY 数值硬匹配
    ok_raw_hkd, _, _ = mod.price_matches_in_quote_ccy(
        "SAMPLEUSDT", 66.0, {"regularMarketPrice": 66.0, "currency": "HKD"}, fx)
    ok_conv_cny, _, _ = mod.price_matches_in_quote_ccy(
        "SAMPLEUSDT", 64.0, {"regularMarketPrice": 425.0, "currency": "CNY"}, fx)
    assert ok_raw_hkd is False
    assert ok_conv_cny is True
    # 3. 包装衍生品识别与实体名解包
    assert mod._is_derivative_wrapper({"shortName": "Natural Gas (Derivatives) USD"}) is True
    assert mod._is_derivative_wrapper({"shortName": "PAX Gold USD"}) is False
    assert mod._unwrap_entity_name("Natural Gas (Derivatives) USD") == "Natural Gas"

