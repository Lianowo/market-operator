"""Bitget USDT 永续实盘客户端(REST v2,urlencode + HMAC;实盘接入件)。

模式参照旧仓 edge-trade-core 的对接纪律(仅模式,不涉其策略语义):
- plain requests.Session(本地代理对若干端点友好;不走 ccxt);
- 网络抖动重试 + 指数退避(scripts/fetch_bitget_ohlcv.py 同型);
- 凭证只经 secrets_loader 注入,本模块绝不入日志/回显。

签名(官方 v2 口径):sign = Base64(HMAC-SHA256(secret,
timestamp + method.upper() + requestPath_with_query + body));GET/DELETE 的
requestPath 带已按 urlencode 拼装的 query;POST body = 紧凑 JSON。参数值一律
str() 后再 urlencode —— 签名与线上接收的串严格一致。

安全:本模块不包含任何下单放行逻辑 —— LIVE/PAPER 闸门在 scripts/live_runner.py
(MODE 未显式给出 ⇒ submit 一律 raise,fail-closed)。
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
import hashlib
import hmac
import json
import math
import time
from typing import Any
from urllib.parse import urlencode

import requests

from .exceptions import ExchangeError
from .timeframes import tf_to_ms


def _fmt_decimal(value: float) -> str:
    """Format finite numbers as plain decimal strings without scientific notation (e.g. 8.52e-05 -> '0.0000852')."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"non-finite order numeric value: {value!r}")
    if v == 0.0:
        v = 0.0
    return format(Decimal(str(v)), "f")


BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

# 端点(v2 mix)
EP_TICKERS = "/api/v2/mix/market/tickers"
EP_HISTORY_CANDLES = "/api/v2/mix/market/history-candles"
EP_CONTRACTS = "/api/v2/mix/market/contracts"
EP_FUNDING_HISTORY = "/api/v2/mix/market/history-fund-rate"
EP_ACCOUNTS = "/api/v2/mix/account/accounts"     # 复数端点(实测;单数端点按 symbol 校验失败)
EP_ACCOUNT_ONE = "/api/v2/mix/account/account"   # 单数端点(实测:带 symbol+marginCoin 可用,返回 posMode)
EP_ACCOUNT_BILLS = "/api/v2/mix/account/bill"
EP_ALL_POSITION = "/api/v2/mix/position/all-position"
EP_SET_LEVERAGE = "/api/v2/mix/account/set-leverage"
EP_PLACE_ORDER = "/api/v2/mix/order/place-order"
EP_CANCEL_ORDER = "/api/v2/mix/order/cancel-order"
EP_PLACE_TPSL = "/api/v2/mix/order/place-tpsl-order"   # 原生仓位级止盈/止损(TP/SL)
EP_MODIFY_TPSL = "/api/v2/mix/order/modify-tpsl-order"
EP_CANCEL_PLAN = "/api/v2/mix/order/cancel-plan-order"  # 计划单与 TPSL 共用(orderIdList)
EP_ORDERS_PENDING = "/api/v2/mix/order/orders-pending"
EP_PLAN_PENDING = "/api/v2/mix/order/orders-plan-pending"
EP_PLAN_HISTORY = "/api/v2/mix/order/orders-plan-history"
EP_FILLS = "/api/v2/mix/order/fills"
EP_ORDER_DETAIL = "/api/v2/mix/order/detail"

RETRIES = 6
RETRY_BACKOFF_S = 1.5
TIMEOUT_S = 20

# 宇宙筛选冻结阈值(任务书):24h USDT 名义量严格大于
UNIVERSE_MIN_USDT_VOLUME_24H = 3_000_000.0

# 交易所业务错误码常量
ERR_TIMESTAMP_EXPIRED = "40008"
ERR_INVALID_SIGN = "40009"
ERR_INVALID_TIMESTAMP = "40012"
ERR_DUPLICATE_OID = "40786"
ERR_NO_POSITION = "22002"
ERR_INSUFFICIENT_POS = "43023"
ERR_INVALID_SCALE = "40808"
ERR_MIN_ORDER_SIZE = "45110"

_RESIGNABLE_CODES: frozenset[str] = frozenset({
    ERR_TIMESTAMP_EXPIRED,
    ERR_INVALID_SIGN,
    ERR_INVALID_TIMESTAMP,
})
_DETERMINISTIC_REJECT_CODES: frozenset[str] = frozenset({
    ERR_DUPLICATE_OID,
    ERR_NO_POSITION,
    ERR_INSUFFICIENT_POS,
    ERR_INVALID_SCALE,
    ERR_MIN_ORDER_SIZE,
})


