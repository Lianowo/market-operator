# v10 研究协议（冻结，2026-10-20)——细化触发尺度 + 订单流接入设计

## 背景与承认
- 白话诊断三面俱确认：晚报(K线语言滞后)、全城看报(信号拥挤)、摊位毛利薄过门票。
- 图纸：结构脑(1h+) × 触发麦(15m/5m) × tape 眼(接受/拒绝本体) × 质量闸门。
- 本轮只做前两者；tape 设计定案但不加判定（积累不够，不许吹）。

## §A 触发麦克风（可历史验证，主交付）

- 结构/区域/episode 语义锁定在认知尺度(1h/2h/4h/12h/1d)不变；
- **触发语句在 trigger_tf ∈ {15m, 5m(dev 敏感性)} 的收盘上评估**：同族模式
  （拒绝回退/接受站稳/破坏收复），阈值仍用**认知帧 ATR**(k_rj 等先验不动，
  数量闸门按尺度比换算：N_f_mic = N_f × (obs/tf) = 12 for 1h→15m);
- 发射后入场=该 trigger 帧次根开盘（比 1h 收盘最多早 45 分钟）;
- 事件语义纯增量(id 同 E1/E3/E6/E7 + 标注 mic=True，知时=trigger 帧收盘);
- 收割/费用/容量/组合约束全套不变；测试：因果、无前馈、parity、确定性。

## §B 订单流接入（本论只定设计，不评估）

1. 记录：`scripts/tape_recorder.py`（已在仓）持续落 data/tape/*.jsonl;
2. 特征：`market_operator/tape_features.py`（窗口 delta、CVD、超大单占比、
   价格效率 |move|/volume、区域接触时刻的吸收读数）;
   ※ 2026-10-24:该模块与唯一 importer(`scripts/diagnose_current_btc.py`)一并
   归档到 `scripts/archive/`(零引用退役件,见 `reports/refactor_20261023/CLEANUP_PROPOSAL.md`)。
3. 接入：**只读注解**进决策日志（不改白名单/闸门），档案+自评留痕；
4. 评估门槛：积累 ≥60 个区域接触事件（bar-event × tape-features 双全）后，
   单独冻结协议判定 tape 增量；此前一切"流能救命"言论禁入仓文档。

## §C 评估与停止
- §A:dev 主评 + test 一次（曝光 #13,log#20);GO/NO-GO 沿用；
- 判别：若早到 45min 仍不转正，"晚报"假说死于"拥挤+薄利"双因，不再追
  "更早"(5m 若 dev 无改善如实报，不引入生产）。
EOF
cat > /dev/null << 'EOF'
EOF
echo ok