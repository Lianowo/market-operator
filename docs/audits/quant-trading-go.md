# 开源算法审计：quant-trading-go

- 审计日期：2026-10-19
- 本地路径：`D:/LiAn/Web3Code/oss-review/quant-trading-go`
- 审计人：系统化交易研究（market-operator 前期审计）

---

## 1. 来源与许可证

**来源核实（不是"Go 重写的他人项目"）：**

```
git remote -v  → origin https://github.com/asifrahaman13/quant-trading (fetch/push)
git log -3     → 74ebfb7f 2026-04-16 "feat: new entry" (HEAD)
                 ade8ec2c 2026-04-15 "fix: fix choch"
                 1f17e2e2 2026-04-15 "Merge pull request #70 ... snipper"
git ls-remote  → 上游 HEAD = 74ebfb7fd8eba247aaeb4b1a0c992ebf739f753a == 本地 HEAD
```

结论：本地仓库就是 `asifrahaman13/quant-trading` 本身（Go module 路径 `github.com/asifr/quant-trading`，目录名 `quant-trading-go` 只是本地命名），且与上游同步到最新提交。它是一个面向印度/加密交易所（CoinDCX、Kite、Groww）的实盘自动交易系统，微服务架构（direction / liquidation / watchlist / execution / order_manager），用 Redis 在服务间传递方向、区域、持仓状态。

**许可证（重大注意点）：**

仓库内 `LICENSE` 是**私有专有许可证**（PROPRIETARY SOFTWARE LICENSE，Copyright (c) 2026 Asif R. All Rights Reserved），关键条款：

1. 禁止分发；
2. **禁止修改、翻译、逆向、创建衍生作品**；
3. 禁止复制（除单个备份）；
4. 禁止商业使用；
5. 保密条款（声称含商业秘密）。

对 market-operator 的含义：**只能阅读其算法思想并独立重写，不能逐行复制或做"衍生翻译"**。算法思想本身（BOS/CHOCH、FVG、流动性扫荡、成交量拒绝）均来自公开的 SMC/ICT 交易话语体系，独立实现无版权问题，但务必在报告中留痕此许可证状态。上游 README 宣称的 "License" 一节与 LICENSE 文件文本不一致时以文件文本为准。

---

## 2. 实际阅读的文件清单

深度阅读（全文或主体）：

| 文件 | 内容 |
|---|---|
| `internal/core/swing_analysis.go` | 波段点检测（对称 lookback + ATR 验证 + 假突破过滤） |
| `internal/core/market_structure.go` | 市场结构状态机（BOS/CHOCH/InternalLevel） |
| `internal/core/choch_detection.go`（前 250/861 行 + 结构浏览） | Structure Break 版 CHOCH |
| `internal/core/liquidity_sweep_detection.go`（全文） | 流动性扫荡版 CHOCH（失败突破） |
| `internal/core/volume_rejection_detection.go`（全文） | 成交量+长影线价格拒绝 |
| `internal/core/fvg_analysis.go`（全文） | FVG 检测与失效 |
| `internal/core/fvg_detection.go`（前 180/274 行） | FVG 过滤器/入场/止损管线 |
| `internal/core/atr.go`（全文） | ATR、操纵K线过滤 |
| `internal/core/liquidity_invalidation.go`（全文） | 区域失效规则 |
| `internal/core/orderblock.go`（头部算法说明与主循环） | Order Block 检测 |
| `internal/core/pattern_detection.go`（前 350 行） | CHOCH 模式包装、BOS 验证 OB |
| `internal/services/order_manager/choch_exit.go`（前 230/527 行） | 结构破坏式出场 |
| `internal/services/order_manager/partial_profit.go`（全文） | 分批止盈 |
| `internal/services/order_manager/trailing_sl.go`（全文） | MaxTP 分档止盈 |
| `internal/services/execution/trade_sl_tp.go`（前 200/479 行） | VP（成交量分布）止损 |
| `internal/services/execution/symbol_processor.go`（入场分支节选） | FVG/VR 入场执行 |
| `internal/services/direction/analyze_symbol.go`（全文） | HTF 方向分析入口 |
| `internal/services/watchlist/zone_validation.go`（前 120 行） | 区域验证编排 |
| `internal/services/liquidation/zone_generators.go`（头部） | POI 区域生成器 |
| `configs/scalping/service_*.yaml`（节选） | 默认参数 |

