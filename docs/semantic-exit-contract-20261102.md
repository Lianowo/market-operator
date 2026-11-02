# 资格、保护与成交分层（2026-11-02）

> Historical archive: the dedicated study scripts, experimental APIs, candidate configuration and raw generated artifacts described below were removed on 2026-11-02. Commands below are historical, NOT current runnable instructions. Read `session-cleanup-20261102.md` for retained code and `research-results-archive-20261102.md` for archived result tables. Production defaults were not changed by cleanup.


本轮是行为兼容的函数拆分，不改变生产观察周期，不添加新策略规则。

验证：全量测试 526 passed、1 skipped；`git diff --check` 通过。
canonical replay 重跑 24 场景，24 组 summary 与全部 24 份逐笔 JSON
均与拆分前一致。独立产物在 `reports/exit_contract_refactor_20261102/`，
未覆盖原离场实验。这验证兼容性，不代表任何提前策略已获验收。

## 已实现的边界

- `opp_lock_arm_hit`：观察事实是否满足首次锁利资格。负 frac 继续要求调用方
  原观察 bar 收盘越过目标，未改为盘中触价。
- `opp_lock_protection_step`：仅消费 armed、方向、已知峰值、价格单位预算和
  当前参考价。不接收目标、bar 或确认周期，不能自行授予资格。
- `opp_lock_step`：保留原接口、参数域检查、预算公式，组合以上两步。
- `Position.armed`：沿用现有单调状态；没有新增持久化字段或迁移。

例如多头峰值 112，预算 7.5，既定保护价为 104.5。已有资格时，参考价
109 返回 tighten，104 返回 exit；不需要再次收盘越过目标。未获资格时
相同数据返回 hold。参考价更新不意味着峰值更新。

这里返回 tighten 是保护价建议，不保证比现有防线更紧；只紧不松仍由原
持仓/订单调用方保证。返回 exit 是决策，不是已提交、已确认或已成交。
低层函数要求调用方提供已校验的预算；原兼容入口保留原参数域逻辑。

## 尚未实现，不能据此宣称完成

- 未增加资格证据时间、保护决策时间、订单确认时间、成交时间的持久审计。
- 未接入逐笔或更快价格源，未改变已有峰值观察周期。
- 未证明实时下单延迟改善或回测收益改善。
- 未完成同一机会入场的后续证伪与逐笔收益归因。

## 下一阶段的语义契约（设计，不是已部署规则）

每种交易需先写出：主张、作用区域、已知证据、生效条件、证伪条件、执行
前提。不能只用事件名代替证据，也不能用未来极值解释过去可入场。

分开记录候选、证据成立、订单准入、委托、成交、持有、退出决策、退出
成交；锁利资格是持有状态下的独立标记，不等于交易假设被证伪。

对突破，“越过边界”只描述位置；要把“接受”操作化，必须事先定义可观察
的守住/回测/反向尝试失败路径。对拒绝，“出现极值”不够，需定义穿越尝试
以及失败后的返回事实。此处不指定新阈值或形态，避免重演已失败实验。

必须区分：
1. 证伪退出：入场主张已不成立。
2. 风险退出：保护线触发，即使主张未来可能恢复也先执行。
3. 机会完成/持有许可终止：目标或收益回吐契约要求结束，不等价于反向信号。

同一数据前缀应产生同一状态；改变喂入分块不应无意改变语义。如果定义
明确要求某类收盘，分块并不等于该收盘，不能宣称完全周期无关。缺失数据
应标为证据不足，不能当作守住或失败。细网格 OHLC 不能证明根内先后顺序。

后续 canonical replay 应先验证旧路径等价，再分别评价证据定义变化、峰值
更新变化和固定保护的执行响应，不将三者合并成一个“更快”实验。