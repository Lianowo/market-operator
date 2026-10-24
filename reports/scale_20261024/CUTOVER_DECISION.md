# 切换决策:refactor/domain-model 是否替代 main 上实盘(2026-10-24)

> 结论:**条件性 GO** —— 方向正确、状态兼容已验证,但**今晚不能原地重启**。
> 必须先过 5 道闸(提交、停旧、清旧计划单、迁移 runtime、paper 金丝雀),
> 否则会出现「双 runner 真单」「旧止损看不见」「状态回到 20 小时前」三类事故。

## 一、事实基线(本次实测)

| 项 | 生产(main) | 待切(refactor) |
|---|---|---|
| 路径 | `D:\LiAn\Web3Code\market-operator-main` | `D:\LiAn\Web3Code\market-operator` |
| git | `main@14a22b5`,工作区干净 | `refactor/domain-model@78f7d75` + **22 modified / 8 untracked** |
| 心跳 | `ts_ms` 14s 前,`status=ok`,`mode=live`,equity 114.3759 | 停在 `2026-10-24 01:13`(旧副本) |
| runner | pid 49100 | 无 |
| 挂单 | 12 张,`planType=normal_plan`(固定 qty) | `pos_loss` TPSL(整仓,无 size) |
| 计划任务 | `MarketOperatorLive` → `cmd /c tools\schedule\live_supervisor.cmd`,CWD=`-main` | 同一脚本名存在 |

## 二、等价性证据(支持切换)

1. **同一仓库的两个 worktree**,不是两套代码:router 参数 `router_base` / `router_by_tf`
   dump 后**逐键相同**;`Params` 仅多 3 个箱体生命周期默认值
   (`box_keep_active_bars=24` / `box_keep_broken_bars=12` / `box_min_rotations=1`)。
2. **状态模式兼容**:`Position` 相对 main 只**新增** `armed` / `lock_off_logged`
   (均有默认值),无字段改名、无新增必填字段。用 refactor 的 `Position(**fields)`
   重建**生产 12/12 持仓成功,initial_risk 全 > 0**(脚本
   `mo_preflight_refactor.py`,只读)。
3. **回滚也兼容**:refactor 落盘的 `armed/lock_off_logged` 会被 main 的
   `_load_state` 过滤丢弃,不会 `TypeError`。
4. 测试 594 passed / 1 skipped;`config/live.json` 只删了无人读的键
   (`min_notional_source` / `intent_keep_hours` / `symbols_mode` / `note_size`),
   `grep` 全仓无引用;`strategy.json` 改名自洽,无脚本再引用 `v21.json`。
5. 顺带修掉 main 现网三个真实缺陷:计划单撤单口径(标量 orderId 撤不掉)、
   固定数量计划单不覆盖部分减仓、心跳补挂后快照失效导致重复挂/撤。

## 三、行为差异(必须显式接受,不能当"纯重构")

| 维度 | main(现网) | refactor |
|---|---|---|
| 锁利比例 | `reject_opp_lock_frac=-0.50` | **-0.25** |
| 第二把尺子 | `opp_lock_max_giveback_r=0.25`(现价锚,回吐≤0.25R) | **删除**,改为 `stop = peak ∓ |frac|·span` 棘轮 |
| 落位闸门 | `lock_is_protective` + `lock_is_profitable` 过滤 | 由构造保证(`stop_protect_dist ≡ budget − 回撤`) |
| event_id | `E1/E3/E6…` | `BREAKOUT_ACCEPTED/LEVEL_REJECTED/…` |

### 3.1 冻结基线的口径已被改掉,且"PASS/Δ=0"是自证

- 工作区把 `tests/fixtures/replay_baseline.json` **整体重抓**了
  (684 增/683 删),`tools/replay_acceptance.py` 的 `BASELINE_PATH` 是硬编码,
  所以「Δ=+0.0000 / PASS」等于**拿当前输出跟自己比**,不能作为等价性证据。
- 取回提交版基线后实测(**本次运行**):
  `book4|dev|1x` 现网基线 **+176.3601** → 当前树 **+93.0361**;六场景合计
  **+1227.2986 → +593.6514(−52%)**,六段中 **5 段为负**(仅 `explore|dev`
  逆势 +34.42,因为它对旧基线也只有 +38.04 的小基数)。
  复现命令(不影响仓库):
  `python mo_acc_harness.py <git show HEAD:tests/fixtures/replay_baseline.json> --scenario book4 --segments dev`
- 归因可复现:把配置换回 main 的 `v21.json`(frac=-0.50)后为 **+139.9238**,
  与 `reports/lock_ab_v217_frac050` 一行**逐位一致**;剩下的 176.36→139.92
  正是被删掉的 `giveback=0.25R` + 落位闸门(它们**只存在于 main 的实盘路径**,
  main 的回放本身也是 giveback=0)。

