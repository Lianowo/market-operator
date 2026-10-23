# 精度重构设计 — v21 链路(2026-10-23)

> **状态**:设计稿。只描述,不动代码。
> **硬判据**:重构后 a0 parity 逐位不变(BTC/ETH/SOL `N=1153 / +281.0`;book4 `N=1440 / +380.1`)。
> **唯一例外**:§8-D1 若选择"统一容量公式",数字必然变化 —— 必须显式重冻结,并在报告里标注为**修正**而非退化。
> **依据**:本文件所有结论均由代码实测得出,附 `文件:行号`;不引用记忆、不做推测。

## §0 目标与非目标

**目标(按优先级)**

1. **语义唯一** —— 一个概念只有一个定义、一个实现、一个名字。
2. **引用显式** —— 隐式约定(字符串键 `SYM@TF`、布尔开关、事件别名)变成显式类型或参数。
3. **包袱落地** —— 不在 v21 路径上的实现离开 `src/`,但保持可复现(归档进 `research/rounds/`)。
4. **命名如实** —— 模块名/类型名描述语义,不描述版本号。

**非目标**

- 不新增策略假设;不动任何冻结先验数值(`config/*.json` 的数值一个都不改)。
- 不引入新依赖;不改 `data/`、`aggregate` 的产物契约。
- 不重写认知层算法(`structure`/`events` 的检测语义零改动)。
- 不做实盘功能扩展(引擎选择器只做 fail-closed,不加功能)。

## §1 结论摘要

1. `src/` 共 **11,512 行**:v21 生产链 **6,511**、实盘设施 **977**、历史轮次 **3,469**、研究方法 **555**。
2. 包袱主体**不是**孤立死模块(纯死代码约 **1,200 行**),而是**活模块里的版本钩子**:`campaign` 22 个参数、`position` 8 个 v16–v20 开关、`broker` 5 处限价提交、`structure` 4 个造区开关**全部无配置使用**。
3. 三处**语义精度缺陷**(不是风格问题)必须先修:容量三套公式、事件别名重复、文档与代码漂移(§3 D1–D3);另有 D16(配置键静默失效)同属此类。
4. 最大单块可退役:`broker` 的限价撮合路径(约 300 行)在 v21 配置下**不可达**;`position` 的 v16–v20 分支(约 200 行)同理。
5. 退役顺序必须是**先断引用、再移文件**。否则 parity 一旦变化,无法判断是"断引用"还是"移文件"造成的。

## §2 逐文件清单 — src(31 个)

### 2.1 v21 生产链(6,511 行,保留)

| 模块 | 行 | 职责 | 动作 |
|---|---:|---|---|
| `data.py` | 258 | 1h 原始数据加载 + manifest 校验 | 保留 |
| `aggregate.py` | 273 | 1h → TF 帧聚合(UTC 对齐) | 保留 |
| `structure.py` | 704 | ATR / pivot / zone / regime 认知 | **清理 4 个无配置的死开关**(D4) |
| `balance_detect.py` | 216 | 平衡箱识别(`BalanceDetector`,六维打分) | 保留,**改名**(D10) |
| `events.py` | 710 | E1–E10 事件检测 | 保留,**别名收敛**(D2) |
| `hypothesis.py` | 145 | `StructureView` → `Hypothesis` | 保留 |
| `strategy_v6.py` | 440 | 事件 → 路由决策(ACCEPT/REJECT) | **改名**(D11),常量收敛 |
| `position.py` | 706 | 持仓状态机(§8) | 保留,**裁 v16–v20 分支**(D7) |
| `broker.py` | 1095 | 账本 / 撮合 / 容量 / 费用 | 保留,**容量统一**(D1),限价路径隔离(D8) |
| `campaign.py` | 1031 | 多战役合并回放主循环 | 保留,**删版本钩子**(D6) |
| `trigger.py` | 749 | 触发层(原 v10 mic) | 保留,**术语统一**(D12) |
| `backtest_v6.py` | 400 | 认知缓存 / 回放结果 / 指标 | 保留 |

### 2.2 实盘设施(977 行,保留)

| 模块 | 行 | 职责 | 动作 |
|---|---:|---|---|
| `bitget_live.py` | 388 | 交易所适配 | 保留 |
| `tg.py` | 449 | Telegram 通知 | 保留 |
| `secrets_loader.py` | 140 | 密钥加载 | 保留 |

### 2.3 研究方法学(555 行,拆分)

