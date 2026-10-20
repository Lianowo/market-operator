"""v9 测试(docs/v9-protocol.md §1-§3,冻结):E9 PROCESS_ACCEPTED(过程式接受)
与 E10 PROCESS_REJECTED(过程式拒绝)。

场景为手工价格路径(检测场景用编排窗口 Params(atr_n=2),规则本身不变,
同 test_events.py 惯例);所有过程量断言数值以手工重算(收盘序列 / episode
根数 / 当根 atr)为准,并标注结构物理:

- E9 阻力正场景(过程接受,顺延续 +1):zone=[100.66375, 100.98]。结构物理:
  bar9 首次触碰即把 atr 泵大(E3 回退阈随 atr 放大),其后 6 根收盘骑在未破
  位残余区间 (hi, hi+k_bm·atr] 内缓慢贴 cap 下滑,atr 按 Wilder(n=2)每根
  近乎减半 ⇒ centroid = (episode 收盘均值 − hi)/atr(t) 于 bar15 越阈 ⇒ E9。
  episode 内逐 bar 核验:close ≤ hi + 0.1·atr(t)(不破位走 E1)、high ≤ cap
  (无刺穿,不走 E2)、low ≥ 100.0?(不,E3 窗内 low ≥ ex − k_rj·atr(t),
  ex = 触碰 bar 高 101.0;窗 = 刺穿后 t−ex_index ≤ N_f=3 即 bars 10-12,
  该三根 low 手工卡上门;bar13 起窗死)。此等"前期放量停留 + 波动衰减"是
  E9 在 k_bm(0.1) < centroid 阈(0.3) 下唯一可达形态,如实记录。
- E10 阻力正场景(过程拒绝,反向 −1):episode 内 3 次影线越缘收盘不落
  (wickfail=3)、occ=0(无一根收盘越缘)、ex@bar10 后 4 根不回撤超阈
  ⇒ E3 死于窗口(t−ex_index > N_f),bar14 放量收与其极端距离 ≥ k_rj·atr
  ⇒ E10。bar14 的回撤同时把 bar10 高确认成 H pivot ⇒ 阻力区按 §4.1 合并
  为 [100.66375, 100.995],episode 按 A3 续绑语义换乘到合并区(触发区 =
  合并后 zone,如实断言 zone hi=100.995 而非 100.98)。
- 支撑镜像 = 阻力路径的 202−p 逐 bar 镜像(检测器角色对称性直接证据,
  同 test_v8.py 惯例)。

E1-E8 parity 由仓库全量 pytest(352 基线)守护:enable_e9e10 默认关、
默认构造与显式关闭等价、开/关对既有事件目录逐位一致(本文件另有专项)。
"""

from __future__ import annotations

import json

import pytest

from market_operator.events import (
    E1,
    E2,
    E3,
    E9,
    E10,
    EventDetector,
)
from market_operator.structure import Params, ZoneRole, ZoneState

BASE_TS = 1_700_000_000_000
H = 3_600_000
P2 = Params(atr_n=2)      # 编排用小窗口;阈值数值全部 config 先验(见场景注释)


def mk(bars, o, h, l, c):
    bars.append((BASE_TS + H * len(bars), o, h, l, c, 1.0))


def calm(bars, px=100.0, w=0.05, n=1):
    for _ in range(n):
        mk(bars, px, px + w, px - w, px)


def seg(bars, *closes, w=0.03):
    for c in closes:
        o = bars[-1][4]
        mk(bars, o, max(o, c) + w, min(o, c) - w, c)


def h1_prefix(bars):
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.95, 100.3)          # H1=100.98@6 确认于 7;阻力区 [100.664, 100.98]


def mirror(bars, shift=202.0):
    """价格镜像 o/h/l/c → shift−o/shift−l/shift−h/shift−c(支撑↔阻力对称)。"""
    return [(ts, shift - o, shift - l, shift - h, shift - c, v)
            for ts, o, h, l, c, v in bars]


def run(bars, **kw):
    det = EventDetector(P2, enable_e9e10=True, **kw)
    for b in bars:
        det.update(b)
    return det


def by_id(events, eid):
    return [e for e in events if e.id == eid]


