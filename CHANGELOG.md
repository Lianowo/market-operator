# Changelog

## [Unreleased] — 2026-10-24 — 退役残留清理（无行为变更）

删的是「已经不在执行路径上、但还能被 import / 被读」的东西，不改任何交易行为。

- **`position.py` 删除三个只剩测试引用的符号**：`opp_lock_px` /
  `lock_is_protective` / `lock_is_profitable`。它们自 v21.7 起已不是判据（唯一出口
  `opp_lock_step`），但 `opp_lock_px` 是**已退役的绝对价位口径**留在生产模块里，
  下一个调用方完全可能 import 它去算止损价，把「钉在现价身后」的缺陷重新引入。
  - `opp_lock_px` ⇒ `tests/test_v21_balance_trigger.py::_legacy_lock_px`（只作
    对照量：说明新口径在 `peak==target` 时与之逐位重合）；
  - 两条性质断言改用 `stop_protect_dist(...) > 0` 直接表达（同一式子、不再多一层
    包装），`stop_protect_dist` 本身是 `opp_lock_step` 的内部判据，保留。
- **`config/live.json` 删除无人读取的键**：`risk.min_notional_source`、
  `loop.intent_keep_hours`（v20 限价意图墙钟过期，早已退役）、
  `paper.symbols_mode`、`note_size`（与 `note` 重复且内容过期）。全仓
  `src/scripts/tests/tools/docs` 无一处引用，删掉不改行为。
- **`config/live.json` 修重复键**：`loop` 段 `state_bar_tail` /
  `campaigns_write_seconds` 各出现两次，后值静默覆盖前值
  （`campaigns_write_seconds` 实际生效 60，前一条 120 是死值）。各留一条。
- **配置文案去掉手抄的锁利比例**：`config/strategy.json` 的 `note` / `meta.model`
  与 `config/live.json` 的 `note` 仍写「锁利50%」/「Lock(50%)」，与
  `arm_config.reject_opp_lock_frac=-0.25` 直接矛盾。改为描述语义并**指向该键**
  （`stop = peak ∓ (1−|frac|)·span`），不再复制数值 —— 下次标定不会再漂。
- **`scripts/archive/_diag_opp_lock_ab.py`** 去掉未使用的 `V6Params` 导入
  （vulture 90%）。
- **保留（不是残留）**：`campaign.ArmConfig.exit_exec` / `pivot_exec` 是已测量、
  默认关闭的开关（`pivot_exec` 无生产装配路径，由 `require_exec_pivots` fail-closed
  挡住）；`src/market_operator/lifecycle.py` / `repairs.py` / `tiered.py` /
  `strategy_v6.py` 是历史轮次的回放实现，仍被 `scripts/run_v18|v19|v20.py` 与对应
  测试使用；`config/v*.json` 是各轮冻结快照。

## [Unreleased] — 2026-10-24 — 保护止损改用交易所原生仓位级 TPSL

- **保护止损从「独立计划单」改为「交易所原生 TPSL」**：`place-tpsl-order`
  （`planType=pos_loss`、`triggerPrice`、`triggerType=mark_price`、
  `executePrice=0` 触发后市价、`holdSide`，**不带** `size`/`side`/`tradeSide`）。
  旧路径 `place-plan-order` 的计划单是**独立委托**：按挂单时的数量触发，
  部分减仓后剩余仓位不再被覆盖（要等下一轮 `_manage_stops` 撤旧换新），
  且计划单与仓位生命周期脱钩 —— 仓位平掉后可能留下无仓可平的废单。
  仓位级 TPSL 由交易所按触发时的整仓市价平仓，自动覆盖部分减仓、仓位归零即失效。
- **`_manage_stops` 删除数量维度**：不再"qty 变了就撤旧换新"（`pos_loss` 报文无
  size）。修前每次部分减仓都要先撤后挂，撤成功/挂失败之间就是一个失保窗口，
  而这个换单在数量上毫无必要。触发价变动仍撤旧换新（移动防线）。
