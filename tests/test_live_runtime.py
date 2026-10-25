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
import time
import types
from datetime import datetime, timezone
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


def test_daily_loss_breaker_anchors_pre_gap_equity_on_restart():
    """跨日重启:日界锚取缺口前权益,而不是重启瞬间权益(否则缺口亏损被抹掉)。"""
    t0 = 1_797_000_000_000                     # 某 UTC 日(08:00 UTC 前)
    br = lr.DailyLossBreaker(0.05)
    br.update(t0, 100.0)
    br.update(t0 + 8 * 3600_000, 107.6)        # 当日最后一笔事实
    assert br.start_equity == 100.0

    # 停机跨日后再启动:state.json 只带回 day/start_equity/equity(→last_equity)
    nxt = lr.DailyLossBreaker(0.05)
    nxt.day, nxt.start_equity = br.day, br.start_equity
    nxt.last_equity = br.last_equity
    nxt.update(t0 + 86_400_000, 100.4)         # 重启瞬间权益已含缺口亏损

    assert nxt.start_equity == pytest.approx(107.6)
    assert not nxt.should_stop(102.3)          # -4.9%:当日预算仍在
    assert nxt.should_stop(102.2)              # 恰 -5.0% inclusive:熔断

    # 同日重启:锚不变(当日亏损必须继续累计)
    same = lr.DailyLossBreaker(0.05)
    same.day, same.start_equity = br.day, 100.0
    same.update(t0 + 2 * 3600_000, 95.0)
    assert same.start_equity == pytest.approx(100.0)

    # 停机多日:上一笔权益已不可比 ⇒ 只能按重启权益起算
    far = lr.DailyLossBreaker(0.05)
    far.day, far.start_equity = br.day, br.start_equity
    far.last_equity = br.last_equity
    far.update(t0 + 3 * 86_400_000, 80.0)
    assert far.start_equity == pytest.approx(80.0)


def test_daily_report_uses_previous_day_close_not_new_anchor(tmp_path):
    """日结必须报**昨日**锚/收盘:滚动后的新锚会把日盈亏算成 ≈0。"""
    tg = _DailyTG()
    r = _runner(tmp_path, _FakeClient(), tg)
    t0 = 1_797_000_000_000
    r.breaker.update(t0, 100.0)
    r.breaker.update(t0 + 8 * 3600_000, 107.6)
    r.equity = 107.7
    r._daily_date = _utc_day(t0)

    r.breaker.update(t0 + 86_400_000, 107.7)   # 主循环顺序:先滚 breaker
    r._daily_tick(t0 + 86_400_000)

    day, eq, pnl, _sig, _fills, _n, dd = tg.daily[-1]
    assert day == _utc_day(t0)
    assert eq == pytest.approx(107.6)          # 昨日最后一笔权益
    assert pnl == pytest.approx(7.6)
    assert dd == pytest.approx(-0.076)         # (anchor − close)/anchor


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


def _bar2h(label, o, h, low, c, v=1.0):
    return (label, o, h, low, c, v)


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


class _DailyTG(_FakeTG):
    """记录日结上报(其余同 _FakeTG)。"""

    def __init__(self) -> None:
        super().__init__()
        self.daily: list[tuple] = []

    def send_daily(self, date, equity, day_pnl, n_signals, n_fills,
                   n_positions, drawdown_frac):
        self.daily.append((date, equity, day_pnl, n_signals, n_fills,
                           n_positions, drawdown_frac))
        return True


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


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

    def tpsl_orders_pending(self):
        return self._pending

    def place_market_order(self, symbol, **kw):
        self.market_orders.append({"symbol": symbol, **kw})
        return {"orderId": f"M{len(self.market_orders)}"}

    def place_stop_loss(self, symbol, **kw):
        self.plan_placed.append({"symbol": symbol, **kw})
        return {"orderId": f"P{len(self.plan_placed)}"}

    def place_take_profit(self, symbol, **kw):
        return self.place_stop_loss(symbol, **kw)

    def cancel_tpsl_order(self, symbol, order_id):
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
    """交易所在挂且触发价一致 ⇒ 不撤不挂(避免每轮撤挂抖动)。"""
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


def test_manage_stops_keeps_stop_after_partial_close(tmp_path):
    """部分减仓不改 TPSL:pos_loss 是仓位级,交易所按触发时的整仓平,报文无 size。

    修前:数量变动即撤旧换新 ⇒ 每次减仓都开一个"撤成功、挂失败"的失保窗口,
    而新单在数量上其实毫无必要。
    """
    client = _FakeClient(pending={"entrustedList": [{"orderId": "S1"}]})
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=101.0, qty=0.4)
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "S1"}

    r._manage_stops(NOW)

    assert client.plan_placed == [] and client.plan_canceled == []
    assert r.stops["BTCUSDT"]["order_id"] == "S1"


def test_manage_stops_places_position_level_tpsl_without_size(tmp_path):
    """新挂保护单必须是仓位级 TPSL:不带 size/side/tradeSide,带 holdSide。"""
    client = _FakeClient(pending={"entrustedList": []})
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=101.0)

    r._manage_stops(NOW)

    call = client.plan_placed[0]
    assert call["trigger_price"] == 95.0
    assert call["hold_side"] == "long"
    assert "size" not in call and "side" not in call \
        and "trade_side" not in call
    assert r.stops["BTCUSDT"]["order_id"] == "P1"


def test_manage_stops_treats_missing_order_id_as_failure(tmp_path):
    """挂单接口"成功"却无 orderId = 实际没挂:必须记 stop_error,不能当已保护。

    修前:`str(resp.get("orderId") or "")` 落成空串,`stops` 仍写入 ⇒ 下一轮
    missing 判定前本地声称有保护,裸奔到 TTL 过期才被发现。
    """
    class _NoId(_FakeClient):
        def place_stop_loss(self, symbol, **kw):
            self.plan_placed.append({"symbol": symbol, **kw})
            return {}

    client = _NoId(pending={"entrustedList": []})
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=101.0)

    r._manage_stops(NOW)

    assert "BTCUSDT" not in r.stops
    assert r.counters["stop_errors"] == 1


