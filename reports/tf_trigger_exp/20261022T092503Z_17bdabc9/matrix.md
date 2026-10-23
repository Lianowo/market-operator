# tf_trigger_exp 实验矩阵报告(20261022T092503Z_17bdabc9)

## 审计头

| 字段 | 值 |
|---|---|
| git commit | 4017b61523ed14f5f22132a935cfc21c17efc3b0 |
| 实验配置 sha256 | 17bdabc97025f612dfc0ce184d54ce057e4375362ca7e67934a51b0b41d9e11b |
| 数据 manifest sha256 | a989d885aad56ef86ecfad91af8879985abd0ae8c4f74966b4808cc2d56d1aa6 |
| parent config sha256 | 298cfc714daa0038a94b3148309604b727e98aa6d18a3f78006dda12a50a5a60 |
| 运行时间戳 | 2026-10-22T09:25:03Z |
| 复现命令 | python scripts/run_tf_trigger_exp.py --config D:\LiAn\Web3Code\market-operator\config\experiments\tf_trigger_exp.json |
| test 曝光序号 | 本轮未消耗 |
| 零 diff 断言 | PASS |
| INSUFFICIENT 格数 | 1094 |
| 品种 | BTCUSDT, ETHUSDT, SOLUSDT |
| 数据范围 | 2024-10-16 → 2026-10-19 (UTC, 733 天) |


## 三问裁决结论

### Q1_ablation_2h:【负向】

- 结论依据:net(a1)=+194.5 < net(a0)=+280.8(取消 2h 后收益下降);被释放容量去向见分 TF 表(2h 格归零,其余 TF 净额差)
- decided_at: 2026-10-22T09:25:03Z
- config_sha256: `17bdabc97025f612dfc0ce184d54ce057e4375362ca7e67934a51b0b41d9e11b`

| arm | segment | cost | tf | year | N | net | PF | MaxDD% | suff |
|---|---|---|---|---|---|---|---|---|---|
| a1 | full | 1.0 | ALL | ALL | 679 | +194.5 | 1.27 | 29.44 | SUFFICIENT |
| a0 | full | 1.0 | ALL | ALL | 1155 | +280.8 | 1.22 | 22.55 | SUFFICIENT |


### Q2_balance_gate:【负向】

- 结论依据:增益线[False] b2_2026=+147.9 vs 1.10*b1_2026=+156.6;保持线[False] b1_2425<=0: b2=-20.6 vs b1=-19.6(不劣化);MaxDD 线[True] dd(b2)=18.4% vs dd(b1)+5pct=23.7%;错失削减线[False] missed(b2,2026)=+805.4 vs 0.70*missed(b1,2026)=+563.6;公共条件[True]
- decided_at: 2026-10-22T09:25:03Z
- config_sha256: `17bdabc97025f612dfc0ce184d54ce057e4375362ca7e67934a51b0b41d9e11b`

| arm | segment | cost | tf | year | N | net | PF | MaxDD% | suff |
|---|---|---|---|---|---|---|---|---|---|
| b2 | full | 1.0 | ALL | ALL | 681 | +127.4 | 1.20 | 18.36 | SUFFICIENT |
| b1 | full | 1.0 | ALL | ALL | 684 | +122.8 | 1.19 | 18.67 | SUFFICIENT |
| b0 | full | 1.0 | ALL | ALL | 1155 | +280.8 | 1.22 | 22.55 | SUFFICIENT |
| b2 | full | 1.0 | ALL | 2026 | 263 | +147.9 | 1.54 | 18.36 | SUFFICIENT |
| b1 | full | 1.0 | ALL | 2026 | 264 | +142.4 | 1.52 | 18.67 | SUFFICIENT |


### Q3_tf_confirm_grid:【不显著】

- 结论依据:现行 M3c + htf_atr 绑定即最优:16 格无臂同时满足 net ≥ 1.05*net(a0)、PF ≥ PF(a0)、MaxDD ≤ +5pct 与公共条件
- decided_at: 2026-10-22T09:25:03Z
- config_sha256: `17bdabc97025f612dfc0ce184d54ce057e4375362ca7e67934a51b0b41d9e11b`

