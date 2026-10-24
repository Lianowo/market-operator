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

## 5. 第四阶段（v21.6 / v21.7）：结构止损 + REJECT 对侧突破锁利（回吐预算）+ 独立容量池与阶梯动态复利

1. **分类初始防线与目标锚定（`V6Params`）**：
   - `reject_use_struct_stop=True`：REJECT 使用箱体外缘结构止损（拒绝刺穿极值外侧，天然窄止损、高盈亏比），`reject_target_mode="opposite"` 将 `target_1` 锚定在箱体对侧缘 `box_opposite`。
   - `accept_use_struct_stop=False`：ACCEPT 保持 2.0·ATR Turtle 宽止损与无上限移动防线。
2. **REJECT 对侧突破不减仓、直接锁利（v21.7 重写：`reject_opp_lock_frac = -0.25`）**：
   - **语义是回吐预算，不是绝对价位**：`budget = (1 − |frac|)·|target_1 − entry|`（建仓即冻结，价格单位），`stop = peak ∓ budget`（空头 `+`、多头 `−`），`peak` = 入场以来最有利极值（`Position._best`，单调不回落）。`frac = -0.25` ⇒ 预算 = ¾ 行程 =「锁 ¼、让 ¾」（生产值，标定过程见 §5.1）；`frac = -0.50` ⇒ 预算 = ½ 行程 =「锁一半、让一半」。端点自洽：`frac → 0` 不锁（预算 = 整段行程）；`frac → 1` 全锁（预算 = 0，触价即平）。
   - **arming 是价格事实，且单调**：`frac < 0` ⇒ **收盘突破**对侧缘（acceptance，生产口径）；`frac > 0` ⇒ **盘中触及**（touch）。一经 arming 即保持，此后每根 bar 重新推演 ⇒ 棘轮可**多次**推进（不是一次性置位）。`Position.armed` 是独立状态，**不复用** `reduced`（后者只表示「减仓额度已用」）。
   - **三态穷尽**：① 未 arming ⇒ 保持结构防线（不预设任何锁利价）；② `stop` 在现价保护侧 ⇒ 推进（只紧不松）；③ `stop` 已被现价越过 ⇒ **立即市价离场**（`v21|lock_giveback_exhausted`）。不存在「闸门拒单后原地等待」的第四态，也就不存在裸奔窗口。
   - **唯一出口**：`position.opp_lock_step(...)`（纯函数，只读价格事实）。`PositionManager.on_bar`（认知 bar 与 `price_only` 执行细 bar）与 `live_runner._on_sub_bar`（子周期早锁）共用同一函数，禁止各自重算。关键恒等式：`stop_protect_dist(d, stop, ref) ≡ budget − d·(peak − ref)`（保护侧距离 ≡ 预算 − 自峰值回撤）⇒ 「落在保护侧」与「回吐未超预算」同真同假，**合法价位由构造保证，不靠闸门**。因此 `lock_is_protective` / `lock_is_profitable` 不再是执行路径上的判据 —— 二者与 `opp_lock_px` 已于 2026-10-24 从 `position.py` 删除（性质改由测试用 `stop_protect_dist(...) > 0` 直接断言，`opp_lock_px` 的旧口径只留在测试里作对照量），`opp_lock_live_px` / `tighten_lock` 同步删除。
   - **为什么删掉 `opp_lock_max_giveback_r`（2026-10-24）**：它是第二把尺子（R 单位），与 `|frac|`（行程比例）量纲不同却「取更紧者」，互相遮盖 —— `0.25R` 在赢单 MFE 中位 2.07R / p90 5.71R 的分布里约等于只允许回吐 ~12% 行程，必被噪声扫（`reports/opp_lock_ab.md`：0.25R 在 dev/test 净利 −12.83 / −7.38，仅 1.00R 转正）。更根本的是「更紧」≠「更安全」：它把止损推到来不及保护的位置。现在锁利只有 `reject_opp_lock_frac` 一个参数，回放/实盘同一口径、同一函数。
   - **GOOGLUSDT 前后对照**（2026-10-23 实盘，空头 entry 324.02 / target 322.66 / 2h 收盘 321.33 / 当根最低 320.93，span = 1.36、初始 R = 2.86）：旧绝对价位 323.34 落在现价**身后 2.01**（不回踩等于没锁；回踩触发时已把 1.08R 行程回吐到 0.24R，实际成交 324.01）；新口径 `frac=-0.50` ⇒ `320.93 + ½·1.36 = 321.61`，距现价 0.28、锁住 **0.84R**，触发时回吐恰为 `0.68 = ½span`；生产值 `frac=-0.25` ⇒ `320.93 + ¾·1.36 = 321.95`，锁住 **0.72R**、只允许回吐 1.02（¾span）—— 同一价位口径，锁多少由 `frac` 一个旋钮决定。
   - **覆盖缺口**：早期 `_diag_v21_backtest.py` 未设置 `reject_target_mode`，其 M3c 回放里 `target_1` 恒为 `None` ⇒ §8.2 锁利在历史回测中**从未被触发**（实盘缺陷因此逃过回测）。生产口径现统一由 `scenario.py` 按 per-TF `reject_target_mode="opposite"` 装配。

   - **通知图 ENTRY 口径**：`tg.send_fill(..., entry_price=<建仓均价>, price_label=<本次事件价名称>)`。修前图上一律用"本次事件价"画 ENTRY 线，止损推进/平仓通知会把 ENTRY 画在事件价上（例如用 2h 收盘 321.33 顶替建仓价 324.02），图上止损/目标相对 ENTRY 的关系整体错位，读图会把"空头锁利"误读成"止损跑到止盈侧"。
   - **保护性止损全天候生效**：修复 `broker.py` 中同向挂单覆盖保护性止损的边界缺陷，确保从成交下一根细 bar 起 `PROTECTIVE_STOP` 100% 在场。
