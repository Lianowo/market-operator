# 实盘成本对账(Bitget,只读)

- 窗口:最近 30 天;成交 59 笔;流水 137 条(其中资金费 77 条)
- 配置假设:`{"taker_fee": 0.0006, "maker_fee": 0.0002, "slippage_per_side": 0.0002, "funding_per_8h_per_side": 0.0001}`

## 结论

- 实测综合手续费 **5.95 bps/边**(配置 taker 6.00 + slip 2.00 = 8.00 bps)
- 交易所费率档(contracts 端点):taker [6.0] bps / maker [2.0] bps
- 实测资金费合计 **-0.024425 USDT**;折合 **-0.048 bps/8h**(配置 1.00 bps/8h,恒为成本)
- 执行漂移:实盘当前宇宙中位 **+18.71 bps**;回测标的抽样中位 +3.33 bps(配置滑点 2.00 bps/边)
- 按同一成交额,模型要收 fee+slip 3.2786 USDT、资金费 +0.512647 USDT;实付 fee 2.4387、资金费 -0.0244 USDT

## 手续费(按 tradeScope)

| scope | n | 中位 bps | 最小 | 最大 |
|---|---|---|---|---|
| maker | 3 | 2.00 | 2.00 | 2.00 |
| taker | 56 | 6.00 | 6.00 | 6.00 |

## 资金费率(交易所公开历史,近 30 天)

| 项 | 值 |
|---|---|
| 覆盖 | 3 个合约 / 270 个 8h 结算点 |
| 均值(有符号) | +0.56 bps/8h |
| 均值(绝对值) | 0.62 bps/8h |
| 正值占比(多头付费) | 87.0% |
| p10 / p50 / p90 | -0.07 / +0.65 / +1.00 bps |

配置假设 1.00 bps/8h **恒为成本**;
实际:多头在正费率时付费、负费率时收费,空头反之。

## 执行漂移(入场后第 1 分钟,按方向调整;正 = 不利)

| 组 | n | 均值 bps | 中位 bps | p10 | p90 |
|---|---|---|---|---|---|
| 实盘当前宇宙(山寨) | 21 | +17.28 | +18.71 | -24.88 | +64.04 |
| 回测标的 BTC/ETH/SOL/DOGE | 60 | +2.97 | +3.33 | -4.71 | +13.49 |

> 实盘组 = 市价成交 VWAP vs 成交所在 1m bar 开盘(下单在 bar 边界后约 10s);回测组 = 入场 1m bar 的 close vs open 抽样 60 笔。配置假设 2.00 bps/边。

## 账本敏感性:资金费若按实测/0 计,净 E[R] 变化

| TF | N | 毛E[R] | 费+滑 | 资金费 | 净E[R] | 扣掉资金费后 | 名义/R | 隐含 bps/8h |
|---|---|---|---|---|---|---|---|---|
| 2h | 481 | +0.1602 | -0.0880 | -0.0121 | +0.0821 | +0.0700 | 55.0× | 1.003 |
| 4h | 256 | +0.3457 | -0.0635 | -0.0195 | +0.2786 | +0.2590 | 39.6× | 1.009 |
| 12h | 309 | +0.0578 | -0.0408 | -0.0343 | -0.0071 | -0.0414 | 25.4× | 1.011 |
| 1d | 107 | +0.2282 | -0.0312 | -0.0566 | +0.1483 | +0.0917 | 19.5× | 0.990 |

> 隐含 bps/8h = Σ(资金费/R) ÷ Σ(名义/R × 持仓h/8);等于 1.000 说明账本完全按配置的恒定额计提。

## 实盘资金费(按合约,窗口内实测)

