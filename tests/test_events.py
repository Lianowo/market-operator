"""事件层测试(docs/methodology.md §6,精确定义 §6A-A1..A9)。

每个事件一个精确触发场景(hand-crafted 价格路径,价格/索引已逐 bar 排出)
+ 一个不触发的对照场景。断言事件 id/direction/known_index;
对照场景断言目标事件不存在(可存在其它机制允许的伴随事件)。
小 atr_n(=2) 场景用 Params(atr_n=2);E5(RANGE + 空间条款,比值接近 1)
用冻结 config(atr_n=14,Wilder 更平滑)保证阈值余量。
"""

from __future__ import annotations

import pytest

from market_operator.events import E1, E2, E3, E4, E5, E6, EventDetector
from market_operator.structure import Params, Regime, ZoneRole

BASE_TS = 1_700_000_000_000
CONFIG = "config/defaults.json"
P2 = Params(atr_n=2)    # 编排用小窗口;规则本身不变(config 先验值均需用,见各场景)


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


def run(bars, params=P2):
    det = EventDetector(params)
    for b in bars:
        det.update(b)
    return det


def run_with_atrs(bars, params=P2):
    """逐 bar 回放并记录每根收盘时刻的 atr(t)(= 事件判定时看到的阈值尺度)。"""
    det = EventDetector(params)
    atrs = []
    for b in bars:
        det.update(b)
        atrs.append(det.structure.atr())
    return det, atrs


def by_id(events, eid):
    return [e for e in events if e.id == eid]


# ---------------------------------------------------------------------------
# 公共前缀:calm×6 → 冲高 → 回落确认 H1=100.98@7,阻力区 [100.664,100.98]
# ---------------------------------------------------------------------------


def h1_prefix(bars):
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.95, 100.3)          # 索引 6,7;H1 pivot: forming=6, known=7


# ---------------------------------------------------------------------------
# E1 BREAKOUT_ACCEPTANCE(§6, A4)
# ---------------------------------------------------------------------------


def test_e1_breakout_acceptance_positive():
    bars = []
    h1_prefix(bars)
    seg(bars, 100.6, 101.3)    # 8;9 = 破位候选(收盘 > hi + k_bm·atr)
    seg(bars, 101.2, 101.25)   # 10;11 = 第 2、3 根连续收于 hi 之上 → E1 @11
    det = run(bars)
    e1s = by_id(det.events, E1)
    assert len(e1s) == 1
    e = e1s[0]
    assert e.direction == +1            # 向上突破接受,做多语境(§6 表语义)
    assert e.known_index == 11
    assert e.ts == bars[11][0]
    assert e.attrs["ref"] == pytest.approx(e.attrs["zone"].hi)
    assert e.attrs["zone"].role is ZoneRole.RESISTANCE
    assert e.attrs["started"] == 9      # A4:候选 bar = 第 1 根
    # 候选窗口内全部收盘 > zone.hi(A4 判据)
    assert all(bars[i][4] > e.attrs["zone"].hi for i in (9, 10, 11))


def test_e1_control_invalidation_no_event():
    bars = []
    h1_prefix(bars)
    seg(bars, 100.6, 101.3)    # 破位候选 @9
    seg(bars, 100.9, 101.2)    # 10:收盘跌回 hi 之下 → 候选作废(A4);11 再收复不算
    det = run(bars)
    assert by_id(det.events, E1) == [], "连续 N_a 根中断不得发 E1"


# ---------------------------------------------------------------------------
# E2 BREAKOUT_FAILURE(§6, A5)
# ---------------------------------------------------------------------------


