# 开源算法审计报告：pymarket-structure

审计日期：2026-10-19
审计目标：评估其波段构建、结构确认、趋势状态、动态支撑阻力、价格区域生命周期、突破回测与角色转换算法，判断哪些思想值得 market-operator 独立借鉴。

---

## 1. 来源与许可证

- **上游仓库**：`https://github.com/fortunato/pymarket-structure`（经 `git remote -v` 核实，本地目录名与上游一致）。
- **本地 HEAD**：`3502b8ec96959ad47a9b63ae7474c17c7a2dbff7`，提交信息 `chore: release v0.1.0`（2026-04-18）。分支 `main`，与 `origin/main` 同步。
- **许可证**：MIT（`LICENSE`，Copyright (c) 2026 Fortunato）。允许自由使用、修改、再发布（含商用与再授权），唯一义务是保留版权声明与许可文本；软件按 "AS IS" 提供，无担保。对借鉴友好，只需在派生/借鉴部分保留 MIT 声明。
- **血脉说明**：代码多处（`helper.py` 注释、`tests/test_parity.py`）表明本项目是从某个 **TypeScript 原版 MarketStructureHelper 移植** 到 Python，并以 TS 测试套件期望值做定点校验。上游前身未在仓库中署名。
- **发行状态**：PyPI 包名 `market-structure`（导入名 `market_structure`），v0.1.0，属初期发布但工程成熟度高。

## 2. 实际阅读的文件清单（均为逐行阅读）

| 路径 | 行数 | 内容 |
|---|---|---|
| `README.md` | 142 | 定位、Freqtrade 集成、回测 A/B 声明 |
| `src/market_structure/helper.py` | 1175 | **核心**：`MarketStructureHelper`，波段构建/趋势/区域/长周期摆动点 |
| `src/market_structure/types.py` | 118 | `Candle`/`Wave`/`Pullback`/`Zone`/`ZoneLifecycleState`/`LongTermSwing` 数据类 |
| `src/market_structure/hydrate.py` | 177 | `hydrate(df)`：向量化批量构建与增量路径等价的状态 |
| `src/market_structure/tsi.py` | 50 | TSI（双重 EMA 动量）+ 信号线 + 直方图 |
| `src/market_structure/atr.py` | 44 | Wilder ATR |
| `src/market_structure/mtf.py` | 152 | 多时间框架投影（HTF 结构 → LTF，右移一周期防前视） |
| `src/market_structure/freqtrade.py` | 2033 | **第二核心**：67 个 `ms_*` 列的回测快照投影 + 实盘列计算 + 区域生命周期状态机 |
| `tests/test_parity.py` | 350 | hydrate 与 register_candle 两条路径的逐 Wave 相等性 |
| `tests/test_zone_lifecycle.py`、`tests/test_zones.py`、`tests/test_wave_construction.py`、`tests/test_trend.py` 等 | — | 测试覆盖情况（扫读） |
| `refs/freqtrade/README.md` + `backtest_results/` | — | 附带回测数据与免责声明 |

## 3. 核心算法逐一分析

### 3.1 波段（Wave）构建 —— TSI 直方图零穿越分段（`helper.py:203-312`、`hydrate.py`）

**数学定义**：输入为 OHLCV + 一个"直方图列" `histogram_value`（默认 TSI 直方图）。
- 每根柱按 `sign = (hist >= 0 ? up : down)` 分类；
- 相邻柱分类不同即发生 *sign flip*，flip 柱是新波段的首柱，也是前一波段的**确认柱**（`formation_bar_index`）；
- 一个波段 = 两次 flip 之间的所有柱。对每个确认波段计算 6 个极值：`high`/`low`（影线极值）、`highest_close`/`lowest_close`、**HCO**（`max(close, open)` 最大者的柱）与 **LCO**（`min(close, open)` 最小者的柱），及各极值的全局行索引。

**交易语义**：HCO/LCO 用实体端（close/open）而非影线定义摆动点 —— 语义是"被市场接受的价格"（代码注释引 Auction Market Theory / Steidlmayer），影线被视为试探（probe）。波段划分依赖动量振荡器换向，相当于"以动量翻转圈定一段单向运动"。

