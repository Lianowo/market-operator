# 开源算法审计：personalai-mobile-v25(PersonalAIOnMobile / V25 Real-Time Wall Engine)

审计日期：2026-10-19 ｜ 审计人:系统化交易研究(子代理)
审计对象说明：任务指定重点为该仓库内 MT5 实时交易子系统 `mt5-core-server` 中的 **V25 状态机**(接近→试探→突破/拒绝→接受/回测/失败)。仓库主体是一个 Android "AI 伴侣/交易助手" App(Godot 形象、视觉、语音、LLM 管线),与交易研究无关的部分不展开。

## 一、来源与许可证

| 项 | 值 |
| --- | --- |
| 实际来源仓库 | `https://github.com/skyliner2008/PersonalAIOnMobile` |
| 克隆方式 | `git clone --depth 50`(浅克隆)至 `oss-review/PersonalAIOnMobile` |
| 克隆时 HEAD | `72f4ea7d1b486d09c431118dcfaef308154095c4`(2026-10-13 00:21 +0700,"feat(avatar): live vision test screen; vision review fixes") |
| 许可证 | **无 LICENSE 文件**。根目录及全仓库 `git ls-files` 均无 LICENSE/COPYING;GitHub API `license: null`。README 徽章自称 "License-MIT",但**未随附任何许可证文本**,徽章不产生法律效力。按默认(保留所有权利)处理:**思想上可参考，代码不可复制/改写借用**。 |

**性质澄清(重要)**:V25 不是一个回测/研究系统，而是 **MT5 实时交易机器人**(XBTUSD/XAUUSD/US100/US500,tick 驱动，M1/M5/M15,MQL5 bridge,LLM 管线做方向仲裁)。全仓库**没有任何历史回测、成本核算或统计检验**;唯一的"经济学证据"是 wiki 中的实盘日志复盘(约 2 小时 XAU 日志：97 个决策周期、3 笔开仓、9 笔平仓合计 +45.45，无统计意义，且多笔注明"无可用平仓历史"—账目本身不完整)。V25 默认 shadow 模式(只出 TradePlan 候选不落单),live 路径由 AutoEngine 统一仲裁。

## 二、实际阅读的文件清单

逐行深度阅读(非仅 README):

- `mt5-core-server/src/services/auto/v25/state/WallStateMachine.ts`(400 行，全文)— 7 状态 FSM
- `mt5-core-server/src/services/auto/v25/types.ts`(全文)— 状态定义、逐品种 profile 阈值
- `mt5-core-server/src/services/auto/v25/playbooks/index.ts`(867 行，全文)— PlaybookSelector 级联、live 执行前检、账目记录
- `mt5-core-server/src/services/auto/v25/playbooks/PB1_WallTouchScalp.ts` / `PB2_FvgFillReversal.ts` / `PB3_SweepReversal.ts` / `PB4_RangeFade.ts` / `PB5_BreakoutRetest.ts`(全文)
- `mt5-core-server/src/services/auto/v25/state/ReactionDetector.ts`(全文)— M1 吞没/针棒/FVG 回补识别
- `mt5-core-server/src/services/auto/v25/state/BreakoutWallPromoter.ts`(全文)— 破位后角色互换
- `mt5-core-server/src/services/auto/v25/state/WallProximityIndex.ts`(全文)— 最近/最强 wall 查询 + 触及衰减
- `mt5-core-server/src/services/auto/v25/M1MicroWallBuilder.ts`(前 80 行核心)— 60 秒 tick 滚动微高低点
- `mt5-core-server/src/services/auto/analyzers/smc/WallRegistry.ts`(200 行，全文)— "稳定 wall" 注册表(EWMA 平滑、STALE/失效)
- `mt5-core-server/src/services/auto/analyzers/smc/types.ts`(前 60 行)、`smc/utils.ts`(摆动点检测段)
- `mt5-core-server/src/services/auto/core/V25MilestoneProtection.ts`(全文)— 路径障碍保本
- `.obsidian-wiki/07_Trading_Intelligence/25_RealTimeWallEngine_V25.md`(设计文档，前 150 行 + 全目)、`26_Log_Order_Report_2026-05-14.md`(实盘日志复盘，前 60 行)
- `README.md`(许可证核查)、`AGENTS.md`

测试运行:`npm install`(better-sqlite3 原生绑定在本机 node-gyp 构建失败，改用 `--ignore-scripts`)+ `npx vitest run` 针对 V25 相关测试文件。结果:**4 个测试文件 9 个测试全部通过**(PB3/PB4 playbook、WallRegistry、V25MilestoneProtection);`playbooks/index.test.ts` 套件因 better-sqlite3 原生绑定缺失(本机环境缺 VS 构建链，非代码逻辑问题)无法加载，未执行。注意:**FSM 本体(WallStateMachine)没有任何单元测试**,已有测试只覆盖 plan 数学与两个辅助组件。

