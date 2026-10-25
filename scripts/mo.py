#!/usr/bin/env python3
"""仓库内 `mo` 入口:不依赖安装,直接把 `src/` 放进 sys.path。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from market_operator.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