**参数与敏感性**：
- `histogram_key`：波段的**全部**结构都来自该列符号。默认 TSI(12, 8, 4) 的 `tsi_histogram = tsi - ema(tsi, 4)`。
- **无最小波段时长 / 幅度门槛**：单根柱的直方图翻号就能产生一根"波段"，震荡市会产生大量微型波段（可见于 `max_waves=200` 的 FIFO 淘汰设计——他们预期波段很多）。
- TSI 周期直接决定波段粒度：r/s 加大 → 波段变长变少；对噪声极敏感的参数是 `signal_period`。
- `max_waves=200`：回测投影时实际被放宽为 `len(df)//2+10`（`freqtrade.py:1980`），实盘才裁剪。

**关键性质**：波段的极值只在 flip 柱**事后**可知，代码如实记录 `formation_bar_index`（确认柱索引）——这是无未来函数的关键，下面第 4 节详述。

### 3.2 TSI（`tsi.py:10-50`）

**数学定义**：`momentum = close.diff()`；`tsi = 100 * EMA_s(EMA_r(mom)) / EMA_s(EMA_r(|mom|))`（`adjust=False` 递归 EMA）；`signal = EMA_4(tsi)`；`histogram = tsi - signal`。输出 `(tsi, tsi_signal, tsi_histogram)`。

**语义**：Blau TSI 是双重平滑动量与其绝对值之比，直方图零轴穿越即"双线金叉/死叉"，作为波段切换触发器。

**敏感性**：r=12, s=8 为 Blau 经典参数的变体（原版常用 25/13）；`signal_period=4` 很短，histogram 翻号频繁 → 波段偏碎。`adjust=False` 使 EMA 因果（无未来成分）。

### 3.3 high_since / low_since 与长周期摆动点（`helper.py:318-369, 1108-1160`）

**数学定义**：对 up 波段，从其 HCO 位置起向后扫描已确认波段注册表，找到第一个 HCO 更高的历史波段，返回柱距 `high_since`（找不到则返回到历史起点的距离）。`low_since` 对偶。`pick_long_term_top(high_since=100, max_age=50)` 在最近 `max_age` 柱内找第一个 `high_since >= 100` 的 up 波段，返回 `(age, wave)`。

**语义**：`high_since` 是"该高点是多长时间尺度上的新高"——一种**显著性/新鲜度度量**，等价于 Donchian 式"N 柱新高"但锚定在波段极值上而非逐柱。比固定窗口更接近交易者"重要高点"的直觉。

**敏感性**：两个硬阈值（100 / 50）写死为默认参数，无量纲自适应；不同周期（4h vs 15m）下 100 柱含义完全不同。

### 3.4 回撤度量 Pullback（`helper.py:375-458`）

**数学定义**：以上涨波为例：`price_diff = HCO_current - LCO_last_bottom`；`breakout_level = LCO_last_bottom`；`correction_factor = price_diff / (HCO_previous_top - LCO_last_bottom)`（前一段反向运行幅度为分母）；`length` 为两极值间柱数。下跌波对偶。`atr_factor` 在 helper 中恒为 None（回测投影层另行计算 = `|price_diff|/ATR@确认柱`）。

**语义**：`correction_factor` 即"当前推进恢复了前段回调的比例"——0.5 ≈ 半回撤，>1 = 完全收复并创新高。等同 Fibonacci 回撤的连续度量，但锚点是 HCO/LCO 而非影线，避免 wick 噪声。

**敏感性**：对波段划分（TSI 参数）二阶敏感；分母为 0 时返回 None，行为定义良好。

### 3.5 趋势状态（`helper.py:665-721` 与回测修正 `freqtrade.py:1429-1508`）

**数学定义**：
- `is_trending_up`：最近两 top 满足 `HCO_last > HCO_prev`（HH）**且** 最近两 bottom 满足 `LCO_last > LCO_prev`（HL）**且** forming 波段若是 down 波，其 `low > LCO_last_bottom`（结构未被 forming 波破坏）。
- `is_trending_down` 对偶（LH + LL + forming 波 `high < HCO_last_top`）。
- 回测路径把"forming 波不破坏结构"逐柱化：对 base 为 True 的每段反向波，用 running low/high 触及阈值的第一根柱作为趋势终止点（`_correct_trending_up/down`）。

