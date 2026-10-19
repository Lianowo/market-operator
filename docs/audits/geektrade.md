# 开源算法审计：geektrade-strategies

审计日期：2026-10-19 ｜ 审计人：系统化交易研究（子代理）

## 一、来源与许可证

| 项 | 值 |
| --- | --- |
| 实际来源仓库 | `https://github.com/grinay/geektrade-strategies` |
| 克隆方式 | `git clone --depth 50`（仓库实际仅 2 条提交） |
| 克隆时 HEAD | `855641d4667c6dd6bac2815690b15cd9d4991a6b`（2026-03-03 16:18 +0300，grinay，"feat: add Pine Script source code for both strategies"） |
| 许可证 | MIT（`LICENSE`，© 2024 GeekTrade (geektrade.online)）。允许自由使用/修改/再发布（含借鉴代码），仅需保留版权声明；"AS IS" 无担保。借鉴代码与思想均无法律障碍 |

**性质澄清**：仓库全量 = 2 个 TradingView Pine Script 策略（ETH Momentum Breakout v15，Pine v5；Dump Reversal Peak Trail v2 "DP2"，Pine v6）+ README/引流文档，是 **GeekTrade webhook 商业产品的营销引流仓库**。无测试、无 CI、无可执行代码（Pine 只能在 TradingView 运行），无独立数据。

## 二、实际阅读的文件清单

逐行深度阅读（仓库极小，两份源码均全文读完）：

- `strategies/eth-momentum-breakout/strategy.pine`（712 行，全文）— 挤压突破策略
- `strategies/dump-reversal-peak-trail-v2/strategy.pine`（354 行，全文）— 砸盘反转策略(DRPT)
- `strategies/eth-momentum-breakout/README.md`（全文）— 绩效声明与参数说明
- `strategies/dump-reversal-peak-trail-v2/README.md`（全文）
- `README.md`、`docs/automate-with-geektrade.md`、`LICENSE`、`.github/FUNDING.yml`

验证性命令：`git log`（全历史 2 条）；逐行核对 `request.security` 调用形式与 `barstate.isconfirmed` 门控（未来函数审查，见第四节）；核对 `strategy()` 声明与 README 声明的矛盾（pyramiding、推荐配置）。**测试运行：不可能——仓库无任何测试文件，Pine Script 无本地执行环境。**

## 三、核心算法逐一分析

### A. ETH Momentum Breakout v15（多空，默认仅多，2H）

**入场（7 条件与门，strategy.pine:420-421）**：

1. 波动率挤压（TTM Squeeze）：BB(20, 2σ) 整体收进 KC(20, 1.5·ATR) 内，持续 ≥3 根；释放后 6 根内有效（:231-246）。
2. 突破质量：`close > BB上轨 + 0.3·ATR`（空对称），且实体 ≥ 区间 40%（:284-288）。
3. 动量：`linreg(close − SMA12, 12)` 为正且递增（:302-304）。
4. 放量：`volume > 1.8 × SMA20(volume)`（:310-311）。
5. RSI 极值过滤（75/25）—— **指标叠加类内容，不属于本系统路线，按审计要求仅记此一句**。
6. 日线 regime 过滤：价格距 200-EMA ≤35%；ATR/其 100 期均值 ∈ [0.4, 2.0]；可选 ADX 区间（默认关）（:191-201）。
7. 12 根冷却；"Smart Cooldown" 把上一笔是 runner（收过 TP1）的冷却减半（:335-347）。

**出场（5 路）**，全部 ATR 倍数（入场时锁定）：

- SL = 2.0·ATR；TP1 = 1.5·ATR 出 50%（默认）；TP2 = 3.5·ATR 全出（:462-482）。
- BE：MFE ≥ 1.2·ATR 后止损抬到 entry + 0.2·ATR（:493-495）。
- 追踪：`stop = max(stop, close − mult·ATR)`，partial 后 mult 2.5→1.5（:497-505）。
- **Runner Mode**：TP1 后撤掉 TP2 上限，止损取 `max(ATR追踪, lowest(low, 8))`（8 根低点结构轨，:502-503）。
- 失败突破退出（默认关）：持仓第 1~3 根内收盘跌回参照（BB上轨/BB中轨/信号bar收盘"Entry Price"）则市价全出（:376-389, 551-554）。
- 停滞退出：持仓 ≥10 根且 MFE < 0.5·entryATR → 全出（:370, 563-566）；超时 100 根（仅未收 TP1 者生效，:369, 557-560）。

