# v22 真实平衡箱夹板与完备双原语统一架构施工说明书（AI 傻瓜式零歧义规范）

> **文档定位**：本文件是 `market-operator` 策略重构的**唯一最高施工准则（Single Source of Truth）**。
> **阅读对象**：任何执行代码编写的 AI Agent 或工程师。**严禁自行发挥、严禁恢复已废弃的 `stce` 状态机、严禁在未授权处增删条件。**
> **基准底座**：基于 `main` 分支（commit `30fcbab`）的经纪商、容量、持仓与实盘守护底座，全新替换上半身的「市场认知、平衡箱识别、双原语事件、级联/限价路由」核心链。

---

## 0. 历史教训与绝对禁令（DO NOT DO — 违反任意一条即判施工失败）

在动手写任何一行代码前，施工者必须熟记以下已被实盘和回放证伪的死胡同：

1. **【禁令 1：严禁恢复 `stce` 的 8,100 行代码与三语义 A/B/C 状态机】**
   - `stce` 把同一个“边界拒绝回归”拆成 `A_REJECTION`（差 1 tick 没破影线）和 `B_FAILED_BREAK`（刺穿 1 tick 再收回），导致价格在边界附近稍微抖动就在 A/B/C 之间死锁，55.7% 死于 `CONTEXT_STALE`，94.7% 死于 `NET_REWARD_TOO_SMALL / REWARD_CONSUMED`。
   - **唯一合法顶层动作语义只有两个**：`REJECT`（边界拍卖拒绝 $\to$ 区间回归）与 `ACCEPT`（边界拍卖接受 $\to$ 破位迁移）。另有一个非交易垃圾回收态 `CONSUMED`（反复穿刺粘连 $\to$ 销毁该边界不交易）。
2. **【禁令 2：严禁单根 K 线影线造区 & 严禁满屏保留陈旧障碍墙】**
   - 不允许每个 Pivot 造一个永久保留的 Zone，也不允许用 50 天前的一根孤立影线拦截今天的交易。
   - **唯一合法地图视图**：围绕现价 $P_t$ 的**【现价近邻夹板（`ActiveBracket`）】**——在任意时刻、任意单周期上，最多只允许存在 **`1 个当前支撑（Support）`** + **`1 个当前阻力（Resistance）`**！箱体内部的一切历史高低点 100% 抹平。
3. **【禁令 3：严禁使用 `5m` 或 `15m` K 线做策略认知、入场确认或提前惊吓平仓】**
   - 最小策略结构认知周期为 **`1h`**（工作周期集合：`1h, 2h, 4h, 12h, 1d`）。
   - 严禁在 `1h/2h/4h` 开仓后用 `15m` 的 `SOURCE_LOST`、盘中动态新冒出的 `NEW_PHYSICAL_BARRIER` 或未成熟的反向小结构去提前市价砍仓（这是造成 `stce` 12 笔交易 0 次走到止盈、7 次惊吓割肉的元凶）。**哪个周期开仓，就只能由哪个周期裁决逻辑失效！**
4. **【禁令 4：严禁使用旧版固定长度滑动窗口 `(8, 13, 21...)` + 分位数盲扫识别平衡箱】**
   - 旧版 `balance_detect.py` 用固定长度窗口滑动，会把箱体左起点切丢、把单边暴跌大阴线包进窗口，且把 32%~36% 的 45 度阴跌通道误判成水平平衡箱去逆势接飞刀。
   - 必须使用已落地的 **`TrueBalanceDetector`（`src/market_operator/true_balance.py`）**。
5. **【禁令 5：严禁开出“蚂蚁仓” & 严禁窄止损噪声单混入】**
   - 严禁使用 `symbol_notional_fraction = 2` 锁死杠杆；严禁误用 10 亿 USDT 仓位档位的 60% 维持保证金率计算强平价。
   - 必须执行 **F1 最小止损宽度门**（`|entry - invalid| >= k_is * ATR`，默认 `0.8 * ATR`）与 **FIFO 整笔准入跳过纪律**（不足最小步长或缩水超标时整笔 `SKIP`，绝不开 0.0002 BTC / 0.01 ETH 的蚂蚁仓占槽位）。

---

## 1. 系统整体五层架构与模块映射表

