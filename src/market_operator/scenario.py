"""场景回放门面:装配链的唯一入口,也是「同一场景同一品类」判分的唯一入口。

配置解析、行情装载、认知缓存、战役帧装配与撮合回放集中于此,
避免不同入口复制装配逻辑而产生默认值漂移。

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
import sys as _sys
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pandas as pd

from . import aggregate, capacity, data as datafeed
from .broker import ContractSpec
from .campaign import CampaignFrame, build_campaign_frame, run_campaigns
from .engine import ExecutionConfig, load_strategy_bundle, parse_execution_config
from .exceptions import DataIntegrityError
from .replay import SymbolCognition, prepare_cognition
from .routing import RouterParams
from .structure import Params
from .trigger import MicCognition, prepare_mic_cognition, resolve_trigger_tf

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_CONFIG = REPO_ROOT / "config" / "strategy.json"
CONTRACTS_PATH = REPO_ROOT / "data" / "contracts.json"

#: 认知周期(默认兜底;生产以 config/strategy.json 的 tf_list 为准)。
COGNITION_TIMEFRAMES: tuple[str, ...] = ("4h", "1d")

#: 认知周期 → 触发周期(秒;None = 该周期不做降档触发,直接用自身收盘)。
DEFAULT_TRIGGER_SECONDS: Mapping[str, int | None] = MappingProxyType({
    "1h": 900, "2h": 1800, "4h": 3600, "12h": 7200, "1d": 14400,
})

#: 触发粒度(秒) → 行情粒度标签。
TRIGGER_GRANULARITY_BY_SECONDS: Mapping[int, str] = MappingProxyType({
    900: "15m", 1800: "30m", 3600: "1h", 7200: "2h", 14400: "4h",
})


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
    funding_rates: Mapping[str, Sequence[Sequence[float]]] | None = None
    slippage_by_symbol: Mapping[str, float] | None = None

    def scaled(self, multiplier: float) -> ExecutionCosts:
        """成本压力测试:同比例放大手续费/滑点/资金费,几何不变。"""
        table = (None if self.funding_rates is None else
                 MappingProxyType({sym: tuple((int(ts), float(rate) * multiplier)
                                              for ts, rate in rows)
                                   for sym, rows in self.funding_rates.items()}))
        per_sym = (None if self.slippage_by_symbol is None else
                   MappingProxyType({sym: float(v) * multiplier
                                     for sym, v in self.slippage_by_symbol.items()}))
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
    arm: ExecutionConfig
    costs: ExecutionCosts
    trigger_seconds_by_timeframe: Mapping[str, int | None]
    tf_cap_by_timeframe: Mapping[str, float]
    portfolio_cap_frac: float
    directional_cap_frac: float | None
    capacity_mode: str
    risk_frac: float

    leverage_cap: float
    dev_frac: float
    n_fill: int
    factor_burst_cap_1h: int = 0
    factor_burst_cap_4h: int = 0
    markowitz_corr_sizing: bool = False
    max_eff_size_mult: float | None = None


def execution_config_from(raw: Mapping[str, Any], path: Path = PRODUCTION_CONFIG) -> ExecutionConfig:
    _ = path
    return parse_execution_config(raw)


def load_settings(config_path: Path = PRODUCTION_CONFIG) -> ScenarioSettings:
    """通过回放/实盘统一加载器 load_strategy_bundle 读取配置,消除双入口分叉。"""
    config_path = Path(config_path)
    bundle = load_strategy_bundle(config_path)
    raw = bundle.raw

    trigger = MappingProxyType({
        tf: (None if seconds is None else int(seconds))
        for tf, seconds in raw.get("trigger_tf_by_htf",
                                   DEFAULT_TRIGGER_SECONDS).items()
    })
    funding_source = str(raw.get("funding_source", "const"))
    if funding_source == "const":
        funding_rates = None
    elif funding_source == "history":
        cfg_syms = tuple(raw.get("symbols") or ())
        avail_syms = tuple(sorted(p.stem for p in FUNDING_DIR.glob("*.csv"))) if FUNDING_DIR.exists() else ()
        target_syms = tuple(dict.fromkeys((*cfg_syms, *avail_syms)))
        funding_rates = load_funding_rates(target_syms)
    else:
        raise ValueError(f"funding_source 必须是 const|history,收到 {funding_source!r}")
    return ScenarioSettings(
        raw=raw,
        structure=bundle.params,
        router=bundle.router,
        router_by_timeframe=bundle.router_by_tf,
        arm=bundle.arm,
        costs=ExecutionCosts(
            taker_fee=float(raw["taker_fee"]),
            maker_fee=float(raw["maker_fee"]),
            slippage_per_side=float(raw["slippage_per_side"]),
            funding_per_8h=float(raw["funding_per_8h_per_side"]),
            funding_rates=funding_rates,
            slippage_by_symbol=(MappingProxyType(dict(raw["slippage_by_symbol"]))
                                if raw.get("slippage_by_symbol") else None),
        ),
        trigger_seconds_by_timeframe=trigger,
        tf_cap_by_timeframe=MappingProxyType(dict(raw.get("tf_cap_by_tf", {}))),
        portfolio_cap_frac=float(raw.get("portfolio_cap_frac", 0.03)),
        directional_cap_frac=(float(raw["directional_cap_frac"])
                              if "directional_cap_frac" in raw and raw["directional_cap_frac"] is not None
                              else None),
        capacity_mode=str(raw.get("capacity_mode", capacity.ADMISSION_FULL_COST)),
        risk_frac=float(raw.get("risk_frac", 0.0075)),
        leverage_cap=float(raw.get("leverage_cap", 20.0)),
        dev_frac=float(raw.get("dev_frac", 0.7)),
        n_fill=int(raw.get("n_fill", 12)),
        factor_burst_cap_1h=int(raw.get("factor_burst_cap_1h", 0) or 0),
        factor_burst_cap_4h=int(raw.get("factor_burst_cap_4h", 0) or 0),
        markowitz_corr_sizing=bool(raw.get("markowitz_corr_sizing", False)),
        max_eff_size_mult=(float(raw["max_eff_size_mult"])
                           if raw.get("max_eff_size_mult") is not None else None),
    )


# ---------------------------------------------------------------------------
# 行情与认知
# ---------------------------------------------------------------------------


@dataclass
class MarketData:
    """装载后的行情:认知帧 + 执行帧 + 触发帧(与成本无关,可跨段跨成本复用)。"""

    symbols: tuple[str, ...]
    cognition: dict[tuple[str, str], SymbolCognition] = field(default_factory=dict)
    cognition_frame: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    execution_frame: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    trigger_frame: dict[str, dict[int, pd.DataFrame]] = field(default_factory=dict)


def _trigger_frame_for(symbol: str, seconds: int) -> pd.DataFrame:
    """触发粒度行情:1h 及以下取原始数据,以上取聚合帧(同一物理来源)。"""
    if seconds == 900:
        return datafeed.load(symbol, "15m")
    if seconds == 1800:
        return datafeed.load(symbol, "30m")
    if seconds == 3600:
        return datafeed.load(symbol)
    return aggregate.load_tf(symbol, TRIGGER_GRANULARITY_BY_SECONDS[seconds])


def _available_trigger_frames(
    symbol: str, needed_seconds: Sequence[int] | None = None
) -> dict[int, pd.DataFrame]:
    """加载该标的所需的触发粒度行情(needed_seconds 指定时按需惰性加载)。"""
    out: dict[int, pd.DataFrame] = {}
    targets = (
        TRIGGER_GRANULARITY_BY_SECONDS
        if needed_seconds is None
        else sorted({int(s) for s in needed_seconds})
    )
    for seconds in targets:
        try:
            out[seconds] = _trigger_frame_for(symbol, seconds)
        except FileNotFoundError:
            if seconds >= 3600:
                raise
    return out


def _prep_symbol_worker(
    args: tuple[str, Params, tuple[str, ...], tuple[int, ...] | None, bool],
) -> tuple[
    str,
    dict[int, pd.DataFrame],
    dict[tuple[str, str], SymbolCognition],
    dict[tuple[str, str], pd.DataFrame],
    dict[tuple[str, str], pd.DataFrame],
]:
    symbol, params, timeframes, needed_trigger_seconds, enable_flip_retest = args
    trig = _available_trigger_frames(symbol, needed_trigger_seconds)
    execution = trig[3600]
    cogs: dict[tuple[str, str], SymbolCognition] = {}
    cframes: dict[tuple[str, str], pd.DataFrame] = {}
    eframes: dict[tuple[str, str], pd.DataFrame] = {}
    for tf in timeframes:
        seconds = aggregate.TF_HOURS[tf] * 3600
        frame = trig.get(seconds)
        if frame is None:
            frame = aggregate.load_tf(symbol, tf)
        cogs[(symbol, tf)] = prepare_cognition(
            frame, params, symbol, enable_e7=enable_flip_retest)
        cframes[(symbol, tf)] = frame
        eframes[(symbol, tf)] = execution
    return symbol, trig, cogs, cframes, eframes


def load_market_data(symbols: Sequence[str], params: Params,
                     timeframes: Sequence[str] = COGNITION_TIMEFRAMES, *,
                     enable_flip_retest: bool = True,
                     needed_trigger_seconds: Sequence[int] | None = None,
                     progress: bool = True) -> MarketData:
    """装载一个品种集所需全部行情,并把认知缓存算到 bar 粒度。"""
    symbols = tuple(symbols)
    tfs = tuple(timeframes)
    needed = None if needed_trigger_seconds is None else tuple(needed_trigger_seconds)
    market = MarketData(symbols=symbols)
    tasks = [(s, params, tfs, needed, enable_flip_retest) for s in symbols]

    main_file = getattr(_sys.modules.get("__main__"), "__file__", None)
    can_mp = len(symbols) > 2 and bool(main_file) and "<stdin>" not in str(main_file)

    if not can_mp:
        results = [_prep_symbol_worker(t) for t in tasks]
    else:
        with ProcessPoolExecutor(max_workers=min(8, len(symbols))) as ex:
            results = list(ex.map(_prep_symbol_worker, tasks))

    for symbol, trig, cogs, cframes, eframes in results:
        market.trigger_frame[symbol] = trig
        market.cognition.update(cogs)
        market.cognition_frame.update(cframes)
        market.execution_frame.update(eframes)
        if progress:
            print(f"[prep] {symbol} 认知就绪", flush=True)
    return market


def _build_trigger_cognition(
    market: MarketData,
    settings: ScenarioSettings,
    symbol: str,
    tf: str,
    obs_seconds: int,
    avail_trig: Mapping[int, pd.DataFrame],
) -> MicCognition | None:
    trigger_seconds = settings.trigger_seconds_by_timeframe.get(tf)
    if trigger_seconds is None:
        return None
    cands_ltf = [a for a in avail_trig if a < obs_seconds and obs_seconds % a == 0]
    if not cands_ltf:
        return None
    eff_trig = trigger_seconds
    if eff_trig not in avail_trig:
        eff_trig, _ = resolve_trigger_tf(
            obs_seconds,
            tf_map={obs_seconds: trigger_seconds},
            available=tuple(avail_trig.keys()),
        )
    return prepare_mic_cognition(
        market.cognition[(symbol, tf)],
        avail_trig[eff_trig],
        settings.structure,
        symbol,
        obs_tf_seconds=obs_seconds,
        trigger_tf_seconds=eff_trig,
    )


def build_frames(market: MarketData, settings: ScenarioSettings,
                 timeframes: Sequence[str] = COGNITION_TIMEFRAMES, *,
                 execution_seconds: int | None = None) -> dict[str, CampaignFrame]:
    """把 (品种 × 认知周期) 装配成战役帧;触发帧按配置降档挂载。"""
    if execution_seconds is None:
        default_exec = 900 if ("1h" in timeframes and "1h" not in settings.trigger_seconds_by_timeframe) else 3600
        execution_seconds = min(
            [default_exec]
            + [int(settings.trigger_seconds_by_timeframe[tf])
               for tf in timeframes if settings.trigger_seconds_by_timeframe.get(tf)])
    frames: dict[str, CampaignFrame] = {}
    for symbol in market.symbols:
        avail_trig = market.trigger_frame[symbol]
        for tf in timeframes:
            obs_seconds = aggregate.TF_HOURS[tf] * 3600
            trigger_cognition = _build_trigger_cognition(
                market, settings, symbol, tf, obs_seconds, avail_trig
            )
            sym_exec_sec = (
                execution_seconds
                if execution_seconds in avail_trig
                else min(avail_trig.keys())
            )
            frame = build_campaign_frame(
                market.cognition[(symbol, tf)],
                avail_trig[sym_exec_sec],
                tf,
                mic_cog=trigger_cognition,
                execution_seconds=sym_exec_sec,
            )
            frames[frame.key] = frame
    return frames


def load_contract_specs(symbols: Sequence[str],
                        path: Path = CONTRACTS_PATH) -> dict[str, ContractSpec]:
    """离线合约规格(必带 sha 校验;缺失或篡改即失败,绝不静默取默认值)。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    specs: dict[str, ContractSpec] = {}
    for symbol in symbols:
        entry = payload["entries"][symbol]
        actual = hashlib.sha256(
            json.dumps(entry["raw"], sort_keys=True).encode("utf-8")).hexdigest()
        if actual != entry["sha256"]:
            raise DataIntegrityError(f"合约规格缓存 sha256 校验失败: {symbol}")
        specs[symbol] = ContractSpec.from_entry(entry["raw"])
    return specs


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


