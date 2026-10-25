"""存储治理脚本(`scripts/storage_gc.py`)的判定与安全边界测试。

治理工具的风险是"删错东西":故把 ① .gz 孪生体的无损判据、② 压缩后可还原、
③ 白名单边界(源数据/配置/代码绝不被自动清理)钉成断言。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import storage_gc as sg                                          # noqa: E402


def _args(**kw):
    base = dict(log_days=14.0, log_max_days=180.0, log_max_mb=32,
                backup_days=7.0, cold_mb=8, cold_days=30.0,
                candle_stale_days=14.0, archive_max_days=365.0,
                gzip_candles=False,
                dedupe_gz=True, compress_cold=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_human_units():
    assert sg.human(999) == "999.0B"
    assert sg.human(2048) == "2.0KB"
    assert sg.human(5 << 30) == "5.0GB"


def test_gz_twin_ok_accepts_lossless_twin(tmp_path):
    data = b"a,b\n1,2\n" * 1000
    orig = _write(tmp_path / "x.csv", data)
    with gzip.open(str(orig) + ".gz", "wb") as fh:
        fh.write(data)
    ok, why = sg._gz_twin_ok(orig)
    assert ok and why == ""


def test_gz_twin_ok_rejects_size_mismatch_and_missing(tmp_path):
    orig = _write(tmp_path / "x.csv", b"abc" * 10)
    assert sg._gz_twin_ok(orig)[0] is False          # 无孪生体
    with gzip.open(str(orig) + ".gz", "wb") as fh:
        fh.write(b"short")
    ok, why = sg._gz_twin_ok(orig)
    assert ok is False and "尺寸不符" in why


def test_gzip_keep_is_lossless_and_restorable(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "ROOT", tmp_path)
    monkeypatch.setattr(sg, "MANIFEST",
                        tmp_path / "reports" / "_storage_manifest.jsonl")
    data = b"0123456789" * 10_000
    orig = _write(tmp_path / "reports" / "big.csv", data)

    ok, msg = sg.gzip_keep(orig, dry=False)

    assert ok, msg
    assert not orig.exists()
    gz = orig.with_name(orig.name + ".gz")
    assert gz.exists() and gzip.open(gz, "rb").read() == data
    rec = json.loads(sg.MANIFEST.read_text(encoding="utf-8").strip())
    assert rec["orig"] == "reports/big.csv" and rec["size"] == len(data)

    assert sg.restore_from_manifest(dry=False) == 1
    assert orig.read_bytes() == data and not gz.exists()


def test_protection_prefers_git_tracking_over_code_refs():
    """证据优先:被 git 跟踪就一律不自动压缩,哪怕它没被代码引用。"""
    tracked = {"reports/frozen.csv"}
    assert sg._protection("reports/frozen.csv", [], tracked) != ""
    assert "git" in sg._protection("reports/frozen.csv", [], tracked)
    assert sg._protection("reports/scratch.csv", [], tracked) == ""


def test_compress_cold_skips_tracked_and_compresses_untracked(tmp_path,
                                                              monkeypatch):
    monkeypatch.setattr(sg, "ROOT", tmp_path)
    monkeypatch.setattr(sg, "MANIFEST", tmp_path / "reports" / "_m.jsonl")
    frozen = _write(tmp_path / "reports" / "frozen.csv", b"a" * (2 << 20))
    scratch = _write(tmp_path / "reports" / "scratch.csv", b"b" * (2 << 20))
    monkeypatch.setattr(sg, "_tracked_files",
                        lambda: {"reports/frozen.csv"})

    acts = sg.apply_safe(dry=False,
                         args=_args(compress_cold=True, cold_days=0,
                                    cold_mb=1))

    assert frozen.exists() and not frozen.with_name(frozen.name + ".gz").exists()
    assert not scratch.exists() and scratch.with_name(scratch.name + ".gz").exists()
    assert any("git 跟踪" in a for a in acts)


def test_apply_safe_never_touches_source_data(tmp_path, monkeypatch):
    """白名单边界:data/config/src 下的再大文件也不会被自动清理。"""
    monkeypatch.setattr(sg, "ROOT", tmp_path)
    monkeypatch.setattr(sg, "MANIFEST",
                        tmp_path / "reports" / "_storage_manifest.jsonl")
    raw = _write(tmp_path / "data" / "raw" / "big.csv", b"x" * (5 << 20))
    keep = _write(tmp_path / "reports" / "keep.csv", b"y" * (5 << 20))
    dedup = _write(tmp_path / "reports" / "dup.csv", b"z" * (5 << 20))
    with gzip.open(str(dedup) + ".gz", "wb") as fh:
        fh.write(b"z" * (5 << 20))
    pyc = _write(tmp_path / "scripts" / "__pycache__" / "m.pyc", b"q")

    acts = sg.apply_safe(dry=False, args=_args())

    assert raw.exists(), "源数据被误删"
    assert keep.exists(), "无孪生体的大文件被误删"
    assert not dedup.exists(), "有损判据未生效"
    assert not pyc.exists(), "缓存未清理"
    assert any("去重" in a for a in acts)


# ---------------------------------------------------------------------------
# 布局(portable):清理与 manifest 必须跟着 `paths` 走,而不是仓库根
# ---------------------------------------------------------------------------


def _portable(monkeypatch, tmp_path):
    """把 storage_gc 钉到临时目录里的 portable 布局(<tmp>/var)。"""
    monkeypatch.setattr(sg, "ROOT", tmp_path)
    monkeypatch.setattr(sg, "_ops_root", lambda: tmp_path / "var")
    monkeypatch.setattr(sg, "MANIFEST", tmp_path / "var" / "reports" / "_m.jsonl")


def test_ops_root_and_logical_rels(tmp_path, monkeypatch):
    """物理路径是 `var/...`,逻辑相对路径仍是 `logs/...`(白名单/热档判据跨布局稳定)。"""
    _portable(monkeypatch, tmp_path)

    assert sg.op("logs/storage_gc.log") == tmp_path / "var" / "logs" / "storage_gc.log"
    logical = sg.op_rel(tmp_path / "var" / "runtime" / "live" / "state.json")
    assert logical == "runtime/live/state.json"
    assert sg.is_hot(logical), "portable 下热档判据必须仍然命中"


def test_inventory_splits_store_in_portable(tmp_path, monkeypatch):
    """报告里 `var` 要按类别拆开,否则看不见增长源(日志/数据各占多少)。"""
    _portable(monkeypatch, tmp_path)
    _write(tmp_path / "var" / "logs" / "a.log", b"x" * 10)
    _write(tmp_path / "src" / "m.py", b"y" * 5)

    inv = sg.inventory()

    assert inv["var/logs"] == (10, 1)
    assert inv["src"] == (5, 1)
    assert "var" not in inv, "var 被整包算了一次,会与拆分后的类别重复计数"


def test_portable_store_is_governed(tmp_path, monkeypatch):
    """portable 下 `var/logs` 必须被治理:修前所有清理都钉在仓库根,`var/` 无人收。"""
    _portable(monkeypatch, tmp_path)
    stale = (time.time() - 20 * 86400,) * 2       # 超过默认 log_days=14
    store_log = _write(tmp_path / "var" / "logs" / "audit.jsonl", b'{"x":1}\n' * 10)
    legacy_log = _write(tmp_path / "logs" / "audit.jsonl", b'{"x":1}\n' * 10)
    for p in (store_log, legacy_log):
        os.utime(p, stale)

    acts = sg.apply_safe(dry=False, args=_args())

    assert not store_log.exists(), "portable 的日志目录没被治理"
    assert store_log.with_name("audit.jsonl.gz").exists()
    assert legacy_log.exists(), "legacy 残留目录不归 portable 的 gc 管"
    assert any("audit.jsonl" in a for a in acts)


def test_manifest_is_layout_independent(tmp_path, monkeypatch):
    """manifest 存逻辑路径 ⇒ portable 下压缩的文件在 portable 下也能还原。"""
    _portable(monkeypatch, tmp_path)
    data = b"0123456789" * 5_000
    orig = _write(tmp_path / "var" / "reports" / "big.csv", data)

    ok, msg = sg.gzip_keep(orig, dry=False)
    assert ok, msg
    rec = json.loads(sg.MANIFEST.read_text(encoding="utf-8").strip())
    assert rec["orig"] == "reports/big.csv", "manifest 不该带 var/ 前缀"

    assert sg.restore_from_manifest(dry=False) == 1
    assert orig.read_bytes() == data
