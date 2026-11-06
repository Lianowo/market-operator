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
- 每执行细 bar 的因果序(合并全部战役,同 ts 按 campaign key 字典序):
  ① 开盘撮合(broker.on_bar_open)→ ② 挂单处理(broker.on_15m_bar,细
  帧通用)→ ③ mark-to-market(资金费/权益/近似强平,预期 0)→ ④ 认知
  bar 收盘推进(仅本战役最末执行细 bar):§8 持仓管理(PositionManager,
  输入认知 TF bar,同 v4 声明:入场成交的认知 bar 不做裁决)→ v6 路由
  决策(MarketRouter;段内才启用 OPEN)。

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

from bisect import bisect_left, bisect_right
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .aggregate import TF_HOURS
from . import capacity
from .replay import (
    MS_15M,
    MS_1H,
    BacktestResult,
    SymbolCognition,
    assert_aligned,
    assert_fill_after_decision,
    annotate_excursions,
    reconstruct_trades,
)
from .broker import Broker, OrderIntent, OrderKind, RestingLimitOrder, floor_step
from .position import PositionManager
from .hypothesis import StructureView, execution_target
from .split import split_boundary
from .routing import RouterParams, MarketRouter
from .structure import Params

@dataclass(frozen=True)
class ExecutionConfig:
    """Current position-management and execution settings."""

    breakeven_on_reduce: bool = True
    reject_opp_lock_frac: float = 0.0
    # v21.7 锁利口径已收敛为单一参数 `reject_opp_lock_frac`(符号=arming 口径,
    # |frac| = 目标处锁住的行程比例);回吐预算 = (1−|frac|)·|target−entry|。
    # 旧的 `opp_lock_max_giveback_r`(R 单位第二把尺子)已删除。
    # v21.6 出场推进粒度(False ⇒ 逐位旧行为:§8.2/§8.3 只在认知 bar 收盘推进)
    # True ⇒ 每根已收盘执行细 bar 也推进价格型出场(目标/锁利/移动防线),
    # 不影响路由、准入、开仓与任何时间类计数(见 position.on_bar.price_only)。
    exit_exec: bool = False
    # exit_exec=True 时是否也把 §8.3 移动防线搬到执行细 bar(False ⇒ 只提前
    # §8.2 目标/锁利,trail 仍只在认知收盘推进)
    exit_exec_trail: bool = True
    # v21.6 细尺度 pivot 提前推进§8.3(False ⇒ 逐位旧行为):
    # 帧上 `CampaignFrame.exec_pivots` 给出「更细周期在该执行细 bar 收盘确认的
    # pivot」,本开关把它们喂给同一根 bar 的价格型裁决;不加时间计数、不消费事件。
    # 与 exit_exec 正交:pivot_exec 只补 pivot,是否也提前 §8.2 仍由 exit_exec 决定。
    pivot_exec: bool = False
    # ── 回吐预算单位(2026-10-30)───────────────────────────────────────────
    # `None` ⇒ 沿用 span 比例口径:budget = (1−|frac|)·|target−entry|。
    # 数值 ⇒ 改用**ATR 单位**:budget = giveback_atr_k × ATR(建仓后首根冻结),
    # 并封顶为 span(不允许回吐超过整段计划行程)。
    # 动机:span 在 0.95R~6R 之间变化,同一个 75% 在不同目标距离下行为完全不同;
    # `1.0–1.5R` 区间被实测为死区(均净 +0.019R),正是这种尺度不一致的机械后果。
    giveback_atr_k: Optional[float] = None
    accept_reenter_box_exit: bool = False
    vol_based_frac_map: Optional[dict] = None
    partial_exit_enabled: bool = False




def require_exec_pivots(pivot_exec: bool, frames) -> None:
    """`pivot_exec` 的装配前置条件:帧上必须真有细周期 pivot 流。

    `CampaignFrame.exec_pivots` 由显式提供细周期 pivot 的调用方装配;
    `scenario.build_frames`(验收/生产同源链)不装配。若开关打开而帧上没有数据,
    回放逐位等于关闭 —— 这种"静默无效"最容易被读成"该机制没有价值",因此在这里
    直接报错,把"没接上"和"没用"区分开。
    """
    if pivot_exec and not any(getattr(f, "exec_pivots", None) for f in frames):
        raise ValueError(
            "pivot_exec=True 但场景帧没有 exec_pivots。请改用 exit_exec,或先补齐细周期 "
            "pivot 装配,否则该开关会静默无效。")


def _increment_counter(counters: dict, key: str, n: int = 1) -> None:
    """Accumulate optional diagnostic counters without prepopulating keys."""
    counters[key] = counters.get(key, 0) + n


def _count_go(counters: dict, events, go_set: frozenset,
              *, mic: bool = False) -> int:
    """统计"送入路由的 GO 事件"次数,并返回本次条数。

    口径(此前两条路径各自内联,容易漂移):

    - ``go_events`` 是**次数**,不是去重后的事件数:同一市场事件若在认知帧和
      trigger 帧都被检出,两条路径各自独立决策,各计一次;
    - ``mic_go_events`` 是其中来自 trigger 帧的部分,恒 ≤ ``go_events``;
    - 只统计 ``go_set`` 内的 ``(id, direction)`` —— 白名单事件是"GO",被路由
      拒绝/闸门挡下的都不改变这个计数(拒绝原因另有 counters)。
    """
    n = sum(1 for e in events if (e.id, e.direction) in go_set)
    _increment_counter(counters, "go_events", n)
    if mic:
        _increment_counter(counters, "mic_go_events", n)
    return n


def campaign_key(symbol: str, tf_label: str) -> str:
    """战役键(broker/账本/交易归属的复合键;含 "@" 与 symbol 分隔安全)。"""
    return f"{symbol}@{tf_label}"


def parse_key(key: str) -> tuple:
    """campaign_key 逆变换:返回 (symbol, tf_label)。"""
    sym, tf = key.rsplit("@", 1)
    return sym, tf


# ---------------------------------------------------------------------------
# 战役帧(认知 TF bar + 执行细帧,严格对齐断言)
# ---------------------------------------------------------------------------


