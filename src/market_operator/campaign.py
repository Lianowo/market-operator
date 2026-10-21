"""v6 战役引擎(docs/v6-protocol.md §1):多时间尺度并行战役 + 组合容量。

规范地位:多尺度战役引擎是规范语义链的执行/风险层(唯一,不重命名)。
语义合并后 A/B 臂切换移除,持仓侧接线固化为原臂 B(turtle)口径:
PositionManager(breakeven_on_reduce=True, time_stop=False);路由策略语义
见 strategy_v6 docstring。A/B 判定历史与双臂归档见 research/rounds/v6/。

基础设施(非策略变更):每 (symbol, TF ∈ {1h,2h,4h,12h,1d}) 为一个独立
战役 —— 自有 EventDetector 认知缓存(SymbolCognition,TF 帧)、策略状态
(V6Router 实例)、持仓(PositionManager 实例)、挂单;战役之间除账户权益
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
  决策(V6Router;段内才启用 OPEN)。

组合约束(v6-protocol §1 冻结先验,组合容量):
- 全部战役合计 open 最坏情景 = Σ(各仓 |mark→invalid| 止损实现亏损) +
  Σ(平仓侧 taker+滑点费用预算) + 新仓(止损距离风险 + 双边费用预算)
  ≤ portfolio_cap_frac(3%)× equity_mtm;
- 决策时刻评估(按信号时间 FIFO):超出则**整笔跳过**(不缩量),账本外
  记录 cap_skips(ts/campaign/机制/方向/预算),counters 累计
  "portfolio_cap_skips";成交时刻由 PR#9 已修的 broker 口径
  (portfolio_stop_risk_frac / available_entry_notional,跨品种/跨战役
  杠杆账)做第二层保守钳制(缩量,如实声明)。
- 每战役同时一仓一挂单(槽位纪律,同 v5);E1 优先级撤单不动 §8 减仓单
  (持仓不与入场挂单共存)。

段(§9 同一规则):每战役按自身 TF 帧 split_boundary(n, dev_frac);OPEN
决策仅当决策 bar ∈ 段内;持仓管理全程;交易按 OPEN 决策 bar ∈ 段归属。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .aggregate import TF_HOURS
from .backtest_v6 import (
    MS_15M,
    MS_1H,
    BacktestResult,
    SymbolCognition,
    assert_aligned,
    assert_fill_after_decision,
    reconstruct_trades_v5,
)
from .broker import Broker, OrderIntent, OrderKind, RestingLimitOrder, _floor_step
from .position import PositionManager
from .repairs import prior_moves_delta, range_quantile
from .research import split_boundary
from .strategy_v6 import V6Params, V6Router
from .structure import Params

# v14 施工包(docs/v14-protocol.md;None ⇒ canonical 逐位):续涨/fade 家族表
# 与协议原文一致(E1/E6/E7 续涨;E3/E2/E8 fade;E4/E5 不在两族,按 fill_model
# 兜底),不写死阈值 —— 阈值全在 config/v14.json。
STOP_FAMILY = ("E1", "E6", "E7")
FADE_FAMILY = ("E2", "E3", "E8")


@dataclass(frozen=True)
class ArmConfig:
    """v14 arm 接线(docs/v14-protocol.md §判定;None ⇒ canonical 逐位)。

    fill_model ∈ {"canonical","exchange_maker","virtual_limit"}(C2);stop_entry
    开 C3(续涨家族预埋 stop,超时 stop_wait_tf×TF 作废);ladder_fade 开 C4
    (fade 家族限价拆 ladder_slices 份于 zone 前缘/中/内缘,各 1/slices 风险
    份额,maker 成交,未成交过期/作废);k_bm 为破位参考余量(= Params.k_bm
    冻结先验,config/v14.json 同值照抄,非新先验)。全部默认关(canonical)。

    v15 加性扩展(docs/v15-protocol.md;c2_family=None ⇒ 逐位 v14 行为):
    c2_family 非 None 时 C2 maker 双模式只接管该家族事件(v15 合成件 =
    fade 家族 E2/E3/E8),其余事件(E4/E5 与未匹配 C3 的续涨族)不走 C2,
    落回 canonical 提交路径;阈值协议写死,阈值来源 config/v15.json。

    v16 加性扩展(docs/v16-protocol.md,收割婚配;全 None ⇒ 逐位旧行为):
    harvest_family 非 None 时持仓侧 PositionManager 进入收割婚配 —— 家族
    (fade E2/E3/E8)持仓达对面结构缘全部落袋、禁用 trail;time_cap_bars
    (= k_cap_hours,48×TF 小时认知 bar 根数)非 None 开时间帽:到期且
    MFE < cap_mfe_min(恰界 inclusive 持有)⇒ 出;新开关注入见
    run_campaigns 的 PositionManager 接线,阈值来源 config/v16.json。

    v18 加性扩展(docs/v18-protocol.md §R4;False ⇒ 逐位 v16 收割行为):
    harvest_ladder3=True 时收割模式持仓改三段收割(缘前 25%/50% 各落袋
    1/6 仓,到缘收净;无 trail 无保本;阈值协议写死,config/v18.json 同名
    照抄)。注:R4 三段收割(持仓侧兑现拆段)与 v14 C4 ladder_fade(入场
    限价拆层,沿用 v14 ladder 语义未动)是不同语义词,勿混。

    v19 加性扩展(docs/v19-protocol.md §3;False ⇒ 逐位旧行为):early_exit
    =True 时持仓侧启用夭折处置(半窗 MFE<0.3R / 连续≥3 根收盘全逆出仓,
    阈值协议写死 config/v19.json,数源 position.EARLY_HALF_MFE_R /
    EARLY_CONSEC_N;半窗基数 = time_cap_bars)。

    v20 加性扩展(v20 收尾 S1/S2;全 False ⇒ 逐位旧行为):trail_arm_gate
    =True 时持仓侧 S1 结构脉冲激活 trail(cont 家族 E1/E6/E7 + E4,trail
    扶手只在 MFE≥1R 或新确认顺向 pivot 之后上线,Threshold 数源
    position.S1_TRAIL_ARM_MFE_R / S1_FAMILY);harvest_fade_2leg=True 时持
    仓侧 S2 fade 双格收割(缘前 25% 落袋 40% 仓、缘收剩 60%,数源
    position.S2_LEVEL / S2_BANK_FRAC;与 harvest_ladder3 互斥,R4 死亡名
    单不接线)。决策侧 S3/S4 不在本结构(在 lifecycle.V20Params,经
    router_factory/router_params 注入)。
    """

    fill_model: str = "canonical"
    stop_entry: bool = False
    stop_wait_tf: int = 12
    ladder_fade: bool = False
    ladder_slices: int = 3
    k_bm: float = 0.1
    c2_family: Optional[tuple] = None
    harvest_family: Optional[tuple] = None
    time_cap_bars: Optional[int] = None
    cap_mfe_min: float = 0.5
    harvest_ladder3: bool = False
    early_exit: bool = False
    trail_arm_gate: bool = False
    harvest_fade_2leg: bool = False
    breakeven_on_reduce: bool = True
    reject_opp_lock_frac: float = 0.0


def _lc(counters: dict, key: str, n: int = 1) -> None:
    """v14 惰性计数(仅 arm 路径使用;arm0 计数表与 canonical 逐键一致)。"""
    counters[key] = counters.get(key, 0) + n


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


def build_campaign_frame(cog: SymbolCognition, df_exec: pd.DataFrame,
                         tf_label: str,
                         mic_cog: Optional[object] = None) -> CampaignFrame:
    """装配战役帧:认知 TF bar ⟷ 执行细 bar 对齐(硬约束,违即 raise)。

    认知 bar t 的执行区间 [t, t+TF) 必须由更细帧 bar 无洞组成:逐认知 bar
    断言区间内标签恰落在执行网格上且连续(首/末不完整聚合桶只截头尾,不
    许内部缺根);任一认知 bar 若一个细 bar 都没有 ⇒ 区间无法覆盖 ⇒ raise。
    """
    if tf_label not in TF_HOURS:
        raise ValueError(f"unknown tf_label {tf_label!r}")
    tf_hours = TF_HOURS[tf_label]
    ms_tf = tf_hours * MS_1H
    ms_exec = MS_15M if tf_hours == 1 else MS_1H
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
# 组合容量(3% 最坏情景,FIFO 决策时整笔跳过)
# ---------------------------------------------------------------------------


def _outstanding_worst_case(broker: Broker, tf_filter: Optional[str] = None) -> float:
    """open 最坏情景 Σ(|mark→invalid| 止损亏损 + 平仓侧 taker+滑点预算)。"""
    total = 0.0
    suffix = f"@{tf_filter}" if tf_filter else None
    for sym, pos in broker.positions.items():
        if suffix and not sym.endswith(suffix):
            continue
        mark = broker.last_mark.get(sym, pos.entry)
        stop_loss = pos.qty * max(0.0, pos.direction * (mark - pos.invalid))
        close_fee = pos.qty * mark * (broker.taker_fee + broker.slippage)
        total += stop_loss + close_fee
    return total


def _candidate_worst_case(broker: Broker, entry_ref: float,
                          invalid: float, equity: float) -> float:
    """新仓最坏情景预算(risk_frac sizing ∩ 名义上限,含双边 taker+滑点)。

    不取整 step(略高估 = 保守);ref/invalid 退化 ⇒ inf(拒绝新增风险)。"""
    risk_per_unit = abs(entry_ref - invalid)
    if risk_per_unit <= 0.0 or equity <= 0.0:
        return float("inf")
    eff_rf = broker.current_risk_frac()
    qty = min(eff_rf * equity / risk_per_unit,
              broker._available_entry_notional(equity) / entry_ref)
    if qty <= 0.0:
        return float("inf")
    return qty * risk_per_unit + qty * entry_ref * 2.0 * (
        broker.taker_fee + broker.slippage)


# ---------------------------------------------------------------------------
# 引擎:全部战役合并执行细帧时间轴
# ---------------------------------------------------------------------------


@dataclass
class _CampaignState:
    """运行期战役状态(V6Router + PositionManager + 段界)。"""

    frame: CampaignFrame
    router: V6Router
    pos_mgr: PositionManager
    lo: int
    hi: int


def run_campaigns(frames: dict, *, segment: str, dev_frac: float,
                  equity0: float, risk_frac: float, leverage_cap: float,
                  taker_fee: float, slippage_per_side: float,
                  funding_per_8h: float, cost_multiplier: float,
                  contracts: dict, params: Params, n_fill: int,
                  portfolio_cap_frac: float = 0.03,
                  maker_fee: Optional[float] = None,
                  router_params: Optional[V6Params] = None,
                  router_factory: Optional[type] = None,
                  enable_mic: bool = False,
                  arm: Optional[ArmConfig] = None,
                  mic_families: Optional[tuple] = None,
                  router_params_by_tf: Optional[dict] = None,
                  tf_cap_by_tf: Optional[dict] = None,
                  dynamic_risk_tiers: Optional[list] = None,
                  ) -> BacktestResult:
    """全部 (symbol, TF) 战役合并回放(turtle 唯一语义);主循环与缝见模块
    docstring。A/B 臂切换已随语义合并移除(臂 B 固化,见 strategy_v6 docstring)。

    v8 接插缝(非策略变更):`router_factory` = 可调用 (router_params) → 路由
    实例,默认 None ⇒ V6Router(行为逐位不变);v8 轮传入 V8Router
    (docs/v8-protocol.md,strategy_v8.py),执行/持仓/容量/账目层不变。

    v10 接插缝(docs/v10-protocol.md §A;enable_mic=False ⇒ 逐位旧行为):
    enable_mic=True 要求全部战役挂载 MicCognition;trigger 帧时间轴并入主
    循环 —— mic 事件知时 = trigger bar 收盘、mic 市价 = 次一 trigger bar
    开盘、mic 挂单自次一 trigger bar 可成交;认知收盘决策(bar 事件)、§8
    收割、mark/费用/组合容量口径与粒度不变(纯增量:非 mic 订单的撮合时序
    逐位照旧)。

    v14 施工钩(docs/v14-protocol.md;arm=None ⇒ 逐位旧行为,arm0 与
    canonical 产物数值一致由对照测试钉死):arm 非 None 时,_submit_decisions
    对过闸决策按 _arm_route 分流(C3 stop/C4 分层/C2 maker 双模式);质量层
    (C1 五问/C5 配注)在路由侧(quality.V14Router,经 router_factory 注入)。

    v16 施工钩(docs/v16-protocol.md;arm=None ⇒ 逐位旧行为):arm.harvest_
    family/time_cap_bars 非 None 时持仓侧 PositionManager 进入收割婚配
    (fade 对面结构缘收割全平+禁 trail,时间帽 MFE 恰界 inclusive);三闸/
    定档/fade 前缘改单/结构锚重建在路由侧(harvest.V16Router,经
    router_factory/router_params 注入);提交差分(fade maker / 续涨
    stop-entry)沿用 ArmConfig 的 C2/C3 通道。

    v17 加性钩(docs/v17-directive.md;mic_families=None ⇒ 逐位旧行为):
    mic_families 非 None 时 trigger 帧 mic 事件流按事件族过滤后才入路由
    (arm-C = 仅续涨族触发,fade 归 bar;被过滤事件的频次如实记
    mic_family_filtered,仅该路径新增计数键);arm 非 None 时 mic 决策
    与认知收盘决策过同一 _arm_route 分流(C2 maker / C3 stop-entry,
    阈值=frozen 语境 ATR;arm=None 时与 v13 行为逐位一致:mic 决策走
    canonical 提交)。

    v18 加性钩(docs/v18-protocol.md;全默认 ⇒ 逐位旧行为):路由实现
    set_ctx18 时(repairs.V18Router),每次决策前注入 causal 决策语境
    (rngq/pmd,窗口取路由冻结口径;bar 路径取认知帧,mic 路径 rngq 取
    trigger 帧、pmd 取最近已收认知三根);arm.harvest_ladder3=True 时
    持仓侧三段收割,其 REDUCE 段落市量价提交(不转 _arm_reduce_limit
    的 target_1 maker),段落/缘收计数 v18_r4_partial_banked /
    v18_r4_edge_collect(仅该路径新增计数键)。

    v19 加性钩(docs/v19-protocol.md;全默认 ⇒ 逐位旧行为):注入语境增
    "mic" 标志(tiered.V19Router 的"不 mic-only 处"判定;V18Router 只读
    rngq/pmd 不受影响);arm.early_exit=True 时持仓侧启用夭折处置(半窗
    MFE<0.3R / 连续≥3 逆根,position.py,计数 position 持仓侧 intent
    reason 桶标签 early_half_mfe/early_consec 路径)。v19 默认执行
    (fill_model="canonical" + stop_entry=False 的 arm)不退 _arm_route:
    决策全落 canonical 主路(market 提交),与 arm=None 数值逐位一致。

    v20 加性钩(v20 收尾;全默认 ⇒ 逐位旧行为):arm.trail_arm_gate /
    arm.harvest_fade_2leg 接通持仓侧 S1 扶手闸 / S2 双格收割(position.py);
    S2 段/缘收计数 v20_s2_partial_banked / v20_s2_edge_collect(仅该路径
    新增计数键);决策侧 S3(TRANSITION fade 事件层抑制)与 S4(E3 前缘限
    价改单,落 canonical 限价主路 = E7 G3 同一通道)在 lifecycle.V20Router,
    经 router_factory/router_params 注入,引擎零新增缝。
    """
    if segment not in ("full", "dev", "test"):
        raise ValueError(f"unknown segment {segment}")
    if not (0.0 < portfolio_cap_frac <= 1.0):
        raise ValueError("portfolio_cap_frac must be in (0, 1]")

    keys = sorted(frames)
    base_rp = router_params or V6Params()
    router_cls = router_factory or V6Router
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
                # v16 收割婚配接线(arm=None ⇒ 全 None,逐位 canonical)
                harvest_family=(arm.harvest_family if arm is not None
                                else None),
                time_cap_bars=(arm.time_cap_bars if arm is not None
                               else None),
                cap_mfe_min=(arm.cap_mfe_min if arm is not None else 0.5),
                # v18 R4 三段收割接线(arm=None ⇒ False,逐位 v16 收割)
                harvest_ladder3=(arm.harvest_ladder3 if arm is not None
                                 else False),
                # v19 夭折处置接线(arm=None ⇒ False,逐位旧行为)
                early_exit=(arm.early_exit if arm is not None else False),
                # v20 S1 扶手闸 / S2 双格收割接线(arm=None ⇒ False,逐位旧行为)
                trail_arm_gate=(arm.trail_arm_gate if arm is not None
                                else False),
                harvest_fade_2leg=(arm.harvest_fade_2leg if arm is not None
                                   else False),
                reject_opp_lock_frac=(arm.reject_opp_lock_frac if arm is not None
                                      else 0.0)),
            lo=lo, hi=hi)
    go_set = base_rp.whitelist

    broker = Broker(equity=equity0, risk_frac=risk_frac, leverage_cap=leverage_cap,
                    taker_fee=taker_fee, slippage_per_side=slippage_per_side,
                    funding_per_8h=funding_per_8h,
                    contracts={k: contracts[frames[k].symbol] for k in keys},
                    maker_fee=maker_fee, cost_multiplier=cost_multiplier,
                    portfolio_stop_risk_frac=portfolio_cap_frac,
                    tf_stop_risk_frac_by_tf=tf_cap_by_tf,
                    dynamic_risk_tiers=dynamic_risk_tiers)
    equity_rows: list = []
    cap_skips: list = []
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
                _apply_open_rows(st, broker, key, ts, rows, counters)
                # ①.5 保护性止损:结构失效价 stop-market,逐执行细 bar 检查
                #     (触发即成交于失效价/缺口开盘价;兑现止损位,消除 §8.1
                #     收盘越界→次开盘口径导致的平均亏损 ≈2R 超调)
                stop_row = broker.execute_protective_stop(key, ts, _o, _h, _l)
                if stop_row is not None:
                    _apply_open_rows(st, broker, key, ts, [stop_row], counters)
                    counters["protective_stop_fills"] += 1
                # ② 挂单处理(严格穿过成交;超时撤单/转市价)
                rows = broker.on_15m_bar(key, ts, _o, _h, _l, c)
                _apply_resting_rows(st, broker, key, ts, rows, counters)
                # ③ 持仓路径解决:同根因果序 ①先于② 结构性解决(同 v4 声明)
                # ④ mark-to-market(执行细 bar 收盘;强平保护沿用,预期 0)
                broker.mark(key, ts, c)
                counters["liquidations"] = broker.n_liquidations
                # ⑤ 认知 bar 收盘推进(仅本战役最末执行细 bar)
                for i in f.close_edge.get(ts, ()):
                    _cognition_step(st, broker, i, counters, cap_skips,
                                    seg_bounds[key], go_set, n_fill, arm=arm)
            if enable_mic:
                a = mic_index[key].get(ts)
                if a is not None:
                    _mic_trigger_step(st, broker, a, ts, counters, cap_skips,
                                      seg_bounds[key], go_set, n_fill,
                                      arm=arm, mic_families=mic_families)
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
    res = BacktestResult(segment=segment, cost_multiplier=cost_multiplier,
                         equity0=equity0, ledger=list(broker.ledger),
                         equity_rows=equity_rows,
                         trades=_reconstruct_trades_v6(broker.ledger,
                                                       attr_bounds),
                         counters=counters, seg_bounds=seg_bounds,
                         contracts={s: vars(c) for s, c in contracts.items()},
                         cap_skips=cap_skips,
                         campaign_states={k: {"pos_mgr": states[k].pos_mgr,
                                              "router": states[k].router}
                                          for k in keys})
    assert_fill_after_decision(res.ledger)
    return res


def _apply_open_rows(st: _CampaignState, broker: Broker, key: str, ts: int,
                     rows: list, counters: dict) -> None:
    """on_bar_open 账本行的战役侧处理(①;canonical 与 mic 触发共用)。"""
    f = st.frame
    sym = f.symbol
    for row in rows:
        k = row["kind"]
        if k == "OPEN":
            m = row["meta"]
            st.pos_mgr.open(sym, int(m["direction"]), row["price"],
                            float(m["invalid"]), m.get("target_1"),
                            str(m.get("event_id")), ts,
                            _cog_index_of(f, ts))
            counters["opens_filled"] += 1
            if m.get("entry_mode") == "stop_entry":        # v14 C3 触发成交
                _lc(counters, "stop_entry_fills")
            elif m.get("entry_mode") == "virtual_limit":   # v14 C2 虚拟成交
                _lc(counters, "virtual_entry_fills")
        elif k == "REDUCE":
            # TIMEOUT_TO_TAKER 市价转换(非 deferred 语义:pos_mgr 已在决策时
            # 按 §8.2 立即记账,此处仅同步 broker 侧 stop)
            if broker.position_of(key) is not None \
                    and sym in st.pos_mgr.positions:
                broker.update_protective_invalid(
                    key, st.pos_mgr.positions[sym].invalid)
        elif k in ("EXIT", "LIQUIDATION"):
            st.pos_mgr.positions.pop(sym, None)
            rows_cancel = broker.cancel_resting(
                key, ts, reason_code="POSITION_CLOSED",
                detail="position closed by EXIT/LIQUIDATION; resting "
                       "limit canceled")
            counters["resting_canceled"] += len(rows_cancel)
            for rc in rows_cancel:                    # v14:未成交份作废归因
                ot = rc["meta"].get("order_type")
                if ot is not None:
                    _lc(counters, f"{ot}_canceled")
        elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
            counters["min_size_skips"] += 1


def _apply_resting_rows(st: _CampaignState, broker: Broker, key: str, ts: int,
                        rows: list, counters: dict) -> None:
    """on_15m_bar 账本行的战役侧处理(②;canonical 与 mic 触发共用)。"""
    f = st.frame
    sym = f.symbol
    for row in rows:
        k = row["kind"]
        if k == "LIMIT_FILL":
            if row["meta"].get("role") == "reduce":
                counters["reduce_limit_fills"] += 1
            elif row["meta"].get("ladder_add"):
                # v14 C4 分层合并份:持仓已开张,只入账与计数(不重复 open)
                _lc(counters, "ladder_fills")
                _lc(counters, "ladder_add_fills")
            else:
                counters["limit_fills_entry"] += 1
                m = row["meta"]
                st.pos_mgr.open(sym, int(m["direction"]), row["price"],
                                float(m["invalid"]), m.get("target_1"),
                                str(m.get("event_id")), ts,
                                _cog_index_of(f, ts))
                if m.get("order_type") == "ladder_entry":   # v14 C4 首份
                    _lc(counters, "ladder_fills")
                elif m.get("order_type") == "maker_entry":  # v14 C2 真挂单
                    _lc(counters, "maker_entry_fills")
        elif k == "EXPIRED":
            counters["limit_expired"] += 1
            ot = row["meta"].get("order_type")             # v14:漏单/作废归因
            if ot is not None:
                _lc(counters, f"{ot}_expired")
        elif k == "TRIGGER":                               # v14:价达/穿过触发
            if row["reason_code"] == "STOP_TRIGGER":
                _lc(counters, "stop_entry_triggers")
            elif row["reason_code"] == "VIRTUAL_TRIGGER":
                _lc(counters, "virtual_entry_triggers")
        elif k == "CANCEL" and row["reason_code"] == "TIMEOUT_TO_TAKER":
            counters["reduce_timeout_taker"] += 1
        elif k == "CANCEL":
            ot = row["meta"].get("order_type")             # v14:E1撤单/平仓撤销
            if ot is not None:
                _lc(counters, f"{ot}_canceled")
        elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
            counters["min_size_skips"] += 1
            counters["limit_min_size_skips"] += 1


def _mic_trigger_step(st: _CampaignState, broker: Broker, a: int, ts: int,
                      counters: dict, cap_skips: list, bounds: dict,
                      go_set: frozenset, n_fill: int,
                      arm: Optional[ArmConfig] = None,
                      mic_families: Optional[tuple] = None) -> None:
    """v10 §A trigger bar 收盘推进:①' mic 市价撮合 → ②' mic 挂单 → mic 路由。

    §8 收割/mark/费用计提不在此处(canonical 粒度不变);决策只在已收盘
    trigger bar 上做出,成交严格在后(broker 行内断言)。

    v17 加性(docs/v17-directive.md):mic_families 非 None ⇒ mic 事件流先
    按事件族过滤再入路由(arm-C 仅续涨族,fade 归 bar);arm 非 None ⇒
    mic 决策过同一 _arm_route(C2 maker / C3 stop-entry,阈值 mic 冻结
    语境 ATR)。两参默认 None ⇒ 与 v13 行为逐位一致(canonical 提交)。
    """
    f = st.frame
    key, sym = f.key, f.symbol
    mic = f.mic
    _ts, _o, _h, _l, c, _v = mic.bars[a]
    # ①' mic 延迟单撮合(次一 trigger 开盘;执行帧处为 canonical ① 已兜底的
    # no-op —— mic_only 只撮 meta["mic"] 单)
    _apply_open_rows(st, broker, key, ts,
                     broker.on_bar_open(key, ts, _o, mic_only=True), counters)
    # ②' mic 挂单(自次一 trigger bar 可成交;非 mic 挂单不消耗窗口)
    _apply_resting_rows(st, broker, key, ts,
                        broker.on_15m_bar(key, ts, _o, _h, _l, c, mic_only=True),
                        counters)
    # ⑤' mic 路由(段内才启用 OPEN;白名单/闸门在 router 内部)
    i = mic.cog_index_of[a]
    lo, hi = bounds["lo"], bounds["hi"]
    if not (lo <= i < hi):
        return
    new_events = mic.events_by.get(a)
    if not new_events:
        return
    if mic_families is not None:                     # v17 家族过滤(arm-C)
        kept = [e for e in new_events if e.id in mic_families]
        n_sup = len(new_events) - len(kept)
        if n_sup:
            _lc(counters, "mic_family_filtered", n_sup)
        new_events = kept
        if not new_events:
            return
    counters["go_events"] += sum(
        1 for e in new_events if (e.id, e.direction) in go_set)
    counters["mic_go_events"] += sum(
        1 for e in new_events if (e.id, e.direction) in go_set)
    _inject_ctx18(st, cog_bars=f.cog.bars, i=i,
                  rngq_bars=mic.bars, rngq_i=a)
    decisions = st.router.on_bar(
        sym, mic.bars[a], a, mic.views.get(a), mic.atrs[a], new_events,
        position_open=broker.position_of(key) is not None,
        entry_resting=broker.has_entry_limit(key))
    # mic 挂单可成交窗口 = 本 trigger bar 之后任意更细 bar(小时桶口径与
    # broker 窗口机制一致:fill_after = 本小时桶前一小时,即成交流程自次一
    # trigger bar 起可成交);expiry = 决策时刻 + N_fill 墙钟小时(同 canonical)
    fill_after = (ts - (ts % MS_1H)) - MS_1H
    _submit_decisions(st, broker, decisions, ts, fill_after, ts,
                      {"mic": True, "cog_index": i}, counters, cap_skips,
                      n_fill,
                      cancel_moment=ts + mic.trigger_tf_seconds * 1000,
                      arm=arm, atr=mic.atrs[a])


def _cog_index_of(frame: CampaignFrame, exec_ts: int) -> int:
    """执行细 bar 落回认知 bar 索引(成交 bar ∈ 认知区间断言)。"""
    ms_tf = frame.tf_hours * MS_1H
    bucket = exec_ts - (exec_ts % ms_tf)
    for i, bar in enumerate(frame.cog.bars):
        if int(bar[0]) == bucket:
            return i
    raise AssertionError(f"{frame.key} exec ts {exec_ts} outside cognition")


def _rekey(intent: OrderIntent, key: str) -> OrderIntent:
    """OrderIntent 的 symbol 改为战役复合键(broker 以战役键记账)。"""
    return OrderIntent(kind=intent.kind, symbol=key,
                       qty_frac=intent.qty_frac, reason=intent.reason,
                       ref_price=intent.ref_price, ts=intent.ts)


def _inject_ctx18(st: _CampaignState, *, cog_bars, i: int,
                  rngq_bars=None, rngq_i: Optional[int] = None) -> None:
    """v18 决策语境注入(docs/v18-protocol.md;路由无 set_ctx18 ⇒ no-op,
    canonical/v16/v17 逐位不变)。

    rngq = 事件 bar 波幅在前 rngq_window 根波幅中的分位;bar 路径取认知
    帧自身,mic 路径取 trigger 帧(事件 bar = 发射 trigger bar;pmd 恒取
    最近已收三根认知 bar,窗口冻结口径)。读后即清的责任在路由侧。

    v19 加性(docs/v19-protocol.md §2):ctx 增 "mic" 标志(trigger 收盘决
    策 ⇒ True = "mic-only 处",bar 认知收盘 ⇒ False),供 v19 大蛋糕条款
    "不 mic-only 处"判定;V18Router 只读 rngq/pmd,旧路由语义零改动。
    """
    setter = getattr(st.router, "set_ctx18", None)
    if setter is None:
        return
    wq, wp = st.router.ctx18_windows()
    rb_ = cog_bars if rngq_bars is None else rngq_bars
    ri = i if rngq_i is None else rngq_i
    setter({"rngq": range_quantile(rb_, ri, wq),
            "pmd": prior_moves_delta(cog_bars, i, wp),
            "mic": rngq_bars is not None})


def _cognition_step(st: _CampaignState, broker: Broker, i: int,
                    counters: dict, cap_skips: list, bounds: dict,
                    go_set: frozenset, n_fill: int,
                    arm: Optional[ArmConfig] = None) -> None:
    """认知 bar i 收盘:§8 持仓管理 → v6 路由(段内才启用 OPEN)。"""
    f = st.frame
    key, sym = f.key, f.symbol
    cog = f.cog
    bar = cog.bars[i]
    t = int(bar[0])
    new_events = cog.events_by.get(i, [])
    new_pivots = cog.pivots_by.get(i, [])
    atr = cog.atrs[i]

    # §8 六条(入场成交的认知 bar 不做裁决,同 v4/v5 声明)
    pos_pm = st.pos_mgr.positions.get(sym)
    if pos_pm is not None and pos_pm.opened_ts > bar[0]:
        mgmt = []
    else:
        mgmt = st.pos_mgr.on_bar(sym, bar, atr, new_pivots, new_events)
    if sym in st.pos_mgr.positions and broker.position_of(key) is not None:
        broker.update_protective_invalid(key, st.pos_mgr.positions[sym].invalid)
    for intent in mgmt:
        intent = _rekey(intent, key)
        # v18 R4 / v20 S2:分层收割段落 = REDUCE 市价(次一执行细 bar 开盘,
        # 不转 maker 限价;机器可读桶标签前缀只在对应开关开启时出现)
        r4p = intent.kind is OrderKind.REDUCE and \
            intent.reason.startswith("v16|target_partial|")
        if r4p:
            _lc(counters, "v20_s2_partial_banked"
                if intent.reason.startswith("v16|target_partial|S2|")
                else "v18_r4_partial_banked")
        if intent.kind is OrderKind.EXIT and \
                intent.reason.startswith("v16|target|R4|"):
            _lc(counters, "v18_r4_edge_collect")
        if intent.kind is OrderKind.EXIT and \
                intent.reason.startswith("v16|target|S2|"):
            _lc(counters, "v20_s2_edge_collect")
        armed_r = (intent.kind is OrderKind.REDUCE and not r4p
                   and _arm_reduce_limit(broker, st, intent, t, n_fill))
        if armed_r:
            counters["reduce_limits_armed"] += 1
        else:
            broker.submit(intent, decision_ts=t)

    # v6 路由(仅段内启用;白名单与闸门在 V6Router 内部)
    lo, hi = bounds["lo"], bounds["hi"]
    if not (lo <= i < hi):
        return
    counters["go_events"] += sum(
        1 for e in new_events if (e.id, e.direction) in go_set)
    _inject_ctx18(st, cog_bars=cog.bars, i=i)
    decisions = st.router.on_bar(
        sym, bar, i, cog.views.get(i), atr, new_events,
        position_open=broker.position_of(key) is not None,
        entry_resting=broker.has_entry_limit(key))
    fill_after = t + f.tf_hours * MS_1H - f.ms_exec     # 决策 bar 最末细 bar
    _submit_decisions(st, broker, decisions, t, fill_after, fill_after, {},
                      counters, cap_skips, n_fill,
                      cancel_moment=t + f.tf_hours * MS_1H, arm=arm, atr=atr)


def _submit_decisions(st: _CampaignState, broker: Broker, decisions: list,
                      t: int, fill_after: int, expiry_base: int,
                      extra_meta: dict, counters: dict, cap_skips: list,
                      n_fill: int, cancel_moment: int,
                      arm: Optional[ArmConfig] = None,
                      atr: float = 0.0) -> None:
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

    v14 施工钩子(arm=None ⇒ canonical 逐位;仅 arm 非 None 时生效,全部经由
    _arm_route 分流:C3 stop-entry 预埋 / C4 分层 / C2 maker 双模式,组合容量
    闸与槽位纪律在其前、保持不变)。
    """
    key = st.frame.key
    for d in decisions:
        if d.kind == "cancel_entry":
            for od in broker.resting.get(key, ()):
                if od.armed_at_ts >= cancel_moment:
                    raise RuntimeError(
                        f"E1 cancel moment {cancel_moment} <= armed "
                        f"{od.armed_at_ts} ({key})")
            rows_cancel = broker.cancel_resting(
                key, cancel_moment, reason_code="E1_PRIORITY",
                detail=str(d.meta.get("reason")))
            counters["e1_priority_cancels"] += len(rows_cancel)
            for rc in rows_cancel:                    # v14:未成交份作废归因
                ot = rc["meta"].get("order_type")
                if ot is not None:
                    _lc(counters, f"{ot}_canceled")
            continue
        if d.kind not in ("market", "limit"):
            raise RuntimeError(f"unknown route decision {d.kind}")
        # 组合容量 / 各时间尺度(TF)独立容量池(FIFO:决策时刻整笔跳过,记 reason)
        equity = broker.equity_mtm()
        cand_wc = _candidate_worst_case(broker, d.intent.ref_price,
                                        d.hyp.invalid, equity)
        if broker.tf_stop_risk_frac_by_tf and st.frame.tf_label in broker.tf_stop_risk_frac_by_tf:
            tf_cap = float(broker.tf_stop_risk_frac_by_tf[st.frame.tf_label])
            worst_tf = _outstanding_worst_case(broker, st.frame.tf_label) + cand_wc
            if worst_tf > tf_cap * equity:
                counters["portfolio_cap_skips"] += 1
                cap_skips.append({
                    "ts": t, "campaign": key, "event_id": d.hyp.event_id,
                    "direction": d.hyp.direction, "route": d.route,
                    "reason_code": "TF_CAP",
                    "worst_case": worst_tf, "budget_frac": tf_cap,
                    "equity": equity})
                continue
        if broker.portfolio_stop_risk_frac is not None and not broker.tf_stop_risk_frac_by_tf:
            cap_frac = float(broker.portfolio_stop_risk_frac)
            worst = _outstanding_worst_case(broker) + cand_wc
            if worst > cap_frac * equity:
                counters["portfolio_cap_skips"] += 1
                cap_skips.append({
                    "ts": t, "campaign": key, "event_id": d.hyp.event_id,
                    "direction": d.hyp.direction, "route": d.route,
                    "reason_code": "PORTFOLIO_CAP",
                    "worst_case": worst, "budget_frac": cap_frac,
                    "equity": equity})
                continue
        counters["open_intents"] += 1
        if arm is not None and _arm_route(st, broker, d, arm, t, fill_after,
                                          n_fill, atr, extra_meta, counters):
            continue                                 # v14 路径已接管提交
        if d.kind == "market":
            broker.submit(
                _rekey(d.intent, key), decision_ts=t,
                direction=d.hyp.direction, invalid=d.hyp.invalid,
                target_1=d.hyp.target_1, event_id=d.hyp.event_id,
                meta={"direction": d.hyp.direction,
                      "regime": d.hyp.regime.value,
                      "r_mult": d.hyp.r_mult,
                      "route": d.route,
                      **d.meta, **extra_meta})
        else:
            broker.submit_limit(RestingLimitOrder(
                direction=int(d.hyp.direction),
                limit_price=float(d.limit_price),
                armed_at_ts=t,
                expiry_ts=expiry_base + int(n_fill) * MS_1H,
                fill_after_ts=fill_after,
                symbol=key, role="entry",
                invalid=float(d.hyp.invalid),
                target_1=d.hyp.target_1,
                event_id=d.hyp.event_id, reason=d.intent.reason,
                meta={"direction": int(d.hyp.direction),
                      "regime": d.hyp.regime.value,
                      "r_mult": d.hyp.r_mult,
                      "route": d.route,
                      **d.meta, **extra_meta}))
            counters["limits_armed"] += 1


