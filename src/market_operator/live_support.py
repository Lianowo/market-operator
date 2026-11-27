"""Shared pure primitives for live execution, warmup, and supervision.

Placed in the flat ``market_operator`` package so ``ops`` and all ``scripts/live_*``
utilities share one canonical implementation without importing upward from
``scripts/live_runner.py``.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from .broker import ContractSpec
from .engine import (
    build_position_manager,
    load_strategy_bundle,
    parse_execution_config,
)
from .events import EventDetector, canonical_event_id
from .exceptions import DataIntegrityError
from .heartbeat import SCHEMA as HB_SCHEMA, iso_utc, write_atomic
from .paths import app_home
from .position import PositionManager
from .structure import Params
from .timeframes import (
    MS_PER_HOUR,
    TF_SECONDS,
    cascade_label,
    utc_ms_from_ts,
)
from .trigger import MicTracker

STRATEGY_CONFIG = app_home() / "config" / "strategy.json"
MS_1H = MS_PER_HOUR
N_FILL_H = 12
TF_SECONDS_MAP = dict(TF_SECONDS)
#: Longest 15m catch-up window the price clock will replay.
PRICE_CLOCK_MAX_CATCHUP_STEPS = 4
#: Relative tolerance for attributing a fill price to a protective stop order.
EXIT_STOP_TOL_FRAC = 2e-3

_NO_POS_MARKS: tuple[str, ...] = (
    "43023",
    "仓位不足",
    "position is empty",
    "no position",
)
_STOP_TOO_SMALL_MARKS: tuple[str, ...] = (
    "43023",
    "仓位不足",
    "position is not enough",
    "insufficient position",
)


def initial_price_clock_ts(now_ms: int, step: int, opened_ts: int) -> int:
    """Startup anchor for the 15m price clock: the last closed bar, not entry."""
    floor = (int(now_ms) // step) * step
    return max(floor - step, (int(opened_ts) // step) * step)


def load_strategy_params(
    cfg_path: Path = STRATEGY_CONFIG,
) -> tuple[dict[str, Any], Params, Any, dict[str, Any]]:
    """Shared strategy config loader for replay and live (delegates to engine)."""
    bundle = load_strategy_bundle(cfg_path)
    return dict(bundle.raw), bundle.params, bundle.router, dict(bundle.router_by_tf)


def new_position_manager(params: Params, raw: Mapping[str, Any]) -> PositionManager:
    """Shared position manager factory for replay and live (delegates to engine)."""
    arm = parse_execution_config(raw)
    return build_position_manager(params, arm)


def utc_ms(iso: str | None) -> int | None:
    """ISO8601 (UTC) -> epoch ms; returns None when unparseable."""
    return utc_ms_from_ts(iso)


def fill_ts_ms(row: Mapping[str, Any]) -> int | None:
    """Extract comparable fill timestamp (epoch ms) across Bitget v2 field variants."""
    for key in ("cTime", "uTime", "updatedTime"):
        val = row.get(key)
        if val in (None, ""):
            continue
        try:
            return int(float(val))
        except (TypeError, ValueError):
            continue
    return None


def realized_pnl(
    direction: int,
    qty: float,
    entry: float,
    exit_px: float,
    taker_fee: float,
) -> float:
    """Realized PnL under canonical convention (gross minus exit taker fee)."""
    side = 1 if int(direction) > 0 else -1
    qty_f, entry_f, exit_f = float(qty), float(entry), float(exit_px)
    gross = qty_f * side * (exit_f - entry_f)
    fee = qty_f * exit_f * float(taker_fee)
    return gross - fee


def stop_execution_evidence(
    direction: int,
    exit_px: float,
    stop_px: float,
    *,
    tol_frac: float = EXIT_STOP_TOL_FRAC,
) -> bool:
    """Return True if an exit fill price can be attributed to the protective stop."""
    side = 1 if int(direction) > 0 else -1
    px, stop = float(exit_px), float(stop_px)
    tol = abs(px) * float(tol_frac) + 1e-12
    return side * (px - stop) <= tol


def exit_label(*, executor: str, pnl: float | None, reason: str = "") -> str:
    """Map orthogonal exit facts (executor x PnL) into a verifiable Chinese label."""
    gain = None if pnl is None else float(pnl) > 0.0
    if executor == "local_market":
        if "lock" in str(reason):
            return (
                "止盈(回撤预算耗尽·市价离场)"
                if gain
                else "离场(回撤预算耗尽·市价)"
            )
        return "止盈(无保护单·市价离场)" if gain else "止损(无保护单·市价离场)"
    if executor == "protective_stop":
        return "止盈(移动止损触发)" if gain else "止损(保护单触发)"
    if executor == "external":
        return "离场确认(外部平仓·盈利)" if gain else "离场确认(外部平仓·亏损)"
    return "离场确认(交易所已无此仓)"


def mark_from_reject(msg: str) -> float | None:
    """Extract exchange-reported mark price from a 45122 stop-rejection message."""
    text = str(msg)
    for key in ("标记价格", "mark price", "markPrice"):
        idx = text.rfind(key)
        if idx < 0:
            continue
        start = idx + len(key)
        while start < len(text) and not (text[start].isdigit() or text[start] == "."):
            start += 1
        end = start
        while end < len(text) and (text[end].isdigit() or text[end] == "."):
            end += 1
        try:
            value = float(text[start:end])
        except ValueError:
            continue
        if value > 0.0:
            return value
    return None


def stop_reject_no_position(msg: str) -> bool:
    """Return True when a stop-order rejection indicates the position no longer exists."""
    text = str(msg)
    return any(mark in text for mark in _NO_POS_MARKS)


def stop_requires_local(msg: str) -> bool:
    """Return True when a stop-order rejection requires local stop monitoring."""
    text = str(msg)
    return any(mark in text for mark in _STOP_TOO_SMALL_MARKS)


def utc_day_gap(prev: str | None, cur: str) -> int | None:
    """Number of calendar days between two YYYY-MM-DD UTC dates."""
    if prev is None:
        return None
    try:
        return (date.fromisoformat(str(cur)) - date.fromisoformat(str(prev))).days
    except (TypeError, ValueError):
        return None


class DailyLossBreaker:
    """UTC-day drawdown circuit breaker with overnight anchor preservation."""

    def __init__(self, max_frac: float) -> None:
        self.max_frac = float(max_frac)
        self.day: str | None = None
        self.start_equity: float | None = None
        self.tripped = False
        self.last_equity: float | None = None
        self.prev_day: str | None = None
        self.prev_start_equity: float | None = None
        self.prev_close_equity: float | None = None

    def update(self, now_ms: int, equity: float) -> None:
        day = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        eq = float(equity)
        if day != self.day:
            anchor = eq
            if (
                self.start_equity is not None
                and self.last_equity is not None
                and utc_day_gap(self.day, day) == 1
            ):
                anchor = float(self.last_equity)
            self.prev_day, self.prev_start_equity = self.day, self.start_equity
            self.prev_close_equity = self.last_equity
            self.day, self.start_equity, self.tripped = day, anchor, False
        self.last_equity = eq

    def should_stop(self, equity: float) -> bool:
        if self.start_equity in (None, 0.0):
            return False
        if (float(equity) - self.start_equity) / self.start_equity <= -self.max_frac:
            self.tripped = True
        return self.tripped


class RateLimiter:
    """Rolling 3600s order rate cap (fail-closed)."""

    def __init__(
        self,
        max_per_hour: int,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max = int(max_per_hour)
        self.stamps: list[float] = []
        self._clock = clock or time.time

    def allow(self, now_s: float | None = None) -> bool:
        now = self._clock() if now_s is None else float(now_s)
        self.stamps = [t for t in self.stamps if now - t < 3600.0]
        if len(self.stamps) >= self.max:
            return False
        self.stamps.append(now)
        return True

    def pending(self, now_s: float | None = None) -> int:
        now = self._clock() if now_s is None else float(now_s)
        return sum(1 for t in self.stamps if now - t < 3600.0)


def edge_size_multiplier(
    table: Mapping[str, Any] | None,
    event_id: object,
    direction: int,
) -> float:
    """Event x direction size weight bounded strictly to [0.1, 1.0]."""
    if not table:
        return 1.0
    probes = (
        f"{canonical_event_id(event_id)}|{int(direction)}",
        f"{event_id}|{int(direction)}",
    )
    raw = next((table[k] for k in probes if k in table), None)
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    if val != val or val <= 0.0:
        return 1.0
    return min(max(val, 0.1), 1.0)


def client_oid_of(
    prefix: str,
    symbol: str,
    event_id: str,
    side: str,
    decision_ts: int,
    role: str = "open",
) -> str:
    """Deterministic client order ID (<= 36 chars) for idempotent retries/restarts."""
    digest = hashlib.sha1(
        f"{symbol}|{event_id}|{side}|{int(decision_ts)}|{role}".encode()
    ).hexdigest()[:12]
    return f"{prefix}{digest}"


def worst_case_of_position(
    direction: int,
    qty: float,
    mark: float,
    invalid: float,
    taker_fee: float,
) -> float:
    """Canonical worst-case loss for a position (stop loss + exit taker fee)."""
    stop = qty * max(0.0, direction * (mark - invalid)) if mark > 0 else 0.0
    return stop + qty * mark * taker_fee


def aggregate_bucket(bars: Sequence[Sequence[float]], label: int) -> tuple[int, float, float, float, float, float]:
    """Aggregate a single grid bucket of OHLCV bars into one bar."""
    return (
        int(label),
        float(bars[0][1]),
        float(max(b[2] for b in bars)),
        float(min(b[3] for b in bars)),
        float(bars[-1][4]),
        float(sum(b[5] for b in bars)),
    )


def aggregate_tf(
    bars2h: Sequence[Sequence[float]],
    step_seconds: int = 43200,
) -> list[tuple[int, float, float, float, float, float]]:
    """Aggregate fine OHLCV bars into an integer-multiple timeframe grid.

    Drops an incomplete trailing bucket, preserves an incomplete leading bucket,
    and raises ``DataIntegrityError`` (subclass of ``RuntimeError``) on interior gaps.
    """
    base_ms = (
        int(bars2h[1][0] - bars2h[0][0])
        if len(bars2h) >= 2 and int(bars2h[1][0]) > int(bars2h[0][0])
        else 7_200_000
    )
    step_ms = int(step_seconds) * 1000
    if step_ms <= base_ms:
        return [
            (int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5]))
            for b in bars2h
        ]
    buckets: dict[int, list[Sequence[float]]] = {}
    for bar in bars2h:
        ts = int(bar[0])
        buckets.setdefault(ts - (ts % step_ms), []).append(bar)
    labels = sorted(buckets)
    expected = step_ms // base_ms
    out: list[tuple[int, float, float, float, float, float]] = []
    for label in labels:
        got = buckets[label]
        if len(got) < expected:
            if label == labels[-1]:
                continue
            if label == labels[0]:
                out.append(aggregate_bucket(got, label))
                continue
            raise DataIntegrityError(f"{step_seconds}s 桶 {label} 内部缺口({len(got)} 根)")
        out.append(aggregate_bucket(got, label))
    return out


def aggregate_12h(
    bars2h: Sequence[Sequence[float]],
) -> list[tuple[int, float, float, float, float, float]]:
    """Aggregate 2h bars into 12h bars (legacy convenience alias)."""
    return aggregate_tf(bars2h, 43200)


def replay_cognition(
    params: Params,
    symbol: str,
    cog_bars: Sequence[Sequence[float]],
    trig_bars: Sequence[Sequence[float]],
    *,
    obs_tf_seconds: int,
    trigger_tf_seconds: int,
) -> tuple[EventDetector, MicTracker | None]:
    """Replay warmup bars in strict causal order and return ``(detector, tracker)``."""
    _ = symbol
    detector = EventDetector(params)
    actual_trig_s = (
        int(trig_bars[1][0] - trig_bars[0][0]) // 1000
        if len(trig_bars) >= 2 and trig_bars[1][0] > trig_bars[0][0]
        else trigger_tf_seconds
    )
    eff_trig_s = max(int(trigger_tf_seconds), actual_trig_s)
    if eff_trig_s >= obs_tf_seconds:
        for bar in cog_bars:
            detector.update(bar)
        return detector, None
    obs_ms = obs_tf_seconds * 1000
    trig_ms = eff_trig_s * 1000
    t_series = aggregate_tf(trig_bars, eff_trig_s) if eff_trig_s > actual_trig_s else trig_bars
    scale = max(1, obs_ms // trig_ms)
    tracker = MicTracker(params, scale=scale)
    first_open = int(cog_bars[0][0]) if cog_bars else 0
    c_idx = 0
    n_cog = len(cog_bars)
    for tb in t_series:
        ts_t = int(tb[0])
        if cog_bars and ts_t < first_open:
            continue
        close_moment = ts_t + trig_ms
        while c_idx < n_cog and int(cog_bars[c_idx][0]) + obs_ms <= close_moment:
            detector.update(cog_bars[c_idx])
            tracker.sync_from_structure(detector.structure)
            c_idx += 1
        tracker.update(tb)
    while c_idx < n_cog:
        detector.update(cog_bars[c_idx])
        tracker.sync_from_structure(detector.structure)
        c_idx += 1
    return detector, tracker


@dataclass
class CampaignState:
    """Live per-(symbol, timeframe) cognition and position-management state."""

    symbol: str
    detector: EventDetector
    tracker: MicTracker | None
    router: Any
    pm: PositionManager
    cog_bars: list[Any] = field(default_factory=list)
    trig_bars: list[Any] = field(default_factory=list)
    tf_label: str = "12h"
    obs_tf_seconds: int = 43200
    trigger_tf_seconds: int = 7200
    active: bool = True


def trigger_origin_ms(cs: CampaignState) -> int:
    """Return the trigger-index zero timestamp matching cropped cognition warmup."""
    first_cog = int(cs.cog_bars[0][0])
    start = bisect_left(cs.trig_bars, first_cog, key=lambda b: int(b[0]))
    first = int(cs.trig_bars[start][0])
    step = int(cs.trigger_tf_seconds) * 1000
    return first - first % step


def reconcile_positions(
    ours: Mapping[str, Mapping[str, Any]],
    exchange: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Compare local ledger positions against exchange positions (1e-6 rel tol)."""
    out: list[str] = []
    ex_by = {(p["symbol"], p["direction"]): dict(p) for p in exchange}
    for pos in ours.values():
        tp = ex_by.pop((pos["symbol"], pos["direction"]), None)
        if tp is None:
            out.append(f"ours_only {pos['symbol']} dir={pos['direction']} qty={pos['qty']}")
        elif abs(tp["qty"] - pos["qty"]) > max(1e-9, pos["qty"] * 1e-6):
            out.append(f"qty_mismatch {pos['symbol']} ours={pos['qty']} exchange={tp['qty']}")
    for (sym, direction), pos in ex_by.items():
        out.append(f"exchange_only {sym} dir={direction} qty={pos['qty']}")
    return out