@dataclass
class CampaignFrame:
    """单战役:认知缓存(TF bar)+ 执行细帧(已对齐断言)。

    exec_bars[label] = (ts, o, h, l, c, v)(升序网格);close_edge[exec_label]
    = [认知 bar 索引 i, …](该执行细 bar 是这些认知 bar 的最末细 bar)。认知
    bar i 的执行路径区间 [t_i, t_i + TF) 恒为更细帧 bar 的真子序列(首/末
    不完整聚合桶如实,区间内无洞 —— 对齐断言)。

    v10 增量(docs/v10-protocol.md §A,加性,默认 None ⇒ 逐位不变):mic =
    trigger 帧认知缓存(trigger.MicCognition;事件知时细化层,可选挂载)。
    """

    key: str
    symbol: str
    tf_label: str
    tf_hours: int
    ms_exec: int
    cog: SymbolCognition                      # TF 帧认知缓存
    exec_bars: dict                           # 执行细 bar 标签 → OHLCV 元组
    exec_labels: tuple                        # 升序全部执行标签
    close_edge: dict                          # 最末执行标签 → [认知 bar 索引]
    exec_index_of: dict                       # 认知 bar 索引 → 其区间细标签列表
    mic: Optional[object] = None              # v10:MicCognition(可选)
    # v21.6:执行细 bar 标签 → 更细周期在该 bar 收盘确认的 pivot 列表
    # (arm.pivot_exec 才消费;缺省空 ⇒ 逐位旧行为)
    exec_pivots: dict = field(default_factory=dict)


def build_campaign_frame(cog: SymbolCognition, df_exec: pd.DataFrame,
                         tf_label: str,
                         mic_cog: Optional[object] = None, *,
                         execution_seconds: Optional[int] = None) -> CampaignFrame:
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
    if ms_exec not in (MS_15M, MS_1H) or ms_tf % ms_exec:
        raise ValueError("execution step must be 15m or 1h and divide cognition")
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
    if tf_hours == 1:
        # 1h 战役:复用 v4 的 1h⟷15m 覆盖断言(完整覆盖 + 每根 4 子 bar)
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