def test_e2_breakout_failure_positive():
    bars = []
    h1_prefix(bars)
    seg(bars, 100.0)                            # 8:撤到正面之外
    bar(bars, 100.0, 100.78, 99.82, 100.75)     # 9:A2 触碰(开 episode)
    bar(bars, 100.75, 101.06, 100.7, 101.0)     # 10:刺穿 hi+k_bm·atr,收盘未破位
    bar(bars, 101.0, 101.03, 100.87, 100.9)     # 11:收盘回 hi 之下 → E2 @11
    det, atrs = run_with_atrs(bars)
    e2s = by_id(det.events, E2)
    assert len(e2s) == 1
    e = e2s[0]
    assert e.direction == -1            # 阻力侧失败突破,做空语境
    assert e.known_index == 11
    assert e.ts == bars[11][0]
    assert e.attrs["puncture_index"] == 10
    hi = e.attrs["zone"].hi
    assert bars[10][2] > hi + P2.k_bm * atrs[10]   # A5:影线刺穿外缘 + 余量
    assert bars[10][4] <= hi + P2.k_bm * atrs[10]  # A4 未先破位(竞争次序)
    assert bars[11][4] < hi                        # ≤ N_f 根内收回带内
    assert by_id(det.events, E1) == [], "E1 未接受是 E2 成立的必要伴随(§6 表)"


def test_e2_control_no_puncture_no_event():
    bars = []
    h1_prefix(bars)
    seg(bars, 100.0)
    bar(bars, 100.0, 100.78, 99.82, 100.75)   # 9:触碰但 high 未越 hi+k_bm·atr
    seg(bars, 99.5, 99.0)                     # 10,11:直接回落(构成 E3,不构成 E2)
    det = run(bars)
    assert by_id(det.events, E2) == [], "无刺穿则无失败突破"
    assert len(by_id(det.events, E3)) == 1, "同路径应改为触发拒绝(E3)"


# ---------------------------------------------------------------------------
# E3 REJECTION(§6, A6)
# ---------------------------------------------------------------------------


def e3_bars():
    bars = []
    h1_prefix(bars)
    seg(bars, 100.0)                            # 8
    bar(bars, 100.0, 100.78, 99.82, 100.75)     # 9:触碰,ex=100.78
    bar(bars, 100.75, 100.84, 100.72, 100.8)    # 10:ex→100.84
    bar(bars, 100.8, 100.96, 100.76, 100.93)    # 11:ex→100.96
    bar(bars, 100.93, 100.96, 100.6, 100.7)     # 12:回退 0.36 ≥ k_rj·atr → E3 @12
    return bars


def test_e3_rejection_positive():
    bars = e3_bars()
    det = run(bars)
    e3s = by_id(det.events, E3)
    assert len(e3s) == 1
    e = e3s[0]
    assert e.direction == -1            # 阻力侧拒绝,做空语境
    assert e.known_index == 12
    assert e.ts == bars[12][0]
    assert e.attrs["ex"] == pytest.approx(100.96)
    # A6:episode 极值向正面回退 ≥ k_rj·atr
    z = e.attrs["zone"]
    assert z.rejects == 1               # §4.3:拒绝类事实记录(事件快照即时刻)


def test_e3_episode_dedup_and_rearm():
    """A3 去重语义:同一接近过程事件唯一;带内悬停不开新 episode;
    价格退回正面之外后重新触碰 = 新一轮从正面接近,可再次触发。"""
    bars = e3_bars()                               # E3 @12(收盘 100.7 仍在带内)
    seg(bars, 100.75, 100.8)                       # 13,14:带内悬停,不开新 episode
    seg(bars, 100.5)                               # 15:退出正面之外
    bar(bars, 100.5, 100.72, 100.48, 100.7)        # 16:重新(A2)触碰 → 新 episode
    bar(bars, 100.7, 100.75, 100.55, 100.6)        # 17:再度回退 → 第二次 E3 @17
    det = run(bars)
    e3s = by_id(det.events, E3)
    assert [e.known_index for e in e3s] == [12, 17], \
        "悬停 bar(13/14)不得重复触发;正面再接近(16/17)允许新一轮事件"
    assert e3s[0].attrs["zone"].rejects == 1
    assert e3s[1].attrs["zone"].rejects == 2


def test_e3_control_no_touch_no_event():
    bars = []
    h1_prefix(bars)
    seg(bars, 100.0)
    bar(bars, 100.0, 100.65, 99.9, 100.6)  # 9:high 止步于 zone.lo 之下 → 不触碰(A2)
    seg(bars, 100.55, 100.5)
    det = run(bars)
    assert by_id(det.events, E3) == []
    assert by_id(det.events, E2) == []
    assert by_id(det.events, E1) == []