# ---------------------------------------------------------------------------
# v14 施工包提交分流(docs/v14-protocol.md C2/C3/C4;主路与容量闸不变)
# ---------------------------------------------------------------------------


def _arm_route(st: _CampaignState, broker: Broker, d, arm: ArmConfig,
               t: int, fill_after: int, n_fill: int, atr: float,
               extra_meta: dict, counters: dict) -> bool:
    """v14 arm 语义提交分流;返回 True = 已由 arm 路径接管(主路 continue)。

    分流序 = 家族特异性优先:C3(stop,续涨家族)> C4(分层,fade 家族)>
    C2(maker/virtual 双模式,剩余全部入口 —— E4/E5/未配 C3 时的续涨族与
    未配 C4 时的 fade 族)。组合容量闸在主路已先执行,此处只换新成交语义;
    窗口/武装/撤单因果护栏与现有限价框架同一套(同一 RestingLimitOrder,
    E1 优先级撤单/平仓撤销/超时路径自然覆盖新单种)。
    """
    key = st.frame.key
    f = st.frame
    ev = d.hyp.event_id
    dire = int(d.hyp.direction)
    base_meta = {"direction": dire, "regime": d.hyp.regime.value,
                 "r_mult": d.hyp.r_mult, "route": d.route}
    # C3 续涨 stop-entry 预埋(破位参考价 ± k_bm·ATR;超时 stop_wait_tf×TF 作废)
    if arm.stop_entry and ev in STOP_FAMILY and d.meta.get("ev_ref") is not None:
        ref = float(d.meta["ev_ref"])
        stop_px = ref + dire * arm.k_bm * float(atr)
        broker.submit_limit(RestingLimitOrder(
            direction=dire, limit_price=stop_px, armed_at_ts=t,
            expiry_ts=fill_after + int(arm.stop_wait_tf) * (f.tf_hours * MS_1H),
            fill_after_ts=fill_after, symbol=key, role="entry",
            invalid=float(d.hyp.invalid), target_1=d.hyp.target_1,
            event_id=ev, reason=d.intent.reason, is_stop=True,
            meta={**base_meta, **d.meta, "order_type": "stop_entry",
                  "stop_ref": ref, **extra_meta}))
        _lc(counters, "stop_entry_armed")
        return True
    # C4 fade 家族分层(zone 前缘/中/内缘 三份,各 1/slices 风险份额,maker)
    if arm.ladder_fade and ev in FADE_FAMILY \
            and "zone_lo" in d.meta and "zone_hi" in d.meta:
        zlo, zhi = float(d.meta["zone_lo"]), float(d.meta["zone_hi"])
        mid = (zlo + zhi) / 2.0
        prices = [zlo, mid, zhi] if dire < 0 else [zhi, mid, zlo]
        group = f"{key}#{t}#{ev}"
        slice_mult = float(d.meta.get("size_mult", 1.0) or 1.0) \
            / max(1, int(arm.ladder_slices))
        for j, px in enumerate(prices):
            broker.submit_limit(RestingLimitOrder(
                direction=dire, limit_price=px, armed_at_ts=t,
                expiry_ts=fill_after + int(n_fill) * MS_1H,
                fill_after_ts=fill_after, symbol=key, role="entry",
                invalid=float(d.hyp.invalid), target_1=d.hyp.target_1,
                event_id=ev, reason=d.intent.reason, ladder_group=group,
                meta={**base_meta, **d.meta, "order_type": "ladder_entry",
                      "ladder_slice": j, "size_mult": slice_mult,
                      **extra_meta}))
            _lc(counters, "ladder_armed")
        _lc(counters, "ladder_signals")
        return True
    # C2 maker 双模式(市价决策 → 决策收盘参考价挂单;弱 E7 前缘限价 → 同模式)
    # v15:c2_family 非 None 时仅接管该家族事件,其余落回 canonical 主路。
    if arm.fill_model in ("exchange_maker", "virtual_limit") \
            and (arm.c2_family is None or ev in arm.c2_family):
        fm = "virtual" if arm.fill_model == "virtual_limit" else "exchange"
        ot = "virtual_entry" if fm == "virtual" else "maker_entry"
        px = float(d.limit_price) if d.kind == "limit" \
            else float(d.intent.ref_price)
        broker.submit_limit(RestingLimitOrder(
            direction=dire, limit_price=px, armed_at_ts=t,
            expiry_ts=fill_after + int(n_fill) * MS_1H,
            fill_after_ts=fill_after, symbol=key, role="entry",
            invalid=float(d.hyp.invalid), target_1=d.hyp.target_1,
            event_id=ev, reason=d.intent.reason, fill_model=fm,
            meta={**base_meta, **d.meta, "order_type": ot, **extra_meta}))
        counters["limits_armed"] += 1
        _lc(counters, f"{ot}_armed")
        return True
    return False