def run_campaigns(frames: dict, *, segment: str, dev_frac: float,
                  equity0: float, risk_frac: float, leverage_cap: float,
                  taker_fee: float, slippage_per_side: float,
                  funding_per_8h: float, cost_multiplier: float,
                  contracts: dict, params: Params, n_fill: int,
                  portfolio_cap_frac: float = 0.03,
                  maker_fee: Optional[float] = None,
                  router_params: Optional[RouterParams] = None,
                  router_factory: Optional[type] = None,
                  enable_mic: bool = False,
                  arm: Optional[ExecutionConfig] = None,
                  router_params_by_tf: Optional[dict] = None,
                  tf_cap_by_tf: Optional[dict] = None,
                  dynamic_risk_tiers: Optional[list] = None,
                  capacity_mode: str = capacity.ADMISSION_FULL_COST,
                  funding_rates: Optional[dict] = None,
                  slippage_by_symbol: Optional[dict] = None,
                  symbol_stop_risk_frac: Optional[dict] = None,
                  reserve_by_tf: Optional[dict] = None,
                  directional_cap_frac: Optional[float] = None,
                  ) -> BacktestResult:

    """Replay campaigns in timestamp/key order through the production chain."""
    if segment not in ("full", "dev", "test"):
        raise ValueError(f"unknown segment {segment}")
    if not (0.0 < portfolio_cap_frac <= 1.0):
        raise ValueError("portfolio_cap_frac must be in (0, 1]")

    keys = sorted(frames)
    base_rp = router_params or RouterParams()
    router_cls = router_factory or MarketRouter
    seg_bounds: dict = {}
    states: dict[str, _CampaignState] = {}
    for key in keys:
        f = frames[key]
        n = len(f.cog.bars)
        b = split_boundary(n, dev_frac)
        lo, hi = (0, n) if segment == "full" else ((0, b) if segment == "dev"
                                                   else (b, n))
        seg_bounds[key] = {"lo": lo, "hi": hi, "n": n,
                           "ts_lo": f.cog.bars[lo][0] if lo < n else None,
                           "ts_hi": f.cog.bars[hi - 1][0] if hi > 0 else None}
        rp_for_key = (router_params_by_tf.get(f.tf_label, base_rp)
                      if router_params_by_tf is not None else base_rp)
        states[key] = _CampaignState(
            frame=f, router=router_cls(rp_for_key),
            pos_mgr=PositionManager(
                params, allow_add=False,
                breakeven_on_reduce=(arm.breakeven_on_reduce if arm is not None
                                     else True),
                time_stop=False,             # §8.6 关闭(原臂 B 接线)
                reject_opp_lock_frac=(arm.reject_opp_lock_frac if arm is not None
                                      else 0.0),
                giveback_atr_k=(arm.giveback_atr_k if arm is not None
                                else None),
                accept_reenter_box_exit=(arm.accept_reenter_box_exit if arm is not None
                                         else False),
                vol_based_frac_map=(arm.vol_based_frac_map if arm is not None
                                    else None),
                partial_exit_enabled=(arm.partial_exit_enabled if arm is not None
                                      else False)),

            lo=lo, hi=hi)
    # 事件对象使用规范 ID(BREAKOUT_ACCEPTED/LEVEL_REJECTED)，配置允许使用
    # ACCEPT/REJECT 等别名。路由器内部已经按规范集合判定；诊断计数必须使用
    # 同一集合，否则实际发生的 mic/canonical GO 会被静默记成 0。
    go_set = base_rp._normalized_whitelist

    broker = Broker(equity=equity0, risk_frac=risk_frac, leverage_cap=leverage_cap,
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
                    directional_stop_risk_frac=directional_cap_frac)

    equity_rows: list = []
    cap_skips: list = []
    claim_events: list = []
    # go_events/mic_go_events 的口径见 _count_go(送入路由的次数,非去重后事件数)
    counters = {"go_events": 0, "open_intents": 0, "opens_filled": 0,
                "min_size_skips": 0, "liquidations": 0,
                "limits_armed": 0, "limit_fills_entry": 0, "limit_expired": 0,
                "limit_min_size_skips": 0,
                "reduce_limits_armed": 0, "reduce_limit_fills": 0,
                "reduce_timeout_taker": 0, "resting_canceled": 0,
                "e1_priority_cancels": 0, "portfolio_cap_skips": 0,
                "protective_stop_fills": 0,
                "mic_go_events": 0}

    fine_ts = sorted({t for k in keys for t in frames[k].exec_labels})
    exec_set = set(fine_ts)
    if enable_mic:
        # v10:trigger 帧时间轴并入(exec 网格 ⊂ trigger 网格;成交/决策全部
        # 以"已收盘 bar"推进,无前馈 —— 测试钉死)
        # v21.5:支持部分战役挂载 MicCognition(如 4h→1h、12h→2h,而 2h 保持同周期)
        mic_index: dict = {}
        for key in keys:
            mic = frames[key].mic
            if mic is not None:
                mic_index[key] = {int(b[0]): a for a, b in enumerate(mic.bars)}
            else:
                mic_index[key] = {}
        full_ts = sorted(exec_set | {t for key in keys for t in mic_index[key]})
    else:
        full_ts = fine_ts

    # v21.6 出场推进粒度开关(arm 缺省/False ⇒ 逐位旧行为)
    exit_exec = bool(arm.exit_exec) if arm is not None else False
    exit_exec_trail = bool(arm.exit_exec_trail) if arm is not None else True
    pivot_exec = bool(arm.pivot_exec) if arm is not None else False
    # v21.7:`pivot_exec` 只在帧携带 `exec_pivots` 时才有意义 —— 该字段目前只有
    # 显式调用方装配,`scenario.build_frames` 不装配。缺数据时
    # 静默放行会产出"开关无效"的读数,而它极易被误读成"该机制无效"(v21.6 的
    # pivot_exec 结论就来自手工装配的脚本,不是这条生产链)。宁可直接报错。
    require_exec_pivots(pivot_exec, (st.frame for st in states.values()))
    fine_execution = any(f.tf_hours > 1 and f.ms_exec < MS_1H for f in frames.values())
    if fine_execution:
        if any(f.ms_exec != MS_15M for f in frames.values()):
            raise ValueError("fine execution requires a shared 15m execution clock")
        # Drive decisions at their CLOSE, never at the opening label of a
        # coarser trigger. Market fills happen at the following execution open.
        mic_index = {key: ({int(b[0]) + frames[key].mic.trigger_tf_seconds * 1000 - MS_15M: a
                           for a, b in enumerate(frames[key].mic.bars)}
                          if enable_mic and frames[key].mic is not None else {}) for key in keys}
        full_ts = fine_ts
    for ts in full_ts:
        for key in keys:                       # 同 ts 按 campaign key 字典序
            st = states[key]
            f = st.frame
            q = f.exec_bars.get(ts)
            if q is not None:
                sym = f.symbol
                _ts, _o, _h, _l, c, _v = q
                # ① 开盘撮合(认知决策 ⇒ 下一执行细 bar 开盘成交)
                rows = broker.on_bar_open(key, ts, _o)
                _apply_open_rows(st, broker, key, ts, rows, counters, claim_events)
                if fine_execution:
                    rows = broker.on_bar_open(key, ts, _o, mic_only=True)
                    _apply_open_rows(st, broker, key, ts, rows, counters, claim_events)
                # ①.5 保护性止损:结构失效价 stop-market,逐执行细 bar 检查
                #     (触发即成交于失效价/缺口开盘价;兑现止损位,消除 §8.1
                #     收盘越界→次开盘口径导致的平均亏损 ≈2R 超调)
                stop_row = broker.execute_protective_stop(key, ts, _o, _h, _l)
                if stop_row is not None:
                    _apply_open_rows(st, broker, key, ts, [stop_row], counters,
                                     claim_events)
                    counters["protective_stop_fills"] += 1
                # ② 挂单处理(严格穿过成交;超时撤单/转市价)
                had_position = broker.position_of(key) is not None
                rows = broker.on_15m_bar(key, ts, _o, _h, _l, c)
                _apply_resting_rows(st, broker, key, ts, rows, counters, claim_events)
                if fine_execution:
                    rows = broker.on_15m_bar(key, ts, _o, _h, _l, c, mic_only=True)
                    _apply_resting_rows(st, broker, key, ts, rows, counters,
                                        claim_events)
                if not had_position and broker.position_of(key) is not None:
                    # Limit entry and stop in the same OHLC bar: assume the
                    # adverse path, never let a new fill evade protection.
                    # 该复查必须在两条执行路径上都发生:保护是事实,不是快路径的
                    # 特权。此前只在 fine_execution 下检查,粗路径存在当根裸奔缺口。
                    stop_row = broker.execute_protective_stop(key, ts, _o, _h, _l)
                    if stop_row is not None:
                        _apply_open_rows(st, broker, key, ts, [stop_row], counters,
                                         claim_events)
                        counters["protective_stop_fills"] += 1
                # ③ 持仓路径解决:同根因果序 ①先于② 结构性解决(同 v4 声明)
                # ④ mark-to-market(执行细 bar 收盘;强平保护沿用,预期 0)
                broker.mark(key, ts + f.ms_exec if fine_execution else ts, c)
                counters["liquidations"] = broker.n_liquidations
                # ④.5 v21.6 出场推进粒度(arm.exit_exec;False ⇒ 整段不执行):
                #     每根已收盘执行细 bar 推进一次**价格型**出场(§8.2 目标/
                #     锁利、§8.3 移动防线、§8.1 兜底),让 2h/4h/1d 仓位不必
                #     等到认知收盘才收网。只碰持仓管理 —— 路由/准入/开仓、
                #     事件消费与时间类计数(bars_held/§8.6)全部不动,
                #     ATR 沿用最近一次认知 bar 的冻结值。
                if exit_exec or pivot_exec:
                    pm_pos = st.pos_mgr.positions.get(sym)
                    if (pm_pos is not None
                            and broker.position_of(key) is not None
                            and int(ts) >= int(pm_pos.opened_ts)):
                        # v21.6:细尺度 pivot 只在本执行细 bar 收盘才确认,
                        # 逐根喂入才能提前推进§8.3(空列表 ⇒ 与旧路径等价)
                        pv = f.exec_pivots.get(ts, ()) if pivot_exec else ()
                        if exit_exec or pv:
                            _increment_counter(counters, "exit_exec_ticks" if exit_exec
                                else "pivot_exec_ticks")
                            before = _claim_state(pm_pos)
                            m2 = st.pos_mgr.on_bar(sym, q, st.last_atr,
                                                   new_pivots=pv,
                                                   price_only=True,
                                                   skip_trail=not exit_exec_trail)
                            _record_claim_transitions(
                                claim_events, key, broker, pm_pos, before, ts,
                                source="execution")
                            broker.update_protective_invalid(key, pm_pos.invalid)
                            _apply_mgmt(st, broker, m2, ts, counters, n_fill)
                # ⑤ 认知 bar 收盘推进(仅本战役最末执行细 bar)
                for i in f.close_edge.get(ts, ()):
                    _cognition_step(st, broker, i, counters, cap_skips,
                                    seg_bounds[key], go_set, n_fill, claim_events,
                                    states=states)
            if enable_mic:
                a = mic_index[key].get(ts)
                if a is not None:
                    _mic_trigger_step(st, broker, a, ts, counters, cap_skips,
                                      seg_bounds[key], go_set, n_fill,
                                      claim_events,
                                      execution_only=fine_execution,
                                      states=states)
        if ts in exec_set:
            equity_rows.append((ts, broker.equity_mtm(), broker.cash,
                                broker.unrealized(), broker.funding_accrued(),
                                len(broker.positions)))

    per_campaign: dict = {}
    for key in keys:
        per_campaign[key] = dict(states[key].router.counters)
        for k, v in states[key].router.counters.items():
            counters[k] = counters.get(k, 0) + v
    counters["campaigns"] = per_campaign
    # 成交侧拒绝原因(主张前提/结构契约/容量/最小步长):只增字段,不改账目
    counters["entry_skips"] = dict(sorted(broker.n_skips.items()))
    # v10:mic 决策决定时刻可晚至段末认知 bar 覆盖区间内(标签 > 末 bar 标签)
    # ⇒ 段归属窗的上界延至末认知 bar 最末 trigger bar(只对 mic 回放;
    # ts_lo/ts_hi 在 seg_bounds 内的 canonical 口径不变,对外存储不变)
    attr_bounds = seg_bounds
    if enable_mic:
        attr_bounds = {}
        for key in keys:
            b = dict(seg_bounds[key])
            mic = frames[key].mic
            if b["ts_hi"] is not None and mic is not None:
                b = {**b, "ts_hi": b["ts_hi"] + frames[key].tf_hours * MS_1H
                     - mic.trigger_tf_seconds * 1000}
            attr_bounds[key] = b
    trades = _reconstruct_trades(broker.ledger, attr_bounds)
    completed_by_trade: dict = {}
    migrated_by_trade: dict = {}
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
    # A2:右尾量度(mfe_r/mae_r/hold_bars/n_partial_legs);纯增量,不改账目
    annotate_excursions(trades, frames)
    res = BacktestResult(segment=segment, cost_multiplier=cost_multiplier,
                         equity0=equity0, ledger=list(broker.ledger),
                         equity_rows=equity_rows,
                         trades=trades,
                         counters=counters, seg_bounds=seg_bounds,
                         contracts={s: vars(c) for s, c in contracts.items()},
                         cap_skips=cap_skips,
                         campaign_states={k: {"pos_mgr": states[k].pos_mgr,
                                              "router": states[k].router}
                                           for k in keys},
                          claim_events=claim_events)
    assert_fill_after_decision(res.ledger)
    return res


