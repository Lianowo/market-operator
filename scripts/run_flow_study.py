"""v12 判别主实验管线(docs/v12-protocol.md,冻结于任何 v12 运行之前,2026-10-20)。

用法:  python scripts/run_flow_study.py [--render-only]
产物:  reports/flow_study.json                   机器可读全量(桶 × 段 × 分位)
       reports/flow_study.md                     人读报告(主判别表 + H1/H0 判别链)
       reports/flow_study_events_{dev,test}.csv  逐事件明细(含 delta_z 分位标签)

纪律(v12-protocol §1-§4,冻结):
- 数据 = data/raw_binance/{SYM}_1h.csv(v12 §1;venue=Binance USDⓈ-M,窗差
  如实声明,见符号信息);Bitget 数据/历史产物/E 检测零改动。
- 事件 = canonical 语义重放:Params.from_json(config/canonical.json)
  (merge_max_atr=2.0 账面修复语义,与生产认知同座;先验逐项 = canonical),
  E1/E2/E3/E6/E7(E7 = enable_e7,v2 语义;不施 rehearse-whitelist,
  白名单已在 canonical 废弃,research-log #21)。
- 流特征 = flow_features(delta_vol/share/cvd_24/cvd_48/delta_z,全因果,
  tok=收盘知时),切片特征 = 触发 bar(known_index)的 delta_z。
- 判别(v12 §3 H1):桶 = 事件×方向;按 known_index 的 delta_z 三瓜分;
  最高/最低分位"net@1x H24 相对盘中对照的增量"之差,日块 block
  bootstrap 2000 次固定种子 95% CI;CI 不含 0 ⇒ 该桶 H1 成立。
- 测量层 = methodology §9 原样(research.collect_events / measure_events /
  measure_controls / _net_row 不变调用):入场 = 次 1h 开盘市价(taker);
  H ∈ {6,12,24,48},主视界 H=24;对照 = 同品种/同 regime/同方向、避同机制
  事件窗口 24 根、42 固定种子(SEED_BASE+i);成本 1x/1.5x/2x 全测,判别用
  net@1x(ATR 主口径,% 并列)。
- dev/test:每品种前 dev_frac(config/canonical.json,0.7)为 dev,其余 test
  (币安窗内同规则,即 split_boundary;"拐点对应法则" = 同一分位法则,
  窗差使分界 ts 与 Bitget 帧不同,如实声明)。dev 主评(发现语境),
  test 一次(第 15 次曝光;research-log #15/#24 预注册;此后只许前向)。
  判别链:v12 §4 —— test 段上任一桶 CI 不含 0 ⇒ H1(bar 层流特征有增量,
  转 v13 "CVD/delta 语义路由" 设计);全桶 CI 含 0 ⇒ H0,负结果关闭 bar
  层流路线,转 B 线前向 tape 积累。多重性声明:10 桶(5 事件 × 双向)
  家族-wise 未校正,桶级与全局判定均如实标注。
- 桶 n < MIN_TERCILE_N(6) ⇒ 切片病态,INFERENCE=INSUFFICIENT_DATA(不叙述性补救)。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data                      # noqa: E402
from market_operator.events import E1, E2, E3, E6, E7           # noqa: E402
from market_operator.flow_features import attach as attach_flow  # noqa: E402
from market_operator.flow_study import (                         # noqa: E402
    MIN_TERCILE_N,
    tercile_diff_ci,
    tercile_labels,
)
from market_operator.research import (                            # noqa: E402
    BOOT_REPS,
    COST_MULTS,
    HORIZONS,
    MIN_EVENTS_GO,
    PRIMARY_HORIZON,
    CostModel,
    collect_events,
    measure_controls,
    measure_events,
    split_boundary,
    _mult_tag,
)
from market_operator.structure import Params                      # noqa: E402

CONFIG_CANONICAL = ROOT / "config" / "canonical.json"
REPORTS = ROOT / "reports"
DATA_BINANCE = ROOT / "data" / "raw_binance"

STUDY_IDS = frozenset({E1, E2, E3, E6, E7})
MECHS = tuple(f"{e}|{d}" for e in (E1, E2, E3, E6, E7) for d in ("long", "short"))
TERCILE_NAMES = {0: "bottom", 1: "middle", 2: "top"}


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def load_binance(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_BINANCE / f"{symbol}_1h.csv", dtype={"ts": "int64"})
    df = (df.drop_duplicates(subset="ts").sort_values("ts")
          .reset_index(drop=True))
    return df


def run() -> dict:
    raw_cfg = json.loads(CONFIG_CANONICAL.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_CANONICAL)
    assert params.merge_max_atr == 2.0, \
        "v12 认知重放必须同生产语义 merge_max_atr=2.0(config/canonical.json)"
    cost = CostModel.from_config(CONFIG_CANONICAL)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])

    manifest = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))

    all_events, all_ctrls = [], []
    sym_info: dict = {}
    for sym in symbols:
        t0 = time.time()
        df = attach_flow(load_binance(sym))
        n = len(df)
        boundary = split_boundary(n, dev_frac)
        fired, timeline = collect_events(df, params, event_ids=STUDY_IDS,
                                         enable_e7=True)
        rows = measure_events(df, fired, timeline, cost)
        sym_info[sym] = {
            "bars": n,
            "ts_first": int(df["ts"].iloc[0]), "ts_last": int(df["ts"].iloc[-1]),
            "span": f"{_iso(int(df['ts'].iloc[0]))} .. {_iso(int(df['ts'].iloc[-1]))}",
            "manifest_sha256": manifest["files"].get(f"binance_vision/{sym}_1h.csv", {}).get("sha256"),
            "dev_end_index": boundary,
            "dev_end_iso": _iso(int(df["ts"][boundary])),
            "fired_total": len(fired),
            "n_dropped_unmeasured": len(fired) - (rows["uid"].nunique() if len(rows) else 0),
            "per_event_fired": {e: sum(1 for x in fired if x.id == e)
                                for e in sorted(STUDY_IDS)},
            "elapsed_s": round(time.time() - t0, 1),
        }
        if len(rows) == 0:
            print(f"[{sym}] {n} bars, no measurable events", flush=True)
            continue
        # 逐事件挂 delta_z(uid 在 measure_events 内按事件序编号)
        dz = df["delta_z"].to_numpy(np.float64)
        rows = rows.copy()
        rows["symbol"] = sym
        rows["uid"] = sym + "#" + rows["uid"].astype(str)
        rows["segment"] = ["dev" if k < boundary else "test"
                           for k in rows["known_index"]]
        rows["delta_z"] = [float(dz[k]) for k in rows["known_index"]]
        ctrl_parts = []
        for seg, (lo, hi) in (("dev", (0, boundary)), ("test", (boundary, n))):
            seg_rows = rows[rows["segment"] == seg]
            if len(seg_rows) == 0:
                continue
            ctrl = measure_controls(df, fired, timeline, seg_rows, cost, lo, hi)
            ctrl["segment"] = seg
            ctrl["symbol"] = sym
            ctrl_parts.append(ctrl)
        all_events.append(rows)
        if ctrl_parts:
            all_ctrls.append(pd.concat(ctrl_parts, ignore_index=True))
        print(f"[{sym}] {n} bars, fired={sym_info[sym]['fired_total']} "
              f"{sym_info[sym]['per_event_fired']}, {sym_info[sym]['elapsed_s']}s",
              flush=True)

    ev = (pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame())
    ct = (pd.concat(all_ctrls, ignore_index=True) if all_ctrls else pd.DataFrame())

    detail_paths: dict = {}
    if len(ev) and len(ct):
        merged = ev.merge(ct, on=["uid", "symbol", "segment", "known_index",
                                  "event_id", "mechanism", "H"], how="left",
                          suffixes=("", "_ctrl"))
        for m in COST_MULTS:
            tag = _mult_tag(m)
            merged[f"incr_atr_{tag}"] = (merged[f"net_atr_{tag}"]
                                         - merged[f"ctrl_net_atr_mean_{tag}"])
            merged[f"incr_pct_{tag}"] = (merged[f"net_pct_{tag}"]
                                         - merged[f"ctrl_net_pct_mean_{tag}"])
        # 三瓜分标签:桶 = 机制 × 段;主视界 H=24 行才切片(判别只在 H=24)
        merged["tercile"] = -1
        h24 = merged["H"] == PRIMARY_HORIZON
        for (mech, seg), g in merged[h24].groupby(["mechanism", "segment"]):
            lab = tercile_labels(g["delta_z"].to_numpy(), g["ts"].to_numpy())
            merged.loc[g.index, "tercile"] = lab
        for seg in ("dev", "test"):
            path = REPORTS / f"flow_study_events_{seg}.csv"
            merged[merged["segment"] == seg].to_csv(path, index=False)
            detail_paths[seg] = str(path.relative_to(ROOT))
    else:
        merged = pd.DataFrame()

    # ------------------------------------------------------------------
    # 判别:桶 × 段 × (top vs bottom)
    # ------------------------------------------------------------------
    result: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v12-protocol.md(评估测量层 = methodology §9 原样;"
                      "机制 = canonical E1/E2/E3/E6/E7 在币安 1h 重放)",
            "venue_declaration": "Binance USDⓈ-M(data.binance.vision);与 Bitget "
                                 "非同构,检验'bar 层流特征值不值钱',不复刻 Bitget",
            "params_source": "config/canonical.json(merge_max_atr=2.0 断言通过;"
                             "先验逐项同生产语义)",
            "timeframe": "1h(认知帧;入场 = 次 1h 开盘市价)",
            "horizons": list(HORIZONS), "primary_horizon": PRIMARY_HORIZON,
            "cost_mults": list(COST_MULTS),
            "slice_feature": "delta_z @ known_index(flow_features, 全因果)",
            "slice_rule": "三瓜分 (delta_z 升序, ts 平局裁决), 桶 = 机制 × 段; "
                          f"min_tercile_n = {MIN_TERCILE_N}",
            "discriminant": "mean(incr@1x,H24 | top) − mean(incr@1x,H24 | bottom),"
                            " incr = event net − ctrl mean(42 seeds 同池)",
            "bootstrap": f"两组独立日块 block bootstrap ×{BOOT_REPS}, 固定种子,"
                         " 95% percentile CI",
            "increment_convention": "net = event net - mean(control net);同 v1-v9",
            "dev_frac": dev_frac,
            "split_rule": "每品种币安窗内前 dev_frac 为 dev(split_boundary);"
                          "同 Bitget 帧的'前 70%'法则,窗差使分界 ts 不同,如实声明",
            "test_eval_declaration": "test 段第 15 次曝光(v12 唯一一次;"
                                     "research-log #15/#24;此后只许前向)",
            "multiplicity_declaration": "10 桶(5 事件 × 双向)家族-wise 未校正;"
                                        "桶级判定即报告级,整体 H1/H0 按 §4 判别链",
            "cost_model": vars(cost),
        },
        "symbols": sym_info,
        "buckets": {},
        "detail_paths": detail_paths,
    }

    verdicts_test: list[bool] = []     # True = CI 不含 0(H1 桶)
    for mech in MECHS:
        out: dict = {}
        for seg in ("dev", "test"):
            g = merged[(merged["mechanism"] == mech) & (merged["segment"] == seg)
                       & (merged["H"] == PRIMARY_HORIZON)] if len(merged) else merged
            n = int(len(g))
            if n < MIN_TERCILE_N:
                out[seg] = {"n": n, "inference": "INSUFFICIENT_DATA",
                            "reason": f"n < MIN_TERCILE_N({MIN_TERCILE_N})"}
                continue
            valid = g.dropna(subset=["incr_atr_1_0"])
            top = valid[valid["tercile"] == 2]
            bot = valid[valid["tercile"] == 0]
            if len(top) == 0 or len(bot) == 0:   # 对照池全空等病态
                out[seg] = {"n": n, "inference": "INSUFFICIENT_DATA",
                            "reason": "tercile degenerate(对照池空:同机制排除窗在"
                                      "高事件密度下覆盖全段,增量未定义;v9 E3 同病理)"}
                continue
            diff, ci_lo, ci_hi = tercile_diff_ci(
                top["incr_atr_1_0"].to_numpy(), top["ts"].to_numpy(),
                bot["incr_atr_1_0"].to_numpy(), bot["ts"].to_numpy())
            diff_pct = float(top["incr_pct_1_0"].mean() - bot["incr_pct_1_0"].mean())
            cell = {
                "n": n,
                "n_valid_incr": int(len(valid)),
                "tercile_n": {TERCILE_NAMES[t]: int((valid["tercile"] == t).sum())
                              for t in (0, 1, 2)},
                "top_incr_atr_mean": float(top["incr_atr_1_0"].mean()),
                "bottom_incr_atr_mean": float(bot["incr_atr_1_0"].mean()),
                "middle_incr_atr_mean": float(
                    valid.loc[valid["tercile"] == 1, "incr_atr_1_0"].mean()),
                "diff_top_minus_bottom_atr": diff,
                "diff_atr_ci95": [ci_lo, ci_hi],
                "diff_top_minus_bottom_pct": diff_pct,
                "top_delta_z_range": [float(top["delta_z"].min()),
                                      float(top["delta_z"].max())],
                "bottom_delta_z_range": [float(bot["delta_z"].min()),
                                         float(bot["delta_z"].max())],
                "h1_bucket_ci_excludes_zero": bool(ci_lo > 0.0 or ci_hi < 0.0),
            }
            out[seg] = cell
            if seg == "test":
                verdicts_test.append(cell["h1_bucket_ci_excludes_zero"])
        result["buckets"][mech] = out
        dt = out.get("test", {})
        if "diff_top_minus_bottom_atr" in dt:
            print(f"  {mech:9s} test n={dt['n']:4d}  "
                  f"diff={dt['diff_top_minus_bottom_atr']:+.4f} "
                  f"CI[{dt['diff_atr_ci95'][0]:+.4f},{dt['diff_atr_ci95'][1]:+.4f}]"
                  f"  {'H1' if dt['h1_bucket_ci_excludes_zero'] else '—'}", flush=True)
        else:
            print(f"  {mech:9s} test n={dt.get('n', 0)}  {dt.get('inference', '')}",
                  flush=True)

    h1 = any(verdicts_test)
    result["h1_verdict"] = {
        "test_buckets_ci_excluding_zero": int(sum(verdicts_test)),
        "test_buckets_evaluated": len(verdicts_test),
        "verdict": ("H1_supported" if h1 else "H0_stands"),
        "chain": ("存在 test 桶 CI 不含 0 ⇒ bar 层流特征有增量(v12 §3;转 v13 "
                  "CVD/delta 语义路由设计)" if h1 else
                  "全部 test 桶 CI 含 0 ⇒ 切不出差异,H0:bar 层流不解决拥挤;"
                  "按 v12 §4 关闭流路线(bar 层部分),转 B 线前向 tape 积累"),
    }
    print(f"  >>> H1 verdict: {result['h1_verdict']['verdict']} "
          f"({sum(verdicts_test)}/{len(verdicts_test)} test buckets)", flush=True)
    return result


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=4):
    if x is None:
        return "—"
    if isinstance(x, float):
        if x != x:
            return "NaN"
        return f"{x:.{nd}f}"
    return str(x)


def render_md(result: dict) -> str:
    p = result["protocol"]
    b = result["buckets"]
    lines = [
        "# v12 K线级订单流(delta)判别报告(docs/v12-protocol.md)",
        "",
        f"生成时间(UTC):{result['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        "- 检验对象:1h bar 主动买卖差(v12 §1 '可回放的轻量订单流')对 canonical "
        "事件后期望的增量。**venue 声明**:Binance USDⓈ-M 永续(data.binance.vision) "
        "≠ Bitget microstructure;本轮只回答'bar 层流特征值不值钱'。",
        f"- 事件 = canonical 语义重放({p['params_source']}),E1/E2/E3/E6/E7;",
        f"  测量层 = methodology §9 原样:{p['discriminant']}。",
        f"- 切片 = {p['slice_feature']},{p['slice_rule']};"
        f"  CI = {p['bootstrap']}。",
        f"- **test 段声明**:{p['test_eval_declaration']}。本报告为该次曝光的产物。",
        f"- 多重性:{p['multiplicity_declaration']}。",
        f"- 窗差:见 §2(每品种 dev 分界 ts 与 Bitget 帧不同,同窗法则如实声明)。",
        "",
        "## 1. H1 主判别表(桶 = 事件×方向;diff = top 分位增量 − bottom 分位增量,"
        "net@1x H=24 ATR 单位)",
        "",
    ]
    for seg in ("test", "dev"):
        lines += [f"### {seg} 段", "",
                  "| 机制×方向 | n | top n | bottom n | top 增量 | bottom 增量 "
                  "| **diff** | CI95 | CI不含0? |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for mech in MECHS:
            cell = b.get(mech, {}).get(seg, {})
            if "diff_top_minus_bottom_atr" not in cell:
                lines.append(f"| {mech} | {cell.get('n', 0)} | — | — | — | — "
                             f"| — | — | {cell.get('inference', '—')} |")
                continue
            mark = "**✓ H1**" if cell["h1_bucket_ci_excludes_zero"] else "✗"
            lines.append(
                f"| {mech} | {cell['n']} | {cell['tercile_n']['top']} "
                f"| {cell['tercile_n']['bottom']} "
                f"| {_f(cell['top_incr_atr_mean'])} | {_f(cell['bottom_incr_atr_mean'])} "
                f"| **{_f(cell['diff_top_minus_bottom_atr'])}** "
                f"[{_f(cell['diff_atr_ci95'][0])}, {_f(cell['diff_atr_ci95'][1])}] "
                f"| {mark} |")
        lines.append("")
    v = result["h1_verdict"]
    lines += [
        f"### 判别链(v12-protocol §4)",
        "",
        f"test 段 {v['test_buckets_evaluated']} 桶中 CI 不含 0 的桶数 = "
        f"**{v['test_buckets_ci_excluding_zero']}** ⇒ **{v['verdict']}**:{v['chain']}。",
        "",
        "## 2. 数据窗与事件计数",
        "",
    ]
    for sym, info in result["symbols"].items():
        lines.append(
            f"- {sym}:{info['bars']} bars [{info['span']}](sha256 "
            f"`{(info['manifest_sha256'] or 'n/a')[:16]}…`);dev_end = "
            f"{info['dev_end_iso']} (index {info['dev_end_index']});"
            f"fired={info['fired_total']} {info['per_event_fired']},"
            f"未测(尾部前向窗不足/病态)丢弃 {info['n_dropped_unmeasured']}")
    if result["detail_paths"]:
        lines += ["", "## 3. 产物索引", ""]
        for seg in ("dev", "test"):
            lines.append(f"- 逐事件 {seg}(含 delta_z 分位标签):"
                         f"`{result['detail_paths'][seg]}`")
    lines += ["", "机器可读全量:`reports/flow_study.json`(桶 × 段 × 分位,"
                  "含 CI 与分位增量均值)。", ""]
    return "\n".join(lines)


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    if "--render-only" in sys.argv:
        result = json.loads((REPORTS / "flow_study.json").read_text(encoding="utf-8"))
        (REPORTS / "flow_study.md").write_text(render_md(result), encoding="utf-8")
        print("-> reports/flow_study.md (render-only)")
        return
    result = run()
    (REPORTS / "flow_study.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "flow_study.md").write_text(render_md(result), encoding="utf-8")
    print("-> reports/flow_study.json, reports/flow_study.md, "
          "reports/flow_study_events_{dev,test}.csv")


if __name__ == "__main__":
    main()
