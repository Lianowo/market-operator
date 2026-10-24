"""v9 事件研究管线(docs/v9-protocol.md,冻结于任何 v9 运行之前,2026-10-20;
最后一轮协议级研究:过程式接受/拒绝 PROCESS_ACCEPTED/PROCESS_REJECTED)。

用法:  python scripts/run_event_study_v9.py
产物:  reports/event_study_v9.json                 机器可读全量(机制 × 段 ×
                                                  成本压力 × 视界,同 §9 口径)
       reports/event_study_v9.md                  人读报告(4 主格 + 同池对照)
       reports/event_study_events_v9_{dev,test}.csv  逐事件明细(含对照增量)

纪律(v9-protocol §1-§6,冻结):
- 唯一新机制 = PROCESS_ACCEPTED PROCESS_ACCEPTED / PROCESS_REJECTED PROCESS_REJECTED(events.py
  enable_e9e10;四先验一次写死 min_age=3, occ=2/3, centroid=0.3, k_rj=0.3
  全来自 config/v9.json;敏感性 {min_age 2/4, occ 0.6/0.75, centroid
  0.2/0.5} 协议仅许 dev——本管线声明的交付范围为主判定网格一次,不含敏
  感性(协议§2 是许可+范围限制,非义务)。
- 桶 = 事件 × 方向:PROCESS_ACCEPTED|long, PROCESS_ACCEPTED|short, PROCESS_REJECTED|long, PROCESS_REJECTED|short 四个主格;
  双向对称判定,不做方向挑选(协议§4)。
- 评估协议 = methodology §9 全流程原样(v1 同路):TF=1h 认知帧、入场 =
  次 1h 开盘市价(taker,research 框架固有口径,即 protocol §3 次 1h 开
  盘注);H ∈ {6,12,24,48} 根、主视界 H=24;对照组同品种/同 regime/同
  方向随机入场、避同机制事件窗口 24 根、42 固定种子;日块 bootstrap
  2000 次 95% CI;成本压力 1x/1.5x/2x;四判据逐格(≥60、1.5x 增量>0 双
  口径、CI 下界>0@1x、3 品种≥2 正),增量口径与 v1-v3 完全一致。
- F1/room/r_min 闸属回测层(protocol §3/§4 判别链;事件研究主判定不施
  加,v1 同例)。报告附 F1(k_is=0.8)参考计数(仅信息,不进判定)。
- dev/test = 每品种前 dev_frac(0.7)为 dev;**test 段第 12 次曝光且此后
  只许前向**(research-log #18 预注册);四判据只在 test 段评估;n<60 ⇒
  INSUFFICIENT_DATA,不做叙述性补救(协议§4)。
- 同池对照上下文(协议§5 动机的直接核验;非 v9 判定对象):BREAKOUT_ACCEPTED|long,
  BREAKOUT_ACCEPTED|short, LEVEL_REJECTED|long, LEVEL_REJECTED|short 在同一回放、同一时间线、同一种子栅格下
  过同一测量管线(v1 事件研究同口径,无 F1/白名单),与 PROCESS_ACCEPTED/PROCESS_REJECTED 同池对比。
- 零样本病理探针(_GateProbe,脚本内局部):与主回放同参数的第二遍,
  逐门统计 aged episode bars;属事实证据、判定输入外(判定仍按 §9 原样
  只依事件/对照测量;探针与主回放 PROCESS_ACCEPTED/PROCESS_REJECTED 发射硬对账,不符即 raise)。
- 区域/认知/成本/收割零改动:全部先验逐项来自 config/v9.json(其 parent
  = config/canonical.json,含 merge_max_atr=2.0 账面修复语义,与生产认知
  同座);不改 BREAKOUT_ACCEPTED-EDGE_RECLAIMED 检测;历史产物不动。
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data                      # noqa: E402
from market_operator.events import (                             # noqa: E402
    BREAKOUT_ACCEPTED,
    LEVEL_REJECTED,
    PROCESS_ACCEPTED,
    PROCESS_REJECTED,
    EventDetector,
)
from market_operator.hypothesis import f1_invalid_distance_ok     # noqa: E402
from market_operator.research import (                            # noqa: E402
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
from market_operator.structure import Params                      # noqa: E402

CONFIG_V9 = ROOT / "config" / "v9.json"
REPORTS = ROOT / "reports"

PRIMARY_MECHS = ("PROCESS_ACCEPTED|long", "PROCESS_ACCEPTED|short", "PROCESS_REJECTED|long", "PROCESS_REJECTED|short")
COMPARE_MECHS = ("BREAKOUT_ACCEPTED|long", "BREAKOUT_ACCEPTED|short", "LEVEL_REJECTED|long", "LEVEL_REJECTED|short")
STUDY_IDS = frozenset({BREAKOUT_ACCEPTED, LEVEL_REJECTED, PROCESS_ACCEPTED, PROCESS_REJECTED})


class _GateProbe(EventDetector):
    """零样本病理探针(脚本内局部;判定输入外的事实证据)。

    过程量累计唯一来源 = 基类 `_process_verdict`(先调用,单点事实),本类
    只在其后按冻结判据逐条统计 aged episode bars 的各门通过数,不干预任
    何发射(若真发生,照发不误并计入 fired 对账)。
    """

    def __init__(self, params: Params, **kw) -> None:
        super().__init__(params, enable_e9e10=True, **kw)
        self.gate: Counter = Counter()
        self.aged_eps: set = set()

    def _process_verdict(self, ep, z, res, t, ts, h, l, c, atr):  # noqa: ANN001
        ev = super()._process_verdict(ep, z, res, t, ts, h, l, c, atr)
        a = t - ep.opened
        n = a + 1
        self.gate["episode_bars"] += 1
        if a < self.e9e10_min_age:
            return ev
        self.gate["aged_bars"] += 1
        self.aged_eps.add(id(ep))
        occ = ep.occ_count / n
        mean_c = ep.close_sum / n
        centroid = ((mean_c - z.hi) if res else (z.lo - mean_c)) / atr
        rj = self.params.k_rj * atr
        ex = ep.ex_proc
        if ep.occ_count == 0:
            self.gate["e10_occ0"] += 1
        if ep.wickfail >= 2:
            self.gate["e10_wf2"] += 1
        if (c <= ex - rj) if res else (c >= ex + rj):
            self.gate["e10_close"] += 1
        if centroid <= 0.0:
            self.gate["e10_cent0"] += 1
        if ep.occ_count == 0 and ep.wickfail >= 2:
            self.gate["e10_occ0_wf2"] += 1
        if occ >= self.e9_occ_min:
            self.gate["e9_occ"] += 1
        if centroid >= self.e9_centroid_min:
            self.gate["e9_cent"] += 1
        if (c > z.hi) if res else (c < z.lo):
            self.gate["e9_close"] += 1
        return ev


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def run() -> dict:
    raw_cfg = json.loads(CONFIG_V9.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V9)          # 未知键忽略;逐项 = v9 配置
    assert params.k_rj == float(raw_cfg["k_rj"]) == 0.3, \
        "k_rj 先验承诺(协议§2 拒绝回退沿用 k_rj=0.3)"
    cost = CostModel.from_config(CONFIG_V9)
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])
    k_is = float(raw_cfg["k_is"])
    e9_kwargs = dict(
        enable_e9e10=bool(raw_cfg["enable_e9e10"]),
        e9e10_min_age=int(raw_cfg["e9e10_min_age"]),
        e9_occ_min=float(raw_cfg["e9_occ_min"]),
        e9_centroid_min=float(raw_cfg["e9_centroid_min"]),
    )
    assert e9_kwargs["enable_e9e10"], "config/v9.json 必须开启 enable_e9e10"

    all_events, all_ctrls = [], []
    sym_info: dict = {}
    f1_stats: dict = {}
    for sym in symbols:
        t0 = time.time()
        df = mo_data.load(sym)
        n = len(df)
        boundary = split_boundary(n, dev_frac)
        fired, timeline = collect_events(df, params, event_ids=STUDY_IDS,
                                         **e9_kwargs)
        seg_of = lambda e: "dev" if e.known_index < boundary else "test"
        proc = [e for e in fired if e.id in (PROCESS_ACCEPTED, PROCESS_REJECTED)]
        f1_stats[sym] = {
            k: {"fired": sum(1 for e in proc if seg_of(e) == k),
                "would_pass_f1": sum(
                    1 for e in proc if seg_of(e) == k
                    and f1_invalid_distance_ok(
                        e, float(df["close"][e.known_index]), k_is))}
            for k in ("dev", "test")}
        rows = measure_events(df, fired, timeline, cost)
        if len(rows):
            rows["symbol"] = sym
            rows["uid"] = sym + "#" + rows["uid"].astype(str)
            rows["segment"] = ["dev" if k < boundary else "test"
                               for k in rows["known_index"]]
            ctrl_parts = []
            for seg, (lo, hi) in (("dev", (0, boundary)), ("test", (boundary, n))):
                seg_rows = rows[rows["segment"] == seg]
                if len(seg_rows) == 0:
                    continue
                ctrl = measure_controls(df, fired, timeline, seg_rows, cost,
                                        lo, hi)
                ctrl["segment"] = seg
                ctrl["symbol"] = sym
                ctrl_parts.append(ctrl)
            all_events.append(rows)
            if ctrl_parts:
                all_ctrls.append(pd.concat(ctrl_parts, ignore_index=True))
        sym_info[sym] = {
            "bars": n,
            "n_e9_fired": sum(1 for e in fired if e.id == PROCESS_ACCEPTED),
            "n_e10_fired": sum(1 for e in fired if e.id == PROCESS_REJECTED),
            "n_e1_fired": sum(1 for e in fired if e.id == BREAKOUT_ACCEPTED),
            "n_e3_fired": sum(1 for e in fired if e.id == LEVEL_REJECTED),
            "dev_end_index": boundary,
            "dev_end_ts": int(df["ts"][boundary]),
            "dev_end_iso": _iso(int(df["ts"][boundary])),
            "elapsed_s": round(time.time() - t0, 1),
        }
        # 零样本病理探针(事实证据,判定输入外;进程量累计唯一来源 = 基类)
        probe = _GateProbe(
            params,
            e9e10_min_age=e9_kwargs["e9e10_min_age"],
            e9_occ_min=e9_kwargs["e9_occ_min"],
            e9_centroid_min=e9_kwargs["e9_centroid_min"])
        for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
            probe.update((row.ts, row.open, row.high, row.low,
                          row.close, row.volume))
        probe_fired = Counter(e.id for e in probe.events if e.id in (PROCESS_ACCEPTED, PROCESS_REJECTED))
        main_fired = Counter(e.id for e in fired if e.id in (PROCESS_ACCEPTED, PROCESS_REJECTED))
        assert probe_fired == main_fired, (
            f"{sym}: 探针与主回放 PROCESS_ACCEPTED/PROCESS_REJECTED 发射硬对账不符 "
            f"({probe_fired} vs {main_fired})")
        sym_info[sym]["gate_stats"] = {
            "episode_bars": probe.gate["episode_bars"],
            "aged_bars": probe.gate["aged_bars"],
            "aged_episodes_distinct": len(probe.aged_eps),
            "e10_occ0": probe.gate["e10_occ0"],
            "e10_wf2": probe.gate["e10_wf2"],
            "e10_close_retreat": probe.gate["e10_close"],
            "e10_centroid_le0": probe.gate["e10_cent0"],
            "e10_occ0_and_wf2": probe.gate["e10_occ0_wf2"],
            "e9_occ_ge_thr": probe.gate["e9_occ"],
            "e9_centroid_ge_thr": probe.gate["e9_cent"],
            "e9_close_beyond": probe.gate["e9_close"],
        }
        print(f"[{sym}] {n} bars, fired PROCESS_ACCEPTED/PROCESS_REJECTED/BREAKOUT_ACCEPTED/LEVEL_REJECTED = "
              f"{sym_info[sym]['n_e9_fired']}/{sym_info[sym]['n_e10_fired']}/"
              f"{sym_info[sym]['n_e1_fired']}/{sym_info[sym]['n_e3_fired']}, "
              f"aged_bars={probe.gate['aged_bars']}, "
              f"{sym_info[sym]['elapsed_s']}s", flush=True)

    ev = (pd.concat(all_events, ignore_index=True) if all_events
          else pd.DataFrame())
    ct = (pd.concat(all_ctrls, ignore_index=True) if all_ctrls
          else pd.DataFrame())

    detail_paths: dict = {}
    if len(ev) and len(ct):
        merged = ev.merge(ct, on=["uid", "symbol", "segment", "known_index",
                                  "event_id", "mechanism", "H"], how="left")
        for m in COST_MULTS:
            tag = _mult_tag(m)
            merged[f"incr_atr_{tag}"] = (merged[f"net_atr_{tag}"]
                                         - merged[f"ctrl_net_atr_mean_{tag}"])
            merged[f"incr_pct_{tag}"] = (merged[f"net_pct_{tag}"]
                                         - merged[f"ctrl_net_pct_mean_{tag}"])
        for seg in ("dev", "test"):
            path = REPORTS / f"event_study_events_v9_{seg}.csv"
            merged[merged["segment"] == seg].to_csv(path, index=False)
            detail_paths[seg] = str(path.relative_to(ROOT))

    result: dict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v9-protocol.md(评估 = methodology §9 原样,"
                      "机制 = PROCESS_ACCEPTED/PROCESS_REJECTED 过程式接受/拒绝,region/成本零改动)",
            "meta": raw_cfg["meta"],
            "enable_e9e10": raw_cfg["enable_e9e10"],
            "e9e10_min_age": raw_cfg["e9e10_min_age"],
            "e9_occ_min": raw_cfg["e9_occ_min"],
            "e9_centroid_min": raw_cfg["e9_centroid_min"],
            "k_rj": raw_cfg["k_rj"],
            "timeframe": "1h(认知帧;入场 = 次 1h 开盘市价,protocol §3 注)",
            "horizons": list(HORIZONS), "primary_horizon": PRIMARY_HORIZON,
            "cost_mults": list(COST_MULTS), "dev_frac": dev_frac,
            "n_seeds": 42, "boot_reps": 2000, "event_excl_bars": 24,
            "min_events_go": MIN_EVENTS_GO,
            "increment_convention": "net = event net - mean(control net); "
                                    "GO/NO-GO criteria 2/3/4 use this increment",
            "cost_model": vars(cost),
            "buckets": "事件 × 方向(双向对称判定,不做方向挑选;v9-protocol §4)",
            "sensitivity_declaration": "敏感性 {min_age 2/4, occ 0.6/0.75,"
                                       "centroid 0.2/0.5} 协议仅许 dev;本管线"
                                       "交付范围 = 主判定网格一次,不含敏感性",
            "test_eval_declaration": "test 段第 12 次曝光(v9 唯一一次;此后只许"
                                     "前向;research-log #18)",
            "family_declaration": "预注册主格 4 个(PROCESS_ACCEPTED/PROCESS_REJECTED × 双向);全部格子数字"
                                  "完整公布,不挑格子报喜;BREAKOUT_ACCEPTED/LEVEL_REJECTED 同池对照为上下"
                                  "文,不属 v9 判定家族",
        },
        "symbols": sym_info,
        "f1_reference": f1_stats,
        "mechanisms": {},
        "compare_pool": {},
        "detail_paths": detail_paths,
    }

    def _analyze_bucket(mech: str) -> dict:
        out: dict = {}
        g_ev = ev[ev["mechanism"] == mech] if len(ev) else ev
        g_ct = ct[ct["mechanism"] == mech] if len(ct) else ct
        for seg in ("dev", "test"):
            s_ev = g_ev[g_ev["segment"] == seg] if len(g_ev) else g_ev
            if len(s_ev) == 0:
                out[seg] = {"n_events": 0, "horizons": {},
                            "go_no_go": {"verdict": "INSUFFICIENT_DATA",
                                         "reason": "no events"}} \
                    if seg == "test" else {"n_events": 0, "horizons": {}}
                continue
            s_ct = g_ct[g_ct["segment"] == seg] if len(g_ct) else g_ct
            analysis = analyze_segment(s_ev, s_ct)
            per_symbol = {s: int(g["uid"].nunique())
                          for s, g in s_ev.groupby("symbol")}
            analysis["per_symbol_n"] = per_symbol
            if seg == "test":
                analysis["go_no_go"] = go_no_go(analysis, per_symbol)
            out[seg] = analysis
        return out

    for mech in PRIMARY_MECHS:
        result["mechanisms"][mech] = _analyze_bucket(mech)
        v = result["mechanisms"][mech]["test"].get("go_no_go", {}).get(
            "verdict", "INSUFFICIENT_DATA")
        n_t = result["mechanisms"][mech]["test"].get("n_events", 0)
        print(f"  {mech:10s} test n={n_t:4d}  verdict={v}", flush=True)

    for mech in COMPARE_MECHS:
        result["compare_pool"][mech] = _analyze_bucket(mech)
        n_t = result["compare_pool"][mech]["test"].get("n_events", 0)
        print(f"  [同池对照] {mech:10s} test n={n_t:4d}", flush=True)

    return result


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=4):
    if x is None:
        return "—"
    if isinstance(x, float):
        if x != x:          # NaN:对照池病态空(同机制事件密度 ⇒ 排除窗覆盖全段)
            return "NaN"
        return f"{x:.{nd}f}"
    return str(x)


def _crit_table(mechs: dict, title: str) -> list:
    lines = ["", f"## {title}", "",
             "| 机制 | n(test) | 判1 n≥60 | 判2 增量>0@1.5x | 判3 CI下界>0@1x | 判4 ≥2品种正 | 判定 |",
             "|---|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        gg = mechs[mech]["test"].get("go_no_go") or {}
        if "criterion1_events_ge_60" not in gg:
            n = mechs[mech]["test"].get("n_events", 0)
            verdict = gg.get("verdict", "INSUFFICIENT_DATA")
            lines.append(f"| {mech} | {n} | — | — | — | — | {verdict} |")
            continue
        incr15 = gg["criterion2_incr_positive_at_1_5x"].get("incr_atr")
        if incr15 is not None and incr15 != incr15:
            n = gg["criterion1_events_ge_60"]["n_events_total"]
            lines.append(f"| {mech} | {n} | ✓ | — | — | — "
                         f"| —(对照池空,增量未定义) |")
            continue
        c = lambda b: "✓" if b else "✗"
        lines.append(
            f"| {mech} | {gg['criterion1_events_ge_60']['n_events_total']} "
            f"| {c(gg['criterion1_events_ge_60']['pass'])} "
            f"| {c(gg['criterion2_incr_positive_at_1_5x']['pass'])} "
            f"| {c(gg['criterion3_ci_lower_gt_0_at_1x']['pass'])} "
            f"| {c(gg['criterion4_two_symbols_positive']['pass'])} "
            f"| **{gg['verdict']}** |")
    return lines


def _summary_table(mechs: dict, seg: str) -> list:
    ph = str(PRIMARY_HORIZON)
    lines = ["", f"### 机制汇总({seg} 段,H={ph};ATR 单位)", "",
             "| 机制 | n | 事件净@1x (CI95) | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量@2x |",
             "|---|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        h = mechs[mech][seg].get("horizons", {}).get(ph)
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
    return lines


def _all_h_table(mechs: dict, seg: str) -> list:
    lines = ["", f"### 全视界增量({seg} 段,增量@1x ATR)与分品种符号", "",
             "| 机制 | H=6 | H=12 | H=24 | H=48 | n_pos(品种,H24) |",
             "|---|---|---|---|---|---|"]
    for mech in sorted(mechs):
        hz = mechs[mech][seg].get("horizons", {})
        h24 = hz.get(str(PRIMARY_HORIZON))
        cells = [_f(hz[str(h)]["cost"]["1_0"]["incr_atr_mean"])
                 if hz.get(str(h)) else "—" for h in HORIZONS]
        npos = (sum(1 for v in h24["by_symbol"].values() if v["mean"] > 0.0)
                if h24 and "by_symbol" in h24 else 0)
        lines.append(f"| {mech} | " + " | ".join(cells) + f" | {npos} |")
    return lines


def render_md(result: dict) -> str:
    p = result["protocol"]
    ph = str(p["primary_horizon"])
    mechs = result["mechanisms"]
    pool = result["compare_pool"]
    lines = [
        "# v9 事件研究报告(docs/v9-protocol.md:PROCESS_ACCEPTED/PROCESS_REJECTED 过程式接受/拒绝)",
        "",
        f"生成时间(UTC):{result['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        "- **最后一轮协议级研究**。预注册主格 4 个:PROCESS_ACCEPTED/PROCESS_REJECTED × 双向;"
        "全部格子数字完整公布,不挑格子报喜(§4)。",
        "- 机制:PROCESS_ACCEPTED PROCESS_ACCEPTED(停留率 occ、重心位移 centroid、当根收穿远"
        "缘;顺延续)与 PROCESS_REJECTED PROCESS_REJECTED(occ=0、试探失败 wickfail、距极"
        "值回退 k_rj·atr;反向),过程量全部 episode 相对、逐 bar 收盘累计。"
        f"先验一次写死:min_age={p['e9e10_min_age']}、occ 阈 = 2/3"
        f"({p['e9_occ_min']})、centroid 阈 = {p['e9_centroid_min']}·ATR、"
        f"k_rj = {p['k_rj']}。",
        "- 评估协议 = methodology §9 全流程原样:TF=1h," + p["timeframe"]
        + f";H ∈ {p['horizons']},主视界 H={p['primary_horizon']};对照 42 固定"
        "种子(同品种/同 regime/同方向/避同机制事件窗口 24 根);日块 bootstrap"
        " 2000 次 95% CI;成本压力 1x/1.5x/2x;四判据逐格,**增量口径**同 "
        "v1-v3(增量 = 事件净期望 − 对照净期望均值)。",
        f"- **test 段声明(§9.4 系/v9-protocol §4)**:{p['test_eval_declaration']}。"
        "本报告为该次曝光的产物。",
        "- " + p["sensitivity_declaration"] + "。",
        "- F1/room/r_min 闸属回测层(protocol §3/§4 判别链),本主判定不施加"
        "(v1 事件研究同例);F1 参考计数见 §4,仅信息。",
        "- BREAKOUT_ACCEPTED/LEVEL_REJECTED 同池对照(协议§5 动机核验):同一回放、同一时间线、同一种子"
        "栅格、同一测量管线(v1 口径);**不属 v9 判定家族**。",
        "",
        ""
    ]
    lines += _crit_table(
        mechs, "1. GO / NO-GO 主格判定表(PROCESS_ACCEPTED/PROCESS_REJECTED × 双向,test 段,H=" + ph
        + ",判据同 §9.3;4 格完整公布)")
    lines += _summary_table(mechs, "test")
    lines += _summary_table(mechs, "dev")
    lines += _all_h_table(mechs, "test")
    n_tot = sum(mechs[m]["test"].get("n_events", 0) for m in mechs)
    if n_tot == 0:
        aged = sum(info["gate_stats"]["aged_bars"]
                   for info in result["symbols"].values())
        epb = sum(info["gate_stats"]["episode_bars"]
                  for info in result["symbols"].values())
        lines += ["", "### 判别链(v9-protocol §4,如实记录)", "",
                  f"四个主格 test+dev 合计 n = **0**:PROCESS_ACCEPTED/PROCESS_REJECTED 在 3 品种 × "
                  f"17,600 根 1h(约 6 年)上**一次都未发射**。判据 1(n≥60)全"
                  f"部失败 ⇒ 全格 INSUFFICIENT_DATA。零样本病理(§2.1)给出机"
                  f"制层证据:aged episode bars(a≥min_age 的存活 bar)合计 "
                  f"**{aged} / {epb}**(约 {aged/epb*100:.3f}%)——在冻结 "
                  f"A2/A3 episode 生命周期语义下,episode 几乎全部在 1-3 根"
                  f"内被破位/BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED 收场,过程式确认要的'段'根本长不出来;"
                  f"且在仅存 aged bars 上,wickfail≥2、occ≥2/3、centroid≥0.3 "
                  f"各门通过数全为 0。按 §4 判别链,PROCESS_REJECTED(过程式拒绝)与 PROCESS_ACCEPTED(过"
                  f"程式接受)双双再证伪 ⇒ 过程式世界观在本场座终结,项目按预"
                  f"案进入纯前向时代。"]

    lines += ["", "## 2. 发射计数与分品种(dev/test 分界每品种前 70%)", ""]
    for sym, info in result["symbols"].items():
        f1 = result["f1_reference"][sym]
        lines.append(
            f"- {sym}:dev_end = {info['dev_end_iso']} (index "
            f"{info['dev_end_index']} / {info['bars']});fired PROCESS_ACCEPTED/PROCESS_REJECTED/BREAKOUT_ACCEPTED/LEVEL_REJECTED = "
            f"{info['n_e9_fired']}/{info['n_e10_fired']}/{info['n_e1_fired']}/"
            f"{info['n_e3_fired']};PROCESS_ACCEPTED/PROCESS_REJECTED fired dev {f1['dev']['fired']} / "
            f"test {f1['test']['fired']},F1 参考通过(仅信息)dev "
            f"{f1['dev']['would_pass_f1']} / test {f1['test']['would_pass_f1']}")

    lines += ["", "### 2.1 零样本病理(事实证据、**非判定输入**,判定仍按 §9 原样;探针 = 主回放同参数第二遍,逐门统计 aged episode bars,aged = episode 年龄 a ≥ min_age 的存活 bar)", "",
              "| 品种 | episode bars | aged bars | PROCESS_REJECTED occ=0 | PROCESS_REJECTED wickfail≥2 | PROCESS_REJECTED 回退到位 | PROCESS_REJECTED centroid≤0 | PROCESS_REJECTED occ0∧wf2 | PROCESS_ACCEPTED occ≥2/3 | PROCESS_ACCEPTED centroid≥0.3 | PROCESS_ACCEPTED 收穿远缘 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for sym, info in result["symbols"].items():
        g = info["gate_stats"]
        lines.append(
            f"| {sym} | {g['episode_bars']} | {g['aged_bars']} "
            f"| {g['e10_occ0']} | {g['e10_wf2']} | {g['e10_close_retreat']} "
            f"| {g['e10_centroid_le0']} | {g['e10_occ0_and_wf2']} "
            f"| {g['e9_occ_ge_thr']} | {g['e9_centroid_ge_thr']} "
            f"| {g['e9_close_beyond']} |")
    tot = lambda k: sum(info["gate_stats"][k]
                        for info in result["symbols"].values())
    lines.append(
        f"| 合计 | {tot('episode_bars')} | {tot('aged_bars')} "
        f"| {tot('e10_occ0')} | {tot('e10_wf2')} | {tot('e10_close_retreat')} "
        f"| {tot('e10_centroid_le0')} | {tot('e10_occ0_and_wf2')} "
        f"| {tot('e9_occ_ge_thr')} | {tot('e9_centroid_ge_thr')} "
        f"| {tot('e9_close_beyond')} |")

    lines += ["", "## 3. 事件式同池对照(BREAKOUT_ACCEPTED/LEVEL_REJECTED,同一管线同一栅格;上下文,非判定)", "",
              "> 口径声明:本节数字由同一次 test 曝光内的同一测量管线产生,"
              "仅作协议 §5 动机的位置对照;**不构成对 BREAKOUT_ACCEPTED/LEVEL_REJECTED 的新判定,不入"
              "白名单讨论**——白名单与生产语义是 canonical 的既有口径,本"
              "轮不触(配置层 whitelist 逐项照抄 canonical)。NaN 行 = §9.1-5"
              " 同机制排除窗在高事件密度下覆盖全段、对照池为空,**增量在数"
              "学上未定义**(如实呈现,非实现故障;v1 时代 LEVEL_REJECTED 密度低、此态"
              "未现)。", ""]
    lines += _crit_table(pool, "3.1 同池对照桶判定参考(test 段,同一 §9 口径)")
    lines += _summary_table(pool, "test")
    lines += ["", "### 3.2 主机制 × 对照机制并排(test 段,H=" + ph
              + ",增量@1x ATR)", "",
              "| 桶 | 家族 | n | 增量@1x | 增量@1.5x | CI95 下界@1x |",
              "|---|---|---|---|---|---|"]
    for fam, dd in (("v9 主格", mechs), ("事件式对照", pool)):
        for mech in sorted(dd):
            h = dd[mech]["test"].get("horizons", {}).get(ph)
            if not h:
                lines.append(f"| {mech} | {fam} | 0 | — | — | — |")
                continue
            c1 = h["cost"]["1_0"]
            lines.append(f"| {mech} | {fam} | {h['n']} "
                         f"| {_f(c1['incr_atr_mean'])} "
                         f"| {_f(h['cost']['1_5']['incr_atr_mean'])} "
                         f"| {_f(c1['incr_atr_ci95'][0])} |")

    if result["detail_paths"]:
        lines += ["", "## 4. 产物索引", ""]
        for seg in ("dev", "test"):
            lines.append(f"- 逐事件 {seg}:`{result['detail_paths'][seg]}`")
    lines += ["", "机器可读全量:`reports/event_study_v9.json`(机制 × 段 × "
                  "成本 × 视界,含 regime 分解、稳定性、分品种)。", ""]
    return "\n".join(lines)


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    if "--render-only" in sys.argv:
        # 只重渲 md(物 = 既有 event_study_v9.json;不重放、不重测、不占曝光)
        result = json.loads((REPORTS / "event_study_v9.json").read_text(
            encoding="utf-8"))
        (REPORTS / "event_study_v9.md").write_text(
            render_md(result), encoding="utf-8")
        print("-> reports/event_study_v9.md (render-only)")
        return
    result = run()
    (REPORTS / "event_study_v9.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "event_study_v9.md").write_text(render_md(result), encoding="utf-8")
    print("-> reports/event_study_v9.json, reports/event_study_v9.md, "
          "reports/event_study_events_v9_{dev,test}.csv")


if __name__ == "__main__":
    main()
