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

from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import time
from types import MappingProxyType
from typing import Any
import zipfile

from . import __version__, heartbeat as hb, live_support, paths
from .timeframes import bitget_granularity, tf_to_seconds, utc_now_iso

_LOG = logging.getLogger(__name__)

CONFIG_REL = Path("config") / "live.json"
STRATEGY_REL = Path("config") / "strategy.json"
SECRETS_REL = Path("config") / "secrets.env"

STATUS_ORDER: Mapping[str, int] = MappingProxyType({"ok": 0, "warn": 1, "fail": 2, "skip": -1})
STATUS_ICON: Mapping[str, str] = MappingProxyType({
    "ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP",
})
LOG_TARGETS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "live": ("live_stdout.log", "runner 输出"),
    "out": ("live_stdout.log", "runner 输出"),
    "err": ("live_stderr.log", "runner 错误"),
    "error": ("live_stderr.log", "runner 错误"),
    "sup": ("supervisor.jsonl", "看护时间线"),
    "supervisor": ("supervisor.jsonl", "看护时间线"),
    "tg": ("tg_bot.log", "机器人日志"),
})


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

human_bytes = paths.human_bytes


def parse_iso_ms(text: str | None) -> int | None:
    return hb.parse_iso(text)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path).expanduser() if path else paths.app_home() / CONFIG_REL
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"配置不是 JSON 对象:{p}")
    return data


def config_path(path: str | Path | None = None) -> Path:
    return Path(path).expanduser() if path else paths.app_home() / CONFIG_REL


def load_secrets(cfg: Mapping[str, Any]) -> dict[str, str]:
    """按配置解析密钥;只返回内存 dict,任何打印都必须先 redact。"""
    from .secrets_loader import load_secrets as _load
    rel = cfg.get("credentials_source")
    p = paths.app_home() / str(rel) if rel else None
    return _load(p if p and p.exists() else None, config_overrides=cfg.get("credentials"))


def load_telegram_secrets(cfg: Mapping[str, Any]) -> dict[str, str]:
    """只装载 Telegram 键(不要求 Bitget 三件套)。"""
    from .secrets_loader import load_secrets as _load
    rel = cfg.get("credentials_source")
    p = paths.app_home() / str(rel) if rel else None
    return _load(
        p if p and p.exists() else None,
        required=(),
        config_overrides=cfg.get("credentials"),
    )


