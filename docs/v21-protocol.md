# v21 区域模型与实盘统一架构协议（平衡箱识别 + ACCEPT/REJECT 双原语 + 高阶降档混合触发 + 独立容量池与复利收割）

> 冻结日期：2026-10-21。本协议记录从 **v21.1 区域模型诊断** 到 **v21.6 极简平衡箱统一架构与实盘全量落地** 的完整演进。
> 全部机制均为 **opt-in 参数**（默认关闭），历史 canonical parity 逐位不变（全仓 605 测试通过）。
> 参考实现：同作者 `edge-engine` 项目的 `ms/` + `zones.py` + `candidates.py`（MIT，见 `docs/audits/`）。

## 1. 问题诊断（实测，1h BTC 17600 bars）

历史单带区域模型 = 每个 pivot 造一个 0.5·ATR 半宽区域 + "重叠则并（2·ATR 封顶）"，无年龄淘汰。实测：

| 指标 | 历史 |
|---|---|
| 活跃区域 | 255（6368 个 pivot 合并后） |
| 相邻阻力区间距中位 | **−2.44 ATR（负 = 深度重叠）** |
| 阻力区重叠对数 | 125 / 146 |
| 区域年龄中位 | 3908 bars（阻力）/ 1502 bars（支撑） |

三个根因：① 每个 pivot 都造区（噪声）；② "重叠 + 2·ATR 封顶"制造重叠链；③ 无年龄淘汰。

## 2. 第一阶段（v21.1–v21.2）：ZoneBook 四个 opt-in 机制与 Pivot 平衡箱

| 参数 | 语义 | 来源 |
|---|---|---|
| `stale_after_bars` | `last_change_known` 超过 N bars 移出视图（久远无价值） | edge-engine `ranges.py` STALE |
| `min_amplitude_atr` | 腿位移幅度不足的 pivot 不造区 | edge-engine `sig_amplitude_atr` |
| `use_same_balance` | 合并判据改为 `same_balance`（共享>80%跨度 或 高度相近且包含） | edge-engine `zones.py same_balance` |
| `use_balance_recognition` | 仅"失败延伸"的 pivot 造区（`close_extreme <= 前高+tol`） | edge-engine `zones.py recognize` |

- `Pivot` 新增 `close_extreme`（极值 bar 的收盘），供失败延伸判据使用。
- `src/market_operator/balance.py`（`BalanceBook`）：基于失败延伸的箱体识别、合并、取代（supersede）与年龄过期。
- 实测（1h BTC）：`stale=200` 将活跃区域从 255 降至 20（年龄中位 3908→5 bars），叠加失败延伸降至 11 区。

## 3. 第二阶段（v21.3–v21.4）：拍卖市场理论六维框子识别（`balance_detect.py`）与 ZoneBook 桥接

为直接从 K 线价格行为识别高置信度震荡区间（平衡箱），实现 `src/market_operator/balance_detect.py`（`BalanceDetector`）：

- **多尺度窗口扫描**：回看阶梯 `ladder = (8, 13, 21, 34, 55, 89, 144)`。
- **稳健分位数边界**：上沿取窗口 high 的 88% 分位、下沿取窗口 low 的 12% 分位，单根影线尖刺不拉宽箱体。
- **六维打分 + 惩罚项**：方向效率（efficiency）、驻留率（occupancy/dwell）、相邻重叠度（overlap）、路径遍历比（traversal）、上下沿轮动平衡（rotations/balance）、边缘触碰紧致度（edge MAD）与半窗重心稳定性（stability），前缀和 O(1) 加速。
- **去重抑制（`_suppress`）**：按质量分降序保留，剔除共享跨度 >80% 或高度相近且包含的冗余重叠箱。
- **ZoneBook 桥接（`use_balance_detector=True`）**：每个存活 `BalanceBox` 派生下沿 `SUPPORT` 与上沿 `RESISTANCE` 两条带，并在 `Zone` 上携带箱体元数据：
  - `box_opposite`：箱体对侧缘价格（供 REJECT 目标与空间计算）；
  - `box_quality`：箱体六维综合质量分；
  - `box_width_atr`：箱体宽度（ATR 倍数）；
  - `box_rotations`：箱体上下沿完整往返轮动次数。

