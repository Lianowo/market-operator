"""v15 位置带沿条款(docs/v15-protocol.md,冻结 2026-10-20)——四件套 ①。

规范地位:本模块是 v15 合成件中唯一的入场前条款实现:摆动带 B=[L*,H*]
位置 pos,"空 pos≥0.35、多 pos≤0.65,错半边不交易",与 v14 C1-Q1 语义逐
位相同(pos 计算复用 quality.swing_band_pos,不重造;entry_ref 口径同
quality.py Q1:市价路由 = 决策 bar 收盘,限价路由 = 计划限价);但只做拒单
不做配档 —— v14 的 Q2 空间闸(误杀版)/Q3 履历/Q4 顺风/C5 档位配注随 v14
arm 矩阵裁决(log#28)一并弃置,本条款输出仅 过/拒 二值,不触碰
size_mult/杠杆/容量;拒单以 reason 记录于 router notes/counters
(band_side_reject_pos),缺一侧 pivot 或带退化 ⇒ 门不可判 ⇒ fail-closed
拒(同 quality.py Q1/EDGE_RECLAIMED 纪律)。

确定性:纯函数 `band_side_check` + 无状态条款;`V15Router` 在
band_side=None 时逐位同 `MarketRouter`(条款短路不回读,arm0 回归钉死)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .quality import swing_band_pos
from .routing import RouterParams, MarketRouter


# ---------------------------------------------------------------------------
# 参数(冻结先验;config/v15.json 同名照抄,不许调)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BandSideParams:
    """带沿条款冻结先验(协议 ①写死;唯一数据来源;= v14 Q1 同值)。"""

    pos_short_min: float = 0.35      # 空 pos 下限(inclusive,带下半不空)
    pos_long_max: float = 0.65       # 多 pos 上限(inclusive,带上半不多)


def band_side_check(p: BandSideParams, *, direction: int, entry_ref: float,
                    pivots: Sequence) -> tuple:
    """带沿判定(纯函数):返回 (ok, pos);pos=None ⇒ 门不可判 ⇒ fail-closed。"""
    pos = swing_band_pos(pivots, entry_ref)
    if pos is None:
        return False, None
    if int(direction) < 0:
        return bool(pos >= p.pos_short_min), pos
    return bool(pos <= p.pos_long_max), pos


# ---------------------------------------------------------------------------
# 路由接线(MarketRouter 子类;band_side=None ⇒ 逐位同父类)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class V15Params(RouterParams):
    """v6 路由先验 + v15 带沿条款(默认 None ⇒ 逐位 MarketRouter 行为)。"""

    band_side: Optional[BandSideParams] = None


class V15Router(MarketRouter):
    """带沿条款的路由落点:父类产出决策 ⇒ 带沿评估 ⇒ 拒单/注记。

    - 拒单(错半边或门不可判):决策丢弃,counters 记 band_side_reject_pos,
      notes 记 pos 与事件(协议:"否拒并以 reason 记录",同 v14 C1 纪律);
    - 过单:meta 附 band_pos(溯源列,不上数值路径:不配档/不动量);
    - band_side=None(arm0):短路返回父类决策,逐位 MarketRouter 行为。
    """

    def on_bar(self, symbol: str, bar, bar_index: int, view, atr: float,
               new_events, *, position_open: bool, entry_resting: bool):
        decisions = super().on_bar(symbol, bar, bar_index, view, atr,
                                   new_events, position_open=position_open,
                                   entry_resting=entry_resting)
        bp: Optional[BandSideParams] = getattr(self.params, "band_side", None)
        if bp is None:
            return decisions
        out = []
        for dn in decisions:
            if dn.kind == "cancel_entry":
                out.append(dn)
                continue
            entry_ref = (float(dn.limit_price) if dn.kind == "limit"
                         else float(dn.intent.ref_price))
            ok, pos = band_side_check(
                bp, direction=int(dn.hyp.direction), entry_ref=entry_ref,
                pivots=(view.pivots if view is not None else ()))
            self._bump("band_side_evaluated")
            side = "long" if dn.hyp.direction > 0 else "short"
            if not ok:
                self._bump("band_side_reject_pos")
                self._notes.append(
                    f"带沿拒|{symbol} {dn.hyp.event_id}|{side} {dn.route} "
                    f"pos={pos} 阈值(空≥{bp.pos_short_min}/多≤"
                    f"{bp.pos_long_max},错半边不交易;门不可判 fail-closed)")
                continue
            dn.meta.update({"band_pos": pos})
            self._notes.append(
                f"带沿过|{symbol} {dn.hyp.event_id}|{side} {dn.route} "
                f"pos={pos}")
            out.append(dn)
        return out


__all__ = [
    "BandSideParams", "V15Params", "V15Router", "band_side_check",
]
