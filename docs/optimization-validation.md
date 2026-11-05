# 优化与语义修复验证（2026-11-03 本地工作日）

## 验证边界

- 本次不是以 Git HEAD 为用户工作区基线。优化前源码保存在
  `C:\Users\EDY\AppData\Local\Temp\market-operator-optimization-xjwnedg0`。
- 续作前源码、测试、工具与配置快照及 SHA256 清单保存在
  `C:\Users\EDY\AppData\Local\Temp\market-operator-finish-vi1hs1nz`。
- 未修改策略/实盘配置、未提交 Git、未启动、重启或停止实盘进程。
  已存在的 supervisor/runner 未触碰，因此性能环境并非完全独占。
- 优化、语义修复分别验收；语义修复引起的交易变化不能宣称为等价优化。

## 等价优化

保留惰性的不可变 pivot 历史快照，并新增 `pivot_count` / `pivot_slice`。
回放增量读取和事件合成带避免每根 bar 复制全部历史。测试覆盖旧快照稳定性、
负索引、空切片、`[-0:]` 原语义。

用初始快照仅覆盖 `structure.py`、`events.py`、`replay.py` 构建
`optimization-only/src`，独立解释器重新运行 BTC/DOGE/ETH/SOL 的
`dev/test/full × 1.0/1.5` 六种场景：交易、权益、计数器 SHA256 全部精确相同。
记录：续作快照目录内 `optimization-only-parity.json`。

## 语义修复（不要求与错误实现逐位等价）

1. **最终入场成本**：确认价和最终路由限价检查共用公式，拒绝候选恢复区域锁；
   显式成本上限与单边成本估计随意图传递，模拟市价/限价/Watch 成交重验，实盘
   下单前重验报价或精度拟合限价，paper 成交同样检查。没有成本配置时保持旧行为。
2. **触发窗口时钟**：认知 episode 身份和触发索引分离；采用的 opened/ex/puncture
   锚点均映射到触发帧，`N_f+1` 根认知窗口按 scale 换算。保持已推进细时钟状态，
   避免同步重建导致 outside_closes 丢失或已发事件重复采用。实盘增量同步为历史
   episode 映射其自己的区间，而非一律映射到当前区间；增量触发索引沿用裁剪预热
   的索引原点，并处理首桶不完整。映射只做时间算术与二分，不逐次聚合全部历史。
3. **同向在途风险**：从活跃 pending OPEN / resting entry 动态派生预算占用；
   准入与成交重验，超限整笔 `DIRECTIONAL_CAP`，不将该约束混入普通池缩量。
   撤单、过期自然释放；当前成交订单排除一次，未处理订单仍可见。
   实盘/paper 适配层支持显式同向上限（risk 字段优先，否则 strategy 字段），
   缺省不启用；其费用单位沿用该适配层现有完整成本准入单位。

当前语义实现相对初始快照的交易数（不是收益验收）：

| 场景 | 初始 | 修复后 |
|---|---:|---:|
| dev × 1.0 | 457 | 458 |
| dev × 1.5 | 445 | 445 |
| test × 1.0 | 213 | 215 |
| test × 1.5 | 209 | 212 |
| full × 1.0 | 666 | 668 |
| full × 1.5 | 651 | 653 |

六种场景交易、权益、计数器均有变化；上述配置未启用成本或同向上限，变化来自
触发层语义修复。完整指纹在 `semantic-current-parity.json`，没有重写冻结基线。

## 性能证据

旧的按整实现顺序测试出现反向控制组退化，不能直接接受。基准工具已改为每个
case/side/round 新解释器，交替 AB/BA，计时与 tracemalloc 内存阶段分开；
固定 hash seed，逐次检查输出指纹、记录环境、源码与数据摘要、原始样本。

第一组四轮、每 worker 一次预热一次计时，六种认知输出指纹全部一致。
原始结果在 `C:\Users\EDY\AppData\Local\Temp\market-operator-performance-6ebb9287`：
`optimization-before.json` / `.samples.jsonl` / `.stderr`。
期间 broker 语义模块修改导致整 src 摘要变化，**工具返回非零**；这不算完整冻结
验收，不能省略该事实。三个 cognition 优化模块在本组期间保持未改。

