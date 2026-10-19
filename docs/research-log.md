# 研究诚实性日志(方法论 §9.4)

本日志记录阶段 3 研究中在 dev 集上发现并已修正的问题。冻结纪律:test 集只做
一次最终评估(即 `reports/event_study.md/.json` 所载那次);不允许"改参数直到
盈利";config/defaults.json 与 docs/methodology.md 在研究中未做任何修改。

## 条目

1. **2026-10-19 研究协议口径声明(先于任何结果观察,非修正)**
   - 主视界固定为 H=24(= §7 先验 N_time),GO/NO-GO 仅在其上评估;全 H 指标照常报告。
   - §9.3 判据 2/3/4 的"净期望"判定口径采用**事件净期望 − 对照组净期望均值的增量**
     (依据 §9.1-5 "事件必须表现出增量信息");该解释写入 `research.py` 模块 docstring。
   - 命中判定保守口径:同 bar 双命中记 invalid 优先。
   - 对照组决策 bar 排除任一事件窗口 [known_index, known_index+24],同品种/同 regime/
     同方向,42 个固定种子(SEED_BASE=20261019);dev/test 各自段内采样。
   - 前向窗不足 48 bar 的事件整体跳过(各 H 样本一致),仅涉及数据最末端。
2. **2026-10-19 dev 自测修正(运行前单元测试阶段,未接触 test 结果)**
   - `tests/test_research.py` 两处测试编排错误并修正(均为测试自身的价格路径编排
     问题,被测实现未因此改动):
     a. "已知结局"用例中 bar 低点(99.3)未触及 invalid(99.0),改为 98.9 构成真实的
        同 bar 双命中,断言 MFE/MAE 相应更新;
     b. 成本用例引用了不在 §9 视界集合中的 H=8,改为 H=6 并按 funding 双边公式重算
        期望值(0.0012+0.0004+0.0002·(6/8))。
   - `research.py`:`analyze_segment` 合并键由 (known_index, event_id) 改为每事件唯一
     uid——同 bar 同 id 可在两个不同 zone 上各发射一次,旧键会造成对照聚合串行
     (实现正确性修正,发生于首次全管线运行之前)。
   - `research.py`:block bootstrap 由逐次 Python 循环改为日块和/计数的向量化抽样
     (统计语义不变:日块为抽样单元;仅为运行时长可接受的工程修正)。

3. **2026-10-19 dev 修正:E4 方向硬核对的核对依据**
   首次全管线运行(回放未出 dev 段即中止,未产生任何 dev/test 指标)触发自检
   `ValueError: direction mismatch E4`。原因在我的 `research.py:expected_direction`:
   误用 `attrs["regime"]` 推断方向——那是**触发 bar** 的 regime,E4 激活后窗口内
   regime 可能已退出趋势,而 direction 在激活时刻按趋势方向固定,二者本来就
   允许不一致。修正:改用 attrs 中独立可核对的结构证据(多头回调腿
   `leg_start > pb_price`,镜像为空)推断应有方向;同步更新对应单测用例。
   上游(events.py)行为与冻结语义一致,未做任何改动。
 
4. **2026-10-19 dev 修正(首次完整运行产物的内部一致性检查发现)**
   以第一次运行的报告中间产物做一致性核对时(n 口径表间对照、CI 完整性)发现
   并实现两处修正,均在我的模块内,上游未动;因属实现错误修正,纠正后重评
   test(**§9.4 声明:test 此为第二次评估;第一次数值因下述 bug 作废,不构
   成结论依据,结论强度仅此声明**):
   a. **`uid` 跨品种碰撞**:`measure_events` 的 uid 为品种内枚举序号,多品种合并
      后 `analyze_segment` 以 uid 合并对照聚合,跨品种同号串行(第一次运行
      §1 表 n 与 §2 表 n 不一致可证)。修正:`run_event_study.py` 中 uid 加品种前缀;
      `analyze_segment` 的 H 层 n 改为 uid nunique。
   b. **CI 全 NaN**:任一事件对照池为空 ⇒ 其增量 NaN ⇒ `np.bincount` 权重 NaN 传
      染整组抽样。修正:`analyze_segment` 在 bootstrap 前 dropna 增量,并在 JSON 中
      记录 `incr_n_valid`。
   c. **by_symbol 恒空(判据 4 误全挂)**:`analyze_segment` 合并键未含 symbol/segment,
      合并后变 symbol_x/symbol_y,`by_symbol` 判空 ⇒ 判据 4"正品种数"恒 0(第二次
      运行所有机制 pos_sym=0 可证)。修正:两表共有的 symbol/segment 纳入合并键。
      属同一修正批次(条目 4 的声明覆盖本次重评)。
   d. **对照组排除窗口范围错误(严重,曾污染全部"增量"结论)**:初版
      `measure_controls` 对**全部机制**事件施加 [known, known+24] 排除窗;本数据
      事件密度约每 3 bar 一个事件 ⇒ test 段 5280 bar 中 5257 被排除,池退化为
      16 根病态残差(BTC TRANSITION 池净空期望 -6.1 ATR,与同 regime 朴素质差
      -0.47 ATR 相差 10 倍以上,据此核出)。修正:排除窗改为**同机制**
      (event_id × direction)——对照语义"无该机制信号的随机入场";并在对照行
      记录 `pool_n` 供审计(修正后各机制最小池 6-320,BTC E3|long 最小为 6,
      属事件过密的如实反映)。dev 上抽查验证:第二次运行 short 侧 +1.5~+3.7 ATR
      增量全部由该病态对照驱动,修复后预期归零量级。**条目 4 的重评声明延续
      适用:凡此前产生的 verdict 一律作废。**


