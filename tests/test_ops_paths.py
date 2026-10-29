"""运维层(`mo` CLI / `paths` / `ops` / `tg_commands`)离线测试。

这里钉的都是"换机器 / 换布局就会静默出事"的口径:

① 布局优先级:`MARKET_OPERATOR_LAYOUT` > `MARKET_OPERATOR_DATA_ROOT` >
   `config/paths.json` > `var/.mo-layout.json` 标记 > legacy 默认;
② `resolve()` 只改写**运行期类别**,`config/`、`src/` 永远锚定仓库根;
   写路径严格(`resolve`)、读路径可回退(`resolve_existing`)—— 迁移未完成时
   runner 不能把 state 劈成两半,但巡检要读得到旧文件;
③ runner / 看护 / CLI 必须落到**同一份** state 与心跳(修前各处硬编码
   `ROOT/"runtime"`,portable 下等于各读各的);
④ `activate_portable` ↔ `activate_legacy` 可逆,标记文件随写随删;
⑤ `migrate_legacy` 先复制 + 逐文件 sha256,全部通过才写激活配置;
⑥ 两套 Telegram 菜单各自无重名,运营命令不得漏进公开 Bot;
⑦ 心跳里 `positions` 是**数量**、`positions_detail` 才是逐笔(修前
   `_pos_rows` 直接迭代 int,一有仓位 `mo positions` 就 TypeError);
⑧ `ensure_layout()` 幂等,且补齐 runner 依赖的 `live/candles`、`live/paper_check`。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator import cli, ops, paths, tg_commands        # noqa: E402

_ENV_KEYS = ("MARKET_OPERATOR_LAYOUT", "MARKET_OPERATOR_DATA_ROOT",
             "MO_DATA_ROOT", "MO_HOME")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """把 `app_home()` 指向临时目录,并清掉外层可能残留的布局开关。"""
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return paths.app_home()


# ---------------------------------------------------------------------------
# ① 布局优先级
# ---------------------------------------------------------------------------


def test_layout_precedence_marker_config_data_root_env(home, monkeypatch):
    assert paths.layout_mode() == paths.LEGACY_LAYOUT          # 默认

    marker = home / "var" / ".mo-layout.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    assert paths.layout_mode() == paths.PORTABLE_LAYOUT        # 标记

    cfg = home / "config" / "paths.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"layout": "legacy"}), encoding="utf-8")
    assert paths.layout_mode() == paths.LEGACY_LAYOUT          # 配置压过标记

    monkeypatch.setenv(paths.DATA_ROOT_ENV, "var")
    assert paths.layout_mode() == paths.PORTABLE_LAYOUT        # 数据根压过配置

    monkeypatch.setenv(paths.LAYOUT_ENV, "legacy")
    assert paths.layout_mode() == paths.LEGACY_LAYOUT          # env 最高


def test_storage_root_only_implies_portable(home):
    cfg = home / "config" / "paths.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"storage_root": "srv/mo"}), encoding="utf-8")
    assert paths.layout_mode() == paths.PORTABLE_LAYOUT
    assert paths.store_root() == (home / "srv" / "mo").resolve()


# ---------------------------------------------------------------------------
# ② resolve / resolve_existing
# ---------------------------------------------------------------------------


def test_resolve_rewrites_runtime_categories_only(home):
    assert paths.resolve("runtime/live/state.json") == \
        home / "runtime" / "live" / "state.json"
    assert paths.resolve("logs/live_stdout.log") == \
        home / "logs" / "live_stdout.log"
    # 代码/配置路径永远锚定仓库根,不能被当成运行期类别改写
    assert paths.resolve("config/live.json") == home / "config" / "live.json"
    assert paths.resolve("scripts/live_runner.py") == \
        home / "scripts" / "live_runner.py"
    # backups/exports/cache/tmp 即使在 legacy 也放 var/,不污染仓库
    assert paths.resolve("backups/x.zip") == home / "var" / "backups" / "x.zip"
    # 绝对路径原样返回(用户显式指定优先)
    absolute = home / "abs" / "state.json"
    assert paths.resolve(absolute) == absolute


def test_resolve_existing_falls_back_before_migration(home):
    legacy = home / "logs" / "old.log"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("x", encoding="utf-8")
    paths.activate_portable(storage_root="var", reason="test")

    assert paths.resolve("logs/old.log") == home / "var" / "logs" / "old.log"
    assert paths.resolve_existing("logs/old.log") == legacy
    assert paths.resolve_existing("logs/new.log") == \
        home / "var" / "logs" / "new.log"


# ---------------------------------------------------------------------------
# ③ 同一份 state/心跳:ops.live_paths 与 runner 侧 resolve() 同源
# ---------------------------------------------------------------------------


def test_live_paths_single_source_across_layouts(home):
    lp = ops.live_paths({})
    assert lp["state"] == home / "runtime" / "live" / "state.json"
    assert lp["heartbeat"] == home / "runtime" / "live" / "heartbeat.json"
    assert lp["supervisor_pid"] == \
        home / "runtime" / "live" / "supervisor.pid"
    assert lp["supervisor_jsonl"] == home / "logs" / "supervisor.jsonl"
    # runner / live_supervisor 用的是同一套解析函数
    assert paths.resolve("runtime/live/state.json") == lp["state"]
    assert paths.resolve("logs") == lp["journal_dir"]

    paths.activate_portable(storage_root="var", reason="test")
    lp = ops.live_paths({})
    assert lp["state"] == home / "var" / "runtime" / "live" / "state.json"
    assert lp["heartbeat"] == \
        home / "var" / "runtime" / "live" / "heartbeat.json"
    assert lp["tg_pid"] == home / "var" / "runtime" / "live" / "tg_bot.pid"
    assert lp["supervisor_jsonl"] == home / "var" / "logs" / "supervisor.jsonl"
    assert paths.resolve("runtime/live/state.json") == lp["state"]


def test_live_paths_honours_explicit_config_paths(home):
    lp = ops.live_paths({"loop": {"state_path": "runtime/custom/state.json",
                                  "journal_dir": "logs/j"}})
    assert lp["state"] == home / "runtime" / "custom" / "state.json"
    assert lp["heartbeat"] == home / "runtime" / "custom" / "heartbeat.json"
    assert lp["candles"] == home / "runtime" / "custom" / "candles"
    assert lp["journal_dir"] == home / "logs" / "j"


# ---------------------------------------------------------------------------
# ④ 激活可逆
# ---------------------------------------------------------------------------


def test_activate_portable_then_legacy_roundtrip(home):
    paths.activate_portable(storage_root="var", reason="test")
    assert paths.is_portable()
    assert paths.layout_marker_path().exists()
    cfg = json.loads(paths.layout_config_path().read_text(encoding="utf-8"))
    assert cfg["layout"] == paths.PORTABLE_LAYOUT
    assert cfg["reason"] == "test"

    paths.activate_legacy(reason="rollback")
    assert not paths.is_portable()
    assert not paths.layout_marker_path().exists()      # 标记必须一并清掉
    cfg = json.loads(paths.layout_config_path().read_text(encoding="utf-8"))
    assert cfg["layout"] == paths.LEGACY_LAYOUT


# ---------------------------------------------------------------------------
# ⑤ 迁移:先校验再激活
# ---------------------------------------------------------------------------


def test_migrate_legacy_dry_run_writes_nothing(home):
    (home / "logs").mkdir()
    (home / "logs" / "a.log").write_text("hello", encoding="utf-8")

    res = paths.migrate_legacy(dry_run=True)
    assert res["copied"] == 1
    assert not res.get("activated")
    assert not (home / "var" / "logs" / "a.log").exists()
    assert paths.layout_mode() == paths.LEGACY_LAYOUT


def test_migrate_legacy_verifies_then_activates(home):
    (home / "logs").mkdir()
    (home / "logs" / "a.log").write_text("hello", encoding="utf-8")
    (home / "runtime" / "live").mkdir(parents=True)
    (home / "runtime" / "live" / "state.json").write_text(
        '{"positions": {}}', encoding="utf-8")

    res = paths.migrate_legacy()
    assert res["errors"] == []
    assert res["copied"] == 2
    assert res["activated"] is True
    assert (home / "var" / "logs" / "a.log").read_text(encoding="utf-8") == \
        "hello"
    assert paths.layout_mode() == paths.PORTABLE_LAYOUT

    manifest = json.loads(
        (home / "var" / paths.MIGRATION_NAME).read_text(encoding="utf-8"))
    assert {Path(f["src"]).name for f in manifest["files"]} == \
        {"a.log", "state.json"}
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])

    with pytest.raises(paths.LayoutError):          # 已 portable,不重复迁移
        paths.migrate_legacy()


def test_migrate_legacy_refuses_fresh_heartbeat(home):
    beat = home / "runtime" / "live" / "heartbeat.json"
    beat.parent.mkdir(parents=True)
    beat.write_text(json.dumps({"loop_ts_ms": int(time.time() * 1000)}),
                    encoding="utf-8")
    with pytest.raises(paths.LayoutError):
        paths.migrate_legacy()
    assert not paths.is_portable()                  # 拒绝期间不得落激活配置
    # 预演不落字节,所以必须先看到清单再决定停机
    dry = paths.migrate_legacy(dry_run=True)
    assert dry["dry_run"] is True and not dry.get("activated")


# ---------------------------------------------------------------------------
# ⑥ Telegram 菜单 / ⑦ 位置行 / ⑧ 目录创建
# ---------------------------------------------------------------------------


def test_tg_menus_unique_and_ops_only_commands_stay_out_of_public():
    for role, specs in tg_commands.SPECS.items():
        tg_commands.validate_specs(specs)           # 重名/非法字符会抛
        names = [s.name for s in specs]
        assert len(names) == len(set(names)), role
    public = {s.name for s in tg_commands.PUBLIC_SPECS}
    ops_names = {s.name for s in tg_commands.OPS_SPECS}
    assert {"status", "positions", "doctor", "logs"} <= ops_names
    assert not ({"status", "positions", "doctor", "logs"} & public)
    assert [row["command"] for row in tg_commands.menu_payload("ops")][:2] == \
        ["start", "help"]


def test_tg_sync_languages_are_the_ones_telegram_accepts():
    """`zh-hans` 之类 Telegram 直接 400 —— 留在列表里等于每次 sync 白打一圈 API。

    2026-10-25 实测:`getMyCommands` / `deleteMyCommands` 带 `zh-hans`、
    `zh-hant`、`zh-tw` 全部 `HTTP 400 invalid language code`,只有空串与 `zh`
    能用;`zh-hans` 因此也从没被写进去过(`setMyCommands` 同样会被拒),
    删掉它不会漏清任何旧菜单。
    """
    langs = set(tg_commands.SYNC_LANGUAGES)
    assert "" in langs and "zh" in langs
    assert not (langs & {"zh-hans", "zh-hant", "zh-tw"})


def test_positions_rows_read_detail_not_count(monkeypatch):
    doc = {"positions": 2,
           "positions_detail": [{"symbol": "BTCUSDT", "direction": -1,
                                 "qty": 0.5, "entry": 100.0},
                                {"symbol": "ETHUSDT", "direction": 1,
                                 "qty": 1.0, "entry": 2.0}]}
    monkeypatch.setattr(tg_commands.ops, "read_heartbeat", lambda cfg: doc)
    rows = tg_commands._pos_rows({})
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT"]

    # 精简/旧心跳只有数量:不能迭代 int,也不能凭空造行 -> 回退本地账本
    monkeypatch.setattr(tg_commands.ops, "read_heartbeat",
                        lambda cfg: {"positions": 2})
    monkeypatch.setattr(tg_commands.ops, "local_positions",
                        lambda cfg: [{"symbol": "SOLUSDT"}])
    assert tg_commands._pos_rows({}) == [{"symbol": "SOLUSDT"}]


def test_ensure_layout_idempotent_and_adds_runner_subdirs(home):
    out = paths.ensure_layout()
    assert set(out) == set(paths.CATEGORIES)
    assert (home / "runtime" / "live" / "candles").is_dir()
    assert (home / "runtime" / "live" / "paper_check").is_dir()
    assert paths.ensure_layout() == out


# ---------------------------------------------------------------------------
# CLI 契约:每个子命令都能被解析,名字不重复
# ---------------------------------------------------------------------------


def test_mo_parser_accepts_every_subcommand():
    ap = cli.build_parser()
    subs = [a for a in ap._actions
            if isinstance(a, argparse._SubParsersAction)][0]
    assert len(subs.choices) == len(set(subs.choices))

    cases = [
        ["doctor", "--json"],
        ["doctor", "--exchange", "--require-live", "--min-free-gb", "2"],
        ["start", "--wait", "1"],
        ["stop"],
        ["restart", "--warmup-first", "--no-tg"],
        ["update", "--check", "--no-install"],
        ["status"],
        ["warmup", "--symbols", "BTCUSDT", "--tf", "5m", "--bars", "10"],
        ["positions", "--no-exchange"],
        ["logs", "--list"],
        ["logs", "err", "-n", "5"],
        ["tg-sync", "--dry-run"],
        ["tg-run", "--once"],
        ["package", "--verify", "x.zip"],
        ["migrate-data", "--dry-run"],
        ["paths"],
        ["bootstrap", "--layout", "portable", "--no-start", "--bars", "5"],
    ]
    for argv in cases:
        ns = ap.parse_args(argv)
        assert ns.cmd == argv[0], argv


def test_status_snapshot_shows_layout_and_same_files(home):
    lp = ops.live_paths({})
    lp["state"].parent.mkdir(parents=True)
    lp["state"].write_text(json.dumps({"positions": {"BTCUSDT": {"qty": 1}},
                                       "counters": {"signals": 3}}),
                           encoding="utf-8")
    lp["heartbeat"].write_text(
        json.dumps({"schema": 1, "pid": os.getpid(), "status": "ok",
                    "mode": "live", "ts_ms": int(time.time() * 1000),
                    "loop_ts_ms": int(time.time() * 1000)}),
        encoding="utf-8")

    snap = ops.status_snapshot({})
    assert snap["layout"] == paths.LEGACY_LAYOUT
    assert snap["paths"]["state"] == str(lp["state"])
    assert snap["runner"]["alive"] is True
    assert snap["runner"]["heartbeat_age_s"] < 60.0
    assert snap["positions"] == 1
    assert snap["counters"]["signals"] == 3


def test_status_snapshot_reports_tg_bot_pid(home):
    """TG 机器人不在看护监管内、`restart` 也不拉它:不单独报出来只能靠人猜。

    2026-10-25 现场就是"命令没回、日志里却全是绿的" —— 进程根本没起。
    """
    lp = ops.live_paths({})
    lp["tg_pid"].parent.mkdir(parents=True, exist_ok=True)
    assert ops.status_snapshot({})["tg_bot"] == {"pid": None, "alive": False}

    lp["tg_pid"].write_text(str(os.getpid()), encoding="utf-8")   # 一定活着
    assert ops.status_snapshot({})["tg_bot"] == {"pid": os.getpid(),
                                                 "alive": True}

    lp["tg_pid"].write_text("999999", encoding="utf-8")           # 死 pid 不算活
    assert ops.status_snapshot({})["tg_bot"] == {"pid": None, "alive": False}


def test_status_text_prints_tg_bot_line(home, capsys):
    lp = ops.live_paths({})
    lp["tg_pid"].parent.mkdir(parents=True, exist_ok=True)
    lp["tg_pid"].write_text(str(os.getpid()), encoding="utf-8")
    cli._print_status(ops.status_snapshot({}), {})
    out = capsys.readouterr().out
    assert f"tg-bot  pid={os.getpid()} alive=True" in out


@pytest.mark.skipif(os.name != "nt", reason="探针自匹配是 Windows 的特有形态")
def test_find_pids_ignores_its_own_probe():
    """`find_pids` 的探针命令行里就含 marker,不过滤会把探针当目标。

    实测 `mo status` 因此报出 `supervisor pid=12260 alive=True`,而真看护是 19052
    (12260 是探针那次 powershell 自己的 pid)。
    """
    probe = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Start-Sleep -Seconds 8; # live_supervisor.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)                      # 等它起来,确保 cmdline 可读
        assert probe.pid not in ops.find_pids("live_supervisor.py")
    finally:
        probe.kill()
        probe.wait(timeout=10)


def test_supervisor_pid_ignores_spawned_runner(home, monkeypatch):
    """`supervisor.json` 的 `spawned_pid` 是 runner 子进程,不能当成看护 pid。

    修前 `mo status` 把 runner 显示成看护;`mo stop` 于是只杀 runner(看护下一轮
    又拉起来,看起来「停不掉」);`mo doctor` 在「看护已死、runner 还在」时误报健康。
    没有 `supervisor.pid` 的旧版看护改为按进程名兜底。
    """
    lp = ops.live_paths({})
    lp["supervisor_state"].parent.mkdir(parents=True)
    lp["supervisor_state"].write_text(
        json.dumps({"spawned_pid": os.getpid()}), encoding="utf-8")

    monkeypatch.setattr(ops, "find_pids", lambda marker, **kw: [])
    assert ops.supervisor_pid({}) is None

    monkeypatch.setattr(ops, "find_pids", lambda marker, **kw: [4242])
    assert ops.supervisor_pid({}) == 4242

    # pid 文件优先且必须存活
    monkeypatch.setattr(ops, "find_pids", lambda marker, **kw: [4242])
    lp["supervisor_pid"].write_text(str(os.getpid()), encoding="utf-8")
    assert ops.supervisor_pid({}) == os.getpid()


def test_cli_status_survives_gbk_console(home, monkeypatch):
    """cp936 控制台(Windows 默认、未设 `PYTHONUTF8`)遇 emoji 会 `UnicodeEncodeError`。

    修前它被 `main()` 兜底成一行 `ERROR 'gbk' codec ...` 并退出 1 —— 一个箭头
    就让 `mo status` 整条失败。`_soften_stdio()` 只放宽 errors:中文照旧,
    emoji 退化成 `?`,人要看的事实照常打完。
    """
    lp = ops.live_paths({})
    lp["heartbeat"].parent.mkdir(parents=True)
    now_ms = int(time.time() * 1000)
    lp["heartbeat"].write_text(json.dumps({
        "schema": 1, "pid": os.getpid(), "status": "ok", "mode": "live",
        "ts_ms": now_ms, "loop_ts_ms": now_ms, "positions": 1, "stops": 1,
        "equity": 100.0,
        "positions_detail": [{"symbol": "BTCUSDT", "direction": 1, "qty": 1.0,
                              "invalid": 90.0, "mark": 100.0, "tf": "4h",
                              "event_id": "E1", "stop": 90.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(ops, "load_config", lambda path=None: {})

    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", out)
    rc = cli.main(["status"])
    out.flush()
    text = buf.getvalue().decode("gbk")
    assert rc == 0, text
    assert "BTCUSDT" in text                 # 逐笔行照常打印
    assert "?" in text                       # emoji 降级,不炸整条命令



def test_runtime_commands_create_dirs_without_bootstrap(home, capsys):
    """全新主机上没有 bootstrap 也要能用:运行期命令入口自动建目录。"""
    assert not (home / "runtime").exists()
    rc = cli.main(["logs", "--list"])
    out = capsys.readouterr().out
    assert rc == 0, out
    for name in ("data", "logs", "runtime", "reports"):
        assert (home / name).is_dir(), name
    assert (home / "runtime" / "live").is_dir()


def test_start_tg_bot_idempotent_and_skips_without_token(home, monkeypatch):
    lp = ops.live_paths({})
    lp["tg_pid"].parent.mkdir(parents=True, exist_ok=True)
    lp["tg_pid"].write_text(str(os.getpid()), encoding="utf-8")
    rep = ops.start_tg_bot({}, wait_s=0)
    assert rep == {"ok": True, "started": False, "pid": os.getpid(),
                   "alive": True, "why": "已在运行"}

    lp["tg_pid"].unlink()
    monkeypatch.setattr(ops, "load_telegram_secrets", lambda cfg: {})
    rep = ops.start_tg_bot({}, wait_s=0)
    assert rep["ok"] is True and rep["skipped"] is True
    assert rep["started"] is False and "token" in rep["why"]

    def _boom(cfg):
        raise RuntimeError("secrets.env 不可读")
    monkeypatch.setattr(ops, "load_telegram_secrets", _boom)
    rep = ops.start_tg_bot({}, wait_s=0)
    assert rep["ok"] is False and rep["skipped"] is True
    assert "RuntimeError" in rep["why"]        # 不冒泡,但也不假装成功


def test_start_tg_bot_spawns_with_cwd_and_log_files(home, monkeypatch):
    """拉起机器人:入口缺失时退回 `-m market_operator`,日志落项目 logs/。"""
    seen: dict = {}

    def _spawn(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return 4321

    monkeypatch.setattr(ops, "load_telegram_secrets",
                        lambda cfg: {"TELEGRAM_BOT_TOKEN": "123:abc"})
    monkeypatch.setattr(ops.hb, "spawn_detached", _spawn)
    monkeypatch.setattr(ops, "pid_alive", lambda pid: True)
    rep = ops.start_tg_bot({}, wait_s=0)
    assert rep["ok"] is True and rep["started"] is True
    assert rep["pid"] == 4321
    assert seen["argv"][1:3] == ["-m", "market_operator"]
    assert seen["argv"][-1] == "tg-run"
    assert "--no-sync" not in seen["argv"]      # 启动即同步菜单
    assert seen["kw"]["cwd"] == paths.app_home()
    assert seen["kw"]["stdout_path"].parent == paths.log_dir()


def test_restart_all_restarts_tg_bot(home, monkeypatch):
    """`stop_all` 会停 TG;旧 `restart_all` 只拉看护 ⇒ 命令区永久静默。"""
    seen: dict = {}
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)
    monkeypatch.setattr(ops, "stop_all",
                        lambda cfg, include_tg=True: seen.update(
                            stop_tg=include_tg) or {"actions": [],
                                                    "all_stopped": True})
    monkeypatch.setattr(ops, "start_supervisor",
                        lambda cfg, **kw: {"started": True, "pid": 1,
                                           "alive": True, "heartbeat": True})

    def _start_tg(cfg, **kw):
        seen["tg_wait"] = kw.get("wait_s")
        return {"ok": True, "started": True, "pid": 2, "alive": True}
    monkeypatch.setattr(ops, "start_tg_bot", _start_tg)

    rep = ops.restart_all({}, include_tg=True, tg_wait_s=3)
    assert seen == {"stop_tg": True, "tg_wait": 3}
    assert rep["tg"]["alive"] is True

    rep = ops.restart_all({}, include_tg=False)
    # 显式跳过:报告里留痕(而不是悄悄没有 tg 段),且绝不调用 start_tg_bot
    assert rep["tg"]["skipped"] is True and rep["tg"]["why"] == "--no-tg"
    assert seen == {"stop_tg": False, "tg_wait": 3}


def test_invalidate_stale_heartbeat_removes_dead_runner_beat(home, monkeypatch):
    """重启前必须作废「已死 runner 的心跳」,否则看护白等满 stale_seconds。

    `mo restart` 先杀 runner 再启看护:心跳文件是「刚写过、但没有进程」的假活
    证据 ⇒ 看护判 `late` 而不是 `missing`,要等 300s 才拉起 runner,期间无人
    管理持仓(生产实测每次重启都有 ~5 分钟 pid=null)。
    """
    lp = ops.live_paths({})
    lp["heartbeat"].parent.mkdir(parents=True, exist_ok=True)
    lp["heartbeat"].write_text(json.dumps({
        "pid": 999999, "ts_ms": int(time.time() * 1000), "status": "ok"}))
    monkeypatch.setattr(ops, "pid_alive", lambda pid: False)

    rep = ops._invalidate_stale_heartbeat({})
    assert rep["removed"] is True
    assert not lp["heartbeat"].exists()


def test_invalidate_stale_heartbeat_keeps_live_runner_beat(home, monkeypatch):
    """runner 仍存活时必须保留心跳 —— 否则新看护会再拉起一个 runner(双开下单)。"""
    lp = ops.live_paths({})
    lp["heartbeat"].parent.mkdir(parents=True, exist_ok=True)
    lp["heartbeat"].write_text(json.dumps({
        "pid": 4242, "ts_ms": int(time.time() * 1000), "status": "ok"}))
    monkeypatch.setattr(ops, "pid_alive", lambda pid: True)

    rep = ops._invalidate_stale_heartbeat({})
    assert rep["removed"] is False
    assert "仍存活" in rep["why"]
    assert lp["heartbeat"].exists()


def test_start_supervisor_resets_stale_heartbeat_and_reports_it(home, monkeypatch):
    """`start_supervisor` 拉起看护前作废死心跳,并在报告里留痕。"""
    lp = ops.live_paths({})
    lp["heartbeat"].parent.mkdir(parents=True, exist_ok=True)
    lp["heartbeat"].write_text(json.dumps({
        "pid": 777777, "ts_ms": int(time.time() * 1000), "status": "ok"}))
    monkeypatch.setattr(ops, "supervisor_pid", lambda cfg: None)
    monkeypatch.setattr(ops, "pid_alive", lambda pid: False)
    monkeypatch.setattr(ops.hb, "spawn_detached", lambda argv, **kw: 5150)

    rep = ops.start_supervisor({}, wait_s=0)
    assert rep["started"] is True and rep["pid"] == 5150
    assert rep["heartbeat_reset"]["removed"] is True
    assert not lp["heartbeat"].exists()


def test_local_orders_lists_only_armed_intents(home):
    """`open_intents` 是历史表(filled/expired 都留着),面板只该显示还挂着的。"""
    lp = ops.live_paths({})
    lp["state"].parent.mkdir(parents=True)
    lp["state"].write_text(json.dumps({
        "open_intents": {
            "o1": {"oid": "o1", "symbol": "ETHUSDT", "role": "entry",
                   "kind": "limit", "direction": 1, "qty": 2,
                   "limit_price": 3000.0, "expiry_ts": 1_700_000_000_000,
                   "status": "armed", "exchange_order_id": "x1"},
            "o2": {"oid": "o2", "symbol": "BTCUSDT", "role": "exit",
                   "kind": "limit", "direction": -1, "qty": 0.1,
                   "limit_price": 60000.0, "expiry_ts": 1_700_000_000_000,
                   "status": "filled", "exchange_order_id": "x2"},
        },
        "stops": {"BTCUSDT": {"trigger": 61000.0, "qty": 0.1,
                              "order_id": "s1"}},
    }), encoding="utf-8")

    rep = ops.local_orders({})
    assert [r["symbol"] for r in rep["intents"]] == ["ETHUSDT"]
    assert rep["stops"] == [{"symbol": "BTCUSDT", "trigger": 61000.0,
                             "qty": 0.1, "order_id": "s1"}]


def test_tg_orders_command_is_wired_into_ops_menu(monkeypatch):
    names = {s.name for s in tg_commands.OPS_SPECS}
    assert "orders" in names and "orders" not in {
        s.name for s in tg_commands.PUBLIC_SPECS}
    assert tg_commands.HANDLERS["ops"]["orders"] is tg_commands.cmd_orders

    monkeypatch.setattr(tg_commands.ops, "local_orders", lambda cfg: {
        "at_utc": "2026-10-25T00:00:00Z",
        "intents": [{"oid": "o1", "symbol": "ETHUSDT", "role": "entry",
                     "kind": "limit", "direction": 1, "qty": 2,
                     "limit_price": 3000.0, "expiry_ms": 1_700_000_000_000,
                     "exchange_order_id": "1234567890abcdef"}],
        "stops": [{"symbol": "ETHUSDT", "trigger": 2900.0, "qty": 2}],
    })
    ctx = tg_commands.CommandContext(role="ops", chat_id="1", cfg={},
                                     secrets={}, api=None)      # type: ignore[arg-type]
    text = tg_commands.cmd_orders(ctx, [])
    assert "在挂委托" in text and "ETHUSDT" in text
    assert "入场限价" in text and "2,900" in text


# ---------------------------------------------------------------------------
# TG 菜单同步:全局 scope 必须清空 / 白名单必须真的装载
# ---------------------------------------------------------------------------


class _FakeTgAPI:
    """只实现 sync 用到的 4 个方法;`protect` 模拟 Telegram 删不掉的 scope。"""

    def __init__(self, *, protect=(), stale=None):
        self.protect = set(protect)
        self.store = dict(stale or {})
        self.deleted: list[tuple] = []
        self.set_scopes: list[str] = []

    @staticmethod
    def _key(scope: dict, lang: str) -> tuple:
        return (str(scope.get("type")), str(scope.get("chat_id") or ""),
                str(lang))

    def get_me(self) -> dict:
        return {"username": "bot"}

    def get_commands(self, scope: dict, language: str = "") -> list[dict]:
        return list(self.store.get(self._key(scope, language)) or [])

    def delete_commands(self, scope: dict, language: str = "") -> None:
        if str(scope.get("type")) in self.protect:
            return
        self.deleted.append(self._key(scope, language))
        self.store[self._key(scope, language)] = []

    def set_commands(self, commands: list[dict], scope: dict,
                     language: str = "") -> None:
        self.set_scopes.append(str(scope.get("type")))
        self.store[self._key(scope, language)] = list(commands)


_TWO_BOTS = {"TELEGRAM_BOT_TOKEN": "PUB", "TELEGRAM_CHAT_ID": "111",
             "TELEGRAM_TRADE_BOT_TOKEN": "OPS",
             "TELEGRAM_TRADE_CHAT_ID": "222"}


def test_tg_sync_purges_every_scope_and_language(monkeypatch):
    """只清 `default` 时,DM/群/群管 scope 的旧菜单仍会弹出来 —— 必须逐个清。"""
    api = _FakeTgAPI()
    monkeypatch.setattr(tg_commands, "_api_for",
                        lambda token, secrets: api)
    rep = tg_commands.sync_commands(_TWO_BOTS)
    assert rep["ok"] is True and rep["errors"] == []

    entry = rep["bots"][0]
    labels = {row["label"] for row in entry["purged"]}
    assert {"default", "all_private_chats", "all_group_chats",
            "all_chat_administrators", "chat:111", "chat_admin:111"} <= labels
    # 两个 token 都各清一遍:scope 数 × 语言数 × 机器人数
    assert len(api.deleted) == (
        sum(len(b["purged"]) for b in rep["bots"])
        * len(tg_commands.SYNC_LANGUAGES))

    assert ("all_group_chats", "", "zh") in api.deleted

    # 全局 scope 只清不写;命令只写进 default 与已知 chat
    assert "all_group_chats" not in api.set_scopes
    assert "all_chat_administrators" not in api.set_scopes
    assert "default" in api.set_scopes and "chat" in api.set_scopes
    assert entry["residual"] == {}


def test_tg_sync_dry_run_lists_global_scopes_without_writing(monkeypatch):
    """干跑必须能看见"客户端缓存里到底还有哪些全局菜单",且一个写请求都不发。"""
    stale = {("all_group_chats", "", "zh"): [{"command": "status"}]}
    api = _FakeTgAPI(stale=stale)
    monkeypatch.setattr(tg_commands, "_api_for",
                        lambda token, secrets: api)
    rep = tg_commands.sync_commands(_TWO_BOTS, dry_run=True)
    assert rep["ok"] is True and rep["dry_run"] is True
    assert api.deleted == [] and api.set_scopes == []

    entry = rep["bots"][0]
    labels = {row["label"] for row in entry["purged"]}
    assert {"default", "all_private_chats", "all_group_chats",
            "all_chat_administrators", "chat:111", "chat_admin:111"} <= labels
    row = next(r for r in entry["purged"] if r["label"] == "all_group_chats")
    assert row["current"]["zh"] == ["status"]
    assert row["current"]["default"] == []


def test_tg_sync_reports_residual_global_commands(monkeypatch):
    """清不掉的残留必须报错:可点但无权的菜单比没有菜单更糟。"""
    stale = {("all_private_chats", "", ""): [{"command": "status"}]}
    api = _FakeTgAPI(protect={"all_private_chats"}, stale=stale)
    monkeypatch.setattr(tg_commands, "_api_for",
                        lambda token, secrets: api)
    rep = tg_commands.sync_commands(_TWO_BOTS)
    assert rep["ok"] is False
    entry = next(b for b in rep["bots"] if b["roles"] == ["public"])
    assert entry["residual"]["all_private_chats"] == ["status"]
    assert any("all_private_chats" in e for e in rep["errors"])


def test_cli_tg_sync_dry_run_prints_stale_scope(home, monkeypatch, capsys):
    """干跑的人读输出必须点名"哪个 scope 里还留着哪条命令"。"""
    api = _FakeTgAPI(stale={("all_group_chats", "", "zh"): [{"command": "status"}]})
    monkeypatch.setattr(ops, "load_config", lambda path=None: {})
    monkeypatch.setattr(cli, "_safe_secrets", lambda cfg: (_TWO_BOTS, ""))
    monkeypatch.setattr(tg_commands, "_api_for", lambda token, secrets: api)
    rc = cli.main(["tg-sync", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "all_group_chats" in out and "current=" in out
    assert "status" in out and api.set_scopes == []


def test_secrets_loader_loads_telegram_whitelists(tmp_path):
    """文档里的 `TELEGRAM_OPS_CHAT_IDS` / `_PUBLIC_CHAT_IDS` 必须真的被读。

    修前 key map 里没有它们 ⇒ `resolve_bots()` 的白名单分支永远是空集,
    多运营/多公开群怎么配都不生效(且不报错)。
    """
    from market_operator import secrets_loader
    p = tmp_path / "secrets.env"
    p.write_text("TELEGRAM_BOT_TOKEN=bt\nTELEGRAM_CHAT_ID=1\n"
                 "TELEGRAM_OPS_CHAT_IDS='2, 3'\n"
                 "TELEGRAM_PUBLIC_CHAT_IDS=-100,\n", encoding="utf-8")
    got = secrets_loader.load_secrets(p, required=(), environ={})
    assert got["TELEGRAM_OPS_CHAT_IDS"] == "2, 3"
    assert got["TELEGRAM_PUBLIC_CHAT_IDS"] == "-100,"

    _bots, allowed = tg_commands.resolve_bots(got)
    assert allowed["ops"] == {"1", "2", "3"}
    assert allowed["public"] == {"1", "-100"}
    assert tg_commands.role_for_chat("3", allowed) == "ops"


# ---------------------------------------------------------------------------
# CLI:update 子命令与 bootstrap --update / TG 拉起
# ---------------------------------------------------------------------------


def test_cli_update_maps_flags(home, monkeypatch, capsys):
    seen: dict = {}

    def _fake(**kw):
        seen.update(kw)
        return {"ok": True, "skipped": True, "notes": ["非 git 工作区"]}

    monkeypatch.setattr(ops, "git_update", _fake)
    rc = cli.main(["update", "--check", "--no-install", "--timeout", "30",
                   "--json"])
    assert rc == 0
    assert seen == {"check": True, "install": False, "timeout": 30.0}
    doc = json.loads(capsys.readouterr().out)
    assert doc["skipped"] is True and doc["notes"] == ["非 git 工作区"]


def test_bootstrap_runs_update_and_tg_bot(home, monkeypatch, capsys):
    """`mo bootstrap --update` = 更新 → 自检 → 启动 → 暖机 → 对账 → TG。"""
    calls: dict = {}

    def _update(**kw):
        calls["update"] = kw
        return {"ok": True, "changed": False, "skipped": False}

    def _tg(args, **kw):
        calls["tg_wait"] = kw.get("wait_s")
        return {"ok": True, "started": True, "pid": 2, "alive": True}

    monkeypatch.setattr(ops, "git_update", _update)
    monkeypatch.setattr(ops, "load_secrets", lambda cfg: {})
    monkeypatch.setattr(ops, "doctor",
                        lambda *a, **kw: {"ok": True, "checks": [],
                                          "warnings": []})
    monkeypatch.setattr(ops, "start_supervisor",
                        lambda *a, **kw: {"started": True, "pid": 1,
                                          "alive": True, "heartbeat": True})
    monkeypatch.setattr(ops, "start_tg_bot", _tg)
    monkeypatch.setattr(ops, "warmup", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(ops, "positions_report",
                        lambda *a, **kw: {"local": [], "mismatches": [],
                                          "errors": []})

    cfg_dir = home / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "live.json").write_text("{}", encoding="utf-8")

    rc = cli.main(["bootstrap", "--layout", "legacy", "--update",
                   "--no-exchange", "--tg-wait", "4", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0, doc
    assert doc["ok"] is True
    assert doc["update"]["ok"] is True and calls["update"]["install"] is True
    assert doc["tg"]["started"] is True and calls["tg_wait"] == 4.0

    # --no-tg:明确不拉起机器人,结果里要能看出来是"跳过"而不是"失败"
    rc = cli.main(["bootstrap", "--layout", "legacy", "--no-exchange",
                   "--no-tg", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0, doc
    assert doc["tg"] == {"skipped": True, "why": "--no-tg"}


# ---------------------------------------------------------------------------
# 回归护栏:运维脚本里不再出现硬编码的运行期目录
# ---------------------------------------------------------------------------


def test_ops_shims_do_not_hardcode_runtime_dirs():
    """PowerShell 垫片同样必须走 `paths`:portable 布局下硬编码 `runtime\\` / `logs\\`

    会让看护读错心跳、把日志写进没人收的目录 —— 而且**不报错**,只是永远「看起来正常」
    (`live_gap_preview` 曾因此在 portable 下输出「没有缺口」的假结论)。
    只允许显式兜底那一行(`if (-not $RuntimeDir) { ... Join-Path $Repo 'runtime' }`):
    python 不在 PATH 时才走它,与迁移前逐字相同。
    """
    offenders = []
    for shim in sorted((ROOT / "scripts").glob("*.ps1")):
        text = shim.read_text(encoding="utf-8-sig", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if "Join-Path $Repo" not in line:
                continue
            if "if (-not $" in line:            # 显式兜底,不是事实源
                continue
            rest = line.split("Join-Path $Repo", 1)[1].strip()
            if rest.startswith(("'runtime", "'logs", "'reports", "'data")):
                offenders.append(f"{shim.name}:{i}: {line.strip()}")
    assert offenders == []


def test_live_scripts_do_not_hardcode_runtime_dirs():

    """`ROOT / "runtime"` 这类写法在 portable 布局下会各读各写,必须以 `paths` 为准。

    允许 `ROOT / "src"` / `"scripts"` / `"config"`(部署根锚定),只禁运行期类别。
    """
    offenders = []
    for script in sorted((ROOT / "scripts").glob("live_*.py")):
        text = script.read_text(encoding="utf-8")
        for bad in ('ROOT / "runtime"', 'ROOT / "logs"', "ROOT / 'runtime'",
                    'ROOT / "reports"', 'ROOT / "data"'):
            if bad in text:
                offenders.append(f"{script.name}: {bad}")
    assert offenders == []


def test_storage_gc_uses_paths_for_runtime_dirs():
    """`scripts/storage_gc.py` 也必须走 `paths`。

    修前它把 `logs`/`runtime`/`reports`/`data` 前缀钉在仓库根 ⇒ portable 布局下
    `var/logs` 无人治理,而预算闸门只会一直显示「宽裕」(不报错,只是管错了目录)。
    """
    text = (ROOT / "scripts" / "storage_gc.py").read_text(encoding="utf-8")
    for bad in ('ROOT / "runtime"', 'ROOT / "logs"', 'ROOT / "reports"',
                'ROOT / "data"', "ROOT / 'runtime'", "ROOT / 'logs'"):
        assert bad not in text, f"storage_gc.py 仍硬编码 {bad}"
    assert 'op("runtime")' in text and 'op("logs")' in text
    assert "paths.store_root()" in text


def test_schedule_cmds_resolve_log_dir_via_paths():
    """`tools/schedule/*.cmd` 的日志目录同样问 `paths`(portable ⇒ `var\\logs`)。

    修前是 `if not exist logs mkdir logs` + `>>logs\\x.log`:portable 下凭空多一个
    空 `logs\\`,脚本日志还落到没人盘点的目录。只放行显式兜底那一行
    (`if not defined LOGDIR set "LOGDIR=logs"`),即 python 不在 PATH 时的退路。
    """
    cmds = sorted((ROOT / "tools" / "schedule").glob("*.cmd"))
    assert cmds, "调度入口不见了?"
    offenders = []
    for cmd in cmds:
        text = cmd.read_text(encoding="ascii", errors="replace")
        assert "paths.log_dir()" in text, f"{cmd.name}: 没有向 paths 问日志目录"
        for i, line in enumerate(text.splitlines(), 1):
            low = line.strip().lower()
            if not low or low.startswith("rem"):
                continue
            if low == 'if not defined logdir set "logdir=logs"':    # 显式兜底
                continue
            if "if not exist logs" in low or "mkdir logs" in low or ">>logs\\" in low:
                offenders.append(f"{cmd.name}:{i}: {line.strip()}")
    assert offenders == []


# ---------------------------------------------------------------------------
# 一条命令启动:代码同步 / TG 拉起 / restart 不把机器人落下
# ---------------------------------------------------------------------------


def test_git_update_skips_folder_copy_deployment(home):
    """拷贝部署(没有 `.git`)必须 `skipped`,不是报错。

    `mo bootstrap --update` 是**默认启动路径**;把"没有仓库"当失败会让
    解压即用的机器永远起不来。
    """
    rep = ops.git_update(install=False)
    assert rep["ok"] is True and rep["skipped"] is True
    assert rep["is_repo"] is False and rep["changed"] is False
    assert any("非 git 工作区" in n for n in rep["notes"])


def test_scrub_git_redacts_remote_credentials():
    """git 输出会带 remote URL;token 进日志/JSON 等于把仓库打出去。"""
    raw = ("fatal: unable to access 'https://user:sekrit123@github.com/a/b.git/':\n"
           "Could not resolve host")
    out = ops._scrub_git(raw)
    assert "sekrit123" not in out and "user:" not in out
    assert "https://***@github.com" in out


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace")


def _commit(repo: Path, name: str, text: str, msg: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", msg)


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_git_update_check_without_upstream_reports_note(home):
    """没有上游跟踪分支时 `behind` 只能是 None —— 必须说出来,不能留一片空白。"""
    _git(home, "init")
    _commit(home, "a.txt", "v1", "c1")
    rep = ops.git_update(check=True, install=False, timeout=30)
    assert rep["ok"] is True and rep["is_repo"] is True
    assert rep["behind"] is None
    assert any("上游" in n for n in rep["notes"])


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_git_update_fast_forward_only_and_reports_behind(tmp_path, monkeypatch):
    """`--ff-only`:能快进就快进,分叉时宁可失败也不隐式 merge/reset。"""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    work = tmp_path / "app"
    origin.mkdir()
    _git(origin, "init", "--bare")
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "checkout", "-b", "main")
    _commit(seed, "a.txt", "v1", "c1")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    # 裸仓库 HEAD 默认指向 master;不指到 main,clone 出来是空工作区(无上游)
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    subprocess.run(["git", "clone", str(origin), str(work)], check=True,
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace")

    monkeypatch.setenv(paths.HOME_ENV, str(work))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    _commit(seed, "a.txt", "v2", "c2")
    _git(seed, "push", "origin", "main")
    rep = ops.git_update(install=False, timeout=60)
    assert rep["ok"] is True and rep["is_repo"] is True
    assert rep["changed"] is True and rep["before"] != rep["after"]
    assert (work / "a.txt").read_text(encoding="utf-8") == "v2"

    # 已是最新:再拉一次不产生变更,也不会误报
    again = ops.git_update(install=False, timeout=60)
    assert again["ok"] is True and again["changed"] is False
    assert any("已是最新" in n for n in again["notes"])

    # check 模式:只 fetch 报告落后数,不改工作区
    _commit(seed, "a.txt", "v3", "c3")
    _git(seed, "push", "origin", "main")
    chk = ops.git_update(check=True, install=False, timeout=60)
    assert chk["ok"] is True and chk["behind"] == 1
    assert (work / "a.txt").read_text(encoding="utf-8") == "v2"



