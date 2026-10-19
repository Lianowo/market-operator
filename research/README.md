# research/ —— 历史轮次归档(博物馆区)

本目录收纳**不再属于规范语义链**的历史轮次代码与测试,供历史报告复现与
语义考古使用。规范链(当前唯一语言)见 `docs/SEMANTICS.md` 与顶层
`README.md`;规范代码只在 `src/market_operator/` + `scripts/`。

## 规则

- **不删历史产物**:每轮模块/脚本/测试原样冻结,仅做 import 路径修正
  (相对 import → `research.rounds.*` / `market_operator.*` 限定名)与
  `ROOT` 重定位;**逻辑、参数、断言一律不改**。
- 归档代码共享规范基础设施(broker/position/events/structure/hypothesis/
  research/aggregate/backtest_v6 无变更的部分),跨轮次引用按轮次包路径
  显式书写(如 v5 引擎引用 v1 的 strategy、v4 的 backtest_v4)。
- OrderIntent/OrderKind:规范家园为 `market_operator.broker`;
  `research/rounds/v1/strategy.py` 自规范家园再导出(单一枚举身份,
  历史 API 不变)。
- 重跑历史脚本会覆盖其**同名**历史报告(reports/*);复现前如需保留
  现状请先备份或依赖 git 历史。reports/* 的既有内容在本仓库中保持冻结。
- 测试:`python -m pytest` 同时收集 `tests/`(规范)与 `research/rounds/`
  (历史);历史测试对**归档实现**运行(见下表)。

## 轮次索引

| 轮 | 内容 | 协议/文档 | 配置 | 冻结报告产物 |
|---|---|---|---|---|
| **v1** | `rounds/v1/strategy.py`(GO 白名单策略)、`backtest.py`(单帧回放引擎)、`run_backtest.py`、`run_event_study.py`;tests: test_strategy / test_backtest | docs/methodology.md(+ docs/acceptance.md v1 快照) | config/defaults.json | `reports/event_study.*`、`reports/backtest.*`、`trades_*`、`equity_*` |
| **v2** | `rounds/v2/run_backtest_v2.py`、`run_event_study_v2.py`;tests: test_e7_v2(E7 发射+F1 闸) | docs/v2-protocol.md | config/v2.json | `reports/event_study_v2.*`、`backtest_v2.*`、`trades_v2_*`、`equity_v2_*` |
| **v3** | `rounds/v3/run_backtest_v3.py`、`run_event_study_v3.py`(跨 TF 事件证据) | docs/v3-protocol.md | config/v2.json(沿用) | `reports/event_study_v3.*`、`backtest_v3.*`、`equity_v3_*` |
| **v4** | `rounds/v4/backtest_v4.py`(15m 执行帧引擎)、`run_backtest_v4.py`、`run_v4_counterfactual.py`(过期限价市价化反事实);tests: test_v4_broker_limit / test_v4_data / test_v4_engine | docs/v4-protocol.md | config/v4.json | `reports/backtest_v4.*`、`trades_v4_*`、`equity_v4_*`、`v4_counterfactual*` |
| **v5** | `rounds/v5/strategy_v5.py`(破位质量路由)、`backtest_v5.py`、`run_backtest_v5.py`;tests: test_v5_engine / test_v5_routing | docs/v5-protocol.md | config/v5.json | `reports/backtest_v5.*`、`trades_v5_*`、`equity_v5_*` |
| **v6(A/B 快照)** | `rounds/v6/strategy_v6.py`、`campaign.py`、`run_backtest_v6.py` —— **A/B 判选时代的双臂实现**(臂 A clamp2R / 臂 B turtle);tests: test_v6_strategy / test_v6_campaign(完整双臂套件,含 clamp2R 断言,对本归档实现运行) | docs/v6-protocol.md | config/v6.json | `reports/backtest_v6.*`、`trades_v6_{clamp2R,turtle}_*`、`equity_v6_{clamp2R,turtle}_*` |
| **external(PR#9/#10)** | `rounds/external/opportunity.py`(因果机会生命周期)、`market_areas.py`、`orders.py`(sqlite 执行账本)、`diagnose_trade_lifecycle.py`;tests: test_opportunity / test_market_areas / test_orders | PR#9/#10 设施(见 docs/research-log.md、reports/backtest_v6.md 共存声明) | — | `diagnostics/*`(诊断产物目录,不写 reports/) |

## 复现命令

```bash
pip install -e ".[dev]"

# 规范链(当前唯一语言;产物 backtest_v6_turtle.*)
python scripts/run_backtest_v6.py

# 历史轮次(覆盖各自同名历史报告;产物文件名见上表)
python research/rounds/v1/run_event_study.py
python research/rounds/v1/run_backtest.py
python research/rounds/v2/run_event_study_v2.py
python research/rounds/v2/run_backtest_v2.py
python research/rounds/v3/run_event_study_v3.py
python research/rounds/v3/run_backtest_v3.py
python research/rounds/v4/run_backtest_v4.py
python research/rounds/v4/run_v4_counterfactual.py
python research/rounds/v5/run_backtest_v5.py
python research/rounds/v6/run_backtest_v6.py      # v6 A/B 双臂(复现判选)
python research/rounds/external/diagnose_trade_lifecycle.py
```

## 规范链与归档的边界(cheat sheet)

- 规范策略 = `market_operator.strategy_v6`(turtle 唯一语义,**不可与归档
  双臂版互换**);规范引擎 = `market_operator.campaign`(turtle 接线);
  规范回放核心 = `market_operator.backtest_v6`(自 v1/v4/v5 逐字提升的
  共享原语);历史轮次引擎/策略一律在 `research/rounds/`。
- 若需研究 clamp2R:改 `research/rounds/v6/`,不改规范链;docs/v6-protocol.md
  的 A/B 协议原文与 reports/backtest_v6.* 冻结产物是判选的唯一权威记录。
