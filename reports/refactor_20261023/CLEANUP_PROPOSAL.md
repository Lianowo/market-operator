# v21 分支代码收敛提案与执行记录

生成时间:2026-10-24(合并 `main` → `refactor/domain-model` 之后)
执行时间:2026-10-24(标签 `pre-cleanup-20261024` 之后)
方法:`src/market_operator/*.py` 逐模块统计 importer;`scripts/*.py` 逐脚本统计
`src / scripts / tests / tools / docs / config / README` 内的字面引用(排除脚本自身、
`data/` `runtime/` `.git/`)。判定口径:**没有任何 importer 且没有任何文档/命令引用**
才算可删;只在 `reports/` 的历史产物里出现过的不算引用(那是归档,不是入口)。

## A. 已归档(零 importer、零文档引用;落在 `scripts/archive/`)

| 文件 | 类别 | 说明 |
|---|---|---|
| `scripts/_diag_losers.py` | 一次性诊断 | 早期 winners/losers 对照,已被 `_diag_v21_p012.py` 的 tail/counterfactual 指标覆盖 |
| `scripts/_diag_winners_losers.py` | 一次性诊断 | 同上,重复 |
| `scripts/_diag_opp_lock_ab.py` | 一次性诊断 | 对侧锁利 A/B;结论已固化进 `docs/v21-protocol.md` §5(锁利价双闸门)与 `position.py` 用例 |
| `scripts/_diag_tf_quality.py` | 一次性诊断 | 周期质量筛查,结论见 `reports/tf_quality_20261023/`。**与 `_diag_v21_factor_mine.py` 成对**(它 `import _diag_v21_factor_mine as fm`) |
| `scripts/_diag_v21_factor_mine.py` | 一次性统计 | 因子挖掘,产物见 `reports/factor_mine_20261023/`;除 `_diag_tf_quality.py` 外无 importer |
| `scripts/_diag_zone_accuracy.py` | 一次性诊断 | ZoneBook 命中率,结论见 v21.1–v21.2 章节 |
| `scripts/_diag_v21_f34_stats.py` | 一次性统计 | F3/F4 统计产物已落 `reports/p012_20261023/F34_STATS.md` |
| `scripts/_diag_v21_f5_neighborhood.py` | 一次性统计 | 同上,`F5_NEIGHBORHOOD.md` |
| `scripts/_diag_v21_funding_replay.py` | 一次性诊断 | 资金费口径已被 `scenario.load_funding_rates` + `p012` 的 `base_const/base_hist` 双臂取代 |
| `scripts/analyze_deep_trade_anatomy.py` | 一次性分析 | v7 时代产物,无引用 |
| `scripts/plan_watch.py` | 遗留工具 | 「挂单观察」在 `live_status.py` / `storage_gc.py` 上线后失去入口 |
| `scripts/diagnose_current_btc.py` | 遗留工具 | 单标的即时诊断;线上已由 `live_status.py` 取代(`tape_features.py` 另有 `tests/test_layering.py` 引用,故**保留**) |
| `scripts/run_event_study_v9.py` | 遗留入口 | v9 事件研究,已被 v21 `trigger.py` 路线取代 |
| `scripts/run_quattro.py` | 遗留入口 | 四原语原型;`quattro.py` 仍被 `tests/test_quattro.py` 覆盖,但该脚本无入口引用 |
| `scripts/run_aggtrades_study.py` | 遗留入口 | 一次性逐笔研究入口;其依赖的 `flow_features.py` / `flow_study.py` 仍由 `tests/test_flow.py` 覆盖,脚本本身无入口引用 |

合计 **15 个脚本 + `tape_features.py` 模块**,已 `git mv` 到 `scripts/archive/`(不 `git rm`,
历史可检索),并附 `scripts/archive/README.md` 说明退役原因与复跑方式。归档后全量测试:
570 → **574 passed, 1 skipped**。

## A′. 核验与改判(原提案有两处误判,记录以免再犯)

| 模块 | 原判定 | 实际 | 处理 |
|---|---|---|---|
| `src/market_operator/split.py` | 「无导入点」 | `campaign.py:74` 与 `replay.py:36` 都 `from .split import split_boundary`;**相对导入**被原检索漏检,而 `split_boundary` 是 dev/test 判分契约 | **保留**(删了整条回放/战役链就断) |
| `src/market_operator/tape_features.py` | 「只被 `diagnose_current_btc.py` 使用 ⇒ 可连带归档」,后又因 `tests/test_layering.py` 白名单改判保留 | 归档 importer 后确实零引用;而同一测试的 `test_retired_list_has_no_orphans` 要求「退役清单不得有孤儿」⇒ 保留在清单里反而失败 | **随 importer 一起归档**,并从 `RETIRED` 移除(README / v10-protocol 已加注) |

