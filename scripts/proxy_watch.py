"""代理探活。只写日志,不碰交易进程。

默认探测 ``127.0.0.1:7897``。若设置了 ``MARKET_OPERATOR_PROXY`` 或
``https_proxy``,改探那个地址。连不上就在 ``logs/proxy_watch.jsonl`` 留一行,
退出码始终是 0:计划任务失败不得被理解成要重启 runner。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from market_operator import paths                                       # noqa: E402


def proxy_endpoint(env: Mapping[str, str] | None = None) -> tuple[str, int]:
    src = env if env is not None else os.environ
    raw = (
        src.get("MARKET_OPERATOR_PROXY")
        or src.get("https_proxy")
        or src.get("HTTPS_PROXY")
        or "http://127.0.0.1:7897"
    )
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    return parsed.hostname or "127.0.0.1", int(parsed.port or 7897)


def probe(
    host: str,
    port: int,
    *,
    timeout: float = 2.0,
    connect: Callable[[tuple[str, int], float], Any] | None = None,
) -> bool:
    opener = connect or socket.create_connection
    try:
        sock = opener((host, port), timeout)
    except OSError:
        return False
    with suppress(OSError):
        sock.close()
    return True


def watch_once(
    *,
    env: Mapping[str, str] | None = None,
    log_path: Path | None = None,
    connect: Callable[[tuple[str, int], float], Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    host, port = proxy_endpoint(env)
    ok = probe(host, port, connect=connect)
    row: dict[str, Any] = {
        "ts_ms": int(now_ms if now_ms is not None else time.time() * 1000),
        "host": host,
        "port": port,
        "ok": ok,
    }
    if log_path is not None and not ok:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="proxy_watch")
    ap.add_argument("--log", default=str(paths.log_dir() / "proxy_watch.jsonl"))
    args = ap.parse_args(argv)
    watch_once(log_path=Path(args.log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
