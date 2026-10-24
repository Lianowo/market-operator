"""实盘接入件测试(v21 单一生产链;任务书 ≥8 条):

① secret loader 不泄(缺失异常只含键名;别名;EDGE_SECRETS_PATH 覆写;
  防泄闸);② 客户端签名(HMAC-SHA256 确定性与 urlencode query 入签名);
③ 宇宙过滤边界恰 3M(严格大于;字段兜底);④ 熔断条件(日损恰界 -10%
  inclusive、日界复位、API 错计数、每时订单上限);⑤ 对账(ours/exchange
  only/数量容差);⑥ 12h 聚合(末桶不齐丢/内部空洞 raise/首桶保留);
⑦ mic 认知回放 == prepare_cognition 逐 bar(实盘数据锚,canonical 硬约);
⑧ MODE fail-closed;⑨ 幂等 OID / worst-case / intent 闸门(* 冒烟纪律)。

凭证样例一律占位符(token "x"*32 等),永不使用真实值。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator.bitget_live import (                        # noqa: E402
    BitgetError, BitgetLiveClient, UNIVERSE_MIN_USDT_VOLUME_24H)
from market_operator.secrets_loader import (                     # noqa: E402
    SecretsError, assert_never_logged, load_secrets,
    secrets_path as _sp)

import live_runner as lr                                         # noqa: E402


# ---------------------------------------------------------------------------
# ① secret loader 不泄
# ---------------------------------------------------------------------------

PLACEHOLDER = {"BITGET_API_KEY": "x" * 24,
               "BITGET_SECRET_KEY": "s" * 32,
               "BITGET_PASSPHRASE": "p-ph",
               "TELEGRAM_BOT_TOKEN": "0:PLACEHOLDER",
               "TELEGRAM_CHAT_ID": "000000"}


def _write_secrets(tmp: Path, kv: dict) -> Path:
    p = tmp / "secrets.env"
    p.write_text("# 占位符样例(永不真实)\n"
                 + "\n".join(f"{k}={v}" for k, v in kv.items()) + "\n",
                 encoding="utf-8")
    return p


def test_secrets_loader_ok_and_alias(tmp_path, monkeypatch):
    p = _write_secrets(tmp_path, {**PLACEHOLDER,
                                  "TELEGRAM_PROXY": "http://127.0.0.1:7890"})
    monkeypatch.delenv("EDGE_SECRETS_PATH", raising=False)
    sec = load_secrets(p)
    assert sec["BITGET_API_KEY"] == PLACEHOLDER["BITGET_API_KEY"]
    assert sec["TELEGRAM_BOT_TOKEN"] == PLACEHOLDER["TELEGRAM_BOT_TOKEN"]
    # 别名:文件用任务书别名 BITGET_API_SECRET / TG_BOT_TOKEN 也受理
    (tmp_path / "alt").mkdir()
    p2 = _write_secrets(tmp_path / "alt", {
        "BITGET_API_KEY": "y" * 24, "BITGET_API_SECRET": "s2" * 16,
        "BITGET_PASSPHRASE": "pp", "TG_BOT_TOKEN": "0:ALT",
        "TG_CHAT_ID": "111"})
    sec2 = load_secrets(p2)
    assert sec2["BITGET_SECRET_KEY"] == "s2" * 16
    assert sec2["TELEGRAM_CHAT_ID"] == "111"


def test_secrets_loader_missing_leaks_nothing(tmp_path, monkeypatch):
    p = _write_secrets(tmp_path, {"BITGET_API_KEY": "LEAKME-probe-value"})
    monkeypatch.delenv("EDGE_SECRETS_PATH", raising=False)
    for k in ("BITGET_SECRET_KEY", "BITGET_PASSPHRASE",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SecretsError) as ei:
        load_secrets(p)
    msg = str(ei.value)
    assert "LEAKME-probe-value" not in msg        # 异常绝不回显任何值
    assert "BITGET_SECRET_KEY" in msg and "TELEGRAM_BOT_TOKEN" in msg
    # 防泄闸:待输出文本含值即拒
    assert_never_logged("s3cr3t", "正常文本")
    with pytest.raises(SecretsError):
        assert_never_logged("s3cr3t", "账本行 token=s3cr3t 出现")


def test_secrets_env_override(tmp_path, monkeypatch):
    p = _write_secrets(tmp_path, PLACEHOLDER)
    monkeypatch.setenv("EDGE_SECRETS_PATH", str(p))
    assert _sp() == p
    assert load_secrets()["BITGET_PASSPHRASE"] == "p-ph"


# ---------------------------------------------------------------------------
# ② 客户端签名
# ---------------------------------------------------------------------------


def test_sign_deterministic_and_official_layout():
    sec = {"BITGET_API_KEY": "k", "BITGET_SECRET_KEY": "s" * 32,
           "BITGET_PASSPHRASE": "p"}
    cli = BitgetLiveClient(sec, clock=lambda: 1_700_000_000_000)
    path_q = "/api/v2/mix/position/all-position?productType=USDT-FUTURES"
    expect = base64.b64encode(hmac.new(
        ("s" * 32).encode(),
        f"1700000000000GET{path_q}".encode(),
        hashlib.sha256).digest()).decode()
    got = cli._sign("1700000000000", "GET", path_q, "")
    assert got == expect
    hdr = cli._headers("GET", path_q, "")
    assert hdr["ACCESS-SIGN"] == got and hdr["ACCESS-TIMESTAMP"] == \
        "1700000000000" and hdr["ACCESS-KEY"] == "k"
    # POST:body 参与签名;query urlencode(str 化)与传输同一份
    assert BitgetLiveClient.query_string({"a": 1, "b": "x y"}) == "a=1&b=x+y"
    body = '{"size":"0.001"}'
    h2 = cli._headers("POST", "/api/v2/mix/order/place-order", body)
    exp2 = base64.b64encode(hmac.new(
        ("s" * 32).encode(),
        f"1700000000000POST/api/v2/mix/order/place-order{body}".encode(),
        hashlib.sha256).digest()).decode()
    assert h2["ACCESS-SIGN"] == exp2


def test_client_api_error_counts(tmp_path):
    import requests as _rq

    class DeadSession:
        def get(self, *a, **k):
            raise _rq.ConnectionError("offline")

        def request(self, *a, **k):
            raise _rq.ConnectionError("offline")

    cli = BitgetLiveClient(None, session=DeadSession(), max_retries=2)
    with pytest.raises(BitgetError):
        cli.tickers()
    assert cli.api_errors == 1                  # API 错熔断计数(runner 侧读)


# ---------------------------------------------------------------------------
# ③ 宇宙过滤边界恰 3M(严格大于)
# ---------------------------------------------------------------------------


def test_universe_boundary_exact_3m():
    cli = BitgetLiveClient(None)
    eps = 1e-9 * UNIVERSE_MIN_USDT_VOLUME_24H
    ticks = [
        {"symbol": "BELOW", "usdtVolume": f"{3_000_000 - 1}"},
        {"symbol": "EXACT", "usdtVolume": "3000000"},        # 恰 3M ⇒ 排除
        {"symbol": "ABOVE", "usdtVolume": f"{3_000_000 + 1}"},
        {"symbol": "FALLBACK_BV", "baseVolume": "100", "lastPr": "40000"},
        {"symbol": "FALLBACK_LOW", "baseVolume": "100", "lastPr": "10"},
        {"symbol": "QUOTE_OK", "quoteVolume": "3000000000000"},
    ]
    eps_t = [{"symbol": "EPS", "usdtVolume": f"{3_000_000 + eps}"}]
    uni = cli.universe(tickers=ticks + eps_t)
    assert uni == ["ABOVE", "EPS", "FALLBACK_BV", "QUOTE_OK"]
    assert "EXACT" not in uni and "BELOW" not in uni
    # 自定义阈值同口径(严格大于)
    uni2 = cli.universe(4_000_000.0, tickers=ticks + eps_t)
    assert "FALLBACK_BV" not in uni2               # 恰 4M ⇒ 排除
    assert "QUOTE_OK" in uni2


# ---------------------------------------------------------------------------
# ④ 熔断条件
# ---------------------------------------------------------------------------


def test_daily_loss_breaker_threshold_and_reset():
    br = lr.DailyLossBreaker(0.10)
    t0 = 1_797_000_000_000                     # 某 UTC 日正午
    br.update(t0, 1000.0)
    assert not br.should_stop(900.5)           # -9.95%:不熔
    assert br.should_stop(900.0)               # 恰 -10% inclusive:熔断
    assert br.tripped
    assert br.should_stop(950.0)               # 熔后滞留(不自动复位)
    br.update(t0 + 86_400_000 * 2, 800.0)      # UTC 日界:新起点复位
    assert not br.tripped and not br.should_stop(750.0)
    assert br.should_stop(720.0)


def test_rate_limiter_hourly_cap():
    rl = lr.RateLimiter(3)
    base = 1_000_000.0
    assert all(rl.allow(base + i) for i in range(3))
    assert rl.pending(base + 3) == 3
    assert not rl.allow(base + 3)              # 第 4 单到限:拒
    assert rl.pending(base + 3599) == 3
    assert rl.allow(base + 3601)               # 滚出窗口:放行


# ---------------------------------------------------------------------------
# ⑤ 对账
# ---------------------------------------------------------------------------


def test_reconcile_positions_buckets():
    ours = {"BTCUSDT": {"symbol": "BTCUSDT", "direction": 1, "qty": 0.01},
            "ETHUSDT": {"symbol": "ETHUSDT", "direction": -1, "qty": 0.5}}
    ex = [{"symbol": "BTCUSDT", "direction": 1, "qty": 0.0100000001,
           "mark_price": 100.0},
          {"symbol": "SOLUSDT", "direction": 1, "qty": 3.0,
           "mark_price": 10.0}]
    out = lr.reconcile_positions(ours, ex)
    assert any(m.startswith("ours_only ETHUSDT") for m in out)
    assert any(m.startswith("exchange_only SOLUSDT") for m in out)
    assert not any("BTCUSDT" in m for m in out)     # 1e-6 相对容差内
    ex[0]["qty"] = 0.03
    out2 = lr.reconcile_positions(ours, ex)
    assert any(m.startswith("qty_mismatch BTCUSDT") for m in out2)


# ---------------------------------------------------------------------------
# ⑥ 帧聚合
# ---------------------------------------------------------------------------


def _bar2h(label, o, h, l, c, v=1.0):
    return (label, o, h, l, c, v)


def test_aggregate_12h_partial_tail_and_hole():
    step2, step12 = 7_200_000, 43_200_000
    t0 = 1_796_400_000_000 - (1_796_400_000_000 % step12)
    full = [_bar2h(t0 + k * step2, 10 + k, 12 + k, 9 + k, 11 + k)
            for k in range(6)]
    got = lr.aggregate_12h(full)
    assert got == [(t0, 10.0, 17.0, 9.0, 16.0, 6.0)]
    partial = full + [_bar2h(t0 + step12, 20, 21, 19, 20)]
    assert lr.aggregate_12h(partial) == got          # 末桶不齐:丢
    bars = []
    for k in range(6):
        bars.append(_bar2h(t0 + k * step2, 1, 2, 0, 1))
    for j, k in enumerate((0, 1, 2, 4, 5)):          # 第二桶缺 k=3
        bars.append(_bar2h(t0 + step12 + k * step2, 3, 4, 2, 3))
    bars.append(_bar2h(t0 + 2 * step12, 5, 6, 4, 5))  # 尾桶(防与缺根桶混淆)
    with pytest.raises(RuntimeError):
        lr.aggregate_12h(bars)


# ---------------------------------------------------------------------------
# ⑦ mic 认知回放 == prepare_cognition(实盘在册数据锚;canonical 硬约)
# ---------------------------------------------------------------------------


def test_replay_cognition_parity_prepare_cognition():
    from market_operator.replay import prepare_cognition
    from market_operator.events import EventDetector
    import pandas as pd

    df = pd.read_csv(ROOT / "data" / "raw" / "BTCUSDT_2h.csv",
                     dtype={"ts": "int64"}).tail(600)
    trig = [(int(r.ts), float(r.open), float(r.high), float(r.low),
             float(r.close), float(r.volume))
            for r in df[["ts", "open", "high", "low", "close", "volume"]]
            .itertuples(index=False)]
    for a, b in zip(trig, trig[1:]):
        assert b[0] - a[0] == 7_200_000, "在册 2h 帧网格"
    _raw, params, _rp, _rp_by_tf = lr.load_strategy_params()
    cog = lr.aggregate_12h(trig)
    det, trk = lr.replay_cognition(params, "BTCUSDT", cog, trig,
                                   obs_tf_seconds=43200,
                                   trigger_tf_seconds=7200)
    # replay 所得检测器重放 bar 流 == prepare_cognition 同帧逐位
    det2 = EventDetector(params, enable_e7=True)
    got = {}
    for i, bar in enumerate(cog):
        e = det2.update(bar)
        if e:
            got[i] = [(x.id, x.direction) for x in e]
    df_cog = pd.DataFrame(cog, columns=["ts", "open", "high", "low", "close",
                                        "volume"])
    ref = prepare_cognition(df_cog, params, "BTCUSDT", enable_e7=True)
    want = {i: [(x.id, x.direction) for x in evs]
            for i, evs in ref.events_by.items()}
    assert got == want
    assert trk._ctx is not None          # mic 追踪器接续:末语境在场


# ---------------------------------------------------------------------------
# ⑧ MODE fail-closed / ⑨ 幂等与 worst-case
# ---------------------------------------------------------------------------


def _cfg_min() -> dict:
    return json.loads((ROOT / "config" / "live.json").read_text("utf-8"))


def test_mode_gate_fail_closed(tmp_path):
    with pytest.raises(RuntimeError):
        lr.LiveRunner("sandbox", _cfg_min(), object(), object(),
                      state_path=tmp_path / "s.json",
                      journal_dir=tmp_path / "j")
    with pytest.raises(SystemExit):
        lr.parse_args([])                        # --mode 缺省 ⇒ 拒绝启动
    args = lr.parse_args(["--mode", "paper"])
    assert args.mode == "paper"


def test_live_config_has_no_engine_switch():
    """单一生产链:配置里不再保留 engine 开关(模型层不允许选旧链)。"""
    assert "engine" not in _cfg_min()["cognition"]


def test_load_strategy_params_requires_balance_detector(tmp_path):
    """策略配置没有平衡箱 ⇒ 没有事件来源 ⇒ 直接拒绝,而不是退回 v20 语义。"""
    strat = json.loads((ROOT / "config" / "strategy.json").read_text("utf-8"))
    strat["use_balance_detector"] = False
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(strat), encoding="utf-8")
    with pytest.raises(ValueError, match="use_balance_detector"):
        lr.load_strategy_params(path)


def test_new_position_manager_rejects_non_balance_config():
    with pytest.raises(ValueError, match="use_balance_detector"):
        lr.new_position_manager(lr.Params(), {})


def test_runner_rejects_retired_engine(tmp_path):
    """旧配置残留 engine=v20:启动即失败(不静默降级到旧链)。"""
    cfg = _cfg_min()
    cfg["cognition"]["engine"] = "v20"
    with pytest.raises(RuntimeError, match="v20"):
        lr.LiveRunner("paper", cfg, object(), object(),
                      state_path=tmp_path / "s.json",
                      journal_dir=tmp_path / "j")


def test_client_oid_idempotent_and_short():
    a = lr.client_oid_of("mo20", "BTCUSDT", "LEVEL_REJECTED", "buy", 1_796_400_000_000)
    b = lr.client_oid_of("mo20", "BTCUSDT", "LEVEL_REJECTED", "buy", 1_796_400_000_000)
    c = lr.client_oid_of("mo20", "BTCUSDT", "LEVEL_REJECTED", "sell", 1_796_400_000_000)
    assert a == b and a != c and len(a) <= 36


def test_worst_case_of_position():
    wc = lr.worst_case_of_position(1, 1.0, 100.0, 90.0, 0.0006)
    assert wc == pytest.approx(10.0 + 0.06)      # 止损实现 + 平仓费预算
    assert lr.worst_case_of_position(-1, 1.0, 100.0, 110.0, 0.0006) == \
        pytest.approx(10.0 + 0.06)
    # 有利侧止损(移过入场):最坏情景 ≥ 0(费用预算仍在)
    assert lr.worst_case_of_position(1, 1.0, 100.0, 101.0, 0.0006) == \
        pytest.approx(0.06)


# ---------------------------------------------------------------------------
# ⑩ 掉线补做(gap recovery):失联期间的止损/止盈必须被补结算
# ---------------------------------------------------------------------------

NOW = 1_796_400_000_000          # 固定时刻,避免测试依赖挂钟


class _FakeTG:
    def __init__(self) -> None:
        self.alerts: list[str] = []
        self.fills: list[dict] = []

    def send_alert(self, msg, **_kw):
        self.alerts.append(str(msg))

    def send_fill(self, sym, title, qty, price, **kw):
        self.fills.append({"symbol": sym, "title": title, "qty": qty,
                           "price": price, **kw})


class _FakeClient:
    """只实现补做/止损路径用到的接口;未实现的调用会直接 AttributeError。"""

    def __init__(self, positions=(), pending=None):
        self._positions = list(positions)
        self._pending = pending if pending is not None else {"entrustedList": []}
        self.market_orders: list[dict] = []
        self.plan_placed: list[dict] = []
        self.plan_canceled: list[tuple] = []

    def positions(self):
        return list(self._positions)

    def plan_orders_pending(self):
        return self._pending

    def place_market_order(self, symbol, **kw):
        self.market_orders.append({"symbol": symbol, **kw})
        return {"orderId": f"M{len(self.market_orders)}"}

    def place_stop_market_plan(self, symbol, **kw):
        self.plan_placed.append({"symbol": symbol, **kw})
        return {"orderId": f"P{len(self.plan_placed)}"}

    def cancel_plan_order(self, symbol, order_id):
        self.plan_canceled.append((symbol, str(order_id)))
        return {}

    def recent_fills(self, symbol, limit=20):
        return []


def _pos(sym="BTCUSDT", direction=1, entry=100.0, invalid=95.0, qty=1.0,
         mark=101.0, tf="2h"):
    return {"symbol": sym, "direction": direction, "entry": entry,
            "invalid": invalid, "qty": qty, "mark": mark, "tf_label": tf,
            "event_id": "EV1", "target_1": 110.0,
            "box_lo": None, "box_hi": None, "opened_ts": 0}


def _runner(tmp_path, client, tg, *, mode="live", cfg=None):
    return lr.LiveRunner(mode, cfg or _cfg_min(), client, tg,
                         state_path=tmp_path / "state.json",
                         journal_dir=tmp_path / "logs",
                         contracts={}, symbols=())


def test_gap_settle_missed_stop_forces_market_exit(tmp_path):
    """缺口内已触及固定止损而交易所仍有仓 ⇒ 市价离场(而不是重挂止损)。"""
    client, tg = _FakeClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=99.0)
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "OLD"}
    r._gap_stop_hit = {"BTCUSDT": {"ts": NOW - 3600_000, "stop": 95.0,
                                   "bar_close": 96.0}}

    r._gap_settle(NOW, 6 * 3600)

    assert [o["symbol"] for o in client.market_orders] == ["BTCUSDT"]
    assert client.market_orders[0]["side"] == "buy"        # hedge:平多=buy+close
    assert client.market_orders[0]["trade_side"] == "close"
    assert client.market_orders[0]["size"] == pytest.approx(1.0)
    assert client.plan_canceled == [("BTCUSDT", "OLD")]    # 旧保护单必须撤
    assert "BTCUSDT" not in r.positions
    assert r.counters["gap_stop_exits"] == 1
    assert any("缺口内已触及止损" in a for a in tg.alerts)


def test_gap_settle_breached_defense_exits_without_stop_hit(tmp_path):
    """补放把防线推紧后现价已在防线外:市价离场,不挂必然即刻触发的计划单。"""
    client, tg = _FakeClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["ETHUSDT"] = _pos(sym="ETHUSDT", direction=-1, entry=100.0,
                                  invalid=104.0, mark=105.0)

    r._gap_settle(NOW, 8 * 3600)

    assert [o["symbol"] for o in client.market_orders] == ["ETHUSDT"]
    assert client.market_orders[0]["side"] == "sell"       # hedge:平空=sell+close
    assert client.plan_placed == []                        # 不重挂止损
    assert any("已被现价" in a for a in tg.alerts)


def test_gap_settle_no_breach_takes_no_action(tmp_path):
    """防线未被越过:不产生任何订单,仅报告补做完成。"""
    client, tg = _FakeClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["SOLUSDT"] = _pos(sym="SOLUSDT", invalid=95.0, mark=101.0)

    r._gap_settle(NOW, 3600)

    assert client.market_orders == [] and client.plan_placed == []
    assert r.counters["gap_recoveries"] == 1
    assert any("无止损越界需追平" in a for a in tg.alerts)


def test_gap_settle_stop_policy_rearm_keeps_position(tmp_path):
    """stop_policy=rearm:只告警并按最新防线重挂,不动仓位。"""
    cfg = _cfg_min()
    cfg["loop"]["gap_recovery"]["stop_policy"] = "rearm"
    client, tg = _FakeClient(), _FakeTG()
    r = _runner(tmp_path, client, tg, cfg=cfg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=99.0)
    r._gap_stop_hit = {"BTCUSDT": {"ts": NOW - 3600_000, "stop": 95.0,
                                   "bar_close": 96.0}}

    r._gap_settle(NOW, 6 * 3600)

    assert client.market_orders == []
    assert "BTCUSDT" in r.positions
    assert any("stop_policy=rearm" in a for a in tg.alerts)


def test_manage_stops_replaces_stop_missing_on_exchange(tmp_path):
    """本地有记录但交易所已无该计划单(未越界)⇒ 重挂,而不是当作已保护。"""
    client = _FakeClient(pending={"entrustedList": [{"orderId": "OTHER"}]})
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=101.0)
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "GONE"}

    r._manage_stops(NOW)

    assert client.plan_canceled == []          # 已不在挂单列表:无需撤
    assert len(client.plan_placed) == 1
    assert r.stops["BTCUSDT"]["order_id"] == "P1"
    assert r.counters["stop_missing"] == 1


def test_manage_stops_keeps_stop_when_still_pending(tmp_path):
    """交易所在挂且触发价/数量一致 ⇒ 不撤不挂(避免每轮撤挂抖动)。"""
    client = _FakeClient(pending={"entrustedList": [{"orderId": "S1"}]})
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=101.0)
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "S1"}

    r._manage_stops(NOW)

    assert client.plan_placed == [] and client.plan_canceled == []
    assert r.stops["BTCUSDT"]["order_id"] == "S1"


def test_manage_stops_missing_and_breached_exits_at_market(tmp_path):
    """保护单丢了且现价已越过防线 ⇒ 市价离场(重挂只会立刻以更差价成交)。"""
    client = _FakeClient(pending={"entrustedList": []})
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=100.0, mark=99.5)
    r.stops["BTCUSDT"] = {"trigger": 100.0, "qty": 1.0, "order_id": "GONE"}

    r._manage_stops(NOW)

    assert [o["symbol"] for o in client.market_orders] == ["BTCUSDT"]
    assert client.plan_placed == []            # 不再挂必然即刻触发的止损
    assert "BTCUSDT" not in r.positions


def test_manage_stops_cancels_duplicate_stop_orders(tmp_path):
    """同一持仓留有多张自有计划单 ⇒ 只留本地记录的那张,其余撤掉。"""
    pending = {"entrustedList": [
        {"orderId": "KEEP", "symbol": "BTCUSDT", "clientOid": "mo21aaaaaaaaaa",
         "triggerPrice": "95"},
        {"orderId": "DUP1", "symbol": "BTCUSDT", "clientOid": "mo21bbbbbbbbbb",
         "triggerPrice": "98"},
        {"orderId": "FOREIGN", "symbol": "BTCUSDT", "clientOid": "zz999",
         "triggerPrice": "90"},
    ]}
    client = _FakeClient(pending=pending)
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=101.0)
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "KEEP"}

    r._manage_stops(NOW)

    assert client.plan_canceled == [("BTCUSDT", "DUP1")]   # 异己单不动
    assert client.plan_placed == []
    assert r.counters["stop_dupes"] == 1
    assert r.stops["BTCUSDT"]["order_id"] == "KEEP"


def test_manage_stops_dedupe_skips_symbols_without_position(tmp_path):
    """无持仓品种的遗留计划单不归本函数处置(不误撤)。"""
    pending = {"entrustedList": [
        {"orderId": "A", "symbol": "ZZZUSDT", "clientOid": "mo2111111111"},
        {"orderId": "B", "symbol": "ZZZUSDT", "clientOid": "mo2122222222"},
    ]}
    client = _FakeClient(pending=pending)
    r = _runner(tmp_path, client, _FakeTG())
    r.stops["ZZZUSDT"] = {"trigger": 1.0, "qty": 1.0, "order_id": "A"}

    r._manage_stops(NOW)

    assert client.plan_canceled == []


def test_manage_stops_replace_survives_cancel_failure(tmp_path):
    """撤旧单失败(旧单已成交/不存在)不得阻断新单 —— 否则仓位长期失保。

    修前缺陷:撤旧单的 BitgetError 跳到外层 except ⇒ 只记 stop_error 不挂
    新单;下一轮同样撤不掉 ⇒ 同一异常每轮复现,仓位在无人察觉中裸奔。
    """
    pending = {"entrustedList": [
        {"orderId": "DEAD", "symbol": "BTCUSDT", "clientOid": "mo21deadbeef",
         "triggerPrice": "90"}]}

    class _NoCancel(_FakeClient):
        def cancel_plan_order(self, symbol, order_id):
            raise BitgetError("43001 order does not exist")

    client = _NoCancel(pending=pending)
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=101.0)
    r.stops["BTCUSDT"] = {"trigger": 90.0, "qty": 1.0, "order_id": "DEAD"}

    r._manage_stops(NOW)

    assert [o["symbol"] for o in client.plan_placed] == ["BTCUSDT"]
    assert client.plan_placed[0]["trigger_price"] == 95.0
    assert r.stops["BTCUSDT"]["order_id"] == "P1"
    assert r.counters.get("stop_errors", 0) == 0


def test_recover_gap_clears_anchor_after_success(tmp_path):
    """追平后必须清锚:否则下次重启把几小时前的老缺口当成新缺口。

    实测:2026-10-23 补完 18.7h 缺口后锚仍留着,下一次重启(真实缺口 1 分钟)
    会照报"掉线缺口 19h",真缺口被假数字掩护。
    """
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r._gap_started_ms = NOW - 19 * 3600_000
    r._gap_bars = {}                      # 批次已空(补完 / 缺口内无新 bar)

    assert r.recover_gap(NOW) is True

    assert r._gap_started_ms is None
    assert r.counters["gap_recoveries"] == 1


def test_save_state_falls_back_when_replace_blocked(tmp_path, monkeypatch):
    """读者占用 state.json 时 replace 失败,也必须把这次状态写下去。

    否则 `os.replace` 抛 OSError ⇒ 整个 step 记一次错误 ⇒ 连续 20 次就退出,
    而磁盘上还是旧状态(持仓/止损过期)。
    """
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r._save_state()
    before = (tmp_path / "state.json").read_text(encoding="utf-8")

    def boom(*a, **k):
        raise PermissionError("locked by reader")

    monkeypatch.setattr(lr.os, "replace", boom)
    r.counters["fills"] += 1
    r._save_state()

    after = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert after["counters"]["fills"] == json.loads(before)["counters"]["fills"] + 1
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_state_is_atomic(tmp_path):
    """state.json 必须始终是完整 JSON:被 kill 时不得留下半截文件。

    修前 `write_text` 直接覆盖 => 写入中途被杀就留半截 ⇒ `_load_state` 解析
    失败 ⇒ 持仓/止损事实全丢(只能靠交易所侧计划单兜底)。
    """
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r._save_state()
    r._save_state()

    assert not (tmp_path / "state.json.tmp").exists()   # 临时文件不残留
    st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert st["mode"] == "live"
    assert st["saved_at_utc"]


def _fake_campaign(n_bars: int, tf="12h"):
    bars = [_bar2h(NOW - (n_bars - 1 - k) * 7_200_000, 1.0, 2.0, 0.5, 1.5)
            for k in range(n_bars)]
    return types.SimpleNamespace(pm=types.SimpleNamespace(positions={}),
                                 symbol="BTCUSDT", tf_label=tf, trig_bars=bars,
                                 cog_bars=bars[:2])


def test_save_state_splits_hot_and_campaigns(tmp_path):
    """热档每步写、冷档分离:state.json 不再内嵌 campaigns(修前 6.1MB/步)。"""
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r.campaigns["BTCUSDT"] = _fake_campaign(40)

    r._save_state()

    hot = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    cold = json.loads((tmp_path / "campaigns.json").read_text(encoding="utf-8"))
    assert "campaigns" not in hot
    assert hot["saved_at_utc"]
    assert sorted(cold["campaigns"]) == ["BTCUSDT"]


def test_campaigns_written_on_change_not_every_step(tmp_path):
    """冷档只在变更/到档期时写:常态每步只落热档 + 小心跳,不做全量重写。

    每步固定三笔原子写:state.json(热档)/ heartbeat.json(看护判活,<1KB,每步必写)
    / campaigns.json(冷档,仅变更或到档期)。心跳不能跟着冷档档期走,否则看护会
    把"正常但冷档未到档期"误判成停摆。
    """
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r.campaigns["BTCUSDT"] = _fake_campaign(40)
    writes: list[str] = []
    orig = r._write_json_atomic
    r._write_json_atomic = lambda p, s: (writes.append(Path(p).name),
                                         orig(p, s))[1]

    r._save_state()                       # 首次:dirty ⇒ 冷档也写
    assert writes == ["state.json", "heartbeat.json", "campaigns.json"]
    r._save_state()                       # 未变更且在档期内 ⇒ 只写热档 + 心跳
    assert writes == ["state.json", "heartbeat.json", "campaigns.json",
                      "state.json", "heartbeat.json"]

    r._touch_campaigns()                  # 防线推进/新 bar ⇒ 立即补写冷档
    r._save_state()
    assert writes[-3:] == ["state.json", "heartbeat.json", "campaigns.json"]


def test_campaigns_payload_keeps_only_tail_bars(tmp_path):
    """冷档只留尾部 state_bar_tail 根 bar:信息量由 K 线缓存+交易所补齐。"""
    cfg = _cfg_min()
    cfg["loop"]["state_bar_tail"] = 24
    r = _runner(tmp_path, _FakeClient(), _FakeTG(), cfg=cfg)
    r.campaigns["BTCUSDT"] = _fake_campaign(100)

    payload = json.loads(r._campaigns_payload())

    bars = payload["campaigns"]["BTCUSDT"]["trig_bars"]
    assert len(bars) == 24
    assert bars[-1][0] == NOW                     # 尾部对齐最新 bar
    assert payload["bar_tail"] == 24


def test_load_state_reads_cold_file(tmp_path, monkeypatch):
    """新格式:campaigns.json 存在时以其为准(热档里没有 campaigns)。"""
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r.campaigns["BTCUSDT"] = _fake_campaign(30)
    r._save_state()
    hot = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "campaigns" not in hot

    built: dict = {}

    def fake_build(self, sym, bars2h=None):
        built[sym] = bars2h
        return types.SimpleNamespace(pm=types.SimpleNamespace(positions={}),
                                     symbol=sym, trig_bars=bars2h)
    monkeypatch.setattr(lr.LiveRunner, "_prefetch_bars_parallel",
                        lambda self, syms, want, seed_by_sym=None:
                        {"BTCUSDT": _fake_campaign(30).trig_bars})
    monkeypatch.setattr(lr.LiveRunner, "_build_all_tf_campaigns", fake_build)
    monkeypatch.setattr(lr.LiveRunner, "_ensure_leverage",
                        lambda self, sym: None)

    r2 = _runner(tmp_path, _FakeClient(), _FakeTG())

    assert built["BTCUSDT"], "campaigns.json 未被读取"
    assert built["BTCUSDT"][-1][0] == NOW          # 切分锚来自冷档尾部 bar


def test_load_state_backward_compatible_with_embedded_campaigns(
        tmp_path, monkeypatch):
    """旧格式(state.json 内嵌 campaigns、无 campaigns.json)必须能直接读。"""
    (tmp_path / "state.json").write_text(json.dumps({
        "saved_at_utc": "2026-10-22T09:12:06+00:00",
        "positions": {}, "stops": {},
        "campaigns": {"BTCUSDT": {"trig_bars": [[NOW, 1, 2, 0.5, 1.5, 1.0]],
                                  "pm_positions": {}}},
    }), encoding="utf-8")
    built: dict = {}

    def fake_build(self, sym, bars2h=None):
        built[sym] = bars2h
        return types.SimpleNamespace(pm=types.SimpleNamespace(positions={}),
                                     symbol=sym, trig_bars=bars2h)
    monkeypatch.setattr(lr.LiveRunner, "_prefetch_bars_parallel",
                        lambda self, syms, want, seed_by_sym=None:
                        {"BTCUSDT": [[NOW, 1, 2, 0.5, 1.5, 1.0]]})
    monkeypatch.setattr(lr.LiveRunner, "_build_all_tf_campaigns", fake_build)
    monkeypatch.setattr(lr.LiveRunner, "_ensure_leverage",
                        lambda self, sym: None)

    r = _runner(tmp_path, _FakeClient(), _FakeTG())

    assert built["BTCUSDT"], "旧格式内嵌 campaigns 未被读取"
    assert sorted(r.campaigns) == ["BTCUSDT"]


def test_candle_cache_is_gzip_and_backward_compatible(tmp_path):
    """K 线缓存改 gzip(密度 ~1/4),旧明文缓存仍可读(无损迁移)。"""
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r.candle_cache_dir = tmp_path / "candles"
    bars = [_bar2h(NOW - (5 - k) * 7_200_000, 1.0, 2.0, 0.5, 1.5)
            for k in range(6)]

    r._save_cached_bars("BTCUSDT", bars)

    p = r._candle_cache_path("BTCUSDT")
    assert p.name.endswith(".json.gz") and p.exists()
    assert r._load_cached_bars("BTCUSDT") == [tuple(b) for b in bars]

    legacy = r._candle_cache_path("ETHUSDT", gz=False)
    legacy.write_text(json.dumps([list(b) for b in bars]), encoding="utf-8")
    assert r._load_cached_bars("ETHUSDT") == [tuple(b) for b in bars]


def test_gap_bars_live_in_cold_file(tmp_path):
    """未补完的缺口批次进冷档:长停机时热档仍是 22KB 心跳,不随批次膨胀。"""
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    r.campaigns["BTCUSDT"] = _fake_campaign(10)
    r._gap_bars = {"BTCUSDT": [_bar2h(NOW, 1.0, 2.0, 0.5, 1.5)]}
    r._touch_campaigns()

    r._save_state()

    hot = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    cold = json.loads((tmp_path / "campaigns.json").read_text(encoding="utf-8"))
    assert "gap_bars" not in hot
    assert [b[0] for b in cold["gap_bars"]["BTCUSDT"]] == [NOW]


def test_load_state_reads_gap_bars_from_cold(tmp_path, monkeypatch):
    """重启时从冷档恢复待补批次(热档不再承载 gap_bars)。"""
    (tmp_path / "state.json").write_text(json.dumps({
        "saved_at_utc": "2026-10-22T09:12:06+00:00",
        "positions": {}, "stops": {},
    }), encoding="utf-8")
    (tmp_path / "campaigns.json").write_text(json.dumps({
        "campaigns": {"BTCUSDT": {"trig_bars": [[NOW, 1, 2, 0.5, 1.5, 1.0]],
                                  "pm_positions": {}}},
        "gap_bars": {"BTCUSDT": [[NOW, 1, 2, 0.5, 1.5, 1.0]]},
    }), encoding="utf-8")
    monkeypatch.setattr(lr.LiveRunner, "_prefetch_bars_parallel",
                        lambda self, syms, want, seed_by_sym=None:
                        {"BTCUSDT": [[NOW, 1, 2, 0.5, 1.5, 1.0]]})
    monkeypatch.setattr(lr.LiveRunner, "_build_all_tf_campaigns",
                        lambda self, sym, bars2h=None: types.SimpleNamespace(
                            pm=types.SimpleNamespace(positions={}),
                            symbol=sym, trig_bars=bars2h))
    monkeypatch.setattr(lr.LiveRunner, "_ensure_leverage",
                        lambda self, sym: None)

    r = _runner(tmp_path, _FakeClient(), _FakeTG())

    assert [b[0] for b in r._gap_bars["BTCUSDT"]] == [NOW]


def test_load_state_splits_gap_bars_at_crash_bar(tmp_path, monkeypatch):
    """saved_at 之后形成的 bar 必须留作缺口批次,不得在装配时被直接吃掉。"""
    step = 7_200_000
    bars = [_bar2h(NOW - 5 * step + k * step, 1.0, 2.0, 0.5, 1.5)
            for k in range(6)]
    cut = bars[2][0]                       # 崩溃时刻的末根 bar
    st = {
        "saved_at_utc": "2026-10-22T09:12:06+00:00",
        "positions": {}, "stops": {},
        "campaigns": {"BTCUSDT": {
            "trig_bars": [list(b) for b in bars[:3]],
            "pm_positions": {}}},
    }
    (tmp_path / "state.json").write_text(json.dumps(st), encoding="utf-8")
    built: dict = {}

    def fake_build(self, sym, bars2h=None):
        built[sym] = bars2h
        return types.SimpleNamespace(pm=types.SimpleNamespace(positions={}),
                                     symbol=sym, trig_bars=bars2h)

    monkeypatch.setattr(lr.LiveRunner, "_prefetch_bars_parallel",
                        lambda self, syms, want, seed_by_sym=None:
                        {"BTCUSDT": bars})
    monkeypatch.setattr(lr.LiveRunner, "_build_all_tf_campaigns", fake_build)
    monkeypatch.setattr(lr.LiveRunner, "_ensure_leverage",
                        lambda self, sym: None)

    r = _runner(tmp_path, _FakeClient(), _FakeTG())

    assert built["BTCUSDT"][-1][0] == cut          # 认知帧以崩溃时刻为界
    assert [b[0] for b in r._gap_bars["BTCUSDT"]] == [b[0] for b in bars[3:]]
    assert r._gap_started_ms == lr._utc_ms("2026-10-22T09:12:06+00:00")


def test_route_step_suppresses_new_entries_during_gap_replay(tmp_path,
                                                             monkeypatch):
    """补放出的历史信号不得按现价追单(_gap_replay=True)。"""
    class _Router:
        def on_bar(self, *_a, **_kw):
            return [types.SimpleNamespace(
                kind="market", limit_price=101.0,
                hyp=types.SimpleNamespace(event_id="EV1", direction=1))]

    bar = _bar2h(NOW, 100.0, 102.0, 99.0, 101.0)
    cs = types.SimpleNamespace(symbol="BTCUSDT", tf_label="12h",
                               trig_bars=[bar], cog_bars=[bar], router=_Router())
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    submitted: list = []
    monkeypatch.setattr(r, "_submit_open",
                        lambda *a, **kw: submitted.append(a))

    r._gap_replay = True
    r._route_step(cs, 1.0, [], view=None, mic=False)
    assert submitted == []
    assert r.counters["gap_entry_skips"] == 1

    r._gap_replay = False
    r._route_step(cs, 1.0, [], view=None, mic=False)
    assert len(submitted) == 1

