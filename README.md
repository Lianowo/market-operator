# market-operator

模拟专业交易者决策过程的加密货币研究与交易系统：因果市场认知 → 事件目录 → 可证伪交易假设 → 事件驱动持仓管理 → 全成本真实回测。研究标的：Bitget USDT 永续（BTC/ETH/SOL），多尺度（1h/2h/4h/12h/1d），初始权益 300 USDT。

**当前状态：Research Only（研究结论 NO-GO，无实盘路径）。** 见 `docs/acceptance.md`。

## 设计原则

- **时间即真相**：每个结构事实携带 `forming_index`（指向位置）与 `known_index`（可被知道的位置），任何决策只使用 `known_index <= t`。前缀不变性与"增量==批量"由测试锁定。
- **结构来自价格关系**：波动率自适应确认波段（ATR）、关键价格区域生命周期（ACTIVE/BROKEN/RETESTED/FLIPPED/DEAD)、regime 由 pivot 序列判定——不依赖方向性指标。
- **可证伪**：事件定义、参数先验、GO/NO-GO 判据在研究前冻结（`docs/methodology.md`、各轮 `docs/*-protocol.md`、`config/*.json`)，研究过程全程记录（`docs/research-log.md`)。
- **每个决策可解释**：`trades_*.csv` 的 reason 字段可追溯到方法论条款与事件 id。

## 唯一规范语义链（canonical)

系统当前只说一种语言（术语权威定义见 **`docs/SEMANTICS.md`**):

```
data ──► structure ──► events(E1-E7) ──► hypothesis ──► strategy_v6 ──► position(§8)
(抓取/校验/   (ATR/确认波段/    (E1-E7 事件       (可证伪      (turtle 唯一    (持仓管理
 sha256       区域生命周期/      目录,因果知时)    交易假设)      语义:2·atr      六条;
 manifest)   regime)                                        防线,无目标,   turtle 接线)
                                                            trailing 只紧)
                                                                    │
aggregate(多 TF 聚合) ◄──────────────────────────────────────────────┘
        │
        ▼
     broker(撮合/费用/资金费/强平约束;OrderIntent/OrderKind 规范家园)
        │
        ▼
     campaign(多尺度战役引擎:3 symbol × 5 TF = 15 战役隔离 + 3% 组合容量)
        │
        ▼
     backtest_v6(规范回放核心)──► scripts/run_backtest_v6.py(唯一规范
                                    回放入口,配置 config/canonical.json)
```

- **规范策略 = `market_operator.strategy_v6`**:v5 质量路由（E1 市价优先 /
  E6 STRONG 市价、WEAK→同 episode E7 前缘限价）× turtle 出口几何（无固定
  目标、初始防线 2.0·atr、trailing 只紧不松、关时间止损）。v6 协议的 A/B
  二臂判选已终局（臂 B 判选；记录见 `docs/v6-protocol.md` 原文与
  `reports/backtest_v6.json/.md` 冻结产物）,**臂切换与臂 A clamp2R 实现
  已移除**,双臂实现冻结于 `research/rounds/v6/`。
- **规范模块**:`data`、`aggregate`、`structure`、`events`、`hypothesis`、
  `position`、`broker`、`campaign`、`strategy_v6`、`backtest_v6`、`research`。

## 历史轮次归档（research/)

v1-v5 各轮与 v6 双臂 A/B 快照、PR#9/#10 前向设施已按轮次归档至
**`research/rounds/{v1..v6,external}/`**（模块 + 运行脚本 + 对应测试，
仅 import 路径修正，逻辑零改动）；对应关系（轮次 ↔ 协议 ↔ 冻结报告 ↔
复现命令）见 **`research/README.md`**。

## 复现

```bash
pip install -e ".[dev]"
python -m pytest -q                       # 313 passed(规范 tests/ + 历史 research/rounds/)

# 规范链(当前唯一语言;产物 reports/backtest_v6_turtle.*)
python scripts/run_backtest_v6.py

# 历史轮次(覆盖各自同名历史报告,详见 research/README.md)
python research/rounds/v1/run_backtest.py
python research/rounds/v2/run_backtest_v2.py
python research/rounds/v3/run_backtest_v3.py
python research/rounds/v4/run_backtest_v4.py
python research/rounds/v5/run_backtest_v5.py
python research/rounds/v6/run_backtest_v6.py   # v6 A/B 双臂(复现判选)
```

数据固定证据：`data/manifest.json`（每批原始数据 sha256, 2024-10-15 → 2026-10-19, 3×17600 bars, 0 缺口）；合约规格离线缓存强制（`data/contracts.json` 逐条 sha256)。

## 研究结论快照（详见 `docs/acceptance.md`)

- 事件与交易各层证据（v1-v6）均未通过真实成本后的正期望检验；v6 战役
  引擎修复了出口几何（回吐中位达标）但 P&L 仍为负（见
  `reports/backtest_v6.md`)。判定：**Research Only**。失败分解与方向见
  `docs/acceptance.md` 与 `docs/research-log.md`。

## 来源与许可证

本仓库代码全部为原创实现，项目许可证 MIT（见 `LICENSE`)。研究中借鉴开源算法之处均已在 `docs/audits/` 记录来源/许可证/差异：pymarket-structure(MIT)、TapeFlow(MIT) 借鉴了思想级原语；quant-trading（专有许可）与 OrderFlow-absorption（无 LICENSE 文件）仅借鉴思想，无任何代码复制。