| arm | segment | cost | tf | year | N | net | PF | MaxDD% | suff |
|---|---|---|---|---|---|---|---|---|---|
| a0 | full | 1.0 | ALL | ALL | 1155 | +280.8 | 1.22 | 22.55 | SUFFICIENT |
| c_t0f1 | full | 1.0 | ALL | ALL | 1242 | +241.6 | 1.18 | 25.81 | SUFFICIENT |
| c_t0f2 | full | 1.0 | ALL | ALL | 684 | +122.8 | 1.19 | 18.67 | SUFFICIENT |
| c_t0f3 | full | 1.0 | ALL | ALL | 1127 | +256.6 | 1.21 | 23.50 | SUFFICIENT |
| c_t1f0 | full | 1.0 | ALL | ALL | 1179 | +171.7 | 1.13 | 24.84 | SUFFICIENT |
| c_t1f1 | full | 1.0 | ALL | ALL | 1335 | +192.3 | 1.13 | 33.60 | SUFFICIENT |
| c_t1f2 | full | 1.0 | ALL | ALL | 1135 | +199.7 | 1.17 | 23.47 | SUFFICIENT |
| c_t1f3 | full | 1.0 | ALL | ALL | 1128 | +252.7 | 1.21 | 23.50 | SUFFICIENT |
| c_t2f0 | full | 1.0 | ALL | ALL | 1164 | +211.5 | 1.17 | 24.25 | SUFFICIENT |
| c_t2f1 | full | 1.0 | ALL | ALL | 1222 | +79.7 | 1.07 | 37.71 | SUFFICIENT |
| c_t2f2 | full | 1.0 | ALL | ALL | 452 | -40.4 | 0.90 | 28.93 | SUFFICIENT |
| c_t2f3 | full | 1.0 | ALL | ALL | 1124 | +254.8 | 1.21 | 23.45 | SUFFICIENT |
| c_t3f0 | full | 1.0 | ALL | ALL | 1173 | +150.0 | 1.11 | 27.11 | SUFFICIENT |
| c_t3f1 | full | 1.0 | ALL | ALL | 1285 | +260.5 | 1.16 | 27.83 | SUFFICIENT |
| c_t3f2 | full | 1.0 | ALL | ALL | 908 | +54.5 | 1.06 | 23.02 | SUFFICIENT |
| c_t3f3 | full | 1.0 | ALL | ALL | 1125 | +251.0 | 1.21 | 23.45 | SUFFICIENT |
| a0 | full | 1.0 | ALL | ALL | 1155 | +280.8 | 1.22 | 22.55 | SUFFICIENT |


## 矩阵总表(full 段 @ 1.0x,跨 TF 汇总)

| arm | N | net(USDT) | PF | MaxDD% | WR | gR | suff |
|---|---|---|---|---|---|---|---|
| a0 | 1155 | +280.8 | 1.22 | 22.55 | 0.369 | +0.108 | SUFFICIENT |
| a1 | 679 | +194.5 | 1.27 | 29.44 | 0.352 | +0.132 | SUFFICIENT |
| b0 | 1155 | +280.8 | 1.22 | 22.55 | 0.369 | +0.108 | SUFFICIENT |
| b1 | 684 | +122.8 | 1.19 | 18.67 | 0.360 | +0.099 | SUFFICIENT |
| b2 | 681 | +127.4 | 1.20 | 18.36 | 0.359 | +0.102 | SUFFICIENT |
| c_t0f0 | 1155 | +280.8 | 1.22 | 22.55 | 0.369 | +0.108 | SUFFICIENT |
| c_t0f1 | 1242 | +241.6 | 1.18 | 25.81 | 0.352 | +0.103 | SUFFICIENT |
| c_t0f2 | 684 | +122.8 | 1.19 | 18.67 | 0.360 | +0.099 | SUFFICIENT |
| c_t0f3 | 1127 | +256.6 | 1.21 | 23.50 | 0.381 | +0.104 | SUFFICIENT |
| c_t1f0 | 1179 | +171.7 | 1.13 | 24.84 | 0.359 | +0.084 | SUFFICIENT |
| c_t1f1 | 1335 | +192.3 | 1.13 | 33.60 | 0.347 | +0.092 | SUFFICIENT |
| c_t1f2 | 1135 | +199.7 | 1.17 | 23.47 | 0.378 | +0.092 | SUFFICIENT |
| c_t1f3 | 1128 | +252.7 | 1.21 | 23.50 | 0.380 | +0.103 | SUFFICIENT |
| c_t2f0 | 1164 | +211.5 | 1.17 | 24.25 | 0.370 | +0.090 | SUFFICIENT |
| c_t2f1 | 1222 | +79.7 | 1.07 | 37.71 | 0.355 | +0.060 | SUFFICIENT |
| c_t2f2 | 452 | -40.4 | 0.90 | 28.93 | 0.321 | -0.033 | SUFFICIENT |
| c_t2f3 | 1124 | +254.8 | 1.21 | 23.45 | 0.380 | +0.105 | SUFFICIENT |
| c_t3f0 | 1173 | +150.0 | 1.11 | 27.11 | 0.365 | +0.079 | SUFFICIENT |
| c_t3f1 | 1285 | +260.5 | 1.16 | 27.83 | 0.355 | +0.099 | SUFFICIENT |
| c_t3f2 | 908 | +54.5 | 1.06 | 23.02 | 0.361 | +0.045 | SUFFICIENT |
| c_t3f3 | 1125 | +251.0 | 1.21 | 23.45 | 0.380 | +0.104 | SUFFICIENT |


