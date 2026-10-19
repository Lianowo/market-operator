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


class Broker:
    """多品种共享同一权益的模拟经纪商(合并时间轴回放,见 backtest.py)。"""

    def __init__(self, equity: float = 300.0, *, risk_frac: float,
                 leverage_cap: float, taker_fee: float, slippage_per_side: float,
                 funding_per_8h: float, contracts: dict,
                 cost_multiplier: float = 1.0, liq_frac: float = 0.9) -> None:
        if cost_multiplier <= 0.0:
            raise ValueError("cost_multiplier must be positive")
        self.initial_equity = float(equity)
        self.cash = float(equity)             # realized:权益扣已实现部分
        self.risk_frac = float(risk_frac)
        self.leverage_cap = float(leverage_cap)
        self.taker_fee = float(taker_fee) * cost_multiplier
        self.slippage = float(slippage_per_side) * cost_multiplier
        self.funding_8h = float(funding_per_8h) * cost_multiplier
        self.cost_multiplier = float(cost_multiplier)
        self.contracts = contracts
        self.liq_frac = float(liq_frac)

        self.positions: dict[str, _AcctPosition] = {}
        self.pending: dict[str, list[_Pending]] = {}
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
        qty_cap = self.leverage_cap * equity / price
        qty = _floor_step(min(qty_risk, qty_cap), spec.size_step, spec.size_place)
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
                            last_funding_ts=ts)
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
        qty = _floor_step((po.intent.qty_frac or 0.0) * pos.open_qty,
                          self.contracts[sym].size_step,
                          self.contracts[sym].size_place)
        if qty <= 0.0:
            return self._skip_row(po, ts, "MIN_SIZE", "add qty rounds to 0")
        side = "buy" if pos.direction > 0 else "sell"
        price = self._slipped(o, side)
        fee = qty * price * self.taker_fee
        self.cash -= fee
        pos.entry = (pos.qty * pos.entry + qty * price) / (pos.qty + qty)
        pos.qty += qty
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
