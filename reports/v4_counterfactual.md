# v4 反事实测量:过期/武装限价单『如果市价化会怎样』

生成时间(UTC):2026-10-19T09:20:49+00:00;脚本: scripts/run_v4_counterfactual.py;配置:config/v4.json(E7±1 白名单,k_is=0.8, limit_front_edge,N_fill=12)。

## 0. 复核(前置硬闸,对不上即停止)

- dev@1.0x:counters==冻结=True, net_sum=-124.177648 vs frozen -124.177648(match=True), trades_match=True
- dev@1.5x:counters==冻结=True, net_sum=-133.329294 vs frozen -133.329294(match=True), trades_match=True
- dev@2.0x:counters==冻结=True, net_sum=-143.677896 vs frozen -143.677896(match=True), trades_match=True
- full@1.0x:counters==冻结=True, net_sum=-167.999118 vs frozen -167.999118(match=True), trades_match=True
- full@1.5x:counters==冻结=True, net_sum=-178.420883 vs frozen -178.420883(match=True), trades_match=True
- full@2.0x:counters==冻结=True, net_sum=-188.189360 vs frozen -188.189360(match=True), trades_match=True
- test@1.0x:counters==冻结=True, net_sum=-75.380638 vs frozen -75.380638(match=True), trades_match=True
- test@1.5x:counters==冻结=True, net_sum=-80.679609 vs frozen -80.679609(match=True), trades_match=True
- test@2.0x:counters==冻结=True, net_sum=-86.346132 vs frozen -86.346132(match=True), trades_match=True
- armed 复算:full/dev/test = 162/125/37(期望 162/125/37,全部相等)。武装集合跨成本 1.0/1.5/2.0 完全一致(断言)。

## 0.1 声明

