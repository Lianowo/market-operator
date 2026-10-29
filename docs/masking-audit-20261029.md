# 掩蔽审计：仍在用「过滤 / 门槛 / 参数」掩盖语义缺陷的位置（2026-10-29）

> 性质：**只列不改**。除已标注「已落地」的两处底层语义修复外，本文件所有条目
> 都保持现状，等逐条确认后再动。
> 判定口径：`dev` 与 `test` 同向变好、`full@1.5x` 不恶化、去 Top10 不恶化
> ⇒ 该装置在掩盖语义；**反向则说明它有独立信息量，必须保留**（本文件如实标注）。
> 复现：`python scripts/_diag_masking_audit.py [V0 V1 ...]`（认知帧在
> `ScenarioRunner` 构造时就已缓存，凡涉及事件层的变体必须重建 runner，否则读数
> 会与基线逐位相同 —— 这是本脚本第一版的真 bug）。

## 0. 已落地的两处（对照基线，非本轮候选）

| 项 | 位置 | 修复 |
|---|---|---|
| `REJECT` 用影线当回退 | `events.py` / `trigger.py` 的 `_reject` 调用点 | 改由**收盘价**完成抽离、且收在内沿以内、收盘偏向拒绝方 |
| 细周期不建 episode ⇒ 入场迟 10~20h | `trigger.py` `MicTracker.on_trigger_bar` | 细周期触碰即建 `_MicEpisode`；`n_a_mic` 不再 ×scale |

## 1. 已实测：**不是**掩蔽，必须保留（反向证据）

| 变体 | 读数（full@1.0x / 去Top10 / 1.5x） | 结论 |
|---|---|---|
| 基线 V0 | +248.45 / +0.23 / +13.09 | — |
| V4 `accept_use_struct_stop=True` | +196.81 / **−34.55** / **−7.98** | 突破用「贴边缘」止损会被回踩扫掉；`2.0·ATR` 是承重的 |
| V5 `invalid` 改锚 `ep.ex` | +204.49 / **−23.59** / **−19.33** | 箱体**外沿**才是合法证伪位，不是本次极值 |
| V1 `min_bar_impulse_atr 1.2→0` | +65.40 / **−120.14** / **−161.49** | 语义修好后该过滤**更有价值**（挡 2h/4h 噪声触发） |
| V2 `zone_cooldown_bars 4/3→0` | +216.82 / **−19.29** / +3.85 | 同沿连刷确实需要抑制 |

## 2. 已实测：**死旋钮（inert）**

| 项 | 位置 | 现象 |
|---|---|---|
| `RouterParams.min_box_rotations`（3/3/1/2） | `routing.py:135` + `config/strategy.json` | V3 拆成 0 后四个分段**逐位不变**：`TrueBalance` 出生条件（`min_swings=4` ⇒ ≥3 段折返）已保证 `rotations ≥ 3`，这道闸门永远不会拒单。真正的闸门是结构层的 `Params.box_min_rotations`（`structure.py:84`，默认 1）。同一件事有两个旋钮且其中一个恒不生效 —— 属于「配置看起来在调参、其实没接线」。 |

## 3. 待确认：**语义被参数顶替**的清单（未改）

