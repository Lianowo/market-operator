"""事件目录语义化:把 E1..E10 这类编号称呼改成"见名知义"的事件标识。

做法分三步,每步都可核对:
1. src/ 内 ``\\bE(10|1..9)\\b`` 全量替换为语义常量名(注释、字符串值、
   标识符同时命中,保证三者不会漂移);
2. ``events.py`` 里保留**唯一**一张 ``LEGACY_EVENT_CODES`` 表,只为读取历史
   配置(config/v13.json..v21.json)与历史报告;新代码不得使用编号;
3. 白名单在配置边界一次性归一(``RouterParams``),使"旧配置 ↔ 新语义"等价,
   并且计数口径与路由口径使用同一张归一表(消除旧实现里"路由认别名、计数不认"
   的分裂)。

用法:
    python tools/rename_event_vocabulary.py --check    # 只报告
    python tools/rename_event_vocabulary.py --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "market_operator"

VOCABULARY = {
    "E1": "BREAKOUT_ACCEPTED",
    "E2": "BREAKOUT_FAILED",
    "E3": "LEVEL_REJECTED",
    "E4": "TREND_RESUMED",
    "E5": "RANGE_ROTATED",
    "E6": "STRUCTURE_BROKEN",
    "E7": "FLIP_RETESTED",
    "E8": "EDGE_RECLAIMED",
    "E9": "PROCESS_ACCEPTED",
    "E10": "PROCESS_REJECTED",
}

PATTERN = re.compile(r"\bE(?:10|[1-9])\b")


def rewrite(text: str) -> str:
    return PATTERN.sub(lambda m: VOCABULARY[m.group(0)], text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--roots", default="src",
                        help="逗号分隔:src,tests,scripts(默认只改 src)")
    args = parser.parse_args()
    targets: list[Path] = []
    for root in [r.strip() for r in args.roots.split(",") if r.strip()]:
        if root == "src":
            targets.extend(sorted(SRC.glob("*.py")))
        else:
            targets.extend(sorted((ROOT / root).glob("*.py")))
    total = 0
    for path in targets:
        text = path.read_text(encoding="utf-8")
        updated = rewrite(text)
        if updated == text:
            continue
        hits = len(PATTERN.findall(text))
        total += hits
        print(f"{path.parent.name}/{path.name}: {hits}")
        if args.apply:
            path.write_text(updated, encoding="utf-8")
    print(f"total={total} applied={args.apply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
