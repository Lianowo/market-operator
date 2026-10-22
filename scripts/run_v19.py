"""v19 阶梯配注 + 组合 8% + 夭折处置 + 市价默认 统一回测(docs/v19-protocol.md,
冻结 2026-10-20;research-log #35 预注册,先于运行):所有者五条裁决
①portfolio_cap_frac 3%→8%;②v16 三闸改阶梯配注(gates_model=tier_sizing,
条款照 §2:基础 A=1.0,带沿失位×0.5 / room∈[1.0,1.5)×0.75、<1.0×0.5 /
TRANSITION fade×0.5 / 大蛋糕 rngq>0.8 不 mic-only 处×0.75 / 逆三拍×0.5;
硬禁只剩 F1 与结构性 target 不存在);③夭折处置组件(半窗 MFE<0.3R 出、
连续≥3 根收盘全逆出,默认 ON);④默认市价(maker/stop_entry 降级为对照
臂);⑤按"事件×TF×方向 期望矩阵"验收(n<15 ⇒ INSUFFICIENT)。

用法:  python scripts/run_v19.py
配置:  config/v19.json(parent=config/v18.json,逐键硬对账;equity=1000、
       tf_list=[2h,4h,12h,1d]、v16_params/v18_params 数值面深等照链、费用/
       杠杆/单仓风险触发零漂移;portfolio_cap_frac=0.08;gates_model=
       tier_sizing)
产物:  reports/v19.{json,md}
       reports/trades_v19_arm-{T,X,maker,stop}__dev[_cost*].csv
       reports/trades_v19_arm-{T,X}__test[_cost*].csv

口径与纪律:
- 三锚硬断言(不等即 raise,不写产物):① FROZEN_ERA 名单探针(dev 三格,
  arm=None,enable_mic=False,equity=300,**容量按 0.03 锚探针口径**)== v6
  冻结产物逐格全等(代码零回归);② 现行 16 档名单 bar 回放 dev 三格 ==
  v13 在册 bar 臂逐格全等(engine 零回归);③ arm-C(v17 语义,V16Router +
  v16 ArmConfig,容量按 0.03 锚探针口径)dev 三格 == 在册
  reports/v17_arm-C.json dev 三格逐格全等(基线语义锚)。探针容量覆盖
  0.03 只用于锚与 v17 对照,产品臂一律 0.08。
- 判定锚(dev 主评 @1.5x):逐臂净/PF/n/期望/收割桶;组件裁决 = Δ(arm-X −
  arm-T)孤立夭折、Δ(arm-maker/arm-stop − arm-X)孤立执行价差;arm-C(0.03
  探针)只作容量 3%→8% 拒绝率变化与链背景参照,不参与裁决。
- 验收(协议 §4):判定表首页 = 事件×TF×方向 n/净/期望矩阵(旗舰 arm-X
  dev@1.5x;目标每格期望 ≥0;单格 n<15 一律 INSUFFICIENT,不给叙述);
  其次收割桶(early|half_mfe / early|consec)与条款生效统计。
- test 一次(第 22 次评估曝光,log#35 预注册,一次性):仅 arm-T 与 arm-X
  同一轮次同次跑齐 1/1.5/2x 三格;arm-C test 数字只读引用在册 v17 报告
  (第 20 次曝光产物),不重复曝光;样本量 <60 ⇒ INSUFFICIENT_DATA;既往
  升级线逐条照报;不渲染任何转正叙述;此后 v19 只许前向。
- 死亡名单不回锅:层级兑现(R4 三段)不接线;打包优质五问不复活。
- 检测/费用/杠杆上限/单笔风险/历史产物/research-log/acceptance 零改动;
  合约规格强制离线缓存;mic 帧复放 bar 事件流 == prepare_cognition 逐字段
  (硬断言);30m 帧 manifest 哈希复核;级联声明如实。
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
from market_operator.replay import compute_metrics          # noqa: E402
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.campaign import (                           # noqa: E402
    ArmConfig,
    FADE_FAMILY,
    STOP_FAMILY,
    parse_key,
    run_campaigns,
)
from market_operator.position import (                           # noqa: E402
    EARLY_CONSEC_N,
    EARLY_HALF_MFE_R,
)
from market_operator.research import block_bootstrap_ci          # noqa: E402
from market_operator.routing import RouterParams                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402
from market_operator.tiered import (                             # noqa: E402
    TieredParams,
    V19Params,
    V19Router,
)
from market_operator.harvest import HarvestParams, V16Params, V16Router  # noqa: E402
from market_operator.trigger import TRIGGER_TF_MAP               # noqa: E402

CONFIG_V19 = ROOT / "config" / "v19.json"
CONFIG_V18 = ROOT / "config" / "v18.json"
FROZEN_TURTLE = ROOT / "reports" / "replay_turtle.json"
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
CAP_PRODUCT = 0.08                       # 裁决①:产品臂组合容量(8%)
CAP_ANCHOR = 0.03                        # 锚探针容量(v17 在册口径,参照用)
TF_LIST = ["2h", "4h", "12h", "1d"]
ARM_IDS = ("arm-T", "arm-X", "arm-maker", "arm-stop")
TEST_ARMS = ("arm-T", "arm-X")           # 第 22 次曝光(一次性)
UPGRADE_LINE = rv17.UPGRADE_LINE
HARVEST_BUCKETS_V19 = rv16.HARVEST_BUCKETS + ("early_half_mfe", "early_consec")
MIN_CELL_N = 15                          # 协议 §4:单格 n<15 ⇒ INSUFFICIENT
TIER_CLAUSES = ("pos_edge", "room_mid", "room_lo", "transition_fade",
                "rngq_big", "pmd_adverse")
EXPOSURE_NOTE = ("test 段第 22 次评估曝光(research-log #35 预注册,一次性;"
                 "仅 arm-T 与 arm-X 同一轮次同次跑齐 1/1.5/2x 三格;arm-C "
                 "test 数字只读引用在册 reports/v17_arm-C.json(第 20 次曝光"
                 "产物),不重复曝光;maker/stop 对照臂只落 dev,不构成 test "
                 "曝光;此后 v19 只许前向)")


# ---------------------------------------------------------------------------
# 配置(parent=v18 逐键硬对账;v19_params/臂开关冻结复核)
# ---------------------------------------------------------------------------


def load_config() -> dict:
    v19 = json.loads(CONFIG_V19.read_text(encoding="utf-8"))
    v18 = json.loads(CONFIG_V18.read_text(encoding="utf-8"))
    allow_diff = {"version", "note", "meta", "arms", "arm", "gates_model",
                  "portfolio_cap_frac", "v19_params",
                  "v16_params", "v18_params"}   # 数值面下方单独深检(note 叙述)
    for k in set(v19) | set(v18):
        if k in allow_diff:
            continue
        if v19.get(k) != v18.get(k):
            raise RuntimeError(f"config/v19.json parent=v18 漂移 {k}: "
                               f"{v19.get(k)} != {v18.get(k)}")
    for pk in ("v16_params", "v18_params"):
        p19 = {k: v for k, v in v19[pk].items() if k != "note"}
        p18 = {k: v for k, v in v18[pk].items() if k != "note"}
        if p19 != p18:
            raise RuntimeError(f"config/v19.json {pk} 数值面 != config/v18.json"
                               "(链先验不许碰;note 为叙述)")
    if float(v19["portfolio_cap_frac"]) != CAP_PRODUCT:
        raise RuntimeError(f"config/v19.json portfolio_cap_frac 必须 "
                           f"{CAP_PRODUCT}(裁决①): {v19['portfolio_cap_frac']}")
    if v19["gates_model"] != "tier_sizing":
        raise RuntimeError("config/v19.json gates_model 必须 tier_sizing"
                           f": {v19['gates_model']}")
    if v19["arm"] != "v19":
        raise RuntimeError("config/v19.json arm 必须 v19")
    rv17.tf_of(v19["tf_list"])
    if float(v19["equity"]) != EQUITY0:
        raise RuntimeError(f"config/v19.json equity 必须 {EQUITY0}: {v19['equity']}")
    tf_map = {int(k): int(v) for k, v in v19["trigger_tf_map"].items()}
    if tf_map != TRIGGER_TF_MAP:
        raise RuntimeError("config/v19.json trigger_tf_map != 冻结级联映射")
    if set(v19["arms"]) != set(ARM_IDS):
        raise RuntimeError(f"config/v19.json arms 必须恰为 {ARM_IDS}")
    expect = {"arm-T": ("market", False), "arm-X": ("market", True),
              "arm-maker": ("maker", True), "arm-stop": ("stop_entry", True)}
    for a, (ex, early) in expect.items():
        spec = v19["arms"][a]
        if not (spec["tiered"] and spec["early_exit"] is early
                and spec["exec"] == ex):
            raise RuntimeError(f"config/v19.json {a} 臂开关漂移: {spec}")
        if spec["mic_family"] != list(STOP_FAMILY):
            raise RuntimeError(f"config/v19.json {a} mic 拓扑必须沿 arm-C")
        if float(spec["equity"]) != EQUITY0:
            raise RuntimeError(f"config/v19.json {a} equity 必须 {EQUITY0}")
    tp = {k: v for k, v in v19["v19_params"].items() if k != "note"}
    skip = {"fade_front_edge", "fade_family",          # fade_family 下方硬查
            "rngq_window", "rngq_max", "pmd_window", "pmd_min"}  # 照链(v18_params)
    expect_tp = {f.name: getattr(TieredParams(), f.name)
                 for f in TieredParams.__dataclass_fields__.values()
                 if f.name not in skip}
    expect_tp.update({"early_half_mfe_r": EARLY_HALF_MFE_R,
                      "early_consec_n": EARLY_CONSEC_N})
    for k, v in expect_tp.items():
        if tp.get(k) != v:
            raise RuntimeError(f"config/v19.json v19_params 冻结先验漂移 "
                               f"{k}: {tp.get(k)} != {v}")
    if tuple(v19["v16_params"]["fade_family"]) != FADE_FAMILY \
            or tuple(v19["v16_params"]["stop_family"]) != STOP_FAMILY:
        raise RuntimeError("config/v19.json 家族表漂移(协议写死)")
    return v19


# ---------------------------------------------------------------------------
# 臂定义 + 回放驱动
# ---------------------------------------------------------------------------


def _v19_params(rp_base: RouterParams, tp: TieredParams) -> V19Params:
    return V19Params(**{f: getattr(rp_base, f)
                        for f in RouterParams.__dataclass_fields__}, tiered=tp)


def build_arm(raw_cfg: dict, arm_id: str) -> tuple:
    """臂 → (TieredParams, V19Router, ArmConfig, mic_families)。

    全臂:收割婚配/时间帽/结构锚沿链;mic 拓扑 = STOP_FAMILY(arm-C 语义);
    差异 = 执行(exec:market 默认 / maker fade 前缘 / stop_entry 续涨预埋)
    与夭折开关(arm-T 关,其余开)。
    """
    p16 = raw_cfg["v16_params"]
    p19 = {k: v for k, v in raw_cfg["v19_params"].items() if k != "note"}
    spec = raw_cfg["arms"][arm_id]
    ex = spec["exec"]
    tp = TieredParams(
        **{k: p19[k] for k in ("pos_short_min", "pos_long_max", "k_room_R",
                               "room_lo", "k_is", "k_star", "mult_pos",
                               "mult_room_mid", "mult_room_lo",
                               "mult_transition_fade", "mult_rngq_big",
                               "mult_pmd_adverse")},
        fade_family=tuple(p16["fade_family"]),
        fade_front_edge=(ex == "maker"))
    arm = ArmConfig(fill_model=("exchange_maker" if ex == "maker"
                                else "canonical"),
                    stop_entry=(ex == "stop_entry"),
                    stop_wait_tf=int(p16["stop_wait_tf"]),
                    k_bm=float(p16["k_bm"]),
                    c2_family=(tuple(p16["fade_family"]) if ex == "maker"
                               else None),
                    harvest_family=tuple(p16["fade_family"]),
                    time_cap_bars=int(p16["k_cap_hours"]),
                    cap_mfe_min=float(p16["cap_mfe_min"]),
                    early_exit=bool(spec["early_exit"]))
    return tp, V19Router, arm, STOP_FAMILY


def build_arm_c_probe(raw_cfg: dict) -> tuple:
    """arm-C 锚探针(v17 arm-C 语义逐位:V16Router + v16 ArmConfig;early 关)。

    容量 0.03 由锚探针 cfg 覆盖承担(main 侧 anchor_cfg),与 v18 arm-C 同型。
    """
    p = raw_cfg["v16_params"]
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
                    cap_mfe_min=float(p["cap_mfe_min"]))
    return hp, V16Router, arm, STOP_FAMILY


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
            res.trades = augment_trades_v19(res.trades, frames, res.ledger)
        m = compute_metrics(res)
        out[mult] = (res, m)
        c = res.counters
        print(f"      [bt] {tag}{segment}@{mult}x"
              f"{' mic' if enable_mic else ' bar'}: net={m['net_pnl']:+.2f} "
              f"n={m['n_trades']} "
              f"pf={m['profit_factor'] and round(m['profit_factor'], 3)} "
              f"tier={c.get('v19_tier_a', 0)}/{c.get('v19_tier_b', 0)} "
              f"禁={c.get('v19_tier_reject_f1', 0)}/"
              f"{c.get('v19_tier_reject_target_missing', 0)} "
              f"cap={c.get('portfolio_cap_skips', 0)} "
              f"({time.time() - t0:.0f}s)", flush=True)
    liq = sum(res.counters["liquidations"] for res, _ in out.values())
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")
    return out


# ---------------------------------------------------------------------------
# 逐笔增广(v19 = v17 增广 + v19 配注溯源列)
# ---------------------------------------------------------------------------


def augment_trades_v19(trades: list, frames: dict, ledger: list) -> list:
    """rv17.augment_trades_v17 + v19 增量:v19_tier/v19_clauses/v19_pos/
    v19_room_R/v19_rngq/v19_pmd 溯源(决策 meta 通道,不上数值路径);
    harvest_bucket 由 v16 解析承担(early|half_mfe / early|consec 经
    reason 机读标签落桶)。
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
        out.append({**t,
                    "v19_tier": m.get("v19_tier"),
                    "v19_clauses": ",".join(m.get("v19_clauses") or ()),
                    "v19_pos": m.get("v19_pos"),
                    "v19_room_R": m.get("v19_room_R"),
                    "v19_rngq": m.get("v19_rngq"),
                    "v19_pmd": m.get("v19_pmd")})
    return out


