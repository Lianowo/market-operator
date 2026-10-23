"""回放验收:同一场景、同一品类,新实现不得差于基线。

判据(用户口径):重写后的**同场景同品类回放** ≥ 当前实现。
因此本工具不是"金标准比对",而是**逐品类下界断言**:

- 场景(scenario)= 品种集 × 段(dev/test/full)× 成本倍数;
- 品类(category)= 认知周期(2h/4h/12h/1d)与事件族;
- 断言:场景净收益与每个品类净收益 ≥ 基线(容差 1e-6,纯浮点);
  逐笔成交数、胜率、盈亏比、回撤只报告,不参与否决(允许以更少交易取得更高收益)。

用法:
    python tools/replay_acceptance.py --capture     # 冻结当前实现为基线
    python tools/replay_acceptance.py               # 与基线比较(退出码 = 判定)
    python tools/replay_acceptance.py --scenario explore --segments dev

基线文件只存**事实与指标**,不存实现细节,因此可跨重构长期复用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.scenario import (  # noqa: E402
    COGNITION_TIMEFRAMES,
    ScenarioOutcome,
    ScenarioRunner,
)

BASELINE_PATH = ROOT / "tests" / "fixtures" / "replay_baseline.json"
EPS = 1e-6

#: 场景目录:品种集是唯一的身份,段与成本倍数在运行时指定。
SCENARIOS: dict[str, tuple[str, ...]] = {
    "explore": ("BTCUSDT", "ETHUSDT", "SOLUSDT"),
    "book4": ("BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT"),
}

#: 逐笔摘要字段(顺序固定 = 摘要可复现)。
TRADE_DIGEST_FIELDS = (
    "symbol", "tf", "event_id", "direction", "route", "entry_mode",
    "open_decision_ts", "open_ts", "entry", "exit_ts", "exit_price",
    "notional", "risk_usdt", "net_pnl", "reason_exit", "closed",
)


def digest(outcome: ScenarioOutcome) -> str:
    """逐笔事实的稳定摘要:顺序无关(先排序),数值定精度(6 位)。"""
    rows = []
    for trade in outcome.in_segment_trades:
        rows.append("|".join(_cell(trade.get(k)) for k in TRADE_DIGEST_FIELDS))
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def _cell(value) -> str:
    if value is None:
        return "~"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def summarize(outcome: ScenarioOutcome) -> dict:
    """场景指标:净收益是唯一否决量,其余为诊断量。"""
    closed = outcome.closed_trades
    gross = sum(t["gross_pnl"] for t in closed)
    net = sum(t["net_pnl"] for t in closed)
    cost = sum(t.get("fees", 0.0) + t.get("funding", 0.0)
               + t.get("slippage_cost", 0.0) for t in closed)
    wins = [t for t in closed if t["gross_pnl"] > 0]
    gain = sum(t["gross_pnl"] for t in wins)
    loss = -sum(t["gross_pnl"] for t in closed if t["gross_pnl"] <= 0)
    peak, drawdown = float("-inf"), 0.0
    for row in outcome.equity_rows:
        equity = float(row[1])
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)

    def _bucket(key) -> dict:
        buckets: dict = {}
        for trade in closed:
            name = key(trade)
            cell = buckets.setdefault(name, {"n": 0, "net": 0.0})
            cell["n"] += 1
            cell["net"] += trade["net_pnl"]
        return {name: {"n": cell["n"], "net": round(cell["net"], 4)}
                for name, cell in sorted(buckets.items())}

    return {
        "key": outcome.spec.key,
        "n": len(closed),
        "gross": round(gross, 4),
        "cost": round(cost, 4),
        "net": round(net, 4),
        "winrate": round(len(wins) / len(closed), 4) if closed else 0.0,
        "profit_factor": round(gain / loss, 4) if loss > 0 else None,
        "max_drawdown_pct": round(drawdown * 100.0, 4),
        "by_timeframe": _bucket(lambda t: t.get("tf")),
        "by_event": _bucket(lambda t: t.get("event_id")),
        "by_symbol": _bucket(lambda t: t.get("symbol")),
        "counters": {k: v for k, v in outcome.counters.items() if v},
        "digest": digest(outcome),
        "equity_samples": _equity_samples(outcome),
    }


def _equity_samples(outcome: ScenarioOutcome) -> list:
    """净值曲线抽样(起点/中点/终点):只供人眼比对形态,不参与判定。"""
    rows = outcome.equity_rows
    if not rows:
        return []
    picks = {0, len(rows) // 2, len(rows) - 1}
    return [[int(rows[i][0]), round(float(rows[i][1]), 4)] for i in sorted(picks)]


def _category_key(category: str, name: str) -> str:
    """把基线里的历史键名归一到语义键名(仅事件类需要;其余原样)。"""
    if category == "by_event":
        from market_operator.events import canonical_event_id
        return canonical_event_id(name)
    return name


def compare(baseline: dict, current: dict) -> list[str]:
    """逐场景逐品类下界断言。返回失败项(空 = 通过)。

    基线可能由**旧词表**的实现冻结(事件键为历史编号),因此比较前两边都过
    同一个归一函数;净收益才是判据,键名只是对齐手段。
    """
    failures: list[str] = []
    for key, now in current.items():
        base = baseline.get(key)
        if base is None:            # 新场景:基线里没有 ⇒ 无可比,记录但不否决
            continue
        if now["net"] < base["net"] - EPS:
            failures.append(
                f"{key}: 场景净收益 {now['net']:+.4f} < 基线 {base['net']:+.4f}")
        for category in ("by_timeframe", "by_event", "by_symbol"):
            now_cells = {_category_key(category, n): c
                         for n, c in now.get(category, {}).items()}
            for name, cell in base.get(category, {}).items():
                got = now_cells.get(_category_key(category, name))
                if got is None:
                    if cell["net"] > EPS:
                        failures.append(
                            f"{key}/{category}/{name}: 品类消失"
                            f"(基线净 {cell['net']:+.4f})")
                    continue
                if got["net"] < cell["net"] - EPS:
                    failures.append(
                        f"{key}/{category}/{name}: 净 {got['net']:+.4f} "
                        f"< 基线 {cell['net']:+.4f}")
    return failures


def _run_all(scenarios: Iterable[str], segments: Iterable[str],
             cost_multipliers: Iterable[float], progress: bool,
             capacity_mode: str | None = None) -> dict:
    results: dict = {}
    for name in scenarios:
        t0 = time.time()
        runner = ScenarioRunner(SCENARIOS[name], progress=progress)
        print(f"[prep] {name} 装配完成 ({time.time() - t0:.1f}s)", flush=True)
        for segment in segments:
            for cm in cost_multipliers:
                t1 = time.time()
                outcome = runner.run(segment=segment, cost_multiplier=cm,
                                     capacity_mode=capacity_mode)
                summary = summarize(outcome)
                key = f"{name}|{segment}|{cm:g}x"
                summary["scenario"] = name
                summary["segment"] = segment
                summary["cost_multiplier"] = cm
                results[key] = summary
                print(f"[run] {key:28s} n={summary['n']:5d} "
                      f"net={summary['net']:+9.4f} "
                      f"({time.time() - t1:.1f}s)", flush=True)
    return results


def _print_table(current: dict, baseline: dict) -> None:
    header = (f"{'场景':28s} {'N':>5s} {'净收益':>10s} {'Δ净':>9s} "
              f"{'PF':>6s} {'DD%':>6s} {'逐笔':>6s}")
    print("\n" + header)
    print("-" * len(header))
    for key, now in current.items():
        base = baseline.get(key)
        delta = f"{now['net'] - base['net']:+.4f}" if base else "新建"
        pf = now["profit_factor"] if now["profit_factor"] is not None else float("nan")
        same = "一致" if base and base["digest"] == now["digest"] else "不同"
        print(f"{key:28s} {now['n']:5d} {now['net']:+10.4f} {delta:>9s} "
              f"{pf:6.2f} {now['max_drawdown_pct']:6.2f} {same:>6s}")
        if base:
            for tf in COGNITION_TIMEFRAMES:
                cell = now.get("by_timeframe", {}).get(tf)
                ref = base.get("by_timeframe", {}).get(tf)
                if cell is None and ref is None:
                    continue
                got = cell["net"] if cell else 0.0
                was = ref["net"] if ref else 0.0
                print(f"    {tf:3s} n={cell['n'] if cell else 0:5d} "
                      f"net={got:+9.4f} (基线 {was:+9.4f}, "
                      f"Δ{got - was:+.4f})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="store_true", help="冻结基线")
    parser.add_argument("--scenario", default="all", help="explore | book4 | all")
    parser.add_argument("--segments", default="dev,test,full")
    parser.add_argument("--costs", default="1.0")
    parser.add_argument("--quiet-prep", action="store_true")
    parser.add_argument("--capacity-mode", default=None,
                        choices=["full_cost", "stop_risk", "admission_full_cost"],
                        help="覆盖配置的容量口径(对照实验用)")
    args = parser.parse_args()

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    segments = [s.strip() for s in args.segments.split(",") if s.strip()]
    costs = [float(c) for c in args.costs.split(",") if c.strip()]
    current = _run_all(names, segments, costs, progress=not args.quiet_prep,
                       capacity_mode=args.capacity_mode)

    baseline: dict = {}
    if BASELINE_PATH.exists():
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        baseline = payload.get("scenarios", {})

    if args.capture:
        payload = {
            "schema": 1,
            "note": "同场景同品类回放基线:新实现净收益不得低于此表",
            "scenarios": current,
        }
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
        print(f"\n[capture] {BASELINE_PATH}")
        _print_table(current, {})
        return 0

    if not baseline:
        print(f"\n[error] 基线不存在: {BASELINE_PATH}(先 --capture)")
        return 2

    _print_table(current, baseline)
    failures = compare(baseline, current)
    if failures:
        print(f"\n[FAIL] {len(failures)} 项低于基线:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n[PASS] 全部场景与品类净收益 ≥ 基线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