## 分年度表(full @ 1.0x,跨 TF 汇总)

| arm | 2024 | 2025 | 2026 |
|---|---|---|---|
| a0 | +28.0 | -25.2 | +278.0 |
| a1 | +26.5 | -26.6 | +194.6 |
| b0 | +28.0 | -25.2 | +278.0 |
| b1 | +4.9 | -24.5 | +142.4 |
| b2 | +4.9 | -25.4 | +147.9 |
| c_t0f0 | +28.0 | -25.2 | +278.0 |
| c_t0f1 | +12.2 | -49.9 | +279.3 |
| c_t0f2 | +4.9 | -24.5 | +142.4 |
| c_t0f3 | +13.5 | -20.8 | +263.8 |
| c_t1f0 | +36.2 | -32.7 | +168.3 |
| c_t1f1 | -9.0 | -33.2 | +234.5 |
| c_t1f2 | +13.5 | -22.8 | +209.0 |
| c_t1f3 | +13.5 | -20.8 | +259.9 |
| c_t2f0 | +28.1 | -21.6 | +205.1 |
| c_t2f1 | -0.7 | -86.8 | +167.2 |
| c_t2f2 | -3.1 | -67.3 | +30.0 |
| c_t2f3 | +13.5 | -18.2 | +259.4 |
| c_t3f0 | +36.3 | -22.2 | +135.9 |
| c_t3f1 | -9.8 | +14.1 | +256.2 |
| c_t3f2 | +11.0 | -36.0 | +79.4 |
| c_t3f3 | +13.5 | -18.2 | +255.6 |


## 分 TF 表(full @ 1.0x)

