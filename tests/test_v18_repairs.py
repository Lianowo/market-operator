"""v18 修复包 R1-R4(docs/v18-protocol.md;research-log #34 预注册)测试 ——
恰界/枚举/三段兑现/错峰/确定性/parity 钉死。

钉死:
- config/v18.json parent=v17 硬对账 + v18_params 冻结阈值 + arms 单件开关;
- rngq 边界恰 0.8(恰界受理、超界拒、窗 48 截断、确定性);
- pmd 边界恰 0.3%(d·pmd 恰 −0.003 拒、恰上一点点过、方向对称);
- R1:续涨 bar 化身抑制(mic-only;错峰:bar 不再撤 mic/resting 挂单)、
  fade rngq>0.8 拒(恰 0.8 过);
- R3:fade 的 regime 枚举(TRANSITION 拒;TREND_UP/TREND_DOWN/RANGE 受理);
- R4:三段兑现份额(各 1/6 仓、缘收剩)、错峰段序(逐段一次/同 bar 同批)、
  invalid 不动(无 trail 无保本)、campaign 层摊薄均价与 target_partial 桶;
- parity:V18Router(v18=None) ≡ V16Router 逐位账本;
- 确定性:同构场景双跑账本逐位一致;ctx18 注入(mic 路径 pmd 取已收认知
  三根、rngq 取 trigger 帧自身窗)。
"""

from __future__ import annotations

import json
import pathlib
import sys
import types

import pandas as pd
import pytest

from market_operator import campaign as camp
from market_operator.backtest_v6 import SymbolCognition
from market_operator.broker import ContractSpec
from market_operator.campaign import (
    ArmConfig,
    STOP_FAMILY,
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.events import BREAKOUT_ACCEPTED, LEVEL_REJECTED, MarketEvent
from market_operator.harvest import HarvestParams, V16Router
from market_operator.hypothesis import StructureView
from market_operator.position import (
    PositionManager,
    R4_LEVEL_1,
    R4_LEVEL_2,
    R4_PARTIAL_FRAC,
)
from market_operator.repairs import (
    PMD_MIN,
    PMD_WINDOW,
    RNGQ_MAX,
    RNGQ_WINDOW,
    RepairParams,
    V18Params,
    V18Router,
    prior_moves_delta,
    range_quantile,
)
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
from market_operator.trigger import MicCognition, TRIGGER_TF_MAP

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_v18  # noqa: E402

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

WL_ALL = frozenset((eid, d) for eid in ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED",
                                        "STRUCTURE_BROKEN", "FLIP_RETESTED", "EDGE_RECLAIMED")
                   for d in (-1, 1))


def _view(regime: Regime = Regime.TRANSITION) -> StructureView:
    return StructureView(regime=regime, zones=(), atr=ATR, pivots=PIVOTS)


def _vp18(repair: RepairParams = None) -> V18Params:
    kw = {f: getattr(V6Params(), f) for f in V6Params.__dataclass_fields__}
    kw["whitelist"] = WL_ALL
    return V18Params(**kw, v16=HarvestParams(), v18=repair)


def _arm_cfg(ladder3: bool = False) -> ArmConfig:
    return ArmConfig(fill_model="exchange_maker", stop_entry=True,
                     stop_wait_tf=12, k_bm=0.1,
                     c2_family=("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"),
                     harvest_family=("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"),
                     time_cap_bars=48, cap_mfe_min=0.5,
                     harvest_ladder3=ladder3)


def _fade_e3(*, ts, known_index, mic=False):
    zone = Zone(lo=99.0, hi=100.5, role=ZoneRole.RESISTANCE,
                state=ZoneState.ACTIVE, touches=3)
    attrs = {"zone": zone, "ref": 100.5, "invalid": 100.7}
    if mic:
        attrs.update({"mic": True, "trigger_tf": 1800})
    return MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=ts, known_index=known_index,
                       attrs=attrs)


def _cont_e1(*, ts, known_index, mic=False):
    attrs = {"ref": 95.0, "invalid": 94.0}
    if mic:
        attrs.update({"mic": True, "trigger_tf": 1800})
    return MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=ts, known_index=known_index,
                       attrs=attrs)


def _bar(label, close=96.5, low=96.3, high=96.7, open_=96.5):
    return (label, open_, high, low, close, 1.0)