**语义**：经典道氏 HH/HL 定义的机械版，且承认趋势的"死亡时刻"是价格盘中越过 HL/LH 位的**那根柱**，而非下一次波段确认——回测修正层据此逐柱截断趋势列，这是诚实的确认时延处理。

**敏感性**：需凑齐 4 根确认波段才输出非 False，启动慢；用实体端（HCO/LCO）判定使趋势比影线版更"钝"但更抗假破。

### 3.6 背离检测（`helper.py:599-621`）

**数学定义**：up 波比较：`bearish = (last.highest_close.close > prev.highest_close.close) and (max(hist|last) < max(hist|prev))`；down 波对偶（价格更低 close 但直方图谷值抬高）。

**语义**：价格新高/低 + 动量不新高/低的经典背离，但只在**波段级**（两相邻同向波之间）采样，且动量采样点与价格采样点同属一个波段——比逐柱 RSI 背离稳定，但确认要等波段闭合。

### 3.7 动态支撑阻力区域（`helper.py:727-1102`）

**数学定义**：
- 每根已确认 bottom 波生成支撑区：锚柱 = 波段内 `lowest_close` 的柱；`range = (min(open,close), max(open,close))`（**实体区间**）；`wick_range = (wave.low.low, 实体下沿)`。
- 重叠检测 `range_overlaps`：双向包含/相交；`overlapping_low/high_wave_ids` 收集所有历史同向/反向波段实体与之重叠者。
- 双底判定 `is_double`：在最近 `double_bottom_proximity=2` 根同向**历史**波中，若 `|low_prec - low_anchor| <= tolerance` 且无中间波跌破两者低点（`made_lower_low_between`），判为双底；`tolerance = 0.3 * ATR[anchor.low_idx]`，ATR 缺失时回退 `0.004 * low`。
- `only_include_most_recent_zone=True`：新区间与已登记区间重叠则跳过（保证区域不交叠，取最新）。
- 结果列表从新到旧排列，消费端取 `zones[0]` 为最近区。

**语义**：区域=前一波段实体区间的重申；实体代表"接受价"，wick 区间留给止损放置（注释引 Wyckoff spring 下方止损）。"重叠计数"是该价位被反复交易的证据；双底+容差用 ATR 归一，承认"同一水平"是模糊谓词。

**敏感性**：`tolerance_atr_multiple=0.3` / `pct_fallback=0.004` / `proximity=2` 三个旋钮直接改变双底召回率；区域本身无参数（由波段衍生），这是优点。

### 3.8 区域质量评分（`freqtrade.py:443-483`）

式：`min(overlap,5)*0.8 + 2*is_double + max(0, 2 - width/ATR*2) + 2*exp(-bars_since/100) + min(opp_overlaps,3)*0.7`，封顶 10。纯启发式加权和，权重无出处、未做敏感度论证；时间衰减常数 100 柱硬编码。**属可调摆设，不建议借鉴其具体形式**。

### 3.9 区域生命周期状态机（`freqtrade.py:825-996`）

**状态机**（每侧独立，区域以 `(low, high)` 元组标识，区域更换即重置）：
`intact → broken → retested → {flipped | failed_retest} → intact`
- **break**：`close < zone_low`（支撑）/ `close > zone_high`（阻力）——收盘确认，单柱即破；
- **retest**：默认 `wick` 模式 `high >= zone_low` 触碰即算（`close` 模式需收盘落入区间）；
- **flip**：retest 后价格再按突破方向收盘越界 → 记 `zone_flip_*`（角色转换确认）并复位；
- **failed_retest**：retest 后反向收复整个区间 → 记 `failed_retest_*` 并复位；
- `retest_count` 累计。