| 场景 | current / baseline 中位耗时比（>1 较慢） |
|---|---:|
| dense pivots 8k | 0.566 |
| no pivots 8k | 1.158 |
| dense pivots 32k | 0.083 |
| no pivots 32k | 1.027 |
| BTC 1h 普通 | 1.005 |
| BTC 1h balance | 0.741 |

以上只是描述统计，不能当作所有负载无退化的证明。

随后以每样本六次 prepare 平均、每个 worker 三样本、四轮 AB/BA 的独立解释器
复核零 pivot（每侧每尺寸 12 个 batch 样本）。原代码 8k 比为 **1.089**，32k
为 **0.971**，不能忽略小控制组的开销迹象。cProfile 确认该 fixture 根本未调用
`_band` / `pivot_slice`，而新增 `pivot_count` getter 内的 len 有额外开销；
profile 不是验收计时，不将其绝对值当作精确归因。

最小调整：回放中把前次 pivot 数保存为局部变量，每根 bar 只读一次 count，
不更改结构算法、列表或快照契约。相同协议再次验证：

| 零 pivot 尺寸 | baseline 中位秒 | current 中位秒 | current / baseline |
|---|---:|---:|---:|
| 8k | 0.038193 | 0.036251 | 0.949 |
| 32k | 0.149419 | 0.144771 | 0.969 |

8k 四个配对比为 0.989 / 0.842 / 0.912 / 1.164，32k 为
0.985 / 1.006 / 0.960 / 0.938；不能声称每轮都更快。两个尺寸输出指纹全部一致，
优化模块测量前后摘要一致。证据 `targeted-zero.json` / `targeted-zero-after.json`
及同名 `.samples.jsonl`，profile 在 `quiet-zero-*.prof` / `.txt`。
据此旧的 32k 两倍退化未在交错批量实验中复现；8k 的可疑额外访问已最小化。
结论限定为：**本机已测控制组无中位数退化，非所有环境的统计显著性保证**。
最小调整后六场景认知指纹又与已保存的独立 baseline 全部精确匹配，记录
`after-fingerprints.json`；该单次运行只用于指纹确认，不作为新的提速统计。

## 安全限制

- 下单前报价检查无法保证最终市价成交价。成交回执超过成本/同向约束时，只审计、
  去重告警并保留真实持仓及原有保护规则；不谎称拒绝已成交订单，不自动新增平仓策略。
- 冻结 `cost_per_side` 是已有路由成本估计，不等价于实际 fee/funding 的逐笔上界。
- 未连接交易所验证；本地测试使用假客户端。部分成交、回执不完整及轮询延迟仍是
  既有实盘适配限制，本次没有重做成交状态机。
- 同向上限不等于总组合预算/周期预算的在途预留。普通池及其准入与成交的不对称
  单位没有顺便重写。
- 认知/触发修复会改变事件和交易，不证明收益增加；上线前需按新语义另行审阅。
- 非 balance 模式的历史预热趋势候选 future deadline 映射有既有边界差异，
  本次没有扩大到另一套策略规则；当前生产加载校验要求 balance 模式。

## 回归

集成第一次全套：**871 passed, 1 skipped（25.09s）**；
全部收尾调整后最终全套：**885 passed, 1 skipped（17.88s）**。
专项涵盖触发 scale 1/4/6、多空、精确边界、因果前缀、同向三种成本单位、
撤单/过期、Watch 转换、实际成交几何、限价精度、非法显式成本约束 fail-closed、
回执审计、预热裁剪后真实增量推进及每 bar 一次 count 访问。
最终又重新运行两组六场景：纯优化与初始指纹完全相同；当前语义实现再次完成
六场景，交易数保持上表。`git diff --check` 通过。

续作快照中的配置、position、structure、events 与原有相关测试逐文件 SHA256
匹配；在有新增修改的 live_runner/trigger 中以增量补丁保留原实现，未 reset 或覆盖
用户更改。保存最终日志：`final-tests.stdout` / `final-tests.done.json` /
`final-validation.done.json`（均在续作快照目录，不写生产 runtime）。