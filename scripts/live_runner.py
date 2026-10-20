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
② S4 E3 前缘限价(entry,role=entry,kind=limit)→ GTC,N_fill=12 墙钟小时
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
from dataclasses import dataclass, field
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
from market_operator.structure import Params                    # noqa: E402
from market_operator.tg import TelegramNotifier                 # noqa: E402
from market_operator.tiered import TieredParams                 # noqa: E402
from market_operator.trigger import (                           # noqa: E402
    MicTracker, resolve_trigger_tf)

CONFIG_PATH = ROOT / "config" / "live.json"
CONFIG_V20 = ROOT / "config" / "v20.json"

MS_1H = 3_600_000
N_FILL_H = 12                                    # canonical N_fill(墙钟小时)


# ---------------------------------------------------------------------------
# canonical 参数组(arm-full;config/v20.json 照链)
# ---------------------------------------------------------------------------


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
    """arm-full 持仓侧接线(同 run_campaigns:S1/S2 开,early_exit/R4 关)。"""
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


def aggregate_12h(bars2h: list) -> list:
    """2H 序列 → 12h 帧:网格对齐;末桶不齐(未完整)丢弃,内部缺口 raise
    (不可静默);首桶不齐如实保留(canonical 首/末不完整桶口径)。"""
    MS, STEP = 7_200_000, 43_200_000
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
            raise RuntimeError(f"12h 桶 {label} 内部缺口({len(got)} 根)")
        out.append(aggregate_bucket(got, label))
    return out


