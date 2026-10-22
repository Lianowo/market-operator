"""v16 收割婚配 + 位置/空间/F1 三闸集成回测(docs/v16-protocol.md,冻结
2026-10-20):三闸(缺一不存在交易)+ 顺风档(A/B)+ 家族执行差分(fade→
maker 前缘真挂单,续涨→stop-entry 预埋)+ 一生一表收割(fade 达对面结构
缘全平、续涨 turtle 保留、时间帽 48×TF 且 MFE<0.5R 才出),dev 主评 +
test 一次(test 第 19 次曝光,research-log #30)。

用法:  python scripts/run_v16.py
配置:  config/v16.json(parent=config/canonical.json,共享键逐项照抄;
       arms/v16_params 为 v16 新增,协议写死,不许调)
产物:  reports/v16.json / v16.md
       reports/trades_v16_{arm0,v16}__dev[_cost1_5,_cost2_0].csv
       reports/trades_v16_v16__test[_cost1_5,_cost2_0].csv

口径与纪律:
- arm0 = 现 canonical(全部组件关),同 v14/v15 双钉:① FROZEN_ERA 名单
  (8 档,与 reports/replay_turtle.json 自述 config 一致)arm=None 探针
  dev 三格须与冻结产物逐格全等(代码零回归硬断言);② arm0(现行 16 档)dev
  三格须与 reports/backtest_v13_cascade.json 在册 bar 臂逐格全等(Engine
  零回归硬断言)。不等即 raise,不写产物。
- 判定(协议 §判定,预注册,不打包宣胜):对照基线 = arm0(同段同成本,dev
  三格 + test arm0 引用 v13 在册冻结格);dev@1.5x 净期望(expectancy_usdt)
  v16 − arm0 为主锚,Δ ≤ 0 ⇒ 合成如实打回;若整体仍负 ⇒ Acceptance 最终
  章(协议原文:结构语言家族在本场座最优形态=近打平研究系统),如实填报,
  不产生转正叙述。
- 归因(协议 §判定):合成整体 + 分家族(fade BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED / 续涨 BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED /
  其他 TREND_RESUMED/RANGE_ROTATED)+ 分闸(v16_gate_reject_q1_pos/q2_room/f1)与收割桶(目标/
  CHoCH/trail/失效/时间帽,由持仓侧 reason 机读标签解析,arm0 无标签恒空)。
- test 段评估计数 v16 一次:对 v16 合成臂一次跑齐成本 1/1.5/2x 三格(一次
  落座,同 canonical 各轮口径);样本量 <60 ⇒ INSUFFICIENT_DATA 一如以往如
  实填报;既往升级线(test≥60 笔、1.5x 净期望>0、2x PF>1 ⇒ 才谈 forward
  dry-run)逐条照报,不过线即 Research Only 维持;arm0 test 数字只读引用
  v13 在册 bar 臂冻结产物(arm0==v13 bar 已硬对账),不重复曝光。
- 检测/费用/容量/触发框架零改动;合约规格强制离线缓存;历史产物只读比对
  不覆写。
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
from market_operator.replay import compute_metrics          # noqa: E402
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.campaign import ArmConfig, run_campaigns    # noqa: E402
from market_operator.harvest import (                            # noqa: E402
    HarvestParams,
    V16Params,
    V16Router,
)
from market_operator.routing import RouterParams                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402
from market_operator.research import block_bootstrap_ci          # noqa: E402

CONFIG_V16 = ROOT / "config" / "v16.json"
CONFIG_CANONICAL = ROOT / "config" / "canonical.json"
FROZEN_TURTLE = ROOT / "reports" / "replay_turtle.json"
FROZEN_V13 = ROOT / "reports" / "backtest_v13_cascade.json"
REPORTS = ROOT / "reports"
COST_MULTS = (1.0, 1.5, 2.0)
JUDGE_COST = 1.5                       # dev 主锚成本(协议 §判定 dev@1.5x)
ARM_IDS = ("arm0", "v16")
MIN_TRADES_TEST = 60
FROZEN_ERA_WHITELIST = (("BREAKOUT_ACCEPTED", -1), ("BREAKOUT_ACCEPTED", 1), ("LEVEL_REJECTED", -1), ("LEVEL_REJECTED", 1),
                        ("STRUCTURE_BROKEN", -1), ("STRUCTURE_BROKEN", 1), ("FLIP_RETESTED", -1), ("FLIP_RETESTED", 1))
METRIC_EQ = ("net_pnl", "n_trades", "winrate", "profit_factor")
UPGRADE_LINE = ("既往升级线(v4-v8 不变口径):test≥60 笔 ∧ 1.5x 净期望>0 ∧ "
                "2x PF>1 ⇒ 才谈 forward dry-run;否则 Research Only 维持")
FADE_FAMILY = ("BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED")
STOP_FAMILY = ("BREAKOUT_ACCEPTED", "STRUCTURE_BROKEN", "FLIP_RETESTED")
HARVEST_BUCKETS = ("target", "choch", "trail", "invalid", "time_cap", "other")
FINAL_CHAPTER = ("若整体仍负 ⇒ Acceptance 最终章(协议原文):结构语言家族在"
                 "本场座最优形态=近打平研究系统,不再进化。")


# ---------------------------------------------------------------------------
# 配置/锚
# ---------------------------------------------------------------------------


def load_config() -> dict:
    v16 = json.loads(CONFIG_V16.read_text(encoding="utf-8"))
    canon = json.loads(CONFIG_CANONICAL.read_text(encoding="utf-8"))
    for k, v in canon.items():
        if k in ("version", "meta", "note"):
            continue
        if v16.get(k) != v:
            raise RuntimeError(f"config/v16.json 与 canonical 共享键漂移 {k}")
    if set(v16["arms"]) != set(ARM_IDS):
        raise RuntimeError("config/v16.json arms 必须为 arm0/v16")
    spec = v16["arms"]["v16"]
    if not (spec["v16"] and spec["fill_model"] == "exchange_maker"
            and spec["c3_stop_entry"]):
        raise RuntimeError(f"config/v16.json v16 合成开关漂移: {spec}")
    if v16["arms"]["arm0"]["v16"]:
        raise RuntimeError("config/v16.json arm0 必须全组件关(canonical)")
    p = v16["v16_params"]
    frozen = {("pos_short_min", 0.35), ("pos_long_max", 0.65),
              ("k_room_R", 1.5), ("k_is", 0.8), ("k_star", 2),
              ("tier_a_mult", 1.0), ("tier_b_mult", 0.75),
              ("k_cap_hours", 48), ("cap_mfe_min", 0.5),
              ("stop_wait_tf", 12), ("k_bm", 0.1)}
    for k, expect in frozen:
        if p[k] != expect:
            raise RuntimeError(f"config/v16.json v16_params 冻结先验漂移 {k}")
    if p["fade_family"] != list(FADE_FAMILY) \
            or p["stop_family"] != list(STOP_FAMILY):
        raise RuntimeError("config/v16.json 家族表漂移(协议写死)")
    return v16


def _v16_params(rp_base: RouterParams, hp) -> V16Params:
    """canonical RouterParams 逐项提升 + v16 收割婚配层(合成臂路由参数)。"""
    kw = {f: getattr(rp_base, f) for f in RouterParams.__dataclass_fields__}
    return V16Params(**kw, v16=hp)


def build_arm(raw_cfg: dict, arm_id: str) -> tuple:
    """臂定义 → (harvest|None, router_factory, ArmConfig|None)。

    arm0:(None, None, None)= plain MarketRouter + arm=None ⇒ 逐位 canonical;
    v16:V16Router(三闸+顺风档+fade 前缘改单+结构锚重建)+ ArmConfig(C2
    exchange_maker 仅 fade 家族、C3 续涨 stop-entry、收割婚配持仓侧
    harvest_family=fade、时间帽 48×TF)。
    """
    if arm_id == "arm0":
        return None, None, None
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
    return hp, V16Router, arm


# ---------------------------------------------------------------------------
# 逐笔增广(v16):入场语义补丁 + 收割桶/溯源列(不变 canonical 账目)
# ---------------------------------------------------------------------------


def augment_trades_v16(trades: list, frames: dict, ledger: list) -> list:
    """rb.augment_trades + v16 入账修正:

    - stop_entry 行 meta.entry_mode 覆盖 canonical 二分标签(同 v14/v15;
      arm0 账本无新键 ⇒ 逐位旧值);fade 前缘 maker 保 LIMIT_FILL/limit 本相;
    - order_type / stop_ref / v16_tier / size_mult / v16_pos / v16_room_R /
      struct_invalid / harvest_target 溯源列(C5 meta 通道,不上数值路径);
    - harvest_bucket:出场 reason 机读标签 "v16|<bucket>|" 解析(target /
      choch / trail / invalid / time_cap;无标签 = other 或 None(未平仓));
    - signal_decision_ts = armed_at_ts ∪ open_decision_ts(同 v14/v15 口径,
      硬匹配 v16 vs arm0 同信号用)。
    """
    base = rb.augment_trades(trades, frames)
    meta_by_tid: dict = {}
    for row in ledger:
        if row["trade_id"] is None:
            continue
        if row["kind"] == "OPEN" or (row["kind"] == "LIMIT_FILL"
                                     and row["meta"].get("role") != "reduce"):
            meta_by_tid.setdefault(row["trade_id"], row["meta"])
    out = []
    for t in base:
        m = meta_by_tid.get(t["trade_id"], {})
        if m.get("entry_mode") == "stop_entry":
            t = {**t, "entry_mode": m["entry_mode"],
                 "limit_price": m.get("limit_price")}
        bucket = None
        rx = t.get("reason_exit")
        if isinstance(rx, str) and rx.startswith("v16|"):
            bucket = rx.split("|", 3)[1]
        out.append({**t,
                    "order_type": m.get("order_type"),
                    "stop_ref": m.get("stop_ref"),
                    "v16_tier": m.get("v16_tier"),
                    "size_mult": m.get("size_mult"),
                    "v16_pos": m.get("v16_pos"),
                    "v16_room_R": m.get("v16_room_R"),
                    "struct_invalid": m.get("struct_invalid"),
                    "harvest_target": m.get("harvest_target"),
                    "harvest_bucket": bucket,
                    "signal_decision_ts": int(m.get("armed_at_ts")
                                              or t["open_decision_ts"])})
    return out


COLUMNS_V16 = list(rb.TRADE_COLUMNS) + [
    "order_type", "stop_ref", "v16_tier", "size_mult", "v16_pos",
    "v16_room_R", "struct_invalid", "harvest_target", "harvest_bucket",
    "signal_decision_ts",
]


def trades_frame_v16(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": rb._iso(t["open_decision_ts"]),
                     "open_iso": rb._iso(t["open_ts"]),
                     "close_decision_iso": rb._iso(t["close_decision_ts"]),
                     "exit_iso": rb._iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=COLUMNS_V16)


# ---------------------------------------------------------------------------
# 分桶归因(收割桶 × 家族桶;口径同 compute_metrics 的净额/胜率/PF)
# ---------------------------------------------------------------------------


def _family_of(event_id) -> str:
    if event_id in FADE_FAMILY:
        return "fade"
    if event_id in STOP_FAMILY:
        return "continuation"
    return "other"


def _cell_stats(nets: list) -> dict:
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_w = float(sum(wins))
    gross_l = float(-sum(losses))
    return {"n_trades": len(nets),
            "net_pnl": float(sum(nets)),
            "winrate": (len(wins) / len(nets)) if nets else None,
            "profit_factor": (gross_w / gross_l if gross_l > 0
                              else (float("inf") if gross_w > 0 else None))}


def harvest_buckets(trades: list) -> dict:
    """收割桶桶表(协议 §判定"收割三桶"展开为协议五桶:目标/CHoCH/trail/失
    效/时间帽;arm0 无 v16 标签 ⇒ 全落 other,桶结构保留对照)。段内开仓。"""
    nets: dict = {b: [] for b in HARVEST_BUCKETS}
    for t in trades:
        if not t["in_segment_open"] or not t["closed"]:
            continue
        nets[t.get("harvest_bucket") or "other"].append(float(t["net_pnl"]))
    return {b: _cell_stats(nets[b]) for b in HARVEST_BUCKETS if nets[b]}


def family_buckets(trades: list) -> dict:
    """分家族桶(fade/续涨/其他;段内开仓)。"""
    nets: dict = {"fade": [], "continuation": [], "other": []}
    for t in trades:
        if not t["in_segment_open"] or not t["closed"]:
            continue
        nets[_family_of(t.get("event_id"))].append(float(t["net_pnl"]))
    return {f: _cell_stats(nets[f]) for f in nets if nets[f]}


# ---------------------------------------------------------------------------
# 回放驱动(dev 三格;test 一次三格)
# ---------------------------------------------------------------------------


def run_grid(frames: dict, raw_cfg: dict, params: Params, rp, factory, arm,
             contracts: dict, *, segment: str,
             mults: tuple = COST_MULTS) -> dict:
    broker_kw = dict(dev_frac=float(raw_cfg["dev_frac"]), equity0=300.0,
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
                            **broker_kw)
        res.trades = augment_trades_v16(res.trades, frames, res.ledger)
        m = compute_metrics(res)
        out[mult] = (res, m)
        c = res.counters
        print(f"      [bt] {segment} cost {mult}x: net={m['net_pnl']:+.2f} "
              f"n={m['n_trades']} rej(q1/q2/f1)="
              f"{c.get('v16_gate_reject_q1_pos', 0)}/"
              f"{c.get('v16_gate_reject_q2_room', 0)}/"
              f"{c.get('v16_gate_reject_f1', 0)} ({time.time() - t0:.0f}s)",
              flush=True)
    liq = sum(res.counters["liquidations"] for res, _ in out.values())
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")
    return out


def _cell(m: dict) -> dict:
    return {"net_pnl": m["net_pnl"], "n_trades": m["n_trades"],
            "winrate": m["winrate"], "profit_factor": m["profit_factor"],
            "expectancy_usdt": m["expectancy_usdt"],
            "expectancy_r": m["expectancy_r"]}


def assert_anchor(tag: str, got: dict, frozen_runs: dict, prefix: str) -> list:
    """逐格比对(净/笔数/胜率/PF 全等,不等即 raise);返回 parity 行。"""
    rows = []
    for mult in COST_MULTS:
        m = got[mult][1]
        fm = frozen_runs[f"{prefix}@{mult}x"]["metrics"]
        ok = all(m[k] == fm[k] for k in METRIC_EQ)
        rows.append({"run": f"{prefix}@{mult}x", "seed": tag,
                     **{f"v16_{k}": m[k] for k in METRIC_EQ},
                     **{f"frozen_{k}": fm[k] for k in METRIC_EQ},
                     "parity": ok})
        if not ok:
            raise RuntimeError(f"{tag} 与冻结锚不一致 {prefix}@{mult}x: "
                               f"{rows[-1]}")
    return rows


# ---------------------------------------------------------------------------
# 条款生效统计 / 闸门计数 / 成交率
# ---------------------------------------------------------------------------


def clause_stats(counters: dict) -> dict:
    """每格条款生效统计(缺失键 = 0,arm0 全部 0)。"""
    g = counters.get
    stopex_armed = g("stop_entry_armed", 0)
    maker_armed = g("maker_entry_armed", 0)
    return {
        "v16_gate_evaluated": g("v16_gate_evaluated", 0),
        "v16_gate_reject_q1_pos": g("v16_gate_reject_q1_pos", 0),
        "v16_gate_reject_q2_room": g("v16_gate_reject_q2_room", 0),
        "v16_gate_reject_f1": g("v16_gate_reject_f1", 0),
        "v16_tier_a": g("v16_tier_a", 0),
        "v16_tier_b": g("v16_tier_b", 0),
        "v16_fade_no_target": g("v16_fade_no_target", 0),
        "v16_reject_no_zone": g("v16_reject_no_zone", 0),
        "v16_reject_no_invalid": g("v16_reject_no_invalid", 0),
        "stop_entry_armed": stopex_armed,
        "stop_entry_triggers": g("stop_entry_triggers", 0),
        "stop_entry_fills": g("stop_entry_fills", 0),
        "stop_entry_expired": g("stop_entry_expired", 0),
        "stop_entry_canceled": g("stop_entry_canceled", 0),
        "stopex_fill_rate": (g("stop_entry_fills", 0) / stopex_armed
                             if stopex_armed else None),
        "maker_entry_armed": maker_armed,
        "maker_entry_fills": g("maker_entry_fills", 0),
        "maker_entry_expired": g("maker_entry_expired", 0),
        "maker_entry_canceled": g("maker_entry_canceled", 0),
        "maker_fill_rate": (g("maker_entry_fills", 0) / maker_armed
                            if maker_armed else None),
        "open_intents": g("open_intents", 0),
        "opens_filled": g("opens_filled", 0),
        "limit_fills_entry": g("limit_fills_entry", 0),
        "portfolio_cap_skips": g("portfolio_cap_skips", 0),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    t_start = time.time()
    raw_cfg = load_config()
    params = Params.from_json(CONFIG_V16)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    tf_list = list(raw_cfg["tf_list"])
    if tf_list != ["1h", "2h", "4h", "12h", "1d"]:
        raise RuntimeError(f"config/v16.json tf_list 冻结: {tf_list}")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = RouterParams(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v16 parent=canonical:merge_max_atr 必须 2.0")

    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    print("[prep] 全战役帧(canonical 壳,一次性)", flush=True)
    frames, compaction_report = rb.prepare_frames(raw_cfg, params, tf_list)
    xcheck = rb.cross_checks(frames, float(raw_cfg["k_is"]), merge_max_atr=2.0)

    frozen_turtle = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    frozen_v13 = json.loads(FROZEN_V13.read_text(encoding="utf-8"))

    # -- 锚①:FROZEN_ERA 名单(8 档)探针 == replay_turtle 冻结产物 ---------
    print("[anchor-1] 冻结时代名单探针(dev 三格;arm=None)", flush=True)
    rp_probe = dc_replace(rp_base, whitelist=frozenset(FROZEN_ERA_WHITELIST))
    probe = run_grid(frames, raw_cfg, params, rp_probe, None, None, contracts,
                     segment="dev")
    parity_probe = assert_anchor("FROZEN_ERA 探针", probe,
                                 frozen_turtle["runs"], "turtle|dev")
    print("[anchor-1] 探针 3 格 == canonical v6 臂 B 冻结产物(逐格全等)",
          flush=True)

    # -- arm0 / v16(dev 三格) ----------------------------------------------------
    dev: dict = {}
    for arm_id in ARM_IDS:
        hp, factory, arm = build_arm(raw_cfg, arm_id)
        rp_use = rp_base if arm_id == "arm0" else _v16_params(rp_base, hp)
        print(f"[arm] {arm_id}: {raw_cfg['arms'][arm_id]['note']}", flush=True)
        dev[arm_id] = run_grid(frames, raw_cfg, params, rp_use, factory, arm,
                               contracts, segment="dev")

    # -- 锚②:arm0 == v13 在册 bar 臂(现行 16 档,Engine 零回归) ----------------
    parity_arm0 = assert_anchor("arm0==v13 bar", dev["arm0"],
                                frozen_v13["runs"], "bar|dev")
    print("[anchor-2] arm0 3 格 == v13 在册 bar 臂(逐格全等)", flush=True)

    # -- 合成判定(协议 §判定:dev@1.5x 净期望 v16−arm0;如实打回不打包) -----------
    e0 = dev["arm0"][JUDGE_COST][1]["expectancy_usdt"]
    e1 = dev["v16"][JUDGE_COST][1]["expectancy_usdt"]
    composite_verdict = {
        "base": "arm0", "delta_expectancy_usdt": (e1 - e0)
        if e0 is not None and e1 is not None else None,
        "delta_net_pnl": dev["v16"][JUDGE_COST][1]["net_pnl"]
        - dev["arm0"][JUDGE_COST][1]["net_pnl"],
        "verdict": ("effective(改善)" if (e0 is not None and e1 is not None
                                          and (e1 - e0) > 0.0)
                    else "打回(无改善)"),
        "final_chapter_clause": FINAL_CHAPTER,
        "attribution_note": ("逐组件归因在 v14 arm 矩阵已裁决(log#28);v16 按"
                             "冻结合约整臂评估,不再拆臂;分家族/分闸/收割桶见"
                             " buckets/family/clause 各节。")}

    # -- 条款生效统计 / 分桶归因 ---------------------------------------------------
    clauses = {a: {f"{mult}x": clause_stats(dev[a][mult][0].counters)
                   for mult in COST_MULTS} for a in ARM_IDS}
    givebacks = {a: {f"{mult}x": rb.giveback_summary(dev[a][mult][0].trades)
                     for mult in COST_MULTS} for a in ARM_IDS}
    buckets = {a: {f"{mult}x": rb.bucket_tables(dev[a][mult][0].trades)
                   for mult in COST_MULTS} for a in ARM_IDS}
    harvest = {a: {f"{mult}x": harvest_buckets(dev[a][mult][0].trades)
                   for mult in COST_MULTS} for a in ARM_IDS}
    family = {a: {f"{mult}x": family_buckets(dev[a][mult][0].trades)
                  for mult in COST_MULTS} for a in ARM_IDS}

    # -- test 段一次:v16 合成臂三格(第 19 次曝光,research-log #30) ---------------
    print("[test] v16 合成臂一次(第 19 次曝光,一次性三格;此后只许前向)",
          flush=True)
    hp, factory, arm = build_arm(raw_cfg, "v16")
    rp_use = _v16_params(rp_base, hp)
    test = run_grid(frames, raw_cfg, params, rp_use, factory, arm, contracts,
                    segment="test")
    tr_in = [t for t in test[1.0][0].trades if t["in_segment_open"]]
    if tr_in:
        ci = block_bootstrap_ci(
            np.asarray([t["net_pnl"] for t in tr_in], dtype=np.float64),
            np.asarray([t["open_ts"] for t in tr_in], dtype=np.int64))
    else:
        ci = (float("nan"), float("nan"))
    n_test = test[1.0][1]["n_trades"]
    e15_test = test[1.5][1]["expectancy_usdt"]
    pf2_test = test[2.0][1]["profit_factor"]
    upgrade = {
        "line": UPGRADE_LINE,
        "n_trades_test_1x": n_test,
        "sample_size": ("SUFFICIENT(>=60)" if n_test >= MIN_TRADES_TEST
                        else "INSUFFICIENT_DATA"),
        "expectancy_usdt_test_1_5x": e15_test,
        "expectancy_positive_1_5x": (e15_test is not None and e15_test > 0.0),
        "profit_factor_test_2_0x": pf2_test,
        "pf_gt_1_2_0x": (pf2_test is not None and pf2_test > 1.0),
    }
    upgrade["verdict"] = (
        "过线(可谈 forward dry-run)"
        if (upgrade["sample_size"].startswith("SUFFICIENT")
            and upgrade["expectancy_positive_1_5x"] and upgrade["pf_gt_1_2_0x"])
        else "不过线(Research Only 维持)")
    overall_negative = test[1.0][1]["net_pnl"] <= 0.0
    final_chapter = {
        "clause": FINAL_CHAPTER,
        "test_1x_net_pnl": test[1.0][1]["net_pnl"],
        "overall_still_negative": bool(overall_negative),
        "verdict": (FINAL_CHAPTER.split(":", 1)[1] if overall_negative
                    else "整体未负 ⇒ 条款不生效(如实填报,不渲染转正叙述)。"),
    }
    arm0_test_frozen = {f"{mult}x":
                        _cell(frozen_v13["runs"][f"bar|test@{mult}x"]["metrics"])
                        for mult in COST_MULTS}
    verdict_test = {
        "exposure": "test 第 19 次评估曝光(research-log #30 预注册,一次性,"
                    "v16 轮此后只许前向):开发期只跑 dev;test 对 v16 合成臂 "
                    "一次跑齐 1/1.5/2x 三格;arm0 数字只读引用 v13 在册 bar 臂"
                    "冻结产物(不重复曝光)。",
        "v16": {"test_1x": _cell(test[1.0][1]),
                "test_1_5x": _cell(test[1.5][1]),
                "test_2x": _cell(test[2.0][1]),
                "test_net_pnl_mean_ci95_1x": list(ci),
                "clause_1x": clause_stats(test[1.0][0].counters),
                "harvest_1x": harvest_buckets(test[1.0][0].trades),
                "family_1x": family_buckets(test[1.0][0].trades),
                "dev_1x": _cell(dev["v16"][1.0][1]),
                "dev_1_5x": _cell(dev["v16"][1.5][1]),
                "dev_2x": _cell(dev["v16"][2.0][1])},
        "arm0_test_frozen_v13_bar": arm0_test_frozen,
        "final_chapter": final_chapter,
        "upgrade_line": upgrade,
        "honesty_note": ("如实写 dev/test 对照,不渲染任何转正叙述;test<60 ⇒ "
                         "INSUFFICIENT_DATA;既往升级线逐条照报,不过线即 "
                         "Research Only 维持;Acceptance 最终章条款按协议原文"
                         "逐字填报。"),
    }

    # -- CSV 产物(dev 双臂三格 + v16 test 三格;arm0 test 见 v13 冻结产物) ---------
    REPORTS.mkdir(exist_ok=True)
    for arm_id in ARM_IDS:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 \
                else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v16(dev[arm_id][mult][0].trades).to_csv(
                REPORTS / f"trades_v16_{arm_id}__dev{suffix}.csv", index=False)
    for mult in COST_MULTS:
        suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
        trades_frame_v16(test[mult][0].trades).to_csv(
            REPORTS / f"trades_v16_v16__test{suffix}.csv", index=False)

    # -- JSON 产物 -----------------------------------------------------------------
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "doc": "docs/v16-protocol.md(冻结,2026-10-20)",
            "construction": "收割婚配 + 三闸集成:①入场三闸(位置空 pos≥0.35/"
                            "多 pos≤0.65、对面结构缘距离≥1.5R、失效=结构区缘∓"
                            "k_bm·ATR 且 F1≥0.8·ATR,缺一不存在交易)②档(顺风"
                            "=A×1.0、其余=B×0.75,k(v11) 只作档注)③执行差分"
                            "(fade BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED→maker 前缘限价 N_fill=12;续涨 "
                            "BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED→stop-entry 预埋最大等待 12×TF;TREND_RESUMED/RANGE_ROTATED "
                            "canonical 兜底)④一生一表收割(fade 达对面结构缘"
                            "全平+不加仓+禁 trail、续涨 turtle 保留、反向 "
                            "CHoCH、trailing 只收紧、时间帽 48×TF 且 MFE<0.5R "
                            "才出,恰 0.5R 不放逐)。正式死亡名单(不再回锅):"
                            "级联触发、虚拟挂单、分层入场、打包版优质五问。",
            "canonical_status": "arm0 = 现 canonical(v6 turtle 固化语义 × "
                                "canonical.json 16 档语义路由);检测/收割"
                                "(canonical 语义)/费用/容量/触发零改动。",
            "parity_discipline": "双锚硬断言:FROZEN_ERA 探针 == replay_turtle "
                                 "冻结产物逐格全等(代码零回归);arm0 == v13 在册 "
                                 "bar 臂逐格全等(engine 零回归)。不等即 raise, "
                                 "本报告不落地。",
            "test_eval_declaration": verdict_test["exposure"],
            "judge_rule": ("协议 §判定:dev@1.5x 净期望(expectancy_usdt)v16 − "
                           "arm0 为主锚,Δ ≤ 0 ⇒ 合成如实打回;若整体仍负 ⇒ "
                           "Acceptance 最终章(结构语言家族最优形态=近打平研究"
                           "系统),如实填报,不产生转正叙述。"),
            "upgrade_line": UPGRADE_LINE,
        },
        "config": {
            "config_file": "config/v16.json",
            "parent": "config/canonical.json(共享键逐项一致,运行前硬对账)",
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "v16_params": raw_cfg["v16_params"],
            "equity0": 300.0, "dev_frac": dev_frac,
            "cost_mults": list(COST_MULTS), "judge_cost_mult": JUDGE_COST,
            "risk_frac": raw_cfg["risk_frac"],
            "leverage_cap": raw_cfg["leverage_cap"],
            "taker_fee": raw_cfg["taker_fee"],
            "maker_fee": raw_cfg["maker_fee"],
            "slippage_per_side": raw_cfg["slippage_per_side"],
            "funding_per_8h_per_side": raw_cfg["funding_per_8h_per_side"],
            "portfolio_cap_frac": raw_cfg["portfolio_cap_frac"],
            "whitelist": [f"{i}|{'long' if d > 0 else 'short'}"
                          for i, d in sorted(whitelist)],
        },
        "contracts": {"source": spec_src,
                      "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
                      "cache_sha256": contracts_sha},
        "cross_checks": xcheck,
        "compaction_report": compaction_report,
        "parity_frozen_era_probe": parity_probe,
        "parity_arm0_vs_v13_bar": parity_arm0,
        "composite_verdict": composite_verdict,
        "clause_stats": clauses,
        "harvest_buckets": harvest,
        "family_buckets": family,
        "giveback": givebacks,
        "bucket_tables": buckets,
        "dev_signal_counts": rb.signal_counts(frames, dev_frac),
        "test_once": verdict_test,
        "runs": {},
    }
    for arm_id in ARM_IDS:
        for mult in COST_MULTS:
            res, m = dev[arm_id][mult]
            payload["runs"][f"{arm_id}|dev@{mult}x"] = {
                "metrics": m, "counters": res.counters,
                "clause": clauses[arm_id][f"{mult}x"],
                "harvest": harvest[arm_id][f"{mult}x"],
                "family": family[arm_id][f"{mult}x"],
                "giveback": givebacks[arm_id][f"{mult}x"],
                "buckets": buckets[arm_id][f"{mult}x"]}
    for mult in COST_MULTS:
        res, m = test[mult]
        payload["runs"][f"v16|test@{mult}x"] = {
            "metrics": m, "counters": res.counters,
            "clause": clause_stats(res.counters),
            "harvest": harvest_buckets(res.trades),
            "family": family_buckets(res.trades),
            "giveback": rb.giveback_summary(res.trades)}

    (REPORTS / "v16.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "v16.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/v16.json/.md + trades_v16 CSVs "
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


def _bcell(c: dict) -> str:
    return (f"{c['n_trades']} | {_f(c['net_pnl'])} | {_f(c['winrate'], 3)} "
            f"| {_f(c['profit_factor'])}")


def render_md(payload: dict) -> str:
    runs = payload["runs"]
    lines = [
        "# v16 收割婚配 + 位置/空间/F1 三闸集成回测报告(fade 对面结构缘收割 "
        "× 续涨 turtle × 时间帽 48×TF)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明",
        "",
        f"- {payload['protocol']['construction']}",
        f"- canonical 地位:{payload['protocol']['canonical_status']}",
        f"- parity 纪律:{payload['protocol']['parity_discipline']}",
        f"- **{payload['protocol']['test_eval_declaration']}**",
        f"- 判定规则:{payload['protocol']['judge_rule']}",
        f"- 升级线:{payload['protocol']['upgrade_line']}",
        f"- 合约:{payload['contracts']['source']}(离线强制)。",
        "",
        "## 1. 合成总览(dev 逐段对照;权益 300 USDT 起)",
        "",
        "| arm | 成本 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R | "
        "回吐R中位 | MFE_R中位 |",
        "|---|---|---|---|---|---|---|---|---|---|"]
    for a in ARM_IDS:
        for mult in COST_MULTS:
            m = runs[f"{a}|dev@{mult}x"]["metrics"]
            gb = runs[f"{a}|dev@{mult}x"]["giveback"]
            lines.append(
                f"| {a} | {mult}x | {_f(m['net_pnl'])} | {m['n_trades']} "
                f"| {_f(m['winrate'], 3)} | {_f(m['profit_factor'])} "
                f"| {_f(m['expectancy_usdt'], 4)} | {_f(m['expectancy_r'], 4)} "
                f"| {_f(gb['giveback_median_r'])} | {_f(gb['mfe_median_r'])} |")
    for mult in COST_MULTS:
        dv, d0 = (payload["runs"][f"v16|dev@{mult}x"]["metrics"],
                  payload["runs"][f"arm0|dev@{mult}x"]["metrics"])
        lines.append(
            f"| Δ(v16−arm0) | {mult}x | {dv['net_pnl'] - d0['net_pnl']:+.2f} "
            f"| {dv['n_trades'] - d0['n_trades']:+d} | — | — "
            f"| {(dv['expectancy_usdt'] - d0['expectancy_usdt'])
                 if dv['expectancy_usdt'] is not None
                 and d0['expectancy_usdt'] is not None else float('nan'):+.4f} "
            f"| — | — | — |")

    cv = payload["composite_verdict"]
    lines += ["", "## 2. 合成判定(dev@1.5x 净期望对照,协议 §判定;不虚饰)", "",
              "| 对照 | Δ净期望USDT | Δ净盈亏 | 裁决 |",
              "|---|---|---|---|",
              f"| v16 − arm0 | {_f(cv['delta_expectancy_usdt'], 4)} "
              f"| {_f(cv['delta_net_pnl'])} | **{cv['verdict']}** |", "",
              f"> {cv['attribution_note']}", ""]

    lines += ["## 3. 条款生效统计(dev@1.5x;三闸拒数、档、maker/stop 成交率)", "",
              "| arm | 闸评估 | Q1拒 | Q2拒 | F1拒 | 档A | 档B | fade无目标 | "
              "stop埋 | 触发 | 成交 | 漏单 | 成交率 | maker埋 | maker成 | "
              "过期 | 成交率 | 容量跳单 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---"
              "|---|---|"]
    for a in ARM_IDS:
        c = payload["clause_stats"][a][f"{JUDGE_COST}x"]
        lines.append(
            f"| {a} | {c['v16_gate_evaluated']} | {c['v16_gate_reject_q1_pos']} "
            f"| {c['v16_gate_reject_q2_room']} | {c['v16_gate_reject_f1']} "
            f"| {c['v16_tier_a']} | {c['v16_tier_b']} "
            f"| {c['v16_fade_no_target']} | {c['stop_entry_armed']} "
            f"| {c['stop_entry_triggers']} | {c['stop_entry_fills']} "
            f"| {c['stop_entry_expired']} | {_f(c['stopex_fill_rate'], 3)} "
            f"| {c['maker_entry_armed']} | {c['maker_entry_fills']} "
            f"| {c['maker_entry_expired']} | {_f(c['maker_fill_rate'], 3)} "
            f"| {c['portfolio_cap_skips']} |")
    lines += ["",
              "> fail-closed 通道:无 zone 拒 v16_reject_no_zone、无结构失效拒 "
              "v16_reject_no_invalid(各格详见 JSON clause_stats;arm0 不加该层"
              "恒 0)。", ""]

    lines += ["## 4. 收割桶 × 分家族桶(协议 §判定归因;dev@1.5x 评定成本)", ""]
    seg_key = f"{JUDGE_COST}x"
    lines += ["### 4.1 收割桶(目标/CHoCH/trail/失效/时间帽)", "",
              "| arm | 桶 | 笔数 | 净额 | 胜率 | PF |",
              "|---|---|---|---|---|---|"]
    for a in ARM_IDS:
        hb = payload["harvest_buckets"][a][seg_key]
        if not hb:
            lines.append(f"| {a} | — | 0 | — | — | — |")
        for b in HARVEST_BUCKETS:
            if b in hb:
                lines.append(f"| {a} | {b} | {_bcell(hb[b])} |")
    lines += ["", "### 4.2 分家族桶(fade/续涨/其他)", "",
              "| arm | 家族 | 笔数 | 净额 | 胜率 | PF |",
              "|---|---|---|---|---|---|"]
    for a in ARM_IDS:
        fb = payload["family_buckets"][a][seg_key]
        if not fb:
            lines.append(f"| {a} | — | 0 | — | — | — |")
        for fam in ("fade", "continuation", "other"):
            if fam in fb:
                lines.append(f"| {a} | {fam} | {_bcell(fb[fam])} |")
    lines.append("")

    to = payload["test_once"]
    up = to["upgrade_line"]
    lines += ["## 5. test 一次(第 19 次曝光;如实写 dev/test 对比,"
              "不渲染转正叙述)", "",
              f"- {to['exposure']}", "",
              "| arm | 段@成本 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|---|"]
    for a, cells in (("v16", (("dev@1x", to["v16"]["dev_1x"]),
                              ("dev@1.5x", to["v16"]["dev_1_5x"]),
                              ("dev@2x", to["v16"]["dev_2x"]),
                              ("test@1x", to["v16"]["test_1x"]),
                              ("test@1.5x", to["v16"]["test_1_5x"]),
                              ("test@2x", to["v16"]["test_2x"]))),
                     ("arm0(v13 bar 冻结引用)",
                      tuple((f"test@{m}x", to["arm0_test_frozen_v13_bar"][f"{m}x"])
                            for m in COST_MULTS))):
        for seg, c in cells:
            lines.append(
                f"| {a} | {seg} | {_f(c['net_pnl'])} | {c['n_trades']} "
                f"| {_f(c['winrate'], 3)} | {_f(c['profit_factor'])} "
                f"| {_f(c['expectancy_usdt'], 4)} "
                f"| {_f(c['expectancy_r'], 4)} |")
    lo, hi = to["v16"]["test_net_pnl_mean_ci95_1x"]
    harvest_1x_json = json.dumps(to["v16"]["harvest_1x"], ensure_ascii=False)
    family_1x_json = json.dumps(to["v16"]["family_1x"], ensure_ascii=False)
    lines += ["",
              f"- v16 test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,"
              f"2000 次,固定种子;如实报告):{_f(lo, 4)} ~ {_f(hi, 4)} USDT",
              f"- test@1x 收割桶:{harvest_1x_json}",
              f"- test@1x 分家族桶:{family_1x_json}",
              "- 既往升级线逐条照报:",
              f"  - test 笔数:{up['n_trades_test_1x']} ⇒ {up['sample_size']}",
              f"  - 1.5x 净期望:{_f(up['expectancy_usdt_test_1_5x'], 4)} "
              f"USDT ⇒ {'>0 ✔' if up['expectancy_positive_1_5x'] else '≤0 ✘'}",
              f"  - 2x PF:{_f(up['profit_factor_test_2_0x'])} "
              f"⇒ {'>1 ✔' if up['pf_gt_1_2_0x'] else '≤1 ✘'}",
              f"  - **判定:{up['verdict']}**", "",
              f"- **Acceptance 最终章条款(协议原文预注册,逐字填报)**",
              f"  - 条款:{to['final_chapter']['clause']}",
              f"  - test@1x 净盈亏:{_f(to['final_chapter']['test_1x_net_pnl'])}"
              f" USDT ⇒ {'整体仍负' if to['final_chapter']['overall_still_negative'] else '整体未负'}",
              f"  - **结论:{to['final_chapter']['verdict']}**",
              "", f"> {to['honesty_note']}"]

    lines += ["", "## 6. 事件 × TF 归因(dev@1.5x,机制 × TF × 方向)", ""]
    for a in ARM_IDS:
        bt = payload["bucket_tables"][a][f"{JUDGE_COST}x"]
        lines += [f"### {a}", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        any_rows = False
        for mech in ("BREAKOUT_ACCEPTED", "BREAKOUT_FAILED", "LEVEL_REJECTED", "TREND_RESUMED", "RANGE_ROTATED", "STRUCTURE_BROKEN", "FLIP_RETESTED", "EDGE_RECLAIMED"):
            for tf in ("1h", "2h", "4h", "12h", "1d"):
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

    lines += ["", "## 7. 已知近似与如实声明", "",
              "① fade maker 前缘限价 = 事件 zone 接近侧缘(空 zone.lo / 多 "
              "zone.hi,FLIP_RETESTED G3 与 v14 C4 前缘同口径),真挂单严格穿过成交、穿"
              "透缺口按限价成交(排队乐观性如实声明);决策 bar 收盘仍在区内"
              "附近时成交可能劣于收盘参照,保守性如实声明;② EDGE_RECLAIMED 在 canonical "
              "战役帧未开启发射(enable_e8=False,检测零改动纪律):fade 家族"
              "接线含 EDGE_RECLAIMED 但本场无 EDGE_RECLAIMED 事件,如实声明(触发即按同一收割语义处"
              "理);③ 时间帽 48×TF = bars_held ≥ 48 根认知 bar(1h→48h、"
              "12h→24d),到期且 MFE<0.5R 才出、恰 0.5R inclusive 不放逐,到"
              "期后逐收盘 bar 复核(同 §8.6 的 bars_held>= idiom,如实声明);"
              "④ 三闸以结构失效锚(attrs invalid = 结构区缘∓k_bm·ATR 口径)"
              "丈量,turtle 2·ATR 防线在 v16 不参与;R 基准=计划入场价至结构"
              "失效的价差,与 broker 定仓 risk_per_unit 同源;⑤ fade 收割目"
              "标 = 对面结构缘(与空间闸同一搜索口径的对面缘价;对面缘缺失 ⇒"
              " 无收割目标,v16_fade_no_target 如实计数);⑥ TREND_RESUMED/RANGE_ROTATED 不在两家"
              "族,canonical 市价 + turtle 管理(过同一三闸与结构锚、无收割"
              "目标),归入家族桶 other;⑦ 收割桶由出场 reason 机读标签解析"
              "(v16|<bucket>|),arm0 无标签全落 other;trailing 防线收紧后"
              "的破位出场归 trail 桶、防线未动归 invalid 桶;⑧ 组合 3% 容"
              "量决策账未乘档(预算口径略高估=保守,同 v14 声明);⑨ arm0 "
              "test 数字只读引用 v13 在册 bar 臂冻结产物(arm0==v13 bar dev "
              "逐格已硬对账),不重复曝光;⑩ test 曝光纪律见 §5,test<60 ⇒ "
              "INSUFFICIENT_DATA 不另立判据;Acceptance 最终章按协议原文逐"
              "字填报,不渲染任何转正叙述。", "",
              "机器可读全量:`reports/v16.json`;逐笔:"
              "`reports/trades_v16_{arm0,v16}__{dev,test}[_cost*].csv`(增列 "
              "order_type/v16_tier/size_mult/v16_pos/v16_room_R/struct_"
              "invalid/harvest_target/harvest_bucket/signal_decision_ts;"
              "arm0 CSV 为在册 canonical 16 档基线重放,与 v13 bar 臂已硬"
              "对账;arm0 test 逐笔见 v13 冻结产物)。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
