"""保护止损的「本地兜底」语义(执行层;策略链路零改动)。

实盘事实(2026-10-29):`ADVANTESTUSDT` 名义 ~18.5U < 交易所 TPSL 最小名义,
`place-tpsl-order` 返回 `43023 仓位不足`。旧实现把 43023 一律当成"交易所已无
该仓位"⇒ `stops.pop()`,于是该仓位**既无交易所保护单、也不在本地监控**。
这里钉死新语义:
- 挂了交易所单 → 交易所触发;
- 挂不上(43023/仓位过小)→ **本地止损监控**,越过防线即市价离场;
- 两种情形之外,不存在"既没单也没监控"的第三态。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_runner as lr                                          # noqa: E402
from market_operator.bitget_live import BitgetError               # noqa: E402
from market_operator.broker import ContractSpec                   # noqa: E402

SYM = "ADVANTESTUSDT"


class _TG:
    def __init__(self):
        self.alerts: list[str] = []
        self.fills: list[dict] = []

    def send_alert(self, msg, **_kw):
        self.alerts.append(str(msg))

    def send_fill(self, *a, **k):
        pass


class _Client:
    """`place_stop_loss` 一律 43023;记录市价单。"""

    def __init__(self, *, reject="43023 仓位不足，不能设置止盈或者止损"):
        self.reject = reject
        self.market: list[dict] = []

    def positions(self):
        return []

    def tpsl_orders_pending(self):
        return {"entrustedList": []}

    def place_stop_loss(self, symbol, **kw):
        raise BitgetError(f"bitget 400 code=43023: {self.reject}")

    def cancel_tpsl_order(self, symbol, order_id):
        return {}

    def place_market_order(self, symbol, **kw):
        self.market.append({"symbol": symbol, **kw})
        return {"orderId": f"M{len(self.market)}"}


def _cfg() -> dict:
    return json.loads((ROOT / "config" / "live.json").read_text("utf-8"))


def _runner(tmp_path, client, tg, *, mode="live"):
    spec = ContractSpec(symbol=SYM, size_min=0.01, size_step=0.01,
                        size_place=2, price_place=2, min_trade_usdt=5.0)
    return lr.LiveRunner(mode, _cfg(), client, tg,
                         state_path=tmp_path / "state.json",
                         journal_dir=tmp_path / "logs",
                         contracts={SYM: spec}, symbols=())


def _pos(mark=200.0, invalid=190.0, direction=1, qty=0.08):
    return {"symbol": SYM, "direction": direction, "entry": 215.32,
            "invalid": invalid, "qty": qty, "mark": mark, "tf_label": "12h",
            "event_id": "ADOPTED", "target_1": None, "box_lo": None,
            "box_hi": None, "opened_ts": 0}


# ---------------------------------------------------------------------------
# 判定函数
# ---------------------------------------------------------------------------

def test_43023_counts_as_both_no_position_and_local_fallback():
    """43023 同时命中两个判据 —— 所以**顺序**决定行为:必须先试本地兜底。"""
    msg = "bitget 400 code=43023: 仓位不足，不能设置止盈或者止损"
    assert lr._stop_reject_no_position(msg) is True
    assert lr._stop_requires_local(msg) is True


def test_non_43023_errors_are_not_local_fallback():
    msg = "bitget 400 code=45122: 触发价格已越过"
    assert lr._stop_requires_local(msg) is False


# ---------------------------------------------------------------------------
# 行为:挂不上 → 本地监控 → 越线才走
# ---------------------------------------------------------------------------

def test_unplaceable_stop_switches_to_local_mode_and_keeps_position(tmp_path):
    client, tg = _Client(), _TG()
    r = _runner(tmp_path, client, tg)
    r.positions[SYM] = _pos(mark=200.0, invalid=190.0)

    r._manage_stops(1_700_000_000_000)

    assert SYM in r.positions, "仓位绝不能被当成残影丢掉"
    stop = r.stops[SYM]
    assert stop["mode"] == "local" and stop["order_id"] == ""
    assert stop["trigger"] == pytest.approx(190.0)
    assert client.market == [], "未越线时不得市价离场"
    assert any("本地止损" in a for a in tg.alerts)


def test_local_mode_exits_market_when_mark_breaches_defense(tmp_path):
    client, tg = _Client(), _TG()
    r = _runner(tmp_path, client, tg)
    r.positions[SYM] = _pos(mark=200.0, invalid=190.0)
    r._manage_stops(1_700_000_000_000)          # 先切成本地模式

    r.positions[SYM]["mark"] = 189.5             # 跌破防线
    r._manage_stops(1_700_000_060_000)

    assert [o["symbol"] for o in client.market] == [SYM]
    assert client.market[0]["side"] == "buy"     # hedge:平多=buy+close
    assert client.market[0]["trade_side"] == "close"
    assert r.counters["stop_local_triggered"] == 1
    assert SYM not in r.stops


def test_local_mode_does_not_retry_exchange_orders(tmp_path):
    """本地模式下不再反复下交易所单(否则每轮刷 6 次 43023,抬 API 错误计数)。"""
    client, tg = _Client(), _TG()
    r = _runner(tmp_path, client, tg)
    r.positions[SYM] = _pos(mark=200.0)
    r._manage_stops(1_700_000_000_000)
    errs_after_first = r.counters.get("stop_errors", 0)
    for k in range(1, 5):
        r._manage_stops(1_700_000_000_000 + k * 60_000)
    assert r.counters.get("stop_errors", 0) == errs_after_first
    assert r.counters.get("stop_local_mode", 0) == 1
