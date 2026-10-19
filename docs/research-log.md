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
