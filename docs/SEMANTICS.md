# SEMANTICS —— 系统当前唯一语言(规范语义定义)

> 本页定义**当前(规范链)系统说什么语言**。凡代码、报告、讨论中出现的下列
> 术语,以此页为准。历史轮次的差异语义(臂 A clamp2R、v1-v5 入口/出口口径)
> 只在文末表中列入,不在规范链内使用。
> 规范链:`data → structure → events(E1-E7) → hypothesis → routing
> (turtle 唯一语义) → position(§8) → broker → campaign`,回放核心
> `replay`,入口 `scripts/run_backtest_v6.py`,配置 `config/canonical.json`。
> 生产实盘链路 = **同一套引擎**,只是配置换成 `config/strategy.json`(平衡箱
> 双原语 ACCEPT/REJECT、按周期参数、趋势损益),不存在第二套路由/持仓实现;
> 旧 v20 链(`lifecycle`/`tiered`/`repairs`)已退役,启动即校验而非回退。

## 1. 认知帧与时间

- **认知 bar / TF 帧**:决策所用的时间帧,TF ∈ {1h, 2h, 4h, 12h, 1d},由带
  sha256 manifest 的 1h 数据 UTC 对齐聚合而来(aggregate.py);认知帧上的全部
  结构事实只依赖 `known_index ≤ t` 的信息(时间即真相)。
- **decision_ts**:决策时间 = 认知 bar 标签(该 bar **收盘**时刻知时);
  **fill_ts > decision_ts 恒成立**(正确性硬约束,行内断言)。
- **执行细帧**:成交路径所用更细帧 —— TF=1h → 15m,TF≥2h → 1h;认知 bar t
  的执行区间 [t, t+TF) 恒由细帧 bar 无洞组成(对齐断言,违即 raise)。

## 2. 结构认知(structure / hypothesis)

- **pivot(波段端点)**:ATR 自适应确认的摆动极值;携带 `forming_index`(指向
  位置)与 `known_index`(可被知道的位置)。
- **zone(关键价格区域)**:生命周期状态机 ACTIVE / BROKEN / RETESTED /
  FLIPPED / DEAD;角色 SUPPORT / RESISTANCE。
- **regime**:TREND_UP / TREND_DOWN / RANGE,由 pivot 序列判定(谓词含
  "未破坏最近抬升低点"条款)。
- **结构视图(StructureView)**:某一 bar 上供策略消费的只读快照
  (regime, zones, atr, pivots)。

## 3. 事件(events,E1-E7)

事件 = 认知 bar 收盘确认的**市场行为事实**(非信号,不承诺交易)。方向:
阻力侧 −1(short),支撑侧 +1(long)。

| id | 名称 | 含义 |
|---|---|---|
| E1 | BREAKOUT_ACCEPTANCE 突破接受 | zone 收盘破位后连续 N_a 根收在外侧 |
| E2 | BREAKOUT_FAILURE 失败突破 | 刺穿起 ≤ N_f 根内收盘回到带内 |
| E3 | REJECTION 关键位拒绝 | 自极值向正面回退 ≥ k_rj·atr |
| E4 | TREND_RESUMPTION 趋势恢复 | 回调终点 pivot 确认后收盘回收 50% 回调腿 |
| E5 | RANGE_ROTATION 区间回归 | 来源 E3 同 bar 发射的区间反向回归 |
| E6 | STRUCTURE_BREAK 结构破坏 | 最近反 pivot 被突破(HL/LH 序列破坏) |
| E7 | FLIP_RETEST 角色互换回踩 | 破位→回踩→被拒绝→控制权易手确认 |

## 4. 假设(hypothesis)

**Hypothesis** = 事件触发的可证伪交易假设:entry(决策收盘近似)、
invalid(失效价)、target_1(结构目标或 None)、r_mult(或 None)、regime。
F1 闸(k_is=0.8):`direction·(close − invalid) ≥ k_is·atr` —— 天然距离
不足即笔不存在,禁止人工加宽。

## 5. 交易计划(routing,turtle 唯一语义)

事件 → **路由决策**(RouteDecision):`market` / `limit` / `cancel_entry`,
附路由标签。路由 = v5 质量路由,双向白名单 {(E1,E6,E7) × {short,long}}
(方向仅作机制桶标签,不作挑选):

- **E1 ⇒ 市价次开盘**(永远,突破接受不等回踩;有未成交入场限价时先撤单)。
- **E6 质量路由**:STRONG(margin ≥ k_dq=0.5 且 impulse ≥ k_imp=1.5)⇒ 市价
  次开盘;WEAK ⇒ 不入场,登记 weak episode(ref = 被破 pivot 价),待同
  episode 同向 E7 于其 zone 前缘挂限价(G3 语义;episode 消费一次,方向
  错位不匹配,E1 信号到达即丢弃登记)。

**出口几何(turtle,原 v6 臂 B 固化)**:invalid = close − direction·
turtle_stop_atr·atr(初始防线 2.0·atr);**无固定目标**(target_1=None,
不做 room/r_min 门,无结构目标语义,如实声明)。

> 历史:docs/v6-protocol.md §2-S2 保留 A/B 二臂协议原文(未改动);判选
> 结果(两臂 dev@1.5x 均 ≤ 0,按冻结规则取臂 B)见 reports/backtest_v6.json
> 及双臂归档实现 research/rounds/v6/。

## 6. 持仓(position,§8 六条)