# ---------------------------------------------------------------------------
# 合成战役帧(2h 认知 × 1h 执行 × 30m trigger);覆盖价径由覆盖参数给出
# ---------------------------------------------------------------------------


def _frames_2h(events_cog: dict, events_mic: dict, *, with_mic: bool,
               cog_bars=None, exec_bars=None, view=None):
    n_exec, n_cog, n_trig = 48, 24, 96
    trig = [_bar(T0 + i * M30) for i in range(n_trig)]
    exec_rows = (exec_bars if exec_bars is not None
                 else [_bar(T0 + i * H) for i in range(n_exec)])
    df_exec = pd.DataFrame(exec_rows,
                           columns=["ts", "open", "high", "low", "close",
                                    "volume"])
    cogs = (cog_bars if cog_bars is not None
            else [_bar(T0 + i * 2 * H) for i in range(n_cog)])
    vw = view if view is not None else _view()
    cog = SymbolCognition(symbol="TEST", bars=cogs, atrs=[ATR] * len(cogs),
                          events_by=dict(events_cog), pivots_by={},
                          views={i: vw for i in events_cog})
    mic = None
    if with_mic:
        mic = MicCognition(
            symbol="TEST", obs_tf_seconds=7200, trigger_tf_seconds=1800,
            scale=4, bars=trig, cog_index_of=[i // 4 for i in range(n_trig)],
            atrs=[ATR] * n_trig, events_by=dict(events_mic),
            views={a: vw for a in events_mic},
            bar_events_by=dict(events_cog), counters={})
    return build_campaign_frame(cog, df_exec, "2h", mic_cog=mic)


def _run(f, *, repair=None, ladder3=False, mic_families=STOP_FAMILY,
         factory=V18Router):
    return run_campaigns({campaign_key("TEST", "2h"): f}, **dict(KW),
                         router_params=_vp18(repair), router_factory=factory,
                         arm=_arm_cfg(ladder3), enable_mic=True,
                         mic_families=mic_families)


# ---------------------------------------------------------------------------
# 1. config/v18.json parent=v17 硬对账 + 冻结阈值 + arms 开关
# ---------------------------------------------------------------------------


def test_v18_config_parent_v17_hard_check():
    cfg = run_v18.load_config()          # 不 raise = 全部硬对账过
    v18 = json.loads(open("config/v18.json", encoding="utf-8").read())
    v17 = json.loads(open("config/v17.json", encoding="utf-8").read())
    allow = {"version", "note", "meta", "arms", "arm", "v18_params",
             "v16_params"}
    for k in set(v18) | set(v17):
        if k in allow:
            continue
        assert v18.get(k) == v17.get(k), f"parent=v17 逐键不许漂移({k})"
    p18 = {k: v for k, v in v18["v16_params"].items() if k != "note"}
    p17 = {k: v for k, v in v17["v16_params"].items() if k != "note"}
    assert p18 == p17, "v16_params 数值面必须与 v17 深等(note 为叙述)"
    assert cfg["equity"] == 1000.0
    assert cfg["tf_list"] == ["2h", "4h", "12h", "1d"] and "1h" not in cfg["tf_list"]
    assert {int(k): int(v) for k, v in
            cfg["trigger_tf_map"].items()} == TRIGGER_TF_MAP
    vp = cfg["v18_params"]
    assert vp["rngq_window"] == RNGQ_WINDOW == 48
    assert vp["rngq_max"] == RNGQ_MAX == 0.8
    assert vp["pmd_window"] == PMD_WINDOW == 3
    assert vp["pmd_min"] == PMD_MIN == 0.003
    assert vp["r4_level_1"] == R4_LEVEL_1 == 0.25
    assert vp["r4_level_2"] == R4_LEVEL_2 == 0.5
    assert vp["r4_partial_frac"] == pytest.approx(1.0 / 6.0)
    expect = {"arm-C": [], "arm-R1": ["R1"], "arm-R2": ["R2"],
              "arm-R3": ["R3"], "arm-R4": ["R4"],
              "arm-full": ["R1", "R2", "R3", "R4"]}
    for a, reps in expect.items():
        assert cfg["arms"][a]["repairs"] == reps
        assert cfg["arms"][a]["mic_family"] == ["BREAKOUT_ACCEPTED", "STRUCTURE_BROKEN", "FLIP_RETESTED"]
    hp, factory, arm, mic_f, repair = run_v18.build_arm(cfg, "arm-R4")
    assert arm.harvest_ladder3 and repair is not None and not repair.r1
    hp, factory, arm_c, mic_f, repair_c = run_v18.build_arm(cfg, "arm-C")
    assert not arm_c.harvest_ladder3 and repair_c is None
    assert mic_f == STOP_FAMILY


# ---------------------------------------------------------------------------
# 2. rngq 纯函数:恰 0.8 / 超界 / 窗 48 截断 / 确定性
# ---------------------------------------------------------------------------


def test_v18_rngq_pure_boundaries():
    # 5 根前史:4 根波幅 ≤ 事件波幅 ⇒ rngq = 0.8(恰界受理语义由路由侧承担)
    prev = [(k * 100, 0.0, 1.0, 0.0, 1.0, 1.0) for k in range(5)]  # 波幅 1.0
    prev[4] = (400, 0.0, 2.0, 0.0, 2.0, 1.0)                       # 波幅 2.0
    bars = prev + [(500, 0.0, 1.5, 0.5, 1.5, 1.0)]                 # 事件波幅 1.0
    assert range_quantile(bars, 5, 48) == pytest.approx(0.8)       # 4/5
    bars2 = prev + [(500, 0.0, 2.0, 0.5, 2.0, 1.0)]                # 事件波幅 1.5
    assert range_quantile(bars2, 5, 48) == pytest.approx(0.8)      # 4/5(压前四)
    bars3 = prev + [(500, 0.0, 2.5, 0.5, 2.5, 1.0)]                # 事件波幅 2.0
    assert range_quantile(bars3, 5, 48) == pytest.approx(1.0)      # 5/5(与 2.0 平 ≤ 计)
    # 窗 48 截断:60 根前史,仅最近 48 根入窗
    hist = [(k * 100, 0.0, 1.0, 0.0, 1.0, 1.0) for k in range(60)]
    for k in range(12):                                            # 最旧 12 根波幅 5.0
        hist[k] = (k * 100, 0.0, 5.0, 0.0, 5.0, 1.0)
    bars4 = hist + [(6000, 0.0, 1.5, 0.0, 1.5, 1.0)]               # 事件波幅 1.5
    assert range_quantile(bars4, 60, 48) == pytest.approx(1.0)     # 48 窗内全 ≤1.5
    q_all = sum(1 for b in hist if b[2] - b[3] <= 1.5) / 60
    assert q_all == pytest.approx(48 / 60)                          # 全史口径(非实现)
    # 边界/确定性
    assert range_quantile(bars4, 0, 48) is None
    assert range_quantile(bars4, 1, 48) == 1.0                     # 前史 1 根波幅 5.0
    assert range_quantile(bars4, 60, 48) == range_quantile(bars4, 60, 48)


# ---------------------------------------------------------------------------
# 3. pmd 纯函数:净方向幅/整窗要求/确定性(恰界拒绝语义见 #5 路由级)
# ---------------------------------------------------------------------------


def test_v18_pmd_pure_values():
    bars = [(0, 100.0, 101.0, 99.0, 102.0, 1.0),      # +2.0%
            (1, 102.0, 103.0, 100.0, 101.0, 1.0),     # −0.980392…%
            (2, 101.0, 103.0, 100.5, 103.0, 1.0),     # +1.980198…%
            (3, 103.0, 104.0, 102.0, 104.0, 1.0)]     # 事件 bar(不入 pmd)
    expect = (102 - 100) / 100 + (101 - 102) / 102 + (103 - 101) / 101
    assert prior_moves_delta(bars, 3, 3) == pytest.approx(expect)
    assert prior_moves_delta(bars, 2, 3) is None       # 不足整窗 ⇒ None(不可判)
    assert prior_moves_delta(bars, 0, 3) is None
    zbars = [(0, 128.0, 129.0, 127.0, 126.0, 1.0),    # 恰 −1/64(2 进制精确)
             (1, 128.0, 129.0, 127.0, 126.0, 1.0),
             (2, 128.0, 129.0, 127.0, 126.0, 1.0),
             (3, 100.0, 101.0, 99.0, 100.0, 1.0)]
    assert prior_moves_delta(zbars, 3, 3) == pytest.approx(-3 / 64)
    assert prior_moves_delta(zbars, 3, 3) == prior_moves_delta(zbars, 3, 3)


# ---------------------------------------------------------------------------
# 4. R1 fade rngq 恰 0.8 受理 / 超 0.8 拒(路由级,条款生效计数)
# ---------------------------------------------------------------------------


def test_v18_r1_fade_rngq_boundary_router():
    ev = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    bar = _bar(T0 + 3 * 2 * H)

    def eval_with(rngq):
        r = V18Router(_vp18(RepairParams(r1=True)))
        r.set_ctx18({"rngq": rngq, "pmd": None})
        return r.on_bar("TEST", bar, 3, _view(), ATR, [ev],
                        position_open=False, entry_resting=False)

    ds = eval_with(0.8)                              # 恰 0.8 ⇒ 受理
    assert len(ds) == 1 and ds[0].kind == "limit"
    assert ds[0].meta["rngq"] == 0.8
    ds = eval_with(0.8000000000000001)               # 超 0.8 ⇒ 拒
    assert ds == []
    ds = eval_with(1.0)
    assert ds == []
    r = V18Router(_vp18(RepairParams(r1=True)))
    r.set_ctx18({"rngq": 0.81, "pmd": None})
    r.on_bar("TEST", bar, 3, _view(), ATR, [ev],
             position_open=False, entry_resting=False)
    assert r.counters["v18_r1_fade_rngq_evaluated"] == 1
    assert r.counters["v18_r1_fade_rngq_reject"] == 1
    # r1 关 ⇒ 同 rngq 不拒(基线逐位 V16)
    r0 = V18Router(_vp18(None))
    assert len(r0.on_bar("TEST", bar, 3, _view(), ATR, [ev],
                         position_open=False, entry_resting=False)) == 1
    assert "v18_r1_fade_rngq_evaluated" not in r0.counters


# ---------------------------------------------------------------------------
# 5. R2 pmd 恰 −0.003 拒(恰界)/ 方向对称 / 计数(路由级,假设合法性核)
# ---------------------------------------------------------------------------


def test_v18_r2_pmd_boundary_router():
    ev_long = _cont_e1(ts=T0 + 3 * 2 * H, known_index=3)
    ev_short = MarketEvent(id=BREAKOUT_ACCEPTED, direction=-1, ts=T0 + 3 * 2 * H,
                           known_index=3, attrs={"ref": 98.0, "invalid": 99.5})
    bar = _bar(T0 + 3 * 2 * H)

    def eval_with(ev, pmd, repair=None):
        r = V18Router(_vp18(repair or RepairParams(r2=True)))
        r.set_ctx18({"rngq": None, "pmd": pmd})
        return r, r.on_bar("TEST", bar, 3, _view(), ATR, [ev],
                           position_open=False, entry_resting=False)

    r, ds = eval_with(ev_long, -PMD_MIN)             # 恰界:反向幅恰 0.3% ⇒ 拒
    assert ds == []
    assert r.counters["v18_r2_pmd_evaluated"] == 1
    assert r.counters["v18_r2_pmd_reject"] == 1
    _, ds = eval_with(ev_long, -PMD_MIN + 1e-9)      # 恰上一点点 ⇒ 受理
    assert len(ds) == 1 and ds[0].meta["pmd"] == pytest.approx(-PMD_MIN + 1e-9)
    _, ds = eval_with(ev_long, -0.010)               # 深反向 ⇒ 拒
    assert ds == []
    _, ds = eval_with(ev_long, +0.010)               # 顺向 ⇒ 受理
    assert len(ds) == 1
    _, ds = eval_with(ev_short, +PMD_MIN)            # 空单反向对称 ⇒ 拒
    assert ds == []
    _, ds = eval_with(ev_short, -0.010)              # 空单顺向 ⇒ 受理
    assert len(ds) == 1
    _, ds = eval_with(ev_long, None)                 # 不可判(None) ⇒ 不拒
    assert len(ds) == 1
    r0 = V18Router(_vp18(None))                      # r2 关 ⇒ 不拒
    assert len(r0.on_bar("TEST", bar, 3, _view(), ATR, [ev_long],
                         position_open=False, entry_resting=False)) == 1


# ---------------------------------------------------------------------------
# 6. R3 fade regime 枚举:TRANSITION 拒,三态受理(候选条款)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("regime,allowed", [
    (Regime.TRANSITION, False),
    (Regime.TREND_UP, True),
    (Regime.TREND_DOWN, True),
    (Regime.RANGE, True),
])
def test_v18_r3_regime_enumeration(regime, allowed):
    r = V18Router(_vp18(RepairParams(r3=True)))
    r.set_ctx18({"rngq": None, "pmd": None})
    ev = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    ds = r.on_bar("TEST", _bar(T0 + 3 * 2 * H), 3, _view(regime), ATR, [ev],
                  position_open=False, entry_resting=False)
    assert (len(ds) == 1) is allowed
    assert r.counters["v18_r3_fade_evaluated"] == 1
    rej = r.counters.get("v18_r3_fade_transition_reject", 0)
    assert rej == (0 if allowed else 1)
    # 非 fade 家族在 TRANSITION 不受 R3 约束
    r2 = V18Router(_vp18(RepairParams(r3=True)))
    ds2 = r2.on_bar("TEST", _bar(T0 + 3 * 2 * H), 3,
                    _view(Regime.TRANSITION), ATR,
                    [_cont_e1(ts=T0 + 3 * 2 * H, known_index=3)],
                    position_open=False, entry_resting=False)
    assert len(ds2) == 1 and "v18_r3_fade_evaluated" not in r2.counters