def event_key(e):
    """parity/确定性用:全部数值字段的稳定串行化(zone 以边界等字段入)。"""
    a = {}
    for k, v in e.attrs.items():
        if k == "zone":
            a[k] = (v.lo, v.hi, v.role.value, v.state.value,
                    v.touches, v.rejects, v.origin_known)
        else:
            a[k] = v
    return {"id": e.id, "d": e.direction, "ki": e.known_index, "a": a}


# ---------------------------------------------------------------------------
# 场景一:E9 阻力(fire @15,无缝贴合 cap 的停留-衰减梯)
# 逐 bar 手工核验(全在目标 zone [100.66375, 100.98] 的 episode 内):
#   bar9  触碰(pc 100.0 < lo;h 101.0 ≥ lo);close 100.96 ≤ hi ⇒ occ=0;
#         h=101.0 即 episode ex(E3/过程量共用起点);big tr 把 atr9 泵到
#         0.7981 ⇒(E3 阈放大、后续 cap 放大)。
#   bar10-12  收盘 101.02/101.008/100.996 逐根 > hi 且 ≤ hi+0.1·atr(t)
#         (不破位:101.02 ≤ 100.98+0.0467、101.008 ≤ 101.0284、
#         100.996 ≤ 100.9970);low ≥ 101.0 − 0.3·atr(t)(E3 窗内不死:
#         0.135<0.140、0.081<0.085、0.049<0.051);
#   bar13-15  E3 窗死(t−ex ≥ 4)后 atr 继续 ~减半,收盘仍 > hi(occ 满
#         6/7 超 2/3),cap 余量内取值;bar15 centroid 越 0.3 ⇒ E9@15。
# ---------------------------------------------------------------------------


def e9_bars():
    bars = []
    h1_prefix(bars)
    mk(bars, 100.3, 100.33, 99.97, 100.0)           # 8
    mk(bars, 100.0, 101.0, 99.9, 100.96)            # 9 触碰(atr 泵,ex=101.0)
    mk(bars, 100.96, 101.0, 100.8647, 101.02)       # 10
    mk(bars, 101.02, 101.0, 100.9191, 101.008)      # 11
    mk(bars, 101.008, 101.0, 100.9515, 100.996)     # 12(E3 窗最后根)
    mk(bars, 100.996, 100.99, 100.982, 100.989)     # 13
    mk(bars, 100.989, 100.989, 100.9805, 100.984)   # 14
    mk(bars, 100.984, 100.9845, 100.979, 100.9825)  # 15:occ=6/7,centroid 越阈
    return bars


E9_CLOSES = [100.96, 101.02, 101.008, 100.996, 100.989, 100.984, 100.9825]


def test_e9_process_accepted_resistance_positive():
    det = run(e9_bars())
    e9s = by_id(det.events, E9)
    assert len(e9s) == 1, "episode 内至多一枚 E9(协议§2 单次发射)"
    e = e9s[0]
    assert e.direction == +1            # 阻力区过程式接受 = 顺延续做多(§2)
    assert e.known_index == 15
    assert e.ts == e9_bars()[15][0]
    a = e.attrs
    z = a["zone"]
    assert z.role is ZoneRole.RESISTANCE
    assert (z.lo, z.hi) == pytest.approx((100.66375, 100.98))
    assert a["edge"] == pytest.approx(100.98)       # 远缘 = zone.hi(阻力)
    assert a["ref"] == a["edge"]                    # §7 目标搜索锚口径
    assert a["age"] == 6                            # a = t − opened(触=0)
    # occ:episode 7 根中 6 根收盘 > hi(bar9 收 100.96 未越)⇒ 6/7
    assert a["occ"] == pytest.approx(6 / 7)
    # centroid:只用 episode 内收盘价(手工重算均值)
    mean_c = sum(E9_CLOSES) / len(E9_CLOSES)
    assert mean_c == pytest.approx(100.99135714285714)
    atr = a["atr"]
    assert a["centroid"] == pytest.approx((mean_c - 100.98) / atr)
    assert a["centroid"] >= 0.3                     # §2 重心阈
    assert a["centroid"] == pytest.approx(0.40715155952710447)
    assert a["wickfail"] == 1       # bar9:high 101.0 > hi 但 close ≤ hi(触碰根)
    assert a["ex"] == pytest.approx(101.0)          # 过程极值含当根(§1)
    # invalid = zone.lo − k_bm·atr(整区深度+buf,协议§3)
    assert a["invalid"] == pytest.approx(100.66375 - P2.k_bm * atr)
    assert e9_bars()[15][4] > 100.98                # 当前收穿远缘(§2)
    # 竞争次序:目标 zone 上 episode 全程无 E1/E2/E3(见场景注释三根核验)
    old = [x for x in det.events if x.id in (E1, E2, E3)]
    assert old == []


