"""心跳与看护原语(系统级、跨平台;判断口径不含任何操作系统进程管理)。

为什么要独立成模块:
2026-10-22 的掉线事故里,进程静默消失、交易所侧止损成交、本地仍记持仓,
而"谁没了"没有任何人知道 —— 唯一的存活证据是某个 Windows 计划任务的
"最近运行结果",换个系统(Linux/macOS/容器)就完全失效。

本模块把"活着"定义成**系统自己的时间事实**,而不是机器的进程表:

* `runtime/live/heartbeat.json` 由 runner 的心跳线程每 `write_seconds` 写一次,
  与主循环解耦 —— 主循环卡死时心跳仍在前进(据此可区分"进程没了"与"卡住了");
* 文件里同时带 `loop_ts_ms`(主循环最后一次**成功**走完一步)与
  `step_started_ms`(当前步开始时刻),看护据此判断卡在哪一步;
* 文件里带 `positions_detail`(方向/数量/本地防线/在挂止损单号)。即使进程
  真的死了,看护也能凭这份快照把"只保护不主出"的止损重新挂回交易所;
* 所有时间戳都是 UTC epoch ms,跨时区、跨平台一致;不读进程名、不读端口、
  不用 psutil。

平台差异只落在两个最小原语上(`SingletonLock` 的文件锁、`spawn_detached`
的脱离方式),判定与动作全部是同一套系统级语义。
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

SCHEMA = 1
EXIT_SELF_FENCE = 3          # 看护识别"自我熔断,需要拉起"的退出码

# 心跳分类:看护与体检脚本共用这一套口径(不要各自发明阈值)
MISSING, OK, LATE, RECOVERING, STALLED = (
    "missing", "ok", "late", "recovering", "stalled")


# ---------------------------------------------------------------------------
# 时间 / 落盘
# ---------------------------------------------------------------------------


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) \
        .isoformat(timespec="seconds")


def parse_iso(text: Optional[str]) -> Optional[int]:
    """ISO8601 → epoch ms;解析失败返回 None(不猜)。"""
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(str(text)).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def write_atomic(path: Path, text: str) -> None:
    """同目录唯一临时文件 + `os.replace` 原子落盘(读者永远看到完整内容)。

    临时名带 pid 与线程号:心跳线程与主循环会同时写同一份心跳,若共用一个
    `.tmp` 名,两个写者会互相截断对方正在写的内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    if path.suffix == ".gz":
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        tmp.write_text(text, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        # 读者独占打开(资源管理器预览/编辑器)时 replace 会失败:宁可短暂
        # 失去原子性,也不能把这次落盘整个丢掉。
        if path.suffix == ".gz":
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write(text)
        else:
            path.write_text(text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        if path.suffix == ".gz":
            raw = gzip.decompress(raw)
        doc = json.loads(raw.decode("utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, EOFError):
        return {}


def read_heartbeat(path: Path) -> dict:
    doc = read_json(path)
    return doc if doc.get("schema") or doc.get("ts_ms") else {}


def beat_age_seconds(doc: dict, at_ms: Optional[int] = None) -> Optional[float]:
    """心跳年龄(秒)= 距"主循环最近一次进展"的时间。

    取 `max(loop_ts_ms, started_ms)`:
    * 正常运行 → `loop_ts_ms`(主循环每成功走完一步就前移);
    * 刚刚重启、还没走完第一步 → `started_ms`(新进程有完整的 stall 宽限,
      不会被上一具尸体留下的旧 `loop_ts_ms` 误判成卡死);
    * 两个字段都缺失(旧格式心跳)→ 回落 `ts_ms`。
    """
    if not doc:
        return None
    base = max(int(doc.get("loop_ts_ms") or 0), int(doc.get("started_ms") or 0))
    if not base:
        base = doc.get("ts_ms")
    if not base:
        base = parse_iso(doc.get("saved_at_utc"))
    if not base:
        return None
    return max(0.0, ((at_ms if at_ms is not None else now_ms())
                     - int(base)) / 1000.0)


def classify(age_s: Optional[float], status: Optional[str], *,
             ok_s: float, stale_s: float,
             recover_max_s: float = 0.0) -> str:
    """把(年龄, 状态)映射成看护口径的五档。

    `recovering` 是**被允许的长操作**(掉线补做可能要重放几千根 bar):只要
    还在 `recover_max_s` 内就继续等;超过它,说明补做本身也卡住了,按卡死处理。
    """
    if age_s is None:
        return MISSING
    if status == "recovering" and age_s <= max(0.0, recover_max_s):
        return RECOVERING
    if age_s <= max(0.0, ok_s):
        return OK
    if age_s <= max(0.0, stale_s):
        return LATE
    return STALLED


@dataclass(frozen=True)
class Decision:
    """看护动作:`none` / `warn` / `restart` / `give_up`。"""

    action: str
    kind: str
    reason: str

    @property
    def should_start(self) -> bool:
        return self.action == "restart"


def decide(age_s: Optional[float], status: Optional[str], *,
           ok_s: float, stale_s: float, recover_max_s: float = 0.0,
           restarts_left: int = 1) -> Decision:
    """唯一决策表(纯函数,便于逐档测试)。

    | 心跳            | 动作                                  |
    |-----------------|---------------------------------------|
    | 缺失            | restart(从未启动 / 文件被删) / give_up |
    | ok              | none                                  |
    | late            | warn(只记录:宁可漏杀,不可错杀)       |
    | recovering      | none(补做中,受 recover_max 约束)     |
    | stalled         | restart(重启预算内) / give_up(耗尽)   |
    """
    kind = classify(age_s, status, ok_s=ok_s, stale_s=stale_s,
                    recover_max_s=recover_max_s)
    if kind == MISSING:
        if restarts_left <= 0:
            return Decision("give_up", kind,
                            "心跳缺失且重启预算耗尽(启动即崩?)")
        return Decision("restart", kind, "心跳缺失(未在运行)")
    if kind == OK:
        return Decision("none", kind, f"心跳 {age_s:.0f}s")
    if kind == LATE:
        return Decision("warn", kind, f"心跳迟到 {age_s:.0f}s(阈值 {ok_s:.0f}s)")
    if kind == RECOVERING:
        return Decision("none", kind, f"掉线补做中 {age_s:.0f}s")
    if restarts_left <= 0:
        return Decision("give_up", kind,
                        f"心跳停摆 {age_s:.0f}s 且重启预算耗尽")
    return Decision("restart", kind, f"心跳停摆 {age_s:.0f}s")


# ---------------------------------------------------------------------------
# 单实例锁(跨平台:文件锁由内核在进程死亡时自动释放,不存在"锁残留")
# ---------------------------------------------------------------------------


class SingletonLock:
    """同一路径只允许一个活进程持有;`acquire()` 返回是否拿到。

    为什么不用 pid 文件:pid 会被复用,进程崩溃后 pid 文件会变成毒锁。
    二进制文件锁(Windows `msvcrt.locking` / POSIX `flock`)由内核在句柄关闭
    或进程死亡时释放,天然没有残留问题。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                if fh.read(1) == b"":
                    fh.write(b"0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self) -> "SingletonLock":
        if not self.acquire():
            raise RuntimeError(f"单实例锁被占用:{self.path}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ---------------------------------------------------------------------------
# 进程原语(最小的平台适配;判定逻辑不在这里)
# ---------------------------------------------------------------------------


def process_alive(pid: int) -> bool:
    """只回答"这个 pid 现在还活着吗"(不用于决策,仅用于止损/收尾日志)。"""
    if not pid or int(pid) <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                 False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)          # POSIX:信号 0 只做存在性检查
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_pid(pid: Optional[int], *, timeout: float = 10.0) -> bool:
    """终止进程:Windows 用 taskkill /F;POSIX 先 SIGTERM 再 SIGKILL。"""
    if not pid or int(pid) <= 0:
        return True
    pid = int(pid)
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=max(10.0, timeout))
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            if not process_alive(pid):
                return True
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    return not process_alive(pid)


def spawn_detached(argv: list[str], *, cwd: Path,
                   stdout_path: Path, stderr_path: Path) -> Optional[int]:
    """脱离当前会话启动子进程(看护退出/终端关闭都不影响 runner)。"""
    cwd = Path(cwd)
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    with open(stdout_path, "ab") as out, open(stderr_path, "ab") as err:
        proc = subprocess.Popen([str(a) for a in argv], cwd=str(cwd),
                                stdin=subprocess.DEVNULL, stdout=out,
                                stderr=err, close_fds=True, **kwargs)
    return proc.pid


# ---------------------------------------------------------------------------
# 心跳看护线程(在 runner 进程内,与主循环解耦)
# ---------------------------------------------------------------------------


class HeartbeatGuard(threading.Thread):
    """独立心跳线程:主循环活着时只"记账",卡死时才是"保护手"。

    职责边界(绝不越过交易链路):
    ① 每 `write_seconds` 落一次心跳 —— 即使主循环正卡在某一步;
    ② 主循环停摆超过 `stall_seconds` ⇒ 告警(限频)并记录卡在哪一步;
    ③ 停摆且持仓存在 ⇒ **只做保护**:核对交易所原生仓位级 TPSL(缺失则按本地
       防线 `stops[symbol].trigger`,没有则 `invalid` 补挂);不下开仓单、不改策略、
       不动仓位数量;TPSL 无 size 维度,数量始终由交易所按整仓决定;
    ④ 停摆超过 `self_fence_seconds` 且仍有持仓 ⇒ 主动退出(退出码 3),
       由看护进程拉起 → 走掉线补做 + 交易所对账收敛事实。
       为什么敢退出:交易所侧止损是最后一道墙,而带病运行会持续错过
       止盈/止损机会(2026-10-22 教训)。
    """

    def __init__(self, runner, *, path: Path, poll_s: float = 15.0,
                 stall_s: float = 180.0, self_fence_s: float = 900.0,
                 secrets: Optional[dict] = None,
                 client_factory: Optional[Callable[[], object]] = None,
                 notifier=None, fence: Optional[Callable[[int], None]] = None,
                 protect_on_stall: bool = True) -> None:
        super().__init__(name="heartbeat-guard", daemon=True)
        self.runner = runner
        self.path = Path(path)
        self.poll_s = max(1.0, float(poll_s))
        self.stall_s = max(self.poll_s, float(stall_s))
        self.self_fence_s = max(0.0, float(self_fence_s))
        self.protect_on_stall = bool(protect_on_stall)
        self._secrets = secrets
        self._client_factory = client_factory
        self._client = None
        self._notifier = notifier
        self._fence = fence or (lambda code: os._exit(code))
        self._stop = threading.Event()
        self._stall_since: Optional[float] = None
        self._last_alert = 0.0
        self._last_result: dict = {}
        self._fenced = False

    # -- 基础设施 ---------------------------------------------------------
    def _journal(self, kind: str, rec: dict) -> None:
        try:
            self.runner._journal(kind, rec)
        except Exception:
            pass

    def _alert(self, msg: str, *, gap_s: float = 600.0) -> None:
        now = time.time()
        if now - self._last_alert < gap_s:
            return
        self._last_alert = now
        try:
            if self._notifier is not None:
                self._notifier.send_alert(msg)
            else:
                self.runner.tg.send_alert(msg)
        except Exception:
            pass

    def _mk_client(self):
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        if not self._secrets:
            return None
        from market_operator.bitget_live import BitgetLiveClient
        # 独立 session:主循环卡住时,共享 session 可能正被占着(或已坏)
        self._client = BitgetLiveClient(self._secrets)
        return self._client

    def _oid(self, sym: str, tag: str) -> str:
        import hashlib
        prefix = str(getattr(self.runner, "OID_PREFIX", "mo21"))
        h = hashlib.sha1(f"{sym}|hb|{tag}|{int(time.time() * 1000)}"
                         .encode()).hexdigest()[:12]
        return f"{prefix}{h}"

    # -- 保护面(只保护不主出) -------------------------------------------
    def ensure_protection(self) -> dict:
        """核对每个本地持仓在交易所是否仍有**自有**止损单(TPSL),缺失即补挂。

        返回 {checked, placed, missing, errors}。不改仓、不开仓、不改防线。
        """
        runner = self.runner
        out = {"checked": 0, "placed": 0, "missing": 0, "errors": 0}
        if getattr(runner, "mode", "") != "live":
            return out
        client = self._mk_client()
        if client is None:
            return out
        try:
            ex_pos = client.positions()
            raw = client.tpsl_orders_pending()
        except Exception as e:                      # BitgetError / 网络
            out["errors"] += 1
            self._journal("hb_protect_error", {"why": str(e)[:160]})
            return out
        ex_by = {(p.get("symbol"), int(p.get("direction") or 0)): p
                 for p in ex_pos or []}
        rows = raw.get("entrustedList") if isinstance(raw, dict) else raw
        rows = [r for r in (rows or []) if r.get("orderId")]
        prefix = str(getattr(runner, "OID_PREFIX", ""))
        owned: dict[str, set] = {}
        for r in rows:
            if not str(r.get("clientOid") or "").startswith(prefix):
                continue
            owned.setdefault(str(r.get("symbol")), set()).add(
                str(r.get("orderId")))

        for sym, pos in list(dict(runner.positions).items()):
            out["checked"] += 1
            direction = int(pos.get("direction") or 1)
            ex = ex_by.get((sym, direction))
            if ex is None:
                continue                            # 交易所已无仓:交给主循环对账
            local_stop = dict(runner.stops.get(sym) or {})
            live_ids = owned.get(sym) or set()
            if local_stop.get("order_id") in live_ids:
                continue                            # 保护仍在
            if live_ids and not local_stop.get("order_id"):
                continue                            # 已有自有保护单(旧进程残留)
            want = local_stop.get("trigger") or pos.get("invalid")
            if not want:
                out["missing"] += 1
                continue
            try:
                trigger = runner.fit_price(sym, float(want), direction)
                resp = client.place_stop_loss(
                    sym, trigger_price=trigger,
                    hold_side=runner.stop_hold_side(direction),
                    client_oid=self._oid(sym, "stop"))
                oid = str((resp or {}).get("orderId") or "")
                if oid:
                    # 顺手纠正本地记录(赋值原子;主循环下一轮以交易所为准)
                    runner.stops[sym] = {"trigger": trigger,
                                         "qty": float(ex.get("qty") or 0.0),
                                         "order_id": oid, "by": "heartbeat"}
                    # 主循环的在挂快照已过期:不作废的话新 order_id 不在旧快照里,
                    # 会被判"保护丢失"→ 主循环再挂一张、再撤一张,白折腾一轮。
                    for attr, val in (("_stops_live_ids", None),
                                      ("_stops_live_rows", None),
                                      ("_stops_checked_at", 0.0)):
                        if hasattr(runner, attr):
                            setattr(runner, attr, val)
                    out["placed"] += 1
                    self._journal("hb_stop_placed",
                                  {"symbol": sym, "trigger": trigger,
                                   "order_id": oid})
                else:
                    out["errors"] += 1
                    self._journal("hb_stop_error",
                                  {"symbol": sym, "why": "无 orderId"})
            except Exception as e:
                out["errors"] += 1
                self._journal("hb_stop_error", {"symbol": sym,
                                                "why": str(e)[:160]})
        if out["placed"]:
            self._alert(f"心跳看护:主循环停摆期间补挂保护止损 "
                        f"{out['placed']} 张(只保护不主出)")
        elif out["missing"]:
            self._alert(f"心跳看护:{out['missing']} 个持仓无保护且无法补挂,"
                        f"需人工介入", gap_s=300.0)
        return out

    # -- 主循环 -----------------------------------------------------------
    def tick(self) -> dict:
        """一次心跳:落盘 → 判定 → (必要时)保护/告警/自我熔断。"""
        ts = now_ms()
        runner = self.runner
        loop_ts = int(getattr(runner, "_loop_ts_ms", 0)
                      or getattr(runner, "loop_ts_ms", 0) or 0)
        started = int(getattr(runner, "started_ms", 0) or 0)
        base = max(loop_ts, started)          # 刚重启还没走完一步时不误判卡死
        age = (ts - base) / 1000.0 if base else None
        recovering = bool(getattr(runner, "_gap_replay", False))
        stalled = (age is not None and age > self.stall_s and not recovering)
        doc = runner.heartbeat_doc(ts, status="stalled" if stalled else None,
                                   guard={"ts_ms": ts,
                                          "loop_age_s": (round(age, 1)
                                                         if age is not None
                                                         else None),
                                          "stall_seconds": self.stall_s,
                                          "last": self._last_result})
        try:
            write_atomic(self.path, json.dumps(doc, ensure_ascii=False))
        except OSError as e:
            self._journal("hb_write_error", {"why": str(e)[:120]})

        if stalled:
            if self._stall_since is None:
                self._stall_since = time.time()
                self._journal("hb_stall", {
                    "loop_age_s": round(age, 1),
                    "step_started_ms": getattr(runner, "_step_started_ms", 0),
                    "positions": len(getattr(runner, "positions", {}) or {})})
                self._alert(
                    f"主循环已 {age / 60.0:.1f} 分钟无进展(进程仍在);"
                    f"持仓 {len(getattr(runner, 'positions', {}) or {})} 个,"
                    f"开始核对交易所保护单")
            if self.protect_on_stall:
                self._last_result = self.ensure_protection()
            self._maybe_fence(time.time() - (self._stall_since or 0))
        else:
            if self._stall_since is not None:
                self._journal("hb_recovered", {
                    "stalled_s": round(time.time() - self._stall_since, 1)})
                self._alert("主循环恢复推进(心跳看护解除)", gap_s=0.0)
            self._stall_since = None
            self._last_result = {}
        return doc

    def _maybe_fence(self, stalled_s: float) -> None:
        """停摆过久 ⇒ 主动退出,把"重启 + 掉线补做"交给看护系统。

        只在 live 且确实有持仓时熔断:无持仓时退出不影响任何风险敞口,
        但也没必要打断(可能只是交易所接口慢)。
        """
        if self._fenced or self.self_fence_s <= 0:
            return
        if stalled_s < self.self_fence_s:
            return
        if not getattr(self.runner, "positions", None):
            return
        self._fenced = True
        self._journal("hb_self_fence", {"stalled_s": round(stalled_s, 1)})
        self._alert(f"主循环停摆 {stalled_s / 60.0:.0f} 分钟且仍有持仓:"
                    f"自我熔断退出({EXIT_SELF_FENCE}),由看护拉起并补做缺口")
        time.sleep(1.0)
        self._fence(EXIT_SELF_FENCE)

    def run(self) -> None:                       # pragma: no cover - 线程循环
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception as e:
                self._journal("hb_tick_error", {"why": str(e)[:160]})

    def stop(self) -> None:
        self._stop.set()
