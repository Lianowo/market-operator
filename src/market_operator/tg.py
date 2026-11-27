"""Telegram 双通道通知 + 高清黑白 K 线结构图表渲染器(实盘接入件)。

纪律:
- **信号通道**(`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`):只发信号广播
  (`signal`)。该机器人/群可以公开给他人订阅,因此**不得**出现账户规模、持仓
  数量、下单失败、熔断、断线补做等运营信息;
- **运营通道**(`TELEGRAM_TRADE_BOT_TOKEN` + `TELEGRAM_TRADE_CHAT_ID`):发成交 /
  防线推进 / 平仓 / 日结 / 告警(`fill` / `daily` / `alert`)。未单独配置时由
  `secrets_loader` 回退到信号机器人(不静默丢消息),runner 启动时会打印提示;
- 通道由 `_KIND_CHANNEL` 一张表决定,调用方不再手写开关 —— 一张表就是唯一事实,
  避免"某个告警走错房间";
- 黑白 K 线图(`render_bw_kline_png`)与 Emoji 逐行关键信息在同一条消息内发送
  (`sendPhoto` + `caption`);图上带原子水平位带(旧口径的平衡箱体)、事件区间(zone)、`±R` 标尺、
  pivot 与入场/止损/目标位,读图即可回答"这笔交易在结构里的哪个位置";
- 每一行仅一条关键信息,行首配 Emoji;
- 渲染 + 上传都可放进后台队列(`png_factory`):实盘主循环只做一次
  `put_nowait`,Pillow 与网络全部在守护线程,信号链路不被阻塞;
- 异常完全吞吐隔离:发送/渲染失败只记 dropped,绝不 raise、绝不中断交易流程。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import queue
import threading
import time
from types import MappingProxyType
from typing import Any
import urllib.request

import requests

from .charts import (
    bar_gap_stats as _bar_gap_stats,
    box_position_pct,
    box_position_text,
    render_bw_kline_png,
    render_level_map_png,
    rr_multiple,
)
from .exceptions import NotificationError

_LOG = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
API_PHOTO = "https://api.telegram.org/bot{token}/sendPhoto"
RETRIES = 5
RETRY_BACKOFF_S = 1.5
TIMEOUT_S = 15
MAX_LEN = 4000                   # Telegram 单条 4096;留出标签前缀余量
MAX_CAPTION_LEN = 1024
RATE_LIMIT_WAIT_MAX_S = 60.0     # 429 的 retry_after 上限:再长也不傻等

_KIND_TAG: Mapping[str, str] = MappingProxyType({
    "signal": "信号",
    "fill": "成交",
    "daily": "日结",
    "alert": "告警",
    "watch": "关注状态",
})

CHANNEL_SIGNAL = "signal"      # 公开广播:只发信号
CHANNEL_OPS = "ops"            # 私有运营:成交/日结/告警

# 唯一分流事实:消息类型 → 通道。新增消息类型必须在这里显式登记,
# 不做"默认落到信号通道"的隐式回退(公开频道绝不能收到运营信息)。
_KIND_CHANNEL: Mapping[str, str] = MappingProxyType({
    "signal": CHANNEL_SIGNAL,
    "fill": CHANNEL_OPS,
    "daily": CHANNEL_OPS,
    "alert": CHANNEL_OPS,
    "watch": CHANNEL_OPS,
})

_WATCH_REASONS: Mapping[str, str] = MappingProxyType({
    "WATCH_INNER_EDGE_HIT": "价格命中关注内沿",
    "WATCH_NEAR_BOUNCE": "近价区触及后企稳确认",
    "N_fill_expiry": "等待期限已过",
    "position_exit": "持仓离场，关联关注作废",
})

_FILL_NOTE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("BREAKOUT_ACCEPTED e1_market", "ACCEPT (突破接受) | 市价"),
    ("LEVEL_REJECTED e3_market", "REJECT (刺穿拒绝) | 市价"),
    ("breakout_accepted_market", "ACCEPT 市价"),
    ("level_rejected_market", "REJECT 市价"),
    ("v21|sub_bar_partial_exit_on_arm", "锁利减仓并移至保本"),
    ("v21|sub_bar_opp_lock", "对侧锁利"),
)


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    """按行切分长文本(单行超长才硬切),保证每条不超过 ``limit``。"""
    out: list[str] = []
    buf = ""
    for line in str(text).splitlines():
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            if buf:
                out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out or [""]


def _retry_after_seconds(payload: object) -> float | None:
    """从 Telegram 429 响应取 ``parameters.retry_after`` (秒);缺失/坏值 ⇒ None。"""
    if not isinstance(payload, Mapping):
        return None
    params = payload.get("parameters")
    if not isinstance(params, Mapping):
        return None
    try:
        after = float(params.get("retry_after"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if after <= 0.0:
        return None
    return min(after, RATE_LIMIT_WAIT_MAX_S)


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    """429 优先按服务端 retry_after;其余按线性退避。"""
    if retry_after is not None:
        return max(0.0, float(retry_after))
    return RETRY_BACKOFF_S * (attempt + 1)


def public_event_label(event_id: str) -> str:
    """公开频道只用白话,不写内部事件枚举。"""
    eid = str(event_id or "").upper()
    if "REJECT" in eid:
        return "刺穿拒绝"
    if "ACCEPT" in eid or "BREAKOUT" in eid:
        return "接受突破"
    return ""


def level_note_text(kind: object, role: object) -> str:
    kinds = {"SWING": "摆动", "EQUAL": "齐平", "FLIPPED": "极性互换", "ORIGIN": "起点"}
    roles = {"SUPPORT": "支撑", "RESISTANCE": "阻力"}
    k = kinds.get(str(kind or "").upper(), "")
    r = roles.get(str(role or "").upper(), "")
    if k and r:
        return f"{k}·{r}"
    return k or r


class NotificationGuard:
    """Runner boundary: notifier method failures cannot abort bookkeeping."""

    def __init__(self, notifier: Any) -> None:
        self._notifier = notifier
        self.errors = 0

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._notifier, name)
        if not name.startswith("send") or not callable(value):
            return value

        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return value(*args, **kwargs)
            except Exception as exc:
                self.errors += 1
                _LOG.warning("notification guard caught %s in %s: %s", type(exc).__name__, name, exc)
                return False

        return guarded


class TelegramNotifier:
    """双通道发信器(公开信号 Bot + 私有运营 Bot) + 黑白 K 线图同条消息发送。"""

    def __init__(
        self,
        secrets: Mapping[str, Any] | None = None,
        *,
        session: requests.Session | None = None,
        dropped_log: Path | str | None = None,
        max_retries: int = RETRIES,
        async_mode: bool = False,
    ) -> None:
        sec = secrets or {}
        self._token = str(sec.get("TELEGRAM_BOT_TOKEN", ""))
        self._chat_id = str(sec.get("TELEGRAM_CHAT_ID", ""))
        self._ops_token = str(sec.get("TELEGRAM_TRADE_BOT_TOKEN", "")) or self._token
        self._ops_chat_id = str(sec.get("TELEGRAM_TRADE_CHAT_ID", "")) or self._chat_id
        self.max_retries = int(max_retries)
        self._lock = threading.Lock()
        self.dropped = 0
        self.sent = 0
        self.dropped_log = Path(dropped_log) if dropped_log else None
        self.session = session or requests.Session()
        proxy = sec.get("TELEGRAM_PROXY")
        if not proxy and session is None:
            sys_p = urllib.request.getproxies()
            proxy = sys_p.get("https") or sys_p.get("http")
        if proxy:
            self.session.proxies = {"http": str(proxy), "https": str(proxy)}
        self.async_mode = bool(async_mode)
        self._q: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]] | None] = (
            queue.Queue(maxsize=256)
        )
        if self.async_mode:
            t = threading.Thread(target=self._worker, name="tg-notifier", daemon=True)
            t.start()

    @property
    def configured(self) -> bool:
        """信号通道可用(公开广播的最低前提)。"""
        return bool(self._token and self._chat_id)

    @property
    def ops_configured(self) -> bool:
        """运营通道可用(回退算可用:同一机器人也能收)。"""
        return bool(self._ops_token and self._ops_chat_id)

    @property
    def ops_channel_separate(self) -> bool:
        """运营通道是否真的与信号通道分开(启动日志据此提示)。"""
        if not (self._ops_token and self._ops_chat_id):
            return False
        return self._ops_token != self._token or self._ops_chat_id != self._chat_id

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                break
            try:
                fn, args, kwargs = item
                fn(*args, **kwargs)
            except Exception as exc:
                self._record_miss("worker", exc)
            finally:
                self._q.task_done()

    def _dispatch(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        if self.async_mode:
            try:
                self._q.put_nowait((fn, args, kwargs))
                return True
            except queue.Full:
                with self._lock:
                    self.dropped += 1
                return False
        try:
            return bool(fn(*args, **kwargs))
        except Exception as exc:
            self._record_miss("dispatch", exc)
            return False

    def _log_miss(self, kind: str, err: Exception) -> None:
        """只落漏发明细,不动计数。"""
        if self.dropped_log is None:
            return
        try:
            self.dropped_log.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "kind": kind,
                "err": f"{type(err).__name__}: {str(err)[:160]}",
            }
            with self.dropped_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as exc:
            _LOG.debug("failed to append dropped telegram log %s: %s", self.dropped_log, exc)

    def _record_miss(self, kind: str, err: Exception) -> None:
        with self._lock:
            self.dropped += 1
        self._log_miss(kind, err)

    def flush_dropped_report(self) -> int:
        """连接恢复后补报一次累计漏失;成功才清零,返回本次补报的条数。"""
        with self._lock:
            count = self.dropped
            if count <= 0:
                return 0
            self.dropped = 0
        try:
            ok = self.send_alert(
                f"⚠️ 此前 Telegram 连接故障\n📉 累计漏发 {count} 条消息(已补报)"
            )
        except Exception as exc:
            _LOG.debug("flush_dropped_report alert raised: %s", exc)
            ok = False
        if not ok:
            with self._lock:
                # 减去本次补报失败自身可能多计的 1 条,保留并发新增的漏发数
                extra = max(0, self.dropped - 1)
                self.dropped = count + extra
            return 0
        return count

    def _maybe_report_drops(self) -> None:
        """每成功投递一条就检查一次漏发账;0 ⇒ 不动。"""
        with self._lock:
            pending = self.dropped
        if pending > 0:
            self.flush_dropped_report()

    def _post_with_retry(
        self,
        url: str,
        *,
        data: Mapping[str, Any],
        files: Mapping[str, Any] | None = None,
        label: str = "tg",
    ) -> tuple[bool, Exception | None]:
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            retry_after: float | None = None
            try:
                post_kwargs: dict[str, Any] = {"data": dict(data), "timeout": TIMEOUT_S}
                if files is not None:
                    post_kwargs["files"] = dict(files)
                resp = self.session.post(url, **post_kwargs)
                payload = resp.json()
                if resp.status_code == 200 and payload.get("ok"):
                    with self._lock:
                        self.sent += 1
                    return True, None
                retry_after = _retry_after_seconds(payload)
                last_err = NotificationError(
                    f"{label} {resp.status_code}: {str(payload)[:160]}"
                )
            except Exception as exc:
                last_err = exc
            if attempt + 1 < self.max_retries:
                time.sleep(_retry_delay(attempt, retry_after))
        return False, last_err

    def _post_text_chunks(self, token: str, chat_id: str, kind: str, body: str) -> bool:
        """按 Telegram 单条上限逐条发送;任一条失败即返回 False。"""
        ok = True
        for chunk in split_text(body, MAX_LEN):
            if not self._post_text_sync(token, chat_id, kind, chunk):
                ok = False
        if ok:
            self._maybe_report_drops()
        return ok

    def _post_text_sync(self, token: str, chat_id: str, kind: str, body: str) -> bool:
        if not (token and chat_id):
            self._record_miss(kind, NotificationError("telegram 未配置"))
            return False
        ok, last_err = self._post_with_retry(
            API.format(token=token),
            data={"chat_id": chat_id, "text": body, "disable_web_page_preview": "true"},
            label="tg",
        )
        if ok:
            return True
        self._record_miss(kind, last_err or NotificationError("unknown"))
        return False

    def _post_photo_sync(
        self,
        token: str,
        chat_id: str,
        kind: str,
        caption: str,
        png_bytes: bytes,
    ) -> bool:
        if not (token and chat_id):
            self._record_miss(kind, NotificationError("telegram 未配置"))
            return False
        cap = (
            caption
            if len(caption) <= MAX_CAPTION_LEN
            else caption[: MAX_CAPTION_LEN - 12] + "…[截断]"
        )
        ok, last_err = self._post_with_retry(
            API_PHOTO.format(token=token),
            data={"chat_id": chat_id, "caption": cap},
            files={"photo": ("kline.png", png_bytes, "image/png")},
            label="tg photo",
        )
        if ok:
            return True
        if self._post_text_sync(token, chat_id, kind, cap):
            return True
        if last_err is not None:
            self._log_miss(f"{kind}:photo", last_err)
        return False

    def _post_photo_or_render(
        self,
        token: str,
        chat_id: str,
        kind: str,
        body: str,
        png_bytes: bytes | None,
        png_factory: Callable[[], bytes | None] | None,
    ) -> bool:
        """先拿到图(必要时此刻才渲染)再发;渲染失败退回纯文本,不丢消息本身。"""
        png = png_bytes
        if png is None and png_factory is not None:
            try:
                png = png_factory()
            except Exception as exc:
                self._record_miss("render", exc)
                png = None
        if png:
            if len(body) <= MAX_CAPTION_LEN:
                ok = self._post_photo_sync(token, chat_id, kind, body, png)
                if ok:
                    self._maybe_report_drops()
                return ok
            text_ok = self._post_text_chunks(token, chat_id, kind, body)
            photo_ok = self._post_photo_sync(
                token, chat_id, kind, "K线结构图（详见配套文字）", png
            )
            return text_ok and photo_ok
        return self._post_text_chunks(token, chat_id, kind, body)

    def channel_of(self, kind: str) -> str:
        """消息类型 → 通道(未知类型落运营通道)。"""
        return _KIND_CHANNEL.get(kind, CHANNEL_OPS)

    def send(
        self,
        kind: str,
        text: str,
        *,
        channel: str | None = None,
        png_bytes: bytes | None = None,
        png_factory: Callable[[], bytes | None] | None = None,
    ) -> bool:
        """发信(图文同条消息 + 按类型分流通道);异常完全隔离,绝不 raise。"""
        chan = channel or self.channel_of(kind)
        if chan == CHANNEL_SIGNAL:
            token, chat_id = self._token, self._chat_id
        else:
            token, chat_id = self._ops_token, self._ops_chat_id
        tag = _KIND_TAG.get(kind, kind)
        prefix = "" if text.startswith("【") else f"【{tag}】\n"
        body = prefix + text
        if png_bytes or png_factory is not None:
            return self._dispatch(
                self._post_photo_or_render, token, chat_id, kind, body, png_bytes, png_factory
            )
        return self._dispatch(self._post_text_chunks, token, chat_id, kind, body)

    # ------------------------------------------------------------------
    # 四类模板(每行仅一条关键信息,行首配 Emoji;图文同条消息)
    # ------------------------------------------------------------------

    def send_signal(
        self,
        symbol: str,
        event_id: str,
        side: str,
        route: str,
        ref_price: float,
        invalid: float,
        size_note: str = "",
        regime: str = "",
        *,
        tf_label: str = "",
        bars: Sequence[Sequence[Any]] | None = None,
        box_lo: float | None = None,
        box_hi: float | None = None,
        target_1: float | None = None,
        zone_lo: float | None = None,
        zone_hi: float | None = None,
        zone_touches: int | None = None,
        pivots: Sequence[Sequence[Any]] | None = None,
        atr: float | None = None,
        exec_status: str = "",
        decision_ts: int | None = None,
        watch_details: Mapping[str, Any] | None = None,
        price_context: Mapping[str, Any] | None = None,
        box_start_ts: int | None = None,
        bar_tf: str = "",
        level_kind: str = "",
        level_role: str = "",
    ) -> bool:
        """信号广播(公开通道):面向外部使用者的精简交易信号卡(隐藏内部算法细节与账户信息)。"""
        _ = (route, size_note, regime, level_kind, level_role)
        long_side = side in ("long", "buy", "多")
        dir_emoji = "🟢" if long_side else "🔴"
        d_int = 1 if long_side else -1
        dir_label = "做多 (LONG)" if long_side else "做空 (SHORT)"
        entry_label = "关注参考价" if watch_details else "参考入场"
        lines = [
            f"📡 交易信号: {symbol}",
            f"⏱️ 信号周期: {tf_label or 'HTF'}",
        ]
        if bar_tf and bar_tf != (tf_label or ""):
            lines.append(f"🕯️ 图上周期: {bar_tf}")
        event_txt = public_event_label(event_id)
        if event_txt:
            lines.append(f"📌 信号类型: {event_txt}")
        lines.extend([
            f"{dir_emoji} 交易方向: {dir_label}",
            f"🎯 {entry_label}: {ref_price:.8g}",
            f"🛡️ 止损防守: {invalid:.8g}",
        ])
        rr = rr_multiple(d_int, ref_price, invalid, target_1)
        if target_1 is not None:
            rr_txt = f" (R:R {rr:.2f})" if rr is not None else ""
            lines.append(f"🏁 目标参考: {float(target_1):.8g}{rr_txt}")
        if exec_status:
            lines.append(f"🧭 处置: {exec_status}")
        if price_context is not None:
            if box_start_ts is None and price_context.get("box_start_ts") is not None:
                try:
                    box_start_ts = int(price_context["box_start_ts"])
                except (TypeError, ValueError):
                    box_start_ts = None
            px, stamp = price_context.get("snapshot_price"), price_context.get("snapshot_ts")
            if px is not None and stamp is not None:
                sampled = datetime.fromtimestamp(int(stamp) / 1000, tz=timezone.utc).strftime(
                    "%m-%d %H:%M:%S UTC"
                )
                lines.append(f"💹 实时快照: {float(px):.8g} ({sampled})")
        if watch_details:
            for key, label in (("watch_price", "候选触发价"), ("prog_price", "观察失效价")):
                value = watch_details.get(key)
                if value is not None:
                    lines.append(f"👁️ {label}: {float(value):.8g}")
            if watch_details.get("max_wait_hours") is not None:
                lines.append(f"⌛ 观察窗口: {watch_details['max_wait_hours']} 小时")
        if decision_ts:
            try:
                stamp_txt = datetime.fromtimestamp(
                    int(decision_ts) / 1000.0, tz=timezone.utc
                ).strftime("%m-%d %H:%M UTC")
                lines.append(f"🕒 信号时间: {stamp_txt}")
            except (TypeError, ValueError, OSError, OverflowError) as exc:
                _LOG.debug("invalid decision_ts=%r: %s", decision_ts, exc)
        return self.send(
            "signal",
            "\n".join(lines),
            png_factory=self._signal_png_factory(
                symbol=symbol,
                event_id=event_id,
                side=side,
                tf_label=tf_label,
                bar_tf=bar_tf,
                bars=bars,
                box_lo=box_lo,
                box_hi=box_hi,
                ref_price=ref_price,
                invalid=invalid,
                target_1=target_1,
                zone_lo=zone_lo,
                zone_hi=zone_hi,
                zone_touches=zone_touches,
                pivots=pivots,
                atr=atr,
                direction=d_int,
                box_start_ts=box_start_ts,
            ),
        )

    @staticmethod
    def _signal_png_factory(**kw: Any) -> Callable[[], bytes | None] | None:
        """仅当真有 K 线时才建渲染闭包(无 K 线 ⇒ None ⇒ 纯文本发送)。"""
        bars = kw.get("bars")
        if not bars:
            return None
        d_int = int(kw.get("direction") or 1)
        dir_tag = "LONG" if d_int > 0 else "SHORT"
        chart_tf = str(kw.get("bar_tf") or kw["tf_label"] or "")
        decision = str(kw.get("tf_label") or "")
        title = f"SIGNAL {dir_tag}"
        if chart_tf and decision and chart_tf != decision:
            title = f"{title} {decision}"

        def _render() -> bytes | None:
            return render_bw_kline_png(
                bars,
                symbol=kw["symbol"],
                tf_label=chart_tf or decision,
                title_tag=title,
                box_lo=kw["box_lo"],
                box_hi=kw["box_hi"],
                entry_price=kw["ref_price"],
                stop_price=kw["invalid"],
                target_price=kw["target_1"],
                direction=kw["direction"],
                zone_lo=kw["zone_lo"],
                zone_hi=kw["zone_hi"],
                zone_touches=kw["zone_touches"],
                pivots=kw["pivots"],
                atr=kw["atr"],
                max_bars=96,
                box_start_ts=kw.get("box_start_ts"),
                public_mode=True,
            )

        return _render

    def send_fill(
        self,
        symbol: str,
        fill_kind: str,
        qty: float,
        price: float,
        pnl: float | None = None,
        note: str = "",
        *,
        tf_label: str = "",
        direction: int | None = None,
        stop_price: float | None = None,
        target_price: float | None = None,
        box_lo: float | None = None,
        box_hi: float | None = None,
        bars: Sequence[Sequence[Any]] | None = None,
        entry_price: float | None = None,
        price_label: str = "成交价格",
        zone_lo: float | None = None,
        zone_hi: float | None = None,
        pivots: Sequence[Sequence[Any]] | None = None,
        atr: float | None = None,
        box_start_ts: int | None = None,
        level_kind: str = "",
        level_role: str = "",
    ) -> bool:
        """成交通知(运营通道,附黑白 K 线证据图)。"""
        is_update = price_label.startswith("参考价")
        lines = [
            f"⚡ {'持仓更新' if is_update else '交易执行'}: {symbol}",
            f"🏷️ 动作类型: {fill_kind}",
        ]
        if tf_label:
            lines.append(f"⏱️ 所属周期: {tf_label}")
        lines.extend([
            f"📦 {'持仓数量' if is_update else '成交数量'}: {qty:.8g}",
            f"💵 {price_label}: {price:.8g}",
        ])
        if entry_price is not None:
            lines.append(f"🎯 建仓均价: {entry_price:.8g}")
        if direction in (-1, 1):
            lines.append(f"🧭 持仓方向: {'多' if direction > 0 else '空'}")
        if stop_price is not None:
            lines.append(f"🛡️ 当前止损: {stop_price:.8g}")
        if target_price is not None:
            lines.append(f"🏁 结构目标: {target_price:.8g}")
        if box_lo is not None and box_hi is not None:
            lines.append(f"📦 水平带: [{float(box_lo):.8g}, {float(box_hi):.8g}]")
            pos_txt = box_position_text(
                price if entry_price is None else entry_price, box_lo, box_hi
            )
            if pos_txt:
                lines.append(f"📍 入场带内: {pos_txt}")
        if pnl is not None:
            pnl_emoji = "💰" if pnl >= 0 else "💸"
            lines.append(
                f"{pnl_emoji} 本次平仓估算盈亏: {pnl:+.2f} USDT（按当前记账口径，非整笔净收益）"
            )
        if note:
            clean_note = str(note)
            for src, dst in _FILL_NOTE_REPLACEMENTS:
                clean_note = clean_note.replace(src, dst)
            lines.append(f"📝 详情说明: {clean_note}")

        png_factory: Callable[[], bytes | None] | None = None
        if bars:
            note_txt = level_note_text(level_kind, level_role)
            ent_px = price if entry_price is None else float(entry_price)

            def _render_fill() -> bytes | None:
                return render_bw_kline_png(
                    bars,
                    symbol=symbol,
                    tf_label=tf_label,
                    title_tag=f"TRADE {fill_kind}",
                    box_lo=box_lo,
                    box_hi=box_hi,
                    entry_price=ent_px,
                    stop_price=stop_price,
                    target_price=target_price,
                    direction=direction,
                    zone_lo=zone_lo,
                    zone_hi=zone_hi,
                    pivots=pivots,
                    atr=atr,
                    max_bars=96,
                    box_start_ts=box_start_ts,
                    level_note=note_txt,
                )

            png_factory = _render_fill
        body = "\n".join(lines)
        if is_update:
            body = "【持仓更新】\n" + body
        return self.send("fill", body, png_factory=png_factory)

    def send_watch(self, symbol: str, tf_label: str, status: str, reason: str) -> bool:
        pretty_reason = _WATCH_REASONS.get(reason, reason)
        return self.send(
            "watch",
            f"👁️ 价格关注更新: {symbol}\n"
            f"⏱️ 所属周期: {tf_label}\n状态: {status}\n原因: {pretty_reason}\n"
            "关注触发不等于成交，成交以运营通道开仓通知为准。",
        )

    def send_daily(
        self,
        date: str,
        equity: float,
        day_pnl: float,
        n_signals: int,
        n_fills: int,
        n_positions: int,
        drawdown_frac: float,
    ) -> bool:
        """日结走运营通道:账户权益/持仓数属于私有信息,绝不进公开信号群。"""
        lines = [
            f"📅 结算日期: {date}",
            f"🏦 账户权益: {equity:.2f} USDT",
            f"📊 当日盈亏: {day_pnl:+.2f} USDT",
            f"📉 峰值回撤: {drawdown_frac:.1%}",
            f"📡 当日信号: {n_signals}",
            f"⚡ 当日成交: {n_fills}",
            f"📌 当前持仓: {n_positions}",
        ]
        return self.send("daily", "\n".join(lines))

    def send_alert(self, text: str) -> bool:
        """告警走运营通道(熔断/断线/下单失败都是运营信息,不能广播)。"""
        lines = [
            line
            if line.strip().startswith(("⚠️", "🚨", "📉", "🛡️", "❗"))
            else f"🚨 {line.strip()}"
            for line in text.splitlines()
            if line.strip()
        ]
        return self.send("alert", "\n".join(lines))


__all__ = [
    "CHANNEL_OPS",
    "CHANNEL_SIGNAL",
    "NotificationGuard",
    "TelegramNotifier",
    "_bar_gap_stats",
    "box_position_pct",
    "box_position_text",
    "render_bw_kline_png",
    "render_level_map_png",
    "rr_multiple",
    "split_text",
]
