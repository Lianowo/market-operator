"""修复包 R1-R3 的路由落点(docs/v18-protocol.md;
research-log #34 预注册,先于运行)。

规范地位:本模块是 v18 修复包中**决策侧**三件(R1 大炮走法、R2 逆三拍、
R3 TRANSITION fade 候选)的唯一实现;R4(fade 三段收割)在持仓侧
position.py(harvest_ladder3)。基线 = v17 arm-C 语义(仅续涨 mic,fade
归 bar;V18Router(v18=None) ⇒ 逐位 V16Router,基线锚对账钉死)。

协议条款 → 实现映射:
- **R1 大炮走法(事件 bar 波幅分位 rngq,窗 48,恰 0.8 受理)**:
  ① 续涨家族(BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED)仅允许级联 mic 口径入场:bar 化身事件(认知收盘
  到达、attrs 无 mic 标注)在进入 V6 路由前整体抑制 —— 大炮尾巴上的
  决策层入场一律不存在;其 BREAKOUT_ACCEPTED 优先级撤单/弱 episode 登记等副作用一并不
  展开(错峰语义:同现象的 mic 化身先到先决,bar 化身完全惰性);
  事件三根属性(ref/invalid/zone)不变、检测零改动。计数
  v18_r1_cont_bar_suppressed。
  ② fade 家族(BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED):rngq > 0.8(strictly;恰 0.8 受理)的事件不
  入场(小级抢跑三度证伪后的存量条款),计数 v18_r1_fade_rngq_evaluated /
  _reject。
- **R2 逆三拍(入场合法性核,与市况/收割解耦)**:决策时刻前三根认知帧
  bar 的净方向幅 pmd = Σ(c_j − o_j)/o_j,方向落成 d·pmd ≤ −0.003
  (反向幅 ≥ 0.3%,恰界拒)⇒ 不入场。计数 v18_r2_pmd_evaluated/_reject。
- **R3 TRANSITION fade(候选条款,报告单独归因)**:fade 家族仅
  regime ∈ {TREND_UP, TREND_DOWN, RANGE} 受理;TRANSITION 不入场,计数
  v18_r3_fade_evaluated/_reject。

因果/确定性:
- rngq/pmd 由引擎在每次决策时注入(set_ctx18;本次决策读后即清,陈旧
  语境不外溢):bar 路径(认知收盘决策)取认知帧自身 —— rngq = 事件
  bar i 波幅(h−l)在前 rngq_window(48)根波幅中的分位(≤ 占比;前期
  不足窗用可用历史,i=0 ⇒ None);pmd = 前 pmd_window(3)根的
  Σ(c-o)/o(不足整窗 ⇒ None)。
- mic 路径(trigger 收盘决策):pmd 取最近**已收**三根认知 bar(与 bar
  路径同一窗口定义);rngq 取 trigger 帧自身 48 窗(事件 bar = 发射
  trigger bar)—— v18 全臂 fade 归 bar,该支路在测量网格中惰性,仅在
  单元测试中行使,如实声明。
- 条款不可判(ctx 缺失/历史不足)⇒ 不拒(定向禁令,非 fail-closed 核;
  与三闸的 fail-closed 纪律分开,如实声明)。
- 拒绝次序 R1b → R2 → R3(逐决策单拒绝归因,i.e. 一决策至多落一个
  拒绝桶);过单 meta 附 rngq/pmd 溯源(不进数值路径)。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence

from .harvest import HarvestParams, V16Params, V16Router
from .structure import Regime

# v18 家族表(协议写死;与 campaign.STOP/FADE_FAMILY、v16_params 家族表
# 硬对账 —— run_v18.load_config 与 config 测试双锚,漂移即 raise)
CONT_FAMILY = ("BREAKOUT_ACCEPTED", "STRUCTURE_BROKEN", "FLIP_RETESTED")
FADE_FAMILY = ("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED")

RNGQ_WINDOW = 48                   # R1 rngq 窗(协议 §R1 写死)
RNGQ_MAX = 0.8                     # fade 大炮阈值(strictly 超即拒;恰界受理)
PMD_WINDOW = 3                     # R2 前三根(协议 §R2 写死)
PMD_MIN = 0.003                    # 反向幅恰界 0.3%(d·pmd ≤ −PMD_MIN 拒)


# ---------------------------------------------------------------------------
# 决策语境(纯函数;bars = (ts, o, h, l, c, v) 升序)
# ---------------------------------------------------------------------------


def range_quantile(bars: Sequence, i: int, window: int = RNGQ_WINDOW
                   ) -> Optional[float]:
    """rngq:bar i 的波幅(h−l)在其前 window 根波幅中的分位(≤ 占比)。

    可用历史不足 window 根时用全部在场前史(i 根);i = 0 ⇒ None(无前史
    不可判)。确定性:同输入同输出;只看 i 之前的 bar(因果)。
    """
    i = int(i)
    if i <= 0:
        return None
    lo = max(0, i - int(window))
    prev = bars[lo:i]
    if not prev:
        return None
    r = float(bars[i][2]) - float(bars[i][3])
    k = sum(1 for b in prev if float(b[2]) - float(b[3]) <= r)
    return k / len(prev)


def prior_moves_delta(bars: Sequence, i: int, window: int = PMD_WINDOW
                      ) -> Optional[float]:
    """pmd:bar i 之前 window 根的净方向幅 Σ(c_j − o_j)/o_j。

    不足整窗(i < window)或 open 非正 ⇒ None(不可判,不拒)。三根恰界
    语义由路由侧 PMD_MIN 承担(本函数只度量)。
    """
    i = int(i)
    if i < int(window):
        return None
    tot = 0.0
    for j in range(i - int(window), i):
        o, c = float(bars[j][1]), float(bars[j][4])
        if o <= 0.0:
            return None
        tot += (c - o) / o
    return tot


# ---------------------------------------------------------------------------
# 参数(冻结先验;config/v18.json 同名照抄,不许调)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairParams:
    """v18 修复件开关 + 冻结阈值(协议逐字写死;全 False ⇒ 基线 arm-C)。"""

    r1: bool = False               # R1:续涨 mic-only + fade rngq>0.8 拒
    r2: bool = False               # R2:逆三拍 d·pmd ≤ −0.003 拒
    r3: bool = False               # R3:fade TRANSITION 不受理(候选条款)
    rngq_window: int = RNGQ_WINDOW
    rngq_max: float = RNGQ_MAX
    pmd_window: int = PMD_WINDOW
    pmd_min: float = PMD_MIN


@dataclass(frozen=True)
class V18Params(V16Params):
    """v6 路由先验 + v16 层 + v18 修复层(默认 None ⇒ 逐位 V16Router)。"""

    v18: Optional[RepairParams] = None


# ---------------------------------------------------------------------------
# 路由接线(V16Router 子类;v18=None ⇒ 逐位 V16Router 行为)
# ---------------------------------------------------------------------------


class V18Router(V16Router):
    """v18 决策侧三件的路由落点:R1a 事件层抑制(续涨 bar 化身)、
    R1b/R2/R3 决策层合法性(假设合法性核;拒绝记 counters + notes)。

    引擎在每次决策前经 set_ctx18 注入本决策语境({"rngq","pmd"});路由
    读后即清(v18=None ⇒ 语境整层忽略,行为逐位 V16Router)。
    """

    def __init__(self, params: Optional[V18Params] = None) -> None:
        super().__init__(params)
        self._ctx18: Optional[dict] = None

    def ctx18_windows(self) -> tuple:
        """引擎注入语境所需的窗口 (rngq_window, pmd_window)(冻结口径)。"""
        rp: Optional[RepairParams] = getattr(self.params, "v18", None)
        if rp is None:
            return RNGQ_WINDOW, PMD_WINDOW
        return int(rp.rngq_window), int(rp.pmd_window)

    def set_ctx18(self, ctx: Optional[dict]) -> None:
        self._ctx18 = ctx

    def on_bar(self, symbol: str, bar, bar_index: int, view, atr: float,
               new_events, *, position_open: bool, entry_resting: bool):
        rp: Optional[RepairParams] = getattr(self.params, "v18", None)
        ctx = self._ctx18
        self._ctx18 = None
        events = new_events
        notes_pre: list = []
        if rp is not None and rp.r1:
            # R1a:续涨族 bar 化身整层抑制(mic-only;错峰:同现象 mic 先到
            # 先决,bar 尾巴入场/撤单/episode 并发的副作用一律不展开)
            kept: list = []
            for ev in events:
                if ev.id in CONT_FAMILY and not ev.attrs.get("mic"):
                    self._bump("v18_r1_cont_bar_suppressed")
                    side = "long" if ev.direction > 0 else "short"
                    notes_pre.append(
                        f"v18 R1|{symbol} {ev.id}|{side} bar 化身抑制(续涨 "
                        f"mic-only;大炮尾巴决策入场不存在,错峰惰性)")
                    continue
                kept.append(ev)
            events = kept
        decisions = super().on_bar(symbol, bar, bar_index, view, atr, events,
                                   position_open=position_open,
                                   entry_resting=entry_resting)
        if notes_pre:
            # 父类 on_bar 起始重置 notes;R1a 抑制注记回插队首(解释性账本)
            self._notes = notes_pre + self._notes
        if rp is None:
            return decisions
        rngq = ctx.get("rngq") if ctx else None
        pmd = ctx.get("pmd") if ctx else None
        out: list = []
        for dn in decisions:
            if dn.kind == "cancel_entry" or dn.hyp is None:
                out.append(dn)
                continue
            ev_id = dn.hyp.event_id
            d = int(dn.hyp.direction)
            side = "long" if d > 0 else "short"
            if rp.r1 and ev_id in FADE_FAMILY:
                self._bump("v18_r1_fade_rngq_evaluated")
                if rngq is not None and rngq > rp.rngq_max:
                    self._bump("v18_r1_fade_rngq_reject")
                    self._notes.append(
                        f"v18 R1|{symbol} {ev_id}|{side} fade 大炮不入场 "
                        f"rngq={rngq:.6f} > {rp.rngq_max}(恰 {rp.rngq_max} "
                        f"受理;窗 {rp.rngq_window})")
                    continue
            if rp.r2:
                self._bump("v18_r2_pmd_evaluated")
                if pmd is not None and d * pmd <= -rp.pmd_min:
                    self._bump("v18_r2_pmd_reject")
                    self._notes.append(
                        f"v18 R2|{symbol} {ev_id}|{side} 逆三拍不入场 "
                        f"d·pmd={d * pmd:.6f} ≤ −{rp.pmd_min}(反向幅恰 "
                        f"{rp.pmd_min:.1%} 即拒;假设合法性核,与市况/收割解耦)")
                    continue
            if rp.r3 and ev_id in FADE_FAMILY:
                self._bump("v18_r3_fade_evaluated")
                if dn.hyp.regime is Regime.TRANSITION:
                    self._bump("v18_r3_fade_transition_reject")
                    self._notes.append(
                        f"v18 R3|{symbol} {ev_id}|{side} fade TRANSITION "
                        f"不受理(仅 TREND_UP/TREND_DOWN/RANGE;候选条款)")
                    continue
            meta = dict(dn.meta)
            if rngq is not None:
                meta["rngq"] = rngq
            if pmd is not None:
                meta["pmd"] = pmd
            out.append(replace(dn, meta=meta))
        return out


__all__ = [
    "CONT_FAMILY", "FADE_FAMILY", "RNGQ_WINDOW", "RNGQ_MAX",
    "PMD_WINDOW", "PMD_MIN", "RepairParams", "V18Params", "V18Router",
    "range_quantile", "prior_moves_delta",
]