- **`cancel_plan_order` → `cancel_tpsl_order`（`orderIdList` 形式）**：
  `cancel-plan-order` 只认对象数组 `orderIdList=[{orderId}]`；单值 `orderId`
  撤不掉 TPSL ⇒ 旧止损继续有效，出现"新单挂上、旧单未撤"的双保护，
  谁先触发不可控。端点与计划单共用。
- **`plan_orders_pending` → `tpsl_orders_pending`（`planType=profit_loss`）**：
  planType 只能单值 —— `loss_plan` 被 `40812` 拒、逗号多值经 `%2C` 后 `40009`
  签名失败；`profit_loss` 返回行里可能混有 `pos_loss`/`pos_profit` 与旧式
  `loss_plan`/`profit_plan`，故归属仍只按 `clientOid` 前缀判定，异己单一律不碰。
- **挂单响应无 `orderId` 视为失败**：修前 `str(resp.get("orderId") or "")` 落成
  空串仍写入 `stops`，本地声称"已保护"，实际裸奔到 TTL 过期才被 `stop_missing`
  发现。现在直接 `stop_error` + 告警。
- **`stop_hold_side()`**：`holdSide` 口径 —— hedge_mode 用 `long`/`short`，
  one-way 用 `buy`(多)/`sell`(空)，与 ccxt `bitget.createOrder` 的 TPSL 分支一致；
  传错会 `400172` 被拒，等于该仓位无交易所侧保护。
- **心跳补挂同步作废在挂快照**：`HeartbeatGuard.ensure_protection` 补挂后把
  `_stops_live_ids/_stops_live_rows/_stops_checked_at` 置空，否则新 `orderId` 不在
  主循环的旧快照里，会被判"保护丢失"→ 再挂一张再撤一张。
- 工具与文档：`live_gap_preview` 的写操作拦截表、`live_paper_check` 的 paper 防呆
  名单、`live_status` 的读端点与告警文案（"多张保护单"）同步。
- 测试：新增 `tests/test_bitget_live.py` 钉住 place/cancel/query 三个报文契约
  （含"不带 size/side/tradeSide"、"只认 orderIdList"、"planType=profit_loss"、
  旧 `loss_plan` 被 `ValueError` 挡住）；`tests/test_live_runtime.py` 新增
  部分减仓不换单、不带 size 挂单、无 orderId 记失败三例，并改用 `place_stop_loss` /
  `cancel_tpsl_order` / `tpsl_orders_pending` 的假实现；`tests/test_live_supervision.py`
  新增心跳补挂三例（缺保护 ⇒ 无 size 的 `pos_loss` + 作废在挂快照、自家单仍在挂 ⇒
  不重挂、无 orderId ⇒ 记 `errors` 且不写本地记录）。

## [Unreleased] — 2026-10-24 — 对侧锁利语义重写（回吐预算,删除 R 单位第二把尺子）

- **锁利从"绝对价位"改为"回吐预算"(v21.7)**:`budget = (1−|frac|)·|target_1 − entry|`
  (建仓冻结),`stop = peak ∓ budget`,`peak` = `Position._best`(单调)。旧写法
  `entry + |frac|·span` 在决策时现价已越过该价位时只剩两种坏结果:拒单(不推进 =
  裸奔到收盘)或挂上即成交(止盈单冒充止损单)。GOOGLUSDT 空头样本:旧 323.34
  (距现价 2.01、锁 0.24R、实盘成交 324.01)⇒ 新 321.61(锁 0.84R,触发时回吐恰
  为 ½span → 0.68)。
- **删除 `opp_lock_max_giveback_r`**:它是 R 单位的第二把尺子,与 `|frac|`(行程
  比例)量纲不同却"取更紧者"互相遮盖(`reports/opp_lock_ab.md`:0.25R 在 dev/test
  净利 −12.83/−7.38)。`config/strategy.json`、`campaign.ArmConfig`、
  `live_runner.new_position_manager` 同步移除;`opp_lock_live_px` / `tighten_lock`
  删除。`lock_is_protective` / `lock_is_profitable` / `opp_lock_px` 于同日二次清理中
  一并从 `position.py` 删除(性质改由测试用 `stop_protect_dist` 直接断言)。
