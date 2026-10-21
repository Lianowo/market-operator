# Changelog

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

