# market-operator

模拟专业交易者决策过程的加密货币研究与实盘交易系统：因果市场认知（平衡箱体与波段结构） → 事件原语（ACCEPT / REJECT 及 E1–E10） → 可证伪交易假设 → 事件驱动持仓管理 → 全成本真实回测与 Bitget 多周期实盘引擎。研究与实盘标的：Bitget USDT 永续（核心 BTC/ETH/SOL/DOGE + 全市场 >3M USDT 活跃宇宙），多尺度（2h/4h/12h/1d 混合触发）。

**当前状态:平衡箱统一架构与实盘引擎已就绪(`config/strategy.json` / `config/live.json`)。** 策略等价性使用 `tools/verify_strategy_parity.py --ref <commit>` 直接对照指定提交的逐笔交易、净值与计数器;历史收益下界验收仍由 `tools/replay_acceptance.py` 提供,两者判据不同。协议出处见 `docs/v21-protocol.md` 与 `docs/acceptance.md`。


## 设计原则

- **时间即真相**：每个结构事实携带 `forming_index`（指向位置）与 `known_index`（可被知道的位置），任何决策只使用 `known_index <= t`。前缀不变性与"增量==批量"由测试锁定。
- **结构来自价格关系**：波动率自适应确认波段（ATR）、关键价格区域生命周期（ACTIVE/BROKEN/RETESTED/FLIPPED/DEAD)、regime 由 pivot 序列判定——不依赖方向性指标。
- **可证伪**：事件定义、参数先验、GO/NO-GO 判据在研究前冻结（`docs/methodology.md`、各轮 `docs/*-protocol.md`、`config/*.json`)，研究过程全程记录（`docs/research-log.md`)。
- **每个决策可解释**：`trades_*.csv` 的 reason 字段可追溯到方法论条款与事件 id。

## 唯一规范语义链（canonical)

系统当前只说一种语言（术语权威定义见 **`docs/SEMANTICS.md`**):

```
data ──► structure ──► events(E1-E7) ──► hypothesis ──► routing ──► position(§8)
抓取/校验/    ATR/确认波段/    事件目录      可证伪       假设→计划     持仓管理六条;
sha256 校验   区域生命周期/    (因果知时)    交易假设     (turtle)      turtle 接线
manifest     regime
                                                                    │
aggregate(多 TF 聚合) ◄──────────────────────────────────────────────┘
        │
        ▼
     broker(撮合/费用/资金费/强平约束;OrderIntent/OrderKind 规范家园)
        │  ▲
        │  └── capacity(风险预算的唯一口径:准入与取量共用一个公式)
        ▼
     campaign(多尺度战役引擎:symbol×TF 各自独立战役)
        │
        ▼
     replay(规范回放核心)──► scenario(场景门面)──► tools/replay_acceptance.py
                                                 └─► scripts/live_runner.py(实盘)
```

- **规范策略 = `market_operator.routing`**:v5 质量路由（E1 市价优先 /
  E6 STRONG 市价、WEAK→同 episode E7 前缘限价）× turtle 出口几何（无固定
  目标、初始防线 2.0·atr、trailing 只紧不松、关时间止损）。v6 协议的 A/B
  二臂判选已终局（臂 B 判选；记录见 `docs/v6-protocol.md` 原文与
  `reports/backtest_v6.json/.md` 冻结产物）,**臂切换与臂 A clamp2R 实现
  已移除**,历史实现通过 Git 历史追溯,不在当前源码树保留副本。
- **认知与决策主链**:`data`、`aggregate`、`structure`、`true_balance`、`events`、`hypothesis`、`routing`、`position`、`broker`、`capacity`、`campaign`、`replay`、`split`、`trigger`、`scenario`;实盘入口另接执行、通知与运维模块。
- **退役实现已删除**:历史实验模块、专属脚本与配置不再保留运行副本。`tests/test_layering.py` 约束生产闭包不得引用退役模块、模块名不得含版本号,并检查源码、测试和工具中的包内导入是否仍可解析。
- **执行配置**:代码使用 `campaign.ExecutionConfig`,JSON 保持 `arm_config` 这个已发布键不变;历史 stop-entry、虚拟成交模式、分层入场和收割实验开关已删除。观察挂单仍使用原有 `virtual_limit` 账本标识与计数器,这是活跃输出契约,不是可删除的实验模式。