COLUMNS_V19 = list(rb.TRADE_COLUMNS) + [
    "order_type", "stop_ref", "v19_tier", "size_mult", "v19_clauses",
    "v19_pos", "v19_room_R", "struct_invalid", "harvest_target",
    "harvest_bucket", "signal_decision_ts", "mic", "cog_index",
    "advance_minutes", "v19_rngq", "v19_pmd",
]


def trades_frame_v19(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": rb._iso(t["open_decision_ts"]),
                     "open_iso": rb._iso(t["open_ts"]),
                     "close_decision_iso": rb._iso(t["close_decision_ts"]),
                     "exit_iso": rb._iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=COLUMNS_V19)


# ---------------------------------------------------------------------------
# 条款生效统计 / 收割桶 / 期望矩阵 / 容量拒绝率
# ---------------------------------------------------------------------------


def clause_stats_v19(counters: dict) -> dict:
    base = rv16.clause_stats(counters)
    g = counters.get
    base.update({
        "v19_tier_evaluated": g("v19_tier_evaluated", 0),
        "v19_tier_reject_f1": g("v19_tier_reject_f1", 0),
        "v19_tier_reject_target_missing": g("v19_tier_reject_target_missing", 0),
        "v19_tier_reject_no_invalid": g("v19_tier_reject_no_invalid", 0),
        "v19_tier_reject_no_zone": g("v19_tier_reject_no_zone", 0),
        **{f"v19_clause_{c}": g(f"v19_clause_{c}", 0) for c in TIER_CLAUSES},
        "v19_tier_a": g("v19_tier_a", 0),
        "v19_tier_b": g("v19_tier_b", 0),
    })
    return base


