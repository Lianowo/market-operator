"""v14 arm 矩阵接线(docs/v14-protocol.md)战役级测试 + 配置冻结对账。

钉死:
- arm0 回归:arm=None、arm=ArmConfig()(全默认)、quality=None 的 V14Router
  三者账本/指标逐位一致(与 canonical 行为/数值口径同;全量数据锚在
  scripts/run_v14_arms.py 硬断言);
- C1 优质才进拒单计数(Q1/Q2 拒绝)与 C5 档位配注(A/B tier → size_mult);
- C2 maker/virtual 市价单改造(决策收盘价挂单,arm2/arm3 分流计数);
- C3 stop-entry 预埋(破位参考价 − k_bm·atr、12×TF 超时);
- C4 fade 分层 3 份(ladder×3 armed、合并均价、风险份额 1/3);
- 确定性双跑;config/v14.json 与 canonical 共享键逐项一致、arms 矩阵冻结。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from market_operator.backtest_v6 import SymbolCognition, compute_metrics
from market_operator.broker import ContractSpec
from market_operator.campaign import (
    ArmConfig,
    build_campaign_frame,
    run_campaigns,
)
from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.quality import QualityParams, V14Params, V14Router
from market_operator.strategy_v6 import V6Params, V6Router
from market_operator.structure import (
    Params,
    Pivot,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)

H = 3_600_000
T0 = 1_704_067_200_000
ATR = 2.0
PARAMS = Params(atr_n=2)
SPEC = ContractSpec(symbol="TEST", size_min=0.01, size_step=0.01,
                    size_place=2, price_place=2, min_trade_usdt=5.0)
KW = dict(segment="full", dev_frac=0.7, equity0=300.0, risk_frac=0.0075,
          leverage_cap=5.0, taker_fee=0.0006, maker_fee=0.0002,
          slippage_per_side=0.0002, funding_per_8h=0.0001,
          cost_multiplier=1.0, contracts={"TEST": SPEC}, params=PARAMS,
          n_fill=12, portfolio_cap_frac=0.03)
WL16 = frozenset((e, d) for e in ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED", "STRUCTURE_BROKEN", "FLIP_RETESTED",
                                  "EDGE_RECLAIMED") for d in (-1, 1))
RP16 = V6Params(whitelist=WL16)
QPARAMS = QualityParams()
VIEW_OK = StructureView(
    regime=Regime.TREND_DOWN,
    zones=(Zone(lo=80.0, hi=82.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE, touches=4),),
    atr=ATR,
    pivots=(Pivot(price=95.0, forming_index=0, known_index=1,
                  kind=PivotKind.HIGH),
            Pivot(price=90.0, forming_index=2, known_index=3,
                  kind=PivotKind.LOW)))
VIEW_BAD_POS = StructureView(          # band [100,110]:pos<0.35 ⇒ 空 Q1 拒
    regime=Regime.RANGE,
    zones=(Zone(lo=108.0, hi=110.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE),),
    atr=ATR,
    pivots=(Pivot(price=110.0, forming_index=0, known_index=1,
                  kind=PivotKind.HIGH),
            Pivot(price=100.0, forming_index=2, known_index=3,
                  kind=PivotKind.LOW)))


def _ev(eid, direction, tf_idx, tf_hours, ref=99.5, invalid=99.7, zone=None):
    attrs = {"ref": ref, "invalid": invalid, "started": 0, "atr": ATR}
    if zone is not None:
        attrs["zone"] = zone
    return MarketEvent(id=eid, direction=direction, ts=T0 + tf_idx * tf_hours * H,
                       known_index=tf_idx, attrs=attrs)


def _exec_prices(n, overrides=None):
    out = []
    ov = overrides or {}
    for i in range(n):
        o, h, l, c = ov.get(i, (None, None, None, None))
        base = 96.0 + (i % 5) * 0.3
        co = base if c is None else c
        oo = (out[-1][3] if out else co) if o is None else o
        hh = max(oo, co) + 0.5 if h is None else h
        ll = min(oo, co) - 0.5 if l is None else l
        out.append((oo, hh, ll, co))
    return out


def _frame(tf_hours, events_by=None, exec_overrides=None, view=VIEW_OK,
           n_1h=16):
    rows = _exec_prices(n_1h, exec_overrides)
    df_exec = pd.DataFrame(
        [(T0 + i * H, o, h, l, c, 1.0) for i, (o, h, l, c) in enumerate(rows)],
        columns=["ts", "open", "high", "low", "close", "volume"])
    n_tf = n_1h // tf_hours
    cog_bars = []
    for i in range(n_tf):
        chunk = rows[i * tf_hours:(i + 1) * tf_hours]
        cog_bars.append((T0 + i * tf_hours * H, chunk[0][0],
                         max(x[1] for x in chunk), min(x[2] for x in chunk),
                         chunk[-1][3], 1.0))
    ev = {i: list(evs) for i, evs in (events_by or {}).items()}
    cog = SymbolCognition(symbol="TEST", bars=cog_bars,
                          atrs=[ATR] * n_tf, events_by=ev, pivots_by={},
                          views={i: view for i in ev})
    return build_campaign_frame(cog, df_exec, f"{tf_hours}h")


def _run(frames, **over):
    kw = dict(KW)
    kw.update(over)
    rp = kw.get("router_params")
    if rp is not None and rp.whitelist != WL16:
        import dataclasses
        kw["router_params"] = dataclasses.replace(rp, whitelist=WL16)
    kw.setdefault("router_params", RP16)
    return run_campaigns(frames, **kw)


E1_SHORT = lambda i, th: _ev(BREAKOUT_ACCEPTED, -1, i, th)


# ---------------------------------------------------------------------------
# arm0 / canonical parity(回归锚)
# ---------------------------------------------------------------------------


def test_arm0_none_vs_default_armconfig_vs_v14router_bitwise():
    frames = {"TEST@2h": _frame(2, events_by={2: [E1_SHORT(2, 2)]})}
    r_none = _run(frames)
    r_default = _run(frames, arm=ArmConfig())          # 全默认 ⇒ 不接管
    assert json.dumps(r_none.ledger) == json.dumps(r_default.ledger)
    assert r_none.counters == r_default.counters
    assert not any(k.startswith(("stop_entry", "ladder", "maker_entry",
                                 "virtual_entry", "quality_"))
                   for k in r_none.counters)
    r_q_none = _run(frames, router_params=V14Params(), router_factory=V14Router)
    assert json.dumps(r_none.ledger) == json.dumps(r_q_none.ledger)
    assert r_none.counters == r_q_none.counters        # quality=None 短路逐位
    r_v6 = _run(frames, router_params=V6Params(), router_factory=V6Router)
    assert json.dumps(r_none.ledger) == json.dumps(r_v6.ledger)


def test_default_armconfig_flags_all_off():
    a = ArmConfig()
    assert (a.fill_model, a.stop_entry, a.ladder_fade) == ("canonical",
                                                           False, False)


# ---------------------------------------------------------------------------
# C1 优质才进 + C5 档位配注
# ---------------------------------------------------------------------------


def test_quality_gate_rejects_bad_pos_and_counts_reason():
    # 事件挂最末认知 bar ⇒ explain() 保留拒单 reason(_notes 逐 bar 生命周期)
    frames = {"TEST@2h": _frame(2, events_by={7: [E1_SHORT(7, 2)]},
                                view=VIEW_BAD_POS)}
    res = _run(frames, router_params=V14Params(quality=QPARAMS),
               router_factory=V14Router)
    assert res.counters["quality_evaluated"] == 1
    assert res.counters["quality_reject_q1_pos"] == 1
    assert not [r for r in res.ledger if r["kind"] == "OPEN"]
    notes = res.campaign_states["TEST@2h"]["router"].explain()
    assert "优质才进" in notes and "q1_pos" in notes


def test_quality_gate_rejects_crowded_room_q2():
    # 对面支撑缘 94.5 距 close=96.0 仅 1.5/4=0.375R < 1.5R ⇒ Q2 拒
    view = StructureView(
        regime=Regime.TREND_DOWN,
        zones=(Zone(lo=93.0, hi=94.5, role=ZoneRole.SUPPORT,
                    state=ZoneState.ACTIVE),),
        atr=ATR,
        pivots=(Pivot(price=95.0, forming_index=0, known_index=1,
                      kind=PivotKind.HIGH),
                Pivot(price=90.0, forming_index=2, known_index=3,
                      kind=PivotKind.LOW)))
    frames = {"TEST@2h": _frame(2, events_by={7: [E1_SHORT(7, 2)]}, view=view)}
    res = _run(frames, router_params=V14Params(quality=QPARAMS),
               router_factory=V14Router)
    assert res.counters["quality_reject_q2_room"] == 1
    assert res.counters.get("quality_reject_q1_pos", 0) == 0
    assert not [r for r in res.ledger if r["kind"] == "OPEN"]


def test_quality_tier_b_size_mult_reaches_fill():
    frames = {"TEST@2h": _frame(2, events_by={2: [E1_SHORT(2, 2)]})}
    res_q = _run(frames, router_params=V14Params(quality=QPARAMS),
                 router_factory=V14Router)
    res_c = _run(frames)
    oq = [r for r in res_q.ledger if r["kind"] == "OPEN"]
    oc = [r for r in res_c.ledger if r["kind"] == "OPEN"]
    assert len(oq) == len(oc) == 1
    # STRUCTURE_BROKEN 类无 zone / 顺风 TREND_DOWN 短但 zone_touches 由事件携带 … 此处无 zone
    # ⇒ Q3 不成熟 ⇒ B 档 0.75 配注
    assert oq[0]["meta"]["quality_tier"] == "B"
    assert oq[0]["qty"] == pytest.approx(oc[0]["qty"] * 0.75, rel=0.011)
    assert res_q.counters["quality_tier_b"] == 1
    assert compute_metrics(res_q)["n_trades"] == 1


def test_quality_determinism_double_run():
    frames = {"TEST@2h": _frame(2, events_by={2: [E1_SHORT(2, 2)]})}
    kw = dict(router_params=V14Params(quality=QPARAMS), router_factory=V14Router,
              arm=ArmConfig(stop_entry=True))
    r1 = _run(frames, **kw)
    r2 = _run(frames, **kw)
    assert json.dumps(r1.ledger) == json.dumps(r2.ledger)
    assert r1.counters == r2.counters


# ---------------------------------------------------------------------------
# C3 stop-entry 预埋(破位参考价 − k_bm·atr;12×TF 超时作废)
# ---------------------------------------------------------------------------


def test_stop_entry_armed_at_ref_minus_kbm_and_trigger_fill():
    # 空 BREAKOUT_ACCEPTED(决策 2h bar2,close=96.0):ref=99.5 ⇒ stop=99.5−0.1·2=99.3;行情
    # 已在 96 区(sell stop 向下穿过语),首个可成交细 bar(决策 bar 后)即触发
    frames = {"TEST@2h": _frame(2, events_by={2: [E1_SHORT(2, 2)]})}
    res = _run(frames, arm=ArmConfig(stop_entry=True))
    c = res.counters
    assert c["stop_entry_armed"] == 1 and c["stop_entry_triggers"] == 1
    assert c["stop_entry_fills"] == 1
    trig = [r for r in res.ledger if r["kind"] == "TRIGGER"]
    assert len(trig) == 1 and trig[0]["reason_code"] == "STOP_TRIGGER"
    assert trig[0]["ts"] == T0 + 6 * H                # 决策 bar 后首根 1h 细 bar
    assert trig[0]["meta"]["expiry_ts"] == T0 + 5 * H + 12 * 2 * H
    o = [r for r in res.ledger if r["kind"] == "OPEN"][0]
    assert o["meta"]["entry_mode"] == "stop_entry"
    assert o["meta"]["stop_ref"] == 99.5
    assert o["decision_ts"] == T0 + 6 * H < o["ts"] == T0 + 7 * H


def test_stop_entry_timeout_misses_no_trade():
    # 价永不回穿 99.3 ⇒ 超时 12×TF(2h)作废,漏单=stopex_expired
    ov = {i: (100.5, 101.0, 99.6, 100.2) for i in range(3, 32)}
    frames = {"TEST@2h": _frame(2, events_by={1: [E1_SHORT(1, 2)]},
                                exec_overrides=ov, n_1h=32)}
    res = _run(frames, arm=ArmConfig(stop_entry=True))
    c = res.counters
    assert c["stop_entry_armed"] == 1
    assert c["stop_entry_expired"] == 1
    assert c.get("stop_entry_fills", 0) == 0
    exp = [r for r in res.ledger if r["kind"] == "EXPIRED"]
    assert len(exp) == 1
    assert exp[0]["meta"]["expiry_ts"] == T0 + 3 * H + 12 * 2 * H
    assert exp[0]["ts"] == T0 + 28 * H                # 首个越 expiry 的小时 bar


# ---------------------------------------------------------------------------
# C2 maker 双模式(市价决策 → 决策收盘参考价挂单,arm2/arm3 分流)
# ---------------------------------------------------------------------------


def test_c2_exchange_converts_market_to_maker_limit_and_counts():
    # BREAKOUT_ACCEPTED 空方市价 ⇒ 决策收盘(96.0)限价卖单(maker);次细 bar 高 96.8 穿过成交
    frames = {"TEST@2h": _frame(2, events_by={2: [E1_SHORT(2, 2)]})}
    res = _run(frames, arm=ArmConfig(fill_model="exchange_maker"))
    c = res.counters
    assert c["maker_entry_armed"] == 1 and c["maker_entry_fills"] == 1
    lf = [r for r in res.ledger if r["kind"] == "LIMIT_FILL"
          and r["meta"].get("order_type") == "maker_entry"]
    assert len(lf) == 1
    assert lf[0]["decision_ts"] == T0 + 4 * H < lf[0]["ts"] == T0 + 6 * H
    assert lf[0]["price"] == 96.0 and lf[0]["meta"]["entry_mode"] == "limit"
    assert lf[0]["fee"] == pytest.approx(lf[0]["qty"] * 96.0 * 0.0002)  # maker
    assert not [r for r in res.ledger if r["kind"] == "OPEN"]           # 无市价成交


def test_c2_virtual_guarantees_fill_on_touch_with_taker_fee():
    # 空方 virtual 挂 96.0:次细 bar high=96.8 ≥ 96(touch)⇒ 再次细 bar 开盘成交
    frames = {"TEST@2h": _frame(2, events_by={2: [E1_SHORT(2, 2)]})}
    res = _run(frames, arm=ArmConfig(fill_model="virtual_limit"))
    c = res.counters
    assert c["virtual_entry_armed"] == 1
    assert c["virtual_entry_triggers"] == 1
    assert c["virtual_entry_fills"] == 1
    o = [r for r in res.ledger if r["kind"] == "OPEN"][0]
    assert o["meta"]["entry_mode"] == "virtual_limit"
    assert o["meta"]["limit_price"] == pytest.approx(96.0)
    assert o["decision_ts"] == T0 + 6 * H < o["ts"] == T0 + 7 * H
    assert o["fee"] == pytest.approx(o["qty"] * o["price"] * 0.0006, rel=1e-9)


# ---------------------------------------------------------------------------
# C4 fade 分层(3 份各 1/3 风险份,合并均价/同交易、未成交份过期)
# ---------------------------------------------------------------------------


def _fade_frame(exec_overrides=None, n_1h=16):
    zone = Zone(lo=95.0, hi=97.0, role=ZoneRole.RESISTANCE,
                state=ZoneState.ACTIVE, touches=4)
    ev = _ev(LEVEL_REJECTED, -1, 2, 2, ref=97.1, invalid=99.7, zone=zone)
    return _frame(2, events_by={2: [ev]}, exec_overrides=exec_overrides,
                  n_1h=n_1h)


def test_ladder_three_slices_merged_third_risk_weighted():
    # 分层价:前缘 95/中 96/内缘 97(sell);后续全程 96.4>96 穿过前两份,<97
    ov = {i: (95.8, 96.4, 95.0, 95.6) for i in range(3, 32)}
    res = _run({"TEST@2h": _fade_frame(ov, n_1h=32)},
               arm=ArmConfig(ladder_fade=True))
    c = res.counters
    assert c["ladder_signals"] == 1 and c["ladder_armed"] == 3
    fills = [r for r in res.ledger if r["kind"] == "LIMIT_FILL"
             and r["meta"].get("order_type") == "ladder_entry"
             and not r["meta"].get("ladder_add")]
    adds = [r for r in res.ledger if r["meta"].get("ladder_add")]
    assert len(fills) == 1 and len(adds) == 1            # 首份 95 + 合并份 96
    assert len({r["trade_id"] for r in fills + adds}) == 1   # 同一笔交易
    assert c["ladder_fills"] == 2 and c["ladder_add_fills"] == 1
    # 第三份(内缘 97)未成交 ⇒ 过期(T0+18H)
    exp = [r for r in res.ledger if r["kind"] == "EXPIRED"]
    assert len(exp) == 1 and exp[0]["meta"]["limit_price"] == 97.0
    assert exp[0]["meta"]["ladder_group"] == f"TEST@2h#{T0 + 4 * H}#LEVEL_REJECTED"
    assert c.get("ladder_entry_expired", 0) == 1
    # 合并均价 = 95/96 加权(各 1/3 风险份额 ⇒ 同距近等量),低于一口价 95?
    q1, q2 = fills[0]["qty"], adds[0]["qty"]
    assert fills[0]["price"] == 95.0 and adds[0]["price"] == 96.0
    assert 0.7 < q2 / q1 < 1.4
    # 合并风险 = Σ 份额(≈2/3 全份)
    res_c = _run({"TEST@2h": _fade_frame(ov, n_1h=32)}, arm=ArmConfig())
    oc = [r for r in res_c.ledger if r["kind"] == "OPEN"]
    pos_qty = q1 + q2
    assert oc and abs(pos_qty - oc[0]["qty"] * 2 / 3) < oc[0]["qty"] * 0.2
    # 合并均价入账(broker 持仓加权):95·q1+96·q2 之间
    merged = (95.0 * q1 + 96.0 * q2) / pos_qty
    assert 95.0 < merged < 96.0


def test_ladder_expiry_counts_and_causal_rows():
    res = _run({"TEST@2h": _fade_frame()}, arm=ArmConfig(ladder_fade=True))
    for r in res.ledger:
        assert r["ts"] > r["decision_ts"]


# ---------------------------------------------------------------------------
# 配置对账(config/v14.json 与 canonical 共享键逐项一致;arms 矩阵冻结)
# ---------------------------------------------------------------------------


def test_config_v14_matches_canonical_shared_keys():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    v14 = json.loads((root / "config" / "v14.json").read_text(encoding="utf-8"))
    ca = json.loads((root / "config" / "canonical.json").read_text(encoding="utf-8"))
    for k, v in ca.items():
        if k in ("version", "meta", "note"):
            continue
        assert k in v14, f"v14 缺 canonical 键 {k}"
        assert v14[k] == v, f"{k}: v14={v14[k]!r} != canonical={v!r}"
    assert set(v14["arms"]) == {f"arm{i}" for i in range(7)}
    a = v14["arms"]
    assert (a["arm0"]["c1_quality"], a["arm0"]["fill_model"]) == (False,
                                                                  "canonical")
    assert a["arm2"]["fill_model"] == "exchange_maker"
    assert a["arm3"]["fill_model"] == "virtual_limit"
    assert a["arm4"]["c3_stop_entry"] is True \
        and a["arm4"]["c4_laddering"] is False
    assert a["arm5"]["c4_laddering"] is True \
        and a["arm5"]["c3_stop_entry"] is False
    six = a["arm6"]
    assert six["c1_quality"] and six["c3_stop_entry"] and six["c4_laddering"]
    assert six["fill_model"] == "exchange_maker"
    assert v14["c1_quality_params"]["k_room_R"] == 1.5
    assert v14["c1_quality_params"]["k_star"] == 2
    assert v14["c3_params"]["stop_wait_tf"] == 12
    assert v14["c4_params"]["ladder_slices"] == 3
