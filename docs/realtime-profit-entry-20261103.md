# 实时保护 + 反向前提 + 入场确认：干净基线结论（2026-11-03）

> 基线：`origin/feat/stce-v3-strategy`（HEAD `bfb448d`），不是主工作区的研究代码。
> 所有回放为离线 15m OHLC 执行、104U、正式风险/成本；未重启实盘、未下单。
> test 段已被多轮研究使用，不是新的独立样本外。

## 1. IDOL 类问题：目标到了以后如何实时保护利润

### 1.1 结论

**最优解不是“1R 就开始锁”，也不是“每个 tick 触价就锁”，而是：**

1. **15m 已收盘 bar 授予/推进目标锁利**（`price_clock=15m`）；
2. arming 后，**每个新鲜报价推进峰值并收紧防线**（`_poll_position_prices`）；
3. 防守仍是 `peak ∓ (1-|frac|)·|target-entry|`，生产 `frac=-0.25`（锁 25%、允许回吐 75%）；
4. `_manage_stops` 在 `invalid` 变化后立即先挂新 TPSL、再撤旧单。

这样 IDOL 类“盘中到目标、2H 收盘又回到目标内”的单，不再等 2H：15m 收盘越过目标就 arming，之后报价实时棘轮。

### 1.2 隔离回放（dev/test/full，1x/1.5x）

| 变体 | full 1x | full 1.5x | full DD 1x | full DD 1.5x | 目标到过但净亏代理 |
|---|---:|---:|---:|---:|---:|
| baseline（2H 收盘 + 2H 时钟） | 141.45 | 44.90 | 12.66% | 21.26% | 28 / 31 |
| **cadence_close_15m（15m 收盘 arming + 15m 时钟）** | **123.87** | **39.93** | **13.36%** | **18.82%** | **19 / 24** |
| cadence_touch_15m（15m 触价 arming） | 94.05 | 26.74 | 13.75% | 23.51% | 22 / 34 |
| 1R MFE profit ladder（已否决） | dev 3.75 | dev -16.39 | — | — | — |

读法：

- `cadence_close_15m` 把“到过目标后又净亏”的代理从 **28/31 降到 19/24**（约 −32%/−23%），代价是净额 **−12.4%/−11.1%**；
- **1.5x DD 反而改善**（21.26%→18.82%）；1x DD 小幅变差（12.66%→13.36%）；
- `cadence_touch_15m`（tick 触价语义的代理）更差，说明**不要每个 tick 都授予锁利资格**；
- 1R MFE ladder 在 dev 直接崩掉（净额 3.75 / −16.39），否决：过早收紧会把正常回抽当反转。

### 1.3 为什么不是“纯执行提速”

回放 `exit_exec` 的 15m 网格会同时改变 arming 时刻；它不是“同一资格只换单更快”。实盘 `on_price` 因此明确：

- `frac<0`：报价**不** arming，只推进已 armed 的峰值/防线；
- `frac>0`：报价才可触价 arming；
- `price_clock=15m` 的 15m 收盘是 arming 来源，报价是之后的管理时钟。

## 2. 已有仓位遇到同周期/更大周期反向信号

### 2.1 结论

**不需要新增“反向 ACCEPT/REJECT 自动退出”。** 证据：

| 变体（dev） | 1x | 1.5x | DD 1x | DD 1.5x |
|---|---:|---:|---:|---:|
| baseline | 59.07 | 12.69 | 8.89% | 12.98% |
| `accept_reenter_box_exit=True` | 56.20 | 9.58 | 8.91% | 15.86% |
| `accept_reenter_box_exit=True + 15m 时钟` | 44.72 | 6.78 | 10.35% | 15.76% |

- 单独加“突破单重新收回箱内就退出”没有改善，1.5x DD 反而变差；
- 15m 时钟叠加也没有救回来（净额/DD 更差）；之前的“一律反向退出”更是 14.02 / −20.94（研究产物，非生产）。
- 生产已有的 **§8.1 收盘破 invalid** 和 **§8.5 反向 CHoCH** 已经覆盖真正的“前提证伪”；
- 同箱体对侧的 REJECT 是目标/轮动，不是反手信号；更小周期反向不干预；更高级别反向只在同时破坏结构/防线时退出，否则只减仓/收紧。

**最优解：保留现有结构证伪出场，不新增无条件的反向自动退出；反向 ACCEPT/REJECT 只作为人工复核/告警，不作为仓位突变规则。**

## 3. ACCEPT/REJECT 加快确认（不用限价回抽）

### 3.1 结论

**不要无条件把 `n_a=3` 改成 1。** 无条件 1 根会显著增加交易数和 full 净额，但 DD 明显恶化，test 1.5x 反而变差：

