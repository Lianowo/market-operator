"""收尾条款:S3 TRANSITION fade 禁 + S4 LEVEL_REJECTED 归 maker 的路由落点(任务书
冻结语义,2026-10-20 核准;基线 = v19 arm-T:tiered + market 默认 + 8% cap,
early_exit 关)。

规范地位:本模块是 v20 四件中**决策侧两件**(S3/S4)的唯一实现;持仓侧两
件(S1 结构脉冲激活 trail、S2 fade 双格收割)在 position.py(trail_arm_gate
/ harvest_fade_2leg),阈值与族表同源(config/v20.json v20_params 硬对账)。
基线语义锚:arm-T = V19Router(无本层,v20=None ⇒ 逐位 V19Router,parity
钉死);四锚硬断言见 scripts/run_v20.py。

任务书条款 → 实现映射:
- **S3 TRANSITION fade 禁**(v18 R3 已证;独立开关 s3_transition_fade_ban):
  fade 家族(BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED)仅有非 TRANSITION regime 受理 —— 事件层抑制:决策
  视图 regime=TRANSITION 时,fade 事件在进入 V19 配注管线前整体不存在
  (不评估、不计 v19 tier 软条、不产生 open_intents;与 v19 软条款
  transition_fade ×0.5 的关系:S3 开 ⇒ 候选不存在,软条款自然不再命中,
  如实声明);非 fade 事件(BREAKOUT_ACCEPTED/TREND_RESUMED/RANGE_ROTATED/STRUCTURE_BROKEN/FLIP_RETESTED)与非 TRANSITION 三态不受该
  禁;regime 取值 = 决策帧结构视图(bar 路径认知帧 / mic 路径 trigger 帧,
  与 hyp.regime 同源同值)。计数 v20_s3_fade_transition_suppressed。
- **S4 LEVEL_REJECTED 归 maker**(前缘限价语义回归;独立开关 s4_e3_maker):LEVEL_REJECTED 决策改
  写为 kind="limit",计划限价 = zone 前缘(接近侧缘:空=zone.lo /
  多=zone.hi,与 v16 fade/v19 maker 臂同一 fade_front_edge 口径),执行
  走 canonical 限价主路(严格穿过 maker 成交、maker 费、N_fill=12 墙钟小
  时窗口 —— 与 v19 maker 臂 C2 语义逐项同);F1/空间/硬禁以计划限价为
  entry_ref 计量(改单在 evaluate_tiered 之前,因果序同 maker 臂);LEVEL_REJECTED 缺
  attrs zone ⇒ fail-closed 拒(v19_tier_reject_no_zone 沿链通道,同 maker
  臂纪律)。**e3_market 路由下线**:S4 开 ⇒ LEVEL_REJECTED 决策路由标签重贴
  "e3_front_maker",arm 内不再存在 route="level_rejected_market"(S4 关 ⇒ 逐位 v19
  基线,e3_market 照旧)。BREAKOUT_FAILED/EDGE_RECLAIMED 不受 S4 影响(仅 LEVEL_REJECTED;fade 全族前缘回归
  的 maker 臂语义不复活)。
- v20=None(arm-T):S3/S4 钩子整层短路,决策逐位 V19Router(parity 测试
  钉死);计数键只在 S3/S4 开时出现。

因果/确定性:事件层抑制只读决策帧视图 regime(因果);改单只读事件 attrs
zone(知时在场);无新增时钟;纯加性。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from .events import LEVEL_REJECTED
from .structure import Regime
from .tiered import FADE_FAMILY, V19Params, V19Router

# S1/S2 阈值与扶手族表的唯一数据来源在 position.py(持仓侧);此处仅决策侧
from .position import S1_FAMILY, S1_TRAIL_ARM_MFE_R, S2_BANK_FRAC, S2_LEVEL  # noqa: F401

ROUTE_LEVEL_REJECTED_MAKER = "e3_front_maker"    # S4:LEVEL_REJECTED 前缘限价(e3_market 下线替代)


# ---------------------------------------------------------------------------
# 参数(冻结先验;config/v20.json v20_params 同名照抄,不许调)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleParams:
    """v20 四件开关 + 冻结阈值(任务书写死;全 False ⇒ 基线 arm-T)。

    S1/S2 阈值的执行落点在持仓侧 position.py(ArmConfig 接线);此处镜像保
    存以便 config 硬对账单点读数(v20_params 数值面 == position 模块常量)。
    """

    s1_trail_arm: bool = False        # S1:cont+TREND_RESUMED trail 扶手激活闸(position.py)
    s2_fade_2leg: bool = False        # S2:fade 双格收割(position.py 持仓侧)
    s3_transition_fade_ban: bool = False   # S3:fade 仅非 TRANSITION 受理
    s4_e3_maker: bool = False         # S4:LEVEL_REJECTED 归 maker(前缘限价,e3_market 下线)
    s1_mfe_r: float = S1_TRAIL_ARM_MFE_R   # 恰 1R inclusive 上线
    s1_family: tuple = S1_FAMILY           # ("BREAKOUT_ACCEPTED","STRUCTURE_BROKEN","FLIP_RETESTED","TREND_RESUMED")
    s2_level: float = S2_LEVEL             # 0.25(恰触 inclusive)
    s2_bank_frac: float = S2_BANK_FRAC     # 0.40(缘收剩 60%)


@dataclass(frozen=True)
class V20Params(V19Params):
    """v6 路由先验 + v19 阶梯配注层 + v20 生命周期层(默认 None ⇒ 逐位
    V19Router/MarketRouter 行为)。"""

    v20: Optional[LifecycleParams] = None


# ---------------------------------------------------------------------------
# 路由接线(V19Router 子类;v20=None ⇒ 逐位 V19Router)
# ---------------------------------------------------------------------------


class V20Router(V19Router):
    """v20 决策侧两件(S3 事件层抑制 / S4 LEVEL_REJECTED 前缘改单 + 路由重贴)的路由落点。

    - S3:决策视图 regime=TRANSITION ⇒ fade 家族事件整层抑制(候选不存在;
      抑制注记回插队首,计数 v20_s3_fade_transition_suppressed);
    - S4:经父类前缘改单接插缝对 LEVEL_REJECTED 生效(改单在 evaluate_tiered 之前,
      entry_ref/F1/空间一律按计划限价计量);出栈后将 LEVEL_REJECTED 决策的路由标签重
      贴 ROUTE_LEVEL_REJECTED_MAKER(e3_market 下线,仅 S4 开时);
    - v20=None:父类 on_bar 原样返回(钩子逐位旧行为,计数零新增)。
    """

    def on_bar(self, symbol: str, bar, bar_index: int, view, atr: float,
               new_events, *, position_open: bool, entry_resting: bool):
        lp: Optional[LifecycleParams] = getattr(self.params, "v20", None)
        events = new_events
        notes_pre: list = []
        if lp is not None and lp.s3_transition_fade_ban \
                and view is not None and view.regime is Regime.TRANSITION:
            # S3:TRANSITION 内 fade 不受理(事件层抑制;候选不存在语义)
            kept: list = []
            for ev in events:
                if ev.id in FADE_FAMILY:
                    self._bump("v20_s3_fade_transition_suppressed")
                    side = "long" if ev.direction > 0 else "short"
                    notes_pre.append(
                        f"v20 S3|{symbol} {ev.id}|{side} fade TRANSITION 不受理"
                        f"(v18 R3 已证;仅 TREND_UP/TREND_DOWN/RANGE 受理,"
                        f"事件层抑制:v19 软条不展开)")
                    continue
                kept.append(ev)
            events = kept
        decisions = super().on_bar(symbol, bar, bar_index, view, atr, events,
                                   position_open=position_open,
                                   entry_resting=entry_resting)
        if notes_pre:
            # 父类 on_bar 起始重置 notes;S3 抑制注记回插队首(同 v18 R1a)
            self._notes = notes_pre + self._notes
        if lp is None or not lp.s4_e3_maker:
            return decisions
        # S4:LEVEL_REJECTED 前缘改单已在 _front_edge_maker 钩内完成;此处重贴路由标签
        # (e3_market 下线;limit_from=v20_e3_front_edge 是改单完成的唯一印记)
        out: list = []
        for dn in decisions:
            if dn.hyp is not None and dn.hyp.event_id == LEVEL_REJECTED \
                    and dn.kind == "limit" \
                    and dn.meta.get("limit_from") == "v20_e3_front_edge":
                dn = replace(dn, route=ROUTE_LEVEL_REJECTED_MAKER)
            out.append(dn)
        return out

    # -- 父类前缘改单接插缝(tiered.V19Router;S4 只对 LEVEL_REJECTED 生效) -----------------

    def _front_edge_maker(self, tp, ev_id: str) -> bool:
        lp: Optional[LifecycleParams] = getattr(self.params, "v20", None)
        if lp is not None and lp.s4_e3_maker and ev_id == LEVEL_REJECTED:
            return True
        return super()._front_edge_maker(tp, ev_id)

    def _front_edge_label(self, ev_id: str) -> str:
        lp: Optional[LifecycleParams] = getattr(self.params, "v20", None)
        if lp is not None and lp.s4_e3_maker and ev_id == LEVEL_REJECTED:
            return "v20_e3_front_edge"
        return super()._front_edge_label(ev_id)


__all__ = [
    "ROUTE_LEVEL_REJECTED_MAKER", "LifecycleParams", "V20Params", "V20Router",
]