| 模块 | 行 | 现状 | 动作 |
|---|---:|---|---|
| `research.py` | 555 | `split_boundary` 被 `backtest_v6`/`campaign` 生产使用;其余是 §9 全流程 | **拆分**(D13):`split_boundary` → 独立 `split.py`,其余归档 |

### 2.4 历史轮次(3,469 行,归档)

| 模块 | 行 | 唯一引用方 | 动作 |
|---|---:|---|---|
| `strategy_v8.py` | 169 | `run_backtest_v8.py` + `test_v8.py` | 归档 |
| `quality.py` | 241 | `run_v14_arms.py` + 3 个 test;被 `bandside`/`harvest`/`tiered` 反向依赖 | 归档 |
| `harvest.py` | 302 | `run_v16`–`run_v20` + 3 个 test;被 `repairs`/`tiered` 反向依赖 | 归档 |
| `repairs.py` | 235 | `campaign._inject_ctx18` + `run_v18` + `tiered` | **先断钩子**,再归档 |
| `tiered.py` | 344 | `run_v19/v20` + `live_runner`(v20 分支) | 归档 |
| `lifecycle.py` | 156 | `run_v20` + `live_runner`(v20 分支) | 归档 |
| `bandside.py` | 108 | `run_v15.py` + `test_v15.py` | 归档 |
| `zonebio.py` | 504 | `run_zonebio.py` + `test_zonebio.py` | 归档 |
| `quattro.py` | 465 | `run_quattro.py` + `test_quattro.py` | 归档 |
| `aggtape.py` | 358 | `run_aggtrades_study.py` + `test_aggtape.py` | 归档 |
| `balance.py` | 132 | `_diag_balance.py` / `_diag_zone_accuracy.py` | 归档 |
| `flow_features.py` | 95 | `run_flow_study.py` + `test_flow.py` | 归档 |
| `flow_study.py` | 82 | `run_flow_study.py` / `run_aggtrades_study.py` | 归档 |
| `tape_features.py` | 62 | `diagnose_current_btc.py` | 归档 |

> 归档 ≠ 删除。`research/rounds/v1`–`v6` 已是先例;v13–v20 照此办理,保持"每个历史判定可复现"(§4)。
> `zonebio` 与 `quality` 存在反向依赖(`quality.py` → `zonebio`),归档时必须整组移动,不能单独搬一个。

## §3 缺陷明细(按"精度"排序,每条均可复核)

### D1 容量三套公式 — 准入、候选、成交不同源【算法缺陷】

| 位置 | 公式 | 平仓成本 | 双边成本 |
|---|---|---|---|
| `campaign.py:269 _outstanding_worst_case` | `qty·(mark−invalid) + qty·mark·(taker+slip)` | 含 | 单边 |
| `campaign.py:283 _candidate_worst_case` | `qty·risk + qty·ref·2(taker+slip)` | — | 双边 |
| `broker.py:550 _available_stop_risk` | `qty·max(0, dir·(mark−invalid))` | **不含** | **不含** |

**影响**:预检(决策时)比成交钳制(`broker.py:838`)严格。仓位可能在"预检通过"后于成交时被静默缩量,账本只看得到结果,看不到原因;D2 实验"冻结计量基"同时改动两处,测到的是两套公式之差,不是策略之差。
**建议**:单一 `CapacityPolicy.evaluate()`;`basis ∈ {mark, entry}`、`include_close_cost` 显式参数化;预检与成交调用同一函数。

### D2 事件别名双约定 — 过滤生效、计数恒零【语义缺陷 + 静默 bug】

```text
config/v21.json:112          whitelist = [["ACCEPT",±1], ["REJECT",±1]]
_diag_v21_abcd.py:252        wl = frozenset((str(e), int(d)) ...)      # 未归一
campaign.py:436              go_set = base_rp.whitelist                # 未归一
campaign.py:679/681/790      (e.id, e.direction) in go_set             # e.id ∈ {E1,E3}
strategy_v6.py:173           wl = canonical_event_id(eid) ...          # 路由侧归一
```

事件检测器**只**发射 `E1`/`E3`(`events.py` 中 `ACCEPT` 仅出现在 147–151 的常量与别名表)。因此:

- 路由(`strategy_v6.py:173`)先归一 → **决策与成交正常**;
- 计数(`campaign.py:679/681/790`)不归一 → **`go_events` / `mic_go_events` 在 v21 配置下恒为 0**。