**Adaptive Regime**：入场瞬间用 ATR 百分位（percentrank, 100）与 `|close−EMA50|/ATR` 分 HOT/NORMAL/COLD 三档，**锁定**并按 ×1.35 / ×1.0 / ×0.75 缩放 TP/SL/Trail 倍数与 TP1 减仓比例（HOT 35% / NORMAL 50% / COLD 60%）（:261-272, 437-446）。

**账目诚实性审查**：

- 费用：`commission.percent = 0.1`（TV 语义为**每笔成交双向**计 0.1%，进出各收一次，未双扣也未漏扣）+ `slippage=2` tick；`process_orders_on_close=false`，信号 bar 收盘确认、next bar 开盘成交——在 TV 口径下属常规诚实配置。
- 但绩效表述是**营销级**：README 的 +197% / PF 3.08 / 胜率 56% 基于 2H ETH 单品种 2020-10~2026-02，约 12 笔/年 ≈ **66 笔总样本**，参数从 v1 迭代到 v15 全部样本内；100% 权益复利下单仓；"Max DD $4,200(14% of peak)" 的分母是**终点复利峰值**，中途回撤占比被系统性低估；无 walk-forward/OOS。TV broker emulator 对 stop 与 limit 同 bar 命中按假设路径成交（open→low→high→close），无量能/触及细粒度验证。
- 小账目瑕疵：failBrkRef "Entry Price" 实际取的是**信号 bar 收盘**而非真实成交价（:457），两者因 next-open 成交存在价差（功能默认关，影响有限）。

### B. Dump Reversal Peak Trail v2（DRPT/DP2，仅多，5–15m）

**状态机**：单 bar 砸盘异常（|close/open−1| > 1%，红 bar，且 **≤ 2% 的 dump 上限**）＋ 跌破此前 N=7 日最低价（日线序列取 `[1]`，剔除未收盘当日）→ 进入 armed；新低延续（仍在上限内）则**重置参照 bar**；等到对砸盘 bar 高点的收复确认后入场（:96-107, 164-217）。6 种入场模式（enum EntryMode）：

- Classic：收盘 > dumpCandleHigh 即入（:262）。
- TwoCloses：连续 N 根收盘在其上（:223）。
- BreakRetest：crossover 后挂**限价回踩单**于 dumpHigh + 0.2·ATR，8 根过期（:230-237）。
- ATRStop（默认）：挂 dumpHigh + 0.5·ATR 的 stop 单，12 根过期（:239-245）。
- Auto：先限价回踩，过期后降级为 ATR stop 单（:247-260, 344-349）。
- Rebound：wait-for-green + 平整度（5 根 HH-LL 区间 ≤0.4%）+ 从最低点反弹 ≥0.8%（:268-277）。

**出场**：整仓按加权均价的**利润峰值回撤追踪**——MFE(收盘，%口径) 达 +5% 后 armed，利润从峰回撤 ≥1.5 个百分点 → 全出（:317-323）。可选固定 15% 止损（**默认关**）、"亏损中最大持有小时数"（默认 0=关）（:325-335）。冷却 4h（:91-93, 161-162）。过滤器：距 3 日高点 1% 内不追多（:111-113）；BTC 4h 涨幅 > 0 的跨品种门（:116-119）。

**账目诚实性审查**：

