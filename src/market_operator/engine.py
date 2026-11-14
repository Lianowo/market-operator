"""统一策略装配与执行引擎门面(Replay 回放与 Live 实盘共享唯一代码链路)。

设计纪律:
- 回放(``scenario.py`` / ``campaign.py``)与实盘(``scripts/live_runner.py``)必须通过本模块的
  同一组纯函数加载 ``config/strategy.json``、实例化 ``Params`` / ``RouterParams`` /
  ``ExecutionConfig`` / ``PositionManager``,并执行完全相同的认知/触发事件分流规则。
- 彻底消除"回放读一套默认值、实盘手写另一套分支"的链路分叉风险。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .events import MarketEvent
from .position import PositionManager
from .routing import MarketRouter, RouterParams, WHITELIST_DEFAULT
from .structure import Params, Regime


@dataclass(frozen=True)
class ExecutionConfig:
    """持仓管理与细粒度推进配置(回放与实盘唯一共享定义)。"""

    breakeven_on_reduce: bool = True
    partial_exit_enabled: bool = True
    accept_reenter_level_exit: bool = True
    reject_opp_lock_frac: float = 0.0
    exit_exec: bool = True
    exit_exec_trail: bool = True
    pivot_exec: bool = False


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
        raw=raw,
        params=params,
        router=base_rp,
        router_by_tf=by_tf,
        arm=arm,
    )


def build_position_manager(params: Params, arm: ExecutionConfig) -> PositionManager:
    """回放与实盘共用的唯一持仓管理器构造器。"""
    return PositionManager(
        params,
        breakeven_on_reduce=arm.breakeven_on_reduce,
        partial_exit_enabled=arm.partial_exit_enabled,
        accept_reenter_level_exit=arm.accept_reenter_level_exit,
        reject_opp_lock_frac=arm.reject_opp_lock_frac,
    )


def build_router_for_tf(
    bundle: StrategyBundle, tf_label: str, router_factory: type = MarketRouter
) -> MarketRouter:
    rp = bundle.router_by_tf.get(tf_label, bundle.router)
    return router_factory(rp)


def select_cognition_route_events(
    events: Sequence[MarketEvent], *, has_trigger_tracker: bool
) -> list[MarketEvent]:
    """多周期级联职责唯一分流函数(回放 _cognition_step 与实盘 _advance 共用):
    - 当挂载了下级触发器(has_trigger_tracker=True,如 2h->30m / 4h->1h / 1d->4h)时,
      原子水平位上的入场信号确认统一由触发周期(MicTracker)在第一时间完成;
      大周期认知收盘仅负责更新原子水平位地图、波段拐点移动止损与宏观 ORIGIN 结构破位出场,不再迟钝重发入场。
    - 当未挂载下级触发器(has_trigger_tracker=False)时,直接由本周期认知收盘事件驱动路由入场。
    """
    if has_trigger_tracker:
        return []
    return list(events)


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


def blocked_by_market_breadth(
    direction: int, own_regime: Regime, breadth: float, n_universe: int
) -> bool:
    """系统性风暴保护(纯粹过半数原则 Majority > 50%,零小数魔法常数):
    当活跃资产池规模 >= 4 且全市场净超半数标的处于单边崩盘/逼空风暴时,
    禁止仍滞留在震荡态(RANGE / TRANSITION)的个别滞后标的逆全市场风暴摸顶抄底!
    """
    if n_universe < 4:
        return False
    if own_regime in (Regime.TREND_UP, Regime.TREND_DOWN):
        return False
    if direction > 0 and breadth < -0.5:
        return True
    if direction < 0 and breadth > 0.5:
        return True
    return False

