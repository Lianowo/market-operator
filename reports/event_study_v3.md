# v3 事件研究报告(docs/v3-protocol.md:多尺度 × E7±1 + F1)

生成时间(UTC):2026-10-19T07:03:38+00:00

## 0. 协议与诚实性声明

- 预注册家族 = 1 个,最多 8 格子:TF ∈ {2h,4h,12h,1d} × {E7|short,E7|long};**所有格子数字完整公布,整体判断**(§2)。
- 机制 = config/v2.json whitelist_v2 + F1(k_is=0.8 先验);E7 发射 enable_e7=True;全部 ATR/结构/成本先验零改动。
- 评估 = methodology §9 全流程原样:H ∈ [6, 12, 24, 48] 根(主视界 H=24),对照 42 固定种子(同品种/同 regime/同方向/避同机制事件窗口 24 根),日块 bootstrap 2000 次 95% CI,成本压力 1x/1.5x/2x,§9.3 四判据,**增量口径**与 v1/v2 一致。
- **重评声明(§9.4/v3-protocol §4)**:test 段第 5 次评估(v3 唯一一次;此后任何迭代不得再用 test;research-log 条目 7)。本报告为该次评估产物。
- 尺度语义(v3-protocol §1/§2):H/N_time/EVENT_EXCL_BARS 以根数计(bars 语义不变,v3-protocol §1);funding 按持仓真实时长计费 = horizon·bar_hours 小时(research bar_hours 口径,1h 恒等,MULTI_TF 修复)。
- dev/test 拐点(v3-protocol §2):同一拐点 = 1h 索引 12320 的 ts(每品种);逐 (symbol, TF) 断言聚合帧 split_boundary 索引桶起点 = 该 ts 的桶起点(全部通过,tests/test_aggregate.py 另有硬编码核算:2h→6160,4h→3080,12h→1027,1d→514)。

## 1. GO / NO-GO 家族判定表(test 段,H=24,判据同 §9.3;8 格完整公布)

| TF | 机制 | n(test) | 判1 n≥60 | 判2 增量>0@1.5x | 判3 CI下界>0@1x | 判4 ≥2品种正 | 判定 |
|---|---|---|---|---|---|---|---|
| 2h | E7|long | 42 | ✗ | ✗ | ✗ | ✓ | **INSUFFICIENT_DATA** |
| 2h | E7|short | 172 | ✓ | ✗ | ✗ | ✗ | **NO-GO** |
| 4h | E7|long | 26 | ✗ | ✗ | ✗ | ✗ | **INSUFFICIENT_DATA** |
| 4h | E7|short | 67 | ✓ | ✓ | ✗ | ✓ | **NO-GO** |
| 12h | E7|long | 15 | ✗ | ✗ | ✗ | ✗ | **INSUFFICIENT_DATA** |
| 12h | E7|short | 24 | ✗ | ✓ | ✓ | ✓ | **INSUFFICIENT_DATA** |
| 1d | E7|long | 6 | ✗ | ✗ | ✗ | ✗ | **INSUFFICIENT_DATA** |
| 1d | E7|short | 12 | ✗ | ✓ | ✓ | ✓ | **INSUFFICIENT_DATA** |

## 2-2h. 2h 明细(bar_hours=2.0)

### 发射与过 F1(dev/test 分界每品种前 70%)

- BTCUSDT:dev_end = 2026-03-13T04:00:00+00:00 (index 6160 / 8800);E7 fired 545(dev 405 / test 140),过 F1 285(dev 213 / test 72)
- ETHUSDT:dev_end = 2026-03-13T04:00:00+00:00 (index 6160 / 8800);E7 fired 525(dev 391 / test 134),过 F1 282(dev 216 / test 66)
- SOLUSDT:dev_end = 2026-03-13T04:00:00+00:00 (index 6160 / 8800);E7 fired 536(dev 396 / test 140),过 F1 302(dev 222 / test 80)

### 机制汇总(test 段,H=24;ATR 单位)