### 3.2 结论怎么读

- 「refactor 相对 main 的**代码**」在策略上没有退化 —— 逐位相同;
- 「refactor 相对**冻结基线**」的 −52%,是**主动换锁利语义 + 承认旧基线含
  现价身后止损失真利润**的结果,证据与论证在
  `reports/scale_20261024/BASELINE_DRIFT.md`、`reports/lock_frac_sweep.md`。
- 这是**alpha 决策**,不能由测试套件或 replay parity 代表;但它也不是
  "重构写坏了"。切换 = 同时接受一次策略参数变更,须由人签字。

## 四、阻断项(不解决就不要切)

1. **树是脏的**:`78f7d75 + 22 M + 8 ??`(position/live_runner/bitget_live/
   campaign/heartbeat/config/tests/docs)。脏树不可复现、不可回滚到 SHA。
2. **runtime 是按 worktree 隔离的**:`state_path = ROOT/config值`,两棵树各有
   `runtime/live/` 与各自 `live_runner.lock`。直接把任务 CWD 改到新树 ⇒
   ① 用 20 小时前的旧状态重建战役;② 旧 runner 没停时**两把锁互不排斥 = 双开真单**。
3. **旧计划单对新代码不可见**:生产 12 张是 main 的 `normal_plan`(固定 qty);
   refactor `plan_orders_pending()` 把 `planType` 硬编码成 `profit_loss`,
   `cancel_tpsl_order` 也是 TPSL 口径 ⇒ 新 runner 很可能"看不见旧单 → 再挂一张
   pos_loss",形成双保护;旧单在部分减仓后数量还不对。
4. **词表断裂**:存量 12 个持仓的 `event_id` 是 `E3`,新品种写 `LEVEL_REJECTED`;
   只影响报表分桶(代码里唯一比较处 `lifecycle.py` 不在生产链),但记账会断代。
5. 已过时的 `_deploy_preflight\preflight.py` 仍 import 已删除的
   `opp_lock_live_px/lock_is_protective`,且指向**死树**的 runtime,跑不起来,
   不能当成"预检已通过"。

## 五、切换后立刻会发生什么(用生产快照预演)

12 个持仓中 **6 个 `hold(not_armed)`**(未收盘突破对侧缘)、**5 个无 `target_1`**
(§8.2 不适用);**PHAUSDT**(4h 空,entry 0.05755,t1 0.05747,mark 0.05057,
mfe 0.61R)预算仅 6e-5,现价已越过预算 ⇒ 首根子周期即
`exit(giveback_exhausted)` 市价离场。预演脚本:`mo_preflight_refactor.py`。

## 六、放行清单(按序,全部满足才切)

1. **提交**:把 WIP 拆成可回滚的 commit(至少 position / live_runner /
   bitget_live / campaign+heartbeat / config+tests+docs),记录 SHA;
   重抓后的 baseline 与 `reports/*` 同批入库,别留"本地能过、仓库不能过"。
2. **停旧并确认**:`Disable-ScheduledTask MarketOperatorLive` → 结束 supervisor
   → 结束 pid 49100 → 观察 `heartbeat.json` 连续 ≥3 个 tick 不再前进;
   确认无 `live_runner.lock` 占用。
3. **清旧单**:用**老树**的 client 逐张 `cancel_plan_order(orderId)`,
   再 `plan_orders_pending(plan_type="normal_plan")` 复核为空(12→0)。
4. **迁移 runtime**:仅复制 `state.json` + `campaigns.json` 到新树
   `runtime/live/`;**不要**复制 heartbeat/lock/supervisor/gap 锚;
   迁后跑一次只读预检,确认 `positions=12`、`initial_risk>0`、锁利预演同上。
5. **paper 金丝雀 30–60min**:新树 `python scripts/live_runner.py --mode paper`,
   用同一份 state 拷贝,确认不掉线、journal 连续、PHAUSDT 的离场只记不发单。
6. **切 live**:改 `MarketOperatorLive` 的 WorkingDirectory → 新树 → 启动;
   15 分钟内核对 ①心跳 `mode=live` ②首屏日志无 traceback ③交易所挂单数=持仓数
   (`pos_loss`,整仓)④`campaigns.json` 时间戳在走。
7. **回滚预案**:改回 CWD=main + 恢复切换前的 `runtime/live/*.json` 副本即可
   (状态模式双向兼容);回滚同样要先停、再清 TPSL 单。

## 七、一句话答复

**值得切、但别现在裸切**:代码与状态是等价的,真正要签字的是
"锁利口径 −0.50/0.25R → −0.25 峰值预算"和"12 张旧计划单 + 20 小时旧状态"
这两件现场事;把第六节的 1–5 步做完,切换窗口 ≤ 30 分钟且可秒级回滚。