def _arm_reduce_limit(broker: Broker, st: _CampaignState, intent,
                      decision_ts: int, n_fill: int) -> bool:
    """§8.2 减仓 → 目标(target_1)限价挂单(maker);无仓/无目标/取整 0 ⇒ 市价回退。"""
    f = st.frame
    key, sym = f.key, f.symbol
    pos_pm = st.pos_mgr.positions.get(sym)
    pos_acct = broker.position_of(key)
    target = pos_pm.target_1 if pos_pm is not None else None
    if pos_acct is None or target is None:
        return False
    spec = broker.contracts[key]
    qty = _floor_step(float(intent.qty_frac or 0.0) * pos_acct.open_qty,
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
        meta={"direction": pos_acct.direction}))
    return True


# ---------------------------------------------------------------------------
# 交易重构(共享账本 → 逐笔;含 v5 路由标签追溯 + campaign/tf/symbol 标注)
# ---------------------------------------------------------------------------


def _reconstruct_trades_v6(ledger: list, seg_bounds: dict) -> list:
    trades = reconstruct_trades_v5(ledger, seg_bounds)
    for t in trades:
        key = t["symbol"]                    # reconstruct 以复合键为 symbol
        sym, tf = parse_key(key)
        t["campaign"] = key
        t["symbol"] = sym
        t["tf"] = tf
    trades.sort(key=lambda x: (x["open_ts"] or 0, x["campaign"]))
    return trades


__all__ = [
    "ArmConfig", "CampaignFrame", "build_campaign_frame", "run_campaigns",
    "campaign_key", "parse_key", "STOP_FAMILY", "FADE_FAMILY",
]