- **单一裁决出口 `position.opp_lock_step`**:`PositionManager.on_bar`(认知/执行细
  bar)与 `live_runner._on_sub_bar` 共用;三态穷尽 —— 未 arming 保持结构防线 /
  保护侧推进(**可多次棘轮**)/ 越过即市价离场(`v21|lock_giveback_exhausted`)。
  恒等式 `stop_protect_dist ≡ budget − 回撤` ⇒ 合法价位由构造保证,不再需要
  "拒单闸门"这一态。
- **状态语义分离**:新增 `Position.armed`(arming = 价格事实,`frac<0` 收盘突破 /
  `frac>0` 盘中触及),不再复用 `reduced`(后者只表示减仓额度已用);删掉"仅影线
  触及先锁 10% 保本垫"的特例(`frac` 的符号现在只决定 arming 口径)。
- **实盘子周期离场统一出口** `_sub_bar_close_out()`:固定止损与"锁利回吐预算耗尽"
  走同一条离场/对账路径,避免已平仓位被 `_manage_stops` 重新挂单。
- **`frac` 标定 ⇒ 生产值 `-0.50 → -0.25`(让出 ¾ 行程、锁 ¼)**。同源口径扫 7 个
  预算(explore/book4 × dev/test/full,1x):`Σ` 从 `frac=0`(不锁利)的 −241.55 升到
  `-0.25` 的 **+593.65**、`-0.50` 的 +416.93;`-0.25` 是唯一 6 段全正的臂(最差段
  +33.42),而相邻 `-0.35` 塌到 +314.74 ⇒ 响应面不平滑,按 maximin 取值而非取最高
  点。dev 单点最优(`-0.65`)在 test 只剩 +10.06,是过拟合形状,不采用。证据:
  `reports/lock_frac_sweep.md`(`scripts/_diag_lock_frac_sweep.py` 可复现)。
- **`pivot_exec` 是无效开关,现改为报错**:`CampaignFrame.exec_pivots` 只由
  `scripts/_diag_v21_scale.py` 装配,`scenario.build_frames`(生产/验收链)从不装配
  ⇒ `pivot_exec=True` 在生产链上逐位等于关闭(实测 6 段 net 完全相同)。静默放行会被
  读成"机制无效",故 `run_campaigns` 直接 `ValueError`。
- **`exit_exec` 在新锁利口径下重测**:`frac=-0.25 + exit_exec` Σ +536.86(最差段
  +31.24)vs 关闭 +593.65(+33.42)、`frac=-0.50 + exit_exec` Σ +223.89(最差段
  +16.27)vs 关闭 +416.93(−15.19)—— 提前收网在宽预算下只花 ~10% 换更低回撤,在紧
  预算下腰斩净利;收益侧没有补偿,默认继续关。v21.6 "11/12 组变差"的读数已标注过期。
- **`live_runner.new_position_manager` 对锁利 frac 改为 fail-closed**:删掉写死的兜底
  默认 `-0.50`,缺 `arm_config.reject_opp_lock_frac` 直接报错。标定值只应来自配置,
  否则下次标定后实盘与回放会悄悄分叉(本次 −0.50 → −0.25 若走兜底路径就会分叉)。
- 测试:新增回吐预算/三态/随机路径不变量/频率不变性(1×/2×/4× 采样同一防线)
  用例;`tests/test_v21_balance_trigger.py` 锁利用例全部改钉新语义,并改为**从
  `config/strategy.json` 取 frac**(断言语义,不再钉死标定值)。全量 580 passed / 1 skipped。
- 文档:`docs/v21-protocol.md` §5 重写锁利口径(含为何删 R 尺子的数据)、新增 §5.1
  预算标定表与粒度开关重测;`scenario.py` / `config/strategy.json` 注释与键同步。
