# 当前未提交代码回放结果

## 执行范围

- 启动时间：2026-10-30 22:13:34 +08:00。
- 工作区：`D:\LiAn\Web3Code\market-operator`。
- HEAD：`49c066fed7bb2946d92529a6465380db41113a2d`，实际执行当前工作区的未提交源码，不是仅执行该提交。
- 配置：`D:\LiAn\Web3Code\market-operator\config\strategy.json`；历史资金费率，成本倍数 1.0，默认初始权益 300 USDT；不是 104 USDT 小账户阶梯复利场景。
- explore：BTCUSDT、ETHUSDT、SOLUSDT；book4：另加 DOGEUSDT。
- 每组覆盖 dev、test、full；认知周期为 2h、4h、12h、1d。
- 执行入口：`python -u D:\LiAn\Web3Code\market-operator\tools\replay_acceptance.py`。
- 使用本地历史数据，未抓取截至运行当天的新行情；manifest 中四标的 15m 文件各有 70400 行，validation_issues 均为空。

## 回放结果

| 场景 | 已平仓笔数 | 净收益 USDT | 相对冻结基线净收益变化 USDT | PF | 最大回撤 |
| --- | ---: | ---: | ---: | ---: | ---: |
| explore/dev | 346 | +212.2550 | +139.7936 | 1.70 | 10.38% |
| explore/test | 170 | +156.9426 | +59.6189 | 2.17 | 13.34% |
| explore/full | 517 | +481.8055 | +317.3574 | 1.89 | 13.89% |
| book4/dev | 443 | +340.1540 | +247.1179 | 1.74 | 7.82% |
| book4/test | 226 | +163.4918 | +130.0714 | 2.00 | 16.57% |
| book4/full | 669 | +680.7830 | +547.8213 | 1.84 | 16.50% |

净收益是工具统计的已平仓交易 net_pnl 之和；PF 使用毛盈利/毛亏损口径。full 为独立回放，不应将 dev、test 收益相加代替 full。六组逐笔摘要均与历史冻结基线不同。

### book4/full 按周期

| 周期 | 已平仓笔数 | 净收益 USDT |
| --- | ---: | ---: |
| 2h | 305 | +170.1798 |
| 4h | 194 | +112.4055 |
| 12h | 117 | +272.0297 |
| 1d | 53 | +126.1681 |

## 验收边界

六组回放均执行完成，stderr 为空，六组总净收益均高于历史冻结基线；但有以下八项细分类别低于基线：

| 场景/类别 | 当前净收益 USDT | 基线净收益 USDT |
| --- | ---: | ---: |
| explore/dev/2h | +26.0615 | +51.3836 |
| explore/dev/BREAKOUT_ACCEPTED | +62.6642 | +80.7179 |
| explore/dev/ETHUSDT | +83.5533 | +114.0454 |
| explore/test/4h | +16.5839 | +79.1938 |
| explore/full/4h | +59.1932 | +115.8707 |
| book4/dev/2h | +33.5091 | +52.9035 |
| book4/dev/BTCUSDT | -20.7075 | -8.9621 |
| book4/test/4h | +13.4461 | +47.5324 |

工具最终输出为“模型变更”，不是 `[PASS]`：当前配置 funding_source=history，工具会豁免这些收益下界差异。因此不能宣称严格的“所有品类不低于基线”验收通过，也不能仅凭这一结果断定差异由资金费口径造成。

本轮没有执行 `verify_strategy_parity.py --ref 49c066f` 的历史实现/当前实现精确对照，故不证明未提交重构与参考提交的逐笔、完整净值及计数器等价。

## 附加验证及产物

- 执行 test_campaign_contracts.py、test_refactor_compatibility.py、test_strategy_parity.py、test_layering.py、test_v21_balance_trigger.py：**40 passed in 7.94s**，stderr 为空。这不是全量测试结果。
- `git diff --check` 通过。
- 最终工作区状态与运行前相比仅新增本次报告目录；未修改策略源码、配置、冻结基线，也未启动实盘。
- 本目录保留 replay.stdout.log、replay.stderr.log、pytest.stdout.log、pytest.stderr.log、运行元数据、前后 Git 状态及已跟踪文件的工作区 diff。
- working_tree.patch.gz 不包含未跟踪源码文件内容；Git 状态清单保留其文件名，因此这些产物并非完整可独立重建的工作区快照。

Pre-commit validation: full pytest suite: 440 passed, 1 skipped in 20.40s. Working-tree patch snapshot is losslessly gzip-compressed.
