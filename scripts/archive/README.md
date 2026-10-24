# scripts/archive —— 已退役的一次性研究脚本(只读归档)

归档时间:2026-10-24(v21 分支收敛,标签 `pre-cleanup-20261024`)

这里的文件**不是可执行入口**,不再维护、不再进测试门禁。判定口径(归档前核验):
在 `src/ scripts/ tests/ tools/ docs/ config/ README` 内**零 importer、零文档命令引用**;
只在 `reports/` 的历史产物里出现过的不算引用。

| 文件 | 原用途 | 结论落在哪 |
|---|---|---|
| `_diag_losers.py` / `_diag_winners_losers.py` | 早期赢家/输家对照 | 已被 `_diag_v21_p012.py::tail_stats` 的 MFE/MAE/give-back 指标覆盖 |
| `_diag_opp_lock_ab.py` | 对侧锁利 A/B | `docs/v21-protocol.md` §5 + `position.py` 双闸门用例 |
| `_diag_tf_quality.py` + `_diag_v21_factor_mine.py` | 周期质量筛查 / 因子挖掘(前者 import 后者,须成对归档) | `reports/tf_quality_20261023/`、`reports/factor_mine_20261023/` |
| `_diag_zone_accuracy.py` | ZoneBook 命中率 | v21.1–v21.2 章节 |
| `_diag_v21_f34_stats.py` / `_diag_v21_f5_neighborhood.py` | F3/F4/F5 统计 | `reports/p012_20261023/F34_STATS.md`、`F5_NEIGHBORHOOD.md` |
| `_diag_v21_funding_replay.py` | 资金费口径 | 被 `scenario.load_funding_rates` + `p012` 的 `base_const/base_hist` 双臂取代 |
| `analyze_deep_trade_anatomy.py` | v7 交易解剖 | 无后继(结论已进协议) |
| `plan_watch.py` | 挂单观察 | `live_status.py` / `storage_gc.py` |
| `diagnose_current_btc.py` | 单标的即时诊断 | `live_status.py` |
| `run_event_study_v9.py` | v9 事件研究 | v21 `trigger.py` 路线 |
| `run_quattro.py` | 四原语原型 | `quattro.py` 本体仍保留并有 `tests/test_quattro.py` |
| `run_aggtrades_study.py` | 逐笔研究入口 | `flow_features.py` / `flow_study.py` 仍保留并有 `tests/test_flow.py` |
| `tape_features.py`(原 `src/market_operator/`) | 只读注解层 | 唯一 importer 是 `diagnose_current_btc.py`;归档后零引用 ⇒ 按 `tests/test_layering.py` 的「退役清单不得有孤儿」规则一并归档 |

## 使用注意

1. **路径语义已失效**:这些脚本按 `Path(__file__).resolve().parents[1]` 找 `ROOT`,
   归档后 `parents[1]` = `scripts/`,相对路径全错。要复跑请从 git 历史取回原位置:
   `git show pre-cleanup-20261024:scripts/<name> > scripts/<name>`。
2. **不要在这里新增文件**。真需要重开的实验,应作为新的 `_diag_v21_*.py` 写回
   `scripts/` 并给出与 `reports/` 产物的对照口径。
3. 归档判定里被**否决/改判**的三项(说明核验有效,勿再当死代码处理):
   - `src/market_operator/split.py`:**生产契约**,`campaign.py` / `replay.py` 都
     `from .split import split_boundary`(原提案漏检相对导入)⇒ **保留**。
   - `src/market_operator/tape_features.py`:原提案说「可连带归档」,但 `tests/test_layering.py`
     的退役白名单引用了它 ⇒ 一度改判保留;归档 `diagnose_current_btc.py` 之后它变成
     零 importer,而同一个测试(`test_retired_list_has_no_orphans`)明确要求「退役清单
     不得有孤儿」⇒ 最终**随 importer 一起归档**,并从白名单移除。