浏览未细读：`volumeprofile.go`（1024 行）、`poi_zones.go`、`bos_chain_aligned.go`、`watchlist/service.go`（1800 行）。

---

## 3. 核心算法逐一分析

### 3.1 波段高低点 `FindSwingPoints` — `internal/core/swing_analysis.go:13`

**(a) 数学定义**
输入：K线序列 `candles[0..n-1]`，`SwingConfig{Lookback L, FilterManipulation, ATRSwingValidation, ATRSwingMultiplier k, ManipulationWickThreshold w_th, ManipulationATRMultiplier a_th}`。
- 波段高点：当且仅当对所有 `j ∈ [i-L, i+L], j≠i`，`effHigh[i] ≥ effHigh[i+j]`，且等值时取最左者（`(neighborHigh == currentHigh && j < 0)` 否决右侧等值点）。
- 等高点：`effHigh[i] = H[i]`；若开启 `FilterManipulation` 且 K线满足 `WickRatio > w_th（默认0.65）且 Range ≥ a_th·ATR（默认3）`，则用实体端点 `BodyHigh/BodyLow` 替代影线端点（`atr.go:62 GetEffectiveHighLow`）。
- 可选 ATR 验证：要求波段点两侧各自出现 ≥ `k·ATR` 的反向位移（对波段高点：左右两侧最低 low 均须低于 `swingLow_i − k·ATR`），且**左侧也要看**——即右侧确认、左侧也要满足。

输出：`[]SwingPoint{Index, Price}`（高/低各一列）。ATR 为 period 期 SMA 型 TR 均值（`atr.go:11`，前 period-1 根用递推均值热启动）。

**(b) 交易语义**：经典分形波段。操纵过滤试图剔除"插针扫止损"的孤立长影线——把影线极端我当噪声，只认实体推进的极端点，这与市场微观结构里 stop-hunt 的意义相反：它选择在结构图上忽略插针（把插针留给 liquidity-sweep 模块单独处理）。双侧 ATR 位移验证试图保证波段"显著"，即波段之间至少有一次像样的回调。

**(c) 参数敏感性**：`L` 直接决定结构尺度（scalping 配置 `choch_swing_lookback: 5`），确认延迟恒为 L 根；`ATRSwingValidation` 的非对称要求（左右都要 k·ATR）在长趋势初段会大量误杀真实波段点（趋势启动侧没有深度回撤），参数 `k` 与 L 耦合——k 大则两侧各需 ≥2L 区间容纳位移。

### 3.2 市场结构状态机 `AnalyzeMarketStructure` — `internal/core/market_structure.go:27`

**(a) 数学定义**：先调 `FindSwingPoints`（仅 Lookback，无 ATR 验证/操纵过滤——注意与 3.1 的可选项在这里被关闭）得到全量波段，冒泡排序（O(S²)）后：
- 初始方向：第一个波段高点与第一个波段低点的先后关系决定（高先现→bearish；低先现→bullish）。
- 逐级扫描 K线，`i` 到达某波段索引时"揭示"该波段为 `latestSwingHigh/Low`。
- 趋势内：`Close` 突破（bullish: `Close > latestSwingHigh.price`）→ 记录 BOS 事件并更新 protected 对侧波段。
- 反向：`Close` 突破 protected 波段 → 记录 CHOCH 事件并翻转方向、交换 protected。
- 同一波段只触发一次 BOS（`isSwingAlreadyBroken`）。
- InternalLevel：相邻两个 BOS 之间的极值（bearish 链取最高点，反之取最低点），`market_structure.go:292-345`。

输出：最终 Direction、事件序列、BOS 级别、内部级别。

**(b) 交易语义**：教科书 SMC 的市场结构定义：HH/HL 延续即 BOS，最近 protected 摆动被收盘击穿即 CHOCH（方向改变），internal level 受托观测次级结构。用 **收盘价** 而非影线判突破——抗影线噪声，但放弃盘中反应速度。

**(c) 参数敏感性**：实质上只有一个参数 `SwingLookback` + `MinSwings`。方向判定对序列**头部样本敏感**（初始方向由最早两个波段定夺，旧数据质量差会污染全段）；`isSwingAlreadyBroken` 依赖 SwingIndex 相等比较，配合"最左等值"规则稳定。