@dataclass
class ScenarioOutcome:
    """一次回放的完整产物。指标是派生量(``summarize``),不是事实本身。"""

    spec: ScenarioSpec
    trades: list[dict[str, Any]]
    equity_rows: list[tuple[Any, ...]]
    counters: dict[str, Any]
    equity0: float

    @property
    def in_segment_trades(self) -> list[dict[str, Any]]:
        return [t for t in self.trades if t.get("in_segment_open")]

    @property
    def closed_trades(self) -> list[dict[str, Any]]:
        return [t for t in self.in_segment_trades if t.get("closed")]


class ScenarioRunner:
    """一次装配、多次回放。装配成本(认知)与回放成本(撮合)严格分离。"""

    def __init__(self, symbols: Sequence[str],
                 config_path: Path | str | None = PRODUCTION_CONFIG, *,
                 timeframes: Sequence[str] | None = None,
                 progress: bool = True) -> None:
        self.config_path = Path(PRODUCTION_CONFIG if config_path is None else config_path)
        self.settings = load_settings(self.config_path)
        if timeframes is None:
            cfg_tfs = self.settings.raw.get("tf_list")
            timeframes = tuple(cfg_tfs) if cfg_tfs else COGNITION_TIMEFRAMES
        self.timeframes = tuple(timeframes)
        needed_sec = {
            3600,
            *(
                aggregate.TF_HOURS[tf] * 3600
                for tf in self.timeframes
                if aggregate.TF_HOURS[tf] * 3600 in TRIGGER_GRANULARITY_BY_SECONDS
            ),
        }
        if "1h" in self.timeframes and "1h" not in self.settings.trigger_seconds_by_timeframe:
            needed_sec.add(900)
        for tf in self.timeframes:
            ts_cfg = self.settings.trigger_seconds_by_timeframe.get(tf)
            if ts_cfg is not None:
                needed_sec.add(int(ts_cfg))
        self.market = load_market_data(symbols, self.settings.structure,
                                       self.timeframes, needed_trigger_seconds=tuple(needed_sec),
                                       progress=progress)
        self.frames = build_frames(self.market, self.settings, self.timeframes)
        self.contracts = load_contract_specs(self.market.symbols)

    def run(self, *, segment: str = "full", cost_multiplier: float = 1.0,
            equity0: float = 300.0,
            router_factory: type[Any] | None = None,
            router_params: RouterParams | None = None,
            router_params_by_timeframe: Mapping[str, RouterParams] | None = None,
            tf_cap_by_timeframe: Mapping[str, float] | None = None,
            capacity_mode: str | None = None,
            directional_cap_frac: float | None = None,
            portfolio_cap_frac: float | None = None,
            risk_frac: float | None = None,
            dynamic_risk_tiers: Sequence[Sequence[float]] | None = None,
            symbol_stop_risk_frac: Mapping[str, float] | None = None,
            reserve_by_tf: Mapping[str, float] | None = None,
            arm: ExecutionConfig | None = None) -> ScenarioOutcome:
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
            risk_frac=settings.risk_frac if risk_frac is None else float(risk_frac),
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
            portfolio_cap_frac=(settings.portfolio_cap_frac
                                if portfolio_cap_frac is None
                                else float(portfolio_cap_frac)),
            directional_cap_frac=(settings.directional_cap_frac
                                  if directional_cap_frac is None
                                  else directional_cap_frac),
            dynamic_risk_tiers=dynamic_risk_tiers,
            symbol_stop_risk_frac=symbol_stop_risk_frac,
            reserve_by_tf=reserve_by_tf,
            factor_burst_cap_1h=settings.factor_burst_cap_1h,
            factor_burst_cap_4h=settings.factor_burst_cap_4h,
            markowitz_corr_sizing=settings.markowitz_corr_sizing,
            max_eff_size_mult=settings.max_eff_size_mult,
            tf_cap_by_tf=dict(settings.tf_cap_by_timeframe
                              if tf_cap_by_timeframe is None
                              else tf_cap_by_timeframe),
            capacity_mode=settings.capacity_mode if capacity_mode is None else str(capacity_mode),
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
    "ScenarioSettings", "ScenarioSpec", "execution_config_from", "build_frames",
    "load_contract_specs",
    "load_market_data", "load_settings",
]