| 层级 | 职责定义 | 对应文件（绝对路径相对于仓库根） | 施工状态与处理方式 |
| :--- | :--- | :--- | :--- |
| **L1：市场认知与真平衡箱夹板层** | 波段定框生长、双层边界 `[lo_ext, VAL]` / `[VAH, hi_ext]`、水平度与 POC 居中校验、围绕现价输出 `[1支撑, 1阻力]` 夹板 | `src/market_operator/true_balance.py`<br>`src/market_operator/structure.py` | `true_balance.py` **已完成并通过实测**；<br>需桥接进 `structure.py` 的 `ZoneBook` |
| **L2：完备双原语事件检测层** | 基于当前活跃夹板检测 `REJECT`（含 R1 影线拒绝 / R2 受阻回落 / R3 收盘假突破吞没收复）、`ACCEPT`（含 A1 紧贴站稳 / A2 过伸转回踩 `FLIPPED`）及 `CONSUMED` 粘连淘汰 | `src/market_operator/events.py` | **待按本文 §3 改造** |
| **L3：快速进场路由与触发层** | ① 高阶周期（`4h/12h/1d`）用 `1h` 收盘级联触发；<br>② 本周期（`1h/2h`）已验证箱沿与 `A2` 翻转回踩挂 **前缘 Limit 单**，右侧过长确认 K 线挂 **50% 回抽 Limit 单（带 TTL）** | `src/market_operator/routing.py`<br>`src/market_operator/trigger.py` | **待按本文 §4 改造** |
| **L4：持仓与分类止盈止损层** | ① `REJECT` 单：结构外沿止损 + 对侧 `box_opposite` 锚定 + `opp_lock_step` 峰值回吐预算锁利；<br>② `ACCEPT` 单：翻转边界止损 + 浮盈 `>=1R` 后确认防守 Pivot 棘轮移动止损 | `src/market_operator/position.py` | **复用 `main` 现有实现并按 §5 核对参数** |
| **L5：容量风控与反蚂蚁仓执行层** | 104U 小资金阶梯风险预算 + 纯止损距离定仓 + 整笔准入跳过 + 跨标的同向净风险上限（`directional_cap_frac`） | `src/market_operator/capacity.py`<br>`src/market_operator/broker.py`<br>`config/strategy.json` | **复用 `main` 并按 §6 增补同向风控锁** |


---

## 2. 第一层详规：真平衡箱与现价近邻夹板引擎（L1 — 已落地基准）

> **代码位置**：`src/market_operator/true_balance.py`（单元测试：`tests/test_true_balance.py`，实测诊断脚本：`scripts/_diag_true_balance.py`）。

### 2.1 数学定义与物理构造四步法

1. **波段拐点提取（`SwingPoint`）**
   - 当价格从当前极值回退超过 `pivot_rev_atr * ATR`（默认 `0.90 * ATR`）时，确认一个波段拐点 `SwingPoint`：
     - 若为高点（`kind = "HIGH"`）：记录影线极值 `price = high`，实体上沿 `body_edge = max(open, close)`；
     - 若为低点（`kind = "LOW"`）：记录影线极值 `price = low`，实体下沿 `body_edge = min(open, close)`。

2. **波段定框生长（Swing-Anchored Box Spawn & Grow）**
   - **出生条件**：在近期未破位的拐点序列中，存在至少 `min_swings = 4` 个拐点（至少 2 个 `HIGH` + 2 个 `LOW`），且在上下沿区域之间完成了至少 **3 段交替折返行程**（即 `T-B-T-B` 或 `B-T-B-T`，交替次数 `alternations >= 3`），持续根数 `length >= min_bars`（默认 `10` 根）。
   - **向右生长**：一旦定框成功，左起点 `start_index` 钉死不动。此后每来一根新 K 线：
     - 只要未发生**连续 `break_confirm_bars = 2` 根收盘越过外沿 `hi_ext + bm`（或 `lo_ext - bm`）**，且未发生单根收盘逃逸超过 `1.25 * ATR`，箱体保持 `ACTIVE` 并向右延展 `end_index = idx`；
     - 当箱内确认新的波段拐点时，自动将新拐点融合进边界簇更新；
     - 一旦满足破位条件，箱体状态转为 `BROKEN_UP` 或 `BROKEN_DOWN`，记录 `broken_index = idx` 并移入 `history_boxes`。