def test_e9_support_mirror_negative_direction():
    det = run(mirror(e9_bars()))
    e9s = by_id(det.events, E9)
    assert len(e9s) == 1
    e = e9s[0]
    assert e.direction == -1            # 支撑区过程式接受 = 顺延续做空(§2 镜像)
    assert e.known_index == 15
    a = e.attrs
    z = a["zone"]
    assert z.role is ZoneRole.SUPPORT
    assert (z.lo, z.hi) == pytest.approx((202.0 - 100.98, 202.0 - 100.66375))
    assert a["edge"] == pytest.approx(z.lo)         # 支撑远缘 = zone.lo
    assert a["occ"] == pytest.approx(6 / 7)
    mean_c = sum(202.0 - c for c in E9_CLOSES) / len(E9_CLOSES)
    assert a["centroid"] == pytest.approx((z.lo - mean_c) / a["atr"])
    assert a["centroid"] >= 0.3
    assert a["ex"] == pytest.approx(202.0 - 101.0)  # 过程极值镜像(min low)
    # invalid = zone.hi + k_bm·atr(镜像整深+buf)
    assert a["invalid"] == pytest.approx(z.hi + P2.k_bm * a["atr"])


# ---------------------------------------------------------------------------
# 场景二:E10 阻力(fire @14;合并续绑在断言中如实体现)
# 逐 bar 手工核验:bar9 触碰开 episode(ex=100.78@9,occ=0);
#   bar10 影刺 100.995 > hi 收 100.93 ≤ hi(wickfail-1),ex→100.995@10;
#   bar11-13 悬近 ex:low ≥ ex − 0.3·atr(t)(E3 窗 bars 10-13 不死:
#     0.080<0.0865、0.047<0.0495、0.027<0.0279);bar12/13 再刺(wf=3),
#     全部收盘 ≤ hi(occ=0);bar14 收 100.6 ≤ ex − k_rj·atr(t)(窗死,
#     t−ex=4 ⇒ E3 不可达)⇒ E10@14;该回撤确认 bar10 高为 H pivot ⇒
#     阻力区合并 [100.66375, 100.995],episode 按 A3 续绑语义换乘。
# ---------------------------------------------------------------------------


def e10_bars():
    bars = []
    h1_prefix(bars)
    mk(bars, 100.3, 100.33, 99.97, 100.0)           # 8
    mk(bars, 100.0, 100.78, 99.82, 100.75)          # 9 触碰
    mk(bars, 100.75, 100.995, 100.7, 100.93)        # 10 wickfail-1
    mk(bars, 100.93, 100.98, 100.915, 100.96)       # 11
    mk(bars, 100.96, 100.99, 100.948, 100.97)       # 12 wickfail-2
    mk(bars, 100.97, 100.989, 100.968, 100.975)     # 13 wickfail-3
    mk(bars, 100.975, 100.99, 100.6, 100.6)         # 14 收离极端
    return bars


