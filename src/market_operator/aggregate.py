"""v3 观察尺度聚合(docs/v3-protocol.md §1,冻结)。

由带哈希的 1h 数据(data/raw/{SYMBOL}_1h.csv,data/manifest.json 可复现到
同一批原始字节)UTC 对齐聚合出 TF ∈ {2h, 4h, 12h, 1d}:

- 桶 id = ts // step_ms(step_ms = TF 小时 × 3_600_000),分组聚合 OHLCV:
  open=first / high=max / low=min / close=last / volume=sum;输出 bar 的 ts =
  桶起点(区间起点,UTC 对齐,§1"bar 开盘时刻为区间起点")。
- 首/末边界可为不完整桶(1h 数据起点 20:00 UTC 未对齐 12h/1d 网格),如实
  保留——不丢数据,总量守恒:Σvolume 与 1h 全量一致、首 open / 末 close 与
  1h 一致(测试断言);相邻桶 ts 严格等距(step_ms),validate(step_ms) 零
  缺口(测试断言)。
- 产物 data/raw/{SYMBOL}_{TF}.csv,并追加 manifest:sha256/行数/首末 ts/
  来源(1h 聚合,含源流文件与其 sha256)。**不重抓 API,不改任何先验参数。**
- dev/test 拐点(v3-protocol §2):同一拐点 = 1h bar 索引 12320(int(17600×
  0.7),与 research.split_boundary 同源)对应的 ts,逐 TF 断言落在该 TF 帧
  的 split_boundary 索引桶内(tests/test_aggregate.py 逐 TF 核算)。

幂等与确定性:同 1h 输入 ⇒ 输出 CSV 字节完全一致;manifest 条目按文件名键
覆盖重写(字段不含易变量),重复运行不产生漂移。

v13 增量(docs/v13-protocol.md §1,加性,既有函数逐位不变):级联触发尺度
2h 战役的 trigger 帧 = 30m,由 15m 帧(data/raw/{SYMBOL}_15m.csv,带哈希)
2:1 聚合 —— aggregate_to_step(任意 step_ms 的 UTC 对齐桶,与 aggregate()
同一 OHLCV 语义)+ aggregate_symbol_30m / ensure_30m(写 CSV + manifest,
条目含源流 15m sha256 与锚定 1h 逐位对账记录;30m 桶 2:1 回 1h 不与盘上
1h 帧分叉 —— 构建即硬断言)。
"""

from __future__ import annotations

import hashlib
import json
from io import StringIO
from pathlib import Path

import pandas as pd

from . import data as mo_data

MS_PER_HOUR = 3_600_000
MS_15M = 900_000
MS_30M = 1_800_000                    # v13:级联触发 2h 战役 trigger 帧步长
TF_HOURS: dict = {"1h": 1, "2h": 2, "4h": 4, "12h": 12, "1d": 24}
_TF_DIVISORS = tuple(sorted({v for v in TF_HOURS.values()}))

ROOT = mo_data.ROOT
DATA_DIR = mo_data.DATA_DIR
MANIFEST_PATH = mo_data.MANIFEST_PATH


def raw_path(symbol: str, tf_label: str, data_dir: Path = DATA_DIR) -> Path:
    if tf_label not in TF_HOURS:
        raise ValueError(f"unknown tf_label {tf_label!r} (allowed {sorted(TF_HOURS)})")
    return data_dir / "raw" / f"{symbol}_{tf_label}.csv"


