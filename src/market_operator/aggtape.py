"""v19-orderflow aggTrades tape 特征(docs/v19-protocol-orderflow.md §2-§3;
本模块全部口径先于任何结果观察冻结)。

数据单元 = Binance vision futures/um daily aggTrades 落地日档
(data/aggtrades/{SYM}/{YYYY-MM-DD}.csv.gz,列 ts_ms, price, qty,
is_buyer_maker;fetch_aggtrades.py 契约)。taker 方向:主动买 = is_buyer_maker
== False(买方是 taker);主动卖 = is_buyer_maker == True。

接触点(v19-orderflow §2,因果窗):事件 = canonical 语义重放
(research.collect_events 原样,Params.from_json(config/canonical.json),
BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED/STRUCTURE_BROKEN/FLIP_RETESTED,enable_e7=True);接触时刻 t_contact = known bar 收盘 =
known_ts + 3_600_000(1h bar 时间戳 = 开盘时刻,收盘即知时,A10 纪律)。
全部 tape 特征只用 [t_contact − W, t_contact) 的 trade 行 —— 严格 trailing,
不含收盘时刻本身(ts == t_contact 的 trade 属于下一根 bar,排除;因果)。

特征(v19-orderflow §3 逐字;分母为 0 或窗内行不足一律 NaN,不编造):
- delta_share_{15,60}m = (主动买名义 − 主动卖名义)/总名义,窗口 [t−W, t),
  名义 = price*qty(USDT);
- cvd_slope_60m = 窗口 [t−60m, t) 内 1 分钟桶 signed-qty(+主动买/−主动卖,
  基币)累计序列对分钟序号 0..59 的最小二乘斜率(基币/分钟),除以
  该窗总名义/60(即平均每分钟名义)作归一;
- big_share_15m = [t−15m, t) 内按名义降序 Top-10% 交易(笔数上取整,
  k = max(1, ceil(0.10*n)))的名义合计/总名义;
- eff_15m = |最后一个价 − 第一个价| / 总名义([t−15m, t) 内首尾 trade 价);
  吸收签名读法 = 小 eff + 大 vol;
- vol15_rel = [t−15m, t) 名义 / UTC 对齐 15 分钟桶名义中位;中位取自桶
  b(t) = floor(t/15m) 之前(不含 b(t))的最近至多 96 桶(24h);可用过去
  桶 < 48 记 NaN(warmup 如实声明),48..95 用全部可用,≥96 用最近 96;
  中位 ≤ 0 记 NaN。

内存纪律:单日载入-聚合-落盘-丢弃;全程不驻全部原始行。两遍流式:
pass 1 只累计 UTC 15m 名义网格(全窗每天 96 桶,float64 小数组);
pass 2 按时间序处理特征请求,仅在 RAM 保留"当日 + 前一日尾 61 分钟"
(tail_ms)原始行,逐请求 searchsorted 切片。特征请求 = 接触点与对照点,
按 t 升序喂入。

确定性:搜索/分箱/多项式拟合均确定性实现;同输入 ⇒ 逐位同输出(测试守护)。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .events import BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, STRUCTURE_BROKEN, FLIP_RETESTED
from .research import collect_events
from .structure import Params

# --- 冻结口径常量(v19-orderflow §2-§3;先于结果观察) --------------------------
WIN_15M_MS = 900_000
WIN_60M_MS = 3_600_000
MS_PER_DAY = 86_400_000
BAR_MS = 3_600_000                     # 1h 认知帧;t_contact = known_ts + BAR_MS
GRID_MS = 900_000                      # vol15_rel UTC 15m 网格
VOL_LOOKBACK_BUCKETS = 96              # 24h = 96 桶
VOL_MIN_BUCKETS = 48                   # warmup 下限(如实声明)
CVD_MINUTES = 60                       # cvd_slope 分钟桶数
CVD_GRID_MS = 60_000
BIG_TOP_FRAC = 0.10                    # big_share Top-10% 笔数占比
TAIL_MS = WIN_60M_MS + 60_000          # 前一日尾保留(61 分钟余量)

STUDY_EVENT_IDS = frozenset({BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, STRUCTURE_BROKEN, FLIP_RETESTED})   # v19-orderflow §2
REJECTION_EVENT_IDS = frozenset({BREAKOUT_FAILED, LEVEL_REJECTED})           # §4 H2 拒绝类

FEATURE_NAMES = ("delta_share_15m", "delta_share_60m", "cvd_slope_60m",
                 "big_share_15m", "eff_15m", "vol15_rel")


# ---------------------------------------------------------------------------
# 1. 日档载入与切片(向量化;单日驻存)
# ---------------------------------------------------------------------------


def load_day(path: str | Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """读一日 csv.gz → (ts_ms int64, price f64, qty f64, maker bool)。

    落地文件由 fetch_aggtrades.validate 保证非降序;此处防御性断言单调,
    不达标即抛错(切片 searchsorted 依赖有序)。
    """
    df = pd.read_csv(path, dtype={"ts_ms": "int64", "price": "float64",
                                  "qty": "float64", "is_buyer_maker": bool})
    ts = df["ts_ms"].to_numpy(np.int64)
    if len(ts) > 1 and bool((ts[1:] < ts[:-1]).any()):
        raise RuntimeError(f"{path}: ts_ms not non-decreasing")
    return (ts, df["price"].to_numpy(np.float64),
            df["qty"].to_numpy(np.float64),
            df["is_buyer_maker"].to_numpy(bool))


def window_slice(ts: np.ndarray, lo: int, hi: int) -> slice:
    """[lo, hi) 的行切片(ts 非降,searchsorted;hi = t_contact 严格排他)。"""
    return slice(int(np.searchsorted(ts, lo, side="left")),
                 int(np.searchsorted(ts, hi, side="left")))


# ---------------------------------------------------------------------------
# 2. 特征纯函数(离线可测;NaN 口径见模块 docstring)
# ---------------------------------------------------------------------------


def _notional(price: np.ndarray, qty: np.ndarray) -> np.ndarray:
    return np.asarray(price, dtype=np.float64) * np.asarray(qty, dtype=np.float64)


def delta_share(price: np.ndarray, qty: np.ndarray,
                maker: np.ndarray) -> float:
    """(主动买名义 − 主动卖名义)/总名义;空窗或总名义 ≤ 0 ⇒ NaN。"""
    if len(price) == 0:
        return float("nan")
    notion = _notional(price, qty)
    total = float(notion.sum())
    if total <= 0.0:
        return float("nan")
    sell_notional = float(notion[maker.astype(bool)].sum())
    buy_notional = total - sell_notional
    return (buy_notional - sell_notional) / total


def cvd_slope(ts: np.ndarray, price: np.ndarray, qty: np.ndarray,
              maker: np.ndarray, win_start_ms: int,
              win_ms: int = WIN_60M_MS,
              n_buckets: int = CVD_MINUTES) -> float:
    """窗口内分钟 CVD 最小二乘斜率 / 平均每分钟名义(冻结口径见 docstring)。

    分钟桶序号 = (ts − win_start)//CVD_GRID_MS,截到 [0, n_buckets−1](防御
    越界行);空窗或名义 ≤ 0 ⇒ NaN。
    """
    n = len(price)
    if n == 0:
        return float("nan")
    notion = _notional(price, qty)
    total = float(notion.sum())
    if total <= 0.0:
        return float("nan")
    sign = np.where(np.asarray(maker, dtype=bool), -1.0, 1.0)
    idx = ((np.asarray(ts, dtype=np.int64) - int(win_start_ms))
           // CVD_GRID_MS).astype(np.int64)
    idx = np.clip(idx, 0, n_buckets - 1)
    minute_delta = np.bincount(idx, weights=sign * np.asarray(qty, dtype=np.float64),
                               minlength=n_buckets)
    cvd = np.cumsum(minute_delta)
    x = np.arange(n_buckets, dtype=np.float64)
    slope = float(np.polyfit(x, cvd, 1)[0])      # 基币/分钟
    return slope / (total / n_buckets)


def big_share(price: np.ndarray, qty: np.ndarray,
              top_frac: float = BIG_TOP_FRAC) -> float:
    """Top-10% 交易(按名义,k = max(1, ceil(frac*n)))名义占比;空窗 ⇒ NaN。"""
    n = len(price)
    if n == 0:
        return float("nan")
    notion = _notional(price, qty)
    total = float(notion.sum())
    if total <= 0.0:
        return float("nan")
    k = max(1, int(math.ceil(top_frac * n)))
    if k >= n:
        return 1.0
    topk = np.partition(notion, n - k)[n - k:]
    return float(topk.sum()) / total


def eff(price: np.ndarray, qty: np.ndarray) -> float:
    """|尾价 − 首价| / 总名义(窗内首尾 trade);行 < 2 或名义 ≤ 0 ⇒ NaN。"""
    n = len(price)
    if n < 2:
        return float("nan")
    total = float(_notional(price, qty).sum())
    if total <= 0.0:
        return float("nan")
    return abs(float(price[-1]) - float(price[0])) / total


def vol15_rel(notional_15: float, prev_bucket_notionals: np.ndarray) -> float:
    """15m 名义 / 过去桶中位;过去桶 < VOL_MIN_BUCKETS 或中位 ≤ 0 ⇒ NaN。

    prev_bucket_notionals = b(t) 之前(不含)按时间升序的 UTC 15m 桶名义;
    取最近至多 VOL_LOOKBACK_BUCKETS 桶。
    """
    avail = np.asarray(prev_bucket_notionals, dtype=np.float64)
    avail = avail[~np.isnan(avail)]
    if len(avail) < VOL_MIN_BUCKETS:
        return float("nan")
    med = float(np.median(avail[-VOL_LOOKBACK_BUCKETS:]))
    if med <= 0.0 or not np.isfinite(notional_15) or notional_15 < 0.0:
        return float("nan")
    return float(notional_15) / med


def _notional_15(price: np.ndarray, qty: np.ndarray) -> float:
    if len(price) == 0:
        return 0.0
    return float(_notional(price, qty).sum())


def features_at(ts: np.ndarray, price: np.ndarray, qty: np.ndarray,
                maker: np.ndarray, t_contact: int,
                grid: np.ndarray | None = None,
                grid_start_ms: int = 0) -> dict:
    """单接触点六特征(t = t_contact;输入 = 覆盖 [t−60m, t) 的有序 tape 块)。

    grid/grid_start_ms:UTC 15m 名义网格(vol15_rel 分母;None ⇒ vol15_rel=NaN,
    调用方声明)。返回 FEATURE_NAMES + n_trades_{15,60}m + notional_15m。
    """
    s60 = window_slice(ts, t_contact - WIN_60M_MS, t_contact)
    s15 = window_slice(ts, t_contact - WIN_15M_MS, t_contact)
    p60, q60, m60, t60 = price[s60], qty[s60], maker[s60], ts[s60]
    p15, q15, m15 = price[s15], qty[s15], maker[s15]

    notion15 = _notional_15(p15, q15)
    vol_rel = float("nan")
    if grid is not None:
        b = (int(t_contact) - int(grid_start_ms)) // GRID_MS
        if b > 0:
            vol_rel = vol15_rel(notion15, grid[:b])
    return {
        "delta_share_15m": delta_share(p15, q15, m15),
        "delta_share_60m": delta_share(p60, q60, m60),
        "cvd_slope_60m": cvd_slope(t60, p60, q60, m60,
                                   t_contact - WIN_60M_MS),
        "big_share_15m": big_share(p15, q15),
        "eff_15m": eff(p15, q15),
        "vol15_rel": vol_rel,
        "notional_15m": notion15,
        "n_trades_15m": int(s15.stop - s15.start),
        "n_trades_60m": int(s60.stop - s60.start),
    }


# ---------------------------------------------------------------------------
# 3. 流式两遍(逐日载入-聚合-丢弃;不驻全量原始行)
# ---------------------------------------------------------------------------


def notional_grid(paths: list, grid_ms: int = GRID_MS,
                  grid_start_ms: int = 0) -> tuple[np.ndarray, int]:
    """pass 1:日档序列 → UTC 15m 名义网格(pass 2 的 vol15_rel 分母)。

    paths 按日升序;grid_start_ms = 第一日 00:00 UTC(缺省 0 ⇒ x轴对齐绝对
    epoch 桶,返回 grid_start 供索引换算)。返回 (grid, grid_start_ms),
    grid[i] = [grid_start + i*grid_ms, +(i+1)*grid_ms) 的名义合计。
    """
    if not paths:
        raise ValueError("no day paths")
    if grid_start_ms == 0:
        first_ts = pd.read_csv(paths[0], usecols=["ts_ms"])["ts_ms"]
        grid_start_ms = int(first_ts.iloc[0]) // grid_ms * grid_ms
    acc: np.ndarray | None = None
    for path in paths:
        ts, price, qty, _ = load_day(path)
        idx = ((ts - grid_start_ms) // grid_ms).astype(np.int64)
        need = int(idx.max()) + 1
        bc = np.bincount(idx, weights=_notional(price, qty),
                         minlength=need).astype(np.float64)
        if acc is None:
            acc = bc
            continue
        if len(bc) > len(acc):
            acc = np.concatenate([acc, np.zeros(len(bc) - len(acc))])
        acc[: len(bc)] += bc
    if acc is None:
        raise ValueError("no day paths")
    return acc, grid_start_ms


def stream_features(paths: list, t_contacts: list,
                    grid: np.ndarray | None = None,
                    grid_start_ms: int = 0) -> pd.DataFrame:
    """pass 2:按 t 升序的请求序列计算全部接触点特征(流式,尾 61m 驻存)。

    每个请求 = 一个 t_contact(ms)。当日 + 前一日尾(≥ t−61m)在 RAM;
    进入新一日前丢弃更早原始行。返回按请求序(rows = len(t_contacts))的
    特征帧(FEATURE_NAMES + n_trades_*+ notional_15m + t_contact)。
    窗口左沿早于首日数据的请求仍出特征(实际可得窗 < 名义窗;n_trades_60m
    与 tape 覆盖率如实反映,调用方按 warmup 声明处理)。t 超出最后一日
    日历域的请求以尾块切片(覆盖外窗 = 空窗 ⇒ 相关特征 NaN)。
    """
    order = sorted({int(t) for t in t_contacts})   # 去重;重复请求由 reindex 还原
    rows: list[dict] = []
    req_i = 0
    tail: tuple | None = None            # (ts, price, qty, maker) 尾 61m
    for path in paths:
        ts, price, qty, maker = load_day(path)
        if len(ts) == 0:
            continue
        day_close = (int(ts[0]) // MS_PER_DAY) * MS_PER_DAY + MS_PER_DAY
        if tail is not None:
            ts = np.concatenate([tail[0], ts])
            price = np.concatenate([tail[1], price])
            qty = np.concatenate([tail[2], qty])
            maker = np.concatenate([tail[3], maker])
        while req_i < len(order) and order[req_i] < day_close:
            t = order[req_i]
            rows.append({"t_contact": t,
                         **features_at(ts, price, qty, maker, t,
                                       grid=grid, grid_start_ms=grid_start_ms)})
            req_i += 1
        cut = int(np.searchsorted(ts, day_close - TAIL_MS, side="left"))
        tail = (ts[cut:], price[cut:], qty[cut:], maker[cut:])
    # 末端:t 超出最后一日日历域的请求以尾块切片(出覆盖的窗 ⇒ 空窗 NaN,
    # warmup 如实声明,调用方按覆盖率处理)。
    while req_i < len(order):
        t = order[req_i]
        block = tail if tail is not None else (np.array([], dtype=np.int64),
                                               np.array([], dtype=np.float64),
                                               np.array([], dtype=np.float64),
                                               np.array([], dtype=bool))
        rows.append({"t_contact": t,
                     **features_at(block[0], block[1], block[2], block[3], t,
                                   grid=grid, grid_start_ms=grid_start_ms)})
        req_i += 1
    out = pd.DataFrame(rows).set_index("t_contact")
    return out.reindex([int(t) for t in t_contacts]).reset_index()


# ---------------------------------------------------------------------------
# 4. 接触点 metadata(bar-side canonical 认知;v19-orderflow §2)
# ---------------------------------------------------------------------------


def collect_contacts(df_bars: pd.DataFrame, params: Params,
                     event_ids: frozenset = STUDY_EVENT_IDS,
                     enable_e7: bool = True,
                     ts_lo: int | None = None,
                     ts_hi: int | None = None) -> tuple:
    """canonical 事件重放 → 接触点列表(t_contact = known bar 收盘)。

    ts_lo/ts_hi(ms,含):t_contact 落在该窗内才收(研究窗;t_contact 因
    known_ts + BAR_MS 可与 bar 帧尾部错位,调用方负责 tape 覆盖)。
    返回 (contacts: pd.DataFrame[event_id, direction, mechanism, known_index,
    known_ts, t_contact, regime, atr], timeline: StructureTimeline,
    fired: 全部回放事件(未按 ts 窗过滤;供同机制排除窗等研究层使用))。
    """
    fired, timeline = collect_events(df_bars, params, event_ids=event_ids,
                                     enable_e7=enable_e7)
    rows: list[dict] = []
    for uid, e in enumerate(fired):
        t_contact = int(e.ts) + BAR_MS
        if ts_lo is not None and t_contact < ts_lo:
            continue
        if ts_hi is not None and t_contact > ts_hi:
            continue
        rows.append({
            "uid": uid,
            "event_id": e.id,
            "direction": int(e.direction),
            "mechanism": f"{e.id}|{'long' if e.direction > 0 else 'short'}",
            "known_index": int(e.known_index),
            "known_ts": int(e.ts),
            "t_contact": t_contact,
            "regime": timeline.regime[e.known_index],
            "atr": float(timeline.atr[e.known_index]),
        })
    return pd.DataFrame(rows), timeline, fired
