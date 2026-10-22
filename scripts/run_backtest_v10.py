"""v10 §A 触发麦克风回测管线(docs/v10-protocol.md,冻结 2026-10-20):
认知帧不变 × trigger 帧知时细化 × 多尺度战役 × turtle 唯一语义。

用法:  python scripts/run_backtest_v10.py
配置:  config/v10.json(parent=config/canonical.json,不改动;
       enable_mic=false 为默认/parity 臂,trigger_tf_seconds=900 主口径,
       300 仅 dev 敏感性)
产物:  reports/backtest_v10_turtle.json / backtest_v10_turtle.md
       reports/trades_v10_turtle_{bar,mic15m}__{seg}[_cost1_5,_cost2_0].csv
       reports/equity_v10_turtle_{bar,mic15m}__{seg}[_cost1_5,_cost2_0].csv
       (mic5m 臂仅 dev:trades_v10_turtle_mic5m__dev[_cost*].csv)

口径与纪律:
- 两臂同台:bar 臂 = enable_mic=False 走同一批(挂载 mic 认知的)战役帧 ⇒
  与 canonical 规范链产物 reports/backtest_v6_turtle.json(臂 B 冻结产物)
  逐格全等 —— 硬断言,不等即 raise(parity 锚);mic 臂 = enable_mic=True,
  同一战略/收割/费用/容量,唯一差异 = 事件知时与成交细化到 trigger 帧
  (协议 §A:N_a_mic=N_a×scale,N_f_mic=N_f×scale,阈值=认知帧 ATR)。
- 检测器零改动硬约束:per-战役 mic 帧内复放的 bar 事件流 ==
  prepare_cognition 逐字段(逐 id/direction/known_index/ref/invalid)断言。
- 裁决对比(报告 §2):① dev@1.5x 内部对照 mic vs bar;② dev@1.5x 对照
  canonical v6 臂 B 冻结产物;协议 §C:早到(≤45min@1h)仍不转正 ⇒ "晚报"
  假说死于"拥挤+薄利"双因,不追更早;5m 若 dev 无改善如实报,不引入生产。
- test 段曝光:第 13 次评估曝光(research-log #20 预注册,一次性,此后只许
  前向)。本脚本为唯一一次 v10 test 评估的执行入口;判据沿用 v6 升级线
  (n≥60 且 test 净期望@1.5x>0 且 test PF@2x>1 ⇒ PAPER_CANDIDATE,否则
  RESEARCH_ONLY),如实填报,不产生额外叙述。
- 5m:仅 dev 段敏感性(data/raw/{sym}_5m.csv 缺失 ⇒ 如实记 not_evaluated)。
- 合约规格强制离线缓存;freeze:历史产物(reports/backtest_v6_turtle.* 等)
  只读比对,不覆写。
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
sys.path.insert(0, str(ROOT / "scripts"))

import run_backtest_v6 as rb                                     # noqa: E402
from market_operator import aggregate as agg                     # noqa: E402
from market_operator import data as mo_data                      # noqa: E402
from market_operator.backtest_v6 import (                        # noqa: E402
    alignment_issues_ci,
    compute_metrics,
    prepare_cognition,
)
from market_operator.campaign import (                           # noqa: E402
    build_campaign_frame,
    run_campaigns,
)
from market_operator.broker import CONTRACTS_PATH                # noqa: E402
from market_operator.research import block_bootstrap_ci          # noqa: E402
from market_operator.strategy_v6 import V6Params                 # noqa: E402
from market_operator.structure import Params                     # noqa: E402
from market_operator.trigger import prepare_mic_cognition        # noqa: E402

CONFIG_V10 = ROOT / "config" / "v10.json"
FROZEN_TURTLE = ROOT / "reports" / "backtest_v6_turtle.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev", "test")
COST_MULTS = (1.0, 1.5, 2.0)
SEMANTICS = "turtle"
MIN_TRADES_TEST = 60
TF_MS = {"1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
         "12h": 43_200_000, "1d": 86_400_000}

TRADE_COLUMNS = list(rb.TRADE_COLUMNS) + ["mic", "cog_index", "advance_minutes"]


def _iso(ts) -> str:
    return rb._iso(ts)


def _median(xs: list):
    return rb._median(xs)


# ---------------------------------------------------------------------------
# 帧/认知装载(canonical 壳 + mic 触发帧;校验纪律同 v6)
# ---------------------------------------------------------------------------


def prepare_frames_v10(raw_cfg: dict, params: Params, tf_list: list,
                       *, with_mic: bool, trigger_tf_seconds: int) -> tuple:
    frames: dict = {}
    parity: dict = {}
    trig_step = trigger_tf_seconds * 1000
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
        exact = all((compact[c] - df_1h[c]).abs().max() == 0.0
                    for c in ("open", "high", "low", "close"))
        if not exact or not (compact["ts"] == df_1h["ts"]).all():
            raise RuntimeError(f"{sym} 15m compaction != 1h frame (OHLC/ts)")
        if with_mic and trigger_tf_seconds == 900:
            df_trig = df_15m
        elif with_mic:
            df_trig = mo_data.load(sym, f"{trigger_tf_seconds // 60}m")
            issues_t = mo_data.validate(df_trig, step_ms=trig_step)
            if issues_t:
                raise RuntimeError(f"{sym} {trigger_tf_seconds}s validation: "
                                   f"{issues_t[:5]}")
            align_t = mo_data.check_frame_alignment(df_trig, df_1h,
                                                    step_fine_ms=trig_step)
            if align_t:
                raise RuntimeError(f"{sym} trigger↔1h alignment: {align_t[:5]}")
            compact_t = agg.aggregate(df_trig, 1)
            exact_t = all((compact_t[c] - df_1h[c]).abs().max() == 0.0
                          for c in ("open", "high", "low", "close"))
            if not exact_t or not (compact_t["ts"] == df_1h["ts"]).all():
                raise RuntimeError(f"{sym} trigger compaction != 1h frame")
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
            cog = prepare_cognition(df_cog, params, sym, enable_e7=True)
            if tf == "1h":
                ci = alignment_issues_ci(cog.bars, [(int(r.ts),) for r in
                                                    df_15m.itertuples(index=False)],
                                         float(raw_cfg["dev_frac"]))
                if ci:
                    raise RuntimeError(f"{sym} boundary alignment: {ci}")
            mic = None
            if with_mic:
                mic = prepare_mic_cognition(
                    df_cog, df_trig, params, sym,
                    obs_tf_seconds=agg.TF_HOURS[tf] * 3600,
                    trigger_tf_seconds=trigger_tf_seconds, enable_e7=True)
                # 检测器零改动硬约束:mic 帧复放的 bar 事件流 ==
                # prepare_cognition 逐字段一致
                want = sorted((i, e.id, e.direction,
                               e.attrs.get("ref"), e.attrs.get("invalid"))
                              for i, evs in cog.events_by.items() for e in evs)
                got = sorted((i, e.id, e.direction,
                              e.attrs.get("ref"), e.attrs.get("invalid"))
                             for i, evs in mic.bar_events_by.items() for e in evs)
                if got != want:
                    raise RuntimeError(
                        f"{sym}@{tf} mic 帧 bar 事件流 != prepare_cognition"
                        "(检测器改动?)")
                parity[f"{sym}@{tf}"] = sum(len(v) for v in
                                            mic.bar_events_by.values())
            f = build_campaign_frame(cog, df_exec, tf, mic_cog=mic)
            frames[f.key] = f
            n_ev = sum(len(v) for v in cog.events_by.values())
            n_mic = sum(len(v) for v in mic.events_by.values()) if mic else 0
            print(f"[frame] {sym}@{tf}{'(mic)' if mic else ''}: "
                  f"{len(df_cog)} 认知 bar, {n_ev} bar events, {n_mic} mic events "
                  f"({time.time() - t0:.1f}s)", flush=True)
    return frames, parity


# ---------------------------------------------------------------------------
# mic 提前量统计(入场时刻相对认知收盘的提前分钟)
# ---------------------------------------------------------------------------


def mic_advance_stats(res, frames: dict, seg_bounds_attr: dict = None) -> dict:
    """逐 mic 成交(open 行 meta.mic):advance = 认知收盘 − 实际入场(分钟)。

    对照口径:同现象的 bar 事件成交时刻 = 认知 bar 标签 + TF(次一执行 bar
    开盘);mic 市价入场 = 次一 trigger bar 开盘 ⇒ advance ∈ [0, TF − step],
    市价(broker OPEN)是提前量的干净度量。mic 挂单(LIMIT_FILL)武装早但成
    交于窗口内任意时点(可晚于认知收盘,advance 可负)⇒ 单列 by_kind,不与
    市价混算。
    """
    advances = []
    adv_market = []
    adv_limit = []
    by_tf: dict = {}
    for row in res.ledger:
        if row["kind"] not in ("OPEN", "LIMIT_FILL"):
            continue
        m = row["meta"]
        if not m.get("mic"):
            continue
        key = row["symbol"]
        f = frames[key]
        i = int(m.get("cog_index"))
        tf_ms = f.tf_hours * 3_600_000
        cog_close = int(f.cog.bars[i][0]) + tf_ms
        adv_min = (cog_close - int(row["ts"])) / 60_000.0
        advances.append(adv_min)
        (adv_market if row["kind"] == "OPEN" else adv_limit).append(adv_min)
        by_tf.setdefault(f.tf_label, []).append(adv_min)
    if not advances:
        return {"n": 0, "n_market": 0, "n_limit": 0, "mean_min": None,
                "median_min": None, "min_min": None, "max_min": None,
                "dist_min": {}, "by_tf": {}, "by_kind": {}}
    dist = {}
    for a in adv_market:
        dist[int(a)] = dist.get(int(a), 0) + 1

    def agg(v):
        if not v:
            return {"n": 0}
        return {"n": len(v), "mean_min": sum(v) / len(v),
                "median_min": _median(v), "min_min": min(v),
                "max_min": max(v)}

    return {
        "n": len(advances),
        "n_market": len(adv_market),
        "n_limit": len(adv_limit),
        "mean_min": sum(adv_market) / len(adv_market) if adv_market else None,
        "median_min": _median(adv_market),
        "min_min": min(adv_market) if adv_market else None,
        "max_min": max(adv_market) if adv_market else None,
        "dist_min": dict(sorted(dist.items())),          # 市价(open)口径
        "by_kind": {"market": agg(adv_market), "limit": agg(adv_limit)},
        "by_tf": {tf: {"n": len(v), "mean_min": sum(v) / len(v),
                       "median_min": _median(v)}
                  for tf, v in sorted(by_tf.items())},
    }


def mic_signal_counts(frames: dict, dev_frac: float) -> dict:
    """dev 段 mic 信号数(机制 × TF × 方向;trigger 帧认知缓存直数,段口径 =
    其认知 bar 归属,与 rb.signal_counts 同构)。"""
    from market_operator.research import split_boundary
    cells: dict = {}
    for key, f in sorted(frames.items()):
        if f.mic is None:
            continue
        b = split_boundary(len(f.cog.bars), dev_frac)
        for a, evs in f.mic.events_by.items():
            i = f.mic.cog_index_of[a]
            if not (0 <= i < b):
                continue
            for e in evs:
                k = (e.id, f.tf_label, "long" if e.direction > 0 else "short")
                cells[k] = cells.get(k, 0) + 1
    return {f"{m}×{tf}×{side}": n for (m, tf, side), n in sorted(cells.items())}


# ---------------------------------------------------------------------------
# 逐笔增广(mic 标注 + 提前分钟)
# ---------------------------------------------------------------------------


def augment_trades_v10(trades: list, frames: dict, ledger: list) -> list:
    out = rb.augment_trades(trades, frames)
    meta_by_tid: dict = {}
    for row in ledger:
        if row["trade_id"] is None or row["trade_id"] in meta_by_tid:
            continue
        if row["kind"] in ("OPEN", "LIMIT_FILL"):
            meta_by_tid[row["trade_id"]] = row["meta"]
    fin = []
    for t in out:
        m = meta_by_tid.get(t["trade_id"], {})
        is_mic = bool(m.get("mic"))
        adv = None
        if is_mic:
            f = frames[t["campaign"]]
            i = int(m.get("cog_index"))
            cog_close = int(f.cog.bars[i][0]) + f.tf_hours * 3_600_000
            adv = (cog_close - t["open_ts"]) / 60_000.0
        fin.append({**t, "mic": is_mic, "cog_index": m.get("cog_index"),
                    "advance_minutes": adv})
    return fin


def trades_frame_v10(trades: list) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({**t,
                     "open_decision_iso": _iso(t["open_decision_ts"]),
                     "open_iso": _iso(t["open_ts"]),
                     "close_decision_iso": _iso(t["close_decision_ts"]),
                     "exit_iso": _iso(t["exit_ts"]),
                     "reason_reduce": " ;; ".join(t["reasons_reduce"])})
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_grid(frames: dict, raw_cfg: dict, params: Params, rp_base: V6Params,
             contracts: dict, *, enable_mic: bool, segments=SEGMENTS) -> dict:
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
    results = {}
    for seg in segments:
        for mult in COST_MULTS:
            t0 = time.time()
            res = run_campaigns(frames, segment=seg, cost_multiplier=mult,
                                router_params=rp_base, enable_mic=enable_mic,
                                **broker_kw)
            res.trades = augment_trades_v10(res.trades, frames, res.ledger)
            m = compute_metrics(res)
            results[(seg, mult)] = (res, m)
            c = res.counters
            print(f"[bt]{' mic' if enable_mic else ' bar'} {seg:4s} "
                  f"cost {mult}x: net={m['net_pnl']:+.2f} "
                  f"trades={m['n_trades']} fills={c['opens_filled']}+"
                  f"{c['limit_fills_entry']} mic_go={c['mic_go_events']} "
                  f"cap_skips={c['portfolio_cap_skips']} "
                  f"({time.time() - t0:.1f}s)", flush=True)
    liq = sum(results[(s, mm)][0].counters["liquidations"]
              for s in segments for mm in COST_MULTS)
    if liq != 0:
        raise RuntimeError(f"强平非预期触发: {liq}")
    return results


def verdict_block(n_test: int, exp_15x, pf_2x, note: str) -> dict:
    ok = (n_test >= MIN_TRADES_TEST and exp_15x is not None and exp_15x > 0.0
          and pf_2x is not None and pf_2x > 1.0)
    return {
        "principal_unit": "合并战役(全部 symbol×TF 合并,与协议 §4/§C 一致)",
        "criteria": {
            "n_trades_test_ge_60": n_test >= MIN_TRADES_TEST,
            "expectancy_test_1_5x_gt_0": bool(exp_15x is not None
                                              and exp_15x > 0.0),
            "pf_test_2x_gt_1": bool(pf_2x is not None and pf_2x > 1.0),
            "expectancy_test_usdt_1_5x": exp_15x,
            "pf_test_2x": pf_2x,
        },
        "verdict": "PAPER_CANDIDATE" if ok else "RESEARCH_ONLY",
        "note": note,
    }


def main() -> None:
    t_start = time.time()
    raw_cfg = json.loads(CONFIG_V10.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V10)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    tf_list = list(raw_cfg["tf_list"])
    if tf_list != ["1h", "2h", "4h", "12h", "1d"]:
        raise RuntimeError(f"config/v10.json tf_list 冻结: {tf_list}")
    if raw_cfg["entry_mode"] != "quality_routed":
        raise RuntimeError("config/v10.json entry_mode must be quality_routed")
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v10 parent=canonical:merge_max_atr 必须 2.0")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    # -- 帧:bar 臂(不带 mic 认知,canonical 壳)+ mic 臂(900 主口径) ------------
    print("[prep] bar 臂帧(canonical 壳,validate+compaction 断言)", flush=True)
    frames_bar, _ = prepare_frames_v10(raw_cfg, params, tf_list,
                                       with_mic=False, trigger_tf_seconds=900)
    xcheck = rb.cross_checks(frames_bar, float(raw_cfg["k_is"]),
                             merge_max_atr=2.0)
    print("[prep] mic 臂帧(trigger=900;含 bar 事件流逐字段对账)", flush=True)
    frames_mic, mic_parity = prepare_frames_v10(raw_cfg, params, tf_list,
                                                with_mic=True,
                                                trigger_tf_seconds=900)

    # -- 臂一:bar(enable_mic=False;parity 锚) ----------------------------------
    res_bar = run_grid(frames_bar, raw_cfg, params, rp_base, contracts,
                       enable_mic=False)

    # -- parity 硬断言:bar 臂 == canonical v6 臂 B 冻结产物,逐格 ----------------
    frozen = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    parity_rows = []
    for (seg, mult), (_r, m) in sorted(res_bar.items()):
        tag = f"{SEMANTICS}|{seg}@{mult}x"
        fm = frozen["runs"][tag]["metrics"]
        row = {
            "run": tag,
            "v10_net": m["net_pnl"], "frozen_net": fm["net_pnl"],
            "v10_trades": m["n_trades"], "frozen_trades": fm["n_trades"],
            "v10_pf": m["profit_factor"], "frozen_pf": fm["profit_factor"],
            "v10_winrate": m["winrate"], "frozen_winrate": fm["winrate"],
        }
        ok = (m["net_pnl"] == fm["net_pnl"] and m["n_trades"] == fm["n_trades"]
              and m["winrate"] == fm["winrate"]
              and m["profit_factor"] == fm["profit_factor"])
        row["parity"] = ok
        parity_rows.append(row)
        if not ok:
            raise RuntimeError(f"bar 臂与冻结产物不一致 {tag}: {row}")
    print("[xcheck] bar 臂 9 格 == canonical v6 臂 B 冻结产物(逐格全等)",
          flush=True)

    # -- 臂二:mic 900(主交付) ---------------------------------------------------
    res_mic = run_grid(frames_mic, raw_cfg, params, rp_base, contracts,
                       enable_mic=True)

    adv_stats = {}
    for (seg, mult), (res, _m) in sorted(res_mic.items()):
        adv_stats[f"mic15m|{seg}@{mult}x"] = mic_advance_stats(res, frames_mic)

    # -- 臂三:mic 300(仅 dev 敏感性) -------------------------------------------
    sens5m: dict = {"evaluated": False, "reason": None, "runs": {}}
    have_5m = all((ROOT / "data" / "raw" / f"{s}_5m.csv").exists()
                  for s in symbols)
    if have_5m:
        print("[prep] mic 臂帧(trigger=300;dev 敏感性)", flush=True)
        frames_5m, _ = prepare_frames_v10(raw_cfg, params, tf_list,
                                          with_mic=True,
                                          trigger_tf_seconds=300)
        res_5m = run_grid(frames_5m, raw_cfg, params, rp_base, contracts,
                          enable_mic=True, segments=("dev",))
        sens5m["evaluated"] = True
        for (seg, mult), (res, m) in sorted(res_5m.items()):
            sens5m["runs"][f"mic5m|{seg}@{mult}x"] = {
                "metrics": m, "advance": mic_advance_stats(res, frames_5m)}
            REPORTS.mkdir(exist_ok=True)
            trades_frame_v10(res.trades).to_csv(
                REPORTS / f"trades_v10_turtle_mic5m__{seg}"
                        f"{'' if mult == 1.0 else '_cost' + str(mult).replace('.', '_')}.csv",
                index=False)
    else:
        sens5m["reason"] = ("data/raw/*_5m.csv 缺失 ⇒ 5m 敏感性未评估"
                            "(如实记录;协议 §A:5m 仅 dev 敏感性)")
        print(f"[sens] {sens5m['reason']}", flush=True)

    # -- 裁决对比(协议 §C 两种) --------------------------------------------------
    def cell(results, seg, mult):
        m = results[(seg, mult)][1]
        return {"net_pnl": m["net_pnl"], "n_trades": m["n_trades"],
                "winrate": m["winrate"], "profit_factor": m["profit_factor"],
                "expectancy_usdt": m["expectancy_usdt"],
                "expectancy_r": m["expectancy_r"],
                "max_dd_pct": m["max_dd_pct"]}

    adjudication = {
        "dev_1_5x_mic_vs_bar": {
            "mic15m": cell(res_mic, "dev", 1.5),
            "bar(v10-parity)": cell(res_bar, "dev", 1.5),
            "delta_net": res_mic[("dev", 1.5)][1]["net_pnl"]
            - res_bar[("dev", 1.5)][1]["net_pnl"],
        },
        "dev_1_5x_vs_frozen_armB": {
            "mic15m": cell(res_mic, "dev", 1.5),
            "frozen(backtest_v6_turtle)": cell(
                {("dev", 1.5): (None, frozen["runs"]["turtle|dev@1.5x"]["metrics"])},
                "dev", 1.5),
            "delta_net": res_mic[("dev", 1.5)][1]["net_pnl"]
            - frozen["runs"]["turtle|dev@1.5x"]["metrics"]["net_pnl"],
        },
        "test_once_exposure_13": {
            "mic15m": {k: cell(res_mic, "test", mm) for k, mm in
                       (("1x", 1.0), ("1_5x", 1.5), ("2x", 2.0))},
            "bar(v10-parity)": {k: cell(res_bar, "test", mm) for k, mm in
                                (("1x", 1.0), ("1_5x", 1.5), ("2x", 2.0))},
        },
        " interpretation_note": None,
    }
    d15 = adjudication["dev_1_5x_mic_vs_bar"]["delta_net"]
    adjudication["interpretation_note"] = (
        f"mic 早到(dev@1.5x Δ净 = {d15:+.2f})"
        + ("仍未转正 ⇒ 『晚报』假说死于『拥挤+薄利』双因候选成立,"
           "按 v10-protocol §C 不再追更早;" if d15 <= 0 or
           (adjudication["dev_1_5x_mic_vs_bar"]["mic15m"]["expectancy_usdt"]
            or 0) <= 0
           else "dev 有增量,test 判据见下;"))

    # -- GO/NO-GO(沿用 v6 升级线;test 已用唯一一次曝光) -------------------------
    m_mic_test1x = res_mic[("test", 1.0)][1]
    n_test = m_mic_test1x["n_trades"]
    exp_15x = res_mic[("test", 1.5)][1]["expectancy_usdt"]
    pf_2x = res_mic[("test", 2.0)][1]["profit_factor"]
    verdict = verdict_block(
        n_test, exp_15x, pf_2x,
        note=("v10 §A 触发麦克风一次性 test 评估(第 13 次曝光,research-log "
              "#20 预注册);GO/NO-GO 沿用 v6 升级线,如实填报。"))
    sample = {
        "n_trades_test_1x": n_test, "threshold": MIN_TRADES_TEST,
        "sample_size_verdict": ("SUFFICIENT(>=60)" if n_test >= MIN_TRADES_TEST
                                else "INSUFFICIENT_DATA"),
    }
    trades_test_1x = [t for t in res_mic[("test", 1.0)][0].trades
                      if t["in_segment_open"]]
    if trades_test_1x:
        ci_lo, ci_hi = block_bootstrap_ci(
            np.asarray([t["net_pnl"] for t in trades_test_1x], dtype=np.float64),
            np.asarray([t["open_ts"] for t in trades_test_1x], dtype=np.int64))
    else:
        ci_lo = ci_hi = float("nan")
    sample["test_net_pnl_mean_ci95_1x"] = [ci_lo, ci_hi]

    # -- CSV 产物(v10 独立文件名;不覆写任何历史产物) ------------------------------
    REPORTS.mkdir(exist_ok=True)
    for arm, results in (("bar", res_bar), ("mic15m", res_mic)):
        for (seg, mult), (res, _m) in sorted(results.items()):
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v10(res.trades).to_csv(
                REPORTS / f"trades_v10_turtle_{arm}__{seg}{suffix}.csv",
                index=False)
            rb.equity_frame(res).to_csv(
                REPORTS / f"equity_v10_turtle_{arm}__{seg}{suffix}.csv",
                index=False)

    # -- JSON 产物 ---------------------------------------------------------------
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "semantics": SEMANTICS,
        "protocol": {
            "doc": "docs/v10-protocol.md §A(冻结,2026-10-20)",
            "meta": raw_cfg["meta"],
            "mic_semantics": "认知帧(1h/2h/4h/12h/1d)结构/区域/episode/pivot/"
                             "regime 锁定;收盘类确认(BREAKOUT_ACCEPTED 站稳/BREAKOUT_FAILED 收复/LEVEL_REJECTED 回退/"
                             "TREND_RESUMED 回收/STRUCTURE_BROKEN 破坏/FLIP_RETESTED 回踩拒绝)允许在 trigger_tf "
                             "收盘先满足;N_a_mic=N_a×scale、N_f_mic=N_f×scale;"
                             "阈值=最近已收认知 bar 的 ATR;知时=trigger 收盘,"
                             "成交=次一 trigger 开盘;收割/费用/容量/组合不变。",
            "test_eval_declaration": "test 段第 13 次评估曝光(research-log #20 "
                                     "预注册,一次性,此后只许前向);本报告如实"
                                     "填报,不产生额外叙述。",
            "e1_e8_detector_invariant": "BREAKOUT_ACCEPTED-EDGE_RECLAIMED bar 事件检测零改动;mic 帧复放的 "
                                        "bar 事件流 == prepare_cognition 逐字段"
                                        "硬断言(见 bar_stream_parity)。",
        },
        "config": {**{k: raw_cfg[k] for k in (
            "version", "enable_mic", "trigger_tf_seconds", "risk_frac",
            "leverage_cap", "taker_fee", "maker_fee", "slippage_per_side",
            "funding_per_8h_per_side", "dev_frac", "portfolio_cap_frac",
            "n_fill", "exec_tf_seconds", "merge_max_atr")},
            "equity0": 300.0, "tf_list": tf_list, "k_is": rp_base.k_is,
            "k_dq": rp_base.k_dq, "k_imp": rp_base.k_imp,
            "turtle_stop_atr": rp_base.turtle_stop_atr,
            "whitelist": [f"{i}|{'long' if d > 0 else 'short'}"
                          for i, d in sorted(whitelist)],
            "cost_mults": list(COST_MULTS),
            "n_a_mic_1h": int(raw_cfg["N_a"]) * 4,
            "n_f_mic_1h": int(raw_cfg["N_f"]) * 4},
        "contracts": {
            "source": spec_src,
            "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
            "cache_sha256": contracts_sha,
        },
        "cross_checks_canonical_baseline": xcheck,
        "bar_stream_parity": {"description": "mic 帧复放 bar 事件流 == "
                                             "prepare_cognition 逐字段",
                              "n_events_by_campaign": mic_parity},
        "frozen_armB_parity": parity_rows,
        "dev_signal_counts_bar": rb.signal_counts(frames_bar, dev_frac),
        "dev_signal_counts_mic15m": mic_signal_counts(frames_mic, dev_frac),
        "adjudication": adjudication,
        "mic_advance_stats": adv_stats,
        "sample_size": sample,
        "verdict_v10": verdict,
        "runs": {},
    }
    for arm, results in (("bar", res_bar), ("mic15m", res_mic)):
        for (seg, mult), (res, m) in sorted(results.items()):
            payload["runs"][f"{arm}|{seg}@{mult}x"] = {
                "metrics": m,
                "counters": res.counters,
                "bucket_tables": rb.bucket_tables(res.trades),
                "giveback": rb.giveback_summary(res.trades),
                "advance": (mic_advance_stats(res, frames_mic)
                            if arm == "mic15m" else None)}
    payload["sensitivity_5m"] = sens5m

    (REPORTS / "backtest_v10_turtle.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v10_turtle.md").write_text(render_md(payload),
                                                   encoding="utf-8")
    print(f"-> reports/backtest_v10_turtle.json/.md + v10 CSVs "
          f"(total {time.time() - t_start:.0f}s)", flush=True)


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=2, dash="—"):
    if x is None or (isinstance(x, float) and x != x):
        return dash
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def _grid_runs(payload, arm):
    return [(s, mm, payload["runs"][f"{arm}|{s}@{mm}x"])
            for s in SEGMENTS for mm in COST_MULTS]


def render_md(payload: dict) -> str:
    cfg = payload["config"]
    adj = payload["adjudication"]
    lines = [
        "# v10 §A 触发麦克风回测报告(trigger=15m 主口径 × turtle 唯一语义)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明",
        "",
        f"- 机制:{payload['protocol']['mic_semantics']}",
        f"- **{payload['protocol']['test_eval_declaration']}**",
        f"- 检测器不变式:{payload['protocol']['e1_e8_detector_invariant']}",
        "- 两臂同台:bar 臂(enable_mic=False)与 canonical 规范链产物 "
        "reports/backtest_v6_turtle.json 逐格**全等**(硬断言已过);mic 臂与其"
        "余唯一差异 = 事件知时/成交细化。",
        f"- 合约:{payload['contracts']['source']}(离线强制)。",
        "",
        "## 1. 总览(段 × 成本;权益 300 USDT 起;mic15m 臂)",
        "",
        "| 段 | 成本 | 净盈亏 | 收益率 | 最大回撤% | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R |",
        "|---|---|---|---|---|---|---|---|---|---|"]
    for seg, mult, r in _grid_runs(payload, "mic15m"):
        m = r["metrics"]
        lines.append(
            f"| {seg} | {mult}x | {_f(m['net_pnl'])} "
            f"| {_f(100 * m['return_pct'])}% | {_f(100 * m['max_dd_pct'])}% "
            f"| {m['n_trades']} | {_f(m['winrate'], 3)} "
            f"| {_f(m['profit_factor'])} | {_f(m['expectancy_usdt'], 4)} "
            f"| {_f(m['expectancy_r'], 4)} |")
    lines += ["", "对照 bar 臂(parity 锚):", "",
              "| 段 | 成本 | 净盈亏 | 交易数 | 胜率 | 盈亏因子 | 期望USDT |",
              "|---|---|---|---|---|---|---|"]
    for seg, mult, r in _grid_runs(payload, "bar"):
        m = r["metrics"]
        lines.append(
            f"| {seg} | {mult}x | {_f(m['net_pnl'])} | {m['n_trades']} "
            f"| {_f(m['winrate'], 3)} | {_f(m['profit_factor'])} "
            f"| {_f(m['expectancy_usdt'], 4)} |")

    lines += ["", "## 2. 裁决对比(协议 §C 两种+ test 一次性曝光)", "",
              "### 2.1 dev@1.5x:mic vs 同程 bar 臂", "",
              "| 臂 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|"]
    for arm in ("mic15m", "bar(v10-parity)"):
        c = adj["dev_1_5x_mic_vs_bar"][arm]
        lines.append(f"| {arm} | {_f(c['net_pnl'])} | {c['n_trades']} "
                     f"| {_f(c['winrate'], 3)} | {_f(c['profit_factor'])} "
                     f"| {_f(c['expectancy_usdt'], 4)} "
                     f"| {_f(c['expectancy_r'], 4)} |")
    lines.append(f"| **Δ(mic−bar)** | **{_f(adj['dev_1_5x_mic_vs_bar']['delta_net'])}** "
                 f"| — | — | — | — | — |")
    c = adj["dev_1_5x_vs_frozen_armB"]
    lines += ["", "### 2.2 dev@1.5x:mic vs canonical v6 臂 B 冻结产物", "",
              "| 臂 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|",
              f"| mic15m | {_f(c['mic15m']['net_pnl'])} "
              f"| {c['mic15m']['n_trades']} | {_f(c['mic15m']['winrate'], 3)} "
              f"| {_f(c['mic15m']['profit_factor'])} "
              f"| {_f(c['mic15m']['expectancy_usdt'], 4)} "
              f"| {_f(c['mic15m']['expectancy_r'], 4)} |",
              f"| frozen backtest_v6_turtle | {_f(c['frozen(backtest_v6_turtle)']['net_pnl'])} "
              f"| {c['frozen(backtest_v6_turtle)']['n_trades']} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['winrate'], 3)} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['profit_factor'])} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['expectancy_usdt'], 4)} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['expectancy_r'], 4)} |",
              f"| **Δ(mic−frozen)** | **{_f(c['delta_net'])}** | — | — | — | — | — |",
              "",
              f"> {adj['interpretation_note']}",
              "",
              "### 2.3 test 段(第 13 次曝光,一次性)", "",
              "| 臂 | 成本 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT |",
              "|---|---|---|---|---|---|---|"]
    for arm in ("mic15m", "bar(v10-parity)"):
        for kk in ("1x", "1_5x", "2x"):
            cc = adj["test_once_exposure_13"][arm][kk]
            lines.append(f"| {arm} | {kk} | {_f(cc['net_pnl'])} "
                         f"| {cc['n_trades']} | {_f(cc['winrate'], 3)} "
                         f"| {_f(cc['profit_factor'])} "
                         f"| {_f(cc['expectancy_usdt'], 4)} |")

    lines += ["", "## 3. mic 提前量(入场时刻相对认知收盘,分钟;"
              "市价口径:限价挂单成交时点含窗口等待,单独列示)", "",
              "| 段@成本 | mic 市价成交 | 均值 | 中位 | 最小 | 最大 | 分布(min:count) |",
              "|---|---|---|---|---|---|---|"]
    for tag, st in payload["mic_advance_stats"].items():
        if st["n"] == 0:
            continue
        lim = st.get("by_kind", {}).get("limit", {})
        dist = json.dumps(st['dist_min'], separators=(',', ':'))
        if lim.get("n"):
            dist += f"(限价 {lim['n']} 笔,均 {_f(lim.get('mean_min'), 1)})"
        lines.append(f"| {tag} | {st['n_market']} | {_f(st['mean_min'], 1)} "
                     f"| {_f(st['median_min'], 1)} | {_f(st['min_min'], 0)} "
                     f"| {_f(st['max_min'], 0)} | {dist} |")

    s5 = payload["sensitivity_5m"]
    lines += ["", "## 4. 5m 敏感性(dev only)", ""]
    if not s5["evaluated"]:
        lines.append(f"- {s5['reason']}")
    else:
        lines += ["| 段@成本 | 净盈亏 | 笔数 | PF | 期望USDT | 提前均/中位(min) |",
                  "|---|---|---|---|---|---|---|"]
        for tag, r in sorted(s5["runs"].items()):
            m, st = r["metrics"], r["advance"]
            lines.append(f"| {tag} | {_f(m['net_pnl'])} | {m['n_trades']} "
                         f"| {_f(m['profit_factor'])} "
                         f"| {_f(m['expectancy_usdt'], 4)} "
                         f"| {_f(st.get('mean_min'), 1)}/{_f(st.get('median_min'), 1)} |")
        lines.append("- 协议 §A/§C:5m 若 dev 无改善如实报,不引入生产。")

    up = payload["verdict_v10"]
    ss = payload["sample_size"]
    lines += ["", "## 5. 样本量与 GO/NO-GO(沿用 v6 升级线,如实填报)", "",
              f"- test 合并战役成交(mic15m,1x,OPEN ∈ test):**{ss['n_trades_test_1x']}**"
              f"(阈值 ≥ {ss['threshold']})⇒ **{ss['sample_size_verdict']}**。",
              f"- test@1x 逐笔净盈亏均值 95% CI(日块 bootstrap):"
              f"{_f(ss['test_net_pnl_mean_ci95_1x'][0], 4)} ~ "
              f"{_f(ss['test_net_pnl_mean_ci95_1x'][1], 4)} USDT。",
              f"- {up['note']}", "",
              "| 判据 | 值 | 通过 |", "|---|---|---|",
              f"| test 成交 ≥ 60 | {up['criteria']['n_trades_test_ge_60']} "
              f"(n={ss['n_trades_test_1x']}) "
              f"| {'✓' if up['criteria']['n_trades_test_ge_60'] else '✗'} |",
              f"| test 净期望@1.5x > 0 | {_f(up['criteria']['expectancy_test_usdt_1_5x'], 4)} "
              f"| {'✓' if up['criteria']['expectancy_test_1_5x_gt_0'] else '✗'} |",
              f"| test PF@2x > 1 | {_f(up['criteria']['pf_test_2x'])} "
              f"| {'✓' if up['criteria']['pf_test_2x_gt_1'] else '✗'} |",
              f"| **结论** | — | **{up['verdict']}** |",
              "",
              "## 6. 已知近似与如实声明",
              "",
              "①采用窗口锚点(刺穿/ex 帧→首根 trigger)取保守(窗口最早关闭);"
              "②ATR 抖动边界可致认知破位被 mic 漏记(bar 事件照常,如实);"
              "③E6 同现象每认知覆盖区间至多一发(频率镜像认知层);④E1 在破位"
              "迟揭示角点可与 bar 事件同刻或晚 ≤15m(槽位先到先得);⑤5m 为 dev "
              "敏感性,不入生产;⑥bar 臂与 canonical 的 parity 为逐格全等硬断言,"
              "非抽样;⑦test 曝光纪律见 §0。",
              "",
              "机器可读全量:`reports/backtest_v10_turtle.json`;逐笔:"
              "`reports/trades_v10_turtle_*.csv`(增列 mic/cog_index/"
              "advance_minutes);权益:`reports/equity_v10_turtle_*.csv`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