## 平衡箱统一架构与实盘引擎(当前活跃架构)

详见 **`docs/v21-protocol.md`**、**`config/strategy.json`** 与 **`config/live.json`**：

- **平衡箱识别（`true_balance.py`）**：由确认波段定框,维护价值边界与外缘双层结构,经水平性与中枢校验生成现价周围的唯一活跃支撑/阻力夹板,并桥接到区域生命周期与事件链。
- **双原语与高阶降档混合触发（M3c）**：
  - `ACCEPT`（突破接受，顺趋势迁移）与 `REJECT`（刺穿拒绝，箱内回归）；
  - 具体认知周期与触发周期映射以 `config/strategy.json` 的 `trigger_tf_by_htf` 为准;结构冲量确认与区域离开重置分别由 `struct_impulse_confirm`、`zone_departure_reset` 控制,不再使用旧固定冲量阈值与冷却根数；
  - `12h -> 2h`、`1d -> 4h` 降一档细周期触发，兼顾大箱体高胜率与早入场高盈亏比。
- **分类止损与对侧突破锁利**：`REJECT` 采用箱体外缘结构止损，触及或突破对侧缘 `box_opposite` 时**不平半仓**，而是把防线交给"**回吐预算**"（v21.7：`budget = (1−|frac|)·|target−entry|`，`stop = peak ∓ budget`，`peak` 为入场以来最有利极值）—— 推进可多次棘轮、回吐超预算立刻市价离场，不再把仓位钉在一个落后于行情的绝对价位上（`reject_opp_lock_frac=-0.25` 即"锁 ¼、让 ¾"，`-0.50` 为"锁一半"；标定见 `docs/v21-protocol.md` §5.1）；剩余仓位交由 §8.3 确认 pivot 移动防线收割；`ACCEPT` 采用 2.0·ATR 初始防线与无上限移动防线。
- **各周期独立风险容量池与阶梯动态复利**：`tf_cap_by_tf` 为 `2h`/`4h`/`12h`/`1d` 分配独立风险预算，防止短周期仓位挤占大周期高胜率机会；实盘支持 104 USDT 起步 × 20x 杠杆 × 四档净值阶梯动态风险（`<=250U:4%`, `<=600U:3%`, `<=1500U:2%`, `>1500U:1%`）与 Telegram 信号/运营双通道高清黑白 K 线结构图推送（信号广播与容量闸门解耦：公开信号频道只发市场结构与 `exec_status` 处置说明，成交/防线推进/平仓/日结/告警全部走运营通道）。

## 历史轮次归档（research/)

v1-v5 各轮与 v6 双臂 A/B 快照、PR#9/#10 前向设施已按轮次归档至
**`research/rounds/{v1..v6,external}/`**（模块 + 运行脚本 + 对应测试，
仅 import 路径修正，逻辑零改动）；对应关系（轮次 ↔ 协议 ↔ 冻结报告 ↔
复现命令）见 **`research/README.md`**。

## 复现与运行

```bash
pip install -e ".[dev]"
python -m pytest -q                       # 634 passed / 1 skipped(全量回归 + 各轮历史测试)

# v21 平衡箱与多周期混合触发回测诊断
python scripts/_diag_v21_backtest.py

# v21 实盘引擎 paper 冒烟校验与实盘启动
python scripts/live_paper_check.py
python scripts/live_runner.py --mode paper
python scripts/live_runner.py --mode live

# 实盘运维(只读自检 / 缺口补做预演 / 常驻看护 / 存储治理)
python scripts/live_status.py            # 本地↔交易所↔保护单三方核对,不一致时退出码 1
python scripts/live_gap_preview.py       # 只读预演掉线缺口补做(所有写操作被拦截)
python scripts/storage_gc.py             # 存储盘点 + 预算判定(默认只读)
python scripts/storage_gc.py --apply --dedupe-gz   # 安全清理(日志/备份/缓存/无损去重)
powershell -File scripts/live_watchdog.ps1 -Action register   # 注册每 3 分钟拉起的计划任务(目录同样经 paths 解析)

# 规范基线回放(产物 reports/backtest_v6_turtle.*)
python scripts/run_backtest_v6.py

# 历史轮次(覆盖各自同名历史报告,详见 research/README.md)
python research/rounds/v1/run_backtest.py
python research/rounds/v2/run_backtest_v2.py
python research/rounds/v3/run_backtest_v3.py
python research/rounds/v4/run_backtest_v4.py
python research/rounds/v5/run_backtest_v5.py
python research/rounds/v6/run_backtest_v6.py   # v6 A/B 双臂(复现判选)
```

