"""Telegram 命令层:命令注册(菜单自愈)+ 长轮询机器人。

设计口径
--------
* **两套命令互不相交**:公开信号机器人只看到 `/start /help /symbols /ping
  /id`(绝不出现账户、持仓、日志);运营机器人只看到 `/status /positions
  /system /host /disk /doctor /logs /symbols /help /start /id`;
* **注册即事实**:`sync_commands()` 先 `deleteMyCommands`(逐 scope × 逐
  language)再 `setMyCommands`,旧版本残留/语言覆盖不会继续显示;
* **授权即事实**:只认配置里的 chat id(可再加 `TELEGRAM_OPS_CHAT_IDS` /
  `TELEGRAM_PUBLIC_CHAT_IDS` 白名单);未知 chat 一律拒绝,公开 chat 发运营
  命令一律拒绝(菜单隐藏 + 服务端二次校验);
* 回复只读本地状态与只读交易所快照,所有文本先过 `ops.redact()`;
* 单实例:`heartbeat` 的 pid 文件 + `SingletonLock`;不阻塞 runner。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from . import __version__, heartbeat as hb, ops, paths

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
HTTP_TIMEOUT_S = 35
POLL_TIMEOUT_S = 25
SEND_LIMIT = 3900
SYNC_LANGUAGES = ("", "zh", "zh-hans", "en")
LOG_MAX_BYTES = 5 * 1024 * 1024


class TelegramError(RuntimeError):
    """只带方法名与 Telegram 描述,绝不含 token/URL。"""


class TelegramAPI:
    """最小 Bot API 客户端(只实现本模块用到的 6 个方法)。"""

    def __init__(self, token: str, *, proxy: Optional[str] = None,
                 timeout: float = HTTP_TIMEOUT_S) -> None:
        if not token:
            raise TelegramError("bot token 为空")
        self._token = token
        self._timeout = timeout
        self.session = requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def call(self, method: str, params: Optional[dict] = None,
             *, timeout: Optional[float] = None) -> object:
        url = TG_API.format(token=self._token, method=method)
        try:
            resp = self.session.post(
                url, json=params or {},
                timeout=timeout if timeout is not None else self._timeout)
        except requests.RequestException as e:
            raise TelegramError(f"{method} 网络错误:{type(e).__name__}") from None
        if resp.status_code == 429:
            retry = 3
            try:
                retry = int((resp.json().get("parameters") or {})
                            .get("retry_after") or 3)
            except ValueError:
                pass
            raise TelegramRateLimited(method, min(retry, 60))
        try:
            payload = resp.json()
        except ValueError:
            raise TelegramError(f"{method} 非 JSON 响应(HTTP {resp.status_code})")
        if resp.status_code != 200 or not payload.get("ok"):
            desc = str(payload.get("description") or "")[:160]
            raise TelegramError(f"{method} 失败(HTTP {resp.status_code}):{desc}")
        return payload.get("result")

    # -- 便捷封装 ---------------------------------------------------------
    def get_me(self) -> dict:
        return self.call("getMe") or {}

    def send_message(self, chat_id: str, text: str, *,
                     reply_to: Optional[int] = None) -> None:
        for chunk in split_text(text, SEND_LIMIT):
            params = {"chat_id": chat_id, "text": chunk,
                      "disable_web_page_preview": "true"}
            if reply_to:
                params["reply_parameters"] = {"message_id": reply_to,
                                              "allow_sending_without_reply": True}
            self.call("sendMessage", params)
            reply_to = None      # 只给首条挂引用

    def get_commands(self, scope: dict, language: str = "") -> list[dict]:
        params: dict = {"scope": scope}
        if language:
            params["language_code"] = language
        return self.call("getMyCommands", params) or []

    def delete_commands(self, scope: dict, language: str = "") -> None:
        params: dict = {"scope": scope}
        if language:
            params["language_code"] = language
        self.call("deleteMyCommands", params)

    def set_commands(self, commands: list[dict], scope: dict,
                     language: str = "") -> None:
        params: dict = {"commands": commands, "scope": scope}
        if language:
            params["language_code"] = language
        self.call("setMyCommands", params)

    def get_updates(self, offset: Optional[int], timeout: int) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset:
            params["offset"] = offset
        result = self.call("getUpdates", params, timeout=timeout + 10)
        return result or []


class TelegramRateLimited(TelegramError):
    def __init__(self, method: str, retry_after: int) -> None:
        super().__init__(f"{method} 限流,{retry_after}s 后重试")
        self.retry_after = retry_after


def split_text(text: str, limit: int = SEND_LIMIT) -> list[str]:
    """按行切分,硬切只在单行超长时发生;保证每条 < 4096。"""
    out: list[str] = []
    buf = ""
    for line in str(text).splitlines():
        while len(line) > limit:                 # 极长单行:硬切
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out or [""]


# ---------------------------------------------------------------------------
# 命令表(两套机器人严格不相交)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str


PUBLIC_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("start", "订阅说明与快捷命令"),
    CommandSpec("help", "查看公开命令列表"),
    CommandSpec("symbols", "当前观察标的"),
    CommandSpec("ping", "机器人在线检查"),
    CommandSpec("id", "显示当前 chat id"),
)

OPS_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("start", "运营台入口"),
    CommandSpec("help", "查看运营命令列表"),
    CommandSpec("status", "进程/心跳/权益/持仓概览"),
    CommandSpec("positions", "本地与交易所持仓对账"),
    CommandSpec("system", "版本/布局/运行时长/计数器"),
    CommandSpec("host", "主机 CPU/内存/磁盘"),
    CommandSpec("disk", "磁盘与各类目录占用"),
    CommandSpec("doctor", "本地自检(不联网)"),
    CommandSpec("logs", "最近日志:logs [live|err|sup|tg] [行数]"),
    CommandSpec("symbols", "观察池与核心标的"),
    CommandSpec("id", "显示当前 chat id"),
)

SPECS = {"public": PUBLIC_SPECS, "ops": OPS_SPECS}

# Telegram 命令名校验:小写字母/数字/下划线,1..32 字符
_NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def validate_specs(specs: Iterable[CommandSpec]) -> None:
    seen: set[str] = set()
    for spec in specs:
        if not (1 <= len(spec.name) <= 32) or not set(spec.name) <= _NAME_OK:
            raise ValueError(f"非法命令名:{spec.name!r}")
        if spec.name in seen:
            raise ValueError(f"重复命令:{spec.name}")
        if not (1 <= len(spec.description) <= 256):
            raise ValueError(f"命令描述长度非法:{spec.name}")
        seen.add(spec.name)


for _specs in SPECS.values():
    validate_specs(_specs)


def menu_payload(role: str) -> list[dict]:
    return [{"command": s.name, "description": s.description}
            for s in SPECS[role]]


@dataclass
class BotIdentity:
    role: str                    # "public" | "ops"
    token: str
    chat_id: str


@dataclass
class CommandContext:
    role: str
    chat_id: str
    cfg: dict
    secrets: dict
    api: TelegramAPI
    bot_username: str = ""


# ---------------------------------------------------------------------------
# 身份与授权
# ---------------------------------------------------------------------------


def _id_set(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw).replace(";", ",").split(",")
    return {str(x).strip() for x in items if str(x).strip()}


def resolve_bots(secrets: dict) -> tuple[list[BotIdentity], dict[str, set[str]]]:
    """返回 (机器人列表, 角色→允许的 chat id 集合)。

    两个 token 相同 ⇒ 单机器人同时服务两种角色,用 chat scope 分开菜单;
    相异 ⇒ 各自默认 scope。
    """
    secrets = secrets or {}
    pub_token = str(secrets.get("TELEGRAM_BOT_TOKEN") or "").strip()
    pub_chat = str(secrets.get("TELEGRAM_CHAT_ID") or "").strip()
    ops_token = str(secrets.get("TELEGRAM_TRADE_BOT_TOKEN") or "").strip()
    ops_chat = str(secrets.get("TELEGRAM_TRADE_CHAT_ID") or "").strip()
    if not ops_token and ops_chat:
        ops_token = pub_token
    if not ops_chat and ops_token == pub_token and pub_chat:
        ops_chat = pub_chat

    public_ids = ({pub_chat} if pub_chat else set()) | _id_set(
        secrets.get("TELEGRAM_PUBLIC_CHAT_IDS"))
    # 运营白名单:显式配置优先;否则只认运营 chat
    ops_extra = _id_set(secrets.get("TELEGRAM_OPS_CHAT_IDS"))
    ops_ids = ({ops_chat} if ops_chat else set()) | ops_extra
    allowed = {"public": public_ids, "ops": ops_ids}

    bots: list[BotIdentity] = []
    if pub_token:
        bots.append(BotIdentity("public", pub_token, pub_chat))
    if ops_token and ops_token != pub_token:
        bots.append(BotIdentity("ops", ops_token, ops_chat))
    elif ops_token and ops_token == pub_token and ops_chat != pub_chat:
        # 同一 token 下的运营 chat:同步时用 chat scope 覆盖
        bots.append(BotIdentity("ops", ops_token, ops_chat))
    return bots, allowed


def role_for_chat(chat_id: str, allowed: dict[str, set[str]]) -> Optional[str]:
    chat_id = str(chat_id)
    if chat_id in allowed.get("ops", set()):
        return "ops"
    if chat_id in allowed.get("public", set()):
        return "public"
    return None


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------


def _fmt_num(v: object, digits: int = 4) -> str:
    try:
        x = float(v)                              # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.{min(digits, 2)}f}"
    return f"{x:.{digits}g}"


def _fmt_pct(frac: object) -> str:
    try:
        return f"{float(frac) * 100:+.2f}%"       # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def _fmt_age(seconds: object) -> str:
    try:
        s = int(float(seconds))                   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "未知"
    if s < 0:
        return "未知"
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _fmt_ms(ms: object) -> str:
    try:
        ts = int(ms) / 1000.0                     # type: ignore[arg-type]
        return time.strftime("%m-%d %H:%M", time.gmtime(ts)) + "Z"
    except (TypeError, ValueError, OSError):
        return "—"


def _arrow(direction: object) -> str:
    try:
        d = int(direction)                        # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "·"
    return "⬆️ 多" if d > 0 else ("⬇️ 空" if d < 0 else "·")


def _pos_line(row: dict) -> str:
    sym = row.get("symbol") or "?"
    d = int(row.get("direction") or 0)
    qty = row.get("qty")
    entry, mark = row.get("entry"), row.get("mark")
    head = f"{_arrow(d)} {sym} · {_fmt_num(qty)}"
    body = (f"💰 入场 {_fmt_num(entry)} · 标记 {_fmt_num(mark)}\n"
            f"🛑 防线 {_fmt_num(row.get('invalid'))}")
    try:
        pnl = (float(mark) - float(entry)) * float(qty) * d  # type: ignore[arg-type]
        risk = abs(float(entry) - float(row.get("invalid"))) * float(qty)  # type: ignore[arg-type]
        r_mult = (pnl / risk) if risk > 0 else None
        body += f" · 浮盈 {_fmt_num(pnl, 2)}"
        if r_mult is not None:
            body += f" ({r_mult:+.2f}R)"
    except (TypeError, ValueError):
        pass
    if row.get("stop") is not None:
        body += f"\n🧱 止损单 {_fmt_num(row.get('stop'))}"
    return f"{head}\n{body}"


def _symbols_text(cfg: dict, secrets: dict, *, ops_role: bool) -> str:
    core = list(cfg.get("symbols_core") or [])
    uni = cfg.get("universe") or {}
    lines = ["📊 观察池"]
    lines.append(f"⭐ 核心 {len(core)} 个")
    if core:
        for i in range(0, len(core), 6):
            lines.append("  " + " · ".join(str(s) for s in core[i:i + 6]))
    if ops_role:
        lines.append(f"🌐 选币上限 {uni.get('max_symbols', '?')} 个")
        lines.append(f"📈 24h 成交额门槛 "
                     f"{_fmt_num(uni.get('min_usdt_volume_24h'), 0)} USDT")
        lines.append(f"🔁 刷新 {uni.get('refresh_minutes', '?')} 分钟")
        lines.append(f"🧮 产品线 {uni.get('product_type', '?')}")
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# 命令处理(只读;启动/停止/下单一律不开放)
# ---------------------------------------------------------------------------


def _help_text(role: str, bot_username: str = "") -> str:
    lines = ["🤖 可用命令"]
    for spec in SPECS[role]:
        suffix = f"@{bot_username}" if bot_username else ""
        lines.append(f"/{spec.name}{suffix} · {spec.description}")
    if role == "ops":
        lines += ["", "🔒 启动/停止/改配置只在服务器上执行,机器人只读。",
                  "📎 /positions 会同时读取交易所持仓(只读 API)。"]
    else:
        lines += ["", "📣 这里只发布信号,不含任何账户与持仓数据。"]
    return "\n".join(lines)


def cmd_start(ctx: CommandContext, args: list[str]) -> str:
    if ctx.role == "public":
        return ("👋 欢迎订阅信号广播\n"
                "📣 本机器人只推送信号与结构图,不含账户信息。\n\n"
                + _help_text("public", ctx.bot_username))
    return ("🛠️ 运营控制台\n"
            "🔒 仅授权 chat 可用;写操作仍只在服务器执行。\n\n"
            + _help_text("ops", ctx.bot_username))


def cmd_help(ctx: CommandContext, args: list[str]) -> str:
    return _help_text(ctx.role, ctx.bot_username)


def cmd_ping(ctx: CommandContext, args: list[str]) -> str:
    return (f"🏓 在线 · v{__version__}\n"
            f"🖥️ {ops.machine_info().get('hostname', '?')}\n"
            f"🧭 {paths.layout_mode()} · {ops.human_bytes(paths.dir_size(paths.log_dir())[0])} 日志")


def cmd_id(ctx: CommandContext, args: list[str]) -> str:
    key = ("TELEGRAM_TRADE_CHAT_ID" if ctx.role == "ops"
           else "TELEGRAM_CHAT_ID")
    return (f"🪪 chat id:{ctx.chat_id}\n"
            f"🎭 角色:{ctx.role}\n"
            f"⚙️ 对应配置项:{key}")


def cmd_symbols(ctx: CommandContext, args: list[str]) -> str:
    return _symbols_text(ctx.cfg, ctx.secrets, ops_role=ctx.role == "ops")


def _pos_rows(cfg: dict) -> list[dict]:
    beat = ops.read_heartbeat(cfg)
    # 心跳里的 `positions` 是**数量**(int),逐笔明细在 `positions_detail`;
    # 直接迭代 `positions` 会在有仓位时抛 TypeError(2026-10-24 修复)。
    detail = beat.get("positions_detail")
    if detail is None and isinstance(beat.get("positions"), list):
        detail = beat["positions"]
    rows = []
    for p in (detail or []):
        if not isinstance(p, dict):
            continue
        rows.append({"symbol": p.get("symbol"),
                     "direction": p.get("direction"),
                     "qty": p.get("qty"), "entry": p.get("entry"),
                     "mark": p.get("mark"), "invalid": p.get("invalid"),
                     "stop": p.get("stop") or p.get("stop_id"),
                     "tf": p.get("tf")})
    return rows or ops.local_positions(cfg)


def cmd_status(ctx: CommandContext, args: list[str]) -> str:
    snap = ops.status_snapshot(ctx.cfg)
    beat = ops.read_heartbeat(ctx.cfg)
    rows = _pos_rows(ctx.cfg)
    breaker = snap.get("breaker") or {}
    alive = snap["runner"]["alive"]
    fresh = snap["runner"].get("heartbeat_age_s")
    icon = "🟢" if alive and fresh is not None and fresh < 180 else (
        "🟡" if alive else "🔴")
    lines = [f"{icon} {'运行中' if alive else '未运行'} · "
             f"{(snap.get('mode') or '?')}",
             f"💓 心跳 {_fmt_age(fresh)} 前",
             f"📦 持仓 {len(rows)} · 挂单 {snap.get('open_intents', 0)}"]
    eq = snap.get("equity")
    if eq is not None:
        lines.append(f"💰 权益 {_fmt_num(eq, 2)} USDT")
    lines.append("🛡️ 熔断 " + ("已触发" if breaker.get("tripped") else "正常"))
    sup = snap.get("supervisor") or {}
    lines.append(f"🖥️ 看护 {'pid ' + str(sup.get('pid')) if sup.get('alive') else '未运行'}"
                 f" · 本小时可重启 {sup.get('restarts_left', '?')}")
    lines.append(f"🕒 {snap.get('at_utc')}")
    return "\n".join(lines)


def cmd_positions(ctx: CommandContext, args: list[str]) -> str:
    rep = ops.positions_report(ctx.cfg, exchange=True)
    lines = [f"📦 持仓对账 · {rep['at_utc']}"]
    local = rep.get("local") or []
    exchange = rep.get("exchange")
    lines.append(f"🧾 本地 {len(local)} 笔 · "
                 f"🏦 交易所 {'—' if exchange is None else str(len(exchange)) + ' 笔'}")
    if not local:
        lines.append("📭 本地账本无持仓")
    for row in local:
        lines.append(_pos_line(row))
    bal = rep.get("balance") or {}
    if bal:
        lines.append(f"💰 权益 {_fmt_num(bal.get('equity'), 2)} · "
                     f"可用 {_fmt_num(bal.get('available'), 2)} · "
                     f"浮盈 {_fmt_num(bal.get('unrealized_pl'), 2)}")
    if exchange:
        seen = {(r.get("symbol"), int(r.get("direction") or 0)) for r in local}
        extra = [p for p in exchange
                 if (p.get("symbol"), int(p.get("direction") or 0)) not in seen]
        if extra:
            lines.append(f"🛰️ 交易所独有 {len(extra)} 笔:")
            for p in extra[:10]:
                lines.append(f"· {_arrow(p.get('direction'))} {p.get('symbol')}"
                             f" · {_fmt_num(p.get('qty'))}")
    mis = rep.get("mismatches") or []
    lines.append("✅ 账本一致" if not mis else f"⚠️ 差异 {len(mis)} 条")
    for m in mis[:10]:
        lines.append(f"· {m}")
    for e in rep.get("errors") or []:
        lines.append(f"❌ {e}")
    return "\n".join(lines)



def cmd_system(ctx: CommandContext, args: list[str]) -> str:
    snap = ops.status_snapshot(ctx.cfg)
    beat = ops.read_heartbeat(ctx.cfg)
    started = beat.get("started_ms")
    up = None
    if started:
        try:
            up = time.time() - float(started) / 1000.0
        except (TypeError, ValueError):
            up = None
    c = snap.get("counters") or {}
    lines = [f"🧩 v{__version__} · {paths.layout_mode()}",
             f"🏠 {paths.app_home()}",
             f"🗂️ 存储 {paths.store_root()}",
             f"⏱️ 运行 {_fmt_age(up)}",
             f"📊 信号 {c.get('signals', 0)} · 下单 {c.get('orders_placed', 0)}"
             f" · 成交 {c.get('fills', 0)} · 平仓 {c.get('exits', 0)}",
             f"⚠️ 对账告警 {c.get('reconcile_alerts', 0)}"
             f" · 下单错误 {c.get('order_errors', 0)}"
             f" · 熔断跳过 {c.get('api_halt_skips', 0)}",
             f"🗃️ state {snap['paths']['state']}",
             f"💓 心跳 {_fmt_age(snap['runner'].get('heartbeat_age_s'))} 前"
             f" · {snap['runner'].get('status') or '?'}"]
    return "\n".join(lines)


def cmd_host(ctx: CommandContext, args: list[str]) -> str:
    info = ops.machine_info()
    mem = info.get("memory") or {}
    disk = info.get("disk") or {}
    load = info.get("loadavg")
    lines = [f"🖥️ {info.get('hostname')} · {info.get('ip') or '—'}",
             f"🧠 {info.get('system')} {info.get('release')}"
             f" · {info.get('machine')} · {info.get('cpu_count')} 核",
             f"🐍 Python {info.get('python')} · pid {info.get('pid')}"]
    if mem:
        lines.append(f"💾 内存 {ops.human_bytes(mem.get('available', 0))} 可用"
                     f" / {ops.human_bytes(mem.get('total', 0))}"
                     f" ({mem.get('used_pct', 0)}% 已用)")
    if disk:
        lines.append(f"💽 磁盘 {ops.human_bytes(disk.get('free', 0))} 可用"
                     f" / {ops.human_bytes(disk.get('total', 0))}"
                     f" ({disk.get('used_pct', 0)}%)")
    if load:
        lines.append("📈 load " + " · ".join(f"{x:.2f}" for x in load))
    return "\n".join(lines)


def cmd_disk(ctx: CommandContext, args: list[str]) -> str:
    disk = paths.disk_usage()
    lines = [f"💽 {disk['path']}",
             f"📦 已用 {ops.human_bytes(disk['used'])}"
             f" / {ops.human_bytes(disk['total'])}"
             f" · 剩余 {ops.human_bytes(disk['free'])} ({disk['used_pct']}%)",
             "🗂️ 目录占用"]
    for name, row in paths.category_usage().items():
        flag = "" if row["exists"] else "(无)"
        lines.append(f"· {name}{flag} {ops.human_bytes(row['bytes'])}"
                     f" · {row['files']} 文件")
    return "\n".join(lines)


def cmd_doctor(ctx: CommandContext, args: list[str]) -> str:
    try:
        rep = ops.doctor(ctx.cfg, exchange=False, require_live=False)
    except Exception as e:                        # noqa: BLE001 回复优先
        return f"❌ 自检失败:{ops.redact(e, ctx.secrets)}"
    counts = rep.get("counts") or {}
    icon = "✅" if rep.get("ok") else "❌"
    lines = [f"{icon} 自检 · ok {counts.get('ok', 0)}"
             f" · warn {counts.get('warn', 0)} · fail {counts.get('fail', 0)}"]
    bad = [c for c in rep.get("checks", [])
           if c["status"] in ("fail", "warn")]
    for c in (bad or rep.get("checks", []))[:24]:
        mark = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}.get(
            c["status"], "·")
        line = f"{mark} {c['name']}"
        if c.get("detail"):
            line += f": {c['detail']}"
        if c.get("hint") and c["status"] != "ok":
            line += f" → {c['hint']}"
        lines.append(line)
    return "\n".join(lines)


_LOG_TARGETS = {
    "live": ("live_stdout.log", "runner 输出"),
    "out": ("live_stdout.log", "runner 输出"),
    "err": ("live_stderr.log", "runner 错误"),
    "error": ("live_stderr.log", "runner 错误"),
    "sup": ("supervisor.jsonl", "看护时间线"),
    "supervisor": ("supervisor.jsonl", "看护时间线"),
    "tg": ("tg_bot.log", "机器人日志"),
}


def cmd_logs(ctx: CommandContext, args: list[str]) -> str:
    target, lines_n = "live", 60
    for tok in args:
        low = tok.lower()
        if low in _LOG_TARGETS:
            target = low
        elif tok.isdigit():
            lines_n = max(5, min(int(tok), 200))
    fname, label = _LOG_TARGETS[target]
    path = paths.log_dir() / fname
    if not path.exists():
        avail = [r["name"] for r in ops.log_files()][:15]
        return (f"📜 暂无 {fname}\n🗂️ 现有日志:"
                + ("、".join(avail) if avail else "无"))
    rows = ops.tail_lines(path, lines_n)
    text = ops.redact("\n".join(rows), ctx.secrets)
    if len(text) > 3300:
        text = "…[前部省略]\n" + text[-3300:]
    return (f"📜 {label} · {fname} · 末 {len(rows)} 行\n"
            f"────────\n{text or '(空)'}")


HANDLERS: dict[str, dict[str, Callable[[CommandContext, list[str]], str]]] = {
    "public": {
        "start": cmd_start, "help": cmd_help, "symbols": cmd_symbols,
        "ping": cmd_ping, "id": cmd_id,
    },
    "ops": {
        "start": cmd_start, "help": cmd_help, "status": cmd_status,
        "positions": cmd_positions, "system": cmd_system, "host": cmd_host,
        "disk": cmd_disk, "doctor": cmd_doctor, "logs": cmd_logs,
        "symbols": cmd_symbols, "id": cmd_id,
    },
}



# ---------------------------------------------------------------------------
# 菜单同步(注册即事实)
# ---------------------------------------------------------------------------


def _default_scope() -> dict:
    return {"type": "default"}


def _chat_scope(chat_id: str) -> dict:
    return {"type": "chat", "chat_id": chat_id}


def _api_for(token: str, secrets: dict) -> TelegramAPI:
    return TelegramAPI(token, proxy=str(secrets.get("TELEGRAM_PROXY") or "")
                       or None)


def sync_commands(secrets: dict, *, purge: bool = True,
                  dry_run: bool = False) -> dict:
    """把两套命令写进 Telegram,并清掉旧版残留(含语言覆盖)。

    串行执行:同一 token 的默认 scope 与 chat scope 必须落在同一机器人上;
    失败只记录,不影响其他 token。
    """
    bots, allowed = resolve_bots(secrets)
    token_roles: dict[str, set[str]] = {}
    for bot in bots:
        token_roles.setdefault(bot.token, set()).add(bot.role)
    report: dict = {"at_utc": ops.utc_now_iso(), "bots": [],
                    "errors": [], "ok": True, "dry_run": dry_run}
    for token, roles in token_roles.items():
        api = _api_for(token, secrets)
        entry: dict = {"roles": sorted(roles), "username": "",
                       "scopes": [], "errors": []}
        try:
            me = api.get_me()
            entry["username"] = str(me.get("username") or "")
        except TelegramError as e:
            entry["errors"].append(str(e))
            report["errors"].append(str(e))
            report["ok"] = False
            report["bots"].append(entry)
            continue
        default_role = "public" if "public" in roles else "ops"
        scopes: list[tuple[dict, str, str]] = [
            (_default_scope(), default_role, "default")]
        for role in sorted(roles):
            for chat in sorted(allowed.get(role, set())):
                if chat:
                    scopes.append((_chat_scope(chat), role, f"{role}:{chat}"))
        for scope, role, label in scopes:
            wanted = menu_payload(role)
            row = {"label": label, "role": role, "scope": scope,
                   "commands": len(wanted), "purged": 0}
            try:
                if dry_run:
                    row["current"] = len(api.get_commands(scope))
                    entry["scopes"].append(row)
                    continue
                if purge:
                    for lang in SYNC_LANGUAGES:
                        try:
                            api.delete_commands(scope, lang)
                            row["purged"] += 1
                        except TelegramError as e:
                            row.setdefault("purge_errors", []).append(
                                f"{lang or 'default'}:{e}")
                api.set_commands(wanted, scope)
                row["verified"] = [
                    c.get("command") for c in api.get_commands(scope)]
                if row["verified"] != [s.name for s in SPECS[role]]:
                    raise TelegramError(f"{label} 校验不一致")
            except TelegramError as e:
                row["error"] = str(e)
                entry["errors"].append(f"{label}: {e}")
                report["errors"].append(f"{label}: {e}")
                report["ok"] = False
            entry["scopes"].append(row)
        report["bots"].append(entry)
    return report



# ---------------------------------------------------------------------------
# 长轮询与命令分发
# ---------------------------------------------------------------------------


def _log_tg(secrets: dict, msg: str) -> None:
    path = paths.log_dir() / "tg_bot.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name("tg_bot.log.1"))
    except OSError:
        pass
    line = f"{ops.utc_now_iso()} {ops.redact(msg, secrets)}"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def parse_command(text: str, username: str = "") -> tuple[str, list[str]]:
    """`/status@bot arg` → ("status", ["arg"]);非命令返回 ("", [])。"""
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        return "", []
    parts = raw.split()
    name = parts[0][1:].split("@", 1)[0].lower()
    if "@" in parts[0] and username:
        target = parts[0].split("@", 1)[1].lower()
        if target != username.lower():
            return "", []
    return name, parts[1:]


def _token_key(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _load_offsets(path: Path) -> dict:
    doc = ops.read_json_file(path)
    offs = doc.get("offsets")
    return offs if isinstance(offs, dict) else {}


def _save_offsets(path: Path, offsets: dict) -> None:
    hb.write_atomic(path, json.dumps(
        {"schema": 1, "offsets": offsets,
         "updated_utc": ops.utc_now_iso()}, ensure_ascii=False))


def _dispatch(ctx: CommandContext, name: str, args: list[str]) -> Optional[str]:
    if name == "id":                              # 配置期也需要拿到 chat id
        return cmd_id(ctx, args)
    allowed_names = {s.name for s in SPECS[ctx.role]}
    if name not in allowed_names:
        other = "ops" if ctx.role == "public" else "public"
        if name in {s.name for s in SPECS[other]}:
            return "⛔ 该命令不属于此机器人。"
        return f"❓ 未知命令 /{name}\n发送 /help 查看可用命令。"
    fn = HANDLERS.get(ctx.role, {}).get(name)
    if fn is None:
        return "❓ 命令未实现。"
    return fn(ctx, args)


def handle_update(update: dict, *, token_roles: set[str],
                  cfg: dict, secrets: dict, api: TelegramAPI,
                  allowed: dict[str, set[str]],
                  bot_username: str = "") -> dict:
    """处理一条 update;返回 {handled, chat_id, command, error}。"""
    msg = (update or {}).get("message") or {}
    text = msg.get("text") or ""
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    frm = msg.get("from") or {}
    out = {"handled": False, "chat_id": chat_id, "command": ""}
    if not chat_id or frm.get("is_bot") or not text.startswith("/"):
        return out
    name, args = parse_command(text, bot_username)
    if not name:
        return out
    out["command"] = name
    role = role_for_chat(chat_id, allowed)
    if role is None:
        if name == "id":
            out["handled"] = True
            api.send_message(chat_id,
                             f"🪪 chat id:{chat_id}\n"
                             "🔒 尚未授权;把它写进 "
                             "TELEGRAM_TRADE_CHAT_ID / TELEGRAM_CHAT_ID 后重启机器人。",
                             reply_to=msg.get("message_id"))
            return out
        _log_tg(secrets, f"deny chat={chat_id} cmd={name} 未授权")
        return out
    if role not in token_roles:
        _log_tg(secrets, f"deny chat={chat_id} cmd={name} 角色越界:{role}")
        return out
    ctx = CommandContext(role=role, chat_id=chat_id, cfg=cfg,
                         secrets=secrets, api=api,
                         bot_username=bot_username)
    out["handled"] = True
    try:
        reply = _dispatch(ctx, name, args)
        if reply:
            api.send_message(chat_id, ops.redact(reply, secrets),
                             reply_to=msg.get("message_id"))
    except TelegramRateLimited as e:
        out["error"] = str(e)
    except Exception as e:                        # noqa: BLE001 命令层不崩
        err = ops.redact(e, secrets)
        out["error"] = err
        _log_tg(secrets, f"error chat={chat_id} cmd={name}: {err}")
        try:
            api.send_message(chat_id, f"❌ 命令执行失败:{err[:300]}")
        except TelegramError:
            pass
    return out


def poll_once(api: TelegramAPI, *, token_roles: set[str], cfg: dict,
              secrets: dict, allowed: dict[str, set[str]], offset: Optional[int],
              bot_username: str = "", timeout: int = 0) -> tuple[int, list[dict]]:
    updates = api.get_updates(offset, timeout)
    results = []
    next_offset = offset or 0
    for upd in updates:
        try:
            next_offset = max(next_offset, int(upd.get("update_id") or 0) + 1)
        except (TypeError, ValueError):
            continue
        results.append(handle_update(
            upd, token_roles=token_roles, cfg=cfg,
            secrets=secrets, api=api, allowed=allowed,
            bot_username=bot_username))
    return next_offset, results



def _poll_loop(bot: BotIdentity, roles: set[str], cfg: dict, secrets: dict,
               allowed: dict[str, set[str]], state_path: Path,
               state_lock, stop_event, *, timeout: int = POLL_TIMEOUT_S,
               once: bool = False) -> dict:
    api = _api_for(bot.token, secrets)
    key = _token_key(bot.token)
    stats = {"role": bot.role, "handled": 0, "errors": []}
    try:
        username = str(api.get_me().get("username") or "")
    except TelegramError as e:
        stats["errors"].append(str(e))
        username = ""
    offset = _load_offsets(state_path).get(key)
    while not stop_event.is_set():
        try:
            offset, results = poll_once(
                api, token_roles=roles, cfg=cfg, secrets=secrets,
                allowed=allowed, offset=offset, bot_username=username,
                timeout=0 if once else timeout)
            if offset:
                with state_lock:
                    offsets = _load_offsets(state_path)
                    offsets[key] = int(offset)
                    _save_offsets(state_path, offsets)
            stats["handled"] += sum(1 for r in results if r.get("handled"))
            for r in results:
                if r.get("error"):
                    stats["errors"].append(r["error"])
        except TelegramRateLimited as e:
            _log_tg(secrets, f"{bot.role} 限流:{e}")
            stop_event.wait(min(10, getattr(e, "retry_after", 3)))
        except TelegramError as e:
            _log_tg(secrets, f"{bot.role} 轮询错误:{e}")
            stop_event.wait(5.0)
        except Exception as e:                    # noqa: BLE001 长轮询不退出
            _log_tg(secrets, f"{bot.role} 未知错误:{type(e).__name__}: {e}")
            stop_event.wait(5.0)
        if once:
            break
    return stats


def run_bots(cfg: dict, secrets: Optional[dict] = None, *, once: bool = False,
             sync: bool = True, purge: bool = True,
             stop_event=None) -> dict:
    """长轮询入口(双机器人)。`once=True` 只拉一轮,供冒烟/测试。"""
    import threading
    secrets = secrets if secrets is not None else ops.load_secrets(cfg)
    bots, allowed = resolve_bots(secrets)
    report: dict = {"ok": False, "at_utc": ops.utc_now_iso(),
                    "bots": [], "errors": []}
    if not bots:
        report["errors"].append("未配置 Telegram bot token")
        return report
    if sync:
        report["sync"] = sync_commands(secrets, purge=purge)
    buckets: dict[str, dict] = {}
    for bot in bots:
        row = buckets.setdefault(bot.token, {"roles": set(), "chat": ""})
        row["roles"].add(bot.role)
        row["chat"] = row["chat"] or bot.chat_id
    lp = ops.live_paths(cfg)
    state_path, pid_path = lp["tg_state"], lp["tg_pid"]
    lock = hb.SingletonLock(lp["tg_lock"])
    if not lock.acquire():
        report["errors"].append("已有 tg-bot 实例在运行")
        return report
    stop_event = stop_event or threading.Event()
    state_lock = threading.Lock()
    threads = []
    hb.write_atomic(pid_path, str(os.getpid()))
    try:
        for token, row in buckets.items():
            bot = BotIdentity("public" if "public" in row["roles"] else "ops",
                              token, row["chat"])
            t = threading.Thread(
                target=_poll_loop,
                args=(bot, row["roles"], cfg, secrets, allowed, state_path,
                      state_lock, stop_event),
                kwargs={"once": once},
                name=f"tg-{bot.role}", daemon=True)
            t.start()
            threads.append(t)
        if once:
            for t in threads:
                t.join(timeout=60)
        else:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=1.0)
                if stop_event.is_set():
                    break
        report["ok"] = True
    except KeyboardInterrupt:
        stop_event.set()
        report["ok"] = True
        report["stopped"] = "keyboard"
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=5)
        try:
            if pid_path.exists() and pid_path.read_text(
                    encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
        lock.release()
    report["bots"] = [{"token_key": _token_key(t),
                       "roles": sorted(r["roles"]),
                       "username": ""} for t, r in buckets.items()]
    return report


def main(cfg: dict, secrets: Optional[dict] = None, **kwargs) -> int:
    report = run_bots(cfg, secrets, **kwargs)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":                        # pragma: no cover
    from . import ops as _ops
    _cfg = _ops.load_config()
    sys.exit(main(_cfg))