# ---------------------------------------------------------------------------
# 7. R1 续涨 mic-only:bar 化身抑制(错峰:不撤挂单/不入 episode),mic 照常
# ---------------------------------------------------------------------------


def test_v18_r1_continuation_mic_only_stagger():
    # (a) bar 化身 BREAKOUT_ACCEPTED:R1 ⇒ 抑制(无 stop 预埋;错峰语义)
    f = _frames_2h({5: [_cont_e1(ts=T0 + 5 * 2 * H, known_index=5)]}, {},
                   with_mic=True)
    base = _run(f, repair=None)
    assert base.counters.get("stop_entry_armed", 0) == 1
    assert base.counters.get("v18_r1_cont_bar_suppressed", 0) == 0
    r1 = _run(f, repair=RepairParams(r1=True))
    assert r1.counters.get("stop_entry_armed", 0) == 0
    assert r1.counters.get("v18_r1_cont_bar_suppressed", 0) == 1
    assert not [row for row in r1.ledger if row["kind"] == "OPEN"]

    # (b) mic 化身 BREAKOUT_ACCEPTED:R1 ⇒ 照常(C3 stop-entry 预埋;mic-only 的"only"侧)
    fmic = _frames_2h({}, {21: [_cont_e1(ts=T0 + 21 * M30, known_index=21,
                                         mic=True)]}, with_mic=True)
    r1m = _run(fmic, repair=RepairParams(r1=True))
    assert r1m.counters.get("stop_entry_armed", 0) == 1
    assert r1m.counters.get("v18_r1_cont_bar_suppressed", 0) == 0
    assert [row for row in r1m.ledger if row["kind"] == "OPEN"], \
        "mic 续涨应正常预埋/成交(stop 价)"

    # (c) 错峰:resting fade maker 不被 bar 化身 BREAKOUT_ACCEPTED 撤单(bar 化身完全惰性)
    # (认知帧波幅差异化:fade 事件 bar rngq = 2/3 ≤ 0.8 ⇒ R1b 不拒)
    cogs = []
    for k in range(24):
        rng = (0.2, 0.5, 0.3, 0.4, 0.35)[k % 5]
        cogs.append((T0 + k * 2 * H, 96.5, 96.5 + rng / 2, 96.5 - rng / 2,
                     96.5, 1.0))
    fb = _frames_2h({3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)],
                     5: [_cont_e1(ts=T0 + 5 * 2 * H, known_index=5)]},
                    {}, with_mic=True, cog_bars=cogs)
    base_b = _run(fb, repair=None)
    r1_b = _run(fb, repair=RepairParams(r1=True))
    assert base_b.counters.get("e1_priority_cancels", 0) == 1, \
        "基线:bar BREAKOUT_ACCEPTED 优先级撤掉 resting fade maker(v17 既定)"
    assert r1_b.counters.get("e1_priority_cancels", 0) == 0, \
        "R1:bar 化身 BREAKOUT_ACCEPTED 入路由前被抑制 ⇒ 不产生撤单副作用"
    assert r1_b.counters.get("maker_entry_expired", 0) == 1, \
        "R1:fade maker 挂单自然到期(错峰保留,不被大炮尾巴撤单)"


