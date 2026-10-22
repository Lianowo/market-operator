"""端到端验收门:同场景、同品类回放净收益不得低于冻结基线。

默认跳过(整套回放约 2 分钟);需要判定时:

    $env:MARKET_OPERATOR_ACCEPTANCE=1; python -m pytest tests/test_replay_acceptance.py

判据由 ``tools/replay_acceptance.py`` 实现(单一来源),本测试只断言其退出码,
避免"测试里的判据"与"工具里的判据"漂移成两套。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("MARKET_OPERATOR_ACCEPTANCE") != "1",
    reason="端到端回放验收需显式开启 MARKET_OPERATOR_ACCEPTANCE=1",
)


def test_replay_not_worse_than_frozen_baseline():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "replay_acceptance.py"),
         "--quiet-prep"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "[PASS]" in proc.stdout
