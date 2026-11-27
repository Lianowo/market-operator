"""存储治理:盘点 → 可逆压缩 → 保留期清理 → 增长预测 → 预算闸门。

设计原则(与"信息量不降、密度提升"对齐):
① **只自动清理可再生产物**(日志、备份副本、`__pycache__`、`.pytest_cache`、
   临时文件、已有 `.gz` 孪生体的原文件)。源数据(`data/raw`)、配置、正在被
   runner 使用的热档(`runtime/live/*.json`)、研究冻结产物一律不动;
② 大文件压缩是**可逆**的:写 `reports/_storage_manifest.jsonl`(原名/大小/
   sha256/mtime),`--restore` 可原样还原;压缩后立即校验解压尺寸,不一致即放弃;
③ **删字节不删信息**:控制台日志超限时先整份 gzip 归档(`logs/*.log.gz`)再截断,
   归档校验通过才截断;审计 JSONL 只做"明文 → .gz",`.gz` 默认**永不删除**
   (`--log-max-days 0`),一年审计日志压缩后通常只有几十 MB;
④ 被代码引用或 git 跟踪的文件不压缩(避免打断 `tools/replay_acceptance.py`
   这类读固定路径的闸门,也避免冻结证据变成巨大的 git 变更);
⑤ 预算默认 35GB(软 30GB),超出即非零退出;`--enforce` 供看护进程每日调用。

运行期目录(数据/日志/runtime/报告)经 `market_operator.paths` 解析:legacy 布局
下仍是仓库根旧目录(与修前逐字相同),portable 布局下是 `<repo>/var/...`(或
`MARKET_OPERATOR_DATA_ROOT`)。白名单、热档判据、manifest 里存的都是**逻辑相对
路径**(`logs/x.log`、`runtime/live/state.json`),两种布局写法一致,`--restore`
因此可以跨布局还原。

用法:
    python scripts/storage_gc.py                    # 只读盘点 + 计划 + 增长预测
    python scripts/storage_gc.py --apply            # 执行安全清理
    python scripts/storage_gc.py --enforce           # 执行 + 预算闸门(看护每日调用)
    python scripts/storage_gc.py --apply --compress-cold   # 另压冷研究产物
    python scripts/storage_gc.py --restore          # 按 manifest 还原
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from market_operator import paths                                        # noqa: E402

IO_CHUNK_BYTES = 1 << 20
BIG_FILE_THRESHOLD = 8 << 20
DEDUPE_MIN_BYTES = 4 << 20
CONSOLE_KEEP_BYTES = 4 << 20
BYTES_PER_GB = 1 << 30
SECONDS_PER_DAY = 86_400.0
SECONDS_PER_HOUR = 3_600.0
TMP_STALE_HOURS = 24.0
GZIP_LEVEL = 6

CONSOLE_LOG_NAMES = (
    "live_stdout.log",
    "live_stderr.log",
    "watchdog.log",
    "storage_gc.log",
    "supervisor.log",
)

# 热档白名单:runner 正在读写,任何"压缩/截断/删除"都会让它读到半截数据
HOT_FILES = (
    "runtime/live/state.json",
    "runtime/live/campaigns.json",
    "runtime/live/heartbeat.json",
    "runtime/live/supervisor.json",
    "runtime/live/live_runner.lock",
    "runtime/live/runner.lock",
    "runtime/live/supervisor.lock",
)
OPS_PREFIXES = ("logs/", "runtime/", "data/")


def _ops_root() -> Path:
    """运行期目录的物理根:legacy = 仓库根;portable = `<repo>/var`(或 data_root)。

    测试会把 `ROOT` 指到临时目录,而 `paths.app_home()` 仍指向真实仓库 ——
    只有两者一致才采纳布局,否则按 legacy 处理(临时目录里的语义一目了然)。
    """
    with suppress(OSError, ValueError):
        if ROOT.resolve() == paths.app_home():
            return paths.store_root() if paths.is_portable() else ROOT
    return ROOT


def op(rel: str | Path) -> Path:
    """逻辑相对路径 → 物理路径(`logs/x.log` 在 portable 下是 `var/logs/x.log`)。"""
    p = Path(str(rel).replace("\\", "/"))
    return p if p.is_absolute() else _ops_root() / p


def op_rel(path: str | Path) -> str:
    """物理路径 → 逻辑相对路径(跨布局稳定;白名单/manifest/输出都用它)。"""
    p = Path(path)
    for base in (_ops_root(), ROOT):
        try:
            return p.relative_to(base).as_posix()
        except ValueError:
            continue
    return p.as_posix()


def repo_rel(path: str | Path) -> str:
    """物理路径 → 仓库相对路径(只用于和 `git ls-files` 的结果比对)。"""
    try:
        return Path(path).relative_to(ROOT).as_posix()
    except ValueError:
        return op_rel(path)


def _scan_roots() -> list[Path]:
    """盘点的物理根:仓库根 + portable 且数据根在仓外时的运行期根。"""
    roots = [ROOT]
    ops = _ops_root()
    if ops != ROOT:
        try:
            ops.relative_to(ROOT)
        except ValueError:
            roots.append(ops)
    return roots


MANIFEST = op("reports") / "_storage_manifest.jsonl"


def human(n: float) -> str:
    """Format byte size with one decimal place across all units (e.g. '999.0B')."""
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(val) < 1024.0:
            return f"{val:,.1f}{unit}"
        val /= 1024.0
    return f"{val:,.1f}PB"


def dir_size(path: Path) -> tuple[int, int]:
    total, count = 0, 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            count += 1
    return total, count


_CODE_TEXT_CACHE: dict[Path, str] = {}
_TRACKED_CACHE: dict[Path, set[str]] = {}


def _code_files() -> list[Path]:
    out: list[Path] = []
    for d in ("scripts", "src", "tests", "tools", "research"):
        p = ROOT / d
        if p.exists():
            out.extend(p.rglob("*.py"))
    return out


def _code_text() -> str:
    """所有生产/研究/测试代码的文本并集(按 ROOT 缓存一次,避免逐文件重复读盘)。"""
    key = ROOT.resolve()
    cached = _CODE_TEXT_CACHE.get(key)
    if cached is None:
        parts: list[str] = []
        for f in _code_files():
            try:
                parts.append(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
        cached = "\n".join(parts)
        _CODE_TEXT_CACHE[key] = cached
    return cached


def _referenced_in_code(
    name: str,
    code_files: list[Path] | None = None,
) -> bool:
    """文件 basename 是否出现在代码里(读取或写入都算)。

    无法区分读写,所以它只是**第二道**护栏;第一道是 `_tracked_files()`
    (git 跟踪 = 冻结证据,一律不自动动)。
    """
    if code_files is None:
        return name in _code_text()
    for f in code_files:
        try:
            if name in f.read_text(encoding="utf-8", errors="ignore"):
                return True
        except OSError:
            continue
    return False


def _tracked_files() -> set[str]:
    """git 跟踪的文件集合(POSIX 相对路径);取不到返回空集(退化为只用代码引用判据)。

    为什么需要:`reports/` 里的大件多是**冻结研究证据**(docs 引用、逐格可复现)。
    自动压缩/删除它们会产生巨大的 git 变更并让文档引用落空。判据必须比
    "是否被代码文本提到"更硬 —— 被 git 跟踪就一律不自动动。
    """
    key = ROOT.resolve()
    cached = _TRACKED_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        cached = {p for p in (out.stdout or "").split("\0") if p}
    except (OSError, subprocess.SubprocessError):
        cached = set()
    _TRACKED_CACHE[key] = cached
    return cached


def _protection(
    rel: str,
    code: list[Path] | None = None,
    tracked: set[str] | None = None,
) -> str:
    """返回不自动压缩的原因("" = 可压缩)。顺序即优先级:证据 > 被读 > 可压。"""
    tracked_set = tracked if tracked is not None else _tracked_files()
    if rel.replace("\\", "/") in tracked_set:
        return "git 跟踪(冻结证据)"
    if _referenced_in_code(Path(rel).name, code):
        return "被代码引用(可能是读取输入)"
    return ""


def _count_gzip_bytes(gz: Path) -> int:
    total = 0
    with gzip.open(gz, "rb") as fh:
        while chunk := fh.read(IO_CHUNK_BYTES):
            total += len(chunk)
    return total


def _gz_twin_ok(orig: Path) -> tuple[bool, str]:
    """`orig + .gz` 是否存在且解压后与原文件字节数一致(无损判据)。"""
    gz = orig.with_name(orig.name + ".gz")
    if not gz.exists():
        return False, "无 .gz"
    try:
        n = _count_gzip_bytes(gz)
    except (OSError, EOFError) as exc:
        return False, f".gz 损坏:{type(exc).__name__}"
    orig_size = orig.stat().st_size
    if n != orig_size:
        return False, f"解压尺寸不符 {n} != {orig_size}"
    return True, ""


def _sha256(path: Path) -> str:
    return paths.file_sha256(path)


def _split_store(entry: Path) -> dict[str, tuple[int, int]]:
    """portable 下把 `var/` 拆成 `var/<类别>`(否则报告里只有一个 `var`,看不出增长源)。"""
    out: dict[str, tuple[int, int]] = {}
    for sub in sorted(entry.iterdir()):
        if sub.is_dir():
            out[f"var/{sub.name}"] = dir_size(sub)
        elif sub.is_file():
            out[f"var/{sub.name}"] = (sub.stat().st_size, 1)
    return out


def inventory() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    store = _ops_root()
    seen_store = store == ROOT
    for entry in sorted(ROOT.iterdir()):
        with suppress(OSError):
            if not seen_store and entry.resolve() == store.resolve():
                out.update(_split_store(entry))
                seen_store = True
                continue
        if entry.is_dir():
            out[entry.name] = dir_size(entry)
        elif entry.is_file():
            out[entry.name] = (entry.stat().st_size, 1)
    if not seen_store and store.exists():           # 数据根在仓库外
        out.update(_split_store(store))
    return out


def _collect_large_files(threshold: int = BIG_FILE_THRESHOLD) -> list[tuple[int, Path]]:
    big: list[tuple[int, Path]] = []
    for base in _scan_roots():
        for p in base.rglob("*"):
            if not p.is_file() or ".git" in p.relative_to(base).parts:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size >= threshold:
                big.append((size, p))
    big.sort(key=lambda t: -t[0])
    return big


def print_report(
    inv: Mapping[str, tuple[int, int]],
    budget_gb: float,
    soft_gb: float,
) -> float:
    total = sum(v[0] for v in inv.values())
    print("=" * 74)
    print("存储盘点")
    print("=" * 74)
    for name, (size, n) in sorted(inv.items(), key=lambda kv: -kv[1][0]):
        if n:
            print(f"  {name:<22}{human(size):>11}{n:>8} files")
    print(f"  {'TOTAL':<22}{human(total):>11}")
    print()
    big = _collect_large_files()
    print(f"  ≥8MB 文件 {len(big)} 个(前 10;[…] 为不会自动压缩的原因):")
    tracked = _tracked_files()
    for s, p in big[:10]:
        why = _protection(repo_rel(p), None, tracked)
        suffix = f"  [{why}]" if why else ""
        print(f"    {human(s):>10}  {op_rel(p)}{suffix}")
    print()
    gb = total / BYTES_PER_GB
    status = "OK"
    if gb > budget_gb:
        status = f"超预算 {gb - budget_gb:.2f}GB"
    elif gb > soft_gb:
        status = f"接近预算(软线 {soft_gb}GB)"
    print(f"预算:{gb:.2f}GB / 硬 {budget_gb}GB(软 {soft_gb}GB)→ {status}")
    return gb


def is_hot(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return rel in HOT_FILES or rel.endswith(".lock")


def growth_stats(days: float = 7.0) -> dict[str, Any]:
    """近 N 天的写入速率(按顶层目录),用于"这条曲线还能跑多久"的预测。

    口径:`mtime` 落在窗口内的文件大小求和 / 窗口天数。不复用 `inventory()`
    (那是总量,不是速率)。窗口内被 GC 压掉的字节不再计入 —— 预测因此偏保守。

    预测只用**运维目录**(`logs/` `runtime/` `data/`)的速率:研究报告
    (`reports/`)是一次性产物,混进来会把"实盘还能跑多久"估得离谱。
    """
    now = time.time()
    span = max(1.0, days)
    horizon = now - span * SECONDS_PER_DAY
    per: dict[str, dict[str, int]] = {}
    total = ops_total = 0
    for base in _scan_roots():
        for p in base.rglob("*"):
            if not p.is_file() or ".git" in p.relative_to(base).parts:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < horizon:
                continue
            rel = op_rel(p)                     # 逻辑相对路径(portable 下不含 var/)
            parts = rel.split("/")
            key = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
            slot = per.setdefault(key, {"bytes": 0, "files": 0})
            slot["bytes"] += st.st_size
            slot["files"] += 1
            total += st.st_size
            if rel.startswith(OPS_PREFIXES):
                ops_total += st.st_size
    inv = inventory()
    size_gb = sum(v[0] for v in inv.values()) / BYTES_PER_GB
    ops_rate = ops_total / span
    return {
        "days": days,
        "window_bytes": total,
        "bytes_per_day": total / span,
        "ops_bytes_per_day": ops_rate,
        "size_gb": size_gb,
        "per_bucket": per,
        "projected_30d_gb": size_gb + ops_rate * 30 / BYTES_PER_GB,
    }


def print_growth(stats: Mapping[str, Any], budget_gb: float) -> None:
    rate = float(stats["ops_bytes_per_day"])
    print()
    print(
        f"增长(近 {stats['days']:.0f} 天写入窗口):运维目录 {human(rate)}/天"
        f"(全仓 {human(stats['bytes_per_day'])}/天,含一次性研究产物);"
        f"当前 {stats['size_gb']:.2f}GB → "
        f"30 天后约 {stats['projected_30d_gb']:.2f}GB"
    )
    if rate > 0:
        left = max(0.0, budget_gb - float(stats["size_gb"])) * BYTES_PER_GB
        print(f"按运维写入速率距硬预算 {budget_gb}GB 还有 {left / rate:.0f} 天")
    else:
        print("窗口内几乎没有运维写入(或刚做过治理)")


def archive_and_truncate(path: Path, keep_bytes: int, dry: bool) -> tuple[bool, str]:
    """控制台日志:整份 gzip 归档 → 校验 → 截断为尾部 `keep_bytes`。

    直接截断会永久丢掉故障现场;先归档就变成"省空间但不丢信息":
    `logs/live_stdout_20261022_031500.log.gz` 里是完整历史,当前文件留尾巴
    供 `tail`/排障即时查看。归档失败即放弃截断(宁可不省空间)。
    """
    size = path.stat().st_size
    if size <= keep_bytes:
        return False, f"{path.name} 无需处理"
    if dry:
        return True, f"[dry] {path.name} {human(size)} → 归档 + 尾部 {human(keep_bytes)}"
    gz = path.with_name(
        f"{path.stem}_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}.gz"
    )
    try:
        with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=GZIP_LEVEL) as dst:
            shutil.copyfileobj(src, dst, IO_CHUNK_BYTES)
        n = _count_gzip_bytes(gz)
        if n != size:
            gz.unlink(missing_ok=True)
            return False, f"{path.name} 归档校验失败({n} != {size}),未截断"
        with path.open("rb") as fh:
            fh.seek(max(0, size - keep_bytes))
            tail = fh.read()
        with path.open("wb") as fh:
            fh.write(
                f"[storage_gc] 已归档至 {gz.name} 后截断;"
                f"此处保留尾部 {human(len(tail))}\n".encode()
            )
            fh.write(tail)
        return True, (
            f"{path.name} {human(size)} → 归档 {gz.name} + "
            f"尾部 {human(len(tail))}"
        )
    except OSError as exc:
        gz.unlink(missing_ok=True)
        return False, f"{path.name} 归档失败:{exc}"


def gzip_keep(
    path: Path,
    dry: bool,
    *,
    manifest: bool = True,
) -> tuple[bool, str]:
    """压缩为 .gz 并删除原文件;压缩后校验解压尺寸,不符则保留原文件。

    `manifest=False` 用于可再生产物(K 线缓存等):它们的"原文件"随时可由交易所
    重建,记进还原清单只会让清单本身变成新的膨胀源。
    """
    gz = path.with_name(path.name + ".gz")
    if gz.exists():
        return False, "同名 .gz 已存在(跳过)"
    if dry:
        return True, f"[dry] {op_rel(path)} → {gz.name}"
    try:
        with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=GZIP_LEVEL) as dst:
            shutil.copyfileobj(src, dst, IO_CHUNK_BYTES)
        before = path.stat().st_size
        n = _count_gzip_bytes(gz)
        if n != before:
            gz.unlink(missing_ok=True)
            return False, f"校验失败({n} != {before}),已丢弃 .gz"
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        if manifest:
            with MANIFEST.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "orig": op_rel(path),
                    "size": before,
                    "sha256": _sha256(path),
                }, ensure_ascii=False) + "\n")
        path.unlink()
        return True, (
            f"{op_rel(path)} → .gz ({human(before)} → {human(gz.stat().st_size)})"
        )
    except OSError as exc:
        gz.unlink(missing_ok=True)
        return False, f"压缩失败:{exc}"


def restore_from_manifest(dry: bool) -> int:
    if not MANIFEST.exists():
        print("无 manifest,无需还原")
        return 0
    n = 0
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        gz = op(rec["orig"] + ".gz")
        orig = op(rec["orig"])
        if not gz.exists() or orig.exists():
            continue
        print(("  [dry] " if dry else "  ") + f"还原 {rec['orig']}")
        if not dry:
            with gzip.open(gz, "rb") as src, orig.open("wb") as dst:
                shutil.copyfileobj(src, dst, IO_CHUNK_BYTES)
            gz.unlink()
        n += 1
    print(f"还原 {n} 个文件{'([dry])' if dry else ''}")
    return n


def _clean_logs(
    log_dir: Path,
    now: float,
    args: argparse.Namespace,
    dry: bool,
) -> list[str]:
    acts: list[str] = []
    for p in sorted(log_dir.glob("*.jsonl")):
        if is_hot(op_rel(p)):
            continue
        if (now - p.stat().st_mtime) / SECONDS_PER_DAY > args.log_days:
            ok, msg = gzip_keep(p, dry)
            acts.append(("✔ " if ok else "· ") + msg)
    if args.log_max_days > 0:
        for p in sorted(log_dir.glob("*.gz")):
            if (now - p.stat().st_mtime) / SECONDS_PER_DAY > args.log_max_days:
                if not dry:
                    p.unlink()
                acts.append(f"✔ 删除过期日志归档 {p.name}")
    cap = args.log_max_mb << 20
    for name in CONSOLE_LOG_NAMES:
        p = log_dir / name
        if not p.exists() or p.stat().st_size <= cap:
            continue
        ok, msg = archive_and_truncate(p, CONSOLE_KEEP_BYTES, dry)
        acts.append(("✔ " if ok else "· ") + msg)
    return acts


def _clean_backups(
    rt_dir: Path,
    now: float,
    args: argparse.Namespace,
    dry: bool,
) -> list[str]:
    acts: list[str] = []
    arch = rt_dir / "_archive"
    for z in sorted(arch.glob("*.zip")) if arch.exists() else []:
        if (now - z.stat().st_mtime) / SECONDS_PER_DAY > args.archive_max_days:
            if not dry:
                z.unlink()
            acts.append(f"✔ 删除过期备份归档 {z.name}")
    backup_dirs = sorted(rt_dir.glob("**/*_backup_*")) + sorted(rt_dir.glob("**/_pre_*"))
    for d in backup_dirs:
        if not d.is_dir() or is_hot(op_rel(d)):
            continue
        if (now - d.stat().st_mtime) / SECONDS_PER_DAY < args.backup_days:
            continue
        zip_path = arch / (d.name + ".zip")
        if zip_path.exists():
            if not dry:
                shutil.rmtree(d)
            acts.append(f"✔ 删除已归档的备份目录 {d.name}(zip 已存在)")
            continue
        if dry:
            acts.append(f"[dry] {op_rel(d)} → {zip_path.name}")
            continue
        arch.mkdir(parents=True, exist_ok=True)
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=d)
        shutil.rmtree(d)
        acts.append(
            f"✔ 备份归档 {d.name} → {zip_path.name} ({human(zip_path.stat().st_size)})"
        )
    return acts


def _clean_caches_and_tmps(rt_dir: Path, now: float, dry: bool) -> list[str]:
    acts: list[str] = []
    for base in _scan_roots():
        for pat in ("**/__pycache__", ".pytest_cache"):
            for d in list(base.glob(pat)):
                if not d.is_dir():
                    continue
                if not dry:
                    shutil.rmtree(d, ignore_errors=True)
                acts.append(f"✔ 清理缓存 {op_rel(d)}")
    for p in rt_dir.glob("**/*.tmp"):
        try:
            age_h = (now - p.stat().st_mtime) / SECONDS_PER_HOUR
        except OSError:
            continue
        if age_h < TMP_STALE_HOURS:
            continue
        if not dry:
            p.unlink()
        acts.append(f"✔ 删除陈留临时文件 {op_rel(p)}")
    return acts


def _dedupe_gz_twins(dry: bool) -> list[str]:
    acts: list[str] = []
    tracked = _tracked_files()
    cand = list(op("reports").rglob("*")) + list(op("data").rglob("*"))
    for p in cand:
        if not p.is_file() or p.suffix == ".gz":
            continue
        size = p.stat().st_size
        if size < DEDUPE_MIN_BYTES:
            continue
        rel = op_rel(p)
        if repo_rel(p) in tracked:
            acts.append(f"· 跳过 {rel}:git 跟踪(冻结证据)")
            continue
        ok, why = _gz_twin_ok(p)
        if not ok:
            if why and why != "无 .gz":
                acts.append(f"· 跳过 {rel}:{why}")
            continue
        if not dry:
            p.unlink()
        acts.append(f"✔ 去重(保留 .gz) {rel} {human(size)}")
    return acts


def _compress_cold_reports(
    now: float,
    args: argparse.Namespace,
    dry: bool,
) -> list[str]:
    acts: list[str] = []
    tracked = _tracked_files()
    min_bytes = args.cold_mb << 20
    for p in sorted(op("reports").rglob("*")):
        if not p.is_file() or p.suffix in (".gz", ".md", ".jsonl"):
            continue
        if p.stat().st_size < min_bytes:
            continue
        if (now - p.stat().st_mtime) / SECONDS_PER_DAY < args.cold_days:
            continue
        rel = op_rel(p)
        why = _protection(repo_rel(p), None, tracked)
        if why:
            acts.append(f"· 跳过 {rel}:{why}")
            continue
        ok, msg = gzip_keep(p, dry)
        acts.append(("✔ " if ok else "· ") + msg)
    return acts


def _clean_candles(
    rt_dir: Path,
    now: float,
    args: argparse.Namespace,
    dry: bool,
) -> list[str]:
    candles = rt_dir / "live" / "candles"
    if not candles.exists():
        return []
    acts: list[str] = []
    if getattr(args, "gzip_candles", False):
        for p in sorted(candles.glob("*.json")):
            ok, msg = gzip_keep(p, dry, manifest=False)
            acts.append(("✔ " if ok else "· ") + msg)
    live_syms: set[str] = set()
    cold = rt_dir / "live" / "campaigns.json"
    if cold.exists():
        try:
            live_syms = set(
                json.loads(cold.read_text(encoding="utf-8")).get("campaigns") or {}
            )
        except (OSError, ValueError):
            live_syms = set()
    for p in sorted(list(candles.glob("*.json.gz")) + list(candles.glob("*.json"))):
        sym = p.name.split("_")[0]
        if sym in live_syms:
            continue
        if (now - p.stat().st_mtime) / SECONDS_PER_DAY < args.candle_stale_days:
            continue
        if not dry:
            p.unlink()
        acts.append(f"✔ 删除退市/过期品种缓存 {p.name}")
    return acts


def apply_safe(dry: bool, args: argparse.Namespace) -> list[str]:
    """执行安全清理(仅可再生产物)。返回动作清单。"""
    now = time.time()
    log_dir = op("logs")
    rt_dir = op("runtime")
    acts: list[str] = []
    acts.extend(_clean_logs(log_dir, now, args, dry))
    acts.extend(_clean_backups(rt_dir, now, args, dry))
    acts.extend(_clean_caches_and_tmps(rt_dir, now, dry))
    if args.dedupe_gz:
        acts.extend(_dedupe_gz_twins(dry))
    if args.compress_cold:
        acts.extend(_compress_cold_reports(now, args, dry))
    acts.extend(_clean_candles(rt_dir, now, args, dry))
    return acts


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="storage_gc")
    ap.add_argument("--apply", action="store_true", help="真正执行(默认只盘点)")
    ap.add_argument(
        "--dry-run", action="store_true", help="与 --apply 同路径但不动盘"
    )
    ap.add_argument(
        "--dedupe-gz",
        action="store_true",
        help="删除已有 .gz 孪生体的原文件(无损)",
    )
    ap.add_argument(
        "--compress-cold",
        action="store_true",
        help="压缩冷研究产物(>cold-mb 且 >cold-days 且未被代码引用)",
    )
    ap.add_argument(
        "--restore",
        action="store_true",
        help="按 reports/_storage_manifest.jsonl 还原被压缩文件",
    )
    ap.add_argument(
        "--enforce",
        action="store_true",
        help="执行安全清理 + 增长预测 + 预算闸门(看护每日调用)",
    )
    ap.add_argument(
        "--growth-days",
        type=float,
        default=7.0,
        help="增长预测的观察窗口天数,默认 7",
    )
    ap.add_argument(
        "--log-days",
        type=float,
        default=3.0,
        help="审计日志转 .gz 的天数阈值(只压缩不删除),默认 3",
    )
    ap.add_argument(
        "--log-max-days",
        type=float,
        default=0.0,
        help="日志 .gz 归档上限天数;0 = 永久保留(默认)",
    )
    ap.add_argument(
        "--log-max-mb",
        type=int,
        default=32,
        help="控制台日志截断阈值 MB,默认 32",
    )
    ap.add_argument(
        "--backup-days",
        type=float,
        default=7.0,
        help="runtime/*_backup_* 打 zip 的年龄阈值,默认 7 天",
    )
    ap.add_argument(
        "--cold-mb", type=int, default=8, help="--compress-cold 的最小体积 MB,默认 8"
    )
    ap.add_argument(
        "--cold-days",
        type=float,
        default=30.0,
        help="--compress-cold 的最小年龄天数,默认 30",
    )
    ap.add_argument(
        "--candle-stale-days",
        type=float,
        default=14.0,
        help="非宇宙品种 K 线缓存的保留天数,默认 14",
    )
    ap.add_argument(
        "--gzip-candles",
        action="store_true",
        help="把 K 线缓存转成 .json.gz(仅当运行的 runner 支持 gz 读取)",
    )
    ap.add_argument(
        "--archive-max-days",
        type=float,
        default=365.0,
        help="runtime/_archive 备份 zip 上限天数,默认 365",
    )
    ap.add_argument("--budget-gb", type=float, default=35.0)
    ap.add_argument("--soft-gb", type=float, default=30.0)
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    paths.soften_stdio()
    args = _build_parser().parse_args(argv)

    if args.restore:
        restore_from_manifest(dry=not args.apply)
        return 0

    enforce = args.enforce
    inv = inventory()
    gb_before = print_report(inv, args.budget_gb, args.soft_gb)
    stats = growth_stats(args.growth_days)
    print_growth(stats, args.budget_gb)
    _write_growth_report(stats, inv, args.budget_gb, args.soft_gb, gb_before)
    if not (args.apply or args.dry_run or enforce):
        print(
            "\n(只读模式;加 --apply 执行上述安全清理,"
            "加 --enforce 执行并做预算闸门)"
        )
        return 0 if gb_before <= args.budget_gb else 1

    print()
    print("=" * 74)
    print("执行" + ("" if (args.apply or enforce) else "(dry-run)"))
    print("=" * 74)
    for act in apply_safe(dry=not (args.apply or enforce), args=args):
        print("  " + act)
    if not (args.apply or enforce):
        return 0

    gb_after = print_report(inventory(), args.budget_gb, args.soft_gb)
    print(f"本轮释放 {human(max(0.0, gb_before - gb_after) * BYTES_PER_GB)}")
    if enforce:
        return _gate(gb_after, stats, args.budget_gb)
    return 0 if gb_after <= args.budget_gb else 1


def _write_growth_report(
    stats: Mapping[str, Any],
    inv: Mapping[str, tuple[int, int]],
    budget_gb: float,
    soft_gb: float,
    gb: float,
) -> None:
    """把这次盘点的结论落成 JSON(给看护/人看曲线,不必重跑扫描)。"""
    doc = {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "size_gb": round(gb, 3),
        "budget_gb": budget_gb,
        "soft_gb": soft_gb,
        "bytes_per_day": int(stats["bytes_per_day"]),
        "ops_bytes_per_day": int(stats["ops_bytes_per_day"]),
        "projected_30d_gb": round(float(stats["projected_30d_gb"]), 3),
        "per_bucket": {
            k: {"bytes": v["bytes"], "files": v["files"]}
            for k, v in sorted(stats["per_bucket"].items())
        },
        "inventory": {k: [v[0], v[1]] for k, v in inv.items()},
    }
    try:
        path = op("reports") / "_storage_growth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"  ! 增长报告落盘失败:{exc}")


def _gate(gb_after: float, stats: Mapping[str, Any], budget_gb: float) -> int:
    """预算闸门:超过硬预算 → 退出码 2;预测 30 天内越线 → 1;否则 0。"""
    if gb_after > budget_gb:
        print(f"闸门:已用 {gb_after:.2f}GB > 硬预算 {budget_gb}GB → 需人工扩容/迁移")
        return 2
    if float(stats["projected_30d_gb"]) > budget_gb:
        print(
            f"闸门:30 天预测 {stats['projected_30d_gb']:.2f}GB 将越线"
            f"({budget_gb}GB)→ 建议先跑 --compress-cold"
        )
        return 1
    print(
        f"闸门:通过({gb_after:.2f}GB / {budget_gb}GB;"
        f"30 天预测 {stats['projected_30d_gb']:.2f}GB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