# ---------------------------------------------------------------------------
# E4 TREND_RESUMPTION(§6, A7)
# ---------------------------------------------------------------------------


def trend_up_prefix(bars):
    """4 pivot 递升结构 @14 起 TREND_UP=L2 低 pivot 100.52@14 确认。"""
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.9, 101.8)    # 6,7
    seg(bars, 100.9, 100.4)    # 8(H1=101.83@8),9
    seg(bars, 101.4, 102.3)    # 10(L1=100.37@10),11
    seg(bars, 101.4, 100.55)   # 12(H2=102.33@12),13
    seg(bars, 101.45, 102.35, 103.25)  # 14(L2=100.52@14),15,16


def test_e4_trend_resumption_positive():
    bars = []
    trend_up_prefix(bars)
    seg(bars, 102.3, 101.35)           # 17(H3=103.28@17),18 回调腿
    seg(bars, 102.35, 103.0, 103.2)    # 19(L_pb=101.32@19),20,21
    det, atrs = run_with_atrs(bars)
    s = det.structure
    assert s.regime() is Regime.TREND_UP
    e4s = by_id(det.events, E4)
    # L2 确认本身即满足 A7(回调腿 H2→L2,深度在窗口内,收盘收复 50%)→ E4 @15
    # L_pb(101.32)确认后 @20 再次触发第二个 E4
    assert [e.known_index for e in e4s] == [15, 20]
    assert all(e.direction == +1 for e in e4s)
    first, second = e4s
    assert first.attrs["pb_price"] == pytest.approx(100.52)
    assert first.attrs["leg_start"] == pytest.approx(102.33)
    assert second.attrs["pb_price"] == pytest.approx(101.32)
    assert second.attrs["leg_start"] == pytest.approx(103.28)
    # A7:深度窗口以激活 bar 的 atr 度量;触发收盘 > L_pb + 0.5·D;守最近 HL
    lo_atr, hi_atr = P2.e4_depth_atr
    d = second.attrs["depth"]
    assert d == pytest.approx(103.28 - 101.32)
    assert lo_atr * atrs[19] <= d <= hi_atr * atrs[19]
    assert bars[20][4] > 101.32 + 0.5 * d
    assert 101.32 > 100.52                      # L_pb 守住定义趋势的最近 HL


def test_e4_control_no_recovery_within_window():
    bars = []
    trend_up_prefix(bars)
    seg(bars, 102.3, 101.35)                       # 17,18 回调(L_pb=101.32@19)
    seg(bars, 102.45, 102.2, 102.25, 102.28, 102.6)  # 19-23:收盘始终 < 50% 回收位
    det = run(bars)
    late = [e for e in by_id(det.events, E4) if e.known_index >= 19]
    assert late == [], "N_f 窗口内未回收 50% 不得发 E4(23 迟到的收复已过窗)"
    got15 = [e for e in by_id(det.events, E4) if e.known_index == 15]
    assert len(got15) == 1, "对照组不应误伤前序合法 E4(L2 腿 @15)"


# ---------------------------------------------------------------------------
# E5 RANGE_ROTATION(§6, A8)——冻结 config(atr_n=14)
# ---------------------------------------------------------------------------


def range_bars():
    p = Params.from_json(CONFIG)
    bars = []
    calm(bars, 100.0, 0.05, 16)                 # ATR 自 13 起可用(0.1)
    seg(bars, 100.9, 101.7, 100.9, 100.0)       # 16-19
    seg(bars, 100.9, 101.7, 100.85, 100.0)      # 20-23
    seg(bars, 100.86, 101.65)                   # 24,25
    seg(bars, 100.9, 100.0)                     # 26,27
    seg(bars, 100.85)                           # 28
    bar(bars, 100.85, 101.68, 100.82, 101.65)   # 29:触碰上缘区域
    bar(bars, 101.65, 101.68, 101.35, 101.45)   # 30:回退 → E3(-1) + E5(-1)
    return p, bars


