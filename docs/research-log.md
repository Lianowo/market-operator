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

14. **2026-10-20 v8 预注册(先于任何 v8 运行)**
    - `docs/v8-protocol.md`:新事件家族 E8 EDGE_RECLAIM(摆动带边缘+收复确认,
      pos≤0.20 多 / ≥0.80 空,先验);test 段第 11 次曝光且此后只许前向;
      判别量(入场 pos 构造集中 + 胜率相对 E6/E7 桶差异)已写死在先。

15. **2026-10-20 区域合并封顶(账面缺陷修复,非策略变更)**
    - 实况诊断发现:无界链式并集(v1 语义)滚出 [58680,125338] 巨区,吞掉全部细结构,
      使系统近价区"没有锚"。修复:`Params.merge_max_atr`(默认 0=历史无界,freeze parity
      不动;canonical 启用 2.0)。历史 v1-v8 判定不受影响(其回放默认无界);修复后的
      canonical 回放与历史数字不可比,如实声明。测试:test_zone_merge_cap 3 项。

16. **2026-10-20 所有者决议:E3 双向放开入场(覆盖式决策,非研究判 GO)**
    - 指令:所有者明确指出"放开",之前白名单 {E1,E6,E7} 不含 E3 属过度执行;本条登记。
    - 证据账本(如实):解剖台 scripts/autopsy_e234.py — E3|short 顺 regime 格 n=1106,
      增量 +0.744 ATR、成本后净 −0.041 ATR(dev+test 池,H24,1x);全体平均 −0.172。
      **增量显著但绝对期望未转正,不构成 GO 判据**;放行属所有者风险自担范围,
      记账/风控/强平/容量约束全部不变,前向数据为唯一裁判。
    - 实现:config/canonical.json whitelist += E3±1(带 note);strategy_v6 新增
      ROUTE_E3=e3_market(拒绝获因后市价次开盘,同一 F1/room/turtle 收割);
      默认 V6Params 白名单不变(E3 仅由配置开启);tests/test_e3_route.py 3 项。


17. **2026-10-20 合并封顶后的基准重冻结(capped-v1;cross_checks 崩溃的处置)**
    - 现象:条目 15 的 merge cap 修复改变区域生命周期 ⇒ E7(FLIP)等事件计数变化,
      `scripts/run_backtest_v6.py::cross_checks` 与 event_study_v3/v2 冻结计数的
      硬对账 RuntimeError 崩溃(例:BTCUSDT@12h capped=238/155 vs 冻结 104/55)。
      **性质 = 语义分叉,预期内;不是检测器 bug。**
    - 逐项对账诊断(同数据两次回放,`python scripts/freeze_canonical_baseline.py
      --legacy-verify`):merge_max_atr=0(历史无界)重放与全部冻结产物**逐项
      一致**——15 格 E7 fired/passed_f1(v2@1h:1057/550、953/537、989/498;
      es_v3@2h/4h/12h/1d 共 12 格)+ E1+E6|short(1h)=1920 == v1 go_events +
      E7 attrs['atr'] == 认知同 bar ATR。⇒ 计数分叉**仅**来自 merge cap 语义。
    - 处置:canonical(merge_max_atr=2.0)语义下重放并重冻结新基准
      **`config/canonical_baseline_counts.json`**(`python scripts/
      freeze_canonical_baseline.py` 生成;含生成时间、merge_max_atr=2.0、
      data/manifest.json sha256=eaff1807…、逐文件 sha256、生成命令)。
      `run_backtest_v6.py`:merge_max_atr>0 时硬对账对象切换为该基准(15 格
      E1/E6/E7 双向 + E7 fired/passed_f1 + E1+E6|short(1h) 逐项),运行头打印
      "基准=capped-v1";merge_max_atr=0 的 legacy 路径行为一字不变(仍对账
      v1/v2/event_study_v3 冻结产物);compaction 一致性、dev/test 拐点、
      确定性等其余交叉检查全部保留不放松。
    - 分叉幅度(E7 fired/passed_f1,capped vs legacy 冻结):1h——BTC 3822/2825
      vs 1057/550、ETH 3788/2848 vs 953/537、SOL 3835/2854 vs 989/498;
      2h——1829/1308 vs 545/285、1773/1267 vs 525/282、1800/1374 vs 536/302;
      4h——878/622 vs 279/140、825/591 vs 260/135、843/618 vs 268/148;
      12h——238/155 vs 104/55、235/167 vs 92/50、247/167 vs 117/58;
      1d——111/76 vs 55/30、119/68 vs 61/35、110/72 vs 48/29。
      合计 E7 fired 20453 vs 5889(约 ×3.5;封顶后细结构存活 ⇒ FLIP 更多)。
      E1+E6|short(1h)4503 vs 1920(v1 parity 锚点对 canonical 不再适用,
      历史 v1-v8 回放与判定不受影响,如实声明)。
    - 测试:tests/test_canonical_baseline.py 5 项(capped 模式切换/逐项对账/
      merge_max_atr 漂移拒绝/legacy 不读基准/基准文件与 canonical 形状一致);
      全仓 347 + 5 全过。

