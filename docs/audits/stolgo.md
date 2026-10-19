# 开源算法审计：stolgo

审计日期：2026-10-19 ｜ 审计人：系统化交易研究（子代理）
审计目的：回答"stolgo 是否含有 market-operator 尚未覆盖、且可能改变经济结论的交易原语"。

## 一、来源与许可证

| 项 | 值 |
| --- | --- |
| 实际来源仓库 | `https://github.com/stockalgo/stolgo` |
| 克隆方式 | `git clone --depth 50`（本地路径 `oss-review/stolgo`） |
| 克隆时 HEAD | `1b99e2c8e677084247c60c6bfe3d951fcdb3466c`（2026-09-13，"Merge pull request #98 frontend-audit-fixes"，项目活跃，近 5 条提交均在 2026-09-13） |
| 许可证 | **MIT**（`LICENSE`，© 2020-present chiranjeev gomatiwal）。允许自由使用/修改/再发布/再许可，仅需保留版权声明；AS IS 无担保。**可借鉴代码**，无任何法律障碍 |

**项目性质**：印度股票/加密现货向的 Python 回测框架（Bandl 数据源 + 向量化 `pa` 价格行为规则 DSL + 事件驱动 Engine/OMS + 报告/前端）。核心是"价格行为谓词组合式 DSL"，不是学术论文或特定策略；无经济学结论（命中、期望、成本模型结论一概没有），只是工具箱。

## 二、实际阅读的文件清单

逐行深读（非仅 README）：

- `lib/stolgo/pa/relations/price_level.py`（104 行，全文）— above/below/crosses_above/crosses_below/rejected_at/recovered_at/near/touched 全部谓词定义
- `lib/stolgo/pa/_core.py`（169 行，全文）— Level/Rule 抽象、`Level.touches`、And/Or/Not/Then 组合子
- `lib/stolgo/pa/levels/static.py`（47 行，全文）— 静态价位、level_from_series、cluster 聚合
- `lib/stolgo/pa/levels/rolling.py`（37 行，全文）— support/resistance（Donchian 先移位滚动极值）
- `lib/stolgo/pa/levels/range.py`（35 行，全文）— range_high/range_low
- `lib/stolgo/pa/levels/extended.py`（81 行，全文）— swing_high/swing_low、prev_day_high/low、vwap、pivot_point
- `lib/stolgo/pa/_level_util.py`、`lib/stolgo/pa/_mtf.py`（全文）— 滚动窗口移位纪律、多周期 resample/对齐
- `lib/stolgo/pa/patterns/structure.py`（50 行，全文）— consolidation/breakout_up/breakout_down
- `lib/stolgo/pa/patterns/candles.py`（105 行，全文）— 单 K/双 K 形态（doji/hammer/engulfing 等）
- `lib/stolgo/pa/patterns/momentum.py`、`streaks.py`、`preset/scalp.py`（全文）— 连涨连跌、streak、first_red_day
- `lib/stolgo/core/engine.py`（166 行，全文）— 事件驱动主循环
- `lib/stolgo/oms/sim_broker.py`（91 行，全文）、`oms/order_book.py`（39 行，全文）、`oms/fill_model.py`、`oms/slippage.py`、`oms/commission.py`（全文）— 撮合链
- `lib/stolgo/trade/bracket.py`（126 行，全文）— 固定 RR  bracket + bracket_hit 判定
- `lib/stolgo/portfolio/risk.py`（26 行，全文）— 50% 回撤一刀切断流
- `lib/stolgo/report/metrics.py`（152 行，全文）— 指标计算
- `lib/stolgo/core/config.py`（默认值段）、`lib/stolgo/breakout.py`（遗留兼容层）、`signals/indicators.py`（前 40 行，确认路线）
- `tests/`（整目录跑通，见第六节）；`README.md`、`docs/PA.md` 浏览

**验证性实验**（本节结论均有可复现脚本支撑，见第四、五节）：

1. `touched()` 恒真性：随机 500 根 K 线 × 4 个价位（50/99/100/200），真值占比全部 = 1.0000。
2. `swing_high` 未来依赖：仅改动未来 bar `h[6]`，先前 bar 5 处的 level 值由 15 变为被抹除（重打在第 6 bar）。
3. STOP 单成交偏倚：买单 stop=100，bar O95/H105 → 实际成交 95.0（优于挂单价 5 点）。

