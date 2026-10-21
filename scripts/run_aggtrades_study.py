"""v19-orderflow aggTrades 接触点检验主管线(docs/v19-protocol-orderflow.md;
一切判别口径先于结果观察冻结,本文件即冻结文本)。

用法:  python scripts/run_aggtrades_study.py [--render-only] [--skip-klines-fetch]
产物:  reports/aggtrades.json                  机器可读全量
       reports/aggtrades.md                    人读报告(H1/H2 判别表)
       reports/aggtrades_contacts.csv.gz       接触点明细(压缩,含特征/对照增量)

冻结口径全文(协议 + 解释性决定,均先于结果观察):
- 数据:tape = data/aggtrades/{SYM}/{date}.csv.gz(fetch_aggtrades.py 落地,
  Binance vision futures/um daily aggTrades;venue 声明与 manifest 一致)。
  认知帧 = Binance 1h K线(data/raw_binance/{SYM}_1h_w90.csv,同窗 + 180d
  因果 warm-up,沿用 fetch_binance_klines 的实现与 manifest 追记;v12 既有
  {SYM}_1h.csv 与其产物零改动)。
- 窗:磁带首日 00:00 UTC 起 90 天(2026-10-21 时最近已发布日 = 2026-10-19,
  10-20 未发布 404 实测;实际窗以落地文件集导出并如实写入报告)。
  分割 = 前 60 天 dev / 后 30 天 test,按 t_contact(known bar 收盘 =
  known_ts + 3_600_000)归属;split_ts = 首日 + 60d。
- 接触点 = canonical 重放(Params.from_json(config/canonical.json),断言
  merge_max_atr=2.0)E1/E2/E3/E6/E7(enable_e7=True)的 t_contact;测量层
  沿用 methodology §9 语义:入场 = 次 1h 开盘,H = 24h,MFE/MAE 以事件
  ATR 为单位(net@1x = 毛 − taker 双程成本,CostModel.from_config(
  canonical));对照 = 同品种/同 regime/同方向、避同机制 [known, +24] 窗、
  42 固定种子(SEED_BASE+i = 20261019+i)随机入场,决策 bar 取自认知帧
  同段(dev = [0, split_bar),test = [split_bar, n)—— §9/v12 段口径,
  不加磁带窗限制;§3 特征基线 = 对照抽样中收盘落在磁带窗内的子集)—
  与 research.measure_controls 同一抽样引擎(单机校验:在 H=48 兼容池上
  与 research.measure_controls 逐事件对照均值全等,结果写入本报告)。
  迭代硬声明:首跑误把对照池限制于磁带窗内(超出 §9 口径),~70% 接触
  点 incr 未定义(E3 高密度段结构性饱和,v12 docstring 同病理),该跑
  标题判定 = H1 no_increment(dev 推进 0/50)、H2 INSUFFICIENT_DATA
  (test 吸收/非吸收 26/141 < 50,拒绝人口实际只剩 E2);按 §9 还原段
  口径重跑即本报告,两跑判定均在 §6 如实并列。
- 特征 = aggtape 模块六特征(§3 逐字;全部 trailing 因果窗;NaN 口径同模块
  docstring,不编造)。±15m 敏感性 = 特征族本身即 15m/60m 双档(delta_15/60,
  cvd/big/eff/vol 档),判别线只用冻结主档,两档 pass 计数作描述性敏感性。
- H1(§4):判别单元 = (机制=事件×方向,符号合并) × 切片特征 5 个:
  方向化 s_delta_share_15m = direction*delta_share_15m,s_delta_share_60m,
  s_cvd_slope_60m("对拒绝方向":正 = 与事件预期方向同号的 tape 失衡),
  无方向 big_share_15m,eff_15m。分位 = flow_study.tercile_labels 三瓜分
  (升序,t_contact 平局裁决);统计 = diff = mean(incr@1x H24 | top) −
  mean(incr | bottom),incr = 事件 net@1x − 对照 42 种子净均值;CI =
  flow_study.tercile_diff_ci(两组独立日块 block bootstrap,2000,固定种子)。
  dev 推进规则:dev diff > 0 且 CI 不含 0 ⇒ 该单元获 test 评估资格;
  test 判别:test diff > 0 且 CI 不含 0 ⇒ 该单元过。n < 50(dev 或 test)
  ⇒ INSUFFICIENT_DATA(协议 §4 样品量线)。全局 H1 = 任一 test 单元过 ⇒
  "有增量 + 哪个特征";全部不过 ⇒ "无增量"。多重性:10 机制 × 5 特征 =
  50 单元族,家族-wise 未校正,如实声明;信息维度各自的 test 只评一次
  (本报告即该次)。
- H2(§4,吸收签名):接触全体 = 拒绝类 E2/E3(双向合并,符号合并)。
  阈:dev 拒绝接触的 vol15_rel ≥ dev 70 分位 且 eff_15m ≤ dev 30 分位
  (H2_VOL_Q = 0.70,H2_EFF_Q = 0.30,分位法 numpy linear,dev 计算后冻结
  应用于 test)。拒绝残幅 = mfe_incr = mfe_atr − 对照 42 种子 mfe 均值。
  H2a:mean(mfe_incr | 吸收 ∩ test) > 0,日块 block bootstrap(2000,
  research.BOOT_SEED)CI 不含 0。H2b:mean(吸收) − mean(非吸收 ∩ test
  拒绝) > 0,两组独立日块 bootstrap(2000,flow_study.DIFF_BOOT_SEED)CI
  不含 0。两个都过才算"增量存在"(保守多重性裁判,协议 §4 原话)。
  各亚假设 n < 50 ⇒ INSUFFICIENT_DATA;每个亚假设 test 各评一次(本报告)。
- 禁止:本管线只产出"增量有/无 + 哪特征 + CI";不含任何策略语义。

内存纪律:tape 两遍流式(aggtape.notional_grid / stream_features),单日
载入-聚合-丢弃,全程不驻 90 天原始行;认知帧(1h bar)全量驻存(数百 KB)。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import aggtape as at                            # noqa: E402
from market_operator.events import E1, E2, E3, E6, E7                # noqa: E402
from market_operator.flow_study import tercile_diff_ci, tercile_labels  # noqa: E402
from market_operator.research import (                               # noqa: E402
    BOOT_REPS,
    HORIZONS,
    N_SEEDS,
    SEED_BASE,
    CostModel,
    MS_PER_DAY,
    block_bootstrap_ci,
    control_pool,
    exclusion_mask,
    measure_controls,
    measure_path,
    _mult_tag,
    _net_row,
)
from market_operator.structure import Params                         # noqa: E402

CONFIG_CANONICAL = ROOT / "config" / "canonical.json"
DATA_BINANCE = ROOT / "data" / "raw_binance"
DATA_AGG = ROOT / "data" / "aggtrades"
MANIFEST_PATH = ROOT / "data" / "manifest.json"
REPORTS = ROOT / "reports"

# --- 冻结研究常量(先于结果观察;变更即新协议) ---------------------------------
H24 = 24                                # 主视界 = 后续 24h(协议 §4 唯一视界)
KLINE_WARMUP_DAYS = 180                 # 认知帧因果 warm-up
DEV_DAYS = 60                           # 分割:dev 60d / test 30d(协议 §4)
H2_VOL_Q = 0.70                         # 吸收阈:vol15_rel ≥ dev 70 分位
H2_EFF_Q = 0.30                         #       eff_15m ≤ dev 30 分位
MIN_N = 50                              # 协议 §4:样品量 < 50 ⇒ INSUFFICIENT_DATA
MECHS = tuple(f"{e}|{d}" for e in (E1, E2, E3, E6, E7)
              for d in ("long", "short"))
SLICERS = ("s_delta_share_15m", "s_delta_share_60m", "s_cvd_slope_60m",
           "big_share_15m", "eff_15m")
TIER_15M = frozenset({"s_delta_share_15m", "big_share_15m", "eff_15m"})
NET_TAG = _mult_tag(1.0)                # 判别用 net@1x(v12 同口径)


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(
        timespec="seconds")


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------


def tape_days(symbol: str) -> list:
    """落地磁带文件名导出日期序列(源数据即真源);升序。"""
    d = DATA_AGG / symbol
    return sorted(p.stem.replace(".csv", "") for p in d.glob("*.csv.gz"))


def discover_window(symbols: list) -> dict:
    """三品种落地磁带 ⇒ 研究窗(date 列表须全等、连续、= 90 天)。"""
    per_sym = {s: tape_days(s) for s in symbols}
    ref = per_sym[symbols[0]]
    for s in symbols[1:]:
        if per_sym[s] != ref:
            raise RuntimeError(f"tape day sets differ: {symbols[0]} vs {s}")
    d0 = datetime.strptime(ref[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for i, name in enumerate(ref):
        want = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        if name != want:
            raise RuntimeError(f"tape days not consecutive at {name} != {want}")
    return {"dates": ref, "n_days": len(ref),
            "start_ms": int(d0.timestamp() * 1000),
            "end_ms": int(d0.timestamp() * 1000) + len(ref) * MS_PER_DAY,
            "start_date": ref[0], "end_date": ref[-1]}


def ensure_klines_frame(symbol: str, window_start_ms: int) -> Path:
    """同窗 + 180d warm-up 的 Binance 1h 帧;缺失则用 fetch_binance_klines
    的 api-fallback 路径(fapi,与 vision 同日 parity 已实测:close/volume
    逐位一致,2026-10-18 重叠日)取到最近已收盘 bar —— vision 日档发布
    滞后使尾部缺当日前向窗,API 路径同源同场只是传输不同,如实声明。
    落地 {SYM}_1h_w90.csv,manifest 追记(既有 v12 帧与其产物零改动)。"""
    path = DATA_BINANCE / f"{symbol}_1h_w90.csv"
    if path.exists():
        return path
    spec = importlib.util.spec_from_file_location(
        "fetch_binance_klines", ROOT / "scripts" / "fetch_binance_klines.py")
    fbk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fbk)
    start_ts = window_start_ms - KLINE_WARMUP_DAYS * MS_PER_DAY
    end_ts = int(time.time() * 1000)
    df, parts = fbk.fetch_api_fallback(symbol, start_ts, end_ts)
    issues = fbk.validate(df)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, columns=fbk.COLUMNS)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    ts = df["ts"]
    record = {
        "path": str(path.relative_to(ROOT)),
        "sha256": sha,
        "rows": int(len(df)),
        "ts_first": int(ts.iloc[0]),
        "ts_last": int(ts.iloc[-1]),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "binance-fapi-api(fetch_binance_klines api-fallback;"
                  "与 vision 重叠日 parity 实测逐位一致)",
        "purpose": "v19-orderflow 认知帧(同窗+180d warm-up);v12 既有帧零改动",
        "granularity": "1h",
        "parts_n": len(parts),
        "validation_issues": issues,
    }
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) \
        if MANIFEST_PATH.exists() else {"files": {}}
    manifest.setdefault("files", {})[f"binance_vision/{symbol}_1h_w90.csv"] = record
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False)
                             + "\n", encoding="utf-8")
    print(f"[{symbol}] klines w90 frame: {len(df)} bars [{_iso(int(ts.iloc[0]))} .. "
          f"{_iso(int(ts.iloc[-1]))}], {len(issues)} issues", flush=True)
    return path


def load_klines(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_BINANCE / f"{symbol}_1h_w90.csv", dtype={"ts": "int64"})
    return (df.drop_duplicates(subset="ts").sort_values("ts")
            .reset_index(drop=True))


# ---------------------------------------------------------------------------
# 单品种:接触点 + H24 测量 + 对照(§9 同引擎)+ tape 特征
# ---------------------------------------------------------------------------


def _measure_h24(bars: dict, entry_i: int, direction: int, atr: float,
                 cost: CostModel) -> dict | None:
    """次 1h 开盘入场,H=24 MFE/MAE/毛/净@1x(ATR 单位);窗不足 ⇒ None。"""
    n = len(bars["o"])
    if entry_i + H24 - 1 >= n or atr <= 0.0:
        return None
    mfe, mae, gross, _ = measure_path(bars["o"], bars["h"], bars["l"], bars["c"],
                                      entry_i, H24, direction, None, None)
    row = _net_row(mfe, mae, gross, None, float(bars["o"][entry_i]), atr,
                   H24, cost)
    return {"mfe_atr": row["mfe_atr"], "mae_atr": row["mae_atr"],
            "gross_atr": row["gross_atr"], "net_atr_1x": row[f"net_atr_{NET_TAG}"]}


def _ctrl_draws(bars: dict, timeline, pool: np.ndarray, direction: int,
                cost: CostModel) -> list:
    """一个池上的 42 固定种子对照抽样与测量(measure_controls 同引擎语义)。"""
    draws: list = []
    for seed_i in range(N_SEEDS):
        if len(pool) == 0:
            break
        rng = np.random.default_rng(SEED_BASE + seed_i)
        i = int(pool[rng.integers(len(pool))])
        entry_i = i + 1
        atr = float(timeline.atr[i])
        m = _measure_h24(bars, entry_i, direction, atr, cost)
        if m is None:
            continue
        draws.append({"ctrl_index": i, "seed_i": seed_i,
                      "t_ctrl": int(bars["ts"][i]) + at.BAR_MS, **m})
    return draws


def verify_draw_engine(bars: dict, timeline, fired: list, seg_rows: pd.DataFrame,
                       cost: CostModel, lo: int, hi: int) -> dict:
    """单机校验:本管线对照引擎(H=48 兼容池)与 research.measure_controls
    在同段/同池/同种子下的逐事件对照均值须全等(浮点恒等)。结果写报告。"""
    ev_rows = seg_rows[["uid", "known_index", "event_id", "direction",
                        "mechanism", "regime"]].copy()
    ev_rows["H"] = H24
    ref = measure_controls(_bars_to_df(bars), fired, timeline, ev_rows, cost,
                           lo, hi, bar_hours=1.0)
    by_mech: dict = {}
    for e in fired:
        key = (e.id, "long" if e.direction > 0 else "short")
        by_mech.setdefault(key, []).append(e)
    diffs: list = []
    pool_cache: dict = {}
    for ev in seg_rows[["uid", "mechanism", "regime", "direction"]].itertuples(index=False):
        key = (ev.mechanism, ev.regime)
        if key not in pool_cache:
            m_id, m_dir = ev.mechanism.split("|")
            excl = exclusion_mask(len(timeline.atr),
                                  by_mech.get((m_id, m_dir), []))
            pool_cache[key] = control_pool(timeline, lo, hi, ev.regime, excl,
                                           max_h=max(HORIZONS))
        draws = _ctrl_draws(bars, timeline, pool_cache[key],
                            int(ev.direction), cost)
        mine = float(np.mean([d["net_atr_1x"] for d in draws])) if draws else float("nan")
        r = ref[ref["uid"] == ev.uid]
        if len(r):
            refv = float(r[f"ctrl_net_atr_mean_{NET_TAG}"].iloc[0])
            if refv == refv and mine == mine:
                diffs.append(abs(refv - mine))
    return {"check": "ctr_draw_engine_vs_research.measure_controls",
            "n_compared": len(diffs),
            "max_abs_diff": float(max(diffs)) if diffs else None,
            "passed": bool(diffs) and max(diffs) < 1e-12}


def _bars_to_df(bars: dict) -> pd.DataFrame:
    return pd.DataFrame({"ts": bars["ts"], "open": bars["o"], "high": bars["h"],
                         "low": bars["l"], "close": bars["c"], "volume": bars["v"]})


def process_symbol(symbol: str, window: dict, params: Params, cost: CostModel
                   ) -> tuple:
    """返回 (接触点明细帧(含特征与对照增量), symbol_info, verify_check)。"""
    t0 = time.time()
    df = load_klines(symbol)
    n = len(df)
    bars = {"ts": df["ts"].to_numpy(np.int64), "o": df["open"].to_numpy(np.float64),
            "h": df["high"].to_numpy(np.float64), "l": df["low"].to_numpy(np.float64),
            "c": df["close"].to_numpy(np.float64), "v": df["volume"].to_numpy(np.float64)}
    contacts, timeline, fired = at.collect_contacts(
        df, params, ts_lo=window["start_ms"], ts_hi=window["end_ms"])

    split_ts = window["start_ms"] + DEV_DAYS * MS_PER_DAY
    contacts["segment"] = np.where(contacts["t_contact"] < split_ts, "dev", "test")

    # --- H24 测量(尾部窗不足如实丢弃) ---
    meas, dropped_tail = [], 0
    for orig_idx, row in zip(contacts.index, contacts.itertuples(index=False)):
        m = _measure_h24(bars, int(row.known_index) + 1, int(row.direction),
                         float(row.atr), cost)
        if m is None:
            dropped_tail += 1
            continue
        meas.append((orig_idx, m))
    keep_idx = [k for k, _ in meas]
    contacts = contacts.loc[keep_idx].reset_index(drop=True)
    for col in ("mfe_atr", "mae_atr", "gross_atr", "net_atr_1x"):
        contacts[col] = [m[col] for _, m in meas]

    # --- 对照池(§9 协议语义:决策 bar 取自认知帧同段 —— dev = [0, split),
    # test = [split, n),与 v12 measure_controls 的段口径一致;不在磁带窗上
    # 再加限制。首跑曾把池限制在磁带窗内,超出 §9 口径,使 ~70% 接触点
    # incr 未定义(池退化为空,尤其 E3 高密度段结构性饱和 —— v12 docstring
    # 已载同病理);本版按 §9 还原,两跑标题判定都在报告 §6 硬声明) ---
    by_mech: dict = {}
    for e in fired:
        key = (e.id, "long" if e.direction > 0 else "short")
        by_mech.setdefault(key, []).append(e)
    split_bar = int(np.searchsorted(bars["ts"], split_ts - at.BAR_MS,
                                    side="left"))
    seg_bounds = {"dev": (0, split_bar), "test": (split_bar, n)}
    pool_cache: dict = {}
    ctrl_rows: list = []
    for row in contacts.itertuples(index=False):
        key = (row.segment, row.mechanism, row.regime)
        if key not in pool_cache:
            m_id, m_dir = row.mechanism.split("|")
            excl = exclusion_mask(n, by_mech.get((m_id, m_dir), []))
            lo, hi_s = seg_bounds[row.segment]
            pool_cache[key] = control_pool(timeline, lo, hi_s, row.regime,
                                           excl, max_h=H24)
        pool = pool_cache[key]
        draws = _ctrl_draws(bars, timeline, pool, int(row.direction), cost)
        for d in draws:
            ctrl_rows.append({"uid": row.uid, "pool": key, **d})
    ctr = pd.DataFrame(ctrl_rows) if ctrl_rows else pd.DataFrame(
        columns=["uid", "pool", "ctrl_index", "seed_i", "t_ctrl",
                 "mfe_atr", "mae_atr", "gross_atr", "net_atr_1x"])
    if len(ctr):
        agg = ctr.groupby("uid").agg(
            n_ctrl=("seed_i", "count"),
            ctrl_net_atr_mean=("net_atr_1x", "mean"),
            ctrl_mfe_atr_mean=("mfe_atr", "mean"),
            ctrl_mae_atr_mean=("mae_atr", "mean")).reset_index()
        contacts = contacts.merge(agg, on="uid", how="left")
    else:
        contacts["n_ctrl"] = 0
        contacts["ctrl_net_atr_mean"] = np.nan
        contacts["ctrl_mfe_atr_mean"] = np.nan
        contacts["ctrl_mae_atr_mean"] = np.nan
    contacts["incr_atr_1x"] = contacts["net_atr_1x"] - contacts["ctrl_net_atr_mean"]
    contacts["mfe_incr"] = contacts["mfe_atr"] - contacts["ctrl_mfe_atr_mean"]
    contacts["mae_incr"] = contacts["mae_atr"] - contacts["ctrl_mae_atr_mean"]

    # --- 抽样引擎单机校验(H=48 兼容池 vs research.measure_controls) ---
    dev_rows = contacts[contacts["segment"] == "dev"]
    hi48 = min(seg_bounds["dev"][1], n - 49)
    verify = (verify_draw_engine(bars, timeline, fired, dev_rows, cost,
                                 seg_bounds["dev"][0], hi48)
              if len(dev_rows) and hi48 > seg_bounds["dev"][0]
              else {"check": "skipped", "passed": None})

    # --- tape 特征:两遍流式 ---
    paths = [DATA_AGG / symbol / f"{d}.csv.gz" for d in window["dates"]]
    grid, g0 = at.notional_grid(paths, grid_start_ms=window["start_ms"])
    requests = list(contacts["t_contact"]) + (list(ctr["t_ctrl"]) if len(ctr) else [])
    feats = at.stream_features(paths, requests, grid=grid, grid_start_ms=g0)
    fc = feats.iloc[: len(contacts)].reset_index(drop=True)
    for col in at.FEATURE_NAMES + ("notional_15m", "n_trades_15m", "n_trades_60m"):
        contacts[col] = fc[col].to_numpy(np.float64)

    # 方向化切片特征("对拒绝方向":与事件预期方向同号为正)
    for base, name in (("delta_share_15m", "s_delta_share_15m"),
                       ("delta_share_60m", "s_delta_share_60m"),
                       ("cvd_slope_60m", "s_cvd_slope_60m")):
        contacts[name] = contacts[base] * contacts["direction"].astype(np.float64)

    # --- §3 参照基线(对照 bar 的同特征分布,描述性;§9 对照抽样子集 =
    # 其中决策 bar 收盘落在磁带窗内者 —— 窗外对照无 tape 特征,如实声明) ---
    baseline = {}
    if len(ctr):
        cf = feats.iloc[len(contacts):].reset_index(drop=True)
        ctr_feat = ctr.reset_index(drop=True).copy()
        for col in at.FEATURE_NAMES:
            ctr_feat[col] = cf[col].to_numpy(np.float64)
        ctr_feat = ctr_feat[ctr_feat["t_ctrl"] >= window["start_ms"]]
        for col in at.FEATURE_NAMES:
            v_c = contacts[col].to_numpy(np.float64)
            v_k = ctr_feat[col].to_numpy(np.float64)
            v_c, v_k = v_c[~np.isnan(v_c)], v_k[~np.isnan(v_k)]
            baseline[col] = {
                "contact_n": int(len(v_c)), "ctrl_n": int(len(v_k)),
                "contact_mean": float(v_c.mean()) if len(v_c) else None,
                "ctrl_mean": float(v_k.mean()) if len(v_k) else None,
                "contact_median": float(np.median(v_c)) if len(v_c) else None,
                "ctrl_median": float(np.median(v_k)) if len(v_k) else None,
            }

    contacts["symbol"] = symbol
    per_event = contacts.groupby(["event_id", "segment"]).size().unstack(
        fill_value=0).to_dict("index")
    info = {
        "bars": n,
        "klines_span": f"{_iso(int(bars['ts'][0]))} .. {_iso(int(bars['ts'][-1]))}",
        "klines_last_bar_ts": int(bars["ts"][-1]),
        "contacts": int(len(contacts)),
        "contacts_dev": int((contacts["segment"] == "dev").sum()),
        "contacts_test": int((contacts["segment"] == "test").sum()),
        "dropped_tail_h24": dropped_tail,
        "per_event_segment": {k: {kk: int(vv) for kk, vv in v.items()}
                              for k, v in per_event.items()},
        "vol15_rel_nan": int(contacts["vol15_rel"].isna().sum()),
        "contacts_missing_ctrl": int((contacts["n_ctrl"].fillna(0) == 0).sum()),
        "ctrl_pool_universe": "认知帧同段(dev=[0,split), test=[split,n)),§9 口径",
        "feature_nan": {c: int(contacts[c].isna().sum())
                        for c in at.FEATURE_NAMES},
        "elapsed_s": round(time.time() - t0, 1),
    }
    return contacts, info, verify, baseline


# ---------------------------------------------------------------------------
# 判别(H1 / H2;判别口径见模块 docstring 冻结文本)
# ---------------------------------------------------------------------------


def h1_cell(g: pd.DataFrame) -> dict:
    """单(机制 × 特征 × 段)判别单元:三瓜分 diff + 两组独立日块 CI。"""
    valid = g.dropna(subset=["slicer", "incr_atr_1x"])
    n = int(len(valid))
    if n < MIN_N:
        return {"n": n, "inference": "INSUFFICIENT_DATA",
                "reason": f"n < {MIN_N}(协议 §4)"}
    lab = tercile_labels(valid["slicer"].to_numpy(np.float64),
                         valid["t_contact"].to_numpy(np.int64))
    valid = valid.assign(tercile=lab)
    top = valid[valid["tercile"] == 2]
    bot = valid[valid["tercile"] == 0]
    if len(top) == 0 or len(bot) == 0:
        return {"n": n, "inference": "INSUFFICIENT_DATA",
                "reason": "tercile degenerate"}
    diff, ci_lo, ci_hi = tercile_diff_ci(
        top["incr_atr_1x"].to_numpy(), top["t_contact"].to_numpy(),
        bot["incr_atr_1x"].to_numpy(), bot["t_contact"].to_numpy())
    return {
        "n": n,
        "tercile_n": {"top": int(len(top)), "bottom": int(len(bot))},
        "top_incr_atr_mean": float(top["incr_atr_1x"].mean()),
        "bottom_incr_atr_mean": float(bot["incr_atr_1x"].mean()),
        "diff_top_minus_bottom_atr": diff,
        "diff_atr_ci95": [ci_lo, ci_hi],
        "top_slicer_range": [float(top["slicer"].min()), float(top["slicer"].max())],
        "bottom_slicer_range": [float(bot["slicer"].min()),
                                float(bot["slicer"].max())],
        "diff_positive_and_ci_excludes_zero": bool(diff > 0.0 and ci_lo > 0.0),
    }


def run_h1(ev: pd.DataFrame) -> dict:
    cells: dict = {}
    n_dev_advanced = 0
    test_passes: list = []
    for mech in MECHS:
        for slicer in SLICERS:
            key = f"{mech}||{slicer}"
            g = ev[ev["mechanism"] == mech].copy()
            g["slicer"] = g[slicer]
            dev = h1_cell(g[g["segment"] == "dev"])
            cell = {"dev": dev, "advanced_to_test": False, "test": None}
            if dev.get("diff_positive_and_ci_excludes_zero"):
                cell["advanced_to_test"] = True
                n_dev_advanced += 1
                test = h1_cell(g[g["segment"] == "test"])
                cell["test"] = test
                if test.get("diff_positive_and_ci_excludes_zero"):
                    test_passes.append(key)
            cells[key] = cell
    any_pass = bool(test_passes)
    by_tier = {"15m": sorted({k for k in test_passes
                              if k.split("||")[1] in TIER_15M}),
               "60m": sorted({k for k in test_passes
                              if k.split("||")[1] not in TIER_15M})}
    return {"cells": cells, "n_cells": len(MECHS) * len(SLICERS),
            "n_dev_advanced": n_dev_advanced,
            "test_passes": test_passes, "test_passes_by_tier": by_tier,
            "verdict": "H1_supported" if any_pass else "no_increment",
            "chain": (f"test 单元过线 {len(test_passes)}/{n_dev_advanced}(先进后评,"
                      "每单元一次)⇒ 接触点 tape 特征有增量(只表述度量)"
                      if any_pass else
                      "全部 test 单元未过线 ⇒ 接触点 tape 特征无增量(H0 站住)")}


def run_h2(ev: pd.DataFrame) -> dict:
    rej = ev[ev["event_id"].isin(sorted(at.REJECTION_EVENT_IDS))].copy()
    # 判别可用人口构成(协议 §4 样品统计的事实底座:inc r未定义 = §9 对照池空)
    comp = {}
    for seg in ("dev", "test"):
        g = rej[rej["segment"] == seg]
        comp[seg] = {eid: {"total": int(len(ge)),
                           "valid_incr": int(ge["mfe_incr"].notna().sum())}
                     for eid, ge in g.groupby("event_id")}
    dev = rej[rej["segment"] == "dev"].dropna(subset=["vol15_rel", "eff_15m",
                                                      "mfe_incr"])
    test = rej[rej["segment"] == "test"].dropna(subset=["vol15_rel", "eff_15m",
                                                        "mfe_incr"])
    n_dev, n_test = int(len(dev)), int(len(test))
    if n_dev < MIN_N:
        return {"inference": "INSUFFICIENT_DATA",
                "reason": f"dev 拒绝接触 n={n_dev} < {MIN_N}",
                "n_dev": n_dev, "n_test": n_test,
                "population_composition": comp}
    q_vol = float(np.quantile(dev["vol15_rel"], H2_VOL_Q))
    q_eff = float(np.quantile(dev["eff_15m"], H2_EFF_Q))
    dev_abs = dev[(dev["vol15_rel"] >= q_vol) & (dev["eff_15m"] <= q_eff)]
    abs_t = test[(test["vol15_rel"] >= q_vol) & (test["eff_15m"] <= q_eff)]
    non_t = test.drop(index=abs_t.index)
    n_a, n_n = int(len(abs_t)), int(len(non_t))
    out = {"thresholds": {"vol15_rel_q": H2_VOL_Q, "eff_15m_q": H2_EFF_Q,
                          "vol15_rel_threshold": q_vol, "eff_15m_threshold": q_eff,
                          "computed_on": "dev(60d)拒绝接触,冻结后应用于 test"},
           "n_dev": n_dev, "n_test": n_test, "n_test_absorption": n_a,
           "n_test_non_absorption": n_n,
           "n_dev_absorption": int(len(dev_abs)),
           "dev_absorption_mfe_incr_mean": (float(dev_abs["mfe_incr"].mean())
                                            if len(dev_abs) else None),
           "population_composition": comp}

    def _stats() -> tuple:
        vals, ts = abs_t["mfe_incr"].to_numpy(), abs_t["t_contact"].to_numpy()
        lo_a, hi_a = block_bootstrap_ci(vals, ts)
        h2a = {"mean_mfe_incr": float(vals.mean()), "ci95": [lo_a, hi_a],
               "pass": bool(vals.mean() > 0.0 and lo_a > 0.0)}
        diff_b, lo_b, hi_b = tercile_diff_ci(
            vals, ts,
            non_t["mfe_incr"].to_numpy(), non_t["t_contact"].to_numpy())
        h2b = {"diff_absorption_minus_non": diff_b, "ci95": [lo_b, hi_b],
               "pass": bool(diff_b > 0.0 and lo_b > 0.0),
               "non_absorption_mean_mfe_incr": float(non_t["mfe_incr"].mean())}
        return h2a, h2b

    if n_a < MIN_N or n_n < MIN_N:
        out["inference"] = "INSUFFICIENT_DATA"
        out["reason"] = (f"test 吸收/非吸收样本 {n_a}/{n_n} < {MIN_N}(协议 §4,"
                         f"一律;判别人口构成见 population_composition)")
        if n_a >= 3 and n_n >= 3:      # 可算但不可判:参考值,非判别
            h2a, h2b = _stats()
            out["reference_only_not_adjudicated"] = {
                "declaration": "以下数字仅为参考(协议 §4 样本线未过,不作判别,"
                               "不进入任何增量判定)",
                "h2a_vs_control": h2a, "h2b_vs_non_absorption": h2b}
        return out
    h2a, h2b = _stats()
    both = h2a["pass"] and h2b["pass"]
    out.update({"h2a_vs_control": h2a, "h2b_vs_non_absorption": h2b,
                "both_pass": both,
                "verdict": ("increment_exists" if both else "no_increment"),
                "chain": ("两亚假设均过(保守多重性裁判)⇒ 吸收签名有增量"
                          "(只表述度量)" if both else
                          "未两过 ⇒ 吸收签名无增量")})
    return out


# ---------------------------------------------------------------------------
# main 管线
# ---------------------------------------------------------------------------


def run(skip_klines_fetch: bool = False, symbols: list | None = None) -> dict:
    raw_cfg = json.loads(CONFIG_CANONICAL.read_text(encoding="utf-8"))
    params = Params.from_json(CONFIG_CANONICAL)
    assert params.merge_max_atr == 2.0, \
        "canonical 认知重放必须同生产语义 merge_max_atr=2.0"
    cost = CostModel.from_config(CONFIG_CANONICAL)
    if symbols is None:
        symbols = list(raw_cfg["symbols"])

    window = discover_window(symbols)
    split_ts = window["start_ms"] + DEV_DAYS * MS_PER_DAY
    print(f"tape window: {window['start_date']} .. {window['end_date']} "
          f"({window['n_days']}d); split {datetime.fromtimestamp(split_ts/1000, tz=timezone.utc):%Y-%m-%d}",
          flush=True)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    tape_manifest = {s: [manifest["files"].get(f"aggtrades/{s}/{d}.csv.gz", {})
                         for d in window["dates"]] for s in symbols}

    all_contacts, sym_info, verifies, baselines = [], {}, [], {}
    for sym in symbols:
        if not skip_klines_fetch:
            ensure_klines_frame(sym, window["start_ms"])
        contacts, info, verify, baseline = process_symbol(sym, window, params, cost)
        all_contacts.append(contacts)
        sym_info[sym] = info
        verifies.append({"symbol": sym, **verify})
        baselines[sym] = baseline
        print(f"[{sym}] bars={info['bars']} contacts={info['contacts']} "
              f"(dev {info['contacts_dev']}/test {info['contacts_test']}) "
              f"drop_tail={info['dropped_tail_h24']} "
              f"volnan={info['vol15_rel_nan']} {info['elapsed_s']}s", flush=True)
    ev = (pd.concat(all_contacts, ignore_index=True) if all_contacts
          else pd.DataFrame())

    h1 = run_h1(ev) if len(ev) else {"verdict": "no_increment",
                                     "reason": "no contacts"}
    h2 = run_h2(ev) if len(ev) else {"verdict": "no_increment",
                                     "reason": "no contacts"}
    print(f"  >>> H1: {h1['verdict']} (pass {len(h1.get('test_passes', []))}); "
          f"H2: {h2.get('verdict', h2.get('inference'))}", flush=True)

    # --- 疑似问题/硬声明(仅列可由数据佐证者 + 结构性事实) ---
    issues: list = []
    for sym, info in sym_info.items():
        if info["contacts_missing_ctrl"]:
            issues.append(
                f"{sym}:{info['contacts_missing_ctrl']}/{info['contacts']} 接触点"
                "对照池为空(n_ctrl=0,§9 同机制排除窗在事件高密度段的已知病理),"
                "其 incr 未定义 ⇒ 自 H1/H2 单元样本中剔除(dropna 如实反映)。")
        if info["dropped_tail_h24"]:
            issues.append(
                f"{sym}:{info['dropped_tail_h24']} 接触点因 24h 前向窗不足"
                "(k线帧尾界)整体未测,不进任何判别样本。")
        nan15 = info["feature_nan"].get("delta_share_15m", 0)
        if nan15:
            issues.append(
                f"{sym}:{nan15} 接触点 15m 窗内零成交(低流动性时段),"
                "15m 档特征 NaN ⇒ 相关单元剔除该行。")
        last_bar = info["klines_last_bar_ts"]
        gap_h = round((last_bar + MS_PER_DAY // 24
                       - window["end_ms"]) / 3_600_000, 1)
        if gap_h < 24:
            issues.append(
                f"{sym}:认知帧最后 bar 收盘距磁带窗终点 {gap_h}h < 24h,"
                "尾部接触前向窗覆盖不完全(见上条丢弃计数)。")
    checksum_by_sym = {sym: sorted({r.get("zip_checksum", "?")
                                    for r in tape_manifest[sym]})
                       for sym in symbols}
    issues.append(f"zip CHECKSUM 状态(各品种取值集合):{checksum_by_sym}")
    issues.append(
        "结构事实:① venue = Binance ≠ Bitget(协议 §1 声明,不跨场座外推);"
        "② 多重性 = H1 50 单元族未校正 + H2 两亚假设取交(均已声明);"
        "③ tape 特征只有 trailing 窗因果口径,±60m/±15m 评价窗的后侧从未入"
        "特征(协议 §2 因果窗读法,本报告即冻结文本);④ H1 dev 推进单元数即"
        "信息维度多比一次的次数,各维度 test 只评一次(本报告即该次)。")
    issues.append(
        "实现迭代(两跑如实并列):首跑误将对照池限制在磁带窗内(超出 §9 段"
        "口径),标题判定 = H1 no_increment(dev 推进 0/50)、"
        "H2 INSUFFICIENT_DATA(test 吸收/非吸收 26/141 < 50,拒绝人口因 E3 "
        "对照池结构性空而只剩 E2);本报告 = 按 §9 段口径(dev=[0,split),"
        "test=[split,n))还原后的一跑。E3 对照池在两种口径下均结构性饱和"
        "(密度驱动,v12 docstring 已载同病理),其 incr 大面积未定义为数据"
        "事实,非实现选择。")
    issues.append(
        "对照池含 warm-up 段(认知帧前 180d)决策 bar(dev 池):era 差异由 "
        "regime 匹配吸收,与 v12 全帧段同例;如怀疑 era 漂移,dev/test 各自"
        "段内的一致性即为本研究的全部检验尺度,如实声明。")

    detail_path = REPORTS / "aggtrades_contacts.csv.gz"
    cols = ["symbol", "uid", "event_id", "direction", "mechanism", "regime",
            "known_ts", "t_contact", "segment", "atr",
            "mfe_atr", "mae_atr", "gross_atr", "net_atr_1x",
            "n_ctrl", "ctrl_net_atr_mean", "ctrl_mfe_atr_mean",
            "incr_atr_1x", "mfe_incr", "mae_incr",
            *at.FEATURE_NAMES, "notional_15m", "n_trades_15m", "n_trades_60m",
            "s_delta_share_15m", "s_delta_share_60m", "s_cvd_slope_60m"]
    if len(ev):
        ev[cols].to_csv(detail_path, index=False, compression="gzip")

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "protocol": {
            "parent": "docs/v19-protocol-orderflow.md(冻结);测量层 = methodology "
                      "§9 语义(H=24 单视界,H48 全 span 不适用本协议)",
            "venue_declaration": "Binance USDⓈ-M 永续(data.binance.vision "
                                 "futures/um daily aggTrades)≠ Bitget;本研究只"
                                 "回答'接触点 tape 特征有没有增量'",
            "params_source": "config/canonical.json(merge_max_atr=2.0 断言通过)",
            "klines_source": "data/raw_binance/{SYM}_1h_w90.csv = fapi api-fallback"
                             "(fetch_binance_klines 实现;与 vision 重叠日 parity "
                             "实测逐位一致;覆盖到最近已收盘 bar 以保 24h 前向窗)",
            "contact_definition": "E1/E2/E3/E6/E7 known bar 收盘 = known_ts + "
                                  "3_600_000(enable_e7=True)",
            "feature_module": "src/market_operator/aggtape.py(六特征口径与 NaN/"
                              "warmup 规则见其 docstring,先于结果观察冻结)",
            "window": {k: window[k] for k in ("start_date", "end_date", "n_days",
                                              "start_ms", "end_ms")},
            "split": f"dev 前 {DEV_DAYS}d / test 后 {window['n_days'] - DEV_DAYS}d;"
                     f"split_ts = {_iso(split_ts)}(按 t_contact 归属)",
            "h1_rule": "单元 = 机制×5 特征;三瓜分 top−bottom diff(incr@1x H24);"
                       "日块 bootstrap 2000 固定种子;dev diff>0 且 CI 不含 0 推进,"
                       "test 同线判别,每维度 test 一次;n<50 INSUFFICIENT",
            "h2_rule": "吸收 = vol15_rel ≥ dev q70 且 eff_15m ≤ dev q30(E2/E3,"
                       "阈 dev 定后冻结);H2a vs 对照均值>0 且 CI 不含 0,H2b vs 非"
                       "吸收差>0 且 CI 不含 0;两个都过才算增量存在;n<50 INSUFFICIENT",
            "seeds": {"controls": SEED_BASE, "bootstrap": "research.BOOT_SEED / "
                      "flow_study.DIFF_BOOT_SEED(固定)", "reps": BOOT_REPS},
            "multiplicity_declaration": "H1 50 单元族(10 机制×5 特征)家族-wise 未"
                                        "校正;H2 两亚假设取交(保守);均如实声明",
            "cost_model": vars(cost),
            "measurement_only_declaration": "只产'增量有/无+哪特征+CI';无任何策略语义",
        },
        "symbols": sym_info,
        "draw_engine_verifications": verifies,
        "tape_manifest_rows_total": {s: int(sum(r.get("rows", 0) for r in rs))
                                     for s, rs in tape_manifest.items()},
        "tape_manifest_zip_checksum": {s: sorted({r.get("zip_checksum", "?")
                                                  for r in rs})
                                       for s, rs in tape_manifest.items()},
        "h1": h1,
        "h2": h2,
        "feature_baseline_§3": baselines,
        "issues_hard_declarations": issues,
        "detail_path": str(detail_path.relative_to(ROOT)),
    }
    return result


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _f(x, nd=4):
    if x is None:
        return "—"
    if isinstance(x, float):
        if x != x:
            return "NaN"
        return f"{x:.{nd}f}"
    return str(x)


def _cell_md(cell: dict | None) -> str:
    if not cell:
        return "—(未进 test)"
    if "diff_top_minus_bottom_atr" not in cell:
        return cell.get("inference", "—")
    return (f"n={cell['n']} diff={_f(cell['diff_top_minus_bottom_atr'])} "
            f"CI[{_f(cell['diff_atr_ci95'][0])},{_f(cell['diff_atr_ci95'][1])}]"
            f"{' **PASS**' if cell['diff_positive_and_ci_excludes_zero'] else ''}")


def render_md(result: dict) -> str:
    p = result["protocol"]
    h1, h2 = result["h1"], result["h2"]
    lines = [
        "# aggTrades 接触点检验报告(docs/v19-protocol-orderflow.md,测量协议)",
        "",
        f"生成时间(UTC):{result['generated_at_utc']}",
        "",
        "## 0. 协议与诚实性声明",
        "",
        f"- 场所:{p['venue_declaration']}。",
        f"- 数据窗:{p['window']['start_date']} .. {p['window']['end_date']} "
        f"({p['window']['n_days']} 天日档);认知帧 = Binance 1h(同窗 + 180d "
        "warm-up,fetch_binance_klines 实现,manifest 追记)。",
        f"- 接触点:{p['contact_definition']};认知 = canonical 重放"
        f"({p['params_source']})。",
        f"- 判别只表述度量:{p['measurement_only_declaration']};"
        f"多重性:{p['multiplicity_declaration']}。",
        f"- H1 规则:{p['h1_rule']}。",
        f"- H2 规则:{p['h2_rule']}。",
        f"- 种子/次数:对照 42 种子(SEED_BASE={p['seeds']['controls']}),"
        f"block bootstrap ×{p['seeds']['reps']} 固定种子;对照 = 同品种/"
        "regime/方向、避同机制 24 根、认知帧同段随机 bar(与 §9 "
        "measure_controls 同引擎,单机校验见 §5)。",
        "",
        "## 1. 数据与接触点统计",
        "",
        "| 品种 | bars | 接触点(dev/test) | H24 尾部丢弃 | vol15_rel NaN(warmup) | 无对照(n_ctrl=0) | 磁带行数 | CHECKSUM |",
        "|---|---|---|---|---|---|---|---|"]
    for sym, info in result["symbols"].items():
        lines.append(
            f"| {sym} | {info['bars']} | {info['contacts']}"
            f"({info['contacts_dev']}/{info['contacts_test']}) "
            f"| {info['dropped_tail_h24']} | {info['vol15_rel_nan']} "
            f"| {info['contacts_missing_ctrl']} "
            f"| {result['tape_manifest_rows_total'][sym]:,} "
            f"| {','.join(result['tape_manifest_zip_checksum'][sym])} |")
    lines += ["", "逐事件×段计数见 reports/aggtrades.json symbols.*.per_event_"
              "segment;接触点明细(压缩)见 §6。", "",
              f"## 2. H1 判别(tape 特征分位切片 ↔ 后续 24h 净期望;incr@1x ATR)", ""]
    if "cells" in h1:
        lines += ["| 机制 × 特征 | dev | test(仅 dev 推进单元,各评一次) |",
                  "|---|---|---|"]
        for key, cell in h1["cells"].items():
            lines.append(f"| {key} | {_cell_md(cell['dev'])} "
                         f"| {_cell_md(cell['test'])} |")
        lines += ["",
                  f"**H1 判别链**:dev 推进 {h1['n_dev_advanced']}/{h1['n_cells']} 单元;"
                  f"test 过线 **{len(h1['test_passes'])}** 个"
                  f"({', '.join(h1['test_passes']) or '无'})⇒ **{h1['verdict']}**:"
                  f"{h1['chain']}。",
                  "",
                  f"±15m/60m 档敏感性(描述性,不另开判别线):15m 档过线 "
                  f"{h1['test_passes_by_tier']['15m'] or '无'};60m 档过线 "
                  f"{h1['test_passes_by_tier']['60m'] or '无'}。", ""]
    else:
        lines += [f"H1 未评估:{h1.get('reason', '—')}", ""]
    lines += ["## 3. H2 吸收签名(高 vol15_rel + 低 eff_15m 拒绝接触 ⇒ 拒绝残增厚)", ""]
    if "thresholds" in h2:
        th = h2["thresholds"]
        lines += [
            f"- 阈(dev 60d 拒绝接触计算后冻结):vol15_rel ≥ "
            f"**{_f(th['vol15_rel_threshold'])}**(q{th['vol15_rel_q']}) 且 "
            f"eff_15m ≤ **{th['eff_15m_threshold']:.2e}**(q{th['eff_15m_q']})。",
            f"- 样本:dev 拒绝接触 {h2['n_dev']}(吸收 {h2['n_dev_absorption']},"
            f"dev 吸收残幅均值 {_f(h2.get('dev_absorption_mfe_incr_mean'))});"
            f"test 拒绝接触 {h2['n_test']}(吸收 {h2['n_test_absorption']} / "
            f"非吸收 {h2['n_test_non_absorption']})。", ""]
        comp = h2.get("population_composition", {})
        if comp:
            lines += ["判别人口构成(§9 对照池口径下 incr 定义的接触数/总接触数):",
                      "",
                      "| 段 | 事件 | total | incr 有效 |", "|---|---|---|---|"]
            for seg in ("dev", "test"):
                for eid, c in sorted(comp.get(seg, {}).items()):
                    lines.append(f"| {seg} | {eid} | {c['total']} "
                                 f"| {c['valid_incr']} |")
            lines.append("")
        if "h2a_vs_control" in h2:
            a, b = h2["h2a_vs_control"], h2["h2b_vs_non_absorption"]
            lines += [
                f"- **H2a**(吸收残幅 vs 对照,ATR):mean = "
                f"**{_f(a['mean_mfe_incr'])}**,CI95 "
                f"[{_f(a['ci95'][0])}, {_f(a['ci95'][1])}] ⇒ "
                f"{'**过**' if a['pass'] else '不过'}。",
                f"- **H2b**(吸收 vs 非吸收):diff = "
                f"**{_f(b['diff_absorption_minus_non'])}**,CI95 "
                f"[{_f(b['ci95'][0])}, {_f(b['ci95'][1])}](非吸收均值 "
                f"{_f(b['non_absorption_mean_mfe_incr'])})⇒ "
                f"{'**过**' if b['pass'] else '不过'}。",
                "",
                f"**H2 判别链(两个都过才算增量存在)**:{h2['chain']} ⇒ "
                f"**{h2['verdict']}**。"]
        else:
            lines += [f"**H2 判别:{h2.get('inference')}** — "
                      f"{h2.get('reason', '')}"]
            ref = h2.get("reference_only_not_adjudicated")
            if ref:
                a, b = ref["h2a_vs_control"], ref["h2b_vs_non_absorption"]
                lines += ["",
                          f"参考值 — {ref['declaration']}:H2a mean = "
                          f"{_f(a['mean_mfe_incr'])} CI95 "
                          f"[{_f(a['ci95'][0])}, {_f(a['ci95'][1])}];H2b diff = "
                          f"{_f(b['diff_absorption_minus_non'])} CI95 "
                          f"[{_f(b['ci95'][0])}, {_f(b['ci95'][1])}]"
                          f"(非吸收均值 {_f(b['non_absorption_mean_mfe_incr'])})。"]
    else:
        lines += [f"H2:{h2.get('inference', h2.get('reason', '—'))}"]
    lines += ["", "## 4. §3 参照基线(接触 vs 对照特征均值,描述性)", "",
              "| 品种 | 特征 | 接触 n/均值/中位 | 对照 n/均值/中位 |",
              "|---|---|---|---|"]
    for sym, base in result["feature_baseline_§3"].items():
        for col, r in base.items():
            lines.append(
                f"| {sym} | {col} | {r['contact_n']}/{_f(r['contact_mean'])}"
                f"/{_f(r['contact_median'])} | {r['ctrl_n']}"
                f"/{_f(r['ctrl_mean'])}/{_f(r['ctrl_median'])} |")
    lines += ["", "## 5. 引擎校验", ""]
    for v in result["draw_engine_verifications"]:
        lines.append(f"- {v['symbol']}:{v.get('check')} ⇒ "
                     f"n={v.get('n_compared')},max|diff|={v.get('max_abs_diff')},"
                     f"passed={v.get('passed')}")
    lines += ["", "## 6. 疑似问题与硬声明", ""]
    for i, it in enumerate(result.get("issues_hard_declarations", []), 1):
        lines.append(f"{i}. {it}")
    lines += ["", "## 7. 产物索引", "",
              f"- 机器可读全量:`reports/aggtrades.json`",
              f"- 接触点明细(压缩):`{result['detail_path']}`", ""]
    return "\n".join(lines)


def main() -> None:
    REPORTS.mkdir(exist_ok=True)
    if "--render-only" in sys.argv:
        result = json.loads((REPORTS / "aggtrades.json").read_text(encoding="utf-8"))
        (REPORTS / "aggtrades.md").write_text(render_md(result), encoding="utf-8")
        print("-> reports/aggtrades.md (render-only)")
        return
    parser = argparse.ArgumentParser(prog="run_aggtrades_study")
    parser.add_argument("--skip-klines-fetch", action="store_true")
    parser.add_argument("--symbols", default=None,
                        help="逗号分隔;缺省 = canonical symbols(分阶段/冒烟用)")
    args, _ = parser.parse_known_args(
        [a for a in sys.argv[1:] if a != "--render-only"])
    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else None)
    result = run(skip_klines_fetch=args.skip_klines_fetch, symbols=symbols)
    (REPORTS / "aggtrades.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS / "aggtrades.md").write_text(render_md(result), encoding="utf-8")
    print("-> reports/aggtrades.json, reports/aggtrades.md, "
          "reports/aggtrades_contacts.csv.gz")


if __name__ == "__main__":
    main()
