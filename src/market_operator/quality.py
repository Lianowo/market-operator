"""优质五问(C1)与质量配注(C5)(出处 docs/v14-protocol.md)。

规范地位:本模块是 v14 施工包的"优质才进"强制条款实现 —— 入场前评估
(全部因果:只用事件知时 StructureView 与决策参考价),Q1/Q2/Q5 缺一即无
交易(拒单以 reason 记录于 router notes/counters),Q3/Q4 决定档
(A=1.0×risk,B=0.75×risk),档位只缩放 risk_frac 定仓(杠杆/容量不动)。

五问操作化(解释性决定,逐条标注;先验全部冻结,不许调):
- **Q1 位置正当**:摆动带 B=[L*,H*] = 已 confirmed pivots 中最近 LOW/HIGH
  pivot 价(全名单中最后同型 pivot;口径同 scripts/analyze_entry_position.py
  与 EDGE_RECLAIMED(§1);pivot 严格高低交错 ⇒ StructureView.pivots(≤8 个)与全名单
  的"最近同型"恒等价,因果)。pos = (entry_ref − L*)/(H* − L*);空需
  pos ≥ 0.35,多需 pos ≤ 0.65(边界 inclusive,"错半边不交易"死因簿实证)。
  缺一侧 pivot 或 H*−L* ≤ 0 ⇒ 门不可判 ⇒ fail-closed 拒绝(同 EDGE_RECLAIMED 纪律)。
  entry_ref:市价路由 = 决策 bar 收盘(= intent.ref_price);限价路由 =
  计划限价(FLIP_RETESTED 前缘)——计划入场价丈量位置,与协议"入场前评估"一致。
- **Q2 空间够**:对面结构缘 = 沿交易方向、状态 ∈ hypothesis.TARGET_STATES
  (ACTIVE/RETESTED/FLIPPED)、严格越过 entry_ref 的最近对侧区域近缘(多:
  阻力 z.lo 最小者;空:支撑 z.hi 最大者;§7 目标搜索同口径)。room_R =
  direction·(对面缘 − entry_ref)/|entry_ref − invalid| ≥ k_room_R(1.5,
  先验写死)。对面缘缺失 ⇒ 空间无界 ⇒ Q2 过(拒绝只在"缘在场且太挤");
  risk_px ≤ 0 ⇒ 拒绝。
- **Q3 区域履历**(只做档注,不构强制):k = 触发 zone 的触碰序数
  (zone.touches,冻结 §4.3 事实);k > k_*(=2,v11 dev 冻结断点,
  reports/zonebio.json)即"k 成熟"(v11 厚假说:越被触碰防守越强 ⇒
  供给累积为加分信息);zone 缺失(STRUCTURE_BROKEN 等非区事件)⇒ 不成熟(不加成)。
- **Q4 regime 顺风**:多且 TREND_UP / 空且 TREND_DOWN。
- **Q5 失效定结构 + F1 ≥ 0.8·ATR**:invalid 有限且沿方向距离
  dist = direction·(entry_ref − invalid) ≥ k_is·atr(atr>0);turtle 语义
  dist ≡ turtle_stop_atr·atr(2.0) ⇒ 常态过,条款作 fail-closed 兜底
  (atr ≤ 0 或距离不足即拒,禁止人工加宽,同 `_gates` F1 口径)。

档:"优质才进"强制 = Q1∧Q2∧Q5;A 档 = 强制全过 ∧ Q3 ∧ Q4(=1.0×risk),
B 档 = 强制全过(=0.75×risk);C(不可进)= 强制条款拒单、counters 记
quality_reject_q1_pos / quality_reject_q2_room / quality_reject_q5_f1。

确定性:纯函数 `evaluate_quality` + 无状态档注;`V14Router` 在 quality=None
时逐位同 `MarketRouter`(质量层短路不回读)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .hypothesis import TARGET_STATES
from .routing import RouterParams, MarketRouter
from .structure import PivotKind, Regime, ZoneRole


# ---------------------------------------------------------------------------
# 参数(冻结先验;config/v14.json 同名照抄,不许调)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityParams:
    """C1/C5 冻结先验(协议 §C1/C5 写死;唯一数据来源)。"""

    pos_short_min: float = 0.35      # Q1:空 pos 下限(inclusive)
    pos_long_max: float = 0.65       # Q1:多 pos 上限(inclusive)
    k_room_R: float = 1.5            # Q2:对面缘距离下限(R 倍数)
    k_is: float = 0.8                # Q5:F1 失效距离下限(= canonical k_is)
    k_star: int = 2                  # Q3:k 成熟断点(v11 dev 冻结,k > k_* 成熟)
    tier_a_mult: float = 1.0         # C5:A 档 risk 系数
    tier_b_mult: float = 0.75        # C5:B 档 risk 系数


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityVerdict:
    """五问评估输出(全部字段因果可复算;tradable=False ⇒ 优质才进拒单)。"""

    tradable: bool
    tier: Optional[str]              # "A" / "B" / None(不可进)
    size_mult: float                 # tier 系数(不可进 = 0.0)
    q1_pos: bool
    q2_room: bool
    q3_mature: bool                  # 档注(不参与强制)
    q4_tailwind: bool                # 档注(档 A 要件)
    q5_f1: bool
    pos: Optional[float]
    room_R: Optional[float]          # 对面缘缺失 = None(空间无界,Q2 过)
    rejects: tuple                   # 拒单 reason 码(强制条款,按 Q1→Q2→Q5 序)


def swing_band_pos(pivots: Sequence, entry_ref: float) -> Optional[float]:
    """摆动带位置 pos(协议 Q1 口径;缺侧/退化 ⇒ None,门不可判)。"""
    hi = None
    lo = None
    for pv in reversed(tuple(pivots)):
        if hi is None and pv.kind is PivotKind.HIGH:
            hi = float(pv.price)
        elif lo is None and pv.kind is PivotKind.LOW:
            lo = float(pv.price)
        if hi is not None and lo is not None:
            break
    if hi is None or lo is None or hi - lo <= 0.0:
        return None
    return (float(entry_ref) - lo) / (hi - lo)


def opposite_edge_R(zones: Sequence, direction: int, entry_ref: float,
                    risk_px: float) -> Optional[float]:
    """对面结构缘距离(R)。缘缺失 ⇒ None(空间无界);risk_px ≤ 0 ⇒ -inf。"""
    if risk_px <= 0.0:
        return float("-inf")
    ref = float(entry_ref)
    if direction > 0:
        cands = [float(z.lo) for z in zones
                 if z.role is ZoneRole.RESISTANCE and z.state in TARGET_STATES
                 and float(z.lo) > ref]
        if not cands:
            return None
        return (min(cands) - ref) / risk_px
    cands = [float(z.hi) for z in zones
             if z.role is ZoneRole.SUPPORT and z.state in TARGET_STATES
             and float(z.hi) < ref]
    if not cands:
        return None
    return (ref - max(cands)) / risk_px


def evaluate_quality(p: QualityParams, *, direction: int, entry_ref: float,
                     invalid: float, atr: float, zones: Sequence,
                     pivots: Sequence, regime: Regime,
                     zone_touches: Optional[int]) -> QualityVerdict:
    """优质五问(纯函数):Q1/Q2/Q5 强制,Q3/Q4 定档。"""
    d = int(direction)
    ref = float(entry_ref)
    # Q5(先算:失效/F1 是 risk 口径前提)
    dist = d * (ref - float(invalid))
    q5 = bool(atr > 0.0 and dist >= p.k_is * float(atr))
    # Q1
    pos = swing_band_pos(pivots, ref)
    if pos is None:
        q1 = False
    elif d < 0:
        q1 = bool(pos >= p.pos_short_min)
    else:
        q1 = bool(pos <= p.pos_long_max)
    # Q2
    risk_px = abs(ref - float(invalid))
    room = opposite_edge_R(zones, d, ref, risk_px)
    q2 = room is None or room >= p.k_room_R
    # Q3/Q4(档注)
    q3 = zone_touches is not None and int(zone_touches) > p.k_star
    q4 = ((d > 0 and regime is Regime.TREND_UP)
          or (d < 0 and regime is Regime.TREND_DOWN))
    rejects = tuple(r for r, ok in (("q1_pos", q1), ("q2_room", q2),
                                    ("q5_f1", q5)) if not ok)
    tradable = not rejects
    if not tradable:
        return QualityVerdict(tradable=False, tier=None, size_mult=0.0,
                              q1_pos=q1, q2_room=q2, q3_mature=q3,
                              q4_tailwind=q4, q5_f1=q5, pos=pos, room_R=room,
                              rejects=rejects)
    tier = "A" if (q3 and q4) else "B"
    mult = p.tier_a_mult if tier == "A" else p.tier_b_mult
    return QualityVerdict(tradable=True, tier=tier, size_mult=float(mult),
                          q1_pos=q1, q2_room=q2, q3_mature=q3,
                          q4_tailwind=q4, q5_f1=q5, pos=pos, room_R=room,
                          rejects=())


# ---------------------------------------------------------------------------
# 路由接线(MarketRouter 子类;quality=None ⇒ 逐位同父类)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class V14Params(RouterParams):
    """v6 路由先验 + v14 质量层(默认 None ⇒ 逐位 MarketRouter 行为)。"""

    quality: Optional[QualityParams] = None


class V14Router(MarketRouter):
    """优质才进条款的路由落点:父类产出决策 ⇒ C1 评估 ⇒ 拒单/定档注记。

    - 拒单(强制条款不过):决策丢弃,counters 记 quality_reject_<reason>,
      notes 记完整 reason(协议:"否拒并以 reason 记录");
    - 过单:meta 附 quality_tier / size_mult / quality_pos / quality_room_R
      (C5 配注:broker 定仓读 meta.size_mult;杠杆/容量不动)。
    - quality=None(arm0):短路返回父类决策,逐位 MarketRouter 行为。
    """

    def on_bar(self, symbol: str, bar, bar_index: int, view, atr: float,
               new_events, *, position_open: bool, entry_resting: bool):
        decisions = super().on_bar(symbol, bar, bar_index, view, atr,
                                   new_events, position_open=position_open,
                                   entry_resting=entry_resting)
        qp: Optional[QualityParams] = getattr(self.params, "quality", None)
        if qp is None:
            return decisions
        out = []
        for dn in decisions:
            if dn.kind == "cancel_entry":
                out.append(dn)
                continue
            entry_ref = (float(dn.limit_price) if dn.kind == "limit"
                         else float(dn.intent.ref_price))
            v = evaluate_quality(
                qp, direction=int(dn.hyp.direction), entry_ref=entry_ref,
                invalid=float(dn.hyp.invalid), atr=float(atr),
                zones=(view.zones if view is not None else ()),
                pivots=(view.pivots if view is not None else ()),
                regime=dn.hyp.regime,
                zone_touches=dn.meta.get("zone_touches"))
            self._bump("quality_evaluated")
            side = "long" if dn.hyp.direction > 0 else "short"
            if not v.tradable:
                for r in v.rejects:
                    self._bump(f"quality_reject_{r}")
                self._notes.append(
                    f"优质才进|{symbol} {dn.hyp.event_id}|{side} {dn.route} "
                    f"拒:{','.join(v.rejects)} pos={v.pos} room_R={v.room_R} "
                    f"(强制条款 Q1/Q2/Q5,缺一即无交易)")
                continue
            self._bump("quality_tier_a" if v.tier == "A" else "quality_tier_b")
            dn.meta.update({
                "quality_tier": v.tier, "size_mult": v.size_mult,
                "quality_pos": v.pos, "quality_room_R": v.room_R,
                "quality_q3_mature": v.q3_mature,
                "quality_q4_tailwind": v.q4_tailwind})
            self._notes.append(
                f"优质才进|{symbol} {dn.hyp.event_id}|{side} {dn.route} "
                f"过:tier={v.tier} size_mult={v.size_mult} pos={v.pos} "
                f"room_R={v.room_R} q3={v.q3_mature} q4={v.q4_tailwind}")
            out.append(dn)
        return out


__all__ = [
    "QualityParams", "QualityVerdict", "V14Params", "V14Router",
    "evaluate_quality", "swing_band_pos", "opposite_edge_R",
]
