"""v6 回测全管线(docs/v6-protocol.md §1-§4):多尺度战役 × 语义修复(S1/S2)
× A/B 二臂判选 × 9 格(主臂 full/dev/test;对照臂 dev-only;成本 1x/1.5x/2x)。

用法:  python scripts/run_backtest_v6.py
产物:  reports/backtest_v6.json / backtest_v6.md
       reports/trades_v6_{arm}_{seg}[_cost1_5,_cost2_0].csv
       reports/equity_v6_{arm}_{seg}[_cost1_5,_cost2_0].csv

纪律(v6-protocol,冻结于任何 v6 运行之前):
- 战役:3 symbol × TF ∈ {1h,2h,4h,12h,1d} = 15 战役/(臂),隔离互不知晓;
  共享 equity + 组合容量 3%(最坏情景,按信号时间 FIFO 整笔跳过并记
  PORTFOLIO_CAP;成交时刻由 PR#9 已修 broker 口径第二层钳制,如实声明)。
- 语义修复:S1 失效锚定结构区边界(E1/E7 沿用检测器冻结口径即区缘 ±
  k_bm·atr;E6 重锚含 ref 的区缘 ± buf,无区拒);F1(k_is=0.8)闸保留。
  S2:臂 A(clamp2R)target_1 = 决策收盘 ± 2.0R(实际 entry-invalid 计),
  room < 1.5R 拒,减仓 50% 后禁保本、只 trailing;臂 B(turtle,dev-only)
  无目标、初始防线 2.0·atr、trailing 只紧不松、关时间止损。§8.1/§8.5
  (§8.6 仅臂 B 关)不变;加仓仍关闭。
- 路由 = v5 质量路由(E1 市价优先 / E6 STRONG 市价 WEAK=E7 前缘限价)双向。
- 执行路径:TF=1h → 15m;TF ≥ 2h → 1h(认知 bar 区间 [t,t+TF) 由细帧 bar
  无洞组成,对齐断言,违即 raise)。
- 臂选择(只允许选臂,不允许调参):dev@1.5x 净期望较大者;两臂 dev@1.5x
  净期望均 ≤ 0 则取显著性较好者(逐笔净额 t 统计大者)并在报告声明;test
  只许跑判选臂一次(§4:test 段第 10 次曝光,终生声明后绝不再用于迭代)。
- 样本量:test 合并战役成交 < 60 ⇒ INSUFFICIENT_DATA,报告不给转正叙述;
  升级线:test≥60 ∧ 1.5x 净期望>0 ∧ 2x PF>1(主判定单元 = 合并战役)。
- 对账硬约束(任一不符即 raise):15m↔1h(v4 同契约)+ 1h↔TF compaction
  OHLC 逐格一致;E7 发射/过 F1 计数(1h 战役)== reports/backtest_v2.json
  冻结产物;E1+E6|short(1h 全历史)== reports/backtest.json(v1)go_events
  (v1 parity 锚点);各 (symbol, TF≠1h) E7 计数 ==
  reports/event_study_v3.json(逐格,多 TF 证据锚)。
- PR#9 的 opportunity.py 因果生命周期管线不参与本回测(其 15m 保护性止损/
  执行账本为独立前向设施;本管线 §8 决策以认知 TF 收盘为口径,与 v1-v5 同;
  如实声明共存,不混用)。
- 合约规格强制离线缓存(同 v3/v4/v5 纪律;data/contracts.json 逐条目
  sha256 校验)。
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import aggregate as agg                     # noqa: E402
from market_operator import data as mo_data                      # noqa: E402
from market_operator.backtest_v6 import compute_metrics, prepare_cognition  # noqa: E402
from market_operator.campaign import (                           # noqa: E402
    ARM_CLAMP2R,
    ARM_TURTLE,
    build_campaign_frame,
    campaign_key,
    run_campaigns,
)
from market_operator.backtest_v6 import alignment_issues_ci      # noqa: E402
from market_operator.broker import CONTRACTS_PATH, ContractSpec  # noqa: E402
from market_operator.hypothesis import f1_invalid_distance_ok    # noqa: E402
from market_operator.research import block_bootstrap_ci          # noqa: E402
from market_operator.strategy_v6 import V6Params                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402

CONFIG_V6 = ROOT / "config" / "v6.json"
BT_V1_PATH = ROOT / "reports" / "backtest.json"
BT_V2_PATH = ROOT / "reports" / "backtest_v2.json"
ES_V3_PATH = ROOT / "reports" / "event_study_v3.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev")
COST_MULTS = (1.0, 1.5, 2.0)
ARMS = (ARM_CLAMP2R, ARM_TURTLE)
ARM_LABEL = {ARM_CLAMP2R: "臂A Target-Clamp(2R)", ARM_TURTLE: "臂B Turtle(dev对照)"}
MIN_TRADES_TEST = 60                       # v6-protocol §4 样本量阈
MECHANISMS = ("E1", "E6", "E7")
GIVEBACK_V1_BASELINE_R = 3.6               # v1 逐笔诊断回吐中位(协议 §4 锚点)
GIVEBACK_TARGET_R = 1.5                    # v6-protocol §4 目标阈值

TRADE_COLUMNS = [
    "trade_id", "campaign", "symbol", "tf", "direction", "arm", "route",
    "event_id", "regime", "r_mult_planned",
    "open_decision_ts", "open_decision_iso", "open_ts", "open_iso", "entry",
    "entry_mode", "fill_wait_bars", "limit_price",
    "qty", "notional", "used_leverage", "invalid", "target_1", "risk_usdt",
    "closed", "close_decision_ts", "close_decision_iso", "exit_ts", "exit_iso",
    "exit_price", "exit_kind", "hold_hours", "gross_pnl", "fees",
    "fees_maker", "fees_taker", "funding",
    "slippage_cost", "net_pnl", "r_multiple", "mfe_r", "giveback_r",
    "reason_open", "reason_reduce", "reason_exit",
]


def _iso(ts) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _median(xs: list) -> float:
    xs = sorted(x for x in xs if x is not None and not (isinstance(x, float) and x != x))
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else 0.5 * (xs[mid - 1] + xs[mid])


def _tstat(nets: list) -> float:
    """逐笔净额 t 统计(均正性显著性;两臂 dev 均负时的次序量)。"""
    a = np.asarray(nets, dtype=np.float64)
    if len(a) < 2 or a.std(ddof=1) <= 0.0:
        return 0.0 if len(a) >= 1 and float(a.mean()) == 0.0 else float(
            np.sign(float(a.mean())) * 1e9)
    return float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a))))


# ---------------------------------------------------------------------------
# 帧/认知成本装载(v5 同纪律:验证 + 对齐 + compaction + 哈希)
# ---------------------------------------------------------------------------


def load_contracts_cache(symbols: list) -> tuple:
    payload = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    specs = {}
    for s in symbols:
        ent = payload["entries"][s]
        ent_sha = hashlib.sha256(json.dumps(
            ent["raw"], sort_keys=True).encode("utf-8")).hexdigest()
        if ent_sha != ent["sha256"]:
            raise RuntimeError(f"contracts cache sha256 mismatch for {s}")
        specs[s] = ContractSpec.from_entry(ent["raw"])
    return specs, f"cache(offline enforced, cached_at={payload['fetched_at_utc']})"


def prepare_frames(raw_cfg: dict, params: Params, tf_list: list) -> dict:
    frames: dict = {}
    compaction_report: dict = {}
    for sym in raw_cfg["symbols"]:
        df_1h = mo_data.load(sym)
        df_15m = mo_data.load(sym, "15m")
        issues = mo_data.validate(df_15m, step_ms=900_000)
        if issues:
            raise RuntimeError(f"{sym} 15m validation: {issues[:5]}")
        align = mo_data.check_frame_alignment(df_15m, df_1h, step_fine_ms=900_000)
        if align:
            raise RuntimeError(f"{sym} 15m↔1h alignment: {align[:5]}")
        compact = agg.aggregate(df_15m, 1)
        exact_15m = all((compact[c] - df_1h[c]).abs().max() == 0.0
                        for c in ("open", "high", "low", "close"))
        if not exact_15m or not (compact["ts"] == df_1h["ts"]).all():
            raise RuntimeError(f"{sym} 15m compaction != 1h frame (OHLC/ts)")
        compaction_report[sym] = {"15m_vs_1h_exact": bool(exact_15m)}
        for tf in tf_list:
            t0 = time.time()
            if tf == "1h":
                df_cog, df_exec = df_1h, df_15m
            else:
                df_cog = agg.load_tf(sym, tf)
                df_exec = df_1h
                step = agg.TF_HOURS[tf] * 3_600_000
                issues_tf = mo_data.validate(df_cog, step_ms=step)
                if issues_tf:
                    raise RuntimeError(f"{sym} {tf} validation: {issues_tf[:5]}")
                expected = agg.aggregate(df_1h, agg.TF_HOURS[tf])
                ok = all((expected[c] - df_cog[c]).abs().max() == 0.0
                         for c in ("open", "high", "low", "close"))
                if not ok or not (expected["ts"] == df_cog["ts"]).all():
                    raise RuntimeError(f"{sym} {tf} 盘上帧 != 1h 聚合重算(漂移)")
                compaction_report[sym][f"1h_vs_{tf}_exact"] = bool(ok)
            cog = prepare_cognition(df_cog, params, sym, enable_e7=True)
            if tf == "1h":
                ci = alignment_issues_ci(cog.bars, [(int(r.ts),) for r in
                                                    df_15m.itertuples(index=False)],
                                         float(raw_cfg["dev_frac"]))
                if ci:
                    raise RuntimeError(f"{sym} boundary alignment: {ci}")
            f = build_campaign_frame(cog, df_exec, tf)
            frames[f.key] = f
            n_ev = sum(len(v) for v in cog.events_by.values())
            print(f"[frame] {sym}@{tf}: {len(df_cog)} 认知 bar / "
                  f"{len(f.exec_labels)} 执行 bar, {n_ev} events "
                  f"({time.time() - t0:.1f}s)", flush=True)
    return frames, compaction_report


# ---------------------------------------------------------------------------
# 冻结产物对账(v1 parity / v2 E7 / v3 多 TF E7)
# ---------------------------------------------------------------------------


def cross_checks(frames: dict, k_is: float) -> dict:
    bt_v2 = json.loads(BT_V2_PATH.read_text(encoding="utf-8"))
    bt_v1 = json.loads(BT_V1_PATH.read_text(encoding="utf-8"))
    es_v3 = json.loads(ES_V3_PATH.read_text(encoding="utf-8")) \
        if ES_V3_PATH.exists() else None
    e7_counts: dict = {}
    tf_counts: dict = {}
    atr_equal = True
    e1e6_short_total = 0
    for key, f in sorted(frames.items()):
        cog = f.cog
        fired, passed = [], []
        e1s = e6s = 0
        for i, evs in cog.events_by.items():
            for e in evs:
                if e.id == "E7" and e.attrs.get("atr") != cog.atrs[i]:
                    atr_equal = False
                if e.id == "E7":
                    fired.append(e)
                    if f1_invalid_distance_ok(e, float(cog.bars[i][4]), k_is):
                        passed.append(e)
                elif e.id == "E1" and e.direction == -1:
                    e1s += 1
                elif e.id == "E6" and e.direction == -1:
                    e6s += 1
        rec = {"fired": len(fired), "passed_f1": len(passed),
               "E1_short": e1s, "E6_short": e6s}
        if f.tf_label == "1h":
            e7_counts[f.symbol] = rec
            e1e6_short_total += e1s + e6s
            v2c = bt_v2["e7_counts"][f.symbol]
            if (v2c["whitelist_events"], v2c["passed_f1"]) \
                    != (len(fired), len(passed)):
                raise RuntimeError(
                    f"E7 计数对账失败 {f.symbol}: v6 fired={len(fired)} "
                    f"passed={len(passed)} vs v2 冻结产物 {v2c}")
        else:
            tf_counts[key] = rec
            if es_v3 is not None:
                es_c = es_v3["timeframes"][f.tf_label]["symbols"][f.symbol]
                if (es_c["n_events_fired"], es_c["n_passed_f1"]) \
                        != (len(fired), len(passed)):
                    raise RuntimeError(
                        f"E7 多TF对账失败 {key}: v6 fired={len(fired)} "
                        f"passed={len(passed)} vs event_study_v3 冻结产物 "
                        f"{es_c['n_events_fired']}/{es_c['n_passed_f1']}")
    if not atr_equal:
        raise RuntimeError("E7 attrs['atr'] != 认知缓存同 bar ATR(F1 口径漂移)")
    v1_go = bt_v1["runs"]["full@1.0x"]["metrics"]["counters"]["go_events"]
    if e1e6_short_total != v1_go:
        raise RuntimeError(f"E1+E6|short 计数对账失败: v6 认知={e1e6_short_total}"
                           f" vs v1 冻结产物 go_events={v1_go}")
    print(f"[xcheck] E7 1h 计数 == v2 冻结产物;E7 多TF计数 == event_study_v3"
          f"({'在场' if es_v3 is not None else '缺失,跳过'});E1+E6|short="
          f"{e1e6_short_total} == v1 go_events={v1_go}(v1 parity);E7 atr 一致",
          flush=True)
    return {"e7_counts_1h": e7_counts, "e7_counts_tf": tf_counts,
            "e1e6_short_vs_v1": {"v6": e1e6_short_total, "v1": v1_go},
            "e7_attrs_atr_equals_cognition_atr": atr_equal,
            "es_v3_tf_match": es_v3 is not None}


# ---------------------------------------------------------------------------
# 逐笔增广:MFE_R / 回吐 R(战役认知帧 OHLC)
# ---------------------------------------------------------------------------


def augment_trades(trades: list, frames: dict, arm: str) -> list:
    out = []
    for t in trades:
        f = frames[t["campaign"]]
        ms_tf = f.tf_hours * 3_600_000
        label_of = {int(b[0]): i for i, b in enumerate(f.cog.bars)}
        e_i = label_of.get(t["open_ts"] - (t["open_ts"] % ms_tf))
        x_i = e_i if t["exit_ts"] is None else \
            label_of.get(t["exit_ts"] - (t["exit_ts"] % ms_tf))
        mfe_r = None
        if e_i is not None and x_i is not None and t["risk_usdt"] and \
                t["entry"] and t["invalid"]:
            d = t["direction"]
            risk_px = abs(t["entry"] - t["invalid"])
            highs = max(b[2] for b in f.cog.bars[e_i:x_i + 1])
            lows = min(b[3] for b in f.cog.bars[e_i:x_i + 1])
            mfe_px = (highs - t["entry"]) if d > 0 else (t["entry"] - lows)
            mfe_r = mfe_px / risk_px if risk_px > 0 else None
        out.append({**t, "arm": arm, "mfe_r": mfe_r,
                    "giveback_r": (mfe_r - t["r_multiple"]
                                   if mfe_r is not None
                                   and t["r_multiple"] is not None else None)})
    return out


def giveback_summary(trades: list) -> dict:
    gbs = [t["giveback_r"] for t in trades if t["closed"]
           and t["giveback_r"] is not None and t["in_segment_open"]]
    mfes = [t["mfe_r"] for t in trades if t["closed"]
            and t["mfe_r"] is not None and t["in_segment_open"]]
    return {"n": len(gbs), "giveback_median_r": _median(gbs),
            "mfe_median_r": _median(mfes),
            "v1_baseline_r": GIVEBACK_V1_BASELINE_R,
            "target_r": GIVEBACK_TARGET_R}


# ---------------------------------------------------------------------------
# 分桶:机制 × TF × 方向(协议强制分桶表)
# ---------------------------------------------------------------------------


def bucket_tables(trades: list) -> dict:
    cells: dict = {}
    for t in trades:
        if not t["in_segment_open"]:
            continue
        key = (t["event_id"], t["tf"], "long" if t["direction"] > 0 else "short")
        g = cells.setdefault(key, {"n": 0, "n_closed": 0, "wins": 0,
                                   "rs": [], "net": 0.0})
        g["n"] += 1
        g["net"] += t["net_pnl"]
        if t["closed"]:
            g["n_closed"] += 1
            if t["net_pnl"] > 0:
                g["wins"] += 1
            if t["r_multiple"] is not None:
                g["rs"].append(t["r_multiple"])
    out = {}
    for (mech, tf, side), g in sorted(cells.items()):
        out.setdefault(mech, {}).setdefault(tf, {})[side] = {
            "n_trades": g["n"],
            "winrate": (g["wins"] / g["n_closed"]) if g["n_closed"] else None,
            "avg_r": (sum(g["rs"]) / len(g["rs"])) if g["rs"] else None,
            "net_pnl": g["net"]}
    return out


def signal_counts(frames: dict, dev_frac: float) -> dict:
    """dev 段信号数(机制 × TF × 方向;认知缓存直数,与路由无关)。"""
    from market_operator.research import split_boundary
    cells: dict = {}
    for key, f in sorted(frames.items()):
        b = split_boundary(len(f.cog.bars), dev_frac)
        for i, evs in f.cog.events_by.items():
            if not (0 <= i < b):
                continue
            for e in evs:
                if e.id not in MECHANISMS:
                    continue
                k = (e.id, f.tf_label, "long" if e.direction > 0 else "short")
                cells[k] = cells.get(k, 0) + 1
    return {f"{m}×{tf}×{side}": n
            for (m, tf, side), n in sorted(cells.items())}


def trades_frame(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": _iso(t["open_decision_ts"]),
                     "open_iso": _iso(t["open_ts"]),
                     "close_decision_iso": _iso(t["close_decision_ts"]),
                     "exit_iso": _iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def equity_frame(res) -> pd.DataFrame:
    return pd.DataFrame(
        [(ts, _iso(ts), eq, cash, upnl, fnd, n)
         for ts, eq, cash, upnl, fnd, n in res.equity_rows],
        columns=["ts", "iso", "equity", "cash", "unrealized",
                 "funding_accrued", "n_open"])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(CONFIG_V6.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V6)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    tf_list = list(raw_cfg["tf_list"])
    if tf_list != ["1h", "2h", "4h", "12h", "1d"]:
        raise RuntimeError(f"config/v6.json tf_list 冻结为 "
                           f"['1h','2h','4h','12h','1d']: {tf_list}")
    if raw_cfg["arm"] not in (ARM_CLAMP2R, ARM_TURTLE):
        raise RuntimeError(f"config/v6.json arm 非法: {raw_cfg['arm']}")
    if raw_cfg["entry_mode"] != "quality_routed":
        raise RuntimeError("config/v6.json entry_mode must be quality_routed")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(arm=ARM_CLAMP2R, k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       r_min=float(raw_cfg["r_min"]),
                       target_r=float(raw_cfg["target_r"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    cap_frac = float(raw_cfg["portfolio_cap_frac"])
    n_fill = int(raw_cfg["n_fill"])
    if int(raw_cfg["exec_tf_seconds"]) != 900:
        raise RuntimeError("exec_tf_seconds 冻结为 900(15m)")
    equity0 = 300.0

    contracts, spec_src = load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    frames, compaction_report = prepare_frames(raw_cfg, params, tf_list)
    xcheck = cross_checks(frames, float(raw_cfg["k_is"]))

    broker_kw = dict(dev_frac=dev_frac, equity0=equity0,
                     risk_frac=float(raw_cfg["risk_frac"]),
                     leverage_cap=float(raw_cfg["leverage_cap"]),
                     taker_fee=float(raw_cfg["taker_fee"]),
                     maker_fee=float(raw_cfg["maker_fee"]),
                     slippage_per_side=float(raw_cfg["slippage_per_side"]),
                     funding_per_8h=float(raw_cfg["funding_per_8h_per_side"]),
                     contracts=contracts, params=params, n_fill=n_fill,
                     portfolio_cap_frac=cap_frac)

    def run_one(arm: str, seg: str, mult: float) -> tuple:
        res = run_campaigns(frames, arm=arm, segment=seg,
                            cost_multiplier=mult,
                            router_params=V6Params(
                                arm=arm, k_dq=rp_base.k_dq, k_imp=rp_base.k_imp,
                                k_is=rp_base.k_is, r_min=rp_base.r_min,
                                target_r=rp_base.target_r,
                                turtle_stop_atr=rp_base.turtle_stop_atr,
                                k_bm=rp_base.k_bm, whitelist=whitelist),
                            **broker_kw)
        res.trades = augment_trades(res.trades, frames, arm)
        return res, compute_metrics(res)

    results: dict = {}
    for arm in ARMS:
        for seg in SEGMENTS:
            for mult in COST_MULTS:
                t0 = time.time()
                res, m = run_one(arm, seg, mult)
                results[(arm, seg, mult)] = (res, m)
                c = res.counters
                print(f"[bt] {arm:8s} {seg:4s} cost {mult}x: net={m['net_pnl']:+.2f} "
                      f"trades={m['n_trades']} fills={c['opens_filled']}+"
                      f"{c['limit_fills_entry']} cap_skips={c['portfolio_cap_skips']} "
                      f"({time.time() - t0:.1f}s)", flush=True)
    liq = sum(results[(a, s, mm)][0].counters["liquidations"]
              for a in ARMS for s in SEGMENTS for mm in COST_MULTS)
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")

    # -- 臂判选(仅 dev 证据;test 尚未运行) -------------------------------------
    def dev_stats(arm: str) -> dict:
        _, m = results[(arm, "dev", 1.5)]
        nets = [t["net_pnl"] for t in results[(arm, "dev", 1.5)][0].trades
                if t["in_segment_open"]]
        return {"expectancy_usdt": m["expectancy_usdt"], "net_pnl": m["net_pnl"],
                "n_trades": m["n_trades"], "t_stat": _tstat(nets)}

    sel_a, sel_b = dev_stats(ARM_CLAMP2R), dev_stats(ARM_TURTLE)
    both_neg = (sel_a["expectancy_usdt"] is not None
                and sel_b["expectancy_usdt"] is not None
                and sel_a["expectancy_usdt"] <= 0.0
                and sel_b["expectancy_usdt"] <= 0.0)
    if not both_neg:
        winner = (ARM_CLAMP2R if (sel_a["expectancy_usdt"] or float("-inf"))
                  >= (sel_b["expectancy_usdt"] or float("-inf"))
                  else ARM_TURTLE)
        rule = "dev@1.5x 净期望较大者"
    else:
        winner = (ARM_CLAMP2R if sel_a["t_stat"] >= sel_b["t_stat"]
                  else ARM_TURTLE)
        rule = ("两臂 dev@1.5x 净期望均 <= 0,取显著性较好者(t 统计较大);"
                "如实声明,非转正证据")
    arm_selection = {"rule": rule, "both_dev_negative": both_neg,
                     "winner": {"arm": winner, "label": ARM_LABEL[winner]},
                     "clamp2R": sel_a, "turtle": sel_b}
    print(f"[arm] A exp={sel_a['expectancy_usdt']:+.4f} t={sel_a['t_stat']:+.2f} | "
          f"B exp={sel_b['expectancy_usdt']:+.4f} t={sel_b['t_stat']:+.2f} -> "
          f"winner={winner} ({rule})", flush=True)

    # -- test 一次性(只跑判选臂) -----------------------------------------------
    for mult in COST_MULTS:
        t0 = time.time()
        res, m = run_one(winner, "test", mult)
        results[(winner, "test", mult)] = (res, m)
        print(f"[bt] {winner:8s} test cost {mult}x: net={m['net_pnl']:+.2f} "
              f"trades={m['n_trades']} ({time.time() - t0:.1f}s)", flush=True)
    liq_t = sum(results[(winner, "test", mm)][0].counters["liquidations"]
                for mm in COST_MULTS)
    if liq_t != 0:
        raise RuntimeError(f"test 段强平非预期触发: {liq_t}")

    # -- 样本量与升级判据(先于叙述) ---------------------------------------------
    m_test_1x = results[(winner, "test", 1.0)][1]
    exp_test_15x = results[(winner, "test", 1.5)][1]["expectancy_usdt"]
    pf_2x = results[(winner, "test", 2.0)][1]["profit_factor"]
    n_test = m_test_1x["n_trades"]
    sample = {
        "n_trades_test_1x": n_test, "threshold": MIN_TRADES_TEST,
        "sample_size_verdict": ("SUFFICIENT(>=60)" if n_test >= MIN_TRADES_TEST
                                else "INSUFFICIENT_DATA"),
        "honesty_note": ("test<60 ⇒ INSUFFICIENT_DATA:不产生任何'看起来转正'"
                         "的叙述;合规补证手段 = 前向 paper 积累,不修回测曲线"),
    }
    upgrade = {
        "principal_unit": "合并战役(全部 symbol×TF 合并,与协议 §4 一致)",
        "criteria": {
            "n_trades_test_ge_60": n_test >= MIN_TRADES_TEST,
            "expectancy_test_1_5x_gt_0": bool(exp_test_15x is not None
                                              and exp_test_15x > 0.0),
            "pf_test_2x_gt_1": bool(pf_2x is not None and pf_2x > 1.0),
            "expectancy_test_usdt_1_5x": exp_test_15x,
            "pf_test_2x": pf_2x,
        },
        "verdict": ("PAPER_CANDIDATE" if (
            n_test >= MIN_TRADES_TEST
            and exp_test_15x is not None and exp_test_15x > 0.0
            and pf_2x is not None and pf_2x > 1.0) else "RESEARCH_ONLY"),
    }
    trades_test_1x = [t for t in results[(winner, "test", 1.0)][0].trades
                      if t["in_segment_open"]]
    if trades_test_1x:
        ci_lo, ci_hi = block_bootstrap_ci(
            np.asarray([t["net_pnl"] for t in trades_test_1x], dtype=np.float64),
            np.asarray([t["open_ts"] for t in trades_test_1x], dtype=np.int64))
    else:
        ci_lo = ci_hi = float("nan")
    sample["test_net_pnl_mean_ci95_1x"] = [ci_lo, ci_hi]

    # -- 强制分桶与回吐中位 R ----------------------------------------------------
    buckets: dict = {}
    givebacks: dict = {}
    cap_stats: dict = {}
    for (arm, seg, mult), (res, m) in sorted(results.items()):
        tag = f"{arm}|{seg}@{mult}x"
        buckets[tag] = bucket_tables(res.trades)
        givebacks[tag] = giveback_summary(res.trades)
        cap_stats[tag] = {
            "portfolio_cap_skips": res.counters.get("portfolio_cap_skips", 0),
            "cap_skips": res.cap_skips,
            "limits_armed": res.counters["limits_armed"],
            "limit_fills_entry": res.counters["limit_fills_entry"],
            "limit_expired": res.counters["limit_expired"],
            "opens_filled": res.counters["opens_filled"],
            "e1_priority_cancels": res.counters["e1_priority_cancels"],
            "reduce_limit_fills": res.counters["reduce_limit_fills"],
            "reduce_timeout_taker": res.counters["reduce_timeout_taker"],
            "min_size_skips": res.counters["min_size_skips"],
        }

    REPORTS.mkdir(exist_ok=True)
    for (arm, seg, mult), (res, m) in sorted(results.items()):
        suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
        trades_frame(res.trades).to_csv(
            REPORTS / f"trades_v6_{arm}_{seg}{suffix}.csv", index=False)
        equity_frame(res).to_csv(
            REPORTS / f"equity_v6_{arm}_{seg}{suffix}.csv", index=False)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "doc": "docs/v6-protocol.md(冻结于任何 v6 运行之前)",
            "meta": raw_cfg["meta"],
            "arm_selection": arm_selection,
            "test_eval_declaration": "test 段第 10 次曝光(v6 唯一一次;research-log "
                                     "#12 终生声明:此后绝不再用于研究迭代)",
            "opportunity_coexistence": "PR#9 opportunity.py 因果生命周期/15m 保护"
                                       "性止损管线不参与本回测;本管线 §8 决策以"
                                       "认知 TF 收盘为口径(v1-v5 同);两设施共存"
                                       "不混用,如实声明。",
        },
        "config": {
            "config_file": "config/v6.json",
            "equity0": equity0, "dev_frac": dev_frac,
            "risk_frac": broker_kw["risk_frac"],
            "leverage_cap": broker_kw["leverage_cap"],
            "taker_fee": broker_kw["taker_fee"],
            "maker_fee": broker_kw["maker_fee"],
            "slippage_per_side": broker_kw["slippage_per_side"],
            "funding_per_8h_per_side": broker_kw["funding_per_8h"],
            "cost_mults": list(COST_MULTS),
            "whitelist": [f"{i}|{'long' if d > 0 else 'short'}"
                          for i, d in sorted(whitelist)],
            "tf_list": tf_list, "k_is": rp_base.k_is, "k_dq": rp_base.k_dq,
            "k_imp": rp_base.k_imp, "r_min": rp_base.r_min,
            "target_r": rp_base.target_r,
            "turtle_stop_atr": rp_base.turtle_stop_atr,
            "portfolio_cap_frac": cap_frac,
            "n_fill": n_fill, "exec_tf_seconds": 900,
        },
        "contracts": {
            "source": spec_src, "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
            "cache_sha256": contracts_sha,
            "specs": {s: vars(c) for s, c in contracts.items()},
        },
        "conventions": {
            "campaign": "(symbol, TF) 战役隔离:自有认知/事件/策略/持仓/挂单;",
            "decision_ts": "认知 TF bar 标签(收盘决策)",
            "fill_ts": "市价 = 下一 TF bar 首根执行细 bar 开盘(TF=1h→15m,"
                       "TF≥2h→1h,决策须早于成交,HaR 断言);限价 = 细帧严格穿过"
                       "(fill_after = 决策 bar 最末细 bar;N_fill=12 墙钟小时)",
            "portfolio_cap": "全部战役合计 open 最坏情景(止损距离 + 费用预算) "
                             f"≤ {cap_frac}×权益,决策时刻 FIFO 整笔跳过"
                             "(counters.portfolio_cap_skips);成交时刻 PR#9 "
                             "broker 口径第二层缩量钳制(如发生,如实入账)",
        },
        "compaction_report": compaction_report,
        "cross_checks": xcheck,
        "dev_signal_counts": signal_counts(frames, dev_frac),
        "arm_selection": arm_selection,
        "sample_size": sample,
        "upgrade_v6": upgrade,
        "bucket_tables": buckets,
        "giveback": givebacks,
        "portfolio_capacity": cap_stats,
        "runs": {},
    }
    for (arm, seg, mult), (res, m) in sorted(results.items()):
        payload["runs"][f"{arm}|{seg}@{mult}x"] = {"metrics": m,
                                                   "trades": res.trades}

    (REPORTS / "backtest_v6.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v6.md").write_text(render_md(payload), encoding="utf-8")
    print(f"-> reports/backtest_v6.json/.md, trades_v6/equity_v6 CSVs "
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
    cfg = payload["config"]
    sel = payload["arm_selection"]
    winner = sel["winner"]["arm"]

    def m(tag):
        return runs[tag]["metrics"]

    insufficient = payload["sample_size"]["sample_size_verdict"] \
        == "INSUFFICIENT_DATA"

    lines = [
        "# v6 回测报告(docs/v6-protocol.md:出口几何修复 × 多尺度并行战役)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        "- 战役:3 symbol × TF ∈ {1h,2h,4h,12h,1d} = 15 战役/臂,隔离互不知晓;"
        f"共享权益 + 组合容量 {cfg['portfolio_cap_frac']}(最坏情景 FIFO 跳过,"
        "成交时刻 PR#9 broker 口径第二层钳制)。",
        f"- 语义修复(唯一策略变更):S1 失效锚定结构区边界(F1 k_is={cfg['k_is']}"
        " 闸保留);S2 臂 A clamp2R(target=决策收盘±2.0R,room<1.5R 拒,减仓后"
        "**禁保本**只 trailing)vs 臂 B turtle(dev-only:无目标,初始防线 "
        f"{cfg['turtle_stop_atr']}·atr,trailing 只紧不松,关时间止损)。"
        "§8.1/§8.5/§8.6(臂 B 除外)不变;加仓仍关闭。",
        f"- 路由 = v5 质量路由双向(E1 市价优先/E6 STRONG 市价 WEAK=E7 前缘限价;"
        f"k_dq={cfg['k_dq']} k_imp={cfg['k_imp']};白名单 {cfg['whitelist']})。",
        "- 执行路径:TF=1h→15m,TF≥2h→1h(对齐断言;fill_after = 决策 bar 最末"
        f"细 bar;N_fill={cfg['n_fill']} 墙钟小时)。",
        f"- **{payload['protocol']['test_eval_declaration']}**:本报告为该次评估"
        "的回测侧产物,此后不再以 test 迭代。",
        f"- 样本量(v6-protocol §4):test 合并战役成交 < "
        f"{payload['sample_size']['threshold']} ⇒ "
        f"**{payload['sample_size']['sample_size_verdict']}**"
        + ("——按协议**本报告不给出任何转正叙述**。" if insufficient else "。"),
        f"- {payload['protocol']['opportunity_coexistence']}",
        f"- 合约规格:{payload['contracts']['source']}(离线强制)。",
        "- 认知对账(全部硬约束一致):E7 1h 计数 == v2 冻结产物("
        + "; ".join(f"{s}={v['passed_f1']}/{v['fired']}"
                    for s, v in payload["cross_checks"]["e7_counts_1h"].items())
        + f");E7 多TF计数 == event_study_v3 冻结产物({len(payload['cross_checks']['e7_counts_tf'])} 格;"
        f"E1+E6|short={payload['cross_checks']['e1e6_short_vs_v1']['v6']} == "
        f"v1 go_events={payload['cross_checks']['e1e6_short_vs_v1']['v1']}"
        "(v1 parity 锚点,未破)。",
        "",
        "## 1. 臂判选(dev 证据,先于 test;只允许选臂,不允许调参)",
        "",
        "| 臂 | dev@1.5x 净期望USDT | dev@1.5x 净额 | 笔数 | t统计 |",
        "|---|---|---|---|---|",
        f"| {ARM_LABEL[ARM_CLAMP2R]} | {_f(sel[ARM_CLAMP2R]['expectancy_usdt'], 4)} "
        f"| {_f(sel[ARM_CLAMP2R]['net_pnl'])} | {sel[ARM_CLAMP2R]['n_trades']} "
        f"| {_f(sel[ARM_CLAMP2R]['t_stat'])} |",
        f"| {ARM_LABEL[ARM_TURTLE]} | {_f(sel[ARM_TURTLE]['expectancy_usdt'], 4)} "
        f"| {_f(sel[ARM_TURTLE]['net_pnl'])} | {sel[ARM_TURTLE]['n_trades']} "
        f"| {_f(sel[ARM_TURTLE]['t_stat'])} |",
        "",
        f"**判选规则**:{sel['rule']} ⇒ **test 只跑 {sel['winner']['label']}**"
        + ("(两臂 dev@1.5x 均 ≤ 0,如实声明:此为显著性次序选择,非转正证据)。"
           if sel["both_dev_negative"] else "。"),
        "",
        "## 2. 总览(臂 × 段 × 成本;test 仅判选臂;权益 300 USDT 起)",
        "",
        "| 臂 | 段 | 成本 | 净盈亏 | 收益率 | 最大回撤% | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R |",
        "|---|---|---|---|---|---|---|---|---|---|---|"]
    for (arm, seg, mult) in [(a, s, mm) for a in ARMS for s in SEGMENTS
                             for mm in COST_MULTS] + \
                            [(winner, "test", mm) for mm in COST_MULTS]:
        g = m(f"{arm}|{seg}@{mult}x")
        lines.append(
            f"| {ARM_LABEL[arm]} | {seg} | {mult}x | {_f(g['net_pnl'])} "
            f"| {_f(100 * g['return_pct'])}% "
            f"| {_f(100 * g['max_dd_pct'])}% "
            f"| {g['n_trades']} | {_f(g['winrate'], 3)} "
            f"| {_f(g['profit_factor'])} | {_f(g['expectancy_usdt'], 4)} "
            f"| {_f(g['expectancy_r'], 4)} |")

    lines += ["", "## 3. 回吐中位 R(核心判别量;战役认知帧 OHLC;v1 基线 3.6R,",
              f"协议目标 ≤ {GIVEBACK_TARGET_R}R)", "",
              "| 臂 | 段 | 成本 | n | MFE_R 中位 | 回吐中位 R |",
              "|---|---|---|---|---|---|"]
    for (arm, seg, mult) in [(a, s, mm) for a in ARMS for s in SEGMENTS
                             for mm in COST_MULTS] + \
                            [(winner, "test", mm) for mm in COST_MULTS]:
        gb = payload["giveback"][f"{arm}|{seg}@{mult}x"]
        lines.append(f"| {ARM_LABEL[arm]} | {seg} | {mult}x | {gb['n']} "
                     f"| {_f(gb['mfe_median_r'])} | {_f(gb['giveback_median_r'])} |")

    lines += ["", "## 4. 强制分桶(机制 × TF × 方向;笔数/胜率/平均R/净额)", ""]
    for (arm, seg, mult) in [(a, s, mm) for a in ARMS for s in SEGMENTS
                             for mm in COST_MULTS] + \
                            [(winner, "test", mm) for mm in COST_MULTS]:
        tag = f"{arm}|{seg}@{mult}x"
        lines += [f"### {ARM_LABEL[arm]} {seg}@{mult}x", "",
                  "| 机制 | TF | 方向 | 笔数 | 胜率 | 平均R | 净额 |",
                  "|---|---|---|---|---|---|---|"]
        bt = payload["bucket_tables"][tag]
        any_rows = False
        for mech in MECHANISMS:
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

    lines += ["### dev 段信号数(机制 × TF × 方向;认知缓存直数)", "",
              "| 桶 | 事件数 |", "|---|---|"]
    for k, v in payload["dev_signal_counts"].items():
        lines.append(f"| {k} | {v} |")

    lines += ["", "## 5. 组合容量(3% 最坏情景 FIFO 命中统计)", "",
              "| 臂 | 段 | 成本 | cap_skips | 市价成交 | 限价武装 | 限价成交 | 过期 | E1撤单 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for (arm, seg, mult) in [(a, s, mm) for a in ARMS for s in SEGMENTS
                             for mm in COST_MULTS] + \
                            [(winner, "test", mm) for mm in COST_MULTS]:
        cs = payload["portfolio_capacity"][f"{arm}|{seg}@{mult}x"]
        lines.append(f"| {ARM_LABEL[arm]} | {seg} | {mult}x "
                     f"| {cs['portfolio_cap_skips']} | {cs['opens_filled']} "
                     f"| {cs['limits_armed']} | {cs['limit_fills_entry']} "
                     f"| {cs['limit_expired']} | {cs['e1_priority_cancels']} |")

    up = payload["upgrade_v6"]
    ss = payload["sample_size"]
    lines += ["", "## 6. 样本量判定与升级判据(v6-protocol §4,先于叙述)", "",
              f"- test 合并战役成交笔数(判选臂 {ARM_LABEL[winner]},1x,OPEN 决策 ∈ "
              f"test):**{ss['n_trades_test_1x']}**(阈值 ≥ {ss['threshold']})⇒ "
              f"**{ss['sample_size_verdict']}**。",
              f"- test@1x 逐笔净盈亏均值 95% CI(自然日 block bootstrap,2000 次,"
              f"固定种子):{_f(ss['test_net_pnl_mean_ci95_1x'][0], 4)} ~ "
              f"{_f(ss['test_net_pnl_mean_ci95_1x'][1], 4)} USDT —— 如实报告。",
              "",
              "| 判据 | 值 | 通过 |", "|---|---|---|",
              f"| test 成交 ≥ 60(合并战役) | {up['criteria']['n_trades_test_ge_60']}"
              f" (n={ss['n_trades_test_1x']}) "
              f"| {'✓' if up['criteria']['n_trades_test_ge_60'] else '✗'} |",
              f"| test 净期望 > 0(1.5x) | {_f(up['criteria']['expectancy_test_usdt_1_5x'], 4)} "
              f"| {'✓' if up['criteria']['expectancy_test_1_5x_gt_0'] else '✗'} |",
              f"| test@2x PF > 1 | {_f(up['criteria']['pf_test_2x'])} "
              f"| {'✓' if up['criteria']['pf_test_2x_gt_1'] else '✗'} |",
              f"| **结论(全部满足才升级)** | — | **{up['verdict']}** |"]
    if insufficient:
        lines += ["",
                  "> INSUFFICIENT_DATA 处置(v6-protocol §4):不产生任何『看起来转"
                  "正』的叙述;样本量按格如实报告;两种结局都关闭协议级优化循环,基"
                  "础设施(战役引擎)保留为永久资产。"]

    lines += ["", "## 7. 已知近似与如实声明", "",
              "① E6→E7『同 episode』以 E7 attrs ref == 登记 ref 操作化(零检测器"
              "改动,浮点同源 1e-9);② 每战役至多保留最新一个 weak-E6 登记;③ 限"
              "价严格穿过才成交、穿透缺口按 limit 价成交(保守);④ 武装成交的认"
              "知 bar 不做 §8 裁决(同 v4/v5);⑤ 组合容量按决策时刻参考价估算"
              "(不取整,略高估=保守),成交时刻 PR#9 broker 口径第二层钳制;⑥ "
              "N_fill 按墙钟 12 小时统一(各 TF 不引入新先验);⑦ E1/E7 的 S1 "
              "invalid 即检测器冻结 attrs 区缘口径(无改动),E6 重锚含 ref 的"
              "结构区缘(无区拒,不编造宽度);⑧ 决策时刻容量检查为预算口径,"
              "不含资金费;强弱路由质量量(impulse/margin)以认知 TF bar 度量。"
              "",
              "机器可读全量:`reports/backtest_v6.json`;逐笔:"
              "`reports/trades_v6_*.csv`(增列 campaign/tf/arm/route/mfe_r/"
              "giveback_r);逐执行细 bar 权益:`reports/equity_v6_*.csv`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