### 3.3 Structure Break CHOCH `DetectCHOCH` / `detectBullishCHOCH` — `internal/core/choch_detection.go:16/99`

**(a) 数学定义**（以看多 CHOCH 为例，即 HTF 看多、等待回调结束）：
1. 锚点 B1 = `[startIdx, n)` 区间内**最高点**的 low（`findHighestHigh`，全窗口极值）；
2. BOS 链：从 B1 起找首个 `Close < currentLevel` 的K线，通过波段检验（左 L 根内最低、右侧在数据允许范围内最低——**右端数据不足时放宽**，见 4.1）后入链，`currentLevel` 更新为该K线的 low，迭代；
3. InternalLevel = 相邻 BOS 间的最高点，要求回撤幅度 ≥ `CHOCHInternalRetracementPct`（默认 50%，`calculateInternalLevelsWithValidation`）；
4. CHOCH：某K线 `Close >` 最后一个有效 internal high，且距最后 BOS ≤ `MaxAgeAfterBOS`、距当前 ≤ `MaxAgeCandles`；
5. 输出 POI 区、protected swing、突破信息。

镜像函数 `detectBearishCHOCH` 完全对称。

**(b) 交易语义**：HTF 顺势 + LTF 回调结构 reversals 确认。"回调（逆势 BOS 链）→ 回调内部结构被反向击穿 = 回调结束、顺大势恢复"。本质上是一个**带结构确认的回调入场**，试图回答"这是回调还是反转"。

**(c) 参数敏感性**：`RetracementPct=0.5` 是最强约束——回撤不足 50% 的浅回调永远没有"有效 internal level"，信号被系统性推迟到深回调；`MaxAgeAfterBOS`（配置 20）决定结构"保鲜期"；B1 取全窗口极值使结果对 `CHOCHAnalysisCandles`（分析窗口长度）非常敏感。

### 3.4 流动性扫荡 CHOCH `DetectLiquiditySweepCHOCH` — `internal/core/liquidity_sweep_detection.go:24`

**(a) 数学定义**（以 bearish 扫荡为例）：
1. BOS 链同 3.3（逆势方向建链，锚点 = 窗口最低点 `findLowestLow`）；
2. **扫荡K线 = 最后一个 BOS 之后直至数据末尾的最高点所在K线**（`extremeIdx`，全后缀极值），且须满足 `High ≥ lastBOSHigh`（影线越过前高流动性池）且 `Close < lastBOSHigh`（收盘回到池内 = 假突破）；
3. internal low = [lastBOS, sweep] 区间最低 low；
4. 确认K线：sweep 之后首个 `Close < internalLow` 且龄期 ≤ `maxAge`；
5. POI 区 = [sweepK线, breakK线] 的 high 与两K线 low 的较小者。

**(b) 交易语义**：教科书式 stop-hunt / 失败突破：价格刺破前高（收割挂在 high 上方的空头止损与追涨 breakout 买单uition），收盘回到区间，再击穿回调内部低点确认 reversal。POI 区=扫荡K到确认K的范围，作为后续限价回补区。这是对"关键位置 + 假突破 + 结构确认"三段论的完整工程化。

**(c) 参数敏感性**：`swingLookback=5` 控制 BOS 链质量；`maxAge`（scalping 默认经 `Patterns.LiquiditySweep.MaxAgeCandles`，回退 100）控制信号时效。**整段模式由"全后缀极值"锚定，新数据到来时 extreme candle 会迁移**——见 4.2。

### 3.5 成交量价格拒绝 `DetectVolumeRejection[InZone]` — `internal/core/volume_rejection_detection.go:31/208`

**(a) 数学定义**：
- 均量基准：`avgVol = SMA(volume, VolumeAvgPeriod=20)` 取序列末端、**排除最后一根**（`volume_rejection_detection.go:391`）；
- 拒绝K线：`Vol ≥ RejectionVolumeMult(2.0)·avgVol` 且 方向相反侧影线占全幅 ≥ `RejectionMinWickPct(0.30)`，且该K线必须是**全部样本**中的最高/最低点（`isHighestInRange:416` 扫描 `candles` 全序列，两端都查）；
- 可选（InZone 版）：影线须触及给定区域（`High ≥ zoneLow` / `Low ≤ zoneHigh`）；
- 确认K线：拒绝K线后 ≤ `ConfirmationLookback(5)` 根内，`Vol ≥ 1.5·avgVol`、`Body/Range ≥ ConfirmationMinBodyPct(0.80)` 且颜色同向；
- 交易级别：Entry = 确认K收盘；SL = 拒绝K影线极端。