def test_e5_range_rotation_positive():
    p, bars = range_bars()
    det2, atrs = run_with_atrs(bars, p)
    by_bar = {}
    det3 = EventDetector(p)
    for i, b in enumerate(bars):
        for e in det3.update(b):
            by_bar.setdefault(i, []).append(e)
    e5s = by_id(det2.events, E5)
    assert [e.known_index for e in e5s] == [30], \
        "全路径只有满足 A8 全部条款的那一次 E3 升级为 E5"
    e = e5s[0]
    assert e.direction == -1
    assert e.ts == bars[30][0]
    band_lo, band_hi = e.attrs["band"]
    z = e.attrs["zone"]
    zc = 0.5 * (z.lo + z.hi)
    # A8 条款逐项(阈值以事件 bar 的 atr(30) 计):
    assert atrs[30] > 0
    assert band_hi - band_lo <= p.k_span * atrs[30]      # RANGE 跨度条款(§5)
    assert abs(zc - band_hi) <= p.e5_edge_atr * atrs[30]  # zone 中心贴上缘
    assert abs(zc - band_lo) > p.e5_edge_atr * atrs[30]
    assert e.attrs["room"] == pytest.approx(bars[30][4] - band_lo)
    assert e.attrs["room"] >= p.k_room * atrs[30]         # 对侧空间充足
    # 同 bar 的 E3(-1) 是 E5 的来源(§6 表:E5 = RANGE 中带边缘的 E3)
    e3_same = [x for x in by_bar[30] if x.id == E3]
    assert len(e3_same) == 1 and e3_same[0].direction == -1
    # 早前 E3(22/24/26/28)未升级为 E5:REGIME 非 RANGE 或空间不足
    assert all(e.known_index not in (22, 24, 26, 28) for e in e5s)
    assert len(by_id(det2.events, E3)) >= 4, "场景中确有多次 E3 供 A8 甄别"


def test_e5_control_insufficient_room():
    """窄平衡带:多处 E3 均触发,但对侧空间恒 < k_room·atr → 全程无 E5(A8)。"""
    p = Params.from_json(CONFIG)
    bars = []
    calm(bars, 100.0, 0.05, 16)
    for _ in range(6):
        seg(bars, 100.9, 100.1)     # span ≈ 0.86 ≈ 1.7·atr 的平衡带
    seg(bars, 100.9)
    bar(bars, 100.9, 100.93, 100.87, 100.9)
    bar(bars, 100.9, 100.9, 100.7, 100.75)
    det = run(bars, p)
    assert det.structure.regime() is Regime.RANGE
    assert len(by_id(det.events, E3)) >= 2, "对照场景必须真的多次触发 E3"
    assert by_id(det.events, E5) == [], "空间不足时 E3 不得升级为 E5"


# ---------------------------------------------------------------------------
# E6 STRUCTURE_BREAK(§6, A9)
# ---------------------------------------------------------------------------


def test_e6_structure_break_positive():
    bars = []
    trend_up_prefix(bars)
    seg(bars, 103.0, 100.2)        # 17,18:18 收盘 < L*(100.52) - k_bm·atr → E6 @18
    det = run(bars)
    e6s = by_id(det.events, E6)
    assert len(e6s) == 1
    e = e6s[0]
    assert e.direction == -1            # 上升趋势被破坏,转空警告
    assert e.known_index == 18
    assert e.ts == bars[18][0]
    assert e.attrs["ref"] == pytest.approx(100.52)
    assert e.attrs["regime_before"] == Regime.TREND_UP.value
    l_star = e.attrs["ref"]
    assert bars[18][4] < l_star         # A9:收盘破坏最近 confirmed HL
    # 破坏 bar 后 regime 退出趋势(§5:价格已破坏最近抬升低点)
    assert det.structure.regime() is not Regime.TREND_UP


def test_e6_control_holds_above_hl():
    bars = []
    trend_up_prefix(bars)
    seg(bars, 102.2, 101.0)        # 回调但守住 L* → 无 E6
    det = run(bars)
    assert by_id(det.events, E6) == [], "价格未破坏最近抬升低点不得发 E6"