def test_e10_process_rejected_resistance_positive():
    det = run(e10_bars())
    e10s = by_id(det.events, E10)
    assert len(e10s) == 1, "episode 内至多一枚 E10(协议§2 单次发射)"
    e = e10s[0]
    assert e.direction == -1            # 阻力区过程式拒绝 = 反向做空(§2)
    assert e.known_index == 14
    a = e.attrs
    z = a["zone"]
    assert z.role is ZoneRole.RESISTANCE
    # A3 合并续绑:触发区快照 = 合并后 zone(hi 上抬至 bar10 刺穿位 100.995)
    assert (z.lo, z.hi) == pytest.approx((100.66375, 100.995))
    assert a["edge"] == pytest.approx(100.995)      # 远缘按合并后区取
    assert a["age"] == 5                            # 14 − 9
    assert a["occ"] == 0.0                          # 无一根收盘越缘(绝对 0)
    assert a["wickfail"] == 3                       # bars 10/12/13
    assert a["ex"] == pytest.approx(100.995)        # 过程极值(bar10 高)
    atr = a["atr"]
    assert e10_bars()[14][4] <= a["ex"] - P2.k_rj * atr   # §2 回退阈
    assert a["invalid"] == pytest.approx(a["ex"] + P2.k_bm * atr)
    # centroid:episode 收盘均值(6 根)≤ 合并后 hi ⇒ ≤ 0(§2)
    closes = [b[4] for b in e10_bars()[9:15]]
    mean_c = sum(closes) / len(closes)
    assert mean_c == pytest.approx(100.86416666666667)
    assert a["centroid"] == pytest.approx((mean_c - z.hi) / atr)
    assert a["centroid"] == pytest.approx(-0.5416740791055241)
    assert a["centroid"] <= 0.0
    # 目标 zone 上该 episode 全程无 E1/E2/E3(E3 死于 t−ex > N_f 窗口)
    old = [x for x in det.events if x.id in (E1, E2, E3)]
    assert old == []


def test_e10_support_mirror_positive_direction():
    det = run(mirror(e10_bars()))
    e10s = by_id(det.events, E10)
    assert len(e10s) == 1
    e = e10s[0]
    assert e.direction == +1            # 支撑区过程式拒绝 = 反向做多(§2 镜像)
    assert e.known_index == 14
    a = e.attrs
    z = a["zone"]
    assert z.role is ZoneRole.SUPPORT
    assert (z.lo, z.hi) == pytest.approx((202.0 - 100.995, 202.0 - 100.66375))
    assert a["edge"] == pytest.approx(z.lo)
    assert a["occ"] == 0.0
    assert a["wickfail"] == 3
    assert a["ex"] == pytest.approx(202.0 - 100.995)
    assert mirror(e10_bars())[14][4] >= a["ex"] + P2.k_rj * a["atr"]  # 镜像回退
    assert a["invalid"] == pytest.approx(a["ex"] - P2.k_bm * a["atr"])
    assert a["centroid"] <= 0.0


# ---------------------------------------------------------------------------
# 判据闸门(正反隔离对照)
# ---------------------------------------------------------------------------


def test_e9_occ_gate_below_two_thirds_no_event():
    """bar13/14 收回落带内:occ=4/7 < 2/3 ⇒ 无 E9;centroid 仍 ≥ 0.3(闸隔离)。

    隔离构造:bar12 高先压回 cap 之下(100.9965 < hi+0.1·atr12),避免
    跌落收盘在 A5 刺穿窗内先走 E2(E2 属另一判据,不在本反事实考察范围)。
    """
    bars = e9_bars()
    bars[12] = (BASE_TS + 12 * H, 101.008, 100.9965, 100.9515, 100.996, 1.0)
    bars[13] = (BASE_TS + 13 * H, 100.996, 100.988, 100.982, 100.978, 1.0)
    bars[14] = (BASE_TS + 14 * H, 100.978, 100.983, 100.9795, 100.979, 1.0)
    bars[15] = (BASE_TS + 15 * H, 100.979, 100.9823, 100.977, 100.982, 1.0)
    det = run(bars)
    assert by_id(det.events, E9) == []
    ep = [e for e in det._episodes if abs(e.zone.hi - 100.98) < 1e-9][0]
    n_bars = 15 - ep.opened + 1
    occ = ep.occ_count / n_bars
    assert occ == pytest.approx(4 / 7)   # bars 10,11,12,15 越缘
    mean_c = ep.close_sum / n_bars
    assert (mean_c - ep.zone.hi) / det.structure.atr() >= 0.3, \
        "centroid 已达阈 ⇒ 正是 occ 一单闸挡下"


def test_e10_wickfail_gate_one_poke_only_no_event():
    """只留 bar10 一次试探失败(wf=1<2),其余 E10 判据全部在 ⇒ 无 E10。"""
    bars = e10_bars()
    bars[12] = (BASE_TS + 12 * H, 100.96, 100.98, 100.948, 100.97, 1.0)   # h≤hi
    bars[13] = (BASE_TS + 13 * H, 100.97, 100.98, 100.972, 100.975, 1.0)  # h≤hi
    det = run(bars)
    assert by_id(det.events, E10) == []
    ep = det._episodes[0]
    assert ep.wickfail == 1 and ep.occ_count == 0, \
        "仅 wickfail 不足;occ/ex/年龄判据与正场景一致"