def test_manage_stops_replace_survives_cancel_failure(tmp_path):
    """撤旧单失败(旧单已成交/不存在)不得阻断新单 —— 否则仓位长期失保。

    修前缺陷:撤旧单的 BitgetError 跳到外层 except ⇒ 只记 stop_error 不挂
    新单;下一轮同样撤不掉 ⇒ 同一异常每轮复现,仓位在无人察觉中裸奔。
    """
    pending = {"entrustedList": [
        {"orderId": "DEAD", "symbol": "BTCUSDT", "clientOid": "mo21deadbeef",
         "triggerPrice": "90"}]}

    class _NoCancel(_FakeClient):
        def cancel_tpsl_order(self, symbol, order_id):
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

    _runner(tmp_path, _FakeClient(), _FakeTG())    # 构造即读 campaigns.json(本测试的副作用)

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


# ---------------------------------------------------------------------------
# ⑩ 离场执行者(P0)/ 离场分型(P1)/ 成交回执(P2)
# ---------------------------------------------------------------------------


class _FillClient(_FakeClient):
    """可注入成交回执的假客户端(离场回执路径用);market 单可开关失败。"""

    def __init__(self, fills=(), **kw):
        super().__init__(**kw)
        self.fills = list(fills)
        self.fail_market = False
        self.market_attempts = 0

    def recent_fills(self, symbol, limit=20):
        return list(self.fills)

    def place_market_order(self, symbol, **kw):
        self.market_attempts += 1
        if self.fail_market:
            raise BitgetError("market rejected")
        return super().place_market_order(symbol, **kw)


def _journal_rows(tmp_path: Path) -> list[dict]:
    rows: list[dict] = []
    for p in sorted((tmp_path / "logs").glob("*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _row(tmp_path: Path, **match) -> dict:
    for r in _journal_rows(tmp_path):
        if all(r.get(k) == v for k, v in match.items()):
            return r
    raise AssertionError(f"journal 无此行:{match}")


def test_stop_execution_evidence_is_one_sided():
    """止损市价单只能“不优于触发价”成交:优于它 ⇒ 一定不是这张挂单。

    反向判据(≥ −tol)会把每一次盈利离场都归因成移动止损 —— 比不分类更危险。
    """
    assert lr.stop_execution_evidence(1, 94.0, 95.0)          # 多头越价成交
    assert lr.stop_execution_evidence(1, 95.0, 95.0)          # 恰在触发价
    assert not lr.stop_execution_evidence(1, 104.0, 95.0)     # 优于止损 ⇒ 非本单
    assert lr.stop_execution_evidence(-1, 106.0, 105.0)       # 空头越价成交
    assert not lr.stop_execution_evidence(-1, 90.0, 105.0)    # 空头优于止损


def test_exit_label_matrix():
    assert lr.exit_label(executor="protective_stop", pnl=12.0) == "止盈(移动止损触发)"
    assert lr.exit_label(executor="protective_stop", pnl=-3.0) == "止损(保护单触发)"
    assert lr.exit_label(executor="external", pnl=12.0).startswith("离场确认(外部平仓·盈利")
    assert lr.exit_label(executor="external", pnl=-1.0).startswith("离场确认(外部平仓·亏损")
    assert lr.exit_label(executor="unknown", pnl=None).startswith("离场确认")
    assert lr.exit_label(executor="local_market", pnl=5.0,
                         reason="sub_bar_lock_exhausted").startswith("止盈")


def test_lock_exhausted_live_sends_market_exit_once(tmp_path):
    """P0:回撤预算耗尽必须由**本进程**市价离场,且同一仓只发一次。"""
    client, tg = _FillClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=94.0, qty=2.0, mark=106.0)
    r.stops["BTCUSDT"] = {"trigger": 94.0, "qty": 2.0, "order_id": "S1"}
    cs = types.SimpleNamespace(symbol="BTCUSDT")
    pm = types.SimpleNamespace(direction=1, invalid=94.0, entry=100.0,
                               target_1=110.0)

    r._sub_bar_close_out(cs, pm, r.positions["BTCUSDT"], 1_700_000_000_000,
                         106.0, why="sub_bar_lock_exhausted",
                         route="LOCK_EXIT|giveback=4 budget=7.5",
                         executor="local_market")

    assert len(client.market_orders) == 1
    od = client.market_orders[0]
    assert od["symbol"] == "BTCUSDT"
    assert od["side"] == r.close_side_of(1)          # hedge_mode 语义由 runner 决定
    assert od["trade_side"] == r.close_trade_side()
    assert od["size"] == 2.0
    assert "S1" in [c[1] for c in client.plan_canceled]   # 先撤保护单,避免双平
    assert "BTCUSDT" not in r.positions              # 成交即清仓(幂等的根据)
    assert r.counters["lock_exits"] == 1
    assert r.counters["exits"] == 1
    assert tg.fills[-1]["title"].startswith("止盈")
    assert _row(tmp_path, kind="exit_submit")["qty"] == 2.0

    # 下一根 bar(即使调用方仍拿旧字段)不得再发一张:已无此仓
    r._sub_bar_close_out(cs, pm, {"symbol": "BTCUSDT"}, 1_700_000_007_200,
                         104.0, why="sub_bar_lock_exhausted",
                         route="LOCK_EXIT|again", executor="local_market")
    assert len(client.market_orders) == 1



def test_lock_exhausted_paper_stays_virtual(tmp_path):
    """paper 路径不受 P0 影响:绝不发真单,仍按参考价虚拟结算。"""
    client, tg = _FillClient(), _FakeTG()
    r = lr.LiveRunner("paper", _cfg_min(), client, tg,
                      state_path=tmp_path / "state.json",
                      journal_dir=tmp_path / "logs", equity0=1000.0,
                      contracts={}, symbols=())
    pm = types.SimpleNamespace(direction=1, invalid=94.0, entry=100.0,
                               target_1=110.0)
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=94.0, qty=1.0, mark=106.0)
    cs = types.SimpleNamespace(
        symbol="BTCUSDT", pm=types.SimpleNamespace(positions={"BTCUSDT": pm}))

    r._sub_bar_close_out(cs, pm, r.positions["BTCUSDT"], 1_700_000_000_000,
                         106.0, why="sub_bar_lock_exhausted", route="LOCK_EXIT",
                         executor="local_market")

    assert client.market_orders == []
    assert "BTCUSDT" not in r.positions
    assert cs.pm.positions == {}
    assert r.equity > 1000.0