3. **双层物理边界公式（Dual-Layer Boundaries）**
   - **内沿（价值边界 `VAL` / `VAH`，用于触碰感知与 Limit 挂单）**：
     $$\text{VAH} = 0.60 \times \text{Median}(\text{TopSwings.body\_edge}) + 0.40 \times \text{Quantile}_{0.86}(\text{body\_highs})$$
     $$\text{VAL} = 0.60 \times \text{Median}(\text{BotSwings.body\_edge}) + 0.40 \times \text{Quantile}_{0.14}(\text{body\_lows})$$
   - **外沿（极值掩体 `lo_ext` / `hi_ext`，用于锚定结构止损）**：
     $$\text{hi\_ext} = \max\Big(\text{VAH} + 0.15 \cdot \text{ATR},\ \min\big(\max(\text{TopCluster.price},\ Q_{0.94}(\text{highs})),\ \text{VAH} + \text{wick\_cap\_atr} \cdot \text{ATR}\big)\Big)$$
     $$\text{lo\_ext} = \min\Big(\text{VAL} - 0.15 \cdot \text{ATR},\ \max\big(\min(\text{BotCluster.price},\ Q_{0.06}(\text{lows})),\ \text{VAL} - \text{wick\_cap\_atr} \cdot \text{ATR}\big)\Big)$$
     *注：`wick_cap_atr = 0.85` 防止单根异常插针把外沿拉到天际，同时保证正常影线极值完整包入防线。*

4. **三道硬性防伪门（缺一不可）**
   - **防伪门 1（水平度校验）**：计算前半窗与后半窗收盘均值的每 10 根 K 线漂移，以及上下沿拐点实体边连线的每 10 根 K 线斜率 `slope_atr_per_10`。若 `slope_atr_per_10 > 0.30`，**立即否决**（100% 拦截 45 度阴跌/阳涨通道）。
   - **防伪门 2（驻留率与中枢居中）**：箱体存续期收盘价落在 `[lo_ext - 0.15*ATR, hi_ext + 0.15*ATR]` 内的比例 `dwell >= 0.68`；按 **K 线实体覆盖（权重 0.75）+ 影线覆盖（权重 0.25）乘以成交量 `volume`** 统计 12 档价格密度的峰值中枢 `POC`，其相对位置 `poc_pos = (poc - val) / (vah - val)` 必须落在 `[0.25, 0.75]` 之间。
   - **防伪门 3（可交易跨度与低方向效率）**：`1.80 <= (hi_ext - lo_ext) / ATR <= 12.0`，且路径效率 `efficiency = |close[-1] - close[0]| / sum(|Δclose|) <= 0.48`。

5. **现价近邻夹板输出算子（`ActiveBracket`）**
   - 每根 K 线收盘调用 `det.bracket()`，只可能返回两种模式之一：
     - **`mode == "IN_BOX"`**（当前存在活跃平衡箱 `b`）：
       - `support`: `role="SUPPORT", source_kind="BOX_EDGE", inner=b.val, outer=b.lo_ext, opposite=b.vah, poc=b.poc`
       - `resistance`: `role="RESISTANCE", source_kind="BOX_EDGE", inner=b.vah, outer=b.hi_ext, opposite=b.val, poc=b.poc`
     - **`mode == "MIGRATION"`**（处于箱体破位后的趋势迁移期）：
       - 从近期破位箱体的翻转边（`BROKEN_UP` 的原上沿 `[vah, hi_ext]` 翻转为 `FLIPPED_BOX_EDGE` 支撑；`BROKEN_DOWN` 的原下沿 `[lo_ext, val]` 翻转为 `FLIPPED_BOX_EDGE` 阻力）以及近期 `SwingPoint`（`MAJOR_SWING`）中，围绕现价 `close` **向下只取最近的 1 个 `support`，向上只取最近的 1 个 `resistance`**（同距离下 `FLIPPED_BOX_EDGE` 优先级高于普通 `MAJOR_SWING`）。

### 2.2 与 `src/market_operator/structure.py` 的桥接规范（施工动作 1）

