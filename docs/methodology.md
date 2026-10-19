# Market Operator 交易方法论（v1，研究前冻结）

> 本文档先于任何回测实现与研究运行提交。其中所有事件定义、参数先验（标记为 **先验** 的量）与研究协议在进入阶段 3 研究之前冻结。研究阶段不得为追求盈利结果而修改本文件；任何修改必须在新提交中说明理由，且修改后此前产生的所有"样本外"结论作废。

项目目标一句话版：把一名专业交易者"观察 → 判断 → 行动 → 调整"的过程，转化为明确的、因果的、可证伪的计算语义，并用真实数据检验它有没有经济价值。

---

## 0. 设计哲学与范围声明

1. **机会先于时间尺度。** 系统不绑定固定 K 线周期做认知。v1 工程上选择 1h 作为观察尺度（`obs_tf`），执行在下一根 K 线开盘成交。波段、区域、事件均由价格波动幅度与结构关系定义，而非固定窗口长度。
2. **结构来自真实价格关系。** 市场认知 = 波动率自适应的确认波段 + 关键价格区域生命周期 + 结构状态。指标只允许用 ATR 作为波动率尺度（它不产生方向判断，只提供度量单位）。不使用均线、RSI、MACD、TSI 等派生指标做认知。
3. **时间是真相。** 每个被识别的事实在逻辑上都有两个时刻：`forming_index`（事实指向的历史位置）与 `known_index`（该事实第一次可以被知道的位置）。任何交易判断只允许使用 `known_index <= 当前bar` 的事实。这是反未来函数的唯一原则，所有代码围绕它审计。
4. **不预设盈利。** 本文件只定义语义与协议。任何机制若无统计优势，输出 NO-GO，项目降级为 Research Only，不伪造能力。
5. **v1 范围。** 基于 OHLCV（1h）的认知与事件。订单流（Footprint/Delta/CVD/VP）经审计后判定为微观确认层，数据成本高、且不能替代尚未被证实的价格行为优势——**v1 明确排除**，仅作为 v2 候选（见 §10）。这是有意的范围控制，不是遗漏。

## 1. 数据来源与因果纪律（Market Observer)

- 数据源：Bitget USDT 永续 `mix/market/history-candles`。品种：BTCUSDT、ETHUSDT、SOLUSDT。观察尺度：1h。
- 数据验收：时间连续性与缺口检查、重复时间戳检查、OHLC 内部合法性（`low <= o,c <= high`)、原始响应 SHA-256 哈希存档（`data/manifest.json`)，任何研究运行须能通过 manifest 复现到同一批原始字节。
- 因果纪律：在 bar `t` 收盘时刻的认知 = 仅用 `[0, t]` 数据计算出的全部事实。研究回放慢放 bar 序列，严禁向认知模块一次性喂入全量数据后回看。

## 2. 波动率尺度

ATR(14, Wilder 平滑，Wilder, 1978) 记作 `atr(t)`，单位是价格，作为所有空间阈值的度量单位。

选择理由：稳定、业界标准、无方向性。距离、波段幅度、区域宽度、突破余量全部以 `atr` 的倍数表达，使同一套规则跨品种、跨波动环境成立。**所有 k 系数是先验，不做样本内寻优；研究阶段只报告敏感性（±），不做择优。**

## 3. 波段构建（确认驱动，非窗口驱动）

交易者说的"波段"是：价格朝一个方向推进到极值后，反向撤退足够深，使我们确信极值成立。"足够深"不能是固定根数，而是相对于当时波动率的幅度确认。

算法 `SwingTracker`（事件驱动状态机，记知时间如实标记）:

- 状态：当前腿方向 `dir ∈ {up, down}`，当前腿极值 `ext_price`、`ext_index`。
- 初始化：起始于第 0 根；方向由首次满足确认条件的反向撤退决定（第一腿方向 = 先走出 `k_rev·atr` 推进的方向）。
- 极值更新：up 腿中以 `high` 更新 `ext_price`。
- **确认**：up 腿中，当某 bar 满足 `ext_price - low(t) ≥ k_rev · atr(t)`，波段高点在 bar `t` 被确认：发射 `Pivot(price=ext_price, forming_index=ext_index, known_index=t, kind=high)`，方向翻转为 down，新腿从 `low(t)` 起。down 腿镜像。
- 同一根超宽 bar 同时含新极值与足够回撤：按 bar 路径启发式处理（收盘 < 开盘视为先高后低，反之先低后高；该启发式统一用于事件检测与回测撮合，见 §8.4)。

**先验：`k_rev = 1.0`。** 含义：反转幅度达到一个 ATR，才把前一个极值确认为波段端点。震荡里的小于 ATR 的抖动不会生成伪波段（这正是 pymarket-structure 用 TSI 符号分段、无幅度门槛所导致的单柱微波段缺陷的本项目修正，见 `docs/audits/pymarket-structure.md` §4）。

## 4. 关键价格区域（Zone)：形成、确认、生命周期

关键位置是有经济意义的价格区域：市场曾经在那里给出过明确反应（波段端点），或多次拒绝推进。区域不是任意窗口高低价。

### 4.1 生成

一个 confirmed pivot 生成一个区域：

- **阻力区**（来自 pivot high，价格 `P`, 知时 `atr`):`zone = [P - k_zw·atr, P]`
- **支撑区**（来自 pivot low)：对称，`zone = [P, P + k_zw·atr]`

**先验：`k_zw = 0.5`。** 区域是带，不是线——反映真实订单聚集在一个价格带而非精确价位。

- **合并**：新区域与现存同向区域重叠时合并：取并集边界，`touches` 相加，`origin` 保留最早者。
- 属性（不做主观评分，只记录事实）:`touches`（触碰次数）、`rejects`（被拒绝次数）、`origin_known_index`、`role ∈ {support, resistance}`、合并来源数。

### 4.2 状态机（生命周期）

```
ACTIVE ──价格从正面进入带内──> (测试,仍为 ACTIVE, touches+1)
   │ acceptance 突破（见 §5 E1 的破位判据） ▼
 BROKEN ──首次从远侧返回带内──> RETESTED
   ├─ 回踩被拒绝(§5 E3 于远侧成立)──> FLIPPED(角色互换,旧区 DEAD,生成反向新区并继承 touches)
   └─ 回踩后价格反向重新 acceptance 穿回──> DEAD
```

- **acceptance 破位**:bar 收盘越过区域外缘（阻力区：`close > zone.hi + k_bm·atr`；支撑区镜像），即"收盘接受于区域之外"，而非影线刺穿。**先验：`k_bm = 0.1`。**
- 角色转换（support↔resistance flip）是区域的核心语义之一：被市场接受的突破把旧的供给/需求关系逆转。

### 4.3 重要程度

`strength` 不设评分函数。交易规则可直接引用事实属性作过滤（"至少 2 次触碰"）。评分函数属于不可证伪的复杂度，v1 不做。

## 5. 市场状态（Regime)：趋势 / 区间 / 过渡

状态判断只使用已确认 pivot 序列（`known_index <= t`）与区域关系：

- 记最近 4+ 个交错 pivot 的高低点序列为 `H1,H2,...` / `L1,L2,...`（同为 confirmed)。相等容差 `tol = 0.1·atr`。
- **TREND_UP**：最近两个高点递升（`H2 > H1 + tol`）且最近两个低点递升（`L2 > L1 + tol`), 且价格未破坏最近抬升低点（见 E6)。**TREND_DOWN** 镜像。
- **RANGE**：最近 ≥4 个 pivot 的总跨度 `max(H) - min(L) ≤ k_span · atr` 且没有递升/递降结构，价格在该合成带来回。**先验：`k_span = 3.0`。**
- **TRANSITION**：以上皆否（典型：递升高点后出现更低低点，即结构开始矛盾）。
- 状态是新 pivot 确认或重大事件时重算的查询结果，不是粘滞标签；因此"区间→趋势"的转换天然由 E1/E6 表达，而非状态滞后翻转。

## 6. 市场行为事件目录（v1 冻结定义）

