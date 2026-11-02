# 资格与执行：因果审计结论（2026-11-02）

> Historical archive: the dedicated study scripts, experimental APIs, candidate configuration and raw generated artifacts described below were removed on 2026-11-02. Commands below are historical, NOT current runnable instructions. Read `session-cleanup-20261102.md` for retained code and `research-results-archive-20261102.md` for archived result tables. Production defaults were not changed by cleanup.


## 结论与决策

没有找到已通过验收的、更优入场/退出替代策略，也不能证明当前基线全局最优。
当前决策是保留基线，不部署已有失败候选，不把语义解释替代盈利证据。

新增 canonical 链内只读诊断：`ScenarioRunner.run(exit_audit=list)`，
默认 None，不改变订单或状态决策。不是 shadow 服务或第二套信号系统。

## 隔离方法

沿用四标的四认知周期、300U、同一历史数据、15m 执行、2h 价格管理与原
认知退出。未调整入场、风险、费用、目标、预算、峰值观察或资格条件。
六个场景（dev/test/full × 1/1.5x）逐笔 JSON 与先前重构基线完全一致，
汇总除两个新增审计计数器外完全一致，原实验产物未覆盖。

管理事件记录原 bar 标签与实际观察完成时间，资格、峰值、防线前后值和
退出意图。既定防线检查在下一次管理之前执行；审计断言触线与 broker
成交对应，并核对已更新防线不被反向用于观察完成之前的细 bar。

## 结果

| 场景 | 既定防线检查 | 保护成交 | 回吐耗尽裁决 | 首次资格与耗尽同观察 |
|---|---:|---:|---:|---:|
| dev/1x | 98,391 | 400 | 30 | 24 |
| dev/1.5x | 98,731 | 395 | 30 | 23 |
| test/1x | 56,911 | 199 | 22 | 17 |
| test/1.5x | 56,692 | 198 | 21 | 16 |
| full/1x | 156,445 | 600 | 51 | 40 |
| full/1.5x | 156,709 | 594 | 50 | 38 |

各段独立账户，不能相加。保护成交包括初始/结构/锁利防线，不全是锁利。
full/1x 另有 166 次持久 armed 转换、372 次资格后峰值更新、1,236 次
防线更新、446 次防线更新后的保护成交。立即退出时 PM 已移除仓位，未
回写 armed，因此“持久 armed 转换”不包含那 40 次即时资格退出。

51 次回吐耗尽中 40 次在此前未 armed 的观察中直接退出：按现有语义，
首次获准锁利时，已知峰值至当前收盘的回吐已超预算。它不是已有固定
保护价等待下一次收盘的问题。改变这类退出须改变资格证据或峰值/预算
契约，不能称为单纯执行提速。

## 为什么不继续搜索

既定防线已在执行网格逐根检查。资格后更快 on_bar 会观察新峰值，而非只
执行旧防线，先前 armed_15m 已未通过验收。更早资格与 attempt_failure
也未通过；本轮不能把它们换名后重新当有效规则。

入场目前仍有 n_a 等根数条件；本次没有完成替代它们的因果规则。因此不
宣称已经实现“完全事件化最优入场”。新的操作化证据必须先固定定义，再
明确首次可知时间、证伪、订单准入与持有许可，独立评价机会身份及风险。

## 限制

- 复用既有数据和已被查看的 test，不是新样本、样本外发现或优势证明。
- OHLC 不能确定根内顺序；stop fill 是模型，不是交易所实际延迟/成交保证。
- 记录的保护 fill 标签为细 bar 开始，不冒充精确触价时刻。
- 模型无漏执行不证明实盘无故障；实盘撤挂窗口、行情新鲜度、网络、回执与
  重启恢复链仍须另行验证。本轮未读取账户或进程，未重启、下单。
- pickle 恢复测试仅验证测试对象序列化后的固定防线执行，不证明 live 冷档
  全链恢复；本轮没有添加或迁移生产持久状态。

## 文件与复现

`tools/audit_exit_contract.py`；`tests/test_exit_contract_audit.py`；
`reports/exit_contract_audit_20261102/summary.json` 与六份事件文件。

工具依赖历史 `reports/exit_contract_refactor_20261102/` 作逐笔等价对照，
缺失或不同即失败，不静默重建或覆盖基线。

```text
python tools/audit_exit_contract.py
python -m pytest tests -q
git diff --check
```