在 `structure.py` 的 `Params` 与 `ZoneBook` 中接入 `TrueBalanceDetector`：
- 当 `use_balance_detector=True` 时，`ZoneBook` 内部实例化 `TrueBalanceDetector`（替代旧版 `BalanceDetector`）。
- 在 `ZoneBook._sync_balance_boxes()` 中，每根 K 线获取 `bracket = self._bal_det.push(...)`：
  - 将 `bracket.support`（若非空）映射为场内唯一的 `ZoneRole.SUPPORT` 活跃区：`lo = support.outer, hi = support.inner, box_opposite = support.opposite, box_quality = support.quality, box_width_atr = support.width_atr, box_rotations = support.rotations`；
  - 将 `bracket.resistance`（若非空）映射为场内唯一的 `ZoneRole.RESISTANCE` 活跃区：`lo = resistance.inner, hi = resistance.outer, box_opposite = resistance.opposite, box_quality = resistance.quality, box_width_atr = resistance.width_atr, box_rotations = resistance.rotations`；
  - 若新夹板边与场内已有同角色 `ACTIVE` 区的边界差 `<= tol`（`0.5 * ATR`），**原位更新该 `Zone` 对象属性并保持对象 `id(z)` 不变**，以确保 `events.py` 中正在观察的 `_Episode` 不被中断！
  - 不在当前 `ActiveBracket` 内的旧 `ACTIVE` 区域，超过短窗口（如刚破位正在走 2 根确认的窗口）后立即移出 `self.zones`，保证任何时刻活跃 `Zone` 数量严格 $\le 2$。

---

## 3. 第二层详规：完备双原语事件检测层（L2 — `ACCEPT` / `REJECT` / `CONSUMED`）

> **代码位置**：`src/market_operator/events.py` 与 `src/market_operator/structure.py`。

### 3.1 为什么必须升级 `main` 现有的事件检测？
`main` 分支旧版 `events.py` 有两个结构性漏网与错位：
1. **漏掉 R3 收盘假突破吞没收复（Trap / Spring / UTAD）**：旧版只要有 1 根 K 线收盘越过 `hi + bm`，`structure.py` 立即把 Zone 设为 `BROKEN` 并杀死 `_Episode`。若第 2 根 K 线立刻大阴线砸回箱内，旧版既不发 `ACCEPT` 也不发 `REJECT`，白白丢掉胜率最高的假突破陷阱单。
2. **把过伸长阳脉冲当成 A1 市价追高**：旧版连收 3 根外侧 K 线无论跑出多远一律发 `BREAKOUT_ACCEPTED` 市价追入，导致 `ACCEPT|多` 胜率仅 25%~26%。

### 3.2 边界交互的 5+1 完备分类公式（以向上测试阻力区 $Z = [\text{lo}=\text{VAH},\ \text{hi}=\text{hi\_ext}]$ 为例，支撑区完全镜像）

令当前 K 线为 $(o, h, l, c)$，当前周期 ATR 为 $\text{atr}$，破位余量 $\text{bm} = k_{\text{bm}} \cdot \text{atr}$（默认 $0.10 \cdot \text{atr}$），拒绝回退阈值 $\text{rj} = k_{\text{rj}} \cdot \text{atr}$（默认 $0.30 \cdot \text{atr}$）。
当价格从正面接近并触碰 $Z$（即前收 $<\text{lo}$ 且当根 $h \ge \text{lo}$）时开启观察 `_Episode`，记录试探最高极值 $\text{ex} = \max(h)$：

#### 一、`REJECT` 原语（向外探索失败，价值退回箱内，方向 = 空 `-1`；支撑区方向 = 多 `+1`）
只要在触碰开启后的 $N_f$ 根 K 线窗口内（默认 $N_f = 3$）满足以下 **R1、R2、R3 任一条件**，统一发射 `REJECT`（规范事件名 `LEVEL_REJECTED` / 别名 `REJECT`），且**止损建议价 `invalid` 统一锚定在本次试探最深极值外侧**：
$$\text{invalid}_{\text{short}} = \text{ex} + \text{bm}, \qquad \text{invalid}_{\text{long}} = \text{ex} - \text{bm}$$

1. **R1 · 单根影线扫损拒绝（Wick Sweep / Pinbar）**：
   - 触发 K 线最高价刺入或刺穿阻力带（$h \ge \text{lo}$），当根收盘直接退回内沿下方或带内下半部（$c \le \text{lo}$），且上影线回退幅度 $h - c \ge \text{rj}$（同时当根满足先高后低启发式 $c < o$）。
