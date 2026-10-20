"""canonical 合并封顶语义基准重冻结(capped-v1,research-log #17)。

背景:#15 区域合并封顶(merge_max_atr)修复巨区吞细结构的账面缺陷,代价是
区域生命周期改变 ⇒ E7(FLIP)等事件计数与 v1/v2/event_study_v3 历史冻结产物
(默认无界)语义性分叉,run_backtest_v6.py 的 cross_checks 无法再以历史
产物为硬对账锚。本脚本在 canonical(merge_max_atr>0)语义下重放事件检测,
把逐 symbol×TF 的 E1/E6/E7 双向计数与 E7 fired/passed_f1 重冻结为
config/canonical_baseline_counts.json;此后 canonical 回放的硬对账基准即该
文件(merge_max_atr=0 的 legacy 路径仍对账历史冻结产物,一字不变)。

用法:
  python scripts/freeze_canonical_baseline.py
      生成/覆写 config/canonical_baseline_counts.json(仅 canonical 语义,
      merge_max_atr 必须 >0,否则拒绝)。
  python scripts/freeze_canonical_baseline.py --legacy-verify
      重冻结前置审计:以 merge_max_atr=0(历史无界)重放并逐项比对 v1 parity
      (go_events)、v2 e7_counts(1h)、event_study_v3(多 TF)。全部一致
      ⇒ 计数分叉仅来自 merge cap 效应;有任何不一致 ⇒ 真 bug,拒绝继续。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_backtest_v6 as rb                              # noqa: E402
from market_operator.structure import Params              # noqa: E402

BASELINE_PATH = ROOT / "config" / "canonical_baseline_counts.json"


def _build_counts(merge_max_atr: float) -> tuple:
    """按给定 merge_max_atr 重放 canonical 数据并逐格计数(含全部对齐断言)。"""
    raw_cfg = json.loads(rb.CONFIG_CANONICAL.read_text(encoding="utf-8"))
    params = dataclasses.replace(Params.from_json(rb.CONFIG_CANONICAL),
                                 merge_max_atr=merge_max_atr)
    frames, _ = rb.prepare_frames(raw_cfg, params, list(raw_cfg["tf_list"]))
    counts, atr_equal = rb.campaign_event_counts(frames, float(raw_cfg["k_is"]))
    return raw_cfg, counts, atr_equal


def legacy_verify() -> int:
    """merge_max_atr=0 重放 == v1/v2/event_study_v3 冻结产物(逐项)。"""
    raw_cfg, counts, atr_equal = _build_counts(0.0)
    bt_v2 = json.loads(rb.BT_V2_PATH.read_text(encoding="utf-8"))
    bt_v1 = json.loads(rb.BT_V1_PATH.read_text(encoding="utf-8"))
    es_v3 = json.loads(rb.ES_V3_PATH.read_text(encoding="utf-8"))
    ok = atr_equal
    e1e6_short_total = 0
    print(f"[legacy-verify] merge_max_atr=0(历史无界)重放,逐项对账冻结产物:")
    for key in sorted(counts):
        cell = counts[key]
        sym, tf = key.rsplit("@", 1)
        if tf == "1h":
            v2c = bt_v2["e7_counts"][sym]
            frozen = f"{v2c['whitelist_events']}/{v2c['passed_f1']}"
            src = "backtest_v2"
            e1e6_short_total += cell["E1_short"] + cell["E6_short"]
        else:
            es_c = es_v3["timeframes"][tf]["symbols"][sym]
            frozen = f"{es_c['n_events_fired']}/{es_c['n_passed_f1']}"
            src = "event_study_v3"
        replay = f"{cell['E7_fired']}/{cell['E7_passed_f1']}"
        match = replay == frozen
        ok = ok and match
        print(f"  {key:16s} E7 fired/passed_f1 回放={replay:>10s} "
              f"{src} 冻结={frozen:>10s} {'== OK' if match else '!= MISMATCH'}")
    v1_go = bt_v1["runs"]["full@1.0x"]["metrics"]["counters"]["go_events"]
    match = e1e6_short_total == v1_go
    ok = ok and match
    print(f"  E1+E6|short(1h 合计) 回放={e1e6_short_total} "
          f"backtest.json go_events={v1_go} {'== OK' if match else '!= MISMATCH'}")
    print(f"  E7 attrs['atr'] == 认知缓存同 bar ATR: {atr_equal}")
    verdict = ("全部一致 ⇒ 计数分叉仅来自 merge cap 语义" if ok
               else "存在不一致 ⇒ 真 bug,停止重冻结!")
    print(f"[legacy-verify] 结论: {verdict}", flush=True)
    return 0 if ok else 1


def freeze() -> None:
    raw_cfg = json.loads(rb.CONFIG_CANONICAL.read_text(encoding="utf-8"))
    merge_max_atr = float(raw_cfg.get("merge_max_atr", 0.0))
    if merge_max_atr <= 0:
        raise SystemExit("canonical 配置 merge_max_atr 必须 >0 才可冻结 capped "
                         "基准(legacy 语义的对账对象仍是历史冻结产物)")
    _, counts, atr_equal = _build_counts(merge_max_atr)
    if not atr_equal:
        raise SystemExit("E7 attrs['atr'] != 认知缓存同 bar ATR(F1 口径漂移),"
                         "拒绝冻结")
    manifest_bytes = (ROOT / "data" / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    cells = {key: counts[key] for key in sorted(counts)}
    payload = {
        "version": "capped-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": {
            "command": "python scripts/freeze_canonical_baseline.py",
            "script": "scripts/freeze_canonical_baseline.py",
            "config_file": "config/canonical.json",
            "config_version": raw_cfg.get("version"),
            "merge_max_atr": merge_max_atr,
            "k_is": float(raw_cfg["k_is"]),
            "symbols": list(raw_cfg["symbols"]),
            "tf_list": list(raw_cfg["tf_list"]),
            "data_manifest": {
                "path": "data/manifest.json",
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "n_files": len(manifest["files"]),
                "files_sha256": {name: ent["sha256"] for name, ent in
                                 sorted(manifest["files"].items())},
            },
        },
        "semantics_note": (
            "canonical 合并封顶语义(merge_max_atr>0,research-log #15)下的事件"
            "计数基准;与 v1/v2/event_study_v3(默认无界)历史冻结产物语义性"
            "分叉,不可混用 —— run_backtest_v6.py cross_checks 在 "
            "merge_max_atr>0 时仅以本文件为硬对账对象(research-log #17)。"),
        "totals": {
            "e1e6_short_1h": sum(c["E1_short"] + c["E6_short"]
                                 for k, c in cells.items() if k.endswith("@1h")),
            "E7_fired": sum(c["E7_fired"] for c in cells.values()),
            "E7_passed_f1": sum(c["E7_passed_f1"] for c in cells.values()),
        },
        "cells": cells,
    }
    BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1)
                             + "\n", encoding="utf-8")
    print(f"[freeze] merge_max_atr={merge_max_atr} 逐格计数(E7 fired/passed_f1,"
          f"E1/E6/E7 双向见文件):")
    for key in sorted(cells):
        c = cells[key]
        print(f"  {key:16s} E7={c['E7_fired']}/{c['E7_passed_f1']:<5d} "
              f"E1={c['E1_long']}/{c['E1_short']} E6={c['E6_long']}/{c['E6_short']} "
              f"E7L/S={c['E7_long']}/{c['E7_short']}")
    print(f"[freeze] totals={payload['totals']}")
    print(f"-> {BASELINE_PATH.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legacy-verify", action="store_true",
                    help="merge_max_atr=0 重放逐项对账历史冻结产物(前置审计)")
    args = ap.parse_args()
    if args.legacy_verify:
        sys.exit(legacy_verify())
    freeze()