## 4. 第三阶段（v21.5）：极简双原语（ACCEPT / REJECT）与高阶降档混合触发（M3c）

将交易事件收敛为拍卖市场理论的两个正交动作原语（`events.py` / `strategy_v6.py`）：

1. **`ACCEPT`（别名映射 `E1`，路由 `accept_market` / `e1_market`）**：箱体边缘突破接受（顺趋势迁移），市价入场。
2. **`REJECT`（别名映射 `E3`，路由 `reject_market` / `e3_market`）**：箱体边缘刺穿拒绝（箱内均值回归），市价入场。

### 跨周期触发模式对比与 M3c 混合触发

在 `tf_list = ["2h", "4h", "12h", "1d"]` 四周期并发体系下：
- **2h / 4h（中短周期）**：采用**同周期收盘确认触发**（`trigger_tf = null`），并施加强克制过滤（`min_box_rotations=3`, `min_box_quality=0.50/0.45`, `min_bar_impulse_atr=1.2`, `zone_cooldown_bars=4/3`），杜绝小级别抢跑与同一箱沿高频刷单。
- **12h / 1d（高阶周期）**：采用**降一档细周期触发**（`12h -> 2h`, `1d -> 4h`），在大箱体边缘用次级收盘确认第一时间入场，避免 12h/1d 等收盘时利润空间已被吞噬。

## 5. 第四阶段（v21.6）：结构止损 + REJECT 对侧突破锁利 50% + 独立容量池与阶梯动态复利

1. **分类初始防线与目标锚定（`V6Params`）**：
   - `reject_use_struct_stop=True`：REJECT 使用箱体外缘结构止损（拒绝刺穿极值外侧，天然窄止损、高盈亏比），`reject_target_mode="opposite"` 将 `target_1` 锚定在箱体对侧缘 `box_opposite`。
   - `accept_use_struct_stop=False`：ACCEPT 保持 2.0·ATR Turtle 宽止损与无上限移动防线。
2. **REJECT 对侧突破不减仓、直接锁利 50%（`PositionManager.reject_opp_lock_frac = -0.50`）**：
   - 当 REJECT 仓位触及或突破箱体对侧缘 `target_1` 时，**不执行平半仓**（避免截断大级别反转趋势），而是将防线 `invalid` 推进到锁利位（若仅影线触及未收盘突破则先锁 10% 保本垫），剩余全程交由 §8.3 确认 pivot 移动防线收割。
   - **锁利价口径（2026-10-23 实盘事故修复，唯一出口 `position.opp_lock_*`，`position.on_bar` 与 `live_runner._on_sub_bar` 共用）**：
     - **计划口径** `opp_lock_px(entry, target, frac) = entry + |frac|·(target − entry)`：锁的是 **entry→target 行程**的比例（不是箱体跨度；仅当建仓价恰在箱缘时二者等价）。
     - **落位闸门** `lock_is_protective` / `lock_is_profitable`：候选锁利价必须 ① 在**当下可见价**（逐 bar = 收盘价 c；实盘子周期 = c 与 mark 的更保守一侧）的保护侧，且 ② 在 **entry 的盈利侧**。任一不满足 ⇒ **不推进防线、不置 `reduced`**，台账写 `v21|*_lock_reject`（实盘另记 `kind=lock_reject` journal）。
       修前的边界缺陷：锁利价落到现价盈利侧 = 把唯一一张保护单变成"挂单即按市价成交"的止盈单，仓位瞬间裸奔；退化行程（建仓价已在箱内/越过对侧）还会把锁利价算到 entry 亏损侧 = 凭空收紧亏损止损。
     - **行情锚定** `opp_lock_live_px(direction, ref, giveback_r, R) = ref ∓ giveback_r·R`：锁利只在收盘裁决，而收盘价往往已走过计划口径的位置 ⇒ 计划价退化成"等回踩的止盈单"。GOOGLUSDT 样本：计划锁利 323.34 而 2h 收盘 321.33（空头），锁利价落在现价身后 0.63%（0.65R），名义锁 50% 实际只锁到 0.24R。两条口径取**更贴近现价者**（`tighten_lock`，空头取小/多头取大）⇒ 既满足协议行程比例，又不回吐超过 `giveback_r` 个 R。
     - 开关：`config/v21.json → arm_config.opp_lock_max_giveback_r`（默认 **0.25**；`0` = 关闭行情锚定，逐位退回旧计划口径）。
   - **证据**：`reports/opp_lock_ab.md`（生产口径 A/B，3 币 × 4 TF）。同一锁利事件下，计划口径的落地价距现价 **+1.31R**（只锁到 0.36R），0.25R 锚定把它压到 **+0.25R**、锁到 **0.66R**。锁利事件在回测样本里只有个位数，净利差异由容量路径主导，**不据此调参**。
   - **覆盖缺口（同批修复的观察项）**：`_diag_v21_backtest.py` 未设置 `reject_target_mode`，其 M3c 回放里 `target_1` 恒为 `None` ⇒ §8.2 锁利在历史回测中**从未被触发**（实盘缺陷因此逃过回测）。生产口径请看 `scripts/_diag_opp_lock_ab.py`（复用 `live_runner.load_v21_params` 的 per-TF `reject_target_mode="opposite"`）。
   - **保护性止损全天候生效**：修复 `broker.py` 中同向挂单覆盖保护性止损的边界缺陷，确保从成交下一根细 bar 起 `PROTECTIVE_STOP` 100% 在场。
