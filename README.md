# market-operator

面向 Bitget USDT 永续（原生加密 + RWA 非加密永续）的因果市场结构认知、可证伪策略回放与多周期实盘交易系统。

核心主链：**二维时空结构认知 (`structure` / `level`) → 因果事件原语 (`events`) → 可证伪交易假设 (`hypothesis`) → 质量路由 (`routing`) → 事件驱动持仓管理 (`position` / `trigger`) → 统一风险容量与撮合 (`capacity` / `broker` / `campaign`) → 同源实盘与运维台 (`live_runner` / `ops` / `cli` / `tg_commands`)**。

- **活跃策略配置**：[`config/strategy.json`](config/strategy.json)（`2d-spatiotemporal-field-v3-triple-screen`）
- **实盘接入配置**：[`config/live.json`](config/live.json)（`live-atomic-level-v2-triple-screen`）
- **RWA 交易时段配置**：[`config/sessions.json`](config/sessions.json)（零硬编码日期、零硬编码合约代码）
- **策略等价性校验**：`python tools/verify_strategy_parity.py --ref <commit>`（逐笔交易、净值与计数器逐项比对）
- **变更记录**：[`CHANGELOG.md`](CHANGELOG.md)

---

## 设计原则与硬不变量

1. **时间即真相（零未来函数）**：每个结构事实携带 `forming_index` 与 `known_index`，任何决策只允许读取 `known_index <= t`。前缀不变性、增量 == 批量、价格缩放不变性由 `tests/test_invariance_gates.py` 强制锁定。
2. **严格分层与单向依赖**：策略内核、回放门面、实盘辅助、运维台与脚本层职责隔离，严禁跨层反向依赖（例如 `ops` / `cli` / `tg_commands` 禁止顶层导入 `scripts/live_runner.py`），由 `tests/test_layering.py` 静态校验。
3. **规则推导与零魔法硬编码**：非加密（RWA）交易时段与节假日完全由交易所日历规则推导，仓库源码与配置文件禁止出现固定年月日或具体合约代码，由 `tests/test_sessions_calendar.py` 强制校验。
4. **回放与实盘完全同源**：实盘引擎与回放引擎共用 `engine`、`capacity`、`routing`、`position`、`trigger`、`sessions` 与 `live_support` 同一套纯计算原语。

---

## 核心策略与风控架构（当前生产口径）

以 [`config/strategy.json`](config/strategy.json) 与 [`config/live.json`](config/live.json) 为唯一事实源：

- **二维 `(t, P)` 时空结构与原子水平位 (`structure.py` / `level.py`)**：
  - 双视野（广角 Macro + 近景聚焦 Foveal）自适应识别活跃支撑/阻力水平位带，画布内生振幅分位自标定带宽，零 ATR 比例魔法常数。
- **三重滤网正交级联 (`1d->4h` / `4h->1h` / `1h->15m`)**：
  - `1d -> 4h`：宏观顺势突破（白名单仅 `ACCEPT`），并提供更高周期墙体拦截与趋势罗盘；
  - `4h -> 1h`：核心波段（`ACCEPT` + `REJECT`）；
  - `1h -> 15m`：龙头高流动性标的短线反转（白名单仅 `REJECT`）。
- **持仓管理与细周期级联证伪 (`position.py` / `trigger.py`)**：
  - 单标的唯一主槽位（支持低周期向高周期原子升格）；
  - 启用顺势浮盈金字塔加仓（`allow_add=true`，单仓最多加仓 1 次、倒金字塔 `0.5x` 上限），需满足新结构失效位越过首仓成本；
  - `partial_exit_enabled=false` + `reject_opp_lock_frac=1.0` + `accept_reenter_level_exit=true` + `exit_exec=true` + `exit_exec_trail=true`；突破证伪与反向主导破位下放至触发细周期收盘实时防守。
- **风险容量池与四档 HWM 动态复利 (`capacity.py` / `broker.py`)**：
  - 准入口径：`capacity_mode = "cash_net_admission"`（本金现金池口径 + 同向/反向对冲抵扣）；
  - 组合约束：`portfolio_cap_frac = 0.30`、`directional_cap_frac = 0.20`、`tf_cap_by_tf = {1h: 0.15, 2h: 0.15, 4h: 0.30, 1d: 0.15}`；
  - 同向突发与相关性缩放：`factor_burst_cap_1h = 1`、`factor_burst_cap_4h = 2`、`markowitz_corr_sizing = true`、`max_eff_size_mult = 1.5`；
  - 小账户复利阶梯（起步 `500 USDT`，最大杠杆 `20x`）：高水位净值 `<= 15,000U: 4.0%`、`<= 100,000U: 3.4%`、`<= 300,000U: 2.8%`、`> 300,000U: 2.0%`；
  - 资金费率回放采用真实历史结算序列（`funding_source = "history"`）。