def test_protective_stop_with_resting_order_only_reconciles(tmp_path):
    """执行者是交易所时**不得**再发市价单(仓位可能已成交 ⇒ 会反向开仓)。"""
    client, tg = _FillClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=94.0)
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "S1"}
    pm = types.SimpleNamespace(direction=1, invalid=95.0, entry=100.0,
                               target_1=110.0)

    r._sub_bar_close_out(types.SimpleNamespace(symbol="BTCUSDT"), pm,
                         r.positions["BTCUSDT"], 1_700_000_000_000, 95.0,
                         why="sub_bar_protective_stop", route="STOP",
                         executor="exchange_stop")

    assert client.market_orders == []
    assert r.counters["unprotected_stop_exits"] == 0
    assert r._missing_streak.get("BTCUSDT") == 1      # 只推了一轮对账(待下轮确认)


def test_protective_stop_without_resting_order_market_exits(tmp_path):
    """防线上没有在挂的保护单 = 没有任何挂单会执行 ⇒ 本进程补市价单。"""
    client, tg = _FillClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=94.0)
    pm = types.SimpleNamespace(direction=1, invalid=95.0, entry=100.0,
                               target_1=110.0)

    r._sub_bar_close_out(types.SimpleNamespace(symbol="BTCUSDT"), pm,
                         r.positions["BTCUSDT"], 1_700_000_000_000, 95.0,
                         why="sub_bar_protective_stop", route="STOP",
                         executor="exchange_stop")

    assert len(client.market_orders) == 1
    assert r.counters["unprotected_stop_exits"] == 1
    assert tg.fills[-1]["title"] == "止损(无保护单·市价离场)"
    assert _row(tmp_path, kind="stop_unprotected_exit")["invalid"] == 95.0


def test_lock_exit_market_failure_alerts_and_keeps_position(tmp_path):
    """下单失败不能装成已平:仓位仍在、保护单待重挂、告警必须说清。"""
    client, tg = _FillClient(), _FakeTG()
    client.fail_market = True
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=94.0, qty=1.0, mark=106.0)
    r.stops["BTCUSDT"] = {"trigger": 94.0, "qty": 1.0, "order_id": "S1"}

    ok = r._live_market_exit(r.positions["BTCUSDT"], ts=1_700_000_000_000,
                             why="sub_bar_lock_exhausted", route="LOCK_EXIT",
                             ref_px=106.0)

    assert ok is False
    assert "BTCUSDT" in r.positions                  # 仓位仍在(不得静默清账)
    assert r.stops.get("BTCUSDT") is None            # 已撤 ⇒ 下一拍 _manage_stops 重挂
    assert r.counters["exit_submit_errors"] == 1
    assert r.counters["lock_exits"] == 0
    assert "决策型离场下单失败" in tg.alerts[-1]



def test_lock_exit_failure_is_retried_next_step(tmp_path):
    """失败必须**下一步**就重试(不能等下一根 2H bar 才重试,最长 2h 裸奔)。"""
    client, tg = _FillClient(), _FakeTG()
    client.fail_market = True
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=94.0, qty=1.0, mark=106.0)
    r.stops["BTCUSDT"] = {"trigger": 94.0, "qty": 1.0, "order_id": "S1"}

    assert r._live_market_exit(r.positions["BTCUSDT"], ts=1_700_000_000_000,
                               why="sub_bar_lock_exhausted", route="LOCK_EXIT",
                               ref_px=106.0) is False
    assert r._exit_retry["BTCUSDT"]["attempts"] == 0

    client.fail_market = False
    r._retry_exits(1_700_000_015_000)

    assert client.market_attempts == 2            # 首发(失败) + 重试(成功)
    assert len(client.market_orders) == 1
    assert "BTCUSDT" not in r.positions
    assert r._exit_retry == {}
    assert r.counters["lock_exits"] == 1


def test_lock_exit_gives_up_after_retry_cap(tmp_path):
    """重试用完仍是失败 ⇒ 必须发一条到人的终态告警并停止重试(不再静默)。"""
    client, tg = _FillClient(), _FakeTG()
    client.fail_market = True
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=94.0, qty=1.0, mark=106.0)
    r._live_market_exit(r.positions["BTCUSDT"], ts=1_700_000_000_000,
                        why="sub_bar_lock_exhausted", route="LOCK_EXIT",
                        ref_px=106.0)
    for i in range(r.MAX_EXIT_SUBMIT_RETRIES):
        r._retry_exits(1_700_000_015_000 + i * 15_000)

    assert r._exit_retry == {}
    assert "BTCUSDT" in r.positions               # 仍未平:必须由人接手
    assert "请立即人工平仓" in tg.alerts[-1]
    assert _row(tmp_path, kind="exit_submit_giveup")["symbol"] == "BTCUSDT"
    assert r.counters["exit_submit_errors"] == 1 + r.MAX_EXIT_SUBMIT_RETRIES


