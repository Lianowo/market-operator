"""v3 观察尺度聚合测试(docs/v3-protocol.md §1/§2,tests/test_aggregate.py)。

覆盖任务书五项 + 一处已确认上游修复的锁定测试:
1. 聚合数学正确性(手算可核对小样例,含不完整首桶);
2. ts 边界(2h 偶数小时对齐、12h/1d UTC 零点网格对齐);
3. 聚合后 mo_data.validate(step_ms) 零缺口断言(真实三品种 × 4 TF);
4. 与 1h 首尾总量守恒(Σvolume、首 open、末 close;合成 + 真实);
5. manifest 追加往返(tmp 树:sha/行数/首末 ts/来源;重跑字节幂等);
dev/test 时间拐点(协议 §2):12320 号 1h bar 的 ts 落在每个 TF 帧
split_boundary 索引桶内,逐 TF 核算一致(硬编码期望索引核算);
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from market_operator import aggregate as agg
from market_operator import data as mo_data
from market_operator.split import split_boundary

MS_H = 3_600_000
T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC(整网格)

REAL_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TF_LABELS = ("2h", "4h", "12h", "1d")
# 协议 §2 拐点核算(1h n=17600,dev_frac=0.7 ⇒ boundary=12320;聚合桶对齐后
# 逐 TF 期望帧长与边界索引,见 docs/v3-protocol.md §2 与 tests 注释)
EXPECTED_N_TF = {"2h": 8800, "4h": 4400, "12h": 1468, "1d": 735}
EXPECTED_BOUNDARY = {"2h": 6160, "4h": 3080, "12h": 1027, "1d": 514}
BOUNDARY_1H_INDEX = 12320
N_1H = 17600


def _df(rows: list) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=mo_data.COLUMNS)


def _sample_5bars() -> pd.DataFrame:
    """5 根 1h bar,起于奇数小时 01:00(2h 首桶不完整,12h/1d 同日)。"""
    rows = [
        # (hour_offset, open, high, low, close, volume)
        (1, 10.0, 12.0, 9.0, 11.0, 100.0),
        (2, 11.0, 15.0, 10.0, 14.0, 200.0),
        (3, 14.0, 16.0, 13.0, 15.0, 300.0),
        (4, 15.0, 17.0, 14.0, 16.0, 400.0),
        (5, 16.0, 18.0, 12.0, 13.0, 500.0),
    ]
    return _df([(T0 + h * MS_H, o, hh, l, c, v) for h, o, hh, l, c, v in rows])


# ---------------------------------------------------------------------------
# 1. 聚合数学正确性(手算)
# ---------------------------------------------------------------------------


def test_aggregate_2h_hand_computed_partial_first_bucket():
    out = agg.aggregate(_sample_5bars(), 2)
    expected = _df([
        (T0 + 0 * MS_H, 10.0, 12.0, 9.0, 11.0, 100.0),   # 桶[00,02): 仅 01:00
        (T0 + 2 * MS_H, 11.0, 16.0, 10.0, 15.0, 500.0),  # 桶[02,04): 02+03
        (T0 + 4 * MS_H, 15.0, 18.0, 12.0, 13.0, 900.0),  # 桶[04,06): 04+05
    ])
    pd.testing.assert_frame_equal(
        out.reset_index(drop=True), expected, check_dtype=True)


def test_aggregate_1d_hand_computed_single_bucket():
    out = agg.aggregate(_sample_5bars(), 24)
    expected = _df([(T0, 10.0, 18.0, 9.0, 13.0, 1500.0)])
    pd.testing.assert_frame_equal(out, expected, check_dtype=True)


def test_aggregate_4h_hand_computed():
    out = agg.aggregate(_sample_5bars(), 4)
    expected = _df([
        (T0 + 0 * MS_H, 10.0, 16.0, 9.0, 15.0, 600.0),   # 01+02+03
        (T0 + 4 * MS_H, 15.0, 18.0, 12.0, 13.0, 900.0),  # 04+05
    ])
    pd.testing.assert_frame_equal(out, expected, check_dtype=True)


def test_aggregate_rejects_bad_tf_hours():
    with pytest.raises(ValueError):
        agg.aggregate(_sample_5bars(), 7)


# ---------------------------------------------------------------------------
# 2. ts 边界(合成数据:2h 偶数小时、12h/1d UTC 零点网格)
# ---------------------------------------------------------------------------


def test_bucket_ts_alignment_grids():
    d = _sample_5bars()
    for hours, mod in ((2, 2), (4, 4), (12, 12), (24, 24)):
        out = agg.aggregate(d, hours)
        assert (out["ts"] % (mod * MS_H) == 0).all(), f"{hours}h grid misaligned"
        diffs = out["ts"].diff().dropna()
        assert (diffs == hours * MS_H).all(), f"{hours}h spacing irregular"


def test_1d_bars_start_at_utc_midnight():
    out = agg.aggregate(_sample_5bars(), 24)
    assert out["ts"].iloc[0] == T0          # 恰为 UTC 零点


# ---------------------------------------------------------------------------
# 3/4. 真实数据:零缺口 + 守恒(内存聚合,不写盘)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sym", REAL_SYMBOLS)
@pytest.mark.parametrize("tf", TF_LABELS)
def test_real_aggregate_zero_gap_and_conservation(sym, tf):
    hours = agg.TF_HOURS[tf]
    step = hours * MS_H
    df_1h = mo_data.load(sym)
    assert len(df_1h) == N_1H               # manifest 冻结行数
    out = agg.aggregate(df_1h, hours)

    assert len(out) == EXPECTED_N_TF[tf]
    assert mo_data.validate(out, step_ms=step) == []                # 零缺口
    assert float(out["volume"].sum()) == pytest.approx(             # 总量守恒
        float(df_1h["volume"].sum()), rel=1e-12)
    assert out["open"].iloc[0] == df_1h["open"].iloc[0]             # 首 open
    assert out["close"].iloc[-1] == df_1h["close"].iloc[-1]         # 末 close
    # OHLC 合法性由 validate 覆盖;桶覆盖关系:首桶 ts ≤ 1h 首 ts < ts+step
    assert out["ts"].iloc[0] <= df_1h["ts"].iloc[0] < out["ts"].iloc[0] + step
    assert out["ts"].iloc[-1] <= df_1h["ts"].iloc[-1] < out["ts"].iloc[-1] + step


# ---------------------------------------------------------------------------
# 协议 §2 dev/test 拐点:12320 号 1h bar ts 逐 TF 落在边界索引桶内
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sym", REAL_SYMBOLS)
@pytest.mark.parametrize("tf", TF_LABELS)
def test_dev_test_boundary_landmark_consistent_per_tf(sym, tf):
    hours = agg.TF_HOURS[tf]
    step = hours * MS_H
    df_1h = mo_data.load(sym)
    ts_landmark = int(df_1h["ts"].iloc[BOUNDARY_1H_INDEX])

    out = agg.aggregate(df_1h, hours)
    b = split_boundary(len(out), 0.7)
    assert b == EXPECTED_BOUNDARY[tf]                    # 硬编码核算值
    # 拐点 ts 的桶起点恰为边界 TF bar 的 ts(报 evidence 一致,非近似)
    assert int(out["ts"].iloc[b]) == (ts_landmark // step) * step
    assert out["ts"].iloc[b] <= ts_landmark < out["ts"].iloc[b] + step


def test_boundary_proportional_for_aligned_tfs():
    """2h/4h 与 1h 同网格:boundary·tf_hours 恰为 12320(线性核算)。"""
    for tf, b in EXPECTED_BOUNDARY.items():
        if tf in ("2h", "4h"):
            assert b * agg.TF_HOURS[tf] == BOUNDARY_1H_INDEX


# ---------------------------------------------------------------------------
# 5. manifest 追加往返(tmp 树,不触碰真实 data/)
# ---------------------------------------------------------------------------


def test_aggregate_symbol_manifest_roundtrip(tmp_path):
    data_dir = tmp_path / "data"
    raw = data_dir / "raw"
    raw.mkdir(parents=True)
    src = raw / "TESTSYM_1h.csv"
    src.write_bytes(agg._csv_bytes(_sample_5bars()))
    manifest_path = data_dir / "manifest.json"

    out_path = agg.aggregate_symbol("TESTSYM", "2h", data_dir=data_dir,
                                    manifest_path=manifest_path)
    written = out_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rec = manifest["files"]["TESTSYM_2h.csv"]

    assert out_path.exists() and rec["sha256"] == hashlib.sha256(written).hexdigest()
    assert rec["rows"] == 3                                 # 手算:3 桶
    assert rec["ts_first"] == T0 and rec["ts_last"] == T0 + 4 * MS_H
    assert "1h" in rec["source"] and "聚合" in rec["source"]
    assert rec["aggregated_from"]["file"] == "TESTSYM_1h.csv"
    assert rec["aggregated_from"]["sha256"] == hashlib.sha256(
        src.read_bytes()).hexdigest()
    assert rec["validation_issues"] == []

    payload_before = manifest_path.read_bytes()             # 幂等:重跑同字节
    csv_before = out_path.read_bytes()
    agg.aggregate_symbol("TESTSYM", "2h", data_dir=data_dir,
                         manifest_path=manifest_path)
    assert manifest_path.read_bytes() == payload_before
    assert out_path.read_bytes() == csv_before


def test_aggregate_symbol_rejects_1h(tmp_path):
    with pytest.raises(ValueError):
        agg.aggregate_symbol("TESTSYM", "1h", data_dir=tmp_path / "d",
                             manifest_path=tmp_path / "m.json")