**语义**：把 S/R 教科书的"破位-回踩-转换/失败"流程显式状态化；flip 即"支撑转阻力"。但**没有突破接受（acceptance）概念**：单柱收盘破位即 broken，无多柱/幅度确认；wick 模式下任何影线扫过即 retest，噪声大。状态转移在一次 flip/failed 后就丢弃整个历史，无法表达"多次测试的挂单薄化"。

### 3.10 SFP 摆动失败（`freqtrade.py:690-721`、实盘版 `1832-1862`）

`sfp_high = (high > HCO_last_top) and (close < HCO_last_top)`，再做 `_edge_detect_with_level`（仅簇首柱触发，参考位变化时重新武装）。即 Artem Borisov 式 swing failure pattern / 流动性扫荡拒绝。定义干净、逐柱因果。**这是与本项目"拒绝（rejection）"最贴近的原语。**

### 3.11 结构破坏（`freqtrade.py:177-198, 723-761`）

HH+HL 成立时破坏位 = 最近底 LCO（收盘跌破即 `structure_break_confirmed`）；LH+LL 时对偶。事件语义同上（首柱触发 + 位变化重武装）。与 3.5 趋势终止条件同源，但作为显式事件列输出。

### 3.12 其他
- **三推衰竭**（`freqtrade.py:227-254`）：连续三个同向波、幅度递减、极值递伸且步幅收敛。形态学启发式，阈值全硬编码。
- **ATR**（`atr.py`）：教科书 Wilder 平滑，前 `period-1` 个 NaN。实现正确。
- **MTF**（`mtf.py`）：LTF 重采样→HTF 跑同一套→HTF 列右移一个 HTF 周期→ffill 回 LTF。右移保证形成中的 HTF 柱不可见，防前视处理标准且明确。
- **回测快照投影**（`freqtrade.py:486-822, 999-1255`）：先对全帧 `hydrate`（不淘汰），再重算 flip 边界，对每个波段边界生成"截至该边界已确认波段"的快照 dict；第 j 根波段的柱只能看到快照 j-1（即确认柱处的新状态）——见第 4 节。

## 4. 未来函数与重绘审查

**结论：回测投影公开路径不存在未来函数；实盘增量路径与回测存在一处未公开说明的语义分歧（forming 柱直方图首跳冻结）；"波段标签"本身存在固有的确认时延（已如实建模，不算缺陷但使用者必须知情）。**

逐项：

1. **回测快照因果性（合格）**：`_project_backtest` 中第 j 波段区间的柱仅接收 `snapshots[j-1]`，而快照 j-1 由 `registry[:j]` 构造——即只含确认柱 `wave_starts[j]` 之前已确认的波段。`_snapshot_zones` 通过临时交换 helper 的 `_wave_registry/_top_waves/_bottom_waves` 为切片再调用原查询函数，保证区域查询同样因果（`freqtrade.py:371-402`）。ATR/TSI 均为递归因果实现。
2. **确认时延如实标记（合格）**：波段极值只在 flip 柱确认，`formation_bar_index` 即确认柱索引，测试（`test_parity.py`）用其做 `bars_since` 起点；README/demo 也明确波段是确认后才可用。**但** `ms_wave_id` 把第 wi 波段区间内的柱直接标注为 `w-wi`（该 id 要到本段末尾的 flip 柱才"存在"），id 编号本身实时可推（forming-N 同号），无信息泄漏，仅属标注便利，需在文档中注意。
3. **趋势/破坏/SFP 的逐柱化（合格）**：`_correct_trending_up/down` 用 running min/max 找盘中首破柱；事件列用 `_edge_detect*` 只在首柱触发，没有发现回扫改写历史列的行为。
4. **MTF 投影（合格）**：HTF 右移一周期 + ffill，形成中的 HTF 柱不可见。
5. **重绘（repainting）**：回测快照一旦生成不随后续数据变化（快照按边界冻结）；**实盘路径有重绘风险**：`_project_live` 每次用当前 helper 状态填充整列，Freqtrade 实盘只读最后一柱所以无害，但若有人把实盘列当历史序列用会被误导。库的 docstring 已说明 "Only the last row matters"，属知情设计。
6. **实盘/回测语义分歧（缺陷，未如实标记）**：`register_candle` 按 `open_time` 去重后**永久冻结该柱的直方图取值**（`helper.py:218-221`）。Freqtrade 实盘中每 tick 重发 forming 柱，第一次到达时的 histogram 值被冻结；若该柱剩余时间内直方图翻号又翻回，实盘会确认一根回测中不存在的波段；反之亦然。即：**live 与 backtest 的波段序列可发散，且无任何护栏/告警**。使用其做实盘信号时必须意识到这一点（缓解：只在柱收盘后注册）。
7. ** warmed-up `high_since`**：在历史起点附近 `high_since` 受可见历史长度截断（返回"到起点的距离"），属边界行为而非前视，但首 200 波段的显著性度量偏低。