def harvest_buckets_v19(trades: list) -> dict:
    """收割桶(v16 六桶 + early|half_mfe / early|consec;段内开仓平仓笔)。"""
    nets: dict = {b: [] for b in HARVEST_BUCKETS_V19}
    for t in trades:
        if not t["in_segment_open"] or not t["closed"]:
            continue
        nets[t.get("harvest_bucket") or "other"].append(float(t["net_pnl"]))
    return {b: rv16._cell_stats(nets[b]) for b in HARVEST_BUCKETS_V19
            if nets[b]}


def expectation_matrix(trades: list) -> dict:
    """验收矩阵:事件×TF×方向 → n/净/期望(n<15 ⇒ INSUFFICIENT,不给叙述)。"""
    cells: dict = {}
    for t in trades:
        if not t["in_segment_open"]:
            continue
        k = (str(t["event_id"]), str(t["tf"]),
             "long" if t["direction"] > 0 else "short")
        c = cells.setdefault(k, {"n": 0, "net": 0.0})
        c["n"] += 1
        c["net"] += float(t["net_pnl"])
    out: dict = {}
    for (ev, tf, side), c in sorted(cells.items()):
        exp = c["net"] / c["n"] if c["n"] else None
        out.setdefault(ev, {}).setdefault(tf, {})[side] = {
            "n": c["n"], "net_pnl": c["net"], "expectancy_usdt": exp,
            "status": ("INSUFFICIENT" if c["n"] < MIN_CELL_N else "EVAL"),
            "positive": (exp is not None and exp > 0.0)}
    return out