def _claim_id(meta: dict) -> Optional[tuple]:
    """账本 meta 里的主张身份 → 可哈希 tuple(box/uid, role, geometry_version)。"""
    raw = meta.get("claim_id")
    if not raw:
        return None
    box, role, version = raw
    return (tuple(box), str(role), int(version))


def _claim_state(pos) -> tuple:
    """持仓 claim 状态快照；仅用于检测状态机已发生的事实变化。"""
    return (getattr(pos, "claim_completed_ts", None),
            getattr(pos, "claim_migrated_ts", None))


def _record_claim_transitions(events: list, key: str, broker: Broker, pos,
                              before: tuple, ts: int, *, source: str) -> None:
    """把 PositionManager 已完成的生命周期变化复制到只读事件流。"""
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


def _claim_event_for_trade(events: list, trade_id: str) -> dict:
    """返回该交易最早的 claim 身份事实，供退出成交关联。"""
    return next((event for event in events
                 if event.get("trade_id") == trade_id
                 and event.get("event") == "CREATED"), {})


def _record_claim_exit(events: list, key: str, row: dict) -> None:
    if row.get("kind") not in ("EXIT", "LIQUIDATION") or not row.get("trade_id"):
        return
    origin = _claim_event_for_trade(events, row["trade_id"])
    events.append({
        "trade_id": row["trade_id"], "campaign": key,
        "symbol": origin.get("symbol"), "claim_id": origin.get("claim_id"),
        "box_id": origin.get("box_id"),
        "geometry_version": origin.get("geometry_version"),
        "event": "EXITED", "ts": int(row["ts"]), "price": row.get("price"),
        "reason_code": row.get("reason_code"),
    })


def _record_claim_open(events: list, key: str, symbol: str, row: dict) -> None:
    """成交后用 trade_id 回填此前已发生的创建、确认和下单事实。"""
    m = row["meta"]
    common = {"trade_id": row["trade_id"], "campaign": key, "symbol": symbol,
              "claim_id": m.get("claim_id"), "box_id": m.get("box_id"),
              "geometry_version": m.get("geometry_version")}
    events.extend((
        {**common, "event": "CREATED",
         "ts": int(m.get("claim_created_ts", row["decision_ts"])),
         "price": m.get("claim_created_price"),
         "regime": m.get("claim_birth_regime")},
        {**common, "event": "CONFIRMED",
         "ts": int(m.get("claim_confirmed_ts", row["decision_ts"])),
         "price": m.get("claim_confirmed_price"),
         "regime": m.get("claim_confirm_regime", m.get("regime"))},
        {**common, "event": "ORDERED", "ts": int(row["decision_ts"]),
         "price": m.get("limit_price", m.get("claim_confirmed_price"))},
        {**common, "event": "FILLED", "ts": int(row["ts"]),
         "price": row["price"]},
    ))