**(b) 交易语义**：在流动性区域处的放量长影线 = 主动买方/卖方吸收（absorption）+ 追单被套；随后放量长实体确认K = 拒绝后一方接管。与 tapeflow 类项目的"吸收"概念同族，但完全基于K线集计（无成交明细）。Entry/SL 定义干净、风险距离天然等于影线长度。

**(c) 参数敏感性**：2×/1.5× 放量、30% 影线、80% 实体四个魔数均无文档依据；均量窗口 20 对加密 7×24 成交量周期（亚洲/欧美盘轮换）太短，易把"正常放量时段"误判为异常；"全样本极值"过滤极强——只交易整个已加载窗口的绝对顶/底，随窗口扩大信号条件改变（4.2）。

### 3.6 FVG 与区域失效 — `internal/core/fvg_analysis.go:51` + `liquidity_invalidation.go`

**(a) 数学定义**：
- Bullish FVG：`c3.Low > c1.High` 且 `c2` 为阳线，`gap = c3.Low − c1.High ≥ MinGapATRPct·ATR`；可选 impulse 量能与影线过滤；失效：`∃ j>i: Low_j < gapBottom`（只认影线全穿，`isGapTestedBullish:186`）。
- 入场价 = `GapBottom + entryLevelPct·gap`（FVG 中点类，`fvg_detection.go:134`）；SL = 对侧 zone edge，无 buffer（`fvg_detection.go:144`）。
- 区域失效（`liquidity_invalidation.go:104 CheckLiquidityZoneInvalidation`）：规则1 = 收盘击穿整个区域；规则2 = 触碰区域的K线形成后，反向回撤越过该K线振幅的 `SwingRetracementPct`（60%）。

**(b) 交易语义**：失衡区回补/一线支撑阻力假设；失效规则是"区域生命周期"的简化版（仅"失效"终态，无"进行中/已缓解/衰减"中间态）。

**(c) 参数敏感性**：`MinGapATRPct` 与 ATR 窗口耦合；失效用影线（而非收盘）使 FVG 偏脆弱——与 zone invalidation 用收盘的规则相互不一致。

### 3.7 持仓管理

**CHOCH 出场**（`order_manager/choch_exit.go:46`）：以入场K线为第一 BOS，顺持仓方向滚动建 BOS 链（收盘判定），取最后两个 BOS 间 internal level，**任何已收盘K线反向收破 internal level → 立即市价退出**。只用 `[:len-2]` 已完结K线，因果性干净。语义=用持仓后市场微观结构的变化做离场（而非静态止盈），结构破坏即立场失效假设。

**分批止盈**（`partial_profit.go:41`）：tier 表 `(MinProfitPct, MinRRMultiple, ClosePercent)`，R 倍数基于**原始 SL 距离**（存 Redis 的 `OriginalSLDistancePct`，避免移动 SL 后分母漂移——细节正确），每 tier 只执行一次、状态存 Redis（7 天 TTL）。

**MaxTP 分档**（`trailing_sl.go:44`）：按原始 SL 距离档位给出固定利润百分比（如 2.5%）强制落袋——等价于分级限价 TP。文件中注明传统 trailing SL 已被 CHOCH 出场取代。

**VP 止损**（`execution/trade_sl_tp.go:37`）：从最近 BOS K线到当前构建 Volume Profile，把 SL 放在 HVN 边界外侧——"止损放在成交量密集区之外"的机构式论据，工程上是 `trade_sl_tp.go` 中最厚的部分。

### 3.8 系统级交易流程（管道）

`direction` 服务（3.2，HTF 分钟级重算、Redis TTL 发布）→ `liquidation` 服务生成 POI 区（OB/FVG composite）→ `watchlist` 服务在区域内检测 CHOCH/成交量拒绝并做区域失效过滤 → `execution` 服务做"price reached zone → 市价入场"或 LTF CHOCH 二次确认入场（`symbol_processor.go:410-477`），SL=模式固有极端，TP 由 order_manager 的 CHOCH 出场+分批+MaxTP 组合管理，方向只许顺 HTF（`pattern_detection.go:95` 硬编码 HTF bias 门）。

