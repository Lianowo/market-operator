"""v20 收尾:S1 结构脉冲激活 trail / S2 fade 双格收割 / S3 TRANSITION fade 禁
/ S4 LEVEL_REJECTED 归 maker 统一回测(任务书 v20/lifecycle,2026-10-20 核准语义;直接
提交,不推不合)。基线 = arm-T(v19 arm-T 逐位:tier_sizing + market 默认 +
8% cap,early_exit 关)。

用法:  python scripts/run_v20.py
配置:  config/v20.json(parent=config/v19.json 逐键硬对账;v19/v16/v18_params
       数值面深等照链;portfolio_cap_frac 0.08、gates_model tier_sizing、
       tf_list/equity/费用/触发链零漂移;v20_params 冻结阈值与 position 模块
       常量硬对账)
产物:  reports/v20_arms.{json,md}
       reports/trades_v20_arm-{T,S1,S2,S4,full}__dev[_cost*].csv
       reports/trades_v20_arm-{full,<最强件>}__test[_cost*].csv

口径与纪律:
- 四锚硬断言(不等即 raise,不写产物):① FROZEN_ERA 名单探针(dev 三格,
  arm=None,enable_mic=False,equity=300,**容量按 0.03 锚探针口径**)== v6
  冻结产物逐格全等(代码零回归);② 现行 16 档名单 bar 回放 dev 三格 ==
  v13 在册 bar 臂逐格全等(engine 零回归);③ arm-C(v17 语义,V16Router +
  v16 ArmConfig,容量按 0.03 锚探针口径)dev 三格 == 在册
  reports/v17_arm-C.json dev 三格逐格全等(基线语义锚);④ v20 arm-T dev
  三格 == 在册 reports/v19.json arm-T|dev@ 三格逐格全等(下线基线锚)。
  探针容量覆盖 0.03 只用于锚与 v17 对照,产品臂一律 0.08。
- 判定锚(dev 主评 @1.5x):逐臂净/PF/n/期望/收割桶;组件裁决 =
  Δ(arm-S1/S2/S4 − arm-T)孤立;S3 无独立臂(v18 R3 已证),full − arm-T
  − Σ(三件孤立差) = S3+交互残差,不打包报喜;trail 错杀回补(信号硬匹
  配 arm-T trail 桶净负笔)与 S2 死桶回补(缘前 25% 段已落袋而未到缘笔)
  单独归因。
- 验收首页 = 事件×TF×方向 n/净/期望矩阵(旗舰 arm-full dev@1.5x;单格
  n<15 一律 INSUFFICIENT,不给叙述);其次收割桶(trail_armed/
  trail_pending 与 S2 段桶)与四件计数。
- test 一次(第 23 次评估曝光,任务书预声明,一次性):仅 arm-full 与
  dev@1.5x Δ最强件同一轮次同次跑齐 1/1.5/2x 三格;arm-C test 数字只读
  引用在册 v17 报告(第 20 次曝光产物),不重复曝光;样本量 <60 ⇒
  INSUFFICIENT_DATA;既往升级线逐条照报;不渲染任何转正叙述;此后 v20
  只许前向。
- 死亡名单不回锅:R4 三段不接线(harvest_ladder3 与 S2 互斥,同栈 raise);
  打包优质五问不复活;E 检测/费用/杠杆上限/单笔风险/容量/历史产物/
  research-log/acceptance 零改动;合约规格强制离线缓存;mic 帧复放 bar
  事件流 == prepare_cognition 逐字段(硬断言);30m 帧 manifest 哈希复核;
  账户默认参数自 config;级联声明如实。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_backtest_v6 as rb                                     # noqa: E402
import run_v16 as rv16                                           # noqa: E402
import run_v17 as rv17                                           # noqa: E402
import run_v19 as rv19                                           # noqa: E402
from market_operator import aggregate as agg                     # noqa: E402
from market_operator.backtest_v6 import compute_metrics          # noqa: E402
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.campaign import (                           # noqa: E402
    ArmConfig,
    FADE_FAMILY,
    STOP_FAMILY,
    run_campaigns,
)
from market_operator.harvest import V16Params                   # noqa: E402
from market_operator.lifecycle import (                         # noqa: E402
    ROUTE_LEVEL_REJECTED_MAKER,
    LifecycleParams,
    V20Params,
    V20Router,
)
from market_operator.position import (                          # noqa: E402
    S1_FAMILY,
    S1_TRAIL_ARM_MFE_R,
    S2_BANK_FRAC,
    S2_LEVEL,
)
from market_operator.research import block_bootstrap_ci         # noqa: E402
from market_operator.strategy_v6 import V6Params                # noqa: E402
from market_operator.structure import Params                    # noqa: E402
from market_operator.tiered import TieredParams, V19Params, V19Router  # noqa: E402
from market_operator.trigger import TRIGGER_TF_MAP              # noqa: E402

CONFIG_V20 = ROOT / "config" / "v20.json"
CONFIG_V19 = ROOT / "config" / "v19.json"
FROZEN_TURTLE = ROOT / "reports" / "backtest_v6_turtle.json"
FROZEN_V13 = ROOT / "reports" / "backtest_v13_cascade.json"
V17_REPORT_C = ROOT / "reports" / "v17_arm-C.json"
V19_REPORT = ROOT / "reports" / "v19.json"
REPORTS = ROOT / "reports"

COST_MULTS = rv16.COST_MULTS
JUDGE_COST = rv16.JUDGE_COST
MIN_TRADES_TEST = rv16.MIN_TRADES_TEST
FROZEN_ERA_WHITELIST = rv16.FROZEN_ERA_WHITELIST
METRIC_EQ = rv16.METRIC_EQ
EQUITY0 = 1000.0
PROBE_EQUITY = 300.0
CAP_PRODUCT = 0.08                       # 产品臂组合容量(照 v19 链,不动)
CAP_ANCHOR = 0.03                        # 锚探针容量(v17 在册口径,参照用)
TF_LIST = ["2h", "4h", "12h", "1d"]
ARM_IDS = ("arm-T", "arm-S1", "arm-S2", "arm-S4", "arm-full")
COMPONENT_ARMS = ("arm-S1", "arm-S2", "arm-S4")     # S3 无独立臂(v18 已证)
UPGRADE_LINE = rv17.UPGRADE_LINE
HARVEST_BUCKETS_V20 = rv19.HARVEST_BUCKETS_V19 + ("trail_armed",
                                                  "trail_pending")
MIN_CELL_N = rv19.MIN_CELL_N             # 15:单格 n<15 ⇒ INSUFFICIENT
TIER_CLAUSES = rv19.TIER_CLAUSES
EXPOSURE_NOTE = ("test 段第 23 次评估曝光(任务书 v20 预声明,一次性;仅 "
                 "arm-full 与 dev@1.5x Δ最强件同一轮次同次跑齐 1/1.5/2x 三"
                 "格;arm-C test 数字只读引用在册 reports/v17_arm-C.json(第"
                 " 20 次曝光产物),不重复曝光;此后 v20 只许前向)")


# ---------------------------------------------------------------------------
# 配置(parent=v19 逐键硬对账;v20_params/臂开关冻结复核)
# ---------------------------------------------------------------------------


def load_config() -> dict:
    v20 = json.loads(CONFIG_V20.read_text(encoding="utf-8"))
    v19 = json.loads(CONFIG_V19.read_text(encoding="utf-8"))
    allow_diff = {"version", "note", "meta", "arms", "arm", "v20_params",
                  "v19_params", "v16_params", "v18_params"}   # 参数块下方深检
    for k in set(v20) | set(v19):
        if k in allow_diff:
            continue
        if v20.get(k) != v19.get(k):
            raise RuntimeError(f"config/v20.json parent=v19 漂移 {k}: "
                               f"{v20.get(k)} != {v19.get(k)}")
    for pk in ("v19_params", "v16_params", "v18_params"):
        p20 = {k: v for k, v in v20[pk].items() if k != "note"}
        p19 = {k: v for k, v in v19[pk].items() if k != "note"}
        if p20 != p19:
            raise RuntimeError(f"config/v20.json {pk} 数值面 != config/v19.json"
                               "(链先验不许碰;note 为叙述)")
    if float(v20["portfolio_cap_frac"]) != CAP_PRODUCT:
        raise RuntimeError(f"config/v20.json portfolio_cap_frac 必须 "
                           f"{CAP_PRODUCT}(照 v19 链): {v20['portfolio_cap_frac']}")
    if v20["gates_model"] != "tier_sizing":
        raise RuntimeError("config/v20.json gates_model 必须 tier_sizing"
                           f": {v20['gates_model']}")
    if v20["arm"] != "v20":
        raise RuntimeError("config/v20.json arm 必须 v20")
    rv17.tf_of(v20["tf_list"])
    if float(v20["equity"]) != EQUITY0:
        raise RuntimeError(f"config/v20.json equity 必须 {EQUITY0}: {v20['equity']}")
    tf_map = {int(k): int(v) for k, v in v20["trigger_tf_map"].items()}
    if tf_map != TRIGGER_TF_MAP:
        raise RuntimeError("config/v20.json trigger_tf_map != 冻结级联映射")
    if set(v20["arms"]) != set(ARM_IDS):
        raise RuntimeError(f"config/v20.json arms 必须恰为 {ARM_IDS}")
    expect_switch = {"arm-T": (False, False, False, False),
                     "arm-S1": (True, False, False, False),
                     "arm-S2": (False, True, False, False),
                     "arm-S4": (False, False, False, True),
                     "arm-full": (True, True, True, True)}
    for a, sw in expect_switch.items():
        spec = v20["arms"][a]
        if not (spec["tiered"] and spec["early_exit"] is False
                and spec["exec"] == "market"):
            raise RuntimeError(f"config/v20.json {a} 基线开关漂移: {spec}")
        if (bool(spec["s1"]), bool(spec["s2"]), bool(spec["s3"]),
                bool(spec["s4"])) != sw:
            raise RuntimeError(f"config/v20.json {a} 组件开关漂移: {spec} != {sw}")
        if spec["mic_family"] != list(STOP_FAMILY):
            raise RuntimeError(f"config/v20.json {a} mic 拓扑必须沿 arm-C")
        if float(spec["equity"]) != EQUITY0:
            raise RuntimeError(f"config/v20.json {a} equity 必须 {EQUITY0}")
    p20 = v20["v20_params"]
    if float(p20["s1_mfe_r"]) != S1_TRAIL_ARM_MFE_R:
        raise RuntimeError(f"config/v20.json v20_params.s1_mfe_r 冻结漂移: "
                           f"{p20['s1_mfe_r']} != {S1_TRAIL_ARM_MFE_R}")
    if tuple(p20["s1_family"]) != S1_FAMILY:
        raise RuntimeError(f"config/v20.json v20_params.s1_family 冻结漂移: "
                           f"{p20['s1_family']} != {S1_FAMILY}")
    if float(p20["s2_level"]) != S2_LEVEL \
            or float(p20["s2_bank_frac"]) != S2_BANK_FRAC:
        raise RuntimeError("config/v20.json v20_params.s2 阈值冻结漂移: "
                           f"{p20['s2_level']}/{p20['s2_bank_frac']} != "
                           f"{S2_LEVEL}/{S2_BANK_FRAC}")
    if tuple(v20["v16_params"]["fade_family"]) != FADE_FAMILY \
            or tuple(v20["v16_params"]["stop_family"]) != STOP_FAMILY:
        raise RuntimeError("config/v20.json 家族表漂移(协议写死)")
    return v20


# ---------------------------------------------------------------------------
# 臂定义 + 回放驱动
# ---------------------------------------------------------------------------


def _tiered_of(raw_cfg: dict) -> TieredParams:
    """全臂同一 v19 配注先验(arm-T 基线;无 maker 臂 ⇒ fade_front_edge 恒
    False,S4 只经 lifecycle 钩对 LEVEL_REJECTED 生效)。"""
    p19 = {k: v for k, v in raw_cfg["v19_params"].items() if k != "note"}
    return TieredParams(
        **{k: p19[k] for k in ("pos_short_min", "pos_long_max", "k_room_R",
                               "room_lo", "k_is", "k_star", "mult_pos",
                               "mult_room_mid", "mult_room_lo",
                               "mult_transition_fade", "mult_rngq_big",
                               "mult_pmd_adverse")},
        fade_family=tuple(raw_cfg["v16_params"]["fade_family"]),
        fade_front_edge=False)


def _arm_base(raw_cfg: dict, arm_id: str) -> ArmConfig:
    """全臂共用持仓侧接线(v19 arm-T 逐位)+ v20 S1/S2 开关。"""
    p16 = raw_cfg["v16_params"]
    spec = raw_cfg["arms"][arm_id]
    return ArmConfig(fill_model="canonical", stop_entry=False,
                     stop_wait_tf=int(p16["stop_wait_tf"]),
                     k_bm=float(p16["k_bm"]), c2_family=None,
                     harvest_family=tuple(p16["fade_family"]),
                     time_cap_bars=int(p16["k_cap_hours"]),
                     cap_mfe_min=float(p16["cap_mfe_min"]),
                     early_exit=False,
                     trail_arm_gate=bool(spec["s1"]),
                     harvest_fade_2leg=bool(spec["s2"]))


def build_arm(raw_cfg: dict, arm_id: str) -> tuple:
    """臂 → (router_params, router_factory, ArmConfig, mic_families)。

    arm-T:V19Params + V19Router(无 v20 层 ⇒ 与 v19 arm-T 逐位,锚④对上);
    组件臂:V20Params(tiered=同基线,v20=LifecycleParams(臂开关)) +
    V20Router;S1/S2 持仓侧开关经 ArmConfig 注入;mic 拓扑 = STOP_FAMILY。
    """
    spec = raw_cfg["arms"][arm_id]
    tp = _tiered_of(raw_cfg)
    arm = _arm_base(raw_cfg, arm_id)
    kw = {f: getattr(V6Params(), f) for f in V6Params.__dataclass_fields__}
    if arm_id == "arm-T":
        rp = V19Params(**kw, tiered=tp)
        # whitelist 由 main 的 rp_base 承担(此处返回元组,main 里提升)
        return rp, V19Router, arm, STOP_FAMILY
    lp = LifecycleParams(s1_trail_arm=bool(spec["s1"]),
                         s2_fade_2leg=bool(spec["s2"]),
                         s3_transition_fade_ban=bool(spec["s3"]),
                         s4_e3_maker=bool(spec["s4"]))
    rp = V20Params(**kw, tiered=tp, v20=lp)
    return rp, V20Router, arm, STOP_FAMILY


def lift_rp(rp_base: V6Params, rp):
    """canonical V6Params 逐项提升(whitelist/k_dq 等以 config 为准)。"""
    kw = {f: getattr(rp_base, f) for f in V6Params.__dataclass_fields__}
    if isinstance(rp, V20Params):
        return V20Params(**kw, tiered=rp.tiered, v20=rp.v20)
    return V19Params(**kw, tiered=rp.tiered)


def build_arm_c_probe(raw_cfg: dict) -> tuple:
    """arm-C 锚探针(v17 arm-C 语义逐位;照 v19 同型,cap 0.03 由 main 覆盖)。"""
    return rv19.build_arm_c_probe(raw_cfg)


def run_grid(frames: dict, raw_cfg: dict, params: Params, rp, factory, arm,
             contracts: dict, *, segment: str, equity0: float,
             mic_families=None, enable_mic: bool = True,
             mults: tuple = COST_MULTS, tag: str = "") -> dict:
    broker_kw = dict(dev_frac=float(raw_cfg["dev_frac"]), equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     maker_fee=float(raw_cfg["maker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params,
                     n_fill=int(raw_cfg["n_fill"]),
                     portfolio_cap_frac=float(raw_cfg["portfolio_cap_frac"]))
    out = {}
    for mult in mults:
        t0 = time.time()
        res = run_campaigns(frames, segment=segment, cost_multiplier=mult,
                            router_params=rp, router_factory=factory, arm=arm,
                            enable_mic=enable_mic, mic_families=mic_families,
                            **broker_kw)
        if enable_mic:
            res.trades = augment_trades_v20(res.trades, frames, res.ledger)
        m = compute_metrics(res)
        out[mult] = (res, m)
        c = res.counters
        print(f"      [bt] {tag}{segment}@{mult}x"
              f"{' mic' if enable_mic else ' bar'}: net={m['net_pnl']:+.2f} "
              f"n={m['n_trades']} "
              f"pf={m['profit_factor'] and round(m['profit_factor'], 3)} "
              f"tier={c.get('v19_tier_a', 0)}/{c.get('v19_tier_b', 0)} "
              f"s3禁={c.get('v20_s3_fade_transition_suppressed', 0)} "
              f"s2段={c.get('v20_s2_partial_banked', 0)} "
              f"cap={c.get('portfolio_cap_skips', 0)} "
              f"({time.time() - t0:.0f}s)", flush=True)
    liq = sum(res.counters["liquidations"] for res, _ in out.values())
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")
    return out


# ---------------------------------------------------------------------------
# 逐笔增广(v20 = v19 增广 + S4/S2 溯源列)
# ---------------------------------------------------------------------------


def augment_trades_v20(trades: list, frames: dict, ledger: list) -> list:
    """rv19.augment_trades_v19 + v20 增量:limit_from(S4 LEVEL_REJECTED 前缘改单印记;
    e3_front_maker 路由重贴在 route 列)与 s2_partial(任一段落袋 reason
    带 "v16|target_partial|S2|";死桶回补分析用)。均溯源列,不上数值路径。
    """
    aug = rv19.augment_trades_v19(trades, frames, ledger)
    meta_by_tid: dict = {}
    for row in ledger:
        if row["trade_id"] is None:
            continue
        if row["kind"] == "OPEN" or (row["kind"] == "LIMIT_FILL"
                                     and row["meta"].get("role") != "reduce"):
            meta_by_tid.setdefault(row["trade_id"], row["meta"])
    out = []
    for t in aug:
        m = meta_by_tid.get(t["trade_id"], {})
        s2p = any(str(r).startswith("v16|target_partial|S2|")
                  for r in t.get("reasons_reduce", ()))
        out.append({**t, "limit_from": m.get("limit_from"),
                    "s2_partial": s2p})
    return out


COLUMNS_V20 = list(rv19.COLUMNS_V19) + ["limit_from", "s2_partial"]


def trades_frame_v20(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": rb._iso(t["open_decision_ts"]),
                     "open_iso": rb._iso(t["open_ts"]),
                     "close_decision_iso": rb._iso(t["close_decision_ts"]),
                     "exit_iso": rb._iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=COLUMNS_V20)


# ---------------------------------------------------------------------------
# 收割桶 / 错杀回补 / 死桶回补
# ---------------------------------------------------------------------------


def harvest_buckets_v20(trades: list) -> dict:
    """收割桶(v19 谱系 + trail_armed/trail_pending;段内开仓平仓笔)。"""
    nets: dict = {b: [] for b in HARVEST_BUCKETS_V20}
    for t in trades:
        if not t["in_segment_open"] or not t["closed"]:
            continue
        nets[t.get("harvest_bucket") or "other"].append(float(t["net_pnl"]))
    return {b: rv16._cell_stats(nets[b]) for b in HARVEST_BUCKETS_V20
            if nets[b]}


def clause_stats_v20(counters: dict) -> dict:
    base = rv19.clause_stats_v19(counters)
    g = counters.get
    base.update({
        "v20_s3_fade_transition_suppressed":
            g("v20_s3_fade_transition_suppressed", 0),
        "v20_s2_partial_banked": g("v20_s2_partial_banked", 0),
        "v20_s2_edge_collect": g("v20_s2_edge_collect", 0),
    })
    return base


def _by_signal(trades: list) -> dict:
    """信号硬匹配键:(campaign, signal_decision_ts);同 v16/v17/v19 口径。"""
    out: dict = {}
    for t in trades:
        if not t["closed"]:
            continue
        key = (t["campaign"], int(t["signal_decision_ts"])
               if t.get("signal_decision_ts") is not None else None)
        out[key] = t
    return out


def trail_kill_recovery(tr_base: list, tr_s1: list) -> dict:
    """S1 trail 错杀回补(dev@1.5x,信号硬匹配):arm-T 出场桶 = trail(防线
    已被 §8.3 收紧后的破位)且净额 < 0 的"错杀"笔,在 arm-S1 的同信号净
    额对照;recovered = Σ(S1 − T)。不匹配笔如实计数。"""
    tb = _by_signal(tr_base)
    ts = _by_signal(tr_s1)
    kills = {k: t for k, t in tb.items()
             if t.get("harvest_bucket") == "trail"
             and float(t["net_pnl"]) < 0.0}
    matched = [(k, t, ts[k]) for k, t in kills.items() if k in ts]
    net_t = sum(float(t["net_pnl"]) for _k, t, _s in matched)
    net_s = sum(float(s["net_pnl"]) for _k, _t, s in matched)
    return {
        "definition": ("arm-T 收割桶 trail ∧ net<0(trail 错杀);按 (campaign,"
                       "signal_decision_ts) 硬匹配 arm-S1 同信号;recovered ="
                       " Σ(arm-S1 净 − arm-T 净)"),
        "trail_kills_arm_T": len(kills),
        "matched": len(matched),
        "unmatched": len(kills) - len(matched),
        "net_arm_T_matched": net_t,
        "net_arm_S1_matched": net_s,
        "recovered": net_s - net_t,
        "improved_n": sum(1 for _k, t, s in matched
                          if float(s["net_pnl"]) > float(t["net_pnl"])),
    }


def s2_dead_bucket_recovery(tr_base: list, tr_s2: list) -> dict:
    """S2 死桶回补(dev@1.5x,信号硬匹配):arm-S2 缘前 25% 段已落袋
    (s2_partial)而最终**未到缘**(收割桶 != target)的笔 —— v19 单段收割
    下这些笔段落袋额不存在(死桶);按 (campaign,signal_decision_ts) 硬
    匹配 arm-T 同信号;recovered = Σ(S2 − T)。最先落地段数/缘收数另计数。
    """
    tb = _by_signal(tr_base)
    ts = _by_signal(tr_s2)
    dead = {k: t for k, t in ts.items()
            if t.get("s2_partial") and t.get("harvest_bucket") != "target"}
    matched = [(k, t, tb[k]) for k, t in dead.items() if k in tb]
    net_s = sum(float(t["net_pnl"]) for _k, t, _b in matched)
    net_b = sum(float(b["net_pnl"]) for _k, _t, b in matched)
    partial_all = [t for t in ts.values() if t.get("s2_partial")]
    return {
        "definition": ("arm-S2 内 s2_partial(缘前 25% 段已落袋 40%)且收割桶"
                       " != target(未到缘而死;v19 单段收割下段落袋不存在 ⇒"
                       " 死桶);硬匹配 arm-T 同信号;recovered = Σ(arm-S2 净 −"
                       " arm-T 净)"),
        "s2_partial_trades": len(partial_all),
        "dead_after_bank": len(dead),
        "edge_after_bank": len(partial_all) - len(dead),
        "matched": len(matched),
        "unmatched": len(dead) - len(matched),
        "net_arm_S2_matched": net_s,
        "net_arm_T_matched": net_b,
        "recovered": net_s - net_b,
        "dead_bucket_breakdown": {b: sum(1 for t in dead.values()
                                         if t.get("harvest_bucket") == b)
                                  for b in ("invalid", "time_cap", "choch",
                                            "early_half_mfe", "early_consec",
                                            "other")
                                  if any(t.get("harvest_bucket") == b
                                         for t in dead.values())},
    }


def cap_skip_row(res) -> dict:
    return rv19.cap_skip_row(res)


def _cellm(m: dict) -> dict:
    return rv19._cellm(m)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    t_start = time.time()
    for stream in (sys.stdout, sys.stderr):      # GBK 控制台:不可编码字符
        try:                                   # 以 ? 替代(报告 utf-8 不受影响)
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    raw_cfg = load_config()
    params = Params.from_json(CONFIG_V20)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v20 parent=v19:merge_max_atr 必须 2.0")
    anchor_cfg = dict(raw_cfg)
    anchor_cfg["portfolio_cap_frac"] = CAP_ANCHOR

    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    for sym in symbols:
        p30 = agg.ensure_30m([sym])[sym]
        rec = json.loads((ROOT / "data" / "manifest.json")
                         .read_text(encoding="utf-8"))["files"][f"{sym}_30m.csv"]
        assert hashlib.sha256(p30.read_bytes()).hexdigest() == rec["sha256"]
    print("[data] 30m 帧齐(manifest 哈希复核过)", flush=True)

    print("[prep] v20 战役帧构建(v17 帧装载复用;认知一次性)", flush=True)
    frames, probe_frames, prep = rv17.prepare_frames_v17(raw_cfg, params)
    rv17.assert_frames_no_1h(frames)
    for d in prep["declarations"]:
        print(f"[decl] {d}", flush=True)
    xcheck = rb.cross_checks(probe_frames, float(raw_cfg["k_is"]),
                             merge_max_atr=2.0)

    frozen_turtle = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    frozen_v13 = json.loads(FROZEN_V13.read_text(encoding="utf-8"))
    v17_c_report = json.loads(V17_REPORT_C.read_text(encoding="utf-8"))
    v19_report = json.loads(V19_REPORT.read_text(encoding="utf-8"))
    if v17_c_report["arm"] != "arm-C":
        raise RuntimeError("在册基线引用必须 v17 arm-C 报告")

    # -- 锚①/②(engine/代码零回归;探针 equity=300,cap 0.03,mic 禁用) ---------------
    print("[anchor-1] FROZEN_ERA 探针(dev 三格,arm=None,mic 禁用,cap 0.03)",
          flush=True)
    rp_probe = dc_replace(rp_base, whitelist=frozenset(FROZEN_ERA_WHITELIST))
    probe = run_grid(probe_frames, anchor_cfg, params, rp_probe, None, None,
                     contracts, segment="dev", equity0=PROBE_EQUITY,
                     enable_mic=False, tag="anchor1 ")
    parity_probe = rv16.assert_anchor("FROZEN_ERA 探针", probe,
                                      frozen_turtle["runs"], "turtle|dev")
    print("[anchor-1] 探针 3 格 == v6 冻结产物(逐格全等)", flush=True)

    print("[anchor-2] 现行 16 档名单 bar 回放 == v13 在册 bar 臂", flush=True)
    probe16 = run_grid(probe_frames, anchor_cfg, params, rp_base, None, None,
                       contracts, segment="dev", equity0=PROBE_EQUITY,
                       enable_mic=False, tag="anchor2 ")
    parity_bar16 = rv16.assert_anchor("16 档 bar 回放", probe16,
                                      frozen_v13["runs"], "bar|dev")
    print("[anchor-2] 16 档 3 格 == v13 在册 bar 臂(逐格全等)", flush=True)

    # -- 锚③:arm-C(v17 语义,cap 0.03 探针)dev 三格 == 在册 v17 arm-C ---------------
    print("[anchor-3] arm-C(v17 语义,cap 0.03)== 在册 v17 arm-C(dev 三格)",
          flush=True)
    hp_c, factory_c, arm_c, mic_c = build_arm_c_probe(raw_cfg)
    rp_c = V16Params(**{f: getattr(rp_base, f)
                        for f in V6Params.__dataclass_fields__}, v16=hp_c)
    arm_c_grid = run_grid(frames, anchor_cfg, params, rp_c, factory_c, arm_c,
                          contracts, segment="dev", equity0=EQUITY0,
                          mic_families=mic_c, enable_mic=True, tag="arm-C ")
    parity_baseline = []
    for mult in COST_MULTS:
        m = arm_c_grid[mult][1]
        fm = v17_c_report["runs"][f"arm-C|dev@{mult}x"]["metrics"]
        ok = all(m[k] == fm[k] for k in METRIC_EQ)
        parity_baseline.append({"run": f"arm-C|dev@{mult}x",
                                **{f"v20_{k}": m[k] for k in METRIC_EQ},
                                **{f"v17_{k}": fm[k] for k in METRIC_EQ},
                                "parity": ok})
        if not ok:
            raise RuntimeError(f"基线锚不一致 arm-C|dev@{mult}x: "
                               f"{parity_baseline[-1]}")
    print("[anchor-3] arm-C 3 格 == v17 在册 arm-C(逐格全等)", flush=True)

    # -- dev 主网格:五臂 equity=1000,cap 0.08,三格/臂 --------------------------------
    grids: dict = {}
    for arm_id in ARM_IDS:
        rp0, factory, arm, mic_families = build_arm(raw_cfg, arm_id)
        rp_use = lift_rp(rp_base, rp0)
        print(f"[arm] {arm_id}: {raw_cfg['arms'][arm_id]['note']}", flush=True)
        grids[arm_id] = {"dev": run_grid(
            frames, raw_cfg, params, rp_use, factory, arm, contracts,
            segment="dev", equity0=EQUITY0, mic_families=mic_families,
            enable_mic=True, tag=f"{arm_id} ")}

    # -- 锚④:v20 arm-T dev 三格 == 在册 v19 arm-T dev 三格 -----------------------------
    print("[anchor-4] v20 arm-T == 在册 v19 arm-T(dev 三格,下线基线锚)", flush=True)
    parity_arm_t = []
    for mult in COST_MULTS:
        m = grids["arm-T"]["dev"][mult][1]
        fm = v19_report["runs"][f"arm-T|dev@{mult}x"]["metrics"]
        ok = all(m[k] == fm[k] for k in METRIC_EQ)
        parity_arm_t.append({"run": f"arm-T|dev@{mult}x",
                             **{f"v20_{k}": m[k] for k in METRIC_EQ},
                             **{f"v19_{k}": fm[k] for k in METRIC_EQ},
                             "parity": ok})
        if not ok:
            raise RuntimeError(f"下线基线锚不一致 arm-T|dev@{mult}x: "
                               f"{parity_arm_t[-1]}")
    print("[anchor-4] arm-T 3 格 == v19 在册 arm-T(逐格全等)", flush=True)

    # -- 组件裁决(dev 主评 @1.5x;孤立 Δ;S3+交互残差不打包) -------------------------
    def m_of(a, seg, mult):
        return grids[a][seg][mult][1]

    def dv(a, base="arm-T"):
        ea, eb = (m_of(a, "dev", JUDGE_COST)["expectancy_usdt"],
                  m_of(base, "dev", JUDGE_COST)["expectancy_usdt"])
        return {
            "component": a, "baseline": base,
            "expectancy_usdt_dev_1_5x": ea,
            "expectancy_usdt_baseline": eb,
            "delta_expectancy_usdt": (ea - eb)
            if ea is not None and eb is not None else None,
            "delta_net_pnl": (m_of(a, "dev", JUDGE_COST)["net_pnl"]
                              - m_of(base, "dev", JUDGE_COST)["net_pnl"]),
        }

    component_verdicts = {
        "s1_isolation": {**dv("arm-S1"),
                         "reading": "S1 结构脉冲激活 trail 孤立差(arm-S1 − arm-T)"},
        "s2_isolation": {**dv("arm-S2"),
                         "reading": "S2 fade 双格收割孤立差(arm-S2 − arm-T)"},
        "s4_isolation": {**dv("arm-S4"),
                         "reading": "S4 LEVEL_REJECTED 归 maker 孤立差(arm-S4 − arm-T)"},
    }
    net_full = m_of("arm-full", "dev", JUDGE_COST)["net_pnl"]
    net_t = m_of("arm-T", "dev", JUDGE_COST)["net_pnl"]
    iso_sum = sum(component_verdicts[k]["delta_net_pnl"]
                  for k in ("s1_isolation", "s2_isolation", "s4_isolation"))
    s3_residue = (net_full - net_t) - iso_sum
    component_verdicts["s3_interaction_residue"] = {
        "component": "arm-full", "baseline": "arm-T",
        "delta_net_pnl_full_vs_T": net_full - net_t,
        "delta_net_pnl_isolated_sum_S1S2S4": iso_sum,
        "delta_net_pnl": s3_residue,
        "reading": ("S3+交互残差 = (full − arm-T) − Σ(S1/S2/S4 孤立差);S3 无独"
                    "立臂(v18 R3 已证),残差含 S3 真实贡献与四件交互,不打包喜"
                    "报(不归因给任一单件)")}

    # -- 最强件(dev@1.5x Δ净;test 曝光对象之一) --------------------------------------
    best_component = max(COMPONENT_ARMS,
                         key=lambda a: dv(a)["delta_net_pnl"])
    best_delta = dv(best_component)["delta_net_pnl"]
    test_arms = tuple(dict.fromkeys(("arm-full", best_component)))
    print(f"[select] 最强件(dev@1.5x Δ净)= {best_component} "
          f"(Δ {best_delta:+.2f});test 曝光 = {test_arms}(第 23 次,一次性)",
          flush=True)

    # -- test 一次(第 23 次曝光;arm-full + 最强件) ------------------------------------
    print(f"[test] 第 23 次曝光:{test_arms}(一次性三格;此后只许前向)", flush=True)
    for arm_id in test_arms:
        rp0, factory, arm, mic_families = build_arm(raw_cfg, arm_id)
        rp_use = lift_rp(rp_base, rp0)
        grids[arm_id]["test"] = run_grid(
            frames, raw_cfg, params, rp_use, factory, arm, contracts,
            segment="test", equity0=EQUITY0, mic_families=mic_families,
            enable_mic=True, tag=f"{arm_id} ")

    # -- 归因装配 ------------------------------------------------------------------
    def cell_attr(arm_id, seg, mult):
        res, m = grids[arm_id][seg][mult]
        mat = rv19.expectation_matrix(res.trades)
        fam_tf = rv19.family_tf_matrix(res.trades)
        return {
            "metrics": m, "counters": res.counters,
            "clause": clause_stats_v20(res.counters),
            "harvest": harvest_buckets_v20(res.trades),
            "family": rv16.family_buckets(res.trades),
            "matrix": mat, "matrix_ratio": rv19.matrix_ratio(mat),
            "family_tf": fam_tf, "family_tf_ratio": rv19.matrix_ratio(fam_tf),
            "micro_segment": rv17.mic_entry_share(res.trades),
            "cap": cap_skip_row(res),
            "giveback": rb.giveback_summary(res.trades),
            "buckets": rb.bucket_tables(res.trades),
        }

    attr: dict = {a: {} for a in ARM_IDS}
    for a in ARM_IDS:
        for mult in COST_MULTS:
            attr[a][f"dev@{mult}x"] = cell_attr(a, "dev", mult)
    for a in test_arms:
        for mult in COST_MULTS:
            attr[a][f"test@{mult}x"] = cell_attr(a, "test", mult)
    attr["arm-C"] = {f"dev@{mult}x": {
        "metrics": arm_c_grid[mult][1],
        "cap": cap_skip_row(arm_c_grid[mult][0]),
        "clause": rv16.clause_stats(arm_c_grid[mult][0].counters)}
        for mult in COST_MULTS}

    # -- S1 错杀回补 / S2 死桶回补(dev@1.5x,信号硬匹配) ------------------------------
    recovery = {
        "s1_trail_kill": trail_kill_recovery(
            grids["arm-T"]["dev"][JUDGE_COST][0].trades,
            grids["arm-S1"]["dev"][JUDGE_COST][0].trades),
        "s2_dead_bucket": s2_dead_bucket_recovery(
            grids["arm-T"]["dev"][JUDGE_COST][0].trades,
            grids["arm-S2"]["dev"][JUDGE_COST][0].trades),
        "s1_buckets_arm_S1_dev_1_5x": {
            b: attr["arm-S1"][f"dev@{JUDGE_COST}x"]["harvest"].get(b)
            for b in ("trail_armed", "trail_pending")
            if b in attr["arm-S1"][f"dev@{JUDGE_COST}x"]["harvest"]},
        "s4_route_audit": {
            "e3_market_route_trades_arm_S4": sum(
                1 for t in grids["arm-S4"]["dev"][JUDGE_COST][0].trades
                if t.get("route") == "level_rejected_market"),
            "e3_front_maker_route_trades_arm_S4": sum(
                1 for t in grids["arm-S4"]["dev"][JUDGE_COST][0].trades
                if t.get("route") == ROUTE_LEVEL_REJECTED_MAKER),
            "e3_market_route_trades_arm_full": sum(
                1 for t in grids["arm-full"]["dev"][JUDGE_COST][0].trades
                if t.get("route") == "level_rejected_market"),
            "e3_front_maker_route_trades_arm_full": sum(
                1 for t in grids["arm-full"]["dev"][JUDGE_COST][0].trades
                if t.get("route") == ROUTE_LEVEL_REJECTED_MAKER),
        },
    }

    # -- test 一次判定(升级线逐条 + CI;arm-C 引用在册) --------------------------------------
    test_once: dict = {}
    for a in test_arms:
        res1, m1 = grids[a]["test"][1.0]
        _, m15 = grids[a]["test"][1.5]
        _, m20 = grids[a]["test"][2.0]
        tr_in = [t for t in res1.trades if t["in_segment_open"]]
        ci = (block_bootstrap_ci(
            np.asarray([t["net_pnl"] for t in tr_in], dtype=np.float64),
            np.asarray([t["open_ts"] for t in tr_in], dtype=np.int64))
            if tr_in else (float("nan"), float("nan")))
        up = {
            "n_trades_test_1x": m1["n_trades"],
            "sample_size": ("SUFFICIENT(>=60)" if m1["n_trades"] >= MIN_TRADES_TEST
                            else "INSUFFICIENT_DATA"),
            "expectancy_usdt_test_1_5x": m15["expectancy_usdt"],
            "expectancy_positive_1_5x": (m15["expectancy_usdt"] is not None
                                         and m15["expectancy_usdt"] > 0.0),
            "profit_factor_test_2_0x": m20["profit_factor"],
            "pf_gt_1_2_0x": (m20["profit_factor"] is not None
                             and m20["profit_factor"] > 1.0),
        }
        up["verdict"] = (
            "过线(可谈 forward dry-run)"
            if (up["sample_size"].startswith("SUFFICIENT")
                and up["expectancy_positive_1_5x"] and up["pf_gt_1_2_0x"])
            else "不过线(Research Only 维持)")
        test_once[a] = {
            "test_1x": _cellm(m1), "test_1_5x": _cellm(m15),
            "test_2x": _cellm(m20),
            "dev_1x": _cellm(grids[a]["dev"][1.0][1]),
            "dev_1_5x": _cellm(grids[a]["dev"][1.5][1]),
            "dev_2x": _cellm(grids[a]["dev"][2.0][1]),
            "test_net_pnl_mean_ci95_1x": list(ci),
            "matrix_test_1x": attr[a]["test@1.0x"]["matrix"],
            "matrix_ratio_test_1x": attr[a]["test@1.0x"]["matrix_ratio"],
            "family_tf_ratio_test_1x": attr[a]["test@1.0x"]["family_tf_ratio"],
            "upgrade_line": up}
    baseline_test_reg = {f"test@{m}x": _cellm(
        v17_c_report["runs"][f"arm-C|test@{m}x"]["metrics"])
        for m in COST_MULTS}

    # -- 产物装配/落盘 ------------------------------------------------------------------
    REPORTS.mkdir(exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "doc": "任务书 v20 收尾 S1-S4 + 统一回测(2026-10-20 核准语义)",
            "pre_registration": "任务书预声明(直接提交 v20/lifecycle,不推不合)",
            "owner_rulings": ("核准语义(冻结,不谈判):S1 cont 家族与 TREND_RESUMED 的 trail "
                              "扶手只在 MFE≥1R 或新确认顺向 pivot 之后上线,此前只许"
                              "破位/CHoCH/到目标出,上线后只紧不松,桶标签 "
                              "trail_armed/trail_pending;S2 fade 收割双格=缘前 25%"
                              " 落袋 40% 仓、缘收剩 60%(不是 R4 三段);S3 fade 仅非 "
                              "TRANSITION regime 受理(v18 已证,无独立臂);S4 LEVEL_REJECTED 归 "
                              "maker(前缘限价语义回归,e3_market 路由下线)"),
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "parity_discipline": ("四锚硬断言:FROZEN_ERA 探针(cap 0.03)== v6 冻结"
                                  "产物逐格全等(代码零回归);16 档 bar 回放(cap "
                                  "0.03)== v13 在册 bar 臂逐格全等(engine 零回归);"
                                  "arm-C(v17 语义,cap 0.03)dev 三格 == 在册 v17 "
                                  "arm-C 逐格全等(基线语义锚);v20 arm-T dev 三格 "
                                  "== 在册 v19 arm-T 逐格全等(下线基线锚)。不等即 "
                                  "raise,报告不落地(本轮已通过)"),
            "test_eval_declaration": EXPOSURE_NOTE,
            "judge_rule": ("dev 主评 @1.5x:组件裁决 = Δ(arm-S1/S2/S4 − arm-T)孤立;"
                           "S3+交互残差 = (full − T) − Σ孤立差,不打包报喜;验收矩阵"
                           "(事件×TF×方向,n<15 ⇒ INSUFFICIENT)首页;死亡名单(R4 "
                           "层级兑现/打包五问)不回锅;trail 错杀回补与 S2 死桶回补"
                           "信号硬匹配单列"),
            "upgrade_line": UPGRADE_LINE,
            "cascade_declarations": prep["declarations"],
            "honesty_note": ("全臂 early_exit=False(v19 arm-T 基线,孤立组件差;"
                             "夭折不混入);S3 为事件层抑制 —— TRANSITION fade 候选"
                             "不存在,v19 软条款 transition_fade 自然不再命中(S3 "
                             "开臂内),如实声明;S4 LEVEL_REJECTED 走 canonical 限价主路(=FLIP_RETESTED "
                             "G3 同通道:严格穿过 maker 成交、maker 费、N_fill=12 "
                             "墙钟窗),F1/空间按计划限价计量(改单在配注评估之前,"
                             "因果序同 v19 maker 臂);e3_market 仅在 S4 开臂内下"
                             "线(arm-T/S1/S2 照旧存在);S1 恰 1R inclusive 上线,"
                             "顺向 pivot=多 pivot high 新高/空 pivot low 新低"
                             "(脉冲向结构确认,与 §8.3 收紧消费的逆向 pivot "
                             "互补;同种则闸退化 no-op,已实证钉死),上线后逆"
                             "向 pivot 起只紧不松;扶手族外(RANGE_ROTATED)与 fade 收割"
                             "模式不受 S1;组合容量决策账未乘档(照 v19 声明);"
                             "检测/费用/杠杆/单笔风险/历史产物/research-log 零改"
                             "动;不渲染任何转正叙述。"),
        },
        "config": {
            "config_file": "config/v20.json",
            "parent": "config/v19.json(逐键硬对账,run 前通过)",
            "tf_list": TF_LIST, "equity0": EQUITY0,
            "risk_frac": raw_cfg["risk_frac"],
            "leverage_cap": raw_cfg["leverage_cap"],
            "taker_fee": raw_cfg["taker_fee"],
            "maker_fee": raw_cfg["maker_fee"],
            "slippage_per_side": raw_cfg["slippage_per_side"],
            "funding_per_8h_per_side": raw_cfg["funding_per_8h_per_side"],
            "portfolio_cap_frac": raw_cfg["portfolio_cap_frac"],
            "portfolio_cap_frac_anchor_probe": CAP_ANCHOR,
            "dev_frac": dev_frac,
            "cost_mults": list(COST_MULTS), "judge_cost_mult": JUDGE_COST,
            "gates_model": raw_cfg["gates_model"],
            "trigger_tf_map": {str(k): v for k, v in TRIGGER_TF_MAP.items()},
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "v20_params": raw_cfg["v20_params"],
            "v19_params": raw_cfg["v19_params"],
            "v16_params": raw_cfg["v16_params"],
            "whitelist": [f"{i}|{'long' if d > 0 else 'short'}"
                          for i, d in sorted(whitelist)],
        },
        "contracts": {"source": spec_src,
                      "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
                      "cache_sha256": contracts_sha},
        "cross_checks_canonical_baseline": xcheck,
        "bar_stream_parity": {"description": "mic 帧复放 bar 事件流 == "
                                             "prepare_cognition 逐字段(硬断言)",
                              "n_events_by_campaign": prep["bar_parity"]},
        "parity_frozen_era_probe": parity_probe,
        "parity_bar16_vs_v13": parity_bar16,
        "parity_baseline_arm_c_vs_v17": parity_baseline,
        "parity_arm_t_vs_v19": parity_arm_t,
        "component_verdicts": component_verdicts,
        "best_component_dev_1_5x": {"arm": best_component,
                                    "delta_net_pnl": best_delta},
        "recovery": recovery,
        "cap_skip_rates": {
            "note": "组合拒绝率(照 v19 链 0.08):arm-C(0.03 锚探针)== 在册"
                    " v17 arm-C 重放 vs 产品臂(0.08);rate = skips/(skips+"
                    "open_intents)(决策时刻口径)",
            **{f"arm-C(0.03)|dev@{m}x": attr["arm-C"][f"dev@{m}x"]["cap"]
               for m in COST_MULTS},
            **{f"{a}(0.08)|dev@{m}x": attr[a][f"dev@{m}x"]["cap"]
               for a in ARM_IDS for m in (JUDGE_COST,)},
        },
        "acceptance_front": {
            "matrix_arm_full_dev_1_5x": attr["arm-full"][f"dev@{JUDGE_COST}x"]["matrix"],
            "ratio_arm_full_dev_1_5x": attr["arm-full"][f"dev@{JUDGE_COST}x"]["matrix_ratio"],
            "family_tf_ratio_arm_full_dev_1_5x":
                attr["arm-full"][f"dev@{JUDGE_COST}x"]["family_tf_ratio"],
        },
        "test_once_exposure": {
            "note": EXPOSURE_NOTE,
            "strongest_component": best_component,
            **test_once,
            "baseline_arm_c_test_in_register_v17": baseline_test_reg,
        },
        "runs": {},
    }
    for a in ARM_IDS:
        for mult in COST_MULTS:
            payload["runs"][f"{a}|dev@{mult}x"] = attr[a][f"dev@{mult}x"]
    for a in test_arms:
        for mult in COST_MULTS:
            payload["runs"][f"{a}|test@{mult}x"] = attr[a][f"test@{mult}x"]
    for mult in COST_MULTS:
        payload["runs"][f"arm-C(0.03锚探针)|dev@{mult}x"] = attr["arm-C"][f"dev@{mult}x"]

    (REPORTS / "v20_arms.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "v20_arms.md").write_text(
        render_md(payload, grids, attr, test_arms, best_component), encoding="utf-8")
    for a in ARM_IDS:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v20(grids[a]["dev"][mult][0].trades).to_csv(
                REPORTS / f"trades_v20_{a}__dev{suffix}.csv", index=False)
    for a in test_arms:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v20(grids[a]["test"][mult][0].trades).to_csv(
                REPORTS / f"trades_v20_{a}__test{suffix}.csv", index=False)
    print(f"-> reports/v20_arms.json/.md + trades_v20 CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    return rv19._f(x, nd, dash)


def _mrow(m: dict) -> str:
    return rv19._mrow(m)


def render_matrix(mat: dict, ratio: dict) -> list:
    return rv19.render_matrix(mat, ratio)


def render_md(payload: dict, grids: dict, attr: dict, test_arms: tuple,
              best_component: str) -> str:
    front = payload["acceptance_front"]
    J = JUDGE_COST
    lines = [
        "# v20 收尾:S1 扶手激活 trail / S2 fade 双格收割 / S3 TRANSITION fade 禁"
        " / S4 LEVEL_REJECTED 归 maker 统一回测报告(判定表首页=期望矩阵;dev 主评 + test "
        "一次)", "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明", "",
        f"- {payload['protocol']['owner_rulings']}",
        f"- 预声明:{payload['protocol']['pre_registration']};"
        f"**{payload['protocol']['test_eval_declaration']}**",
        f"- parity 纪律:{payload['protocol']['parity_discipline']}",
        "- 级联声明:"
        + (";".join(payload["protocol"]["cascade_declarations"])
           if payload["protocol"]["cascade_declarations"]
           else "映射全部精确命中,无声明。"),
        f"- {payload['protocol']['honesty_note']}",
        f"- 合约:{payload['contracts']['source']}(离线强制)。",
        "",
        "## 1. 判定表首页:事件 × TF × 方向 期望矩阵(旗舰 arm-full,"
        "dev@1.5x)", ""]
    lines += render_matrix(front["matrix_arm_full_dev_1_5x"],
                           front["ratio_arm_full_dev_1_5x"])

    lines += ["### 1a. 家族 × TF 辅助矩阵(arm-full,dev@1.5x;方向合并)", "",
              "| 家族 | TF | n | 净盈亏 | 期望USDT | 判定 |",
              "|---|---|---|---|---|---|"]
    fam_tf = attr["arm-full"][f"dev@{J}x"]["family_tf"]
    for fam in ("fade", "continuation", "other"):
        for tf in TF_LIST:
            c = fam_tf.get(fam, {}).get(tf)
            if c is None:
                continue
            lines.append(f"| {fam} | {tf} | {c['n']} | {_f(c['net_pnl'])} "
                         f"| {_f(c['expectancy_usdt'], 4)} | {c['status']} |")
    fr = front["family_tf_ratio_arm_full_dev_1_5x"]
    lines += ["", f"> 家族×TF 正期望格占比(EVAL): {fr['cells_positive']}/"
                  f"{fr['cells_eval']} = {_f(fr['positive_share'], 4)}"
                  f"(INSUFFICIENT {fr['cells_insufficient']}).", ""]

    lines += ["## 2. 收割桶与四件计数(旗舰 arm-full dev@1.5x;含 S1 扶手桶"
              "与 S2 段桶)", "", "| 桶 | 笔数 | 净额 | 胜率 | PF |",
              "|---|---|---|---|---|"]
    hb = attr["arm-full"][f"dev@{J}x"]["harvest"]
    for b in HARVEST_BUCKETS_V20:
        if b in hb:
            c = hb[b]
            lines.append(f"| {b} | {c['n_trades']} "
                         f"| {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
                         f"| {_f(c['profit_factor'])} |")
    fb = attr["arm-full"][f"dev@{J}x"]["family"]
    for fam in ("fade", "continuation", "other"):
        if fam in fb:
            c = fb[fam]
            lines.append(f"| 家族:{fam} | {c['n_trades']} "
                         f"| {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
                         f"| {_f(c['profit_factor'])} |")
    rec = payload["recovery"]
    lines += ["",
              f"- S4 路由审计(e3_market 下线):arm-S4 e3_market="
              f"{rec['s4_route_audit']['e3_market_route_trades_arm_S4']} / "
              f"e3_front_maker="
              f"{rec['s4_route_audit']['e3_front_maker_route_trades_arm_S4']}"
              f";arm-full e3_market="
              f"{rec['s4_route_audit']['e3_market_route_trades_arm_full']} / "
              f"e3_front_maker="
              f"{rec['s4_route_audit']['e3_front_maker_route_trades_arm_full']}",
              ""]
    for a in ("arm-S1", "arm-S2", "arm-full"):
        cl = attr[a][f"dev@{J}x"]["clause"]
        lines.append(f"- {a} 计数:配注 A/B={cl['v19_tier_a']}/"
                     f"{cl['v19_tier_b']} 硬禁 F1/tm="
                     f"{cl['v19_tier_reject_f1']}/{cl['v19_tier_reject_target_missing']} "
                     f"S3 抑制={cl['v20_s3_fade_transition_suppressed']} "
                     f"S2 段/缘={cl['v20_s2_partial_banked']}/"
                     f"{cl['v20_s2_edge_collect']} 容量跳="
                     f"{cl['portfolio_cap_skips']}")
    lines.append("")

    lines += ["## 3. 判定总览(五臂 dev 三格;arm-C 为 0.03 锚探针参照行)", "",
              "| 臂 | 格 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|---|"]
    for a in ARM_IDS:
        for mult in COST_MULTS:
            m = grids[a]["dev"][mult][1]
            lines.append(f"| {a} | dev@{mult}x | {_mrow(m)} |")
    for mult in COST_MULTS:
        m = attr["arm-C"][f"dev@{mult}x"]["metrics"]
        lines.append(f"| arm-C(0.03 探针,==v17 在册) | dev@{mult}x "
                     f"| {_mrow(m)} |")
    lines.append("")

    lines += ["## 4. 组件裁决(dev@1.5x,孤立 Δ;S3+交互残差不打包报喜)", "",
              "| 组件 | 对照基线 | Δ净期望USDT | Δ净盈亏 | 判读 |",
              "|---|---|---|---|---|"]
    for k in ("s1_isolation", "s2_isolation", "s4_isolation"):
        cv = payload["component_verdicts"][k]
        lines.append(f"| {cv['component']} | {cv['baseline']} "
                     f"| {_f(cv['delta_expectancy_usdt'], 4)} "
                     f"| {_f(cv['delta_net_pnl'])} | {cv['reading']} |")
    cv = payload["component_verdicts"]["s3_interaction_residue"]
    lines.append(f"| arm-full(S3+交互残差) | arm-T | — "
                 f"| {_f(cv['delta_net_pnl'])} | {cv['reading']} |")
    lines.append("")

    csr = payload["cap_skip_rates"]
    lines += ["## 5. 组合容量拒绝率(0.08 照链;dev 决策时刻口径)", "",
              "| 臂 | 容量跳 | open_intents | 拒绝率 |", "|---|---|---|---|"]
    for k, c in csr.items():
        if k == "note":
            continue
        lines.append(f"| {k} | {c['portfolio_cap_skips']} "
                     f"| {c['open_intents']} | {_f(c['cap_skip_rate'], 4)} |")
    lines += ["", f"> {csr['note']}。", ""]

    r1, r2 = rec["s1_trail_kill"], rec["s2_dead_bucket"]
    lines += ["## 6. 错杀/死桶回补(dev@1.5x,信号硬匹配)", "",
              f"- **S1 trail 错杀回补**:arm-T trail 桶净负笔(错杀)n="
              f"{r1['trail_kills_arm_T']};硬匹配 {r1['matched']}"
              f"(不匹配 {r1['unmatched']});arm-T 净 {_f(r1['net_arm_T_matched'])}"
              f" → arm-S1 净 {_f(r1['net_arm_S1_matched'])};**回补 "
              f"{_f(r1['recovered'])}**(改善笔 {r1['improved_n']})",
              f"- **S2 死桶回补**:arm-S2 落袋段在场笔 "
              f"{r2['s2_partial_trades']}(缘后到缘 {r2['edge_after_bank']},"
              f"未到缘而死 {r2['dead_after_bank']});死桶硬匹配 "
              f"{r2['matched']}(不匹配 {r2['unmatched']});arm-S2 净 "
              f"{_f(r2['net_arm_S2_matched'])} vs arm-T 净 "
              f"{_f(r2['net_arm_T_matched'])};**回补 {_f(r2['recovered'])}**",
              f"  - 死桶构成:{r2['dead_bucket_breakdown']}",
              f"- S1 扶手桶(arm-S1 dev@1.5x):{rec['s1_buckets_arm_S1_dev_1_5x']}",
              ""]

    to = payload["test_once_exposure"]
    lines += ["## 7. test 一次(第 23 次曝光;如实写 dev/test 对照,"
              "不渲染转正叙述)", "", f"- {to['note']}",
              f"- 最强件(dev@1.5x Δ净)= **{best_component}**"
              f"(Δ {payload['best_component_dev_1_5x']['delta_net_pnl']:+.2f});"
              f"曝光 = {test_arms}", ""]
    seg_labels = (("dev_1x", "dev@1x"), ("dev_1_5x", "dev@1.5x"),
                  ("dev_2x", "dev@2x"), ("test_1x", "test@1x"),
                  ("test_1_5x", "test@1.5x"), ("test_2x", "test@2x"))
    for a in test_arms:
        cell = to[a]
        lines += [f"### {a}", "",
                  "| 格 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
                  "|---|---|---|---|---|---|---|"]
        for kk, lab in seg_labels:
            lines.append(f"| {lab} | {_mrow(cell[kk])} |")
        lo, hi = cell["test_net_pnl_mean_ci95_1x"]
        up = cell["upgrade_line"]
        lines += ["",
                  f"- test@1x 逐笔净盈亏均值 95% CI(block bootstrap,"
                  f"2000 次,固定种子):{_f(lo, 4)} ~ {_f(hi, 4)} USDT",
                  "- 既往升级线逐条照报:",
                  f"  - test 笔数:{up['n_trades_test_1x']}"
                  f" ⇒ {up['sample_size']}",
                  f"  - 1.5x 净期望:{_f(up['expectancy_usdt_test_1_5x'], 4)}"
                  f" ⇒ {'>0 ✔' if up['expectancy_positive_1_5x'] else '≤0 ✘'}",
                  f"  - 2x PF:{_f(up['profit_factor_test_2_0x'])}"
                  f" ⇒ {'>1 ✔' if up['pf_gt_1_2_0x'] else '≤1 ✘'}",
                  f"  - **判定({a}):{up['verdict']}**", ""]
        r = cell["matrix_ratio_test_1x"]
        lines += [f"- test@1x 验收矩阵正期望格占比(EVAL):"
                  f" {r['cells_positive']}/{r['cells_eval']} = "
                  f"{_f(r['positive_share'], 4)}(INSUFFICIENT "
                  f"{r['cells_insufficient']})", ""]
        lines += render_matrix(cell["matrix_test_1x"],
                               cell["matrix_ratio_test_1x"])
    lines += ["",
              "- 对照柱(只读引用,不重复曝光):**arm-C(v17 在册,第 20 次"
              "曝光产物)** —— "
              + ";".join(
                  f"{k} 净 {_f(c['net_pnl'])}(n={c['n_trades']},"
                  f"PF {_f(c['profit_factor'])})"
                  for k, c in sorted(
                      payload["test_once_exposure"]
                      ["baseline_arm_c_test_in_register_v17"].items())),
              "",
              "> test<60 ⇒ INSUFFICIENT_DATA;升级线不过线即 Research Only 维持;"
              "不渲染任何转正叙述。", ""]

    lines += ["## 8. 事件 × 家族归因(dev@1.5x;机制 × TF × 方向,五臂)", ""]
    for a in ARM_IDS:
        bt = attr[a][f"dev@{J}x"]["buckets"]
        lines += [f"### {a}", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        any_rows = False
        for mech in ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED", "STRUCTURE_BROKEN", "FLIP_RETESTED", "EDGE_RECLAIMED"):
            for tf in TF_LIST:
                for side in ("short", "long"):
                    cell = bt.get(mech, {}).get(tf, {}).get(side)
                    if cell is None:
                        continue
                    any_rows = True
                    lines.append(f"| {mech} | {tf} | {side} | {cell['n_trades']} "
                                 f"| {_f(cell['winrate'], 3)} "
                                 f"| {_f(cell['avg_r'], 3)} "
                                 f"| {_f(cell['net_pnl'])} |")
        if not any_rows:
            lines.append("| — | — | — | 0 | — | — | — |")
        lines.append("")

    lines += ["## 9. 已知近似与如实声明", "",
              "① 全臂 early_exit=False:v20 基线 = v19 arm-T(锚④逐格硬对账"
              "通过),夭折处置不混入任何臂,孤立组件差读数干净;② S3=事件层"
              "抑制(候选不存在语义):arm-full 内 TRANSITION fade 不产生 v19 "
              "配注评估与 open_intent,计数单列 v20_s3_fade_transition_"
              "suppressed;S3 无独立臂(v18 R3 已证 §2),裁决表只给 full 残差"
              ",不给 S3 打包喜报;③ S4 只对 LEVEL_REJECTED:前缘改单在配注评估之前(计"
              "划限价计 F1/空间),执行落 canonical 限价主路(=FLIP_RETESTED G3 同通道:"
              "严格穿过 maker 成交、maker 费 0.2bp、N_fill=12 墙钟窗、BREAKOUT_ACCEPTED 优"
              "先级撤单/平仓撤销同一套);e3_market 仅在 S4 开臂内下线,arm-T/"
              "S1/S2 照旧;e3 缺 zone ⇒ fail-closed(v19_tier_reject_no_zone"
              " 沿链);④ S1 恰界与口径:MFE 恰 1R inclusive 上线;顺向 pivot"
              " = 多 pivot high 新高/空 pivot low 新低(脉冲向结构确认,与"
              " §8.3 收紧消费的逆向 pivot 互补;同种则闸退化 no-op,已实证"
              "钉死),上线后(含上线同批)首个逆向 pivot 起收紧;扶手族 = "
              "BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED/TREND_RESUMED(RANGE_ROTATED 与 "
              "fade 收割模式不受 S1 闸);trail_pending/trail_armed 桶标签只在"
              "扶手族 §8.1 出场替换 trail/invalid,破位谓词本身不受限;⑤ S2 "
              "与 R4 互斥(同栈 raise,死亡名单不回锅);S2 段落袋 40% 以 OPEN"
              " 名义 1.0 为基准,缘收剩 60%;目标方向不齐退回 v16 单段(如实"
              "声明);⑥ 组合容量决策账未乘档(保守,照 v19 声明);0.08 对产"
              "品臂、0.03 仅锚探针;⑦ 收割目标缘口径不变(h/l 恰触 "
              "inclusive);mic 拓扑沿 arm-C(仅续涨 mic,fade 归 bar);⑧ "
              "arm-C test 数字只读引用在册 v17 报告(第 20 次曝光),不重复曝"
              "光;test 第 23 次曝光纪律见 §7;⑨ 检测/费用/杠杆/单笔风险/历"
              "史产物/research-log 零改动;不渲染任何转正叙述。",
              "",
              "机器可读全量:`reports/v20_arms.json`;逐笔:"
              "`reports/trades_v20_arm-{T,S1,S2,S4,full}__dev[_cost*].csv`"
              "、`reports/trades_v20_arm-{full,<最强件>}__test[_cost*].csv`"
              "(增列 limit_from/s2_partial;收割桶含 trail_armed/"
              "trail_pending)。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