def _apply_open_rows(st: _CampaignState, broker: Broker, key: str, ts: int,
                     rows: list, counters: dict, claim_events: Optional[list] = None) -> None:
    """on_bar_open 账本行的战役侧处理(①;canonical 与 mic 触发共用)。"""
    f = st.frame
    sym = f.symbol
    for row in rows:
        k = row["kind"]
        if k == "OPEN":
            m = row["meta"]
            d_open = int(m["direction"])
            re_px = (m.get("zone_lo") if d_open > 0 else m.get("zone_hi"))
            st.pos_mgr.open(sym, d_open, row["price"],
                            float(m["invalid"]), execution_target(m, m.get("target_1")),
                            str(m.get("event_id")), ts,
                            _cog_index_of(f, ts),
                            reenter_invalid_px=re_px,
                            claim_id=_claim_id(m))
            if claim_events is not None:
                _record_claim_open(claim_events, key, sym, row)
            counters["opens_filled"] += 1
            if m.get("entry_mode") == "virtual_limit":
                _increment_counter(counters, "virtual_entry_fills")
        elif k == "REDUCE":
            # TIMEOUT_TO_TAKER 市价转换(非 deferred 语义:pos_mgr 已在决策时
            # 按 §8.2 立即记账,此处仅同步 broker 侧 stop)
            if broker.position_of(key) is not None \
                    and sym in st.pos_mgr.positions:
                broker.update_protective_invalid(
                    key, st.pos_mgr.positions[sym].invalid)
        elif k in ("EXIT", "LIQUIDATION"):
            if claim_events is not None:
                _record_claim_exit(claim_events, key, row)
            st.pos_mgr.positions.pop(sym, None)
            rows_cancel = broker.cancel_resting(
                key, ts, reason_code="POSITION_CLOSED",
                detail="position closed by EXIT/LIQUIDATION; resting "
                       "limit canceled")
            counters["resting_canceled"] += len(rows_cancel)
        elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
            counters["min_size_skips"] += 1


def _apply_resting_rows(st: _CampaignState, broker: Broker, key: str, ts: int,
                        rows: list, counters: dict,
                        claim_events: Optional[list] = None) -> None:
    """on_15m_bar 账本行的战役侧处理(②;canonical 与 mic 触发共用)。"""
    f = st.frame
    sym = f.symbol
    for row in rows:
        k = row["kind"]
        if k == "LIMIT_FILL":
            if row["meta"].get("role") == "reduce":
                counters["reduce_limit_fills"] += 1
            else:
                counters["limit_fills_entry"] += 1
                m = row["meta"]
                d_open = int(m["direction"])
                re_px = (m.get("zone_lo") if d_open > 0 else m.get("zone_hi"))
                st.pos_mgr.open(sym, d_open, row["price"],
                                float(m["invalid"]), execution_target(m, m.get("target_1")),
                                str(m.get("event_id")), ts,
                                _cog_index_of(f, ts),
                                reenter_invalid_px=re_px,
                                claim_id=_claim_id(m))
                if claim_events is not None:
                    _record_claim_open(claim_events, key, sym, row)
        elif k == "EXPIRED":
            counters["limit_expired"] += 1
        elif k == "TRIGGER" and row["reason_code"] == "VIRTUAL_TRIGGER":
            _increment_counter(counters, "virtual_entry_triggers")
        elif k == "CANCEL" and row["reason_code"] == "TIMEOUT_TO_TAKER":
            counters["reduce_timeout_taker"] += 1
        elif k == "CANCEL":
            if row.get("reason_code") == "MOVE_ALREADY_PLAYED_OUT":
                _increment_counter(counters, "watch_canceled_played_out")
        elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
            counters["min_size_skips"] += 1
            counters["limit_min_size_skips"] += 1


def _htf_views_for(st: _CampaignState, states: Optional[dict], close_moment_ms: int) -> tuple:
    """获取同标的更高周期(htf_hours >= 2 * tf_hours,如 2h->4h/12h/1d, 4h->12h/1d)在 close_moment_ms 前最新已收盘认知视图。"""
    if not states or not getattr(getattr(st.router, "params", None), "htf_wall_filter", False):
        return ()
    sym = st.frame.symbol
    tf_h = st.frame.tf_hours
    out = []
    for other in states.values():
        hf = other.frame
        if hf.symbol != sym or hf.tf_hours < 2 * tf_h:
            continue
        ms_htf = hf.tf_hours * MS_1H
        max_open_ts = int(close_moment_ms) - ms_htf
        bars = hf.cog.bars
        idx = bisect_right(bars, max_open_ts, key=lambda b: int(b[0])) - 1
        if idx >= 0:
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
            out.append(
                StructureView(
                    regime=(vw.regime if vw is not None else hf.cog.regimes[idx]),
                    zones=(vw.zones if vw is not None and vw.zones else act_zones),
                    atr=(vw.atr if vw is not None else hf.cog.atrs[idx]),
                    pivots=(vw.pivots if vw is not None else ()),
                    macro_imbalance=m_imb,
                    tf_hours=hf.tf_hours,
                )
            )
    return tuple(out)


def _mic_trigger_step(st: _CampaignState, broker: Broker, a: int, ts: int,
                      counters: dict, cap_skips: list, bounds: dict,
                      go_set: frozenset, n_fill: int, claim_events: Optional[list] = None,
                      *, execution_only: bool = False,
                      states: Optional[dict] = None) -> None:
    """v10 §A trigger bar 收盘推进:①' mic 市价撮合 → ②' mic 挂单 → mic 路由。

    §8 收割/mark/费用计提不在此处(canonical 粒度不变);决策只在已收盘
    trigger bar 上做出,成交严格在后(broker 行内断言)。

    """
    f = st.frame
    key, sym = f.key, f.symbol
    mic = f.mic
    _ts, _o, _h, _l, c, _v = mic.bars[a]
    # ①' mic 延迟单撮合(次一 trigger 开盘;执行帧处为 canonical ① 已兜底的
    # no-op —— mic_only 只撮 meta["mic"] 单)
    if not execution_only:
        _apply_open_rows(st, broker, key, ts,
                         broker.on_bar_open(key, ts, _o, mic_only=True), counters,
                         claim_events)
    # ②' mic 挂单(自次一 trigger bar 可成交;非 mic 挂单不消耗窗口)
    if not execution_only:
        _apply_resting_rows(st, broker, key, ts,
                            broker.on_15m_bar(key, ts, _o, _h, _l, c, mic_only=True),
                            counters, claim_events)
    # ⑤' mic 路由(段内才启用 OPEN;白名单/闸门在 router 内部)
    i = mic.cog_index_of[a]
    lo, hi = bounds["lo"], bounds["hi"]
    if not (lo <= i < hi):
        return
    new_events = mic.events_by.get(a)
    if not new_events:
        return
    _count_go(counters, new_events, go_set, mic=True)
    # Mixed clocks retain each event group's original OHLC/index for quality
    # and stop geometry. Only execution scheduling shares the fine grid.
    groups = {}
    for event in new_events:
        routing_bar = tuple(event.attrs.get("routing_bar", mic.bars[a]))
        routing_index = int(event.attrs.get("routing_index", a))
        groups.setdefault((routing_bar, routing_index), []).append(event)
    close_moment = int(_ts) + mic.trigger_tf_seconds * 1000
    htf_views = _htf_views_for(st, states, close_moment)
    decisions = []
    for (routing_bar, routing_index), events in groups.items():
        if htf_views:
            decisions.extend(st.router.on_bar(
                sym, routing_bar, routing_index, mic.views.get(a), mic.atrs[a], events,
                position_open=broker.position_of(key) is not None,
                entry_resting=broker.has_entry_limit(key),
                htf_views=htf_views))
        else:
            decisions.extend(st.router.on_bar(
                sym, routing_bar, routing_index, mic.views.get(a), mic.atrs[a], events,
                position_open=broker.position_of(key) is not None,
                entry_resting=broker.has_entry_limit(key)))
    # mic 挂单可成交窗口 = 本 trigger bar 之后任意更细 bar(小时桶口径与
    # broker 窗口机制一致:fill_after = 本小时桶前一小时,即成交流程自次一
    # trigger bar 起可成交);expiry = 决策时刻 + N_fill 墙钟小时(同 canonical)
    fill_after = (ts - (ts % MS_1H)) - MS_1H
    if execution_only:
        close_ts = int(_ts) + mic.trigger_tf_seconds * 1000
        _submit_decisions(st, broker, decisions, int(_ts), ts, close_ts,
                          {"mic": True, "cog_index": i,
                           "execution_step_ms": f.ms_exec,
                           "execution_not_before": close_ts}, counters, cap_skips,
                          n_fill, cancel_moment=close_ts)
        return
    _submit_decisions(st, broker, decisions, ts, fill_after, ts,
                      {"mic": True, "cog_index": i}, counters, cap_skips,
                      n_fill,
                      cancel_moment=ts + mic.trigger_tf_seconds * 1000)


