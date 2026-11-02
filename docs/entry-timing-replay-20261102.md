# 细粒度执行回放与单事件提前验收（2026-11-02）

> Historical archive: the dedicated study scripts, experimental APIs, candidate configuration and raw generated artifacts described below were removed on 2026-11-02. Commands below are historical, NOT current runnable instructions. Read `session-cleanup-20261102.md` for retained code and `research-results-archive-20261102.md` for archived result tables. Production defaults were not changed by cleanup.


## 交付范围

- 删除上一轮独立影子研究入口、对应测试与方法文档；旧 reports 仅保留历史
  产物，不被生产或当前回放导入。
- 在规范 campaign/broker/scenario 链上显式支持 15m 执行，不新增第二套
  信号检测器。默认参数不变，生产仍走现有配置。
- 四臂：原 1h 执行、原触发+15m 执行、仅 2h/4h REJECT 改 15m 触发、
  仅 2h/4h ACCEPT 改 15m 触发。12h/1d 触发规则、风险与持仓管理不变。
- 提前臂沿用 MicTracker 的现有信号；没有引入影子算法的两根确认、局部
  中位数缓冲或新止损。HTF 事件仍作为原有管理和路由回退来源。

## 时序修复

1. `build_campaign_frame(..., execution_seconds=900)` 显式构造细网格；
   省略参数保持既有网格。混用不同执行网格直接报错。
2. 细网格模式将 trigger 事件安排在其真实收盘对应的执行细 bar 处理。
   不再把粗 trigger 的完整 OHLC 在其开盘标签处提前用于决策。
3. mic 市价与限价统一由细执行流撮合；`execution_not_before` 保证市场
   意图不能在信号实际收盘前成交。账本 decision_ts 仍保留原 bar 标签契约。
4. 细网格限价窗口用实际执行标签而非向下取整小时；Watch 等待继续以
   墙钟小时计，不将 4 小时误当成 4 根 15m。
5. 细网格在 mic 市价成交后立即检查本根保护止损；新限价成交同根触及
   防线按不利顺序处理。止损定价/风险公式未改。
6. 细网格盯市与资金费推进使用真实细 bar 收盘时间。原路径不改。
7. 单事件混合流保留未提前事件的原周期 OHLC/index，避免只改 REJECT
   却顺带改变 ACCEPT 的质量/几何输入。

## 完整回放

BTC/ETH/SOL/DOGE，2h/4h/12h/1d，300U 初始权益；正式配置的费用、
历史资金费、风险预算、容量和管理参数；不是实盘阶梯复利风险的额外实验。
dev/test/full 独立账户，不可相加。既有 test 已被多轮研究使用，不能称为
新的独立样本。本轮不据结果继续选参。

| 段/成本 | 原 1h 执行净收益 | 原触发+15m 执行 | 仅提前 REJECT | 仅提前 ACCEPT |
|---|---:|---:|---:|---:|
| dev/1x | 363.7918 | 320.3191 | 140.6841 | 343.8308 |
| dev/1.5x | 216.2612 | 205.9972 | -5.7309 | 203.9748 |
| test/1x | 159.0406 | 157.6895 | 124.1698 | 138.9855 |
| test/1.5x | 95.0150 | 101.1968 | 46.2533 | 76.8201 |
| full/1x | 715.7364 | 647.0151 | 324.9551 | 638.4656 |
| full/1.5x | 374.0988 | 354.3923 | 43.2987 | 317.6350 |

full/1x 最大回撤：原 1h 12.9220%，细执行基线 13.8993%，
提前 REJECT 15.7692%，提前 ACCEPT 20.4735%。
full/1.5x 最大回撤：21.6008%、24.7825%、25.6629%、29.9473%。

**两提前臂均未通过共同 15m 基线的逐场景/周期/事件/标的净收益下界。**
REJECT 明显退化；ACCEPT 仅 dev/1x 总收益改善，不支持全面上线。
原 1h 与 15m 差分还混合执行路径、时序修复、同根保护与资金费推进差异，
不能全部解释为原来存在前视或某单一 bug 的收益贡献。

保留原 HTF 回退意味着本轮检验“新增提前执行机会”，不是强制把每个 HTF
交易一一前移；其他周期收益也会因共享权益、容量与持仓机会改变。

## 生产决定

不引入影子服务或观察阶段；以本轮回放判据作决定。
本轮结果不支持上线，因此没有修改正式 trigger 配置、实盘入口或运行状态，
没有重启/发送订单。细执行能力留在规范回放中，默认关闭。
这不是“先等影子验证”，而是回放明确拒绝这两种提前方案。

## 复现

```text
python tools/replay_entry_timing.py
python -m pytest tests -q
```

结果在 `reports/entry_timing_replay_20261102/summary.json` 和 24 份逐笔
JSON 中；四臂 × 三段 × 两成本。重复执行会覆盖该研究报告目录。
验收脚本不部署、不读凭证、不发网络订单。

验证：497 passed、1 skipped。新增 7 个测试覆盖实际收盘成交护栏、
15m 限价窗口、Watch 墙钟到期、显式装配、粗信号收盘安排、默认网格
一致性和未提前事件的原始路由输入。默认 full/1x、test/1x 等输出与本轮
改动前已记录的工作区基线值一致；不是对所有历史提交的全路径等价证明。