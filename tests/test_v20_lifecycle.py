"""v20 收尾 S1-S4(任务书 v20/lifecycle,2026-10-20 核准语义)测试 ——
S1 激活前后边界/break 类退出不受限/只紧不松与桶标签、S2 份额恰界/同批/
退化/互斥、S3 拒 TRANSITION fade、S4 LEVEL_REJECTED 走 maker 不走市价(e3_market
下线)、parity(v20=None ≡ V19Router)、确定性、arm-C v17 锚。

钉死:
- config/v20.json parent=v19 硬对账 + 臂开关矩阵 + v20_params 冻结阈值与
  position 模块常量同值 + arm-T 构建 == v19 arm-T 构建逐项同值 + arm-C 锚
  探针 == v17 arm-C 构建逐项同值;
- S1:恰 1R inclusive 上线/不足不上线;新确认顺向 pivot 上线(多=pivot
  high 新高/空=pivot low 新低 —— 脉冲向结构确认,与 §8.3 收紧消费的逆
  向 pivot 互补;同种则闸退化 no-op,已实证钉死);逆向 pivot 未上线不
  收紧(基线会收紧,S1 留存原 invalid);未上线破位/CHoCH/到目标不受
  限;上线后只紧不松;桶标签 trail_armed/trail_pending 只换扶手族,RANGE_ROTATED
  与收割模式照旧;gate 关逐位旧行为;
- S2:缘前 25% 恰触 inclusive 落袋 40%、缘收剩 60%(0.4+0.6 对账);同一
  bar 段缘同触 REDUCE 先于 EXIT;目标方向不齐退回 v16 单段;与 R4 互斥
  raise;关 ⇒ 逐位 v16 单段;
- S3:TRANSITION fade 事件层抑制(候选不存在,v19 tier 不评估);非 fade
  与非 TRANSITION 受理;关 ⇒ v19 软条照旧;
- S4:LEVEL_REJECTED kind=limit 99.0(前缘)、entry_ref 重建、route=e3_front_maker、
  无 e3_market;BREAKOUT_FAILED 不受影响;缺 zone fail-closed;关 ⇒ e3_market 照旧;
- 战役级:S4 限价真成交 + S2 段/缘计数 + 确定性双跑。
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
from market_operator.events import BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, STRUCTURE_BROKEN, MarketEvent
from market_operator.hypothesis import StructureView
from market_operator.lifecycle import (
    ROUTE_LEVEL_REJECTED_MAKER,
    LifecycleParams,
    V20Params,
    V20Router,
)
from market_operator.position import (
    S1_FAMILY,
    S1_TRAIL_ARM_MFE_R,
    S2_BANK_FRAC,
    S2_LEVEL,
    PositionManager,
)
from market_operator.routing import RouterParams, ROUTE_LEVEL_REJECTED
from market_operator.structure import (
    Params,
    Pivot,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)
from market_operator.tiered import TieredParams, V19Params, V19Router
from market_operator.trigger import MicCognition, TRIGGER_TF_MAP

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_v17  # noqa: E402
import run_v19  # noqa: E402
import run_v20  # noqa: E402

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

# 满档对照区(与 test_v19 同口径:96.5 短单 room=(96.5−90)/4.2=1.5476 ≥1.5)
SUPPORT_FULL = Zone(lo=86.0, hi=90.0, role=ZoneRole.SUPPORT,
                    state=ZoneState.ACTIVE, touches=2)
# 远阻力(BREAKOUT_ACCEPTED 多单的对面缘:让 v19 硬禁 target_missing 不触发)
RESIST_AWAY = Zone(lo=102.0, hi=104.0, role=ZoneRole.RESISTANCE,
                   state=ZoneState.ACTIVE, touches=2)


def _view(zones=(), pivots=PIVOTS, regime=Regime.TRANSITION) -> StructureView:
    return StructureView(regime=regime, zones=zones, atr=ATR, pivots=pivots)


def _vp19(tp=None) -> V19Params:
    kw = {f: getattr(RouterParams(), f) for f in RouterParams.__dataclass_fields__}
    kw["whitelist"] = WL_ALL
    return V19Params(**kw, tiered=tp)


def _vp20(lp=None, tp=None) -> V20Params:
    kw = {f: getattr(RouterParams(), f) for f in RouterParams.__dataclass_fields__}
    kw["whitelist"] = WL_ALL
    return V20Params(**kw, tiered=tp if tp is not None else TieredParams(),
                     v20=lp)


def _fade_ev(eid, *, ts, known_index, invalid=100.7, zone_lo=99.0,
             zone_hi=100.5, ref=100.5):
    zone = Zone(lo=zone_lo, hi=zone_hi, role=ZoneRole.RESISTANCE,
                state=ZoneState.ACTIVE, touches=3)
    attrs = {"zone": zone, "ref": ref, "invalid": invalid}
    return MarketEvent(id=eid, direction=-1, ts=ts, known_index=known_index,
                       attrs=attrs)


def _fade_e3(*, ts, known_index, invalid=100.7, zone_lo=99.0, zone_hi=100.5):
    return _fade_ev(LEVEL_REJECTED, ts=ts, known_index=known_index, invalid=invalid,
                    zone_lo=zone_lo, zone_hi=zone_hi)


def _bar(label, close=96.5, low=96.3, high=96.7, open_=96.5):
    return (label, open_, high, low, close, 1.0)


def _frames_2h(events_cog: dict, events_mic: dict, *, n_cog=24, view=None,
               cog_bars=None, exec_rows=None):
    n_exec, n_trig = n_cog * 2, n_cog * 4
    trig = [_bar(T0 + i * M30) for i in range(n_trig)]
    exec_rows = (exec_rows if exec_rows is not None
                 else [_bar(T0 + i * H) for i in range(n_exec)])
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


def _arm_v20(*, s1=False, s2=False) -> ArmConfig:
    return ArmConfig(fill_model="canonical", stop_entry=False, stop_wait_tf=12,
                     k_bm=0.1, c2_family=None,
                     harvest_family=("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"),
                     time_cap_bars=48, cap_mfe_min=0.5, early_exit=False,
                     trail_arm_gate=s1, harvest_fade_2leg=s2)


def _run20(f, *, lp, arm=None):
    return run_campaigns({campaign_key("TEST", "2h"): f}, **dict(KW),
                         router_params=_vp20(lp), router_factory=V20Router,
                         arm=arm if arm is not None else _arm_v20(),
                         enable_mic=True, mic_families=STOP_FAMILY)


# ---------------------------------------------------------------------------
# 1. config/v20.json parent=v19 硬对账 + 冻结阈值 + 臂矩阵 + arm-C/v17 锚
# ---------------------------------------------------------------------------


def test_v20_config_parent_v19_hard_check():
    cfg = run_v20.load_config()          # 不 raise = 全部硬对账过
    v20 = json.loads(open("config/v20.json", encoding="utf-8").read())
    v19 = json.loads(open("config/v19.json", encoding="utf-8").read())
    allow = {"version", "note", "meta", "arms", "arm", "v20_params",
             "v19_params", "v16_params", "v18_params"}
    for k in set(v20) | set(v19):
        if k in allow:
            continue
        assert v20.get(k) == v19.get(k), f"parent=v19 逐键不许漂移({k})"
    for pk in ("v19_params", "v16_params", "v18_params"):
        a = {k: v for k, v in v20[pk].items() if k != "note"}
        b = {k: v for k, v in v19[pk].items() if k != "note"}
        assert a == b, f"{pk} 数值面必须与 v19 深等(链先验,note 为叙述)"
    assert cfg["portfolio_cap_frac"] == 0.08
    assert cfg["gates_model"] == "tier_sizing" and cfg["arm"] == "v20"
    assert cfg["tf_list"] == ["2h", "4h", "12h", "1d"]
    assert {int(k): int(v) for k, v in
            cfg["trigger_tf_map"].items()} == TRIGGER_TF_MAP
    expect = {"arm-T": (False, False, False, False),
              "arm-S1": (True, False, False, False),
              "arm-S2": (False, True, False, False),
              "arm-S4": (False, False, False, True),
              "arm-full": (True, True, True, True)}
    assert set(cfg["arms"]) == set(expect)
    for a, sw in expect.items():
        s = cfg["arms"][a]
        assert (s["s1"], s["s2"], s["s3"], s["s4"]) == sw, f"{a} 组件开关"
        assert s["tiered"] and s["early_exit"] is False and s["exec"] == "market"
        assert s["mic_family"] == list(STOP_FAMILY) and s["equity"] == 1000.0
    p = cfg["v20_params"]
    assert p["s1_mfe_r"] == S1_TRAIL_ARM_MFE_R == 1.0
    assert tuple(p["s1_family"]) == S1_FAMILY == ("BREAKOUT_ACCEPTED", "STRUCTURE_BROKEN", "FLIP_RETESTED", "TREND_RESUMED")
    assert p["s2_level"] == S2_LEVEL == 0.25
    assert p["s2_bank_frac"] == S2_BANK_FRAC == 0.4


def test_v20_arm_t_build_equals_v19_arm_t():
    """v20 arm-T 构建逐位 == v19 arm-T(下线基线;锚④的构建侧钉死)。"""
    cfg20 = run_v20.load_config()
    cfg19 = json.loads(open("config/v19.json", encoding="utf-8").read())
    rp20, fac20, arm20, mic20 = run_v20.build_arm(cfg20, "arm-T")
    tp19, fac19, arm19, mic19 = run_v19.build_arm(cfg19, "arm-T")
    assert rp20.tiered == tp19 and fac20 is fac19 is V19Router
    assert arm20 == arm19, "arm-T 的 ArmConfig 必须逐项同值(S1/S2 默认关)"
    assert mic20 == mic19 == STOP_FAMILY
    assert run_v19.build_arm_c_probe(cfg20) == run_v20.build_arm_c_probe(cfg20)


def test_v20_arm_c_probe_equals_v17_arm_c():
    """arm-C/v17 锚探针:构建逐项同值(V16Router/ArmConfig/mic 拓扑/HP)。"""
    cfg20 = json.loads(open("config/v20.json", encoding="utf-8").read())
    cfg17 = json.loads(open("config/v17.json", encoding="utf-8").read())
    hp20, fac20, arm20, mic20 = run_v20.build_arm_c_probe(cfg20)
    hp17, fac17, arm17, mic17 = run_v17.build_arm(cfg17, "arm-C")
    assert hp20 == hp17 and fac20 is fac17
    assert arm20 == arm17, "ArmConfig 必须逐项同值(v20 新键默认关)"
    assert mic20 == mic17 == STOP_FAMILY


def test_v20_build_arm_component_switches():
    cfg = run_v20.load_config()
    _, fac, arm_s1, _ = run_v20.build_arm(cfg, "arm-S1")
    assert fac is V20Router and arm_s1.trail_arm_gate and not arm_s1.harvest_fade_2leg
    _, _, arm_s2, _ = run_v20.build_arm(cfg, "arm-S2")
    assert arm_s2.harvest_fade_2leg and not arm_s2.trail_arm_gate
    rp_s4, _, arm_s4, _ = run_v20.build_arm(cfg, "arm-S4")
    assert rp_s4.v20.s4_e3_maker and not rp_s4.v20.s1_trail_arm
    assert not arm_s4.trail_arm_gate and not arm_s4.harvest_fade_2leg
    rp_f, _, arm_f, _ = run_v20.build_arm(cfg, "arm-full")
    assert rp_f.v20.s1_trail_arm and rp_f.v20.s2_fade_2leg
    assert rp_f.v20.s3_transition_fade_ban and rp_f.v20.s4_e3_maker
    assert arm_f.trail_arm_gate and arm_f.harvest_fade_2leg
    assert not arm_f.harvest_ladder3, "R4 死亡名单不接线"


# ---------------------------------------------------------------------------
# 2. S1 扶手闸:激活前后边界 / break 类退出不受限 / 只紧不松 / 桶标签 / parity
# ---------------------------------------------------------------------------


def _pm_s1(gate=True) -> PositionManager:
    return PositionManager(time_stop=False, time_cap_bars=48, cap_mfe_min=0.5,
                           trail_arm_gate=gate)


def _pvl(price):
    return [Pivot(price=price, forming_index=1, known_index=2,
                  kind=PivotKind.LOW)]


def _pvh(price):
    return [Pivot(price=price, forming_index=1, known_index=2,
                  kind=PivotKind.HIGH)]


def test_v20_s1_pending_withheld_then_arm_by_impulse_pivot():
    pm = _pm_s1(True)
    pos = pm.open("X", +1, 100.0, 95.0, None, "BREAKOUT_ACCEPTED", 0, 0)     # risk 5
    # 前:无 pivot 也无 MFE ⇒ 扶手未上线,invalid 不动
    pm.on_bar("X", (1000, 100.0, 102.0, 99.0, 101.0, 1.0), ATR)
    assert pos.invalid == 95.0 and not pos.trail_armed
    # 逆向 pivot(多仓=LOW 97):基线会收紧至 96.8,S1 未上线 ⇒ 不收紧不激活
    pm.on_bar("X", (2000, 101.0, 101.5, 98.0, 100.5, 1.0), ATR, _pvl(97.0))
    assert pos.invalid == 95.0 and not pos.trail_armed
    assert not any("§8.3" in str(l) for l in pos.log), "扶手未上线 §8.3 不裁决"
    # 新确认顺向 pivot(多仓=pivot HIGH 新高)⇒ 上线(未达 1R);本 bar 无逆向
    # pivot 故 invalid 尚不动
    pm.on_bar("X", (3000, 100.5, 103.5, 99.5, 102.0, 1.0), ATR, _pvh(103.0))
    assert pos.trail_armed and pos.invalid == 95.0
    assert any(l.startswith("v20 S1|trail_armed") for l in pos.log)
    # 上线后首个逆向 pivot ⇒ 只紧不松恢复(收紧至 97.8)
    pm.on_bar("X", (4000, 102.0, 103.0, 99.0, 101.0, 1.0), ATR, _pvl(98.0))
    assert pos.invalid == pytest.approx(98.0 - 0.1 * ATR)


def test_v20_s1_short_arms_on_low_pivot():
    """空镜像:顺向 pivot = pivot LOW 新低;pivot HIGH(逆向)不激活。"""
    pm = _pm_s1(True)
    pos = pm.open("X", -1, 100.0, 105.0, None, "BREAKOUT_ACCEPTED", 0, 0)    # risk 5
    pm.on_bar("X", (1000, 100.0, 101.0, 98.5, 99.5, 1.0), ATR, _pvh(103.0))
    assert pos.invalid == 105.0 and not pos.trail_armed        # 逆向(空)不激活
    pm.on_bar("X", (2000, 99.5, 100.0, 97.0, 98.0, 1.0), ATR, _pvl(97.0))
    assert pos.trail_armed and pos.invalid == 105.0            # 顺向(空)上线
    pm.on_bar("X", (3000, 98.0, 98.5, 97.0, 97.5, 1.0), ATR, _pvh(102.0))
    assert pos.invalid == pytest.approx(102.0 + 0.1 * ATR)     # 逆向 pivot 收紧


def test_v20_s1_arm_by_mfe_boundary_inclusive():
    # 恰 1R inclusive 上线:high=105.0 ⇒ mfe 恰 1.0R
    pm = _pm_s1(True)
    pos = pm.open("X", +1, 100.0, 95.0, None, "BREAKOUT_ACCEPTED", 0, 0)
    pm.on_bar("X", (1000, 100.0, 105.0, 99.0, 103.0, 1.0), ATR)
    assert pos.mfe_r == pytest.approx(1.0)
    assert pos.trail_armed, "MFE 恰 1R inclusive 必须上线"
    # 恰不足 1R 不上线
    pm2 = _pm_s1(True)
    pos2 = pm2.open("Y", +1, 100.0, 95.0, None, "BREAKOUT_ACCEPTED", 0, 0)
    pm2.on_bar("Y", (1000, 100.0, 104.9, 99.0, 103.0, 1.0), ATR)
    assert pos2.mfe_r == pytest.approx(0.98)
    assert not pos2.trail_armed
    # 上线后经 pivot 收紧;一经上线不回落
    pm.on_bar("X", (2000, 103.0, 104.0, 98.0, 101.0, 1.0), ATR, _pvl(97.5))
    assert pos.trail_armed
    assert pos.invalid == pytest.approx(97.5 - 0.1 * ATR)


def test_v20_s1_break_chocho_target_unrestricted_pending():
    # 破位(§8.1)不受限:扶手未上线,收盘破原始 invalid ⇒ 出,trail_pending 标签
    pm = _pm_s1(True)
    pos = pm.open("X", +1, 100.0, 95.0, None, "BREAKOUT_ACCEPTED", 0, 0)
    ints = pm.on_bar("X", (1000, 96.0, 97.0, 94.5, 94.9, 1.0), ATR)
    assert len(ints) == 1
    assert ints[0].reason.startswith("v16|trail_pending|")
    assert not pos.trail_armed
    # CHoCH(§8.5)不受限:反向 STRUCTURE_BROKEN ⇒ 出(chocho 桶标签照旧)
    pm2 = _pm_s1(True)
    pm2.open("Y", +1, 100.0, 95.0, None, "BREAKOUT_ACCEPTED", 0, 0)
    ev = MarketEvent(id=STRUCTURE_BROKEN, direction=-1, ts=1, known_index=1,
                     attrs={"ref": 101.0, "invalid": 102.0})
    ints2 = pm2.on_bar("Y", (1000, 99.0, 100.0, 98.0, 99.5, 1.0), ATR,
                       (), (ev,))
    assert len(ints2) == 1 and ints2[0].reason.startswith("v16|choch|")
    # 到目标(§8.2)不受限:扶手未上线,target 触及 ⇒ 减仓 50% 照发
    # (触及即 MFE 0.9R < 1R 的构造 ⇒ 条件 (a)/(b) 均不满足,扶手不上线)
    pm3 = _pm_s1(True)
    pos3 = pm3.open("Z", +1, 100.0, 95.0, 104.0, "BREAKOUT_ACCEPTED", 0, 0)
    ints3 = pm3.on_bar("Z", (1000, 102.0, 104.5, 101.0, 103.5, 1.0), ATR)
    assert len(ints3) == 1 and "§8.2|target_1 hit reduce 50%" in ints3[0].reason
    assert not pos3.trail_armed                      # §8.2 保本不属 trail 扶手


def test_v20_s1_armed_only_tighten_and_label():
    pm = _pm_s1(True)
    pos = pm.open("X", +1, 100.0, 95.0, None, "BREAKOUT_ACCEPTED", 0, 0)
    pm.on_bar("X", (1000, 100.0, 105.0, 99.0, 103.0, 1.0), ATR)   # 恰 1R 上线
    pm.on_bar("X", (2000, 103.0, 104.0, 98.0, 101.0, 1.0), ATR, _pvl(97.5))
    inv_tight = pos.invalid
    assert inv_tight == pytest.approx(97.3)
    # 只紧不松:更低的 pivot 候选不回落
    pm.on_bar("X", (3000, 101.0, 102.0, 96.0, 99.0, 1.0), ATR, _pvl(95.5))
    assert pos.invalid == inv_tight
    # trail 收紧后的破位 ⇒ trail_armed 标签
    iv = pos.invalid
    ints = pm.on_bar("X", (4000, 98.0, 98.5, 96.0, iv - 0.01, 1.0), ATR)
    assert len(ints) == 1
    assert ints[0].reason.startswith("v16|trail_armed|")


def test_v20_s1_non_family_and_gate_off_parity():
    # RANGE_ROTATED(非扶手族、非收割):gate 开也不受闸 ⇒ 首个 pivot 即收紧,旧标签照旧
    pm = _pm_s1(True)
    pos = pm.open("X", +1, 100.0, 95.0, None, "RANGE_ROTATED", 0, 0)
    pm.on_bar("X", (1000, 100.0, 101.0, 98.5, 100.0, 1.0), ATR, _pvl(97.0))
    assert pos.invalid == pytest.approx(96.8) and not pos.trail_armed
    ints = pm.on_bar("X", (2000, 97.0, 97.5, 95.0, 96.7, 1.0), ATR)
    assert ints[0].reason.startswith("v16|trail|")              # 旧 trail 桶
    # TREND_RESUMED ∈ 扶手族(族表钉死)
    assert "TREND_RESUMED" in S1_FAMILY and "RANGE_ROTATED" not in S1_FAMILY
    # gate 关 ⇒ 逐位旧行为(BREAKOUT_ACCEPTED 首 pivot 即收紧,无 S1 注记,旧标签)
    pm2 = _pm_s1(False)
    pos2 = pm2.open("Y", +1, 100.0, 95.0, None, "BREAKOUT_ACCEPTED", 0, 0)
    pm2.on_bar("Y", (1000, 100.0, 101.0, 98.5, 100.0, 1.0), ATR, _pvl(97.0))
    assert pos2.invalid == pytest.approx(96.8) and not pos2.trail_armed
    assert not any("S1" in str(l) for l in pos2.log)
    ints2 = pm2.on_bar("Y", (2000, 97.0, 97.5, 95.0, 96.7, 1.0), ATR)
    assert ints2[0].reason.startswith("v16|trail|")
    assert "trail_pending" not in ints2[0].reason


# ---------------------------------------------------------------------------
# 3. S2 双格收割:份额恰界 / 同批 / 退化 / 互斥 / off parity
# ---------------------------------------------------------------------------


def _pm_s2(on=True, ladder3=False) -> PositionManager:
    return PositionManager(time_stop=False, harvest_family=("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"),
                           time_cap_bars=48, cap_mfe_min=0.5,
                           harvest_ladder3=ladder3, harvest_fade_2leg=on)


def test_v20_s2_two_leg_shares_boundary_and_labels():
    pm = _pm_s2(True)
    pos = pm.open("X", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)   # level=97.5
    # 恰不上(low > level)⇒ 无动作
    assert pm.on_bar("X", (1000, 100.0, 100.2, 97.500001, 99.8, 1.0),
                     ATR) == []
    assert not pos.s2_banked
    # 恰触 97.5(inclusive)⇒ 落袋 40%
    ints = pm.on_bar("X", (2000, 99.8, 100.0, 97.5, 98.5, 1.0), ATR)
    assert len(ints) == 1
    assert ints[0].kind.name == "REDUCE"
    assert ints[0].qty_frac == pytest.approx(0.4)
    assert ints[0].reason.startswith("v16|target_partial|S2|")
    assert pos.size_frac == pytest.approx(0.6) and pos.s2_banked
    assert pos.invalid == 105.0                              # 保本/trail 不动
    # 到缘 ⇒ 收净剩 60%
    ints2 = pm.on_bar("X", (3000, 98.5, 99.0, 89.9, 91.0, 1.0), ATR)
    assert len(ints2) == 1 and ints2[0].kind.name == "EXIT"
    assert ints2[0].qty_frac == pytest.approx(0.6)
    assert ints2[0].reason.startswith("v16|target|S2|")
    assert "X" not in pm.positions


def test_v20_s2_same_bar_partial_then_edge_batched():
    pm = _pm_s2(True)
    pos = pm.open("X", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)
    ints = pm.on_bar("X", (1000, 100.0, 100.2, 89.0, 91.0, 1.0), ATR)
    assert [i.kind.name for i in ints] == ["REDUCE", "EXIT"]
    assert ints[0].qty_frac == pytest.approx(0.4)
    assert ints[1].qty_frac == pytest.approx(0.6)
    assert 0.4 + 0.6 == pytest.approx(1.0)                   # 账本对账
    assert "X" not in pm.positions


def test_v20_s2_degenerate_off_parity_and_mutex():
    # 目标方向不齐 ⇒ 退回 v16 单段收割(无 S2 段标签)
    pm = _pm_s2(True)
    pm.open("X", -1, 100.0, 105.0, 101.0, "LEVEL_REJECTED", 0, 0)        # 方向不齐
    ints = pm.on_bar("X", (1000, 100.0, 100.2, 100.5, 100.6, 1.0), ATR)
    assert len(ints) == 1 and ints[0].kind.name == "EXIT"
    assert ints[0].reason.startswith("v16|target|")
    assert "S2" not in ints[0].reason
    # S2 关 ⇒ 逐位 v16 单段:level 无段,缘一次收净
    pm2 = _pm_s2(False)
    pos2 = pm2.open("Y", -1, 100.0, 105.0, 90.0, "LEVEL_REJECTED", 0, 0)
    assert pm2.on_bar("Y", (1000, 100.0, 100.2, 97.5, 98.5, 1.0), ATR) == []
    assert not pos2.s2_banked
    ints2 = pm2.on_bar("Y", (2000, 98.5, 99.0, 89.0, 91.0, 1.0), ATR)
    assert len(ints2) == 1 and ints2[0].qty_frac == pytest.approx(1.0)
    assert ints2[0].reason.startswith("v16|target|")
    assert "S2" not in ints2[0].reason and "R4" not in ints2[0].reason
    # 与 R4 互斥 ⇒ raise(死亡名单不回锅)
    with pytest.raises(ValueError):
        _pm_s2(True, ladder3=True)


# ---------------------------------------------------------------------------
# 4. S3 TRANSITION fade 禁(路由)
# ---------------------------------------------------------------------------


def _bar_i(i):
    return _bar(T0 + i * 2 * H)


def test_v20_s3_transition_fade_suppressed_event_layer():
    # S3 开 + TRANSITION:fade 候选不存在(事件层抑制,v19 tier 不评估)
    r = V20Router(_vp20(LifecycleParams(s3_transition_fade_ban=True)))
    r.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ev = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3)
    ds = r.on_bar("TEST", _bar_i(3), 3,
                  _view(zones=(SUPPORT_FULL,), regime=Regime.TRANSITION),
                  ATR, [ev], position_open=False, entry_resting=False)
    assert ds == []
    assert r.counters["v20_s3_fade_transition_suppressed"] == 1
    assert "v19_tier_evaluated" not in r.counters, "S3=候选不存在,tier 不评估"
    # 非 fade(BREAKOUT_ACCEPTED)在 TRANSITION 照受理(对缘在场 ⇒ 硬禁 target_missing 不触)
    r1 = V20Router(_vp20(LifecycleParams(s3_transition_fade_ban=True)))
    r1.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ev1 = MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 3 * 2 * H, known_index=3,
                      attrs={"ref": 95.0, "invalid": 94.0})
    ds1 = r1.on_bar("TEST", _bar_i(3), 3,
                    _view(zones=(SUPPORT_FULL, RESIST_AWAY),
                          regime=Regime.TRANSITION),
                    ATR, [ev1], position_open=False, entry_resting=False)
    assert len(ds1) == 1 and "v20_s3_fade_transition_suppressed" not in r1.counters
    # fade 在非 TRANSITION 三态照受理(S3 开)
    for rg in (Regime.TREND_UP, Regime.TREND_DOWN, Regime.RANGE):
        r2 = V20Router(_vp20(LifecycleParams(s3_transition_fade_ban=True)))
        r2.set_ctx18({"rngq": None, "pmd": None, "mic": False})
        ds2 = r2.on_bar("TEST", _bar_i(3), 3,
                        _view(zones=(SUPPORT_FULL,), regime=rg), ATR,
                        [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)],
                        position_open=False, entry_resting=False)
        assert len(ds2) == 1, f"fade {rg} 必须受理"
        assert "v20_s3_fade_transition_suppressed" not in r2.counters


def test_v20_s3_off_keeps_v19_soft_clause():
    r = V20Router(_vp20(LifecycleParams()))                  # S3 默认关
    r.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ds = r.on_bar("TEST", _bar_i(3), 3,
                  _view(zones=(SUPPORT_FULL,), regime=Regime.TRANSITION),
                  ATR, [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)],
                  position_open=False, entry_resting=False)
    assert len(ds) == 1
    assert ds[0].meta["v19_clauses"] == ["transition_fade"]
    assert ds[0].meta["size_mult"] == pytest.approx(0.5)
    assert "v20_s3_fade_transition_suppressed" not in r.counters


# ---------------------------------------------------------------------------
# 5. S4 LEVEL_REJECTED 归 maker(路由)
# ---------------------------------------------------------------------------


def test_v20_s4_e3_front_maker_route_retired_e2_unaffected():
    r = V20Router(_vp20(LifecycleParams(s4_e3_maker=True)))
    r.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ds = r.on_bar("TEST", _bar_i(3), 3,
                  _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN),
                  ATR, [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)],
                  position_open=False, entry_resting=False)
    assert len(ds) == 1
    d = ds[0]
    assert d.kind == "limit" and d.limit_price == 99.0       # 前缘(zone.lo)
    assert d.meta["limit_from"] == "v20_e3_front_edge"
    assert d.route == ROUTE_LEVEL_REJECTED_MAKER == "e3_front_maker"
    assert all(x.route != ROUTE_LEVEL_REJECTED for x in ds), "e3_market 路由必须下线"
    assert d.hyp.entry == 99.0                                # entry_ref 重建
    assert d.hyp.target_1 == 90.0
    # 同路由 BREAKOUT_FAILED 不受影响(仅 LEVEL_REJECTED 归 maker)
    r2 = V20Router(_vp20(LifecycleParams(s4_e3_maker=True)))
    r2.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ds2 = r2.on_bar("TEST", _bar_i(3), 3,
                    _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN),
                    ATR, [_fade_ev(BREAKOUT_FAILED, ts=T0 + 3 * 2 * H, known_index=3)],
                    position_open=False, entry_resting=False)
    assert len(ds2) == 1 and ds2[0].kind == "market"
    assert ds2[0].route == "breakout_failed_market"
    # S4 关 ⇒ e3_market 照旧(v19 基线)
    r3 = V20Router(_vp20(LifecycleParams()))
    r3.set_ctx18({"rngq": None, "pmd": None, "mic": False})
    ds3 = r3.on_bar("TEST", _bar_i(3), 3,
                    _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN),
                    ATR, [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3)],
                    position_open=False, entry_resting=False)
    assert len(ds3) == 1 and ds3[0].kind == "market"
    assert ds3[0].route == "level_rejected_market"


def test_v20_s4_e3_no_zone_fail_closed():
    ev = MarketEvent(id=LEVEL_REJECTED, direction=-1, ts=T0 + 3 * 2 * H, known_index=3,
                     attrs={"ref": 100.5, "invalid": 100.7})
    r = V20Router(_vp20(LifecycleParams(s4_e3_maker=True)))
    ds = r.on_bar("TEST", _bar_i(3), 3,
                  _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN),
                  ATR, [ev], position_open=False, entry_resting=False)
    assert ds == []
    assert r.counters["v19_tier_reject_no_zone"] == 1


def test_v20_router_none_parity_equals_v19():
    evs = [_fade_e3(ts=T0 + 3 * 2 * H, known_index=3),
           MarketEvent(id=BREAKOUT_ACCEPTED, direction=+1, ts=T0 + 3 * 2 * H, known_index=3,
                       attrs={"ref": 95.0, "invalid": 94.0})]
    vw = _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN)
    r19 = V19Router(_vp19(TieredParams()))
    r20 = V20Router(_vp20(None))
    for r in (r19, r20):
        r.set_ctx18({"rngq": 0.9, "pmd": -0.005, "mic": False})
    d19 = r19.on_bar("TEST", _bar_i(3), 3, vw, ATR, evs,
                     position_open=False, entry_resting=False)
    d20 = r20.on_bar("TEST", _bar_i(3), 3, vw, ATR, evs,
                     position_open=False, entry_resting=False)
    assert json.dumps(d19, default=lambda o: vars(o), sort_keys=True) \
        == json.dumps(d20, default=lambda o: vars(o), sort_keys=True)
    assert not any(k.startswith("v20_") for k in r20.counters)


# ---------------------------------------------------------------------------
# 6. 战役级:S4 限价真成交 + S2 段/缘计数 + 确定性
# ---------------------------------------------------------------------------


def _frames_s2s4():
    """LEVEL_REJECTED 短 fade @i=3:zone [96.55,100.5] 前缘 96.55;S2 level=94.9125。
    i=12 low=94.5(触段),i=13 low=89(到缘);对缘 SUPPORT_FULL hi=90。"""
    n_cog = 40
    cogs = [_bar(T0 + i * 2 * H) for i in range(n_cog)]
    cogs[12] = (T0 + 12 * 2 * H, 96.5, 96.7, 94.5, 96.0, 1.0)
    cogs[13] = (T0 + 13 * 2 * H, 96.0, 96.2, 89.0, 95.0, 1.0)
    ev = _fade_e3(ts=T0 + 3 * 2 * H, known_index=3, zone_lo=96.55,
                  zone_hi=100.5)
    vw = _view(zones=(SUPPORT_FULL,), regime=Regime.TREND_DOWN)
    return _frames_2h({3: [ev]}, {}, n_cog=n_cog, view=vw, cog_bars=cogs)


def test_v20_campaign_s4_limit_fill_and_s2_counters():
    f = _frames_s2s4()
    lp = LifecycleParams(s1_trail_arm=True, s2_fade_2leg=True,
                         s3_transition_fade_ban=True, s4_e3_maker=True)
    res = _run20(f, lp=lp, arm=_arm_v20(s1=True, s2=True))
    # S4:LEVEL_REJECTED 走 maker 不走市价 —— 无 market OPEN,限价成交于前缘 96.55
    assert not [r for r in res.ledger if r["kind"] == "OPEN"]
    fills = [r for r in res.ledger if r["kind"] == "LIMIT_FILL"]
    assert len(fills) == 1 and fills[0]["price"] == pytest.approx(96.55)
    assert fills[0]["meta"]["limit_from"] == "v20_e3_front_edge"
    assert fills[0]["meta"]["route"] == "e3_front_maker"
    # S2:段/缘计数恰各一次
    assert res.counters.get("v20_s2_partial_banked", 0) == 1
    assert res.counters.get("v20_s2_edge_collect", 0) == 1
    assert res.counters.get("v18_r4_partial_banked", 0) == 0
    trades = run_v20.augment_trades_v20(res.trades, {f.key: f}, res.ledger)
    assert len(trades) == 1
    t = trades[0]
    assert t["limit_from"] == "v20_e3_front_edge"
    assert t["route"] == ROUTE_LEVEL_REJECTED_MAKER
    assert t["s2_partial"] and t["harvest_bucket"] == "target"
    assert t["closed"]


def test_v20_determinism_double_run_full_arm():
    lp = LifecycleParams(s1_trail_arm=True, s2_fade_2leg=True,
                         s3_transition_fade_ban=True, s4_e3_maker=True)
    r1 = _run20(_frames_s2s4(), lp=lp, arm=_arm_v20(s1=True, s2=True))
    r2 = _run20(_frames_s2s4(), lp=lp, arm=_arm_v20(s1=True, s2=True))
    assert json.dumps(r1.ledger, sort_keys=True, default=str) \
        == json.dumps(r2.ledger, sort_keys=True, default=str)
    assert json.dumps(r1.equity_rows) == json.dumps(r2.equity_rows)
