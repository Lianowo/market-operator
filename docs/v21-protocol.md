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
   - 当 REJECT 仓位触及或突破箱体对侧缘 `target_1` 时，**不执行平半仓**（避免截断大级别反转趋势），而是将防线 `invalid` 直接推进至锁住 **`entry→target_1` 行程 50%** 的位置（若仅影线触及未收盘突破则先锁 10% 保本垫），剩余全程交由 §8.3 确认 pivot 移动防线收割。
   - **锁利价唯一口径**：`position.opp_lock_px(entry, target_1, frac) = entry + |frac|·(target_1 − entry)`，`position.py` 与 `live_runner.py`（`_on_sub_bar` 子周期早锁）共用同一函数，禁止各自重算。注意它是"行程比例"而非"箱体跨度比例"：只有建仓价恰在箱体边缘（canonical REJECT）时二者等价；建仓价深入箱体时行程远窄于箱体，锁利价会明显更靠近建仓价。
- **行情锚定 `opp_lock_max_giveback_r`（2026-10-23 复盘追加；默认 `0.25`，0 = 关闭）**：锁利只在**收盘**裁决，收盘价往往已经走过了计划口径算出的位置 ⇒ 计划锁利价退化为"价格得先回踩到那里才生效"的止盈单。GOOGLUSDT 样本：计划口径 323.34 落在 2h 收盘 321.33 **身后 2.01（0.63%）**，不回踩等于没锁，回踩触发时已把 1.08R 行程回吐到 0.24R（实际成交 324.01）。故锁利价取两口径**更贴近现价者**：
  - 计划口径 `opp_lock_px(...)`：至少锁到 `entry→target_1` 行程的 `|frac|`；
  - 行情口径 `opp_lock_live_px(direction, ref_px, giveback_r, R) = ref_px ∓ giveback_r·R`：只允许回吐 `giveback_r` 个 R（以 R 为单位，2h/12h/不同币种同尺度，不依赖 ATR 管线）；
  - `tighten_lock(direction, plan_px, live_px)`：空头取更小、多头取更大（更贴近现价 = 更紧），仍须通过落位闸门。
  该口径只改变"锁利价已在现价身后"的情形（首次触及时两种口径几乎重合），`giveback_r = 0` 时逐位回到历史行为。
   - **落位闸门 `lock_is_protective(direction, lock_px, ref_px)`（2026-10-23 GOOGLUSDT 事故修复）**：锁利/保本推进价必须仍在**当下参考价的保护侧**（空头 `> ref_px`、多头 `< ref_px`，`ref_px` 取当时收盘价或更保守的 `mark`）。闸门不通过 ⇒ **不推进**（`pos.log` 记 `v21|opp_break_lock_reject` / `v21|opp_lock_reject`，实盘另记 `lock_reject` 台账 + 限流告警），保留原结构止损，且**不置 `reduced`** 以便后续 bar 重新判定。修前该边界缺陷会把唯一的保护单挂到现价盈利侧：挂单瞬间即按市价成交（GOOGLUSDT 空头：锁利 323.34 于现价 321.33 挂出，46 分钟后按 324.01 成交，比离场时市价差 0.62%），仓位等于裸奔。
   - **盈利侧闸门 `lock_is_profitable(direction, lock_px, entry)`**：候选锁利价还必须落在 **entry 的盈利侧**。退化行程（建仓价已在箱体内侧/越过对侧）下 `opp_lock_px` 可能把锁利价算到 entry 亏损侧，把它当止损推进等于**凭空收紧亏损容忍度**（一分钱没落袋，只增加被扫概率），不属于 §8.2 锁利语义。
   - **证据**：`reports/opp_lock_ab.md`（生产口径 A/B，3 币 × 4 TF）。同一锁利事件下，计划口径的落地价距现价 **+1.31R**（只锁到 0.36R），`0.25R` 锚定把它压到 **+0.25R**、锁到 **0.66R**。锁利事件在回测样本里只有个位数，净利差异由容量路径主导，**不据此调参**。
   - **覆盖缺口**：早期 `_diag_v21_backtest.py` 未设置 `reject_target_mode`，其 M3c 回放里 `target_1` 恒为 `None` ⇒ §8.2 锁利在历史回测中**从未被触发**（实盘缺陷因此逃过回测）。生产口径现统一由 `scenario.py` 按 per-TF `reject_target_mode="opposite"` 装配。

   - **通知图 ENTRY 口径**：`tg.send_fill(..., entry_price=<建仓均价>, price_label=<本次事件价名称>)`。修前图上一律用"本次事件价"画 ENTRY 线，止损推进/平仓通知会把 ENTRY 画在事件价上（例如用 2h 收盘 321.33 顶替建仓价 324.02），图上止损/目标相对 ENTRY 的关系整体错位，读图会把"空头锁利"误读成"止损跑到止盈侧"。
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
python scripts/live_paper_check.py         # v21 实盘多周期装配与真网行情 paper 冒烟校验
python scripts/_diag_v21_scale.py --segments dev,test --funding const,history --xexec
                                           # v21.6 尺度级联 + 出场推进粒度判定(4 标的,~6min)
