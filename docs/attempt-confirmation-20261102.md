# 价格尝试失败确认（2026-11-02）

> Historical archive: the dedicated study scripts, experimental APIs, candidate configuration and raw generated artifacts described below were removed on 2026-11-02. Commands below are historical, NOT current runnable instructions. Read `session-cleanup-20261102.md` for retained code and `research-results-archive-20261102.md` for archived result tables. Production defaults were not changed by cleanup.


## 决策定义（非收益保证）

固定根数不应自动授予入场资格。ACCEPT 原确认由连续外侧收盘计数承担；
REJECT 的 n_f 主要是观察期限，不能简单声称 REJECT 总要等满 n_f 根。

本轮在原 MicTracker 内增加显式 `confirmation_mode=attempt_failure`：

- 仅已确认 BOX_EDGE、从正面以收盘进入区域才建立接触。
- ACCEPT：外侧推进 → 至少已有 k_rj×HTF_ATR 的回撤 → 未收回内沿之外
  k_bm×HTF_ATR → 收盘重新越过之前推进点及缓冲。直线突破不因等待而获准。
- REJECT：试探 → 回到内侧形成反应点 → 再向外试探 → 未恢复原收盘极值
  附近的推进 → 收盘越过反应点及缓冲。一次反弹本身不算拒绝。
- 每次调用最多完成一个阶段转移，阶段有先后，但不要求固定持续根数。
- 阈值在接触时冻结，沿用 k_bm、k_rj 的 HTF ATR 尺度，不用 sqrt(scale)
  缩小退让要求。无新增数值搜索。极值突破失败则恢复观察，不发交易资格。
- 完成后必须先离开前方 k_rj×ATR 才重新武装；区域消失/边界变化取消尝试。
  没有根数超时；候选可跨认知收盘，直到价格/结构失效。
- 用不同 bar 的收盘描述路径，OHLC 高低不用于倒推阶段顺序；REJECT
  结构止损仍保留接触以来观测到的真实 wick 极值与原缓冲，而非收盘极值。
- 风险预算、路由质量过滤、止损公式与退出管理未修改。实际止损位置和
  交易集合会因事件不同而不同，不能说每笔止损价完全不变。

## 实现边界

新定义只是现有 MicTracker 的可选确认方式，无影子服务/独立信号模块。
生产默认 `bars`，正式配置未改，实盘未重启、未下单。可选模式目前完整替换
该 tracker 的事件输出为 ACCEPT/REJECT；本轮装配仅挑选目标事件族，其他
事件沿原流保留。不是可直接全局启用的完整高阶事件替代品。

HTF 认知不变，原 HTF 路由回退保留。故此对照是“加入结构式提前机会”，
而非全系统清除所有固定根数，也不是原交易逐笔前移。仍要等细 bar 收盘，
不声称实现逐笔即时执行或找到最佳价。形态阶段也可能增加等待。

## 回放（四标的四周期、300U、正式风险/成本，24 场景）

| 段/成本 | 原触发+15m 执行 | 仅结构式 REJECT | 仅结构式 ACCEPT |
|---|---:|---:|---:|
| dev/1x | 320.3191 | 260.4898 | 273.6463 |
| dev/1.5x | 205.9972 | 116.4113 | 153.6607 |
| test/1x | 157.6895 | 92.5210 | 129.9382 |
| test/1.5x | 101.1968 | 22.3137 | 48.3446 |
| full/1x | 647.0151 | 434.3864 | 520.6998 |
| full/1.5x | 354.3923 | 147.7152 | 224.5630 |

full/1x 最大回撤：13.8993% / 18.3277% / 27.8332%。
full/1.5x 最大回撤：24.7825% / 29.8937% / 38.4493%。
两臂均未通过总收益及逐品类下界，不上线。结果证明的是该具体定义与
原回退组合未改善，不是证明一切结构确认无效。不能仅凭交易增多便断言
亏损根因是噪音；未完成同机会反事实归因，也未证明入场价整体改善。
本轮没有按结果继续改参数。既有 test 不是新的独立样本。

```text
python tools/replay_entry_timing.py --confirmation attempt_failure
python -m pytest tests -q
```

产物：`reports/attempt_confirmation_replay_20261102/summary.json` 与逐笔文件。
13 个新增测试：方向镜像、固定根数不授予资格、再试成功不误发拒绝、禁止
同根路径推断、重复接触锁、真实 wick 止损、未来前缀不变、语境取消与认知
事件流等价。全套 510 passed、1 skipped。