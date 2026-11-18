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
# 长文本切分规则唯一事实在 `tg.py`;回复层复用同一份实现(发送处传 SEND_LIMIT)。
from .tg import split_text

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
HTTP_TIMEOUT_S = 35
POLL_TIMEOUT_S = 25
SEND_LIMIT = 3900
SYNC_LANGUAGES = ("", "zh", "en")
# 清理旧菜单时按语言逐条打 `deleteMyCommands`。Telegram 只认它自己那份语言表:
# `zh` 合法,而 `zh-hans` / `zh-hant` / `zh-tw` 一律 HTTP 400
# "invalid language code"(2026-10-25 实测 getMyCommands / deleteMyCommands 都拒),
# 所以别往里加 —— 加了只会每次 sync 白打一圈 API,再把 PURGE 失败刷进报告。
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
        if not proxy:
            import urllib.request
            sys_p = urllib.request.getproxies()
            proxy = sys_p.get("https") or sys_p.get("http")
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def call(self, method: str, params: Optional[dict] = None,
             *, timeout: Optional[float] = None) -> object:
        url = TG_API.format(token=self._token, method=method)
        req_timeout = timeout if timeout is not None else self._timeout
        resp = None
        for attempt in range(2):
            try:
                resp = self.session.post(url, json=params or {}, timeout=req_timeout)
                break
            except requests.RequestException as e:
                try:
                    self.session.close()
                except Exception:
                    pass
                if attempt == 0:
                    continue
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

    def send_photo(self, chat_id: str, photo_bytes: bytes, caption: str = "", *,
                   reply_to: Optional[int] = None) -> None:
        url = TG_API.format(token=self._token, method="sendPhoto")
        cap = caption[:1000] if caption else ""
        data: dict = {"chat_id": chat_id}
        if cap:
            data["caption"] = cap
        if reply_to:
            data["reply_to_message_id"] = str(reply_to)
        for attempt in range(2):
            try:
                resp = self.session.post(
                    url, data=data,
                    files={"photo": ("levels.png", photo_bytes, "image/png")},
                    timeout=self._timeout)
                if resp.status_code == 200:
                    return
                break
            except requests.RequestException:
                try:
                    self.session.close()
                except Exception:
                    pass
                if attempt == 0:
                    continue
        if caption:
            self.send_message(chat_id, caption, reply_to=reply_to)

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
    CommandSpec("levels", "待交易水平位与结构图: /levels BTC [1h|2h|4h|1d]"),
    CommandSpec("scan", "临墙雷达: 扫描距待交易水平位最近的标的"),
    CommandSpec("top", "多空强弱榜: 全市场二维失衡强弱排行"),
    CommandSpec("frame", "当前信号周期:1d导航 / 4h结构 / 1h扳机"),
    CommandSpec("symbols", "当前观察标的"),
    CommandSpec("ping", "机器人在线检查"),
    CommandSpec("id", "显示当前 chat id"),
)

OPS_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("start", "运营台入口"),
    CommandSpec("help", "查看运营命令列表"),
    CommandSpec("levels", "待交易水平位与结构图: /levels BTC [1h|2h|4h|1d]"),
    CommandSpec("scan", "临墙雷达: 扫描距待交易水平位最近的标的"),
    CommandSpec("top", "多空强弱榜: 全市场二维失衡强弱排行"),
    CommandSpec("risk", "复利档位与风险池: HWM水位/单笔档位/同向槽位"),
    CommandSpec("status", "进程/心跳/权益/持仓概览"),
    CommandSpec("positions", "本地与交易所持仓对账"),
    CommandSpec("orders", "在挂委托、关注与保护止损"),
    CommandSpec("watches", "尚未下单的价格关注"),
    CommandSpec("protection", "有仓无止损或防线不一致"),
    CommandSpec("capacity", "容量上限与持仓笔数"),
    CommandSpec("gates", "闸门跳过计数"),
    CommandSpec("frame", "决策周期与扳机周期"),
    CommandSpec("universe", "最近一次宇宙筛选"),
    CommandSpec("notify", "信号发送失败与漏发"),
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
    if core:
        lines.append(f"⭐ 核心 {len(core)} 个")
        for i in range(0, len(core), 6):
            lines.append("  " + " · ".join(str(s) for s in core[i:i + 6]))
    else:
        lines.append("⭐ 全市场无差别动态扫描(无固定核心标的绑定)")
    if ops_role:
        if uni.get("max_symbols") is not None:
            lines.append(f"🌐 选币上限 {uni.get('max_symbols')} 个")
        crypto_min = (uni.get("crypto") or {}).get(
            "min_usdt_volume_24h", uni.get("min_usdt_volume_24h"))
        rwa_min = (uni.get("rwa") or {}).get("min_usdt_volume_24h", crypto_min)
        if uni.get("rwa_no_volume_filter"):
            lines.append(
                f"📈 24h 成交额门槛 Crypto > {_fmt_num(crypto_min, 0)} USDT · 非Crypto(RWA) 不限"
            )
        else:
            lines.append(
                f"📈 24h 成交额门槛 Crypto > {_fmt_num(crypto_min, 0)} USDT"
                f" · 非Crypto(RWA) > {_fmt_num(rwa_min, 0)} USDT"
            )
        mult = (uni.get("rwa") or {}).get("capacity_multiplier")
        if mult is not None:
            lines.append(f"📉 非加密容量系数 {mult}")
        refresh_s = (cfg.get("loop") or {}).get("universe_refresh_seconds")
        if refresh_s:
            lines.append(f"🔁 宇宙刷新 {int(int(refresh_s) / 60)} 分钟")
        elif uni.get("refresh_minutes") is not None:
            lines.append(f"🔁 刷新 {uni.get('refresh_minutes')} 分钟")
        lines.append(f"🧮 产品线 {uni.get('product_type', '?')}")
    return "\n".join(lines)


