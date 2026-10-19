"""v4 真实数据测试(docs/v4-protocol.md §1/§3,tests):15m 帧完整性、15m↔1h
对齐硬约束、dev/test 拐点(1h 索引 12320)、compaction 一致性(15m 聚合 1h
== 带哈希 1h 帧)与 compaction 1h 决策流 == v2 逐 bar 回放决策流(events/
zone 状态逐格一致;另与 v2 冻结产物 backtest_v2.json 的 E7 计数交叉核对)。

全部离线:不改写任何数据/产物文件。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from market_operator import aggregate as agg
from market_operator import data as mo_data
from market_operator.backtest import prepare_cognition
from market_operator.backtest_v4 import (
    alignment_issues_ci,
    assert_aligned,
    build_exec_frame,
)
from market_operator.hypothesis import f1_invalid_distance_ok
from market_operator.research import split_boundary
from market_operator.structure import Params

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
N_1H = 17_600
BOUNDARY_1H = 12_320
MS_Q = 900_000
MS_H = 3_600_000

CFG = json.loads((ROOT / "config" / "v4.json").read_text(encoding="utf-8"))
PARAMS = Params.from_json(ROOT / "config" / "v4.json")
WHITELIST = frozenset((str(i), int(d)) for i, d in CFG["whitelist_v2"])
K_IS = float(CFG["k_is"])
DEV_FRAC = float(CFG["dev_frac"])


@pytest.mark.parametrize("sym", SYMBOLS)
def test_15m_frame_integrity_and_manifest(sym):
    df15 = mo_data.load(sym, "15m")
    df1h = mo_data.load(sym)
    assert len(df15) == 4 * N_1H                            # 70400
    assert mo_data.validate(df15, step_ms=MS_Q) == []       # 零缺口/重复/坏 OHLC
    ts15, ts1h = df15["ts"].to_numpy(), df1h["ts"].to_numpy()
    assert ts15[0] == ts1h[0]                               # 覆盖首
    assert ts15[-1] == ts1h[-1] + 3 * MS_Q                  # 末根 1h 的 4 子 bar 齐

    manifest = json.loads((ROOT / "data" / "manifest.json").read_text(
        encoding="utf-8"))
    rec = manifest["files"][f"{sym}_15m.csv"]
    raw = (ROOT / "data" / "raw" / f"{sym}_15m.csv").read_bytes()
    assert rec["sha256"] == hashlib.sha256(raw).hexdigest()  # manifest sha256 追加
    assert rec["rows"] == 4 * N_1H
    assert rec["ts_first"] == int(ts15[0]) and rec["ts_last"] == int(ts15[-1])
    assert rec["granularity"] == "15m" and rec["step_ms"] == MS_Q
    assert rec["validation_issues"] == []
    # 既有 1h manifest 记录不被重写(sha 未变)
    rec_1h = manifest["files"][f"{sym}_1h.csv"]
    assert rec_1h["rows"] == N_1H and rec_1h["validation_issues"] == []


@pytest.mark.parametrize("sym", SYMBOLS)
def test_alignment_and_boundary_landmark(sym):
    df15 = mo_data.load(sym, "15m")
    df1h = mo_data.load(sym)
    # 数据层覆盖/网格断言(v4-protocol §1:15m 首末必须覆盖 1h 首末)
    assert mo_data.check_frame_alignment(df15, df1h, step_fine_ms=MS_Q) == []
    bars_1h = [(int(r.ts),) for r in df1h.itertuples(index=False)]
    bars_15m = [(int(r.ts),) for r in df15.itertuples(index=False)]
    assert_aligned(bars_1h, bars_15m)
    # dev/test 拐点(同 v3):split_boundary == 冻结 1h 索引 12320,落桶齐整
    assert len(df1h) == N_1H
    assert split_boundary(len(df1h), DEV_FRAC) == BOUNDARY_1H
    assert alignment_issues_ci(bars_1h, bars_15m, DEV_FRAC) == []
    assert int(df1h["ts"].iloc[BOUNDARY_1H]) == int(df15["ts"].iloc[4 * BOUNDARY_1H])


@pytest.mark.parametrize("sym", SYMBOLS)
def test_compaction_equals_1h_frame_ohlc(sym):
    """15m 聚合 1h 与带哈希 1h 帧逐格一致(OHLC 确切;volume 相对差 ≤1e-3,
    认知不使用 volume,如实声明)。"""
    df15 = mo_data.load(sym, "15m")
    df1h = mo_data.load(sym)
    compact = agg.aggregate(df15, 1)
    assert len(compact) == N_1H
    assert (compact["ts"].to_numpy() == df1h["ts"].to_numpy()).all()
    for col in ("open", "high", "low", "close"):
        assert (compact[col] - df1h[col]).abs().max() == 0.0
    vol_rel = float(((compact["volume"] - df1h["volume"]).abs()
                     / df1h["volume"].clip(lower=1e-9)).max())
    assert vol_rel <= 1e-3


def _assert_cogs_equal(a, b, sym):
    """两个 SymbolCognition(15m-compaction vs 直取 1h)决策产物逐格一致。

    bars 逐格比较 ts/OHLC(确切);volume 单独断言(交易所不同粒度报量的
    口径残差,实测 rel ≤ 1.3e-4,阈 1e-3;认知不使用 volume —— 事件/zone/
    pivot/atr 与 volume 无关,如实声明,见 test_compaction_equals_1h_frame_ohlc)。
    """
    assert len(a.bars) == len(b.bars)
    for i, (x, y) in enumerate(zip(a.bars, b.bars)):
        assert x[:5] == y[:5], f"{sym}@{i}: ts/OHLC diverge {x} != {y}"
        assert x[5] == pytest.approx(y[5], rel=1e-3), \
            f"{sym}@{i}: volume beyond reporting residual {x[5]} != {y[5]}"
    assert a.atrs == b.atrs, f"{sym}: atrs diverge"
    assert a.events_by.keys() == b.events_by.keys()
    assert a.pivots_by.keys() == b.pivots_by.keys()
    for i in a.events_by:
        ea, eb = a.events_by[i], b.events_by[i]
        assert len(ea) == len(eb), f"{sym}@{i}: event count"
        for x, y in zip(ea, eb):
            assert (x.id, x.direction, x.ts, x.known_index) == \
                   (y.id, y.direction, y.ts, y.known_index)
            assert x.attrs["invalid"] == pytest.approx(y.attrs["invalid"],
                                                       rel=0, abs=1e-12)
            assert x.attrs["ref"] == pytest.approx(y.attrs["ref"], rel=0,
                                                   abs=1e-12)
    for i in a.pivots_by:
        assert [(p.price, p.forming_index, p.known_index, p.kind)
                for p in a.pivots_by[i]] == \
               [(p.price, p.forming_index, p.known_index, p.kind)
                for p in b.pivots_by[i]], f"{sym}@{i}: pivots diverge"
    # zone 状态(事件 bar 结构视图:lo/hi/role/state 逐格)
    assert a.views.keys() == b.views.keys()
    for i in a.views:
        va, vb = a.views[i], b.views[i]
        assert va.regime is vb.regime
        assert va.atr == pytest.approx(vb.atr, rel=0, abs=1e-12)
        za = tuple((z.lo, z.hi, z.role.value, z.state.value) for z in va.zones)
        zb = tuple((z.lo, z.hi, z.role.value, z.state.value) for z in vb.zones)
        assert za == zb, f"{sym}@{i}: zone states diverge"


@pytest.mark.parametrize("sym", SYMBOLS)
def test_compaction_decision_stream_equals_v2_parity(sym):
    """compaction 1h 决策流 == v2 运行时逐 bar 回放决策流(events/zone/pivot/
    atr 同构)。另:E7 发射/过 F1 计数与 v2 冻结产物交叉核对(同一认知源)。"""
    df15 = mo_data.load(sym, "15m")
    df1h = mo_data.load(sym)
    cog_compact = prepare_cognition(agg.aggregate(df15, 1), PARAMS, sym,
                                    enable_e7=True)
    cog_direct = prepare_cognition(df1h, PARAMS, sym, enable_e7=True)
    _assert_cogs_equal(cog_compact, cog_direct, sym)

    bt_v2 = json.loads((ROOT / "reports" / "backtest_v2.json").read_text(
        encoding="utf-8"))
    fired, passed = [], []
    for i, evs in cog_direct.events_by.items():
        for e in evs:
            if (e.id, e.direction) in WHITELIST:
                fired.append(e)
                if f1_invalid_distance_ok(e, float(cog_direct.bars[i][4]), K_IS):
                    passed.append(e)
    v2c = bt_v2["e7_counts"][sym]
    assert (v2c["whitelist_events"], v2c["passed_f1"]) == \
        (len(fired), len(passed)), f"{sym}: E7 计数与 v2 冻结产物不一致"


@pytest.mark.parametrize("sym", SYMBOLS)
def test_exec_frame_grid(sym):
    """build_exec_frame:每根 1h bar 恰 4 子 bar 且聚合回 1h bar 一致。"""
    df15 = mo_data.load(sym, "15m")
    df1h = mo_data.load(sym)
    cog = prepare_cognition(df1h, PARAMS, sym, enable_e7=True)
    frame = build_exec_frame(cog, df15)
    assert len(frame.quarters) == N_1H
    for i, qs in frame.quarters.items():
        assert [q[0] for q in qs] == [cog.bars[i][0] + k * MS_Q for k in range(4)]
        b = cog.bars[i]
        assert qs[0][1] == b[1]                          # open
        assert max(q[2] for q in qs) == b[2]             # high
        assert min(q[3] for q in qs) == b[3]             # low
        assert qs[-1][4] == b[4]                         # close
    assert frame.hour_index[cog.bars[BOUNDARY_1H][0]] == BOUNDARY_1H
