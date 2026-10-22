"""v15 四件套合成回测(docs/v15-protocol.md,冻结 2026-10-20):canonical
(arm0 语义)+ v14 裁决存活件(带沿条款/fade maker 真挂单/续涨 stop-entry),
dev 主评 + test 一次。

用法:  python scripts/run_v15.py
配置:  config/v15.json(parent=config/canonical.json,共享键逐项照抄;
       arms/band_side_params/c2_params/c3_params/off_note 为 v15 新增,
       协议写死,不许调)
产物:  reports/v15.json / v15.md
       reports/trades_v15_{arm0,v15}__dev[_cost1_5,_cost2_0].csv
       reports/trades_v15_v15__test[_cost1_5,_cost2_0].csv

口径与纪律:
- arm0 = 现 canonical(全部组件关),与 v14 同规双钉:① FROZEN_ERA 名单
  (8 档,与 reports/backtest_v6_turtle.json 自述 config 一致)arm=None 探针
  dev 三格须与冻结产物逐格全等(代码零回归硬断言);② arm0(现行 16 档)dev
  三格须与 reports/backtest_v13_cascade.json 在册 bar 臂逐格全等(Engine
  零回归硬断言)。不等即 raise,不写产物。
- 合成判定(协议 §判定,预注册,不打包宣胜):dev@1.5x 净期望
  (expectancy_usdt)v15 − arm0 为主锚;Δ ≤ 0 ⇒ 合成如实打回
  ("effective/打回"按格如实,dev/test 对照仅写数不渲染转正叙述)。
- 条款生效统计(每格):带沿评估/拒绝数(band_side_evaluated/reject_pos)、
  续涨 stopex 预埋/触发/成交/漏单(stop_entry_*)、fade maker 预埋/成交/
  过期(maker_entry_*)、组合容量跳单;v15 vs arm0 同信号成交差(硬匹配
  差价/费用差/仅单方成交笔数)。
- test 段评估计数 v15 一次(research-log #29 预注册,test 第 18 次曝光):
  v15 合成臂一次跑齐成本 1/1.5/2x 三格(一次落座,同 canonical 各轮口径);
  样本量 <60 ⇒ INSUFFICIENT_DATA 一如以往如实填报;既往升级线(test≥60
  笔、1.5x 净期望>0、2x PF>1 ⇒ 才谈 forward dry-run)逐条照报,不过线
  即 Research Only 维持,不渲染任何转正叙述;arm0 test 数字只读引用
  v13 在册 bar 臂冻结产物(arm0==v13 bar 已硬对账),不重复曝光。
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
import run_v14_arms as r14                                      # noqa: E402
from market_operator.backtest_v6 import compute_metrics          # noqa: E402
from market_operator.bandside import (                           # noqa: E402
    BandSideParams,
    V15Params,
    V15Router,
)
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.campaign import ArmConfig, run_campaigns    # noqa: E402
from market_operator.strategy_v6 import V6Params                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402
from market_operator.research import block_bootstrap_ci          # noqa: E402

CONFIG_V15 = ROOT / "config" / "v15.json"
CONFIG_CANONICAL = ROOT / "config" / "canonical.json"
FROZEN_TURTLE = ROOT / "reports" / "backtest_v6_turtle.json"
FROZEN_V13 = ROOT / "reports" / "backtest_v13_cascade.json"
REPORTS = ROOT / "reports"
COST_MULTS = (1.0, 1.5, 2.0)
JUDGE_COST = 1.5                       # dev 主锚成本(协议 §判定 dev@1.5x)
ARM_IDS = ("arm0", "v15")
MIN_TRADES_TEST = 60
FROZEN_ERA_WHITELIST = (("BREAKOUT_ACCEPTED", -1), ("BREAKOUT_ACCEPTED", 1), ("LEVEL_REJECTED", -1), ("LEVEL_REJECTED", 1),
                        ("STRUCTURE_BROKEN", -1), ("STRUCTURE_BROKEN", 1), ("FLIP_RETESTED", -1), ("FLIP_RETESTED", 1))
METRIC_EQ = ("net_pnl", "n_trades", "winrate", "profit_factor")
UPGRADE_LINE = ("既往升级线(v4-v8 不变口径):test≥60 笔 ∧ 1.5x 净期望>0 ∧ "
                "2x PF>1 ⇒ 才谈 forward dry-run;否则 Research Only 维持")


# ---------------------------------------------------------------------------
# 配置/锚
# ---------------------------------------------------------------------------


def load_config() -> dict:
    v15 = json.loads(CONFIG_V15.read_text(encoding="utf-8"))
    canon = json.loads(CONFIG_CANONICAL.read_text(encoding="utf-8"))
    for k, v in canon.items():
        if k in ("version", "meta", "note"):
            continue
        if v15.get(k) != v:
            raise RuntimeError(f"config/v15.json 与 canonical 共享键漂移 {k}")
    if set(v15["arms"]) != set(ARM_IDS):
        raise RuntimeError("config/v15.json arms 必须为 arm0/v15")
    spec = v15["arms"]["v15"]
    if not (spec["band_side"] and spec["fill_model"] == "exchange_maker"
            and spec["c3_stop_entry"] and not spec["c4_laddering"]
            and not spec["tiersize"]):
        raise RuntimeError(f"config/v15.json v15 合成开关漂移: {spec}")
    a0 = v15["arms"]["arm0"]
    if any((a0["band_side"], a0["fill_model"] != "canonical",
            a0["c3_stop_entry"], a0["c4_laddering"], a0["tiersize"])):
        raise RuntimeError(f"config/v15.json arm0 必须全组件关(canonical): {a0}")
    if json.loads(json.dumps(v15["c2_params"]["family"])) != ["BREAKOUT_FAILED", "LEVEL_REJECTED", "EDGE_RECLAIMED"]:
        raise RuntimeError("config/v15.json c2_params.family 冻结 = fade 家族")
    return v15


def _v15_params(rp_base: V6Params, band) -> V15Params:
    """canonical V6Params 逐项提升 + v15 带沿条款(合成臂路由参数)。"""
    kw = {f: getattr(rp_base, f) for f in V6Params.__dataclass_fields__}
    return V15Params(**kw, band_side=band)


def build_arm(raw_cfg: dict, arm_id: str) -> tuple:
    """臂定义 → (band|None, router_factory, ArmConfig|None)。

    arm0:(None, None, None)= plain V6Router + arm=None ⇒ 逐位 canonical;
    v15:V15Router(band_side 条款)+ ArmConfig(C2 仅 fade 家族,C3 续涨
    stop-entry,分层/配档/virtual 全关)。
    """
    spec = raw_cfg["arms"][arm_id]
    if arm_id == "arm0":
        return None, None, None
    bp = BandSideParams(
        **{k: raw_cfg["band_side_params"][k]
           for k in ("pos_short_min", "pos_long_max")}) \
        if spec["band_side"] else None
    arm = ArmConfig(fill_model=spec["fill_model"],
                    stop_entry=bool(spec["c3_stop_entry"]),
                    stop_wait_tf=int(raw_cfg["c3_params"]["stop_wait_tf"]),
                    ladder_fade=bool(spec["c4_laddering"]),
                    k_bm=float(raw_cfg["c3_params"]["k_bm"]),
                    c2_family=tuple(raw_cfg["c2_params"]["family"]))
    return bp, V15Router, arm


# ---------------------------------------------------------------------------
# 逐笔增广(v15):交易入场语义补丁(不变 canonical;只对 v15 新语义的账目修正)
# ---------------------------------------------------------------------------


def augment_trades_v15(trades: list, frames: dict, ledger: list) -> list:
    """rb.augment_trades + v15 入账修正:

    - stop_entry 行 meta.entry_mode 覆盖 canonical 二分标签(归因准确;arm0
      账本无新键 ⇒ 逐位旧值);maker 真挂单保 LIMIT_FILL/limit 本相不改;
    - band_pos / order_type / stop_ref 溯源列;
    - signal_decision_ts = armed_at_ts ∪ open_decision_ts(同 v14 口径,
      硬匹配 v15 vs arm0 同信号用)。
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
        out.append({**t,
                    "order_type": m.get("order_type"),
                    "band_pos": m.get("band_pos"),
                    "stop_ref": m.get("stop_ref"),
                    "signal_decision_ts": int(m.get("armed_at_ts")
                                              or t["open_decision_ts"])})
    return out