---

## 4. 未来函数与重绘审查

### 4.1 未来函数（lookahead）

**存在，且多处，均未如实标记确认延迟：**

1. `FindSwingPoints` 是对称窗口分形，波段在 i 处成立须等到 i+L 才可知晓。**`SwingPoint` 结构没有"确认索引"字段**；`AnalyzeMarketStructure` 的扫描循环在 `i == swing.index` 时即"揭示"波段（`market_structure.go:156-164`），随后同一根 K线就允许用它判 BOS。对整个历史窗口运行得到的 BOS/CHOCH 事件时间戳等价于把波段知识提前了 L 根。若只取窗口末端的最终方向状态供实盘，偏差有限（尾部 L 根本来就无波段）；但任何对 `StructureEvents` 做回放/回测的用法都会被 L 根的提前量污染。
2. `detectBullishCHOCH/detectBearishCHOCH` 中 BOS 波段右侧检验"数据不足时放宽"（`choch_detection.go:178-189`：`rightEnd = min(bosIdx+L+1, n)`）——越靠近数据末端，波段检验越弱，等价于用未来 L 根（当它们存在时）事后否决先前已接受的 BOS。跨周期重跑会改变历史事件集合。
3. 流动性扫荡的 sweep candle 定义为"BOS 之后**到数据末尾**的极值点"（`liquidity_sweep_detection.go:221-228, 457-464`）——在 t 时刻无法知道后续是否还有更高点；该定义只有在"每次对最新全量快照做一次性扫描"语义下才自洽，作为事件序列回放是非因果的。
4. 成交量拒绝的"全样本最高/最低"过滤（`isHighestInRange/isLowestInRange:416-456`，循环 `for i := 0; i < len(candles); i++`，**包含拒绝K线之后到当前的所有K线**）。拒绝K线 t 处该谓词引用了 (t, now] 的数据，确认K收盘时（≤t+5）部分可得、更长窗口部分不可得。属于未标记的确认时延/非因果过滤。
5. FVG 的 `isGapTested*` 用全部后续K线判失效——作为"当前是否还值得交易"的过滤完全正当（本来就是为了过滤已失效区域），但若把检测结果当历史信号流则非因果。定位介于上述 3、4 与真正硬缺陷之间。

**对照正面案例**：`CHOCHExitCalculator` 明确排除进行中的K线（`currentIdx = len(candles)-2`）、只用入场时刻之后的序列、无向前扫描——因果处理是本仓库中做得最好的模块。

### 4.2 重绘（repainting）

**存在，机制明确**：所有检测器都是"每次调用对完整最新窗口重新计算"的快照语义，无增量状态：

- 3.4 的 POI 区端点为 extreme candle，新K线创出新高/新低则 extreme 迁移，同一交易计划的 POI 边界两次扫描之间会移动；
- 4.1(2) 的右端放宽使最近的 BOS 级别在随后 L 根内可被"撤销"；
- 3.5 的全样本极值过滤随窗口加长（数据服务每次 fetch 500–1500 根）改变信号判定。

实盘后果：watchlist 中已发布的区域/信号可能在下一轮扫描时消失或换位置，执行层以 ticker 命中区域时当下的数据为准，等于用未来数据确认的计划在成交时可能已经不是同一个计划。仓库无任何"信号落库后不可变"的保护（Redis 覆盖写）。

### 4.3 判定小结

该系统**只能按"实盘快照扫描器"理解才没有致命偏差**；它的所有历史事件产物（StructureEvents、BOSLevels、POI zones）都不适合直接用于回测或离线统计，除非自行加上确认时延标注。README 与代码均未声明这一点。

---

## 5. 不合理假设

