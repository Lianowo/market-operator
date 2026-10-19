# 开源算法审计报告：OrderFlow-absorption

- 审计日期：2026-10-19
- 审计人：系统化交易研究（子代理）
- 审计方法：git 溯源 + 全部核心源码逐行阅读 + 合成数据端到端烟雾测试

---

## 1. 来源与许可证

| 项 | 值 |
|---|---|
| 实际远端仓库 | `https://github.com/Mamallann/OrderFlow-absorption`（`git remote -v` 证实） |
| HEAD 提交 | `d7599d5d32b91e4939c65303716b2020a2ed83f7`（2025-11-23，“fix: Add rate limit handling with exponential backoff”） |
| 提交历史 | 全部 6 个提交，集中在 2025-11-23 一天内（初始实现 + 5 个 fix），无后续维护 |
| LICENSE 文件 | **不存在**（`git ls-files | grep -i license` 无结果） |
| 许可证声明 | README 末尾自称 “MIT License”，但仓库中没有 LICENSE 文本 |

**许可证结论（重要）**：README 口头声称 MIT，但缺少标准 LICENSE 文件。从严格法律角度看，这等于**未授予任何许可**（声明与文本不一致时以缺失文本为准，默认保留所有权利）。借鉴“思想”不构成版权问题；但若照搬代码片段存在合规风险。本项目只应借鉴算法思想并独立实现。

代码规模：约 5000 行 Python，纯批处理（backtest-only），无 live 执行能力。

## 2. 实际阅读的文件清单

逐行完整阅读：

- `src/footprint/builder.py`（Footprint 构建，315 行）
- `src/footprint/imbalance.py`（对角失衡检测，352 行）
- `src/footprint/delta.py`（Delta / 背离，205 行）
- `src/footprint/structures.py`（数据结构，218 行）
- `src/strategy/absorption.py`（吸收检测，487 行）
- `src/strategy/shift.py`（控制权转移检测，395 行）
- `src/strategy/signals.py`（信号生成 / 入场 / 止损 / 止盈定价，371 行）
- `src/strategy/rules.py`（信号过滤器，204 行）
- `src/backtest/engine.py`（回测主循环，395 行）
- `src/backtest/risk.py`（SL/TP/追踪止损，389 行）
- `src/backtest/position.py`（持仓与成交记录，286 行）
- `src/backtest/metrics.py`（绩效指标，前 120 行 + 关键计算路径）
- `src/data/binance_client.py`（Binance aggTrades 下载，373 行）
- `src/data/data_loader.py`（数据加载，289 行）
- `src/config.py`（配置解析，关键段落）
- `config.yaml`、`scripts/run_backtest.py`、`README.md`、`requirements.txt`

可执行验证：用合成 tick 数据在本机（Python 3.14）端到端跑通了 footprint 构建 → 失衡检测 → 吸收 → 转移 → 信号 → 成交全链路（详见 §4、§6）。

## 3. 核心算法逐一分析

### 3.1 Footprint 构建（`builder.py`）

**(a) 数学定义**
输入：Binance aggTrades tick 流 `(price, quantity, timestamp, is_buyer_maker)`。输出：`FootprintCandle` 序列。
- K 线分桶：`ts_candle = floor(ts / TF_ms) * TF_ms`（`_build_candle_from_group`，builder.py:153）。
- 价格量化：`level = floor(price / tick_size) * tick_size`，默认 `tick_size = 100`（美元固定步长，builder.py:59-69, 157）。
- 主动盘分类：`is_buyer_maker=True`（买方是 maker ⇒ 卖方主动砸向 bid）计入 `bid_volume`；反之计入 `ask_volume`（builder.py:204-209）。语义正确（对应 aggTrades 的 `m` 字段，是**真实 taker 方向**，不是 tick-rule 推断）。
- Delta：`Δ = Σ_level (ask_volume − bid_volume)`（`footprint/structures.py:28-30`，正 = 净主动买入）。
- POC：`argmax_level(total_volume)`（builder.py:215）。

**(b) 交易含义**：还原每根 K 线内部各价位上的主动买卖对抗强度，是 ATAS/Exocharts 类足迹图的裸实现。

**(c) 参数敏感性**：`tick_size` 是全局固定美元值——BTC@60k 时约 0.17%/档可用，但 config 把同一 `tick_size: 100` 同时套在 BTC/ETH/SOL 上，**对 SOL（$100–200 价位）等于只留 1–2 个足迹档，失衡检测完全退化**。这是参数化设计缺陷。`timeframe` 固定枚举，10m 对 BTC 流动性合理。