2. **R2 · 带内驻留受阻回落（Stall & Turn）**：
   - 价格进入 $[\text{lo}, \text{hi} + \text{bm}]$ 测试但未发生有效逃逸，在窗口 $[1, N_f]$ 内收盘跌回内沿下方（$c < \text{lo}$），且自试探最高点回退 $\text{ex} - c \ge \text{rj}$。
3. **R3 · 收盘假突破吞没收复（Trap / Failed Acceptance — 关键补齐！）**：
   - 允许 **1~2 根** K 线收盘越过外沿（$c > \text{hi} + \text{bm}$），只要极值未发生单边逃逸（$\text{ex} - \text{hi} \le 1.25 \cdot \text{atr}$），**禁止立即销毁 `_Episode`**！
   - 若在刺穿后的 $\le N_f$ 根 K 线内，出现一根反向吞没 K 线**直接收盘重新砸回内沿 `lo` 以内**（阻力侧 $c < \text{lo}$ 且 $c < o$；支撑侧 $c > \text{hi}$ 且 $c > o$），立即判定假突破成立，发射 `REJECT`（附带标签 `sub_kind="R3_TRAP"`），同时终止该区的破位候选计数！

#### 二、`ACCEPT` 原语（边界防守崩溃，在外侧建立新价值，方向 = 多 `+1`；支撑破位方向 = 空 `-1`）
按突破后的位移是否过伸，严格拆分为 **A1（紧贴站稳直接进）** 与 **A2（过伸转回踩进）**：

1. **A1 · 紧贴驻留接受（Build-Value Outside）**：
   - 连续 $N_a$ 根 K 线（默认 $N_a = 2$ 或 $3$）收盘站稳在外沿外侧（阻力侧 $c > \text{hi} + \text{bm}$，支撑侧 $c < \text{lo} - \text{bm}$），期间低点未跌回内沿 $\text{lo}$；
   - **过伸检查**：确认当根收盘距离外沿未过度透支，即 $|c - \text{hi}| \le k_{\text{ext}} \cdot \text{atr}$（默认 $k_{\text{ext}} = 1.0$）。
   - 满足则发射 `ACCEPT`（`sub_kind="A1_HOLD"`），止损锚定在原边界另一侧 `lo - bm`（空头为 `hi + bm`）。
2. **A2 · 过伸突破转回踩确认（Break & Retest / S-R Flip）**：
   - 若满足连续外侧收盘（或单根巨量长阳收盘越过外沿 $> 1.0 \cdot \text{atr}$），因距离原防线太远（$|c - \text{hi}| > 1.0 \cdot \text{atr}$），**禁止发射市价追高信号**！
   - 将该区域翻转为反向 `FLIPPED_BOX_EDGE`（原阻力翻转为新支撑 $[\text{lo}, \text{hi}]$）；当后续价格首次回踩触及 $[\text{lo}, \text{hi}]$ 并出现企稳（盘中前缘 Limit 挂单成交，或回踩回退 $\ge \text{rj}$ 发射 `FLIP_RETESTED`），按 `ACCEPT`（`sub_kind="A2_RETEST"`）执行入场！

#### 三、`CONSUMED` 钝化淘汰态（中轴粘连 — 销毁不交易）
- 若 `_Episode` 存续超过 $N_f + 1$ 根 K 线，价格仍在 $[\text{lo}, \text{hi}]$ 上下反复穿刺收盘，既未满足 `REJECT` 回落，也未满足 `ACCEPT` 外侧连续站稳，直接将该 `_Episode` 标记为 `CONSUMED` 终结，并对该区域施加冷却锁，绝不在钝化泥潭中开仓。
---

## 4. 第三层详规：快速进场与触发路由层（L3 — 消灭 15m/5m 盯盘，防止收线吃掉利润）

> **代码位置**：`src/market_operator/routing.py`、`src/market_operator/trigger.py`、`src/market_operator/broker.py`。

### 4.1 核心分工原则（如何不用 5m/15m 也能拿到极佳入场位置？）
1. **高阶周期结构（`4h / 12h / 1d`）$\implies$ 采用 `1h`（或降一档周期）收盘级联确认**
   - 大箱体宽度达 `3%~8%`，当价格进入大箱体边界带 `[lo_ext, VAL]` 或 `[VAH, hi_ext]` 时，使用 `trigger.py` 的 `MicTracker` 在次级触发周期（如 `4h -> 1h`、`12h -> 2h`、`1d -> 4h`）收盘第一时间确认 `REJECT` 或 `ACCEPT` 入场。
   - **硬约束**：`trigger_tf` **严禁低于 `1h`**（即 `trigger_tf_seconds >= 3600`）。
