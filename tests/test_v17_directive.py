"""v17 指令变体(docs/v17-directive.md)测试 —— 指令三闸边界与 arm 语义钉死。

钉死:
- config/v17.json parent=v16 硬对账(逐键仅允许 (version/note/meta/arms/
  tf_list/arm/equity/enable_mic/trigger_tf_map) 差;equity=1000;tf_list
  恰 [2h,4h,12h,1d] 不含 1h;双臂钉死:arm-M mic_family=all、arm-C
  mic_family=[E1,E6,E7];v16_params 深等;trigger 映射 == 冻结级联);
- 1h 不在场断言:tf_of / assert_frames_no_1h(脚本级)+ 级联解析四档精确;
- mic 家族过滤(arm-C):fade(E3)mic 事件不入路由(mic_family_filtered+1,
  成交账本无 mic 痕迹),其 bar 化身仍按 bar 语义路由(归 bar);
  arm-C ≡ mic 全族臂的 fade 差分;arm-M 全族 unfiltered;
- mic 决策接 arm 语义(V16Router 三闸 + C3 stop-entry / C2 maker):mic
  续涨 E1 → order_type=stop_entry(stop 价 = ev_ref ± k_bm·ATR,v16_tier/
  size_mult 注入,fill 严格晚于决策 trigger 收盘);mic fade E3 → maker
  前缘限价(zone.lo,成交窗口超期 EXPIRED 于 armed+12h);
- equity–min_size 边界:qty 恰落 size_min 过(inclusive)、半步之下拒
  (floor 至 0),同一决策 300 拒而 1000 过(真实 BTC 合约规格);
- 确定性:双臂同构场景双跑账本逐位一致;
- 引擎 parity:enable_mic=False + arm 前提下挂/不挂 mic 帧账本逐位一致。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd
import pytest

from market_operator.backtest_v6 import SymbolCognition
from market_operator.broker import Broker, ContractSpec, OrderIntent, OrderKind
from market_operator.campaign import (
    ArmConfig,
    STOP_FAMILY,
    build_campaign_frame,
    campaign_key,
    parse_key,
    run_campaigns,
)
from market_operator.events import E1, E3, MarketEvent
from market_operator.harvest import HarvestParams, V16Params, V16Router
from market_operator.hypothesis import StructureView
from market_operator.strategy_v6 import V6Params
from market_operator.structure import (
    Params,
    Pivot,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)
from market_operator.trigger import MicCognition, TRIGGER_TF_MAP, resolve_trigger_tf

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_v17  # noqa: E402

T0 = 1_704_067_200_000                 # 2024-01-01 00:00:00 UTC(网格对齐)
H = 3_600_000
M30 = 1_800_000
ATR = 2.0
P2 = Params(atr_n=2)

SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(segment="full", dev_frac=0.7, equity0=1000.0, risk_frac=0.0075,
          leverage_cap=5.0, taker_fee=0.0006, maker_fee=0.0002,
          slippage_per_side=0.0002, funding_per_8h=0.0001,
          cost_multiplier=1.0, contracts={"TEST": SPEC},
          params=P2, n_fill=12, portfolio_cap_frac=0.03)

PIVOTS = (Pivot(price=110.0, forming_index=0, known_index=1,
                kind=PivotKind.HIGH),
          Pivot(price=80.0, forming_index=2, known_index=3,
                kind=PivotKind.LOW))
VIEW = StructureView(regime=Regime.TRANSITION, zones=(), atr=ATR,
                     pivots=PIVOTS)

WL_ALL = frozenset((eid, d) for eid in ("E1", "E2", "E3", "E4", "E5",
                                        "E6", "E7", "E8")
                   for d in (-1, 1))


def _vp16() -> V16Params:
    kw = {f: getattr(V6Params(), f) for f in V6Params.__dataclass_fields__}
    kw["whitelist"] = WL_ALL
    return V16Params(**kw, v16=HarvestParams())


def _arm_cfg() -> ArmConfig:
    return ArmConfig(fill_model="exchange_maker", stop_entry=True,
                     stop_wait_tf=12, k_bm=0.1,
                     c2_family=("E2", "E3", "E8"),
                     harvest_family=("E2", "E3", "E8"),
                     time_cap_bars=48, cap_mfe_min=0.5)


# ---------------------------------------------------------------------------
# 合成战役帧(2h 认知 × 1h 执行 × 30m trigger);事件注入由参数给出
# ---------------------------------------------------------------------------


def _frames_2h(events_cog: dict, events_mic: dict, *, with_mic: bool):
    n_exec, n_cog, n_trig = 48, 24, 96
    trig = [(T0 + i * M30, 96.5, 96.7, 96.3, 96.5, 1.0) for i in range(n_trig)]
    df_exec = pd.DataFrame(
        [(T0 + i * H, 96.5, 96.7, 96.3, 96.5, 1.0) for i in range(n_exec)],
        columns=["ts", "open", "high", "low", "close", "volume"])
    cog_bars = [(T0 + i * 2 * H, 96.5, 96.7, 96.3, 96.5, 1.0)
                for i in range(n_cog)]
    cog = SymbolCognition(symbol="TEST", bars=cog_bars, atrs=[ATR] * n_cog,
                          events_by=dict(events_cog), pivots_by={},
                          views={i: VIEW for i in events_cog})
    mic = None
    if with_mic:
        mic = MicCognition(
            symbol="TEST", obs_tf_seconds=7200, trigger_tf_seconds=1800,
            scale=4, bars=trig, cog_index_of=[i // 4 for i in range(n_trig)],
            atrs=[ATR] * n_trig, events_by=dict(events_mic),
            views={a: VIEW for a in events_mic},
            bar_events_by=dict(events_cog), counters={})
    return build_campaign_frame(cog, df_exec, "2h", mic_cog=mic)


def _fade_e3(*, ts, known_index, mic=False):
    zone = Zone(lo=99.0, hi=100.5, role=ZoneRole.RESISTANCE,
                state=ZoneState.ACTIVE, touches=3)
    attrs = {"zone": zone, "ref": 100.5, "invalid": 100.7}
    if mic:
        attrs.update({"mic": True, "trigger_tf": 1800})
    return MarketEvent(id=E3, direction=-1, ts=ts, known_index=known_index,
                       attrs=attrs)


def _cont_e1(*, ts, known_index, mic=False):
    attrs = {"ref": 95.0, "invalid": 94.0}
    if mic:
        attrs.update({"mic": True, "trigger_tf": 1800})
    return MarketEvent(id=E1, direction=+1, ts=ts, known_index=known_index,
                       attrs=attrs)


# ---------------------------------------------------------------------------
# 1. config/v17.json parent=v16 硬对账 + 1h 不在场(config 函数级)
# ---------------------------------------------------------------------------


def test_v17_config_parent_v16_hard_check():
    v17 = json.loads(open("config/v17.json", encoding="utf-8").read())
    v16 = json.loads(open("config/v16.json", encoding="utf-8").read())
    allow = {"version", "note", "meta", "arms", "tf_list", "arm",
             "equity", "enable_mic", "trigger_tf_map", "merge_note",
             "whitelist_note", "v16_params"}        # v16_params 数值面单独深检
    for k in set(v17) | set(v16):
        if k in allow:
            continue
        assert v17.get(k) == v16.get(k), \
            f"parent=v16 逐键不许漂移({k}): {v17.get(k)} != {v16.get(k)}"
    p17 = {k: v for k, v in v17["v16_params"].items() if k != "note"}
    p16 = {k: v for k, v in v16["v16_params"].items() if k != "note"}
    assert p17 == p16, "三闸/收割婚配先验(数值面)必须与 v16 深等(note 为叙述)"
    assert v17["equity"] == 1000.0, "指令①:equity=1000 USDT"
    assert v17["tf_list"] == ["2h", "4h", "12h", "1d"]
    assert "1h" not in v17["tf_list"]
    assert {int(k): int(v) for k, v in
            v17["trigger_tf_map"].items()} == TRIGGER_TF_MAP
    assert set(v17["arms"]) == {"arm-M", "arm-C"}
    assert v17["arms"]["arm-M"]["mic_family"] == "all"
    assert v17["arms"]["arm-C"]["mic_family"] == ["E1", "E6", "E7"]
    for a in ("arm-M", "arm-C"):
        s = v17["arms"][a]
        assert s["v16"] and s["fill_model"] == "exchange_maker" \
            and s["c3_stop_entry"], f"{a} v16 合成开关漂移"


def test_v17_1h_offline_assertions():
    assert list(STOP_FAMILY) == ["E1", "E6", "E7"]
    # 函数级:tf 冻结与 1h 帧守卫
    assert run_v17.tf_of(["2h", "4h", "12h", "1d"]) is None
    with pytest.raises(RuntimeError):
        run_v17.tf_of(["1h", "2h", "4h", "12h", "1d"])
    fake = {"BTCUSDT@1h": object(), "BTCUSDT@2h": object()}
    with pytest.raises(RuntimeError):
        run_v17.assert_frames_no_1h(fake)
    assert run_v17.assert_frames_no_1h({"BTCUSDT@2h": object()}) is None
    # 级联解析:v17 四档 obs 精确命中(2h→30m/4h→1h/12h→2h/1d→4h)
    expect = {7200: 1800, 14400: 3600, 43200: 7200, 86400: 14400}
    for obs, trig in expect.items():
        got, decl = resolve_trigger_tf(obs)
        assert (got, decl) == (trig, None)
    # 1h 不属 v17 obs 档(仍映射 15m,只作 4h trigger 的从属帧语义)
    assert {parse_key(k)[1] for k in ("BTCUSDT@2h",)} == {"2h"}


# ---------------------------------------------------------------------------
# 2. mic 家族过滤(arm-C:fade 归 bar)与 arm-M 全族对照
# ---------------------------------------------------------------------------


def _expired_row(res, order_type=None):
    return [r for r in res.ledger
            if r["kind"] == "EXPIRED"
            and (order_type is None or r["meta"].get("order_type")
                 == order_type)]


def test_v17_mic_family_filter_fade_suppressed_in_arm_c():
    """arm-C:fade(E3)的 mic 化身被滤掉;其 bar 化身照常按 bar 语义路由。"""
    f = _frames_2h({3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)]},
                   {13: [_fade_e3(ts=T0 + 13 * M30, known_index=13, mic=True)]},
                   with_mic=True)
    kw = dict(KW)
    # arm-C:仅续涨 mic ⇒ 本例 fade mic 全滤,bar E3 路由成 maker 挂单到期
    rc = run_campaigns({campaign_key("TEST", "2h"): f}, **kw,
                       router_params=_vp16(), router_factory=V16Router,
                       arm=_arm_cfg(), enable_mic=True,
                       mic_families=STOP_FAMILY)
    assert rc.counters.get("mic_family_filtered", 0) == 1
    assert rc.counters.get("mic_go_events", 0) == 0
    assert rc.counters.get("maker_entry_armed", 0) == 1
    ex = _expired_row(rc, "maker_entry")
    assert len(ex) == 1
    assert ex[0]["price"] == 99.0            # fade 前缘(zone.lo,G3 口径)
    assert not ex[0]["meta"].get("mic"), "fade 归 bar:路由痕迹不得含 mic"
    assert ex[0]["decision_ts"] == T0 + 3 * 2 * H     # bar 标签武装(v16 口径)
    assert ex[0]["meta"]["expiry_ts"] == T0 + 7 * H + 12 * H
    # arm-M:mic 化身先武装(更早),bar 化身槽位去重 —— 唯一成交痕迹带 mic
    rm = run_campaigns({campaign_key("TEST", "2h"): f}, **kw,
                       router_params=_vp16(), router_factory=V16Router,
                       arm=_arm_cfg(), enable_mic=True, mic_families=None)
    assert rm.counters.get("mic_family_filtered", 0) == 0
    assert rm.counters.get("mic_go_events", 0) == 1
    assert rm.counters.get("maker_entry_armed", 0) == 1
    exm = _expired_row(rm, "maker_entry")
    assert len(exm) == 1 and exm[0]["meta"].get("mic") is True
    assert exm[0]["decision_ts"] == T0 + 13 * M30    # trigger 标签武装
    assert exm[0]["ts"] < ex[0]["ts"], \
        "mic 提前量:同现象 maker 到期应早于 bar 触发版本"


# ---------------------------------------------------------------------------
# 3. mic 决策接 arm 语义(续涨 C3 stop-entry;三闸与定档注入)
# ---------------------------------------------------------------------------


def test_v17_mic_decision_stop_entry_via_arm():
    """mic 续涨 E1 ⇒ C3 stop-entry 预埋(stop = ev_ref + k_bm·ATR)、
    决策/成交因果严格(trigger 收盘决策,次一 trigger bar 成交)。"""
    f = _frames_2h({}, {9: [_cont_e1(ts=T0 + 9 * M30, known_index=9, mic=True)]},
                   with_mic=True)
    res = run_campaigns({campaign_key("TEST", "2h"): f}, **dict(KW),
                        router_params=_vp16(), router_factory=V16Router,
                        arm=_arm_cfg(), enable_mic=True, mic_families=None)
    assert res.counters.get("stop_entry_armed", 0) == 1
    assert res.counters.get("stop_entry_fills", 0) == 1
    opens = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(opens) == 1
    row = opens[0]
    assert row["meta"]["order_type"] == "stop_entry"
    assert row["meta"]["entry_mode"] == "stop_entry"
    assert row["meta"]["stop_ref"] == 95.0
    assert row["meta"]["limit_price"] == 95.0 + 0.1 * ATR   # C3 stop 价
    assert row["decision_ts"] == T0 + 10 * M30     # stop 触发 trigger bar
    assert row["ts"] == T0 + 11 * M30              # 触发后次一 trigger 开盘市价
    assert row["price"] == pytest.approx(96.5 * (1 + 0.0002))
    assert row["meta"]["mic"] is True and row["meta"]["cog_index"] == 2
    assert row["meta"]["v16_tier"] == "B"          # TRANSITION ⇒ 其余档 B
    assert row["meta"]["size_mult"] == 0.75
    assert row["meta"]["struct_invalid"] == 94.0   # 三闸结构锚(非 turtle 2·ATR)
    assert res.counters.get("v16_gate_evaluated", 0) == 1
    trig = [r for r in res.ledger if r["kind"] == "TRIGGER"]
    assert len(trig) == 1 and trig[0]["ts"] == T0 + 10 * M30


# ---------------------------------------------------------------------------
# 4. equity–min_size 关系边界
# ---------------------------------------------------------------------------


def _broker(equity: float, spec: ContractSpec, *, slippage: float = 0.0):
    return Broker(equity=equity, risk_frac=0.0075, leverage_cap=5.0,
                  taker_fee=0.0006, slippage_per_side=slippage,
                  funding_per_8h=0.0001, contracts={spec.symbol: spec},
                  maker_fee=0.0002, portfolio_stop_risk_frac=0.03)


def _submit_short_open(b: Broker, ref: float, invalid: float, decision_ts=T0):
    b.submit(OrderIntent(kind=OrderKind.OPEN, symbol="TEST", qty_frac=None,
                         reason="E1|short boundary", ref_price=ref,
                         ts=decision_ts),
             decision_ts=decision_ts, direction=-1, invalid=invalid,
             target_1=None, event_id="E1", meta={"direction": -1})
    return b.on_bar_open("TEST", decision_ts + H, ref)[0]


def test_v17_equity_min_size_boundary():
    spec = ContractSpec(symbol="TEST", size_min=0.10, size_step=0.10,
                        size_place=1, price_place=2, min_trade_usdt=1.0)
    # 恰界:qty_risk 恰 == size_min ⇒ inclusive 通过(0.0075·E/75,滑点 0)
    row = _submit_short_open(_broker(1000.0, spec), 100.0, 175.0)
    assert row["kind"] == "OPEN" and row["qty"] == 0.1
    # 半步之下:qty_risk = 0.0999… ⇒ floor 至 0 ⇒ MIN_SIZE 拒(恰界向下拟征)
    row2 = _submit_short_open(_broker(999.0, spec), 100.0, 175.0)
    assert row2["kind"] == "SKIP" and row2["reason_code"] == "MIN_SIZE"
    # 半步之上(恰界向上拟征)
    row3 = _submit_short_open(_broker(1001.0, spec), 100.0, 175.0)
    assert row3["kind"] == "OPEN" and row3["qty"] == 0.1
    # 300→1000 机械差(指令①:sizing 差的实际落点):真实 BTC 合约规格
    ent = json.loads(open("data/contracts.json", encoding="utf-8")
                     .read())["entries"]["BTCUSDT"]["raw"]
    btc = ContractSpec.from_entry(ent)
    b300 = Broker(equity=300.0, risk_frac=0.0075, leverage_cap=5.0,
                  taker_fee=0.0006, slippage_per_side=0.0,
                  funding_per_8h=0.0001, contracts={"TEST": btc},
                  maker_fee=0.0002, portfolio_stop_risk_frac=0.03)
    r300 = _submit_short_open(b300, 66_000.0, 91_000.0)
    b1000 = Broker(equity=1000.0, risk_frac=0.0075, leverage_cap=5.0,
                   taker_fee=0.0006, slippage_per_side=0.0,
                   funding_per_8h=0.0001, contracts={"TEST": btc},
                   maker_fee=0.0002, portfolio_stop_risk_frac=0.03)
    r1000 = _submit_short_open(b1000, 66_000.0, 91_000.0)
    assert r300["kind"] == "SKIP" and r300["reason_code"] == "MIN_SIZE"
    assert r1000["kind"] == "OPEN" and r1000["qty"] >= btc.size_min \
        and r1000["qty"] * 66_000.0 >= btc.min_trade_usdt


# ---------------------------------------------------------------------------
# 5. 确定性 + 引擎 parity(arm 在场)
# ---------------------------------------------------------------------------


def test_v17_determinism_double_run_arms():
    f4 = lambda: _frames_2h(
        {3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)]},
        {13: [_fade_e3(ts=T0 + 13 * M30, known_index=13, mic=True)]},
        with_mic=True)
    f5 = lambda: _frames_2h(
        {}, {9: [_cont_e1(ts=T0 + 9 * M30, known_index=9, mic=True)]},
        with_mic=True)
    for maker, fams in ((f4, STOP_FAMILY), (f4, None), (f5, None)):
        kw = dict(KW)
        r1 = run_campaigns({campaign_key("TEST", "2h"): maker()}, **kw,
                           router_params=_vp16(), router_factory=V16Router,
                           arm=_arm_cfg(), enable_mic=True,
                           mic_families=fams)
        r2 = run_campaigns({campaign_key("TEST", "2h"): maker()}, **kw,
                           router_params=_vp16(), router_factory=V16Router,
                           arm=_arm_cfg(), enable_mic=True,
                           mic_families=fams)
        assert json.dumps(r1.ledger, sort_keys=True, default=str) \
            == json.dumps(r2.ledger, sort_keys=True, default=str)
        assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)


def test_v17_engine_parity_mic_disabled_with_arm():
    """enable_mic=False 时挂载/不挂 mic 帧账本逐位一致(v13 引擎钉对 arm 生效)。"""
    kw = dict(KW)
    with_mic = _frames_2h({3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)]},
                          {13: [_fade_e3(ts=T0 + 13 * M30, known_index=13,
                                         mic=True)]},
                          with_mic=True)
    no_mic = _frames_2h({3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)]},
                        {}, with_mic=False)
    r1 = run_campaigns({campaign_key("TEST", "2h"): with_mic}, **kw,
                       router_params=_vp16(), router_factory=V16Router,
                       arm=_arm_cfg(), enable_mic=False)
    r2 = run_campaigns({campaign_key("TEST", "2h"): no_mic}, **kw,
                       router_params=_vp16(), router_factory=V16Router,
                       arm=_arm_cfg(), enable_mic=False)
    assert json.dumps(r1.ledger, sort_keys=True, default=str) \
        == json.dumps(r2.ledger, sort_keys=True, default=str)
    assert r1.counters.get("maker_entry_armed", 0) == 1
