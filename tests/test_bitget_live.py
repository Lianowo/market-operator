"""Bitget v2 客户端「原生仓位级 TPSL」报文契约(离线假 session,无网络)。

背景:保护止损从「独立计划单(plan-order,带 size/side/tradeSide)」改为
「交易所原生仓位级 TPSL(pos_loss/pos_profit)」。两者报文不可混用:

- `place-tpsl-order` 带 size/side/tradeSide 会被当成另一种单型;
- `cancel-plan-order` 只认 `orderIdList`(对象数组),单值 `orderId` 撤不掉
  ⇒ 旧止损继续有效,双保护各自触发;
- `orders-plan-pending` 的 planType 只接受单值,`loss_plan` 被 40812 拒、
  逗号多值经 %2C 后 40009 签名失败,能用的只有 `profit_loss`。

这些失败都只在线上暴露(且都指向"保护其实没挂/没撤"),所以在报文层钉死。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.bitget_live import BitgetError, BitgetLiveClient  # noqa: E402

SECRETS = {"BITGET_API_KEY": "k", "BITGET_SECRET_KEY": "s",
           "BITGET_PASSPHRASE": "p"}


class _Resp:
    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data

    def json(self):
        return {"code": "00000", "data": self._data}


class _Session:
    """记录真实发出的 (method, url, body),返回统一成功壳。"""

    def __init__(self, data=None):
        self.calls: list[tuple] = []
        self._payload = [] if data is None else data

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None))
        return _Resp(self._payload)

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append((method, url, data))
        return _Resp(self._payload)


def _client(session, secrets=SECRETS):
    return BitgetLiveClient(secrets, session=session, max_retries=1)


# ---------------------------------------------------------------------------
# 挂:place-tpsl-order(仓位级,无数量维度)
# ---------------------------------------------------------------------------

def test_place_stop_loss_sends_position_level_tpsl_body():
    sess = _Session(data={"orderId": "111"})
    resp = _client(sess).place_stop_loss(
        "BTCUSDT", trigger_price=95.5, hold_side="long", client_oid="mo21stop")

    method, url, raw = sess.calls[0]
    body = json.loads(raw)
    assert method == "POST"
    assert url.endswith("/api/v2/mix/order/place-tpsl-order")
    assert body["planType"] == "pos_loss"
    assert body["triggerPrice"] == "95.5"
    assert body["triggerType"] == "mark_price"
    assert body["executePrice"] == "0"        # 触发后市价执行
    assert body["holdSide"] == "long"
    assert body["clientOid"] == "mo21stop"
    # 仓位级 TPSL 没有数量/方向维度:带上就是另一种单型
    assert "size" not in body and "side" not in body and "tradeSide" not in body
    assert resp == {"orderId": "111"}


def test_place_take_profit_uses_pos_profit():
    sess = _Session(data={"orderId": "222"})
    _client(sess).place_take_profit(
        "ETHUSDT", trigger_price=2900.0, hold_side="short", client_oid="mo21tp")

    body = json.loads(sess.calls[0][2])
    assert body["planType"] == "pos_profit"
    assert body["holdSide"] == "short"


def test_place_tpsl_rejects_legacy_plan_type():
    """旧式 loss_plan/profit_plan 必须被挡住,不能悄悄发出去当"成功"。"""
    sess = _Session()
    with pytest.raises(ValueError):
        _client(sess).place_tpsl(
            "BTCUSDT", plan_type="loss_plan", trigger_price=95.0,
            hold_side="long", client_oid="mo21x")
    assert sess.calls == []


def test_place_tpsl_requires_credentials():
    sess = _Session()
    with pytest.raises(BitgetError):
        _client(sess, secrets={}).place_stop_loss(
            "BTCUSDT", trigger_price=95.0, hold_side="long",
            client_oid="mo21x")
    assert sess.calls == []


# ---------------------------------------------------------------------------
# 撤:cancel-plan-order 只认 orderIdList
# ---------------------------------------------------------------------------

def test_cancel_tpsl_order_sends_order_id_list():
    sess = _Session()
    _client(sess).cancel_tpsl_order("BTCUSDT", "123456")

    method, url, raw = sess.calls[0]
    body = json.loads(raw)
    assert method == "POST"
    assert url.endswith("/api/v2/mix/order/cancel-plan-order")
    assert body["orderIdList"] == [{"orderId": "123456"}]
    assert "orderId" not in body        # 单值形式撤不掉 TPSL(旧止损存活)


# ---------------------------------------------------------------------------
# 查:orders-plan-pending 的 planType 只能单值
# ---------------------------------------------------------------------------

def test_tpsl_orders_pending_queries_profit_loss():
    rows = [{"orderId": "1", "symbol": "BTCUSDT", "planType": "pos_loss"}]
    sess = _Session(data=rows)
    got = _client(sess).tpsl_orders_pending()

    method, url, _ = sess.calls[0]
    assert method == "GET"
    assert "/api/v2/mix/order/orders-plan-pending?" in url
    assert "planType=profit_loss" in url
    assert "productType=USDT-FUTURES" in url
    assert got == rows


def test_tpsl_orders_pending_symbol_filter_and_empty_is_list():
    sess = _Session(data=None)                  # 成功但无数据
    got = _client(sess).tpsl_orders_pending("BTCUSDT")

    url = sess.calls[0][1]
    assert "symbol=BTCUSDT" in url
    assert got == []


def test_tpsl_orders_pending_reports_exchange_error(monkeypatch):
    from market_operator import bitget_live as bl
    monkeypatch.setattr(bl, "RETRY_BACKOFF_S", 0.0)

    class _Bad(_Session):
        def get(self, url, headers=None, timeout=None):
            self.calls.append(("GET", url, None))

            class _R:
                status_code = 400
                def json(self):
                    return {"code": "40812", "msg": "planType error"}
            return _R()

    with pytest.raises(BitgetError) as ei:
        _client(_Bad()).tpsl_orders_pending()
    assert "40812" in str(ei.value)


# ---------------------------------------------------------------------------
# 重试必须重签:ACCESS-TIMESTAMP 不得跨重试复用
# ---------------------------------------------------------------------------

def test_signed_retry_regenerates_access_timestamp(monkeypatch):
    """40008(时间戳过期)重试时必须用**新**时间戳重新签名。

    2026-10-29 实盘事故:签名 headers 只在重试循环外生成一次,6 次退避共
    22.5s 全部复用同一过期时间戳 ⇒ 每次都是 40008,一次瞬时抖动被放大成
    「启动必崩 + 烧掉看护重启预算」。这里用递增时钟把该语义钉死。
    """
    import market_operator.bitget_live as bl

    monkeypatch.setattr(bl.time, "sleep", lambda *_: None)
    seen_ts: list[str] = []

    class _Flaky:
        """前两次返回 40008,第三次成功。"""

        def __init__(self):
            self.n = 0

        def get(self, url, headers=None, timeout=None):
            seen_ts.append((headers or {}).get("ACCESS-TIMESTAMP"))
            self.n += 1
            code = "40008" if self.n < 3 else "00000"

            class _R:
                status_code = 200

                def json(self_inner):
                    return {"code": code, "msg": "timestamp expired",
                            "data": {"ok": 1}}
            return _R()

    clock = iter(range(1_700_000_000_000, 1_700_000_100_000, 1_000))
    client = bl.BitgetLiveClient(SECRETS, session=_Flaky(), max_retries=3,
                                 clock=lambda: next(clock))

    assert client._request("GET", "/api/v2/mix/account/account",
                           params={"symbol": "BTCUSDT"}, signed=True) == {"ok": 1}
    assert len(seen_ts) == 3, "40008 必须触发重试"
    assert len(set(seen_ts)) == 3, (
        f"每次重试都要重签时间戳,实际复用: {seen_ts}")


def test_signed_retry_does_not_sleep_after_last_attempt(monkeypatch):
    """重试预算用尽后不得再多睡一轮退避(白等 RETRY_BACKOFF_S*N)。"""
    import market_operator.bitget_live as bl

    sleeps: list[float] = []
    monkeypatch.setattr(bl.time, "sleep", lambda s: sleeps.append(s))

    class _AlwaysFail:
        def get(self, url, headers=None, timeout=None):
            class _R:
                status_code = 400

                def json(self_inner):
                    return {"code": "40404", "msg": "bad"}
            return _R()

    client = bl.BitgetLiveClient(SECRETS, session=_AlwaysFail(), max_retries=3)
    with pytest.raises(BitgetError):
        client._request("GET", "/api/v2/mix/account/account", signed=True)
    assert len(sleeps) == 2, f"3 次尝试只应有 2 次退避,实际 {sleeps}"

