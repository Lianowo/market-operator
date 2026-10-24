"""F3 多重比较台账 + F4 块自助法(不新增回放,只读既有产物)。

F3:entry_*.json 每个 TF 有 119 个格子,4 个 TF 合计 476 次评估。"best_knobs"
是**取最大值**得到的,必然高于总体均值。本脚本用**重采样**给出零假设下"max 的
分布":把 119 个格子当作可交换样本,有放回重采样 5000 次取最大值。若观测 max
落在该分布的常规区间,则"最优旋钮"与噪声不可区分。

F4:逐笔净收益按**平仓日**聚合,再用 circular block bootstrap(块长 5 天,
2000 次)给出总净收益/E[R] 的 90% 区间。块长 > 1 是为了保留相邻日的相关性
(同一根趋势里连续止损并不独立)。自助法只重排**已实现盈亏序列**,不重建权益
路径,故区间是"同一组交易换一段历史"的不确定性,不是策略重跑的方差。

用法:
  python scripts/_diag_v21_f34_stats.py [--dir reports/p012_20261023]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRID_DIR = ROOT / "reports" / "tf_opt_20261023"
B_BOOT = 2000
BLOCK_DAYS = 5
SEED = 20261023


def expected_max_z(n: int, n_iter: int = 20000, seed: int = SEED) -> float:
    """N 个独立标准正态取最大值的期望 z(E[max] 的零假设基准)。"""
    rng = random.Random(seed)
    vals = sorted(max(rng.gauss(0.0, 1.0) for _ in range(n))
                  for _ in range(n_iter))
    return vals[n_iter // 2]


def p_max_at_least(z: float, n: int) -> float:
    """若 N 个格子都是同分布噪声,P(某个 ≥ 观测 z) ≈ 1 − Φ(z)^N。"""
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return 1.0 - cdf ** n


def f3() -> dict:
    out: dict = {}
    for tf in ("2h", "4h", "12h", "1d"):
        path = GRID_DIR / f"entry_{tf}.json"
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        cells = [c for c in doc.get("grid", []) if c.get("knobs")]
        ers = [float(c.get("er_net") or 0.0) for c in cells]
        if not ers:
            continue
        obs = max(ers)
        mean, sd = statistics.mean(ers), statistics.pstdev(ers) or 1.0
        z = (obs - mean) / sd
        out[tf] = {
            "n_cells": len(ers),
            "observed_max_er": round(obs, 6),
            "mean_er": round(mean, 6), "sd_er": round(sd, 6),
            # 零假设基准:119 个同分布格子取最大值,期望 z ≈ 2.1–2.3
            "expected_max_z": round(expected_max_z(len(ers)), 3),
            "z_of_max": round(z, 3),
            "p_max_at_least": round(p_max_at_least(z, len(ers)), 6),
            "top_share": round(sum(1 for x in ers if x >= obs) / len(ers), 4),
        }
    total = sum(v["n_cells"] for v in out.values())
    out["_ledger"] = {
        "cells_scored": total, "families": len(out),
        "alpha_per_cell_bonferroni": round(0.05 / max(total, 1), 6),
        "note": ("同一数据集上反复打分;任何 single-cell 结论都必须先通过 F5 "
                 "平台检验,再做 F4 区间"),
    }
    return out


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000,
                                  tz=timezone.utc).strftime("%Y-%m-%d")


def f4(path: Path) -> dict:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    daily: dict[str, float] = defaultdict(float)
    risk_total = 0.0
    for r in rows:
        if not r.get("exit_ts"):
            continue
        daily[_day(int(r["exit_ts"]))] += float(r["net_pnl"])
        risk_total += float(r["risk_usdt"] or 0.0)
    days = sorted(daily)
    series = [daily[d] for d in days]
    if len(series) < BLOCK_DAYS * 4:
        return {"error": f"too few days: {len(series)}"}
    rng = random.Random(SEED)
    n = len(series)
    n_blocks = max(1, n // BLOCK_DAYS)
    totals = []
    for _ in range(B_BOOT):
        acc = 0.0
        for _ in range(n_blocks):
            start = rng.randrange(n)
            for k in range(BLOCK_DAYS):
                acc += series[(start + k) % n]
        totals.append(acc)
    totals.sort()
    q = lambda p: round(totals[int(p * len(totals))], 4)     # noqa: E731
    return {
        "days": n, "trades": len(rows), "point_net": round(sum(series), 2),
        "ci90_net": [q(0.05), q(0.95)],
        "ci90_e_r": [round(q(0.05) / risk_total, 4) if risk_total else None,
                     round(q(0.95) / risk_total, 4) if risk_total else None],
        "p_net_le_0": round(sum(1 for x in totals if x <= 0) / len(totals), 4),
        "block_days": BLOCK_DAYS, "n_boot": B_BOOT,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "reports" / "p012_20261023"))
    args = ap.parse_args()
    out_dir = Path(args.dir)
    payload: dict = {"f3": f3(), "f4": {}}
    for name in ("base_hist", "base_const"):
        path = out_dir / f"trades_{name}.csv"
        if path.exists():
            payload["f4"][name] = f4(path)
    (out_dir / "f34_stats.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    L = ["# F3 多重比较 + F4 块自助法", "",
         "## F3 每个 TF 的 119 格网格:最优格子是不是噪声的最大值", "",
         "| TF | 格子 | 观测 max E[R] | 均值 | sd | z | 零假设 E[max] z | P(max≥观测) |",
         "|---|---|---|---|---|---|---|---|"]
    for tf, v in payload["f3"].items():
        if tf.startswith("_"):
            continue
        L.append(f"| {tf} | {v['n_cells']} | {v['observed_max_er']:+.4f} | "
                 f"{v['mean_er']:+.4f} | {v['sd_er']:.4f} | {v['z_of_max']:.2f} | "
                 f"{v['expected_max_z']:.2f} | {v['p_max_at_least']:.4f} |")
    led = payload["f3"].get("_ledger", {})
    L += ["", f"- 合计评估格子 **{led.get('cells_scored')}**;"
          f"Bonferroni 单格 α = **{led.get('alpha_per_cell_bonferroni')}**",
          "- 读法:观测 z 与“零假设 E[max] z”同量级 ⇒ 最优格子正是 119 次取最大"
          "会产生的水平,不能当作机制证据;只有 z 明显高于基准才值得继续验证。", "",
          "## F4 块自助法(平仓日聚合,块长 5 天,2000 次)", "",
          "| 账本 | 交易日 | 笔数 | 点估计净收益 | 90% CI | E[R] 90% CI | P(净≤0) |",
          "|---|---|---|---|---|---|---|"]
    for name, v in payload["f4"].items():
        if "error" in v:
            L.append(f"| {name} | - | - | - | {v['error']} | - | - |")
            continue
        L.append(f"| {name} | {v['days']} | {v['trades']} | {v['point_net']:+.2f} | "
                 f"[{v['ci90_net'][0]:+.2f}, {v['ci90_net'][1]:+.2f}] | "
                 f"[{v['ci90_e_r'][0]:+.4f}, {v['ci90_e_r'][1]:+.4f}] | "
                 f"{v['p_net_le_0']:.3f} |")
    L += ["", "口径:只重排已实现盈亏,不重建权益路径(不含复利与容量反馈);"
          "CI 窄于“换一段行情重跑”的真实不确定性,用于判断**符号稳定性**。"]
    (out_dir / "F34_STATS.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