- `commission 0.1%`（双向）+ `slippage=10` tick 存在；但 `process_orders_on_close=true`（:2）入场在**信号 bar 收盘精确成交**，偏乐观。
- **默认配置没有下行退出**：止损关、时间限亏关，唯一退出是 +5% 利润才 armed 的峰值回撤——从未盈利的单子在回测里**无限期持仓**，亏损只体现在权益曲线浮亏、不进 closed-trade 净利，README 恰好**未披露任何绩效数字**（主 README 表格为 "—"）——不披露本身可算诚实，但账目结构决定了其披露的任何东西都会高估。
- **文档与代码矛盾**：① `strategy(..., pyramiding=1)`（:2）与代码中 `canPyramid = strategy.opentrades < 5`（:122）及整套"加仓成交"处理（:297-312）矛盾——pyramiding=1 下同向加仓永不发生，该代码路径是死代码；② 策略 README 描述的是 Rebound 模式语义（"等待首个绿色收盘"），而代码默认模式是 ATRStop 挂突破单；③ `margin_long=20`（:2）= 隐含 5× 杠杆保证金设定，README 未披露。
- 跨品种过滤引固定 `BYBIT:BTCUSDT.P`（:117），复现依赖 TradingView 数据源。

## 四、未来函数与重绘审查

两策略在结构性审查中**未发现未来函数**，"non-repainting" 宣传与代码一致：

- 所有 HTF/日线数据经 `request.security` 调用，未传 `lookahead` 参数（默认 `barmerge.lookahead_off`），历史 bar 上取值为已确认的 HTF 值；HTF EMA 斜率用 `[slopeBars]` 历史索引，DRPT 的 N 日最低取 `[1]` 剔除未收盘日——这两项是容易被写错的前视点，此处写法正确（ETH :191-209；DRPT :103-106, 118）。
- DRPT 全逻辑显式 `barstate.isconfirmed` 门控（:147 起 10 余处），armed/挂单状态只在收盘确认后迁移；ETH 信号全部 bar 收盘口径、next-bar-open 成交。
- 挤压时长判定 `sqzDuration[1]`、partial 检测 `position_size < entryQty*0.75` 等均为因果序列操作；无可写历史序列的 `var` 误用。
- 残留级别：TradingView broker emulator 的**bar 内路径假设**（stop/limit 同 bar 命中按 OHLC 推定顺序成交）是平台级不确定性，不是代码级前视；严格研究须以 bar 内可验证性重审这些成交。

## 五、不合理假设与固定阈值清单

| # | 假设/固定值 | 位置 | 影响 |
| --- | --- | --- | --- |
| 1 | 全参数固定倍数（SL 2.0/TP1 1.5/TP2 3.5/Trail 2.5/1.5·ATR、放量 1.8×、实体 40%、挤压 ≥3/≤6 根） | ETH 输入区 | 全部阈值样本内调参产物；换品种/周期不可比 |
| 2 | 绩效基于 ~66 笔单品种样本 + 100% 权益复利 | ETH README | 统计功效不足；DD% 被复利峰值低估 |
| 3 | dump "上限" 2%（大于 2% 的砸盘**放弃**做多） | DRPT :14, 98 | 人为排除深跌样本，回测篮天然只含浅破位 |
| 4 | 默认无下行止损、退出 armed 阈值 +5% 利润 | DRPT :17-19 | 深度套牢仓无限期挂账，closed-PnL 口径系统性偏乐观 |
| 5 | `process_orders_on_close=true` 精确按收盘成交 | DRPT :2 | 入场成交价乐观化 |
| 6 | pyramiding=1 与 5 档加仓代码并存 | DRPT :2, 122 | 文档与实测行为不符（加仓为死路径） |
| 7 | 跨品种门固定 BTC 永续 4h 涨跌 >0 | DRPT :116-119 | 单因子相关性代理，无统计验证 |
| 8 | 失败突破退出参照 = 信号 bar 收盘（非真实成交价） | ETH :457 | 参照与滑点错位（默认关，影响小） |
| 9 | runner 结构轨 = `lowest(low, 8)` 固定窗口 | ETH :254, 502 | Donchian 式退化结构，非 pivot 语义 |

## 六、借鉴价值判定表