| # | 位置 | 参数在顶替什么语义 | 建议的语义化方向（待批） |
|---|---|---|---|
| A1 | `config/live.json` `risk.edge_size_by_event_direction["BREAKOUT_ACCEPTED\|1"]=0.35`；消费点 `live_runner.py:2318` + `edge_size_multiplier` | 用**仓位权重**给一个方向打 35 折，依据是「旧语义下 ACCEPT 多亏钱」。语义修好后回放里 ACCEPT\|LONG 已是正贡献（+28~34U/两年），这条补丁变成**给正期望方向降杠杆** | 先置空 `{}` 跑一轮 live/paper 对照，再决定去留；`edge_size` 机制本身保留（它是风险约束，不是信号） |
| A2 | `position.py:115-128`：`MFE_MIN_R=0.5`、`EARLY_HALF_MFE_R=0.3`、`EARLY_CONSEC_N=3`、`S1_TRAIL_ARM_MFE_R=1.0`、`S2_LEVEL=0.25`、`S2_BANK_FRAC=0.4`、`R4_LEVEL_1/2=0.25/0.50` | **⚠ 更正：本行常量在生产里基本是死代码，不是「正在生效的门槛」。** `campaign.py:456-462` 与 `live_runner.new_position_manager` 都显式 `time_stop=False`、`allow_add=False`、`harvest_family=None`；`arm_config` 只有 `breakeven_on_reduce` 与 `reject_opp_lock_frac` 两个键 ⇒ `harvest_ladder3 / early_exit / trail_arm_gate / harvest_fade_2leg` 全为 False ⇒ `MFE_MIN_R / EARLY_* / S1 / S2 / R4` 这些 R 倍数**永远不会被读到**。真正生效的只有 `reject_opp_lock_frac=-0.25`（唯一出口）与 `breakeven_on_reduce=True` | 若日后重新启用 S2/R4 分段落袋，必须先把 0.25/0.50 换成 POC / 对侧内沿 / 新确认 pivot 三个结构节点，再谈启用 |

| A3 | `routing.py:127` `k_is=0.8`（F1 闸） | 「止损太近 ⇒ 成本占比过高」被写成**ATR 倍数**。真实约束是 `成本/风险 ≤ x%`；当费率或波动率结构变化时，0.8·ATR 会跟着漂 | 改成显式成本地板：`(taker+slippage)·2 / risk ≥ 阈值` 用**钱**比，而不是用 ATR 比 |
| A4 | `routing.py:145-146` `max_confirm_stop_atr=1.2` + `allow_pullback_limit` | 「止损太宽」→ 不进场、改挂 50%/内沿回抽限价。它补的是**入场价与带内沿的距离**，而 V5 证明止损锚点本身是对的 | 该补丁存在即说明「确认 K 线离带太远」，源头是 A6 允许在箱体半山腰确认；应修「确认位置」而不是修「下单方式」 |
| A5 | `routing.py:141` `reject_target_mode="opposite"` + `routing.py:144` `max_target_r` | 目标锚在对侧内沿，宽箱时目标不可达 ⇒ 又加一个 `max_target_r` 截断。**两个旋钮互相打架** | 目标应取「对侧内沿与最近 pivot 的较近者」，用一次几何判断取代「锚点 + 截断」 |
| A6 | `true_balance.py:38-54` 七个 `w_*` 权重 + 七处手调归一化(`/0.50 /0.35 /0.30 /2.0 …`) + `quality_min=0.35` | 用**加权几何均值分数**回答「这是不是一个平衡区」。拍卖理论的判据是四件事：重叠、有界、多次折返、无方向性推进 | 改成四条**谓词**（每条 True/False 且可解释），`quality` 只作排序键不作准入门槛 |
| A7 | `true_balance.py:49-52` `max_slope_atr_per_10=0.30`、`dwell_min=0.68`、`poc_zone_lo/hi=0.25/0.75` | 「水平」「没跑出去」「成交重心居中」都用连续量阈值近似 | 水平度用「上下沿在窗口内的极值顺序」判定；居中用「POC 落在 [VAL, VAH]」判定（本质是区间归属，不是百分比） |
| A8 | `true_balance.py:41-42` `width_min_atr=1.80 / width_max_atr=12.0` | 「可交易跨度」用 ATR 倍数定，替代了「跨度 ≥ 覆盖成本的最小 R」 | 下限改由成本反解（`min_R × 风险距离 ≥ 往返成本`），上限由结构（是否仍在同一箱体）决定 |
| A9 | `structure.py:77` `merge_max_atr`（生产 2.0）、`structure.py:78` `stale_after_bars`（生产未设） | **⚠ 更正：`use_balance_detector=true` 时 `on_pivot()` 第一句就 `return`（`structure.py:354-355`），`_insert_merged` / `merge_max_atr` 整条历史合并路径被旁路** ⇒ 生产值 2.0 实际不参与任何判定。`stale_after_bars` 唯一生效点是 `structure.py:629`，但 TrueBalance 桥接走的是 `active_by_role` + `box_keep_*_bars`，两者是两套老化机制 | 删掉未被旁路路径使用的旋钮，或明确只保留一套老化/合并语义 |