def test_close_fill_fact_picks_newest_order_vwap(tmp_path):
    """P2:必须取**最新**那笔平仓单,并按 size 聚合;顺序与时间窗都要对。"""
    fills = [
        {"symbol": "BABAUSDT", "tradeSide": "close", "price": "110.47",
         "size": "1", "cTime": "1699000000000", "orderId": "OLD"},
        {"symbol": "BABAUSDT", "tradeSide": "close", "price": "104",
         "size": "0.5", "cTime": "1700000900000", "orderId": "NEW"},
        {"symbol": "BABAUSDT", "tradeSide": "close", "price": "106",
         "size": "0.5", "cTime": "1700000900500", "orderId": "NEW"},
        {"symbol": "BABAUSDT", "tradeSide": "open", "price": "100", "size": "1",
         "cTime": "1700000990000", "orderId": "ENTRY"},
    ]
    r = _runner(tmp_path, _FillClient(fills=fills), _FakeTG())
    px, ts = r._close_fill_fact("BABAUSDT")
    assert px == pytest.approx(105.0)                # (104×0.5 + 106×0.5) / 1
    assert ts == 1_700_000_900_500

    # 端点若按正序返回(旧→新),结论必须一致(不假定列表顺序)
    r2 = _runner(tmp_path, _FillClient(fills=list(reversed(fills))), _FakeTG())
    assert r2._close_fill_fact("BABAUSDT")[0] == pytest.approx(105.0)

    # 时间窗:开仓之前那笔平仓不许算进来(否则拿上一笔仓的价格冒充)
    assert r._close_fill_fact("BABAUSDT",
                              opened_ts_ms=1_700_001_000_000) is None


def test_reconcile_closed_labels_loss_as_protective_stop(tmp_path):
    client = _FillClient(fills=[{"symbol": "BTCUSDT", "tradeSide": "close",
                                 "price": "94.0", "size": "1",
                                 "cTime": "1700000100000", "orderId": "C1"}])
    tg = _FakeTG()
    r = _runner(tmp_path, client, tg)
    pos = _pos(entry=100.0, invalid=95.0, qty=1.0, mark=94.0)
    pos["opened_ts"] = 1_700_000_000_000
    r.positions["BTCUSDT"] = pos
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "S1"}

    r._reconcile_closed("BTCUSDT", pos)

    row = _row(tmp_path, kind="reconcile_closed")
    assert row["executor"] == "protective_stop"
    assert row["exit_price"] == 94.0
    assert row["exit_ts_ms"] == 1_700_000_100_000
    assert row["pnl"] < 0
    assert tg.fills[-1]["title"] == "止损(保护单触发)"


def test_reconcile_closed_labels_profit_above_stop_as_external(tmp_path):
    """成交优于保护单 ⇒ 不是本系统止损:如实标成外部平仓(盈利),不冒充止盈。"""
    client = _FillClient(fills=[{"symbol": "BTCUSDT", "tradeSide": "close",
                                 "price": "104.0", "size": "1",
                                 "cTime": "1700000100000", "orderId": "C1"}])
    tg = _FakeTG()
    r = _runner(tmp_path, client, tg)
    pos = _pos(entry=100.0, invalid=95.0, qty=1.0, mark=104.0)
    pos["opened_ts"] = 1_700_000_000_000
    r.positions["BTCUSDT"] = pos
    r.stops["BTCUSDT"] = {"trigger": 95.0, "qty": 1.0, "order_id": "S1"}

    r._reconcile_closed("BTCUSDT", pos)

    row = _row(tmp_path, kind="reconcile_closed")
    assert row["executor"] == "external"
    assert row["pnl"] > 0
    assert tg.fills[-1]["title"] == "离场确认(外部平仓·盈利)"


def test_reconcile_closed_ignores_fills_before_position_open(tmp_path):
    """窗口内没有平仓成交 ⇒ exit_price/pnl 如实为 None,绝不借用上一笔仓。"""
    client = _FillClient(fills=[{"symbol": "BTCUSDT", "tradeSide": "close",
                                 "price": "94.0", "size": "1",
                                 "cTime": "1699999990000", "orderId": "OLD"}])
    tg = _FakeTG()
    r = _runner(tmp_path, client, tg)
    pos = _pos(entry=100.0, invalid=95.0, qty=1.0, mark=94.0)
    pos["opened_ts"] = 1_700_000_000_000
    r.positions["BTCUSDT"] = pos

    r._reconcile_closed("BTCUSDT", pos)

    row = _row(tmp_path, kind="reconcile_closed")
    assert row["exit_price"] is None
    assert row["executor"] == "unknown"
    assert row["pnl"] is None
    assert tg.fills[-1]["title"] == "离场确认(交易所已无此仓)"


def test_exit_settle_corrects_reference_price(tmp_path):
    """P0 回执:下单瞬间没取到成交价 ⇒ 后续步用权威成交价校正并留痕。"""
    now = int(time.time() * 1000)
    client = _FillClient()
    r = _runner(tmp_path, client, _FakeTG())
    r._exit_settle["BTCUSDT"] = {"oid": "X", "since_ms": now - 5000,
                                 "deadline_ms": now + 60_000, "ref_px": 100.0,
                                 "direction": 1, "qty": 1.0, "entry": 90.0,
                                 "why": "sub_bar_lock_exhausted"}
    client.fills = [{"symbol": "BTCUSDT", "tradeSide": "close",
                     "price": "101.5", "size": "1",
                     "cTime": str(now - 1000), "orderId": "X"}]

    r._settle_exits(now)

    assert "BTCUSDT" not in r._exit_settle
    row = _row(tmp_path, kind="exit_settle")
    assert row["exit_price"] == pytest.approx(101.5)
    assert row["pnl"] > 0
    assert "离场回执校正" in r.tg.alerts[-1]


def test_exit_settle_gives_up_after_deadline(tmp_path):
    """超时仍无回执 ⇒ 如实记 miss 并停止重试(不无限刷接口)。"""
    now = int(time.time() * 1000)
    r = _runner(tmp_path, _FillClient(), _FakeTG())
    r._exit_settle["BTCUSDT"] = {"oid": "X", "since_ms": now - 5000,
                                 "deadline_ms": now - 1, "ref_px": 100.0,
                                 "direction": 1, "qty": 1.0, "entry": 90.0,
                                 "why": "sub_bar_lock_exhausted"}

    r._settle_exits(now)

    assert r._exit_settle == {}
    assert r.counters["exit_settle_miss"] == 1
    assert _row(tmp_path, kind="exit_settle_miss")["ref_px"] == 100.0


def _entry_rec(**kw) -> dict:
    now = int(time.time() * 1000)
    rec = {"oid": "E1", "order_id": "M1", "event_id": "EV1",
           "ref_px": 100.0, "direction": 1, "submitted_ms": now - 1000,
           "deadline_ms": now + 60_000}
    rec.update(kw)
    return rec