### 3.2 对角失衡检测（`imbalance.py`，ATAS 风格）

**(a) 数学定义**
- BID 失衡于价 P：`bid_volume[P] > ratio × ask_volume[P+tick]`（imbalance.py:67-118），对角项为 0 时记 ratio=∞（存为 999.0，imbalance.py:104, 111）。
- ASK 失衡于价 P：`ask_volume[P] > ratio × bid_volume[P−tick]`（imbalance.py:120-169）。
- 连续分组：相邻失衡价差恰为 `tick` 时归入同组，组内每行标 `consecutive_count=len(group)`，仅保留 `len(group) ≥ consecutive_rows` 的组（imbalance.py:171-222）。

**(b) 交易含义**：ATAS 对角比较是足迹分析的事实标准——在哪一侧的市价单流量压倒对方对角流量。连续 3 档失衡相当于“一堵被单边流量贯穿的价位墙”，是吸收/堆叠失衡（stacked imbalance）的代理。语义本身是成立的，与专业 DOM/footprint 实践一致。

**(c) 参数**：`ratio_threshold=3.0`（业界常用 3–4 倍，合理）、`min_volume=1.0`（固定币量，未随 symbol/波动率自适应，ETH/SOL 上量级失配）、`consecutive_rows=3`（每提高 1 会指数级减少信号数，敏感）。注意 `config.yaml` 的 `diagonal_offset: 1` 是**死配置**，实现里硬编码为 1 档。

### 3.3 Delta 分析（`delta.py`）

**(a) 数学定义**
- 累计 Delta：`cumΔ_t = cumΔ_{t-1} + Δ_t`，可选按日重置（delta.py:24-54）。
- Delta 背离（`compute_delta_divergence`, delta.py:56-110）：阴 K 且（`Δ_t > 全部前 N 根 Δ` 或 `Δ_t>0`）⇒ 看多背离，强度 `= |Δ_t − mean(Δ_prev)| / |mean(Δ_prev)|`；镜像为看空。
- Delta 相对强度（`is_delta_larger_than_previous`, delta.py:112-171）：`|Δ_t|` 对前 N 根做 `both/any/average` 比较。

**(b) 交易含义**：“价跌但主动卖压在衰竭/主动买盘更强”是吸收的标准量价背离叙事。方向正确。
**(c) 参数**：`lookback=2` 过短，背离强度以均值的相对差来衡量，前 N 根 Δ 均值接近 0 时强度爆炸（无正则化）。`window=20` 统计仅供报表。

### 3.4 吸收检测（`strategy/absorption.py`）

**(a) 定义**（以 `detect_sell_absorption`，absorption.py:86-166 为例，做多前的“卖吸收”）：
1. 当前 K 内存在 `consecutive_count ≥ min_imbalance_rows(3)` 且 `ratio ≥ min_imbalance_ratio(3.0)` 的 BID 失衡（`_check_bid_imbalance`, absorption.py:249-261）——即大量主动卖出；
2. Delta 条件（`_check_delta_condition`, absorption.py:276-332）：`|Δ_t|` 大于前 `delta_lookback=2` 根（按 `delta_comparison` 模式）；
3. 价格守住（`_check_price_holds_low`, absorption.py:334-375）：`low_t ≥ low_{t-1}`（`allow_equal_lows`），或“wick 拒绝”；
4. 置信度打分（`_calculate_confidence`, absorption.py:420-456）：失衡质量 0.4 + delta 0.3 + 价格行为 0.3，硬编码加权和，阈值 0.5。

**(b) 交易含义**：吸收（absorption）叙事的标准定义——大量主动卖单砸向低位但价格不再创新低且有被动买盘承接，卖方流动性被“吸收”，预期反转向上。这个两阶段（先吸收、后确认）的叙事本身**与真实市场微观结构相符**，是本项目最接近“市场行为事件”的部件。

