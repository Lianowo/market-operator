"""v14 施工包统一回测(docs/v14-protocol.md,冻结 2026-10-20):arm0-arm6
arm 矩阵 × 多尺度战役 × turtle 唯一语义,dev 全战役预评,test 一次只在
dev 最强两 arm。

用法:  python scripts/run_v14_arms.py
配置:  config/v14.json(parent=config/canonical.json,共享键逐项照抄;
       arm/arms/c1_quality_params/c3_params/c4_params 为 v14 新增,
       协议写死,不许调)
产物:  reports/v14_arms.json / v14_arms.md
       reports/trades_v14_arm{i}__{seg}[_cost1_5,_cost2_0].csv

口径与纪律:
- arm0 = 现 canonical(全部组件关),行为/数值锚双钉:① FROZEN_ERA 名单
  (8 档,与 reports/backtest_v6_turtle.json 自述 config 一致)arm=None 探针
  dev 三格须与冻结产物逐格全等(代码零回归硬断言);② arm0(现行 16 档)dev
  三格须与 reports/backtest_v13_cascade.json 在册 bar 臂逐格全等(Engine
  零回归硬断言)。不等即 raise,不写产物。
- 组件判定(协议 §判定,预注册,不打包宣胜):以 dev@1.5x 净期望
  (expectancy_usdt)为主锚 —— C1:arm1−arm0;C2-exchange:arm2−arm1;
  C2-virtual:arm3−arm1;C3:arm4−arm1;C4:arm5−arm1。Δ净期望 ≤ 0 ⇒ 该组
  件如实打回("effective/打回"按格如实,dev/test 对照仅写数不渲染转正)。
- 条款生效统计(每 arm dev 格):Q1/Q2/Q5 拒绝数(quality_reject_*)、
  stopex 成交率/漏单率(stop_entry_fills/expired ÷ armed)、两模式差价
  (arm2 vs arm3 同信号入场价差/费用差/成交差)、分层 vs 一口价均价差
  (arm5 vs arm1 fade 合并均价差)、事件×TF 归因表(bucket_tables 同
  canonical 口径)。
- test 段评估计数 v14 一次(research-log #27 预注册,test 第 17 次曝光):
  只对 dev@1.5x 净期望排名的最强两 arm 各跑一次(1x),排名规则写死在本
  脚本 SELECTION_RULE(dev@1.5x expectancy_usdt 降序,并列取净盈亏降序,
  arm1-arm6 候选,arm0 为对照不参与候选);如实写 dev/test 对比,不渲染
  任何转正叙述(样本量 <60 ⇒ INSUFFICIENT_DATA 一如以往如实填报)。
- 收割(turtle 持仓语义)/成本/容量/检测/触发框架零改动;合约规格强制
  离线缓存;历史产物只读比对不覆写。
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
from market_operator.backtest_v6 import compute_metrics          # noqa: E402
from market_operator.campaign import (                           # noqa: E402
    ArmConfig,
    run_campaigns,
)
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.quality import QualityParams, V14Params     # noqa: E402
from market_operator.quality import V14Router                    # noqa: E402
from market_operator.strategy_v6 import V6Params                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402
from market_operator.research import block_bootstrap_ci          # noqa: E402

CONFIG_V14 = ROOT / "config" / "v14.json"
CONFIG_CANONICAL = ROOT / "config" / "canonical.json"
FROZEN_TURTLE = ROOT / "reports" / "backtest_v6_turtle.json"
FROZEN_V13 = ROOT / "reports" / "backtest_v13_cascade.json"
REPORTS = ROOT / "reports"
COST_MULTS = (1.0, 1.5, 2.0)
JUDGE_COST = 1.5                       # dev 主锚成本(协议 §判定 dev@1.5x)
ARM_IDS = [f"arm{i}" for i in range(7)]
MIN_TRADES_TEST = 60
SELECTION_RULE = ("dev@1.5x expectancy_usdt 降序,并列取净盈亏降序;"
                  "候选 = arm1-arm6(arm0 为 canonical 对照不参与候选)")
FROZEN_ERA_WHITELIST = (("E1", -1), ("E1", 1), ("E3", -1), ("E3", 1),
                        ("E6", -1), ("E6", 1), ("E7", -1), ("E7", 1))
METRIC_EQ = ("net_pnl", "n_trades", "winrate", "profit_factor")


# ---------------------------------------------------------------------------
# 配置/锚
# ---------------------------------------------------------------------------


def load_config() -> dict:
    v14 = json.loads(CONFIG_V14.read_text(encoding="utf-8"))
    canon = json.loads(CONFIG_CANONICAL.read_text(encoding="utf-8"))
    for k, v in canon.items():
        if k in ("version", "meta", "note"):
            continue
        if v14.get(k) != v:
            raise RuntimeError(f"config/v14.json 与 canonical 共享键漂移 {k}")
    if set(v14["arms"]) != set(ARM_IDS):
        raise RuntimeError("config/v14.json arms 矩阵必须为 arm0..arm6")
    return v14


def _v14_params(rp_base: V6Params, quality) -> V14Params:
    """canonical V6Params 逐项提升 + v14 质量层(arm1-6 路由参数)。"""
    kw = {f: getattr(rp_base, f) for f in V6Params.__dataclass_fields__}
    return V14Params(**kw, quality=quality)


def build_arm(raw_cfg: dict, arm_id: str) -> tuple:
    """臂定义 → (quality|None, router_factory, ArmConfig|None)。

    arm0:(None, None, None)= plain V6Router + arm=None ⇒ 逐位 canonical;
    arm1-6:V14Router(c1_quality 开闭)+ ArmConfig(组件开关矩阵)。
    """
    spec = raw_cfg["arms"][arm_id]
    if arm_id == "arm0":
        return None, None, None
    q = QualityParams(**{k: raw_cfg["c1_quality_params"][k]
                         for k in ("pos_short_min", "pos_long_max",
                                   "k_room_R", "k_is", "k_star",
                                   "tier_a_mult", "tier_b_mult")}) \
        if spec["c1_quality"] else None
    arm = ArmConfig(fill_model=spec["fill_model"],
                    stop_entry=bool(spec["c3_stop_entry"]),
                    stop_wait_tf=int(raw_cfg["c3_params"]["stop_wait_tf"]),
                    ladder_fade=bool(spec["c4_laddering"]),
                    ladder_slices=int(raw_cfg["c4_params"]["ladder_slices"]),
                    k_bm=float(raw_cfg["c3_params"]["k_bm"]))
    return q, V14Router, arm


# ---------------------------------------------------------------------------
# 逐笔增广(v14):交易入场语义补丁(不变 canonical;只对 v14 新语义的账目修正)
# ---------------------------------------------------------------------------


def augment_trades_v14(trades: list, frames: dict, ledger: list) -> list:
    """rb.augment_trades + v14 入账修正:

    - entry_mode:open 行 meta.entry_mode(stop_entry / virtual_limit / limit /
      market)覆盖 canonical 二分标签(归因准确;arm0 账本无新键 ⇒ 逐位旧值);
    - stop_ref / quality_tier / ladder_group 溯源列;
    - C4 合并入账:同 trade_id 的 LIMIT_FILL(首份)+ ladder_add 份 ⇒ 合并
      加权均价 entry、合并 qty/notional/risk_usdt(Σ 份额)、entry_mode=
      "ladder";r_multiple 以合并风险重算;费用/盈亏本为全行 Σ,天然一致。
    """
    base = rb.augment_trades(trades, frames)
    meta_by_tid: dict = {}
    ladder_rows: dict = {}
    for row in ledger:
        if row["trade_id"] is None:
            continue
        if row["kind"] == "OPEN" or (row["kind"] == "LIMIT_FILL"
                                     and row["meta"].get("role") != "reduce"):
            meta_by_tid.setdefault(row["trade_id"], row["meta"])
            if row["meta"].get("ladder_group") is not None:
                ladder_rows.setdefault(row["trade_id"], []).append(row)
    out = []
    for t in base:
        m = meta_by_tid.get(t["trade_id"], {})
        rows = ladder_rows.get(t["trade_id"])
        if rows:
            q = sum(r["qty"] for r in rows)
            w = sum(r["qty"] * r["price"] for r in rows)
            risk = sum(float(r["meta"].get("risk_usdt") or 0.0) for r in rows)
            t = {**t, "entry": (w / q if q else t["entry"]), "qty": q,
                 "notional": w, "risk_usdt": risk,
                 "entry_mode": "ladder",
                 "ladder_slices_filled": len(rows),
                 "limit_price": None, "fill_wait_bars": None,
                 "ladder_group": m.get("ladder_group"),
                 "r_multiple": (t["net_pnl"] / risk
                                if risk > 0 else t["r_multiple"])}
        elif m.get("entry_mode") in ("stop_entry", "virtual_limit"):
            t = {**t, "entry_mode": m["entry_mode"],
                 "limit_price": m.get("limit_price")}
        out.append({**t,
                    "quality_tier": m.get("quality_tier"),
                    "size_mult": m.get("size_mult"),
                    "stop_ref": m.get("stop_ref"),
                    "signal_decision_ts": int(m.get("armed_at_ts")
                                              or t["open_decision_ts"]),
                    "ladder_slices_filled":
                        t.get("ladder_slices_filled"),
                    "ladder_group": t.get("ladder_group")})
    return out


COLUMNS_V14 = list(rb.TRADE_COLUMNS) + ["quality_tier", "size_mult",
                                        "stop_ref", "signal_decision_ts",
                                        "ladder_slices_filled",
                                        "ladder_group"]


def trades_frame_v14(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": rb._iso(t["open_decision_ts"]),
                     "open_iso": rb._iso(t["open_ts"]),
                     "close_decision_iso": rb._iso(t["close_decision_ts"]),
                     "exit_iso": rb._iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=COLUMNS_V14)


# ---------------------------------------------------------------------------
# 回放驱动(dev 三格;test 只对入选臂)
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
        res.trades = augment_trades_v14(res.trades, frames, res.ledger)
        m = compute_metrics(res)
        out[mult] = (res, m)
        c = res.counters
        print(f"      [bt] {segment} cost {mult}x: net={m['net_pnl']:+.2f} "
              f"n={m['n_trades']} q_rej="
              f"{c.get('quality_reject_q1_pos', 0)}/"
              f"{c.get('quality_reject_q2_room', 0)}/"
              f"{c.get('quality_reject_q5_f1', 0)} ({time.time() - t0:.0f}s)",
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
                     **{f"v14_{k}": m[k] for k in METRIC_EQ},
                     **{f"frozen_{k}": fm[k] for k in METRIC_EQ},
                     "parity": ok})
        if not ok:
            raise RuntimeError(f"{tag} 与冻结锚不一致 {prefix}@{mult}x: "
                               f"{rows[-1]}")
    return rows


# ---------------------------------------------------------------------------
# 条款生效统计 / 差价归因
# ---------------------------------------------------------------------------


def clause_stats(counters: dict) -> dict:
    """每格条款生效统计(缺失键 = 0,arm0 全部 0)。"""
    g = counters.get
    stopex_armed = g("stop_entry_armed", 0)
    return {
        "quality_evaluated": g("quality_evaluated", 0),
        "quality_reject_q1_pos": g("quality_reject_q1_pos", 0),
        "quality_reject_q2_room": g("quality_reject_q2_room", 0),
        "quality_reject_q5_f1": g("quality_reject_q5_f1", 0),
        "quality_tier_a": g("quality_tier_a", 0),
        "quality_tier_b": g("quality_tier_b", 0),
        "stop_entry_armed": stopex_armed,
        "stop_entry_triggers": g("stop_entry_triggers", 0),
        "stop_entry_fills": g("stop_entry_fills", 0),
        "stop_entry_expired": g("stop_entry_expired", 0),
        "stop_entry_canceled": g("stop_entry_canceled", 0),
        "stopex_fill_rate": (g("stop_entry_fills", 0) / stopex_armed
                             if stopex_armed else None),
        "stopex_miss_rate": (g("stop_entry_expired", 0) / stopex_armed
                             if stopex_armed else None),
        "maker_entry_armed": g("maker_entry_armed", 0),
        "maker_entry_fills": g("maker_entry_fills", 0),
        "maker_entry_expired": g("maker_entry_expired", 0),
        "maker_entry_canceled": g("maker_entry_canceled", 0),
        "virtual_entry_armed": g("virtual_entry_armed", 0),
        "virtual_entry_triggers": g("virtual_entry_triggers", 0),
        "virtual_entry_fills": g("virtual_entry_fills", 0),
        "virtual_entry_expired": g("virtual_entry_expired", 0),
        "virtual_entry_canceled": g("virtual_entry_canceled", 0),
        "ladder_signals": g("ladder_signals", 0),
        "ladder_armed": g("ladder_armed", 0),
        "ladder_fills": g("ladder_fills", 0),
        "ladder_add_fills": g("ladder_add_fills", 0),
        "ladder_entry_expired": g("ladder_entry_expired", 0),
        "ladder_entry_canceled": g("ladder_entry_canceled", 0),
    }


def _match_key(t: dict) -> tuple:
    """同信号硬匹配键:信号决策时刻(stop/virtual 触发晚于 signaling,须以
    武装 armed/原决策时刻对齐,不能用账本 open_decision_ts)。"""
    return (t["campaign"], int(t.get("signal_decision_ts")
                                 or t["open_decision_ts"]),
            str(t["event_id"]), int(t["direction"]))


def two_mode_diff(trades_ex: list, trades_vi: list) -> dict:
    """两模式差价(arm2 exchange vs arm3 virtual,同事件×信号决策匹配)。

    差口径(px_ex − px_vi)·direction:正 = virtual 入场更贵(exchange 更好);
    费用差 = Σfees(vi) − Σfees(ex);成交差 = 各模式独有笔数(virtual 保成交)。
    """
    a = {_match_key(t): t for t in trades_ex if t["in_segment_open"]}
    b = {_match_key(t): t for t in trades_vi if t["in_segment_open"]}
    both = sorted(set(a) & set(b))
    dpx = []
    drel = []
    dfee = []
    for k in both:
        d = int(a[k]["direction"])
        dpx.append((a[k]["entry"] - b[k]["entry"]) * d)
        drel.append((a[k]["entry"] - b[k]["entry"]) * d / a[k]["entry"])
        dfee.append(b[k]["fees"] - a[k]["fees"])
    return {"n_matched": len(both),
            "n_exchange_only": len(set(a) - set(b)),
            "n_virtual_only": len(set(b) - set(a)),
            "dpx_direction_signed": {
                "mean": float(np.mean(dpx)) if dpx else None,
                "median": rb._median(dpx),
                "note": "原始价位单位(跨品种不可比,见 dpx_rel)"},
            "dpx_rel_direction_signed": {
                "mean_bps": (float(np.mean(drel) * 1e4) if drel else None),
                "median_bps": (rb._median(drel) * 1e4 if drel else None),
                "note": "(px_ex − px_vi)·d/px_ex ×1e4;正=virtual 入场更贵"},
            "dfee_virtual_minus_exchange": {
                "mean": float(np.mean(dfee)) if dfee else None,
                "sum": float(np.sum(dfee)) if dfee else None}}


def ladder_vs_flat_diff(trades_ladder: list, trades_flat: list) -> dict:
    """分层 vs 一口价均价差(arm5 vs arm1,同事件×决策时刻匹配,方向带号)。

    (px_flat − px_ladder)·direction:正 = 分层均价更优(fade 回落被前缘
    先接);负 = 更劣(内缘未达只吃到前缘浅价)。另报每信号平均成交份数。
    """
    a = {_match_key(t): t for t in trades_flat if t["in_segment_open"]}
    b = {_match_key(t): t for t in trades_ladder if t["in_segment_open"]}
    both = sorted(set(a) & set(b))
    dpx = []
    drel = []
    slices = []
    for k in both:
        d = int(a[k]["direction"])
        dpx.append((a[k]["entry"] - b[k]["entry"]) * d)
        drel.append((a[k]["entry"] - b[k]["entry"]) * d / a[k]["entry"])
        slices.append(b[k].get("ladder_slices_filled") or 0)
    return {"n_matched": len(both),
            "n_flat_only": len(set(a) - set(b)),
            "n_ladder_only": len(set(b) - set(a)),
            "avg_slices_filled": (float(np.mean(slices)) if slices else None),
            "dpx_direction_signed": {
                "mean": float(np.mean(dpx)) if dpx else None,
                "median": rb._median(dpx),
                "note": "原始价位单位(跨品种不可比,见 dpx_rel)"},
            "dpx_rel_direction_signed": {
                "mean_bps": (float(np.mean(drel) * 1e4) if drel else None),
                "median_bps": (rb._median(drel) * 1e4 if drel else None),
                "note": "(px_flat − px_ladder)·d/px_flat ×1e4;正=分层均价更优"}}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    t_start = time.time()
    raw_cfg = load_config()
    params = Params.from_json(CONFIG_V14)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    tf_list = list(raw_cfg["tf_list"])
    if tf_list != ["1h", "2h", "4h", "12h", "1d"]:
        raise RuntimeError(f"config/v14.json tf_list 冻结: {tf_list}")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v14 parent=canonical:merge_max_atr 必须 2.0")

    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    print("[prep] 全战役帧(canonical 壳,一次性)", flush=True)
    frames, compaction_report = rb.prepare_frames(raw_cfg, params, tf_list)
    xcheck = rb.cross_checks(frames, float(raw_cfg["k_is"]), merge_max_atr=2.0)

    frozen_turtle = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    frozen_v13 = json.loads(FROZEN_V13.read_text(encoding="utf-8"))

    # -- 锚①:FROZEN_ERA 名单(8 档)探针 == backtest_v6_turtle 冻结产物 ---------
    print("[anchor-1] 冻结时代名单探针(dev 三格;arm=None)" , flush=True)
    rp_probe = dc_replace(rp_base, whitelist=frozenset(FROZEN_ERA_WHITELIST))
    probe = run_grid(frames, raw_cfg, params, rp_probe, None, None, contracts,
                     segment="dev")
    parity_probe = assert_anchor("FROZEN_ERA 探针", probe,
                                 frozen_turtle["runs"], "turtle|dev")
    print("[anchor-1] 探针 3 格 == canonical v6 臂 B 冻结产物(逐格全等)",
          flush=True)

    # -- arm 矩阵(dev 三格 × 7 臂) --------------------------------------------
    dev: dict = {}
    for arm_id in ARM_IDS:
        q, factory, arm = build_arm(raw_cfg, arm_id)
        rp_use = rp_base if arm_id == "arm0" else _v14_params(rp_base, q)
        print(f"[arm] {arm_id}: {raw_cfg['arms'][arm_id]['note']}", flush=True)
        dev[arm_id] = run_grid(frames, raw_cfg, params, rp_use, factory, arm,
                               contracts, segment="dev")

    # -- 锚②:arm0 == v13 在册 bar 臂(现行 16 档,Engine 零回归) ----------------
    parity_arm0 = assert_anchor("arm0==v13 bar", dev["arm0"],
                                frozen_v13["runs"], "bar|dev")
    print("[anchor-2] arm0 3 格 == v13 在册 bar 臂(逐格全等)", flush=True)

    # -- 组件判定(协议 §判定:dev@1.5x 净期望,如实打回不打包) -------------------
    def judge(pair: tuple) -> dict:
        a_id, b_id = pair
        ea = dev[a_id][JUDGE_COST][1]["expectancy_usdt"]
        eb = dev[b_id][JUDGE_COST][1]["expectancy_usdt"]
        ok = (ea is not None and eb is not None) and (ea - eb) > 0.0
        return {"base": a_id, "delta_expectancy_usdt": (ea - eb)
                if ea is not None and eb is not None else None,
                "delta_net_pnl": dev[a_id][JUDGE_COST][1]["net_pnl"]
                - dev[b_id][JUDGE_COST][1]["net_pnl"],
                "verdict": "effective(改善)" if ok else "打回(无改善)"}

    verdicts = {
        "C1 优质五问(arm1−arm0)": judge(("arm1", "arm0")),
        "C2-exchange_maker(arm2−arm1)": judge(("arm2", "arm1")),
        "C2-virtual_limit(arm3−arm1)": judge(("arm3", "arm1")),
        "C3 stop-entry(arm4−arm1)": judge(("arm4", "arm1")),
        "C4 分层入场(arm5−arm1)": judge(("arm5", "arm1")),
        "C5 质量配注": {"note": "并入 C1 判定(档 A/B size_mult 是 C1 的配注,"
                              "无独立臂;arm1 vs arm0 的差即 C1∧C5 联合效果)"},
    }

    # -- 条款生效统计 / 差价归因 -------------------------------------------------
    clauses = {arm_id: {f"{mult}x": clause_stats(dev[arm_id][mult][0].counters)
                        for mult in COST_MULTS} for arm_id in ARM_IDS}
    givebacks = {arm_id: {f"{mult}x": rb.giveback_summary(dev[arm_id][mult][0].trades)
                          for mult in COST_MULTS} for arm_id in ARM_IDS}
    buckets = {arm_id: {f"{mult}x": rb.bucket_tables(dev[arm_id][mult][0].trades)
                        for mult in COST_MULTS} for arm_id in ARM_IDS}
    diffs = {
        "two_mode_exchange_vs_virtual_dev_1x": two_mode_diff(
            dev["arm2"][1.0][0].trades, dev["arm3"][1.0][0].trades),
        "two_mode_exchange_vs_virtual_dev_1_5x": two_mode_diff(
            dev["arm2"][1.5][0].trades, dev["arm3"][1.5][0].trades),
        "ladder_vs_flat_dev_1x": ladder_vs_flat_diff(
            dev["arm5"][1.0][0].trades, dev["arm1"][1.0][0].trades),
        "ladder_vs_flat_dev_1_5x": ladder_vs_flat_diff(
            dev["arm5"][1.5][0].trades, dev["arm1"][1.5][0].trades),
    }

    # -- test 段一次:dev@1.5x 净期望最强两臂(SELECTION_RULE 写死) --------------
    ranking = sorted(
        (arm_id for arm_id in ARM_IDS[1:]),
        key=lambda a: (dev[a][JUDGE_COST][1]["expectancy_usdt"]
                       if dev[a][JUDGE_COST][1]["expectancy_usdt"] is not None
                       else float("-inf"),
                       dev[a][JUDGE_COST][1]["net_pnl"]),
        reverse=True)
    selected = ranking[:2]
    print(f"[test] 入选(第 17 次曝光,research-log #27):{selected}", flush=True)
    sample = {}
    for arm_id in selected:
        q, factory, arm = build_arm(raw_cfg, arm_id)
        rp_use = _v14_params(rp_base, q)
        res, m1x = run_grid(frames, raw_cfg, params, rp_use, factory, arm,
                            contracts, segment="test", mults=(1.0,))[1.0]
        tr_in = [t for t in res.trades if t["in_segment_open"]]
        if tr_in:
            ci = block_bootstrap_ci(
                np.asarray([t["net_pnl"] for t in tr_in], dtype=np.float64),
                np.asarray([t["open_ts"] for t in tr_in], dtype=np.int64))
        else:
            ci = (float("nan"), float("nan"))
        sample[arm_id] = {"test_1x": _cell(m1x),
                          "n_trades_test_1x": m1x["n_trades"],
                          "test_net_pnl_mean_ci95_1x": list(ci),
                          "dev_1x": _cell(dev[arm_id][1.0][1]),
                          "dev_1_5x": _cell(dev[arm_id][1.5][1]),
                          "test_trades": res.trades}
        trades_frame_v14(res.trades).to_csv(
            REPORTS / f"trades_v14_{arm_id}__test.csv", index=False)

    verdict_test = {
        "exposure": "test 第 17 次评估曝光(research-log #27 预注册,一次性,"
                    "v14 轮此后只许前向):开发期只跑 dev;test 对 dev 最强两 "
                    "arm 各跑一次(1x)。",
        "selection_rule": SELECTION_RULE,
        "selected": selected,
        "sample_size_verdict": {a: ("SUFFICIENT(>=60)"
                                    if sample[a]["n_trades_test_1x"]
                                    >= MIN_TRADES_TEST
                                    else "INSUFFICIENT_DATA")
                                for a in selected},
        "honesty_note": ("如实写 dev/test 对比,不渲染任何转正叙述;test<60 ⇒ "
                         "INSUFFICIENT_DATA;历史升级线判据(v6)是另一协议语境,"
                         "本轮不另立判据。"),
    }

    # -- CSV 产物(逐 arm dev;arm0 一并产出以作在册基线) --------------------------
    REPORTS.mkdir(exist_ok=True)
    for arm_id in ARM_IDS:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 \
                else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v14(dev[arm_id][mult][0].trades).to_csv(
                REPORTS / f"trades_v14_{arm_id}__dev{suffix}.csv", index=False)

    # -- JSON 产物 -----------------------------------------------------------------
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "doc": "docs/v14-protocol.md(冻结,2026-10-20)",
            "construction": "施工包 C1-C5 全 Semantics 落(可开可关;非过滤名单; "
                            "判定分组件归因,不打包宣胜)。",
            "canonical_status": "arm0 = 现 canonical(v6 turtle 固化语义 × "
                                "canonical.json 16 档语义路由);检测/收割/"
                                "成本/容量/触发零改动。",
            "parity_discipline": "双锚硬断言:FROZEN_ERA 探针 == backtest_v6_turtle "
                                 "冻结产物逐格全等(代码零回归);arm0 == v13 在册 "
                                 "bar 臂逐格全等(engine 零回归)。不等即 raise, "
                                 "本报告不落地。",
            "test_eval_declaration": verdict_test["exposure"],
            "judge_rule": ("协议 §判定:dev@1.5x 净期望(expectancy_usdt)单组件 "
                           "对照,Δ ≤ 0 ⇒ 如实打回;不整包报喜。"),
            "selection_rule": SELECTION_RULE,
        },
        "config": {
            "config_file": "config/v14.json",
            "parent": "config/canonical.json(共享键逐项一致,运行前硬对账)",
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "c1_quality_params": raw_cfg["c1_quality_params"],
            "c3_params": raw_cfg["c3_params"],
            "c4_params": raw_cfg["c4_params"],
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
        "component_verdicts": verdicts,
        "clause_stats": clauses,
        "giveback": givebacks,
        "bucket_tables": buckets,
        "price_diffs": diffs,
        "dev_signal_counts": rb.signal_counts(frames, dev_frac),
        "test_once": {**verdict_test,
                      **{a: sample[a] for a in selected}},
        "runs": {},
    }
    for arm_id in ARM_IDS:
        for mult in COST_MULTS:
            res, m = dev[arm_id][mult]
            payload["runs"][f"{arm_id}|dev@{mult}x"] = {
                "metrics": m, "counters": res.counters,
                "clause": clauses[arm_id][f"{mult}x"],
                "giveback": givebacks[arm_id][f"{mult}x"],
                "buckets": buckets[arm_id][f"{mult}x"]}
    for arm_id in selected:
        payload["runs"][f"{arm_id}|test@1.0x"] = {
            "metrics": sample[arm_id]["test_1x"],
            "trades": sample[arm_id]["test_trades"]}

    (REPORTS / "v14_arms.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "v14_arms.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/v14_arms.json/.md + trades_v14 CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    if x is None or (isinstance(x, float) and x != x):
        return dash
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def render_md(payload: dict) -> str:
    runs = payload["runs"]
    lines = [
        "# v14 施工包 arm 矩阵统一回测报告(优质五问 × maker 双模式 × "
        "stop-entry × 分层/质量配注)",
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
        f"- test 入选规则:{payload['protocol']['selection_rule']}",
        "- 合约:"
        f"{payload['contracts']['source']}(离线强制)。",
        "",
        "## 1. arm 矩阵总览(dev;权益 300 USDT 起)",
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

    lines += ["", "## 2. 组件判定(dev@1.5x 净期望对照,协议 §判定;不虚饰)", "",
              "| 组件 | 对照 | Δ净期望USDT | Δ净盈亏 | 裁决 |",
              "|---|---|---|---|---|"]
    for k, v in payload["component_verdicts"].items():
        if "verdict" not in v:
            lines.append(f"| {k} | {v['note']} | — | — | — |")
            continue
        lines.append(f"| {k} | {v['base']} | {_f(v['delta_expectancy_usdt'], 4)} "
                     f"| {_f(v['delta_net_pnl'])} | **{v['verdict']}** |")

    lines += ["", "## 3. 条款生效统计(dev@1.5x;Q1/Q2/Q5 拒单数、stopex、"
              "分层)", "",
              "| arm | Q评 | Q1拒 | Q2拒 | Q5拒 | 档A | 档B | stop埋 | "
              "触发 | 成交 | 漏单 | 成交率 | maker埋 | maker成 | "
              "virt埋 | virt触发 | virt成 | 层信号 | 层份成 | 层过期 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
              "---|---|---|---|---|---|"]
    for a in ARM_IDS:
        c = payload["clause_stats"][a][f"{JUDGE_COST}x"]
        lines.append(
            f"| {a} | {c['quality_evaluated']} | {c['quality_reject_q1_pos']} "
            f"| {c['quality_reject_q2_room']} | {c['quality_reject_q5_f1']} "
            f"| {c['quality_tier_a']} | {c['quality_tier_b']} "
            f"| {c['stop_entry_armed']} | {c['stop_entry_triggers']} "
            f"| {c['stop_entry_fills']} | {c['stop_entry_expired']} "
            f"| {_f(c['stopex_fill_rate'], 3)} | {c['maker_entry_armed']} "
            f"| {c['maker_entry_fills']} | {c['virtual_entry_armed']} "
            f"| {c['virtual_entry_triggers']} | {c['virtual_entry_fills']} "
            f"| {c['ladder_signals']} | {c['ladder_fills']} "
            f"| {c['ladder_entry_expired']} |")

    lines += ["", "## 4. 差价归因(同事件×决策时刻匹配,方向带号)", ""]
    for tag, key in (("两模式 arm2-ex vs arm3-vi(dev@1x)",
                      "two_mode_exchange_vs_virtual_dev_1x"),
                     ("两模式 arm2-ex vs arm3-vi(dev@1.5x)",
                      "two_mode_exchange_vs_virtual_dev_1_5x"),
                     ("分层 arm5 vs 一口价 arm1(dev@1x)",
                      "ladder_vs_flat_dev_1x"),
                     ("分层 arm5 vs 一口价 arm1(dev@1.5x)",
                      "ladder_vs_flat_dev_1_5x")):
        d = payload["price_diffs"][key]
        lines.append(f"### {tag}")
        lines.append("")
        if "two_mode" in key:
            lines += [
                f"- 匹配笔数 {d['n_matched']};exchange 独有 "
                f"{d['n_exchange_only']}(virtual 保成交多吃);virtual 独有 "
                f"{d['n_virtual_only']}",
                f"- 入场价差 (px_ex − px_vi)·d.relative(bps):均 "
                f"{_f(d['dpx_rel_direction_signed']['mean_bps'], 2)} "
                f"| 中位 {_f(d['dpx_rel_direction_signed']['median_bps'], 2)}"
                "(正 = virtual 入场更贵,exchange 更优)",
                f"- 费用差 vi−ex:均 "
                f"{_f(d['dfee_virtual_minus_exchange']['mean'], 6)} USDT | Σ "
                f"{_f(d['dfee_virtual_minus_exchange']['sum'], 4)} USDT", ""]
        else:
            lines += [
                f"- 匹配笔数 {d['n_matched']};一口价独有 {d['n_flat_only']};"
                f"分层独有 {d['n_ladder_only']};平均每信号成交份 "
                f"{_f(d['avg_slices_filled'], 2)}",
                f"- 均价差 (px_flat − px_ladder)·d.relative(bps):均 "
                f"{_f(d['dpx_rel_direction_signed']['mean_bps'], 2)} "
                f"| 中位 {_f(d['dpx_rel_direction_signed']['median_bps'], 2)}"
                "(正 = 分层均价更优)", ""]

    lines += ["", "## 5. test 一次(第 17 次曝光;如实写 dev/test 对比,"
              "不渲染转正叙述)", "",
              f"- {payload['test_once']['exposure']}",
              f"- 入选:{', '.join(payload['test_once']['selected'])}"
              f"(规则:{payload['test_once']['selection_rule']})", "",
              "| arm | dev@1x 净 | dev@1x PF | test@1x 净 | test@1x 笔数 "
              "| test@1x PF | 样本量 |",
              "|---|---|---|---|---|---|---|"]
    for a in payload["test_once"]["selected"]:
        s = payload["test_once"][a]
        lines.append(
            f"| {a} | {_f(s['dev_1x']['net_pnl'])} "
            f"| {_f(s['dev_1x']['profit_factor'])} "
            f"| {_f(s['test_1x']['net_pnl'])} | {s['n_trades_test_1x']} "
            f"| {_f(s['test_1x']['profit_factor'])} "
            f"| {payload['test_once']['sample_size_verdict'][a]} |")
    lines += ["", "test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,"
              "2000 次,固定种子;如实报告):"]
    for a in payload["test_once"]["selected"]:
        lo, hi = payload["test_once"][a]["test_net_pnl_mean_ci95_1x"]
        lines.append(f"- {a}:{_f(lo, 4)} ~ {_f(hi, 4)} USDT")
    lines += ["", f"> {payload['test_once']['honesty_note']}"]

    lines += ["", "## 6. 事件 × TF 归因(dev@1.5x,机制 × TF × 方向)", ""]
    for a in ARM_IDS:
        bt = payload["bucket_tables"][a][f"{JUDGE_COST}x"]
        lines += [f"### {a}", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        any_rows = False
        for mech in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8"):
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
              "① C2 exchange_maker 即既有严格穿过 maker 语义,排队乐观性如实"
              "声明(穿透缺口按限价成交,不给价格改善);② virtual_limit 价达"
              "(含持平)触发、次细 bar 开盘 taker+滑点成交(保成交、守序差一"
              "档),价达与真排队之间残余乐观如实声明;③ C3 stop-entry 触发"
              "用严格穿过(持平不触发,同保守口径),触发后次细 bar 开盘 taker;"
              "超时 12×TF 自决策 bar 最末细 bar 起锚;④ C4 首份以后的合并份"
              "按 maker 限价并入(同 trade_id,加权均价),PositionManager 的"
              " R 基准取首份价(收割语义只用结构 invalid,MFE_R 报告口径受此"
              "近似,如实声明);⑤ 组合 3% 容量决策账未乘 tier(预算口径略高估"
              "=保守);⑥ Q2 对面缘缺失判空间无界=过(拒绝只认缘在场太挤);"
              "⑦ Q3 为档注(k>k_*=2 成熟,v11 厚假说口径),E6 类无 zone 事件"
              "不加成;⑧ pos 带取 E8 同口径(全名单最近 LOW/HIGH pivot;≤8 "
              "窗口与全名单等价,因 pivot 严格高低交错);⑨ 差价归因按(战役×"
              "决策时刻×事件)硬匹配,匹配不上者单列不做插补;⑩ test 曝光纪"
              "律见 §5,test<60 ⇒ INSUFFICIENT_DATA 不另立判据。", "",
              "机器可读全量:`reports/v14_arms.json`;逐笔:"
              "`reports/trades_v14_arm{i}__{dev,test}[_cost*].csv`(增列 "
              "quality_tier/size_mult/stop_ref/ladder_*;arm0 CSV 为在册 "
              "canonical 16 档基线重放,与 v13 bar 臂已硬对账)。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