2. **本周期结构（`1h / 2h`）$\implies$ 采用「前缘/回抽 Limit 限价单 + 紧凑收盘市价」双轨制**
   - **轨道 A（紧凑确认直接进）**：当本周期 K 线收盘触发 `REJECT`（R1/R2/R3）或 `ACCEPT`（A1）时，计算当前收盘价 `close` 到结构止损 `invalid` 的距离 `stop_dist = |close - invalid|`，以及到对侧目标 `box_opposite` 的剩余空间 `room = |box_opposite - close|`：
     - 若 `stop_dist <= 1.2 * ATR` 且剩余盈亏比 `room / stop_dist >= min_box_rr`（默认 `1.5`）$\implies$ **利润未被确认 K 线吃掉，直接发 `kind="market"` 下一根开盘成交**！
   - **轨道 B（确认 K 线过长时的 50% 回抽 Limit 挂单 — 严禁半山腰硬追！）**：
     - 若确认 K 线是一根巨阳/巨阴，导致 `stop_dist > 1.2 * ATR` 或剩余空间 `room / stop_dist < 1.5`，说明当根 K 线已透支短期行程；
     - 此时**禁止发市价单**！改为发出 `kind="limit"` 的 **`RestingLimitOrder`**，挂单价设为**该边界内沿 `VAL / VAH`（或确认 K 线 50% 回撤位 `0.5 * (close + ref_edge)`）**，有效期 `n_fill = 3` 根认知 K 线（超时未成交自动撤单 `EXPIRED`，绝不追高）。
   - **轨道 C（`A2_RETEST` 突破回踩前缘 Limit 挂单）**：
     - 当箱体发生过伸突破生成 `FLIPPED_BOX_EDGE` 时，直接在翻转边界内沿挂 `kind="limit"` 前缘限价单（Maker 费率 `0.02%`，零滑点，盘中触价即成交）。

### 4.2 入场前硬闸门（在 `MarketRouter.on_bar()` 中统一拦截）
任何信号在转化为 `RouteDecision` 前，必须同时通过以下 4 道硬闸门：
1. **F1 最小波动率止损宽度门（防窄止损噪声单）**：
   $$\text{direction} \cdot (\text{entry\_px} - \text{invalid}) \ge k_{\text{is}} \cdot \text{ATR} \quad (\text{默认 } k_{\text{is}} = 0.80)$$
   *铁律：天然结构止损距离小于 `0.8 * ATR` 的交易属于微观噪声，直接丢弃（计数器记 `f1_gate_skip`），**严禁人工把止损拉宽凑够 0.8 ATR**！*
2. **箱体质量与轮动门槛**：
   - `zone.box_quality >= min_box_quality`（默认 `0.35~0.45`），`zone.box_rotations >= min_box_rotations`（默认 `>= 1`）。
3. **`REJECT` 对侧净空间门槛**：
   - 对 `REJECT` 单，必须存在 `box_opposite`，且 `|box_opposite - entry_px| / |entry_px - invalid| >= min_box_rr`（默认 `1.5`）。
4. **同边界冷却锁（`zone_cooldown_bars`）**：
   - 同一条箱体边界触发开仓后，冷却 `zone_cooldown_bars`（默认 `3~4` 根认知 K 线）内禁止在同一边界重复刷单。

---

## 5. 第四层详规：持仓管理与分类止盈止损层（L4 — 谁开仓谁管理）

> **代码位置**：`src/market_operator/position.py`。

### 5.1 绝对一致性原则
- **认知周期裁决**：`4h` 战役开的仓，只由 `4h` 收盘推进 `PositionManager.on_bar()`；`2h` 战役开的仓，只由 `2h` 收盘推进。`exit_exec` 与 `pivot_exec` 保持默认 `False`（已被 84 组实验证明跨小周期提前裁决只会把正常回抽变成割肉）。
- **盘中硬止损兜底**：初始防线 `invalid` 作为交易所原生仓位级止损（回放中由 `broker.py` 按 K 线高低点穿越触发），盘中触及即止损出局，绝不抗单。

