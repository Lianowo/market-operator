"""阶段 5 模拟经纪商:撮合、费用、资金费、账本、权益、保证金与强平约束。

成本模型 = docs/methodology.md §9.2 先验 × cost_multiplier;资金模型 = 阶段 5
规格(初始权益 300 USDT、risk_frac 风险定仓、leverage_cap 名义上限、交易所
最小下单量约束)。

时间约定(正确性硬约束的实现基础):
- 决策在 bar t 收盘做出,账本记 decision_ts = 决策 bar 的 ts(本数据 ts =
  bar 开盘毫秒,bar 以开盘时刻为标签);
- 成交固定在下一根该品种 bar 开盘(§7/§8:决策在收盘,执行在下一根开盘),
  fill_ts = 成交 bar 的 ts;
- 因 bar 标签严格递增,恒有 fill_ts > decision_ts;_fill 中显式断言,
  任一成交 ts ≤ 决策 ts 立即 raise(无 epsilon、无伪造时间)。

成本(每边,cost_multiplier 同时乘 fee+slippage+funding):
- 费用:fee = notional·taker_fee·m;
- 滑点:成交价 = open·(1 ± slippage_per_side·m),对成交方向不利
  (买入更贵,卖出更廉);
- 资金费:持仓期间按 notional·funding_per_8h·m 每 8h 计提,方向无关、双边
  恒为成本(不预测资金费方向);按持仓时长精确到毫秒比例记
  (accrue = qty·mark·rate·m·dt/(8h)),持仓中逐 bar 计提,平仓(全部)时
  一次结清累计;REDUCE 部分平仓不结算,随余仓最终 EXIT 结清。

sizing(规格):qty = risk_frac·equity / |entry − invalid|;notional 上限 =
leverage_cap·equity(超上限按 cap·equity/entry 缩量);向下取整至合约
size step;qty < sizeMin 或 notional < minTradeUSDT ⇒ 拒绝,账本记
SKIP(reason_code=MIN_SIZE);同 symbol 已有仓位 ⇒ OPEN 拒绝,记
SKIP(DUP_POSITION)。equity 取最近一次 mark-to-market 权益
(realized_cash + Σ未实现盈亏 − Σ已计提未结算资金费)。used_leverage =
notional/equity 记录于 OPEN 账本行(恒 ≤ leverage_cap)。

保证金/强平(近似,风控失效保护;正常交易不应触发):mark-to-market 时若某仓
 |浮亏| ≥ liq_frac·equity_mtm(0.9),对该仓入队强制市价平仓(与其余订单同一
撮合路径,下一根开盘成交),账本 reason_code=LIQUIDATION,n_liquidations
计数,报告中断言 0 次。

账本(ledger):逐笔成交一行:ts/symbol/side(buy|sell)/kind/qty/price/
open_price/fee/funding_delta/realized_pnl/decision_ts/reason_code/reason/
trade_id/used_leverage/slippage_cost/cash_after(+meta JSON:事件 id/regime/
r_mult/invalid/target/risk_usdt);SKIP/LIQUIDATION 同表记录(SKIP 无成交,
qty/price 为该意图当时参考或 None)。funding_delta 约定为现金影响(结算时
为负);realized_pnl 为纯价格盈亏(费用与资金费单列,权益对账:
cash = equity0 + Σ(realized_pnl − fee + funding_delta))。

合约规格:fetch_contract_specs 抓 Bitget 公开接口 v2 mix/market/contracts
(productType=USDT-FUTURES),取 minTradeNum(sizeMin)、volumePlace
(sizePlace)、sizeMultiplier(size step)、pricePlace、minTradeUSDT,
缓存 data/contracts.json(含抓取时间与各原始条目 sha256);离线或抓取失败
回退读缓存。实时规格与 2024-2026 历史可能存在最小下单量差异,报告如实声明。

v4 增量(docs/v4-protocol.md §1-G3,执行路径细化;不改变任何既有市价路径):
- RestingLimitOrder(挂单):{direction, limit_price, armed_at_ts, expiry_ts
  (= armed + n_fill×1h,最后一根允许成交的 1h bar 标签),maker=True}。
  武装时刻 = 1h 决策 bar 标签(与 v1/v2 decision_ts 同约定);15m bar ts 的
  可成交窗口 = 其所属 1h bar 标签 ∈ (armed_at_ts, expiry_ts]。
- 成交规则(保守):仅当 15m bar **严格穿过**限价才算成交(sell limit L:
  bar.high > L;buy limit L:bar.low < L),持平不成交 —— 不假设排队靠前
  (协议如实声明的残余乐观风险);穿过后按 limit_price 成交(穿透缺口不给
  价格改善,保守如实声明),无滑点;成交 ts = 该 15m bar 标签,恒 > 武装 ts
  (因果:首根可成交 15m bar 即决策小时之后的第一根,行内 assert)。
- 费用非对称:挂单成交 = maker(默认 maker_fee=taker_fee,不影响 v1/v2 构造;
  v4 传入 maker_fee=0.0002);cost_multiplier 同时乘 maker/taker/slippage/funding。
- 超时:入场挂单超时 ⇒ 撤单,kind=EXPIRED(没有这笔交易);目标减仓挂单
  (armed 时快照数量)超时 ⇒ kind=CANCEL(reason_code=TIMEOUT_TO_TAKER)
  并自动转 1h 决策语义的市价 REDUCE(taker+滑点,下一根 15m 开盘成交)。
- 仓位平仓(EXIT/LIQUIDATION 成交)后,该 symbol 残余挂单由调用方
  cancel_resting 撤销 ⇒ kind=CANCEL(reason_code=POSITION_CLOSED)。
- 账本新增 kind:LIMIT_FILL(挂单成交,maker 费、slippage_cost=0,
  meta 携带 entry_mode='limit'/limit_price/fill_wait_bars(成交时已过的
  可成交 15m bar 数))/EXPIRED/CANCEL;资金费口径不变(dt 毫秒比例)。
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import requests

from . import capacity


class OrderKind(str, Enum):
    OPEN = "OPEN"
    REDUCE = "REDUCE"
    ADD = "ADD"
    EXIT = "EXIT"


@dataclass(frozen=True)
class OrderIntent:
    """下单意图(与 broker 对接,字段固定)。qty_frac 为仓位名义额占权益的
    比例建议;OPEN 时为 None,由 sizing 模块决定。reason 为 "code|detail"。
    ref_price 为决策时点参考价(事件 bar 收盘;实际成交在下一 bar 开盘)。

    规范家园:v1 起定义于 strategy.py;语义合并后订单意图词表固化为执行
    边界(broker)的规范类型,全链(策略/持仓/回放引擎)统一从这里导入。
    """

    kind: OrderKind
    symbol: str
    qty_frac: Optional[float]
    reason: str
    ref_price: float
    ts: object


CONTRACTS_URL = "https://api.bitget.com/api/v2/mix/market/contracts"
PRODUCT_TYPE = "USDT-FUTURES"
ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_PATH = ROOT / "data" / "contracts.json"

MS_8H = 8 * 3_600_000


# ---------------------------------------------------------------------------
# 合约规格(交易所实际字段映射,minTradeNum/volumePlace/sizeMultiplier/…)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractSpec:
    """单品种下单约束(Bitget USDT 永续)。"""

    symbol: str
    size_min: float        # minTradeNum
    size_step: float       # sizeMultiplier
    size_place: int        # volumePlace
    price_place: int       # pricePlace
    min_trade_usdt: float  # minTradeUSDT

    @classmethod
    def from_entry(cls, entry: dict) -> "ContractSpec":
        return cls(
            symbol=str(entry["symbol"]),
            size_min=float(entry["minTradeNum"]),
            size_step=float(entry["sizeMultiplier"]),
            size_place=int(entry["volumePlace"]),
            price_place=int(entry["pricePlace"]),
            min_trade_usdt=float(entry["minTradeUSDT"]),
        )


def _entry_sha(entry: dict) -> str:
    return hashlib.sha256(
        json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()


def fetch_contract_specs(symbols: list[str],
                         cache_path: Path = CONTRACTS_PATH,
                         timeout: int = 20) -> tuple[dict, str]:
    """抓 Bitget 合约规格并缓存;失败回退缓存。返回 (specs, source_desc)。

    source_desc ∈ {"live", "cache"} 加说明,供报告如实声明规格近似。
    """
    try:
        resp = requests.get(CONTRACTS_URL, params={"productType": PRODUCT_TYPE},
                            timeout=timeout)
        body = resp.json()
        if resp.status_code != 200 or body.get("code") != "00000":
            raise RuntimeError(f"bitget {resp.status_code} {body.get('code')}")
        data = body.get("data") or []
        by_sym = {d["symbol"]: d for d in data}
        missing = [s for s in symbols if s not in by_sym]
        if missing:
            raise RuntimeError(f"contracts missing symbols: {missing}")
        entries = {}
        specs = {}
        for s in symbols:
            raw = by_sym[s]
            entries[s] = {"sha256": _entry_sha(raw), "raw": raw}
            specs[s] = ContractSpec.from_entry(raw)
        payload = {
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_url": CONTRACTS_URL, "product_type": PRODUCT_TYPE,
            "entries": entries,
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
        return specs, "live"
    except Exception as e:  # noqa: BLE001 离线/接口异常一律回退缓存并如实标记
        if not cache_path.exists():
            raise RuntimeError(
                f"contracts fetch failed ({e}) and no cache at {cache_path}") from e
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        specs = {}
        for s in symbols:
            ent = payload["entries"][s]
            if _entry_sha(ent["raw"]) != ent["sha256"]:
                raise RuntimeError(f"contracts cache sha256 mismatch for {s}")
            specs[s] = ContractSpec.from_entry(ent["raw"])
        return specs, f"cache(fetch failed: {e}; cached_at={payload['fetched_at_utc']})"


# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    """待成交订单(决策 bar 收盘产生,下一根 bar 开盘成交)。"""

    intent: OrderIntent
    decision_ts: int
    direction: Optional[int] = None      # OPEN 必填(+1/-1)
    invalid: Optional[float] = None      # OPEN 必填(sizing 与保护性校验)
    target_1: Optional[float] = None
    event_id: Optional[str] = None
    meta: dict = field(default_factory=dict)


MS_1H = 3_600_000


@dataclass
class RestingLimitOrder:
    """v4 挂单(docs/v4-protocol.md §1-G3):1h 收盘武装,15m 严格穿过成交。

    协议字段:direction/limit_price/armed_at_ts/expiry_ts(= armed+N_fill×1h)/maker。
    armed_at_ts = 武装的 1h 决策 bar 标签(= decision_ts);expiry_ts = 最后
    一根允许成交的 1h bar 标签;15m bar ts 可成交 ⇔ ts//1h·1h ∈ (armed, expiry]。
    wait_bars = 已度过的可成交 15m bar 数(成交时 = fill_wait_bars)。
    """

    direction: int                 # 挂单方向:+1 buy limit / -1 sell limit
    limit_price: float
    armed_at_ts: int               # 武装(1h 决策)bar 标签
    expiry_ts: int                 # armed_at_ts + n_fill×1h(最后一根允许成交的 1h bar)
    maker: bool = True             # 挂单成交恒为 maker(如实声明)
    # v6 增量(docs/v6-protocol.md §1,加性):认知帧 TF ≥ 2h 时,决策 TF bar 的
    # 更细执行 bar 全部落在 [armed, armed+TF) —— 决策须早于成交 ⇒ 这些执行 bar
    # 不可成交。fill_after_ts = 最后一个不可成交的执行 bar 标签(决策 TF bar 最
    # 末细 bar);None ⇒ 沿用 armed_at_ts(v4/v5 行为逐位不变)。
    fill_after_ts: Optional[int] = None
    symbol: str = ""
    role: str = "entry"            # "entry"(G3 入场)/ "reduce"(§8.2 目标减仓)
    # entry 侧(open 成交所需,与 _Pending OPEN 字段同语义)
    invalid: Optional[float] = None
    target_1: Optional[float] = None
    event_id: Optional[str] = None
    meta: dict = field(default_factory=dict)
    reason: str = ""
    # reduce 侧(武装时对 open_qty 向 step 下取整的快照;超时转市价同数量)
    reduce_qty: Optional[float] = None
    qty_frac_snap: Optional[float] = None
    wait_bars: int = 0             # 已度过的可成交 15m bar 数
    # v14 增量(docs/v14-protocol.md C2/C3,加性,默认值 ⇒ 逐位旧行为):
    # fill_model="exchange" 即现有真挂单口径(严格穿过、maker 费);
    # "virtual"=代码内虚拟挂单:可成交窗口内价达(含持平)即触发,按次一执行
    # 细 bar 开盘 taker+滑点成交(保证成交、守序差一档 —— 真挂单于穿过时刻
    # 限价成交,虚拟单晚一档及其后漂移如实承担);
    # is_stop=True 为 C3 续涨 stop-entry 预埋:窗口内严格穿过触发(穿过即),
    # 次一执行细 bar 开盘 taker+滑点成交,超时作废(EXPIRED,漏单如实统计);
    # ladder_group 非 None 为 C4 分层组号:同组后续份在同战役持仓上按 maker
    # 限价合并加仓(加权均价、同 trade_id、份间风险份额=size_mult 口径)。
    fill_model: str = "exchange"
    is_stop: bool = False
    ladder_group: Optional[str] = None


@dataclass
class _AcctPosition:
    """经纪商侧仓位(数量、加权均价、计提资金费)。"""

    symbol: str
    direction: int
    qty: float                 # 当前数量
    open_qty: float            # OPEN 成交数量(qty_frac 换算基准)
    entry: float               # 加权均价(实际滑点成交价)
    trade_id: str
    opened_ts: int             # OPEN 成交 bar ts
    funding_acc: float = 0.0   # 已计提未结算资金费(正数 = 成本)
    last_funding_ts: int = 0   # 上次计提时刻
    invalid: Optional[float] = None  # 当前结构失效位置;组合风险预算的真实输入
    ladder_group: Optional[str] = None  # v14 C4 分层组号(见 RestingLimitOrder)
    decision_ts: int = 0       # OPEN 决策 bar 标签(保护性止损的因果锚,< fill ts)


def _normalize_funding_table(table: Optional[dict]) -> Optional[dict]:
    """``{sym: [(ts, rate), ...]}`` → ``{sym: (times, rates)}``(升序,供二分)。

    A1 接插缝:表里缺某标的 ⇒ 该标的回落到常数近似(不静默改变口径,见
    :meth:`Broker._accrue_funding`)。
    """
    if not table:
        return None
    out: dict = {}
    for sym, rows in table.items():
        items = sorted((int(ts), float(rate)) for ts, rate in rows)
        out[str(sym)] = (tuple(ts for ts, _ in items),
                         tuple(rate for _, rate in items))
    return out


class Broker:
    """多品种共享同一权益的模拟经纪商(合并时间轴回放,见 backtest.py)。"""

    def __init__(self, equity: float = 300.0, *, risk_frac: float,
                 leverage_cap: float, taker_fee: float, slippage_per_side: float,
                 funding_per_8h: float, contracts: dict,
                 maker_fee: Optional[float] = None,
                 cost_multiplier: float = 1.0, liq_frac: float = 0.9,
                 portfolio_stop_risk_frac: Optional[float] = None,
                 tf_stop_risk_frac_by_tf: Optional[dict] = None,
                 dynamic_risk_tiers: Optional[list] = None,
                 capacity_mode: str = capacity.ADMISSION_FULL_COST,
                 funding_rates: Optional[dict] = None,
                 slippage_by_symbol: Optional[dict] = None,
                 symbol_stop_risk_frac: Optional[dict] = None,
                 reserve_by_tf: Optional[dict] = None) -> None:
        if cost_multiplier <= 0.0:
            raise ValueError("cost_multiplier must be positive")
        self.initial_equity = float(equity)
        self.cash = float(equity)             # realized:权益扣已实现部分
        self.risk_frac = float(risk_frac)
        self.leverage_cap = float(leverage_cap)
        self.taker_fee = float(taker_fee) * cost_multiplier
        # maker 费率(v4 非对称成本);缺省 = taker(v1/v2 构造与行为不变)
        self.maker_fee = (self.taker_fee if maker_fee is None
                          else float(maker_fee) * cost_multiplier)
        self.slippage = float(slippage_per_side) * cost_multiplier
        self.funding_8h = float(funding_per_8h) * cost_multiplier
        self.cost_multiplier = float(cost_multiplier)
        self.contracts = contracts
        self.liq_frac = float(liq_frac)
        if portfolio_stop_risk_frac is not None and not (0 < portfolio_stop_risk_frac <= 1):
            raise ValueError("portfolio_stop_risk_frac must be in (0,1]")
        self.portfolio_stop_risk_frac = portfolio_stop_risk_frac
        self.tf_stop_risk_frac_by_tf = (
            {str(k): float(v) for k, v in tf_stop_risk_frac_by_tf.items()}
            if tf_stop_risk_frac_by_tf else None
        )
        self.dynamic_risk_tiers = (
            [(float(t[0]), float(t[1])) for t in dynamic_risk_tiers]
            if dynamic_risk_tiers else None
        )
        # 风险预算的唯一口径(见 capacity 模块):决策门与成交裁剪共用
        # B1 标的池 / B2 慢周期保留额度:缺省空 ⇒ 逐位旧行为
        self.reserve_by_tf = ({str(k): float(v) for k, v in reserve_by_tf.items()}
                              if reserve_by_tf else None)
        self.risk_budget = capacity.RiskBudget(
            portfolio_frac=portfolio_stop_risk_frac,
            per_timeframe_frac=self.tf_stop_risk_frac_by_tf or {},
            symbol_frac=({str(k): float(v)
                          for k, v in symbol_stop_risk_frac.items()}
                         if symbol_stop_risk_frac else {}),
            mode=capacity_mode)
        # A1 真实资金费率表:{symbol: [(ts, rate), ...]};None ⇒ 常数近似
        self.funding_rates = _normalize_funding_table(funding_rates)
        # E1 分标的滑点:{symbol: 每边比例};缺省 = 全局 slippage
        self.slippage_by_symbol = ({str(k): float(v) * cost_multiplier
                                    for k, v in slippage_by_symbol.items()}
                                   if slippage_by_symbol else {})

        self.positions: dict[str, _AcctPosition] = {}
        self.pending: dict[str, list[_Pending]] = {}
        self.resting: dict[str, list[RestingLimitOrder]] = {}   # v4 挂单
        self.ledger: list[dict] = []
        self.last_mark: dict[str, float] = {}          # symbol → 最近 mark 价
        self.n_liquidations = 0                        # 强平触发次数(报告断言 0)
        self.n_skips: dict[str, int] = {}              # reason_code → 次数
        self._seq = 0

    # -- 下单(决策 bar 收盘) -------------------------------------------------

    def submit(self, intent: OrderIntent, decision_ts: int, *,
               direction: Optional[int] = None, invalid: Optional[float] = None,
               target_1: Optional[float] = None, event_id: Optional[str] = None,
               meta: Optional[dict] = None) -> None:
        if intent.kind is OrderKind.OPEN:
            if direction not in (+1, -1) or invalid is None:
                raise ValueError("OPEN requires direction and invalid")
        po = _Pending(intent=intent, decision_ts=int(decision_ts),
                      direction=direction, invalid=invalid, target_1=target_1,
                      event_id=event_id, meta=dict(meta or {}))
        self.pending.setdefault(intent.symbol, []).append(po)

    # -- v4 挂单(docs/v4-protocol.md §1-G3) ------------------------------------

    def submit_limit(self, order: RestingLimitOrder) -> None:
        """武装一只挂单(1h 决策 bar 收盘)。expiry_ts 由调用方 = armed+n_fill×1h。"""
        if order.direction not in (+1, -1):
            raise ValueError("RestingLimitOrder.direction must be ±1")
        if order.expiry_ts <= order.armed_at_ts:
            raise ValueError("expiry_ts must be > armed_at_ts")
        if order.role == "entry" and order.invalid is None:
            raise ValueError("entry limit requires invalid")
        if order.role == "reduce" and order.reduce_qty is None:
            raise ValueError("reduce limit requires reduce_qty snapshot")
        self.resting.setdefault(order.symbol, []).append(order)

    def has_entry_limit(self, symbol: str) -> bool:
        return any(o.role == "entry" for o in self.resting.get(symbol, ()))

    def on_15m_bar(self, symbol: str, ts: int, o: float, h: float, l: float,
                   c: float, *, mic_only: bool = False) -> list[dict]:
        """15m bar 上的挂单处理:先判超时,再判严格穿过成交(FIFO)。

        可成交窗口 = 本 15m bar 所属 1h bar 标签 ∈ (fill_after_ts(缺省 =
        armed_at_ts), expiry_ts];决策 bar 自身(及其全部细 bar)不参与(因果:
        决策须早于成交)。v6:认知 TF ≥ 2h 的战役在执行帧(1h)上调用本方法,
        fill_after_ts 把决策 TF bar 的全部执行细 bar 排除出成交窗口。

        v10 增量(docs/v10-protocol.md §A,加性,默认逐位不变):mic_only=True
        只处理 meta["mic"] 为真的挂单(mic 武装 ⇒ 自次一 trigger bar 可成交),
        非 mic 挂单原样保留、不消耗 wait_bars(canonical 窗口/超时口径不变)。
        v13 修补(docs/v13-protocol.md,级联 trigger 可粗于执行帧):mic_only
        =False 时对称排除 mic 挂单(原样保留、不消耗 wait_bars)—— 否则中间
        执行细 bar 会先于 mic 决策收盘消耗其窗口(级联下构成前馈;v10 15m/5m
        trigger 恒细于执行帧,同 ts 同价,数值不变)。
        """
        hour_label = int(ts) - (int(ts) % MS_1H)
        rows: list[dict] = []
        kept: list[RestingLimitOrder] = []
        for od in self.resting.pop(symbol, []):
            if bool(od.meta.get("mic")) != bool(mic_only):
                kept.append(od)
                continue
            if hour_label > od.expiry_ts:
                rows.append(self._timeout(od, int(ts)))
                continue
            barred = (od.fill_after_ts if od.fill_after_ts is not None
                      else od.armed_at_ts)
            if hour_label <= barred:
                kept.append(od)                       # 决策 bar 覆盖区间,不可成交
                continue
            od.wait_bars += 1
            if od.is_stop:
                # v14 C3 stop-entry:严格穿过触发(穿过即;持平不触发,同既有
                # 保守口径),触发后转次一执行细 bar 开盘 taker 成交
                trig = (h > od.limit_price) if od.direction > 0 \
                    else (l < od.limit_price)
            elif od.fill_model == "virtual":
                # v14 C2 虚拟挂单:价达(含持平)触发,次细 bar 开盘 taker 成交
                trig = (l <= od.limit_price) if od.direction > 0 \
                    else (h >= od.limit_price)
            else:
                trig = None
            if trig is not None:
                if trig:
                    if int(ts) <= od.armed_at_ts:     # 因果硬断言(同 limit fill)
                        raise AssertionError(
                            f"trigger ts {ts} <= armed_at_ts {od.armed_at_ts}")
                    rows.append(self._trigger_to_pending(od, int(ts)))
                    continue
                kept.append(od)
                continue
            cross = (h > od.limit_price) if od.direction < 0 \
                else (l < od.limit_price)
            if not cross:                             # 严格穿过;持平不成交
                kept.append(od)
                continue
            if int(ts) <= od.armed_at_ts:             # 因果硬断言:成交晚于武装
                raise AssertionError(
                    f"limit fill ts {ts} <= armed_at_ts {od.armed_at_ts}")
            row = (self._fill_open_limit(od, int(ts)) if od.role == "entry"
                   else self._fill_reduce_limit(od, int(ts)))
            rows.append(row)
        if kept:
            self.resting[symbol] = kept
        return rows

    def _timeout(self, od: RestingLimitOrder, ts: int) -> dict:
        """超时:entry ⇒ EXPIRED(撤单,没有这笔交易);
        reduce ⇒ CANCEL(TIMEOUT_TO_TAKER)并转同数量市价 REDUCE(taker+滑点,
        下一根 15m 开盘成交,决策 ts = 检出超时的本 15m bar)。"""
        if od.role == "reduce":
            row = self._admin_row(od, ts, kind="CANCEL",
                                  reason_code="TIMEOUT_TO_TAKER",
                                  detail=f"reduce limit {od.limit_price:.8g} not "
                                         f"filled within window; convert to taker "
                                         f"market REDUCE ({od.wait_bars} 15m bars)")
            intent = OrderIntent(
                kind=OrderKind.REDUCE, symbol=od.symbol,
                qty_frac=od.qty_frac_snap,
                reason=(f"TIMEOUT_TO_TAKER|reduce limit {od.limit_price:.8g} "
                        f"expired after {od.wait_bars} 15m bars "
                        f"(armed {od.armed_at_ts}, expiry {od.expiry_ts}); "
                        f"taker market at next open"),
                ref_price=od.limit_price, ts=ts)
            self.pending.setdefault(od.symbol, []).append(_Pending(
                intent=intent, decision_ts=ts,
                direction=-od.direction,
                meta={**od.meta, "role": "deferred_reduce"}))
            return row
        return self._admin_row(od, ts, kind="EXPIRED", reason_code="EXPIRED",
                               detail=f"entry limit {od.limit_price:.8g} not filled "
                                      f"within N_fill 1h bars ({od.wait_bars} 15m "
                                      f"bars waited); no trade")

    def _trigger_to_pending(self, od: RestingLimitOrder, ts: int) -> dict:
        """v14 触发单(virtual 价达 / stop-entry 穿过)→ 次一执行细 bar 开盘
        taker 成交。

        触发即把挂单转正为待成交 OPEN(决策 ts = 触发的执行细 bar 标签,恒
        < 成交 bar,on_bar_open 行内断言);sizing/失效校验与市价 OPEN 同路
        (taker 费 + 滑点,"费用=taker+滑点"的协议口径)。本行只记账本事实
        (kind=TRIGGER,无成交、无费用;成交由 _fill_open 在下一细 bar 写)。
        """
        mode = "stop_entry" if od.is_stop else "virtual_limit"
        intent = OrderIntent(
            kind=OrderKind.OPEN, symbol=od.symbol, qty_frac=None,
            reason=(f"{'STOP_TRIGGER' if od.is_stop else 'VIRTUAL_TRIGGER'}|"
                    f"{mode} {od.limit_price:.8g} triggered at fine bar "
                    f"(armed {od.armed_at_ts}, waited {od.wait_bars}); "
                    f"taker market at next fine open"),
            ref_price=od.limit_price, ts=ts)
        self.pending.setdefault(od.symbol, []).append(_Pending(
            intent=intent, decision_ts=int(ts), direction=od.direction,
            invalid=od.invalid, target_1=od.target_1, event_id=od.event_id,
            meta={**od.meta, "entry_mode": mode,
                  "limit_price": od.limit_price, "fill_wait_bars": od.wait_bars,
                  "trigger_ts": int(ts), "armed_at_ts": int(od.armed_at_ts),
                  "ladder_group": od.ladder_group}))
        return self._admin_row(
            od, ts, kind="TRIGGER",
            reason_code=("STOP_TRIGGER" if od.is_stop else "VIRTUAL_TRIGGER"),
            detail=f"{mode} {od.limit_price:.8g} triggered "
                   f"({od.wait_bars} fine bars waited); fill at next fine open")

    def _admin_row(self, od: RestingLimitOrder, ts: int, *, kind: str,
                   reason_code: str, detail: str) -> dict:
        """EXPIRED/CANCEL/TRIGGER 账本行(无成交;fee/pnl 全 0;ts > armed 由
        on_15m_bar 窗口保证,cancel_resting 由调用方保证)。"""
        if ts <= od.armed_at_ts:
            raise AssertionError(
                f"{kind} ts {ts} <= armed_at_ts {od.armed_at_ts}")
        row = {
            "seq": self._next_seq_row(), "ts": int(ts), "symbol": od.symbol,
            "side": None, "kind": kind, "qty": 0.0, "price": od.limit_price,
            "open_price": None, "fee": 0.0, "funding_delta": 0.0,
            "realized_pnl": 0.0, "decision_ts": int(od.armed_at_ts),
            "reason_code": reason_code, "reason": f"{reason_code}|{detail}",
            "trade_id": None, "used_leverage": None, "slippage_cost": 0.0,
            "cash_after": self.cash,
            "meta": {**od.meta, "limit_price": od.limit_price, "role": od.role,
                     "fill_wait_bars": od.wait_bars,
                     "expiry_ts": od.expiry_ts,
                     **({"ladder_group": od.ladder_group}
                        if od.ladder_group is not None else {})},
        }
        self.ledger.append(row)
        return row

    def cancel_resting(self, symbol: str, ts: int, *, reason_code: str,
                       detail: str) -> list[dict]:
        """撤销该 symbol 全部残余挂单(仓位平仓后由调用方触发)。"""
        rows = [self._admin_row(od, int(ts), kind="CANCEL",
                                reason_code=reason_code, detail=detail)
                for od in self.resting.pop(symbol, [])]
        return rows

    def _gross_notional(self) -> float:
        """当前全部品种未平仓名义敞口(多空均取绝对值,不得互相抵扣)。"""
        return sum(abs(pos.qty * self.last_mark.get(sym, pos.entry))
                   for sym, pos in self.positions.items())

    def _available_entry_notional(self, equity: float) -> float:
        """账户级剩余总敞口;防止每个 symbol 各自使用完整 leverage_cap。"""
        return max(0.0, self.leverage_cap * equity - self._gross_notional())

    def update_protective_invalid(self, symbol: str, invalid: float) -> None:
        """同步已确认市场结构失效位置;只允许收紧,不可用未成交意图提前移动。"""
        pos = self.positions[symbol]
        if not math.isfinite(invalid) or invalid <= 0:
            raise ValueError("invalid protective price")
        prev = pos.invalid
        if prev is not None and pos.direction * (invalid - prev) < -1e-10:
            raise ValueError("stop may not be loosened")
        pos.invalid = float(invalid)

    def current_risk_frac(self) -> float:
        """基于已落袋本金(self.cash)的阶梯动态单笔风险率;未配置时返回静态 self.risk_frac。"""
        if not self.dynamic_risk_tiers:
            return self.risk_frac
        realized = self.cash
        for max_cash, r_frac in self.dynamic_risk_tiers:
            if realized <= max_cash:
                return r_frac
        return self.dynamic_risk_tiers[-1][1]

    # -- 风险预算:唯一口径见 capacity 模块 ---------------------------------
    def _budget_headroom(self, equity: float, symbol: Optional[str], *,
                         moment: str) -> tuple[float, str, float]:
        """各生效池剩余额度的最小值:``(剩余, 最紧池名, 该池上限占比)``。"""
        headroom, name, frac = capacity.budget_headroom(
            self.risk_budget.pools(capacity.timeframe_of(symbol), moment=moment,
                                   symbol=capacity.symbol_root(symbol)),
            equity, self.positions, self.last_mark,
            taker_fee=self.taker_fee, slippage=self.slippage,
            include_costs=self.risk_budget.includes_costs(moment),
            reserve_by_tf=self.reserve_by_tf,
            candidate_timeframe=capacity.timeframe_of(symbol))
        if math.isfinite(headroom):
            headroom = max(0.0, headroom)
        return headroom, name, frac

    def available_risk_budget(self, equity: float,
                              symbol: Optional[str] = None) -> float:
        """成交时刻仍可占用的最坏情景额度;``inf`` = 未配置容量上限。"""
        return self._budget_headroom(equity, symbol, moment=capacity.FILL)[0]

    def _available_stop_risk(self, equity: float,
                             symbol: Optional[str] = None) -> float:
        """历史接缝名:冻结诊断脚本在此 monkeypatch,语义 = 成交时刻剩余预算。"""
        return self.available_risk_budget(equity, symbol)

    def capacity_usage(self, equity: float, symbol: Optional[str] = None, *,
                       moment: str = capacity.DECISION
                       ) -> tuple[float, float, float, str]:
        """``(上限占比, 已占用, 剩余, 最紧池名)``;未配置上限时剩余为 inf。"""
        if not self.risk_budget.is_active:
            return 0.0, 0.0, math.inf, ""
        headroom, name, frac = self._budget_headroom(equity, symbol,
                                                     moment=moment)
        return frac, frac * equity - headroom, headroom, name

    def qty_within_risk_budget(self, equity: float, price: float,
                               risk_per_unit: float,
                               symbol: Optional[str] = None) -> float:
        """按成交时刻口径反解数量(与决策门同一分母,故两边同解)。"""
        return capacity.qty_for_budget(
            self._available_stop_risk(equity, symbol), price, risk_per_unit,
            taker_fee=self.taker_fee, slippage=self.slippage,
            include_costs=self.risk_budget.includes_costs(capacity.FILL))

    def qty_for_entry(self, equity: float, price: float, stop: Optional[float],
                      symbol: Optional[str] = None, *, size_mult: float = 1.0,
                      use_budget: bool = True) -> float:
        """统一建仓数量 = 单笔风险 ∩ 账户名义上限 (∩ 风险预算),未取整。"""
        risk_per_unit = abs(price - stop) if stop is not None else 0.0
        if risk_per_unit <= 0.0 or equity <= 0.0:
            return 0.0
        qty = min(self.current_risk_frac() * size_mult * equity / risk_per_unit,
                  self._available_entry_notional(equity) / price)
        if use_budget:
            qty = min(qty, self.qty_within_risk_budget(equity, price,
                                                       risk_per_unit, symbol))
        return max(0.0, qty)

    def _fill_open_limit(self, od: RestingLimitOrder, ts: int) -> dict:
        """入场挂单成交:limit_price 精确成交(无滑点),maker 费,sizing 同 OPEN。"""
        sym = od.symbol
        spec = self.contracts[sym]
        d = od.direction
        side = "buy" if d > 0 else "sell"
        price = od.limit_price
        ref = {"armed_at_ts": od.armed_at_ts, "intent_reason": od.reason}
        if sym in self.positions:
            pos_cur = self.positions[sym]
            if od.ladder_group is not None \
                    and pos_cur.ladder_group == od.ladder_group \
                    and pos_cur.direction == d:
                return self._fill_ladder_add(od, pos_cur, ts)
            return self._limit_skip(od, ts, "DUP_POSITION",
                                    f"position already open for {sym}", ref)
        if d * (price - od.invalid) <= 0.0:
            return self._limit_skip(od, ts, "BAD_INVALID",
                                    f"limit {price} not protective vs "
                                    f"invalid={od.invalid}", ref)
        equity = self.equity_mtm()
        risk_per_unit = abs(price - od.invalid)
        # v14 C5/C4:size_mult(quality tier × 1/切份数;缺省 1.0 ⇒ 逐位旧口径)
        size_mult = float(od.meta.get("size_mult", 1.0) or 1.0)
        qty = _floor_step(self.qty_for_entry(equity, price, od.invalid, sym,
                                             size_mult=size_mult),
                          spec.size_step, spec.size_place)
        notional = qty * price
        if qty < spec.size_min or notional < spec.min_trade_usdt:
            return self._limit_skip(
                od, ts, "MIN_SIZE",
                f"qty={qty} < size_min={spec.size_min} or notional={notional:.4f} "
                f"< min_trade_usdt={spec.min_trade_usdt} (limit fill)", ref)
        fee = notional * self.maker_fee
        self.cash -= fee
        self._seq += 1
        trade_id = f"{sym}#{self._seq}"
        pos = _AcctPosition(symbol=sym, direction=d, qty=qty, open_qty=qty,
                            entry=price, trade_id=trade_id, opened_ts=ts,
                            last_funding_ts=ts, invalid=float(od.invalid),
                            ladder_group=od.ladder_group,
                            decision_ts=int(od.armed_at_ts))
        self.positions[sym] = pos
        self.last_mark[sym] = price
        meta = dict(od.meta)
        meta.update({"invalid": od.invalid, "target_1": od.target_1,
                     "event_id": od.event_id, "risk_usdt": qty * risk_per_unit,
                     "entry_mode": "limit", "limit_price": od.limit_price,
                     "fill_wait_bars": od.wait_bars,
                     **({"ladder_group": od.ladder_group}
                        if od.ladder_group is not None else {})})
        row = {
            "seq": self._next_seq_row(), "ts": int(ts), "symbol": sym,
            "side": side, "kind": "LIMIT_FILL", "qty": qty, "price": price,
            "open_price": None, "fee": fee, "funding_delta": 0.0,
            "realized_pnl": 0.0, "decision_ts": int(od.armed_at_ts),
            "reason_code": od.reason.split("|", 1)[0] if od.reason else "G3",
            "reason": od.reason, "trade_id": trade_id,
            "used_leverage": notional / equity, "slippage_cost": 0.0,
            "cash_after": self.cash, "meta": meta,
        }
        self.ledger.append(row)
        return row

    def _fill_ladder_add(self, od: RestingLimitOrder, pos: _AcctPosition,
                         ts: int) -> dict:
        """v14 C4 分层合并加仓:同组后续份在同战役持仓上按 maker 限价并入。

        加权均价入账、同 trade_id(一笔交易)、invalid 不变(只收紧闸门不
        在此触发);sizing 同 _fill_open_limit(risk_frac×size_mult 份额,
        组合钳制不动);账本 LIMIT_FILL(role=entry,ladder_add=True)。
        """
        sym = od.symbol
        spec = self.contracts[sym]
        d = od.direction
        price = od.limit_price
        ref = {"armed_at_ts": od.armed_at_ts, "intent_reason": od.reason}
        if d * (price - pos.invalid) <= 0.0:
            return self._limit_skip(od, ts, "BAD_INVALID",
                                    f"ladder add {price} not protective vs "
                                    f"invalid={pos.invalid}", ref)
        equity = self.equity_mtm()
        risk_per_unit = abs(price - pos.invalid)
        size_mult = float(od.meta.get("size_mult", 1.0) or 1.0)
        qty = _floor_step(self.qty_for_entry(equity, price, pos.invalid, sym,
                                             size_mult=size_mult),
                          spec.size_step, spec.size_place)
        if qty < spec.size_min or qty * price < spec.min_trade_usdt:
            return self._limit_skip(
                od, ts, "MIN_SIZE",
                f"ladder add qty={qty} < size_min={spec.size_min} "
                f"(limit fill)", ref)
        fee = qty * price * self.maker_fee
        self.cash -= fee
        pos.entry = (pos.qty * pos.entry + qty * price) / (pos.qty + qty)
        pos.qty += qty
        self.last_mark[sym] = price
        meta = dict(od.meta)
        meta.update({"invalid": pos.invalid, "target_1": od.target_1,
                     "event_id": od.event_id, "risk_usdt": qty * risk_per_unit,
                     "entry_mode": "limit", "limit_price": od.limit_price,
                     "fill_wait_bars": od.wait_bars, "role": "entry",
                     "ladder_add": True, "ladder_group": od.ladder_group})
        row = {
            "seq": self._next_seq_row(), "ts": int(ts), "symbol": sym,
            "side": "buy" if d > 0 else "sell", "kind": "LIMIT_FILL",
            "qty": qty, "price": price, "open_price": None, "fee": fee,
            "funding_delta": 0.0, "realized_pnl": 0.0,
            "decision_ts": int(od.armed_at_ts),
            "reason_code": od.reason.split("|", 1)[0] if od.reason else "G3",
            "reason": od.reason, "trade_id": pos.trade_id,
            "used_leverage": qty * price / equity, "slippage_cost": 0.0,
            "cash_after": self.cash, "meta": meta,
        }
        self.ledger.append(row)
        return row

    def _fill_reduce_limit(self, od: RestingLimitOrder, ts: int) -> dict:
        """目标减仓挂单成交(部分平仓,maker 费);仍随最终 EXIT 结算资金费。"""
        sym = od.symbol
        pos = self.positions.get(sym)
        ref = {"armed_at_ts": od.armed_at_ts, "intent_reason": od.reason}
        if pos is None:
            return self._limit_skip(od, ts, "NO_POSITION",
                                    f"no open position for {sym}", ref)
        qty = min(float(od.reduce_qty), pos.qty)
        if qty <= 0.0:
            return self._limit_skip(od, ts, "MIN_SIZE",
                                    "reduce limit qty rounds to 0", ref)
        side = "sell" if pos.direction > 0 else "buy"
        price = od.limit_price
        self._accrue_funding(pos, price, ts)
        pnl = pos.direction * (price - pos.entry) * qty
        fee = qty * price * self.maker_fee
        full = qty >= pos.qty
        funding_delta = 0.0
        if full:
            funding_delta = -pos.funding_acc
            self.cash -= pos.funding_acc
            pos.funding_acc = 0.0
        self.cash += pnl - fee
        pos.qty -= qty
        if full:
            self.positions.pop(sym, None)
        self.last_mark[sym] = price
        meta = dict(od.meta)
        meta.update({"role": "reduce", "limit_price": od.limit_price,
                     "fill_wait_bars": od.wait_bars})
        row = {
            "seq": self._next_seq_row(), "ts": int(ts), "symbol": sym,
            "side": side, "kind": "LIMIT_FILL", "qty": qty, "price": price,
            "open_price": None, "fee": fee, "funding_delta": funding_delta,
            "realized_pnl": pnl, "decision_ts": int(od.armed_at_ts),
            "reason_code": od.reason.split("|", 1)[0] if od.reason else "§8.2",
            "reason": od.reason, "trade_id": pos.trade_id,
            "used_leverage": None, "slippage_cost": 0.0,
            "cash_after": self.cash, "meta": meta,
        }
        self.ledger.append(row)
        return row

    def _limit_skip(self, od: RestingLimitOrder, ts: int, code: str,
                    detail: str, ref: dict) -> dict:
        """挂单成交时刻的拒绝路径(MIN_SIZE/DUP/BAD_INVALID/NO_POSITION)。"""
        self.n_skips[code] = self.n_skips.get(code, 0) + 1
        row = {
            "seq": self._next_seq_row(), "ts": int(ts),
            "symbol": od.symbol, "side": None, "kind": "SKIP",
            "qty": 0.0, "price": od.limit_price, "open_price": None, "fee": 0.0,
            "funding_delta": 0.0, "realized_pnl": 0.0,
            "decision_ts": int(od.armed_at_ts), "reason_code": code,
            "reason": f"{code}|{detail} (order: {od.reason})",
            "trade_id": None, "used_leverage": None, "slippage_cost": 0.0,
            "cash_after": self.cash,
            "meta": {**od.meta, "limit_price": od.limit_price, "role": od.role,
                     **ref},
        }
        self.ledger.append(row)
        return row

    # -- 撮合(成交 bar 开盘) --------------------------------------------------

    def on_bar_open(self, symbol: str, ts: int, open_price: float, *,
                    mic_only: bool = False) -> list[dict]:
        """撮合该 symbol 全部待成交订单(按提交先后),返回本 bar 账本行。

        v10 增量(docs/v10-protocol.md §A,加性,默认逐位不变):mic_only=True
        只撮合 meta["mic"] 为真的延迟单(mic 决策 ⇒ 下一 trigger bar 开盘),
        非 mic 单原样保留待本战役执行帧撮合(canonical 时序不受影响)。
        v13 修补(级联 trigger 可粗于执行帧):mic_only=False 时对称排除 mic
        延迟单 —— 否则中间执行细 bar 会揪出 mic 单早于其决策收盘成交(级联
        下构成前馈;v10 15m/5m trigger 恒细于执行帧,mic 单本就只在其 trigger
        标签处到达,两个路径同 ts 同价,数值不变)。
        """
        orders = self.pending.pop(symbol, [])
        rows: list[dict] = []
        kept = [po for po in orders
                if bool(po.meta.get("mic")) != bool(mic_only)]
        orders = [po for po in orders
                  if bool(po.meta.get("mic")) == bool(mic_only)]
        if kept:
            self.pending[symbol] = kept + self.pending.get(symbol, [])
        for po in orders:
            if ts <= po.decision_ts:
                raise AssertionError(
                    f"fill ts {ts} <= decision ts {po.decision_ts} "
                    f"({symbol} {po.intent.kind})")
            if po.intent.kind is OrderKind.OPEN:
                row = self._fill_open(po, ts, open_price)
            elif po.intent.kind is OrderKind.ADD:
                row = self._fill_add(po, ts, open_price)
            else:  # REDUCE / EXIT
                row = self._fill_close(po, ts, open_price)
            rows.append(row)
            self.ledger.append(row)
        return rows

    def _slip_for(self, symbol: Optional[str]) -> float:
        """该标的每边滑点(E1):分标的表 > 全局 ``slippage``。"""
        if symbol and self.slippage_by_symbol:
            root = capacity.symbol_root(symbol)
            if root in self.slippage_by_symbol:
                return self.slippage_by_symbol[root]
        return self.slippage

    def _slipped(self, price: float, side: str,
                 symbol: Optional[str] = None) -> float:
        """对成交方向不利的滑点(买入更贵,卖出更廉)。"""
        slip = self._slip_for(symbol)
        return price * (1.0 + slip) if side == "buy" \
            else price * (1.0 - slip)

    def _fill_open(self, po: _Pending, ts: int, o: float) -> dict:
        sym = po.intent.symbol
        spec = self.contracts[sym]
        d = po.direction
        side = "buy" if d > 0 else "sell"
        price = self._slipped(o, side, sym)

        if sym in self.positions:
            return self._skip_row(po, ts, "DUP_POSITION",
                                  f"position already open for {sym}", price=price)
        if d * (price - po.invalid) <= 0.0:
            return self._skip_row(po, ts, "BAD_INVALID",
                                  f"fill {price} not protective vs "
                                  f"invalid={po.invalid}", price=price)

        equity = self.equity_mtm()
        risk_per_unit = abs(price - po.invalid)
        # v14 C5:size_mult(quality tier;缺省 1.0 ⇒ 逐位旧口径)
        size_mult = float(po.meta.get("size_mult", 1.0) or 1.0)
        qty = _floor_step(self.qty_for_entry(equity, price, po.invalid, sym,
                                             size_mult=size_mult),
                          spec.size_step, spec.size_place)
        notional = qty * price
        if qty < spec.size_min or notional < spec.min_trade_usdt:
            return self._skip_row(po, ts, "MIN_SIZE",
                                  f"qty={qty} < size_min={spec.size_min} or "
                                  f"notional={notional:.4f} < "
                                  f"min_trade_usdt={spec.min_trade_usdt}",
                                  price=price)

        fee = notional * self.taker_fee
        self.cash -= fee
        self._seq += 1
        trade_id = f"{sym}#{self._seq}"
        pos = _AcctPosition(symbol=sym, direction=d, qty=qty, open_qty=qty,
                            entry=price, trade_id=trade_id, opened_ts=ts,
                            last_funding_ts=ts, invalid=float(po.invalid),
                            ladder_group=po.meta.get("ladder_group"),
                            decision_ts=po.decision_ts)
        self.positions[sym] = pos
        self.last_mark[sym] = price
        meta = dict(po.meta)
        meta.update({"invalid": po.invalid, "target_1": po.target_1,
                     "event_id": po.event_id, "risk_usdt": qty * risk_per_unit})
        return self._row(po, ts, sym, side, "OPEN", qty, price, o, fee,
                         0.0, 0.0, trade_id,
                         used_leverage=notional / equity, meta=meta)

    def _fill_close(self, po: _Pending, ts: int, o: float) -> dict:
        sym = po.intent.symbol
        pos = self.positions.get(sym)
        side_close_of = {OrderKind.REDUCE: True, OrderKind.EXIT: True}
        assert po.intent.kind in side_close_of
        if pos is None:
            return self._skip_row(po, ts, "NO_POSITION",
                                  f"no open position for {sym}")
        if po.intent.kind is OrderKind.EXIT or po.intent.qty_frac is None:
            qty = pos.qty                              # 全平(含 LIQUIDATION)
        else:
            qty = min(_floor_step(po.intent.qty_frac * pos.open_qty,
                                  self.contracts[sym].size_step,
                                  self.contracts[sym].size_place), pos.qty)
        if qty <= 0.0:
            return self._skip_row(po, ts, "MIN_SIZE", "close qty rounds to 0")
        return self._close_qty(po, pos, qty, ts, o,
                               full=(qty >= pos.qty or
                                     po.intent.kind is OrderKind.EXIT))

    def _fill_add(self, po: _Pending, ts: int, o: float) -> dict:
        sym = po.intent.symbol
        pos = self.positions.get(sym)
        if pos is None:
            return self._skip_row(po, ts, "NO_POSITION",
                                  f"no open position for {sym}")
        side = "buy" if pos.direction > 0 else "sell"
        price = self._slipped(o, side, sym)
        spec = self.contracts[sym]
        requested = (po.intent.qty_frac or 0.0) * pos.open_qty
        equity = self.equity_mtm()
        cap_qty = self._available_entry_notional(equity) / price
        # 加仓与建仓共用同一预算。admission_full_cost 复现现行口径(仅在配置了
        # 账户池时钳制,且只按账户池);full_cost / stop_risk 让周期池同样生效。
        if self.risk_budget.mode == capacity.ADMISSION_FULL_COST:
            constrained = self.portfolio_stop_risk_frac is not None
            scope = None
        else:
            constrained = self.risk_budget.is_active
            scope = sym
        if constrained:
            if pos.invalid is None or pos.direction * (price - pos.invalid) <= 0:
                return self._skip_row(po, ts, "BAD_INVALID", "add without protective stop")
            risk_per_unit = pos.direction * (price - pos.invalid)
            cap_qty = min(cap_qty, self.qty_within_risk_budget(
                equity, price, risk_per_unit, scope))


        qty = _floor_step(min(requested, cap_qty), spec.size_step, spec.size_place)
        if qty < spec.size_min or qty * price < spec.min_trade_usdt:
            return self._skip_row(po, ts, "MIN_SIZE",
                                  "add exceeds portfolio gross-leverage budget "
                                  "or exchange minimum")
        new_invalid = pos.invalid
        if po.meta.get("role") == "deferred_add":
            candidate = po.meta.get("add_invalid")
            if candidate is None or new_invalid is None:
                return self._skip_row(po, ts, "BAD_INVALID",
                                      "deferred add missing structural stop")
            candidate = float(candidate)
            new_invalid = (min(new_invalid, candidate) if pos.direction < 0
                           else max(new_invalid, candidate))
            if pos.direction * (price - new_invalid) <= 0.0:
                return self._skip_row(po, ts, "BAD_INVALID",
                                      "actual add fill beyond protective stop")
            # Decision-time approval is not sufficient: gap/slippage can turn
            # the real fill into a risk-increasing addition. Reject at fill.
            before = pos.direction * (pos.entry - pos.invalid) * pos.qty
            after = (pos.direction * (pos.entry - new_invalid) * pos.qty
                     + pos.direction * (price - new_invalid) * qty)
            if after > before + 1e-9:
                return self._skip_row(po, ts, "ADD_RISK",
                                      "actual fill violates risk-neutral add")
        fee = qty * price * self.taker_fee
        self.cash -= fee
        pos.entry = (pos.qty * pos.entry + qty * price) / (pos.qty + qty)
        pos.qty += qty
        if new_invalid is not None:
            pos.invalid = new_invalid
        self.last_mark[sym] = price
        return self._row(po, ts, sym, side, "ADD", qty, price, o, fee,
                         0.0, 0.0, pos.trade_id, meta=dict(po.meta))

    def _close_qty(self, po: _Pending, pos: _AcctPosition, qty: float,
                   ts: int, o: float, full: bool) -> dict:
        sym = pos.symbol
        side = "sell" if pos.direction > 0 else "buy"
        price = self._slipped(o, side, sym)
        # 资金费计提至本笔成交刻(精确到毫秒比例),再决定是否结算
        self._accrue_funding(pos, price, ts)
        pnl = pos.direction * (price - pos.entry) * qty
        fee = qty * price * self.taker_fee
        funding_delta = 0.0
        if full:
            funding_delta = -pos.funding_acc            # 平仓时结算累计(§规格)
            self.cash -= pos.funding_acc
            pos.funding_acc = 0.0
        self.cash += pnl - fee
        pos.qty -= qty
        if full:
            self.positions.pop(sym, None)
        self.last_mark[sym] = price
        kind = ("LIQUIDATION" if po.meta.get("liquidation")
                else po.intent.kind.value)
        return self._row(po, ts, sym, side, kind, qty, price, o, fee,
                         funding_delta, pnl, pos.trade_id, meta=dict(po.meta))

    def execute_protective_stop(self, symbol: str, ts: int, open_price: float,
                                high: float, low: float) -> Optional[dict]:
        """15m native stop-market: actually close, not 1h close-only invalidation.

        Stop is live immediately after a confirmed entry fill. The stop fill
        reference is its structural stop, unless the candle OPENS across it,
        in which case use the adverse opening price (plus modeled taker
        slippage). An OHLC candle cannot establish intrabar ordering; this
        path makes a protective fill assumption rather than fabricating an
        exchange guarantee. Caller handles its rows as broker executions.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        stop = pos.invalid
        if stop is None or not (math.isfinite(stop) and stop > 0):
            raise AssertionError("active position has no executable protective stop")
        breached = low <= stop if pos.direction > 0 else high >= stop
        if not breached:
            return None
        reference = (min(open_price, stop) if pos.direction > 0
                     else max(open_price, stop))
        order = OrderIntent(
            kind=OrderKind.EXIT, symbol=symbol, qty_frac=None,
            reason=(f"PROTECTIVE_STOP|15m trigger stop={stop:.8g} "
                    f"ref={reference:.8g} gap={'yes' if reference != stop else 'no'}"),
            ref_price=reference, ts=ts)
        pending = _Pending(intent=order, decision_ts=int(pos.decision_ts), meta={
            "role": "protective_stop", "stop_level": stop,
            "stop_install_ts": int(pos.opened_ts),
            "gap_at_open": reference != stop})
        # All actual trading/accounting uses the same _close_qty as normal
        # taker EXIT; no signal-intent state is mistaken for a fill.
        row = self._close_qty(pending, pos, pos.qty, int(ts), reference,
                              full=True)
        self.ledger.append(row)  # broker.on_bar_open usually owns this append
        return row

    # -- mark-to-market(每 bar,权益/资金费/强平) ------------------------------

    def mark(self, symbol: str, ts: int, close: float) -> None:
        """每 bar 收盘盯市:计提资金费、更新 mark 价、近似强平检查。"""
        pos = self.positions.get(symbol)
        if pos is not None:
            self._accrue_funding(pos, close, ts)
        self.last_mark[symbol] = close
        equity = self.equity_mtm()
        for sym, p in list(self.positions.items()):
            price = self.last_mark[sym]
            upnl = p.direction * (price - p.entry) * p.qty
            if -upnl >= self.liq_frac * equity:
                self.n_liquidations += 1
                self._seq += 1
                fake = OrderIntent(kind=OrderKind.EXIT, symbol=sym, qty_frac=None,
                                   reason=(f"LIQUIDATION|floating loss "
                                           f"{-upnl:.8g} >= {self.liq_frac}*equity "
                                           f"{equity:.8g} force market close"),
                                   ref_price=price, ts=ts)
                po = _Pending(intent=fake, decision_ts=ts,
                              meta={"liquidation": True})
                self.pending.setdefault(sym, []).append(po)

    def _accrue_funding(self, pos: _AcctPosition, price: float, ts: int) -> None:
        """资金费计提:有真实费率表按**结算点**逐笔有符号计提,否则按常数近似。

        常数口径(现行,``funding_per_8h``):方向无关、恒为成本、按持仓时长
        线性摊。实测该假设在 4 个标的上高估 5.4 倍,且约 1/4 的结算点方向
        相反(空头收费)→ 见 reports/live_cost_audit_20261023/FUNDING_REPLAY.md。

        真实口径(``funding_rates``):对每个落在 ``(last_funding_ts, ts]`` 的
        结算点 ``k``,成本 = ``qty × price × rate_k × direction``。用当根 bar 的
        成交价作为名义价值近似(结算价是 mark price,与 bar 收盘的偏差在 2h 以上
        周期上远小于费率本身的量级;该近似在报告中如实写明)。
        """
        dt = ts - pos.last_funding_ts
        if dt <= 0:
            return
        table = self.funding_rates
        if table:
            root = capacity.symbol_root(pos.symbol)
            entry = table.get(root)
            if entry is not None:
                times, rates = entry
                lo = bisect.bisect_right(times, pos.last_funding_ts)
                hi = bisect.bisect_right(times, ts)
                if hi > lo:
                    total = 0.0
                    for k in range(lo, hi):
                        total += rates[k]
                    pos.funding_acc += pos.qty * price * total * pos.direction
                pos.last_funding_ts = ts      # 无结算点也必须推进,否则重复扫描
                return
        pos.funding_acc += pos.qty * price * self.funding_8h * (dt / MS_8H)
        pos.last_funding_ts = ts

    # -- 权益 -----------------------------------------------------------------

    def unrealized(self) -> float:
        upnl = 0.0
        for sym, p in self.positions.items():
            price = self.last_mark.get(sym, p.entry)
            upnl += p.direction * (price - p.entry) * p.qty
        return upnl

    def funding_accrued(self) -> float:
        return sum(p.funding_acc for p in self.positions.values())

    def equity_mtm(self) -> float:
        """realized_cash + Σ未实现盈亏 − Σ已计提未结算资金费。"""
        return self.cash + self.unrealized() - self.funding_accrued()

    def position_of(self, symbol: str) -> Optional[_AcctPosition]:
        return self.positions.get(symbol)

    # -- 账本 -----------------------------------------------------------------

    def _next_seq_row(self) -> int:
        self._seq += 1
        return self._seq

    def _row(self, po: _Pending, ts: int, sym: str, side: str, kind: str,
             qty: float, price: float, open_price: float, fee: float,
             funding_delta: float, realized_pnl: float, trade_id: str,
             used_leverage: Optional[float] = None, meta: Optional[dict] = None,
             reason_code: Optional[str] = None) -> dict:
        row = {
            "seq": self._next_seq_row(), "ts": int(ts), "symbol": sym,
            "side": side, "kind": kind, "qty": qty, "price": price,
            "open_price": open_price, "fee": fee, "funding_delta": funding_delta,
            "realized_pnl": realized_pnl, "decision_ts": int(po.decision_ts),
            "reason_code": reason_code or po.intent.reason.split("|", 1)[0],
            "reason": po.intent.reason, "trade_id": trade_id,
            "used_leverage": used_leverage,
            "slippage_cost": qty * abs(price - open_price),
            "cash_after": self.cash, "meta": meta or {},
        }
        return row

    def _skip_row(self, po: _Pending, ts: int, code: str, detail: str,
                  price: Optional[float] = None, qty: float = 0.0) -> dict:
        self.n_skips[code] = self.n_skips.get(code, 0) + 1
        return {
            "seq": self._next_seq_row(), "ts": int(ts),
            "symbol": po.intent.symbol, "side": None, "kind": "SKIP",
            "qty": qty, "price": price, "open_price": None, "fee": 0.0,
            "funding_delta": 0.0, "realized_pnl": 0.0,
            "decision_ts": int(po.decision_ts), "reason_code": code,
            "reason": f"{code}|{detail} (intent: {po.intent.reason})",
            "trade_id": None, "used_leverage": None, "slippage_cost": 0.0,
            "cash_after": self.cash, "meta": dict(po.meta),
        }


def floor_step(qty: float, step: float, place: int) -> float:
    """向下取整至 size step(防二进制浮点尾数误吃一个 step)。

    公开 API:``campaign`` 与实盘 runner 都要按合约步长取量,跨模块使用私有名
    是封装漏缝。历史轮次脚本仍可继续 import ``_floor_step``(下方兼容别名)。
    """
    if qty <= 0.0 or step <= 0.0:
        return 0.0
    return round(math.floor(qty / step + 1e-9) * step, place)


#: 兼容别名:research/rounds 与 v8-v20 历史脚本按旧私有名取用。
_floor_step = floor_step