| 变体 | test 1x | test 1.5x | full 1x | full 1.5x | test DD 1.5x | full DD 1.5x |
|---|---:|---:|---:|---:|---:|---:|
| baseline n_a=3 | 51.36 | 34.14 | 141.45 | 44.90 | 18.13% | 21.26% |
| n_a=2 | 49.03 | 29.30 | 172.87 | 61.81 | 21.71% | 26.55% |
| n_a=1 | 51.86 | 29.03 | 214.32 | 79.09 | 22.40% | 30.15% |
| **fast_accept_atr=0.50（条件式）** | **55.92** | **35.74** | **175.21** | **64.45** | **20.36%** | **25.26%** |
| fast_accept_atr=0.75 | 48.07 | 29.63 | 153.39 | — | 18.44% | — |

**条件式 fast ACCEPT：**

- 只在**第一根收盘**同时满足：
  - 突破边沿的幅度 `ext_dist >= 0.50 × ATR`；
  - 收盘位于当根区间有利侧 `>= 65%`；
  - 方向实体/推进成立（`progressing`，非逆高级别趋势）；
- 才跳过剩余 2 根；否则仍等 `n_a=3`。
- 结果：test 1x/1.5x **均略高于 baseline**，full 净额显著提高；代价是 DD 上升（full 1x 12.66%→17.65%，1.5x 21.26%→25.26%）。
- 这是“第一根足够强才提前”的实际实现，不是无条件 1 根。

**2h→30m 触发**（dev）：64.73 / 3.13，1.5x 明显恶化；不作为默认。

**REJECT**：`n_f=3` 是“极值后最多观察 3 根”的上限，不是最少等待；它不需要 3 根确认。真正能提速的是更细的观察帧（30m），但 30m 单独证据不足；15m 更差。当前不改成限价回抽。

### 3.2 组合交互：Q1 与 Q3 不可加

在“fast_accept_atr=0.50”的配置下再开 15m 保护时钟，full 1.5x DD 反而更差：

| 组合（full） | 1x 净额 | 1.5x 净额 | DD 1x | DD 1.5x |
|---|---:|---:|---:|---:|
| baseline（无 fast accept，2H 时钟） | 141.45 | 44.90 | 12.66% | 21.26% |
| 仅 15m 时钟（无 fast accept） | 123.87 | 39.93 | 13.36% | 18.82% |
| 仅 fast_accept 0.50（2H 时钟） | 210.72 | 61.75 | 29.35% | 36.96% |
| fast_accept 0.50 + 15m 时钟 | 182.79 | 35.37 | 26.97% | 40.82% |

结论：

- **15m 时钟是“风险优先”档**：净额下降，但“到过目标又净亏”代理下降、1.5x DD 改善；
- **fast_accept 是“收益优先”档**：净额和交易数上升，但 DD 大幅上升；
- **两者叠加不是折中，而是 1.5x DD 最差（40.82%）**。在固定风险预算下不要同时默认开启；需要分别作为两个风险档位。

## 4. 当前候选代码/配置

- `position.on_price`：报价推进已 armed 的锁利；`frac<0` 报价不 arming。
- `live_runner`：
  - `protection.price_clock`：`bar`（旧 2H）/`15m`（推荐）/`quote`（仅报价，负 frac 不 arming）；
  - `_advance_price_clock`：15m 收盘调用同一 `on_bar(price_only=True)`；
  - `_poll_position_prices`：新鲜报价棘轮/越线离场；
  - `_on_sub_bar` 的目标锁利块只在 `price_clock=bar` 运行。
- `config/live.json`：
  - `poll_seconds=5`，`protection.price_clock="15m"`，`max_quote_age_seconds=30`。
- `events.py`/`trigger.py`/`structure.py`：
  - `fast_accept_atr` 条件式快速确认（未设置 ⇒ 旧 `n_a=3` 行为逐位不变）；这是**收益优先档的可选键**，最终风险优先配置不启用。
- `config/strategy.json`：`reject_opp_lock_frac` 保持 `-0.25`；`fast_accept_atr` 未设置（默认 `None`）。
- 1R MFE profit ladder 仅在临时实验分支验证，dev 已否决；**最终候选代码中已移除**，不保留死开关。

## 5. 验证

```text
python -m pytest -q                                     # 529 passed, 1 skipped
python -m pytest tests/test_realtime_protection.py -q   # 8 passed
python -m pytest tests/test_fast_accept.py -q           # 2 passed
python tools/replay_profit_protection.py --segments dev,test,full ...
python tools/replay_entry_clock.py --segments dev,test,full ...
```

## 6. 边界

- 15m OHLC 不是 tick；实盘用 5s 报价做棘轮，但采样间极值仍可能漏掉，交易所 TPSL 是固定止损后备。
- `price_source=="ref"` 的仓位先保留固定保护，等 `_reconcile` 用交易所 VWAP 回写后再进入锁利。
- 回放的 15m 网格与实盘 5s/15m 组合不完全等价；收益数字用于隔离方向，不是上线收益承诺。
- test 已被多次使用；本报告不宣称独立样本外优势。
