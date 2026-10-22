"""v19 阶梯配注(docs/v19-protocol.md,冻结 2026-10-20;research-log #35
预注册,先于运行)。

规范地位:本模块是 v19 协议 §2"三闸软着陆为阶梯配注"在路由侧的唯一落点
(V14Router/V16Router/V18Router 同型构造;tiered=None ⇒ 逐位 MarketRouter,
parity 钉死)。v16 三闸(位置/空间/F1)与 v18 决策侧三件(R1 大炮、R2 逆
三拍、R3 TRANSITION fade)全部由"缺一不存在交易"改为逐条款 size_mult
计档;硬禁只剩两种(F1 失效不足 0.8·ATR、结构性 target 不存在)。持仓侧
夭折处置在 position.py(early_exit),执行差分降级为臂(maker/stop_entry,
协议默认执行价=market,由 config 臂接线承担)。

协议条款 → 实现映射:
- **基础 A=1.0**:每张假设先给 1.0,逐条款相乘计档(记账 reason 带得分条
  款,meta 附逐条款命中与乘积);
- **带沿失位 ×0.5**(原闸①软着陆):摆动带 pos(quality.swing_band_pos 同
  口径,边界 inclusive)空 <0.35 / 多 >0.65,或 pos 不可判(缺侧/退化)⇒
  乘 0.5;
- **空间两档**(原闸②软着陆;对面结构缘 = harvest.opposite_edge_price 同
  一搜索口径):room = d·(target−entry_ref)/|entry_ref−invalid|,
  ≥ k_room_R(1.5)⇒ 不乘(1.0);∈ [1.0,1.5) ⇒ ×0.75;< 1.0 ⇒ ×0.5;
- **REGIME=TRANSITION 内 fade ×0.5**(v18 R3 软着陆):仅 fade 家族
  (BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED)且 regime=TRANSITION;三态(TREND_UP/TREND_DOWN/RANGE)与
  非 fade 不受该条;
- **大蛋糕 ×0.75**(v18 R1 软着陆):rngq(窗 48,恰 0.8 受理,>0.8 计档)
  事件到达于"不 mic-only 处"(bar 认知收盘路径)⇒ ×0.75;mic-only 处
  (trigger 收盘决策,续涨 mic 通道)不乘;
- **逆三拍 ×0.5**(v18 R2 软着陆):d·pmd ≤ −0.003(反向幅恰 0.3% 起,恰
  界计档;窗 3)⇒ ×0.5;ctx 缺失/历史不足 ⇒ 不乘(非 fail-closed,同
  v18 声明);
- **硬禁(缺一不存在交易)**:
  ① F1:dist = d·(entry_ref−invalid) < k_is·ATR(0.8;ATR ≤ 0 或 dist
  ≤ 0 = 非保护性失效 ⇒ 拒,fail-closed;ATR 取决策帧);
  ② 结构性 target 不存在:对面结构缘缺失 ⇒ 拒(原闸②"缘缺失 ⇒ 空间无界
  ⇒ 过"反转;fade 无收割目标与续涨无结构空间同禁,如实声明);
  另沿链 fail-closed:attrs invalid 缺失 ⇒ 拒(F1 不可判,计
  v19_tier_reject_no_invalid);fade_front_edge 臂 fade 缺 attrs zone ⇒
  拒(计 v19_tier_reject_no_zone,maker 臂前缘不可判,同 v16 纪律);
- **结构锚(照链)**:过单 hyp 重建 entry=entry_ref、invalid=attrs 结构失
  效(turtle 2·ATR 继续不参与)、target_1=对面缘(fade;续涨/TREND_RESUMED/RANGE_ROTATED
  None)、r_mult 重算;档注 q3(zone_touches > k_*=2)只注不参档;v16 的
  顺风 A/B 双档在 tier_sizing 下退位(基础 A=1.0 已含,q4 只作注记);
- **执行差分臂化**(协议默认执行价=market):fade 前缘改单只在
  TieredParams.fade_front_edge=True(maker 臂)发生;默认臂(kind 原样
  market)不碰,提交侧 canonical。

因果/确定性:evaluate_tiered 纯函数;rngq/pmd 由引擎每次决策注入
(set_ctx18,读后即清;bar 路径取认知帧,mic 路径 rngq 取 trigger 帧、
pmd 取最近已收三根认知 bar,与 v18 同一注入链);"mic-only 处"判定 =
注入 ctx 的 mic 标志(trigger 收盘决策为 True);硬禁逐项计数(no_invalid
→ no_zone(maker 臂)→ F1 → target_missing,逐项记账,同 v16 口径);
理由注记与 ledger reason 后缀("|v19tier:")同步。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence

from .harvest import fade_front_edge, opposite_edge_price
from .quality import swing_band_pos
from .repairs import PMD_MIN, PMD_WINDOW, RNGQ_MAX, RNGQ_WINDOW
from .routing import RouteDecision, RouterParams, MarketRouter
from .structure import Regime

# v19 §2 家族表(协议写死;与 campaign/repairs/v16_params 家族表硬对账)
FADE_FAMILY = ("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED")
STOP_FAMILY = ("BREAKOUT_ACCEPTED", "STRUCTURE_BROKEN", "FLIP_RETESTED")

ROOM_LO = 1.0                        # 空间中档下缘(恰 1.0 入中档 ×0.75)


# ---------------------------------------------------------------------------
# 参数(冻结先验;config/v19.json 同名照抄,不许调)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TieredParams:
    """v19 阶梯配注冻结先验(协议 §2 写死;唯一数据来源)。"""

    pos_short_min: float = 0.35      # 原闸①:空 pos 下限(inclusive)
    pos_long_max: float = 0.65       # 原闸①:多 pos 上限(inclusive)
    k_room_R: float = 1.5            # 原闸②:满档空间下缘(恰 1.5 ⇒ 不乘)
    room_lo: float = 1.0             # 空间中档下缘(恰 1.0 ⇒ ×0.75)
    k_is: float = 0.8                # F1 硬禁:失效距离下限(ATR 倍数)
    k_star: int = 2                  # 档注:k 成熟断点(只注不参档)
    mult_pos: float = 0.5            # 带沿失位
    mult_room_mid: float = 0.75      # room ∈ [1.0,1.5)
    mult_room_lo: float = 0.5        # room < 1.0
    mult_transition_fade: float = 0.5    # TRANSITION 内 fade
    mult_rngq_big: float = 0.75      # 大蛋糕(不 mic-only 处)
    mult_pmd_adverse: float = 0.5    # 逆三拍
    rngq_window: int = RNGQ_WINDOW   # 48(照 v18 链)
    rngq_max: float = RNGQ_MAX       # 0.8(恰界受理,>0.8 计档)
    pmd_window: int = PMD_WINDOW     # 3(照 v18 链)
    pmd_min: float = PMD_MIN         # 0.003(恰界计档)
    fade_family: tuple = FADE_FAMILY
    fade_front_edge: bool = False    # maker 臂:fade 前缘改单(默认关=market)


@dataclass(frozen=True)
class V19Params(RouterParams):
    """v6 路由先验 + v19 阶梯配注层(默认 None ⇒ 逐位 MarketRouter 行为)。"""

    tiered: Optional[TieredParams] = None


# ---------------------------------------------------------------------------
# 判定结果与阶梯配注(纯函数)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TieredVerdict:
    """阶梯配注评估输出(全部字段因果可复算;tradable=False ⇒ 硬禁拒单)。"""

    tradable: bool
    size_mult: float                 # 条款乘积(硬禁 = 0.0)
    tier: str                        # "A"(满 1.0)/ "B"(减档)/ "X"(硬禁)
    clauses: tuple                   # 命中扣分条款(按 pos→room→r3→r1→r2 序)
    rejects: tuple                   # 硬禁 reason 码(F1 → target_missing 序)
    pos: Optional[float]
    room_R: Optional[float]          # target 缺失 = None(此时硬禁)
    target: Optional[float]
    f1: bool
    q3_mature: bool                  # 档注(不参与计档)
    q4_tailwind: bool                # 注记(v16 顺风双档退位,只注)


def evaluate_tiered(p: TieredParams, *, direction: int, entry_ref: float,
                    invalid: float, atr: float, zones: Sequence,
                    pivots: Sequence, regime, zone_touches: Optional[int],
                    rngq: Optional[float], pmd: Optional[float],
                    mic_path: bool, fade: bool) -> TieredVerdict:
    """阶梯配注(纯函数):硬禁 F1/target_missing;逐条款相乘计档。"""
    d = int(direction)
    ref = float(entry_ref)
    inv = float(invalid)
    atr = float(atr)
    # 硬禁① F1:dist(entry_ref, invalid) ≥ k_is·ATR(dist ≤ 0 非保护 ⇒ 拒)
    dist = d * (ref - inv)
    f1 = bool(atr > 0.0 and dist >= p.k_is * atr)
    # 对面结构缘(空间丈量 + 硬禁② + fade 收割目标)
    target = opposite_edge_price(zones, d, ref)
    risk_px = abs(ref - inv)
    if risk_px <= 0.0:
        room: Optional[float] = float("-inf")
    elif target is None:
        room = None
    else:
        room = d * (target - ref) / risk_px
    rejects = tuple(r for r, ok in (("f1", f1),
                                    ("target_missing", target is not None))
                    if not ok)
    # 注记
    q3 = zone_touches is not None and int(zone_touches) > p.k_star
    q4 = ((d > 0 and regime is Regime.TREND_UP)
          or (d < 0 and regime is Regime.TREND_DOWN))
    if rejects:
        return TieredVerdict(tradable=False, size_mult=0.0, tier="X",
                             clauses=(), rejects=rejects, pos=None,
                             room_R=room, target=target, f1=f1,
                             q3_mature=q3, q4_tailwind=q4)
    # 逐条款计档(协议 §2 序列)
    mult = 1.0
    clauses: list = []
    # 带沿失位 ×0.5(pos 不可判 ⇒ 同失位计,谨慎口径)
    pos = swing_band_pos(pivots, ref)
    pos_ok = (pos is not None and (pos >= p.pos_short_min if d < 0
                                   else pos <= p.pos_long_max))
    if not pos_ok:
        mult *= p.mult_pos
        clauses.append("pos_edge")
    # 空间两档(∈[1.0,1.5) ×0.75;<1.0 ×0.5;恰 1.5 不乘)
    if room < p.k_room_R:
        if room >= p.room_lo:
            mult *= p.mult_room_mid
            clauses.append("room_mid")
        else:
            mult *= p.mult_room_lo
            clauses.append("room_lo")
    # REGIME=TRANSITION 内 fade ×0.5
    if fade and regime is Regime.TRANSITION:
        mult *= p.mult_transition_fade
        clauses.append("transition_fade")
    # 大蛋糕(不 mic-only 处)×0.75(恰 0.8 受理)
    if not mic_path and rngq is not None and rngq > p.rngq_max:
        mult *= p.mult_rngq_big
        clauses.append("rngq_big")
    # 逆三拍 ×0.5(d·pmd ≤ −0.003,恰界计档)
    if pmd is not None and d * pmd <= -p.pmd_min:
        mult *= p.mult_pmd_adverse
        clauses.append("pmd_adverse")
    tier = "A" if not clauses else "B"
    return TieredVerdict(tradable=True, size_mult=float(mult), tier=tier,
                         clauses=tuple(clauses), rejects=(),
                         pos=pos, room_R=room, target=target, f1=True,
                         q3_mature=q3, q4_tailwind=q4)


# ---------------------------------------------------------------------------
# 路由接线(MarketRouter 子类;tiered=None ⇒ 逐位同父类)
# ---------------------------------------------------------------------------


class V19Router(MarketRouter):
    """v19 阶梯配注的路由落点:父类决策 ⇒ (maker 臂 fade 前缘改单)⇒ 硬禁
    核 ⇒ 计档注记 ⇒ 结构锚 hyp 重建。

    - 硬禁:decision 丢弃,counters 记 v19_tier_reject_{f1,target_missing,
      no_invalid,no_zone}(逐项记账,notes 记完整 reason);
    - 过单:meta 附 v19_tier / size_mult / v19_clauses / v19_pos /
      v19_room_R / v19_rngq / v19_pmd / q3/q4 注记 / struct_invalid /
      (fade)harvest_target;ledger reason 追加 "|v19tier:" 后缀(记账带条
      款);hyp 重建 entry=entry_ref、invalid=结构锚、target_1=对面缘
      (fade;其余 None)、r_mult 重算;
    - 引擎在每决策前经 set_ctx18 注入 {"rngq","pmd","mic"}(读后即清;
      tiered=None ⇒ 语境整层忽略,行为逐位 MarketRouter)。
    """

    def __init__(self, params: Optional[V19Params] = None) -> None:
        super().__init__(params)
        self._ctx19: Optional[dict] = None

    def ctx18_windows(self) -> tuple:
        """引擎注入语境所需的窗口 (rngq_window, pmd_window)(冻结口径)。"""
        tp: Optional[TieredParams] = getattr(self.params, "tiered", None)
        if tp is None:
            return RNGQ_WINDOW, PMD_WINDOW
        return int(tp.rngq_window), int(tp.pmd_window)

    def set_ctx18(self, ctx: Optional[dict]) -> None:
        self._ctx19 = ctx

    # -- 前缘改单接插缝(v20 S4 重载;tiered 路径默认逐位旧行为) -----------------

    def _front_edge_maker(self, tp: TieredParams, ev_id: str) -> bool:
        """fade 前缘改单谓词(v19 maker 臂 = tp.fade_front_edge;v20 S4 对 LEVEL_REJECTED
        重载,见 lifecycle.V20Router)。默认逐位旧行为。"""
        return bool(tp.fade_front_edge)

    def _front_edge_label(self, ev_id: str) -> str:
        """前缘改单溯源标签(v20 S4 LEVEL_REJECTED 重载为 v20_e3_front_edge)。"""
        return "v19_fade_front_edge"

    def on_bar(self, symbol: str, bar, bar_index: int, view, atr: float,
               new_events, *, position_open: bool, entry_resting: bool):
        decisions = super().on_bar(symbol, bar, bar_index, view, atr,
                                   new_events, position_open=position_open,
                                   entry_resting=entry_resting)
        tp: Optional[TieredParams] = getattr(self.params, "tiered", None)
        if tp is None:
            self._ctx19 = None
            return decisions
        ctx = self._ctx19
        self._ctx19 = None
        rngq = ctx.get("rngq") if ctx else None
        pmd = ctx.get("pmd") if ctx else None
        mic_path = bool(ctx.get("mic")) if ctx else False
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
            fade = ev in tp.fade_family
            self._bump("v19_tier_evaluated")
            struct_invalid = dn.meta.get("ev_invalid")
            if struct_invalid is None:
                self._bump("v19_tier_reject_no_invalid")
                self._notes.append(
                    f"v19禁|{symbol} {ev}|{side} {dn.route} 结构失效缺失 "
                    f"(attrs invalid 不在场;fail-closed,沿链)")
                continue
            # maker 臂:fade ⇒ 前缘改单(计划限价 = zone 接近侧缘,同 v16)
            if fade and self._front_edge_maker(tp, ev):
                if "zone_lo" not in dn.meta or "zone_hi" not in dn.meta:
                    self._bump("v19_tier_reject_no_zone")
                    self._notes.append(
                        f"v19禁|{symbol} {ev}|{side} {dn.route} fade 缺 attrs "
                        f"zone(前缘不可判;fail-closed,maker 臂)")
                    continue
                front = fade_front_edge(float(dn.meta["zone_lo"]),
                                        float(dn.meta["zone_hi"]), d)
                dn = replace(dn, kind="limit", limit_price=front,
                             meta={**dn.meta,
                                   "limit_from": self._front_edge_label(ev)})
            entry_ref = (float(dn.limit_price) if dn.kind == "limit"
                         else float(dn.intent.ref_price))
            v = evaluate_tiered(
                tp, direction=d, entry_ref=entry_ref,
                invalid=float(struct_invalid), atr=float(atr), zones=zones,
                pivots=pivots, regime=dn.hyp.regime,
                zone_touches=dn.meta.get("zone_touches"),
                rngq=rngq, pmd=pmd, mic_path=mic_path, fade=fade)
            if not v.tradable:
                for r in v.rejects:
                    self._bump(f"v19_tier_reject_{r}")
                self._notes.append(
                    f"v19禁|{symbol} {ev}|{side} {dn.route} "
                    f"拒:{','.join(v.rejects)} pos={v.pos} room_R={v.room_R} "
                    f"entry_ref={entry_ref:.8g} invalid={struct_invalid:.8g} "
                    f"(硬禁:F1 / 结构性 target 不存在)")
                continue
            for cl in v.clauses:
                self._bump(f"v19_clause_{cl}")
            self._bump("v19_tier_a" if v.tier == "A" else "v19_tier_b")
            target = v.target if fade else None
            risk = d * (entry_ref - float(struct_invalid))
            r_mult = (d * (target - entry_ref) / risk
                      if target is not None and risk > 0 else None)
            hyp = replace(dn.hyp, entry=float(entry_ref),
                          invalid=float(struct_invalid), target_1=target,
                          r_mult=r_mult)
            meta = {**dn.meta,
                    "v19_tier": v.tier, "size_mult": v.size_mult,
                    "v19_clauses": list(v.clauses),
                    "v19_pos": v.pos, "v19_room_R": v.room_R,
                    "v19_rngq": rngq, "v19_pmd": pmd,
                    "v19_q3_mature": v.q3_mature,
                    "v19_q4_tailwind": v.q4_tailwind,
                    "struct_invalid": float(struct_invalid)}
            if fade:
                meta["harvest_target"] = target
            ctag = ",".join(v.clauses) if v.clauses else "clean"
            reason = f"{dn.intent.reason} |v19tier:{v.tier}×{v.size_mult:g}[{ctag}]"
            self._notes.append(
                f"v19过|{symbol} {ev}|{side} {dn.route} tier={v.tier} "
                f"size_mult={v.size_mult} clauses={ctag} pos={v.pos} "
                f"room_R={v.room_R} target={target} rngq={rngq} "
                f"pmd={pmd} mic_path={mic_path}")
            out.append(replace(dn, hyp=hyp, meta=meta,
                               intent=replace(dn.intent, reason=reason)))
        return out


__all__ = [
    "FADE_FAMILY", "STOP_FAMILY", "ROOM_LO", "TieredParams", "V19Params",
    "TieredVerdict", "V19Router", "evaluate_tiered",
]