def replay_cognition(params: Params, symbol: str, cog_bars: list,
                     trig_bars: list, *, obs_tf_seconds: int,
                     trigger_tf_seconds: int):
    """warmup 回放 → (detector, tracker);与 prepare_mic_cognition 同一因果序
    (逐认知 bar:先 trigger 段推进,再 bar 收盘 update + sync),实例留存供
    增量接续;bar 事件流 == prepare_cognition 由测试钉死。
    """
    obs_ms = obs_tf_seconds * 1000
    trig_ms = trigger_tf_seconds * 1000
    scale = obs_ms // trig_ms
    detector = EventDetector(params, enable_e7=True)
    tracker = MicTracker(params, scale=scale,
                         trigger_tf_seconds=trigger_tf_seconds,
                         obs_tf_seconds=obs_tf_seconds, enable_e7=True)
    labels = {b[0]: i for i, b in enumerate(cog_bars)}
    spans: dict = {}
    for k, b in enumerate(trig_bars):
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
        while k <= hi:
            tb = trig_bars[k]
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
    tracker: MicTracker
    router: V20Router
    pm: PositionManager
    cog_bars: list = field(default_factory=list)
    trig_bars: list = field(default_factory=list)
    active: bool = True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class LiveRunner:
    """主循环接线;client/tg 注入(paper 冒烟零真单;测试注入假件)。"""

    OID_PREFIX = "mo20"

    def __init__(self, mode: str, cfg: dict, client, tg: TelegramNotifier, *,
                 equity0: Optional[float] = None,
                 contracts: Optional[dict] = None,
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
        self.raw20, self.params, self.rp = load_v20_params()
        self.contracts = contracts or {}
        self.equity: Optional[float] = \
            float(equity0) if equity0 is not None else None
        self.state_path = Path(state_path) if state_path else \
            ROOT / self.loop_cfg["state_path"]
        self.journal_dir = Path(journal_dir) if journal_dir else \
            ROOT / self.loop_cfg["journal_dir"]
        self.breaker = DailyLossBreaker(self.risk["max_daily_loss_frac"])
        self.rate = RateLimiter(self.risk["orders_per_hour"])
        self.universe_enabled = bool(universe_enabled)
        self.campaigns: dict[str, CampaignState] = {}
        self.symbols_pending: list = sorted(symbols)
        self.positions: dict[str, dict] = {}
        self.open_intents: dict[str, dict] = {}
        self.stops: dict[str, dict] = {}
        self.counters = {"signals": 0, "orders_placed": 0, "cap_skips": 0,
                         "min_notional_skips": 0, "rate_skips": 0,
                         "breaker_blocks": 0, "fills": 0, "mgmt_execs": 0,
                         "expired_intents": 0, "reconcile_alerts": 0,
                         "reduce_fills": 0, "exits": 0}
        self._universe_at = 0.0
        self._reconcile_at = 0.0
        self._daily_date: Optional[str] = None
        self._api_error_streak = 0
        self._load_state()

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
        self.counters.update(st.get("counters", {}))
        b = st.get("breaker", {})
        self.breaker.day = b.get("day")
        self.breaker.start_equity = b.get("start_equity")
        self.breaker.tripped = bool(b.get("tripped"))
        if st.get("equity") and self.equity is None:
            self.equity = float(st["equity"])
        for sym, saved in st.get("campaigns", {}).items():
            try:
                cs = self._build_campaign(sym)
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
                cs.pm.positions[s] = pos
            self.campaigns[sym] = cs
            if sym in self.symbols_pending:
                self.symbols_pending.remove(sym)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        camps = {}
        for sym, cs in self.campaigns.items():
            pm_pos = {}
            for s, p in cs.pm.positions.items():
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
            "breaker": {"day": self.breaker.day,
                        "start_equity": self.breaker.start_equity,
                        "tripped": self.breaker.tripped},
            "campaigns": camps}, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # 战役装配(warmup 整段回放;回放段绝不产生任何意图)
    # ------------------------------------------------------------------

    def _fetch_trigger_bars(self, symbol: str, want: int) -> list:
        """2H 帧回补:倒序分页(endTime 排他锚)合并去重;截 `want` 窗口。"""
        have: dict = {}
        end_ms, guard = None, 0
        while len(have) < want and guard < max(1, (want // 200) + 2):
            page = self.client.history_candles(
                symbol, self.cog_cfg["granularity"], end_ms=end_ms, limit=200)
            fresh = [b for b in page if b[0] not in have]
            if not fresh:
                break
            for b in fresh:
                have[b[0]] = b
            end_ms = fresh[0][0]
            guard += 1
        bars = [have[t] for t in sorted(have)]
        return bars[-want:]

    def _build_campaign(self, symbol: str) -> Optional[CampaignState]:
        obs_s = int(self.cog_cfg["obs_tf_seconds"])
        trig_s = int(self.cog_cfg["trigger_tf_seconds"])
        ttf, decl = resolve_trigger_tf(obs_s)
        if ttf != trig_s or decl is not None:      # 12h→7200 精确命中硬断言
            raise RuntimeError(f"trigger 解析漂移: {decl}")
        want = int(self.cog_cfg["warmup_trigger_bars"])
        bars2h = self._fetch_trigger_bars(symbol, want)
        if len(bars2h) < want * 0.9:
            self.tg.send_alert(f"{symbol} 2H 历史不足({len(bars2h)}/{want}"
                               f" 根),暂不交易;如实不计入执行名单")
            return None
        for a, b in zip(bars2h, bars2h[1:]):       # 网格完整性(不可静默)
            if b[0] - a[0] != trig_s * 1000:
                self.tg.send_alert(f"{symbol} 2H 序列缺口 {a[0]}→{b[0]},"
                                   f"暂不交易")
                return None
        cog_bars = aggregate_12h(bars2h)
        if len(cog_bars) < int(self.cog_cfg["warmup_cog_bars"]) * 0.5:
            self.tg.send_alert(f"{symbol} 12h 认知帧过短({len(cog_bars)}"
                               f" 根),暂不交易")
            return None
        detector, tracker = replay_cognition(
            self.params, symbol, cog_bars, bars2h,
            obs_tf_seconds=obs_s, trigger_tf_seconds=trig_s)
        return CampaignState(symbol=symbol, detector=detector, tracker=tracker,
                             router=V20Router(self.rp),
                             pm=new_position_manager(self.params, self.raw20),
                             cog_bars=list(cog_bars),
                             trig_bars=list(bars2h))

    # ------------------------------------------------------------------
    # 主循环一步
    # ------------------------------------------------------------------

    def step(self, now_ms: int) -> None:
        if self.mode == "live":
            bal = self.client.balance()
            self.equity = float(bal["equity"])
        if self.equity is None:
            raise RuntimeError("equity 未定(live 取交易所权益;paper 需 "
                               "equity0)")
        self.breaker.update(now_ms, self.equity)
        self._universe_tick()
        for sym in list(self.symbols_pending):
            try:
                cs = self._build_campaign(sym)
            except (BitgetError, RuntimeError) as e:
                self._api_note(e)
                self.tg.send_alert(f"{sym} 认知帧装配失败:{type(e).__name__} "
                                   f"{str(e)[:120]}(暂不交易)")
                self.symbols_pending.remove(sym)
                continue
            if cs is not None:
                self.campaigns[sym] = cs
            self.symbols_pending.remove(sym)
        if self.mode == "live":
            self._sync_live_fills(now_ms)
        for sym, cs in sorted(self.campaigns.items()):
            try:
                self._advance(cs, now_ms)
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
        self._save_state()

    def _api_note(self, e: Exception) -> None:
        self._api_error_streak += 1
        if self._api_error_streak >= int(self.loop_cfg["api_error_max"]):
            self.tg.send_alert(
                f"API 错熔断:连续 {self._api_error_streak} 次接口异常"
                f"({type(e).__name__}),新增风险暂停并人工核查")
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

    def _advance(self, cs: CampaignState, now_ms: int) -> None:
        last = cs.trig_bars[-1][0] if cs.trig_bars else 0
        new_bars = self.client.history_candles(
            cs.symbol, self.cog_cfg["granularity"], start_ms=last + 1,
            limit=100)
        trig_ms = int(self.cog_cfg["trigger_tf_seconds"]) * 1000
        obs_ms = int(self.cog_cfg["obs_tf_seconds"]) * 1000
        scale = obs_ms // trig_ms
        for bar in new_bars:
            if bar[0] <= last:
                continue
            last = bar[0]
            if cs.trig_bars and bar[0] - cs.trig_bars[-1][0] != trig_ms:
                self.tg.send_alert(
                    f"{cs.symbol} 2H 序列缺口 {cs.trig_bars[-1][0]} → "
                    f"{bar[0]};认知帧重装配(持仓事实保留)")
                fresh = self._build_campaign(cs.symbol)
                if fresh is not None:
                    fresh.pm.positions = cs.pm.positions
                    self.campaigns[cs.symbol] = fresh
                return
            if self.mode == "paper":
                self._paper_fill_pass(cs.symbol, bar)
            cs.trig_bars.append(bar)
            for pos in self.positions.values():    # worst-case mark 就近更新
                if pos["symbol"] == cs.symbol:
                    pos["mark"] = float(bar[4])
            k = len(cs.trig_bars) - 1
            ts, o, h, l, c, _v = bar
            emitted = cs.tracker.on_trigger_bar(k, ts, o, h, l, c)
            if emitted:
                ctx = cs.tracker._ctx
                view = StructureView(
                    regime=ctx.regime,
                    zones=tuple(z.clone() for z in ctx.zones), atr=ctx.atr,
                    pivots=ctx.pivots[-self.params.range_max_pivots:])
                self._route_step(cs, ctx.atr, emitted, view, mic=True)
            if (bar[0] + trig_ms) % obs_ms == 0:   # 12h 桶最末 2H ⇒ 认知收盘
                cog_bar = aggregate_bucket(cs.trig_bars[-scale:],
                                           bar[0] + trig_ms - obs_ms)
                self._close_cognition(cs, cog_bar)

    def _close_cognition(self, cs: CampaignState, cog_bar) -> None:
        s = cs.detector.structure
        n_pivots_before = len(s.pivots)
        emitted = cs.detector.update(cog_bar)
        new_pivots = list(s.pivots[n_pivots_before:])
        i = len(cs.cog_bars)
        scale = int(self.cog_cfg["obs_tf_seconds"]) // \
            int(self.cog_cfg["trigger_tf_seconds"])
        hi = len(cs.trig_bars) - 1
        cs.tracker.sync(i, cs.detector,
                        lambda p: max(0, hi - scale + 1),
                        lambda p, _h=hi: _h)
        cs.cog_bars.append(cog_bar)
        atr = s.atr()
        sym = cs.symbol
        # §8 持仓管理(入场成交的认知 bar 不做裁决,canonical 同口径)
        pos_pm = cs.pm.positions.get(sym)
        if pos_pm is not None and not (pos_pm.opened_ts > cog_bar[0]):
            mgmt = cs.pm.on_bar(sym, cog_bar, atr, new_pivots, emitted or [])
            for intent in mgmt:
                self._exec_mgmt(cs, intent, cog_bar)
        if emitted:
            view = StructureView(
                regime=s.regime(), zones=s.zones, atr=atr,
                pivots=s.pivots[-self.params.range_max_pivots:])
            self._route_step(cs, atr, list(emitted), view, mic=False)

    def _route_step(self, cs: CampaignState, atr: float, events: list, view,
                    *, mic: bool) -> None:
        """canonical 决策:ctx18 注入(rngq/pmd 同回放口径)→ 路由 → 意图。"""
        sym = cs.symbol
        wq, wp = cs.router.ctx18_windows()
        if mic:
            rngq_bars, ri = cs.trig_bars, len(cs.trig_bars) - 1
            bar = cs.trig_bars[-1]
        else:
            rngq_bars, ri = cs.cog_bars, len(cs.cog_bars) - 1
            bar = cs.cog_bars[-1]
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
        qty = self.risk["risk_frac"] * size_mult * equity / risk_per_unit
        qty = min(qty, self.risk["leverage_cap"] * equity / entry_ref)
        qty = _floor_step(qty, spec.size_step, spec.size_place)
        # 组合容量(canonical worst-case FIFO:整笔跳过)
        outstanding = sum(
            worst_case_of_position(p["direction"], p["qty"],
                                   p.get("mark") or p["entry"], p["invalid"],
                                   self.risk["taker_fee"])
            for p in self.positions.values())
        candidate = qty * risk_per_unit + qty * entry_ref * 2.0 * \
            (self.risk["taker_fee"] + self.risk["slippage_per_side"])
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
        if self.open_intents.get(oid, {}).get("status") in ("armed", "filled"):
            return                               # 断点幂等:同键不重发
        self.counters["signals"] += 1
        regime = hyp.regime.value if hyp.regime is not None else ""
        intent = {"oid": oid, "symbol": sym, "role": "entry",
                  "kind": d.kind, "direction": hyp.direction, "qty": qty,
                  "limit_price": entry_ref if d.kind == "limit" else None,
                  "invalid": float(hyp.invalid),
                  "target_1": hyp.target_1, "event_id": hyp.event_id,
                  "route": d.route, "size_mult": size_mult,
                  "decision_ts": t, "mic": mic,
                  "expiry_ts": t + N_FILL_H * MS_1H, "status": "armed",
                  "exchange_order_id": None}
        self.open_intents[oid] = intent
        self.tg.send_signal(sym, hyp.event_id,
                            "多" if hyp.direction > 0 else "空", d.route,
                            entry_ref, float(hyp.invalid),
                            f" 规模 {qty}(×{size_mult:g})"
                            f"{' [mic]' if mic else ''}", regime=regime)
        if self.mode == "live":
            if d.kind == "limit":
                resp = self.client.place_limit_order(
                    sym, side=side, size=qty, price=entry_ref, client_oid=oid)
                intent["exchange_order_id"] = str(resp.get("orderId") or "")
                self.counters["orders_placed"] += 1
            else:
                resp = self.client.place_market_order(
                    sym, side=side, size=qty, client_oid=oid)
                intent["exchange_order_id"] = str(resp.get("orderId") or "")
                self.counters["orders_placed"] += 1
                self._live_entry_filled(intent, side_fallback_price=entry_ref,
                                        now_ms=t)
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
        cs = self.campaigns.get(sym)
        self.positions[sym] = {
            "symbol": sym, "direction": it["direction"], "qty": it["qty"],
            "entry": price, "invalid": it["invalid"],
            "target_1": it["target_1"], "event_id": it["event_id"],
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
        self.tg.send_fill(sym, f"开仓({self.mode})", it["qty"], price,
                          note=f"{it['event_id']} {it['route']}")
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
            self._arm_close_intent(sym, pos, pos["qty"], "market", None,
                                   intent.reason, t, role="exit")
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
        side = "sell" if pos["direction"] > 0 else "buy"
        oid = client_oid_of(self.OID_PREFIX, sym, str(reason)[:16], side, t,
                            role=role)[:36]
        if self.open_intents.get(oid, {}).get("status") == "armed":
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
            if kind == "limit":
                resp = self.client.place_limit_order(
                    sym, side=side, size=qty, price=limit_price,
                    client_oid=oid)
                it["exchange_order_id"] = str(resp.get("orderId") or "")
            else:
                resp = self.client.place_market_order(
                    sym, side=side, size=qty, client_oid=oid)
                it["exchange_order_id"] = str(resp.get("orderId") or "")
            self.counters["orders_placed"] += 1
            if kind == "market":                 # 市价即记账(限价待成交确认)
                self._live_close_filled(it, ref_price=pos.get("mark") or
                                        pos["entry"])
        self._journal("mgmt_intent", {"symbol": sym, "role": role,
                                      "kind": kind, "qty": qty,
                                      "reason": str(reason)[:80]})

    def _live_close_filled(self, it: dict, *, ref_price: float) -> None:
        pos = self.positions.get(it["symbol"])
        if pos is None:
            return
        qty = min(it["qty"], pos["qty"])
        pos["qty"] = round(pos["qty"] - qty, 10)
        it["status"] = "filled"
        self.counters["reduce_fills" if it["role"] == "reduce" else "exits"] += 1
        self.tg.send_fill(it["symbol"], f"平仓({self.mode},{it['role']})",
                          qty, float(ref_price),
                          note=str(it["route"])[:60])
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
            want = float(pos["invalid"])
            cur = self.stops.get(sym)
            if cur is not None and abs(cur["trigger"] - want) < 1e-12 \
                    and abs(cur["qty"] - pos["qty"]) < 1e-12:
                continue
            try:
                if cur is not None and cur.get("order_id"):
                    self.client.cancel_plan_order(sym, cur["order_id"])
                resp = self.client.place_stop_market_plan(
                    sym, trigger_price=want, size=pos["qty"],
                    plan_type="loss_plan",
                    hold_side="long" if pos["direction"] > 0 else "short",
                    client_oid=client_oid_of(
                        self.OID_PREFIX, sym, str(pos["event_id"]),
                        "stop", int(now_ms), role="stop"))
                self.stops[sym] = {"trigger": want, "qty": pos["qty"],
                                   "order_id": str(resp.get("orderId") or "")}
                self._journal("stop", {"symbol": sym, "trigger": want,
                                       "qty": pos["qty"]})
            except BitgetError as e:
                self.tg.send_alert(f"{sym} 保护止损计划失败:{str(e)[:120]}"
                                   f"(当前无触发保护,人工介入)")

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
                    # 平仓单方向存的就是报单 side 符号(+1=buy/-1=sell)
                    side = "buy" if it["direction"] > 0 else "sell"
                    try:
                        self.client.place_market_order(
                            it["symbol"], side=side, size=it["qty"],
                            client_oid=oid[:30] + "cv")
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

    def _reconcile(self) -> None:
        try:
            ex_pos = self.client.positions()
        except BitgetError as e:
            self._api_note(e)
            return
        for p in ex_pos:
            if p["symbol"] in self.positions:
                self.positions[p["symbol"]]["mark"] = p["mark_price"]
        mismatches = reconcile_positions(self.positions, ex_pos)
        for m in mismatches:
            self.counters["reconcile_alerts"] += 1
            self._journal("reconcile", {"mismatch": m})
            self.tg.send_alert(f"持仓对账不一致:{m}(异己仓不动作,人工核查)")
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
        self.tg.send_fill(sym, f"平仓(paper,{it['role']})", qty, price,
                          pnl=gross - fee, note=str(it["route"])[:60])
        self._journal("close_fill", {"oid": it["oid"], "symbol": sym,
                                     "qty": qty, "price": price,
                                     "pnl": gross - fee})
        pos["qty"] = round(pos["qty"] - qty, 10)
        if it["role"] == "exit" or pos["qty"] <= 1e-9:
            self.positions.pop(sym, None)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load_contracts_live(client, symbols: list) -> dict:
    """合约规格(实盘拉取 → ContractSpec);min_notional 按合约的唯一来源。"""
    want = set(symbols)
    out = {}
    for entry in client.contracts():
        if entry.get("symbol") in want:
            out[entry["symbol"]] = ContractSpec.from_entry(entry)
    return out


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
    secrets = load_secrets()
    client = BitgetLiveClient(secrets)
    tg = TelegramNotifier(
        secrets, dropped_log=ROOT / "runtime" / "live" / "tg_missed.jsonl")
    contracts = load_contracts_live(client, list(cfg["symbols_core"]))
    runner = LiveRunner(args.mode, cfg, client, tg,
                        contracts=contracts,
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
