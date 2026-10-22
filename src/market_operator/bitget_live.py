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
import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import urlencode

import requests

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"

# 端点(v2 mix)
EP_TICKERS = "/api/v2/mix/market/tickers"
EP_HISTORY_CANDLES = "/api/v2/mix/market/history-candles"
EP_CONTRACTS = "/api/v2/mix/market/contracts"
EP_ACCOUNTS = "/api/v2/mix/account/accounts"     # 复数端点(实测;单数端点按 symbol 校验失败)
EP_ACCOUNT_ONE = "/api/v2/mix/account/account"   # 单数端点(实测:带 symbol+marginCoin 可用,返回 posMode)
EP_ALL_POSITION = "/api/v2/mix/position/all-position"
EP_SET_LEVERAGE = "/api/v2/mix/account/set-leverage"
EP_PLACE_ORDER = "/api/v2/mix/order/place-order"
EP_CANCEL_ORDER = "/api/v2/mix/order/cancel-order"
EP_PLACE_PLAN = "/api/v2/mix/order/place-plan-order"
EP_CANCEL_PLAN = "/api/v2/mix/order/cancel-plan-order"
EP_ORDERS_PENDING = "/api/v2/mix/order/orders-pending"
EP_PLAN_PENDING = "/api/v2/mix/order/orders-plan-pending"
EP_FILLS = "/api/v2/mix/order/fills"

RETRIES = 6
RETRY_BACKOFF_S = 1.5
TIMEOUT_S = 20

# 宇宙筛选冻结阈值(任务书):24h USDT 名义量严格大于
UNIVERSE_MIN_USDT_VOLUME_24H = 3_000_000.0


class BitgetError(RuntimeError):
    """交易所层错误(http/code/网络;消息只含 code/msg 不含凭证)。"""