def test_entry_settle_rewrites_entry_with_fill_vwap(tmp_path):
    """P0 观测:市价开仓先按参考价记账 ⇒ 回执到达后按**该订单**成交均价回写。

    只认 orderId 相同的成交(别人的单子不得算进来),同单多笔按 size 加权。
    """
    now = int(time.time() * 1000)
    client = _FillClient()
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=95.0, direction=1)
    r._entry_settle["BTCUSDT"] = _entry_rec()
    client.fills = [
        {"symbol": "BTCUSDT", "orderId": "M1", "price": "101.0", "size": "1",
         "tradeSide": "open", "cTime": str(now - 900)},
        {"symbol": "BTCUSDT", "orderId": "M1", "price": "101.4", "size": "3",
         "tradeSide": "open", "cTime": str(now - 800)},
        {"symbol": "BTCUSDT", "orderId": "OTHER", "price": "90.0", "size": "9",
         "tradeSide": "open", "cTime": str(now - 700)},
    ]

    r._settle_entries(now)

    assert r._entry_settle == {}
    assert r.positions["BTCUSDT"]["entry"] == pytest.approx(101.3)  # 加权
    row = _row(tmp_path, kind="entry_settle")
    assert row["fill_px"] == pytest.approx(101.3)
    assert row["fill_qty"] == pytest.approx(4.0)
    assert row["applied"] is True
    assert row["slip_bps"] == pytest.approx(130.0)   # 逆向漂移为正
    assert any("开仓成交价" in a for a in r.tg.alerts)




# ---------------------------------------------------------------------------
# ⑩ 事件 × 方向期望值权重:只缩不放、永不归零、别名归一
# ---------------------------------------------------------------------------


def test_edge_size_multiplier_defaults_to_one():
    """表缺失/键缺失 ⇒ 1.0(不改变现行行为)。"""
    assert lr.edge_size_multiplier(None, "BREAKOUT_ACCEPTED", 1) == 1.0
    assert lr.edge_size_multiplier({}, "BREAKOUT_ACCEPTED", 1) == 1.0
    assert lr.edge_size_multiplier({"LEVEL_REJECTED|-1": 0.5},
                                   "BREAKOUT_ACCEPTED", 1) == 1.0


def test_edge_size_multiplier_normalizes_legacy_event_codes():
    """配置写 E1 或 BREAKOUT_ACCEPTED 等价;方向不匹配不生效。"""
    table = {"BREAKOUT_ACCEPTED|1": 0.35}
    assert lr.edge_size_multiplier(table, "E1", 1) == pytest.approx(0.35)
    assert lr.edge_size_multiplier(table, "E1", -1) == 1.0


def test_edge_size_multiplier_only_shrinks_and_never_removes_direction():
    """配置只能减少仓位:0/负/NaN/非法 ⇒ 1.0;放大夹回 1.0;下限 10%。"""
    assert lr.edge_size_multiplier({"E1|1": 0.0}, "E1", 1) == 1.0
    assert lr.edge_size_multiplier({"E1|1": -1.0}, "E1", 1) == 1.0
    assert lr.edge_size_multiplier({"E1|1": float("nan")}, "E1", 1) == 1.0
    assert lr.edge_size_multiplier({"E1|1": "oops"}, "E1", 1) == 1.0
    assert lr.edge_size_multiplier({"E1|1": 3.0}, "E1", 1) == 1.0
    assert lr.edge_size_multiplier({"E1|1": 0.01}, "E1", 1) == 0.1


def test_live_config_edge_multiplier_is_bounded_and_keyed_validly():
    """生产配置里的键必须能解析成 (事件, 方向),且落在 [10%, 100%]。"""
    raw = json.loads((ROOT / "config" / "live.json").read_text(encoding="utf-8"))
    table = raw["risk"]["edge_size_by_event_direction"]
    for key in table:
        ev, _, d = key.partition("|")
        w = lr.edge_size_multiplier(table, ev, int(d))
        assert 0.1 <= w <= 1.0, key


def test_accept_long_sized_down_while_reject_long_kept(tmp_path):
    """接线:同一套参数下 ACCEPT 多单缩到 0.35,REJECT 多单保持 1.0(方向不被封)。"""
    cfg = _cfg_min()
    cfg["risk"]["edge_size_by_event_direction"] = {"BREAKOUT_ACCEPTED|1": 0.35}
    r = _runner(tmp_path, _FakeClient(), _FakeTG(), mode="paper", cfg=cfg)
    r.equity = 104.0

    r._submit_open(_campaign("BTCUSDT"), _decision(), NOW, mic=False)
    accept = _decision()
    accept.hyp.event_id = "E1"                      # 同方向,机制换成 ACCEPT
    r._submit_open(_campaign("ETHUSDT"), accept, NOW + 1000, mic=False)

    intents = {v["symbol"]: v for v in r.open_intents.values()}
    assert intents["BTCUSDT"]["edge_mult"] == 1.0
    assert intents["BTCUSDT"]["size_mult"] == pytest.approx(1.0)
    assert intents["ETHUSDT"]["edge_mult"] == pytest.approx(0.35)
    assert intents["ETHUSDT"]["size_mult"] == pytest.approx(0.35)
    assert intents["ETHUSDT"]["qty"] < intents["BTCUSDT"]["qty"]


def test_entry_settle_keeps_ref_when_fill_already_through_defense(tmp_path):
    """成交价已越过防线 ⇒ 只留痕不覆盖 entry(防线/R 交给止损路径)。"""
    now = int(time.time() * 1000)
    client = _FillClient()
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=95.0, direction=1)
    r._entry_settle["BTCUSDT"] = _entry_rec()
    client.fills = [{"symbol": "BTCUSDT", "orderId": "M1", "price": "94.0",
                     "size": "1", "tradeSide": "open", "cTime": str(now - 900)}]

    r._settle_entries(now)

    assert r.positions["BTCUSDT"]["entry"] == 100.0
    row = _row(tmp_path, kind="entry_settle")
    assert row["applied"] is False and row["fill_px"] == pytest.approx(94.0)