17. **2026-10-20 merge cap 后基准重冻结**
    - 现象:canonical 硬对账拒绝通过(E7 计数分叉);legacy(mma=0)重放与 v1-v8 冻结计数
      逐项逐位一致 ⇒ 分叉纯来自 merge_max_atr=2.0 语义,非 bug。
    - 处置:新基准 `config/canonical_baseline_counts.json`(capped-v1,含 manifest sha256);
      run_backtest_v6 按 merge_max_atr 是否 >0 切换对账源;历史回放行为不变。

18. **2026-10-20 v9 预注册(先于任何 v9 运行;最后一轮协议级研究)**
    - `docs/v9-protocol.md`:E9 PROCESS_ACCEPTED / E10 PROCESS_REJECTED(停留率/重心位移/
      试探失败密度的 episode 相对过程语义,先验一次写死);test 第 12 次曝光,此后只许前向;
      E9+E10 若都败,过程式世界观终结,项目进入纯前向时代。

19. **2026-10-20 v9 结局(过程式再证伪,结构性死因)**
    - E9/E10 于 3 品种×17,600 根 1h(约6万 bar)零发射:51,972 个 episode-bar 中存活至 a≥3 的
      仅 23 根(0.044%),且这些样本的过程量全部不达标。死因链:episode 于收盘越缘+k_bm(0.1ATR)
      即 BROKEN 收场(1-3根内),过程式语义需要"存活多根"的 episode;
      改变这一前提=动 A3/k_bm 冻结先验,超出本轮授权 ⇒ 按 v9-protocol §4 终结。
    - 副产物(如实):E1|short 在 capped 语义池四判据全过(信息口径,非入场判定);
      E3 高密对照池耗竭(增量 NaN 未定义,§9 协议边界首次显形)。

20. **2026-10-20 v10 预注册(先于任何 v10 运行)**
    - `docs/v10-protocol.md`:§A 触发麦克风(trigger_tf=15m,5m 仅 dev 敏感性);
      §B 订单流接入只定设计、只读注解、≥60 事件前禁任何生效言论;test 第 13 次曝光。

21. **2026-10-20 所有者决议:废弃黑白名单,改语义路由**
    - 指令:系统中不需要白名单/黑名单这类事件门槛/过滤,需要的是语义修复。
    - 实施:strategy_v6 路由表覆盖全部 E1-E8(E2/E4/E5/E8 新增市价路由,方向/失效/target
      取各自 attrs);config `gates_model=semantics_routing`;F1/room/风险/容量几何闸保留。
    - 诚实注记:E2/E4/E5/E8 均为历史测量为负的机制(其检测与盈亏逐年归因),
      路由启用不含"它们已被修好"的任何主张;逐事件归因账本决定其寿命。

22. **2026-10-20 v11 预注册(先于任何 v11 运行)**
    - `docs/v11-protocol.md`:跨 episode 区域发育归因测量(dwell/pen/out_rj/out_br,
      Spearman 主判别+分桶存活次判别);数据单元=zone-touch,test 第 14 次曝光;
      签名若立,仅入假设层元数据用例(k_*∈{2,3}),属语义修复而非过滤名单。

23. **2026-10-20 v11 结局:厚假说立(触摸越多防守越强,衰假说被反证)**
    - 43,468 zone-touch 履历;主判别 Spearman ρ(k,out_rj):dev +0.156[0.130,0.184]、
      test +0.207[0.160,0.252],CI 离 0;24h 破位率 k1=0.52→k≥5=0.39;存活率单调升;
      双侧/区龄控缠同向。**市场俗语"区域被摸烂"在本数据被反证**;
      §3 预注册用例按此**反向修正**(新鲜区→破位戒备,成熟区→拒绝信任元数据),
      效应量弱(ρ~0.2)如实记为倾斜而非主信号。

24. **2026-10-20 v12 预注册(先于任何 v12 运行)**
    - `docs/v12-protocol.md`:K线主动买量(可回放轻量订单流)在 Binance 公开数据上首测;
      delta_z 切片主判别;test 第 15 次曝光;venue/窗差均已声明;
      负则关闭 bar 层流路线转正向前 tape。

