# aggTrades 接触点检验报告(docs/v19-protocol-orderflow.md,测量协议)

生成时间(UTC):2026-10-21T01:34:24+00:00

## 0. 协议与诚实性声明

- 场所:Binance USDⓈ-M 永续(data.binance.vision futures/um daily aggTrades)≠ Bitget;本研究只回答'接触点 tape 特征有没有增量'。
- 数据窗:2026-07-22 .. 2026-10-19 (90 天日档);认知帧 = Binance 1h(同窗 + 180d warm-up,fetch_binance_klines 实现,manifest 追记)。
- 接触点:E1/E2/E3/E6/E7 known bar 收盘 = known_ts + 3_600_000(enable_e7=True);认知 = canonical 重放(config/canonical.json(merge_max_atr=2.0 断言通过))。
- 判别只表述度量:只产'增量有/无+哪特征+CI';无任何策略语义;多重性:H1 50 单元族(10 机制×5 特征)家族-wise 未校正;H2 两亚假设取交(保守);均如实声明。
- H1 规则:单元 = 机制×5 特征;三瓜分 top−bottom diff(incr@1x H24);日块 bootstrap 2000 固定种子;dev diff>0 且 CI 不含 0 推进,test 同线判别,每维度 test 一次;n<50 INSUFFICIENT。
- H2 规则:吸收 = vol15_rel ≥ dev q70 且 eff_15m ≤ dev q30(E2/E3,阈 dev 定后冻结);H2a vs 对照均值>0 且 CI 不含 0,H2b vs 非吸收差>0 且 CI 不含 0;两个都过才算增量存在;n<50 INSUFFICIENT。
- 种子/次数:对照 42 种子(SEED_BASE=20261019),block bootstrap ×2000 固定种子;对照 = 同品种/regime/方向、避同机制 24 根、认知帧同段随机 bar(与 §9 measure_controls 同引擎,单机校验见 §5)。

## 1. 数据与接触点统计

| 品种 | bars | 接触点(dev/test) | H24 尾部丢弃 | vol15_rel NaN(warmup) | 无对照(n_ctrl=0) | 磁带行数 | CHECKSUM |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 6505 | 3465(2503/962) | 0 | 11 | 1396 | 100,311,157 | verified |
| ETHUSDT | 6505 | 3233(2229/1004) | 0 | 21 | 1044 | 95,890,494 | verified |
| SOLUSDT | 6505 | 3341(2353/988) | 0 | 18 | 1443 | 23,829,246 | verified |

逐事件×段计数见 reports/aggtrades.json symbols.*.per_event_segment;接触点明细(压缩)见 §6。

## 2. H1 判别(tape 特征分位切片 ↔ 后续 24h 净期望;incr@1x ATR)