### 5.2 两类原语的正交出场引擎
1. **`REJECT`（箱内均值回归单）$\implies$ 对侧锚定 + 峰值回吐预算锁利（`opp_lock_step`）**
   - **初始止损**：刺穿极值外侧 `ex ∓ k_bm * ATR`。
   - **目标锚点**：`target_1 = box_opposite`（活跃夹板对侧内沿 `VAH` 或 `VAL`）。
   - **为什么不在半路保本，也不在对侧 100% 死限价平仓？**
     - 半路（`< 1R`）推保本会被箱内正常回抽扫出局；到对侧死限价平仓又会错失假突破演变为大级别趋势反转的肥尾。
   - **`opp_lock_step` 锁利公式（已内置于 `position.py:134-225`）**：
     - 建仓时冻结回吐预算：`budget = (1 - |reject_opp_lock_frac|) * |target_1 - entry|`（生产值 `reject_opp_lock_frac = -0.25` $\implies$ 允许回吐 `25%` 行程，锁住 `75%` 行程）；
     - **激活条件（Arming）**：当认知 K 线收盘越过 `target_1`（或盘中触及且配置为正号）时，`pos.armed = True`；
     - **棘轮推进**：激活后每根 K 线更新最有利极值 `peak = pos._best`，将保护止损单调收紧至：
       $$\text{stop}_{\text{long}} = \max(\text{invalid},\ \text{peak} - \text{budget}), \qquad \text{stop}_{\text{short}} = \min(\text{invalid},\ \text{peak} + \text{budget})$$
2. **`ACCEPT`（破位迁移顺势单）$\implies$ 无上限目标 + 脉冲激活确认 Pivot 棘轮移动止损**
   - **初始止损**：翻转边界另一侧（或 `turtle_stop_atr = 2.0 * ATR`）。
   - **目标设置**：`target_1 = None`（不设死上限，吃趋势右尾）。
   - **移动防线（`PositionManager._trail`）**：
     - 当持仓浮盈达到安全区（`MFE >= 1.0R` 或确认新的顺向推进 Pivot）后，本周期每确认一个新的同向防守 Pivot（多头 `PivotKind.LOW`、空头 `PivotKind.HIGH`），若 `pivot.price ∓ k_bm * ATR` 优于当前 `invalid`，立即单调收紧 `invalid`！

---

## 6. 第五层详规：资金容量与反蚂蚁仓风控层（L5 — 104U 阶梯复利）

> **代码位置**：`src/market_operator/capacity.py`、`src/market_operator/broker.py`、`config/strategy.json`、`config/live.json`。

1. **小资金阶梯动态风险预算（`dynamic_risk_tiers`）**
   - 针对 104 USDT 起步账户，按当前账户净值 `equity` 自动匹配单笔基础风险比例 `risk_frac`：
     - `equity <= 250 USDT`: **`3.0%`**（在 104U 下约 `3.12 USDT`/笔，彻底消灭 0.3U 蚂蚁仓与步长截断损耗）；
     - `250 < equity <= 600 USDT`: **`2.5%`**；
     - `600 < equity <= 1500 USDT`: **`1.8%`**；
     - `equity > 1500 USDT`: **`1.0%`**。
2. **事件×方向期望权重（`edge_size_by_event_direction`）**
   - 保留 `main` 实盘验证过的“只缩不放”机制：对期望较高的 `REJECT` 保持 `1.0x`；对追突破类的 `ACCEPT|long` 乘以 `0.35x ~ 0.60x` 权重。
3. **定仓公式与反蚂蚁仓硬闸门（`admission_full_cost` + Whole-Trade Gate）**
   - **定仓数量**：`qty = floor_step(min(risk_budget / |entry - invalid|, leverage_cap * equity / entry), size_step)`；
   - **整笔准入跳过纪律**：
     - 若计算出的 `qty < contract.size_min` 或 `qty * entry < max(contract.min_notional, 0.25 * equity)`，或者因容量上限导致实际风险 `< 0.60 * risk_budget` $\implies$ **直接整笔拒绝（记 `SKIP: MIN_SIZE / CAPACITY`）**，绝不下残缺蚂蚁仓占用槽位！