1. **隐含成交价**：入场逻辑为"价格进入区域 → 市价单成交于 ticker 最新价"（`symbol_processor.go:420-444`）。止损、R 倍数、分批止盈全部按纯价格百分比计算，任何位置未见手续费、滑点、资金费率建模（`tests/services/execution/` 的 risk test 也只算数量不含成本）。对 scalping 档（5m/1m 结构）这是量级级的失真。
2. **固定窗口假设**：`maxAgeCandles=100`、`MaxAgeAfterBOS=20`、均量 20、FVG 年龄 30 等全是K线数而非波动率时间或真实时间；不同波动期结构寿命差数倍，固定K线龄期没有依据（与他们自己的 ATR 思路都不自洽）。
3. **魔数无出处**：2×/1.5× 放量、30% wick、80% body、50% 回撤、60% 失效回撤、65% 操纵影线、3×ATR——任何注释/文档/测试都未提供校准依据，也无回测框架支撑调参。
4. **单周期窗口极值锚定**（`findHighestHigh/findLowestLow`、全后缀 extreme）：结构叙事完全由分析师手选窗口决定，`CHOCHAnalysisCandles` 改 20% 结论就变，无稳健性检验。
5. **收盘突破唯一论**：BOS/CHOCH/失效全部以 Close 判定，加密 1m/5m 上长影线插针+收盘回归极常见，这会系统性迟到；但方向性假设本身是合理选择（取舍），问题在没有提供 wick 判定的对照与敏感性。
6. 初始方向由最早两个决定（3.2b），窗口起点任选——方向状态对 startIdx 不稳定。
7. 无幸存者偏差问题（实盘系统无选股逻辑），但其 watchlist 以交易所全量合约为池，`max_workers: 400` 全量扫描，隐含"所有流动性稀薄小币的K线形态等价有效"假设。

---

## 6. 代码质量与测试

**测试结果（本机 go1.26.2 windows/amd64）：**

```
go test ./tests/core/ ./tests/models/ ./internal/models/  → 全部 ok
go test ./... → tests/* 全部 PASS（config, core, models, services×7, integration）
                server 包 BUILD FAILED：
                internal/exchange/kite.go:220,223 使用了 syscall.Flock/LOCK_EX/LOCK_UN（Unix-only）
```

即：**除 `server`（Web 仪表盘）在 Windows 上因 Unix syscall 无法编译外，全部测试通过**。算法层 tests/core 含 79 个测试函数（atr 7、choch 13、fvg 15、liq_invalidation 6、orderblock 11、pattern 14、poi 4、swing 20、volumeprofile 13 减去 helpers 0，粗计≈93 处 `func Test` 计数含子文件），覆盖各检测器的正反样本，还有 e2e pipeline 测试。但**没有任何测试检验因果性/重绘**（如"用 t 时刻截断数据重跑，断言 t 之前的信号不变"）——这正是 4.1/4.2 问题能在测试绿灯下存在的原因。

**质量观察**：
- 优点：接口/实现分离（`interfaces/` + 编译期断言）、服务化清晰、参数全部进 YAML、debug 日志结构化。
- 缺点：`market_structure.go` 手写冒泡排序；`min/max/abs` 在 core 包私有重复定义（Go 1.21+ 已有内建 min/max）；`watchlist/service.go` 1800 行、`symbol_processor.go` 978 行的巨石；同一 BOS 链构建逻辑在 `choch_detection.go`、`liquidity_sweep_detection.go`、`choch_exit.go` 三处近似复制（去重失败是"fix: fix choch"这类提交出现的土壤）；emoji 日志满地。

---

## 7. 借鉴价值判定表