- **实盘宇宙、RWA 时段与成交质量保护 (`live.json` / `sessions.py` / `adopt.py`)**：
  - 活跃宇宙：原生加密 24h 名义成交额 `> 5,000,000 USDT`（容量系数 `1.0`），RWA 永续 `> 3,000,000 USDT`（容量系数 `0.5`）；
  - RWA 交易时段四层解析：`data/calendars/overrides.json`（运维覆盖）> 结构命名族（外汇/港股规则）> `data/calendars/assignments.json`（`scripts/learn_sessions.py` 自动分派）> `weekday_only` 兜底，叠加静默死水窗口检测与 `quote_guard`（仅 RWA：报价年龄 `<= 30s`、点差 `<= 30bps`、基差 `<= 150bps`）；
  - 强制挂交易所原生止损保护（`require_exchange_protection=true`、`hedge_mode`），并支持接管交易所既有外部仓位（`adopt_external_positions.enabled=true`）。

---

## 代码分层与模块索引

### 1. 核心包 `src/market_operator/`

```
基础设施与共享契约:  exceptions.py · timeframes.py · paths.py · secrets_loader.py
数据与交易时段:      data.py · aggregate.py · split.py · sessions.py
认知与策略决策链:    structure.py ──► level.py ──► events.py ──► hypothesis.py ──► routing.py ──► position.py
                                                                                      ▲
                                                                                 trigger.py (细周期级联)
容量、撮合与回放:    capacity.py ──► broker.py ──► engine.py ──► campaign.py ──► replay.py ──► scenario.py
                                                                                  └── adopt.py (外部仓接管)
实盘、可视化与运维:  live_support.py · bitget_live.py · heartbeat.py · charts.py · tg.py · ops.py · tg_commands.py · cli.py
```

| 层级 | 模块 | 职责 |
|---|---|---|
| 基础设施 | `exceptions.py` | 统一业务异常层次（`MarketOperatorError` 及其配置/数据/交易所/限流/通知子类） |
| 基础设施 | `timeframes.py` | 时间周期秒/毫秒换算、Bitget 粒度映射、`cascade_label` 与 UTC 工具的唯一事实源 |
| 基础设施 | `paths.py` | `legacy` / `portable` 目录布局解析、`ensure_layout`、`file_sha256`、`soften_stdio` |
| 基础设施 | `secrets_loader.py` | `config/secrets.env` 加载与校验（绝不入库、绝不明文输出） |
| 数据与日历 | `data.py` / `aggregate.py` / `split.py` | 历史 K 线与资金费加载、多周期无损聚合、dev/test 切分 |
| 数据与日历 | `sessions.py` | RWA 跨市场交易时段日历、节假日规则推导与静默死水窗口判定 |
| 认知与策略 | `structure.py` / `level.py` | 二维时空结构、波段枢轴、原子水平位生命周期与动态势场 |
| 认知与策略 | `events.py` / `hypothesis.py` | 因果事件检测（ACCEPT / REJECT / SWEEP_TRAP / CHOCH）与可证伪交易假设 |
| 认知与策略 | `routing.py` / `position.py` / `trigger.py` | 信号质量路由、事件驱动持仓管理、细周期触发与级联证伪追踪 |
| 组合与回放 | `capacity.py` / `broker.py` / `engine.py` | 统一风险预算公式、全成本撮合与资金费结算、策略配置装配 |
| 组合与回放 | `campaign.py` / `replay.py` / `scenario.py` / `adopt.py` | 多标的×多周期战役引擎、规范回放核心、场景门面、既有外部仓位结构接管 |
| 实盘与运维 | `live_support.py` / `bitget_live.py` / `heartbeat.py` | 实盘共享纯原语、Bitget REST v2 适配与重试、进程内心跳守卫与内核单实例锁 |
| 通知与台面 | `charts.py` / `tg.py` / `ops.py` / `tg_commands.py` / `cli.py` | 高清黑白 K 线结构图绘制、双通道 Telegram 通知、运维聚合接口、只读 TG 机器人与 `mo` CLI |

### 2. 运维与实盘脚本 `scripts/`

