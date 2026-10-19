"""v2 测试(docs/v2-protocol.md §1):E7 FLIP_RETEST 发射、F1 门、v1 回归 parity。

新增文件:不改动任何既有断言。v1 行为零扰动由 test_v1_regression_parity
直接以 reports/event_study_events_test.csv(v1 事件流逐事件明细,test 段)
比对 E1-E6 序列的数量与关键字段(event_id/direction/ts/known_index/invalid/
entry/atr)来守护。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from market_operator.events import E7, EventDetector, MarketEvent
from market_operator.hypothesis import StructureView, f1_invalid_distance_ok
from market_operator.research import collect_events, split_boundary
from research.rounds.v1.strategy import Strategy, StrategyParams
from market_operator.structure import Params, Regime, Zone, ZoneRole, ZoneState

ROOT = Path(__file__).resolve().parents[4]
BASE_TS = 1_700_000_000_000
P2 = Params(atr_n=2)    # 编排用小窗口(同 test_events.py 惯例);规则阈值不变


def _mk(bars, o, h, l, c):
    bars.append((BASE_TS + 3_600_000 * len(bars), o, h, l, c, 1.0))


def calm(bars, px=100.0, w=0.05, n=1):
    for _ in range(n):
        _mk(bars, px, px + w, px - w, px)


def seg(bars, *closes, w=0.03):
    for c in closes:
        o = bars[-1][4]
        _mk(bars, o, max(o, c) + w, min(o, c) - w, c)


def bar(bars, o, h, l, c):
    _mk(bars, o, h, l, c)


def run_with_atrs(bars, params=P2):
    det = EventDetector(params, enable_e7=True)   # v2 E7 显式开启(默认关=v1)
    atrs = []
    for b in bars:
        det.update(b)
        atrs.append(det.structure.atr())
    return det, atrs


def by_id(events, eid):
    return [e for e in events if e.id == eid]


# ---------------------------------------------------------------------------
# E7 FLIP_RETEST:支撑破位 → 回踩 → 被拒绝 → FLIPPED → E7|short
# ---------------------------------------------------------------------------


def short_bars():
    bars = []
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 99.05, 99.7)        # 6(下探形成 L pivot),7(确认;生成支撑区)
    seg(bars, 98.4)               # 8:收盘 < zone.lo - k_bm·atr → BROKEN
    bar(bars, 98.4, 99.05, 98.45, 98.6)   # 9:返回带内(h≥lo)且自回踩极值回退
    return bars                   # ≥ k_rj·atr → FLIPPED@9(角色 → resistance)


def test_e7_flip_retest_short():
    bars = short_bars()
    det, atrs = run_with_atrs(bars)
    e7s = by_id(det.events, E7)
    assert len(e7s) == 1, "FLIPPED 转移 bar 应恰好发射一个 E7"
    e = e7s[0]
    assert e.direction == -1                      # role_new=resistance ⇒ 做空
    assert e.known_index == 9                     # 转移知时 = FLIPPED bar
    assert e.ts == bars[9][0]
    assert e.attrs["from_role"] == ZoneRole.SUPPORT.value
    z = e.attrs["zone"]
    assert z.role is ZoneRole.RESISTANCE          # 已角色互换(旧支撑边缘新阻力)
    assert z.state is ZoneState.ACTIVE
    assert e.attrs["ref"] == pytest.approx(z.lo)  # ref = 破位边缘(short:lo)
    assert e.attrs["invalid"] == pytest.approx(z.hi + P2.k_bm * atrs[9])
    assert e.attrs["atr"] == pytest.approx(atrs[9])
    # 同 bar 结构层 role_new 已在视图中(旧区 FLIPPED 移除;破位 bar 另确认一个
    # H pivot 生成同向阻力区,翻转新区按 §4.1 与重叠同向区合并为并集)
    assert any(zv.role is ZoneRole.RESISTANCE and zv.lo <= z.lo and zv.hi >= z.hi
               and zv.last_change_known == 9
               for zv in det.structure.zones)


def test_e7_flip_retest_long_mirror():
    bars = []
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.95, 100.3)      # 6(冲高形成 H pivot),7(确认;生成阻力区)
    seg(bars, 101.6)              # 8:收盘 > zone.hi + k_bm·atr → BROKEN
    bar(bars, 101.6, 101.65, 100.95, 101.55)  # 9:回踩(l≤hi)后向上回退 → FLIPPED@9
    det, atrs = run_with_atrs(bars)
    e7s = by_id(det.events, E7)
    assert len(e7s) == 1
    e = e7s[0]
    assert e.direction == +1                      # role_new=support ⇒ 做多
    assert e.known_index == 9
    assert e.ts == bars[9][0]
    assert e.attrs["from_role"] == ZoneRole.RESISTANCE.value
    z = e.attrs["zone"]
    assert z.role is ZoneRole.SUPPORT
    assert e.attrs["ref"] == pytest.approx(z.hi)  # long:破位边缘 = hi
    assert e.attrs["invalid"] == pytest.approx(z.lo - P2.k_bm * atrs[9])
    assert e.attrs["atr"] == pytest.approx(atrs[9])


def test_e7_control_broken_without_retest_no_event():
    bars = short_bars()
    bars[9] = (bars[9][0], 98.4, 98.43, 97.97, 98.0, 1.0)  # 改版:全程未回带内
    seg(bars, 97.5)                                        # 10:继续远离
    det = run_with_atrs(bars)[0]
    assert by_id(det.events, E7) == [], "破位后未回踩接受/拒绝,不得发射 E7"


def test_e7_control_retest_reacceptance_dead_no_event():
    bars = []
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 99.05, 99.7)
    seg(bars, 98.4)                    # 8:BROKEN
    bar(bars, 99.0, 99.15, 98.95, 99.5)   # 9:小幅回踩带内后反向穿回 → DEAD(非 FLIPPED)
    det, atrs = run_with_atrs(bars)
    assert by_id(det.events, E7) == [], "回踩后反向 acceptance 穿回(→DEAD)不得发射 E7"
    # 原支撑区(本例 lo≈99.02)应 DEAD 移除(视图中残留的是破位 bar 确认 pivot
    # 另成的阻力区,非角色互换产物)
    assert not any(z.lo == pytest.approx(99.02, abs=1e-3)
                   for z in det.structure.zones)


# ---------------------------------------------------------------------------
# F1 门(v2-protocol §1):dist(close(t), invalid) ≥ k_is·atr(t),k_is=0.8
# ---------------------------------------------------------------------------

K_IS = 0.8
F1_ATR = 5.0          # k_is·atr = 4.0(恰可表示,边界断言无浮点歧义)
F1_THRESH = K_IS * F1_ATR


def _e7_event(direction, invalid, atr=F1_ATR, ref=100.0):
    return MarketEvent(
        id=E7, direction=direction, ts=BASE_TS, known_index=0,
        attrs={"zone": Zone(lo=ref - 0.5, hi=ref + 0.5,
                            role=ZoneRole.RESISTANCE if direction < 0
                            else ZoneRole.SUPPORT,
                            state=ZoneState.ACTIVE),
               "ref": ref, "invalid": invalid,
               "from_role": (ZoneRole.SUPPORT.value if direction < 0
                             else ZoneRole.RESISTANCE.value),
               "atr": atr})


def _view_for(direction, ref=100.0, atr=F1_ATR):
    """含一个结构目标的视图(target 存在,使 r_min 阶段可达,隔离 F1 判定)。"""
    if direction < 0:
        zones = (Zone(lo=91.0, hi=92.0, role=ZoneRole.SUPPORT,
                      state=ZoneState.ACTIVE),)
    else:
        zones = (Zone(lo=108.0, hi=109.0, role=ZoneRole.RESISTANCE,
                      state=ZoneState.ACTIVE),)
    return StructureView(regime=Regime.RANGE, zones=zones, atr=atr)


def _bar_with_close(close):
    return (BASE_TS, close, close + 0.05, close - 0.05, close, 1.0)


def test_f1_gate_at_boundary_passes():
    close = 100.0
    ev = _e7_event(-1, invalid=close + F1_THRESH)   # dist 恰 = k_is·atr = 4.0
    assert f1_invalid_distance_ok(ev, close, K_IS), "恰 k_is·atr 应判满足(≥号)"
    s = Strategy(StrategyParams(whitelist=frozenset({(E7, -1)}), k_is=K_IS))
    intents = s.on_bar("XYZ", _bar_with_close(close), _view_for(-1), [ev])
    assert len(intents) == 1                        # r_mult = 8/4 = 2.0 ≥ 1.5


def test_f1_gate_below_blocks():
    close = 100.0
    ev = _e7_event(-1, invalid=close + F1_THRESH - 0.1)   # dist = 3.9 < 4.0
    assert not f1_invalid_distance_ok(ev, close, K_IS)
    s = Strategy(StrategyParams(whitelist=frozenset({(E7, -1)}), k_is=K_IS))
    intents = s.on_bar("XYZ", _bar_with_close(close), _view_for(-1), [ev])
    assert intents == []
    assert "f1|" in s.explain()


def test_f1_gate_long_mirror():
    close = 100.0
    ev = _e7_event(+1, invalid=close - F1_THRESH)    # dist 恰 = 4.0 通过
    assert f1_invalid_distance_ok(ev, close, K_IS)
    s = Strategy(StrategyParams(whitelist=frozenset({(E7, +1)}), k_is=K_IS))
    intents = s.on_bar("XYZ", _bar_with_close(close), _view_for(+1), [ev])
    assert len(intents) == 1
    ev2 = _e7_event(+1, invalid=close - F1_THRESH + 0.1)   # dist = 3.9 < 4.0
    s2 = Strategy(StrategyParams(whitelist=frozenset({(E7, +1)}), k_is=K_IS))
    assert not f1_invalid_distance_ok(ev2, close, K_IS)
    assert s2.on_bar("XYZ", _bar_with_close(close), _view_for(+1), [ev2]) == []
    assert "f1|" in s2.explain()


def test_f1_gate_off_by_default():
    """k_is=0(v1 默认)不应用 F1;v1 白名单路径不变(E7 仍不在 GO 集)。"""
    ev = _e7_event(-1, invalid=101.0)
    s = Strategy()      # v1 参数
    assert s.on_bar("XYZ", _bar_with_close(100.9), _view_for(-1), [ev]) == []
    assert "f1|" not in s.explain()
    assert "nogo" in s.explain() or "dir" in s.explain()


# ---------------------------------------------------------------------------
# v1 回归 parity:E1-E6 事件序列与 v1 产物(reports/event_study_events_test.csv)
# 完全一致(数量 + 关键字段);E7 仅追加,不得影响任何既有事件。
# ---------------------------------------------------------------------------

V1_IDS = frozenset({"E1", "E2", "E3", "E4", "E5", "E6"})


def test_v1_regression_parity():
    csv_path = ROOT / "reports" / "event_study_events_test.csv"
    cfg = json_loads(ROOT / "config" / "defaults.json")
    params = Params.from_json(ROOT / "config" / "defaults.json")
    dev_frac = float(cfg["dev_frac"])
    v1 = pd.read_csv(csv_path)
    max_h = 48

    for sym in cfg["symbols"]:
        df = pd.read_csv(ROOT / "data" / "raw" / f"{sym}_1h.csv",
                         dtype={"ts": "int64"})
        df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
        n = len(df)
        boundary = split_boundary(n, dev_frac)
        events, timeline = collect_events(df, params)
        kept = [e for e in events if e.id in V1_IDS]   # E7 仅追加,旧序列不动

        # 复刻 v1 测量过滤(measure_events:前向窗不足 / atr<=0 / 假设缺失 ⇒ 跳过)
        measured = {}
        for uid, e in enumerate(kept):
            entry_i = e.known_index + 1
            if e.known_index < boundary:
                continue                      # 仅 test 段(v1 产物 test.csv)
            if entry_i + max_h - 1 >= n:
                continue                      # 前向窗不足(数据尾部)
            view = timeline.views[e.known_index]
            if float(view.atr) <= 0.0:
                continue
            measured[uid] = (e, view, entry_i)

        rows = (v1[v1["symbol"] == sym]
                .drop_duplicates(subset=["uid"]))
        exp_uids = {int(u.split("#")[1]) for u in rows["uid"]}
        assert exp_uids == set(measured), \
            f"{sym}: per-event uid 集合与 v1 事件流不一致(数量 {len(exp_uids)} vs {len(measured)})"

        for uid, (e, view, entry_i) in measured.items():
            r = rows[rows["uid"] == f"{sym}#{uid}"].iloc[0]
            assert e.id == r["event_id"]
            assert e.direction == int(r["direction"])
            assert int(e.ts) == int(r["ts"])
            assert e.known_index == int(r["known_index"])
            assert e.attrs["invalid"] == pytest.approx(float(r["invalid"]), rel=1e-9)
            assert float(df["open"][entry_i]) == pytest.approx(float(r["entry"]), rel=1e-9)
            assert float(view.atr) == pytest.approx(float(r["atr"]), rel=1e-9)
            assert view.regime.value == r["regime"]


def json_loads(path):
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))