3. **各周期独立风险容量池（`tf_cap_by_tf`）**：
   - `campaign.py` 与 `live_runner.py` 支持按周期独立分配风险上限（回测基线每 TF `2.25%` / 总 `12%`；小资金实盘复利每 TF `12%` / 总 `50%`），彻底解决 2h/4h 持仓占满容量导致 12h/1d 高胜率大单被 `cap_skip` 挤出的问题。
4. **小资金阶梯动态复利（`small_account_compounding` / `config/live.json`）**：
   - 104 USDT 起步 × 20x 杠杆上限；
   - 按账户实时净值阶梯调整单笔风险：`<=250U: 4%`、`<=600U: 3%`、`<=1500U: 2%`、`>1500U: 1%`。
5. **实盘多周期并发、信号闸门解耦与双通道黑白 K 线结构通知（`live_runner.py` + `tg.py`）**：
   - `LiveRunner` 在 `engine="v21"` 下自动为每个品种装配 `2h`、`4h`、`12h(->2h)`、`1d(->4h)` 四个独立战役实例。
   - **信号广播与容量闸门解耦**：`_submit_open` 先结算结构事实（区间/箱体/风险档/周期）与容量/风控闸门，无论是否被 `TF_CAP` / `PORTFOLIO_CAP` / `MIN_NOTIONAL` / 熔断挡下都推送信号通知，并以 `exec_status` 明确说明本系统处置（如 `仅参考 · 12h 周期容量已满,本系统本拍未开仓` 或 `本系统已同向开仓(市价)`）；同事件按 `(symbol, tf, event, ts, direction, route)` 去重，通知异常记入 `signal_notice_errors` 且绝不打断决策链。
   - **`TelegramNotifier` 信号/运营双通道分离（`_KIND_CHANNEL`）**：`signal` 走公开信号频道（不含账户权益、持仓量或下单回执，可公开订阅），`fill`（开仓/减仓/锁利与防线推进/平仓）、`daily`、`alert` 及未知类型一律走运营通道（`TELEGRAM_TRADE_*` 未单独配置时回退信号 Bot 并如实报告 `ops_channel_separate=False`）。
   - **纯 Python 高清黑白 K 线结构图（`render_bw_kline_png`）**：标注平衡箱体（浅灰带 + 粗虚线上下沿）、事件区间（斜线填充 + 细虚线 + 触边次数）、`±R` 标尺、认知帧结构端点 `PH/PL`（白底衬 + 右沿自动左翻），右侧标签栏按 12px 行距碰撞避让，纵轴按间距统一小数位精度。