| 机制 | n | 事件净@1x (CI95) | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量@2x |
|---|---|---|---|---|---|---|
| E7|long | 42 | -0.1537 [-1.1642, 0.8665] | 0.2477 | -0.4014 [-1.4453, 0.6282] | -0.3827 | -0.3640 |
| E7|short | 172 | -0.9552 [-1.8424, -0.1853] | -0.7490 | -0.2062 [-1.1152, 0.5772] | -0.1990 | -0.1917 |

### 全视界增量(test 段,增量@1x ATR)与分品种符号

| 机制 | H=6 | H=12 | H=24 | H=48 | n_pos(品种) |
|---|---|---|---|---|---|
| E7|long | 0.0980 | -0.2476 | -0.4014 | 0.6114 | 2 |
| E7|short | 0.1867 | 0.0468 | -0.2062 | -0.4428 | 1 |

### dev↔test 一致性(诚实性参考;判据只依 test)

| 机制 | dev n | dev 增量@1x | test 增量@1x | 符号一致 |
|---|---|---|---|---|
| E7|long | 304 | 0.5136 | -0.4014 | ✗ |
| E7|short | 347 | -0.8145 | -0.2062 | ✓ |

## 2-4h. 4h 明细(bar_hours=4.0)

### 发射与过 F1(dev/test 分界每品种前 70%)

- BTCUSDT:dev_end = 2026-03-13T04:00:00+00:00 (index 3080 / 4400);E7 fired 279(dev 205 / test 74),过 F1 140(dev 103 / test 37)
- ETHUSDT:dev_end = 2026-03-13T04:00:00+00:00 (index 3080 / 4400);E7 fired 260(dev 211 / test 49),过 F1 135(dev 112 / test 23)
- SOLUSDT:dev_end = 2026-03-13T04:00:00+00:00 (index 3080 / 4400);E7 fired 268(dev 202 / test 66),过 F1 148(dev 110 / test 38)

### 机制汇总(test 段,H=24;ATR 单位)

| 机制 | n | 事件净@1x (CI95) | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量@2x |
|---|---|---|---|---|---|---|
| E7|long | 26 | 0.2406 [-0.8246, 1.3582] | 0.5717 | -0.3311 [-1.4299, 0.8566] | -0.3136 | -0.2961 |
| E7|short | 67 | -0.3457 [-1.1791, 0.4282] | -0.9960 | 0.6503 [-0.2095, 1.4425] | 0.6761 | 0.7019 |

### 全视界增量(test 段,增量@1x ATR)与分品种符号

| 机制 | H=6 | H=12 | H=24 | H=48 | n_pos(品种) |
|---|---|---|---|---|---|
| E7|long | -0.2515 | -0.5881 | -0.3311 | -2.3109 | 1 |
| E7|short | 0.0409 | 0.1573 | 0.6503 | 1.3693 | 3 |

### dev↔test 一致性(诚实性参考;判据只依 test)

| 机制 | dev n | dev 增量@1x | test 增量@1x | 符号一致 |
|---|---|---|---|---|
| E7|long | 168 | -0.5700 | -0.3311 | ✓ |
| E7|short | 157 | -0.8591 | 0.6503 | ✗ |

## 2-12h. 12h 明细(bar_hours=12.0)

### 发射与过 F1(dev/test 分界每品种前 70%)

- BTCUSDT:dev_end = 2026-03-13T00:00:00+00:00 (index 1027 / 1468);E7 fired 104(dev 77 / test 27),过 F1 55(dev 40 / test 15)
- ETHUSDT:dev_end = 2026-03-13T00:00:00+00:00 (index 1027 / 1468);E7 fired 92(dev 79 / test 13),过 F1 50(dev 42 / test 8)
- SOLUSDT:dev_end = 2026-03-13T00:00:00+00:00 (index 1027 / 1468);E7 fired 117(dev 88 / test 29),过 F1 58(dev 39 / test 19)

### 机制汇总(test 段,H=24;ATR 单位)

