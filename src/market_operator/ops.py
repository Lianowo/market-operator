"""运维动作层:自检、进程快照、暖机/对账、日志与数据打包。

本模块只做**只读或有明确副作用边界**的运维动作,不参与策略决策,也不被
`live_runner` / 研究链 import(保持生产链不受运维代码影响)。所有路径经
`market_operator.paths` 解析,跨平台(windows / linux / macos)。

关键安全口径
------------
* `doctor` 默认不联网;显式 `--exchange` 才做公共行情 + 只读账户检查;
* `warmup` 在沙盒 state/journal 下构造 runner(不写生产 state/heartbeat),
  只拉行情、回放认知、与交易所持仓对账;`--write-cache` 才写 K 线缓存;
* Telegram/API 密钥只检查"是否配置",错误信息里用 `redact()` 脱敏;
* `package` 默认只打包运行期产物(日志/runtime/data 可选),绝不包含
  `config/secrets.env` 与 `.git`。
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import __version__, paths
from . import heartbeat as hb

CONFIG_REL = Path("config") / "live.json"
STRATEGY_REL = Path("config") / "strategy.json"
SECRETS_REL = Path("config") / "secrets.env"

STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2, "skip": -1}
STATUS_ICON = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f}{unit}"
        n /= 1024.0
    return f"{n:,.1f}PB"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_ms(text: Optional[str]) -> Optional[int]:
    return hb.parse_iso(text)


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path).expanduser() if path else paths.app_home() / CONFIG_REL
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"配置不是 JSON 对象:{p}")
    return data


def config_path(path: str | Path | None = None) -> Path:
    return Path(path).expanduser() if path else paths.app_home() / CONFIG_REL


def load_secrets(cfg: dict):
    """按配置解析密钥;只返回内存 dict,任何打印都必须先 redact。"""
    from .secrets_loader import load_secrets as _load
    rel = cfg.get("credentials_source")
    p = paths.app_home() / str(rel) if rel else None
    return _load(p if p and p.exists() else None,
                 config_overrides=cfg.get("credentials"))


def load_telegram_secrets(cfg: dict) -> dict:
    """只装载 Telegram 键(不要求 Bitget 三件套)。

    用于"只想起 TG 运维机器人"的场景:bootstrap 在缺少交易凭证时仍应把
    交互命令拉起,不能因为 Bitget 键缺失就整条 Telegram 链路消失。
    """
    from .secrets_loader import load_secrets as _load
    rel = cfg.get("credentials_source")
    p = paths.app_home() / str(rel) if rel else None
    return _load(p if p and p.exists() else None, required=(),
                 config_overrides=cfg.get("credentials"))


def secret_values(secrets: dict | None) -> list[str]:
    if not secrets:
        return []
    vals = [str(v) for v in secrets.values()
            if isinstance(v, str) and len(v) >= 6]
    return sorted(set(vals), key=len, reverse=True)


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|passphrase|token)(\s*[=:]\s*)"
               r"([^\s,;'\"]{4,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b"),
)


def redact(text: object, secrets: dict | None = None) -> str:
    """把已知密钥值与常见 key=value 形态替换为 `***`(日志/回复前必过)。"""
    out = str(text)
    for value in secret_values(secrets):
        if value:
            out = out.replace(value, "***")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    return out


def live_paths(cfg: dict) -> dict[str, Path]:
    loop = cfg.get("loop") or {}
    state = paths.resolve(loop.get("state_path", "runtime/live/state.json"))
    hb_rel = loop.get("heartbeat_path")
    heartbeat = (paths.resolve(hb_rel) if hb_rel
                 else state.parent / "heartbeat.json")
    return {
        "state": state,
        "campaigns": state.with_name("campaigns.json"),
        "heartbeat": heartbeat,
        "journal_dir": paths.resolve(loop.get("journal_dir", "logs")),
        "candles": state.parent / "candles",
        "supervisor_state": paths.resolve("runtime/live/supervisor.json"),
        "supervisor_lock": paths.resolve("runtime/live/supervisor.lock"),
        "supervisor_pid": paths.resolve("runtime/live/supervisor.pid"),
        "tg_pid": paths.resolve("runtime/live/tg_bot.pid"),
        "tg_state": paths.resolve("runtime/live/tg_bot_state.json"),
        "tg_lock": paths.resolve("runtime/live/tg_bot.lock"),
        "supervisor_jsonl": paths.log_dir() / "supervisor.jsonl",
    }


def read_state(cfg: dict) -> dict:
    return hb.read_json(live_paths(cfg)["state"])


def read_heartbeat(cfg: dict) -> dict:
    return hb.read_heartbeat(live_paths(cfg)["heartbeat"])


def read_json_file(path: str | Path, default: dict | None = None) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def pid_alive(pid: object) -> bool:
    try:
        return hb.process_alive(int(pid or 0))
    except (TypeError, ValueError):
        return False


_SHELL_NAMES = {"sh", "bash", "dash", "zsh", "ksh", "fish", "pgrep",
                "timeout", "xargs", "env"}


def _drop_shell_wrappers(pids: list[int]) -> list[int]:
    """去掉"命令行里带着 marker 的壳进程"(`sh -c 'pgrep -f <marker>'` 也算命中)。"""
    if not pids:
        return pids
    try:
        out = subprocess.run(["ps", "-o", "pid=,comm=", "-p",
                              ",".join(str(p) for p in pids)],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return pids
    shells = set()
    for line in (out.stdout or "").splitlines():
        pid, _, comm = line.strip().partition(" ")
        if pid.isdigit() and comm.strip().rsplit("/", 1)[-1] in _SHELL_NAMES:
            shells.add(int(pid))
    return [p for p in pids if p not in shells]


def find_pids(marker: str, *, exclude_self: bool = True) -> list[int]:
    """按命令行找进程(兜底用;正常路径一律读 pid 文件/心跳)。

    **探针自己也算命中**:Windows 上探针就是
    `powershell -Command "... -like '*live_supervisor.py*'"`,POSIX 上是
    `sh -c 'pgrep -f live_supervisor.py'` —— 不过滤就会张冠李戴:
    `mo status` 曾报出一个转瞬即逝的 powershell pid 当"看护"(alive=True),
    `mo stop` 还可能去杀它。所以按镜像名/壳名把探针排除掉。
    """
    marker = str(marker)
    pids: list[int] = []
    if os.name == "nt":
        esc = marker.replace("'", "''")
        ps = ("Get-CimInstance Win32_Process "
              "| Where-Object { $_.CommandLine -like '*%s*' -and "
              "$_.Name -notmatch 'powershell|pwsh|cmd|conhost|WmiPrvSE|bash' } "
              "| ForEach-Object { $_.ProcessId }" % esc)
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=30)
            pids = [int(x) for x in (out.stdout or "").split() if x.isdigit()]
        except (OSError, subprocess.SubprocessError, ValueError):
            pids = []
    else:
        try:
            out = subprocess.run(["pgrep", "-f", marker],
                                 capture_output=True, text=True, timeout=15)
            pids = [int(x) for x in (out.stdout or "").split() if x.isdigit()]
        except (OSError, subprocess.SubprocessError, ValueError):
            pids = []
        pids = _drop_shell_wrappers(pids)
    if exclude_self:
        pids = [p for p in pids if p != os.getpid()]
    return sorted(set(pids))


def tail_lines(path: str | Path, lines: int = 80,
               *, max_bytes: int = 512 * 1024) -> list[str]:
    """跨平台 tail(二进制读尾部,避免大日志整份进内存)。"""
    p = Path(path)
    n = max(1, min(int(lines), 5000))
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            fh.seek(max(0, size - max_bytes))
            blob = fh.read()
    except OSError:
        return []
    text = blob.decode("utf-8", "replace")
    return text.splitlines()[-n:]


# ---------------------------------------------------------------------------
# 自检(doctor)
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skip")

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "hint": self.hint}

    def render(self) -> str:
        line = f"[{STATUS_ICON.get(self.status, self.status)}] {self.name}"
        if self.detail:
            line += f": {self.detail}"
        if self.hint:
            line += f"  -> {self.hint}"
        return line


def _check_python() -> Check:
    v = sys.version_info
    if v >= (3, 11):
        return Check("Python", "ok", platform.python_version())
    return Check("Python", "fail", platform.python_version(), "需要 >= 3.11")


def _check_deps() -> Check:
    import importlib.util
    missing = [m for m in ("numpy", "pandas", "requests")
               if importlib.util.find_spec(m) is None]
    if missing:
        return Check("依赖", "fail", f"缺少 {', '.join(missing)}",
                     "pip install -e .")
    return Check("依赖", "ok", "numpy / pandas / requests")


def _check_config_file(path: Path) -> Check:
    if not path.exists():
        return Check(path.name, "fail", f"缺失:{path}", "从模板恢复配置文件")
    try:
        cfg = load_config(path)
    except (OSError, ValueError) as e:
        return Check(path.name, "fail", f"解析失败:{e}")
    if not cfg.get("risk") or not cfg.get("loop") or not cfg.get("cognition"):
        return Check(path.name, "fail", "缺少 risk/loop/cognition 段")
    return Check(path.name, "ok", f"version={cfg.get('version', '-')}")


def _check_strategy() -> Check:
    p = paths.app_home() / STRATEGY_REL
    if not p.exists():
        return Check("策略配置", "fail", f"缺失:{p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return Check("策略配置", "fail", f"解析失败:{e}")
    if not isinstance(raw, dict) or "params" not in raw:
        return Check("策略配置", "warn", "缺少 params 段(沿用默认参数)")
    return Check("策略配置", "ok", f"{len(raw)} 个顶层键")


def _check_secrets(cfg: dict) -> Check:
    try:
        secrets = load_secrets(cfg)
    except Exception as e:                       # noqa: BLE001 自检需报全因
        return Check("密钥", "fail", redact(e), "检查 config/secrets.env")
    need = ("BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE")
    missing = [k for k in need if not secrets.get(k)]
    if missing:
        return Check("密钥", "warn", f"缺少 {', '.join(missing)}",
                     "实盘需要 Bitget 三件套;paper 可忽略")
    tg = bool(secrets.get("TELEGRAM_BOT_TOKEN")
              and secrets.get("TELEGRAM_CHAT_ID"))
    return Check("密钥", "ok",
                 f"Bitget 已配置;Telegram={'已配置' if tg else '未配置'}")


def _check_dirs() -> Check:
    try:
        created = paths.ensure_layout()
    except OSError as e:
        return Check("运行期目录", "fail", str(e))
    for name, p in created.items():
        probe = p / ".mo_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            return Check("运行期目录", "fail", f"{name}: {e}")
    return Check("运行期目录", "ok",
                 f"{paths.layout_mode()} / {len(created)} 个类别可写")


def _check_disk(min_free_gb: float) -> Check:
    try:
        du = paths.disk_usage()
    except OSError as e:
        return Check("磁盘", "fail", str(e))
    status = "ok" if du["free_gb"] >= min_free_gb else "fail"
    return Check("磁盘", status,
                 f"剩余 {du['free_gb']:.1f}GB / 已用 {du['used_pct']}%",
                 f"低于 {min_free_gb:g}GB 阈值" if status == "fail" else "")


def _check_live(cfg: dict, require_live: bool) -> Check:
    doc = read_heartbeat(cfg)
    if not doc:
        return Check("实盘心跳", "fail" if require_live else "warn",
                     "缺失(未运行或刚换目录)", "mo start" if require_live else "")
    age = hb.beat_age_seconds(doc)
    pid = doc.get("pid")
    alive = pid_alive(pid)
    if age is None:
        return Check("实盘心跳", "warn", "缺少可解析时间戳")
    if not alive:
        return Check("实盘心跳", "fail" if require_live else "warn",
                     f"pid={pid} 已不在;age={age:.0f}s", "mo start")
    if age <= 120:
        return Check("实盘心跳", "ok",
                     f"pid={pid} status={doc.get('status')} age={age:.0f}s")
    return Check("实盘心跳", "warn",
                 f"pid={pid} status={doc.get('status')} age={age:.0f}s")



def _check_supervisor(cfg: dict, require_live: bool) -> Check:
    sup = cfg.get("supervisor") or {}
    if not sup.get("enabled", True):
        return Check("守护进程", "skip", "配置未启用")
    # 只认 `supervisor_pid()`:它先读 `supervisor.pid`,再按进程名兜底。
    # 千万别读 `supervisor.json` 的 `spawned_pid` —— 那是看护拉起的 runner 子进程,
    # 拿它当看护 pid 会让「看护已死但 runner 还在」被判成健康,`mo stop` 也只杀
    # runner(看护立刻再拉起来)。
    pid = supervisor_pid(cfg)
    if pid and pid_alive(pid):
        return Check("守护进程", "ok", f"pid={pid}")
    return Check("守护进程", "fail" if require_live else "warn",
                 "未运行(无存活 pid)", "mo start")


def _check_layout_state(cfg: dict) -> Check:
    lp = live_paths(cfg)
    if lp["state"].exists():
        size = lp["state"].stat().st_size
        return Check("布局/state", "ok",
                     f"{paths.layout_mode()} state={size}B")
    legacy = paths.app_home() / "runtime" / "live" / "state.json"
    if paths.is_portable() and legacy.exists():
        return Check("布局/state", "fail",
                     "portable 下 state 缺失,但旧目录仍有 state",
                     "mo migrate-data --move 后重启,或 mo layout legacy")
    if legacy.exists():
        return Check("布局/state", "warn", "state 缺失,旧路径有历史 state")
    return Check("布局/state", "ok", "尚无 state(首次启动)")


def _check_exchange(cfg: dict) -> list[Check]:
    """只读联网:公共行情 + 账户/持仓/持仓模式一致性。"""
    from .bitget_live import BitgetError, BitgetLiveClient
    acct = cfg.get("account") or {}
    uni = cfg.get("universe") or {}
    product = str(uni.get("product_type") or "USDT-FUTURES")
    try:
        secrets = load_secrets(cfg)
        client = BitgetLiveClient(secrets, product_type=product)
    except Exception as e:                       # noqa: BLE001
        return [Check("交易所", "fail", redact(e))]

    out: list[Check] = []
    try:
        tickers = client.tickers()
        min_v = float(uni.get("min_usdt_volume_24h") or 0.0)
        picked = client.universe(min_v, tickers=tickers)
        core = [s for s in (cfg.get("symbols_core") or []) if s in picked]
        status = "ok" if picked else "fail"
        out.append(Check("交易所行情", status,
                         f"{len(tickers)} 合约 / 宇宙 {len(picked)} / "
                         f"core 命中 {len(core)}"))
    except BitgetError as e:
        return out + [Check("交易所行情", "fail", redact(e))]

    try:
        positions = client.positions()
        bal = client.balance()
        out.append(Check("交易所账户", "ok",
                         f"持仓 {len(positions)} / equity={bal['equity']:.2f} "
                         f"available={bal['available']:.2f}"))
    except BitgetError as e:
        msg = redact(e)
        status = "warn" if "缺少 Bitget 凭证" in msg else "fail"
        out.append(Check("交易所账户", status, msg))
        return out

    expected = str(acct.get("position_mode") or "").strip()
    if expected:
        probe = ((cfg.get("symbols_core") or []) or
                 [p["symbol"] for p in positions] or ["BTCUSDT"])[0]
        try:
            actual = client.position_mode(probe)
        except BitgetError as e:
            out.append(Check("持仓模式", "warn", redact(e)))
        else:
            if actual and actual != expected:
                out.append(Check("持仓模式", "fail",
                                 f"配置={expected} 交易所={actual}",
                                 "runner 会 fail-closed 拒绝启动"))
            else:
                out.append(Check("持仓模式", "ok",
                                 f"{actual or expected}({probe})"))
    return out


def build_checks(cfg: dict, *, exchange: bool = False,
                 require_live: bool = False,
                 min_free_gb: float = 1.0,
                 cfg_file: Path | None = None) -> list[Check]:
    checks = [
        _check_python(),
        _check_deps(),
        Check("版本", "ok", __version__),
        _check_config_file(cfg_file or config_path()),
        _check_strategy(),
        _check_secrets(cfg),
        _check_dirs(),
        _check_disk(min_free_gb),
        _check_layout_state(cfg),
        _check_live(cfg, require_live),
        _check_supervisor(cfg, require_live),
    ]
    if exchange:
        checks.extend(_check_exchange(cfg))
    return checks


def summarize_checks(checks: Iterable[Check]) -> dict:
    rows = [c.as_dict() for c in checks]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "ok": not any(r["status"] == "fail" for r in rows),
        "counts": counts,
        "failures": [r for r in rows if r["status"] == "fail"],
        "warnings": [r for r in rows if r["status"] == "warn"],
        "checks": rows,
    }


def doctor(cfg: dict, **kwargs) -> dict:
    result = summarize_checks(build_checks(cfg, **kwargs))
    result["at_utc"] = utc_now_iso()
    result["layout"] = paths.layout_report()
    result["machine"] = machine_info()
    return result



def machine_info() -> dict:
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = ""
    info = {
        "hostname": socket.gethostname(),
        "ip": ip,
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "pid": os.getpid(),
    }
    try:
        info["loadavg"] = list(os.getloadavg())
    except (OSError, AttributeError):
        info["loadavg"] = None
    try:
        info["memory"] = _memory_info()
    except Exception:                            # noqa: BLE001 平台差异
        info["memory"] = {}
    try:
        info["disk"] = paths.disk_usage()
    except OSError:
        info["disk"] = {}
    return info


def _memory_info() -> dict:
    """总/可用内存:Linux 读 /proc/meminfo;Windows 用 ctypes API。"""
    if os.name == "nt":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return {}
        return {"total": stat.ullTotalPhys, "available": stat.ullAvailPhys,
                "used_pct": float(stat.dwMemoryLoad)}
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            fields[key] = int(rest.strip().split()[0]) * 1024
        total = fields.get("MemTotal", 0)
        avail = fields.get("MemAvailable", 0)
        return {"total": total, "available": avail,
                "used_pct": round(100.0 * (total - avail) / total, 1)
                if total else 0.0}
    except (OSError, ValueError, IndexError):
        return {}


def uptime_seconds(pid: object) -> Optional[float]:
    """进程已运行秒数(仅 Linux 可精确读 /proc;其他平台返回 None)。"""
    try:
        if os.name == "nt":
            return None
        stat = Path(f"/proc/{int(pid)}/stat").read_text().split()
        start_ticks = int(stat[21])
        with Path("/proc/uptime").open() as fh:
            up = float(fh.read().split()[0])
        return max(0.0, up - start_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError, TypeError, AttributeError):
        return None



# ---------------------------------------------------------------------------
# 代码同步(git 拉取 / 拷贝部署两相兼容)
# ---------------------------------------------------------------------------

_GIT_URL_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@")


def _git(home: Path, args: list[str], *, timeout: float = 300.0):
    """执行 git;任何异常都折叠成 CompletedProcess,绝不冒泡到运维命令。"""
    try:
        return subprocess.run(
            ["git", "-C", str(home), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=max(5.0, timeout))
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 127, "", f"{type(e).__name__}: {e}")


def _scrub_git(text: object, *, lines: int = 20) -> str:
    """git 输出可能带 remote 里的 token(`https://user:token@host`),必须脱敏。"""
    raw = str(text or "")
    raw = _GIT_URL_RE.sub(lambda m: f"{m.group('scheme')}***@", raw)
    rows = [r for r in raw.strip().splitlines() if r.strip()]
    return "\n".join(rows[-max(1, lines):])


def git_update(*, check: bool = False, install: bool = True,
               timeout: float = 300.0) -> dict:
    """一条命令同步代码:git 工作区 `pull --ff-only`;拷贝部署则安全跳过。

    设计口径
    --------
    * 只在**有 `.git` 且不是浅克隆异常**时动 git;拷贝过去的代码文件夹
      (没有 `.git`)直接 `skipped`,不会把运维命令变成报错;
    * 只用 `--ff-only`:本地有未提交改动/分叉时宁可失败,也不做隐式 merge
      或 reset —— 生产目录不允许被自动改写;
    * `pull` 后默认 `pip install -e .`(pyproject 可能变了);`--no-install`
      可在离线机器上跳过;
    * 返回值里的 git 输出统一走 `_scrub_git`,防止 remote URL 里的凭据泄漏。
    """
    home = paths.app_home()
    rep: dict = {
        "ok": True, "home": str(home), "is_repo": False, "check": bool(check),
        "skipped": False, "branch": "", "before": "", "after": "",
        "changed": False, "dirty": 0, "behind": None, "install": None,
        "pull": None, "errors": [], "notes": [],
    }
    if not (home / ".git").exists():
        rep["skipped"] = True
        rep["notes"].append("非 git 工作区(代码文件夹拷贝部署):跳过拉取")
        return rep
    inside = _git(home, ["rev-parse", "--is-inside-work-tree"], timeout=30)
    if inside.returncode != 0:
        rep["ok"] = False
        rep["errors"].append("git 不可用或目录损坏: "
                             + _scrub_git(inside.stderr or inside.stdout, lines=3))
        return rep
    rep["is_repo"] = True
    rep["branch"] = _git(home, ["rev-parse", "--abbrev-ref", "HEAD"],
                         timeout=30).stdout.strip()
    rep["before"] = _git(home, ["rev-parse", "--short", "HEAD"],
                         timeout=30).stdout.strip()
    dirty = _git(home, ["status", "--porcelain"], timeout=60).stdout
    rep["dirty"] = len([r for r in dirty.splitlines() if r.strip()])

    if check:
        fetch = _git(home, ["fetch", "--all", "--prune"], timeout=timeout)
        if fetch.returncode != 0:
            rep["ok"] = False
            rep["errors"].append("git fetch 失败: "
                                 + _scrub_git(fetch.stderr or fetch.stdout, lines=5))
            return rep
        behind = _git(home, ["rev-list", "--count", "HEAD..@{u}"], timeout=60)
        if behind.returncode == 0:
            try:
                rep["behind"] = int((behind.stdout or "0").strip() or 0)
            except ValueError:
                rep["behind"] = None
        else:
            rep["notes"].append("当前分支没有上游跟踪分支,无法报告 behind")
        return rep

    pull = _git(home, ["pull", "--ff-only"], timeout=timeout)
    rep["pull"] = {"rc": pull.returncode,
                   "out": _scrub_git((pull.stdout or "") + "\n" + (pull.stderr or ""))}
    if pull.returncode != 0:
        rep["ok"] = False
        rep["errors"].append("git pull --ff-only 失败(本地改动/无上游/认证): "
                             + _scrub_git(pull.stderr or pull.stdout, lines=5))
        return rep
    rep["after"] = _git(home, ["rev-parse", "--short", "HEAD"],
                        timeout=30).stdout.strip()
    rep["changed"] = bool(rep["before"] and rep["after"]
                          and rep["before"] != rep["after"])
    if not rep["changed"]:
        rep["notes"].append("已是最新")
    if install:
        try:
            pip = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", "."],
                cwd=str(home), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=max(60.0, timeout))
            rep["install"] = {"rc": pip.returncode,
                              "out": _scrub_git((pip.stdout or "")
                                                + "\n" + (pip.stderr or ""), lines=8)}
            if pip.returncode != 0:
                rep["ok"] = False
                rep["errors"].append("依赖安装失败(可用 --no-install 跳过并手动执行)")
        except (OSError, subprocess.SubprocessError) as e:
            rep["ok"] = False
            rep["errors"].append(f"依赖安装异常: {type(e).__name__}")
    return rep


# ---------------------------------------------------------------------------
# 进程控制 / 状态
# ---------------------------------------------------------------------------


def supervisor_pid(cfg: dict) -> Optional[int]:
    lp = live_paths(cfg)
    try:
        pid = int(lp["supervisor_pid"].read_text().strip())
        if pid_alive(pid):
            return pid
    except (OSError, ValueError):
        pass
    # `supervisor.json` 的 `spawned_pid` 是看护拉起的 runner 子进程,**不是**看护自己;
    # 旧版看护没写 pid 文件时按进程名精确匹配(误用 spawned_pid 会让 `mo stop`
    # 只杀掉 runner,看护下一轮又把它拉起来)。
    found = find_pids("live_supervisor.py")
    return found[0] if found else None


def _runner_pid(cfg: dict) -> Optional[int]:
    doc = read_heartbeat(cfg)
    pid = doc.get("pid")
    return int(pid) if pid_alive(pid) else None


def _tg_pid(cfg: dict) -> Optional[int]:
    lp = live_paths(cfg)
    try:
        pid = int(lp["tg_pid"].read_text().strip())
        return pid if pid_alive(pid) else None
    except (OSError, ValueError):
        return None


def status_snapshot(cfg: dict) -> dict:
    lp, state = live_paths(cfg), read_state(cfg)
    doc = read_heartbeat(cfg)
    age = hb.beat_age_seconds(doc)
    sup_pid, runner_pid = supervisor_pid(cfg), _runner_pid(cfg)
    tg_pid = _tg_pid(cfg)
    counters = state.get("counters") or {}
    return {
        "at_utc": utc_now_iso(),
        "mode": (doc.get("mode") or state.get("mode") or "?"),
        "runner": {"pid": runner_pid, "alive": bool(runner_pid),
                   "heartbeat_age_s": age,
                   "status": doc.get("status")},
        "supervisor": {"pid": sup_pid, "alive": bool(sup_pid),
                       "restarts_left": _restarts_left(cfg)},
        # TG 机器人是**独立常驻进程**(`mo tg-run`),既不在看护的监管范围里,
        # 也不由看护拉起;不报出来就只能靠人猜"命令为什么没回"。
        "tg_bot": {"pid": tg_pid, "alive": bool(tg_pid)},
        "positions": len(state.get("positions") or {}),
        "stops": len(state.get("stops") or {}),
        "equity": state.get("equity", doc.get("equity")),
        "open_intents": len(state.get("open_intents") or {}),
        "counters": {k: counters.get(k, 0) for k in
                     ("signals", "orders_placed", "fills", "exits",
                      "reconcile_alerts", "order_errors", "api_halt_skips")},
        "breaker": state.get("breaker") or {},
        "paths": {k: str(v) for k, v in lp.items()},
        "layout": paths.layout_mode(),
        "disk": paths.disk_usage(),
        "usage": paths.category_usage(),
    }


def _restarts_left(cfg: dict) -> Optional[int]:
    sup = cfg.get("supervisor") or {}
    doc = read_json_file(live_paths(cfg)["supervisor_state"])
    rows = [float(t) for t in (doc.get("restarts") or [])
            if isinstance(t, (int, float))]
    now = time.time()
    recent = [t for t in rows if now - t < 3600.0]
    return max(0, int(sup.get("max_restarts_per_hour", 6)) - len(recent))


def start_supervisor(cfg: dict, *, cfg_file: Path | None = None,
                     wait_s: float = 20.0) -> dict:
    """脱离会话启动看护(由看护拉起 runner),返回 pid 与首个心跳观测。"""
    lp = live_paths(cfg)
    pid = supervisor_pid(cfg)
    if pid:
        return {"started": False, "pid": pid, "why": "已在运行"}
    cfg_path = Path(cfg_file) if cfg_file else config_path()
    argv = [sys.executable,
            str(paths.app_home() / "scripts" / "live_supervisor.py"),
            "--config", str(cfg_path)]
    out = paths.log_dir() / "supervisor_stdout.log"
    err = paths.log_dir() / "supervisor_stderr.log"
    pid = hb.spawn_detached(argv, cwd=paths.app_home(),
                            stdout_path=out, stderr_path=err)
    if pid:
        hb.write_atomic(lp["supervisor_pid"], str(pid))
    deadline = time.time() + max(0.0, wait_s)
    beat = None
    while time.time() < deadline:
        beat = read_heartbeat(cfg)
        if beat or not pid_alive(pid):
            break
        time.sleep(1.0)
    return {"started": bool(pid), "pid": pid,
            "alive": pid_alive(pid),
            "heartbeat": bool(beat),
            "stdout": str(out), "stderr": str(err)}


def start_tg_bot(cfg: dict, *, cfg_file: Path | None = None,
                 wait_s: float = 10.0, sync: bool = True) -> dict:
    """脱离会话启动 Telegram 机器人(`mo tg-run`),幂等。

    机器人是独立常驻进程,不属于看护范围;所以"一条命令把系统拉起来"必须
    显式包含它,否则 bootstrap 完成后命令区仍然是死的。缺 token 时按**跳过**
    处理(不影响实盘链路),启动失败才报 ok=False。
    """
    pid = _tg_pid(cfg)
    if pid:
        return {"ok": True, "started": False, "pid": pid, "alive": True,
                "why": "已在运行"}
    try:
        secrets = load_telegram_secrets(cfg)
    except Exception as e:                       # noqa: BLE001 - 运维命令不冒泡
        return {"ok": False, "started": False, "skipped": True,
                "why": f"密钥读取失败: {type(e).__name__}"}
    tokens = {str(secrets.get(k) or "").strip()
              for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TRADE_BOT_TOKEN")}
    tokens.discard("")
    if not tokens:
        return {"ok": True, "started": False, "skipped": True,
                "why": "未配置 Telegram bot token"}
    cfg_path = Path(cfg_file) if cfg_file else config_path()
    entry = paths.app_home() / "scripts" / "mo.py"
    # 部署包里可能没有 scripts/(纯 wheel 安装) ⇒ 退回 `python -m market_operator`,
    # 两者都是同一入口,别让"机器人在起停流程里被静默漏掉"。
    argv = [sys.executable, str(entry)] if entry.is_file() else [
        sys.executable, "-m", "market_operator"]
    argv += ["--config", str(cfg_path), "tg-run"]
    if not sync:
        argv.append("--no-sync")
    out = paths.log_dir() / "tg_bot_stdout.log"
    err = paths.log_dir() / "tg_bot_stderr.log"
    try:
        spawned = hb.spawn_detached(argv, cwd=paths.app_home(),
                                    stdout_path=out, stderr_path=err)
    except OSError as e:
        return {"ok": False, "started": False,
                "why": f"spawn 失败: {type(e).__name__}: {e}"}
    if not spawned:
        return {"ok": False, "started": False, "why": "spawn 未返回 pid"}
    deadline = time.time() + max(0.0, wait_s)
    while time.time() < deadline:
        if _tg_pid(cfg) or not pid_alive(spawned):
            break
        time.sleep(0.5)
    pid_file = _tg_pid(cfg)
    return {"ok": True, "started": True, "pid": pid_file or spawned,
            "alive": pid_alive(pid_file or spawned),
            "synced": bool(sync),
            "stdout": str(out), "stderr": str(err)}


def stop_all(cfg: dict, *, timeout: float = 15.0,
             include_tg: bool = True) -> dict:
    """先停看护(防止它把 runner 再拉起来),再停 runner 与 TG 机器人。"""
    lp = live_paths(cfg)
    actions: list[dict] = []

    def _stop(label: str, pid: Optional[int], pid_file: Path | None = None):
        if not pid:
            actions.append({"target": label, "pid": None, "stopped": True})
            return
        ok = hb.terminate_pid(pid, timeout=timeout)
        actions.append({"target": label, "pid": int(pid), "stopped": ok})
        if ok and pid_file is not None:
            try:
                pid_file.unlink()
            except OSError:
                pass

    sup_pid = supervisor_pid(cfg)
    if not sup_pid:
        found = find_pids("live_supervisor.py")
        sup_pid = found[0] if found else None
    _stop("supervisor", sup_pid, lp["supervisor_pid"])

    runner_pid = _runner_pid(cfg)
    if not runner_pid:
        # 只匹配文件名:Windows 的命令行是 `...\scripts\live_runner.py`,
        # 带正斜杠的 "scripts/live_runner.py" 永远匹配不上 -> 停不掉 runner。
        found = find_pids("live_runner.py")
        runner_pid = found[0] if found else None
    _stop("runner", runner_pid)

    if include_tg:
        _stop("tg_bot", _tg_pid(cfg), lp["tg_pid"])

    return {"actions": actions,
            "all_stopped": all(a["stopped"] for a in actions),
            "at_utc": utc_now_iso()}


def restart_all(cfg: dict, *, cfg_file: Path | None = None,
                warmup_first: bool = False, include_tg: bool = True,
                tg_wait_s: float = 10.0, **warmup_kw) -> dict:
    """重启整条运行链。

    `stop_all` 会停掉 TG 机器人;旧实现只拉看护,导致 `mo restart` 之后
    交互命令永久静默。这里把 TG 一起拉回来(缺 token 则跳过)。
    """
    stopped = stop_all(cfg, include_tg=include_tg)
    time.sleep(1.5)
    report: dict = {"stop": stopped}
    if warmup_first:
        report["warmup"] = warmup(cfg, **warmup_kw)
    report["start"] = start_supervisor(cfg, cfg_file=cfg_file)
    if include_tg:
        report["tg"] = start_tg_bot(cfg, cfg_file=cfg_file, wait_s=tg_wait_s)
    else:
        # 显式跳过也要留痕:`restart --no-tg` 不会停旧机器人,别让人以为它重启了
        report["tg"] = {"started": False, "skipped": True, "why": "--no-tg",
                        "ok": True}
    return report


def log_files() -> list[dict]:
    root = paths.log_dir()
    rows = []
    if not root.exists():
        return rows
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rows.append({"name": p.relative_to(root).as_posix(),
                     "path": str(p), "bytes": st.st_size,
                     "mtime": st.st_mtime,
                     "mtime_utc": datetime.fromtimestamp(
                         st.st_mtime, timezone.utc).isoformat(
                             timespec="seconds")})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 持仓查看 / 暖机
# ---------------------------------------------------------------------------


def local_positions(cfg: dict) -> list[dict]:
    state = read_state(cfg)
    stops = state.get("stops") or {}
    rows = []
    for sym, pos in (state.get("positions") or {}).items():
        stop = stops.get(sym) or {}
        rows.append({
            "symbol": sym,
            "direction": int(pos.get("direction") or 0),
            "qty": float(pos.get("qty") or 0.0),
            "entry": pos.get("entry"),
            "mark": pos.get("mark"),
            "invalid": pos.get("invalid"),
            "stop": stop.get("trigger"),
            "stop_order": stop.get("order_id"),
            "tf": pos.get("tf_label"),
            "opened_ts": pos.get("opened_ts"),
        })
    rows.sort(key=lambda r: r["symbol"])
    return rows


def local_orders(cfg: dict) -> dict:
    """在挂委托 + 保护性止损(只读本地 state,不访问交易所)。

    `open_intents` 里保留历史(持有 filled/expired/canceled),这里只露
    `status=armed` 的,也就是"现在真的还挂在盘口上"的那一批。
    """
    state = read_state(cfg)
    intents: list[dict] = []
    for oid, it in (state.get("open_intents") or {}).items():
        if not isinstance(it, dict) or it.get("status") != "armed":
            continue
        intents.append({
            "oid": str(it.get("oid") or oid),
            "symbol": it.get("symbol"),
            "role": it.get("role"),
            "kind": it.get("kind"),
            "direction": int(it.get("direction") or 0),
            "qty": float(it.get("qty") or 0.0),
            "limit_price": it.get("limit_price"),
            "expiry_ms": it.get("expiry_ts"),
            "exchange_order_id": it.get("exchange_order_id"),
        })
    intents.sort(key=lambda r: (str(r["symbol"]), str(r["role"])))
    stops: list[dict] = []
    for sym, st in (state.get("stops") or {}).items():
        if not isinstance(st, dict):
            continue
        stops.append({"symbol": sym, "trigger": st.get("trigger"),
                      "qty": st.get("qty"), "order_id": st.get("order_id")})
    stops.sort(key=lambda r: str(r["symbol"]))
    return {"at_utc": utc_now_iso(), "intents": intents, "stops": stops}


def position_mismatches(local: list[dict], exchange: list[dict]) -> list[str]:
    ours = {(r["symbol"], int(r["direction"])): r for r in local}
    theirs = {(p["symbol"], int(p["direction"])): p for p in exchange}
    out = []
    for key, row in ours.items():
        ex = theirs.get(key)
        if ex is None:
            out.append(f"ours_only {key[0]} dir={key[1]} qty={row['qty']}")
        elif abs(float(ex["qty"]) - row["qty"]) > max(
                1e-9, float(ex["qty"]) * 1e-6):
            out.append(f"qty_mismatch {key[0]} ours={row['qty']} "
                       f"exchange={ex['qty']}")
    for key, p in theirs.items():
        if key not in ours:
            out.append(f"exchange_only {key[0]} dir={key[1]} qty={p['qty']}")
    return out


def positions_report(cfg: dict, *, exchange: bool = True) -> dict:
    """本系统持仓(本地账本) + 交易所持仓(只读) + 差异。"""
    local = local_positions(cfg)
    out = {"at_utc": utc_now_iso(), "local": local,
           "runner_alive": bool(_runner_pid(cfg)),
           "heartbeat_age_s": hb.beat_age_seconds(read_heartbeat(cfg)),
           "exchange": None, "balance": None, "mismatches": [],
           "errors": []}
    if not local and not out["runner_alive"]:
        out["note"] = "本地无持仓且 runner 未运行"
    if not exchange:
        return out
    try:
        from .bitget_live import BitgetLiveClient
        client = BitgetLiveClient(
            load_secrets(cfg),
            product_type=str((cfg.get("universe") or {})
                             .get("product_type") or "USDT-FUTURES"))
        out["exchange"] = client.positions()
        bal = client.balance()
        out["balance"] = {"equity": bal["equity"],
                          "available": bal["available"],
                          "unrealized_pl": bal["unrealized_pl"]}
        out["mismatches"] = position_mismatches(local, out["exchange"])
    except Exception as e:                       # noqa: BLE001 展示用
        out["errors"].append(redact(e))
    return out


def _tf_seconds(label: str) -> int:
    text = str(label).strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        return int(text[:-1]) * units[text[-1]]
    except (ValueError, KeyError, IndexError):
        raise ValueError(f"无法识别的周期:{label!r}")


def _bitget_gran(label: str) -> str:
    text = str(label).strip().lower()
    return f"{int(text[:-1])}{text[-1].upper()}"


def load_runner_module():
    """按文件路径加载 scripts/live_runner.py(它不在包里,但暖机要复用同一份
    认知装配函数;只读导入,不启动主循环)。"""
    import importlib.util
    path = paths.app_home() / "scripts" / "live_runner.py"
    if not path.exists():
        raise FileNotFoundError(f"缺少 runner:{path}")
    name = "_market_operator_live_runner"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_candle_cache(cfg: dict, symbol: str, bars: list,
                        *, force: bool = False) -> Path | None:
    """写 runner 同格式 K 线缓存(`<symbol>_<gran>.json.gz`),只在无缓存时写。"""
    import gzip
    gran = str((cfg.get("cognition") or {}).get("granularity") or "2H").upper()
    target = live_paths(cfg)["candles"] / f"{symbol}_{gran}.json.gz"
    if target.exists() and not force:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    payload = json.dumps(bars, separators=(",", ":"))
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, target)
    return target



def warmup(cfg: dict, *, symbols: Iterable[str] | None = None,
           timeframes: Iterable[str] | None = None, bars: int = 300,
           write_cache: bool = False, check_account: bool = True) -> dict:
    """沙盒暖机:拉行情/合约 -> 回放 v21 认知 -> 只读对账。

    不构造 LiveRunner、不写生产 state/heartbeat、不下单;唯一可写动作是
    `write_cache=True` 且 runner 未运行时补 K 线缓存(与 runner 同格式)。
    """
    from .bitget_live import BitgetError, BitgetLiveClient
    mod = load_runner_module()
    raw, params, _rp, _rp_by_tf = mod.load_strategy_params(
        paths.app_home() / STRATEGY_REL)
    tfs = list(timeframes or raw.get("tf_list") or ["2h", "4h", "12h", "1d"])
    core = list(symbols or cfg.get("symbols_core") or [])[:20]
    trig_by = {str(k).lower(): v for k, v in
               (raw.get("trigger_tf_by_htf") or {}).items()}
    client = BitgetLiveClient(
        load_secrets(cfg),
        product_type=str((cfg.get("universe") or {}).get("product_type")
                         or "USDT-FUTURES"))
    result: dict = {"ok": True, "symbols": {}, "timeframes": tfs,
                    "errors": [], "warnings": [], "at_utc": utc_now_iso()}
    if not core:
        result["ok"] = False
        result["errors"].append("symbols_core 为空")
        return result

    try:
        contracts, _contract_raw = mod.load_contracts_live(client)
        result["contracts"] = len(contracts)
    except BitgetError as e:
        result["ok"] = False
        result["errors"].append(f"合约规格:{redact(e)}")
        return result

    for sym in core:
        rec: dict = {"bars": 0, "tfs": {}, "cache": None}
        try:
            bars2h = client.history_candles(sym, _bitget_gran(tfs[0]),
                                            limit=max(60, int(bars)))
            rec["bars"] = len(bars2h)
            if len(bars2h) < 30:
                result["warnings"].append(f"{sym} 2H 仅 {len(bars2h)} 根")
            for tf in tfs:
                obs = _tf_seconds(tf)
                trig = int(trig_by.get(str(tf).lower()) or obs)
                cog = (bars2h if obs == 7200
                       else mod.aggregate_tf(bars2h, obs))
                trig_bars = (bars2h if trig <= 7200
                             else mod.aggregate_tf(bars2h, trig))
                if len(cog) < 5:
                    rec["tfs"][tf] = {"bars": len(cog), "skipped": True}
                    result["warnings"].append(f"{sym} {tf} 认知 bar 不足")
                    continue
                mod.replay_cognition(params, sym, cog, trig_bars,
                                     obs_tf_seconds=obs,
                                     trigger_tf_seconds=trig)
                rec["tfs"][tf] = {"bars": len(cog), "trigger_s": trig}
            if write_cache and not _runner_pid(cfg):
                p = _write_candle_cache(cfg, sym, bars2h)
                rec["cache"] = str(p) if p else None
            result["symbols"][sym] = rec
        except (BitgetError, RuntimeError, ValueError) as e:
            result["ok"] = False
            result["errors"].append(f"{sym}:{redact(e)}")
            result["symbols"][sym] = rec

    if check_account:
        try:
            ex_pos = client.positions()
            bal = client.balance()
            result["account"] = {"positions": len(ex_pos),
                                 "equity": bal["equity"],
                                 "available": bal["available"]}
            result["mismatches"] = position_mismatches(local_positions(cfg),
                                                       ex_pos)
        except BitgetError as e:
            result["warnings"].append(f"账户对账跳过:{redact(e)}")
    return result



# ---------------------------------------------------------------------------
# 数据打包(跨机器搬运行期产物)
# ---------------------------------------------------------------------------

_BANNED_NAME_PARTS = ("secrets.env", ".env", "id_rsa", "id_ed25519",
                      ".pem", ".key", "credentials")
_BANNED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _iter_package_files(cats: Iterable[str]) -> Iterable[tuple[Path, str]]:
    for cat in cats:
        root = paths.category_root(cat)
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(paths.app_home()).as_posix()
            parts = set(p.parts)
            if parts & _BANNED_DIRS:
                continue
            low = p.name.lower()
            if any(part in low for part in _BANNED_NAME_PARTS):
                continue
            if low.endswith((".tmp", ".lock", ".pid")):
                continue
            yield p, rel


def _contains_secret(path: Path, secrets: dict | None,
                     *, max_bytes: int = 2 * 1024 * 1024) -> bool:
    values = secret_values(secrets)
    if not values:
        return False
    try:
        if path.stat().st_size > max_bytes:
            return False
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(v in text for v in values)


def package_data(*, include_data: bool = False, include_reports: bool = False,
                 include_backups: bool = False,
                 dest: str | Path | None = None,
                 secrets: dict | None = None) -> dict:
    """打包运行期产物(zip + 清单),用于换机/备份。

    默认 `logs` + `runtime`;`data`/`reports`/`backups` 需显式开启。命中
    密钥文件或文件正文含已知密钥值的条目会被跳过并记录(宁可漏备,不泄密)。
    """
    cats = ["logs", "runtime"]
    if include_data:
        cats.append("data")
    if include_reports:
        cats.append("reports")
    if include_backups:
        cats.append("backups")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = (Path(dest) if dest else
           paths.backup_dir() / f"mo-{stamp}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, "created_at_utc": utc_now_iso(),
                "home": str(paths.app_home()),
                "layout": paths.layout_mode(),
                "categories": cats, "machine": machine_info(),
                "files": [], "skipped": []}
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for path, rel in _iter_package_files(cats):
            if _contains_secret(path, secrets):
                manifest["skipped"].append({"path": rel, "why": "含密钥"})
                continue
            try:
                zf.write(path, arcname=rel)
                manifest["files"].append({
                    "path": rel, "bytes": path.stat().st_size,
                    "sha256": _file_sha256(path)})
            except OSError as e:
                manifest["skipped"].append(
                    {"path": rel, "why": f"{type(e).__name__}: {e}"})
        zf.writestr("MANIFEST.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
    side = out.with_suffix(".manifest.json")
    side.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    size, _ = paths.dir_size(out.parent)
    return {"ok": True, "archive": str(out), "manifest": str(side),
            "files": len(manifest["files"]),
            "skipped": len(manifest["skipped"]),
            "bytes": out.stat().st_size,
            "categories": cats}


def _file_sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for blob in iter(lambda: fh.read(chunk), b""):
            h.update(blob)
    return h.hexdigest()


def verify_archive(path: str | Path) -> dict:
    """校验 zip 内每个文件与清单 sha256 一致(搬机后先验再解压)。"""
    p = Path(path)
    out = {"ok": True, "archive": str(p), "checked": 0, "errors": []}
    try:
        zf = zipfile.ZipFile(p, "r")
    except (OSError, zipfile.BadZipFile) as e:
        return {"ok": False, "archive": str(p), "checked": 0,
                "errors": [str(e)]}
    with zf:
        try:
            manifest = json.loads(zf.read("MANIFEST.json"))
        except (KeyError, ValueError) as e:
            return {"ok": False, "archive": str(p), "checked": 0,
                    "errors": [f"MANIFEST.json:{e}"]}
        for row in manifest.get("files") or []:
            try:
                import hashlib
                h = hashlib.sha256()
                with zf.open(row["path"]) as fh:
                    for blob in iter(lambda: fh.read(1 << 20), b""):
                        h.update(blob)
                if h.hexdigest() != row.get("sha256"):
                    out["ok"] = False
                    out["errors"].append(f"sha256 不一致:{row['path']}")
                out["checked"] += 1
            except (KeyError, OSError) as e:
                out["ok"] = False
                out["errors"].append(f"{row.get('path')}:{e}")
    return out

