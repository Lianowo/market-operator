"""High-contrast Pillow chart renderers for Telegram signals and level maps.

Shares canvas layout, logarithmic/linear Y-axis scaling, right-rail collision
avoidance, dashed-line drawing, and font loading between ``render_bw_kline_png``
and ``render_level_map_png``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import math
from typing import Any

Color = tuple[int, int, int]

_BW_WIDTH = 1000
_BW_HEIGHT = 560
_BW_MARGINS = (58, 156, 62, 44)  # left, right, top, bottom

_MAP_WIDTH = 1060
_MAP_HEIGHT = 580
_MAP_MARGINS = (62, 195, 64, 44)

_PAD_FRAC = 0.08


def fmt_px(value: float) -> str:
    """Compact price formatter with 8 significant digits (no scientific clutter)."""
    return f"{float(value):.8g}"


def _positive_finite(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and num > 0.0 else None


def box_position_pct(
    price: float | None,
    box_lo: float | None,
    box_hi: float | None,
) -> float | None:
    """Relative position inside a level band (0=bottom, 1=top); None if invalid."""
    try:
        px, lo, hi = float(price), float(box_lo), float(box_hi)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    return (px - lo) / (hi - lo)


def box_position_text(
    price: float | None,
    box_lo: float | None,
    box_hi: float | None,
) -> str:
    """Human-readable in-band position phrase, or empty string if unavailable."""
    pct = box_position_pct(price, box_lo, box_hi)
    if pct is None:
        return ""
    tone = "靠上沿" if pct >= 0.66 else ("靠下沿" if pct <= 0.34 else "居中")
    return f"{pct:.0%} · {tone}"


def rr_multiple(
    direction: int | None,
    entry: float | None,
    stop: float | None,
    target: float | None,
) -> float | None:
    """Directional R:R multiple; None when risk distance is non-positive."""
    try:
        e, s, tg = float(entry), float(stop), float(target)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    risk = abs(e - s)
    if risk <= 0.0:
        return None
    if direction is None:
        return abs(tg - e) / risk
    side = 1 if float(direction) > 0 else -1
    return side * (tg - e) / risk


def bar_gap_stats(bars: Sequence[Sequence[Any]]) -> tuple[int, int, int]:
    """Return ``(count, median_step_minutes, gap_count)`` for a chart window."""
    count = len(bars)
    if count < 2:
        return count, 0, 0
    diffs: list[float] = []
    for prev, cur in zip(bars, bars[1:]):
        try:
            diff = (int(cur[0]) - int(prev[0])) / 60000.0
        except (TypeError, ValueError, IndexError):
            return count, 0, 0
        if diff > 0:
            diffs.append(diff)
    if not diffs:
        return count, 0, 0
    med = sorted(diffs)[len(diffs) // 2]
    gaps = sum(1 for diff in diffs if diff > med * 1.5 + 1e-9)
    return count, int(round(med)), gaps


def _load_font(image_font_mod: Any, size: int, *, bold: bool = False) -> tuple[Any, bool]:
    """Load CJK-capable TrueType font when available; return ``(font, cjk_ok)``."""
    names = (
        ("msyhbd.ttc", "simhei.ttf", "arialbd.ttf")
        if bold
        else ("msyh.ttc", "simhei.ttf", "arial.ttf")
    )
    for name in names:
        try:
            return image_font_mod.truetype(name, size), name not in ("arial.ttf", "arialbd.ttf")
        except OSError:
            continue
    return image_font_mod.load_default(), False


@dataclass(frozen=True)
class _AxisScale:
    """Logarithmic or linear price-to-pixel Y axis transform."""

    use_log: bool
    y_lo: float
    y_hi: float
    log_lo: float
    log_hi: float
    top_m: int
    plot_h: int

    @classmethod
    def from_bounds(
        cls,
        p_min: float,
        p_max: float,
        *,
        log_scale: bool,
        top_m: int,
        plot_h: int,
    ) -> _AxisScale:
        use_log = bool(log_scale and p_min > 0.0 and p_max > 0.0)
        if use_log:
            log_max, log_min = math.log(p_max), math.log(p_min)
            log_span = max(log_max - log_min, 0.002, 1e-8)
            log_pad = log_span * _PAD_FRAC
            log_hi, log_lo = log_max + log_pad, log_min - log_pad
            return cls(True, math.exp(log_lo), math.exp(log_hi), log_lo, log_hi, top_m, plot_h)
        span = max(p_max - p_min, p_max * 0.002, 1e-8)
        pad = span * _PAD_FRAC
        return cls(False, p_min - pad, p_max + pad, 0.0, 0.0, top_m, plot_h)

    def price_to_y(self, price: float) -> int:
        pf = float(price)
        if self.use_log and pf > 0.0:
            frac = (self.log_hi - math.log(pf)) / (self.log_hi - self.log_lo)
        else:
            frac = (self.y_hi - pf) / (self.y_hi - self.y_lo)
        return int(round(self.top_m + max(0.0, min(1.0, frac)) * self.plot_h))

    def tick_values(self, count: int = 5) -> list[float]:
        denom = float(max(count - 1, 1))
        if self.use_log:
            return [
                math.exp(self.log_lo + (self.log_hi - self.log_lo) * (k / denom))
                for k in range(count)
            ]
        return [self.y_lo + (self.y_hi - self.y_lo) * (k / denom) for k in range(count)]


class _LabelRail:
    """Right-margin price label list with vertical overlap avoidance."""

    def __init__(self, x_pos: int, max_y: int, min_gap: int = 12) -> None:
        self._x = x_pos
        self._max_y = max_y
        self._min_gap = min_gap
        self._items: list[tuple[int, str, Color]] = []

    def add(self, y: float, text: str, fill: Color = (0, 0, 0)) -> None:
        self._items.append((int(y), str(text), fill))

    def flush(self, draw: Any, font: Any) -> None:
        self._items.sort(key=lambda item: item[0])
        last_y = -(10**6)
        for y, text, fill in self._items:
            yy = min(max(y, last_y + self._min_gap), self._max_y)
            draw.text((self._x, yy - 6), text, fill=fill, font=font)
            last_y = yy


class _ChartCanvas:
    """Shared drawing primitives over a Pillow image canvas."""

    def __init__(
        self,
        width: int,
        height: int,
        margins: tuple[int, int, int, int],
        scale: _AxisScale,
        image_mod: Any,
        draw_mod: Any,
        font_mod: Any,
        *,
        rail_gap: int = 12,
    ) -> None:
        self.width = width
        self.height = height
        self.left_m, self.right_m, self.top_m, self.bot_m = margins
        self.plot_w = width - self.left_m - self.right_m
        self.plot_h = height - self.top_m - self.bot_m
        self.scale = scale
        self.img = image_mod.new("RGB", (width, height), (255, 255, 255))
        self.draw = draw_mod.Draw(self.img)
        self.font_s, cjk_s = _load_font(font_mod, 11)
        self.font_b, cjk_b = _load_font(font_mod, 15, bold=True)
        self.cjk_ok = bool(cjk_s or cjk_b)
        self.rail = _LabelRail(
            self.left_m + self.plot_w + 8,
            height - self.bot_m - 6,
            min_gap=rail_gap,
        )

    def text_w(self, txt: str, font: Any) -> float:
        try:
            return float(self.draw.textlength(txt, font=font))
        except (AttributeError, TypeError, ValueError):
            return float(len(txt) * 6)

    def hline(
        self,
        y: int,
        on: int,
        off: int,
        fill: Color,
        width: int = 1,
        x_start: int | None = None,
    ) -> None:
        x = max(self.left_m, self.left_m if x_start is None else int(x_start))
        x_end = self.left_m + self.plot_w
        while x < x_end:
            self.draw.line([(x, y), (min(x + on, x_end), y)], fill=fill, width=width)
            x += on + off

    def vline(
        self,
        x: int,
        y_top: int,
        y_bot: int,
        on: int,
        off: int,
        fill: Color,
        width: int = 1,
    ) -> None:
        y = min(y_top, y_bot)
        y_end = max(y_top, y_bot)
        while y < y_end:
            self.draw.line([(x, y), (x, min(y + on, y_end))], fill=fill, width=width)
            y += on + off

    def draw_grid(self, dot_color: Color = (206, 206, 206)) -> None:
        ticks = self.scale.tick_values(5)
        step = abs(ticks[1] - ticks[0]) if len(ticks) > 1 else 0.0
        decimals = max(0, min(8, 2 - int(math.floor(math.log10(step))))) if step > 0.0 else 2
        for price_val in ticks:
            yk = self.scale.price_to_y(price_val)
            for x in range(self.left_m, self.left_m + self.plot_w, 12):
                self.draw.point((x, yk), fill=dot_color)
            self.draw.text(
                (4, yk - 6),
                f"{price_val:.{decimals}f}",
                fill=(110, 110, 110),
                font=self.font_s,
            )

    def draw_time_axis(self, bars: Sequence[Sequence[Any]], step_x: float) -> None:
        count = len(bars)
        for k in range(5):
            idx = int(round((count - 1) * k / 4.0))
            ts = int(bars[idx][0])
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
            x = int(self.left_m + (idx + 0.5) * step_x)
            tw = self.text_w(dt, self.font_s)
            x_clamped = min(max(self.left_m, x - tw / 2.0), self.left_m + self.plot_w - tw)
            self.draw.text((x_clamped, self.height - self.bot_m + 12), dt, fill=(90, 90, 90), font=self.font_s)

    def finish_png(self) -> bytes:
        self.rail.flush(self.draw, self.font_s)
        self.draw.rectangle(
            [self.left_m, self.top_m, self.left_m + self.plot_w, self.top_m + self.plot_h],
            outline=(0, 0, 0),
            width=2,
        )
        buf = io.BytesIO()
        self.img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _normalize_band(lo: float | None, hi: float | None) -> tuple[float | None, float | None]:
    lo_n, hi_n = _positive_finite(lo), _positive_finite(hi)
    if lo_n is not None and hi_n is not None and hi_n < lo_n:
        return hi_n, lo_n
    return lo_n, hi_n


def _parse_pivots(pivots: Sequence[Sequence[Any]] | None) -> list[tuple[int | None, float, str]]:
    parsed: list[tuple[int | None, float, str]] = []
    for item in pivots or ():
        try:
            raw_idx, raw_price, raw_kind = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        price = _positive_finite(raw_price)
        if price is None:
            continue
        parsed.append((raw_idx if isinstance(raw_idx, int) else None, price, str(raw_kind).lower()))
    return parsed


def _resolve_box_start_index(
    sub: Sequence[Sequence[Any]],
    *,
    box_lo: float | None,
    box_hi: float | None,
    box_start_ts: int | None,
    box_start_index: int | None,
) -> int:
    count = len(sub)
    if box_start_index is not None:
        try:
            return max(0, min(count - 1, int(box_start_index)))
        except (TypeError, ValueError):
            return 0
    if box_start_ts is not None:
        try:
            target = int(box_start_ts)
        except (TypeError, ValueError):
            return 0
        for idx, bar in enumerate(sub):
            if int(bar[0]) >= target:
                return idx
        return max(0, count - 1)
    if box_lo is not None and box_hi is not None and box_hi > box_lo and count > 12:
        for idx in range(count - 1, -1, -1):
            if float(sub[idx][2]) < box_lo or float(sub[idx][3]) > box_hi:
                return min(count - 1, idx + 1)
    return 0


def _draw_bw_overlays(
    canvas: _ChartCanvas,
    sub: Sequence[Sequence[Any]],
    step_x: float,
    *,
    box_lo: float | None,
    box_hi: float | None,
    box_start_ts: int | None,
    box_start_index: int | None,
    zone_lo: float | None,
    zone_hi: float | None,
    zone_touches: int | None,
    entry_price: float | None,
    stop_price: float | None,
    target_price: float | None,
    d_int: int,
    public_mode: bool,
) -> None:
    b_start_idx = _resolve_box_start_index(
        sub,
        box_lo=box_lo,
        box_hi=box_hi,
        box_start_ts=box_start_ts,
        box_start_index=box_start_index,
    )
    box_x0 = int(round(canvas.left_m + b_start_idx * step_x)) if b_start_idx > 0 else canvas.left_m
    if box_lo is not None and box_hi is not None and box_hi > box_lo:
        y1, y2 = canvas.scale.price_to_y(box_hi), canvas.scale.price_to_y(box_lo)
        if y2 > y1:
            canvas.draw.rectangle([box_x0, y1, canvas.left_m + canvas.plot_w, y2], fill=(240, 240, 240))
            if b_start_idx > 0:
                canvas.vline(box_x0, y1, y2, 5, 3, (90, 90, 90), 2)
        canvas.hline(y1, 8, 4, (0, 0, 0), 2, x_start=box_x0)
        canvas.hline(y2, 8, 4, (0, 0, 0), 2, x_start=box_x0)
        hi_tag = "UPPER" if public_mode else "BAND HI"
        lo_tag = "LOWER" if public_mode else "BAND LO"
        canvas.rail.add(y1, f"{hi_tag} {fmt_px(box_hi)}")
        canvas.rail.add(y2, f"{lo_tag} {fmt_px(box_lo)}")

    if not public_mode and zone_lo is not None and zone_hi is not None and zone_hi >= zone_lo:
        yz1, yz2 = canvas.scale.price_to_y(zone_hi), canvas.scale.price_to_y(zone_lo)
        if yz2 - yz1 >= 2:
            x = box_x0
            x_end = canvas.left_m + canvas.plot_w
            while x < x_end:
                canvas.draw.line([(x, yz2), (min(x + 6, x_end), yz1)], fill=(198, 198, 198), width=1)
                x += 9
        canvas.hline(yz1, 3, 3, (70, 70, 70), x_start=box_x0)
        canvas.hline(yz2, 3, 3, (70, 70, 70), x_start=box_x0)
        touch_txt = ""
        if zone_touches:
            n_touch = int(zone_touches)
            touch_txt = f" · {n_touch}触" if canvas.cjk_ok else f" · {n_touch} touch"
        canvas.rail.add(yz1, f"ZONE HI {fmt_px(zone_hi)}{touch_txt}", (70, 70, 70))
        canvas.rail.add(yz2, f"ZONE LO {fmt_px(zone_lo)}", (70, 70, 70))

    canvas.draw_grid()

    if entry_price is not None and stop_price is not None and d_int:
        r_unit = abs(entry_price - stop_price)
        if r_unit > 0.0:
            for k in (1, 2, 3):
                lvl = entry_price + d_int * k * r_unit
                if not (canvas.scale.y_lo <= lvl <= canvas.scale.y_hi):
                    continue
                yk = canvas.scale.price_to_y(lvl)
                canvas.hline(yk, 2, 5, (160, 160, 160))
                canvas.rail.add(yk, f"+{k}R {fmt_px(lvl)}", (140, 140, 140))

    for px, label, dash, width, color in (
        (entry_price, "ENTRY", 7, 2, (0, 0, 0)),
        (stop_price, "STOP", 3, 2, (0, 0, 0)),
        (target_price, "TARGET", 5, 2, (40, 40, 40)),
    ):
        if px is None or px <= 0.0:
            continue
        yy = canvas.scale.price_to_y(px)
        canvas.hline(yy, dash, max(2, dash - 1), color, width)
        canvas.rail.add(yy, f"{label} {fmt_px(px)}", color)


def _draw_bw_candles_and_markers(
    canvas: _ChartCanvas,
    sub: Sequence[Sequence[Any]],
    step_x: float,
    *,
    near_piv: Sequence[tuple[int | None, float, str]],
    mark_index: int | None,
    direction: int | None,
    public_mode: bool,
) -> None:
    count = len(sub)
    body_w = max(3, int(step_x * 0.86))
    for idx, bar in enumerate(sub):
        o, h, lo, c = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
        cx = int(round(canvas.left_m + (idx + 0.5) * step_x))
        yh, yl = canvas.scale.price_to_y(h), canvas.scale.price_to_y(lo)
        yo, yc = canvas.scale.price_to_y(o), canvas.scale.price_to_y(c)
        canvas.draw.line([(cx, yh), (cx, yl)], fill=(0, 0, 0), width=1)
        top_b, bot_b = min(yo, yc), max(yo, yc)
        if bot_b - top_b < 2:
            bot_b = top_b + 2
        x0, x1 = cx - body_w // 2, cx + body_w // 2
        fill = (232, 232, 232) if c >= o else (0, 0, 0)
        canvas.draw.rectangle([x0, top_b, x1, bot_b], fill=fill, outline=(0, 0, 0), width=1)

    if not public_mode:
        labeled = 0
        for p_idx, p_price, p_kind in reversed(near_piv):
            if p_idx is None or not (0 <= p_idx < count):
                continue
            pcx = int(round(canvas.left_m + (p_idx + 0.5) * step_x))
            py = canvas.scale.price_to_y(p_price)
            is_high = p_kind.startswith("h")
            if is_high:
                tri = [
                    (pcx, max(canvas.top_m + 12, py - 4)),
                    (pcx - 5, max(canvas.top_m + 4, py - 12)),
                    (pcx + 5, max(canvas.top_m + 4, py - 12)),
                ]
                ly = max(canvas.top_m + 2, py - 26)
            else:
                tri = [
                    (pcx, min(canvas.top_m + canvas.plot_h - 12, py + 4)),
                    (pcx - 5, min(canvas.top_m + canvas.plot_h - 4, py + 12)),
                    (pcx + 5, min(canvas.top_m + canvas.plot_h - 4, py + 12)),
                ]
                ly = min(canvas.top_m + canvas.plot_h - 18, py + 14)
            canvas.draw.polygon(tri, fill=(60, 60, 60))
            if labeled < 2:
                label = f"P{'H' if is_high else 'L'} {fmt_px(p_price)}"
                w_txt = canvas.text_w(label, canvas.font_s)
                right_lim = canvas.left_m + canvas.plot_w - w_txt - 2
                lx = pcx + 7
                if lx > right_lim:
                    lx = max(canvas.left_m + 2, pcx - w_txt - 7)
                canvas.draw.rectangle([lx - 1, ly - 1, lx + w_txt + 1, ly + 13], fill=(255, 255, 255))
                canvas.draw.text((lx, ly), label, fill=(60, 60, 60), font=canvas.font_s)
                labeled += 1

    m_idx = (count - 1) if mark_index is None else max(0, min(count - 1, mark_index))
    mcx = int(round(canvas.left_m + (m_idx + 0.5) * step_x))
    mb = sub[m_idx]
    if direction is not None and direction < 0:
        my = max(canvas.top_m + 6, canvas.scale.price_to_y(float(mb[2])) - 14)
        canvas.draw.polygon([(mcx, my + 8), (mcx - 5, my), (mcx + 5, my)], fill=(0, 0, 0))
    else:
        my = min(canvas.top_m + canvas.plot_h - 6, canvas.scale.price_to_y(float(mb[3])) + 14)
        canvas.draw.polygon([(mcx, my - 8), (mcx - 5, my), (mcx + 5, my)], fill=(0, 0, 0))


def _draw_bw_header(
    canvas: _ChartCanvas,
    sub: Sequence[Sequence[Any]],
    *,
    symbol: str,
    tf_label: str,
    title_tag: str,
    step_min: int,
    gaps: int,
    level_note: str,
    d_int: int,
    entry_price: float | None,
    stop_price: float | None,
    target_price: float | None,
    box_lo: float | None,
    box_hi: float | None,
    zone_touches: int | None,
    atr: float | None,
    public_mode: bool,
) -> None:
    ts_first = datetime.fromtimestamp(int(sub[0][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
    ts_last = datetime.fromtimestamp(int(sub[-1][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
    scale_tag = "LOG" if canvas.scale.use_log else "LIN"
    head = f"{symbol} [{tf_label}] {title_tag} [{scale_tag}]".strip()
    canvas.draw.text((canvas.left_m, 6), head, fill=(0, 0, 0), font=canvas.font_b)
    canvas.draw.text((canvas.left_m, 36), f"{ts_first} -> {ts_last}", fill=(90, 90, 90), font=canvas.font_s)
    step_txt = f"{step_min}m" if step_min else "?"
    foot = f"bars {len(sub)} · step {step_txt} · gaps {gaps}"
    if level_note and not public_mode:
        foot = f"{foot} · {level_note}"
    canvas.draw.text((canvas.left_m, 48), foot, fill=(120, 120, 120), font=canvas.font_s)

    bits: list[str] = []
    rr = rr_multiple(d_int, entry_price, stop_price, target_price)
    if rr is not None:
        bits.append(f"R:R {rr:.2f}")
    if not public_mode:
        pos_txt = box_position_text(entry_price, box_lo, box_hi)
        if atr is not None and entry_price:
            bits.append(f"ATR {atr / entry_price:.2%}")
        if box_lo is not None and box_hi is not None and entry_price:
            bits.append(
                f"水平带跨度 {(box_hi - box_lo) / entry_price:.2%}"
                if canvas.cjk_ok
                else f"BAND span {(box_hi - box_lo) / entry_price:.2%}"
            )
        if zone_touches:
            bits.append(
                f"触边 {int(zone_touches)} 次" if canvas.cjk_ok else f"touch {int(zone_touches)}x"
            )
        if pos_txt:
            pct = box_position_pct(entry_price, box_lo, box_hi)
            bits.append(
                f"入场带内 {pos_txt}" if canvas.cjk_ok else f"entry in band {pct:.0%}"
            )
    if bits:
        summary = " | ".join(bits)
        x_sum = max(canvas.left_m, canvas.left_m + canvas.plot_w - canvas.text_w(summary, canvas.font_s))
        canvas.draw.text((x_sum, 36), summary, fill=(50, 50, 50), font=canvas.font_s)


def render_bw_kline_png(
    bars: Sequence[Sequence[Any]],
    *,
    symbol: str = "",
    tf_label: str = "",
    title_tag: str = "",
    box_lo: float | None = None,
    box_hi: float | None = None,
    entry_price: float | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    mark_index: int | None = None,
    direction: int | None = None,
    zone_lo: float | None = None,
    zone_hi: float | None = None,
    zone_touches: int | None = None,
    pivots: Sequence[Sequence[Any]] | None = None,
    atr: float | None = None,
    max_bars: int = 96,
    log_scale: bool = True,
    box_start_ts: int | None = None,
    box_start_index: int | None = None,
    public_mode: bool = False,
    level_note: str = "",
) -> bytes | None:
    """Render high-contrast monochrome K-line chart with structure overlays as PNG."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    if not bars:
        return None
    sub = list(bars[-max_bars:])
    count, step_min, gaps = bar_gap_stats(sub)
    if count == 0:
        return None

    box_lo, box_hi = _normalize_band(box_lo, box_hi)
    zone_lo, zone_hi = _normalize_band(zone_lo, zone_hi)
    entry_price = _positive_finite(entry_price)
    stop_price = _positive_finite(stop_price)
    target_price = _positive_finite(target_price)
    atr = _positive_finite(atr)
    piv = _parse_pivots(pivots)

    p_max = max(float(b[2]) for b in sub)
    p_min = min(float(b[3]) for b in sub)
    visible_levels: tuple[float | None, ...] = (
        (box_lo, box_hi, entry_price, stop_price, target_price)
        if public_mode
        else (box_lo, box_hi, entry_price, stop_price, target_price, zone_lo, zone_hi)
    )
    for lvl in visible_levels:
        if lvl is not None:
            p_max = max(p_max, lvl)
            p_min = min(p_min, lvl)

    bar_span = max(p_max - p_min, p_max * 0.002, 1e-9)
    near_piv = [
        p for p in piv if p_min - bar_span * 0.3 <= p[1] <= p_max + bar_span * 0.3
    ]
    if not public_mode:
        for _idx, price, _kind in near_piv:
            p_max = max(p_max, price)
            p_min = min(p_min, price)

    _, _, top_m, bot_m = _BW_MARGINS
    scale = _AxisScale.from_bounds(
        p_min, p_max, log_scale=log_scale, top_m=top_m, plot_h=_BW_HEIGHT - top_m - bot_m
    )
    canvas = _ChartCanvas(_BW_WIDTH, _BW_HEIGHT, _BW_MARGINS, scale, Image, ImageDraw, ImageFont)
    step_x = canvas.plot_w / max(count, 1)

    d_int = 0
    if direction is not None:
        try:
            d_int = 1 if float(direction) > 0 else (-1 if float(direction) < 0 else 0)
        except (TypeError, ValueError):
            d_int = 0

    _draw_bw_overlays(
        canvas,
        sub,
        step_x,
        box_lo=box_lo,
        box_hi=box_hi,
        box_start_ts=box_start_ts,
        box_start_index=box_start_index,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        zone_touches=zone_touches,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        d_int=d_int,
        public_mode=public_mode,
    )
    _draw_bw_candles_and_markers(
        canvas,
        sub,
        step_x,
        near_piv=near_piv,
        mark_index=mark_index,
        direction=direction,
        public_mode=public_mode,
    )
    _draw_bw_header(
        canvas,
        sub,
        symbol=symbol,
        tf_label=tf_label,
        title_tag=title_tag,
        step_min=step_min,
        gaps=gaps,
        level_note=level_note,
        d_int=d_int,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        box_lo=box_lo,
        box_hi=box_hi,
        zone_touches=zone_touches,
        atr=atr,
        public_mode=public_mode,
    )
    canvas.draw_time_axis(sub, step_x)
    return canvas.finish_png()


