"""实盘换进程(代码更新后重启,不等看护的 stale 窗口)。

Python 不会热加载已 import 的模块:改完 `live_runner.py` / `tg.py` 必须换进程。
而"kill 掉等看护拉起"的判活口径是 `heartbeat.stale_seconds=300` —— 最长 5 分钟
盲窗、白吃一次重启预算、还会发一条"停摆"告警。本工具按看护**同一条**
`spawn_detached` 路径立刻拉起,把盲窗压到 runner 自己的启动时间(加载密钥 +
掉线补做 + 对账);交易所侧 TPSL 是常驻挂单,那几十秒仍在保护。

行为:
① 读 `runtime/live/heartbeat.json` 取 pid(与看护同一事实源,不从命令行猜);
② 打印终止前的权益/持仓/挂单数,留证;
③ `terminate_pid` → 确认死亡 → `spawn_detached`(参数与看护完全一致);
④ 轮询心跳,直到 `pid` 变成新进程且 `status` 不再是 `recovering`,打印结论。

用法:
    python scripts/live_restart.py --dry-run      # 只看快照与计划,不动进程
    python scripts/live_restart.py                # 真重启(默认最多等 120s 确认)
    python scripts/live_restart.py --no-wait      # 拉起即返回(无人值守)
    python scripts/live_restart.py --mode paper   # 换进程但跑沙盒

退出码:0 已切换且心跳确认;1 拉起失败;2 新进程未在预算内写出心跳。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import heartbeat as hb     # noqa: E402

HB_PATH = ROOT / "runtime" / "live" / "heartbeat.json"
RUNNER_LOG = ROOT / "logs" / "live_stdout.log"
RUNNER_ERR = ROOT / "logs" / "live_stderr.log"


def _read_hb() -> dict:
    try:
        return json.loads(HB_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _snapshot(doc: dict) -> str:
    if not doc:
        return "心跳文件不可读(视同没在跑)"
    return (f"pid={doc.get('pid')} mode={doc.get('mode')} "
            f"status={doc.get('status')} steps={doc.get('step_count')} "
            f"equity={doc.get('equity')} positions={doc.get('positions')} "
            f"stops={doc.get('stops')} gap_bars={doc.get('gap_bars')} "
            f"saved_at={doc.get('saved_at_utc')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="实盘换进程(改完代码后的重启)")
    ap.add_argument("--mode", default="live", choices=("live", "paper"))
    ap.add_argument("--dry-run", action="store_true", help="只看快照与计划")
    ap.add_argument("--no-wait", action="store_true", help="拉起即返回,不确认心跳")
    ap.add_argument("--wait-seconds", type=float, default=120.0,
                    help="等待新进程写心跳的上限(默认 120s)")
    ap.add_argument("--term-timeout", type=float, default=15.0,
                    help="终止旧进程的宽限(默认 15s)")
    args = ap.parse_args(argv)

    doc = _read_hb()
    old = int(doc.get("pid") or 0)
    spawn_argv = [sys.executable, "scripts/live_runner.py", "--mode", args.mode]

    print(f"仓库      : {ROOT}")
    print(f"旧进程快照: {_snapshot(doc)}")
    print(f"新进程参数: {' '.join(spawn_argv)}")
    print(f"日志      : {RUNNER_LOG} / {RUNNER_ERR}")
    if args.dry_run:
        print("--dry-run:不终止、不拉起")
        return 0

    if old and hb.process_alive(old):
        t0 = time.time()
        ok = hb.terminate_pid(old, timeout=args.term_timeout)
        print(f"终止旧进程 {old}: ok={ok} 用时 {time.time() - t0:.1f}s")
        t0 = time.time()
        while hb.process_alive(old) and time.time() - t0 < args.term_timeout + 5:
            time.sleep(0.2)
        if hb.process_alive(old):
            print(f"[FAIL] 旧进程 {old} 仍在运行,放弃拉起(避免双开下单)",
                  file=sys.stderr)
            return 1
    else:
        print("旧进程不在(或心跳缺失),直接拉起")

    new_pid = hb.spawn_detached(spawn_argv, cwd=ROOT,
                                stdout_path=RUNNER_LOG, stderr_path=RUNNER_ERR)
    print(f"新进程 pid={new_pid}")
    if not new_pid:
        print("[FAIL] spawn 失败,详见 logs/live_stderr.log", file=sys.stderr)
        return 1
    if args.no_wait:
        return 0

    deadline = time.time() + args.wait_seconds
    last = None
    while time.time() < deadline:
        doc = _read_hb()
        if doc.get("pid") == new_pid:
            last = doc
            if doc.get("status") != "recovering":
                print(f"心跳确认: {_snapshot(doc)}")
                return 0
        time.sleep(1.0)
    print(f"[WARN] {args.wait_seconds:.0f}s 内未确认新进程心跳;"
          f"最后一次: {_snapshot(last or _read_hb())}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
