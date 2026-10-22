"""v15 四件套合成(docs/v15-protocol.md)条款/接线测试 + 配置冻结对账。

钉死:
- 带沿条款边界:inclusive(pos=0.35 空过、pos=0.65 多过,半步外拒)、
  缺侧 pivot/带退化 fail-closed、pos 计算与 quality.swing_band_pos 同值;
- arm0 回归:V15Router(band_side=None)与 MarketRouter 账本逐位一致;
- 组件启停矩阵:maker 仅 fade 家族(c2_family)、stop-entry 仅续涨家族
  (未匹配 C3 的续涨信号落回 canonical)、TREND_RESUMED/RANGE_ROTATED 不落 C2;
- v14 行为保存:ArmConfig 缺省 c2_family=None ⇒ C2 接管全部入口(逐位);
- 确定性双跑;config/v15.json 与 canonical 共享键逐项一致、开关矩阵冻结。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from market_operator.replay import SymbolCognition
from market_operator.bandside import (
    BandSideParams,
    V15Params,
    V15Router,
    band_side_check,
)
from market_operator.broker import ContractSpec
from market_operator.campaign import ArmConfig, build_campaign_frame, run_campaigns
from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, TREND_RESUMED, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.quality import swing_band_pos
from market_operator.routing import RouterParams, MarketRouter
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
FADE = ("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED")
BS = BandSideParams()
V15_ARM = ArmConfig(fill_model="exchange_maker", stop_entry=True,
                    c2_family=FADE)
VIEW_OK = StructureView(
    regime=Regime.TREND_DOWN,
    zones=(Zone(lo=80.0, hi=82.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE, touches=4),),
    atr=ATR,
    pivots=(Pivot(price=95.0, forming_index=0, known_index=1,
                  kind=PivotKind.HIGH),
            Pivot(price=90.0, forming_index=2, known_index=3,
                  kind=PivotKind.LOW)))
VIEW_BAD_POS = StructureView(          # band [100,110]:pos<0.35 ⇒ 空带沿拒
    regime=Regime.RANGE,
    zones=(Zone(lo=108.0, hi=110.0, role=ZoneRole.SUPPORT,
                state=ZoneState.ACTIVE),),
    atr=ATR,
    pivots=(Pivot(price=110.0, forming_index=0, known_index=1,
                  kind=PivotKind.HIGH),
            Pivot(price=100.0, forming_index=2, known_index=3,
                  kind=PivotKind.LOW)))


def _pivots(hi, lo):
    return (Pivot(price=hi, forming_index=0, known_index=1,
                  kind=PivotKind.HIGH),
            Pivot(price=lo, forming_index=2, known_index=3,
                  kind=PivotKind.LOW))


def _ev(eid, direction, tf_idx, tf_hours, ref=99.5, zone=None):
    attrs = {"ref": ref, "invalid": 99.7, "started": 0, "atr": ATR}
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
    kw.setdefault("router_params", RouterParams(whitelist=WL16))
    return run_campaigns(frames, **kw)


def _fade_zone():
    return Zone(lo=95.0, hi=97.0, role=ZoneRole.RESISTANCE,
                state=ZoneState.ACTIVE, touches=4)


# ---------------------------------------------------------------------------
# 带沿条款边界(纯函数;inclusive 边界 + fail-closed + quality 同值)
# ---------------------------------------------------------------------------


def test_band_side_short_boundary_inclusive():
    pvs = _pivots(hi=110.0, lo=100.0)          # band 宽 10
    ok, pos = band_side_check(BS, direction=-1, entry_ref=103.5, pivots=pvs)
    assert ok and pos == pytest.approx(0.35)   # 下沿触线即过(inclusive)
    ok, pos = band_side_check(BS, direction=-1, entry_ref=103.49, pivots=pvs)
    assert not ok                              # 错半边拒


def test_band_side_long_boundary_inclusive():
    pvs = _pivots(hi=110.0, lo=100.0)
    ok, pos = band_side_check(BS, direction=1, entry_ref=106.5, pivots=pvs)
    assert ok and pos == pytest.approx(0.65)   # 上沿触线即过(inclusive)
    ok, pos = band_side_check(BS, direction=1, entry_ref=106.51, pivots=pvs)
    assert not ok


def test_band_side_fail_closed_missing_side_or_degenerate():
    only_hi = (Pivot(price=110.0, forming_index=0, known_index=1,
                     kind=PivotKind.HIGH),)
    ok, pos = band_side_check(BS, direction=-1, entry_ref=103.5,
                              pivots=only_hi)
    assert not ok and pos is None              # 缺 LOW 侧 ⇒ 门不可判 ⇒ 拒
    ok, pos = band_side_check(BS, direction=-1, entry_ref=103.5,
                              pivots=_pivots(hi=100.0, lo=100.0))
    assert not ok and pos is None              # H*−L*≤0 退化 ⇒ 拒


def test_band_side_pos_identical_to_quality_swing_band_pos():
    pvs = _pivots(hi=97.3, lo=90.1)
    for ref in (90.1, 93.7, 97.3, 99.9):
        _, pos = band_side_check(BS, direction=1, entry_ref=ref, pivots=pvs)
        assert pos == swing_band_pos(pvs, ref)  # 同函数同值,不重造口径


# ---------------------------------------------------------------------------
# arm0 回归 / 路由条款计数
# ---------------------------------------------------------------------------


def test_v15router_none_bitwise_v6router():
    frames = {"TEST@2h": _frame(2, events_by={2: [_ev(BREAKOUT_ACCEPTED, -1, 2, 2)]})}
    r_v6 = _run(frames, router_factory=MarketRouter)
    r_15 = _run(frames, router_params=V15Params(whitelist=WL16),
                router_factory=V15Router)
    assert json.dumps(r_v6.ledger) == json.dumps(r_15.ledger)
    assert r_v6.counters == r_15.counters      # band_side=None 短路逐位
    assert not any(k.startswith("band_side") for k in r_15.counters)


def test_band_side_rejects_wrong_half_and_counts():
    frames = {"TEST@2h": _frame(2, events_by={7: [_ev(BREAKOUT_ACCEPTED, -1, 7, 2)]},
                                view=VIEW_BAD_POS)}
    res = _run(frames, router_params=V15Params(whitelist=WL16, band_side=BS),
               router_factory=V15Router)
    assert res.counters["band_side_evaluated"] == 1
    assert res.counters["band_side_reject_pos"] == 1
    assert not [r for r in res.ledger if r["kind"] == "OPEN"]
    notes = res.campaign_states["TEST@2h"]["router"].explain()
    assert "带沿拒" in notes and "pos=" in notes


def test_band_side_pass_carries_band_pos_no_tier_meta():
    frames = {"TEST@2h": _frame(2, events_by={2: [_ev(BREAKOUT_ACCEPTED, -1, 2, 2)]})}
    res = _run(frames, router_params=V15Params(whitelist=WL16, band_side=BS),
               router_factory=V15Router, arm=ArmConfig(stop_entry=True))
    o = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(o) == 1
    assert o[0]["meta"]["band_pos"] == pytest.approx(1.2)    # 溯源列
    assert "size_mult" not in o[0]["meta"]                   # 只拒不配档
    assert "quality_tier" not in o[0]["meta"]


# ---------------------------------------------------------------------------
# 组件启停矩阵(maker 仅 fade;stop 仅续涨;c2_family=None 保存 v14)
# ---------------------------------------------------------------------------


def test_c2_family_fade_only_maker_armed_continuation_canonical():
    # BREAKOUT_ACCEPTED 续涨:c2_family=fade 即使 fill_model=exchange_maker 也不接管 ⇒
    # 无 stop_entry 时落回 canonical 市价;LEVEL_REJECTED fade ⇒ maker 真挂单。
    # fade 限价挂出后价格反向远离,12×N_fill 细 bar 内不触价 ⇒ 过期(BREAKOUT_ACCEPTED
    # 决策在过期之后,槽位已空)。
    zone = _fade_zone()
    evs = {1: [_ev(LEVEL_REJECTED, -1, 1, 2, ref=97.1, zone=zone)],
           10: [_ev(BREAKOUT_ACCEPTED, -1, 10, 2)]}
    # 决策收盘 96.9 ⇒ 空限价 96.9;后续 bar 高点恒 ≤96.0 永不回穿 ⇒ 过期
    ov = {i: (95.5, 96.0, 94.6, 95.2) for i in range(4, 21)}
    frames = {"TEST@2h": _frame(2, events_by=evs, exec_overrides=ov, n_1h=32)}
    res = _run(frames, arm=ArmConfig(fill_model="exchange_maker",
                                     c2_family=FADE))
    c = res.counters
    assert c["maker_entry_armed"] == 1                       # 仅 LEVEL_REJECTED fade
    lf = [r for r in res.ledger if r["kind"] == "LIMIT_FILL"
          and r["meta"].get("order_type") == "maker_entry"]
    exps = [r for r in res.ledger if r["kind"] == "EXPIRED"]
    assert not lf and len(exps) == 1                         # 偏离后未触价过期
    o = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(o) == 1 and o[0]["meta"]["event_id"] == "BREAKOUT_ACCEPTED"  # 续涨市价兜底


def test_stop_entry_only_continuation_fade_not_stopped():
    frames = {"TEST@2h": _frame(2, events_by={2: [_ev(BREAKOUT_ACCEPTED, -1, 2, 2)]})}
    res = _run(frames, arm=V15_ARM)
    c = res.counters
    assert c["stop_entry_armed"] == 1 and c.get("maker_entry_armed", 0) == 0
    assert c["stop_entry_fills"] == 1


def test_fade_event_never_routes_to_stop_entry():
    zone = _fade_zone()
    frames = {"TEST@2h": _frame(2, events_by={2: [_ev(LEVEL_REJECTED, -1, 2, 2, ref=97.1,
                                                      zone=zone)]})}
    res = _run(frames, arm=V15_ARM)
    c = res.counters
    assert c.get("stop_entry_armed", 0) == 0
    assert c["maker_entry_armed"] == 1      # fade ⇒ maker,不挂 stop


def test_c2_family_none_preserves_v14_takeover_all():
    frames = {"TEST@2h": _frame(2, events_by={2: [_ev(BREAKOUT_ACCEPTED, -1, 2, 2)]})}
    res = _run(frames, arm=ArmConfig(fill_model="exchange_maker"))
    c = res.counters                        # 缺省 c2_family=None ⇒ v14 逐位
    assert c["maker_entry_armed"] == 1 and c["maker_entry_fills"] == 1
    assert not [r for r in res.ledger if r["kind"] == "OPEN"]


def test_e4_not_in_families_never_maker():
    frames = {"TEST@2h": _frame(2, events_by={2: [_ev(TREND_RESUMED, -1, 2, 2)]})}
    res = _run(frames, arm=V15_ARM)
    c = res.counters                        # TREND_RESUMED 不在两族 ⇒ canonical 市价兜底
    assert c.get("maker_entry_armed", 0) == 0
    assert c.get("stop_entry_armed", 0) == 0
    o = [r for r in res.ledger if r["kind"] == "OPEN"]
    assert len(o) == 1 and o[0]["meta"]["event_id"] == "TREND_RESUMED"


# ---------------------------------------------------------------------------
# 确定性 / 配置对账
# ---------------------------------------------------------------------------


def test_composition_determinism_double_run():
    zone = _fade_zone()
    frames = {"TEST@2h": _frame(2, events_by={1: [_ev(LEVEL_REJECTED, -1, 1, 2, ref=97.1,
                                                      zone=zone)],
                                              4: [_ev(BREAKOUT_ACCEPTED, -1, 4, 2)]})}
    kw = dict(router_params=V15Params(whitelist=WL16, band_side=BS),
              router_factory=V15Router, arm=V15_ARM)
    r1 = _run(frames, **kw)
    r2 = _run(frames, **kw)
    assert json.dumps(r1.ledger) == json.dumps(r2.ledger)
    assert r1.counters == r2.counters


def test_config_v15_matches_canonical_shared_keys_and_switch_matrix():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    v15 = json.loads((root / "config" / "v15.json").read_text(encoding="utf-8"))
    ca = json.loads((root / "config" / "canonical.json").read_text(
        encoding="utf-8"))
    for k, v in ca.items():
        if k in ("version", "meta", "note"):
            continue
        assert k in v15, f"v15 缺 canonical 键 {k}"
        assert v15[k] == v, f"{k}: v15={v15[k]!r} != canonical={v!r}"
    assert set(v15["arms"]) == {"arm0", "v15"}
    a = v15["arms"]
    on = a["v15"]
    assert on["band_side"] is True
    assert on["fill_model"] == "exchange_maker"
    assert on["c3_stop_entry"] is True
    assert on["c4_laddering"] is False and on["tiersize"] is False
    off = a["arm0"]
    assert not off["band_side"] and off["fill_model"] == "canonical"
    assert not off["c3_stop_entry"] and not off["c4_laddering"] \
        and not off["tiersize"]
    assert v15["c2_params"]["family"] == ["BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"]
    assert v15["band_side_params"]["pos_short_min"] == 0.35
    assert v15["band_side_params"]["pos_long_max"] == 0.65
    assert v15["c3_params"]["stop_wait_tf"] == 12
    assert "Q2" in v15["off_note"] and "分层" in v15["off_note"]