| # | 算法/模块 | 判定 | 理由 |
|---|---|---|---|
| 3.2 | 市场结构状态机（BOS/CHOCH/protected swing） | **有缺陷但思想可取** | SMC 结构化的最简正确骨架；事件时延未标注是缺陷，加"确认索引"即可修复。能解释真实行为（结构破坏=筹码换手点），复杂度恰当 |
| 3.4 | 流动性扫荡 CHOCH | **值得借鉴（重写）** | 与 market-operator 的"失败突破"方向几乎同构：扫荡（影线过池+收盘回内）→ internal level → 收盘确认，三段式清晰且每段有真实微观结构含义**；但必须改为因果实现（sweep 用滚动极值、POI 冻结于确认K）而非全后缀极值 |
| 3.5 | 成交量拒绝 | **有缺陷但思想可取** | 放量长影线+放量同向实体确认是真行为代理；SL 定义优雅。全样本极值过滤必须删除（非因果且过强），四魔数需自校准 |
| 3.1 | 波段检测+操纵过滤 | **有缺陷但思想可取** | 对称分形是基础件；"长影线异常K线用实体端点"的操纵过滤是少有人写的细节，适合融入我们波动率自适应波段。双侧 ATR 验证（尤其左侧要求）会误杀趋势段，应只保留右侧确认 |
| 3.7 | CHOCH 出场 | **值得借鉴** | 因果干净、语义强（持仓假设=结构延续，结构破坏即退出），比 ATR trailing 更有解释力；与 partial tier 组合即为完整离场框架 |
| 3.7 | 原始 SL 距离入 Redis 算 R 倍数 | **值得借鉴** | 防分母漂移的工程细节，小但正确 |
| 3.7 | VP-HVN 止损 | **有缺陷但思想可取** | "止损避开量密集区"合理；实现重（500-1500 根 VP per trade），在确认其增量价值前不上同等复杂度 |
| 3.6 | FVG/区域失效规则 | **有缺陷但思想可取** | 失效规则 1（收盘全穿）简单可用；FVG 本体属 ICT 假设，解释力弱于扫荡/拒绝，只做备选 |
| 3.6 | 回撤 60% 失效规则 | **不值得借鉴** | 以触碰K自身振幅定义回撤基准，量纲混乱（K线振幅≠其后的 swing），60% 无出处 |
| 3.3 | 全窗口极值锚定 B1 的 BOS 链 | **不值得借鉴** | 锚定于任意窗口极值使整套结构叙事对窗口敏感；应与 3.2 的滚动结构合并而非另起炉灶 |
| — | 微服务+Redis 管道形态 | **不值得借鉴** | 对 market-operator 单进程研究方向是过度工程；参数/状态跨服务序列化徒增复杂度 |

---

## 8. 与本项目方向的差异

对照 market-operator 计划（波动率自适应确认波段、关键价格区域生命周期、突破接受/失败、拒绝、结构破坏事件）：

1. **波动率自适应确认波段 vs 其 3.1**：它有 ATR 验证与操纵过滤的雏形，但确认语义是"双侧同时显著"而非"右侧确认+时延标注"，且无确认索引。本质差异：**我们要把"波段在 t+L 才可确认"作为一等公民写进数据结构**，它把这 L 根静默吞掉。
2. **关键价格区域生命周期 vs 其 3.6**：它只有生成+二元失效（收盘穿破），无"生成→测试→缓解→衰减→失效"的状态机，且 FVG 用影线判失效与 POI 用收盘判失效自相矛盾。我们的生命周期模型是它的超集；其"收盘判失效"与"区域龄期"两条规则可直接吸收。
3. **突破接受/失败 vs 其 3.3/3.4**：它只有"失败"一支（sweep+收盘回内=拒绝；收盘过 internal=确认 reversal），**没有"接受"概念**（突破后连续收盘/回踩站稳视为真突破）。BOS 即"收盘过 level"隐含了单根收盘接受判据。我们要显式建模接受 vs 失败的二分支，它把接受当成默认、只检测失败——这是覆盖面差异。
4. **拒绝 vs 其 3.5**：形态学上最接近的一套；差异在它的确认只看下一根内的量价，没有吸收/失衡更细的代理，也没有与非因果极值过滤器解耦。
5. **结构破坏事件 vs 其 3.2/3.7**：事件模型（BOS/CHOCH + protected swing）几乎可直接映射为我们的事件 schema；它的 CHOCH 出场证明了"结构事件同时驱动入场过滤与离场"的闭环价值。差异在时延标注与重绘保护（信号/区域一经发布不可变）——这是 market-operator 必须自建的两条纪律，本仓库完全没有。

**总评**：这是一个"思想语料库质量高于工程因果性"的项目。SMC 概念工程化得最完整的部分是 liquidity-sweep→internal-level→close-confirmation 的三段式和 CHOCH-based 出场；最不可接受的是普遍的全窗口/全后缀极值锚定带来的非因果与重绘。借鉴方式应限于"语义骨架 + 参数清单作为先验"，全部用因果、带确认时延、事件不可变的方式重写。

---

## 附：复现命令记录

```bash
git -C oss-review/quant-trading-go remote -v
git -C oss-review/quant-trading-go log -3 --format='%H %ad %s'
git ls-remote https://github.com/asifrahaman13/quant-trading | head -5
cd oss-review/quant-trading-go && go test ./...   # tests/* PASS, server 包 Windows 构建失败(syscall.Flock)
```