## 5. 不合理假设

- **波段=振荡器零穿越段**：把市场结构完全外包给 TSI(12,8,4) 的符号，无最小幅度/时长波动率门槛——震荡市中产生单柱"波段"，趋势/区域/双底全部建立在这些碎波之上。这是全库最大的结构性假设。
- **单柱收盘破位**（生命周期 `broken`）与**任意影线触碰即回踩**（wick 模式）——无突破幅度、停留时间或成交量确认，假破敏感。
- **breakout 隐含成交假设**：其 Freqtrade 回测示例以收盘确认的破位/回踩做过滤，实际入场仍受滑点/资金费率影响；refs 中的 A/B 收益声明作者自己标注了"参数对特定历史窗口过拟合"的免责声明——态度诚实，但 +39.85% vs +22.59% 的对比不可作为算法有效性的证据。
- **区域身份=区间浮点元组**：生命周期把 `(low, high)` 相等当同一区域，跨波段同一价位重建的区域会继承/错误重置状态。
- **质量评分权重任意**（见 3.8）。
- **幸存者/品种偏差**：示例回测仅 5 个主流币 4h × 360 天，单一熊市窗口。
- 无成本建模（库本身不含执行模型，属合理边界）。

## 6. 代码质量与测试

- **测试套件实跑结果**：在本地临时 venv（Python 3.12 + `pip install -e . pytest pytest-cov`）运行 `pytest -q`：**394 passed，0 failed**（约 32s）。分支覆盖率：`helper.py` 98%，`hydrate/atr/tsi/types/mtf` 100%，`freqtrade.py` 38%（实盘列分组函数、部分边界分支未测——未测区恰好是第 4.6 条分歧所在的实盘路径）。
- **工程质量高**：`frozen+slots` 数据类；增量与批量两条构建路径以逐 Wave 相等性测试锁定（`test_parity.py` 是全库最有价值的资产）；TS 原版期望值定点回归（fixture `ms-LTCUSDT-4h.json`）；闪崩 fixture（`btc-2025-10-10-flash-crash.json`）；严格 pyright + ruff + pre-commit + CI。docstring 含决策理由与已知陷阱（如 zone 缓存以 `id(atr_arr)` 为键的 CPython 地址复用 caveat）。
- 小瑕疵：`helper.py` 引用的 `docs/porting-market-structure-helper.md` 在仓库中不存在；`Pullback.atr_factor` 在 helper 层恒为 None（测试中以"已知分歧"固化为期望）；`_assert_alternation` 用 `assert`，`python -O` 下被剥离（docstring 已声明）。

## 7. 借鉴价值判定表

