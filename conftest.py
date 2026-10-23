"""pytest 路径锁:**本仓库的 src 优先于任何已安装副本**。

背景(2026-10-23):环境里存在一份同名的 editable 安装指向旧快照
(``…/market-operator-main/src``)。site-packages 的 ``.pth`` 在解释器启动时就把
旧路径插进了 ``sys.path``,于是 ``import market_operator`` 拿到的是**旧实现**:
``tests/test_broker.py`` 因为旧副本没有 ``floor_step`` 而在收集期就 ImportError,
真实回归被这些假象掩盖。此处把本仓库 ``src`` 置顶,保证"测的就是当前代码"。

规则:任何脚本若要 import 本仓库,也应在导入前 ``sys.path.insert(0, ROOT/"src")``
(``tools/replay_acceptance.py`` / ``scripts/_diag_*.py`` 已是如此)。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) in sys.path:
    sys.path.remove(str(SRC))
sys.path.insert(0, str(SRC))