| A10 | `capacity.py:149-157`（docstring 自述）+ `capacity_mode="admission_full_cost"` | 代码自己写着「周期池存在时账户池不参与准入，4×2.25% < 12% ⇒ 账户池实际上从不生效」，`symbol_frac` 恒为空 | 账户池要么接线（参与准入），要么删掉；留一个永不触发的约束会让人误以为有全账户风险上限 |
| A11 | `config/strategy.json` `n_fill=12`（挂单 12 小时过期） | 「这张限价单该不该留」被**墙钟时长**代替；结构语义是「箱沿被破/被取代则撤」 | 过期条件接结构事件（zone BROKEN/DEAD/superseded），墙钟只作最后兜底 |
| A12 | `trigger.py` `n_f_mic = n_f × scale`（12h→2h 时窗口 18 根） | 与我已修的 `n_a_mic` 同一类：细周期把窗口放大 `scale` 倍，等于把「N_f 根认知 bar 内收复」偷换成「N_f 根 ×scale 细 bar 内收复」 | 统一按**墙钟/认知窗口**换算，而不是按根数乘 scale |
| A13 | `routing.py:130` `whitelist`（event × direction） | 一个能把某个方向**整体静默删除**的开关。当前四原语全开是对的，但机制本身鼓励用「关掉」代替「修好」 | 保留但加落地校验：任何非全开的 whitelist 必须在启动日志与 TG 日结里显式声明「本方向被禁用」 |
| A14 | `position.py` `n_time=24` `time_stop` | 「持太久没进展」用**bar 数**表达；结构语义是「N 根内没有出现新的顺向 pivot」 | 时间帽改挂结构事件（无新 pivot 计数） |
| A15 | `adopt.py` 兜底 `turtle_stop_atr`（本次新增） | 无结构时的兜底本身就是参数。生产实测：本次两个被接管仓位（`BEUSDT` / `MSFTUSDT`）都落在 `atr_fallback`，因为当时 12h 战役在正确一侧没有 ACTIVE 带 | 如实标注；后续可让接管层在**更细周期**（2h/4h）取结构，减少兜底比例 |

## 4. 关于 `min_box_rr = 1.4 ~ 1.8` 这类门槛

**是门槛参数**，而且有实测证据说明它**曾经在掩盖慢入场**：

语义修好之后（同一套代码，仅改 `min_box_rr`）：

| `min_box_rr` | full@1.0x | 去Top10 | 1.5x |
|---|---|---|---|
| **0（现状）** | **+248.45** | **+0.23** | **+13.09** |
| 1.0 | +96.76 | −93.49 | −4.25 |
| 1.2 | +121.13 | −87.71 | +15.51 |
| 1.4 | +159.90 | −58.72 | +78.12 |
| 1.6 | +142.21 | −74.33 | +59.02 |

- 修好入场之前：`min_box_rr=1.8` 能把 full 从 +106 抬到 +225 —— 因为它**间接起到了「不要在箱体半山腰进场」的作用**，也就是用盈亏比门槛替代了「什么叫拒绝」的定义。
- 修好之后：同样的门槛让 full 从 +248 掉到 +160，去 Top10 从 +0.23 掉到 −59 —— 它开始**砍掉真实有效的高胜率反转**（先前解剖过：那批 `RR<1.0` 的单子胜率 54.8%，问题从来不是它们，而是入场晚）。
- 结论：**保持 0**。它唯一合理的形态是 `1.4` 附近的成本保险（1.5x 下 +78 好于 +13），但那属于「成本上行保险」，不是常态参数，且必须与 A3 的成本地板合并考虑，不能当成两个独立旋钮。

