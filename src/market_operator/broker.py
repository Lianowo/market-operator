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

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from .strategy import OrderIntent, OrderKind

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


class Broker:
    """多品种共享同一权益的模拟经纪商(合并时间轴回放,见 backtest.py)。"""

    def __init__(self, equity: float = 300.0, *, risk_frac: float,
                 leverage_cap: float, taker_fee: float, slippage_per_side: float,
                 funding_per_8h: float, contracts: dict,
                 maker_fee: Optional[float] = None,
                 cost_multiplier: float = 1.0, liq_frac: float = 0.9,
                 portfolio_stop_risk_frac: Optional[float] = None) -> None:
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
                   c: float) -> list[dict]:
        """15m bar 上的挂单处理:先判超时,再判严格穿过成交(FIFO)。

        可成交窗口 = 本 15m bar 所属 1h bar 标签 ∈ (armed_at_ts, expiry_ts];
        armed 小时自身的子 bar 不参与(因果:决策须早于成交)。
        """
        hour_label = int(ts) - (int(ts) % MS_1H)
        rows: list[dict] = []
        kept: list[RestingLimitOrder] = []
        for od in self.resting.pop(symbol, []):
            if hour_label > od.expiry_ts:
                rows.append(self._timeout(od, int(ts)))
                continue
            if hour_label <= od.armed_at_ts:
                kept.append(od)                       # 武装小时自身,不可成交
                continue
            od.wait_bars += 1
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

    def _admin_row(self, od: RestingLimitOrder, ts: int, *, kind: str,
                   reason_code: str, detail: str) -> dict:
        """EXPIRED/CANCEL 账本行(无成交;fee/pnl 全 0;ts > armed 由
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
                     "expiry_ts": od.expiry_ts},
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

    def _available_stop_risk(self, equity: float) -> float:
        """从当前盯市价到结构止损的总账户风险;未启用时保持 v4 原始口径。"""
        if self.portfolio_stop_risk_frac is None:
            return float("inf")
        outstanding = 0.0
        for sym, pos in self.positions.items():
            if pos.invalid is None:
                return 0.0  # stop 未知,不接受任何新增风险
            mark = self.last_mark.get(sym, pos.entry)
            outstanding += pos.qty * max(0.0, pos.direction * (mark - pos.invalid))
        return max(0.0, equity * self.portfolio_stop_risk_frac - outstanding)

    def _fill_open_limit(self, od: RestingLimitOrder, ts: int) -> dict:
        """入场挂单成交:limit_price 精确成交(无滑点),maker 费,sizing 同 OPEN。"""
        sym = od.symbol
        spec = self.contracts[sym]
        d = od.direction
        side = "buy" if d > 0 else "sell"
        price = od.limit_price
        ref = {"armed_at_ts": od.armed_at_ts, "intent_reason": od.reason}
        if sym in self.positions:
            return self._limit_skip(od, ts, "DUP_POSITION",
                                    f"position already open for {sym}", ref)
        if d * (price - od.invalid) <= 0.0:
            return self._limit_skip(od, ts, "BAD_INVALID",
                                    f"limit {price} not protective vs "
                                    f"invalid={od.invalid}", ref)
        equity = self.equity_mtm()
        risk_per_unit = abs(price - od.invalid)
        qty_risk = self.risk_frac * equity / risk_per_unit
        qty_cap = self._available_entry_notional(equity) / price
        qty_portfolio_stop = self._available_stop_risk(equity) / risk_per_unit
        qty = _floor_step(min(qty_risk, qty_cap, qty_portfolio_stop),
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
                            last_funding_ts=ts, invalid=float(od.invalid))
        self.positions[sym] = pos
        self.last_mark[sym] = price
        meta = dict(od.meta)
        meta.update({"invalid": od.invalid, "target_1": od.target_1,
                     "event_id": od.event_id, "risk_usdt": qty * risk_per_unit,
                     "entry_mode": "limit", "limit_price": od.limit_price,
                     "fill_wait_bars": od.wait_bars})
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

    def on_bar_open(self, symbol: str, ts: int, open_price: float) -> list[dict]:
        """撮合该 symbol 全部待成交订单(按提交先后),返回本 bar 账本行。"""
        orders = self.pending.pop(symbol, [])
        rows: list[dict] = []
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

    def _slipped(self, price: float, side: str) -> float:
        """对成交方向不利的滑点(买入更贵,卖出更廉)。"""
        return price * (1.0 + self.slippage) if side == "buy" \
            else price * (1.0 - self.slippage)

    def _fill_open(self, po: _Pending, ts: int, o: float) -> dict:
        sym = po.intent.symbol
        spec = self.contracts[sym]
        d = po.direction
        side = "buy" if d > 0 else "sell"
        price = self._slipped(o, side)

        if sym in self.positions:
            return self._skip_row(po, ts, "DUP_POSITION",
                                  f"position already open for {sym}", price=price)
        if d * (price - po.invalid) <= 0.0:
            return self._skip_row(po, ts, "BAD_INVALID",
                                  f"fill {price} not protective vs "
                                  f"invalid={po.invalid}", price=price)

        equity = self.equity_mtm()
        risk_per_unit = abs(price - po.invalid)
        qty_risk = self.risk_frac * equity / risk_per_unit
        qty_cap = self._available_entry_notional(equity) / price
        qty_portfolio_stop = self._available_stop_risk(equity) / risk_per_unit
        qty = _floor_step(min(qty_risk, qty_cap, qty_portfolio_stop),
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
                            last_funding_ts=ts, invalid=float(po.invalid))
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
        price = self._slipped(o, side)
        spec = self.contracts[sym]
        requested = (po.intent.qty_frac or 0.0) * pos.open_qty
        equity = self.equity_mtm()
        cap_qty = self._available_entry_notional(equity) / price
        if self.portfolio_stop_risk_frac is not None:
            if pos.invalid is None or pos.direction * (price - pos.invalid) <= 0:
                return self._skip_row(po, ts, "BAD_INVALID", "add without protective stop")
            stop_qty = self._available_stop_risk(equity) / (
                pos.direction * (price - pos.invalid))
            cap_qty = min(cap_qty, stop_qty)
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
        price = self._slipped(o, side)
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
        """按持仓时长精确到毫秒比例计提资金费(方向无关,恒为成本)。"""
        dt = ts - pos.last_funding_ts
        if dt <= 0:
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


def _floor_step(qty: float, step: float, place: int) -> float:
    """向下取整至 size step(防二进制浮点尾数误吃一个 step)。"""
    if qty <= 0.0 or step <= 0.0:
        return 0.0
    return round(math.floor(qty / step + 1e-9) * step, place)