def test_entry_settle_gives_up_after_deadline(tmp_path):
    """超时仍无开仓回执 ⇒ 记 miss 并停手;仓位保留参考价口径。"""
    now = int(time.time() * 1000)
    r = _runner(tmp_path, _FillClient(), _FakeTG())
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=95.0, direction=1)
    r._entry_settle["BTCUSDT"] = _entry_rec(deadline_ms=now - 1)

    r._settle_entries(now)

    assert r._entry_settle == {}
    assert r.counters["entry_settle_miss"] == 1
    assert r.positions["BTCUSDT"]["entry"] == 100.0
    assert _row(tmp_path, kind="entry_settle_miss")["ref_px"] == 100.0


def test_entry_settle_drops_record_when_position_is_gone(tmp_path):
    """仓位已平(或已被新事件顶替)⇒ 回执作废,绝不记到下一仓头上。"""
    now = int(time.time() * 1000)
    client = _FillClient()
    r = _runner(tmp_path, client, _FakeTG())
    r.positions["BTCUSDT"] = _pos(entry=100.0, invalid=95.0, direction=1)
    r.positions["BTCUSDT"]["event_id"] = "EV2"      # 已被新事件顶替
    r._entry_settle["BTCUSDT"] = _entry_rec()
    client.fills = [{"symbol": "BTCUSDT", "orderId": "M1", "price": "101.0",
                     "size": "1", "tradeSide": "open", "cTime": str(now - 900)}]

    r._settle_entries(now)

    assert r._entry_settle == {}
    assert r.positions["BTCUSDT"]["entry"] == 100.0
    assert not [x for x in _journal_rows(tmp_path) if x["kind"] == "entry_settle"]


def test_entry_journal_carries_tf_opened_ts_and_prices(tmp_path):
    """开仓事实自解释:tf_label / opened_ts / ref_px / price_source 必须留痕。"""
    r = _runner(tmp_path, _FakeClient(), _FakeTG())
    it = {"oid": "E1", "symbol": "BTCUSDT", "tf_label": "4h", "direction": 1,
          "qty": 1.0, "invalid": 95.0, "target_1": 110.0, "event_id": "EV1",
          "route": "e3_market", "box_lo": None, "box_hi": None,
          "ref_px": 100.0, "fill_price": 100.0, "fill_price_source": "ref"}

    r._register_position("BTCUSDT", it, 100.0, NOW)

    row = _row(tmp_path, kind="fill")
    assert row["tf_label"] == "4h"
    assert row["opened_ts"] == NOW
    assert row["ref_px"] == pytest.approx(100.0)
    assert row["price_source"] == "ref"
    assert row["invalid"] == pytest.approx(95.0)
    assert row["direction"] == 1 and row["event_id"] == "EV1"


class _StopRejectClient(_FakeClient):
    """保护止损挂单必被拒(实测 400:价格已穿越触发价)。"""

    def place_stop_loss(self, symbol, **kw):
        raise BitgetError("400 trigger price invalid")


def test_manage_stops_place_reject_breached_exits_at_market(tmp_path):
    """挂单被拒且现价已越线 ⇒ 立刻市价离场,而不是等下个 tick 重试裸奔。"""
    client, tg = _StopRejectClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=94.0)

    r._manage_stops(NOW)

    assert [o["symbol"] for o in client.market_orders] == ["BTCUSDT"]
    assert client.market_orders[0]["trade_side"] == "close"
    assert "BTCUSDT" not in r.positions
    assert r.counters["stop_errors"] == 1
    assert r.counters["stop_place_breached"] == 1
    assert _row(tmp_path, kind="stop_place_breached")["mark"] == pytest.approx(94.0)
    assert any("市价离场" in a for a in tg.alerts)


def test_manage_stops_place_reject_not_breached_keeps_position(tmp_path):
    """挂单失败但未越线 ⇒ 保留仓位并告警(下轮继续尝试,不制造无谓市价单)。"""
    client, tg = _StopRejectClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=99.0)

    r._manage_stops(NOW)

    assert client.market_orders == []
    assert "BTCUSDT" in r.positions
    assert r.counters["stop_errors"] == 1
    assert r.counters.get("stop_place_breached", 0) == 0
    assert any("保护止损挂单失败" in a for a in tg.alerts)


class _NoPosStopClient(_FakeClient):
    """保护止损挂单被拒:交易所已无该仓位(实测 43023「仓位不足」)。"""

    def place_stop_loss(self, symbol, **kw):
        raise BitgetError("bitget 400 code=43023: 仓位不足,不能设置止盈或者止损")


def test_manage_stops_place_reject_no_position_only_drops_local_stop(tmp_path):
    """43023「仓位不足」= 交易所已无仓 ⇒ 只丢本地止损,不发市价单、不告警。

    实测 10-24 1000BONKUSDT 连续 4 次重试全为此因;若按"已越线"处理,会在
    仓位并不存在时反复发平仓单并被拒,还误报"仓位仍在,人工核查"。
    """
    client, tg = _NoPosStopClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=94.0)

    r._manage_stops(NOW)

    assert client.market_orders == []
    assert "BTCUSDT" in r.positions          # 仓位是否存在由 reconcile 判定
    assert "BTCUSDT" not in r.stops
    assert r.counters["stop_place_no_pos"] == 1
    assert r.counters.get("stop_place_breached", 0) == 0
    assert _row(tmp_path, kind="stop_place_no_pos")["trigger"] == pytest.approx(95.0)
    assert tg.alerts == []


class _StaleMarkStopClient(_FakeClient):
    """挂单被拒,且报文里带了交易所标记价(本地 mark 已过期)。"""

    def place_stop_loss(self, symbol, **kw):
        raise BitgetError(
            "bitget 400 code=45122: 多头仓位止损价格请<标记价格 94.5")


def test_mark_from_reject():
    """报文里的标记价要能被取出来(含中文/英文两种写法,取不到返回 None)。"""
    assert lr._mark_from_reject(
        "bitget 400 code=45122: 空头仓位止损价格请>标记价格 0.34387") == 0.34387
    assert lr._mark_from_reject(
        "code=45122: trigger price must be < mark price 94.5") == 94.5
    assert lr._mark_from_reject("400 trigger price invalid") is None