**影响**:v21 的报告若引用 `go_events` 作为"信号数",该值不可信。测试未覆盖(v10/v13/v17 用 E1/E6/E7 口径书写,天然通过;`test_canonical_baseline.py:83` 只对 canonical 口径)。
**建议**:别名归一**只发生在配置解析边界**;`go_set` 一律存 canonical id;`campaign` 计数与路由共用同一个 `canonical_event_id`。同时 `WHITELIST_BALANCE`(`strategy_v6.py:90`)8 项去重后实际只有 4 项。

### D3 文档与代码漂移 — `_sync_balance_boxes`【语义缺陷】

| 位置 | 文档说 | 代码做 |
|---|---|---|
| `structure.py:440-441` | "只有高质量平衡箱(**rotations >= 2**)" | `:453` `b.rotations >= 1` |
| `structure.py:445-446` | 破位边缘保留 **60 bars** / 活跃边缘 **120 bars** | `:492` `<= 12` / `:493` `<= 24` |

**影响**:注释是唯一的"参数来源说明",它现在是错的。任何据此判断"为什么这个箱被采纳"的推理都会错。
**建议**:数值全部从 `Params` 显式取(新增 `box_min_rotations` / `box_keep_broken_bars` / `box_keep_active_bars`),注释改为引用参数名,不再写死数字。

### D4 `structure` 的四个造区开关,全部无配置、全部不可达【死代码】

`structure.py:67-70`:`stale_after_bars` / `min_amplitude_atr` / `use_same_balance` / `use_balance_recognition`。
实测 `config/*.json`:**没有任何配置设置它们**(唯一设置的是 `use_balance_detector: true` 与 `balance_quality_min: 0.35`)。
且 `use_balance_detector=True` 时 `on_pivot` 在 `structure.py:324-325` 直接 `return` —— 以 pivot 造区的整段实现(约 110 行)在 v21 下不可达。

**建议**:四个开关 + `on_pivot` 造区分支保留为 `legacy_zone_construction` 独立函数(供历史轮次归档使用),v21 路径只留 `_sync_balance_boxes`。

### D5 `whitelist` 三重身份【退役未拆】

- `config/v14.json:123` / `v15` / `v16`:"2026-10-20 所有者决议:**废弃黑白名单机制**"。
- 但 `config/v21.json:112` 仍有 `whitelist`,`strategy_v6.py:173` 仍在用它做**真实过滤**(只放行 E1/E3)。
- `campaign.py:436` 又用它做计数(且如上所述失效)。

**结论**:它不能简单删 —— 它现在承担的是"router 的事件订阅"。但它不该是一个"名单字段"。
**建议**:降格为 `EntryPolicy.subscribes: frozenset[EventKey]`,由 router 构造时声明,配置只描述策略而不描述名单;`campaign` 不再持有 `go_set`。

### D6 `campaign` 的版本钩子 — 22 个参数【版本化编码】

关键字参数:`segment, dev_frac, equity0, risk_frac, leverage_cap, taker_fee, slippage_per_side, funding_per_8h, cost_multiplier, contracts, params, n_fill, portfolio_cap_frac, maker_fee, router_params, router_factory, enable_mic, arm, mic_families, router_params_by_tf, tf_cap_by_tf, dynamic_risk_tiers`。

其中"默认 None/False ⇒ 逐位旧行为"的钩子 9 个,对应实现:`_mic_trigger_step`(635–699)、`_inject_ctx18`(716–740)、`_arm_route`(909–983)、`_arm_reduce_limit`(983–1016),合计约 **250 行**。模块 docstring 用 9 段"vN 加性钩"描述这些分支。

**建议**:`run_campaigns(config: CampaignConfig)` 收敛;钩子移入归档目录后,主函数只保留 v21 一条路径。`router_factory` 的 5 个实现(`V6/V8/V14/V16/V20Router`)随之归一为 1 个。

### D7 `position` 的开关矩阵 — 8 个 v16–v20 开关,v21 只用 1 个【死代码】

`PositionManager.__init__`(`position.py:183-224`)接收 `harvest_family / time_cap_bars / cap_mfe_min / harvest_ladder3 / early_exit / trail_arm_gate / harvest_fade_2leg / reject_opp_lock_frac` 八个 v16–v20 开关;v21 只传 `reject_opp_lock_frac`(与 `breakeven_on_reduce`)。v16–v20 逻辑在 `position.py:249-340` 一带,约 **200 行**不可达。

**建议**:`reject_opp_lock_frac` 提升为一等策略参数;其余七个小开关移入 `research/rounds/v16-v20/position_ext.py`,v21 主类只保留 §8.1/§8.2/§8.3/§8.5。

### D8 `broker` 的限价撮合路径在 v21 下不可达【死代码】

