"""v12 测试(docs/v12-protocol.md §1-§3,冻结):K线级订单流特征与判别统计。

全部离线(合成序列 / 合成 zip 字节;零网络)。覆盖:
- 流特征数值(delta_vol/delta_share/cvd_24/cvd_48/delta_z 手工重算恒等);
- 因果性(无未来):截断前缀上的特征与全长序列逐值恒等;
- 三瓜分切片:容量、确定性平局裁决(ts)、病态 n 行为;
- 判别 CI:可分离构造下 CI 不含 0、同分布构造下 CI 含 0、
  固定种子逐位决定论;
- 抓取层纯函数:vision zip(含/不含表头)解析、taker 域校验、
  微秒 open_time 防御归一。
"""

from __future__ import annotations

import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from market_operator import flow_features as ff
from market_operator.flow_study import (
    MIN_TERCILE_N,
    tercile_diff_ci,
    tercile_labels,
)
from scripts import fetch_binance_klines as fb


# ---------------------------------------------------------------------------
# 特征数值
# ---------------------------------------------------------------------------


def test_delta_vol_and_share_numeric():
    tb = np.array([10.0, 30.0, 0.0, 50.0])
    v = np.array([40.0, 40.0, 20.0, 100.0])
    dv = ff.delta_vol(tb, v)
    assert np.allclose(dv, [-20.0, 20.0, -20.0, 0.0])       # 2·tb − v
    ds = ff.delta_share(tb, v)
    assert np.allclose(ds, [-0.5, 0.5, -1.0, 0.0])
    assert np.all(np.abs(ds) <= 1.0)


def test_delta_share_zero_volume_guard():
    ds = ff.delta_share(np.array([0.0]), np.array([0.0]))   # 停盘 bar(实存特例)
    assert ds[0] == 0.0 and np.isfinite(ds[0])


def test_cvd_numeric_rolling_mean():
    tb = np.array([6.0, 8.0, 10.0, 12.0, 14.0])
    v = np.full(5, 20.0)                                    # dv = [-8,-4,0,4,8]
    cvd2 = ff.cvd(tb, v, 2)
    # cumsum = [-8,-12,-12,-8,0];近2根均值(首根扩张窗)
    assert np.allclose(cvd2, [-8.0, -10.0, -12.0, -10.0, -4.0])
    cvd3 = ff.cvd(tb, v, 3)
    assert np.allclose(cvd3, [-8.0, -10.0, -32 / 3, -32 / 3, -20 / 3])


def test_delta_z_matches_manual_rolling():
    rng = np.random.default_rng(7)
    v = rng.uniform(50.0, 500.0, size=300)
    share_in = rng.uniform(0.35, 0.65, size=300)
    tb = (share_in + 1.0) * 0.5 * v
    got = ff.delta_z(tb, v, window=48)
    s = pd.Series(ff.delta_share(tb, v))
    want = (s - s.rolling(48, min_periods=1).mean()) \
        / s.rolling(48, min_periods=1).std(ddof=0).replace(0.0, np.nan)
    want = want.fillna(0.0).to_numpy()
    assert np.allclose(got, want, atol=1e-12)