| 机制 | n | 事件净@1x (CI95) | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量@2x |
|---|---|---|---|---|---|---|
| E7|long | 15 | -0.4702 [-1.3615, 0.4944] | 1.6006 | -2.0708 [-3.2333, -0.8781] | -2.0360 | -2.0012 |
| E7|short | 24 | -0.5486 [-1.6067, 0.5723] | -2.2411 | 1.6925 [0.3691, 2.9240] | 1.7358 | 1.7790 |

### 全视界增量(test 段,增量@1x ATR)与分品种符号

| 机制 | H=6 | H=12 | H=24 | H=48 | n_pos(品种) |
|---|---|---|---|---|---|
| E7|long | -0.7668 | -0.7621 | -2.0708 | -3.2566 | 0 |
| E7|short | 0.5062 | 1.2093 | 1.6925 | 2.5914 | 2 |

### dev↔test 一致性(诚实性参考;判据只依 test)

| 机制 | dev n | dev 增量@1x | test 增量@1x | 符号一致 |
|---|---|---|---|---|
| E7|long | 75 | 0.5945 | -2.0708 | ✗ |
| E7|short | 46 | -1.1936 | 1.6925 | ✗ |

## 2-1d. 1d 明细(bar_hours=24.0)

### 发射与过 F1(dev/test 分界每品种前 70%)

- BTCUSDT:dev_end = 2026-03-13T00:00:00+00:00 (index 514 / 735);E7 fired 55(dev 44 / test 11),过 F1 30(dev 22 / test 8)
- ETHUSDT:dev_end = 2026-03-13T00:00:00+00:00 (index 514 / 735);E7 fired 61(dev 54 / test 7),过 F1 35(dev 30 / test 5)
- SOLUSDT:dev_end = 2026-03-13T00:00:00+00:00 (index 514 / 735);E7 fired 48(dev 35 / test 13),过 F1 29(dev 20 / test 9)

### 机制汇总(test 段,H=24;ATR 单位)

| 机制 | n | 事件净@1x (CI95) | 对照净@1x | 增量@1x (CI95) | 增量@1.5x | 增量@2x |
|---|---|---|---|---|---|---|
| E7|long | 6 | -1.6559 [-4.6564, 0.4884] | 2.7017 | -4.3576 [-8.2615, -1.2609] | -4.3248 | -4.2920 |
| E7|short | 12 | 0.6417 [-0.7077, 2.0608] | -1.2699 | 1.9115 [0.2762, 3.4385] | 1.9465 | 1.9815 |

### 全视界增量(test 段,增量@1x ATR)与分品种符号

| 机制 | H=6 | H=12 | H=24 | H=48 | n_pos(品种) |
|---|---|---|---|---|---|
| E7|long | -2.1050 | -3.9576 | -4.3576 | -6.2525 | 0 |
| E7|short | 0.8012 | 1.3285 | 1.9115 | 1.4255 | 3 |

### dev↔test 一致性(诚实性参考;判据只依 test)

| 机制 | dev n | dev 增量@1x | test 增量@1x | 符号一致 |
|---|---|---|---|---|
| E7|long | 40 | -1.9293 | -4.3576 | ✓ |
| E7|short | 32 | -1.1012 | 1.9115 | ✗ |

## 3. 产物索引

- 2h:逐事件 `reports/reports\event_study_events_v3_2h_dev.csv`, `reports/reports\event_study_events_v3_2h_test.csv`
- 4h:逐事件 `reports/reports\event_study_events_v3_4h_dev.csv`, `reports/reports\event_study_events_v3_4h_test.csv`
- 12h:逐事件 `reports/reports\event_study_events_v3_12h_dev.csv`, `reports/reports\event_study_events_v3_12h_test.csv`
- 1d:逐事件 `reports/reports\event_study_events_v3_1d_dev.csv`, `reports/reports\event_study_events_v3_1d_test.csv`

机器可读全量:`reports/event_study_v3.json`(逐 TF × 机制 × 段 × 成本 × 视界,含 regime 分解与稳定性)。