`submit_limit` 的调用方**只有** `campaign.py`(886 为 canonical weak-E7;930/950/970/1000 为 arm 路径)。而 v21 名单只含 E1/E3 ⇒ `ROUTE_WEAK_E7`(`strategy_v6.py:315`,要求 E6 登记 + 同 episode E7 回踩)不可能产生 ⇒ `campaign.py:886` 永不执行 ⇒ `broker.resting` 恒空。

受影响的实现:`RestingLimitOrder` / `submit_limit` / `has_entry_limit` / `cancel_resting` / `on_15m_bar` 的挂单分支 / `_timeout` / `_trigger_to_pending` / `_admin_row` / `_fill_open_limit` / `_fill_ladder_add` / `_fill_reduce_limit` / `_limit_skip`,合计约 **300 行**。

**建议**:隔离为 `broker_limits.py`,v21 主 broker 只保留市价撮合 + 账本 + 容量;`on_15m_bar` 在主路径退化为断言(`resting` 必须为空)。

### D9 实盘双引擎由同一个布尔区分【风险】

```text
live_runner.py:372   self.engine_version = str(self.cog_cfg.get("engine") or "v20")
live_runner.py:373   if self.engine_version == "v21": ...
live_runner.py:134   if raw.get("use_balance_detector"):   # 决定 PositionManager 走 v21 还是 v20
live_runner.py:108   def load_v20_params() ... V20Router    # v20 专用,死支
```

两个开关(`engine` 与 `use_balance_detector`)可组合出四种状态,其中两种未定义(`engine=v21` + `use_balance_detector=false`;`engine=v20` + `true`)。

**建议**:删除 `engine` 字段,只保留 `use_balance_detector` 并**启动即校验**;v20 分支随 §7 Batch 2 归档。

### D10 三个 "balance"【命名失真】

| 名称 | 位置 | 实际用途 |
|---|---|---|
| `balance.py`(`BalanceBook`) | 132 行 | v21.2 前身,**仅** `_diag_balance.py` / `_diag_zone_accuracy.py` |
| `balance_detect.py`(`BalanceDetector`) | 216 行 | **v21 生产实现**,由 `structure.py:311` 惰性导入 |
| `structure._sync_balance_boxes` | `structure.py:436` | 生产桥接:箱沿 → R/S 区 |

惰性导入让"生产依赖"在模块头上看不见。
**建议**:`balance_detect.py` → `box_detector.py`;`balance.py` 归档;导入提到模块顶层,依赖显式化。

### D11 命名按版本,而非语义【命名失真】

- `strategy_v6.py` / `V6Params` / `V6Router` 承载的是 **v21** 语义(balance box + ACCEPT/REJECT + per-TF 参数)。
- `backtest_v6.py` 是唯一回放核心,也被 v21 使用。
- `campaign._reconstruct_trades_v6`(1016)包装的是 `backtest_v6.reconstruct_trades_v5` —— v6 名与 v5 名同时出现。

**建议**:`router.py`(`Router` / `RouterParams`)、`replay.py`、`reconstruct_trades`;版本后缀只在归档目录保留。

### D12 `mic` 与 `trigger` 是同一件事【命名失真】

`trigger.py` 的 docstring 通篇是 v10 "触发麦克风(mic)";`config/v21.json` 用 `trigger_tf_by_htf` / `tf_list`;`campaign` 用 `enable_mic` / `mic_families` / `mic_go_events`;`live_runner` 用 `mic_cog`。

**建议**:统一为 `trigger`(`trigger_tf` / `trigger_cog` / `trigger_events`);`mic` 仅作为归档术语保留。

### D13 `research.py` 混合职责【分层不清】

`split_boundary` 是**生产契约**(`backtest_v6.py:35`、`campaign.py:73` 都在用),却与 §9 研究流程(`collect_events` / `measure_symbol` / bootstrap)同处一个 555 行文件。

**建议**:`split.py`(纯函数,约 20 行)进生产层;其余归档到 `research/`。

### D14 归档位置不一致【流程债】

`research/rounds/v1`–`v6` 已归档;`v13`–`v20` 的 runner + 实现 + 测试仍在 `src/`、`scripts/`、`tests/` 主目录。

**建议**:统一到 `research/rounds/vN/`,并在 `docs/research-log.md` 保留指引。

### D15 跨模块使用私有函数【封装缺陷】

`campaign.py:70` `from .broker import ..., _floor_step`。

**建议**:`_floor_step` → `broker.floor_step`(公开),或移入 `units.py`(`size_step/size_place` 换算与撮合无关)。