def load_contracts_live(
    client: Any,
    symbols: Sequence[str] | None = None,
) -> tuple[dict[str, ContractSpec], dict[str, dict[str, Any]]]:
    """Load contract specs and raw exchange metadata for all or selected symbols."""
    want = set(symbols) if symbols else None
    out: dict[str, ContractSpec] = {}
    raw: dict[str, dict[str, Any]] = {}
    for entry in client.contracts():
        sym = entry.get("symbol")
        if sym and (want is None or sym in want):
            out[sym] = ContractSpec.from_entry(entry)
            raw[sym] = entry
    return out, raw


def write_starting_heartbeat(
    path: Path,
    *,
    mode: str = "live",
    now_ms: int | None = None,
) -> None:
    """Write an initial ``status=starting`` heartbeat before warmup begins."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    doc = {
        "schema": HB_SCHEMA,
        "ts_ms": now,
        "started_ms": now,
        "loop_ts_ms": now,
        "step_started_ms": 0,
        "step_count": 0,
        "saved_at_utc": iso_utc(now),
        "pid": os.getpid(),
        "mode": mode,
        "status": "starting",
        "positions": 0,
        "stops": 0,
        "armed": 0,
        "positions_detail": [],
    }
    write_atomic(Path(path), json.dumps(doc, ensure_ascii=False))


__all__ = [
    "CampaignState",
    "DailyLossBreaker",
    "EXIT_STOP_TOL_FRAC",
    "MS_1H",
    "N_FILL_H",
    "PRICE_CLOCK_MAX_CATCHUP_STEPS",
    "RateLimiter",
    "STRATEGY_CONFIG",
    "TF_SECONDS_MAP",
    "aggregate_12h",
    "aggregate_bucket",
    "aggregate_tf",
    "cascade_label",
    "client_oid_of",
    "edge_size_multiplier",
    "exit_label",
    "fill_ts_ms",
    "initial_price_clock_ts",
    "load_contracts_live",
    "load_strategy_params",
    "mark_from_reject",
    "new_position_manager",
    "realized_pnl",
    "reconcile_positions",
    "replay_cognition",
    "stop_execution_evidence",
    "stop_reject_no_position",
    "stop_requires_local",
    "trigger_origin_ms",
    "utc_day_gap",
    "utc_ms",
    "worst_case_of_position",
    "write_starting_heartbeat",
]