class BitgetLiveClient:
    """REST v2 客户端;secrets = secrets_loader.load_secrets() 的返回值。"""

    def __init__(self, secrets: Optional[dict] = None, *,
                 product_type: str = PRODUCT_TYPE,
                 session: Optional[requests.Session] = None,
                 base_url: str = BASE_URL,
                 max_retries: int = RETRIES,
                 clock=lambda: int(time.time() * 1000)) -> None:
        self._key = (secrets or {}).get("BITGET_API_KEY", "")
        self._secret = (secrets or {}).get("BITGET_SECRET_KEY", "")
        self._pass = (secrets or {}).get("BITGET_PASSPHRASE", "")
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
    def query_string(params: dict) -> str:
        """GET 参数串:值一律 str() 后 urlencode(签名/传输同一份)。"""
        return urlencode({k: str(v) for k, v in params.items() if v is not None})

    def _sign(self, timestamp: str, method: str, path_with_query: str,
              body: str) -> str:
        prehash = f"{timestamp}{method.upper()}{path_with_query}{body}"
        digest = hmac.new(self._secret.encode("utf-8"),
                          prehash.encode("utf-8"),
                          hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _headers(self, method: str, path_with_query: str, body: str) -> dict:
        ts = str(self._clock())
        return {
            "ACCESS-KEY": self._key,
            "ACCESS-SIGN": self._sign(ts, method, path_with_query, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self._pass,
            "Content-Type": "application/json",
            "locale": "zh-CN",
        }

    def _request(self, method: str, path: str, *, params: Optional[dict] = None,
                 body: Optional[dict] = None, signed: bool = False) -> object:
        qs = self.query_string(params or {})
        path_q = f"{path}?{qs}" if qs else path
        body_s = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._headers(method, path_q, body_s) if signed else {}
        url = f"{self.base_url}{path_q}"
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                if method.upper() == "GET":
                    resp = self.session.get(url, headers=headers,
                                            timeout=TIMEOUT_S)
                else:
                    resp = self.session.request(
                        method.upper(), url, headers=headers, data=body_s,
                        timeout=TIMEOUT_S)
                payload = resp.json()
                if resp.status_code == 200 and payload.get("code") == "00000":
                    return payload.get("data")
                last_err = BitgetError(
                    f"bitget {resp.status_code} code={payload.get('code')}: "
                    f"{str(payload.get('msg'))[:160]}")
            except (requests.RequestException, ValueError) as e:
                last_err = e
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        self.api_errors += 1
        raise BitgetError(f"{method} {path} failed after {self.max_retries} "
                          f"tries: {type(last_err).__name__} {last_err}")

    # ------------------------------------------------------------------
    # 公共:宇宙 / K线 / 合约
    # ------------------------------------------------------------------

    def tickers(self) -> list:
        return self._request("GET", EP_TICKERS,
                             params={"productType": self.product_type}) or []

    @staticmethod
    def _usdt_volume(t: dict) -> float:
        """24h USDT 名义量:官方字段 usdtVolume;缺失时以 baseVolume×lastPr
        兜底(如实口径,字段在册)。"""
        v = t.get("usdtVolume")
        if v not in (None, ""):
            return float(v)
        v = t.get("quoteVolume")
        if v not in (None, ""):
            return float(v)
        base, last = float(t.get("baseVolume") or 0.0), float(t.get("lastPr") or 0.0)
        return base * last

    def universe(self, min_usdt_volume_24h: float = UNIVERSE_MIN_USDT_VOLUME_24H,
                 tickers: Optional[list] = None) -> list[str]:
        """宇宙筛选:24h USDT 名义量**严格大于**阈值(恰 3M 不入选)。"""
        out = []
        for t in (self.tickers() if tickers is None else tickers):
            sym = t.get("symbol")
            if not sym:
                continue
            if self._usdt_volume(t) > min_usdt_volume_24h:
                out.append(str(sym))
        return sorted(out)

    @staticmethod
    def _granularity_ms(granularity: str) -> int:
        units = {"m": 60_000, "H": 3_600_000, "D": 86_400_000,
                 "W": 604_800_000}
        return int(granularity[:-1]) * units[granularity[-1]]

    def history_candles(self, symbol: str, granularity: str = "2H", *,
                        start_ms: Optional[int] = None,
                        end_ms: Optional[int] = None,
                        limit: int = 200) -> list[tuple]:
        """history-candles 增量拉取:返回**已收盘** bar 升序 (ts,o,h,l,c,v)。

        endTime 为排他锚(口径同 market_operator.data.fetch_klines);本地再
        按 granularity 步长剔除未收齐 bar(ts + step > end_ms),防止行情端
        返回正在形成的 bar 污染认知帧;增量(传 start_ms = 本地末 bar + 1)。
        """
        step = self._granularity_ms(granularity)
        now = int(time.time() * 1000)
        end_ms = now if end_ms is None else int(end_ms)
        params = {"symbol": symbol, "productType": self.product_type,
                  "granularity": granularity, "limit": int(limit),
                  "endTime": end_ms}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        raw = self._request("GET", EP_HISTORY_CANDLES, params=params) or []
        bars = sorted({int(r[0]): (int(r[0]), float(r[1]), float(r[2]),
                                   float(r[3]), float(r[4]), float(r[5]))
                       for r in raw}.values())
        return [b for b in bars if b[0] + step <= end_ms
                and (start_ms is None or b[0] >= int(start_ms))]

    def contracts(self) -> list:
        return self._request("GET", EP_CONTRACTS,
                             params={"productType": self.product_type}) or []

    # ------------------------------------------------------------------
    # 私有:账户 / 持仓 / 杠杆 / 订单
    # ------------------------------------------------------------------

    def require_credentials(self) -> None:
        if not (self._key and self._secret and self._pass):
            raise BitgetError("缺少 Bitget 凭证(键名见 secrets_loader;值不回显)")

    def balance(self, margin_coin: str = "USDT") -> dict:
        """USDT 永续账户(复数端点;equity 取 usdtEquity,缺省回退 equity)。"""
        self.require_credentials()
        data = self._request("GET", EP_ACCOUNTS, signed=True,
                             params={"productType": self.product_type})
        rows = data if isinstance(data, list) else [data]
        d = next((r for r in rows or [] if r.get("marginCoin") == margin_coin),
                 (rows or [{}])[0]) or {}
        return {"available": float(d.get("available") or 0.0),
                "equity": float(d.get("usdtEquity", d.get("equity")) or 0.0),
                "unrealized_pl": float(d.get("unrealizedPL") or 0.0),
                "raw_keys": sorted(d.keys())}

    def positions(self) -> list[dict]:
        self.require_credentials()
        data = self._request("GET", EP_ALL_POSITION, signed=True,
                             params={"productType": self.product_type}) or []
        out = []
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
        """账户持仓模式('hedge_mode' / 'one_way_mode');只读,凭证不入账。

        实测口径(2026-10-22):posMode 为该 productType 的账户级设置,任一
        symbol 查询同值;hedge_mode 下所有 place-order 必须带 tradeSide。
        """
        self.require_credentials()
        data = self._request("GET", EP_ACCOUNT_ONE, signed=True,
                             params={"symbol": symbol,
                                     "marginCoin": "USDT",
                                     "productType": self.product_type})
        row = data[0] if isinstance(data, list) and data else (data or {})
        return str(row.get("posMode") or "")

    def set_leverage(self, symbol: str, leverage: int,
                     hold_side: Optional[str] = None) -> dict:
        """设置杠杆;hedge_mode 需逐侧设置(hold_side=long/short),one-way 不传。"""
        self.require_credentials()
        body = {"symbol": symbol,
                "productType": self.product_type,
                "marginCoin": "USDT",
                "leverage": str(int(leverage))}
        if hold_side:                       # 双向持仓:逐侧设置
            body["holdSide"] = hold_side
        return self._request("POST", EP_SET_LEVERAGE, signed=True,
                             body=body) or {}

    # -- 订单(place/cancel 只组装报文;是否真发由 runner 的 MODE 闸门决定) --

    def place_limit_order(self, symbol: str, *, side: str, size: float,
                          price: float, client_oid: str,
                          trade_side: Optional[str] = None) -> dict:
        """限价 GTC 单。hedge_mode 下 side 表持仓方向、tradeSide 表开平:

        开多 buy+open / 开空 sell+open / 平多 buy+close / 平空 sell+close
        (Bitget 官方口径);one-way-mode 下省略 tradeSide(传了会报 40774)。
        """
        self.require_credentials()
        body = {
            "symbol": symbol, "productType": self.product_type,
            "marginMode": "crossed", "marginCoin": "USDT",
            "size": f"{size}", "price": f"{price}", "side": side,
            "orderType": "limit", "force": "gtc",
            "clientOid": client_oid}
        if trade_side:
            body["tradeSide"] = trade_side
        return self._request("POST", EP_PLACE_ORDER, signed=True,
                             body=body) or {}

    def place_market_order(self, symbol: str, *, side: str, size: float,
                           client_oid: str,
                           trade_side: Optional[str] = None) -> dict:
        """市价单;side / tradeSide 语义同 place_limit_order。"""
        self.require_credentials()
        body = {
            "symbol": symbol, "productType": self.product_type,
            "marginMode": "crossed", "marginCoin": "USDT",
            "size": f"{size}", "side": side, "orderType": "market",
            "clientOid": client_oid}
        if trade_side:
            body["tradeSide"] = trade_side
        return self._request("POST", EP_PLACE_ORDER, signed=True,
                             body=body) or {}

    def place_stop_market_plan(self, symbol: str, *, trigger_price: float,
                               size: float, plan_type: str,
                               hold_side: str, client_oid: str,
                               side: Optional[str] = None,
                               trade_side: Optional[str] = None) -> dict:
        """止损市价计划单(planType=loss_plan 平仓止损;hold_side=long/short)。

        hedge_mode 下 tradeSide 必填(Bitget 官方):平仓止损 = 'close'
        (缺失即报 400172 "开仓方向不能为空");one-way 下省略。

        planType 实测口径(2026-10-22):该端点只接受 normal_plan —— loss_plan
        报 400172 "planType 类型不合法";止损语义由 triggerPrice +
        orderType=market + tradeSide=close 表达,故 loss_plan 归一为 normal_plan。
        """
        self.require_credentials()
        if plan_type == "loss_plan":
            plan_type = "normal_plan"
        body = {
            "symbol": symbol, "productType": self.product_type,
            "marginMode": "crossed", "marginCoin": "USDT",
            "planType": plan_type, "triggerPrice": f"{trigger_price}",
            "triggerType": "mark_price", "executePrice": "0",
            "orderType": "market",          # 止损触发后市价执行(缺失 ⇒ 400172)
            "holdSide": hold_side, "size": f"{size}",
            "clientOid": client_oid}
        if trade_side:
            body["tradeSide"] = trade_side
        if side:
            body["side"] = side            # hedge 下亦必填(持仓方向):缺失 ⇒ 400172
        return self._request("POST", EP_PLACE_PLAN, signed=True,
                             body=body) or {}

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        self.require_credentials()
        return self._request("POST", EP_CANCEL_ORDER, signed=True, body={
            "symbol": symbol, "productType": self.product_type,
            "orderId": str(order_id)}) or {}

    def cancel_plan_order(self, symbol: str, order_id: str) -> dict:
        self.require_credentials()
        return self._request("POST", EP_CANCEL_PLAN, signed=True, body={
            "symbol": symbol, "productType": self.product_type,
            "orderId": str(order_id)}) or {}

    def orders_pending(self, symbol: Optional[str] = None) -> list:
        self.require_credentials()
        params = {"productType": self.product_type}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", EP_ORDERS_PENDING, signed=True,
                             params=params) or []

    def plan_orders_pending(self, symbol: Optional[str] = None, *,
                            plan_type: str = "normal_plan") -> list:
        """在挂计划单。实测口径(2026-10-21):planType 必填且按单值校验 —
        loss_plan 被 40812 拒、逗号多值经 %2C 后 40009 签名失败;normal_plan
        可用(runner 对账只认自有 clientOid 前缀,异己单一律不碰)。"""
        self.require_credentials()
        params = {"productType": self.product_type, "planType": plan_type}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", EP_PLAN_PENDING, signed=True,
                             params=params) or []

    def recent_fills(self, symbol: Optional[str] = None,
                     limit: int = 50) -> list:
        self.require_credentials()
        params = {"productType": self.product_type, "limit": int(limit)}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", EP_FILLS, signed=True,
                             params=params) or []


__all__ = [
    "BASE_URL", "PRODUCT_TYPE", "UNIVERSE_MIN_USDT_VOLUME_24H",
    "BitgetError", "BitgetLiveClient",
]