def secret_values(secrets: Mapping[str, Any] | None) -> list[str]:
    if not secrets:
        return []
    vals = [str(v) for v in secrets.values() if isinstance(v, str) and len(v) >= 6]
    return sorted(set(vals), key=len, reverse=True)


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|passphrase|token)(\s*[=:]\s*)([^\s,;'\"]{4,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b"),
)


def redact(text: object, secrets: Mapping[str, Any] | None = None) -> str:
    """把已知密钥值与常见 key=value 形态替换为 ``***`` (日志/回复前必过)。"""
    out = str(text)
    for value in secret_values(secrets):
        if value:
            out = out.replace(value, "***")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    return out


def live_paths(cfg: Mapping[str, Any]) -> dict[str, Path]:
    loop = cfg.get("loop") or {}
    state = paths.resolve(loop.get("state_path", "runtime/live/state.json"))
    hb_rel = loop.get("heartbeat_path")
    heartbeat = paths.resolve(hb_rel) if hb_rel else state.parent / "heartbeat.json"
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


def read_state(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return hb.read_json(live_paths(cfg)["state"])


def read_heartbeat(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return hb.read_heartbeat(live_paths(cfg)["heartbeat"])


def read_json_file(path: str | Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def pid_alive(pid: object) -> bool:
    try:
        return hb.process_alive(int(pid or 0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


_SHELL_NAMES = frozenset({
    "sh", "bash", "dash", "zsh", "ksh", "fish", "pgrep", "timeout", "xargs", "env",
})


def _drop_shell_wrappers(pids: list[int]) -> list[int]:
    """去掉命令行里带着 marker 的壳进程。"""
    if not pids:
        return pids
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,comm=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return pids
    shells: set[int] = set()
    for line in (out.stdout or "").splitlines():
        pid, _, comm = line.strip().partition(" ")
        if pid.isdigit() and comm.strip().rsplit("/", 1)[-1] in _SHELL_NAMES:
            shells.add(int(pid))
    return [p for p in pids if p not in shells]


def find_pids(marker: str, *, exclude_self: bool = True) -> list[int]:
    """按命令行找进程(兜底用;正常路径一律读 pid 文件/心跳)。"""
    marker = str(marker)
    pids: list[int] = []
    if os.name == "nt":
        esc = marker.replace("'", "''")
        ps = (
            "Get-CimInstance Win32_Process "
            "| Where-Object { $_.CommandLine -like '*%s*' -and "
            "$_.Name -notmatch 'powershell|pwsh|cmd|conhost|WmiPrvSE|bash' } "
            "| ForEach-Object { $_.ProcessId }" % esc
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=30,
            )
            pids = [int(x) for x in (out.stdout or "").split() if x.isdigit()]
        except (OSError, subprocess.SubprocessError, ValueError):
            pids = []
    else:
        try:
            out = subprocess.run(
                ["pgrep", "-f", marker], capture_output=True, text=True, timeout=15
            )
            pids = [int(x) for x in (out.stdout or "").split() if x.isdigit()]
        except (OSError, subprocess.SubprocessError, ValueError):
            pids = []
        pids = _drop_shell_wrappers(pids)
    if exclude_self:
        pids = [p for p in pids if p != os.getpid()]
    return sorted(set(pids))


def tail_lines(path: str | Path, lines: int = 80, *, max_bytes: int = 512 * 1024) -> list[str]:
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
    return blob.decode("utf-8", "replace").splitlines()[-n:]


# ---------------------------------------------------------------------------
# 共享持仓格式化(CLI 与 TG 共用,避免跨层引用私有函数)
# ---------------------------------------------------------------------------


def fmt_num(v: object, digits: int = 4) -> str:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.{min(digits, 2)}f}"
    return f"{x:.{digits}g}"


def direction_arrow(direction: object) -> str:
    try:
        d = int(direction)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "·"
    return "⬆️ 多" if d > 0 else ("⬇️ 空" if d < 0 else "·")


def heartbeat_position_rows(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    beat = read_heartbeat(cfg)
    detail = beat.get("positions_detail")
    if detail is None and isinstance(beat.get("positions"), list):
        detail = beat["positions"]
    rows: list[dict[str, Any]] = []
    for p in detail or ():
        if not isinstance(p, Mapping):
            continue
        rows.append({
            "symbol": p.get("symbol"),
            "direction": p.get("direction"),
            "qty": p.get("qty"),
            "entry": p.get("entry"),
            "mark": p.get("mark"),
            "invalid": p.get("invalid"),
            "stop": p.get("stop") or p.get("stop_id"),
            "tf": p.get("tf"),
        })
    return rows


def merged_position_rows(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    """合并本地 state.json 与 heartbeat.json 持仓视图(返回新列表与新字典,不原地修改)。"""
    local = local_positions(cfg)
    if local:
        by_sym = {r.get("symbol"): r for r in heartbeat_position_rows(cfg)}
        merged: list[dict[str, Any]] = []
        for row in local:
            hb_row = by_sym.get(row.get("symbol")) or {}
            out = dict(row)
            if out.get("mark") is None and hb_row.get("mark") is not None:
                out["mark"] = hb_row.get("mark")
            if out.get("stop") is None and hb_row.get("stop") is not None:
                out["stop"] = hb_row.get("stop")
            merged.append(out)
        return merged
    return heartbeat_position_rows(cfg) or local_positions(cfg)


def format_position_line(row: Mapping[str, Any]) -> str:
    """格式化单笔持仓摘要行(CLI 与 Telegram 共用)。"""
    sym = row.get("symbol") or "?"
    d = int(row.get("direction") or 0)
    qty = row.get("qty")
    entry, mark = row.get("entry"), row.get("mark")
    head = f"{direction_arrow(d)} {sym} · {fmt_num(qty)}"
    body = (
        f"💰 入场 {fmt_num(entry)} · 标记 {fmt_num(mark)}\n"
        f"🛑 防线 {fmt_num(row.get('invalid'))}"
    )
    try:
        pnl = (float(mark) - float(entry)) * float(qty) * d  # type: ignore[arg-type]
        risk = abs(float(entry) - float(row.get("invalid"))) * float(qty)  # type: ignore[arg-type]
        r_mult = (pnl / risk) if risk > 0 else None
        body += f" · 浮盈 {fmt_num(pnl, 2)}"
        if r_mult is not None:
            body += f" ({r_mult:+.2f}R)"
    except (TypeError, ValueError):
        _LOG.debug("position PnL skipped due to missing numeric fields for %s", sym)
    if row.get("stop") is not None:
        body += f"\n🧱 止损单 {fmt_num(row.get('stop'))}"
    return f"{head}\n{body}"


# ---------------------------------------------------------------------------
# 自检(doctor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skip")

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
        }

    def render(self) -> str:
        line = f"[{STATUS_ICON.get(self.status, self.status)}] {self.name}"
        if self.detail:
            line += f": {self.detail}"
        if self.hint:
            line += f"  -> {self.hint}"
        return line


def _check_python() -> Check:
    if sys.version_info >= (3, 11):
        return Check("Python", "ok", platform.python_version())
    return Check("Python", "fail", platform.python_version(), "需要 >= 3.11")


def _check_timezones() -> Check:
    """IANA 时区可用(Windows 需要 tzdata 包;否则 sessions/日历全部失效)。"""
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        ZoneInfo("Asia/Tokyo")
    except Exception as exc:
        return Check("时区库", "fail", f"{type(exc).__name__}", "pip install tzdata")
    return Check("时区库", "ok", "America/New_York · Asia/Tokyo")


def _check_sessions_config() -> Check:
    """会话日历配置可加载(结构/时区/正则都在加载时校验)。"""
    from . import sessions
    p = paths.app_home() / "config" / "sessions.json"
    if not p.exists():
        return Check("会话日历", "warn", f"未找到 {p.name}", "回退 legacy_weekend(非加密按旧周末窗口)")
    try:
        cfg = sessions.load_config(p)
    except Exception as exc:
        return Check("会话日历", "fail", f"{type(exc).__name__}: {exc}")
    return Check("会话日历", "ok", f"{len(cfg.get('calendars') or {})} 个日历")


def _check_deps() -> Check:
    import importlib.util
    missing = [m for m in ("numpy", "pandas", "requests") if importlib.util.find_spec(m) is None]
    if missing:
        return Check("依赖", "fail", f"缺少 {', '.join(missing)}", "pip install -e .")
    return Check("依赖", "ok", "numpy / pandas / requests")


def _check_config_file(path: Path) -> Check:
    if not path.exists():
        return Check(path.name, "fail", f"缺失:{path}", "从模板恢复配置文件")
    try:
        cfg = load_config(path)
    except (OSError, ValueError) as exc:
        return Check(path.name, "fail", f"解析失败:{exc}")
    if not cfg.get("risk") or not cfg.get("loop") or not cfg.get("cognition"):
        return Check(path.name, "fail", "缺少 risk/loop/cognition 段")
    return Check(path.name, "ok", f"version={cfg.get('version', '-')}")


def _check_strategy() -> Check:
    p = paths.app_home() / STRATEGY_REL
    if not p.exists():
        return Check("策略配置", "fail", f"缺失:{p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Check("策略配置", "fail", f"解析失败:{exc}")
    if not isinstance(raw, dict):
        return Check("策略配置", "fail", "根对象不是字典")
    if "params" not in raw and "version" not in raw:
        return Check("策略配置", "warn", "缺少 version/params 标识")
    try:
        from .engine import load_strategy_bundle
        load_strategy_bundle(p)
    except Exception as exc:
        return Check("策略配置", "fail", f"策略装配校验失败:{exc}")
    return Check("策略配置", "ok", f"version={raw.get('version', '-')} ({len(raw)} 键)")


def _check_secrets(cfg: Mapping[str, Any]) -> Check:
    try:
        secrets = load_secrets(cfg)
    except Exception as exc:
        return Check("密钥", "fail", redact(exc), "检查 config/secrets.env")
    need = ("BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE")
    missing = [k for k in need if not secrets.get(k)]
    if missing:
        return Check("密钥", "warn", f"缺少 {', '.join(missing)}", "实盘需要 Bitget 三件套;paper 可忽略")
    tg = bool(secrets.get("TELEGRAM_BOT_TOKEN") and secrets.get("TELEGRAM_CHAT_ID"))
    return Check("密钥", "ok", f"Bitget 已配置;Telegram={'已配置' if tg else '未配置'}")


def _check_dirs() -> Check:
    try:
        created = paths.ensure_layout()
    except OSError as exc:
        return Check("运行期目录", "fail", str(exc))
    for name, p in created.items():
        probe = p / ".mo_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return Check("运行期目录", "fail", f"{name}: {exc}")
    return Check("运行期目录", "ok", f"{paths.layout_mode()} / {len(created)} 个类别可写")


def _check_disk(min_free_gb: float) -> Check:
    try:
        du = paths.disk_usage()
    except OSError as exc:
        return Check("磁盘", "fail", str(exc))
    status = "ok" if du["free_gb"] >= min_free_gb else "fail"
    return Check(
        "磁盘",
        status,
        f"剩余 {du['free_gb']:.1f}GB / 已用 {du['used_pct']}%",
        f"低于 {min_free_gb:g}GB 阈值" if status == "fail" else "",
    )


def _check_live(cfg: Mapping[str, Any], require_live: bool) -> Check:
    doc = read_heartbeat(cfg)
    if not doc:
        return Check(
            "实盘心跳",
            "fail" if require_live else "warn",
            "缺失(未运行或刚换目录)",
            "mo start" if require_live else "",
        )
    age = hb.beat_age_seconds(doc)
    pid = doc.get("pid")
    alive = pid_alive(pid)
    if age is None:
        return Check("实盘心跳", "warn", "缺少可解析时间戳")
    if not alive:
        return Check(
            "实盘心跳",
            "fail" if require_live else "warn",
            f"pid={pid} 已不在;age={age:.0f}s",
            "mo start",
        )
    if age <= 120:
        return Check("实盘心跳", "ok", f"pid={pid} status={doc.get('status')} age={age:.0f}s")
    return Check("实盘心跳", "warn", f"pid={pid} status={doc.get('status')} age={age:.0f}s")


def _check_supervisor(cfg: Mapping[str, Any], require_live: bool) -> Check:
    sup = cfg.get("supervisor") or {}
    if not sup.get("enabled", True):
        return Check("守护进程", "skip", "配置未启用")
    pid = supervisor_pid(cfg)
    if pid and pid_alive(pid):
        return Check("守护进程", "ok", f"pid={pid}")
    return Check(
        "守护进程",
        "fail" if require_live else "warn",
        "未运行(无存活 pid)",
        "mo start",
    )


def _check_layout_state(cfg: Mapping[str, Any]) -> Check:
    lp = live_paths(cfg)
    if lp["state"].exists():
        size = lp["state"].stat().st_size
        return Check("布局/state", "ok", f"{paths.layout_mode()} state={size}B")
    legacy = paths.app_home() / "runtime" / "live" / "state.json"
    if paths.is_portable() and legacy.exists():
        return Check(
            "布局/state",
            "fail",
            "portable 下 state 缺失,但旧目录仍有 state",
            "mo migrate-data --move 后重启,或 mo layout legacy",
        )
    if legacy.exists():
        return Check("布局/state", "warn", "state 缺失,旧路径有历史 state")
    return Check("布局/state", "ok", "尚无 state(首次启动)")


def _check_exchange(cfg: Mapping[str, Any]) -> list[Check]:
    """只读联网:公共行情 + 账户/持仓/持仓模式一致性。"""
    from .bitget_live import BitgetError, BitgetLiveClient
    acct = cfg.get("account") or {}
    uni = cfg.get("universe") or {}
    product = str(uni.get("product_type") or "USDT-FUTURES")
    try:
        secrets = load_secrets(cfg)
        client = BitgetLiveClient(secrets, product_type=product)
    except Exception as exc:
        return [Check("交易所", "fail", redact(exc))]

    out: list[Check] = []
    try:
        tickers = client.tickers()
        picked = client.universe(uni, tickers=tickers)
        core = [s for s in (cfg.get("symbols_core") or []) if s in picked]
        status = "ok" if picked else "fail"
        out.append(
            Check(
                "交易所行情",
                status,
                f"{len(tickers)} 合约 / 宇宙 {len(picked)} / core 命中 {len(core)}",
            )
        )
    except BitgetError as exc:
        return [*out, Check("交易所行情", "fail", redact(exc))]

    try:
        positions = client.positions()
        bal = client.balance()
        out.append(
            Check(
                "交易所账户",
                "ok",
                f"持仓 {len(positions)} / equity={bal['equity']:.2f} available={bal['available']:.2f}",
            )
        )
    except BitgetError as exc:
        msg = redact(exc)
        status = "warn" if "缺少 Bitget 凭证" in msg else "fail"
        out.append(Check("交易所账户", status, msg))
        return out

    expected = str(acct.get("position_mode") or "").strip()
    if expected:
        probe = ((cfg.get("symbols_core") or []) or [p["symbol"] for p in positions] or ["BTCUSDT"])[0]
        try:
            actual = client.position_mode(probe)
        except BitgetError as exc:
            out.append(Check("持仓模式", "warn", redact(exc)))
        else:
            if actual and actual != expected:
                out.append(
                    Check(
                        "持仓模式",
                        "fail",
                        f"配置={expected} 交易所={actual}",
                        "runner 会 fail-closed 拒绝启动",
                    )
                )
            else:
                out.append(Check("持仓模式", "ok", f"{actual or expected}({probe})"))
    return out


def build_checks(
    cfg: Mapping[str, Any],
    *,
    exchange: bool = False,
    require_live: bool = False,
    min_free_gb: float = 1.0,
    cfg_file: Path | None = None,
) -> list[Check]:
    checks = [
        _check_python(),
        _check_deps(),
        _check_timezones(),
        _check_sessions_config(),
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


def summarize_checks(checks: Iterable[Check]) -> dict[str, Any]:
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


def doctor(cfg: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    result = summarize_checks(build_checks(cfg, **kwargs))
    result["at_utc"] = utc_now_iso()
    result["layout"] = paths.layout_report()
    result["machine"] = machine_info()
    return result


def machine_info() -> dict[str, Any]:
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = ""
    info: dict[str, Any] = {
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
    except Exception:
        info["memory"] = {}
    try:
        info["disk"] = paths.disk_usage()
    except OSError:
        info["disk"] = {}
    return info


def _memory_info() -> dict[str, Any]:
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
        return {
            "total": stat.ullTotalPhys,
            "available": stat.ullAvailPhys,
            "used_pct": float(stat.dwMemoryLoad),
        }
    try:
        fields: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            fields[key] = int(rest.strip().split()[0]) * 1024
        total = fields.get("MemTotal", 0)
        avail = fields.get("MemAvailable", 0)
        return {
            "total": total,
            "available": avail,
            "used_pct": round(100.0 * (total - avail) / total, 1) if total else 0.0,
        }
    except (OSError, ValueError, IndexError):
        return {}


def uptime_seconds(pid: object) -> float | None:
    """进程已运行秒数(仅 Linux 可精确读 /proc;其他平台返回 None)。"""
    try:
        if os.name == "nt":
            return None
        stat = Path(f"/proc/{int(pid)}/stat").read_text().split()  # type: ignore[arg-type]
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


def _git(home: Path, args: list[str], *, timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
    """执行 git;任何异常都折叠成 CompletedProcess,绝不冒泡到运维命令。"""
    try:
        return subprocess.run(
            ["git", "-C", str(home), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5.0, timeout),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 127, "", f"{type(exc).__name__}: {exc}")


def _scrub_git(text: object, *, lines: int = 20) -> str:
    """git 输出可能带 remote 里的 token,必须脱敏。"""
    raw = str(text or "")
    raw = _GIT_URL_RE.sub(lambda m: f"{m.group('scheme')}***@", raw)
    rows = [r for r in raw.strip().splitlines() if r.strip()]
    return "\n".join(rows[-max(1, lines):])


def git_update(*, check: bool = False, install: bool = True, timeout: float = 300.0) -> dict[str, Any]:
    """一条命令同步代码:git 工作区 ``pull --ff-only``;拷贝部署则安全跳过。"""
    home = paths.app_home()
    rep: dict[str, Any] = {
        "ok": True,
        "home": str(home),
        "is_repo": False,
        "check": bool(check),
        "skipped": False,
        "branch": "",
        "before": "",
        "after": "",
        "changed": False,
        "dirty": 0,
        "behind": None,
        "install": None,
        "pull": None,
        "errors": [],
        "notes": [],
    }
    if not (home / ".git").exists():
        rep["skipped"] = True
        rep["notes"].append("非 git 工作区(代码文件夹拷贝部署):跳过拉取")
        return rep
    inside = _git(home, ["rev-parse", "--is-inside-work-tree"], timeout=30)
    if inside.returncode != 0:
        rep["ok"] = False
        rep["errors"].append(
            "git 不可用或目录损坏: " + _scrub_git(inside.stderr or inside.stdout, lines=3)
        )
        return rep
    rep["is_repo"] = True
    rep["branch"] = _git(home, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=30).stdout.strip()
    rep["before"] = _git(home, ["rev-parse", "--short", "HEAD"], timeout=30).stdout.strip()
    dirty = _git(home, ["status", "--porcelain"], timeout=60).stdout
    rep["dirty"] = len([r for r in dirty.splitlines() if r.strip()])

    if check:
        fetch = _git(home, ["fetch", "--all", "--prune"], timeout=timeout)
        if fetch.returncode != 0:
            rep["ok"] = False
            rep["errors"].append(
                "git fetch 失败: " + _scrub_git(fetch.stderr or fetch.stdout, lines=5)
            )
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
    rep["pull"] = {
        "rc": pull.returncode,
        "out": _scrub_git((pull.stdout or "") + "\n" + (pull.stderr or "")),
    }
    if pull.returncode != 0:
        rep["ok"] = False
        rep["errors"].append(
            "git pull --ff-only 失败(本地改动/无上游/认证): "
            + _scrub_git(pull.stderr or pull.stdout, lines=5)
        )
        return rep
    rep["after"] = _git(home, ["rev-parse", "--short", "HEAD"], timeout=30).stdout.strip()
    rep["changed"] = bool(rep["before"] and rep["after"] and rep["before"] != rep["after"])
    if not rep["changed"]:
        rep["notes"].append("已是最新")
    if install:
        try:
            pip = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", "."],
                cwd=str(home),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(60.0, timeout),
            )
            rep["install"] = {
                "rc": pip.returncode,
                "out": _scrub_git((pip.stdout or "") + "\n" + (pip.stderr or ""), lines=8),
            }
            if pip.returncode != 0:
                rep["ok"] = False
                rep["errors"].append("依赖安装失败(可用 --no-install 跳过并手动执行)")
        except (OSError, subprocess.SubprocessError) as exc:
            rep["ok"] = False
            rep["errors"].append(f"依赖安装异常: {type(exc).__name__}")
    return rep


# ---------------------------------------------------------------------------
# 进程控制 / 状态
# ---------------------------------------------------------------------------


def supervisor_pid(cfg: Mapping[str, Any]) -> int | None:
    lp = live_paths(cfg)
    try:
        pid = int(lp["supervisor_pid"].read_text().strip())
        if pid_alive(pid):
            return pid
    except (OSError, ValueError):
        _LOG.debug("supervisor_pid file unreadable or invalid: %s", lp["supervisor_pid"])
    found = find_pids("live_supervisor.py")
    return found[0] if found else None


def runner_pid(cfg: Mapping[str, Any]) -> int | None:
    doc = read_heartbeat(cfg)
    pid = doc.get("pid")
    return int(pid) if pid_alive(pid) else None


_runner_pid = runner_pid


def tg_pid(cfg: Mapping[str, Any]) -> int | None:
    lp = live_paths(cfg)
    try:
        pid = int(lp["tg_pid"].read_text().strip())
        return pid if pid_alive(pid) else None
    except (OSError, ValueError):
        return None


_tg_pid = tg_pid


def status_snapshot(cfg: Mapping[str, Any]) -> dict[str, Any]:
    lp, state = live_paths(cfg), read_state(cfg)
    doc = read_heartbeat(cfg)
    age = hb.beat_age_seconds(doc)
    sup_pid, run_pid = supervisor_pid(cfg), _runner_pid(cfg)
    bot_pid = _tg_pid(cfg)
    counters = state.get("counters") or {}
    return {
        "at_utc": utc_now_iso(),
        "mode": doc.get("mode") or state.get("mode") or "?",
        "runner": {
            "pid": run_pid,
            "alive": bool(run_pid),
            "heartbeat_age_s": age,
            "status": doc.get("status"),
        },
        "supervisor": {
            "pid": sup_pid,
            "alive": bool(sup_pid),
            "restarts_left": _restarts_left(cfg),
        },
        "tg_bot": {"pid": bot_pid, "alive": bool(bot_pid)},
        "positions": len(state.get("positions") or {}),
        "stops": len(state.get("stops") or {}),
        "equity": state.get("equity", doc.get("equity")),
        "open_intents": _armed_intent_count(state),
        "watches": _armed_intent_count(state, watch=True),
        "entry_orders": _armed_intent_count(state, entry=True),
        "counters": {
            k: counters.get(k, 0)
            for k in (
                "signals", "orders_placed", "fills", "exits",
                "reconcile_alerts", "order_errors", "api_halt_skips",
            )
        },
        "breaker": state.get("breaker") or {},
        "paths": {k: str(v) for k, v in lp.items()},
        "layout": paths.layout_mode(),
        "disk": paths.disk_usage(),
        "usage": paths.category_usage(),
    }


def _armed_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        it for it in (state.get("open_intents") or {}).values()
        if isinstance(it, dict) and it.get("status") == "armed"
    ]


def _armed_intent_count(
    state: Mapping[str, Any],
    *,
    watch: bool = False,
    entry: bool = False,
) -> int:
    rows = _armed_rows(state)
    if watch:
        return sum(1 for it in rows if it.get("watch_entry"))
    if entry:
        return sum(1 for it in rows if it.get("role") == "entry" and not it.get("watch_entry"))
    return len(rows)


def _restarts_left(cfg: Mapping[str, Any]) -> int | None:
    sup = cfg.get("supervisor") or {}
    doc = read_json_file(live_paths(cfg)["supervisor_state"])
    rows = [float(t) for t in (doc.get("restarts") or []) if isinstance(t, (int, float))]
    now = time.time()
    recent = [t for t in rows if now - t < 3600.0]
    return max(0, int(sup.get("max_restarts_per_hour", 6)) - len(recent))


def _invalidate_stale_heartbeat(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """启动看护前作废已死进程留下的心跳,避免白等 stale_seconds。"""
    lp = live_paths(cfg)
    doc = read_heartbeat(cfg)
    if not doc:
        return {"removed": False, "why": "无心跳文件"}
    pid = doc.get("pid")
    if pid and pid_alive(pid):
        return {"removed": False, "why": f"runner {pid} 仍存活,心跳保留"}
    try:
        lp["heartbeat"].unlink()
    except OSError as exc:
        return {"removed": False, "why": f"删除心跳失败: {type(exc).__name__}"}
    return {"removed": True, "why": f"runner {pid} 已不在,心跳作废(免看护白等 stale_seconds)"}


def start_supervisor(
    cfg: Mapping[str, Any],
    *,
    cfg_file: Path | None = None,
    wait_s: float = 20.0,
) -> dict[str, Any]:
    """脱离会话启动看护(由看护拉起 runner),返回 pid 与首个心跳观测。"""
    lp = live_paths(cfg)
    pid = supervisor_pid(cfg)
    if pid:
        return {"started": False, "pid": pid, "why": "已在运行"}
    heartbeat = _invalidate_stale_heartbeat(cfg)
    cfg_path = Path(cfg_file) if cfg_file else config_path()
    argv = [
        sys.executable,
        str(paths.app_home() / "scripts" / "live_supervisor.py"),
        "--config",
        str(cfg_path),
    ]
    out = paths.log_dir() / "supervisor_stdout.log"
    err = paths.log_dir() / "supervisor_stderr.log"
    pid = hb.spawn_detached(argv, cwd=paths.app_home(), stdout_path=out, stderr_path=err)
    if pid:
        hb.write_atomic(lp["supervisor_pid"], str(pid))
    deadline = time.time() + max(0.0, wait_s)
    beat = None
    while time.time() < deadline:
        beat = read_heartbeat(cfg)
        if beat or not pid_alive(pid):
            break
        time.sleep(1.0)
    return {
        "started": bool(pid),
        "pid": pid,
        "alive": pid_alive(pid),
        "heartbeat": bool(beat),
        "heartbeat_reset": heartbeat,
        "stdout": str(out),
        "stderr": str(err),
    }


def register_tasks(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """注册/刷新 Windows 计划任务(幂等;非 Windows 返回 skipped)。

    换机场景: ``mo bootstrap`` 直接调用本函数,新主机只需 clone + secrets.env 即可
    拥有"看护每 3 分钟拉起 + 每日存储治理 + 每周资金费刷新"三项计划任务。
    """
    _ = cfg
    out: dict[str, Any] = {"supported": os.name == "nt", "ok": True, "tasks": []}
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("MO_NO_TASKS"):
        out["skipped"] = "测试/禁用环境(MO_NO_TASKS)"
        return out
    if os.name != "nt":
        out["skipped"] = "非 Windows:用 systemd/cron 承载等价调度"
        return out
    home = paths.app_home()
    specs = (
        ("MarketOperatorLive", ("/SC", "MINUTE", "/MO", "3"), "live_supervisor.cmd"),
        ("MarketOperatorStorageGC", ("/SC", "DAILY", "/ST", "04:10"), "storage_gc.cmd"),
        ("MarketOperatorFunding", ("/SC", "WEEKLY", "/D", "SUN", "/ST", "07:30"), "fetch_funding.cmd"),
    )
    for name, sched, entry in specs:
        script = home / "tools" / "schedule" / entry
        if not script.exists():
            out["ok"] = False
            out["tasks"].append({"name": name, "ok": False, "why": f"缺少入口 {script}"})
            continue
        if name == "MarketOperatorLive":
            pyw = Path(sys.executable).with_name("pythonw.exe")
            if not pyw.exists():
                pyw = Path(sys.executable)
            target = (
                f'"{pyw}" "{home / "scripts" / "live_supervisor.py"}" '
                f'--config "{home / "config" / "live.json"}"'
            )
        else:
            target = f'cmd.exe /c "{script}"'
        argv = ["schtasks", "/Create", "/TN", f"\\MarketOperator\\{name}", "/TR", target, *sched, "/F"]
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            ok = res.returncode == 0
            why = (res.stdout or res.stderr or "").strip().splitlines()
            out["tasks"].append({"name": name, "ok": ok, "why": (why[-1] if why else "")[:160]})
            out["ok"] = out["ok"] and ok
        except Exception as exc:
            out["ok"] = False
            out["tasks"].append({"name": name, "ok": False, "why": f"{type(exc).__name__}: {exc}"})
    return out


def start_tg_bot(
    cfg: Mapping[str, Any],
    *,
    cfg_file: Path | None = None,
    wait_s: float = 10.0,
    sync: bool = True,
) -> dict[str, Any]:
    """脱离会话启动 Telegram 机器人(``mo tg-run``),幂等。"""
    pid = _tg_pid(cfg)
    if pid:
        return {"ok": True, "started": False, "pid": pid, "alive": True, "why": "已在运行"}
    try:
        secrets = load_telegram_secrets(cfg)
    except Exception as exc:
        return {
            "ok": False,
            "started": False,
            "skipped": True,
            "why": f"密钥读取失败: {type(exc).__name__}",
        }
    tokens = {
        str(secrets.get(k) or "").strip()
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TRADE_BOT_TOKEN")
    }
    tokens.discard("")
    if not tokens:
        return {"ok": True, "started": False, "skipped": True, "why": "未配置 Telegram bot token"}
    cfg_path = Path(cfg_file) if cfg_file else config_path()
    entry = paths.app_home() / "scripts" / "mo.py"
    argv = (
        [sys.executable, str(entry)]
        if entry.is_file()
        else [sys.executable, "-m", "market_operator"]
    )
    argv += ["--config", str(cfg_path), "tg-run"]
    if not sync:
        argv.append("--no-sync")
    out = paths.log_dir() / "tg_bot_stdout.log"
    err = paths.log_dir() / "tg_bot_stderr.log"
    try:
        spawned = hb.spawn_detached(argv, cwd=paths.app_home(), stdout_path=out, stderr_path=err)
    except OSError as exc:
        return {"ok": False, "started": False, "why": f"spawn 失败: {type(exc).__name__}: {exc}"}
    if not spawned:
        return {"ok": False, "started": False, "why": "spawn 未返回 pid"}
    deadline = time.time() + max(0.0, wait_s)
    while time.time() < deadline:
        if _tg_pid(cfg) or not pid_alive(spawned):
            break
        time.sleep(0.5)
    pid_file = _tg_pid(cfg)
    return {
        "ok": True,
        "started": True,
        "pid": pid_file or spawned,
        "alive": pid_alive(pid_file or spawned),
        "synced": bool(sync),
        "stdout": str(out),
        "stderr": str(err),
    }


def stop_all(
    cfg: Mapping[str, Any],
    *,
    timeout: float = 15.0,
    include_tg: bool = True,
) -> dict[str, Any]:
    """先停看护(防止它把 runner 再拉起来),再停 runner 与 TG 机器人。"""
    lp = live_paths(cfg)
    actions: list[dict[str, Any]] = []

    def _stop(label: str, pid: int | None, pid_file: Path | None = None) -> None:
        if not pid:
            actions.append({"target": label, "pid": None, "stopped": True})
            return
        ok = hb.terminate_pid(pid, timeout=timeout)
        actions.append({"target": label, "pid": int(pid), "stopped": ok})
        if ok and pid_file is not None:
            with suppress(OSError):
                pid_file.unlink()

    sup_pid = supervisor_pid(cfg)
    if not sup_pid:
        found = find_pids("live_supervisor.py")
        sup_pid = found[0] if found else None
    _stop("supervisor", sup_pid, lp["supervisor_pid"])

    run_pid = _runner_pid(cfg)
    if not run_pid:
        found = find_pids("live_runner.py")
        run_pid = found[0] if found else None
    _stop("runner", run_pid)

    if include_tg:
        _stop("tg_bot", _tg_pid(cfg), lp["tg_pid"])

    return {
        "actions": actions,
        "all_stopped": all(a["stopped"] for a in actions),
        "at_utc": utc_now_iso(),
    }


def restart_all(
    cfg: Mapping[str, Any],
    *,
    cfg_file: Path | None = None,
    warmup_first: bool = False,
    include_tg: bool = True,
    tg_wait_s: float = 10.0,
    **warmup_kw: Any,
) -> dict[str, Any]:
    """重启整条运行链(含看护、runner 与可选 TG 机器人)。"""
    stopped = stop_all(cfg, include_tg=include_tg)
    time.sleep(1.5)
    report: dict[str, Any] = {"stop": stopped}
    if warmup_first:
        report["warmup"] = warmup(cfg, **warmup_kw)
    report["start"] = start_supervisor(cfg, cfg_file=cfg_file)
    if include_tg:
        report["tg"] = start_tg_bot(cfg, cfg_file=cfg_file, wait_s=tg_wait_s)
    else:
        report["tg"] = {"started": False, "skipped": True, "why": "--no-tg", "ok": True}
    return report


def log_files() -> list[dict[str, Any]]:
    root = paths.log_dir()
    rows: list[dict[str, Any]] = []
    if not root.exists():
        return rows
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rows.append({
            "name": p.relative_to(root).as_posix(),
            "path": str(p),
            "bytes": st.st_size,
            "mtime": st.st_mtime,
            "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(
                timespec="seconds"
            ),
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 持仓查看 / 暖机
# ---------------------------------------------------------------------------


def local_positions(cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
    state = read_state(cfg)
    stops = state.get("stops") or {}
    rows: list[dict[str, Any]] = []
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


def local_orders(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """在挂委托 + 保护性止损(只读本地 state,不访问交易所)。"""
    state = read_state(cfg)
    intents: list[dict[str, Any]] = []
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
            "watch_entry": bool(it.get("watch_entry")),
            "tf": it.get("tf_label"),
            "invalid": it.get("invalid"),
            "leverage": it.get("leverage"),
            "event_id": it.get("event_id"),
        })
    intents.sort(key=lambda r: (str(r["symbol"]), str(r["role"])))
    stops: list[dict[str, Any]] = []
    for sym, st in (state.get("stops") or {}).items():
        if not isinstance(st, dict):
            continue
        stops.append({
            "symbol": sym,
            "trigger": st.get("trigger"),
            "qty": st.get("qty"),
            "order_id": st.get("order_id"),
        })
    stops.sort(key=lambda r: str(r["symbol"]))
    return {"at_utc": utc_now_iso(), "intents": intents, "stops": stops}


_GATE_KEYS = (
    "tf_cap_skips", "cap_skips", "rwa_session_skips", "quote_guard_skips",
    "gate_bar_fallbacks", "manage_only_skips",
    "min_notional_skips", "rate_skips", "breaker_blocks",
    "signal_notice_errors", "adopted", "gap_recoveries",
    "unprotected_stop_exits", "protection_unavailable",
    "entry_settle_miss", "exit_settle_miss", "stop_errors", "stop_local_mode",
)


def gate_counters(cfg: Mapping[str, Any]) -> dict[str, int]:
    counters = read_state(cfg).get("counters") or {}
    return {k: int(counters.get(k) or 0) for k in _GATE_KEYS}


def position_mix(cfg: Mapping[str, Any]) -> dict[str, Any]:
    state = read_state(cfg)
    by_tf: dict[str, int] = {}
    long_n = short_n = rwa_n = 0
    for pos in (state.get("positions") or {}).values():
        if not isinstance(pos, dict):
            continue
        tf = str(pos.get("tf_label") or "?")
        by_tf[tf] = by_tf.get(tf, 0) + 1
        d = int(pos.get("direction") or 0)
        if d > 0:
            long_n += 1
        elif d < 0:
            short_n += 1
        if pos.get("is_rwa"):
            rwa_n += 1
    return {
        "n": long_n + short_n,
        "by_tf": by_tf,
        "long": long_n,
        "short": short_n,
        "rwa": rwa_n,
        "equity": state.get("equity"),
    }


def protection_report(cfg: Mapping[str, Any]) -> dict[str, Any]:
    state = read_state(cfg)
    positions = state.get("positions") or {}
    stops = state.get("stops") or {}
    missing: list[str] = []
    mismatch: list[str] = []
    for sym, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        st = stops.get(sym)
        if not isinstance(st, dict) or not (st.get("order_id") or st.get("mode")):
            missing.append(str(sym))
            continue
        try:
            if abs(float(st.get("trigger")) - float(pos.get("invalid"))) > 1e-6:  # type: ignore[arg-type]
                mismatch.append(str(sym))
        except (TypeError, ValueError):
            mismatch.append(str(sym))
    return {
        "missing": missing,
        "mismatch": mismatch,
        "n_pos": len(positions),
        "n_stops": len(stops),
    }


def notify_tail(cfg: Mapping[str, Any], n: int = 8) -> dict[str, Any]:
    counters = read_state(cfg).get("counters") or {}
    path = paths.runtime_dir() / "live" / "tg_missed.jsonl"
    rows: list[str] = []
    try:
        if path.exists():
            rows = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        rows = []
    return {
        "signal_notice_errors": int(counters.get("signal_notice_errors") or 0),
        "missed": rows,
    }


def latest_universe(cfg: Mapping[str, Any]) -> dict[str, Any]:
    _ = cfg
    root = paths.log_dir()
    if not root.exists():
        return {}
    files = sorted(root.glob("live_*.jsonl"))
    if not files:
        return {}
    try:
        raw = files[-1].read_bytes()[-200_000:]
    except OSError:
        return {}
    last: dict[str, Any] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("kind") == "universe":
            last = row
    return last


def position_mismatches(
    local: Iterable[Mapping[str, Any]],
    exchange: Iterable[Mapping[str, Any]],
) -> list[str]:
    ours = {(r["symbol"], int(r["direction"])): r for r in local}
    theirs = {(p["symbol"], int(p["direction"])): p for p in exchange}
    out: list[str] = []
    for key, row in ours.items():
        ex = theirs.get(key)
        if ex is None:
            out.append(f"ours_only {key[0]} dir={key[1]} qty={row['qty']}")
        elif abs(float(ex["qty"]) - float(row["qty"])) > max(1e-9, float(ex["qty"]) * 1e-6):
            out.append(f"qty_mismatch {key[0]} ours={row['qty']} exchange={ex['qty']}")
    for key, p in theirs.items():
        if key not in ours:
            out.append(f"exchange_only {key[0]} dir={key[1]} qty={p['qty']}")
    return out


def positions_report(cfg: Mapping[str, Any], *, exchange: bool = True) -> dict[str, Any]:
    """本系统持仓(本地账本) + 交易所持仓(只读) + 差异。"""
    local = local_positions(cfg)
    out: dict[str, Any] = {
        "at_utc": utc_now_iso(),
        "local": local,
        "runner_alive": bool(_runner_pid(cfg)),
        "heartbeat_age_s": hb.beat_age_seconds(read_heartbeat(cfg)),
        "exchange": None,
        "balance": None,
        "mismatches": [],
        "errors": [],
    }
    if not local and not out["runner_alive"]:
        out["note"] = "本地无持仓且 runner 未运行"
    if not exchange:
        return out
    try:
        from .bitget_live import BitgetLiveClient
        client = BitgetLiveClient(
            load_secrets(cfg),
            product_type=str((cfg.get("universe") or {}).get("product_type") or "USDT-FUTURES"),
        )
        out["exchange"] = client.positions()
        bal = client.balance()
        out["balance"] = {
            "equity": bal["equity"],
            "available": bal["available"],
            "unrealized_pl": bal["unrealized_pl"],
        }
        out["mismatches"] = position_mismatches(local, out["exchange"])
    except Exception as exc:
        out["errors"].append(redact(exc))
    return out


def _tf_seconds(label: str) -> int:
    return tf_to_seconds(label)


def _bitget_gran(label: str) -> str:
    return bitget_granularity(label)


def load_runner_module() -> Any:
    """Return shared live-support helpers (in-package; no upward script import)."""
    return live_support


def _write_candle_cache(
    cfg: Mapping[str, Any],
    symbol: str,
    bars: list[Any],
    *,
    force: bool = False,
) -> Path | None:
    """写 runner 同格式 K 线缓存(``<symbol>_<gran>.json.gz``),只在无缓存时写。"""
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


def _warmup_symbol(
    cfg: Mapping[str, Any],
    client: Any,
    mod: Any,
    params: Any,
    sym: str,
    *,
    tfs: list[str],
    trig_by: Mapping[str, Any],
    base_gran: str,
    base_sec: int,
    bars: int,
    write_cache: bool,
    warnings: list[str],
) -> dict[str, Any]:
    rec: dict[str, Any] = {"bars": 0, "tfs": {}, "cache": None}
    bars_base = client.history_candles(sym, base_gran, limit=max(60, int(bars)))
    rec["bars"] = len(bars_base)
    if len(bars_base) < 30:
        warnings.append(f"{sym} {base_gran} 仅 {len(bars_base)} 根")
    for tf in tfs:
        obs = _tf_seconds(tf)
        trig = int(trig_by.get(str(tf).lower()) or obs)
        if obs < base_sec:
            cog = client.history_candles(sym, _bitget_gran(tf), limit=max(60, int(bars)))
            trig_bars = (
                client.history_candles(sym, _bitget_gran(f"{trig // 60}m"), limit=max(60, int(bars)))
                if trig < obs and trig >= 60 and trig % 60 == 0
                else cog
            )
        else:
            cog = bars_base if obs == base_sec else mod.aggregate_tf(bars_base, obs)
            trig_bars = bars_base if trig <= base_sec else mod.aggregate_tf(bars_base, trig)
        if len(cog) < 5:
            rec["tfs"][tf] = {"bars": len(cog), "skipped": True}
            warnings.append(f"{sym} {tf} 认知 bar 不足")
            continue
        mod.replay_cognition(
            params, sym, cog, trig_bars, obs_tf_seconds=obs, trigger_tf_seconds=trig
        )
        rec["tfs"][tf] = {"bars": len(cog), "trigger_s": trig}
    if write_cache and not _runner_pid(cfg):
        cache_path = _write_candle_cache(cfg, sym, bars_base)
        rec["cache"] = str(cache_path) if cache_path else None
    return rec


def warmup(
    cfg: Mapping[str, Any],
    *,
    symbols: Iterable[str] | None = None,
    timeframes: Iterable[str] | None = None,
    bars: int = 300,
    write_cache: bool = False,
    check_account: bool = True,
) -> dict[str, Any]:
    """沙盒暖机:拉行情/合约 -> 回放认知 -> 只读对账(不写生产 state/heartbeat)。"""
    from .bitget_live import BitgetError, BitgetLiveClient
    mod = load_runner_module()
    raw, params, _rp, _rp_by_tf = mod.load_strategy_params(paths.app_home() / STRATEGY_REL)
    tfs = list(timeframes or raw.get("tf_list") or ["1h", "4h", "1d"])
    core = list(symbols or cfg.get("symbols_core") or [])[:20]
    trig_by = {str(k).lower(): v for k, v in (raw.get("trigger_tf_by_htf") or {}).items()}
    client = BitgetLiveClient(
        load_secrets(cfg),
        product_type=str((cfg.get("universe") or {}).get("product_type") or "USDT-FUTURES"),
    )
    result: dict[str, Any] = {
        "ok": True,
        "symbols": {},
        "timeframes": tfs,
        "errors": [],
        "warnings": [],
        "at_utc": utc_now_iso(),
    }
    if not core:
        try:
            core = list(client.universe(cfg.get("universe") or {}))[:20]
        except Exception as exc:
            _LOG.warning("warmup dynamic universe failed: %s", exc)
            core = []
    if not core:
        result["ok"] = False
        result["errors"].append("未指定暖机标的且动态宇宙为空")
        return result

    try:
        contracts, _contract_raw = mod.load_contracts_live(client)
        result["contracts"] = len(contracts)
    except BitgetError as exc:
        result["ok"] = False
        result["errors"].append(f"合约规格:{redact(exc)}")
        return result

    base_gran = str((cfg.get("cognition") or {}).get("granularity") or "2H")
    base_sec = _tf_seconds(base_gran)
    for sym in core:
        try:
            result["symbols"][sym] = _warmup_symbol(
                cfg,
                client,
                mod,
                params,
                sym,
                tfs=tfs,
                trig_by=trig_by,
                base_gran=base_gran,
                base_sec=base_sec,
                bars=bars,
                write_cache=write_cache,
                warnings=result["warnings"],
            )
        except (BitgetError, RuntimeError, ValueError) as exc:
            result["ok"] = False
            result["errors"].append(f"{sym}:{redact(exc)}")
            result["symbols"][sym] = {"bars": 0, "tfs": {}, "cache": None}

    if check_account:
        try:
            ex_pos = client.positions()
            bal = client.balance()
            result["account"] = {
                "positions": len(ex_pos),
                "equity": bal["equity"],
                "available": bal["available"],
            }
            result["mismatches"] = position_mismatches(local_positions(cfg), ex_pos)
        except BitgetError as exc:
            result["warnings"].append(f"账户对账跳过:{redact(exc)}")
    return result


# ---------------------------------------------------------------------------
# 数据打包(跨机器搬运行期产物)
# ---------------------------------------------------------------------------

_BANNED_NAME_PARTS = (
    "secrets.env", ".env", "id_rsa", "id_ed25519", ".pem", ".key", "credentials",
)
_BANNED_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv"})


def _iter_package_files(cats: Iterable[str]) -> Iterable[tuple[Path, str]]:
    for cat in cats:
        root = paths.category_root(cat)
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(paths.app_home()).as_posix()
            if set(p.parts) & _BANNED_DIRS:
                continue
            low = p.name.lower()
            if any(part in low for part in _BANNED_NAME_PARTS):
                continue
            if low.endswith((".tmp", ".lock", ".pid")):
                continue
            yield p, rel


def _contains_secret(
    path: Path,
    secrets: Mapping[str, Any] | None,
    *,
    max_bytes: int = 2 * 1024 * 1024,
) -> bool:
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


_file_sha256 = paths.file_sha256


def package_data(
    *,
    include_data: bool = False,
    include_reports: bool = False,
    include_backups: bool = False,
    dest: str | Path | None = None,
    secrets: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """打包运行期产物(zip + 清单),用于换机/备份。"""
    cats = ["logs", "runtime"]
    if include_data:
        cats.append("data")
    if include_reports:
        cats.append("reports")
    if include_backups:
        cats.append("backups")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(dest) if dest else paths.backup_dir() / f"mo-{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema": 1,
        "created_at_utc": utc_now_iso(),
        "home": str(paths.app_home()),
        "layout": paths.layout_mode(),
        "categories": cats,
        "machine": machine_info(),
        "files": [],
        "skipped": [],
    }
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, rel in _iter_package_files(cats):
            if _contains_secret(path, secrets):
                manifest["skipped"].append({"path": rel, "why": "含密钥"})
                continue
            try:
                zf.write(path, arcname=rel)
                manifest["files"].append({
                    "path": rel,
                    "bytes": path.stat().st_size,
                    "sha256": _file_sha256(path),
                })
            except OSError as exc:
                manifest["skipped"].append({"path": rel, "why": f"{type(exc).__name__}: {exc}"})
        zf.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    side = out.with_suffix(".manifest.json")
    side.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "archive": str(out),
        "manifest": str(side),
        "files": len(manifest["files"]),
        "skipped": len(manifest["skipped"]),
        "bytes": out.stat().st_size,
        "categories": cats,
    }


def _verify_archive_member(zf: zipfile.ZipFile, row: Mapping[str, Any]) -> str | None:
    """Verify one zip archive member against its expected sha256; return error or None."""
    try:
        digest = hashlib.sha256()
        with zf.open(row["path"]) as fh:
            for blob in iter(lambda: fh.read(1 << 20), b""):
                digest.update(blob)
        if digest.hexdigest() != row.get("sha256"):
            return f"sha256 不一致:{row['path']}"
        return None
    except (KeyError, OSError) as exc:
        return f"{row.get('path')}:{exc}"


def verify_archive(path: str | Path) -> dict[str, Any]:
    """校验 zip 内每个文件与清单 sha256 一致(搬机后先验再解压)。"""
    p = Path(path)
    out: dict[str, Any] = {"ok": True, "archive": str(p), "checked": 0, "errors": []}
    try:
        zf = zipfile.ZipFile(p, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "archive": str(p), "checked": 0, "errors": [str(exc)]}
    with zf:
        try:
            manifest = json.loads(zf.read("MANIFEST.json"))
        except (KeyError, ValueError) as exc:
            return {"ok": False, "archive": str(p), "checked": 0, "errors": [f"MANIFEST.json:{exc}"]}
        for row in manifest.get("files") or ():
            err = _verify_archive_member(zf, row)
            if err is not None:
                out["ok"] = False
                out["errors"].append(err)
            out["checked"] += 1
    return out

