"""v13 级联触发尺度回测管线(docs/v13-protocol.md,冻结 2026-10-20):
认知帧不变 × 逐级 trigger 帧知时细化(1h→15m/2h→30m/4h→1h/12h→2h/1d→4h)
× 多尺度战役 × turtle 唯一语义。

用法:  python scripts/run_backtest_v13.py
配置:  config/v13.json(parent=config/canonical.json,不改动;
       enable_mic=false 为默认/parity 臂,trigger_tf_map 级联映射冻结)
产物:  reports/backtest_v13_cascade.json / backtest_v13_cascade.md
       reports/trades_v13_cascade_{bar,cascade}__{seg}[_cost1_5,_cost2_0].csv
       reports/equity_v13_cascade_{bar,cascade}__{seg}[_cost1_5,_cost2_0].csv

口径与纪律:
- 两臂同台:bar/cascade 两臂同行于**现行 canonical.json 语义**(log#21
  语义路由,全 E1-E8 路由表;v13.json 逐项照抄)。cascade 臂 =
  enable_mic=True,同一战略/收割/费用/容量,唯一差异 = 事件知时与成交按
  战役认知帧细化到 trigger_tf_map 对应帧(协议:映射缺失/帧不可用 ⇒
  最近可用帧 + 如实声明;N_a_mic=N_a×scale,N_f_mic=N_f×scale,阈值=认知帧 ATR)。
- parity 双锚:① 冻结时代名名单(8 档 E1/E3/E6/E7,与 reports/
  backtest_v6_turtle.json 自述 config 一致)探针回放走同一批战役帧 ⇒
  与该冻结产物**逐格全等** —— 硬断言,不等即 raise(代码零回归锚,与
  v10 同锚但只读比对,历史产物不覆写);② 现行名单(16 档)bar 臂 9 格
  作为 v13 canonical-parity 在册基线(冻结产物系 #19 语义路由决议之前的
  运行,名单语义不同 ⇒ 与现行 bar 臂之差如实归因 log#21,不视为违规)。
- trigger 帧数据契约:15m 已有(v10 链);30m 由 15m 2:1 聚合(v13 §1,构建
  与运行双重锚定 1h 逐位对账);1h/2h/4h 沿用现有(聚合守恒每轮复查)。
- 检测器零改动硬约束:per-战役 mic 帧内复放的 bar 事件流 ==
  prepare_cognition 逐字段(逐 id/direction/known_index/ref/invalid)断言。
- 裁决对比(报告 §2):① dev@1.5x 内部对照 cascade vs bar(同名语义);
  ② dev@1.5x 对照 canonical v6 臂 B 冻结产物(冻结时代名单语义,跨语义
  背景参照,如实标注);判定口径沿用 v10-protocol §C(早到仍不转正 ⇒
  "晚报"假说死于"拥挤+薄利"双因,不再追更早)。
- test 段曝光:第 16 次评估曝光(research-log #26 预注册,一次性,此后只许
  前向)。本脚本为唯一一次 v13 test 评估的执行入口;判据沿用 v6 升级线
  (n≥60 且 test 净期望@1.5x>0 且 test PF@2x>1 ⇒ PAPER_CANDIDATE,否则
  RESEARCH_ONLY),如实填报,不产生额外叙述。
- 合约规格强制离线缓存;freeze:历史产物(reports/backtest_v6_turtle.*、
  reports/backtest_v10_* 等)只读比对,不覆写。
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
from market_operator.trigger import (                            # noqa: E402
    TRIGGER_TF_MAP,
    prepare_mic_cognition,
    resolve_trigger_tf,
)

CONFIG_V13 = ROOT / "config" / "v13.json"
FROZEN_TURTLE = ROOT / "reports" / "backtest_v6_turtle.json"
REPORTS = ROOT / "reports"
SEGMENTS = ("full", "dev", "test")
COST_MULTS = (1.0, 1.5, 2.0)
SEMANTICS = "turtle"
MIN_TRADES_TEST = 60
MS_1H = 3_600_000
TRIGGER_LABEL = {900: "15m", 1800: "30m", 3600: "1h", 7200: "2h", 14400: "4h"}
# 冻结时代名单(#16 时代 canonical 8 档;log#21 语义路由决议前的运行口径,
# 与 reports/backtest_v6_turtle.json 自述 config.whitelist 逐一致 —— 运行
# 时硬断言同源)。仅用于代码零回归探针;v13 在册两臂用现行 canonical 全表。
FROZEN_ERA_WHITELIST = (("E1", -1), ("E1", 1), ("E3", -1), ("E3", 1),
                        ("E6", -1), ("E6", 1), ("E7", -1), ("E7", 1))

TRADE_COLUMNS = list(rb.TRADE_COLUMNS) + ["mic", "cog_index", "advance_minutes"]


def _iso(ts) -> str:
    return rb._iso(ts)


def _median(xs: list):
    return rb._median(xs)


def _quantiles(xs: list) -> dict:
    """提前量分位(p25/p50/p75 + min/max;线性插值,确定性)。"""
    if not xs:
        return {"n": 0, "mean_min": None, "p25_min": None, "p50_min": None,
                "p75_min": None, "min_min": None, "max_min": None}
    a = np.asarray(xs, dtype=np.float64)
    q25, q50, q75 = np.percentile(a, (25.0, 50.0, 75.0))
    return {"n": int(a.size), "mean_min": float(a.mean()),
            "p25_min": float(q25), "p50_min": float(q50),
            "p75_min": float(q75), "min_min": float(a.min()),
            "max_min": float(a.max())}


# ---------------------------------------------------------------------------
# trigger 帧装载(级联;校验纪律同 v10:validate + 对齐 + compaction 守恒)
# ---------------------------------------------------------------------------


def _compact_equals_1h(df_fine: pd.DataFrame, df_1h: pd.DataFrame,
                       fine_label: str) -> None:
    compact = agg.aggregate_to_step(df_fine, MS_1H)
    exact = all((compact[c] - df_1h[c]).abs().max() == 0.0
                for c in ("open", "high", "low", "close"))
    if not exact or not (compact["ts"] == df_1h["ts"]).all():
        raise RuntimeError(f"{fine_label} compaction != 1h frame (OHLC/ts)")


def _load_trigger_frame(sym: str, trig_tf: int, df_1h: pd.DataFrame,
                        df_15m: pd.DataFrame) -> pd.DataFrame:
    """级联 trigger 帧装载 + 全量校验(validate/对齐/compaction,违即 raise)。

    细档(≤1h):以 1h 为锚(check_frame_alignment + compaction 到 1h 逐位);
    粗档(>1h):以 1h 聚合重算为锚(v3 守恒同型,1h↔trigger 方向互换)。
    """
    step = trig_tf * 1000
    if trig_tf == 900:
        return df_15m
    if trig_tf == 3600:
        return df_1h
    if trig_tf == 1800:
        df = mo_data.load(sym, "30m")
    elif trig_tf == 7200:
        df = agg.load_tf(sym, "2h")
    elif trig_tf == 14400:
        df = agg.load_tf(sym, "4h")
    else:
        raise RuntimeError(f"{sym} trigger_tf {trig_tf}s 无装载口径"
                           "(映射缺失 ⇒ 应先在 resolve 层声明/拒绝)")
    issues = mo_data.validate(df, step_ms=step)
    if issues:
        raise RuntimeError(f"{sym} {TRIGGER_LABEL.get(trig_tf, trig_tf)} "
                           f"validation: {issues[:5]}")
    if step < MS_1H:
        align = mo_data.check_frame_alignment(df, df_1h, step_fine_ms=step)
        if align:
            raise RuntimeError(f"{sym} {trig_tf}s↔1h alignment: {align[:5]}")
        _compact_equals_1h(df, df_1h, f"{sym} {trig_tf}s")
    else:
        align = mo_data.check_frame_alignment(df_1h, df, step_fine_ms=MS_1H,
                                              step_coarse_ms=step)
        if align:
            raise RuntimeError(f"{sym} 1h↔{trig_tf}s alignment: {align[:5]}")
        expected = agg.aggregate_to_step(df_1h, step)
        exact = all((expected[c] - df[c]).abs().max() == 0.0
                    for c in ("open", "high", "low", "close"))
        if not exact or not (expected["ts"] == df["ts"]).all():
            raise RuntimeError(f"{sym} {trig_tf}s trigger 帧 != 1h 聚合重算"
                               "(漂移)")
    return df


# ---------------------------------------------------------------------------
# 帧/认知装载(canonical 壳 + 级联 mic 触发帧;校验纪律同 v10)
# ---------------------------------------------------------------------------


def prepare_frames_v13(raw_cfg: dict, params: Params, tf_list: list,
                       *, with_mic: bool) -> tuple:
    frames: dict = {}
    parity: dict = {}
    declarations: list = []
    tf_map = {int(k): int(v) for k, v in raw_cfg["trigger_tf_map"].items()}
    for sym in raw_cfg["symbols"]:
        df_1h = mo_data.load(sym)
        df_15m = mo_data.load(sym, "15m")
        issues = mo_data.validate(df_15m, step_ms=900_000)
        if issues:
            raise RuntimeError(f"{sym} 15m validation: {issues[:5]}")
        align = mo_data.check_frame_alignment(df_15m, df_1h, step_fine_ms=900_000)
        if align:
            raise RuntimeError(f"{sym} 15m↔1h alignment: {align[:5]}")
        _compact_equals_1h(df_15m, df_1h, f"{sym} 15m")
        trig_cache: dict = {}
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
                obs = agg.TF_HOURS[tf] * 3600
                trig_tf, decl = resolve_trigger_tf(obs, tf_map=tf_map)
                if decl is not None:
                    declarations.append(f"{sym}@{tf}: {decl}")
                if trig_tf not in trig_cache:
                    trig_cache[trig_tf] = _load_trigger_frame(
                        sym, trig_tf, df_1h, df_15m)
                mic = prepare_mic_cognition(
                    df_cog, trig_cache[trig_tf], params, sym,
                    obs_tf_seconds=obs,
                    trigger_tf_seconds=trig_tf, enable_e7=True)
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
            trig_note = (f" trig={TRIGGER_LABEL.get(mic.trigger_tf_seconds)}"
                         if mic else "")
            print(f"[frame] {sym}@{tf}{'(mic)' if mic else ''}{trig_note}: "
                  f"{len(df_cog)} 认知 bar, {n_ev} bar events, {n_mic} mic events "
                  f"({time.time() - t0:.1f}s)", flush=True)
    return frames, parity, declarations


# ---------------------------------------------------------------------------
# mic 提前量统计(入场时刻相对认知收盘的提前分钟;v10 口径 + 逐战役 + 分位)
# ---------------------------------------------------------------------------


def mic_advance_stats(res, frames: dict) -> dict:
    """逐 mic 成交(open 行 meta.mic):advance = 认知收盘 − 实际入场(分钟)。

    对照口径:同现象的 bar 事件成交时刻 = 认知 bar 标签 + TF(次一执行 bar
    开盘);mic 市价入场 = 次一 trigger bar 开盘 ⇒ advance ∈ [0, TF − step],
    市价(broker OPEN)是提前量的干净度量。mic 挂单(LIMIT_FILL)武装早但成
    交于窗口内任意时点(可晚于认知收盘,advance 可负)⇒ 单列 by_kind,不与
    市价混算。v13:附逐战役(by_campaign)与逐 TF(by_tf)分位。
    """
    advances = []
    adv_market = []
    adv_limit = []
    by_tf: dict = {}
    by_campaign: dict = {}
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
        if row["kind"] == "OPEN":
            by_tf.setdefault(f.tf_label, []).append(adv_min)
        c_entry = by_campaign.setdefault(key, {"market": [], "limit": []})
        c_entry["market" if row["kind"] == "OPEN" else "limit"].append(adv_min)
    by_tf = {tf: _quantiles(v) for tf, v in sorted(by_tf.items())}
    if not advances:
        return {"n": 0, "n_market": 0, "n_limit": 0,
                "overall_market": _quantiles([]), "by_tf": {},
                "by_campaign": {}, "by_kind": {}}
    return {
        "n": len(advances),
        "n_market": len(adv_market),
        "n_limit": len(adv_limit),
        "overall_market": _quantiles(adv_market),
        "by_tf": by_tf,
        "by_campaign": {k: {"market": _quantiles(v["market"]),
                            "limit": {kk: vv for kk, vv in
                                      _quantiles(v["limit"]).items()
                                      if kk in ("n", "mean_min")}}
                        for k, v in sorted(by_campaign.items())},
        "by_kind": {"market": _quantiles(adv_market),
                    "limit": _quantiles(adv_limit)},
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


def augment_trades_v13(trades: list, frames: dict, ledger: list) -> list:
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


def trades_frame_v13(trades: list) -> pd.DataFrame:
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
            res.trades = augment_trades_v13(res.trades, frames, res.ledger)
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
        "principal_unit": "合并战役(全部 symbol×TF 合并,与协议 §2/§C 一致)",
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
    raw_cfg = json.loads(CONFIG_V13.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V13)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    tf_list = list(raw_cfg["tf_list"])
    if tf_list != ["1h", "2h", "4h", "12h", "1d"]:
        raise RuntimeError(f"config/v13.json tf_list 冻结: {tf_list}")
    if raw_cfg["entry_mode"] != "quality_routed":
        raise RuntimeError("config/v13.json entry_mode must be quality_routed")
    if float(raw_cfg.get("merge_max_atr", 0.0)) != 2.0:
        raise RuntimeError("v13 parent=canonical:merge_max_atr 必须 2.0")
    tf_map = {int(k): int(v) for k, v in raw_cfg["trigger_tf_map"].items()}
    if tf_map != TRIGGER_TF_MAP:
        raise RuntimeError(f"config/v13.json trigger_tf_map != 冻结级联映射"
                           f"({tf_map} != {TRIGGER_TF_MAP})")
    if raw_cfg.get("enable_mic") is not False:
        raise RuntimeError("v13 默认 enable_mic=false(bar 对照臂;协议 §2)")
    whitelist = frozenset((str(i), int(d)) for i, d in raw_cfg["whitelist"])
    rp_base = V6Params(k_dq=float(raw_cfg["k_dq"]),
                       k_imp=float(raw_cfg["k_imp"]), k_is=float(raw_cfg["k_is"]),
                       turtle_stop_atr=float(raw_cfg["turtle_stop_atr"]),
                       k_bm=float(raw_cfg["k_bm"]), whitelist=whitelist)
    contracts, spec_src = rb.load_contracts_cache(symbols)
    contracts_sha = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    print(f"[contracts] source={spec_src} sha256={contracts_sha[:12]}…", flush=True)

    # -- v13 §1:30m 帧幂等保障(缺失/漂移才重写,锚定 1h 逐位对账在构建内) ----
    for sym in symbols:
        p30 = agg.ensure_30m([sym])[sym]
        rec = json.loads((ROOT / "data" / "manifest.json")
                         .read_text(encoding="utf-8"))["files"][f"{sym}_30m.csv"]
        assert hashlib.sha256(p30.read_bytes()).hexdigest() == rec["sha256"]
    print("[data] 30m 帧齐(manifest 哈希复核过)", flush=True)

    # -- 帧:bar 臂(不带 mic 认知,canonical 壳)+ cascade 臂(级联映射) ---------
    print("[prep] bar 臂帧(canonical 壳,validate+compaction 断言)", flush=True)
    frames_bar, _, _ = prepare_frames_v13(raw_cfg, params, tf_list,
                                          with_mic=False)
    xcheck = rb.cross_checks(frames_bar, float(raw_cfg["k_is"]),
                             merge_max_atr=2.0)
    print("[prep] cascade 臂帧(级联 trigger_tf_map;含 bar 事件流逐字段对账)",
          flush=True)
    frames_mic, mic_parity, declarations = prepare_frames_v13(
        raw_cfg, params, tf_list, with_mic=True)
    for d in declarations:
        print(f"[decl] {d}", flush=True)

    # -- 冻结锚装载:名单同源硬断言 ---------------------------------------------
    frozen = json.loads(FROZEN_TURTLE.read_text(encoding="utf-8"))
    declared_wl = frozenset(
        (s.split("|")[0], 1 if s.split("|")[1] == "long" else -1)
        for s in frozen["config"]["whitelist"])
    if declared_wl != frozenset(FROZEN_ERA_WHITELIST):
        raise RuntimeError(f"FROZEN_ERA_WHITELIST != 冻结产物自述名单:"
                           f"{sorted(declared_wl)}")

    # -- 探针:冻结时代名单(8 档)同一批战役帧 ⇒ 与冻结产物逐格全等(回归锚) ----
    rp_probe = dc_replace(rp_base, whitelist=frozenset(FROZEN_ERA_WHITELIST))
    print("[probe] 冻结时代名单探针(8 档;代码零回归锚,只读比对冻结产物)",
          flush=True)
    res_probe = run_grid(frames_bar, raw_cfg, params, rp_probe, contracts,
                         enable_mic=False)
    parity_rows = []
    for (seg, mult), (_r, m) in sorted(res_probe.items()):
        tag = f"{SEMANTICS}|{seg}@{mult}x"
        fm = frozen["runs"][tag]["metrics"]
        row = {
            "run": tag,
            "probe_net": m["net_pnl"], "frozen_net": fm["net_pnl"],
            "probe_trades": m["n_trades"], "frozen_trades": fm["n_trades"],
            "probe_pf": m["profit_factor"], "frozen_pf": fm["profit_factor"],
            "probe_winrate": m["winrate"], "frozen_winrate": fm["winrate"],
        }
        ok = (m["net_pnl"] == fm["net_pnl"] and m["n_trades"] == fm["n_trades"]
              and m["winrate"] == fm["winrate"]
              and m["profit_factor"] == fm["profit_factor"])
        row["parity"] = ok
        parity_rows.append(row)
        if not ok:
            raise RuntimeError(f"冻结时代探针与冻结产物不一致 {tag}: {row}")
    print("[xcheck] 冻结时代探针 9 格 == canonical v6 臂 B 冻结产物(逐格全等)",
          flush=True)

    # -- 臂一:bar(现行 canonical 名单,16 档;enable_mic=False) ------------------
    res_bar = run_grid(frames_bar, raw_cfg, params, rp_base, contracts,
                       enable_mic=False)

    # -- log#21 名单叉差归因(现行 16 档 bar 臂 vs 冻结时代产物;背景参照) --------
    wl_delta_rows = []
    for (seg, mult), (_r, m) in sorted(res_bar.items()):
        tag = f"{SEMANTICS}|{seg}@{mult}x"
        fm = frozen["runs"][tag]["metrics"]
        wl_delta_rows.append({
            "run": tag,
            "bar16_net": m["net_pnl"], "frozen8_net": fm["net_pnl"],
            "delta_net": m["net_pnl"] - fm["net_pnl"],
            "bar16_trades": m["n_trades"], "frozen8_trades": fm["n_trades"],
            "delta_trades": m["n_trades"] - fm["n_trades"]})
    print("[xcheck] log#21 名单叉差(16 档现义 − 8 档冻义)已如实记录"
          "(背景归因,非违规)", flush=True)

    # -- 臂二:cascade(级联触发,主交付) ------------------------------------------
    res_mic = run_grid(frames_mic, raw_cfg, params, rp_base, contracts,
                       enable_mic=True)

    adv_stats = {}
    for (seg, mult), (res, _m) in sorted(res_mic.items()):
        adv_stats[f"cascade|{seg}@{mult}x"] = mic_advance_stats(res, frames_mic)

    # -- 裁决对比(v10 §C 口径) ---------------------------------------------------
    def cell(results, seg, mult):
        m = results[(seg, mult)][1]
        return {"net_pnl": m["net_pnl"], "n_trades": m["n_trades"],
                "winrate": m["winrate"], "profit_factor": m["profit_factor"],
                "expectancy_usdt": m["expectancy_usdt"],
                "expectancy_r": m["expectancy_r"],
                "max_dd_pct": m["max_dd_pct"]}

    adjudication = {
        "dev_1_5x_cascade_vs_bar": {
            "cascade": cell(res_mic, "dev", 1.5),
            "bar(v13-canonical16)": cell(res_bar, "dev", 1.5),
            "delta_net": res_mic[("dev", 1.5)][1]["net_pnl"]
            - res_bar[("dev", 1.5)][1]["net_pnl"],
            "note": "同名语义对照(唯一差异=知时细化);v10 §C 判读主锚",
        },
        "dev_1_5x_vs_frozen_armB": {
            "cascade": cell(res_mic, "dev", 1.5),
            "frozen(backtest_v6_turtle)": cell(
                {("dev", 1.5): (None, frozen["runs"]["turtle|dev@1.5x"]["metrics"])},
                "dev", 1.5),
            "delta_net": res_mic[("dev", 1.5)][1]["net_pnl"]
            - frozen["runs"]["turtle|dev@1.5x"]["metrics"]["net_pnl"],
            "cross_semantics_note": "冻结产物 = 冻结时代 8 档名单(#16 时代,"
                                    "log#21 前);cascade/bar 在册两臂 = 现行 16 "
                                    "档语义路由 ⇒ 本对照跨名单语义,仅作背景"
                                    "参照;名单叉差见 canonical19_whitelist_delta。",
        },
        "test_once_exposure_16": {
            "cascade": {k: cell(res_mic, "test", mm) for k, mm in
                        (("1x", 1.0), ("1_5x", 1.5), ("2x", 2.0))},
            "bar(v13-canonical16)": {k: cell(res_bar, "test", mm) for k, mm in
                                     (("1x", 1.0), ("1_5x", 1.5), ("2x", 2.0))},
            "frozen8_era_reference": {k: cell(
                {("test", mm): (None, frozen["runs"][f"turtle|test@{mm}x"]["metrics"])},
                "test", mm) for k, mm in
                (("1x", 1.0), ("1_5x", 1.5), ("2x", 2.0))},
        },
        "interpretation_note": None,
    }
    d15 = adjudication["dev_1_5x_cascade_vs_bar"]["delta_net"]
    adjudication["interpretation_note"] = (
        f"级联早到(dev@1.5x Δ净 = {d15:+.2f})"
        + ("仍未转正 ⇒ 『晚报』假说死于『拥挤+薄利』双因候选成立,"
           "按 v10-protocol §C 不再追更早;" if d15 <= 0 or
           (adjudication["dev_1_5x_cascade_vs_bar"]["cascade"]["expectancy_usdt"]
            or 0) <= 0
           else "dev 有增量,test 判据见下;"))

    # -- GO/NO-GO(沿用 v6 升级线;test 已用唯一一次曝光) -------------------------
    m_mic_test1x = res_mic[("test", 1.0)][1]
    n_test = m_mic_test1x["n_trades"]
    exp_15x = res_mic[("test", 1.5)][1]["expectancy_usdt"]
    pf_2x = res_mic[("test", 2.0)][1]["profit_factor"]
    verdict = verdict_block(
        n_test, exp_15x, pf_2x,
        note=("v13 级联触发尺度一次性 test 评估(第 16 次曝光,research-log "
              "#26 预注册);GO/NO-GO 沿用 v6 升级线,如实填报。"))
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

    # -- CSV 产物(v13 独立文件名;不覆写任何历史产物) ------------------------------
    REPORTS.mkdir(exist_ok=True)
    for arm, results in (("bar", res_bar), ("cascade", res_mic)):
        for (seg, mult), (res, _m) in sorted(results.items()):
            suffix = "" if mult == 1.0 else f"_cost{str(mult).replace('.', '_')}"
            trades_frame_v13(res.trades).to_csv(
                REPORTS / f"trades_v13_cascade_{arm}__{seg}{suffix}.csv",
                index=False)
            rb.equity_frame(res).to_csv(
                REPORTS / f"equity_v13_cascade_{arm}__{seg}{suffix}.csv",
                index=False)

    # -- JSON 产物 ---------------------------------------------------------------
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "semantics": SEMANTICS,
        "protocol": {
            "doc": "docs/v13-protocol.md(冻结,2026-10-20)",
            "meta": raw_cfg["meta"],
            "cascade_semantics": "认知帧(1h/2h/4h/12h/1d)结构/区域/episode/pivot/"
                                 "regime 锁定;收盘类确认(E1 站稳/E2 收复/E3 回退/"
                                 "E4 回收/E6 破坏/E7 回踩拒绝)允许在战役映射的 "
                                 "trigger_tf 收盘先满足;N_a_mic=N_a×scale、"
                                 "N_f_mic=N_f×scale;阈值=最近已收认知 bar 的 "
                                 "ATR;知时=trigger 收盘,成交=次一 trigger 开盘;"
                                 "收割/费用/容量/组合不变。",
            "test_eval_declaration": "test 段第 16 次评估曝光(research-log #26 "
                                     "预注册,一次性,此后只许前向);本报告如实"
                                     "填报,不产生额外叙述。",
            "e1_e8_detector_invariant": "E1-E8 bar 事件检测零改动;mic 帧复放的 "
                                        "bar 事件流 == prepare_cognition 逐字段"
                                        "硬断言(见 bar_stream_parity)。",
            "parity_discipline": "双锚:冻结时代名单(8 档,与 backtest_v6_turtle "
                                 "自述 config 一致)探针 == 冻结产物逐格全等(代码"
                                 "零回归,只读比对);在册 bar/cascade 两臂 = 现行 "
                                 "canonical.json 16 档语义路由(log#21),与冻结"
                                 "产物之差如实归因名单语义,见 "
                                 "canonical19_whitelist_delta。",
            "engine_causality_fix": "v13 在途发现并修补 broker mic 撮合缝:"
                                    "on_bar_open/on_15m_bar 的非 mic 路径此前"
                                    "不排除 meta.mic 订单,级联(trigger 可粗于"
                                    "执行帧)下中间执行细 bar 会先于 mic 决策"
                                    "收盘撮合 mic 订单(前馈;实测 12h→2h 战役"
                                    "提前量 660min > 上界 600min 暴露)。修补="
                                    "非 mic 路径对称排除 mic 订单(因果护栏);"
                                    "canonical(无 mic)逐位不变 —— 由全仓 407+"
                                    "老测试与冻结时代探针 9 格 parity 钉死;"
                                    "v10 语义(trigger 恒细于执行帧)同 ts 同价"
                                    "数值不变,v10 冻结产物不受影响。",
            "mapping_declarations": declarations,
        },
        "config": {**{k: raw_cfg[k] for k in (
            "version", "enable_mic", "trigger_tf_map", "risk_frac",
            "leverage_cap", "taker_fee", "maker_fee", "slippage_per_side",
            "funding_per_8h_per_side", "dev_frac", "portfolio_cap_frac",
            "n_fill", "exec_tf_seconds", "merge_max_atr", "gates_model")},
            "equity0": 300.0, "tf_list": tf_list, "k_is": rp_base.k_is,
            "k_dq": rp_base.k_dq, "k_imp": rp_base.k_imp,
            "turtle_stop_atr": rp_base.turtle_stop_atr,
            "whitelist": [f"{i}|{'long' if d > 0 else 'short'}"
                          for i, d in sorted(whitelist)],
            "cost_mults": list(COST_MULTS),
            "scale_by_tf": {tf: int(agg.TF_HOURS[tf] * 3600
                                    // tf_map[agg.TF_HOURS[tf] * 3600])
                            for tf in tf_list},
            "n_a_mic_by_tf": {tf: int(raw_cfg["N_a"]) * int(
                agg.TF_HOURS[tf] * 3600 // tf_map[agg.TF_HOURS[tf] * 3600])
                for tf in tf_list},
            "n_f_mic_by_tf": {tf: int(raw_cfg["N_f"]) * int(
                agg.TF_HOURS[tf] * 3600 // tf_map[agg.TF_HOURS[tf] * 3600])
                for tf in tf_list}},
        "contracts": {
            "source": spec_src,
            "cache_path": str(CONTRACTS_PATH.relative_to(ROOT)),
            "cache_sha256": contracts_sha,
        },
        "data_30m": {"files": {sym: str(agg.raw_30m_path(sym))
                               for sym in symbols},
                     "note": "15m 2:1 聚合(v13 §1);manifest 含源流 15m sha256 "
                             "与锚定 1h 对账记录(构建即硬断言)"},
        "cross_checks_canonical_baseline": xcheck,
        "bar_stream_parity": {"description": "mic 帧复放 bar 事件流 == "
                                             "prepare_cognition 逐字段",
                              "n_events_by_campaign": mic_parity},
        "frozen_era_parity_probe": {
            "description": "冻结时代名单(8 档 E1/E3/E6/E7)同一批战役帧回放 "
                           "== reports/backtest_v6_turtle.json 逐格全等(硬断言;"
                           "代码零回归锚,与 v10 同锚,历史产物只读不覆写)",
            "runs": parity_rows},
        "canonical19_whitelist_delta": {
            "description": "现行 canonical.json 16 档语义路由 bar 臂 vs 冻结时代 "
                           "产物(8 档名单)之差;纯归因 log#21 决议(F1/room/风险"
                           "几何闸保留),非本轮机制引入,不视为违规",
            "runs": wl_delta_rows},
        "dev_signal_counts_bar": rb.signal_counts(frames_bar, dev_frac),
        "dev_signal_counts_cascade": mic_signal_counts(frames_mic, dev_frac),
        "adjudication": adjudication,
        "mic_advance_stats": adv_stats,
        "sample_size": sample,
        "verdict_v13": verdict,
        "runs": {},
    }
    for arm, results in (("bar", res_bar), ("cascade", res_mic)):
        for (seg, mult), (res, m) in sorted(results.items()):
            payload["runs"][f"{arm}|{seg}@{mult}x"] = {
                "metrics": m,
                "counters": res.counters,
                "bucket_tables": rb.bucket_tables(res.trades),
                "giveback": rb.giveback_summary(res.trades),
                "advance": (mic_advance_stats(res, frames_mic)
                            if arm == "cascade" else None)}

    (REPORTS / "backtest_v13_cascade.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (REPORTS / "backtest_v13_cascade.md").write_text(render_md(payload),
                                                     encoding="utf-8")
    print(f"-> reports/backtest_v13_cascade.json/.md + v13 CSVs "
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
        "# v13 级联触发尺度回测报告(1h→15m/2h→30m/4h→1h/12h→2h/1d→4h × turtle)",
        "",
        f"生成时间(UTC):{payload['generated_at_utc']}",
        "",
        "## 0. 地位与诚实性声明",
        "",
        f"- 机制:{payload['protocol']['cascade_semantics']}",
        f"- **{payload['protocol']['test_eval_declaration']}**",
        f"- 检测器不变式:{payload['protocol']['e1_e8_detector_invariant']}",
        f"- parity 纪律:{payload['protocol']['parity_discipline']}",
        f"- **在途因果修补:{payload['protocol']['engine_causality_fix']}**",
        "- 级联声明:"
        + (";".join(payload["protocol"]["mapping_declarations"])
           if payload["protocol"]["mapping_declarations"]
           else "全部五档映射帧可用,精确命中(12h→2h、1d→4h 即协议 §1 对 "
                "obs/4=3h/6h 无现帧的『最近可用』声明档,映射表即声明)。"),
        "- 30m 数据:15m 2:1 聚合(v13 §1),manifest 含源流 sha256 与锚定 1h "
        "逐位对账记录。",
        f"- 合约:{payload['contracts']['source']}(离线强制)。",
        "",
        "- 数量闸门(scale = obs/trigger):"
        + ", ".join(f"{tf}→×{cfg['scale_by_tf'][tf]}(N_a_mic="
                    f"{cfg['n_a_mic_by_tf'][tf]},N_f_mic={cfg['n_f_mic_by_tf'][tf]})"
                    for tf in ("1h", "2h", "4h", "12h", "1d")),
        "",
        "## 1. 总览(段 × 成本;权益 300 USDT 起;cascade 臂)",
        "",
        "| 段 | 成本 | 净盈亏 | 收益率 | 最大回撤% | 交易数 | 胜率 | 盈亏因子 | 期望USDT | 期望R |",
        "|---|---|---|---|---|---|---|---|---|---|"]
    for seg, mult, r in _grid_runs(payload, "cascade"):
        m = r["metrics"]
        lines.append(
            f"| {seg} | {mult}x | {_f(m['net_pnl'])} "
            f"| {_f(100 * m['return_pct'])}% | {_f(100 * m['max_dd_pct'])}% "
            f"| {m['n_trades']} | {_f(m['winrate'], 3)} "
            f"| {_f(m['profit_factor'])} | {_f(m['expectancy_usdt'], 4)} "
            f"| {_f(m['expectancy_r'], 4)} |")
    lines += ["", "对照 bar 臂(现行 canonical 16 档,parity 在册基线):", "",
              "| 段 | 成本 | 净盈亏 | 交易数 | 胜率 | 盈亏因子 | 期望USDT |",
              "|---|---|---|---|---|---|---|"]
    for seg, mult, r in _grid_runs(payload, "bar"):
        m = r["metrics"]
        lines.append(
            f"| {seg} | {mult}x | {_f(m['net_pnl'])} | {m['n_trades']} "
            f"| {_f(m['winrate'], 3)} | {_f(m['profit_factor'])} "
            f"| {_f(m['expectancy_usdt'], 4)} |")

    lines += ["", "### 1a. 冻结时代探针 == 冻结产物(逐格全等,回归锚)", "",
              "| 格 | 探针净 | 冻结净 | 探针笔数 | 冻结笔数 | parity |",
              "|---|---|---|---|---|---|"]
    for r in payload["frozen_era_parity_probe"]["runs"]:
        lines.append(f"| {r['run']} | {_f(r['probe_net'])} "
                     f"| {_f(r['frozen_net'])} | {r['probe_trades']} "
                     f"| {r['frozen_trades']} | {'✓' if r['parity'] else '✗'} |")
    lines += ["", "### 1b. log#21 名单叉差(16 档现义 bar − 8 档冻义;背景归因)", "",
              "| 格 | bar16 净 | 冻8 净 | Δ净 | bar16 笔数 | 冻8 笔数 | Δ笔数 |",
              "|---|---|---|---|---|---|---|"]
    for r in payload["canonical19_whitelist_delta"]["runs"]:
        lines.append(f"| {r['run']} | {_f(r['bar16_net'])} "
                     f"| {_f(r['frozen8_net'])} | {r['delta_net']:+.2f} "
                     f"| {r['bar16_trades']} | {r['frozen8_trades']} "
                     f"| {r['delta_trades']} |")

    lines += ["", "## 2. 裁决对比(v10 §C 口径 + test 一次性曝光)", "",
              "### 2.1 dev@1.5x:cascade vs 同程 bar 臂(同名语义,主锚)", "",
              "| 臂 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|"]
    for arm in ("cascade", "bar(v13-canonical16)"):
        c = adj["dev_1_5x_cascade_vs_bar"][arm]
        lines.append(f"| {arm} | {_f(c['net_pnl'])} | {c['n_trades']} "
                     f"| {_f(c['winrate'], 3)} | {_f(c['profit_factor'])} "
                     f"| {_f(c['expectancy_usdt'], 4)} "
                     f"| {_f(c['expectancy_r'], 4)} |")
    lines.append(f"| **Δ(cascade−bar)** | **{_f(adj['dev_1_5x_cascade_vs_bar']['delta_net'])}** "
                 f"| — | — | — | — | — |")
    c = adj["dev_1_5x_vs_frozen_armB"]
    lines += ["", "### 2.2 dev@1.5x:cascade vs canonical v6 臂 B 冻结产物"
              "(跨名单语义,背景参照)", "",
              "| 臂 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT | 期望R |",
              "|---|---|---|---|---|---|---|",
              f"| cascade | {_f(c['cascade']['net_pnl'])} "
              f"| {c['cascade']['n_trades']} | {_f(c['cascade']['winrate'], 3)} "
              f"| {_f(c['cascade']['profit_factor'])} "
              f"| {_f(c['cascade']['expectancy_usdt'], 4)} "
              f"| {_f(c['cascade']['expectancy_r'], 4)} |",
              f"| frozen backtest_v6_turtle(8 档冻义) | {_f(c['frozen(backtest_v6_turtle)']['net_pnl'])} "
              f"| {c['frozen(backtest_v6_turtle)']['n_trades']} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['winrate'], 3)} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['profit_factor'])} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['expectancy_usdt'], 4)} "
              f"| {_f(c['frozen(backtest_v6_turtle)']['expectancy_r'], 4)} |",
              f"| **Δ(cascade−frozen)** | **{_f(c['delta_net'])}** | — | — | — | — | — |",
              "",
              f"> {c['cross_semantics_note']}", "",
              f"> {adj['interpretation_note']}",
              "",
              "### 2.3 test 段(第 16 次曝光,一次性)", "",
              "| 臂 | 成本 | 净盈亏 | 笔数 | 胜率 | PF | 期望USDT |",
              "|---|---|---|---|---|---|---|"]
    for arm in ("cascade", "bar(v13-canonical16)", "frozen8_era_reference"):
        for kk in ("1x", "1_5x", "2x"):
            cc = adj["test_once_exposure_16"][arm][kk]
            lines.append(f"| {arm} | {kk} | {_f(cc['net_pnl'])} "
                         f"| {cc['n_trades']} | {_f(cc['winrate'], 3)} "
                         f"| {_f(cc['profit_factor'])} "
                         f"| {_f(cc['expectancy_usdt'], 4)} |")

    lines += ["", "## 3. mic 提前量(入场时刻相对认知收盘,分钟;"
              "市价口径:限价挂单成交时点含窗口等待,单独列示)", "",
              "### 3.1 合并 × 逐 TF(市价,分位)", "",
              "| 段@成本 | mic 市价成交 | 均值 | p25 | p50 | p75 | 最小 | 最大 |",
              "|---|---|---|---|---|---|---|---|"]
    for tag, st in payload["mic_advance_stats"].items():
        if st["n"] == 0:
            continue
        q = st["overall_market"]
        lim = st.get("by_kind", {}).get("limit", {})
        lim_note = f"(限价 {lim['n']} 笔,均 {_f(lim.get('mean_min'), 1)})" \
            if lim.get("n") else ""
        lines.append(f"| {tag} {lim_note} | {st['n_market']} "
                     f"| {_f(q['mean_min'], 1)} | {_f(q['p25_min'], 0)} "
                     f"| {_f(q['p50_min'], 0)} | {_f(q['p75_min'], 0)} "
                     f"| {_f(q['min_min'], 0)} | {_f(q['max_min'], 0)} |")
    lines += ["", "### 3.2 逐 TF 提前量分位(full@1x,市价)", "",
              "| TF | n | 均值 | p25 | p50 | p75 | 最小 | 最大 |",
              "|---|---|---|---|---|---|---|---|"]
    st_full = payload["mic_advance_stats"].get("cascade|full@1.0x", {})
    for tf, q in st_full.get("by_tf", {}).items():
        lines.append(f"| {tf} | {q['n']} | {_f(q['mean_min'], 1)} "
                     f"| {_f(q['p25_min'], 0)} | {_f(q['p50_min'], 0)} "
                     f"| {_f(q['p75_min'], 0)} | {_f(q['min_min'], 0)} "
                     f"| {_f(q['max_min'], 0)} |")
    lines += ["", "### 3.3 逐战役提前量(full@1x,市价)", "",
              "| 战役 | n | 均值 | p25 | p50 | p75 | 最小 | 最大 |",
              "|---|---|---|---|---|---|---|---|"]
    for k, v in st_full.get("by_campaign", {}).items():
        q = v["market"]
        lines.append(f"| {k} | {q['n']} | {_f(q['mean_min'], 1)} "
                     f"| {_f(q['p25_min'], 0)} | {_f(q['p50_min'], 0)} "
                     f"| {_f(q['p75_min'], 0)} | {_f(q['min_min'], 0)} "
                     f"| {_f(q['max_min'], 0)} |")

    up = payload["verdict_v13"]
    ss = payload["sample_size"]
    lines += ["", "## 4. 样本量与 GO/NO-GO(沿用 v6 升级线,如实填报)", "",
              f"- test 合并战役成交(cascade,1x,OPEN ∈ test):**{ss['n_trades_test_1x']}**"
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
              "## 5. 已知近似与如实声明",
              "",
              "①采用窗口锚点(刺穿/ex 帧→首根 trigger)取保守(窗口最早关闭);"
              "②ATR 抖动边界可致认知破位被 mic 漏记(bar 事件照常,如实);"
              "③E6 同现象每认知覆盖区间至多一发(频率镜像认知层);④E1 在破位"
              "迟揭示角点可与 bar 事件同刻或晚 ≤trigger 步长(槽位先到先得);"
              "⑤12h→2h、1d→4h 为协议 §1『obs/4 无现帧取最近可用』声明档"
              "(scale 6);⑥parity 双锚:冻结时代名单探针与冻结产物逐格全等为"
              "硬断言(非抽样),在册 bar/cascade 两臂行于现行 16 档语义路由,"
              "与冻结产物之差归因 log#21(§1b 如实列数);⑦test 曝光纪律见 §0;"
              "⑧退出节拍(先损后盈 40.6%)修理非本轮目标,另立 v14;⑨broker "
              "mic 缝因果修补见 §0(canonical 逐位不变,v10 数值不变)。",
              "",
              "机器可读全量:`reports/backtest_v13_cascade.json`;逐笔:"
              "`reports/trades_v13_cascade_*.csv`(增列 mic/cog_index/"
              "advance_minutes);权益:`reports/equity_v13_cascade_*.csv`。",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