def _seconds_label(seconds: int) -> str:
    table = {900: "15m", 1800: "30m", 3600: "1h", 7200: "2h",
             14400: "4h", 43200: "12h", 86400: "1d"}
    if int(seconds) in table:
        return table[int(seconds)]
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return str(seconds)


def _frame_line(cfg: dict) -> str:
    cog = (cfg or {}).get("cognition") or {}
    tfs = list(cog.get("tf_list") or [])
    trig = cog.get("trigger_tf_by_htf") or {}
    parts = []
    for tf in tfs:
        sub = trig.get(tf) if tf in trig else trig.get(str(tf))
        if sub is None and tf in trig:
            parts.append(f"{tf} 导航")
        elif sub is None:
            parts.append(str(tf))
        else:
            parts.append(f"{tf}→{_seconds_label(int(sub))}")
    return "⏱️ 级联 " + (" · ".join(parts) if parts else "未配置")



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


def _heartbeat_pos_rows(cfg: dict) -> list[dict]:
    beat = ops.read_heartbeat(cfg)
    # 心跳里的 `positions` 是**数量**(int),逐笔明细在 `positions_detail`。
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
    return rows


def _pos_rows(cfg: dict) -> list[dict]:
    """账本入场价在 state.json。心跳明细没有 entry,只用来补标记价。"""
    local = ops.local_positions(cfg)
    if local:
        by_sym = {r.get("symbol"): r for r in _heartbeat_pos_rows(cfg)}
        for row in local:
            hb_row = by_sym.get(row.get("symbol")) or {}
            if row.get("mark") is None and hb_row.get("mark") is not None:
                row["mark"] = hb_row.get("mark")
            if row.get("stop") is None and hb_row.get("stop") is not None:
                row["stop"] = hb_row.get("stop")
        return local
    return _heartbeat_pos_rows(cfg) or ops.local_positions(cfg)


def cmd_status(ctx: CommandContext, args: list[str]) -> str:
    snap = ops.status_snapshot(ctx.cfg)
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
    lines.append(_frame_line(ctx.cfg))
    lines.append(f"💓 状态 {snap['runner'].get('status') or '?'}")
    tg = snap.get("tg_bot") or {}
    lines.append("🤖 机器人 " + ("在线" if tg.get("alive") else "未运行"))
    if snap.get("watches"):
        lines.append(f"👁️ 关注 {snap.get('watches')} · 入场 {snap.get('entry_orders', 0)}")
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



def cmd_orders(ctx: CommandContext, args: list[str]) -> str:
    rep = ops.local_orders(ctx.cfg)
    intents = rep.get("intents") or []
    stops = rep.get("stops") or []
    lines = [f"🧾 在挂委托 · {rep['at_utc']}",
             f"⏳ 委托 {len(intents)} 张 · 🛡️ 止损 {len(stops)} 张"]
    if not intents:
        lines.append("📭 无在挂委托")
    for it in intents[:20]:
        if it.get("watch_entry"):
            label = "关注"
        elif it.get("role") == "entry":
            label = "入场"
        else:
            label = "管理"
        kind = "限价" if it.get("kind") == "limit" else "市价"
        row = (f"· {_arrow(it.get('direction'))} {it.get('symbol')} "
               f"· {label}{kind} · {_fmt_num(it.get('qty'))}")
        if it.get("limit_price") is not None:
            row += f" @ {_fmt_num(it.get('limit_price'))}"
        lines.append(row)
        lines.append(f"  到期 {_fmt_ms(it.get('expiry_ms'))} · "
                     f"id {str(it.get('exchange_order_id') or '—')[:16]}")
    for st in stops[:20]:
        lines.append(f"· 🛡️ {st.get('symbol')} 触发 {_fmt_num(st.get('trigger'))}"
                     f" · {_fmt_num(st.get('qty'))}")
    if len(intents) > 20:
        lines.append(f"…另有 {len(intents) - 20} 张未显示")
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