## 三、核心算法分析

### 3.1 V25 状态机语义与阈值(`WallStateMachine.ts`)

- 状态机(每个 品种×方向 各一份):
  `IDLE → APPROACH → REACT → CONFIRM`(M1 收盘确认反转形态)
  `REACT → BREAK_OUT → RETEST → COMMITTED`(COMMITTED 在代码中**无任何迁移入口，死状态**)。
- 阈值性质:**不是固定价格，但是固定的人工乘数**。`budget = ATR(M15)`(无 ATR 时兜底 `0.4% × wall 价`),`approachZone = approachAtrMul × budget`(1.0–1.5,逐品种手调)、`reactZone = reactAtrMul × budget`(0.3–0.5),见 `types.ts:118-159` 的 `V25_DEFAULT_PROFILES`(XBTUSD 1.5/0.5、XAUUSD 1.2/0.4 …)。即"波动率缩放但常数拍脑袋",无估计、无敏感性分析。
- BREAK_OUT 判定：**极松**——tick mid 穿越 wall 价超过 `reactZone × 0.2`(WallStateMachine.ts:240-244),单 tick 毛刺即触发，无确认波段、无收盘确认。
- 假突破/扫流动性(PB3)判定：依赖 M1 收盘形态(吞没/针棒/FVG 回补,ReactionDetector.ts:121-149),即"影线穿越 + M1 收回墙内",严格度中等；但**接线已断**(见 3.4)。
- RETEST 判定:**只要求价格回到 reactZone 内**(WallStateMachine.ts:253-258),**不要求任何回踩拒绝确认**;PB5 入场价 = wall ± 1 tick，即"回来就买"。相对 market-operator E7(破位→回踩**拒绝**→角色互换**确认后**入场)明显更松。
- 反振荡工程:APPROACH 退出需 2.0× 带宽滞后 + ≥1.5s 最短驻留;REACT ≥3s;状态 churn >6 次/10s 告警；wall 身份切换有"强者保留"规则。这些是 tick 级节流/防抖，纯执行层。
- 触碰衰减：每次进入 REACT 对 wall 记一次 decay(WallProximityIndex.ts:38-47),`touchCount` 作为 PB1 对 1★ 微墙的放行条件(≥2 次)。"区域强度随测试次数衰减"是一个可冻结的**参数先验**,但语义上是区域先验修正，不是新事件类型。

### 3.2 Wall(关键区域)的构建与生命周期 —— 重要缺陷

- 墙来源:SMC 引擎 V2(swing(对称 5-左右 pivot)、OB、FVG、等高低点流动性),按多周期重合度打 1–5★。
- **`WallRegistry.mergeWall` 对 wall 价格做 EWMA 平滑**(`alpha=0.35/0.2`,WallRegistry.ts:82-83):**同一个 wall 的参考价随时间漂移**;`rebuildMap` 又按"当前价"把 wall 重新划到支撑/阻力侧。这意味着历史决策所依据的事实事后被改写——在我们系统里会直接违反前缀不变性测试。它只有 ACTIVE/TOUCHED/STALE/失效(close 越界+缓冲)的粗粒度生命周期，**无 forming/known 双时间戳、无 DEAD 语义、无 FLIP 的持久化**(BreakoutWallPromoter 的 FLIPPED 对象是临时构造、stars−1,不回流 registry)。
- `detectSwings`(smc/utils.ts:52-73)用左右各 len 根 pivot,右侧 len 根为确认时延——语义正常(墙"晚到" len 根而非偷看未来),但无任何确认时延的显式标记。

### 3.3 交易语义(playbooks)