def _cog_index_of(frame: CampaignFrame, exec_ts: int) -> int:
    """执行细 bar 落回认知 bar 索引(成交 bar ∈ 认知区间断言)。"""
    ms_tf = frame.tf_hours * MS_1H
    bucket = exec_ts - (exec_ts % ms_tf)
    bars = frame.cog.bars
    index = bisect_left(bars, bucket, key=lambda bar: int(bar[0]))
    if index < len(bars) and int(bars[index][0]) == bucket:
        return index
    raise AssertionError(f"{frame.key} exec ts {exec_ts} outside cognition")


def _rekey(intent: OrderIntent, key: str) -> OrderIntent:
    """OrderIntent 的 symbol 改为战役复合键(broker 以战役键记账)。"""
    return OrderIntent(kind=intent.kind, symbol=key,
                       qty_frac=intent.qty_frac, reason=intent.reason,
                       ref_price=intent.ref_price, ts=intent.ts)


def _apply_mgmt(st: _CampaignState, broker: Broker, mgmt: list, t: int,
                counters: dict, n_fill: int) -> None:
    """Submit management intents identically for both execution granularities."""
    key = st.frame.key
    for intent in mgmt:
        intent = _rekey(intent, key)
        armed_r = (intent.kind is OrderKind.REDUCE
                   and _arm_reduce_limit(broker, st, intent, t, n_fill))
        if armed_r:
            counters["reduce_limits_armed"] += 1
        else:
            broker.submit(intent, decision_ts=t)


def _cancel_structurally_dead_entries(st: "_CampaignState", broker: Broker, key: str,
                                      bar, atr: float, counters: dict) -> None:
    """A11:挂单的**结构性**失效 —— 结构前提没了就撤,而不是等 `n_fill` 墙钟小时。

    旧实现的过期条件是"决策后 12 小时"(`expiry_ts = fill_after + n_fill×1h`):
    箱沿早已被破、目标早已被行情走完,那张限价单还挂在那里等成交 —— 一旦成交,
    接的是行情末端。语义上挂单只应在**它的结构前提仍成立**时存在:

    1. 带被破:`level_rejected_limit`(拒绝后回抽买箱沿)与
       `breakout_accepted_limit`(A2 过伸突破回抽翻转边)都是"在
       `[zone_lo, zone_hi]` 这条带上接货"。收盘越过该带**远端外侧**
       (多头跌破 `zone_lo − k_bm·ATR` / 空头升破 `zone_hi + k_bm·ATR`)
       ⇒ 那条带已不在拍卖中,撤。
    2. 目标已过:收盘越过该单自己的 `target_1` ⇒ 它要等的回抽机会已被走完,
       再成交就是末端接货,撤。
    3. `n_fill` 墙钟过期保留为最后兜底(不删除)。

    只用因果可得事实:本根认知 bar 的收盘 + 挂单自身 meta(`zone_lo/zone_hi/
    direction/target_1`)。不查未来、不改策略判定。
    """
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
            t = float(o.target_1)
            if d > 0 and c > t:
                why = f"target_passed(close={c:.8g} > {t:.8g})"
            elif d < 0 and c < t:
                why = f"target_passed(close={c:.8g} < {t:.8g})"
        if why is not None:
            reasons.append(why)
    # 槽位纪律是"每战役同时一仓一挂单";只有**全部**挂单都失效才整批撤,
    # 避免把仍在观察的同族挂单误杀(当前观察挂单仍沿用整组撤单口径)。
    if not reasons or len(reasons) != len(orders):
        return
    cancel_ts = int(ts) + int(st.frame.tf_hours * MS_1H)
    rows = broker.cancel_resting(key, cancel_ts, reason_code="STRUCTURE_INVALID",
                                 detail=" | ".join(reasons)[:200])
    counters["struct_canceled"] = counters.get("struct_canceled", 0) + len(rows)