## 三、核心算法逐一分析

### 3.1 价格行为谓词（`pa/relations/price_level.py`）

数学定义（`v` 为 Level 序列值，可为静态价或任意序列）：

- `above/below`：`close > v` / `close < v`。
- `crosses_above`：`(c_t > v_t) ∧ (c_{t-1} ≤ v_{t-1})`（收盘穿越，单 bar 确认，无幅度过滤）。
- `rejected_at(v)`：`high_t > v_t ∧ close_t < v_t` —— "上影线穿刺 + 收盘回到位以下"。
- `recovered_at(v)`：`low_t < v_t ∧ close_t > v_t` —— 镜像（别名 `bounced_off`、`failed_breakdown_below`）。
- `near(v, pct=0.005)`：`|close − v|/v ≤ pct`，固定百分比。
- `touched(v, pct=0.002)`：`(high ≥ v·(1−pct)) ∨ (low ≤ v·(1+pct))` —— **判定有缺陷，恒为 True，见第四节**。
- 交易语义：全部为"单 bar 形态 vs 一条价位线"，无区域宽度概念（cluster 的 band 仅限用作可视化/未接入任何关系谓词——已有谓词全部只取 `values()` 中线，不读 band）。

### 3.2 Level 来源

- `level(price)` 静态横线；`cluster(*levels, tol_pct)` 均值折中。
- `support/resistance(lookback)`：`high.shift(1).rolling(N).max()` 先移位滚动极值（= 经典 Donchian 前 N 根不含当根），**无前视**。
- `range_high/low(days)`：同上先移位滚动，供 consolidation/breakout 使用。
- `swing_high/low(lookback=5)`：**中心化窗口** pivot —— 在第 j 根立即打上 `h[j] = max(h[j-half..j+half])` 的标记并 `ffill`，**确认需 j+half 之后的 K 线，属未来函数/重绘**（实测见第四节）。
- `prev_day_high/low`、`pivot_point`：日内 shift(1)，安全；`vwap` 累积型，安全。
- MTF：`resample(...)` 左标签 + `ffill` 对齐回基准周期，配合 level 内置 shift(1) 时无泄漏；但 `swing_*` 若套 tf 仍带中心化窗口泄漏。

### 3.3 组合子与序列

- `And/Or/Not` 逐 bar 布尔组合；`a.then(b)` = `b_t ∧ a_{t-1}`（1 根时滞的"setup→trigger"复合），无前视。
- 结构规则：`consolidation(days, range_pct=0.12)` = 先移位 N 根区间宽度/均价 ≤ 固定 12%；`breakout_up/down` = consolidation 状态下 `close > range_high`（收盘是否收在区间外）。
- candles（doji/hammer/engulfing）、streak/run/parabolic：**属于指标型/形态学叠加内容，不属于本系统路线，仅记此一句。**

### 3.4 执行与账目

- 主循环 `engine.py`：每根 bar 先撮合（match 在先，on_bar 在后）→ 市价单等同**下一根开盘**成交（`NextOpenFill`）或下一根收盘（`CloseFill`），无同 bar 信号-成交前视。
- `OrderBook`：限价单**检查触及**（buy：`bar.low ≤ limit`）按挂单价成交；**STOP 单触发后按 `bar.open` 成交**（非 max(open, stop)），产生系统性有利成交偏倚（见五）。
- 滑点 `BpsSlippage` 每边按 bps 单向加/减一次；手续费 `BpsCommission` 按笔按边各计一次，**无费用双扣**。默认 `commission=0.0、slippage_bps=0.0`（**零成本默认**）。
- `bracket.long/short`：entry 记为 `ctx.data.close[-1]`，但实际市价单下一根开盘成交——**记账入场价与真实成交价不一致**，不回填；`bracket_hit` 同 bar 先判 stop 后判 target（偏保守，方向正确）。
- `portfolio/risk.py`：净值较峰值回撤 > 50% 即永久拒绝一切新单（硬编码阈值，无仓位层级）。
- 指标口径 `report/metrics.py`：跨周期硬编码 252 交易日年化（对 crypto 7×24 不适用）、turnover 用 entry+exit 名义额近似；账目本身（fills → trades → net_pnl）链路诚实。