class BitgetError(ExchangeError):
    """交易所层错误(http/code/网络;消息只含 code/msg 不含凭证)。"""


def _retry_after_from_429(resp: Any, default_s: float = 3.0) -> float:
    """Parse HTTP 429 retry delay from Retry-After header or JSON body."""
    retry_after = default_s
    headers = getattr(resp, "headers", None)
    if isinstance(headers, Mapping):
        raw_hdr = headers.get("Retry-After")
        if raw_hdr is not None:
            try:
                retry_after = float(raw_hdr)
            except (TypeError, ValueError):
                retry_after = default_s
    try:
        limited = resp.json()
    except (ValueError, TypeError, AttributeError):
        return retry_after
    if isinstance(limited, Mapping):
        hint = limited.get("parameters") or limited.get("data") or {}
        if isinstance(hint, Mapping) and hint.get("retry_after") is not None:
            try:
                retry_after = float(hint["retry_after"])
            except (TypeError, ValueError):
                return retry_after
    return retry_after


class BitgetLiveClient:
    """REST v2 客户端;secrets = secrets_loader.load_secrets() 的返回值。"""

    def __init__(
        self,
        secrets: Mapping[str, Any] | None = None,
        *,
        product_type: str = PRODUCT_TYPE,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        max_retries: int = RETRIES,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        sec = secrets or {}
        self._key = str(sec.get("BITGET_API_KEY", ""))
        self._secret = str(sec.get("BITGET_SECRET_KEY", ""))
        self._pass = str(sec.get("BITGET_PASSPHRASE", ""))
        self.product_type = product_type
        self.base_url = base_url.rstrip("/")
        self.max_retries = int(max_retries)
        self._clock = clock
        self.api_errors = 0                     # 熔断计数(runner 侧读)
        self.session = session or requests.Session()

    # ------------------------------------------------------------------
    # 签名/请求
    # ------------------------------------------------------------------

    @staticmethod
    def query_string(params: Mapping[str, Any]) -> str:
        """GET 参数串:值一律 str() 后 urlencode(签名/传输同一份)。"""
        return urlencode({k: str(v) for k, v in params.items() if v is not None})

    def _sign(self, timestamp: str, method: str, path_with_query: str, body: str) -> str:
        prehash = f"{timestamp}{method.upper()}{path_with_query}{body}"
        digest = hmac.new(
            self._secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _headers(self, method: str, path_with_query: str, body: str) -> dict[str, str]:
        ts = str(self._clock())
        return {
            "ACCESS-KEY": self._key,
            "ACCESS-SIGN": self._sign(ts, method, path_with_query, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self._pass,
            "Content-Type": "application/json",
            "locale": "zh-CN",
        }

    def _send_http(self, method: str, url: str, headers: dict[str, str], body_s: str) -> Any:
        if method.upper() == "GET":
            return self.session.get(url, headers=headers, timeout=TIMEOUT_S)
        return self.session.request(
            method.upper(), url, headers=headers, data=body_s, timeout=TIMEOUT_S
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        qs = self.query_string(params or {})
        path_q = f"{path}?{qs}" if qs else path
        body_s = json.dumps(body, separators=(",", ":")) if body else ""
        url = f"{self.base_url}{path_q}"
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            # ACCESS-TIMESTAMP 必须每次重试重新生成,避免 40008 放大瞬时抖动
            headers = self._headers(method, path_q, body_s) if signed else {}
            try:
                resp = self._send_http(method, url, headers, body_s)
                if resp.status_code == 429:
                    retry_after = _retry_after_from_429(resp)
                    last_err = BitgetError(f"bitget 429 retry_after={retry_after}")
                    if attempt + 1 < self.max_retries:
                        time.sleep(min(60.0, max(0.0, retry_after)))
                    continue
                payload = resp.json()
                if resp.status_code == 200 and payload.get("code") == "00000":
                    return payload.get("data")
                code = str(payload.get("code"))
                last_err = BitgetError(
                    f"bitget {resp.status_code} code={code}: "
                    f"{str(payload.get('msg'))[:160]}"
                )
                if code in _RESIGNABLE_CODES and attempt + 1 < self.max_retries:
                    continue
                if code in _DETERMINISTIC_REJECT_CODES:
                    break
            except (requests.RequestException, ValueError) as exc:
                last_err = exc
            if attempt + 1 < self.max_retries:
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        self.api_errors += 1
        raise BitgetError(
            f"{method} {path} failed after {self.max_retries} "
            f"tries: {type(last_err).__name__} {last_err}"
        )

    # ------------------------------------------------------------------
    # 公共:宇宙 / K线 / 合约 / 资金费
    # ------------------------------------------------------------------

    def tickers(self) -> list[dict[str, Any]]:
        return self._request("GET", EP_TICKERS, params={"productType": self.product_type}) or []

    @staticmethod
    def _usdt_volume(t: Mapping[str, Any]) -> float:
        """24h USDT 名义量:官方字段 usdtVolume;缺失时以 baseVolume×lastPr 兜底。"""
        for key in ("usdtVolume", "quoteVolume"):
            val = t.get(key)
            if val not in (None, ""):
                return float(val)
        base, last = float(t.get("baseVolume") or 0.0), float(t.get("lastPr") or 0.0)
        return base * last

    def universe(
        self,
        thresholds: Mapping[str, Any] | float | None = None,
        tickers: Sequence[Mapping[str, Any]] | None = None,
        contracts: Sequence[Mapping[str, Any]] | None = None,
        rwa_no_volume_filter: bool | None = None,
    ) -> list[str]:
        """宇宙筛选:Crypto 与非加密传统(RWA)合约按各自规则独立判定。"""
        if isinstance(thresholds, Mapping):
            uni: dict[str, Any] = dict(thresholds)
        elif thresholds is None:
            uni = {}
        else:
            uni = {"crypto": {"min_usdt_volume_24h": float(thresholds)}}
        crypto_min = float(
            (uni.get("crypto") or {}).get(
                "min_usdt_volume_24h",
                uni.get("min_usdt_volume_24h", UNIVERSE_MIN_USDT_VOLUME_24H),
            )
            or 0.0
        )
        rwa_min = float((uni.get("rwa") or {}).get("min_usdt_volume_24h", crypto_min) or 0.0)
        skip_rwa_volume = (
            bool(uni.get("rwa_no_volume_filter"))
            if rwa_no_volume_filter is None
            else bool(rwa_no_volume_filter)
        )

        specs = {str(c["symbol"]): c for c in (contracts or ()) if c.get("symbol")}
        out: set[str] = set()
        for row in (tickers if tickers is not None else self.tickers()):
            sym = row.get("symbol")
            if not sym:
                continue
            sym_s = str(sym)
            spec = specs.get(sym_s)
            if spec is not None and str(spec.get("symbolStatus") or "normal").strip().lower() != "normal":
                continue
            is_rwa = str((spec or {}).get("isRwa") or "").strip().upper() == "YES"
            if is_rwa and skip_rwa_volume:
                out.add(sym_s)
                continue
            floor = rwa_min if is_rwa else crypto_min
            if self._usdt_volume(row) > floor:
                out.add(sym_s)
        return sorted(out)

    @staticmethod
    def _granularity_ms(granularity: str) -> int:
        return tf_to_ms(granularity)

    def history_candles(
        self,
        symbol: str,
        granularity: str = "2H",
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 200,
    ) -> list[tuple[int, float, float, float, float, float]]:
        """history-candles 增量拉取:返回已收盘 bar 升序 ``(ts,o,h,l,c,v)``。"""
        step = self._granularity_ms(granularity)
        now = int(time.time() * 1000)
        end_ms = now if end_ms is None else int(end_ms)
        params: dict[str, Any] = {
            "symbol": symbol,
            "productType": self.product_type,
            "granularity": granularity,
            "limit": int(limit),
            "endTime": end_ms,
        }
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        raw = self._request("GET", EP_HISTORY_CANDLES, params=params) or []
        bars = sorted(
            {
                int(r[0]): (
                    int(r[0]),
                    float(r[1]),
                    float(r[2]),
                    float(r[3]),
                    float(r[4]),
                    float(r[5]),
                )
                for r in raw
            }.values()
        )
        return [
            b for b in bars
            if b[0] + step <= end_ms and (start_ms is None or b[0] >= int(start_ms))
        ]

    def contracts(self) -> list[dict[str, Any]]:
        return self._request("GET", EP_CONTRACTS, params={"productType": self.product_type}) or []

    def rwa_symbols(self) -> list[str]:
        """Return sorted list of active RWA contract symbols."""
        return sorted(
            str(c["symbol"])
            for c in self.contracts()
            if c.get("symbol") and str(c.get("isRwa") or "").strip().upper() == "YES"
        )

    def funding_rate_history(
        self,
        symbol: str,
        *,
        page_size: int = 100,
        page_no: int = 1,
    ) -> list[dict[str, Any]]:
        """Fetch public historical funding rates for a contract."""
        return (
            self._request(
                "GET",
                EP_FUNDING_HISTORY,
                params={
                    "symbol": symbol,
                    "productType": self.product_type,
                    "pageSize": int(page_size),
                    "pageNo": int(page_no),
                },
            )
            or []
        )

    # ------------------------------------------------------------------
    # 私有:账户 / 持仓 / 杠杆 / 订单
    # ------------------------------------------------------------------

    def require_credentials(self) -> None:
        if not (self._key and self._secret and self._pass):
            raise BitgetError("缺少 Bitget 凭证(键名见 secrets_loader;值不回显)")

    def balance(self, margin_coin: str = "USDT") -> dict[str, Any]:
        """USDT 永续账户(复数端点;equity 取 usdtEquity,缺省回退 equity)。"""
        self.require_credentials()
        data = self._request(
            "GET", EP_ACCOUNTS, signed=True, params={"productType": self.product_type}
        )
        rows = data if isinstance(data, list) else [data]
        d = next(
            (r for r in rows or [] if r.get("marginCoin") == margin_coin),
            (rows or [{}])[0],
        ) or {}
        return {
            "available": float(d.get("available") or 0.0),
            "equity": float(d.get("usdtEquity", d.get("equity")) or 0.0),
            "unrealized_pl": float(d.get("unrealizedPL") or 0.0),
            "raw_keys": sorted(d.keys()),
        }

    def account_bills(
        self,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        id_less_than: str | None = None,
        business_type: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch signed account bill ledger (fees, funding, realized PnL)."""
        self.require_credentials()
        params: dict[str, Any] = {
            "productType": self.product_type,
            "limit": int(limit),
        }
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        if id_less_than is not None:
            params["idLessThan"] = str(id_less_than)
        if business_type is not None:
            params["businessType"] = str(business_type)
        data = self._request("GET", EP_ACCOUNT_BILLS, signed=True, params=params)
        return data if isinstance(data, dict) else {"bills": data or []}

    def positions(self) -> list[dict[str, Any]]:
        self.require_credentials()
        data = (
            self._request(
                "GET", EP_ALL_POSITION, signed=True, params={"productType": self.product_type}
            )
            or []
        )
        out: list[dict[str, Any]] = []
        for p in data:
            side = str(p.get("holdSide") or "").lower()
            qty = float(p.get("total") or 0.0)
            if qty <= 0.0:
                continue
            out.append({
                "symbol": str(p.get("symbol")),
                "direction": +1 if side == "long" else (-1 if side == "short" else 0),
                "qty": qty,
                "entry_price": float(p.get("openPriceAvg") or 0.0),
                "mark_price": float(p.get("markPrice") or 0.0),
                "unrealized_pl": float(p.get("unrealizedPL") or 0.0),
                "leverage": float(p.get("leverage") or 0.0),
            })
        return out

    def position_mode(self, symbol: str) -> str:
        """账户持仓模式('hedge_mode' / 'one_way_mode');只读,凭证不入账。"""
        self.require_credentials()
        data = self._request(
            "GET",
            EP_ACCOUNT_ONE,
            signed=True,
            params={"symbol": symbol, "marginCoin": "USDT", "productType": self.product_type},
        )
        row = data[0] if isinstance(data, list) and data else (data or {})
        return str(row.get("posMode") or "")

    def set_leverage(
        self,
        symbol: str,
        leverage: int,
        hold_side: str | None = None,
    ) -> dict[str, Any]:
        """设置杠杆;hedge_mode 需逐侧设置(hold_side=long/short),one-way 不传。"""
        self.require_credentials()
        body: dict[str, Any] = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginCoin": "USDT",
            "leverage": str(int(leverage)),
        }
        if hold_side:
            body["holdSide"] = hold_side
        return self._request("POST", EP_SET_LEVERAGE, signed=True, body=body) or {}

    # -- 订单(place/cancel 只组装报文;是否真发由 runner 的 MODE 闸门决定) --

    def place_limit_order(
        self,
        symbol: str,
        *,
        side: str,
        size: float,
        price: float,
        client_oid: str,
        trade_side: str | None = None,
        preset_stop_loss: float | None = None,
    ) -> dict[str, Any]:
        """限价 GTC 单;hedge_mode 下带 tradeSide,one-way 省略。"""
        self.require_credentials()
        body: dict[str, Any] = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginMode": "crossed",
            "marginCoin": "USDT",
            "size": _fmt_decimal(size),
            "price": _fmt_decimal(price),
            "side": side,
            "orderType": "limit",
            "force": "gtc",
            "clientOid": client_oid,
        }
        if trade_side:
            body["tradeSide"] = trade_side
        if preset_stop_loss is not None:
            body["presetStopLossPrice"] = _fmt_decimal(preset_stop_loss)
            body["presetStopLossTriggerType"] = "mark_price"
        return self._request("POST", EP_PLACE_ORDER, signed=True, body=body) or {}

    def place_market_order(
        self,
        symbol: str,
        *,
        side: str,
        size: float,
        client_oid: str,
        trade_side: str | None = None,
        preset_stop_loss: float | None = None,
    ) -> dict[str, Any]:
        """市价单;side / tradeSide 语义同 place_limit_order。"""
        self.require_credentials()
        body: dict[str, Any] = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginMode": "crossed",
            "marginCoin": "USDT",
            "size": _fmt_decimal(size),
            "side": side,
            "orderType": "market",
            "clientOid": client_oid,
        }
        if trade_side:
            body["tradeSide"] = trade_side
        if preset_stop_loss is not None:
            body["presetStopLossPrice"] = _fmt_decimal(preset_stop_loss)
            body["presetStopLossTriggerType"] = "mark_price"
        return self._request("POST", EP_PLACE_ORDER, signed=True, body=body) or {}

    def place_tpsl(
        self,
        symbol: str,
        *,
        plan_type: str,
        trigger_price: float,
        hold_side: str,
        client_oid: str,
        trigger_type: str = "mark_price",
    ) -> dict[str, Any]:
        """交易所原生仓位级止盈/止损(TPSL):只接受 pos_loss / pos_profit。"""
        self.require_credentials()
        if plan_type not in ("pos_loss", "pos_profit"):
            raise ValueError(
                f"place_tpsl 只接受仓位级 pos_loss/pos_profit,收到 {plan_type!r}"
            )
        body = {
            "symbol": symbol,
            "productType": self.product_type,
            "marginCoin": "USDT",
            "planType": plan_type,
            "triggerPrice": _fmt_decimal(trigger_price),
            "triggerType": trigger_type,
            "executePrice": "0",
            "holdSide": hold_side,
            "clientOid": client_oid,
        }
        return self._request("POST", EP_PLACE_TPSL, signed=True, body=body) or {}

    def place_stop_loss(
        self,
        symbol: str,
        *,
        trigger_price: float,
        hold_side: str,
        client_oid: str,
        trigger_type: str = "mark_price",
    ) -> dict[str, Any]:
        """整仓止损(pos_loss):触发后市价平掉该方向的全部持仓。"""
        return self.place_tpsl(
            symbol,
            plan_type="pos_loss",
            trigger_price=trigger_price,
            hold_side=hold_side,
            client_oid=client_oid,
            trigger_type=trigger_type,
        )

    def place_take_profit(
        self,
        symbol: str,
        *,
        trigger_price: float,
        hold_side: str,
        client_oid: str,
        trigger_type: str = "mark_price",
    ) -> dict[str, Any]:
        """整仓止盈(pos_profit):语义同 place_stop_loss,方向相反。"""
        return self.place_tpsl(
            symbol,
            plan_type="pos_profit",
            trigger_price=trigger_price,
            hold_side=hold_side,
            client_oid=client_oid,
            trigger_type=trigger_type,
        )

    def modify_stop_loss(
        self,
        symbol: str,
        *,
        order_id: str,
        trigger_price: float,
    ) -> dict[str, Any]:
        """Amend position-level protection without cancelling the old defense."""
        self.require_credentials()
        return (
            self._request(
                "POST",
                EP_MODIFY_TPSL,
                signed=True,
                body={
                    "symbol": symbol,
                    "productType": self.product_type,
                    "marginCoin": "USDT",
                    "orderId": str(order_id),
                    "triggerPrice": _fmt_decimal(trigger_price),
                    "triggerType": "mark_price",
                    "executePrice": "0",
                    "size": "",
                },
            )
            or {}
        )

    def place_partial_take_profit(
        self,
        symbol: str,
        *,
        trigger_price: float,
        size: float,
        hold_side: str,
        client_oid: str,
    ) -> dict[str, Any]:
        """Fixed-quantity target, market execution on last-price touch."""
        self.require_credentials()
        if size <= 0:
            raise ValueError("take-profit size must be positive")
        return (
            self._request(
                "POST",
                EP_PLACE_TPSL,
                signed=True,
                body={
                    "symbol": symbol,
                    "productType": self.product_type,
                    "marginCoin": "USDT",
                    "planType": "profit_plan",
                    "triggerPrice": _fmt_decimal(trigger_price),
                    "triggerType": "fill_price",
                    "executePrice": "0",
                    "holdSide": hold_side,
                    "size": _fmt_decimal(size),
                    "clientOid": client_oid,
                },
            )
            or {}
        )

    def tpsl_order_history(self, symbol: str, order_id: str) -> list[dict[str, Any]]:
        self.require_credentials()
        data = (
            self._request(
                "GET",
                EP_PLAN_HISTORY,
                signed=True,
                params={
                    "symbol": symbol,
                    "productType": self.product_type,
                    "planType": "profit_loss",
                    "orderId": str(order_id),
                },
            )
            or {}
        )
        return (data.get("entrustedList") or []) if isinstance(data, dict) else (data or [])

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        self.require_credentials()
        return (
            self._request(
                "POST",
                EP_CANCEL_ORDER,
                signed=True,
                body={
                    "symbol": symbol,
                    "productType": self.product_type,
                    "orderId": str(order_id),
                },
            )
            or {}
        )

    def cancel_tpsl_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        """撤销在挂 TPSL(orderIdList 形态)。"""
        self.require_credentials()
        return (
            self._request(
                "POST",
                EP_CANCEL_PLAN,
                signed=True,
                body={
                    "symbol": symbol,
                    "productType": self.product_type,
                    "orderIdList": [{"orderId": str(order_id)}],
                },
            )
            or {}
        )

    def cancel_plan_order(
        self,
        symbol: str,
        order_id: str,
        plan_type: str = "loss_plan",
    ) -> dict[str, Any]:
        """撤销旧式计划单(loss_plan,单值 orderId + planType 形态)。"""
        self.require_credentials()
        return (
            self._request(
                "POST",
                EP_CANCEL_PLAN,
                signed=True,
                body={
                    "symbol": symbol,
                    "productType": self.product_type,
                    "marginCoin": "USDT",
                    "orderId": str(order_id),
                    "planType": str(plan_type),
                },
            )
            or {}
        )

    def orders_pending(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self.require_credentials()
        params: dict[str, Any] = {"productType": self.product_type}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", EP_ORDERS_PENDING, signed=True, params=params) or []

    def tpsl_orders_pending(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """在挂 TPSL / 计划单(planType=profit_loss,单值必填)。"""
        self.require_credentials()
        params: dict[str, Any] = {"productType": self.product_type, "planType": "profit_loss"}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", EP_PLAN_PENDING, signed=True, params=params) or []

    def recent_fills(self, symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self.require_credentials()
        params: dict[str, Any] = {"productType": self.product_type, "limit": int(limit)}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", EP_FILLS, signed=True, params=params) or []

    def order_detail(self, symbol: str, order_id: str) -> dict[str, Any]:
        """单笔订单详情。开仓回执优先读 priceAvg,成交列表还没索引时用它校正入场价。"""
        self.require_credentials()
        data = self._request(
            "GET",
            EP_ORDER_DETAIL,
            signed=True,
            params={
                "symbol": symbol,
                "productType": self.product_type,
                "orderId": str(order_id),
            },
        )
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}


__all__ = [
    "BASE_URL",
    "BitgetError",
    "BitgetLiveClient",
    "ERR_DUPLICATE_OID",
    "ERR_INSUFFICIENT_POS",
    "ERR_INVALID_SCALE",
    "ERR_INVALID_SIGN",
    "ERR_INVALID_TIMESTAMP",
    "ERR_MIN_ORDER_SIZE",
    "ERR_NO_POSITION",
    "ERR_TIMESTAMP_EXPIRED",
    "PRODUCT_TYPE",
    "UNIVERSE_MIN_USDT_VOLUME_24H",
]