### D16 配置键静默失效 — `N_a` / `N_f` / `N_time`【语义缺陷】

```text
config/v21.json:16-18    "N_a": 3, "N_f": 3, "N_time": 24
structure.py:84           wanted = {f: raw[f] for f in cls.__dataclass_fields__ if f in raw}
structure.py:65-67       Params 字段名是 n_a / n_f / n_time(小写)
```

`Params.from_json` 按**字段名精确匹配**,`"N_a"` ≠ `"n_a"` ⇒ 三个键被静默丢弃,实际用默认值。默认值恰好相同(3/3/24),所以行为没漂移 —— 但**改配置不会生效**。
同样的写法存在于 `canonical.json`、`defaults.json`、`v2`–`v20` 全部配置,说明这是长期约定的笔误。

**建议**:`from_json` 增加别名表(`N_a→n_a` 等)或直接改配置键名;增加一个"配置键未被消费即 raise"的启动校验(与 D9 的 fail-closed 同一条纪律)。

## §4 测试清单 — tests(35 个,11,268 行)

### 4.1 绑定 v21 生产链(保留,4,382 行 / 19 个)

| 测试 | 行 | 覆盖 |
|---|---:|---|
| `test_data.py` / `test_aggregate.py` | 328 | 数据与聚合契约 |
| `test_structure.py` / `test_events.py` / `test_hypothesis.py` | 746 | 认知层 |
| `test_e3_route.py` / `test_zone_merge_cap.py` | 92 | E3 路由 / 区域合并上限 |
| `test_broker.py` / `test_position.py` | 888 | 账本 / 撮合 / 持仓 |
| `test_portfolio_stop_risk.py` / `test_protective_execution.py` | 146 | 容量 / 保护性止损 |
| `test_v6_campaign.py` / `test_v6_strategy.py` | 424 | 回放主循环 / 路由 |
| `test_canonical_baseline.py` | 157 | canonical 对账 |
| `test_v21_balance_trigger.py` / `test_balance_detect.py` | 477 | v21 平衡箱 / 触发 |
| `test_live_runtime.py` | 321 | 实盘运行时 |
| `test_v10_trigger.py` | 538 | 触发层(现役) |
| `test_research.py` | 265 | 研究工具 |

### 4.2 绑定历史轮次(随代码归档,6,886 行 / 16 个)

`test_v8 448` / `test_v9 441` / `test_v13_cascade 646` / `test_v14_arms 380` / `test_v14_fill_models 254` / `test_v14_quality 176` / `test_v15 326` / `test_v16_harvest 503` / `test_v17_directive 374` / `test_v18_repairs 621` / `test_v19_tiered 621` / `test_v20_lifecycle 638` / `test_quattro 377` / `test_zonebio 513` / `test_aggtape 317` / `test_flow 251`。

> **注意**:`test_v13_cascade`(646)与 `test_v10_trigger`(538)都触及 `campaign`+`trigger`。归档时保留 `test_v10_trigger` 作为触发层现役回归,v13 那份随轮次移动。
> **注意**:归档后主仓测试数会从 607 降到约 **310**(估算),必须在 `docs/acceptance.md` 记录新基线,否则"607 passed"这条历史凭据会被误读为退化。

## §5 脚本清单 — scripts(37 个,17,454 行)

| 分组 | 数量 | 行数 | 文件 | 处置 |
|---|---:|---:|---|---|
| 运维 | 3 | 2,320 | `live_runner 1973` / `live_paper_check 229` / `plan_watch 118` | 保留 |
| 当前回放入口 | 1 | 848 | `run_backtest_v6.py` | 保留(改名 `run_replay.py`,与 D11 一致) |
| v21 研究 | 3 | 1,128 | `_diag_v21_abcd 458` / `_diag_v21_backtest 175` / `_diag_v21_tf_sweep 495` | 保留,合并为一个 `study_v21.py` |
| 数据 | 3 | 726 | `fetch_binance_klines 338` / `fetch_aggtrades 315` / `tape_recorder 73` | 保留 |
| 固化 | 1 | 155 | `freeze_canonical_baseline.py` | 保留 |
| 历史轮次 | 15 | 11,519 | `run_backtest_v8 812` / `v10 769` / `v13 943` / `run_v14_arms 791` / `run_v15 693` / `run_v16 847` / `run_v17 1027` / `run_v18 893` / `run_v19 1053` / `run_v20 1136` / `run_event_study_v9 555` / `run_quattro 385` / `run_zonebio 320` / `run_aggtrades_study 904` / `run_flow_study 391` | 归档 |
| 诊断 | 11 | 758 | `_diag_balance 53` / `_diag_balance_detect 29` / `_diag_losers 105` / `_diag_winners_losers 94` / `_diag_zones 45` / `_diag_zone_accuracy 50` / `diagnose_current_btc 148` / `analyze_deep_trade_anatomy 102` / `analyze_entry_position 52` / `analyze_stage_diagnosis 49` / `autopsy_e234 31` | 归档(依赖历史数据形状) |

