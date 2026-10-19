# 第二轮开源深挖（2026-10-19，面向"止损/目标几何"病灶）

动机：链路诊断发现主病灶为几何错位（中位 MFE +1.91R 却回吐中位 3.6R 至负收益）。web 深挖三路，许可证逐一核实，营销数字一律不采信。

## 关键候选

### MrOwl1011/Smc_ZOB_CISD(MIT, HEAD af253a8)——本轮最重要
- 证据等级 A-:81 组参数 × 3 年 MT5 黄金回测，271 笔，如实记录失败实验。
- 入场：扫流动性（破 swing low 收回）→ CHoCH/CISD 收盘确认 → 回踩触及触发。
- **失效取位=结构区边界**(OB 区底，非微观极值——区高天然含波动缩放）。
- **目标几何实证：2R 最优；>3R 必崩（RF 0.33)；流动性目标钳制 [1R, 3.5R]**。
- **镜像反模式（与我们病灶逐条对应，冻结为禁令）**:① 过早移保本：+810→+430;
  ② 无目标"等反向结构才平":RF 5.69→1.28——无止尽等待=回吐根源。

### Turtle/Donchian 系（Jesse jesse-ai/example-strategies MIT;vnpy/vnpy_ctastrategy MIT)
- 止损=入场 ∓ 2N(ATR20)，加仓棘轮 ½N,**不设目标**, 反向 Donchian(10/20) 通道出场。
- 近止损+无远目标几何：linha de que 回吐上界=通道宽度。仓内自报回测只示形态不作业绩。
- 学术锚点：Liu/Tsyvinski/Wu(NBER WP25882/RFS 2022)crypto 存在动量溢价（方向性证据，非策略背书）。

### 收割机制参照（chrisleekr/binance-trading-bot Apache-2.0;NFI GPL-3.0;passivbot Unlicense)
- "达标→激活 trailing"二段收割；近端多档目标替代远端单目标。
- 无 price-action 内核，仅收割语义可参考。

## 判定（何者为几何修复而非过滤器）
1. **失效锚定结构区边界**（天然波动缩放，替代微观极值）——两族共识，与 F1 同源但语义化。
2. **目标钳制**：近端 2R 为主、流动目标 ≤3.5R（ZOB 实证） 或 **无目标+结构通道 trailing**(Turtle)——
   两者都是重新设计出口几何，正对我们"回吐 3.6R"。
3. **禁令**：保本移动禁止提前；"等反向结构才平"禁止。

## 排除路径（勿再挖）
LuxAlgo SMC 移植族（纯画图）、vlex05/SMC-Algo-Trading（停更指标库）、xaubot-ai 营销金油 bot、
低星 SMC 玩具仓、OctoBot/hummingbot（做市）、geektrade（营销数字）、quant-trading-go /
OrderFlow-absorption（许可不可用）、joshyattridge/smart-money-concepts（指标库无策略链，
仅 bos_choch 收线/影线突破区分可借一态）。
