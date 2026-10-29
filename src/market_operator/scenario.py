"""场景回放门面:装配链的唯一入口,也是「同一场景同一品类」判分的唯一入口。

为什么需要一个门面
------------------
一次回放 = 五段职责串联:配置解析 → 行情装载 → 认知缓存 → 战役帧装配 →
撮合回放。此前这五段被三个诊断脚本各自抄了一份(`_diag_v21_backtest.load_all_data`、
`_diag_v21_tf_sweep.Ctx`、`_diag_v21_abcd.Ctx`),任何一段的默认值改动
(例如 `use_balance_detector` 被硬编码为 True)都只会在其中一个脚本里生效,
"同一场景"因此无法被证明是同一场景。

本模块把这条链固化为**带类型的显式契约**:

- ``ScenarioSpec``      场景 = (品种集, 段, 成本倍数, 配置)。场景身份即判分身份。
- ``ScenarioSettings``  配置解析结果(结构参数 / 路由参数 / 执行成本),数值全部来自
                        配置文件,本模块只做**转写**,不做调参。
- ``MarketData``        装载后的行情(认知帧 + 执行帧 + 触发帧),与成本无关,可跨段复用。
- ``ScenarioRunner``    一次装配(装载 + 建帧),多次回放(段 × 成本倍数)。
- ``ScenarioOutcome``   回放产物(逐笔、净值行、计数器),指标由 ``summarize`` 派生。

拆分原则:装配是纯函数(给定数据与参数,输出唯一),回放是纯函数(给定帧与参数,
输出唯一)。诊断脚本只负责选场景与打印,不再拥有装配逻辑。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import aggregate, capacity, data as datafeed
from .replay import prepare_cognition
from .broker import ContractSpec
from .campaign import ArmConfig, build_campaign_frame, run_campaigns
from .structure import Params
from .routing import RouterParams
from .trigger import prepare_mic_cognition

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_CONFIG = REPO_ROOT / "config" / "strategy.json"
CONTRACTS_PATH = REPO_ROOT / "data" / "contracts.json"

#: 认知周期(策略在哪些周期的收盘上做决策)。顺序即报表顺序。
COGNITION_TIMEFRAMES: tuple[str, ...] = ("2h", "4h", "12h", "1d")

#: 认知周期 → 触发周期(秒;None = 该周期不做降档触发,直接用自身收盘)。
DEFAULT_TRIGGER_SECONDS: dict[str, Optional[int]] = {
    "2h": None, "4h": 7200, "12h": 7200, "1d": 14400,
}

#: 触发粒度(秒) → 行情粒度标签。行情层只提供这四种原始/聚合粒度。
TRIGGER_GRANULARITY_BY_SECONDS: dict[int, str] = {
    1800: "30m", 3600: "1h", 7200: "2h", 14400: "4h",
}

#: 真实资金费率表目录(scripts/fetch_funding.py 落地,manifest 记 sha256)。
FUNDING_DIR = REPO_ROOT / "data" / "raw" / "funding"


def load_funding_rates(symbols: Sequence[str],
                       directory: Path = FUNDING_DIR
                       ) -> dict[str, tuple[tuple[int, float], ...]]:
    """读取 ``{SYM}.csv`` → ``{symbol: ((ts, rate), ...)}``。

    缺失标的不静默跳过:资金费口径一旦切换就必须覆盖全部回放标的,否则不同
    标的会同时跑在真实/常数两种口径下,净 E[R] 不可比。缺文件直接抛错。
    """
    table: dict[str, tuple[tuple[int, float], ...]] = {}
    for symbol in symbols:
        path = directory / f"{symbol}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"funding_source=history 需要 {path};先跑 "
                f"`python scripts/fetch_funding.py`")
        rows: list[tuple[int, float]] = []
        with path.open(encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                if not line.strip():
                    continue
                ts, rate = line.split(",")
                rows.append((int(ts), float(rate)))
        rows.sort()
        table[symbol] = tuple(rows)
    return table


# ---------------------------------------------------------------------------
# 场景与配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioSpec:
    """场景身份:品种集 × 段 × 成本倍数 × 配置。"""

    symbols: tuple[str, ...]
    segment: str = "full"
    cost_multiplier: float = 1.0
    config_path: Path = PRODUCTION_CONFIG
    equity0: float = 300.0

    @property
    def key(self) -> str:
        return f"{'+'.join(self.symbols)}|{self.segment}|{self.cost_multiplier:g}x"


@dataclass(frozen=True)
class ExecutionCosts:
    """撮合侧成本口径(每笔全额计入,不做免佣假设)。

    ``funding_rates``(A1)非空时,资金费按交易所**真实结算表**逐点计提(有符号,
    空头可收费);为空则沿用 ``funding_per_8h`` 常数近似(现行行为,逐位不变)。
    ``slippage_by_symbol``(E1)按标的分档;缺省标的用 ``slippage_per_side``。
    """

    taker_fee: float
    maker_fee: float
    slippage_per_side: float
    funding_per_8h: float
    funding_rates: Optional[Mapping[str, Sequence[Sequence[float]]]] = None
    slippage_by_symbol: Optional[Mapping[str, float]] = None

    def scaled(self, multiplier: float) -> "ExecutionCosts":
        """成本压力测试:同比例放大手续费/滑点/资金费,几何不变。"""
        table = (None if self.funding_rates is None else
                 {sym: tuple((int(ts), float(rate) * multiplier)
                             for ts, rate in rows)
                  for sym, rows in self.funding_rates.items()})
        per_sym = (None if self.slippage_by_symbol is None else
                   {sym: float(v) * multiplier
                    for sym, v in self.slippage_by_symbol.items()})
        return replace(
            self,
            taker_fee=self.taker_fee * multiplier,
            maker_fee=self.maker_fee * multiplier,
            slippage_per_side=self.slippage_per_side * multiplier,
            funding_per_8h=self.funding_per_8h * multiplier,
            funding_rates=table,
            slippage_by_symbol=per_sym,
        )


@dataclass(frozen=True)
class ScenarioSettings:
    """一次场景所需的全部参数,来源唯一(配置文件)。"""

    raw: Mapping[str, Any]
    structure: Params
    router: RouterParams
    router_by_timeframe: Mapping[str, RouterParams]
    arm: ArmConfig
    costs: ExecutionCosts
    trigger_seconds_by_timeframe: Mapping[str, Optional[int]]
    tf_cap_by_timeframe: Mapping[str, float]
    portfolio_cap_frac: float
    directional_cap_frac: Optional[float]
    capacity_mode: str
    risk_frac: float

    leverage_cap: float
    dev_frac: float
    n_fill: int


def arm_config_from(raw: Mapping[str, Any], path: Path = PRODUCTION_CONFIG) -> ArmConfig:
    """``arm_config`` → ``ArmConfig``,忽略仅供实盘使用的键。

    ``config/strategy.json`` 被回放与实盘**共用**,里面可能带只被
    ``scripts/live_runner.py`` 读取的键(联调期常见)。若不认识的键直接塞进
    ``ArmConfig(**...)``,整份生产配置会加载失败 —— 宁可显式列出未知键并忽略,
    也不能让配置与链路互锁。未知键在此打印一次,便于发现拼写错误。

    注:v21.7 起锁利只有 ``reject_opp_lock_frac`` 一个参数(回放与实盘
    共用 ``position.opp_lock_step``),不再有"只给实盘看"的锁利键。
    """
    known = {f.name for f in fields(ArmConfig)}
    arm = dict(raw.get("arm_config", {}))
    unknown = sorted(set(arm) - known)
    if unknown:
        print(f"[config] {path.name}: arm_config 忽略回放链不用的键 {unknown}",
              flush=True)
    return ArmConfig(**{k: v for k, v in arm.items() if k in known})


def load_settings(config_path: Path = PRODUCTION_CONFIG) -> ScenarioSettings:
    """读取配置并转写成带类型的参数包(不发明默认值)。

    唯一一处**显式策略**:平衡箱检测器在正式链路恒为开。它是该链路的事件来源;
    配置写 False 说明配置与链路不匹配,直接报错,不静默降级成"无线箱"的第二条
    语义(否则同一个配置名会对应两种行为,场景不再可比)。
    """
    config_path = Path(config_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not bool(raw.get("use_balance_detector", False)):
        raise ValueError(
            f"{config_path}: 正式链路要求 use_balance_detector=true"
            "(否则事件来源不存在,场景不可比)")
    structure = Params.from_json(config_path)

    whitelist = frozenset(
        (str(event), int(direction)) for event, direction in raw["whitelist"])
    router = RouterParams(
        k_dq=float(raw["k_dq"]),
        k_imp=float(raw["k_imp"]),
        k_is=float(raw["k_is"]),
        turtle_stop_atr=float(raw["turtle_stop_atr"]),
        k_bm=float(raw["k_bm"]),
        whitelist=whitelist,
        **raw.get("router_params", {}),
    )
    router_by_timeframe = {
        tf: replace(router, **overrides)
        for tf, overrides in raw.get("router_params_by_tf", {}).items()
    }
    trigger = {
        tf: (None if seconds is None else int(seconds))
        for tf, seconds in raw.get("trigger_tf_by_htf",
                                   DEFAULT_TRIGGER_SECONDS).items()
    }
    funding_source = str(raw.get("funding_source", "const"))
    if funding_source == "const":
        funding_rates = None
    elif funding_source == "history":
        funding_rates = load_funding_rates(tuple(raw["symbols"]))
    else:
        raise ValueError(f"funding_source 必须是 const|history,收到 "
                         f"{funding_source!r}")
    return ScenarioSettings(
        raw=raw,
        structure=structure,
        router=router,
        router_by_timeframe=router_by_timeframe,
        arm=arm_config_from(raw, config_path),
        costs=ExecutionCosts(
            taker_fee=float(raw["taker_fee"]),
            maker_fee=float(raw["maker_fee"]),
            slippage_per_side=float(raw["slippage_per_side"]),
            funding_per_8h=float(raw["funding_per_8h_per_side"]),
            funding_rates=funding_rates,
            slippage_by_symbol=(dict(raw["slippage_by_symbol"])
                                if raw.get("slippage_by_symbol") else None),
        ),
        trigger_seconds_by_timeframe=trigger,
        tf_cap_by_timeframe=dict(raw.get("tf_cap_by_tf", {})),
        portfolio_cap_frac=float(raw.get("portfolio_cap_frac", 0.03)),
        directional_cap_frac=(float(raw["directional_cap_frac"])
                              if "directional_cap_frac" in raw and raw["directional_cap_frac"] is not None
                              else None),
        capacity_mode=str(raw.get("capacity_mode",
                                  capacity.ADMISSION_FULL_COST)),

        risk_frac=float(raw["risk_frac"]),
        leverage_cap=float(raw["leverage_cap"]),
        dev_frac=float(raw["dev_frac"]),
        n_fill=int(raw["n_fill"]),
    )


# ---------------------------------------------------------------------------
# 行情与认知
# ---------------------------------------------------------------------------


@dataclass
class MarketData:
    """装载后的行情:认知帧 + 执行帧 + 触发帧(与成本无关,可跨段跨成本复用)。"""

    symbols: tuple[str, ...]
    cognition: dict = field(default_factory=dict)        # (sym, tf) -> SymbolCognition
    cognition_frame: dict = field(default_factory=dict)  # (sym, tf) -> DataFrame
    execution_frame: dict = field(default_factory=dict)  # (sym, tf) -> DataFrame(1h)
    trigger_frame: dict = field(default_factory=dict)    # sym -> {seconds: DataFrame}


def _trigger_frame_for(symbol: str, seconds: int):
    """触发粒度行情:1h 及以下取原始数据,以上取聚合帧(同一物理来源)。"""
    if seconds == 1800:
        return datafeed.load(symbol, "30m")
    if seconds == 3600:
        return datafeed.load(symbol)
    return aggregate.load_tf(symbol, TRIGGER_GRANULARITY_BY_SECONDS[seconds])


def load_market_data(symbols: Sequence[str], params: Params,
                     timeframes: Sequence[str] = COGNITION_TIMEFRAMES, *,
                     enable_flip_retest: bool = True,
                     progress: bool = True) -> MarketData:
    """装载一个品种集所需全部行情,并把认知缓存算到 bar 粒度。

    认知缓存与成本、段、路由参数无关,因此一次装载可服务多段多成本回放 ——
    这是"同场景"可复现的物理基础:所有对照共享同一份认知对象。
    """
    symbols = tuple(symbols)
    market = MarketData(symbols=symbols)
    for symbol in symbols:
        market.trigger_frame[symbol] = {
            seconds: _trigger_frame_for(symbol, seconds)
            for seconds in TRIGGER_GRANULARITY_BY_SECONDS
        }
        for tf in timeframes:
            frame = aggregate.load_tf(symbol, tf)
            market.cognition[(symbol, tf)] = prepare_cognition(
                frame, params, symbol, enable_e7=enable_flip_retest)
            market.cognition_frame[(symbol, tf)] = frame
            market.execution_frame[(symbol, tf)] = datafeed.load(symbol)
        if progress:
            print(f"[prep] {symbol} 认知就绪", flush=True)
    return market


def build_frames(market: MarketData, settings: ScenarioSettings,
                 timeframes: Sequence[str] = COGNITION_TIMEFRAMES) -> dict:
    """把 (品种 × 认知周期) 装配成战役帧;触发帧按配置降档挂载。"""
    frames: dict = {}
    for symbol in market.symbols:
        for tf in timeframes:
            trigger_seconds = settings.trigger_seconds_by_timeframe.get(tf)
            trigger_cognition = None
            if trigger_seconds is not None:
                trigger_cognition = prepare_mic_cognition(
                    market.cognition_frame[(symbol, tf)],
                    market.trigger_frame[symbol][trigger_seconds],
                    settings.structure, symbol,
                    obs_tf_seconds=aggregate.TF_HOURS[tf] * 3600,
                    trigger_tf_seconds=trigger_seconds,
                    enable_e7=True,
                )
            frame = build_campaign_frame(
                market.cognition[(symbol, tf)],
                market.execution_frame[(symbol, tf)],
                tf, mic_cog=trigger_cognition)
            frames[frame.key] = frame
    return frames


def load_contract_specs(symbols: Sequence[str],
                        path: Path = CONTRACTS_PATH) -> dict:
    """离线合约规格(必带 sha 校验;缺失或篡改即失败,绝不静默取默认值)。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    specs: dict = {}
    for symbol in symbols:
        entry = payload["entries"][symbol]
        actual = hashlib.sha256(
            json.dumps(entry["raw"], sort_keys=True).encode("utf-8")).hexdigest()
        if actual != entry["sha256"]:
            raise RuntimeError(f"合约规格缓存 sha256 校验失败: {symbol}")
        specs[symbol] = ContractSpec.from_entry(entry["raw"])
    return specs


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


