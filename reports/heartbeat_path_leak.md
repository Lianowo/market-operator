# 心跳文件归属缺陷:测试能覆盖生产 heartbeat(2026-10-24)

## 1. 现象(两次实测)

| 时刻 | 触发 | 被写入的 `runtime/live/heartbeat.json` |
|---|---|---|
| 2026-10-24 00:53(部署前) | 在 `market-operator-main` 跑全量 pytest | `mode=paper, step_count=1, positions=0, equity=1000.0, saved_at_utc=2026-10-23T22:00:00+00:00` |
| 2026-10-24 01:19(实盘已起, pid 49100) | 在**实盘树**跑 `pytest tests/test_position.py tests/test_v21_balance_trigger.py`(38 passed) | 同上:`pid=50828, mode=paper, step=1, pos=0, equity=1000.0, at=2026-10-23T22:00:00` |

第二次 ~15s 后由实盘 runner 自己的呢节拍覆盖回 `pid=49100, mode=live, step=20, pos=10, at=17:19:21`;
`state.json`(10 仓/10 止损/49 意图)与 `logs/live_*.jsonl`(`reconcile ok=true ours=10 exchange=10`)未被污染 ——
泄漏面只有心跳文件本身。

## 2. 根因

`scripts/live_runner.py` → `LiveRunner.__init__`(修前):

```python
self.state_path = Path(state_path) if state_path else \
    ROOT / self.loop_cfg["state_path"]
...
self.heartbeat_path = Path(
    self.loop_cfg.get("heartbeat_path")            # "runtime/live/heartbeat.json"
    or (self.state_path.parent / "heartbeat.json"))
...
self._write_json_atomic(self.heartbeat_path, ...)  # 起动即落一次心跳
```

三处叠加:

1. **不对称**:`state_path` 覆盖生效且锚定 `ROOT`;`heartbeat_path` 直接吃配置里的相对串,**按 cwd 解析**,
   完全无视 `state_path` 覆盖。在仓库根跑测试 ⇒ 相对串正好指向生产文件。
2. **构造即写**:`__init__` 末尾"起动即落一次心跳"(为看护立刻看到新 pid),所以测试只要**构造** runner 就会写盘,
   不需要跑完一步、也不需要 15s。
3. 测试用真 `config/live.json`(v21 参数需要),`state_path`/`journal_dir` 已指 `tmp_path`,唯独心跳路径漏了。

## 3. 影响(为何不是"测试噪音")

* 看护判活的**唯一证据**就是这个文件(README:`age <= supervisor.ok_seconds` → 正常、无动作)。
* `market_operator.heartbeat.beat_age_seconds` 结尾是 `max(0.0, (now - base))`;测试合成时钟(`22:00 UTC`)在当下之后
  ⇒ `age = 0.0` ⇒ 判定表永远走"正常"这一行。**此时真实 runner 已死也不会被拉起**,与 2026-10-22 掉线事故同一类失效。
* 心跳里还带着 `mode=paper / positions=0`:任何据心跳 `positions`/`mode` 决策的巡检都会看到"空仓的纸面实例"。
* 覆盖窗口 = 测试进程存活期间 + 至多 15s(下个实盘节拍),这段时间实盘处于"看护失明"。

## 4. 修法

* `LiveRunner.__init__` 新增 `heartbeat_path` 关键字参数,决议顺序:
  **显式 `heartbeat_path` > 显式 `state_path` 的同目录 > `loop.heartbeat_path`(锚定 `ROOT`)**。
* 生产调用(`main()`)不传 `state_path`/`heartbeat_path`,口径不变(`runtime/live/heartbeat.json`),
  但不再依赖 cwd。
* 测试/沙盒只要传了 `state_path`,心跳自动跟随进 `tmp`;**现有 4 处 `LiveRunner(...)` 调用点无需改动即被隔离**。
* 回归测试(`tests/test_live_supervision.py`):
  `test_runner_heartbeat_follows_explicit_state_path`、`test_runner_heartbeat_explicit_arg_wins`、
  `test_runner_heartbeat_default_is_root_anchored`(构造时禁用 `_load_state` 与落盘,只验路径决议,
  保证测试自身绝不碰生产文件)。

## 5. 验证

在**实盘目录**内跑全量(`python -m pytest -q`),前后同一文件对比:

| 时点 | heartbeat |
|---|---|
| 跑前 | `pid=49100 mode=live step=26 pos=10` |
| 636 passed 之后 | `pid=49100 mode=live step=32 pos=10` |
| +18s | `pid=49100 mode=live step=34 pos=10` |

修前同一命令会把该文件覆盖成 `paper/0 仓`(见 §1)。测试套件 636 passed(原 633 + 本缺陷 3 条)。
实盘侧同时复核:`reconcile ok=true ours=10 exchange=10`、10 张交易所止损在位、`live_status.py` 正常退出。

## 6. 口径(已写入 README)

测试/沙盒必须传 `state_path`(或显式 `heartbeat_path`);`runtime/live/` 只许唯一的实盘进程写。
