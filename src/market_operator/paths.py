"""运行期目录的唯一事实源(跨平台,零硬编码盘符/家目录假设)。

背景
----
2026-10 之前的运维产物分散在仓库根的 `data/` `logs/` `runtime/` `reports/`,
换机器时只能"整个文件夹拷走",而代码/配置/产物又混在一起。本模块把**运行期
目录**抽成一层:

* `legacy`(默认):仍是仓库根下的旧目录 —— 现有实盘、脚本、测试行为**零变化**;
* `portable`:所有运行期产物落到 `<repo>/var/{data,logs,runtime,reports,...}`,
  `bootstrap` 在**全新主机**上自动启用,已有实盘必须显式 `mo migrate-data`
  完成"复制 → 校验 → 激活",避免 runner 换目录后读不到旧 state。

切换优先级(高 → 低):
1. 环境变量 `MARKET_OPERATOR_LAYOUT=legacy|portable`
2. 环境变量 `MARKET_OPERATOR_DATA_ROOT`(绝对/相对仓库根)→ portable
3. `config/paths.json` 的 `layout` / `storage_root`
4. `<repo>/var/.mo-layout.json` 标记(拷贝整个文件夹时随目录一起走)
5. 默认 legacy

刻意不按"目录是否存在"自动推断:空 `var/`、研究报告、测试临时目录都可能
误触发切换;布局是**部署事实**,只能由人/迁移工具写入。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]

LAYOUT_ENV = "MARKET_OPERATOR_LAYOUT"
HOME_ENV = "MARKET_OPERATOR_HOME"
DATA_ROOT_ENV = "MARKET_OPERATOR_DATA_ROOT"
LEGACY_LAYOUT = "legacy"
PORTABLE_LAYOUT = "portable"

CONFIG_REL = Path("config") / "paths.json"
MARKER_REL = Path("var") / ".mo-layout.json"
MIGRATION_NAME = "MIGRATION.json"

# 运行期类别。portable 下全部位于 <store>/<category>;legacy 下前四个沿用
# 仓库根旧目录,其余(备份/导出/缓存/临时)始终放 `var/`,避免和源码混在一起。
CATEGORIES = ("data", "logs", "runtime", "reports",
              "backups", "exports", "cache", "tmp")
_LEGACY_CATEGORIES = ("data", "logs", "runtime", "reports")

# 迁移默认只搬"可丢但难重建"的运行期事实:日志与 runtime。`data/` 里既有
# 研究 CSV 也有 git 跟踪文件,必须显式 --include-data;`reports/` 是研究
# 产物(大量 git 跟踪),不参与迁移,避免把仓库变成两份。
MIGRATE_CATEGORIES = ("logs", "runtime")


class LayoutError(RuntimeError):
    """布局配置非法 / 迁移前置条件不满足(错误信息直接给运维看)。"""


def app_home() -> Path:
    """仓库(或部署包)根目录;`MARKET_OPERATOR_HOME` 可覆盖(测试/多副本)。"""
    raw = os.environ.get(HOME_ENV) or os.environ.get("MO_HOME")
    return Path(raw).expanduser().resolve() if raw else ROOT


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _normalise_layout(value: object) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in (LEGACY_LAYOUT, "repo", "old"):
        return LEGACY_LAYOUT
    if text in (PORTABLE_LAYOUT, "var", "packaged"):
        return PORTABLE_LAYOUT
    return None


def layout_config_path() -> Path:
    return app_home() / CONFIG_REL


def layout_marker_path() -> Path:
    return app_home() / MARKER_REL


def layout_mode() -> str:
    """返回 `legacy` 或 `portable`(纯读取,不创建任何目录)。"""
    env = _normalise_layout(os.environ.get(LAYOUT_ENV))
    if env is not None:
        return env
    if os.environ.get(DATA_ROOT_ENV) or os.environ.get("MO_DATA_ROOT"):
        return PORTABLE_LAYOUT
    cfg = _read_json(layout_config_path())
    mode = _normalise_layout(cfg.get("layout"))
    if mode is not None:
        return mode
    if cfg.get("storage_root"):
        return PORTABLE_LAYOUT
    if layout_marker_path().exists():
        return PORTABLE_LAYOUT
    return LEGACY_LAYOUT


def is_portable() -> bool:
    return layout_mode() == PORTABLE_LAYOUT


def store_root() -> Path:
    """portable 的根(`var/`);legacy 下也用于 backups/exports/cache/tmp。"""
    raw = os.environ.get(DATA_ROOT_ENV) or os.environ.get("MO_DATA_ROOT")
    if raw:
        p = Path(raw).expanduser()
        return (p if p.is_absolute() else app_home() / p).resolve()
    cfg = _read_json(layout_config_path())
    if cfg.get("storage_root"):
        p = Path(str(cfg["storage_root"])).expanduser()
        return (p if p.is_absolute() else app_home() / p).resolve()
    return app_home() / "var"


def category_root(name: str) -> Path:
    if name not in CATEGORIES:
        raise LayoutError(f"未知运行期目录类别:{name!r}(可选 {CATEGORIES})")
    if is_portable() or name not in _LEGACY_CATEGORIES:
        return store_root() / name
    return app_home() / name


def data_dir() -> Path:
    return category_root("data")


def log_dir() -> Path:
    return category_root("logs")


def runtime_dir() -> Path:
    return category_root("runtime")


def report_dir() -> Path:
    return category_root("reports")


def backup_dir() -> Path:
    return category_root("backups")


def export_dir() -> Path:
    return category_root("exports")


def cache_dir() -> Path:
    return category_root("cache")


def tmp_dir() -> Path:
    return category_root("tmp")


def resolve(rel: str | Path) -> Path:
    """把配置里的相对路径解析到当前布局。

    绝对路径原样返回(用户显式指定优先)。仅当首段是运行期类别时才改写;
    `config/...`、`src/...` 等代码/配置路径永远锚定仓库根。
    """
    p = Path(rel)
    if p.is_absolute():
        return p
    parts = p.parts
    if parts and parts[0] in CATEGORIES:
        return category_root(parts[0]).joinpath(*parts[1:])
    return app_home() / p


def resolve_existing(rel: str | Path) -> Path:
    """只读解析:portable 下若新位置还没有、旧位置有,则回退旧位置。

    仅用于"迁移未完成但已有人手工写了 config/paths.json"的只读场景;所有
    写入路径必须用 `resolve`(严格),避免 state 被劈成两半。
    """
    p = Path(rel)
    if p.is_absolute():
        return p
    target = resolve(p)
    if is_portable() and not target.exists():
        legacy = app_home() / p
        if legacy.exists():
            return legacy
    return target


def ensure_layout() -> dict[str, Path]:
    """创建全部运行期目录并返回类别 → 路径;幂等。"""
    out: dict[str, Path] = {}
    for name in CATEGORIES:
        p = category_root(name)
        p.mkdir(parents=True, exist_ok=True)
        out[name] = p
    # runner 依赖的同级子目录(与 live_runner 的默认推导保持一致)
    live = runtime_dir() / "live"
    for sub in ("candles", "paper_check"):
        (live / sub).mkdir(parents=True, exist_ok=True)
    return out


def activate_portable(*, storage_root: str = "var",
                      reason: str = "manual") -> Path:
    """写入 `config/paths.json` + `var/.mo-layout.json`(不做任何迁移)。

    调用方必须先完成数据复制与校验;本函数只负责"从下一个进程开始生效"。
    """
    cfg = {"layout": PORTABLE_LAYOUT, "storage_root": storage_root,
           "activated_at_utc": _now_iso(), "reason": reason}
    path = layout_config_path()
    _write_json_atomic(path, cfg)
    _write_json_atomic(layout_marker_path(), cfg)
    return path


def activate_legacy(*, reason: str = "manual") -> None:
    """显式回到旧目录(保留 var 数据,不删除任何文件)。"""
    _write_json_atomic(layout_config_path(),
                       {"layout": LEGACY_LAYOUT, "reason": reason,
                        "activated_at_utc": _now_iso()})
    try:
        layout_marker_path().unlink()
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def layout_report() -> dict:
    """当前布局的机器可读快照(doctor/status/package 使用)。"""
    cats = {name: str(category_root(name)) for name in CATEGORIES}
    return {
        "home": str(app_home()),
        "layout": layout_mode(),
        "store_root": str(store_root()),
        "portable": is_portable(),
        "categories": cats,
        "config": str(layout_config_path()),
        "marker": str(layout_marker_path()),
        "config_exists": layout_config_path().exists(),
        "marker_exists": layout_marker_path().exists(),
    }


def dir_size(path: str | Path) -> tuple[int, int]:
    total = count = 0
    root = Path(path)
    if not root.exists():
        return (0, 0)
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
                count += 1
        except OSError:
            continue
    return (total, count)


def disk_usage(path: str | Path | None = None) -> dict:
    target = Path(path) if path else store_root()
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return {"path": str(target), "probe": str(probe),
            "total": usage.total, "used": usage.used, "free": usage.free,
            "free_gb": round(usage.free / (1 << 30), 3),
            "used_pct": round(100.0 * usage.used / usage.total, 1)
            if usage.total else 0.0}


def category_usage() -> dict[str, dict]:
    out = {}
    for name in CATEGORIES:
        p = category_root(name)
        size, files = dir_size(p)
        out[name] = {"path": str(p), "bytes": size, "files": files,
                     "exists": p.exists()}
    return out


def legacy_snapshot() -> dict:
    home = app_home()
    out = {}
    for name in _LEGACY_CATEGORIES:
        p = home / name
        size, files = dir_size(p)
        out[name] = {"path": str(p), "exists": p.exists(),
                     "bytes": size, "files": files}
    return out



def _sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            blob = fh.read(chunk)
            if not blob:
                break
            h.update(blob)
    return h.hexdigest()


def _git_tracked(home: Path) -> set[str]:
    """git 跟踪文件(相对 posix)。非 git 目录返回空集。"""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(home), "ls-files", "-z"],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    return {x for x in out.stdout.decode("utf-8", "replace").split("\0") if x}


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass


def migrate_legacy(*, move: bool = False, include_data: bool = False,
                   force: bool = False, dry_run: bool = False,
                   categories: Optional[Iterable[str]] = None) -> dict:
    """把旧目录复制到 `var/`,逐文件 sha256 校验通过后才激活 portable。

    * 默认只迁 `logs` / `runtime`;`data` 需 `include_data=True`;
    * `move=True` 只删除**已校验且未被 git 跟踪**的原文件(git 跟踪的
      研究 CSV 留在仓库,避免 `git status` 出现大批删除);
    * runner 心跳新鲜时拒绝迁移(可用 `force=True` 演练),防止边跑边搬;
    * 任一步失败 → 不写激活配置,原目录不动。
    """
    home = app_home()
    if is_portable():
        raise LayoutError("当前已是 portable 布局;如需重迁先 activate_legacy()")
    cats = tuple(categories or MIGRATE_CATEGORIES)
    if include_data and "data" not in cats:
        cats = cats + ("data",)
    unknown = [c for c in cats if c not in _LEGACY_CATEGORIES]
    if unknown:
        raise LayoutError(f"不可迁移的类别:{unknown}")

    heartbeat = home / "runtime" / "live" / "heartbeat.json"
    hb_doc = _read_json(heartbeat)
    # `dry_run` 不落任何字节,连心跳闸门也放行:运维需要"先看清单再决定停机"。
    if hb_doc and not force and not dry_run:
        from . import heartbeat as hb
        age = hb.beat_age_seconds(hb_doc)
        if age is not None and age < 600.0:
            raise LayoutError(
                f"实盘心跳仍新鲜(age={age:.0f}s);先 `mo stop` 或加 --force")

    target_home = store_root()
    tracked = _git_tracked(home) if move else set()
    entries: list[dict] = []
    errors: list[dict] = []
    for cat in cats:
        src_root = home / cat
        dst_root = target_home / cat
        if not src_root.exists():
            continue
        for src in sorted(src_root.rglob("*")):
            if not src.is_file():
                continue
            rec = {"src": str(src),
                   "dst": str(dst_root / src.relative_to(src_root)),
                   "size": src.stat().st_size}
            try:
                if not dry_run:
                    dst = Path(rec["dst"])
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not (dst.exists()
                            and dst.stat().st_size == rec["size"]):
                        shutil.copy2(src, dst)
                    digest = _sha256(dst)
                    rec["sha256"] = digest
                    if digest != _sha256(src):
                        errors.append({**rec, "why": "sha256 不一致"})
                        continue
                entries.append(rec)
            except OSError as e:
                errors.append({**rec, "why": f"{type(e).__name__}: {e}"})

    result = {"dry_run": dry_run, "move": move, "categories": list(cats),
              "target": str(target_home), "copied": len(entries),
              "errors": errors, "tracked_skipped": 0}
    if errors or dry_run:
        return result

    # 只有全部校验通过才落迁移清单并激活;清单本身是回滚/审计证据。
    manifest = {"schema": 1, "migrated_at_utc": _now_iso(),
                "home": str(home), "target": str(target_home),
                "categories": list(cats), "files": entries}
    _write_json_atomic(target_home / MIGRATION_NAME, manifest)

    deleted = 0
    if move:
        for rec in entries:
            src = Path(rec["src"])
            rel = src.relative_to(home).as_posix()
            if rel in tracked:
                result["tracked_skipped"] += 1
                continue
            try:
                src.unlink()
                deleted += 1
            except OSError:
                pass
        for cat in cats:
            _prune_empty_dirs(home / cat)

    activate_portable(storage_root=_rel_to_home(target_home), reason="migrate")
    ensure_layout()
    result["activated"] = True
    result["deleted"] = deleted
    return result


def _rel_to_home(path: Path) -> str:
    try:
        return path.resolve().relative_to(app_home()).as_posix()
    except ValueError:
        return str(path.resolve())

