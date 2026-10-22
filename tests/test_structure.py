"""结构层测试(docs/methodology.md §2-§5, §6A-A10)。

覆盖:ATR 精确性、pivot 知时不变量、区域合并、FLIPPED 继承、
前缀不变性(本系统最关键性质,A10/§0.3)、增量==批量同一性。
场景参数与价格路径均为合成构造,与断言一起冻结在本文件中。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from market_operator.structure import (
    MarketStructure,
    Params,
    PivotKind,
    Regime,
    ZoneRole,
    ZoneState,
    replay,
)
from market_operator.events import EventDetector

BASE_TS = 1_700_000_000_000
CONFIG = "config/defaults.json"


def _mk(bars, o, h, l, c):
    bars.append((BASE_TS + 3_600_000 * len(bars), o, h, l, c, 1.0))


def calm(bars, px=100.0, w=0.05, n=1):
    """等幅平静 bar(价格不动的 bar 序列)。"""
    for _ in range(n):
        _mk(bars, px, px + w, px - w, px)


def seg(bars, *closes, w=0.03):
    """以前一收盘为开盘、步进收盘的路径段。"""
    for c in closes:
        o = bars[-1][4]
        _mk(bars, o, max(o, c) + w, min(o, c) - w, c)


def bar(bars, o, h, l, c):
    _mk(bars, o, h, l, c)


def feed_all(structure, bars):
    for b in bars:
        structure.update(b)


def make_random_df(n=220, seed=42):
    rng = np.random.RandomState(seed)
    steps = rng.normal(0.0, 0.7, n)
    price = 100.0
    rows = []
    for i in range(n):
        o = price
        c = o + steps[i]
        span = abs(rng.normal(0.0, 0.4))
        h = max(o, c) + span
        l = min(o, c) - span
        rows.append((BASE_TS + 3_600_000 * i, o, h, l, c, 1.0))
        price = c
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])


# ---------------------------------------------------------------------------
# 参数与 ATR(§2)
# ---------------------------------------------------------------------------


def test_params_from_json_matches_frozen_priors():
    p = Params.from_json(CONFIG)
    assert p.atr_n == 14 and p.k_rev == 1.0 and p.k_zw == 0.5
    assert p.k_bm == 0.1 and p.k_rj == 0.3 and p.k_span == 3.0
    assert p.k_room == 2.0 and p.tol_atr == 0.1
    assert p.n_a == 3 and p.n_f == 3 and p.range_max_pivots == 8
    assert p.e4_depth_atr == (0.5, 2.5) and p.e4_recovery == 0.5
    assert p.e5_edge_atr == 0.75


def test_atr_warmup_zero_then_wilder_exact():
    p = Params.from_json(CONFIG)  # atr_n = 14
    bars = []
    calm(bars, 100.0, 0.5, 14)              # tr = 1.0 × 14
    bar(bars, 100.0, 102.0, 100.0, 101.0)   # tr = 2.0(含跳空幅度)
    bar(bars, 101.0, 101.5, 100.5, 101.0)   # tr = 1.0
    s = MarketStructure(p)
    for b in bars:
        s.update(b)
    # 前 13 根 ATR 不可用(0.0);第 14 根(索引 13)= tr 均值 = 1.0
    s_probe = MarketStructure(p)
    for b in bars[:13]:
        s_probe.update(b)
        assert s_probe.atr() == 0.0
    v14 = (13 * 1.0 + 2.0) / 14                         # Wilder 递推第一步
    v15 = (v14 * 13 + 1.0) / 14
    # bars[13] 是 calm 序列最后一根,bars[14]/[15] 才是上面两根手工 bar
    s2 = MarketStructure(p)
    for b in bars[:14]:
        s2.update(b)
    assert s2.atr() == pytest.approx(1.0)
    s2.update(bars[14])
    assert s2.atr() == pytest.approx(v14)
    s2.update(bars[15])
    assert s2.atr() == pytest.approx(v15)


# ---------------------------------------------------------------------------
# Pivot 不变量(§3, A10)
# ---------------------------------------------------------------------------


def test_pivot_invariants_on_random_walk():
    p = Params.from_json(CONFIG)
    df = make_random_df()
    s = MarketStructure(p)
    feed_all(s, list(df.itertuples(index=False, name=None)))
    pivots = s.pivots
    assert len(pivots) >= 3, "随机游走应产生足够 pivot 供断言"
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    for pv in pivots:
        # A10/任务契约:知时恒晚于成形时
        assert pv.known_index > pv.forming_index
        # 极值价取自成形 bar 的对应极值
        if pv.kind is PivotKind.HIGH:
            assert pv.price == pytest.approx(highs[pv.forming_index])
        else:
            assert pv.price == pytest.approx(lows[pv.forming_index])
        # ATR 可用前不可能有任何确认(§3 判据需要 k_rev*atr > 0)
        assert pv.known_index >= p.atr_n - 1
    # 高低交错(确认驱动的腿状态机不产生同类连续 pivot)
    kinds = [pv.kind for pv in pivots]
    assert all(a is not b for a, b in zip(kinds, kinds[1:]))


# ---------------------------------------------------------------------------
# 区域:生成、合并、FLIPPED 继承(§4)
# ---------------------------------------------------------------------------


def test_zone_merge_unions_overlapping_same_role():
    p = Params(atr_n=2)
    bars = []
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.95, 100.3)     # H1=100.98 确认 @7 → zone [100.66,100.98]
    seg(bars, 99.9, 100.94)      # 回落后再上探(触碰,不破位)
    s = MarketStructure(p)
    feed_all(s, bars)
    (z1,) = [z for z in s.zones if z.role is ZoneRole.RESISTANCE]
    assert z1.state is ZoneState.ACTIVE
    seg(bars, 100.1)             # 深度回撤 → H2=100.97 确认,触发合并
    feed_all(s, bars[len(bars) - 1:])
    res = [z for z in s.zones if z.role is ZoneRole.RESISTANCE]
    assert len(res) == 1, "同向重叠区域必须合并为一个(§4.1)"
    z = res[0]
    assert z.hi == pytest.approx(max(z1.hi, 100.97))
    assert z.lo == pytest.approx(min(z1.lo, 100.97 - p.k_zw * s.atr()))
    assert z.touches == z1.touches + 1      # touches 相加
    assert z.merged == z1.merged + 1        # 合并来源数
    assert z.origin_known == z1.origin_known  # origin 保留最早者
    assert z.state is ZoneState.ACTIVE


def test_zone_flip_inherits_facts():
    p = Params(atr_n=2)
    bars = []
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.95, 100.3, 100.6, 101.3)  # 破位 bar @9(收盘越 hi+k_bm·atr)
    s = MarketStructure(p)
    feed_all(s, bars)
    (broken,) = [z for z in s.zones if z.role is ZoneRole.RESISTANCE]
    assert broken.state is ZoneState.BROKEN
    snap = broken.clone()
    # 从远侧返回带内并被拒绝(回退 ≥ k_rj·atr)→ FLIPPED,生成反向继承区
    bar(bars, 101.3, 101.25, 100.9, 101.15)
    feed_all(s, bars[-1:])
    res = [z for z in s.zones if z.role is ZoneRole.RESISTANCE]
    assert not any(z.lo == snap.lo and z.hi == snap.hi for z in res), \
        "旧阻力区应已消亡(A10:终止态移出视图)"
    (sup,) = [z for z in s.zones
              if z.role is ZoneRole.SUPPORT and z.lo == pytest.approx(snap.lo)]
    assert sup.state is ZoneState.ACTIVE
    assert sup.hi == pytest.approx(snap.hi)
    assert sup.touches == snap.touches      # 继承 touches(§4.2 FLIPPED)
    assert sup.rejects == snap.rejects
    assert sup.origin_known == snap.origin_known


# ---------------------------------------------------------------------------
# 前缀不变性(A10/§0.3,本系统最关键测试)与增量==批量(§1 因果纪律)
# ---------------------------------------------------------------------------


def _snapshot(structure, events_new):
    return (
        structure.pivots,
        structure.zones,
        structure.atr(),
        structure.regime(),
        list(events_new),
    )


def test_prefix_invariance_structure_and_events():
    """先喂 t 根得到的认知 == 喂全部后回看在 t 时刻的认知(多切点)。"""
    p = Params.from_json(CONFIG)
    df = make_random_df()
    bars = list(df.itertuples(index=False, name=None))
    cuts = list(range(25, len(bars), 15)) + [len(bars) - 1]

    full_snaps = {}
    det_full = EventDetector(p)
    full_events = []
    for i, b in enumerate(bars):
        evs = det_full.update(b)
        full_events.extend(evs)
        if i in cuts:
            full_snaps[i] = _snapshot(det_full.structure, evs)

    for t0 in cuts:
        det_pre = EventDetector(p)
        pre_events = []
        for b in bars[: t0 + 1]:
            pre_events.extend(det_pre.update(b))
        s_pre = det_pre.structure
        pivots_f, zones_f, atr_f, regime_f, ev_f = full_snaps[t0]
        assert s_pre.pivots == pivots_f, f"pivots 在 cut={t0} 发生回溯改写"
        assert s_pre.zones == zones_f, f"zones 在 cut={t0} 发生回溯改写"
        assert s_pre.atr() == atr_f
        assert s_pre.regime() is regime_f
        assert pre_events == [e for e in full_events if e.known_index <= t0], \
            f"事件序列在 cut={t0} 发生回溯改写"
        assert ev_f == [e for e in full_events if e.known_index == t0]


def test_incremental_equals_batch_replay():
    """replay(df) 与逐 bar update(混合 bar 输入形式)结果完全一致。"""
    df = make_random_df()
    structure_r, events_r = replay(df)

    det = EventDetector(Params.from_json(CONFIG))
    events_m = []
    for i, row in enumerate(df.itertuples(index=False)):
        if i % 3 == 0:
            b = (row.ts, row.open, row.high, row.low, row.close, row.volume)
        elif i % 3 == 1:
            b = {"ts": row.ts, "open": row.open, "high": row.high,
                 "low": row.low, "close": row.close, "volume": row.volume}
        else:
            b = df.iloc[i]  # pandas Series 形式
        events_m.extend(det.update(b))

    s = det.structure
    assert s.pivots == structure_r.pivots
    assert s.zones == structure_r.zones
    assert s.atr() == structure_r.atr()
    assert s.regime() is structure_r.regime()
    assert s.bars_seen == structure_r.bars_seen == len(df)
    assert events_m == events_r


def test_replay_contract_and_event_timestamps():
    df = make_random_df()
    structure, events = replay(df)
    assert structure.bars_seen == len(df)
    assert all(isinstance(e.id, str) and e.id in {"BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED", "STRUCTURE_BROKEN"}
               for e in events)
    tss = df["ts"].tolist()
    for e in events:
        assert 0 <= e.known_index < len(df)
        assert e.ts == tss[e.known_index], "事件 ts 必须是知时 bar 的时间戳"
        assert e.direction in (+1, -1)
    knowns = [e.known_index for e in events]
    assert knowns == sorted(knowns), "回放按时间序发射,known_index 不可回退"
    # 只读视图:zones 是克隆,外部改动不影响内部状态
    zs = structure.zones
    for z in zs:
        z.touches = -999
    assert all(z.touches != -999 for z in structure.zones)
    assert isinstance(structure.regime(), Regime)