# ---------------------------------------------------------------------------
# 8. R4 三段收割错峰段序:逐段一次、各 1/6 仓、invalid 不动(无 trail 无保本)
# ---------------------------------------------------------------------------


def _fade_pm(ladder3: bool = True) -> PositionManager:
    return PositionManager(harvest_family=("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"),
                           time_cap_bars=48, cap_mfe_min=0.5,
                           harvest_ladder3=ladder3)


def test_v18_r4_ladder_stagger_fills_pm():
    pm = _fade_pm(True)
    pos = pm.open("X", -1, 99.0, 100.7, 96.0, "LEVEL_REJECTED", 0, 0)
    inv0 = pos.invalid
    # 段 1:low=98.2 触及 lvl25(98.25),不触 lvl50(97.5)
    ints1 = pm.on_bar("X", (1000, 99.0, 99.6, 98.2, 98.8, 1.0), ATR)
    assert len(ints1) == 1
    assert ints1[0].qty_frac == pytest.approx(1.0 / 6.0)
    assert ints1[0].reason.startswith("v16|target_partial|R4|段1")
    assert pos.r4_stage == 1 and pos.size_frac == pytest.approx(5.0 / 6.0)
    assert pos.invalid == inv0 and not pos.reduced, "R4 不动 invalid/保本"
    # 级位之下再来一根:段不重发(逐段一次)
    ints1b = pm.on_bar("X", (2000, 98.8, 99.0, 98.15, 98.6, 1.0), ATR)
    assert ints1b == []
    # 段 2:low=97.4 触及 lvl50(97.5)
    ints2 = pm.on_bar("X", (3000, 98.6, 98.8, 97.4, 97.8, 1.0), ATR)
    assert len(ints2) == 1 and ints2[0].qty_frac == pytest.approx(1.0 / 6.0)
    assert ints2[0].reason.startswith("v16|target_partial|R4|段2")
    assert pos.r4_stage == 2 and pos.size_frac == pytest.approx(4.0 / 6.0)
    # 无段触发 bar(96.4 > 96.0)
    assert pm.on_bar("X", (4000, 97.8, 98.0, 96.4, 96.8, 1.0), ATR) == []
    # 缘:low=95.9 ≤ 96.0 ⇒ 收剩余 2/3
    ints4 = pm.on_bar("X", (5000, 96.8, 97.0, 95.9, 96.1, 1.0), ATR)
    assert len(ints4) == 1
    assert ints4[0].qty_frac == pytest.approx(2.0 / 3.0)
    assert ints4[0].reason.startswith("v16|target|R4|")
    assert "X" not in pm.positions
    assert not any("§8.3" in line for line in pos.log), "收割模式无 trail"