### 5.1 锁利预算 `frac` 的标定（2026-10-24）

`frac` 此前是**语义直觉值**（"锁一半、让一半"），从未标定。回吐预算改完后它成了唯一旋钮，因此用与验收工具**同源**的口径（`ScenarioRunner` + `summarize`：explore / book4 × dev/test/full × 1x 成本 × 生产配置）扫了一遍；`Σ` 为 6 段净利相加，300 USDT 固定基准：

| `frac` | 预算 | Σnet | dev | test | full | 最差段 |
|---|---:|---:|---:|---:|---:|---:|
| `0.00`（不锁利，走 §8.2 减仓 50%） | 整段行程 | **−241.55** | −153.62 | +79.82 | −167.75 | −130.27 |
| **`-0.25`（生产）** | ¾ span | **+593.65** | +165.50 | +130.74 | +297.41 | **+33.42** |
| `-0.35` | 0.65 span | +314.74 | +112.71 | +56.11 | +145.92 | −4.05 |
| `-0.50` | ½ span | +416.93 | +208.69 | +17.45 | +190.79 | −15.19 |
| `-0.65` | 0.35 span | +478.07 | +221.33 | +10.06 | +246.68 | −27.92 |
| `-0.80` | 0.20 span | +167.65 | +77.22 | +16.03 | +74.40 | −11.56 |
| `-1.00`（触价即平） | 0 | +133.68 | +66.05 | +10.28 | +57.35 | −11.34 |

怎么读这张表（`reports/lock_frac_sweep.md` 有逐场景明细与 2x 成本复核；旧 HEAD 的对照见 `reports/lock_ab_head.txt`，新语义生产值见 `reports/lock_ab_v217_frac025.txt`）：

1. **锁利机制本身是正贡献**：`frac=0`（等于回到"触及减仓 50%"）是 −241.55，任一预算都比它好 **+375 ~ +835**。这条与参数无关，是机制的功劳。
2. **响应面不平滑，所以不取最高点、取最大最小**：只动 `frac` 的 7 个臂里，`-0.25` 是**唯一 6 段全为正**的（最差段 +33.42），而相邻 `-0.35` 反而塌到 +314.74 —— 相邻两点跳 279，说明单点差异大半是容量路径噪声，不能当"最优点"抄。故按 maximin 选 `-0.25`（唯一能与之并列的是 `frac=-0.50 + exit_exec`，最差段 +16.27、`Σ` 只有 +223.89，见下文粒度开关表）。
3. **test 段是照妖镜**：`-0.50` 的 dev 更好（+208.69 vs +165.50）但 test 只有 +17.45，`-0.25` 的 test 是 +130.74。dev 单点最优（`-0.65`）在 test 只剩 +10.06 —— 典型的 dev 过拟合形状，不采用。
4. **成本加倍后排序不变但全线转负**（`-0.25`/`-0.50` 分别 −1148 / −1184）：这是策略整体的成本敏感度，不是锁利的锅，但说明这条参数不值得为几个点去精调。
5. **回撤不是单调的**：`-0.25` 在 dev 段 DD 略高（24.0% vs 21.9%），在 book4|full 反而更低（28.9% vs 34.6%）。协议不把回撤当否决量，但记录在此。

> 取舍说明：`frac` 只决定"到目标后允许让出多少行程"，与"价位必须是行情事实"无关；后者由回吐预算语义保证。若更看重落袋确定性，`-0.50`（锁一半）仍是合法选择 —— 代价是 test 段少 ~113 USDT（6 段合计 −177）。改这一个数是一行配置，可随时回退。

**粒度开关在同一口径下的重测（v21.6 的结论已过期）**：

