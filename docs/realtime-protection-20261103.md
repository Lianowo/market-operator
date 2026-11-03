# 生产分支范围：实时价格保护与趋势快速确认（2026-11-03）

## 只保留的行为变化

1. **15m 已收盘价格保护 + 每轮新鲜报价推进 + 重启安全。**
2. **顺趋势第一根强收盘允许快速 ACCEPT；非趋势普通三根确认不变。**

风险配比、杠杆、复利阶梯、周期/组合容量、REJECT 语义、目标、白名单均保持实盘基线。不新增实盘同方向闸门或普通 ACCEPT 的 regime 黑名单，不保留研究脚本和实验开关。

## 价格保护时钟

`D:/LiAn/Web3Code/market-operator-realtime/config/live.json`：

```json
"loop": {"poll_seconds": 5},
"protection": {
  "price_clock": "15m",
  "max_quote_age_seconds": 30
}
```

- 15m 已收盘 bar 调用与既有持仓管理同源的 `price_only` 路径，授予/推进目标锁利资格。
- 已武装后，每轮名义 5s 的新鲜报价推进峰值与防线，不能靠报价自行授予收盘型锁利资格。
- 非新鲜、重复、建仓前的报价不推进；没有权威成交价的 `price_source=ref` 先保持固定保护，待成交回执/对账校正。
- 防线只紧不松。更新保护单仍先挂新 TPSL，确认 orderId 后撤旧单；交易所固定止损继续兜底。
- 回吐预算耗尽由本地市价执行；15m 收盘裁决越过结构防线也是本地决策，不冒充交易所挂单触价。
- 旧 2H 的目标管理块在 15m 模式不重复执行，固定止损检查仍保留；结构 pivot 的确认时钟不在本次改动范围。
- `price_clock=bar` 保留旧保护时钟用于回退，不改变快速 ACCEPT 或风险预算。

## 重启安全

- 时钟初始化、长停顿恢复时从最近已收盘位置重新锚定，不从多天前的建仓时刻翻页爬取价格。
- 短缺口最多按 4 个 15m 步长处理；更长的结构补做仍由原有 gap recovery 负责，不补开历史仓。
- 新仓登记、仓位清除时移除旧 symbol 时钟锚，避免上一笔污染下一笔。
- `_best`、`invalid`、`armed` 沿用既有持久化状态。重启不会为了填满价格历史而产生过期离场。

## 趋势快速 ACCEPT

`D:/LiAn/Web3Code/market-operator-realtime/config/strategy.json` 仅新增 `fast_accept_atr=0.5`，`n_a=3` 保持不变。

快速路径只接受方向匹配的趋势和第一根强收盘；RANGE/TRANSITION、弱首根继续普通三根确认，无两根快速或非趋势快速入口。两个事件层共用同一判断；报价轮询不触发开仓。

详情与历史研究界限：`D:/LiAn/Web3Code/market-operator-realtime/docs/accept-fast-confirm-20261103.md`。

## 研究与部署边界

- 更快保护解决执行滞后，但可能提前结束后来能盈利的仓位，不承诺收益/回撤必然改善。
- 15m OHLC 不等于实盘 5s 报价；轮询间极值、API 延迟与限流仍可能存在，5s 是配置目标而非硬实时保证。
- 旧研究曾混淆 1h/15m 执行网格；历史数字不作为本分支组合盈利的精确证明。
- 风险实验已撤销：`D:/LiAn/Web3Code/market-operator-realtime/docs/risk-budget-audit-20261103.md`。
- 震荡实验未通过：`D:/LiAn/Web3Code/market-operator-realtime/docs/range-policy-20261103.md`。
- 分支整理不等于实盘部署。本次未重启、下单或改动实盘工作区；部署按 `D:/LiAn/Web3Code/market-operator-realtime/docs/realtime-protection-deploy-20261103.md` 单独执行。