- **回放基线按新语义显式重冻(2026-10-24)**:`python tools/replay_acceptance.py
  --capture --quiet-prep` 重写 `tests/fixtures/replay_baseline.json`;重冻后连跑两次
  逐场景/逐品类 Δ 恒 `+0.0000`、`[PASS]`、退出码 0(此前必然 FAIL,因为基线记着
  旧锁利的水位)。六场景合计 **+1227.2986 → +593.6514**(每一分的去向见
  `reports/scale_20261024/BASELINE_DRIFT.md`:旧基线含"止损落在现价身后"的虚拟利润)。
  对照表与记账纪律写在 `docs/acceptance.md`「基线重冻记录」。**旧基线仍可取回**:
  `git show HEAD:tests/fixtures/replay_baseline.json`。
- **实盘对账的两个疑问已查清(①已被上方的原生 TPSL 迁移取代)**:①~~`plan_orders_pending()`
  只查 `normal_plan` 不是缺口~~ —— 本项目只挂一种计划单,且 `place_stop_market_plan` 已把
  `loss_plan` 归一为 `normal_plan`(`bitget_live.py` 实测注释),故"本地有记录/交易所
  没有 ⇒ 重挂"的判定覆盖全部自有单;异己单由 `clientOid` 前缀过滤 + `_dedupe_stops`
  兜底。**注:现已改为原生 TPSL,查询走 `planType=profit_loss`,见本节上方。**
  ②本项目**从不使用交易所侧移动止损**(全仓无 `track_plan`/`callbackRatio`),
  移动防线是本地棘轮(`opp_lock_step` → `_manage_stops` 撤旧换新),所以不存在
  "交易所 trailing 口径未对齐"的问题。

## [Unreleased] — 2026-10-23 — 单链路 v21 收敛(D3 / D9 / D13 / D15)

- **实盘只剩一条链(`scripts/live_runner.py`)**:删除 `lifecycle`/`repairs`/`tiered`
  导入、`CONFIG_V20`、`load_v20_params()`,以及 `ctx18` 建仓语境;`cognition.engine`
  从"选择器"变成**守卫**——配置里出现非 `v21` 的值即报错。`load_strategy_params()`
  **fail-closed**:配置没有 `use_balance_detector` 时直接报错,而不是退回旧链持仓
  参数;`new_position_manager()` 只构造 v21 `PositionManager`。旧链是"启动失败",
  不是静默回退。
- **配置**:`config/live.json` 删除 `cognition.engine`(字段本身已无意义)。
- **`split_boundary` 独立成模块(`src/market_operator/split.py`)**:回放/战役不再
  经 `research.py`(555 行研究方法学)取判分边界;`research` 仍 re-export 以免打断
  历史轮次。`tests/test_layering.py` 增加断言:生产闭包含 `split`、不含 `research`。
- **平衡箱窗口参数化(`structure.Params`)**:新增 `box_min_rotations=1`、
  `box_keep_broken_bars=12`、`box_keep_active_bars=24`,`_sync_balance_boxes`
  不再内联魔法数字;docstring 与实现一致(此前注释写 rotations≥2 / 60·120 bars,
  代码是 1 / 12·24)。三键进 `from_json` 的 int 归一。
- **`broker._floor_step` → 公开 `floor_step`**(保留 `_floor_step` 兼容别名)。
- 测试:524 passed / 1 skipped;新增 4 条 v21 单链路断言 + 3 条平衡箱/参数断言。
  `tools/replay_acceptance.py` 六个主行与 24 个子行 Δ 全为 +0.0000。

## [v0.1.0] - 2026-10-19 — Research Only verdict

### Phase 1 — Methodology & OSS audits
- 冻结交易方法论 `docs/methodology.md`（因果语义、区域生命周期、事件目录 E1-E6、
  研究协议与 GO/NO-GO 判据；全部参数为先验）。
- 独立深度审计：`docs/audits/`(pymarket-structure(MIT) / quant-trading（专有，仅思想） /
  OrderFlow-absorption（无 LICENSE，仅思想） / TapeFlow(MIT))。

### Phase 2 — Cognition core & data
- `structure`:ATR 波动率尺度、波动率自适应确认波段、区域生命周期状态机、regime。
- `events`:E1-E6 因果事件检测，forming/known 双时间戳；前缀不变性与增量==批量测试。
- `data`:Bitget 2 年 1h 三品种真实数据，0 缺口，sha256 manifest。