def test_e10_occ_gate_any_close_beyond_edge_no_event():
    """bar11 收盘越缘一根(occ=1/6>0)⇒ 无 E10(协议要求 occ=0 绝对零)。"""
    bars = e10_bars()
    bars[11] = (BASE_TS + 11 * H, 100.93, 100.99, 100.915, 100.99, 1.0)
    det = run(bars)
    assert by_id(det.events, E10) == []
    ep = det._episodes[0]
    assert ep.occ_count == 1
    assert ep.wickfail >= 2, "wickfail 仍足 ⇒ 正是 occ 单闸挡下"


def test_min_age_gate_e9_and_e10():
    """min_age 从 config 传入(协议§2 冻结 3):升 1 即挡、原值即发(边界含)。"""
    assert by_id(run(e10_bars(), e9e10_min_age=6).events, E10) == []   # 5<6
    assert len(by_id(run(e10_bars(), e9e10_min_age=5).events, E10)) == 1
    assert by_id(run(e9_bars(), e9e10_min_age=7).events, E9) == []     # 6<7
    assert len(by_id(run(e9_bars(), e9e10_min_age=6).events, E9)) == 1


# ---------------------------------------------------------------------------
# episode 竞争与生命周期
# ---------------------------------------------------------------------------


def test_e10_blocks_e9_timing_competition():
    """协议§2:同 episode E10 已发 ⇒ E9 永不再发(含时序竞争)。

    反事实构造(测试专用宽松阈值:occ_min=0.2 仍起判据作用,centroid_min
    旁路,非协议敏感性清单内取值;注意合并后 hi=100.995,"越缘"即越过
    100.995):E10 情景后续三根爬回收越缘 ⇒ (occ≥0.2, close>hi, a≥min_age)
    的 E9 判据自 bar17 起客观成立(由 run B 反证)。run A(min_age=3):
    E10@14 先发 ⇒ 全程无 E9;run B(min_age=9:bar14 时点 age=5 不够,E10
    永远错过 occ=0 窗口)⇒ 同一路径 E9@18 发射。两臂唯一差别 = E10 是否
    先发,⇒ 是 E10 的无效化条款挡下 E9,而非判据不足。
    """
    bars = e10_bars()
    mk(bars, 100.6, 100.95, 100.58, 100.9)          # 15 带内爬
    mk(bars, 100.9, 101.002, 100.985, 101.005)      # 16 close>hi(100.995)
    mk(bars, 101.005, 101.006, 100.99, 101.004)     # 17:E9 判据齐备@17起
    mk(bars, 101.004, 101.002, 100.988, 101.0)      # 18
    det_a = run(bars, e9_occ_min=0.2, e9_centroid_min=-10.0)
    assert len(by_id(det_a.events, E10)) == 1
    assert by_id(det_a.events, E9) == [], "E10 已发 ⇒ 该 episode 永不发 E9"
    det_b = run(bars, e9_occ_min=0.2, e9_centroid_min=-10.0, e9e10_min_age=9)
    assert by_id(det_b.events, E10) == []
    e9s = by_id(det_b.events, E9)
    assert len(e9s) == 1 and e9s[0].known_index == 18, \
        "反事实成立:E9 判据确曾于该 episode 齐备 ⇒ run A 的缺席即 E10 阻断"


def test_e10_does_not_consume_episode_old_track_intact():
    """A3 语义:研究目录事件不成交、不终结 episode;老轨道其后仍可判发。

    E10@14 之后同 episode 再刺穿(hi 合并后 = 100.995)收回 ⇒ E2@17 照常
    发射(E1-E8 零改动的直接证据之一);且 E10 全程只发射一次。
    """
    bars = e10_bars()
    mk(bars, 100.6, 100.7, 100.55, 100.65)          # 15 退出正面之外
    mk(bars, 100.65, 101.05, 100.6, 101.0)          # 16 刺穿(h > hi+bm)
    mk(bars, 101.0, 101.0, 100.97, 100.98)          # 17 收回(< hi)
    det = run(bars)
    e10s = by_id(det.events, E10)
    assert len(e10s) == 1
    e2s = [e for e in by_id(det.events, E2) if e.known_index == 17]
    assert len(e2s) == 1 and e2s[0].direction == -1
    assert e2s[0].attrs["puncture_index"] == 16
    assert e2s[0].attrs["zone"].hi == pytest.approx(100.995), \
        "E2 与 E10 同一 episode(合并续绑后的同一 zone)"


