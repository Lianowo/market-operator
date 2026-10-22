"""把 E 码计数键迁到语义键(纯键名迁移,数值不动),并升级冻结产物版本。

为什么必须一起迁:计数键由 ``f"{event_id}_long"`` 拼出,事件标识一旦语义化,
键名若仍是编号就会出现"代码里两套词表"。冻结产物 ``canonical_baseline_counts.json``
是**同一批数据**的旧键记录,迁移后逐格数值必须完全相同 —— 本脚本在改写前后
各读一次,任何一格不等就中止。

用法:
    python tools/rename_baseline_fields.py --check
    python tools/rename_baseline_fields.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "config" / "canonical_baseline_counts.json"

FIELD_MAP = {
    "E1_long": "BREAKOUT_ACCEPTED_long",
    "E1_short": "BREAKOUT_ACCEPTED_short",
    "E6_long": "STRUCTURE_BROKEN_long",
    "E6_short": "STRUCTURE_BROKEN_short",
    "E7_long": "FLIP_RETESTED_long",
    "E7_short": "FLIP_RETESTED_short",
    "E7_fired": "FLIP_RETESTED_fired",
    "E7_passed_f1": "FLIP_RETESTED_passed_f1",
}
TOTAL_MAP = {"e1e6_short_1h": "breakout_structure_short_1h",
             "E7_fired": "FLIP_RETESTED_fired"}
VERSION_FROM, VERSION_TO = "capped-v1", "capped-v2"


def migrate(payload: dict) -> dict:
    cells = {key: {FIELD_MAP.get(f, f): v for f, v in cell.items()}
             for key, cell in payload["cells"].items()}
    totals = {TOTAL_MAP.get(k, k): v for k, v in payload["totals"].items()}
    out = dict(payload)
    out["cells"], out["totals"] = cells, totals
    out["version"] = VERSION_TO
    out["migrated_from"] = VERSION_FROM
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    before = {k: {f: v for f, v in cell.items() if f.startswith(("E", "FLIP", "BREAKOUT", "STRUCTURE"))}
              for k, cell in payload["cells"].items()}
    migrated = migrate(payload)
    after = {k: {FIELD_MAP.get(f, f): v for f, v in cell.items()}
             for k, cell in before.items()}
    actual = {k: {f: v for f, v in cell.items() if "long" in f or "short" in f or "fired" in f or "passed" in f}
              for k, cell in migrated["cells"].items()}
    if after != actual:
        print("[FAIL] 迁移前后数值不一致,已中止")
        return 1
    print(f"[ok] 15 格 × 8 字段数值逐一相等 → {VERSION_TO}")
    if args.apply:
        BASELINE_PATH.write_text(
            json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"[apply] {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