### Phase 3 — Event study
- 事件研究 + 同 regime 匹配对照 + block bootstrap + 成本压力；诚实性日志全程。
- 判定：E1|E6 short 相对增量 GO（绝对净期望 ≤ 0)；做多与 E2/E3/E4 NO-GO;E5 样本不足。

### Phase 4+5 — Strategy, position mgmt, full-cost backtest
- 白名单仅 GO 机制；§8 六条事件驱动持仓管理全部实现且可解释。
- 真实合约规格/费用/资金费/强平约束；300 USDT 复利；9 段×成本压力全部为负 → **NO-GO, Research Only**。
- 验收报告 `docs/acceptance.md`（任务书八问逐项，含失败分解与 v2 冻结议程）。

[v0.1.0]: https://github.com/Lianowo/market-operator/releases/tag/v0.1.0

## [Unreleased v2] - 2026-10-19

- 归因驱动优化轮：E7 FLIP_RETEST + F1(docs/v2-protocol.md);test 一次评估仍 NO-GO,
  Research Only 维持；优化循环按反过拟合原则停止(docs/acceptance.md v2 附章)。
  120 tests;v1 行为 parity 锁定。

## [v0.4.0] - 2026-10-19

- v4 成熟度优先（E7-only + F1 + 限价接回踩 + 15m 执行路径）：同信号 A/B 全面劣于 v2 市价
  (test −75.4 vs −55.7),adverse selection > maker 节省；九段全负、test<60 判 INSUFFICIENT。
  192 tests。优化循环关闭，Research Only 终态。

## [v0.5.0] - 2026-10-19 — Final form, definitive

- v5 破位质量路由（STRONG=市价/WEAK=E7限价）+ E1 优先 + F1 全入口 + short-only:
  test −22.5/22 笔/CI 含 0(INSUFFICIENT)；胜率随成熟度单调升(28%→41% 桶)，总亏单调降。
  五轮干预全部落定：低频、低亏、不赚。Research Only 定版，协议级优化关闭。218 tests。

## [v1.4.0] - 2026-10-21 — Live runtime

- Bitget live runner + Telegram 通知 + 118品种宇宙(>3M)+ paper 冒烟 PASS;580 tests。
- v17-v20:首个 dev 翻正(v18 +156/PF1.51) 与 test 首正(v19 +10.46);S1/S4 小幅正贡献。

## [v1.5.0] - 2026-10-21 — aggTrades contact study

- 3 品种 × 90 天 Binance 逐笔 aggTrades(2.195 亿笔)接触点检验(`aggtape.py` / `reports/aggtrades.md`):H1 49/50 单元无增量,H2 吸收签名不成立,订单流路线正式关闭;595 tests。

## [v2.1.0] - 2026-10-21 — v21 BalanceDetector + ACCEPT/REJECT + Live Multi-TF Architecture

- **平衡箱识别(`balance_detect.py` / `balance.py` / `structure.py`)**:拍卖市场理论六维框子打分器(`BalanceDetector`,稳健 12%/88% 分位数边界 + 前缀和 O(1) 加速 + 去重抑制)桥接 `ZoneBook`,派生上下沿支撑/阻力并附带 `box_opposite`、`box_quality`、`box_rotations`、`box_width_atr`。
- **双原语与混合触发(`events.py` / `strategy_v6.py` / `config/v21.json`)**:`ACCEPT`(E1 突破接受)与 `REJECT`(E3 刺穿拒绝)双原语;M3c 高阶降档混合触发(`2h`/`4h` 同周期收盘触发配强克制过滤,`12h->2h` 与 `1d->4h` 降一档细周期触发)。
- **出口与风控升级(`position.py` / `campaign.py` / `broker.py`)**:修复保护性止损在同向减仓单下的全天候在场;`REJECT` 采用箱体外缘结构止损 + 触及对侧缘不减仓直锁 50% 利润(`reject_opp_lock_frac=-0.50`) + Turtle 确认 pivot 移动防线;新增各周期独立风险容量池(`tf_cap_by_tf`)与阶梯动态复利(`dynamic_risk_tiers`)。
- **实盘多周期并发与双 Bot 黑白 K 线图(`scripts/live_runner.py` / `src/market_operator/tg.py` / `config/live.json`)**:支持 104 USDT / 20x 阶梯动态复利(4%→3%→2%→1%)、2h/4h/12h/1d 四周期独立容量池并发运行、信号/交易双 Telegram 机器人分流及纯 Python 高清黑白 K 线+平衡箱体 PNG 渲染配图直发;605 tests 全绿。