| arm | tf | N | net(USDT) | PF | suff |
|---|---|---|---|---|---|
| a0 | 12h | 310 | +5.5 | 1.01 | SUFFICIENT |
| a0 | 1d | 108 | +35.4 | 1.26 | SUFFICIENT |
| a0 | 2h | 481 | +69.2 | 1.13 | SUFFICIENT |
| a0 | 4h | 256 | +170.6 | 1.69 | SUFFICIENT |
| a1 | 12h | 321 | -13.1 | 0.97 | SUFFICIENT |
| a1 | 1d | 103 | +43.4 | 1.38 | SUFFICIENT |
| a1 | 4h | 255 | +164.3 | 1.73 | SUFFICIENT |
| b0 | 12h | 310 | +5.5 | 1.01 | SUFFICIENT |
| b0 | 1d | 108 | +35.4 | 1.26 | SUFFICIENT |
| b0 | 2h | 481 | +69.2 | 1.13 | SUFFICIENT |
| b0 | 4h | 256 | +170.6 | 1.69 | SUFFICIENT |
| b1 | 12h | 296 | -12.4 | 0.96 | SUFFICIENT |
| b1 | 1d | 101 | +24.0 | 1.23 | SUFFICIENT |
| b1 | 2h | 34 | -10.4 | 0.73 | INSUFFICIENT |
| b1 | 4h | 253 | +121.6 | 1.62 | SUFFICIENT |
| b2 | 12h | 296 | -7.4 | 0.98 | SUFFICIENT |
| b2 | 1d | 101 | +24.3 | 1.23 | SUFFICIENT |
| b2 | 2h | 31 | -11.4 | 0.70 | INSUFFICIENT |
| b2 | 4h | 253 | +122.0 | 1.62 | SUFFICIENT |
| c_t0f0 | 12h | 310 | +5.5 | 1.01 | SUFFICIENT |
| c_t0f0 | 1d | 108 | +35.4 | 1.26 | SUFFICIENT |
| c_t0f0 | 2h | 481 | +69.2 | 1.13 | SUFFICIENT |
| c_t0f0 | 4h | 256 | +170.6 | 1.69 | SUFFICIENT |
| c_t0f1 | 12h | 336 | +37.7 | 1.09 | SUFFICIENT |
| c_t0f1 | 1d | 131 | -3.8 | 0.98 | SUFFICIENT |
| c_t0f1 | 2h | 480 | +56.7 | 1.12 | SUFFICIENT |
| c_t0f1 | 4h | 295 | +151.0 | 1.52 | SUFFICIENT |
| c_t0f2 | 12h | 296 | -12.4 | 0.96 | SUFFICIENT |
| c_t0f2 | 1d | 101 | +24.0 | 1.23 | SUFFICIENT |
| c_t0f2 | 2h | 34 | -10.4 | 0.73 | INSUFFICIENT |
| c_t0f2 | 4h | 253 | +121.6 | 1.62 | SUFFICIENT |
| c_t0f3 | 12h | 295 | -7.9 | 0.98 | SUFFICIENT |
| c_t0f3 | 1d | 98 | +70.7 | 1.60 | SUFFICIENT |
| c_t0f3 | 2h | 481 | +73.1 | 1.14 | SUFFICIENT |
| c_t0f3 | 4h | 253 | +120.6 | 1.52 | SUFFICIENT |
| c_t1f0 | 12h | 314 | -29.3 | 0.93 | SUFFICIENT |
| c_t1f0 | 1d | 110 | +40.0 | 1.30 | SUFFICIENT |
| c_t1f0 | 2h | 500 | -4.3 | 0.99 | SUFFICIENT |
| c_t1f0 | 4h | 255 | +165.3 | 1.69 | SUFFICIENT |
| c_t1f1 | 12h | 340 | +7.7 | 1.02 | SUFFICIENT |
| c_t1f1 | 1d | 141 | -39.0 | 0.79 | SUFFICIENT |
| c_t1f1 | 2h | 560 | +79.0 | 1.12 | SUFFICIENT |
| c_t1f1 | 4h | 294 | +144.6 | 1.53 | SUFFICIENT |
| c_t1f2 | 12h | 295 | +15.6 | 1.05 | SUFFICIENT |
| c_t1f2 | 1d | 104 | +21.5 | 1.17 | SUFFICIENT |
| c_t1f2 | 2h | 483 | +46.4 | 1.09 | SUFFICIENT |
| c_t1f2 | 4h | 253 | +116.2 | 1.52 | SUFFICIENT |
| c_t1f3 | 12h | 295 | -7.9 | 0.98 | SUFFICIENT |
| c_t1f3 | 1d | 98 | +70.7 | 1.60 | SUFFICIENT |
| c_t1f3 | 2h | 482 | +69.2 | 1.13 | SUFFICIENT |
| c_t1f3 | 4h | 253 | +120.6 | 1.52 | SUFFICIENT |
| c_t2f0 | 12h | 322 | -30.2 | 0.93 | SUFFICIENT |
| c_t2f0 | 1d | 108 | +46.1 | 1.35 | SUFFICIENT |
| c_t2f0 | 2h | 482 | +73.0 | 1.14 | SUFFICIENT |
| c_t2f0 | 4h | 252 | +122.6 | 1.55 | SUFFICIENT |
| c_t2f1 | 12h | 356 | -39.3 | 0.90 | SUFFICIENT |
| c_t2f1 | 1d | 135 | -16.3 | 0.90 | SUFFICIENT |
| c_t2f1 | 2h | 480 | +43.9 | 1.11 | SUFFICIENT |
| c_t2f1 | 4h | 251 | +91.5 | 1.54 | SUFFICIENT |
| c_t2f2 | 12h | 291 | -34.8 | 0.86 | SUFFICIENT |
| c_t2f2 | 1d | 103 | +0.8 | 1.01 | SUFFICIENT |
| c_t2f2 | 2h | 34 | -5.9 | 0.80 | INSUFFICIENT |
| c_t2f2 | 4h | 24 | -0.5 | 0.97 | INSUFFICIENT |
| c_t2f3 | 12h | 293 | -10.4 | 0.97 | SUFFICIENT |
| c_t2f3 | 1d | 98 | +70.6 | 1.59 | SUFFICIENT |
| c_t2f3 | 2h | 481 | +72.0 | 1.14 | SUFFICIENT |
| c_t2f3 | 4h | 252 | +122.6 | 1.54 | SUFFICIENT |
| c_t3f0 | 12h | 312 | -24.6 | 0.94 | SUFFICIENT |
| c_t3f0 | 1d | 109 | +60.4 | 1.45 | SUFFICIENT |
| c_t3f0 | 2h | 499 | -8.1 | 0.99 | SUFFICIENT |
| c_t3f0 | 4h | 253 | +122.3 | 1.53 | SUFFICIENT |
| c_t3f1 | 12h | 336 | +58.5 | 1.13 | SUFFICIENT |
| c_t3f1 | 1d | 137 | -2.8 | 0.99 | SUFFICIENT |
| c_t3f1 | 2h | 561 | +76.2 | 1.10 | SUFFICIENT |
| c_t3f1 | 4h | 251 | +128.5 | 1.56 | SUFFICIENT |
| c_t3f2 | 12h | 296 | +7.0 | 1.02 | SUFFICIENT |
| c_t3f2 | 1d | 103 | +4.6 | 1.04 | SUFFICIENT |
| c_t3f2 | 2h | 485 | +43.4 | 1.10 | SUFFICIENT |
| c_t3f2 | 4h | 24 | -0.5 | 0.97 | INSUFFICIENT |
| c_t3f3 | 12h | 293 | -10.3 | 0.97 | SUFFICIENT |
| c_t3f3 | 1d | 98 | +70.6 | 1.59 | SUFFICIENT |
| c_t3f3 | 2h | 482 | +68.0 | 1.13 | SUFFICIENT |
| c_t3f3 | 4h | 252 | +122.7 | 1.54 | SUFFICIENT |


