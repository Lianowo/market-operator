"""统一策略装配与执行引擎门面(Replay 回放与 Live 实盘共享唯一代码链路)。

设计纪律:
- 回放(``scenario.py`` / ``campaign.py``)与实盘(``scripts/live_runner.py``)必须通过本模块的
  同一组纯函数加载 ``config/strategy.json``、实例化 ``Params`` / ``RouterParams`` /
  ``ExecutionConfig`` / ``PositionManager``,并执行完全相同的认知/触发事件分流规则。
- 彻底消除"回放读一套默认值、实盘手写另一套分支"的链路分叉风险。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import sessions
from .capacity import cap_effective_size_multiplier
from .events import MarketEvent
from .position import PositionManager
from .routing import MarketRouter, RouterParams, WHITELIST_DEFAULT, blocked_by_market_breadth
from .structure import Params, Regime


@dataclass(frozen=True)
class ExecutionConfig:
    """持仓管理与细粒度推进配置(回放与实盘唯一共享定义)。"""

    allow_add: bool = False
    breakeven_on_reduce: bool = True
    partial_exit_enabled: bool = True
    accept_reenter_level_exit: bool = True
    reject_opp_lock_frac: float = 0.0
    exit_exec: bool = True
    exit_exec_trail: bool = True
    pivot_exec: bool = False
    ltf_opposing_reject_exit: bool = False


@dataclass(frozen=True)
class StrategyBundle:
    """从 strategy.json 解析出的唯一权威策略参数集。"""

    raw: Mapping[str, Any]
    params: Params
    router: RouterParams
    router_by_tf: Mapping[str, RouterParams]
    arm: ExecutionConfig


def parse_execution_config(raw: Mapping[str, Any]) -> ExecutionConfig:
    arm_raw = dict(raw.get("arm_config") or {})
    # 兼容字段名归一(旧配置若写 accept_reenter_box_exit 映射至 accept_reenter_level_exit,建议新配置直接用新键)
    if "accept_reenter_level_exit" not in arm_raw and "accept_reenter_box_exit" in arm_raw:
        arm_raw["accept_reenter_level_exit"] = bool(arm_raw["accept_reenter_box_exit"])
    known = {f.name for f in fields(ExecutionConfig)}
    return ExecutionConfig(**{k: v for k, v in arm_raw.items() if k in known})


def parse_router_params(raw: Mapping[str, Any], params: Params) -> tuple[RouterParams, dict[str, RouterParams]]:
    wl_raw = raw.get("whitelist")
    whitelist = (
        frozenset((str(ev), int(d)) for ev, d in wl_raw)
        if wl_raw is not None
        else WHITELIST_DEFAULT
    )
    rp_cfg = dict(raw.get("router_params") or {})
    known = {f.name for f in fields(RouterParams)} - {"whitelist"}
    filtered = {k: v for k, v in rp_cfg.items() if k in known}
    if "k_bm" not in filtered:
        filtered["k_bm"] = params.k_bm
    base_rp = RouterParams(whitelist=whitelist, **filtered)

    by_tf: dict[str, RouterParams] = {}
    for tf, ov in (raw.get("router_params_by_tf") or {}).items():
        ov_dict = dict(ov)
        ov_clean = {k: v for k, v in ov_dict.items() if k in known}
        if "whitelist" in ov_dict:
            ov_clean["whitelist"] = frozenset(
                (str(ev), int(d)) for ev, d in ov_dict["whitelist"]
            )
        by_tf[str(tf)] = replace(base_rp, **ov_clean)
    return base_rp, by_tf


def load_strategy_bundle(config_path: str | Path) -> StrategyBundle:
    """回放与实盘共用的唯一策略配置加载器。"""
    p = Path(config_path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    params = Params.from_json(p)
    base_rp, by_tf = parse_router_params(raw, params)
    arm = parse_execution_config(raw)
    return StrategyBundle(
        raw=MappingProxyType(raw),
        params=params,
        router=base_rp,
        router_by_tf=MappingProxyType(by_tf),
        arm=arm,
    )


def build_position_manager(params: Params, arm: ExecutionConfig) -> PositionManager:
    """回放与实盘共用的唯一持仓管理器构造器。"""
    return PositionManager(
        params,
        allow_add=arm.allow_add,
        breakeven_on_reduce=arm.breakeven_on_reduce,
        partial_exit_enabled=arm.partial_exit_enabled,
        accept_reenter_level_exit=arm.accept_reenter_level_exit,
        reject_opp_lock_frac=arm.reject_opp_lock_frac,
        ltf_opposing_reject_exit=arm.ltf_opposing_reject_exit,
    )


def resolve_cost_per_side(
    rp: RouterParams,
    symbol: str,
    *,
    taker_fee: float,
    slippage_per_side: float,
    slippage_by_symbol: Mapping[str, float] | None = None,
) -> RouterParams:
    """按交易场所与品种动态派生单边摩擦成本 ``taker_fee + slippage_by_symbol[symbol]``。

    - 若配置显式固定了数值型 ``rp.cost_per_side``(非 None),保持原值不变;
    - 若 ``rp.cost_per_side is None``(动态场所模式),则按当前交易所/市场真实费率与品种滑点精确派生,
      自动适配加密永续(8bps)、美股/商品/外汇(1~2bps)与高滑点小币,彻底消除全局硬编码 8bps。
    """
    if rp.cost_per_side is not None:
        return rp
    sym_slip = (
        float(slippage_by_symbol[symbol])
        if slippage_by_symbol and symbol in slippage_by_symbol
        else float(slippage_per_side)
    )
    eff = max(0.0, float(taker_fee) + sym_slip)
    return replace(rp, cost_per_side=eff)


def build_router_for_tf(
    bundle: StrategyBundle, tf_label: str, router_factory: type[MarketRouter] = MarketRouter
) -> MarketRouter:
    rp = bundle.router_by_tf.get(tf_label, bundle.router)
    return router_factory(rp)


def select_cognition_route_events(
    events: Sequence[MarketEvent], *, has_trigger_tracker: bool
) -> list[MarketEvent]:
    """多周期级联职责唯一分流函数(回放 _cognition_step 与实盘 _advance 共用):
    - 当挂载了下级触发器(has_trigger_tracker=True,如 1h->15m / 4h->1h / 1d->4h)时,
      原子水平位上的入场信号确认统一由触发周期(MicTracker)在第一时间完成;
      大周期认知收盘仅负责更新原子水平位地图、波段拐点移动止损与宏观 ORIGIN 结构破位出场,不再迟钝重发入场。
    - 当未挂载下级触发器(has_trigger_tracker=False)时,直接由本周期认知收盘事件驱动路由入场。
    """
    if has_trigger_tracker:
        return []
    return list(events)


def peer_lookback_bars(tf_hours: int = 0, *, canvas_bars: int = 160) -> tuple[int, int]:
    """返回跨品种相对走势图短期/中期无量纲棒数窗口(完全锚定画布宽度,零 24h 墙钟假设)。

    默认 canvas_bars=160 ⇒ (10, 32) 根认知棒(W//16, W//5),对加密/美股/商品/外汇与任意周期时钟完全一致。
    """
    _ = tf_hours  # 保留历史签名兼容,不再以 24h/天换算
    w = max(64, int(canvas_bars))
    return max(4, w // 16), max(12, w // 5)


def compute_peer_excess_returns(
    own_ret_48h: float | None,
    own_ret_120h: float | None,
    peer_rets_48h: Sequence[float],
    peer_rets_120h: Sequence[float],
) -> tuple[float, float]:
    """纯 K 线无状态横截面超额对数收益(本标的收益 − 同资产类横截面中位收益)。

    零外部基准硬编码、零状态持久化、重启幂等、跨加密/美股/商品/外汇完全通用;
    同资产类有效同伴不足 4 个时安全退回 (0.0, 0.0)。
    """
    def _excess(own: float | None, peers: Sequence[float]) -> float:
        if own is None or len(peers) < 4:
            return 0.0
        s = sorted(float(x) for x in peers)
        mid = len(s) // 2
        med = s[mid] if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid])
        return float(own) - float(med)

    return _excess(own_ret_48h, peer_rets_48h), _excess(own_ret_120h, peer_rets_120h)


def markowitz_corr_multiplier(
    direction: int,
    own_returns: Sequence[float],
    peer_positions: Sequence[tuple[int, Sequence[float]]],
    *,
    min_obs: int = 16,
) -> float:
    """无状态 Markowitz 零魔法常数协方差仓位调节因子: sqrt(2 / (1 + Σ max(0, d_i · d_j · ρ_ij)))。

    第一性原理(等方差组合风险分配,零拟合参数,回放与实盘共享单一实现,跨加密/美股/商品/外汇通用):
    - 独狼单 / 跨资产零相关 / 对冲反向单(Σ = 0) ⇒ sqrt(2) ≈ 1.414x(奖励真正分散化 Alpha);
    - 同方向高相关跟风拥挤单(Σ 随同向相关持仓数线性增加) ⇒ 自动平滑缩量至等边际组合方差!
    """
    d_i = int(direction)
    n_own = len(own_returns)
    if d_i == 0 or n_own < min_obs:
        return 1.0
    corr_sum = 0.0
    if peer_positions:
        for d_j_raw, p_rets in peer_positions:
            d_j = int(d_j_raw)
            n = min(n_own, len(p_rets))
            if d_j == 0 or n < min_obs:
                continue
            xa = own_returns[n_own - n :]
            xb = p_rets[len(p_rets) - n :]
            ma = sum(xa) / n
            mb = sum(xb) / n
            cov = 0.0
            va = 0.0
            vb = 0.0
            for k in range(n):
                da = xa[k] - ma
                db = xb[k] - mb
                cov += da * db
                va += da * da
                vb += db * db
            den = math.sqrt(va * vb)
            if den <= 1e-15:
                continue
            rho = max(-1.0, min(1.0, cov / den))
            load = float(d_i * d_j) * rho
            if load > 0.0:
                corr_sum += load
    return float(math.sqrt(2.0 / (1.0 + corr_sum)))


def compute_market_breadth(regimes: Sequence[Regime]) -> tuple[float, int]:
    """纯 K 线流内生的横截面市场多空宽度(Cross-Sectional Market Breadth):
    不依赖任何特定品种名称(如 BTC/SPY),仅统计当前活跃宇宙中处于二维单边上行失衡 vs 下行失衡的净占比。
    """
    n = len(regimes)
    if n == 0:
        return 0.0, 0
    up = sum(1 for r in regimes if r is Regime.TREND_UP)
    dn = sum(1 for r in regimes if r is Regime.TREND_DOWN)
    return float(up - dn) / float(n), n


def rwa_session_entry_block(
    is_rwa: bool,
    confirm_ts_ms: int,
    bar: Sequence[float] | None = None,
    *,
    symbol: str | None = None,
    history: Sequence[Sequence[float]] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> str | None:
    """非加密合约"能否新开仓"的执行闸门(回放与实盘共用,策略语义零参与)。

    判定在 :mod:`market_operator.sessions` 里按 **底层市场日历** 完成:
    美股现货(RTH + 假日 + 半日市)、CME 期货(Globex + 每日维护)、即期外汇、
    港股/日股/韩股/台股/印股(各自时区与午休)分开判定,夏令时由 IANA 时区处理。
    ``symbol`` 缺失时按配置的未映射兜底(``config/sessions.json``)。

    - 24x7 原生加密(is_rwa=False)恒放行;
    - 既有持仓的交易所原生止损/止盈单不受本函数影响,保持全天候守护;
    - 确认棒零成交或一字静止(volume<=0 或 high<=low)⇒ ``RWA_INACTIVE_SESSION``
      (覆盖临时停盘与未登记的本地假日);
    - 不在该标的日历时段内 ⇒ ``RWA_MARKET_CLOSED``;
    - 运维覆盖文件 ``mode="24x7"`` 时所有 RWA 恒放行,等同原生加密。
    """
    return sessions.block_reason(is_rwa, symbol, confirm_ts_ms, bar,
                                 history=history, overrides=overrides)


__all__ = [
    "ExecutionConfig",
    "StrategyBundle",
    "blocked_by_market_breadth",
    "build_position_manager",
    "build_router_for_tf",
    "cap_effective_size_multiplier",
    "compute_market_breadth",
    "compute_peer_excess_returns",
    "load_strategy_bundle",
    "markowitz_corr_multiplier",
    "parse_execution_config",
    "parse_router_params",
    "peer_lookback_bars",
    "resolve_cost_per_side",
    "rwa_session_entry_block",
    "select_cognition_route_events",
]