| 机制 × 特征 | dev | test(仅 dev 推进单元,各评一次) |
|---|---|---|
| E1|long||s_delta_share_15m | n=432 diff=2.2547 CI[-2.2349,6.0751] | —(未进 test) |
| E1|long||s_delta_share_60m | n=432 diff=2.8262 CI[-1.2235,6.9609] | —(未进 test) |
| E1|long||s_cvd_slope_60m | n=432 diff=3.1692 CI[-2.1193,7.3336] | —(未进 test) |
| E1|long||big_share_15m | n=432 diff=-2.9234 CI[-6.8315,3.1876] | —(未进 test) |
| E1|long||eff_15m | n=432 diff=-0.9268 CI[-4.7681,2.8172] | —(未进 test) |
| E1|short||s_delta_share_15m | n=236 diff=0.2557 CI[-1.8811,2.2926] | —(未进 test) |
| E1|short||s_delta_share_60m | n=236 diff=1.1032 CI[-0.8502,2.9715] | —(未进 test) |
| E1|short||s_cvd_slope_60m | n=236 diff=0.8878 CI[-1.0217,2.6621] | —(未进 test) |
| E1|short||big_share_15m | n=236 diff=-0.7128 CI[-2.1237,0.7825] | —(未进 test) |
| E1|short||eff_15m | n=236 diff=-0.4468 CI[-2.1193,1.2479] | —(未进 test) |
| E2|long||s_delta_share_15m | n=190 diff=0.2348 CI[-1.0895,1.5702] | —(未进 test) |
| E2|long||s_delta_share_60m | n=190 diff=-0.8126 CI[-2.4165,0.7842] | —(未进 test) |
| E2|long||s_cvd_slope_60m | n=190 diff=-0.3460 CI[-1.8403,1.0184] | —(未进 test) |
| E2|long||big_share_15m | n=190 diff=0.1243 CI[-1.2279,1.5289] | —(未进 test) |
| E2|long||eff_15m | n=190 diff=0.3637 CI[-1.1250,1.9428] | —(未进 test) |
| E2|short||s_delta_share_15m | n=185 diff=1.7911 CI[-1.9693,4.7959] | —(未进 test) |
| E2|short||s_delta_share_60m | n=185 diff=1.9848 CI[-2.4462,5.4200] | —(未进 test) |
| E2|short||s_cvd_slope_60m | n=185 diff=2.4797 CI[-0.7076,5.3760] | —(未进 test) |
| E2|short||big_share_15m | n=185 diff=-0.5528 CI[-4.7511,3.3457] | —(未进 test) |
| E2|short||eff_15m | n=185 diff=-0.0627 CI[-3.7640,3.2796] | —(未进 test) |
| E3|long||s_delta_share_15m | n=1443 diff=0.1807 CI[-2.3123,2.5396] | —(未进 test) |
| E3|long||s_delta_share_60m | n=1443 diff=0.5494 CI[-1.3145,2.8925] | —(未进 test) |
| E3|long||s_cvd_slope_60m | n=1443 diff=-0.0039 CI[-1.5802,1.9931] | —(未进 test) |
| E3|long||big_share_15m | n=1443 diff=1.2516 CI[-0.8263,3.1028] | —(未进 test) |
| E3|long||eff_15m | n=1443 diff=2.3096 CI[0.3190,4.3073] **PASS** | INSUFFICIENT_DATA |
| E3|short||s_delta_share_15m | n=1636 diff=0.6512 CI[-2.0824,3.4205] | —(未进 test) |
| E3|short||s_delta_share_60m | n=1636 diff=1.5739 CI[-1.1849,4.8727] | —(未进 test) |
| E3|short||s_cvd_slope_60m | n=1636 diff=2.0708 CI[-1.1842,6.2670] | —(未进 test) |
| E3|short||big_share_15m | n=1636 diff=0.9510 CI[-2.1647,4.3650] | —(未进 test) |
| E3|short||eff_15m | n=1636 diff=-0.4583 CI[-3.7691,2.3629] | —(未进 test) |
| E6|long||s_delta_share_15m | n=70 diff=-0.9935 CI[-10.1380,6.5524] | —(未进 test) |
| E6|long||s_delta_share_60m | n=70 diff=2.5255 CI[-3.4315,8.1356] | —(未进 test) |
| E6|long||s_cvd_slope_60m | n=70 diff=2.5014 CI[-0.5556,7.7181] | —(未进 test) |
| E6|long||big_share_15m | n=70 diff=-2.5990 CI[-10.8134,4.4662] | —(未进 test) |
| E6|long||eff_15m | n=70 diff=2.6353 CI[-0.3473,6.4305] | —(未进 test) |
| E6|short||s_delta_share_15m | n=82 diff=-0.9439 CI[-2.7972,1.2519] | —(未进 test) |
| E6|short||s_delta_share_60m | n=82 diff=-0.6561 CI[-2.5921,1.5721] | —(未进 test) |
| E6|short||s_cvd_slope_60m | n=82 diff=1.5546 CI[-0.7276,4.5308] | —(未进 test) |
| E6|short||big_share_15m | n=82 diff=-1.0950 CI[-3.0845,0.8817] | —(未进 test) |
| E6|short||eff_15m | n=82 diff=-1.4559 CI[-4.0818,0.7354] | —(未进 test) |
| E7|long||s_delta_share_15m | n=485 diff=1.2765 CI[-1.6217,4.6821] | —(未进 test) |
| E7|long||s_delta_share_60m | n=485 diff=2.1454 CI[-1.3898,6.3510] | —(未进 test) |
| E7|long||s_cvd_slope_60m | n=485 diff=0.8555 CI[-2.7188,5.1985] | —(未进 test) |
| E7|long||big_share_15m | n=485 diff=-0.6724 CI[-4.1308,3.1469] | —(未进 test) |
| E7|long||eff_15m | n=485 diff=-2.3337 CI[-5.5399,0.9214] | —(未进 test) |
| E7|short||s_delta_share_15m | n=441 diff=-0.6001 CI[-2.1999,0.9864] | —(未进 test) |
| E7|short||s_delta_share_60m | n=441 diff=0.8477 CI[-0.8522,2.4395] | —(未进 test) |
| E7|short||s_cvd_slope_60m | n=441 diff=0.4765 CI[-0.7408,1.5604] | —(未进 test) |
| E7|short||big_share_15m | n=441 diff=-1.0402 CI[-3.0372,0.6110] | —(未进 test) |
| E7|short||eff_15m | n=441 diff=-1.3297 CI[-3.2963,0.2470] | —(未进 test) |