## 错失利润表(门控臂 vs 基线,full @ 1.0x)

| 门控臂 | 基线臂 | 被拒数 | 错失利润 | 规避亏损 | MFE_R 中位 | 被拒率 | 配对率 | 未配对 |
|---|---|---|---|---|---|---|---|---|
| b1 | a0 | 25539 | +1332.6 | +966.5 | +0.750 | 0.935 | 0.033 | 24702 |
| b2 | a0 | 25641 | +1333.3 | +966.5 | +0.750 | 0.939 | 0.033 | 24802 |
| c_t0f2 | a0 | 25539 | +1332.6 | +966.5 | +0.750 | 0.935 | 0.033 | 24702 |
| c_t0f3 | a0 | 8455 | +748.5 | +463.0 | +0.938 | 0.309 | 0.044 | 8087 |
| c_t1f2 | a0 | 22037 | +962.7 | +685.0 | +0.804 | 0.939 | 0.026 | 21465 |
| c_t1f3 | a0 | 19397 | +960.4 | +682.3 | +0.807 | 0.827 | 0.029 | 18828 |
| c_t2f2 | a0 | 27320 | +1415.7 | +1053.3 | +0.746 | 0.932 | 0.035 | 26376 |
| c_t2f3 | a0 | 2973 | +442.5 | +348.9 | +0.922 | 0.101 | 0.086 | 2716 |
| c_t3f2 | a0 | 23818 | +1045.8 | +771.7 | +0.799 | 0.936 | 0.029 | 23139 |
| c_t3f3 | a0 | 13915 | +654.4 | +568.2 | +0.800 | 0.547 | 0.033 | 13457 |


## T×F 因子边际效应表(full @ 1.0x)

| 因子水平 | n 组合 | net 边际均值 | PF 边际均值 |
|---|---|---|---|
| T.t0 | 4 | +225.4 | 1.20 |
| T.t1 | 4 | +204.1 | 1.16 |
| T.t2 | 4 | +126.4 | 1.09 |
| T.t3 | 4 | +179.0 | 1.13 |
| F.f0 | 4 | +203.5 | 1.16 |
| F.f1 | 4 | +193.6 | 1.13 |
| F.f2 | 4 | +84.1 | 1.08 |
| F.f3 | 4 | +253.8 | 1.21 |