| 组件 | 判定 | 理由 |
| --- | --- | --- |
| TTM 挤压 + 放量 + 动量 + RSI 叠加入场 | 不值得借鉴 | 指标叠加路线，不属于本系统；固定阈值样本内调参 |
| Partial TP 1.5ATR 出 50% / TP2 3.5ATR | 不值得借鉴 | 固定 R 分批，market-operator 的结构目标减仓语义更严 |
| MFE 触发的 BE（+1.2ATR → entry+0.2ATR） | 不值得借鉴 | 巡游阈值版保本；我们的结构目标触发保本含更多信息 |
| Runner Mode（撤 TP2 上限 + lowest(low,8) 结构轨） | 不值得借鉴 | 8 根低点是 pivot 防线退化版；"无上限"是策略偏好非机制 |
| 失败突破退出（收盘退回参照即出） | 不值得借鉴（但有旁证价值） | 作者自测"v7 数据显示多头成本 −$4,565"而默认关闭（:80-81 注释）——**与我们"天真失败退出有损"的发现互为独立旁证** |
| 停滞退出（MFE<0.5ATR/10 根） | 不值得借鉴 | 与 market-operator §8.6 时间止损（N 根内 MFE<0.5R）同型 |
| Adaptive Regime 锁定缩放（HOT/COLD ×1.35/×0.75） | 仅思想登记 | regime 量化缩放出场参数；冻结容易，但我们样本量下三档拆分后不可检验 |
| DRPT 砸盘量级窗口过滤（下限 1% + **上限 2%**） | **唯一可登记候选** | 对失败破位事件按刺穿深度设反向窗口；见第七节 |
| 限价回踩单 / stop 挂单 + 过期 TTL（8/12 根） | 有缺陷但思想可取 | 待成交入场单的生命周期管理；execution 微机制，独立经济含义弱 |
| 利润域峰值回撤追踪（armed@+5%，回撤 1.5pp） | 不值得借鉴 | 将追踪域从结构换为持仓利润%，等价于延迟激活追踪止损 |
| 冷却后重入场（含 runner 减半） | 不值得借鉴 | 节流机制，无独立经济含义 |
| BTC 4h 跨品种趋势门 | 不值得借鉴 | 单因子相关性启发式 |
| IntoTheBlock 链上过滤（默认关） | 不属于本系统路线 | 一句话登记 |

## 七、与 market-operator 语义对照与新增判定

对照基准 = market-operator 已有语义（因果波段/区域生命周期/E1–E7/F1/持仓五机制/经济学结论）。

| geektrade 语义 | 最近对应物 | 判定 |
| --- | --- | --- |
| 挤压后突破入场（ETH 7 条件） | E1 突破接受的"指标前过滤+固定 R"版 | **重复且更弱**：我们要求区域生命周期上的连续收盘接受，他只要求 BB 阈值外放量收盘；RSI/量能/斜率叠加不属本系统路线 |
| DRPT 砸盘→破 N 日低→收复做空 bar 高点 | E2 失败突破的**支撑侧镜像**（刺穿支撑 zone 外缘后收回带内，direction=+1） | **事件核重复**：E2 双向覆盖该语义；差异在于他无 zone 对象（用 7 日 Donchian low，zone 生命周期退化）、确认参照为单 bar 高点而非带内回收 |
| BreakRetest / ATRStop 挂回踩单 | E7 FLIP_RETEST 的盲挂版 | **重复且更弱**：我们要求破位→回踩**拒绝证据**→角色互换确认后入场；他是无拒绝证据的盲限价单。唯一形态差异是**挂单 TTL（8/12 根过期）**——execution 微机制，可冻结但独立经济含义弱 |
| 利润域峰值回撤追踪（DRPT 出场） | pivot 单向追踪防线 | **重复**：域替换（结构价→利润%）不构成新机制类别，且丢失结构语义 |
| Partial 50% + BE + Trail(收紧) | 结构目标减仓 50%+保本 + pivot 防线 | **重复**：固定 R 版；我们的触发条件是结构证据（target_1 触及），信息量更高 |
| Runner Mode（8 根低点轨） | pivot 防线 | **重复**：Donchian 低点是 pivot 序列的粗采样版 |
| 失败突破退出（close 退回参照） | §8.1 破位退出（结构失效收盘退出） | **重复且经验上负收益**（作者自证） |
| 停滞退出 | §8.6 时间止损（MFE 条件） | **完全同型** |
| Adaptive Regime 三档锁定缩放 | （无直接对应物；我们有 ATR 归一但无 regime 量化缩放） | **形态新增、非原语新增**：是参数化机制而非事件/持仓原语；独立经济含义存疑；我们 2 年三品种样本按 3 档拆分后每档 ≈ 数十笔，**不可检验** |
| 冷却/智能冷却/BTC 门/平整度门 | （无对应物） | **形态新增、非原语**：节流/外生门控，无独立经济含义 |
| **dump 量级窗口（≥1% 且 ≤2% 才入场）** | E2 支撑镜像的**深度条件变体** | **唯一接近"新增"的候选**：对失败破位按刺穿深度设反向窗口（只 fade 浅破位、放过深破位）。独立经济假设明确（深破位继续、浅破位失败）、可平凡冻结、5–15m 频率下样本量级可检验。但它本质是**既有 E2 事件族上的一个过滤器**，不是新事件类型/新持仓机制/新执行假设——且作者同时设置了 2% 的 cap 又给了 15% 的可选止损，说明该机制本身未被作者信任为独立原语 |