> `run_backtest_v6.py` 是 `docs/SEMANTICS.md` 声明的唯一入口,**不能归档**;但它引用 `config/canonical.json`(v6 名单 E1/E6/E7),与 v21 的 E1/E3 是两套策略。`run_backtest_v6.py:5` 至今仍自称"当前生产配置",这句话本身就是 D11/D14 的证据 —— 文档必须写清 `canonical` ≠ `v21`。

## §6 配置键审计 — `config/v21.json`

| 键组 | 键 | 读取者 | 状态 |
|---|---|---|---|
| 认知层 | `atr_n` `k_rev` `k_zw` `k_bm` `k_rj` `k_span` `k_room` `tol_atr` `e4_depth_atr` `e4_recovery` `range_max_pivots` `e5_edge_atr` `merge_max_atr` `use_balance_detector` `balance_quality_min` | `structure.Params.from_json` | 生效 |
| **静默失效** | `N_a` `N_f` `N_time` | —(被 `from_json` 丢弃) | **D16** |
| 账户层 | `risk_frac` `leverage_cap` `taker_fee` `maker_fee` `slippage_per_side` `funding_per_8h` `portfolio_cap_frac` `tf_cap_by_tf` | `broker` / `campaign` / `_diag_v21_*` | 生效 |
| 分段 | `dev_frac` | `campaign.run_campaigns` | 生效 |
| 路由 | `k_is` `k_dq` `k_imp` `turtle_stop_atr` `router_params` `router_params_by_tf` `whitelist` | `strategy_v6` | 生效(名单语义见 D5) |
| 时间 | `tf_list` `trigger_tf_by_htf` `obs_tf_seconds` `exec_tf_seconds` | `trigger` / 回放入口 | 生效(`exec_tf_seconds` 仅被 `run_backtest_v6/v8/v10/v13` 校验 900) |
| 执行 | `n_fill` | `broker` / `campaign` | 生效 |
| 持仓 | `arm_config`(`breakeven_on_reduce` / `reject_opp_lock_frac`) | `live_runner.new_position_manager` | 生效(仅 2 键) |
| 研究 | `small_account_compounding` | **仅** `_diag_v21_tf_sweep.py` | 不进实盘,标注清楚 |
| 数据 | `symbols` | 各入口 | 生效 |
| 元数据 | `version` `note` `meta` | 无(人读) | 保留 |

> `exec_tf_seconds` 在实盘路径**没有校验点**(只有回放脚本校验),这与 `live_runner` 固定 15m 子帧的假设之间没有任何断言连接 —— 属 D9 同类问题,归入 Batch 2 的 fail-closed。

## §7 分批执行(capacity 放最后,因为它会动数字)

### Batch 0 — parity 基座(前置,必须第一个做)

1. 新增 `tests/test_parity_a0.py`:固化 BTC/ETH/SOL `N=1153 / +281.0` 与 book4 `N=1440 / +380.1` 两条断言。
2. `_diag_v21_abcd.py` 的 outdir `stem` 加入 symbol 集,消除同名覆盖(本轮已手工用 `explore/` `doge/` `book4/` 规避)。
3. 产出 `docs/parity.md`:一条命令、两个期望值、判定口径。

**验收**:两条基线可一条命令复现;任何后续批次跑它必须逐位相等。

### Batch 1 — 命名与常量(不改数字)

| 项 | 内容 | 影响 |
|---|---|---|
| 1.1 | 别名归一移到配置解析边界(D2/`events.py`) | 路由不变 |
| 1.2 | `WHITELIST_BALANCE` 去重(8→4) | 不变 |
| 1.3 | `_floor_step` → 公开 API(D15) | 不变 |
| 1.4 | `balance_detect` → `box_detector`,`balance.py` 归档(D10) | 不变 |
| 1.5 | `strategy_v6` → `router`、`V6Params`→`RouterParams`(旧名 alias 保留一版)(D11) | 不变 |
| 1.6 | `mic` → `trigger` 术语统一(D12) | 不变 |
| **1.7** | **修 `go_events` 计数(D2)** | **唯一会改变输出字段的项**:从 0 变为真实值。需在报告标注"计数修正",不影响盈亏 |

