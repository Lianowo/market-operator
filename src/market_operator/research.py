"""事件研究计算核心(docs/methodology.md §9,阶段 3)。

与 §9.1 七步流程一一对应:

1. `collect_events(df, params)`:全历史严格时间序回放(structure.replay 同路的
   EventDetector),逐 bar 记录 regime/atr/zones 快照(StructureTimeline,供对照
   组采样),事件 bar 另存 StructureView(供 §7 假设构建)。
2. `measure_symbol`:事件 +1 根 bar 开盘建立理论头寸(§9.1-3,无持仓管理),
   H ∈ {6,12,24,48} 内测 MFE/MAE(以事件时 ATR 为单位)、target/invalid 命中
   (同 bar 双命中按保守口径记 invalid 优先)、固定 H 收盘退出的毛/净收益
   (ATR 与 % 双口径;净 = 毛 − taker 成本模型,成本从 config/defaults.json 读,
   持仓 H 小时,funding 按 0.01%/8h 双边保守收取 §9.2)。
3. 事件方向映射(§9.1-3 口径,与 events.py 的发射语义硬核对,见
   `expected_direction`;collect_events 对每个事件断言,不符即抛错):
   E1 顺势(阻力破位 → 多 / 支撑破位 → 空);E2 反向(刺穿收回 → 阻力空 / 支撑多);
   E3 区域正面反向(阻力区拒绝 → 空,支撑区拒绝 → 多);E4 顺势(TREND_UP 恢复 → 多);
   E5 向对侧边缘(跟随来源 E3:上缘拒绝 → 空);E6 反向(TREND_UP 破坏 → 空)。
4. 对照组(§9.1-5):同品种、同 regime、同方向、且决策 bar 不在**同机制**事件
   窗口 [known_index, known_index+24] 内的随机入场(口径见 measure_controls
   docstring);42 个固定种子(SEED_BASE+i,完全可复现),测同样指标(MFE/MAE
   与毛/净收益;无结构 invalid/target,命中类指标不适用)。
   事件"增量" = 事件净期望 − 该事件 42 个对照样本净期望均值。
5. 统计:按自然日(UTC)分块 block bootstrap(2000 次,固定种子)给增量/event
   净期望的 95% CI;前/后半样本(按事件 ts 中位切分)稳定性;按 regime 分解;
   成本压力 1x/1.5x/2x(§9.1-7)。
6. dev/test:每品种前 dev_frac(config, 0.7)为 dev,其余 test(dev 分界 ts/索引
   由 `split_boundary` 给出并写入报告)。全部指标对两段分别给出;
   **GO/NO-GO 判据只在 test 段评估**(§9.3 照抄,判据中"净期望"的解释见下)。

GO/NO-GO 的"净期望"口径解释(docstring 冻结,先于结果观察):
   §9.3 判据 2/3/4 中的"净期望"判定口径取**事件净期望相对对照组的增量**
   (increment = event net − mean(control net))。理由:§9.1-5 明确要求"事件
   必须表现出增量信息,不仅是有方向的波动",§9.3 的本意是判事件机制是否提供
   增量信息而非判方向性行情本身;控制组与事件付同一成本模型,故增量对成本
   压力近似不变(1x/1.5x/2x 下几乎平行),这一性质在报告中如实展示。
   主口径:ATR 单位;% (USDT 名义)口径并列报告。判据 2 要求双口径均 > 0。

主视界声明(先于结果观察固定):H = 24(= §7 先验 N_time 假设失效根数)。
全部 H ∈ {6,12,24,48} 的指标照常报告;判据只在 H=24 上评估。

事件样本不足的机制(判据 1,test 段全品种合计 < 60,或对照池为空、或正向
窗不足)标记 INSUFFICIENT_DATA,不给 GO/NO-GO 之外的解读。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .events import E1, E2, E3, E4, E5, E6, E7, EventDetector, MarketEvent
from .hypothesis import Hypothesis, StructureView, build
from .structure import Params, Regime, ZoneRole

# ---------------------------------------------------------------------------
# 研究协议常量(§9 冻结;非策略先验,是协议参数)
# ---------------------------------------------------------------------------

HORIZONS = (6, 12, 24, 48)          # §9.1-4
PRIMARY_HORIZON = 24                # 主视界 = §7 N_time 先验(先于结果声明)
COST_MULTS = (1.0, 1.5, 2.0)        # §9.1-7 成本压力
N_SEEDS = 42                        # §9.1-5:42 个固定种子
SEED_BASE = 2026_1019               # 固定种子基
BOOT_REPS = 2000                    # §9.1-6 block bootstrap 次数
BOOT_SEED = 2026_1019
EVENT_EXCL_BARS = 24                # §9.1-5:排除事件后 24 bar 窗口
MIN_EVENTS_GO = 60                  # §9.3-1(全品种合计)
MIN_SYMBOLS_POSITIVE = 2            # §9.3-4
MS_PER_DAY = 86_400_000

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "defaults.json"


# ---------------------------------------------------------------------------
# 成本模型(§9.2 先验,来自 config/defaults.json;不允许在研究中修改)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostModel:
    """taker 双程成本。funding 按持仓小时数、双边保守收取(§9.2)。"""

    taker_fee: float                # 每边
    slippage_per_side: float        # 每边
    funding_per_8h_per_side: float  # 每边每 8h

    @classmethod
    def from_config(cls, path=DEFAULT_CONFIG) -> "CostModel":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(taker_fee=float(raw["taker_fee"]),
                   slippage_per_side=float(raw["slippage_per_side"]),
                   funding_per_8h_per_side=float(raw["funding_per_8h_per_side"]))

    def round_trip_frac(self, holding_hours: float, mult: float = 1.0) -> float:
        base = (2.0 * self.taker_fee + 2.0 * self.slippage_per_side
                + 2.0 * self.funding_per_8h_per_side * (holding_hours / 8.0))
        return mult * base


# ---------------------------------------------------------------------------
# 方向映射硬核对(§9.1-3)
# ---------------------------------------------------------------------------


def expected_direction(event: MarketEvent) -> int:
    """§9.1-3 冻结方向映射下该事件应有的方向(+1/-1)。"""
    a = event.attrs
    if event.id == E1:      # 顺势:阻力破位 → 多;支撑破位 → 空
        return +1 if a["zone"].role is ZoneRole.RESISTANCE else -1
    if event.id in (E2, E3, E5):   # 反向 / 区域正面反向 / E5 跟随来源 E3
        return -1 if a["zone"].role is ZoneRole.RESISTANCE else +1
    if event.id == E4:      # 顺势:TREND_UP 恢复 → 多(回调腿:前高 > 回调低点)
        # 注意不能用 attrs["regime"](发射 bar 的 regime,激活后可能已变);
        # 独立可核对证据:多头回调腿 leg_start(前高 pivot) > pb_price(L_pb)。
        return +1 if a["leg_start"] > a["pb_price"] else -1
    if event.id == E6:      # 反向:TREND_UP 破坏 → 空
        return -1 if a["regime_before"] == Regime.TREND_UP.value else +1
    if event.id == E7:      # v2 FLIP_RETEST(v2-protocol §1):role_new 阻力 → 空;
        # from_role(旧区角色)为独立可核对证据:旧支撑破位 ⇒ role_new=resistance
        return -1 if a["from_role"] == ZoneRole.SUPPORT.value else +1
    raise ValueError(f"unknown event id: {event.id}")


# ---------------------------------------------------------------------------
# 1. 事件采集与结构时间线
# ---------------------------------------------------------------------------


@dataclass
class StructureTimeline:
    """逐 bar 认知快照(供对照组按 regime 采样)与事件 bar 的结构视图。"""

    regime: np.ndarray            # str, 每 bar regime(t 收盘已知)
    atr: np.ndarray               # float64, 每 bar atr(t)
    zones: list                   # 每 bar tuple[(lo, hi, role, state)](事实快照)
    views: dict                   # known_index -> StructureView(仅事件 bar)


def collect_events(df: pd.DataFrame, params: Params,
                   event_ids: Optional[frozenset] = None,
                   enable_e7: bool = False) -> tuple:
    """严格时间序回放,返回 (events, StructureTimeline)。

    每个事件的方向按 §9.1-3 映射硬核对一次(不一致即 ValueError)。
    `event_ids` 为可选机制过滤器(v2 事件研究用,如 frozenset({"E7"}));
    None = 全量(v1 路径,行为不变)。`enable_e7` 打开 v2 E7 发射(v1 默认关)。
    """
    detector = EventDetector(params, enable_e7=enable_e7)
    n = len(df)
    regimes = np.empty(n, dtype=object)
    atrs = np.zeros(n, dtype=np.float64)
    zones_snap: list = []
    views: dict = {}
    events: list[MarketEvent] = []

    for row in df[["ts", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        bar = (row.ts, row.open, row.high, row.low, row.close, row.volume)
        emitted = detector.update(bar)
        if event_ids is not None:
            emitted = [e for e in emitted if e.id in event_ids]
        s = detector.structure
        t = s.bars_seen - 1
        regimes[t] = s.regime().value
        atrs[t] = s.atr()
        zones_snap.append(tuple((z.lo, z.hi, z.role.value, z.state.value)
                                for z in s._zones.zones))
        if emitted:
            views[t] = StructureView(regime=s.regime(), zones=s.zones, atr=s.atr())
            events.extend(emitted)

    for e in events:
        exp = expected_direction(e)
        if e.direction != exp:
            raise ValueError(
                f"direction mismatch {e.id}@{e.known_index}: "
                f"emitted {e.direction}, §9.1-3 expects {exp}")
    return events, StructureTimeline(regime=regimes, atr=atrs,
                                     zones=zones_snap, views=views)


# ---------------------------------------------------------------------------
# 2. 理论头寸测量(§9.1-3/4)
# ---------------------------------------------------------------------------


def split_boundary(n_bars: int, dev_frac: float) -> int:
    """dev/test 分界 bar 索引:known_index < boundary 为 dev,否则 test。"""
    return int(n_bars * dev_frac)


def _mult_tag(mult: float) -> str:
    return str(mult).replace(".", "_")


def measure_path(o, h, l, c, entry_i: int, horizon: int, direction: int,
                 invalid: Optional[float], target: Optional[float]) -> tuple:
    """在 bars [entry_i, entry_i+horizon) 上测 MFE/MAE/毛收益/命中。

    返回 (mfe, mae, gross, hit);价格单位。hit ∈ {"target","invalid",None},
    同 bar 双命中按保守口径记 "invalid"(§9 任务书口径)。
    """
    hh = h[entry_i:entry_i + horizon]
    ll = l[entry_i:entry_i + horizon]
    entry = o[entry_i]
    if direction > 0:
        mfe = float(hh.max()) - entry
        mae = entry - float(ll.min())
    else:
        mfe = entry - float(ll.min())
        mae = float(hh.max()) - entry
    gross = direction * (float(c[entry_i + horizon - 1]) - entry)

    hit = None
    if invalid is not None:
        for j in range(horizon):
            if direction > 0:
                inv = ll[j] <= invalid
                tgt = target is not None and hh[j] >= target
            else:
                inv = hh[j] >= invalid
                tgt = target is not None and ll[j] <= target
            if inv and tgt:
                hit = "invalid"      # 保守:同 bar 双命中记 invalid 优先
                break
            if inv:
                hit = "invalid"
                break
            if tgt:
                hit = "target"
                break
    return mfe, mae, gross, hit


def _net_row(mfe, mae, gross, hit, entry, atr, horizon, cost: CostModel) -> dict:
    gross_atr = gross / atr
    gross_pct = gross / entry
    row = {"mfe_atr": mfe / atr, "mae_atr": mae / atr,
           "gross_atr": gross_atr, "gross_pct": gross_pct, "hit": hit}
    for m in COST_MULTS:
        frac = cost.round_trip_frac(horizon, m)
        tag = _mult_tag(m)
        row[f"net_atr_{tag}"] = gross_atr - frac * entry / atr
        row[f"net_pct_{tag}"] = gross_pct - frac
    return row


def measure_events(df: pd.DataFrame, events: list, timeline: StructureTimeline,
                   cost: CostModel) -> pd.DataFrame:
    """对每个事件建 §7 假设并按 §9.1-3/4 测量(全部 H × 成本压力)。

    前向窗不足 max(HORIZONS) 的事件整体跳过(保证各 H 样本一致,记入
    n_dropped_tail 由调用方报告)。
    """
    o = df["open"].to_numpy(np.float64)
    h = df["high"].to_numpy(np.float64)
    l = df["low"].to_numpy(np.float64)
    c = df["close"].to_numpy(np.float64)
    n = len(df)
    max_h = max(HORIZONS)
    rows: list[dict] = []

    for uid, e in enumerate(events):
        entry_i = e.known_index + 1
        if entry_i + max_h - 1 >= n:
            continue                              # 前向窗不足(数据尾部)
        view = timeline.views[e.known_index]
        atr = float(view.atr)
        if atr <= 0.0:
            continue
        hypo = build(e, view, None)
        if hypo is None:
            continue
        hypo = hypo.with_entry(float(o[entry_i]))
        base = {
            "uid": uid,                           # 同 bar 同 id 双事件去键
            "event_id": e.id, "direction": e.direction,
            "mechanism": f"{e.id}|{'long' if e.direction > 0 else 'short'}",
            "ts": int(e.ts), "known_index": e.known_index,
            "regime": hypo.regime.value,
            "entry": hypo.entry, "invalid": hypo.invalid,
            "target_1": hypo.target_1, "r_mult": hypo.r_mult, "atr": atr,
        }
        for horizon in HORIZONS:
            mfe, mae, gross, hit = measure_path(
                o, h, l, c, entry_i, horizon, e.direction,
                hypo.invalid, hypo.target_1)
            rows.append({**base, "H": horizon,
                         **_net_row(mfe, mae, gross, hit, hypo.entry, atr,
                                    horizon, cost)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 4. 对照组(§9.1-5)
# ---------------------------------------------------------------------------


def exclusion_mask(n: int, events: list) -> np.ndarray:
    """决策 bar i 落在任一事件窗口 [known_index, known_index+24] 内 ⇒ 排除。"""
    mask = np.zeros(n, dtype=bool)
    for e in events:
        lo = e.known_index
        hi = min(n, e.known_index + EVENT_EXCL_BARS + 1)
        mask[lo:hi] = True
    return mask


def control_pool(timeline: StructureTimeline, seg_lo: int, seg_hi: int,
                 regime: str, excl: np.ndarray, max_h: int) -> np.ndarray:
    """同 regime、同段、不在事件窗口、前向窗足够的决策 bar 索引。"""
    idx = np.arange(seg_lo, seg_hi)
    ok = ~excl[idx]
    ok &= np.array([timeline.regime[i] == regime for i in idx])
    ok &= idx + 1 + max_h - 1 < len(timeline.atr)
    return idx[ok]


def measure_controls(df: pd.DataFrame, events: list, timeline: StructureTimeline,
                     event_rows: pd.DataFrame, cost: CostModel,
                     seg_lo: int, seg_hi: int) -> pd.DataFrame:
    """每事件 × 42 固定种子:同品种/同 regime/同方向/避事件窗口的随机入场。

    "避开事件窗口"(§9.1-5)的口径:**同机制**(event_id × direction)事件的
    [known_index, known_index+24] 窗口——对照语义是"无该机制信号的随机入场"。
    (全事件排除在本事件密度下会排除 >99% 的 bar,池退化为病态残差,见
    docs/research-log.md 条目 4d。)
    返回每事件 × H 的对照样本聚合(均值/标准差)与池规模 pool_n;完整样本不复存
    (可复现:种子固定)。池为空 ⇒ 该行 n_ctrl = 0,调用方据此标 INSUFFICIENT_DATA。
    """
    o = df["open"].to_numpy(np.float64)
    h = df["high"].to_numpy(np.float64)
    l = df["low"].to_numpy(np.float64)
    c = df["close"].to_numpy(np.float64)
    max_h = max(HORIZONS)
    by_mech: dict = {}
    for e in events:
        key = (e.id, "long" if e.direction > 0 else "short")
        by_mech.setdefault(key, []).append(e)
    pool_cache: dict = {}

    rows: list[dict] = []
    for ev in event_rows[["uid", "known_index", "event_id", "direction",
                          "mechanism", "regime", "H"]].itertuples(index=False):
        key = (ev.mechanism, ev.regime)
        if key not in pool_cache:
            m_id, m_dir = ev.mechanism.split("|")
            excl = exclusion_mask(
                len(df), by_mech.get((m_id, m_dir), []))
            pool_cache[key] = control_pool(timeline, seg_lo, seg_hi,
                                           ev.regime, excl, max_h)
        pool = pool_cache[key]
        nets: dict = {m: [] for m in COST_MULTS}
        mfes, maes, grosses = [], [], []
        for seed_i in range(N_SEEDS):
            rng = np.random.default_rng(SEED_BASE + seed_i)
            if len(pool) == 0:
                break
            i = int(pool[rng.integers(len(pool))])
            entry_i = i + 1
            atr = float(timeline.atr[i])
            if atr <= 0.0:
                continue
            mfe, mae, gross, _ = measure_path(o, h, l, c, entry_i, ev.H,
                                              ev.direction, None, None)
            r = _net_row(mfe, mae, gross, None, float(o[entry_i]), atr,
                         ev.H, cost)
            mfes.append(r["mfe_atr"])
            maes.append(r["mae_atr"])
            grosses.append(r["gross_atr"])
            for m in COST_MULTS:
                nets[m].append((r[f"net_atr_{_mult_tag(m)}"],
                                r[f"net_pct_{_mult_tag(m)}"]))
        row = {"uid": ev.uid, "known_index": ev.known_index,
               "event_id": ev.event_id, "mechanism": ev.mechanism,
               "H": ev.H, "n_ctrl": len(mfes), "pool_n": int(len(pool))}
        if mfes:
            row["ctrl_mfe_atr_mean"] = float(np.mean(mfes))
            row["ctrl_mae_atr_mean"] = float(np.mean(maes))
            row["ctrl_gross_atr_mean"] = float(np.mean(grosses))
            row["ctrl_gross_atr_std"] = float(np.std(grosses))
            for m in COST_MULTS:
                tag = _mult_tag(m)
                row[f"ctrl_net_atr_mean_{tag}"] = float(np.mean([x[0] for x in nets[m]]))
                row[f"ctrl_net_pct_mean_{tag}"] = float(np.mean([x[1] for x in nets[m]]))
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 5. 统计(§9.1-6)
# ---------------------------------------------------------------------------


def block_bootstrap_ci(values: np.ndarray, ts_ms: np.ndarray,
                       reps: int = BOOT_REPS, seed: int = BOOT_SEED) -> tuple:
    """按自然日(UTC)分块 block bootstrap,返回均值统计量的 95% CI。

    以日聚合(和/计数)后向量化抽样:每次重复 = 有放回抽 n_days 个日块的
    总和均值,与逐事件重抽等价(日块内事件全体随块走,块即抽样单元)。
    """
    vals = np.asarray(values, dtype=np.float64)
    if len(vals) == 0:
        return (float("nan"), float("nan"))
    days = np.asarray(ts_ms, dtype=np.int64) // MS_PER_DAY
    uniq, inverse = np.unique(days, return_inverse=True)
    n_days = len(uniq)
    day_sums = np.bincount(inverse, weights=vals)
    day_counts = np.bincount(inverse).astype(np.float64)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, n_days, size=(reps, n_days))
    stats = day_sums[draws].sum(axis=1) / day_counts[draws].sum(axis=1)
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


def _by(rows: pd.DataFrame, col: str, val: str) -> dict:
    out = {}
    for k, g in rows.groupby(col):
        out[str(k)] = {"n": int(len(g)), "mean": float(g[val].mean())}
    return out


def analyze_segment(event_rows: pd.DataFrame, ctrl_rows: pd.DataFrame) -> dict:
    """单机制 × 单段的全套指标(H × 成本压力;增量 = 事件 − 对照均值)。"""
    on = ["uid", "known_index", "event_id", "mechanism", "H"]
    for extra in ("symbol", "segment"):   # 两表都有 ⇒ 进键,避免 _x/_y 后缀丢列
        if extra in event_rows.columns and extra in ctrl_rows.columns:
            on.append(extra)
    merged = event_rows.merge(ctrl_rows, on=on, how="left")
    for m in COST_MULTS:
        tag = _mult_tag(m)
        merged[f"incr_atr_{tag}"] = (merged[f"net_atr_{tag}"]
                                     - merged[f"ctrl_net_atr_mean_{tag}"])
        merged[f"incr_pct_{tag}"] = (merged[f"net_pct_{tag}"]
                                     - merged[f"ctrl_net_pct_mean_{tag}"])

    out: dict = {"n_events": int(merged["uid"].nunique()),
                 "horizons": {}}
    if out["n_events"] == 0:
        return out

    for horizon in HORIZONS:
        g = merged[merged["H"] == horizon]
        h_out: dict = {"n": int(g["uid"].nunique()),
                       "mfe_atr_mean": float(g["mfe_atr"].mean()),
                       "mae_atr_mean": float(g["mae_atr"].mean()),
                       "gross_atr_mean": float(g["gross_atr"].mean()),
                       "has_target_frac": float(g["target_1"].notna().mean()),
                       "hit": {k: float((g["hit"] == k).mean())
                               for k in ("target", "invalid")},
                       "cost": {}}
        for m in COST_MULTS:
            tag = _mult_tag(m)
            inc = g.dropna(subset=[f"incr_atr_{tag}"])   # 对照池空 ⇒ 增量缺失
            ci_lo, ci_hi = block_bootstrap_ci(inc[f"incr_atr_{tag}"].to_numpy(),
                                              inc["ts"].to_numpy())
            e_ci_lo, e_ci_hi = block_bootstrap_ci(g[f"net_atr_{tag}"].to_numpy(),
                                                  g["ts"].to_numpy())
            h_out["cost"][tag] = {
                "event_net_atr_mean": float(g[f"net_atr_{tag}"].mean()),
                "event_net_pct_mean": float(g[f"net_pct_{tag}"].mean()),
                "event_net_atr_ci95": [e_ci_lo, e_ci_hi],
                "ctrl_net_atr_mean": float(g[f"ctrl_net_atr_mean_{tag}"].mean()),
                "ctrl_n_mean": float(g["n_ctrl"].mean()),
                "incr_atr_mean": float(g[f"incr_atr_{tag}"].mean()),
                "incr_pct_mean": float(g[f"incr_pct_{tag}"].mean()),
                "incr_n_valid": int(len(inc)),
                "incr_atr_ci95": [ci_lo, ci_hi],
                "ctrl_gross_atr_mean": float(g["ctrl_gross_atr_mean"].mean()),
            }
        if horizon == PRIMARY_HORIZON:
            tag = _mult_tag(1.0)
            med = float(g["ts"].median())
            first, second = g[g["ts"] <= med], g[g["ts"] > med]
            h_out["stability"] = {
                "first_half_incr_atr": float(first[f"incr_atr_{tag}"].mean())
                if len(first) else None,
                "second_half_incr_atr": float(second[f"incr_atr_{tag}"].mean())
                if len(second) else None,
                "split_ts": med,
            }
            h_out["by_regime"] = _by(g, "regime", f"incr_atr_{tag}")
            h_out["by_symbol"] = _by(g, "symbol", f"incr_atr_{tag}") \
                if "symbol" in g.columns else {}
        out["horizons"][str(horizon)] = h_out
    return out


# ---------------------------------------------------------------------------
# 6. GO / NO-GO(§9.3,只在 test 段调用)
# ---------------------------------------------------------------------------


def go_no_go(analysis: dict, per_symbol: dict) -> dict:
    """§9.3 冻结判据;"净期望"口径 = 事件净期望 − 对照净期望(见模块 docstring)。

    analysis: analyze_segment 输出(test 段);per_symbol: {symbol: n_events}。
    """
    h24 = analysis["horizons"].get(str(PRIMARY_HORIZON), {})
    n = analysis["n_events"]
    if n == 0 or not h24:
        return {"verdict": "INSUFFICIENT_DATA", "reason": "no test events"}
    c1 = n >= MIN_EVENTS_GO
    incr_15 = h24["cost"][_mult_tag(1.5)]
    incr_10 = h24["cost"][_mult_tag(1.0)]
    c2 = incr_15["incr_atr_mean"] > 0.0 and incr_15["incr_pct_mean"] > 0.0
    c3 = incr_10["incr_atr_ci95"][0] > 0.0
    pos = sum(1 for v in h24["by_symbol"].values() if v["mean"] > 0.0)
    c4 = pos >= MIN_SYMBOLS_POSITIVE
    verdict = "GO" if all((c1, c2, c3, c4)) else (
        "INSUFFICIENT_DATA" if not c1 else "NO-GO")
    return {
        "verdict": verdict,
        "primary_horizon": PRIMARY_HORIZON,
        "criterion1_events_ge_60": {"pass": c1, "n_events_total": n,
                                    "per_symbol": per_symbol},
        "criterion2_incr_positive_at_1_5x": {
            "pass": c2, "incr_atr": incr_15["incr_atr_mean"],
            "incr_pct": incr_15["incr_pct_mean"]},
        "criterion3_ci_lower_gt_0_at_1x": {
            "pass": c3, "ci95_lower": incr_10["incr_atr_ci95"][0]},
        "criterion4_two_symbols_positive": {
            "pass": c4, "n_positive": pos,
            "per_symbol_incr": {k: v["mean"]
                                for k, v in h24["by_symbol"].items()}},
    }