| 脚本 | 用途 |
|---|---|
| `scripts/mo.py` | 统一运维台 CLI 入口（等价 `python -m market_operator` 或装包后的 `mo`） |
| `scripts/live_runner.py` | 多周期实盘 / paper 引擎主进程 |
| `scripts/live_supervisor.py` | 跨平台常驻看护进程（只读 `heartbeat.json` 判活、拉起、重启预算、日度存储治理） |
| `scripts/live_status.py` | 本地账本 ↔ 交易所持仓 ↔ 保护止损三方只读核对（不一致返回退出码 1） |
| `scripts/live_restart.py` | 代码更新后平滑重启实盘进程并等待新心跳就绪 |
| `scripts/live_gap_preview.py` | 掉线缺口补做只读预演（白名单拦截一切写接口） |
| `scripts/live_paper_check.py` | 离线/回放态实盘引擎冒烟自检（白名单拦截一切写接口） |
| `scripts/live_cost_audit.py` | 实盘成交手续费、资金费率与滑点审计（按加密/RWA 分组统计） |
| `scripts/storage_gc.py` | 存储盘点、增长预测、日志/备份压缩与预算治理（支持 `--restore` 还原） |
| `scripts/fetch_funding.py` | 拉取 Bitget 历史真实资金费率结算数据并原子落盘 |
| `scripts/learn_sessions.py` | 基于真实活跃度分布与市场基准自动学习 RWA 交易时段分派 |
| `scripts/calendar_report.py` | 导出全市场标的当前 RWA 日历归属、来源与开闭市状态 |
| `scripts/proxy_watch.py` | 本地代理连通性只读巡检 |

### 3. 研究与验收工具 `tools/`

| 工具 | 用途 |
|---|---|
| `tools/verify_strategy_parity.py` | 对比当前源码与指定 Git 提交在 `dev`/`test`/`full` × `1.0x`/`1.5x` 下的逐笔交易与计数器完全等价性 |
| `tools/replay_acceptance.py` | 多维度历史回放基线与成本压力验收 |
| `tools/replay_compounding.py` | 小账户阶梯动态复利全周期回放与拆解报告 |
| `tools/search_risk_budget.py` | 风险预算与容量参数网格搜索与筛选 |
| `tools/report_risk_budget.py` | 风险预算搜索结果汇总报告渲染 |
| `tools/benchmark_cognition.py` | 认知链路性能基准测试 |

---

## 安装、测试与验证

要求 **Python >= 3.11**。

```bash
# 安装（含开发依赖 pytest）
pip install -e ".[dev]"

# 1. 全量单元测试 + 因果不变量 + 分层约束 + 日历规范校验
python -m pytest -q

# 2. 策略等价性校验（对照基准提交或本地基线指纹）
python tools/verify_strategy_parity.py --ref HEAD
python tools/verify_strategy_parity.py --source src --output reports/parity_snapshot.json

# 3. 历史回放验收与复利回放
python tools/replay_acceptance.py
python tools/replay_compounding.py

# 4. 实盘冒烟、只读对账与缺口预演
python scripts/live_paper_check.py
python scripts/live_status.py
python scripts/live_gap_preview.py
python scripts/live_runner.py --mode paper
python scripts/live_runner.py --mode live
```

---

## 首次部署与统一运维台 `mo`

在全新主机部署时，仅需准备 `config/secrets.env`（`BITGET_*` 与 `TELEGRAM_*` 两组凭证），运行：

```bash
git clone <repo> && cd market-operator
python scripts/mo.py bootstrap --update
```

`mo bootstrap` 自动完成：代码更新（`git pull --ff-only`）→ 目录布局初始化（全新主机默认 `portable` 至 `var/`）→ 依赖安装 → 本地/交易所自检 → 注册 3 条计划任务（看护 / 存储治理 / 资金费同步）→ 启动看护与 TG 机器人 → 沙盒暖机 → 交易所对账。

### 常用 `mo` 子命令（均支持 `--json`）

```bash
python scripts/mo.py bootstrap --update --layout auto  # 一键部署与启动
python scripts/mo.py update [--check]                  # ff-only 拉取更新并按需装包
python scripts/mo.py doctor [--exchange]               # 环境自检（默认不联网）
python scripts/mo.py start | stop | restart            # 看护 → runner → TG 整条实盘链启停
python scripts/mo.py status                            # 进程 / 心跳 / 持仓 / 权益 / 计数器快照
python scripts/mo.py warmup [--write-cache]            # 沙盒暖机（不写生产 state）
python scripts/mo.py positions [--no-exchange]         # 本地账本 ↔ 交易所持仓对账
python scripts/mo.py logs [live|err|sup|tg] -n 80      # 查看日志尾部（--list 列出全部）
python scripts/mo.py tg-sync [--dry-run]               # 清理旧菜单并重新注册 TG 命令
python scripts/mo.py tg-run [--once]                   # 启动 TG 双机器人长轮询
python scripts/mo.py tasks                             # 注册 / 刷新计划任务
python scripts/mo.py paths                             # 目录布局与磁盘占用
python scripts/mo.py package -o backup.zip             # 打包运行期产物（zip + sha256 清单）
python scripts/mo.py migrate-data --dry-run            # legacy → portable 校验迁移预演
```

---

## Telegram 只读双机器人

系统运行单一长轮询进程（`mo tg-run`，内核单例锁 `tg_bot.lock`），同时服务公开信号与私密运营两个机器人。**所有交互命令严格只读**，且经过 `ops.redact()` 脱敏；公开机器人绝不暴露账户、持仓或日志。

