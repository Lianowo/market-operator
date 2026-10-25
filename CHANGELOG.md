# Changelog

## [Unreleased] — 2026-10-24 — 跨平台运维层:`mo` 命令台 + 运行期目录唯一事实源

两件事同源:**换机器/换系统时,"路径"和"判活"都不该再靠人记**。修前 `scripts/
live_*` 里 `ROOT / "runtime"` / `ROOT / "logs"` 硬编码在十几处,一旦把产物挪出
仓库(打包部署、`var/` 布局),runner 写的心跳、看护读的心跳、CLI 报的心跳就会是
三个不同文件 —— 判活永久"缺失",看护反复拉起;运维还要记一堆脚本名和参数。

- **新增 `market_operator/paths.py`(运行期目录唯一事实源)**:`layout_mode()`
  按 `MARKET_OPERATOR_LAYOUT` > `MARKET_OPERATOR_DATA_ROOT` > `config/paths.json`
  > `var/.mo-layout.json` 标记 > legacy 默认解析;`resolve()` 只改写运行期类别
  (`data/logs/runtime/reports` 等),`config/`、`scripts/` 永远锚定部署根;
  `resolve_existing()` 只在"读"时回退旧目录,写路径始终严格 —— 迁移未完成时不会
  把 state 劈成两半。`legacy` 行为零变化。
- **`migrate_legacy()` 是可审计的迁移**:默认只搬 `logs`/`runtime`(`data` 需显式
  `--include-data`),逐文件复制 + sha256 比对,**全部通过才**写 `var/MIGRATION.json`
  与激活配置;心跳新鲜时拒绝迁移(`--force` 可演练);`--move` 只删除已校验且未被
  git 跟踪的源文件。`activate_legacy()` 保留 `var/` 数据、删标记,可随时回滚。
- **新增 `mo` 运维台(`cli.py` + `scripts/mo.py` + `__main__.py`)**:`bootstrap /
  doctor / start / stop / restart / status / positions / warmup / logs / tg-sync /
  tg-run / package / migrate-data / paths`,全部支持 `--json`。`doctor` 默认不联网
  (可选 `--exchange` 做公共行情 + 只读账户);`warmup` 是沙盒导入,不写生产 state;
  `package` 产出 zip + sha256 清单并可用 `--verify` 复核。