### Batch 2 — 退役(不改数字)

1. `live_runner` 删 `engine` 字段 + v20 分支,启动即校验(`D9`)。
2. `campaign` 移除 `arm` / `mic_families` / `router_factory` / `_inject_ctx18` / `_arm_route` / `_arm_reduce_limit`(D6);`enable_mic` **保留**(v21 生产在用),Batch 1.6 后更名为 `enable_trigger`。
3. `position` 的 v16–v20 七个开关外移(D7);`broker` 限价路径隔离(D8)。
4. 归档 `repairs` / `quality` / `harvest` / `tiered` / `lifecycle` / `strategy_v8` / `bandside`;随后归档 `quattro` / `aggtape` / `tape_features` / `flow_*` / `zonebio` / `balance`。
5. 同步迁移 15 个 runner + 16 个测试(§4.2 / §5)。

**验收**:`git grep` 在 `src/` 已无 `V14Router|V16Router|V18Router|V20Router|ArmConfig|quality|harvest|tiered|lifecycle|repairs`;`pytest` 全绿;parity 不变。

### Batch 3 — 算法收准(部分会改数字)

| 项 | 内容 | 数字 |
|---|---|---|
| 3.1 | `CapacityPolicy` 统一(D1) | **会变**(见 §8-D1) |
| 3.2 | `structure` 死开关清理 + docstring 改为参数引用(D3/D4) | 不变(不可达路径) |
| 3.3 | `from_json` 别名 + 未知键 raise(D16) | 不变(默认值相同) |
| 3.4 | `CampaignConfig` 收敛(D6 参数) | 不变 |

### Batch 4 — 契约与文档

1. `docs/SEMANTICS.md`:链路图去掉版本号;`canonical` 与 `v21` 的关系写清(两套名单)。
2. `docs/acceptance.md`:记录新测试基线数(约 310)。
3. `docs/research-log.md`:归档指引。
4. `config/v21.json`:仅删无读取者的键(不改任何数值)。

## §8 需要拍板的决定

| 编号 | 决定 | 选项 | 影响 |
|---|---|---|---|
| **D-1** | 容量公式统一后的基线 | (a) 统一 + 重冻结 (b) 统一 + `legacy` 开关过渡 (c) 暂不动 | (a) 会让 `+281.0` 失去与既有报告的可比性;我建议 (b) |
| **D-2** | v13–v20 归档 or 删除 | 归档 / 删除 | 归档保留可复现;删除让仓库更干净但历史判定不可重跑 |
| **D-3** | `go_events` 修正是否视为行为变更 | 视为 bug 修正 / 视为变更需重跑 | 不影响盈亏,只影响计数列 |
| **D-4** | 旧名 alias 保留多久 | 一个 PR / 立即删 | 立即删更干净,但会打断所有脚本 |
| **D-5** | 测试基线 607 → ~310 | 接受 / 保留占位测试 | 需写入 `acceptance.md` |

## §9 完成判据

1. **parity**:`tests/test_parity_a0.py` 两条基线逐位相等(除非 §8-D1 选定 (a),则同时更新基线并标注)。
2. **引用**:`src/` 只保留一个 router 实现(`Router`);容量计算中不再出现 `"@" in symbol` 这类字符串解析。
3. **单一来源**:容量、槽位、事件订阅各只有一个实现;`git grep` 能证明。
4. **文档一致**:所有注释中的阈值改为参数名引用(D3 类问题清零)。
5. **测试**:`pytest -q` 全绿,基线数写入 `acceptance.md`。
6. **可复现**:`python scripts/study_v21.py --parity` 一条命令输出两个基线。

## §10 风险与回滚

| 风险 | 触发 | 回滚 |
|---|---|---|
| 断引用时改了语义 | parity 数字变化 | 单批 revert;禁止跨批提交 |
| 归档漏依赖(如 `quality`→`zonebio`) | ImportError | 归档前跑一次 `python -c "import market_operator"` + 全量 pytest |
| 容量统一后报告数字不可比 | §8-D1 选 (a) | 保留旧报告并在新报告头部声明基线变更 |
| 实盘选择器改动引入启动失败 | live smoke | 选择器改动单独一批,附 `live_paper_check` 冒烟记录 |

---

## §11 执行状态(2026-10-23 批次)

