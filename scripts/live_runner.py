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
from bisect import bisect_left
import gzip
import hashlib
import json
import math
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace as dc_replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import paths                                # noqa: E402
from market_operator.bitget_live import (                        # noqa: E402
    BitgetError, BitgetLiveClient)
from market_operator.broker import (                            # noqa: E402
    ContractSpec, floor_step)
from market_operator.events import EventDetector, canonical_event_id  # noqa: E402
from market_operator.engine import (                             # noqa: E402
    build_position_manager,
    load_strategy_bundle,
    parse_execution_config,
    select_cognition_route_events,
)
from market_operator.heartbeat import (                          # noqa: E402
    SCHEMA as HB_SCHEMA, HeartbeatGuard, SingletonLock, iso_utc)
from market_operator.hypothesis import StructureView, entry_contract_error, entry_cost_error, execution_target  # noqa: E402
from market_operator.position import (                          # noqa: E402
    Position, PositionManager, opp_lock_step)
from market_operator import adopt as adopt_mod                   # noqa: E402

from market_operator.secrets_loader import SecretsError, load_secrets  # noqa: E402
from market_operator.routing import RouterParams, MarketRouter      # noqa: E402
from market_operator.structure import Params                    # noqa: E402
from market_operator.tg import NotificationGuard, TelegramNotifier  # noqa: E402
from market_operator.trigger import (                           # noqa: E402
    MicTracker, resolve_trigger_tf)

CONFIG_PATH = ROOT / "config" / "live.json"
STRATEGY_CONFIG = ROOT / "config" / "strategy.json"   # 生产策略配置(唯一)