| symbol | 资金费(USDT) | 仓位名义(USDT) | 持仓h | bps/8h |
|---|---|---|---|---|
| 1000CATUSDT | -0.008287 | 5.00 | 57.42 | -2.31 |
| AAVEUSDT | +0.010946 | 170.20 | 10.41 | +0.49 |
| ADAUSDT | -0.015322 | 155.34 | 6.11 | -1.29 |
| AMZNUSDT | -0.005376 | 154.70 | 30.57 | -0.09 |
| AMZUUSDT | +0.000000 | 6.60 | 14.19 | +0.00 |
| ARBUSDT | +0.018406 | 60.50 | 32.59 | +0.75 |
| ARIAUSDT | +0.115235 | 32.89 | 34.63 | +8.09 |
| ARKMUSDT | +0.000000 | 36.32 | 4.32 | +0.00 |
| ARMUSDT | +0.000000 | 70.63 | 4.32 | +0.00 |
| BABAUSDT | +0.000000 | 186.95 | 5.36 | +0.00 |
| BLURUSDT | -0.078900 | 6.17 | 22.71 | -45.08 |
| CLUSDT | -0.044175 | 91.89 | 34.63 | -1.11 |
| CSOPSK2LHKDUSDT | +0.000000 | 29.66 | 4.32 | +0.00 |
| GOOGLUSDT | +0.000000 | 518.59 | 2.32 | +0.00 |
| GUSDT | +0.004000 | 40.23 | 8.89 | +0.90 |
| JCTUSDT | -0.007294 | 17.65 | 34.63 | -0.95 |
| MAGMAUSDT | +0.000000 | 21.02 | 0.31 | +0.00 |
| MINIMAXUSDT | -0.023675 | 106.33 | 17.61 | -1.01 |
| MOONSHOTUSDT | +0.000000 | 50.15 | 2.31 | +0.00 |
| PAXGUSDT | +0.080557 | 573.54 | 30.56 | +0.37 |
| TIAUSDT | +0.013035 | 61.18 | 26.61 | +0.64 |
| UNIUSDT | +0.007413 | 58.04 | 26.61 | +0.38 |
| WUSDT | -0.090988 | 48.56 | 34.63 | -4.33 |

## 逐笔成交

