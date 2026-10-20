"""v16 收割婚配 + 位置/空间/F1 三闸集成(docs/v16-protocol.md,冻结 2026-10-20)。

规范地位:本模块是 v16 协议"入场三闸 + 档 + 家族执行差分 + 一生一表收
割"在路由侧的落点(V14Router/V15Router 同型构造;v16=None ⇒ 逐位
V6Router,arm0 回归钉死)。持仓侧(收割模式/时间帽)在 position.py,提交
侧(fade maker / 续涨 stop-entry)沿用 campaign ArmConfig 的 C2/C3 通道。

协议条款 → 实现映射:
- **闸(缺一不存在交易;拒单以 reason 记录于 router notes/counters)**:
  ① 位置:摆动带 pos 空 ≥0.35 / 多 ≤0.65(quality.swing_band_pos 同口径,
  边界 inclusive,缺侧/退化 fail-closed);
  ② 空间:对面结构缘距离 ≥ k_room_R(1.5R);对面缘缺失 ⇒ 空间无界 ⇒ 过
  (拒绝只在缘在场且太挤,同 quality Q2 纪律);缘取法 = opposite_edge_price
  (与 quality.opposite_edge_R 同一搜索口径:沿交易方向、状态 ∈
  hypothesis.TARGET_STATES、严格越过 entry_ref 的最近对侧区域近缘);
  ③ 失效 = 结构区缘 ∓ k_bm·ATR(= 事件 attrs["invalid"] §7 建议口径:E1
  正缘外侧 / E2/E3 外缘外侧 / E8 刺穿极值外侧 / E6 结构 pivot±buf / E7
  远端边缘;v16 以此为唯一失效锚,替换 canonical turtle 2·ATR)且 F1:
  dist(entry_ref, invalid) ≥ k_is·ATR(0.8;dist ≤ 0 = 非保护性失效 ⇒
  拒,fail-closed;ATR 取决策 bar)。
  entry_ref 口径同 quality Q1:市价/续涨路由 = 决策 bar 收盘,限价路由 =
  计划限价(fade ⇒ 计划 maker 前缘,E7 ⇒ G3 前缘)。
- **档(不挡)**:regime 顺风 ⇒ A×1.0,其余 ⇒ B×0.75(size_mult 经既有 C5
  meta 通道缩放 risk_frac 定仓;杠杆/容量不动);k(v11 厚假说 zone_touches
  > k_*=2)只作档注,不参与定档(与 v14 的 Q3∧Q4 定 A 不同,协议原文)。
- **执行差分(协议 §入场-执行)**:fade(E2/E3/E8)决策改写为 kind="limit"
  计划限价 = zone 前缘(接近侧缘:空 = zone.lo / 多 = zone.hi,与 E7 G3 及
  v14 C4 分层"前缘"同口径),由 campaign C2 通道(exchange_maker,
  c2_family=fade)真挂单,N_fill=12;续涨(E1/E6/E7)由 C3 通道 stop-entry
  预埋(ref ± k_bm·ATR,超时 12×TF 作废);E4/E5 不在两族 ⇒ canonical 市价
  兜底。其余成交价/费/滑点/容量沿 canonical 不变。
- **收割婚配(协议 §持仓)**:fade 持仓 target_1 = 对面结构缘(=空间闸丈量
  的同一对面缘,k_room=1.5 对面缘=收割目标;到价全平在 position.py 收割
  模式实现,trail 禁用);续涨 target_1=None 保留 turtle(trail 只收紧)。
  对面缘缺失(fade 无收割目标)⇒ 空间闸仍过,counters 记 v16_fade_no_
  target,持仓由失效/CHoCH/时间帽管理。

  [fade target 取法注明]对面结构缘 ←→ zone 源数据存在两种自然取法:(a)
  Q2/opposite_edge_R 同口径的"最近对侧区域近缘"(沿交易方向越过 entry_ref
  搜索视图 zones);(b)事件 attrs 自带 zone 的"对侧缘"。按任务裁决取
  **(a)**(与质量 Q2 同源同口径,"k_room=1.5 对面缘 = 收割目标"即此缘),
  fail-closed 说明:attrs zone 缺(E6 类)或对面缘缺失时 fade 不编造目标,
  target_1=None 如实记录;与既有 hypothesis._structural_target 的差仅在
  搜索锚(本实现锚 entry_ref 计划入场价,hypothesis 锚 attrs["ref"])与
  返回量(本实现取缘价本身)。

确定性:纯函数 evaluate_v16 / opposite_edge_price / fade_front_edge +
无状态注记;拒单/定档/改单全在因果内(事件知时结构视图 + 决策 bar)。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence

from .hypothesis import TARGET_STATES
from .quality import swing_band_pos
from .strategy_v6 import RouteDecision, V6Params, V6Router
from .structure import Regime, ZoneRole


# ---------------------------------------------------------------------------
# 参数(冻结先验;config/v16.json 同名照抄,不许调)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarvestParams:
    """v16 收割婚配冻结先验(协议 §入场/§持仓写死;唯一数据来源)。"""

    pos_short_min: float = 0.35      # 闸①位置:空 pos 下限(inclusive)
    pos_long_max: float = 0.65       # 闸①位置:多 pos 上限(inclusive)
    k_room_R: float = 1.5            # 闸②空间:对面结构缘距离下限(R 倍数)
    k_is: float = 0.8                # 闸③F1:失效距离下限(ATR 倍数)
    k_star: int = 2                  # 档注:k 成熟断点(v11,k > k_* 成熟,不参档)
    tier_a_mult: float = 1.0         # 顺风档 A risk 系数
    tier_b_mult: float = 0.75        # 其余档 B risk 系数
    k_cap_hours: int = 48            # 时间帽(×TF 小时;经 ArmConfig 入持仓侧)
    cap_mfe_min: float = 0.5         # 时间帽 MFE 恰界(inclusive 持有)
    fade_family: tuple = ("E2", "E3", "E8")


@dataclass(frozen=True)
class V16Params(V6Params):
    """v6 路由先验 + v16 收割婚配层(默认 None ⇒ 逐位 V6Router 行为)。"""

    v16: Optional[HarvestParams] = None


# ---------------------------------------------------------------------------
# 几何(纯函数;对面缘取法见模块 docstring"取法注明")
# ---------------------------------------------------------------------------


def opposite_edge_price(zones: Sequence, direction: int, entry_ref: float
                        ) -> Optional[float]:
    """对面结构缘价(与 quality.opposite_edge_R 同一搜索口径,取缘价本身)。

    沿交易方向、状态 ∈ TARGET_STATES、严格越过 entry_ref 的最近对侧区域
    近缘:多 = 阻力区 lo 最小者;空 = 支撑区 hi 最大者。缺失 ⇒ None(空间
    无界,闸过、收割无目标如实)。
    """
    ref = float(entry_ref)
    if int(direction) > 0:
        cands = [float(z.lo) for z in zones
                 if z.role is ZoneRole.RESISTANCE and z.state in TARGET_STATES
                 and float(z.lo) > ref]
        return min(cands) if cands else None
    cands = [float(z.hi) for z in zones
             if z.role is ZoneRole.SUPPORT and z.state in TARGET_STATES
             and float(z.hi) < ref]
    return max(cands) if cands else None


def fade_front_edge(zone_lo: float, zone_hi: float, direction: int) -> float:
    """fade maker 计划限价 = zone 前缘(接近侧缘,与 E7 G3/v14 C4 同口径):
    空(fade 阻力)= zone.lo;多(fade 支撑)= zone.hi。"""
    return float(zone_lo) if int(direction) < 0 else float(zone_hi)


# ---------------------------------------------------------------------------
# 判定结果与三闸(纯函数)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class V16Verdict:
    """三闸 + 档评估输出(全部字段因果可复算;tradable=False ⇒ 拒单)。"""

    tradable: bool
    tier: Optional[str]               # "A"(顺风)/ "B"(其余)/ None(拒)
    size_mult: float                  # 档系数(拒 = 0.0)
    q1_pos: bool
    q2_room: bool
    f1: bool
    q3_mature: bool                   # 档注(不参与定档)
    q4_tailwind: bool                 # 定档要件(顺风)
    pos: Optional[float]
    room_R: Optional[float]           # 对面缘缺失 = None(空间无界,闸过)
    target: Optional[float]           # 对面结构缘价(=收割目标;缺失 None)
    rejects: tuple                    # 拒单 reason 码(按 q1→q2→f1 序)


def evaluate_v16(p: HarvestParams, *, direction: int, entry_ref: float,
                 invalid: float, atr: float, zones: Sequence,
                 pivots: Sequence, regime, zone_touches: Optional[int]
                 ) -> V16Verdict:
    """入场三闸(位置/空间/F1-结构失效)+ 顺风档(纯函数)。"""
    d = int(direction)
    ref = float(entry_ref)
    inv = float(invalid)
    atr = float(atr)
    # 闸③ F1:dist(entry_ref, invalid) ≥ k_is·ATR(dist ≤ 0 非保护 ⇒ 拒)
    dist = d * (ref - inv)
    f1 = bool(atr > 0.0 and dist >= p.k_is * atr)
    # 闸① 位置
    pos = swing_band_pos(pivots, ref)
    if pos is None:
        q1 = False
    elif d < 0:
        q1 = bool(pos >= p.pos_short_min)
    else:
        q1 = bool(pos <= p.pos_long_max)
    # 闸② 空间:对面缘距离 ≥ k_room_R(缘缺失 ⇒ 无界过)
    risk_px = abs(ref - inv)
    target = opposite_edge_price(zones, d, ref)
    if risk_px <= 0.0:
        room: Optional[float] = float("-inf")
        q2 = False
    elif target is None:
        room = None
        q2 = True
    else:
        room = d * (target - ref) / risk_px
        q2 = bool(room >= p.k_room_R)
    # 档注(Q3 只注不定档;Q4 顺风定 A)
    q3 = zone_touches is not None and int(zone_touches) > p.k_star
    q4 = ((d > 0 and regime is Regime.TREND_UP)
          or (d < 0 and regime is Regime.TREND_DOWN))
    rejects = tuple(r for r, ok in (("q1_pos", q1), ("q2_room", q2),
                                    ("f1", f1)) if not ok)
    tradable = not rejects
    if not tradable:
        return V16Verdict(tradable=False, tier=None, size_mult=0.0,
                          q1_pos=q1, q2_room=q2, f1=f1, q3_mature=q3,
                          q4_tailwind=q4, pos=pos, room_R=room,
                          target=target, rejects=rejects)
    tier = "A" if q4 else "B"
    mult = p.tier_a_mult if tier == "A" else p.tier_b_mult
    return V16Verdict(tradable=True, tier=tier, size_mult=float(mult),
                      q1_pos=q1, q2_room=q2, f1=f1, q3_mature=q3,
                      q4_tailwind=q4, pos=pos, room_R=room,
                      target=target, rejects=())


# ---------------------------------------------------------------------------
# 路由接线(V6Router 子类;v16=None ⇒ 逐位同父类)
# ---------------------------------------------------------------------------


class V16Router(V6Router):
    """v16 收割婚配的路由落点:父类决策 ⇒ fade 前缘改单 ⇒ 三闸 ⇒ 定档 ⇒
    结构锚 hyp 重建(invalid=结构、fade target=对面缘)。

    - fade(E2/E3/E8):市场决策改写为 kind="limit",limit_price = zone 前缘
      (attrs 溯源 zone_lo/zone_hi;缺 zone ⇒ fail-closed 拒,v16_reject_no_zone);
    - invalid:替换为事件结构失效(attrs["invalid"] 经 meta ev_invalid 溯源;
      缺失 ⇒ fail-closed 拒,v16_reject_no_invalid)——协议"一生一表全结构锚";
    - 拒单:决策丢弃,counters 记 v16_gate_reject_q1_pos / _q2_room / _f1,
      notes 记完整 reason(协议"缺一不存在交易");
    - 过单:meta 附 v16_tier / size_mult / v16_pos / v16_room_R / q3/q4 档注 /
      struct_invalid /(fade)harvest_target;hyp 重建 entry=entry_ref、
      invalid=结构锚、target_1=对面缘(fade;续涨 None)、r_mult 重算;
    - v16=None(arm0):短路返回父类决策,逐位 V6Router 行为。
    """

    def on_bar(self, symbol: str, bar, bar_index: int, view, atr: float,
               new_events, *, position_open: bool, entry_resting: bool):
        decisions = super().on_bar(symbol, bar, bar_index, view, atr,
                                   new_events, position_open=position_open,
                                   entry_resting=entry_resting)
        hp: Optional[HarvestParams] = getattr(self.params, "v16", None)
        if hp is None:
            return decisions
        zones = view.zones if view is not None else ()
        pivots = view.pivots if view is not None else ()
        out: list[RouteDecision] = []
        for dn in decisions:
            if dn.kind == "cancel_entry":
                out.append(dn)
                continue
            ev = dn.hyp.event_id
            d = int(dn.hyp.direction)
            side = "long" if d > 0 else "short"
            self._bump("v16_gate_evaluated")
            struct_invalid = dn.meta.get("ev_invalid")
            if struct_invalid is None:
                self._bump("v16_reject_no_invalid")
                self._notes.append(
                    f"v16拒|{symbol} {ev}|{side} {dn.route} 结构失效缺失 "
                    f"(attrs invalid 不在场;fail-closed)")
                continue
            fade = ev in hp.fade_family
            # fade ⇒ maker 前缘改单(计划限价 = zone 接近侧缘)
            if fade:
                if "zone_lo" not in dn.meta or "zone_hi" not in dn.meta:
                    self._bump("v16_reject_no_zone")
                    self._notes.append(
                        f"v16拒|{symbol} {ev}|{side} {dn.route} fade 缺 "
                        f"attrs zone(前缘不可判;fail-closed)")
                    continue
                front = fade_front_edge(float(dn.meta["zone_lo"]),
                                        float(dn.meta["zone_hi"]), d)
                dn = replace(dn, kind="limit", limit_price=front,
                             meta={**dn.meta, "limit_from":
                                   "v16_fade_front_edge"})
            entry_ref = (float(dn.limit_price) if dn.kind == "limit"
                         else float(dn.intent.ref_price))
            v = evaluate_v16(
                hp, direction=d, entry_ref=entry_ref,
                invalid=float(struct_invalid), atr=float(atr), zones=zones,
                pivots=pivots, regime=dn.hyp.regime,
                zone_touches=dn.meta.get("zone_touches"))
            if not v.tradable:
                for r in v.rejects:
                    self._bump(f"v16_gate_reject_{r}")
                self._notes.append(
                    f"v16拒|{symbol} {ev}|{side} {dn.route} "
                    f"拒:{','.join(v.rejects)} pos={v.pos} room_R={v.room_R} "
                    f"entry_ref={entry_ref:.8g} invalid={struct_invalid:.8g} "
                    f"(三闸:位置/空间/F1,缺一不存在交易)")
                continue
            self._bump("v16_tier_a" if v.tier == "A" else "v16_tier_b")
            target = v.target if fade else None
            if fade and target is None:
                self._bump("v16_fade_no_target")
            risk = d * (entry_ref - float(struct_invalid))
            r_mult = (d * (target - entry_ref) / risk
                      if target is not None and risk > 0 else None)
            hyp = replace(dn.hyp, entry=float(entry_ref),
                          invalid=float(struct_invalid), target_1=target,
                          r_mult=r_mult)
            meta = {**dn.meta,
                    "v16_tier": v.tier, "size_mult": v.size_mult,
                    "v16_pos": v.pos, "v16_room_R": v.room_R,
                    "v16_q3_mature": v.q3_mature,
                    "v16_q4_tailwind": v.q4_tailwind,
                    "struct_invalid": float(struct_invalid)}
            if fade:
                meta["harvest_target"] = target
            self._notes.append(
                f"v16过|{symbol} {ev}|{side} {dn.route} tier={v.tier} "
                f"size_mult={v.size_mult} pos={v.pos} room_R={v.room_R} "
                f"target={target} q3={v.q3_mature} q4={v.q4_tailwind}")
            out.append(replace(dn, hyp=hyp, meta=meta))
        return out


__all__ = [
    "HarvestParams", "V16Params", "V16Router", "V16Verdict",
    "evaluate_v16", "opposite_edge_price", "fade_front_edge",
]