def cmd_frame(ctx: CommandContext, args: list[str]) -> str:
    cog = (ctx.cfg or {}).get("cognition") or {}
    lines = ["🧭 信号口径", _frame_line(ctx.cfg),
             "1d 白名单为空时日线只导航,不开仓。"]
    raw = cog.get("trigger_tf_by_htf") or {}
    if "1d" in raw and raw.get("1d") is None:
        lines.append("当前 1d 没有下级扳机。")
    if ctx.role == "ops":
        lines.append("原始 K 线缓存粒度 "
                     + str(cog.get("granularity") or "未写"))
    return "\n".join(lines)


def cmd_capacity(ctx: CommandContext, args: list[str]) -> str:
    risk = (ctx.cfg or {}).get("risk") or {}
    uni = (ctx.cfg or {}).get("universe") or {}
    mix = ops.position_mix(ctx.cfg)
    caps = risk.get("tf_cap_by_tf") or {}
    cap_txt = " · ".join(f"{k} {float(v):.0%}" for k, v in caps.items()) or "—"
    lines = [
        "📊 容量(配置,不重算准入)",
        f"组合上限 {float(risk.get('portfolio_cap_frac') or 0):.0%}"
        f" · 同向上限 {float(risk.get('directional_cap_frac') or 0):.0%}",
        f"周期上限 {cap_txt}",
        f"非加密系数 {(uni.get('rwa') or {}).get('capacity_multiplier', '—')}",
        f"持仓 {mix['n']} · 多 {mix['long']} · 空 {mix['short']} · 非加密 {mix['rwa']}",
    ]
    if mix["by_tf"]:
        lines.append("周期笔数 " + " · ".join(
            f"{k} {v}" for k, v in sorted(mix["by_tf"].items())))
    if mix.get("equity") is not None:
        lines.append(f"权益 {_fmt_num(mix.get('equity'), 2)}")
    return "\n".join(lines)


def cmd_gates(ctx: CommandContext, args: list[str]) -> str:
    labels = {
        "tf_cap_skips": "周期容量", "cap_skips": "容量跳过",
        "rwa_session_skips": "非加密休市", "manage_only_skips": "只管理出场",
        "min_notional_skips": "最小下单", "rate_skips": "频率",
        "breaker_blocks": "当日熔断", "signal_notice_errors": "信号发送失败",
        "adopted": "接管", "gap_recoveries": "缺口补做",
        "unprotected_stop_exits": "保护失效离场",
        "protection_unavailable": "挂不上止损",
        "entry_settle_miss": "开仓回执缺失", "exit_settle_miss": "离场回执缺失",
        "stop_errors": "止损错误", "stop_local_mode": "本地止损",
    }
    counts = ops.gate_counters(ctx.cfg)
    lines = ["🚦 闸门计数"]
    for key, label in labels.items():
        lines.append(f"· {label} {counts.get(key, 0)}")
    return "\n".join(lines)


def cmd_watches(ctx: CommandContext, args: list[str]) -> str:
    rep = ops.local_orders(ctx.cfg)
    rows = [it for it in (rep.get("intents") or []) if it.get("watch_entry")]
    lines = [f"👁️ 价格关注 {len(rows)}"]
    if not rows:
        lines.append("📭 没有关注中的标的")
    for it in rows[:20]:
        lines.append(
            f"· {_arrow(it.get('direction'))} {it.get('symbol')}"
            f" · {it.get('tf') or '—'}"
            f" · 触发 {_fmt_num(it.get('limit_price'))}"
            f" · 失效 {_fmt_num(it.get('invalid'))}")
        lines.append(f"  到期 {_fmt_ms(it.get('expiry_ms'))}")
    return "\n".join(lines)