## 四、未来函数与重绘审查

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| `touched(v, pct)` 谓词 | **恒为 True（逻辑上不可能为 False）** | 其否定要求 `high < v(1−pct) 且 low > v(1+pct)` ⇒ `high < low`，矛盾（v>0, pct≥0）。**应为 AND**：`(high ≥ v(1−pct)) ∧ (low ≤ v(1+pct))`。实测随机 500 根 K 线 × 4 价位，真值占比 1.0000/1.0000/1.0000/1.0000。同样的 OR 缺陷复制在 `_core.py: Level.touches` | 
| `swing_high/low` | **中心化窗口 + 立即 stamping = 未来函数 + 重绘** | 实测：仅改动未来 bar `h[6]`，此前第 5 bar 处 level 值由 15 变为消失（改到第 6 bar）。pivot 在形成根 j 即向前 `ffill` 可见，但其成立与否取决于 j+1..j+half；任何策略在 bar j 读该 level 都在用未来信息。无 forming/known 双时间戳、无确认时延概念——与我们 k_rev·ATR 确认 + 双时间戳 + 前缀不变性测试的纪律完全相反 |
| 滚动/区间/prev_day/vwap/pivot level | 无前视 | 全部 `.shift(1)` 起步或累积型 |
| MTF 对齐 | 条件安全 | resample 左标签 + ffill，配合 level 内部 shift(1) 时安全；`swing_*` 例外 |
| signals → fill | 无同 bar 前视 | 引擎 match 先于 on_bar，市价单下一根 O/C 成交 |

## 五、不合理假设清单

1. **STOP 单成交价 = bar.open**：止损/停止入场单一旦触发按开盘价成交，未取 `max(open, stop_price)`。实测买入 stop=100、bar 开 95 高 105 → 成交 95，系统性**优于挂单价**。对任何 stop 入场（breakout 类）回测结果造成正向偏倚——账目"诚实"但是朝着美化方向不诚实。
2. **零成本默认**：`commission=0.0`、`slippage_bps=0.0`；所有示例回测若不改配置即零成本结论。
3. **bracket 记账错位**：Bracket.entry 记信号根收盘价，实际成交是下一根开盘价（还有滑点），止损/目标的 R 多倍口径与实际 fills 脱节；`bracket_hit` 只是辅助判定，不回写成交。
4. **固定阈值遍地**：consolidation 宽度 12%、near 0.5%、touched 0.2%、锤形影线比例、streak 根数——全部无量纲化/无 ATR 自适应。
5. **risk 层粗糙**：净值回撤 50% 一刀切断流，与仓位/波动无关。
6. 年化按 252 交易日，crypto 时间表不适用。
7. `run_up(min_pct=2.0)` 参数单位陷阱：实现按"收益率 ≥ 2.0（200%）"解释，参数名却叫 pct。

## 六、代码质量与测试

- 跑通：`PYTHONPATH=lib python -m pytest tests/`（忽略 3 个仅因本机缺 duckdb/fastapi 依赖而收集失败的 ui/cli 模块）→ **78 passed, 1 failed（该 failed 亦仅为缺 duckdb 依赖的 import 错误，非代码缺陷）, 2 skipped**。
- `tests/pa/` 对 each 谓词有专项测试（test_relations.py 等），但 **`touched` 的测试用例只验了 True 一侧**——构造的 bar 都落在带内，恰好印证 OR 缺陷从未暴露。
- 工程面整洁（pydantic 配置、协议式 fill/slippage/commission、组合子 DSL），注释含"agent mistake checklist"清单说明作者已知历史坑，但 touched/swing/stop 三处仍在。

## 七、借鉴价值判定表

| 对象 | 判定 | 理由 |
| --- | --- | --- |
| `touched()` / `Level.touches` | **不可借鉴（反面教材）** | 恒真谓词；其缺陷本身记录了"触及判定必须是区间相交（AND）"这一教训，我们区域判定本就按相交实现 |
| `rejected_at` / `recovered_at` | 思想重复，不借鉴 | 单 bar SFP 雏形，见八 |
| `swing_high/low` | **不可借鉴（反面教材）** | 中心化窗口未来函数；恰可作为"为什么 pivot 必须有确认时延 + forming/known 双时间戳"的负例 |
| Stop 单 open 成交 | 不借鉴（执行假设过松） | 我们的成本/滑点纪律严于它 |
| Rule 组合子（and/or/then）DSL | 仅风格参考 | 无独立经济含义，我们的冻结事件目录无需此动态组合 |
| 整体 OMS 骨架 | 风格参考，MIT 可读 | 不引入；我们的价值链已真实数据锁定 |