## 运维台 `mo`(跨平台一条命令)

`scripts/mo.py`(等价 `python -m market_operator`,装包后直接 `mo`)把启停 / 自检 /
对账 / 日志 / 打包 / 迁移收敛到同一张命令表:所有子命令都支持 `--json`,
Telegram 运营命令(`market_operator/tg_commands.py`)与人读输出共用同一份数据
(两者都只调 `ops`,不 import `live_runner`)。

```bash
python scripts/mo.py bootstrap --update --layout auto  # 更新→布局→自检→启动→暖机→对账→TG
python scripts/mo.py update [--check]             # git pull --ff-only + pip install -e .(拷贝部署自动跳过)
python scripts/mo.py doctor [--exchange]          # 环境自检(默认不联网)
python scripts/mo.py start | stop | restart       # 看护 → runner 整条链(restart 连 TG 一起拉回)
python scripts/mo.py status                       # 进程/心跳/持仓/权益/计数器
python scripts/mo.py positions [--no-exchange]    # 本地账本 ↔ 交易所持仓
python scripts/mo.py logs [live|err|sup|tg] -n 80 # 日志尾部(--list 列全部)
python scripts/mo.py paths                        # 当前布局与各类目录占用
python scripts/mo.py package -o backup.zip        # 运行期产物 zip + sha256
python scripts/mo.py migrate-data --dry-run       # 旧目录 → var/ 预演
```

一台全新机器只需要 `bootstrap.cmd` / `bootstrap.sh`(内部就是 `mo bootstrap --update`):

* **更新**:有 `.git` 就在工作区 `fetch` + `pull --ff-only`(分叉/本地改动一律失败,
  绝不自动 merge/reset),随后 `pip install -e .`;代码是文件夹拷贝(没有 `.git`)则
  `skipped`,不报错。`--check` 只 fetch 并报告落后几个提交(没有上游跟踪分支时在
  `notes` 里说明"无法比较 behind",不会假装已是最新),不动工作区。更新失败时
  **不会带着旧代码启动**(退出 1,信息里给出 `--force` 以便确认后跳过);输出/日志
  里的 remote 凭据(`https://user:token@host`)一律脱敏。
* **更新到新代码后进程还在跑旧模块**:`bootstrap --update` 发现代码有变化且服务在跑
  时会先 `stop` 再继续,后面的 `start` 一定是新代码。
* **目录**:所有数据/日志目录由 `paths.ensure_layout()` 自动创建,不存在就建,
  不需要手工 `mkdir`(详见下面「目录布局」)。
* **TG**:`bootstrap` / `restart` 结束时会显式拉起 TG 机器人(幂等;没有 token 按
  `skipped` 处理,不影响实盘链路);`--no-tg` 可跳过,`--tg-wait` 控制等待秒数。

### Telegram 交互命令(只读运维台)

两个机器人、两套菜单,由一个常驻进程长轮询提供服务。命令全部**只读**:启动 / 停止 /
改配置 / 下单都不开放,`/help` 里也写明"写操作只在服务器执行"。

```bash
python scripts/mo.py tg-sync             # 写菜单:先清全局+逐 scope 旧残留,写完自校验
python scripts/mo.py tg-sync --dry-run   # 只看现状(每个 scope 当前几条、要写几条)
python scripts/mo.py tg-run              # 常驻长轮询;`--once` 只拉一轮(冒烟)
```

| 机器人 | 角色 | 菜单 |
|---|---|---|
| `@lian_sniper_alert_bot` | public · 信号频道 | `/start` `/help` `/symbols` `/ping` `/id` |
| `@LianowoBot2_bot` | ops · 运营台 | 上述 5 条 + `/status` `/positions` `/orders` `/system` `/host` `/disk` `/doctor` `/logs` |

