# ACCEPT：只保留顺趋势第一根强收盘确认（2026-11-03）

## 当前分支的唯一生产语义

配置 `D:/LiAn/Web3Code/market-operator-realtime/config/strategy.json`：

```json
"n_a": 3,
"fast_accept_atr": 0.5
```

- 仅 BalanceDetector 模式、第一根外侧收盘的候选可以快速确认。
- 多头必须为 `TREND_UP`，空头必须为 `TREND_DOWN`；这是代码硬条件，没有允许非趋势快速确认的配置入口。
- 当前收盘沿突破方向推进、有利侧收盘位置至少 65%、突破距离至少 0.5 ATR，且不超过既有 `accept_max_ext_atr`。
- 符合条件发出普通 `BREAKOUT_ACCEPTED`，附带 `fast=true`、`fast_bars=1`、`sub_kind=A3_FAST`。
- `RANGE/TRANSITION`、弱趋势首根、关闭快速阈值的候选继续普通 `n_a=3` 确认；普通路径既有的有效性条件不变。
- 第一根不够强时，第二根即使变强也不能走另一条快速路径；没有“两根快速确认”实现。
- `fast_accept_atr=null` 可回到普通确认；不会改变 REJECT、仓位、风险预算、目标和容量规则。

`EventDetector` 与 `MicTracker` 共用 `events.fast_accept_ok`。前者采用当前已收盘认知事实，后者采用已冻结高周期语境、在触发 bar 收盘裁决。**这是收盘入场提速，不是 5s 报价触发新开仓。**

## 必要回归测试

`D:/LiAn/Web3Code/market-operator-realtime/tests/test_fast_accept.py` 同时覆盖两个事件层：

- 多/空顺趋势第一根真实发射；
- RANGE、TRANSITION 多/空均等三根，普通 ACCEPT 不被封禁；
- 弱第一根和关闭阈值时等三根；
- 逆势、弱收盘、过伸、第二根、无有效 ATR 不快速确认；
- 正式配置为 0.5、普通确认仍为三根、白名单仍为 ACCEPT/REJECT。

```powershell
python -m pytest -q D:/LiAn/Web3Code/market-operator-realtime/tests/test_fast_accept.py
```

## 历史研究结果：不作为已上线或盈利保证

早期研究比较过无条件两根、强第二根、第一根不同阈值和非趋势快速确认。这些代码和研究脚本均不属于本次保留范围，已经移出分支；下列数字仅记录历史观察。

| 历史方案，104U | test 净额 1x / 1.5x | test DD 1x / 1.5x |
|---|---:|---:|
| 普通三根 | 51.36 / 34.14 | 12.46% / 18.13% |
| 第一根 0.50、全 regime | 55.92 / 35.74 | 14.93% / 20.36% |
| 第一根 0.75、全 regime | 48.07 / 29.63 | 14.41% / 18.44% |
| 第二根 0.50 | 49.85 / 30.04 | 12.74% / 19.71% |
| 无条件两根 | 49.03 / 29.30 | 13.72% / 21.71% |
| 第一根 0.50、仅趋势 | 62.25 / 40.14 | 13.28% / 17.61% |

历史趋势门控方案 full 净额 253.46 / 76.50，DD 13.72% / 21.08%；普通三根为 141.45 / 44.90、12.66% / 21.26%。1x DD 是略高而非“完全没有变差”。

这些早期脚本曾把默认 1h 装配标为 15m，且未完整冻结输入；数据反复用于调研，不是独立 OOS，也没有精确模拟实盘报价。不能用表中收益为当前生产组合背书。新的显式 15m 固定预算复核记录在：

`D:/LiAn/Web3Code/market-operator-realtime/docs/risk-budget-audit-20261103.md`。

## 本次明确不做

- 不实现两根快速确认、盘中试探仓或非趋势快速入口。
- 不加入普通 ACCEPT/REJECT 的 regime 黑名单。
- 不降低风险预算，不新增实盘同方向容量门。
- 不把趋势快速入场称为震荡 alpha；是否改善未来净收益仍须独立验证。
- 分支配置已选择该语义，但实盘切换仍按部署手册单独验收，不表示已重启上线。