教训:检索「零引用」必须同时覆盖 `from .x import`、`import x as y` 与测试白名单,
不能只搜 `market_operator.x` 字面量;而「测试里提过名字」不等于「有引用方」——
退役清单本身是**待删除队列**,不是保留理由。


## B. 明确保留(有 importer / 被当门禁引用)

- **canonical 基线链**:`scripts/run_backtest_v6.py`(27 处引用,`canonical.json`
  与 CI 门禁依赖)、`freeze_canonical_baseline.py`、`fetch_*`。
- **v8–v20 研究入口**:`run_backtest_v8/v10/v13`、`run_v14_arms`、`run_v15`…`run_v20`
  —— 均被后续版本脚本或 `tests/test_v1x_*.py` 导入,构成历史可比性。
- **v21 诊断四件套**:`_diag_v21_abcd.py`(被 `_diag_v21_p012.py`、`tfsolo`、`tf_opt`、
  `tf_sweep` 导入)、`_diag_v21_backtest.py`(README + 协议文档的可复现命令)、
  `_diag_v21_p012.py`、`_diag_v21_tfsolo.py`、`_diag_v21_tf_opt.py`。**删除 `abcd`
  会连带打断另外四个入口**,统一留到 v21 报告冻结之后再评估。
- **线上运维面**:`live_runner.py`、`live_status.py`、`live_supervisor.py`、
  `storage_gc.py`、`live_paper_check.py`、`live_gap_preview.py`、`live_cost_audit.py`、
  `tape_recorder.py` —— 全都有测试或计划任务/文档入口。

## C. 合并 `main` 后的结构性事实(不作为删除项,但要记录)

1. `main` 的完整 `live_runner.py` 依赖 `strategy_v6.V6Router` + `routing.py` 的
   `TieredParams`,本分支两者都没有 ⇒ **不采用整文件替换**,改为功能级移植:
   心跳文档/看护线程/路径归属/锁利双闸门已进 `scripts/live_runner.py`;
   `main` 的 campaign/lifecycle 差异留在 `main`,等 v21 路由层出现同名接缝再谈。
2. `src/market_operator/heartbeat.py`、`scripts/live_status.py`、`scripts/storage_gc.py`
   来自 `main`,与 v21 无冲突,已保留并有测试(`tests/test_live_supervision.py`、
   `tests/test_live_status.py`、`tests/test_storage_gc.py`)。
3. 本分支 `broker.floor_step` 即 `main` 的 `_floor_step`(兼容别名已存在),
   因此 `heartbeat.ensure_protection()` 的 `from ... import _floor_step` 可用。

## D. 执行记录(已完成)

1. 标签:`git tag pre-cleanup-20261024`(打在本轮任何改动之前,回滚锚点)。
2. 15 个脚本一次性 `git mv` 到 `scripts/archive/` + `scripts/archive/README.md`。
3. 归档后重跑全量:`python -m pytest -q -p no:cacheprovider` ⇒ **574 passed, 1 skipped**
   (其中 +4 条是本轮新增的 `price_only` 用例)。`tests/` 内无任何 import 归档脚本
   的用例(归档前已逐名核验)。
4. 核验发现原提案两处误判(`split.py`、`tape_features.py`),**均予保留**,见 A′。
5. `docs/v21-protocol.md` §7 可复现命令清单核对:无任何条目引用被归档脚本,无需删改。

## E. 仍可考虑、但本轮不动

- `_diag_v21_abcd.py` 仍被 `_diag_v21_p012.py` / `tfsolo` / `tf_opt` / `tf_sweep` 四个
  入口导入(删它会一次打断四条研究链)。等 v21 报告冻结、把可复现命令改为引用
  `reports/` 产物之后再评估。
- `src/market_operator/research.py`(554 行)是 v16 之前的研究方法学,生产闭包已
  经不再经它(判分契约上收到 `split.py`)。但它被 `scripts/run_backtest_v10/v13/v6`、
  `run_zonebio`、`run_v17`、`run_flow_study` 与 4 个测试模块导入,属「历史可比性」
  保留面 —— 要动必须连整条 v8–v20 复现链一起处理,不在本次收敛范围。

