"""实盘凭证装载(实盘接入件;v20/lifecycle)。

纪律(任务书硬性安全):
- 默认路径 C:\\Users\\EDY\\.edge-engine\\secrets.env,env 变量
  EDGE_SECRETS_PATH 可覆写;程序运行时自读 —— 永不打印、永不入日志、永不
  写 response/账本/回复;
- dotenv 式简析:KEY=VALUE 行(支持单/双引号、`export ` 前缀、`#` 注释、
  值内 `#` 不截断 —— passphrase/token 可能含该字符);
- 校验键存在即罢:require 只判空/缺失,绝不回显任何值;
- live 模式要求 Bitget 三件 + Telegram 两件;paper/离线冒烟按需提供。

键名以 secrets.env 文件实际为准(BITGET_SECRET_KEY / TELEGRAM_BOT_TOKEN),
同时接受任务书别名(BITGET_API_SECRET / TG_BOT_TOKEN / TG_CHAT_ID)。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_SECRETS_PATH = Path(r"C:\Users\EDY\.edge-engine\secrets.env")
ENV_OVERRIDE = "EDGE_SECRETS_PATH"

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# 键名别名(右 = 兼容名;顺序即优先序:首个存在者生效)
_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "BITGET_API_KEY": ("BITGET_API_KEY",),
    "BITGET_SECRET_KEY": ("BITGET_SECRET_KEY", "BITGET_API_SECRET"),
    "BITGET_PASSPHRASE": ("BITGET_PASSPHRASE", "BITGET_API_PASSWORD"),
    "TELEGRAM_BOT_TOKEN": ("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID": ("TELEGRAM_CHAT_ID", "TG_CHAT_ID"),
    "TELEGRAM_PROXY": ("TELEGRAM_PROXY",),          # 可选
}

REQUIRED_BITGET = ("BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE")
REQUIRED_TELEGRAM = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def secrets_path(path: Optional[os.PathLike | str] = None) -> Path:
    """装载路径:显式参数 > EDGE_SECRETS_PATH env > 默认路径。"""
    if path is not None:
        return Path(path)
    env = os.environ.get(ENV_OVERRIDE)
    return Path(env) if env else DEFAULT_SECRETS_PATH


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _load_env(path: Path) -> dict[str, str]:
    """解析 secrets.env(dotenv 式简析;返回键 → 值,值不经任何脱敏改动)。"""
    out: dict[str, str] = {}
    for ln, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m is None:
            continue                         # 无法识别的行:跳过(不解析)
        key, val = m.group(1), m.group(2)
        out[key] = _unquote(val)
    return out


class SecretsError(RuntimeError):
    """凭证缺失/路径异常(消息只含键名,绝不含值)。"""


def load_secrets(path: Optional[os.PathLike | str] = None,
                 required: tuple[str, ...] = REQUIRED_BITGET + REQUIRED_TELEGRAM,
                 environ: Optional[Mapping[str, str]] = None) -> dict:
    """装载并校验凭证;返回"规格化键" → 值的映射(值原样,调用方不入日志)。

    - required 中的规格化键(BITGET_* / TELEGRAM_*):文件或环境二者其一提供
      即可(文件优先?否 —— env 进程变量优先,便于 CI 注入;均不存在的键名
      标为 missing,异常消息只列键名);
    - file/env 均已找到的别名的值以**环境变量优先**,其次文件;
    - 可选键(如 TELEGRAM_PROXY)缺失不报错。
    """
    p = secrets_path(path)
    file_kv: dict[str, str] = {}
    if p.exists():
        file_kv = _load_env(p)
    env = dict(os.environ if environ is None else environ)

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for canon, aliases in _KEY_ALIASES.items():
        val: Optional[str] = None
        for name in aliases:
            if env.get(name):
                val = env[name]
                break
            if file_kv.get(name):
                val = file_kv[name]
                break
        if val is not None:
            resolved[canon] = val
        elif canon in required:
            missing.append(f"{canon}({ '/'.join(aliases) })")

    if missing:
        raise SecretsError(
            f"凭证缺失:{', '.join(missing)};路径={p}"
            f"({'存在' if p.exists() else '不存在'};可用 "
            f"{ENV_OVERRIDE} 覆写)— 值绝不回显,请检查键名")
    return resolved


def assert_never_logged(value: str, *texts: str) -> None:
    """防泄闸(测试/自检用):任一文本包含凭证值即 raise。

    runner 在拼装账本/告警文本后可调用;消息只说明发生了泄漏,不回显值。
    """
    for t in texts:
        if value and value in t:
            raise SecretsError("凭证值出现在待输出文本中(拒绝输出)")