@dataclass
class ScenarioOutcome:
    """一次回放的完整产物。指标是派生量(``summarize``),不是事实本身。"""

    spec: ScenarioSpec
    trades: list
    equity_rows: list
    counters: dict
    equity0: float

    @property
    def in_segment_trades(self) -> list:
        return [t for t in self.trades if t.get("in_segment_open")]

    @property
    def closed_trades(self) -> list:
        return [t for t in self.in_segment_trades if t.get("closed")]


class ScenarioRunner:
    """一次装配、多次回放。装配成本(认知)与回放成本(撮合)严格分离。"""

    def __init__(self, symbols: Sequence[str],
                 config_path: Path = PRODUCTION_CONFIG, *,
                 timeframes: Sequence[str] = COGNITION_TIMEFRAMES,
                 progress: bool = True) -> None:
        self.config_path = Path(config_path)
        self.settings = load_settings(self.config_path)
        self.timeframes = tuple(timeframes)
        self.market = load_market_data(symbols, self.settings.structure,
                                       self.timeframes, progress=progress)
        self.frames = build_frames(self.market, self.settings, self.timeframes)
        self.contracts = load_contract_specs(self.market.symbols)

    def run(self, *, segment: str = "full", cost_multiplier: float = 1.0,
            equity0: float = 300.0,
            router_factory: Optional[type] = None,
            router_params: Optional[RouterParams] = None,
            router_params_by_timeframe: Optional[Mapping[str, RouterParams]] = None,
            tf_cap_by_timeframe: Optional[Mapping[str, float]] = None,
            capacity_mode: Optional[str] = None,
            directional_cap_frac: Optional[float] = None,
            arm: Optional[ArmConfig] = None) -> ScenarioOutcome:

        """按场景回放。所有可替换项缺省为配置值(即"现行语义")。"""
        settings = self.settings
        costs = settings.costs.scaled(cost_multiplier)
        spec = ScenarioSpec(symbols=self.market.symbols, segment=segment,
                            cost_multiplier=cost_multiplier,
                            config_path=self.config_path, equity0=equity0)
        result = run_campaigns(
            self.frames,
            segment=segment,
            dev_frac=settings.dev_frac,
            equity0=equity0,
            risk_frac=settings.risk_frac,
            leverage_cap=settings.leverage_cap,
            taker_fee=costs.taker_fee,
            maker_fee=costs.maker_fee,
            slippage_per_side=costs.slippage_per_side,
            funding_per_8h=costs.funding_per_8h,
            cost_multiplier=cost_multiplier,
            funding_rates=costs.funding_rates,
            slippage_by_symbol=costs.slippage_by_symbol,
            contracts=self.contracts,
            params=settings.structure,
            n_fill=settings.n_fill,
            portfolio_cap_frac=settings.portfolio_cap_frac,
            directional_cap_frac=(settings.directional_cap_frac
                                  if directional_cap_frac is None
                                  else directional_cap_frac),
            tf_cap_by_tf=dict(settings.tf_cap_by_timeframe

                              if tf_cap_by_timeframe is None
                              else tf_cap_by_timeframe),
            capacity_mode=settings.capacity_mode if capacity_mode is None
            else str(capacity_mode),
            arm=settings.arm if arm is None else arm,
            router_params=settings.router if router_params is None else router_params,
            router_params_by_tf=dict(settings.router_by_timeframe
                                     if router_params_by_timeframe is None
                                     else router_params_by_timeframe),
            enable_mic=True,
            router_factory=router_factory,
        )
        return ScenarioOutcome(
            spec=spec, trades=list(result.trades),
            equity_rows=list(result.equity_rows),
            counters=dict(getattr(result, "counters", {}) or {}),
            equity0=equity0)


__all__ = [
    "COGNITION_TIMEFRAMES", "CONTRACTS_PATH", "DEFAULT_TRIGGER_SECONDS",
    "PRODUCTION_CONFIG", "REPO_ROOT", "TRIGGER_GRANULARITY_BY_SECONDS",
    "ExecutionCosts", "MarketData", "ScenarioOutcome", "ScenarioRunner",
    "ScenarioSettings", "ScenarioSpec", "arm_config_from", "build_frames",
    "load_contract_specs",
    "load_market_data", "load_settings",
]
