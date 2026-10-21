"""Telegram 双机器人通知 + 高清黑白 K 线结构图表渲染器(实盘接入件)。

纪律:
- 信号机器人(`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`)负责信号/日结/告警;
- 交易机器人(`TELEGRAM_TRADE_BOT_TOKEN` + `TELEGRAM_TRADE_CHAT_ID`)负责开仓/减仓/防线收紧/平仓;
- 黑白 K 线图(`render_bw_kline_png`)与 Emoji 逐行关键信息在同一条消息内发送(`sendPhoto` + `caption`);
- 每一行仅一条关键信息,行首配 Emoji;
- 异常完全吞吐隔离 + 可选后台守护线程队列,绝不阻塞或中断任何交易流程。
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
API_PHOTO = "https://api.telegram.org/bot{token}/sendPhoto"
RETRIES = 5
RETRY_BACKOFF_S = 1.5
TIMEOUT_S = 15
MAX_LEN = 4000
MAX_CAPTION_LEN = 1000

_KIND_TAG = {"signal": "信号", "fill": "成交", "daily": "日结", "alert": "告警"}

_EVENT_DISPLAY = {
    "E1": "ACCEPT (突破接受)",
    "ACCEPT": "ACCEPT (突破接受)",
    "E3": "REJECT (刺穿拒绝)",
    "REJECT": "REJECT (刺穿拒绝)",
}

_ROUTE_DISPLAY = {
    "e1_market": "ACCEPT 市价",
    "e3_market": "REJECT 市价",
    "accept_market": "ACCEPT 市价",
    "reject_market": "REJECT 市价",
}


def format_event_display(event_id: str) -> str:
    return _EVENT_DISPLAY.get(str(event_id).strip(), str(event_id))


def format_route_display(route: str) -> str:
    r = str(route).strip()
    if r in _ROUTE_DISPLAY:
        return _ROUTE_DISPLAY[r]
    return r.replace("e1_market", "ACCEPT 市价").replace("e3_market", "REJECT 市价")


def render_bw_kline_png(
    bars: Sequence[tuple],
    *,
    symbol: str = "",
    tf_label: str = "",
    title_tag: str = "",
    box_lo: Optional[float] = None,
    box_hi: Optional[float] = None,
    entry_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    target_price: Optional[float] = None,
    mark_index: Optional[int] = None,
    direction: Optional[int] = None,
    max_bars: int = 48,
) -> Optional[bytes]:
    """用 Pillow 绘制高对比度黑白 K 线图(带平衡箱体、入场/止损/目标水平线),返回 PNG bytes。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    if not bars:
        return None
    sub = list(bars[-max_bars:])
    n = len(sub)
    if n == 0:
        return None

    highs = [float(b[2]) for b in sub]
    lows = [float(b[3]) for b in sub]
    p_max, p_min = max(highs), min(lows)
    for lvl in (box_lo, box_hi, entry_price, stop_price, target_price):
        if lvl is not None and lvl > 0:
            p_max = max(p_max, float(lvl))
            p_min = min(p_min, float(lvl))
    span = max(p_max - p_min, p_max * 0.002, 1e-8)
    pad = span * 0.08
    y_hi, y_lo = p_max + pad, p_min - pad

    W, H = 920, 520
    left_m, right_m, top_m, bot_m = 24, 110, 44, 36
    plot_w, plot_h = W - left_m - right_m, H - top_m - bot_m

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    def price_to_y(p: float) -> int:
        frac = (y_hi - float(p)) / (y_hi - y_lo)
        return int(round(top_m + max(0.0, min(1.0, frac)) * plot_h))

    if box_lo is not None and box_hi is not None and box_hi > box_lo:
        y1, y2 = price_to_y(box_hi), price_to_y(box_lo)
        if y2 > y1:
            draw.rectangle([left_m, y1, left_m + plot_w, y2], fill=(238, 238, 238))
        for x in range(left_m, left_m + plot_w, 10):
            draw.line([(x, y1), (min(x + 6, left_m + plot_w), y1)], fill=(50, 50, 50), width=2)
            draw.line([(x, y2), (min(x + 6, left_m + plot_w), y2)], fill=(50, 50, 50), width=2)
        draw.text((left_m + plot_w + 6, y1 - 6), f"HI {box_hi:.5g}", fill=(30, 30, 30), font=font)
        draw.text((left_m + plot_w + 6, y2 - 6), f"LO {box_lo:.5g}", fill=(30, 30, 30), font=font)

    for k in range(5):
        p_val = y_lo + (y_hi - y_lo) * (k / 4.0)
        yk = price_to_y(p_val)
        for x in range(left_m, left_m + plot_w, 12):
            draw.point((x, yk), fill=(200, 200, 200))
        draw.text((left_m + plot_w + 6, yk - 5), f"{p_val:.6g}", fill=(110, 110, 110), font=font)

    def _draw_level(px: Optional[float], label: str, dash: int = 4) -> None:
        if px is None or px <= 0:
            return
        yy = price_to_y(px)
        for x in range(left_m, left_m + plot_w, dash * 2):
            draw.line([(x, yy), (min(x + dash, left_m + plot_w), yy)], fill=(0, 0, 0), width=1)
        draw.text((left_m + 6, max(top_m + 2, yy - 12)), f"{label}: {px:.6g}", fill=(0, 0, 0), font=font)

    _draw_level(entry_price, "ENTRY", dash=6)
    _draw_level(stop_price, "STOP", dash=3)
    _draw_level(target_price, "TARGET", dash=5)

    step_x = plot_w / max(n, 1)
    body_w = max(3, int(step_x * 0.66))
    for idx, b in enumerate(sub):
        _ts, o, h, l, c = float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])
        cx = int(round(left_m + (idx + 0.5) * step_x))
        yh, yl, yo, yc = price_to_y(h), price_to_y(l), price_to_y(o), price_to_y(c)
        draw.line([(cx, yh), (cx, yl)], fill=(0, 0, 0), width=1)
        top_b, bot_b = min(yo, yc), max(yo, yc)
        if bot_b - top_b < 2:
            bot_b = top_b + 2
        x0, x1 = cx - body_w // 2, cx + body_w // 2
        if c >= o:
            draw.rectangle([x0, top_b, x1, bot_b], fill=(255, 255, 255), outline=(0, 0, 0), width=1)
        else:
            draw.rectangle([x0, top_b, x1, bot_b], fill=(0, 0, 0), outline=(0, 0, 0), width=1)

    m_idx = (n - 1) if mark_index is None else max(0, min(n - 1, mark_index))
    mcx = int(round(left_m + (m_idx + 0.5) * step_x))
    mb = sub[m_idx]
    if direction is not None and direction < 0:
        my = max(top_m + 6, price_to_y(float(mb[2])) - 14)
        draw.polygon([(mcx, my + 8), (mcx - 5, my), (mcx + 5, my)], fill=(0, 0, 0))
    else:
        my = min(top_m + plot_h - 6, price_to_y(float(mb[3])) + 14)
        draw.polygon([(mcx, my - 8), (mcx - 5, my), (mcx + 5, my)], fill=(0, 0, 0))

    draw.rectangle([left_m, top_m, left_m + plot_w, top_m + plot_h], outline=(0, 0, 0), width=2)
    ts_first = datetime.fromtimestamp(int(sub[0][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
    ts_last = datetime.fromtimestamp(int(sub[-1][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
    header = f"{symbol} [{tf_label}] {title_tag}  ({ts_first} -> {ts_last})"
    draw.text((left_m, 14), header.strip(), fill=(0, 0, 0), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()



class TelegramNotifier:
    """双机器人发信器(信号 Bot + 交易 Bot) + 黑白 K 线图同条消息发送;无网络或异常零副作用。"""

    def __init__(self, secrets: Optional[dict] = None, *,
                 session: Optional[requests.Session] = None,
                 dropped_log: Optional[Path] = None,
                 max_retries: int = RETRIES,
                 async_mode: bool = False) -> None:
        secrets = secrets or {}
        self._token = secrets.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = secrets.get("TELEGRAM_CHAT_ID", "")
        self._trade_token = secrets.get("TELEGRAM_TRADE_BOT_TOKEN", "") or self._token
        self._trade_chat_id = secrets.get("TELEGRAM_TRADE_CHAT_ID", "") or self._chat_id
        self.max_retries = int(max_retries)
        self.dropped = 0
        self.sent = 0
        self.dropped_log = Path(dropped_log) if dropped_log else None
        self.session = session or requests.Session()
        proxy = secrets.get("TELEGRAM_PROXY")
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self.async_mode = bool(async_mode)
        self._q: queue.Queue = queue.Queue(maxsize=256)
        if self.async_mode:
            t = threading.Thread(target=self._worker, name="tg-notifier", daemon=True)
            t.start()

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def trade_configured(self) -> bool:
        return bool(self._trade_token and self._trade_chat_id)

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            try:
                fn, args, kwargs = item
                fn(*args, **kwargs)
            except Exception:
                pass
            finally:
                self._q.task_done()

    def _dispatch(self, fn, *args, **kwargs) -> bool:
        if self.async_mode:
            try:
                self._q.put_nowait((fn, args, kwargs))
                return True
            except queue.Full:
                self.dropped += 1
                return False
        try:
            return bool(fn(*args, **kwargs))
        except Exception as e:
            self._record_miss("dispatch", e)
            return False

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
                pass

    def flush_dropped_report(self) -> int:
        """连接恢复后补发漏失告警;返回补发时累计漏失条数(0 ⇒ 不动)。"""
        n = self.dropped
        if n and self.send_alert(
                f"⚠️ 此前 Telegram 连接故障\n📉 累计漏发 {n} 条消息(已补报)"):
            return n
        return 0

    def _post_text_sync(self, token: str, chat_id: str, kind: str, body: str) -> bool:
        if not (token and chat_id):
            self._record_miss(kind, RuntimeError("telegram 未配置"))
            return False
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(
                    API.format(token=token),
                    data={"chat_id": chat_id, "text": body,
                          "disable_web_page_preview": "true"},
                    timeout=TIMEOUT_S)
                payload = resp.json()
                if resp.status_code == 200 and payload.get("ok"):
                    self.sent += 1
                    return True
                last_err = RuntimeError(
                    f"tg {resp.status_code}: {str(payload)[:160]}")
            except Exception as e:
                last_err = e
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        self._record_miss(kind, last_err or RuntimeError("unknown"))
        return False

    def _post_photo_sync(self, token: str, chat_id: str, kind: str,
                         caption: str, png_bytes: bytes) -> bool:
        if not (token and chat_id):
            self._record_miss(kind, RuntimeError("telegram 未配置"))
            return False
        cap = caption if len(caption) <= MAX_CAPTION_LEN else caption[:MAX_CAPTION_LEN - 12] + "…[截断]"
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(
                    API_PHOTO.format(token=token),
                    data={"chat_id": chat_id, "caption": cap},
                    files={"photo": ("kline.png", png_bytes, "image/png")},
                    timeout=TIMEOUT_S)
                payload = resp.json()
                if resp.status_code == 200 and payload.get("ok"):
                    self.sent += 1
                    return True
                last_err = RuntimeError(
                    f"tg photo {resp.status_code}: {str(payload)[:160]}")
            except Exception as e:
                last_err = e
            time.sleep(RETRY_BACKOFF_S * (attempt + 1))
        return self._post_text_sync(token, chat_id, kind, cap)

    def send(self, kind: str, text: str, *, use_trade_bot: bool = False,
             png_bytes: Optional[bytes] = None) -> bool:
        """发信(支持图文同条消息与双 Bot 分流);异常完全隔离,绝不 raise。"""
        token = self._trade_token if use_trade_bot else self._token
        chat_id = self._trade_chat_id if use_trade_bot else self._chat_id
        tag = _KIND_TAG.get(kind, kind)
        prefix = "" if text.startswith("【") else f"【{tag}】\n"
        body = prefix + (text if len(text) <= MAX_LEN
                         else text[:MAX_LEN - 20] + "…[截断]")
        if png_bytes:
            return self._dispatch(self._post_photo_sync, token, chat_id, kind, body, png_bytes)
        return self._dispatch(self._post_text_sync, token, chat_id, kind, body)

    # ------------------------------------------------------------------
    # 四类模板(每行仅一条关键信息,行首配 Emoji;图文同条消息)
    # ------------------------------------------------------------------

    def send_signal(self, symbol: str, event_id: str, side: str, route: str,
                    ref_price: float, invalid: float, size_note: str = "",
                    regime: str = "", *, tf_label: str = "",
                    bars: Optional[Sequence[tuple]] = None,
                    box_lo: Optional[float] = None,
                    box_hi: Optional[float] = None,
                    target_1: Optional[float] = None) -> bool:
        dir_emoji = "🟢" if side in ("long", "buy", "多") else "🔴"
        ev_disp = format_event_display(event_id)
        rt_disp = format_route_display(route)
        lines = [
            f"📡 信号触发: {symbol}",
            f"⏱️ 交易周期: {tf_label or 'HTF'}",
            f"🧭 原语事件: {ev_disp} [{regime or 'BALANCE'}]",
            f"{dir_emoji} 交易方向: {side}",
            f"🛣️ 路由方式: {rt_disp}",
            f"🎯 参考价格: {ref_price:.8g}",
            f"🛡️ 初始止损: {invalid:.8g}",
        ]
        if box_lo is not None and box_hi is not None:
            lines.append(f"📦 平衡箱体: [{box_lo:.8g}, {box_hi:.8g}]")
        if target_1 is not None:
            lines.append(f"🏁 结构对侧: {target_1:.8g}")
        if size_note:
            clean_note = size_note.strip().lstrip("|").strip()
            if clean_note:
                lines.append(f"📐 仓位备注: {clean_note}")
        png = None
        if bars:
            d_int = 1 if side in ("long", "buy", "多") else -1
            ev_short = "ACCEPT" if str(event_id) in ("E1", "ACCEPT") else (
                "REJECT" if str(event_id) in ("E3", "REJECT") else str(event_id))
            png = render_bw_kline_png(
                bars, symbol=symbol, tf_label=tf_label,
                title_tag=f"SIGNAL {ev_short} {side.upper()}",
                box_lo=box_lo, box_hi=box_hi,
                entry_price=ref_price, stop_price=invalid,
                target_price=target_1, direction=d_int)
        return self.send("signal", "\n".join(lines), use_trade_bot=False, png_bytes=png)

    def send_fill(self, symbol: str, fill_kind: str, qty: float, price: float,
                  pnl: Optional[float] = None, note: str = "", *,
                  tf_label: str = "", direction: Optional[int] = None,
                  stop_price: Optional[float] = None,
                  target_price: Optional[float] = None,
                  box_lo: Optional[float] = None,
                  box_hi: Optional[float] = None,
                  bars: Optional[Sequence[tuple]] = None) -> bool:
        lines = [
            f"⚡ 交易执行: {symbol}",
            f"🏷️ 动作类型: {fill_kind}",
        ]
        if tf_label:
            lines.append(f"⏱️ 所属周期: {tf_label}")
        lines.extend([
            f"📦 成交数量: {qty:.8g}",
            f"💵 成交价格: {price:.8g}",
        ])
        if stop_price is not None:
            lines.append(f"🛡️ 当前止损: {stop_price:.8g}")
        if target_price is not None:
            lines.append(f"🏁 结构目标: {target_price:.8g}")
        if pnl is not None:
            pnl_emoji = "💰" if pnl >= 0 else "💸"
            lines.append(f"{pnl_emoji} 实现盈亏: {pnl:+.2f} USDT")
        if note:
            clean_note = (str(note)
                          .replace("E1 e1_market", "ACCEPT (突破接受) | 市价")
                          .replace("E3 e3_market", "REJECT (刺穿拒绝) | 市价")
                          .replace("e1_market", "ACCEPT 市价")
                          .replace("e3_market", "REJECT 市价"))
            lines.append(f"📝 详情说明: {clean_note}")
        png = None
        if bars:
            png = render_bw_kline_png(
                bars, symbol=symbol, tf_label=tf_label,
                title_tag=f"TRADE {fill_kind}",
                box_lo=box_lo, box_hi=box_hi,
                entry_price=price, stop_price=stop_price,
                target_price=target_price, direction=direction)
        return self.send("fill", "\n".join(lines), use_trade_bot=True, png_bytes=png)

    def send_daily(self, date: str, equity: float, day_pnl: float,
                   n_signals: int, n_fills: int, n_positions: int,
                   drawdown_frac: float) -> bool:
        lines = [
            f"📅 结算日期: {date}",
            f"🏦 账户权益: {equity:.2f} USDT",
            f"📊 当日盈亏: {day_pnl:+.2f} USDT",
            f"📉 峰值回撤: {drawdown_frac:.1%}",
            f"📡 当日信号: {n_signals}",
            f"⚡ 当日成交: {n_fills}",
            f"📌 当前持仓: {n_positions}",
        ]
        return self.send("daily", "\n".join(lines), use_trade_bot=False)

    def send_alert(self, text: str) -> bool:
        lines = [
            line if line.strip().startswith(("⚠️", "🚨", "📉", "🛡️", "❗"))
            else f"🚨 {line.strip()}"
            for line in text.splitlines() if line.strip()
        ]
        return self.send("alert", "\n".join(lines), use_trade_bot=False)


__all__ = ["TelegramNotifier", "render_bw_kline_png"]
