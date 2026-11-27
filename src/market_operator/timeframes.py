"""Single source of truth for timeframe constants, conversions, and UTC helpers.

Zero external dependencies (stdlib only). Shared across data ingestion,
aggregation, replay, live runner, ops, Telegram commands, and scripts to
eliminate duplicated millisecond/second/granularity lookup tables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

SECONDS_PER_MINUTE: int = 60
SECONDS_PER_HOUR: int = 3_600
SECONDS_PER_DAY: int = 86_400

MS_PER_SECOND: int = 1_000
MS_PER_MINUTE: int = 60_000
MS_15M: int = 15 * MS_PER_MINUTE
MS_30M: int = 30 * MS_PER_MINUTE
MS_PER_HOUR: int = 3_600_000
MS_4H: int = 4 * MS_PER_HOUR
MS_8H: int = 8 * MS_PER_HOUR
MS_12H: int = 12 * MS_PER_HOUR
MS_PER_DAY: int = 24 * MS_PER_HOUR

# Canonical duration maps (read-only)
TF_SECONDS: Mapping[str, int] = MappingProxyType({
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "2h": 7_200,
    "4h": 14_400,
    "6h": 21_600,
    "12h": 43_200,
    "1d": 86_400,
    "1w": 604_800,
})

TF_HOURS: Mapping[str, float] = MappingProxyType({
    "15m": 0.25,
    "30m": 0.5,
    "1h": 1.0,
    "2h": 2.0,
    "4h": 4.0,
    "6h": 6.0,
    "12h": 12.0,
    "1d": 24.0,
})

# Bitget REST v2 granularity strings
BITGET_GRANULARITY: Mapping[str, str] = MappingProxyType({
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "12h": "12H",
    "1d": "1Dutc",
    "1w": "1Wutc",
})

def normalize_tf_label(tf: str) -> str:
    """Normalise a timeframe string such as ``'1H'`` or ``'1Dutc'`` to canonical lowercase."""
    raw = str(tf).strip().lower()
    if raw.endswith("utc"):
        raw = raw[:-3]
    return raw


def tf_to_seconds(tf: str, *, default: int | None = None) -> int:
    """Convert a timeframe string (e.g. ``'15m'``, ``'4h'``, ``'1Dutc'``) into seconds."""
    key = normalize_tf_label(tf)
    if key in TF_SECONDS:
        return TF_SECONDS[key]
    if key.endswith("m") and key[:-1].isdigit():
        return int(key[:-1]) * SECONDS_PER_MINUTE
    if key.endswith("h") and key[:-1].isdigit():
        return int(key[:-1]) * SECONDS_PER_HOUR
    if key.endswith("d") and key[:-1].isdigit():
        return int(key[:-1]) * SECONDS_PER_DAY
    if default is not None:
        return int(default)
    raise ValueError(f"Unsupported timeframe label: {tf!r}")


def tf_to_ms(tf: str, *, default: int | None = None) -> int:
    """Convert a timeframe string into milliseconds."""
    if default is not None:
        return tf_to_seconds(tf, default=default // MS_PER_SECOND) * MS_PER_SECOND
    return tf_to_seconds(tf) * MS_PER_SECOND


def tf_to_hours(tf: str) -> float:
    """Convert a timeframe string into fractional hours."""
    return tf_to_seconds(tf) / float(SECONDS_PER_HOUR)


def seconds_to_label(sec: int) -> str:
    """Format a bar duration in seconds into a compact timeframe label (e.g. ``'4h'``, ``'1d'``)."""
    val = int(sec)
    if val >= SECONDS_PER_DAY and val % SECONDS_PER_DAY == 0:
        return f"{val // SECONDS_PER_DAY}d"
    if val >= SECONDS_PER_HOUR and val % SECONDS_PER_HOUR == 0:
        return f"{val // SECONDS_PER_HOUR}h"
    if val >= SECONDS_PER_MINUTE and val % SECONDS_PER_MINUTE == 0:
        return f"{val // SECONDS_PER_MINUTE}m"
    return f"{val}s"


def bitget_granularity(tf: str) -> str:
    """Map a canonical timeframe string to Bitget REST v2 granularity parameter."""
    key = normalize_tf_label(tf)
    if key in BITGET_GRANULARITY:
        return BITGET_GRANULARITY[key]
    raise ValueError(f"Unsupported Bitget granularity: {tf!r}")


def cascade_label(
    tf: str,
    trigger_tf: str | int | None = None,
) -> str:
    """Build a display label for cognition→trigger cascades (e.g. ``'4h->1h'``)."""
    if trigger_tf is None or trigger_tf == "":
        return str(tf)
    try:
        tf_sec = tf_to_seconds(tf)
    except ValueError:
        return str(tf)
    if isinstance(trigger_tf, bool):
        return str(tf)
    if isinstance(trigger_tf, int):
        trig_sec = trigger_tf
        trig_label = seconds_to_label(trig_sec)
    else:
        raw = str(trigger_tf).strip()
        if raw.isdigit():
            trig_sec = int(raw)
            trig_label = seconds_to_label(trig_sec)
        else:
            try:
                trig_sec = tf_to_seconds(raw)
            except ValueError:
                return str(tf)
            trig_label = raw
    if 0 < trig_sec < tf_sec:
        return f"{tf}->{trig_label}"
    return str(tf)


def utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_ms_from_ts(ts: str | int | float | None) -> int:
    """Parse an ISO-8601 string or numeric timestamp into UTC milliseconds (0 if invalid)."""
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        val = int(ts)
        return val if val > 0 else 0
    text = str(ts).strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * MS_PER_SECOND)
    except ValueError:
        return 0


__all__ = [
    "SECONDS_PER_MINUTE",
    "SECONDS_PER_HOUR",
    "SECONDS_PER_DAY",
    "MS_PER_SECOND",
    "MS_PER_MINUTE",
    "MS_15M",
    "MS_30M",
    "MS_PER_HOUR",
    "MS_4H",
    "MS_8H",
    "MS_12H",
    "MS_PER_DAY",
    "TF_SECONDS",
    "TF_HOURS",
    "BITGET_GRANULARITY",
    "normalize_tf_label",
    "tf_to_seconds",
    "tf_to_ms",
    "tf_to_hours",
    "seconds_to_label",
    "bitget_granularity",
    "cascade_label",
    "utc_now_iso",
    "utc_ms_from_ts",
]