def aggregate(df: pd.DataFrame, tf_hours: int) -> pd.DataFrame:
    """UTC 对齐分组聚合 1h OHLCV → tf_hours 小时帧(纯函数,不读写盘)。

    桶 id = ts // step_ms;open=first/high=max/low=min/close=last/volume=sum;
    输出 ts = 桶起点(= 桶 id × step_ms,UTC 对齐)。首/末不完整桶保留。
    """
    if tf_hours not in _TF_DIVISORS:
        raise ValueError(f"tf_hours must be one of {_TF_DIVISORS}, got {tf_hours}")
    step_ms = tf_hours * MS_PER_HOUR
    d = df.sort_values("ts").reset_index(drop=True)
    if d.empty:
        return d.copy()[mo_data.COLUMNS]
    d = d.assign(_bucket=(d["ts"].astype("int64") // step_ms).astype("int64"))
    g = d.groupby("_bucket", sort=True)
    out = g.agg(open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum")).reset_index()
    out["ts"] = (out["_bucket"] * step_ms).astype("int64")
    return out[mo_data.COLUMNS]


def _csv_bytes(df: pd.DataFrame) -> bytes:
    """CSV 落盘编码的唯一实现(写盘与幂等比对共用,LF 换行,字节确定)。"""
    buf = StringIO()
    df.to_csv(buf, index=False, columns=mo_data.COLUMNS, lineterminator="\n")
    return buf.getvalue().encode("utf-8")


def load_tf(symbol: str, tf_label: str, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """读聚合帧(与 data.load 同一整理口径:去重 ts、升序)。"""
    df = pd.read_csv(raw_path(symbol, tf_label, data_dir), dtype={"ts": "int64"})
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def _manifest_record(out_path: Path, df: pd.DataFrame, tf_label: str,
                     src_file: str, src_sha: str, issues: list) -> dict:
    ts = df.sort_values("ts")["ts"]
    try:
        path_repr = str(out_path.relative_to(DATA_DIR.parent))
    except ValueError:                       # 测试用临时 data_dir(树外)
        path_repr = str(out_path)
    return {
        "path": path_repr,
        "sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
        "rows": int(len(df)),
        "ts_first": int(ts.iloc[0]),
        "ts_last": int(ts.iloc[-1]),
        "span_days": round(float(ts.iloc[-1] - ts.iloc[0]) / 86_400_000, 2),
        "source": f"1h聚合(UTC 对齐,docs/v3-protocol.md §1;tf={tf_label})",
        "aggregated_from": {"file": src_file, "sha256": src_sha},
        "validation_issues": list(issues),
    }


def aggregate_symbol(symbol: str, tf_label: str,
                     data_dir: Path = DATA_DIR,
                     manifest_path: Path = MANIFEST_PATH) -> Path:
    """1h → tf_label 聚合:写 CSV + 追加 manifest(幂等,同输入同字节)。"""
    if tf_label not in TF_HOURS or tf_label == "1h":
        raise ValueError("aggregate_symbol 只服务 "
                         f"{sorted(k for k in TF_HOURS if k != '1h')};"
                         "1h 走 data.fetch")
    if data_dir is DATA_DIR:
        df_1h = mo_data.load(symbol)
    else:
        df_1h = pd.read_csv(data_dir / "raw" / f"{symbol}_1h.csv",
                            dtype={"ts": "int64"})
    df = aggregate(df_1h, TF_HOURS[tf_label])
    step_ms = TF_HOURS[tf_label] * MS_PER_HOUR
    issues = mo_data.validate(df, step_ms=step_ms)

    out_path = raw_path(symbol, tf_label, data_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_csv_bytes(df))          # 与 _csv_bytes 比对同源 ⇒ 幂等

    src_file = f"{symbol}_1h.csv"
    src_sha = hashlib.sha256(
        (data_dir / "raw" / src_file).read_bytes()).hexdigest()
    manifest: dict = {"files": {}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[f"{symbol}_{tf_label}.csv"] = _manifest_record(
        out_path, df, tf_label, src_file, src_sha, issues)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return out_path


def ensure_aggregated(symbols: list, tf_labels: list) -> dict:
    """v3 脚本入口级幂等保障:对每个 (symbol, tf) 重算聚合,与盘上字节不一致
    (或缺失)才重写并更新 manifest;同输入绝不漂移。返回 {(sym, tf): path}。"""
    out = {}
    for sym in symbols:
        for tf in tf_labels:
            if tf == "1h":
                continue
            out_path = raw_path(sym, tf)
            df_new = aggregate(mo_data.load(sym), TF_HOURS[tf])
            if not (out_path.exists()
                    and out_path.read_bytes() == _csv_bytes(df_new)):
                aggregate_symbol(sym, tf)
            out[(sym, tf)] = out_path
    return out


# ---------------------------------------------------------------------------
# v13 级联触发:30m 帧(15m 2:1 聚合,锚定 1h 逐位对账)
# ---------------------------------------------------------------------------


def raw_30m_path(symbol: str, data_dir: Path = DATA_DIR) -> Path:
    """30m 帧路径(30m 非 TF_HOURS 成员 —— 由 15m 聚合,不走 raw_path)。"""
    return data_dir / "raw" / f"{symbol}_30m.csv"


def aggregate_to_step(df: pd.DataFrame, step_ms: int) -> pd.DataFrame:
    """UTC 对齐分组聚合任意细帧 → step_ms 帧(v13 增量;纯函数,不读写盘)。

    桶 id = ts // step_ms、OHLCV 规则、ts = 桶起点,与 aggregate() 同一语义,
    差别仅在 step 不限于整小时除数(15m→30m、30m→1h 对账用)。输入必须严格
    升序网格(调用方以 validate 担保);首/末不完整桶如实保留。
    """
    if step_ms <= 0:
        raise ValueError(f"step_ms must be positive, got {step_ms}")
    d = df.sort_values("ts").reset_index(drop=True)
    if d.empty:
        return d.copy()[mo_data.COLUMNS]
    d = d.assign(_bucket=(d["ts"].astype("int64") // int(step_ms)).astype("int64"))
    g = d.groupby("_bucket", sort=True)
    out = g.agg(open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
                volume=("volume", "sum")).reset_index()
    out["ts"] = (out["_bucket"] * int(step_ms)).astype("int64")
    return out[mo_data.COLUMNS]


def _manifest_record_30m(out_path: Path, df: pd.DataFrame, src_file: str,
                         src_sha: str, anchor_sha: str, issues: list) -> dict:
    ts = df.sort_values("ts")["ts"]
    try:
        path_repr = str(out_path.relative_to(DATA_DIR.parent))
    except ValueError:                       # 测试用临时 data_dir(树外)
        path_repr = str(out_path)
    return {
        "path": path_repr,
        "sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
        "rows": int(len(df)),
        "ts_first": int(ts.iloc[0]),
        "ts_last": int(ts.iloc[-1]),
        "span_days": round(float(ts.iloc[-1] - ts.iloc[0]) / 86_400_000, 2),
        "source": "15m聚合(UTC 对齐,docs/v13-protocol.md §1;tf=30m)",
        "aggregated_from": {"file": src_file, "sha256": src_sha},
        "anchored_to_1h": {"file": anchor_sha[0], "sha256": anchor_sha[1],
                           "check": "30m 帧 2:1 聚合 → 1h == 盘上 1h 帧"
                                    " 逐位(OHLC+ts;构图前硬断言)"},
        "validation_issues": list(issues),
    }


def aggregate_symbol_30m(symbol: str, data_dir: Path = DATA_DIR,
                         manifest_path: Path = MANIFEST_PATH) -> Path:
    """15m → 30m 聚合:写 CSV + 追加 manifest(幂等,同输入同字节)。

    锚定 1h 对账(构建前置硬断言,违即 raise,不落盘):30m 帧 2:1 聚合
    → 1h 必须与盘上 1h CSV 逐位一致(OHLC+ts);15m→1h 的同型对账由 v10
    管线(run_backtest_v10.prepare_frames_v10)在每轮运行复查。
    """
    if data_dir is DATA_DIR:
        df_15m = mo_data.load(symbol, "15m")
        df_1h = mo_data.load(symbol)
    else:
        df_15m = pd.read_csv(data_dir / "raw" / f"{symbol}_15m.csv",
                             dtype={"ts": "int64"})
        df_1h = pd.read_csv(data_dir / "raw" / f"{symbol}_1h.csv",
                            dtype={"ts": "int64"})
    df = aggregate_to_step(df_15m, MS_30M)
    issues = mo_data.validate(df, step_ms=MS_30M)

    compact = aggregate_to_step(df, MS_PER_HOUR)
    exact = all((compact[c] - df_1h[c]).abs().max() == 0.0
                for c in ("open", "high", "low", "close"))
    if not exact or not (compact["ts"] == df_1h["ts"]).all():
        raise RuntimeError(f"{symbol} 30m 聚合 -> 1h != 盘上 1h 帧"
                           "(锚定对账失败)")
    out_path = raw_30m_path(symbol, data_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_csv_bytes(df))          # 与 _csv_bytes 比对同源 ⇒ 幂等

    src_file = f"{symbol}_15m.csv"
    src_sha = hashlib.sha256(
        (data_dir / "raw" / src_file).read_bytes()).hexdigest()
    anchor = (f"{symbol}_1h.csv",
              hashlib.sha256(
                  (data_dir / "raw" / f"{symbol}_1h.csv").read_bytes()).hexdigest())
    manifest: dict = {"files": {}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[f"{symbol}_30m.csv"] = _manifest_record_30m(
        out_path, df, src_file, src_sha, anchor, issues)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
    return out_path


def ensure_30m(symbols: list) -> dict:
    """v13 脚本入口级幂等保障:缺失或字节漂移才重写 30m(同 ensure_aggregated)。
    盘上存在但要求重算时由 ensure 比对;返回 {sym: path}。"""
    out = {}
    for sym in symbols:
        out_path = raw_30m_path(sym)
        df_new = aggregate_to_step(mo_data.load(sym, "15m"), MS_30M)
        if not (out_path.exists()
                and out_path.read_bytes() == _csv_bytes(df_new)):
            aggregate_symbol_30m(sym)
        out[sym] = out_path
    return out