| 角色 | 鉴权白名单 | 已注册命令 |
|---|---|---|
| `public`（公开信号） | `TELEGRAM_CHAT_ID` / `TELEGRAM_PUBLIC_CHAT_IDS` | `/start` `/help` `/levels` `/scan` `/top` `/frame` `/symbols` `/ping` `/id`（9 条） |
| `ops`（内部运营台） | `TELEGRAM_TRADE_CHAT_ID` / `TELEGRAM_OPS_CHAT_IDS` | `/start` `/help` `/levels` `/scan` `/top` `/risk` `/status` `/positions` `/orders` `/watches` `/protection` `/capacity` `/gates` `/frame` `/universe` `/notify` `/system` `/host` `/disk` `/doctor` `/logs` `/symbols` `/id`（23 条） |

- `mo tg-sync` 在写入前先清理 `default` / `all_private_chats` / `all_group_chats` / `all_chat_administrators` 及已配置 chat 的 `""`/`zh`/`en` 三种语言残留，再逐一 `getMyCommands` 复核。
- `tg-run` 启动时自动同步菜单（`--no-sync` 可跳过）；未授权 chat 仅允许 `/id` 以便运维获取 chat id。

---

## 目录布局、看护与计划任务

### 目录布局 (`market_operator/paths.py`)

运行期路径优先级：`MARKET_OPERATOR_LAYOUT` > `MARKET_OPERATOR_DATA_ROOT` > `config/paths.json` > `var/.mo-layout.json` > `legacy`。
- `legacy` 布局：使用仓库根下 `data/`、`logs/`、`runtime/`、`reports/`、`backups/`；
- `portable` 布局：全部运行态收敛至 `var/{data,logs,runtime,reports,backups}/`，代码与配置目录（`src/`、`config/`、`scripts/`、`tools/`）始终锚定部署根。
- 已有实盘切换布局请使用 `mo migrate-data`（逐文件 `sha256` 校验通过后才切换）。

### 心跳判活与自愈 (`heartbeat.py` / `scripts/live_supervisor.py`)

判活唯一事实源为 `runtime/live/heartbeat.json`：

| 心跳状态 | 判定 | 看护动作 |
|---|---|---|
| 文件不存在 | 未运行 | 按重启预算拉起 |
| `age <= supervisor.ok_seconds` (120s) | 正常 | 无 |
| `ok < age <= stale_seconds` (300s) | 迟到 | 仅记录，避免误杀 |
| `status=starting` / `recovering` 且未超上限 | 启动/补做中 | 等待完成 |
| `age > stale_seconds` | 卡死/已退出 | 终止残留并重启 |
| 超出 `max_restarts_per_hour` (6) | 需人工介入 | 仅告警，不再拉起 |

- **进程内守护 (`HeartbeatGuard`)**：主循环停摆超过 `stall_seconds` (180s) 时仅补挂缺失的交易所原生止损单（只保护不主出）；持续卡死达 `self_fence_seconds` (900s) 且有持仓时以退出码 `3` 主动自熔断，交由看护拉起并触发掉线缺口补做。
- **沙盒隔离规范**：测试或预演构造 `LiveRunner` 时必须显式传入独立的 `state_path` 或 `heartbeat_path`，避免覆盖生产 `runtime/live/heartbeat.json`。

### 存储治理与 Windows 计划任务

- 存储治理（`scripts/storage_gc.py`）坚持"只删字节不删信息"：历史审计 JSONL 压缩为 `.gz`、控制台日志先校验归档再保留尾部、备份打包为 zip；`data/raw/`、`config/` 与热运行档（`runtime/live/*.json`、`runner.lock`）受强制保护。
- `mo tasks` 注册 3 条 Windows 计划任务（路径 `\MarketOperator\`）：
  1. `MarketOperatorLive`：每 3 分钟检查拉起常驻看护（`pythonw.exe scripts/live_supervisor.py --config config/live.json`）；
  2. `MarketOperatorStorageGC`：每日 `04:10` 执行 `tools/schedule/storage_gc.cmd`；
  3. `MarketOperatorFunding`：每周日 `07:30` 执行 `tools/schedule/fetch_funding.cmd`。
- 如需修改 `tools/schedule/*.cmd`，请先暂停任务并执行 `python scripts/mo.py stop`，修改完成后再重新启用任务，避免运行中的 `cmd.exe` 按字节偏移重读旧行。

---

## 数据证据与许可证

- 历史离线基准数据指纹固定于 [`data/manifest.json`](data/manifest.json)（逐文件 `sha256` 校验），合约规格缓存于 `data/contracts.json`。
- 本仓库全部核心代码为原创实现，采用 MIT 许可证（见 [`LICENSE`](LICENSE)）。