- PB1 墙触头皮(默认):墙星数/周期锚门槛 + RANGING 需对方新鲜 FVG + SL ≤ 1×ATR(M15)(TRENDING 放宽到 2×)+ RRR ≥ profile.minRrr(1.5–1.8,5★ 墙放宽 0.3)。入场 = wall ± 1 tick,TP = 对方 wall ∓ (tick+spread),SL = 摆动极值/墙沿 ± (slBufferAtrMul × max(0.2×ATR, 4×spread))。
- PB2 OB+FVG 重合墙、PB3 扫流动性反转(高 RRR 2–5)、PB4 区间边界 fade(目标 = 两墙中点,RRR ≥ 1.5)、PB5 破位回踩延续(SL ≤ 1×ATR,RRR ≥ max(2.0, minRrr))。
- 方向裁定：纯墙位力学(ABOVE 墙→SELL),叠加 AI Bias Gate(与最近一次 AutoEngine 决策方向冲突则 SKIP)与 regime 门(TRENDING/RANGING 由结构方向+延续计数启发式判定)。
- 执行假设(live 前检,playbooks/index.ts:290-329):买卖分别用 ask/bid 入场参考、SL/TP 距离下限(max(品种最小停距， 3×spread, 50×tick))、按计划漂移后的实时价重算 RRR(不达标记 `MISSED_BY_DRIFT` 放弃)、15 秒计划过期、近重复入场门。TP 价格已减 spread,SL 缓冲含 4–6×spread——成本意识在 plan 层面粗略内嵌，但**没有任何历史触发模拟**(不检查触及、无成交率/滑点模型);成交账目用实际持仓回填实际入场价/SL/TP 并重算实际 RRR(这部分记账较诚实)。

### 3.4 账目与接线的诚实性发现(三处硬伤)

1. **PB3(扫流动性)在 Selector 路径下是死代码**:Selector 构造上下文时硬编码 `reversalKind: 'CONFIRM_VIA_STATE_MACHINE'`(index.ts:498),并注释"细节来自 ss.lastReversal"——但从未真正把它接上;而 `evaluatePB3` 的门是 `/FVG_FILL|PINBAR/.test(reversalKind)`,**永远为 false**。设计上最严格的假突破确认 playbook 实际从未触发。
2. **COMMITTED 状态死**:注释称"Phase C/D 会移出",代码无任何进入路径。
3. 实盘日志复盘(26_Log_Order_Report)自证账目残缺：3 笔开仓中 2 笔"无可用平仓历史",9 笔"已平仓"仅按日志 tag 分类。该系统从不产生可归因的净期望结论。

## 四、未来函数与重绘审查

- 经典未来函数(用未来数据确认过去信号):**无**。状态机/反应检测均只用决策时刻之前的数据(tick 缓冲、已收 M1)。
- 准前视/时延：pivot 墙有 len 根确认时延(未显式标记);
- **重绘(主要问题)**:`WallRegistry` 的 EWMA 平滑使 wall 参考价事后漂移、支撑/阻力归属随当前价重划——属于同一"关键位对象"的历史属性被持续改写。对实时助手影响小，但在研究语义下等同于重绘：我们系统"每个事实带 forming/known 双时间戳 + 前缀不变性"的纪律在该项目中完全缺席。
- 固定阈值：带宽常数为逐品种人工乘数(非固定价但固定手调参数),无自适应估计依据;BREAK_OUT 的 20% reactZone 穿越、velocityGate 的速度上限均为拍脑袋常数。
- 成交假设:live-only,无历史回测，故不存在"触及即成交/费用双扣"类回测造假;plan 层面 spread 内嵌、SL/TP 方向性检查完备。账目层面缺闭环统计(见 3.4)。

## 五、借鉴价值判定

| 项 | 判定 | 理由 |
| --- | --- | --- |
| 7 状态 tick FSM 骨架 | 思想可参考(不可抄代码) | "接近→反应→确认/突破→回测"是合理的执行层枚举；我们已用区域生命周期等价覆盖且更严 |
| 反振荡工程(带宽滞后 2.0×、最短驻留、churn 遥测) | 不值得借鉴 | 解决的是 tick 驱动+LLM 成本的工程噪声，与研究无关 |
| Wall 触及衰减(touchCount→放行/降星) | 弱候选思想 | "区域强度随测试次数衰减"可冻结为区域先验参数，但它是参数修正而非事件原语；样本上我们已有区域生命周期可挂此维度自行测 |
| Wall 价格 EWMA 平滑 | **判为反例** | 破坏事实不可变性，我们明确拒绝 |
| PB1–PB5 计划数学(SL=极值±缓冲、TP=对方墙、RRR 门) | 不值得借鉴 | 与我们破位/结构目标框架同族且更粗(固定 1 tick 入场、手调乘数) |
| live 前检(spread/停距下限、漂移后重算 RRR、MISS_BY_DRIFT) | 记录而已 | 实盘工程清单，非经济原语 |
| 路径障碍保本(首个 ≥3★ 中间墙触发 protectAt) | 不新增 | 与我们"结构目标减仓 50%+保本"同类，我们的定义更明确且已有经济结论 |

**总判定：无可借鉴的经济内容；思想层面仅"接近/反应/确认"的状态命名法与"区域随测试衰减"的参数先验值得知道，二者均不构成新增原语。**

## 六、与 market-operator 语义对照与新增判定

逐条对照已有语义清单:

| V25/PersonalAI 语义 | market-operator 对应 | 判定 |
| --- | --- | --- |
| IDLE/APPROACH/REACT(距离带 proximity) | 无对应事件(我们不把"接近"建模为事件) | **表面新增，实为执行层节流**,无独立经济含义(它的目的是省 LLM token,不是 alpha);若冻结为"接近事件"只会让事件目录膨胀而不改变经济结论 |
| REACT→CONFIRM(M1 吞没/针棒在墙处拒绝) | E3 区域拒绝 | **重复，我们更严**(我们要求波动率自适应确认;他们用单根 M1 形态比值阈值 0.55/0.35,固定常数) |
| REACT→BREAK_OUT(tick 穿越 0.2×reactZone) | E1 突破(k_rev·ATR 自适应确认波段) | **重复，我们更严**(他们单 tick 毛刺即破位，无确认,假阳性高) |
| PB3 扫流动性反转(影线穿越+M1 收回) | E2 失败突破/SFP | **重复**,且**接线断裂从未触发**(3.4) |
| BREAK_OUT→RETEST + 角色互换(stars−1)→ PB5 入场 | E7 FLIP_RETEST | **重复，我们更严**:他们回踩不要求拒绝确认(回到带内即入场),我们要求"回踩拒绝→角色互换确认后入场" |
| PB4 区间边界 fade(目标=两墙中点) | E5 区间回归 | **重复**(目标取中点 vs 我们对侧/均值回归定义，粒度差异不构成新事件) |
| 路径障碍保本(首个中间 ≥3★ 墙 protectAt) | 结构目标减仓 50%+保本 | **重复**,同类收割端保护；我们已有真实数据结论(收割端健康，触目标 96 笔 +3.9R) |
| 时间成分：最短驻留 1.5–3s、15s 计划过期 | 时间止损 | **重复但更弱**(他们保护状态机防抖，我们保护持仓经济) |
| 区域强度随触及次数衰减(touchCount) | 区域生命周期参数维度 | **唯一可记名的新语义候选**,但定性为**区域先验参数**而非事件/持仓/执行原语；可冻结(触及 n 次降权)、可检验，但预期是边际修正，不改变机制级 NO-GO 结论 |
| Wall 价格 EWMA 漂移 + 按现价重划支撑/阻力 | forming/known 双时间戳 + 前缀不变 | **方向相反**:他们让事实漂移，我们锁定不变 |

**新增判定结论：无真正新增的事件类型 / 持仓机制 / 执行假设。** 唯一可记名的语义差异是"区域强度随测试次数衰减"的参数先验，属于认知层参数而非交易原语，可自行挂载到既有区域生命周期上检验，无需借鉴该项目。

针对任务指定重点逐条:

1. **阈值是否固定**:带宽为 ATR 缩放 × 逐品种人工固定乘数(approach 1.0–1.5 / react 0.3–0.5),破位为 reactZone 的固定 20%——**半固定**(波动率缩放但常数拍脑袋，无估计依据)。
2. **假突破判定严格度**:机制上(BREAK_OUT)极松——单 tick 穿越 20% 反应带即破位；专门判定假突破的 PB3 因接线断裂**实际从不触发**;RETEST 不要求回踩拒绝。整体比我们的 E2/E7 显著宽松。
3. **许可证能否借鉴**:**不能借鉴代码**(无 LICENSE，徽章无效);思想层面可参考的部分(proximity 状态命名、触及衰减先验)已在上表定性为非原语。
4. **相对因果区域生命周期有无实质新增语义**:**无**。其状态机是"价格-墙距离的时间状态机",我们是"区域对象的事实生命周期";前者每个非平凡语义(CONFIRM/BREAK_OUT/RETEST/保本)都能映射到 E1/E2/E3/E5/E7 与既有收割机制且我们更严；其特有部分(proximity 带、防抖、velocity 门、LLM 仲裁)是执行/成本工程，无独立经济含义，且发生在 M1/M5 scalp 粒度——正是我们已用全成本数据证明"成本/运动比"杀死选择性的频段。

诚实性备注：仓库其余部分(LLM 管线、技术指标型 Pine Script 策略集：TSMOM/EMA 趋势/UT Bot 等信号叠加)**不属于本系统路线**,不展开。

---

*验证记录*:`git log -1` 核实 HEAD;`git ls-files` 与 GitHub API 双重确认无 LICENSE;`npm install --ignore-scripts` + `npx vitest run` 实测 4 文件 9 测试通过、1 套件因本机缺 better-sqlite3 原生绑定(node-gyp 需 VS 构建链)未加载——已在正文标注为环境限制而非代码失败。