## 口径附录

- 指标公式(照抄 compute_metrics 口径):net=Σnet_pnl;return=net/300;胜负按 net_pnl 符号;PF=Σ赢净额/|Σ亏净额|(分母 0 ⇒ inf);gR=mean(r_multiple);avgW_R/avgL_R=mean(gross_pnl/risk_usdt);MaxDD=峰值回撤(年度切片同式)。
- 年度归属:交易按 open_decision_ts(信号 bar 时间戳,决策时刻口径);跨年持仓整体归属开仓年度;年度边界 = 2025-01-01 / 2026-01-01 UTC(毫秒常量预注册)。
- 占位约定:N=0 ⇒ WR/PF/gR/avgW/avgL 显示 NA(净额与回撤仍为数值);PF 分母 0 ⇒ inf。
- test 段与 2026 段高度重叠(dev_frac=0.7 切分点约落在 2026 年初附近);报告如实声明该重叠,分年度属 full 段描述性细分、不构成 test 段择优。
- 计价基准:equity0=300 USDT 固定基准;不使用 v21.json 的 small_account_compounding 子树(104U 阶梯复利),与在册 v21 数字口径差异声明。
- 品种差异面:parent(v21.json)symbols 含 DOGEUSDT,但 data/ 无 DOGE 历史回测数据;本轮以 3 品种(BTC/ETH/SOL)运行,与在册 4 品种数字不可逐位对账(决策 D6:首轮自锚登记)。
- 聚合帧差异面:2h/4h/12h/1d 聚合帧的 manifest sha256 记录在仓库在册状态下即已过期(文件重新聚合后 manifest 未同步;git diff 为空⇒ 既有现实,非本轮引入);主数据帧(1h/30m)严格复核一致;实际 sha256 见审计头 agg_frame_drift;在册 v21 数字与既有诊断脚本基于同一当前文件内容。
- test 曝光纪律:A0 锚定格点的 test@1.0x/1.5x 重放为基线登记(v21 4 品种 test 数字已在册曝光),不计为择优曝光;候选 winner 的test 曝光自 25 起续号。

- 在册数字 sanity 对照(量级/方向,不作 fail-fast、不入裁决依据):

  - dev@1.0x: +360.8 USDT (PF 1.48)

  - dev@1.5x: +321.4 USDT (PF 1.41)

  - test@1.0x: +214.5 USDT (PF 1.59)

  - test@1.5x: +193.2 USDT (PF 1.51)

  - full@1.0x: +575.3 USDT (gR +0.351, WR 52.8%)

- B2 状态切换覆盖率(各战役):

  - BTCUSDT@2h: {"n_bars": 8800, "high_frac": 0.09056818181818181, "switches": 58}

  - BTCUSDT@4h: {"n_bars": 4400, "high_frac": 0.1125, "switches": 34}

  - BTCUSDT@12h: {"n_bars": 1468, "high_frac": 0.09604904632152589, "switches": 8}

  - BTCUSDT@1d: {"n_bars": 735, "high_frac": 0.06666666666666667, "switches": 2}

  - ETHUSDT@2h: {"n_bars": 8800, "high_frac": 0.10056818181818182, "switches": 62}

  - ETHUSDT@4h: {"n_bars": 4400, "high_frac": 0.11272727272727273, "switches": 30}

  - ETHUSDT@12h: {"n_bars": 1468, "high_frac": 0.12329700272479564, "switches": 14}

  - ETHUSDT@1d: {"n_bars": 735, "high_frac": 0.0380952380952381, "switches": 2}

  - SOLUSDT@2h: {"n_bars": 8800, "high_frac": 0.09920454545454545, "switches": 58}

  - SOLUSDT@4h: {"n_bars": 4400, "high_frac": 0.10977272727272727, "switches": 26}

  - SOLUSDT@12h: {"n_bars": 1468, "high_frac": 0.09128065395095368, "switches": 8}

  - SOLUSDT@1d: {"n_bars": 735, "high_frac": 0.0653061224489796, "switches": 6}
