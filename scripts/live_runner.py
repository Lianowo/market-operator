"""实盘 runner(实盘接入件;v21 单一生产链;直接提交,不推不合)。

主循环(每 5s 轮询):宇宙发现(Crypto 24h 名义量 > 5M, RWA > 3M)→ 三重滤网多周期
认知帧(1d 战略导航 -> 4h 核心结构 -> 1h 扳机)→ 全局阶段屏障收敛全市场最新收盘 →
canonical 认知 + 路由(MarketRouter + 原子水平位 PositionManager)→ 订单意图 →
broker-live(--mode live 才发真单)→ Telegram 通知;持仓对账 runner↔exchange 每 2min;
断点重启 state.json。

单一生产链(v20/lifecycle 已退役):启动即校验,``cognition.engine`` 出现非 v21
的值或策略配置 ``use_balance_detector`` 不为 true 都直接拒绝启动 —— 旧链不是
"回退选项",让它在配置层显式失败,好过静默换一套语义继续下单。

语义纪律:E 检测/策略语义/历史产物零改动 —— EventDetector/MicTracker/
MarketRouter/PositionManager 一律 canonical 件;本脚本只做接线、执行近似与
风控断件。风控档(risk_frac=0.0075、leverage_cap=20、portfolio_cap=0.5、
日损 -10% 熔断、API 错熔断、每时订单上限)唯一来源 = config/live.json。

硬性安全:凭证运行时自读(secrets_loader),永不打印/入日志/写账本 —— 账本
只记 symbol/价格/数量/路由/客户单号,绝无凭证字段;MODE 必须显式(--mode
live|paper),缺省拒绝启动(fail-closed)。

断点重启不重开重单:开仓意图客户单号 = 确定性 OID(SHA1(symbol|event|
side|decision_ts[:12])),重试/重启同键同号;账本意图与交易所挂单以
clientOid 前缀(OID_PREFIX)对账,异己单一律不触碰。

执行语义(canonical 时序的实盘近似,config/live.json meta 同步):
① 开仓市价意图 → next 2H 开盘成交(paper 虚拟撮合)/live 检测即下市价单;
② S4 LEVEL_REJECTED 前缘限价(entry,role=entry,kind=limit)→ GTC,N_fill=12 墙钟小时
  未成交撤单;③ §8.2 目标减仓(role=reduce,kind=limit @ target_1)→
  同窗口,超时转市价(TIMEOUT_TO_TAKER);S2 段落袋等部分 REDUCE 市价;
④ EXIT 市价;⑤ 保护止损(交易所原生 TPSL `pos_loss` @ 当前 invalid)只保护
  不主出,invalid 变动撤旧换新;⑥ pm(§8)按 12h 收盘裁决,入场成交的认知 bar 不
  裁决(canonical 同口径:opened_ts > bar 标签才跳过);⑦ **决策型离场**由本进程
  发市价单(触发条件是行情状态的函数,不存在可指望的挂单:锁利回吐预算耗尽 /
  防线触及但交易所无保护单),按"成交即记账 + 回执校正"两步出账 —— 详见
  `_sub_bar_close_out` 的 `executor` 契约与 `_live_market_exit`。

用法:
    python scripts/live_runner.py --mode live     # 主 agent 启动(本件不启动)
    python scripts/live_runner.py --mode paper    # 干跑(不发真单)
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
import gzip
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import adopt as adopt_mod                             # noqa: E402
from market_operator import paths                                          # noqa: E402
from market_operator.bitget_live import BitgetError, BitgetLiveClient      # noqa: E402
from market_operator.broker import ContractSpec, floor_step                # noqa: E402
from market_operator.capacity import resolve_dynamic_risk_tier             # noqa: E402
from market_operator.engine import (                                       # noqa: E402
    cap_effective_size_multiplier,
    compute_market_breadth,
    compute_peer_excess_returns,
    markowitz_corr_multiplier,
    peer_lookback_bars,
    resolve_cost_per_side,
    rwa_session_entry_block,
    select_cognition_route_events,
)
from market_operator.heartbeat import (                                    # noqa: E402
    EXIT_LOCK_BUSY,
    SCHEMA as HB_SCHEMA,
    HeartbeatGuard,
    SingletonLock,
    iso_utc,
    write_atomic,
)
from market_operator.hypothesis import (                                   # noqa: E402
    StructureView,
    entry_contract_error,
    entry_cost_error,
    execution_target,
)
from market_operator.live_support import (                                 # noqa: E402
    MS_1H,
    N_FILL_H,
    PRICE_CLOCK_MAX_CATCHUP_STEPS,
    TF_SECONDS_MAP,
    CampaignState,
    DailyLossBreaker,
    RateLimiter as _BaseRateLimiter,
    aggregate_12h,
    aggregate_bucket,
    aggregate_tf,
    cascade_label,
    client_oid_of,
    edge_size_multiplier,
    exit_label,
    fill_ts_ms as _fill_ts_ms,
    initial_price_clock_ts,
    load_contracts_live,
    load_strategy_params,
    mark_from_reject as _mark_from_reject,
    new_position_manager,
    realized_pnl,
    reconcile_positions,
    replay_cognition,
    stop_execution_evidence,
    stop_reject_no_position as _stop_reject_no_position,
    stop_requires_local as _stop_requires_local,
    trigger_origin_ms,
    utc_ms as _utc_ms,
    worst_case_of_position,
    write_starting_heartbeat,
)
from market_operator.position import Position, PositionManager, opp_lock_step  # noqa: E402
from market_operator.routing import MarketRouter                           # noqa: E402
from market_operator.secrets_loader import SecretsError, load_secrets      # noqa: E402
from market_operator.structure import Params                               # noqa: E402
from market_operator.tg import NotificationGuard, TelegramNotifier         # noqa: E402
from market_operator.trigger import resolve_trigger_tf                     # noqa: E402

__all__ = [
    "CampaignState",
    "DailyLossBreaker",
    "LiveRunner",
    "Params",
    "RateLimiter",
    "aggregate_12h",
    "aggregate_bucket",
    "aggregate_tf",
    "cascade_label",
    "client_oid_of",
    "edge_size_multiplier",
    "exit_label",
    "initial_price_clock_ts",
    "load_contracts_live",
    "load_strategy_params",
    "main",
    "new_position_manager",
    "realized_pnl",
    "reconcile_positions",
    "replay_cognition",
    "stop_execution_evidence",
    "trigger_origin_ms",
    "worst_case_of_position",
    "write_starting_heartbeat",
]

CONFIG_PATH = ROOT / "config" / "live.json"
_STOP_TTL_DEFAULT_S = 120.0
_SETTLE_DEADLINE_MS = 180_000
_QUOTE_CACHE_TTL_S = 15.0
_PRUNE_INTERVAL_S = 300.0
_PRUNE_AGE_MS = 86_400_000
_PRUNE_AGE_S = 86_400.0
_JOURNAL_MISS_TTL_S = 120.0
_WARMUP_BATCH_LIMIT = 48
_CHART_BAR_WINDOW = 96
_SIGNAL_DEDUPE_MAX = 2048


class RateLimiter(_BaseRateLimiter):
    """Per-hour order rate limiter bound to this module's ``time.time`` clock."""

    def allow(self, now_s: float | None = None) -> bool:
        return super().allow(time.time() if now_s is None else float(now_s))

    def pending(self, now_s: float | None = None) -> int:
        return super().pending(time.time() if now_s is None else float(now_s))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class LiveRunner:
    """主循环接线;client/tg 注入(paper 冒烟零真单;测试注入假件)。"""

    OID_PREFIX = "mo21"
    MAX_EXIT_SUBMIT_RETRIES = 3      # 决策型离场下单失败的重试上限(每步一次)

    def __init__(
        self,
        mode: str,
        cfg: dict[str, Any],
        client: Any,
        tg: TelegramNotifier,
        *,
        equity0: float | None = None,
        contracts: dict[str, ContractSpec] | None = None,
        contract_raw: dict[str, dict[str, Any]] | None = None,
        state_path: Path | None = None,
        journal_dir: Path | None = None,
        heartbeat_path: Path | None = None,
        symbols: Sequence[str] = (),
        universe_enabled: bool = True,
    ) -> None:
        if mode not in ("live", "paper"):
            raise RuntimeError("MODE 必须显式为 live 或 paper(fail-closed)")
        self.mode, self.cfg, self.client = mode, cfg, client
        self.tg = NotificationGuard(tg)
        self.risk = cfg["risk"]
        self.loop_cfg = cfg["loop"]
        self.cog_cfg = cfg["cognition"]
        self._init_strategy_and_risk()
        self.contracts: dict[str, ContractSpec] = contracts or {}
        self.contract_raw: dict[str, dict[str, Any]] = contract_raw or {}
        self.leverage_by_sym: dict[str, int] = {}
        self._exch_lev_cap: dict[str, int] = {}
        self.equity: float | None = float(equity0) if equity0 is not None else None
        self.unrealized_pl: float = 0.0
        self.peak_equity: float | None = float(equity0) if equity0 is not None else None
        self._init_paths(state_path=state_path, journal_dir=journal_dir, heartbeat_path=heartbeat_path)
        self._init_runtime_state(symbols=symbols, universe_enabled=universe_enabled)
        self._booting = True
        self._boot_beat_at = 0.0
        self._touch_boot_heartbeat()
        self._load_state()
        self._booting = False
        # 启动即落一次心跳:看护立刻看到新 pid + 新 started_ms,不会把旧 loop_ts_ms 误判成卡死。
        with suppress(OSError):
            self._write_json_atomic(
                self.heartbeat_path, self._heartbeat_payload(self.started_ms)
            )

    def _init_strategy_and_risk(self) -> None:
        engine = str(self.cog_cfg.get("engine") or "v21")
        if engine != "v21":
            raise RuntimeError(
                f"cognition.engine={engine!r} 不再支持:实盘只有 v21 一条生产链"
            )
        self.raw_strat, self.params, self.rp, self.rp_by_tf = load_strategy_params()
        self.capacity_mode = str(self.raw_strat.get("capacity_mode") or "admission_full_cost")
        directional = self.risk.get("directional_cap_frac", self.raw_strat.get("directional_cap_frac"))
        self.directional_cap_frac = float(directional) if directional is not None else None
        if self.directional_cap_frac is not None and not (0.0 < self.directional_cap_frac <= 1.0):
            raise ValueError("directional_cap_frac must be in (0,1]")
        self.markowitz_corr_sizing = bool(self.risk.get("markowitz_corr_sizing", False))
        raw_cap_mult = self.risk.get("max_eff_size_mult", self.raw_strat.get("max_eff_size_mult"))
        self.max_eff_size_mult = float(raw_cap_mult) if raw_cap_mult is not None else None
        if self.max_eff_size_mult is not None and self.max_eff_size_mult <= 0.0:
            raise ValueError("max_eff_size_mult must be positive")
        self.price_clock = str((self.cfg.get("protection") or {}).get("price_clock", "bar")).lower()
        if self.price_clock not in ("bar", "15m", "realtime"):
            raise RuntimeError(
                f"protection.price_clock 非法: {self.price_clock!r}(仅允许 bar|15m|realtime)"
            )

    def _init_paths(
        self,
        *,
        state_path: Path | None,
        journal_dir: Path | None,
        heartbeat_path: Path | None,
    ) -> None:
        self.state_path = (
            Path(state_path) if state_path else paths.resolve(self.loop_cfg["state_path"])
        )
        self.campaigns_path = self.state_path.with_name("campaigns.json")
        if heartbeat_path is not None:
            self.heartbeat_path = Path(heartbeat_path)
        elif state_path is not None:
            self.heartbeat_path = self.state_path.parent / "heartbeat.json"
        else:
            hb_rel = self.loop_cfg.get("heartbeat_path")
            self.heartbeat_path = (
                paths.resolve(str(hb_rel)) if hb_rel else self.state_path.parent / "heartbeat.json"
            )
        self.candle_cache_dir = self.state_path.parent / "candles"
        self.journal_dir = (
            Path(journal_dir) if journal_dir else paths.resolve(self.loop_cfg["journal_dir"])
        )

    def _init_runtime_state(
        self,
        *,
        symbols: Sequence[str],
        universe_enabled: bool,
    ) -> None:
        self.started_ms = int(time.time() * 1000)
        self._loop_ts_ms = self.started_ms
        self._step_started_ms = 0
        self._step_count = 0
        self._campaigns_dirty = True
        self._campaigns_saved_at = 0.0
        self.breaker = DailyLossBreaker(self.risk["max_daily_loss_frac"])
        self.rate = RateLimiter(self.risk["orders_per_hour"])
        adopt_cfg = self.cfg.get("adopt_external_positions") or {}
        self.adopt_enabled = bool(adopt_cfg.get("enabled"))
        self.universe_enabled = bool(universe_enabled)
        self.campaigns: dict[str, CampaignState] = {}
        self.extra_campaigns: dict[tuple[str, str], CampaignState] = {}
        self.symbols_pending: list[str] = sorted(symbols)
        self._manage_only: set[str] = set()
        self._universe_symbols: set[str] = set()
        self._short_history_alerted: set[str] = set()
        self.positions: dict[str, dict[str, Any]] = {}
        self.open_intents: dict[str, dict[str, Any]] = {}
        self.stops: dict[str, dict[str, Any]] = {}
        self._signal_sent: dict[tuple[Any, ...], int] = {}
        self.counters: dict[str, int] = {
            "signals": 0, "orders_placed": 0, "cap_skips": 0,
            "tf_cap_skips": 0, "min_notional_skips": 0,
            "rate_skips": 0, "breaker_blocks": 0, "fills": 0,
            "mgmt_execs": 0, "expired_intents": 0,
            "reconcile_alerts": 0, "reduce_fills": 0, "exits": 0,
            "api_halt_skips": 0, "order_errors": 0,
            "lock_exits": 0,
            "exit_submit_errors": 0,
            "unprotected_stop_exits": 0,
            "exit_settle_miss": 0,
            "entry_settle_miss": 0,
            "signal_notice_errors": 0,
        }
        self._universe_at = 0.0
        self._reconcile_at = 0.0
        self._prune_at = 0.0
        self._daily_date: str | None = None
        self._api_error_streak = 0
        self.hedge_mode = (
            str((self.cfg.get("account") or {}).get("position_mode") or "one_way_mode")
            == "hedge_mode"
        )
        self.api_halted = False
        self._api_halt_alerted = False
        self._leverage_done: set[str] = set()
        self._alert_last: dict[str, float] = {}
        self._missing_streak: dict[str, int] = {}
        self._exit_settle: dict[str, dict[str, Any]] = {}
        self._entry_settle: dict[str, dict[str, Any]] = {}
        self._exit_retry: dict[str, dict[str, Any]] = {}
        self._gap_bars: dict[str, list[Any]] = {}
        self._gap_started_ms: int | None = None
        self._gap_replay = False
        self._gap_stop_hit: dict[str, dict[str, Any]] = {}
        self._stops_checked_at = 0.0
        self._stops_live_ids: set[str] | None = None
        self._stops_live_rows: list[dict[str, Any]] | None = None
        self._price_seen: dict[str, int] = {}
        self._price_last: dict[str, int] = {}
        self._signal_quote_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._journal_miss_cache: dict[tuple[str, int], tuple[float, float]] = {}
        self._step_extra_by_sym: dict[str, list[CampaignState]] | None = None
        self._step_peer_cache: dict[tuple[int, bool], tuple[float, int, list[float], list[float]]] | None = None

    def invalidate_stop_cache(self) -> None:
        """Invalidate cached pending TPSL snapshot (also called by HeartbeatGuard)."""
        self._stops_live_ids = None
        self._stops_live_rows = None
        self._stops_checked_at = 0.0

    def _extra_campaigns_for(self, symbol: str) -> list[CampaignState]:
        """Return extra TF campaigns for ``symbol`` in O(1) inside step or fallback safely."""
        step_idx = getattr(self, "_step_extra_by_sym", None)
        if step_idx is not None:
            return list(step_idx.get(symbol, ()))
        extra = getattr(self, "extra_campaigns", None) or {}
        return [sub for (s2, _tf), sub in sorted(extra.items()) if s2 == symbol]

    def _alert_throttled(self, key: str, msg: str, gap_s: float = 1800.0) -> None:
        """同类告警限频(默认 30 分钟一条)。"""
        now = time.time()
        if now - self._alert_last.get(key, 0.0) < gap_s:
            return
        self._alert_last[key] = now
        self.tg.send_alert(msg)

    # ------------------------------------------------------------------
    # 执行口径:持仓模式 → side / tradeSide(纯函数级,不触策略语义)
    # ------------------------------------------------------------------

    def open_trade_side(self) -> str | None:
        """开仓 tradeSide(hedge_mode 必填 'open';one-way 省略)。"""
        return "open" if self.hedge_mode else None

    def close_side_of(self, position_direction: int) -> str:
        """平仓单 side:hedge_mode 下与开仓同侧(靠 tradeSide='close' 区分开平),
        one-way 下取反向买卖方向。position_direction = 被平仓位方向(+1 多)。"""
        if self.hedge_mode:
            return "buy" if position_direction > 0 else "sell"
        return "sell" if position_direction > 0 else "buy"

    def close_trade_side(self) -> str | None:
        """平仓 tradeSide(hedge_mode 必填 'close';one-way 省略)。"""
        return "close" if self.hedge_mode else None

    def stop_hold_side(self, position_direction: int) -> str:
        """原生 TPSL 的 holdSide:hedge_mode 用持仓方向(long/short),
        one-way 用买卖方向(buy 表多仓 / sell 表空仓,同 ccxt 口径)。"""
        if self.hedge_mode:
            return "long" if int(position_direction) > 0 else "short"
        return "buy" if int(position_direction) > 0 else "sell"

    def fit_price(self, sym: str, price: float, direction: int) -> float:
        """按合约价格精度拟合:买/平空向下、卖/平多向上(只紧不松)。"""
        sp = self._spec(sym)
        place = int(sp.price_place)
        step = 10.0 ** (-place)
        px = float(price)
        lo = floor_step(px, step, place)
        if direction > 0:
            return lo
        return round(lo + step, place) if lo < px - 1e-12 else lo

    def current_risk_frac(self, equity: float | None = None) -> float:
        """阶梯动态复利:按历史最高已实现净值(High-Water Mark)单向降档匹配风险档位。"""
        if equity is None:
            raw_eq = float(self.equity)
            upnl = float(getattr(self, "unrealized_pl", 0.0) or 0.0)
            cap = max(0.0, raw_eq - upnl)
        else:
            cap = max(0.0, float(equity))
        prev_hwm = getattr(self, "peak_equity", None)
        hwm = max(cap, float(prev_hwm)) if prev_hwm is not None else cap
        self.peak_equity = hwm
        return resolve_dynamic_risk_tier(
            hwm,
            self.risk.get("dynamic_risk_tiers"),
            float(self.risk["risk_frac"]),
        )

    # ------------------------------------------------------------------
    # 账本 / 状态(只记账不忘秘:绝无凭证字段)
    # ------------------------------------------------------------------

    def _journal(self, kind: str, rec: Mapping[str, Any]) -> None:
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        path = (
            self.journal_dir
            / f"{self.loop_cfg['journal_prefix']}{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        )
        row = {"ts": int(time.time() * 1000), "kind": kind, "mode": self.mode, **rec}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _load_campaigns_doc(self, st: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """优先读取冷档 campaigns.json,缺失或损坏时回落到 state.json 内嵌数据。"""
        saved_camps: dict[str, Any] = {}
        cold_doc: dict[str, Any] = {}
        if self.campaigns_path.exists():
            try:
                cold_doc = json.loads(self.campaigns_path.read_text(encoding="utf-8"))
                saved_camps = cold_doc.get("campaigns") or {}
            except (OSError, ValueError) as exc:
                self.tg.send_alert(
                    f"campaigns.json 读取失败({type(exc).__name__});回落 state.json 内嵌数据"
                )
                saved_camps, cold_doc = {}, {}
        if not saved_camps:
            saved_camps = st.get("campaigns") or {}
        return saved_camps, cold_doc

    def _restore_pm_positions(
        self,
        sym: str,
        cs: CampaignState,
        pm_positions: Mapping[str, Mapping[str, Any]],
    ) -> None:
        for s, pd in pm_positions.items():
            fields = {
                f: pd[f]
                for f in Position.__dataclass_fields__
                if f in pd and not f.startswith("_")
            }
            pos = Position(**fields)
            pos._best = pd.get("_best")
            pos._entry0 = pd.get("_entry0", pos.entry)
            if pos.reenter_invalid_px is None:
                re_saved = self.positions.get(s, {}).get("reenter_invalid_px")
                if re_saved is not None:
                    pos.reenter_invalid_px = float(re_saved)
            pos_tf = self.positions.get(s, {}).get("tf_label")
            target_cs = self._get_cs_for(sym, pos_tf) or cs
            target_cs.pm.positions[s] = pos

    def _restore_saved_campaigns(self, saved_camps: Mapping[str, Mapping[str, Any]]) -> None:
        self._touch_boot_heartbeat()
        want = int(self.cog_cfg["warmup_trigger_bars"])
        prefetched = self._prefetch_bars_parallel(
            list(saved_camps.keys()),
            want,
            seed_by_sym={sym: saved.get("trig_bars") for sym, saved in saved_camps.items()},
        )
        for sym, saved in saved_camps.items():
            self._touch_boot_heartbeat()
            try:
                bars2h = prefetched.get(sym) or []
                saved_bars = saved.get("trig_bars") or []
                if (
                    len(saved_bars) >= 2
                    and len(bars2h) >= 2
                    and int(saved_bars[1][0] - saved_bars[0][0]) != int(bars2h[1][0] - bars2h[0][0])
                ):
                    saved_bars = []
                cut = max((int(b[0]) for b in saved_bars), default=None)
                if cut is not None and bars2h and int(bars2h[-1][0]) > cut:
                    gap = [tuple(b) for b in bars2h if int(b[0]) > cut]
                    if gap:
                        self._gap_bars.setdefault(sym, gap)
                    bars2h = [b for b in bars2h if int(b[0]) <= cut]
                cs = self._build_all_tf_campaigns(sym, bars2h=bars2h)
            except (BitgetError, RuntimeError) as exc:
                self.tg.send_alert(f"{sym} 断点恢复失败:{type(exc).__name__};待宇宙重新发现")
                if sym not in self.symbols_pending:
                    self.symbols_pending.append(sym)
                continue
            if cs is None:
                if sym not in self.symbols_pending:
                    self.symbols_pending.append(sym)
                continue
            self._restore_pm_positions(sym, cs, saved.get("pm_positions") or {})
            self.campaigns[sym] = cs
            if sym in self.positions or self.mode != "live":
                self._ensure_leverage(sym)
            if sym in self.symbols_pending:
                self.symbols_pending.remove(sym)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        st = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.positions = st.get("positions", {})
        self.open_intents = st.get("open_intents", {})
        self.stops = st.get("stops", {})
        self._leverage_done = set(st.get("leverage_done") or [])
        self.leverage_by_sym = dict(st.get("leverage_by_sym") or {})
        self.counters.update(st.get("counters", {}))
        raw_entry = st.get("entry_settle") or {}
        if isinstance(raw_entry, dict):
            self._entry_settle = {str(k): v for k, v in raw_entry.items() if isinstance(v, dict)}
        raw_exit = st.get("exit_settle") or {}
        if isinstance(raw_exit, dict):
            self._exit_settle = {str(k): v for k, v in raw_exit.items() if isinstance(v, dict)}
        b = st.get("breaker", {})
        self.breaker.day = b.get("day")
        self.breaker.start_equity = b.get("start_equity")
        self.breaker.tripped = bool(b.get("tripped"))
        if st.get("equity") and self.equity is None:
            self.equity = float(st["equity"])
        if st.get("peak_equity") is not None:
            saved_hwm = float(st["peak_equity"])
            self.peak_equity = (
                max(saved_hwm, float(self.peak_equity))
                if self.peak_equity is not None
                else saved_hwm
            )
        elif self.equity is not None and self.peak_equity is None:
            self.peak_equity = float(self.equity)
        if st.get("equity") is not None:
            self.breaker.last_equity = float(st["equity"])
        saved_camps, cold_doc = self._load_campaigns_doc(st)
        persisted_gap = cold_doc.get("gap_bars") or st.get("gap_bars") or {}
        persisted_start = st.get("gap_started_ms")
        self._gap_started_ms = (
            int(persisted_start) if persisted_start else _utc_ms(st.get("saved_at_utc"))
        )
        for sym, bars in persisted_gap.items():
            if bars:
                self._gap_bars[sym] = [tuple(b) for b in bars]
        if saved_camps:
            self._restore_saved_campaigns(saved_camps)
        if not self._gap_bars:
            self._gap_started_ms = None


    def heartbeat_doc(
        self,
        now_ms: int,
        *,
        status: str | None = None,
        guard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """心跳文档(跨平台看护的**唯一**判活依据;不含任何进程名/端口概念)。"""
        if status is None:
            if self._gap_replay:
                status = "recovering"
            elif self._gap_bars:
                status = "gap_pending"
            else:
                status = "ok"
        gap_syms = sorted(self._gap_bars)
        detail = []
        for sym, pos in list(self.positions.items())[:64]:
            st = self.stops.get(sym) or {}
            detail.append({
                "symbol": sym, "direction": pos.get("direction"),
                "qty": pos.get("qty"), "invalid": pos.get("invalid"),
                "mark": pos.get("mark"), "tf": pos.get("tf_label"),
                "event_id": pos.get("event_id"),
                "stop": st.get("trigger"), "stop_id": st.get("order_id"),
            })
        doc: dict[str, Any] = {
            "schema": HB_SCHEMA,
            "ts_ms": int(now_ms),
            "started_ms": int(self.started_ms),
            "loop_ts_ms": int(self._loop_ts_ms or self.started_ms),
            "step_started_ms": int(self._step_started_ms or 0),
            "step_count": int(self._step_count),
            "saved_at_utc": iso_utc(int(now_ms)),
            "pid": os.getpid(),
            "mode": self.mode,
            "status": status,
            "equity": self.equity,
            "positions": len(self.positions),
            "stops": len(self.stops),
            "armed": sum(1 for it in self.open_intents.values() if it.get("status") == "armed"),
            "positions_detail": detail,
            "gap_started_ms": self._gap_started_ms,
            "gap_symbols_total": len(gap_syms),
            "gap_symbols": gap_syms[:20],
            "gap_bars": sum(len(v) for v in self._gap_bars.values()),
        }
        if guard is not None:
            doc["guard"] = guard
        return doc

    def _heartbeat_payload(self, now_ms: int) -> str:
        """兼容入口:心跳文档的 JSON 文本(落盘用)。"""
        return json.dumps(self.heartbeat_doc(now_ms), ensure_ascii=False)

    def _touch_boot_heartbeat(self) -> None:
        """预热中刷新 status=starting,最多每 10 秒一次。"""
        if not getattr(self, "_booting", False):
            return
        now = time.time()
        if now - float(getattr(self, "_boot_beat_at", 0.0) or 0.0) < 10.0:
            return
        self._boot_beat_at = now
        with suppress(OSError):
            self._write_json_atomic(
                self.heartbeat_path,
                json.dumps(self.heartbeat_doc(int(now * 1000), status="starting"), ensure_ascii=False),
            )

    def _write_json_atomic(self, path: Path, payload: str) -> None:
        """委托 heartbeat.write_atomic(同目录 pid.tid 唯一临时文件 + os.replace)。"""
        write_atomic(path, payload)

    def _touch_campaigns(self) -> None:
        """标记战役状态已变:下一次落盘必须写 campaigns.json(不等档期)。"""
        self._campaigns_dirty = True

    def _campaigns_payload(self) -> str:
        """战役状态(冷数据):每品种 **尾部** bar + pm 持仓事实(单遍 O(M) 聚合)。"""
        tail = max(1, int(self.loop_cfg.get("state_bar_tail", 24)))
        extra_by_sym: dict[str, list[CampaignState]] = defaultdict(list)
        for (s2, _tf), sub in self.extra_campaigns.items():
            extra_by_sym[s2].append(sub)
        camps: dict[str, dict[str, Any]] = {}
        for sym, cs in self.campaigns.items():
            pm_pos: dict[str, dict[str, Any]] = {}
            for c_item in (cs, *extra_by_sym.get(sym, ())):
                for s, p in c_item.pm.positions.items():
                    rec = {
                        f: getattr(p, f)
                        for f in Position.__dataclass_fields__
                        if not f.startswith("_")
                    }
                    rec["_best"], rec["_entry0"] = p._best, p._entry0
                    pm_pos[s] = rec
            camps[sym] = {"trig_bars": cs.trig_bars[-tail:], "pm_positions": pm_pos}
        return json.dumps(
            {
                "saved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "bar_tail": tail,
                "gap_bars": self._gap_bars,
                "campaigns": camps,
            },
            ensure_ascii=False,
        )

    def _save_state(self) -> None:
        """两档落盘:state.json(热,每步) + campaigns.json(冷,变更或到档期)。"""
        payload = json.dumps(
            {
                "saved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mode": self.mode,
                "equity": self.equity,
                "peak_equity": getattr(self, "peak_equity", None),
                "positions": self.positions,
                "open_intents": self.open_intents,
                "stops": self.stops,
                "counters": self.counters,
                "entry_settle": self._entry_settle,
                "exit_settle": self._exit_settle,
                "leverage_done": sorted(self._leverage_done),
                "leverage_by_sym": self.leverage_by_sym,
                "gap_started_ms": self._gap_started_ms,
                "breaker": {
                    "day": self.breaker.day,
                    "start_equity": self.breaker.start_equity,
                    "tripped": self.breaker.tripped,
                },
            },
            ensure_ascii=False,
        )
        self._write_json_atomic(self.state_path, payload)
        try:
            self._write_json_atomic(
                self.heartbeat_path,
                self._heartbeat_payload(int(time.time() * 1000)),
            )
        except OSError as exc:
            with suppress(OSError):
                self._journal("heartbeat_write_error", {"why": str(exc)[:160]})

        every = float(self.loop_cfg.get("campaigns_write_seconds", 60))
        now = time.time()
        if not (self._campaigns_dirty or now - self._campaigns_saved_at >= every):
            return
        self._write_json_atomic(self.campaigns_path, self._campaigns_payload())
        self._campaigns_dirty = False
        self._campaigns_saved_at = now

    # ------------------------------------------------------------------
    # 战役装配(本地磁盘 K 线缓存 + 增量回补 + 并发拉取 + 按周期裁剪认知重放)
    # ------------------------------------------------------------------

    def _candle_cache_path(
        self,
        symbol: str,
        *,
        gz: bool = True,
        gran: str | None = None,
    ) -> Path:
        g = str(gran or self.cog_cfg.get("granularity") or "2H").upper()
        suffix = ".json.gz" if gz else ".json"
        return self.candle_cache_dir / f"{symbol}_{g}{suffix}"

    def _load_cached_bars(
        self,
        symbol: str,
        *,
        gran: str | None = None,
    ) -> list[tuple[int, float, float, float, float, float]]:
        """读缓存:优先 .json.gz,回落旧 .json。"""
        for gz in (True, False):
            p = self._candle_cache_path(str(symbol), gz=gz, gran=gran)
            if not p.exists():
                continue
            try:
                if gz:
                    with gzip.open(p, "rt", encoding="utf-8") as fh:
                        raw = json.load(fh)
                else:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                return [
                    (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                    for r in raw
                ]
            except (OSError, ValueError, TypeError, KeyError, IndexError):
                continue
        return []

    def _save_cached_bars(
        self,
        symbol: str,
        bars: Sequence[Any],
        *,
        gran: str | None = None,
    ) -> None:
        if not bars:
            return
        try:
            self.candle_cache_dir.mkdir(parents=True, exist_ok=True)
            want = int(self.cog_cfg.get("warmup_trigger_bars", 3600))
            p = self._candle_cache_path(str(symbol), gran=gran)
            payload = json.dumps(list(bars[-want:]), separators=(",", ":")).encode("utf-8")
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
            tmp.write_bytes(gzip.compress(payload))
            os.replace(tmp, p)
            legacy = self._candle_cache_path(str(symbol), gz=False, gran=gran)
            if legacy.exists():
                legacy.unlink(missing_ok=True)
        except OSError as exc:
            with suppress(OSError):
                self._journal("candle_cache_write_error", {"symbol": symbol, "why": str(exc)[:120]})

    def _base_step_seconds(
        self,
        bars: Sequence[Any] | None = None,
        *,
        gran: str | None = None,
    ) -> int:
        if bars and len(bars) >= 2 and int(bars[1][0]) > int(bars[0][0]):
            return int(bars[1][0] - bars[0][0]) // 1000
        g = str(gran or self.cog_cfg.get("granularity") or "2H").strip().lower()
        if g.endswith("utc"):
            g = g[:-3]
        return int(TF_SECONDS_MAP.get(g, 7200))

    def _refresh_cached_tail(
        self,
        symbol: str,
        use_gran: str,
        base_ms: int,
        have: dict[int, tuple[int, float, float, float, float, float]],
    ) -> None:
        if not have or not hasattr(self.client, "history_candles"):
            return
        now_ms = int(time.time() * 1000)
        last_ts = max(have)
        if now_ms < last_ts + 2 * base_ms:
            return
        # 回看 64 根做 UPSERT 覆盖交易所偶发的零成交占位条
        refresh_from = max(0, last_ts - 64 * base_ms)
        for _ in range(20):
            fresh = self.client.history_candles(symbol, use_gran, start_ms=refresh_from, limit=200)
            if not fresh:
                break
            for b in fresh:
                have[int(b[0])] = b
            new_last = max(have)
            if new_last <= last_ts:
                break
            last_ts = new_last
            if len(fresh) < 200 or now_ms < last_ts + 2 * base_ms:
                break
            refresh_from = last_ts + 1

    def _fetch_trigger_bars(
        self,
        symbol: str,
        want: int,
        seed_bars: Sequence[Any] | None = None,
        *,
        gran: str | None = None,
    ) -> list[tuple[int, float, float, float, float, float]]:
        """基础触发帧回补:优先复用本地缓存 + 增量补齐;不足时倒序分页补齐并落盘缓存。"""
        use_gran = str(gran or self.cog_cfg["granularity"])
        base_ms = self._base_step_seconds(gran=use_gran) * 1000
        valid_seed = list(seed_bars or ())
        if len(valid_seed) >= 2 and int(valid_seed[1][0] - valid_seed[0][0]) != base_ms:
            valid_seed = []
        have: dict[int, tuple[int, float, float, float, float, float]] = {}
        for src in (self._load_cached_bars(symbol, gran=use_gran), valid_seed):
            for b in src:
                have[int(b[0])] = (
                    int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])
                )
        self._refresh_cached_tail(symbol, use_gran, base_ms, have)
        bars_cached = [have[t] for t in sorted(have)]
        if len(bars_cached) >= 2 and any(
            b[0] - a[0] != base_ms for a, b in zip(bars_cached, bars_cached[1:])
        ):
            have.clear()
        end_ms: int | None = min(have) if have else None
        max_pages = max(1, (want // 200) + 2)
        for _ in range(max_pages):
            if len(have) >= want:
                break
            page = self.client.history_candles(symbol, use_gran, end_ms=end_ms, limit=200)
            fresh = [b for b in page if b[0] not in have]
            if not fresh:
                break
            for b in fresh:
                have[int(b[0])] = b
            end_ms = fresh[0][0]
        bars = [have[t] for t in sorted(have)][-want:]
        self._save_cached_bars(symbol, bars, gran=use_gran)
        return bars

    def _prefetch_bars_parallel(
        self,
        symbols: list[str],
        want: int,
        seed_by_sym: Mapping[str, Any] | None = None,
    ) -> dict[str, list[Any]]:
        """多线程并发预取各品种 K 线(冷启动/宇宙批量加入时提速 6~8 倍)。"""
        if not symbols:
            return {}
        seed_map = seed_by_sym or {}
        if len(symbols) == 1:
            sym = symbols[0]
            return {sym: self._fetch_trigger_bars(sym, want, seed_map.get(sym))}
        out: dict[str, list[Any]] = {}
        max_workers = min(16, len(symbols))
        sub_gran, _sub_sec = self._sub_gran_and_seconds()
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        need_sub = (
            self.mode == "live"
            and hasattr(self.client, "history_candles")
            and any(int(trig_map.get(tf) or 7200) < 7200 for tf in (self.cog_cfg.get("tf_list") or ()))
        )
        sub_want = max(520, (self._min_cog_bars_for_tf() + 5) * 8)

        def _fetch_one(s: str) -> list[Any]:
            res_2h = self._fetch_trigger_bars(s, want, seed_map.get(s))
            if need_sub:
                try:
                    self._fetch_trigger_bars(s, sub_want, gran=sub_gran)
                except (BitgetError, OSError, ValueError) as exc:
                    with suppress(OSError):
                        self._journal("prefetch_sub_error", {"symbol": s, "why": str(exc)[:120]})
            return res_2h

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_fetch_one, sym): sym for sym in symbols}
            for fut in as_completed(futs):
                self._touch_boot_heartbeat()
                sym = futs[fut]
                try:
                    out[sym] = fut.result()
                except Exception as exc:
                    with suppress(OSError):
                        self._journal("prefetch_bar_error", {"symbol": sym, "why": str(exc)[:120]})
                    out[sym] = []
        return out

    def _sub_gran_and_seconds(self) -> tuple[str, int]:
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        tf_list = self.cog_cfg.get("tf_list") or ()
        min_trig = min(
            (int(trig_map[tf]) for tf in tf_list if trig_map.get(tf) is not None),
            default=7200,
        )
        return ("15m", 900) if min_trig <= 900 else ("30m", 1800)

    def _min_cog_bars_for_tf(self) -> int:
        return max(30, int(self.cog_cfg.get("min_warmup_cog_bars", 60)))

    def _resolve_tf_seconds(self, tf_label: str) -> tuple[int, int]:
        obs_s = TF_SECONDS_MAP.get(tf_label, int(self.cog_cfg["obs_tf_seconds"]))
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        if tf_label in trig_map and trig_map[tf_label] is not None:
            return obs_s, int(trig_map[tf_label])
        if tf_label in trig_map and trig_map[tf_label] is None:
            return obs_s, obs_s
        trig_s = int(self.cog_cfg["trigger_tf_seconds"])
        ttf, decl = resolve_trigger_tf(obs_s)
        if ttf != trig_s or decl is not None:
            raise RuntimeError(f"trigger 解析漂移: {decl}")
        return obs_s, trig_s

    def _select_trigger_series(
        self,
        symbol: str,
        bars2h: list[Any],
        trig_bars: list[Any] | None,
        *,
        obs_s: int,
        trig_s: int,
        base_s: int,
        min_cog_bars: int,
        warmup_cog: int,
    ) -> tuple[list[Any], str]:
        sub_gran, sub_sec = ("15m", 900) if trig_s <= 900 else ("30m", 1800)
        if (
            trig_bars is None
            and trig_s < base_s
            and self.mode == "live"
            and hasattr(self.client, "history_candles")
        ):
            sub_scale = max(1, obs_s // sub_sec)
            sub_want = max(520, (warmup_cog + 5) * sub_scale)
            cand_sub = self._fetch_trigger_bars(symbol, sub_want, gran=sub_gran)
            if (
                self._base_step_seconds(cand_sub, gran=sub_gran) == sub_sec
                and len(cand_sub) >= min_cog_bars * 4
            ):
                trig_bars = cand_sub
        use_trig = trig_bars if trig_bars is not None else bars2h
        if trig_bars is not None:
            trig_base_s = self._base_step_seconds(trig_bars, gran=sub_gran)
            if any(b[0] - a[0] != trig_base_s * 1000 for a, b in zip(trig_bars, trig_bars[1:])):
                use_trig = bars2h
        return use_trig, sub_gran

    def _build_campaign(
        self,
        symbol: str,
        tf_label: str | None = None,
        bars2h: list[Any] | None = None,
        *,
        trig_bars: list[Any] | None = None,
        quiet_if_short: bool = False,
    ) -> CampaignState | None:
        tf_label = tf_label or str(self.cog_cfg.get("main_tf_label") or "12h")
        obs_s, trig_s = self._resolve_tf_seconds(tf_label)
        want = int(self.cog_cfg["warmup_trigger_bars"])
        if bars2h is None:
            bars2h = self._fetch_trigger_bars(symbol, want)
        min_cog_bars = self._min_cog_bars_for_tf()
        base_s = self._base_step_seconds(bars2h)
        scale_2h = max(1, obs_s // base_s)
        min_2h_needed = min_cog_bars * scale_2h
        if len(bars2h) < min_2h_needed:
            if not quiet_if_short and symbol not in self._short_history_alerted:
                self._short_history_alerted.add(symbol)
                self.tg.send_alert(
                    f"{symbol}({tf_label}) 2H 历史不足({len(bars2h)}/{min_2h_needed} 根),暂不开启该周期"
                )
            return None
        for a, b in zip(bars2h, bars2h[1:]):
            if b[0] - a[0] != base_s * 1000:
                self.tg.send_alert(f"{symbol} 2H 序列缺口 {a[0]}→{b[0]},暂不交易")
                return None
        warmup_cog = max(
            min_cog_bars,
            int(self.cog_cfg.get("warmup_cog_bars", 160)),
            int(self.params.canvas_bars),
        )
        use_trig, sub_gran = self._select_trigger_series(
            symbol, bars2h, trig_bars,
            obs_s=obs_s, trig_s=trig_s, base_s=base_s,
            min_cog_bars=min_cog_bars, warmup_cog=warmup_cog,
        )
        if obs_s < base_s:
            if use_trig is bars2h:
                return None
            trig_step = self._base_step_seconds(use_trig, gran=sub_gran)
            scale_sub = max(1, obs_s // trig_step)
            slice_sub = use_trig[-min(len(use_trig), (warmup_cog + 2) * scale_sub):]
            cog_bars = aggregate_tf(slice_sub, obs_s)
        else:
            need_2h = min(len(bars2h), (warmup_cog + 2) * scale_2h)
            cog_bars = aggregate_tf(bars2h[-need_2h:], obs_s)
        if len(cog_bars) > warmup_cog:
            cog_bars = cog_bars[-warmup_cog:]
        if len(cog_bars) < min_cog_bars:
            if not quiet_if_short and symbol not in self._short_history_alerted:
                self._short_history_alerted.add(symbol)
                self.tg.send_alert(
                    f"{symbol} {tf_label} 认知帧过短({len(cog_bars)}/{min_cog_bars} 根),暂不开启该周期"
                )
            return None
        first_cog_ts = cog_bars[0][0]
        replay_trig = [b for b in use_trig if b[0] >= first_cog_ts]
        detector, tracker = replay_cognition(
            self.params, symbol, cog_bars, replay_trig,
            obs_tf_seconds=obs_s, trigger_tf_seconds=trig_s,
        )
        rp_tf = resolve_cost_per_side(
            self.rp_by_tf.get(tf_label, self.rp),
            symbol,
            taker_fee=float(self.risk.get("taker_fee", self.raw_strat.get("taker_fee", 0.0006))),
            slippage_per_side=float(
                self.risk.get("slippage_per_side", self.raw_strat.get("slippage_per_side", 0.0002))
            ),
            slippage_by_symbol=self.risk.get("slippage_by_symbol") or self.raw_strat.get("slippage_by_symbol"),
        )
        return CampaignState(
            symbol=symbol,
            detector=detector,
            tracker=tracker,
            router=MarketRouter(rp_tf),
            pm=new_position_manager(self.params, self.raw_strat),
            cog_bars=list(cog_bars),
            trig_bars=list(use_trig),
            tf_label=tf_label,
            obs_tf_seconds=obs_s,
            trigger_tf_seconds=trig_s,
        )

    def _build_all_tf_campaigns(
        self,
        symbol: str,
        bars2h: list[Any] | None = None,
    ) -> CampaignState | None:
        """构建主周期 CampaignState 并返回,同时装配其余周期至 extra_campaigns。"""
        want = int(self.cog_cfg["warmup_trigger_bars"])
        if bars2h is None:
            bars2h = self._fetch_trigger_bars(symbol, want)
        bars_sub: list[Any] | None = None
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        sub_gran, sub_sec = self._sub_gran_and_seconds()
        if (
            self.mode == "live"
            and hasattr(self.client, "history_candles")
            and any(int(trig_map.get(tf) or 7200) < 7200 for tf in (self.cog_cfg.get("tf_list") or ()))
        ):
            sub_want = max(680, (self._min_cog_bars_for_tf() + 5) * 8)
            cand_sub = self._fetch_trigger_bars(symbol, sub_want, gran=sub_gran)
            if (
                self._base_step_seconds(cand_sub, gran=sub_gran) == sub_sec
                and len(cand_sub) >= self._min_cog_bars_for_tf() * 4
            ):
                bars_sub = cand_sub
        main_tf = str(self.cog_cfg.get("main_tf_label") or "12h")
        tf_list = list(self.cog_cfg.get("tf_list") or [main_tf])
        built: dict[str, CampaignState] = {}
        for tf in tf_list:
            sub_trig = (
                bars_sub
                if (bars_sub is not None and int(trig_map.get(tf) or 7200) < 7200)
                else None
            )
            cs_tf = self._build_campaign(symbol, tf, bars2h=bars2h, trig_bars=sub_trig, quiet_if_short=True)
            if cs_tf is None and sub_trig is not None:
                cs_tf = self._build_campaign(symbol, tf, bars2h=bars2h, quiet_if_short=True)
            if cs_tf is not None:
                built[tf] = cs_tf
        if not built:
            min_cog = self._min_cog_bars_for_tf()
            if symbol not in self._short_history_alerted:
                self._short_history_alerted.add(symbol)
                self.tg.send_alert(
                    f"{symbol} 新币 2H 历史不足({len(bars2h)}/{min_cog} 根),"
                    f"满 {min_cog} 根(约 {min_cog * 2 // 24} 天)后将自动开启交易"
                )
            return None
        self._short_history_alerted.discard(symbol)
        primary_tf = main_tf if main_tf in built else next(iter(built))
        for tf, cs_sub in built.items():
            if tf != primary_tf:
                self.extra_campaigns[(symbol, tf)] = cs_sub
        return built[primary_tf]

    def _try_unlock_higher_tfs(self, symbol: str, bars2h: list[Any]) -> None:
        """新币随 K 线积累达到高周期准入门槛时,自动解锁并挂载对应周期战役。"""
        if symbol not in self.campaigns:
            return
        tf_list = list(self.cog_cfg.get("tf_list") or [])
        extras = self._extra_campaigns_for(symbol)
        active_tfs = {self.campaigns[symbol].tf_label} | {sc.tf_label for sc in extras}
        min_cog = self._min_cog_bars_for_tf()
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        sub_30m = next(
            (sc.trig_bars for sc in extras if self._base_step_seconds(sc.trig_bars) in (900, 1800)),
            None,
        )
        base_s = self._base_step_seconds(bars2h)
        for tf in tf_list:
            if tf in active_tfs:
                continue
            obs_s = TF_SECONDS_MAP.get(tf, int(self.cog_cfg["obs_tf_seconds"]))
            scale_2h = max(1, obs_s // base_s)
            if len(bars2h) < (min_cog + 1) * scale_2h:
                continue
            use_sub = (
                sub_30m
                if (sub_30m is not None and int(trig_map.get(tf) or 7200) < 7200)
                else None
            )
            cs_new = self._build_campaign(symbol, tf, bars2h=bars2h, trig_bars=use_sub, quiet_if_short=True)
            if cs_new is None and use_sub is not None:
                cs_new = self._build_campaign(symbol, tf, bars2h=bars2h, quiet_if_short=True)
            if cs_new is not None:
                self.extra_campaigns[(symbol, tf)] = cs_new

    def _get_cs_for(self, symbol: str, tf_label: str | None = None) -> CampaignState | None:
        if tf_label and (symbol, tf_label) in self.extra_campaigns:
            return self.extra_campaigns[(symbol, tf_label)]
        return self.campaigns.get(symbol)

    def _max_allowed_leverage(self, symbol: str) -> int:
        """返回该标的允许的最大整数杠杆:min(配置 leverage_cap, 交易所合约 maxLever)。"""
        cap = int(self.risk.get("leverage_cap") or 20)
        try:
            max_lev = int(float((self.contract_raw.get(symbol) or {}).get("maxLever") or cap))
        except (TypeError, ValueError):
            max_lev = cap
        ceiling = max(1, min(cap, max_lev))
        exch_cap = getattr(self, "_exch_lev_cap", {}).get(symbol)
        if exch_cap is not None:
            ceiling = max(1, min(ceiling, int(exch_cap)))
        return ceiling

    def _plan_trade_leverage(
        self,
        symbol: str,
        notional: float,
        equity: float,
        capital: float,
        candidate_risk: float,
    ) -> int:
        """根据单笔风险预算、止损距离与组合保证金规划,计算所需最小整数杠杆。"""
        max_allowed = self._max_allowed_leverage(symbol)
        if equity <= 0.0 or notional <= 0.0:
            return max(1, min(5, max_allowed))
        port_cap = max(float(self.risk.get("portfolio_cap_frac") or 0.50), 1e-6)
        risk_slot_share = candidate_risk / max(port_cap * max(capital, 1e-6), 1e-6)
        planned_margin_frac = min(0.25, max(0.05, risk_slot_share * 0.80))
        used_margin = 0.0
        for s_open, p_open in self.positions.items():
            p_notional = abs(
                float(p_open.get("qty") or 0.0)
                * float(p_open.get("mark") or p_open.get("entry") or 0.0)
            )
            p_lev = max(1.0, float(p_open.get("leverage") or self.leverage_by_sym.get(s_open) or max_allowed))
            used_margin += p_notional / p_lev
        avail_margin = max(0.04 * equity, 0.85 * equity - used_margin)
        margin_budget = max(1e-6, min(equity * planned_margin_frac, avail_margin))
        req_lev = int(math.ceil(notional / margin_budget))
        return max(1, min(max_allowed, req_lev))

    def _ensure_leverage(self, symbol: str, target_lev: int | None = None) -> None:
        """杠杆动态设置(在 <= min(leverage_cap, maxLever) 范围内按需设置;hedge_mode 逐侧设置)。"""
        if self.mode != "live":
            return
        max_allowed = self._max_allowed_leverage(symbol)
        if target_lev is None:
            if symbol not in self._leverage_done:
                self.leverage_by_sym.setdefault(symbol, max_allowed)
            return
        want = max(1, min(int(target_lev), max_allowed))
        if not hasattr(self.client, "set_leverage"):
            self._leverage_done.add(symbol)
            self.leverage_by_sym[symbol] = want
            return
        if symbol in self._leverage_done and self.leverage_by_sym.get(symbol) == want:
            return
        if symbol in self.positions and symbol in self._leverage_done:
            return
        sides = ("long", "short") if self.hedge_mode else (None,)
        ladder = [want] + [lv for lv in (max_allowed, 10, 5, 3, 2, 1) if lv != want and lv <= max_allowed]
        last_err: Exception | None = None
        for lev in ladder:
            try:
                for hs in sides:
                    self.client.set_leverage(symbol, lev, hold_side=hs)
                self._leverage_done.add(symbol)
                self.leverage_by_sym[symbol] = lev
                if lev < want:
                    exch_cap = getattr(self, "_exch_lev_cap", None)
                    if exch_cap is None:
                        exch_cap = {}
                        self._exch_lev_cap = exch_cap
                    exch_cap[symbol] = lev
                self._journal(
                    "leverage",
                    {
                        "symbol": symbol,
                        "leverage": lev,
                        "planned_leverage": want,
                        "max_lever": max_allowed,
                        "sides": [s or "single" for s in sides],
                    },
                )
                return
            except BitgetError as exc:
                last_err = exc
        self._alert_throttled(
            f"lev:{symbol}",
            f"{symbol} 杠杆设置失败(已试 {ladder}):{str(last_err)[:120]}"
            "(按交易所上限降级后仍失败,该品种下单可能受保证金限制)",
        )

    # ------------------------------------------------------------------
    # 主循环一步
    # ------------------------------------------------------------------

    def step(self, now_ms: int) -> None:
        """主循环一步:异常照常上抛(main 处置),但断点状态必定落盘(finally)。"""
        self._step_started_ms = int(now_ms)
        self._step_count += 1
        try:
            if self._gap_bars and not self.recover_gap(now_ms):
                return
            self._step_inner(now_ms)
            self._loop_ts_ms = int(now_ms)
        finally:
            try:
                self._save_state()
            except Exception as exc:
                err_txt = f"{type(exc).__name__}: {str(exc)[:160]}"
                with suppress(OSError):
                    self._journal("state_save_error", {"why": err_txt})
                with suppress(Exception):
                    self._alert_throttled("state_save", f"断点状态落盘失败: {err_txt}")

    # ------------------------------------------------------------------
    # 掉线补做(gap recovery):缺失时段内的 canonical 持仓管理补执行
    # ------------------------------------------------------------------

    def _recover_symbol_gap(
        self,
        sym: str,
        cs: CampaignState,
        extra_list: Sequence[CampaignState],
        bars: list[Any],
        now_ms: int,
    ) -> None:
        gap_step_ms = self._base_step_seconds(bars) * 1000
        cs_step_ms = self._base_step_seconds(cs.trig_bars or bars) * 1000
        if cs_step_ms == gap_step_ms:
            self._advance(cs, now_ms, new_bars=bars)
        sub_cache: dict[int, list[Any]] = {}
        for sub in extra_list:
            sub_step_ms = self._base_step_seconds(sub.trig_bars or bars) * 1000
            if sub_step_ms == gap_step_ms:
                self._advance(sub, now_ms, new_bars=bars, run_paper_fills=False)
                continue
            if not (
                sub_step_ms in (900_000, 1_800_000)
                and self.mode == "live"
                and sub.trig_bars
                and hasattr(self.client, "history_candles")
            ):
                continue
            if sub_step_ms not in sub_cache:
                s_gran = "15m" if sub_step_ms == 900_000 else "30m"
                sub_cache[sub_step_ms] = (
                    self.client.history_candles(
                        sym, s_gran, start_ms=int(sub.trig_bars[-1][0]) + 1, limit=200
                    )
                    or []
                )
            if sub_cache[sub_step_ms]:
                self._advance(sub, now_ms, new_bars=sub_cache[sub_step_ms], run_paper_fills=False)

    def recover_gap(self, now_ms: int) -> bool:
        """把崩溃期间已收盘的 bar 逐根补放进 canonical 管理链,再结算。"""
        gcfg = dict(self.loop_cfg.get("gap_recovery") or {})
        gap_s = (
            0.0
            if self._gap_started_ms is None
            else max(0.0, (now_ms - self._gap_started_ms) / 1000.0)
        )
        big = (
            self.mode == "live"
            and bool(gcfg.get("enabled", True))
            and gap_s >= float(gcfg.get("min_gap_seconds", 180))
        )
        if self.mode == "live" and not self._pre_gap_sync():
            return False
        if big:
            self.tg.send_alert(
                f"检测到掉线缺口 {gap_s / 3600:.2f}h:开始补做持仓管理"
                f"({len(self._gap_bars)} 个品种,"
                f"{sum(len(v) for v in self._gap_bars.values())} 根 2H bar);"
                "补做期间暂停新开仓"
            )
        extra_by_sym: dict[str, list[CampaignState]] = defaultdict(list)
        for (s2, _tf), sub in sorted(self.extra_campaigns.items()):
            extra_by_sym[s2].append(sub)
        self._gap_replay = True
        self._gap_stop_hit = {}
        try:
            for sym in sorted(self._gap_bars):
                cs = self.campaigns.get(sym)
                if cs is not None:
                    self._recover_symbol_gap(
                        sym, cs, extra_by_sym.get(sym, ()), self._gap_bars[sym], now_ms
                    )
        except BitgetError as exc:
            self._api_note(exc)
            self.tg.send_alert(f"掉线补做中断({type(exc).__name__});保留缺口批次,下一轮重试")
            return False
        finally:
            self._gap_replay = False
        self._gap_bars.clear()
        self._touch_campaigns()
        self._gap_started_ms = None
        if big:
            self._gap_settle(now_ms, gap_s)
        return True

    def _pre_gap_sync(self) -> bool:
        """补做前的一次性权威同步:数量/标记价以交易所为准,已平仓即清理。"""
        try:
            ex_pos = self.client.positions()
        except BitgetError as exc:
            self._api_note(exc)
            return False
        ex_by = {p["symbol"]: p for p in ex_pos}
        for sym, pos in list(self.positions.items()):
            p = ex_by.get(sym)
            if p is None or int(p["direction"]) != int(pos["direction"]):
                self._journal("gap_closed_elsewhere", {"symbol": sym, "qty": pos.get("qty")})
                self._reconcile_closed(sym, pos)
                continue
            if abs(float(p["qty"]) - float(pos["qty"])) > max(1e-9, float(p["qty"]) * 1e-6):
                self._journal(
                    "gap_qty_fix",
                    {"symbol": sym, "ours": float(pos["qty"]), "exchange": float(p["qty"])},
                )
                pos["qty"] = float(p["qty"])
            pos["mark"] = float(p["mark_price"])
            cs = self._get_cs_for(sym, pos.get("tf_label"))
            if cs is not None and sym in cs.pm.positions:
                with suppress(AttributeError, TypeError, ValueError):
                    cs.pm.positions[sym].qty = float(p["qty"])
        return True

    def _gap_settle(self, now_ms: int, gap_s: float) -> None:
        """缺口补放后的结算:把本应已发生但错过的止损补成市价动作。"""
        gcfg = dict(self.loop_cfg.get("gap_recovery") or {})
        stop_policy = str(gcfg.get("stop_policy", "market")).lower()
        marker = f"GAP|gap={gap_s / 3600:.2f}h"
        acted = 0
        for sym, hit in sorted(self._gap_stop_hit.items()):
            pos = self.positions.get(sym)
            if pos is None:
                continue
            mark = float(pos.get("mark") or 0.0)
            if stop_policy != "market":
                self._alert_throttled(
                    f"gapstop:{sym}",
                    f"{marker} {sym} 缺口内已触及止损 {hit['stop']:.8g}"
                    f"(bar={hit['ts']}),现价 {mark:.8g};stop_policy="
                    f"{stop_policy} → 保留仓位并按最新防线重挂止损",
                )
                continue
            self._gap_market_exit(
                sym, pos, now_ms, f"{marker}|STOP_MISSED|bar={hit['ts']}|stop={hit['stop']:.8g}"
            )
            self.tg.send_alert(
                f"{marker} {sym} 缺口内已触及止损 {hit['stop']:.8g}"
                f",交易所仍有仓(计划单未触发)→ 市价离场(现价 {mark:.8g})"
            )
            acted += 1
        for sym, pos in sorted(self.positions.items()):
            if sym in self._gap_stop_hit:
                continue
            mark = float(pos.get("mark") or 0.0)
            if mark <= 0.0:
                continue
            stop = float(pos["invalid"])
            breached = (mark <= stop) if int(pos["direction"]) > 0 else (mark >= stop)
            if not breached:
                continue
            self._gap_market_exit(sym, pos, now_ms, f"{marker}|STOP_ALREADY_BREACHED|stop={stop:.8g}")
            self.tg.send_alert(
                f"{marker} {sym} 补放后防线 {stop:.8g} 已被现价 {mark:.8g} "
                "越过(移动防线/锁利错过)→ 市价离场,不挂即刻触发的计划单"
            )
            acted += 1
        self.counters["gap_recoveries"] = self.counters.get("gap_recoveries", 0) + 1
        self._journal(
            "gap_recovery",
            {
                "gap_hours": round(gap_s / 3600, 3),
                "stop_hits": sorted(self._gap_stop_hit),
                "actions": acted,
                "stop_policy": stop_policy,
                "reduce_policy": str(gcfg.get("reduce_policy", "market")),
            },
        )
        if acted == 0:
            self.tg.send_alert(
                f"{marker} 补做完成:缺口内的防线推进/减仓已按 canonical 重放,无止损越界需追平"
            )

    def _gap_market_exit(self, sym: str, pos: dict[str, Any], now_ms: int, reason: str) -> None:
        """撤掉入场/止损挂单后按现价市价离场(强制,不受速率限制约束)。"""
        qty = floor_step(float(pos["qty"]), self._spec(sym).size_step, self._spec(sym).size_place)
        self._cancel_entry(sym, "gap_exit")
        self._cancel_stop(sym, "gap_exit")
        self._arm_close_intent(sym, pos, qty, "market", None, reason, int(now_ms), role="exit", force=True)
        self.counters["gap_stop_exits"] = self.counters.get("gap_stop_exits", 0) + 1

    def _warmup_pending_symbols(self) -> None:
        if not self.symbols_pending:
            return
        pending_batch = list(self.symbols_pending[:_WARMUP_BATCH_LIMIT])
        want = int(self.cog_cfg["warmup_trigger_bars"])
        prefetched = self._prefetch_bars_parallel(pending_batch, want)
        for sym in pending_batch:
            try:
                bars2h = prefetched.get(sym) or self._fetch_trigger_bars(sym, want)
                cs = self._build_all_tf_campaigns(sym, bars2h=bars2h)
            except (BitgetError, RuntimeError) as exc:
                self._api_note(exc)
                self.tg.send_alert(
                    f"{sym} 认知帧装配失败:{type(exc).__name__} {str(exc)[:120]}(暂不交易)"
                )
                if sym in self.symbols_pending:
                    self.symbols_pending.remove(sym)
                continue
            if cs is not None:
                self.campaigns[sym] = cs
                self._touch_campaigns()
                if sym in self.positions or self.mode != "live":
                    self._ensure_leverage(sym)
            if sym in self.symbols_pending:
                self.symbols_pending.remove(sym)

    def _collect_due_symbols(
        self,
        now_ms: int,
        extra_by_sym: Mapping[str, list[CampaignState]],
    ) -> tuple[
        dict[str, list[CampaignState]],
        dict[str, list[CampaignState]],
        dict[str, int],
        dict[str, int],
        str,
    ]:
        base_ms = self._base_step_seconds() * 1000
        sub_gran, sub_sec = self._sub_gran_and_seconds()
        sub_ms = sub_sec * 1000
        sub_30m_by_sym: dict[str, list[CampaignState]] = {}
        base_2h_by_sym: dict[str, list[CampaignState]] = {}
        due_30m: dict[str, int] = {}
        due_base: dict[str, int] = {}
        for sym, cs in sorted(self.campaigns.items()):
            all_cs = [cs, *extra_by_sym.get(sym, ())]
            s_list = [c for c in all_cs if self._base_step_seconds(c.trig_bars) in (900, 1800)]
            b_list = [c for c in all_cs if self._base_step_seconds(c.trig_bars) == base_ms // 1000]
            if s_list:
                sub_30m_by_sym[sym] = s_list
                sub_last = min(sc.trig_bars[-1][0] for sc in s_list if sc.trig_bars)
                if sub_last > 0 and now_ms >= sub_last + 2 * sub_ms:
                    due_30m[sym] = sub_last + 1
            if b_list:
                base_2h_by_sym[sym] = b_list
                last_ts = min(bc.trig_bars[-1][0] for bc in b_list if bc.trig_bars)
                if last_ts == 0 or now_ms >= last_ts + 2 * base_ms:
                    due_base[sym] = last_ts + 1
        return sub_30m_by_sym, base_2h_by_sym, due_30m, due_base, sub_gran

    def _prefetch_due_bars(
        self,
        due_30m: Mapping[str, int],
        due_base: Mapping[str, int],
        sub_gran: str,
    ) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
        pref_30m: dict[str, list[Any]] = {}
        pref_base: dict[str, list[Any]] = {}
        if self.mode != "live" or (len(due_30m) + len(due_base)) <= 2:
            return pref_30m, pref_base
        gran_base = self.cog_cfg["granularity"]
        with ThreadPoolExecutor(max_workers=12) as ex:
            f30 = {
                ex.submit(self.client.history_candles, s, sub_gran, start_ms=st, limit=100): s
                for s, st in due_30m.items()
            }
            fb = {
                ex.submit(self.client.history_candles, s, gran_base, start_ms=st, limit=100): s
                for s, st in due_base.items()
            }
            for fut, s in f30.items():
                try:
                    pref_30m[s] = fut.result()
                except Exception as exc:
                    with suppress(OSError):
                        self._journal("prefetch_sub_error", {"symbol": s, "why": str(exc)[:120]})
            for fut, s in fb.items():
                try:
                    pref_base[s] = fut.result()
                except Exception as exc:
                    with suppress(OSError):
                        self._journal("prefetch_base_error", {"symbol": s, "why": str(exc)[:120]})
        return pref_30m, pref_base

    def _advance_symbol_series(
        self,
        sym: str,
        now_ms: int,
        cs_list: Sequence[CampaignState],
        *,
        due_map: Mapping[str, int],
        prefetched: Mapping[str, list[Any]],
        gran: str,
        is_base: bool,
        deferred_routes: list[tuple[Any, ...]],
    ) -> None:
        if sym not in due_map or not cs_list:
            return
        ordered = sorted(cs_list, key=lambda c: -int(c.obs_tf_seconds))
        new_bars = (
            prefetched[sym]
            if sym in prefetched
            else self.client.history_candles(sym, gran, start_ms=due_map[sym], limit=100)
        )
        if not new_bars:
            return
        for idx, cs_item in enumerate(ordered):
            self._advance(
                cs_item,
                now_ms,
                new_bars=new_bars,
                run_paper_fills=(is_base and idx == 0),
                defer_routes=deferred_routes,
            )
        if is_base:
            self._try_unlock_higher_tfs(sym, ordered[0].trig_bars)
            self._save_cached_bars(sym, ordered[0].trig_bars)
        else:
            self._save_cached_bars(sym, ordered[0].trig_bars, gran=gran)

    def _advance_phase1(
        self,
        now_ms: int,
        base_2h_by_sym: Mapping[str, list[CampaignState]],
        sub_30m_by_sym: Mapping[str, list[CampaignState]],
        due_base: Mapping[str, int],
        due_30m: Mapping[str, int],
        pref_base: Mapping[str, list[Any]],
        pref_30m: Mapping[str, list[Any]],
        sub_gran: str,
    ) -> list[tuple[Any, ...]]:
        deferred_routes: list[tuple[Any, ...]] = []
        ordered_campaigns = sorted(
            self.campaigns.items(),
            key=lambda kv: (
                -float(kv[1].detector.structure.variance_ratio(4)),
                -abs(float(kv[1].detector.structure.imbalance_strength())),
                kv[0],
            ),
        )
        base_gran = str(self.cog_cfg["granularity"])
        for sym, _cs in ordered_campaigns:
            try:
                self._advance_symbol_series(
                    sym, now_ms, base_2h_by_sym.get(sym) or (),
                    due_map=due_base, prefetched=pref_base, gran=base_gran,
                    is_base=True, deferred_routes=deferred_routes,
                )
                self._advance_symbol_series(
                    sym, now_ms, sub_30m_by_sym.get(sym) or (),
                    due_map=due_30m, prefetched=pref_30m, gran=sub_gran,
                    is_base=False, deferred_routes=deferred_routes,
                )
            except BitgetError as exc:
                self._api_note(exc)
                self.tg.send_alert(f"{sym} bar 推进异常:{str(exc)[:120]}")
        return deferred_routes

    def _dispatch_phase2(self, deferred_routes: Sequence[tuple[Any, ...]]) -> None:
        if not deferred_routes:
            return
        pending_entries: list[tuple[Any, ...]] = []
        for cs_d, atr_d, evs_d, view_d, mic_d, bar_d, bidx_d in deferred_routes:
            routed = self._evaluate_route(
                cs_d, atr_d, evs_d, view_d, mic=mic_d, trig_bar=bar_d, bar_index=bidx_d
            )
            for dec_tuple in routed:
                if dec_tuple[0].kind == "cancel_entry":
                    self._dispatch_route_decision(*dec_tuple)
                else:
                    pending_entries.append(dec_tuple)
        pending_entries.sort(
            key=lambda x: (
                -int(x[1].obs_tf_seconds),
                -float(x[0].meta.get("priority_score", 0.0)),
                x[1].symbol,
            )
        )
        for dec_tuple in pending_entries:
            self._dispatch_route_decision(*dec_tuple)

    def _step_inner(self, now_ms: int) -> None:
        if self.mode == "live":
            if self.price_clock == "realtime":
                self._sync_target_fills(now_ms)
                self._manage_targets(now_ms)
                self._poll_position_prices(now_ms)
                self._manage_stops(now_ms)
            bal = self.client.balance()
            self.equity = float(bal["equity"])
            self.unrealized_pl = float(bal.get("unrealized_pl") or 0.0)
        if self.equity is None:
            raise RuntimeError("equity 未定(live 取交易所权益;paper 需 equity0)")
        realized_cap = max(0.0, float(self.equity) - float(self.unrealized_pl or 0.0))
        if self.peak_equity is None or realized_cap > self.peak_equity:
            self.peak_equity = realized_cap
        self.breaker.update(now_ms, self.equity)
        self._universe_tick()
        self._warmup_pending_symbols()
        if self.mode == "live":
            self._retry_exits(now_ms)
            self._settle_exits(now_ms)
            self._settle_entries(now_ms)
            self._sync_live_fills(now_ms)
            if self.price_clock == "realtime":
                self._sync_target_fills(now_ms)
                self._manage_targets(now_ms)
            self._advance_price_clock(now_ms)
            self._poll_position_prices(now_ms)
            if self.price_clock == "realtime":
                self._manage_stops(now_ms)

        extra_by_sym: dict[str, list[CampaignState]] = defaultdict(list)
        for (s2, _tf), sub_cs in sorted(self.extra_campaigns.items()):
            extra_by_sym[s2].append(sub_cs)
        self._step_extra_by_sym = extra_by_sym
        self._step_peer_cache = {}
        try:
            sub_30m_by_sym, base_2h_by_sym, due_30m, due_base, sub_gran = (
                self._collect_due_symbols(now_ms, extra_by_sym)
            )
            pref_30m, pref_base = self._prefetch_due_bars(due_30m, due_base, sub_gran)
            deferred_routes = self._advance_phase1(
                now_ms, base_2h_by_sym, sub_30m_by_sym,
                due_base, due_30m, pref_base, pref_30m, sub_gran,
            )
            # Phase 1 可能解锁了新周期,刷新 step 索引供 Phase 2 使用
            if self._step_extra_by_sym is not None:
                refreshed: dict[str, list[CampaignState]] = defaultdict(list)
                for (s2, _tf), sub_cs in sorted(self.extra_campaigns.items()):
                    refreshed[s2].append(sub_cs)
                self._step_extra_by_sym = refreshed
            self._dispatch_phase2(deferred_routes)
        finally:
            self._step_extra_by_sym = None
            self._step_peer_cache = None

        self._expiry_sweep(now_ms)
        if self.mode == "live":
            if not self.api_halted:
                self._poll_watch_entries(now_ms)
            self._manage_stops(now_ms)
            if now_ms / 1000 - self._reconcile_at >= self.loop_cfg["reconcile_seconds"]:
                self._reconcile_at = now_ms / 1000
                self._reconcile()
        if now_ms / 1000.0 - self._prune_at >= _PRUNE_INTERVAL_S:
            self._prune_at = now_ms / 1000.0
            self._prune_runtime_state(now_ms)
        self._daily_tick(now_ms)
        self._api_error_streak = 0

    def _advance_price_clock(self, now_ms: int) -> None:
        """Closed 15m bars arm/tighten the target lock (no 2H wait)."""
        if self.price_clock != "15m" or not self.positions:
            return
        step = 15 * 60 * 1000
        for sym, pos in list(self.positions.items()):
            if pos.get("price_source", "ref") == "ref":
                continue
            cs = self._get_cs_for(sym, pos.get("tf_label"))
            pm_pos = cs.pm.positions.get(sym) if cs is not None else None
            if cs is None or pm_pos is None:
                continue
            last = int(self._price_last.get(sym, 0))
            if last <= 0 or now_ms - last > step * PRICE_CLOCK_MAX_CATCHUP_STEPS:
                last = initial_price_clock_ts(now_ms, step, int(pm_pos.opened_ts))
                self._price_last[sym] = last
            if now_ms < last + step:
                continue
            try:
                bars = self.client.history_candles(sym, "15m", start_ms=last + 1, limit=100)
            except BitgetError as exc:
                self._journal("price_bar_error", {"symbol": sym, "why": str(exc)[:160]})
                continue
            for bar in bars:
                ts = int(bar[0])
                if ts <= last or ts + step > now_ms:
                    continue
                last = ts
                if ts < int(pm_pos.opened_ts):
                    continue
                old = float(pm_pos.invalid)
                intents = cs.pm.on_bar(
                    sym, bar, float(cs.detector.structure.atr() or 0.0),
                    price_only=True, skip_trail=True,
                )
                if abs(float(pm_pos.invalid) - old) > 1e-12:
                    pos["invalid"] = float(pm_pos.invalid)
                    self._touch_campaigns()
                    self._journal(
                        "price_defense",
                        {
                            "symbol": sym, "clock": "15m", "bar_ts": ts,
                            "old_invalid": old, "invalid": float(pm_pos.invalid),
                            "armed": bool(pm_pos.armed), "peak": pm_pos._best,
                        },
                    )
                for intent in intents:
                    if getattr(intent.kind, "value", None) == "EXIT":
                        reason = str(intent.reason)
                        executor = (
                            "local_market"
                            if ("lock_giveback" in reason or "profit|" in reason or "§8.1" in reason)
                            else "exchange_stop"
                        )
                        ref_px = float(pos.get("mark") or bar[4])
                        self._sub_bar_close_out(
                            cs, pm_pos, pos, ts, ref_px,
                            why="price_bar_exit", route=reason, executor=executor,
                        )
                        break
                    self._exec_mgmt(cs, intent, bar)
            self._price_last[sym] = last

    def _parse_fresh_tick(
        self,
        tick: Mapping[str, Any],
        sym: str,
        pos: Mapping[str, Any],
        received: int,
        max_age: int,
    ) -> tuple[int, float, float] | None:
        try:
            observed = int(tick.get("ts") or 0)
            last, mark = float(tick["lastPr"]), float(tick["markPrice"])
        except (KeyError, ValueError, TypeError):
            return None
        if (
            observed <= self._price_seen.get(sym, 0)
            or observed < int(pos.get("opened_ts") or 0)
            or not (0 <= received - observed <= max_age)
            or not (math.isfinite(last) and last > 0 and math.isfinite(mark) and mark > 0)
        ):
            return None
        return observed, last, mark

    def _handle_realtime_position_tick(
        self,
        sym: str,
        cs: CampaignState,
        pm_pos: Position,
        pos: dict[str, Any],
        observed: int,
        last: float,
        mark: float,
        ref: float,
    ) -> bool:
        """Handle realtime-clock target/lock rules for one tick. Returns True if fully handled."""
        d = int(pos["direction"])
        if (
            not pm_pos.pending_reduce
            and not pm_pos.reduced
            and pm_pos.target_1 is not None
            and cs.pm.resolve_lock_frac(pm_pos) > 0
        ):
            atr = float(cs.detector.structure.atr() or 0.0)
            quote = (observed, last, last, last, last, 0.0)
            for intent in cs.pm.on_bar(sym, quote, atr, price_only=True, skip_trail=True):
                self._exec_mgmt(cs, intent, quote)
            pos["invalid"] = pm_pos.invalid
            self._touch_campaigns()
        if pm_pos.target_1 is not None:
            frac = cs.pm.resolve_lock_frac(pm_pos)
            if frac > 0 and d * (last - pm_pos.target_1) >= 0:
                pm_pos.armed = True
                self._touch_campaigns()
        if not (pm_pos.target_1 is not None and pm_pos.pending_reduce and not pm_pos.reduced):
            return False
        full_tp_resting = (
            bool(pos.get("target_order"))
            and (not getattr(cs.pm, "partial_exit_enabled", False))
            and cs.pm.resolve_lock_frac(pm_pos) >= 1.0 - 1e-9
        )
        if full_tp_resting:
            if d * (last - pm_pos.target_1) >= 0:
                pm_pos.armed = True
                self._reconcile_at = 0.0
                self._touch_campaigns()
            return True
        pm_pos._best = max(pm_pos._best, last) if d > 0 else min(pm_pos._best, last)
        lock = opp_lock_step(
            direction=d, entry=pm_pos.entry, target=pm_pos.target_1,
            frac=cs.pm.resolve_lock_frac(pm_pos), peak=pm_pos._best,
            high=last, low=last, close=last, ref_px=ref, armed=pm_pos.armed,
        )
        if lock.action == "tighten":
            pm_pos.invalid = (
                max(pm_pos.invalid, lock.stop_px) if d > 0 else min(pm_pos.invalid, lock.stop_px)
            )
            pos["invalid"] = pm_pos.invalid
            self._touch_campaigns()
        elif lock.action == "exit":
            self._sub_bar_close_out(
                cs, pm_pos, pos, observed, ref,
                why="price_lock_exhausted", route="PRICE_LOCK|giveback_exhausted",
                executor="local_market",
            )
        return True

    def _poll_position_prices(self, now_ms: int) -> None:
        """Fresh quotes ratchet an existing defense between 15m closes."""
        if self.price_clock not in ("15m", "realtime") or not self.positions:
            return
        if not hasattr(self.client, "tickers"):
            return
        try:
            ticks = self.client.tickers()
        except BitgetError as exc:
            self._journal("price_poll_error", {"why": str(exc)[:160]})
            self._alert_throttled(
                "price_poll", "持仓实时行情获取失败，保留交易所保护单；本轮不推进峰值"
            )
            return
        received = max(now_ms, int(time.time() * 1000))
        max_age = int(
            float((self.cfg.get("protection") or {}).get("max_quote_age_seconds", 30)) * 1000
        )
        for tick in ticks:
            sym = str(tick.get("symbol") or "")
            pos = self.positions.get(sym)
            if pos is None or sym in self._exit_retry:
                continue
            parsed = self._parse_fresh_tick(tick, sym, pos, received, max_age)
            if parsed is None:
                continue
            observed, last, mark = parsed
            self._price_seen[sym] = observed
            pos["mark"], pos["mark_ts"] = mark, observed
            cs = self._get_cs_for(sym, pos.get("tf_label"))
            pm_pos = cs.pm.positions.get(sym) if cs is not None else None
            if cs is None or pm_pos is None:
                continue
            d = int(pos["direction"])
            if d * (mark - float(pm_pos.invalid)) <= 0:
                self._sub_bar_close_out(
                    cs, pm_pos, pos, observed, mark,
                    why="price_protective_stop", route="PRICE_STOP|fresh mark",
                    executor="exchange_stop",
                )
                continue
            if self.price_clock != "realtime" and pos.get("price_source", "ref") == "ref":
                continue
            old, armed = float(pm_pos.invalid), bool(pm_pos.armed)
            ref = min(last, mark) if d > 0 else max(last, mark)
            if self.price_clock == "realtime" and self._handle_realtime_position_tick(
                sym, cs, pm_pos, pos, observed, last, mark, ref
            ):
                continue
            intents = cs.pm.on_price(
                sym, ts=observed, price=last, ref_price=ref,
                atr=float(cs.detector.structure.atr() or 0.0),
            )
            if float(pm_pos.invalid) != old or bool(pm_pos.armed) != armed:
                pos["invalid"] = float(pm_pos.invalid)
                self._touch_campaigns()
                self._journal(
                    "price_defense",
                    {
                        "symbol": sym, "quote_ts": observed, "last": last,
                        "mark": mark, "clock": self.price_clock,
                        "old_invalid": old, "invalid": float(pm_pos.invalid),
                        "armed": bool(pm_pos.armed), "peak": pm_pos._best,
                    },
                )
            quote_bar = (observed, last, last, last, last, 0.0)
            for intent in intents:
                if getattr(intent.kind, "value", None) == "EXIT":
                    self._sub_bar_close_out(
                        cs, pm_pos, pos, observed, ref,
                        why="price_lock_exhausted", route=intent.reason,
                        executor="local_market",
                    )
                else:
                    self._exec_mgmt(cs, intent, quote_bar)

    def _api_note(self, exc: Exception) -> None:
        """API 错计数与熔断(fail-closed)。"""
        self._api_error_streak += 1
        if self._api_error_streak >= int(self.loop_cfg["api_error_max"]):
            self.api_halted = True
            if not self._api_halt_alerted:
                self._api_halt_alerted = True
                self.tg.send_alert(
                    f"API 错熔断:连续 {self._api_error_streak} 次接口异常"
                    f"({type(exc).__name__}),新增风险已暂停(既有仓位照常管理)"
                    ",人工核查后于跨 UTC 日自动复位"
                )
            raise exc

    def _prune_runtime_state(self, now_ms: int) -> None:
        """定期清理已终结的历史意图与过期告警限频键,防止长跑内存与 state.json 无限增长。"""
        cutoff_ms = int(now_ms) - _PRUNE_AGE_MS
        terminal = ("filled", "expired", "canceled", "error", "void")
        for oid, it in list(self.open_intents.items()):
            if it.get("status") not in terminal:
                continue
            ref_ts = max(
                int(it.get("fill_ts") or 0),
                int(it.get("expiry_ts") or 0),
                int(it.get("decision_ts") or 0),
            )
            if 0 < ref_ts < cutoff_ms:
                self.open_intents.pop(oid, None)
        cutoff_s = now_ms / 1000.0 - _PRUNE_AGE_S
        for k, ts_s in list(self._alert_last.items()):
            if float(ts_s) < cutoff_s:
                self._alert_last.pop(k, None)

    def _universe_tick(self) -> None:
        if not self.universe_enabled:
            return
        now = time.time()
        if now - self._universe_at < self.loop_cfg["universe_refresh_seconds"]:
            return
        self._universe_at = now
        uni_cfg = self.cfg["universe"]
        uni = self.client.universe(
            uni_cfg,
            contracts=list(self.contract_raw.values()) if self.contract_raw else None,
        )
        self._journal("universe", {"config": uni_cfg, "n": len(uni), "head": uni[:8]})
        self._universe_symbols = set(uni)
        self._manage_only = {sym for sym in self.positions if sym not in self._universe_symbols}
        if self._universe_symbols:
            evicted: list[str] = []
            for sym in list(self.campaigns):
                if (
                    sym not in self._universe_symbols
                    and sym not in self.positions
                    and sym not in self._entry_settle
                    and sym not in self._exit_settle
                    and sym not in self._exit_retry
                    and not self._entry_resting(sym)
                ):
                    self.campaigns.pop(sym, None)
                    for ek in [k for k in self.extra_campaigns if k[0] == sym]:
                        self.extra_campaigns.pop(ek, None)
                    evicted.append(sym)
            if evicted:
                self._touch_campaigns()
                self._journal("universe_evict", {"n": len(evicted), "symbols": evicted[:20]})
        known = set(self.campaigns) | set(self.symbols_pending)
        missing_specs = [sym for sym in uni if sym not in self.contracts]
        if missing_specs and hasattr(self.client, "contracts"):
            try:
                fresh_specs, fresh_raw = load_contracts_live(self.client, missing_specs)
                self.contracts.update(fresh_specs)
                self.contract_raw.update(fresh_raw)
            except (BitgetError, OSError, ValueError) as exc:
                self._journal("contracts_refresh_error", {"why": str(exc)[:160]})
        for sym in uni:
            if sym not in known:
                self.symbols_pending.append(sym)
                self._journal("universe_add", {"symbol": sym})

    # ------------------------------------------------------------------
    # 增量推进:开盘撮合① 先于 mic 步⑤' 先于认知收盘⑤
    # ------------------------------------------------------------------

    def _handle_sequence_gap(self, cs: CampaignState, bar: Sequence[Any], base_ms: int) -> None:
        self.tg.send_alert(
            f"{cs.symbol}({cs.tf_label}) 序列缺口 {cs.trig_bars[-1][0]} → "
            f"{bar[0]};认知帧重装配(持仓事实保留)"
        )
        sub_trig: list[Any] | None = None
        if base_ms in (900_000, 1_800_000) and self.mode == "live" and hasattr(self.client, "history_candles"):
            s_gran = "15m" if base_ms == 900_000 else "30m"
            s_sec = base_ms // 1000
            sub_want = max(520, (self._min_cog_bars_for_tf() + 5) * 8)
            cand_sub = self._fetch_trigger_bars(cs.symbol, sub_want, gran=s_gran)
            if self._base_step_seconds(cand_sub, gran=s_gran) == s_sec:
                sub_trig = cand_sub
        fresh = self._build_campaign(cs.symbol, cs.tf_label, trig_bars=sub_trig)
        if fresh is None and sub_trig is not None:
            fresh = self._build_campaign(cs.symbol, cs.tf_label)
        if fresh is not None:
            fresh.pm.positions = cs.pm.positions
            if (cs.symbol, cs.tf_label) in self.extra_campaigns:
                self.extra_campaigns[(cs.symbol, cs.tf_label)] = fresh
            else:
                self.campaigns[cs.symbol] = fresh
            self._touch_campaigns()

    def _advance_trigger_tracker(
        self,
        cs: CampaignState,
        bar: Sequence[Any],
        *,
        base_ms: int,
        obs_ms: int,
        trig_ms: int,
        trig_scale: int,
        defer_routes: list[tuple[Any, ...]] | None,
    ) -> None:
        if cs.tracker is None or trig_ms >= obs_ms:
            return
        if (bar[0] + base_ms) % trig_ms != 0 or len(cs.trig_bars) < trig_scale:
            return
        t_bar = (
            bar
            if trig_scale == 1
            else aggregate_bucket(cs.trig_bars[-trig_scale:], bar[0] + base_ms - trig_ms)
        )
        emitted = cs.tracker.update(t_bar)
        pm_inst = getattr(cs, "pm", None)
        pos_pm = pm_inst.positions.get(cs.symbol) if pm_inst is not None else None
        pos_dict = self.positions.get(cs.symbol)
        if (
            pm_inst is not None
            and pos_pm is not None
            and pos_dict is not None
            and pos_dict.get("tf_label", cs.tf_label) == cs.tf_label
        ):
            for intent in pm_inst.on_trigger_bar(cs.symbol, t_bar, emitted or (), tf_label=cs.tf_label):
                self._exec_mgmt(cs, intent, t_bar)
        if not emitted:
            return
        view = cs.tracker.current_view(tf_hours=int(obs_ms // 3_600_000))
        if defer_routes is not None:
            defer_routes.append((
                cs, cs.tracker.frozen_atr, list(emitted), view, True, t_bar, len(cs.trig_bars) - 1,
            ))
        else:
            self._route_step(cs, cs.tracker.frozen_atr, emitted, view, mic=True, trig_bar=t_bar)

    def _advance(
        self,
        cs: CampaignState,
        now_ms: int,
        new_bars: list[Any] | None = None,
        run_paper_fills: bool = True,
        defer_routes: list[tuple[Any, ...]] | None = None,
    ) -> None:
        last = cs.trig_bars[-1][0] if cs.trig_bars else 0
        if new_bars is None:
            new_bars = self.client.history_candles(
                cs.symbol, self.cog_cfg["granularity"], start_ms=last + 1, limit=100
            )
        base_ms = self._base_step_seconds(cs.trig_bars or new_bars) * 1000
        trig_ms = max(base_ms, int(cs.trigger_tf_seconds) * 1000)
        obs_ms = max(base_ms, int(cs.obs_tf_seconds) * 1000)
        trig_scale = max(1, trig_ms // base_ms)
        scale = max(1, obs_ms // base_ms)
        for bar in new_bars:
            if bar[0] <= last:
                continue
            last = bar[0]
            if cs.trig_bars and bar[0] - cs.trig_bars[-1][0] != base_ms:
                self._handle_sequence_gap(cs, bar, base_ms)
                return
            if self.mode == "paper" and run_paper_fills:
                self._paper_fill_pass(cs.symbol, bar)
            cs.trig_bars.append(bar)
            self._touch_campaigns()
            for pos in self.positions.values():
                if pos["symbol"] == cs.symbol:
                    pos["mark"] = float(bar[4])
            self._on_sub_bar(cs, bar)
            if (bar[0] + base_ms) % obs_ms == 0 and len(cs.trig_bars) >= scale:
                cog_bar = (
                    bar
                    if scale == 1
                    else aggregate_bucket(cs.trig_bars[-scale:], bar[0] + base_ms - obs_ms)
                )
                self._close_cognition(cs, cog_bar, defer_routes=defer_routes)
            self._advance_trigger_tracker(
                cs, bar,
                base_ms=base_ms, obs_ms=obs_ms, trig_ms=trig_ms,
                trig_scale=trig_scale, defer_routes=defer_routes,
            )

    def _stop_is_resting(self, sym: str, trigger: float) -> bool:
        """防线上是否**确有**交易所保护单在挂(本地记录 + 触发价同档)。

        用途:决定固定止损被触及时"谁来执行"。本地记录只证明"我们挂过",
        这里要求 order_id 在册且触发价与当前防线同档(一个价格档内),才承认
        交易所是执行者;否则就是"没人会执行",必须本进程补市价单。
        """
        cur = self.stops.get(sym)
        if not cur or not cur.get("order_id"):
            return False
        try:
            placed = float(cur.get("trigger"))
        except (TypeError, ValueError):
            return False
        tick = 10.0 ** (-int(self._spec(sym).price_place))
        # 容差 = 1.5 个价格档:挂单价经过 `fit_price` 取整(多头向下、空头向上),
        # 与 invalid 的差值必然 < 1 档;超过 1.5 档就是"另一条防线"(旧单),
        # 不能拿它冒充当前防线的执行者。
        return abs(placed - float(trigger)) <= tick * 1.5 + 1e-12

    def _sub_bar_close_out(
        self,
        cs: CampaignState,
        pos_pm: Position,
        pos_dict: dict[str, Any],
        ts: int,
        ref_px: float,
        *,
        why: str,
        route: str,
        executor: str,
    ) -> None:
        """子周期触价离场的统一出口(paper 记账 / live 按执行者分流)。"""
        sym = cs.symbol
        if self.mode == "paper":
            self._cancel_entry(sym, why)
            self._cancel_stop(sym, why)
            for _oid, it in list(self.open_intents.items()):
                if it["symbol"] == sym and it["status"] == "armed":
                    it["status"] = "canceled"
            cs.pm.positions.pop(sym, None)
            stop_oid = client_oid_of(
                self.OID_PREFIX, sym, str(pos_dict.get("event_id", "STOP")),
                "stop", ts, role="exit",
            )[:36]
            stop_it = {
                "oid": stop_oid,
                "symbol": sym,
                "role": "exit",
                "kind": "market",
                "direction": -int(pos_dict["direction"]),
                "qty": float(pos_dict["qty"]),
                "route": route,
                "status": "armed",
            }
            self._paper_close_fill(sym, stop_it, float(ref_px), ts)
            return
        if self._gap_replay:
            self._gap_stop_hit[sym] = {"ts": ts, "stop": float(ref_px), "bar_close": float(ref_px)}
            return
        if executor == "exchange_stop" and self._stop_is_resting(sym, float(pos_pm.invalid)):
            self._reconcile()
            return
        if executor == "exchange_stop":
            self.counters["unprotected_stop_exits"] = self.counters.get("unprotected_stop_exits", 0) + 1
            self._journal(
                "stop_unprotected_exit",
                {
                    "symbol": sym,
                    "invalid": float(pos_pm.invalid),
                    "qty": pos_dict.get("qty"),
                    "ref_px": float(ref_px),
                    "why": why,
                },
            )
            self._alert_throttled(
                f"unprot:{sym}",
                f"{sym} 防线 {float(pos_pm.invalid):.8g} 已触及但交易所无保护单在挂"
                " → 本进程市价离场(修前该情形无任何动作)",
            )
            why = f"{why}_unprotected"
        self._live_market_exit(pos_dict, ts=ts, why=why, route=route, ref_px=ref_px)

    def _handle_market_exit_error(
        self,
        sym: str,
        pos: dict[str, Any],
        oid: str,
        why: str,
        route: str,
        ref_px: float,
        exc: BitgetError,
    ) -> bool:
        why_err = str(exc)
        if "22002" in why_err or "暂无仓位可平" in why_err or "43023" in why_err:
            ex_qty = self._exchange_position_qty(sym, int(pos["direction"]))
            if ex_qty is None or ex_qty <= 0.0:
                self.counters["close_no_pos"] = self.counters.get("close_no_pos", 0) + 1
                self._journal(
                    "close_no_pos",
                    {"symbol": sym, "role": "exit", "why": why_err[:160], "why_action": why},
                )
                self.stops.pop(sym, None)
                self._exit_retry.pop(sym, None)
                self._reconcile_closed(sym, dict(pos))
                return True
        self.counters["exit_submit_errors"] = self.counters.get("exit_submit_errors", 0) + 1
        self._journal(
            "exit_submit_error",
            {"symbol": sym, "oid": oid, "why": why_err[:160], "why_action": why},
        )
        tries = int((self._exit_retry.get(sym) or {}).get("attempts", 0))
        self._exit_retry[sym] = {
            "why": why,
            "route": route,
            "ref_px": float(ref_px),
            "attempts": tries,
        }
        if tries == 0:
            self._alert_throttled(
                f"exitfail:{sym}",
                f"{sym} 决策型离场下单失败:{why_err[:120]}"
                f"(仓位仍在;保护单已撤,每步自动重试 ≤{self.MAX_EXIT_SUBMIT_RETRIES} 次,人工核查)",
                gap_s=300.0,
            )
        self._api_note(exc)
        return False

    def _live_market_exit(
        self,
        pos: dict[str, Any],
        *,
        ts: int,
        why: str,
        route: str,
        ref_px: float,
    ) -> bool:
        """决策型离场:由本进程自己发市价单(不占订单配额,成交即记账 + 回执校正)。"""
        sym = str(pos["symbol"])
        if sym in self._exit_settle:
            return False
        cur = self.positions.get(sym)
        if cur is None or float(cur.get("qty") or 0.0) <= 1e-9:
            return False
        pos = cur
        spec = self._spec(sym)
        qty = floor_step(float(pos["qty"]), spec.size_step, spec.size_place)
        if qty <= 0.0:
            self.counters["exit_submit_errors"] = self.counters.get("exit_submit_errors", 0) + 1
            self._journal(
                "exit_submit_error",
                {"symbol": sym, "why": f"qty<=0 after floor_step({spec.size_step})"},
            )
            self.tg.send_alert(
                f"{sym} 决策型离场数量按合约精度取整为 0(step={spec.size_step}) → 无法下单,人工核查"
            )
            return False
        side = self.close_side_of(int(pos["direction"]))
        oid = client_oid_of(
            self.OID_PREFIX, sym, str(pos.get("event_id") or "STOP"),
            side, int(ts), role="exit",
        )[:36]
        self._cancel_entry(sym, why)
        self._cancel_stop(sym, why)
        it: dict[str, Any] = {
            "oid": oid, "symbol": sym, "role": "exit", "kind": "market",
            "direction": -int(pos["direction"]), "qty": qty,
            "limit_price": None, "invalid": pos["invalid"],
            "target_1": pos.get("target_1"), "event_id": pos.get("event_id"),
            "route": f"exit:{str(why)[:32]}", "decision_ts": int(ts),
            "expiry_ts": int(ts) + N_FILL_H * MS_1H,
            "status": "armed", "exchange_order_id": None,
        }
        try:
            resp = self.client.place_market_order(
                sym, side=side, size=qty, client_oid=oid, trade_side=self.close_trade_side()
            )
            it["exchange_order_id"] = str((resp or {}).get("orderId") or "")
            self.counters["orders_placed"] += 1
            req_ms = int(time.time() * 1000)
            fact = self._close_fill_fact(sym, since_ms=req_ms - 2000)
            if fact is not None:
                px, px_src = float(fact[0]), "fills"
            else:
                px, px_src = float(ref_px), "ref"
                self._exit_settle[sym] = {
                    "oid": oid, "since_ms": req_ms - 2000,
                    "deadline_ms": req_ms + _SETTLE_DEADLINE_MS,
                    "ref_px": float(ref_px), "direction": int(pos["direction"]),
                    "qty": float(qty), "entry": float(pos["entry"]), "why": str(why),
                }
            it["price_source"] = px_src
            pnl = realized_pnl(
                int(pos["direction"]), qty, float(pos["entry"]), px, self.risk["taker_fee"]
            )
            self.counters["lock_exits"] = self.counters.get("lock_exits", 0) + 1
            self._exit_retry.pop(sym, None)
            self._journal(
                "exit_submit",
                {
                    "symbol": sym, "oid": oid, "exchange_order_id": it["exchange_order_id"],
                    "qty": qty, "why": why, "ref_px": float(ref_px),
                    "px": px, "price_source": px_src, "pnl": pnl, "route": route[:80],
                },
            )
            self._live_close_filled(
                it,
                ref_price=px,
                pnl=pnl,
                title=exit_label(executor="local_market", pnl=pnl, reason=why),
                note=f"{why} · {route}",
                price_label="成交价" if px_src == "fills" else "参考价(下单时,回执待校正)",
            )
            return True
        except BitgetError as exc:
            it["status"] = "error"
            return self._handle_market_exit_error(sym, pos, oid, why, route, ref_px, exc)

    def _retry_exits(self, now_ms: int) -> None:
        """决策型离场下单失败的逐步重试(不依赖可能过期的 mark)。"""
        for sym, rec in list(self._exit_retry.items()):
            pos = self.positions.get(sym)
            if pos is None:
                self._exit_retry.pop(sym, None)
                continue
            ref = float(pos.get("mark") or rec.get("ref_px") or 0.0)
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            ok = self._live_market_exit(
                pos,
                ts=int(now_ms),
                why=str(rec.get("why")),
                route=str(rec.get("route")),
                ref_px=ref or float(pos["entry"]),
            )
            if ok:
                self._exit_retry.pop(sym, None)
                continue
            if int(rec["attempts"]) >= self.MAX_EXIT_SUBMIT_RETRIES:
                self._exit_retry.pop(sym, None)
                self._journal(
                    "exit_submit_giveup",
                    {"symbol": sym, "attempts": rec.get("attempts"), "why": rec.get("why")},
                )
                self.tg.send_alert(
                    f"{sym} 决策型离场重试 {rec.get('attempts')} 次仍失败"
                    f"(原因 {rec.get('why')}) —— 已停止自动重试,仓位仍在,请立即人工平仓/核查"
                )

    def _defense_title(self, pos_pm: Position, last_log: str) -> str:
        """防线推进通知的语义标题。"""
        d = 1 if int(pos_pm.direction) > 0 else -1
        entry = float(pos_pm.entry)
        stop = float(pos_pm.invalid)
        if "opp_lock" in last_log:
            tgt = pos_pm.target_1
            span = abs(float(tgt) - entry) if tgt is not None else 0.0
            frac = (d * (stop - entry) / span) if span > 0 else 0.0
            return (
                f"锁利推进(已锁 {frac:.0%} TP1 行程)"
                if frac > 0
                else "锁利推进(防线仍在成本侧)"
            )
        if "partial_exit" in last_log:
            return "减仓50% · 余仓保本防线"
        if "§8.3|trail" in last_log:
            return "结构防线推进(§8.3 trail)"
        if d * (stop - entry) > 0:
            return "防线推进(已进入盈利区)"
        return "防线收紧"

    def _notify_defense_advance(
        self,
        cs: CampaignState,
        pos_pm: Position,
        pos_dict: dict[str, Any] | None,
        old_inv: float,
        ref_close: float,
        *,
        default_log: str,
        price_label: str,
        bars: Sequence[Any],
        pivots: list[Any] | None = None,
        atr: float | None = None,
    ) -> None:
        last_log = pos_pm.log[-1] if pos_pm.log else default_log
        kwargs: dict[str, Any] = {
            "note": f"{old_inv:.6g} -> {pos_pm.invalid:.6g} ({last_log[:48]})",
            "tf_label": cs.tf_label,
            "direction": pos_pm.direction,
            "entry_price": float(pos_dict["entry"]) if pos_dict else None,
            "price_label": price_label,
            "stop_price": float(pos_pm.invalid),
            "target_price": pos_pm.target_1,
            "box_lo": pos_dict.get("box_lo") if pos_dict else None,
            "box_hi": pos_dict.get("box_hi") if pos_dict else None,
            "box_start_ts": pos_dict.get("box_start_ts") if pos_dict else None,
            "bars": bars,
        }
        if pivots is not None:
            kwargs["pivots"] = pivots
        if atr is not None:
            kwargs["atr"] = atr
        self.tg.send_fill(
            cs.symbol,
            f"{self._defense_title(pos_pm, last_log)}({self.mode})",
            float(pos_dict["qty"]) if pos_dict else 0.0,
            ref_close,
            **kwargs,
        )

    def _apply_opp_lock_decision(
        self,
        cs: CampaignState,
        pos_pm: Position,
        pos_dict: dict[str, Any],
        bar: Sequence[Any],
        *,
        ts: int,
        h: float,
        low: float,
        c: float,
        ref_px: float,
        target: float,
        frac_cfg: float,
        old_inv: float,
    ) -> bool:
        """执行子周期对侧锁利三态裁决;返回 True 表示已离场/减仓无需后续通知。"""
        reached = (low <= target) if pos_pm.direction < 0 else (h >= target)
        if (
            reached
            and pos_pm.direction * (target - pos_pm.entry) > 0.0
            and getattr(pos_pm, "claim_completed_ts", None) is None
        ):
            pos_pm.claim_completed_ts = ts
            pos_pm.log.append(
                f"claim|completed|target={target:.8g} reached at {ts};"
                " recorded as fact only (risk re-base requires acceptance close)"
            )
        ld = opp_lock_step(
            direction=pos_pm.direction, entry=pos_pm.entry, target=target,
            frac=frac_cfg, peak=pos_pm._best, high=h, low=low, close=c,
            ref_px=ref_px, armed=pos_pm.armed,
        )
        if ld.action == "off":
            if not pos_pm.lock_off_logged:
                pos_pm.lock_off_logged = True
                pos_pm.log.append(
                    f"v21|sub_bar_opp_lock_off|{ld.reason}|"
                    f"entry={pos_pm.entry:.8g} target={target:.8g} "
                    f"frac={frac_cfg:+.2f} ⇒ 不锁利、不减仓,保持结构防线 invalid={old_inv:.8g}"
                )
            return False
        if ld.action == "exit":
            pos_pm.armed = True
            if getattr(cs.pm, "partial_exit_enabled", False) and not pos_pm.reduced:
                pos_pm.log.append(
                    f"v21|sub_bar_partial_exit|峰值={pos_pm._best:.8g} "
                    f"回吐={ld.drawdown:.8g} ≥ 预算={ld.budget:.8g} "
                    f"ref={ref_px:.8g} ⇒ 减仓50%并保本(余仓交棘轮)"
                )
                intent = cs.pm.reduce_position(pos_pm, ts, c, ld.drawdown)
                pos_dict["invalid"] = float(pos_pm.invalid)
                self._touch_campaigns()
                self._exec_mgmt(cs, intent, bar)
                return True
            pos_pm.log.append(
                f"v21|sub_bar_lock_giveback_exhausted|"
                f"峰值={pos_pm._best:.8g} 回吐={ld.drawdown:.8g} "
                f"≥ 预算={ld.budget:.8g} ref={ref_px:.8g} ⇒ 市价离场"
            )
            self._journal(
                "lock_exhausted",
                {
                    "symbol": cs.symbol, "peak": pos_pm._best,
                    "giveback": ld.drawdown, "budget": ld.budget,
                    "ref_px": ref_px, "invalid": old_inv,
                    "direction": int(pos_pm.direction),
                },
            )
            self._sub_bar_close_out(
                cs, pos_pm, pos_dict, ts, ref_px,
                why="sub_bar_lock_exhausted",
                route=f"LOCK_EXIT|giveback={ld.drawdown:.8g} budget={ld.budget:.8g} ref={ref_px:.8g}",
                executor="local_market",
            )
            return True
        if ld.armed and not pos_pm.armed:
            pos_pm.armed = True
            pos_pm.log.append(
                f"v21|sub_bar_opp_lock_armed|"
                f"{'收盘突破' if frac_cfg < 0 else '触及'}对侧缘 "
                f"target={target:.8g} 预算={(1.0 - abs(frac_cfg)):.0%}·span "
                "⇒ 此后按峰值棘轮(可多次)"
            )
            if getattr(cs.pm, "partial_exit_enabled", False) and not pos_pm.reduced:
                pos_pm.log.append(
                    f"v21|sub_bar_partial_exit_on_arm|"
                    f"target={target:.8g} ⇒ 减仓50%并移至保本,余仓交由棘轮/结构防线"
                )
                intent = cs.pm.reduce_position(pos_pm, ts, c, pos_pm._best)
                pos_dict["invalid"] = float(pos_pm.invalid)
                self._touch_campaigns()
                self._exec_mgmt(cs, intent, bar)
        if ld.action == "tighten":
            stop_px = float(ld.stop_px)
            pos_pm.invalid = (
                min(pos_pm.invalid, stop_px) if pos_pm.direction < 0 else max(pos_pm.invalid, stop_px)
            )
            if abs(pos_pm.invalid - old_inv) > 1e-12:
                pos_pm.log.append(
                    f"v21|sub_bar_opp_lock|峰值={pos_pm._best:.8g} "
                    f"预算={ld.budget:.8g} 回吐={ld.drawdown:.8g} "
                    f"ref={ref_px:.8g} invalid {old_inv:.8g}->{pos_pm.invalid:.8g}"
                )
        return False

    def _on_sub_bar(self, cs: CampaignState, bar: Sequence[Any]) -> None:
        """持仓期间子周期触价同步与结算(固定 STOP + 对侧锁利 / 传统减仓)。"""
        sym = cs.symbol
        pos_pm = cs.pm.positions.get(sym)
        pos_dict = self.positions.get(sym)
        if pos_pm is None or pos_dict is None:
            return
        if pos_dict.get("tf_label", cs.tf_label) != cs.tf_label:
            return
        ts, o, h, low, c = int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
        if int(pos_pm.opened_ts) > ts:
            return

        pos_pm._best = min(pos_pm._best, low) if pos_pm.direction < 0 else max(pos_pm._best, h)

        stop = float(pos_pm.invalid)
        breached = (low <= stop) if pos_pm.direction > 0 else (h >= stop)
        if breached:
            ref_px = min(o, stop) if pos_pm.direction > 0 else max(o, stop)
            self._sub_bar_close_out(
                cs, pos_pm, pos_dict, ts, ref_px,
                why="sub_bar_protective_stop",
                route=f"STOP|sub_bar trigger={stop:.8g}",
                executor="exchange_stop",
            )
            return

        if not (self.price_clock == "bar" and pos_pm.target_1 is not None and not pos_pm.pending_reduce):
            return
        target = float(pos_pm.target_1)
        try:
            atr_now = float(cs.detector.structure.atr() or 0.0)
        except (AttributeError, TypeError, ValueError):
            atr_now = 0.0
        frac_cfg = float(cs.pm.resolve_lock_frac(pos_pm, atr_now))
        old_inv = float(pos_pm.invalid)
        ref_px = c
        mark = float(pos_dict.get("mark") or 0.0)
        if mark > 0.0:
            ref_px = max(c, mark) if pos_pm.direction < 0 else min(c, mark)
        if frac_cfg == 0.0:
            hit = (low <= target) if pos_pm.direction < 0 else (h >= target)
            if hit and not pos_pm.reduced:
                extreme = low if pos_pm.direction < 0 else h
                self._exec_mgmt(cs, cs.pm.reduce_position(pos_pm, ts, c, extreme), bar)
        elif self._apply_opp_lock_decision(
            cs, pos_pm, pos_dict, bar,
            ts=ts, h=h, low=low, c=c, ref_px=ref_px,
            target=target, frac_cfg=frac_cfg, old_inv=old_inv,
        ):
            return

        if abs(float(pos_pm.invalid) - old_inv) > 1e-12:
            pos_dict["invalid"] = float(pos_pm.invalid)
            self._touch_campaigns()
            self._notify_defense_advance(
                cs, pos_pm, pos_dict, old_inv, c,
                default_log="子周期防线锁利",
                price_label=f"参考价({self._bar_tf_label(int(getattr(cs, 'trigger_tf_seconds', 0) or 0))}收盘)",
                bars=cs.trig_bars[-_CHART_BAR_WINDOW:] if cs.trig_bars else cs.cog_bars[-_CHART_BAR_WINDOW:],
            )

    def _update_position_on_cognition(
        self,
        cs: CampaignState,
        pos_pm: Position,
        cog_bar: Sequence[Any],
        atr: float,
        new_pivots: list[Any],
        emitted: list[Any],
        base_s: int,
        obs_s: int,
    ) -> None:
        sym = cs.symbol
        old_inv = float(pos_pm.invalid)
        if pos_pm.opened_ts > cog_bar[0]:
            scale_n = max(1, obs_s // base_s)
            sub_bars = [b for b in cs.trig_bars[-scale_n:] if int(b[0]) >= int(pos_pm.opened_ts)]
            if sub_bars:
                sub_cog = (
                    cog_bar[0], sub_bars[0][1],
                    max(b[2] for b in sub_bars), min(b[3] for b in sub_bars),
                    sub_bars[-1][4], sum(b[5] for b in sub_bars),
                )
                mgmt = cs.pm.on_bar(sym, sub_cog, atr, (), [], price_only=True)
            else:
                mgmt = []
        else:
            mgmt = cs.pm.on_bar(sym, cog_bar, atr, new_pivots, emitted or [])
        if abs(float(pos_pm.invalid) - old_inv) > 1e-12:
            pos_dict = self.positions.get(sym)
            self._touch_campaigns()
            if pos_dict is not None:
                pos_dict["invalid"] = float(pos_pm.invalid)
            self._notify_defense_advance(
                cs, pos_pm, pos_dict, old_inv, float(cog_bar[4]),
                default_log="防线收紧",
                price_label=f"参考价({cs.tf_label}收盘)",
                bars=cs.cog_bars[-_CHART_BAR_WINDOW:] if cs.cog_bars else cs.trig_bars[-_CHART_BAR_WINDOW:],
                pivots=self._chart_pivots(cs.detector.structure.pivots[-6:]),
                atr=atr,
            )
        for intent in mgmt:
            self._exec_mgmt(cs, intent, cog_bar)

    def _close_cognition(
        self,
        cs: CampaignState,
        cog_bar: Sequence[Any],
        defer_routes: list[tuple[Any, ...]] | None = None,
    ) -> None:
        s = cs.detector.structure
        n_pivots_before = len(s.pivots)
        emitted = cs.detector.update(cog_bar)
        new_pivots = list(s.pivots[n_pivots_before:])
        base_s = self._base_step_seconds(cs.trig_bars) if len(cs.trig_bars) >= 2 else 7200
        obs_s = max(base_s, int(cs.obs_tf_seconds))
        if cs.tracker is not None:
            cs.tracker.sync_from_structure(s)
        cs.cog_bars.append(cog_bar)
        atr = s.atr()
        pos_pm = cs.pm.positions.get(cs.symbol)
        if pos_pm is not None:
            self._update_position_on_cognition(
                cs, pos_pm, cog_bar, atr, new_pivots, emitted, base_s, obs_s
            )
        if not emitted:
            return
        route_events = select_cognition_route_events(
            emitted,
            has_trigger_tracker=(cs.tracker is not None or int(cs.obs_tf_seconds) == 3600),
        )
        if not route_events:
            return
        view = StructureView(
            regime=s.regime(), zones=s.levels, atr=atr,
            pivots=s.pivots[-self.params.range_max_pivots:],
            macro_imbalance=s.macro_imbalance(),
            tf_hours=int(cs.obs_tf_seconds // 3600),
            imbalance_strength=s.imbalance_strength(),
            dollar_volume=s.dollar_volume(),
            cog_vr4=s.variance_ratio(4),
            cog_park_ratio=s.multioctave_parkinson_ratio(),
            cog_er20=s.path_efficiency(20),
            cog_er_norm=s.multioctave_brownian_er(),
        )
        if defer_routes is not None:
            defer_routes.append((
                cs, atr, list(route_events), view, False, cog_bar, len(cs.cog_bars) - 1,
            ))
        else:
            self._route_step(cs, atr, route_events, view, mic=False)

    def _build_htf_views(self, cs: CampaignState, sym: str) -> tuple[StructureView, ...]:
        r_params = getattr(cs.router, "params", None)
        if not getattr(r_params, "htf_wall_check", True):
            return ()
        all_cs = [self.campaigns.get(sym), *self._extra_campaigns_for(sym)]
        htf_list: list[StructureView] = []
        for ocs in all_cs:
            if ocs is None or ocs.obs_tf_seconds < 2 * cs.obs_tf_seconds or not ocs.cog_bars:
                continue
            os_st = ocs.detector.structure
            htf_list.append(
                StructureView(
                    regime=os_st.regime(),
                    zones=os_st.levels,
                    atr=os_st.atr(),
                    pivots=(),
                    macro_imbalance=os_st.macro_imbalance(),
                    tf_hours=int(ocs.obs_tf_seconds // 3600),
                    imbalance_strength=os_st.imbalance_strength(),
                    dollar_volume=os_st.dollar_volume(),
                    cog_vr4=os_st.variance_ratio(4),
                )
            )
        return tuple(htf_list)

    def _peer_breadth_and_returns(
        self,
        obs_tf_seconds: int,
        my_rwa: bool,
        lb48: int,
        lb120: int,
    ) -> tuple[float, int, list[float], list[float]]:
        cache = getattr(self, "_step_peer_cache", None)
        cache_key = (int(obs_tf_seconds), bool(my_rwa))
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        peer_cs = [
            c_item
            for c_item in (*self.campaigns.values(), *self.extra_campaigns.values())
            if (
                c_item is not None
                and c_item.obs_tf_seconds == obs_tf_seconds
                and c_item.cog_bars
                and self._is_rwa(c_item.symbol) == my_rwa
            )
        ]
        peer_regs = [c_item.detector.structure.regime() for c_item in peer_cs]
        raw_breadth, n_univ = compute_market_breadth(peer_regs)
        m_breadth = raw_breadth if n_univ >= 4 else 0.0
        peer_r48 = [
            r
            for r in (c_item.detector.structure.log_return(lb48) for c_item in peer_cs)
            if r is not None
        ]
        peer_r120 = [
            r
            for r in (c_item.detector.structure.log_return(lb120) for c_item in peer_cs)
            if r is not None
        ]
        result = (m_breadth, n_univ, peer_r48, peer_r120)
        if cache is not None:
            cache[cache_key] = result
        return result

    def _evaluate_route(
        self,
        cs: CampaignState,
        atr: float,
        events: list[Any],
        view: StructureView,
        *,
        mic: bool,
        trig_bar: Sequence[Any] | None = None,
        bar_index: int | None = None,
    ) -> list[tuple[Any, ...]]:
        """纯信号路由评估(与 campaign.py 完全同源):产出待全局排序分发的决策元组列表。"""
        sym = cs.symbol
        bars = cs.trig_bars if mic else cs.cog_bars
        bar = trig_bar if trig_bar is not None else bars[-1]
        bidx = int(bar_index) if bar_index is not None else (len(bars) - 1)
        htf_views = self._build_htf_views(cs, sym)
        obs_s = int(
            getattr(cs, "obs_tf_seconds", 0)
            or TF_SECONDS_MAP.get(str(getattr(cs, "tf_label", "")), 14400)
        )
        lb48, lb120 = peer_lookback_bars(max(1, obs_s // 3600), canvas_bars=self.params.canvas_bars)
        m_breadth, n_univ, peer_r48, peer_r120 = self._peer_breadth_and_returns(
            obs_s, self._is_rwa(sym), lb48, lb120
        )
        cs_det = getattr(cs, "detector", None)
        own_r48 = cs_det.structure.log_return(lb48) if cs_det is not None else None
        own_r120 = cs_det.structure.log_return(lb120) if cs_det is not None else None
        ex48, ex120 = compute_peer_excess_returns(own_r48, own_r120, peer_r48, peer_r120)
        existing_pos = self.positions.get(sym)
        existing_tf_s = (
            TF_SECONDS_MAP.get(str(existing_pos.get("tf_label") or ""), 0)
            if existing_pos is not None
            else 0
        )
        allow_add = bool(getattr(getattr(cs, "pm", None), "allow_add", False))
        can_pyramid = (
            allow_add
            and existing_pos is not None
            and existing_tf_s == obs_s
            and int(existing_pos.get("adds_done") or 0) < 1
            and not self._entry_resting(sym)
        )
        blocked_by_same_or_htf = (
            existing_pos is not None
            and existing_tf_s >= obs_s
            and not can_pyramid
        )
        decisions = cs.router.on_bar(
            sym, bar, bidx, view, atr, events,
            position_open=blocked_by_same_or_htf,
            entry_resting=self._entry_resting(sym),
            htf_views=htf_views,
            market_breadth=m_breadth,
            universe_size=n_univ,
            median_dollar_volume=0.0,
            excess_ret_48h=ex48,
            excess_ret_120h=ex120,
        )
        t = int(bar[0])
        return [(d, cs, t, mic, view, bar) for d in decisions]

    def _dispatch_route_decision(
        self,
        d: Any,
        cs: CampaignState,
        t: int,
        mic: bool,
        view: StructureView | None,
        bar: Sequence[Any],
    ) -> None:
        """分发单条路由决策(在全局排序后顺序执行,处理加仓/升格原子交接)。"""
        sym = cs.symbol
        if d.kind == "cancel_entry":
            self._cancel_entry(sym, str(d.meta.get("reason", "")))
            return
        if d.kind not in ("market", "limit"):
            raise RuntimeError(f"unknown decision {d.kind}")
        if self._gap_replay:
            self.counters["gap_entry_skips"] = self.counters.get("gap_entry_skips", 0) + 1
            self._journal(
                "gap_entry_skip",
                {"symbol": sym, "ts": int(bar[0]), "event": getattr(d.hyp, "event_id", None)},
            )
            return
        if self._entry_resting(sym):
            return
        cur_pos = self.positions.get(sym)
        obs_s = int(
            getattr(cs, "obs_tf_seconds", 0)
            or TF_SECONDS_MAP.get(str(getattr(cs, "tf_label", "")), 14400)
        )
        cur_tf_s = (
            TF_SECONDS_MAP.get(str(cur_pos.get("tf_label") or ""), 0)
            if cur_pos is not None
            else 0
        )
        if cur_pos is not None and cur_tf_s > obs_s:
            return
        allow_add = bool(getattr(getattr(cs, "pm", None), "allow_add", False))
        promote_from_pos: dict[str, Any] | None = None
        if cur_pos is not None and cur_tf_s == obs_s:
            dr_p = int(cur_pos["direction"])
            ref_p = float(d.intent.ref_price) if d.kind == "market" else float(d.limit_price)
            sig_inv = float(d.hyp.invalid)
            base_en = float(cur_pos["entry"])
            base_inv = float(cur_pos["invalid"])
            if (
                not allow_add
                or d.kind != "market"
                or int(cur_pos.get("adds_done") or 0) >= 1
                or int(d.hyp.direction) != dr_p
                or dr_p * (ref_p - base_en) <= 0.0
                or dr_p * (sig_inv - base_en) < 0.0
                or dr_p * (ref_p - base_inv) <= 0.0
            ):
                self.counters["pyramid_gate_skips"] = self.counters.get("pyramid_gate_skips", 0) + 1
                return
            d.meta["pyramid_add"] = True
            d.meta["base_invalid"] = base_inv
            d.meta["size_mult"] = min(float(d.meta.get("size_mult") or 1.0), 0.5)
        elif cur_pos is not None and cur_tf_s < obs_s:
            promote_from_pos = cur_pos
        self._submit_open(
            cs, d, t, mic=mic, view=view, trigger_bar=bar, promote_from_pos=promote_from_pos
        )

    def _route_step(
        self,
        cs: CampaignState,
        atr: float,
        events: list[Any],
        view: StructureView,
        *,
        mic: bool,
        trig_bar: Sequence[Any] | None = None,
    ) -> None:
        """与 campaign.py 完全同源的路由决策入口(MarketRouter → 意图)。"""
        for dec_tuple in self._evaluate_route(cs, atr, events, view, mic=mic, trig_bar=trig_bar):
            self._dispatch_route_decision(*dec_tuple)

    # ------------------------------------------------------------------
    # 意图 → 闸门 → 装配(执行在撮合/live 下单层)
    # ------------------------------------------------------------------

    def _entry_resting(self, sym: str) -> bool:
        return any(
            it["status"] == "armed" and it["symbol"] == sym and it["role"] == "entry"
            for it in self.open_intents.values()
        )

    def _spec(self, sym: str) -> ContractSpec:
        sp = self.contracts.get(sym)
        raw_ent = self.contract_raw.get(sym)
        if sp is None:
            is_rwa = str((raw_ent or {}).get("isRwa") or "").strip().upper() == "YES"
            return ContractSpec(
                symbol=sym, size_min=1e-6, size_step=1e-6,
                size_place=6, price_place=4, min_trade_usdt=5.0, is_rwa=is_rwa,
            )
        if (
            raw_ent is not None
            and not sp.is_rwa
            and str(raw_ent.get("isRwa") or "").strip().upper() == "YES"
        ):
            return ContractSpec(
                symbol=sp.symbol, size_min=sp.size_min, size_step=sp.size_step,
                size_place=sp.size_place, price_place=sp.price_place,
                min_trade_usdt=sp.min_trade_usdt, is_rwa=True,
            )
        return sp

    def _is_rwa(self, sym: str) -> bool:
        root = sym.split("@", 1)[0] if "@" in sym else sym
        sp = self.contracts.get(root)
        if sp is not None and bool(getattr(sp, "is_rwa", False)):
            return True
        raw_ent = self.contract_raw.get(root)
        return str((raw_ent or {}).get("isRwa") or "").strip().upper() == "YES"

    @staticmethod
    def _box_bounds(
        zone_lo: Any,
        zone_hi: Any,
        box_opposite: Any,
    ) -> tuple[float | None, float | None]:
        """原子水平位带。`box_opposite` 保留参数以兼容旧调用,不再把带加宽成平衡箱。"""
        del box_opposite
        if zone_lo is None or zone_hi is None:
            return None, None
        lo, hi = float(zone_lo), float(zone_hi)
        return (lo, hi) if hi >= lo else (hi, lo)

    @staticmethod
    def _bar_tf_label(seconds: int) -> str:
        for name, sec in TF_SECONDS_MAP.items():
            if int(sec) == int(seconds):
                return name
        seconds = int(seconds or 0)
        if seconds >= 86400 and seconds % 86400 == 0:
            return f"{seconds // 86400}d"
        if seconds >= 3600 and seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        if seconds >= 60 and seconds % 60 == 0:
            return f"{seconds // 60}m"
        return f"{seconds}s" if seconds else ""

    def _require_exchange_protection(self) -> bool:
        return bool((self.cfg.get("execution") or {}).get("require_exchange_protection"))

    @staticmethod
    def _resolve_box_start_ts(cs: CampaignState, meta: Mapping[str, Any] | None) -> int | None:
        """根据认知帧的 zone_origin_known 还原平衡区起始时间戳(毫秒)。"""
        if not meta:
            return None
        origin = meta.get("zone_origin_known")
        if origin is None:
            return None
        try:
            idx = int(origin)
        except (TypeError, ValueError):
            return None
        cog = getattr(cs, "cog_bars", None) or ()
        if 0 <= idx < len(cog):
            return int(cog[idx][0])
        return None

    @staticmethod
    def _signal_chart_bars(cs: CampaignState, mic: bool) -> tuple[list[Any], bool]:
        """信号图用认知帧(4h/1d),不用原始 30m 触发缓存。"""
        del mic
        cog = list(cs.cog_bars[-_CHART_BAR_WINDOW:]) if cs.cog_bars else []
        if cog:
            return cog, True
        return list(cs.trig_bars[-_CHART_BAR_WINDOW:]) if cs.trig_bars else [], False

    @staticmethod
    def _chart_pivots(pivots: Sequence[Any]) -> list[tuple[int | None, float, str]]:
        """图上的结构端点:`[(窗口下标, 价格, "high"|"low")]`。"""
        out: list[tuple[int | None, float, str]] = []
        win = _CHART_BAR_WINDOW
        for p in list(pivots or ())[-6:]:
            idx = int(getattr(p, "known_index", -1))
            if idx < 0:
                continue
            kind = getattr(p, "kind", "high")
            out.append((
                win - 1 - idx if idx < win else None,
                float(p.price),
                getattr(kind, "value", str(kind)),
            ))
        return out

    def _signal_status_text(
        self,
        block: str | None,
        *,
        tf_label: str,
        kind: str,
        watch_entry: bool = False,
    ) -> str:
        """把闸门结果翻成一句对外口径(公开频道不得出现账户数字)。"""
        if block == "TF_CAP":
            return f"仅参考 · {tf_label} 周期容量已满,本系统本拍未开仓"
        if block is not None:
            return self._BLOCK_STATUS.get(block, f"仅参考 · {block}")
        if watch_entry:
            return "计划进入系统价格关注（尚未下单；触发后再检查并提交市价单）"
        act = "提交限价单" if kind == "limit" else "提交市价开仓单"
        return f"本系统计划{act}（非成交确认，成交以运营通知及交易所回执为准）"

    def _note_block(
        self,
        sym: str,
        cs: CampaignState,
        hyp: Any,
        block: str,
        *,
        qty: float,
        ref: float,
    ) -> None:
        """闸门记账:计数器与日志字段与解耦前逐条一致。"""
        for name, inc in self._BLOCK_COUNTERS.get(block, ()):
            self.counters[name] = self.counters.get(name, 0) + inc
        rec: dict[str, Any] = {"symbol": sym, "why": block, "event": hyp.event_id}
        if block == "TF_CAP":
            rec["tf"] = cs.tf_label
        if block == "MIN_NOTIONAL":
            rec.update({"qty": qty, "ref": ref})
        self._journal("reject" if block == "degenerate_risk" else "skip", rec)

    def _notify_signal(
        self,
        key: tuple[Any, ...],
        *,
        size_note: str,
        exec_status: str,
        **payload: Any,
    ) -> None:
        """信号广播:同一事件只推一条,且永不 raise。"""
        if key in self._signal_sent:
            return
        self._signal_sent[key] = 1
        while len(self._signal_sent) > _SIGNAL_DEDUPE_MAX:
            self._signal_sent.pop(next(iter(self._signal_sent)))
        fn = getattr(self.tg, "send_signal", None)
        if fn is None:
            return
        try:
            target = self.tg._notifier if isinstance(self.tg, NotificationGuard) else self.tg
            target.send_signal(size_note=size_note, exec_status=exec_status, **payload)
        except Exception as exc:
            self.counters["signal_notice_errors"] = self.counters.get("signal_notice_errors", 0) + 1
            with suppress(OSError):
                self._journal(
                    "signal_notice_error",
                    {
                        "symbol": payload.get("symbol"),
                        "event": payload.get("event_id"),
                        "why": f"{type(exc).__name__}: {str(exc)[:120]}",
                    },
                )

    _BLOCK_STATUS = {
        "API_HALT": "仅参考 · API 熔断中,本系统暂停新开仓",
        "degenerate_risk": "仅参考 · 参考价已越过失效位(风险距离为 0)",
        "TF_CAP": "",
        "PORTFOLIO_CAP": "仅参考 · 组合容量已满,本系统本拍未开仓",
        "OPPOSITE_CLAIM": "仅参考 · 同标的已有反向持仓,本系统本拍未开仓",
        "CLAIM_CAP": "仅参考 · 该标的同向判断的风险额度已用完,本系统本拍未开仓",
        "DIRECTIONAL_CAP": "仅参考 · 同向风险额度已用完,本系统本拍未开仓",
        "RWA_MARKET_CLOSED": "仅参考 · 该非加密标的处于其底层市场的休市时段,本系统暂停开新仓",
        "RWA_INACTIVE_SESSION": "仅参考 · 非加密传统标的处于零成交休市死水时段,本系统暂停开新仓",
        "QUOTE_STALE": "仅参考 · 非加密标的报价缺失或过期,本系统本拍未开仓",
        "SPREAD_WIDE": "仅参考 · 非加密标的买卖点差过宽,本系统本拍未开仓",
        "BASIS_WIDE": "仅参考 · 非加密标的与指数价格偏离过大,本系统本拍未开仓",
        "MANAGE_ONLY": "仅参考 · 该标的已退出交易宇宙但仍有持仓,本系统只管理出场、不新开仓",
        "MIN_NOTIONAL": "仅参考 · 数量低于交易所最小下单,本系统本拍未开仓",
        "DAILY_LOSS_BREAKER": "仅参考 · 当日亏损熔断,本系统暂停新开仓",
        "RATE_LIMIT": "仅参考 · 触达下单频率上限,本系统本拍未开仓",
        "DUPLICATE": "仅参考 · 同一信号已处理,本次为重复回放",
    }
    _BLOCK_COUNTERS = {
        "API_HALT": (("api_halt_skips", 1),),
        "degenerate_risk": (),
        "TF_CAP": (("tf_cap_skips", 1), ("cap_skips", 1)),
        "PORTFOLIO_CAP": (("cap_skips", 1),),
        "OPPOSITE_CLAIM": (("cap_skips", 1),),
        "CLAIM_CAP": (("cap_skips", 1),),
        "DIRECTIONAL_CAP": (("cap_skips", 1),),
        "RWA_MARKET_CLOSED": (("rwa_session_skips", 1),),
        "RWA_INACTIVE_SESSION": (("rwa_session_skips", 1),),
        "QUOTE_STALE": (("quote_guard_skips", 1),),
        "SPREAD_WIDE": (("quote_guard_skips", 1),),
        "BASIS_WIDE": (("quote_guard_skips", 1),),
        "MANAGE_ONLY": (("manage_only_skips", 1),),
        "MIN_NOTIONAL": (("min_notional_skips", 1),),
        "DAILY_LOSS_BREAKER": (("breaker_blocks", 1),),
        "RATE_LIMIT": (("rate_skips", 1),),
        "DUPLICATE": (),
    }

    def _position_worst_case(self, p: Mapping[str, Any], fee_rate: float) -> float:
        """与 capacity 模块严格同源的持仓风险占用(cash_net_admission 下浮盈不倒扣本金池)。"""
        d = int(p["direction"])
        qty = float(p["qty"])
        mk = float(p.get("mark") or p["entry"])
        inv = float(p["invalid"])
        if getattr(self, "capacity_mode", "") == "cash_net_admission":
            en = float(p["entry"])
            ref = min(en, mk) if d > 0 else max(en, mk)
            stop = qty * max(0.0, d * (ref - inv)) if ref > 0 else 0.0
            return stop + qty * mk * float(fee_rate)
        return worst_case_of_position(d, qty, mk, inv, fee_rate)

    def _factor_burst_blocked(
        self,
        symbol: str,
        direction: int,
        ts_ms: int,
        exclude_oid: str | None = None,
    ) -> str | None:
        """同资产类 × 同向单根 1h / 4h 棒因子并发上限(与回放 Broker._fill_open 逐位同构)。"""
        cap_1h = int(self.risk.get("factor_burst_cap_1h", 0) or 0)
        cap_4h = int(self.risk.get("factor_burst_cap_4h", 0) or 0)
        if cap_1h <= 0 and cap_4h <= 0:
            return None
        target_rwa = self._is_rwa(symbol)
        b1 = int(ts_ms) // 3_600_000
        b4 = int(ts_ms) // 14_400_000
        syms_1h: set[str] = set()
        syms_4h: set[str] = set()
        for k_sym, p in self.positions.items():
            if int(p.get("direction") or 0) != direction or self._is_rwa(k_sym) != target_rwa:
                continue
            pts = int(p.get("opened_ts") or 0)
            if pts <= 0:
                continue
            root = k_sym.split("@")[0]
            if pts // 3_600_000 == b1:
                syms_1h.add(root)
            if pts // 14_400_000 == b4:
                syms_4h.add(root)
        for k_oid, it in self.open_intents.items():
            if (
                k_oid == exclude_oid
                or it.get("role") != "entry"
                or it.get("status") not in ("armed", "filled")
                or (it.get("watch_entry") and it.get("status") == "armed")
                or it.get("pyramid_add")
                or int(it.get("direction") or 0) != direction
            ):
                continue
            isym = str(it.get("symbol") or "")
            if not isym or self._is_rwa(isym) != target_rwa:
                continue
            its = int(it.get("fill_ts") or it.get("confirm_ts") or it.get("decision_ts") or 0)
            if its <= 0:
                continue
            root = isym.split("@")[0]
            if its // 3_600_000 == b1:
                syms_1h.add(root)
            if its // 14_400_000 == b4:
                syms_4h.add(root)
        if cap_1h > 0 and len(syms_1h) >= cap_1h:
            return "FACTOR_BURST_CAP_1H"
        if cap_4h > 0 and len(syms_4h) >= cap_4h:
            return "FACTOR_BURST_CAP_4H"
        return None

    def _markowitz_corr_mult(self, symbol: str, direction: int) -> float:
        """与回放 campaign.py 逐位同构的无状态 4H Markowitz 协方差仓位调节因子。"""
        def _rets_for(sym_name: str) -> tuple[float, ...]:
            cands = [getattr(self, "campaigns", {}).get(sym_name), *self._extra_campaigns_for(sym_name)]
            best: CampaignState | None = None
            for c_item in cands:
                if c_item is None or getattr(c_item, "symbol", None) != sym_name:
                    continue
                obs_s = int(getattr(c_item, "obs_tf_seconds", 0) or 0)
                if (
                    best is None
                    or obs_s == 14400
                    or (int(getattr(best, "obs_tf_seconds", 0) or 0) != 14400 and obs_s < int(getattr(best, "obs_tf_seconds", 0) or 0))
                ):
                    best = c_item
            det = getattr(best, "detector", None) if best is not None else None
            st = getattr(det, "structure", None) if det is not None else None
            fn = getattr(st, "recent_log_returns", None) if st is not None else None
            return fn(60) if callable(fn) else ()

        own_rets = _rets_for(symbol)
        peers: list[tuple[int, tuple[float, ...]]] = []
        for p_sym, pos in (getattr(self, "positions", None) or {}).items():
            if p_sym == symbol or not isinstance(pos, dict):
                continue
            d_j = int(pos.get("direction") or 0)
            if d_j != 0:
                peers.append((d_j, _rets_for(p_sym)))
        return markowitz_corr_multiplier(int(direction), own_rets, peers)

    def _directional_entry_blocked(
        self,
        direction: int,
        qty: float,
        price: float,
        invalid: float,
        *,
        symbol: str | None = None,
        exclude_oid: str | None = None,
        exclude_symbol: str | None = None,
    ) -> bool:
        """Derive same-side in-flight exposure per asset class ('crypto' vs 'rwa')."""
        frac = self.directional_cap_frac
        if frac is None or self.equity is None:
            return False
        target_rwa = self._is_rwa(symbol) if symbol is not None else None
        capital = max(0.0, float(self.equity) - float(self.unrealized_pl or 0.0))
        fee, slip = float(self.risk["taker_fee"]), float(self.risk["slippage_per_side"])
        used = sum(
            self._position_worst_case(p, fee + slip)
            for k_sym, p in self.positions.items()
            if k_sym != exclude_symbol
            and p["direction"] == direction
            and (target_rwa is None or self._is_rwa(k_sym) == target_rwa)
        )
        if getattr(self, "capacity_mode", "") == "cash_net_admission":
            opp_filled = sum(
                self._position_worst_case(p, fee + slip)
                for k_sym, p in self.positions.items()
                if k_sym != exclude_symbol
                and p["direction"] == -direction
                and (target_rwa is None or self._is_rwa(k_sym) == target_rwa)
            )
            used = max(0.0, used - opp_filled)
        for oid, it in self.open_intents.items():
            if (
                oid == exclude_oid
                or it.get("status") != "armed"
                or it.get("role") != "entry"
                or it["direction"] != direction
                or it["symbol"] == exclude_symbol
                or it["symbol"] in self.positions
            ):
                continue
            if target_rwa is not None and self._is_rwa(str(it["symbol"])) != target_rwa:
                continue
            ref = float(it.get("limit_price") or it["ref_px"])
            used += float(it["qty"]) * (abs(ref - float(it["invalid"])) + ref * 2.0 * (fee + slip))
        candidate = qty * (abs(price - invalid) + price * 2.0 * (fee + slip))
        return used + candidate > frac * capital + 1e-9

    def _gate_bar_and_history(
        self,
        cs: CampaignState,
        eval_bar: Sequence[Any] | None,
        mic: bool,
    ) -> tuple[Sequence[Any] | None, list[Any] | None]:
        """闸门输入:(bar, history);占位条存在时回落至同窗口有成交认知条。"""
        src = cs.trig_bars if mic else cs.cog_bars
        if eval_bar is None:
            return None, None
        hist = [b for b in (src or ())[-250:] if len(b) >= 6 and int(b[0]) < int(eval_bar[0])]
        bar = eval_bar
        if (
            mic
            and cs.cog_bars
            and len(eval_bar) >= 6
            and (float(eval_bar[5]) <= 0.0 or float(eval_bar[2]) <= float(eval_bar[3]))
        ):
            cog = cs.cog_bars[-1]
            if int(cog[0]) <= int(eval_bar[0]) and len(cog) >= 6 and float(cog[5]) > 0.0:
                bar = cog
                hist = [b for b in cs.cog_bars[-250:] if len(b) >= 6 and int(b[0]) < int(cog[0])]
                self.counters["gate_bar_fallbacks"] = self.counters.get("gate_bar_fallbacks", 0) + 1
                self._journal(
                    "gate_bar_fallback",
                    {
                        "symbol": cs.symbol,
                        "tf": cs.tf_label,
                        "eval_ts": int(eval_bar[0]),
                        "cog_ts": int(cog[0]),
                    },
                )
        return bar, hist

    def _fetch_signal_quote(
        self,
        sym: str,
        direction: int,
    ) -> tuple[float | None, int | None, float | None, dict[str, Any] | None]:
        """Fetch cached O(1) ticker quote for ``sym`` in live mode."""
        if self.mode != "live":
            return None, None, None, None
        try:
            now_s = time.time()
            cache = getattr(self, "_signal_quote_cache", None)
            if cache is None or now_s - cache[0] >= _QUOTE_CACHE_TTL_S:
                rows = self.client.tickers() or []
                indexed = {str(r["symbol"]): r for r in rows if r.get("symbol")}
                cache = (now_s, indexed)
                self._signal_quote_cache = cache
            row = cache[1].get(sym)
            if row is None:
                return None, None, None, None
            snapshot_px = float(row.get("lastPr") or 0.0) or None
            snapshot_ts = int(cache[0] * 1000)
            execution_quote = float(row.get("askPr" if direction > 0 else "bidPr") or 0.0) or None
            quote = {
                "bid": float(row.get("bidPr") or 0.0) or None,
                "ask": float(row.get("askPr") or 0.0) or None,
                "last": snapshot_px,
                "index": float(row.get("indexPrice") or 0.0) or None,
                "ts": int(row.get("ts") or snapshot_ts or 0) or snapshot_ts,
            }
            return snapshot_px, snapshot_ts, execution_quote, quote
        except (BitgetError, OSError, TypeError, ValueError, AttributeError) as exc:
            with suppress(OSError):
                self._journal("signal_quote_error", {"symbol": sym, "why": str(exc)[:120]})
            return None, None, None, None

    def _size_and_gate_entry(
        self,
        cs: CampaignState,
        d: Any,
        *,
        entry_ref: float,
        fitted_entry: float,
        invalid: float,
        risk_per_unit: float,
        size_mult: float,
        eff_rf: float,
        equity: float,
        capital: float,
        confirm_ts: int,
        execution_quote: float | None,
        quote: dict[str, Any] | None,
        cost_limited: bool,
        is_pyramid: bool,
        mic: bool,
        trigger_bar: Sequence[Any] | None,
        promote_from_pos: dict[str, Any] | None,
    ) -> tuple[str | None, float, float, float, float]:
        """Run all pre-order entry gates and risk sizing.

        Returns ``(block, qty, candidate, size_mult, risk_per_unit)``.
        """
        sym = cs.symbol
        hyp = d.hyp
        spec = self._spec(sym)
        block: str | None = entry_cost_error(hyp.direction, fitted_entry, invalid, d.meta)
        if block is None:
            eval_bar = (
                trigger_bar
                if trigger_bar is not None
                else (cs.trig_bars[-1] if mic and cs.trig_bars else (cs.cog_bars[-1] if cs.cog_bars else None))
            )
            gate_bar, hist = self._gate_bar_and_history(cs, eval_bar, mic)
            block = rwa_session_entry_block(
                self._is_rwa(sym), int(confirm_ts), gate_bar, symbol=sym, history=hist
            )
        if block is None:
            block = self._manage_only_block(sym)
        if block is None and self.mode == "live":
            block = self._quote_guard_block(sym, quote)

        if self.mode == "live" and self.api_halted:
            return "API_HALT", 0.0, 0.0, size_mult, risk_per_unit
        if risk_per_unit <= 0.0:
            return "degenerate_risk", 0.0, 0.0, size_mult, risk_per_unit
        if block is not None:
            return block, 0.0, 0.0, size_mult, risk_per_unit
        if self.mode == "live" and d.kind == "limit" and cost_limited:
            err = entry_contract_error(hyp.direction, fitted_entry, invalid, hyp.target_1, d.meta)
            if err is not None:
                return err, 0.0, 0.0, size_mult, risk_per_unit
        elif self.mode == "live" and d.kind == "market" and (d.meta.get("bounded_entry") or cost_limited):
            err = (
                entry_contract_error(
                    hyp.direction, execution_quote, invalid, hyp.target_1, d.meta, at_execution=True
                )
                if execution_quote is not None
                else "EXECUTION_QUOTE_UNAVAILABLE"
            )
            if err is not None:
                return err, 0.0, 0.0, size_mult, risk_per_unit
            risk_per_unit = max(risk_per_unit, abs(float(execution_quote) - invalid))

        exclude_sym = sym if promote_from_pos is not None else None
        fee_slip = float(self.risk["taker_fee"]) + float(self.risk.get("slippage_per_side", 0.0))
        qty = eff_rf * size_mult * capital / risk_per_unit
        if is_pyramid and sym in self.positions:
            base_q0 = float(self.positions[sym].get("entry_qty") or self.positions[sym].get("qty") or 0.0)
            if base_q0 > 0.0:
                qty = min(qty, 0.5 * base_q0)
        eff_lev = float(self._max_allowed_leverage(sym))
        port_lev = float(self.risk.get("leverage_cap") or eff_lev)
        gross_notional = sum(
            abs(float(p.get("qty") or 0.0) * float(p.get("mark") or p.get("entry") or 0.0))
            for k_s, p in self.positions.items()
            if k_s != exclude_sym
        )
        rem_gross_qty = max(0.0, port_lev * equity - gross_notional) / entry_ref
        qty = floor_step(min(qty, eff_lev * equity / entry_ref, rem_gross_qty), spec.size_step, spec.size_place)

        outstanding = sum(
            self._position_worst_case(p, fee_slip)
            for k_s, p in self.positions.items()
            if k_s != exclude_sym
        )
        unit_exp = risk_per_unit + entry_ref * 2.0 * fee_slip
        candidate = qty * unit_exp
        tf_caps = self.risk.get("tf_cap_by_tf") or {}
        if self._is_rwa(sym):
            mult = self.cfg["universe"].get("rwa", {}).get("capacity_multiplier", 1.0)
            tf_caps = {k: v * mult for k, v in tf_caps.items()}
        tf_cap = tf_caps.get(cs.tf_label)
        tf_out = 0.0
        group = 0.0
        if tf_cap is not None:
            tf_out = sum(
                self._position_worst_case(p, fee_slip)
                for k_s, p in self.positions.items()
                if k_s != exclude_sym and p.get("tf_label", "12h") == cs.tf_label
            )
            group = sum(
                self._position_worst_case(p, fee_slip)
                for key, p in self.positions.items()
                if key != exclude_sym and key.split("@")[0] == sym and p["direction"] == hyp.direction
            )
        opposite_open = any(
            p["direction"] == -hyp.direction
            for key, p in self.positions.items()
            if key != exclude_sym and key.split("@")[0] == sym
        )
        if not opposite_open and candidate > 0.0:
            eff_headroom = float(self.risk["portfolio_cap_frac"]) * capital - outstanding
            if tf_cap is not None:
                eff_headroom = min(
                    eff_headroom,
                    float(tf_cap) * capital - tf_out,
                    float(tf_cap) * capital - group,
                )
            if candidate > eff_headroom + 1e-9:
                scale = (max(0.0, eff_headroom) * 0.999) / candidate
                if scale >= 0.25:
                    new_mult = size_mult * scale
                    qty_scaled = floor_step(
                        min(eff_rf * new_mult * capital / risk_per_unit, eff_lev * equity / entry_ref, rem_gross_qty),
                        spec.size_step,
                        spec.size_place,
                    )
                    min_notional = max(spec.min_trade_usdt, spec.size_min * entry_ref)
                    if qty_scaled > 0.0 and qty_scaled * entry_ref >= min_notional:
                        size_mult = new_mult
                        d.meta["size_mult"] = size_mult
                        qty = qty_scaled
                        candidate = qty * unit_exp

        if tf_cap is not None and tf_out + candidate > float(tf_cap) * capital + 1e-9:
            block = "TF_CAP"
        elif outstanding + candidate > self.risk["portfolio_cap_frac"] * capital + 1e-9:
            block = "PORTFOLIO_CAP"
        elif opposite_open:
            block = "OPPOSITE_CLAIM"
        elif tf_cap is not None and group + candidate > float(tf_cap) * capital + 1e-9:
            block = "CLAIM_CAP"
        elif qty * entry_ref < max(spec.min_trade_usdt, spec.size_min * entry_ref):
            block = "MIN_NOTIONAL"
        elif self._directional_entry_blocked(
            hyp.direction,
            qty,
            execution_quote if (self.mode == "live" and d.kind == "market" and execution_quote is not None) else fitted_entry,
            invalid,
            symbol=sym,
            exclude_symbol=exclude_sym,
        ):
            block = "DIRECTIONAL_CAP"
        elif not is_pyramid and not bool(d.meta.get("watch_entry")):
            block = self._factor_burst_blocked(sym, int(hyp.direction), int(confirm_ts))
        if block is None and self.breaker.should_stop(equity):
            block = "DAILY_LOSS_BREAKER"
        if block is None and not self.rate.allow():
            block = "RATE_LIMIT"
        return block, qty, candidate, size_mult, risk_per_unit

    def _build_entry_intent(
        self,
        cs: CampaignState,
        d: Any,
        *,
        oid: str,
        t: int,
        qty: float,
        planned_lev: int,
        entry_ref: float,
        invalid: float,
        risk_per_unit: float,
        size_mult: float,
        edge_mult: float,
        box_lo: float | None,
        box_hi: float | None,
        box_start_ts: int | None,
        is_pyramid: bool,
        is_watch: bool,
        wait_h: int,
        confirm_ts: int,
        mic: bool,
        cost_limited: bool,
    ) -> dict[str, Any]:
        hyp = d.hyp
        reenter_px = (
            (float(box_lo) if hyp.direction > 0 else float(box_hi))
            if (box_lo is not None and box_hi is not None)
            else None
        )
        intent: dict[str, Any] = {
            "oid": oid, "symbol": cs.symbol, "tf_label": cs.tf_label,
            "role": "entry", "kind": d.kind, "direction": hyp.direction,
            "qty": qty, "leverage": planned_lev,
            "limit_price": entry_ref if d.kind == "limit" else None,
            "ref_px": float(entry_ref),
            "invalid": float(invalid),
            "reenter_invalid_px": reenter_px,
            "pyramid_add": is_pyramid,
            "target_1": hyp.target_1, "event_id": hyp.event_id,
            "structural_target": hyp.target_1,
            "execution_target": execution_target(d.meta, hyp.target_1),
            "route": d.route, "size_mult": size_mult, "edge_mult": edge_mult,
            "box_lo": box_lo, "box_hi": box_hi, "box_start_ts": box_start_ts,
            "level_kind": d.meta.get("level_kind"),
            "level_role": d.meta.get("level_role"),
            "bounded_entry": bool(d.meta.get("bounded_entry")),
            "entry_min_rr": float(d.meta.get("entry_min_rr", 0.0)),
            "inner_edge": d.meta.get("inner_edge"),
            "max_box_progress_frac": d.meta.get("max_box_progress_frac"),
            "watch_entry": is_watch,
            "watch_price": float(entry_ref) if is_watch else None,
            "near_price": float(d.meta["near_price"]) if d.meta.get("near_price") is not None else None,
            "prog_price": float(d.meta["prog_price"]) if d.meta.get("prog_price") is not None else None,
            "confirm_close": float(d.meta["confirm_close"]) if d.meta.get("confirm_close") is not None else None,
            "max_prog_frac": float(d.meta["max_prog_frac"]) if d.meta.get("max_prog_frac") is not None else None,
            "max_wait_hours": wait_h,
            "near_touched": False, "near_extreme": None,
            "risk_usdt_cap": float(qty * risk_per_unit),
            "decision_ts": t, "confirm_ts": int(confirm_ts), "mic": mic,
            "expiry_ts": confirm_ts + wait_h * MS_1H, "status": "armed",
            "exchange_order_id": None,
        }
        if cost_limited:
            intent.update(
                max_cost_frac=d.meta["max_cost_frac"],
                cost_per_side=d.meta["cost_per_side"],
            )
        return intent

    def _submit_live_entry_order(
        self,
        sym: str,
        d: Any,
        intent: dict[str, Any],
        *,
        oid: str,
        side: str,
        qty: float,
        planned_lev: int,
        entry_ref: float,
        fitted_entry: float,
        equity: float,
        t: int,
        is_watch: bool,
    ) -> None:
        spec = self._spec(sym)
        try:
            if is_watch:
                self.counters["watch_armed"] = self.counters.get("watch_armed", 0) + 1
                return
            try:
                self._ensure_leverage(sym, planned_lev)
            except TypeError:
                self._ensure_leverage(sym)
            act_lev = float(self.leverage_by_sym.get(sym) or planned_lev)
            intent["leverage"] = int(act_lev)
            if qty * entry_ref > act_lev * equity + 1e-9:
                qty = floor_step(act_lev * equity / entry_ref, spec.size_step, spec.size_place)
                intent["qty"] = qty
            if d.kind == "limit":
                resp = self.client.place_limit_order(
                    sym, side=side, size=qty, price=fitted_entry,
                    client_oid=oid, trade_side=self.open_trade_side(),
                    **self._entry_protection(sym, intent),
                )
                intent["exchange_order_id"] = str(resp.get("orderId") or "")
                self.counters["orders_placed"] += 1
            else:
                resp = self.client.place_market_order(
                    sym, side=side, size=qty, client_oid=oid,
                    trade_side=self.open_trade_side(),
                    **self._entry_protection(sym, intent),
                )
                intent["exchange_order_id"] = str(resp.get("orderId") or "")
                self.counters["orders_placed"] += 1
                self._live_entry_filled(intent, side_fallback_price=entry_ref, now_ms=t)
        except BitgetError as exc:
            if self._require_exchange_protection() and "43023" in str(exc):
                intent["status"] = "error"
                self.counters["protection_unavailable"] = self.counters.get("protection_unavailable", 0) + 1
                self._journal(
                    "protection_unavailable",
                    {"oid": oid, "symbol": sym, "event": d.hyp.event_id, "why": str(exc)[:160]},
                )
                self.tg.send_alert(f"{sym} 开仓被拒(无法挂交易所止损):{str(exc)[:100]} → 本笔不开仓")
                self._api_note(exc)
            elif d.kind != "limit" and "40786" in str(exc):
                self._journal(
                    "order_idempotent_recover",
                    {"oid": oid, "symbol": sym, "event": d.hyp.event_id, "why": str(exc)[:160]},
                )
                self._live_entry_filled(intent, side_fallback_price=entry_ref, now_ms=t)
            else:
                intent["status"] = "error"
                self.counters["order_errors"] = self.counters.get("order_errors", 0) + 1
                self._journal(
                    "order_error",
                    {"oid": oid, "symbol": sym, "event": d.hyp.event_id, "why": str(exc)[:160]},
                )
                self.tg.send_alert(f"{sym} 开仓下单失败:{str(exc)[:120]}(意图作废,不重试)")
                self._api_note(exc)

    @staticmethod
    def _build_watch_details(
        d: Any,
        hyp: Any,
        entry_ref: float,
        *,
        is_watch: bool,
        wait_h: int,
    ) -> dict[str, Any] | None:
        if not is_watch:
            return None
        try:
            prog = d.meta.get("prog_price")
            if (
                prog is None
                and d.meta.get("max_prog_frac") is not None
                and hyp.target_1 is not None
                and d.meta.get("confirm_close") is not None
            ):
                close = float(d.meta["confirm_close"])
                prog = close + hyp.direction * float(d.meta["max_prog_frac"]) * abs(
                    float(hyp.target_1) - close
                )
            return {
                "watch_price": entry_ref,
                "near_price": d.meta.get("near_price"),
                "prog_price": prog,
                "max_wait_hours": wait_h,
            }
        except (TypeError, ValueError):
            return {"watch_price": entry_ref, "max_wait_hours": wait_h}

    def _plan_and_broadcast_signal(
        self,
        cs: CampaignState,
        d: Any,
        *,
        t: int,
        side: str,
        entry_ref: float,
        invalid: float,
        block: str | None,
        qty: float,
        candidate: float,
        eff_rf: float,
        edge_mult: float,
        equity: float,
        capital: float,
        confirm_ts: int,
        box_lo: float | None,
        box_hi: float | None,
        box_start_ts: int | None,
        snapshot_px: float | None,
        snapshot_ts: int | None,
        chart_bars: list[Any],
        on_cog: bool,
        chart_pivots: list[Any],
        view: StructureView | None,
        mic: bool,
        is_watch: bool,
        watch_details: dict[str, Any] | None,
    ) -> int:
        sym = cs.symbol
        hyp = d.hyp
        regime = hyp.regime.value if hyp.regime is not None else ""
        zone_lo, zone_hi = d.meta.get("zone_lo"), d.meta.get("zone_hi")
        sig_size_mult = float(d.meta.get("size_mult", 1.0) or 1.0)
        sig_rf = eff_rf * sig_size_mult * edge_mult
        sig_stop_dist = abs(entry_ref - invalid) / entry_ref if entry_ref > 0.0 else 0.0
        sig_notional = (
            qty * entry_ref
            if (block is None and qty > 0.0)
            else ((sig_rf * capital / sig_stop_dist) if sig_stop_dist > 0.0 else 0.0)
        )
        sig_cand_risk = candidate if (block is None and qty > 0.0) else (sig_rf * capital)
        planned_lev = self._plan_trade_leverage(sym, sig_notional, equity, capital, sig_cand_risk)
        if sig_stop_dist > 0.0:
            planned_lev = max(1, min(planned_lev, int(max(1, 0.75 / sig_stop_dist))))
        sig_margin_pct = (
            (sig_notional / (equity * float(planned_lev))) * 100.0
            if (equity > 0.0 and planned_lev > 0 and sig_notional > 0.0)
            else None
        )
        bar_tf = (
            str(cs.tf_label or "")
            if on_cog
            else self._bar_tf_label(self._base_step_seconds(chart_bars))
        )
        self._notify_signal(
            (sym, cs.tf_label, hyp.event_id, t, hyp.direction, d.route),
            size_note="降档触发" if mic else "本周期触发",
            exec_status=self._signal_status_text(
                block, tf_label=cs.tf_label, kind=d.kind, watch_entry=is_watch
            ),
            symbol=sym, tf_label=cs.tf_label, bar_tf=bar_tf,
            event_id=hyp.event_id, side=side, route=d.route,
            ref_price=entry_ref, invalid=invalid, target_1=hyp.target_1,
            regime=regime, bars=chart_bars, box_lo=box_lo, box_hi=box_hi,
            zone_lo=zone_lo, zone_hi=zone_hi,
            zone_touches=d.meta.get("zone_touches"),
            level_kind=d.meta.get("level_kind"),
            level_role=d.meta.get("level_role"),
            pivots=chart_pivots, atr=getattr(view, "atr", None),
            decision_ts=confirm_ts, watch_details=watch_details,
            price_context={
                **d.meta,
                "box_start_ts": box_start_ts,
                "snapshot_price": snapshot_px,
                "snapshot_ts": snapshot_ts,
                "snapshot_source": "lastPr",
                "suggested_leverage": planned_lev,
                "suggested_margin_pct": sig_margin_pct,
                "suggested_risk_pct": round(sig_rf * 100.0, 2),
                "max_allowed_leverage": self._max_allowed_leverage(sym),
            },
        )
        self._journal(
            "signal_structure",
            {
                "symbol": sym, "tf": cs.tf_label, "event": hyp.event_id,
                "confirm_ts": confirm_ts, "entry_ref": entry_ref,
                "invalid": invalid, "target_1": hyp.target_1,
                "box_lo": box_lo, "box_hi": box_hi, "box_start_ts": box_start_ts,
                "snapshot_price": snapshot_px, "snapshot_ts": snapshot_ts,
                "exec_block": block, **d.meta,
            },
        )
        return planned_lev

    def _submit_open(
        self,
        cs: CampaignState,
        d: Any,
        t: int,
        *,
        mic: bool,
        view: StructureView | None = None,
        trigger_bar: Sequence[Any] | None = None,
        promote_from_pos: dict[str, Any] | None = None,
    ) -> None:
        """意图 → 闸门 → 信号广播 → 装配(执行在撮合/live 下单层)。"""
        sym = cs.symbol
        hyp = d.hyp
        side = "buy" if hyp.direction > 0 else "sell"
        entry_ref = float(d.limit_price) if d.kind == "limit" else float(d.intent.ref_price)
        is_pyramid = bool(d.meta.get("pyramid_add"))
        invalid = (
            float(d.meta["base_invalid"])
            if is_pyramid and d.meta.get("base_invalid") is not None
            else float(hyp.invalid)
        )
        risk_per_unit = abs(entry_ref - invalid)
        try:
            cost_limited = not float(d.meta.get("max_cost_frac") or 0.0) <= 0.0
        except (TypeError, ValueError):
            cost_limited = True
        fitted_entry = (
            self.fit_price(sym, entry_ref, hyp.direction)
            if self.mode == "live" and d.kind == "limit" and not d.meta.get("watch_entry")
            else entry_ref
        )
        edge_mult = edge_size_multiplier(
            self.risk.get("edge_size_by_event_direction"), hyp.event_id, hyp.direction
        )
        size_mult = float(d.meta.get("size_mult") or 1.0) * edge_mult
        mark_mult = 1.0
        if (not is_pyramid) and getattr(self, "markowitz_corr_sizing", False):
            mark_mult = self._markowitz_corr_mult(sym, int(hyp.direction))
            d.meta["markowitz_mult"] = mark_mult
        size_mult = cap_effective_size_multiplier(
            size_mult, mark_mult, getattr(self, "max_eff_size_mult", None)
        )
        equity = float(self.equity)
        capital = max(0.0, equity - float(self.unrealized_pl or 0.0))
        eff_rf = self.current_risk_frac(capital)

        box_lo, box_hi = self._box_bounds(
            d.meta.get("zone_lo"), d.meta.get("zone_hi"), d.meta.get("box_opposite")
        )
        box_start_ts = self._resolve_box_start_ts(cs, d.meta)
        chart_bars, on_cog = self._signal_chart_bars(cs, mic)
        period_s = (
            getattr(cs, "trigger_tf_seconds", 0)
            if mic
            else getattr(cs, "obs_tf_seconds", TF_SECONDS_MAP.get(cs.tf_label, 0))
        )
        confirm_ts = int(d.meta.get("confirm_ts") or (t + int(period_s) * 1000))
        snapshot_px, snapshot_ts, execution_quote, quote = self._fetch_signal_quote(
            sym, int(hyp.direction)
        )
        chart_pivots = self._chart_pivots(getattr(view, "pivots", ())) if on_cog else []

        block, qty, candidate, size_mult, risk_per_unit = self._size_and_gate_entry(
            cs, d,
            entry_ref=entry_ref, fitted_entry=fitted_entry, invalid=invalid,
            risk_per_unit=risk_per_unit, size_mult=size_mult, eff_rf=eff_rf,
            equity=equity, capital=capital, confirm_ts=confirm_ts,
            execution_quote=execution_quote, quote=quote, cost_limited=cost_limited,
            is_pyramid=is_pyramid, mic=mic, trigger_bar=trigger_bar,
            promote_from_pos=promote_from_pos,
        )
        oid: str | None = None
        if block is None:
            oid = client_oid_of(self.OID_PREFIX, sym, hyp.event_id, side, t)
            if self.open_intents.get(oid, {}).get("status") in ("armed", "filled", "error"):
                block = "DUPLICATE"

        is_watch = bool(d.meta.get("watch_entry"))
        wait_h = int(d.meta.get("max_wait_hours") or N_FILL_H) if is_watch else N_FILL_H
        watch_details = self._build_watch_details(d, hyp, entry_ref, is_watch=is_watch, wait_h=wait_h)
        planned_lev = self._plan_and_broadcast_signal(
            cs, d,
            t=t, side=side, entry_ref=entry_ref, invalid=invalid,
            block=block, qty=qty, candidate=candidate, eff_rf=eff_rf,
            edge_mult=edge_mult, equity=equity, capital=capital, confirm_ts=confirm_ts,
            box_lo=box_lo, box_hi=box_hi, box_start_ts=box_start_ts,
            snapshot_px=snapshot_px, snapshot_ts=snapshot_ts,
            chart_bars=chart_bars, on_cog=on_cog, chart_pivots=chart_pivots,
            view=view, mic=mic, is_watch=is_watch, watch_details=watch_details,
        )
        if block is not None or oid is None:
            self._note_block(sym, cs, hyp, str(block), qty=qty, ref=entry_ref)
            return
        if promote_from_pos is not None and sym in self.positions:
            cur_pos = self.positions[sym]
            low_cs = self._get_cs_for(sym, cur_pos.get("tf_label"))
            low_pm = low_cs.pm.positions.get(sym) if low_cs is not None else None
            if low_cs is not None and low_pm is not None:
                ref_close = float(trigger_bar[4]) if trigger_bar is not None else float(entry_ref)
                self._sub_bar_close_out(
                    low_cs, low_pm, cur_pos, t, ref_close,
                    why="scale_promoted", route=f"SCALE_PROMOTED|to={cs.tf_label}",
                    executor="local_market",
                )
                if sym in self.positions:
                    return
                self.counters["scale_promotions"] = self.counters.get("scale_promotions", 0) + 1
        self.counters["signals"] += 1
        intent = self._build_entry_intent(
            cs, d,
            oid=oid, t=t, qty=qty, planned_lev=planned_lev,
            entry_ref=entry_ref, invalid=invalid, risk_per_unit=risk_per_unit,
            size_mult=size_mult, edge_mult=edge_mult,
            box_lo=box_lo, box_hi=box_hi, box_start_ts=box_start_ts,
            is_pyramid=is_pyramid, is_watch=is_watch, wait_h=wait_h,
            confirm_ts=confirm_ts, mic=mic, cost_limited=cost_limited,
        )
        self.open_intents[oid] = intent
        if self.mode == "live":
            self._submit_live_entry_order(
                sym, d, intent,
                oid=oid, side=side, qty=qty, planned_lev=planned_lev,
                entry_ref=entry_ref, fitted_entry=fitted_entry,
                equity=equity, t=t, is_watch=is_watch,
            )
        self._journal(
            "intent",
            {
                k: intent[k]
                for k in (
                    "oid", "symbol", "role", "kind", "direction", "qty",
                    "limit_price", "ref_px", "invalid", "tf_label", "event_id",
                    "route", "decision_ts", "mic", "size_mult", "edge_mult",
                    "watch_entry", "watch_price", "near_price",
                )
            },
        )
        if is_watch and intent["status"] == "armed":
            self._notify_watch(
                intent,
                f"关注已建立({self.mode}，尚未下单)",
                f"关注价 {entry_ref:.8g}；等待 {wait_h} 小时；非交易所挂单",
            )

    def _live_entry_filled(self, it: dict, *, side_fallback_price: float,
                           now_ms: int) -> None:
        """live 市价开仓:先按参考价记账建仓,再由 `_settle_entries` 用成交回执
        校正 `entry`(参考价与成交价的差不再系统性污染 R);限价单留 armed 待
        `_sync_live_fills` 确认(那条路径拿到的就是成交价)。"""
        if it["kind"] == "limit" and not it.get("watch_entry"):
            return
        price = float(side_fallback_price)
        risk = it["direction"] * (price - it["invalid"])
        if risk <= 0.0:
            self.tg.send_alert(f"{it['symbol']} 开仓参考价穿越失效位,人工核查")
            it["status"] = "void"
            return
        # `now_ms` 是决策 K 线时间,常常早于真实下单数小时。持仓 opened_ts 继续用它
        # (持仓管理按 bar 对齐)。回执窗口只用墙上时钟,否则登记时截止时间已经过期。
        wall_ms = int(time.time() * 1000)
        it["status"], it["fill_price"], it["fill_ts"] = "filled", price, wall_ms
        it["ref_px"] = price
        it["fill_price_source"] = "ref"
        is_pyr = bool(it.get("pyramid_add"))
        prev_pos = self.positions.get(it["symbol"]) if is_pyr else None
        pre_add_qty = float(prev_pos["qty"]) if prev_pos is not None else 0.0
        pre_add_entry = float(prev_pos["entry"]) if prev_pos is not None else 0.0
        base_ev = str(prev_pos.get("event_id")) if prev_pos is not None else str(it.get("event_id"))
        self._register_position(it["symbol"], it, price, int(now_ms))
        if self.mode == "live" and it.get("exchange_order_id"):
            rec = {
                "oid": it["oid"], "order_id": str(it["exchange_order_id"]),
                "event_id": str(it.get("event_id")),
                "ref_px": price, "direction": int(it["direction"]),
                "submitted_ms": wall_ms,
                "deadline_ms": wall_ms + 180_000,
            }
            if is_pyr and prev_pos is not None:
                rec["pyramid_add"] = True
                rec["base_event_id"] = base_ev
                rec["old_qty"] = pre_add_qty
                rec["old_entry"] = pre_add_entry
                rec["add_qty"] = float(it["qty"])
            self._entry_settle[it["symbol"]] = rec

    def _notify_watch(self, it: Mapping[str, Any], status: str, reason: str) -> None:
        """Best-effort presentation only; never mutate intent/trading state."""
        with suppress(Exception):
            self.tg.send_watch(str(it["symbol"]), str(it.get("tf_label") or "HTF"), status, reason)

    @staticmethod
    def _index_watch_tickers(
        raw_tickers: Sequence[Mapping[str, Any]],
        now_ms: int,
    ) -> tuple[dict[str, tuple[float, float, float]], dict[str, dict[str, Any]]]:
        px_by_sym: dict[str, tuple[float, float, float]] = {}
        quote_by_sym: dict[str, dict[str, Any]] = {}
        for row in raw_tickers:
            s_name = str(row.get("symbol") or "")
            lp = float(row.get("lastPr") or row.get("markPrice") or 0.0) if s_name else 0.0
            if lp <= 0.0:
                continue
            bp = float(row.get("bidPr") or lp)
            ap = float(row.get("askPr") or lp)
            px_by_sym[s_name] = (lp, bp, ap)
            quote_by_sym[s_name] = {
                "bid": float(row.get("bidPr") or 0.0) or None,
                "ask": float(row.get("askPr") or 0.0) or None,
                "last": lp,
                "index": float(row.get("indexPrice") or 0.0) or None,
                "ts": int(row.get("ts") or now_ms or 0) or now_ms,
            }
        return px_by_sym, quote_by_sym

    @staticmethod
    def _watch_prog_price(it: Mapping[str, Any], direction: int) -> float | None:
        prog_px = it.get("prog_price")
        if prog_px is not None:
            return float(prog_px)
        max_prog, c0, tgt = it.get("max_prog_frac"), it.get("confirm_close"), it.get("target_1")
        if max_prog is None or tgt is None or c0 is None:
            return None
        span_rem = abs(float(tgt) - float(c0))
        if span_rem <= 0.0:
            return None
        return float(c0) + direction * float(max_prog) * span_rem

    def _evaluate_watch_intent(
        self,
        oid: str,
        it: dict[str, Any],
        px_by_sym: Mapping[str, tuple[float, float, float]],
        quote_by_sym: Mapping[str, dict[str, Any]],
        now_ms: int,
    ) -> None:
        sym = str(it["symbol"])
        if sym in self.positions:
            it["status"] = "canceled"
            self._journal("watch_cancel", {"oid": oid, "symbol": sym, "why": "POSITION_ALREADY_OPEN"})
            self._notify_watch(it, "关注作废", "该品种已有持仓")
            return
        last_p, bid_p, ask_p = px_by_sym.get(sym, (0.0, 0.0, 0.0))
        if last_p <= 0.0:
            return
        d, inv = int(it["direction"]), float(it["invalid"])
        if (d > 0 and last_p <= inv) or (d < 0 and last_p >= inv):
            it["status"] = "canceled"
            self.counters["watch_canceled_invalid"] = self.counters.get("watch_canceled_invalid", 0) + 1
            self._journal(
                "watch_cancel",
                {"oid": oid, "symbol": sym, "why": "ZONE_BROKEN", "price": last_p, "invalid": inv},
            )
            self._notify_watch(it, "关注作废", "价格已越过失效位")
            return
        watch_px = float(it.get("watch_price") or it.get("limit_price") or it["ref_px"])
        touched_watch = (last_p <= watch_px) if d > 0 else (last_p >= watch_px)
        prog_f = self._watch_prog_price(it, d)
        if not touched_watch and prog_f is not None:
            if (last_p >= prog_f) if d > 0 else (last_p <= prog_f):
                it["status"] = "canceled"
                self.counters["watch_canceled_played_out"] = self.counters.get("watch_canceled_played_out", 0) + 1
                self._journal(
                    "watch_cancel",
                    {"oid": oid, "symbol": sym, "why": "MOVE_ALREADY_PLAYED_OUT", "price": last_p, "prog_px": prog_f},
                )
                self._notify_watch(it, "关注作废", "未触发前行情已走完")
                return
        trigger_why: str | None = "WATCH_INNER_EDGE_HIT" if touched_watch else None
        if trigger_why is None and it.get("near_price") is not None:
            near_f = float(it["near_price"])
            if (last_p <= near_f) if d > 0 else (last_p >= near_f):
                it["near_touched"] = True
                prev_ext = it.get("near_extreme")
                it["near_extreme"] = (
                    last_p
                    if prev_ext is None
                    else (min(float(prev_ext), last_p) if d > 0 else max(float(prev_ext), last_p))
                )
            elif it.get("near_touched"):
                ext_f = float(it.get("near_extreme") if it.get("near_extreme") is not None else near_f)
                band_span = max(abs(near_f - watch_px), 1e-12)
                bounced = (
                    (last_p > near_f and (last_p - ext_f) >= 0.10 * band_span)
                    if d > 0
                    else (last_p < near_f and (ext_f - last_p) >= 0.10 * band_span)
                )
                if bounced:
                    trigger_why = "WATCH_NEAR_BOUNCE"
        if trigger_why is not None:
            self._trigger_watch_entry_live(
                oid, it, ask_p if d > 0 else bid_p, now_ms, trigger_why,
                quote=quote_by_sym.get(sym),
            )

    def _poll_watch_entries(self, now_ms: int) -> None:
        """实盘轮询监控系统价格关注(WatchEntry)。"""
        watched = [
            (oid, it)
            for oid, it in list(self.open_intents.items())
            if it.get("status") == "armed" and it.get("role") == "entry" and it.get("watch_entry")
        ]
        if not watched:
            return
        tickers_fn = getattr(self.client, "tickers", None)
        if not callable(tickers_fn):
            return
        try:
            raw_tickers = tickers_fn() or []
        except BitgetError as exc:
            self._api_note(exc)
            return
        px_by_sym, quote_by_sym = self._index_watch_tickers(raw_tickers, now_ms)
        for oid, it in watched:
            self._evaluate_watch_intent(oid, it, px_by_sym, quote_by_sym, now_ms)

    def _check_watch_capacity_and_gates(
        self,
        oid: str,
        it: dict[str, Any],
        qty: float,
        exec_px: float,
        inv: float,
        now_ms: int,
    ) -> str | None:
        sym = str(it["symbol"])
        d = int(it["direction"])
        eq_now = float(self.equity or 0.0)
        cap_now = max(0.0, eq_now - float(self.unrealized_pl or 0.0))
        fee_slip = float(self.risk["taker_fee"]) + float(self.risk.get("slippage_per_side", 0.0))
        cand_exp = qty * (abs(exec_px - inv) + exec_px * 2.0 * fee_slip)
        if cap_now > 0.0:
            outstanding = sum(self._position_worst_case(p, fee_slip) for p in self.positions.values())
            if outstanding + cand_exp > float(self.risk["portfolio_cap_frac"]) * cap_now + 1e-9:
                return "PORTFOLIO_CAP"
            tf_caps = self.risk.get("tf_cap_by_tf") or {}
            if self._is_rwa(sym):
                mult = self.cfg["universe"].get("rwa", {}).get("capacity_multiplier", 1.0)
                tf_caps = {k: v * mult for k, v in tf_caps.items()}
            tf_lbl = str(it.get("tf_label") or self.cog_cfg.get("main_tf_label") or "12h")
            tf_cap = tf_caps.get(tf_lbl)
            if tf_cap is not None:
                tf_out = sum(
                    self._position_worst_case(p, fee_slip)
                    for p in self.positions.values()
                    if p.get("tf_label", "12h") == tf_lbl
                )
                if tf_out + cand_exp > float(tf_cap) * cap_now + 1e-9:
                    return "TF_CAP"
        if self._directional_entry_blocked(d, qty, exec_px, inv, symbol=sym, exclude_oid=oid):
            return "DIRECTIONAL_CAP"
        return self._factor_burst_blocked(sym, d, int(now_ms), exclude_oid=oid)

    def _trigger_watch_entry_live(
        self,
        oid: str,
        it: dict[str, Any],
        exec_px: float,
        now_ms: int,
        trigger_why: str,
        quote: dict[str, Any] | None = None,
    ) -> None:
        sym = str(it["symbol"])
        d, inv = int(it["direction"]), float(it["invalid"])
        if sym in self._manage_only:
            it["status"] = "canceled"
            self._journal("watch_cancel", {"oid": oid, "symbol": sym, "why": "MANAGE_ONLY"})
            self._notify_watch(it, "关注作废", "该标的仅出场管理,不再新开仓")
            return
        error = entry_contract_error(d, exec_px, inv, it.get("target_1"), it)
        if error:
            it["status"] = "canceled"
            self._journal("watch_cancel", {"oid": oid, "symbol": sym, "why": error, "exec_px": exec_px})
            self._notify_watch(it, "关注作废", error)
            return
        if d * (exec_px - inv) <= 0.0:
            it["status"] = "canceled"
            self._notify_watch(it, "关注作废", "执行参考价已越过失效位")
            return
        spec = self._spec(sym)
        risk_cap = float(it.get("risk_usdt_cap") or 0.0)
        qty = float(it["qty"])
        if risk_cap > 0.0 and abs(exec_px - inv) > 0.0:
            qty = min(qty, floor_step(risk_cap / abs(exec_px - inv), spec.size_step, spec.size_place))
        if qty <= 0.0 or qty * exec_px < max(spec.min_trade_usdt, spec.size_min * exec_px):
            return
        if rwa_session_entry_block(self._is_rwa(sym), int(now_ms), symbol=sym) is not None:
            return
        if quote is not None and self._quote_guard_block(sym, quote) is not None:
            return
        cap_block = self._check_watch_capacity_and_gates(oid, it, qty, exec_px, inv, now_ms)
        if cap_block is not None:
            it["status"] = "canceled"
            self._journal("watch_cancel", {"oid": oid, "symbol": sym, "why": cap_block})
            self._notify_watch(it, "关注作废", cap_block)
            return
        if self.breaker.should_stop(self.equity) or not self.rate.allow():
            return
        try:
            eq_now = float(self.equity or 0.0)
            cap_now = max(0.0, eq_now - float(self.unrealized_pl or 0.0))
            planned_lev = int(
                it.get("leverage")
                or self._plan_trade_leverage(sym, qty * exec_px, eq_now, cap_now, qty * abs(exec_px - inv))
            )
            try:
                self._ensure_leverage(sym, planned_lev)
            except TypeError:
                self._ensure_leverage(sym)
            it["qty"], it["kind"], it["watch_trigger"] = qty, "market", trigger_why
            resp = self.client.place_market_order(
                sym, side="buy" if d > 0 else "sell", size=qty, client_oid=oid,
                trade_side=self.open_trade_side(),
                **self._entry_protection(sym, it),
            )
            it["exchange_order_id"] = str(resp.get("orderId") or "")
            self.counters["orders_placed"] += 1
            self.counters["watch_triggered"] = self.counters.get("watch_triggered", 0) + 1
            self._journal(
                "watch_trigger",
                {"oid": oid, "symbol": sym, "why": trigger_why, "exec_px": exec_px, "qty": qty},
            )
            self._notify_watch(it, "市价委托已提交（非成交确认）", trigger_why)
            self._live_entry_filled(it, side_fallback_price=exec_px, now_ms=now_ms)
        except BitgetError as exc:
            it["status"] = "error"
            self.counters["order_errors"] = self.counters.get("order_errors", 0) + 1
            self._journal(
                "order_error",
                {"oid": oid, "symbol": sym, "event": it.get("event_id"), "why": str(exc)[:160]},
            )
            self._api_note(exc)

    def _entry_protection(self, sym: str, it: Mapping[str, Any]) -> dict[str, Any]:
        if self.price_clock != "realtime":
            return {}
        return {"preset_stop_loss": self.fit_price(sym, float(it["invalid"]), int(it["direction"]))}

    def _register_pyramid_add(
        self,
        sym: str,
        cs: CampaignState | None,
        prev_pos: dict[str, Any],
        it: dict[str, Any],
        price: float,
        ts: int,
        tf_lbl: str,
    ) -> None:
        old_qty = float(prev_pos["qty"])
        add_qty = float(it["qty"])
        tot_qty = old_qty + add_qty
        avg_entry = (
            (old_qty * float(prev_pos["entry"]) + add_qty * float(price)) / tot_qty
            if tot_qty > 0.0
            else float(price)
        )
        prev_pos["qty"] = tot_qty
        prev_pos["entry"] = avg_entry
        prev_pos["mark"] = float(price)
        prev_pos["adds_done"] = int(prev_pos.get("adds_done") or 0) + 1
        if cs is not None and sym in cs.pm.positions:
            pm_p = cs.pm.positions[sym]
            pm_p.qty = tot_qty
            if int(pm_p.direction) * (avg_entry - float(pm_p.invalid)) > 0.0:
                cs.pm.retag_entry(sym, avg_entry)
        if prev_pos.get("target_order") and self.mode == "live" and self.price_clock == "realtime":
            t_ord = prev_pos.pop("target_order", None)
            if t_ord and t_ord.get("order_id"):
                try:
                    self.client.cancel_tpsl_order(sym, t_ord["order_id"])
                except BitgetError as exc:
                    self._journal("target_cancel_error", {"symbol": sym, "why": str(exc)[:160]})
            prev_pos["target_generation"] = int(prev_pos.get("target_generation", 0)) + 1
            if cs is not None and sym in cs.pm.positions:
                cs.pm.positions[sym].pending_reduce = False
        self._touch_campaigns()
        if self.mode == "live" and self.price_clock == "realtime":
            self._manage_stops(ts)
            self._manage_targets(ts)
        self.counters["pyramid_adds"] = self.counters.get("pyramid_adds", 0) + 1
        chart_bars = (
            cs.trig_bars[-_CHART_BAR_WINDOW:]
            if (cs and it.get("mic") and cs.trig_bars)
            else (cs.cog_bars[-_CHART_BAR_WINDOW:] if (cs and cs.cog_bars) else None)
        )
        self.tg.send_fill(
            sym, f"顺势加仓({self.mode})", add_qty, price,
            note=f"PYRAMID_ADD {it['event_id']} · 总仓 {tot_qty:.6g} · 均价 {avg_entry:.6g}",
            tf_label=tf_lbl, direction=it["direction"],
            entry_price=avg_entry, stop_price=float(prev_pos["invalid"]),
            target_price=prev_pos.get("target_1"),
            box_lo=prev_pos.get("box_lo"), box_hi=prev_pos.get("box_hi"),
            box_start_ts=prev_pos.get("box_start_ts"),
            bars=chart_bars,
        )
        self._journal(
            "pyramid_add_fill",
            {
                "oid": it["oid"], "symbol": sym, "add_qty": add_qty, "total_qty": tot_qty,
                "fill_px": float(price), "avg_entry": avg_entry,
                "invalid": float(prev_pos["invalid"]), "direction": int(it["direction"]),
                "tf_label": tf_lbl, "ts": int(ts), "event_id": it["event_id"], "mode": self.mode,
            },
        )

    def _notify_position_registered(
        self,
        sym: str,
        cs: CampaignState | None,
        it: Mapping[str, Any],
        price: float,
        tf_lbl: str,
        origin: str,
    ) -> None:
        chart_bars = (
            cs.trig_bars[-_CHART_BAR_WINDOW:]
            if (cs and it.get("mic") and cs.trig_bars)
            else (
                cs.cog_bars[-_CHART_BAR_WINDOW:]
                if (cs and cs.cog_bars)
                else (cs.trig_bars[-_CHART_BAR_WINDOW:] if cs else None)
            )
        )
        if origin == "adopted":
            self.tg.send_alert(
                f"🛠 {sym} 接管交易所既有仓位 dir={it['direction']} "
                f"qty={it['qty']} @ {price:.8g} —— 已按新策略结构挂保护:"
                f"失效 {float(it['invalid']):.8g}"
                f"({'struct' if it.get('stop_source') == 'structure' else 'atr'})"
                f", 目标 {it.get('target_1')}；此后由 §8 统一管理"
            )
            return
        if origin == "recovered":
            self.tg.send_alert(
                f"🧾 {sym} 账本重建:按审计日志的原始成交记录恢复持仓事实 "
                f"dir={it['direction']} qty={it['qty']} @ {price:.8g} "
                f"(原始防线 {float(it['invalid']):.8g},目标 {it.get('target_1')})"
                " —— 非新开仓;交易所既有 TPSL 按确定性 clientOid 直接沿用"
            )
            return
        is_pending_ref = self.mode == "live" and it.get("fill_price_source") != "fills"
        note = (
            f"{it['event_id']} {it['route']}"
            + (
                f" · 结构目标 {it['target_1']} 仅观察，不触发锁利/减仓"
                if it.get("target_1") is not None and execution_target(it, it["target_1"]) is None
                else ""
            )
            + (
                f" · 系统Watch触发 {it.get('watch_trigger') or '价格条件满足'}"
                if it.get("watch_entry")
                else ""
            )
            + (" · 参考价暂记，成交均价待回执校正" if is_pending_ref else "")
        )
        self.tg.send_fill(
            sym,
            f"开仓暂记({self.mode},待成交回执)" if is_pending_ref else f"开仓({self.mode})",
            it["qty"],
            price,
            note=note,
            tf_label=tf_lbl,
            direction=it["direction"],
            price_label="参考价(待成交回执)" if is_pending_ref else "成交价格",
            stop_price=float(it["invalid"]),
            target_price=it.get("target_1"),
            box_lo=it.get("box_lo"),
            box_hi=it.get("box_hi"),
            box_start_ts=it.get("box_start_ts"),
            bars=chart_bars,
        )

    def _register_position(
        self,
        sym: str,
        it: dict[str, Any],
        price: float,
        ts: int,
        *,
        origin: str = "strategy",
    ) -> None:
        """建仓事实入库 + pm 建仓(策略链路与接管共用同一份持仓格式)。"""
        tf_lbl = it.get("tf_label") or str(self.cog_cfg.get("main_tf_label") or "12h")
        cs = self._get_cs_for(sym, tf_lbl) or self.campaigns.get(sym)
        if cs is not None and not it.get("tf_label"):
            tf_lbl = cs.tf_label
        prev_pos = self.positions.get(sym) if it.get("pyramid_add") else None
        if prev_pos is not None and int(prev_pos.get("direction") or 0) == int(it["direction"]):
            self._register_pyramid_add(sym, cs, prev_pos, it, price, ts, tf_lbl)
            return
        reenter_px = it.get("reenter_invalid_px")
        if reenter_px is None and it.get("box_lo") is not None and it.get("box_hi") is not None:
            reenter_px = float(it["box_lo"]) if int(it["direction"]) > 0 else float(it["box_hi"])
        elif reenter_px is not None:
            reenter_px = float(reenter_px)
        self.positions[sym] = {
            "symbol": sym, "tf_label": tf_lbl, "direction": it["direction"],
            "qty": it["qty"], "entry": price, "invalid": it["invalid"],
            "reenter_invalid_px": reenter_px,
            "leverage": int(it.get("leverage") or self.leverage_by_sym.get(sym) or self._max_allowed_leverage(sym)),
            "target_1": execution_target(it, it["target_1"]), "event_id": it["event_id"],
            "structural_target": it.get("structural_target", it["target_1"]),
            "box_lo": it.get("box_lo"), "box_hi": it.get("box_hi"),
            "box_start_ts": it.get("box_start_ts"),
            "level_kind": it.get("level_kind"), "level_role": it.get("level_role"),
            "is_rwa": self._is_rwa(sym),
            "protection_required": bool(origin == "strategy" and self._require_exchange_protection()),
            "opened_ts": ts, "mark": price,
            "entry_invalid": float(it["invalid"]), "entry_qty": float(it["qty"]),
            "price_source": str(it.get("fill_price_source") or "ref"),
        }
        self._price_last.pop(sym, None)
        self._price_seen.pop(sym, None)
        self._touch_campaigns()
        if cs is not None:
            risk = it["direction"] * (price - it["invalid"])
            if risk > 0.0:
                try:
                    cs.pm.open(
                        sym, it["direction"], price, it["invalid"],
                        execution_target(it, it["target_1"]), it["event_id"], ts,
                        len(cs.cog_bars), reenter_invalid_px=reenter_px,
                    )
                except ValueError as exc:
                    self.tg.send_alert(f"{sym} pm 建仓拒绝:{exc}")
        if self.mode == "live" and self.price_clock == "realtime":
            self._manage_stops(ts)
            self._manage_targets(ts)
        if origin == "adopted":
            self.counters["adopted"] = self.counters.get("adopted", 0) + 1
        elif origin == "recovered":
            self.counters["recovered_positions"] = self.counters.get("recovered_positions", 0) + 1
        else:
            self.counters["fills"] += 1
        self._notify_position_registered(sym, cs, it, price, tf_lbl, origin)
        kind = "adopt" if origin == "adopted" else ("fill_recovered" if origin == "recovered" else "fill")
        self._journal(
            kind,
            {
                "oid": it["oid"], "symbol": sym,
                "qty": it["qty"], "price": price,
                "ref_px": float(it.get("ref_px") or price),
                "fill_px": float(it.get("fill_price") or price),
                "price_source": str(it.get("fill_price_source") or "ref"),
                "invalid": float(it["invalid"]), "reenter_invalid_px": reenter_px,
                "direction": int(it["direction"]), "tf_label": tf_lbl,
                "opened_ts": int(ts), "event_id": it["event_id"],
                "structural_target": it.get("structural_target", it.get("target_1")),
                "execution_target": execution_target(it, it.get("target_1")),
                "role": "entry", "mode": self.mode, "origin": origin,
                "stop_source": it.get("stop_source"),
                "target_source": it.get("target_source"),
                "risk_atr": it.get("risk_atr"),
                "adopted_breached": it.get("adopted_breached"),
            },
        )

    # ------------------------------------------------------------------
    # §8 持仓管理意图(REDUCE/EXIT)
    # ------------------------------------------------------------------

    def _exec_mgmt(self, cs: CampaignState, intent: Any, cog_bar: Sequence[Any]) -> None:
        sym = cs.symbol
        pos = self.positions.get(sym)
        pos_pm = cs.pm.positions.get(sym)
        if pos is None:
            return
        kind = intent.kind.value
        t = int(cog_bar[0])
        self.counters["mgmt_execs"] += 1
        if kind == "EXIT":
            self._cancel_entry(sym, "position_exit")
            self._cancel_stop(sym, "position_exit")
            spec = self._spec(sym)
            self._arm_close_intent(
                sym, pos,
                floor_step(float(pos["qty"]), spec.size_step, spec.size_place),
                "market", None, intent.reason, t, role="exit", force=self._gap_replay,
            )
        elif kind == "REDUCE":
            qty = floor_step(
                float(intent.qty_frac or 0.0) * pos["qty"],
                self._spec(sym).size_step,
                self._spec(sym).size_place,
            )
            if qty <= 0.0:
                return
            target = pos_pm.target_1 if pos_pm is not None else None
            partial_market = (
                str(intent.reason).startswith("v16|target_partial|")
                or "partial_exit" in str(intent.reason)
            )
            gap_market = (
                self._gap_replay
                and str((self.loop_cfg.get("gap_recovery") or {}).get("reduce_policy", "market")).lower() == "market"
            )
            if target is not None and not partial_market and not gap_market:
                self._arm_close_intent(
                    sym, pos, qty, "limit", float(target), intent.reason, t, role="reduce", force=gap_market
                )
            else:
                self._arm_close_intent(
                    sym, pos, qty, "market", None, intent.reason, t, role="reduce", force=gap_market
                )

    def _arm_close_intent(
        self,
        sym: str,
        pos: dict[str, Any],
        qty: float,
        kind: str,
        limit_price: float | None,
        reason: str,
        t: int,
        *,
        role: str,
        force: bool = False,
    ) -> None:
        side = self.close_side_of(pos["direction"])
        oid = client_oid_of(self.OID_PREFIX, sym, str(reason)[:16], side, t, role=role)[:36]
        if self.open_intents.get(oid, {}).get("status") in ("armed", "filled", "error"):
            return
        if not force and not self.rate.allow():
            self.counters["rate_skips"] += 1
            return
        it: dict[str, Any] = {
            "oid": oid, "symbol": sym, "role": role, "kind": kind,
            "direction": -pos["direction"], "qty": qty,
            "limit_price": limit_price, "invalid": pos["invalid"],
            "target_1": pos["target_1"], "event_id": pos["event_id"],
            "route": f"mgmt:{str(reason)[:40]}", "size_mult": 1.0,
            "decision_ts": t, "mic": False,
            "expiry_ts": t + N_FILL_H * MS_1H, "status": "armed",
            "exchange_order_id": None,
        }
        self.open_intents[oid] = it
        if self.mode == "live":
            try:
                if kind == "limit":
                    resp = self.client.place_limit_order(
                        sym, side=side, size=qty,
                        price=self.fit_price(sym, float(limit_price), it["direction"]),
                        client_oid=oid, trade_side=self.close_trade_side(),
                    )
                else:
                    resp = self.client.place_market_order(
                        sym, side=side, size=qty, client_oid=oid, trade_side=self.close_trade_side()
                    )
                it["exchange_order_id"] = str(resp.get("orderId") or "")
                self.counters["orders_placed"] += 1
                if kind == "market":
                    self._live_close_filled(it, ref_price=pos.get("mark") or pos["entry"])
            except BitgetError as exc:
                it["status"] = "error"
                self.counters["order_errors"] = self.counters.get("order_errors", 0) + 1
                why = str(exc)
                self._journal("order_error", {"oid": oid, "symbol": sym, "role": role, "why": why[:160]})
                if role == "exit" and ("22002" in why or "暂无仓位可平" in why or "43023" in why):
                    self.counters["close_no_pos"] = self.counters.get("close_no_pos", 0) + 1
                    self._journal("close_no_pos", {"symbol": sym, "role": role, "why": why[:160]})
                    self.stops.pop(sym, None)
                    self._reconcile_closed(sym, dict(pos))
                    self.tg.send_alert(
                        f"{sym} 交易所已无仓位可平(22002/43023)⇒ 已按交易所为准清理本地记录(不再重试)"
                    )
                    return
                self.tg.send_alert(f"{sym} 平仓({role})下单失败:{why[:120]}(仓位仍在,人工核查)")
                self._api_note(exc)
        self._journal(
            "mgmt_intent",
            {"symbol": sym, "role": role, "kind": kind, "qty": qty, "reason": str(reason)[:80]},
        )

    def _close_action_label(self, it: Mapping[str, Any]) -> str:
        role = str(it.get("role") or "")
        reason = str(it.get("route") or "")
        if role == "reduce":
            return f"减仓50%并保本({self.mode})" if "partial_exit" in reason else f"减仓({self.mode})"
        return f"平仓({self.mode},{role})"

    def _live_close_filled(
        self,
        it: dict[str, Any],
        *,
        ref_price: float,
        title: str | None = None,
        note: str | None = None,
        price_label: str = "成交价格",
        pnl: float | None = None,
    ) -> None:
        pos = self.positions.get(it["symbol"])
        if pos is None:
            return
        qty = min(it["qty"], pos["qty"])
        if pnl is None:
            gross = qty * pos["direction"] * (float(ref_price) - float(pos["entry"]))
            fee = qty * float(ref_price) * self.risk["taker_fee"]
            pnl = gross - fee
        pos["qty"] = round(pos["qty"] - qty, 10)
        it["status"] = "filled"
        self.counters["reduce_fills" if it["role"] == "reduce" else "exits"] += 1
        cs = self._get_cs_for(it["symbol"], pos.get("tf_label")) or self.campaigns.get(it["symbol"])
        chart_bars = (
            cs.cog_bars[-_CHART_BAR_WINDOW:]
            if (cs and cs.cog_bars)
            else (cs.trig_bars[-_CHART_BAR_WINDOW:] if cs else None)
        )
        self.tg.send_fill(
            it["symbol"], title or self._close_action_label(it),
            qty, float(ref_price), pnl=pnl,
            note=str(it["route"])[:60] if note is None else str(note)[:160],
            tf_label=pos.get("tf_label", cs.tf_label if cs else "12h"),
            direction=pos["direction"],
            entry_price=float(pos["entry"]),
            stop_price=pos.get("invalid"), target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            box_start_ts=pos.get("box_start_ts"),
            bars=chart_bars, price_label=price_label,
        )
        self._journal(
            "close_fill",
            {
                "oid": it["oid"], "symbol": it["symbol"], "qty": qty,
                "role": it["role"], "price": float(ref_price),
                "pnl": pnl, "price_source": it.get("price_source"),
            },
        )
        if it["role"] == "exit" or pos["qty"] <= 1e-9:
            sym = it["symbol"]
            self.positions.pop(sym, None)
            self._missing_streak.pop(sym, None)
            if cs is not None:
                cs.pm.positions.pop(sym, None)
        self._touch_campaigns()

    # ------------------------------------------------------------------
    # live:限价成交同步 / 保护止损 / 撤单 / 过期与超时转市价 / 对账 / 日结
    # ------------------------------------------------------------------

    def _sync_live_fills(self, now_ms: int) -> None:
        for _oid, it in list(self.open_intents.items()):
            if it["status"] != "armed" or it["kind"] != "limit":
                continue
            ex_id = it.get("exchange_order_id")
            if not ex_id:
                continue
            try:
                fills = self.client.recent_fills(it["symbol"], limit=20)
            except BitgetError as exc:
                self._journal("fills_fetch_error", {"symbol": it["symbol"], "why": str(exc)[:120]})
                continue
            matches = [f for f in (fills or []) if str(f.get("orderId")) == ex_id]
            if not matches:
                continue
            tot_sz = 0.0
            vwap_num = 0.0
            for m in matches:
                try:
                    msz = float(m.get("size") or 0.0)
                    mpx = float(m.get("price") or 0.0)
                except (TypeError, ValueError):
                    continue
                if msz > 0.0 and mpx > 0.0:
                    tot_sz += msz
                    vwap_num += msz * mpx
            if tot_sz > 0.0:
                qty, price = tot_sz, vwap_num / tot_sz
            else:
                hit = matches[0]
                qty = float(hit.get("size", it["qty"]))
                price = float(hit.get("price", it.get("limit_price") or 0.0))
            if it["role"] == "entry":
                it["qty"] = qty
                it["status"], it["fill_price"], it["fill_ts"] = "filled", price, now_ms
                it.setdefault("ref_px", float(it.get("limit_price") or price))
                it["fill_price_source"] = "fills"
                self._register_position(it["symbol"], it, price, now_ms)
                self._audit_entry_cost(it, price)
                self._audit_directional_fill(it)
            else:
                self._live_close_filled(it, ref_price=price)

    def _exchange_position_qty(self, sym: str, direction: int) -> float | None:
        """向交易所实时核验单标的同向持仓量;无法查询时返回 None(保持 fail-closed)。"""
        pos_fn = getattr(self.client, "positions", None)
        if not callable(pos_fn):
            return None
        try:
            ex_pos = pos_fn()
        except BitgetError:
            return None
        if not isinstance(ex_pos, list):
            return None
        return sum(
            max(0.0, float(p.get("qty") or 0.0))
            for p in ex_pos
            if str(p.get("symbol")) == sym and int(p.get("direction") or 0) == int(direction)
        )

    def _sync_one_target_order(self, sym: str, pos: dict[str, Any], now_ms: int) -> None:
        cs = self._get_cs_for(sym, pos.get("tf_label"))
        pm = cs.pm.positions.get(sym) if cs else None
        target = pos.get("target_1")
        if pm is not None and pm.target_1 is None and pos.get("target_order"):
            order = pos["target_order"]
            try:
                self.client.cancel_tpsl_order(sym, order["order_id"])
            except BitgetError as exc:
                self._alert_throttled(f"target_cancel:{sym}", str(exc)[:160])
            else:
                pos.pop("target_order", None)
                pos["target_1"] = None
                pm.pending_reduce = False
            return
        if pm is None or cs is None or pm.reduced or target is None:
            return
        part_en = bool(getattr(cs.pm, "partial_exit_enabled", False))
        lock_f = float(getattr(cs.pm, "reject_opp_lock_frac", 0.0) or 0.0)
        full_tp_mode = (not part_en) and lock_f >= 1.0 - 1e-9
        if not part_en and not full_tp_mode:
            return
        if pos.get("target_order") or pos.get("target_tpsl_unsupported"):
            if pos.get("target_order"):
                pm.pending_reduce = True
            return
        spec = self._spec(sym)
        tp_frac = 1.0 if full_tp_mode else 0.5
        qty = floor_step(float(pos["qty"]) * tp_frac, spec.size_step, spec.size_place)
        if qty <= 0:
            self._alert_throttled(f"target_size:{sym}", f"{sym} 半仓止盈数量不足最小精度，未擅自改成全平")
            return
        oid = client_oid_of(
            self.OID_PREFIX, sym, str(pos["event_id"]),
            "target", int(pos["opened_ts"]),
            role=f"target{int(pos.get('target_generation', 0))}",
        )
        try:
            self._pending_stop_ids(now_ms)
            resp = next(
                (
                    row
                    for row in self._stops_live_rows or []
                    if str(row.get("clientOid")) == oid
                    and str(row.get("symbol")) == sym
                    and row.get("planType") == "profit_plan"
                ),
                None,
            )
            if resp is None:
                resp = self.client.place_partial_take_profit(
                    sym,
                    trigger_price=self.fit_price(sym, float(target), -int(pos["direction"])),
                    size=qty,
                    hold_side=self.stop_hold_side(pos["direction"]),
                    client_oid=oid,
                )
            ex_id = str((resp or {}).get("orderId") or "")
            if not ex_id:
                raise BitgetError("target protection missing orderId")
            pos["target_order"] = {
                "order_id": ex_id, "qty": qty, "accounted_qty": 0.0,
                "original_qty": float(pos["qty"]), "size_frac": pm.size_frac,
                "client_oid": oid,
            }
            pm.pending_reduce = True
            self._touch_campaigns()
            self._stops_live_ids, self._stops_live_rows = None, None
            self._journal("target_protection", {"symbol": sym, "order_id": ex_id, "qty": qty, "target": target})
        except BitgetError as exc:
            why = str(exc)
            if _stop_reject_no_position(why) or _stop_requires_local(why):
                ex_qty = self._exchange_position_qty(sym, int(pos["direction"]))
                if ex_qty is not None and ex_qty <= 0.0:
                    self._reconcile_at = 0.0
                    self._journal("target_place_no_pos", {"symbol": sym, "why": why[:160]})
                    return
                if ex_qty is not None and ex_qty > 0.0:
                    pos["qty"] = ex_qty
                    pos["target_tpsl_unsupported"] = True
                    self._journal("target_tpsl_unsupported", {"symbol": sym, "qty": ex_qty, "why": why[:160]})
                    self._alert_throttled(
                        f"target:{sym}",
                        f"{sym} 交易所止盈挂单被拒(43023,持仓 {ex_qty}),已停止重复重试并保留止损",
                    )
                    return
            self._journal("target_protection_error", {"symbol": sym, "why": why[:160]})
            self._alert_throttled(f"target:{sym}", f"{sym} 交易所半仓止盈未建立：{why[:120]}")

    def _manage_targets(self, now_ms: int) -> None:
        """Rest the existing strategy's first 50% (or full) target at the exchange."""
        if self.mode != "live" or self.price_clock != "realtime":
            return
        for sym, pos in list(self.positions.items()):
            self._sync_one_target_order(sym, pos, now_ms)

    def _sync_target_fills(self, now_ms: int) -> None:
        """Only exchange execution facts consume the one-time partial exit."""
        del now_ms
        for sym, pos in list(self.positions.items()):
            order = pos.get("target_order")
            if not order:
                continue
            if float(order.get("accounted_qty", 0.0)) >= float(order["qty"]) - 1e-9:
                continue
            cs = self._get_cs_for(sym, pos.get("tf_label"))
            pm = cs.pm.positions.get(sym) if cs else None
            if pm is None or cs is None:
                continue
            try:
                history = self.client.tpsl_order_history(sym, order["order_id"])
            except BitgetError as exc:
                self._journal("target_sync_error", {"symbol": sym, "why": str(exc)[:160]})
                continue
            row = next((r for r in (history or []) if str(r.get("orderId")) == order["order_id"]), None)
            if row is None:
                continue
            status = str(row.get("planStatus") or "")
            if (
                status in ("cancelled", "fail_execute")
                and not row.get("executeOrderId")
                and float(order.get("accounted_qty", 0.0)) == 0
            ):
                pos.pop("target_order", None)
                pos["target_generation"] = int(pos.get("target_generation", 0)) + 1
                pm.pending_reduce = False
                self._touch_campaigns()
                self._journal("target_rearm", {"symbol": sym, "status": status})
                continue
            if status in ("cancelled", "fail_execute"):
                self._alert_throttled(
                    f"target_terminal:{sym}",
                    f"{sym} 止盈计划终态 {status}，需核对成交，保留止损",
                )
                continue
            execute_id = str(row.get("executeOrderId") or "")
            if not execute_id:
                continue
            fact = self._entry_fill_fact(sym, execute_id)
            if fact is None:
                continue
            px, _ts, total = fact
            total = min(float(total), float(order["qty"]))
            delta = total - float(order.get("accounted_qty", 0.0))
            if delta <= 1e-9:
                continue
            fraction = delta / float(order["original_qty"]) * float(order["size_frac"])
            pm.pending_reduce = True
            cs.pm.confirm_reduce(sym, min(fraction, pm.size_frac))
            pm.armed = True
            pm._best = min(pm._best, px) if pm.direction < 0 else max(pm._best, px)
            pos["invalid"] = pm.invalid
            order["accounted_qty"] = total
            remaining = min(float(pos["qty"]), float(order["original_qty"]) - total)
            pos["qty"] = remaining + delta
            close_role = "exit" if remaining <= 1e-9 else "reduce"
            self._live_close_filled(
                {
                    "oid": order["client_oid"], "symbol": sym, "role": close_role,
                    "qty": delta,
                    "route": f"{'full_tp_exit' if close_role == 'exit' else 'partial_exit'}|exchange target fill",
                    "price_source": "fills",
                },
                ref_price=px,
            )
            if sym in self.positions:
                self.positions[sym]["qty"] = remaining
            pm.pending_reduce = total < float(order["qty"]) - 1e-9
            self._touch_campaigns()

    def _pending_stop_ids(self, now_ms: int) -> set[str] | None:
        """交易所在挂 TPSL orderId 集合(TTL 缓存;取不到返回 None ≡ 不判定)。"""
        ttl_ms = int(
            float((self.loop_cfg.get("gap_recovery") or {}).get("stop_check_seconds", _STOP_TTL_DEFAULT_S)) * 1000
        )
        if self._stops_live_ids is not None and now_ms - self._stops_checked_at < ttl_ms:
            return self._stops_live_ids
        try:
            raw = self.client.tpsl_orders_pending()
        except BitgetError as exc:
            self._api_note(exc)
            self._stops_checked_at = now_ms
            return self._stops_live_ids
        rows = raw.get("entrustedList") if isinstance(raw, dict) else raw
        rows = [r for r in (rows or []) if r.get("orderId")]
        ids = {str(r.get("orderId")) for r in rows}
        self._stops_live_ids, self._stops_live_rows = ids, rows
        self._stops_checked_at = now_ms
        return ids

    def _dedupe_stops(self, now_ms: int) -> None:
        """每个持仓只留一张保护单:清理重复/遗留 TPSL。"""
        del now_ms
        rows = self._stops_live_rows
        if not rows:
            return
        by_sym: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if not str(r.get("clientOid") or "").startswith(self.OID_PREFIX):
                continue
            if r.get("planType") in ("profit_plan", "pos_profit"):
                continue
            by_sym.setdefault(str(r.get("symbol")), []).append(r)
        for sym, items in by_sym.items():
            cur = self.stops.get(sym)
            keep = str(cur.get("order_id") or "") if cur else ""
            if not keep or sym not in self.positions:
                continue
            for r in items:
                oid = str(r.get("orderId"))
                if oid == keep:
                    continue
                try:
                    self.client.cancel_tpsl_order(sym, oid)
                except BitgetError as exc:
                    self._journal("stop_dupe_cancel_error", {"symbol": sym, "order_id": oid, "why": str(exc)[:120]})
                    continue
                self.counters["stop_dupes"] = self.counters.get("stop_dupes", 0) + 1
                self._journal(
                    "stop_dupe_cancel",
                    {"symbol": sym, "order_id": oid, "keep": keep, "trigger": r.get("triggerPrice")},
                )
                self.tg.send_alert(
                    f"{sym} 发现重复保护止损单({oid},触发 {r.get('triggerPrice')})已撤,保留 {keep}"
                )
                self.invalidate_stop_cache()

    def _entry_loss_plan_rows(
        self,
        sym: str,
        pos: Mapping[str, Any],
        rows: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """交易所自动生成的 `loss_plan`(开仓预设止损)候选行。"""
        side = self.stop_hold_side(int(pos["direction"]))
        entry_invalid = pos.get("entry_invalid")
        want_entry: float | None = None
        if entry_invalid is not None:
            try:
                want_entry = self.fit_price(sym, float(entry_invalid), int(pos["direction"]))
            except (TypeError, ValueError):
                want_entry = None
        out: list[dict[str, Any]] = []
        for row in rows or ():
            if (
                str(row.get("symbol")) != sym
                or str(row.get("planType") or "") != "loss_plan"
                or (row.get("holdSide") or row.get("posSide")) != side
                or str(row.get("clientOid") or "").startswith(self.OID_PREFIX)
            ):
                continue
            try:
                trig = float(row.get("triggerPrice") or 0.0)
                size = float(row.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if trig <= 0.0 or size <= 0.0:
                continue
            if want_entry is not None:
                try:
                    tick = 10.0 ** (-int(self._spec(sym).price_place))
                except (TypeError, ValueError):
                    tick = 0.0
                if abs(trig - want_entry) > tick * 1.5 + 1e-12:
                    continue
            if size + max(1e-9, abs(size) * 1e-6) < float(pos["qty"]):
                continue
            out.append(row)
        return out

    def _cancel_entry_loss_plans(
        self,
        sym: str,
        pos: dict[str, Any],
        rows: list[dict[str, Any]] | None,
        *,
        reason: str,
    ) -> None:
        """撤掉开仓预设止损的 `loss_plan`;绝不动 pos_loss / profit_plan。"""
        if self.mode != "live":
            return
        for row in self._entry_loss_plan_rows(sym, pos, rows):
            oid = str(row.get("orderId") or "")
            if not oid:
                continue
            pos["entry_preset_order_id"] = oid
            try:
                resp = self.client.cancel_plan_order(sym, oid)
                if isinstance(resp, dict) and not (resp.get("successList") or resp.get("failureList")):
                    raise BitgetError("cancel-plan-order 未返回成功项")
            except BitgetError as exc:
                self.counters["stop_preset_cancel_errors"] = self.counters.get("stop_preset_cancel_errors", 0) + 1
                self._journal(
                    "stop_preset_cancel_error",
                    {"symbol": sym, "order_id": oid, "why": str(exc)[:160], "reason": reason},
                )
                self._alert_throttled(
                    f"stop_preset:{sym}",
                    f"{sym} 开仓预设止损未能撤销({oid});保留 pos_loss 并重试",
                )
                continue
            pos["entry_preset_order_id"] = None
            self.counters["stop_preset_cancels"] = self.counters.get("stop_preset_cancels", 0) + 1
            self._journal(
                "stop_preset_cancel",
                {"symbol": sym, "order_id": oid, "trigger": row.get("triggerPrice"), "reason": reason},
            )
            self.invalidate_stop_cache()

    def _manage_local_stop(
        self,
        sym: str,
        pos: dict[str, Any],
        want: float,
        stop_qty: float,
        now_ms: int,
    ) -> None:
        mark = float(pos.get("mark") or 0.0)
        breached = mark > 0.0 and (
            (int(pos["direction"]) > 0 and mark <= want)
            or (int(pos["direction"]) < 0 and mark >= want)
        )
        if not breached:
            return
        self.stops.pop(sym, None)
        self._cancel_entry(sym, "local_stop")
        self._arm_close_intent(
            sym, pos, stop_qty, "market", None,
            f"LOCAL_STOP|{want:.8g}", int(now_ms), role="exit", force=True,
        )
        self.counters["stop_local_triggered"] = self.counters.get("stop_local_triggered", 0) + 1
        self._journal("stop_local_triggered", {"symbol": sym, "trigger": want, "mark": mark})
        self.tg.send_alert(
            f"{sym} 本地止损触发(该仓位无法挂交易所 TPSL):现价 {mark:.8g} 越过防线 {want:.8g} → 市价离场"
        )

    def _sync_existing_stop_trigger(self, cur: dict[str, Any]) -> None:
        if not (cur.get("order_id") and self._stops_live_rows):
            return
        ex_row = next(
            (r for r in self._stops_live_rows if str(r.get("orderId")) == str(cur["order_id"])),
            None,
        )
        if ex_row is None:
            return
        with suppress(TypeError, ValueError):
            ex_trig = float(
                ex_row.get("triggerPrice") or ex_row.get("stopLossTriggerPrice") or cur["trigger"]
            )
            if abs(ex_trig - float(cur["trigger"])) > 1e-9:
                cur["trigger"] = ex_trig

    def _find_preset_pos_loss(
        self,
        sym: str,
        pos: Mapping[str, Any],
        want: float,
        stop_qty: float,
    ) -> dict[str, Any] | None:
        if self.price_clock != "realtime":
            return None
        hold_side = self.stop_hold_side(pos["direction"])
        for row in self._stops_live_rows or []:
            if (
                str(row.get("symbol")) == sym
                and row.get("planType") == "pos_loss"
                and (row.get("holdSide") or row.get("posSide")) == hold_side
                and str(row.get("clientOid") or "").startswith(self.OID_PREFIX)
                and abs(float(row.get("triggerPrice") or 0) - want) < 1e-12
            ):
                adopted = {"trigger": want, "qty": stop_qty, "order_id": str(row["orderId"])}
                self.stops[sym] = adopted
                return adopted
        return None

    def _handle_stop_place_error(
        self,
        sym: str,
        pos: dict[str, Any],
        cur: dict[str, Any] | None,
        want: float,
        stop_qty: float,
        now_ms: int,
        exc: BitgetError,
    ) -> None:
        self.counters["stop_errors"] = self.counters.get("stop_errors", 0) + 1
        why = str(exc)
        self._journal("stop_error", {"symbol": sym, "trigger": want, "qty": stop_qty, "why": why[:160]})
        if _stop_requires_local(why):
            ex_qty = self._exchange_position_qty(sym, int(pos["direction"]))
            if ex_qty is not None and ex_qty <= 0.0:
                self.stops.pop(sym, None)
                self._reconcile_at = 0.0
                self.counters["stop_place_no_pos"] = self.counters.get("stop_place_no_pos", 0) + 1
                self._journal("stop_place_no_pos", {"symbol": sym, "trigger": want, "qty": stop_qty, "why": why[:160]})
                return
            first_place = cur is None or not cur.get("order_id")
            if pos.get("protection_required") and first_place and self._require_exchange_protection():
                pos["protection_required"] = False
                self.stops[sym] = {"trigger": want, "qty": stop_qty, "order_id": "", "mode": "flattening"}
                self.counters["protection_unavailable"] = self.counters.get("protection_unavailable", 0) + 1
                self._journal("protection_unavailable", {"symbol": sym, "trigger": want, "qty": stop_qty, "why": why[:160]})
                self.tg.send_alert(
                    f"{sym} 新开仓无法挂交易所止损({why[:80]}) → 本笔立即平仓,不保留无交易所保护的新仓"
                )
                try:
                    self._arm_close_intent(
                        sym, pos, stop_qty, "market", None,
                        "PROTECTION_UNAVAILABLE", int(now_ms), role="exit", force=True,
                    )
                except Exception as close_err:
                    self.stops[sym] = {"trigger": want, "qty": stop_qty, "order_id": "", "mode": "local"}
                    self.counters["stop_local_mode"] = self.counters.get("stop_local_mode", 0) + 1
                    self.tg.send_alert(
                        f"{sym} 无法挂交易所止损且立即平仓失败({type(close_err).__name__}) → 退回本地止损"
                    )
                return
            self.stops[sym] = {"trigger": want, "qty": stop_qty, "order_id": "", "mode": "local"}
            if not cur or cur.get("mode") != "local":
                self.counters["stop_local_mode"] = self.counters.get("stop_local_mode", 0) + 1
                self._journal("stop_local_mode", {"symbol": sym, "trigger": want, "qty": stop_qty, "why": why[:160]})
                self.tg.send_alert(
                    f"{sym} 该仓位无法挂交易所保护单(仓位过小,43023)⇒ 已切成**本地止损**监控:防线 {want:.8g},越过即市价离场"
                )
            return
        if _stop_reject_no_position(why):
            self.stops.pop(sym, None)
            self.counters["stop_place_no_pos"] = self.counters.get("stop_place_no_pos", 0) + 1
            self._journal("stop_place_no_pos", {"symbol": sym, "trigger": want, "qty": stop_qty, "why": why[:160]})
            return
        self._alert_throttled(
            f"stop:{sym}",
            f"{sym} 保护止损挂单失败:{why[:120]}(当前无触发保护,人工介入;同类告警 30 分钟内不重复)",
        )
        mark = _mark_from_reject(why) or float(pos.get("mark") or 0.0)
        breached = mark > 0.0 and (
            (int(pos["direction"]) > 0 and mark <= want)
            or (int(pos["direction"]) < 0 and mark >= want)
        )
        if breached:
            self.stops.pop(sym, None)
            self.counters["stop_place_breached"] = self.counters.get("stop_place_breached", 0) + 1
            self._journal("stop_place_breached", {"symbol": sym, "trigger": want, "mark": mark, "why": why[:160]})
            self._arm_close_intent(
                sym, pos, stop_qty, "market", None,
                f"STOP_PLACE_BREACHED|{want:.8g}", int(now_ms), role="exit", force=True,
            )
            self.tg.send_alert(
                f"{sym} 保护止损挂单被拒({why[:80]}),且现价 {mark:.8g} 已越过防线 {want:.8g} → 市价离场"
            )

    def _manage_one_stop(
        self,
        sym: str,
        pos: dict[str, Any],
        live_ids: set[str] | None,
        now_ms: int,
    ) -> None:
        want = self.fit_price(sym, float(pos["invalid"]), pos["direction"])
        cur = self.stops.get(sym)
        spec = self._spec(sym)
        stop_qty = floor_step(float(pos["qty"]), spec.size_step, spec.size_place)
        if cur is not None and cur.get("mode") == "flattening":
            return
        if cur is not None and cur.get("mode") == "local":
            self._manage_local_stop(sym, pos, want, stop_qty, now_ms)
            return
        missing = bool(
            cur and cur.get("order_id") and live_ids is not None and str(cur["order_id"]) not in live_ids
        )
        if not missing and cur:
            self._sync_existing_stop_trigger(cur)
        if missing:
            self.counters["stop_missing"] = self.counters.get("stop_missing", 0) + 1
            self._journal(
                "stop_missing",
                {"symbol": sym, "order_id": cur.get("order_id"), "trigger": cur.get("trigger")},
            )
            mark = float(pos.get("mark") or 0.0)
            breached = mark > 0.0 and (
                (int(pos["direction"]) > 0 and mark <= want)
                or (int(pos["direction"]) < 0 and mark >= want)
            )
            if breached:
                self._cancel_entry(sym, "stop_missing_breached")
                self.stops.pop(sym, None)
                self._arm_close_intent(
                    sym, pos, stop_qty, "market", None,
                    f"STOP_MISSING_BREACHED|{want:.8g}", int(now_ms), role="exit", force=True,
                )
                self.tg.send_alert(
                    f"{sym} 交易所止损单已不在挂(被撤/被触发/被清理),且现价 {mark:.8g} 已越过防线 {want:.8g} → 市价离场"
                )
                return
            cur = None
        if cur is None:
            cur = self._find_preset_pos_loss(sym, pos, want, stop_qty)
        if cur is not None and abs(float(cur["trigger"]) - want) < 1e-12:
            return
        try:
            if self.price_clock == "realtime" and cur is not None and cur.get("order_id") and not missing:
                self.client.modify_stop_loss(sym, order_id=cur["order_id"], trigger_price=want)
                cur["trigger"] = want
                self.invalidate_stop_cache()
                self._journal("stop_amend", {"symbol": sym, "trigger": want, "order_id": cur["order_id"]})
                return
            if cur is not None and cur.get("order_id"):
                try:
                    self.client.cancel_tpsl_order(sym, cur["order_id"])
                except BitgetError as exc:
                    self._journal("stop_cancel_skip", {"symbol": sym, "order_id": cur["order_id"], "why": str(exc)[:160]})
            resp = self.client.place_stop_loss(
                sym,
                trigger_price=want,
                hold_side=self.stop_hold_side(pos["direction"]),
                client_oid=client_oid_of(
                    self.OID_PREFIX, sym, str(pos["event_id"]), "stop", int(now_ms), role="stop"
                ),
            )
            oid = str((resp or {}).get("orderId") or "")
            if not oid:
                raise BitgetError("place-tpsl-order 未返回 orderId")
            self.stops[sym] = {"trigger": want, "qty": stop_qty, "order_id": oid}
            self.invalidate_stop_cache()
            self._journal("stop", {"symbol": sym, "trigger": want, "qty": stop_qty, "order_id": oid})
        except BitgetError as exc:
            self._handle_stop_place_error(sym, pos, cur, want, stop_qty, now_ms, exc)

    def _manage_stops(self, now_ms: int) -> None:
        """保护止损单(交易所原生 TPSL `pos_loss` @ 当前 invalid)。"""
        live_ids = self._pending_stop_ids(now_ms) if self.mode == "live" else None
        for sym, pos in list(self.positions.items()):
            self._manage_one_stop(sym, pos, live_ids, now_ms)
        if self.mode == "live" and live_ids is not None:
            rows = self._stops_live_rows
            for sym, pos in list(self.positions.items()):
                cur = self.stops.get(sym)
                if cur is not None and cur.get("order_id") and not cur.get("mode") and str(cur["order_id"]) in live_ids:
                    self._cancel_entry_loss_plans(sym, pos, rows, reason="pos_loss_active")
        self._dedupe_stops(now_ms)

    def _cancel_stop(self, sym: str, why: str) -> None:
        del why
        pos = self.positions.get(sym)
        if self.price_clock == "realtime":
            target = pos.get("target_order") if pos else None
            if target and target.get("order_id"):
                try:
                    self.client.cancel_tpsl_order(sym, target["order_id"])
                except BitgetError as exc:
                    self._journal("target_cancel_error", {"symbol": sym, "why": str(exc)[:160]})
        cur = self.stops.pop(sym, None)
        if cur is not None and cur.get("order_id") and self.mode == "live":
            self.invalidate_stop_cache()
            try:
                self.client.cancel_tpsl_order(sym, cur["order_id"])
            except BitgetError as exc:
                self._journal("stop_cancel_error", {"symbol": sym, "order_id": cur["order_id"], "why": str(exc)[:120]})
        if self.mode == "live" and pos is not None:
            rows = self._stops_live_rows
            if rows is None:
                try:
                    raw = self.client.tpsl_orders_pending()
                    rows = raw.get("entrustedList") if isinstance(raw, dict) else raw
                except BitgetError:
                    rows = None
            self._cancel_entry_loss_plans(sym, pos, rows, reason="exit")

    def _cancel_entry(self, sym: str, reason: str) -> None:
        for oid, it in list(self.open_intents.items()):
            if it["symbol"] != sym or it["role"] != "entry" or it["status"] != "armed":
                continue
            if self.mode == "live" and it.get("exchange_order_id"):
                try:
                    self.client.cancel_order(sym, it["exchange_order_id"])
                except BitgetError as exc:
                    self._journal("entry_cancel_error", {"oid": oid, "symbol": sym, "why": str(exc)[:120]})
            it["status"] = "canceled"
            self._journal("cancel", {"oid": oid, "symbol": sym, "reason": str(reason)[:80]})
            if it.get("watch_entry"):
                self._notify_watch(it, "关注到期" if reason == "N_fill_expiry" else "关注作废", str(reason))

    def _expiry_sweep(self, now_ms: int) -> None:
        """到期处置:entry 撤单;reduce/exit 超时转市价(TIMEOUT_TO_TAKER)。"""
        for oid, it in list(self.open_intents.items()):
            if it["status"] != "armed" or now_ms <= it["expiry_ts"]:
                continue
            if it["role"] == "entry":
                if self.mode == "live" and it.get("exchange_order_id"):
                    try:
                        self.client.cancel_order(it["symbol"], it["exchange_order_id"])
                    except BitgetError as exc:
                        self._journal("entry_expiry_cancel_error", {"oid": oid, "symbol": it["symbol"], "why": str(exc)[:120]})
                it["status"] = "expired"
                self._journal("cancel", {"oid": oid, "symbol": it["symbol"], "reason": "N_fill_expiry"})
                if it.get("watch_entry"):
                    self._notify_watch(it, "关注到期", "N_fill_expiry")
                self.counters["expired_intents"] += 1
                continue
            if self.mode == "live" and it.get("exchange_order_id"):
                try:
                    self.client.cancel_order(it["symbol"], it["exchange_order_id"])
                except BitgetError as exc:
                    self._journal("close_expiry_cancel_error", {"oid": oid, "symbol": it["symbol"], "why": str(exc)[:120]})
                side = self.close_side_of(-it["direction"])
                try:
                    self.client.place_market_order(
                        it["symbol"], side=side, size=it["qty"],
                        client_oid=oid[:30] + "cv", trade_side=self.close_trade_side(),
                    )
                    self.counters["orders_placed"] += 1
                    self._live_close_filled(
                        it, ref_price=self.positions.get(it["symbol"], {}).get("mark") or 0.0
                    )
                except BitgetError as exc:
                    it["status"] = "error"
                    self.tg.send_alert(f"{it['symbol']} 超时转市价失败:{str(exc)[:100]}")
            self.counters["expired_intents"] += 1
            self._journal("timeout_to_taker", {"oid": oid, "symbol": it["symbol"]})

    def _close_fill_fact(
        self,
        sym: str,
        *,
        since_ms: int | None = None,
        opened_ts_ms: int | None = None,
    ) -> tuple[float, int | None] | None:
        """该品种最近一笔平仓成交的 `(vwap, 成交时刻ms)`;取不到返回 None。"""
        try:
            res = self.client.recent_fills(sym, limit=100)
        except BitgetError:
            return None
        rows = (
            res
            if isinstance(res, list)
            else ((res or {}).get("fillList") or (res or {}).get("fills") or [])
        )
        cand: list[tuple[int, float, float, str]] = []
        for r in rows:
            if str(r.get("symbol")) != sym or str(r.get("tradeSide") or "").lower() != "close":
                continue
            ts = _fill_ts_ms(r)
            if ts is None:
                continue
            if since_ms is not None and ts < int(since_ms):
                continue
            if opened_ts_ms is not None and ts < int(opened_ts_ms):
                continue
            try:
                px, size = float(r.get("price")), float(r.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if px <= 0.0:
                continue
            cand.append((ts, px, size, str(r.get("orderId") or "")))
        if not cand:
            return None
        cand.sort(key=lambda x: (x[0], x[1]), reverse=True)
        newest_ts, newest_px, _sz, oid = cand[0]
        if not oid:
            return newest_px, newest_ts
        same = [c for c in cand if c[3] == oid]
        tot = sum(c[2] for c in same)
        if tot <= 0.0:
            return newest_px, newest_ts
        return sum(c[1] * c[2] for c in same) / tot, max(c[0] for c in same)

    def _settle_exits(self, now_ms: int) -> None:
        """决策型离场的成交回执结算(下单瞬间取不到成交价时的二次确认)。"""
        for sym, rec in list(self._exit_settle.items()):
            fact = self._close_fill_fact(sym, since_ms=int(rec["since_ms"]))
            if fact is None:
                if now_ms >= int(rec["deadline_ms"]):
                    self._exit_settle.pop(sym, None)
                    self.counters["exit_settle_miss"] = self.counters.get("exit_settle_miss", 0) + 1
                    self._journal(
                        "exit_settle_miss",
                        {"symbol": sym, "oid": rec["oid"], "ref_px": rec["ref_px"]},
                    )
                continue
            self._exit_settle.pop(sym, None)
            px, ts = float(fact[0]), fact[1]
            pnl = realized_pnl(rec["direction"], rec["qty"], rec["entry"], px, self.risk["taker_fee"])
            pnl_ref = realized_pnl(
                rec["direction"], rec["qty"], rec["entry"], float(rec["ref_px"]), self.risk["taker_fee"]
            )
            self._journal(
                "exit_settle",
                {
                    "symbol": sym, "oid": rec["oid"], "ref_px": float(rec["ref_px"]),
                    "exit_price": px, "exit_ts_ms": ts, "pnl": pnl, "pnl_ref": pnl_ref,
                    "why": rec.get("why"),
                },
            )
            ref = float(rec["ref_px"])
            if ref > 0.0 and abs(px - ref) / ref > 2e-3:
                self.tg.send_alert(
                    f"{sym} 离场回执校正:成交 {px:.8g} ≠ 记账参考 {ref:.8g}"
                    f"(差 {abs(px - ref) / ref:.2%}),已实现 {pnl:+.2f} USDT"
                )

    def _settle_entries(self, now_ms: int) -> None:
        """开仓成交回执结算:把 `entry` 从参考价校正为交易所成交均价(§6a)。"""
        for sym, rec in list(self._entry_settle.items()):
            pos = self.positions.get(sym)
            expected_ev = str(rec.get("base_event_id") if rec.get("pyramid_add") else rec["event_id"])
            if pos is None or str(pos.get("event_id")) != expected_ev:
                self._entry_settle.pop(sym, None)
                continue
            fact = self._entry_fill_fact(sym, str(rec["order_id"]))
            if fact is None:
                if now_ms >= int(rec["deadline_ms"]):
                    self._entry_settle.pop(sym, None)
                    self.counters["entry_settle_miss"] = self.counters.get("entry_settle_miss", 0) + 1
                    self._journal(
                        "entry_settle_miss",
                        {
                            "symbol": sym, "oid": rec["oid"], "order_id": rec["order_id"],
                            "ref_px": float(rec["ref_px"]), "event_id": rec["event_id"],
                        },
                    )
                continue
            self._entry_settle.pop(sym, None)
            px, ts, qty = float(fact[0]), fact[1], float(fact[2])
            ref = float(rec["ref_px"])
            d = int(rec["direction"])
            slip_bps = (d * (px - ref) / ref * 1e4) if ref > 0.0 else 0.0
            if rec.get("pyramid_add"):
                old_q = float(rec.get("old_qty") or 0.0)
                old_e = float(rec.get("old_entry") or 0.0)
                add_q = qty if qty > 0.0 else float(rec.get("add_qty") or 0.0)
                tot_q = old_q + add_q
                new_avg = ((old_q * old_e + add_q * px) / tot_q) if tot_q > 0.0 else px
                applied = self._apply_entry_fill(sym, new_avg)
            else:
                applied = self._apply_entry_fill(sym, px)
            it = self.open_intents.get(rec["oid"])
            if it is not None:
                self._audit_entry_cost(it, px)
                self._audit_directional_fill(it)
            self._journal(
                "entry_settle",
                {
                    "symbol": sym, "oid": rec["oid"], "order_id": rec["order_id"],
                    "event_id": rec["event_id"], "ref_px": ref, "fill_px": px,
                    "fill_qty": qty, "fill_ts_ms": ts, "slip_bps": slip_bps,
                    "applied": applied, "price_source": "fills",
                },
            )
            if applied and abs(slip_bps) >= 20.0:
                self.tg.send_alert(
                    f"{sym} 开仓成交价 {px:.8g} ≠ 信号参考价 {ref:.8g}"
                    f"({slip_bps:+.0f} bps,逆向为正);entry 已按成交价重记,"
                    "实际 R 与计划 R 的偏差请以本条为准"
                )

    def _audit_directional_fill(self, it: dict[str, Any]) -> None:
        """Report actual exposure without pretending an executed order can be rejected."""
        if it.get("directional_cap_breach") or not self._directional_entry_blocked(
            int(it["direction"]), 0.0, 0.0, 0.0, exclude_oid=it["oid"]
        ):
            return
        it["directional_cap_breach"] = True
        self.counters["directional_cap_breaches"] = self.counters.get("directional_cap_breaches", 0) + 1
        self._journal(
            "directional_cap_breach",
            {"oid": it["oid"], "symbol": it["symbol"], "directional_cap_frac": self.directional_cap_frac},
        )
        self.tg.send_alert(
            f"{it['symbol']} 成交回执时同向在途风险超限;已保留真实持仓与保护规则,后续入场受同向上限约束"
        )

    def _audit_entry_cost(self, it: dict[str, Any], fill_px: float) -> None:
        """A fill is a fact, not a rejectable candidate. Record ceiling breaches."""
        if not it.get("max_cost_frac") or it.get("entry_cost_breach"):
            return
        error = entry_contract_error(
            int(it["direction"]),
            float(fill_px),
            float(it["invalid"]),
            it.get("target_1"),
            {"max_cost_frac": it["max_cost_frac"], "cost_per_side": it.get("cost_per_side")},
        )
        if error:
            it["entry_cost_breach"] = error
            self.counters["entry_cost_breaches"] = self.counters.get("entry_cost_breaches", 0) + 1
            self._journal(
                "entry_cost_breach",
                {
                    "oid": it["oid"], "symbol": it["symbol"], "fill_px": fill_px,
                    "invalid": it["invalid"], "max_cost_frac": it["max_cost_frac"], "why": error,
                },
            )
            self.tg.send_alert(
                f"{it['symbol']} 成交回执超出入场成本约束({error});已保留真实持仓与保护规则,未自动平仓"
            )

    def _fill_fact_from_order_detail(
        self,
        sym: str,
        oid: str,
    ) -> tuple[float, int | None, float] | None:
        detail_fn = getattr(self.client, "order_detail", None)
        if not callable(detail_fn):
            return None
        try:
            detail = detail_fn(sym, oid) or {}
        except BitgetError:
            return None
        if not isinstance(detail, dict):
            return None
        try:
            px = float(detail.get("priceAvg") or detail.get("price") or 0)
            qty = float(detail.get("baseVolume") or detail.get("size") or detail.get("fillSize") or 0)
        except (TypeError, ValueError):
            return None
        state = str(detail.get("state") or detail.get("status") or "").lower()
        if px <= 0 or qty <= 0 or state in ("live", "new", "canceled", "cancelled"):
            return None
        ts: int | None = None
        for key in ("uTime", "cTime", "fillTime"):
            raw = detail.get(key)
            if raw is None:
                continue
            try:
                ts_val = int(raw)
            except (TypeError, ValueError):
                continue
            ts = ts_val * 1000 if ts_val < 10_000_000_000 else ts_val
            break
        return px, ts, qty

    def _entry_fill_fact(
        self,
        sym: str,
        order_id: str,
    ) -> tuple[float, int | None, float] | None:
        """该开仓单的成交 `(vwap, 成交时刻ms, 成交张数)`;取不到返回 None。"""
        oid = str(order_id or "")
        if not oid:
            return None
        detail_hit = self._fill_fact_from_order_detail(sym, oid)
        if detail_hit is not None:
            return detail_hit
        try:
            res = self.client.recent_fills(sym, limit=100)
        except BitgetError:
            return None
        rows = (
            res
            if isinstance(res, list)
            else ((res or {}).get("fillList") or (res or {}).get("fills") or [])
        )
        num = amt = 0.0
        last_ts: int | None = None
        for r in rows:
            if str(r.get("symbol")) != sym or str(r.get("orderId") or "") != oid:
                continue
            try:
                px, size = float(r.get("price")), float(r.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if px <= 0.0 or size <= 0.0:
                continue
            num += size * px
            amt += size
            ts = _fill_ts_ms(r)
            if ts is not None and (last_ts is None or ts > last_ts):
                last_ts = ts
        if amt <= 0.0:
            return None
        return num / amt, last_ts, amt

    def _apply_entry_fill(self, sym: str, fill_px: float) -> bool:
        """把建仓均价校正为真实成交价(`positions` 与 pm 的 R 基准同步)。"""
        pos = self.positions.get(sym)
        if pos is None:
            return False
        px = float(fill_px)
        d = int(pos["direction"])
        inv = float(pos["invalid"])
        if px <= 0.0 or d * (px - inv) <= 0.0:
            return False
        pos["entry"] = px
        pos["price_source"] = "fills"
        cs = self._get_cs_for(sym, pos.get("tf_label")) or self.campaigns.get(sym)
        if cs is not None:
            cs.pm.retag_entry(sym, px)
        return True

    def _reconcile_closed(self, sym: str, pos: Mapping[str, Any]) -> None:
        """交易所已无此仓:以交易所为准清理本地事实,并如实分型离场。"""
        cs = self._get_cs_for(sym, pos.get("tf_label")) or self.campaigns.get(sym)
        if cs is not None:
            cs.pm.positions.pop(sym, None)
        st = self.stops.pop(sym, None)
        self.positions.pop(sym, None)
        self._price_last.pop(sym, None)
        self._price_seen.pop(sym, None)
        self._missing_streak.pop(sym, None)
        self._exit_settle.pop(sym, None)
        self._exit_retry.pop(sym, None)
        self._touch_campaigns()
        fact = self._close_fill_fact(
            sym, opened_ts_ms=int(pos["opened_ts"]) if pos.get("opened_ts") else None
        )
        px: float | None = float(fact[0]) if fact is not None else None
        stop_px: float | None = None
        for cand in ((st or {}).get("trigger"), pos.get("invalid")):
            if cand in (None, 0, 0.0):
                continue
            try:
                stop_px = float(cand)
                break
            except (TypeError, ValueError):
                continue
        if px is None:
            executor = "unknown"
        elif stop_px is not None and stop_execution_evidence(int(pos["direction"]), px, stop_px):
            executor = "protective_stop"
        else:
            executor = "external"
        pnl = (
            realized_pnl(pos["direction"], pos["qty"], pos["entry"], px, self.risk["taker_fee"])
            if px is not None
            else None
        )
        label = exit_label(executor=executor, pnl=pnl)
        self.counters["reconcile_closed"] = self.counters.get("reconcile_closed", 0) + 1
        self._journal(
            "reconcile_closed",
            {
                "symbol": sym, "direction": pos["direction"], "qty": pos["qty"],
                "entry": pos["entry"], "exit_price": px,
                "exit_ts_ms": fact[1] if fact else None,
                "price_source": "fills" if px is not None else None,
                "stop": stop_px, "executor": executor, "pnl": pnl,
            },
        )
        if executor == "protective_stop":
            note = (
                f"成交价落在保护单成交侧(止损 {stop_px:.8g})"
                f"{' · 盈利侧成交即移动止损止盈' if (pnl or 0.0) > 0 else ''}"
                ";本地持仓事实已按交易所清理"
            )
        elif executor == "external":
            note = f"成交价未落在保护单成交侧(止损 {stop_px:.8g}) ⇒ 外部/人工平仓;本地持仓事实已按交易所清理"
        else:
            note = "取不到成交回执(fills 无本仓开仓后的平仓成交);仅清理本地事实"
        self.tg.send_fill(
            sym, label, float(pos["qty"]),
            float(px if px is not None else pos.get("mark") or pos["entry"]),
            pnl=pnl, note=note,
            tf_label=pos.get("tf_label"), direction=int(pos["direction"]),
            entry_price=float(pos["entry"]),
            stop_price=float(pos["invalid"]), target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            price_label="成交价" if px is not None else "参考价(无成交回执)",
        )

    def _journal_paths_recent(self, days: int = 14) -> list[Path]:
        """按 UTC 日期倒序的审计日志。"""
        prefix = str(self.loop_cfg.get("journal_prefix") or "live_")
        found = sorted(self.journal_dir.glob(f"{prefix}*.jsonl"), key=lambda p: p.name, reverse=True)
        return found[:max(1, int(days))]

    def _recover_intent_from_journal(self, sym: str, direction: int) -> dict[str, Any] | None:
        """从审计日志重建本系统自己的建仓事实(带 mtime 校验的阴性缓存,避免反复全盘扫描)。"""
        if self.mode != "live":
            return None
        recent_paths = self._journal_paths_recent()
        latest_mtime = 0.0
        if recent_paths:
            with suppress(OSError):
                latest_mtime = recent_paths[0].stat().st_mtime
        miss_cache = getattr(self, "_journal_miss_cache", None)
        cache_key = (sym, int(direction))
        now_s = time.time()
        if miss_cache is not None and cache_key in miss_cache:
            cached_at, cached_mtime = miss_cache[cache_key]
            if now_s - cached_at < _JOURNAL_MISS_TTL_S and cached_mtime == latest_mtime:
                return None

        fills: list[dict[str, Any]] = []
        closed: set[str] = set()
        for path in recent_paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("symbol") != sym:
                    continue
                kind = rec.get("kind")
                if kind == "close_fill":
                    closed.add(str(rec.get("oid")))
                elif (
                    kind == "fill"
                    and int(rec.get("direction") or 0) == int(direction)
                    and str(rec.get("role") or "entry") == "entry"
                    and str(rec.get("origin") or "strategy") == "strategy"
                ):
                    fills.append(rec)
        for rec in fills:
            if str(rec.get("oid")) in closed:
                continue
            try:
                invalid = float(rec.get("invalid"))
                qty = float(rec.get("qty") or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0.0 or not math.isfinite(invalid):
                continue
            tgt = rec.get("structural_target")
            intent: dict[str, Any] = {
                "oid": rec.get("oid") or f"{self.OID_PREFIX}recover{sym}",
                "symbol": sym, "role": "entry", "kind": "market",
                "direction": int(direction), "qty": qty, "limit_price": None,
                "ref_px": rec.get("ref_px") or rec.get("price"),
                "invalid": invalid,
                "reenter_invalid_px": (
                    float(rec["reenter_invalid_px"])
                    if rec.get("reenter_invalid_px") is not None
                    else None
                ),
                "target_1": float(tgt) if tgt is not None else None,
                "structural_target": tgt,
                "tf_label": rec.get("tf_label"),
                "event_id": rec.get("event_id"),
                "route": rec.get("route") or "recovered",
                "decision_ts": int(rec.get("opened_ts") or rec.get("ts") or 0),
                "mic": bool(rec.get("mic")), "size_mult": 1.0, "edge_mult": 1.0,
                "fill_price": float(rec.get("fill_px") or rec.get("price") or 0.0),
                "fill_price_source": "fills",
            }
            if rec.get("execution_target") is not None:
                intent["execution_target"] = float(rec["execution_target"])
            return intent
        if miss_cache is not None:
            miss_cache[cache_key] = (now_s, latest_mtime)
        return None

    def _ensure_campaign_for_manage(self, sym: str) -> CampaignState | None:
        """为已持有但当前不在交易宇宙的标的惰性重建战役事实(只管理出场)。"""
        cs = self.campaigns.get(sym)
        if cs is not None:
            return cs
        if not hasattr(self.client, "history_candles"):
            return None
        try:
            want = int(self.cog_cfg["warmup_trigger_bars"])
            bars = self._prefetch_bars_parallel([sym], want).get(sym) or []
            cs = self._build_all_tf_campaigns(sym, bars2h=bars) if bars else None
        except (BitgetError, RuntimeError) as exc:
            self._journal("manage_campaign_failed", {"symbol": sym, "why": type(exc).__name__})
            return None
        if cs is None:
            return None
        self.campaigns[sym] = cs
        self._manage_only.add(sym)
        self._touch_campaigns()
        if sym in self.symbols_pending:
            self.symbols_pending.remove(sym)
        self._cancel_entry(sym, "manage_only")
        self._journal("manage_campaign_built", {"symbol": sym, "tf_label": cs.tf_label})
        self._alert_throttled(
            f"manage:{sym}",
            f"{sym} 已不在交易宇宙但仍有持仓:重建战役仅用于出场管理(不再新开仓,交易所在挂 TPSL 照旧保护)",
        )
        return cs

    def _manage_only_block(self, sym: str) -> str | None:
        if sym in self._manage_only:
            return "MANAGE_ONLY"
        if self.universe_enabled and self._universe_symbols and sym not in self._universe_symbols:
            return "MANAGE_ONLY"
        return None

    def _quote_guard_block(self, sym: str, quote: Mapping[str, Any] | None) -> str | None:
        """RWA 成交质量闸门:只在真实交易时段内生效。"""
        g = (self.cfg.get("execution") or {}).get("quote_guard") or {}
        if not bool(g.get("enabled")):
            return None
        if bool(g.get("rwa_only", True)) and not self._is_rwa(sym):
            return None
        max_age = int(g.get("max_quote_age_seconds") or 30) * 1000
        if not quote or quote.get("bid") is None or quote.get("ask") is None:
            return "QUOTE_STALE"
        ts = int(quote.get("ts") or 0)
        if ts <= 0 or int(time.time() * 1000) - ts > max_age:
            return "QUOTE_STALE"
        bid, ask = float(quote["bid"]), float(quote["ask"])
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return "QUOTE_STALE"
        mid = (bid + ask) / 2.0
        max_spread = float(g.get("max_spread_bps") or 0.0)
        if max_spread > 0.0 and (ask - bid) / mid * 1e4 > max_spread:
            return "SPREAD_WIDE"
        max_basis = float(g.get("max_basis_bps") or 0.0)
        index, last = quote.get("index"), quote.get("last")
        if (
            max_basis > 0.0
            and index
            and last
            and float(index) > 0.0
            and abs(float(last) - float(index)) / float(index) * 1e4 > max_basis
        ):
            return "BASIS_WIDE"
        return None

    def _try_recover_own_intent(
        self,
        sym: str,
        d: int,
        entry: float,
        mark: float,
        qty: float,
        now_ms: int,
    ) -> bool:
        own_cands = [
            it
            for it in self.open_intents.values()
            if it.get("symbol") == sym
            and it.get("role") == "entry"
            and int(it.get("direction") or 0) == d
            and it.get("event_id") not in (None, "ADOPTED")
            and it.get("invalid") is not None
            and d * (entry - float(it["invalid"])) > 0.0
            and d * (mark - float(it["invalid"])) > 0.0
        ]
        if own_cands:
            own_it = max(own_cands, key=lambda x: int(x.get("decision_ts") or 0))
            own_it["qty"] = qty
            own_it["status"] = "filled"
            own_it["fill_price"] = entry
            own_it["fill_price_source"] = "fills"
            opened_ts = int(own_it.get("decision_ts") or now_ms)
            self._register_position(sym, own_it, entry, opened_ts, origin="strategy")
            self._journal(
                "reconcile_recovered_own_intent",
                {
                    "symbol": sym, "oid": own_it.get("oid"),
                    "tf_label": own_it.get("tf_label"),
                    "entry": entry, "invalid": own_it.get("invalid"),
                    "target_1": own_it.get("target_1"),
                },
            )
            return True
        own_rec = self._recover_intent_from_journal(sym, d)
        if (
            own_rec is not None
            and d * (entry - float(own_rec["invalid"])) > 0.0
            and d * (mark - float(own_rec["invalid"])) > 0.0
        ):
            own_rec["qty"] = qty
            self._ensure_campaign_for_manage(sym)
            self._register_position(
                sym, own_rec, entry, int(own_rec.get("decision_ts") or now_ms), origin="recovered"
            )
            self._journal(
                "intent_recovered",
                {
                    "symbol": sym, "direction": d, "qty": qty,
                    "invalid": float(own_rec["invalid"]), "target_1": own_rec.get("target_1"),
                    "tf_label": own_rec.get("tf_label"), "event_id": own_rec.get("event_id"),
                    "source": "journal",
                },
            )
            return True
        return False

    def _adopt_exchange_only(self, ex_pos: Sequence[Mapping[str, Any]]) -> None:
        """接管交易所独有仓位(优先认回自有仓,再按结构语义接管)。"""
        if not self.adopt_enabled or self.mode != "live":
            return
        now_ms = int(time.time() * 1000)
        for p in ex_pos:
            sym = str(p.get("symbol") or "")
            d = int(p.get("direction") or 0)
            if not sym or d not in (1, -1) or sym in self.positions:
                continue
            entry = float(p.get("entry_price") or 0.0)
            mark = float(p.get("mark_price") or 0.0) or entry
            qty = float(p.get("qty") or 0.0)
            if self._try_recover_own_intent(sym, d, entry, mark, qty, now_ms):
                continue
            cs = (
                self._get_cs_for(sym, str(self.cog_cfg.get("main_tf_label") or "12h"))
                or self.campaigns.get(sym)
                or self._ensure_campaign_for_manage(sym)
            )
            if cs is None:
                self._journal("adopt_skipped", {"symbol": sym, "direction": d, "why": "无战役(不在交易宇宙)且日志无自有成交记录"})
                self._alert_throttled(
                    f"adopt:{sym}",
                    f"{sym} 交易所独有仓位无法接管:该标的无战役/无结构事实,且审计日志里查不到本系统的建仓记录(确认非本系统所开),人工核查",
                )
                continue
            try:
                atr = float(cs.detector.structure.atr())
                zones = list(cs.detector.structure.zones)
            except Exception as exc:
                self._journal("adopt_skipped", {"symbol": sym, "direction": d, "why": f"结构读取失败:{type(exc).__name__}"})
                continue
            struct = adopt_mod.structure_from_zones(atr=atr, zones=zones, k_bm=float(self.rp.k_bm))
            ad = adopt_mod.propose(symbol=sym, direction=d, entry=entry, mark=mark, structure=struct)
            if ad is None:
                self._journal("adopt_skipped", {"symbol": sym, "direction": d, "why": "ATR/均价不可用"})
                self._alert_throttled(
                    f"adopt:{sym}",
                    f"{sym} 交易所独有仓位无法接管:ATR 或持仓均价不可用(entry={entry}, atr={atr}),人工核查",
                )
                continue
            it: dict[str, Any] = {
                "oid": f"{self.OID_PREFIX}adopt{sym}{now_ms}",
                "symbol": sym, "role": "entry", "kind": "market",
                "direction": d, "qty": qty, "limit_price": None,
                "ref_px": ad.entry, "invalid": ad.invalid, "target_1": ad.target_1,
                "tf_label": cs.tf_label, "event_id": "ADOPTED",
                "route": "adopt_external", "decision_ts": now_ms,
                "mic": False, "size_mult": 1.0, "edge_mult": 1.0,
                "stop_source": ad.stop_source, "target_source": ad.target_source,
                "risk_atr": round(float(ad.risk_atr), 4),
                "adopted_breached": bool(ad.already_breached),
            }
            self._register_position(sym, it, ad.entry, now_ms, origin="adopted")
            if ad.already_breached:
                self.tg.send_alert(
                    f"{sym} 接管时现价已越过结构失效位 {ad.invalid:.8g}(现价 {mark:.8g})—— 下一根认知 bar 收盘仍在外侧则按 §8.1 出场"
                )

    def _reconcile(self) -> None:
        try:
            ex_pos = self.client.positions()
        except BitgetError as exc:
            self._api_note(exc)
            return
        ex_by = {(p["symbol"], p["direction"]): p for p in ex_pos}
        for sym in list(self.positions):
            pos = self.positions[sym]
            if (sym, int(pos["direction"])) in ex_by:
                self._missing_streak.pop(sym, None)
                continue
            n = self._missing_streak.get(sym, 0) + 1
            self._missing_streak[sym] = n
            if n >= 2:
                self._reconcile_closed(sym, pos)
        for p in ex_pos:
            sym = p["symbol"]
            if sym not in self.positions:
                continue
            self.positions[sym]["mark"] = p["mark_price"]
            ours = float(self.positions[sym]["qty"])
            if abs(float(p["qty"]) - ours) > max(1e-9, float(p["qty"]) * 1e-6):
                self.positions[sym]["qty"] = float(p["qty"])
                cs = self._get_cs_for(sym, self.positions[sym].get("tf_label"))
                pos_pm = cs.pm.positions.get(sym) if cs is not None else None
                if pos_pm is not None:
                    with suppress(AttributeError, TypeError, ValueError):
                        pos_pm.qty = float(p["qty"])
                self.counters["reconcile_fixes"] = self.counters.get("reconcile_fixes", 0) + 1
                self._journal("reconcile_fix", {"symbol": sym, "ours": ours, "exchange": float(p["qty"])})
                self._alert_throttled(
                    f"qty:{sym}",
                    f"{sym} 持仓数量以交易所为准修正 {ours} -> {p['qty']}(下单精度须取自合约规格)",
                )
        self._adopt_exchange_only(ex_pos)
        mismatches = reconcile_positions(self.positions, ex_pos)
        for m in mismatches:
            self.counters["reconcile_alerts"] += 1
            self._journal("reconcile", {"mismatch": m})
            kind = str(m).split()[0]
            if kind == "exchange_only":
                tail = "交易所存在非本系统所开仓位 —— 本系统不触碰,人工核查"
            elif kind == "qty_mismatch":
                tail = "数量不一致 —— 已以交易所为准修正(下单精度须取自合约规格)"
            else:
                tail = "本地有仓而交易所已无 —— 待下轮确认后按交易所为准清理"
            self._alert_throttled(
                f"reconcile:{' '.join(str(m).split()[:2])}",
                f"持仓对账不一致:{m} —— {tail}",
            )
        if not mismatches:
            self._journal("reconcile", {"ok": True, "ours": len(self.positions), "exchange": len(ex_pos)})

    def _daily_tick(self, now_ms: int) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if self._daily_date is None:
            self._daily_date = day
            return
        if day == self._daily_date:
            return
        self.api_halted, self._api_halt_alerted = False, False
        self._api_error_streak = 0
        anchor = self.breaker.prev_start_equity or self.breaker.start_equity or self.equity
        close = self.breaker.prev_close_equity or self.equity
        dd = (anchor - close) / anchor if anchor else 0.0
        self.tg.send_daily(
            self._daily_date,
            float(close),
            float(close) - float(anchor or 0),
            self.counters["signals"],
            self.counters["fills"],
            len(self.positions),
            dd,
        )
        self._daily_date = day

    # ------------------------------------------------------------------
    # paper 虚拟经纪
    # ------------------------------------------------------------------

    def _resolve_paper_fill_price(
        self,
        oid: str,
        it: dict[str, Any],
        sym: str,
        bar: Sequence[Any],
    ) -> float | None:
        ts, o, h, low, c, _v = bar
        if ts > it["expiry_ts"]:
            if it["role"] == "entry":
                it["status"] = "expired"
                self.counters["expired_intents"] += 1
                self._journal("expired", {"oid": oid, "symbol": sym})
                if it.get("watch_entry"):
                    self._notify_watch(it, "关注到期(paper)", "等待期限已过，未开仓")
                return None
            self.counters["expired_intents"] += 1
            return float(o)
        if it["kind"] == "market":
            return float(o)
        lp = float(it["limit_price"])
        d = int(it["direction"])
        if it["role"] == "entry" and it.get("watch_entry"):
            prog_f = self._watch_prog_price(it, d)
            if prog_f is not None:
                already_ran = (h >= prog_f) if d > 0 else (low <= prog_f)
                touched_watch = (low <= lp) if d > 0 else (h >= lp)
                if already_ran and not touched_watch:
                    it["status"] = "canceled"
                    self.counters["watch_canceled_played_out"] = self.counters.get("watch_canceled_played_out", 0) + 1
                    self._journal(
                        "watch_cancel",
                        {"oid": oid, "symbol": sym, "why": "MOVE_ALREADY_PLAYED_OUT", "prog_px": prog_f},
                    )
                    self._notify_watch(it, "关注作废(paper)", "未触发前行情已走完")
                    return None
        if (d > 0 and low < lp) or (d < 0 and h > lp):
            return lp
        if it["role"] == "entry" and it.get("watch_entry") and it.get("near_price") is not None:
            near_f = float(it["near_price"])
            near_hit = (low <= near_f) if d > 0 else (h >= near_f)
            bounce_ok = (c > o and c >= near_f) if d > 0 else (c < o and c <= near_f)
            if near_hit and bounce_ok:
                return float(c)
        return None

    def _paper_fill_pass(self, sym: str, bar: Sequence[Any]) -> None:
        ts = int(bar[0])
        for oid, it in list(self.open_intents.items()):
            if it["status"] != "armed" or it["symbol"] != sym:
                continue
            fill = self._resolve_paper_fill_price(oid, it, sym, bar)
            if fill is None:
                continue
            if it["role"] == "entry":
                self._paper_entry_fill(sym, oid, it, fill, ts)
            else:
                self._paper_close_fill(sym, it, fill, ts)

    def _paper_entry_fill(
        self,
        sym: str,
        oid: str,
        it: dict[str, Any],
        price: float,
        ts: int,
    ) -> None:
        error = entry_contract_error(
            int(it["direction"]), price, float(it["invalid"]), it.get("target_1"), it
        )
        if error:
            it["status"] = "void"
            self._journal("paper_entry_void", {"oid": oid, "symbol": sym, "why": error, "price": price})
            return
        if it["direction"] * (price - it["invalid"]) <= 0.0:
            it["status"] = "void"
            self.tg.send_alert(f"{sym} 成交价穿越失效位,放弃建仓(price={price} invalid={it['invalid']})")
            return
        if self._directional_entry_blocked(
            int(it["direction"]), float(it["qty"]), price, float(it["invalid"]), exclude_oid=oid
        ):
            it["status"] = "void"
            self._journal("paper_entry_void", {"oid": oid, "symbol": sym, "why": "DIRECTIONAL_CAP", "price": price})
            return
        fee = it["qty"] * price * self.risk["taker_fee"]
        self.equity -= fee
        self._register_position(sym, it, price, ts)

    def _paper_close_fill(self, sym: str, it: dict[str, Any], price: float, ts: int) -> None:
        del ts
        pos = self.positions.get(sym)
        if pos is None:
            it["status"] = "void"
            return
        qty = min(it["qty"], pos["qty"])
        gross = qty * pos["direction"] * (price - pos["entry"])
        fee = qty * price * self.risk["taker_fee"]
        self.equity += gross - fee
        it["status"] = "filled"
        self.counters["reduce_fills" if it["role"] == "reduce" else "exits"] += 1
        cs = self._get_cs_for(sym, pos.get("tf_label")) or self.campaigns.get(sym)
        chart_bars = (
            cs.cog_bars[-_CHART_BAR_WINDOW:]
            if (cs and cs.cog_bars)
            else (cs.trig_bars[-_CHART_BAR_WINDOW:] if cs else None)
        )
        self.tg.send_fill(
            sym, self._close_action_label(it), qty, price,
            pnl=gross - fee, note=str(it["route"])[:60],
            tf_label=pos.get("tf_label", cs.tf_label if cs else "12h"),
            direction=pos["direction"], entry_price=float(pos["entry"]),
            stop_price=pos.get("invalid"), target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            box_start_ts=pos.get("box_start_ts"), bars=chart_bars,
        )
        self._journal("close_fill", {"oid": it["oid"], "symbol": sym, "qty": qty, "price": price, "pnl": gross - fee})
        pos["qty"] = round(pos["qty"] - qty, 10)
        if it["role"] == "exit" or pos["qty"] <= 1e-9:
            self.positions.pop(sym, None)
        self._touch_campaigns()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="live_runner")
    ap.add_argument(
        "--mode", required=True, choices=("live", "paper"),
        help="MODE 必须显式(live 发真单;paper 干跑)",
    )
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument(
        "--heartbeat-path", default=None,
        help="心跳落盘路径(默认:config loop.heartbeat_path 锚定仓库根;测试/沙盒用它指到临时目录)",
    )
    return ap.parse_args(argv)


def _acquire_live_lock(args: argparse.Namespace, cfg: Mapping[str, Any]) -> SingletonLock | None:
    if args.mode != "live" or args.heartbeat_path:
        return None
    lock_path = paths.runtime_dir() / "live" / "runner.lock"
    runner_lock = SingletonLock(lock_path)
    if not runner_lock.acquire():
        marker = paths.runtime_dir() / "live" / "runner_lock_busy.json"
        with suppress(OSError):
            write_atomic(
                marker,
                json.dumps(
                    {
                        "code": EXIT_LOCK_BUSY,
                        "ts_ms": int(time.time() * 1000),
                        "pid": os.getpid(),
                        "why": "lock_busy",
                    },
                    ensure_ascii=False,
                ),
            )
        print(f"[live_runner] 已有實盤實例持有單實例鎖({lock_path}),本進程退出", flush=True)
        raise SystemExit(EXIT_LOCK_BUSY)
    hb_rel = (cfg.get("loop") or {}).get("heartbeat_path")
    boot_hb = paths.resolve(str(hb_rel)) if hb_rel else paths.runtime_dir() / "live" / "heartbeat.json"
    with suppress(OSError):
        write_starting_heartbeat(boot_hb, mode=args.mode)
    return runner_lock


def _verify_position_mode(client: BitgetLiveClient, cfg: Mapping[str, Any]) -> None:
    want_mode = str((cfg.get("account") or {}).get("position_mode") or "")
    probe_sym = str((cfg.get("symbols_core") or ["BTCUSDT"])[0])
    got_mode = client.position_mode(probe_sym)
    if want_mode and got_mode != want_mode:
        raise RuntimeError(
            f"持仓模式不一致(fail-closed):config={want_mode} exchange={got_mode};"
            "请修正 config/live.json account.position_mode"
        )
    print(
        f"[live_runner] 持仓模式={got_mode}(开多 buy+open / 开空 sell+open / 平多 buy+close / 平空 sell+close)"
        if got_mode == "hedge_mode"
        else f"[live_runner] 持仓模式={got_mode}(单边:side 表买卖方向)",
        flush=True,
    )


def _start_heartbeat_guard(
    runner: LiveRunner,
    cfg: Mapping[str, Any],
    secrets: Mapping[str, str],
    mode: str,
) -> HeartbeatGuard | None:
    hb_cfg = dict(cfg.get("heartbeat") or {})
    if not hb_cfg.get("enabled", True):
        return None
    guard = HeartbeatGuard(
        runner,
        path=runner.heartbeat_path,
        poll_s=float(hb_cfg.get("write_seconds", 15)),
        stall_s=float(hb_cfg.get("stall_seconds", 180)),
        self_fence_s=float(hb_cfg.get("self_fence_seconds", 900)) if mode == "live" else 0.0,
        secrets=secrets,
        protect_on_stall=bool(hb_cfg.get("protect_on_stall", True)),
    )
    guard.start()
    print(
        f"[live_runner] 心跳看护起动:每 {guard.poll_s:.0f}s 落盘 / "
        f"停摆阈值 {guard.stall_s:.0f}s / 自我熔断 {guard.self_fence_s:.0f}s(0=关) / "
        f"{runner.heartbeat_path}",
        flush=True,
    )
    return guard


def _run_main_loop(runner: LiveRunner, tg: TelegramNotifier, cfg: Mapping[str, Any]) -> None:
    poll = int(cfg["loop"]["poll_seconds"])
    max_consec = int(cfg["loop"].get("max_consecutive_errors", 20))
    consec = 0
    while True:
        now_ms = int(time.time() * 1000)
        try:
            runner.step(now_ms)
            consec = 0
        except BitgetError as exc:
            consec += 1
            print(f"[live_runner] api error({consec}): {exc}", flush=True)
        except SecretsError as exc:
            print(f"[live_runner] secrets error: {exc}", flush=True)
            raise
        except Exception as exc:
            consec += 1
            traceback.print_exc()
            with suppress(Exception):
                runner._journal("crash", {"err": f"{type(exc).__name__}: {exc}"[:300], "consec": consec})
                if consec == 1:
                    tg.send_alert(
                        f"live_runner 未预期异常({type(exc).__name__}):{str(exc)[:160]}"
                        f"(进程保活;连续 {max_consec} 次即退出,由看门狗重启)"
                    )
            if consec >= max_consec:
                raise
        time.sleep(poll)


def main(argv: Sequence[str] | None = None) -> None:
    paths.soften_stdio()
    args = parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    _runner_lock = _acquire_live_lock(args, cfg)
    cred_src = cfg.get("credentials_source")
    cred_path = paths.resolve_existing(cred_src) if cred_src else None
    if cred_path is not None and not cred_path.exists():
        cred_path = None
    secrets = load_secrets(cred_path, config_overrides=cfg.get("credentials"))
    client = BitgetLiveClient(secrets)
    if args.mode == "live":
        _verify_position_mode(client, cfg)
    tg = TelegramNotifier(
        secrets, dropped_log=paths.runtime_dir() / "live" / "tg_missed.jsonl", async_mode=True
    )
    chan_txt = (
        "信号/运营 独立"
        if tg.ops_channel_separate
        else ("运营回退信号 Bot" if tg.ops_configured else "未配置")
    )
    print(f"[live_runner] Telegram:信号通道={'on' if tg.configured else 'off'} / {chan_txt}", flush=True)
    contracts, contract_raw = load_contracts_live(client)
    runner = LiveRunner(
        args.mode, cfg, client, tg,
        contracts=contracts, contract_raw=contract_raw,
        heartbeat_path=Path(args.heartbeat_path) if args.heartbeat_path else None,
        symbols=tuple(cfg.get("symbols_core") or ()),
    )
    poll = int(cfg["loop"]["poll_seconds"])
    r = cfg["risk"]
    tf_list = list((cfg.get("cognition") or {}).get("tf_list") or ["2h", "4h", "1d"])
    trig_map = (cfg.get("cognition") or {}).get("trigger_tf_by_htf") or {}
    cascade_txt = ", ".join(cascade_label(tf, trig_map.get(tf)) for tf in tf_list)
    uni_cfg = cfg.get("universe") or {}
    crypto_min = float(
        (uni_cfg.get("crypto") or {}).get("min_usdt_volume_24h", uni_cfg.get("min_usdt_volume_24h") or 0.0)
        or 0.0
    )
    rwa_txt = (
        "RWA 不限"
        if uni_cfg.get("rwa_no_volume_filter")
        else f"RWA > {float((uni_cfg.get('rwa') or {}).get('min_usdt_volume_24h') or 0.0):.0f}"
    )
    print(
        f"[live_runner] mode={args.mode} 主循环起动(每 {poll}s;cascade=[{cascade_txt}];"
        f"risk_frac={r['risk_frac']} 杠杆≤{r['leverage_cap']} 组合≤{r['portfolio_cap_frac']} "
        f"日损熔断 {r['max_daily_loss_frac']} 每时单限 {r['orders_per_hour']};"
        f"宇宙 crypto > {crypto_min:.0f} USDT / {rwa_txt})",
        flush=True,
    )
    first_tf_cap = list((r.get("tf_cap_by_tf") or {}).values())[0] if r.get("tf_cap_by_tf") else 0.15
    tg.send_alert(
        f"live_runner 起动(mode={args.mode} | 策略={cfg.get('version', 'v22-pure-pa-amt')})\n"
        f"⏱️ 周期级联: {cascade_txt}\n"
        f"🛡️ 风控配置: 单笔风险 {r['risk_frac']:.1%} (动态阶梯复利) | 单周期上限 {first_tf_cap:.0%} | 组合上限 {r['portfolio_cap_frac']:.0%}\n"
        f"📡 通知通道: 信号=公开广播(结构+执行口径), 成交/日结/告警={chan_txt}"
    )
    print(
        f"[live_runner] gap_started={runner._gap_started_ms} "
        f"gap_symbols={sorted(runner._gap_bars)} "
        f"gap_bars={sum(len(v) for v in runner._gap_bars.values())}",
        flush=True,
    )
    if runner._gap_bars:
        tg.send_alert(
            f"live_runner 起动:检测到上次断点后存在缺口("
            f"{len(runner._gap_bars)} 品种/{sum(len(v) for v in runner._gap_bars.values())} 根 2H bar),"
            "将先补做持仓管理再恢复常规循环"
        )
    _guard = _start_heartbeat_guard(runner, cfg, secrets, args.mode)
    _run_main_loop(runner, tg, cfg)
    del _runner_lock, _guard


if __name__ == "__main__":
    main()