所有事件在 `known_index = t` 的 bar 收盘判出，仅用 `[0,t]`。

| ID | 名称 | 定义（上涨侧，下跌侧镜像） | 语义 |
|---|---|---|---|
| E1 | BREAKOUT_ACCEPTANCE 突破接受 | 收盘 `> zone.hi + k_bm·atr`，且随后连续 `N_a` 根 bar 全部收在 `zone.hi` 之上（事件于第 `N_a` 根收盘发射）。**先验 `N_a = 3`** | 价格被接受于区域之外；旧的拒绝失败，供给/需求关系可能逆转 |
| E2 | BREAKOUT_FAILURE 失败突破 | bar 极值越过 `zone.hi` 至少 `k_bm·atr`（刺穿），在 ≤ `N_f` 根内收盘回到 `zone.hi` 之下，且期间未发生 E1 接受。**先验 `N_f = 3`** | 扫过区域上方流动性后被拒绝（pymarket-structure 的 SFP 原语语义，本项目用区域+收盘重定义） |
| E3 | REJECTION 关键位拒绝 | 价格从正面进入 zone，未破位，在 ≤ `N_f` 根内出现反向撤退：自进入极值回退 ≥ `k_rj·atr`（以收盘或极值先到为准）。**先验 `k_rj = 0.3`** | 区域上的防守方仍在控制 |
| E4 | TREND_RESUMPTION 趋势恢复 | TREND_UP 中，回调腿深度 ∈ `[0.5, 2.5]·atr`（由已确认下跌 pivot 度量），且回调低点守住定义趋势的最近 `HL`，随后确认新的上升 pivot 且其 price > 回调腿起点高点的回收判据：收盘收复回调腿 50% | 趋势内回调结束、原推进方恢复 |
| E5 | RANGE_ROTATION 区间回归 | RANGE 状态 + 在合成带边缘区域发生 E3，且对侧边缘距离 ≥ `k_room·atr`。**先验 `k_room = 2.0`** | 平衡区内的均值回归运动空间充足 |
| E6 | STRUCTURE_BREAK 结构破坏 | TREND_UP 中收盘 < 最近 confirmed HL 的 price - k_bm·atr（事件于该收盘发射）；镜像 | 趋势假设被否定的第一证据（CHoCH，最先出场信号） |

审计借鉴关系：E2 综合 pymarket-structure(MIT) 的 SFP 拒绝原语与 quant-trading（专有，仅思想） 的"扫荡→内部位→收盘确认"三段式，以区域/收盘重定义并重写；E6 即 CHOCH 出场语义；E1 补其缺口（pymarket-structure 生命周期无"接受"语义）。所有实现为本项目原创代码。

## 6A. 精确定义附录（与 §4-§6 同等冻结效力）

**A1 区域的"面"**：对阻力区 `[lo, hi]`（hi = pivot 价）, 正面 = 下侧（价格自下方来）, 远侧 = 上侧；支撑区镜像。

**A2 触碰**：bar 的 `[low, high]` 与 zone 相交，且前一根收盘在正面之外（阻力区：`prev_close < zone.lo`)，记一次 touch。

**A3 进场事件（approach episode）生命周期**：一个 zone 从第一次触碰起处于"被接近"中；事件检测按 episode 去重——每个 episode 至多产生 E1 或 E2 或 E3 之一；zone 状态离开 ACTIVE(BROKEN/DEAD）或事件成交后 episode 结束。zone 返回 ACTIVE（新一轮从正面接近）才开始新 episode。

**A4 E1 判据**：阻力区上，`close(t) > zone.hi + k_bm·atr(t)` 触发破位候选（zone→BROKEN); 自候选 bar 起连续 `N_a` 根 bar 收盘全部 `> zone.hi` ⇒ 于第 N_a 根收盘发 E1。任一收盘跌回 `zone.hi` 之下 ⇒ 候选作废，回到 episode 检测。

