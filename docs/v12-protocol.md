# v12 研究协议（冻结，2026-10-20)——K线级订单流(delta)特征检验

> 死结：tape 只有前向；K 线主动买量是**可回放的轻量订单流**,先量它有没有用，
> 再决定要不要为真 tape 付商业历史价款。

## 1. 数据（非 Bitget，如实分层声明）

Binance 现货/期货 1h K线：data.binance.vision，含 `taker_buy_base_volume`。
品种 BTCUSDT/ETHUSDT/SOLUSDT，时段尽量对齐 2024-10→2026-10（窗差声明；venue 差声明：
币安与 Bitget 微观不完全同构，本检验回答"流特征值不值钱",不回答"Bitget 复刻")。
原始字节 sha256 入 manifest 同规。

## 2. 流特征（全部因果，tok=收盘知时）

- `delta_vol_k` = 2·taker_buy − total(主动买卖差，基币量）
- `delta_share_k` = delta_vol / total
- `cvd_k` = delta_vol 累计（近 24/48 根均值化）
- `delta_z` = (delta_share − 近48均值）/ 近48标准差

## 3. 假设与判别（写死在先）

事件 = canonical E1/E2/E3/E6/E7(1h，币安数据重放，同语义同先验）。
**H1**:事件同向条件下，`delta_z` 同号（或强度分位）显著改变事件后期望——
判别按 §9 现有事件研究流程，把 (事件×方向) 按触发时 delta_z 三等分切片，
最高/最低分位相对盘中对照的增量差，若 CI 不含 0 ⇒ 流特征有增量。
**H0(v1-v9 的最后告慰）**:若切不出差异 ⇒ "bar 层流"不解决拥挤，
只剩更高频 tape/盘口(花钱的方向),如实归途。

## 4. 试与停

- dev 主评，test 一次（曝光 #15,log#24)；四判据沿用；
- 负结果即关闭流路线（bar 层部分）,转入前向 tape 积累（B 线）;
- 正结果 → 协议 v13 设计"CVD/delta 语义路由"(非过滤器，入假设元数据)。