MS_1H = 3_600_000
N_FILL_H = 12                                    # canonical N_fill(墙钟小时)
TF_SECONDS_MAP = {"15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "12h": 43200, "1d": 86400}
#: Longest 15m catch-up window the price clock will replay. A longer gap means
#: the process was stalled/restarted; re-anchor to the last closed bar instead
#: of crawling days of history and acting on stale closes.
PRICE_CLOCK_MAX_CATCHUP_STEPS = 4


def initial_price_clock_ts(now_ms: int, step: int, opened_ts: int) -> int:
    """Startup anchor for the 15m price clock: the last closed bar, not entry.

    ``_best`` / ``invalid`` / ``armed`` are persisted in ``campaigns.json``; a
    restart must not walk the whole position history 100 bars at a time. Doing
    so would replay days-old closes and could execute an exit at a stale price.
    We only need the most recent closed 15m bar (plus the entry bar for a brand
    new position so the first fetch is not wasted).
    """
    floor = (int(now_ms) // step) * step
    return max(floor - step, (int(opened_ts) // step) * step)


def cascade_label(tf: str, trigger_s: object) -> str:
    """周期级联的展示串。`None`/0 表示该周期没有下级扳机(如 1d 直达)。

    修前主写 `int(trig_map[tf]) // 60`,而三重滤网配置里 `1d: null` 是合法值,
    启动横幅会在进入主循环前抛 `int(None)` —— runner 抓完冷数据即崩,看护
    只能反复重拉。展示层必须把"没有下级周期"当成一等状态。
    """
    try:
        secs = int(float(trigger_s or 0))
    except (TypeError, ValueError):
        secs = 0
    if secs <= 0:
        return tf
    if secs < 3600:
        return f"{tf}->{secs // 60}m"
    return f"{tf}->{secs // 3600}h"


# ---------------------------------------------------------------------------
# canonical 参数组(v21 唯一生产链)
# ---------------------------------------------------------------------------


def load_strategy_params(cfg_path: Path = STRATEGY_CONFIG) -> tuple:
    """回放与实盘唯一共享的策略配置入口(委托 market_operator.engine)。"""
    bundle = load_strategy_bundle(cfg_path)
    return dict(bundle.raw), bundle.params, bundle.router, dict(bundle.router_by_tf)


def new_position_manager(params: Params, raw: dict) -> PositionManager:
    """回放与实盘唯一共享的持仓管理器构造器(委托 market_operator.engine)。"""
    arm = parse_execution_config(raw)
    return build_position_manager(params, arm)


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


def _fill_ts_ms(row: dict) -> Optional[int]:
    """成交行的**可比较**时间戳(epoch ms);取不到返回 None。

    Bitget v2 fills 的字段随端点/版本漂移(`cTime` / `uTime` / `updatedTime`),
    因此不假定某一个是权威,只要求"同一字段口径内可比" —— 排序与
    `since_ms` 过滤都只依赖可比性,不依赖绝对含义。
    """
    for key in ("cTime", "uTime", "updatedTime"):
        v = row.get(key)
        if v in (None, ""):
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# 离场事实分型(纯函数;P1 语义:谁执行的 × 赚没赚,两个正交事实)
# ---------------------------------------------------------------------------
#
# 为什么必须分开:同一句"平仓"里混着三件不同的事 ——
#   ① 交易所挂着的保护单被触发(执行者=交易所,runner 只对账);
#   ② 外部/人工平仓(执行者不是我方任何逻辑);
#   ③ 策略决定**此刻**离场(执行者必须是本进程,见 `_live_market_exec`)。
# 把 ③ 写进 ① 的分支就是 2026-10-24 的事故:日志写"⇒ 市价离场",仓还在。
#
# 盈利与否是**独立**维度:保护单在盈利侧成交(移动止损)= 事实上的止盈,
# 但它的执行者仍是交易所;外部平仓也可能盈利。分开记,文案才不会说谎。

# 保护单成交的判据容差(相对价格):止损市价单允许滑点,但滑过 0.2%
# 就不能再把成交归因于那张保护单 —— 否则"外部平仓"会被冒充成止损。
EXIT_STOP_TOL_FRAC = 2e-3


def realized_pnl(direction: int, qty: float, entry: float, exit_px: float,
                 taker_fee: float) -> float:
    """已实现盈亏(canonical 口径:价差 × 数量 × 方向 − 平仓侧 taker)。"""
    d = 1 if int(direction) > 0 else -1
    qty, entry, exit_px = float(qty), float(entry), float(exit_px)
    gross = qty * d * (exit_px - entry)
    fee = qty * exit_px * float(taker_fee)
    return gross - fee


def stop_execution_evidence(direction: int, exit_px: float, stop_px: float,
                            *, tol_frac: float = EXIT_STOP_TOL_FRAC) -> bool:
    """成交价能否**归因于**那张保护单(挂单不可能成交得比触发价更好)。

    判据:`d·(exit − stop) ≤ +tol`
      * 多头(d=+1)⇒ `exit ≤ stop + tol`(在止损或其**下方**成交);
      * 空头(d=−1)⇒ `exit ≥ stop − tol`(在止损或其**上方**成交)。

    不对称是刻意的:止损市价单只在"不利侧"成交,所以
      · 成交优于触发价 ⇒ **一定不是**这张挂单(外部/人工平仓);
      · 成交不优于触发价 ⇒ 可归因(可能滑点,含跳空越价成交)。
    反过来的判据(≥ −tol)会让**每一次盈利离场**都变成"移动止损止盈",
    把外部平仓冒充成自家止盈 —— 比不分类更危险,故此处钉死方向。

    `tol` 吸收"标记价触发 / 最新价成交"的正常偏差(相对价格 0.2%);
    超过 `tol` 的有利偏差一律记为外部平仓,并在文案里写出两个价位供核对。
    """
    d = 1 if int(direction) > 0 else -1
    px, stop = float(exit_px), float(stop_px)
    tol = abs(px) * float(tol_frac) + 1e-12
    return d * (px - stop) <= tol


def exit_label(*, executor: str, pnl: Optional[float],
               reason: str = "") -> str:
    """离场通知的**动作类型**:执行者 × 盈亏 → 一句可核对的中文事实。

    executor ∈ {protective_stop, external, unknown, local_market}:
      * `protective_stop` —— 保护单成交;盈利侧成交即"移动止损止盈";
      * `external`      —— 成交价不在保护单成交侧 ⇒ 不是本系统止损;
      * `unknown`       —— 取不到成交价,只如实报"交易所已无此仓";
      * `local_market`  —— 本进程按决策发的市价单(回撤预算耗尽/裸奔补平)。
    """
    gain = None if pnl is None else float(pnl) > 0.0
    if executor == "local_market":
        if "lock" in str(reason):
            return ("止盈(回撤预算耗尽·市价离场)" if gain
                    else "离场(回撤预算耗尽·市价)")
        return "止盈(无保护单·市价离场)" if gain else "止损(无保护单·市价离场)"
    if executor == "protective_stop":
        return "止盈(移动止损触发)" if gain else "止损(保护单触发)"
    if executor == "external":
        return "离场确认(外部平仓·盈利)" if gain else "离场确认(外部平仓·亏损)"
    return "离场确认(交易所已无此仓)"


_NO_POS_MARKS = ("43023", "仓位不足", "position is empty", "no position")
# 43023 有**两种**含义,处置完全不同:
#   ① 交易所已无该仓位(本地残影)          ⇒ 丢弃本地止损记录,等 reconcile 收尾
#   ② 仓位小于交易所 TPSL 的最小名义(蚂蚁仓) ⇒ 仓位是真的,只是挂不上交易所单
# 旧实现把两种都当 ①:直接 `stops.pop()`,于是小仓位**既没有交易所保护单、
# 也不在本地监控里**,等于裸奔(2026-10-29 实盘 ADVANTESTUSDT 名义 ~18.5U 即此)。
# 现在一律先切"本地止损"模式:要么交易所触发、要么本地裁决,绝不允许两者都没有。
_STOP_TOO_SMALL_MARKS = ("43023", "仓位不足", "position is not enough",
                         "insufficient position")



def _mark_from_reject(msg: str) -> Optional[float]:
    """从交易所拒单文本里取它自己报的**标记价**。

    45122 报文形如 `... 空头仓位止损价格请>标记价格 0.34387`。用它判\"是否已越线\"
    比本地 `pos["mark"]`(最多 2 分钟前 reconcile 的值)更准,也不必额外调行情。
    取不到返回 `None`,调用方回退到本地 mark。
    """
    s = str(msg)
    for key in ("标记价格", "mark price", "markPrice"):
        i = s.rfind(key)
        if i < 0:
            continue
        j = i + len(key)
        while j < len(s) and not (s[j].isdigit() or s[j] == "."):
            j += 1
        k = j
        while k < len(s) and (s[k].isdigit() or s[k] == "."):
            k += 1
        try:
            v = float(s[j:k])
        except ValueError:
            continue
        if v > 0:
            return v
    return None


def _stop_reject_no_position(msg: str) -> bool:
    """挂保护单被拒的原因是不是\"交易所已无该仓位\"。

    实测(2026-10-24 1000BONKUSDT)连续 4 次 `code=43023 仓位不足` —— 本地还有
    残影、交易所已平,重挂永远不会成功。这与"价格已穿越触发价"(45122)是两种
    完全不同的处置:前者只能等 `reconcile` 收尾,对它发市价平仓只会再被拒一次,
    还会误报"仓位仍在,人工核查"。
    """
    s = str(msg)
    return any(k in s for k in _NO_POS_MARKS)


def _stop_requires_local(msg: str) -> bool:
    """是不是「仓位太小/挂不上交易所 TPSL」——此时必须退化为**本地止损监控**。

    与 `_stop_reject_no_position` 的区别只有一个,但很关键:这里**不丢仓位**。
    仓位是真金白银,交易所不让我们挂保护单,不代表风险不存在。
    """
    s = str(msg)
    return any(k in s for k in _STOP_TOO_SMALL_MARKS)



def _utc_day_gap(prev: Optional[str], cur: str) -> Optional[int]:
    """两个 `YYYY-MM-DD`(UTC 日)相隔几天;任一不可解析返回 None。

    日损熔断用它判断"跨日重启是否只隔一夜":只有隔夜时,上一笔权益才是
    日界权益的合理估计;停机多日时它已不具可比性。
    """
    try:
        return (date.fromisoformat(str(cur))
                - date.fromisoformat(str(prev))).days
    except (TypeError, ValueError):
        return None


class DailyLossBreaker:
    """日损熔断:UTC 日界内 (equity − start)/start ≤ −max_frac 即停;日界复位。

    日界锚点 = **该日第一笔事实的权益**。进程连续运行时它就是上一笔 tick(≈ 日界
    瞬间);崩溃跨日重启时不能用"重启瞬间权益" —— 那已经含掉线期间的亏损,等于
    把缺口亏损从当日预算里抹掉(实测重启后锚 100.3977,而日界真实权益 ~107.6,
    当天可再亏满 10%)。改用**上一日最后一笔权益**(缺口前的已知事实)为锚;
    停机超过一日则无任何可比事实,只能按重启权益起算(如实在 daily 上报里体现)。
    """

    def __init__(self, max_frac: float) -> None:
        self.max_frac = float(max_frac)
        self.day: Optional[str] = None
        self.start_equity: Optional[float] = None
        self.tripped = False
        self.last_equity: Optional[float] = None   # 上一笔 tick 权益(日界锚来源)
        # 上一日的收盘事实:日结上报要用**昨日**锚/收盘,而不是滚动后的新锚。
        self.prev_day: Optional[str] = None
        self.prev_start_equity: Optional[float] = None
        self.prev_close_equity: Optional[float] = None

    def update(self, now_ms: int, equity: float) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc) \
            .strftime("%Y-%m-%d")
        eq = float(equity)
        if day != self.day:
            anchor = eq
            if (self.start_equity is not None and self.last_equity is not None
                    and _utc_day_gap(self.day, day) == 1):
                anchor = float(self.last_equity)
            self.prev_day, self.prev_start_equity = self.day, self.start_equity
            self.prev_close_equity = self.last_equity
            self.day, self.start_equity, self.tripped = day, anchor, False
        self.last_equity = eq

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


def edge_size_multiplier(table: Optional[dict], event_id: object,
                         direction: int) -> float:
    """事件 × 方向的**期望值权重**(1.0 = 不缩放)。

    键 = ``"<canonical_event_id>|<±1>"``(写 ``E1`` 或 ``BREAKOUT_ACCEPTED``
    等价,归一化只走 ``canonical_event_id`` 一处)。

    口径(2026-10-25 实盘归因):
    - 只允许**缩**,不允许放大 —— 放大等于拿实盘去赌一个只在回放里成立的
      正期望,而回放的滑点/成交近似比实盘乐观。
    - 下限固定 10%:**代码层面不允许把任何 (事件, 方向) 归零**。配置只能
      决定"少做",不能决定"不能做",做多做空的能力不被配置剥夺。
    - 表缺失 / 值非法(NaN、非数、≤0)⇒ 1.0,即完全不改变现行行为。
    """
    if not table:
        return 1.0
    # 两个探针:语义标识优先,其次是配置里原样写的别名(两者等价)
    probe = (f"{canonical_event_id(event_id)}|{int(direction)}",
             f"{event_id}|{int(direction)}")
    raw = next((table[k] for k in probe if k in table), None)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if val != val or val <= 0.0:          # NaN / 非正数 ⇒ 不缩放
        return 1.0
    return min(max(val, 0.1), 1.0)


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
    """子序列 → 任意整数倍周期(30m/1h/2h/4h/12h/1d)帧:网格对齐;末桶不齐(未完整)丢弃,
    内部缺口 raise(不可静默);首桶不齐如实保留(canonical 首/末不完整桶口径)。"""
    MS = (
        int(bars2h[1][0] - bars2h[0][0])
        if len(bars2h) >= 2 and int(bars2h[1][0]) > int(bars2h[0][0])
        else 7_200_000
    )
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
        # 与 trigger.prepare_mic_cognition 及 campaign.run_campaigns 完全相同的因果时序:
        # 凡是在 close_moment 前(含)已收盘的大周期认知 bar 先推进并冻结快照,随后再推进触发 bar!
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


def trigger_origin_ms(cs: CampaignState) -> int:
    """The same trigger-index zero used by the cropped cognition warmup.

    Raw 2h history is retained for charts/management. Its first bar is not
    necessarily the first bar replayed into the tracker. The first 4h bucket
    can also be partial, so counting raw bars is not a valid trigger index.
    """
    first_cog = int(cs.cog_bars[0][0])
    start = bisect_left(cs.trig_bars, first_cog, key=lambda b: int(b[0]))
    first = int(cs.trig_bars[start][0])
    step = int(cs.trigger_tf_seconds) * 1000
    return first - first % step



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
    MAX_EXIT_SUBMIT_RETRIES = 3      # 决策型离场下单失败的重试上限(每步一次)

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
        self.mode, self.cfg, self.client = mode, cfg, client
        self.tg = NotificationGuard(tg)
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
        self.capacity_mode = str(self.raw_strat.get("capacity_mode") or "admission_full_cost")
        directional = self.risk.get("directional_cap_frac", self.raw_strat.get("directional_cap_frac"))
        self.directional_cap_frac = float(directional) if directional is not None else None
        if self.directional_cap_frac is not None and not (0.0 < self.directional_cap_frac <= 1.0):
            raise ValueError("directional_cap_frac must be in (0,1]")
        self.contracts = contracts or {}
        self.contract_raw = contract_raw or {}     # 交易所原始合约条目(maxLever 等)
        self.leverage_by_sym: dict[str, int] = {}  # 各品种实际生效杠杆
        self.equity: Optional[float] = \
            float(equity0) if equity0 is not None else None
        # 未实现盈亏(交易所口径);风险资本 = equity − unrealized_pl
        self.unrealized_pl: float = 0.0
        self.state_path = Path(state_path) if state_path else \
            paths.resolve(self.loop_cfg["state_path"])
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
            self.heartbeat_path = (paths.resolve(str(hb_rel)) if hb_rel
                                   else self.state_path.parent / "heartbeat.json")
        self.started_ms = int(time.time() * 1000)
        self._loop_ts_ms = self.started_ms
        self._step_started_ms = 0                  # 当前步的开始时刻(卡在哪一步)
        self._step_count = 0
        self._campaigns_dirty = True
        self._campaigns_saved_at = 0.0
        self.candle_cache_dir = self.state_path.parent / "candles"
        self.journal_dir = Path(journal_dir) if journal_dir else \
            paths.resolve(self.loop_cfg["journal_dir"])

        self.breaker = DailyLossBreaker(self.risk["max_daily_loss_frac"])
        self.rate = RateLimiter(self.risk["orders_per_hour"])
        # 接管交易所既有仓位(扩展层,默认关;见 `market_operator/adopt.py`)。
        # 开关放在 `config/live.json` 的 `adopt_external_positions.enabled`,
        # **不读策略配置** —— 接管是运维/执行面的事,不是策略参数。
        _adopt_cfg = cfg.get("adopt_external_positions") or {}
        self.adopt_enabled = bool(_adopt_cfg.get("enabled"))

        self.universe_enabled = bool(universe_enabled)
        self.campaigns: dict[str, CampaignState] = {}
        self.extra_campaigns: dict[tuple[str, str], CampaignState] = {}
        self.symbols_pending: list = sorted(symbols)
        self._short_history_alerted: set[str] = set()
        self.positions: dict[str, dict] = {}
        self.open_intents: dict[str, dict] = {}
        self.stops: dict[str, dict] = {}
        # 信号频道去重窗口(进程内):同一事件同一拍只推一条,与闸门结果无关
        self._signal_sent: dict[tuple, int] = {}
        self.counters = {"signals": 0, "orders_placed": 0, "cap_skips": 0,
                         "tf_cap_skips": 0, "min_notional_skips": 0,
                         "rate_skips": 0, "breaker_blocks": 0, "fills": 0,
                         "mgmt_execs": 0, "expired_intents": 0,
                         "reconcile_alerts": 0, "reduce_fills": 0, "exits": 0,
                         "api_halt_skips": 0, "order_errors": 0,
                         "lock_exits": 0,            # 回撤预算耗尽 → 本进程市价离场
                         "exit_submit_errors": 0,    # 决策型离场下单失败
                         "unprotected_stop_exits": 0,  # 防线触及但无交易所保护单
                         "exit_settle_miss": 0,      # 决策型离场成交回执始终未取到
                         "entry_settle_miss": 0,     # 开仓成交回执始终未取到(仍按参考价)
                         "signal_notice_errors": 0}  # 信号广播自身异常(不影响决策)
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
        # 决策型离场的**成交回执待结**:market 单下单瞬间 fills 端点常还没索引到,
        # 先用参考价记账(宁可偏差也不静默),随后每步用权威成交价校正一次。
        # 键 = symbol(同一仓同一时刻只可能有一笔离场在结算)。
        self._exit_settle: dict[str, dict] = {}
        # 开仓成交回执待结:市价开仓下单瞬间 fills 端点通常还没索引到,先用参考
        # 价记账(宁可偏差也不静默),随后每步用**该订单号**的成交均价回写 entry。
        # 键 = symbol(同一仓同一时刻只可能有一笔开仓在结算)。
        self._entry_settle: dict[str, dict] = {}
        # 决策型离场**下单失败**的重试队列:失败绝不能等到下一根 2H bar(最长 2h 裸奔)。
        # 每步重试一次,上限 MAX_EXIT_SUBMIT_RETRIES,终态必须告警到人。
        self._exit_retry: dict[str, dict] = {}
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
        # Price-protection clock (independent of entry/event clocks):
        #   bar = legacy 2H sub-bar only
        #   15m = closed 15m bars arm the target lock; fresh quotes ratchet
        #   realtime = native target exit + positive-frac quote-touch arming
        self.price_clock = str((cfg.get("protection") or {})
                               .get("price_clock", "bar")).lower()
        if self.price_clock not in ("bar", "15m", "realtime"):
            raise RuntimeError(
                f"protection.price_clock 非法: {self.price_clock!r}"
                f"(仅允许 bar|15m|realtime)")
        self._price_seen: dict[str, int] = {}
        self._price_last: dict[str, int] = {}
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
        # 跨日重启的日界锚估计源:上一笔 tick 权益(缺口前的已知事实)。
        if st.get("equity") is not None:
            self.breaker.last_equity = float(st["equity"])
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
                    if (len(saved_bars) >= 2 and len(bars2h) >= 2
                            and int(saved_bars[1][0] - saved_bars[0][0]) != int(bars2h[1][0] - bars2h[0][0])):
                        saved_bars = []
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

    def _candle_cache_path(self, symbol: str, *, gz: bool = True,
                           gran: Optional[str] = None) -> Path:
        """K 线缓存路径:默认 `.json.gz`(密度高 3~4 倍),`gz=False` 为旧格式。

        缓存是**可再生产物**(交易所随时可补齐),压缩它不损失任何信息:
        1800 根 × 6 字段的 JSON 纯文本 ~100KB,压后 ~25KB。
        """
        g = str(gran or self.cog_cfg.get("granularity") or "2H").upper()
        suffix = ".json.gz" if gz else ".json"
        return self.candle_cache_dir / f"{symbol}_{g}{suffix}"

    def _load_cached_bars(self, symbol: str, *, gran: Optional[str] = None) -> list[tuple]:
        """读缓存:优先 .json.gz,回落旧 .json(便于无损迁移,不必停机改格式)。"""
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
                return [(int(r[0]), float(r[1]), float(r[2]),
                         float(r[3]), float(r[4]), float(r[5])) for r in raw]
            except Exception:
                continue
        return []

    def _save_cached_bars(self, symbol: str, bars: list, *,
                          gran: Optional[str] = None) -> None:
        if not bars:
            return
        try:
            self.candle_cache_dir.mkdir(parents=True, exist_ok=True)
            want = int(self.cog_cfg.get("warmup_trigger_bars", 3600))
            p = self._candle_cache_path(str(symbol), gran=gran)
            payload = json.dumps(bars[-want:], separators=(",", ":"))
            self._write_json_atomic(p, payload)          # 文本原子写
            legacy = self._candle_cache_path(str(symbol), gz=False, gran=gran)
            if legacy.exists():                          # 迁移:旧明文缓存删除
                legacy.unlink(missing_ok=True)
        except Exception:
            pass

    def _base_step_seconds(self, bars: Optional[list] = None, *,
                           gran: Optional[str] = None) -> int:
        if bars and len(bars) >= 2 and int(bars[1][0]) > int(bars[0][0]):
            return int(bars[1][0] - bars[0][0]) // 1000
        g = str(gran or self.cog_cfg.get("granularity") or "2H").strip().lower()
        if g.endswith("utc"):
            g = g[:-3]
        return int(TF_SECONDS_MAP.get(g, 7200))

    def _fetch_trigger_bars(self, symbol: str, want: int,
                            seed_bars: Optional[list] = None, *,
                            gran: Optional[str] = None) -> list:
        """基础触发帧回补:优先复用本地缓存 + 增量补齐;不足时倒序分页补齐并落盘缓存。"""
        use_gran = str(gran or self.cog_cfg["granularity"])
        base_ms = self._base_step_seconds(gran=use_gran) * 1000
        valid_seed = seed_bars or []
        if len(valid_seed) >= 2 and int(valid_seed[1][0] - valid_seed[0][0]) != base_ms:
            valid_seed = []
        have: dict = {}
        for src in (self._load_cached_bars(symbol, gran=use_gran), valid_seed):
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
                        symbol, use_gran,
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
                symbol, use_gran, end_ms=end_ms, limit=200)
            fresh = [b for b in page if b[0] not in have]
            if not fresh:
                break
            for b in fresh:
                have[int(b[0])] = b
            end_ms = fresh[0][0]
            guard += 1
        bars = [have[t] for t in sorted(have)][-want:]
        self._save_cached_bars(symbol, bars, gran=use_gran)
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
        max_workers = min(16, len(symbols))
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        need_30m = (
            self.mode == "live"
            and hasattr(self.client, "history_candles")
            and any(int(trig_map.get(tf) or 7200) < 7200 for tf in (self.cog_cfg.get("tf_list") or ()))
        )
        sub_want = max(520, (self._min_cog_bars_for_tf() + 5) * 8)

        def _fetch_one(s: str) -> list:
            res_2h = self._fetch_trigger_bars(s, want, seed_by_sym.get(s))
            if need_30m:
                try:
                    self._fetch_trigger_bars(s, sub_want, gran="30m")
                except Exception:
                    pass
            return res_2h

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_fetch_one, sym): sym for sym in symbols}
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
                        *, trig_bars: Optional[list] = None,
                        quiet_if_short: bool = False) -> Optional[CampaignState]:
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
        base_s = self._base_step_seconds(bars2h)
        scale_2h = max(1, obs_s // base_s)
        min_2h_needed = min_cog_bars * scale_2h
        if len(bars2h) < min_2h_needed:
            if not quiet_if_short and symbol not in self._short_history_alerted:
                self._short_history_alerted.add(symbol)
                self.tg.send_alert(f"{symbol}({tf_label}) 2H 历史不足({len(bars2h)}/{min_2h_needed}"
                                   f" 根),暂不开启该周期")
            return None
        for a, b in zip(bars2h, bars2h[1:]):       # 网格完整性(不可静默)
            if b[0] - a[0] != base_s * 1000:
                self.tg.send_alert(f"{symbol} 2H 序列缺口 {a[0]}→{b[0]},"
                                   f"暂不交易")
                return None
        if (
            trig_bars is None
            and trig_s < base_s
            and self.mode == "live"
            and hasattr(self.client, "history_candles")
        ):
            sub_want = max(520, (min_cog_bars + 5) * 8)
            cand_30m = self._fetch_trigger_bars(symbol, sub_want, gran="30m")
            if self._base_step_seconds(cand_30m, gran="30m") == 1800 and len(cand_30m) >= min_cog_bars * 4:
                trig_bars = cand_30m
        use_trig = trig_bars if trig_bars is not None else bars2h
        if trig_bars is not None:
            trig_base_s = self._base_step_seconds(trig_bars, gran="30m")
            for a, b in zip(trig_bars, trig_bars[1:]):
                if b[0] - a[0] != trig_base_s * 1000:
                    use_trig = bars2h
                    break
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
        replay_trig = [b for b in use_trig if b[0] >= first_cog_ts]
        detector, tracker = replay_cognition(
            self.params, symbol, cog_bars, replay_trig,
            obs_tf_seconds=obs_s, trigger_tf_seconds=trig_s)
        rp_tf = self.rp_by_tf.get(tf_label, self.rp)
        router = MarketRouter(rp_tf)
        pm = new_position_manager(self.params, self.raw_strat)
        return CampaignState(symbol=symbol, detector=detector, tracker=tracker,
                             router=router, pm=pm,
                             cog_bars=list(cog_bars),
                             trig_bars=list(use_trig),
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
        bars_30m: Optional[list] = None
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        if (
            self.mode == "live"
            and hasattr(self.client, "history_candles")
            and any(int(trig_map.get(tf) or 7200) < 7200 for tf in (self.cog_cfg.get("tf_list") or ()))
        ):
            sub_want = max(520, (self._min_cog_bars_for_tf() + 5) * 8)
            cand_30m = self._fetch_trigger_bars(symbol, sub_want, gran="30m")
            if self._base_step_seconds(cand_30m, gran="30m") == 1800 and len(cand_30m) >= self._min_cog_bars_for_tf() * 4:
                bars_30m = cand_30m
        main_tf = str(self.cog_cfg.get("main_tf_label") or "12h")
        tf_list = list(self.cog_cfg.get("tf_list") or [main_tf])
        built: dict[str, CampaignState] = {}
        for tf in tf_list:
            sub_trig = (
                bars_30m
                if (bars_30m is not None and int(trig_map.get(tf) or 7200) < 7200)
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
        trig_map = self.cog_cfg.get("trigger_tf_by_htf") or {}
        sub_30m = next(
            (
                sc.trig_bars
                for (s2, _tf), sc in self.extra_campaigns.items()
                if s2 == symbol and self._base_step_seconds(sc.trig_bars) == 1800
            ),
            None,
        )
        for tf in tf_list:
            if tf in active_tfs:
                continue
            obs_s = TF_SECONDS_MAP.get(tf, int(self.cog_cfg["obs_tf_seconds"]))
            base_s = self._base_step_seconds(bars2h)
            scale_2h = max(1, obs_s // base_s)
            if len(bars2h) >= (min_cog + 1) * scale_2h:
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


    def _get_cs_for(self, symbol: str, tf_label: Optional[str] = None) -> Optional[CampaignState]:
        if tf_label and (symbol, tf_label) in self.extra_campaigns:
            return self.extra_campaigns[(symbol, tf_label)]
        return self.campaigns.get(symbol)

    def _max_allowed_leverage(self, symbol: str) -> int:
        """返回该标的允许的最大整数杠杆:min(配置 leverage_cap, 交易所合约 maxLever)。"""
        cap = int(self.risk.get("leverage_cap") or 20)
        try:
            max_lev = int(float((self.contract_raw.get(symbol) or {})
                                .get("maxLever") or cap))
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
        """根据单笔风险预算、止损距离(决定名义价值 notional)与组合仓位保证金规划,
        在 [1, min(leverage_cap, maxLever)] 范围内自动计算该笔开仓所需的最小整数杠杆。
        """
        max_allowed = self._max_allowed_leverage(symbol)
        if equity <= 0.0 or notional <= 0.0:
            return max(1, min(5, max_allowed))
        port_cap = max(float(self.risk.get("portfolio_cap_frac") or 0.50), 1e-6)
        risk_slot_share = candidate_risk / max(port_cap * max(capital, 1e-6), 1e-6)
        # 按该笔风险占总容量池的比例分配计划保证金预算(留出 20% 账户级缓冲,单笔保证金预算占权益 5% ~ 25%)
        planned_margin_frac = min(0.25, max(0.05, risk_slot_share * 0.80))
        used_margin = 0.0
        for s_open, p_open in self.positions.items():
            p_notional = abs(float(p_open.get("qty") or 0.0) * float(p_open.get("mark") or p_open.get("entry") or 0.0))
            p_lev = max(1.0, float(p_open.get("leverage") or self.leverage_by_sym.get(s_open) or max_allowed))
            used_margin += p_notional / p_lev
        avail_margin = max(0.04 * equity, 0.85 * equity - used_margin)
        margin_budget = max(1e-6, min(equity * planned_margin_frac, avail_margin))
        req_lev = int(math.ceil(notional / margin_budget))
        return max(1, min(max_allowed, req_lev))

    def _ensure_leverage(self, symbol: str, target_lev: Optional[int] = None) -> None:
        """杠杆动态设置(在 <= min(leverage_cap, maxLever) 范围内按需设置;hedge_mode 逐侧设置)。

        - 启动/发现品种未指定 target_lev 时:不盲目顶格设 20x,仅记录品种上限待实际开仓时按仓位规划下发;
        - 开仓指定 target_lev 时:若该品种当前无持仓且目标杠杆与已生效杠杆不同,动态调整为 target_lev(失败则降档重试)。
        """
        if self.mode != "live":
            return
        max_allowed = self._max_allowed_leverage(symbol)
        if target_lev is None:
            if symbol in self._leverage_done:
                return
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
            # 已有在途持仓时不中途改低杠杆,避免交易所因在仓保证金重算拒单
            return
        sides = ("long", "short") if self.hedge_mode else (None,)
        ladder = [want] + [lv for lv in (max_allowed, 10, 5, 3, 2, 1) if lv != want and lv <= max_allowed]
        last_err: Optional[Exception] = None
        for lev in ladder:
            try:
                for hs in sides:
                    self.client.set_leverage(symbol, lev, hold_side=hs)
                self._leverage_done.add(symbol)
                self.leverage_by_sym[symbol] = lev
                if lev < want:
                    if not hasattr(self, "_exch_lev_cap"):
                        self._exch_lev_cap = {}
                    self._exch_lev_cap[symbol] = lev
                self._journal("leverage", {"symbol": symbol, "leverage": lev,
                                           "planned_leverage": want,
                                           "max_lever": max_allowed,
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
            if self.price_clock == "realtime":
                # Existing exposure is protected before universe expansion or
                # historical candle work can block the main loop.
                self._sync_target_fills(now_ms)
                self._manage_targets(now_ms)
                self._poll_position_prices(now_ms)
                self._manage_stops(now_ms)
            bal = self.client.balance()
            self.equity = float(bal["equity"])
            # 风险资本 = 权益 − 未实现盈亏(即"已落袋"部分);与回放 Broker.risk_capital
            # 同一口径。浮盈能撑保证金,但不是可以拿去下注的本金。
            self.unrealized_pl = float(bal.get("unrealized_pl") or 0.0)
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
            self._retry_exits(now_ms)       # 上次离场下单失败:本步立即重试
            self._settle_exits(now_ms)      # 决策型离场:用权威成交价校正回执
            self._settle_entries(now_ms)    # 开仓:用权威成交均价校正 entry(§6a)
            self._sync_live_fills(now_ms)
            if self.price_clock == "realtime":
                self._sync_target_fills(now_ms)
                self._manage_targets(now_ms)
            self._advance_price_clock(now_ms)
            self._poll_position_prices(now_ms)
            if self.price_clock == "realtime":
                self._manage_stops(now_ms)
        base_ms = self._base_step_seconds() * 1000
        sub_ms = 1800 * 1000
        sub_30m_by_sym: dict[str, list[CampaignState]] = {}
        base_2h_by_sym: dict[str, list[CampaignState]] = {}
        due_30m: dict[str, int] = {}
        due_base: dict[str, int] = {}
        for sym, cs in sorted(self.campaigns.items()):
            all_cs = [cs] + [
                sub_cs for (s2, _tf), sub_cs in sorted(self.extra_campaigns.items())
                if s2 == sym
            ]
            s_list = [c for c in all_cs if self._base_step_seconds(c.trig_bars) == 1800]
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

        pref_30m: dict[str, list] = {}
        pref_base: dict[str, list] = {}
        if self.mode == "live" and (len(due_30m) + len(due_base)) > 2:
            gran_base = self.cog_cfg["granularity"]
            with ThreadPoolExecutor(max_workers=8) as ex:
                f30 = {
                    ex.submit(self.client.history_candles, s, "30m", start_ms=st, limit=100): s
                    for s, st in due_30m.items()
                }
                fb = {
                    ex.submit(self.client.history_candles, s, gran_base, start_ms=st, limit=100): s
                    for s, st in due_base.items()
                }
                for fut, s in f30.items():
                    try:
                        pref_30m[s] = fut.result()
                    except Exception:
                        pass
                for fut, s in fb.items():
                    try:
                        pref_base[s] = fut.result()
                    except Exception:
                        pass

        # 纯 K 线内生二维相对强弱优先轮询(与回放 _mic_rank 对齐,取代字母序 A->Z 抢夺同向敞口额度)
        ordered_campaigns = sorted(
            self.campaigns.items(),
            key=lambda kv: (-abs(float(kv[1].detector.structure.imbalance_strength())), kv[0]),
        )
        for sym, cs in ordered_campaigns:
            try:
                # 1) 自顶向下(Top-Down)纪律:先推进 2H 基础帧与高周期战役(如 1d->4h),
                #    确保在 00:00 UTC 等交汇点先完成日线宏观结构闭合与优先决策权!
                b_list = sorted(
                    base_2h_by_sym.get(sym) or [],
                    key=lambda c: -int(c.obs_tf_seconds),
                )
                if sym in due_base and b_list:
                    new_bars = (
                        pref_base[sym]
                        if sym in pref_base
                        else self.client.history_candles(
                            sym, self.cog_cfg["granularity"], start_ms=due_base[sym], limit=100)
                    )
                    if new_bars:
                        for idx_b, bc in enumerate(b_list):
                            self._advance(bc, now_ms, new_bars=new_bars, run_paper_fills=(idx_b == 0))
                        self._try_unlock_higher_tfs(sym, b_list[0].trig_bars)
                        self._save_cached_bars(sym, b_list[0].trig_bars)
                # 2) 再推进下级细周期触发战役(如 4h->1h),此时可因果读取刚刚闭合的最新日线宏观结构视图!
                sub_30m_list = sorted(
                    sub_30m_by_sym.get(sym) or [],
                    key=lambda c: -int(c.obs_tf_seconds),
                )
                if sym in due_30m and sub_30m_list:
                    sub_new = (
                        pref_30m[sym]
                        if sym in pref_30m
                        else self.client.history_candles(sym, "30m", start_ms=due_30m[sym], limit=100)
                    )
                    if sub_new:
                        for sc in sub_30m_list:
                            self._advance(sc, now_ms, new_bars=sub_new, run_paper_fills=False)
                        self._save_cached_bars(sym, sub_30m_list[0].trig_bars, gran="30m")
            except BitgetError as e:
                self._api_note(e)
                self.tg.send_alert(f"{sym} bar 推进异常:{str(e)[:120]}")

        self._expiry_sweep(now_ms)
        if self.mode == "live":
            if not self.api_halted:
                self._poll_watch_entries(now_ms)
            self._manage_stops(now_ms)
            if now_ms / 1000 - self._reconcile_at >= \
                    self.loop_cfg["reconcile_seconds"]:
                self._reconcile_at = now_ms / 1000
                self._reconcile()
        self._daily_tick(now_ms)
        self._api_error_streak = 0          # 本轮无异常:连续错计数复位

    def _advance_price_clock(self, now_ms: int) -> None:
        """Closed 15m bars arm/tighten the target lock (no 2H wait).

        Execution-clock only: calls the same ``PositionManager.on_bar(
        price_only=True)`` used by replay, so arming semantics do not fork.
        The 2H ``_on_sub_bar`` target block is skipped while this clock is
        active; its fixed-stop check remains as a fallback.
        """
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
            if last <= 0:
                last = initial_price_clock_ts(
                    now_ms, step, int(pm_pos.opened_ts))
                self._price_last[sym] = last
            elif now_ms - last > step * PRICE_CLOCK_MAX_CATCHUP_STEPS:
                # Stalled/resumed process: skip the missed history. The 2H gap
                # recovery owns structural catch-up; this clock only acts on
                # fresh closes, never on a stale one.
                last = initial_price_clock_ts(
                    now_ms, step, int(pm_pos.opened_ts))
                self._price_last[sym] = last
            if now_ms < last + step:
                continue
            try:
                bars = self.client.history_candles(
                    sym, "15m", start_ms=last + 1, limit=100)
            except BitgetError as exc:
                self._journal("price_bar_error",
                              {"symbol": sym, "why": str(exc)[:160]})
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
                    price_only=True, skip_trail=True)
                if abs(float(pm_pos.invalid) - old) > 1e-12:
                    pos["invalid"] = float(pm_pos.invalid)
                    self._touch_campaigns()
                    self._journal("price_defense", {
                        "symbol": sym, "clock": "15m", "bar_ts": ts,
                        "old_invalid": old, "invalid": float(pm_pos.invalid),
                        "armed": bool(pm_pos.armed), "peak": pm_pos._best})
                for intent in intents:
                    if getattr(intent.kind, "value", None) == "EXIT":
                        reason = str(intent.reason)
                        # §8.1 is a close-based *decision*, not a resting stop
                        # touch: if the exchange TPSL is merely resting (mark
                        # never crossed), waiting for it would leave the
                        # position open. Lock giveback is likewise local.
                        executor = ("local_market"
                                    if ("lock_giveback" in reason
                                        or "profit|" in reason
                                        or "§8.1" in reason)
                                    else "exchange_stop")
                        ref_px = float(pos.get("mark") or bar[4])
                        self._sub_bar_close_out(
                            cs, pm_pos, pos, ts, ref_px,
                            why="price_bar_exit", route=reason,
                            executor=executor)
                        break
                    self._exec_mgmt(cs, intent, bar)
            self._price_last[sym] = last

    def _poll_position_prices(self, now_ms: int) -> None:
        """Fresh quotes ratchet an existing defense between 15m closes.

        Legacy 15m mode arms on closed bars. Realtime mode arms positive-frac
        touch rules on fresh quotes and leaves the half exit to the exchange.
        """
        if self.price_clock not in ("15m", "realtime") or not self.positions:
            return
        if not hasattr(self.client, "tickers"):
            return
        try:
            ticks = self.client.tickers()
        except BitgetError as exc:
            self._journal("price_poll_error", {"why": str(exc)[:160]})
            self._alert_throttled(
                "price_poll", "持仓实时行情获取失败，保留交易所保护单；"
                              "本轮不推进峰值")
            return
        received = max(now_ms, int(time.time() * 1000))
        max_age = int(float((self.cfg.get("protection") or {})
                            .get("max_quote_age_seconds", 30)) * 1000)
        for tick in ticks:
            sym = str(tick.get("symbol") or "")
            pos = self.positions.get(sym)
            if pos is None or sym in self._exit_retry:
                continue
            try:
                observed = int(tick.get("ts") or 0)
                last, mark = float(tick["lastPr"]), float(tick["markPrice"])
                if (observed <= self._price_seen.get(sym, 0)
                        or observed < int(pos.get("opened_ts") or 0)
                        or not 0 <= received - observed <= max_age
                        or not all(v == v and v > 0 for v in (last, mark))):
                    continue
            except (KeyError, ValueError, TypeError):
                continue
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
                    why="price_protective_stop",
                    route="PRICE_STOP|fresh mark", executor="exchange_stop")
                continue
            if self.price_clock != "realtime" and pos.get("price_source", "ref") == "ref":
                continue
            old, armed = float(pm_pos.invalid), bool(pm_pos.armed)
            ref = min(last, mark) if d > 0 else max(last, mark)
            if (self.price_clock == "realtime" and not pm_pos.pending_reduce
                    and not pm_pos.reduced and pm_pos.target_1 is not None
                    and cs.pm._resolve_lock_frac(pm_pos) > 0):
                # Submission failure fallback uses existing touch semantics;
                # with an exchange target pending this branch is forbidden.
                atr = float(cs.detector.structure.atr() or 0.0)
                quote = (observed, last, last, last, last, 0.0)
                for intent in cs.pm.on_bar(sym, quote, atr, price_only=True,
                                           skip_trail=True):
                    self._exec_mgmt(cs, intent, quote)
                pos["invalid"] = pm_pos.invalid
                self._touch_campaigns()
            if self.price_clock == "realtime" and pm_pos.target_1 is not None:
                frac = cs.pm._resolve_lock_frac(pm_pos)
                # Positive frac means touch; negative frac still requires its
                # original bar-close evidence. Never change strategy semantics.
                if frac > 0 and d * (last - pm_pos.target_1) >= 0:
                    pm_pos.armed = True
                    self._touch_campaigns()
            if (self.price_clock == "realtime" and pm_pos.target_1 is not None
                    and pm_pos.pending_reduce
                    and not pm_pos.reduced):
                # The exchange owns the pending 50% exit. Advancing a defense
                # must never consume the same partial exit locally a second time.
                pm_pos._best = (max(pm_pos._best, last) if d > 0
                                else min(pm_pos._best, last))
                lock = opp_lock_step(
                    direction=d, entry=pm_pos.entry, target=pm_pos.target_1,
                    frac=cs.pm._resolve_lock_frac(pm_pos), peak=pm_pos._best,
                    high=last, low=last, close=last, ref_px=ref,
                    armed=pm_pos.armed)
                if lock.action == "tighten":
                    pm_pos.invalid = (max(pm_pos.invalid, lock.stop_px) if d > 0
                                      else min(pm_pos.invalid, lock.stop_px))
                    pos["invalid"] = pm_pos.invalid
                    self._touch_campaigns()
                elif lock.action == "exit":
                    self._sub_bar_close_out(
                        cs, pm_pos, pos, observed, ref,
                        why="price_lock_exhausted", route="PRICE_LOCK|giveback_exhausted",
                        executor="local_market")
                continue
            intents = cs.pm.on_price(
                sym, ts=observed, price=last, ref_price=ref,
                atr=float(cs.detector.structure.atr() or 0.0))
            if float(pm_pos.invalid) != old or bool(pm_pos.armed) != armed:
                pos["invalid"] = float(pm_pos.invalid)
                self._touch_campaigns()
                self._journal("price_defense", {
                    "symbol": sym, "quote_ts": observed, "last": last,
                    "mark": mark, "clock": self.price_clock,
                    "old_invalid": old, "invalid": float(pm_pos.invalid),
                    "armed": bool(pm_pos.armed), "peak": pm_pos._best})
            quote_bar = (observed, last, last, last, last, 0.0)
            for intent in intents:
                if getattr(intent.kind, "value", None) == "EXIT":
                    self._sub_bar_close_out(
                        cs, pm_pos, pos, observed, ref,
                        why="price_lock_exhausted", route=intent.reason,
                        executor="local_market")
                else:
                    self._exec_mgmt(cs, intent, quote_bar)

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
                self.tg.send_alert(
                    f"{cs.symbol}({cs.tf_label}) 序列缺口 {cs.trig_bars[-1][0]} → "
                    f"{bar[0]};认知帧重装配(持仓事实保留)")
                sub_trig = None
                if base_ms == 1800 * 1000 and self.mode == "live" and hasattr(self.client, "history_candles"):
                    sub_want = max(520, (self._min_cog_bars_for_tf() + 5) * 8)
                    cand_30m = self._fetch_trigger_bars(cs.symbol, sub_want, gran="30m")
                    if self._base_step_seconds(cand_30m, gran="30m") == 1800:
                        sub_trig = cand_30m
                fresh = self._build_campaign(cs.symbol, cs.tf_label, trig_bars=sub_trig)
                if fresh is None and sub_trig is not None:
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
            # 与回放 campaign.run_campaigns 同构时序:若当前时刻恰逢大周期认知收盘,先闭合认知并冻结原子水平位地图,
            # 再推进下级触发收盘判定
            if (bar[0] + base_ms) % obs_ms == 0 and len(cs.trig_bars) >= scale:
                cog_bar = (bar if scale == 1
                           else aggregate_bucket(cs.trig_bars[-scale:],
                                                 bar[0] + base_ms - obs_ms))
                self._close_cognition(cs, cog_bar)
            if cs.tracker is not None and trig_ms < obs_ms:
                if (bar[0] + base_ms) % trig_ms == 0 and len(cs.trig_bars) >= trig_scale:
                    t_bar = (bar if trig_scale == 1
                             else aggregate_bucket(cs.trig_bars[-trig_scale:],
                                                   bar[0] + base_ms - trig_ms))
                    emitted = cs.tracker.update(t_bar)
                    if emitted:
                        view = cs.tracker.current_view(tf_hours=int(obs_ms // 3_600_000))
                        self._route_step(cs, cs.tracker.frozen_atr, emitted, view, mic=True)

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

    def _sub_bar_close_out(self, cs: CampaignState, pos_pm,
                           pos_dict: dict, ts: int, ref_px: float, *,
                           why: str, route: str, executor: str) -> None:
        """子周期触价离场的统一出口(paper 记账 / live 按**执行者**分流)。

        固定止损与"锁利回吐预算耗尽"都在子周期立刻离场,但**执行者不同**,
        这一步必须显式写清,而不是含混地"统一对账":

        * `executor="exchange_stop"` —— 触发条件是"价格触及某个既定价位",
          该价位上挂着交易所原生 TPSL ⇒ 触发即由交易所成交,本进程只对账
          (`_reconcile`)。此处**不得**再发市价单:仓位可能已经成交,再发一张
          就是反向开仓。前提是 `_stop_is_resting` 为真;保护单不在挂 ⇒ 无人
          执行,降级为 `local_market`(计数/告警),这是修前的裸奔盲区。
        * `executor="local_market"` —— 触发条件是**行情状态的函数**(例如
          "自峰值回吐 > 预算"),事前写不成一张挂单价 ⇒ 只能由本进程此刻发
          市价单(`_live_market_exit`)。2026-10-24 的事故正是把这一支当成
          "交易所已有计划单":日志写"⇒ 市价离场",仓却一直在。

        两条路径都要先把本地止损记账撤掉,否则下一拍 `_manage_stops` 会把
        已平的仓位重新挂单,形成幽灵保护单。
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
            return
        if self._gap_replay:
            # 补放缺口 bar:本进程当时不在场,计划单可能未触发/未挂出。
            # 先记账(不在此处平仓,避免与补放中的后续 bar 逻辑交错),
            # 补放结束后由 _gap_settle 按 stop_policy 一次性结算。
            self._gap_stop_hit[sym] = {"ts": ts, "stop": float(ref_px),
                                       "bar_close": float(ref_px)}
            return
        if executor == "exchange_stop" \
                and self._stop_is_resting(sym, float(pos_pm.invalid)):
            # 交易所是执行者:触价即刻触发对账以消除状态延迟
            self._reconcile()
            return
        if executor == "exchange_stop":
            # 防线被触及但交易所侧没有对应保护单:没有任何挂单会替我们离场。
            self.counters["unprotected_stop_exits"] = \
                self.counters.get("unprotected_stop_exits", 0) + 1
            self._journal("stop_unprotected_exit",
                          {"symbol": sym, "invalid": float(pos_pm.invalid),
                           "qty": pos_dict.get("qty"), "ref_px": float(ref_px),
                           "why": why})
            self._alert_throttled(
                f"unprot:{sym}",
                f"{sym} 防线 {float(pos_pm.invalid):.8g} 已触及但交易所无保护单在挂"
                f" → 本进程市价离场(修前该情形无任何动作)")
            why = f"{why}_unprotected"
        # 决策型:触发条件是行情状态的函数,只能本进程此刻市价离场
        self._live_market_exit(pos_dict, ts=ts, why=why, route=route,
                               ref_px=ref_px)

    def _live_market_exit(self, pos: dict, *, ts: int,
                          why: str, route: str, ref_px: float) -> bool:
        """决策型离场:由本进程**自己**发市价单(没有可指望的交易所挂单)。

        与 `_arm_close_intent`(意图池 + 限频 + 到期转市价)的区别:这是风控
        离场,必须一次到位 —— 不占订单配额、不进意图池、不留"挂而不成"的窗口。

        幂等由三处共同保证(缺一不可):
        ① 以 `self.positions` 的**当前事实**为准,本地已无此仓 ⇒ 绝不下单
          (否则"对已平的仓再平一次"就是反向开仓);
        ② `_exit_settle` 里已有该品种的待结回执 ⇒ 上一笔离场还在下单/结算中;
        ③ 成交即 `_live_close_filled` 清仓,后续 bar 的 `_on_sub_bar` 直接返回。

        记账顺序刻意如此:先下单(交易所事实),再取成交回执,最后记账。取不到
        回执时用参考价记账并登记 `_exit_settle`,由后续步校正 —— 绝不让
        "取不到成交价"变成"不发通知/不记账"。
        """
        sym = str(pos["symbol"])
        if sym in self._exit_settle:
            return False
        cur = self.positions.get(sym)
        if cur is None or float(cur.get("qty") or 0.0) <= 1e-9:
            return False
        pos = cur                        # 数量/入场价一律取本地最新事实
        spec = self._spec(sym)
        qty = floor_step(float(pos["qty"]), spec.size_step, spec.size_place)
        if qty <= 0.0:
            self.counters["exit_submit_errors"] = \
                self.counters.get("exit_submit_errors", 0) + 1
            self._journal("exit_submit_error",
                          {"symbol": sym,
                           "why": f"qty<=0 after floor_step({spec.size_step})"})
            self.tg.send_alert(f"{sym} 决策型离场数量按合约精度取整为 0"
                               f"(step={spec.size_step}) → 无法下单,人工核查")
            return False
        side = self.close_side_of(int(pos["direction"]))
        oid = client_oid_of(self.OID_PREFIX, sym,
                            str(pos.get("event_id") or "STOP"), side,
                            int(ts), role="exit")[:36]
        # 先撤入场/保护单:市价单与仍挂着的 TPSL 并存会双平(甚至反手)。
        # 撤单失败不阻断下单(TPSL 在仓位归零后自然失效)。
        self._cancel_entry(sym, why)
        self._cancel_stop(sym, why)
        it = {"oid": oid, "symbol": sym, "role": "exit", "kind": "market",
              "direction": -int(pos["direction"]), "qty": qty,
              "limit_price": None, "invalid": pos["invalid"],
              "target_1": pos.get("target_1"), "event_id": pos.get("event_id"),
              "route": f"exit:{str(why)[:32]}", "decision_ts": int(ts),
              "expiry_ts": int(ts) + N_FILL_H * MS_1H,
              "status": "armed", "exchange_order_id": None}
        try:
            resp = self.client.place_market_order(
                sym, side=side, size=qty, client_oid=oid,
                trade_side=self.close_trade_side())
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
                    "deadline_ms": req_ms + 180_000, "ref_px": float(ref_px),
                    "direction": int(pos["direction"]), "qty": float(qty),
                    "entry": float(pos["entry"]), "why": str(why)}
            it["price_source"] = px_src
            pnl = realized_pnl(int(pos["direction"]), qty, float(pos["entry"]),
                               px, self.risk["taker_fee"])
            self.counters["lock_exits"] = self.counters.get("lock_exits", 0) + 1
            self._exit_retry.pop(sym, None)     # 成功即出队(重试队列只服务失败)
            self._journal("exit_submit",
                          {"symbol": sym, "oid": oid,
                           "exchange_order_id": it["exchange_order_id"],
                           "qty": qty, "why": why, "ref_px": float(ref_px),
                           "px": px, "price_source": px_src,
                           "pnl": pnl, "route": route[:80]})
            self._live_close_filled(
                it, ref_price=px, pnl=pnl,
                title=exit_label(executor="local_market", pnl=pnl, reason=why),
                note=f"{why} · {route}",
                price_label=("成交价" if px_src == "fills"
                             else "参考价(下单时,回执待校正)"))
            return True
        except BitgetError as e:
            it["status"] = "error"
            self.counters["exit_submit_errors"] = \
                self.counters.get("exit_submit_errors", 0) + 1
            self._journal("exit_submit_error",
                          {"symbol": sym, "oid": oid, "why": str(e)[:160],
                           "why_action": why})
            tries = int((self._exit_retry.get(sym) or {}).get("attempts", 0))
            self._exit_retry[sym] = {"why": why, "route": route,
                                     "ref_px": float(ref_px),
                                     "attempts": tries}
            if tries == 0:
                # 首次失败立刻告警(限频 5 分钟:重试期间不再刷屏)
                self._alert_throttled(
                    f"exitfail:{sym}",
                    f"{sym} 决策型离场下单失败:{str(e)[:120]}"
                    f"(仓位仍在;保护单已撤,每步自动重试 ≤"
                    f"{self.MAX_EXIT_SUBMIT_RETRIES} 次,人工核查)", gap_s=300.0)
            self._api_note(e)          # 计数/熔断口径与其他下单路径一致
            return False

    def _retry_exits(self, now_ms: int) -> None:
        """决策型离场下单失败的**逐步重试**(不依赖可能过期的 mark)。

        为什么必须重试而不是等下一根 bar:锁利回吐预算已耗尽,防线已被证明
        守不住,等 2H 收盘才重试等于放任回撤扩大。重试由"当时的离场决策"驱动,
        不看 `mark` —— mark 在两次对账之间可能过期,用它判"是否还该离场"会
        造成无谓平仓或漏平。

        终态:`attempts` 用完仍失败 ⇒ 必须发一条**到人**的告警并出队(不再重试),
        因为此后该仓已无任何自动化保护。
        """
        for sym, rec in list(self._exit_retry.items()):
            pos = self.positions.get(sym)
            if pos is None:
                self._exit_retry.pop(sym, None)     # 已平(可能是别的路径平的)
                continue
            ref = float(pos.get("mark") or rec.get("ref_px") or 0.0)
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            ok = self._live_market_exit(
                pos, ts=int(now_ms), why=str(rec.get("why")),
                route=str(rec.get("route")), ref_px=ref or float(pos["entry"]))
            if ok:
                self._exit_retry.pop(sym, None)
                continue
            if int(rec["attempts"]) >= self.MAX_EXIT_SUBMIT_RETRIES:
                # 终态:重试用尽 ⇒ 立刻出队并发一条到人的告警(此刻该仓已无
                # 任何自动化保护,继续静默重试只会让损失扩大)
                self._exit_retry.pop(sym, None)
                self._journal("exit_submit_giveup",
                              {"symbol": sym, "attempts": rec.get("attempts"),
                               "why": rec.get("why")})
                self.tg.send_alert(
                    f"{sym} 决策型离场重试 {rec.get('attempts')} 次仍失败"
                    f"(原因 {rec.get('why')}) —— 已停止自动重试,仓位仍在,"
                    f"请立即人工平仓/核查")

    def _defense_title(self, pos_pm, last_log: str) -> str:
        """防线推进通知的**语义标题**(不能一律叫"止损推进")。

        修前:锁利把防线推到盈利侧时,通知仍写"止损推进" —— 用户看到的是
        "又在收紧止损",实际发生的是"已经锁住浮盈",两件事的后续处置完全
        不同(前者是风险,后者是收益兑现的路径)。这里按**推进的来源**命名,
        并把"已锁住多少 TP1 行程"算出来,可核对:

            locked = d·(invalid − entry) / |target_1 − entry|      # 盈利侧为正

        ≤0 表示防线仍在亏损侧(结构防线/收紧),不能说成"锁利"。
        """
        d = 1 if int(pos_pm.direction) > 0 else -1
        entry = float(pos_pm.entry)
        stop = float(pos_pm.invalid)
        if "opp_lock" in last_log:
            tgt = pos_pm.target_1
            span = abs(float(tgt) - entry) if tgt is not None else 0.0
            frac = (d * (stop - entry) / span) if span > 0 else 0.0
            return (f"锁利推进(已锁 {frac:.0%} TP1 行程)" if frac > 0
                    else "锁利推进(防线仍在成本侧)")
        if "partial_exit" in last_log:
            # 部分减仓把 invalid 推到 entry:这是"余仓保本",不是"继续收紧止损"
            return "减仓50% · 余仓保本防线"
        if "§8.3|trail" in last_log:
            return "结构防线推进(§8.3 trail)"
        if d * (stop - entry) > 0:
            return "防线推进(已进入盈利区)"
        return "防线收紧"

    def _on_sub_bar(self, cs: CampaignState, bar) -> None:
        """持仓期间子周期(2H)触价同步与结算:
        1. 固定 STOP(pos_pm.invalid):与回测 broker.execute_protective_stop 同口径,
           子周期极值触及(low<=stop / high>=stop)时:
           - paper 模式立即按止损价(或缺口开盘价)平仓结算并清理 pm/positions;
           - live 模式:执行者是交易所 TPSL ⇒ 触发一次 _reconcile() 同步成交;
             若该价位上**没有**在挂的保护单,则由本进程市价离场(不能等)。
        2. 固定 TP1(pos_pm.target_1):
           - 更新 pos_pm._best 实时 MFE(即锁利的峰值锚);
           - 当启用对侧锁利(reject_opp_lock_frac != 0)时,用与回放 on_bar 同一个
             裁决函数 `position.opp_lock_step` 推演:未 arming 保持结构防线 /
             保护侧推进(可多次棘轮)/ 回吐超预算则立刻市价离场。子周期只是
             **更早看到同一事实**,不是另一套语义 —— 2h 与 12h 采样得到同一
              防线公式相同；负 frac 的收盘启用条件随采样周期变化，不能据此
              声称 2h 与 12h 的启用时间或持仓路径等价。
        """
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

        # 实时维护子周期极值 MFE
        pos_pm._best = min(pos_pm._best, low) if pos_pm.direction < 0 else max(pos_pm._best, h)

        # ① 固定 STOP 盘中触价结算
        stop = float(pos_pm.invalid)
        breached = (low <= stop) if pos_pm.direction > 0 else (h >= stop)
        if breached:
            ref_px = min(o, stop) if pos_pm.direction > 0 else max(o, stop)
            self._sub_bar_close_out(
                cs, pos_pm, pos_dict, ts, ref_px,
                why="sub_bar_protective_stop",
                route=f"STOP|sub_bar trigger={stop:.8g}",
                executor="exchange_stop")
            return

        # ② 固定 TP1(target_1)盘中触价 ⇒ 对侧锁利 / 传统减仓
        #    唯一裁决入口 `position.opp_lock_step`(与回放 `PositionManager.on_bar`
        #    同一函数)。子周期只负责"更早看到同一事实",不引入第二套语义:
        #      未 arming ⇒ 保持结构防线;保护侧 ⇒ 推进(可多次棘轮);
        #      回吐超预算 ⇒ 立刻市价离场。三态穷尽,没有"拒单原地等"的裸奔态。
        if (self.price_clock == "bar"
                and pos_pm.target_1 is not None and not pos_pm.pending_reduce):
            target = float(pos_pm.target_1)
            # 与 canonical `PositionManager.on_bar` 共用**同一个** frac 解析器:
            # 策略自 2026-11-05 起按 ATR/entry 波动率分档(vol_based_frac_map),
            # 这里若仍读静态 reject_opp_lock_frac,子周期会拿旧 frac 推演另一条
            # 防线 —— 同一笔仓在两个时钟上得到不同的锁利预算。
            try:
                _atr_now = float(cs.detector.structure.atr() or 0.0)
            except (AttributeError, TypeError, ValueError):
                _atr_now = 0.0
            frac_cfg = float(cs.pm._resolve_lock_frac(pos_pm, _atr_now))
            old_inv = float(pos_pm.invalid)
            # 换单参考价:本地收盘 c 与已知 mark 中**更保守**的一侧(空头取较高价、
            # 多头取较低价),避免换单瞬间现价已越过止损/锁利价。
            ref_px = c
            _mark = float(pos_dict.get("mark") or 0.0)
            if _mark > 0.0:
                ref_px = max(c, _mark) if pos_pm.direction < 0 else min(c, _mark)
            if frac_cfg == 0.0:
                # 传统 §8.2 target_1 触及减仓 50%(仅一次;仅未启用锁利时)
                hit = (low <= target) if pos_pm.direction < 0 else (h >= target)
                if hit and not pos_pm.reduced:
                    extreme = low if pos_pm.direction < 0 else h
                    self._exec_mgmt(cs, cs.pm._reduce(pos_pm, ts, c, extreme), bar)
            else:
                # 主张完成 = 价格到达对侧,这是**事实**,记下来;但风险重定基
                # 需要"接受"(收盘越过),否则一次插针就会把整段行程的持有权改掉。
                # 归因实验:触价即重定基会把趋势右尾剪掉(净 −19.4k、回撤 +12pp)。
                reached = (low <= target) if pos_pm.direction < 0 else (h >= target)
                if (reached
                        and pos_pm.direction * (target - pos_pm.entry) > 0.0
                        and getattr(pos_pm, "claim_completed_ts", None) is None):
                    pos_pm.claim_completed_ts = ts
                    pos_pm.log.append(
                        f"claim|completed|target={target:.8g} reached at {ts};"
                        f" recorded as fact only (risk re-base requires acceptance close)")
                ld = opp_lock_step(
                    direction=pos_pm.direction, entry=pos_pm.entry, target=target,
                    frac=frac_cfg, peak=pos_pm._best, high=h, low=low, close=c,
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
                    # 执行者必须是本进程:这个触发条件是"自峰值回撤 > 预算",
                    # 随行情连续变化,事前无法写成一张挂单价。
                    pos_pm.armed = True          # 事实:锁利已 arming(审计要一致)
                    if (getattr(cs.pm, "partial_exit_enabled", False)
                            and not pos_pm.reduced):
                        # 与 canonical `PositionManager.on_bar` 同语义:预算耗尽
                        # 先兑现一半并保本(一次性额度),余仓继续按棘轮/结构防线。
                        pos_pm.log.append(
                            f"v21|sub_bar_partial_exit|峰值={pos_pm._best:.8g} "
                            f"回吐={ld.drawdown:.8g} ≥ 预算={ld.budget:.8g} "
                            f"ref={ref_px:.8g} ⇒ 减仓50%并保本(余仓交棘轮)")
                        intent = cs.pm._reduce(pos_pm, ts, c, ld.drawdown)
                        pos_dict["invalid"] = float(pos_pm.invalid)
                        self._touch_campaigns()
                        self._exec_mgmt(cs, intent, bar)
                        return
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
                               f"budget={ld.budget:.8g} ref={ref_px:.8g}"),
                        executor="local_market")
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
                        if (getattr(cs.pm, "partial_exit_enabled", False)
                                and not pos_pm.reduced):
                            # 武装即兑现一半并移保本(canonical `partial_exit_on_arm`)
                            pos_pm.log.append(
                                f"v21|sub_bar_partial_exit_on_arm|"
                                f"target={target:.8g} ⇒ 减仓50%并移至保本,"
                                f"余仓交由棘轮/结构防线")
                            intent = cs.pm._reduce(pos_pm, ts, c, pos_pm._best)
                            pos_dict["invalid"] = float(pos_pm.invalid)
                            self._touch_campaigns()
                            self._exec_mgmt(cs, intent, bar)
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
                    sym, f"{self._defense_title(pos_pm, last_log)}({self.mode})",
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
                    box_start_ts=pos_dict.get("box_start_ts"),
                    bars=cs.trig_bars[-96:] if cs.trig_bars else cs.cog_bars[-96:],
                )

    def _close_cognition(self, cs: CampaignState, cog_bar) -> None:
        s = cs.detector.structure
        n_pivots_before = len(s.pivots)
        emitted = cs.detector.update(cog_bar)
        new_pivots = list(s.pivots[n_pivots_before:])
        base_s = (
            int(cs.trig_bars[1][0] - cs.trig_bars[0][0]) // 1000
            if len(cs.trig_bars) >= 2 and int(cs.trig_bars[1][0]) > int(cs.trig_bars[0][0])
            else 7200
        )
        obs_s = max(base_s, int(cs.obs_tf_seconds))
        if cs.tracker is not None:
            cs.tracker.sync_from_structure(s)
        cs.cog_bars.append(cog_bar)
        atr = s.atr()
        sym = cs.symbol
        # §8 持仓管理:若仓位在本根认知 bar 内成交(opened_ts > cog_bar[0]),
        # 取成交后已收盘子周期区间极值推进价格型保护,消除建仓当根大周期裸奔盲区。
        pos_pm = cs.pm.positions.get(sym)
        if pos_pm is not None:
            old_inv = float(pos_pm.invalid)
            if pos_pm.opened_ts > cog_bar[0]:
                scale_n = max(1, obs_s // base_s)
                sub_bars = [
                    b for b in cs.trig_bars[-scale_n:]
                    if int(b[0]) >= int(pos_pm.opened_ts)
                ]
                if sub_bars:
                    sub_cog = (
                        cog_bar[0],
                        sub_bars[0][1],
                        max(b[2] for b in sub_bars),
                        min(b[3] for b in sub_bars),
                        sub_bars[-1][4],
                        sum(b[5] for b in sub_bars),
                    )
                    mgmt = cs.pm.on_bar(sym, sub_cog, atr, (), [], price_only=True)
                else:
                    mgmt = []
            else:
                mgmt = cs.pm.on_bar(sym, cog_bar, atr, new_pivots, emitted or [])
            if abs(float(pos_pm.invalid) - old_inv) > 1e-12:
                pos_dict = self.positions.get(sym)
                self._touch_campaigns()      # 防线推进:冷档必须留住
                if pos_dict is not None:
                    pos_dict["invalid"] = float(pos_pm.invalid)
                last_log = pos_pm.log[-1] if pos_pm.log else "防线收紧"
                self.tg.send_fill(
                    sym, f"{self._defense_title(pos_pm, last_log)}({self.mode})",
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
                    box_start_ts=pos_dict.get("box_start_ts") if pos_dict else None,
                    pivots=self._chart_pivots(s.pivots[-6:]),
                    atr=atr,
                    bars=cs.cog_bars[-96:] if cs.cog_bars else cs.trig_bars[-96:],
                )
            for intent in mgmt:
                self._exec_mgmt(cs, intent, cog_bar)
        if emitted:
            route_events = select_cognition_route_events(
                emitted, has_trigger_tracker=(cs.tracker is not None)
            )
            if route_events:
                view = StructureView(
                    regime=s.regime(), zones=s.levels, atr=atr,
                    pivots=s.pivots[-self.params.range_max_pivots:])
                self._route_step(cs, atr, route_events, view, mic=False)

    def _route_step(self, cs: CampaignState, atr: float, events: list, view,
                    *, mic: bool) -> None:
        """与 campaign.py 完全同源的路由决策入口(MarketRouter → 意图)。"""
        sym = cs.symbol
        if mic:
            bars, bar = cs.trig_bars, cs.trig_bars[-1]
        else:
            bars, bar = cs.cog_bars, cs.cog_bars[-1]
        htf_views = ()
        r_params = getattr(cs.router, "params", None)
        if getattr(r_params, "htf_wall_check", True):
            all_cs = [self.campaigns.get(sym)] + [
                sub for (s2, _), sub in self.extra_campaigns.items() if s2 == sym
            ]
            htf_list = []
            for ocs in all_cs:
                if (
                    ocs is None
                    or ocs.obs_tf_seconds < 2 * cs.obs_tf_seconds
                    or not ocs.cog_bars
                ):
                    continue
                os_st = ocs.detector.structure
                htf_list.append(StructureView(
                    regime=os_st.regime(),
                    zones=os_st.levels,
                    atr=os_st.atr(),
                    pivots=(),
                    macro_imbalance=os_st.macro_imbalance(),
                    tf_hours=int(ocs.obs_tf_seconds // 3600),
                ))
            htf_views = tuple(htf_list)
        peer_cs = [
            c_item
            for c_item in (*self.campaigns.values(), *self.extra_campaigns.values())
            if c_item is not None and c_item.obs_tf_seconds == cs.obs_tf_seconds and c_item.cog_bars
        ]
        peer_regs = [c_item.detector.structure.regime() for c_item in peer_cs]
        peer_dvs = sorted(c_item.detector.structure.dollar_volume() for c_item in peer_cs)
        med_dv = float(peer_dvs[len(peer_dvs) // 2]) if peer_dvs else 0.0
        n_univ = len(peer_regs)
        m_breadth = (
            float(sum(1 for r in peer_regs if r.value == "trend_up") - sum(1 for r in peer_regs if r.value == "trend_down")) / float(n_univ)
            if n_univ >= 4 else 0.0
        )
        existing_pos = self.positions.get(sym)
        existing_tf_s = (
            TF_SECONDS_MAP.get(str(existing_pos.get("tf_label") or ""), 0)
            if existing_pos is not None else 0
        )
        blocked_by_same_or_htf = existing_pos is not None and existing_tf_s >= int(cs.obs_tf_seconds)
        decisions = cs.router.on_bar(
            sym, bar, len(bars) - 1, view, atr, events,
            position_open=blocked_by_same_or_htf,
            entry_resting=self._entry_resting(sym),
            htf_views=htf_views,
            market_breadth=m_breadth,
            universe_size=n_univ,
            median_dollar_volume=med_dv)
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
                # 实盘仓位升格(Scale Promotion):若同标的持有更小周期的旧仓位,先平仓落袋再无缝开立更高周期新仓位!
                cur_pos = self.positions.get(sym)
                if cur_pos is not None and TF_SECONDS_MAP.get(str(cur_pos.get("tf_label") or ""), 0) < int(cs.obs_tf_seconds):
                    low_cs = self._get_cs_for(sym, cur_pos.get("tf_label"))
                    low_pm = low_cs.pm.positions.get(sym) if low_cs is not None else None
                    if low_cs is not None and low_pm is not None:
                        self._sub_bar_close_out(
                            low_cs, low_pm, cur_pos, t, float(bar[4]),
                            why="scale_promoted",
                            route=f"SCALE_PROMOTED|to={cs.tf_label}",
                            executor="local_market",
                        )
                        self.counters["scale_promotions"] = self.counters.get("scale_promotions", 0) + 1
                self._submit_open(cs, d, t, mic=mic, view=view)
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

    @staticmethod
    def _box_bounds(zone_lo, zone_hi, box_opposite):
        """平衡箱体上下沿:事件区间 + 对侧近缘的极值;区间缺失则整体不画。"""
        if zone_lo is None or zone_hi is None:
            return None, None
        pts = [float(zone_lo), float(zone_hi)]
        if box_opposite is not None:
            pts.append(float(box_opposite))
        return min(pts), max(pts)

    @staticmethod
    def _resolve_box_start_ts(cs: CampaignState, meta: Optional[dict]) -> Optional[int]:
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
    def _signal_chart_bars(cs: CampaignState, mic: bool):
        """信号图取哪个帧,并说明该帧是否为认知帧。

        pivot 的 `known_index` 是认知帧坐标:只有画认知帧时下标才对得上,
        画触发帧时若硬标就会整体错位(标注错位比不标更有害)。
        """
        if mic and cs.trig_bars:
            return cs.trig_bars[-96:], False
        cog = cs.cog_bars[-96:]
        if cog:
            return cog, True
        return cs.trig_bars[-96:], False

    @staticmethod
    def _chart_pivots(pivots) -> list:
        """图上的结构端点:`[(窗口下标, 价格, "high"|"low")]`。

        `pivots` 传 `StructureView.pivots`(认知帧坐标):下标按 `known_index`
        折算到末 96 根;落在窗口外的丢掉,None 由渲染器自行忽略。
        """
        out: list = []
        win = 96
        for p in list(pivots or ())[-6:]:
            idx = int(getattr(p, "known_index", -1))
            if idx < 0:
                continue
            kind = getattr(p, "kind", "high")
            out.append((win - 1 - idx if idx < win else None,
                        float(p.price), getattr(kind, "value", str(kind))))
        return out

    def _signal_status_text(self, block: Optional[str], *, tf_label: str,
                            kind: str, watch_entry: bool = False) -> str:
        """把闸门结果翻成一句对外口径(公开频道不得出现账户数字)。"""
        if block == "TF_CAP":
            return f"仅参考 · {tf_label} 周期容量已满,本系统本拍未开仓"
        if block is not None:
            return self._BLOCK_STATUS.get(block, f"仅参考 · {block}")
        if watch_entry:
            return "计划进入系统价格关注（尚未下单；触发后再检查并提交市价单）"
        else:
            act = "提交限价单" if kind == "limit" else "提交市价开仓单"
        return f"本系统计划{act}（非成交确认，成交以运营通知及交易所回执为准）"

    def _note_block(self, sym: str, cs: CampaignState, hyp, block: str, *,
                    qty: float, ref: float) -> None:
        """闸门记账:计数器与日志字段与解耦前逐条一致。"""
        for name, inc in self._BLOCK_COUNTERS.get(block, ()):
            self.counters[name] = self.counters.get(name, 0) + inc
        rec = {"symbol": sym, "why": block, "event": hyp.event_id}
        if block == "TF_CAP":
            rec["tf"] = cs.tf_label
        if block == "MIN_NOTIONAL":
            rec.update({"qty": qty, "ref": ref})
        self._journal("reject" if block == "degenerate_risk" else "skip", rec)

    def _notify_signal(self, key: tuple, *, size_note: str, exec_status: str,
                       **payload) -> None:
        """信号广播:同一事件只推一条,且永不 raise。

        与容量/熔断/下单结果无关(见 `_submit_open` 文档)。渲染与 HTTP 都在
        通知器工作线程里跑,主循环只做入队;任何异常只记
        `signal_notice_errors` + 日志,绝不反噬决策链。
        """
        if key in self._signal_sent:
            return
        self._signal_sent[key] = 1
        while len(self._signal_sent) > 2048:        # 只做窗口内去重
            self._signal_sent.pop(next(iter(self._signal_sent)))
        fn = getattr(self.tg, "send_signal", None)
        if fn is None:
            return
        try:
            # Keep the existing signal-specific error counter as well as the
            # general guard used for fills/alerts elsewhere in the runner.
            target = self.tg._notifier if isinstance(self.tg, NotificationGuard) else self.tg
            target.send_signal(size_note=size_note, exec_status=exec_status, **payload)
        except Exception as e:                      # noqa: BLE001 通知不反噬
            self.counters["signal_notice_errors"] = \
                self.counters.get("signal_notice_errors", 0) + 1
            try:
                self._journal("signal_notice_error", {
                    "symbol": payload.get("symbol"),
                    "event": payload.get("event_id"),
                    "why": f"{type(e).__name__}: {str(e)[:120]}"})
            except Exception:
                pass

    # 闸门 → 对外口径:信号频道只看到确定性文案,不含账户数字。
    # 空串表示按周期名动态拼装(见 _signal_status_text)。
    _BLOCK_STATUS = {
        "API_HALT": "仅参考 · API 熔断中,本系统暂停新开仓",
        "degenerate_risk": "仅参考 · 参考价已越过失效位(风险距离为 0)",
        "TF_CAP": "",
        "PORTFOLIO_CAP": "仅参考 · 组合容量已满,本系统本拍未开仓",
        "OPPOSITE_CLAIM": "仅参考 · 同标的已有反向持仓,本系统本拍未开仓",
        "CLAIM_CAP": "仅参考 · 该标的同向判断的风险额度已用完,本系统本拍未开仓",
        "DIRECTIONAL_CAP": "仅参考 · 同向风险额度已用完,本系统本拍未开仓",
        "MIN_NOTIONAL": "仅参考 · 数量低于交易所最小下单,本系统本拍未开仓",
        "DAILY_LOSS_BREAKER": "仅参考 · 当日亏损熔断,本系统暂停新开仓",
        "RATE_LIMIT": "仅参考 · 触达下单频率上限,本系统本拍未开仓",
        "DUPLICATE": "仅参考 · 同一信号已处理,本次为重复回放",
    }
    # 闸门 → 计数器增量(与解耦前逐条对齐,历史 counters 仍可直读)
    _BLOCK_COUNTERS = {
        "API_HALT": (("api_halt_skips", 1),),
        "degenerate_risk": (),
        "TF_CAP": (("tf_cap_skips", 1), ("cap_skips", 1)),
        "PORTFOLIO_CAP": (("cap_skips", 1),),
        "OPPOSITE_CLAIM": (("cap_skips", 1),),
        "CLAIM_CAP": (("cap_skips", 1),),
        "DIRECTIONAL_CAP": (("cap_skips", 1),),
        "MIN_NOTIONAL": (("min_notional_skips", 1),),
        "DAILY_LOSS_BREAKER": (("breaker_blocks", 1),),
        "RATE_LIMIT": (("rate_skips", 1),),
        "DUPLICATE": (),
    }

    def _position_worst_case(self, p: dict, fee_rate: float) -> float:
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

    def _directional_entry_blocked(self, direction: int, qty: float, price: float,
                                   invalid: float, *, exclude_oid: Optional[str] = None) -> bool:
        """Derive same-side in-flight exposure; no reservation state to leak.

        Live admission already uses full costs. Keep that unit at trigger/fill
        checks too, and exclude the intent currently being executed.
        """
        frac = self.directional_cap_frac
        if frac is None or self.equity is None:
            return False
        capital = max(0.0, float(self.equity) - float(self.unrealized_pl or 0.0))
        fee, slip = float(self.risk["taker_fee"]), float(self.risk["slippage_per_side"])
        used = sum(self._position_worst_case(p, fee + slip)
                   for p in self.positions.values() if p["direction"] == direction)
        if getattr(self, "capacity_mode", "") == "cash_net_admission":
            opp_filled = sum(self._position_worst_case(p, fee + slip)
                             for p in self.positions.values() if p["direction"] == -direction)
            used = max(0.0, used - opp_filled)
        for oid, it in self.open_intents.items():
            if (oid == exclude_oid or it.get("status") != "armed"
                    or it.get("role") != "entry" or it["direction"] != direction
                    or it["symbol"] in self.positions):
                continue
            ref = float(it.get("limit_price") or it["ref_px"])
            used += float(it["qty"]) * (abs(ref - float(it["invalid"])) + ref * 2.0 * (fee + slip))
        candidate = qty * (abs(price - invalid) + price * 2.0 * (fee + slip))
        return used + candidate > frac * capital + 1e-9

    def _submit_open(self, cs: CampaignState, d, t: int, *, mic: bool,
                     view=None) -> None:
        """意图 → 闸门 → 信号广播 → 装配(执行在撮合/live 下单层)。

        信号广播与容量/熔断/下单结果**完全解耦**:检测到事件就推送结构
        事实,再用 `exec_status` 说明本系统是否真的下手。解耦前闸门 `return`
        会连带吞掉通知 —— 容量一满,ACCEPT/REJECT 在信号频道彻底不可见。
        闸门的计数器/日志语义保持不变(见 `_BLOCK_COUNTERS` / `_note_block`)。
        """
        sym = cs.symbol
        hyp = d.hyp
        # hedge_mode:side 表持仓方向(开多 buy / 开空 sell)+ tradeSide=open
        side = "buy" if hyp.direction > 0 else "sell"
        entry_ref = float(d.limit_price) if d.kind == "limit" \
            else float(d.intent.ref_price)
        invalid = float(hyp.invalid)
        risk_per_unit = abs(entry_ref - invalid)
        spec = self._spec(sym)
        try:
            cost_limited = not float(d.meta.get("max_cost_frac") or 0.0) <= 0.0
        except (TypeError, ValueError):
            cost_limited = True  # malformed explicit constraints fail closed below
        fitted_entry = (self.fit_price(sym, entry_ref, hyp.direction)
                        if self.mode == "live" and d.kind == "limit"
                        and not d.meta.get("watch_entry") else entry_ref)
        size_mult = float(d.meta.get("size_mult") or 1.0)
        # 事件 × 方向期望值权重(只缩不放;代码内下限 10%,见函数 docstring)
        edge_mult = edge_size_multiplier(
            self.risk.get("edge_size_by_event_direction"),
            hyp.event_id, hyp.direction)
        size_mult *= edge_mult
        equity = float(self.equity)
        capital = max(0.0, equity - float(self.unrealized_pl or 0.0))
        eff_rf = self.current_risk_frac(equity)
        # ---- 与容量无关的结构事实:一次算清,信号与执行共用同一份 ----
        regime = hyp.regime.value if hyp.regime is not None else ""
        zone_lo = d.meta.get("zone_lo")
        zone_hi = d.meta.get("zone_hi")
        box_lo, box_hi = self._box_bounds(zone_lo, zone_hi,
                                          d.meta.get("box_opposite"))
        box_start_ts = self._resolve_box_start_ts(cs, d.meta)
        chart_bars, on_cog = self._signal_chart_bars(cs, mic)
        period_s = (getattr(cs, "trigger_tf_seconds", 0) if mic else
                    getattr(cs, "obs_tf_seconds", TF_SECONDS_MAP.get(cs.tf_label, 0)))
        confirm_ts = d.meta.get("confirm_ts") or (t + int(period_s) * 1000)
        snapshot_px, snapshot_ts, execution_quote = None, None, None
        if self.mode == "live":
            try:
                now_s = time.time()
                cache = getattr(self, "_signal_quote_cache", None)
                if cache is None or now_s - cache[0] >= 15.0:
                    cache = (now_s, self.client.tickers() or [])
                    self._signal_quote_cache = cache
                for row in cache[1]:
                    if row.get("symbol") == sym:
                        snapshot_px = float(row.get("lastPr") or 0.0) or None
                        snapshot_ts = int(cache[0] * 1000)
                        execution_quote = float(row.get("askPr" if hyp.direction > 0 else "bidPr") or 0.0) or None
                        break
            except Exception:
                pass  # Missing quote is explicit; notification must not invent one.
        chart_pivots = self._chart_pivots(getattr(view, "pivots", ())) \
            if on_cog else []
        # ---- 闸门:只产出 block 码,不决定要不要通知 ----
        block: Optional[str] = entry_cost_error(hyp.direction, fitted_entry, invalid, d.meta)
        qty = 0.0
        if self.mode == "live" and self.api_halted:
            block = "API_HALT"
        elif risk_per_unit <= 0.0:
            block = "degenerate_risk"
        elif block is not None:
            pass
        elif self.mode == "live" and d.kind == "limit" and cost_limited:
            block = entry_contract_error(hyp.direction, fitted_entry, invalid,
                                         hyp.target_1, d.meta)
        elif self.mode == "live" and d.kind == "market" and (d.meta.get("bounded_entry") or cost_limited):
            block = (entry_contract_error(hyp.direction, execution_quote, invalid,
                     hyp.target_1, d.meta, at_execution=True) if execution_quote is not None else "EXECUTION_QUOTE_UNAVAILABLE")
            if block is None:
                # Size against the adverse quote as well as the decision price.
                risk_per_unit = max(risk_per_unit, abs(execution_quote - invalid))
        if block is None:
            # 定仓用"风险资本"(已落袋),杠杆上限仍用交易所权益(偿付能力)
            qty = eff_rf * size_mult * capital / risk_per_unit
            # 最大允许杠杆上限 <= min(leverage_cap, maxLever)
            eff_lev = float(self._max_allowed_leverage(sym))
            qty = min(qty, eff_lev * equity / entry_ref)
            qty = floor_step(qty, spec.size_step, spec.size_place)
            # 组合容量、周期独立容量与主张容量(剩余额度 >= 25% 时动态缩量分配,否则整笔跳过)
            outstanding = sum(
                self._position_worst_case(p, self.risk["taker_fee"])
                for p in self.positions.values())
            unit_exp = risk_per_unit + entry_ref * 2.0 * (
                self.risk["taker_fee"] + self.risk["slippage_per_side"])
            candidate = qty * unit_exp
            tf_caps = self.risk.get("tf_cap_by_tf") or {}
            tf_cap = tf_caps.get(cs.tf_label)
            tf_out = 0.0
            group = 0.0
            if tf_cap is not None:
                tf_out = sum(
                    self._position_worst_case(p, self.risk["taker_fee"])
                    for p in self.positions.values()
                    if p.get("tf_label", "12h") == cs.tf_label)
                group = sum(
                    self._position_worst_case(p, self.risk["taker_fee"])
                    for key, p in self.positions.items()
                    if key.split("@")[0] == sym and p["direction"] == hyp.direction)
            opposite_open = any(
                p["direction"] == -hyp.direction
                for key, p in self.positions.items()
                if key.split("@")[0] == sym)
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
                        qty_scaled = eff_rf * new_mult * capital / risk_per_unit
                        qty_scaled = min(qty_scaled, eff_lev * equity / entry_ref)
                        qty_scaled = floor_step(qty_scaled, spec.size_step, spec.size_place)
                        min_notional = max(spec.min_trade_usdt, spec.size_min * entry_ref)
                        if qty_scaled > 0.0 and qty_scaled * entry_ref >= min_notional:
                            size_mult = new_mult
                            d.meta["size_mult"] = size_mult
                            qty = qty_scaled
                            candidate = qty * unit_exp
            if tf_cap is not None and tf_out + candidate > float(tf_cap) * capital + 1e-9:
                block = "TF_CAP"
            if block is None and outstanding + candidate > \
                    self.risk["portfolio_cap_frac"] * capital + 1e-9:
                block = "PORTFOLIO_CAP"
            # 同一标的不能同时持两个相反主张(净敞口为零却双倍成本/双倍风险)
            if block is None and opposite_open:
                block = "OPPOSITE_CLAIM"
            # 主张预算:同标的同方向的全部在仓(跨周期)共用一份,周期不叠加
            if block is None and tf_cap is not None and group + candidate > float(tf_cap) * capital + 1e-9:
                block = "CLAIM_CAP"
            if block is None and qty * entry_ref < max(
                    spec.min_trade_usdt, spec.size_min * entry_ref):
                block = "MIN_NOTIONAL"
            if block is None and self._directional_entry_blocked(
                    hyp.direction, qty,
                    (execution_quote if self.mode == "live" and d.kind == "market"
                     and execution_quote is not None else fitted_entry), invalid):
                block = "DIRECTIONAL_CAP"
            if block is None and self.breaker.should_stop(equity):
                block = "DAILY_LOSS_BREAKER"
            if block is None and not self.rate.allow():
                block = "RATE_LIMIT"
        oid = None
        if block is None:
            oid = client_oid_of(self.OID_PREFIX, sym, hyp.event_id, side, t)
            # 断点幂等:同键不重发;已失败的意图亦不再重试(避免每轮重发同一坏单)
            if self.open_intents.get(oid, {}).get("status") in (
                    "armed", "filled", "error"):
                block = "DUPLICATE"
        is_watch = bool(d.meta.get("watch_entry"))
        wait_h = int(d.meta.get("max_wait_hours") or N_FILL_H) if is_watch else N_FILL_H
        watch_details = None
        if is_watch:
            try:
                prog = d.meta.get("prog_price")
                if (prog is None and d.meta.get("max_prog_frac") is not None
                        and hyp.target_1 is not None and d.meta.get("confirm_close") is not None):
                    close = float(d.meta["confirm_close"])
                    prog = close + hyp.direction * float(d.meta["max_prog_frac"]) * abs(float(hyp.target_1) - close)
                watch_details = {"watch_price": entry_ref, "near_price": d.meta.get("near_price"),
                                 "prog_price": prog, "max_wait_hours": wait_h}
            except Exception:
                watch_details = {"watch_price": entry_ref, "max_wait_hours": wait_h}
        # ---- 信号广播(公开频道):先发事实,再记账 ----
        sig_size_mult = float(d.meta.get("size_mult", 1.0) or 1.0)
        sig_rf = eff_rf * sig_size_mult * edge_mult
        sig_stop_dist = abs(entry_ref - invalid) / entry_ref if entry_ref > 0.0 else 0.0
        sig_notional = (
            (qty * entry_ref)
            if (block is None and qty > 0.0)
            else ((sig_rf * capital / sig_stop_dist) if sig_stop_dist > 0.0 else 0.0)
        )
        sig_cand_risk = (
            candidate
            if (block is None and qty > 0.0)
            else (sig_rf * capital)
        )
        planned_lev = self._plan_trade_leverage(
            sym, sig_notional, equity, capital, sig_cand_risk
        )
        # 结合止损幅度限制安全逐仓杠杆(止损距离不超过保证金的 75%)
        if sig_stop_dist > 0.0:
            planned_lev = max(1, min(planned_lev, int(max(1, 0.75 / sig_stop_dist))))
        sig_margin_pct = (
            (sig_notional / (equity * float(planned_lev))) * 100.0
            if (equity > 0.0 and planned_lev > 0 and sig_notional > 0.0)
            else None
        )
        self._notify_signal(
            (sym, cs.tf_label, hyp.event_id, t, hyp.direction, d.route),
            size_note="降档触发" if mic else "本周期触发",
            exec_status=self._signal_status_text(block, tf_label=cs.tf_label,
                                                kind=d.kind, watch_entry=is_watch),
            symbol=sym, tf_label=cs.tf_label, event_id=hyp.event_id,
            side=side, route=d.route,
            ref_price=entry_ref, invalid=invalid, target_1=hyp.target_1,
            regime=regime, bars=chart_bars, box_lo=box_lo, box_hi=box_hi,
            zone_lo=zone_lo, zone_hi=zone_hi,
            zone_touches=d.meta.get("zone_touches"),
            pivots=chart_pivots, atr=getattr(view, "atr", None),
            decision_ts=confirm_ts, watch_details=watch_details,
            price_context={**d.meta, "box_start_ts": box_start_ts,
                           "snapshot_price": snapshot_px,
                           "snapshot_ts": snapshot_ts, "snapshot_source": "lastPr",
                           "suggested_leverage": planned_lev,
                           "suggested_margin_pct": sig_margin_pct,
                           "suggested_risk_pct": round(sig_rf * 100.0, 2),
                           "max_allowed_leverage": self._max_allowed_leverage(sym)})
        self._journal("signal_structure", {
            "symbol": sym, "tf": cs.tf_label, "event": hyp.event_id,
            "confirm_ts": confirm_ts, "entry_ref": entry_ref,
            "invalid": invalid, "target_1": hyp.target_1,
            "box_lo": box_lo, "box_hi": box_hi,
            "box_start_ts": box_start_ts,
            "snapshot_price": snapshot_px, "snapshot_ts": snapshot_ts,
            "exec_block": block, **d.meta})
        if block is not None:
            self._note_block(sym, cs, hyp, block, qty=qty, ref=entry_ref)
            return
        self.counters["signals"] += 1
        intent = {"oid": oid, "symbol": sym, "tf_label": cs.tf_label,
                  "role": "entry", "kind": d.kind, "direction": hyp.direction,
                  "qty": qty, "leverage": planned_lev,
                  "limit_price": entry_ref if d.kind == "limit" else None,
                  "ref_px": float(entry_ref),
                  "invalid": float(hyp.invalid),
                  "target_1": hyp.target_1, "event_id": hyp.event_id,
                  "structural_target": hyp.target_1,
                  "execution_target": execution_target(d.meta, hyp.target_1),
                  "route": d.route, "size_mult": size_mult,
                  "edge_mult": edge_mult,
                  "box_lo": box_lo, "box_hi": box_hi,
                  "box_start_ts": box_start_ts,
                  "bounded_entry": bool(d.meta.get("bounded_entry")),
                  "entry_min_rr": float(d.meta.get("entry_min_rr", 0.0)),
                  "inner_edge": d.meta.get("inner_edge"),
                  "max_box_progress_frac": d.meta.get("max_box_progress_frac"),
                  "watch_entry": is_watch,
                  "watch_price": float(entry_ref) if is_watch else None,
                  "near_price": (float(d.meta["near_price"])
                                 if d.meta.get("near_price") is not None else None),
                  "prog_price": (float(d.meta["prog_price"])
                                 if d.meta.get("prog_price") is not None else None),
                  "confirm_close": (float(d.meta["confirm_close"])
                                    if d.meta.get("confirm_close") is not None else None),
                  "max_prog_frac": (float(d.meta["max_prog_frac"])
                                    if d.meta.get("max_prog_frac") is not None else None),
                  "max_wait_hours": wait_h,
                  "near_touched": False,
                  "near_extreme": None,
                  "risk_usdt_cap": float(qty * risk_per_unit),
                  "decision_ts": t, "mic": mic,
                  "expiry_ts": confirm_ts + wait_h * MS_1H, "status": "armed",
                  "exchange_order_id": None}
        if cost_limited:
            intent.update(max_cost_frac=d.meta["max_cost_frac"],
                          cost_per_side=d.meta["cost_per_side"])
        self.open_intents[oid] = intent
        if self.mode == "live":
            try:
                if is_watch:
                    # 系统价格关注态:不在交易所挂死板限价单,由 15s 主循环监控回踩触发/走完作废
                    self.counters["watch_armed"] = self.counters.get("watch_armed", 0) + 1
                else:
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
                            sym, side=side, size=qty,
                            price=fitted_entry,
                            client_oid=oid, trade_side=self.open_trade_side(),
                            **self._entry_protection(sym, intent))
                        intent["exchange_order_id"] = str(resp.get("orderId") or "")
                        self.counters["orders_placed"] += 1
                    else:
                        resp = self.client.place_market_order(
                            sym, side=side, size=qty, client_oid=oid,
                            trade_side=self.open_trade_side(),
                            **self._entry_protection(sym, intent))
                        intent["exchange_order_id"] = str(resp.get("orderId") or "")
                        self.counters["orders_placed"] += 1
                        self._live_entry_filled(intent,
                                                side_fallback_price=entry_ref,
                                                now_ms=t)
            except BitgetError as e:
                if d.kind != "limit" and "40786" in str(e):
                    # 40786 = 重复的 clientOid:说明该笔市价单已在交易所落地成交,
                    # 按已成交建仓入账,绝不能标为 error 后被 _adopt_exchange_only 当异己仓覆盖止损!
                    self._journal("order_idempotent_recover", {
                        "oid": oid, "symbol": sym, "event": hyp.event_id,
                        "why": str(e)[:160]})
                    self._live_entry_filled(intent,
                                            side_fallback_price=entry_ref,
                                            now_ms=t)
                else:
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
            "limit_price", "ref_px", "invalid", "tf_label", "event_id",
            "route", "decision_ts", "mic", "size_mult", "edge_mult",
            "watch_entry", "watch_price", "near_price")})
        if is_watch and intent["status"] == "armed":
            self._notify_watch(intent, f"关注已建立({self.mode}，尚未下单)",
                               f"关注价 {entry_ref:.8g}；等待 {wait_h} 小时；非交易所挂单")

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
        it["status"], it["fill_price"], it["fill_ts"] = "filled", price, now_ms
        it["ref_px"] = price
        it["fill_price_source"] = "ref"
        self._register_position(it["symbol"], it, price, now_ms)
        if self.mode == "live" and it.get("exchange_order_id"):
            self._entry_settle[it["symbol"]] = {
                "oid": it["oid"], "order_id": str(it["exchange_order_id"]),
                "event_id": str(it.get("event_id")),
                "ref_px": price, "direction": int(it["direction"]),
                "submitted_ms": int(now_ms),
                "deadline_ms": int(now_ms) + 180_000}

    def _notify_watch(self, it: dict, status: str, reason: str) -> None:
        """Best-effort presentation only; never mutate intent/trading state."""
        try:
            self.tg.send_watch(str(it["symbol"]), str(it.get("tf_label") or "HTF"), status, reason)
        except Exception:
            pass

    def _poll_watch_entries(self, now_ms: int) -> None:
        """实盘 15s 轮询监控死区系统价格关注(WatchEntry:F1钳制内沿+差25%企稳补票+走完65%作废)。"""
        watched = [
            (oid, it) for oid, it in list(self.open_intents.items())
            if it.get("status") == "armed" and it.get("role") == "entry" and it.get("watch_entry")
        ]
        if not watched:
            return
        tickers_fn = getattr(self.client, "tickers", None)
        if not callable(tickers_fn):
            return
        try:
            raw_tickers = tickers_fn() or []
        except BitgetError as e:
            self._api_note(e)
            return
        px_by_sym: dict[str, tuple[float, float, float]] = {}
        for row in raw_tickers:
            s_name = str(row.get("symbol") or "")
            lp = float(row.get("lastPr") or row.get("markPrice") or 0.0) if s_name else 0.0
            if lp > 0.0:
                px_by_sym[s_name] = (lp, float(row.get("bidPr") or lp), float(row.get("askPr") or lp))
        for oid, it in watched:
            sym = str(it["symbol"])
            if sym in self.positions:
                it["status"] = "canceled"
                self._journal("watch_cancel", {"oid": oid, "symbol": sym, "why": "POSITION_ALREADY_OPEN"})
                self._notify_watch(it, "关注作废", "该品种已有持仓")
                continue
            last_p, bid_p, ask_p = px_by_sym.get(sym, (0.0, 0.0, 0.0))
            if last_p <= 0.0:
                continue
            d, inv = int(it["direction"]), float(it["invalid"])
            if (d > 0 and last_p <= inv) or (d < 0 and last_p >= inv):
                it["status"] = "canceled"
                self.counters["watch_canceled_invalid"] = self.counters.get("watch_canceled_invalid", 0) + 1
                self._journal("watch_cancel", {"oid": oid, "symbol": sym, "why": "ZONE_BROKEN",
                                               "price": last_p, "invalid": inv})
                self._notify_watch(it, "关注作废", "价格已越过失效位")
                continue
            watch_px = float(it.get("watch_price") or it.get("limit_price") or it["ref_px"])
            touched_watch = (last_p <= watch_px) if d > 0 else (last_p >= watch_px)
            prog_px = it.get("prog_price")
            max_prog, c0, tgt = it.get("max_prog_frac"), it.get("confirm_close"), it.get("target_1")
            if prog_px is None and max_prog is not None and tgt is not None and c0 is not None:
                span_rem = abs(float(tgt) - float(c0))
                if span_rem > 0.0:
                    prog_px = float(c0) + d * float(max_prog) * span_rem
            if not touched_watch and prog_px is not None:
                prog_f = float(prog_px)
                if (last_p >= prog_f) if d > 0 else (last_p <= prog_f):
                    it["status"] = "canceled"
                    self.counters["watch_canceled_played_out"] = \
                        self.counters.get("watch_canceled_played_out", 0) + 1
                    self._journal("watch_cancel", {"oid": oid, "symbol": sym,
                                                   "why": "MOVE_ALREADY_PLAYED_OUT",
                                                   "price": last_p, "prog_px": prog_f})
                    self._notify_watch(it, "关注作废", "未触发前行情已走完")
                    continue
            trigger_why = "WATCH_INNER_EDGE_HIT" if touched_watch else None
            if trigger_why is None and it.get("near_price") is not None:
                near_f = float(it["near_price"])
                if (last_p <= near_f) if d > 0 else (last_p >= near_f):
                    it["near_touched"] = True
                    prev_ext = it.get("near_extreme")
                    it["near_extreme"] = (
                        last_p if prev_ext is None
                        else (min(float(prev_ext), last_p) if d > 0 else max(float(prev_ext), last_p))
                    )
                elif it.get("near_touched"):
                    ext_f = float(it.get("near_extreme") if it.get("near_extreme") is not None else near_f)
                    band_span = max(abs(near_f - watch_px), 1e-12)
                    bounced = (
                        (last_p > near_f and (last_p - ext_f) >= 0.10 * band_span) if d > 0
                        else (last_p < near_f and (ext_f - last_p) >= 0.10 * band_span)
                    )
                    if bounced:
                        trigger_why = "WATCH_NEAR_BOUNCE"
            if trigger_why is None:
                continue
            self._trigger_watch_entry_live(oid, it, ask_p if d > 0 else bid_p, now_ms, trigger_why)

    def _trigger_watch_entry_live(self, oid: str, it: dict, exec_px: float,
                                  now_ms: int, trigger_why: str) -> None:
        sym = str(it["symbol"])
        d, inv = int(it["direction"]), float(it["invalid"])
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
        if self._directional_entry_blocked(d, qty, exec_px, inv, exclude_oid=oid):
            it["status"] = "canceled"
            self._journal("watch_cancel", {"oid": oid, "symbol": sym, "why": "DIRECTIONAL_CAP"})
            self._notify_watch(it, "关注作废", "DIRECTIONAL_CAP")
            return
        if self.breaker.should_stop(self.equity) or not self.rate.allow():
            return
        try:
            eq_now = float(self.equity or 0.0)
            cap_now = max(0.0, eq_now - float(self.unrealized_pl or 0.0))
            planned_lev = int(it.get("leverage") or self._plan_trade_leverage(
                sym, qty * exec_px, eq_now, cap_now,
                qty * abs(exec_px - inv)))
            try:
                self._ensure_leverage(sym, planned_lev)
            except TypeError:
                self._ensure_leverage(sym)
            it["qty"], it["kind"], it["watch_trigger"] = qty, "market", trigger_why
            resp = self.client.place_market_order(
                sym, side="buy" if d > 0 else "sell", size=qty, client_oid=oid,
                trade_side=self.open_trade_side(),
                **self._entry_protection(sym, it))
            it["exchange_order_id"] = str(resp.get("orderId") or "")
            self.counters["orders_placed"] += 1
            self.counters["watch_triggered"] = self.counters.get("watch_triggered", 0) + 1
            self._journal("watch_trigger", {"oid": oid, "symbol": sym, "why": trigger_why,
                                            "exec_px": exec_px, "qty": qty})
            self._notify_watch(it, "市价委托已提交（非成交确认）", trigger_why)
            self._live_entry_filled(it, side_fallback_price=exec_px, now_ms=now_ms)
        except BitgetError as e:
            it["status"] = "error"
            self.counters["order_errors"] = self.counters.get("order_errors", 0) + 1
            self._journal("order_error", {"oid": oid, "symbol": sym,
                                          "event": it.get("event_id"), "why": str(e)[:160]})
            self._api_note(e)

    def _entry_protection(self, sym: str, it: dict) -> dict:
        if self.price_clock != "realtime":
            return {}
        return {"preset_stop_loss": self.fit_price(
            sym, float(it["invalid"]), int(it["direction"]))}

    def _register_position(self, sym: str, it: dict, price: float,
                           ts: int, *, origin: str = "strategy") -> None:
        """建仓事实入库 + pm 建仓(策略链路与"接管"共用同一份持仓格式)。

        `origin="adopted"`:交易所既有仓位的接管(见 `market_operator/adopt.py`)。
        差异只有记账口径 —— 它不是本系统的成交,所以**不计入 `fills`**、不发
        "开仓"成交单(避免把接管伪装成本系统信号),而是发一条明确的接管告警。
        """
        tf_lbl = it.get("tf_label") or str(self.cog_cfg.get("main_tf_label") or "12h")

        cs = self._get_cs_for(sym, tf_lbl) or self.campaigns.get(sym)
        if cs is not None and not it.get("tf_label"):
            tf_lbl = cs.tf_label
        self.positions[sym] = {
            "symbol": sym, "tf_label": tf_lbl, "direction": it["direction"],
            "qty": it["qty"], "entry": price, "invalid": it["invalid"],
            "leverage": int(it.get("leverage") or self.leverage_by_sym.get(sym) or self._max_allowed_leverage(sym)),
            "target_1": execution_target(it, it["target_1"]), "event_id": it["event_id"],
            "structural_target": it.get("structural_target", it["target_1"]),
            "box_lo": it.get("box_lo"), "box_hi": it.get("box_hi"),
            "box_start_ts": it.get("box_start_ts"),
            "opened_ts": ts, "mark": price,
            "entry_invalid": float(it["invalid"]),
            "entry_qty": float(it["qty"]),
            "price_source": str(it.get("fill_price_source") or "ref")}
        # New position: drop any clock anchor left by a previous position on
        # this symbol. Otherwise the 15m clock would fetch from that old anchor
        # and walk the whole history in 100-bar pages.
        self._price_last.pop(sym, None)
        self._price_seen.pop(sym, None)
        self._touch_campaigns()              # 新仓:冷档里的 pm 事实必须同步
        if cs is not None:
            risk = it["direction"] * (price - it["invalid"])
            if risk > 0.0:
                try:
                    cs.pm.open(sym, it["direction"], price, it["invalid"],
                               execution_target(it, it["target_1"]), it["event_id"], ts,
                               len(cs.cog_bars))
                except ValueError as e:
                    self.tg.send_alert(f"{sym} pm 建仓拒绝:{e}")
        if self.mode == "live" and self.price_clock == "realtime":
            # Protection precedes chart rendering and notifications.
            self._manage_stops(ts)
            self._manage_targets(ts)
        if origin == "adopted":
            self.counters["adopted"] = self.counters.get("adopted", 0) + 1
        else:
            self.counters["fills"] += 1
        chart_bars = (cs.trig_bars[-96:] if (cs and it.get("mic") and cs.trig_bars)
                      else (cs.cog_bars[-96:] if (cs and cs.cog_bars) else (cs.trig_bars[-96:] if cs else None)))
        if origin == "adopted":
            # 接管不是本系统信号:用告警通道说清"这是交易所既有仓位 + 按什么
            # 结构价挂的保护",绝不伪装成一次"开仓成交"(会污染信号/胜率归因)。
            self.tg.send_alert(
                f"🛠 {sym} 接管交易所既有仓位 dir={it['direction']} "
                f"qty={it['qty']} @ {price:.8g} —— 已按新策略结构挂保护:"
                f"失效 {float(it['invalid']):.8g}"
                f"({'struct' if it.get('stop_source') == 'structure' else 'atr'})"
                f", 目标 {it.get('target_1')}；此后由 §8 统一管理")
        else:
            self.tg.send_fill(
                sym, (f"开仓暂记({self.mode},待成交回执)"
                      if self.mode == "live" and it.get("fill_price_source") != "fills"
                      else f"开仓({self.mode})"), it["qty"], price,
                note=f"{it['event_id']} {it['route']}"
                     + (f" · 结构目标 {it['target_1']} 仅观察，不触发锁利/减仓"
                        if it.get("target_1") is not None and execution_target(it, it["target_1"]) is None else "")
                     + (f" · 系统Watch触发 {it.get('watch_trigger') or '价格条件满足'}" if it.get("watch_entry") else "")
                     + (" · 参考价暂记，成交均价待回执校正" if self.mode == "live" and it.get("fill_price_source") != "fills" else ""),
                tf_label=tf_lbl, direction=it["direction"],
                price_label=("参考价(待成交回执)" if self.mode == "live" and it.get("fill_price_source") != "fills" else "成交价格"),
                stop_price=float(it["invalid"]), target_price=it.get("target_1"),
                box_lo=it.get("box_lo"), box_hi=it.get("box_hi"),
                box_start_ts=it.get("box_start_ts"),
                bars=chart_bars)
        # 开仓事实必须**自解释**:口径归因(MFE/ATR、按 TF 分桶、参考价 vs 成交价)
        # 全靠这一行,缺 `tf_label`/`opened_ts` 就只能靠事后猜。
        self._journal("adopt" if origin == "adopted" else "fill",
                      {"oid": it["oid"], "symbol": sym,
                       "qty": it["qty"], "price": price,
                       "ref_px": float(it.get("ref_px") or price),
                       "fill_px": float(it.get("fill_price") or price),
                       "price_source": str(
                           it.get("fill_price_source") or "ref"),
                       "invalid": float(it["invalid"]),
                       "direction": int(it["direction"]),
                       "tf_label": tf_lbl,
                       "opened_ts": int(ts),
                       "event_id": it["event_id"],
                       "structural_target": it.get("structural_target", it.get("target_1")),
                       "execution_target": execution_target(it, it.get("target_1")),
                       "role": "entry", "mode": self.mode,
                       "origin": origin,
                       "stop_source": it.get("stop_source"),
                       "target_source": it.get("target_source"),
                       "risk_atr": it.get("risk_atr"),
                       "adopted_breached": it.get("adopted_breached")})


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
            partial_market = (str(intent.reason).startswith(
                "v16|target_partial|")           # S2 段落袋市价(canonical)
                or "partial_exit" in str(intent.reason))
            # partial_exit 是"预算耗尽/武装即减仓"的状态型触发(campaign 侧同样
            # 只走市价):target_1 限价单在价格已回撤到预算线时不会成交 —— 挂出去
            # 等于 pm 已按"减仓后保本"口径记账,交易所却仍是整仓在裸奔。
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
                why = str(e)
                if role == "exit" and ("22002" in why or "暂无仓位可平" in why
                                       or "43023" in why):
                    # 交易所明确说"没有仓位可平" ⇒ 本地这条是残影(持仓早已不在)。
                    # 旧实现只发"仓位仍在,人工核查"并**每轮重试**,既刷 API 错误
                    # 也把真仓位(如果有)的处置延后。以交易所为真相立即收尾。
                    self.counters["close_no_pos"] = \
                        self.counters.get("close_no_pos", 0) + 1
                    self._journal("close_no_pos", {"symbol": sym, "role": role,
                                                   "why": why[:160]})
                    self.stops.pop(sym, None)
                    self._reconcile_closed(sym, dict(pos))
                    self.tg.send_alert(
                        f"{sym} 交易所已无仓位可平(22002/43023)⇒ 已按交易所为准"
                        f"清理本地记录(不再重试)")
                    return
                self.tg.send_alert(f"{sym} 平仓({role})下单失败:{str(e)[:120]}"
                                   f"(仓位仍在,人工核查)")
                self._api_note(e)
        self._journal("mgmt_intent", {"symbol": sym, "role": role,
                                      "kind": kind, "qty": qty,
                                      "reason": str(reason)[:80]})

    def _close_action_label(self, it: dict) -> str:
        """平仓/减仓通知的**动作类型**:部分减仓不得说成"平仓"。

        50% 减仓后余仓仍在场内(且防线已移到保本),用户若把它读成"已平仓"
        会直接误判账户风险敞口。
        """
        role = str(it.get("role") or "")
        reason = str(it.get("route") or "")
        if role == "reduce":
            return (f"减仓50%并保本({self.mode})" if "partial_exit" in reason
                    else f"减仓({self.mode})")
        return f"平仓({self.mode},{role})"

    def _live_close_filled(self, it: dict, *, ref_price: float,
                           title: Optional[str] = None,
                           note: Optional[str] = None,
                           price_label: str = "成交价格",
                           pnl: Optional[float] = None) -> None:
        """live 平仓记账(市价成交即记账;限价由 `_sync_live_fills` 确认)。

        可选 `title` / `note` / `pnl` 让调用方带**语义**入账(决策型离场要把
        "止盈(回撤预算耗尽)"与"止损(保护单触发)"分开),而不必再造一条记账
        路径 —— 记账口径(数量、费用、pm/positions 清理)只有这一处。
        """
        pos = self.positions.get(it["symbol"])
        if pos is None:
            return
        qty = min(it["qty"], pos["qty"])
        if pnl is None:
            gross = qty * pos["direction"] * (float(ref_price)
                                              - float(pos["entry"]))
            fee = qty * float(ref_price) * self.risk["taker_fee"]
            pnl = gross - fee
        pos["qty"] = round(pos["qty"] - qty, 10)
        it["status"] = "filled"
        self.counters["reduce_fills" if it["role"] == "reduce" else "exits"] += 1
        cs = self._get_cs_for(it["symbol"], pos.get("tf_label")) or self.campaigns.get(it["symbol"])
        chart_bars = (cs.cog_bars[-96:] if (cs and cs.cog_bars) else (cs.trig_bars[-96:] if cs else None))
        self.tg.send_fill(
            it["symbol"], title or self._close_action_label(it),
            qty, float(ref_price), pnl=pnl,
            note=str(it["route"])[:60] if note is None else str(note)[:160],
            tf_label=pos.get("tf_label", cs.tf_label if cs else "12h"),
            direction=pos["direction"],
            entry_price=float(pos["entry"]),      # 图 ENTRY 用真实建仓均价
            stop_price=pos.get("invalid"),
            target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            box_start_ts=pos.get("box_start_ts"),
            bars=chart_bars, price_label=price_label)
        self._journal("close_fill", {"oid": it["oid"], "symbol": it["symbol"],
                                     "qty": qty, "role": it["role"],
                                     "price": float(ref_price), "pnl": pnl,
                                     "price_source": it.get("price_source")})
        if it["role"] == "exit" or pos["qty"] <= 1e-9:
            sym = it["symbol"]
            self.positions.pop(sym, None)
            self._missing_streak.pop(sym, None)
            # 已知平仓:pm 侧事实必须**同时**清 —— 否则 `_close_cognition`
            # 继续管理一个不存在的仓,且该品种再触发 `pm.open` 会因"已持仓"
            # 被拒(告警"pm 建仓拒绝")。与 `_reconcile_closed` 同口径。
            if cs is not None:
                cs.pm.positions.pop(sym, None)
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
                it.setdefault("ref_px", float(it.get("limit_price") or price))
                it["fill_price_source"] = "fills"
                self._register_position(it["symbol"], it, price, now_ms)
                self._audit_entry_cost(it, price)
                self._audit_directional_fill(it)
            else:
                self._live_close_filled(it, ref_price=price)

    def _manage_targets(self, now_ms: int) -> None:
        """Rest the existing strategy's first 50% target at the exchange."""
        if self.mode != "live" or self.price_clock != "realtime":
            return
        for sym, pos in list(self.positions.items()):
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
                continue
            if pm is None or cs is None or pm.reduced or target is None:
                continue
            part_en = bool(getattr(cs.pm, "partial_exit_enabled", False))
            lock_f = float(getattr(cs.pm, "reject_opp_lock_frac", 0.0) or 0.0)
            full_tp_mode = (not part_en) and lock_f >= 1.0 - 1e-9
            if not part_en and not full_tp_mode:
                continue
            if pos.get("target_order"):
                pm.pending_reduce = True
                continue
            spec = self._spec(sym)
            tp_frac = 1.0 if full_tp_mode else 0.5
            qty = floor_step(float(pos["qty"]) * tp_frac,
                             spec.size_step, spec.size_place)
            if qty <= 0:
                self._alert_throttled(f"target_size:{sym}",
                                      f"{sym} 半仓止盈数量不足最小精度，未擅自改成全平")
                continue
            oid = client_oid_of(self.OID_PREFIX, sym, str(pos["event_id"]),
                                "target", int(pos["opened_ts"]),
                                role=f"target{int(pos.get('target_generation', 0))}")
            try:
                # Recover an accepted request after a crash/timeout before
                # local state was persisted; ownership is the exact client ID.
                self._pending_stop_ids(now_ms)
                resp = next((row for row in self._stops_live_rows or []
                             if str(row.get("clientOid")) == oid
                             and str(row.get("symbol")) == sym
                             and row.get("planType") == "profit_plan"), None)
                if resp is None:
                    resp = self.client.place_partial_take_profit(
                        sym, trigger_price=self.fit_price(sym, float(target),
                                                           -int(pos["direction"])),
                        size=qty, hold_side=self.stop_hold_side(pos["direction"]),
                        client_oid=oid)
                ex_id = str((resp or {}).get("orderId") or "")
                if not ex_id:
                    raise BitgetError("target protection missing orderId")
                pos["target_order"] = {"order_id": ex_id, "qty": qty,
                                       "accounted_qty": 0.0,
                                       "original_qty": float(pos["qty"]),
                                       "size_frac": pm.size_frac,
                                       "client_oid": oid}
                pm.pending_reduce = True
                self._touch_campaigns()
                self._stops_live_ids, self._stops_live_rows = None, None
                self._journal("target_protection", {"symbol": sym,
                              "order_id": ex_id, "qty": qty, "target": target})
            except BitgetError as exc:
                self._journal("target_protection_error", {"symbol": sym,
                                                           "why": str(exc)[:160]})
                self._alert_throttled(f"target:{sym}",
                                      f"{sym} 交易所半仓止盈未建立：{str(exc)[:120]}")

    def _sync_target_fills(self, now_ms: int) -> None:
        """Only exchange execution facts consume the one-time partial exit."""
        for sym, pos in list(self.positions.items()):
            order = pos.get("target_order")
            if not order:
                continue
            if float(order.get("accounted_qty", 0.0)) >= float(order["qty"]) - 1e-9:
                continue
            cs = self._get_cs_for(sym, pos.get("tf_label"))
            pm = cs.pm.positions.get(sym) if cs else None
            if pm is None:
                continue
            try:
                history = self.client.tpsl_order_history(sym, order["order_id"])
            except BitgetError as exc:
                self._journal("target_sync_error", {"symbol": sym,
                                                    "why": str(exc)[:160]})
                continue
            row = next((r for r in (history or []) if str(r.get("orderId")) ==
                        order["order_id"]), None)
            if row is None:
                continue  # Absence is not proof of cancellation or execution.
            status = str(row.get("planStatus") or "")
            if (status in ("cancelled", "fail_execute")
                    and not row.get("executeOrderId")
                    and float(order.get("accounted_qty", 0.0)) == 0):
                pos.pop("target_order", None)
                pos["target_generation"] = int(pos.get("target_generation", 0)) + 1
                pm.pending_reduce = False
                self._touch_campaigns()
                self._journal("target_rearm", {"symbol": sym, "status": status})
                continue
            if status in ("cancelled", "fail_execute"):
                # A triggered order may have partially filled: never blindly
                # reissue the full target quantity.
                self._alert_throttled(f"target_terminal:{sym}",
                                      f"{sym} 止盈计划终态 {status}，需核对成交，保留止损")
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
            # Reconcile may already have observed the lower exchange quantity.
            # Feed the common fill recorder its pre-fill quantity exactly once.
            pos["qty"] = remaining + delta
            close_role = "exit" if remaining <= 1e-9 else "reduce"
            self._live_close_filled({
                "oid": order["client_oid"], "symbol": sym, "role": close_role,
                "qty": delta, "route": f"{'full_tp_exit' if close_role == 'exit' else 'partial_exit'}|exchange target fill",
                "price_source": "fills"}, ref_price=px)
            if sym in self.positions:
                self.positions[sym]["qty"] = remaining
            pm.pending_reduce = total < float(order["qty"]) - 1e-9
            self._touch_campaigns()

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
            if r.get("planType") in ("profit_plan", "pos_profit"):
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

    def _entry_loss_plan_rows(self, sym: str, pos: dict,
                              rows: Optional[list]) -> list:
        """交易所自动生成的 `loss_plan`(开仓预设止损)候选行。

        这类单是 entry `presetStopLossPrice` 成交后由交易所自动生成的旧式
        计划单,不是本地的 `pos_loss`:它不随防线推进、平仓时也不会被本地
        撤掉。只在同 symbol/同方向、且 size 仍覆盖入场数量时认领;真正撤销
        前由 `_cancel_entry_loss_plans` 确保 `pos_loss` 已在挂。
        """
        side = self.stop_hold_side(int(pos["direction"]))
        entry_invalid = pos.get("entry_invalid")
        want_entry = None
        if entry_invalid is not None:
            try:
                want_entry = self.fit_price(
                    sym, float(entry_invalid), int(pos["direction"]))
            except (TypeError, ValueError):
                want_entry = None
        out: list = []
        for row in rows or ():
            if str(row.get("symbol")) != sym:
                continue
            if str(row.get("planType") or "") != "loss_plan":
                continue
            if (row.get("holdSide") or row.get("posSide")) != side:
                continue
            if str(row.get("clientOid") or "").startswith(self.OID_PREFIX):
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
            # 预设单是整仓 size;部分减仓后它仍可能大于剩余仓位,不能小于剩余仓位。
            if size + max(1e-9, abs(size) * 1e-6) < float(pos["qty"]):
                continue
            out.append(row)
        return out

    def _cancel_entry_loss_plans(self, sym: str, pos: dict,
                                 rows: Optional[list], *, reason: str) -> None:
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
                if (isinstance(resp, dict)
                        and not (resp.get("successList") or resp.get("failureList"))):
                    raise BitgetError("cancel-plan-order 未返回成功项")
            except BitgetError as e:
                self.counters["stop_preset_cancel_errors"] = \
                    self.counters.get("stop_preset_cancel_errors", 0) + 1
                self._journal("stop_preset_cancel_error",
                              {"symbol": sym, "order_id": oid,
                               "why": str(e)[:160], "reason": reason})
                self._alert_throttled(
                    f"stop_preset:{sym}",
                    f"{sym} 开仓预设止损未能撤销({oid});保留 pos_loss 并重试")
                continue
            pos["entry_preset_order_id"] = None
            self.counters["stop_preset_cancels"] = \
                self.counters.get("stop_preset_cancels", 0) + 1
            self._journal("stop_preset_cancel",
                          {"symbol": sym, "order_id": oid,
                           "trigger": row.get("triggerPrice"),
                           "reason": reason})
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
            # 本地止损模式:交易所拒绝为小仓位挂 TPSL(43023)时退化为本地裁决。
            # 语义:保护只能缺席「交易所单」,不能缺席「风险」。每轮用标记价与本
            # 地防线比较,越过即市价离场(与 stop_missing_breached 同一条纪律)。
            if cur is not None and cur.get("mode") == "local":
                mark = float(pos.get("mark") or 0.0)
                breached = mark > 0.0 and (
                    (int(pos["direction"]) > 0 and mark <= want)
                    or (int(pos["direction"]) < 0 and mark >= want))
                if breached:
                    self.stops.pop(sym, None)
                    self._cancel_entry(sym, "local_stop")
                    self._arm_close_intent(
                        sym, pos, stop_qty, "market", None,
                        f"LOCAL_STOP|{want:.8g}", int(now_ms),
                        role="exit", force=True)
                    self.counters["stop_local_triggered"] = \
                        self.counters.get("stop_local_triggered", 0) + 1
                    self._journal("stop_local_triggered",
                                  {"symbol": sym, "trigger": want,
                                   "mark": mark})
                    self.tg.send_alert(
                        f"{sym} 本地止损触发(该仓位无法挂交易所 TPSL):"
                        f"现价 {mark:.8g} 越过防线 {want:.8g} → 市价离场")
                continue
            missing = bool(cur and cur.get("order_id") and live_ids is not None
                           and str(cur["order_id"]) not in live_ids)
            if not missing and cur and cur.get("order_id") and self._stops_live_rows:
                ex_row = next(
                    (r for r in self._stops_live_rows
                     if str(r.get("orderId")) == str(cur["order_id"])),
                    None,
                )
                if ex_row is not None:
                    try:
                        ex_trig = float(
                            ex_row.get("triggerPrice")
                            or ex_row.get("stopLossTriggerPrice")
                            or cur["trigger"]
                        )
                        if abs(ex_trig - float(cur["trigger"])) > 1e-9:
                            cur["trigger"] = ex_trig
                    except (TypeError, ValueError):
                        pass
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
            if cur is None and self.price_clock == "realtime":
                # Preset SL can already exist before local registration. Reuse
                # its exchange ID instead of creating a duplicate defense.
                for row in self._stops_live_rows or []:
                    if (str(row.get("symbol")) == sym
                            and row.get("planType") == "pos_loss"
                            and (row.get("holdSide") or row.get("posSide")) ==
                            self.stop_hold_side(pos["direction"])
                            and str(row.get("clientOid") or "").startswith(self.OID_PREFIX)
                            and abs(float(row.get("triggerPrice") or 0) - want) < 1e-12):
                        cur = {"trigger": want, "qty": stop_qty,
                               "order_id": str(row["orderId"])}
                        self.stops[sym] = cur
                        break
            if cur is not None and abs(float(cur["trigger"]) - want) < 1e-12:
                continue
            try:
                if (self.price_clock == "realtime" and cur is not None
                        and cur.get("order_id") and not missing):
                    self.client.modify_stop_loss(
                        sym, order_id=cur["order_id"], trigger_price=want)
                    cur["trigger"] = want
                    self._stops_live_ids, self._stops_live_rows = None, None
                    self._stops_checked_at = 0.0
                    self._journal("stop_amend", {"symbol": sym, "trigger": want,
                                                  "order_id": cur["order_id"]})
                    continue
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
                why = str(e)
                self._journal("stop_error", {"symbol": sym, "trigger": want,
                                             "qty": stop_qty,
                                             "why": why[:160]})
                if _stop_requires_local(why):
                    # 仓位太小/交易所不支持 TPSL:切成**本地止损监控**,绝不再
                    # 丢记录(见 `_STOP_TOO_SMALL_MARKS` 注释)。错误只记一次,
                    # 之后本模式不再尝试下交易所单 ⇒ 不刷 API 错误计数。
                    self.stops[sym] = {"trigger": want, "qty": stop_qty,
                                       "order_id": "", "mode": "local"}
                    if not cur or cur.get("mode") != "local":
                        self.counters["stop_local_mode"] = \
                            self.counters.get("stop_local_mode", 0) + 1
                        self._journal("stop_local_mode",
                                      {"symbol": sym, "trigger": want,
                                       "qty": stop_qty, "why": why[:160]})
                        self.tg.send_alert(
                            f"{sym} 该仓位无法挂交易所保护单(仓位过小,"
                            f"43023)⇒ 已切成**本地止损**监控:防线 "
                            f"{want:.8g},越过即市价离场")
                    continue
                if _stop_reject_no_position(why):
                    # 交易所已无该仓位(43023):本地是残影,不是"价格穿越"。
                    # 丢本地止损记录即可,仓位是否真的消失由 `reconcile` 判定;
                    # 此处发市价平仓只会再被拒一次并误报"仓位仍在"。
                    self.stops.pop(sym, None)
                    self.counters["stop_place_no_pos"] = \
                        self.counters.get("stop_place_no_pos", 0) + 1
                    self._journal("stop_place_no_pos",
                                  {"symbol": sym, "trigger": want,
                                   "qty": stop_qty, "why": why[:160]})
                    continue
                self._alert_throttled(
                    f"stop:{sym}",
                    f"{sym} 保护止损挂单失败:{why[:120]}"
                    f"(当前无触发保护,人工介入;同类告警 30 分钟内不重复)")
                # 挂单失败最常见的原因就是**价格已越过触发价**(交易所直接 400)。
                # 这时重试永远不会成功,而"下个 tick 再试"= 整段裸奔:已越线即
                # 市价离场(与 stop_missing_breached 同口径,同一条纪律)。
                # 越线判定优先用**交易所报文里自己报的标记价**(比本地 2 分钟前的
                # reconcile 值准),取不到才回退本地 mark。
                mark = _mark_from_reject(why) or float(pos.get("mark") or 0.0)
                breached = mark > 0.0 and (
                    (int(pos["direction"]) > 0 and mark <= want)
                    or (int(pos["direction"]) < 0 and mark >= want))
                if breached:
                    self.stops.pop(sym, None)
                    self.counters["stop_place_breached"] = \
                        self.counters.get("stop_place_breached", 0) + 1
                    self._journal("stop_place_breached",
                                  {"symbol": sym, "trigger": want,
                                   "mark": mark, "why": str(e)[:160]})
                    self._arm_close_intent(
                        sym, pos, stop_qty, "market", None,
                        f"STOP_PLACE_BREACHED|{want:.8g}", int(now_ms),
                        role="exit", force=True)
                    self.tg.send_alert(
                        f"{sym} 保护止损挂单被拒({str(e)[:80]}),且现价 "
                        f"{mark:.8g} 已越过防线 {want:.8g} → 市价离场")
        # pos_loss 已确认后,把开仓时交易所自动生成的 loss_plan 撤掉;
        # 保留 pos_loss(唯一事实源)和 profit_plan(半仓止盈)。
        if self.mode == "live" and live_ids is not None:
            rows = self._stops_live_rows
            for sym, pos in list(self.positions.items()):
                cur = self.stops.get(sym)
                if (cur is not None and cur.get("order_id")
                        and not cur.get("mode")
                        and str(cur["order_id"]) in live_ids):
                    self._cancel_entry_loss_plans(
                        sym, pos, rows, reason="pos_loss_active")
        # 快照有效才去重(刚换过单时快照已作废,留待下轮 TTL 重读)
        self._dedupe_stops(now_ms)

    def _cancel_stop(self, sym: str, why: str) -> None:
        pos = self.positions.get(sym)
        if self.price_clock == "realtime":
            target = pos.get("target_order") if pos else None
            if target and target.get("order_id"):
                try:
                    self.client.cancel_tpsl_order(sym, target["order_id"])
                except BitgetError as exc:
                    self._journal("target_cancel_error", {"symbol": sym,
                                                           "why": str(exc)[:160]})
        cur = self.stops.pop(sym, None)
        if cur is not None and cur.get("order_id") and self.mode == "live":
            self._stops_live_ids, self._stops_live_rows = None, None
            self._stops_checked_at = 0.0
            try:
                self.client.cancel_tpsl_order(sym, cur["order_id"])
            except BitgetError:
                pass
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
                if it.get("watch_entry"):
                    self._notify_watch(it, "关注到期" if reason == "N_fill_expiry" else "关注作废", str(reason))

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

    def _close_fill_fact(self, sym: str, *, since_ms: Optional[int] = None,
                         opened_ts_ms: Optional[int] = None
                         ) -> Optional[tuple[float, Optional[int]]]:
        """该品种**最近一笔**平仓成交的 `(vwap, 成交时刻ms)`;取不到返回 None。

        三条修正(修前一个都不成立,所以离场回执系统性错):
        ① **不许假定列表顺序**:fills 端点按新→旧返回,但排序口径随端点/版本
           漂移。这里显式按成交时间**降序**取第一条 —— 修前是遍历覆盖,留下
           的是窗口内**最旧**一笔平仓成交(实测 BABA 回执 110.47,而它的保护单
           105.148 早该成交:拿的是上一笔)。
        ② **同一订单的多笔成交按 size 加权聚合**(vwap + 最晚时刻),而不是
           只取其中一笔 —— 分批成交时单笔价格不代表这笔平仓。
        ③ **时间窗**:只用"本仓开仓之后"的平仓成交(`opened_ts_ms`)。窗口内
           没有 ⇒ 返回 None(如实不编),绝不把上一笔仓的平仓价记到这一仓头上。

        `since_ms` 供"我刚下的这张单"用:只要那之后的成交,避免把同时段内
        另一笔平仓算进来。
        """
        try:
            res = self.client.recent_fills(sym, limit=100)
        except BitgetError:
            return None
        rows = res if isinstance(res, list) else (
            (res or {}).get("fillList") or (res or {}).get("fills") or [])
        cand: list[tuple[int, float, float, str]] = []
        for r in rows:
            if str(r.get("symbol")) != sym:
                continue
            if str(r.get("tradeSide") or "").lower() != "close":
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
        if not oid:                       # 无订单号的端点:只能如实取最新一笔
            return newest_px, newest_ts
        same = [c for c in cand if c[3] == oid]
        tot = sum(c[2] for c in same)
        if tot <= 0.0:
            return newest_px, newest_ts
        vwap = sum(c[1] * c[2] for c in same) / tot
        return vwap, max(c[0] for c in same)

    def _settle_exits(self, now_ms: int) -> None:
        """决策型离场的**成交回执结算**(下单瞬间取不到成交价时的二次确认)。

        记账已在 `_live_market_exit` 用参考价落地(宁可偏差,也不静默),所以这里
        **只做校正**:journal 记权威成交价与已实现盈亏;偏差显著(>0.2%)才补发
        一条告警 —— 一条离场通知绝不因为"接口晚到 200ms"而消失。
        超时仍未取到回执 ⇒ 如实记 `exit_settle_miss` 并停手(不无限重试)。
        """
        for sym, rec in list(self._exit_settle.items()):
            fact = self._close_fill_fact(sym, since_ms=int(rec["since_ms"]))
            if fact is None:
                if now_ms >= int(rec["deadline_ms"]):
                    self._exit_settle.pop(sym, None)
                    self.counters["exit_settle_miss"] = \
                        self.counters.get("exit_settle_miss", 0) + 1
                    self._journal("exit_settle_miss",
                                  {"symbol": sym, "oid": rec["oid"],
                                   "ref_px": rec["ref_px"]})
                continue
            self._exit_settle.pop(sym, None)
            px, ts = float(fact[0]), fact[1]
            pnl = realized_pnl(rec["direction"], rec["qty"], rec["entry"], px,
                               self.risk["taker_fee"])
            pnl_ref = realized_pnl(rec["direction"], rec["qty"], rec["entry"],
                                   float(rec["ref_px"]), self.risk["taker_fee"])
            self._journal("exit_settle",
                          {"symbol": sym, "oid": rec["oid"],
                           "ref_px": float(rec["ref_px"]), "exit_price": px,
                           "exit_ts_ms": ts, "pnl": pnl, "pnl_ref": pnl_ref,
                           "why": rec.get("why")})
            ref = float(rec["ref_px"])
            if ref > 0.0 and abs(px - ref) / ref > 2e-3:
                self.tg.send_alert(
                    f"{sym} 离场回执校正:成交 {px:.8g} ≠ 记账参考 {ref:.8g}"
                    f"(差 {abs(px - ref) / ref:.2%}),已实现 {pnl:+.2f} USDT")

    def _settle_entries(self, now_ms: int) -> None:
        """开仓成交回执结算:把 `entry` 从参考价校正为交易所成交均价(§6a)。

        与 `_settle_exits` 同一套纪律 —— 记账**先落地**(下单即记,宁可偏差也不
        静默漏记),这里只做校正并留痕:
        * 命中 ⇒ 改 `positions.entry` 与 pm 的 R 基准,journal 记 `ref_px`/
          `fill_px`/`slip_bps`(逆向漂移为正);偏离 ≥20 bps 补一条告警,
          因为这正是实盘 R 与计划 R 脱钩的来源。
        * 超时仍取不到 ⇒ 记 `entry_settle_miss` 后停手(不无限刷接口),
          仓位保留参考价口径且 `price_source="ref"` 如实标注。
        * 仓位已平/已被新事件顶替 ⇒ 直接出队(绝不把回执记到下一仓头上)。
        """
        for sym, rec in list(self._entry_settle.items()):
            pos = self.positions.get(sym)
            if pos is None or str(pos.get("event_id")) != str(rec["event_id"]):
                self._entry_settle.pop(sym, None)
                continue
            fact = self._entry_fill_fact(sym, str(rec["order_id"]))
            if fact is None:
                if now_ms >= int(rec["deadline_ms"]):
                    self._entry_settle.pop(sym, None)
                    self.counters["entry_settle_miss"] = \
                        self.counters.get("entry_settle_miss", 0) + 1
                    self._journal("entry_settle_miss",
                                  {"symbol": sym, "oid": rec["oid"],
                                   "order_id": rec["order_id"],
                                   "ref_px": float(rec["ref_px"]),
                                   "event_id": rec["event_id"]})
                continue
            self._entry_settle.pop(sym, None)
            px, ts, qty = float(fact[0]), fact[1], float(fact[2])
            ref = float(rec["ref_px"])
            d = int(rec["direction"])
            slip_bps = (d * (px - ref) / ref * 1e4) if ref > 0.0 else 0.0
            applied = self._apply_entry_fill(sym, px)
            it = self.open_intents.get(rec["oid"])
            if it is not None:
                self._audit_entry_cost(it, px)
                self._audit_directional_fill(it)
            self._journal("entry_settle",
                          {"symbol": sym, "oid": rec["oid"],
                           "order_id": rec["order_id"],
                           "event_id": rec["event_id"],
                           "ref_px": ref, "fill_px": px,
                           "fill_qty": qty, "fill_ts_ms": ts,
                           "slip_bps": slip_bps, "applied": applied,
                           "price_source": "fills"})
            if applied and abs(slip_bps) >= 20.0:
                self.tg.send_alert(
                    f"{sym} 开仓成交价 {px:.8g} ≠ 信号参考价 {ref:.8g}"
                    f"({slip_bps:+.0f} bps,逆向为正);entry 已按成交价重记,"
                    f"实际 R 与计划 R 的偏差请以本条为准")

    def _audit_directional_fill(self, it: dict) -> None:
        """Report actual exposure without pretending an executed order can be rejected."""
        if it.get("directional_cap_breach") or not self._directional_entry_blocked(
                int(it["direction"]), 0.0, 0.0, 0.0, exclude_oid=it["oid"]):
            return
        it["directional_cap_breach"] = True
        self.counters["directional_cap_breaches"] = self.counters.get("directional_cap_breaches", 0) + 1
        self._journal("directional_cap_breach", {"oid": it["oid"], "symbol": it["symbol"],
                                                "directional_cap_frac": self.directional_cap_frac})
        self.tg.send_alert(f"{it['symbol']} 成交回执时同向在途风险超限;"
                           "已保留真实持仓与保护规则,后续入场受同向上限约束")

    def _audit_entry_cost(self, it: dict, fill_px: float) -> None:
        """A fill is a fact, not a rejectable candidate. Record ceiling breaches.

        A market order cannot guarantee its eventual execution price. Never
        erase a filled position or invent an automatic liquidation policy.
        """
        if not it.get("max_cost_frac") or it.get("entry_cost_breach"):
            return
        error = entry_contract_error(int(it["direction"]), float(fill_px),
                                     float(it["invalid"]), it.get("target_1"),
                                     {"max_cost_frac": it["max_cost_frac"],
                                      "cost_per_side": it.get("cost_per_side")})
        if error:
            it["entry_cost_breach"] = error
            self.counters["entry_cost_breaches"] = self.counters.get("entry_cost_breaches", 0) + 1
            self._journal("entry_cost_breach", {"oid": it["oid"], "symbol": it["symbol"],
                                               "fill_px": fill_px, "invalid": it["invalid"],
                                               "max_cost_frac": it["max_cost_frac"], "why": error})
            self.tg.send_alert(f"{it['symbol']} 成交回执超出入场成本约束({error});"
                               "已保留真实持仓与保护规则,未自动平仓")

    def _entry_fill_fact(self, sym: str, order_id: str
                         ) -> Optional[tuple[float, Optional[int], float]]:
        """该**开仓单**的成交 `(vwap, 成交时刻ms, 成交张数)`;取不到返回 None。

        与 `_close_fill_fact` 同口径(按订单号锁定 + size 加权聚合),但开仓
        不需要时间窗猜:订单号本身就是唯一事实,因此不按 `tradeSide` 过滤 ——
        该字段随端点漂移,而 orderId 不会把别人的成交算进来。
        """
        oid = str(order_id or "")
        if not oid:
            return None
        try:
            res = self.client.recent_fills(sym, limit=100)
        except BitgetError:
            return None
        rows = res if isinstance(res, list) else (
            (res or {}).get("fillList") or (res or {}).get("fills") or [])
        num = amt = 0.0
        last_ts: Optional[int] = None
        for r in rows:
            if str(r.get("symbol")) != sym:
                continue
            if str(r.get("orderId") or "") != oid:
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
            return False        # 成交价已越过防线:交给止损路径,不覆盖
        pos["entry"] = px
        pos["price_source"] = "fills"
        cs = self._get_cs_for(sym, pos.get("tf_label")) \
            or self.campaigns.get(sym)
        if cs is not None:
            cs.pm.retag_entry(sym, px)
        return True

    def _reconcile_closed(self, sym: str, pos: dict) -> None:
        """交易所已无此仓:以交易所为准清理本地事实,并**如实分型**离场。

        修前两处失真(都是"把未知写成确定"):
        ① 一律报"离场确认(交易所已无此仓)",盈利离场与亏损离场无法区分;
        ② 成交价取 fills 窗口里最旧的一笔 ⇒ 回执价格/PnL 系统性错。

        现在按两个正交事实出账:
        * `executor` —— 成交价是否落在保护单成交侧(`stop_execution_evidence`);
        * `pnl`      —— 按该笔成交价与 taker 费计算。
        取不到成交价 ⇒ executor="unknown"、pnl=None,文案只陈述已知事实。
        """
        cs = self._get_cs_for(sym, pos.get("tf_label")) or self.campaigns.get(sym)
        if cs is not None:
            cs.pm.positions.pop(sym, None)
        st = self.stops.pop(sym, None)       # 取证据:触发前那张保护单的价位
        self.positions.pop(sym, None)
        self._price_last.pop(sym, None)
        self._price_seen.pop(sym, None)
        self._missing_streak.pop(sym, None)
        self._exit_settle.pop(sym, None)     # 仓已确认归零:无需再校正回执
        self._exit_retry.pop(sym, None)      # 同上:重试队列也随仓位归零作废
        self._touch_campaigns()              # 交易所已无此仓:冷档 pm 事实要清
        fact = self._close_fill_fact(
            sym, opened_ts_ms=(int(pos["opened_ts"]) if pos.get("opened_ts")
                               else None))
        px: Optional[float] = float(fact[0]) if fact is not None else None
        stop_px: Optional[float] = None
        for cand in ((st or {}).get("trigger"), pos.get("invalid")):
            try:
                if cand not in (None, 0, 0.0):
                    stop_px = float(cand)
                    break
            except (TypeError, ValueError):
                continue
        if px is None:
            executor = "unknown"
        elif stop_px is not None and stop_execution_evidence(
                int(pos["direction"]), px, stop_px):
            executor = "protective_stop"
        else:
            executor = "external"
        pnl = (realized_pnl(pos["direction"], pos["qty"], pos["entry"], px,
                            self.risk["taker_fee"]) if px is not None else None)
        label = exit_label(executor=executor, pnl=pnl)
        self.counters["reconcile_closed"] = \
            self.counters.get("reconcile_closed", 0) + 1
        self._journal("reconcile_closed",
                      {"symbol": sym, "direction": pos["direction"],
                       "qty": pos["qty"], "entry": pos["entry"],
                       "exit_price": px,
                       "exit_ts_ms": (fact[1] if fact else None),
                       "price_source": ("fills" if px is not None else None),
                       "stop": stop_px, "executor": executor, "pnl": pnl})
        if executor == "protective_stop":
            note = (f"成交价落在保护单成交侧(止损 {stop_px:.8g})"
                    f"{' · 盈利侧成交即移动止损止盈' if (pnl or 0.0) > 0 else ''}"
                    f";本地持仓事实已按交易所清理")
        elif executor == "external":
            note = (f"成交价未落在保护单成交侧(止损 {stop_px:.8g})"
                    f" ⇒ 外部/人工平仓;本地持仓事实已按交易所清理")
        else:
            note = "取不到成交回执(fills 无本仓开仓后的平仓成交);仅清理本地事实"
        self.tg.send_fill(
            sym, label, float(pos["qty"]),
            float(px if px is not None else pos.get("mark") or pos["entry"]),
            pnl=pnl, note=note,
            tf_label=pos.get("tf_label"), direction=int(pos["direction"]),
            entry_price=float(pos["entry"]),      # 图 ENTRY 用真实建仓均价
            stop_price=float(pos["invalid"]), target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            price_label=("成交价" if px is not None else "参考价(无成交回执)"))


    def _adopt_exchange_only(self, ex_pos: list) -> None:
        """接管"交易所独有仓位"(扩展层;策略链路零改动)。

        为什么要有:旧实现对异己仓一律"绝不动作",它既没有交易所侧保护止损,
        也不进入 §8 持仓管理 —— 风险留在账本之外,只能靠人盯告警。接管做的事
        只有两件:①按**已有结构事实**算一个与策略同源的失效价;②用与策略完全
        相同的持仓格式建档(于是 `_manage_stops` 挂 TPSL、`on_bar` 做结构出场
        都自动生效)。

        边界(缺一不可,否则宁可不接管也要留痕):
        - 只在 live 模式、且 `adopt_external_positions.enabled` 为真时执行;
        - 同标的本地已有仓位 ⇒ 不接管(状态模型是 symbol→单仓,hedge_mode 下
          反手侧会互相覆盖);
        - 该标的没有战役/拿不到 ATR ⇒ 不接管,并如实告警"无结构可用";
        - 接管后若现价已越过失效价,记 `adopted_breached` 并告警:下一根认知
          bar 收盘仍在外侧时由 §8.1 正常出场,不另造就地强平路径。
        """
        if not self.adopt_enabled or self.mode != "live":
            return
        now_ms = int(time.time() * 1000)
        for p in ex_pos:
            sym = str(p.get("symbol") or "")
            d = int(p.get("direction") or 0)
            if not sym or d not in (1, -1):
                continue
            if sym in self.positions:
                continue
            entry = float(p.get("entry_price") or 0.0)
            mark = float(p.get("mark_price") or 0.0) or entry
            qty = float(p.get("qty") or 0.0)
            # 优先匹配本系统自身的近期开仓意图(如因超时/40786/重启未入 positions 的策略单):
            # 必须保留原策略周期(如 4h)、原结构止损 invalid 与原 target_1,绝不用 1d ATR 覆盖!
            own_cands = [
                it for it in self.open_intents.values()
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
                self._journal("reconcile_recovered_own_intent", {
                    "symbol": sym, "oid": own_it.get("oid"),
                    "tf_label": own_it.get("tf_label"),
                    "entry": entry, "invalid": own_it.get("invalid"),
                    "target_1": own_it.get("target_1"),
                })
                continue
            cs = (self._get_cs_for(sym, str(self.cog_cfg.get("main_tf_label") or "12h"))
                  or self.campaigns.get(sym))
            if cs is None:
                self._journal("adopt_skipped", {"symbol": sym, "direction": d,
                                                "why": "无战役(不在交易宇宙)"})
                self._alert_throttled(
                    f"adopt:{sym}",
                    f"{sym} 交易所独有仓位无法接管:该标的无战役/无结构事实"
                    f"(不在宇宙),人工核查")
                continue
            try:
                atr = float(cs.detector.structure.atr())
                zones = list(cs.detector.structure.zones)
            except Exception as e:                      # noqa: BLE001
                self._journal("adopt_skipped", {"symbol": sym, "direction": d,
                                                "why": f"结构读取失败:{type(e).__name__}"})
                continue
            struct = adopt_mod.structure_from_zones(
                atr=atr, zones=zones, k_bm=float(self.rp.k_bm))
            entry = float(p.get("entry_price") or 0.0)
            mark = float(p.get("mark_price") or 0.0) or entry
            ad = adopt_mod.propose(symbol=sym, direction=d, entry=entry,
                                   mark=mark, structure=struct)
            if ad is None:
                self._journal("adopt_skipped", {"symbol": sym, "direction": d,
                                                "why": "ATR/均价不可用"})
                self._alert_throttled(
                    f"adopt:{sym}",
                    f"{sym} 交易所独有仓位无法接管:ATR 或持仓均价不可用"
                    f"(entry={entry}, atr={atr}),人工核查")
                continue
            qty = float(p.get("qty") or 0.0)
            it = {
                "oid": f"{self.OID_PREFIX}adopt{sym}{now_ms}",
                "symbol": sym, "role": "entry", "kind": "market",
                "direction": d, "qty": qty, "limit_price": None,
                "ref_px": ad.entry, "invalid": ad.invalid,
                "target_1": ad.target_1,
                "tf_label": cs.tf_label, "event_id": "ADOPTED",
                "route": "adopt_external", "decision_ts": now_ms,
                "mic": False, "size_mult": 1.0, "edge_mult": 1.0,
                "stop_source": ad.stop_source,
                "target_source": ad.target_source,
                "risk_atr": round(float(ad.risk_atr), 4),
                "adopted_breached": bool(ad.already_breached),
            }
            self._register_position(sym, it, ad.entry, now_ms, origin="adopted")
            if ad.already_breached:
                self.tg.send_alert(
                    f"{sym} 接管时现价已越过结构失效位 "
                    f"{ad.invalid:.8g}(现价 {mark:.8g})—— 下一根认知 bar 收盘"
                    f"仍在外侧则按 §8.1 出场")

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
        # ② 交易所独有仓位:先尝试**按结构接管**(默认开,见 config/live.json 的
        # adopt_external_positions)。接管成功后它已进入 self.positions,下一行
        # 的对账就不再把它报成 exchange_only —— "不触碰"从默认行为变成真接管。
        self._adopt_exchange_only(ex_pos)
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
            # 昨日事实必须取 `prev_*`:此刻 breaker 已滚到新一天,直接用
            # `start_equity` 会把"新日锚"当昨日基线 ⇒ 日报日盈亏恒 ≈ 0。
            anchor = (self.breaker.prev_start_equity
                      or self.breaker.start_equity or self.equity)
            close = self.breaker.prev_close_equity or self.equity
            dd = (anchor - close) / anchor if anchor else 0.0
            self.tg.send_daily(self._daily_date, float(close),
                               float(close) - float(anchor or 0),
                               self.counters["signals"], self.counters["fills"],
                               len(self.positions), dd)
            self._daily_date = day

    # ------------------------------------------------------------------
    # paper 虚拟经纪(canonical 撮合语义:市价=次一 2H 开盘;限价=严格穿过;
    # 超时转市价;费用 taker)
    # ------------------------------------------------------------------

    def _paper_fill_pass(self, sym: str, bar) -> None:
        ts, o, h, low, c, _v = bar
        for oid, it in list(self.open_intents.items()):
            if it["status"] != "armed" or it["symbol"] != sym:
                continue
            if ts > it["expiry_ts"]:
                if it["role"] == "entry":
                    it["status"] = "expired"
                    self.counters["expired_intents"] += 1
                    self._journal("expired", {"oid": oid, "symbol": sym})
                    if it.get("watch_entry"):
                        self._notify_watch(it, "关注到期(paper)", "等待期限已过，未开仓")
                    continue
                fill = float(o)                  # TIMEOUT_TO_TAKER
                self.counters["expired_intents"] += 1
            else:
                if it["kind"] == "market":
                    fill = float(o)
                else:
                    lp = float(it["limit_price"])
                    d = int(it["direction"])
                    if it["role"] == "entry" and it.get("watch_entry"):
                        prog_px = it.get("prog_price")
                        max_prog = it.get("max_prog_frac")
                        c0 = it.get("confirm_close")
                        tgt = it.get("target_1")
                        if prog_px is None and max_prog is not None and tgt is not None and c0 is not None:
                            span_rem = abs(float(tgt) - float(c0))
                            if span_rem > 0.0:
                                prog_px = float(c0) + d * float(max_prog) * span_rem
                        if prog_px is not None:
                            prog_f = float(prog_px)
                            already_ran = (h >= prog_f) if d > 0 else (low <= prog_f)
                            touched_watch = (low <= lp) if d > 0 else (h >= lp)
                            if already_ran and not touched_watch:
                                it["status"] = "canceled"
                                self.counters["watch_canceled_played_out"] = \
                                    self.counters.get("watch_canceled_played_out", 0) + 1
                                self._journal("watch_cancel", {"oid": oid, "symbol": sym,
                                                               "why": "MOVE_ALREADY_PLAYED_OUT",
                                                               "prog_px": prog_f})
                                self._notify_watch(it, "关注作废(paper)", "未触发前行情已走完")
                                continue
                    # role=entry:direction=开仓方向;role=reduce:direction=平仓
                    # 单方向。两种角色同判:买单等低穿、卖单等高穿(严格,maker)
                    if d > 0 and low < lp:
                        fill = lp
                    elif d < 0 and h > lp:
                        fill = lp
                    elif it["role"] == "entry" and it.get("watch_entry") and it.get("near_price") is not None:
                        near_f = float(it["near_price"])
                        near_hit = (low <= near_f) if d > 0 else (h >= near_f)
                        bounce_ok = (c > o and c >= near_f) if d > 0 else (c < o and c <= near_f)
                        if near_hit and bounce_ok:
                            fill = float(c)
                        else:
                            continue
                    else:
                        continue
            if it["role"] == "entry":
                self._paper_entry_fill(sym, oid, it, fill, ts)
            else:
                self._paper_close_fill(sym, it, fill, ts)

    def _paper_entry_fill(self, sym: str, oid: str, it: dict, price: float,
                          ts: int) -> None:
        error = entry_contract_error(int(it["direction"]), price, float(it["invalid"]),
                                     it.get("target_1"), it)
        if error:
            it["status"] = "void"
            self._journal("paper_entry_void", {"oid": oid, "symbol": sym, "why": error, "price": price})
            return
        risk = it["direction"] * (price - it["invalid"])
        if risk <= 0.0:
            it["status"] = "void"
            self.tg.send_alert(f"{sym} 成交价穿越失效位,放弃建仓"
                               f"(price={price} invalid={it['invalid']})")
            return
        if self._directional_entry_blocked(int(it["direction"]), float(it["qty"]), price,
                                           float(it["invalid"]), exclude_oid=oid):
            it["status"] = "void"
            self._journal("paper_entry_void", {"oid": oid, "symbol": sym,
                                               "why": "DIRECTIONAL_CAP", "price": price})
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
        chart_bars = (cs.cog_bars[-96:] if (cs and cs.cog_bars) else (cs.trig_bars[-96:] if cs else None))
        self.tg.send_fill(
            sym, self._close_action_label(it), qty, price,
            pnl=gross - fee, note=str(it["route"])[:60],
            tf_label=pos.get("tf_label", cs.tf_label if cs else "12h"),
            direction=pos["direction"],
            entry_price=float(pos["entry"]),      # 图 ENTRY 用真实建仓均价
            stop_price=pos.get("invalid"),
            target_price=pos.get("target_1"),
            box_lo=pos.get("box_lo"), box_hi=pos.get("box_hi"),
            box_start_ts=pos.get("box_start_ts"),
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
    runner_lock = None
    if args.mode == "live" and not args.heartbeat_path:
        lock_path = paths.runtime_dir() / "live" / "runner.lock"
        runner_lock = SingletonLock(lock_path)
        if not runner_lock.acquire():
            print(f"[live_runner] 已有實盤實例持有單實例鎖({lock_path}),本進程退出",
                  flush=True)
            return
    cred_src = cfg.get("credentials_source")
    cred_path = paths.resolve_existing(cred_src) if cred_src else None
    if cred_path is not None and not cred_path.exists():
        cred_path = None
    secrets = load_secrets(cred_path, config_overrides=cfg.get("credentials"))
    client = BitgetLiveClient(secrets)
    if args.mode == "live":
        # 持仓模式执行口径校验(fail-closed):配置与交易所不一致即拒绝启动,
        # 否则 side/tradeSide 语义错位会把平仓下成反向开仓。
        want_mode = str((cfg.get("account") or {}).get("position_mode") or "")
        probe_sym = str((cfg.get("symbols_core") or ["BTCUSDT"])[0])
        got_mode = client.position_mode(probe_sym)
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
        secrets, dropped_log=paths.runtime_dir() / "live" / "tg_missed.jsonl",
        async_mode=True)
    # 通道路由如实播报:运营通道未单独配置时会与信号同房间(宁可如此,也不静默
    # 丢告警),启动时把这件事说清楚,免得日后误以为运营信息没有外泄面。
    chan_txt = ("信号/运营 独立" if tg.ops_channel_separate
                else "运营回退信号 Bot" if tg.ops_configured else "未配置")
    print(f"[live_runner] Telegram:信号通道={'on' if tg.configured else 'off'}"
          f" / {chan_txt}", flush=True)
    contracts, contract_raw = load_contracts_live(client)   # 全量:精度/min_notional/maxLever
    runner = LiveRunner(args.mode, cfg, client, tg,
                        contracts=contracts, contract_raw=contract_raw,
                        heartbeat_path=(Path(args.heartbeat_path)
                                        if args.heartbeat_path else None),
                        symbols=tuple(cfg.get("symbols_core") or ()))
    poll = int(cfg["loop"]["poll_seconds"])
    r = cfg["risk"]
    tf_list = list((cfg.get("cognition") or {}).get("tf_list") or ["2h", "4h", "1d"])
    trig_map = (cfg.get("cognition") or {}).get("trigger_tf_by_htf") or {}
    cascade_txt = ", ".join(
        cascade_label(tf, trig_map.get(tf)) for tf in tf_list
    )
    print(f"[live_runner] mode={args.mode} 主循环起动(每 {poll}s;cascade=[{cascade_txt}];risk_frac="
          f"{r['risk_frac']} 杠杆≤{r['leverage_cap']} 组合≤"
          f"{r['portfolio_cap_frac']} 日损熔断 {r['max_daily_loss_frac']} "
          f"每时单限 {r['orders_per_hour']};宇宙 >= "
          f"{cfg['universe']['min_usdt_volume_24h']:.0f} USDT)", flush=True)
    tg.send_alert(f"live_runner 起动(mode={args.mode} | 策略={cfg.get('version', 'v22-pure-pa-amt')})\n"
                  f"⏱️ 周期级联: {cascade_txt}\n"
                  f"🛡️ 风控配置: 单笔风险 {r['risk_frac']:.1%} (动态阶梯复利) | 单周期上限 {list((r.get('tf_cap_by_tf') or {}).values())[0] if r.get('tf_cap_by_tf') else 0.15:.0%} | 组合上限 {r['portfolio_cap_frac']:.0%}\n"
                  f"📡 通知通道: 信号=公开广播(结构+执行口径), 成交/日结/告警={chan_txt}")
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