COLUMNS_V15 = list(rb.TRADE_COLUMNS) + ["order_type", "band_pos", "stop_ref",
                                        "signal_decision_ts"]


def trades_frame_v15(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": rb._iso(t["open_decision_ts"]),
                     "open_iso": rb._iso(t["open_ts"]),
                     "close_decision_iso": rb._iso(t["close_decision_ts"]),
                     "exit_iso": rb._iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=COLUMNS_V15)


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
        res.trades = augment_trades_v15(res.trades, frames, res.ledger)
        m = compute_metrics(res)
        out[mult] = (res, m)
        c = res.counters
        print(f"      [bt] {segment} cost {mult}x: net={m['net_pnl']:+.2f} "
              f"n={m['n_trades']} band_rej={c.get('band_side_reject_pos', 0)} "
              f"({time.time() - t0:.0f}s)", flush=True)
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
                     **{f"v15_{k}": m[k] for k in METRIC_EQ},
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
    maker_armed = g("maker_entry_armed", 0)
    return {
        "band_side_evaluated": g("band_side_evaluated", 0),
        "band_side_reject_pos": g("band_side_reject_pos", 0),
        "stop_entry_armed": stopex_armed,
        "stop_entry_triggers": g("stop_entry_triggers", 0),
        "stop_entry_fills": g("stop_entry_fills", 0),
        "stop_entry_expired": g("stop_entry_expired", 0),
        "stop_entry_canceled": g("stop_entry_canceled", 0),
        "stopex_fill_rate": (g("stop_entry_fills", 0) / stopex_armed
                             if stopex_armed else None),
        "stopex_miss_rate": (g("stop_entry_expired", 0) / stopex_armed
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
    params = Params.from_json(CONFIG_V15)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    tf_list = list(raw_cfg["tf_list"])
    if tf_list != ["1h", "2h", "4h", "12h", "1d"]:
        raise RuntimeError(f"config/v15.json tf_list 冻结: {tf_list}")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v15 parent=canonical:merge_max_atr 必须 2.0")

    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    print("[prep] 全战役帧(canonical 壳,一次性)", flush=True)
    frames, compaction_report = rb.prepare_frames(raw_cfg, params, tf_list)
    xcheck = rb.cross_checks(frames, float(raw_cfg["k_is"]), merge_max_atr=2.0)

    frozen_turtle = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    frozen_v13 = json.loads(FROZEN_V13.read_text(encoding="utf-8"))

    # -- 锚①:FROZEN_ERA 名单(8 档)探针 == backtest_v6_turtle 冻结产物 ---------
    print("[anchor-1] 冻结时代名单探针(dev 三格;arm=None)", flush=True)
    rp_probe = dc_replace(rp_base, whitelist=frozenset(FROZEN_ERA_WHITELIST))
    probe = run_grid(frames, raw_cfg, params, rp_probe, None, None, contracts,
                     segment="dev")
    parity_probe = assert_anchor("FROZEN_ERA 探针", probe,
                                 frozen_turtle["runs"], "turtle|dev")
    print("[anchor-1] 探针 3 格 == canonical v6 臂 B 冻结产物(逐格全等)",
          flush=True)

    # -- arm0 / v15(dev 三格) ----------------------------------------------------
    dev: dict = {}
    for arm_id in ARM_IDS:
        bp, factory, arm = build_arm(raw_cfg, arm_id)
        rp_use = rp_base if arm_id == "arm0" else _v15_params(rp_base, bp)
        print(f"[arm] {arm_id}: {raw_cfg['arms'][arm_id]['note']}", flush=True)
        dev[arm_id] = run_grid(frames, raw_cfg, params, rp_use, factory, arm,
                               contracts, segment="dev")

    # -- 锚②:arm0 == v13 在册 bar 臂(现行 16 档,Engine 零回归) ----------------
    parity_arm0 = assert_anchor("arm0==v13 bar", dev["arm0"],
                                frozen_v13["runs"], "bar|dev")
    print("[anchor-2] arm0 3 格 == v13 在册 bar 臂(逐格全等)", flush=True)

    # -- 合成判定(协议 §判定:dev@1.5x 净期望 v15−arm0,如实打回不打包) ------------
    e0 = dev["arm0"][JUDGE_COST][1]["expectancy_usdt"]
    e1 = dev["v15"][JUDGE_COST][1]["expectancy_usdt"]
    composite_verdict = {
        "base": "arm0", "delta_expectancy_usdt": (e1 - e0)
        if e0 is not None and e1 is not None else None,
        "delta_net_pnl": dev["v15"][JUDGE_COST][1]["net_pnl"]
        - dev["arm0"][JUDGE_COST][1]["net_pnl"],
        "verdict": ("effective(改善)" if (e0 is not None and e1 is not None
                                          and (e1 - e0) > 0.0)
                    else "打回(无改善)"),
        "component_attribution_note": ("逐组件归因在 v14 arm 矩阵已裁决完成"
                                       "(log#28:C2-ex/C3 effective,C1 联合"
                                       "配档打回⇒v15 仅留带沿条款、Q2-Q5 全"
                                       "弃);本轮不重复拆臂,按格如实对照。")}

    # -- 条款生效统计 / 成交差归因 -------------------------------------------------
    clauses = {a: {f"{mult}x": clause_stats(dev[a][mult][0].counters)
                   for mult in COST_MULTS} for a in ARM_IDS}
    givebacks = {a: {f"{mult}x": rb.giveback_summary(dev[a][mult][0].trades)
                     for mult in COST_MULTS} for a in ARM_IDS}
    buckets = {a: {f"{mult}x": rb.bucket_tables(dev[a][mult][0].trades)
                   for mult in COST_MULTS} for a in ARM_IDS}
    fill_diffs = {
        "v15_vs_arm0_dev_1x": r14.two_mode_diff(
            dev["v15"][1.0][0].trades, dev["arm0"][1.0][0].trades),
        "v15_vs_arm0_dev_1_5x": r14.two_mode_diff(
            dev["v15"][1.5][0].trades, dev["arm0"][1.5][0].trades)}

    # -- test 段一次:v15 合成臂三格(第 18 次曝光,research-log #29) ---------------
    print("[test] v15 合成臂一次(第 18 次曝光,一次性三格;此后只许前向)",
          flush=True)
    bp, factory, arm = build_arm(raw_cfg, "v15")
    rp_use = _v15_params(rp_base, bp)
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
    arm0_test_frozen = {f"{mult}x":
                        _cell(frozen_v13["runs"][f"bar|test@{mult}x"]["metrics"])
                        for mult in COST_MULTS}
    verdict_test = {
        "exposure": "test 第 18 次评估曝光(research-log #29 预注册,一次性,"
                    "v15 轮此后只许前向):开发期只跑 dev;test 对 v15 合成臂 "
                    "一次跑齐 1/1.5/2x 三格;arm0 数字只读引用 v13 在册 bar 臂"
                    "冻结产物(不重复曝光)。",
        "v15": {"test_1x": _cell(test[1.0][1]),
                "test_1_5x": _cell(test[1.5][1]),
                "test_2x": _cell(test[2.0][1]),
                "test_net_pnl_mean_ci95_1x": list(ci),
                "clause_1x": clause_stats(test[1.0][0].counters),
                "dev_1x": _cell(dev["v15"][1.0][1]),
                "dev_1_5x": _cell(dev["v15"][1.5][1]),
                "dev_2x": _cell(dev["v15"][2.0][1])},
        "arm0_test_frozen_v13_bar": arm0_test_frozen,
        "upgrade_line": upgrade,
        "honesty_note": ("如实写 dev/test 对照,不渲染任何转正叙述;test<60 ⇒ "
                         "INSUFFICIENT_DATA;既往升级线逐条照报,不过线即 "
                         "Research Only 维持。"),
    }

    # -- CSV 产物(dev 双臂三格 + v15 test 三格;arm0 test 见 v13 冻结产物) ---------
    REPORTS.mkdir(exist_ok=True)
    for arm_id in ARM_IDS:
        for mult in COST_MULTS:
            suffix = "" if mult == 1.0 \
                else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v15(dev[arm_id][mult][0].trades).to_csv(
                REPORTS / f"trades_v15_{arm_id}__dev{suffix}.csv", index=False)
    for mult in COST_MULTS:
        suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
        trades_frame_v15(test[mult][0].trades).to_csv(
            REPORTS / f"trades_v15_v15__test{suffix}.csv", index=False)

    # -- JSON 产物 -----------------------------------------------------------------
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "doc": "docs/v15-protocol.md(冻结,2026-10-20)",
            "construction": "四件套合成(v14 arm 矩阵裁决 log#28 存活件):①位置"
                            "带沿条款(v14 Q1 语义,只拒不配档)②fade 家族"
                            "(BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED)exchange_maker 真挂单 ③续涨家族"
                            "(BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED)stop-entry 预埋 ④收割/费用/容量/组合 "
                            "3% 沿 canonical 不变;明确丢开 Q2 空间闸(误杀版)/"
                            "Q3-Q4 配档与 tiersize/virtual 挂单/分层/级联。",
            "canonical_status": "arm0 = 现 canonical(v6 turtle 固化语义 × "
                                "canonical.json 16 档语义路由);检测/收割/"
                                "成本/容量/触发零改动。",
            "parity_discipline": "双锚硬断言:FROZEN_ERA 探针 == backtest_v6_turtle "
                                 "冻结产物逐格全等(代码零回归);arm0 == v13 在册 "
                                 "bar 臂逐格全等(engine 零回归)。不等即 raise, "
                                 "本报告不落地。",
            "test_eval_declaration": verdict_test["exposure"],
            "judge_rule": ("协议 §判定:dev@1.5x 净期望(expectancy_usdt)v15 − "
                           "arm0 为主锚,Δ ≤ 0 ⇒ 合成如实打回;若合成仍负,不"
                           "打包不合。"),
            "upgrade_line": UPGRADE_LINE,
        },
        "config": {
            "config_file": "config/v15.json",
            "parent": "config/canonical.json(共享键逐项一致,运行前硬对账)",
            "arms": {a: raw_cfg["arms"][a]["note"] for a in ARM_IDS},
            "band_side_params": raw_cfg["band_side_params"],
            "c2_params": raw_cfg["c2_params"],
            "c3_params": raw_cfg["c3_params"],
            "off_note": raw_cfg["off_note"],
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
        "giveback": givebacks,
        "bucket_tables": buckets,
        "fill_diffs": fill_diffs,
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
                "giveback": givebacks[arm_id][f"{mult}x"],
                "buckets": buckets[arm_id][f"{mult}x"]}
    for mult in COST_MULTS:
        res, m = test[mult]
        payload["runs"][f"v15|test@{mult}x"] = {
            "metrics": m, "counters": res.counters,
            "clause": clause_stats(res.counters),
            "giveback": rb.giveback_summary(res.trades)}

    (REPORTS / "v15.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "v15.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/v15.json/.md + trades_v15 CSVs "
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
        "# v15 四件套合成回测报告(带沿条款 × fade maker 真挂单 × 续涨 "
        "stop-entry × canonical 其余)",
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
        "## 1. 合成总览(dev;权益 300 USDT 起)",
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

    cv = payload["composite_verdict"]
    lines += ["", "## 2. 合成判定(dev@1.5x 净期望对照,协议 §判定;不虚饰)", "",
              "| 对照 | Δ净期望USDT | Δ净盈亏 | 裁决 |",
              "|---|---|---|---|",
              f"| v15 − arm0 | {_f(cv['delta_expectancy_usdt'], 4)} "
              f"| {_f(cv['delta_net_pnl'])} | **{cv['verdict']}** |",
              "",
              f"> {cv['component_attribution_note']}",
              "",
              "## 3. 条款生效统计(dev@1.5x;带沿拒绝、stopex、maker)", "",
              "| arm | 带沿评 | 带沿拒 | stop埋 | 触发 | 成交 | 漏单 | "
              "成交率 | maker埋 | maker成 | 过期 | 成交率 | 容量跳单 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in ARM_IDS:
        c = payload["clause_stats"][a][f"{JUDGE_COST}x"]
        lines.append(
            f"| {a} | {c['band_side_evaluated']} | {c['band_side_reject_pos']} "
            f"| {c['stop_entry_armed']} | {c['stop_entry_triggers']} "
            f"| {c['stop_entry_fills']} | {c['stop_entry_expired']} "
            f"| {_f(c['stopex_fill_rate'], 3)} | {c['maker_entry_armed']} "
            f"| {c['maker_entry_fills']} | {c['maker_entry_expired']} "
            f"| {_f(c['maker_fill_rate'], 3)} | {c['portfolio_cap_skips']} |")

    lines += ["", "## 4. 成交差归因(v15 vs arm0,同事件×信号决策时刻硬匹配,"
              "方向带号)", ""]
    for tag, key in (("dev@1x", "v15_vs_arm0_dev_1x"),
                     ("dev@1.5x", "v15_vs_arm0_dev_1_5x")):
        d = payload["fill_diffs"][key]
        lines += [f"### {tag}", "",
                  f"- 匹配笔数 {d['n_matched']};v15 独有(arm0 错过)"
                  f" {d['n_exchange_only']};arm0 独有(v15 未成交:"
                  f"带沿拒/maker 或 stop 漏单){d['n_virtual_only']}",
                  f"- 入场价差 (px_v15 − px_arm0)·d.relative(bps):均 "
                  f"{_f(d['dpx_rel_direction_signed']['mean_bps'], 2)} "
                  f"| 中位 {_f(d['dpx_rel_direction_signed']['median_bps'], 2)}"
                  "(正 = v15 入场更贵;负 = maker/stop 入场更优)",
                  f"- 费用差 arm0−v15:均 "
                  f"{_f(d['dfee_virtual_minus_exchange']['mean'], 6)} USDT | Σ "
                  f"{_f(d['dfee_virtual_minus_exchange']['sum'], 4)} USDT"
                  "(正 = v15 费用更省,maker 0.02% vs taker 0.06%+滑点)", ""]

    to = payload["test_once"]
    up = to["upgrade_line"]
    lines += ["", "## 5. test 一次(第 18 次曝光;如实写 dev/test 对比,"
              "不渲染转正叙述)", "",
              f"- {to['exposure']}", "",
              "| arm | 段@成本 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|---|"]
    for a, cells in (("v15", (("dev@1x", to["v15"]["dev_1x"]),
                              ("dev@1.5x", to["v15"]["dev_1_5x"]),
                              ("test@1x", to["v15"]["test_1x"]),
                              ("test@1.5x", to["v15"]["test_1_5x"]),
                              ("test@2x", to["v15"]["test_2x"]))),
                     ("arm0(v13 bar 冻结引用)",
                      tuple((f"test@{m}x", to["arm0_test_frozen_v13_bar"][f"{m}x"])
                            for m in COST_MULTS))):
        for seg, c in cells:
            lines.append(
                f"| {a} | {seg} | {_f(c['net_pnl'])} | {c['n_trades']} "
                f"| {_f(c['winrate'], 3)} | {_f(c['profit_factor'])} "
                f"| {_f(c['expectancy_usdt'], 4)} "
                f"| {_f(c['expectancy_r'], 4)} |")
    lo, hi = to["v15"]["test_net_pnl_mean_ci95_1x"]
    lines += ["",
              f"- v15 test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,"
              f"2000 次,固定种子;如实报告):{_f(lo, 4)} ~ {_f(hi, 4)} USDT",
              "- 既往升级线逐条照报:",
              f"  - test 笔数:{up['n_trades_test_1x']} ⇒ {up['sample_size']}",
              f"  - 1.5x 净期望:{_f(up['expectancy_usdt_test_1_5x'], 4)} "
              f"USDT ⇒ {'>0 ✔' if up['expectancy_positive_1_5x'] else '≤0 ✘'}",
              f"  - 2x PF:{_f(up['profit_factor_test_2_0x'])} "
              f"⇒ {'>1 ✔' if up['pf_gt_1_2_0x'] else '≤1 ✘'}",
              f"  - **判定:{up['verdict']}**",
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
              "① fade maker 真挂单即既有严格穿过 exchange maker 语义,排队"
              "乐观性如实声明(穿透缺口按限价成交,不给价格改善);② maker "
              "未在 N_fill=12 窗口触及即过期漏单(条款生效统计 §3 如实,"
              "arm0 市价保成交的对照即 §4 成交差);③ C3 stop-entry 触发"
              "用严格穿过(持平不触发,同保守口径),触发后次细 bar 开盘 "
              "taker+滑点;超时 12×TF 自决策 bar 最末细 bar 起锚作废,漏"
              "单单独统计;④ 带沿 pos 取 quality.swing_band_pos 同口径"
              "(全名单最近 LOW/HIGH pivot;≤8 窗口与全名单等价,因 pivot "
              "严格高低交错),缺侧/退化 fail-closed 拒;⑤ 带沿只拒不配档"
              "(tiersize=false):size_mult 不上账本,定仓/杠杆/容量与 "
              "canonical 逐位;⑥ 组合 3% 容量闸在 arm 分流之前,口径与 "
              "canonical 一致;⑦ 差价归因按(战役×信号决策时刻×事件)硬匹"
              "配,匹配不上者单列不做插补;⑧ arm0 test 数字只读引用 v13 "
              "在册 bar 臂冻结产物(arm0==v13 bar dev 逐格已硬对账),不"
              "重复曝光;⑨ test 曝光纪律见 §5,test<60 ⇒ INSUFFICIENT_DATA "
              "不另立判据。", "",
              "机器可读全量:`reports/v15.json`;逐笔:"
              "`reports/trades_v15_{arm0,v15}__{dev,test}[_cost*].csv`"
              "(增列 order_type/band_pos/stop_ref/signal_decision_ts;"
              "arm0 CSV 为在册 canonical 16 档基线重放,与 v13 bar 臂已硬"
              "对账;arm0 test 逐笔见 v13 冻结产物)。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
