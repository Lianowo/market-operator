# market-operator

模拟专业交易者决策过程的加密货币研究与交易系统：因果市场认知 → 事件目录 → 可证伪交易假设 → 事件驱动持仓管理 → 全成本真实回测。研究标的：Bitget USDT 永续（BTC/ETH/SOL），观察尺度 1h，初始权益 300 USDT。

**当前状态：Research Only（研究结论 NO-GO，无实盘路径）。** 见 `docs/acceptance.md`。

**最新 v4（2026-10-19，修复前冻结研究产物）：** test@1x 净收益 −25.13%、PF 0.29、32 笔；full@1x −56.00%、PF 0.39、140 笔（见 `reports/backtest_v4.md`）。test 已历经多轮复用，绝不代表新的独立 OOS。历史报告不得拿来宣称已验证正期望。

**代码审计（仅审查分支）：** `docs/audits/2026-10-19-execution-reconciliation.md` 记录 v4 maker 减仓意图与真实成交状态脱节的问题、修复范围和残留阻断项。审计分支上的正确性修复尚未重新认证任何策略收益；`reports/` 下旧结果不会被覆盖，策略仍然 NO-GO。

## 设计原则

- **时间即真相**：每个结构事实携带 `forming_index`（指向位置）与 `known_index`（可被知道的位置），任何决策只使用 `known_index <= t`。前缀不变性与"增量==批量"由测试锁定。
- **结构来自价格关系**：波动率自适应确认波段（ATR）、关键价格区域生命周期（ACTIVE/BROKEN/RETESTED/FLIPPED/DEAD)、regime 由 pivot 序列判定——不依赖方向性指标。
- **可证伪**：事件定义、参数先验、GO/NO-GO 判据在研究前冻结（`docs/methodology.md`,`config/defaults.json`)，研究过程全程记录（`docs/research-log.md`)。
- **每个决策可解释**：`trades_*.csv` 的 reason 字段可追溯到方法论条款与事件 id。

## 架构（单体·纯 Python·无服务依赖）

| 职责 | 模块 |
|---|---|
| Market Observer 数据与因果纪律 | `market_operator/data`（抓取/校验/sha256 manifest) |
| Market Understanding 认知 | `market_operator/structure`(ATR/确认波段/区域生命周期/regime) |
| Opportunity Reasoner | `market_operator/events`(E1-E6) + `market_operator/hypothesis` |
| Position Manager | `market_operator/position`(§8 六条） + `market_operator/strategy` |
| Execution & Accounting | `market_operator/broker`（真实合约规格/费用/资金费/滑点/强平约束） |
| 研究与回测 | `market_operator/research`、`market_operator/backtest` |

事件目录 E1-E6 及全部语义见 `docs/methodology.md`；开源算法独立审计（决定借鉴边界）见 `docs/audits/`。

## 复现

```bash
pip install -e ".[dev]"
python -m pytest tests -q                      # 111 passed
python -m market_operator.data fetch --symbols BTCUSDT,ETHUSDT,SOLUSDT --years 2
python scripts/run_event_study.py              # -> reports/event_study.{json,md}
python scripts/run_backtest.py                 # -> reports/backtest.{json,md}, trades_*.csv, equity_*.csv
```

数据固定证据：`data/manifest.json`（每批原始数据 sha256, 2024-10-15 → 2026-10-19, 3×17600 bars, 0 缺口）。

## v1 研究结论快照（详见 `docs/acceptance.md`)

- 事件层面（test 段，相对同 regime 匹配对照的增量）:E1|short +1.00 ATR、E6|short +1.76 ATR（结构性信息存在），但**绝对净期望分别为 −0.32 / −0.05 ATR**。
- 交易层面（§8 持仓管理 + 真实成交 + 全部成本, 300 USDT)：9 段×成本全负，full@1x 净 −240.7 USDT, PF 0.76。成本吞噬全部毛利。
- 判定：**无机制在真实成本后正期望 → Research Only**。失败分解与 v2 方向见 `docs/acceptance.md`。

## 来源与许可证

本仓库代码全部为原创实现，项目许可证 MIT（见 `LICENSE`)。研究中借鉴开源算法之处均已在 `docs/audits/` 记录来源/许可证/差异：pymarket-structure(MIT)、TapeFlow(MIT) 借鉴了思想级原语；quant-trading（专有许可）与 OrderFlow-absorption（无 LICENSE 文件）仅借鉴思想，无任何代码复制。
