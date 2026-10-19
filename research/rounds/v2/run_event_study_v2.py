"""v2 事件研究管线(docs/v2-protocol.md §1-§3:E7 FLIP_RETEST + F1 失效距离下限)。

用法:  python scripts/run_event_study_v2.py
产物:  reports/event_study_v2.json                机器可读全量(E7|short/E7|long
                                                  × 段 × 成本压力,同 §9 口径)
       reports/event_study_v2.md                  人读报告
       reports/event_study_events_v2_{dev,test}.csv  逐事件明细(含对照增量)

纪律(v2-protocol §3,冻结于本脚本首次运行之前):
- 评估协议沿用 methodology §9 全流程原样(主视界 H=24、对照 42 种子、日块
  bootstrap 2000、成本压力 1x/1.5x/2x、§9.3 四判据,增量口径);机制 =
  E7|short 与 E7|long 分别判定。
- 唯一对事件集的改动 = F1 门(v2-protocol §1):dist(close(t), invalid) ≥
  k_is·atr(t),k_is=0.8 先验;被过滤事件记数报告。{0.6, 1.0} 敏感性仅报告
  (同 §1 声明),不参与判定、不择优。
- 本管线含 test 段 ⇒ test 第 4 次评估(v2 唯一一次;此后仅 dev,
  见 docs/research-log.md 条目 6)。n<60 ⇒ INSUFFICIENT_DATA,无叙述性补救。
- 不改 v1 任何先验/报告;config/defaults.json 只读(v2 读 config/v2.json)。
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

from market_operator import data as mo_data                       # noqa: E402
from market_operator.hypothesis import f1_invalid_distance_ok      # noqa: E402
from market_operator.research import (                             # noqa: E402
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
from market_operator.structure import Params                       # noqa: E402

CONFIG_V2 = ROOT / "config" / "v2.json"
REPORTS = ROOT / "reports"


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _f1_pass(events: list, close: pd.Series, k_is: float) -> list:
    """F1 门(v2-protocol §1):事件 bar 收盘至 invalid 的沿方向距离下限。"""
    return [e for e in events
            if f1_invalid_distance_ok(e, float(close[e.known_index]), k_is)]


def run() -> dict:
    raw_cfg = json.loads(CONFIG_V2.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V2)          # 未知键忽略;先验逐项 = defaults
    cost = CostModel.from_config(CONFIG_V2)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    k_is = float(raw_cfg["k_is"])
    k_is_sens = [float(x) for x in raw_cfg.get("k_is_sensitivity_report", [])]
    whitelist = {(str(i), int(d)) for i, d in raw_cfg["whitelist_v2"]}
    event_ids = frozenset({i for i, _ in whitelist})
    assert whitelist, "whitelist_v2 为空"

    all_events, all_ctrls = [], []
    sens_rows: dict = {k: {"ev": [], "ct": []} for k in k_is_sens}
    sym_info: dict = {}
    f1_stats: dict = {}
    for sym in symbols:
        t0 = time.time()
        df = mo_data.load(sym)
        n = len(df)
        boundary = split_boundary(n, dev_frac)
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
        rows = measure_events(df, passed, timeline, cost)
        rows["symbol"] = sym
        rows["uid"] = sym + "#" + rows["uid"].astype(str)
        rows["segment"] = ["dev" if k < boundary else "test"
                           for k in rows["known_index"]]
        ctrl_parts = []
        for seg, (lo, hi) in (("dev", (0, boundary)), ("test", (boundary, n))):
            seg_rows = rows[rows["segment"] == seg]
            ctrl = measure_controls(df, passed, timeline, seg_rows, cost, lo, hi)
            ctrl["segment"] = seg
            ctrl["symbol"] = sym
            ctrl_parts.append(ctrl)
        # F1 敏感性(声明在先、仅报告):同一测量管线换 k_is(含独立对照),不进判定
        for k_alt in k_is_sens:
            alt = _f1_pass(fired, df["close"], k_alt)
            alt_rows = measure_events(df, alt, timeline, cost)
            if len(alt_rows):
                alt_rows["symbol"] = sym
                alt_rows["uid"] = sym + "#" + alt_rows["uid"].astype(str)
                alt_rows["segment"] = ["dev" if k < boundary else "test"
                                       for k in alt_rows["known_index"]]
                alt_ctl = []
                for seg, (lo, hi) in (("dev", (0, boundary)), ("test", (boundary, n))):
                    ac = measure_controls(
                        df, alt, timeline,
                        alt_rows[alt_rows["segment"] == seg], cost, lo, hi)
                    ac["symbol"], ac["segment"] = sym, seg
                    alt_ctl.append(ac)
                sens_rows[k_alt]["ev"].append(alt_rows)
                sens_rows[k_alt]["ct"].append(
                    pd.concat(alt_ctl, ignore_index=True))
        all_events.append(rows)
        all_ctrls.append(pd.concat(ctrl_parts, ignore_index=True))
        sym_info[sym] = {
            "bars": n, "n_events_fired": len(fired), "n_passed_f1": len(passed),
            "dev_end_index": boundary,
            "dev_end_ts": int(df["ts"][boundary]),
            "dev_end_iso": _iso(int(df["ts"][boundary])),
            "elapsed_s": round(time.time() - t0, 1),
        }
        print(f"[{sym}] {n} bars, {len(fired)} E7 fired, {len(passed)} pass F1, "
              f"{sym_info[sym]['elapsed_s']}s")

    ev = pd.concat(all_events, ignore_index=True)
    ct = pd.concat(all_ctrls, ignore_index=True)

    result: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v2-protocol.md(V1 协议 methodology §9 原样)",
            "meta": raw_cfg["meta"], "whitelist_v2": raw_cfg["whitelist_v2"],
            "k_is": k_is, "sensitivity_k_is": k_is_sens,
            "horizons": list(HORIZONS), "primary_horizon": PRIMARY_HORIZON,
            "cost_mults": list(COST_MULTS), "dev_frac": dev_frac,
            "n_seeds": 42, "boot_reps": 2000, "event_excl_bars": 24,
            "min_events_go": MIN_EVENTS_GO,
            "increment_convention": "net = event net - mean(control net); "
                                    "GO/NO-GO criteria 2/3/4 use this increment",
            "cost_model": vars(cost),
            "test_eval_declaration": "test 段第 4 次评估(v2 唯一一次;此后仅 dev;"
                                     "research-log 条目 6)",
        },
        "symbols": sym_info,
        "f1_gate": f1_stats,
        "mechanisms": {},
        "detail_paths": {},
    }

    merged = ev.merge(ct, on=["uid", "symbol", "segment", "known_index",
                              "event_id", "mechanism", "H"], how="left")
    for m in COST_MULTS:
        tag = _mult_tag(m)
        merged[f"incr_atr_{tag}"] = (merged[f"net_atr_{tag}"]
                                     - merged[f"ctrl_net_atr_mean_{tag}"])
        merged[f"incr_pct_{tag}"] = (merged[f"net_pct_{tag}"]
                                     - merged[f"ctrl_net_pct_mean_{tag}"])
    for seg in ("dev", "test"):
        path = REPORTS / f"event_study_events_v2_{seg}.csv"
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

    # -- F1 敏感性(仅报告;同协议独立对照的描述统计,不进判定、不择优) ----------
    on = ["uid", "symbol", "segment", "known_index", "event_id", "mechanism", "H"]
    result["f1_sensitivity"] = {}
    for k_alt, parts in sens_rows.items():
        if not parts["ev"]:
            continue
        aev = pd.concat(parts["ev"], ignore_index=True)
        act = pd.concat(parts["ct"], ignore_index=True)
        am = aev.merge(act, on=on, how="left")
        am = am[am["H"] == PRIMARY_HORIZON]
        am["incr"] = am["net_atr_1_0"] - am["ctrl_net_atr_mean_1_0"]
        for mech, g0 in am.groupby("mechanism"):
            mo = result["f1_sensitivity"].setdefault(str(k_alt), {}).setdefault(
                mech, {})
            for seg in ("dev", "test"):
                g = g0[g0["segment"] == seg]
                inc = g["incr"].dropna()
                mo[seg] = {
                    "n_events": int(g["uid"].nunique()),
                    "event_net_atr_1x_mean": (float(g["net_atr_1_0"].mean())
                                              if len(g) else None),
                    "incr_atr_1x_mean": (float(inc.mean()) if len(inc) else None),
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
        "# v2 事件研究报告(docs/v2-protocol.md:E7 FLIP_RETEST + F1)",
        "",
        f"生成时间(UTC):{result['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        "- 机制:v2-protocol §1 冻结的 E7 FLIP_RETEST(破位→回踩被拒绝→角色互换,"
        "控制权易手确认);E7|short 与 E7|long 对称分开判定,不做方向挑选。",
        f"- F1 失效距离下限(k_is={p['k_is']} 先验):dist(close(t), invalid) ≥ "
        "k_is·atr(t),先验声明在先;对事件集唯一改动即此门。",
        "- 评估协议 = v1 事件研究全管线原样(methodology §9):H ∈ "
        f"{p['horizons']},主视界 H={p['primary_horizon']};对照组同品种/同 regime/"
        "同方向、避同机制事件窗口、42 固定种子;日块 bootstrap 2000 次 95% CI;"
        "成本压力 1x/1.5x/2x;§9.3 四判据,**增量口径**与 v1 完全一致"
        "(增量 = 事件净期望 − 对照净期望均值)。",
        f"- **重评声明(§9.4 / v2-protocol §3-2)**:{p['test_eval_declaration']}。"
        "本报告为该次评估的产物。",
        f"- 成本模型与 v1 同源(config/v2.json → {p['cost_model']})。",
        "- E7 发射数与过 F1 数(dev/test 分界每品种前 70%):",
    ]
    for sym, info in result["symbols"].items():
        st = result["f1_gate"][sym]
        lines.append(
            f"  - {sym}:dev_end = {info['dev_end_iso']} "
            f"(index {info['dev_end_index']} / {info['bars']});E7 fired "
            f"{info['n_events_fired']}(dev {st['dev']['fired']} / test "
            f"{st['test']['fired']}),过 F1 {info['n_passed_f1']}"
            f"(dev {st['dev']['passed_f1']} / test {st['test']['passed_f1']})")

    mechs = result["mechanisms"]
    lines += ["", "## 1. GO / NO-GO 判定表(test 段,H=" + ph + ",判据同 §9.3)", "",
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
              "| 机制 | n | 事件净@1x (CI95) | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量%@1.5x |",
              "|---|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        h = mechs[mech]["test"]["horizons"].get(ph)
        if not h:
            lines.append(f"| {mech} | 0 | — | — | — | — | — |")
            continue
        c1, c15 = h["cost"]["1_0"], h["cost"]["1_5"]
        lines.append(
            f"| {mech} | {h['n']} | {_f(c1['event_net_atr_mean'])} "
            f"[{_f(c1['event_net_atr_ci95'][0])}, {_f(c1['event_net_atr_ci95'][1])}] "
            f"| {_f(c1['ctrl_net_atr_mean'])} "
            f"| {_f(c1['incr_atr_mean'])} [{_f(c1['incr_atr_ci95'][0])}, {_f(c1['incr_atr_ci95'][1])}] "
            f"| {_f(c15['incr_atr_mean'])} | {_f(c15['incr_pct_mean'], 5)} |")

    lines += ["", "## 3. 成本压力(test 段,H=" + ph + ",增量 ATR)", "",
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

    lines += ["", "## 8. F1 敏感性(k_is ∈ 声明值,仅报告,不择优、无判定;",
              "对照组同协议独立重采)", "",
              "| k_is | 机制 | 段 | n(H=24) | 事件净@1x | 增量@1x(独立对照) |",
              "|---|---|---|---|---|---|"]
    for k_alt in sorted(result["f1_sensitivity"], key=float):
        s = result["f1_sensitivity"][k_alt]
        for mech in sorted(s):
            for seg in ("dev", "test"):
                g = s[mech].get(seg)
                if not g or not g["n_events"]:
                    continue
                lines.append(f"| {k_alt} | {mech} | {seg} | {g['n_events']} "
                             f"| {_f(g['event_net_atr_1x_mean'])} "
                             f"| {_f(g['incr_atr_1x_mean'])} |")

    lines += ["", f"逐事件明细:`{result['detail_paths']['dev']}`, "
                  f"`{result['detail_paths']['test']}`;全量指标:`reports/event_study_v2.json`。", ""]
    return "\n".join(lines)


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    result = run()
    (REPORTS / "event_study_v2.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "event_study_v2.md").write_text(render_md(result), encoding="utf-8")
    print("-> reports/event_study_v2.json, reports/event_study_v2.md, "
          "reports/event_study_events_v2_{dev,test}.csv")


if __name__ == "__main__":
    main()
