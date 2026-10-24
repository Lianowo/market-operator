"""F5:邻域稳健性重评分(零回放成本,只读既有 entry_*.json 网格)。

问题:entry_*.json 里的 `best_knobs` 是**在 dev 段上挑出来的最大值**。任何一次
网格搜索都会产生一个最大值;要判断它是不是"噪声里的尖峰",必须看它的**邻居**:
如果相邻一档的参数也拿到相近的分,即使具体取值是拟合出来的,机制本身仍在
平台上;若只有这一个格子好,则该结论不可上线。

本脚本不做任何回放,只对已落盘的 grid 做三件事:
1. **平台宽度**:达到 best 分数 90% 的格子数 / 总格子数;
2. **一跳邻居**:best 的每个相邻取值(每次只动一个旋钮、动到相邻档)的分数
   中位数 / best 分数;
3. **符号一致性**:全部格子里 `er_net > 0` 的占比(边缘方向是否稳定)。

用法: python scripts/_diag_v21_f5_neighborhood.py
输出: reports/p012_20261023/F5_NEIGHBORHOOD.md / .json
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "reports" / "tf_opt_20261023"
OUT = ROOT / "reports" / "p012_20261023"


def knob_values(cells: list, knob: str) -> list:
    vals = {c["knobs"].get(knob) for c in cells if knob in c.get("knobs", {})}
    return sorted(vals, key=lambda v: (isinstance(v, str), v))


def neighbors(cells: list, best: dict, knobs: list) -> list:
    """一跳邻居:恰好一个旋钮动到相邻档,其余完全相同。"""
    out = []
    for knob in knobs:
        vals = knob_values(cells, knob)
        cur = best["knobs"].get(knob)
        if cur not in vals:
            continue
        i = vals.index(cur)
        for j in (i - 1, i + 1):
            if not (0 <= j < len(vals)):
                continue
            target = dict(best["knobs"])
            target[knob] = vals[j]
            for c in cells:
                if c.get("knobs") == target:
                    out.append((knob, vals[j], c))
                    break
    return out


def analyze(name: str, doc: dict) -> dict:
    cells = [c for c in doc.get("grid", []) if c.get("knobs")]
    if not cells:
        return {"tf": name, "error": "grid empty"}
    score_key = "score" if "score" in cells[0] else "er_net"
    best = max(cells, key=lambda c: float(c.get(score_key) or 0.0))
    bs = float(best.get(score_key) or 0.0)
    plateau = [c for c in cells if float(c.get(score_key) or 0.0) >= 0.9 * bs]
    knobs = sorted({k for c in cells for k in c["knobs"]})
    nb = neighbors(cells, best, knobs)
    nb_scores = [float(c.get(score_key) or 0.0) for _, _, c in nb]
    ers = [float(c.get("er_net") or 0.0) for c in cells]
    return {
        "tf": name, "n_cells": len(cells), "score_key": score_key,
        "best_knobs": best["knobs"], "best_score": round(bs, 6),
        "best_net": best.get("net"), "best_n": best.get("n"),
        "plateau_n": len(plateau),
        "plateau_share": round(len(plateau) / len(cells), 4),
        "neighbors_n": len(nb),
        "neighbor_median": round(statistics.median(nb_scores), 6) if nb_scores else None,
        "neighbor_ratio": (round(statistics.median(nb_scores) / bs, 4)
                           if nb_scores and bs else None),
        "er_pos_share": round(sum(1 for x in ers if x > 0) / len(ers), 4),
        "er_iqr": round(statistics.quantiles(ers, n=4)[2]
                        - statistics.quantiles(ers, n=4)[0], 6),
        "neighbor_detail": [{"knob": k, "value": v,
                             "er_net": c.get("er_net"), "n": c.get("n")}
                            for k, v, c in nb],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows, payload = [], {}
    for tf in ("2h", "4h", "12h", "1d"):
        path = SRC / f"entry_{tf}.json"
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        r = analyze(tf, doc)
        payload[tf] = r
        rows.append(r)
    lines = ["# F5 邻域稳健性(只读重评分,零回放)", "",
             "| TF | 格子 | best score | 平台数 | 平台占比 | 一跳邻居中位/best | "
             "E[R]>0 占比 | E[R] IQR |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['tf']} | {r['n_cells']} | {r['best_score']:.4f} | "
            f"{r['plateau_n']} | {r['plateau_share']:.1%} | "
            f"{r['neighbor_ratio']:.3f} | {r['er_pos_share']:.1%} | "
            f"{r['er_iqr']:.4f} |")
    lines += ["", "判定口径:", "- 平台占比 ≥ 10% 且邻居比 ≥ 0.85 ⇒ 机制在平台上,"
              "具体取值不必精确;", "- 平台占比 < 5% 或邻居比 < 0.6 ⇒ 该 TF 的"
              "“最优旋钮”是噪声尖峰,不应固化。"]
    (OUT / "F5_NEIGHBORHOOD.md").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
    (OUT / "f5_neighborhood.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