def _cognition_step(st: _CampaignState, broker: Broker, i: int,
                    counters: dict, cap_skips: list, bounds: dict,
                    go_set: frozenset, n_fill: int, claim_events: list,
                    *, states: Optional[dict] = None) -> None:
    """认知 bar i 收盘:§8 持仓管理 → v6 路由(段内才启用 OPEN)。"""
    f = st.frame
    key, sym = f.key, f.symbol
    cog = f.cog
    bar = cog.bars[i]
    t = int(bar[0])
    new_events = cog.events_by.get(i, [])
    new_pivots = cog.pivots_by.get(i, [])
    atr = cog.atrs[i]
    st.last_atr = atr

    # A11:挂单只在其结构前提仍成立时存在(带被破 / 目标已过即撤;
    # `n_fill` 墙钟过期只作最后兜底)。放在 §8 之前:本 bar 的收盘已经是
    # "结构前提是否还成立"的最新事实。
    _cancel_structurally_dead_entries(st, broker, key, bar, atr, counters)

    # §8 六条:若仓位在本根认知 bar 区间内(opened_ts > bar[0])由降档触发或限价成交,
    # 截取 [opened_ts, t + TF) 的成交后真实子区间极值参与价格型保护裁决,消除建仓当根大周期裸奔盲区。
    pos_pm = st.pos_mgr.positions.get(sym)
    if pos_pm is not None and pos_pm.opened_ts > bar[0]:
        sub_labels = [
            ts_sub for ts_sub in f.exec_index_of.get(i, ())
            if int(ts_sub) >= int(pos_pm.opened_ts)
        ]
        if sub_labels:
            sub_bars = [f.exec_bars[ts_sub] for ts_sub in sub_labels]
            sub_bar = (
                bar[0],
                sub_bars[0][1],
                max(b[2] for b in sub_bars),
                min(b[3] for b in sub_bars),
                sub_bars[-1][4],
                sum(b[5] for b in sub_bars),
            )
            before = _claim_state(pos_pm)
            mgmt = st.pos_mgr.on_bar(sym, sub_bar, atr, (), (), price_only=True)
            _record_claim_transitions(claim_events, key, broker, pos_pm, before, t,
                                      source="cognition")
        else:
            mgmt = []
    else:
        before = _claim_state(pos_pm) if pos_pm is not None else None
        mgmt = st.pos_mgr.on_bar(sym, bar, atr, new_pivots, new_events)
        if pos_pm is not None:
            _record_claim_transitions(claim_events, key, broker, pos_pm, before, t,
                                      source="cognition")
    if sym in st.pos_mgr.positions and broker.position_of(key) is not None:
        broker.update_protective_invalid(key, st.pos_mgr.positions[sym].invalid)
    _apply_mgmt(st, broker, mgmt, t, counters, n_fill)

    # v6 路由(仅段内启用;白名单与闸门在 MarketRouter 内部)
    lo, hi = bounds["lo"], bounds["hi"]
    if not (lo <= i < hi):
        return
    # 多周期级联职责边界:当本战役已挂载细周期触发器(f.mic is not None)时,
    # 边界拒绝(LEVEL_REJECTED)已由细周期实时捕捉与确认,粗周期收盘不再迟钝重发 LEVEL_REJECTED
    route_events = (
        [e for e in new_events if e.id != "LEVEL_REJECTED"]
        if f.mic is not None
        else new_events
    )
    if not route_events:
        return
    _count_go(counters, route_events, go_set)
    htf_views = _htf_views_for(st, states, t + f.tf_hours * MS_1H)
    if htf_views:
        decisions = st.router.on_bar(
            sym, bar, i, cog.views.get(i), atr, route_events,
            position_open=broker.position_of(key) is not None,
            entry_resting=broker.has_entry_limit(key),
            htf_views=htf_views)
    else:
        decisions = st.router.on_bar(
            sym, bar, i, cog.views.get(i), atr, route_events,
            position_open=broker.position_of(key) is not None,
            entry_resting=broker.has_entry_limit(key))
    fill_after = t + f.tf_hours * MS_1H - f.ms_exec     # 决策 bar 最末细 bar
    extra = ({"execution_step_ms": f.ms_exec,
              "execution_not_before": t + f.tf_hours * MS_1H}
             if f.tf_hours > 1 and f.ms_exec < MS_1H else {})
    _submit_decisions(st, broker, decisions, t, fill_after,
                      t + f.tf_hours * MS_1H if extra else fill_after, extra,
                      counters, cap_skips, n_fill,
                      cancel_moment=t + f.tf_hours * MS_1H)