| 开关 | Σnet | 最差段 | 结论 |
|---|---:|---:|---|
| 生产(`exit_exec=False`，`frac=-0.25`) | +593.65 | +33.42 | 默认 |
| `frac=-0.25 + exit_exec=True` | +536.86 | +31.24 | 提前收网只花 −57（~10%）换来更低回撤（explore\|dev 22.7% vs 24.0%），收益侧无溢价 ⇒ 保持关闭 |
| `frac=-0.50 + exit_exec=True` | +223.89 | +16.27 | 在更紧的预算下提前收网会腰斩净利（vs +416.93） |
| `pivot_exec=True` | 未跑成 | — | **无效开关**：`exec_pivots` 只由 `scripts/_diag_v21_scale.py` 装配，`scenario.build_frames`（验收与生产同源链）不装配 ⇒ 此前它在这个口径下逐位等于关闭（v21.6 的 pivot_exec 结论来自手工装配的脚本）。现已在 `run_campaigns` 里**直接报错**，把"没接上"与"没用"分开 |

因此 `exit_exec` 保持默认关（它买的是回撤、不是收益，且收益侧无补偿）；`pivot_exec` 在补齐细周期 pivot 装配前不可用。

## 6. 回测验证结果（BTC / ETH / SOL / DOGE，2024-10 → 2026-10）

| 模式 / 配置 | dev @ 1.0x | dev @ 1.5x | test @ 1.0x | test @ 1.5x | full @ 1.0x |
|---|---|---|---|---|---|
| v21 M3c 统一架构（300U 固定基准） | +360.8 USDT (PF 1.48) | +321.4 USDT (PF 1.41) | +214.5 USDT (PF 1.59) | +193.2 USDT (PF 1.51) | **+575.3 USDT (gR +0.351, WR 52.8%)** |
| v21 小资金阶梯复利（104U / 20x） | — | — | — | — | **104U → 2,523.7 USDT (+2326%, MaxDD 15.8%)** |

- 四周期（`2h` / `4h` / `12h` / `1d`）在 `dev` 与 `test` 段、1.0x/1.5x/2.0x 成本压力下**全部独立正期望**。

> 上表是 v21.x 早期 `_diag_v21_backtest.py` 口径(300U 固定基准、旧锁利),留作历史。
> v21.7 之后的**验收口径**水位见 §5.1 与 `docs/acceptance.md`:回放基线已于
> 2026-10-24 按新语义显式重冻(`tests/fixtures/replay_baseline.json`,六场景合计
> **+593.6514**),闸门 `replay_acceptance` 退出码 0。两组数字口径不同,不可互相换算,
> 也不能用任一组的绝对值判断参数好坏。

## 7. 可复现命令