def test_attach_columns_and_invariants():
    n = 120
    rng = np.random.default_rng(11)
    v = rng.uniform(100.0, 900.0, size=n)
    tb = v * rng.uniform(0.4, 0.6, size=n)
    df = pd.DataFrame({"ts": np.arange(n) * 3_600_000,
                       "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                       "volume": v, "taker_buy_base_volume": tb})
    out = ff.attach(df)
    for col in ("delta_vol", "delta_share", "cvd_24", "cvd_48", "delta_z"):
        assert col in out.columns and np.all(np.isfinite(out[col]))
    assert np.allclose(out["delta_share"], (2 * tb - v) / v)
    assert np.all(np.abs(out["delta_share"]) <= 1.0 + 1e-12)


# ---------------------------------------------------------------------------
# 因果性(无未来):前缀截断恒等
# ---------------------------------------------------------------------------


def test_features_causal_prefix_invariant():
    n = 200
    rng = np.random.default_rng(3)
    v = rng.uniform(100.0, 900.0, size=n)
    tb = v * rng.uniform(0.2, 0.8, size=n)
    full = {"dv": ff.delta_vol(tb, v), "ds": ff.delta_share(tb, v),
            "z": ff.delta_z(tb, v)}
    for w in (24, 48):
        full[f"cvd{w}"] = ff.cvd(tb, v, w)
    for cut in (50, 120, 199):
        assert np.allclose(full["dv"][:cut], ff.delta_vol(tb[:cut], v[:cut]))
        assert np.allclose(full["ds"][:cut], ff.delta_share(tb[:cut], v[:cut]))
        assert np.allclose(full["z"][:cut], ff.delta_z(tb[:cut], v[:cut]))
        for w in (24, 48):
            assert np.allclose(full[f"cvd{w}"][:cut], ff.cvd(tb[:cut], v[:cut], w))
    # 反向证据:扰动未来段,历史特征不得改变
    tb2, v2 = tb.copy(), v.copy()
    tb2[150:] *= 2.0
    v2[150:] *= 3.0
    assert np.allclose(full["z"][:150], ff.delta_z(tb2, v2)[:150])
    assert np.allclose(full["cvd24"][:150], ff.cvd(tb2, v2, 24)[:150])


# ---------------------------------------------------------------------------
# 三瓜分切片
# ---------------------------------------------------------------------------


def test_tercile_labels_sizes_and_members():
    z = np.array([5.0, -3.0, 2.0, 0.0, 4.0, -1.0, 3.0, 1.0, -2.0])
    ts = np.array([100, 300, 200, 400, 500, 600, 700, 800, 900], dtype=np.int64)
    lab = tercile_labels(z, ts)
    assert (lab == 0).sum() == 3 and (lab == 2).sum() == 3 and (lab == 1).sum() == 3
    order = np.argsort(z, kind="stable")
    assert (lab[order[:3]] == 0).all()          # 最低 3 件 = z 最小三
    assert (lab[order[-2:]] == 2).all()
    assert lab[np.argmax(z)] == 2 and lab[np.argmin(z)] == 0


def test_tercile_labels_tie_break_by_ts_deterministic():
    z = np.full(9, 0.5)                         # 全平局 ⇒ 秩完全由 ts 定
    ts = np.array([900, 100, 800, 200, 700, 300, 600, 400, 500], dtype=np.int64)
    lab = tercile_labels(z, ts)
    order = np.argsort(ts, kind="stable")
    assert (lab[order[:3]] == 0).all()
    assert (lab[order[3:6]] == 1).all()
    assert (lab[order[6:]] == 2).all()
    assert (tercile_labels(z, ts) == lab).all()  # 重跑逐位恒等


def test_tercile_labels_tiny_n_pathology():
    lab = tercile_labels(np.array([1.0, -1.0]), np.array([1, 2]))
    assert (lab == 1).all()                     # n<3:无 top/bottom,由调用方闸 MIN_TERCILE_N
    assert MIN_TERCILE_N >= 6                   # 病态闸 ≥ 每分位 2 件


# ---------------------------------------------------------------------------
# 判别 CI(日块 bootstrap,固定种子,2000 次用粗 reps 加速测试)
# ---------------------------------------------------------------------------


def test_tercile_diff_ci_separated_excludes_zero():
    # 两组日块内高度一致、组间可分离 ⇒ CI 必不含 0
    days_top = np.repeat(np.arange(30), 2) * 86_400_000
    days_bot = days_top.copy()
    top_incr = np.tile(np.array([0.9, 1.1]), 30)
    bot_incr = np.tile(np.array([-0.1, 0.1]), 30)
    diff, lo, hi = tercile_diff_ci(top_incr, days_top, bot_incr, days_bot,
                                   reps=500, seed=123)
    assert diff == pytest.approx(1.0, abs=0.06)
    assert lo > 0.0 < hi


def test_tercile_diff_ci_identical_distribution_contains_zero():
    rng = np.random.default_rng(5)
    days = np.repeat(np.arange(40), 2) * 86_400_000
    top_incr = rng.normal(0.0, 1.0, size=80)
    bot_incr = rng.normal(0.0, 1.0, size=80)
    diff, lo, hi = tercile_diff_ci(top_incr, days, bot_incr, days,
                                   reps=500, seed=321)
    assert lo <= diff and diff <= hi
    assert lo < 0.0 < hi                        # 同分布构造 ⇒ CI 必含 0


def test_tercile_diff_ci_deterministic_bitwise():
    rng = np.random.default_rng(9)
    ts = np.repeat(np.arange(25), 3) * 86_400_000
    top_incr = rng.normal(0.4, 1.0, size=75)
    bot_incr = rng.normal(-0.2, 1.0, size=75)
    a = tercile_diff_ci(top_incr, ts, bot_incr, ts, reps=500, seed=42)
    b = tercile_diff_ci(top_incr, ts, bot_incr, ts, reps=500, seed=42)
    assert a == b                               # 逐位决定论(固定种子)
    c = tercile_diff_ci(top_incr, ts, bot_incr, ts, reps=500, seed=43)
    assert a[1:] != c[1:]                       # 种子生效(非常量假性确定)


def test_tercile_diff_ci_empty_group_nan():
    diff, lo, hi = tercile_diff_ci(np.array([1.0]), np.array([0]),
                                   np.array([]), np.array([]), reps=100)
    assert all(x != x for x in (diff, lo, hi))  # 空组 ⇒ 全 NaN


# ---------------------------------------------------------------------------
# 抓取层纯函数(离线,合成 zip 字节)
# ---------------------------------------------------------------------------


def _make_zip(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BTCUSDT-1h-2025-01.csv", csv_text)
    return buf.getvalue()


_ROW = "1735689600000,100.0,110.0,90.0,105.0,1000.0,1735693199999,105000.0,500,600.0,63000.0,0"
_ROW2 = "1735693200000,105.0,108.0,95.0,102.0,800.0,1735696799999,81600.0,400,300.0,30600.0,0"


def test_zip_to_frame_headerless():
    df = fb._zip_to_frame(_make_zip(_ROW + "\n" + _ROW2 + "\n"))
    assert list(df.columns) == fb.COLUMNS
    assert len(df) == 2 and df["ts"].iloc[0] == 1735689600000
    assert df["taker_buy_base_volume"].iloc[0] == 600.0      # 主动买基币量
    assert df["volume"].iloc[1] == 800.0


def test_zip_to_frame_with_header():
    head = ("open_time,open,high,low,close,volume,close_time,quote_volume,"
            "count,taker_buy_volume,taker_buy_quote_volume,ignore\n")
    df = fb._zip_to_frame(_make_zip(head + _ROW + "\n"))
    assert len(df) == 1 and df["close"].iloc[0] == 105.0


def test_zip_to_frame_microsecond_open_time():
    us_row = _ROW.replace("1735689600000", "1735689600000000").replace(
        "1735693199999", "1735693199999000")
    df = fb._zip_to_frame(_make_zip(us_row + "\n"))
    assert df["ts"].iloc[0] == 1735689600000                 # μs → ms 归一


def test_validate_taker_domain():
    df = fb._zip_to_frame(_make_zip(_ROW + "\n" + _ROW2 + "\n"))
    assert fb.validate(df) == []
    bad = df.copy()
    bad.loc[0, "taker_buy_base_volume"] = 1001.0             # tb > volume
    assert any("taker_buy out of" in s for s in fb.validate(bad))
    bad2 = df.copy()
    bad2.loc[1, "volume"] = 0.0                              # 非正量(停盘 bar 侦测同规)
    assert any("non-positive volume" in s for s in fb.validate(bad2))


def test_validate_gap_and_dup_same_as_core():
    df = fb._zip_to_frame(_make_zip(_ROW + "\n" + _ROW + "\n" + _ROW2 + "\n"))
    issues = fb.validate(df)
    assert any("duplicate ts" in s for s in issues)
    row_gap = _ROW2.replace("1735693200000", "1735700400000")  # +3h ⇒ 缺 2 根
    gap = fb._zip_to_frame(_make_zip(_ROW + "\n" + row_gap + "\n"))
    assert any("gap:" in s for s in fb.validate(gap))
