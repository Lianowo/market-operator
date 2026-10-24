"""实盘 runner(实盘接入件;v21 单一生产链;直接提交,不推不合)。

主循环(每 15s 轮询):宇宙发现(24h USDT 名义量 > 3M)→ 每品种认知帧
(12h 主 + 下级触发 2H,TRIGGER_TF_MAP 精确命中)→ canonical 认知 + 路由
(MarketRouter + 对侧锁利 PositionManager)→ 订单意图 → broker-live(--mode live
才发真单)→ Telegram 通知;持仓对账 runner↔exchange 每 2min;断点重启 state.json。

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
  裁决(canonical 同口径:opened_ts > bar 标签才跳过)。

用法:
    python scripts/live_runner.py --mode live     # 主 agent 启动(本件不启动)
    python scripts/live_runner.py --mode paper    # 干跑(不发真单)
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import traceback
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
    ContractSpec, floor_step)
from market_operator.events import EventDetector                # noqa: E402
from market_operator.heartbeat import (                          # noqa: E402
    SCHEMA as HB_SCHEMA, HeartbeatGuard, iso_utc)
from market_operator.hypothesis import StructureView            # noqa: E402
from market_operator.position import (                          # noqa: E402
    Position, PositionManager, opp_lock_step)
from market_operator.secrets_loader import SecretsError, load_secrets  # noqa: E402
from market_operator.routing import RouterParams, MarketRouter      # noqa: E402
from market_operator.structure import Params                    # noqa: E402
from market_operator.tg import TelegramNotifier                 # noqa: E402
from market_operator.trigger import (                           # noqa: E402
    MicTracker, resolve_trigger_tf)

CONFIG_PATH = ROOT / "config" / "live.json"
STRATEGY_CONFIG = ROOT / "config" / "strategy.json"   # 生产策略配置(唯一)

MS_1H = 3_600_000
N_FILL_H = 12                                    # canonical N_fill(墙钟小时)
TF_SECONDS_MAP = {"1h": 3600, "2h": 7200, "4h": 14400, "12h": 43200, "1d": 86400}


# ---------------------------------------------------------------------------
# canonical 参数组(v21 唯一生产链)
# ---------------------------------------------------------------------------


def load_strategy_params(cfg_path: Path = STRATEGY_CONFIG) -> tuple:
    """加载生产策略参数:平衡箱 + 双原语(ACCEPT/REJECT) + 周期分治路由。

    与 ``scenario.load_settings`` 同一 fail-closed 纪律:事件来源必须是平衡箱,
    否则这条链根本没有事件可路由,拿到手的只是一套"看起来在跑"的参数。
    """
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not bool(raw.get("use_balance_detector", False)):
        raise ValueError(
            f"{cfg_path}: 实盘链路要求 use_balance_detector=true"
            "(v21 之外没有可用的生产事件来源)")
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


def new_position_manager(params: Params, raw: dict) -> PositionManager:
    """持仓侧接线:唯一生产语义(对侧突破锁利 + Turtle)。

    v20 arm-full(harvest/time_cap/trail_arm_gate)已随 ``lifecycle`` 退役;
    没有平衡箱的配置直接报错,而不是拿旧 chain 的持仓参数顶上 —— 那样会让
    "换了一套语义"在运行期悄无声息地发生。
    """
    if not raw.get("use_balance_detector"):
        raise ValueError(
            "new_position_manager: 需要 use_balance_detector=true"
            "(v21 之外没有可用的生产持仓语义)")
    arm_cfg = raw.get("arm_config") or {}
    # 锁利 frac 不设兜底默认值:它已被标定过(2026-10-24 −0.50 → −0.25),任何写死的
    # 默认值都会在下次标定后悄悄让实盘与回放分叉 —— 这正是 v21.6 的老问题。缺键
    # 直接报错,让"没配"和"配了这个值"可区分。
    if "reject_opp_lock_frac" not in arm_cfg:
        raise ValueError(
            "new_position_manager: 缺 arm_config.reject_opp_lock_frac。"
            "锁利预算没有安全默认值,请显式写入配置(生产值见 config/strategy.json)。")
    return PositionManager(
        params,
        allow_add=False,
        breakeven_on_reduce=bool(arm_cfg.get("breakeven_on_reduce", True)),
        time_stop=False,
        reject_opp_lock_frac=float(arm_cfg["reject_opp_lock_frac"]),
        # 锁利只有一个参数:符号 = arming 口径(<0 收盘突破 / >0 盘中触及),
        # |frac| = 目标处锁住的行程比例 ⇒ 回吐预算 (1−|frac|)·|target−entry|。
        # v21.7 起回放与实盘共用 position.opp_lock_step,不再有"实盘专用"
        # 的 R 单位缓冲(旧的 opp_lock_max_giveback_r 已删除)。
    )


# ---------------------------------------------------------------------------
# 风控断件(纯函数级;测试钉死)
# ---------------------------------------------------------------------------


def _utc_ms(iso: Optional[str]) -> Optional[int]:
    """ISO8601(UTC) → epoch ms;解析失败返回 None(不猜测,不兜底)。"""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(str(iso)).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


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
                 heartbeat_path: Optional[Path] = None,
                 symbols: tuple = (),
                 universe_enabled: bool = True) -> None:
        if mode not in ("live", "paper"):
            raise RuntimeError("MODE 必须显式为 live 或 paper(fail-closed)")
        self.mode, self.cfg, self.client, self.tg = mode, cfg, client, tg
        self.risk = cfg["risk"]
        self.loop_cfg = cfg["loop"]
        self.cog_cfg = cfg["cognition"]
        # 单一生产链:engine 字段已从 config/live.json 删除;这里读取只为了让
        # 残留旧配置显式失败,而不是悄悄跑成 v21。
        engine = str(self.cog_cfg.get("engine") or "v21")
        if engine != "v21":
            raise RuntimeError(
                f"cognition.engine={engine!r} 不再支持:实盘只有 v21 一条生产链"
                "(v20/lifecycle 已退役,见 reports/refactor_20261023/DESIGN.md D9)")
        self.raw_strat, self.params, self.rp, self.rp_by_tf = load_strategy_params()
        self.contracts = contracts or {}
        self.contract_raw = contract_raw or {}     # 交易所原始合约条目(maxLever 等)
        self.leverage_by_sym: dict[str, int] = {}  # 各品种实际生效杠杆
        self.equity: Optional[float] = \
            float(equity0) if equity0 is not None else None
        self.state_path = Path(state_path) if state_path else \
            ROOT / self.loop_cfg["state_path"]
        # 冷数据同目录:热档 state.json(每步) + 冷档 campaigns.json(变更/60s)
        self.campaigns_path = self.state_path.with_name("campaigns.json")
        # 心跳路径归属(2026-10-24 缺陷修复,移植 main 14a22b5):修前只认 config
        # 里的相对串,且按 **cwd** 解析 —— "真配置 + 只把 state/journal 指到 tmp"
        # 的测试会在构造那一刻覆盖**生产** runtime/live/heartbeat.json,而看护唯一
        # 的判活证据就是它(测试的合成时钟还可能在未来 ⇒ age<0 恒判"正常")。
        # 现在:显式参数 > 显式 state_path 的同目录(沙盒/测试)> 配置(锚定 ROOT)。
        if heartbeat_path is not None:
            self.heartbeat_path = Path(heartbeat_path)
        elif state_path is not None:
            self.heartbeat_path = self.state_path.parent / "heartbeat.json"
        else:
            hb_rel = self.loop_cfg.get("heartbeat_path")
            self.heartbeat_path = (ROOT / str(hb_rel) if hb_rel
                                   else self.state_path.parent / "heartbeat.json")
        self.started_ms = int(time.time() * 1000)
        self._loop_ts_ms = self.started_ms
        self._step_started_ms = 0                  # 当前步的开始时刻(卡在哪一步)
        self._step_count = 0
        self._campaigns_dirty = True
        self._campaigns_saved_at = 0.0
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
        # 掉线补做(gap recovery):_load_state 以崩溃时刻的末根 bar 为界切分,
        # 缺口 bar 在首次 step 内经 _advance 逐根补放(canonical 持仓管理),
        # 补放期间禁止新开仓,补放后按交易所事实结算"错过"的止盈/止损。
        self._gap_bars: dict[str, list] = {}
        self._gap_started_ms: Optional[int] = None
        self._gap_replay = False                   # True 时只做管理,不产生新开仓
        self._gap_stop_hit: dict[str, dict] = {}   # 缺口内曾触及的固定止损
        self._stops_checked_at = 0.0               # 交易所止损单存在性校验 TTL
        self._stops_live_ids: Optional[set] = None
        self._stops_live_rows: Optional[list] = None   # 同上快照的原始行(去重用)
        self._load_state()
        # 启动即落一次心跳:看护立刻看到新 pid + 新 started_ms,不会把上一具
        # 尸体留下的旧 loop_ts_ms 误判成"卡死"并对新进程动手。
        try:
            self._write_json_atomic(
                self.heartbeat_path, self._heartbeat_payload(self.started_ms))
        except Exception:
            pass

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

    def stop_hold_side(self, position_direction: int) -> str:
        """原生 TPSL 的 holdSide:hedge_mode 用持仓方向(long/short),
        one-way 用买卖方向(buy 表多仓 / sell 表空仓,同 ccxt 口径)。

        传错会 400172 被拒 ⇒ 该仓位无交易所侧止损(只剩本地轮询看护)。
        """
        if self.hedge_mode:
            return "long" if int(position_direction) > 0 else "short"
        return "buy" if int(position_direction) > 0 else "sell"

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
        lo = floor_step(px, step, place)
        if direction > 0:                      # 买/平空:向下(不越过前缘)
            return lo
        # 卖/平多:向上一个 step 至不小于原触发价(floor_step 对负数返回 0,
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
        # 冷数据优先读 campaigns.json(新格式);缺失则回落到 state.json 内嵌
        # campaigns(旧格式),旧文件在下一次落盘时自动瘦身。
        saved_camps: dict = {}
        cold_doc: dict = {}
        if self.campaigns_path.exists():
            try:
                cold_doc = json.loads(
                    self.campaigns_path.read_text(encoding="utf-8"))
                saved_camps = cold_doc.get("campaigns") or {}
            except (OSError, ValueError) as e:
                self.tg.send_alert(f"campaigns.json 读取失败({type(e).__name__})"
                                   f";回落 state.json 内嵌数据")
                saved_camps, cold_doc = {}, {}
        if not saved_camps:
            saved_camps = st.get("campaigns") or {}
        # 掉线补做的两个锚点:
        #   gap_started_ms = 崩溃前的最后心跳(优先用上次未补完的锚,避免二次
        #   重启把缺口起点挪到补做中途);缺口 bar = (崩溃末根 bar, 现在]。
        persisted_gap = (cold_doc.get("gap_bars")
                         or st.get("gap_bars") or {})
        persisted_start = st.get("gap_started_ms")
        self._gap_started_ms = (int(persisted_start) if persisted_start
                                else _utc_ms(st.get("saved_at_utc")))
        for sym, bars in persisted_gap.items():
            if bars:
                self._gap_bars[sym] = [tuple(b) for b in bars]
        if saved_camps:
            want = int(self.cog_cfg["warmup_trigger_bars"])
            prefetched = self._prefetch_bars_parallel(
                list(saved_camps.keys()), want,
                seed_by_sym={sym: saved.get("trig_bars")
                             for sym, saved in saved_camps.items()})
            for sym, saved in saved_camps.items():
                try:
                    bars2h = prefetched.get(sym) or []
                    saved_bars = saved.get("trig_bars") or []
                    cut = max((int(b[0]) for b in saved_bars), default=None)
                    if cut is not None and bars2h and int(bars2h[-1][0]) > cut:
                        gap = [tuple(b) for b in bars2h if int(b[0]) > cut]
                        if gap:
                            # 已持久化的缺口批次优先(补做中断后重入同一批)
                            self._gap_bars.setdefault(sym, gap)
                        bars2h = [b for b in bars2h if int(b[0]) <= cut]
                    # 以崩溃时刻为界重建认知:随后的缺口 bar 必须重新走一遍
                    # _advance(否则 _on_sub_bar / pm.on_bar 永远看不到它们,
                    # 移动防线与 TP1 会在重启后静默丢失)。
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
        if not self._gap_bars:
            # 没有待补批次 ⇒ 不留缺口锚。锚只为"未补完"的批次存在;
            # 老锚留着会让下一次重启把几小时前的缺口当成新缺口(实测报 19h)。
            self._gap_started_ms = None


    def heartbeat_doc(self, now_ms: int, *, status: Optional[str] = None,
                      guard: Optional[dict] = None) -> dict:
        """心跳文档(跨平台看护的**唯一**判活依据;不含任何进程名/端口概念)。

        三个时间事实分开写,看护据此区分三种"没有进展":
        * `ts_ms` —— 心跳最后一次落盘(进程还活着吗);
        * `loop_ts_ms` —— 主循环最后一次**成功**走完一步(是不是卡住了);
        * `step_started_ms` —— 当前步的开始时刻(卡在哪一步)。

        `positions_detail` 是"进程已经没了"场景的保护快照:看护凭它把止损
        重新挂回交易所(2026-10-22 事故:进程消失、交易所侧止损成交已了、本地无感知)。
        """
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
                "stop": st.get("trigger"), "stop_id": st.get("order_id")})
        doc = {
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
            "armed": sum(1 for it in self.open_intents.values()
                         if it.get("status") == "armed"),
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

    def _write_json_atomic(self, path: Path, payload: str) -> None:
        """同目录临时文件 + `os.replace` 原子落盘(读者永远看到完整文件)。

        修前 `write_text` 直接覆盖:进程在写入中途被杀就留下半截 JSON ⇒
        `_load_state` 解析失败 ⇒ 持仓/止损事实全丢,只能靠交易所侧计划单兜底。
        后缀为 `.gz` 时按 gzip 写(内容仍是同一份 JSON 文本,纯密度优化)。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")

        def _dump(target: Path) -> None:
            if path.suffix == ".gz":
                with gzip.open(target, "wt", encoding="utf-8") as fh:
                    fh.write(payload)
            else:
                target.write_text(payload, encoding="utf-8")

        _dump(tmp_path)
        try:
            os.replace(tmp_path, path)
        except OSError:
            # 读者以独占方式打开(资源管理器预览 / 编辑器)时 replace 会失败。
            # 宁可短暂失去原子性,也不能把这次落盘整个丢掉。
            _dump(path)
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _touch_campaigns(self) -> None:
        """标记战役状态已变:下一次落盘必须写 campaigns.json(不等档期)。"""
        self._campaigns_dirty = True

    def _campaigns_payload(self) -> str:
        """战役状态(冷数据):每品种 **尾部** bar + pm 持仓事实。

        为什么只留尾部:`_load_state` 只用它做两件事 —— ① 缺口切分锚
        (`cut = max(ts)`)② 增量拉取的 seed。完整历史在 K 线缓存与交易所,
        留 720 根纯属浪费(实测 151 品种 × 720 根 = 6.1MB,占 state.json 的
        99.6%)。尾部 `state_bar_tail` 根足以覆盖任何合理停机窗口,再长的
        缺口由缓存 + 分页补齐。
        """
        tail = max(1, int(self.loop_cfg.get("state_bar_tail", 24)))
        camps = {}
        for sym, cs in self.campaigns.items():
            pm_pos = {}
            all_cs = [cs] + [sub for (s2, _), sub in self.extra_campaigns.items()
                             if s2 == sym]
            for c_item in all_cs:
                for s, p in c_item.pm.positions.items():
                    rec = {f: getattr(p, f) for f in Position.__dataclass_fields__
                           if not f.startswith("_")}
                    rec["_best"], rec["_entry0"] = p._best, p._entry0
                    pm_pos[s] = rec
            camps[sym] = {"trig_bars": cs.trig_bars[-tail:],
                          "pm_positions": pm_pos}
        return json.dumps(
            {"saved_at_utc": datetime.now(timezone.utc)
             .isoformat(timespec="seconds"),
             "bar_tail": tail,
             # 未补完的缺口批次放冷档:长停机时它可达数万根 bar,若放热档会
             # 让"每步 22KB 的心跳"在补做期间膨胀成 MB 级全量重写。
             "gap_bars": self._gap_bars,
             "campaigns": camps}, ensure_ascii=False)

    def _save_state(self) -> None:
        """两档落盘:state.json(热,每步) + campaigns.json(冷,变更或到档期)。

        热档只有持仓/意图/保护单/熔断/心跳(实测 23KB),必须每步写;冷档是
        战役历史(曾被每 15s 全量重写 6.1MB ≈ 425KB/s 持续写盘,并把每步
        阻塞几百毫秒)。拆分后常态写放大降到 ~3.4KB/s,且信息量不减:冷档
        只在 bar 追加 / 防线推进 / 仓位变化时写,外加
        `loop.campaigns_write_seconds`(默认 120s)兜底。
        """
        payload = json.dumps({
            "saved_at_utc": datetime.now(timezone.utc)
            .isoformat(timespec="seconds"),
            "mode": self.mode, "equity": self.equity,
            "positions": self.positions, "open_intents": self.open_intents,
            "stops": self.stops, "counters": self.counters,
            "leverage_done": sorted(self._leverage_done),
            "leverage_by_sym": self.leverage_by_sym,
            "gap_started_ms": self._gap_started_ms,
            "breaker": {"day": self.breaker.day,
                        "start_equity": self.breaker.start_equity,
                        "tripped": self.breaker.tripped},
        }, ensure_ascii=False)
        self._write_json_atomic(self.state_path, payload)
        # 心跳每步都写(15s 级):看护进程靠它判活,不能等冷档档期。
        try:
            self._write_json_atomic(self.heartbeat_path,
                                    self._heartbeat_payload(
                                        int(time.time() * 1000)))
        except Exception:
            pass

        every = float(self.loop_cfg.get("campaigns_write_seconds", 60))
        now = time.time()
        if not (self._campaigns_dirty
                or now - self._campaigns_saved_at >= every):
            return
        self._write_json_atomic(self.campaigns_path, self._campaigns_payload())
        self._campaigns_dirty = False
        self._campaigns_saved_at = now

    # ------------------------------------------------------------------
    # 战役装配(本地磁盘 K 线缓存 + 增量回补 + 并发拉取 + 按周期裁剪认知重放)
    # ------------------------------------------------------------------

    def _candle_cache_path(self, symbol: str, *, gz: bool = True) -> Path:
        """K 线缓存路径:默认 `.json.gz`(密度高 3~4 倍),`gz=False` 为旧格式。

        缓存是**可再生产物**(交易所随时可补齐),压缩它不损失任何信息:
        1800 根 × 6 字段的 JSON 纯文本 ~100KB,压后 ~25KB。
        """
        gran = str(self.cog_cfg.get("granularity") or "2H").upper()
        suffix = ".json.gz" if gz else ".json"
        return self.candle_cache_dir / f"{symbol}_{gran}{suffix}"

    def _load_cached_bars(self, symbol: str) -> list[tuple]:
        """读缓存:优先 .json.gz,回落旧 .json(便于无损迁移,不必停机改格式)。"""
        for gz in (True, False):
            p = self._candle_cache_path(str(symbol), gz=gz)
            if not p.exists():
                continue
            try:
                if gz:
                    with gzip.open(p, "rt", encoding="utf-8") as fh:
                        raw = json.load(fh)
                else:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                return [(int(r[0]), float(r[1]), float(r[2]),
                         float(r[3]), float(r[4]), float(r[5])) for r in raw]
            except Exception:
                continue
        return []

    def _save_cached_bars(self, symbol: str, bars: list) -> None:
        if not bars:
            return
        try:
            self.candle_cache_dir.mkdir(parents=True, exist_ok=True)
            want = int(self.cog_cfg.get("warmup_trigger_bars", 3600))
            p = self._candle_cache_path(str(symbol))
            payload = json.dumps(bars[-want:], separators=(",", ":"))
            self._write_json_atomic(p, payload)          # 文本原子写
            legacy = self._candle_cache_path(str(symbol), gz=False)
            if legacy.exists():                          # 迁移:旧明文缓存删除
                legacy.unlink(missing_ok=True)
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
        rp_tf = self.rp_by_tf.get(tf_label, self.rp)
        router = MarketRouter(rp_tf)
        pm = new_position_manager(self.params, self.raw_strat)
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
        if symbol not in self.campaigns:
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

        掉线补做:缺口批次未完成前不推进常规 step(下轮重试),避免"没补完
        就先按新价管理"造成两套事实并存。

        心跳口径:`loop_ts_ms` **只在成功走完一步后才前移** —— 卡在哪一步就停在
        哪一步,看护据此区分"活着但没进展"(stalled)与"进程已消失"(missing);
        `step_started_ms` 记录当前步起点,便于定位卡死位置。
        """
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
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 掉线补做(gap recovery):缺失时段内的 canonical 持仓管理补执行
    # ------------------------------------------------------------------

    def recover_gap(self, now_ms: int) -> bool:
        """把崩溃期间**已收盘**的 bar 逐根补放进 canonical 管理链,再结算。

        为什么必须补:重启时 `_load_state` 拉到的 bar 已包含缺口段,若不
        显式切分并重放,`_advance` 会把它们当成"已处理"——移动防线、
        对侧锁利、TP1 减仓全部静默丢失,而价格往往已不再回来。

        语义边界(与 canonical 一致):
        - 补放只走持仓管理(_on_sub_bar / pm.on_bar)与结构推进;
          `_gap_replay=True` 时 **不产生新开仓路由**(陈旧信号不得按现价追单)。
        - 缺口 ≥ gap_recovery.min_gap_seconds 才算"掉线":
          * 缺口内触及固定止损、交易所仍有仓 → 按 stop_policy 结算;
          * 防线已被推紧且现价已越过 → 立即市价离场(不挂必然即刻触发的计划单);
          * TP1 曾触及应减仓 → 按 reduce_policy 结算(默认市价,接受回撤)。
        - 接口异常:保留缺口批次,返回 False,下一轮重试。
        """
        gcfg = dict(self.loop_cfg.get("gap_recovery") or {})
        gap_s = 0.0 if self._gap_started_ms is None else \
            max(0.0, (now_ms - self._gap_started_ms) / 1000.0)
        big = (self.mode == "live" and bool(gcfg.get("enabled", True))
               and gap_s >= float(gcfg.get("min_gap_seconds", 180)))
        if self.mode == "live" and not self._pre_gap_sync():
            return False                     # 交易所事实没读到:不补,不推进
        if big:
            self.tg.send_alert(
                f"检测到掉线缺口 {gap_s / 3600:.2f}h:开始补做持仓管理"
                f"({len(self._gap_bars)} 个品种,"
                f"{sum(len(v) for v in self._gap_bars.values())} 根 2H bar);"
                f"补做期间暂停新开仓")
        self._gap_replay = True
        self._gap_stop_hit = {}
        try:
            for sym in sorted(self._gap_bars):
                cs = self.campaigns.get(sym)
                if cs is None:
                    continue
                bars = self._gap_bars[sym]
                self._advance(cs, now_ms, new_bars=bars)
                for (s2, _tf), sub in sorted(self.extra_campaigns.items()):
                    if s2 == sym:
                        self._advance(sub, now_ms, new_bars=bars,
                                      run_paper_fills=False)
        except BitgetError as e:
            self._api_note(e)
            self.tg.send_alert(f"掉线补做中断({type(e).__name__});保留缺口批次,"
                               f"下一轮重试")
            return False
        finally:
            self._gap_replay = False
        self._gap_bars.clear()
        self._touch_campaigns()             # 冷档里的待补批次要同步清掉
        # 追平后必须清锚:锚的语义是"尚未补完的缺口起点",留着会让下一次重启
        # 把几小时前的老缺口当成新缺口(实测:补完后仍报 19h 缺口,真缺口反而
        # 被这个假数字淹没)。清空后 _load_state 回落 saved_at_utc = 真实心跳。
        self._gap_started_ms = None
        if big:
            self._gap_settle(now_ms, gap_s)
        return True

    def _pre_gap_sync(self) -> bool:
        """补做前的一次性权威同步:数量/标记价以交易所为准,已平仓即清理。

        与 `_reconcile` 的"连续两轮确认"不同:这里刚读过交易所,单次即可
        判定;否则对已止损离场的品种补放管理会按本地幽灵仓位下单。
        """
        try:
            ex_pos = self.client.positions()
        except BitgetError as e:
            self._api_note(e)
            return False
        ex_by = {p["symbol"]: p for p in ex_pos}
        for sym, pos in list(self.positions.items()):
            p = ex_by.get(sym)
            if p is None or int(p["direction"]) != int(pos["direction"]):
                self._journal("gap_closed_elsewhere",
                              {"symbol": sym, "qty": pos.get("qty")})
                self._reconcile_closed(sym, pos)
                continue
            if abs(float(p["qty"]) - float(pos["qty"])) > max(
                    1e-9, float(p["qty"]) * 1e-6):
                self._journal("gap_qty_fix", {"symbol": sym,
                                              "ours": float(pos["qty"]),
                                              "exchange": float(p["qty"])})
                pos["qty"] = float(p["qty"])
            pos["mark"] = float(p["mark_price"])
            cs = self._get_cs_for(sym, pos.get("tf_label"))
            if cs is not None and sym in cs.pm.positions:
                try:
                    cs.pm.positions[sym].qty = float(p["qty"])
                except Exception:
                    pass
        return True

    def _gap_settle(self, now_ms: int, gap_s: float) -> None:
        """缺口补放后的结算:把"本应已发生但错过"的止损补成市价动作。

        两条判据都是**事实**(bar 极值 / 现价 vs 防线),不含主观判断:
        1. 缺口内 bar 极值已触及固定止损 → canonical 此刻本应离场;交易所
           仍有仓说明计划单未触发或当时不在场,按 stop_policy 结算。
        2. 补放把防线推紧后,现价已在防线之外 → 挂计划单必然即刻触发,
           直接市价离场(避免"挂单 → 立刻以更差价成交"的二次滑点)。

        TP1 减仓不在此重复结算:补放时 `_on_sub_bar → _exec_mgmt` 已按缺口
        bar 的收盘价把该减的仓位减掉(reduce_policy=market 时走市价)。
        """
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
                    f"{stop_policy} → 保留仓位并按最新防线重挂止损")
                continue
            self._gap_market_exit(sym, pos, now_ms,
                                  f"{marker}|STOP_MISSED|bar={hit['ts']}"
                                  f"|stop={hit['stop']:.8g}")
            self.tg.send_alert(
                f"{marker} {sym} 缺口内已触及止损 {hit['stop']:.8g}"
                f",交易所仍有仓(计划单未触发)→ 市价离场(现价 {mark:.8g})")
            acted += 1
        for sym, pos in sorted(self.positions.items()):
            if sym in self._gap_stop_hit:
                continue
            mark = float(pos.get("mark") or 0.0)
            if mark <= 0.0:
                continue
            stop = float(pos["invalid"])
            breached = (mark <= stop) if int(pos["direction"]) > 0 \
                else (mark >= stop)
            if not breached:
                continue
            self._gap_market_exit(sym, pos, now_ms,
                                  f"{marker}|STOP_ALREADY_BREACHED"
                                  f"|stop={stop:.8g}")
            self.tg.send_alert(
                f"{marker} {sym} 补放后防线 {stop:.8g} 已被现价 {mark:.8g} "
                f"越过(移动防线/锁利错过)→ 市价离场,不挂即刻触发的计划单")
            acted += 1
        self.counters["gap_recoveries"] = \
            self.counters.get("gap_recoveries", 0) + 1
        self._journal("gap_recovery", {
            "gap_hours": round(gap_s / 3600, 3),
            "stop_hits": sorted(self._gap_stop_hit),
            "actions": acted,
            "stop_policy": stop_policy,
            "reduce_policy": str(gcfg.get("reduce_policy", "market")),
        })
        if acted == 0:
            self.tg.send_alert(
                f"{marker} 补做完成:缺口内的防线推进/减仓已按 canonical 重放,"
                f"无止损越界需追平")

    def _gap_market_exit(self, sym: str, pos: dict, now_ms: int,
                         reason: str) -> None:
        """撤掉入场/止损挂单后按现价市价离场(强制,不受速率限制约束)。"""
        qty = floor_step(float(pos["qty"]), self._spec(sym).size_step,
                         self._spec(sym).size_place)
        self._cancel_entry(sym, "gap_exit")
        self._cancel_stop(sym, "gap_exit")
        self._arm_close_intent(sym, pos, qty, "market", None, reason,
                               int(now_ms), role="exit", force=True)
        self.counters["gap_stop_exits"] = \
            self.counters.get("gap_stop_exits", 0) + 1

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
                    self._touch_campaigns()      # 新纳入宇宙的品种要进冷档
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
                    self._touch_campaigns()      # 认知帧重装配:冷档同步
                return
            if self.mode == "paper" and run_paper_fills:
                self._paper_fill_pass(cs.symbol, bar)
            cs.trig_bars.append(bar)
            self._touch_campaigns()          # 新 bar 必须尽快进冷档(缺口切分锚)
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

    def _sub_bar_close_out(self, cs: CampaignState, pos_pm,
                           pos_dict: dict, ts: int, ref_px: float, *,
                           why: str, route: str) -> None:
        """子周期触价离场的统一出口(paper 记账 / live 对账)。

        固定止损与"锁利回吐预算耗尽"是同一个动作的两条触发路径 —— 都必须在
        子周期立刻离场,且都必须先把本地止损记账撤掉(否则下一拍 `_manage_stops`
        会把已平的仓位重新挂单,形成幽灵保护单)。
        """
        sym = cs.symbol
        if self.mode == "paper":
            self._cancel_entry(sym, why)
            self._cancel_stop(sym, why)
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
                "route": route,
                "status": "armed",
            }
            self._paper_close_fill(sym, stop_it, float(ref_px), ts)
        elif self._gap_replay:
            # 补放缺口 bar:本进程当时不在场,计划单可能未触发/未挂出。
            # 先记账(不在此处平仓,避免与补放中的后续 bar 逻辑交错),
            # 补放结束后由 _gap_settle 按 stop_policy 一次性结算。
            self._gap_stop_hit[sym] = {"ts": ts, "stop": float(ref_px),
                                       "bar_close": float(ref_px)}
        else:
            # live:交易所已有计划单,触价即刻触发对账以消除状态延迟
            self._reconcile()

    def _on_sub_bar(self, cs: CampaignState, bar) -> None:
        """持仓期间子周期(2H)触价同步与结算:
        1. 固定 STOP(pos_pm.invalid):与回测 broker.execute_protective_stop 同口径,
           子周期极值触及(low<=stop / high>=stop)时:
           - paper 模式立即按止损价(或缺口开盘价)平仓结算并清理 pm/positions;
           - live 模式立即触发一次 _reconcile() 同步交易所保护单成交状态。
        2. 固定 TP1(pos_pm.target_1):
           - 更新 pos_pm._best 实时 MFE(即锁利的峰值锚);
           - 当启用对侧锁利(reject_opp_lock_frac != 0)时,用与回放 on_bar 同一个
             裁决函数 `position.opp_lock_step` 推演:未 arming 保持结构防线 /
             保护侧推进(可多次棘轮)/ 回吐超预算则立刻市价离场。子周期只是
             **更早看到同一事实**,不是另一套语义 —— 2h 与 12h 采样得到同一
             防线水平(`idempotent`,不随裁决频率漂移)。
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
            self._sub_bar_close_out(
                cs, pos_pm, pos_dict, ts, ref_px,
                why="sub_bar_protective_stop",
                route=f"STOP|sub_bar trigger={stop:.8g}")
            return

        # ② 固定 TP1(target_1)盘中触价 ⇒ 对侧锁利 / 传统减仓
        #    唯一裁决入口 `position.opp_lock_step`(与回放 `PositionManager.on_bar`
        #    同一函数)。子周期只负责"更早看到同一事实",不引入第二套语义:
        #      未 arming ⇒ 保持结构防线;保护侧 ⇒ 推进(可多次棘轮);
        #      回吐超预算 ⇒ 立刻市价离场。三态穷尽,没有"拒单原地等"的裸奔态。
        if pos_pm.target_1 is not None and not pos_pm.pending_reduce:
            target = float(pos_pm.target_1)
            frac_cfg = float(getattr(cs.pm, "reject_opp_lock_frac", 0.0) or 0.0)
            old_inv = float(pos_pm.invalid)
            # 换单参考价:本地收盘 c 与已知 mark 中**更保守**的一侧(空头取较高价、
            # 多头取较低价),避免换单瞬间现价已越过止损/锁利价。
            ref_px = c
            _mark = float(pos_dict.get("mark") or 0.0)
            if _mark > 0.0:
                ref_px = max(c, _mark) if pos_pm.direction < 0 else min(c, _mark)
            if frac_cfg == 0.0:
                # 传统 §8.2 target_1 触及减仓 50%(仅一次;仅未启用锁利时)
                hit = (l <= target) if pos_pm.direction < 0 else (h >= target)
                if hit and not pos_pm.reduced:
                    extreme = l if pos_pm.direction < 0 else h
                    self._exec_mgmt(cs, cs.pm._reduce(pos_pm, ts, c, extreme), bar)
            else:
                ld = opp_lock_step(
                    direction=pos_pm.direction, entry=pos_pm.entry, target=target,
                    frac=frac_cfg, peak=pos_pm._best, high=h, low=l, close=c,
                    ref_px=ref_px, armed=pos_pm.armed)
                if ld.action == "off":
                    if not pos_pm.lock_off_logged:
                        pos_pm.lock_off_logged = True
                        pos_pm.log.append(
                            f"v21|sub_bar_opp_lock_off|{ld.reason}|"
                            f"entry={pos_pm.entry:.8g} target={target:.8g} "
                            f"frac={frac_cfg:+.2f} ⇒ 不锁利、不减仓,"
                            f"保持结构防线 invalid={old_inv:.8g}")
                elif ld.action == "exit":
                    # 回吐预算已耗尽:此时再挂止损就是挂一张"永不触发"的单,
                    # 直接市价离场 —— 把"锁利"兑现成事实,而不是承诺。
                    pos_pm.log.append(
                        f"v21|sub_bar_lock_giveback_exhausted|"
                        f"峰值={pos_pm._best:.8g} 回吐={ld.drawdown:.8g} "
                        f"≥ 预算={ld.budget:.8g} ref={ref_px:.8g} ⇒ 市价离场")
                    self._journal("lock_exhausted",
                                  {"symbol": sym, "peak": pos_pm._best,
                                   "giveback": ld.drawdown, "budget": ld.budget,
                                   "ref_px": ref_px, "invalid": old_inv,
                                   "direction": int(pos_pm.direction)})
                    self._sub_bar_close_out(
                        cs, pos_pm, pos_dict, ts, ref_px,
                        why="sub_bar_lock_exhausted",
                        route=(f"LOCK_EXIT|giveback={ld.drawdown:.8g} "
                               f"budget={ld.budget:.8g} ref={ref_px:.8g}"))
                    return
                else:
                    if ld.armed and not pos_pm.armed:
                        pos_pm.armed = True
                        pos_pm.log.append(
                            f"v21|sub_bar_opp_lock_armed|"
                            f"{'收盘突破' if frac_cfg < 0 else '触及'}对侧缘 "
                            f"target={target:.8g} "
                            f"预算={(1.0 - abs(frac_cfg)):.0%}·span "
                            f"⇒ 此后按峰值棘轮(可多次)")
                    if ld.action == "tighten":
                        stop_px = float(ld.stop_px)
                        pos_pm.invalid = (min(pos_pm.invalid, stop_px)
                                          if pos_pm.direction < 0
                                          else max(pos_pm.invalid, stop_px))
                        if abs(pos_pm.invalid - old_inv) > 1e-12:
                            pos_pm.log.append(
                                f"v21|sub_bar_opp_lock|峰值={pos_pm._best:.8g} "
                                f"预算={ld.budget:.8g} 回吐={ld.drawdown:.8g} "
                                f"ref={ref_px:.8g} "
                                f"invalid {old_inv:.8g}->{pos_pm.invalid:.8g}")
            if abs(float(pos_pm.invalid) - old_inv) > 1e-12:
                pos_dict["invalid"] = float(pos_pm.invalid)
                self._touch_campaigns()      # 防线推进:冷档必须留住
                last_log = pos_pm.log[-1] if pos_pm.log else "子周期防线锁利"
                self.tg.send_fill(
                    sym, f"止损推进({self.mode})",
                    float(pos_dict["qty"]),
                    c,
                    note=f"{old_inv:.6g} -> {pos_pm.invalid:.6g} ({last_log[:48]})",
                    tf_label=cs.tf_label,
                    direction=pos_pm.direction,
                    entry_price=float(pos_dict["entry"]),
                    price_label="参考价(子周期收盘)",
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
                self._touch_campaigns()      # 防线推进:冷档必须留住
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
                    entry_price=(float(pos_dict["entry"]) if pos_dict else None),
                    price_label="参考价(认知帧收盘)",
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
        """canonical 决策:MarketRouter → 意图(v21 唯一链)。"""
        sym = cs.symbol
        if mic:
            bars, bar = cs.trig_bars, cs.trig_bars[-1]
        else:
            bars, bar = cs.cog_bars, cs.cog_bars[-1]
        decisions = cs.router.on_bar(
            sym, bar, len(bars) - 1, view, atr, events,
            position_open=sym in self.positions,
            entry_resting=self._entry_resting(sym))
        t = int(bar[0])
        for d in decisions:
            if d.kind == "cancel_entry":
                self._cancel_entry(sym, str(d.meta.get("reason", "")))
            elif d.kind in ("market", "limit"):
                if self._gap_replay:
                    # 掉线补做:补放出的历史信号不得按现价追单。canonical 里它们
                    # 当时会立刻成交;现在补做只会在价格已经走开之后追高/追低,
                    # 属于凭空新增风险(也不是"恢复"任何既有事实)。
                    self.counters["gap_entry_skips"] = \
                        self.counters.get("gap_entry_skips", 0) + 1
                    self._journal("gap_entry_skip", {
                        "symbol": sym, "ts": int(bar[0]),
                        "event": getattr(d.hyp, "event_id", None)})
                    continue
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
        qty = floor_step(qty, spec.size_step, spec.size_place)
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
        self._touch_campaigns()              # 新仓:冷档里的 pm 事实必须同步
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
                floor_step(float(pos["qty"]), spec.size_step, spec.size_place),
                "market", None, intent.reason, t, role="exit",
                force=self._gap_replay)
        elif kind == "REDUCE":
            qty = floor_step(float(intent.qty_frac or 0.0) * pos["qty"],
                             self._spec(sym).size_step,
                             self._spec(sym).size_place)
            if qty <= 0.0:
                return
            target = pos_pm.target_1 if pos_pm is not None else None
            partial_market = str(intent.reason).startswith(
                "v16|target_partial|")           # S2 段落袋市价(canonical)
            # 掉线补做:缺口 bar 的限价单已无意义(价格早走开/可能一直不成交),
            # 按 reduce_policy 用市价把该减的仓位减掉,接受回撤换确定性。
            gap_market = self._gap_replay and str(
                (self.loop_cfg.get("gap_recovery") or {})
                .get("reduce_policy", "market")).lower() == "market"
            if target is not None and not partial_market and not gap_market:
                self._arm_close_intent(sym, pos, qty, "limit", float(target),
                                       intent.reason, t, role="reduce",
                                       force=gap_market)
            else:
                self._arm_close_intent(sym, pos, qty, "market", None,
                                       intent.reason, t, role="reduce",
                                       force=gap_market)

    def _arm_close_intent(self, sym: str, pos: dict, qty: float, kind: str,
                          limit_price: Optional[float], reason: str, t: int,
                          *, role: str, force: bool = False) -> None:
        # hedge_mode:平仓 side 与开仓同侧(表持仓方向),靠 tradeSide=close 区分
        side = self.close_side_of(pos["direction"])
        oid = client_oid_of(self.OID_PREFIX, sym, str(reason)[:16], side, t,
                            role=role)[:36]
        if self.open_intents.get(oid, {}).get("status") in (
                "armed", "filled", "error"):
            return
        # force=True 走风控离场(止损追平/掉线补做):不受每小时下单数限制,
        # 否则限频会把"该跑的止损"静默吞掉(rate_skips 只许影响新增风险)。
        if not force and not self.rate.allow():
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
            entry_price=float(pos["entry"]),      # 图 ENTRY 用真实建仓均价
            stop_price=pos.get("invalid"),
            target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            bars=chart_bars)
        self._journal("close_fill", {"oid": it["oid"], "symbol": it["symbol"],
                                     "qty": qty, "role": it["role"]})
        if it["role"] == "exit" or pos["qty"] <= 1e-9:
            self.positions.pop(it["symbol"], None)
        self._touch_campaigns()              # 平仓/减仓:pm 数量已变

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

    def _pending_stop_ids(self, now_ms: int) -> Optional[set]:
        """交易所在挂 TPSL orderId 集合(TTL 缓存;取不到返回 None ≡ 不判定)。

        用途:本地 `stops` 只证明"我们挂过",不证明"现在还挂着"。TPSL 被交易所
        撤掉/触发/风控清理后本地无从得知,表现就是**永久无保护**(2026-10-22
        掉线期间正是这种盲区)。取不到就返回上次结果,避免把"接口抖动"当成
        "保护丢了"而反复撤单重挂。
        """
        ttl_ms = int(float((self.loop_cfg.get("gap_recovery") or {})
                           .get("stop_check_seconds", 120)) * 1000)
        if self._stops_live_ids is not None \
                and now_ms - self._stops_checked_at < ttl_ms:
            return self._stops_live_ids
        try:
            raw = self.client.tpsl_orders_pending()
        except BitgetError as e:
            self._api_note(e)
            self._stops_checked_at = now_ms
            return self._stops_live_ids
        rows = raw.get("entrustedList") if isinstance(raw, dict) else raw
        rows = [r for r in (rows or []) if r.get("orderId")]
        ids = {str(r.get("orderId")) for r in rows}
        self._stops_live_ids, self._stops_live_rows = ids, rows
        self._stops_checked_at = now_ms
        return ids

    def _dedupe_stops(self, now_ms: int) -> None:
        """每个持仓只留一张保护单:清理重复/遗留 TPSL。

        为什么需要:掉线补做、本地记录丢失后重挂、旧进程残留,都可能在同一
        symbol 上留下多张 close 单(旧式计划单与原生 TPSL 混挂也一样)。它们
        各自按触发价成交,第一张成交后其余成为无仓可平的废单;而 `stops` 只
        记得其中一张 ⇒ 对账看不见另一张,撤旧换新时也会漏撤。
        只处理自有 clientOid 前缀的挂单,异己单一律不碰。
        """
        rows = self._stops_live_rows
        if not rows:
            return
        by_sym: dict[str, list] = {}
        for r in rows:
            if not str(r.get("clientOid") or "").startswith(self.OID_PREFIX):
                continue
            by_sym.setdefault(str(r.get("symbol")), []).append(r)
        for sym, items in by_sym.items():
            cur = self.stops.get(sym)
            keep = str(cur.get("order_id") or "") if cur else ""
            if not keep or sym not in self.positions:
                continue                  # 无本地记录/无持仓:留给 _manage_stops 处置
            for r in items:
                oid = str(r.get("orderId"))
                if oid == keep:
                    continue
                try:
                    self.client.cancel_tpsl_order(sym, oid)
                except BitgetError:
                    continue
                self.counters["stop_dupes"] = \
                    self.counters.get("stop_dupes", 0) + 1
                self._journal("stop_dupe_cancel",
                              {"symbol": sym, "order_id": oid, "keep": keep,
                               "trigger": r.get("triggerPrice")})
                self.tg.send_alert(
                    f"{sym} 发现重复保护止损单({oid},触发 "
                    f"{r.get('triggerPrice')})已撤,保留 {keep}")
                self._stops_live_ids, self._stops_live_rows = None, None
                self._stops_checked_at = 0.0

    def _manage_stops(self, now_ms: int) -> None:
        """保护止损单(交易所原生 TPSL `pos_loss` @ 当前 invalid)。

        只保护不主出:结构出场一律经 §8/canonical 路径。另外每轮(带 TTL)核对
        TPSL 是否仍在交易所;本地有记录而交易所没有 = 保护失效,必须重挂,
        若价格已越过防线则直接市价离场(挂单只会立刻以更差价成交)。

        `pos_loss` 是**仓位级**:交易所按触发时的整仓数量市价平仓,报文不带
        size。因此"数量变了"不构成换单理由 —— 部分减仓后旧 TPSL 自动覆盖剩余
        仓位,若按数量撤旧换新,每轮减仓都会多开一个失保窗口(撤成功、挂失败)。
        """
        live_ids = self._pending_stop_ids(now_ms) if self.mode == "live" else None
        for sym, pos in list(self.positions.items()):
            want = self.fit_price(sym, float(pos["invalid"]), pos["direction"])
            cur = self.stops.get(sym)
            # 本地退出量仍按合约精度取整:精度不符会被交易所直接拒单
            # (实测 40808 size checkScale);TPSL 报文本身不带 size。
            spec = self._spec(sym)
            stop_qty = floor_step(float(pos["qty"]), spec.size_step,
                                  spec.size_place)
            missing = bool(cur and cur.get("order_id") and live_ids is not None
                           and str(cur["order_id"]) not in live_ids)
            if missing:
                self.counters["stop_missing"] = \
                    self.counters.get("stop_missing", 0) + 1
                self._journal("stop_missing", {"symbol": sym,
                                               "order_id": cur.get("order_id"),
                                               "trigger": cur.get("trigger")})
                mark = float(pos.get("mark") or 0.0)
                breached = mark > 0.0 and (
                    (int(pos["direction"]) > 0 and mark <= want)
                    or (int(pos["direction"]) < 0 and mark >= want))
                if breached:
                    self._cancel_entry(sym, "stop_missing_breached")
                    self.stops.pop(sym, None)
                    self._arm_close_intent(
                        sym, pos, stop_qty, "market", None,
                        f"STOP_MISSING_BREACHED|{want:.8g}", int(now_ms),
                        role="exit", force=True)
                    self.tg.send_alert(
                        f"{sym} 交易所止损单已不在挂(被撤/被触发/被清理)"
                        f",且现价 {mark:.8g} 已越过防线 {want:.8g} → 市价离场")
                    continue
                cur = None      # 无保护单:落到下面重挂
            if cur is not None and abs(float(cur["trigger"]) - want) < 1e-12:
                continue
            try:
                if cur is not None and cur.get("order_id"):
                    # 撤旧单失败**不得**阻断新单:旧单可能已成交或已被交易所
                    # 撤销(cancel 返回 order does not exist)。修前该异常跳到
                    # 外层 except ⇒ 不挂新单,而下一轮会因同一原因再失败 ——
                    # 仓位在无人察觉的情况下持续失保。
                    try:
                        self.client.cancel_tpsl_order(sym, cur["order_id"])
                    except BitgetError as e:
                        self._journal("stop_cancel_skip",
                                      {"symbol": sym,
                                       "order_id": cur["order_id"],
                                       "why": str(e)[:160]})
                resp = self.client.place_stop_loss(
                    sym, trigger_price=want,
                    hold_side=self.stop_hold_side(pos["direction"]),
                    client_oid=client_oid_of(
                        self.OID_PREFIX, sym, str(pos["event_id"]),
                        "stop", int(now_ms), role="stop"))
                oid = str((resp or {}).get("orderId") or "")
                if not oid:
                    # 无单号的"成功"等于没挂:必须当失败处理,否则本地记录
                    # 声称有保护、实际裸奔到下轮才发现。
                    raise BitgetError("place-tpsl-order 未返回 orderId")
                self.stops[sym] = {"trigger": want, "qty": stop_qty,
                                   "order_id": oid}
                # 刚换过单:缓存里的在挂集合已过期,置空强制下轮重读,
                # 否则新 order_id 不在旧快照里会被判"保护丢失"→ 反复撤挂。
                self._stops_live_ids, self._stops_live_rows = None, None
                self._stops_checked_at = 0.0
                self._journal("stop", {"symbol": sym, "trigger": want,
                                       "qty": stop_qty, "order_id": oid})
            except BitgetError as e:
                self.counters["stop_errors"] = \
                    self.counters.get("stop_errors", 0) + 1
                self._journal("stop_error", {"symbol": sym, "trigger": want,
                                             "qty": stop_qty,
                                             "why": str(e)[:160]})
                self._alert_throttled(
                    f"stop:{sym}",
                    f"{sym} 保护止损挂单失败:{str(e)[:120]}"
                    f"(当前无触发保护,人工介入;同类告警 30 分钟内不重复)")
        if live_ids is not None:
            # 快照有效才去重(刚换过单时快照已作废,留待下轮 TTL 重读)
            self._dedupe_stops(now_ms)

    def _cancel_stop(self, sym: str, why: str) -> None:
        cur = self.stops.pop(sym, None)
        if cur is not None and cur.get("order_id") and self.mode == "live":
            self._stops_live_ids, self._stops_live_rows = None, None
            self._stops_checked_at = 0.0
            try:
                self.client.cancel_tpsl_order(sym, cur["order_id"])
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
        self._touch_campaigns()              # 交易所已无此仓:冷档 pm 事实要清
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
            entry_price=float(pos["entry"]),      # 图 ENTRY 用真实建仓均价
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
            entry_price=float(pos["entry"]),      # 图 ENTRY 用真实建仓均价
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
        self._touch_campaigns()              # paper 平仓/减仓同样进冷档


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
    ap.add_argument("--heartbeat-path", default=None,
                    help="心跳落盘路径(默认:config loop.heartbeat_path 锚定仓库根;"
                         "测试/沙盒用它指到临时目录,避免覆盖生产心跳)")
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
                        heartbeat_path=(Path(args.heartbeat_path)
                                        if args.heartbeat_path else None),
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
    print(f"[live_runner] gap_started={runner._gap_started_ms} "
          f"gap_symbols={sorted(runner._gap_bars)} "
          f"gap_bars={sum(len(v) for v in runner._gap_bars.values())}",
          flush=True)
    if runner._gap_bars:
        tg.send_alert(
            f"live_runner 起动:检测到上次断点后存在缺口("
            f"{len(runner._gap_bars)} 品种/"
            f"{sum(len(v) for v in runner._gap_bars.values())} 根 2H bar),"
            f"将先补做持仓管理再恢复常规循环")
    # 心跳看护线程:与主循环同进程但独立调度(主循环卡死时它仍是活的)。
    # 判活只读 heartbeat.json 的时间事实,不看进程树/端口/任务计划 —— 平台无关。
    # paper 模式关掉自我熔断(0):没有真实敞口,退出只会白白引入重启噪声。
    hb_cfg = dict(cfg.get("heartbeat") or {})
    guard = None
    if hb_cfg.get("enabled", True):
        guard = HeartbeatGuard(
            runner, path=runner.heartbeat_path,
            poll_s=float(hb_cfg.get("write_seconds", 15)),
            stall_s=float(hb_cfg.get("stall_seconds", 180)),
            self_fence_s=(float(hb_cfg.get("self_fence_seconds", 900))
                          if args.mode == "live" else 0.0),
            secrets=secrets,
            protect_on_stall=bool(hb_cfg.get("protect_on_stall", True)))
        guard.start()                      # daemon:进程退出即随主进程消失
        print(f"[live_runner] 心跳看护起动:每 {guard.poll_s:.0f}s 落盘 / "
              f"停摆阈值 {guard.stall_s:.0f}s / 自我熔断 "
              f"{guard.self_fence_s:.0f}s(0=关) / {runner.heartbeat_path}",
              flush=True)
    # 连续失败上限:超过即退出交给看门狗(CLI 与计划任务都能拉起)。
    # 修前缺陷:任何非 BitgetError 异常都会让进程直接消失且没有任何告警
    # (2026-10-22 17:12 起的静默停摆)。
    max_consec = int(cfg["loop"].get("max_consecutive_errors", 20))
    consec = 0
    while True:
        now_ms = int(time.time() * 1000)
        try:
            runner.step(now_ms)
            consec = 0
        except BitgetError as e:
            consec += 1
            print(f"[live_runner] api error({consec}): {e}", flush=True)
        except SecretsError as e:
            print(f"[live_runner] secrets error: {e}", flush=True)
            raise
        except Exception as e:
            consec += 1
            traceback.print_exc()
            try:
                runner._journal("crash", {
                    "err": f"{type(e).__name__}: {e}"[:300],
                    "consec": consec})
                if consec == 1:
                    tg.send_alert(
                        f"live_runner 未预期异常({type(e).__name__}):"
                        f"{str(e)[:160]}(进程保活;连续 {max_consec} 次即退出,"
                        f"由看门狗重启)")
            except Exception:
                pass
            if consec >= max_consec:
                raise
            time.sleep(poll)
            continue
        time.sleep(poll)


if __name__ == "__main__":
    main()
