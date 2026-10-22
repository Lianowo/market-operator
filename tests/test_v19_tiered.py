"""v19 阶梯配注 + 夭折处置(docs/v19-protocol.md;research-log #35 预注册)
测试 —— 配注各档边界/硬禁恰界/半窗恰界/连续逆根/parity/确定性/arm-C 锚。

钉死:
- config/v19.json parent=v18 硬对账 + tier_sizing + cap 0.08 + arms 开关
  + v19_params 冻结阈值 + arm-C 锚探针与 v17 arm-C 构建逐项同值;
- 配注纯函数:room 恰 1.0 入中档×0.75 / 恰 1.5 不扣 /<1.0×0.5;pos 边界
  inclusive 与失位×0.5(缺判同);TRANSITION fade×0.5(非 fade 豁免);
  rngq 恰 0.8 受理 / 超 0.8×0.75(mic-only 处豁免);pmd 恰 −0.003 计档
  ×0.5;多条款相乘(0.5×0.75=0.375);硬禁 F1 / target_missing(恰界);
- 夭折:半窗恰界(23 根不出 / 24 根 MFE<0.3R 出,恰 0.3R 持有)+ 连续≥3
  逆根(恰 0 收平复位)+ 先触发先算(同 bar 同触发 ⇒ 半窗优先);
- parity:V19Router(tiered=None) ≡ MarketRouter;early_exit=False 时夭折块
  整段不生效;
- 战役级:size_mult 上成交数量,v19tier 记账后缀;半窗夭折真实出场与
  early_half_mfe 收割桶标签;确定性双跑。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd
import pytest

from market_operator.replay import SymbolCognition
from market_operator.broker import ContractSpec
from market_operator.campaign import (
    ArmConfig,
    STOP_FAMILY,
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.position import (
    EARLY_CONSEC_N,
    EARLY_HALF_MFE_R,
    PositionManager,
)
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
from market_operator.tiered import (
    TieredParams,
    V19Params,
    V19Router,
    evaluate_tiered,
)
from market_operator.trigger import MicCognition, TRIGGER_TF_MAP

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_v17  # noqa: E402
import run_v19  # noqa: E402

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
          params=P2, n_fill=12, portfolio_cap_frac=0.08)

PIVOTS = (Pivot(price=110.0, forming_index=0, known_index=1,
                kind=PivotKind.HIGH),
          Pivot(price=80.0, forming_index=2, known_index=3,
                kind=PivotKind.LOW))

WL_ALL = frozenset((eid, d) for eid in ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED",
                                        "STRUCTURE_BROKEN", "FLIP_RETESTED", "EDGE_RECLAIMED")
                   for d in (-1, 1))

# 满档对照区(短单 ref=96.5,risk=4.2 ⇒ room=(96.5−90)/4.2=1.5476 ≥1.5)
SUPPORT_FULL = Zone(lo=86.0, hi=90.0, role=ZoneRole.SUPPORT,
                    state=ZoneState.ACTIVE, touches=2)
# 远缘不参与收割/触发(long hold 场景;room 也不扣)
SUPPORT_FAR = Zone(lo=80.0, hi=82.0, role=ZoneRole.SUPPORT,
                   state=ZoneState.ACTIVE, touches=2)


def _view(zones=(), pivots=PIVOTS, regime=Regime.TRANSITION) -> StructureView:
    return StructureView(regime=regime, zones=zones, atr=ATR, pivots=pivots)


def _vp19(tp=None) -> V19Params:
    kw = {f: getattr(RouterParams(), f) for f in RouterParams.__dataclass_fields__}
    kw["whitelist"] = WL_ALL
    return V19Params(**kw, tiered=tp)


def _fade_e3(*, ts, known_index, invalid=100.7):
    zone = Zone(lo=99.0, hi=100.5, role=ZoneRole.RESISTANCE,
                state=ZoneState.ACTIVE, touches=3)
    attrs = {"zone": zone, "ref": 100.5, "invalid": invalid}
    return MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=ts, known_index=known_index,
                       attrs=attrs)


def _bar(label, close=96.5, low=96.3, high=96.7, open_=96.5):
    return (label, open_, high, low, close, 1.0)


def _frames_2h(events_cog: dict, events_mic: dict, *, n_cog=24, view=None,
               cog_bars=None):
    n_exec, n_trig = n_cog * 2, n_cog * 4
    trig = [_bar(T0 + i * M30) for i in range(n_trig)]
    exec_rows = [_bar(T0 + i * H) for i in range(n_exec)]
    df_exec = pd.DataFrame(exec_rows,
                           columns=["ts", "open", "high", "low", "close",
                                    "volume"])
    cogs = (cog_bars if cog_bars is not None
            else [_bar(T0 + i * 2 * H) for i in range(n_cog)])
    vw = view if view is not None else _view(zones=(SUPPORT_FULL,))
    cog = SymbolCognition(symbol="TEST", bars=cogs, atrs=[ATR] * len(cogs),
                          events_by=dict(events_cog), pivots_by={},
                          views={i: vw for i in events_cog})
    mic = MicCognition(
        symbol="TEST", obs_tf_seconds=7200, trigger_tf_seconds=1800,
        scale=4, bars=trig, cog_index_of=[i // 4 for i in range(n_trig)],
        atrs=[ATR] * n_trig, events_by=dict(events_mic),
        views={a: vw for a in events_mic},
        bar_events_by=dict(events_cog), counters={})
    return build_campaign_frame(cog, df_exec, "2h", mic_cog=mic)


def _arm_v19(*, early=True, front_edge=False, fill="canonical",
             stop=False) -> ArmConfig:
    return ArmConfig(fill_model=fill, stop_entry=stop, stop_wait_tf=12,
                     k_bm=0.1,
                     c2_family=(("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED") if front_edge else None),
                     harvest_family=("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"),
                     time_cap_bars=48, cap_mfe_min=0.5, early_exit=early)


def _run19(f, *, tp=None, arm=None, factory=V19Router):
    return run_campaigns({campaign_key("TEST", "2h"): f}, **dict(KW),
                         router_params=_vp19(tp if tp is not None
                                             else TieredParams()),
                         router_factory=factory,
                         arm=arm if arm is not None else _arm_v19(),
                         enable_mic=True, mic_families=STOP_FAMILY)


def _cogs_big_first(n=24):
    """前史 3 根大波幅(rngq 事件 bar 波幅 0.4 ⇒ 分位 0.0,不触大蛋糕)。"""
    cs = [_bar(T0 + i * 2 * H) for i in range(n)]
    for i in (0, 1, 2):
        cs[i] = (T0 + i * 2 * H, 97.0, 98.5, 95.0, 96.0, 1.0)
    return cs


def _short(*, invalid=100.7, zones=(SUPPORT_FULL,), ref=96.5, pivots=PIVOTS,
           fade=True, regime=Regime.TREND_DOWN, atr=0.8, rngq=None, pmd=None,
           mic_path=False):
    return evaluate_tiered(TieredParams(), direction=-1, entry_ref=ref,
                           invalid=invalid, atr=atr, zones=zones,
                           pivots=pivots, regime=regime, zone_touches=3,
                           rngq=rngq, pmd=pmd, mic_path=mic_path, fade=fade)


# ---------------------------------------------------------------------------
# 1. config/v19.json parent=v18 硬对账 + 冻结阈值 + 臂接线 + arm-C/v17 锚
# ---------------------------------------------------------------------------


def test_v19_config_parent_v18_hard_check():
    cfg = run_v19.load_config()          # 不 raise = 全部硬对账过
    v19 = json.loads(open("config/v19.json", encoding="utf-8").read())
    v18 = json.loads(open("config/v18.json", encoding="utf-8").read())
    allow = {"version", "note", "meta", "arms", "arm", "gates_model",
             "portfolio_cap_frac", "v19_params", "v16_params", "v18_params"}
    for k in set(v19) | set(v18):
        if k in allow:
            continue
        assert v19.get(k) == v18.get(k), f"parent=v18 逐键不许漂移({k})"
    for pk in ("v16_params", "v18_params"):
        p19 = {k: v for k, v in v19[pk].items() if k != "note"}
        p18 = {k: v for k, v in v18[pk].items() if k != "note"}
        assert p19 == p18, f"{pk} 数值面必须与 v18 深等(链先验,note 为叙述)"
    assert cfg["portfolio_cap_frac"] == 0.08       # 裁决①
    assert cfg["gates_model"] == "tier_sizing"     # 裁决②
    assert cfg["tf_list"] == ["2h", "4h", "12h", "1d"] \
        and "1h" not in cfg["tf_list"]
    assert cfg["equity"] == 1000.0
    assert {int(k): int(v) for k, v in
            cfg["trigger_tf_map"].items()} == TRIGGER_TF_MAP
    expect = {"arm-T": ("market", False), "arm-X": ("market", True),
              "arm-maker": ("maker", True), "arm-stop": ("stop_entry", True)}
    for a, (ex, early) in expect.items():
        tp, factory, arm, mic_f = run_v19.build_arm(cfg, a)
        assert factory is V19Router and mic_f == STOP_FAMILY
        assert arm.early_exit is early, f"{a} 夭折开关"
        assert tp.fade_front_edge is (ex == "maker")
        assert arm.fill_model == ("exchange_maker" if ex == "maker"
                                  else "canonical")
        assert arm.stop_entry is (ex == "stop_entry")
        assert arm.harvest_family == ("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED")
        assert arm.time_cap_bars == 48 and not arm.harvest_ladder3  # R4 不回锅
    p19 = v19["v19_params"]
    d = TieredParams()
    for k in ("pos_short_min", "pos_long_max", "k_room_R", "room_lo", "k_is",
              "k_star", "mult_pos", "mult_room_mid", "mult_room_lo",
              "mult_transition_fade", "mult_rngq_big", "mult_pmd_adverse"):
        assert p19[k] == getattr(d, k), f"v19_params.{k} 冻结漂移"
    assert p19["early_half_mfe_r"] == EARLY_HALF_MFE_R == 0.3
    assert p19["early_consec_n"] == EARLY_CONSEC_N == 3


def test_v19_arm_c_probe_equals_v17_arm_c():
    """arm-C/v17 锚探针:构建逐项同值(V16Router/ArmConfig/mic 拓扑/HP)。"""
    cfg19 = json.loads(open("config/v19.json", encoding="utf-8").read())
    cfg17 = json.loads(open("config/v17.json", encoding="utf-8").read())
    hp19, fac19, arm19, mic19 = run_v19.build_arm_c_probe(cfg19)
    hp17, fac17, arm17, mic17 = run_v17.build_arm(cfg17, "arm-C")
    assert hp19 == hp17 and fac19 is fac17
    assert arm19 == arm17, "ArmConfig 必须逐项同值(early/ladder3 默认关)"
    assert mic19 == mic17 == STOP_FAMILY


# ---------------------------------------------------------------------------
# 2. 配注纯函数:room 三档恰界 + pos 边界 + 硬禁恰界
# ---------------------------------------------------------------------------


def test_v19_tier_room_tiers_boundaries():
    # 恰界稳数:ref=100 / invalid=105 ⇒ risk 恰 5.0;空 room = (100−z.hi)/5
    def z(x):
        return (Zone(lo=70.0, hi=100.0 - x * 5.0, role=ZoneRole.SUPPORT,
                     state=ZoneState.ACTIVE, touches=2),)
    v = _short(ref=100.0, invalid=105.0, zones=z(1.5))   # 恰 1.5 ⇒ 不扣
    assert v.tradable and v.size_mult == 1.0
    assert v.clauses == () and v.tier == "A"
    v = _short(ref=100.0, invalid=105.0, zones=z(1.49))  # 恰 <1.5 ⇒ 中档
    assert v.tradable and v.size_mult == pytest.approx(0.75)
    assert v.clauses == ("room_mid",)
    v = _short(ref=100.0, invalid=105.0, zones=z(1.0))   # 恰 1.0 ⇒ 中档(恰界)
    assert v.tradable and v.size_mult == pytest.approx(0.75)
    assert v.clauses == ("room_mid",)
    v = _short(ref=100.0, invalid=105.0, zones=z(0.999))  # 恰 <1.0 ⇒ 低档
    assert v.tradable and v.size_mult == pytest.approx(0.5)
    assert v.clauses == ("room_lo",)


def test_v19_tier_pos_edge_and_inclusive():
    z_full = (Zone(lo=80.0, hi=84.0, role=ZoneRole.SUPPORT,
                   state=ZoneState.ACTIVE, touches=2),)
    # 空 pos 恰 0.35(inclusive 过):ref = 80 + 0.35×(110−80) = 90.5
    v = _short(ref=90.5, invalid=94.0, zones=z_full)
    assert v.tradable and "pos_edge" not in v.clauses
    # 恰下一点点(<0.35)⇒ ×0.5
    v = _short(ref=90.4, invalid=94.0, zones=z_full)
    assert v.size_mult == pytest.approx(0.5) and v.clauses == ("pos_edge",)
    # pos 不可判(缺 LOW pivot)⇒ 按失位 ×0.5(谨慎口径)
    hi_only = (Pivot(price=110.0, forming_index=0, known_index=1,
                     kind=PivotKind.HIGH),)
    v = _short(pivots=hi_only)
    assert v.pos is None and v.size_mult == pytest.approx(0.5)
    assert v.clauses == ("pos_edge",)
    # 多对照:pos 恰 0.65(inclusive 过)
    v = evaluate_tiered(TieredParams(), direction=+1, entry_ref=99.5,
                        invalid=95.0, atr=0.8, zones=(
                            Zone(lo=104.0, hi=106.0, role=ZoneRole.RESISTANCE,
                                 state=ZoneState.ACTIVE, touches=2),),
                        pivots=PIVOTS, regime=Regime.TREND_UP,
                        zone_touches=None, rngq=None, pmd=None,
                        mic_path=False, fade=False)
    assert v.tradable and "pos_edge" not in v.clauses


def test_v19_tier_hard_bans_f1_and_target_missing():
    # F1 恰界:dist = k_is·ATR(0.8×0.8,同一浮点构造)⇒ 过(≥);恰下 ⇒ 拒
    thr = 0.8 * 0.8
    v = _short(ref=100.0, invalid=100.0 + thr, atr=0.8)
    assert v.tradable and v.rejects == (), "F1 恰界(同浮点构造)必须过"
    v = _short(ref=100.0, invalid=100.0 + thr - 1e-9, atr=0.8)
    assert not v.tradable and v.rejects == ("f1",)
    # 非保护性失效(dist ≤ 0)⇒ F1 拒
    v = _short(ref=100.0, invalid=100.0, atr=0.8)
    assert not v.tradable and v.rejects == ("f1",)
    # 结构性 target 不存在 ⇒ 拒(原"空间无界过"按协议反转)
    v = _short(zones=())
    assert not v.tradable and v.rejects == ("target_missing",)
    # 两禁同场逐项计数(F1 序先)
    v = _short(invalid=96.6, zones=())
    assert not v.tradable and v.rejects == ("f1", "target_missing")


# ---------------------------------------------------------------------------
# 3. 条款:R3/R1/R2 软着陆恰界 + 多条款相乘
# ---------------------------------------------------------------------------


def test_v19_tier_transition_fade_enumeration():
    v = _short(regime=Regime.TRANSITION)           # TRANSITION 内 fade ×0.5
    assert v.size_mult == pytest.approx(0.5)
    assert v.clauses == ("transition_fade",)
    v = _short(regime=Regime.TRANSITION, fade=False)   # 非 fade 豁免
    assert v.size_mult == 1.0 and v.clauses == ()
    for rg in (Regime.TREND_UP, Regime.TREND_DOWN, Regime.RANGE):
        v = _short(regime=rg)
        assert v.clauses == (), f"fade {rg} 不扣该条"


def test_v19_tier_rngq_big_cake_mic_exemption():
    v = _short(rngq=0.8)                           # 恰 0.8 受理(不扣)
    assert v.clauses == ()
    v = _short(rngq=0.8000000000000001)            # 超 0.8 ⇒ ×0.75
    assert v.size_mult == pytest.approx(0.75) and v.clauses == ("rngq_big",)
    v = _short(rngq=0.95, mic_path=True)           # mic-only 处豁免
    assert v.clauses == ()
    v = _short(rngq=None)                          # ctx 不可判 ⇒ 不扣(fail-open)
    assert v.clauses == ()


def test_v19_tier_pmd_boundary_and_composition():
    v = _short(pmd=0.003)                          # d=−1 ⇒ d·pmd=−0.003 恰界
    assert v.size_mult == pytest.approx(0.5) and v.clauses == ("pmd_adverse",)
    v = _short(pmd=0.0029999999)                   # 恰上 ⇒ 不扣
    assert v.clauses == ()
    v = _short(pmd=None)                           # 不可判 ⇒ 不扣
    assert v.clauses == ()
    # 二连:TRANSITION fade ×0.5 · rngq ×0.75 = 0.375
    v = _short(regime=Regime.TRANSITION, rngq=0.9)
    assert v.size_mult == pytest.approx(0.5 * 0.75)
    assert v.clauses == ("transition_fade", "rngq_big")
    # 三连:pos × room × transition = 0.5×0.5×0.5 = 0.125
    z_lo = (Zone(lo=70.0, hi=96.5 - 0.5 * 4.2, role=ZoneRole.SUPPORT,
                 state=ZoneState.ACTIVE, touches=2),)
    v = _short(zones=z_lo, regime=Regime.TRANSITION,
               pivots=(Pivot(price=110.0, forming_index=0, known_index=1,
                             kind=PivotKind.HIGH),))
    assert v.size_mult == pytest.approx(0.5 ** 3)
    assert v.clauses == ("pos_edge", "room_lo", "transition_fade")


# ---------------------------------------------------------------------------
# 4. 路由级:no_invalid/no_zone fail-closed + 记账 reason 后缀 + 结构锚重建
# ---------------------------------------------------------------------------


def test_v19_router_fail_closed_and_reason_tag():
    bar = _bar(T0 + 3 * 2 * H)
    # no_invalid:attrs invalid 缺失 ⇒ 拒(fail-closed 沿链)
    ev_noinv = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    ev_noinv.attrs.pop("invalid")
    r = V19Router(_vp19(TieredParams()))
    r.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ds = r.on_bar("TEST", bar, 3, _view(zones=(SUPPORT_FULL,)), ATR,
                  [ev_noinv], position_open=False, entry_resting=False)
    assert ds == []
    assert r.counters["v19_tier_reject_no_invalid"] == 1
    # 过单:reason 带 "|v19tier:" 后缀、meta 溯源、结构锚(market 默认不改单)
    ev = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    r2 = V19Router(_vp19(TieredParams()))
    r2.set_ctx18({"rngq": 0.9, "pmd": -0.005, "mic": False})
    ds2 = r2.on_bar("TEST", bar, 3,
                    _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN),
                    ATR, [ev], position_open=False, entry_resting=False)
    assert len(ds2) == 1
    d2 = ds2[0]
    assert "|v19tier:" in d2.intent.reason
    assert d2.meta["size_mult"] == pytest.approx(0.75)
    assert d2.meta["v19_clauses"] == ["rngq_big"]
    assert d2.hyp.invalid == 100.7                       # 结构锚沿链
    assert d2.hyp.target_1 == 90.0                       # fade 收割目标
    assert d2.hyp.entry == 96.5                          # market 默认不改单
    assert d2.kind == "market"
    assert r2.counters["v19_clause_rngq_big"] == 1
    assert r2.counters["v19_tier_b"] == 1
    # maker 臂:fade 前缘改单(计划限价 = zone 接近侧缘 99.0)
    ev3 = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    r3 = V19Router(_vp19(TieredParams(fade_front_edge=True)))
    r3.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ds3 = r3.on_bar("TEST", bar, 3,
                    _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN),
                    ATR, [ev3], position_open=False, entry_resting=False)
    assert len(ds3) == 1 and ds3[0].kind == "limit"
    assert ds3[0].limit_price == 99.0
    assert ds3[0].meta["limit_from"] == "v19_fade_front_edge"
    # maker 臂 fade 缺 attrs zone ⇒ no_zone fail-closed
    ev4 = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=T0 + 3 * 2 * H, known_index=3,
                      attrs={"ref": 100.5, "invalid": 100.7})
    r4 = V19Router(_vp19(TieredParams(fade_front_edge=True)))
    ds4 = r4.on_bar("TEST", bar, 3, _view(zones=(SUPPORT_FULL,)), ATR,
                    [ev4], position_open=False, entry_resting=False)
    assert ds4 == []
    assert r4.counters["v19_tier_reject_no_zone"] == 1


def test_v19_router_tiered_none_is_v6_identical():
    r6 = MarketRouter(RouterParams(whitelist=WL_ALL))
    r19 = V19Router(_vp19(None))
    bar = _bar(T0 + 3 * 2 * H)
    evs = [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3),
           MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 3 * 2 * H, known_index=3,
                       attrs={"ref": 95.0, "invalid": 94.0})]
    vw = _view()
    d6 = r6.on_bar("TEST", bar, 3, vw, ATR, evs,
                   position_open=False, entry_resting=False)
    d19 = r19.on_bar("TEST", bar, 3, vw, ATR, evs,
                     position_open=False, entry_resting=False)
    assert json.dumps(d6, default=lambda o: vars(o), sort_keys=True) \
        == json.dumps(d19, default=lambda o: vars(o), sort_keys=True)
    assert not any(k.startswith("v19_") for k in r19.counters)


# ---------------------------------------------------------------------------
# 5. 夭折处置:半窗恰界 / 连续逆根 / 先触发先算 / off parity
# ---------------------------------------------------------------------------


def _pm19(early=True) -> PositionManager:
    return PositionManager(time_stop=False, harvest_family=("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"),
                           time_cap_bars=48, cap_mfe_min=0.5,
                           early_exit=early)


def _fav(i):                                       # 空顺根(c<o)
    return (i * 1000, 99.4, 99.6, 98.9, 99.0, 1.0)


def _adv(i):                                       # 空全逆(c>o)
    return (i * 1000, 99.0, 99.4, 98.8, 99.4, 1.0)


def _doji(i):                                      # 恰 0 收平(复位)
    return (i * 1000, 99.4, 99.6, 98.9, 99.4, 1.0)


def _lowbar(i, low):                               # 顺根 + 指定下影(MFE 控制)
    return (i * 1000, 99.4, 99.6, low, 99.0, 1.0)


def test_v19_early_half_window_boundary_pm():
    # A. 23 根未窗不许出;第 24 根仍 <0.3R ⇒ 出(半窗恰界 inclusive 于 bars_held)
    pm = _pm19(True)
    pos = pm.open("X", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)  # risk=5, 0.3R=1.5
    for i in range(1, 24):
        assert pm.on_bar("X", _lowbar(i, 98.8), ATR) == [], \
            f"bars_held={i} 半窗未到不许出"
    assert pos.bars_held == 23
    assert pos.mfe_r == pytest.approx(1.2 / 5)     # 0.24 < 0.3R
    ints = pm.on_bar("X", _lowbar(24, 98.8), ATR)
    assert len(ints) == 1
    assert ints[0].reason.startswith("v16|early_half_mfe|")
    assert ints[0].qty_frac == pytest.approx(1.0)
    assert "X" not in pm.positions
    # B. 恰 0.3R inclusive 持有:第 24 根首次达到 0.3R ⇒ 不出,其后 MFE 不降
    pm_b = _pm19(True)
    p2 = pm_b.open("Y", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)
    for i in range(1, 24):
        pm_b.on_bar("Y", _lowbar(i, 98.8), ATR)
    assert pm_b.on_bar("Y", _lowbar(24, 98.5), ATR) == []   # mfe 恰 0.3R
    assert p2.mfe_r == pytest.approx(0.3)
    assert pm_b.on_bar("Y", _lowbar(25, 98.8), ATR) == []   # 恰界之下不逐
    # C. 半窗到达时 MFE 已 ≥0.3R ⇒ 不出
    pm_c = _pm19(True)
    pm_c.open("Z", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)
    for i in range(1, 24):
        pm_c.on_bar("Z", _lowbar(i, 98.2), ATR)    # mfe=0.36R
    assert pm_c.on_bar("Z", _lowbar(24, 98.2), ATR) == []


def test_v19_early_consec_adverse_reset_pm():
    pm = _pm19(True)
    pos = pm.open("X", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)
    pm.on_bar("X", _adv(1), ATR)
    assert pos.consec_adverse == 1
    pm.on_bar("X", _adv(2), ATR)
    assert pos.consec_adverse == 2
    pm.on_bar("X", _doji(3), ATR)                  # 恰 0 收平复位
    assert pos.consec_adverse == 0
    pm.on_bar("X", _adv(4), ATR)
    pm.on_bar("X", _fav(5), ATR)                   # 顺根复位
    assert pos.consec_adverse == 0
    pm.on_bar("X", _adv(6), ATR)
    pm.on_bar("X", _adv(7), ATR)
    ints = pm.on_bar("X", _adv(8), ATR)            # 连续第 3 根 ⇒ 出
    assert len(ints) == 1
    assert ints[0].reason.startswith("v16|early_consec|")
    assert "X" not in pm.positions


def test_v19_early_samebar_both_trigger_half_wins():
    """先触发先算:第 24 根同触 半窗(MFE<0.3R)与 3 连逆 ⇒ 半窗优先。"""
    pm = _pm19(True)
    pos = pm.open("X", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)
    for i in range(1, 22):                         # bars 1..21 顺(MFE 浅)
        pm.on_bar("X", _lowbar(i, 98.8), ATR)
    pm.on_bar("X", _adv(22), ATR)                  # consec=1
    pm.on_bar("X", _adv(23), ATR)                  # consec=2(bars_held=23)
    assert pos.consec_adverse == 2
    ints = pm.on_bar("X", _adv(24), ATR)           # 同触:半窗 & 3 连逆
    assert len(ints) == 1
    assert ints[0].reason.startswith("v16|early_half_mfe|"), \
        "同 bar 同触发 ⇒ 半窗先于逆根(内序固定)"


def test_v19_early_exit_off_parity():
    pm = _pm19(False)
    pos = pm.open("X", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)
    for i in range(1, 30):                         # 半窗过 + 全程连逆
        ints = pm.on_bar("X", _adv(i), ATR)
        assert ints == [], f"early_exit=False 时夭折块整段不生效({i})"
    assert pos.consec_adverse == 0                   # 连计数都不展开
    pm2 = _pm19(True)
    assert pm2._early_half_bars == 24                # 半窗 = k_cap/2
    with pytest.raises(ValueError):
        PositionManager(time_stop=False, time_cap_bars=None, early_exit=True)


# ---------------------------------------------------------------------------
# 6. 战役级:size_mult 上量 / 夭折真出桶标签 / parity / 确定性
# ---------------------------------------------------------------------------


def test_v19_campaign_size_mult_flows_to_qty():
    ev = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    vw_full = _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN)
    f_full = _frames_2h({3: [ev]}, {}, view=vw_full,
                        cog_bars=_cogs_big_first())
    z_mid = (Zone(lo=70.0, hi=96.5 - 1.25 * 4.2, role=ZoneRole.SUPPORT,
                  state=ZoneState.ACTIVE, touches=2),)     # room≈1.25 中档
    vw_mid = _view(zones=z_mid, regime=Regime.TREND_DOWN)
    f_mid = _frames_2h({3: [ev]}, {}, view=vw_mid,
                       cog_bars=_cogs_big_first())
    r_full = _run19(f_full)
    r_mid = _run19(f_mid)
    o_full = [r for r in r_full.ledger if r["kind"] == "OPEN"]
    o_mid = [r for r in r_mid.ledger if r["kind"] == "OPEN"]
    assert len(o_full) == 1 and len(o_mid) == 1
    assert o_full[0]["meta"]["size_mult"] == 1.0
    assert o_full[0]["meta"]["v19_tier"] == "A"
    assert o_mid[0]["meta"]["size_mult"] == pytest.approx(0.75)
    assert o_mid[0]["meta"]["v19_clauses"] == ["room_mid"]
    assert 1.7 < o_full[0]["qty"] < 1.9            # 满档(risk_frac×1.0)
    assert o_mid[0]["qty"] == pytest.approx(o_full[0]["qty"] * 0.75, abs=0.02)
    assert "|v19tier:" in o_full[0]["reason"]
    assert r_full.counters.get("v19_tier_a", 0) == 1
    assert r_mid.counters.get("v19_clause_room_mid", 0) == 1
    assert r_mid.counters.get("v19_tier_b", 0) == 1
    # 硬禁真实发生在战役层:zones 空 ⇒ target_missing ⇒ 无 OPEN
    f_none = _frames_2h({3: [ev]}, {}, view=_view(zones=()))
    r_none = _run19(f_none)
    assert not [r for r in r_none.ledger if r["kind"] == "OPEN"]
    assert r_none.counters.get("v19_tier_reject_target_missing", 0) == 1


def test_v19_campaign_early_exit_fills_and_bucket_label():
    ev = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    vw = _view(zones=(SUPPORT_FAR,), regime=Regime.TREND_DOWN)
    f = _frames_2h({3: [ev]}, {}, n_cog=40, view=vw)
    res = _run19(f)
    exits = [r for r in res.ledger if r["kind"] == "EXIT"]
    assert len(exits) == 1
    assert exits[0]["reason"].startswith("v16|early_half_mfe|")
    trades = run_v19.augment_trades_v19(res.trades, {f.key: f}, res.ledger)
    assert len(trades) == 1
    assert trades[0]["harvest_bucket"] == "early_half_mfe"
    hb = run_v19.harvest_buckets_v19(trades)
    assert "early_half_mfe" in hb and hb["early_half_mfe"]["n_trades"] == 1
    # arm-T(early 关)同场景 ⇒ 无 early 出场(收割/时间帽兜底照旧)
    res_t = _run19(f, arm=_arm_v19(early=False))
    assert not [r for r in res_t.ledger
                if r["kind"] == "EXIT"
                and str(r["reason"]).startswith("v16|early_")]


def test_v19_campaign_parity_tiered_none_router():
    mk = lambda: _frames_2h(
        {3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)],
         5: [MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 5 * 2 * H,
                         known_index=5, attrs={"ref": 95.0, "invalid": 94.0})]},
        {21: [MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 21 * M30,
                          known_index=21,
                          attrs={"ref": 95.0, "invalid": 94.0,
                                 "mic": True, "trigger_tf": 1800})]})
    r6 = run_campaigns({campaign_key("TEST", "2h"): mk()}, **dict(KW),
                       router_params=RouterParams(whitelist=WL_ALL),
                       router_factory=MarketRouter, enable_mic=True,
                       mic_families=STOP_FAMILY)
    r19 = run_campaigns({campaign_key("TEST", "2h"): mk()}, **dict(KW),
                        router_params=_vp19(None), router_factory=V19Router,
                        enable_mic=True, mic_families=STOP_FAMILY)
    assert json.dumps(r6.ledger, sort_keys=True, default=str) \
        == json.dumps(r19.ledger, sort_keys=True, default=str)
    assert json.dumps(r6.equity_rows) == json.dumps(r19.equity_rows)
    assert not any(k.startswith("v19_") for k in r19.counters
                   if k != "campaigns")


def test_v19_determinism_double_run_tiered_early():
    mk = lambda: _frames_2h(
        {3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)]},
        {21: [MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 21 * M30,
                          known_index=21,
                          attrs={"ref": 95.0, "invalid": 94.0,
                                 "mic": True, "trigger_tf": 1800})]},
        n_cog=40)
    r1 = _run19(mk())
    r2 = _run19(mk())
    assert json.dumps(r1.ledger, sort_keys=True, default=str) \
        == json.dumps(r2.ledger, sort_keys=True, default=str)
    assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)
