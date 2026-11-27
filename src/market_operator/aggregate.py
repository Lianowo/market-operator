"""观察尺度 OHLCV 聚合与多周期帧构建。

由带哈希的 1h 数据(``data/raw/{SYMBOL}_1h.csv``, ``data/manifest.json``)
UTC 对齐聚合出 ``TF ∈ {2h, 4h, 12h, 1d}``,并支持由 15m 帧 2:1 聚合出 30m
触发帧(构建时硬断言 30m 2:1 聚合回 1h 与盘上 1h 帧逐位一致)。

- 桶 id = ``ts // step_ms``,OHLCV 规则为 ``open=first / high=max / low=min /
  close=last / volume=sum``,输出 ``ts`` 为桶起点(UTC 对齐);
- 首/末不完整桶如实保留以保证总量守恒;
- 幂等确定性:同源输入 ⇒ 输出 CSV(LF)字节完全一致。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from . import data as mo_data
from .exceptions import DataIntegrityError
from .timeframes import MS_15M, MS_30M, MS_PER_DAY, MS_PER_HOUR

TF_HOURS: dict[str, int] = {"1h": 1, "2h": 2, "4h": 4, "12h": 12, "1d": 24}
_TF_DIVISORS = tuple(sorted(set(TF_HOURS.values())))

ROOT = mo_data.ROOT
DATA_DIR = mo_data.DATA_DIR
MANIFEST_PATH = mo_data.MANIFEST_PATH

_csv_bytes = mo_data.csv_bytes


def raw_path(symbol: str, tf_label: str, data_dir: Path = DATA_DIR) -> Path:
    if tf_label not in TF_HOURS:
        raise ValueError(f"unknown tf_label {tf_label!r} (allowed {sorted(TF_HOURS)})")
    return data_dir / "raw" / f"{symbol}_{tf_label}.csv"


def aggregate_to_step(df: pd.DataFrame, step_ms: int) -> pd.DataFrame:
    """UTC 对齐分组聚合任意细帧 → ``step_ms`` 帧(纯函数,不读写盘)。

    桶 id = ``ts // step_ms``、OHLCV 聚合规则与输出 ``ts`` 为桶起点;
    首/末不完整桶如实保留。
    """
    if step_ms <= 0:
        raise ValueError(f"step_ms must be positive, got {step_ms}")
    step = int(step_ms)
    d = df.sort_values("ts").reset_index(drop=True)
    if d.empty:
        return d.copy()[mo_data.COLUMNS]
    d = d.assign(_bucket=(d["ts"].astype("int64") // step).astype("int64"))
    grouped = d.groupby("_bucket", sort=True)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()
    out["ts"] = (out["_bucket"] * step).astype("int64")
    return out[mo_data.COLUMNS]


def aggregate(df: pd.DataFrame, tf_hours: int) -> pd.DataFrame:
    """UTC 对齐分组聚合 1h OHLCV → ``tf_hours`` 小时帧(纯函数,不读写盘)。"""
    if tf_hours not in _TF_DIVISORS:
        raise ValueError(f"tf_hours must be one of {_TF_DIVISORS}, got {tf_hours}")
    return aggregate_to_step(df, tf_hours * MS_PER_HOUR)


def load_tf(symbol: str, tf_label: str, data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """读聚合帧(与 data.load 同一整理口径:去重 ts、升序)。"""
    df = pd.read_csv(raw_path(symbol, tf_label, data_dir), dtype={"ts": "int64"})
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


def _base_manifest_record(
    out_path: Path,
    df: pd.DataFrame,
    *,
    source: str,
    src_file: str,
    src_sha: str,
    issues: Sequence[str],
) -> dict[str, Any]:
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
        "span_days": round(float(ts.iloc[-1] - ts.iloc[0]) / MS_PER_DAY, 2),
        "source": source,
        "aggregated_from": {"file": src_file, "sha256": src_sha},
        "validation_issues": list(issues),
    }


def _update_manifest(manifest_path: Path, filename: str, record: dict[str, Any]) -> None:
    manifest: dict[str, Any] = {"files": {}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[filename] = record
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _manifest_record(
    out_path: Path,
    df: pd.DataFrame,
    tf_label: str,
    src_file: str,
    src_sha: str,
    issues: Sequence[str],
) -> dict[str, Any]:
    return _base_manifest_record(
        out_path,
        df,
        source=f"1h聚合(UTC 对齐;tf={tf_label})",
        src_file=src_file,
        src_sha=src_sha,
        issues=issues,
    )


def aggregate_symbol(
    symbol: str,
    tf_label: str,
    data_dir: Path = DATA_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> Path:
    """1h → ``tf_label`` 聚合:写 CSV + 追加 manifest(幂等,同输入同字节)。"""
    if tf_label not in TF_HOURS or tf_label == "1h":
        raise ValueError(
            "aggregate_symbol 只服务 "
            f"{sorted(k for k in TF_HOURS if k != '1h')};1h 走 data.fetch"
        )
    if data_dir is DATA_DIR:
        df_1h = mo_data.load(symbol)
    else:
        df_1h = pd.read_csv(data_dir / "raw" / f"{symbol}_1h.csv", dtype={"ts": "int64"})
    df = aggregate(df_1h, TF_HOURS[tf_label])
    step_ms = TF_HOURS[tf_label] * MS_PER_HOUR
    issues = mo_data.validate(df, step_ms=step_ms)

    out_path = raw_path(symbol, tf_label, data_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_csv_bytes(df))

    src_file = f"{symbol}_1h.csv"
    src_sha = hashlib.sha256((data_dir / "raw" / src_file).read_bytes()).hexdigest()
    record = _manifest_record(out_path, df, tf_label, src_file, src_sha, issues)
    _update_manifest(manifest_path, f"{symbol}_{tf_label}.csv", record)
    return out_path


def ensure_aggregated(
    symbols: Sequence[str], tf_labels: Sequence[str]
) -> dict[tuple[str, str], Path]:
    """幂等聚合保障:缺失或字节漂移时重写并更新 manifest;返回 ``{(sym, tf): path}``。"""
    out: dict[tuple[str, str], Path] = {}
    for sym in symbols:
        for tf in tf_labels:
            if tf == "1h":
                continue
            out_path = raw_path(sym, tf)
            df_new = aggregate(mo_data.load(sym), TF_HOURS[tf])
            if not (out_path.exists() and out_path.read_bytes() == _csv_bytes(df_new)):
                aggregate_symbol(sym, tf)
            out[(sym, tf)] = out_path
    return out


# ---------------------------------------------------------------------------
# 30m 触发帧(15m 2:1 聚合,锚定 1h 逐位对账)
# ---------------------------------------------------------------------------


def raw_30m_path(symbol: str, data_dir: Path = DATA_DIR) -> Path:
    """30m 帧路径(30m 由 15m 聚合,不走 raw_path)。"""
    return data_dir / "raw" / f"{symbol}_30m.csv"


def _manifest_record_30m(
    out_path: Path,
    df: pd.DataFrame,
    src_file: str,
    src_sha: str,
    anchor_sha: tuple[str, str],
    issues: Sequence[str],
) -> dict[str, Any]:
    rec = _base_manifest_record(
        out_path,
        df,
        source="15m聚合(UTC 对齐;tf=30m)",
        src_file=src_file,
        src_sha=src_sha,
        issues=issues,
    )
    rec["anchored_to_1h"] = {
        "file": anchor_sha[0],
        "sha256": anchor_sha[1],
        "check": "30m 帧 2:1 聚合 → 1h == 盘上 1h 帧 逐位(OHLC+ts;构图前硬断言)",
    }
    return rec


def aggregate_symbol_30m(
    symbol: str,
    data_dir: Path = DATA_DIR,
    manifest_path: Path = MANIFEST_PATH,
) -> Path:
    """15m → 30m 聚合:写 CSV + 追加 manifest(幂等,同输入同字节)。

    锚定 1h 对账(构建前置硬断言,违即 raise,不落盘):30m 帧 2:1 聚合
    → 1h 必须与盘上 1h CSV 逐位一致(OHLC+ts)。
    """
    if data_dir is DATA_DIR:
        df_15m = mo_data.load(symbol, "15m")
        df_1h = mo_data.load(symbol)
    else:
        df_15m = pd.read_csv(data_dir / "raw" / f"{symbol}_15m.csv", dtype={"ts": "int64"})
        df_1h = pd.read_csv(data_dir / "raw" / f"{symbol}_1h.csv", dtype={"ts": "int64"})
    df = aggregate_to_step(df_15m, MS_30M)
    issues = mo_data.validate(df, step_ms=MS_30M)

    compact = aggregate_to_step(df, MS_PER_HOUR)
    exact = all(
        (compact[col] - df_1h[col]).abs().max() == 0.0
        for col in ("open", "high", "low", "close")
    )
    if not exact or not (compact["ts"] == df_1h["ts"]).all():
        raise DataIntegrityError(f"{symbol} 30m 聚合 -> 1h != 盘上 1h 帧(锚定对账失败)")

    out_path = raw_30m_path(symbol, data_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(_csv_bytes(df))

    src_file = f"{symbol}_15m.csv"
    src_sha = hashlib.sha256((data_dir / "raw" / src_file).read_bytes()).hexdigest()
    anchor = (
        f"{symbol}_1h.csv",
        hashlib.sha256((data_dir / "raw" / f"{symbol}_1h.csv").read_bytes()).hexdigest(),
    )
    record = _manifest_record_30m(out_path, df, src_file, src_sha, anchor, issues)
    _update_manifest(manifest_path, f"{symbol}_30m.csv", record)
    return out_path


def ensure_30m(symbols: Sequence[str]) -> dict[str, Path]:
    """幂等保障:缺失或字节漂移时重写 30m 聚合帧;返回 ``{sym: path}``。"""
    out: dict[str, Path] = {}
    for sym in symbols:
        out_path = raw_30m_path(sym)
        df_new = aggregate_to_step(mo_data.load(sym, "15m"), MS_30M)
        if not (out_path.exists() and out_path.read_bytes() == _csv_bytes(df_new)):
            aggregate_symbol_30m(sym)
        out[sym] = out_path
    return out


__all__ = [
    "DATA_DIR",
    "MANIFEST_PATH",
    "MS_15M",
    "MS_30M",
    "MS_PER_HOUR",
    "ROOT",
    "TF_HOURS",
    "aggregate",
    "aggregate_symbol",
    "aggregate_symbol_30m",
    "aggregate_to_step",
    "ensure_30m",
    "ensure_aggregated",
    "load_tf",
    "raw_30m_path",
    "raw_path",
]