`/logs` 接受参数:`/logs [live|err|sup|tg] [行数]`;`/orders` 只列**在挂**的委托与保护
止损(`armed`),空仓时明确说"没有在挂"。运营命令只对
`TELEGRAM_TRADE_CHAT_ID` / `TELEGRAM_OPS_CHAT_IDS` 白名单内的 chat 生效,未授权的
chat 只有 `/id` 可用(先让它报出 id,再写进配置重启机器人);公开 bot 里发运营命令
直接回"该命令不属于此机器人"。

`tg-sync` 的清场范围包含**全局 scope**(`default` / `all_private_chats` /
`all_group_chats` / `all_chat_administrators`)与已知 chat 的 `chat` /
`chat_administrator` scope,每种各清 `""`/`zh`/`en` 三种语言:只清 `default` 时,
老客户端缓存的全局菜单仍会弹出来 —— 那等于"点得到但无权执行"。全局 scope 只清不写
(菜单只落在 `default` 与配置里的 chat),清完逐个 `getMyCommands` 复核,清不掉的
残留会进 `residual` 并让 `--json` 的 `ok=false`。

三件事一起看:`tg-run` 是**独立常驻进程**,看护不监管它 —— 但 `mo bootstrap` /
`mo restart` 结束时会显式把它拉起(幂等、没有 token 按 `skipped` 处理,`--no-tg`
可跳过),`mo stop` 会停,`mo status` 的 `tg-bot pid/alive` 行能看出死活;菜单在
`tg-run` **启动时自动同步**(清场 → 写入 → 自校验,`--no-sync` 可跳过),所以改完
`SPECS` 重启机器人就生效,`mo tg-sync` 用于"不重启、只刷菜单";升级后第一次启动也
不需要人工再跑一次注册;两台机器/两份进程同时 `getUpdates` 会被 Telegram 判 409,
offset 落 `runtime/live/tg_bot_state.json`,只允许一个实例(单例锁 `tg_bot.lock`)。

### 目录布局(`market_operator/paths.py`)

运行期目录有唯一事实源,不再硬编码仓库根:默认 `legacy`(仍是 `data/ logs/
runtime/ reports/`,行为零变化);全新主机 `bootstrap` 自动切 `portable`
(`var/{data,logs,runtime,reports,...}`)。切换优先级:
`MARKET_OPERATOR_LAYOUT` > `MARKET_OPERATOR_DATA_ROOT` > `config/paths.json` >
`var/.mo-layout.json` 标记 > legacy。已有实盘换布局必须显式 `mo migrate-data`
(复制 → 逐文件 sha256 → 全部通过才写激活配置;`--move` 只删已校验且未被 git
跟踪的源文件)。`paths.resolve()` 只改写运行期类别,`config/`、`scripts/` 永远锚定
部署根;写入严格,读取(`resolve_existing`)可在迁移未完成时回退旧目录 —— 不会把
state 劈成两半。

runner / 看护 / `mo` / TG 机器人全部走同一套 `resolve()`,portable 下心跳、state、
日志只有一份;`live_supervisor.py` 自己登记 `runtime/live/supervisor.pid`,
因此 `mo status|stop` 对"非 mo 启动"的看护同样有效。

