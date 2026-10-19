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
from .research import split_boundary
from .strategy_v6 import V6Params, V6Router
from .structure import Params


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


def build_campaign_frame(cog: SymbolCognition, df_exec: pd.DataFrame,
                         tf_label: str) -> CampaignFrame:
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
    return CampaignFrame(
        key=campaign_key(cog.symbol, tf_label), symbol=cog.symbol,
        tf_label=tf_label, tf_hours=tf_hours, ms_exec=ms_exec, cog=cog,
        exec_bars=exec_bars, exec_labels=exec_labels, close_edge=close_edge,
        exec_index_of=exec_index_of)


# ---------------------------------------------------------------------------
# 组合容量(3% 最坏情景,FIFO 决策时整笔跳过)
# ---------------------------------------------------------------------------


def _outstanding_worst_case(broker: Broker) -> float:
    """open 最坏情景 Σ(|mark→invalid| 止损亏损 + 平仓侧 taker+滑点预算)。"""
    total = 0.0
    for sym, pos in broker.positions.items():
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
    qty = min(broker.risk_frac * equity / risk_per_unit,
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
                  ) -> BacktestResult:
    """全部 (symbol, TF) 战役合并回放(turtle 唯一语义);主循环与缝见模块
    docstring。A/B 臂切换已随语义合并移除(臂 B 固化,见 strategy_v6 docstring)。

    v8 接插缝(非策略变更):`router_factory` = 可调用 (router_params) → 路由
    实例,默认 None ⇒ V6Router(行为逐位不变);v8 轮传入 V8Router
    (docs/v8-protocol.md,strategy_v8.py),执行/持仓/容量/账目层不变。
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
        states[key] = _CampaignState(
            frame=f, router=router_cls(base_rp),
            pos_mgr=PositionManager(
                params, allow_add=False,
                breakeven_on_reduce=True,    # turtle 固化(原臂 B 接线)
                time_stop=False),            # §8.6 关闭(原臂 B 接线)
            lo=lo, hi=hi)
    go_set = base_rp.whitelist

    broker = Broker(equity=equity0, risk_frac=risk_frac, leverage_cap=leverage_cap,
                    taker_fee=taker_fee, slippage_per_side=slippage_per_side,
                    funding_per_8h=funding_per_8h,
                    contracts={k: contracts[frames[k].symbol] for k in keys},
                    maker_fee=maker_fee, cost_multiplier=cost_multiplier,
                    portfolio_stop_risk_frac=portfolio_cap_frac)
    equity_rows: list = []
    cap_skips: list = []
    counters = {"go_events": 0, "open_intents": 0, "opens_filled": 0,
                "min_size_skips": 0, "liquidations": 0,
                "limits_armed": 0, "limit_fills_entry": 0, "limit_expired": 0,
                "limit_min_size_skips": 0,
                "reduce_limits_armed": 0, "reduce_limit_fills": 0,
                "reduce_timeout_taker": 0, "resting_canceled": 0,
                "e1_priority_cancels": 0, "portfolio_cap_skips": 0}

    fine_ts = sorted({t for k in keys for t in frames[k].exec_labels})

    for ts in fine_ts:
        for key in keys:                       # 同 ts 按 campaign key 字典序
            st = states[key]
            f = st.frame
            q = f.exec_bars.get(ts)
            if q is None:
                continue
            sym = f.symbol
            _ts, _o, _h, _l, c, _v = q
            # ① 开盘撮合(认知决策 ⇒ 下一执行细 bar 开盘成交)
            for row in broker.on_bar_open(key, ts, _o):
                k = row["kind"]
                if k == "OPEN":
                    m = row["meta"]
                    st.pos_mgr.open(sym, int(m["direction"]), row["price"],
                                    float(m["invalid"]), m.get("target_1"),
                                    str(m.get("event_id")), ts,
                                    _cog_index_of(f, ts))
                    counters["opens_filled"] += 1
                elif k == "REDUCE":
                    # TIMEOUT_TO_TAKER 市价转换(非 deferred 语义:pos_mgr 已
                    # 在决策时按 §8.2 立即记账,此处仅同步 broker 侧 stop)
                    if broker.position_of(key) is not None \
                            and sym in st.pos_mgr.positions:
                        broker.update_protective_invalid(
                            key, st.pos_mgr.positions[sym].invalid)
                elif k in ("EXIT", "LIQUIDATION"):
                    st.pos_mgr.positions.pop(sym, None)
                    counters["resting_canceled"] += len(broker.cancel_resting(
                        key, ts, reason_code="POSITION_CLOSED",
                        detail="position closed by EXIT/LIQUIDATION; resting "
                               "limit canceled"))
                elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
                    counters["min_size_skips"] += 1
            # ② 挂单处理(严格穿过成交;超时撤单/转市价)
            for row in broker.on_15m_bar(key, ts, _o, _h, _l, c):
                k = row["kind"]
                if k == "LIMIT_FILL":
                    if row["meta"].get("role") == "reduce":
                        counters["reduce_limit_fills"] += 1
                    else:
                        counters["limit_fills_entry"] += 1
                        m = row["meta"]
                        st.pos_mgr.open(sym, int(m["direction"]), row["price"],
                                        float(m["invalid"]), m.get("target_1"),
                                        str(m.get("event_id")), ts,
                                        _cog_index_of(f, ts))
                elif k == "EXPIRED":
                    counters["limit_expired"] += 1
                elif k == "CANCEL" and row["reason_code"] == "TIMEOUT_TO_TAKER":
                    counters["reduce_timeout_taker"] += 1
                elif k == "SKIP" and row["reason_code"] == "MIN_SIZE":
                    counters["min_size_skips"] += 1
                    counters["limit_min_size_skips"] += 1
            # ③ 持仓路径解决:同根因果序 ①先于② 结构性解决(同 v4 声明)
            # ④ mark-to-market(执行细 bar 收盘;强平保护沿用,预期 0)
            broker.mark(key, ts, c)
            counters["liquidations"] = broker.n_liquidations
            # ⑤ 认知 bar 收盘推进(仅本战役最末执行细 bar)
            for i in f.close_edge.get(ts, ()):
                _cognition_step(st, broker, i, counters, cap_skips,
                                seg_bounds[key], go_set, n_fill)
        equity_rows.append((ts, broker.equity_mtm(), broker.cash,
                            broker.unrealized(), broker.funding_accrued(),
                            len(broker.positions)))

    per_campaign: dict = {}
    for key in keys:
        per_campaign[key] = dict(states[key].router.counters)
        for k, v in states[key].router.counters.items():
            counters[k] = counters.get(k, 0) + v
    counters["campaigns"] = per_campaign
    res = BacktestResult(segment=segment, cost_multiplier=cost_multiplier,
                         equity0=equity0, ledger=list(broker.ledger),
                         equity_rows=equity_rows,
                         trades=_reconstruct_trades_v6(broker.ledger, seg_bounds),
                         counters=counters, seg_bounds=seg_bounds,
                         contracts={s: vars(c) for s, c in contracts.items()},
                         cap_skips=cap_skips,
                         campaign_states={k: {"pos_mgr": states[k].pos_mgr,
                                              "router": states[k].router}
                                          for k in keys})
    assert_fill_after_decision(res.ledger)
    return res


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


def _cognition_step(st: _CampaignState, broker: Broker, i: int,
                    counters: dict, cap_skips: list, bounds: dict,
                    go_set: frozenset, n_fill: int) -> None:
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
        armed_r = (intent.kind is OrderKind.REDUCE
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
    decisions = st.router.on_bar(
        sym, bar, i, cog.views.get(i), atr, new_events,
        position_open=broker.position_of(key) is not None,
        entry_resting=broker.has_entry_limit(key))
    fill_after = t + f.tf_hours * MS_1H - f.ms_exec     # 决策 bar 最末细 bar
    for d in decisions:
        if d.kind == "cancel_entry":
            for od in broker.resting.get(key, ()):
                if od.armed_at_ts >= t:
                    raise RuntimeError(
                        f"E1 cancel ts {t} <= armed {od.armed_at_ts} "
                        f"({key} bar {i})")
            counters["e1_priority_cancels"] += len(
                broker.cancel_resting(
                    key, t, reason_code="E1_PRIORITY",
                    detail=str(d.meta.get("reason"))))
            continue
        if d.kind not in ("market", "limit"):
            raise RuntimeError(f"unknown route decision {d.kind}")
        # 组合容量(3% 最坏情景,FIFO:决策时刻整笔跳过,记 reason)
        equity = broker.equity_mtm()
        cap_frac = float(broker.portfolio_stop_risk_frac)
        worst = (_outstanding_worst_case(broker)
                 + _candidate_worst_case(broker, d.intent.ref_price,
                                         d.hyp.invalid, equity))
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
        if d.kind == "market":
            broker.submit(
                _rekey(d.intent, key), decision_ts=t,
                direction=d.hyp.direction, invalid=d.hyp.invalid,
                target_1=d.hyp.target_1, event_id=d.hyp.event_id,
                meta={"direction": d.hyp.direction,
                      "regime": d.hyp.regime.value,
                      "r_mult": d.hyp.r_mult,
                      "route": d.route,
                      **d.meta})
        else:
            broker.submit_limit(RestingLimitOrder(
                direction=int(d.hyp.direction),
                limit_price=float(d.limit_price),
                armed_at_ts=t,
                expiry_ts=fill_after + int(n_fill) * MS_1H,
                fill_after_ts=fill_after,
                symbol=key, role="entry",
                invalid=float(d.hyp.invalid),
                target_1=d.hyp.target_1,
                event_id=d.hyp.event_id, reason=d.intent.reason,
                meta={"direction": int(d.hyp.direction),
                      "regime": d.hyp.regime.value,
                      "r_mult": d.hyp.r_mult,
                      "route": d.route,
                      **d.meta}))
            counters["limits_armed"] += 1


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
    "CampaignFrame", "build_campaign_frame", "run_campaigns",
    "campaign_key", "parse_key",
]
