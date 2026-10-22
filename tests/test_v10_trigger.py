"""v10 测试(docs/v10-protocol.md §A,冻结):触发麦克风(trigger mic)知时细化。

覆盖(认知帧场景沿用 test_events.py/test_e7_v2.py/test_v8.py 的确定性价格
路径;trigger 帧由 trig_of 合成,逐位聚合回认知帧 —— 测试内硬断言,保证
两帧描述同一市场):
- 知时细化正确性:LEVEL_REJECTED/STRUCTURE_BROKEN/FLIP_RETESTED/BREAKOUT_ACCEPTED 在 trigger 收盘先满足 ⇒ mic 事件知时=该
  trigger 收盘(ts=trigger 标签,known_index=trigger 序号),attrs 同族
  (id 与 bar 事件同族同方向同 ref;mic=True + trigger_tf 标注;阈值=最
  近已收**认知** bar 的 ATR —— invalid 公式的 atr 项钉死 atr(t-1) ≠ atr(t));
- 无前馈:发射之后(含同认知 bar 内更晚的 trigger bar)的任意价格扰动不得
  改变已发射事件流(两段扰动:同 bar 后续根 + 全部未来),逐字段比对;
- 超车早到量:LEVEL_REJECTED 早 30 分钟、STRUCTURE_BROKEN 早 30 分钟、FLIP_RETESTED 早 30 分钟(构造);
  数量闸门:N_a_mic = 3×4 = 12 根连续站稳恰界发射/断于第 12 根前的对照;
  N_f_mic = 3×4 = 12:BREAKOUT_FAILED 收复在窗口内发射 / 窗口外不发射(镜像 A5);
- parity:mic 帧的 bar 事件流 == prepare_cognition 逐字段一致(检测器零改
  动的硬约束);run_campaigns(enable_mic=False) 挂不挂 mic 帧逐位一致;
- 引擎接线:mic BREAKOUT_ACCEPTED 注入 ⇒ 次一 trigger bar 开盘成交(decision_ts=trigger
  标签,meta.mic=True,fill−decision=900s),同现象 bar 事件于认知收盘被槽
  位去重(恰一仓);确定性双跑(认知 + 引擎两层)。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from market_operator.backtest_v6 import SymbolCognition, prepare_cognition
from market_operator.broker import ContractSpec
from market_operator.campaign import (
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.events import (
    BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, STRUCTURE_BROKEN, FLIP_RETESTED, MarketEvent,
)
from market_operator.hypothesis import StructureView
from market_operator.structure import Params, Regime
from market_operator.trigger import MicCognition, prepare_mic_cognition

T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC(对齐网格)
H = 3_600_000
Q = 900_000
P2 = Params(atr_n=2)                   # 编排用小窗口(同 test_events 惯例)
CFG = Params.from_json("config/defaults.json")


def _mk(bars, o, h, l, c):
    bars.append((T0 + H * len(bars), o, h, l, c, 1.0))


def calm(bars, px=100.0, w=0.05, n=1):
    for _ in range(n):
        _mk(bars, px, px + w, px - w, px)


def seg(bars, *closes, w=0.03):
    for c in closes:
        o = bars[-1][4]
        _mk(bars, o, max(o, c) + w, min(o, c) - w, c)


def bar(bars, o, h, l, c):
    _mk(bars, o, h, l, c)


def h1_prefix(bars):
    """calm×6 → 冲高 → 回落确认 H1=100.98@7,阻力区 [100.664,100.98]。"""
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.95, 100.3)          # 6,7;H1 pivot: forming=6, known=7


def trend_up_prefix(bars):
    """4 pivot 递升结构 @14 起 TREND_UP=L2 低 pivot 100.52@14 确认。"""
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.9, 101.8)    # 6,7
    seg(bars, 100.9, 100.4)    # 8(H1=101.83@8),9
    seg(bars, 101.4, 102.3)    # 10(L1=100.37@10),11
    seg(bars, 101.4, 100.55)   # 12(H2=102.33@12),13
    seg(bars, 101.45, 102.35, 103.25)  # 14(L2=100.52@14),15,16


def e3_bars():
    """阻力区触碰(9)→ ex 逐 bar 推进(10/11)→ 回退 ≥ k_rj·atr ⇒ LEVEL_REJECTED @12。"""
    bars = []
    h1_prefix(bars)
    seg(bars, 100.0)                            # 8
    bar(bars, 100.0, 100.78, 99.82, 100.75)     # 9:触碰,ex=100.78
    bar(bars, 100.75, 100.84, 100.72, 100.8)    # 10:ex→100.84
    bar(bars, 100.8, 100.96, 100.76, 100.93)    # 11:ex→100.96
    bar(bars, 100.93, 100.96, 100.6, 100.7)     # 12:回退 ⇒ LEVEL_REJECTED @12
    return bars


def e1_bars():
    """h1_prefix + 破位候选(9)+ 连续 3 根站稳(9/10/11)⇒ BREAKOUT_ACCEPTED @11。"""
    bars = []
    h1_prefix(bars)
    seg(bars, 100.6, 101.3)    # 8;9 = 破位候选
    seg(bars, 101.2, 101.25)   # 10;11 = 第 2、3 根 ⇒ BREAKOUT_ACCEPTED @11
    return bars


def e7_long_bars():
    """阻力破位(8)→ 回踩返回带内(9)→ 向上回退拒绝 ⇒ FLIPPED@9,FLIP_RETESTED|long。"""
    bars = []
    calm(bars, 100.0, 0.05, 6)
    seg(bars, 100.95, 100.3)      # 6(冲高形成 H pivot),7(确认;生成阻力区)
    seg(bars, 101.6)              # 8:收盘 > zone.hi + k_bm·atr → BROKEN
    bar(bars, 101.6, 101.65, 100.95, 101.55)  # 9:回踩(l≤hi)后向上回退 ⇒ FLIPPED
    return bars


# ---------------------------------------------------------------------------
# trigger 帧合成(聚合守恒是硬断言:两帧必须描述同一市场)
# ---------------------------------------------------------------------------


def trig_of(cog_bars, reveal=3, carry_at=None):
    """认知帧 → 15m trigger 帧:reveal=k ⇒ (h,l) 与最终 close 于第 k 根揭示。

    k 之前的 15m 收盘 = 认知 bar 开盘 o(未揭示);k 及其后 = c。聚合(4:1)
    open/high/low/close 逐位回认知帧 —— 由 _assert_reaggregate 钉死。
    """
    out = []
    for i, (ts, o, h, l, c, v) in enumerate(cog_bars):
        k = (carry_at or {}).get(i, reveal)
        closes = [o] * k + [c] * (4 - k)
        opens = [o] + closes[:-1]
        for j in range(4):
            hj = h if j == k else max(opens[j], closes[j])
            lj = l if j == k else min(opens[j], closes[j])
            out.append((ts + Q * j, opens[j], hj, lj, closes[j], v))
    return out


def _assert_reaggregate(cog_bars, trig_bars):
    assert len(trig_bars) == 4 * len(cog_bars)
    for i, (ts, o, h, l, c, _v) in enumerate(cog_bars):
        q = trig_bars[4 * i:4 * i + 4]
        assert q[0][0] == ts and q[0][1] == o
        assert max(x[2] for x in q) == h
        assert min(x[3] for x in q) == l
        assert q[-1][4] == c


def prep(cog_bars, trig_bars, params=P2, **kw):
    _assert_reaggregate(cog_bars, trig_bars)
    return prepare_mic_cognition(cog_bars, trig_bars, params, "SYN",
                                 obs_tf_seconds=3600, trigger_tf_seconds=900,
                                 **kw)


def all_mic(mic, eid=None):
    return [e for _a, evs in sorted(mic.events_by.items())
            for e in evs if eid is None or e.id == eid]


def bar_stream(cog_bars, params=P2):
    return prepare_cognition(pd.DataFrame(
        cog_bars, columns=["ts", "open", "high", "low", "close", "volume"]),
        params, "SYN", enable_e7=True)


def close_ms_cog(i):                       # 认知 bar i 收盘时刻(ms)
    return T0 + H * (i + 1)


def close_ms_trig(a):                      # trigger bar a 收盘时刻(ms)
    return T0 + Q * (a + 1)


# ---------------------------------------------------------------------------
# parity:bar 事件流零改动(检测器只是被复放,不是被改写)
# ---------------------------------------------------------------------------


def _sig(e, atrs):
    z = e.attrs.get("zone")
    return (e.id, e.direction, e.known_index, e.ts,
            e.attrs.get("ref"), e.attrs.get("invalid"),
            e.attrs.get("started"), e.attrs.get("ex"),
            e.attrs.get("puncture_index"), e.attrs.get("from_role"),
            (z.lo, z.hi, str(z.role)) if z is not None else None)


def test_mic_bar_stream_parity_strict():
    cog_bars = e3_bars() + []
    # 复合场景:LEVEL_REJECTED + BREAKOUT_ACCEPTED + STRUCTURE_BROKEN + FLIP_RETESTED 各自价格段顺序拼接(时间标签顺延)
    more = []
    for build in (e1_bars, e7_long_bars):
        off = H * len(cog_bars)
        more += [(b[0] + off, *b[1:]) for b in build()]
        cog_bars += [(b[0] + off, *b[1:]) for b in build()]
    trig = trig_of(cog_bars, reveal=3)     # 全部"晚揭示":聚合仍逐位一致
    mic = prep(cog_bars, trig)
    ref = bar_stream(cog_bars)
    got = sorted(_sig(e, None) for i, evs in mic.bar_events_by.items()
                 for e in evs)
    want = sorted(_sig(e, None) for i, evs in ref.events_by.items()
                  for e in evs)
    assert got == want and len(want) >= 4, \
        "mic 帧的 bar 事件流必须与 prepare_cognition 逐字段一致(BREAKOUT_ACCEPTED-FLIP_RETESTED 零改动)"


# ---------------------------------------------------------------------------
# 知时细化:LEVEL_REJECTED 提前 30 分钟;阈值 = 已收认知 bar ATR
# ---------------------------------------------------------------------------


def test_mic_e3_early_know_time_and_cognitive_atr_threshold():
    cog_bars = e3_bars()
    trig = trig_of(cog_bars, reveal=1)     # 每根 1h 的信息于第 2 根 15m 揭示
    mic = prep(cog_bars, trig)
    ref = bar_stream(cog_bars)
    mic_e3 = all_mic(mic, LEVEL_REJECTED)
    bar_e3 = [e for evs in ref.events_by.values() for e in evs if e.id == LEVEL_REJECTED]
    assert len(bar_e3) == 1 and len(mic_e3) == 1
    e, b = mic_e3[0], bar_e3[0]
    assert e.id == b.id and e.direction == b.direction == -1
    assert e.known_index == 4 * 12 + 1 == 49      # trigger 收盘先满足(回退)
    assert b.known_index == 12                    # bar 事件知时 = 认知收盘
    assert e.attrs["mic"] is True and e.attrs["trigger_tf"] == 900
    assert e.attrs["ref"] == pytest.approx(b.attrs["ref"])      # 同族同 ref
    assert e.attrs["ex"] == pytest.approx(100.96)
    # 阈值钉死:invalid 用最近已收**认知** bar 的 ATR(11),非当期(12)
    z = e.attrs["zone"]
    atr11, atr12 = ref.atrs[11], ref.atrs[12]
    assert atr11 != atr12                        # 场景必须真的有 ATR 演化
    assert e.attrs["invalid"] == pytest.approx(
        e.attrs["ref"] - e.direction * P2.k_bm * atr11)
    assert b.attrs["invalid"] == pytest.approx(
        b.attrs["ref"] - b.direction * P2.k_bm * atr12)
    # 知时提前量 = (12 收盘) − (trigger 49 收盘) = 30 分钟
    advance = close_ms_cog(12) - close_ms_trig(49)
    assert advance == 30 * 60_000
    # 发射时刻只依赖 ≤ 该 trigger 收盘的数据:同 bar 后续根扰动不变(见无前馈)


# ---------------------------------------------------------------------------
# BREAKOUT_FAILED:窗口 N_f_mic = 12(镜像 A5);恰界发射 / 窗外不发射
# ---------------------------------------------------------------------------


def _e2_prefix(hover_c):
    bars = []
    h1_prefix(bars)
    seg(bars, 100.0)                            # 8
    bar(bars, 100.0, 100.78, 99.82, 100.75)     # 9:A2 触碰
    bar(bars, 100.75, 101.06, 100.7, 101.0)     # 10:刺穿 hi+k_bm·atr,收盘未破位
    return bars


def test_mic_e2_reclaim_inside_window_emits():
    cog_bars = _e2_prefix(None)
    bar(cog_bars, 101.0, 101.03, 100.87, 100.9)     # 11:收复(c<hi)⇒ BREAKOUT_FAILED @11
    trig = trig_of(cog_bars, reveal=0)              # 每根信息于首根 15m 揭示
    mic = prep(cog_bars, trig)
    e2s = all_mic(mic, BREAKOUT_FAILED)
    assert len(e2s) == 1
    e = e2s[0]
    assert e.direction == -1
    assert e.known_index == 4 * 11 == 44            # 刺穿 40,收复 44:44−40<12
    assert e.attrs["puncture_index"] == 40
    z = e.attrs["zone"]
    assert max(trig[4 * 10 + j][2] for j in range(4)) > z.hi   # 真刺穿


def test_mic_e2_reclaim_outside_n_f_mic_window_no_event():
    # 先跑一遍拿到认知 ATR,再定悬停价(悬于 hi 与 hi+k_bm·atr 之间)
    pre = _e2_prefix(None)
    ref0 = bar_stream(pre + [(pre[-1][0] + H, 101.0, 101.01, 100.97, 100.99, 1.0)])
    hi = 100.98
    bm_min = min(P2.k_bm * a for a in ref0.atrs[10:])
    hover = hi + 0.4 * bm_min                   # 未收复(≥hi)也未破位(<hi+bm)
    bars = _e2_prefix(None)
    for _ in range(4):                          # 11..14:悬缓冲带(15 根 15m)
        bar(bars, hover, hover + 0.005, hover - 0.005, hover)
    bar(bars, hover, hover, 100.8, 100.9)       # 15:收复(已窗外)
    cog_bars = bars
    assert hover < hi + bm_min and hover > hi
    trig = trig_of(cog_bars, reveal=0)
    mic = prep(cog_bars, trig)
    assert all_mic(mic, BREAKOUT_FAILED) == [], \
        "收复落于 N_f_mic=12 窗外(60−40=20)⇒ 无 mic BREAKOUT_FAILED(镜像 A5 窗口)"
    # 窗口边界反证:同场次把收复挪进窗内(11 根内)则必须发射
    bars2 = _e2_prefix(None)
    bar(bars2, 101.0, 101.03, 100.87, 100.9)    # 11:收复
    for _ in range(4):
        bar(bars2, hover, hover + 0.005, hover - 0.005, hover)
    mic2 = prep(bars2, trig_of(bars2, reveal=0))
    assert len(all_mic(mic2, BREAKOUT_FAILED)) == 1


# ---------------------------------------------------------------------------
# BREAKOUT_ACCEPTED:N_a_mic = N_a×scale = 12 根连续 trigger 站稳;断于第 12 根前不发射
# ---------------------------------------------------------------------------


def test_mic_e1_counting_gate_exactly_12_closes():
    cog_bars = e1_bars()
    ref = bar_stream(cog_bars)
    bar_e1 = [e for evs in ref.events_by.values() for e in evs if e.id == BREAKOUT_ACCEPTED]
    assert len(bar_e1) == 1 and bar_e1[0].known_index == 11
    trig = trig_of(cog_bars, reveal=0)            # 破位于首根 15m 揭示(9.q0)
    mic = prep(cog_bars, trig)
    e1s = all_mic(mic, BREAKOUT_ACCEPTED)
    assert len(e1s) == 1
    e, b = e1s[0], bar_e1[0]
    assert e.attrs["started"] == 4 * 9 == 36      # 候选 = 断位 trigger 收盘第 1 根
    assert e.known_index == 36 + 11 == 47         # 恰第 12 根连续外侧收盘发射
    assert e.direction == b.direction == +1
    assert e.attrs["ref"] == pytest.approx(b.attrs["ref"])
    assert e.attrs["invalid"] == pytest.approx(
        e.attrs["zone"].lo - P2.k_bm * ref.atrs[10])   # 语境 = 最近已收认知 ATR
    # 知时重合(站稳 12 根 ≡ 3 认知 bar 全程):提前 0 —— 不破"最多 45 分钟"上界
    assert close_ms_cog(11) - close_ms_trig(47) == 0

    # 对照:第 12 根收盘失守 ⇒ 不得发射(bar 流同样无 BREAKOUT_ACCEPTED,镜像 A4)
    ctrl = []
    h1_prefix(ctrl)
    seg(ctrl, 100.6, 101.3)
    seg(ctrl, 101.2)
    bar(ctrl, 101.2, 101.25, 100.4, 100.5)      # 11:收盘跌回带内 ⇒ 候选作废
    mic_c = prep(ctrl, trig_of(ctrl, reveal=0))
    assert all_mic(mic_c, BREAKOUT_ACCEPTED) == []
    ref_c = bar_stream(ctrl)
    assert [e for evs in ref_c.events_by.values() for e in evs if e.id == BREAKOUT_ACCEPTED] == []


# ---------------------------------------------------------------------------
# STRUCTURE_BROKEN:破坏收盘在 trigger 帧先满足(趋势结构锁定认知帧,ref 逐位相同)
# ---------------------------------------------------------------------------


def test_mic_e6_early_break_ref_identical():
    cog_bars = []
    trend_up_prefix(cog_bars)
    seg(cog_bars, 103.0, 100.2)       # 17,18:收盘 < L*(100.52) − k_bm·atr ⇒ STRUCTURE_BROKEN@18
    ref = bar_stream(cog_bars)
    bar_e6 = [e for evs in ref.events_by.values() for e in evs if e.id == STRUCTURE_BROKEN]
    assert len(bar_e6) == 1 and bar_e6[0].known_index == 18
    trig = trig_of(cog_bars, reveal=3, carry_at={18: 1})   # 破坏收盘于 18.q1 揭示
    mic = prep(cog_bars, trig)
    e6s = all_mic(mic, STRUCTURE_BROKEN)
    assert len(e6s) == 1, "首个满足破坏的 trigger 收盘发射一次(后续 episode 另计)"
    e, b = e6s[0], bar_e6[0]
    assert e.id == b.id and e.direction == b.direction == -1
    assert e.known_index == 4 * 18 + 1 == 73
    assert e.attrs["ref"] == pytest.approx(b.attrs["ref"]) == pytest.approx(100.52)
    assert e.attrs["regime_before"] == Regime.TREND_UP.value
    assert e.attrs["invalid"] == pytest.approx(
        100.52 + P2.k_bm * ref.atrs[17])       # 语境 atr(17),非 atr(18)
    assert close_ms_cog(18) - close_ms_trig(73) == 30 * 60_000


# ---------------------------------------------------------------------------
# FLIP_RETESTED:回踩拒绝在 trigger 帧先确认(翻转仍是认知帧结构事实,双发如实)
# ---------------------------------------------------------------------------


def test_mic_e7_flip_retest_early():
    cog_bars = e7_long_bars()
    ref = bar_stream(cog_bars)
    bar_e7 = [e for evs in ref.events_by.values() for e in evs if e.id == FLIP_RETESTED]
    assert len(bar_e7) == 1 and bar_e7[0].known_index == 9
    trig = trig_of(cog_bars, reveal=3, carry_at={9: 1})  # 回踩+回退于 9.q1 完成
    mic = prep(cog_bars, trig)
    e7s = all_mic(mic, FLIP_RETESTED)
    assert len(e7s) == 1
    e, b = e7s[0], bar_e7[0]
    assert e.id == b.id and e.direction == b.direction == +1
    assert e.known_index == 4 * 9 + 1 == 37
    assert e.attrs["from_role"] == b.attrs["from_role"] == "resistance"
    assert e.attrs["ref"] == pytest.approx(b.attrs["ref"])
    assert e.attrs["atr"] == pytest.approx(ref.atrs[8])   # 语境 atr(8),非 atr(9)
    assert b.attrs["atr"] == pytest.approx(ref.atrs[9])
    assert close_ms_cog(9) - close_ms_trig(37) == 30 * 60_000


# ---------------------------------------------------------------------------
# 无前馈(钉死):发射后任意扰动不得改变已发射事件流
# ---------------------------------------------------------------------------


def test_mic_no_future_perturbation_intra_bar_and_future():
    cog_bars = e3_bars()
    trig = trig_of(cog_bars, reveal=1)
    base = prep(cog_bars, trig)
    base_seq = sorted((a, e.id, e.direction, e.ts, e.attrs.get("ref"),
                       e.attrs.get("invalid"), e.attrs.get("ex"))
                      for a, evs in base.events_by.items() for e in evs)
    first_a = min(a for a, *_ in base_seq)
    assert first_a == 49

    def seq_upto(seq, a_max):
        return [row for row in seq if row[0] <= a_max]

    # 扰动一:同认知 bar 的更晚 trigger 根(50/51)随意改写 —— 未收盘 bar 不得用
    pert = [b if k <= 49 else (b[0], 13.7, 9900.0, 0.01, 55.5, b[5])
            for k, b in enumerate(trig)]
    mic_a = prep_cog = prepare_mic_cognition(
        cog_bars, pert, P2, "SYN", obs_tf_seconds=3600, trigger_tf_seconds=900)
    seq_a = sorted((a, e.id, e.direction, e.ts, e.attrs.get("ref"),
                    e.attrs.get("invalid"), e.attrs.get("ex"))
                   for a, evs in mic_a.events_by.items() for e in evs)
    assert seq_upto(seq_a, 49) == seq_upto(base_seq, 49), \
        "同认知 bar 内更晚的 trigger bar(当时未收盘)不得影响已发射事件"
    # 扰动二:全部未来 trigger bar 任意改写 —— 发射前缀逐字段不变
    pert2 = [b if k <= 49 else (b[0], 999.9, 1999.9, 0.5, 777.7, b[5])
             for k, b in enumerate(trig)]
    mic_b = prepare_mic_cognition(cog_bars, pert2, P2, "SYN",
                                  obs_tf_seconds=3600, trigger_tf_seconds=900)
    seq_b = sorted((a, e.id, e.direction, e.ts, e.attrs.get("ref"),
                    e.attrs.get("invalid"), e.attrs.get("ex"))
                   for a, evs in mic_b.events_by.items() for e in evs)
    assert seq_upto(seq_b, 49) == seq_upto(base_seq, 49)


# ---------------------------------------------------------------------------
# 确定性(认知层双跑)
# ---------------------------------------------------------------------------


def test_mic_prep_determinism_double_run():
    cog_bars = e3_bars()
    trig = trig_of(cog_bars, reveal=1)
    m1 = prep(cog_bars, trig)
    m2 = prep(cog_bars, trig)

    def dump(m):
        return json.dumps(
            {a: [(e.id, e.direction, e.ts,
                  {k: v for k, v in e.attrs.items() if k != "zone"})
                 for e in evs] for a, evs in sorted(m.events_by.items())},
            sort_keys=True, default=str)

    assert dump(m1) == dump(m2)
    assert m1.bar_events_by.keys() == m2.bar_events_by.keys()


# ---------------------------------------------------------------------------
# 引擎接线:2h 战役 + 注入 mic 事件 ⇒ 次一 trigger 开盘成交 + 同现象去重
# ---------------------------------------------------------------------------

SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(segment="full", dev_frac=0.7, equity0=300.0, risk_frac=0.0075,
          leverage_cap=5.0, taker_fee=0.0006, maker_fee=0.0002,
          slippage_per_side=0.0002, funding_per_8h=0.0001,
          cost_multiplier=1.0, contracts={"TEST": SPEC},
          params=P2, n_fill=12, portfolio_cap_frac=0.03)
ATR = 2.0
VIEW2H = StructureView(regime=Regime.TRANSITION, zones=(), atr=ATR)


def _engine_frames(with_mic):
    """2h = 8×15m;认知 bar 2 注入 bar BREAKOUT_ACCEPTED;mic(可选)注入同现象 trigger BREAKOUT_ACCEPTED @17。"""
    n_trig = 64
    rows = []
    for i in range(n_trig):
        c = 96.5
        rows.append((T0 + i * Q, c, c + 0.2, c - 0.2, c, 1.0))
    df_exec = pd.DataFrame(
        [(T0 + i * H, rows[i * 4][1], max(r[2] for r in rows[i * 4:i * 4 + 4]),
          min(r[3] for r in rows[i * 4:i * 4 + 4]), rows[i * 4 + 3][4], 1.0)
         for i in range(16)],
        columns=["ts", "open", "high", "low", "close", "volume"])
    cog_bars = [(T0 + i * 2 * H,
                 df_exec.open[i * 2], max(df_exec.high[i * 2:i * 2 + 2]),
                 min(df_exec.low[i * 2:i * 2 + 2]), df_exec.close[i * 2 + 1], 1.0)
                for i in range(8)]
    bar_e1 = MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 2 * 2 * H, known_index=2,
                         attrs={"ref": 95.0, "invalid": 94.0})
    cog = SymbolCognition(symbol="TEST", bars=cog_bars, atrs=[ATR] * 8,
                          events_by={2: [bar_e1]}, pivots_by={},
                          views={2: VIEW2H})
    mic = None
    if with_mic:
        mic_e1 = MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 17 * Q, known_index=17,
                             attrs={"ref": 95.0, "invalid": 94.0,
                                    "mic": True, "trigger_tf": 900})
        mic = MicCognition(
            symbol="TEST", obs_tf_seconds=7200, trigger_tf_seconds=900,
            scale=8, bars=rows, cog_index_of=[i // 8 for i in range(64)],
            atrs=[ATR] * 64, events_by={17: [mic_e1]}, views={17: VIEW2H},
            bar_events_by={2: [bar_e1]}, counters={"BREAKOUT_ACCEPTED": 1})
    return build_campaign_frame(cog, df_exec, "2h", mic_cog=mic)


def _run_engine(**over):
    kw = dict(KW)
    kw.update(over)
    return kw


def test_engine_mic_fill_next_trigger_open_and_bar_dup_skip():
    f = _engine_frames(with_mic=True)
    res = run_campaigns({campaign_key("TEST", "2h"): f},
                        **_run_engine(enable_mic=True))
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1, "同现象 mic/bar 双发必须由槽位去重为恰一仓"
    row = opens[0]
    assert row["decision_ts"] == T0 + 17 * Q        # 决策 = trigger bar 17 收盘
    assert row["ts"] == T0 + 18 * Q                 # 成交 = 次一 trigger 开盘
    assert row["ts"] - row["decision_ts"] == 900_000
    assert row["meta"]["mic"] is True
    assert row["meta"]["cog_index"] == 2
    # 对照:同现象的 bar BREAKOUT_ACCEPTED(认知 bar 2 收盘)若成交应在 T0+6h;实际被 dup
    assert row["ts"] < T0 + 6 * H
    assert res.counters.get("dup", 0) >= 1
    assert res.counters["opens_filled"] == 1
    assert res.counters["mic_go_events"] == 1


def test_engine_parity_mic_disabled_regardless_of_attachment():
    kw = _run_engine(enable_mic=False)
    r1 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames(False)}, **kw)
    r2 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames(True)}, **kw)
    assert json.dumps(r1.ledger, sort_keys=True) \
        == json.dumps(r2.ledger, sort_keys=True), \
        "enable_mic=False ⇒ 挂载 mic 帧不得改变任何账目"
    opens = [r for r in r1.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1
    assert opens[0]["decision_ts"] == T0 + 2 * 2 * H
    assert opens[0]["ts"] == T0 + 6 * H            # canonical:次一执行(1h)bar 开盘
    assert r1.counters["mic_go_events"] == 0


def test_engine_determinism_double_run_with_mic():
    kw = _run_engine(enable_mic=True)
    r1 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames(True)}, **kw)
    r2 = run_campaigns({campaign_key("TEST", "2h"): _engine_frames(True)}, **kw)
    assert json.dumps(r1.ledger, sort_keys=True) \
        == json.dumps(r2.ledger, sort_keys=True)
    assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)