def test_manage_stops_place_reject_uses_exchange_mark_over_stale_local(tmp_path):
    """本地 mark 尚未越线、但交易所报文已报越线 ⇒ 仍须市价离场。

    本地 mark 只在 reconcile(≤2 分钟)刷新;挂单被拒恰恰发生在快速行情里,
    用旧值判会得出\"没越线,继续重试\",于是整段裸奔。
    """
    client, tg = _StaleMarkStopClient(), _FakeTG()
    r = _runner(tmp_path, client, tg)
    r.positions["BTCUSDT"] = _pos(invalid=95.0, mark=99.0)   # 本地:未越线

    r._manage_stops(NOW)

    assert [o["symbol"] for o in client.market_orders] == ["BTCUSDT"]
    assert "BTCUSDT" not in r.positions
    assert r.counters["stop_place_breached"] == 1
    assert _row(tmp_path, kind="stop_place_breached")["mark"] == pytest.approx(94.5)


def test_defense_title_separates_lock_from_trail():
    """“止损推进”必须按来源分述:锁利进盈利区 ≠ 收紧止损。"""
    r = lr.LiveRunner.__new__(lr.LiveRunner)      # 纯判定,无需构造
    pos = types.SimpleNamespace(direction=1, entry=100.0, invalid=102.5,
                                target_1=110.0)
    assert r._defense_title(pos, "v21|opp_lock|峰值=110") == \
        "锁利推进(已锁 25% TP1 行程)"
    assert r._defense_title(pos, "§8.3|trail new pivot low=101") == \
        "结构防线推进(§8.3 trail)"
    pos.invalid = 97.0
    assert r._defense_title(pos, "其它") == "防线收紧"


# ---------------------------------------------------------------------------
# ⑪ 通知分通道 + 信号广播与容量闸门解耦
#
# 修前:信号广播写在 `_submit_open` 的闸门之后,任一闸门 `return` 都会连带
# 吞掉通知 —— 容量池一满,ACCEPT/REJECT 在信号频道就彻底不可见了。
# 现在:检测到事件即广播结构事实,`exec_status` 只说明"本系统有没有上车"。
# ---------------------------------------------------------------------------


class _SignalTG(_FakeTG):
    """记录信号广播的假发信器(成交/告警仍走父类)。"""

    def __init__(self) -> None:
        super().__init__()
        self.signals: list[dict] = []

    def send_signal(self, symbol, event_id, side, route, ref_price, invalid,
                    size_note="", regime="", **kw):
        self.signals.append({"symbol": symbol, "event_id": event_id,
                             "side": side, "route": route,
                             "ref_price": ref_price, "invalid": invalid,
                             "size_note": size_note, "regime": regime, **kw})
        return True


def _decision(*, kind="market", direction=1, ref=100.0, invalid=95.0,
              target=110.0, zone=(99.0, 101.0), opposite=97.0):
    hyp = types.SimpleNamespace(
        event_id="LEVEL_REJECTED", direction=direction, invalid=invalid,
        target_1=target, regime=types.SimpleNamespace(value="RANGE_ROTATED"))
    return types.SimpleNamespace(
        kind=kind, hyp=hyp, intent=types.SimpleNamespace(ref_price=ref),
        route="e3_market", limit_price=None,
        meta={"zone_lo": zone[0], "zone_hi": zone[1], "zone_touches": 3,
              "box_opposite": opposite, "size_mult": 1.0})


def _campaign(symbol="BTCUSDT", tf_label="12h"):
    bars = [(NOW - (45 - i) * 43_200_000, 100.0, 101.0, 99.0, 100.5, 1.0)
            for i in range(45)]
    return types.SimpleNamespace(symbol=symbol, tf_label=tf_label,
                                 trig_bars=list(bars), cog_bars=list(bars))


def test_signal_broadcast_survives_portfolio_cap(tmp_path):
    """容量闸门可以挡执行,但不能挡通知:结构信号照发,执行口径如实标注。"""
    client, tg = _FakeClient(), _SignalTG()
    cfg = _cfg_min()
    cfg["risk"]["portfolio_cap_frac"] = 0.0        # 任何新仓都超预算
    r = _runner(tmp_path, client, tg, mode="paper", cfg=cfg)
    r.equity = 104.0                     # paper 尚未跑第一根 K 线时的权益占位

    r._submit_open(_campaign(), _decision(), NOW, mic=False)

    assert len(tg.signals) == 1
    sig = tg.signals[0]
    assert "组合容量已满" in sig["exec_status"]
    # 结构事实随信号一起出:箱体=区间+对侧近缘的极值,触边次数原样透传
    assert sig["box_lo"] == pytest.approx(97.0)
    assert sig["box_hi"] == pytest.approx(101.0)
    assert sig["zone_lo"] == pytest.approx(99.0)
    assert sig["zone_touches"] == 3
    assert sig["tf_label"] == "12h"
    # 闸门记账与"没真开仓"这件事都还在
    assert r.counters["cap_skips"] == 1
    assert r.counters["signals"] == 0
    assert r.open_intents == {}
    assert client.market_orders == []


def test_signal_broadcast_tf_cap_names_the_bucket(tmp_path):
    """周期容量池挡下时,信号里必须点名是哪个周期满了(否则读者无法判断)。"""
    tg = _SignalTG()
    cfg = _cfg_min()
    cfg["risk"]["tf_cap_by_tf"] = {"12h": 0.0}
    r = _runner(tmp_path, _FakeClient(), tg, mode="paper", cfg=cfg)
    r.equity = 104.0

    r._submit_open(_campaign(tf_label="12h"), _decision(), NOW, mic=False)

    assert "12h 周期容量已满" in tg.signals[0]["exec_status"]
    assert r.counters["tf_cap_skips"] == 1
    assert r.counters["cap_skips"] == 1


