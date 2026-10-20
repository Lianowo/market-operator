"""v11 归因测量管线(docs/v11-protocol.md,冻结 2026-10-20:跨 episode 区域发育)。

用法:  python scripts/run_zonebio.py
产物:  reports/zonebio.json   机器可读全量(判别/分桶/台账)
       reports/zonebio.csv    逐触碰履历行(全品种全段)
       reports/zonebio.md     人读报告(判别句 + 分桶 + k_* + test 复报)

纪律(v11-protocol §1-§4,冻结):
- 事件检测零改动,只读结构层输出:每 (symbol, 1h) 重放 MarketStructure,
  登记每区域每次接近(A2 入、A3 段)的履历快照(全部因果),前瞻仅标注
  (H=24 事件研究口径)。解释性决定全在 src/market_operator/zonebio.py
  模块 docstring(触碰单元 = A3 接近段;k = 身份链履历行序数,冻结 per-bar
  touches 另列 touches_frozen;out 锚 = 外缘;终止 BROKEN/FLIPPED/DEAD/
  ALIVE;合并并集续接身份链)。
- 判别族只在 dev 段(dev_frac 同既有拐点 research.split_boundary,0.7);
  test 段复报同表一次性(v11 唯一一次,§2;此后只许前向)。数据单元 =
  zone-touch(接近段行),跨品种合池;日块(UTC)跨品种同一自然日为块,
  保留跨品种同日相关性;bootstrap 2000 次,种子 = research.BOOT_SEED。
- 主判别两假说句(§2,写死在先):衰假说 = 全池 dev Spearman(k, out_rj) < 0
  且 CI 不含 0(越摸越薄);厚假说 = ρ > 0 且 CI 不含 0(流动性累积)。两者
  都不成立 ⇒ 按 §4 如实得"区域履历不含方向信息",不做再联想。
- k_* 断点候选 {2,3},只许 dev 判定(zonebio.decide_k_star 冻结规则),
  取 separation 较大者、并列取小;判定后冻结,test 不再择优(§3)。
- 不改 events/structure/收割/费用;merge cap 按 canonical 语义(内含,
  merge_max_atr=2.0);历史产物/config v1-v9 不动;research-log(#22)不动。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import data as mo_data                       # noqa: E402
from market_operator.research import BOOT_SEED, split_boundary    # noqa: E402
from market_operator.structure import Params                      # noqa: E402
from market_operator.zonebio import (                             # noqa: E402
    BOOT_REPS,
    H_FORWARD,
    K_STAR_CANDIDATES,
    age_cross_table,
    bootstrap_spearman_ci,
    bucket_table,
    collect_biography,
    decide_k_star,
    spearman,
)

CONFIG = ROOT / "config" / "canonical.json"
REPORTS = ROOT / "reports"


def _spearman_block(df: pd.DataFrame, xcol: str) -> dict:
    g = df.dropna(subset=["out_rj_k"])
    if len(g) < 3:
        return {"n": int(len(g)), "rho": None, "ci95": [None, None]}
    rho, ci = bootstrap_spearman_ci(g[xcol].to_numpy(),
                                    g["out_rj_k"].to_numpy(),
                                    g["ts"].to_numpy(),
                                    reps=BOOT_REPS, seed=BOOT_SEED)
    return {"n": int(len(g)), "rho": rho,
            "ci95": [ci[0], ci[1]]}


def _verdict(main: dict) -> dict:
    rho, (lo, hi) = main["rho"], main["ci95"]
    out = {"fade": False, "thick": False,
           "sentence": "主判别不可计算(样本不足)"}
    if rho is None or rho != rho or lo is None or lo != lo:
        return out
    if rho < 0.0 and hi < 0.0:
        out["fade"] = True
        out["sentence"] = ("衰假说成立:ρ<0 且 CI 不含 0——越摸越薄,"
                           "防守随触碰次数衰减")
    elif rho > 0.0 and lo > 0.0:
        out["thick"] = True
        out["sentence"] = ("厚假说成立:ρ>0 且 CI 不含 0——供给随触碰累积,"
                           "防守随触碰次数增强(可能流动性累积)")
    else:
        out["sentence"] = ("两假说均不成立(CI 含 0 或符号不符)——按 §4 如实得:"
                           "区域履历(k)对 out_rj_k 不含方向信息")
    return out


def run() -> dict:
    params = Params.from_json(CONFIG)            # 含 merge_max_atr=2.0(canonical)
    raw_cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    dev_frac = float(raw_cfg["dev_frac"])
    symbols = list(raw_cfg["symbols"])

    parts, sym_info, zone_parts = [], {}, []
    for sym in symbols:
        t0 = time.time()
        df = mo_data.load(sym)
        n = len(df)
        boundary = split_boundary(n, dev_frac)
        touches, zones = collect_biography(df, params, symbol=sym)
        touches["segment"] = np.where(touches["touch_index"] < boundary,
                                      "dev", "test")
        parts.append(touches)
        zones["symbol"] = sym
        zone_parts.append(zones)
        sym_info[sym] = {
            "bars": n, "boundary": boundary,
            "n_touches": int(len(touches)),
            "n_zones": int(len(zones)),
            "n_dev": int((touches["segment"] == "dev").sum()),
            "n_test": int((touches["segment"] == "test").sum()),
            "terminations": zones["termination"].value_counts().to_dict(),
            "elapsed_s": round(time.time() - t0, 1),
        }
        print(f"[{sym}] {n} bars, 触碰行 {len(touches)} "
              f"(dev {sym_info[sym]['n_dev']} / test {sym_info[sym]['n_test']}), "
              f"身份链头 {len(zones)}, {sym_info[sym]['elapsed_s']}s", flush=True)

    all_t = pd.concat(parts, ignore_index=True)
    all_z = pd.concat(zone_parts, ignore_index=True)
    dev = all_t[all_t["segment"] == "dev"]
    test = all_t[all_t["segment"] == "test"]

    # 主判别(dev;合池;数据单元 = zone-touch)
    main_k = _spearman_block(dev, "k")
    main_logk = _spearman_block(
        dev.assign(logk=np.log(dev["k"].astype(float))), "logk")
    verdict = _verdict(main_k)
    # 分品种上下文(非判定族)
    per_symbol = {}
    for sym in symbols:
        per_symbol[sym] = _spearman_block(dev[dev["symbol"] == sym], "k")

    # 次判别(dev):分桶 × role;区龄混缠复核;k_* 断点
    dev_bucket = bucket_table(dev)
    dev_age = age_cross_table(dev)
    k_decision = decide_k_star(dev, K_STAR_CANDIDATES)

    # test 段复报同表(一次性;此后只许前向)
    test_main_k = _spearman_block(test, "k")
    test_logk = _spearman_block(
        test.assign(logk=np.log(test["k"].astype(float))), "logk")
    test_bucket = bucket_table(test)
    test_age = age_cross_table(test)

    result = {
        "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "protocol": {
            "parent": "docs/v11-protocol.md(冻结 2026-10-20)",
            "unit": "zone-touch(A3 接近段行;含 origin 行 k=1)",
            "config": "config/canonical.json(含 merge_max_atr=2.0)",
            "dev_frac": dev_frac,
            "boundary_rule": "research.split_boundary(每品种前 dev_frac 为 dev)",
            "H_FORWARD": H_FORWARD,
            "boot": {"reps": BOOT_REPS, "seed": BOOT_SEED,
                     "block": "UTC 自然日,跨品种同日同块"},
            "k_star_candidates": list(K_STAR_CANDIDATES),
            "k_star_rule": "median(out_rj|k≤c) − median(out_rj|k>c) 取大,并列取小",
            "test_exposure": "test 段 v11 唯一一次全表复报(此后只许前向;log#22 既定)",
        },
        "symbols": sym_info,
        "dev": {
            "spearman_k": main_k,
            "spearman_logk": main_logk,
            "verdict": verdict,
            "per_symbol_spearman_k": per_symbol,
            "bucket_table": dev_bucket.to_dict("records"),
            "age_cross": dev_age.to_dict("records"),
            "k_star": k_decision,
        },
        "test": {
            "spearman_k": test_main_k,
            "spearman_logk": test_logk,
            "bucket_table": test_bucket.to_dict("records"),
            "age_cross": test_age.to_dict("records"),
        },
        "zone_ledger": all_z.to_dict("records"),
    }
    return result, all_t


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=3):
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def _bucket_lines(records, seg):
    lines = [f"### k 分桶({seg};out 单位 ATR,H=24)", "",
             "| role | k | n | out_rj 中位 | out_rj 均值 | out_br 中位 | out_br 均值 | 24h破位率 | 存活率 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        if r["role"] == "all":
            lines.append(f"| {r['role']} | {r['bucket']} | {r['n']} "
                         f"| {_f(r['out_rj_med'])} | {_f(r['out_rj_mean'])} "
                         f"| {_f(r['out_br_med'])} | {_f(r['out_br_mean'])} "
                         f"| {_f(r['break24_rate'])} | {_f(r['survive24_rate'])} |")
    return lines


def _role_lines(records, seg):
    lines = [f"### 按 role 拆分({seg})", "",
             "| role | k | n | out_rj 中位 | out_rj 均值 | out_br 中位 | 24h破位率 |",
             "|---|---|---|---|---|---|---|"]
    for r in records:
        if r["role"] != "all":
            lines.append(f"| {r['role']} | {r['bucket']} | {r['n']} "
                         f"| {_f(r['out_rj_med'])} | {_f(r['out_rj_mean'])} "
                         f"| {_f(r['out_br_med'])} | {_f(r['break24_rate'])} |")
    return lines


def _age_lines(records, seg):
    lines = [f"### 区龄 × k 混缠复核({seg};out_rj 中位)", "",
             "| 区龄 | k=1 | k=2 | k=3 | k=4 | k≥5 |", "|---|---|---|---|---|---|"]
    for al in ("≤1d", "1-7d", "7+d"):
        row = [r for r in records if r["age"] == al]
        cells = []
        for kl in ("1", "2", "3", "4", "≥5"):
            g = [r for r in row if r["bucket"] == kl][0]
            cells.append(f"{_f(g['out_rj_med'])} (n={g['n']})")
        lines.append(f"| {al} | " + " | ".join(cells) + " |")
    return lines


def render_md(result: dict) -> str:
    dev, test, syms = result["dev"], result["test"], result["symbols"]
    mk, mlk = dev["spearman_k"], dev["spearman_logk"]
    tk = test["spearman_k"]
    ks = dev["k_star"]
    tot_dev = sum(i["n_dev"] for i in syms.values())
    tot_test = sum(i["n_test"] for i in syms.values())
    role_dev = int(len([r for r in result["zone_ledger"]]))
    lines = [
        "# v11 区域发育履历报告(docs/v11-protocol.md:跨 episode 区域发育,归因测量)",
        "",
        f"生成时间(UTC):{result['generated_at_utc']}",
        "",
        "## 0. 协议与口径",
        "",
        "- 数据单元 = **zone-touch(A3 接近段行)**:沿 A2 入、至首次整根脱离带;origin 行"
        " k=1;pivot 知时生成。**全部输入因果(k/age/role/merged/regime/touches_frozen),"
        "前瞻(H=24)仅用于标注**;窗与锚定义见 zonebio.py docstring(外缘基准,ATR 单位)。",
        f"- 判别族只在 **dev**(每品种前 {result['protocol']['dev_frac']};既有拐点"
        f" split_boundary);test 全表为 v11 唯一一次复报(log#22 既定曝光)。"
        f"日块 bootstrap {result['protocol']['boot']['reps']} 次,种子 "
        f"{result['protocol']['boot']['seed']},跨品种同日同块。",
        "- 结构/检测/收割/费用零改动;merge cap 按 canonical(merge_max_atr=2.0)语义内含。",
        "",
        "## 1. 履历总量",
        "",
        "| 品种 | bars | 拐点位 | 触碰行(dev/test) | 身份链头 | 终止分布 |",
        "|---|---|---|---|---|---|",
    ]
    for sym, i in syms.items():
        lines.append(f"| {sym} | {i['bars']} | {i['boundary']} "
                     f"| {i['n_touches']} ({i['n_dev']}/{i['n_test']}) "
                     f"| {i['n_zones']} | {i['terminations']} |")
    lines.append(f"| 合计 | — | — | {tot_dev + tot_test} ({tot_dev}/{tot_test}) "
                 f"| {role_dev} | — |")

    lines += ["", "## 2. 主判别(dev,合池;k 与 out_rj_k)", "",
              f"- Spearman(k, out_rj_k):ρ = **{_f(mk['rho'])}**,"
              f" CI95 = [{_f(mk['ci95'][0])}, {_f(mk['ci95'][1])}](n={mk['n']})",
              f"- Spearman(log k, out_rj_k):ρ = {_f(mlk['rho'])},"
              f" CI95 = [{_f(mlk['ci95'][0])}, {_f(mlk['ci95'][1])}](n={mlk['n']})",
              f"- **判定句**:{dev['verdict']['sentence']}", "",
              "| 品种 | n | ρ(k) |", "|---|---|---|"]
    for sym, s in dev["per_symbol_spearman_k"].items():
        lines.append(f"| {sym} | {s['n']} | {_f(s['rho'])} |")
    lines.append("")
    lines += _bucket_lines(dev["bucket_table"], "dev")
    lines.append("")
    lines += _role_lines(dev["bucket_table"], "dev")
    lines.append("")
    lines += _age_lines(dev["age_cross"], "dev")
    lines += ["", "## 3. k_* 断点(dev 判定,候选 {2,3},冻结规则)", ""]
    for c, v in ks["candidates"].items():
        lines.append(f"- c={c}:fresh(k≤{c}) n={v['n_fresh']} med={_f(v['med_fresh'])};"
                     f" stale(k>{c}) n={v['n_stale']} med={_f(v['med_stale'])};"
                     f" separation={_f(v['separation'])}")
    lines.append(f"- **k_* = {ks['k_star']}**(取 separation 较大者,并列取小;"
                 "判定后冻结,test 不再择优)")

    lines += ["", "## 4. test 段复报(v11 唯一一次;此后只许前向)", "",
              f"- Spearman(k, out_rj_k):ρ = **{_f(tk['rho'])}**,"
              f" CI95 = [{_f(tk['ci95'][0])}, {_f(tk['ci95'][1])}](n={tk['n']})",
              f"- Spearman(log k, out_rj_k):ρ = {_f(test['spearman_logk']['rho'])},"
              f" CI95 = [{_f(test['spearman_logk']['ci95'][0])},"
              f" {_f(test['spearman_logk']['ci95'][1])}]"
              f"(n={test['spearman_logk']['n']})", ""]
    lines += _bucket_lines(test["bucket_table"], "test")
    lines.append("")
    lines += _age_lines(test["age_cross"], "test")
    lines += ["", "机器可读全量:`reports/zonebio.json`;逐触碰履历:`reports/zonebio.csv`。", ""]
    return "\n".join(lines)


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    result, all_t = run()
    (REPORTS / "zonebio.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    all_t.to_csv(REPORTS / "zonebio.csv", index=False)
    (REPORTS / "zonebio.md").write_text(render_md(result), encoding="utf-8")
    print("-> reports/zonebio.json, reports/zonebio.csv, reports/zonebio.md")


if __name__ == "__main__":
    main()