# ---------------------------------------------------------------------------
# 9. R4 错峰语义(同 bar 穿透):两段同批 + 缘收,份额恰 1/6+1/6+2/3
# ---------------------------------------------------------------------------


def test_v18_r4_ladder_samebar_batch_stagger():
    pm = _fade_pm(True)
    pos = pm.open("X", -1, 99.0, 100.7, 96.0, "LEVEL_REJECTED", 0, 0)
    ints = pm.on_bar("X", (1000, 99.0, 99.2, 95.5, 96.5, 1.0), ATR)
    kinds = [i.kind.value for i in ints]
    assert kinds == ["REDUCE", "REDUCE", "EXIT"]
    qtys = [i.qty_frac for i in ints]
    assert qtys[0] == pytest.approx(1.0 / 6.0)
    assert qtys[1] == pytest.approx(1.0 / 6.0)
    assert qtys[2] == pytest.approx(2.0 / 3.0)
    assert sum(qtys) == pytest.approx(1.0)
    assert ints[0].reason.find("段1") > 0 and ints[1].reason.find("段2") > 0
    assert "X" not in pm.positions
    # 多向对照:entry 90 invalid 88 target 96;lvl25=91.5 lvl50=93.0
    pl = _fade_pm(True)
    pos_l = pl.open("Y", +1, 90.0, 88.0, 96.0, "LEVEL_REJECTED", 0, 0)
    ints_l = pl.on_bar("Y", (1000, 90.0, 91.6, 89.8, 91.2, 1.0), ATR)
    assert [i.qty_frac for i in ints_l] == [pytest.approx(1.0 / 6.0)]
    assert pos_l.r4_stage == 1
    ints_l2 = pl.on_bar("Y", (2000, 91.2, 96.5, 90.5, 96.1, 1.0), ATR)
    assert [i.kind.value for i in ints_l2] == ["REDUCE", "EXIT"]
    assert ints_l2[-1].qty_frac == pytest.approx(2.0 / 3.0)
    # 退化(目标方向不齐)⇒ 退回 v16 单段收割
    pd_ = _fade_pm(True)
    pd_.open("Z", -1, 99.0, 100.7, 100.5, "LEVEL_REJECTED", 0, 0)
    ints_d = pd_.on_bar("Z", (1000, 99.0, 99.2, 96.0, 97.0, 1.0), ATR)
    assert ints_d and ints_d[0].reason.startswith("v16|target|对面结构缘收割")
    assert ints_d[0].qty_frac == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 10. R4 campaign:段落市价成交份额/摊薄均价入账/target_partial 桶标注
