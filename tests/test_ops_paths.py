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
        ["restart", "--warmup-first"],
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



