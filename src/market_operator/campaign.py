"""战役引擎:多时间尺度并行战役 + 组合容量(预算公式在 capacity;出处 docs/v6-protocol.md §1)。

规范地位:多尺度战役引擎是规范语义链的执行/风险层(唯一,不重命名)。
语义合并后 A/B 臂切换移除,持仓侧接线固化为原臂 B(turtle)口径:
PositionManager(breakeven_on_reduce=True, time_stop=False);路由策略语义
见 routing docstring。历史实现由 Git 提交追溯。

基础设施(非策略变更):每 (symbol, TF ∈ {1h,2h,4h,12h,1d}) 为一个独立
战役 —— 自有 EventDetector 认知缓存(SymbolCognition,TF 帧)、策略状态
(MarketRouter 实例)、持仓(PositionManager 实例)、挂单;战役之间除账户权益
与组合风控(共享 Broker)外互不感知、互不触发对方的出场/防线。

时间约定(v4/v5 同构推广;正确性硬约束的实现基础):
- 认知(决策)帧 = TF bar;决策在 TF bar t **收盘**做出(ledger decision_ts
  = TF bar 标签 t,知时 = t + TF);
- 执行路径:TF=1h → 15m CSV(复用 v4 的 ExecFrame/15m 契约);TF ≥ 2h →
  1h CSV。认知 bar t 的执行路径区间 = [t, t + TF),恒由更细帧 bar 组成
  (build_campaign_frame 对齐断言:无洞、无错位、首/末不完整桶如实);
- 市价(OPEN/REDUCE/EXIT/强平/TIMEOUT_TO_TAKER)成交于决策 bar 之后的
  **下一执行细 bar 开盘**:引擎保证决策只在认知 bar 的最末执行细 bar 处理
  完成后提交,下一次该战役的执行细 bar(ts = t + TF)即撮合 —— 价格 = 该细
  bar 开盘(滑点调整),fill_ts > decision_ts 恒成立(Broker 行内断言 +
  模块末 assert_fill_after_decision);
- 限价挂单:RestingLimitOrder(armed = 认知 bar 标签 t),成交/过期判定在
  更细帧执行 bar 上逐根进行;**可成交窗口排除决策认知 bar 的全部细 bar**
  (fill_after_ts = t + TF − exec_step,v6 新增字段,TF=1h 时 = t,
  与 v4/v5 行为完全一致);expiry = fill_after + n_fill × 1h(N_fill=12,
  按墙钟 12 小时统一,各 TF 不新引入先验);严格穿过成交(maker)、持平不
  成交、穿透缺口按 limit 价成交(保守,沿用 v4/v5 声明);
- 统一真实时钟:所有标的先解决已结束执行区间的止损/挂单,批量发布收盘
  盯市与资金费,再推进持仓/认知/MIC收盘决策;最后批量发布下一执行开盘
  报价并撮合。MIC不再另行撮合粗OHLC。比执行粒度更细的信号只能等待下一
  可用执行开盘,限价也不得使用信号形成前已开始的执行区间。
- 同一阶段内按 campaign key 字典序FIFO分配容量。这是明确的竞争规则,
  不是宣称交换标的名称也能得到相同成交。所有标的先经过阶段屏障,避免
  某个标的的下一根完整行情在其他标的当前开盘之前可见。

组合约束(口径唯一,见 capacity 模块):
- 准入:本周期池内 已占用最坏情景 + 新仓最坏情景 ≤ 上限 × equity_mtm,最坏
  情景 = 止损亏损 + 平仓侧 taker+滑点;超出则按信号时间 FIFO **整笔跳过**
  (不缩量),账本外记录 cap_skips(ts/campaign/事件/方向/预算),counters 累计
  "portfolio_cap_skips";
- 成交:同一模块按止损风险反解数量(账户池与名义杠杆上限做第二层钳制)。
  两个时刻的预算单位不同(准入含费用、裁剪只算止损风险)是**有意**的:实测
  对称化会让净收益变差,理由与数字见 capacity.RiskBudget.includes_costs。
- 每战役同时一仓一挂单(槽位纪律,同 v5);BREAKOUT_ACCEPTED 优先级撤单不动 §8 减仓单
  (持仓不与入场挂单共存)。

段(§9 同一规则):每战役按自身 TF 帧 split_boundary(n, dev_frac);OPEN
决策仅当决策 bar ∈ 段内;持仓管理全程;交易按 OPEN 决策 bar ∈ 段归属。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from . import capacity
from .aggregate import TF_HOURS
from .broker import Broker, ContractSpec, OrderIntent, OrderKind, RestingLimitOrder, floor_step
from .engine import (
    ExecutionConfig,
    build_position_manager,
    compute_peer_excess_returns,
    markowitz_corr_multiplier,
    peer_lookback_bars,
    resolve_cost_per_side,
    rwa_session_entry_block,
    select_cognition_route_events,
)
from .exceptions import StrategyExecutionError
from .hypothesis import StructureView, execution_target
from .position import Position, PositionManager
from .replay import (
    MS_15M,
    MS_1H,
    BacktestResult,
    SymbolCognition,
    annotate_excursions,
    assert_aligned,
    assert_fill_after_decision,
    reconstruct_trades,
)
from .routing import MarketRouter, RouteDecision, RouterParams
from .split import split_boundary
from .structure import Params

_MIN_CAPACITY_SCALE_FRAC = 0.25
_CAPACITY_SCALE_MARGIN = 0.999
_CORR_LOOKBACK_BARS = 60
_RWA_HISTORY_WINDOW_BARS = 250


def require_exec_pivots(pivot_exec: bool, frames: Iterable[CampaignFrame]) -> None:
    """``pivot_exec`` 的装配前置条件:帧上必须真有细周期 pivot 流。"""
    if pivot_exec and not any(getattr(f, "exec_pivots", None) for f in frames):
        raise ValueError(
            "pivot_exec=True 但场景帧没有 exec_pivots。请改用 exit_exec,或先补齐细周期 "
            "pivot 装配,否则该开关会静默无效。")


def _increment_counter(counters: dict[str, Any], key: str, n: int = 1) -> None:
    """Accumulate optional diagnostic counters without prepopulating keys."""
    counters[key] = counters.get(key, 0) + n


def _count_go(
    counters: dict[str, Any],
    events: Sequence[Any],
    go_set: frozenset[tuple[str, int]],
    *,
    mic: bool = False,
) -> int:
    """统计送入路由的 GO 事件次数,并返回本次条数。"""
    n = sum(1 for e in events if (e.id, e.direction) in go_set)
    _increment_counter(counters, "go_events", n)
    if mic:
        _increment_counter(counters, "mic_go_events", n)
    return n


def campaign_key(symbol: str, tf_label: str) -> str:
    """战役键(broker/账本/交易归属的复合键;含 "@" 与 symbol 分隔安全)。"""
    return f"{symbol}@{tf_label}"


def parse_key(key: str) -> tuple[str, str]:
    """campaign_key 逆变换:返回 (symbol, tf_label)。"""
    sym, tf = key.rsplit("@", 1)
    return sym, tf


# ---------------------------------------------------------------------------
# 战役帧(认知 TF bar + 执行细帧,严格对齐断言)
# ---------------------------------------------------------------------------


@dataclass
class CampaignFrame:
    """单战役:认知缓存(TF bar)+ 执行细帧(已对齐断言)。"""

    key: str
    symbol: str
    tf_label: str
    tf_hours: int
    ms_exec: int
    cog: SymbolCognition
    exec_bars: dict[int, tuple[int, float, float, float, float, float]]
    exec_labels: tuple[int, ...]
    close_edge: dict[int, list[int]]
    exec_index_of: dict[int, list[int]]
    mic: Any | None = None
    exec_pivots: dict[int, list[Any]] = field(default_factory=dict)
    cog_opens: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.cog_opens and self.cog is not None and self.cog.bars:
            self.cog_opens = tuple(int(b[0]) for b in self.cog.bars)


def build_campaign_frame(cog: SymbolCognition, df_exec: pd.DataFrame,
                         tf_label: str,
                         mic_cog: Any | None = None, *,
                         execution_seconds: int | None = None) -> CampaignFrame:
    """装配战役帧:认知 TF bar ⟷ 执行细 bar 对齐(硬约束,违即 raise)。

    认知 bar t 的执行区间 [t, t+TF) 必须由更细帧 bar 无洞组成:逐认知 bar
    断言区间内标签恰落在执行网格上且连续(首/末不完整聚合桶只截头尾,不
    许内部缺根);任一认知 bar 若一个细 bar 都没有 ⇒ 区间无法覆盖 ⇒ raise。
    """
    if tf_label not in TF_HOURS:
        raise ValueError(f"unknown tf_label {tf_label!r}")
    tf_hours = TF_HOURS[tf_label]
    ms_tf = tf_hours * MS_1H
    ms_exec = (MS_15M if tf_hours == 1 else MS_1H) if execution_seconds is None else int(execution_seconds) * 1000
    if ms_exec not in (MS_15M, 2 * MS_15M, MS_1H) or ms_tf % ms_exec:
        raise ValueError("execution step must be 15m, 30m or 1h and divide cognition")
    n_fine = ms_tf // ms_exec

    rows = [(int(r.ts), float(r.open), float(r.high), float(r.low),
             float(r.close), float(r.volume))
            for r in df_exec.itertuples(index=False)]
    exec_bars = {b[0]: b for b in rows}
    if len(exec_bars) != len(rows):
        raise AssertionError(f"{cog.symbol}@{tf_label} duplicate exec ts")
    exec_labels = tuple(sorted(exec_bars))
    for a, b in zip(exec_labels, exec_labels[1:]):
        if b - a != ms_exec:
            raise AssertionError(
                f"{cog.symbol}@{tf_label} exec grid gap {a} -> {b} "
                f"(step {ms_exec})")
    if tf_hours == 1 and ms_exec == MS_15M:
        # 1h 战役配 15m 执行帧时:复用 v4 的 1h⟷15m 覆盖断言;配 30m/1h 执行帧时走下方通用网格对齐断言
        assert_aligned(cog.bars, rows)
    # TF ≥ 2h:首/末认知桶可为不完整聚合桶(同 aggregate.py 口径,如实保留,
    # 数据缺失只发生在桶的头或尾);对齐完整性由逐认知 bar 的"区间内无洞
    # + 非空 + 于执行网格上"断言承担(见下方循环)。

    close_edge: dict = {}
    exec_index_of: dict = {}
    for i, bar in enumerate(cog.bars):
        t = int(bar[0])
        if t % ms_tf != 0 or t % ms_exec != 0:
            raise AssertionError(
                f"{cog.symbol}@{tf_label} cognition bar {t} off grid")
        span = [t + k * ms_exec for k in range(n_fine)
                if t + k * ms_exec in exec_bars]
        if not span:
            raise AssertionError(
                f"{cog.symbol}@{tf_label} cognition bar {t} has no exec bars")
        # 区间内无洞:在场细 bar 必须连续成网格
        for a, b in zip(span, span[1:]):
            if b - a != ms_exec:
                raise AssertionError(
                    f"{cog.symbol}@{tf_label} cognition bar {t}: exec hole "
                    f"{a} -> {b}")
        exec_index_of[i] = span
        close_edge.setdefault(span[-1], []).append(i)
    if mic_cog is not None:
        # v10 [§A-锚]:trigger 帧必须逐认知 bar 覆盖(prepare_mic_cognition
        # 同名断言已保证;此处只对战役帧再钉一遍,违即 raise)
        if len(mic_cog.bars) != len(mic_cog.cog_index_of):
            raise AssertionError(f"{cog.symbol}@{tf_label} mic bars/index 错位")
        have = set(mic_cog.cog_index_of)
        missing = [i for i in range(len(cog.bars)) if i not in have]
        if missing:
            raise AssertionError(
                f"{cog.symbol}@{tf_label} mic 缺认知 bar {missing[:4]}…")
    return CampaignFrame(
        key=campaign_key(cog.symbol, tf_label), symbol=cog.symbol,
        tf_label=tf_label, tf_hours=tf_hours, ms_exec=ms_exec, cog=cog,
        exec_bars=exec_bars, exec_labels=exec_labels, close_edge=close_edge,
        exec_index_of=exec_index_of, mic=mic_cog)


# ---------------------------------------------------------------------------
# 引擎:全部战役合并执行细帧时间轴
# ---------------------------------------------------------------------------


@dataclass
class _CampaignState:
    """运行期战役状态(MarketRouter + PositionManager + 段界)。"""

    frame: CampaignFrame
    router: MarketRouter
    pos_mgr: PositionManager
    lo: int
    hi: int
    last_atr: float = 0.0      # 最近一次认知 bar 的 ATR(执行细 bar 出场推进复用)


@dataclass(slots=True)
class _ReplayClock:
    """Mutable simulation clock shared cleanly with correlation sizing callbacks."""

    now_ms: int = 0


def _init_campaign_states(
    frames: Mapping[str, CampaignFrame],
    keys: Sequence[str],
    *,
    segment: str,
    dev_frac: float,
    params: Params,
    base_rp: RouterParams,
    router_cls: type[Any],
    router_params_by_tf: Mapping[str, RouterParams] | None,
    arm: ExecutionConfig | None,
    taker_fee: float,
    slippage_per_side: float,
    slippage_by_symbol: Mapping[str, float] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, _CampaignState]]:
    seg_bounds: dict[str, dict[str, Any]] = {}
    states: dict[str, _CampaignState] = {}
    eff_arm = arm if arm is not None else ExecutionConfig()
    for key in keys:
        f = frames[key]
        n = len(f.cog.bars)
        b = split_boundary(n, dev_frac)
        lo, hi = (0, n) if segment == "full" else ((0, b) if segment == "dev" else (b, n))
        seg_bounds[key] = {
            "lo": lo,
            "hi": hi,
            "n": n,
            "ts_lo": f.cog.bars[lo][0] if lo < n else None,
            "ts_hi": f.cog.bars[hi - 1][0] if hi > 0 else None,
        }
        rp_for_key = (
            router_params_by_tf.get(f.tf_label, base_rp)
            if router_params_by_tf is not None
            else base_rp
        )
        rp_for_key = resolve_cost_per_side(
            rp_for_key,
            f.symbol,
            taker_fee=taker_fee,
            slippage_per_side=slippage_per_side,
            slippage_by_symbol=slippage_by_symbol,
        )
        states[key] = _CampaignState(
            frame=f,
            router=router_cls(rp_for_key),
            pos_mgr=build_position_manager(params, eff_arm),
            lo=lo,
            hi=hi,
        )
    return seg_bounds, states


def _build_corr_size_fn(
    frames: Mapping[str, CampaignFrame],
    keys: Sequence[str],
    clock: _ReplayClock,
) -> Callable[[Broker, str, int], float]:
    """Build causal Markowitz covariance sizing callback backed by 4h (or finest) log-returns."""
    by_root: dict[str, CampaignFrame] = {}
    for k_f in keys:
        fr = frames[k_f]
        prev = by_root.get(fr.symbol)
        if prev is None or (fr.tf_hours == 4) or (prev.tf_hours != 4 and fr.tf_hours < prev.tf_hours):
            by_root[fr.symbol] = fr

    ret_series: dict[str, tuple[list[int], list[float]]] = {}
    for root_sym, fr in by_root.items():
        ms_c = fr.tf_hours * MS_1H
        ts_list: list[int] = []
        lr_list: list[float] = []
        prev_c = 0.0
        for b in fr.cog.bars:
            c_val = float(b[4])
            ts_list.append(int(b[0]) + ms_c)
            lr_list.append(math.log(c_val / prev_c) if (prev_c > 0.0 and c_val > 0.0) else 0.0)
            prev_c = c_val
        ret_series[root_sym] = (ts_list, lr_list)

    def _corr_size_fn(brk: Broker, sym_key: str, d_i: int) -> float:
        root_i = capacity.symbol_root(sym_key)
        now_ms = clock.now_ms
        own_pair = ret_series.get(root_i) if root_i else None
        own_rets: Sequence[float] = ()
        if own_pair is not None:
            ts_i, lr_i = own_pair
            idx_i = bisect_right(ts_i, now_ms)
            own_rets = lr_i[max(0, idx_i - _CORR_LOOKBACK_BARS) : idx_i]
        peers: list[tuple[int, Sequence[float]]] = []
        for k_j, p_j in brk.positions.items():
            root_j = capacity.symbol_root(k_j)
            if root_j == root_i or not root_j:
                continue
            p_pair = ret_series.get(root_j)
            if p_pair is None:
                continue
            ts_j, lr_j = p_pair
            idx_j = bisect_right(ts_j, now_ms)
            peers.append((int(p_j.direction), lr_j[max(0, idx_j - _CORR_LOOKBACK_BARS) : idx_j]))
        return markowitz_corr_multiplier(d_i, own_rets, peers)

    return _corr_size_fn


def _build_execution_schedule(
    frames: Mapping[str, CampaignFrame], keys: Sequence[str], *, enable_mic: bool
) -> tuple[dict[int, list[str]], dict[int, list[str]], dict[int, list[tuple[str, int]]]]:
    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}
    mic_closes: dict[int, list[tuple[str, int]]] = {}
    for key in keys:
        f = frames[key]
        for label in f.exec_labels:
            opens.setdefault(label, []).append(key)
            closes.setdefault(label + f.ms_exec, []).append(key)
        if enable_mic and f.mic is not None:
            step = f.mic.trigger_tf_seconds * 1000
            if step < f.ms_exec or step % f.ms_exec:
                raise ValueError(
                    f"{key}: execution step must divide trigger step; load finer execution data"
                )
            for index, bar in enumerate(f.mic.bars):
                mic_closes.setdefault(int(bar[0]) + step, []).append((key, index))
    return opens, closes, mic_closes


def _process_closing_moment(
    moment: int,
    closing: Sequence[str],
    *,
    frames: Mapping[str, CampaignFrame],
    states: dict[str, _CampaignState],
    broker: Broker,
    counters: dict[str, Any],
    cap_skips: list[dict[str, Any]],
    seg_bounds: Mapping[str, dict[str, Any]],
    go_set: frozenset[tuple[str, int]],
    n_fill: int,
    claim_events: list[dict[str, Any]],
    claim_created_index: dict[str, dict[str, Any]],
    exit_exec: bool,
    exit_exec_trail: bool,
    pivot_exec: bool,
) -> None:
    if not closing:
        return
    # 1. Resolve ended intervals before publishing closing prices.
    for key in closing:
        st = states[key]
        f = st.frame
        label = moment - f.ms_exec
        _, o, h, low, c, _ = f.exec_bars[label]
        stop = broker.execute_protective_stop(key, label, o, h, low)
        if stop is not None:
            _apply_open_rows(st, broker, key, label, [stop], counters, claim_events, claim_created_index)
            counters["protective_stop_fills"] += 1
        had_position = broker.position_of(key) is not None
        for mic_only in (False, True):
            rows = broker.on_15m_bar(key, label, o, h, low, c, mic_only=mic_only)
            _apply_resting_rows(st, broker, key, label, rows, counters, claim_events, claim_created_index)
        if not had_position and broker.position_of(key) is not None:
            stop = broker.execute_protective_stop(key, label, o, h, low)
            if stop is not None:
                _apply_open_rows(st, broker, key, label, [stop], counters, claim_events, claim_created_index)
                counters["protective_stop_fills"] += 1

    # 2. Publish closing prices and run liquidation checks across the whole portfolio.
    broker.mark_batch({key: frames[key].exec_bars[moment - frames[key].ms_exec][4] for key in closing}, moment)
    counters["liquidations"] = broker.n_liquidations

    # 3. Management and cognition on already closed intervals.
    for key in closing:
        st = states[key]
        f = st.frame
        label = moment - f.ms_exec
        q = f.exec_bars[label]
        if exit_exec or pivot_exec:
            pos = st.pos_mgr.positions.get(f.symbol)
            if pos is not None and broker.position_of(key) is not None and label >= int(pos.opened_ts):
                pivots = f.exec_pivots.get(label, ()) if pivot_exec else ()
                if exit_exec or pivots:
                    _increment_counter(counters, "exit_exec_ticks" if exit_exec else "pivot_exec_ticks")
                    before = _claim_state(pos)
                    mgmt = st.pos_mgr.on_bar(
                        f.symbol, q, st.last_atr, new_pivots=pivots, price_only=True, skip_trail=not exit_exec_trail
                    )
                    _record_claim_transitions(claim_events, key, broker, pos, before, moment, source="execution")
                    broker.update_protective_invalid(key, pos.invalid)
                    _apply_mgmt(st, broker, mgmt, label, counters, n_fill, known_at=moment)
        for i in f.close_edge.get(label, ()):
            if int(f.cog.bars[i][0]) + f.tf_hours * MS_1H <= moment:
                _cognition_step(
                    st, broker, i, counters, cap_skips, seg_bounds[key], go_set, n_fill, claim_events, states=states
                )


def _process_mic_batch(
    moment: int,
    mic_batch: Sequence[tuple[str, int]],
    *,
    frames: Mapping[str, CampaignFrame],
    states: dict[str, _CampaignState],
    broker: Broker,
    counters: dict[str, Any],
    cap_skips: list[dict[str, Any]],
    seg_bounds: Mapping[str, dict[str, Any]],
    go_set: frozenset[tuple[str, int]],
    n_fill: int,
    claim_events: list[dict[str, Any]],
) -> None:
    if not mic_batch:
        return
    ordered = mic_batch
    if len(mic_batch) > 1:
        def _mic_rank(item: tuple[str, int]) -> tuple[int, float, float, str]:
            k_m, idx_m = item
            fm = frames[k_m]
            mic_views = getattr(fm.mic, "views", None) if fm.mic is not None else None
            vw_m = mic_views.get(idx_m) if isinstance(mic_views, dict) else None
            vr_s = float(getattr(vw_m, "cog_vr4", 1.0)) if vw_m is not None else 1.0
            imb_s = abs(float(getattr(vw_m, "imbalance_strength", 0.0))) if vw_m is not None else 0.0
            return (-fm.tf_hours, -vr_s, -imb_s, k_m)

        ordered = sorted(mic_batch, key=_mic_rank)

    for key, index in ordered:
        st = states[key]
        _mic_trigger_step(
            st, broker, index, moment - st.frame.ms_exec,
            counters, cap_skips, seg_bounds[key], go_set,
            n_fill, claim_events, execution_only=True, states=states,
        )


def _process_opening_moment(
    moment: int,
    opening: Sequence[str],
    *,
    frames: Mapping[str, CampaignFrame],
    states: dict[str, _CampaignState],
    broker: Broker,
    counters: dict[str, Any],
    claim_events: list[dict[str, Any]],
    claim_created_index: dict[str, dict[str, Any]],
) -> None:
    if not opening:
        return
    broker.last_mark.update({key: frames[key].exec_bars[moment][1] for key in opening})
    ordered = opening
    if len(opening) > 1:
        def _open_rank(k_o: str) -> tuple[int, int, float, str]:
            p_list = broker.pending.get(k_o) or ()
            has_close = any(po.intent.kind in (OrderKind.EXIT, OrderKind.REDUCE) for po in p_list)
            best_pri = max(
                (float(po.meta.get("priority_score", 0.0)) for po in p_list if po.intent.kind is OrderKind.OPEN),
                default=-1e9,
            )
            return (0 if has_close else 1, -frames[k_o].tf_hours, -best_pri, k_o)

        ordered = sorted(opening, key=_open_rank)
    for key in ordered:
        st = states[key]
        o = st.frame.exec_bars[moment][1]
        for mic_only in (False, True):
            rows = broker.on_bar_open(key, moment, o, mic_only=mic_only)
            _apply_open_rows(st, broker, key, moment, rows, counters, claim_events, claim_created_index)


def run_campaigns(frames: Mapping[str, CampaignFrame], *, segment: str, dev_frac: float,
                  equity0: float, risk_frac: float, leverage_cap: float,
                  taker_fee: float, slippage_per_side: float,
                  funding_per_8h: float, cost_multiplier: float,
                  contracts: Mapping[str, ContractSpec], params: Params, n_fill: int,
                  portfolio_cap_frac: float = 0.03,
                  maker_fee: float | None = None,
                  router_params: RouterParams | None = None,
                  router_factory: type[Any] | None = None,
                  enable_mic: bool = False,
                  arm: ExecutionConfig | None = None,
                  router_params_by_tf: Mapping[str, RouterParams] | None = None,
                  tf_cap_by_tf: Mapping[str, float] | None = None,
                  dynamic_risk_tiers: Sequence[Sequence[float]] | None = None,
                  capacity_mode: str = capacity.ADMISSION_FULL_COST,
                  funding_rates: Mapping[str, Sequence[Sequence[float]]] | None = None,
                  slippage_by_symbol: Mapping[str, float] | None = None,
                  symbol_stop_risk_frac: Mapping[str, float] | None = None,
                  reserve_by_tf: Mapping[str, float] | None = None,
                  directional_cap_frac: float | None = None,
                  factor_burst_cap_1h: int = 0,
                  factor_burst_cap_4h: int = 0,
                  markowitz_corr_sizing: bool = False,
                  max_eff_size_mult: float | None = None,
                  ) -> BacktestResult:
    """Replay actual close/open times with portfolio-wide phase barriers."""
    if segment not in ("full", "dev", "test"):
        raise ValueError(f"unknown segment {segment}")
    if not (0.0 < portfolio_cap_frac <= 1.0):
        raise ValueError("portfolio_cap_frac must be in (0, 1]")

    keys = sorted(frames, key=lambda k: (frames[k].symbol, -frames[k].tf_hours, k))
    base_rp = router_params or RouterParams()
    router_cls = router_factory or MarketRouter
    seg_bounds, states = _init_campaign_states(
        frames, keys, segment=segment, dev_frac=dev_frac, params=params,
        base_rp=base_rp, router_cls=router_cls, router_params_by_tf=router_params_by_tf,
        arm=arm, taker_fee=taker_fee, slippage_per_side=slippage_per_side,
        slippage_by_symbol=slippage_by_symbol,
    )
    go_set = base_rp._normalized_whitelist
    clock = _ReplayClock()
    corr_size_fn = _build_corr_size_fn(frames, keys, clock) if markowitz_corr_sizing else None

    broker = Broker(
        equity=equity0, risk_frac=risk_frac, leverage_cap=leverage_cap,
        taker_fee=taker_fee, slippage_per_side=slippage_per_side,
        funding_per_8h=funding_per_8h,
        contracts={k: contracts[frames[k].symbol] for k in keys},
        maker_fee=maker_fee, cost_multiplier=cost_multiplier,
        portfolio_stop_risk_frac=portfolio_cap_frac,
        tf_stop_risk_frac_by_tf=tf_cap_by_tf,
        dynamic_risk_tiers=dynamic_risk_tiers,
        capacity_mode=capacity_mode,
        funding_rates=funding_rates,
        slippage_by_symbol=slippage_by_symbol,
        symbol_stop_risk_frac=symbol_stop_risk_frac,
        reserve_by_tf=reserve_by_tf,
        directional_stop_risk_frac=directional_cap_frac,
        factor_burst_cap_1h=factor_burst_cap_1h,
        factor_burst_cap_4h=factor_burst_cap_4h,
        corr_size_fn=corr_size_fn,
        max_eff_size_mult=max_eff_size_mult,
    )

    equity_rows: list[tuple[Any, ...]] = []
    cap_skips: list[dict[str, Any]] = []
    claim_events: list[dict[str, Any]] = []
    claim_created_index: dict[str, dict[str, Any]] = {}
    counters: dict[str, Any] = {
        "go_events": 0, "open_intents": 0, "opens_filled": 0,
        "min_size_skips": 0, "liquidations": 0,
        "limits_armed": 0, "limit_fills_entry": 0, "limit_expired": 0,
        "limit_min_size_skips": 0,
        "reduce_limits_armed": 0, "reduce_limit_fills": 0,
        "reduce_timeout_taker": 0, "resting_canceled": 0,
        "e1_priority_cancels": 0, "portfolio_cap_skips": 0,
        "protective_stop_fills": 0,
        "mic_go_events": 0,
    }

    exit_exec = bool(arm.exit_exec) if arm is not None else False
    exit_exec_trail = bool(arm.exit_exec_trail) if arm is not None else True
    pivot_exec = bool(arm.pivot_exec) if arm is not None else False
    require_exec_pivots(pivot_exec, (st.frame for st in states.values()))
    opens, closes, mic_closes = _build_execution_schedule(frames, keys, enable_mic=enable_mic)

    for moment in sorted(opens.keys() | closes.keys() | mic_closes.keys()):
        clock.now_ms = moment
        closing = closes.get(moment, ())
        _process_closing_moment(
            moment, closing, frames=frames, states=states, broker=broker,
            counters=counters, cap_skips=cap_skips, seg_bounds=seg_bounds,
            go_set=go_set, n_fill=n_fill, claim_events=claim_events,
            claim_created_index=claim_created_index,
            exit_exec=exit_exec, exit_exec_trail=exit_exec_trail, pivot_exec=pivot_exec,
        )
        _process_mic_batch(
            moment, mic_closes.get(moment, ()), frames=frames, states=states,
            broker=broker, counters=counters, cap_skips=cap_skips,
            seg_bounds=seg_bounds, go_set=go_set, n_fill=n_fill, claim_events=claim_events,
        )
        _process_opening_moment(
            moment, opens.get(moment, ()), frames=frames, states=states,
            broker=broker, counters=counters, claim_events=claim_events,
            claim_created_index=claim_created_index,
        )
        if closing:
            equity_rows.append((
                moment, broker.equity_mtm(), broker.cash,
                broker.unrealized(), broker.funding_accrued(), len(broker.positions),
            ))

    per_campaign: dict[str, dict[str, int]] = {}
    for key in keys:
        per_campaign[key] = dict(states[key].router.counters)
        for k, v in states[key].router.counters.items():
            counters[k] = counters.get(k, 0) + v
    counters["campaigns"] = per_campaign
    counters["entry_skips"] = dict(sorted(broker.n_skips.items()))

    attr_bounds = seg_bounds
    if enable_mic:
        attr_bounds = {}
        for key in keys:
            b = dict(seg_bounds[key])
            mic = frames[key].mic
            if b["ts_hi"] is not None and mic is not None:
                b = {**b, "ts_hi": b["ts_hi"] + frames[key].tf_hours * MS_1H - mic.trigger_tf_seconds * 1000}
            attr_bounds[key] = b

    trades = _reconstruct_trades(broker.ledger, attr_bounds)
    completed_by_trade: dict[str, int] = {}
    migrated_by_trade: dict[str, int] = {}
    for event in claim_events:
        tid = event.get("trade_id")
        if not tid:
            continue
        if event["event"] == "COMPLETED":
            completed_by_trade.setdefault(tid, event["ts"])
        elif event["event"] == "MIGRATED":
            migrated_by_trade.setdefault(tid, event["ts"])
    for trade in trades:
        trade["claim_completed_ts"] = completed_by_trade.get(trade["trade_id"])
        trade["claim_migrated_ts"] = migrated_by_trade.get(trade["trade_id"])
    annotate_excursions(trades, frames)
    res = BacktestResult(
        segment=segment, cost_multiplier=cost_multiplier,
        equity0=equity0, ledger=list(broker.ledger),
        equity_rows=equity_rows, trades=trades,
        counters=counters, seg_bounds=seg_bounds,
        contracts={s: vars(c) for s, c in contracts.items()},
        cap_skips=cap_skips,
        campaign_states={k: {"pos_mgr": states[k].pos_mgr, "router": states[k].router} for k in keys},
        claim_events=claim_events,
    )
    assert_fill_after_decision(res.ledger)
    return res


def _claim_id(meta: Mapping[str, Any]) -> tuple[tuple[Any, ...], str, int] | None:
    """账本 meta 里的主张身份 → 可哈希 tuple(box/uid, role, geometry_version)。"""
    raw = meta.get("claim_id")
    if not raw:
        return None
    box, role, version = raw
    return (tuple(box), str(role), int(version))


def _claim_state(pos: Position) -> tuple[int | None, int | None]:
    """持仓 claim 状态快照;仅用于检测状态机已发生的事实变化。"""
    return (getattr(pos, "claim_completed_ts", None),
            getattr(pos, "claim_migrated_ts", None))


def _record_claim_transitions(
    events: list[dict[str, Any]],
    key: str,
    broker: Broker,
    pos: Position,
    before: tuple[int | None, int | None],
    ts: int,
    *,
    source: str,
) -> None:
    """把 PositionManager 已完成的生命周期变化复制到只读事件流。"""
    _ = ts
    trade = broker.position_of(key)
    trade_id = trade.trade_id if trade is not None else None
    completed_before, migrated_before = before
    completed = getattr(pos, "claim_completed_ts", None)
    migrated = getattr(pos, "claim_migrated_ts", None)
    common = {"trade_id": trade_id, "campaign": key, "symbol": pos.symbol,
              "claim_id": pos.claim_id, "source": source}
    if completed_before is None and completed is not None:
        events.append({**common, "event": "COMPLETED", "ts": int(completed),
                       "target": pos.target_1})
    if migrated_before is None and migrated is not None:
        events.append({**common, "event": "MIGRATED", "ts": int(migrated),
                       "target": pos.target_1})


def _claim_event_for_trade(events: Sequence[dict[str, Any]], trade_id: str) -> dict[str, Any]:
    """返回该交易最早的 claim 身份事实,供退出成交关联。"""
    return next((event for event in events
                 if event.get("trade_id") == trade_id
                 and event.get("event") == "CREATED"), {})


def _record_claim_exit(
    events: list[dict[str, Any]],
    key: str,
    row: Mapping[str, Any],
    created_index: Mapping[str, dict[str, Any]] | None = None,
) -> None:
    if row.get("kind") not in ("EXIT", "LIQUIDATION") or not row.get("trade_id"):
        return
    tid = row["trade_id"]
    origin = (
        created_index.get(tid, {})
        if created_index is not None
        else _claim_event_for_trade(events, tid)
    )
    events.append({
        "trade_id": tid, "campaign": key,
        "symbol": origin.get("symbol"), "claim_id": origin.get("claim_id"),
        "box_id": origin.get("box_id"),
        "geometry_version": origin.get("geometry_version"),
        "event": "EXITED", "ts": int(row["ts"]), "price": row.get("price"),
        "reason_code": row.get("reason_code"),
    })


def _record_claim_open(
    events: list[dict[str, Any]],
    key: str,
    symbol: str,
    row: Mapping[str, Any],
    created_index: dict[str, dict[str, Any]] | None = None,
) -> None:
    """成交后用 trade_id 回填此前已发生的创建、确认和下单事实。"""
    m = row["meta"]
    common = {"trade_id": row["trade_id"], "campaign": key, "symbol": symbol,
              "claim_id": m.get("claim_id"), "box_id": m.get("box_id"),
              "geometry_version": m.get("geometry_version")}
    created_ev = {
        **common,
        "event": "CREATED",
        "ts": int(m.get("claim_created_ts", row["decision_ts"])),
        "price": m.get("claim_created_price"),
        "regime": m.get("claim_birth_regime"),
    }
    if created_index is not None and row["trade_id"] not in created_index:
        created_index[row["trade_id"]] = created_ev
    events.extend((
        created_ev,
        {**common, "event": "CONFIRMED",
         "ts": int(m.get("claim_confirmed_ts", row["decision_ts"])),
         "price": m.get("claim_confirmed_price"),
         "regime": m.get("claim_confirm_regime", m.get("regime"))},
        {**common, "event": "ORDERED", "ts": int(row["decision_ts"]),
         "price": m.get("limit_price", m.get("claim_confirmed_price"))},
        {**common, "event": "FILLED", "ts": int(row["ts"]),
         "price": row["price"]},
    ))


def _apply_open_rows(
    st: _CampaignState,
    broker: Broker,
    key: str,
    ts: int,
    rows: Sequence[dict[str, Any]],
    counters: dict[str, Any],
    claim_events: list[dict[str, Any]] | None = None,
    claim_created_index: dict[str, dict[str, Any]] | None = None,
) -> None:
    """on_bar_open 账本行的战役侧处理(canonical 与 mic 触发共用)。"""
    f = st.frame
    sym = f.symbol
    for row in rows:
        k = row["kind"]
        if k == "OPEN":
            m = row["meta"]
            d_open = int(m["direction"])
            re_px = m.get("zone_lo") if d_open > 0 else m.get("zone_hi")
            st.pos_mgr.open(
                sym, d_open, row["price"], float(m["invalid"]),
                execution_target(m, m.get("target_1")), str(m.get("event_id")),
                ts, _cog_index_of(f, ts), reenter_invalid_px=re_px, claim_id=_claim_id(m),
            )
            if claim_events is not None:
                _record_claim_open(claim_events, key, sym, row, claim_created_index)
            counters["opens_filled"] += 1
            if m.get("entry_mode") == "virtual_limit":
                _increment_counter(counters, "virtual_entry_fills")
        elif k == "ADD":
            _increment_counter(counters, "adds_filled")
        elif k == "REDUCE":
            if broker.position_of(key) is not None and sym in st.pos_mgr.positions:
                broker.update_protective_invalid(key, st.pos_mgr.positions[sym].invalid)
        elif k in ("EXIT", "LIQUIDATION"):
            if claim_events is not None:
                _record_claim_exit(claim_events, key, row, claim_created_index)
            st.pos_mgr.drop_position(sym)
            rows_cancel = broker.cancel_resting(
                key, ts, reason_code="POSITION_CLOSED",
                detail="position closed by EXIT/LIQUIDATION; resting limit canceled",
            )
            counters["resting_canceled"] += len(rows_cancel)
        elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
            counters["min_size_skips"] += 1


def _handle_resting_limit_fill(
    st: _CampaignState,
    broker: Broker,
    key: str,
    ts: int,
    row: Mapping[str, Any],
    counters: dict[str, Any],
    claim_events: list[dict[str, Any]] | None,
    claim_created_index: dict[str, dict[str, Any]] | None,
) -> None:
    f = st.frame
    sym = f.symbol
    if row["meta"].get("role") == "reduce":
        counters["reduce_limit_fills"] += 1
        if not (row["meta"].get("full_close") or broker.position_of(key) is None):
            return
        if claim_events is not None:
            _record_claim_exit(claim_events, key, {**row, "kind": "EXIT"}, claim_created_index)
        st.pos_mgr.drop_position(sym)
        rows_cancel = broker.cancel_resting(
            key, ts, reason_code="POSITION_CLOSED",
            detail="position fully closed by reduce LIMIT_FILL; resting limit canceled",
        )
        counters["resting_canceled"] += len(rows_cancel)
        return

    counters["limit_fills_entry"] += 1
    m = row["meta"]
    d_open = int(m["direction"])
    re_px = m.get("zone_lo") if d_open > 0 else m.get("zone_hi")
    st.pos_mgr.open(
        sym, d_open, row["price"], float(m["invalid"]),
        execution_target(m, m.get("target_1")), str(m.get("event_id")),
        ts, _cog_index_of(f, ts), reenter_invalid_px=re_px, claim_id=_claim_id(m),
    )
    if claim_events is not None:
        _record_claim_open(claim_events, key, sym, row, claim_created_index)


def _apply_resting_rows(
    st: _CampaignState,
    broker: Broker,
    key: str,
    ts: int,
    rows: Sequence[dict[str, Any]],
    counters: dict[str, Any],
    claim_events: list[dict[str, Any]] | None = None,
    claim_created_index: dict[str, dict[str, Any]] | None = None,
) -> None:
    """on_15m_bar 账本行的战役侧处理(canonical 与 mic 触发共用)。"""
    for row in rows:
        k = row["kind"]
        if k == "LIMIT_FILL":
            _handle_resting_limit_fill(st, broker, key, ts, row, counters, claim_events, claim_created_index)
        elif k == "EXPIRED":
            counters["limit_expired"] += 1
        elif k == "TRIGGER" and row["reason_code"] == "VIRTUAL_TRIGGER":
            _increment_counter(counters, "virtual_entry_triggers")
        elif k == "CANCEL" and row["reason_code"] == "TIMEOUT_TO_TAKER":
            counters["reduce_timeout_taker"] += 1
        elif k == "CANCEL" and row.get("reason_code") == "MOVE_ALREADY_PLAYED_OUT":
            _increment_counter(counters, "watch_canceled_played_out")
        elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
            counters["min_size_skips"] += 1
            counters["limit_min_size_skips"] += 1


def _tf_hours_of_key(key: str) -> int:
    tf = capacity.timeframe_of(key)
    return int(TF_HOURS.get(tf, 0)) if tf else 0


def _symbol_position_open(broker: Any, key: str, *, allow_add: bool = False) -> bool:
    """多时间尺度仓位升格与金字塔加仓协调。"""
    my_h = _tf_hours_of_key(key)
    if my_h <= 0:
        fn = getattr(broker, "symbol_has_position", None)
        return bool(fn(key)) if fn is not None else (broker.position_of(key) is not None)
    root = capacity.symbol_root(key)
    positions = getattr(broker, "positions", {})
    for k_open in positions:
        if capacity.symbol_root(k_open) != root:
            continue
        open_h = _tf_hours_of_key(k_open)
        if open_h > my_h:
            return True
        if open_h == my_h:
            if allow_add and k_open == key:
                pos_self = broker.position_of(key)
                pend = getattr(broker, "pending", {}) or {}
                busy = any(po.intent.kind in (OrderKind.ADD, OrderKind.EXIT) for po in pend.get(key, ()))
                if pos_self is not None and int(getattr(pos_self, "adds_done", 0)) < 1 and not busy:
                    continue
            return True
    return False


def _symbol_entry_resting(broker: Any, key: str) -> bool:
    my_h = _tf_hours_of_key(key)
    if my_h <= 0:
        fn = getattr(broker, "symbol_has_entry_order", None)
        return bool(fn(key)) if fn is not None else bool(broker.has_entry_limit(key))
    root = capacity.symbol_root(key)
    for k_p, orders in getattr(broker, "pending", {}).items():
        if capacity.symbol_root(k_p) == root and _tf_hours_of_key(k_p) >= my_h:
            if any(po.intent.kind is OrderKind.OPEN for po in orders):
                return True
    for k_r, orders in getattr(broker, "resting", {}).items():
        if capacity.symbol_root(k_r) == root and _tf_hours_of_key(k_r) >= my_h:
            if any(od.role == "entry" for od in orders):
                return True
    return False


def _bar_log_return_at(
    bars: Sequence[tuple[int, float, float, float, float, float]], idx: int, lookback: int
) -> float | None:
    if idx < lookback:
        return None
    c_now = float(bars[idx][4])
    c_prev = float(bars[idx - lookback][4])
    if c_now <= 0.0 or c_prev <= 0.0:
        return None
    return math.log(c_now / c_prev)


def _portfolio_breadth_at(
    st: _CampaignState,
    states: Mapping[str, _CampaignState] | None,
    close_moment_ms: int,
    contracts: Mapping[str, Any] | None = None,
) -> tuple[float, int, float, float, float]:
    """在 close_moment_ms 计算同周期、同资产类别标的因果可见的横截面市场宽度与相对走势超额收益。"""
    if not states:
        return 0.0, 1, 0.0, 0.0, 0.0
    tf_h = st.frame.tf_hours
    max_open_ts = int(close_moment_ms) - tf_h * MS_1H
    my_ac = capacity.asset_class_of(st.frame.key, contracts) if contracts else None
    w_canvas = int(getattr(getattr(st.pos_mgr, "p", None), "canvas_bars", 160) or 160)
    lb48, lb120 = peer_lookback_bars(tf_h, canvas_bars=w_canvas)
    regs: list[Any] = []
    p_r48: list[float] = []
    p_r120: list[float] = []
    own_r48: float | None = None
    own_r120: float | None = None

    for other in states.values():
        of = other.frame
        if of.tf_hours != tf_h:
            continue
        if my_ac is not None and capacity.asset_class_of(of.key, contracts) != my_ac:
            continue
        idx = bisect_right(of.cog_opens, max_open_ts) - 1
        if idx < 0 or idx >= len(of.cog.regimes):
            continue
        regs.append(of.cog.regimes[idx])
        r48 = _bar_log_return_at(of.cog.bars, idx, lb48)
        if r48 is not None:
            p_r48.append(r48)
            if other is st:
                own_r48 = r48
        r120 = _bar_log_return_at(of.cog.bars, idx, lb120)
        if r120 is not None:
            p_r120.append(r120)
            if other is st:
                own_r120 = r120

    ex48, ex120 = compute_peer_excess_returns(own_r48, own_r120, p_r48, p_r120)
    if len(regs) < 4:
        return 0.0, len(regs), 0.0, ex48, ex120
    up = sum(1 for r in regs if r.value == "trend_up")
    dn = sum(1 for r in regs if r.value == "trend_down")
    return float(up - dn) / float(len(regs)), len(regs), 0.0, ex48, ex120


def _htf_views_for(
    st: _CampaignState, states: Mapping[str, _CampaignState] | None, close_moment_ms: int
) -> tuple[StructureView, ...]:
    """获取同标的更高周期(htf_hours >= 2 * tf_hours)在 close_moment_ms 前最新已收盘结构视图。"""
    if not states:
        return ()
    sym = st.frame.symbol
    tf_h = st.frame.tf_hours
    out: list[StructureView] = []
    for other in states.values():
        hf = other.frame
        if hf.symbol != sym or hf.tf_hours < 2 * tf_h:
            continue
        max_open_ts = int(close_moment_ms) - hf.tf_hours * MS_1H
        idx = bisect_right(hf.cog_opens, max_open_ts) - 1
        if idx < 0:
            continue
        vw = hf.cog.views.get(idx)
        act_zones = (
            hf.cog.active_zones[idx]
            if idx < len(getattr(hf.cog, "active_zones", ()))
            else (vw.zones if vw is not None else ())
        )
        m_imb = (
            int(hf.cog.macro_imbalances[idx])
            if idx < len(getattr(hf.cog, "macro_imbalances", ()))
            else 0
        )
        imb_str = (
            float(hf.cog.imbalance_strengths[idx])
            if idx < len(getattr(hf.cog, "imbalance_strengths", ()))
            else (vw.imbalance_strength if vw is not None else 0.0)
        )
        out.append(StructureView(
            regime=(vw.regime if vw is not None else hf.cog.regimes[idx]),
            zones=(vw.zones if vw is not None and vw.zones else act_zones),
            atr=(vw.atr if vw is not None else hf.cog.atrs[idx]),
            pivots=(vw.pivots if vw is not None else ()),
            macro_imbalance=m_imb,
            tf_hours=hf.tf_hours,
            imbalance_strength=imb_str,
        ))
    return tuple(out)


def _mic_trigger_step(st: _CampaignState, broker: Broker, a: int, ts: int,
                      counters: dict[str, Any], cap_skips: list[dict[str, Any]], bounds: Mapping[str, Any],
                      go_set: frozenset[tuple[str, int]], n_fill: int, claim_events: list[dict[str, Any]] | None = None,
                      *, execution_only: bool = False,
                      states: Mapping[str, _CampaignState] | None = None) -> None:
    """trigger bar 收盘推进:触发级联防守 + 路由入场决策。"""
    f = st.frame
    key, sym = f.key, f.symbol
    mic = f.mic
    _ts, _o, _h, _l, c, _v = mic.bars[a]
    if not execution_only:
        _apply_open_rows(st, broker, key, ts, broker.on_bar_open(key, ts, _o, mic_only=True), counters, claim_events)
        _apply_resting_rows(st, broker, key, ts, broker.on_15m_bar(key, ts, _o, _h, _l, c, mic_only=True), counters, claim_events)

    pos_mgr = getattr(st, "pos_mgr", None)
    new_events = mic.events_by.get(a)
    if pos_mgr is not None and broker.position_of(key) is not None:
        pend = getattr(broker, "pending", None) or {}
        if not any(po.intent.kind is OrderKind.EXIT for po in pend.get(key, ())):
            trig_mgmt = pos_mgr.on_trigger_bar(sym, mic.bars[a], new_events or (), tf_label=st.frame.tf_label)
            if trig_mgmt:
                close_m = int(_ts) + mic.trigger_tf_seconds * 1000
                _apply_mgmt(
                    st, broker, trig_mgmt, int(_ts) if execution_only else ts,
                    counters, n_fill, known_at=close_m if execution_only else None,
                )

    i = mic.cog_index_of[a]
    lo, hi = bounds["lo"], bounds["hi"]
    if not (lo <= i < hi) or not new_events:
        return
    _count_go(counters, new_events, go_set, mic=True)
    groups: dict[tuple[tuple[Any, ...], int], list[Any]] = {}
    for event in new_events:
        routing_bar = tuple(event.attrs.get("routing_bar", mic.bars[a]))
        routing_index = int(event.attrs.get("routing_index", a))
        groups.setdefault((routing_bar, routing_index), []).append(event)

    close_moment = int(_ts) + mic.trigger_tf_seconds * 1000
    htf_views = _htf_views_for(st, states, close_moment)
    b_contracts = getattr(broker, "contracts", None) or {}
    m_breadth, n_univ, med_dv, ex48, ex120 = _portfolio_breadth_at(st, states, close_moment, b_contracts)
    decisions: list[RouteDecision] = []
    is_rwa = capacity.asset_class_of(key, b_contracts) == "rwa"
    allow_add = bool(getattr(pos_mgr, "allow_add", False))
    for (routing_bar, routing_index), events in groups.items():
        if rwa_session_entry_block(
            is_rwa, close_moment, routing_bar, symbol=key,
            history=mic.bars[max(0, a - _RWA_HISTORY_WINDOW_BARS):a],
        ) is not None:
            counters["rwa_session_skips"] = counters.get("rwa_session_skips", 0) + 1
            continue
        decisions.extend(st.router.on_bar(
            sym, routing_bar, routing_index, mic.views.get(a), mic.atrs[a], events,
            position_open=_symbol_position_open(broker, key, allow_add=allow_add),
            entry_resting=_symbol_entry_resting(broker, key),
            htf_views=htf_views,
            market_breadth=m_breadth,
            universe_size=n_univ,
            median_dollar_volume=med_dv,
            excess_ret_48h=ex48,
            excess_ret_120h=ex120,
        ))
    fill_after = (ts - (ts % MS_1H)) - MS_1H
    if execution_only:
        close_ts = int(_ts) + mic.trigger_tf_seconds * 1000
        _submit_decisions(
            st, broker, decisions, int(_ts), ts, close_ts,
            {"mic": True, "cog_index": i, "execution_step_ms": f.ms_exec, "execution_not_before": close_ts},
            counters, cap_skips, n_fill, cancel_moment=close_ts,
        )
        return
    _submit_decisions(
        st, broker, decisions, ts, fill_after, ts,
        {"mic": True, "cog_index": i}, counters, cap_skips,
        n_fill, cancel_moment=ts + mic.trigger_tf_seconds * 1000,
    )


def _cog_index_of(frame: CampaignFrame, exec_ts: int) -> int:
    """执行细 bar 落回认知 bar 索引(成交 bar ∈ 认知区间断言)。"""
    ms_tf = frame.tf_hours * MS_1H
    bucket = exec_ts - (exec_ts % ms_tf)
    opens: tuple[int, ...] | None = getattr(frame, "cog_opens", None)
    if opens is not None:
        index = bisect_left(opens, bucket)
        if index < len(opens) and opens[index] == bucket:
            return index
        raise AssertionError(f"{frame.key} exec ts {exec_ts} outside cognition")
    bars = frame.cog.bars
    index = bisect_left(bars, bucket, key=lambda b: b[0])
    if index < len(bars) and bars[index][0] == bucket:
        return index
    raise AssertionError(f"{frame.key} exec ts {exec_ts} outside cognition")


def _rekey(intent: OrderIntent, key: str) -> OrderIntent:
    """OrderIntent 的 symbol 改为战役复合键(broker 以战役键记账)。"""
    return OrderIntent(kind=intent.kind, symbol=key,
                       qty_frac=intent.qty_frac, reason=intent.reason,
                       ref_price=intent.ref_price, ts=intent.ts)


def _apply_mgmt(st: _CampaignState, broker: Broker, mgmt: Sequence[OrderIntent], t: int,
                counters: dict[str, Any], n_fill: int, *, known_at: int | None = None) -> None:
    """Submit management intents identically for both execution granularities."""
    key = st.frame.key
    for intent in mgmt:
        intent = _rekey(intent, key)
        armed_r = (
            intent.kind is OrderKind.REDUCE
            and _arm_reduce_limit(broker, st, intent, t, n_fill, known_at=known_at)
        )
        if armed_r:
            counters["reduce_limits_armed"] += 1
        else:
            broker.submit(
                intent, decision_ts=t,
                meta={"execution_not_before": known_at} if known_at is not None else None,
            )


def _cancel_structurally_dead_entries(
    st: _CampaignState,
    broker: Broker,
    key: str,
    bar: Sequence[float],
    atr: float,
    counters: dict[str, Any],
) -> None:
    """挂单结构性失效撤单(带被破 / 目标已过即撤,n_fill 墙钟过期保留兜底)。"""
    orders = [o for o in broker.resting.get(key, ()) if o.role == "entry"]
    if not orders:
        return
    params = getattr(st.pos_mgr, "p", None)
    k_bm = float(getattr(params, "k_bm", 0.1) or 0.1)
    bm = k_bm * float(atr)
    ts, _o, _h, _l, c, _v = bar
    reasons: list[str] = []
    for o in orders:
        meta = getattr(o, "meta", None) or {}
        d = int(meta.get("direction") or 0)
        if d not in (1, -1):
            continue
        lo, hi = meta.get("zone_lo"), meta.get("zone_hi")
        why = None
        if lo is not None and hi is not None:
            if d > 0 and c < float(lo) - bm:
                why = f"zone_broken(close={c:.8g} < {float(lo) - bm:.8g})"
            elif d < 0 and c > float(hi) + bm:
                why = f"zone_broken(close={c:.8g} > {float(hi) + bm:.8g})"
        if why is None and getattr(o, "target_1", None) is not None:
            t_px = float(o.target_1)
            if d > 0 and c > t_px:
                why = f"target_passed(close={c:.8g} > {t_px:.8g})"
            elif d < 0 and c < t_px:
                why = f"target_passed(close={c:.8g} < {t_px:.8g})"
        if why is not None:
            reasons.append(why)
    if not reasons or len(reasons) != len(orders):
        return
    cancel_ts = int(ts) + int(st.frame.tf_hours * MS_1H)
    rows = broker.cancel_resting(key, cancel_ts, reason_code="STRUCTURE_INVALID", detail=" | ".join(reasons)[:200])
    counters["struct_canceled"] = counters.get("struct_canceled", 0) + len(rows)


def _cognition_step(st: _CampaignState, broker: Broker, i: int,
                    counters: dict[str, Any], cap_skips: list[dict[str, Any]], bounds: Mapping[str, Any],
                    go_set: frozenset[tuple[str, int]], n_fill: int, claim_events: list[dict[str, Any]],
                    *, states: Mapping[str, _CampaignState] | None = None) -> None:
    """认知 bar i 收盘:持仓管理 → 路由(段内才启用 OPEN)。"""
    f = st.frame
    key, sym = f.key, f.symbol
    cog = f.cog
    bar = cog.bars[i]
    t = int(bar[0])
    new_events = cog.events_by.get(i, [])
    new_pivots = cog.pivots_by.get(i, [])
    atr = cog.atrs[i]
    st.last_atr = atr

    _cancel_structurally_dead_entries(st, broker, key, bar, atr, counters)

    pos_pm = st.pos_mgr.positions.get(sym)
    if pos_pm is not None and pos_pm.opened_ts > bar[0]:
        sub_labels = [ts_sub for ts_sub in f.exec_index_of.get(i, ()) if int(ts_sub) >= int(pos_pm.opened_ts)]
        if sub_labels:
            sub_bars = [f.exec_bars[ts_sub] for ts_sub in sub_labels]
            sub_bar = (
                bar[0], sub_bars[0][1],
                max(b[2] for b in sub_bars), min(b[3] for b in sub_bars),
                sub_bars[-1][4], sum(b[5] for b in sub_bars),
            )
            before = _claim_state(pos_pm)
            mgmt = st.pos_mgr.on_bar(sym, sub_bar, atr, (), (), price_only=True)
            _record_claim_transitions(claim_events, key, broker, pos_pm, before, t, source="cognition")
        else:
            mgmt = []
    else:
        before = _claim_state(pos_pm) if pos_pm is not None else None
        mgmt = st.pos_mgr.on_bar(sym, bar, atr, new_pivots, new_events)
        if pos_pm is not None and before is not None:
            _record_claim_transitions(claim_events, key, broker, pos_pm, before, t, source="cognition")
    if sym in st.pos_mgr.positions and broker.position_of(key) is not None:
        broker.update_protective_invalid(key, st.pos_mgr.positions[sym].invalid)
    _apply_mgmt(st, broker, mgmt, t, counters, n_fill, known_at=t + f.tf_hours * MS_1H)

    lo, hi = bounds["lo"], bounds["hi"]
    if not (lo <= i < hi):
        return
    route_events = select_cognition_route_events(
        new_events, has_trigger_tracker=(f.mic is not None or f.tf_hours == 1)
    )
    if not route_events:
        return
    _count_go(counters, route_events, go_set)
    close_moment_cog = t + f.tf_hours * MS_1H
    b_contracts_cog = getattr(broker, "contracts", None) or {}
    if rwa_session_entry_block(
        capacity.asset_class_of(key, b_contracts_cog) == "rwa",
        close_moment_cog, bar, symbol=key,
        history=cog.bars[max(0, i - _RWA_HISTORY_WINDOW_BARS):i],
    ) is not None:
        counters["rwa_session_skips"] = counters.get("rwa_session_skips", 0) + 1
        return
    allow_add = bool(getattr(st.pos_mgr, "allow_add", False))
    htf_views = _htf_views_for(st, states, close_moment_cog)
    m_breadth_cog, n_univ_cog, med_dv_cog, ex48_cog, ex120_cog = _portfolio_breadth_at(
        st, states, close_moment_cog, b_contracts_cog
    )
    extra_kw: dict[str, Any] = {"htf_views": htf_views} if htf_views else {}
    decisions = st.router.on_bar(
        sym, bar, i, cog.views.get(i), atr, route_events,
        position_open=_symbol_position_open(broker, key, allow_add=allow_add),
        entry_resting=_symbol_entry_resting(broker, key),
        market_breadth=m_breadth_cog,
        universe_size=n_univ_cog,
        median_dollar_volume=med_dv_cog,
        excess_ret_48h=ex48_cog,
        excess_ret_120h=ex120_cog,
        **extra_kw,
    )
    fill_after = t + f.tf_hours * MS_1H - f.ms_exec
    extra = {"execution_step_ms": f.ms_exec, "execution_not_before": close_moment_cog}
    _submit_decisions(
        st, broker, decisions, t, fill_after, close_moment_cog, extra,
        counters, cap_skips, n_fill, cancel_moment=close_moment_cog,
    )


def _handle_cancel_entry(
    broker: Broker, key: str, d: RouteDecision, cancel_moment: int, counters: dict[str, Any]
) -> None:
    for od in broker.resting.get(key, ()):
        if od.armed_at_ts >= cancel_moment:
            raise StrategyExecutionError(
                f"BREAKOUT_ACCEPTED cancel moment {cancel_moment} <= armed {od.armed_at_ts} ({key})"
            )
    rows_cancel = broker.cancel_resting(
        key, cancel_moment, reason_code="E1_PRIORITY", detail=str(d.meta.get("reason"))
    )
    counters["e1_priority_cancels"] += len(rows_cancel)


def _try_submit_pyramid_add(
    st: _CampaignState,
    broker: Broker,
    key: str,
    d: RouteDecision,
    pos_existing: Any,
    t: int,
    extra_meta: Mapping[str, Any],
    counters: dict[str, Any],
) -> None:
    dr = int(pos_existing.direction)
    ref_px = float(d.intent.ref_price)
    sig_inv = float(d.hyp.invalid)
    if (
        d.kind != "market"
        or int(getattr(pos_existing, "adds_done", 0)) >= 1
        or int(d.hyp.direction) != dr
        or pos_existing.invalid is None
        or dr * (ref_px - pos_existing.entry) <= 0.0
        or dr * (sig_inv - pos_existing.entry) < 0.0
        or dr * (ref_px - pos_existing.invalid) <= 0.0
    ):
        counters["pyramid_gate_skips"] = counters.get("pyramid_gate_skips", 0) + 1
        return
    equity = broker.equity_mtm()
    capital = broker.risk_capital()
    tq = min(
        0.5 * pos_existing.open_qty,
        broker.qty_for_entry(
            capital, ref_px, pos_existing.invalid, key,
            size_mult=0.5, notional_equity=equity, direction=dr,
        ),
    )
    spec = broker.contracts[key]
    qs = floor_step(tq, spec.size_step, spec.size_place)
    if (
        qs < spec.size_min
        or qs * ref_px < spec.min_trade_usdt
        or broker.directional_blocked(dr, ref_px, pos_existing.invalid, key, moment=capacity.DECISION, qty=qs)
    ):
        counters["pyramid_cap_skips"] = counters.get("pyramid_cap_skips", 0) + 1
        return
    add_intent = OrderIntent(
        kind=OrderKind.ADD, symbol=key, qty_frac=qs / pos_existing.open_qty,
        reason=f"PYRAMID_ADD|{d.hyp.event_id}", ref_price=ref_px, ts=int(t),
    )
    pm_pos = st.pos_mgr.positions.get(st.frame.symbol) if getattr(st, "pos_mgr", None) is not None else None
    broker.submit(
        add_intent, decision_ts=int(t), direction=dr, invalid=pos_existing.invalid,
        target_1=(pm_pos.target_1 if pm_pos is not None else d.hyp.target_1),
        event_id=d.hyp.event_id, meta={**(extra_meta or {}), "role": "pyramid_add"},
    )
    _increment_counter(counters, "pyramid_add_intents")


def _try_scale_to_headroom(
    broker: Broker,
    key: str,
    d: RouteDecision,
    *,
    capital: float,
    equity: float,
    direction: int,
    base_size_mult: float,
    risk_per_unit: float,
    include_costs: bool,
    delta: float,
    eff_headroom: float,
) -> tuple[bool, float, float]:
    """Try scaling entry size to fit remaining headroom. Returns ``(ok, size_mult, new_delta)``."""
    scale = (eff_headroom * _CAPACITY_SCALE_MARGIN) / delta if delta > 0.0 else 0.0
    if scale < _MIN_CAPACITY_SCALE_FRAC:
        return False, base_size_mult, delta
    new_mult = base_size_mult * scale
    qty_scaled = broker.qty_for_entry(
        capital, d.intent.ref_price, d.hyp.invalid, key,
        use_budget=False, size_mult=new_mult, notional_equity=equity, direction=direction,
    )
    spec = broker.contracts.get(key)
    min_notional = spec.min_trade_usdt if spec is not None else 5.0
    if qty_scaled <= 0.0 or qty_scaled * d.intent.ref_price < min_notional:
        return False, base_size_mult, delta
    new_delta = capacity.delta_exposure(
        qty_scaled, d.intent.ref_price, risk_per_unit,
        taker_fee=broker.taker_fee, slippage=broker.slippage, include_costs=include_costs,
    )
    return True, new_mult, new_delta


def _evaluate_capacity_and_scale(
    broker: Broker,
    key: str,
    d: RouteDecision,
    promo_low_positions: Mapping[str, Any],
) -> tuple[bool, str, float, float, float, float, float, float]:
    """Evaluate portfolio/TF/claim/directional capacity without mutating ``d.meta``.

    Returns ``(cap_blocked, skip_reason, eff_size_mult, cap_frac, used, delta, equity, capital)``.
    """
    equity = broker.equity_mtm()
    capital = broker.risk_capital()
    risk_per_unit = abs(d.intent.ref_price - d.hyp.invalid)
    direction = int(d.hyp.direction)
    base_size_mult = float(d.meta.get("size_mult", 1.0) or 1.0)
    eff_size_mult = base_size_mult

    for k_low in promo_low_positions:
        broker.positions.pop(k_low, None)
    try:
        opposite = broker.opposite_claim_open(key, direction)
        qty_full = (
            0.0 if opposite is not None else broker.qty_for_entry(
                capital, d.intent.ref_price, d.hyp.invalid, key,
                use_budget=False, size_mult=base_size_mult, notional_equity=equity, direction=direction,
            )
        )
        cap_frac, used, headroom, pool = broker.capacity_usage(capital, key, moment=capacity.DECISION)
        include_costs = broker.risk_budget.includes_costs(capacity.DECISION)
        delta = capacity.delta_exposure(
            qty_full, d.intent.ref_price, risk_per_unit,
            taker_fee=broker.taker_fee, slippage=broker.slippage, include_costs=include_costs,
        )
        claim, claim_pool, _cf = broker.claim_headroom(key, direction, capital, moment=capacity.DECISION)

        cap_blocked = False
        skip_reason = "PORTFOLIO_CAP"
        if opposite is not None:
            cap_blocked, skip_reason = True, "OPPOSITE_CLAIM"
        elif broker.risk_budget.is_active and qty_full <= 0.0:
            cap_blocked = True
            skip_reason = "TF_CAP" if pool == "tf" else "PORTFOLIO_CAP"
        elif broker.risk_budget.is_active:
            eff_headroom = min(headroom, max(0.0, claim)) if math.isfinite(claim) else headroom
            if delta > eff_headroom + 1e-9:
                ok_scale, eff_size_mult, delta = _try_scale_to_headroom(
                    broker, key, d,
                    capital=capital, equity=equity, direction=direction,
                    base_size_mult=base_size_mult, risk_per_unit=risk_per_unit,
                    include_costs=include_costs, delta=delta, eff_headroom=eff_headroom,
                )
                if not ok_scale:
                    cap_blocked = True
                    skip_reason = (
                        ("TF_CAP" if pool == "tf" else "PORTFOLIO_CAP")
                        if used + delta > cap_frac * capital + 1e-9
                        else "CLAIM_CAP"
                    )

        dir_cap = broker.risk_budget.directional_cap_frac
        if not cap_blocked and dir_cap is not None and dir_cap > 0.0:
            dir_used = broker.directional_usage(
                direction, moment=capacity.DECISION, capital=capital, notional_equity=equity, symbol=key
            )
            dir_price = d.intent.ref_price if d.kind == "market" else d.limit_price
            dir_qty = broker.qty_for_entry(
                capital, dir_price, d.hyp.invalid, key, use_budget=False,
                size_mult=eff_size_mult, notional_equity=equity, direction=direction,
            )
            dir_delta = capacity.delta_exposure(
                dir_qty, dir_price, abs(dir_price - d.hyp.invalid),
                taker_fee=broker.taker_fee, slippage=broker.slippage, include_costs=include_costs,
            )
            if dir_used + dir_delta > dir_cap * capital + 1e-9:
                cap_blocked = True
                skip_reason = "DIRECTIONAL_CAP"
                cap_frac, used, delta = dir_cap, dir_used, dir_delta
    finally:
        broker.positions.update(promo_low_positions)

    return cap_blocked, skip_reason, eff_size_mult, cap_frac, used, delta, equity, capital


def _submit_scale_promotions(
    broker: Broker,
    key: str,
    root_sym: str | None,
    my_h: int,
    promo_low_positions: Mapping[str, Any],
    t: int,
    extra_meta: Mapping[str, Any],
    counters: dict[str, Any],
) -> None:
    if my_h <= 0:
        return
    exec_nb = extra_meta.get("execution_not_before") if extra_meta else None
    for k_low, pos_low in promo_low_positions.items():
        if any(po.intent.kind is OrderKind.EXIT for po in broker.pending.get(k_low, ())):
            continue
        promo_exit = OrderIntent(
            kind=OrderKind.EXIT, symbol=k_low, qty_frac=1.0,
            reason=f"SCALE_PROMOTED|to={key}",
            ref_price=float(broker.last_mark.get(k_low, pos_low.entry)),
            ts=int(t),
        )
        broker.submit(
            promo_exit, decision_ts=int(t),
            meta={"execution_not_before": exec_nb} if exec_nb is not None else None,
        )
        counters["scale_promotions"] = counters.get("scale_promotions", 0) + 1
    for k_pend in list(broker.pending):
        if capacity.symbol_root(k_pend) == root_sym and 0 < _tf_hours_of_key(k_pend) < my_h:
            broker.pending[k_pend] = [po for po in broker.pending[k_pend] if po.intent.kind is not OrderKind.OPEN]
            if not broker.pending[k_pend]:
                broker.pending.pop(k_pend, None)


def _build_lifecycle_meta(
    st: _CampaignState, d: RouteDecision, extra_meta: Mapping[str, Any]
) -> dict[str, Any]:
    origin = d.meta.get("zone_origin_known")
    if origin is not None and 0 <= int(origin) < len(st.frame.cog.bars):
        origin_i = int(origin)
        birth_bar = st.frame.cog.bars[origin_i]
        birth_meta = {
            "claim_created_ts": int(birth_bar[0]),
            "claim_created_price": float(birth_bar[4]),
            "claim_birth_regime": st.frame.cog.regimes[origin_i].value,
        }
    else:
        birth_meta = {
            "claim_created_ts": int(d.hyp.ts),
            "claim_created_price": d.intent.ref_price,
            "claim_birth_regime": d.hyp.regime.value,
        }
    lifecycle_meta: dict[str, Any] = {
        **birth_meta,
        "claim_confirmed_ts": int(d.hyp.ts),
        "claim_confirmed_price": d.intent.ref_price,
        "claim_confirm_regime": d.hyp.regime.value,
    }
    evidence_index = d.meta.get("evidence_started_index")
    if evidence_index is not None:
        ev_idx = int(evidence_index)
        evidence_bars = (
            st.frame.mic.bars
            if extra_meta.get("mic") and st.frame.mic is not None
            else st.frame.cog.bars
        )
        if 0 <= ev_idx < len(evidence_bars):
            evidence_bar = evidence_bars[ev_idx]
            lifecycle_meta.update({
                "claim_evidence_started_ts": int(evidence_bar[0]),
                "claim_evidence_started_price": float(
                    d.meta.get("event_extreme")
                    if d.meta.get("event_extreme") is not None
                    else evidence_bar[4]
                ),
            })
    return lifecycle_meta


def _submit_decisions(st: _CampaignState, broker: Broker, decisions: Sequence[RouteDecision],
                      t: int, fill_after: int, expiry_base: int,
                      extra_meta: Mapping[str, Any], counters: dict[str, Any], cap_skips: list[dict[str, Any]],
                      n_fill: int, cancel_moment: int) -> None:
    """路由决策 → 组合容量闸 → broker 提交(认知收盘与触发共用)。"""
    key = st.frame.key
    for d in decisions:
        if d.kind == "cancel_entry":
            _handle_cancel_entry(broker, key, d, cancel_moment, counters)
            continue
        if d.kind not in ("market", "limit"):
            raise StrategyExecutionError(f"unknown route decision {d.kind}")

        allow_add = bool(getattr(getattr(st, "pos_mgr", None), "allow_add", False))
        if _symbol_position_open(broker, key, allow_add=allow_add) or _symbol_entry_resting(broker, key):
            counters["symbol_slot_skips"] = counters.get("symbol_slot_skips", 0) + 1
            continue

        pos_existing = broker.position_of(key) if allow_add and hasattr(broker, "position_of") else None
        if pos_existing is not None:
            _try_submit_pyramid_add(st, broker, key, d, pos_existing, t, extra_meta, counters)
            continue

        my_h = int(getattr(st.frame, "tf_hours", 0) or _tf_hours_of_key(key))
        root_sym = capacity.symbol_root(key)
        promo_low_positions = (
            {
                k_low: pos_low
                for k_low, pos_low in list(broker.positions.items())
                if capacity.symbol_root(k_low) == root_sym and 0 < _tf_hours_of_key(k_low) < my_h
            }
            if my_h > 0
            else {}
        )

        cap_blocked, skip_reason, eff_size_mult, cap_frac, used, delta, equity, capital = (
            _evaluate_capacity_and_scale(broker, key, d, promo_low_positions)
        )
        if cap_blocked:
            counters["portfolio_cap_skips"] += 1
            cap_skips.append({
                "ts": t, "campaign": key, "event_id": d.hyp.event_id,
                "direction": d.hyp.direction, "route": d.route,
                "reason_code": skip_reason,
                "worst_case": used + delta, "budget_frac": cap_frac,
                "equity": equity, "risk_capital": capital,
            })
            continue

        _submit_scale_promotions(broker, key, root_sym, my_h, promo_low_positions, t, extra_meta, counters)
        counters["open_intents"] += 1
        lifecycle_meta = _build_lifecycle_meta(st, d, extra_meta)
        decision_meta = dict(d.meta)
        if "size_mult" in decision_meta or eff_size_mult != 1.0:
            decision_meta["size_mult"] = eff_size_mult

        submit_meta = {
            "direction": int(d.hyp.direction),
            "regime": d.hyp.regime.value,
            "r_mult": d.hyp.r_mult,
            "route": d.route,
            **lifecycle_meta,
            **decision_meta,
            **extra_meta,
        }
        if d.kind == "market":
            broker.submit(
                _rekey(d.intent, key), decision_ts=t,
                direction=d.hyp.direction, invalid=d.hyp.invalid,
                target_1=d.hyp.target_1, event_id=d.hyp.event_id, meta=submit_meta,
            )
            continue

        wait_h = int(d.meta.get("max_wait_hours") or n_fill) if d.meta.get("watch_entry") else int(n_fill)
        broker.submit_limit(RestingLimitOrder(
            direction=int(d.hyp.direction),
            limit_price=float(d.limit_price),
            armed_at_ts=t,
            expiry_ts=expiry_base + wait_h * MS_1H,
            fill_after_ts=fill_after,
            symbol=key, role="entry",
            invalid=float(d.hyp.invalid),
            target_1=d.hyp.target_1,
            event_id=d.hyp.event_id, reason=d.intent.reason,
            meta=submit_meta,
        ))
        counters["limits_armed"] += 1
        if d.meta.get("watch_entry"):
            _increment_counter(counters, "watch_entries_armed")


def _arm_reduce_limit(broker: Broker, st: _CampaignState, intent: OrderIntent,
                      decision_ts: int, n_fill: int, *,
                      known_at: int | None = None) -> bool:
    """状态型目标减仓(PARTIAL_TARGET / partial_exit)与实盘一致统一走市价减仓,不挂可能悬空的限价单。"""
    if intent.reason and ("partial_exit" in intent.reason or "PARTIAL_TARGET" in intent.reason):
        return False
    f = st.frame
    key, sym = f.key, f.symbol
    pos_pm = st.pos_mgr.positions.get(sym)
    pos_acct = broker.position_of(key)
    target = pos_pm.target_1 if pos_pm is not None else None
    if pos_acct is None or target is None:
        return False
    spec = broker.contracts[key]
    qty = floor_step(float(intent.qty_frac or 0.0) * pos_acct.open_qty,
                      spec.size_step, spec.size_place)
    if qty <= 0.0:
        return False
    direction = -1 if pos_acct.direction > 0 else +1
    close_ts = (decision_ts + f.tf_hours * MS_1H if known_at is None else known_at)
    fill_after = close_ts - f.ms_exec
    broker.submit_limit(RestingLimitOrder(
        direction=direction, limit_price=float(target),
        armed_at_ts=int(decision_ts),
        expiry_ts=fill_after + int(n_fill) * MS_1H,
        fill_after_ts=fill_after,
        symbol=key, role="reduce", reason=intent.reason,
        reduce_qty=qty, qty_frac_snap=qty / pos_acct.open_qty,
        meta={"direction": pos_acct.direction,
              "execution_step_ms": f.ms_exec,
              "execution_not_before": close_ts}))
    return True


# ---------------------------------------------------------------------------
# 交易重构(共享账本 → 逐笔;含 v5 路由标签追溯 + campaign/tf/symbol 标注)
# ---------------------------------------------------------------------------


def _reconstruct_trades(ledger: list, seg_bounds: dict) -> list:
    trades = reconstruct_trades(ledger, seg_bounds)
    for t in trades:
        key = t["symbol"]                    # reconstruct 以复合键为 symbol
        sym, tf = parse_key(key)
        t["campaign"] = key
        t["symbol"] = sym
        t["tf"] = tf
    trades.sort(key=lambda x: (x["open_ts"] or 0, x["campaign"]))
    return trades


__all__ = [
    "ExecutionConfig", "CampaignFrame", "build_campaign_frame", "run_campaigns",
    "campaign_key", "parse_key",
]
