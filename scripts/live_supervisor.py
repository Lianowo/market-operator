"""跨平台实盘看护(替代 OS 专用 watchdog:PowerShell 计划任务 / systemd / launchd)。

2026-10-22 事故的直接成因不是"策略错",而是**没有人对"进程没了"负责**:
唯一的存活证据是 Windows 计划任务的"上次运行结果",换台机器、换个系统就失效,
而掉线期间交易所侧的止损/止盈被打掉,本地却仍按旧状态记账。

本看护的判活口径全部来自系统自己写的心跳(`runtime/live/heartbeat.json`),
不读进程名、不查端口、不依赖任何 OS 进程管理器:

| 心跳事实                                   | 结论         | 动作 |
|--------------------------------------------|--------------|------|
| 文件不存在                                 | 没在跑       | 拉起 |
| `beat_age <= ok_seconds`                   | 正常         | 无   |
| `ok < age <= stale`                        | 迟到         | 只记录(宁可漏杀,不可错杀) |
| `status=recovering` 且 `age <= recover_max`| 掉线补做中   | 等(补做要重放几千根 bar) |
| `age > stale`                              | 卡死/进程没了| 终止残留 → 拉起(受重启预算约束) |
| 预算耗尽(含"心跳缺失"即崩)                 | 需人工       | 只告警,不再拉起 |

用法(两种都跨平台):
    python scripts/live_supervisor.py             # 常驻(推荐,自带 GC 与告警)
    python scripts/live_supervisor.py --once      # 单次(cron / 计划任务每 5 分钟)
    python scripts/live_supervisor.py --status    # 只体检,不动作
    python scripts/live_supervisor.py --once --no-start   # 只告警演练

自保:
* 单实例锁(文件锁,进程死亡即释放)⇒ 不会出现两个看护互相拉起/互相杀;
* 重启预算 + 启动宽限 ⇒ 崩溃循环时不会把系统刷成"每 30 秒重启一次";
* 拉起前后都记 `logs/supervisor.jsonl`(时间线可审计),告警走 Telegram。
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import suppress
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import heartbeat as hb                                # noqa: E402
from market_operator import ops                                            # noqa: E402
from market_operator import paths                                          # noqa: E402
from market_operator.secrets_loader import SecretsError, load_secrets      # noqa: E402

CONFIG_PATH = ROOT / "config" / "live.json"
# 运行期路径一律走 paths:legacy 仍是仓库根旧目录(行为零变化),portable 则是
# `<repo>/var/...`。看护与 `mo` CLI 必须读**同一个**心跳,否则判活会永久
# "缺失" -> 反复拉起 runner。
LIVE_DIR = paths.runtime_dir() / "live"
HB_PATH = LIVE_DIR / "heartbeat.json"
STATE_PATH = LIVE_DIR / "supervisor.json"
LOCK_PATH = LIVE_DIR / "supervisor.lock"
PID_PATH = LIVE_DIR / "supervisor.pid"
LOG_DIR = paths.log_dir()
RUNNER_LOG = LOG_DIR / "live_stdout.log"
RUNNER_ERR = LOG_DIR / "live_stderr.log"
CONSOLE_LOG = LOG_DIR / "supervisor.log"
MIRROR: Path | None = None       # 常驻模式下由 main() 指向 logs/supervisor.log

SECONDS_PER_HOUR = 3600.0
BYTES_PER_MB = 1 << 20
IO_CHUNK_BYTES = 1 << 20
LOCK_BUSY_GRACE_S = 5.0
GIVE_UP_ALERT_GAP_S = 1800.0
TERMINATE_TIMEOUT_S = 15.0
GC_TIMEOUT_S = 1800
_MISSING_AGE_SENTINEL = 1e9


def _safe_print(line: str) -> None:
    """pythonw/计划任务无控制台时 stdout 可能不可用,日志已落文件,打印失败不影响运行。"""
    with suppress(AttributeError, OSError, ValueError):
        print(line, flush=True)


def log_line(kind: str, rec: Mapping[str, Any]) -> None:
    """结构化时间线(唯一事实归档;不受 log_max_mb 截断影响)。

    常驻模式下同时把同一行追加到 `logs/supervisor.log`:计划任务/无控制台场景
    没人做重定向,自己写才不会丢;自己写也避免了 `cmd.exe ... >> supervisor.log`
    与上一实例的**独占句柄**抢占(那会让 cmd 在打开文件时直接失败,退出码 0、
    日志全空 —— 2026-10-23 排查了半天的「静默不启动」)。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = hb.now_ms()
    row = {
        "ts": now,
        "ts_utc": hb.iso_utc(now),
        "actor": "supervisor",
        "kind": kind,
        **rec,
    }
    with suppress(OSError):
        with open(LOG_DIR / "supervisor.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    line = (
        f"[supervisor] {row['ts_utc']} {kind} "
        f"{rec.get('reason') or rec.get('why') or ''}"
    )
    if MIRROR is not None:
        with suppress(OSError):
            with open(MIRROR, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    _safe_print(line)


def _build_notifier(cfg: Mapping[str, Any]) -> Any | None:
    """按配置初始化同步 Telegram 告警器;凭据缺失时记录并降级为无告警。"""
    try:
        cred_src = cfg.get("credentials_source")
        cred_path: Path | None = None
        if cred_src:
            cand = paths.resolve_existing(str(cred_src))
            cred_path = cand if cand.exists() else None
        secrets = load_secrets(cred_path, config_overrides=cfg.get("credentials"))
        from market_operator.tg import TelegramNotifier
        return TelegramNotifier(
            secrets,
            dropped_log=LOG_DIR / "sup_tg_missed.jsonl",
            async_mode=False,
        )
    except (SecretsError, OSError, KeyError) as exc:
        log_line("notify_disabled", {"why": f"{type(exc).__name__}: {exc}"})
        return None


class Supervisor:
    """一份看护实例:判定 → 动作 → 归档。所有阈值来自 config/live.json。"""

    def __init__(
        self,
        cfg: Mapping[str, Any],
        *,
        notify: bool = True,
        allow_start: bool = True,
    ) -> None:
        sup = dict(cfg.get("supervisor") or {})
        self.ok_s = float(sup.get("ok_seconds", 120))
        self.stale_s = float(sup.get("stale_seconds", 300))
        self.recover_max_s = float(sup.get("recover_max_seconds", 2700))
        self.poll_s = max(5.0, float(sup.get("poll_seconds", 30)))
        self.grace_s = float(sup.get("startup_grace_seconds", 180))
        self.starting_max_s = float(sup.get("starting_max_seconds", 1200))
        self.lock_backoff_s = float(sup.get("lock_backoff_seconds", 180))
        self.max_restarts = int(sup.get("max_restarts_per_hour", 6))
        self.gc_hours = float(sup.get("gc_hours", 24))
        self.log_max_mb = float(sup.get("log_max_mb", 32))
        self.argv = [
            str(a) for a in (
                sup.get("runner_argv")
                or ["scripts/live_runner.py", "--mode", "live"]
            )
        ]
        self.cfg: dict[str, Any] = dict(cfg)
        self.allow_start = bool(allow_start)
        self.tg: Any | None = _build_notifier(cfg) if notify else None
        self.state: dict[str, Any] = hb.read_json(STATE_PATH)
        self._last_gc = float(self.state.get("gc_at_s") or 0.0)
        self._alert_last: dict[str, float] = {}

    # -- 状态(重启预算跨进程持久化:看护自己被重启也不清空预算) ----------
    def _save_state(self) -> None:
        try:
            hb.write_atomic(STATE_PATH, json.dumps(self.state, ensure_ascii=False))
        except OSError as exc:
            log_line("state_write_error", {"why": str(exc)[:120]})

    def _restarts(self) -> list[float]:
        now = time.time()
        rows = [
            float(t) for t in (self.state.get("restarts") or [])
            if isinstance(t, (int, float))
        ]
        return [t for t in rows if now - t < SECONDS_PER_HOUR]

    def restarts_left(self) -> int:
        return max(0, self.max_restarts - len(self._restarts()))

    def _note_restart(self) -> None:
        rows = self._restarts()
        now = time.time()
        rows.append(now)
        self.state["restarts"] = rows
        self.state["last_restart_s"] = now
        self._save_state()

    def _alert(
        self,
        msg: str,
        *,
        key: str | None = None,
        gap_s: float = 0.0,
    ) -> None:
        if key and gap_s > 0.0:
            now = time.time()
            if now - self._alert_last.get(key, 0.0) < gap_s:
                return
            self._alert_last[key] = now
        log_line("alert", {"reason": msg})
        if self.tg is None:
            return
        try:
            self.tg.send_alert(msg)
        except Exception as exc:                      # 告警失败绝不阻断看护
            log_line("alert_error", {"why": str(exc)[:120]})

    # -- 判定 -------------------------------------------------------------
    def _absorb_lock_busy(self) -> None:
        """子进程因单实例锁退出(码 4)时,撤销刚才计入的重启,并退避。"""
        marker = HB_PATH.parent / "runner_lock_busy.json"
        try:
            doc = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if int(doc.get("code") or 0) != hb.EXIT_LOCK_BUSY:
            return
        try:
            ts = int(doc.get("ts_ms") or 0) / 1000.0
        except (TypeError, ValueError):
            return
        last = float(self.state.get("last_restart_s") or 0.0)
        if ts + LOCK_BUSY_GRACE_S < last:
            return
        pid = self.state.get("spawned_pid")
        if pid and hb.process_alive(int(pid)):
            return
        rows = self._restarts()
        if rows:
            rows.pop()
            self.state["restarts"] = rows
        self.state["lock_backoff_until"] = time.time() + self.lock_backoff_s
        self._save_state()
        with suppress(OSError):
            marker.unlink()
        log_line("lock_busy", {"backoff_s": self.lock_backoff_s})

    def _ensure_tg_bot(self) -> None:
        """Telegram 机器人掉了就拉起来。它只读,失败不影响 runner。"""
        try:
            if ops.tg_pid(self.cfg):
                return
            rep = ops.start_tg_bot(self.cfg, wait_s=0, sync=True)
            log_line(
                "tg_bot",
                {
                    "ok": bool(rep.get("ok") or rep.get("alive")),
                    "why": str(rep.get("why") or "")[:120],
                },
            )
        except Exception as exc:
            log_line("tg_bot_error", {"why": f"{type(exc).__name__}: {exc}"[:160]})

    def _judge_starting(self, doc: Mapping[str, Any]) -> hb.Decision | None:
        """runner 处于启动预热期且进程存活时,给足预热窗口。"""
        if str(doc.get("status") or "") != "starting":
            return None
        pid = doc.get("pid")
        try:
            started = int(doc.get("started_ms") or 0)
            ts = int(doc.get("ts_ms") or 0)
        except (TypeError, ValueError):
            started, ts = 0, 0
        now = hb.now_ms()
        age_start = (now - started) / 1000.0 if started else _MISSING_AGE_SENTINEL
        age_ts = (now - ts) / 1000.0 if ts else _MISSING_AGE_SENTINEL
        alive = bool(pid) and hb.process_alive(int(pid))
        if alive and age_start <= self.starting_max_s and age_ts <= self.stale_s:
            return hb.Decision(
                "none",
                "starting",
                f"启动预热 {age_start:.0f}s / {self.starting_max_s:.0f}s",
            )
        return None

    def judge(self) -> tuple[hb.Decision, dict[str, Any], float | None]:
        doc = hb.read_heartbeat(HB_PATH)
        age = hb.beat_age_seconds(doc)
        until = float(self.state.get("lock_backoff_until") or 0.0)
        if time.time() < until:
            return (
                hb.Decision("none", "lock_backoff", "锁冲突退避中,暂不拉起"),
                doc,
                age,
            )
        starting = self._judge_starting(doc)
        if starting is not None:
            return starting, doc, age
        # 刚拉起还没写出心跳 → 启动宽限内不下杀手(不杀"刚起来的东西")
        spawned = float(self.state.get("spawned_at_s") or 0.0)
        if spawned and time.time() - spawned < self.grace_s:
            if age is None or int(doc.get("started_ms") or 0) < int(spawned * 1000):
                return (
                    hb.Decision(
                        "none",
                        "startup",
                        f"启动宽限 {time.time() - spawned:.0f}s / {self.grace_s:.0f}s",
                    ),
                    doc,
                    age,
                )
        decision = hb.decide(
            age,
            doc.get("status"),
            ok_s=self.ok_s,
            stale_s=self.stale_s,
            recover_max_s=self.recover_max_s,
            restarts_left=self.restarts_left(),
        )
        return decision, doc, age

    # -- 动作 -------------------------------------------------------------
    def _rotate_runner_log(self) -> None:
        """日志有界:超限先 gzip 归档再截断(可以丢字节,不可以丢信息)。"""
        if self.log_max_mb <= 0 or not RUNNER_LOG.exists():
            return
        size = RUNNER_LOG.stat().st_size
        if size < self.log_max_mb * BYTES_PER_MB:
            return
        arch = LOG_DIR / f"live_stdout_{time.strftime('%Y%m%d_%H%M%S')}.log.gz"
        try:
            with open(RUNNER_LOG, "rb") as src, gzip.open(arch, "wb") as dst:
                while chunk := src.read(IO_CHUNK_BYTES):
                    dst.write(chunk)
            with open(RUNNER_LOG, "wb") as fh:      # 归档成功后才截断
                fh.write(b"")
            log_line("log_rotated", {"bytes": size, "archive": arch.name})
        except OSError as exc:
            log_line("log_rotate_error", {"why": str(exc)[:120]})

    def start_runner(self) -> int | None:
        if not self.allow_start:
            log_line(
                "would_start",
                {"why": "演练模式(--no-start)", "argv": " ".join(self.argv)},
            )
            return None
        self._rotate_runner_log()
        argv = [sys.executable, *self.argv]
        pid = hb.spawn_detached(
            argv, cwd=ROOT, stdout_path=RUNNER_LOG, stderr_path=RUNNER_ERR
        )
        self.state["spawned_at_s"] = time.time()
        self.state["spawned_pid"] = pid
        self._note_restart()
        log_line("started", {"pid": pid, "argv": " ".join(self.argv)})
        return pid

    def _kill(self, pid: int | None) -> None:
        if not pid or not hb.process_alive(int(pid)):
            return
        ok = hb.terminate_pid(int(pid), timeout=TERMINATE_TIMEOUT_S)
        log_line("terminated", {"pid": int(pid), "ok": ok})

    def gc(self) -> None:
        """顺带做存储治理(与心跳无关,失败不影响看护)。"""
        if self.gc_hours <= 0:
            return
        if time.time() - self._last_gc < self.gc_hours * SECONDS_PER_HOUR:
            return
        self._last_gc = time.time()
        self.state["gc_at_s"] = self._last_gc
        self._save_state()
        script = ROOT / "scripts" / "storage_gc.py"
        try:
            res = subprocess.run(
                [sys.executable, str(script), "--enforce"],
                cwd=str(ROOT),
                capture_output=True,
                timeout=GC_TIMEOUT_S,
            )
            tail = (res.stdout or b"").decode("utf-8", "replace").strip()[-600:]
            log_line("gc", {"rc": res.returncode, "tail": tail})
        except (OSError, subprocess.TimeoutExpired) as exc:
            log_line("gc_error", {"why": f"{type(exc).__name__}: {exc}"})

    def run_once(self, *, dry: bool = False) -> hb.Decision:
        if not dry:
            self._absorb_lock_busy()
        decision, doc, _age = self.judge()
        pid = doc.get("pid")
        # 必须先看 action:预算耗尽时 `decide()` 返回 action="give_up",而 kind 仍是
        # STALLED / MISSING —— 若先判 kind,下面两个分支会无视预算无限重启
        # (2026-10-25 实测:崩溃循环一小时重启 19 次,配置上限是 6)。
        if decision.action == "give_up":
            self._alert(
                f"实盘看护:{decision.reason};不再自动拉起,请人工介入",
                key="give_up",
                gap_s=GIVE_UP_ALERT_GAP_S,
            )
            return decision
        if decision.kind == hb.STALLED:
            if dry:
                log_line("dry_stalled", {"reason": decision.reason, "pid": pid})
            else:
                self._kill(pid)
                new_pid = self.start_runner()
                self._alert(
                    f"实盘看护:{decision.reason};旧进程 {pid} 已终止,"
                    f"新进程 {new_pid} 已拉起(掉线补做将自动对齐交易所)"
                )
        elif decision.kind == hb.MISSING:
            if dry:
                log_line("dry_missing", {"reason": decision.reason})
            else:
                new_pid = self.start_runner()
                self._alert(
                    f"实盘看护:{decision.reason};"
                    + (
                        f"已拉起 {new_pid}"
                        if new_pid
                        else "拉起失败(详见 logs/live_stderr.log)"
                    )
                )
        elif decision.kind == hb.LATE:
            log_line("late", {"reason": decision.reason, "pid": pid})
        if not dry and self.allow_start:
            self._ensure_tg_bot()
        return decision

    def status_text(self) -> str:
        decision, doc, age = self.judge()
        return "\n".join([
            f"心跳文件 : {HB_PATH}",
            f"心跳年龄 : {'n/a' if age is None else f'{age:.0f}s'}",
            f"判定     : {decision.kind} / {decision.action} ({decision.reason})",
            f"pid/mode : {doc.get('pid')} / {doc.get('mode')}",
            f"状态     : {doc.get('status')} (steps={doc.get('step_count')},"
            f" 补做 {doc.get('gap_bars')} bars)",
            f"持仓/挂单: {doc.get('positions')} / {doc.get('stops')}",
            f"权益     : {doc.get('equity')}",
            f"重启预算 : {self.restarts_left()}/{self.max_restarts} 次/小时",
        ])

    def run_daemon(self) -> None:               # pragma: no cover - 常驻循环
        log_line(
            "daemon_up",
            {"poll_s": self.poll_s, "argv": " ".join(self.argv)},
        )
        self._alert(
            f"实盘看护已起动(每 {self.poll_s:.0f}s 判活;"
            f"心跳 >{self.stale_s:.0f}s 即重启)"
        )
        while True:
            try:
                self.run_once()
                self.gc()
            except Exception as exc:               # 看护自己绝不能死
                log_line("tick_error", {"why": f"{type(exc).__name__}: {exc}"})
            time.sleep(self.poll_s)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="跨平台实盘看护(心跳判活)")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--once", action="store_true", help="只执行一次判定+动作")
    ap.add_argument("--status", action="store_true", help="只打印体检结果")
    ap.add_argument(
        "--no-start",
        action="store_true",
        help="演练:判定照做但不拉起进程",
    )
    ap.add_argument("--no-notify", action="store_true", help="不发 Telegram")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sup = Supervisor(cfg, notify=not args.no_notify, allow_start=not args.no_start)
    if args.status:
        _safe_print(sup.status_text())
        return 0
    if args.once:
        sup.run_once(dry=args.no_start)
        _safe_print(sup.status_text())
        return 0
    lock = hb.SingletonLock(LOCK_PATH)
    if not lock.acquire():
        _safe_print("[supervisor] 已有看护实例在运行(单实例锁被占用),本次退出")
        return 0
    # 自己登记 pid:`mo status` / `mo stop` 对"非 mo 启动"的看护也要认得出来
    # (迁移前只有 `mo start` 会写这个文件,手工/计划任务启动的看护在 CLI 里是隐形的)。
    with suppress(OSError):
        hb.write_atomic(PID_PATH, str(os.getpid()))
    global MIRROR
    MIRROR = CONSOLE_LOG          # 常驻模式:console 行同时落 logs/supervisor.log
    try:
        sup.run_daemon()
    except BaseException:                       # 无控制台时 stderr 会丢,自己留证
        log_line("crash", {"why": traceback.format_exc()[-1500:]})
        raise
    finally:
        with suppress(OSError):
            PID_PATH.unlink()
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
