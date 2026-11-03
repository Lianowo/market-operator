# 实时保护与趋势快速 ACCEPT：部署验收手册（2026-11-03）

> 本文是后续部署步骤，**本次分支整理没有执行部署、重启或下单**。
> 只部署保护时钟与顺趋势第一根快速确认；风险预算与实盘基线一致，不含 J 档或新增同方向容量门。

## 1. 核验过的现场

- 实盘工作区：`D:/LiAn/Web3Code/market-operator`，分支 `feat/stce-v3-strategy`。
- 开发工作区：`D:/LiAn/Web3Code/market-operator-realtime`，分支 `feat/realtime-protection`。
- 观测到 supervisor PID 24756、runner PID 26084；**PID 仅为历史观测，部署时必须重新确认命令行与路径**。
- 当前实盘风险为旧生产预算，不在此次改动范围。

不要从开发工作区启动另一套 live runner。状态、心跳、日志及 secrets 锚定工作区根目录；必须保留实盘工作区原有运行文件，并确保只有一套 supervisor/runner。

## 2. 切换前验收

1. 两个工作区的 `git status --porcelain=v1` 必须为空。
2. 记录实盘 HEAD、实际 supervisor/runner 命令行、持仓及交易所 TPSL，备份原地 `runtime/live/`；不打印 secrets。
3. 确保已有仓位有交易所保护；本地 pending/仓位/保护单与交易所一致。
4. 在开发工作区执行离线全套测试；不要把 live 启动当测试。

```powershell
git -C D:/LiAn/Web3Code/market-operator status --porcelain=v1
git -C D:/LiAn/Web3Code/market-operator rev-parse HEAD
git -C D:/LiAn/Web3Code/market-operator-realtime status --porcelain=v1
python -m pytest -q D:/LiAn/Web3Code/market-operator-realtime/tests
```

## 3. 单进程切换顺序

1. 重新确认 PID 后先停 supervisor，再停 runner，避免自动拉起；不撤掉交易所保护单。
2. 开发工作区确认干净后 detach，释放 `feat/realtime-protection` 分支。
3. 实盘工作区 checkout 该分支，原地保留 `runtime/live/`、`logs/`、secrets；不搬到另一个 ROOT。

```powershell
git -C D:/LiAn/Web3Code/market-operator-realtime checkout --detach
git -C D:/LiAn/Web3Code/market-operator checkout feat/realtime-protection
```

4. 在实盘工作区用只读解析确认配置：
   - `D:/LiAn/Web3Code/market-operator/config/live.json`：轮询 5s、`price_clock=15m`、报价最大年龄 30s；risk 整段与切换前一致。
   - `D:/LiAn/Web3Code/market-operator/config/strategy.json`：`fast_accept_atr=0.5`、`n_a=3`；没有两根快速、regime 黑名单或本次新增方向预算键。
   - ACCEPT/REJECT 白名单、复利阶梯、杠杆、目标、组合/周期预算不变。
5. 在原实盘工作区启动一套 supervisor，由它拉起 runner，不再单独起第二个 runner：

```powershell
Set-Location -LiteralPath D:/LiAn/Web3Code/market-operator
python D:/LiAn/Web3Code/market-operator/scripts/live_supervisor.py
```

## 4. 启动后验收

- 对账完成、权威 entry 回写、仓位数量与保护计划单一致；不补开历史信号。
- 心跳持续推进；5s 是轮询配置，不要求 API 调用绝对无延迟。
- 持仓存在且行情满足条件时，`price_defense` 先体现 15m 收盘授予，再有新鲜报价棘轮；没有满足条件时不强求出现该日志。
- 重启/长停顿不从建仓日爬取历史；新仓没有前仓时钟锚。
- TPSL 先挂新后撤旧，禁止有意留出无保护窗口；无异常重复单、API 失败告警或重复离场。
- 快速 ACCEPT 必须来自已收盘 bar 且方向匹配趋势；非趋势连续到第三根仍可发普通 ACCEPT。
- 检查风险预算未变化，不以较小仓位美化本次执行提速结果。

## 5. 回滚与限制

- 回滚前先停 supervisor/runner，再在原实盘工作区切回所记录的原版本，保留原地状态并由 supervisor 启动；切回前检查分支未被其他 worktree 占用。
- 恢复旧版本后会恢复旧价格时钟；不能承诺回滚后仍有新 15m/5s 保护。
- 未校正的 `price_source=ref` 保持固定保护，待对账后参与锁利。
- REST 采样可能漏极值；交易所止损继续兜底。预算是准入规则，不是最大回撤保证。
- 历史研究不是独立 OOS，也不是实盘报价精确模拟；不沿用旧 27% DD 或更快就必然更赚钱的结论。