**新增判定结论：无真正新增经济原语。** 全部事件与持仓语义要么是 market-operator 已有机制的固定 R／无 zone／无拒绝证据的退化版本，要么是节流与参数化类的非原语机制。唯一值得登记备查的是"失败破位深度窗口过滤"这一 E2 变体思想；鉴于 market-operator 经济学结论的瓶颈在**入场选择性**而非收割端（收割端已验证健康：触目标 96 笔 +3.9R），该过滤器即便成立，改变的是入场集合并可能改善选择性——可作为**低优先级研究备忘**（若未来重测 E2，可按刺穿深度分桶检验增量），不构成当前任何机制的必要增量。

**任务指定重点逐条**：

1. **Runner Mode**：数学 = TP1 成交后 TP2 上限删除，止损取 `max(当前止损, max(close−1.5ATR, lowest(low,8)))` 只升不降。结构证据驱动？**否**——8 根低点是固定窗口 Donchian 代理，不是 pivot/zone 语义。判定：重复（pivot 防线的粗版）+ "无 TP 上限"策略偏好。
2. **分批减仓**：TP1(1.5ATR) 出 50%、TP2(3.5ATR) 出余、hot/cold 档调比例(35%/60%)。**固定 R**，非结构驱动；重复（我们的结构目标减仓更严）。
3. **动态止损**：BE 由 MFE(+1.2ATR) 触发而非结构目标；Trail 固定 ATR 倍数两段式。"动态"仅是激活条件，参数全固定。判定：重复（语义信息量低于我们的结构触发保本）。
4. **失败突破退出**：持仓 1–3 根内收盘跌破信号 bar 收盘/BB 即市价出。**默认关闭**，作者公开的自测结果为负（−$4,565）——与我们 §8.1 结构失效退出同为"失败即退"类，但参照物是价格而非结构；其负结果对我们当前设计构成独立旁证。
5. **回测账目是否诚实**：**结构性诚实**（佣金双向 0.1%、滑点、bar 收盘确认、无前视、N 日低剔除未收盘日），**呈现性不诚实**（样本内 v1→v15 调参、~66 笔样本、100% 复利、DD 分母取终点峰值、无 OOS；DRPT 默认无下行退出且 pyramiding 声明与代码矛盾、默认入场模式与自家 README 描述矛盾）。**其绩效数字不可作为任何外部证据使用。**

---

*验证记录*：`git log` 全历史 2 条提交、HEAD 与 LICENSE 逐字核对；两份 `strategy.pine` 全文逐行阅读；`request.security` 全部 6 处调用逐一检查 lookahead/历史索引写法；DRPT 全部 `barstate.isconfirmed` 门控与状态变量逐一追踪（armed/waiting/挂单/过期/冷却/平仓重置路径）；`strategy()` 声明与两份 README 逐项比对（发现 pyramiding、默认模式两处矛盾）；仓库无测试文件、无可本地执行代码，测试无法运行（如实记录）。
