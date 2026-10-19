"""v3 事件研究管线(docs/v3-protocol.md,冻结于 2026-10-19 首跑之前)。

用法:  python scripts/run_event_study_v3.py
产物:  reports/event_study_v3.json                 机器可读全量(逐 TF × 机制 ×
                                                  段 × 成本压力,同 §9 口径)
       reports/event_study_v3.md                  人读报告(8 格子完整公布)
       reports/event_study_events_v3_{tf}_{dev,test}.csv  逐格逐事件明细
                                                  (含对照增量,命名含 TF)

纪律(v3-protocol §1-§3,冻结):
- 唯一变更 = 观察尺度 TF ∈ {2h,4h,12h,1d}(aggregate.ensure_aggregated 由
  带哈希 1h 数据 UTC 对齐聚合,幂等;不重抓 API,不改任何先验参数)。
- 机制 = config/v2.json whitelist_v2({E7|short, E7|long}) + F1(k_is=0.8
  先验);E7 发射 enable_e7=True;评估协议 = methodology §9 全流程原样
  (H ∈ {6,12,24,48} 根、主视界 H=24、对照 42 种子、日块 bootstrap 2000、
  成本压力 1x/1.5x/2x、§9.3 四判据,增量口径同 v1/v2)。
- 预注册家族 8 格子(TF × 方向),所有格子数字完整公布,不挑格子报喜;
  n<60 ⇒ INSUFFICIENT_DATA,不做叙述性补救(§2)。
- **test 段第 5 次评估(v3 唯一一次;research-log 条目 7)**;此后任何迭代
  不得再用 test。
- 尺度语义(v3-protocol §1/§2;上游隐含周期假设审计,见交付摘要):
  H/N_time/对照排除窗均以**根数**计(bars 语义不变);funding 按真实持仓
  时长计费 = horizon 根 × bar_hours 小时/根(research._net_row bar_hours
  口径,1h 恒等)——v2 管线对非 1h 输入会把"根"误当"小时",本管线按
  协议 §2 "长尺度持仓更久自动如实计入"修正(默认参数修复,v1/v2 产物不变)。
- dev/test 拐点(§2):同一拐点 = 1h bar 索引 12320 的 ts;每 (symbol, TF)
  断言聚合帧 split_boundary 索引桶起点恰为该 ts 的桶起点(核算在
  tests/test_aggregate.py;此处运行期再断言一次,不合格即 raise)。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import aggregate as agg                     # noqa: E402
from market_operator import data as mo_data                      # noqa: E402
from market_operator.hypothesis import f1_invalid_distance_ok    # noqa: E402
from market_operator.research import (                           # noqa: E402
    COST_MULTS,
    HORIZONS,
    MIN_EVENTS_GO,
    PRIMARY_HORIZON,
    CostModel,
    analyze_segment,
    collect_events,
    go_no_go,
    measure_controls,
    measure_events,
    split_boundary,
    _mult_tag,
)
from market_operator.structure import Params                     # noqa: E402

CONFIG_V2 = ROOT / "config" / "v2.json"
REPORTS = ROOT / "reports"
TF_LABELS = ("2h", "4h", "12h", "1d")
BOUNDARY_1H_INDEX = 12320               # v3-protocol §2 拐点(1h 索引)


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _f1_pass(events: list, close: pd.Series, k_is: float) -> list:
    """F1 门(v2-protocol §1):事件 bar 收盘至 invalid 的沿方向距离下限。"""
    return [e for e in events
            if f1_invalid_distance_ok(e, float(close[e.known_index]), k_is)]


def run_tf(tf_label: str, symbols: list, params: Params, cost: CostModel,
           event_ids: frozenset, whitelist: set, k_is: float,
           dev_frac: float, landmarks: dict) -> dict:
    """单 TF 全品种事件研究(v2 管线同路,bar_hours 按 TF 计 funding)。"""
    bar_hours = float(agg.TF_HOURS[tf_label])
    step_ms = int(bar_hours * agg.MS_PER_HOUR)
    all_events, all_ctrls = [], []
    sym_info: dict = {}
    f1_stats: dict = {}
    for sym in symbols:
        t0 = time.time()
        df = agg.load_tf(sym, tf_label)
        n = len(df)
        boundary = split_boundary(n, dev_frac)
        # v3-protocol §2:同一拐点(1h 索引 12320 的 ts)落在本帧 boundary 桶内
        ts_lm = landmarks[sym]
        assert int(df["ts"].iloc[boundary]) == (ts_lm // step_ms) * step_ms, (
            f"{sym}/{tf_label}: 拐点桶错位 boundary_ts={df['ts'].iloc[boundary]} "
            f"vs bucket({ts_lm})={(ts_lm // step_ms) * step_ms}")
        fired, timeline = collect_events(df, params, event_ids=event_ids,
                                         enable_e7=True)
        passed = _f1_pass(fired, df["close"], k_is)
        seg_of = lambda e: "dev" if e.known_index < boundary else "test"
        f1_stats[sym] = {
            k: {"fired": sum(1 for e in fired
                             if seg_of(e) == k
                             and (e.id, e.direction) in whitelist),
                "passed_f1": sum(1 for e in passed
                                 if seg_of(e) == k
                                 and (e.id, e.direction) in whitelist)}
            for k in ("dev", "test")}
        rows = measure_events(df, passed, timeline, cost, bar_hours=bar_hours)
        rows["symbol"] = sym
        rows["uid"] = sym + "#" + rows["uid"].astype(str)
        rows["segment"] = ["dev" if k < boundary else "test"
                           for k in rows["known_index"]]
        ctrl_parts = []
        for seg, (lo, hi) in (("dev", (0, boundary)), ("test", (boundary, n))):
            seg_rows = rows[rows["segment"] == seg]
            ctrl = measure_controls(df, passed, timeline, seg_rows, cost, lo, hi,
                                    bar_hours=bar_hours)
            ctrl["segment"] = seg
            ctrl["symbol"] = sym
            ctrl_parts.append(ctrl)
        all_events.append(rows)
        all_ctrls.append(pd.concat(ctrl_parts, ignore_index=True))
        sym_info[sym] = {
            "bars": n, "n_events_fired": len(fired), "n_passed_f1": len(passed),
            "dev_end_index": boundary,
            "dev_end_ts": int(df["ts"][boundary]),
            "dev_end_iso": _iso(int(df["ts"][boundary])),
            "boundary_bucket_of_12320_ok": True,
            "elapsed_s": round(time.time() - t0, 1),
        }
        print(f"  [{tf_label}/{sym}] {n} bars, {len(fired)} E7 fired, "
              f"{len(passed)} pass F1, {sym_info[sym]['elapsed_s']}s",
              flush=True)

    ev = pd.concat(all_events, ignore_index=True)
    ct = pd.concat(all_ctrls, ignore_index=True)

    # 逐事件明细(合并对照,增列 incr_*;命名含 TF)
    merged = ev.merge(ct, on=["uid", "symbol", "segment", "known_index",
                              "event_id", "mechanism", "H"], how="left")
    for m in COST_MULTS:
        tag = _mult_tag(m)
        merged[f"incr_atr_{tag}"] = (merged[f"net_atr_{tag}"]
                                     - merged[f"ctrl_net_atr_mean_{tag}"])
        merged[f"incr_pct_{tag}"] = (merged[f"net_pct_{tag}"]
                                     - merged[f"ctrl_net_pct_mean_{tag}"])
    merged.insert(0, "tf", tf_label)
    detail_paths = {}
    for seg in ("dev", "test"):
        path = REPORTS / f"event_study_events_v3_{tf_label}_{seg}.csv"
        merged[merged["segment"] == seg].to_csv(path, index=False)
        detail_paths[seg] = str(path.relative_to(ROOT))

    mechanisms: dict = {}
    for mech, g_ev in ev.groupby("mechanism"):
        g_ct = ct[ct["mechanism"] == mech]
        mech_out: dict = {}
        for seg in ("dev", "test"):
            s_ev = g_ev[g_ev["segment"] == seg]
            s_ct = g_ct[g_ct["segment"] == seg]
            analysis = analyze_segment(s_ev, s_ct)
            per_symbol = {s: int(g["uid"].nunique())
                          for s, g in s_ev.groupby("symbol")}
            analysis["per_symbol_n"] = per_symbol
            if seg == "test":
                analysis["go_no_go"] = go_no_go(analysis, per_symbol)
            mech_out[seg] = analysis
        mechanisms[mech] = mech_out
        v = mech_out["test"].get("go_no_go", {}).get("verdict", "?")
        nt = mech_out["test"]["n_events"]
        print(f"  {tf_label:4s} {mech:12s} test n={nt:4d}  verdict={v}",
              flush=True)

    return {"bar_hours": bar_hours, "symbols": sym_info, "f1_gate": f1_stats,
            "mechanisms": mechanisms, "detail_paths": detail_paths}


def run() -> dict:
    raw_cfg = json.loads(CONFIG_V2.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V2)          # 未知键忽略;先验逐项 = defaults
    cost = CostModel.from_config(CONFIG_V2)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    k_is = float(raw_cfg["k_is"])
    whitelist = {(str(i), int(d)) for i, d in raw_cfg["whitelist_v2"]}
    event_ids = frozenset({i for i, _ in whitelist})
    assert whitelist == {("E7", -1), ("E7", 1)}, "v3 机制白名单 = v2 whitelist"

    # 聚合幂等保障(§1:不重抓 API;同 1h 输入 ⇒ 同字节,manifest 不漂移)
    agg.ensure_aggregated(symbols, TF_LABELS)
    # v3-protocol §2 拐点证据(每品种 1h 索引 12320 的 ts)
    landmarks = {}
    for sym in symbols:
        df1 = mo_data.load(sym)
        landmarks[sym] = int(df1["ts"].iloc[BOUNDARY_1H_INDEX])

    result: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v3-protocol.md(评估 = methodology §9 原样,"
                      "机制 = config/v2.json whitelist + F1)",
            "meta": raw_cfg["meta"], "whitelist_v2": raw_cfg["whitelist_v2"],
            "k_is": k_is,
            "timeframes": {tf: agg.TF_HOURS[tf] for tf in TF_LABELS},
            "horizons": list(HORIZONS), "primary_horizon": PRIMARY_HORIZON,
            "cost_mults": list(COST_MULTS), "dev_frac": dev_frac,
            "n_seeds": 42, "boot_reps": 2000, "event_excl_bars": 24,
            "min_events_go": MIN_EVENTS_GO,
            "increment_convention": "net = event net - mean(control net); "
                                    "GO/NO-GO criteria 2/3/4 use this increment",
            "cost_model": vars(cost),
            "bar_semantics": "H/N_time/EVENT_EXCL_BARS 以根数计(bars 语义不变,"
                             "v3-protocol §1);funding 按持仓真实时长计费 = "
                             "horizon·bar_hours 小时(research bar_hours 口径,"
                             "1h 恒等,MULTI_TF 修复)",
            "boundary_landmark": {"n_1h": 17600, "index": BOUNDARY_1H_INDEX,
                                  "ts": landmarks},
            "test_eval_declaration": "test 段第 5 次评估(v3 唯一一次;此后任何"
                                     "迭代不得再用 test;research-log 条目 7)",
            "family_declaration": "预注册家族 1 个,最多 8 格子(TF × 方向);"
                                  "全部格子数字完整公布,整体判断,不挑格子报喜",
        },
        "timeframes": {},
        "cells": {},
    }
    for tf_label in TF_LABELS:
        print(f"[tf={tf_label}] bar_hours={agg.TF_HOURS[tf_label]}", flush=True)
        result["timeframes"][tf_label] = run_tf(
            tf_label, symbols, params, cost, event_ids, whitelist, k_is,
            dev_frac, landmarks)

    # 家族格子汇总(8 格完整公布)
    for tf_label in TF_LABELS:
        for mech in ("E7|short", "E7|long"):
            tt = result["timeframes"][tf_label]["mechanisms"].get(mech, {})
            test = tt.get("test", {})
            result["cells"][f"{tf_label}|{mech}"] = {
                "tf": tf_label, "mechanism": mech,
                "n_events_test": test.get("n_events", 0),
                "verdict": (test.get("go_no_go", {}) or {}).get(
                    "verdict", "INSUFFICIENT_DATA"),
            }
    return result


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=4):
    return "—" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render_md(result: dict) -> str:
    p = result["protocol"]
    ph = str(p["primary_horizon"])
    lines = [
        "# v3 事件研究报告(docs/v3-protocol.md:多尺度 × E7±1 + F1)",
        "",
        f"生成时间(UTC):{result['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        "- 预注册家族 = 1 个,最多 8 格子:TF ∈ {2h,4h,12h,1d} × {E7|short,"
        "E7|long};**所有格子数字完整公布,整体判断**(§2)。",
        "- 机制 = config/v2.json whitelist_v2 + F1(k_is=0.8 先验);E7 发射 "
        "enable_e7=True;全部 ATR/结构/成本先验零改动。",
        "- 评估 = methodology §9 全流程原样:H ∈ "
        f"{p['horizons']} 根(主视界 H={p['primary_horizon']}),对照 42 固定种子"
        "(同品种/同 regime/同方向/避同机制事件窗口 24 根),日块 bootstrap 2000"
        " 次 95% CI,成本压力 1x/1.5x/2x,§9.3 四判据,**增量口径**与 v1/v2 一致。",
        f"- **重评声明(§9.4/v3-protocol §4)**:{p['test_eval_declaration']}。"
        "本报告为该次评估产物。",
        "- 尺度语义(v3-protocol §1/§2):" + p["bar_semantics"] + "。",
        "- dev/test 拐点(v3-protocol §2):同一拐点 = 1h 索引 12320 的 ts(每"
        "品种);逐 (symbol, TF) 断言聚合帧 split_boundary 索引桶起点 = 该 ts"
        " 的桶起点(全部通过,tests/test_aggregate.py 另有硬编码核算:2h→6160,"
        "4h→3080,12h→1027,1d→514)。",
        "",
        "## 1. GO / NO-GO 家族判定表(test 段,H=" + ph + ",判据同 §9.3;8 格完整公布)",
        "",
        "| TF | 机制 | n(test) | 判1 n≥60 | 判2 增量>0@1.5x | 判3 CI下界>0@1x | 判4 ≥2品种正 | 判定 |",
        "|---|---|---|---|---|---|---|---|"]
    for tf in TF_LABELS:
        mechs = result["timeframes"][tf]["mechanisms"]
        for mech in sorted(("E7|short", "E7|long")):
            md = mechs.get(mech)
            gg = (md or {}).get("test", {}).get("go_no_go")
            if not gg:
                lines.append(f"| {tf} | {mech} | 0 | — | — | — | — "
                             f"| INSUFFICIENT_DATA |")
                continue
            c = lambda b: "✓" if b else "✗"
            lines.append(
                f"| {tf} | {mech} | {gg['criterion1_events_ge_60']['n_events_total']} "
                f"| {c(gg['criterion1_events_ge_60']['pass'])} "
                f"| {c(gg['criterion2_incr_positive_at_1_5x']['pass'])} "
                f"| {c(gg['criterion3_ci_lower_gt_0_at_1x']['pass'])} "
                f"| {c(gg['criterion4_two_symbols_positive']['pass'])} "
                f"| **{gg['verdict']}** |")

    for tf in TF_LABELS:
        tf_out = result["timeframes"][tf]
        mechs = tf_out["mechanisms"]
        lines += ["", f"## 2-{tf}. {tf} 明细(bar_hours={tf_out['bar_hours']})", "",
                  "### 发射与过 F1(dev/test 分界每品种前 70%)", ""]
        for sym, info in tf_out["symbols"].items():
            st = tf_out["f1_gate"][sym]
            lines.append(
                f"- {sym}:dev_end = {info['dev_end_iso']} "
                f"(index {info['dev_end_index']} / {info['bars']});E7 fired "
                f"{info['n_events_fired']}(dev {st['dev']['fired']} / test "
                f"{st['test']['fired']}),过 F1 {info['n_passed_f1']}"
                f"(dev {st['dev']['passed_f1']} / test "
                f"{st['test']['passed_f1']})")
        lines += ["", "### 机制汇总(test 段,H=" + ph + ";ATR 单位)", "",
                  "| 机制 | n | 事件净@1x (CI95) | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量@2x |",
                  "|---|---|---|---|---|---|---|"]
        for mech in sorted(mechs):
            h = mechs[mech]["test"]["horizons"].get(ph)
            if not h:
                lines.append(f"| {mech} | 0 | — | — | — | — | — |")
                continue
            cc = h["cost"]
            c1 = cc["1_0"]
            lines.append(
                f"| {mech} | {h['n']} | {_f(c1['event_net_atr_mean'])} "
                f"[{_f(c1['event_net_atr_ci95'][0])}, {_f(c1['event_net_atr_ci95'][1])}] "
                f"| {_f(c1['ctrl_net_atr_mean'])} "
                f"| {_f(c1['incr_atr_mean'])} [{_f(c1['incr_atr_ci95'][0])}, "
                f"{_f(c1['incr_atr_ci95'][1])}] "
                f"| {_f(cc['1_5']['incr_atr_mean'])} "
                f"| {_f(cc['2_0']['incr_atr_mean'])} |")
        lines += ["", "### 全视界增量(test 段,增量@1x ATR)与分品种符号", "",
                  "| 机制 | H=6 | H=12 | H=24 | H=48 | n_pos(品种) |",
                  "|---|---|---|---|---|---|"]
        for mech in sorted(mechs):
            hz = mechs[mech]["test"]["horizons"]
            h24 = hz.get(ph)
            cells = [_f(hz[str(h)]["cost"]["1_0"]["incr_atr_mean"])
                     if hz.get(str(h)) else "—" for h in HORIZONS]
            npos = (sum(1 for v in h24["by_symbol"].values() if v["mean"] > 0.0)
                    if h24 and "by_symbol" in h24 else 0)
            lines.append(f"| {mech} | " + " | ".join(cells) + f" | {npos} |")
        lines += ["", "### dev↔test 一致性(诚实性参考;判据只依 test)", "",
                  "| 机制 | dev n | dev 增量@1x | test 增量@1x | 符号一致 |",
                  "|---|---|---|---|---|"]
        for mech in sorted(mechs):
            d = mechs[mech]["dev"]["horizons"].get(ph)
            t = mechs[mech]["test"]["horizons"].get(ph)
            if not d or not t:
                continue
            dv, tv = d["cost"]["1_0"]["incr_atr_mean"], t["cost"]["1_0"]["incr_atr_mean"]
            same = (dv > 0) == (tv > 0)
            lines.append(f"| {mech} | {mechs[mech]['dev']['n_events']} | {_f(dv)} "
                         f"| {_f(tv)} | {'✓' if same else '✗'} |")

    lines += ["", "## 3. 产物索引", ""]
    for tf in TF_LABELS:
        dp = result["timeframes"][tf]["detail_paths"]
        lines.append(f"- {tf}:逐事件 `reports/{dp['dev'].split('/')[-1]}`, "
                     f"`reports/{dp['test'].split('/')[-1]}`")
    lines += ["", "机器可读全量:`reports/event_study_v3.json`(逐 TF × 机制 × "
                  "段 × 成本 × 视界,含 regime 分解与稳定性)。", ""]
    return "\n".join(lines)


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    result = run()
    (REPORTS / "event_study_v3.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "event_study_v3.md").write_text(render_md(result), encoding="utf-8")
    print("-> reports/event_study_v3.json, reports/event_study_v3.md, "
          "reports/event_study_events_v3_{tf}_{dev,test}.csv")


if __name__ == "__main__":
    main()
