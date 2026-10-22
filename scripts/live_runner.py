"""实盘 runner(实盘接入件;v20/lifecycle;直接提交,不推不合)。

主循环(每 15s 轮询):宇宙发现(24h USDT 名义量 > 3M)→ 每品种认知帧
(12h 主 + 下级触发 2H,TRIGGER_TF_MAP 精确命中)→ canonical 认知 + 路由
arm-full(v20 四件全开)→ 订单意图 → broker-live(--mode live 才发真单)
→ Telegram 通知;持仓对账 runner↔exchange 每 2min;断点重启 state.json。

语义纪律:E 检测/策略语义/历史产物零改动 —— EventDetector/MicTracker/
V20Router/PositionManager 一律 canonical 件;本脚本只做接线、执行近似与
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
④ EXIT 市价;⑤ 保护止损计划单(loss_plan @ 当前 invalid)只保护不主出,
  invalid 变动撤旧换新;⑥ pm(§8)按 12h 收盘裁决,入场成交的认知 bar 不
  裁决(canonical 同口径:opened_ts > bar 标签才跳过)。

用法:
    python scripts/live_runner.py --mode live     # 主 agent 启动(本件不启动)
    python scripts/live_runner.py --mode paper    # 干跑(不发真单)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.bitget_live import (                        # noqa: E402
    BitgetError, BitgetLiveClient)
from market_operator.broker import (                            # noqa: E402
    ContractSpec, _floor_step)
from market_operator.events import EventDetector                # noqa: E402
from market_operator.hypothesis import StructureView            # noqa: E402
from market_operator.lifecycle import (                         # noqa: E402
    LifecycleParams, V20Params, V20Router)
from market_operator.position import Position, PositionManager  # noqa: E402
from market_operator.repairs import (                           # noqa: E402
    prior_moves_delta, range_quantile)
from market_operator.secrets_loader import SecretsError, load_secrets  # noqa: E402
from market_operator.routing import RouterParams, MarketRouter      # noqa: E402
from market_operator.structure import Params                    # noqa: E402
from market_operator.tg import TelegramNotifier                 # noqa: E402
from market_operator.tiered import TieredParams                 # noqa: E402
from market_operator.trigger import (                           # noqa: E402
    MicTracker, resolve_trigger_tf)

CONFIG_PATH = ROOT / "config" / "live.json"
CONFIG_V20 = ROOT / "config" / "v20.json"
CONFIG_V21 = ROOT / "config" / "v21.json"

MS_1H = 3_600_000
N_FILL_H = 12                                    # canonical N_fill(墙钟小时)
TF_SECONDS_MAP = {"1h": 3600, "2h": 7200, "4h": 14400, "12h": 43200, "1d": 86400}


# ---------------------------------------------------------------------------
# canonical 参数组(v20 / v21 双模支持)
# ---------------------------------------------------------------------------


def load_v21_params(cfg_path: Path = CONFIG_V21) -> tuple:
    """加载 v21 极简平衡箱 + 双原语(ACCEPT/REJECT) + 周期分治路由参数。"""
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    params = Params.from_json(cfg_path)
    whitelist = frozenset((str(i), int(d)) for i, d in raw["whitelist"])
    base_kw = dict(
        k_dq=float(raw["k_dq"]),
        k_imp=float(raw["k_imp"]),
        k_is=float(raw["k_is"]),
        turtle_stop_atr=float(raw["turtle_stop_atr"]),
        k_bm=float(raw["k_bm"]),
        whitelist=whitelist,
    )
    rp_extra = raw.get("router_params") or {}
    base_rp = dc_replace(RouterParams(**base_kw), **rp_extra)
    rp_by_tf = {}
    for tf, tf_ov in (raw.get("router_params_by_tf") or {}).items():
        clean_ov = {("zone_cooldown_bars" if k == "min_zone_cooldown_bars" else k): v
                    for k, v in tf_ov.items()}
        rp_by_tf[tf] = dc_replace(base_rp, **clean_ov)
    return raw, params, base_rp, rp_by_tf


def load_v20_params() -> tuple:
    """(raw, Params, V20Params-arm-full);与 scripts/run_v20.py 同一接线。"""
    raw = json.loads(CONFIG_V20.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_V20)
    p19, p16 = raw["v19_params"], raw["v16_params"]
    tp = TieredParams(
        **{k: p19[k] for k in ("pos_short_min", "pos_long_max", "k_room_R",
                               "room_lo", "k_is", "k_star", "mult_pos",
                               "mult_room_mid", "mult_room_lo",
                               "mult_transition_fade", "mult_rngq_big",
                               "mult_pmd_adverse")},
        fade_family=tuple(p16["fade_family"]), fade_front_edge=False)
    whitelist = frozenset((str(i), int(d)) for i, d in raw["whitelist"])
    rp = V20Params(k_dq=float(raw["k_dq"]), k_imp=float(raw["k_imp"]),
                   k_is=float(raw["k_is"]),
                   turtle_stop_atr=float(raw["turtle_stop_atr"]),
                   k_bm=float(raw["k_bm"]), whitelist=whitelist,
                   tiered=tp,
                   v20=LifecycleParams(s1_trail_arm=True, s2_fade_2leg=True,
                                       s3_transition_fade_ban=True,
                                       s4_e3_maker=True))
    return raw, params, rp


def new_position_manager(params: Params, raw: dict) -> PositionManager:
    """持仓侧接线:兼容 v21(对侧突破锁利 + Turtle)与 v20(arm-full)。"""
    if raw.get("use_balance_detector"):
        arm_cfg = raw.get("arm_config") or {}
        return PositionManager(
            params,
            allow_add=False,
            breakeven_on_reduce=bool(arm_cfg.get("breakeven_on_reduce", True)),
            time_stop=False,
            reject_opp_lock_frac=float(arm_cfg.get("reject_opp_lock_frac", -0.50)),
        )
    p16 = raw["v16_params"]
    return PositionManager(
        params, allow_add=False, breakeven_on_reduce=True, time_stop=False,
        harvest_family=tuple(p16["fade_family"]),
        time_cap_bars=int(p16["k_cap_hours"]),
        cap_mfe_min=float(p16["cap_mfe_min"]),
        harvest_ladder3=False, early_exit=False,
        trail_arm_gate=True, harvest_fade_2leg=True)


# ---------------------------------------------------------------------------
# 风控断件(纯函数级;测试钉死)
# ---------------------------------------------------------------------------


class DailyLossBreaker:
    """日损熔断:UTC 日界内 (equity − start)/start ≤ −max_frac 即停;日界复位。"""

    def __init__(self, max_frac: float) -> None:
        self.max_frac = float(max_frac)
        self.day: Optional[str] = None
        self.start_equity: Optional[float] = None
        self.tripped = False

    def update(self, now_ms: int, equity: float) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) \
            .strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.start_equity, self.tripped = \
                day, float(equity), False

    def should_stop(self, equity: float) -> bool:
        if self.start_equity in (None, 0.0):
            return False
        if (float(equity) - self.start_equity) / self.start_equity \
                <= -self.max_frac:
            self.tripped = True
        return self.tripped


class RateLimiter:
    """每时订单上限(滚动 3600s 窗;到限即拒,fail-closed 计数)。"""

    def __init__(self, max_per_hour: int) -> None:
        self.max = int(max_per_hour)
        self.stamps: list[float] = []

    def allow(self, now_s: Optional[float] = None) -> bool:
        now_s = time.time() if now_s is None else float(now_s)
        self.stamps = [t for t in self.stamps if now_s - t < 3600.0]
        if len(self.stamps) >= self.max:
            return False
        self.stamps.append(now_s)
        return True

    def pending(self, now_s: Optional[float] = None) -> int:
        now_s = time.time() if now_s is None else float(now_s)
        return sum(1 for t in self.stamps if now_s - t < 3600.0)


def client_oid_of(prefix: str, symbol: str, event_id: str, side: str,
                  decision_ts: int, role: str = "open") -> str:
    """确定性客户单号(≤36 字符):重启/重试同键同号,交易所侧幂等。"""
    h = hashlib.sha1(
        f"{symbol}|{event_id}|{side}|{int(decision_ts)}|{role}"
        .encode()).hexdigest()[:12]
    return f"{prefix}{h}"


def worst_case_of_position(direction: int, qty: float, mark: float,
                           invalid: float, taker_fee: float) -> float:
    """单仓最坏情景(canonical 口径:止损实现亏损 + 平仓侧 taker 预算)。"""
    stop = qty * max(0.0, direction * (mark - invalid)) if mark > 0 else 0.0
    return stop + qty * mark * taker_fee


# ---------------------------------------------------------------------------
# 帧纯函数(测试钉死)
# ---------------------------------------------------------------------------


def aggregate_bucket(bars, label: int) -> tuple:
    """同桶 2H bar 子序列 → 12h OHLCV(canonical aggregate 同一语义)。"""
    return (int(label), float(bars[0][1]),
            float(max(b[2] for b in bars)), float(min(b[3] for b in bars)),
            float(bars[-1][4]), float(sum(b[5] for b in bars)))


def aggregate_tf(bars2h: list, step_seconds: int = 43200) -> list:
    """2H 序列 → 任意整数倍周期(2h/4h/12h/1d)帧:网格对齐;末桶不齐(未完整)丢弃,
    内部缺口 raise(不可静默);首桶不齐如实保留(canonical 首/末不完整桶口径)。"""
    MS = 7_200_000
    STEP = int(step_seconds) * 1000
    if STEP <= MS:
        return list(bars2h)
    buckets: dict = {}
    for b in bars2h:
        buckets.setdefault(b[0] - (b[0] % STEP), []).append(b)
    labels = sorted(buckets)
    out = []
    for label in labels:
        got = buckets[label]
        if len(got) < STEP // MS:
            if label == labels[-1]:
                continue                          # 未完整末桶:如实丢弃
            if label == labels[0]:
                out.append(aggregate_bucket(got, label))
                continue                          # 首桶不完整:保留
            raise RuntimeError(f"{step_seconds}s 桶 {label} 内部缺口({len(got)} 根)")
        out.append(aggregate_bucket(got, label))
    return out


def aggregate_12h(bars2h: list) -> list:
    """2H 序列 → 12h 帧(兼容旧接口)。"""
    return aggregate_tf(bars2h, 43200)


def replay_cognition(params: Params, symbol: str, cog_bars: list,
                     trig_bars: list, *, obs_tf_seconds: int,
                     trigger_tf_seconds: int):
    """warmup 回放 → (detector, tracker);与 prepare_mic_cognition 同一因果序
    (逐认知 bar:先 trigger 段推进,再 bar 收盘 update + sync),实例留存供
    增量接续;bar 事件流 == prepare_cognition 由测试钉死。
    优化:同周期触发(trigger_tf_seconds >= obs_tf_seconds)无需构建/步进 MicTracker。
    """
    detector = EventDetector(params, enable_e7=True)
    if trigger_tf_seconds >= obs_tf_seconds:
        for bar in cog_bars:
            detector.update(bar)
        return detector, None
    obs_ms = obs_tf_seconds * 1000
    trig_ms = trigger_tf_seconds * 1000
    t_series = aggregate_tf(trig_bars, trigger_tf_seconds) if trigger_tf_seconds > 7200 else trig_bars
    scale = max(1, obs_ms // trig_ms)
    tracker = MicTracker(params, scale=scale,
                         trigger_tf_seconds=trigger_tf_seconds,
                         obs_tf_seconds=obs_tf_seconds, enable_e7=True)
    labels = {b[0]: i for i, b in enumerate(cog_bars)}
    spans: dict = {}
    for k, b in enumerate(t_series):
        i = labels.get(b[0] - (b[0] % obs_ms))
        if i is None:                             # 认知帧外的头尾 trigger:跳过
            continue
        sp = spans.setdefault(i, [k, k])
        sp[1] = k
    k = 0
    n = len(cog_bars)
    for i, bar in enumerate(cog_bars):
        sp = spans.get(i)
        if sp is None:
            raise RuntimeError(f"{symbol} 认知 bar {i} 无 trigger 覆盖")
        lo, hi = sp
        if k < lo:
            k = lo
        while k <= hi:
            tb = t_series[k]
            tracker.on_trigger_bar(k, tb[0], tb[1], tb[2], tb[3], tb[4])
            k += 1
        detector.update(bar)
        tracker.sync(i, detector,
                     lambda p, _sp=spans: _sp[p][0],
                     lambda p, _sp=spans, _n=n: _sp[min(p, _n - 1)][1])
    return detector, tracker



def reconcile_positions(ours: dict, exchange: list) -> list:
    """runner↔exchange 持仓对账:方向/数量(相对容差 1e-6);exchange_only
    = 异己仓(单列,绝不动作)。"""
    out = []
    ex_by = {(p["symbol"], p["direction"]): dict(p) for p in exchange}
    for p in ours.values():
        tp = ex_by.pop((p["symbol"], p["direction"]), None)
        if tp is None:
            out.append(f"ours_only {p['symbol']} dir={p['direction']} "
                       f"qty={p['qty']}")
        elif abs(tp["qty"] - p["qty"]) > max(1e-9, p["qty"] * 1e-6):
            out.append(f"qty_mismatch {p['symbol']} ours={p['qty']} "
                       f"exchange={tp['qty']}")
    for (sym, d), p in ex_by.items():
        out.append(f"exchange_only {sym} dir={d} qty={p['qty']}")
    return out


# ---------------------------------------------------------------------------
# 战役状态
# ---------------------------------------------------------------------------


@dataclass
class CampaignState:
    symbol: str
    detector: EventDetector
    tracker: Optional[MicTracker]
    router: object
    pm: PositionManager
    cog_bars: list = field(default_factory=list)
    trig_bars: list = field(default_factory=list)
    tf_label: str = "12h"
    obs_tf_seconds: int = 43200
    trigger_tf_seconds: int = 7200
    active: bool = True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class LiveRunner:
    """主循环接线;client/tg 注入(paper 冒烟零真单;测试注入假件)。"""

    OID_PREFIX = "mo21"

    def __init__(self, mode: str, cfg: dict, client, tg: TelegramNotifier, *,
                 equity0: Optional[float] = None,
                 contracts: Optional[dict] = None,
                 contract_raw: Optional[dict] = None,
                 state_path: Optional[Path] = None,
                 journal_dir: Optional[Path] = None,
                 symbols: tuple = (),
                 universe_enabled: bool = True) -> None:
        if mode not in ("live", "paper"):
            raise RuntimeError("MODE 必须显式为 live 或 paper(fail-closed)")
        self.mode, self.cfg, self.client, self.tg = mode, cfg, client, tg
        self.risk = cfg["risk"]
        self.loop_cfg = cfg["loop"]
        self.cog_cfg = cfg["cognition"]
        self.engine_version = str(self.cog_cfg.get("engine") or "v20")
        if self.engine_version == "v21":
            self.raw_strat, self.params, self.rp, self.rp_by_tf = load_v21_params()
            self.raw20 = self.raw_strat
        else:
            self.raw20, self.params, self.rp = load_v20_params()
            self.raw_strat = self.raw20
            self.rp_by_tf = {}
        self.contracts = contracts or {}
        self.contract_raw = contract_raw or {}     # 交易所原始合约条目(maxLever 等)
        self.leverage_by_sym: dict[str, int] = {}  # 各品种实际生效杠杆
        self.equity: Optional[float] = \
            float(equity0) if equity0 is not None else None
        self.state_path = Path(state_path) if state_path else \
            ROOT / self.loop_cfg["state_path"]
        self.candle_cache_dir = self.state_path.parent / "candles"
        self.journal_dir = Path(journal_dir) if journal_dir else \
            ROOT / self.loop_cfg["journal_dir"]

        self.breaker = DailyLossBreaker(self.risk["max_daily_loss_frac"])
        self.rate = RateLimiter(self.risk["orders_per_hour"])
        self.universe_enabled = bool(universe_enabled)
        self.campaigns: dict[str, CampaignState] = {}
        self.extra_campaigns: dict[tuple[str, str], CampaignState] = {}
        self.symbols_pending: list = sorted(symbols)
        self._short_history_alerted: set[str] = set()
        self.positions: dict[str, dict] = {}
        self.open_intents: dict[str, dict] = {}
        self.stops: dict[str, dict] = {}
        self.counters = {"signals": 0, "orders_placed": 0, "cap_skips": 0,
                         "tf_cap_skips": 0, "min_notional_skips": 0,
                         "rate_skips": 0, "breaker_blocks": 0, "fills": 0,
                         "mgmt_execs": 0, "expired_intents": 0,
                         "reconcile_alerts": 0, "reduce_fills": 0, "exits": 0,
                         "api_halt_skips": 0, "order_errors": 0}
        self._universe_at = 0.0
        self._reconcile_at = 0.0
        self._daily_date: Optional[str] = None
        self._api_error_streak = 0
        # 执行口径(接入件):hedge_mode 下 side 表持仓方向且下单必带 tradeSide
        self.hedge_mode = str((cfg.get("account") or {}).get("position_mode")
                              or "one_way_mode") == "hedge_mode"
        self.api_halted = False           # API 错熔断:暂停新增风险(平仓/止损照常)
        self._api_halt_alerted = False
        self._leverage_done: set[str] = set()
        self._alert_last: dict[str, float] = {}
        self._missing_streak: dict[str, int] = {}   # 对账缺失连续轮次(防误清)
        self._load_state()

    def _alert_throttled(self, key: str, msg: str, gap_s: float = 1800.0) -> None:
        """同类告警限频(默认 30 分钟一条)。

        修前缺陷:每轮(15s)重试即发一条 ⇒ 一晚刷屏数百条,真正的新问题
        反而被淹没(2026-10-22 事件)。
        """
        now = time.time()
        if now - self._alert_last.get(key, 0.0) < gap_s:
            return
        self._alert_last[key] = now
        self.tg.send_alert(msg)

    # ------------------------------------------------------------------
    # 执行口径:持仓模式 → side / tradeSide(纯函数级,不触策略语义)
    # ------------------------------------------------------------------

    def open_trade_side(self) -> Optional[str]:
        """开仓 tradeSide(hedge_mode 必填 'open';one-way 省略)。"""
        return "open" if self.hedge_mode else None

    def close_side_of(self, position_direction: int) -> str:
        """平仓单 side:hedge_mode 下与开仓同侧(靠 tradeSide='close' 区分开平),
        one-way 下取反向买卖方向。position_direction = 被平仓位方向(+1 多)。"""
        if self.hedge_mode:
            return "buy" if position_direction > 0 else "sell"
        return "sell" if position_direction > 0 else "buy"

    def close_trade_side(self) -> Optional[str]:
        """平仓 tradeSide(hedge_mode 必填 'close';one-way 省略)。"""
        return "close" if self.hedge_mode else None

    def fit_price(self, sym: str, price: float, direction: int) -> float:
        """按合约价格精度拟合:交易所逐字段校验 scale,超限即 40808 拒单
        (实测 trigger price checkScale 与 size checkScale 同样受检)。

        方向保守:买/平空向下、卖/平多向上 —— 不越过原定前缘或触发价,
        只会更早触发、不会放宽止损。
        """
        sp = self._spec(sym)
        place = int(sp.price_place)
        step = 10.0 ** (-place)
        px = float(price)
        lo = _floor_step(px, step, place)
        if direction > 0:                      # 买/平空:向下(不越过前缘)
            return lo
        # 卖/平多:向上一个 step 至不小于原触发价(_floor_step 对负数返回 0,
        # 不能用 -f(-x) 取反)
        return round(lo + step, place) if lo < px - 1e-12 else lo

    def current_risk_frac(self, equity: Optional[float] = None) -> float:
        """阶梯动态复利:根据当前已实现净值自动匹配风险档位。"""
        eq = float(self.equity if equity is None else equity)
        tiers = self.risk.get("dynamic_risk_tiers") or []
        for max_eq, rf in tiers:
            if eq <= float(max_eq):
                return float(rf)
        return float(self.risk["risk_frac"])

    # ------------------------------------------------------------------
    # 账本 / 状态(只记账不忘秘:绝无凭证字段)
    # ------------------------------------------------------------------

    def _journal(self, kind: str, rec: dict) -> None:
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        path = self.journal_dir / \
            f"{self.loop_cfg['journal_prefix']}" \
            f"{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        row = {"ts": int(time.time() * 1000), "kind": kind, "mode": self.mode,
               **rec}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

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
        b = st.get("breaker", {})
        self.breaker.day = b.get("day")
        self.breaker.start_equity = b.get("start_equity")
        self.breaker.tripped = bool(b.get("tripped"))
        if st.get("equity") and self.equity is None:
            self.equity = float(st["equity"])
        saved_camps = st.get("campaigns", {})
        if saved_camps:
            want = int(self.cog_cfg["warmup_trigger_bars"])
            prefetched = self._prefetch_bars_parallel(
                list(saved_camps.keys()), want,
                seed_by_sym={sym: saved.get("trig_bars")
                             for sym, saved in saved_camps.items()})
            for sym, saved in saved_camps.items():
                try:
                    bars2h = prefetched.get(sym)
                    cs = self._build_all_tf_campaigns(sym, bars2h=bars2h)
                except (BitgetError, RuntimeError) as e:
                    self.tg.send_alert(f"{sym} 断点恢复失败:{type(e).__name__};"
                                       f"待宇宙重新发现")
                    if sym not in self.symbols_pending:
                        self.symbols_pending.append(sym)
                    continue
                if cs is None:
                    if sym not in self.symbols_pending:
                        self.symbols_pending.append(sym)
                    continue
                for s, pd in saved.get("pm_positions", {}).items():
                    fields = {f: pd[f] for f in Position.__dataclass_fields__
                              if f in pd and not f.startswith("_")}
                    pos = Position(**fields)
                    pos._best = pd.get("_best")
                    pos._entry0 = pd.get("_entry0", pos.entry)
                    pos_tf = self.positions.get(s, {}).get("tf_label")
                    target_cs = self._get_cs_for(sym, pos_tf) or cs
                    target_cs.pm.positions[s] = pos
                self.campaigns[sym] = cs
                self._ensure_leverage(sym)
                if sym in self.symbols_pending:
                    self.symbols_pending.remove(sym)


    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        camps = {}
        for sym, cs in self.campaigns.items():
            pm_pos = {}
            all_cs = [cs] + [sub for (s2, _), sub in self.extra_campaigns.items() if s2 == sym]
            for c_item in all_cs:
                for s, p in c_item.pm.positions.items():
                    rec = {f: getattr(p, f) for f in Position.__dataclass_fields__
                           if not f.startswith("_")}
                    rec["_best"], rec["_entry0"] = p._best, p._entry0
                    pm_pos[s] = rec
            camps[sym] = {"trig_bars": cs.trig_bars[-720:],
                          "pm_positions": pm_pos}
        self.state_path.write_text(json.dumps({
            "saved_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="seconds"),
            "mode": self.mode, "equity": self.equity,
            "positions": self.positions, "open_intents": self.open_intents,
            "stops": self.stops, "counters": self.counters,
            "leverage_done": sorted(self._leverage_done),
            "leverage_by_sym": self.leverage_by_sym,
            "breaker": {"day": self.breaker.day,
                        "start_equity": self.breaker.start_equity,
                        "tripped": self.breaker.tripped},
            "campaigns": camps}, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # 战役装配(本地磁盘 K 线缓存 + 增量回补 + 并发拉取 + 按周期裁剪认知重放)
    # ------------------------------------------------------------------

    def _candle_cache_path(self, symbol: str) -> Path:
        gran = str(self.cog_cfg.get("granularity") or "2H").upper()
        return self.candle_cache_dir / f"{symbol}_{gran}.json"

    def _load_cached_bars(self, symbol: str) -> list[tuple]:
        p = self._candle_cache_path(symbol)
        if not p.exists():
            return []
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            return [(int(r[0]), float(r[1]), float(r[2]),
                     float(r[3]), float(r[4]), float(r[5])) for r in raw]
        except Exception:
            return []

    def _save_cached_bars(self, symbol: str, bars: list) -> None:
        if not bars:
            return
        try:
            self.candle_cache_dir.mkdir(parents=True, exist_ok=True)
            want = int(self.cog_cfg.get("warmup_trigger_bars", 3600))
            self._candle_cache_path(symbol).write_text(
                json.dumps(bars[-want:]), encoding="utf-8")
        except Exception:
            pass

    def _fetch_trigger_bars(self, symbol: str, want: int,
                            seed_bars: Optional[list] = None) -> list:
        """2H 帧回补:优先复用本地缓存 + 增量补齐;不足时倒序分页补齐并落盘缓存。"""
        base_ms = 7200 * 1000
        have: dict = {}
        for src in (self._load_cached_bars(symbol), seed_bars or []):
            for b in src:
                have[int(b[0])] = (int(b[0]), float(b[1]), float(b[2]),
                                   float(b[3]), float(b[4]), float(b[5]))
        if have and hasattr(self.client, "history_candles"):
            now_ms = int(time.time() * 1000)
            last_ts = max(have)
            if now_ms >= last_ts + 2 * base_ms:
                guard_fwd = 0
                while guard_fwd < 20:
                    fresh = self.client.history_candles(
                        symbol, self.cog_cfg["granularity"],
                        start_ms=last_ts + 1, limit=200)
                    new_items = [b for b in fresh if b[0] not in have]
                    if not new_items:
                        break
                    for b in new_items:
                        have[int(b[0])] = b
                    last_ts = max(have)
                    guard_fwd += 1
                    if len(fresh) < 200 or now_ms < last_ts + 2 * base_ms:
                        break
        bars_cached = [have[t] for t in sorted(have)]
        if len(bars_cached) >= 2:
            if any(b[0] - a[0] != base_ms for a, b in zip(bars_cached, bars_cached[1:])):
                have.clear()
        end_ms = min(have) if have else None
        guard = 0
        while len(have) < want and guard < max(1, (want // 200) + 2):
            page = self.client.history_candles(
                symbol, self.cog_cfg["granularity"], end_ms=end_ms, limit=200)
            fresh = [b for b in page if b[0] not in have]
            if not fresh:
                break
            for b in fresh:
                have[int(b[0])] = b
            end_ms = fresh[0][0]
            guard += 1
        bars = [have[t] for t in sorted(have)][-want:]
        self._save_cached_bars(symbol, bars)
        return bars

    def _prefetch_bars_parallel(self, symbols: list[str], want: int,
                                seed_by_sym: Optional[dict] = None) -> dict[str, list]:
        """多线程并发预取各品种 2H K 线(冷启动/宇宙批量加入时提速 6~8 倍)。"""
        if not symbols:
            return {}
        seed_by_sym = seed_by_sym or {}
        if len(symbols) == 1:
            sym = symbols[0]
            return {sym: self._fetch_trigger_bars(sym, want, seed_by_sym.get(sym))}
        out: dict[str, list] = {}
        max_workers = min(8, len(symbols))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {
                ex.submit(self._fetch_trigger_bars, sym, want, seed_by_sym.get(sym)): sym
                for sym in symbols
            }
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    out[sym] = fut.result()
                except Exception:
                    out[sym] = []
        return out


    def _min_cog_bars_for_tf(self) -> int:
        return max(30, int(self.cog_cfg.get("min_warmup_cog_bars", 60)))

    def _build_campaign(self, symbol: str, tf_label: Optional[str] = None,
                        bars2h: Optional[list] = None,
                        *, quiet_if_short: bool = False) -> Optional[CampaignState]:
        tf_label = tf_label or str(self.cog_cfg.get("main_tf_label") or "12h")
        obs_s = TF_SECONDS_MAP.get(tf_label, int(self.cog_cfg["obs_tf_seconds"]))
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        if tf_label in trig_map and trig_map[tf_label] is not None:
            trig_s = int(trig_map[tf_label])
        elif tf_label in trig_map and trig_map[tf_label] is None:
            trig_s = obs_s
        else:
            trig_s = int(self.cog_cfg["trigger_tf_seconds"])
            ttf, decl = resolve_trigger_tf(obs_s)
            if ttf != trig_s or decl is not None:      # 12h→7200 精确命中硬断言
                raise RuntimeError(f"trigger 解析漂移: {decl}")
        want = int(self.cog_cfg["warmup_trigger_bars"])
        if bars2h is None:
            bars2h = self._fetch_trigger_bars(symbol, want)
        min_cog_bars = self._min_cog_bars_for_tf()
        scale_2h = max(1, obs_s // 7200)
        min_2h_needed = min_cog_bars * scale_2h
        if len(bars2h) < min_2h_needed:
            if not quiet_if_short and symbol not in self._short_history_alerted:
                self._short_history_alerted.add(symbol)
                self.tg.send_alert(f"{symbol}({tf_label}) 2H 历史不足({len(bars2h)}/{min_2h_needed}"
                                   f" 根),暂不开启该周期")
            return None
        for a, b in zip(bars2h, bars2h[1:]):       # 网格完整性(不可静默)
            if b[0] - a[0] != 7200 * 1000:
                self.tg.send_alert(f"{symbol} 2H 序列缺口 {a[0]}→{b[0]},"
                                   f"暂不交易")
                return None
        # 优化:按周期裁剪所需 2H 窗口(每个周期仅需 warmup_cog_bars 根自身认知 bar,
        # BalanceDetector 最大窗口 144 <= 160;新币满 min_cog_bars(60根)即可自适应准入)
        warmup_cog = max(min_cog_bars, int(self.cog_cfg.get("warmup_cog_bars", 160)))
        need_2h = min(len(bars2h), (warmup_cog + 2) * scale_2h)
        slice_2h = bars2h[-need_2h:]
        cog_bars = aggregate_tf(slice_2h, obs_s)
        if len(cog_bars) > warmup_cog:
            cog_bars = cog_bars[-warmup_cog:]
        if len(cog_bars) < min_cog_bars:
            if not quiet_if_short and symbol not in self._short_history_alerted:
                self._short_history_alerted.add(symbol)
                self.tg.send_alert(f"{symbol} {tf_label} 认知帧过短({len(cog_bars)}/{min_cog_bars}"
                                   f" 根),暂不开启该周期")
            return None
        first_cog_ts = cog_bars[0][0]
        replay_trig = [b for b in slice_2h if b[0] >= first_cog_ts]
        detector, tracker = replay_cognition(
            self.params, symbol, cog_bars, replay_trig,
            obs_tf_seconds=obs_s, trigger_tf_seconds=trig_s)
        if self.engine_version == "v21":
            rp_tf = self.rp_by_tf.get(tf_label, self.rp)
            router = MarketRouter(rp_tf)
            pm = new_position_manager(self.params, self.raw_strat)
        else:
            router = V20Router(self.rp)
            pm = new_position_manager(self.params, self.raw20)
        return CampaignState(symbol=symbol, detector=detector, tracker=tracker,
                             router=router, pm=pm,
                             cog_bars=list(cog_bars),
                             trig_bars=list(bars2h),
                             tf_label=tf_label,
                             obs_tf_seconds=obs_s,
                             trigger_tf_seconds=trig_s)

    def _build_all_tf_campaigns(self, symbol: str,
                                bars2h: Optional[list] = None) -> Optional[CampaignState]:
        """构建主周期 CampaignState 并返回,同时装配 v21 的其余周期(2h/4h/1d)至 extra_campaigns。
        支持按周期自适应准入:新币若不足 12h/1d 预热长度但满足 2h/4h(>=60根认知bar),
        自动以已就绪的周期作为主实例先行交易,后续高周期随 K 线增长自动解锁。"""
        want = int(self.cog_cfg["warmup_trigger_bars"])
        if bars2h is None:
            bars2h = self._fetch_trigger_bars(symbol, want)
        main_tf = str(self.cog_cfg.get("main_tf_label") or "12h")
        if self.engine_version != "v21":
            return self._build_campaign(symbol, main_tf, bars2h=bars2h)

        tf_list = list(self.cog_cfg.get("tf_list") or [main_tf])
        built: dict[str, CampaignState] = {}
        for tf in tf_list:
            cs_tf = self._build_campaign(symbol, tf, bars2h=bars2h, quiet_if_short=True)
            if cs_tf is not None:
                built[tf] = cs_tf
        if not built:
            min_cog = self._min_cog_bars_for_tf()
            if symbol not in self._short_history_alerted:
                self._short_history_alerted.add(symbol)
                self.tg.send_alert(
                    f"{symbol} 新币 2H 历史不足({len(bars2h)}/{min_cog} 根),"
                    f"满 {min_cog} 根(约 {min_cog * 2 // 24} 天)后将自动开启交易")
            return None
        self._short_history_alerted.discard(symbol)
        primary_tf = main_tf if main_tf in built else next(iter(built))
        cs_main = built[primary_tf]
        for tf, cs_sub in built.items():
            if tf != primary_tf:
                self.extra_campaigns[(symbol, tf)] = cs_sub
        return cs_main

    def _try_unlock_higher_tfs(self, symbol: str, bars2h: list) -> None:
        """新币随 K 线积累达到高周期准入门槛时,自动解锁并挂载对应周期战役。"""
        if self.engine_version != "v21" or symbol not in self.campaigns:
            return
        tf_list = list(self.cog_cfg.get("tf_list") or [])
        active_tfs = {self.campaigns[symbol].tf_label} | {
            tf for (s2, tf) in self.extra_campaigns if s2 == symbol
        }
        min_cog = self._min_cog_bars_for_tf()
        for tf in tf_list:
            if tf in active_tfs:
                continue
            obs_s = TF_SECONDS_MAP.get(tf, int(self.cog_cfg["obs_tf_seconds"]))
            scale_2h = max(1, obs_s // 7200)
            if len(bars2h) >= (min_cog + 1) * scale_2h:
                cs_new = self._build_campaign(symbol, tf, bars2h=bars2h, quiet_if_short=True)
                if cs_new is not None:
                    self.extra_campaigns[(symbol, tf)] = cs_new


    def _get_cs_for(self, symbol: str, tf_label: Optional[str] = None) -> Optional[CampaignState]:
        if tf_label and (symbol, tf_label) in self.extra_campaigns:
            return self.extra_campaigns[(symbol, tf_label)]
        return self.campaigns.get(symbol)

    def _ensure_leverage(self, symbol: str) -> None:
        """杠杆初始化(每品种一次;hedge_mode 逐侧设置,one-way 省略 holdSide)。

        只做一次(成功入 _leverage_done);失败仅告警不中断 —— 交易所侧杠杆
        不足会在下单时如实暴露,不静默降级。
        """
        if symbol in self._leverage_done or self.mode != "live":
            return
        cap = int(self.risk.get("leverage_cap") or 20)
        # 每个合约的杠杆上限不同(AKE/ARIA/BR/JCT=10、BTW=5、BTC=150),统一按
        # 20x 设置会报 40797;低流动性期还会被动态限到更低(25159),故降档重试。
        try:
            max_lev = int(float((self.contract_raw.get(symbol) or {})
                                .get("maxLever") or cap))
        except (TypeError, ValueError):
            max_lev = cap
        want = max(1, min(cap, max_lev))
        sides = ("long", "short") if self.hedge_mode else (None,)
        ladder = [want] + [lv for lv in (10, 5, 3, 2, 1) if lv < want]
        last_err: Optional[Exception] = None
        for lev in ladder:
            try:
                for hs in sides:
                    self.client.set_leverage(symbol, lev, hold_side=hs)
                self._leverage_done.add(symbol)
                self.leverage_by_sym[symbol] = lev
                self._journal("leverage", {"symbol": symbol, "leverage": lev,
                                           "max_lever": max_lev,
                                           "sides": [s or "single" for s in sides]})
                return
            except BitgetError as e:
                last_err = e
        self._alert_throttled(
            f"lev:{symbol}",
            f"{symbol} 杠杆设置失败(已试 {ladder}):{str(last_err)[:120]}"
            f"(按交易所上限降级后仍失败,该品种下单可能受保证金限制)")

    # ------------------------------------------------------------------
    # 主循环一步
    # ------------------------------------------------------------------

    def step(self, now_ms: int) -> None:
        """主循环一步:异常照常上抛(main 处置),但断点状态必定落盘(finally)。

        修前缺陷:step 中途抛错 ⇒ _save_state() 不执行 ⇒ state.json 停摆,
        内存里的 armed 意图与持仓事实随之丢失(2026-10-22 40774 事件实证)。
        """
        try:
            self._step_inner(now_ms)
        finally:
            try:
                self._save_state()
            except Exception:
                pass

    def _step_inner(self, now_ms: int) -> None:
        if self.mode == "live":
            bal = self.client.balance()
            self.equity = float(bal["equity"])
        if self.equity is None:
            raise RuntimeError("equity 未定(live 取交易所权益;paper 需 "
                               "equity0)")
        self.breaker.update(now_ms, self.equity)
        self._universe_tick()
        if self.symbols_pending:
            pending_batch = list(self.symbols_pending)
            want = int(self.cog_cfg["warmup_trigger_bars"])
            prefetched = self._prefetch_bars_parallel(pending_batch, want)
            for sym in pending_batch:
                try:
                    bars2h = prefetched.get(sym)
                    if not bars2h:
                        bars2h = self._fetch_trigger_bars(sym, want)
                    cs = self._build_all_tf_campaigns(sym, bars2h=bars2h)
                except (BitgetError, RuntimeError) as e:
                    self._api_note(e)
                    self.tg.send_alert(f"{sym} 认知帧装配失败:{type(e).__name__} "
                                       f"{str(e)[:120]}(暂不交易)")
                    if sym in self.symbols_pending:
                        self.symbols_pending.remove(sym)
                    continue
                if cs is not None:
                    self.campaigns[sym] = cs
                    self._ensure_leverage(sym)
                if sym in self.symbols_pending:
                    self.symbols_pending.remove(sym)
        if self.mode == "live":
            self._sync_live_fills(now_ms)
        base_ms = 7200 * 1000
        for sym, cs in sorted(self.campaigns.items()):
            try:
                last_ts = cs.trig_bars[-1][0] if cs.trig_bars else 0
                # 优化:2H K 线每 7200s 收盘一次;下一根 2H bar 收盘时刻为 last_ts + 2 * base_ms。
                # 未到新 bar 收盘时刻前跳过冗余 HTTP 请求,避免每 15s 空轮询全宇宙品种。
                if last_ts > 0 and now_ms < last_ts + 2 * base_ms:
                    continue
                new_bars = self.client.history_candles(
                    sym, self.cog_cfg["granularity"], start_ms=last_ts + 1,
                    limit=100)
                if not new_bars:
                    continue
                self._advance(cs, now_ms, new_bars=new_bars, run_paper_fills=True)
                for (s2, _tf), sub_cs in sorted(self.extra_campaigns.items()):
                    if s2 == sym:
                        self._advance(sub_cs, now_ms, new_bars=new_bars, run_paper_fills=False)
                self._try_unlock_higher_tfs(sym, cs.trig_bars)
                self._save_cached_bars(sym, cs.trig_bars)
            except BitgetError as e:
                self._api_note(e)
                self.tg.send_alert(f"{sym} bar 推进异常:{str(e)[:120]}")

        self._expiry_sweep(now_ms)
        if self.mode == "live":
            self._manage_stops(now_ms)
            if now_ms / 1000 - self._reconcile_at >= \
                    self.loop_cfg["reconcile_seconds"]:
                self._reconcile_at = now_ms / 1000
                self._reconcile()
        self._daily_tick(now_ms)
        self._api_error_streak = 0          # 本轮无异常:连续错计数复位

    def _api_note(self, e: Exception) -> None:
        """API 错计数与熔断(fail-closed)。

        修前缺陷:① 计数永不复位 ⇒ 每轮重复告警刷屏(实测 5→18 连发);
        ② 熔断只中断本轮,下一轮照旧新开仓 ⇒ 告警声称"暂停"实则未停。
        现:达阈值即置 api_halted(暂停新增风险,平仓/止损/对账照常),
        告警只发一次,由 _daily_tick 跨 UTC 日复位(fail-closed 且不永久停摆)。
        """
        self._api_error_streak += 1
        if self._api_error_streak >= int(self.loop_cfg["api_error_max"]):
            self.api_halted = True
            if not self._api_halt_alerted:
                self._api_halt_alerted = True
                self.tg.send_alert(
                    f"API 错熔断:连续 {self._api_error_streak} 次接口异常"
                    f"({type(e).__name__}),新增风险已暂停(既有仓位照常管理)"
                    f",人工核查后于跨 UTC 日自动复位")
            raise e

    def _universe_tick(self) -> None:
        if not self.universe_enabled:
            return
        now = time.time()
        if now - self._universe_at < self.loop_cfg["universe_refresh_seconds"]:
            return
        self._universe_at = now
        min_vol = self.cfg["universe"]["min_usdt_volume_24h"]
        uni = self.client.universe(min_vol)
        self._journal("universe", {"min_usdt_volume_24h": min_vol,
                                   "n": len(uni), "head": uni[:8]})
        known = set(self.campaigns) | set(self.symbols_pending)
        for sym in uni:
            if sym not in known:
                self.symbols_pending.append(sym)
                self._journal("universe_add", {"symbol": sym})

    # ------------------------------------------------------------------
    # 增量推进:每根新 2H(先 paper 撮合这根的开盘/穿过,再 mic,再认知收盘)
    # —— 因果序同回放:开盘撮合① 先于 mic 步⑤' 先于认知收盘⑤
    # ------------------------------------------------------------------

    def _advance(self, cs: CampaignState, now_ms: int,
                 new_bars: Optional[list] = None,
                 run_paper_fills: bool = True) -> None:
        last = cs.trig_bars[-1][0] if cs.trig_bars else 0
        if new_bars is None:
            new_bars = self.client.history_candles(
                cs.symbol, self.cog_cfg["granularity"], start_ms=last + 1,
                limit=100)
        base_ms = 7200 * 1000
        trig_ms = max(base_ms, int(cs.trigger_tf_seconds) * 1000)
        obs_ms = max(base_ms, int(cs.obs_tf_seconds) * 1000)
        trig_scale = max(1, trig_ms // base_ms)
        scale = max(1, obs_ms // base_ms)
        for bar in new_bars:
            if bar[0] <= last:
                continue
            last = bar[0]
            if cs.trig_bars and bar[0] - cs.trig_bars[-1][0] != base_ms:
                self.tg.send_alert(
                    f"{cs.symbol}({cs.tf_label}) 2H 序列缺口 {cs.trig_bars[-1][0]} → "
                    f"{bar[0]};认知帧重装配(持仓事实保留)")
                fresh = self._build_campaign(cs.symbol, cs.tf_label)
                if fresh is not None:
                    fresh.pm.positions = cs.pm.positions
                    if (cs.symbol, cs.tf_label) in self.extra_campaigns:
                        self.extra_campaigns[(cs.symbol, cs.tf_label)] = fresh
                    else:
                        self.campaigns[cs.symbol] = fresh
                return
            if self.mode == "paper" and run_paper_fills:
                self._paper_fill_pass(cs.symbol, bar)
            cs.trig_bars.append(bar)
            for pos in self.positions.values():    # worst-case mark 就近更新
                if pos["symbol"] == cs.symbol:
                    pos["mark"] = float(bar[4])
            # 盘中子周期触价结算:持仓期间在 2H/触发子周期上检查固定 STOP / TP1,
            # 消除 12h/1d 等高周期收盘前数小时的实盘/paper 状态同步延迟
            self._on_sub_bar(cs, bar)
            # 降档触发步(当 trigger 细于认知周期,即 trig_ms < obs_ms 时生效)
            if cs.tracker is not None and trig_ms < obs_ms:
                if (bar[0] + base_ms) % trig_ms == 0 and len(cs.trig_bars) >= trig_scale:
                    t_bar = (bar if trig_scale == 1
                             else aggregate_bucket(cs.trig_bars[-trig_scale:],
                                                   bar[0] + base_ms - trig_ms))
                    k = len(cs.trig_bars) // trig_scale - 1
                    ts, o, h, l, c, _v = t_bar
                    emitted = cs.tracker.on_trigger_bar(k, ts, o, h, l, c)
                    if emitted:
                        ctx = cs.tracker._ctx
                        view = StructureView(
                            regime=ctx.regime,
                            zones=tuple(z.clone() for z in ctx.zones), atr=ctx.atr,
                            pivots=ctx.pivots[-self.params.range_max_pivots:])
                        self._route_step(cs, ctx.atr, emitted, view, mic=True)
            if (bar[0] + base_ms) % obs_ms == 0 and len(cs.trig_bars) >= scale:
                cog_bar = (bar if scale == 1
                           else aggregate_bucket(cs.trig_bars[-scale:],
                                                 bar[0] + base_ms - obs_ms))
                self._close_cognition(cs, cog_bar)

    def _on_sub_bar(self, cs: CampaignState, bar) -> None:
        """持仓期间子周期(2H)触价同步与结算:
        1. 固定 STOP(pos_pm.invalid):与回测 broker.execute_protective_stop 同口径,
           子周期极值触及(low<=stop / high>=stop)时:
           - paper 模式立即按止损价(或缺口开盘价)平仓结算并清理 pm/positions;
           - live 模式立即触发一次 _reconcile() 同步交易所保护单成交状态。
        2. 固定 TP1(pos_pm.target_1):
           - 更新 pos_pm._best 实时 MFE;
           - 当启用对侧锁利(reject_opp_lock_frac != 0)且子周期突破/触及 target_1 时,
             立即推进 invalid 并同步至 self.positions(下一拍 _manage_stops 即刻更新
             交易所止损单),无需等待 12h/1d 高周期收盘。
        """
        sym = cs.symbol
        pos_pm = cs.pm.positions.get(sym)
        pos_dict = self.positions.get(sym)
        if pos_pm is None or pos_dict is None:
            return
        if pos_dict.get("tf_label", cs.tf_label) != cs.tf_label:
            return
        ts, o, h, l, c = int(bar[0]), float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
        if int(pos_pm.opened_ts) > ts:
            return

        # 实时维护子周期极值 MFE
        pos_pm._best = min(pos_pm._best, l) if pos_pm.direction < 0 else max(pos_pm._best, h)

        # ① 固定 STOP 盘中触价结算
        stop = float(pos_pm.invalid)
        breached = (l <= stop) if pos_pm.direction > 0 else (h >= stop)
        if breached:
            ref_px = min(o, stop) if pos_pm.direction > 0 else max(o, stop)
            if self.mode == "paper":
                self._cancel_entry(sym, "sub_bar_protective_stop")
                self._cancel_stop(sym, "sub_bar_protective_stop")
                for oid, it in list(self.open_intents.items()):
                    if it["symbol"] == sym and it["status"] == "armed":
                        it["status"] = "canceled"
                cs.pm.positions.pop(sym, None)
                stop_oid = client_oid_of(
                    self.OID_PREFIX, sym, str(pos_dict.get("event_id", "STOP")),
                    "stop", ts, role="exit")[:36]
                stop_it = {
                    "oid": stop_oid,
                    "symbol": sym,
                    "role": "exit",
                    "kind": "market",
                    "direction": -int(pos_dict["direction"]),
                    "qty": float(pos_dict["qty"]),
                    "route": f"STOP|sub_bar trigger={stop:.8g}",
                    "status": "armed",
                }
                self._paper_close_fill(sym, stop_it, float(ref_px), ts)
            else:
                # live 模式:交易所已有 loss_plan 计划单,触价即刻触发对账以消除状态延迟
                self._reconcile()
            return

        # ② 固定 TP1(target_1)盘中触价/突破同步
        if pos_pm.target_1 is not None and not pos_pm.reduced and not pos_pm.pending_reduce:
            target = float(pos_pm.target_1)
            lock_cfg = float(getattr(cs.pm, "reject_opp_lock_frac", 0.0) or 0.0)
            old_inv = float(pos_pm.invalid)
            if lock_cfg < 0.0:
                broke = (c < target) if pos_pm.direction < 0 else (c > target)
                if broke:
                    pos_pm.reduced = True
                    span = target - float(pos_pm.entry)
                    frac = abs(lock_cfg)
                    lock_px = float(pos_pm.entry) + frac * span
                    pos_pm.invalid = (min(pos_pm.invalid, lock_px) if pos_pm.direction < 0
                                      else max(pos_pm.invalid, lock_px))
                    pos_pm.log.append(
                        f"v21|sub_bar_opp_break_lock|子周期收盘突破对侧缘 target={target:.8g} "
                        f"锁利({frac:.0%}) invalid {old_inv:.8g}->{pos_pm.invalid:.8g}")
            elif lock_cfg > 0.0:
                hit = (l <= target) if pos_pm.direction < 0 else (h >= target)
                if hit:
                    pos_pm.reduced = True
                    span = target - float(pos_pm.entry)
                    broke_through = (c < target) if pos_pm.direction < 0 else (c > target)
                    frac = lock_cfg if broke_through else min(0.10, lock_cfg)
                    lock_px = float(pos_pm.entry) + frac * span
                    pos_pm.invalid = (min(pos_pm.invalid, lock_px) if pos_pm.direction < 0
                                      else max(pos_pm.invalid, lock_px))
                    pos_pm.log.append(
                        f"v21|sub_bar_opp_lock|子周期触及对侧缘 target={target:.8g} "
                        f"锁利({frac:.0%}) invalid {old_inv:.8g}->{pos_pm.invalid:.8g}")
            else:
                # 传统 §8.2 target_1 触及减仓 50%
                hit = (l <= target) if pos_pm.direction < 0 else (h >= target)
                if hit:
                    extreme = l if pos_pm.direction < 0 else h
                    intent = cs.pm._reduce(pos_pm, ts, c, extreme)
                    self._exec_mgmt(cs, intent, bar)

            if abs(float(pos_pm.invalid) - old_inv) > 1e-12:
                pos_dict["invalid"] = float(pos_pm.invalid)
                last_log = pos_pm.log[-1] if pos_pm.log else "子周期防线锁利"
                self.tg.send_fill(
                    sym, f"止损推进({self.mode})",
                    float(pos_dict["qty"]),
                    c,
                    note=f"{old_inv:.6g} -> {pos_pm.invalid:.6g} ({last_log[:48]})",
                    tf_label=cs.tf_label,
                    direction=pos_pm.direction,
                    stop_price=float(pos_pm.invalid),
                    target_price=pos_pm.target_1,
                    box_lo=pos_dict.get("box_lo"),
                    box_hi=pos_dict.get("box_hi"),
                    bars=cs.trig_bars[-45:] if cs.trig_bars else cs.cog_bars[-45:],
                )

    def _close_cognition(self, cs: CampaignState, cog_bar) -> None:
        s = cs.detector.structure
        n_pivots_before = len(s.pivots)
        emitted = cs.detector.update(cog_bar)
        new_pivots = list(s.pivots[n_pivots_before:])
        i = len(cs.cog_bars)
        trig_s = max(7200, int(cs.trigger_tf_seconds))
        obs_s = max(7200, int(cs.obs_tf_seconds))
        scale = max(1, obs_s // trig_s)
        hi = max(0, len(cs.trig_bars) // max(1, trig_s // 7200) - 1)
        if cs.tracker is not None:
            cs.tracker.sync(i, cs.detector,
                            lambda p: max(0, hi - scale + 1),
                            lambda p, _h=hi: _h)
        cs.cog_bars.append(cog_bar)
        atr = s.atr()
        sym = cs.symbol
        # §8 持仓管理(入场成交的认知 bar 不做裁决,canonical 同口径)
        pos_pm = cs.pm.positions.get(sym)
        if pos_pm is not None and not (pos_pm.opened_ts > cog_bar[0]):
            old_inv = float(pos_pm.invalid)
            mgmt = cs.pm.on_bar(sym, cog_bar, atr, new_pivots, emitted or [])
            if abs(float(pos_pm.invalid) - old_inv) > 1e-12:
                pos_dict = self.positions.get(sym)
                if pos_dict is not None:
                    pos_dict["invalid"] = float(pos_pm.invalid)
                last_log = pos_pm.log[-1] if pos_pm.log else "防线收紧"
                self.tg.send_fill(
                    sym, f"止损推进({self.mode})",
                    pos_dict["qty"] if pos_dict else 0.0,
                    float(cog_bar[4]),
                    note=f"{old_inv:.6g} -> {pos_pm.invalid:.6g} ({last_log[:48]})",
                    tf_label=cs.tf_label,
                    direction=pos_pm.direction,
                    stop_price=float(pos_pm.invalid),
                    target_price=pos_pm.target_1,
                    box_lo=pos_dict.get("box_lo") if pos_dict else None,
                    box_hi=pos_dict.get("box_hi") if pos_dict else None,
                    bars=cs.cog_bars[-45:] if cs.cog_bars else cs.trig_bars[-45:],
                )
            for intent in mgmt:
                self._exec_mgmt(cs, intent, cog_bar)
        if emitted:
            view = StructureView(
                regime=s.regime(), zones=s.zones, atr=atr,
                pivots=s.pivots[-self.params.range_max_pivots:])
            self._route_step(cs, atr, list(emitted), view, mic=False)

    def _route_step(self, cs: CampaignState, atr: float, events: list, view,
                    *, mic: bool) -> None:
        """canonical 决策:支持 v20(ctx18)与 v21(MarketRouter) → 路由 → 意图。"""
        sym = cs.symbol
        if mic:
            rngq_bars, ri = cs.trig_bars, len(cs.trig_bars) - 1
            bar = cs.trig_bars[-1]
        else:
            rngq_bars, ri = cs.cog_bars, len(cs.cog_bars) - 1
            bar = cs.cog_bars[-1]
        if hasattr(cs.router, "ctx18_windows") and hasattr(cs.router, "set_ctx18"):
            wq, wp = cs.router.ctx18_windows()
            cs.router.set_ctx18({
                "rngq": range_quantile(rngq_bars, ri, wq),
                "pmd": prior_moves_delta(cs.cog_bars, len(cs.cog_bars) - 1, wp),
                "mic": mic})
        decisions = cs.router.on_bar(
            sym, bar, ri, view, atr, events,
            position_open=sym in self.positions,
            entry_resting=self._entry_resting(sym))
        t = int(bar[0])
        for d in decisions:
            if d.kind == "cancel_entry":
                self._cancel_entry(sym, str(d.meta.get("reason", "")))
            elif d.kind in ("market", "limit"):
                self._submit_open(cs, d, t, mic=mic)
            else:
                raise RuntimeError(f"unknown decision {d.kind}")

    # ------------------------------------------------------------------
    # 意图 → 闸门 → 装配(执行在撮合/live 下单层)
    # ------------------------------------------------------------------

    def _entry_resting(self, sym: str) -> bool:
        return any(it["status"] == "armed" and it["symbol"] == sym
                   and it["role"] == "entry" for it in self.open_intents.values())

    def _spec(self, sym: str) -> ContractSpec:
        sp = self.contracts.get(sym)
        if sp is None:                             # 无规格 ⇒ 兜底保守值
            return ContractSpec(symbol=sym, size_min=1e-6, size_step=1e-6,
                                size_place=6, price_place=4,
                                min_trade_usdt=5.0)
        return sp

    def _submit_open(self, cs: CampaignState, d, t: int, *, mic: bool) -> None:
        sym = cs.symbol
        hyp = d.hyp
        if self.mode == "live" and self.api_halted:
            self.counters["api_halt_skips"] = self.counters.get("api_halt_skips", 0) + 1
            self._journal("skip", {"symbol": sym, "why": "API_HALT",
                                   "event": hyp.event_id})
            return
        # hedge_mode:side 表持仓方向(开多 buy / 开空 sell)+ tradeSide=open
        side = "buy" if hyp.direction > 0 else "sell"
        entry_ref = float(d.limit_price) if d.kind == "limit" \
            else float(d.intent.ref_price)
        risk_per_unit = abs(entry_ref - float(hyp.invalid))
        if risk_per_unit <= 0.0:
            self._journal("reject", {"symbol": sym, "why": "degenerate_risk",
                                     "event": hyp.event_id})
            return
        spec = self._spec(sym)
        size_mult = float(d.meta.get("size_mult") or 1.0)
        equity = float(self.equity)
        eff_rf = self.current_risk_frac(equity)
        qty = eff_rf * size_mult * equity / risk_per_unit
        # 用该品种实际生效杠杆(而非统一上限):低上限品种按 20x 算出的数量
        # 会超出交易所可用保证金而被拒
        eff_lev = float(self.leverage_by_sym.get(sym)
                        or self.risk.get("leverage_cap") or 20)
        qty = min(qty, eff_lev * equity / entry_ref)
        qty = _floor_step(qty, spec.size_step, spec.size_place)
        # 组合容量与周期独立容量(canonical worst-case FIFO:整笔跳过)
        outstanding = sum(
            worst_case_of_position(p["direction"], p["qty"],
                                   p.get("mark") or p["entry"], p["invalid"],
                                   self.risk["taker_fee"])
            for p in self.positions.values())
        candidate = qty * risk_per_unit + qty * entry_ref * 2.0 * \
            (self.risk["taker_fee"] + self.risk["slippage_per_side"])
        tf_caps = self.risk.get("tf_cap_by_tf") or {}
        tf_cap = tf_caps.get(cs.tf_label)
        if tf_cap is not None:
            tf_out = sum(
                worst_case_of_position(p["direction"], p["qty"],
                                       p.get("mark") or p["entry"], p["invalid"],
                                       self.risk["taker_fee"])
                for p in self.positions.values()
                if p.get("tf_label", "12h") == cs.tf_label)
            if tf_out + candidate > float(tf_cap) * equity:
                self.counters["tf_cap_skips"] = self.counters.get("tf_cap_skips", 0) + 1
                self.counters["cap_skips"] += 1
                self._journal("skip", {"symbol": sym, "tf": cs.tf_label,
                                       "why": "TF_CAP", "event": hyp.event_id})
                return
        if outstanding + candidate > self.risk["portfolio_cap_frac"] * equity:
            self.counters["cap_skips"] += 1
            self._journal("skip", {"symbol": sym, "why": "PORTFOLIO_CAP",
                                   "event": hyp.event_id})
            return
        if qty * entry_ref < max(spec.min_trade_usdt,
                                 spec.size_min * entry_ref):
            self.counters["min_notional_skips"] += 1
            self._journal("skip", {"symbol": sym, "why": "MIN_NOTIONAL",
                                   "event": hyp.event_id, "qty": qty,
                                   "ref": entry_ref})
            return
        if self.breaker.should_stop(equity):
            self.counters["breaker_blocks"] += 1
            self._journal("skip", {"symbol": sym, "why": "DAILY_LOSS_BREAKER",
                                   "event": hyp.event_id})
            return
        if not self.rate.allow():
            self.counters["rate_skips"] += 1
            self._journal("skip", {"symbol": sym, "why": "RATE_LIMIT",
                                   "event": hyp.event_id})
            return
        oid = client_oid_of(self.OID_PREFIX, sym, hyp.event_id, side, t)
        # 断点幂等:同键不重发;已失败的意图亦不再重试(避免每轮重发同一坏单)
        if self.open_intents.get(oid, {}).get("status") in (
                "armed", "filled", "error"):
            return
        self.counters["signals"] += 1
        regime = hyp.regime.value if hyp.regime is not None else ""
        z_lo = d.meta.get("zone_lo")
        z_hi = d.meta.get("zone_hi")
        b_opp = d.meta.get("box_opposite")
        if z_lo is not None and z_hi is not None and b_opp is not None:
            box_lo = float(min(z_lo, z_hi, b_opp))
            box_hi = float(max(z_lo, z_hi, b_opp))
        elif z_lo is not None and z_hi is not None:
            box_lo, box_hi = float(z_lo), float(z_hi)
        else:
            box_lo, box_hi = None, None
        chart_bars = cs.trig_bars[-45:] if mic and cs.trig_bars else (cs.cog_bars[-45:] or cs.trig_bars[-45:])
        intent = {"oid": oid, "symbol": sym, "tf_label": cs.tf_label,
                  "role": "entry", "kind": d.kind, "direction": hyp.direction,
                  "qty": qty,
                  "limit_price": entry_ref if d.kind == "limit" else None,
                  "invalid": float(hyp.invalid),
                  "target_1": hyp.target_1, "event_id": hyp.event_id,
                  "route": d.route, "size_mult": size_mult,
                  "box_lo": box_lo, "box_hi": box_hi,
                  "decision_ts": t, "mic": mic,
                  "expiry_ts": t + N_FILL_H * MS_1H, "status": "armed",
                  "exchange_order_id": None}
        self.open_intents[oid] = intent
        self.tg.send_signal(sym, hyp.event_id,
                            "多" if hyp.direction > 0 else "空", d.route,
                            entry_ref, float(hyp.invalid),
                            f"规模 {qty}(风险档 {eff_rf:.2%}, ×{size_mult:g})"
                            f"{' [降档触发]' if mic else ''}",
                            regime=regime, tf_label=cs.tf_label,
                            bars=chart_bars, box_lo=box_lo, box_hi=box_hi,
                            target_1=hyp.target_1)
        if self.mode == "live":
            try:
                if d.kind == "limit":
                    resp = self.client.place_limit_order(
                        sym, side=side, size=qty,
                        price=self.fit_price(sym, entry_ref, hyp.direction),
                        client_oid=oid, trade_side=self.open_trade_side())
                    intent["exchange_order_id"] = str(resp.get("orderId") or "")
                    self.counters["orders_placed"] += 1
                else:
                    resp = self.client.place_market_order(
                        sym, side=side, size=qty, client_oid=oid,
                        trade_side=self.open_trade_side())
                    intent["exchange_order_id"] = str(resp.get("orderId") or "")
                    self.counters["orders_placed"] += 1
                    self._live_entry_filled(intent,
                                            side_fallback_price=entry_ref,
                                            now_ms=t)
            except BitgetError as e:
                # 下单失败:该意图作废且不重发;连续错达阈值由 _api_note 熔断
                intent["status"] = "error"
                self.counters["order_errors"] = \
                    self.counters.get("order_errors", 0) + 1
                self._journal("order_error", {"oid": oid, "symbol": sym,
                                              "event": hyp.event_id,
                                              "why": str(e)[:160]})
                self.tg.send_alert(f"{sym} 开仓下单失败:{str(e)[:120]}"
                                   f"(意图作废,不重试)")
                self._api_note(e)
        self._journal("intent", {k: intent[k] for k in (
            "oid", "symbol", "role", "kind", "direction", "qty",
            "limit_price", "event_id", "route", "decision_ts", "mic")})

    def _live_entry_filled(self, it: dict, *, side_fallback_price: float,
                           now_ms: int) -> None:
        """live 市价开仓:下单即按参考价记账建仓(成交价差如实以对账暴露);
        限价单留 armed 待 _sync_live_fills 确认。"""
        if it["kind"] == "limit":
            return
        price = float(side_fallback_price)
        risk = it["direction"] * (price - it["invalid"])
        if risk <= 0.0:
            self.tg.send_alert(f"{it['symbol']} 开仓参考价穿越失效位,人工核查")
            it["status"] = "void"
            return
        it["status"], it["fill_price"], it["fill_ts"] = "filled", price, now_ms
        self._register_position(it["symbol"], it, price, now_ms)

    def _register_position(self, sym: str, it: dict, price: float,
                           ts: int) -> None:
        tf_lbl = it.get("tf_label") or str(self.cog_cfg.get("main_tf_label") or "12h")
        cs = self._get_cs_for(sym, tf_lbl) or self.campaigns.get(sym)
        if cs is not None and not it.get("tf_label"):
            tf_lbl = cs.tf_label
        self.positions[sym] = {
            "symbol": sym, "tf_label": tf_lbl, "direction": it["direction"],
            "qty": it["qty"], "entry": price, "invalid": it["invalid"],
            "target_1": it["target_1"], "event_id": it["event_id"],
            "box_lo": it.get("box_lo"), "box_hi": it.get("box_hi"),
            "opened_ts": ts, "mark": price}
        if cs is not None:
            risk = it["direction"] * (price - it["invalid"])
            if risk > 0.0:
                try:
                    cs.pm.open(sym, it["direction"], price, it["invalid"],
                               it["target_1"], it["event_id"], ts,
                               len(cs.cog_bars))
                except ValueError as e:
                    self.tg.send_alert(f"{sym} pm 建仓拒绝:{e}")
        self.counters["fills"] += 1
        chart_bars = (cs.trig_bars[-45:] if (cs and it.get("mic") and cs.trig_bars)
                      else (cs.cog_bars[-45:] if (cs and cs.cog_bars) else (cs.trig_bars[-45:] if cs else None)))
        self.tg.send_fill(
            sym, f"开仓({self.mode})", it["qty"], price,
            note=f"{it['event_id']} {it['route']}",
            tf_label=tf_lbl, direction=it["direction"],
            stop_price=float(it["invalid"]), target_price=it.get("target_1"),
            box_lo=it.get("box_lo"), box_hi=it.get("box_hi"),
            bars=chart_bars)
        self._journal("fill", {"oid": it["oid"], "symbol": sym,
                               "qty": it["qty"], "price": price,
                               "role": "entry", "mode": self.mode})

    # ------------------------------------------------------------------
    # §8 持仓管理意图(REDUCE/EXIT;canonical:目标减仓=target_1 限价 maker,
    # 超时转市价;S2 段落袋等部分 REDUCE = 市价;EXIT = 市价)
    # ------------------------------------------------------------------

    def _exec_mgmt(self, cs: CampaignState, intent, cog_bar) -> None:
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
            spec = self._spec(sym)         # 全平数量同样按合约精度取整
            self._arm_close_intent(
                sym, pos,
                _floor_step(float(pos["qty"]), spec.size_step, spec.size_place),
                "market", None, intent.reason, t, role="exit")
        elif kind == "REDUCE":
            qty = _floor_step(float(intent.qty_frac or 0.0) * pos["qty"],
                              self._spec(sym).size_step,
                              self._spec(sym).size_place)
            if qty <= 0.0:
                return
            target = pos_pm.target_1 if pos_pm is not None else None
            partial_market = str(intent.reason).startswith(
                "v16|target_partial|")           # S2 段落袋市价(canonical)
            if target is not None and not partial_market:
                self._arm_close_intent(sym, pos, qty, "limit", float(target),
                                       intent.reason, t, role="reduce")
            else:
                self._arm_close_intent(sym, pos, qty, "market", None,
                                       intent.reason, t, role="reduce")

    def _arm_close_intent(self, sym: str, pos: dict, qty: float, kind: str,
                          limit_price: Optional[float], reason: str, t: int,
                          *, role: str) -> None:
        # hedge_mode:平仓 side 与开仓同侧(表持仓方向),靠 tradeSide=close 区分
        side = self.close_side_of(pos["direction"])
        oid = client_oid_of(self.OID_PREFIX, sym, str(reason)[:16], side, t,
                            role=role)[:36]
        if self.open_intents.get(oid, {}).get("status") in (
                "armed", "filled", "error"):
            return
        if not self.rate.allow():
            self.counters["rate_skips"] += 1
            return
        it = {"oid": oid, "symbol": sym, "role": role, "kind": kind,
              "direction": -pos["direction"], "qty": qty,
              "limit_price": limit_price, "invalid": pos["invalid"],
              "target_1": pos["target_1"], "event_id": pos["event_id"],
              "route": f"mgmt:{str(reason)[:40]}", "size_mult": 1.0,
              "decision_ts": t, "mic": False,
              "expiry_ts": t + N_FILL_H * MS_1H, "status": "armed",
              "exchange_order_id": None}
        self.open_intents[oid] = it
        if self.mode == "live":
            try:
                if kind == "limit":
                    resp = self.client.place_limit_order(
                        sym, side=side, size=qty,
                        price=self.fit_price(sym, float(limit_price),
                                             it["direction"]),
                        client_oid=oid, trade_side=self.close_trade_side())
                    it["exchange_order_id"] = str(resp.get("orderId") or "")
                else:
                    resp = self.client.place_market_order(
                        sym, side=side, size=qty, client_oid=oid,
                        trade_side=self.close_trade_side())
                    it["exchange_order_id"] = str(resp.get("orderId") or "")
                self.counters["orders_placed"] += 1
                if kind == "market":             # 市价即记账(限价待成交确认)
                    self._live_close_filled(it, ref_price=pos.get("mark") or
                                            pos["entry"])
            except BitgetError as e:
                it["status"] = "error"
                self.counters["order_errors"] = \
                    self.counters.get("order_errors", 0) + 1
                self._journal("order_error", {"oid": oid, "symbol": sym,
                                              "role": role,
                                              "why": str(e)[:160]})
                self.tg.send_alert(f"{sym} 平仓({role})下单失败:{str(e)[:120]}"
                                   f"(仓位仍在,人工核查)")
                self._api_note(e)
        self._journal("mgmt_intent", {"symbol": sym, "role": role,
                                      "kind": kind, "qty": qty,
                                      "reason": str(reason)[:80]})

    def _live_close_filled(self, it: dict, *, ref_price: float) -> None:
        pos = self.positions.get(it["symbol"])
        if pos is None:
            return
        qty = min(it["qty"], pos["qty"])
        gross = qty * pos["direction"] * (float(ref_price) - float(pos["entry"]))
        fee = qty * float(ref_price) * self.risk["taker_fee"]
        pos["qty"] = round(pos["qty"] - qty, 10)
        it["status"] = "filled"
        self.counters["reduce_fills" if it["role"] == "reduce" else "exits"] += 1
        cs = self._get_cs_for(it["symbol"], pos.get("tf_label")) or self.campaigns.get(it["symbol"])
        chart_bars = (cs.cog_bars[-45:] if (cs and cs.cog_bars) else (cs.trig_bars[-45:] if cs else None))
        self.tg.send_fill(
            it["symbol"], f"平仓({self.mode},{it['role']})",
            qty, float(ref_price), pnl=gross - fee,
            note=str(it["route"])[:60],
            tf_label=pos.get("tf_label", cs.tf_label if cs else "12h"),
            direction=-pos["direction"],
            stop_price=pos.get("invalid"),
            target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            bars=chart_bars)
        self._journal("close_fill", {"oid": it["oid"], "symbol": it["symbol"],
                                     "qty": qty, "role": it["role"]})
        if it["role"] == "exit" or pos["qty"] <= 1e-9:
            self.positions.pop(it["symbol"], None)

    # ------------------------------------------------------------------
    # live:限价成交同步 / 保护止损 / 撤单 / 过期与超时转市价 / 对账 / 日结
    # ------------------------------------------------------------------

    def _sync_live_fills(self, now_ms: int) -> None:
        for oid, it in list(self.open_intents.items()):
            if it["status"] != "armed" or it["kind"] != "limit":
                continue
            ex_id = it.get("exchange_order_id")
            if not ex_id:
                continue
            try:
                fills = self.client.recent_fills(it["symbol"], limit=20)
            except BitgetError:
                return
            hit = next((f for f in fills
                        if str(f.get("orderId")) == ex_id), None)
            if hit is None:
                continue
            qty = float(hit.get("size", it["qty"]))
            price = float(hit.get("price", it.get("limit_price") or 0.0))
            if it["role"] == "entry":
                it["qty"] = qty
                it["status"], it["fill_price"], it["fill_ts"] = \
                    "filled", price, now_ms
                self._register_position(it["symbol"], it, price, now_ms)
            else:
                self._live_close_filled(it, ref_price=price)

    def _manage_stops(self, now_ms: int) -> None:
        """保护止损计划单(loss_plan @ 当前 invalid;invalid/数量变动撤旧换新)。
        只保护不主出:结构出场一律经 §8/canonical 路径。"""
        for sym, pos in self.positions.items():
            want = self.fit_price(sym, float(pos["invalid"]), pos["direction"])
            cur = self.stops.get(sym)
            # 数量必须按合约精度取整:精度不符会被交易所直接拒单
            # (实测 40808 size checkScale),导致保护永远挂不上
            spec = self._spec(sym)
            stop_qty = _floor_step(float(pos["qty"]), spec.size_step,
                                   spec.size_place)
            if cur is not None and abs(cur["trigger"] - want) < 1e-12 \
                    and abs(cur["qty"] - stop_qty) < 1e-12:
                continue
            try:
                if cur is not None and cur.get("order_id"):
                    self.client.cancel_plan_order(sym, cur["order_id"])
                resp = self.client.place_stop_market_plan(
                    sym, trigger_price=want, size=stop_qty,
                    plan_type="loss_plan",
                    hold_side="long" if pos["direction"] > 0 else "short",
                    side=self.close_side_of(pos["direction"]),
                    trade_side=self.close_trade_side(),
                    client_oid=client_oid_of(
                        self.OID_PREFIX, sym, str(pos["event_id"]),
                        "stop", int(now_ms), role="stop"))
                self.stops[sym] = {"trigger": want, "qty": stop_qty,
                                   "order_id": str(resp.get("orderId") or "")}
                self._journal("stop", {"symbol": sym, "trigger": want,
                                       "qty": stop_qty})
            except BitgetError as e:
                self.counters["stop_errors"] = \
                    self.counters.get("stop_errors", 0) + 1
                self._journal("stop_error", {"symbol": sym, "trigger": want,
                                             "qty": stop_qty,
                                             "why": str(e)[:160]})
                self._alert_throttled(
                    f"stop:{sym}",
                    f"{sym} 保护止损计划失败:{str(e)[:120]}"
                    f"(当前无触发保护,人工介入;同类告警 30 分钟内不重复)")

    def _cancel_stop(self, sym: str, why: str) -> None:
        cur = self.stops.pop(sym, None)
        if cur is not None and cur.get("order_id") and self.mode == "live":
            try:
                self.client.cancel_plan_order(sym, cur["order_id"])
            except BitgetError:
                pass

    def _cancel_entry(self, sym: str, reason: str) -> None:
        for oid, it in list(self.open_intents.items()):
            if it["symbol"] == sym and it["role"] == "entry" \
                    and it["status"] == "armed":
                if self.mode == "live" and it.get("exchange_order_id"):
                    try:
                        self.client.cancel_order(sym, it["exchange_order_id"])
                    except BitgetError:
                        pass
                it["status"] = "canceled"
                self._journal("cancel", {"oid": oid, "symbol": sym,
                                         "reason": str(reason)[:80]})

    def _expiry_sweep(self, now_ms: int) -> None:
        """到期处置:entry 撤单作废(canonical limit_expired);reduce/exit
        超时转市价(TIMEOUT_TO_TAKER;live 撤单+市价,report 如实计数)。"""
        for oid, it in list(self.open_intents.items()):
            if it["status"] != "armed" or now_ms <= it["expiry_ts"]:
                continue
            if it["role"] == "entry":
                self._cancel_entry(it["symbol"], "N_fill_expiry")
                it["status"] = "expired"
                self.counters["expired_intents"] += 1
            else:
                if self.mode == "live" and it.get("exchange_order_id"):
                    try:
                        self.client.cancel_order(it["symbol"],
                                                 it["exchange_order_id"])
                    except BitgetError:
                        pass
                    # 平仓单 direction 记的是报单方向(= -持仓方向);超时转市价
                    # 同为平仓单,side 语义由持仓模式决定(hedge:与持仓同侧 + close)
                    side = self.close_side_of(-it["direction"])
                    try:
                        self.client.place_market_order(
                            it["symbol"], side=side, size=it["qty"],
                            client_oid=oid[:30] + "cv",
                            trade_side=self.close_trade_side())
                        self._live_close_filled(
                            it, ref_price=(self.positions.get(it["symbol"], {})
                                           .get("mark") or 0.0))
                    except BitgetError as e:
                        self.tg.send_alert(f"{it['symbol']} 超时转市价失败:"
                                           f"{str(e)[:100]}")
                    self.counters["orders_placed"] += 1
                self.counters["expired_intents"] += 1
                self._journal("timeout_to_taker", {"oid": oid,
                                                   "symbol": it["symbol"]})

    def _last_fill_price(self, sym: str) -> Optional[float]:
        """该品种最近一笔'平仓'成交价(离场价);取不到返回 None(如实不编)。

        只认 tradeSide=close:fills 为倒序(新→旧)且含开仓成交,直接取末条会
        拿成开仓价 ⇒ 算出 pnl≈0 的误导性回执(实测 AAVE 取成 157.67)。
        """
        try:
            res = self.client.recent_fills(sym, limit=50)
        except BitgetError:
            return None
        rows = res if isinstance(res, list) else (
            (res or {}).get("fillList") or (res or {}).get("fills") or [])
        px: Optional[float] = None
        for r in rows:
            if str(r.get("symbol")) != sym:
                continue
            if str(r.get("tradeSide") or "") != "close":
                continue
            try:
                px = float(r.get("price"))
            except (TypeError, ValueError):
                continue
        return px

    def _reconcile_closed(self, sym: str, pos: dict) -> None:
        """交易所已无此仓(保护止损触发 / 外部平仓):以交易所为准清理本地事实。

        修前缺陷:只报 ours_only 且文案误写为"异己仓",本地持仓事实永不清理
        ⇒ ① 该品种被 position_open 卡住不再开新仓;② 每轮重复误报告警。
        """
        cs = self._get_cs_for(sym, pos.get("tf_label")) or self.campaigns.get(sym)
        if cs is not None:
            cs.pm.positions.pop(sym, None)
        self.stops.pop(sym, None)
        self.positions.pop(sym, None)
        self._missing_streak.pop(sym, None)
        px = self._last_fill_price(sym)
        pnl = None
        if px is not None:
            pnl = (float(pos["qty"]) * int(pos["direction"])
                   * (px - float(pos["entry"]))
                   - float(pos["qty"]) * px * self.risk["taker_fee"])
        self.counters["reconcile_closed"] = \
            self.counters.get("reconcile_closed", 0) + 1
        self._journal("reconcile_closed", {"symbol": sym,
                                           "direction": pos["direction"],
                                           "qty": pos["qty"],
                                           "exit_price": px})
        self.tg.send_fill(
            sym, "离场确认(交易所已无此仓)", float(pos["qty"]),
            float(px if px is not None else pos.get("mark") or pos["entry"]),
            pnl=pnl,
            note="保护止损触发或外部平仓;本地持仓事实已按交易所为准清理",
            tf_label=pos.get("tf_label"), direction=int(pos["direction"]),
            stop_price=float(pos["invalid"]), target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"))

    def _reconcile(self) -> None:
        try:
            ex_pos = self.client.positions()
        except BitgetError as e:
            self._api_note(e)
            return
        ex_by = {(p["symbol"], p["direction"]): p for p in ex_pos}
        # ① 交易所已无此仓:连续 2 轮确认(防接口偶发漏报)后清理本地事实
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
                # 交易所即真相:本地数量因精度放大(下单被截断)会使平仓超仓,
                # 故以实际持仓为准修正本地与 pm 侧数量,并如实入账。
                self.positions[sym]["qty"] = float(p["qty"])
                cs = self._get_cs_for(sym, self.positions[sym].get("tf_label"))
                pos_pm = cs.pm.positions.get(sym) if cs is not None else None
                if pos_pm is not None:
                    try:
                        pos_pm.qty = float(p["qty"])
                    except Exception:
                        pass
                self.counters["reconcile_fixes"] = \
                    self.counters.get("reconcile_fixes", 0) + 1
                self._journal("reconcile_fix", {"symbol": sym, "ours": ours,
                                                "exchange": float(p["qty"])})
                self._alert_throttled(
                    f"qty:{sym}",
                    f"{sym} 持仓数量以交易所为准修正 {ours} -> {p['qty']}"
                    f"(下单精度须取自合约规格)")
        mismatches = reconcile_positions(self.positions, ex_pos)
        for m in mismatches:
            self.counters["reconcile_alerts"] += 1
            self._journal("reconcile", {"mismatch": m})
            # 文案按 mismatch 类型分述:修前三型一律写成"异己仓",与 ours_only
            # (本地有仓、交易所已无)的语义正好相反,误导人工核查。
            kind = str(m).split()[0]
            if kind == "exchange_only":
                tail = "交易所存在非本系统所开仓位 —— 本系统不触碰,人工核查"
            elif kind == "qty_mismatch":
                tail = "数量不一致 —— 已以交易所为准修正(下单精度须取自合约规格)"
            else:                       # ours_only
                tail = "本地有仓而交易所已无 —— 待下轮确认后按交易所为准清理"
            self._alert_throttled(
                f"reconcile:{' '.join(str(m).split()[:2])}",
                f"持仓对账不一致:{m} —— {tail}")
        if not mismatches:
            self._journal("reconcile", {"ok": True,
                                        "ours": len(self.positions),
                                        "exchange": len(ex_pos)})

    def _daily_tick(self, now_ms: int) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) \
            .strftime("%Y-%m-%d")
        if self._daily_date is None:
            self._daily_date = day
            return
        if day != self._daily_date:
            self.api_halted, self._api_halt_alerted = False, False
            self._api_error_streak = 0       # 跨 UTC 日:熔断复位(fail-closed 且不永久停摆)
            anchor = self.breaker.start_equity or self.equity
            dd = (anchor - self.equity) / anchor if anchor else 0.0
            self.tg.send_daily(self._daily_date, float(self.equity),
                               float(self.equity) - float(anchor or 0),
                               self.counters["signals"], self.counters["fills"],
                               len(self.positions), dd)
            self._daily_date = day

    # ------------------------------------------------------------------
    # paper 虚拟经纪(canonical 撮合语义:市价=次一 2H 开盘;限价=严格穿过;
    # 超时转市价;费用 taker)
    # ------------------------------------------------------------------

    def _paper_fill_pass(self, sym: str, bar) -> None:
        ts, o, h, l, c, _v = bar
        for oid, it in list(self.open_intents.items()):
            if it["status"] != "armed" or it["symbol"] != sym:
                continue
            if ts > it["expiry_ts"]:
                if it["role"] == "entry":
                    it["status"] = "expired"
                    self.counters["expired_intents"] += 1
                    self._journal("expired", {"oid": oid, "symbol": sym})
                    continue
                fill, mk = float(o), "market"    # TIMEOUT_TO_TAKER
                self.counters["expired_intents"] += 1
            else:
                mk = None
                if it["kind"] == "market":
                    fill = float(o)
                else:
                    lp = float(it["limit_price"])
                    # role=entry:direction=开仓方向;role=reduce:direction=平仓
                    # 单方向。两种角色同判:买单等低穿、卖单等高穿(严格,maker)
                    if it["direction"] > 0 and l < lp:
                        fill = lp
                    elif it["direction"] < 0 and h > lp:
                        fill = lp
                    else:
                        continue
            if it["role"] == "entry":
                self._paper_entry_fill(sym, oid, it, fill, ts)
            else:
                self._paper_close_fill(sym, it, fill, ts)

    def _paper_entry_fill(self, sym: str, oid: str, it: dict, price: float,
                          ts: int) -> None:
        risk = it["direction"] * (price - it["invalid"])
        if risk <= 0.0:
            it["status"] = "void"
            self.tg.send_alert(f"{sym} 成交价穿越失效位,放弃建仓"
                               f"(price={price} invalid={it['invalid']})")
            return
        fee = it["qty"] * price * self.risk["taker_fee"]
        self.equity -= fee
        self._register_position(sym, it, price, ts)

    def _paper_close_fill(self, sym: str, it: dict, price: float,
                          ts: int) -> None:
        pos = self.positions.get(sym)
        if pos is None:
            it["status"] = "void"
            return
        qty = min(it["qty"], pos["qty"])
        gross = qty * pos["direction"] * (price - pos["entry"])
        fee = qty * price * self.risk["taker_fee"]
        self.equity += gross - fee
        it["status"] = "filled"
        self.counters["reduce_fills" if it["role"] == "reduce"
                      else "exits"] += 1
        cs = self._get_cs_for(sym, pos.get("tf_label")) or self.campaigns.get(sym)
        chart_bars = (cs.cog_bars[-45:] if (cs and cs.cog_bars) else (cs.trig_bars[-45:] if cs else None))
        self.tg.send_fill(
            sym, f"平仓(paper,{it['role']})", qty, price,
            pnl=gross - fee, note=str(it["route"])[:60],
            tf_label=pos.get("tf_label", cs.tf_label if cs else "12h"),
            direction=-pos["direction"],
            stop_price=pos.get("invalid"),
            target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            bars=chart_bars)
        self._journal("close_fill", {"oid": it["oid"], "symbol": sym,
                                     "qty": qty, "price": price,
                                     "pnl": gross - fee})
        pos["qty"] = round(pos["qty"] - qty, 10)
        if it["role"] == "exit" or pos["qty"] <= 1e-9:
            self.positions.pop(sym, None)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load_contracts_live(client, symbols: Optional[list] = None) -> dict:
    """合约规格(实盘拉取 → ContractSpec);min_notional 与数量精度唯一来源。

    symbols=None ⇒ 全量加载。修前缺陷:只为核心 4 品种加载 ⇒ 其余品种走
    _spec() 兜底(1e-6 / 6 位) ⇒ 下单数量被交易所按真实精度截断,而本地按
    未截断值记账 ⇒ 持仓对账 qty_mismatch(2026-10-22 PAXGUSDT
    ours=0.139496 vs exchange=0.139),平仓时会超仓。
    """
    want = set(symbols) if symbols else None
    out, raw = {}, {}
    for entry in client.contracts():
        sym = entry.get("symbol")
        if want is None or sym in want:
            out[sym] = ContractSpec.from_entry(entry)
            raw[sym] = entry          # 保留原始条目:maxLever 等交易所口径字段
    return out, raw


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="live_runner")
    ap.add_argument("--mode", required=True, choices=("live", "paper"),
                    help="MODE 必须显式(live 发真单;paper 干跑)")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    return ap.parse_args(argv)


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):      # GBK 控制台
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    cred_src = cfg.get("credentials_source")
    cred_path = (ROOT / cred_src) if cred_src and (ROOT / cred_src).exists() else None
    secrets = load_secrets(cred_path, config_overrides=cfg.get("credentials"))
    client = BitgetLiveClient(secrets)
    if args.mode == "live":
        # 持仓模式执行口径校验(fail-closed):配置与交易所不一致即拒绝启动,
        # 否则 side/tradeSide 语义错位会把平仓下成反向开仓。
        want_mode = str((cfg.get("account") or {}).get("position_mode") or "")
        got_mode = client.position_mode(str(cfg["symbols_core"][0]))
        if want_mode and got_mode != want_mode:
            raise RuntimeError("持仓模式不一致(fail-closed):"
                               f"config={want_mode} exchange={got_mode};"
                               "请修正 config/live.json account.position_mode")
        print(f"[live_runner] 持仓模式={got_mode}"
              f"(开多 buy+open / 开空 sell+open / 平多 buy+close / 平空 sell+close)"
              if got_mode == "hedge_mode" else
              f"[live_runner] 持仓模式={got_mode}(单边:side 表买卖方向)",
              flush=True)
    tg = TelegramNotifier(
        secrets, dropped_log=ROOT / "runtime" / "live" / "tg_missed.jsonl",
        async_mode=True)
    contracts, contract_raw = load_contracts_live(client)   # 全量:精度/min_notional/maxLever
    runner = LiveRunner(args.mode, cfg, client, tg,
                        contracts=contracts, contract_raw=contract_raw,
                        symbols=tuple(cfg["symbols_core"]))
    poll = int(cfg["loop"]["poll_seconds"])
    r = cfg["risk"]
    print(f"[live_runner] mode={args.mode} 主循环起动(每 {poll}s;risk_frac="
          f"{r['risk_frac']} 杠杆≤{r['leverage_cap']} 组合≤"
          f"{r['portfolio_cap_frac']} 日损熔断 {r['max_daily_loss_frac']} "
          f"每时单限 {r['orders_per_hour']};宇宙 >= "
          f"{cfg['universe']['min_usdt_volume_24h']:.0f} USDT)", flush=True)
    tg.send_alert(f"live_runner 起动(mode={args.mode});信号/成交/日结/告警"
                  f"通道就绪")
    while True:
        now_ms = int(time.time() * 1000)
        try:
            runner.step(now_ms)
        except BitgetError as e:
            print(f"[live_runner] api error: {e}", flush=True)
        except SecretsError as e:
            print(f"[live_runner] secrets error: {e}", flush=True)
            raise
        time.sleep(poll)


if __name__ == "__main__":
    main()