**A5 E2 判据**：episode 内某 bar `high > zone.hi + k_bm·atr`（刺穿），自刺穿 bar 起 ≤ N_f 根内出现 `close < zone.hi` ⇒ 该收盘发 E2；若期间先满足 A4 破位候选，则 E2 检测终止（竞争以时间先后为准）。

**A6 E3 判据**：episode 内进入极值 `ex` 建立后，≤ N_f 根内出现从 `ex` 向正面的回退，幅度 `≥ k_rj·atr`（以 bar 收盘或极值满足先到为准）⇒ 发 E3;E1/E2 先发生则终止。

**A7 E4 判据（趋势恢复）**:TREND_UP；回调腿 = 最近确认的下跌腿，起点 `H_start`（前高 pivot)、终点 `L_pb`（最近确认的下跌 pivot low)，深度 `D = H_start - L_pb ∈ [0.5, 2.5]·atr` 且 `L_pb` > 定义本趋势的最近 HL pivot 价；随后 ≤ N_f 根内某 bar 收盘 `> L_pb + 0.5·D` ⇒ 该收盘发 E4（方向多）。TREND_DOWN 镜像。

**A8 合成带与 E5**:RANGE 状态由最近至多 8 个 confirmed pivots 判定；合成带 `band = [min(pivot lows), max(pivot highs)]`。E5 = RANGE 中在带边缘区域（满足 `zone 区域中心距 band 边缘 ≤ 0.75·atr`）发生 E3，且事件时价格至对侧边缘距离 `≥ k_room·atr`。

**A9 E6 判据**:TREND_UP 中，记最近一个确认低点 pivot（该趋势的 HL）价为 `L*`；某 bar 收盘 `< L* - k_bm·atr` ⇒ 该收盘发 E6（转空警告）。TREND_DOWN 镜像。

**A10 状态变迁的知时**:pivot/zone/regime 的全部状态变迁只在确认的 bar 收盘时点生效；`forming_index` 与 `known_index` 双记录贯穿全系统，测试对此有断言。

## 7. 交易假设（Hypothesis）与机会判断（Opportunity Reasoner)

每个交易计划是一个可证伪结构：

```
Hypothesis {
  direction: long | short
  event:     触发的市场行为事件 ID 与其属性
  entry:     事件已知后下一根 bar 开盘价（基准；不做挂单假设）
  invalid:   失效价 = 结构价格 ∓ buf（buf = k_bm·atr）——多头在区域下沿/前 HL 之下，空头镜像
  target_1:  最近的对侧区域边缘 / RANGE 对侧边缘 / 下一未测试区域（结构的，不是固定 R）
  r_mult:    (target_1 - entry) / (entry - invalid)
  regime:    事件发生时刻的状态快照
  notional:  由风险预算推出（§8.3)
  expiry:    假设失效：invalid 被触，或 E6 反向，或 N_time 根内 MFE < 0.5 个 R（先验 N_time = 24）
}
```

行动门槛（先验）：`r_mult ≥ 1.5` 才允许建仓。不交易本身是一等公民输出：无事件 / 状态 TRANSITION 且无事件 / r_mult 不足 / 风险预算被占用，系统输出 FLAT 并携带理由。

## 8. 持仓管理（Position Manager):持有、减、加、出

建仓后不停止决策。v1 机制化规则（先验，全部可在回放中逐条审计）:

1. **破位退出**：任何时刻收盘越过 `invalid` ⇒ 下一 bar 开盘退出全部（结构失效优先于固定止损）。
2. **触及 target_1**:bar 极值触及目标区域近缘 ⇒ 减仓 50%（下一 bar 开盘）, 剩余仓位的 invalid 移至 entry（保本）。
3. **移动防线**:每确认一个顺向新 pivot，剩余仓位 invalid 移至该 pivot 外侧 ± buf——最坏情况随市场证据改善，而非主观盯盘。
4. **加仓**：仅当出现同向新 E4 且持仓浮盈 ≥ 1R 时允许一次；加仓后合并仓位的最坏情景损失不得劣于加仓前（风险中性加仓约束）。
5. **E6 反向** ⇒ 全部退出（CHoCH 出场，借鉴 quant-trading 思想的重写实现）。
6. **到期**：假设 expiry 触发 ⇒ 退出并记录"时间止损"。