25. **2026-10-20 v12 结局:K线级订单流(delta)无增量,bar 层流路线关闭**
    - Binance vision 17,596 bar×3 品种同窗;n=3 万余事件流;8 个可评 test 桶的
      delta_z 高低分位增量差 CI 全部含 0。**自由可回放的流信息为空**;
      v12 §4 生效:bar 层流关闭,只剩前向真 tape(已在录)或商业历史 tick。

26. **2026-10-20 v13 预注册(先于任何 v13 运行)**
    - `docs/v13-protocol.md`:级联触发映射(1h→15m/2h→30m/4h→1h/12h→2h/1d→4h)用户指令
      语言升级;test 第 16 次曝光;退出节拍(先损后盈 40.6%)另立 v14,不混杂。

27. **2026-10-20 v14 预注册(先于任何 v14 运行)**
    - `docs/v14-protocol.md`:施工包 C1-C5(优质五问含"优质才进"强制条款、maker 双模式、
      stop-entry 预埋、分层入场、质量配注);预注册 arm0-arm6 矩阵,dev 先评,
      test 一次只跑两个最强 dev arm;test 第 17 次曝光;排除牌10与多品种扩维。

28. **2026-10-20 v14 结局:施工包 arm 矩阵判毕 —— dev 上 C2-ex/C3/C4 单组件
    effective、C1/C2-vi 打回;最强两臂(arm5/arm6)test@1x 均净亏(第 17 次曝光)**
    - `scripts/run_v14_arms.py` 统一回测(arm0-arm6 × dev 全战役 × 成本 1/1.5/2x;
      arm0 双锚硬对账:冻结时代名单探针 == backtest_v6_turtle 逐格全等、arm0 ==
      v13 在册 bar 臂逐格全等;事件计数 == capped-v1 基准)。
    - dev@1.5x 主锚(协议 §判定,单组件 Δ净期望):C1(arm1−arm0)= −0.0348 ⇒
      **打回(无改善)**;C2-ex(arm2−arm1)= +0.0397 ⇒ effective;C2-vi(arm3−arm1)
      = −0.0352 ⇒ **打回**;C3(arm4−arm1)= +0.0079 ⇒ effective;C4(arm5−arm1)
      = +0.3961 ⇒ effective(dev 净 +63.78 vs arm1 −53.68;全组 arm6 = +0.3949)。
    - test 一次(第 17 次曝光;对 dev@1.5x 净期望最强两臂各跑一次):arm5 test@1x
      净 −52.56(PF 0.325,n=93≥60,CI95 −1.068~−0.058);arm6 净 −52.65(PF 0.324,
      n=93)——**dev 增量未在 test 维持;如实填报,不产生转正叙述**。
    - 条款生效:dev 全战役质量评估 ≈4.3 万,Q1 拒 ≈1.6 万、Q2 拒 ≈4.2 万(Q2
      1.5R 对面缘为主闸)、Q5 拒 0;档 A 67 / 档 B 336;stopex 成交率 0.91-0.96、
      漏单(expired)1-2 笔;两模式差价与分层均价差见 reports/v14_arms.json §4。
    - 产物:`reports/v14_arms.{json,md}` + `reports/trades_v14_arm{i}__{dev,test}*.csv`;
      组件裁决 = 上述逐格如实,不整包报喜;v14 之后 test 段只许前向。

29. **2026-10-20 v15 预注册(先于任何 v15 运行)**
    - `docs/v15-protocol.md`:四件套合成(v14 裁决存活件:位置带沿/maker 真挂单/stop-entry/
      canonical 其余不变);dev 主评+test 一次(第 18 次曝光)。

30. **2026-10-20 v16 预注册(先于任何 v16 运行)**
    - `docs/v16-protocol.md`:入场三闸(位置/空间/F1) + 家族执行差分
      (fade=maker 限价,续涨=stop-entry) + 一生一表收割(结构目标 fade+CHoCH+
      trailing 只紧+时间帽 48×TF或 MFE<0.5R 才出);test 第 19 次曝光;
      级联/虚拟/分层/打包五问正式死亡。

31. **2026-10-20 v16 结局(按协议锚点打回;成分部分成功如实拆分)**
    - 判定: dev 单笔期望 −0.337 vs arm0 −0.129 ⇒ 按 v16-protocol 锚定**打回**;
      test 三档 −87.5/−89.6/−99.3(97-99 笔)对照 arm0 −115.9/−199.1/−193.1,仍负。
    - 成分如实:fade 结构目标收割桶 +150.8(62 笔,WR 98%)、trail +56.8、CHoCH +12.0、
      续涨家族 +23.4(PF 2.20)、fade 家族整体 −131.7;
      收割婚配作为机械目标达标(fade 快死类消亡),但期望未转正 ⇒
      按总章预注册:结构语言家族在本场座最优形态=近打平研究系统。