5. **2026-10-19 阶段 4 后规格一致性修正(E6 invalid 缓冲)+ 方法论文本澄清**
   - 实现与冻结规格不一致:E1-E4 的 `attrs["invalid"]` 均含 §7 规定的
     `k_bm·atr` 缓冲,E6 遗漏(裸 pivot 价)。修正 `events.py::_structure_break`
     使 E6 一致(short 的 invalid 上移至 `l_star + bm`,long 镜像)。
     影响:E6|short 失效距离变大、r_mult 变小、部分交易被 r_min 过滤——方向
     保守。因此**事件研究重跑一次(§9.4 声明:test 段为第三次评估;仅因实现
     修正,参数与判据未动)**,阶段 5 回测基于重跑后结果。
   - `docs/methodology.md` §8.3 措辞澄清(非参数修改):原文"顺向新 pivot"
     系多头视角,空方无法执行;改为"防线一侧新确认的 pivot(多单=pivot low,
     空单=pivot high)"。语义自始即此,测试与实现均按此执行。


6. **2026-10-19 v2 研究轮协议冻结 + test 段第四次评估声明 + v2 改动清单**
   - **协议冻结**:`docs/v2-protocol.md` 在任何 v2 实现与 v2 运行之前冻结
     （机制 E7 FLIP_RETEST 定义、F1 失效距离下限先验 k_is=0.8、对称双方向、
     {0.6, 1.0} 敏感性仅报告不择优、H1/H2/H3 可证伪假设、评估协议 = methodology
     §9 全流程原样）。`config/defaults.json`、`docs/methodology.md`、v1 全部
     报告与 `docs/acceptance.md` 在本轮中零改动。
   - **test 段第四次评估声明(唯一一次)**:v2 设计依据全部来自 v1 既有产物
     （其 test 段已经三次评估，见条目 4/5)。v2 首次运行脚本
     `scripts/run_event_study_v2.py` 与 `scripts/run_backtest_v2.py` 均含 test
     段 ⇒ **为 test 第 4 次评估，且是 v2 唯一一次**；此后任何 v2 迭代只许用
     dev,test 不再服务于 v2 结论；真正 OOS 只能等 2026-10-19 之后的前向数据
     (v2-protocol §3-2)。若 n<60 判 INSUFFICIENT_DATA，不做叙述性补救。
   - **v2 改动清单（全部可追溯至 v2-protocol 条款；E1-E6 检测输出不变，由
     `tests/test_e7_v2.py::test_v1_regression_parity` 以 v1 产物
     `reports/event_study_events_test.csv` 逐事件比对守护）**:
     a. `structure.py`：新增只读事实记录 `ZoneBook.flips`/`ZoneFlip`(FLIPPED
        转移 bar 的旧角色 + 新角色区快照，只读追加，不改变 §4.2 状态机与
        视图移除时序）——v2-protocol §1 事件信息来源。
     b. `events.py`:E7 常量与 `_flip_retest` 发射器（direction 由 role_new
        决定；invalid = 远端边缘 ± k_bm·atr;ref = 破位边缘；atr = 转移 bar
        ATR)；默认 `enable_e7=False`(v1 目录行为字节级不变）。
     c. `hypothesis.py`:`f1_invalid_distance_ok`(v2-protocol §1 F1 门）。
     d. `research.py`:`expected_direction` 增 E7 分支（以 from_role 为独立
        核对证据）;`collect_events` 增可选 `event_ids` 过滤与 `enable_e7`
        （默认均不变）。
     e. `strategy.py`:`StrategyParams.whitelist`/`k_is` 参数化入口（None/0 =
        v1 路径，explain 文本与 reason 字段逐字兼容）;F1 门按 k_is 生效。
     f. `backtest.py`:`run_backtest`/`prepare_cognition` 增可选
        strategy/whitelist/enable_e7 参数（默认 = v1)。
     g. `config/v2.json`：继承 defaults 全部键（逐项照抄）+ k_is=0.8 +
        whitelist_v2=[["E7",-1],["E7",1]] + k_is_sensitivity_report[0.6,1.0] +
        meta{parent:defaults, protocol:docs/v2-protocol.md}。
     h. `scripts/run_event_study_v2.py`、`scripts/run_backtest_v2.py`：产物
        `reports/event_study_v2.{json,md}`、`reports/event_study_events_v2_*.csv`、
        `reports/backtest_v2.{json,md}`、`reports/trades_v2_*.csv`、
        `reports/equity_v2_*.csv`（与 v1 产物文件名隔离，v1 产物不动）。
     i. `tests/test_e7_v2.py`(9 项）：支撑/阻力两侧 FLIP 正向场景、两个不触发
        对照、F1 恰 k_is 两侧断言、k_is=0 旁路、v1 回归 parity；全仓
        `python -m pytest tests -q` = 111(v1)+ 9(v2)全过。

7. **2026-10-19 v3 预注册(先于任何 v3 运行)**
   - `docs/v3-protocol.md` 已冻结:尺度家族 {2h,4h,12h,1d} × {E7±1},全部 ATR 先验零改动;
     升级判据与 v2 §3-4 逐字相同;本轮为 test 段第 5 次评估且为 v3 唯一一次。
   - 聚合路径:由 data/raw/*_1h.csv UTC 对齐聚合,不重抓 API;新文件入 manifest。

8. **2026-10-19 v3 运行后发现并修复的真 bug(research.py 资金费单位)**
   - `research._net_row` 把"根数"当"小时"计提资金费。1h 帧上 1 根=1 小时,
     **v1/v2 全量数字不受影响**(修复路径 `bar_hours` 默认 1.0 且与原值恒等);
     多尺度下已如实计入。属实现错误修正,非语义/参数变化;v3 判定不受其影响。

9. **2026-10-19 v4 预注册(先于任何 v4 运行)**
   - `docs/v4-protocol.md` 已冻结:成熟度三闸门(E7-only + k_is=0.8 沿用 + 限价接回踩
     N_fill=12)、15m 执行路径、maker/taker 非对称费用;test 段第 6 次评估,v4 唯一一次;
     低样本预期与 INSUFFICIENT 处置已预先声明。

10. **2026-10-19 v5 预注册(先于任何 v5 运行,最终轮)**
    - `docs/v5-protocol.md` 已冻结:破位质量路由(E6 STRONG=市价/WEAK=E7限价)+ E1 市价
      (最高优先级)+ F1 全入口 + short-only(无信息规则)。新先验仅 k_dq=0.5/k_imp=1.5,
      敏感性仅 dev。test 第 7 次评估且最终;升级线不变;无论结局关闭协议级优化循环。

11. **2026-10-19 PR#9(外来贡献)合并前合规登记**
    - 事实登记:PR#9 的诊断脚本 `scripts/diagnose_trade_lifecycle.py` 在 test 段执行两轮
      ⇒ 构成 test 段第 8/9 次曝光;作者声明"diagnostic, 不作 OOS 定论",
      与脚本行为一致,照此登记,不追加任何结论主张。
    - errata:PR#9 对 broker 的修复(跨品种组合杠杆账、幻影保本去除、跳空成交规则)
      使 v1–v5 冻结回放不再逐字可复现;冻结产物保留为历史快照,见 acceptance errata。
    - 数字勘误:PR 说明称基线"225 测试",主线实际为 218(PR#9 后为 257)。

12. **2026-10-19 v6 预注册(先于任何 v6 运行)**
    - `docs/v6-protocol.md` 已冻结:战役引擎(5 TF×3 品种并行隔离)、S1 失效锚定结构区、
      S2 出口几何(主臂 2R clamp+禁保本土,dev 对照臂 Turtle 无目标,dev 判选臂);
      双向恢复(未检验尺度格对称检验);test 段为第 10 次曝光且**终生命名:此后永不用于
      研究迭代**;升级线不变;基础设施与结论分离交付。

13. **2026-10-19 v7 预注册(先于任何 v7 运行)**
    - `docs/v7-protocol.md`:quattro(MIT, upstream/crypto_systematic_research)逐字复刻对照,
      零改动零调参,仅 venue sizing 落地;数据窗差异(2年 vs 上游4.1年)已声明;
      结果仅裁决"同场座照抄规则的正/负",移植与否另立协议。