def test_signal_broadcast_once_per_event(tmp_path):
    """同一事件同一拍被重复评估:闸门照旧计数,信号只推一条(不刷屏)。"""
    tg = _SignalTG()
    cfg = _cfg_min()
    cfg["risk"]["portfolio_cap_frac"] = 0.0
    r = _runner(tmp_path, _FakeClient(), tg, mode="paper", cfg=cfg)
    r.equity = 104.0
    cs, d = _campaign(), _decision()

    r._submit_open(cs, d, NOW, mic=False)
    r._submit_open(cs, d, NOW, mic=False)

    assert len(tg.signals) == 1
    assert r.counters["cap_skips"] == 2


def test_signal_notice_error_never_breaks_decision(tmp_path):
    """通知链路抛异常只记账:闸门与决策链必须照常跑完。"""
    class _BoomTG(_FakeTG):
        def send_signal(self, *a, **kw):
            raise RuntimeError("boom")

    tg = _BoomTG()
    cfg = _cfg_min()
    cfg["risk"]["portfolio_cap_frac"] = 0.0
    r = _runner(tmp_path, _FakeClient(), tg, mode="paper", cfg=cfg)
    r.equity = 104.0

    r._submit_open(_campaign(), _decision(), NOW, mic=False)

    assert r.counters["signal_notice_errors"] == 1
    assert r.counters["cap_skips"] == 1


def test_signal_broadcast_kwargs_match_notifier_signature(tmp_path):
    """runner 传给 `send_signal` 的关键字必须与模板签名逐一对得上。

    假发信器收 `**kwargs`,拼错关键字会变成"静默不发"——测试期间就撞到过
    `direction=` 与模板 `side=` 的错位。这里用**真通知器**走一遍完整链路:
    任何签名错位都会立刻变成 `signal_notice_errors`,而且一条也发不出去。
    """
    from market_operator.tg import TelegramNotifier

    calls: list[tuple] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class _Sess:
        proxies = {}
        timeout = None

        def post(self, url, data=None, files=None, timeout=None):
            calls.append((url, data, files))
            return _Resp()

    tg = TelegramNotifier({"TELEGRAM_BOT_TOKEN": "SIG",
                           "TELEGRAM_CHAT_ID": "C1",
                           "TELEGRAM_TRADE_BOT_TOKEN": "OPS",
                           "TELEGRAM_TRADE_CHAT_ID": "C2"},
                          session=_Sess(), async_mode=False)
    cfg = _cfg_min()
    cfg["risk"]["portfolio_cap_frac"] = 0.0
    r = _runner(tmp_path, _FakeClient(), tg, mode="paper", cfg=cfg)
    r.equity = 104.0

    r._submit_open(_campaign(), _decision(), NOW, mic=False)

    assert r.counters.get("signal_notice_errors", 0) == 0
    assert len(calls) == 1
    url, data, _files = calls[0]
    assert "/botSIG/" in url                # 信号只走公开频道
    assert data["chat_id"] == "C1"
    body = data.get("caption") or data.get("text") or ""
    assert "系统执行" in body and "组合容量已满" in body
    assert "平衡箱体" in body and "事件区间" in body


def test_notifier_routes_by_kind_only():
    """分流是一张表:未知类型落运营通道,公开频道永远只收信号。"""
    from market_operator.tg import (CHANNEL_OPS, CHANNEL_SIGNAL,
                                    TelegramNotifier)

    n = TelegramNotifier({"TELEGRAM_BOT_TOKEN": "SIGTOK",
                          "TELEGRAM_CHAT_ID": "SIGCHAT",
                          "TELEGRAM_TRADE_BOT_TOKEN": "OPSTOK",
                          "TELEGRAM_TRADE_CHAT_ID": "OPSCHAT"})
    sent: list[tuple] = []

    def _fake_post(token, chat_id, kind, body):
        sent.append((token, chat_id, kind))
        return True

    n._post_text_sync = _fake_post
    for kind in ("signal", "fill", "daily", "alert", "mystery"):
        assert n.send(kind, "x") is True

    assert [s[0] for s in sent] == ["SIGTOK", "OPSTOK", "OPSTOK",
                                    "OPSTOK", "OPSTOK"]
    assert n.channel_of("signal") == CHANNEL_SIGNAL
    assert n.channel_of("mystery") == CHANNEL_OPS
    assert n.ops_channel_separate is True


def test_notifier_reports_fallback_instead_of_pretending_split():
    """secrets_loader 会把运营键回退填成同值:此时必须如实报"未分开"。"""
    from market_operator.tg import TelegramNotifier

    n = TelegramNotifier({"TELEGRAM_BOT_TOKEN": "T",
                          "TELEGRAM_CHAT_ID": "C",
                          "TELEGRAM_TRADE_BOT_TOKEN": "T",
                          "TELEGRAM_TRADE_CHAT_ID": "C"})
    assert n.configured is True
    assert n.ops_configured is True          # 回退可用:不静默丢告警
    assert n.ops_channel_separate is False   # 但没有真的分开
    sent: list[tuple] = []
    n._post_text_sync = lambda token, chat_id, kind, body: \
        sent.append((token, chat_id)) or True
    n.send("alert", "x")
    assert sent == [("T", "C")]


def test_secrets_loader_ops_channel_is_optional(tmp_path):
    """运营通道是可选件:未配置时回退到信号机器人,不应让启动失败。"""
    p = _write_secrets(tmp_path, PLACEHOLDER)
    sec = load_secrets(p, environ={})
    assert sec["TELEGRAM_TRADE_BOT_TOKEN"] == PLACEHOLDER["TELEGRAM_BOT_TOKEN"]
    assert sec["TELEGRAM_TRADE_CHAT_ID"] == PLACEHOLDER["TELEGRAM_CHAT_ID"]

    p2 = _write_secrets(tmp_path, {**PLACEHOLDER,
                                   "TELEGRAM_TRADE_BOT_TOKEN": "0:OPS",
                                   "TELEGRAM_TRADE_CHAT_ID": "-100123"})
    sec2 = load_secrets(p2, environ={})
    assert sec2["TELEGRAM_TRADE_BOT_TOKEN"] == "0:OPS"
    assert sec2["TELEGRAM_TRADE_CHAT_ID"] == "-100123"