4. **组合总风险池与跨标的同向净风险锁（防止主流币同向暴跌连环爆）**
   - 账户总风险池上限 `portfolio_cap_frac = 0.12`（小资金复利模式下上限不超过 `0.15`，严禁设回旧实盘的 `0.50`）；
   - **新增同向净风险上限 `directional_cap_frac = 0.08`**：在 `capacity.py` 准入检查中，统计当前所有在途同方向（全多或全空）持仓的最坏止损风险之和，若加上新意向单超过 `directional_cap_frac * equity`，整笔跳过（记 `SKIP: DIRECTIONAL_CAP`）。

---

## 7. 傻瓜式分步施工与验收清单（Step-by-Step Execution Checklist）

后续施工必须严格按以下 **3 个原子步骤** 顺序执行，每完成一步必须跑通对应测试与回放验证，严禁跳步：

- [x] **Step 1（已完成）：恢复 `main` 底座 + 实现独立 `TrueBalanceDetector` 与 `ActiveBracket`**
  - 产物：`src/market_operator/true_balance.py`、`tests/test_true_balance.py`、`scripts/_diag_true_balance.py`。
  - 验收指标：全仓 `683 + 4` 测试通过；`BTC/ETH/SOL` 在 `1H/2H/4H` 上斜通道误判率降至 `0.0%`，场上活跃夹板数严格 $\le 2$，运行耗时较旧版提速 `12x~17x`。
- [x] **Step 2（已完成）：将 `TrueBalanceDetector` 桥接进 `structure.py` 并升级 `events.py` / `routing.py` 双原语（补齐 R3 假突破吞没收复与回抽 Limit 挂单）**
  - **动作 2.1**：已修改 `src/market_operator/structure.py` 的 `ZoneBook._sync_balance_boxes()`，当 `use_balance_detector=True` 时调用 `TrueBalanceDetector.push()`，将 `ActiveBracket` 的 `support / resistance` 映射为场内唯一支撑/阻力 `Zone`（保留 `[lo_ext, VAL]` 与 `[VAH, hi_ext]` 双层边界及 `box_opposite`，原位更新保持 `id(z)` 不中断 `_Episode`）。
  - **动作 2.2**：已修改 `src/market_operator/events.py` 的 `_zone_events()` 及 `src/market_operator/trigger.py`，在保持 `LEVEL_REJECTED (REJECT)` 与 `BREAKOUT_ACCEPTED (ACCEPT)` 标准事件名不变的前提下，补齐 **R3（1~2 根收盘假突破后吞没收复回内沿发 `REJECT`）** 与 **`CONSUMED`（超时粘连淘汰）**。
  - **动作 2.3**：已在 `src/market_operator/routing.py` 中增加确认 K 线过伸检查：当 `REJECT` 确认 K 线过长导致剩余 RR 不足（以及 `A2_OVEREXTENDED` 过伸突破）时，自动转为挂内沿/50% 回撤 `RestingLimitOrder`（`kind="limit"`）。
  - **验收结果**：专项测试 `tests/test_v22_pipeline.py` 及全仓测试全部通过。
- [x] **Step 3（已完成）：在 `capacity.py` 增加同向净风险锁 `directional_cap_frac` + 跑通两年全量对比回放**
  - **动作 3.1**：已在 `src/market_operator/capacity.py`、`broker.py`、`campaign.py` 与 `scenario.py` 中加入同方向风险聚合检查（`directional_cap_frac = 0.08`，超限记 `DIRECTIONAL_CAP` 整笔跳过），并收紧 `config/strategy.json` 中小账户并发总敞口至 `0.15` 及 `3.0% -> 2.5% -> 1.8% -> 1.0%` 动态风险阶梯。
  - **动作 3.2**：已通过 `scripts/_diag_v22_replay.py` 跑通两年全量四标的（`BTCUSDT / ETHUSDT / SOLUSDT / DOGEUSDT`）多周期（`2h / 4h / 12h / 1d`）回放，样本内 `dev@1.0x`（`+15.91U, PF=1.03`）、样本外 `test@1.0x`（`+72.63U, WR=41.5%, 盈亏比=2.22, PF=1.28, E[gR]=+0.232R`）与全期 `full@1.0x`（`+96.40U, PF=1.12, MaxDD=31.39%`）全面正期望，104U 阶梯复利终值增长至 `184.46U (+77.4%, 同向锁 0.08)` / `329.65U (+217.0%, 无同向锁)`。