每战役一仓(不与入场挂单共存);加仓关闭(allow_add=False)。管理规则按
确认事实逐认知 bar 裁决(入场成交的认知 bar 不裁决):

- **§8.1 破位出**:认知 bar 收盘越过 invalid ⇒ 市价出场。
- **§8.2 目标减仓**:触 target_1 减 50%(挂目标前缘限价,超时转市价);
  turtle 语义无目标 ⇒ 恒走其市价回退分支(机制设计如此)。
- **§8.3 pivot trailing**:confirmed pivot 后防线只收紧不放松。
- **§8.4 加仓**:关闭。
- **§8.5 反向 CHoCH**:证据充分 ⇒ 市价出场。
- **§8.6 时间止损**:**关闭**(turtle 接线,time_stop=False);
  breakeven_on_reduce=True(臂 A 的"减仓后禁保本"随臂 A 退役)。

## 7. 执行与账户(broker)

- **intent**:OrderIntent(OPEN/REDUCE/ADD/EXIT;词表规范家园 = broker.py)。
- **市价单**:下一执行细 bar 开盘成交,taker + 滑点(成本乘数放大)。
- **限价单**:armed = 认知 bar 标签;fill_after = 决策 bar 最末细 bar;
  expiry = fill_after + N_fill(12 墙钟小时);**严格穿过才成交**(maker,
  持平不成交,穿透缺口按 limit 价,保守);入场超时 = 没有这笔交易
  (EXPIRED),减仓超时转 taker 市价(TIMEOUT_TO_TAKER)。
- **账户**:共享权益;资金费按 8h 标准计提;近似强平(裕度保护,预期 0 触发);
  跨战役杠杆账与 portfolio_stop_risk_frac(PR#9 口径)做成交时刻第二层钳制。
- **合约规格**:离线缓存强制(data/contracts.json,逐条 sha256),不联网。

## 8. 战役(campaign)与组合风险

- **战役** = (symbol, TF) 独立单元:自有认知缓存 / 路由状态 / 持仓 / 挂单,
  互不感知、互不触发对方出场;共 3 × 5 = 15 战役并行。
- **组合容量**:全部战役合计 open 最坏情景(止损实现亏损 + 平仓侧费用预算
  + 新仓风险与双边费用)≤ portfolio_cap_frac(3%)× 权益;决策时刻按信号
  时间 FIFO **整笔跳过**(counters.portfolio_cap_skips + cap_skips 明细)。
- **段(segment)**:full/dev/test;每战役按自身 TF 帧 split_boundary(0.7);
  OPEN 决策仅段内启用,交易按 OPEN 决策 bar ∈ 段归属;dev/test 拐点 = 冻结
  1h 索引 12320。

## 9. 风险与诚实纪律

- 冻结先验不许上 test 择优;cost_multiplier ∈ {1.0, 1.5, 2.0} 成本压力。
- test 段评估纪律:v6 轮已消耗唯一一次曝光(research-log #12 终生声明),
  此后 test 仅作现状度量,不产生新协议判定。
- 冻结产物对账(违即 raise):E7 1h 计数 == reports/backtest_v2.json;
  E7 多 TF == reports/event_study_v3.json;E1+E6|short == reports/backtest.json
  go_events(v1 parity)。

## 历史轮次语义(仅列入,不在规范链使用)

| 轮 | 协议 | 语义要点(与规范链的差异) | 归档 |
|---|---|---|---|
| v1 | docs/methodology.md | 仅 {E1,E6}|short;结构目标假设 + r_min 门;1h 帧 | research/rounds/v1/ |
| v2 | docs/v2-protocol.md | + E7 发射、F1 闸、白名单化 | research/rounds/v2/ |
| v3 | docs/v3-protocol.md | 事件目录跨 TF 证据(12h/1d),多 TF 聚合对账 | research/rounds/v3/ |
| v4 | docs/v4-protocol.md | 15m 执行帧;G3 前缘限价;§8.2 限价减仓+超时转市价 | research/rounds/v4/ |
| v5 | docs/v5-protocol.md | E6 质量路由(STRONG 市价 / WEAK→E7 限价) | research/rounds/v5/ |
| v6 A/B | docs/v6-protocol.md | 臂 A clamp2R:S1 区缘失效、room≥1.5R、target 钳 2R、减仓后禁保本;臂 B 即规范 turtle | research/rounds/v6/ |
| PR#9/#10 | (设施) | opportunity 因果生命周期 / 15m 保护性止损 / sqlite 执行账本(前向设施,不入回放链) | research/rounds/external/ |
| v7-v20 | docs/v7..v20-protocol.md | E8-E10 扩展事件、级联触发(trigger.py)、质量五问(quality.py)、收割婚配(harvest.py)、修复包(repairs.py)、阶梯配注(tiered.py)、生命周期(lifecycle.py)、逐笔流研究(aggtape.py) | src/market_operator/ & scripts/run_v*.py |
| v21 (当前实盘架构) | docs/v21-protocol.md | `BalanceDetector` 六维平衡箱识别(`use_balance_detector=True`) + `ACCEPT`/`REJECT` 双原语 + M3c 高阶降档混合触发(`2h`/`4h` 同周期,`12h->2h`,`1d->4h`) + `REJECT` 箱体外缘结构止损 & 对侧突破锁利 50%(`reject_opp_lock_frac=-0.50`) + 各周期独立风险容量池(`tf_cap_by_tf`) + 阶梯动态复利与双 Bot 黑白 K 线图通知 | config/strategy.json & config/live.json |

