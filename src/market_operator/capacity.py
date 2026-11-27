"""组合容量:风险预算的唯一口径。

历史上有三份互不一致的实现,分别住在三个地方:

===============================  ==========================================
决策门(整笔跳过)                成交时裁剪(缩量)
===============================  ==========================================
止损亏损 + 平仓侧成本,          纯止损亏损,
只看本周期池                    取 min(本周期池, 账户池)
===============================  ==========================================

于是同一笔交易在"决策时刻"和"成交时刻"被两套定义衡量:一个把平仓费用算进
预算,一个不算。更隐蔽的是池之间的关系——决策只看本周期池,成交才取两者较小
值,而 v21 的 4 个周期池合计 4×2.25% = 9% < 账户池 12%,所以**账户池永远不会
生效**:配置里写着,运行时不参与,读者无从判断它到底管不管用。

本模块把口径收敛成一句话,决策与成交共用:

    仓位最坏情景 = 数量 × |mark − 结构止损|          # 止损被打到的亏损
                 + 数量 × mark × (taker + 滑点)      # 平仓侧成本

    新仓最坏情景 = 数量 × |entry − 结构止损|
                 + 数量 × entry × 2 × (taker + 滑点)  # 双边成本(开 + 平)

生效上限是**每个池各自**的约束(账户池约束全部仓位,周期池约束该周期仓位),
任一池不满足即不满足。决策在 FIFO 时刻**整笔跳过**(不缩量,纪律不变);成交侧
的可用额度按止损风险反解,恒不低于准入判定时用掉的额度,因此"过了门却被砍量"
不会发生(``full_cost`` 下两者同解,另两种模式下成交侧更宽)。

三种预算单位,由配置 ``capacity_mode`` 显式选择:

``admission_full_cost``(生产默认)
    准入按含成本的最坏情景整笔判断,成交裁剪只按止损风险取量。**实测两者都
    对称化会更差**(见 :meth:`RiskBudget.includes_costs`),故保留这个不对称,
    但它现在是一处写明的规则,而不是三份各自演化的代码。
``full_cost``
    两个时刻都含成本(真实最坏情景,最保守)。
``stop_risk``
    两个时刻都只按止损风险(与 ``risk_frac`` 同单位,费用由 PnL 承担)。

默认值等于现行生产行为:沉默不得改变数字,切换口径必须显式写进配置。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

# 决策时刻(路由产生意向时)与成交时刻(经纪商真正建仓时)
DECISION = "decision"
FILL = "fill"
MOMENTS = (DECISION, FILL)

FULL_COST = "full_cost"                    # 两个时刻都按含成本的完整最坏情景
STOP_RISK = "stop_risk"                    # 两个时刻都只按止损风险
ADMISSION_FULL_COST = "admission_full_cost"  # 准入含成本、成交裁剪只按止损风险
CASH_NET_ADMISSION = "cash_net_admission"    # 本金风险口径(浮盈不扣现金池) + 已成交反向仓位 Beta 对冲抵扣
MODES = (FULL_COST, STOP_RISK, ADMISSION_FULL_COST)
VALID_MODES = MODES + (CASH_NET_ADMISSION,)


def timeframe_of(symbol: str | None) -> str | None:
    """``BTCUSDT@4h`` → ``4h``;无周期后缀时返回 None(单周期/测试夹具)。"""
    if symbol and "@" in symbol:
        return symbol.rsplit("@", 1)[1]
    return None


def symbol_root(symbol: str | None) -> str | None:
    """``BTCUSDT@4h`` → ``BTCUSDT``(标的池 B1 的键);无后缀时原样返回。"""
    if symbol and "@" in symbol:
        return symbol.rsplit("@", 1)[0]
    return symbol


def asset_class_of(symbol: str | None,
                   contracts: Mapping[str, Any] | None = None) -> str:
    """从合约规格提取资产类别('rwa' | 'crypto');缺失规格时默认归入 'crypto'。"""
    if not symbol or not contracts:
        return "crypto"
    spec = contracts.get(symbol)
    if spec is None:
        root = symbol_root(symbol)
        spec = contracts.get(root) if root else None
    if spec is not None and bool(getattr(spec, "is_rwa", False)):
        return "rwa"
    return "crypto"


def resolve_dynamic_risk_tier(
    hwm: float,
    tiers: Sequence[tuple[float, float]] | Sequence[Sequence[float]] | None,
    default_frac: float,
) -> float:
    """Resolve active single-trade risk fraction from HWM dynamic risk tiers.

    Tiers are ordered ``(upper_equity_bound, risk_frac)`` pairs. When ``hwm``
    exceeds all upper bounds, the last (most conservative) tier applies.
    Shared by ``Broker``, ``LiveRunner``, and Telegram ``/risk`` reporting.
    """
    if not tiers:
        return float(default_frac)
    hwm_val = float(hwm)
    last_frac = float(default_frac)
    for pair in tiers:
        bound, frac = float(pair[0]), float(pair[1])
        last_frac = frac
        if hwm_val <= bound:
            return frac
    return last_frac


@dataclass(frozen=True)
class Exposure:
    """最坏情景的拆解:止损亏损 + 平仓侧成本。"""

    stop_loss: float = 0.0
    exit_cost: float = 0.0

    @property
    def total(self) -> float:
        return self.stop_loss + self.exit_cost

    def __add__(self, other: Exposure) -> Exposure:
        return Exposure(self.stop_loss + other.stop_loss,
                        self.exit_cost + other.exit_cost)

    def measure(self, *, include_costs: bool) -> float:
        """预算占用口径:含成本 = 真实最坏情景;不含 = 纯止损风险。"""
        return self.total if include_costs else self.stop_loss

    def __str__(self) -> str:                     # pragma: no cover - 诊断输出
        return (f"止损 {self.stop_loss:.4f} + 平仓成本 {self.exit_cost:.4f} "
                f"= {self.total:.4f}")


@dataclass(frozen=True)
class RiskBudget:
    """生效的风险池集合与口径模式。

    ``portfolio_frac``     账户级上限(占权益比例),覆盖全部仓位,None = 不设。
    ``per_timeframe_frac`` 周期级上限,只覆盖该周期的仓位。
    ``mode``               预算单位,见 :meth:`includes_costs`;默认
                           ``admission_full_cost`` = 现行生产行为(切换必须显式,
                           沉默不得改变数字)。
    """

    portfolio_frac: float | None = None
    directional_cap_frac: float | None = None
    per_timeframe_frac: Mapping[str, float] = field(default_factory=dict)
    symbol_frac: Mapping[str, float] = field(default_factory=dict)
    mode: str = ADMISSION_FULL_COST

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"capacity mode 必须是 {VALID_MODES},收到 {self.mode!r}")
        checks: list[tuple[str, float | None]] = [
            ("portfolio_frac", self.portfolio_frac),
            ("directional_cap_frac", self.directional_cap_frac),
        ]
        checks += [(f"per_timeframe_frac[{tf}]", frac)
                   for tf, frac in self.per_timeframe_frac.items()]
        checks += [(f"symbol_frac[{sym}]", frac)
                   for sym, frac in self.symbol_frac.items()]
        for label, frac in checks:
            if frac is None:
                continue
            if not (0.0 < float(frac) <= 1.0):
                raise ValueError(f"{label} 必须在 (0,1],收到 {frac!r}")
        # Freeze internal mapping fields to enforce immutability across layers.
        object.__setattr__(
            self,
            "per_timeframe_frac",
            MappingProxyType({str(k): float(v) for k, v in self.per_timeframe_frac.items()}),
        )
        object.__setattr__(
            self,
            "symbol_frac",
            MappingProxyType({str(k): float(v) for k, v in self.symbol_frac.items()}),
        )

    @property
    def is_active(self) -> bool:
        return (self.portfolio_frac is not None
                or self.directional_cap_frac is not None
                or bool(self.per_timeframe_frac) or bool(self.symbol_frac))

    def pools(self, timeframe: str | None, *,
              moment: str,
              symbol: str | None = None
              ) -> tuple[tuple[str, float, str | None], ...]:
        """该时刻生效的池:``(池名, 上限占比, 约束范围)``。

        范围 ``None`` = 全部仓位;``"4h"`` = 该周期后缀;``"sym:BTCUSDT"`` =
        单个标的(跨周期合并)。账户池在所有时点参与,周期池作为其内部子配额。
        """
        if moment not in MOMENTS:
            raise ValueError(f"moment 必须是 {MOMENTS},收到 {moment!r}")
        tf_frac = self.per_timeframe_frac.get(timeframe) if timeframe else None
        sym_frac = self.symbol_frac.get(symbol) if symbol else None
        active_pools: list[tuple[str, float, str | None]] = []
        if tf_frac is not None:
            active_pools.append(("tf", float(tf_frac), timeframe))
        if self.portfolio_frac is not None:
            active_pools.append(("portfolio", float(self.portfolio_frac), None))
        if sym_frac is not None:
            active_pools.append(("symbol", float(sym_frac), f"sym:{symbol}"))
        return tuple(active_pools)

    def includes_costs(self, moment: str) -> bool:
        """该时刻的预算是否计入平仓侧成本。"""
        if self.mode == STOP_RISK:
            return False
        return not (self.mode in (ADMISSION_FULL_COST, CASH_NET_ADMISSION) and moment == FILL)

    @property
    def cash_principal_basis(self) -> bool:
        """分母为已实现现金(risk_capital)时,分子止损仅计量本金风险(浮盈不倒扣现金额度)。"""
        return self.mode == CASH_NET_ADMISSION

    @property
    def net_directional_hedge(self) -> bool:
        """同向风险上限计量净 Beta 敞口(已成交反向持仓提供天然大盘崩盘/逼空对冲抵扣,未成交挂单不抵扣)。"""
        return self.mode == CASH_NET_ADMISSION


def stop_distance(direction: int, price: float, stop: float | None) -> float:
    """保护性止损的每单位风险;止损缺失/方向错误返回 ``inf``(视为无预算)。"""
    if stop is None or not math.isfinite(stop):
        return math.inf
    return max(0.0, direction * (price - stop))


def position_exposure(qty: float, direction: int, mark: float,
                      stop: float | None, *, taker_fee: float,
                      slippage: float) -> Exposure:
    """单仓最坏情景;止损缺失 ⇒ ``inf``(不给出新增额度,而不是崩溃)。"""
    distance = stop_distance(direction, mark, stop)
    if not math.isfinite(distance):
        return Exposure(stop_loss=math.inf, exit_cost=0.0)
    return Exposure(stop_loss=qty * distance,
                    exit_cost=qty * mark * (taker_fee + slippage))


def single_position_exposure(
    qty: float,
    direction: int,
    entry: float,
    mark: float,
    stop: float | None,
    *,
    taker_fee: float,
    slippage: float,
    cash_basis: bool = False,
) -> Exposure:
    """Single-position worst-case exposure supporting both mark and cash-principal bases."""
    if not cash_basis:
        return position_exposure(
            qty, direction, mark, stop, taker_fee=taker_fee, slippage=slippage
        )
    ref_px = min(entry, mark) if direction > 0 else max(entry, mark)
    distance = stop_distance(direction, ref_px, stop)
    if not math.isfinite(distance):
        return Exposure(stop_loss=math.inf, exit_cost=0.0)
    return Exposure(
        stop_loss=qty * distance,
        exit_cost=qty * mark * (taker_fee + slippage),
    )


def _scope_matches(symbol: str, scope: str | None,
                   timeframe: str | None) -> bool:
    """池范围过滤:``None`` = 全部;``"sym:BTCUSDT"`` = 单标的;``"4h"`` = 周期后缀。"""
    if scope:
        if scope.startswith("sym:"):
            return symbol_root(symbol) == scope[4:]
        return symbol.endswith(f"@{scope}")
    if timeframe:
        return symbol.endswith(f"@{timeframe}")
    return True


def outstanding_exposure(positions: Mapping[str, Any], marks: Mapping[str, float], *,
                         timeframe: str | None = None,
                         scope: str | None = None,
                         direction: int | None = None,
                         asset_class: str | None = None,
                         contracts: Mapping[str, Any] | None = None,
                         taker_fee: float = 0.0,
                         slippage: float = 0.0,
                         cash_basis: bool = False) -> Exposure:
    """全部未平仓仓位的最坏情景;``scope``/``timeframe``/``direction``/``asset_class`` 非 None 时只算该范围。"""
    total = Exposure()
    for symbol, pos in positions.items():
        if direction is not None and getattr(pos, "direction", None) != direction:
            continue
        if asset_class is not None and asset_class_of(symbol, contracts) != asset_class:
            continue
        if not _scope_matches(symbol, scope, timeframe):
            continue

        mark = float(marks.get(symbol, pos.entry))
        entry_px = float(getattr(pos, "entry", mark))
        total = total + single_position_exposure(
            pos.qty,
            pos.direction,
            entry_px,
            mark,
            pos.invalid,
            taker_fee=taker_fee,
            slippage=slippage,
            cash_basis=cash_basis,
        )
    return total


def candidate_exposure(qty: float, entry: float, stop: float | None, *,
                       taker_fee: float, slippage: float) -> Exposure:
    """新仓最坏情景:开平各一次成本,故乘以 2。"""
    distance = stop_distance(1, entry, stop)
    if not math.isfinite(distance):
        return Exposure(stop_loss=math.inf, exit_cost=0.0)
    return Exposure(stop_loss=qty * distance,
                    exit_cost=qty * entry * 2.0 * (taker_fee + slippage))


def delta_exposure(qty: float, price: float, risk_per_unit: float, *,
                   taker_fee: float, slippage: float,
                   include_costs: bool) -> float:
    """新仓占用的预算额度(与 :func:`candidate_exposure` 同一公式的总量)。"""
    if not math.isfinite(risk_per_unit):
        return math.inf
    cost = qty * price * 2.0 * (taker_fee + slippage) if include_costs else 0.0
    return qty * risk_per_unit + cost


def qty_for_budget(headroom: float, price: float, risk_per_unit: float, *,
                   taker_fee: float, slippage: float,
                   include_costs: bool) -> float:
    """:func:`delta_exposure` 的反解:预算剩余能支撑的数量。"""
    if headroom <= 0.0 or not math.isfinite(risk_per_unit) or risk_per_unit <= 0.0:
        return 0.0
    if not math.isfinite(headroom):
        return math.inf
    per_unit = risk_per_unit
    if include_costs:
        per_unit += price * 2.0 * (taker_fee + slippage)
    return headroom / per_unit if per_unit > 0.0 else 0.0


def budget_headroom(pools: Sequence[tuple[str, float, str | None]], equity: float,
                    positions: Mapping[str, Any], marks: Mapping[str, float], *,
                    taker_fee: float, slippage: float,
                    include_costs: bool,
                    reserve_by_tf: Mapping[str, float] | None = None,
                    candidate_timeframe: str | None = None,
                    cash_basis: bool = False
                    ) -> tuple[float, str, float]:
    """各池剩余额度的最小值;返回 ``(剩余额度, 最紧池名, 该池上限占比)``。

    无生效池 ⇒ ``(inf, "", 0.0)``(不限容量)。仓位止损缺失时该池剩余为 0。

    ``reserve_by_tf``:给 ``{tf: frac}`` 里的慢周期保留专属额度,非预留周期准入时先扣除未占用预留。
    """
    if not pools:
        return math.inf, "", 0.0
    best_headroom = math.inf
    best_name = ""
    best_frac = 0.0
    for name, frac, scope in pools:
        used = outstanding_exposure(positions, marks, scope=scope,
                                    taker_fee=taker_fee,
                                    slippage=slippage,
                                    cash_basis=cash_basis).measure(
                                        include_costs=include_costs)
        headroom = frac * equity - used
        if headroom < best_headroom:
            best_headroom, best_name, best_frac = headroom, name, frac
    if reserve_by_tf and candidate_timeframe not in reserve_by_tf:
        reserved = 0.0
        for tf, frac in reserve_by_tf.items():
            used = outstanding_exposure(positions, marks, timeframe=tf,
                                        taker_fee=taker_fee,
                                        slippage=slippage,
                                        cash_basis=cash_basis).measure(
                                            include_costs=include_costs)
            reserved += max(0.0, float(frac) * equity - used)
        if reserved > 0.0 and math.isfinite(best_headroom):
            best_headroom = max(0.0, best_headroom - reserved)
            best_name = f"{best_name}+reserve" if best_name else "reserve"
    return best_headroom, best_name, best_frac


def cap_effective_size_multiplier(
    size_mult: float,
    corr_mult: float = 1.0,
    max_eff_size_mult: float | None = None,
) -> float:
    """合成单笔有效仓位乘数并施加凯利单笔硬封顶(回放 Broker 与实盘 LiveRunner 唯一共享实现)。

    避免多因子放大(如信念乘数 × Markowitz 独狼因子)叠乘越过几何复利凯利峰导致左尾过押回撤放大;
    ``max_eff_size_mult=None`` 时保持原值不截断(向后兼容历史测试)。
    """
    eff = max(0.0, float(size_mult)) * max(0.0, float(corr_mult))
    if max_eff_size_mult is not None and float(max_eff_size_mult) > 0.0:
        eff = min(eff, float(max_eff_size_mult))
    return eff


__all__ = [
    "ADMISSION_FULL_COST", "CASH_NET_ADMISSION", "DECISION", "FILL", "FULL_COST",
    "MODES", "MOMENTS", "STOP_RISK", "VALID_MODES", "Exposure", "RiskBudget",
    "asset_class_of", "budget_headroom", "candidate_exposure",
    "cap_effective_size_multiplier", "delta_exposure",
    "outstanding_exposure", "position_exposure", "qty_for_budget",
    "resolve_dynamic_risk_tier", "single_position_exposure",
    "stop_distance", "symbol_root", "timeframe_of",
]