def _level_kind_tag(lv: Any) -> str:
    if getattr(lv, "flipped", False):
        return "FLIP"
    raw = str(getattr(getattr(lv, "kind", None), "value", getattr(lv, "kind", ""))).upper()
    if raw == "ORIGIN":
        return "ORIG"
    if raw == "EQUAL":
        return "EQL"
    return "SWG"


def render_level_map_png(
    symbol: str,
    tf_label: str,
    bars: Sequence[Sequence[Any]],
    levels: Sequence[Any],
    *,
    regime_text: str = "",
    topo_text: str = "",
    atr: float = 0.0,
    max_bars: int = 96,
    log_scale: bool = True,
) -> bytes | None:
    """Render a multi-level support/resistance panorama map for ``/levels``."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    if not bars or len(bars) < 5:
        return None

    sub = list(bars[-max_bars:])
    count, last_px = len(sub), float(sub[-1][4])
    p_max = max(float(b[2]) for b in sub)
    p_min = min(float(b[3]) for b in sub)
    bar_span = max(p_max - p_min, last_px * 0.005, 1e-9)

    vis_levels: list[Any] = []
    for lv in levels:
        lo_b = float(getattr(lv, "band_lo", getattr(lv, "lo", 0.0)))
        hi_b = float(getattr(lv, "band_hi", getattr(lv, "hi", 0.0)))
        if hi_b <= 0.0:
            continue
        vis_levels.append(lv)
        if p_min - bar_span * 0.45 <= lo_b and hi_b <= p_max + bar_span * 0.45:
            p_max, p_min = max(p_max, hi_b), min(p_min, lo_b)

    _, _, top_m, bot_m = _MAP_MARGINS
    scale = _AxisScale.from_bounds(
        p_min, p_max, log_scale=log_scale, top_m=top_m, plot_h=_MAP_HEIGHT - top_m - bot_m
    )
    canvas = _ChartCanvas(
        _MAP_WIDTH, _MAP_HEIGHT, _MAP_MARGINS, scale, Image, ImageDraw, ImageFont, rail_gap=13
    )
    step_x = canvas.plot_w / float(max(count, 1))
    body_half = max(1, int(round(step_x * 0.36)))

    res_i = sup_i = 0
    for lv in vis_levels:
        lo_b = float(getattr(lv, "band_lo", getattr(lv, "lo", 0.0)))
        hi_b = float(getattr(lv, "band_hi", getattr(lv, "hi", 0.0)))
        if hi_b < scale.y_lo or lo_b > scale.y_hi:
            continue
        role_val = str(getattr(getattr(lv, "role", None), "value", getattr(lv, "role", ""))).lower()
        is_res = "res" in role_val
        if is_res:
            res_i += 1
            tag_p, fill_rgb, line_rgb = f"R{res_i}", (252, 235, 235), (185, 40, 40)
        else:
            sup_i += 1
            tag_p, fill_rgb, line_rgb = f"S{sup_i}", (232, 247, 236), (30, 130, 60)
        orig_ts = int(getattr(lv, "origin_ts", 0) or 0)
        x0_idx = next((idx for idx, b in enumerate(sub) if orig_ts > 0 and int(b[0]) >= orig_ts), 0)
        bx0 = int(round(canvas.left_m + x0_idx * step_x))
        y1 = scale.price_to_y(min(hi_b, scale.y_hi))
        y2 = scale.price_to_y(max(lo_b, scale.y_lo))
        if y2 < y1:
            y1, y2 = y2, y1
        y2 = max(y1 + 2, y2)
        canvas.draw.rectangle([bx0, y1, canvas.left_m + canvas.plot_w, y2], fill=fill_rgb)
        canvas.hline(y1, 6, 3, line_rgb, 1, x_start=bx0)
        canvas.hline(y2, 6, 3, line_rgb, 1, x_start=bx0)
        ref_e = lo_b if is_res else hi_b
        dp = (ref_e - last_px) / last_px * 100.0 if last_px > 0.0 else 0.0
        canvas.rail.add(
            (y1 + y2) // 2,
            f"{tag_p}[{_level_kind_tag(lv)}] {fmt_px(ref_e)} ({dp:+.1f}%)",
            line_rgb,
        )

    canvas.draw_grid(dot_color=(210, 210, 210))

    for idx, bar in enumerate(sub):
        o, h, lo, c = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
        cx = int(round(canvas.left_m + (idx + 0.5) * step_x))
        canvas.draw.line([(cx, scale.price_to_y(h)), (cx, scale.price_to_y(lo))], fill=(30, 30, 30), width=1)
        yo, yc = scale.price_to_y(o), scale.price_to_y(c)
        tb, bb = min(yo, yc), max(min(yo, yc) + 1, max(yo, yc))
        fill = (255, 255, 255) if c >= o else (35, 35, 35)
        canvas.draw.rectangle([cx - body_half, tb, cx + body_half, bb], fill=fill, outline=(20, 20, 20))

    y_last = scale.price_to_y(last_px)
    canvas.hline(y_last, 3, 3, (20, 90, 200), 1)
    canvas.rail.add(y_last, f"LAST {fmt_px(last_px)}", (20, 90, 200))

    t0 = datetime.fromtimestamp(int(sub[0][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M")
    t1 = datetime.fromtimestamp(int(sub[-1][0]) / 1000.0, tz=timezone.utc).strftime("%m-%d %H:%M UTC")
    canvas.draw.text(
        (canvas.left_m, 8),
        f"{symbol} [{tf_label}] LEVEL MAP · LAST {fmt_px(last_px)}",
        fill=(0, 0, 0),
        font=canvas.font_b,
    )
    atr_txt = f"ATR {atr / last_px:.2%}" if (atr > 0.0 and last_px > 0.0) else ""
    subtitle = "  |  ".join(x for x in (f"{t0} -> {t1}", regime_text, topo_text, atr_txt) if x)
    canvas.draw.text((canvas.left_m, 36), subtitle, fill=(70, 70, 70), font=canvas.font_s)
    canvas.draw_time_axis(sub, step_x)
    return canvas.finish_png()


__all__ = [
    "bar_gap_stats",
    "box_position_pct",
    "box_position_text",
    "fmt_px",
    "render_bw_kline_png",
    "render_level_map_png",
    "rr_multiple",
]