def family_tf_matrix(trades: list) -> dict:
    """家族×TF 辅助矩阵(方向合并;fade/continuation/other)。"""
    cells: dict = {}
    for t in trades:
        if not t["in_segment_open"]:
            continue
        k = (rv16._family_of(t.get("event_id")), str(t["tf"]))
        c = cells.setdefault(k, {"n": 0, "net": 0.0})
        c["n"] += 1
        c["net"] += float(t["net_pnl"])
    out: dict = {}
    for (fam, tf), c in sorted(cells.items()):
        exp = c["net"] / c["n"] if c["n"] else None
        out.setdefault(fam, {})[tf] = {
            "n": c["n"], "net_pnl": c["net"], "expectancy_usdt": exp,
            "status": ("INSUFFICIENT" if c["n"] < MIN_CELL_N else "EVAL"),
            "positive": (exp is not None and exp > 0.0)}
    return out


def matrix_ratio(mat: dict) -> dict:
    """正期望格占比(仅 EVAL 格(n≥15)计入分母;n<15 记 INSUFFICIENT)。

    兼容两层(家族×TF)与三层(事件×TF×方向)嵌套:叶格以 "status" 键识别。
    """
    flat: list = []
    def walk(node):
        if isinstance(node, dict) and "status" in node:
            flat.append(node)
            return
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
    walk(mat)
    eval_cells = [c for c in flat if c["status"] == "EVAL"]
    pos = [c for c in eval_cells if c["positive"]]
    return {"cells_total": len(flat),
            "cells_eval": len(eval_cells),
            "cells_insufficient": len(flat) - len(eval_cells),
            "cells_positive": len(pos),
            "positive_share": (len(pos) / len(eval_cells))
            if eval_cells else None}