# ---------------------------------------------------------------------------


def test_v18_r4_campaign_partials_avg_price_bucket():
    support = Zone(lo=94.0, hi=96.0, role=ZoneRole.SUPPORT,
                   state=ZoneState.ACTIVE, touches=2)
    vw = StructureView(regime=Regime.TRANSITION, zones=(support,), atr=ATR,
                       pivots=PIVOTS)
    cogs = [_bar(T0 + i * 2 * H) for i in range(24)]
    cogs[9] = _bar(T0 + 9 * 2 * H, low=95.9)        # 缘触发认知 bar
    execs = [_bar(T0 + i * H) for i in range(48)]
    execs[8] = (T0 + 8 * H, 96.5, 99.2, 96.3, 96.5, 1.0)   # maker 填充触发
    # 决策 bar 后的次一执行细 bar 开盘撮合(taker 买单 = open×(1+slip)):
    execs[10] = (T0 + 10 * H, 96.4, 96.7, 96.3, 96.5, 1.0)  # 段落市价成交开盘
    execs[20] = (T0 + 20 * H, 95.9, 96.1, 95.7, 95.9, 1.0)  # 缘收市价成交开盘
    f = _frames_2h({3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)]}, {},
                   with_mic=True, cog_bars=cogs, exec_bars=execs, view=vw)
    res = _run(f, repair=RepairParams(), ladder3=True)
    assert res.counters.get("v18_r4_partial_banked", 0) == 2
    assert res.counters.get("v18_r4_edge_collect", 0) == 1
    opens = [row for row in res.ledger
             if row["kind"] == "LIMIT_FILL" and row["meta"].get("role") != "reduce"]
    assert len(opens) == 1
    o = opens[0]
    assert o["price"] == 99.0 and o["qty"] == pytest.approx(3.3)
    red = [row for row in res.ledger if row["kind"] == "REDUCE"]
    exits = [row for row in res.ledger if row["kind"] == "EXIT"]
    assert len(red) == 2 and len(exits) == 1
    # 各段落袋 1/6 仓(以 OPEN 名义为基准),缘收剩余
    assert [row["qty"] for row in red] == [pytest.approx(0.55),
                                           pytest.approx(0.55)]
    assert exits[0]["qty"] == pytest.approx(3.3 - 2 * 0.55)
    assert exits[0]["reason"].startswith("v16|target|R4|")
    assert all(row["reason"].startswith("v16|target_partial|") for row in red)
    # 摊薄均价入账:段落(96.4×(1+slip))×2 + 缘收(95.9×(1+slip)),整笔
    # exit_price = 加权均价(taker 买单滑点口径,次一执行细 bar 开盘)
    p_part = 96.4 * (1 + 0.0002)
    p_edge = 95.9 * (1 + 0.0002)
    trades = run_v18.augment_trades_v18(res.trades, {f.key: f}, res.ledger)
    assert len(trades) == 1
    t = trades[0]
    expect_avg = (0.55 * p_part + 0.55 * p_part + 2.2 * p_edge) / 3.3
    assert t["exit_price"] == pytest.approx(expect_avg)
    assert [row["price"] for row in red] == [pytest.approx(p_part),
                                             pytest.approx(p_part)]
    assert exits[0]["price"] == pytest.approx(p_edge)
    expect_gross = 0.55 * (99 - p_part) * 2 + 2.2 * (99 - p_edge)
    assert t["gross_pnl"] == pytest.approx(expect_gross)
    # 收割桶标注:整笔归 target_partial;桶表含 target_partial
    assert t["harvest_bucket"] == "target_partial"
    hb = run_v18.harvest_buckets_v18(trades)
    assert hb["target_partial"]["n_trades"] == 1
    assert hb["target_partial"]["net_pnl"] == pytest.approx(t["net_pnl"])
    # 段落走市价:无 reduce maker 限价成交
    assert res.counters.get("reduce_limit_fills", 0) == 0