def _submit_decisions(st: _CampaignState, broker: Broker, decisions: list,
                      t: int, fill_after: int, expiry_base: int,
                      extra_meta: dict, counters: dict, cap_skips: list,
                      n_fill: int, cancel_moment: int) -> None:
    """路由决策 → 组合容量闸 → broker 提交(canonical 认知收盘与 v10 mic 触发
    共用;t = 决策时刻标签(账本 decision_ts 口径,canonical 不变);
    fill_after/expiry_base:挂单窗口锚点;extra_meta:溯源附加 —— mic 路径
    带 {"mic": True, "cog_index": i};cancel_moment = 撤单因果护栏与撤单账
    本行时标:决策的**收盘时刻**(认知路径 = 认知 bar 标签+TF,mic 路径 =
    trigger 标签+step;任何 armed_at_ts ≥ 该时刻的挂单都是不可能的未来物)。

    注:v10 之前撤单时标用认知标签 t,因 canonical 挂单 armed 只取认知标
    签(< t)而恒成立;v10 mic 挂单合法以 trigger 标签武装(∈ [t, t+TF),
    仍严格早于决策收盘时刻)⇒ 护栏/时标统一为收盘时刻,canonical 行为与
    既有断言(fill>decision、撤单晚于武装)不变。

    """
    key = st.frame.key
    for d in decisions:
        if d.kind == "cancel_entry":
            for od in broker.resting.get(key, ()):
                if od.armed_at_ts >= cancel_moment:
                    raise RuntimeError(
                        f"BREAKOUT_ACCEPTED cancel moment {cancel_moment} <= armed "
                        f"{od.armed_at_ts} ({key})")
            rows_cancel = broker.cancel_resting(
                key, cancel_moment, reason_code="E1_PRIORITY",
                detail=str(d.meta.get("reason")))
            counters["e1_priority_cancels"] += len(rows_cancel)
            continue
        if d.kind not in ("market", "limit"):
            raise RuntimeError(f"unknown route decision {d.kind}")
        # 组合容量(口径唯一,见 capacity):FIFO 决策时刻整笔跳过,不缩量。
        # 风险金额与成交裁剪同用「风险资本」;杠杆上限仍用盯市权益(偿付能力)。
        equity = broker.equity_mtm()
        capital = broker.risk_capital()
        risk_per_unit = abs(d.intent.ref_price - d.hyp.invalid)
        direction = int(d.hyp.direction)
        # 相反主张:同一标的只能有一个答案,先了结旧的才允许新的。
        opposite = broker.opposite_claim_open(key, direction)
        qty_full = (0.0 if opposite is not None else
                    broker.qty_for_entry(capital, d.intent.ref_price,
                                         d.hyp.invalid, key, use_budget=False,
                                         notional_equity=equity,
                                         direction=direction))
        cap_frac, used, _headroom, pool = broker.capacity_usage(
            capital, key, moment=capacity.DECISION)
        delta = capacity.delta_exposure(
            qty_full, d.intent.ref_price, risk_per_unit,
            taker_fee=broker.taker_fee, slippage=broker.slippage,
            include_costs=broker.risk_budget.includes_costs(capacity.DECISION))
        cap_blocked = False
        skip_reason = "PORTFOLIO_CAP"
        if opposite is not None:
            cap_blocked = True
            skip_reason = "OPPOSITE_CLAIM"
        elif broker.risk_budget.is_active and (
                qty_full <= 0.0 or used + delta > cap_frac * capital + 1e-9):
            cap_blocked = True
            skip_reason = "TF_CAP" if pool == "tf" else "PORTFOLIO_CAP"
        # 主张预算:同一标的同方向的全部在仓(跨周期)共用一份,周期不叠加
        if not cap_blocked:
            claim, claim_pool, _cf = broker.claim_headroom(
                key, direction, capital, moment=capacity.DECISION)
            if used + delta > 0 and math.isfinite(claim) and delta > max(claim, 0.0) + 1e-9:
                cap_blocked = True
                skip_reason = "CLAIM_CAP"
                pool = claim_pool

        # v22:同向净风险上限 (directional_cap_frac, §6.4)
        dir_cap = broker.risk_budget.directional_cap_frac
        if not cap_blocked and dir_cap is not None and dir_cap > 0.0:
            dir_used = broker.directional_usage(
                direction, moment=capacity.DECISION, capital=capital,
                notional_equity=equity)
            # 同向意图用实际计划入场价及 size_mult,与 direct broker 准入一致;
            # 普通池的既有整笔判断/成交裁剪口径不变。
            dir_price = d.intent.ref_price if d.kind == "market" else d.limit_price
            dir_qty = broker.qty_for_entry(
                capital, dir_price, d.hyp.invalid, key, use_budget=False,
                size_mult=float(d.meta.get("size_mult", 1.0) or 1.0),
                notional_equity=equity, direction=direction)
            dir_delta = capacity.delta_exposure(
                dir_qty, dir_price, abs(dir_price - d.hyp.invalid),
                taker_fee=broker.taker_fee, slippage=broker.slippage,
                include_costs=broker.risk_budget.includes_costs(capacity.DECISION))
            if dir_used + dir_delta > dir_cap * capital + 1e-9:
                cap_blocked = True
                skip_reason = "DIRECTIONAL_CAP"
                cap_frac = dir_cap
                used = dir_used
                delta = dir_delta

        if cap_blocked:
            counters["portfolio_cap_skips"] += 1
            cap_skips.append({
                "ts": t, "campaign": key, "event_id": d.hyp.event_id,
                "direction": d.hyp.direction, "route": d.route,
                "reason_code": skip_reason,
                "worst_case": used + delta, "budget_frac": cap_frac,
                "equity": equity, "risk_capital": capital})
            continue


        counters["open_intents"] += 1
        origin = d.meta.get("zone_origin_known")
        if origin is not None and 0 <= int(origin) < len(st.frame.cog.bars):
            origin = int(origin)
            birth_bar = st.frame.cog.bars[origin]
            birth_meta = {
                "claim_created_ts": int(birth_bar[0]),
                "claim_created_price": float(birth_bar[4]),
                "claim_birth_regime": st.frame.cog.regimes[origin].value,
            }
        else:
            birth_meta = {
                "claim_created_ts": int(d.hyp.ts),
                "claim_created_price": d.intent.ref_price,
                "claim_birth_regime": d.hyp.regime.value,
            }
        lifecycle_meta = {
            **birth_meta,
            "claim_confirmed_ts": int(d.hyp.ts),
            "claim_confirmed_price": d.intent.ref_price,
            "claim_confirm_regime": d.hyp.regime.value,
        }
        evidence_index = d.meta.get("evidence_started_index")
        if evidence_index is not None:
            evidence_index = int(evidence_index)
            evidence_bars = (st.frame.mic.bars
                             if extra_meta.get("mic") and st.frame.mic is not None
                             else st.frame.cog.bars)
            if 0 <= evidence_index < len(evidence_bars):
                evidence_bar = evidence_bars[evidence_index]
                lifecycle_meta.update({
                    "claim_evidence_started_ts": int(evidence_bar[0]),
                    "claim_evidence_started_price": float(
                        d.meta.get("event_extreme")
                        if d.meta.get("event_extreme") is not None
                        else evidence_bar[4]),
                })
        if d.kind == "market":
            broker.submit(
                _rekey(d.intent, key), decision_ts=t,
                direction=d.hyp.direction, invalid=d.hyp.invalid,
                target_1=d.hyp.target_1, event_id=d.hyp.event_id,
                meta={"direction": d.hyp.direction,
                      "regime": d.hyp.regime.value,
                      "r_mult": d.hyp.r_mult,
                      "route": d.route,
                      **lifecycle_meta, **d.meta, **extra_meta})
        else:
            wait_h = (int(d.meta.get("max_wait_hours") or n_fill)
                      if d.meta.get("watch_entry") else int(n_fill))
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
                meta={"direction": int(d.hyp.direction),
                      "regime": d.hyp.regime.value,
                      "r_mult": d.hyp.r_mult,
                      "route": d.route,
                      **lifecycle_meta, **d.meta, **extra_meta}))
            counters["limits_armed"] += 1
            if d.meta.get("watch_entry"):
                _increment_counter(counters, "watch_entries_armed")


def _arm_reduce_limit(broker: Broker, st: _CampaignState, intent,
                      decision_ts: int, n_fill: int) -> bool:
    """§8.2 减仓 → 目标(target_1)限价挂单(maker);无仓/无目标/取整 0 ⇒ 市价回退。"""
    if intent.reason and ("partial_exit" in intent.reason):
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
    fill_after = decision_ts + f.tf_hours * MS_1H - f.ms_exec
    broker.submit_limit(RestingLimitOrder(
        direction=direction, limit_price=float(target),
        armed_at_ts=int(decision_ts),
        expiry_ts=fill_after + int(n_fill) * MS_1H,
        fill_after_ts=fill_after,
        symbol=key, role="reduce", reason=intent.reason,
        reduce_qty=qty, qty_frac_snap=qty / pos_acct.open_qty,
        meta={"direction": pos_acct.direction,
              **({"execution_step_ms": f.ms_exec} if f.tf_hours > 1 and f.ms_exec < MS_1H else {})}))
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
