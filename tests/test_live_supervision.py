"""实盘看护 + 存储治理测试(判定表逐档 / 单实例锁 / 看护动作 / 可逆压缩)。

判活口径只认 `runtime/live/heartbeat.json` 里的时间事实 —— 这里也按同一口径
逐档断言,避免"改阈值顺手改语义":
① `classify` 五档边界(含掉线补做的 `recover_max` 上限);
② `decide` 动作表(缺失/正常/迟到/补做/停摆;重启预算耗尽 → give_up);
③ `beat_age_seconds` 的启动宽限(旧尸体留下的 `loop_ts_ms` 不得误判);
④ `SingletonLock` 跨进程互斥、进程退出即内核释放(无 pid 毒锁);
⑤ `Supervisor.judge/run_once`:缺失、停摆、补做中、启动宽限、演练不拉起;
⑥ 存储治理:`gzip_keep` / `archive_and_truncate` 无损(可解压、留尾巴)、
   `_gate` 三档退出码、增长预测只按运维目录口径。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_operator import heartbeat as hb                          # noqa: E402

import live_supervisor as supv                                       # noqa: E402
import storage_gc as gc                                              # noqa: E402

OK_S, STALE_S, RECOVER_MAX_S = 120.0, 300.0, 2700.0


# ---------------------------------------------------------------------------
# ① classify:五档边界
# ---------------------------------------------------------------------------


def test_classify_five_bands_boundaries():
    kw = {"ok_s": OK_S, "stale_s": STALE_S}
    assert hb.classify(None, "ok", **kw) == hb.MISSING
    assert hb.classify(0.0, "ok", **kw) == hb.OK
    assert hb.classify(OK_S, "ok", **kw) == hb.OK          # 恰界算正常
    assert hb.classify(OK_S + 0.1, "ok", **kw) == hb.LATE
    assert hb.classify(STALE_S, "ok", **kw) == hb.LATE     # 恰界仍只记录
    assert hb.classify(STALE_S + 0.1, "ok", **kw) == hb.STALLED


def test_classify_recovering_respects_recover_max():
    # 掉线补做可能重放几千根 bar:阈值内继续等,超过上限按卡死处理
    assert hb.classify(1800.0, "recovering", ok_s=OK_S, stale_s=STALE_S,
                       recover_max_s=RECOVER_MAX_S) == hb.RECOVERING
    assert hb.classify(RECOVER_MAX_S, "recovering", ok_s=OK_S,
                       stale_s=STALE_S,
                       recover_max_s=RECOVER_MAX_S) == hb.RECOVERING
    assert hb.classify(RECOVER_MAX_S + 1.0, "recovering", ok_s=OK_S,
                       stale_s=STALE_S,
                       recover_max_s=RECOVER_MAX_S) == hb.STALLED
    # 未开豁免(recover_max_s=0)时该状态不外溢
    assert hb.classify(200.0, "recovering", ok_s=OK_S,
                       stale_s=STALE_S) == hb.LATE


# ---------------------------------------------------------------------------
# ② decide:动作表 + 重启预算
# ---------------------------------------------------------------------------


def test_decide_action_table():
    kw = {"ok_s": OK_S, "stale_s": STALE_S, "recover_max_s": RECOVER_MAX_S}
    assert hb.decide(None, "ok", restarts_left=1, **kw).action == "restart"
    assert hb.decide(10.0, "ok", restarts_left=1, **kw).action == "none"
    assert hb.decide(200.0, "ok", restarts_left=1, **kw).action == "warn"
    assert hb.decide(1800.0, "recovering", restarts_left=1,
                     **kw).action == "none"
    stalled = hb.decide(400.0, "ok", restarts_left=1, **kw)
    assert (stalled.action, stalled.kind) == ("restart", hb.STALLED)
    assert stalled.should_start is True


def test_decide_budget_exhausted_stops_spawning():
    """预算耗尽后连"心跳缺失"也不再拉起:否则启动即崩会刷成进程风暴。"""
    kw = {"ok_s": OK_S, "stale_s": STALE_S, "recover_max_s": RECOVER_MAX_S}
    assert hb.decide(400.0, "ok", restarts_left=0, **kw).action == "give_up"
    missing = hb.decide(None, "ok", restarts_left=0, **kw)
    assert (missing.action, missing.kind) == ("give_up", hb.MISSING)
    assert missing.should_start is False


# ---------------------------------------------------------------------------
# ③ beat_age_seconds:刚重启不得被旧 loop_ts_ms 误判
# ---------------------------------------------------------------------------


def test_beat_age_uses_started_when_loop_is_older():
    now = hb.now_ms()
    fresh = {"ts_ms": now, "started_ms": now - 5_000,
             "loop_ts_ms": 0}
    age = hb.beat_age_seconds(fresh, now)
    assert age is not None and 4.0 <= age <= 6.0
    # 上一具尸体留下的 loop_ts_ms 比 started_ms 更旧 → 取 started_ms
    dirty = {"ts_ms": now, "started_ms": now - 5_000,
             "loop_ts_ms": now - 7_200_000}
    age = hb.beat_age_seconds(dirty, now)
    assert age is not None and age <= 6.0


def test_beat_age_falls_back_and_handles_empty():
    now = hb.now_ms()
    assert hb.beat_age_seconds({}, now) is None
    legacy = {"ts_ms": now - 30_000}
    assert hb.beat_age_seconds(legacy, now) == pytest.approx(30.0, abs=0.5)


def test_read_heartbeat_ignores_junk(tmp_path):
    junk = tmp_path / "heartbeat.json"
    junk.write_text("{}", encoding="utf-8")
    assert hb.read_heartbeat(junk) == {}
    junk.write_text("not json", encoding="utf-8")
    assert hb.read_heartbeat(junk) == {}
    good = {"schema": hb.SCHEMA, "ts_ms": hb.now_ms()}
    junk.write_text(json.dumps(good), encoding="utf-8")
    assert hb.read_heartbeat(junk)["schema"] == hb.SCHEMA


# ---------------------------------------------------------------------------
# ④ SingletonLock:跨进程互斥,进程死亡即释放(不用 pid 文件)
# ---------------------------------------------------------------------------


def test_singleton_lock_is_process_exclusive(tmp_path):
    lock_path = tmp_path / "live_runner.lock"
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT / 'src'}')\n"
        "from market_operator.heartbeat import SingletonLock\n"
        f"lk = SingletonLock(r'{lock_path}')\n"
        "assert lk.acquire(), 'child failed to acquire'\n"
        "print('locked', flush=True)\n"
        "sys.stdin.read()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert hb.SingletonLock(lock_path).acquire() is False
    finally:
        proc.communicate("go", timeout=60)
    # 子进程已退出 → 内核释放锁,本进程立刻可拿到(不存在"锁残留")
    again = hb.SingletonLock(lock_path)
    assert again.acquire() is True
    again.release()
    third = hb.SingletonLock(lock_path)
    assert third.acquire() is True
    third.release()


# ---------------------------------------------------------------------------
# ⑤ Supervisor:判定 → 动作(全程不动真进程)
# ---------------------------------------------------------------------------


SUP_CFG = {"supervisor": {"poll_seconds": 30, "ok_seconds": OK_S,
                         "stale_seconds": STALE_S,
                         "recover_max_seconds": RECOVER_MAX_S,
                         "max_restarts_per_hour": 6,
                         "startup_grace_seconds": 180,
                         "gc_hours": 0, "log_max_mb": 32,
                         "runner_argv": ["scripts/live_runner.py",
                                         "--mode", "live"]}}


@pytest.fixture
def sup(tmp_path, monkeypatch):
    """把看护的文件全景搬进 tmp_path(绝不碰真实 runtime/ 与 logs/)。"""
    monkeypatch.setattr(supv, "HB_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(supv, "STATE_PATH", tmp_path / "supervisor.json")
    monkeypatch.setattr(supv, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(supv, "RUNNER_LOG", tmp_path / "logs/live_stdout.log")
    monkeypatch.setattr(supv, "RUNNER_ERR", tmp_path / "logs/live_stderr.log")
    return supv.Supervisor(SUP_CFG, notify=False, allow_start=False)


def _write_hb(monkeypatch_hb_path: Path, **over) -> dict:
    now = hb.now_ms()
    doc = {"schema": hb.SCHEMA, "ts_ms": now, "started_ms": now - 3_600_000,
           "loop_ts_ms": now - 5_000, "status": "ok", "pid": 4242,
           "mode": "live", "step_count": 7, "positions": 0, "stops": 0,
           "positions_detail": []}
    doc.update(over)
    monkeypatch_hb_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch_hb_path.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_supervisor_healthy_heartbeat_does_nothing(sup):
    _write_hb(supv.HB_PATH)
    decision, doc, age = sup.judge()
    assert decision.action == "none"
    assert decision.kind == hb.OK
    assert doc.get("pid") == 4242 and age is not None


def test_supervisor_missing_heartbeat_restarts_without_spawning(sup):
    decision, _doc, age = sup.judge()
    assert (decision.kind, decision.action) == (hb.MISSING, "restart")
    assert age is None
    sup.run_once(dry=True)                      # 演练:只记录,不拉起
    assert not supv.STATE_PATH.exists()         # 演练不消耗重启预算


def test_supervisor_stalled_heartbeat_requests_restart(sup):
    now = hb.now_ms()
    _write_hb(supv.HB_PATH, loop_ts_ms=now - 400_000)   # 主循环 400s 无进展
    decision, _doc, age = sup.judge()
    assert decision.kind == hb.STALLED
    assert decision.action == "restart"
    assert age is not None and age >= 400.0


def test_supervisor_budget_exhausted_gives_up(sup):
    now = time.time()
    sup.state["restarts"] = [now - i for i in range(6)]
    sup._save_state()
    now_ms = hb.now_ms()
    _write_hb(supv.HB_PATH, loop_ts_ms=now_ms - 400_000)
    decision, _doc, _age = sup.judge()
    assert decision.action == "give_up"
    assert sup.restarts_left() == 0


def test_supervisor_startup_grace_after_spawn(sup):
    now = time.time()
    sup.state["spawned_at_s"] = now
    _write_hb(supv.HB_PATH, ts_ms=hb.now_ms() - 400_000,
              started_ms=hb.now_ms() - 400_000,
              loop_ts_ms=hb.now_ms() - 400_000)
    decision, _doc, _age = sup.judge()
    assert decision.kind == "startup"
    assert decision.action == "none"


def test_supervisor_waits_while_recovering(sup):
    now = hb.now_ms()
    _write_hb(supv.HB_PATH, status="recovering",
              loop_ts_ms=now - 1_800_000)
    decision, _doc, _age = sup.judge()
    assert decision.kind == hb.RECOVERING
    assert decision.action == "none"


def test_supervisor_status_text_reports_budget(sup):
    _write_hb(supv.HB_PATH)
    text = sup.status_text()
    assert "重启预算" in text and "6/6" in text


# ---------------------------------------------------------------------------
# ⑥ 存储治理:可逆压缩 / 无损截断 / 闸门 / 增长口径
# ---------------------------------------------------------------------------


def test_is_hot_whitelist_covers_runner_state():
    assert gc.is_hot("runtime/live/heartbeat.json") is True
    assert gc.is_hot("runtime\\live\\state.json") is True   # Windows 分隔符
    assert gc.is_hot("runtime/live/live_runner.lock") is True
    assert gc.is_hot("logs/live_trades.jsonl") is False
    assert gc.is_hot("data/raw/BTCUSDT_2H.csv") is False


def test_gzip_keep_is_lossless_and_reversible(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "logs").mkdir(parents=True)
    monkeypatch.setattr(gc, "ROOT", root)
    monkeypatch.setattr(gc, "MANIFEST",
                        root / "reports" / "_storage_manifest.jsonl")
    src = root / "logs" / "live_trades.jsonl"
    payload = b'{"trade": 1, "sym": "BTCUSDT"}\n' * 500
    src.write_bytes(payload)

    ok, msg = gc.gzip_keep(src, dry=False)
    assert ok, msg
    assert not src.exists()

    gz = src.with_name(src.name + ".gz")
    assert gzip.decompress(gz.read_bytes()) == payload      # 字节级无损

    rec = json.loads(gc.MANIFEST.read_text(encoding="utf-8").strip())
    assert rec["orig"] == "logs/live_trades.jsonl"
    assert rec["size"] == len(payload)
    assert rec["sha256"] == hashlib.sha256(payload).hexdigest()

    # 同名 .gz 已存在时绝不覆盖(旧 .gz 可能是唯一的审计证据)
    src.write_bytes(payload)
    ok2, msg2 = gc.gzip_keep(src, dry=False)
    assert ok2 is False and "已存在" in msg2
    assert gzip.decompress(gz.read_bytes()) == payload


def test_archive_and_truncate_keeps_full_gzip_and_tail(tmp_path):
    src = tmp_path / "live_stdout.log"
    payload = b"".join(f"line {i:06d} hello\n".encode() for i in range(20_000))
    src.write_bytes(payload)

    ok, msg = gc.archive_and_truncate(src, keep_bytes=4096, dry=False)
    assert ok, msg
    gz = next(tmp_path.glob("*.log.gz"))
    assert gzip.decompress(gz.read_bytes()) == payload       # 现场完整保留
    after = src.read_bytes()
    assert after.startswith("[storage_gc]".encode())         # 截断标记
    assert after.endswith(payload[-4096:])                   # 尾巴原样保留


def test_archive_and_truncate_skips_small_and_honours_dry(tmp_path):
    small = tmp_path / "tiny.log"
    small.write_bytes(b"abc\n")
    ok, msg = gc.archive_and_truncate(small, keep_bytes=4096, dry=False)
    assert ok is False and "无需处理" in msg

    big = tmp_path / "big.log"
    payload = b"x" * 50_000
    big.write_bytes(payload)
    ok, msg = gc.archive_and_truncate(big, keep_bytes=4096, dry=True)
    assert ok is True and msg.startswith("[dry]")
    assert big.read_bytes() == payload                       # 演练不动盘
    assert not list(tmp_path.glob("*.gz"))


def test_gate_exit_codes():
    assert gc._gate(40.0, {"projected_30d_gb": 41.0}, 35.0) == 2   # 已超硬线
    assert gc._gate(30.0, {"projected_30d_gb": 36.0}, 35.0) == 1   # 30 天越线
    assert gc._gate(30.0, {"projected_30d_gb": 31.0}, 35.0) == 0   # 通过


def test_growth_stats_projects_from_ops_traffic_only(tmp_path, monkeypatch):
    """预测只认日志/运行时/数据:一次性研究产物不得把"还能跑多久"撑大。"""
    for rel, blob in (("logs/a.jsonl", 1_000_000),
                      ("reports/big.json", 5_000_000),
                      ("config/c.json", 3_000_000)):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * blob)
    monkeypatch.setattr(gc, "ROOT", tmp_path)
    monkeypatch.setattr(gc, "inventory", lambda: {"logs": [1_000_000, 1]})

    st = gc.growth_stats(1.0)
    assert st["ops_bytes_per_day"] == pytest.approx(1_000_000, rel=0.01)
    assert st["bytes_per_day"] == pytest.approx(9_000_000, rel=0.01)
    assert st["projected_30d_gb"] == pytest.approx(
        (1_000_000 + 30 * 1_000_000) / (1 << 30), rel=0.01)