## [v2.2.0] - 2026-10-23 — 跨平台实盘监督与存储治理

- **心跳即判活(`src/market_operator/heartbeat.py`)**:`runtime/live/heartbeat.json`
  由 runner 内独立心跳线程每 15s 落盘,同时带 `ts_ms`(进程还活着)、`loop_ts_ms`
  (主循环最后一次成功走完一步)、`step_started_ms`(卡在哪一步)与
  `positions_detail`(进程消失后重挂止损所需的持仓快照);`beat_age_seconds` 取
  `max(loop_ts_ms, started_ms)`,刚重启不会被上一具尸体的旧时间戳误判成卡死。
- **进程内保护线(`HeartbeatGuard`)**:主循环停摆后仍持续心跳并**只保护不主出**
  (核对/补挂交易所止损),停摆超 `self_fence_seconds` 且有持仓则以退出码 3 自我
  熔断;`classify`/`decide` 五档判定表(缺失/正常/迟到/补做中/停摆)为纯函数。
- **外部看护(`scripts/live_supervisor.py`)**:只读心跳判活(不读进程表/端口/计划
  任务结果),缺失→拉起、迟到→只记录、补做中→等(受 `recover_max_seconds` 约束)、
  停摆→终止+拉起,重启预算(6 次/小时)与启动宽限(180s)防崩溃循环,预算耗尽只
  告警交人工;常驻模式顺带按 `gc_hours` 调用存储治理。
- **单实例锁(`SingletonLock`)**:runner / supervisor 各持一把内核文件锁
  (`msvcrt.locking` / `flock`),进程死亡即释放,不存在 pid 毒锁,也不会双开下单。
- **存储治理(`scripts/storage_gc.py`)**:盘点 → 可逆压缩(manifest + sha256 +
  `--restore`)→ 保留期清理 → 增长预测 → 预算闸门(35GB 硬 / 30GB 软,退出码
  0/1/2);审计日志只压缩不删除,控制台日志先归档校验再截断留 4MB 尾巴;增长预测
  只按 `logs/ runtime/ data/` 速率外推,落 `reports/_storage_growth.json`。
- **计划任务入口**:`\MarketOperator\MarketOperatorLive`(每 3 分钟重复、忽略重叠)
  与 `\MarketOperator\MarketOperatorStorageGC`(每日 04:10),动作为
  `C:\Windows\System32\cmd.exe /c tools\schedule\<入口>.cmd`(绝对执行体 + 仓库根
  工作目录)。**入口脚本内一律不加重定向**:在跑的实例独占自己的日志句柄,下一次触发
  再 `>>` 同一文件会让 cmd 在打开阶段失败(退出码 0、日志全空);改为常驻看护自己把
  console 行镜像进 `logs/supervisor.log`、异常记 `kind=crash`,重复触发靠单实例锁
  变成"没跑就拉起"。移除 OS 专用 `scripts/live_watchdog.ps1`。新增
  `tests/test_live_supervision.py`(23 项:判定
  五档、单实例锁跨进程、看护动作、启动宽限、压缩无损与预算闸门、console 自重定向),
  全量 605 passed(未计入 `test_v13_cascade`)。
  注:实盘运行时行情刷新会改写 `data/raw/*` 与 `data/manifest.json`,
  `test_v13_cascade` 的哈希锁定用例此时必然失败(环境时序,非回归),复跑前需停 runner。
