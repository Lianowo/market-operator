"""代理探活只记日志,不返回失败退出码。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import proxy_watch as pw                                              # noqa: E402


def test_proxy_endpoint_prefers_explicit_env():
    assert pw.proxy_endpoint({}) == ("127.0.0.1", 7897)
    assert pw.proxy_endpoint(
        {"MARKET_OPERATOR_PROXY": "http://10.0.0.8:1080"}) == ("10.0.0.8", 1080)


def test_down_proxy_is_logged_and_process_still_exits_zero(tmp_path):
    log = tmp_path / "proxy_watch.jsonl"

    def refuse(addr, timeout):
        raise OSError("down")

    row = pw.watch_once(env={}, log_path=log, connect=refuse, now_ms=5)
    assert row["ok"] is False
    assert json.loads(log.read_text(encoding="utf-8"))["port"] == 7897
    assert pw.main(["--log", str(log)]) == 0


def test_up_proxy_writes_nothing(tmp_path):
    log = tmp_path / "proxy_watch.jsonl"

    class _Sock:
        def close(self):
            return None

    row = pw.watch_once(env={}, log_path=log,
                        connect=lambda addr, timeout: _Sock(), now_ms=5)
    assert row["ok"] is True
    assert not log.exists()