**(c) 参数与脆弱点**（实测验证，见 §4/§6）：
- **Delta 符号检查是死代码**：absorption.py:311-315 注释写“Don't return False immediately”，两个分支均 `pass`，实际只比较 `|Δ|`。README 宣称的“negative delta（卖压）”约束**并不存在**——一根巨量正 Delta 阳线同样满足“卖吸收”的 delta 条件。语义与实现脱节。
- **wick 拒绝是退化条件**：absorption.py:360-368 先把“下影线占 full range ≤ 0.3”称为 rejection（方向本身就反了——下影线长才像拒绝），随后 `if close > midpoint: wick_rejection = True` **无条件覆盖**前述计算，于是 wick 拒绝 ≈ “收盘在区间上半部”。config 注释“Wick must be < 30% of candle body”与实际除以 range 的分母也不符。
- **价格守住只用前一根 K 的 low** 作参照，没有任何摆动点/关键区域概念；噪声导致前一根 low 略低就会否决掉结构上完全合理的吸收（我在合成数据上复现：3 档失衡 + Δ=−11.4 仍被否）。
- 置信度各项加分常数（4 行 +0.1、ratio≥10 +0.05 等）是任意拍脑袋值，无标定。

### 3.5 控制权转移检测（`strategy/shift.py`）

**(a) 定义**：在吸收 K 之后 1..`max_candles_after_absorption(3)` 根内逐根检查（`detect_bullish_shift`, shift.py:71-114）。看多转移的三个可配置条件：出现 ≥1 个 ASK 失衡；Δ>0（可选）；“买主动”（任一价位 `ask_volume > bid_volume`，shift.py:198-206）。全部必需项满足且打分 ≥0.4 即成 `ShiftEvent`，即入场信号。

**(b) 交易含义**：“吸收之后主动性换边” = 微观结构版的“拒绝失败突破后反转启动”。两阶段设计（先等吸收、再等主动换边）是对的，**避免了单纯见失衡就逆势进场的常见错误**，这是本仓库最有价值的一个结构思想。

**(c) 脆弱点**：`require_buy_aggression` 几乎恒真（任何正常 K 线总有某个价位 ask>bid），形同虚设；转移窗口 3 根固定，与波动率无关；置信度同样是任意常数加权和（shift.py:328-366），且“K 线越早出现转移加分越多”规则方向可议。

### 3.6 信号定价与风控（`signals.py` / `risk.py` / `engine.py`）

- 入场价三模式（signals.py:236-265）：`shift_close`（转移 K 收盘）、`next_candle_open`（下一根开盘）、`breakout`（突破转移 K 高点 + buffer）。SL 三模式（signals.py:267-304）挂吸收/转移 K 极值 ±0.1% buffer；TP 固定 R:R=2、多目标分批、`rr_then_trail` 追踪（risk.py:96-127, 244-346）。
- 同一 K 线内 SL 优先于 TP（risk.py:60-74），方向保守，**这点做得对**。
- 头寸规模按固定风险 2%（risk.py:348-389）。手续费 0.04%/边、滑点 0.01%。

**(b) 语义**：教科书式 fixed-fractional R 倍数管理；`rr_then_trail` 和分批 TP 的仓位生命周期管理完整可读，是本仓库里实现质量最高的模块。
**(c) 缺陷**：
- config 里 `stop_loss.mode` 声明支持 `atr`/`fixed`，`signals.py:_calculate_stop_loss` **未实现**（只有 absorption_low/shift_low/min-of-both）；
- `trailing.use_atr=true` 时 `risk.py:278-282` 两个分支一模一样（ATR 追踪没实现）；
- config 的 `min_trade_interval`、`session_filter`、`min/max_volatility` 过滤器在引擎中均**未被使用**；
- breakout 入场价 `high×(1+0.0001)` **无条件成交**，不检查该价是否真被触及（见 §4）。

## 4. 未来函数（lookahead）与重绘（repainting）审查

**状态识别无未来函数**：吸收检测只用索引 ≤ idx 的 K 线（absorption.py:106 之前向前看 2 根历史 delta）；转移检测从吸收之后向未来扫 1–3 根，这**不是**用未来确认过去——它把“决策时刻”天然推迟到了转移 K 收盘，即信号的有效生成时刻 = `shift candle close`。批量扫描全历史生成的信号与逐根实时推进产生的信号完全一致。所有失衡/Delta/POC 均为单 K 内闭式计算，追加新数据不改变历史信号，**无重绘**。CVD 依赖数据起点（路径依赖），但仅作展示。