python -m pytest -q                        # 574 passed, 1 skipped（全量回归 + v21 专项测试）
```

## 8. 生产 `main` 合并口径（v21 分支）

2026-10-24 把 `main` 合进 `refactor/domain-model`（合并提交 `b04e4c9`），再以
`fix(live): port main heartbeat/guard into v21 runner`（`8a63992`）补齐线上安全面。
**没有整文件替换 `scripts/live_runner.py`**：`main` 版依赖 `strategy_v6.V6Router`
与 `routing.TieredParams`，本分支两者都没有，照搬会直接 ImportError。

### 8.1 已移植（功能级）

- **心跳文档 `heartbeat_doc()`**：三个时间事实分开写 —— `ts_ms`（心跳线程还活着）、
  `loop_ts_ms`（主循环最后一次**成功**走完一步）、`step_started_ms`（卡在哪一步）。
  看护据此区分「进程没了」与「进程在但没进展」。
- **每步落盘**：`_save_state()` 内固定写 `heartbeat.json`（try/except 包裹，心跳失败
  不得阻断交易链路）；启动 `_load_state()` 之后立刻写一次，避免看护把新进程误判为卡死。
- **路径归属**：显式参数 → `state_path` 同目录 → `config.loop.heartbeat_path`（锚定
  `ROOT`）。2026-10-24 的缺陷正是 cwd 相对路径让测试覆盖了生产
  `runtime/live/heartbeat.json`；新增 `--heartbeat-path` 供沙盒指到临时目录。
- **`HeartbeatGuard` 线程**：`main()` 里随主循环起动（live 自我熔断退出码 3，paper 关闭），
  只做保护面 —— 核对交易所计划单、缺失则按本地防线补挂，不下开仓单、不改策略、不改仓位。
- **锁利价双闸门**：`tighten_lock(direction, plan_px, live_px, ref_px, entry_px, min_buf)`
  先对两个候选各过 `lock_is_protective` / `lock_is_profitable`，**再**在通过的候选里取更紧者。

### 8.2 未移植（与 v21 路由层冲突）

`main` 的 campaign/lifecycle 差异依赖 `strategy_v6` 路由决策链；v21 走
`routing.MarketRouter` + `campaign.run_campaigns`。两者在「一步之内谁决定开仓」这一层
语义不同，整段照搬会造出两套路由事实并存。等 v21 侧出现同名接缝（或明确下线 v6 路由）
再谈这一块。

### 8.3 收敛清单

零引用模块/脚本清单与执行记录见 `reports/refactor_20261023/CLEANUP_PROPOSAL.md`。
2026-10-24 已执行:15 个零引用脚本 `git mv` 到 `scripts/archive/`(标签
`pre-cleanup-20261024` 为回滚锚点),`scripts/archive/README.md` 记录退役原因;
核验时否掉两项误判 —— `src/market_operator/split.py`(生产判分契约,`campaign` /
`replay` 相对导入)与 `tape_features.py`(测试白名单引用)**均保留**。

## 9. 第五阶段（v21.6）：时间尺度级联与出场推进粒度

判定报告:`reports/scale_20261024/REPORT.md`(原始读数同目录 `scale.json` / `table.md`)。
工具:`scripts/_diag_v21_scale.py`(`Ctx(tfs=…, trig_map=…)` 参数化装配,`load_all_data`
新增 `1h` 观察周期 —— 认知帧 = 1h、**执行帧 = 15m**)。

### 9.1 结论(全部否决改口径)

| 问题 | 读数 | 判定 |
|---|---|---|
| `1h` 观察周期自身 | E[R] −0.040(const dev)/ −0.042(hist dev)/ −0.047 / −0.035(test);成本/笔比 2h 高 21% | **不做** |
| `2h` 触发 `1h` | dev +0.045 vs 基线 +0.031,test **−0.157 vs −0.117**(反向) | **不采纳**(dev 拟合) |
| `2h` 触发 `30m` | dev +0.040 vs +0.031,test −0.109 vs −0.117(同向 +0.008R,DD +2.3pt) | **不采纳**(噪声带内) |
| `1h` 触发 `30m` | dev −0.051 vs −0.042,test −0.051 vs −0.035(两段同向恶化) | **不级联** |
| 出场推进粒度 `exit_exec` | 12 组中 11 组变差(2h dev −14.1 / test −7.1 USDT) | **保持默认关** |

### 9.2 出场粒度的语义边界(本轮唯一落地的代码)

- `position.PositionManager.on_bar(price_only=True)`:只跑 §8.1 兜底 / §8.2 目标与
  锁利;**不推进** `bars_held`/`consec_adverse`(§8.6 时间帽、v19 半窗仍按认知 bar 计),
  **不消费**事件(§8.5/§8.4)。
- `campaign.ArmConfig.exit_exec`(默认 `False`,逐位旧行为):每根已收盘执行细 bar
  补一次价格型裁决,ATR 用最近一次认知 bar 的冻结值,且只在 `ts > opened_ts` 时触发;
  路由/准入/开仓/事件流一律不经此路径。`_apply_mgmt()` 与认知收盘共用同一落地口径。
- **关键事实**:`_trail()` 只消费 `new_pivots`,而 pivot 只在认知收盘确认 ⇒ 执行粒度上
  没有 pivot 可喂,`exit_exec` 与 `exit_exec_trail=False` 的 84 组读数**逐位相同**。
  「移动止盈被 2h 卡住」的真正解法是让 pivot 在更低周期确认(策略级改动),不是改
  回放时钟。
- 度量:`give-back` 只降 0.01R(1.121 → 1.111),代价是 −14 USDT ⇒ 提前锁利被正常
  回抽扫掉。`give_back_r_mean ≈ 1.12R` 是「目标常到不了 + 结构防线宽度」的结构性
  让利,不是时钟问题。


