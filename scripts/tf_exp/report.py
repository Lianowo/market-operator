"""报告与明细输出(接口 I-9;spec 5.4 / design 2.2.2 I-9)。

产物落位 reports/tf_trigger_exp/<run_id>/(run_id = YYYYmmddTHHMMSSZ_
config_sha8,时间戳归档不覆盖;临时目录 → 原子 rename):
- matrix.md:矩阵总表/分年度/分 TF/错失利润/T×F 边际效应/三问裁决/审计头/口径附录;
- matrix.json:ArmResult 与 Verdict 全量结构化(报告真源);
- trades_<arm_id>__<segment>[_cost<x10>].csv:引擎列全集 + 实验归属追加列;
- rejected_<arm_id>.csv:门控拒绝记录;
- anchor.json:锚定格点与模式。
"""

from __future__ import annotations

import csv
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from tf_exp.models import ArmResult, ArmRunResult, Verdict


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git_commit_hash(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True,
            text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def make_run_id(config_sha256: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{config_sha256[:8]}"


@dataclass
class AuditHeader:
    git_commit: str = "unknown"
    experiment_config_sha256: str = ""
    manifest_sha256: str = ""
    parent_config_sha256: str = ""
    run_started_at: str = ""
    reproduce_cmd: str = ""
    test_exposure_seq: object = None      # int | "未消耗" | list
    zero_diff_assertion: str = "PASS"
    insufficients: list = field(default_factory=list)
    symbols: tuple = ()
    data_span: str = "2024-10-16 → 2026-10-19 (UTC, 733 天)"

    def as_dict(self) -> dict:
        return {
            "git_commit": self.git_commit,
            "experiment_config_sha256": self.experiment_config_sha256,
            "manifest_sha256": self.manifest_sha256,
            "parent_config_sha256": self.parent_config_sha256,
            "run_started_at": self.run_started_at,
            "reproduce_cmd": self.reproduce_cmd,
            "test_exposure_seq": self.test_exposure_seq,
            "zero_diff_assertion": self.zero_diff_assertion,
            "insufficient_cells": self.insufficients,
            "symbols": list(self.symbols),
            "data_span": self.data_span,
        }


@dataclass
class ReportBundle:
    run_id: str
    experiment_id: str
    cells: list                          # list[ArmResult]
    verdicts: list                       # list[Verdict]
    missed: list                         # list[MissedProfitReport]
    audit: AuditHeader
    marginal: dict = field(default_factory=dict)
    partial: bool = False                # --arms 子集 ⇒ PARTIAL 禁裁决
    anchor: dict = field(default_factory=dict)
    year_bounds_ms: tuple = (0, 0)
    sanity_reference: dict = field(default_factory=dict)
    gate_coverage: dict = field(default_factory=dict)
    test_used_arms: list = field(default_factory=list)


_FMT = {"net_pnl": "{:+.1f}", "return_pct": "{:+.2%}",
        "max_dd_pct": "{:.2f}", "win_rate": "{:.3f}",
        "profit_factor": "{:.2f}", "exp_r": "{:+.3f}",
        "avg_win_r": "{:+.2f}", "avg_loss_r": "{:+.2f}"}


def _fmt(v, key: str) -> str:
    if v is None:
        return "NA"
    if isinstance(v, str):
        return v
    f = _FMT.get(key)
    return f.format(v) if f else str(v)


def _cell_rows(cells: list, **filt) -> list:
    out = []
    for c in cells:
        ok = True
        for k, v in filt.items():
            if getattr(c, k) != v:
                ok = False
                break
        if ok:
            out.append(c)
    out.sort(key=lambda c: (c.arm_id, c.segment, c.cost_mult, c.tf, c.year))
    return out


def _md_table(header: list, rows: list) -> str:
    if not rows:
        return "(无数据)\n"
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines) + "\n"


def _verdict_section(v: Verdict) -> str:
    lines = [f"### {v.question_id}:【{v.verdict}】"]
    lines.append("")
    lines.append(f"- 结论依据:{v.rationale}")
    lines.append(f"- decided_at: {v.decided_at}")
    lines.append(f"- config_sha256: `{v.config_sha256}`")
    if v.test_exposure_seq is not None:
        lines.append(f"- test 曝光序号: {v.test_exposure_seq}")
    hdr = ["arm", "segment", "cost", "tf", "year", "N", "net", "PF",
           "MaxDD%", "suff"]
    rows = []
    for (a, s, c, t, y) in v.supporting_cells:
        cell = next((x for x in _ctx_cells if
                     x.arm_id == a and x.segment == s
                     and abs(x.cost_mult - c) < 1e-9 and x.tf == t
                     and x.year == y), None)
        if cell is not None:
            rows.append([a, s, c, t, y, cell.n_trades,
                         _fmt(cell.net_pnl, "net_pnl"),
                         _fmt(cell.profit_factor, "profit_factor"),
                         _fmt(cell.max_dd_pct, "max_dd_pct"),
                         cell.sufficiency])
    lines.append("")
    lines.append(_md_table(hdr, rows))
    return "\n".join(lines) + "\n"


_ctx_cells: list = []          # _verdict_section 的格点查找上下文


def render_report(bundle: ReportBundle, out_dir: Path) -> None:
    """渲染 matrix.md + matrix.json(报告真源)。"""
    global _ctx_cells
    _ctx_cells = bundle.cells
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cells_json = []
    for c in bundle.cells:
        cells_json.append({
            "arm_id": c.arm_id, "segment": c.segment,
            "cost_mult": c.cost_mult, "tf": c.tf, "year": c.year,
            "net_pnl": c.net_pnl, "return_pct": c.return_pct,
            "max_dd_pct": c.max_dd_pct, "n_trades": c.n_trades,
            "win_rate": c.win_rate, "profit_factor": c.profit_factor,
            "exp_r": c.exp_r, "avg_win_r": c.avg_win_r,
            "avg_loss_r": c.avg_loss_r,
            "missed_profit_usdt": c.missed_profit_usdt,
            "avoided_loss_usdt": c.avoided_loss_usdt,
            "missed_mfe_r_median": c.missed_mfe_r_median,
            "reject_rate": c.reject_rate, "sufficiency": c.sufficiency,
            "anchor_check": c.anchor_check})

    verdicts_json = [{
        "question_id": v.question_id, "verdict": v.verdict,
        "supporting_cells": [list(x) for x in v.supporting_cells],
        "rationale": v.rationale,
        "test_exposure_seq": v.test_exposure_seq,
        "decided_at": v.decided_at, "config_sha256": v.config_sha256}
        for v in bundle.verdicts]

    missed_json = [{
        "arm_id": m.arm_id, "baseline_arm_id": m.baseline_arm_id,
        "segment": m.segment, "cost_mult": m.cost_mult,
        "missed_profit_usdt": m.missed_profit_usdt,
        "avoided_loss_usdt": m.avoided_loss_usdt,
        "n_rejected": m.n_rejected,
        "missed_mfe_r_median": m.missed_mfe_r_median,
        "reject_rate": m.reject_rate, "pairing_rate": m.pairing_rate,
        "n_unpaired": len(m.unpaired)}
        for m in bundle.missed]

    (out_dir / "matrix.json").write_text(json.dumps({
        "run_id": bundle.run_id, "experiment_id": bundle.experiment_id,
        "partial": bundle.partial, "audit": bundle.audit.as_dict(),
        "cells": cells_json, "verdicts": verdicts_json, "missed": missed_json,
        "marginal_effects": bundle.marginal, "anchor": bundle.anchor,
        "year_bounds_ms": list(bundle.year_bounds_ms),
        "sanity_reference": bundle.sanity_reference,
        "gate_coverage": bundle.gate_coverage,
        "test_used_arms": bundle.test_used_arms,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    md: list[str] = []
    a = bundle.audit
    md.append(f"# tf_trigger_exp 实验矩阵报告({bundle.run_id})")
    md.append("")
    if bundle.partial:
        md.append("> **PARTIAL**:本轮为 --arms 调试子集,禁止裁决产出"
                  "(以下裁决章节仅在全量轮有效)。")
        md.append("")
    md.append("## 审计头")
    md.append("")
    md.append(_md_table(
        ["字段", "值"],
        [["git commit", a.git_commit],
         ["实验配置 sha256", a.experiment_config_sha256],
         ["数据 manifest sha256", a.manifest_sha256],
         ["parent config sha256", a.parent_config_sha256],
         ["运行时间戳", a.run_started_at],
         ["复现命令", a.reproduce_cmd],
         ["test 曝光序号", str(a.test_exposure_seq)],
         ["零 diff 断言", a.zero_diff_assertion],
         ["INSUFFICIENT 格数", len(a.insufficients)],
         ["品种", ", ".join(a.symbols)],
         ["数据范围", a.data_span]]))
    md.append("")

    md.append("## 三问裁决结论")
    md.append("")
    if bundle.partial:
        md.append("(PARTIAL 轮不产出裁决)")
        md.append("")
    else:
        for v in bundle.verdicts:
            md.append(_verdict_section(v))

    md.append("## 矩阵总表(full 段 @ 1.0x,跨 TF 汇总)")
    md.append("")
    rows = []
    for c in _cell_rows(bundle.cells, segment="full", cost_mult=1.0,
                        tf="ALL", year="ALL"):
        rows.append([c.arm_id, c.n_trades, _fmt(c.net_pnl, "net_pnl"),
                     _fmt(c.profit_factor, "profit_factor"),
                     _fmt(c.max_dd_pct, "max_dd_pct"),
                     _fmt(c.win_rate, "win_rate"),
                     _fmt(c.exp_r, "exp_r"), c.sufficiency])
    md.append(_md_table(["arm", "N", "net(USDT)", "PF", "MaxDD%",
                         "WR", "gR", "suff"], rows))
    md.append("")

    md.append("## 分年度表(full @ 1.0x,跨 TF 汇总)")
    md.append("")
    by_arm: dict = {}
    for c in _cell_rows(bundle.cells, segment="full", cost_mult=1.0,
                        tf="ALL"):
        if c.year != "ALL":
            by_arm.setdefault(c.arm_id, {})[c.year] = c.net_pnl
    rows = [[arm, _fmt(by_arm.get(arm, {}).get("2024"), "net_pnl"),
             _fmt(by_arm.get(arm, {}).get("2025"), "net_pnl"),
             _fmt(by_arm.get(arm, {}).get("2026"), "net_pnl")]
            for arm in sorted(by_arm)]
    md.append(_md_table(["arm", "2024", "2025", "2026"], rows))
    md.append("")

    md.append("## 分 TF 表(full @ 1.0x)")
    md.append("")
    rows = []
    for c in _cell_rows(bundle.cells, segment="full", cost_mult=1.0,
                        year="ALL"):
        if c.tf != "ALL":
            rows.append([c.arm_id, c.tf, c.n_trades,
                         _fmt(c.net_pnl, "net_pnl"),
                         _fmt(c.profit_factor, "profit_factor"),
                         c.sufficiency])
    md.append(_md_table(["arm", "tf", "N", "net(USDT)", "PF", "suff"], rows))
    md.append("")

    md.append("## 错失利润表(门控臂 vs 基线,full @ 1.0x)")
    md.append("")
    rows = [[m.arm_id, m.baseline_arm_id, m.n_rejected,
             _fmt(m.missed_profit_usdt, "net_pnl"),
             _fmt(m.avoided_loss_usdt, "net_pnl"),
             _fmt(m.missed_mfe_r_median, "exp_r"),
             _fmt(m.reject_rate, "win_rate"),
             _fmt(m.pairing_rate, "win_rate"), len(m.unpaired)]
            for m in bundle.missed
            if m.segment == "full" and abs(m.cost_mult - 1.0) < 1e-9]
    md.append(_md_table(["门控臂", "基线臂", "被拒数", "错失利润",
                         "规避亏损", "MFE_R 中位", "被拒率", "配对率",
                         "未配对"], rows))
    md.append("")

    md.append("## T×F 因子边际效应表(full @ 1.0x)")
    md.append("")
    rows = []
    for lv, st in bundle.marginal.get("T", {}).items():
        rows.append(["T." + lv, st["n"],
                     _fmt(st["net_mean"], "net_pnl"),
                     _fmt(st["pf_mean"], "profit_factor")])
    for lv, st in bundle.marginal.get("F", {}).items():
        rows.append(["F." + lv, st["n"],
                     _fmt(st["net_mean"], "net_pnl"),
                     _fmt(st["pf_mean"], "profit_factor")])
    md.append(_md_table(["因子水平", "n 组合", "net 边际均值", "PF 边际均值"],
                        rows))
    md.append("")

    md.append("## 口径附录")
    md.append("")
    md.append(
        "- 指标公式(照抄 compute_metrics 口径):net=Σnet_pnl;"
        "return=net/300;胜负按 net_pnl 符号;PF=Σ赢净额/|Σ亏净额|"
        "(分母 0 ⇒ inf);gR=mean(r_multiple);avgW_R/avgL_R=mean("
        "gross_pnl/risk_usdt);MaxDD=峰值回撤(年度切片同式)。\n"
        "- 年度归属:交易按 open_decision_ts(信号 bar 时间戳,决策时刻"
        "口径);跨年持仓整体归属开仓年度;年度边界 = 2025-01-01 / "
        "2026-01-01 UTC(毫秒常量预注册)。\n"
        "- 占位约定:N=0 ⇒ WR/PF/gR/avgW/avgL 显示 NA(净额与回撤仍为"
        "数值);PF 分母 0 ⇒ inf。\n"
        "- test 段与 2026 段高度重叠(dev_frac=0.7 切分点约落在 2026 年"
        "初附近);报告如实声明该重叠,分年度属 full 段描述性细分、不构成"
        " test 段择优。\n"
        "- 计价基准:equity0=300 USDT 固定基准;不使用 v21.json 的 "
        "small_account_compounding 子树(104U 阶梯复利),与在册 v21 数字"
        "口径差异声明。\n"
        "- 品种差异面:parent(v21.json)symbols 含 DOGEUSDT,但 data/ 无 "
        "DOGE 历史回测数据;本轮以 3 品种(BTC/ETH/SOL)运行,与在册 4 品种"
        "数字不可逐位对账(决策 D6:首轮自锚登记)。\n"
        "- test 曝光纪律:A0 锚定格点的 test@1.0x/1.5x 重放为基线登记"
        "(v21 4 品种 test 数字已在册曝光),不计为择优曝光;候选 winner 的"
        "test 曝光自 25 起续号。\n")
    if bundle.sanity_reference:
        md.append("- 在册数字 sanity 对照(量级/方向,不作 fail-fast、"
                  "不入裁决依据):\n")
        for k, vv in bundle.sanity_reference.items():
            md.append(f"  - {k}: {vv}\n")
    if bundle.gate_coverage:
        md.append("- B2 状态切换覆盖率(各战役):\n")
        for k, vv in bundle.gate_coverage.items():
            md.append(f"  - {k}: {json.dumps(vv, ensure_ascii=False)}\n")

    (out_dir / "matrix.md").write_text("\n".join(md), encoding="utf-8")


def _cost_suffix(cost_mult: float) -> str:
    if abs(cost_mult - 1.0) < 1e-9:
        return ""
    return f"_cost{round(cost_mult * 10):d}"


def write_trades_csv(runs, out_dir: Path, experiment_id: str,
                     year_bounds_ms: tuple) -> None:
    """逐臂 trades CSV(引擎列全集 + 实验归属追加列)+ 拒绝记录 CSV。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _year(ts: int) -> str:
        if ts < year_bounds_ms[0]:
            return "2024"
        if ts < year_bounds_ms[1]:
            return "2025"
        return "2026"

    for run in runs:
        fname = f"trades_{run.arm_id}__{run.segment}" \
                f"{_cost_suffix(run.cost_mult)}.csv"
        cols_extra = ["exp_id", "arm_id", "series", "segment", "year",
                      "gate_rejected"]
        if run.trades:
            base_cols = list(run.trades[0].keys())
            with open(out_dir / fname, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(base_cols + cols_extra)
                for t in run.trades:
                    row = [t.get(c) for c in base_cols]
                    row += [experiment_id, run.arm_id, run.series,
                            run.segment, _year(int(t["open_decision_ts"])),
                            ""]
                    w.writerow(row)
        else:
            with open(out_dir / fname, "w", newline="", encoding="utf-8") as f:
                f.write(f"# N=0(臂 {run.arm_id} 段 {run.segment} 成本 "
                        f"{run.cost_mult}x 零成交,如实输出不重试)\n")
                f.write(",".join(cols_extra) + "\n")
        if run.rejected:
            rname = f"rejected_{run.arm_id}__{run.segment}" \
                    f"{_cost_suffix(run.cost_mult)}.csv"
            with open(out_dir / rname, "w", newline="",
                      encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "tf", "bar_ts", "event_id",
                            "direction", "gate_kind", "gate_state",
                            "reason", "evidence"])
                for r in run.rejected:
                    sym, tf, ts, eid, d = r.signal_key
                    w.writerow([sym, tf, ts, eid, d, r.gate_kind,
                                r.gate_state, r.reason,
                                json.dumps(r.evidence, ensure_ascii=False,
                                           sort_keys=True)])


def write_anchor_json(anchor: dict, out_dir: Path) -> None:
    (Path(out_dir) / "anchor.json").write_text(
        json.dumps(anchor, ensure_ascii=False, indent=2), encoding="utf-8")


def atomic_archive(tmp_dir: Path, final_dir: Path) -> Path:
    """临时目录 → 原子 rename(append-only,不覆盖历史)。"""
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        raise RuntimeError(f"产物目录已存在(append-only 违约): {final_dir}")
    final_dir.rename(tmp_dir)
    return final_dir


__all__ = ["render_report", "write_trades_csv", "write_anchor_json",
           "atomic_archive", "AuditHeader", "ReportBundle", "make_run_id",
           "git_commit_hash"]