**H1 判别链**:dev 推进 1/50 单元;test 过线 **0** 个(无)⇒ **no_increment**:全部 test 单元未过线 ⇒ 接触点 tape 特征无增量(H0 站住)。

±15m/60m 档敏感性(描述性,不另开判别线):15m 档过线 无;60m 档过线 无。

## 3. H2 吸收签名(高 vol15_rel + 低 eff_15m 拒绝接触 ⇒ 拒绝残增厚)

- 阈(dev 60d 拒绝接触计算后冻结):vol15_rel ≥ **1.6452**(q0.7) 且 eff_15m ≤ **1.60e-08**(q0.3)。
- 样本:dev 拒绝接触 3421(吸收 376,dev 吸收残幅均值 -0.2517);test 拒绝接触 167(吸收 28 / 非吸收 139)。

判别人口构成(§9 对照池口径下 incr 定义的接触数/总接触数):

| 段 | 事件 | total | incr 有效 |
|---|---|---|---|
| dev | E2 | 375 | 375 |
| dev | E3 | 4964 | 3081 |
| test | E2 | 167 | 167 |
| test | E3 | 2000 | 0 |

**H2 判别:INSUFFICIENT_DATA** — test 吸收/非吸收样本 28/139 < 50(协议 §4,一律;判别人口构成见 population_composition)

参考值 — 以下数字仅为参考(协议 §4 样本线未过,不作判别,不进入任何增量判定):H2a mean = -0.0204 CI95 [-0.7679, 0.8195];H2b diff = -0.0865 CI95 [-1.0806, 0.8845](非吸收均值 0.0661)。

## 4. §3 参照基线(接触 vs 对照特征均值,描述性)

| 品种 | 特征 | 接触 n/均值/中位 | 对照 n/均值/中位 |
|---|---|---|---|
| BTCUSDT | delta_share_15m | 3464/-0.0064/-0.0041 | 23706/-0.0271/-0.0144 |
| BTCUSDT | delta_share_60m | 3464/-0.0060/-0.0105 | 23706/-0.0063/-0.0068 |
| BTCUSDT | cvd_slope_60m | 3464/-0.0000/-0.0000 | 23706/-0.0000/-0.0000 |
| BTCUSDT | big_share_15m | 3464/0.8704/0.8725 | 23706/0.8671/0.8702 |
| BTCUSDT | eff_15m | 3464/0.0000/0.0000 | 23706/0.0000/0.0000 |
| BTCUSDT | vol15_rel | 3454/2.0056/1.2572 | 23705/1.4925/0.8556 |
| ETHUSDT | delta_share_15m | 3231/-0.0047/-0.0048 | 34200/-0.0099/-0.0355 |
| ETHUSDT | delta_share_60m | 3231/-0.0069/-0.0043 | 34200/-0.0286/-0.0332 |
| ETHUSDT | cvd_slope_60m | 3231/-0.0000/-0.0000 | 34200/-0.0000/-0.0000 |
| ETHUSDT | big_share_15m | 3231/0.8378/0.8391 | 34200/0.8348/0.8362 |
| ETHUSDT | eff_15m | 3231/0.0000/0.0000 | 34200/0.0000/0.0000 |
| ETHUSDT | vol15_rel | 3212/2.1593/1.1895 | 34135/1.1440/0.7714 |
| SOLUSDT | delta_share_15m | 3340/-0.0196/-0.0214 | 20792/-0.0282/-0.0205 |
| SOLUSDT | delta_share_60m | 3340/-0.0137/-0.0130 | 20792/-0.0090/-0.0140 |
| SOLUSDT | cvd_slope_60m | 3340/-0.0001/-0.0002 | 20792/0.0000/-0.0002 |
| SOLUSDT | big_share_15m | 3340/0.8200/0.8246 | 20792/0.8081/0.8150 |
| SOLUSDT | eff_15m | 3340/0.0000/0.0000 | 20792/0.0000/0.0000 |
| SOLUSDT | vol15_rel | 3323/1.6395/1.0559 | 20783/1.4099/1.0295 |