## 八、与 market-operator 语义对照与新增判定

| stolgo 语义 | market-operator 对应 | 判定 |
| --- | --- | --- |
| `crosses_above/below`（收盘穿越价位线，单 bar） | E1 突破接受（k_rev·ATR 自适应确认 + 波段/区域上下文） | **重复，我们更严**：它无确认幅度、无区域生命周期、无对照 |
| `rejected_at`（high>v 且 close<v） | E2 失败突破（SFP：破 pivot 极值→收回→ATR 归一超出幅度） | **重复且显著弱化**：它的高>v 只要 1 tick 即成立，无破结构极值前提、无收回幅度过滤、无区域上下文 |
| `recovered_at` / `bounced_off` | E3 区域拒绝 / E2 多头镜像 | 同上，重复 |
| `above/below/near` | 区域状态机内部条件 | 重复（我们最严版本还带 forming/known 时间戳） |
| `consolidation+breakout_up/down`（N 根区间宽度 ≤12% 后收盘破 Donchian） | E1 家族（我们 E5 区间回归的对偶面） | 重复，固定阈值 vs 我们波动率自适应 |
| `swing_high/low` 中心化 pivot | 我们的 pivot 序列 regime（确认时延 k_rev·ATR） | 反面：它的定义无法冻结（重绘），我们已实现其正确版本 |
| `a.then(b)` 1-bar setup→trigger | E7 的"破位→回踩→确认"多步序列（时间窗可变） | 重复且更弱，仅 1 根时滞 |
| `bracket`（固定 RR、信号 K 线高/低为止损、风险百分比 sizing） | 我们的破位退出/结构目标减仓/保本/风险中性加仓全持仓栈 | 重复且残缺（无减仓、无保本、无跟踪防线、记账错位） |
| 50% 断流 risk | 无对应（我们在研究层不做组合层风控） | **非交易原语**，组合级开关，不改变单笔事件经济结论 |
| MTF level（Donchian/prev-day/pivot 对齐到基准周期） | 未实现 | **唯一沾边的"新增"，但是素材而非原语**：固定的更高周期参考位可作为 Level 来源喂给我们既有 E1/E2/E3/E7 判定，但事件语义仍由我们的冻结目录定义；其自身在 stolgo 中也无任何经济学证据，样本量级不可独立检验，判定为**不构成可改变经济结论的新事件类型** |
| touched() | 无（我们按区间相交触及） | 缺陷实现，负面新增 |

**新增判定总结**：无真正新增的经济原语。所有价格行为谓词均为本系统已有事件目录的弱化版（无确认时延、无量纲化、无生命周期、无双时间戳）；唯一形式上的差异（MTF 参考位来源）只是输入素材差异，不改变事件定义与经济学结论结构。其两处确凿缺陷（touched 恒真、swing 中心化重绘）与一处有利成交偏倚（STOP 按 open 成交）反而表明：即使按它自己的口径跑回测，结论也不可冻结、不可比对。

## 九、结论

- **许可证**：MIT，代码/思想均可自由借鉴。
- **核心发现**：一个组合式价格行为谓词 DSL + 事件驱动回测引擎；无前视纪律部分（滚动 level/MTF/信号-成交次序）做得对，但存在三处实锤问题——`touched()` 恒真谓词（AND 误写为 OR）、`swing_high/low` 中心化窗口未来函数+重绘、STOP 单按开盘价成交的有利偏倚；默认零成本。
- **借鉴判定**：**无可借鉴的交易原语**。唯一值得保留的是两条反面教训（触及=区间相交须用 AND；pivot 必须有确认时延与双时间戳）——而我们的系统已按正确版本实现并冻结。本项目对 market-operator 的经济学结论（E1|E6|E7 short 相对增量存在但绝对净期望 ≤ 0，全部 NO-GO）无任何修订作用。