def cmd_protection(ctx: CommandContext, args: list[str]) -> str:
    rep = ops.protection_report(ctx.cfg)
    lines = [f"🛡️ 保护 · 仓 {rep['n_pos']} · 止损记录 {rep['n_stops']}"]
    missing, mismatch = rep["missing"], rep["mismatch"]
    lines.append("✅ 本地保护记录齐全" if not missing and not mismatch
                 else f"⚠️ 缺记录 {len(missing)} · 价不一致 {len(mismatch)}")
    for sym in missing[:15]:
        lines.append(f"· 无止损记录 {sym}")
    for sym in mismatch[:15]:
        lines.append(f"· 防线不一致 {sym}")
    return "\n".join(lines)


def cmd_notify(ctx: CommandContext, args: list[str]) -> str:
    rep = ops.notify_tail(ctx.cfg)
    lines = [f"📣 信号发送失败 {rep['signal_notice_errors']}"]
    missed = rep.get("missed") or []
    if not missed:
        lines.append("📭 没有漏发记录")
    else:
        lines.append(f"漏发末 {len(missed)} 条")
        for row in missed:
            lines.append(str(row)[:180])
    return "\n".join(lines)


def cmd_universe(ctx: CommandContext, args: list[str]) -> str:
    row = ops.latest_universe(ctx.cfg)
    if not row:
        return "🌐 尚未落宇宙日志"
    head = row.get("head") or []
    names = " · ".join(str(s) for s in list(head)[:8])
    lines = [f"🌐 宇宙 {row.get('n', '?')} 个"]
    if names:
        lines.append(names)
    return "\n".join(lines)


