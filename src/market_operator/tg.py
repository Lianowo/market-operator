"""Telegram Bot API 发信(实盘接入件;中文模板:信号/成交/日结/告警)。

纪律:
- token/chat_id 只经 secrets_loader 注入;文本绝不包含凭证;
- 重试(指数退避)+ 漏失告警:重试耗尽 → 递增 dropped 计数并记 runtime
  文件(runner 每轮读;连接恢复后补发一条"漏失告警"说明漏发条数);
- 代理:TELEGRAM_PROXY(可选,secrets.env 同目录键;api.telegram.org 直连
  不通时经 HTTP 代理)。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
RETRIES = 5
RETRY_BACKOFF_S = 1.5
TIMEOUT_S = 15
MAX_LEN = 4000                                # TG 单条上限 4096,留余量截断

_KIND_TAG = {"signal": "信号", "fill": "成交", "daily": "日结", "alert": "告警"}


class TelegramNotifier:
    """发信器;session 可注入(测试注 mock),无网络路径零副作用。"""

    def __init__(self, secrets: Optional[dict] = None, *,
                 session: Optional[requests.Session] = None,
                 dropped_log: Optional[Path] = None,
                 max_retries: int = RETRIES) -> None:
        secrets = secrets or {}
        self._token = secrets.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = secrets.get("TELEGRAM_CHAT_ID", "")
        self.max_retries = int(max_retries)
        self.dropped = 0
        self.sent = 0
        self.dropped_log = Path(dropped_log) if dropped_log else None
        self.session = session or requests.Session()
        proxy = secrets.get("TELEGRAM_PROXY")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def _record_miss(self, kind: str, err: Exception) -> None:
        self.dropped += 1
        if self.dropped_log is not None:
            try:
                self.dropped_log.parent.mkdir(parents=True, exist_ok=True)
                rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "kind": kind, "err": f"{type(err).__name__}: {str(err)[:160]}"}
                with self.dropped_log.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass                            # 记账失败不再升级(避免级联)

    def flush_dropped_report(self) -> int:
        """连接恢复后补发漏失告警;返回补发时累计漏失条数(0 ⇒ 不动)。"""
        n = self.dropped
        if n and self.send_alert(
                f"此前 Telegram 连接故障,漏发 {n} 条消息(已按漏失告警补报)。"):
            return n
        return 0

    def send(self, kind: str, text: str) -> bool:
        """发信;成功 True。重试耗尽 → 漏失告警路径(计数+记账),不 raise。"""
        if not self.configured:
            self._record_miss(kind, RuntimeError("telegram 未配置"))
            return False
        tag = _KIND_TAG.get(kind, kind)
        body = f"【{tag}】" + (text if len(text) <= MAX_LEN
                            else text[:MAX_LEN - 20] + "…[截断]")
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(
                    API.format(token=self._token),
                    data={"chat_id": self._chat_id, "text": body,
                          "disable_web_page_preview": "true"},
                    timeout=TIMEOUT_S)
                payload = resp.json()
                if resp.status_code == 200 and payload.get("ok"):
                    self.sent += 1
                    return True
                last_err = RuntimeError(
                    f"tg {resp.status_code}: {str(payload)[:160]}")
            except (requests.RequestException, ValueError) as e:
                last_err = e
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        self._record_miss(kind, last_err or RuntimeError("unknown"))
        return False

    # ------------------------------------------------------------------
    # 四类模板(中文;价格/数量由调用方给足精度)
    # ------------------------------------------------------------------

    def send_signal(self, symbol: str, event_id: str, side: str, route: str,
                    ref_price: float, invalid: float, size_note: str,
                    regime: str = "") -> bool:
        return self.send(
            "signal",
            f"{symbol} {event_id} {side}({regime})路由 {route}\n"
            f"参考价 {ref_price} 失效 {invalid}{size_note}")

    def send_fill(self, symbol: str, fill_kind: str, qty: float, price: float,
                  pnl: Optional[float] = None, note: str = "") -> bool:
        pnl_s = f"\n盈亏 {pnl:+.2f} USDT" if pnl is not None else ""
        return self.send(
            "fill",
            f"{symbol} {fill_kind} {qty} @ {price}{pnl_s}"
            + (f"\n{note}" if note else ""))

    def send_daily(self, date: str, equity: float, day_pnl: float,
                   n_signals: int, n_fills: int, n_positions: int,
                   drawdown_frac: float) -> bool:
        return self.send(
            "daily",
            f"{date} 权益 {equity:.2f} USDT(日盈亏 {day_pnl:+.2f},"
            f"峰值回撤 {drawdown_frac:.1%})\n信号 {n_signals} / 成交 {n_fills}"
            f" / 在持 {n_positions}")

    def send_alert(self, text: str) -> bool:
        return self.send("alert", text)


__all__ = ["TelegramNotifier"]