- 单发反事实:每条信号用全新 Broker/PositionManager 独立重放,与其他持仓/挂单无组合约束交互(每品种一仓限制不生效);合计净额是逐单笔反事实之和,不是任何可实际成交组合的 equity 路径。
- 市价化定义:信号 1h bar 收盘决策 ⇒ 下一根 1h 开盘市价入场(taker 0.0006 + 滑点 0.0002,同 config/v4.json),§8 六条与真实回放同 params、同认知缓存,funding 按 1h bar 计提(v4 真实为 15m记价,dt 比例口径不变,差异声明);§8.2 减仓按 v2 市价口径(真实v4 为减仓限价 maker)。成本恒为 1.0x。
- sizing 近似:qty = risk_frac·equity_at_arm/|entry-invalid|,cap = leverage_cap·equity_at_arm/entry,向下取整;equity_at_arm = 真实 v4(1.0x)回放在武装瞬间的 equity_mtm(如实声明近似,filled 组给出 |qty_cf-qty_actual| 中位相对差作扰动量级,并另给 same-qty 变体(强制真实成交 qty,隔离 sizing 噪声的纯路径 A/B,脚本进程内手工建仓,冻结源码不动,账本口径与 Broker._fill_open 逐字段一致)。
- 结局判定用与 v4 相同的 15m 严格穿过规则(引擎原生回放):sell 限 high>limit,buy 限 low<limit,窗口 12×1h=48×15m。
- resting_at_end/skip_min_size 不是过期单(前者数据末段仍挂单,后者价格穿越但 sizing 拒绝);过期=EXPIRED 才计入过期组。
- test 段过期单 n=4 ⇒ INSUFFICIENT_DATA(如实标注,不引伸)。

## 1. 结局分布(每段武装单 → 15m 严格穿过回放结局)

| 段 | armed | filled | expired | skip_min_size | resting_at_end | canceled |
|---|---|---|---|---|---|---|
| full(全部 162) | 162 | 140 | 20 | 1 | 1 | 0 |
| dev(125) | 125 | 108 | 16 | 1 | 0 | 0 |
| test(37) | 37 | 32 | 4 | 0 | 1 | 0 |

## 2. 过期单反事实(核心问题:超时撤单若次 1h 开盘市价入场)

| 段 | 过期n | 可交易 | 拒绝 | 胜率 | 平均R | 合计净额 | 中位持仓h | 到T1未先破invalid(真赢家) | 真赢家净额合计 | 躲过亏损(net≤0) | 躲过亏损合计 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| full | 20 | 20 | 0 | 0.750 | 1.5288 | 41.22 | 22.0 | 12 | 40.94 | 5 | -1.57 |
| dev | 16 | 16 | 0 | 0.750 | 1.8054 | 39.32 | 22.0 | 9 | 38.53 | 4 | -1.06 |
| test ⚠INSUFFICIENT_DATA | 4 | 4 | 0 | 0.750 | 0.4573 | 3.48 | 19.0 | 3 | 4.38 | 1 | -0.90 |
- full 到期单最终退出规则分布:§8.1:n=19,net=36.79; §8.5:n=1,net=4.43; 拒绝原因=[]
- dev 到期单最终退出规则分布:§8.1:n=15,net=34.89; §8.5:n=1,net=4.43; 拒绝原因=[]
- test 到期单最终退出规则分布:§8.1:n=4,net=3.48; 拒绝原因=[]

## 3. 已成交单 limit-vs-market 逐单 A/B(同信号市价化)

变体① qty_cf = 武装瞬间权益 sizing(默认);变体② same-qty = 强制真实成交 qty(隔离 sizing 噪声,纯执行路径/费用类别效应)。

| 段 | n | Σ价格改善(①qty_cf) | mean | Σ结局改变① (cf−actual) | mean | Σ价格改善②(实量) | Σ结局改变②(同量) | mean | ΔR②(实际risk计) | 符号翻转② |
|---|---|---|---|---|---|---|---|---|---|---|---|
| full | 140 | 35.96 | 0.2569 | 4.53 | 0.0323 | 79.42 | -72.11 | -0.5150 | -0.3664 | 7 |
| dev | 108 | 33.39 | 0.3092 | -6.44 | -0.0596 | 69.97 | -61.87 | -0.5728 | -0.3926 | 5 |
| test | 32 | 4.75 | 0.1484 | 19.15 | 0.5984 | 16.36 | -17.77 | -0.5555 | -0.2789 | 2 |

价格改善 = (open_next − limit_price)×方向×qty(正=限价入场价更优,不含费用类别差:市价入场额外付 taker+滑点、真实限价付 maker 已含在 cf 净额内);结局改变 = cf 净 − 真实 v4 净(同信号市价次开盘全权走到退出,单发反事实)。(a)+(b) 非精确分解:§8 路径(entry 不同 ⇒ 保本/MFE/时间止损不同)与 qty 基准在变体①中均变(|qty相对差|中位 0.3000 full),变体② 锁死 qty 只比路径。

## 4. 其他结局(非过期、非成交,不计入过期组)

- full:ETHUSDT@2025-02-03T01:00:00+00:00 fate=skip_min_size,cf_outcome=traded,cf_net=1.4072826115212536
- full:SOLUSDT@2026-10-19T02:00:00+00:00 fate=resting_at_end,cf_outcome=open_end,cf_net=0.1734249583439862
- dev:ETHUSDT@2025-02-03T01:00:00+00:00 fate=skip_min_size,cf_outcome=traded,cf_net=1.4072826115212536
- test:SOLUSDT@2026-10-19T02:00:00+00:00 fate=resting_at_end,cf_outcome=open_end,cf_net=0.29955220077599165

## 5. 问题回答(cheap numbers only)

- 过期单(full,n=20):市价化胜率 0.750、平均R 1.5288、合计净额 41.22 USDT;真赢家 12 笔(净额 40.94),躲过亏损 5 笔(净额 -1.57)。
- 已成交单 A/B(full):限价相对市价的 Σ价格改善 ①35.96 / ②(同量)79.42 USDT;Σ结局改变(cf−actual) ①4.53 / ②(同量)-72.11 USDT。

机器可读逐信号全量:reports/v4_counterfactual.json。