| 算法 | 判定 | 理由 |
|---|---|---|
| 波段构建（振荡器符号分段 + HCO/LCO 实体锚 + `formation_bar_index` 确认柱制度） | **有缺陷但思想可取** | "实体端锚定摆动点 + 显式确认柱"的元设计优秀且诚实；但把分段完全交给单一振荡器符号、无波动率门槛，可直接改进为波动率自适应确认 |
| TSI | 不值得借鉴 | 标准指标，无增量价值；仅作默认驱动器 |
| high_since/low_since + pick_long_term_* | **值得借鉴（需改造）** | "此极值刷新了多长历史"的显著性度量比固定窗口优雅；阈值需改自适应 |
| Pullback/correction_factor | 值得借鉴 | 以前段幅度为分母的连续回撤比，锚实体端，定义干净 |
| 趋势状态 HH/HL + 盘中首破逐柱截断 | **值得借鉴** | 道氏定义的机械版且正确处理"趋势死亡时刻"，回测/实盘语义一致的做法可照搬 |
| 背离（波段级） | 有缺陷但思想可取 | 波段级采样抗噪合理；但与 TSI 强耦合 |
| 动态支撑阻力区域（实体区间 + wick 区间 + 重叠计数 + ATR 容差双底） | **值得借鉴** | 区域由波段衍生而非独立参数化；"接受价（实体）/试探价（影线）"双几何 + 重叠证据是最接近本项目"区域生命周期"的现成形式 |
| 区域质量评分 | 不值得借鉴 | 权重任意、无常数依据 |
| 区域生命周期状态机（break/retest/flip/failed） | 有缺陷但思想可取 | 状态骨架正确且事件语义清晰；缺接受/幅度确认，身份机制脆弱 |
| SFP（sfp_high/low + 边沿触发） | **值得借鉴** | 即本项目的"拒绝"原语，定义干净因果 |
| 结构破坏事件（HH/HL 破位列） | 值得借鉴 | 与趋势终止同源，事件化输出形式可直接借用 |
| 三推衰竭 | 不值得借鉴 | 硬编码形态启发式 |
| MTF 右移防前视 | 值得借鉴（工程模式） | HTF +1 周期 + ffill 是防止 MTF 前视的标准正确做法 |
| 双路径 parity 测试制度 | **值得借鉴流程** | "增量实时路径 ≡ 批量回测路径"逐对象相等性锁定，是本项目防重绘的最有效工程手段 |

## 8. 与本项目方向的差异

对照 market-operator 计划独立实现的方向：

1. **波动率自适应确认波段**：对方用 TSI 符号分段，确认阈值恒为 0（直方图符号），与波动率无关，且无最小波段过滤；本项目计划的"波动率自适应确认"恰好补上其最大短板（碎波/迟滞不可兼得）。其 `Double-pattern tolerance` 虽已 ATR 化，但仅用于双底标签，未进入分段逻辑。**本质差异：分段是否由自适应幅度门槛驱动。**
2. **关键价格区域生命周期**：对方区域=单波段实体区间，生命周期仅 intact→broken→retested→flip/failed 且一次事件即重置，区域无年龄、无衰减、无多价位聚合（`only_include_most_recent_zone` 甚至主动抑制重叠区并存）。本项目若建模"区域从生成→被测试→弱化→失效"的连续生命周期，区别在于：**区域是状态连续体而非布尔事件序列**。
3. **突破接受/失败**：对方只有单柱收盘破位（broken）与 wick 触碰回踩（retest），没有"多柱停留在区间外 = 接受 / 快速收回 = 失败"的二分类。其 failed_retest 其实是"回踩后收复"（更接近 flip 失败的变体），**不是** acceptance 语义。本质差异：是否用时间/幅度维度确认突破。
4. **拒绝（rejection）**：其 SFP 与该概念同构（影线越界 + 收盘收回），可作为本项目拒绝检测的最小基线，但缺少成交量/摆动强度维度。
5. **结构破坏事件**：其实现 = HH/HL 结构下价格收盘越过最近 HL/LH，事件列编码正确。本项目可复用该事件定义，差异点在于本项目预计要求破坏伴随位移/动量确认（其无任何强度条件）。
6. **流程定位**：对方是"特征/滤波层"（只产出结构标签与事件，不做决策），与本项目"认知→假设→管理"的决策流水线是互补层；借鉴其价值主要在特征原语与无重绘工程制度。

## 9. 附：验证记录

- `git remote -v` → `origin https://github.com/fortunato/pymarket-structure`；HEAD `3502b8e`（v0.1.0）。
- 测试：临时 venv `pip install -e . pytest pytest-cov` → `pytest -q` → `394 passed in 31.58s`；覆盖率 `helper.py 98% / freqtrade.py 38%`。测试残留（venv、`htmlcov`、`*.egg-info`）已清理，仓库 `git status` 干净。