本批只做**不改数字**的收口:单链路、拆分、命名、魔法数字参数化。全部改动以
`tools/replay_acceptance.py`(Δ=0.0000)与 `pytest -q`(524 passed / 1 skipped)复核。

| 项 | 状态 | 证据 / 未闭原因 |
|---|---|---|
| **D3** 平衡箱窗口 | **DONE** | `Params.box_min_rotations/box_keep_broken_bars/box_keep_active_bars`;`structure.py:466` docstring 与实现一致;`tests/test_structure.py` 钉死默认值 (1,12,24) 与门限行为 |
| **D9** 双引擎 | **DONE** | `scripts/live_runner.py` 无 `CONFIG_V20`/`load_v20_params`/`engine_version`;`cognition.engine` 已从 `config/live.json` 删除;`tests/test_live_runtime.py` 4 条断言(启动失败而非回退) |
| **D13** `research` 拆分 | **DONE** | 新增 `src/market_operator/split.py`(20 行);`replay`/`campaign` 改从 `.split` 导入;`research` re-export;`tests/test_layering.py::test_research_methodology_is_not_in_production_closure` |
| **D15** `_floor_step` | **DONE** | `broker.floor_step` 公开,`_floor_step` 兼容别名保留给 `research/rounds` 历史脚本;`campaign`/`live_runner`/`test_broker` 已迁移 |
| **D16** 配置键静默失效 | **DONE**(前批) | `Params.from_json` 用 `_key_id` 归一(`N_a`≡`n_a`),同字段多键直接 raise;`test_params_rejects_two_keys_for_one_field` |
| **D2** 事件别名 | **PARTIAL** | 归一点已唯一(`events.canonical_event_id`,routing 每 bar 归一;`WHITELIST_BALANCE` 去重为 4 项)。**未闭**:`campaign.py:439 go_set = base_rp.whitelist` 未归一,而 `config/strategy.json` 写的是 `ACCEPT/REJECT` ⇒ v21 的 `go_events` 恒为 0。一行可修,但会改冻结 counters,须先按 §8 D-3 裁决 |
| **D12** `mic`→`trigger` | **PARTIAL** | 模块已是 `trigger.py`;`enable_mic`/`mic_families` 参数名仍在 `campaign`(随 D6 一起清) |
| **D1** 容量统一 | OPEN | 会改数字,待 §8 D-1 裁决 |
| **D4** `structure` 4 个死开关 | OPEN | `use_same_balance`/`use_balance_recognition`/`stale_after_bars`/`min_amplitude_atr` 在 v21 不可达,但历史轮次脚本与 3 个 test 仍在用;清理需与归档同批 |
| **D5** 白名单→`EntryPolicy` | OPEN | 与 D2 同批(先定计数口径,再动类型) |
| **D6** `campaign` 版本钩子 | OPEN | `ArmConfig`/`router_factory`/`mic_families` 仍在 `run_campaigns` 签名;v13–v20 脚本依赖它 |
| **D7** `position` v16–v20 开关 | OPEN | 七个开关在 v21 恒定取默认值;外移需同步 16 个历史 test |
| **D8** `broker` 限价路径 | OPEN | v21 不可达;隔离需先证明 `limit_front_edge` 只在历史臂出现 |
| **D10** `balance_detect` 改名 | OPEN | 仍是惰性导入(`structure.py:341`);改名会打断 `_diag_balance_detect.py` 与 `test_balance_detect.py` |
| **D11** 版本化命名 | **DONE** | `routing`/`replay`/`trigger`/`scenario`;`tests/test_layering.py::test_production_module_names_carry_no_version` |
| **D14** 历史轮次归档 | OPEN | `scripts/run_v13..v20` 仍在主目录;归档须与 §4.2 的 15 个 runner 一起迁 |

**下一批建议顺序**(每批单独 commit,每批跑 parity):

1. **D6 → D7 → D8**(纯退役,不改数字):先 `campaign` 摘钩,再 `position` 外移,
   最后 `broker` 限价隔离;每步只动一处,失败可单批 revert。
2. **D2 + D5**(需裁决 §8 D-3):`RouterParams.__post_init__` 归一 whitelist,
   然后 `go_events` 才有真值;基线 counters 需重抓并标注"计数修正"。
3. **D4 + D10 + D14**(归档批):与 `research/rounds/` 一起迁,`src/` 只留生产闭包。

---

**文档状态**:设计稿 + §11 执行状态。D3/D9/D13/D15/D11/D16 已闭合;D1/D4–D8/D10/D14
按上表顺序待做,D2/D5 停在需要口径裁决处。