# ---------------------------------------------------------------------------
# 11. parity:V18Router(v18=None) ≡ V16Router(arm 在场,逐位账本)
# ---------------------------------------------------------------------------


def test_v18_engine_parity_v18_none_router():
    mk = lambda: _frames_2h(
        {3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)]},
        {13: [_fade_e3(ts=T0 + 13 * M30, known_index=13, mic=True)],
         21: [_cont_e1(ts=T0 + 21 * M30, known_index=21, mic=True)]},
        with_mic=True)
    kw = dict(KW)
    r16 = run_campaigns({campaign_key("TEST", "2h"): mk()}, **kw,
                        router_params=_vp18(None), router_factory=V16Router,
                        arm=_arm_cfg(), enable_mic=True,
                        mic_families=STOP_FAMILY)
    r18 = run_campaigns({campaign_key("TEST", "2h"): mk()}, **dict(KW),
                        router_params=_vp18(None), router_factory=V18Router,
                        arm=_arm_cfg(), enable_mic=True,
                        mic_families=STOP_FAMILY)
    assert json.dumps(r16.ledger, sort_keys=True, default=str) \
        == json.dumps(r18.ledger, sort_keys=True, default=str)
    assert json.dumps(r16.equity_rows) == json.dumps(r18.equity_rows)
    for k in ("stop_entry_armed", "maker_entry_armed"):
        assert r16.counters.get(k, 0) == r18.counters.get(k, 0)
    assert not any(k.startswith("v18_") for k in r18.counters
                   if k != "campaigns")