`scripts/storage_gc.py` 同样以 `paths` 为准:物理路径经 `paths.store_root()` 解析,
白名单 / 热档判据 / manifest 里存的都是**逻辑相对路径**(`logs/x.log`、
`runtime/live/state.json`),两种布局写法一致,portable 下 `var/logs`、`var/runtime`
一样有人治理,`--restore` 也能跨布局还原(legacy 行为逐字不变)。
`tools/schedule/*.cmd` 先 `for /f` 问一次 `paths.log_dir()` 再 `mkdir`/重定向,
因此 portable 下不会再凭空多出一个空的 `logs\`;python 不在 PATH 时才退回
`if not defined LOGDIR set "LOGDIR=logs"`。四个入口都有护栏禁止回退
(`scripts/live_*.py`、`scripts/*.ps1`、`scripts/storage_gc.py`、`tools/schedule/*.cmd`)。

## 实盘运维(看护与存储治理)

判活口径只有一条:**`runtime/live/heartbeat.json` 里的时间事实**。不看进程名、
不看端口、不看计划任务的"上次运行结果"(2026-10-22 的掉线事故,根因正是这些
间接证据在换机器/换系统后全部失效,而交易所侧止损已被打掉、本地仍在按旧状态记账)。

| 心跳事实 | 结论 | 看护动作 |
|----------|------|----------|
| 文件不存在 | 没在跑 | 拉起(受重启预算约束) |
| `age <= supervisor.ok_seconds` | 正常 | 无 |
| `ok < age <= stale` | 迟到 | 只记录(宁可漏杀,不可错杀) |
| `status=recovering` 且 `age <= recover_max` | 掉线补做中 | 等(补做要重放几千根 bar) |
| `age > stale` | 卡死 / 进程没了 | 终止残留 → 拉起 |
| 重启预算耗尽 | 需人工 | 只告警,不再拉起 |

```bash
python scripts/live_supervisor.py --status    # 体检:心跳年龄/判定/重启预算
python scripts/live_supervisor.py             # 常驻看护(runner 由它拉起,自带每日存储治理)
python scripts/live_supervisor.py --once       # 单次判定(交给计划任务每 5 分钟触发)
python scripts/live_supervisor.py --once --no-start   # 演练:只告警不拉起
python scripts/live_restart.py                # 改完代码后换进程(不等看护 300s 窗口,立即拉起并确认心跳)
python scripts/live_status.py                 # 人读快照:持仓/挂单/缺口/心跳
python scripts/storage_gc.py                  # 只读盘点 + 增长预测
python scripts/storage_gc.py --enforce        # 执行安全清理 + 预算闸门(0/1/2)
python scripts/storage_gc.py --restore --apply # 按 manifest 还原被压缩文件
```

进程内还有一条独立的保护线(`src/market_operator/heartbeat.py: HeartbeatGuard`):
主循环停摆超过 `heartbeat.stall_seconds` 后,心跳线程仍按 `write_seconds` 落盘并
**只保护不主出** —— 核对交易所原生 TPSL 止损单(`pos_loss`),缺失则按本地防线补挂;停摆超过
`self_fence_seconds` 且仍有持仓,则以退出码 3 自我熔断,由看护拉起并走掉线补做
(交易所侧止损是最后一道墙,带病运行只会持续错过止盈/止损)。runner 与看护各自的
单实例锁是内核文件锁:进程死亡即释放,崩溃不会留下毒锁,也不会出现两个进程同时下单。

**心跳文件归属(2026-10-24 修复)**:runner 的心跳路径决议为
`heartbeat_path` 显式参数 > 显式 `state_path` 的同目录 > `loop.heartbeat_path`(锚定仓库根)。
修前只认配置里的相对串、且按 cwd 解析:在仓库根跑一次测试,测试构造的 runner 就会把
**生产** `runtime/live/heartbeat.json` 覆盖成 `paper`/初始资金/0 仓位;看护只认这一个文件,
而测试合成时钟可能在未来(`age < 0` ⇒ 恒判"正常"),真正掉线的 runner 便永不重启。
**所以测试/沙盒必须传 `state_path`(或 `heartbeat_path`)**,`runtime/live/` 只许唯一的实盘进程写。
详见 `reports/heartbeat_path_leak.md`。

存储治理同样只"删字节不删信息":审计 JSONL 只做明文 → `.gz`(`--log-days 3`),
`.gz` 默认永不删除(`--log-max-days 0`),控制台日志先整份 gzip 归档并校验通过才
截断(尾部 4MB 留作即时排障),备份目录打成 zip 后才删;`data/raw`、`config/`、
热档(`runtime/live/*.json`)、研究冻结产物一律不动。增长预测只按
`logs/ runtime/ data/` 的写入速率外推(一次性研究产物混进来会把"还能跑多久"估得
离谱),结果落 `reports/_storage_growth.json`。

计划任务:`\MarketOperator\MarketOperatorLive`(每 3 分钟重复、忽略重叠)与
`\MarketOperator\MarketOperatorStorageGC`(每日 04:10),动作都是
`C:\Windows\System32\cmd.exe /c tools\schedule\<入口>.cmd`、工作目录 = 仓库根。
三条踩过的坑(都是"任务看起来跑过,但什么都没发生"):

1. **执行体写绝对路径**。计划任务的解析环境未必有 `PATH`,写 `cmd.exe`/`python`
   会解析不到而静默失败;解释器由 .cmd 内部调用(必要时设 `PYTHONUTF8=1`)。
2. **入口脚本里不要做重定向**。正在运行的实例会独占自己的日志句柄,3 分钟后的
   下一次触发再 `>>` 同一个文件,会让 `cmd` 在**打开文件阶段**就失败:退出码 0、
   日志一个字都写不出来。所以常驻看护自己把 console 行镜像进
   `logs/supervisor.log`(异常记为 `kind=crash`),.cmd 只负责 `cd` 到仓库根再调
   python;`live_runner.cmd` / `storage_gc.cmd` 仍重定向,但写的是**无人长期持有**
   的 `logs/live_runner_console.log`、`logs/storage_gc.log`(目录先向
   `paths.log_dir()` 问,portable 下自动变成 `var/logs/...`)。
3. **重复触发靠单实例锁变成"没跑就拉起"**。第二次触发的新实例抢不到
   `runtime/live/supervisor.lock` 就打印一行并 `exit 0`,既不影响在跑的实例,
   也不需要 `IgnoreNew`/上次结果之类的巧妙判断。
4. **不要在实例运行中热改 `.cmd`,改完按"停 → 改 → 起"走**。`cmd.exe` 是**按字节
   偏移逐行读取**批处理文件的:文件在执行途中被改写,它下一次读取就落在错位处,把
   已经执行过的行再跑一遍。2026-10-25 踩到:在 `live_supervisor.cmd` 的 python 调用
   行前面插了几行 `rem`,旧 `cmd.exe` 的偏移因此落回那行 —— `mo stop` 杀掉看护 4 秒
   后又静悄悄起了一个,父进程仍是前一天启动的 `cmd.exe`(心跳/预算看起来都正常,
   只有进程树能看出来)。顺序照抄:

   ```bat
   Disable-ScheduledTask -TaskPath '\MarketOperator\' -TaskName MarketOperatorLive
   python scripts\mo.py stop
   rem ...改 tools\schedule\*.cmd...
   Enable-ScheduledTask  -TaskPath '\MarketOperator\' -TaskName MarketOperatorLive
   Start-ScheduledTask   -TaskPath '\MarketOperator\' -TaskName MarketOperatorLive
   ```


```bat
C:\Windows\System32\cmd.exe /c tools\schedule\live_supervisor.cmd
C:\Windows\System32\cmd.exe /c tools\schedule\storage_gc.cmd
```

人工排障直接带参数调入口(此时不写日志、直接打屏):
`tools\schedule\live_supervisor.cmd --status --no-notify`。

数据固定证据：`data/manifest.json`（每批原始数据 sha256, 2024-10-15 → 2026-10-19, 0 缺口）；合约规格离线缓存强制（`data/contracts.json` 逐条 sha256)。

## 研究演进与结论快照（详见 `docs/acceptance.md` 与 `docs/v21-protocol.md`)

- **v1–v20（单带区域家族与订单流证伪）**：单带 S/R 区域模型下历经二十轮归因与改造（v18 首获 dev 正期望、v19 首获 test 微正，v12 K线 delta 与 v19.5 Binance 2.195 亿笔逐笔 aggTrades 均证伪公开订单流增量），详见 `docs/acceptance.md` 与 `docs/research-log.md`。
- **v21（平衡箱统一架构）**：将认知底座切换为 `BalanceDetector` 高置信度平衡箱，配合 `ACCEPT`/`REJECT` 双原语、M3c 高阶降档混合触发、对侧突破锁利 50% 与各周期独立容量池，在 `2h`/`4h`/`12h`/`1d` 四周期及 `dev`/`test` 全成本压力下实现正期望（300U 固定基准 full@1.0x 净 +575.3 USDT；104U/20x 阶梯复利回测净值达 2,523.7 USDT，MaxDD 15.8%），已同步接入实盘引擎。


## 来源与许可证

本仓库代码全部为原创实现，项目许可证 MIT（见 `LICENSE`)。研究中借鉴开源算法之处均已在 `docs/audits/` 记录来源/许可证/差异：pymarket-structure(MIT)、TapeFlow(MIT) 借鉴了思想级原语；quant-trading（专有许可）与 OrderFlow-absorption（无 LICENSE 文件）仅借鉴思想，无任何代码复制。
