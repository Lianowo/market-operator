"""锁利回吐预算 `reject_opp_lock_frac` 标定扫描(v21.7 口径)。

背景:v21.7 把对侧锁利从"绝对价位"改成"以峰值为锚的回吐预算"
    budget = (1−|frac|)·|target − entry|,  stop = peak ∓ budget
`frac` 是唯一参数(生产值 −0.50),但它此前**没有标定过**:0.50 是"锁一半、
让一半"的语义直觉值。本脚本在同一场景(explore / book4 × dev/test/full、
1x 成本、生产配置)下扫 frac,给出净利/回撤/锁利耗尽离场占比的整条曲线,而不是
只报一个数 —— 取"平台区"而不是取"最高点",避免在 test 段上过拟合。

同时对照两组开关(仅 frac=−0.50):
- `exit_exec=True`:把 §8.1/§8.2/§8.3 搬到执行细 bar(2h 触发粒度);
- `pivot_exec=True`:只把 §8.3 的 pivot 搬到细 bar。
v21.6 曾在**旧锁利口径**下判过这两项"11/12 组变差",语义换掉后必须重测。

口径:与 `tools/replay_acceptance.py` 完全同源(`ScenarioRunner` + `summarize`),
因此数字可与 `reports/_head_acceptance.txt`(HEAD 旧语义)、
`reports/_newlock_acceptance.txt`(v21.7 生产值)直接对账。

    python scripts/_diag_lock_frac_sweep.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from market_operator.campaign import ArmConfig                  # noqa: E402
from market_operator.scenario import PRODUCTION_CONFIG, ScenarioRunner  # noqa: E402
from replay_acceptance import SCENARIOS, summarize              # noqa: E402

SEGMENTS = ("dev", "test", "full")
LOCK_TAG = "lock_giveback_exhausted"

ARMS: list[tuple[str, ArmConfig]] = [
    ("frac=0.00 不锁利(§8.2 减仓 50%)", ArmConfig(reject_opp_lock_frac=0.0)),
    ("frac=-0.25 预算 0.75span(生产)", ArmConfig(reject_opp_lock_frac=-0.25)),
    ("frac=-0.35 预算 0.65span", ArmConfig(reject_opp_lock_frac=-0.35)),
    ("frac=-0.50 预算 0.50span", ArmConfig(reject_opp_lock_frac=-0.50)),
    ("frac=-0.65 预算 0.35span", ArmConfig(reject_opp_lock_frac=-0.65)),
    ("frac=-0.80 预算 0.20span", ArmConfig(reject_opp_lock_frac=-0.80)),
    ("frac=-1.00 预算 0(触价即平)", ArmConfig(reject_opp_lock_frac=-1.00)),
]

#: 粒度开关对照(只在决赛 frac 上跑)。
GATES: list[tuple[str, ArmConfig]] = [
    ("frac=-0.50 + exit_exec", ArmConfig(reject_opp_lock_frac=-0.50,
                                         exit_exec=True)),
    ("frac=-0.25 + exit_exec", ArmConfig(reject_opp_lock_frac=-0.25,
                                         exit_exec=True)),
    ("frac=-0.50 + pivot_exec", ArmConfig(reject_opp_lock_frac=-0.50,
                                          pivot_exec=True)),
]

#: 决赛圈(见报告 §2/§3):只对这两类做 2x 成本复核。
FINALISTS: list[tuple[str, ArmConfig]] = [
    ("frac=-0.25 预算 0.75span(生产)", ArmConfig(reject_opp_lock_frac=-0.25)),
    ("frac=-0.50 预算 0.50span", ArmConfig(reject_opp_lock_frac=-0.50)),
    ("frac=-0.50 + exit_exec", ArmConfig(reject_opp_lock_frac=-0.50,
                                         exit_exec=True)),
]


def _lock_split(outcome) -> tuple[int, float]:
    """锁利耗尽离场:笔数与净利(其余出场合并成一格,便于看占比)。"""
    n, net = 0, 0.0
    for trade in outcome.closed_trades:
        if LOCK_TAG in str(trade.get("reason_exit") or ""):
            n += 1
            net += float(trade.get("net_pnl") or 0.0)
    return n, round(net, 2)


def main() -> None:
    rows: list[dict] = []
    skipped: list[str] = []
    runners: dict[str, ScenarioRunner] = {}
    for name, symbols in SCENARIOS.items():
        print(f"[prep] {name}", flush=True)
        runner = ScenarioRunner(symbols, PRODUCTION_CONFIG, progress=False)
        runners[name] = runner
        for label, arm in ARMS + GATES:
            for seg in SEGMENTS:
                try:
                    out = runner.run(segment=seg, arm=arm)
                except ValueError as exc:
                    # `pivot_exec` 在生产链上装配缺失 ⇒ run_campaigns 报错(设计如此:
                    # 宁可失败,也不产出"开关无效"的假读数)。
                    print(f"  [skip] {label}: {exc}", flush=True)
                    skipped.append(f"{label}: {exc}")
                    break
                s = summarize(out)
                lock_n, lock_net = _lock_split(out)
                row = {"scenario": name, "seg": seg, "arm": label,
                       "n": s["n"], "net": s["net"],
                       "pf": s["profit_factor"], "dd": s["max_drawdown_pct"],
                       "lock_n": lock_n, "lock_net": lock_net,
                       "pivot_ticks": s["counters"].get("pivot_exec_ticks", 0),
                       "exec_ticks": s["counters"].get("exit_exec_ticks", 0)}
                rows.append(row)
                print(f"  {name:7s}|{seg:4s}|{label:34s} n={row['n']:5d} "
                      f"net={row['net']:+9.2f} pf={row['pf']} "
                      f"dd={row['dd']:5.2f}% lock={row['lock_n']:4d} "
                      f"lockNet={row['lock_net']:+8.2f} "
                      f"ticks={row['pivot_ticks']}+{row['exec_ticks']}",
                      flush=True)

    lines = ["# 锁利回吐预算 frac 扫描(v21.7 / 生产配置 / 1x)", "",
             "口径 = `tools/replay_acceptance.py` 同源;`lock` = 回吐预算耗尽离场。",
             "", "## 1. 逐场景", "",
             "| 场景|段 | arm | N | net | PF | DD% | lock | lockNet |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {r['seg']} | {r['arm']} | {r['n']} | "
            f"{r['net']:+.2f} | {r['pf']} | {r['dd']:.2f} | {r['lock_n']} | "
            f"{r['lock_net']:+.2f} |")

    lines += ["", "## 2. 合计(6 段相加)", "",
              "| arm | Σnet | Σlock | ΣlockNet | dev | test | full |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    totals = {}
    for label, _arm in ARMS + GATES:
        cells = [r for r in rows if r["arm"] == label]
        if not cells:
            lines.append(f"| {label} | — | — | — | — | — | — |")
            continue
        by_seg = {s: sum(r["net"] for r in cells if r["seg"] == s)
                  for s in SEGMENTS}
        totals[label] = sum(r["net"] for r in cells)
        lines.append(
            f"| {label} | {totals[label]:+.2f} | "
            f"{sum(r['lock_n'] for r in cells)} | "
            f"{sum(r['lock_net'] for r in cells):+.2f} | "
            f"{by_seg['dev']:+.2f} | {by_seg['test']:+.2f} | "
            f"{by_seg['full']:+.2f} |")

    if skipped:
        lines += ["", "未跑成的臂(`run_campaigns` 按设计直接报错,不产出假读数):", ""]
        lines += [f"- `{s}`" for s in sorted(set(skipped))]

    lines += ["", "## 3. 成本鲁棒性(决赛圈 × 1x/2x)", "",
              "| arm | cost | Σnet | 最差段 | dev | test | full |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for label, arm in FINALISTS:
        for cost in (1.0, 2.0):
            cells = []
            for name in SCENARIOS:
                for seg in SEGMENTS:
                    out = runners[name].run(segment=seg, cost_multiplier=cost,
                                            arm=arm)
                    cells.append({"seg": seg, "net": summarize(out)["net"]})
            by_seg = {s: sum(c["net"] for c in cells if c["seg"] == s)
                      for s in SEGMENTS}
            worst = min(c["net"] for c in cells)
            lines.append(
                f"| {label} | {cost:.0f}x | "
                f"{sum(c['net'] for c in cells):+.2f} | {worst:+.2f} | "
                f"{by_seg['dev']:+.2f} | {by_seg['test']:+.2f} | "
                f"{by_seg['full']:+.2f} |")
            print(f"  [robust] {label} cost={cost} "
                  f"Σ={sum(c['net'] for c in cells):+.2f} worst={worst:+.2f}",
                  flush=True)

    report = ROOT / "reports" / "lock_frac_sweep.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"-> {report}", flush=True)


if __name__ == "__main__":
    main()