# ---------------------------------------------------------------------------
# 12. 确定性 + ctx18 注入因果(mic 路径 pmd 取已收认知三根、rngq trigger 窗)
# ---------------------------------------------------------------------------


def test_v18_determinism_double_run_repairs():
    mk = lambda: _frames_2h(
        {3: [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)],
         5: [_cont_e1(ts=T0 + 5 * 2 * H, known_index=5)]},
        {21: [_cont_e1(ts=T0 + 21 * M30, known_index=21, mic=True)]},
        with_mic=True)
    repair = RepairParams(r1=True, r2=True, r3=True)
    r1 = _run(mk(), repair=repair, ladder3=True)
    r2 = _run(mk(), repair=repair, ladder3=True)
    assert json.dumps(r1.ledger, sort_keys=True, default=str) \
        == json.dumps(r2.ledger, sort_keys=True, default=str)
    assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)


def test_v18_ctx18_injection_causality():
    cog = [_bar(i * 7200, open_=100.0 + i, close=101.0 + i) for i in range(10)]
    trig = [_bar(i * 1800, open_=50.0, close=50.0) for i in range(40)]
    r = V18Router(_vp18(RepairParams(r1=True, r2=True)))
    st = types.SimpleNamespace(router=r)
    camp._inject_ctx18(st, cog_bars=cog, i=5)
    assert r._ctx18["rngq"] == pytest.approx(range_quantile(cog, 5, 48))
    expect_pmd = sum((cog[j][4] - cog[j][1]) / cog[j][1]
                     for j in range(2, 5))
    assert r._ctx18["pmd"] == pytest.approx(expect_pmd)
    # mic 路径:rngq 取 trigger 帧自身窗;pmd 取最近已收三根认知 bar(恒)
    r2 = V18Router(_vp18(RepairParams(r1=True, r2=True)))
    st2 = types.SimpleNamespace(router=r2)
    camp._inject_ctx18(st2, cog_bars=cog, i=6, rngq_bars=trig, rngq_i=9)
    assert r2._ctx18["rngq"] == pytest.approx(range_quantile(trig, 9, 48))
    expect_pmd2 = sum((cog[j][4] - cog[j][1]) / cog[j][1]
                      for j in range(3, 6))
    assert r2._ctx18["pmd"] == pytest.approx(expect_pmd2)