- **`ops.py` 收敛进程/心跳/日志/打包动作**:`live_paths()` 一张表给出 state、
  campaigns、heartbeat、candles、supervisor pid/lock/state、tg pid/state、日志;
  启停一律"先停看护再停 runner"(否则看护会把 runner 立即拉回来);`redact()` 保证
  错误信息不泄露密钥。修掉三处真 bug:`stop_all()` 的兜底 `find_pids(
  "scripts/live_runner.py")` 在 Windows 上永远匹配不到(`\` 与 `/` 的差别),
  改成按文件名 `live_runner.py` 匹配;`find_pids()` 会**匹配到自己的探针**
  (`powershell -Command "... -like '*live_supervisor.py*'"` / `sh -c 'pgrep -f …'`),
  `mo status` 因此报出一个转瞬即逝的 powershell pid 当看护(alive=True),
  `mo stop` 还可能去杀它 —— 现在按镜像名/壳名排除探针;`mo` 在 cp936 控制台
  (Windows 默认、未设 `PYTHONUTF8`)遇 emoji 会 `UnicodeEncodeError`,被 `main()`
  兜底成一行 `ERROR ...` 并退出 1,`_soften_stdio()` 只放宽 `errors="replace"`
  (中文照旧、个别 emoji 退化成 `?`),`mo status` 不再因为一个箭头整条失败。
- **`supervisor_pid()` 只认看护自己**:`supervisor.json` 的 `spawned_pid` 是看护
  拉起的 **runner 子进程**,修前被当成看护 pid —— `mo status` 把 runner 显示成看护、
  `mo stop` 只杀 runner(看护下一轮又拉起来,看起来「停不掉」)、`mo doctor` 在
  「看护已死但 runner 还活着」时误报健康。现在先读 `runtime/live/supervisor.pid`
  (新版看护自己写),旧版没写该文件时按进程名 `live_supervisor.py` 兜底。
- **看护的重启预算修成真的**:`run_once()` 原先把 `decision.kind` 判在
  `decision.action` 之前,而预算耗尽时 `decide()` 返回的是 `action="give_up"` +
  `kind=stalled/missing` 并存 ⇒ `give_up` 分支成了死代码,崩溃循环里看护一小时
  重启 19 次(配置上限 6),「崩溃循环不会刷成无限重启」的承诺被刷没了。现在先认
  `give_up`(只告警、不杀不拉),起不来就交人工;新增
  `test_supervisor_give_up_does_not_spawn` 钉住「一个进程都不许拉」。
- **`tg_commands.py` 与 CLI 共用 `ops`**:两套机器人菜单(`public` / `ops`)写入前
  用 `validate_specs()` 校验命令名与描述,运营命令不落公开 Bot。修掉一处真 bug:
  `_pos_rows()` 直接迭代心跳里的 `positions`(那是**数量**,逐笔明细在
  `positions_detail`),一旦有仓位 `mo positions` / `/status` 就 `TypeError`;
  现在优先读明细,缺失时回退本地账本。
- **`scripts/live_runner.py` / `live_status.py` / `live_restart.py` /
  `live_supervisor.py` 改走 `paths`**:state/心跳/journal/candles/tg_missed 与
  凭据路径全部经 `paths.resolve()`(只读场景用 `resolve_existing`);看护自己登记
  `runtime/live/supervisor.pid` 并在退出时清除 —— 手工或计划任务启动的看护,现在
  `mo status|stop` 也认得出来。`live_restart.py` 以绝对路径拉起 runner、`cwd`
  用 `app_home()`,不再依赖调用者的工作目录。
- **测试 `tests/test_ops_paths.py`(22 例)**:布局优先级、`resolve` 只改写运行期
  类别、`resolve_existing` 迁移前回退、`live_paths` 在 legacy/portable 下与 runner
  同源、激活可逆、迁移 dry-run 零写入 / 校验后激活 / 心跳新鲜拒绝迁移、
  `ensure_layout()` 幂等并补齐 `live/candles`、菜单唯一性、`_pos_rows` 不再迭代
  int、`supervisor_pid` 不再把 `spawned_pid` 当看护、`find_pids` 不认自己的探针、
  cp936 控制台下 `mo status` 不再被 emoji 打断、CLI 子命令全部可解析且不重名、
  `status_snapshot` 报出的就是 runner 写的文件;四条硬编码护栏(`scripts/live_*.py`、
  `scripts/*.ps1`、`scripts/storage_gc.py`、`tools/schedule/*.cmd`)。**另注:`heartbeat.read_heartbeat()` 只认带 `schema`/`ts_ms`
  的文档**(否则返回 `{}`),测试里也按同口径构造心跳。
- `.gitignore` 增 `var/` 与 `config/paths.json`(布局含本机绝对路径,属部署事实);
  `pyproject` 增 `[project.scripts] mo = "market_operator.cli:main"`。
- **已迁移的脚本**:`live_runner / live_status / live_restart / live_supervisor /
  live_gap_preview / live_paper_check / live_cost_audit`,以及 Windows 垫片
  `scripts/live_watchdog.ps1`(改为向 `paths.resolve('runtime'|'logs')` 问一次,
  探测失败才退回旧路径;判活同时改为优先 `heartbeat.json` 的 `ts_ms`,旧部署无
  心跳文件时回退 `state.json saved_at_utc`)。两条护栏防回退:
  `test_live_scripts_do_not_hardcode_runtime_dirs` 扫 `scripts/live_*.py`,
  `test_ops_shims_do_not_hardcode_runtime_dirs` 扫 `scripts/*.ps1`(仅放行显式兜底行)。
- **`scripts/storage_gc.py` 也走 `paths`(收尾项)**:盘点 / 清理 / manifest 原先把
  `logs`、`runtime`、`reports`、`data` 前缀钉在仓库根,portable 布局下 `var/logs`
  **无人治理** —— 而且不报错,预算闸门只会一直显示「宽裕」。现在物理路径经
  `paths.store_root()` 解析,而白名单、热档判据与 manifest 里存的是**逻辑相对路径**
  (`logs/x.log`、`runtime/live/state.json`),两种布局写法一致,`--restore` 因此可
  跨布局还原;`inventory()` 在 portable 下把 `var/` 拆成 `var/logs`、`var/runtime`
  等分别列出(否则增长源被一个 `var` 吞掉)。legacy 行为逐字不变。
  `tests/test_storage_gc.py` 增 4 例(现 11 例):`op()/op_rel()` 换算可逆、
  portable 下 `var/logs` 真的被压缩而 legacy 残留目录不动、`inventory()` 按类别
  拆分、manifest 不带 `var/` 前缀且能还原。
- **`tools/schedule/*.cmd` 不再硬编码 `logs`**:原先是 `if not exist logs mkdir logs`
  + `>>logs\*.log`,portable 下会凭空建一个空 `logs\`,脚本自己的日志也写到没人
  盘点的位置。现在先 `for /f` 问一次 `paths.log_dir()`(legacy ⇒ `logs`、portable
  ⇒ `var\logs`),python 不可用时才落到 `if not defined LOGDIR set "LOGDIR=logs"`
  这一行兜底(与迁移前逐字相同)。护栏 `test_schedule_cmds_resolve_log_dir_via_paths`
  扫 `tools/schedule/*.cmd`,`test_storage_gc_uses_paths_for_runtime_dirs` 扫
  `scripts/storage_gc.py`。
- **运维留档(2026-10-25 实盘,替换看护进程)**:`runtime/live/supervisor.json` 里攒了
  19 条重启记录(全是修复前 197s 崩溃循环的产物),把 1 小时窗口的预算耗到 0 —— 新
  看护一启动就会 `give_up`,再断网只告警不拉起。处理:旧记录归档到
  `restarts_archived_20261025T062137Z`、`restarts` 清空,并写一条
  `actor=operator / kind=budget_reset` 审计(含 `left_before=1 → left_after=6`);
  `spawned_*`/`last_restart_s` 一并清掉(指向已死的 47420)。随后用看护自己的
  `start_runner()` 把 runner 拉回(占 1 次预算,余 5),再由计划任务以 `IgnoreNew`
  属性接管进程树。审计三行同见 `logs/supervisor.jsonl`(`budget_reset`、
  `started`, `manual_start`);持仓 9 笔 / 挂单 51 / 止损单 9 前后一致,`mo doctor`
  `ok=10 warn=1 fail=0`。
- **README 增第 4 条 `.cmd` 坑**(本次事故真因):`cmd.exe` 按**字节偏移**逐行读取
  批处理,实例运行中改写 `.cmd` 会让它重跑已执行过的行 —— `mo stop` 后 4 秒又冒出
  一个看护,父进程是前一天启动的 `cmd.exe`。入口脚本必须"停实例 → 改 → 再启动",
  命令序列已写进 README。
- **TG 交互命令补实测与两处修正(2026-10-25)**:两个 bot 都能连通,但**菜单从未
  同步** —— 公开 bot 默认 scope 残留 8 条旧命令(chat scope 11 条)、运营 bot 一条
  都没有;`tg-sync` 后四组 scope 全部 `verified`(5 / 5 / 11 / 11)。`SYNC_LANGUAGES`
  去掉 `zh-hans`:Telegram 对 `zh-hans` / `zh-hant` / `zh-tw` 一律
  `HTTP 400 invalid language code`(实测 `getMyCommands` 同样被拒,即这些语言下
  根本写不进菜单,删掉不会漏清旧残留)。`mo status` 增 `tg-bot pid/alive`:TG 是
  **独立常驻进程**,看护不监管、`restart` 不拉,不单独报出来就只能对着"命令没回、
  日志全绿"猜(本次现场就是进程根本没起)。补 3 个离线测试
  (`SYNC_LANGUAGES` 白名单 / `status_snapshot` 的 `tg_bot` 三态 / `_print_status`
  那行);README 增"Telegram 交互命令"一节:命令表、chat 白名单规则、
  "改 `SPECS` 必须重跑 `tg-sync`"与 `getUpdates` 单实例(409)边界。

## [Unreleased] — 2026-10-24 — 信号广播与容量闸门解耦 + 通知双通道 + 图表结构标注

三件事同源：**信号是市场事实，执行是本账户状态**。修前两者被同一段代码绑死 ——
信号广播写在 `_submit_open` 的闸门之后，任一闸门 `return` 都连带吞掉通知，容量池
一满，ACCEPT/REJECT 在信号频道就彻底不可见；所有通知又都挤在同一个 Bot 里，公开
频道被迫与账户成交/日结/告警共用一个房间。

- **P0 闸门只产出 `block` 码，不再决定"要不要通知"**：`_submit_open` 重排为
  「结构事实（区间/箱体/风险档/周期）→ 闸门 → 信号广播 → 记账 → 装配」，闸门
  （`API_HALT`/`degenerate_risk`/`TF_CAP`/`PORTFOLIO_CAP`/`MIN_NOTIONAL`/
  `DAILY_LOSS_BREAKER`/`RATE_LIMIT`/`DUPLICATE`）只赋值，广播在记账之前。
  计数器/日志语义逐条沿用 `_BLOCK_COUNTERS` / `_note_block`（`TF_CAP` 仍同时计入
  `tf_cap_skips` + `cap_skips`，`MIN_NOTIONAL` 仍记 qty/ref），历史 counters 与
  journal 口径可直接对读。
- **信号消息新增"系统执行"行**（`exec_status`）：被挡时写
  `仅参考 · 组合容量已满,本系统本拍未开仓`（周容量点名 `12h 周期容量已满`），
  放行时写 `本系统已挂限价单` / `本系统已同向开仓(市价)`。公开频道**只写结构与
  处置**，不出现权益/持仓量/下单成败 —— 该频道可以公开订阅。
- **`_notify_signal` 三重纪律**：① 按 `(symbol, tf, event, ts, direction, route)`
  去重（2048 条滑窗，闸门重复评估不刷屏，但计数器照常累加）② 通知器缺失/抛异常
  只记 `signal_notice_errors` + journal，绝不反噬决策链 ③ 渲染与 HTTP 都在通知器
  工作线程里跑（`png_factory` 延迟渲染），主循环只付一次入队成本。
- **P0 通知双通道（`tg.py`）**：`_KIND_CHANNEL` 一张表就是唯一事实 —— `signal`
  →公开频道，`fill`/`daily`/`alert`→运营频道，**未知类型一律落运营**（宁可多给
  运营看，也不把账户数据塞进公开群）；`send(kind, ...)` 是唯一出口，
  `channel_of()` 供测试直读。运营通道未配置时**回退**到信号 Bot（不静默丢告警），
  `ops_channel_separate` 按解析后的 token/chat 是否不同如实回答 —— 这很重要，
  因为 `secrets_loader` 已把 `TELEGRAM_TRADE_*` 回退填满，"键存在"不再等于"显式
  配置"（修前用 `_ops_explicit` 标记，正好把回退误报成"已分开"）。启动日志与起动
  告警都打 `信号通道=on/off / 信号/运营 独立|运营回退信号 Bot|未配置`。
- **`send_signal` 参数化结构事实**：新增 `tf_label/bars/box_lo/box_hi/target_1/
  zone_lo/zone_hi/zone_touches/pivots/atr/exec_status/decision_ts`；`send_fill`
  同样收 `zone_*`/`pivots`/`atr`，且坚持 `entry_price`（真实建仓价）与 `price`
  （本次事件价）分离。测试期间抓到一个真 bug：runner 侧把方向写成
  `direction=` 而模板要的是 `side=`，每条信号都会 `TypeError` 被 except 吞掉 ——
  正是"通知永不 raise"的反面代价，故测试直接断言 `signals[0]["side"]`。
- **P1 图表结构标注（`render_bw_kline_png`）**：平衡箱体（浅灰带 + 粗虚线上下沿）、
  事件区间（斜线填充 + 细虚线 + 触边次数）、`±R` 标尺（以 entry/stop 定义 1R）、
  结构端点（三角 + 最近两个价位标签，带白底衬、贴右沿时自动改放左侧）、右侧标签
  栏统一登记后**按 12px 行距避让**（修前每条线各自压字，价位接近时互相盖住）。
  纵轴刻度按间距统一定精度（修前 `.6g` 让 113.503 与 98.1832 混排，视觉上像不等
  距）；字体优先中文字体、失败退 ASCII（CJK 不可用时摘要自动改英文）。
  pivot 下标只在画**认知帧**时标注 —— 触发帧与认知帧坐标不同，硬标会整体错位。
- **`secrets_loader`**：`TELEGRAM_TRADE_*` 为可选件（缺省回退 `TELEGRAM_*`），
  新增 `environ=` 注入便于测试，缺这对键不再导致启动失败。
- **新增 `scripts/live_restart.py`**：代码更新后的平滑换进程工具 —— 按看护同一条
  `spawn_detached` 路径终止旧 PID 并立即拉起新进程、轮询确认心跳，避免"kill 后干等
  看护 300s stale 窗口 + 消耗重启预算 + 误报停摆"。
- 测试：`tests/test_live_runtime.py` 新增 8 条 —— 容量挡下仍广播（含 `box/zone/
  touches/tf_label` 逐字段）、周容量点名、同事件只推一条、通知抛异常不打断决策、
  **真通知器跑一遍完整链路**（锁死 `send_signal` 的关键字契约，拼错即重现上面
  那次 `side=`/`direction=` 事故）、`_KIND_CHANNEL` 分流（未知类型落运营）、回退
  不谎报"已分开"、运营键可选；全量 618 passed / 1 skipped。

## [Unreleased] — 2026-10-24 — 修复「锁利回吐预算耗尽」只记账不下单（止盈不兑现）

实盘 10-24 21:47 切到 `live_runner` 后第一次锁利离场（PHAUSDT，+13% 未平）暴露：
日志写 `⇒ 市价离场`，交易所侧**没有任何平仓单**。根因不是行情判断，而是
`_sub_bar_close_out` 的 live 分支对所有触发原因一视同仁地只调 `_reconcile()`。

- **P0 `_sub_bar_close_out(..., executor=...)`**：离场出口现在必须显式声明
  **执行者**，两类触发条件的执行者不同，代码不再含糊：
  - `exchange_stop` —— 触发条件是"价格触及某个既定价位"，该价位有交易所原生
    TPSL `pos_loss` 在挂 ⇒ 触发即由交易所成交，本进程只对账。**前提**
    `_stop_is_resting(sym, invalid)`（本地有 order_id 且触发价在 1.5 个价格档
    内）；实测无保护单时**降级**为本进程市价离场（`unprotected_stop_exits`
    + 告警），修前这种"防线破了却没人执行"的情形没有任何动作。
  - `local_market` —— 触发条件是**行情状态的函数**（`peak − d·budget` 与现价
    的关系），事前写不成一张挂单价，只能此刻市价离场。
- **新增 `_live_market_exit`**：风控离场一步到位（不进意图池、不占每时下单
  配额），先撤入场/保护单再下单（避免市价单与 TPSL 双平甚至反手）；幂等三重
  保证 —— ① 以 `self.positions` 当前事实为准（本地无仓绝不下单）②
  `_exit_settle` 有待结回执则跳过 ③ 成交即清仓。下单失败**不装成已平**：
  仓位保留、`exit_submit_errors` 计数、告警明说"保护单已撤、下一拍自动重挂"。
- **成交回执两步出账**：market 单下单瞬间 fills 常还没索引到，先用参考价记账
  （`price_label="参考价(下单时,回执待校正)"`），登记 `_exit_settle`，由
  `_settle_exits` 在后续步用权威成交价校正（journal `exit_settle`，偏差 >0.2%
  才补发一条校正告警）；超时仍无回执记 `exit_settle_miss` 并停手。**一条离场
  通知绝不因为接口晚到 200ms 而消失。**
- **P1 离场分型（两个正交事实）**：`executor`（`protective_stop` /
  `external` / `unknown` / `local_market`）× `pnl`。判据
  `stop_execution_evidence`：`d·(exit − stop) ≤ +tol`（挂单不可能成交得比触发
  价更好）。反向判据会把**每一次盈利离场**都归因成移动止损，把外部平仓冒充成
  自家止盈 —— 比不分类更危险，故方向钉死。文案随事实：`止盈(移动止损触发)` /
  `止损(保护单触发)` / `离场确认(外部平仓·盈利|亏损)` / `止盈(回撤预算耗尽·
  市价离场)`；journal `reconcile_closed` 增 `executor / stop / exit_ts_ms /
  pnl / price_source`。
- **P1 防线通知按来源命名**：`_defense_title` 把锁利、§8.3 trail、结构收紧分开
  —— 锁利进盈利侧 = "锁利推进(已锁 X% TP1 行程)"，修前一律叫"止损推进"，用户
  看到的是"又在收紧止损"，与"已经锁住浮盈"的后续处置完全不同。
- **P2 `_close_fill_fact`（原 `_last_fill_price`）**：三条修正 —— ① **不假定
  列表顺序**（显式按成交时间降序取最新；修前遍历覆盖，留下的是窗口内**最旧**
  一笔平仓成交，实测 BABA 回执 110.47 而其保护单 105.148 早该成交）② 同一订单
  多笔成交按 `size` 加权聚合（vwap + 最晚时刻）③ 时间窗 `opened_ts`，窗口内
  没有 ⇒ 返回 `None`（如实不编），绝不把上一笔仓的平仓价记到这一仓头上。
- **已知平仓即同时清 pm 事实**：`_live_close_filled(role=exit)` 现在同步
  `cs.pm.positions.pop`（与 `_reconcile_closed` 同口径），修前 pm 侧留幽灵仓，
  该品种再触发 `pm.open` 会因"已持仓"被拒。
- 测试：`tests/test_live_runtime.py` 新增 14 条（执行者分流、幂等、失败兜底、
  分型方向、回执 vwap/时间窗/校正/超时）；`tests/test_v21_balance_trigger.py`
  的三处断言随语义标题改为"锁利推进"（行为未变，只是不再叫止损）。

## [Unreleased] — 2026-10-24 — 退役残留清理（无行为变更）

删的是「已经不在执行路径上、但还能被 import / 被读」的东西，不改任何交易行为。

- **`position.py` 删除三个只剩测试引用的符号**：`opp_lock_px` /
  `lock_is_protective` / `lock_is_profitable`。它们自 v21.7 起已不是判据（唯一出口
  `opp_lock_step`），但 `opp_lock_px` 是**已退役的绝对价位口径**留在生产模块里，
  下一个调用方完全可能 import 它去算止损价，把「钉在现价身后」的缺陷重新引入。
  - `opp_lock_px` ⇒ `tests/test_v21_balance_trigger.py::_legacy_lock_px`（只作
    对照量：说明新口径在 `peak==target` 时与之逐位重合）；
  - 两条性质断言改用 `stop_protect_dist(...) > 0` 直接表达（同一式子、不再多一层
    包装），`stop_protect_dist` 本身是 `opp_lock_step` 的内部判据，保留。
- **`config/live.json` 删除无人读取的键**：`risk.min_notional_source`、
  `loop.intent_keep_hours`（v20 限价意图墙钟过期，早已退役）、
  `paper.symbols_mode`、`note_size`（与 `note` 重复且内容过期）。全仓
  `src/scripts/tests/tools/docs` 无一处引用，删掉不改行为。
- **`config/live.json` 修重复键**：`loop` 段 `state_bar_tail` /
  `campaigns_write_seconds` 各出现两次，后值静默覆盖前值
  （`campaigns_write_seconds` 实际生效 60，前一条 120 是死值）。各留一条。
- **配置文案去掉手抄的锁利比例**：`config/strategy.json` 的 `note` / `meta.model`
  与 `config/live.json` 的 `note` 仍写「锁利50%」/「Lock(50%)」，与
  `arm_config.reject_opp_lock_frac=-0.25` 直接矛盾。改为描述语义并**指向该键**
  （`stop = peak ∓ (1−|frac|)·span`），不再复制数值 —— 下次标定不会再漂。
- **`scripts/archive/_diag_opp_lock_ab.py`** 去掉未使用的 `V6Params` 导入
  （vulture 90%）。
- **保留（不是残留）**：`campaign.ArmConfig.exit_exec` / `pivot_exec` 是已测量、
  默认关闭的开关（`pivot_exec` 无生产装配路径，由 `require_exec_pivots` fail-closed
  挡住）；`src/market_operator/lifecycle.py` / `repairs.py` / `tiered.py` /
  `strategy_v6.py` 是历史轮次的回放实现，仍被 `scripts/run_v18|v19|v20.py` 与对应
  测试使用；`config/v*.json` 是各轮冻结快照。

## [Unreleased] — 2026-10-24 — 保护止损改用交易所原生仓位级 TPSL

- **保护止损从「独立计划单」改为「交易所原生 TPSL」**：`place-tpsl-order`
  （`planType=pos_loss`、`triggerPrice`、`triggerType=mark_price`、
  `executePrice=0` 触发后市价、`holdSide`，**不带** `size`/`side`/`tradeSide`）。
  旧路径 `place-plan-order` 的计划单是**独立委托**：按挂单时的数量触发，
  部分减仓后剩余仓位不再被覆盖（要等下一轮 `_manage_stops` 撤旧换新），
  且计划单与仓位生命周期脱钩 —— 仓位平掉后可能留下无仓可平的废单。
  仓位级 TPSL 由交易所按触发时的整仓市价平仓，自动覆盖部分减仓、仓位归零即失效。
- **`_manage_stops` 删除数量维度**：不再"qty 变了就撤旧换新"（`pos_loss` 报文无
  size）。修前每次部分减仓都要先撤后挂，撤成功/挂失败之间就是一个失保窗口，
  而这个换单在数量上毫无必要。触发价变动仍撤旧换新（移动防线）。
- **`cancel_plan_order` → `cancel_tpsl_order`（`orderIdList` 形式）**：
  `cancel-plan-order` 只认对象数组 `orderIdList=[{orderId}]`；单值 `orderId`
  撤不掉 TPSL ⇒ 旧止损继续有效，出现"新单挂上、旧单未撤"的双保护，
  谁先触发不可控。端点与计划单共用。
- **`plan_orders_pending` → `tpsl_orders_pending`（`planType=profit_loss`）**：
  planType 只能单值 —— `loss_plan` 被 `40812` 拒、逗号多值经 `%2C` 后 `40009`
  签名失败；`profit_loss` 返回行里可能混有 `pos_loss`/`pos_profit` 与旧式
  `loss_plan`/`profit_plan`，故归属仍只按 `clientOid` 前缀判定，异己单一律不碰。
- **挂单响应无 `orderId` 视为失败**：修前 `str(resp.get("orderId") or "")` 落成
  空串仍写入 `stops`，本地声称"已保护"，实际裸奔到 TTL 过期才被 `stop_missing`
  发现。现在直接 `stop_error` + 告警。
- **`stop_hold_side()`**：`holdSide` 口径 —— hedge_mode 用 `long`/`short`，
  one-way 用 `buy`(多)/`sell`(空)，与 ccxt `bitget.createOrder` 的 TPSL 分支一致；
  传错会 `400172` 被拒，等于该仓位无交易所侧保护。
- **心跳补挂同步作废在挂快照**：`HeartbeatGuard.ensure_protection` 补挂后把
  `_stops_live_ids/_stops_live_rows/_stops_checked_at` 置空，否则新 `orderId` 不在
  主循环的旧快照里，会被判"保护丢失"→ 再挂一张再撤一张。
- 工具与文档：`live_gap_preview` 的写操作拦截表、`live_paper_check` 的 paper 防呆
  名单、`live_status` 的读端点与告警文案（"多张保护单"）同步。
- 测试：新增 `tests/test_bitget_live.py` 钉住 place/cancel/query 三个报文契约
  （含"不带 size/side/tradeSide"、"只认 orderIdList"、"planType=profit_loss"、
  旧 `loss_plan` 被 `ValueError` 挡住）；`tests/test_live_runtime.py` 新增
  部分减仓不换单、不带 size 挂单、无 orderId 记失败三例，并改用 `place_stop_loss` /
  `cancel_tpsl_order` / `tpsl_orders_pending` 的假实现；`tests/test_live_supervision.py`
  新增心跳补挂三例（缺保护 ⇒ 无 size 的 `pos_loss` + 作废在挂快照、自家单仍在挂 ⇒
  不重挂、无 orderId ⇒ 记 `errors` 且不写本地记录）。

## [Unreleased] — 2026-10-24 — 对侧锁利语义重写（回吐预算,删除 R 单位第二把尺子）

- **锁利从"绝对价位"改为"回吐预算"(v21.7)**:`budget = (1−|frac|)·|target_1 − entry|`
  (建仓冻结),`stop = peak ∓ budget`,`peak` = `Position._best`(单调)。旧写法
  `entry + |frac|·span` 在决策时现价已越过该价位时只剩两种坏结果:拒单(不推进 =
  裸奔到收盘)或挂上即成交(止盈单冒充止损单)。GOOGLUSDT 空头样本:旧 323.34
  (距现价 2.01、锁 0.24R、实盘成交 324.01)⇒ 新 321.61(锁 0.84R,触发时回吐恰
  为 ½span → 0.68)。
- **删除 `opp_lock_max_giveback_r`**:它是 R 单位的第二把尺子,与 `|frac|`(行程
  比例)量纲不同却"取更紧者"互相遮盖(`reports/opp_lock_ab.md`:0.25R 在 dev/test
  净利 −12.83/−7.38)。`config/strategy.json`、`campaign.ArmConfig`、
  `live_runner.new_position_manager` 同步移除;`opp_lock_live_px` / `tighten_lock`
  删除。`lock_is_protective` / `lock_is_profitable` / `opp_lock_px` 于同日二次清理中
  一并从 `position.py` 删除(性质改由测试用 `stop_protect_dist` 直接断言)。
- **单一裁决出口 `position.opp_lock_step`**:`PositionManager.on_bar`(认知/执行细
  bar)与 `live_runner._on_sub_bar` 共用;三态穷尽 —— 未 arming 保持结构防线 /
  保护侧推进(**可多次棘轮**)/ 越过即市价离场(`v21|lock_giveback_exhausted`)。
  恒等式 `stop_protect_dist ≡ budget − 回撤` ⇒ 合法价位由构造保证,不再需要
  "拒单闸门"这一态。
- **状态语义分离**:新增 `Position.armed`(arming = 价格事实,`frac<0` 收盘突破 /
  `frac>0` 盘中触及),不再复用 `reduced`(后者只表示减仓额度已用);删掉"仅影线
  触及先锁 10% 保本垫"的特例(`frac` 的符号现在只决定 arming 口径)。
- **实盘子周期离场统一出口** `_sub_bar_close_out()`:固定止损与"锁利回吐预算耗尽"
  走同一条离场/对账路径,避免已平仓位被 `_manage_stops` 重新挂单。
- **`frac` 标定 ⇒ 生产值 `-0.50 → -0.25`(让出 ¾ 行程、锁 ¼)**。同源口径扫 7 个
  预算(explore/book4 × dev/test/full,1x):`Σ` 从 `frac=0`(不锁利)的 −241.55 升到
  `-0.25` 的 **+593.65**、`-0.50` 的 +416.93;`-0.25` 是唯一 6 段全正的臂(最差段
  +33.42),而相邻 `-0.35` 塌到 +314.74 ⇒ 响应面不平滑,按 maximin 取值而非取最高
  点。dev 单点最优(`-0.65`)在 test 只剩 +10.06,是过拟合形状,不采用。证据:
  `reports/lock_frac_sweep.md`(`scripts/_diag_lock_frac_sweep.py` 可复现)。
- **`pivot_exec` 是无效开关,现改为报错**:`CampaignFrame.exec_pivots` 只由
  `scripts/_diag_v21_scale.py` 装配,`scenario.build_frames`(生产/验收链)从不装配
  ⇒ `pivot_exec=True` 在生产链上逐位等于关闭(实测 6 段 net 完全相同)。静默放行会被
  读成"机制无效",故 `run_campaigns` 直接 `ValueError`。
- **`exit_exec` 在新锁利口径下重测**:`frac=-0.25 + exit_exec` Σ +536.86(最差段
  +31.24)vs 关闭 +593.65(+33.42)、`frac=-0.50 + exit_exec` Σ +223.89(最差段
  +16.27)vs 关闭 +416.93(−15.19)—— 提前收网在宽预算下只花 ~10% 换更低回撤,在紧
  预算下腰斩净利;收益侧没有补偿,默认继续关。v21.6 "11/12 组变差"的读数已标注过期。
- **`live_runner.new_position_manager` 对锁利 frac 改为 fail-closed**:删掉写死的兜底
  默认 `-0.50`,缺 `arm_config.reject_opp_lock_frac` 直接报错。标定值只应来自配置,
  否则下次标定后实盘与回放会悄悄分叉(本次 −0.50 → −0.25 若走兜底路径就会分叉)。
- 测试:新增回吐预算/三态/随机路径不变量/频率不变性(1×/2×/4× 采样同一防线)
  用例;`tests/test_v21_balance_trigger.py` 锁利用例全部改钉新语义,并改为**从
  `config/strategy.json` 取 frac**(断言语义,不再钉死标定值)。全量 580 passed / 1 skipped。
- 文档:`docs/v21-protocol.md` §5 重写锁利口径(含为何删 R 尺子的数据)、新增 §5.1
  预算标定表与粒度开关重测;`scenario.py` / `config/strategy.json` 注释与键同步。
- **回放基线按新语义显式重冻(2026-10-24)**:`python tools/replay_acceptance.py
  --capture --quiet-prep` 重写 `tests/fixtures/replay_baseline.json`;重冻后连跑两次
  逐场景/逐品类 Δ 恒 `+0.0000`、`[PASS]`、退出码 0(此前必然 FAIL,因为基线记着
  旧锁利的水位)。六场景合计 **+1227.2986 → +593.6514**(每一分的去向见
  `reports/scale_20261024/BASELINE_DRIFT.md`:旧基线含"止损落在现价身后"的虚拟利润)。
  对照表与记账纪律写在 `docs/acceptance.md`「基线重冻记录」。**旧基线仍可取回**:
  `git show HEAD:tests/fixtures/replay_baseline.json`。
- **实盘对账的两个疑问已查清(①已被上方的原生 TPSL 迁移取代)**:①~~`plan_orders_pending()`
  只查 `normal_plan` 不是缺口~~ —— 本项目只挂一种计划单,且 `place_stop_market_plan` 已把
  `loss_plan` 归一为 `normal_plan`(`bitget_live.py` 实测注释),故"本地有记录/交易所
  没有 ⇒ 重挂"的判定覆盖全部自有单;异己单由 `clientOid` 前缀过滤 + `_dedupe_stops`
  兜底。**注:现已改为原生 TPSL,查询走 `planType=profit_loss`,见本节上方。**
  ②本项目**从不使用交易所侧移动止损**(全仓无 `track_plan`/`callbackRatio`),
  移动防线是本地棘轮(`opp_lock_step` → `_manage_stops` 撤旧换新),所以不存在
  "交易所 trailing 口径未对齐"的问题。

## [Unreleased] — 2026-10-23 — 单链路 v21 收敛(D3 / D9 / D13 / D15)

- **实盘只剩一条链(`scripts/live_runner.py`)**:删除 `lifecycle`/`repairs`/`tiered`
  导入、`CONFIG_V20`、`load_v20_params()`,以及 `ctx18` 建仓语境;`cognition.engine`
  从"选择器"变成**守卫**——配置里出现非 `v21` 的值即报错。`load_strategy_params()`
  **fail-closed**:配置没有 `use_balance_detector` 时直接报错,而不是退回旧链持仓
  参数;`new_position_manager()` 只构造 v21 `PositionManager`。旧链是"启动失败",
  不是静默回退。
- **配置**:`config/live.json` 删除 `cognition.engine`(字段本身已无意义)。
- **`split_boundary` 独立成模块(`src/market_operator/split.py`)**:回放/战役不再
  经 `research.py`(555 行研究方法学)取判分边界;`research` 仍 re-export 以免打断
  历史轮次。`tests/test_layering.py` 增加断言:生产闭包含 `split`、不含 `research`。
- **平衡箱窗口参数化(`structure.Params`)**:新增 `box_min_rotations=1`、
  `box_keep_broken_bars=12`、`box_keep_active_bars=24`,`_sync_balance_boxes`
  不再内联魔法数字;docstring 与实现一致(此前注释写 rotations≥2 / 60·120 bars,
  代码是 1 / 12·24)。三键进 `from_json` 的 int 归一。
- **`broker._floor_step` → 公开 `floor_step`**(保留 `_floor_step` 兼容别名)。
- 测试:524 passed / 1 skipped;新增 4 条 v21 单链路断言 + 3 条平衡箱/参数断言。
  `tools/replay_acceptance.py` 六个主行与 24 个子行 Δ 全为 +0.0000。

## [v0.1.0] - 2026-10-19 — Research Only verdict

### Phase 1 — Methodology & OSS audits
- 冻结交易方法论 `docs/methodology.md`（因果语义、区域生命周期、事件目录 E1-E6、
  研究协议与 GO/NO-GO 判据；全部参数为先验）。
- 独立深度审计：`docs/audits/`(pymarket-structure(MIT) / quant-trading（专有，仅思想） /
  OrderFlow-absorption（无 LICENSE，仅思想） / TapeFlow(MIT))。

### Phase 2 — Cognition core & data
- `structure`:ATR 波动率尺度、波动率自适应确认波段、区域生命周期状态机、regime。
- `events`:E1-E6 因果事件检测，forming/known 双时间戳；前缀不变性与增量==批量测试。
- `data`:Bitget 2 年 1h 三品种真实数据，0 缺口，sha256 manifest。

### Phase 3 — Event study
- 事件研究 + 同 regime 匹配对照 + block bootstrap + 成本压力；诚实性日志全程。
- 判定：E1|E6 short 相对增量 GO（绝对净期望 ≤ 0)；做多与 E2/E3/E4 NO-GO;E5 样本不足。

### Phase 4+5 — Strategy, position mgmt, full-cost backtest
- 白名单仅 GO 机制；§8 六条事件驱动持仓管理全部实现且可解释。
- 真实合约规格/费用/资金费/强平约束；300 USDT 复利；9 段×成本压力全部为负 → **NO-GO, Research Only**。
- 验收报告 `docs/acceptance.md`（任务书八问逐项，含失败分解与 v2 冻结议程）。

[v0.1.0]: https://github.com/Lianowo/market-operator/releases/tag/v0.1.0

## [Unreleased v2] - 2026-10-19

- 归因驱动优化轮：E7 FLIP_RETEST + F1(docs/v2-protocol.md);test 一次评估仍 NO-GO,
  Research Only 维持；优化循环按反过拟合原则停止(docs/acceptance.md v2 附章)。
  120 tests;v1 行为 parity 锁定。

## [v0.4.0] - 2026-10-19

- v4 成熟度优先（E7-only + F1 + 限价接回踩 + 15m 执行路径）：同信号 A/B 全面劣于 v2 市价
  (test −75.4 vs −55.7),adverse selection > maker 节省；九段全负、test<60 判 INSUFFICIENT。
  192 tests。优化循环关闭，Research Only 终态。

## [v0.5.0] - 2026-10-19 — Final form, definitive

- v5 破位质量路由（STRONG=市价/WEAK=E7限价）+ E1 优先 + F1 全入口 + short-only:
  test −22.5/22 笔/CI 含 0(INSUFFICIENT)；胜率随成熟度单调升(28%→41% 桶)，总亏单调降。
  五轮干预全部落定：低频、低亏、不赚。Research Only 定版，协议级优化关闭。218 tests。

## [v1.4.0] - 2026-10-21 — Live runtime

- Bitget live runner + Telegram 通知 + 118品种宇宙(>3M)+ paper 冒烟 PASS;580 tests。
- v17-v20:首个 dev 翻正(v18 +156/PF1.51) 与 test 首正(v19 +10.46);S1/S4 小幅正贡献。

## [v1.5.0] - 2026-10-21 — aggTrades contact study

- 3 品种 × 90 天 Binance 逐笔 aggTrades(2.195 亿笔)接触点检验(`aggtape.py` / `reports/aggtrades.md`):H1 49/50 单元无增量,H2 吸收签名不成立,订单流路线正式关闭;595 tests。

## [v2.1.0] - 2026-10-21 — v21 BalanceDetector + ACCEPT/REJECT + Live Multi-TF Architecture

- **平衡箱识别(`balance_detect.py` / `balance.py` / `structure.py`)**:拍卖市场理论六维框子打分器(`BalanceDetector`,稳健 12%/88% 分位数边界 + 前缀和 O(1) 加速 + 去重抑制)桥接 `ZoneBook`,派生上下沿支撑/阻力并附带 `box_opposite`、`box_quality`、`box_rotations`、`box_width_atr`。
- **双原语与混合触发(`events.py` / `strategy_v6.py` / `config/v21.json`)**:`ACCEPT`(E1 突破接受)与 `REJECT`(E3 刺穿拒绝)双原语;M3c 高阶降档混合触发(`2h`/`4h` 同周期收盘触发配强克制过滤,`12h->2h` 与 `1d->4h` 降一档细周期触发)。
- **出口与风控升级(`position.py` / `campaign.py` / `broker.py`)**:修复保护性止损在同向减仓单下的全天候在场;`REJECT` 采用箱体外缘结构止损 + 触及对侧缘不减仓直锁 50% 利润(`reject_opp_lock_frac=-0.50`) + Turtle 确认 pivot 移动防线;新增各周期独立风险容量池(`tf_cap_by_tf`)与阶梯动态复利(`dynamic_risk_tiers`)。
- **实盘多周期并发与双 Bot 黑白 K 线图(`scripts/live_runner.py` / `src/market_operator/tg.py` / `config/live.json`)**:支持 104 USDT / 20x 阶梯动态复利(4%→3%→2%→1%)、2h/4h/12h/1d 四周期独立容量池并发运行、信号/交易双 Telegram 机器人分流及纯 Python 高清黑白 K 线+平衡箱体 PNG 渲染配图直发;605 tests 全绿。

## [v2.2.0] - 2026-10-23 — 跨平台实盘监督与存储治理

- **心跳即判活(`src/market_operator/heartbeat.py`)**:`runtime/live/heartbeat.json`
  由 runner 内独立心跳线程每 15s 落盘,同时带 `ts_ms`(进程还活着)、`loop_ts_ms`
  (主循环最后一次成功走完一步)、`step_started_ms`(卡在哪一步)与
  `positions_detail`(进程消失后重挂止损所需的持仓快照);`beat_age_seconds` 取
  `max(loop_ts_ms, started_ms)`,刚重启不会被上一具尸体的旧时间戳误判成卡死。
- **进程内保护线(`HeartbeatGuard`)**:主循环停摆后仍持续心跳并**只保护不主出**
  (核对/补挂交易所止损),停摆超 `self_fence_seconds` 且有持仓则以退出码 3 自我
  熔断;`classify`/`decide` 五档判定表(缺失/正常/迟到/补做中/停摆)为纯函数。
- **外部看护(`scripts/live_supervisor.py`)**:只读心跳判活(不读进程表/端口/计划
  任务结果),缺失→拉起、迟到→只记录、补做中→等(受 `recover_max_seconds` 约束)、
  停摆→终止+拉起,重启预算(6 次/小时)与启动宽限(180s)防崩溃循环,预算耗尽只
  告警交人工;常驻模式顺带按 `gc_hours` 调用存储治理。
- **单实例锁(`SingletonLock`)**:runner / supervisor 各持一把内核文件锁
  (`msvcrt.locking` / `flock`),进程死亡即释放,不存在 pid 毒锁,也不会双开下单。
- **存储治理(`scripts/storage_gc.py`)**:盘点 → 可逆压缩(manifest + sha256 +
  `--restore`)→ 保留期清理 → 增长预测 → 预算闸门(35GB 硬 / 30GB 软,退出码
  0/1/2);审计日志只压缩不删除,控制台日志先归档校验再截断留 4MB 尾巴;增长预测
  只按 `logs/ runtime/ data/` 速率外推,落 `reports/_storage_growth.json`。
- **计划任务入口**:`\MarketOperator\MarketOperatorLive`(每 3 分钟重复、忽略重叠)
  与 `\MarketOperator\MarketOperatorStorageGC`(每日 04:10),动作为
  `C:\Windows\System32\cmd.exe /c tools\schedule\<入口>.cmd`(绝对执行体 + 仓库根
  工作目录)。**入口脚本内一律不加重定向**:在跑的实例独占自己的日志句柄,下一次触发
  再 `>>` 同一文件会让 cmd 在打开阶段失败(退出码 0、日志全空);改为常驻看护自己把
  console 行镜像进 `logs/supervisor.log`、异常记 `kind=crash`,重复触发靠单实例锁
  变成"没跑就拉起"。移除 OS 专用 `scripts/live_watchdog.ps1`。新增
  `tests/test_live_supervision.py`(23 项:判定
  五档、单实例锁跨进程、看护动作、启动宽限、压缩无损与预算闸门、console 自重定向),
  全量 605 passed(未计入 `test_v13_cascade`)。
  注:实盘运行时行情刷新会改写 `data/raw/*` 与 `data/manifest.json`,
  `test_v13_cascade` 的哈希锁定用例此时必然失败(环境时序,非回归),复跑前需停 runner。