def cap_skip_row(res) -> dict:
    c = res.counters
    skips = c.get("portfolio_cap_skips", 0)
    intents = c.get("open_intents", 0)
    return {"portfolio_cap_skips": skips, "open_intents": intents,
            "cap_skip_rate": (skips / (skips + intents))
            if (skips + intents) else None}


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
        try:                                   # 以 ? 替代(报告 utf-8 不受影响)
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    raw_cfg = load_config()
    params = Params.from_json(CONFIG_V19)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = RouterParams(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v19 parent=v18:merge_max_atr 必须 2.0")
    # 锚探针 cfg:容量 0.03(v17 在册口径;产品臂 0.08 不动,DCF)
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

    print("[prep] v19 战役帧构建(v17 帧装载复用;认知一次性)", flush=True)
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
                        for f in RouterParams.__dataclass_fields__}, v16=hp_c)
    arm_c_grid = run_grid(frames, anchor_cfg, params, rp_c, factory_c, arm_c,
                          contracts, segment="dev", equity0=EQUITY0,
                          mic_families=mic_c, enable_mic=True, tag="arm-C ")
    parity_baseline = []
    for mult in COST_MULTS:
        m = arm_c_grid[mult][1]
        fm = v17_c_report["runs"][f"arm-C|dev@{mult}x"]["metrics"]
        ok = all(m[k] == fm[k] for k in METRIC_EQ)
        parity_baseline.append({"run": f"arm-C|dev@{mult}x",
                                **{f"v19_{k}": m[k] for k in METRIC_EQ},
                                **{f"v17_{k}": fm[k] for k in METRIC_EQ},
                                "parity": ok})
        if not ok:
            raise RuntimeError(f"基线锚不一致 arm-C|dev@{mult}x: "
                               f"{parity_baseline[-1]}")
    print("[anchor-3] arm-C 3 格 == v17 在册 arm-C(逐格全等)", flush=True)

    # -- dev 主网格:四臂 equity=1000,cap 0.08,三格/臂 --------------------------------
    grids: dict = {}
    for arm_id in ARM_IDS:
        tp, factory, arm, mic_families = build_arm(raw_cfg, arm_id)
        rp_use = _v19_params(rp_base, tp)
        print(f"[arm] {arm_id}: {raw_cfg['arms'][arm_id]['note']}", flush=True)
        grids[arm_id] = {"dev": run_grid(
            frames, raw_cfg, params, rp_use, factory, arm, contracts,
            segment="dev", equity0=EQUITY0, mic_families=mic_families,
            enable_mic=True, tag=f"{arm_id} ")}

    # -- test 一次(第 22 次曝光;仅 arm-T 与 arm-X) --------------------------------------
    print(f"[test] 第 22 次曝光:{TEST_ARMS}(一次性三格;此后只许前向)", flush=True)
    for arm_id in TEST_ARMS:
        tp, factory, arm, mic_families = build_arm(raw_cfg, arm_id)
        rp_use = _v19_params(rp_base, tp)
        grids[arm_id]["test"] = run_grid(
            frames, raw_cfg, params, rp_use, factory, arm, contracts,
            segment="test", equity0=EQUITY0, mic_families=mic_families,
            enable_mic=True, tag=f"{arm_id} ")

    # -- 归因装配 ------------------------------------------------------------------
    def exp(a, seg, mult):
        return grids[a][seg][mult][1]["expectancy_usdt"]

    def cell_attr(arm_id, seg, mult):
        res, m = grids[arm_id][seg][mult]
        mat = expectation_matrix(res.trades)
        fam_tf = family_tf_matrix(res.trades)
        return {
            "metrics": m, "counters": res.counters,
            "clause": clause_stats_v19(res.counters),
            "harvest": harvest_buckets_v19(res.trades),
            "family": rv16.family_buckets(res.trades),
            "matrix": mat, "matrix_ratio": matrix_ratio(mat),
            "family_tf": fam_tf, "family_tf_ratio": matrix_ratio(fam_tf),
            "micro_segment": rv17.mic_entry_share(res.trades),
            "cap": cap_skip_row(res),
            "giveback": rb.giveback_summary(res.trades),
            "buckets": rb.bucket_tables(res.trades),
        }

    attr: dict = {a: {} for a in ARM_IDS}
    for a in ARM_IDS:
        for mult in COST_MULTS:
            attr[a][f"dev@{mult}x"] = cell_attr(a, "dev", mult)
    for a in TEST_ARMS:
        for mult in COST_MULTS:
            attr[a][f"test@{mult}x"] = cell_attr(a, "test", mult)
    attr["arm-C"] = {f"dev@{mult}x": {
        "metrics": arm_c_grid[mult][1],
        "cap": cap_skip_row(arm_c_grid[mult][0]),
        "clause": rv16.clause_stats(arm_c_grid[mult][0].counters)}
        for mult in COST_MULTS}

    # -- 组件裁决(dev 主评 @1.5x;孤立 Δ;tie 孤岛不打包) --------------------------------
    eT, eX = exp("arm-T", "dev", JUDGE_COST), exp("arm-X", "dev", JUDGE_COST)
    eM2, eS = exp("arm-maker", "dev", JUDGE_COST), exp("arm-stop", "dev", JUDGE_COST)

    def dv(a, base):
        ea, eb = exp(a, "dev", JUDGE_COST), exp(base, "dev", JUDGE_COST)
        return {
            "component": a, "baseline": base,
            "expectancy_usdt_dev_1_5x": ea,
            "expectancy_usdt_baseline": eb,
            "delta_expectancy_usdt": (ea - eb)
            if ea is not None and eb is not None else None,
            "delta_net_pnl": (grids[a]["dev"][JUDGE_COST][1]["net_pnl"]
                              - grids[base]["dev"][JUDGE_COST][1]["net_pnl"]),
        }

    component_verdicts = {
        "early_exit_isolation": {**dv("arm-X", "arm-T"),
                                 "reading": "夭折处置孤立差(arm-X − arm-T)"},
        "maker_isolation": {**dv("arm-maker", "arm-X"),
                            "reading": "fade maker 执行差孤立差(arm-maker − arm-X)"},
        "stop_entry_isolation": {**dv("arm-stop", "arm-X"),
                                 "reading": "续涨 stop-entry 执行差孤立差"
                                           "(arm-stop − arm-X)"},
    }

    # -- test 一次判定(升级线逐条 + CI;arm-C 引用在册) --------------------------------------
    test_once: dict = {}
    for a in TEST_ARMS:
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
            "doc": "docs/v19-protocol.md(冻结,2026-10-20)",
            "pre_registration": "research-log #35(先于运行)",
            "owner_rulings": ("所有者五条裁决(冻结,不谈判):①组合 3%→8%;②三闸改"
                              "阶梯配注;③负交易夭折处置组件;④默认市价(maker/"
                              "stop_entry 降级为臂);⑤按家族×TF 期望矩阵验收"),
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "parity_discipline": ("三锚硬断言:FROZEN_ERA 探针(cap 0.03)== v6 冻结"
                                  "产物逐格全等(代码零回归);16 档 bar 回放(cap "
                                  "0.03)== v13 在册 bar 臂逐格全等(engine 零回归);"
                                  "arm-C(v17 语义,cap 0.03)dev 三格 == 在册 v17 "
                                  "arm-C 逐格全等(基线语义锚)。不等即 raise,报告"
                                  "不落地(本轮已通过)"),
            "test_eval_declaration": EXPOSURE_NOTE,
            "judge_rule": ("dev 主评 @1.5x:组件裁决 = Δ(arm-X − arm-T)孤立夭折、"
                           "Δ(arm-maker/arm-stop − arm-X)孤立执行价差;验收矩阵"
                           "(事件×TF×方向,n<15 ⇒ INSUFFICIENT)首页照 §4;死亡名"
                           "单(R4 层级兑现/打包五问)不回锅"),
            "upgrade_line": UPGRADE_LINE,
            "cascade_declarations": prep["declarations"],
            "honesty_note": ("硬禁只剩 F1 与结构性 target 不存在(no_invalid/"
                             "no_zone 为沿链 fail-closed 通道,逐键计数);对面缘缺"
                             "失 ⇒ 拒(原 v16 闸②'空间无界 ⇒ 过'反转,续涨/TREND_RESUMED/RANGE_ROTATED "
                             "同受,如实声明);fail-closed 通道与 fail-open 条款"
                             "(rngq/pmd 不可判不乘,同 v18 声明)分记;顺风 A/B 双档"
                             "在 tier_sizing 下退位(q4 只注);'认微帧'按 §8 裁决"
                             "粒度落在认知帧解读(微帧粒度离场不在 §8 既有机制);"
                             "组合容量决策账未乘档(保守,同 v14/v16/v17 声明);检"
                             "测/费用/杠杆/单笔风险/历史产物/research-log 零改动;"
                             "不渲染任何转正叙述。"),
        },
        "config": {
            "config_file": "config/v19.json",
            "parent": "config/v18.json(逐键硬对账,run 前通过)",
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
        "component_verdicts": component_verdicts,
        "cap_skip_rates": {
            "note": "组合拒绝率变化(裁决① 3%→8%):arm-C(0.03 锚探针)== 在册"
                    "v17 arm-C 重放 vs 产品臂(0.08);rate = skips/(skips+"
                    "open_intents)(决策时刻口径)",
            **{f"arm-C(0.03)|dev@{m}x": attr["arm-C"][f"dev@{m}x"]["cap"]
               for m in COST_MULTS},
            **{f"{a}(0.08)|dev@{m}x": attr[a][f"dev@{m}x"]["cap"]
               for a in ARM_IDS for m in (JUDGE_COST,)},
        },
        "acceptance_front": {
            "matrix_arm_X_dev_1_5x": attr["arm-X"][f"dev@{JUDGE_COST}x"]["matrix"],
            "ratio_arm_X_dev_1_5x": attr["arm-X"][f"dev@{JUDGE_COST}x"]["matrix_ratio"],
            "family_tf_ratio_arm_X_dev_1_5x":
                attr["arm-X"][f"dev@{JUDGE_COST}x"]["family_tf_ratio"],
        },
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
    for a in TEST_ARMS:
        for mult in COST_MULTS:
            payload["runs"][f"{a}|test@{mult}x"] = attr[a][f"test@{mult}x"]
    for mult in COST_MULTS:
        payload["runs"][f"arm-C(0.03锚探针)|dev@{mult}x"] = attr["arm-C"][f"dev@{mult}x"]

    (REPORTS / "v19.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "v19.md").write_text(
        render_md(payload, grids, attr), encoding="utf-8")
    for a in ARM_IDS:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v19(grids[a]["dev"][mult][0].trades).to_csv(
                REPORTS / f"trades_v19_{a}__dev{suffix}.csv", index=False)
    for a in TEST_ARMS:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v19(grids[a]["test"][mult][0].trades).to_csv(
                REPORTS / f"trades_v19_{a}__test{suffix}.csv", index=False)
    print(f"-> reports/v19.json/.md + trades_v19 CSVs "
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


def _bucket_label(b: str) -> str:
    return {"early_half_mfe": "early|half_mfe",
            "early_consec": "early|consec"}.get(b, b)


def render_matrix(mat: dict, ratio: dict) -> list:
    lines = ["| 事件 | TF | 方向 | n | 净盈亏 | 期望USDT | 判定 |",
             "|---|---|---|---|---|---|---|"]
    any_rows = False
    for ev in ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED", "STRUCTURE_BROKEN", "FLIP_RETESTED", "EDGE_RECLAIMED"):
        if ev not in mat:
            continue
        for tf in TF_LIST:
            for side in ("short", "long"):
                c = mat[ev].get(tf, {}).get(side)
                if c is None:
                    continue
                any_rows = True
                lines.append(f"| {ev} | {tf} | {side} | {c['n']} "
                             f"| {_f(c['net_pnl'])} "
                             f"| {_f(c['expectancy_usdt'], 4)} "
                             f"| {c['status']} |")
    if not any_rows:
        lines.append("| — | — | — | 0 | — | — | — |")
    lines += ["",
              f"> 正期望格占比(仅 EVAL 格 n≥{MIN_CELL_N}):"
              f" {ratio['cells_positive']}/{ratio['cells_eval']} = "
              f"{_f(ratio['positive_share'], 4)};INSUFFICIENT 格 "
              f"{ratio['cells_insufficient']}(不给叙述);总格 "
              f"{ratio['cells_total']}。", ""]
    return lines


def render_md(payload: dict, grids: dict, attr: dict) -> str:
    front = payload["acceptance_front"]
    lines = [
        "# v19 阶梯配注 + 组合 8% + 夭折处置 + 市价默认 统一回测报告"
        "(判定表首页=期望矩阵;dev 主评 + test 一次)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明", "",
        f"- {payload['protocol']['owner_rulings']}",
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
        "## 1. 判定表首页:事件 × TF × 方向 期望矩阵(旗舰 arm-X,dev@1.5x;"
        "目标每格期望 ≥0)", ""]
    lines += render_matrix(front["matrix_arm_X_dev_1_5x"],
                           front["ratio_arm_X_dev_1_5x"])

    lines += ["### 1a. 家族 × TF 辅助矩阵(arm-X,dev@1.5x;方向合并)", "",
              "| 家族 | TF | n | 净盈亏 | 期望USDT | 判定 |",
              "|---|---|---|---|---|---|"]
    fam_tf = attr["arm-X"][f"dev@{JUDGE_COST}x"]["family_tf"]
    for fam in ("fade", "continuation", "other"):
        for tf in TF_LIST:
            c = fam_tf.get(fam, {}).get(tf)
            if c is None:
                continue
            lines.append(f"| {fam} | {tf} | {c['n']} | {_f(c['net_pnl'])} "
                         f"| {_f(c['expectancy_usdt'], 4)} | {c['status']} |")
    fr = front["family_tf_ratio_arm_X_dev_1_5x"]
    lines += ["", f"> 家族×TF 正期望格占比(EVAL): {fr['cells_positive']}/"
                  f"{fr['cells_eval']} = {_f(fr['positive_share'], 4)}"
                  f"(INSUFFICIENT {fr['cells_insufficient']}).", ""]

    lines += ["## 2. 收割桶与条款生效统计(旗舰 arm-X dev@1.5x;含夭折桶)", "",
              "| 桶 | 笔数 | 净额 | 胜率 | PF |", "|---|---|---|---|---|"]
    hb = attr["arm-X"][f"dev@{JUDGE_COST}x"]["harvest"]
    for b in HARVEST_BUCKETS_V19:
        if b in hb:
            c = hb[b]
            lines.append(f"| 收割:{_bucket_label(b)} | {c['n_trades']} "
                         f"| {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
                         f"| {_f(c['profit_factor'])} |")
    fb = attr["arm-X"][f"dev@{JUDGE_COST}x"]["family"]
    for fam in ("fade", "continuation", "other"):
        if fam in fb:
            c = fb[fam]
            lines.append(f"| 家族:{fam} | {c['n_trades']} "
                         f"| {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
                         f"| {_f(c['profit_factor'])} |")
    cl = attr["arm-X"][f"dev@{JUDGE_COST}x"]["clause"]
    lines += ["",
              f"- 配注评估 {cl['v19_tier_evaluated']}:满档 A {cl['v19_tier_a']} "
              f"/ 减档 B {cl['v19_tier_b']};条款命中 带沿失位="
              f"{cl['v19_clause_pos_edge']} room中档={cl['v19_clause_room_mid']} "
              f"room低档={cl['v19_clause_room_lo']} TRANSITION fade="
              f"{cl['v19_clause_transition_fade']} 大蛋糕="
              f"{cl['v19_clause_rngq_big']} 逆三拍={cl['v19_clause_pmd_adverse']}",
              f"- 硬禁:F1={cl['v19_tier_reject_f1']} target_missing="
              f"{cl['v19_tier_reject_target_missing']} ;fail-closed 通道 "
              f"no_invalid={cl['v19_tier_reject_no_invalid']} no_zone="
              f"{cl['v19_tier_reject_no_zone']};容量跳 "
              f"{cl['portfolio_cap_skips']}", ""]

    lines += ["## 3. 判定总览(四臂 dev 三格;arm-C 为 0.03 锚探针参照行)", "",
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

    lines += ["## 4. 组件裁决(dev@1.5x,孤立 Δ;tie 孤岛不打包)", "",
              "| 组件 | 对照基线 | Δ净期望USDT | Δ净盈亏 | 判读 |",
              "|---|---|---|---|---|"]
    for k in ("early_exit_isolation", "maker_isolation", "stop_entry_isolation"):
        cv = payload["component_verdicts"][k]
        lines.append(f"| {cv['component']} | {cv['baseline']} "
                     f"| {_f(cv['delta_expectancy_usdt'], 4)} "
                     f"| {_f(cv['delta_net_pnl'])} | {cv['reading']} |")
    lines.append("")

    csr = payload["cap_skip_rates"]
    lines += ["## 5. 组合容量 3%→8% 拒绝率变化(裁决①;dev 决策时刻口径)", "",
              "| 臂 | 格 | 容量跳 | open_intents | 拒绝率 |",
              "|---|---|---|---|---|"]
    for k, c in csr.items():
        if k == "note":
            continue
        lines.append(f"| {k} | — | {c['portfolio_cap_skips']} "
                     f"| {c['open_intents']} | {_f(c['cap_skip_rate'], 4)} |")
    lines += ["", f"> {csr['note']}。", ""]

    to = payload["test_once_exposure"]
    lines += ["## 6. test 一次(第 22 次曝光;如实写 dev/test 对照,"
              "不渲染转正叙述)", "", f"- {to['note']}", ""]
    seg_labels = (("dev_1x", "dev@1x"), ("dev_1_5x", "dev@1.5x"),
                  ("dev_2x", "dev@2x"), ("test_1x", "test@1x"),
                  ("test_1_5x", "test@1.5x"), ("test_2x", "test@2x"))
    for a in TEST_ARMS:
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

    lines += ["## 7. 事件 × 家族归因(dev@1.5x;机制 × TF × 方向,四臂)", ""]
    for a in ARM_IDS:
        bt = attr[a][f"dev@{JUDGE_COST}x"]["buckets"]
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

    lines += ["## 8. 已知近似与如实声明", "",
              "① 阶梯配注硬禁恰两条:F1(<0.8·ATR 失效)与结构性 target 不存在"
              "(对面结构缘缺失,no_invalid/no_zone 为沿链 fail-closed 通道,"
              "逐项计数);原闸②'缘缺失 ⇒ 空间无界 ⇒ 过'按协议反转为拒,续涨/"
              "TREND_RESUMED/RANGE_ROTATED 同受,如实声明;② 条款恰界:pos 边界 inclusive(≥0.35 空"
              "/≤0.65 多过,恰界不扣);room 恰 1.0 入中档×0.75、恰 1.5 不扣;"
              "rngq 恰 0.8 受理(>0.8 ×0.75,'mic-only 处' = trigger 收盘决策,"
              "判定用注入 ctx 的 mic 标志);pmd 恰 −0.003 计档 ×0.5(恰界侧与"
              " v18 R2 拒界同侧);③ 条款不可判(rngq/pmd 缺 ctx、pos 摆带缺侧)"
              "口径:rngq/pmd 缺 ⇒ 不乘(fail-open,同 v18 声明),pos 缺 ⇒ 按"
              "失位 ×0.5(谨慎);④ 夭折处置判读'(认微帧)'按 §8 裁决粒度落认"
              "知帧:逐认知收盘评估,半窗 = bars_held ≥ 24(k_cap/2)且 MFE < "
              "0.3R(恰 0.3R 持有),逆根 = d·(c−o)<0 连续 ≥3(恰 0 收平复位);"
              "半窗先于逆根,结构与收割条款先行(先触发先算);微帧粒度离场不"
              "在 §8 既有机制,如实声明;⑤ 执行对照臂 = v16 C2/C3 语义逐项照"
              "抄(fade 前缘 maker N_fill=12;续涨 stop 预埋超时 12×TF),市场"
              "臂 fill_model=canonical 全落 canonical 主路(= arm=None 数"
              "值语义,仅收割婚配/时间帽/夭折接线存在);⑥ 组合容量决策账未"
              "乘档(保守,同 v14/v16/v17 声明);⑧% 对产品臂、3% 仅锚探针"
              "(容量覆盖只在锚与 v17 对照用);⑦ 顺风 A/B 双档在 tier_sizing"
              "下退位(q4 只作注记,q3 同 v16 只注);死亡名单:R4 层级兑现不"
              "接线、打包五问不复活;⑧ arm-C test 数字只读引用在册 v17 报告"
              "(第 20 次曝光),不重复曝光;test 曝光纪律见 §6;⑨ 检测/费用"
              "/杠杆/单笔风险/历史产物/research-log 零改动;不渲染任何转正"
              "叙述。",
              "",
              "机器可读全量:`reports/v19.json`;逐笔:"
              "`reports/trades_v19_arm-{T,X,maker,stop}__dev[_cost*].csv`"
              "、`reports/trades_v19_arm-{T,X}__test[_cost*].csv`(增列 "
              "v19_tier/v19_clauses/v19_pos/v19_room_R/v19_rngq/v19_pmd;"
              "收割桶含 early|half_mfe/early|consec)。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