def _fetch_bars_for_level_query(symbol: str, tf_label: str) -> list[tuple]:
    """为 /levels 查询获取指定标的与尺度的 K 线(优先实时 Bitget 公开行情,离线回退本地 CSV)。"""
    gran_map = {
        "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H",
        "4h": "4H", "12h": "12Hutc", "1d": "1Dutc",
    }
    gran = gran_map.get(tf_label.lower(), "4H")
    try:
        resp = requests.get(
            "https://api.bitget.com/api/v2/mix/market/candles",
            params={"productType": "USDT-FUTURES", "symbol": symbol, "granularity": gran, "limit": "180"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = (resp.json() or {}).get("data") or []
            if len(data) >= 20:
                rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in data]
                rows.sort(key=lambda b: b[0])
                return rows
    except Exception:
        pass
    csv_p = paths.app_home() / "data" / "raw" / f"{symbol}_{tf_label.lower()}.csv"
    if csv_p.exists():
        out = []
        for ln in csv_p.read_text(encoding="utf-8").splitlines()[1:]:
            if ln.strip():
                p = ln.split(",")
                if len(p) >= 6:
                    out.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
        return out[-180:]
    return []


def cmd_levels(ctx: CommandContext, args: list[str]) -> str:
    """查询任意标的在 15m/30m/1h/2h/4h/12h/1d 尺度的待交易原子水平位并直发结构配图。"""
    from .events import EventDetector
    from .hypothesis import StructureView
    from .level import LevelKind, LevelRole
    from .routing import infer_2d_barrier_topology
    from .structure import Params
    from .tg import render_level_map_png

    valid_tfs = {"15m": 0, "30m": 0, "1h": 1, "2h": 2, "4h": 4, "12h": 12, "1d": 24}
    sym, tf = "BTCUSDT", "4h"
    for a in args:
        al = a.strip().lower()
        if al in valid_tfs:
            tf = al
        elif al:
            s_up = a.strip().upper()
            sym = s_up if s_up.endswith("USDT") else f"{s_up}USDT"

    bars = _fetch_bars_for_level_query(sym, tf)
    if len(bars) < 20:
        return f"⚠️ 未获取到 {sym} [{tf}] 足够 K 线数据(用法: /levels BTC [1h|2h|4h|1d])。"

    det = EventDetector(Params())
    for b in (bars[:-1] if len(bars) > 35 else bars)[-160:]:
        det.update(b)
    s = det.structure
    cur_px = float(bars[-1][4])
    atr, reg, imb = float(s.atr() or 0.0), s.regime(), float(s.imbalance_strength())
    view = StructureView(
        regime=reg, zones=s.levels, atr=atr, pivots=s.pivots[-8:],
        macro_imbalance=s.macro_imbalance(), tf_hours=valid_tfs.get(tf, 4), imbalance_strength=imb,
    )
    topo, _, _ = infer_2d_barrier_topology(cur_px, view)
    med_prom = view.med_prominence_2d
    active = [lv for lv in s.levels if lv.is_active]
    res_list = sorted([lv for lv in active if lv.role is LevelRole.RESISTANCE], key=lambda lv: abs(lv.band_lo - cur_px))[:4]
    sup_list = sorted([lv for lv in active if lv.role is LevelRole.SUPPORT], key=lambda lv: abs(cur_px - lv.band_hi))[:4]

    def _lv_desc(prefix: str, lv) -> str:
        dom = (lv.kind is LevelKind.ORIGIN) or (lv.prominence_2d > med_prom)
        star = "★主导" if dom else "次级"
        k_txt = (
            f"极性互换位(FLIP·{star})" if lv.flipped
            else ("宏观起源墙(ORIGIN★)" if lv.kind is LevelKind.ORIGIN
                  else (f"等高池(EQUAL·{lv.touches}触·{star})" if lv.kind is LevelKind.EQUAL else f"波段位(SWING·{star})"))
        )
        ref_p = lv.band_lo if lv.role is LevelRole.RESISTANCE else lv.band_hi
        dist = (ref_p - cur_px) / cur_px * 100.0 if cur_px > 0 else 0.0
        st_txt = "🔥【已入带博弈!】" if (lv.band_lo <= cur_px <= lv.band_hi) else f"距现价 {dist:+.2f}%"
        return (
            f"· {prefix} {k_txt} · {st_txt}\n"
            f"  带: [{_fmt_num(lv.band_lo, 6)} ~ {_fmt_num(lv.band_hi, 6)}] (颈 {_fmt_num(lv.neck, 6)} / 极 {_fmt_num(lv.extreme, 6)})"
        )

    lines = [
        f"🗺️ {sym} · [{tf.upper()}] 待交易原子水平位",
        f"💰 现价: {_fmt_num(cur_px, 6)}  |  ATR: {_fmt_num(atr, 6)} ({atr / cur_px * 100:.2f}%)" if cur_px > 0 else f"💰 现价: {_fmt_num(cur_px, 6)}",
        f"🧭 状态: {reg.value.upper()} · 强弱分 {imb:+.2f}  |  拓扑: {topo.value.upper()}",
        "",
        f"🔴 上方待交易阻力带 ({len(res_list)} 条):",
        *([_lv_desc(f"R{i}", lv) for i, lv in enumerate(res_list, 1)] or ["· 上方无近景阻力墙(净空真空区)"]),
        "",
        f"🟢 下方待交易支撑带 ({len(sup_list)} 条):",
        *([_lv_desc(f"S{i}", lv) for i, lv in enumerate(sup_list, 1)] or ["· 下方无近景支撑墙(净空真空区)"]),
    ]
    text_out = "\n".join(lines)
    png = render_level_map_png(
        sym, tf.upper(), bars, res_list + sup_list,
        regime_text=reg.value.upper(), topo_text=topo.value.upper(), atr=atr,
    )
    if png:
        ctx.api.send_photo(ctx.chat_id, png, caption=text_out[:980])
        return text_out if len(text_out) > 980 else ""
    return text_out


def _iter_cached_4h_structures(max_syms: int = 80) -> list[tuple[str, float, object]]:
    """从本地 K 线缓存快速重建 4H 结构,供 /scan 与 /top 雷达使用。"""
    import gzip
    from .events import EventDetector
    from .structure import Params

    cdir = paths.runtime_dir() / "live" / "candles"
    if not cdir.exists():
        return []
    files = sorted(cdir.glob("*_2H.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_syms]
    out, p_cfg, step_4h = [], Params(), 4 * 3_600_000
    for f in files:
        sym = f.name[:-11]
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                raw2h = json.load(fh)
            if len(raw2h) < 40:
                continue
            buckets: dict[int, list] = {}
            for r in raw2h[-340:]:
                buckets.setdefault(int(r[0]) // step_4h, []).append(r)
            bars4h = [
                (b_id * step_4h, float(g[0][1]), max(float(x[2]) for x in g),
                 min(float(x[3]) for x in g), float(g[-1][4]), sum(float(x[5]) for x in g))
                for b_id, g in sorted(buckets.items())
            ]
            if len(bars4h) < 25:
                continue
            det = EventDetector(p_cfg)
            for b in bars4h[:-1][-160:]:
                det.update(b)
            out.append((sym, float(bars4h[-1][4]), det.structure))
        except Exception:
            continue
    return out


def cmd_scan(ctx: CommandContext, args: list[str]) -> str:
    """全市场临墙雷达:扫描当前距离待交易支撑/阻力水平位最近的 Top 12 标的。"""
    from .level import LevelKind, LevelRole

    items = _iter_cached_4h_structures(max_syms=100)
    if not items:
        return "📡 本地 K 线缓存尚在预热,可直接用 `/levels BTC 4h` 查询指定标的。"
    hits = []
    for sym, cur_px, s in items:
        if cur_px <= 0:
            continue
        active = [lv for lv in s.levels if lv.is_active]
        if not active:
            continue
        best_lv, best_dist = None, 1e9
        for lv in active:
            if lv.band_lo <= cur_px <= lv.band_hi:
                dist = 0.0
            elif lv.role is LevelRole.RESISTANCE:
                dist = abs(lv.band_lo - cur_px) / cur_px * 100.0
            else:
                dist = abs(cur_px - lv.band_hi) / cur_px * 100.0
            if dist < best_dist:
                best_dist, best_lv = dist, lv
        if best_lv is not None:
            hits.append((best_dist, sym, cur_px, best_lv, s.regime().value))
    hits.sort(key=lambda x: x[0])
    lines = ["📡 全市场 [4H] 临墙待交易雷达 (Top 12)", ""]
    for dist, sym, px, lv, reg in hits[:12]:
        r_tag = "🔴阻力" if lv.role is LevelRole.RESISTANCE else "🟢支撑"
        k_tag = "FLIP" if lv.flipped else ("ORIG" if lv.kind is LevelKind.ORIGIN else ("EQL" if lv.kind is LevelKind.EQUAL else "SWG"))
        st = "🔥入带博弈" if dist == 0.0 else f"距墙 {dist:.2f}%"
        lines.append(f"· {sym:<10} {r_tag}[{k_tag}] · {st}\n  现价 {_fmt_num(px, 5)} | 带 [{_fmt_num(lv.band_lo, 5)}~{_fmt_num(lv.band_hi, 5)}] ({reg})")
    lines.append("\n💡 发送 `/levels 币种 4h` (如 `/levels SOL 4h`) 获取高清水平位图。")
    return "\n".join(lines)


def cmd_top(ctx: CommandContext, args: list[str]) -> str:
    """全市场多空二维失衡强弱排行榜。"""
    items = _iter_cached_4h_structures(max_syms=100)
    if not items:
        return "📊 本地 K 线缓存尚在预热,请稍后再试。"
    scored = [(float(s.imbalance_strength()), sym, px, s.regime().value) for sym, px, s in items]
    scored.sort(key=lambda x: x[0], reverse=True)
    bulls = [x for x in scored if x[0] > 0][:6]
    bears = [x for x in reversed(scored) if x[0] < 0][:6]
    lines = ["🏆 全市场 [4H] 二维失衡强弱榜", "", "🚀 多头领涨最强 Top 6:"]
    for sc, sym, px, reg in bulls:
        lines.append(f"· {sym:<10} 强弱 {sc:+5.2f} · 现价 {_fmt_num(px, 5)} ({reg})")
    lines += ["", "🌊 空头领跌最强 Top 6:"]
    for sc, sym, px, reg in bears:
        lines.append(f"· {sym:<10} 强弱 {sc:+5.2f} · 现价 {_fmt_num(px, 5)} ({reg})")
    lines.append("\n💡 发送 `/levels 币种 4h` 可直接绘出该标的待交易水平位图。")
    return "\n".join(lines)


def cmd_risk(ctx: CommandContext, args: list[str]) -> str:
    """查看当前阶梯复利档位、HWM 水位、总组合与同向风险池占用。"""
    st = ops.read_state(ctx.cfg)
    risk = ctx.cfg.get("risk") or {}
    eq = float(st.get("equity") or 0.0)
    tiers = risk.get("dynamic_risk_tiers") or []
    cur_rf = float(risk.get("risk_frac") or 0.05)
    next_tier_eq = None
    for max_eq, rf in tiers:
        if eq <= float(max_eq):
            cur_rf = float(rf)
            next_tier_eq = float(max_eq)
            break
    p_cap = float(risk.get("portfolio_cap_frac") or 0.48)
    d_cap = float(risk.get("directional_cap_frac") or 0.32)
    positions = st.get("positions") or {}
    long_n = sum(1 for p in positions.values() if isinstance(p, dict) and int(p.get("direction") or 0) > 0)
    short_n = sum(1 for p in positions.values() if isinstance(p, dict) and int(p.get("direction") or 0) < 0)
    max_same_slots = int(d_cap / (cur_rf * 1.12)) if cur_rf > 0 else 0
    max_tot_slots = int(p_cap / (cur_rf * 1.12)) if cur_rf > 0 else 0
    lines = [
        "🛡️ 阶梯动态复利与风险容量池 (HWM 单向降档)",
        f"💰 当前权益: {_fmt_num(eq, 2)} USDT",
        f"🎯 当前单笔风险率: {cur_rf * 100:.2f}% (每笔可承受止损 ≈ {_fmt_num(eq * cur_rf, 2)} USDT)",
        f"📈 下一降档门槛: {'≤ ' + _fmt_num(next_tier_eq, 0) + ' USDT' if next_tier_eq else '已达最高档'}",
        f"📦 总组合容量上限: {p_cap * 100:.0f}% (约 {max_tot_slots} 个标准槽位 · 当前在持 {len(positions)} 笔)",
        f"🧭 同向净风险上限: {d_cap * 100:.0f}% (同向最多约 {max_same_slots} 笔 · 当前多 {long_n} / 空 {short_n})",
        "📊 阶梯档位表: " + " -> ".join(f"≤{int(m)}U:{r*100:g}%" for m, r in tiers),
    ]
    return "\n".join(lines)


ALIASES: dict[str, str] = {
    "map": "levels",
    "chart": "levels",
    "radar": "scan",
    "rank": "top",
    "pos": "positions",
}

HANDLERS: dict[str, dict[str, Callable[[CommandContext, list[str]], str]]] = {
    "public": {
        "start": cmd_start, "help": cmd_help, "levels": cmd_levels,
        "scan": cmd_scan, "top": cmd_top, "frame": cmd_frame,
        "symbols": cmd_symbols, "ping": cmd_ping, "id": cmd_id,
    },
    "ops": {
        "start": cmd_start, "help": cmd_help, "levels": cmd_levels,
        "scan": cmd_scan, "top": cmd_top, "risk": cmd_risk,
        "status": cmd_status, "positions": cmd_positions, "orders": cmd_orders,
        "watches": cmd_watches, "protection": cmd_protection,
        "capacity": cmd_capacity, "gates": cmd_gates, "frame": cmd_frame,
        "universe": cmd_universe, "notify": cmd_notify,
        "system": cmd_system, "host": cmd_host,
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


def _chat_admin_scope(chat_id: str) -> dict:
    return {"type": "chat_administrators", "chat_id": chat_id}


# Telegram 的全局 scope:旧版/DM/群组/群管都可能缓存着**整份旧命令表**。
# 只清 `default` 时,客户端仍可能按更具体的 scope 命中残留 —— 必须逐个清空。
GLOBAL_SCOPES: tuple[tuple[str, dict], ...] = (
    ("default", _default_scope()),
    ("all_private_chats", {"type": "all_private_chats"}),
    ("all_group_chats", {"type": "all_group_chats"}),
    ("all_chat_administrators", {"type": "all_chat_administrators"}),
)

# 清理后仍需为空(我们从不写全局覆盖,残留说明旧注册没清掉)。
PURGE_ONLY_LABELS = ("all_private_chats", "all_group_chats",
                     "all_chat_administrators")


def _api_for(token: str, secrets: dict) -> TelegramAPI:
    return TelegramAPI(token, proxy=str(secrets.get("TELEGRAM_PROXY") or "")
                       or None)


def _invalid_language(err: object) -> bool:
    """Telegram 对不认识的语言码返回 400 invalid language code;按跳过处理。"""
    return "invalid language code" in str(err).lower()


def sync_commands(secrets: dict, *, purge: bool = True,
                  dry_run: bool = False) -> dict:
    """把两套命令写进 Telegram,并清掉旧版残留(含全局 scope 与语言覆盖)。

    执行顺序(每个 token 独立)
    --------------------------
    1. 清空:default / all_private_chats / all_group_chats /
       all_chat_administrators,以及已知 chat 的 chat + chat_administrators
       scope,逐语言 `deleteMyCommands`;
    2. 写入:default(该 token 的主角色)+ 每个已知 chat 的 chat scope;
    3. 复核:逐 scope `getMyCommands` 必须等于目标表;**只清不写**的全局
       scope 必须为空,否则报错 —— 残留命令会让用户看到"可点但无权"的菜单。

    失败只记录,不影响其他 token。
    """
    bots, allowed = resolve_bots(secrets)
    token_roles: dict[str, set[str]] = {}
    for bot in bots:
        token_roles.setdefault(bot.token, set()).add(bot.role)
    report: dict = {"at_utc": ops.utc_now_iso(), "bots": [],
                    "errors": [], "ok": True, "dry_run": dry_run,
                    "purge": bool(purge)}
    for token, roles in token_roles.items():
        api = _api_for(token, secrets)
        entry: dict = {"roles": sorted(roles), "username": "",
                       "scopes": [], "purged": [], "residual": {},
                       "errors": []}
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
        chats = sorted({chat for role in roles
                        for chat in allowed.get(role, set()) if chat})

        purge_targets: list[tuple[str, dict]] = list(GLOBAL_SCOPES)
        for chat in chats:
            purge_targets.append((f"chat:{chat}", _chat_scope(chat)))
            purge_targets.append(
                (f"chat_admin:{chat}", _chat_admin_scope(chat)))

        if dry_run:
            # 干跑也必须把"将要清哪些 scope"列全:只看 default 会漏掉客户端缓存的
            # 全局菜单 —— 那正是"点得到但无权执行"的来源,清场前就得看得见。
            for label, scope in purge_targets:
                row: dict = {"label": label, "scope": scope, "purged": 0,
                             "current": {}}
                for lang in SYNC_LANGUAGES:
                    key = lang or "default"
                    try:
                        row["current"][key] = [c.get("command") for c in
                                               api.get_commands(scope, lang)]
                    except TelegramError as e:
                        row["current"][key] = f"ERR {e}"
                entry["purged"].append(row)

        if purge and not dry_run:
            for label, scope in purge_targets:
                row: dict = {"label": label, "scope": scope, "purged": 0}
                for lang in SYNC_LANGUAGES:
                    try:
                        api.delete_commands(scope, lang)
                        row["purged"] += 1
                    except TelegramError as e:
                        if _invalid_language(e):
                            row.setdefault("skipped_languages", []).append(
                                lang or "default")
                            continue
                        row.setdefault("errors", []).append(
                            f"{lang or 'default'}: {e}")
                for msg in row.get("errors", []):
                    entry["errors"].append(f"purge {label}: {msg}")
                    report["errors"].append(f"purge {label}: {msg}")
                    report["ok"] = False
                entry["purged"].append(row)

        scopes: list[tuple[dict, str, str]] = [
            (_default_scope(), default_role, "default")]
        for role in sorted(roles):
            for chat in sorted(allowed.get(role, set())):
                if chat:
                    scopes.append((_chat_scope(chat), role, f"{role}:{chat}"))
        for scope, role, label in scopes:
            wanted = menu_payload(role)
            row = {"label": label, "role": role, "scope": scope,
                   "commands": len(wanted)}
            try:
                if dry_run:
                    row["current"] = [c.get("command")
                                      for c in api.get_commands(scope)]
                    entry["scopes"].append(row)
                    continue
                api.set_commands(wanted, scope)
                got = [c.get("command") for c in api.get_commands(scope)]
                row["verified"] = got
                if got != [s.name for s in SPECS[role]]:
                    raise TelegramError(f"{label} 校验不一致")
            except TelegramError as e:
                row["error"] = str(e)
                entry["errors"].append(f"{label}: {e}")
                report["errors"].append(f"{label}: {e}")
                report["ok"] = False
            entry["scopes"].append(row)

        if purge and not dry_run:
            for label, scope in purge_targets:
                if label not in PURGE_ONLY_LABELS and not label.startswith(
                        "chat_admin:"):
                    continue
                try:
                    got = [c.get("command")
                           for c in api.get_commands(scope)]
                except TelegramError as e:
                    entry["residual"][label] = f"ERR {e}"
                    entry["errors"].append(f"residual {label}: {e}")
                    report["errors"].append(f"residual {label}: {e}")
                    report["ok"] = False
                    continue
                if got:
                    entry["residual"][label] = got
                    msg = f"{label} 仍有残留命令:{','.join(got)}"
                    entry["errors"].append(msg)
                    report["errors"].append(msg)
                    report["ok"] = False
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
    canonical = ALIASES.get(name, name)
    allowed_names = {s.name for s in SPECS[ctx.role]}
    if canonical not in allowed_names:
        other = "ops" if ctx.role == "public" else "public"
        if canonical in {s.name for s in SPECS[other]}:
            return "⛔ 该命令不属于此机器人。"
        return f"❓ 未知命令 /{name}\n发送 /help 查看可用命令。"
    fn = HANDLERS.get(ctx.role, {}).get(canonical)
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


def _refresh_pid_file(pid_path: Path) -> None:
    """单实例锁持有者修复丢失/损坏的登记,避免状态页误报或重复拉起。"""
    pid = str(os.getpid())
    try:
        if pid_path.read_text(encoding="utf-8").strip() == pid:
            return
    except (OSError, UnicodeError):
        pass
    hb.write_atomic(pid_path, pid)


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
    try:
        _refresh_pid_file(pid_path)
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
                _refresh_pid_file(pid_path)
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

