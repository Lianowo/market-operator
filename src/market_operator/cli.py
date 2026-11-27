"""`mo` 命令行:自检 / 启停 / 暖机 / 对账 / 日志 / TG / 打包 / 迁移。

所有子命令都支持 `--json`,便于脚本与 Telegram 复用;人类输出保持一棵
"每行一个事实"的窄格式。CLI 只调用 `ops` / `paths` / `tg_commands`,
不 import `live_runner`(暖机是 `ops.warmup` 的沙盒导入)。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import sys
from types import MappingProxyType
from typing import Any

from . import __version__, ops, paths, tg_commands

LOG_TARGETS = ops.LOG_TARGETS


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="输出 JSON(脚本用)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mo", description=f"market-operator 运维台 v{__version__}"
    )
    ap.add_argument("--config", default=None, help="config/live.json 路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("bootstrap", help="一条命令:更新→布局→自检→启动→暖机→对账→TG")
    p.add_argument(
        "--layout",
        choices=("auto", "legacy", "portable"),
        default="auto",
        help="auto=全新主机自动 portable",
    )
    p.add_argument(
        "--update",
        action="store_true",
        help="先 git pull --ff-only + 安装依赖(拷贝部署自动跳过)",
    )
    p.add_argument("--no-install", action="store_true", help="--update 时不执行 pip install -e .")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--no-warmup", action="store_true")
    p.add_argument("--no-exchange", action="store_true")
    p.add_argument("--no-tg", action="store_true", help="不启动 Telegram 机器人")
    p.add_argument(
        "--no-tasks",
        action="store_true",
        help="不注册/刷新计划任务(默认自动注册:看护/存储/资金费)",
    )
    p.add_argument("--tg-wait", type=float, default=10.0)
    p.add_argument("--force", action="store_true", help="自检失败也继续")
    p.add_argument("--bars", type=int, default=300)
    _add_json(p)

    p = sub.add_parser("update", help="git 拉取代码并按需安装依赖")
    p.add_argument("--check", action="store_true", help="只 fetch 并报告落后提交数")
    p.add_argument("--no-install", action="store_true")
    p.add_argument("--timeout", type=float, default=300.0)
    _add_json(p)

    p = sub.add_parser("doctor", help="环境自检(默认不联网)")
    p.add_argument("--exchange", action="store_true", help="含公共行情+只读账户")
    p.add_argument("--require-live", action="store_true")
    p.add_argument("--min-free-gb", type=float, default=1.0)
    _add_json(p)

    for name, text in (
        ("start", "启动看护(由看护拉起 runner)"),
        ("stop", "停止看护、runner 与 TG 机器人"),
        ("restart", "重启整条实盘链"),
    ):
        p = sub.add_parser(name, help=text)
        if name == "start":
            p.add_argument("--wait", type=float, default=20.0)
        if name == "restart":
            p.add_argument("--warmup-first", action="store_true")
            p.add_argument("--no-tg", action="store_true", help="重启后不拉起 TG 机器人")
            p.add_argument("--tg-wait", type=float, default=10.0)
        _add_json(p)

    p = sub.add_parser("status", help="进程/心跳/持仓快照")
    _add_json(p)

    p = sub.add_parser("warmup", help="沙盒暖机(不写生产 state)")
    p.add_argument("--symbols", default="", help="逗号分隔;默认 symbols_core")
    p.add_argument("--tf", default="", help="逗号分隔;默认 strategy tf_list")
    p.add_argument("--bars", type=int, default=300)
    p.add_argument("--write-cache", action="store_true", help="runner 未运行时补 K 线缓存")
    p.add_argument("--no-account", action="store_true", help="跳过账户对账")
    _add_json(p)

    p = sub.add_parser("positions", help="本地账本 vs 交易所持仓")
    p.add_argument("--no-exchange", action="store_true")
    _add_json(p)

    p = sub.add_parser("logs", help="查看日志尾部")
    p.add_argument("target", nargs="?", default="live", choices=sorted(LOG_TARGETS))
    p.add_argument("-n", "--lines", type=int, default=80)
    p.add_argument("--list", action="store_true", help="列出所有日志文件")
    _add_json(p)

    p = sub.add_parser("tg-sync", help="注册/清理 Telegram 命令菜单")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-purge", action="store_true")
    _add_json(p)

    p = sub.add_parser("tg-run", help="启动 Telegram 长轮询机器人")
    p.add_argument("--once", action="store_true", help="只拉一轮(冒烟)")
    p.add_argument("--no-sync", action="store_true")
    _add_json(p)

    p = sub.add_parser("package", help="打包运行期产物(zip+sha256 清单)")
    p.add_argument("--data", action="store_true", help="含 data/")
    p.add_argument("--reports", action="store_true", help="含 reports/")
    p.add_argument("--backups", action="store_true", help="含 backups/")
    p.add_argument("-o", "--output", default=None)
    p.add_argument("--verify", default=None, metavar="ZIP", help="只校验已有归档")
    _add_json(p)

    p = sub.add_parser("migrate-data", help="旧目录 → var/ 复制校验后切换")
    p.add_argument("--move", action="store_true", help="删除已校验且非 git 的源文件")
    p.add_argument("--include-data", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    _add_json(p)

    p = sub.add_parser("tasks", help="注册/刷新计划任务(看护/存储/资金费)")
    _add_json(p)

    p = sub.add_parser("paths", help="打印目录布局与占用")
    _add_json(p)
    return ap


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------

_ICON: Mapping[str, str] = MappingProxyType({
    "ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP",
})


def _print_doctor(rep: Mapping[str, Any]) -> None:
    print(
        f"自检 v{__version__} · {rep.get('at_utc')} · "
        f"{rep.get('layout', {}).get('layout')} 布局"
    )
    for row in rep.get("checks") or ():
        line = f"[{_ICON.get(row['status'], row['status'])}] {row['name']}"
        if row.get("detail"):
            line += f": {row['detail']}"
        if row.get("hint") and row["status"] != "ok":
            line += f"  -> {row['hint']}"
        print(line)
    counts = rep.get("counts") or {}
    verdict = "通过" if rep.get("ok") else "未通过"
    print(
        f"结论:{verdict} · ok={counts.get('ok', 0)} "
        f"warn={counts.get('warn', 0)} fail={counts.get('fail', 0)}"
        f" skip={counts.get('skip', 0)}"
    )


def _print_status(snap: Mapping[str, Any], cfg: Mapping[str, Any]) -> None:
    runner, sup = snap["runner"], snap["supervisor"]
    age = runner.get("heartbeat_age_s")
    print(f"mode={snap.get('mode')} · 布局={snap.get('layout')} · {snap.get('at_utc')}")
    print(
        f"runner  pid={runner.get('pid')} alive={runner.get('alive')} "
        f"heartbeat={age if age is None else round(age, 1)}s "
        f"status={runner.get('status')}"
    )
    print(
        f"supervisor pid={sup.get('pid')} alive={sup.get('alive')} "
        f"restarts_left={sup.get('restarts_left')}"
    )
    tg = snap.get("tg_bot") or {}
    print(f"tg-bot  pid={tg.get('pid')} alive={tg.get('alive')}")
    eq = snap.get("equity")
    print(
        f"持仓={snap.get('positions')} 挂单={snap.get('open_intents')} "
        f"关注={snap.get('watches', 0)} 入场={snap.get('entry_orders', 0)} "
        f"止损={snap.get('stops')} 权益={eq}"
    )
    c = snap.get("counters") or {}
    print("计数器 " + " ".join(f"{k}={v}" for k, v in c.items()))
    br = snap.get("breaker") or {}
    print(f"熔断 tripped={br.get('tripped')} day={br.get('day')}")
    for row in ops.merged_position_rows(cfg):
        print(ops.format_position_line(row).replace("\n", " | "))


def _print_paths(
    rep: Mapping[str, Any],
    usage: Mapping[str, Any],
    legacy: Mapping[str, Any],
) -> None:
    print(f"home={rep['home']} layout={rep['layout']}")
    print(f"store={rep['store_root']}")
    print(
        f"config={rep['config']} exists={rep['config_exists']} "
        f"marker={rep['marker']} exists={rep['marker_exists']}"
    )
    for name, row in usage.items():
        flag = "" if row["exists"] else " (未创建)"
        print(
            f"{name:<8} {ops.human_bytes(row['bytes']):>10} "
            f"{row['files']:>7} files{flag}  {row['path']}"
        )
    live = [(k, v) for k, v in legacy.items() if v["files"]]
    if live and rep["layout"] == "legacy":
        print("旧目录仍有文件(迁移前保持 legacy):")
        for name, row in live:
            print(f"  {name:<8} {ops.human_bytes(row['bytes']):>10}  {row['path']}")


def _print_positions(rep: Mapping[str, Any]) -> None:
    print(
        f"持仓对账 {rep['at_utc']} · runner_alive={rep['runner_alive']} "
        f"heartbeat={rep.get('heartbeat_age_s')}s"
    )
    local = rep.get("local") or []
    for row in local:
        print(ops.format_position_line(row).replace("\n", " | "))
    bal = rep.get("balance") or {}
    if bal:
        print(
            f"账户 equity={bal.get('equity')} "
            f"available={bal.get('available')} "
            f"unrealized_pl={bal.get('unrealized_pl')}"
        )
    if rep.get("exchange") is not None:
        print(f"交易所持仓 {len(rep['exchange'])} 笔")
    for m in rep.get("mismatches") or ():
        print(f"DIFF {m}")
    for e in rep.get("errors") or ():
        print(f"ERROR {e}")
    if not local and not rep.get("errors"):
        print("本地账本无持仓")


def _print_warmup(rep: Mapping[str, Any]) -> None:
    print(f"暖机 {rep.get('at_utc')} ok={rep.get('ok')} 合约={rep.get('contracts', 0)}")
    for sym, rec in (rep.get("symbols") or {}).items():
        tfs = ", ".join(f"{tf}:{v.get('bars')}" for tf, v in (rec.get("tfs") or {}).items())
        cache_suffix = f" cache={rec['cache']}" if rec.get("cache") else ""
        print(f"{sym:<12} bars={rec.get('bars')} {tfs}{cache_suffix}")
    acc = rep.get("account") or {}
    if acc:
        print(
            f"账户 positions={acc.get('positions')} "
            f"equity={acc.get('equity')} available={acc.get('available')}"
        )
    for w in rep.get("warnings") or ():
        print(f"WARN {w}")
    for e in rep.get("errors") or ():
        print(f"ERROR {e}")
    for m in rep.get("mismatches") or ():
        print(f"DIFF {m}")


def _print_tasks(rep: Mapping[str, Any]) -> None:
    skipped = f" skipped={rep['skipped']}" if rep.get("skipped") else ""
    print(f"计划任务 ok={rep.get('ok')} supported={rep.get('supported')}{skipped}")
    for task in rep.get("tasks") or ():
        why = f" · {task.get('why')}" if task.get("why") else ""
        print(f"  {'OK  ' if task.get('ok') else 'FAIL'} {task.get('name')}{why}")


def _print_tg_sync_bot(bot: Mapping[str, Any]) -> None:
    print(f"@{bot.get('username') or '?'} roles={bot['roles']}")
    purged = bot.get("purged") or []
    if purged:
        total = sum(int(p.get("purged", 0)) for p in purged)
        print(f"  purge scopes={len(purged)} calls={total}")
        for p in purged:
            cur = p.get("current") or {}
            stale = {k: v for k, v in cur.items() if v}
            if not (p.get("errors") or p.get("skipped_languages") or stale):
                continue
            note = ",".join(p.get("skipped_languages") or [])
            line = f"    {p['label']:<22} purged={p['purged']}"
            if note:
                line += f" skipped={note}"
            if stale:
                line += f" current={stale}"
            if p.get("errors"):
                line += f" errors={len(p.get('errors') or [])}"
            print(line)
    for sc in bot.get("scopes") or ():
        extra = ""
        if sc.get("current") is not None:
            extra += f" current={sc['current']}"
        if sc.get("verified") is not None:
            extra += f" verified={len(sc['verified'])}"
        print(f"  {sc['label']:<22} {sc['role']:<6} commands={sc['commands']}{extra}")
        if sc.get("error"):
            print(f"    ERROR {sc['error']}")
    for label, residual in (bot.get("residual") or {}).items():
        print(f"    RESIDUAL {label}: {residual}")


# ---------------------------------------------------------------------------
# 布局决策(只由 bootstrap 显式触发)
# ---------------------------------------------------------------------------


def _looks_fresh_host() -> bool:
    """全新主机判定(仅在 bootstrap auto 时使用)。"""
    home = paths.app_home()
    if paths.layout_config_path().exists() or paths.layout_marker_path().exists():
        return False
    for rel in (
        "runtime/live/state.json",
        "runtime/live/heartbeat.json",
        "runtime/live/supervisor.json",
    ):
        if (home / rel).exists():
            return False
    return not any(row["files"] for row in paths.legacy_snapshot().values())


def _apply_layout(mode: str) -> dict[str, Any]:
    before = paths.layout_mode()
    action = "keep"
    if mode == "auto":
        if before == paths.LEGACY_LAYOUT and _looks_fresh_host():
            paths.activate_portable(reason="bootstrap-fresh")
            action = "activate-portable(fresh)"
        else:
            action = f"keep({before})"
    elif mode == "portable" and before != paths.PORTABLE_LAYOUT:
        paths.activate_portable(reason="bootstrap-explicit")
        action = "activate-portable(explicit)"
    elif mode == "legacy" and before != paths.LEGACY_LAYOUT:
        paths.activate_legacy(reason="bootstrap-explicit")
        action = "activate-legacy(explicit)"
    elif mode != "auto":
        action = f"keep({before})"
    paths.ensure_layout()
    return {
        "requested": mode,
        "before": before,
        "after": paths.layout_mode(),
        "action": action,
        "report": paths.layout_report(),
    }


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def _safe_secrets(cfg: Mapping[str, Any]) -> tuple[dict[str, str], str | None]:
    try:
        return ops.load_secrets(cfg), None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _print_update(rep: Mapping[str, Any]) -> None:
    if rep.get("skipped"):
        print(f"更新跳过:{'; '.join(rep.get('notes') or [])}")
        return
    print(
        f"更新 branch={rep.get('branch')} {rep.get('before') or '?'}"
        f" -> {rep.get('after') or '?'} changed={rep.get('changed')}"
        f" dirty={rep.get('dirty')}"
    )
    if rep.get("behind") is not None:
        print(f"落后上游 {rep['behind']} 个提交")
    for note in rep.get("notes") or ():
        print(f"NOTE {note}")
    for err in rep.get("errors") or ():
        print(f"ERROR {err}")


def _emit_bootstrap_step(as_json: bool, step: str, rep: Mapping[str, Any]) -> None:
    if as_json:
        return
    if step == "doctor":
        _print_doctor(rep)
    elif step == "start":
        why = f" why={rep.get('why')}" if rep.get("why") else ""
        print(
            f"启动 started={rep.get('started')} pid={rep.get('pid')} "
            f"alive={rep.get('alive')} heartbeat={rep.get('heartbeat')}{why}"
        )
    elif step == "tg":
        why = f" why={rep.get('why')}" if rep.get("why") else ""
        print(f"TG started={rep.get('started')} pid={rep.get('pid')} alive={rep.get('alive')}{why}")
    elif step == "warmup":
        _print_warmup(rep)
    elif step == "positions":
        _print_positions(rep)


def _bootstrap_update(
    args: argparse.Namespace,
    cfg: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """Run optional git pull step; return ``(report, should_abort)``."""
    if not getattr(args, "update", False):
        return None, False
    update_rep = ops.git_update(install=not getattr(args, "no_install", False))
    if not args.json:
        _print_update(update_rep)
    if not update_rep.get("ok") and not args.force:
        if args.json:
            print(json.dumps({"update": update_rep, "ok": False}, ensure_ascii=False, indent=2, default=str))
        else:
            print("代码更新失败:已停止(加 --force 可继续)。")
        return update_rep, True
    if update_rep.get("changed") and not args.no_start:
        running = ops.supervisor_pid(cfg) or ops.runner_pid(cfg)
        if running:
            stopped = ops.stop_all(cfg)
            if not args.json:
                print(f"检测到运行中进程,已停止以加载新代码 (all_stopped={stopped['all_stopped']})")
    return update_rep, False


def cmd_bootstrap(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    update_rep, abort = _bootstrap_update(args, cfg)
    if abort:
        return 1

    layout = _apply_layout(args.layout)
    if not args.json:
        print(
            f"布局 {layout['before']} -> {layout['after']} "
            f"[{layout['action']}] · store={layout['report']['store_root']}"
        )

    doc = ops.doctor(cfg, cfg_file=ops.config_path(args.config))
    result: dict[str, Any] = {
        "update": update_rep,
        "layout": layout,
        "doctor": doc,
        "tasks": None,
        "start": None,
        "tg": None,
        "warmup": None,
        "positions": None,
        "ok": True,
    }
    _emit_bootstrap_step(args.json, "doctor", doc)
    if not doc.get("ok") and not args.force:
        result["ok"] = False
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("自检未通过:已停止(加 --force 可继续)。")
        return 1

    if not getattr(args, "no_tasks", False):
        result["tasks"] = ops.register_tasks(cfg)
        if not args.json:
            _print_tasks(result["tasks"])

    if args.no_start:
        result["start"] = {"started": False, "skipped": True}
    else:
        result["start"] = ops.start_supervisor(cfg, cfg_file=ops.config_path(args.config))
    _emit_bootstrap_step(args.json, "start", result["start"] or {})
    if not result["start"].get("alive") and not result["start"].get("skipped"):
        result["ok"] = False

    secrets, secret_err = _safe_secrets(cfg)
    if not args.no_warmup:
        try:
            result["warmup"] = ops.warmup(cfg, bars=args.bars, check_account=not args.no_exchange)
        except Exception as exc:
            result["warmup"] = {"ok": False, "errors": [ops.redact(exc, secrets)]}
        _emit_bootstrap_step(args.json, "warmup", result["warmup"])
        if not result["warmup"].get("ok"):
            result["ok"] = False

    if not args.no_exchange:
        try:
            result["positions"] = ops.positions_report(cfg, exchange=True)
        except Exception as exc:
            result["positions"] = {
                "errors": [ops.redact(exc, secrets)],
                "local": [],
                "mismatches": [],
            }
        _emit_bootstrap_step(args.json, "positions", result["positions"])
        if result["positions"].get("errors"):
            result["ok"] = False

    if args.no_start:
        result["tg"] = {"skipped": True, "why": "--no-start"}
    elif args.no_tg:
        result["tg"] = {"skipped": True, "why": "--no-tg"}
    else:
        result["tg"] = ops.start_tg_bot(
            cfg,
            cfg_file=ops.config_path(args.config),
            wait_s=getattr(args, "tg_wait", 10.0),
        )
    _emit_bootstrap_step(args.json, "tg", result["tg"])
    if result["tg"].get("ok") is False:
        result["ok"] = False

    if secret_err and not args.json:
        print(f"提示:密钥未就绪({secret_err})")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print("完成:" + ("可以开始运营" if result["ok"] else "部分步骤失败,见上面 WARN/ERROR"))
    return 0 if result["ok"] else 1


def cmd_doctor(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    rep = ops.doctor(
        cfg,
        exchange=args.exchange,
        require_live=args.require_live,
        min_free_gb=args.min_free_gb,
        cfg_file=ops.config_path(args.config),
    )
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        _print_doctor(rep)
    return 0 if rep.get("ok") else 1


def cmd_start(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    rep = ops.start_supervisor(cfg, cfg_file=ops.config_path(args.config), wait_s=args.wait)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(
            f"started={rep.get('started')} pid={rep.get('pid')} "
            f"alive={rep.get('alive')} heartbeat={rep.get('heartbeat')}"
        )
        if rep.get("why"):
            print(f"why={rep['why']}")
    return 0 if (rep.get("started") or rep.get("why") == "已在运行") else 1


def cmd_stop(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    rep = ops.stop_all(cfg)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        for a in rep["actions"]:
            print(f"{a['target']:<12} pid={a['pid']} stopped={a['stopped']}")
        print(f"all_stopped={rep['all_stopped']}")
    return 0 if rep.get("all_stopped") else 1


def cmd_restart(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    rep = ops.restart_all(
        cfg,
        cfg_file=ops.config_path(args.config),
        warmup_first=args.warmup_first,
        include_tg=not args.no_tg,
        tg_wait_s=getattr(args, "tg_wait", 10.0),
    )
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"stop all_stopped={rep['stop']['all_stopped']}")
        if rep.get("warmup"):
            _print_warmup(rep["warmup"])
        st = rep["start"]
        print(
            f"start started={st.get('started')} pid={st.get('pid')} "
            f"alive={st.get('alive')} heartbeat={st.get('heartbeat')}"
        )
        tg = rep.get("tg") or {}
        why = f" why={tg.get('why')}" if tg.get("why") else ""
        print(f"tg started={tg.get('started')} pid={tg.get('pid')} alive={tg.get('alive')}{why}")
    tg_ok = (rep.get("tg") or {}).get("ok", True)
    return 0 if (rep["start"].get("alive") and tg_ok) else 1


def cmd_update(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    _ = cfg
    rep = ops.git_update(check=args.check, install=not args.no_install, timeout=args.timeout)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        _print_update(rep)
    return 0 if rep.get("ok") else 1


def cmd_status(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    snap = ops.status_snapshot(cfg)
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
    else:
        _print_status(snap, cfg)
    return 0 if snap["runner"]["alive"] else 1


def cmd_warmup(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    tfs = [s.strip() for s in args.tf.split(",") if s.strip()] or None
    rep = ops.warmup(
        cfg,
        symbols=symbols,
        timeframes=tfs,
        bars=args.bars,
        write_cache=args.write_cache,
        check_account=not args.no_account,
    )
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        _print_warmup(rep)
    return 0 if rep.get("ok") else 1


def cmd_positions(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    rep = ops.positions_report(cfg, exchange=not args.no_exchange)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        _print_positions(rep)
    return 1 if rep.get("errors") else 0


def cmd_logs(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    rows = ops.log_files()
    if args.list:
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                print(f"{row['mtime_utc']} {ops.human_bytes(row['bytes']):>10} {row['name']}")
        return 0
    name, label = LOG_TARGETS[args.target]
    path = paths.log_dir() / name
    if not path.exists():
        msg = f"日志不存在:{path}"
        if args.json:
            print(json.dumps({"ok": False, "error": msg, "files": rows}, ensure_ascii=False, indent=2))
        else:
            print(msg)
            for row in rows[:20]:
                print(f"  {row['name']}")
        return 1
    lines = ops.tail_lines(path, args.lines)
    secrets, _ = _safe_secrets(cfg)
    text = ops.redact("\n".join(lines), secrets)
    if args.json:
        print(
            json.dumps(
                {"ok": True, "path": str(path), "label": label, "lines": len(lines), "text": text},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"== {label} · {path} · 末 {len(lines)} 行 ==")
        print(text)
    return 0


def cmd_tg_sync(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    secrets, err = _safe_secrets(cfg)
    if err:
        print(f"ERROR 密钥未就绪:{err}")
        return 1
    rep = tg_commands.sync_commands(secrets, purge=not args.no_purge, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"tg-sync dry_run={rep['dry_run']} ok={rep['ok']}")
        for bot in rep.get("bots") or ():
            _print_tg_sync_bot(bot)
        for e in rep.get("errors") or ():
            print(f"ERROR {e}")
    return 0 if rep.get("ok") else 1


def cmd_tg_run(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    secrets, err = _safe_secrets(cfg)
    if err:
        print(f"ERROR 密钥未就绪:{err}")
        return 1
    rep = tg_commands.run_bots(cfg, secrets, once=args.once, sync=not args.no_sync)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"tg-bot ok={rep.get('ok')}")
        for bot in rep.get("bots") or ():
            print(f"{bot.get('token_key')} roles={bot.get('roles')}")
        for e in rep.get("errors") or ():
            print(f"ERROR {e}")
    return 0 if rep.get("ok") else 1


def cmd_package(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    if args.verify:
        rep = ops.verify_archive(args.verify)
    else:
        secrets, _ = _safe_secrets(cfg) if cfg else ({}, None)
        rep = ops.package_data(
            include_data=args.data,
            include_reports=args.reports,
            include_backups=args.backups,
            dest=args.output,
            secrets=secrets,
        )
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    elif args.verify:
        print(f"verify ok={rep.get('ok')} checked={rep.get('checked')}")
        for e in rep.get("errors") or ():
            print(f"ERROR {e}")
    else:
        print(f"archive={rep['archive']}")
        print(f"manifest={rep['manifest']}")
        print(
            f"files={rep['files']} skipped={rep['skipped']} "
            f"size={ops.human_bytes(rep['bytes'])} "
            f"categories={','.join(rep['categories'])}"
        )
    return 0 if rep.get("ok") else 1


def cmd_migrate(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    _ = cfg
    try:
        rep = paths.migrate_legacy(
            move=args.move,
            include_data=args.include_data,
            force=args.force,
            dry_run=args.dry_run,
        )
    except paths.LayoutError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR {exc}")
        return 1
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"target={rep['target']} categories={rep['categories']} "
            f"copied={rep['copied']} dry_run={rep['dry_run']}"
        )
        if rep.get("move"):
            print(
                f"deleted={rep.get('deleted', 0)} "
                f"tracked_skipped={rep.get('tracked_skipped', 0)}"
            )
        if rep.get("activated"):
            print("已激活 portable;下一步 `mo doctor` 确认路径。")
        for e in rep.get("errors") or ():
            print(f"ERROR {e.get('src')}: {e.get('why')}")
    return 0 if not rep.get("errors") else 1


def cmd_tasks(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    rep = ops.register_tasks(cfg)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        _print_tasks(rep)
    return 0 if (rep.get("ok") or not rep.get("supported")) else 1


def cmd_paths(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    _ = cfg
    rep = paths.layout_report()
    usage = paths.category_usage()
    legacy = paths.legacy_snapshot()
    if args.json:
        print(
            json.dumps(
                {"layout": rep, "usage": usage, "legacy": legacy},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        _print_paths(rep, usage, legacy)
    return 0


HANDLERS: Mapping[str, Callable[[argparse.Namespace, dict[str, Any]], int]] = MappingProxyType({
    "bootstrap": cmd_bootstrap,
    "update": cmd_update,
    "doctor": cmd_doctor,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "warmup": cmd_warmup,
    "positions": cmd_positions,
    "logs": cmd_logs,
    "tg-sync": cmd_tg_sync,
    "tg-run": cmd_tg_run,
    "package": cmd_package,
    "migrate-data": cmd_migrate,
    "tasks": cmd_tasks,
    "paths": cmd_paths,
})

_NO_CONFIG_CMDS = frozenset({"paths", "package", "migrate-data", "logs", "update"})
_RUNTIME_CMDS = frozenset({
    "start", "restart", "stop", "status", "positions", "warmup",
    "logs", "tg-sync", "tg-run", "package", "doctor",
})

_soften_stdio = paths.soften_stdio


def main(argv: Sequence[str] | None = None) -> int:
    _soften_stdio()
    args = build_parser().parse_args(argv)
    cfg: dict[str, Any] = {}
    cfg_err: Exception | None = None
    try:
        cfg = ops.load_config(args.config)
    except Exception as exc:
        cfg_err = exc
    if cfg_err is not None and args.cmd not in _NO_CONFIG_CMDS:
        print(f"ERROR 配置不可用:{cfg_err}", file=sys.stderr)
        return 2
    if args.cmd in _RUNTIME_CMDS:
        try:
            paths.ensure_layout()
        except OSError as exc:
            print(f"ERROR 运行期目录不可创建({paths.layout_mode()}):{exc}", file=sys.stderr)
            return 2
    fn = HANDLERS[args.cmd]
    try:
        return int(fn(args, cfg))
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR {ops.redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":                        # pragma: no cover
    sys.exit(main())

