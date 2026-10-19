"""阶段 3 事件研究全管线(docs/methodology.md §9)。

用法:  python scripts/run_event_study.py
产物:  reports/event_study.json   机器可读全量指标(每机制 × 段 × 成本压力:
                                   净期望/增量/CI/样本数、对照组统计、regime 分解、
                                   稳定性、GO/NO-GO 判据明细、逐事件明细路径)
       reports/event_study.md    人读报告
       reports/event_study_events_{dev,test}.csv  逐事件明细(含对照增量)

纪律(§9.4):GO/NO-GO 只在 test 段评估;dev 段仅作合理性检查;先验来自
config/defaults.json,本脚本不修改任何先验。test 段只允许一次最终评估。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data                       # noqa: E402
from market_operator.research import (                            # noqa: E402
    COST_MULTS,
    DEFAULT_CONFIG,
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
from market_operator.structure import Params                      # noqa: E402

REPORTS = ROOT / "reports"


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def run() -> dict:
    raw_cfg = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    params = Params.from_json(DEFAULT_CONFIG)
    cost = CostModel.from_config(DEFAULT_CONFIG)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])

    all_events, all_ctrls = [], []
    sym_info: dict = {}
    for sym in symbols:
        t0 = time.time()
        df = mo_data.load(sym)
        n = len(df)
        boundary = split_boundary(n, dev_frac)
        events, timeline = collect_events(df, params)
        rows = measure_events(df, events, timeline, cost)
        rows["symbol"] = sym
        # uid 仅品种内唯一;跨品种合并分析前加品种前缀(否则对照聚合串品种)
        rows["uid"] = sym + "#" + rows["uid"].astype(str)
        rows["segment"] = ["dev" if k < boundary else "test"
                           for k in rows["known_index"]]
        # 对照组:同段采样(dev 事件取 dev 池,test 事件取 test 池)
        ctrl_parts = []
        for seg, (lo, hi) in (("dev", (0, boundary)), ("test", (boundary, n))):
            seg_rows = rows[rows["segment"] == seg]
            ctrl = measure_controls(df, events, timeline, seg_rows, cost, lo, hi)
            ctrl["segment"] = seg
            ctrl["symbol"] = sym
            ctrl_parts.append(ctrl)
        all_events.append(rows)
        all_ctrls.append(pd.concat(ctrl_parts, ignore_index=True))
        sym_info[sym] = {
            "bars": n, "n_events_raw": len(events),
            "dev_end_index": boundary,
            "dev_end_ts": int(df["ts"][boundary]),
            "dev_end_iso": _iso(int(df["ts"][boundary])),
            "elapsed_s": round(time.time() - t0, 1),
        }
        print(f"[{sym}] {n} bars, {len(events)} events, {sym_info[sym]['elapsed_s']}s")

    ev = pd.concat(all_events, ignore_index=True)
    ct = pd.concat(all_ctrls, ignore_index=True)

    result: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "horizons": list(HORIZONS), "primary_horizon": PRIMARY_HORIZON,
            "cost_mults": list(COST_MULTS), "dev_frac": dev_frac,
            "n_seeds": 42, "boot_reps": 2000, "event_excl_bars": 24,
            "min_events_go": MIN_EVENTS_GO,
            "increment_convention": "net = event net - mean(control net); "
                                    "GO/NO-GO criteria 2/3/4 use this increment",
            "cost_model": vars(cost),
        },
        "symbols": sym_info,
        "mechanisms": {},
        "detail_paths": {},
    }

    # 逐事件明细(含对照聚合与增量),按段落盘
    merged = ev.merge(ct, on=["uid", "symbol", "segment", "known_index",
                              "event_id", "mechanism", "H"], how="left")
    for m in COST_MULTS:
        tag = _mult_tag(m)
        merged[f"incr_atr_{tag}"] = (merged[f"net_atr_{tag}"]
                                     - merged[f"ctrl_net_atr_mean_{tag}"])
        merged[f"incr_pct_{tag}"] = (merged[f"net_pct_{tag}"]
                                     - merged[f"ctrl_net_pct_mean_{tag}"])
    for seg in ("dev", "test"):
        path = REPORTS / f"event_study_events_{seg}.csv"
        merged[merged["segment"] == seg].to_csv(path, index=False)
        result["detail_paths"][seg] = str(path.relative_to(ROOT))

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
        result["mechanisms"][mech] = mech_out
        v = mech_out["test"].get("go_no_go", {}).get("verdict", "?")
        nt = mech_out["test"]["n_events"]
        print(f"  {mech:12s} test n={nt:4d}  verdict={v}")

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
        "# 阶段 3 事件研究报告(方法论 §9)",
        "",
        f"生成时间(UTC):{result['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        f"- 视界 H ∈ {p['horizons']},主视界 H={p['primary_horizon']}"
        "(= §7 N_time 先验,先于结果固定);成本压力 1x/1.5x/2x。",
        f"- 成本模型(§9.2 先验,取自 config/defaults.json):{p['cost_model']}",
        f"- 对照组:同品种/同 regime/同方向,排除事件后 24 bar 窗口,42 固定种子。",
        "- **增量口径**:§9.3 判据 2/3/4 的“净期望”= 事件净期望 − 对照净期望均值"
        "(事件必须提供增量信息,§9.1-5);因双边同成本,增量对成本压力近似不变。",
        "- GO/NO-GO 仅在 test 段评估(判据照抄 §9.3);dev 段仅合理性检查。",
        "- **重评声明(§9.4)**:首次运行暴露实现错误(uid 跨品种碰撞、CI 被 NaN 传染,"
        "详见 docs/research-log.md 条目 4),修复后重评;本报告为修复后的唯一一次有效 "
        "test 评估,首次数值作废、不构成结论依据。dev 上的实现修正均见 docs/research-log.md。",
        "- dev/test 分界(每品种前 70% 为 dev):",
    ]
    for sym, info in result["symbols"].items():
        lines.append(f"  - {sym}:dev_end = {info['dev_end_iso']} "
                     f"(index {info['dev_end_index']} / {info['bars']}, "
                     f"原始事件 {info['n_events_raw']})")

    mechs = result["mechanisms"]
    lines += ["", "## 1. GO / NO-GO 判定表(test 段,H="
              + p["primary_horizon"].__str__() + ")", "",
              "| 机制 | n(test) | 判1 n≥60 | 判2 增量>0@1.5x | 判3 CI下界>0@1x | 判4 ≥2品种正 | 判定 |",
              "|---|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        gg = mechs[mech]["test"].get("go_no_go")
        if not gg:
            lines.append(f"| {mech} | 0 | — | — | — | — | INSUFFICIENT_DATA |")
            continue
        c = lambda b: "✓" if b else "✗"
        lines.append(
            f"| {mech} | {gg['criterion1_events_ge_60']['n_events_total']} "
            f"| {c(gg['criterion1_events_ge_60']['pass'])} "
            f"| {c(gg['criterion2_incr_positive_at_1_5x']['pass'])} "
            f"| {c(gg['criterion3_ci_lower_gt_0_at_1x']['pass'])} "
            f"| {c(gg['criterion4_two_symbols_positive']['pass'])} "
            f"| **{gg['verdict']}** |")

    lines += ["", "## 2. 机制汇总(test 段,H=" + ph + ";ATR 单位,%口径并列)", "",
              "| 机制 | n | 事件净@1x | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量%@1.5x |",
              "|---|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        h = mechs[mech]["test"]["horizons"].get(ph)
        if not h:
            lines.append(f"| {mech} | 0 | — | — | — | — | — |")
            continue
        c1, c15 = h["cost"]["1_0"], h["cost"]["1_5"]
        lines.append(
            f"| {mech} | {h['n']} | {_f(c1['event_net_atr_mean'])} "
            f"| {_f(c1['ctrl_net_atr_mean'])} "
            f"| {_f(c1['incr_atr_mean'])} [{_f(c1['incr_atr_ci95'][0])}, {_f(c1['incr_atr_ci95'][1])}] "
            f"| {_f(c15['incr_atr_mean'])} | {_f(c15['incr_pct_mean'], 5)} |")

    lines += ["", "## 3. 成本压力(test 段,H=" + ph + ",增量 ATR;增量近似成本不变)", "",
              "| 机制 | 增量@1x | 增量@1.5x | 增量@2x | 事件净@1x | 事件净@2x |",
              "|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        h = mechs[mech]["test"]["horizons"].get(ph)
        if not h:
            continue
        cc = h["cost"]
        lines.append(f"| {mech} | {_f(cc['1_0']['incr_atr_mean'])} "
                     f"| {_f(cc['1_5']['incr_atr_mean'])} | {_f(cc['2_0']['incr_atr_mean'])} "
                     f"| {_f(cc['1_0']['event_net_atr_mean'])} "
                     f"| {_f(cc['2_0']['event_net_atr_mean'])} |")

    lines += ["", "## 4. 命中与行程(test 段,H=" + ph + ")", "",
              "| 机制 | MFE(atr) | MAE(atr) | P(target先) | P(invalid先) | 有结构目标比例 |",
              "|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        h = mechs[mech]["test"]["horizons"].get(ph)
        if not h:
            continue
        lines.append(f"| {mech} | {_f(h['mfe_atr_mean'])} | {_f(h['mae_atr_mean'])} "
                     f"| {_f(h['hit']['target'])} | {_f(h['hit']['invalid'])} "
                     f"| {_f(h['has_target_frac'])} |")

    lines += ["", "## 5. regime 分解与稳定性(test 段,H=" + ph + ",增量@1x ATR)", ""]
    for mech in sorted(mechs):
        h = mechs[mech]["test"]["horizons"].get(ph)
        if not h or "by_regime" not in h:
            continue
        st = h["stability"]
        lines.append(f"### {mech}")
        lines.append("| regime | n | 增量均值 |")
        lines.append("|---|---|---|")
        for reg, v in sorted(h["by_regime"].items()):
            lines.append(f"| {reg} | {v['n']} | {_f(v['mean'])} |")
        lines.append("")
        lines.append(f"- 前/后半样本增量:{_f(st['first_half_incr_atr'])} / "
                     f"{_f(st['second_half_incr_atr'])}(分界 ts={st['split_ts']:.0f})")
        lines.append(f"- 分品种:{', '.join(f'{k}: n={v['n']}, 增量={_f(v['mean'])}' for k, v in sorted(h['by_symbol'].items()))}")
        lines.append("")

    lines += ["## 6. 全视界增量(test 段,增量@1x ATR)", "",
              "| 机制 | H=6 | H=12 | H=24 | H=48 |",
              "|---|---|---|---|---|"]
    for mech in sorted(mechs):
        hz = mechs[mech]["test"]["horizons"]
        cells = [_f(hz[str(h)]["cost"]["1_0"]["incr_atr_mean"]) if hz.get(str(h)) else "—"
                 for h in HORIZONS]
        lines.append(f"| {mech} | " + " | ".join(cells) + " |")

    lines += ["", "## 7. dev↔test 一致性(诚实性参考,非判据;判据只依 test)", "",
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

    lines += ["", f"逐事件明细:`{result['detail_paths']['dev']}`, "
                  f"`{result['detail_paths']['test']}`；全量指标:`reports/event_study.json`。", ""]
    return "\n".join(lines)


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    result = run()
    (REPORTS / "event_study.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "event_study.md").write_text(render_md(result), encoding="utf-8")
    print("-> reports/event_study.json, reports/event_study.md, "
          "reports/event_study_events_{dev,test}.csv")


if __name__ == "__main__":
    main()
