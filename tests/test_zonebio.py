"""v11 区域发育履历测试(docs/v11-protocol.md;src/market_operator/zonebio.py)。

覆盖:履历拼接正确性(合成 pivot 序列手算 k/dwell/pen/out/终止,含并集行数
续接、FLIPPED 继承、同 bar 出生即 A2 的边界)、观察者对结构层零扰动(parity)、
前瞻无泄漏(out_* 只用触碰结束后窗口内 bar,末端 spike 探针 + 前缀重放对照)、
Spearman 与日块 bootstrap 已知数据断言、dev/test 切点与既有拐点
(research.split_boundary)一致、确定性。

合成场景约定:Params(atr_n=1) ⇒ ATR(t) = TR(t);fixture 逐 bar 自检 TR = 10
(S2/S3 spike bar 除外)⇒ atr_touch 恒 10,手算价格位移 /10 即 ATR 单位。
out 锚 = 外缘(§结构层:阻力的破位外缘 = z.hi,支撑 = z.lo),out_rj 对支撑 =
(窗 max high − z.lo)/atr(跨整带,含带深),out_br = (z.lo − 窗 min low)/atr;
阻力镜像。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_operator.research import split_boundary
from market_operator.structure import MarketStructure, Params
from market_operator.zonebio import (
    K_STAR_CANDIDATES,
    TERMINATIONS,
    _BioZoneBook,
    age_cross_table,
    bootstrap_spearman_ci,
    bucket_table,
    collect_biography,
    decide_k_star,
    spearman,
)

BASE_TS = 1_700_000_000_000
HOUR = 3_600_000

PARAMS = Params(atr_n=1, k_rev=1.0, k_zw=0.5, k_bm=0.1, k_rj=0.3,
                k_span=3.0, k_room=2.0, tol_atr=0.1, range_max_pivots=8,
                e5_edge_atr=0.75, merge_max_atr=2.0)


def _df(bars, start=0):
    rows = [(BASE_TS + HOUR * (start + i), o, h, l, c, 1.0)
            for i, (o, h, l, c) in enumerate(bars)]
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])


def _check_tr10(bars):
    prev_c = None
    for (o, h, l, c) in bars:
        assert l <= min(o, c) and h >= max(o, c), f"OHLC 不合法: {(o, h, l, c)}"
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c)) if prev_c is not None \
            else h - l
        assert abs(tr - 10.0) < 1e-9, f"TR != 10: {(o, h, l, c)} (prev {prev_c})"
        prev_c = c


def s1_bars():
    """S1(TR 恒 10):生成/并集/多段接近/FLIPPED 翻转链/数据末悬挂,见下方注释。"""
    bars = [
        (100, 105, 95, 100),      # b0
        (100, 109, 99, 108),      # b1
        (108, 117, 107, 116),     # b2  定向 up
        (116, 123, 113, 122),     # b3
        (122, 123, 113, 115),     # b4  pivot H(123) ⇒ R1=[118,123]
        (115, 115, 105, 105),     # b5
        (105, 105, 95, 98),       # b6
        (98, 99, 89, 92),         # b7
        (92, 93, 83, 87),         # b8
        (87, 96, 86, 95),         # b9  pivot L(83) ⇒ S1=[83,88]
        (95, 104, 94, 104),       # b10
        (104, 113, 103, 112),     # b11
        (112, 120, 110, 118),     # b12 A2 R1
        (118, 124.5, 114.5, 121),  # b13 滞留
        (121, 122, 112, 114),     # b14 pivot H(124.5) ⇒ 并 uR=[118,124.5]
        (114, 115, 105, 106),     # b15 段合(正面) dwell=3
        (106, 107, 97, 99),       # b16
        (99, 100, 90, 92),        # b17
        (92, 93, 83, 86),         # b18 A2 S1
        (86, 96, 86, 95),         # b19 pivot L(83) ⇒ S 并集;S1 段继续
        (95, 104, 94, 103),       # b20 S 段合(正面) dwell=2
        (103, 112, 102, 111),     # b21
        (111, 119, 109, 117),     # b22 A2 uR(段开)
        (117, 126, 116, 123),     # b23 滞留(per-bar 增量,不开行)
        (123, 131, 121, 130),     # b24 滞留 + BROKEN(c=130>125.5)
        (130, 138, 128, 137),     # b25 段合(远侧) dwell=3
        (137, 144, 134, 143),     # b26
        (143, 149, 139, 148),     # b27
        (148, 153, 143, 152),     # b28
        (152, 156, 146, 155),     # b29
        (155, 156, 146, 150),     # b30 pivot H(156) ⇒ R3=[151,156]
        (150, 151, 141, 144),     # b31 A2 R3
        (144, 145, 135, 137),     # b32 段合(正面) dwell=1
        (137, 138, 128, 131),     # b33
        (131, 132, 122, 126),     # b34 uR RETEST 同根拒 ⇒ FLIP ⇒ F1 支撑[118,124.5]
        (126, 127, 117, 121),     # b35 A2 F1
        (121, 122, 112, 115),     # b36 滞留 + F1 BROKEN(c=115<117)
        (115, 123, 113, 121),     # b37 滞留;F1 RET 即拒 ⇒ G1;pivot L(112) ⇒ S3=[112,117]
        (121, 129, 119, 128),     # b38 滞留;G1 BROKEN(c=128>125.5)
        (128, 130, 120, 126),     # b39 滞留;G1 RET 即拒 ⇒ F2 支撑[118,124.5]
        (126, 127, 117, 118),     # b40 A2: F2/S3;pivot H(130) ⇒ R4=[125,130]
        (118, 119, 109, 110),     # b41 滞留;F2/S3 BROKEN(c=110<117/111)
        (110, 111, 101, 104),     # b42 F1 段合(远侧,dwell=7);F2/S3 段合(远侧,dwell=2)
        (104, 110, 100, 109),     # b43
        (109, 116, 106, 115),     # b44 pivot L(100) ⇒ S4=[100,105];S3 RET 即拒 ⇒ G3
        (115, 116, 106, 112),     # b45 pivot H ⇒ R5u=[111,117] 并 G3
        (112, 120, 110, 119),     # b46 F2 RET 即拒 ⇒ G4;pivot L(106) ⇒ S5=[106,111] 同bar A2
        (119, 125, 115, 124),     # b47 A2 R4;R5u RET 即拒 ⇒ S6 支撑(并 S5)=[106,117]
        (124, 132, 122, 131),     # b48 滞留 R4;G4 BROKEN(c=131>125.5)
        (131, 139, 129, 138),     # b49 滞留 R4(数据末);R4 BROKEN(c=138>131)
    ]
    _check_tr10(bars)
    return bars


def s2_bars_dead():
    """k_rj=30(拒绝阈值不可达,仅为走 DEAD 分支;结构判定本身冻结)。
    S1=[83,88]@9;A2+BROKEN@10(深破,带外缘 lo);深dip不触带@11;
    回带 RETESTED@12;再接受穿回 hi 上方 ⇒ DEAD@13。"""
    return s1_bars()[:10] + [
        (95, 96, 79, 80.4),       # b10 A2 S1(k=2) + BROKEN(c<83-1.7)
        (80.4, 82.9, 80, 82),     # b11 pivot L(79) ⇒ S2=[79,~80.5](不与 S1 重叠)
        (82, 84, 81.5, 83.5),     # b12 S1 RETESTED(LOW pivot 不确认:1.4<TR=2.5)
        (83.5, 89.2, 83.2, 88.9),  # b13 DEAD(c=88.9>88+0.6)
    ]


def _row(df, k, ti):
    hit = df[(df["k"] == k) & (df["touch_index"] == ti)]
    assert len(hit) == 1, f"行不唯一/缺失: k={k} @bar{ti}"
    return hit.iloc[0]


# ---------------------------------------------------------------------------
# 观察者 parity:注入 _BioZoneBook 对结构层行为零扰动
# ---------------------------------------------------------------------------


def test_observer_book_does_not_perturb_structure():
    df = _df(s1_bars())
    ref = MarketStructure(PARAMS)
    obs = MarketStructure(PARAMS)
    obs._zones = _BioZoneBook(PARAMS)
    for row in df.itertuples(index=False):
        bar = (row.ts, row.open, row.high, row.low, row.close, row.volume)
        ref.update(bar)
        obs.update(bar)
        rz, oz = ref.zones, obs.zones
        assert len(rz) == len(oz)
        for a, b in zip(rz, oz):
            assert (a.lo, a.hi, a.role, a.state, a.touches, a.rejects,
                    a.origin_known, a.merged) == \
                   (b.lo, b.hi, b.role, b.state, b.touches, b.rejects,
                    b.origin_known, b.merged)
        assert ref.regime() is obs.regime()
        assert ref.atr() == obs.atr()
    assert [p.price for p in ref.pivots] == [p.price for p in obs.pivots]


# ---------------------------------------------------------------------------
# 履历拼接正确性:S1 手算(行 = A3 接近段;冻结 per-bar 计数另列)
# ---------------------------------------------------------------------------


def test_s1_biography_hand_computed():
    t, z = collect_biography(_df(s1_bars()), PARAMS, symbol="S1")
    assert len(t) == 16, f"S1 应为 16 行: {len(t)}"

    r = _row(t, 1, 4)                        # R1 origin
    assert r["origin"] and r["role"] == "resistance" and r["merged"] == 1
    assert r["touches_frozen"] == 1
    assert r["lo"] == 118.0 and r["hi"] == 123.0 and r["atr_touch"] == 10.0
    assert r["dwell_k"] != r["dwell_k"]      # origin 行 NaN
    assert r["out_rj_k"] == 4.0 and r["out_br_k"] == 3.0 and r["n_fwd"] == 24
    assert r["broken_24h"] and r["zone_end_state"] == "FLIPPED" \
        and r["zone_end_index"] == 34

    r = _row(t, 2, 12)                       # R 链首段接近
    assert not r["origin"] and r["k"] == 2 and r["touches_frozen"] == 2
    assert r["merged"] == 1 and r["regime"] == "TRANSITION"
    assert r["dwell_k"] == 3 and r["exit_side"] == "front"
    assert r["pen_k"] == 0.15                # (124.5-123)/10
    assert r["out_rj_k"] == 4.0 and r["out_br_k"] == 3.3
    assert abs(r["age_days"] - 8 / 24) < 1e-9
    assert r["broken_24h"] and r["zone_end_state"] == "FLIPPED" \
        and r["zone_end_index"] == 34

    r = _row(t, 3, 22)                       # R 链第三行(并集续计)
    assert r["k"] == 3 and r["touches_frozen"] == 4
    assert r["merged"] == 2 and r["lo"] == 118.0 and r["hi"] == 124.5
    assert r["dwell_k"] == 3 and r["exit_side"] == "far"
    assert r["pen_k"] == 0.65                # (131-124.5)/10
    assert r["out_rj_k"] == 2.45 and r["out_br_k"] == 3.15 and r["n_fwd"] == 24
    assert abs(r["age_days"] - 18 / 24) < 1e-9
    assert r["broken_24h"] and r["zone_end_state"] == "FLIPPED" \
        and r["zone_end_index"] == 34

    r = _row(t, 1, 9)                        # S1 origin(支撑:out 锚 = lo=83)
    assert r["origin"] and r["role"] == "support" and r["lo"] == 83.0
    assert r["out_rj_k"] == 7.3 and r["out_br_k"] == 0.0
    assert not r["broken_24h"] and r["zone_end_state"] == "ALIVE" \
        and r["zone_end_index"] == 49

    r = _row(t, 2, 18)                       # S 链第二行
    assert r["dwell_k"] == 2 and r["exit_side"] == "front"
    assert r["pen_k"] == 0.0                 # min low = lo = 83
    assert r["out_rj_k"] == 7.3 and r["out_br_k"] == -1.1
    assert abs(r["age_days"] - 9 / 24) < 1e-9
    assert not r["broken_24h"] and r["zone_end_state"] == "ALIVE"

    r = _row(t, 1, 30)                       # R3 origin
    assert r["lo"] == 151.0 and r["hi"] == 156.0 and r["n_fwd"] == 19
    assert r["out_rj_k"] == 5.6 and r["out_br_k"] == -0.5
    assert not r["broken_24h"] and r["zone_end_state"] == "ALIVE"

    r = _row(t, 2, 31)                       # R3 第二行
    assert r["dwell_k"] == 1 and r["exit_side"] == "front"
    assert r["pen_k"] == -0.5                # (151-156)/10(浅触)
    assert r["out_rj_k"] == 5.6 and r["out_br_k"] == -1.1 and r["n_fwd"] == 18

    r = _row(t, 1, 35)                       # F1(FLIPPED 继承支撑)首行:k 重起
    assert not r["origin"] and r["k"] == 1 and r["touches_frozen"] == 6
    assert r["role"] == "support" and r["merged"] == 2
    assert abs(r["age_days"] - 31 / 24) < 1e-9   # origin 继承(=4)
    assert r["dwell_k"] == 7 and r["exit_side"] == "far"
    assert r["pen_k"] == 0.9                 # (118-109)/10
    assert r["n_fwd"] == 8
    assert r["out_rj_k"] == 2.1 and r["out_br_k"] == 1.8
    assert r["broken_24h"] and r["zone_end_state"] == "FLIPPED" \
        and r["zone_end_index"] == 37

    r = _row(t, 1, 46)                       # S5 origin(同 bar 出生)
    assert r["lo"] == 106.0
    assert r["out_rj_k"] == 3.3 and r["out_br_k"] == -0.9
    assert not r["broken_24h"]
    assert r["zone_end_state"] == "ALIVE"    # 链头 = S6


def test_s1_biography_roles_and_edges():
    t, z = collect_biography(_df(s1_bars()), PARAMS, symbol="S1")
    f2 = t[(t["touch_index"] == 40) & (t["role"] == "support")
           & (t["lo"] == 118.0)]
    assert len(f2) == 1 and f2.iloc[0]["k"] == 1     # F2 首段(新身份重起)
    f2 = f2.iloc[0]
    assert f2["dwell_k"] == 2 and f2["exit_side"] == "far"
    assert f2["pen_k"] == 0.9 and f2["out_rj_k"] == 2.1 and f2["out_br_k"] == 1.8
    assert f2["broken_24h"] and f2["zone_end_state"] == "FLIPPED" \
        and f2["zone_end_index"] == 46
    s3o = t[(t["touch_index"] == 37) & (t["role"] == "support")].iloc[0]
    assert s3o["origin"] and s3o["lo"] == 112.0 and s3o["hi"] == 117.0
    assert s3o["out_rj_k"] == 2.7 and s3o["out_br_k"] == 1.2 \
        and s3o["n_fwd"] == 12
    assert s3o["broken_24h"] and s3o["zone_end_state"] == "FLIPPED" \
        and s3o["zone_end_index"] == 44
    s3 = t[(t["touch_index"] == 40) & (t["role"] == "support")
           & (t["lo"] == 112.0)].iloc[0]
    assert s3["k"] == 2 and s3["dwell_k"] == 2 and s3["pen_k"] == 0.3
    assert abs(s3["age_days"] - 3 / 24) < 1e-9
    r4o = t[(t["touch_index"] == 40) & (t["role"] == "resistance")].iloc[0]
    assert r4o["origin"] and r4o["lo"] == 125.0 and r4o["hi"] == 130.0
    assert r4o["out_rj_k"] == 3.0 and r4o["out_br_k"] == 0.9 \
        and r4o["n_fwd"] == 9
    assert r4o["broken_24h"] and r4o["zone_end_state"] == "BROKEN" \
        and r4o["zone_end_index"] == 49
    r4 = _row(t, 2, 47)
    assert r4["dwell_k"] == 3 and r4["exit_side"] == "data_end"
    assert r4["pen_k"] == 0.9 and r4["n_fwd"] == 0
    assert r4["out_rj_k"] != r4["out_rj_k"]           # 窗空 NaN
    assert r4["broken_24h"]                            # 破位@49 ∈ [47,74)
    s4 = t[t["touch_index"] == 44].iloc[0]
    assert s4["lo"] == 100.0 and s4["out_rj_k"] == 3.9 \
        and s4["out_br_k"] == -0.6
    s5 = t[(t["touch_index"] == 46) & (t["k"] == 2)].iloc[0]
    assert s5["dwell_k"] == 1 and s5["pen_k"] == -0.4 and s5["age_days"] == 0.0
    assert s5["out_rj_k"] == 3.3 and s5["out_br_k"] == -0.9
    assert s5["touches_frozen"] == 2                   # 出生 bar 即被 A2


def test_s1_zone_ledger_and_mixed_terminations():
    _, z = collect_biography(_df(s1_bars()), PARAMS, symbol="S1")
    assert len(z) == 12                        # 链头数
    counts = z["termination"].value_counts().to_dict()
    assert counts["FLIPPED"] == 6 and counts["ALIVE"] == 4 \
        and counts["BROKEN"] == 2 and "DEAD" not in counts
    assert set(z["termination"].unique()) <= set(TERMINATIONS)
    broken = z[z["termination"] == "BROKEN"]
    assert broken["broken_index"].notna().all()   # 悬挂破位必有破位知时


def test_s2_dead_termination():
    p = Params(atr_n=1, k_rev=1.0, k_zw=0.5, k_bm=0.1, k_rj=30.0,
               merge_max_atr=2.0)
    t, z = collect_biography(_df(s2_bars_dead()), p)
    head = z[z["role"] == "support"]
    assert len(head) == 2                        # S1 链(DEAD) + S2(ALIVE)
    h = head[head["termination"] == "DEAD"].iloc[0]
    assert h["broken_index"] == 10 and h["end_index"] == 13
    a2 = _row(t, 2, 10)
    assert a2["broken_24h"]                      # 这一摸就破(broken@10)
    assert a2["dwell_k"] == 1 and a2["exit_side"] == "far"
    assert dict(z["termination"].value_counts()) == {"ALIVE": 3, "DEAD": 1}


# ---------------------------------------------------------------------------
# 前瞻无泄漏:out_* 只用触碰结束后窗口内 bar(S3:单链多段 + 末端 spike)
# ---------------------------------------------------------------------------


def s3_bars(spike_h=500.0):
    """R 链:origin@4,接近段@11/19/26/34/44/52(段 k=2..7;合并只续链);
    b53 spike(h=500)落在 k≥5 段的 out 窗内,k≤4 段窗外。TR 恒 10(除 spike)。"""
    bars = [
        (100, 105, 95, 100), (100, 109, 99, 108), (108, 117, 107, 116),
        (116, 123, 113, 122), (122, 123, 113, 115),   # b4 R1=[118,123]
        (115, 115, 105, 105), (105, 105, 95, 96), (96, 97, 87, 90),
        (90, 100, 90, 99),                             # b8 pivot L(87) ⇒ S1=[82,87]
        (99, 105, 95, 104), (104, 112, 102, 111),
        (111, 119, 109, 116),                          # b11 A2(段开 k=2)
        (116, 122, 112, 114),                          # b12 滞留(段内增量)
        (114, 115, 105, 107),                          # b13 段合(front)+pivot H(122) ⇒ 并[117,123]
        (107, 108, 98, 100), (100, 101, 91, 94),
        (94, 103, 93, 102),                            # b16 pivot L(91) ⇒ S 并[82,91]
        (102, 110, 100, 109), (109, 117, 107, 115),
        (115, 122, 112, 120),                          # b19 A2(段开 k=3)
        (120, 122, 112, 114),                          # b20 滞留+pivot H(122) ⇒ 并(段内增量)
        (114, 115, 105, 108),                          # b21 段合(front)
        (108, 109, 99, 101), (101, 102, 92, 95),
        (95, 104, 94, 103),                            # b24 pivot L(92) ⇒ S 并[82,92]
        (103, 111, 101, 110),
        (110, 117, 107, 116),                          # b26 A2(段开 k=4)
        (116, 122, 112, 114),                          # b27 滞留
        (114, 115, 105, 106),                          # b28 段合(front)+pivot H(122) ⇒ 并
        (106, 107, 97, 99), (99, 100, 90, 93),
        (93, 102, 92, 101),                            # b31 pivot L(90) ⇒ S 并
        (101, 109, 99, 108), (108, 116, 106, 114),
        (114, 121, 111, 119),                          # b34 A2(段开 k=5)
        (119, 122, 112, 112),                          # b35 滞留
        (112, 113, 103, 105),                          # b36 段合(front)+pivot H(121) ⇒ 并[116,123]
        (105, 106, 96, 98), (98, 99, 89, 92), (92, 93, 83, 88),
        (88, 97, 87, 96),                              # b40 pivot L(83) ⇒ S 并[78,92]
        (96, 103, 93, 102), (102, 109, 99, 108),
        (108, 116, 106, 114),                          # b43
        (114, 121, 111, 119),                          # b44 A2(段开 k=6)
        (119, 122, 112, 112),                          # b45 滞留
        (112, 113, 103, 106),                          # b46 段合(front)+pivot H(121) ⇒ 并
        (106, 107, 97, 99), (99, 100, 90, 93),
        (93, 102, 92, 101),                            # b49 pivot L(90) ⇒ S 并
        (101, 110, 100, 109), (109, 116, 106, 114),
        (114, 121, 111, 119),                          # b52 A2(段开 k=7)
        (119, spike_h, 118, 122),                      # b53 spike(泄漏探针)
    ]
    _check_tr10(bars[:-1])
    return bars


def test_s3_chain_progression_and_no_leak():
    t, z = collect_biography(_df(s3_bars()), PARAMS)
    a2r = t[(~t["origin"]) & (t["role"] == "resistance")].sort_values("touch_index")
    assert list(a2r["k"]) == [2, 3, 4, 5, 6, 7]        # 段序数连续
    assert list(a2r["touch_index"]) == [11, 18, 26, 34, 44, 52]
    assert list(a2r["touches_frozen"]) == [2, 5, 8, 11, 13, 15]  # 冻结 per-bar 并存
    assert len(z) == 2 and set(z["termination"]) == {"ALIVE"}

    r = _row(t, 1, 4)                                  # R origin
    assert r["out_rj_k"] == 3.6 and r["out_br_k"] == -0.1
    r = _row(t, 1, 8)                                  # S origin
    assert r["out_rj_k"] == 3.5 and r["out_br_k"] == -0.3

    r = _row(t, 2, 11)
    assert not r["broken_24h"]
    assert r["dwell_k"] == 2 and r["pen_k"] == -0.1 and r["exit_side"] == "front"
    assert r["out_rj_k"] == 3.3 and r["out_br_k"] == -0.1
    assert r["zone_end_state"] == "ALIVE"

    r = _row(t, 3, 18)
    assert r["dwell_k"] == 3
    assert r["out_rj_k"] == 4.0 and r["out_br_k"] == -0.1

    r = _row(t, 4, 26)
    assert r["out_rj_k"] == 4.0 and r["out_br_k"] == -0.1   # 窗外无 spike

    r = _row(t, 5, 34)
    assert r["out_rj_k"] == 4.0 and r["out_br_k"] == 37.7   # spike 在 [36,54)
    assert r["n_fwd"] == 18

    r = _row(t, 6, 44)
    assert r["out_rj_k"] == 3.3 and r["out_br_k"] == 37.7 and r["n_fwd"] == 8

    r = _row(t, 7, 52)                                 # spike 同段,dwell 至数据末
    assert r["dwell_k"] == 2 and r["exit_side"] == "data_end" and r["n_fwd"] == 0
    assert r["pen_k"] == 37.7                          # atr(touch)=TR(b52)=10
    assert r["out_rj_k"] != r["out_rj_k"]              # 窗空 NaN

    # S 侧链也被反复接近,同样按段建行
    a2s = t[(~t["origin"]) & (t["role"] == "support")].sort_values("touch_index")
    assert list(a2s["k"]) == [2, 3, 4, 5, 6]
    s29 = a2s[a2s["touch_index"] == 29].iloc[0]
    assert s29["out_rj_k"] == 41.3                     # spike 在同一 side 窗内

    # 泄漏探针:前缀重放(截 spike 前 53 根),未来数据唯一差异
    t_pre, _ = collect_biography(_df(s3_bars())[:53], PARAMS)
    assert _row(t_pre, 6, 44)["out_br_k"] == -0.2     # [46,53):max=121 ⇒ 无 spike
    assert _row(t_pre, 5, 34)["out_br_k"] == -0.1     # [36,53):max=122
    s29_pre = t_pre[(t_pre["touch_index"] == 29) & (t_pre["role"] == "support")]
    assert s29_pre.iloc[0]["out_rj_k"] == 3.5         # [32,53):无 spike
    for k, ti in ((2, 11), (3, 18), (4, 26)):
        a, b = _row(t, k, ti), _row(t_pre, k, ti)
        assert a["out_rj_k"] == b["out_rj_k"] and a["out_br_k"] == b["out_br_k"]


# ---------------------------------------------------------------------------
# Spearman / bootstrap:已知数据断言与确定性
# ---------------------------------------------------------------------------


def test_spearman_known_values():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    # 平局(x 有结):rx=[1.5,1.5,3,4] ⇒ 手算 ρ = 4.5/√22.5 ≈ 0.9486832980505138
    got = spearman([1, 1, 2, 3], [1, 2, 3, 4])
    assert abs(got - 4.5 / np.sqrt(22.5)) < 1e-12
    assert spearman([1, 1, 1], [1, 2, 3]) != spearman([1, 1, 1], [1, 2, 3])  # 常数 NaN


def test_bootstrap_spearman_ci_determinism_and_bounds():
    k, out, ts = [], [], []
    for d in range(8):                        # 8 个自然日块,近单调含一次倒序
        for j in range(3):
            v = d * 3 + j + 1
            k.append(v)
            out.append(v + (3.0 if (d, j) == (3, 1) else 0.0))
            ts.append(BASE_TS + d * 86_400_000 + j * HOUR)
    p1, ci1 = bootstrap_spearman_ci(k, out, ts, reps=300, seed=7)
    p2, ci2 = bootstrap_spearman_ci(k, out, ts, reps=300, seed=7)
    assert p1 == p2 and ci1 == ci2              # 固定种子 ⇒ 完全确定
    assert 0.0 < p1 < 1.0
    assert ci1[0] <= p1 <= ci1[1] and ci1[0] < ci1[1]  # 非退化且含点估
    _, (lo, hi) = bootstrap_spearman_ci([1, 2], [1, 2], [BASE_TS, BASE_TS + HOUR])
    assert lo != lo and hi != hi                # 单日块 ⇒ CI 未定义(NaN)


# ---------------------------------------------------------------------------
# 分桶/k_*/age 表:已知数据断言
# ---------------------------------------------------------------------------


def test_bucket_table_and_k_star_hand():
    rows = []
    for i, (k, role, rj, brk) in enumerate([
            (1, "resistance", 1.0, False), (2, "resistance", 2.0, False),
            (2, "support", 4.0, True), (5, "resistance", 0.5, True),
            (7, "support", 1.5, False)]):
        rows.append(dict(symbol="X", touch_index=i, ts=BASE_TS + i * HOUR,
                         bio_id=i, k=k, touches_frozen=k, role=role, merged=1,
                         regime="RANGE", age_days=float(i), lo=0.0, hi=1.0,
                         atr_touch=1.0, origin=k == 1, dwell_k=1, pen_k=0.0,
                         exit_side="front", out_rj_k=rj, out_br_k=0.0,
                         n_fwd=24, broken_24h=brk, zone_end_state="ALIVE",
                         zone_end_index=99))
    df = pd.DataFrame(rows)
    bt = bucket_table(df)
    a = lambda role, b: bt[(bt["role"] == role) & (bt["bucket"] == b)].iloc[0]
    assert a("all", "1")["n"] == 1 and a("all", "1")["out_rj_med"] == 1.0
    assert a("all", "2")["n"] == 2 and a("all", "2")["out_rj_med"] == 3.0
    assert a("all", "2")["out_rj_mean"] == 3.0
    assert a("all", "≥5")["n"] == 2 and a("all", "≥5")["out_rj_mean"] == 1.0
    assert a("all", "≥5")["break24_rate"] == 0.5
    assert a("all", "≥5")["survive24_rate"] == 0.5
    assert a("support", "2")["out_rj_med"] == 4.0
    assert a("resistance", "2")["out_rj_med"] == 2.0
    at = age_cross_table(df)
    assert at[(at["age"] == "1-7d") & (at["bucket"] == "2")]["n"].iloc[0] == 1
    dec = decide_k_star(df)
    assert set(dec["candidates"]) == {str(c) for c in K_STAR_CANDIDATES}
    # c=2: fresh{k≤2} 中位 2.0,stale{k>2} 中位 (0.5,1.5)=1.0 ⇒ sep=1.0
    # c=3: 同集 ⇒ sep=1.0;并列 ⇒ 取小者
    assert dec["candidates"]["2"]["separation"] == 1.0
    assert dec["k_star"] == 2


# ---------------------------------------------------------------------------
# dev/test 切点与既有拐点一致 + 真实数据不变量 + 确定性
# ---------------------------------------------------------------------------


def test_collect_deterministic():
    df = _df(s1_bars())
    t1, z1 = collect_biography(df, PARAMS)
    t2, z2 = collect_biography(df, PARAMS)
    pd.testing.assert_frame_equal(t1, t2)
    pd.testing.assert_frame_equal(z1, z2)


def test_real_data_boundary_and_invariants():
    from market_operator import data as mo_data
    df = mo_data.load("BTCUSDT")              # 17,600 根 1h
    n = len(df)
    boundary = split_boundary(n, 0.7)
    assert boundary == 12320                  # 与既有事件研究同一拐点
    t, z = collect_biography(df, Params.from_json("config/canonical.json"),
                             symbol="BTCUSDT")
    assert len(t) > 300 and len(z) > 0        # 真实样本量(下界保守)
    dev = t[t["touch_index"] < boundary]
    test = t[t["touch_index"] >= boundary]
    assert 0 < len(dev) < len(t) and len(test) > 0
    a2 = t[~t["origin"]]
    assert (a2["dwell_k"] >= 1).all()
    assert (a2["touches_frozen"] >= 2).all()
    assert (t["n_fwd"] <= 24).all()
    assert set(t["zone_end_state"].unique()) <= set(TERMINATIONS)
    assert set(t["exit_side"].dropna().unique()) <= {"front", "far", "data_end"}
    assert set(t["role"].unique()) == {"resistance", "support"}