3. **各周期独立风险容量池（`tf_cap_by_tf`）**：
   - `campaign.py` 与 `live_runner.py` 支持按周期独立分配风险上限（回测基线每 TF `2.25%` / 总 `12%`；小资金实盘复利每 TF `12%` / 总 `50%`），彻底解决 2h/4h 持仓占满容量导致 12h/1d 高胜率大单被 `cap_skip` 挤出的问题。
4. **小资金阶梯动态复利（`small_account_compounding` / `config/live.json`）**：
   - 104 USDT 起步 × 20x 杠杆上限；
   - 按账户实时净值阶梯调整单笔风险：`<=250U: 4%`、`<=600U: 3%`、`<=1500U: 2%`、`>1500U: 1%`。
5. **实盘多周期并发与双机器人黑白 K 线通知（`live_runner.py` + `tg.py`）**：
   - `LiveRunner` 在 `engine="v21"` 下自动为每个品种装配 `2h`、`4h`、`12h(->2h)`、`1d(->4h)` 四个独立战役实例。
   - `TelegramNotifier` 支持信号 Bot（信号/日结/告警）与交易 Bot（开仓/减仓/移动防线/平仓）双通道分离，内置无依赖纯 Python 高清黑白 K 线与平衡箱体 PNG 渲染器（`render_bw_kline_png`），每行一条 Emoji 关键信息配图直发。

## 6. 回测验证结果（BTC / ETH / SOL / DOGE，2024-10 → 2026-10）

| 模式 / 配置 | dev @ 1.0x | dev @ 1.5x | test @ 1.0x | test @ 1.5x | full @ 1.0x |
|---|---|---|---|---|---|
| v21 M3c 统一架构（300U 固定基准） | +360.8 USDT (PF 1.48) | +321.4 USDT (PF 1.41) | +214.5 USDT (PF 1.59) | +193.2 USDT (PF 1.51) | **+575.3 USDT (gR +0.351, WR 52.8%)** |
| v21 小资金阶梯复利（104U / 20x） | — | — | — | — | **104U → 2,523.7 USDT (+2326%, MaxDD 15.8%)** |

- 四周期（`2h` / `4h` / `12h` / `1d`）在 `dev` 与 `test` 段、1.0x/1.5x/2.0x 成本压力下**全部独立正期望**。

## 7. 可复现命令

```bash
python scripts/_diag_zones.py              # v21.1 单带区域数量/间距/年龄对照
python scripts/_diag_balance.py            # v21.2 Pivot 平衡箱对照
python scripts/_diag_balance_detect.py     # v21.3 六维框子识别器诊断
python scripts/_diag_v21_backtest.py       # v21 M3c 多周期回测与成本压力验证
python scripts/_diag_opp_lock_ab.py        # 锁利口径 A/B(计划口径 vs 行情锚定 giveback_r)
python scripts/live_paper_check.py         # v21 实盘多周期装配与真网行情 paper 冒烟校验
python -m pytest -q                        # 633 passed（全量回归 + v21 专项测试）
```