**但执行层存在三处未标记的乐观假设（隐含成交）**：
1. `entry.mode = "shift_close"`（默认）：信号在转移 K 收盘时生成、**同时刻恰好以收盘价成交**。实盘按收盘价撮合只能以市价单在收盘瞬间成交，必然有价差/冲击成本，0.01% 滑点不足以覆盖 10 分钟 K 收盘瞬间的实际冲击。属轻度乐观假设，可接受但未被标记。
2. `entry.mode = "breakout"`：signals.py:257-263 直接给出 `shift_high×(1+buffer)` 为入场价，engine.py:206-259 在转移 K 索引处**无条件成交**，从未检查价格是否真实交易到 breakout 价位。这是**虚构成交**，属于执行层 lookahead 等价物。
3. `entry.mode = "next_candle_open"`：入场价取 `candles[shift_idx+1].open`（signals.py:250-255），但持仓在 `shift_idx` 索引处就以该价开立（`entry_time` 记为转移 K 开盘时间）——价格比“决策时刻”晚一根才被知道，却早一根入账。对 PnL 影响小（第一根 K 不参与出场检查），但属时间轴错配的轻度假设。

**SL/TP 撮合假设**：止损按 SL 价精确成交（risk.py:86-92），不建模跳空穿价（stop-market 实际会劣于 SL）；TP 按限价精确成交；同一 K 内 SL 先检是保守的。滑点仅按固定 0.01% 百分比，与 K 线内实际极端价差无关。

## 5. 不合理假设与其他缺陷

1. **手续费双重收取（已实测复现）**：engine.py:250-251 开仓时扣入场佣金；平仓时 engine.py:196-197 再扣 `_calculate_commission` = 入场+出场**双边**佣金。入场佣金被收两次。我的合成回测：名义 19630.34，毛亏 −141.76，权益却少 165.26，差额 23.50 = 7.85（开仓扣的入场佣金）+ 15.65（平仓再扣的双边）。
2. **PnL 口径前后不一致**：`Trade.from_position`（position.py:253）用 `total_pnl = (exit−entry)×quantity`，**完全不含手续费**，而权益曲线含手续费（且多扣）；胜率/profit factor 统计与权益曲线不可对账。
3. **浮动盈亏用 MFE 冒充市值**：`position.unrealized_pnl`（position.py:102-110）对多头返回 `(highest_price − entry)×qty`，即历史最有利价而非当前 close。持仓期间权益被系统性高估、回撤被扭曲，仅平仓后权益才“跳回”真实值。
4. 固定窗口/固定参数：失衡比率 3×、absorption `delta_lookback=2`、shift 窗口 3 根、`min_volume=1.0` 币、tick_size=100 美元全部固定，**无任何波动率/流动性自适应**；SOL/ETH 直接复用 BTC 的量纲参数会失真（见 §3.1）。
5. 样本偏差风险：config 默认区间 2024 全年、Binance 合约单一交易所、3 个大盘币——典型的温和牛市单边 regime，无样本外意识；README 展示的“胜率 62%、PF 2.14”是模板示例文本而非可复现实验产物。
6. 数据层细节： aggTrades 方向分类正确（用 `m` 字段）；`binance_client.py` 的分页/429 退避实现质量尚可。数据无缺失 tick 校验（aggTrade id 连续性未断言）。

## 6. 代码质量与测试

- **无测试套件**：requirements.txt 列了 pytest/pytest-asyncio，但仓库中连一个 `tests/` 目录或 `test_*.py` 都没有。无 CI（无 `.github`）。无法“运行测试套件”，原因是根本不存在。
- 我做了等价验证——**合成数据端到端烟雾测试**（本机 Python 3.14，pandas 3.0.3）：
  1. 构造含 3 档对角失衡的 K 线，失衡检测正确（ratio、连续分组正确）；
  2. 构造“卖吸收（candle 10）→ 看多转移（candle 11）”场景，全链路产出 1 个 LONG 信号（SL=吸收低点−buffer、TP=2R），回测引擎成交出场正常；
  3. 反例验证：低点未守住/收盘在下半区时信号被否决（§3.4 脆弱点）。
- **跨平台 bug（实测复现）**：`config.py:186` `open(path)` 未指定编码，在 GBK locale 的 Windows 上读取含制表符框线的 `config.yaml` 直接 `UnicodeDecodeError`——作者只在 Linux 下跑过。
- 工程风格：dataclass + Decimal（数值正确但 `iterrows` 逐行构建 footprint，大数据量下慢）；模块分层清晰；但发现至少 6 个“config 声明了/代码没实现”的死配置（§3.6），SHORT 方向复用名为 `absorption_low`/`shift_low` 的模式键（signals.py:296-298 靠注释圆回来），体现“一次写完未经使用打磨”的状态。全部开发发生在 2025-11-23 一天，之后无维护。