# ---------------------------------------------------------------------------
# 开关、parity、确定性、因果
# ---------------------------------------------------------------------------


def test_disabled_by_default_and_e1_e8_parity():
    """默认关:零 E9/E10;开/关对 E1-E8 任何输出逐位一致(两场景全路径)。"""
    bars = e9_bars() + [
        (ts + 16 * H, *rest) for ts, *rest in [tuple(b) for b in e10_bars()[7:]]]
    det_off = EventDetector(P2)                      # 默认构造(连 KW 都不给)
    det_off_kw = EventDetector(P2, enable_e9e10=False)
    det_on = EventDetector(P2, enable_e9e10=True)
    for b in bars:
        det_off.update(b)
        det_off_kw.update(b)
        det_on.update(b)
    assert by_id(det_off.events, E9) == [] and by_id(det_off.events, E10) == []
    assert by_id(det_off_kw.events, E9) == []
    assert len(by_id(det_on.events, E9) + by_id(det_on.events, E10)) >= 1, \
        "断言环境有效:开启后确有过程事件"
    ids = lambda evs: [(e.id, e.known_index, e.direction) for e in evs]
    assert ids(det_off.events) == ids(det_off_kw.events)
    keep = [e for e in det_on.events if e.id not in (E9, E10)]
    assert ids(det_off.events) == ids(keep), \
        "enable_e9e10 不得改变 E1-E8 任何输出(id/known_index/direction 逐项)"
    # 数值级 parity:开关两臂 E1-E8 的 attrs 完全一致(含 zone 快照)
    assert ([event_key(e) for e in det_off.events]
            == [event_key(e) for e in keep])


def test_determinism_double_run():
    """确定性:同输入两跑 ⇒ 事件流逐字段一致(含全部过程量浮点)。"""
    bars = e9_bars() + [
        (ts + 16 * H, *rest) for ts, *rest in [tuple(b) for b in e10_bars()[7:]]]
    r1 = run(bars)
    r2 = run(bars)
    s1 = json.dumps([event_key(e) for e in r1.events], sort_keys=True)
    s2 = json.dumps([event_key(e) for e in r2.events], sort_keys=True)
    assert s1 == s2


def test_causality_no_future_data_in_fire_attrs():
    """知时因果:发射 bar 之后追加任何 bar,E9/E10 数值不得改变（前馈无关）。"""
    head = e9_bars()
    tail = [(BASE_TS + 16 * H, 100.9, 100.95, 100.3, 100.4, 1.0),
            (BASE_TS + 17 * H, 100.4, 101.4, 100.3, 101.3, 1.0),
            (BASE_TS + 18 * H, 101.3, 101.35, 99.0, 99.1, 1.0)]
    base = run(head)
    cont = run(head + tail)
    e9_base = by_id(base.events, E9)[0]
    e9_cont = by_id(cont.events, E9)[0]
    assert event_key(e9_base) == event_key(e9_cont), \
        "episode 之后数据绝不得污染已发射事件(occ/centroid/atr 全冻结)"


def test_attrs_contract_and_zone_snapshot():
    """attrs ⊇ {zone,edge,occ,centroid,wickfail,ex,age,invalid,atr}(交付规格);
    zone 为克隆快照而非活引用(此后结构推进不改 attrs)。"""
    det = run(e10_bars())
    e = by_id(det.events, E10)[0]
    need = {"zone", "edge", "occ", "centroid", "wickfail", "ex", "age",
            "invalid", "atr"}
    assert need <= set(e.attrs)
    assert e.attrs["atr"] > 0.0 and e.attrs["occ"] >= 0.0
    assert e.attrs["ref"] == e.attrs["edge"]
    z = e.attrs["zone"]
    assert all(z is not live for live in det.structure._zones.zones), \
        "attrs zone 必须是事件时刻的克隆(与在场区域对象不同一)"
    assert z.state is ZoneState.ACTIVE               # 发射时刻 zone 状态快照
