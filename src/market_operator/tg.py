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
  (`sendPhoto` + `caption`);图上带平衡箱体、事件区间(zone)、`±R` 标尺、
  pivot 与入场/止损/目标位,读图即可回答"这笔交易在结构里的哪个位置";
- 每一行仅一条关键信息,行首配 Emoji;
- 渲染 + 上传都可放进后台队列(`png_factory`):实盘主循环只做一次
  `put_nowait`,Pillow 与网络全部在守护线程,信号链路不被阻塞;
- 异常完全吞吐隔离:发送/渲染失败只记 dropped,绝不 raise、绝不中断交易流程。
"""

from __future__ import annotations

import io
import json
import math
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
API_PHOTO = "https://api.telegram.org/bot{token}/sendPhoto"
RETRIES = 5
RETRY_BACKOFF_S = 1.5
TIMEOUT_S = 15
MAX_LEN = 4000
MAX_CAPTION_LEN = 1024

_KIND_TAG = {"signal": "信号", "fill": "成交", "daily": "日结", "alert": "告警",
             "watch": "关注状态"}

CHANNEL_SIGNAL = "signal"      # 公开广播:只发信号
CHANNEL_OPS = "ops"            # 私有运营:成交/日结/告警

# 唯一分流事实:消息类型 → 通道。新增消息类型必须在这里显式登记,
# 不做"默认落到信号通道"的隐式回退(公开频道绝不能收到运营信息)。
_KIND_CHANNEL = {
    "signal": CHANNEL_SIGNAL,
    "fill": CHANNEL_OPS,
    "daily": CHANNEL_OPS,
    "alert": CHANNEL_OPS,
    "watch": CHANNEL_OPS,
}

_EVENT_DISPLAY = {
    "BREAKOUT_ACCEPTED": "ACCEPT (突破接受)",
    "ACCEPT": "ACCEPT (突破接受)",
    "LEVEL_REJECTED": "REJECT (边界拒绝)",
    "REJECT": "REJECT (边界拒绝)",
}

_ROUTE_DISPLAY = {
    "breakout_accepted_market": "ACCEPT 市价",
    "level_rejected_market": "REJECT 市价",
    "accept_market": "ACCEPT 市价",
    "reject_market": "REJECT 市价",
    "breakout_accepted_limit": "ACCEPT 限价意图",
    "level_rejected_limit": "REJECT 限价意图",
    "e1_market": "ACCEPT 市价",
    "e3_market": "REJECT 市价",
}


def format_event_display(event_id: str) -> str:
    return _EVENT_DISPLAY.get(str(event_id).strip(), str(event_id))


def format_route_display(route: str) -> str:
    r = str(route).strip()
    if r in _ROUTE_DISPLAY:
        return _ROUTE_DISPLAY[r]
    return r.replace("breakout_accepted_market", "ACCEPT 市价").replace("level_rejected_market", "REJECT 市价")


def _fmt_px(value: float) -> str:
    """价格紧凑显示:8 位有效数字,0.000012345 这类小币价不会被挤成科学计数法。"""
    return f"{float(value):.8g}"


def box_position_pct(price: Optional[float], box_lo: Optional[float],
                     box_hi: Optional[float]) -> Optional[float]:
    """价格在箱体内的相对位置(0=下沿,1=上沿);缺边/箱体退化 ⇒ None。

    纯展示量,不参与任何交易判定,故允许落在箱外(返回 <0 或 >1 的原值)。
    """
    try:
        px, lo, hi = float(price), float(box_lo), float(box_hi)
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    return (px - lo) / (hi - lo)


def box_position_text(price: Optional[float], box_lo: Optional[float],
                      box_hi: Optional[float]) -> str:
    """箱内位置的人读短语(百分比 + 靠上/靠下/居中),缺失时返回空串。"""
    pct = box_position_pct(price, box_lo, box_hi)
    if pct is None:
        return ""
    tone = "靠上沿" if pct >= 0.66 else ("靠下沿" if pct <= 0.34 else "居中")
    return f"{pct:.0%} · {tone}"


def rr_multiple(direction: Optional[int], entry: Optional[float],
                stop: Optional[float], target: Optional[float]) -> Optional[float]:
    """按交易方向算 R:R(风险距离无效 ⇒ None);无方向时退化为距离比。"""
    try:
        e, s, tg = float(entry), float(stop), float(target)
    except (TypeError, ValueError):
        return None
    risk = abs(e - s)
    if risk <= 0.0:
        return None
    if direction is None:
        return abs(tg - e) / risk
    d = 1 if float(direction) > 0 else -1
    return d * (tg - e) / risk


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
    zone_lo: Optional[float] = None,
    zone_hi: Optional[float] = None,
    zone_touches: Optional[int] = None,
    pivots: Optional[Sequence[tuple]] = None,
    atr: Optional[float] = None,
    max_bars: int = 48,
    log_scale: bool = True,
) -> Optional[bytes]:
    """用 Pillow 绘制高对比度黑白 K 线图,返回 PNG bytes(无 Pillow / 无 K 线 ⇒ None)。

    图上叠加的结构事实与策略决策同源,不做二次推断:

    - 平衡箱体 `box_lo/box_hi`:浅灰带 + 上下沿粗虚线 + 右栏 `BOX HI/LO`;
    - 事件区间 `zone_lo/zone_hi`(`zone_touches` 为触边次数):斜线填充 + 细虚线
      + 右栏 `ZONE HI/LO` —— 回答"这次触发发生在箱体的哪一段";
    - `±R` 标尺:以 `entry_price`/`stop_price` 定义 1R,顺方向画出 `+1R/+2R/+3R`;
    - `pivots`:`[(下标, 价格, "high"|"low")]`,三角标记 + 最近两个价位标签,
      下标越界/缺失只画到窗口末根,不猜位置;
    - `atr`/`zone_touches` 只进表头摘要,不改变坐标轴。

    远端 pivot(超出 K 线窗口 30% 振幅)不纳入纵轴,否则最近几十根会被压成
    一条线。默认对数坐标,不同价格级别都能看清箱体厚度。
    """
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

    def _num(value) -> Optional[float]:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) and f > 0 else None

    box_lo, box_hi = _num(box_lo), _num(box_hi)
    if box_lo is not None and box_hi is not None and box_hi < box_lo:
        box_lo, box_hi = box_hi, box_lo
    zone_lo, zone_hi = _num(zone_lo), _num(zone_hi)
    if zone_lo is not None and zone_hi is not None and zone_hi < zone_lo:
        zone_lo, zone_hi = zone_hi, zone_lo
    entry_price = _num(entry_price)
    stop_price = _num(stop_price)
    target_price = _num(target_price)
    atr = _num(atr)

    piv: list[tuple[Optional[int], float, str]] = []
    for item in (pivots or ()):
        try:
            raw_idx, raw_price, raw_kind = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        price = _num(raw_price)
        if price is None:
            continue
        piv.append((raw_idx if isinstance(raw_idx, int) else None,
                    price, str(raw_kind).lower()))

    highs = [float(b[2]) for b in sub]
    lows = [float(b[3]) for b in sub]
    p_max, p_min = max(highs), min(lows)
    for lvl in (box_lo, box_hi, entry_price, stop_price, target_price,
                zone_lo, zone_hi):
        if lvl is not None:
            p_max = max(p_max, lvl)
            p_min = min(p_min, lvl)

    bar_span = max(p_max - p_min, p_max * 0.002, 1e-9)
    near_piv = [p for p in piv
                if p_min - bar_span * 0.3 <= p[1] <= p_max + bar_span * 0.3]
    for _idx, price, _kind in near_piv:
        p_max = max(p_max, price)
        p_min = min(p_min, price)

    use_log = bool(log_scale and p_min > 0 and p_max > 0)
    if use_log:
        log_max, log_min = math.log(p_max), math.log(p_min)
        log_span = max(log_max - log_min, 0.002, 1e-8)
        log_pad = log_span * 0.08
        log_hi, log_lo = log_max + log_pad, log_min - log_pad
        y_hi, y_lo = math.exp(log_hi), math.exp(log_lo)
    else:
        span = max(p_max - p_min, p_max * 0.002, 1e-8)
        pad = span * 0.08
        y_hi, y_lo = p_max + pad, p_min - pad

    W, H = 1000, 560
    left_m, right_m, top_m, bot_m = 58, 156, 62, 44
    plot_w, plot_h = W - left_m - right_m, H - top_m - bot_m

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    def _load_font(size: int, bold: bool = False):
        """优先中文字体(图上有中文摘要),再退 ASCII 字体;返回 (font, cjk_ok)。"""
        names = (("msyhbd.ttc", "simhei.ttf", "arialbd.ttf") if bold
                 else ("msyh.ttc", "simhei.ttf", "arial.ttf"))
        for name in names:
            try:
                return ImageFont.truetype(name, size), name != "arial.ttf" and name != "arialbd.ttf"
            except Exception:
                continue
        return ImageFont.load_default(), False

    font_s, cjk_s = _load_font(11)
    font, _cjk = _load_font(13)
    font_b, cjk_b = _load_font(15, bold=True)
    cjk_ok = bool(cjk_s or cjk_b)

    def text_w(txt: str, fnt) -> float:
        try:
            return float(draw.textlength(txt, font=fnt))
        except Exception:
            return float(len(txt) * 6)

    def price_to_y(p: float) -> int:
        pf = float(p)
        if use_log and pf > 0:
            frac = (log_hi - math.log(pf)) / (log_hi - log_lo)
        else:
            frac = (y_hi - pf) / (y_hi - y_lo)
        return int(round(top_m + max(0.0, min(1.0, frac)) * plot_h))

    # 右侧标签栏:所有水平线/箱体/区间先登记,统一避让后再画 ——
    # 修前每条线各自把文字压在图上,价位接近时标签互相盖住。
    rail: list[list] = []

    def rail_add(y: float, text: str, fill=(0, 0, 0)) -> None:
        rail.append([int(y), str(text), fill])

    def rail_flush() -> None:
        rail.sort(key=lambda r: r[0])
        last = -10 ** 6
        for y, text, fill in rail:
            yy = min(max(int(y), last + 12), H - bot_m - 6)
            draw.text((left_m + plot_w + 8, yy - 6), text, fill=fill, font=font_s)
            last = yy

    def hline(y: int, on: int, off: int, fill, width: int = 1) -> None:
        x = left_m
        while x < left_m + plot_w:
            draw.line([(x, y), (min(x + on, left_m + plot_w), y)],
                      fill=fill, width=width)
            x += on + off

    # 平衡箱体:浅灰带 + 上下沿粗虚线
    if box_lo is not None and box_hi is not None and box_hi > box_lo:
        y1, y2 = price_to_y(box_hi), price_to_y(box_lo)
        if y2 > y1:
            draw.rectangle([left_m, y1, left_m + plot_w, y2], fill=(240, 240, 240))
        hline(y1, 8, 4, (0, 0, 0), 2)
        hline(y2, 8, 4, (0, 0, 0), 2)
        rail_add(y1, f"BOX HI {_fmt_px(box_hi)}")
        rail_add(y2, f"BOX LO {_fmt_px(box_lo)}")

    # 事件区间:斜线填充 + 细虚线(与箱体区分:箱体是横带,区间是触发段)
    if zone_lo is not None and zone_hi is not None and zone_hi >= zone_lo:
        yz1, yz2 = price_to_y(zone_hi), price_to_y(zone_lo)
        if yz2 - yz1 >= 2:
            x = left_m
            while x < left_m + plot_w:
                draw.line([(x, yz2), (min(x + 6, left_m + plot_w), yz1)],
                          fill=(198, 198, 198), width=1)
                x += 9
        hline(yz1, 3, 3, (70, 70, 70))
        hline(yz2, 3, 3, (70, 70, 70))
        touch_txt = ""
        if zone_touches:
            n_touch = int(zone_touches)
            touch_txt = (f" · {n_touch}触" if cjk_ok
                         else f" · {n_touch} touch")
        rail_add(yz1, f"ZONE HI {_fmt_px(zone_hi)}{touch_txt}", (70, 70, 70))
        rail_add(yz2, f"ZONE LO {_fmt_px(zone_lo)}", (70, 70, 70))

    # 网格(对数等分)+ 左轴刻度
    tick_vals = [
        (math.exp(log_lo + (log_hi - log_lo) * (k / 4.0)) if use_log
         else y_lo + (y_hi - y_lo) * (k / 4.0))
        for k in range(5)
    ]
    # 同一张图的刻度必须同精度:修前用 `.6g`,113.503 与 98.1832 混排,相邻
    # 刻度的有效位不同,读者会误以为刻度不等距。按刻度间距定小数位。
    tick_step = abs(tick_vals[1] - tick_vals[0]) if len(tick_vals) > 1 else 0.0
    tick_dec = (max(0, min(8, 2 - int(math.floor(math.log10(tick_step)))))
                if tick_step > 0 else 2)
    for p_val in tick_vals:
        yk = price_to_y(p_val)
        for x in range(left_m, left_m + plot_w, 12):
            draw.point((x, yk), fill=(206, 206, 206))
        draw.text((4, yk - 6), f"{p_val:.{tick_dec}f}",
                  fill=(110, 110, 110), font=font_s)

    # ±R 标尺:以 entry/stop 定义 1R,顺方向画出 +1R/+2R/+3R
    d_int = 0
    if direction is not None:
        try:
            d_int = 1 if float(direction) > 0 else (-1 if float(direction) < 0 else 0)
        except (TypeError, ValueError):
            d_int = 0
    if entry_price is not None and stop_price is not None and d_int:
        r_unit = abs(entry_price - stop_price)
        if r_unit > 0:
            for k in (1, 2, 3):
                lvl = entry_price + d_int * k * r_unit
                if not (y_lo <= lvl <= y_hi):
                    continue
                yk = price_to_y(lvl)
                hline(yk, 2, 5, (160, 160, 160))
                rail_add(yk, f"+{k}R {_fmt_px(lvl)}", (140, 140, 140))

    def _draw_level(px: Optional[float], label: str, dash: int = 4,
                    width: int = 1, color=(0, 0, 0)) -> None:
        if px is None or px <= 0:
            return
        yy = price_to_y(px)
        hline(yy, dash, max(2, dash - 1), color, width)
        rail_add(yy, f"{label} {_fmt_px(px)}", color)

    _draw_level(entry_price, "ENTRY", dash=7, width=2)
    _draw_level(stop_price, "STOP", dash=3, width=2)
    _draw_level(target_price, "TARGET", dash=5, width=2, color=(40, 40, 40))

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

    # 结构端点:三角标记 + 最近两个价位标签(不标就读不出哪根是 pivot)
    labeled = 0
    for p_idx, p_price, p_kind in reversed(near_piv):
        if p_idx is None or not (0 <= p_idx < n):
            continue
        pcx = int(round(left_m + (p_idx + 0.5) * step_x))
        py = price_to_y(p_price)
        is_high = p_kind.startswith("h")
        if is_high:
            tri = [(pcx, max(top_m + 12, py - 4)),
                   (pcx - 5, max(top_m + 4, py - 12)),
                   (pcx + 5, max(top_m + 4, py - 12))]
            ly = max(top_m + 2, py - 26)
        else:
            tri = [(pcx, min(top_m + plot_h - 12, py + 4)),
                   (pcx - 5, min(top_m + plot_h - 4, py + 12)),
                   (pcx + 5, min(top_m + plot_h - 4, py + 12))]
            ly = min(top_m + plot_h - 18, py + 14)
        draw.polygon(tri, fill=(60, 60, 60))
        if labeled < 2:
            label = f"P{'H' if is_high else 'L'} {_fmt_px(p_price)}"
            w_txt = text_w(label, font_s)
            right_lim = left_m + plot_w - w_txt - 2
            lx = pcx + 7
            if lx > right_lim:              # 贴近右沿:改放三角左侧,别压住最后几根 K 线
                lx = max(left_m + 2, pcx - w_txt - 7)
            draw.rectangle([lx - 1, ly - 1, lx + w_txt + 1, ly + 13],
                           fill=(255, 255, 255))     # 底衬:标注压影线时仍可读
            draw.text((lx, ly), label, fill=(60, 60, 60), font=font_s)
            labeled += 1

    # 触发 bar 标记(若在窗口内)
    m_idx = (n - 1) if mark_index is None else max(0, min(n - 1, mark_index))
    mcx = int(round(left_m + (m_idx + 0.5) * step_x))
    mb = sub[m_idx]
    if direction is not None and direction < 0:
        my = max(top_m + 6, price_to_y(float(mb[2])) - 14)
        draw.polygon([(mcx, my + 8), (mcx - 5, my), (mcx + 5, my)], fill=(0, 0, 0))
    else:
        my = min(top_m + plot_h - 6, price_to_y(float(mb[3])) + 14)
        draw.polygon([(mcx, my - 8), (mcx - 5, my), (mcx + 5, my)], fill=(0, 0, 0))

    rail_flush()
    draw.rectangle([left_m, top_m, left_m + plot_w, top_m + plot_h],
                   outline=(0, 0, 0), width=2)

    ts_first = datetime.fromtimestamp(int(sub[0][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
    ts_last = datetime.fromtimestamp(int(sub[-1][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
    scale_tag = "LOG" if use_log else "LIN"
    head = f"{symbol} [{tf_label}] {title_tag} [{scale_tag}]".strip()
    draw.text((left_m, 6), head, fill=(0, 0, 0), font=font_b)
    draw.text((left_m, 36), f"{ts_first} -> {ts_last}", fill=(90, 90, 90), font=font_s)

    # 结构摘要(全部来自同一批入参,不做二次推断)
    bits: list[str] = []
    pos_txt = box_position_text(entry_price, box_lo, box_hi)
    rr = rr_multiple(d_int, entry_price, stop_price, target_price)
    if rr is not None:
        bits.append(f"R:R {rr:.2f}")
    if atr is not None and entry_price:
        bits.append(f"ATR {atr / entry_price:.2%}")
    if box_lo is not None and box_hi is not None and entry_price:
        bits.append((f"箱体跨度 {(box_hi - box_lo) / entry_price:.2%}" if cjk_ok
                     else f"BOX span {(box_hi - box_lo) / entry_price:.2%}"))
    if zone_touches:
        bits.append((f"触边 {int(zone_touches)} 次" if cjk_ok
                     else f"touch {int(zone_touches)}x"))
    if pos_txt:
        bits.append((f"入场箱内 {pos_txt}") if cjk_ok
                    else f"entry in box {box_position_pct(entry_price, box_lo, box_hi):.0%}")
    if bits:
        summary = " | ".join(bits)
        draw.text((max(left_m, left_m + plot_w - text_w(summary, font_s)), 36),
                  summary, fill=(50, 50, 50), font=font_s)

    # 底轴时间标签(约 5 个)
    for k in range(5):
        idx = int(round((n - 1) * k / 4.0))
        ts = int(sub[idx][0])
        dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
        x = int(left_m + (idx + 0.5) * step_x)
        tw = text_w(dt, font_s)
        draw.text((min(max(left_m, x - tw / 2), left_m + plot_w - tw),
                   H - bot_m + 12), dt, fill=(90, 90, 90), font=font_s)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()



class NotificationGuard:
    """Runner boundary: notifier method failures cannot abort bookkeeping.

    Does not create threads or change synchronous/async transport policy. The live
    entrypoint continues to provide an async TelegramNotifier.
    """

    def __init__(self, notifier) -> None:
        self._notifier = notifier
        self.errors = 0

    def __getattr__(self, name):
        value = getattr(self._notifier, name)
        if not name.startswith("send") or not callable(value):
            return value

        def guarded(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except Exception:
                self.errors += 1
                return False
        return guarded


class TelegramNotifier:
    """双通道发信器(公开信号 Bot + 私有运营 Bot) + 黑白 K 线图同条消息发送。

    分流由 `_KIND_CHANNEL` 决定(见模块 docstring),调用方不需要、也不允许
    手写通道开关:一张表就是唯一事实。无网络 / 未配置 / 任何异常都只记账,
    永不 raise —— 通知链路不能反噬交易链路。
    """

    def __init__(self, secrets: Optional[dict] = None, *,
                 session: Optional[requests.Session] = None,
                 dropped_log: Optional[Path] = None,
                 max_retries: int = RETRIES,
                 async_mode: bool = False) -> None:
        secrets = secrets or {}
        self._token = secrets.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = secrets.get("TELEGRAM_CHAT_ID", "")
        # 运营通道:未单独配置时回退到信号机器人(宁可与信号同房间,也不静默丢
        # 告警);是否"真的分开了"由 ops_channel_separate 如实回答。
        # 注意:secrets_loader 已把 `TELEGRAM_TRADE_*` 回退填满,所以这里不能
        # 用"键存在与否"判断显式配置 —— 必须比较解析后的 token/chat 是否不同。
        self._ops_token = secrets.get("TELEGRAM_TRADE_BOT_TOKEN", "") or self._token
        self._ops_chat_id = secrets.get("TELEGRAM_TRADE_CHAT_ID", "") or self._chat_id
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
        """信号通道可用(公开广播的最低前提)。"""
        return bool(self._token and self._chat_id)

    @property
    def ops_configured(self) -> bool:
        """运营通道可用(回退算可用:同一机器人也能收)。"""
        return bool(self._ops_token and self._ops_chat_id)

    @property
    def ops_channel_separate(self) -> bool:
        """运营通道是否真的与信号通道分开(启动日志据此提示)。

        判据是解析后的 token/chat 不同 —— 只设了一个键、另一个回退,仍然算
        分开(比如同一 Bot 推到运维群)。
        """
        if not (self._ops_token and self._ops_chat_id):
            return False
        return (self._ops_token != self._token
                or self._ops_chat_id != self._chat_id)

    @property
    def trade_configured(self) -> bool:
        """历史别名:运营通道可用。"""
        return self.ops_configured

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

    def _post_photo_or_render(self, token: str, chat_id: str, kind: str,
                              body: str, png_bytes: Optional[bytes],
                              png_factory: Optional[Callable[[], Optional[bytes]]]) -> bool:
        """先拿到图(必要时此刻才渲染)再发;渲染失败退回纯文本,不丢消息本身。"""
        png = png_bytes
        if png is None and png_factory is not None:
            try:
                png = png_factory()
            except Exception as e:      # 绘图异常(坏 K 线/字体)不得吞掉文字通知
                self._record_miss("render", e)
                png = None
        if png:
            if len(body) > MAX_CAPTION_LEN:
                # Preserve watch conditions/status instead of silently chopping
                # them off a photo caption. Both sends run on the same worker.
                text_ok = self._post_text_sync(token, chat_id, kind, body)
                photo_ok = self._post_photo_sync(token, chat_id, kind, "K线结构图（详见配套文字）", png)
                return text_ok and photo_ok
            return self._post_photo_sync(token, chat_id, kind, body, png)
        return self._post_text_sync(token, chat_id, kind, body)

    def channel_of(self, kind: str) -> str:
        """消息类型 → 通道(未知类型落运营通道:宁可多给运营看,也不进公开群)。"""
        return _KIND_CHANNEL.get(kind, CHANNEL_OPS)

    def send(self, kind: str, text: str, *, channel: Optional[str] = None,
             png_bytes: Optional[bytes] = None,
             png_factory: Optional[Callable[[], Optional[bytes]]] = None) -> bool:
        """发信(图文同条消息 + 按类型分流通道);异常完全隔离,绝不 raise。

        `png_factory` 是**延迟渲染**:异步模式下渲染发生在守护线程里,实盘主循环
        只付出一次入队成本,绘图耗时不会挤进策略决策窗口。
        """
        chan = channel or self.channel_of(kind)
        if chan == CHANNEL_SIGNAL:
            token, chat_id = self._token, self._chat_id
        else:
            token, chat_id = self._ops_token, self._ops_chat_id
        tag = _KIND_TAG.get(kind, kind)
        prefix = "" if text.startswith("【") else f"【{tag}】\n"
        body = prefix + (text if len(text) <= MAX_LEN
                         else text[:MAX_LEN - 20] + "…[截断]")
        if png_bytes or png_factory is not None:
            return self._dispatch(self._post_photo_or_render, token, chat_id,
                                  kind, body, png_bytes, png_factory)
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
                    target_1: Optional[float] = None,
                    zone_lo: Optional[float] = None,
                    zone_hi: Optional[float] = None,
                    zone_touches: Optional[int] = None,
                    pivots: Optional[Sequence[tuple]] = None,
                    atr: Optional[float] = None,
                    exec_status: str = "",
                    decision_ts: Optional[int] = None,
                    watch_details: Optional[dict] = None) -> bool:
        """信号广播(公开通道):结构事实 + 系统如何处置,一条消息读完。

        纪律:这里**只写结构与决策**,不写账户权益/持仓数量/下单成败 —— 该频道
        可以公开订阅。`exec_status` 由 runner 按闸门结果填写(如
        "仅参考 · 组合容量已满"),让订阅者知道"信号是真的,但本系统这拍没上车"。
        """
        long_side = side in ("long", "buy", "多")
        dir_emoji = "🟢" if long_side else "🔴"
        d_int = 1 if long_side else -1
        ev_disp = format_event_display(event_id)
        rt_disp = "系统价格关注 Watch（非交易所限价挂单）" if watch_details else format_route_display(route)
        lines = [
            f"📡 信号触发: {symbol}",
            f"⏱️ 交易周期: {tf_label or 'HTF'}",
            f"🧭 原语事件: {ev_disp} [{regime or 'BALANCE'}]",
            f"{dir_emoji} 交易方向: {side}",
            f"🛣️ 路由方式: {rt_disp}",
            f"🎯 参考价格: {ref_price:.8g}",
            f"🛡️ 初始止损: {invalid:.8g}",
        ]
        rr = rr_multiple(d_int, ref_price, invalid, target_1)
        if target_1 is not None:
            rr_txt = f" (R:R {rr:.2f})" if rr is not None else ""
            lines.append(f"🏁 结构目标: {float(target_1):.8g}{rr_txt}")
        if box_lo is not None and box_hi is not None:
            span_txt = ""
            if ref_price:
                span_txt = (f" · 跨度 {(float(box_hi) - float(box_lo)) / float(ref_price):.2%}")
            lines.append(f"📦 平衡箱体: [{float(box_lo):.8g}, "
                         f"{float(box_hi):.8g}]{span_txt}")
            pos_txt = box_position_text(ref_price, box_lo, box_hi)
            if pos_txt:
                lines.append(f"📍 箱内位置: {pos_txt}")
        if zone_lo is not None and zone_hi is not None:
            touch_txt = f" · 触边 {int(zone_touches)} 次" if zone_touches else ""
            lines.append(f"🧱 事件区间: [{float(zone_lo):.8g}, "
                         f"{float(zone_hi):.8g}]{touch_txt}")
        if atr:
            atr_txt = (f"{float(atr) / float(ref_price):.2%}" if ref_price
                       else f"{float(atr):.8g}")
            lines.append(f"📏 波动基准: ATR {atr_txt}")
        if size_note:
            clean_note = size_note.strip().lstrip("|").strip()
            if clean_note:
                lines.append(f"📐 仓位参数: {clean_note}")
        if exec_status:
            lines.append(f"🤖 系统执行: {exec_status}")
        if watch_details:
            for key, label in (("watch_price", "关注触发价"),
                               ("near_price", "近价区（触及后需企稳确认）"),
                               ("prog_price", "行情走完作废价")):
                value = watch_details.get(key)
                if value is not None:
                    lines.append(f"👁️ {label}: {float(value):.8g}")
            lines.append(f"⌛ 关注期限: {watch_details['max_wait_hours']} 小时（自决策时刻起）")
            lines.append("ℹ️ Watch规则（仅在系统实际建立关注后适用）：失效位被破、行情走完或到期会作废。关注不等于成交，触发后仍需执行检查。")
        if decision_ts:
            try:
                stamp = datetime.fromtimestamp(
                    int(decision_ts) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
                lines.append(f"🕒 决策时刻: {stamp}")
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        return self.send("signal", "\n".join(lines),
                         png_factory=self._signal_png_factory(
                             symbol=symbol, event_id=event_id, side=side,
                             tf_label=tf_label, bars=bars, box_lo=box_lo,
                             box_hi=box_hi, ref_price=ref_price, invalid=invalid,
                             target_1=target_1, zone_lo=zone_lo, zone_hi=zone_hi,
                             zone_touches=zone_touches, pivots=pivots, atr=atr,
                             direction=d_int))

    @staticmethod
    def _signal_png_factory(**kw) -> Optional[Callable[[], Optional[bytes]]]:
        """仅当真有 K 线时才建渲染闭包(无 K 线 ⇒ None ⇒ 纯文本发送)。"""
        bars = kw.get("bars")
        if not bars:
            return None
        side = str(kw["side"])
        event_id = str(kw["event_id"])
        ev_short = ("ACCEPT" if event_id in ("BREAKOUT_ACCEPTED", "ACCEPT")
                    else ("REJECT" if event_id in ("LEVEL_REJECTED", "REJECT")
                          else event_id))

        def _render() -> Optional[bytes]:
            return render_bw_kline_png(
                bars, symbol=kw["symbol"], tf_label=kw["tf_label"],
                title_tag=f"SIGNAL {ev_short} {side.upper()}",
                box_lo=kw["box_lo"], box_hi=kw["box_hi"],
                entry_price=kw["ref_price"], stop_price=kw["invalid"],
                target_price=kw["target_1"], direction=kw["direction"],
                zone_lo=kw["zone_lo"], zone_hi=kw["zone_hi"],
                zone_touches=kw["zone_touches"], pivots=kw["pivots"],
                atr=kw["atr"], max_bars=60)

        return _render

    def send_fill(self, symbol: str, fill_kind: str, qty: float, price: float,
                  pnl: Optional[float] = None, note: str = "", *,
                  tf_label: str = "", direction: Optional[int] = None,
                  stop_price: Optional[float] = None,
                  target_price: Optional[float] = None,
                  box_lo: Optional[float] = None,
                  box_hi: Optional[float] = None,
                  bars: Optional[Sequence[tuple]] = None,
                  entry_price: Optional[float] = None,
                  price_label: str = "成交价格",
                  zone_lo: Optional[float] = None,
                  zone_hi: Optional[float] = None,
                  pivots: Optional[Sequence[tuple]] = None,
                  atr: Optional[float] = None) -> bool:
        """成交通知(运营通道,附黑白 K 线证据图)。

        `price` = **本次事件价**(成交价 / 止损推进时的参考价);
        `entry_price` = 该仓的真实建仓均价 —— 修前图上一律用 `price` 画
        ENTRY 线,止损推进/平仓通知会把 ENTRY 画在"本次事件价"上(例如
        2h 收盘价 321.33 顶替建仓价 324.02),图上止损/目标相对 ENTRY 的
        关系整个错位,读图会把"空头锁利"误读成"止损跑到止盈侧"。
        `price_label` 供非成交类事件改写金额行文案(如"参考价")。
        """
        lines = [
            f"⚡ {'持仓更新' if price_label.startswith('参考价') else '交易执行'}: {symbol}",
            f"🏷️ 动作类型: {fill_kind}",
        ]
        if tf_label:
            lines.append(f"⏱️ 所属周期: {tf_label}")
        lines.extend([
            f"📦 {'持仓数量' if price_label.startswith('参考价') else '成交数量'}: {qty:.8g}",
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
            lines.append(f"📦 平衡箱体: [{float(box_lo):.8g}, {float(box_hi):.8g}]")
            pos_txt = box_position_text(
                price if entry_price is None else entry_price, box_lo, box_hi)
            if pos_txt:
                lines.append(f"📍 入场箱内: {pos_txt}")
        if pnl is not None:
            pnl_emoji = "💰" if pnl >= 0 else "💸"
            lines.append(f"{pnl_emoji} 本次平仓估算盈亏: {pnl:+.2f} USDT（按当前记账口径，非整笔净收益）")
        if note:
            clean_note = (str(note)
                          .replace("BREAKOUT_ACCEPTED e1_market", "ACCEPT (突破接受) | 市价")
                          .replace("LEVEL_REJECTED e3_market", "REJECT (刺穿拒绝) | 市价")
                          .replace("breakout_accepted_market", "ACCEPT 市价")
                          .replace("level_rejected_market", "REJECT 市价"))
            lines.append(f"📝 详情说明: {clean_note}")
        png_factory = None
        if bars:
            png_factory = lambda: render_bw_kline_png(  # noqa: E731
                bars, symbol=symbol, tf_label=tf_label,
                title_tag=f"TRADE {fill_kind}",
                box_lo=box_lo, box_hi=box_hi,
                entry_price=(price if entry_price is None else float(entry_price)),
                stop_price=stop_price,
                target_price=target_price, direction=direction,
                zone_lo=zone_lo, zone_hi=zone_hi, pivots=pivots, atr=atr,
                max_bars=60)
        body = "\n".join(lines)
        if price_label.startswith("参考价"):
            body = "【持仓更新】\n" + body
        return self.send("fill", body, png_factory=png_factory)

    def send_watch(self, symbol: str, tf_label: str, status: str, reason: str) -> bool:
        reason = {"WATCH_INNER_EDGE_HIT": "价格命中关注内沿",
                  "WATCH_NEAR_BOUNCE": "近价区触及后企稳确认",
                  "N_fill_expiry": "等待期限已过",
                  "position_exit": "持仓离场，关联关注作废"}.get(reason, reason)
        return self.send("watch", f"👁️ 价格关注更新: {symbol}\n"
                         f"⏱️ 所属周期: {tf_label}\n状态: {status}\n原因: {reason}\n"
                         "关注触发不等于成交，成交以运营通道开仓通知为准。")

    def send_daily(self, date: str, equity: float, day_pnl: float,
                   n_signals: int, n_fills: int, n_positions: int,
                   drawdown_frac: float) -> bool:
        """日结走**运营通道**:账户权益/持仓数属于私有信息,绝不进公开信号群。"""
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
        """告警走**运营通道**(熔断/断线/下单失败都是运营信息,不能广播)。"""
        lines = [
            line if line.strip().startswith(("⚠️", "🚨", "📉", "🛡️", "❗"))
            else f"🚨 {line.strip()}"
            for line in text.splitlines() if line.strip()
        ]
        return self.send("alert", "\n".join(lines))


__all__ = ["TelegramNotifier", "render_bw_kline_png", "box_position_pct",
           "box_position_text", "rr_multiple"]
