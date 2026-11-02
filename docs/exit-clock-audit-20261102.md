# 入场证据与离场时钟审计（2026-11-02）

> Historical archive: the dedicated study scripts, experimental APIs, candidate configuration and raw generated artifacts described below were removed on 2026-11-02. Commands below are historical, NOT current runnable instructions. Read `session-cleanup-20261102.md` for retained code and `research-results-archive-20261102.md` for archived result tables. Production defaults were not changed by cleanup.


## 实际离场时钟（当前代码/配置，不是运行进程或成交回执的审计）

| 动作 | 实盘当前时钟 | 注意事项 |
|---|---|---|
| 已挂原生 TPSL 止损 | 交易所按触发价执行 | 不等本地 2h 收盘；实际成交取决于交易所订单/行情；本轮没查账户 |
| 本地保护缺失检查/对账 | 主循环及独立运维节奏 | 不能把 15s 主循环理解为所有价格决策 15s 一次 |
| 对侧锁利首次启用 | 已收盘 2h 子 bar | 正式 frac=-0.25，要求该观察 bar 收盘突破目标 |
| 峰值棘轮、回吐预算耗尽 | 已收盘 2h 子 bar | 已经挂好的防线仍可由交易所盘中执行；本地新防线尚未观察就不会生成 |
| 确认 pivot 移动防线 | 本仓认知周期收盘 | 2h/4h/12h/1d，不由小周期替代 |
| 反向 STRUCTURE_BROKEN 退出 | 本仓认知周期事件 | tracker 事件送路由不等于已经送持仓管理执行退出 |

`_step_inner` 在下一根 2h 收盘之前跳过取 bar；`_advance` 的 base_ms 固定
7200000，`_on_sub_bar` 由此调用。所以“本地价格型退出最细仍受 2h 约束”
成立，“所有止损都等 2h”不成立。重返箱体退出、时间退出等可选规则不应
与正式启用规则混淆。

旧注释声称锁利 arming 与采样频率无关是错误的：15m 收盘越过目标，但
2h 收盘返回目标内，负 frac 的结果不同。本轮修正文档并加测试，不改实盘
启用条件。共享函数只证明公式同源，不证明不同周期持仓路径相同。

## 离场独立回放

入场完全沿正式信号；四标的四周期、300U、正式风险与历史资金费；共同
15m 执行。大周期事件、pivot、锁利预算、初始止损不变。

- cognition：不额外推进价格型管理。
- price_2h：每 2h 收盘推进价格型管理，近似当前实盘观察节奏，但非完整实盘等价。
- price_15m：首次启用和后续管理均 15m，混合了启用语义与执行速度变化。
- armed_15m：首次启用仍 2h；已经启用后 15m 更新。仍会改变峰值观察，
  不是纯粹“同一张退出订单更快提交”。HTF 管理照常推进。

新增可选 cadence 在 ExecutionConfig 默认 None，默认回放路径保持不变。
粗观察 bar 只聚合完整已收盘执行片段，禁止纳入开仓前极值；所有组共用
执行网格、保护止损和费用，不额外加小周期 pivot 防线。

| 段/成本 | cognition | price_2h | price_15m | armed_15m |
|---|---:|---:|---:|---:|
| dev/1x | 320.3191 | 278.2201 | 194.6402 | 263.4485 |
| dev/1.5x | 205.9972 | 188.0812 | 91.3608 | 177.0452 |
| test/1x | 157.6895 | 133.8253 | 130.9996 | 129.3791 |
| test/1.5x | 101.1968 | 79.1831 | 60.7472 | 76.7389 |
| full/1x | 647.0151 | 532.0240 | 405.9502 | 521.4786 |
| full/1.5x | 354.3923 | 310.4551 | 170.9390 | 287.5879 |

full/1x 最大回撤分别 13.8993%、14.4909%、12.7909%、14.5635%；
full/1.5x 分别 24.7825%、22.0895%、19.2658%、21.8754%。

price_15m 对 price_2h 的 full/1x 同入场决策匹配已平仓 656 笔，120 笔
早退，提前中位数 1.875h，共同交易实际 R 合计减少 15.8768R。
armed_15m 匹配 659 笔，32 笔早退，提前中位数 1h，共同 R 减少 2.9328R。
共享权益、容量、后续再入场会反馈，因此该对账不是固定仓位反事实归因。

两提前组均未通过以 price_2h 为基线的逐场景/品类收益下界。结果表明
该具体方案“更快但未更好”，并不证明 2h 是最优观察周期。不因回撤局部
改善就部署，也没有据结果继续搜索参数。旧 test 不能宣称新独立样本。

## 入场证据对账（未新增信号）

在现有 `replay_entry_timing.py --audit-evidence-only` 中记录全部细周期
原始事件，不筛掉未匹配者。ACCEPT 记录实际首次外侧收盘至确认的时间；
REJECT 记录局部极值至确认，但明确**极值不是当时可交易的拒绝证据**。
基线按同事件、方向、角色、origin_known、精确区域边界一对一匹配。

本次细周期 ACCEPT 的首次突破至确认中位数为 30min；REJECT 极值至确认
中位数为 15min。它们不是全系统平均等待，也不是证明只需减少这些分钟。
REJECT 能匹配的各组合，提前中位数 90–120min，下一开盘价格改善中位数
约 17.52–53.33bps。未匹配占多数，必须完整保留。

2h ACCEPT 在严格身份和时间窗下零匹配，4h 也仅 2–7 个；不能据此宣称
“原同一交易提前多少”或把未匹配直接叫假突破。它暴露了“提前流与原流
并非同一机会集”的问题，先前的整体收益差不能单纯解释为入场价优劣。

## 生产与复现

没有改正式配置、重启实盘或发送订单；实盘源文件本轮仅修正注释。
不添加影子服务。明确分开证据变化、观察时钟和执行响应；本轮不部署
回放未通过的提前规则。历史研究产物保留，不被生产读取。

```text
python tools/replay_exit_timing.py
python tools/replay_entry_timing.py --audit-evidence-only --output reports/entry_evidence_20261102
python -m pytest tests -q
```

产物：`reports/exit_timing_20261102/summary.json` 与 24 份逐笔，
`reports/entry_evidence_20261102/entry_evidence.json`。