## 9. 研究协议（阶段 3，冻结）

### 9.1 事件研究流程（七步）

1. 用本文档冻结定义在**全历史**上以严格时间顺序标记事件。
2. 数据按时间每品种切分为 **dev 70% / test 30%**（分界点写入 manifest)。参数先验不寻优；dev 集仅用于发现实现错误与合理性检查。
3. 事件发生时，以事件假设方向，在下一根 bar 开盘建立理论头寸，invalid/target_1 按 §7 计算，不使用任何持仓管理（隔离事件本身的信号）。
4. 测量 H ∈ {6,12,24,48} 根内：MFE/MAE（以 atr 为单位）、命中 target_1 先于 invalid 的概率、毛期望、费用后净期望。
5. 对照组：同品种、同 regime、同方向、避开事件窗口的随机入场（42 个固定种子的 Monte Carlo), 测同样的指标。事件必须表现出**增量**信息，不仅是有方向的波动。
6. 统计不确定：按日分块 block bootstrap(2000 次）给净期望 95% CI；分前半/后半稳定性；按 regime 分解。
7. 成本压力：1x / 1.5x / 2x 成本下重算净期望。

### 9.2 成本模型（先验）

- Bitget 永续 taker 费 0.06% 每边（费率以抓取时交易所公布为准并记录）；滑点假设 0.02% 每边（1h bar 开盘、主流品种）; 资金费 0.01%/8h 按持仓时间双边保守收取（不预测方向）。单笔往返基础成本 ≈ 0.18%（费率+滑点），研究中按名义额精确计。
- 全部交易事件一律按 taker 成本计；若未来引入挂单，再设 maker 变体。

### 9.3 GO / NO-GO 判据（冻结）

对某机制（事件 × 方向），在 test 集上同时满足下列才算 GO:
1. 事件数 ≥ 60（全品种合计，允许逐品种 >10);
2. 1.5x 成本压力下净期望 > 0（按 ATR 与 USDT 双口径）;
3. block bootstrap 95% CI 下限 > 0(1x 成本）;
4. 3 个品种中至少 2 个净期望为正（符号一致性）。

任一不满足 ⇒ NO-GO，该机制只保留研究记录，不进入阶段 4 下单策略。若全部机制 NO-GO ⇒ 项目标记 **Research Only**，并如实报告失败原因。

### 9.4 诚实性约束

- dev 集上做过的任何实现修正都要记入 `docs/research-log.md`。
- test 集只允许一次最终评估；若被迫重评，必须显式声明并稀释结论强度。
- 不允许"改参数直到历史上盈利"；参数敏感性只用于报告稳定性。

## 10. v2 候选（明确不在 v1)

- 订单流微观确认：ATAS 式对角失衡 / "吸收→主动换边"（审计 OrderFlow-absorption 后判定思想可取需重写）；TapeFlow 的 VP(POC/VA/HVN/LVN）与 aggressor 判定在线计算（MIT，可借实现思路）。前提是 v1 价格行为层已证明存在优势，再检验其增量信息。
- 多尺度事件（15m 执行细化）、资金费真实历史代入、maker 执行变体。

## 11. 架构映射

| 职责（任务书§八） | 模块 |
|---|---|
| Market Observer | `market_operator/data`（抓取/校验/哈希）, `market_operator/feed`(bar 流） |
| Market Understanding | `market_operator/structure`(atr/swings/zones/regime) |
| Opportunity Reasoner | `market_operator/events`(E1-E6), `market_operator/hypothesis` |
| Position Manager | `market_operator/position` |
| Execution & Accounting | `market_operator/broker`（模拟撮合/费用/资金费/账本/权益/强平约束） |
| 研究与回测 | `market_operator/research`（事件研究）, `market_operator/backtest`（时间序回放+报告） |

单体、纯 Python（依赖仅 numpy/pandas/pytest/requests)、内存状态、事件驱动。禁止 Redis/消息队列/数据库/插件体系。