## 5. 引擎校验

- BTCUSDT:ctr_draw_engine_vs_research.measure_controls ⇒ n=1743,max|diff|=0.0,passed=True
- ETHUSDT:ctr_draw_engine_vs_research.measure_controls ⇒ n=1877,max|diff|=0.0,passed=True
- SOLUSDT:ctr_draw_engine_vs_research.measure_controls ⇒ n=1582,max|diff|=0.0,passed=True

## 6. 疑似问题与硬声明

1. BTCUSDT:1396/3465 接触点对照池为空(n_ctrl=0,§9 同机制排除窗在事件高密度段的已知病理),其 incr 未定义 ⇒ 自 H1/H2 单元样本中剔除(dropna 如实反映)。
2. BTCUSDT:1 接触点 15m 窗内零成交(低流动性时段),15m 档特征 NaN ⇒ 相关单元剔除该行。
3. ETHUSDT:1044/3233 接触点对照池为空(n_ctrl=0,§9 同机制排除窗在事件高密度段的已知病理),其 incr 未定义 ⇒ 自 H1/H2 单元样本中剔除(dropna 如实反映)。
4. ETHUSDT:2 接触点 15m 窗内零成交(低流动性时段),15m 档特征 NaN ⇒ 相关单元剔除该行。
5. SOLUSDT:1443/3341 接触点对照池为空(n_ctrl=0,§9 同机制排除窗在事件高密度段的已知病理),其 incr 未定义 ⇒ 自 H1/H2 单元样本中剔除(dropna 如实反映)。
6. SOLUSDT:1 接触点 15m 窗内零成交(低流动性时段),15m 档特征 NaN ⇒ 相关单元剔除该行。
7. zip CHECKSUM 状态(各品种取值集合):{'BTCUSDT': ['verified'], 'ETHUSDT': ['verified'], 'SOLUSDT': ['verified']}
8. 结构事实:① venue = Binance ≠ Bitget(协议 §1 声明,不跨场座外推);② 多重性 = H1 50 单元族未校正 + H2 两亚假设取交(均已声明);③ tape 特征只有 trailing 窗因果口径,±60m/±15m 评价窗的后侧从未入特征(协议 §2 因果窗读法,本报告即冻结文本);④ H1 dev 推进单元数即信息维度多比一次的次数,各维度 test 只评一次(本报告即该次)。
9. 实现迭代(两跑如实并列):首跑误将对照池限制在磁带窗内(超出 §9 段口径),标题判定 = H1 no_increment(dev 推进 0/50)、H2 INSUFFICIENT_DATA(test 吸收/非吸收 26/141 < 50,拒绝人口因 E3 对照池结构性空而只剩 E2);本报告 = 按 §9 段口径(dev=[0,split),test=[split,n))还原后的一跑。E3 对照池在两种口径下均结构性饱和(密度驱动,v12 docstring 已载同病理),其 incr 大面积未定义为数据事实,非实现选择。
10. 对照池含 warm-up 段(认知帧前 180d)决策 bar(dev 池):era 差异由 regime 匹配吸收,与 v12 全帧段同例;如怀疑 era 漂移,dev/test 各自段内的一致性即为本研究的全部检验尺度,如实声明。

## 7. 产物索引

- 机器可读全量:`reports/aggtrades.json`
- 接触点明细(压缩):`reports\aggtrades_contacts.csv.gz`
