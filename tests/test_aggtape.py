"""v19-orderflow aggTrades 管线离线测试(网络零接触;数学手算守护)。

覆盖(docs/v19-protocol-orderflow.md):
- 聚合数学:delta_share / cvd_slope / big_share / eff / vol15_rel 手算;
- 边界:空窗/单行/分母 0/warmup 阈值 NaN 口径;
- 下载校验:csv 解析(含/无表头、微秒归一)、validate(长度/单调/无负/
  当日域)、CHECKSUM 解析、zip 单文件契约;
- 吸收签名构造:大 vol + 小 eff ⇒ 吸收特征读数;
- 流式切片:跨日尾 61m 驻存、UTC 15m 网格、确定性。
"""

from __future__ import annotations

import io
import math
import zipfile

import numpy as np
import pandas as pd
import pytest

from market_operator import aggtape as at
from scripts import fetch_aggtrades as fa

MS = 1
MIN_MS = 60_000
DAY_MS = 86_400_000
T0 = 1_800_000_000_000          # 任意接触时刻(ms)
DAY0 = (T0 // DAY_MS) * DAY_MS  # 其所在日 00:00 UTC


def _mk(ts, price, qty, maker):
    return (np.asarray(ts, dtype=np.int64), np.asarray(price, dtype=np.float64),
            np.asarray(qty, dtype=np.float64), np.asarray(maker, dtype=bool))


# ---------------------------------------------------------------------------
# 1. 聚合数学(手算)
# ---------------------------------------------------------------------------


def test_delta_share_hand():
    # 主动买 = maker False;买 200 / 卖 200 ⇒ 0;追加买 300 ⇒ 300/700
    p, q, m = [100, 100, 100], [1, 2, 1], [False, True, False]
    assert at.delta_share(np.array(p), np.array(q), np.array(m)) == 0.0
    p.append(100), q.append(3), m.append(False)
    got = at.delta_share(np.array(p), np.array(q), np.array(m))
    assert got == pytest.approx(3.0 / 7.0)
    # 全买 +1,全卖 −1,值域 [-1,1]
    assert at.delta_share(np.array([1.0]), np.array([1.0]),
                          np.array([False])) == 1.0
    assert at.delta_share(np.array([1.0]), np.array([1.0]),
                          np.array([True])) == -1.0


def test_delta_share_empty_and_zero_denominator():
    assert math.isnan(at.delta_share(np.array([]), np.array([]), np.array([], dtype=bool)))
    assert math.isnan(at.delta_share(np.array([0.0]), np.array([1.0]),
                                     np.array([False])))


def test_big_share_hand():
    # 10 笔名义 1..10:k = ceil(0.10*10) = 1 ⇒ 10/55 = 2/11
    notion = np.arange(1, 11, dtype=np.float64)
    got = at.big_share(np.ones(10), notion)
    assert got == pytest.approx(10.0 / 55.0)
    # 20 笔:k = 2 ⇒ (20+19)/210
    notion20 = np.arange(1, 21, dtype=np.float64)
    assert at.big_share(np.ones(20), notion20) == pytest.approx(39.0 / 210.0)
    # 单笔 ⇒ 占比 1;空窗 ⇒ NaN
    assert at.big_share(np.array([100.0]), np.array([2.0])) == 1.0
    assert math.isnan(at.big_share(np.array([]), np.array([])))


def test_eff_hand():
    # |101 − 100| / (100*1 + 101*1) = 1/201
    got = at.eff(np.array([100.0, 101.0]), np.array([1.0, 1.0]))
    assert got == pytest.approx(1.0 / 201.0)
    # 单手 / 空窗 ⇒ NaN;零名义 ⇒ NaN
    assert math.isnan(at.eff(np.array([100.0]), np.array([1.0])))
    assert math.isnan(at.eff(np.array([]), np.array([])))
    assert math.isnan(at.eff(np.array([0.0, 0.0]), np.array([1.0, 1.0])))


def test_cvd_slope_hand():
    # 每分钟 +1 基币(全主动买):cvd = 1..60 斜率恰 1;名义/分钟 = 1 ⇒ 归一 1.0
    ts = [T0 - 3_600_000 + i * MIN_MS for i in range(60)]
    price = [1.0] * 60
    qty = [1.0] * 60
    got = at.cvd_slope(np.array(ts), np.array(price), np.array(qty),
                       np.array([False] * 60), T0 - 3_600_000)
    assert got == pytest.approx(1.0)
    # 全主动卖 ⇒ −1.0;买卖相间(分钟净额全 0)⇒ 0.0
    got_sell = at.cvd_slope(np.array(ts), np.array(price), np.array(qty),
                            np.array([True] * 60), T0 - 3_600_000)
    assert got_sell == pytest.approx(-1.0)
    ts2, q2, m2 = [], [], []
    for i in range(60):
        ts2 += [T0 - 3_600_000 + i * MIN_MS, T0 - 3_600_000 + i * MIN_MS + 1]
        q2 += [1.0, 1.0]
        m2 += [False, True]
    assert at.cvd_slope(np.array(ts2), np.array([1.0] * 120), np.array(q2),
                        np.array(m2), T0 - 3_600_000) == pytest.approx(0.0)
    assert math.isnan(at.cvd_slope(np.array([]), np.array([]), np.array([]),
                                   np.array([], dtype=bool), T0 - 3_600_000))


def test_vol15_rel_thresholds():
    prev = np.full(96, 100.0)
    assert at.vol15_rel(500.0, prev) == pytest.approx(5.0)
    # 47 桶 < VOL_MIN_BUCKETS ⇒ NaN;中位 0 ⇒ NaN;负名义 ⇒ NaN
    assert math.isnan(at.vol15_rel(500.0, np.full(47, 100.0)))
    assert math.isnan(at.vol15_rel(500.0, np.zeros(96)))
    assert math.isnan(at.vol15_rel(-1.0, prev))
    # >96 桶时只用最近 96:前 104 桶 50、末 96 桶 100 ⇒ 中位 100
    long_prev = np.concatenate([np.full(104, 50.0), np.full(96, 100.0)])
    assert at.vol15_rel(500.0, long_prev) == pytest.approx(5.0)


def test_features_at_hand_and_exclusivity():
    # 60 分钟,每分钟一笔 @100:前 30 分钟全主动卖,后 30 分钟全主动买。
    win_start = T0 - 3_600_000
    ts = [win_start + i * MIN_MS for i in range(60)]
    maker = [True] * 30 + [False] * 30
    # t_contact 整点的一笔属于下一根 bar ⇒ 排除;t−60m 整点的一笔含。
    ts = [win_start] + ts[1:] + [T0]
    price = [100.0] * 61
    qty = [1.0] * 61
    maker = maker + [False]
    f = at.features_at(np.array(ts), np.array(price), np.array(qty),
                       np.array(maker), T0, grid=None)
    assert f["n_trades_60m"] == 60                       # t 处那笔被排他
    assert f["delta_share_60m"] == pytest.approx(0.0)    # 买卖名义各 3000
    assert f["delta_share_15m"] == pytest.approx(1.0)    # 末 15 分钟全买
    # cvd:V 形(每分钟 ±1 基币,前卖后买),斜率 = 该形的最小二乘值,
    # 归一分母 = 总名义/60(该形非零斜率,手算以 polyfit 交叉核对语义)。
    minute_delta = np.array([-1.0] * 30 + [1.0] * 30)
    cvd_shape = np.cumsum(minute_delta)
    exp_slope = float(np.polyfit(np.arange(60, dtype=np.float64), cvd_shape, 1)[0])
    assert f["cvd_slope_60m"] == pytest.approx(exp_slope / (6000.0 / 60.0))
    assert f["eff_15m"] == pytest.approx(0.0)            # 价格不动
    assert f["big_share_15m"] == pytest.approx(2.0 / 15.0)  # k=ceil(1.5)=2
    assert f["notional_15m"] == pytest.approx(1500.0)
    assert math.isnan(f["vol15_rel"])                    # grid=None ⇒ NaN


# ---------------------------------------------------------------------------
# 2. 吸收签名构造(大 vol + 小 eff ⇒ 吸收读数)
# ---------------------------------------------------------------------------


def test_absorption_signature_construction():
    # 吸收窗:200 笔,价格在 100/100.01 间抖动,每笔 qty 50 ⇒ 大名义,
    # 首尾几乎同价 ⇒ eff 极小;vol15_rel 相对过去中位极高。
    p_abs = np.tile([100.00, 100.01], 100)
    q_abs = np.full(200, 50.0)
    eff_abs = at.eff(p_abs, q_abs)
    vol_abs = at.vol15_rel(float((p_abs * q_abs).sum()), np.full(96, 100.0))
    # 平静方向窗:10 笔,价格 100→101,qty 1 ⇒ eff 大四个量级,vol 低。
    p_dir = np.linspace(100.0, 101.0, 10)
    q_dir = np.ones(10)
    eff_dir = at.eff(p_dir, q_dir)
    vol_dir = at.vol15_rel(float((p_dir * q_dir).sum()), np.full(96, 100.0))
    assert eff_abs < eff_dir / 1000
    assert vol_abs > 1000 > vol_dir
    # 签名判定的原始读数:absorption = high vol15_rel AND low eff
    assert eff_abs < 1e-8 and vol_abs > 1e4


# ---------------------------------------------------------------------------
# 3. 流式切片(跨日尾 61m 与 UTC 15m 网格;单日驻存)
# ---------------------------------------------------------------------------


def _write_day(path, ts, price, qty, maker):
    pd.DataFrame({"ts_ms": ts, "price": price, "qty": qty,
                  "is_buyer_maker": maker}).to_csv(
        path, index=False, compression="gzip")


def test_stream_features_cross_day_tail(tmp_path):
    # day1 23:30..23:59 全卖 @100 qty1;day2 00:00..00:09 全买 @100 qty1。
    d1, d2 = tmp_path / "d1.csv.gz", tmp_path / "d2.csv.gz"
    day2_start = DAY0 + DAY_MS
    ts1 = [DAY0 + 23 * 3_600_000 + 30 * MIN_MS + i * MIN_MS for i in range(30)]
    ts2 = [day2_start + i * MIN_MS for i in range(10)]
    _write_day(d1, ts1, [100.0] * 30, [1.0] * 30, [True] * 30)
    _write_day(d2, ts2, [100.0] * 10, [1.0] * 10, [False] * 10)
    t = day2_start + 10 * MIN_MS                  # 00:10
    out = at.stream_features([d1, d2], [t])
    f = out.iloc[0]
    assert f["n_trades_60m"] == 40                 # 30(前一日尾) + 10(当日)
    # [t−15m, t) = 23:55..00:10:卖 5(前一日尾)+ 买 10 ⇒ (1000−500)/1500
    assert f["n_trades_15m"] == 15
    assert f["delta_share_15m"] == pytest.approx(500.0 / 1500.0)
    assert f["delta_share_60m"] == pytest.approx((1000 - 3000) / 4000)


def test_notional_grid_and_vol15(tmp_path):
    d1, d2 = tmp_path / "d1.csv.gz", tmp_path / "d2.csv.gz"
    day2_start = DAY0 + DAY_MS
    q15 = 15 * MIN_MS
    # day1:桶 [23:30,23:45) 15 笔 qty1 @100 ⇒ 1500;[23:45,24:00) 15 笔 ⇒ 1500
    # day2:桶 [00:00,00:15) 10 笔 ⇒ 1000
    _write_day(d1, [DAY0 + 23 * 3_600_000 + 30 * MIN_MS + i * MIN_MS
                    for i in range(30)], [100.0] * 30, [1.0] * 30, [True] * 30)
    _write_day(d2, [day2_start + i * MIN_MS for i in range(10)],
               [100.0] * 10, [1.0] * 10, [False] * 10)
    grid, g0 = at.notional_grid([d1, d2], grid_start_ms=DAY0)
    b2330 = (23 * 3_600_000 + 30 * MIN_MS) // q15
    assert grid[b2330] == pytest.approx(1500.0)
    assert grid[b2330 + 1] == pytest.approx(1500.0)
    assert grid[(DAY_MS) // q15] == pytest.approx(1000.0)
    # 过去桶不足 48 ⇒ vol15_rel NaN(warmup 如实)
    f = at.features_at(*at.load_day(d2), day2_start + 10 * MIN_MS,
                       grid=grid, grid_start_ms=g0)
    assert math.isnan(f["vol15_rel"])


# ---------------------------------------------------------------------------
# 4. 下载校验(零网络;_csv_bytes_to_frame / validate / CHECKSUM / zip)
# ---------------------------------------------------------------------------


CSV_HEAD = ("agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "101,82077.9,2.4,11,12,1792368000004,true\n"
            "102,82078.0,0.5,13,13,1792368000013,false\n")
CSV_NOHEAD = ("101,82077.9,2.4,11,12,1792368000004,true\n"
              "102,82078.0,0.5,13,13,1792368000013,false\n")


def test_csv_parse_header_and_no_header_and_microseconds():
    for content in (CSV_HEAD, CSV_NOHEAD):
        df = fa._csv_bytes_to_frame(content.encode())
        assert list(df.columns) == fa.COLUMNS
        assert len(df) == 2
        assert df["ts_ms"].tolist() == [1792368000004, 1792368000013]
        assert df["is_buyer_maker"].tolist() == [True, False]
    # 微秒档案归一(×1000 ⇒ ≥1e14)
    us = CSV_HEAD.replace("1792368000004", "1792368000004000").replace(
        "1792368000013", "1792368000013000")
    df = fa._csv_bytes_to_frame(us.encode())
    assert df["ts_ms"].tolist() == [1792368000004, 1792368000013]


def test_validate_length_monotone_no_negative_day_domain():
    day_start = 1792368000000
    good = pd.DataFrame({"ts_ms": [day_start, day_start + 5, day_start + 9],
                         "price": [1.0, 1.0, 2.0], "qty": [1.0, 2.0, 1.0],
                         "is_buyer_maker": [True, False, False]})
    assert fa.validate(good, day_start) == []
    bad_mono = good.copy()
    bad_mono.loc[2, "ts_ms"] = day_start + 1        # 回退 ⇒ 递减
    assert any("decreasing" in s for s in fa.validate(bad_mono, day_start))
    bad_price = good.copy()
    bad_price.loc[0, "price"] = 0.0
    assert any("price" in s for s in fa.validate(bad_price, day_start))
    bad_qty = good.copy()
    bad_qty.loc[1, "qty"] = -0.5
    assert any("qty" in s for s in fa.validate(bad_qty, day_start))
    bad_day = good.copy()
    bad_day.loc[2, "ts_ms"] = day_start + DAY_MS    # 次日 ⇒ 出当日域
    assert any("outside" in s for s in fa.validate(bad_day, day_start))
    assert fa.validate(good.iloc[0:0], day_start) == ["empty frame"]


def test_parse_checksum():
    sha = "176138a405e38ca9816f179ff8f21ab3c7f2776ed15b961b4062efd5ae0138b0"
    assert fa.parse_checksum(f"{sha}  BTCUSDT-aggTrades-2026-10-19.zip\n") == sha
    with pytest.raises(RuntimeError):
        fa.parse_checksum("not-a-sha filename.zip")
    with pytest.raises(RuntimeError):
        fa.parse_checksum("")


def test_zip_to_frame_single_csv_contract():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("X-aggTrades-2026-10-19.csv", CSV_HEAD)
    df = fa._zip_to_frame(buf.getvalue())
    assert len(df) == 2 and list(df.columns) == fa.COLUMNS
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("a.csv", CSV_HEAD)
        zf.writestr("b.csv", CSV_HEAD)
    with pytest.raises(RuntimeError):
        fa._zip_to_frame(buf2.getvalue())


# ---------------------------------------------------------------------------
# 5. 确定性(同输入逐位同输出)
# ---------------------------------------------------------------------------


def test_stream_features_determinism(tmp_path):
    rng = np.random.default_rng(7)
    day2_start = DAY0 + DAY_MS
    paths = []
    for d, base in ((tmp_path / "d1.csv.gz", DAY0),
                    (tmp_path / "d2.csv.gz", day2_start)):
        n = 500
        ts = np.sort(rng.integers(base, base + DAY_MS, size=n)).astype(np.int64)
        price = 100.0 + rng.normal(0, 1, n).cumsum() * 0.01
        qty = rng.uniform(0.1, 2.0, n)
        maker = rng.integers(0, 2, n).astype(bool)
        _write_day(d, ts, price, qty, maker)
        paths.append(d)
    grid, g0 = at.notional_grid(paths, grid_start_ms=DAY0)
    reqs = [day2_start + 10 * MIN_MS, day2_start + 3_000_000,
            day2_start + 20 * 3_600_000]
    kwargs = {"grid": grid, "grid_start_ms": g0}
    a = at.stream_features(paths, reqs, **kwargs)
    b = at.stream_features(paths, list(reversed(reqs)), **kwargs)
    a = a.set_index("t_contact").sort_index()
    b = b.set_index("t_contact").sort_index()
    pd.testing.assert_frame_equal(a, b)            # 请求序无关,逐位相等
