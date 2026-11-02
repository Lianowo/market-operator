# 入场空间与目标管理解耦（2026-11-02）

> Historical archive: the dedicated study scripts, experimental APIs, candidate configuration and raw generated artifacts described below were removed on 2026-11-02. Commands below are historical, NOT current runnable instructions. Read `session-cleanup-20261102.md` for retained code and `research-results-archive-20261102.md` for archived result tables. Production defaults were not changed by cleanup.


## 当前契约

1. `deadzone_watch_r_lo/hi` 仅选择 Watch 路由区间，不能成为隐式全局准入阈值。
   显式配置 `min_box_rr` 或 `bounded_min_rr` 才启用最低剩余 R 限制。
   目标缺失/走过的检查继续保留，实际候选成交价复核继续保留。
2. `Hypothesis.target_1` 仍是结构空间/入场评估锚点；决策元数据显式包含
   `structural_target` 和 `execution_target`。后者才传入 PositionManager。
3. 重新入箱默认 `reentry_target_management=false`：即使开启
   `reentry_use_box_target`，结构目标也只作观察、空间评估和等待入场期间的
   作废校验，不会自动启动持仓锁利/减仓。显式开启目标管理才允许该联动。
   REJECT 和其他既有目标管理保持原语义。
4. `execution_target` 缺失时沿用旧目标，显式 None 时关闭目标管理。
   因此旧状态/旧意图保持兼容；不回溯修改已开仓位的管理方式。
5. 市价、Watch/限价成交的回测和实盘注册均使用同一目标选择函数。
   实盘意图持久化两个目标；positions.target_1 与 PM.target_1 均为执行目标，
   positions.structural_target 单独保留观察值。信号、成交备注与日志明确区分。

正式 strategy.json 未改动。独立候选配置启用结构止损和结构目标，
bounded_min_rr=0，reentry_target_management=false；不是默认上线配置。
没有重启实盘、修改已有实盘状态或提交任何订单。

## 对照回放

四核心标的/四周期、本地历史行情、300U 初始权益、正式风险/容量/费用配置。
原基线路由从 Git HEAD 加载，与新版本共享同一认知帧和执行引擎。
1.5x 为全成本压力。full/dev/test 是独立账户回放，不可相加。
原始结果：`reports/structure_entry_decoupled_20261102.json`。

| 场景 | 原基线净收益 | 解耦+原配置 | 重新入箱观察候选 | 同候选显式开启目标管理 |
|---|---:|---:|---:|---:|
| dev/1x | 340.1540 | 363.7918 | 383.2423 | 287.0001 |
| test/1x | 163.4918 | 159.0406 | 167.3877 | 137.1970 |
| full/1x | 680.7830 | 715.7364 | 747.3103 | 548.5105 |
| full/1.5x | 354.9018 | 374.0988 | 384.6088 | 272.9589 |

全段最大回撤：原基线 16.4994%，解耦原配置 12.9220%，观察候选 14.7344%。
1.5x 全段最大回撤：原基线 22.6068%，解耦原配置 21.6008%，观察候选 24.2023%。

解耦恢复了主要收益损失，但原配置 test 净收益略低于基线，不能宣布全品类
收益下界验收通过。观察候选净收益更好也不代表全面更优：高成本下回撤变差。
未通过正式生产验收，不自动部署，也不根据这些 test 结果继续选参。