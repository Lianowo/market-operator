"""实盘接入件测试(v20/lifecycle;任务书 ≥8 条):

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
    raw20, params, _rp = lr.load_v20_params()
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