| cTime | symbol | scope | side/tradeSide | 名义(USDT) | fee | bps |
|---|---|---|---|---|---|---|
| 10-04 14:46Z | GUSDT | maker | sell/open | 40.23 | 0.008045 | 2.00 |
| 10-04 23:39Z | GUSDT | taker | sell/close | 40.11 | 0.024066 | 6.00 |
| 10-10 08:50Z | 1000CATUSDT | maker | sell/open | 5.00 | 0.001000 | 2.00 |
| 10-12 13:15Z | 1000CATUSDT | taker | sell/close | 2.41 | 0.001445 | 6.00 |
| 10-12 17:31Z | AMZUUSDT | taker | sell/open | 6.60 | 0.003961 | 6.00 |
| 10-12 18:15Z | 1000CATUSDT | taker | sell/close | 2.37 | 0.001425 | 6.00 |
| 10-13 07:42Z | AMZUUSDT | taker | sell/close | 6.55 | 0.003932 | 6.00 |
| 10-13 16:05Z | BABAUSDT | maker | buy/open | 5.54 | 0.001108 | 2.00 |
| 10-13 16:30Z | BABAUSDT | taker | buy/close | 5.52 | 0.003314 | 6.00 |
| 10-14 10:11Z | BLURUSDT | taker | sell/open | 6.17 | 0.003700 | 6.00 |
| 10-15 08:54Z | BLURUSDT | taker | sell/close | 6.17 | 0.003703 | 6.00 |
| 10-21 20:00Z | AAVEUSDT | taker | buy/open | 110.37 | 0.066221 | 6.00 |
| 10-21 20:00Z | ADAUSDT | taker | buy/open | 114.05 | 0.068430 | 6.00 |
| 10-21 20:00Z | ADAUSDT | taker | buy/open | 41.29 | 0.024772 | 6.00 |
| 10-21 20:00Z | ARIAUSDT | taker | sell/open | 32.89 | 0.019736 | 6.00 |
| 10-21 20:00Z | CLUSDT | taker | sell/open | 91.89 | 0.055133 | 6.00 |
| 10-21 20:00Z | JCTUSDT | taker | buy/open | 17.65 | 0.010589 | 6.00 |
| 10-21 20:00Z | WUSDT | taker | sell/open | 48.56 | 0.029134 | 6.00 |
| 10-21 22:02Z | ARBUSDT | taker | sell/open | 60.50 | 0.036297 | 6.00 |
| 10-22 00:04Z | AMZNUSDT | taker | sell/open | 73.41 | 0.044048 | 6.00 |
| 10-22 00:04Z | AMZNUSDT | taker | sell/open | 81.28 | 0.048769 | 6.00 |
| 10-22 00:04Z | PAXGUSDT | taker | sell/open | 437.37 | 0.262424 | 6.00 |
| 10-22 00:04Z | PAXGUSDT | taker | sell/open | 136.16 | 0.081698 | 6.00 |
| 10-22 02:05Z | AAVEUSDT | taker | buy/close | 15.21 | 0.009127 | 6.00 |
| 10-22 02:05Z | AAVEUSDT | taker | buy/close | 45.64 | 0.027383 | 6.00 |
| 10-22 02:05Z | AAVEUSDT | taker | buy/close | 45.64 | 0.027385 | 6.00 |
| 10-22 02:06Z | ADAUSDT | taker | buy/close | 151.25 | 0.090750 | 6.00 |
| 10-22 04:01Z | TIAUSDT | taker | sell/open | 61.18 | 0.036711 | 6.00 |
| 10-22 04:01Z | UNIUSDT | taker | sell/open | 58.04 | 0.034824 | 6.00 |
| 10-22 08:00Z | MINIMAXUSDT | taker | sell/open | 106.33 | 0.063801 | 6.00 |
| 10-23 01:37Z | MINIMAXUSDT | taker | sell/close | 103.77 | 0.062260 | 6.00 |
| 10-23 01:37Z | MINIMAXUSDT | taker | sell/close | 6.35 | 0.003812 | 6.00 |
| 10-23 06:00Z | BABAUSDT | taker | sell/open | 91.02 | 0.054611 | 6.00 |
| 10-23 06:38Z | UNIUSDT | taker | sell/close | 57.82 | 0.034694 | 6.00 |
| 10-23 06:38Z | PAXGUSDT | taker | sell/close | 56.36 | 0.033818 | 6.00 |
| 10-23 06:38Z | PAXGUSDT | taker | sell/close | 136.88 | 0.082128 | 6.00 |
| 10-23 06:38Z | PAXGUSDT | taker | sell/close | 366.36 | 0.219814 | 6.00 |
| 10-23 06:38Z | WUSDT | taker | sell/close | 44.35 | 0.026610 | 6.00 |
| 10-23 06:38Z | ARBUSDT | taker | sell/close | 57.37 | 0.034425 | 6.00 |
| 10-23 06:38Z | TIAUSDT | taker | sell/close | 2.40 | 0.001440 | 6.00 |
| 10-23 06:38Z | TIAUSDT | taker | sell/close | 56.31 | 0.033787 | 6.00 |
| 10-23 06:38Z | ARIAUSDT | taker | sell/close | 35.27 | 0.021164 | 6.00 |
| 10-23 06:38Z | JCTUSDT | taker | buy/close | 17.84 | 0.010705 | 6.00 |
| 10-23 06:38Z | CLUSDT | taker | sell/close | 20.18 | 0.012109 | 6.00 |
| 10-23 06:38Z | CLUSDT | taker | sell/close | 74.67 | 0.044803 | 6.00 |
| 10-23 06:38Z | AMZNUSDT | taker | sell/close | 32.61 | 0.019566 | 6.00 |
| 10-23 06:38Z | AMZNUSDT | taker | sell/close | 115.39 | 0.069235 | 6.00 |
| 10-23 06:38Z | BABAUSDT | taker | sell/close | 91.30 | 0.054780 | 6.00 |
| 10-23 08:00Z | AAVEUSDT | taker | buy/open | 14.96 | 0.008975 | 6.00 |
| 10-23 08:00Z | AAVEUSDT | taker | buy/open | 44.87 | 0.026924 | 6.00 |
| 10-23 08:00Z | ARKMUSDT | taker | sell/open | 36.32 | 0.021793 | 6.00 |
| 10-23 08:00Z | ARMUSDT | taker | buy/open | 70.63 | 0.042378 | 6.00 |
| 10-23 08:00Z | BABAUSDT | taker | sell/open | 8.03 | 0.004821 | 6.00 |
| 10-23 08:00Z | BABAUSDT | taker | sell/open | 82.36 | 0.049416 | 6.00 |
| 10-23 08:00Z | CSOPSK2LHKDUSDT | taker | buy/open | 29.66 | 0.017798 | 6.00 |
| 10-23 10:00Z | GOOGLUSDT | taker | sell/open | 518.59 | 0.311155 | 6.00 |
| 10-23 10:00Z | MOONSHOTUSDT | taker | buy/open | 40.38 | 0.024228 | 6.00 |
| 10-23 10:00Z | MOONSHOTUSDT | taker | buy/open | 9.77 | 0.005862 | 6.00 |
| 10-23 12:00Z | MAGMAUSDT | taker | sell/open | 21.02 | 0.012612 | 6.00 |