```bash
python scripts/_diag_zones.py              # v21.1 单带区域数量/间距/年龄对照
python scripts/_diag_balance.py            # v21.2 Pivot 平衡箱对照
python scripts/_diag_balance_detect.py     # v21.3 六维框子识别器诊断
python scripts/_diag_v21_backtest.py       # v21 M3c 多周期回测与成本压力验证
python scripts/live_paper_check.py         # v21 实盘多周期装配与真网行情 paper 冒烟校验
python scripts/_diag_v21_scale.py --segments dev,test --funding const,history --xexec
                                           # v21.6 尺度级联 + 出场推进粒度判定(4 标的,~6min)
python scripts/_diag_lock_frac_sweep.py    # v21.7 锁利回吐预算 frac 扫描 + 粒度开关 + 2x 成本(~9min)
python tools/replay_acceptance.py --quiet-prep   # 验收闸门(Δ 必须全为 +0.0000,退出码 0)
python -m pytest -q                        # 580 passed, 1 skipped（全量回归 + v21 专项测试）
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
  只做保护面 —— 核对交易所**原生仓位级 TPSL**（`pos_loss`）、缺失则按本地防线补挂，
  不下开仓单、不改策略、不改仓位。
- **锁利唯一裁决 `opp_lock_step`**：回放与实盘共用；三态（hold / tighten / exit）由
  `budget = (1−|frac|)·span` 与 `peak` 唯一决定，不再有「先过两道闸门再取更紧者」的
  候选合并（保护侧性质 `stop_protect_dist(...) > 0` 由构造保证，测试直接断言；见 §5）。

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



## 10. 第六阶段（v21.6 续）：尺度横截面与 pivot 跨尺度确认

判定报告同 `reports/scale_20261024/REPORT.md`（§3b / §4b），原始读数 `scale.json`、
`pivot_align.json`、`table.md`。

### 10.1 尺度横截面（同一装配，solo，无触发）

E[R]（const / history）：

| 尺度 | dev | test | DD%(dev/test) | N(dev/test) |
|---|---|---|---|---|
| 1h | −0.0402 / −0.0420 | −0.0467 / −0.0350 | 36.4 / 34.2 | 897 / 419 |
| 2h | +0.0242 / +0.0306 | −0.1266 / −0.1173 | 17.0 / 23.3 | 438 / 230 |
| **4h** | **+0.0570 / +0.0664** | **+0.2252 / +0.2367** | **7.4 / 7.1** | 229 / 115 |
| 12h | +0.0150 / +0.0390 | +0.3491 / +0.4184 | 15.6 / 5.1 | 276 / 100 |
| 1d | −0.0041 / +0.0059 | −0.2145 / −0.1803 | 5.2 / 10.3 | 88 / 52 |

- 结论：**倒 U，不是「越小越差」**。4h 是唯一两段 × 两口径全正且 DD 个位数的尺度；
  12h 也全正但只有 100 笔 test、give-back 1.32R，只能支撑「不亏」；1h/1d 在 test 为负。
- `4h` 触发 `2h`：dev +0.0339/+0.0450，test +0.1524/+0.1670 ⇒ 触发层在 4h 上同样是
  **纯成本**（E[R] −0.023 dev / −0.073 test），与 §9 在 2h 上的否决同向。
- 该读数已消耗 test（§5.3）：4h/12h 的正读数不再算 OOS。

### 10.2 pivot 跨尺度确认（`ArmConfig.pivot_exec`，默认 `False`）

前置检验 `scripts/_diag_v21_pivot_align.py`（只读，不跑回放）：

| 对（细:粗） | 覆盖 | 细独有 / 结构下限 = noise_excess | 提前≥1 细 bar | \|Δ价\|/ATR p90 | 判定 |
|---|---|---|---|---|---|
| 1h:2h | 0.968 | 0.517 / 0.500 = **1.032** | 0.572 | 0.069 | PASS |
| 2h:4h | 0.959 | 0.528 / 0.508 = **1.040** | 0.580 | 0.083 | PASS |
| 4h:12h | 0.977 | 0.676 / 0.668 = **1.011** | 0.731 | 0.105 | PASS |

同族成立：细尺度几乎不多出 pivot 家族，只是密度按 2:1 / 3:1 抬高；价位中位偏差 0。

语义（与 `exit_exec` 正交，缺省逐位旧行为）：

- `CampaignFrame.exec_pivots`：执行细 bar 标签 → 更细周期在**该 bar 收盘**确认的 pivot。
  装配侧按墙钟映射（`标签 = 细确认时刻 − ms_exec`），且**跳过认知收盘标签** —— 那一刻
  粗尺度自家 pivot 由 ⑤ 正常喂入，重复喂等于用两个锚点收同一段结构。
- `ArmConfig.pivot_exec=True`：④.5 的价格型 tick 把这些 pivot 作为 `new_pivots` 喂给
  `position.on_bar(price_only=True)`；`bars_held` / `consec_adverse` / 事件流 / 路由 /
  准入 / 开仓一律不动。不开开关时 `exec_pivots` 不被读取。

读数（Δ vs 同臂同段同资金费，12 组）：give-back **一致下降** 0.13–0.39R（trail 这次
真的动了，`pivot_exec_ticks` 1000+/445+/622+/1444+），但笔数一律上升 ⇒ 只有 2h 的 3 组
小赚（≤ +0.0245R），4h 四组全负（−0.019 ~ −0.071R），12h 用 4h pivot 时 −0.065 ~ −0.365R。

判定：**机制证实、收益否决，保持默认关**。与 §9.2 合起来给出同一结论 ——
`give_back_r_mean ≈ 1.1–1.3R` 是「目标常到不了 + 防线宽度」的结构性让利，不是决策
时钟问题；所有「更早收网」的开关都同向亏钱。下一轮该动的是**目标/防线口径**。

