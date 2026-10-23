"""存储治理:盘点 → 可逆压缩 → 保留期清理 → 预算闸门。

设计原则(与"信息量不降、密度提升"对齐):
① **只自动清理可再生产物**(日志、备份副本、`__pycache__`、`.pytest_cache`、
   临时文件、已有 `.gz` 孪生体的原文件)。源数据(`data/raw`)、配置、
   研究冻结产物一律不动;
② 大文件压缩是**可逆**的:写 `reports/_storage_manifest.jsonl`(原名/大小/
   sha256/mtime),`--restore` 可原样还原;压缩后立即校验解压尺寸,不一致即放弃;
③ 被代码引用的文件不压缩(扫 `*.py` 里的 basename)——避免打断
   `tools/replay_acceptance.py` 这类读固定路径的闸门;
④ 预算默认 35GB(软 30GB),超出即非零退出,便于计划任务/监控接警。

用法:
    python scripts/storage_gc.py                    # 只读盘点 + 计划
    python scripts/storage_gc.py --apply            # 执行安全清理
    python scripts/storage_gc.py --apply --compress-cold   # 另压冷研究产物
    python scripts/storage_gc.py --restore          # 按 manifest 还原
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "reports" / "_storage_manifest.jsonl"

# 自动清理只允许落在这几个前缀下(白名单,防止误删源数据/配置/代码)
AUTO_PREFIXES = ("logs", "runtime", ".pytest_cache", "reports")
COLD_PREFIXES = ("reports",)
NEVER_TOUCH = ("data", "config", "src", "tests", "docs", "research", ".git")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:,.1f}{unit}"
        n /= 1024.0
    return f"{n:,.1f}PB"


def dir_size(path: Path) -> tuple[int, int]:
    total, count = 0, 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            count += 1
    return total, count


def _under(rel: str, prefixes) -> bool:
    return any(rel == p or rel.startswith(p + os.sep) for p in prefixes)


_CODE_TEXT_CACHE: str | None = None


def _code_text() -> str:
    """所有生产/研究/测试代码的文本并集(缓存一次,避免逐文件重复读盘)。"""
    global _CODE_TEXT_CACHE
    if _CODE_TEXT_CACHE is None:
        parts: list[str] = []
        for f in _code_files():
            try:
                parts.append(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
        _CODE_TEXT_CACHE = "\n".join(parts)
    return _CODE_TEXT_CACHE


def _referenced_in_code(name: str,
                        code_files: list[Path] | None = None) -> bool:
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


def _code_files() -> list[Path]:
    out: list[Path] = []
    for d in ("scripts", "src", "tests", "tools", "research"):
        p = ROOT / d
        if p.exists():
            out.extend(p.rglob("*.py"))
    return out


_TRACKED_CACHE: set[str] | None = None


def _tracked_files() -> set[str]:
    """git 跟踪的文件集合(POSIX 相对路径);取不到返回空集(退化为只用代码引用判据)。

    为什么需要:`reports/` 里的大件多是**冻结研究证据**(docs 引用、逐格可复现)。
    自动压缩/删除它们会产生巨大的 git 变更并让文档引用落空。判据必须比
    "是否被代码文本提到"更硬 —— 被 git 跟踪就一律不自动动。
    """
    global _TRACKED_CACHE
    if _TRACKED_CACHE is not None:
        return _TRACKED_CACHE
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=60)
        _TRACKED_CACHE = {p for p in (out.stdout or "").split("\0") if p}
    except (OSError, subprocess.SubprocessError):
        _TRACKED_CACHE = set()
    return _TRACKED_CACHE


def _protection(rel: str, code: list[Path], tracked: set[str] | None = None) -> str:
    """返回不自动压缩的原因("" = 可压缩)。顺序即优先级:证据 > 被读 > 可压。"""
    if (tracked if tracked is not None else _tracked_files()).__contains__(
            rel.replace("\\", "/")):
        return "git 跟踪(冻结证据)"
    if _referenced_in_code(Path(rel).name, code):
        return "被代码引用(可能是读取输入)"
    return ""


def _gz_twin_ok(orig: Path) -> tuple[bool, str]:
    """`orig + .gz` 是否存在且解压后与原文件字节数一致(无损判据)。"""
    gz = orig.with_name(orig.name + ".gz")
    if not gz.exists():
        return False, "无 .gz"
    try:
        n = 0
        with gzip.open(gz, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
    except (OSError, EOFError) as e:
        return False, f".gz 损坏:{type(e).__name__}"
    if n != orig.stat().st_size:
        return False, f"解压尺寸不符 {n} != {orig.stat().st_size}"
    return True, ""


def _sha256(path: Path, limit: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def inventory() -> dict[str, tuple[int, int]]:
    out = {}
    for entry in sorted(ROOT.iterdir()):
        if entry.is_dir():
            out[entry.name] = dir_size(entry)
        elif entry.is_file():
            out[entry.name] = (entry.stat().st_size, 1)
    return out


def print_report(inv: dict, budget_gb: float, soft_gb: float) -> float:
    total = sum(v[0] for v in inv.values())
    print("=" * 74)
    print("存储盘点")
    print("=" * 74)
    for name, (size, n) in sorted(inv.items(), key=lambda kv: -kv[1][0]):
        if n:
            print(f"  {name:<22}{human(size):>11}{n:>8} files")
    print(f"  {'TOTAL':<22}{human(total):>11}")
    print()
    big = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not _under(
                p.relative_to(ROOT).as_posix(), (".git",)):
            s = p.stat().st_size
            if s >= 8 << 20:
                big.append((s, p))
    big.sort(reverse=True)
    print(f"  ≥8MB 文件 {len(big)} 个(前 10;[…] 为不会自动压缩的原因):")
    code = _code_files()
    tracked = _tracked_files()
    for s, p in big[:10]:
        rel = p.relative_to(ROOT).as_posix()
        why = _protection(rel, code, tracked)
        print(f"    {human(s):>10}  {rel}{('  [' + why + ']') if why else ''}")
    print()
    gb = total / (1 << 30)
    status = "OK"
    if gb > budget_gb:
        status = f"超预算 {gb - budget_gb:.2f}GB"
    elif gb > soft_gb:
        status = f"接近预算(软线 {soft_gb}GB)"
    print(f"预算:{gb:.2f}GB / 硬 {budget_gb}GB(软 {soft_gb}GB)→ {status}")
    return gb



def gzip_keep(path: Path, dry: bool, *, manifest: bool = True) -> tuple[bool, str]:
    """压缩为 .gz 并删除原文件;压缩后校验解压尺寸,不符则保留原文件。

    `manifest=False` 用于可再生产物(K 线缓存等):它们的"原文件"随时可由交易所
    重建,记进还原清单只会让清单本身变成新的膨胀源。
    """
    gz = path.with_name(path.name + ".gz")
    if gz.exists():
        return False, "同名 .gz 已存在(跳过)"
    if dry:
        return True, f"[dry] {path.relative_to(ROOT)} → {gz.name}"
    try:
        with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        n = 0
        with gzip.open(gz, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                n += len(chunk)
        if n != path.stat().st_size:
            gz.unlink(missing_ok=True)
            return False, f"校验失败({n} != {path.stat().st_size}),已丢弃 .gz"
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        if manifest:
            with MANIFEST.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc)
                    .isoformat(timespec="seconds"),
                    "orig": path.relative_to(ROOT).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }, ensure_ascii=False) + "\n")
        before = path.stat().st_size
        path.unlink()
        return True, (f"{path.relative_to(ROOT)} → .gz "
                      f"({human(before)} → {human(gz.stat().st_size)})")
    except OSError as e:
        gz.unlink(missing_ok=True)
        return False, f"压缩失败:{e}"


def restore_from_manifest(dry: bool) -> int:
    if not MANIFEST.exists():
        print("无 manifest,无需还原")
        return 0
    n = 0
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        gz = ROOT / (rec["orig"] + ".gz")
        orig = ROOT / rec["orig"]
        if not gz.exists() or orig.exists():
            continue
        print(("  [dry] " if dry else "  ") + f"还原 {rec['orig']}")
        if not dry:
            with gzip.open(gz, "rb") as src, orig.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            gz.unlink()
        n += 1
    print(f"还原 {n} 个文件{'([dry])' if dry else ''}")
    return n


def apply_safe(dry: bool, args) -> list[str]:
    """执行安全清理(仅可再生产物)。返回动作清单。"""
    acts: list[str] = []
    now = time.time()

    # ① 日志:jsonl 过保留期 → gz;.gz 过上限 → 删
    for p in sorted((ROOT / "logs").glob("*.jsonl")):
        if (now - p.stat().st_mtime) / 86400 > args.log_days:
            ok, msg = gzip_keep(p, dry)
            acts.append(("✔ " if ok else "· ") + msg)
    for p in sorted((ROOT / "logs").glob("*.gz")):
        if (now - p.stat().st_mtime) / 86400 > args.log_max_days:
            if not dry:
                p.unlink()
            acts.append(f"✔ 删除过期日志归档 {p.name}")

    # ② 控制台日志超大时截断(保留尾部,避免一次故障日志吃满磁盘)
    cap = args.log_max_mb << 20
    for name in ("live_stdout.log", "live_stderr.log", "watchdog.log",
                 "storage_gc.log"):
        p = ROOT / "logs" / name
        if not p.exists() or p.stat().st_size <= cap:
            continue
        keep = 4 << 20
        with p.open("rb") as fh:
            fh.seek(max(0, p.stat().st_size - keep))
            tail = fh.read()
        if not dry:
            with p.open("wb") as fh:
                fh.write("[storage_gc] 超大日志已截断,仅保留尾部 4MB\n".encode())
                fh.write(tail)
        acts.append(f"✔ 截断 {name}(>{args.log_max_mb}MB → 尾部 4MB)")

    # ③ 旧备份目录:打成 zip 后删除目录(可还原);归档本身也有上限
    arch = ROOT / "runtime" / "_archive"
    for z in sorted(arch.glob("*.zip")) if arch.exists() else []:
        if (now - z.stat().st_mtime) / 86400 > args.archive_max_days:
            if not dry:
                z.unlink()
            acts.append(f"✔ 删除过期备份归档 {z.name}")
    for d in sorted((ROOT / "runtime").glob("**/*_backup_*")):
        if not d.is_dir():
            continue
        if (now - d.stat().st_mtime) / 86400 < args.backup_days:
            continue
        zip_path = arch / (d.name + ".zip")
        if dry:
            acts.append(f"[dry] {d.relative_to(ROOT)} → {zip_path.name}")
            continue
        arch.mkdir(parents=True, exist_ok=True)
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=d)
        shutil.rmtree(d)
        acts.append(f"✔ 备份归档 {d.name} → {zip_path.name} "
                    f"({human(zip_path.stat().st_size)})")

    # ④ 可再生缓存
    for pat in ("**/__pycache__", ".pytest_cache"):
        for d in list(ROOT.glob(pat)):
            if not d.is_dir():
                continue
            if not dry:
                shutil.rmtree(d, ignore_errors=True)
            acts.append(f"✔ 清理缓存 {d.relative_to(ROOT)}")

    # ⑤ 崩溃残留的半截临时文件(>24h)
    for p in ROOT.glob("runtime/**/*.tmp"):
        if (now - p.stat().st_mtime) / 3600 < 24:
            continue
        if not dry:
            p.unlink()
        acts.append(f"✔ 删除陈留临时文件 {p.relative_to(ROOT)}")

    # ⑥ 已有 .gz 孪生体的原文件(无损去重,信息量不变)
    if args.dedupe_gz:
        tracked = _tracked_files()
        for p in list(ROOT.glob("reports/**/*")) + list(ROOT.glob("data/**/*")):
            if not p.is_file() or p.suffix == ".gz":
                continue
            if p.stat().st_size < (4 << 20):
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel in tracked:
                # 冻结证据:即使有 .gz 孪生体也不删,避免 git 删除记录 + 引用落空
                acts.append(f"· 跳过 {rel}:git 跟踪(冻结证据)")
                continue
            ok, why = _gz_twin_ok(p)
            if not ok:
                if why and why != "无 .gz":
                    acts.append(f"· 跳过 {rel}:{why}")
                continue
            size = p.stat().st_size
            if not dry:
                p.unlink()
            acts.append(f"✔ 去重(保留 .gz) {rel} {human(size)}")

    # ⑦ 冷研究产物压缩(可选,可逆;git 跟踪或代码引用的一律跳过)
    if args.compress_cold:
        code = _code_files()
        tracked = _tracked_files()
        for p in sorted((ROOT / "reports").rglob("*")):
            if not p.is_file() or p.suffix in (".gz", ".md", ".jsonl"):
                continue
            if p.stat().st_size < (args.cold_mb << 20):
                continue
            if (now - p.stat().st_mtime) / 86400 < args.cold_days:
                continue
            rel = p.relative_to(ROOT).as_posix()
            why = _protection(rel, code, tracked)
            if why:
                acts.append(f"· 跳过 {rel}:{why}")
                continue
            ok, msg = gzip_keep(p, dry)
            acts.append(("✔ " if ok else "· ") + msg)
    # ⑧ K 线缓存:旧明文 → gz(可选:`--gzip-candles`);退市/过期品种按年龄清理
    candles = ROOT / "runtime" / "live" / "candles"
    if candles.exists():
        if getattr(args, "gzip_candles", False):
            for p in sorted(candles.glob("*.json")):
                ok, msg = gzip_keep(p, dry, manifest=False)
                acts.append(("✔ " if ok else "· ") + msg)
        live_syms: set[str] = set()
        cold = ROOT / "runtime" / "live" / "campaigns.json"
        if cold.exists():
            try:
                live_syms = set(json.loads(
                    cold.read_text(encoding="utf-8")).get("campaigns") or {})
            except ValueError:
                live_syms = set()
        for p in sorted(list(candles.glob("*.json.gz"))
                        + list(candles.glob("*.json"))):
            sym = p.name.split("_")[0]
            if sym in live_syms:
                continue
            if (now - p.stat().st_mtime) / 86400 < args.candle_stale_days:
                continue
            if not dry:
                p.unlink()
            acts.append(f"✔ 删除退市/过期品种缓存 {p.name}")
    return acts




def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="storage_gc")
    ap.add_argument("--apply", action="store_true", help="真正执行(默认只盘点)")
    ap.add_argument("--dry-run", action="store_true",
                    help="与 --apply 同路径但不动盘")
    ap.add_argument("--dedupe-gz", action="store_true",
                    help="删除已有 .gz 孪生体的原文件(无损)")
    ap.add_argument("--compress-cold", action="store_true",
                    help="压缩冷研究产物(>cold-mb 且 >cold-days 且未被代码引用)")
    ap.add_argument("--restore", action="store_true",
                    help="按 reports/_storage_manifest.jsonl 还原被压缩文件")
    ap.add_argument("--log-days", type=float, default=14.0,
                    help="审计日志保留天数(超过即 gz),默认 14")
    ap.add_argument("--log-max-days", type=float, default=180.0,
                    help="日志 .gz 归档上限天数,默认 180")
    ap.add_argument("--log-max-mb", type=int, default=32,
                    help="控制台日志截断阈值 MB,默认 32")
    ap.add_argument("--backup-days", type=float, default=7.0,
                    help="runtime/*_backup_* 打 zip 的年龄阈值,默认 7 天")
    ap.add_argument("--cold-mb", type=int, default=8,
                    help="--compress-cold 的最小体积 MB,默认 8")
    ap.add_argument("--cold-days", type=float, default=30.0,
                    help="--compress-cold 的最小年龄天数,默认 30")
    ap.add_argument("--candle-stale-days", type=float, default=14.0,
                    help="非宇宙品种 K 线缓存的保留天数,默认 14")
    ap.add_argument("--gzip-candles", action="store_true",
                    help="把 K 线缓存转成 .json.gz(仅当运行的 runner 支持 gz 读取)")
    ap.add_argument("--archive-max-days", type=float, default=365.0,
                    help="runtime/_archive 备份 zip 上限天数,默认 365")
    ap.add_argument("--budget-gb", type=float, default=35.0)
    ap.add_argument("--soft-gb", type=float, default=30.0)
    args = ap.parse_args(argv)

    if args.restore:
        restore_from_manifest(dry=not args.apply)
        return 0

    inv = inventory()
    gb_before = print_report(inv, args.budget_gb, args.soft_gb)
    if not (args.apply or args.dry_run):
        print("\n(只读模式;加 --apply 执行上述安全清理)")
        return 0 if gb_before <= args.budget_gb else 1

    print()
    print("=" * 74)
    print("执行" + ("(dry-run)" if not args.apply else ""))
    print("=" * 74)
    for a in apply_safe(dry=not args.apply, args=args):
        print("  " + a)
    if not args.apply:
        return 0

    gb_after = print_report(inventory(), args.budget_gb, args.soft_gb)
    print(f"本轮释放 {human(max(0.0, gb_before - gb_after) * (1 << 30))}")
    return 0 if gb_after <= args.budget_gb else 1


if __name__ == "__main__":
    raise SystemExit(main())