---

## 5. 状态总账（回答「这些是不是都没修」）

**不是。** 按「是否真的在生效 / 是否已修」分四类：

### ① 已修复（本轮，已上线并跑在实盘上）
| 项 | 内容 |
|---|---|
| `REJECT` 语义 | 影线伪回退 → 由**收盘价**完成抽离、收回内沿、收盘偏向拒绝方（`events.py` / `trigger.py`） |
| 细周期入场迟缓 | `MicTracker` 细周期触碰即建 `_MicEpisode`；`n_a_mic` 不再 `×scale`（8.1 倍 dev 提升的来源） |
| 交易所客户端 | `ACCESS-TIMESTAMP` 每次重试重签；`40008/40009/40012` 重签重试 |
| 运维重启 | 启看护前作废死心跳（重启恢复 5min → 30s） |
| 新增 | 交易所既有仓位接管（`adopt.py`，执行层，策略链路零改动） |

### ② 实测证明「不是缺陷」，因此**故意保留**（不是「没修」）
`min_bar_impulse_atr=1.2`、`zone_cooldown_bars=4/3`、`turtle_stop_atr=2.0`（`accept_use_struct_stop=False`）、`invalid` 锚箱体外沿 —— 拆掉任一项都显著变差（见 §1）。

### ③ 死旋钮 / 死代码（不影响结果，属清理项）
- `RouterParams.min_box_rotations`（§2，V3 逐位不变）
- `MFE_MIN_R / EARLY_* / S1 / S2 / R4`（A2：相关开关全关，常量永不读取）
- `merge_max_atr`、`stale_after_bars` 在 TrueBalance 路径下被旁路（A9）

### ④ **真正未修、且正在生效**（等确认后再动，共 9 条）
| # | 生效证据 | 现状影响 |
|---|---|---|
| **A1** `edge_size["BREAKOUT_ACCEPTED\|1"]=0.35` | `config/live.json` 现值 + `live_runner.py:2318` | **实盘正在给 ACCEPT 多打 35 折**；而该方向语义修好后已是正贡献 ⇒ 目前唯一「确定在亏效率」的一条 |
| A3 `k_is=0.8`（ATR 单位成本闸） | 每次路由准入都过 | 费率/波动结构变化时阈值会漂 |
| A4 `max_confirm_stop_atr=1.2 + allow_pullback_limit` | `routing.py:145-146`，`allow_pullback_limit` 默认 True | 用「改下单方式」补「确认位置太远」 |
| A5 `reject_target_mode=opposite + max_target_r` | `routing.py:141/144` | 目标锚点与截断两个旋钮互相打架 |
| A6/A7/A8 平衡箱 7 权重 + 连续量阈值 | `true_balance.py:38-54` | 判「是不是平衡区」用复合分数而非四条谓词 |
| A11 `n_fill=12`（墙钟 TTL） | `strategy.json` + `campaign.py` 挂单过期 | 结构失效了还要等 12h 才撤单 |
| A12 `n_f_mic = n_f × scale` | `trigger.py` | 12h→2h 时窗口被放大成 18 根（与我已修的 `n_a_mic` 同类，尚未改） |
| A14 `n_time=24 / time_stop` | 常量在；但 `time_stop=False` | **注**：当前不生效（同 A2 死开关），只有重新启用时才成为问题 |

> 一句话：**真正「没修且在生效」的是 A1、A3、A4、A5、A6、A7、A8、A11、A12（9 条）**；A14 属死开关、A15 是新代码的已知限制；②类是我实测后决定**不动**的。

