"""`python -m market_operator` 等价于 `mo`。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":                        # pragma: no cover
    sys.exit(main())