## 7. 借鉴价值判定表

| 算法/模块 | 判定 | 理由 |
|---|---|---|
| 对角失衡（ATAS-style，imbalance.py） | **值得借鉴** | 业界标准定义、实现正确（含 ∞/连续分组边界），是最好的被动承接代理。需自适应 ratio/min_volume |
| Footprint 构建（builder.py） | **有缺陷但思想可取** | bid/ask 分桶语义正确（真 taker 方向），但固定美元 tick、逐行 iterrows、无 symbol 量纲；自行实现时按相对 tick 分桶 |
| Absorption 两阶段检测（absorption.py + shift.py） | **有缺陷但思想可取** | “先吸收、后主动换边”结构正确且可解释真实承接行为；但 delta 符号死代码、wick 拒绝退化、置信度乱加权、价格守住参照太朴素，需重写 |
| Delta 背离（delta.py） | **有缺陷但思想可取** | 方向想法对；lookback=2 太短、强度度量在均值近 0 时发散 |
| 信号定价/R 倍数风控（signals.py + risk.py） | **有缺陷但思想可取** | SL/TP 优先级处理正确；但 breakout 虚构成交、佣金双扣、MFE 冒充市值，ATR 模式全是空头支票——只能取其 R 框架思想 |
| 回测引擎/指标（engine.py + metrics.py） | **不值得借鉴** | 账目不一致（PnL 不含费、权益含双扣费）、未实现配置项多、无测试；我们自研引擎不应复用 |
| 数据下载（binance_client.py） | **有缺陷但思想可取** | aggTrades 分页/退避可用作数据管道参考，但需加 id 连续性校验 |

## 8. 与本项目计划方向的差异

market-operator 计划独立实现：波动率自适应确认波段、关键价格区域生命周期、突破接受/失败、拒绝、结构破坏事件。本质差异：

1. **无市场状态与波段层**：该仓库没有任何 swing/zigzag/结构提取，“价格守住”只跟**前一根 K 的 low** 比，而不是已确认的摆动点。我们的“波动率自适应确认波段”对它而言是完全缺失的地基——它的吸收事件无法回答“价格守的是不是一个真关键位”，只能回答“比上一根高/低”。
2. **无区域生命周期**：吸收是**单 K 瞬时事件**，无区域形成→回测→吸收→失效的状态机；我们的“关键价格区域生命周期”是跨 bar 持久对象，粒度根本不同。它的连续失衡价位墙可发展为区域定义的原料（用 3 档失衡带标记候选区域），这是可借鉴的接点。
3. **突破接受/失败 vs 吸收+转移**：语义上最接近——“低处巨量主动卖+价格不创新低”本质就是**向下突破失败（breakdown failure）**的足迹证据，shift 的主动换边本质就是**失败后的反向接受**的开始。它提供了微观（footprint/delta）侧的确认手段；我们计划用纯 OHLCV 索引导出接受/失败，它证明了若引入成交量方向（哪怕只是 taker buy/sell 比），同样事件的信噪比会更高。可作为我们事件的**增强过滤器**而非核心。
4. **拒绝（rejection）**：它的 wick 拒绝实现已退化（见 §3.4）且方向反了，证明该指标对“收盘位置 vs 影线”的定义极其敏感——我们实现拒绝事件时必须用 Wick 方向正确的定义并标注确认时延（收盘确认）。
5. **结构破坏事件**：它的 shift 是“控制权翻转”的订单流版类比，但无 swing BOS/CHoCH 概念；结构破坏事件需要波段层在先，该仓库给不出任何参考实现，属于完全独立的方向。
6. **共同弱点警示**：该仓库所有阈值都是固定常数且未经标定，恰好反证我们“波动率自适应”的必要性——同样的失衡比率在 BTC 高波动日与低波动日含义完全不同。

**总结论**：借鉴其“对角失衡 + 两阶段（吸收→主动换边）”叙事框架与 aggTrades 数据语义，全部代码级实现重写；其回测引擎、风控、指标一概不取。法律上注意：README 声称 MIT 但无 LICENSE 文件，禁止直接复制代码。
