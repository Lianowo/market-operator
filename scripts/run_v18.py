"""v18 修复包 R1-R4 arm 矩阵统一回测(docs/v18-protocol.md,冻结 2026-10-20;
research-log #34 预注册,先于运行):基线 = arm-C(v17 语义),R1 大炮走法
(续涨 mic-only / fade rngq>0.8 拒)、R2 逆三拍(反向幅 ≥0.3% 拒,恰界)、
R3 TRANSITION fade(候选条款)、R4 fade 三段收割(25%/50% 各 1/6 仓,缘收净,
无 trail)逐件独立臂 + arm-full 全件;dev 主评(@1.5x 净期望对照),test 一次
仅 arm-full 与最强单件(第 21 次曝光)。

用法:  python scripts/run_v18.py
配置:  config/v18.json(parent=config/v17.json,逐键硬对账;equity=1000、
       tf_list=[2h,4h,12h,1d]、v16_params、费用/容量/trigger 零漂移)
产物:  reports/v18_arms.{json,md}
       reports/trades_v18_arm-{C,R1,R2,R3,R4,full}__dev[_cost*].csv
       reports/trades_v18_arm-{full,<最强单件>}__test[_cost*].csv

口径与纪律:
- 三锚硬断言(不等即 raise,不写产物):① FROZEN_ERA 名单探针(dev 三格,
  arm=None,enable_mic=False,equity=300)== v6 冻结产物逐格全等(代码零回归);
  ② 现行 16 档名单 bar 回放 dev 三格 == v13 在册 bar 臂逐格全等(engine 零
  回归);③ v18 基线 arm-C dev 三格 == 在册 reports/v17_arm-C.json dev 三格
  逐格全等(基线语义锚:arm-C 即 PR#25 的 v17 arm-C)。
- 判定锚(dev 主评 @1.5x):每臂净/PF/n/期望R/收割桶,Δ(单件 − arm-C)净
  期望逐件归因,不打包宣判;R3 为候选条款(单独归因标注)。最强单件 =
  dev@1.5x 净期望最高的 R1..R4 单臂(并列取先,全 None ⇒ arm-R1 顺序兜底)。
- test 一次(第 21 次评估曝光,log#34 预注册):仅 arm-full 与最强单件同一
  轮次同次跑齐 1/1.5/2x 三格;arm-C test 数字只读引用在册 v17 报告(第 20
  次曝光产物),不重复曝光;样本量 <60 ⇒ INSUFFICIENT_DATA;既往升级线逐
  条照报;不渲染任何转正叙述;此后 v18 只许前向。
- 检测/费用/容量/历史产物/research-log 零改动;合约规格强制离线缓存;
  mic 帧复放 bar 事件流 == prepare_cognition 逐字段(脚本内硬断言);30m
  帧 manifest 哈希复核;事件计数对账与级联声明如实。
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
import run_backtest_v13 as rv13                                  # noqa: E402
import run_v16 as rv16                                           # noqa: E402
import run_v17 as rv17                                           # noqa: E402
from market_operator import aggregate as agg                     # noqa: E402
from market_operator.backtest_v6 import compute_metrics          # noqa: E402
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.campaign import (                           # noqa: E402
    ArmConfig,
    FADE_FAMILY,
    STOP_FAMILY,
    parse_key,
    run_campaigns,
)
from market_operator.harvest import HarvestParams, V16Router    # noqa: E402
from market_operator.position import (                           # noqa: E402
    R4_LEVEL_1,
    R4_LEVEL_2,
    R4_PARTIAL_FRAC,
)
from market_operator.repairs import (                            # noqa: E402
    CONT_FAMILY,
    FADE_FAMILY as REPAIRS_FADE_FAMILY,
    PMD_MIN,
    PMD_WINDOW,
    RNGQ_MAX,
    RNGQ_WINDOW,
    RepairParams,
    V18Params,
    V18Router,
)
from market_operator.research import block_bootstrap_ci          # noqa: E402
from market_operator.strategy_v6 import V6Params                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402
from market_operator.trigger import TRIGGER_TF_MAP               # noqa: E402

CONFIG_V18 = ROOT / "config" / "v18.json"
CONFIG_V17 = ROOT / "config" / "v17.json"
FROZEN_TURTLE = ROOT / "reports" / "backtest_v6_turtle.json"
FROZEN_V13 = ROOT / "reports" / "backtest_v13_cascade.json"
V17_REPORT_C = ROOT / "reports" / "v17_arm-C.json"
REPORTS = ROOT / "reports"

COST_MULTS = rv16.COST_MULTS
JUDGE_COST = rv16.JUDGE_COST
MIN_TRADES_TEST = rv16.MIN_TRADES_TEST
FROZEN_ERA_WHITELIST = rv16.FROZEN_ERA_WHITELIST
METRIC_EQ = rv16.METRIC_EQ
EQUITY0 = 1000.0
PROBE_EQUITY = 300.0
TF_LIST = ["2h", "4h", "12h", "1d"]
ARM_IDS = ("arm-C", "arm-R1", "arm-R2", "arm-R3", "arm-R4", "arm-full")
SINGLE_IDS = ("arm-R1", "arm-R2", "arm-R3", "arm-R4")
HARVEST_BUCKETS_V18 = rv16.HARVEST_BUCKETS + ("target_partial",)
UPGRADE_LINE = rv17.UPGRADE_LINE
EXPOSURE_NOTE = ("test 段第 21 次评估曝光(research-log #34 预注册,一次性;"
                 "仅 arm-full 与 dev@1.5x 最强单件同一轮次同次跑齐 "
                 "1/1.5/2x 三格;arm-C test 数字只读引用在册 "
                 "reports/v17_arm-C.json(第 20 次曝光产物),不重复曝光;"
                 "此后 v18 只许前向)")


# ---------------------------------------------------------------------------
# 配置(parent=v17 逐键硬对账;v18_params/家族表/单件开关冻结复核)
# ---------------------------------------------------------------------------


def load_config() -> dict:
    v18 = json.loads(CONFIG_V18.read_text(encoding="utf-8"))
    v17 = json.loads(CONFIG_V17.read_text(encoding="utf-8"))
    allow_diff = {"version", "note", "meta", "arms", "arm", "v18_params",
                  "v16_params"}   # v16_params 数值面下方单独深检(note 为叙述)
    for k in set(v18) | set(v17):
        if k in allow_diff:
            continue
        if v18.get(k) != v17.get(k):
            raise RuntimeError(f"config/v18.json parent=v17 漂移 {k}: "
                               f"{v18.get(k)} != {v17.get(k)}")
    p18 = {k: v for k, v in v18["v16_params"].items() if k != "note"}
    p17 = {k: v for k, v in v17["v16_params"].items() if k != "note"}
    if p18 != p17:
        raise RuntimeError("config/v18.json v16_params 数值面 != config/v17.json"
                           "(三闸/收割婚配先验不许碰;note 为叙述)")
    rv17.tf_of(v18["tf_list"])
    if float(v18["equity"]) != EQUITY0:
        raise RuntimeError(f"config/v18.json equity 必须 {EQUITY0}: {v18['equity']}")
    tf_map = {int(k): int(v) for k, v in v18["trigger_tf_map"].items()}
    if tf_map != TRIGGER_TF_MAP:
        raise RuntimeError("config/v18.json trigger_tf_map != 冻结级联映射")
    if set(v18["arms"]) != set(ARM_IDS):
        raise RuntimeError(f"config/v18.json arms 必须恰为 {ARM_IDS}")
    expect_repairs = {"arm-C": (), "arm-R1": ("R1",), "arm-R2": ("R2",),
                      "arm-R3": ("R3",), "arm-R4": ("R4",),
                      "arm-full": ("R1", "R2", "R3", "R4")}
    for a in ARM_IDS:
        spec = v18["arms"][a]
        if not (spec["v16"] and spec["fill_model"] == "exchange_maker"
                and spec["c3_stop_entry"]):
            raise RuntimeError(f"config/v18.json {a} v16 合成开关漂移: {spec}")
        if spec["mic_family"] != list(STOP_FAMILY):
            raise RuntimeError(f"config/v18.json {a} mic 拓扑必须沿 arm-C "
                               f"(仅续涨 mic): {spec['mic_family']}")
        if tuple(spec["repairs"]) != expect_repairs[a]:
            raise RuntimeError(f"config/v18.json {a} repairs 必须 "
                               f"{expect_repairs[a]}: {spec['repairs']}")
        if float(spec["equity"]) != EQUITY0:
            raise RuntimeError(f"config/v18.json {a} equity 必须 {EQUITY0}")
    vp = v18["v18_params"]
    frozen = (("rngq_window", RNGQ_WINDOW), ("rngq_max", RNGQ_MAX),
              ("pmd_window", PMD_WINDOW), ("pmd_min", PMD_MIN),
              ("r4_level_1", R4_LEVEL_1), ("r4_level_2", R4_LEVEL_2),
              ("r4_partial_frac", R4_PARTIAL_FRAC))
    for k, expect in frozen:
        if vp[k] != expect:
            raise RuntimeError(f"config/v18.json v18_params 冻结先验漂移 "
                               f"{k}: {vp[k]} != {expect}")
    if tuple(CONT_FAMILY) != tuple(STOP_FAMILY):
        raise RuntimeError("repairs.CONT_FAMILY != campaign.STOP_FAMILY(漂移)")
    fade_param = v18["v16_params"]["fade_family"]
    if tuple(fade_param) != tuple(FADE_FAMILY) \
            or tuple(fade_param) != tuple(REPAIRS_FADE_FAMILY):
        raise RuntimeError("fade 家族表三处不一致(repairs/campaign/v16_params)")
    return v18


# ---------------------------------------------------------------------------
# 臂定义 + 回放驱动
# ---------------------------------------------------------------------------


def _v18_params(rp_base: V6Params, hp, repair: RepairParams) -> V18Params:
    return V18Params(**{f: getattr(rp_base, f)
                        for f in V6Params.__dataclass_fields__},
                     v16=hp, v18=repair)


def build_arm(raw_cfg: dict, arm_id: str) -> tuple:
    """臂 → (HarvestParams, V18Router, ArmConfig, mic_families, RepairParams|None)。

    全臂:三闸/收割婚配/执行差分沿 v16(与 v17 arm-C 逐项同值),mic 拓扑
    全臂 = STOP_FAMILY(arm-C 语义);唯一差异 = RepairParams 开关子集;
    R4 在持仓侧(ArmConfig.harvest_ladder3);arm-C 的 v18=None ⇒
    V18Router 逐位 V16Router 行为(基线锚硬对账)。
    """
    p = raw_cfg["v16_params"]
    vp = raw_cfg["v18_params"]
    reps = tuple(raw_cfg["arms"][arm_id]["repairs"])
    hp = HarvestParams(
        **{k: p[k] for k in ("pos_short_min", "pos_long_max", "k_room_R",
                             "k_is", "k_star", "tier_a_mult", "tier_b_mult",
                             "k_cap_hours", "cap_mfe_min")},
        fade_family=tuple(p["fade_family"]))
    arm = ArmConfig(fill_model="exchange_maker",
                    stop_entry=True,
                    stop_wait_tf=int(p["stop_wait_tf"]),
                    k_bm=float(p["k_bm"]),
                    c2_family=tuple(p["fade_family"]),
                    harvest_family=tuple(p["fade_family"]),
                    time_cap_bars=int(p["k_cap_hours"]),
                    cap_mfe_min=float(p["cap_mfe_min"]),
                    harvest_ladder3=("R4" in reps))
    repair = None
    if reps:
        repair = RepairParams(r1=("R1" in reps), r2=("R2" in reps),
                              r3=("R3" in reps),
                              rngq_window=int(vp["rngq_window"]),
                              rngq_max=float(vp["rngq_max"]),
                              pmd_window=int(vp["pmd_window"]),
                              pmd_min=float(vp["pmd_min"]))
    mic_families = tuple(p["stop_family"])
    return hp, V18Router, arm, mic_families, repair


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
            res.trades = augment_trades_v18(res.trades, frames, res.ledger)
        m = compute_metrics(res)
        out[mult] = (res, m)
        c = res.counters
        print(f"      [bt] {tag}{segment}@{mult}x"
              f"{' mic' if enable_mic else ' bar'}: net={m['net_pnl']:+.2f} "
              f"n={m['n_trades']} "
              f"pf={m['profit_factor'] and round(m['profit_factor'], 3)} "
              f"r1s={c.get('v18_r1_cont_bar_suppressed', 0)}/"
              f"{c.get('v18_r1_fade_rngq_reject', 0)} "
              f"r2={c.get('v18_r2_pmd_reject', 0)} "
              f"r3={c.get('v18_r3_fade_transition_reject', 0)} "
              f"r4={c.get('v18_r4_partial_banked', 0)}/"
              f"{c.get('v18_r4_edge_collect', 0)} ({time.time() - t0:.0f}s)",
              flush=True)
    liq = sum(res.counters["liquidations"] for res, _ in out.values())
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")
    return out


# ---------------------------------------------------------------------------
# 逐笔增广(v18 = v17 增广 + rngq/pmd 溯源 + target_partial 收割桶标注)
# ---------------------------------------------------------------------------


def augment_trades_v18(trades: list, frames: dict, ledger: list) -> list:
    """rv17.augment_trades_v17 + v18 增量:

    - rngq/pmd 溯源列(决策 meta 通道,arm-C 恒 None);
    - harvest_bucket:R4 收割序标注 —— reasons_reduce 任一含
      "target_partial" ⇒ "target_partial"(否则沿 v16 解析值);
      escalates:缘收 R4 批若恰无段落(v16 退回路径)标签不变,如实。
    """
    aug = rv17.augment_trades_v17(trades, frames, ledger)
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
        bucket = t.get("harvest_bucket")
        if any(isinstance(r, str) and "target_partial" in r
               for r in t.get("reasons_reduce", ())):
            bucket = "target_partial"
        out.append({**t, "harvest_bucket": bucket,
                    "rngq": m.get("rngq"), "pmd": m.get("pmd")})
    return out


COLUMNS_V18 = list(rv17.COLUMNS_V17) + ["rngq", "pmd"]


def trades_frame_v18(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": rb._iso(t["open_decision_ts"]),
                     "open_iso": rb._iso(t["open_ts"]),
                     "close_decision_iso": rb._iso(t["close_decision_ts"]),
                     "exit_iso": rb._iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=COLUMNS_V18)


# ---------------------------------------------------------------------------
# 条款生效统计(v16 三闸 + v18 四件)+ 收割桶(target_partial 扩桶)
# ---------------------------------------------------------------------------


def clause_stats_v18(counters: dict) -> dict:
    base = rv16.clause_stats(counters)
    g = counters.get
    base.update({
        "v18_r1_cont_bar_suppressed": g("v18_r1_cont_bar_suppressed", 0),
        "v18_r1_fade_rngq_evaluated": g("v18_r1_fade_rngq_evaluated", 0),
        "v18_r1_fade_rngq_reject": g("v18_r1_fade_rngq_reject", 0),
        "v18_r2_pmd_evaluated": g("v18_r2_pmd_evaluated", 0),
        "v18_r2_pmd_reject": g("v18_r2_pmd_reject", 0),
        "v18_r3_fade_evaluated": g("v18_r3_fade_evaluated", 0),
        "v18_r3_fade_transition_reject": g("v18_r3_fade_transition_reject", 0),
        "v18_r4_partial_banked": g("v18_r4_partial_banked", 0),
        "v18_r4_edge_collect": g("v18_r4_edge_collect", 0),
    })
    return base


def harvest_buckets_v18(trades: list) -> dict:
    """收割桶(v16 五桶 + target_partial;段内开仓平仓笔)。"""
    nets: dict = {b: [] for b in HARVEST_BUCKETS_V18}
    for t in trades:
        if not t["in_segment_open"] or not t["closed"]:
            continue
        nets[t.get("harvest_bucket") or "other"].append(float(t["net_pnl"]))
    return {b: rv16._cell_stats(nets[b]) for b in HARVEST_BUCKETS_V18 if nets[b]}


def _cellm(m: dict) -> dict:
    return {"net_pnl": m["net_pnl"], "n_trades": m["n_trades"],
            "winrate": m["winrate"], "profit_factor": m["profit_factor"],
            "expectancy_usdt": m["expectancy_usdt"],
            "expectancy_r": m["expectancy_r"]}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    t_start = time.time()
    for stream in (sys.stdout, sys.stderr):      # GBK 控制台:不可编码字符
        try:                                   # 以 ? 替代(报告文件走 utf-8,
            stream.reconfigure(errors="replace")   # 不受影响;v17 同款纪律)
        except Exception:
            pass
    raw_cfg = load_config()
    params = Params.from_json(CONFIG_V18)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v18 parent=v17:merge_max_atr 必须 2.0")

    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    for sym in symbols:
        p30 = agg.ensure_30m([sym])[sym]
        rec = json.loads((ROOT / "data" / "manifest.json")
                         .read_text(encoding="utf-8"))["files"][f"{sym}_30m.csv"]
        assert hashlib.sha256(p30.read_bytes()).hexdigest() == rec["sha256"]
    print("[data] 30m 帧齐(manifest 哈希复核过)", flush=True)

    print("[prep] v18 战役帧构建(v17 帧装载复用;认知一次性)", flush=True)
    frames, probe_frames, prep = rv17.prepare_frames_v17(raw_cfg, params)
    rv17.assert_frames_no_1h(frames)
    for d in prep["declarations"]:
        print(f"[decl] {d}", flush=True)
    xcheck = rb.cross_checks(probe_frames, float(raw_cfg["k_is"]),
                             merge_max_atr=2.0)

    frozen_turtle = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    frozen_v13 = json.loads(FROZEN_V13.read_text(encoding="utf-8"))
    v17_c_report = json.loads(V17_REPORT_C.read_text(encoding="utf-8"))
    if v17_c_report["arm"] != "arm-C":
        raise RuntimeError("在册基线引用必须 v17 arm-C 报告")

    # -- 锚①/②(engine/代码零回归;探针 equity=300,mic 禁用) ---------------------
    print("[anchor-1] FROZEN_ERA 探针(dev 三格,arm=None,mic 禁用)", flush=True)
    rp_probe = dc_replace(rp_base, whitelist=frozenset(FROZEN_ERA_WHITELIST))
    probe = run_grid(probe_frames, raw_cfg, params, rp_probe, None, None,
                     contracts, segment="dev", equity0=PROBE_EQUITY,
                     enable_mic=False, tag="anchor1 ")
    parity_probe = rv16.assert_anchor("FROZEN_ERA 探针", probe,
                                      frozen_turtle["runs"], "turtle|dev")
    print("[anchor-1] 探针 3 格 == v6 冻结产物(逐格全等)", flush=True)

    print("[anchor-2] 现行 16 档名单 bar 回放 == v13 在册 bar 臂", flush=True)
    probe16 = run_grid(probe_frames, raw_cfg, params, rp_base, None, None,
                       contracts, segment="dev", equity0=PROBE_EQUITY,
                       enable_mic=False, tag="anchor2 ")
    parity_bar16 = rv16.assert_anchor("16 档 bar 回放", probe16,
                                      frozen_v13["runs"], "bar|dev")
    print("[anchor-2] 16 档 3 格 == v13 在册 bar 臂(逐格全等)", flush=True)

    # -- dev 主网格:六臂 equity=1000,三格/臂 -------------------------------------
    grids: dict = {}
    arms_built: dict = {}
    for arm_id in ARM_IDS:
        hp, factory, arm, mic_families, repair = build_arm(raw_cfg, arm_id)
        arms_built[arm_id] = {"repair": repair, "mic_families": mic_families,
                              "arm": arm}
        rp_use = _v18_params(rp_base, hp, repair)
        print(f"[arm] {arm_id}: {raw_cfg['arms'][arm_id]['note']}", flush=True)
        grids[arm_id] = {"dev": run_grid(
            frames, raw_cfg, params, rp_use, factory, arm, contracts,
            segment="dev", equity0=EQUITY0, mic_families=mic_families,
            enable_mic=True, tag=f"{arm_id} ")}

    # -- 锚③:v18 基线 arm-C == 在册 v17 arm-C dev 三格(基线语义锚) -------------
    print("[anchor-3] v18 arm-C 基线 == 在册 v17 arm-C(dev 三格逐格全等)",
          flush=True)
    parity_baseline = []
    for mult in COST_MULTS:
        m = grids["arm-C"]["dev"][mult][1]
        fm = v17_c_report["runs"][f"arm-C|dev@{mult}x"]["metrics"]
        ok = all(m[k] == fm[k] for k in METRIC_EQ)
        parity_baseline.append({"run": f"v18 arm-C|dev@{mult}x",
                                **{f"v18_{k}": m[k] for k in METRIC_EQ},
                                **{f"v17_{k}": fm[k] for k in METRIC_EQ},
                                "parity": ok})
        if not ok:
            raise RuntimeError(f"基线锚不一致 arm-C|dev@{mult}x: "
                               f"{parity_baseline[-1]}")
    print("[anchor-3] 基线 3 格 == v17 在册 arm-C(逐格全等)", flush=True)

    # -- 判定锚(dev 主评 @1.5x:逐件 Δ 净期望;最强单件遴选) ----------------------
    def exp(a, seg, mult):
        return grids[a][seg][mult][1]["expectancy_usdt"]

    eC = exp("arm-C", "dev", JUDGE_COST)
    component_verdicts: dict = {}
    for a in SINGLE_IDS + ("arm-full",):
        ea = exp(a, "dev", JUDGE_COST)
        d = (ea - eC) if (ea is not None and eC is not None) else None
        cid = a.replace("arm-", "")
        component_verdicts[a] = {
            "component": cid, "baseline": "arm-C",
            "expectancy_usdt_dev_1_5x": ea,
            "expectancy_usdt_baseline": eC,
            "delta_expectancy_usdt": d,
            "delta_net_pnl": (grids[a]["dev"][JUDGE_COST][1]["net_pnl"]
                              - grids["arm-C"]["dev"][JUDGE_COST][1]["net_pnl"]),
            "candidate_clause": (a == "arm-R3"),
            "verdict": ("候选(effective)" if a == "arm-R3" and d is not None
                        and d > 0 else
                        "候选(无改善)" if a == "arm-R3" else
                        "effective(改善)" if d is not None and d > 0
                        else "打回(无改善)")}
    best_single = max(
        SINGLE_IDS,
        key=lambda a: (exp(a, "dev", JUDGE_COST)
                       if exp(a, "dev", JUDGE_COST) is not None
                       else float("-inf"), -SINGLE_IDS.index(a)))
    print(f"[judge] 最强单件(dev@1.5x 净期望)= {best_single}"
          f"({exp(best_single, 'dev', JUDGE_COST)})", flush=True)

    # -- test 一次(第 21 次曝光;仅 arm-full 与最强单件) -----------------------------
    test_arms = (best_single, "arm-full")
    print(f"[test] 第 21 次曝光:{test_arms}(一次性三格;此后只许前向)",
          flush=True)
    for arm_id in test_arms:
        hp, factory, arm, mic_families, repair = build_arm(raw_cfg, arm_id)
        rp_use = _v18_params(rp_base, hp, repair)
        grids[arm_id]["test"] = run_grid(
            frames, raw_cfg, params, rp_use, factory, arm, contracts,
            segment="test", equity0=EQUITY0, mic_families=mic_families,
            enable_mic=True, tag=f"{arm_id} ")

    # -- 归因装配 -------------------------------------------------------------------
    sig = {seg: rv17.signals_by_tf(frames, dev_frac, segment=seg)
           for seg in ("dev", "test")}

    def cell_attr(arm_id, seg, mult):
        res, m = grids[arm_id][seg][mult]
        return {
            "metrics": m, "counters": res.counters,
            "clause": clause_stats_v18(res.counters),
            "harvest": harvest_buckets_v18(res.trades),
            "family": rv16.family_buckets(res.trades),
            "tf_ledger": rv17.tf_ledger(res, sig[seg]),
            "mic_share": rv17.mic_entry_share(res.trades),
            "advance": rv13.mic_advance_stats(res, frames),
            "giveback": rb.giveback_summary(res.trades),
            "buckets": rb.bucket_tables(res.trades),
            "n_trades_1d": sum(1 for t in res.trades
                               if t["in_segment_open"] and t["tf"] == "1d"),
        }

    attr: dict = {a: {} for a in ARM_IDS}
    for a in ARM_IDS:
        for mult in COST_MULTS:
            attr[a][f"dev@{mult}x"] = cell_attr(a, "dev", mult)
    for a in test_arms:
        for mult in COST_MULTS:
            attr[a][f"test@{mult}x"] = cell_attr(a, "test", mult)

    # -- test 一次判定(升级线逐条 + CI;arm-C 引用在册) --------------------------------
    test_once: dict = {}
    for a in test_arms:
        res1, m1 = grids[a]["test"][1.0]
        res15, m15 = grids[a]["test"][1.5]
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
            "test_net_pnl_mean_ci95_1x": list(ci), "upgrade_line": up}
    baseline_test_reg = {f"test@{m}x": _cellm(
        v17_c_report["runs"][f"arm-C|test@{m}x"]["metrics"])
        for m in COST_MULTS}

    # -- 产物装配/落盘 -------------------------------------------------------------------
    REPORTS.mkdir(exist_ok=True)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "doc": "docs/v18-protocol.md(冻结,2026-10-20)",
            "pre_registration": "research-log #34(先于运行)",
            "baseline": ("基线 = arm-C(v17 语义,PR#25):仅续涨 mic / fade 归 "
                         "bar;V18Router(v18=None) 重放,与在册 "
                         "reports/v17_arm-C.json dev 三格硬对账通过"),
            "repairs": {
                "R1": "大炮走法:续涨族仅级联 mic 口径入场(bar 化身整层抑制,"
                      "错峰惰性;三根属性不变);fade 族 rngq>0.8 不入场"
                      "(窗 48,恰 0.8 受理)",
                "R2": "逆三拍:前三根认知 bar 净方向反向且幅 ≥0.3%"
                      "(d·pmd ≤ −0.003,恰界拒)⇒ 不入场(假设合法性核)",
                "R3": "TRANSITION fade 不受理(仅 TREND_UP/TREND_DOWN/RANGE;"
                      "候选条款,单独归因)",
                "R4": "fade 三段收割:缘前 25%/50% 各落袋 1/6 仓(半仓 1/3,"
                      "市价次细 bar 开盘,摊薄均价入账),到缘收净,无 trail"
                      "无保本;收割序 target_partial 标注",
            },
            "parity_discipline": ("三锚硬断言:FROZEN_ERA 探针 == v6 冻结产物逐格"
                                  "全等(代码零回归);16 档 bar 回放 == v13 在册 "
                                  "bar 臂逐格全等(engine 零回归);v18 arm-C dev "
                                  "三格 == 在册 v17 arm-C 逐格全等(基线语义锚)。"
                                  "不等即 raise,报告不落地(本轮已通过)"),
            "test_eval_declaration": EXPOSURE_NOTE,
            "judge_rule": ("dev 主评 @1.5x:Δ(单件 − arm-C)净期望逐件归因,"
                           "不打包宣判;R3 候选条款单独标注;损法判定升级线同 "
                           "v17 不变口径"),
            "upgrade_line": UPGRADE_LINE,
            "cascade_declarations": prep["declarations"],
            "honesty_note": ("mic 拓扑全臂沿 arm-C(续涨 mic,fade/E4/E5 归 "
                             "bar);mic 层的 rngq 定义(trigger 帧自身 48 窗)在"
                             "测量网格中惰性(fade 无 mic 决策),仅单元测试行"
                             "使,如实声明;R4 段落市量价撮合(taker 次细 bar "
                             "开盘),缘收净沿 v16 EXIT 口径不变;检测/费用/"
                             "容量/历史产物零改动;不渲染任何转正叙述。"),
        },
        "config": {
            "config_file": "config/v18.json",
            "parent": "config/v17.json(逐键硬对账,run 前通过)",
            "tf_list": TF_LIST, "equity0": EQUITY0,
            "risk_frac": raw_cfg["risk_frac"],
            "leverage_cap": raw_cfg["leverage_cap"],
            "taker_fee": raw_cfg["taker_fee"],
            "maker_fee": raw_cfg["maker_fee"],
            "slippage_per_side": raw_cfg["slippage_per_side"],
            "funding_per_8h_per_side": raw_cfg["funding_per_8h_per_side"],
            "portfolio_cap_frac": raw_cfg["portfolio_cap_frac"],
            "dev_frac": dev_frac,
            "cost_mults": list(COST_MULTS), "judge_cost_mult": JUDGE_COST,
            "trigger_tf_map": {str(k): v for k, v in TRIGGER_TF_MAP.items()},
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "v16_params": raw_cfg["v16_params"],
            "v18_params": raw_cfg["v18_params"],
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
        "component_verdicts": component_verdicts,
        "strongest_single": {"arm": best_single,
                             "rule": "dev@1.5x 净期望最高的 R1..R4 单臂"
                                     "(并列取先;全 None 顺序兜底)",
                             "expectancy_usdt_dev_1_5x":
                                 exp(best_single, "dev", JUDGE_COST)},
        "signals_by_tf": sig,
        "test_once_exposure": {
            "note": EXPOSURE_NOTE,
            **test_once,
            "baseline_arm_c_test_in_register_v17": baseline_test_reg,
        },
        "runs": {},
    }
    for a in ARM_IDS:
        for mult in COST_MULTS:
            payload["runs"][f"{a}|dev@{mult}x"] = attr[a][f"dev@{mult}x"]
        if a in test_arms:
            for mult in COST_MULTS:
                payload["runs"][f"{a}|test@{mult}x"] = attr[a][f"test@{mult}x"]

    (REPORTS / "v18_arms.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "v18_arms.md").write_text(
        render_md(payload, grids, attr), encoding="utf-8")
    for a in ARM_IDS:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v18(grids[a]["dev"][mult][0].trades).to_csv(
                REPORTS / f"trades_v18_{a}__dev{suffix}.csv", index=False)
    for a in test_arms:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v18(grids[a]["test"][mult][0].trades).to_csv(
                REPORTS / f"trades_v18_{a}__test{suffix}.csv", index=False)
    print(f"-> reports/v18_arms.json/.md + trades_v18 CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    if x is None or (isinstance(x, float) and x != x):
        return dash
    if isinstance(x, float) and x == float("inf"):
        return "inf"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def _mrow(m: dict) -> str:
    return (f"{_f(m['net_pnl'])} | {m['n_trades']} | {_f(m['winrate'], 3)} "
            f"| {_f(m['profit_factor'])} | {_f(m['expectancy_usdt'], 4)} "
            f"| {_f(m['expectancy_r'], 4)}")


def _bucket_digest(hb: dict) -> str:
    if not hb:
        return "—"
    parts = []
    for b in HARVEST_BUCKETS_V18:
        if b in hb:
            c = hb[b]
            parts.append(f"{b[:14]}:{c['n_trades']}/{_f(c['net_pnl'], 1)}")
    return " ".join(parts) if parts else "—"


def render_md(payload: dict, grids: dict, attr: dict) -> str:
    lines = [
        "# v18 修复包 R1-R4 arm 矩阵统一回测报告(基线 arm-C;dev 主评 + "
        "test 一次)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明", "",
        f"- {payload['protocol']['baseline']}",
        f"- 预注册:{payload['protocol']['pre_registration']};"
        f"**{payload['protocol']['test_eval_declaration']}**",
        f"- parity 纪律:{payload['protocol']['parity_discipline']}",
        "- 级联声明:"
        + (";".join(payload["protocol"]["cascade_declarations"])
           if payload["protocol"]["cascade_declarations"]
           else "映射全部精确命中,无声明。"),
        f"- {payload['protocol']['honesty_note']}",
        f"- 合约:{payload['contracts']['source']}(离线强制)。",
        "",
        "## 1. 判定总览(dev 主评 @1/1.5/2x;净/PF/n/期望/收割桶)", "",
        "| 臂 | 格 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R | 收割桶"
        "(n/净) |",
        "|---|---|---|---|---|---|---|---|---|"]
    for a in ARM_IDS:
        for mult in COST_MULTS:
            m = grids[a]["dev"][mult][1]
            tag = " ⭐最强单件" if a == payload["strongest_single"]["arm"] \
                and mult == JUDGE_COST else ""
            lines.append(
                f"| {a}{tag} | dev@{mult}x | {_mrow(m)} "
                f"| {_bucket_digest(attr[a][f'dev@{mult}x']['harvest'])} |")
    lines += ["",
              f"| Δ(arm-full − arm-C) | dev@1.5x | "
              f"{grids['arm-full']['dev'][JUDGE_COST][1]['net_pnl']
                 - grids['arm-C']['dev'][JUDGE_COST][1]['net_pnl']:+.2f} "
              f"| {grids['arm-full']['dev'][JUDGE_COST][1]['n_trades']
                 - grids['arm-C']['dev'][JUDGE_COST][1]['n_trades']:+d} "
              f"| — | — "
              f"| {_f(payload['component_verdicts']['arm-full']['delta_expectancy_usdt'], 4)} "
              f"| — | — |", ""]

    lines += ["## 2. 组件裁决表(dev@1.5x,逐件 Δ 净期望;不打包宣判)", "",
              "| 单件 | Δ净期望USDT | Δ净盈亏 | 条款 | 裁决 |",
              "|---|---|---|---|---|"]
    for a in ("arm-R1", "arm-R2", "arm-R3", "arm-R4", "arm-full"):
        cv = payload["component_verdicts"][a]
        note = "候选条款" if cv["candidate_clause"] else "—"
        lines.append(f"| {a} | {_f(cv['delta_expectancy_usdt'], 4)} "
                     f"| {_f(cv['delta_net_pnl'])} | {note} "
                     f"| **{cv['verdict']}** |")
    lines += ["",
              "> R3 为候选条款(docs/v18-protocol.md §R3:候选,报告单独归因);"
              "eC(dev@1.5x 净期望,arm-C)= "
              f"{_f(payload['component_verdicts']['arm-R1']['expectancy_usdt_baseline'], 4)}"
              f" USDT。最强单件 = **{payload['strongest_single']['arm']}**"
              f"(dev@1.5x 净期望 "
              f"{_f(payload['strongest_single']['expectancy_usdt_dev_1_5x'], 4)}"
              ")。", ""]

    lines += ["## 3. 组件条款生效统计(逐臂 dev@1.5x)", "",
              "| 臂 | R1续涨bar抑 | R1fade评估 | R1fade拒 | R2评估 | R2拒 | "
              "R3评估 | R3拒 | R4段落 | R4缘收 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for a in ARM_IDS:
        c = attr[a][f"dev@{JUDGE_COST}x"]["clause"]
        lines.append(
            f"| {a} | {c['v18_r1_cont_bar_suppressed']} "
            f"| {c['v18_r1_fade_rngq_evaluated']} "
            f"| {c['v18_r1_fade_rngq_reject']} "
            f"| {c['v18_r2_pmd_evaluated']} | {c['v18_r2_pmd_reject']} "
            f"| {c['v18_r3_fade_evaluated']} "
            f"| {c['v18_r3_fade_transition_reject']} "
            f"| {c['v18_r4_partial_banked']} | {c['v18_r4_edge_collect']} |")
    lines += ["",
              "> v16 三闸/档/maker/stop 统计逐格在 JSON runs[*].clause"
              "(v16_gate_evaluated/reject/tier、stop_entry_*、maker_*);"
              "arm-C 的 v18 键恒 0(基线语义锚)。", ""]

    lines += ["## 4. 收割桶 × 分家族桶(dev@1.5x;target_partial = R4 收割序)", ""]
    for a in ARM_IDS:
        hb = attr[a][f"dev@{JUDGE_COST}x"]["harvest"]
        fb = attr[a][f"dev@{JUDGE_COST}x"]["family"]
        lines += [f"### {a}", "", "| 桶 | 笔数 | 净额 | 胜率 | PF |",
                  "|---|---|---|---|---|"]
        for b in HARVEST_BUCKETS_V18:
            if b in hb:
                c = hb[b]
                lines.append(f"| 收割:{b} | {c['n_trades']} "
                             f"| {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
                             f"| {_f(c['profit_factor'])} |")
        for fam in ("fade", "continuation", "other"):
            if fam in fb:
                c = fb[fam]
                lines.append(f"| 家族:{fam} | {c['n_trades']} "
                             f"| {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
                             f"| {_f(c['profit_factor'])} |")
        lines.append("")

    to = payload["test_once_exposure"]
    lines += ["## 5. test 一次(第 21 次曝光;如实写 dev/test 对照,"
              "不渲染转正叙述)", "",
              f"- {to['note']}", ""]
    test_arms = tuple(k for k in to if k.startswith("arm-"))
    seg_labels = (("dev_1x", "dev@1x"), ("dev_1_5x", "dev@1.5x"),
                  ("dev_2x", "dev@2x"), ("test_1x", "test@1x"),
                  ("test_1_5x", "test@1.5x"), ("test_2x", "test@2x"))
    for a in test_arms:
        cell = to[a]
        lines += [f"### {a}", "",
                  "| 格 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
                  "|---|---|---|---|---|---|---|"]
        for kk, lab in seg_labels:
            c = cell[kk]
            lines.append(f"| {lab} | {_mrow(c)} |")
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

    lines += ["## 6. 事件 × 家族归因(dev@1.5x;机制 × TF × 方向)", ""]
    for a in ARM_IDS:
        bt = attr[a][f"dev@{JUDGE_COST}x"]["buckets"]
        sh = attr[a][f"dev@{JUDGE_COST}x"]["mic_share"]
        lines += [f"### {a}", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        any_rows = False
        for mech in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"):
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
        share = "; ".join(f"{fam}: mic {sh[fam]['mic']} / bar {sh[fam]['bar']}"
                          for fam in sorted(sh))
        lines += ["", f"mic 入口占比(成交):{share or '—'}", ""]

    lines += ["## 7. 已知近似与如实声明", "",
              "① rngq 定义 = 事件 bar 波幅(h−l)在前 48 根波幅中的 ≤ 占比"
              "分位(可用历史不足 48 根用在场前史,恰 0.8 受理);mic 路径取 "
              "trigger 帧自身 48 窗、pmd 取最近已收三根认知 bar(mic 层 fade "
              "在测量网格中惰性,如实声明);② R2 pmd = 前三根认知 bar "
              "Σ(c−o)/o,方向落成 d·pmd ≤ −0.003 即拒(反向幅恰 0.3% 拒,"
              "协议 pmd≤−0.3 口径);历史不足三根或 ctx 缺失 ⇒ 不拒(定向禁"
              "令非 fail-closed,如实声明);③ 条款拒绝次序 R1b→R2→R3(逐决"
              "策至多落一个拒绝桶);R1a 在事件层(续涨 bar 化身抑制含其 "
              "E1 优先级撤单/episode 副作用,错峰惰性),R1b/R2/R3 在决策层"
              "(v16 三闸之后的假设合法性核);④ R4 段落 1/6 仓以 OPEN 名义"
              "为基准,市价撮合(次一执行细 bar 开盘,taker),摊薄均价入账"
              "由 broker/reconstruct 既有口径承担;缘收净沿 v16 EXIT 口径不"
              "变;R4 不睬中间 trail 与保本(invalid 不动);目标方向不齐的"
              "退化 fade 持仓退回 v16 单段收割(如实);⑤ 收割桶标注:笔内"
              "任一 target_partial 段落 ⇒ 整笔归 target_partial 桶(v16 五桶"
              "扩一桶);⑥ 组合 3% 容量决策账未乘档(保守,同 v14/v17 声"
              "明);⑦ arm-C test 数字只读引用在册 v17 报告(第 20 次曝光),"
              "不重复曝光;test 曝光纪律见 §5;⑧ 检测/费用/容量/历史产物/"
              "research-log 零改动。",
              "",
              "机器可读全量:`reports/v18_arms.json`;逐笔:"
              "`reports/trades_v18_arm-{C,R1,R2,R3,R4,full}__dev[_cost*].csv`"
              "、`reports/trades_v18_arm-{full,<最强单件>}__test[_cost*].csv`"
              "(增列 rngq/pmd;收割桶含 target_partial)。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
