# 重构验收结论:能否替代 main 基线(2026-10-23)

> 判据来自 `docs/acceptance.md` 的「现行验收闸门」:**重构要求 Δ = 0.0000**。
> 本文件只记录可复核的产物,不引用记忆。

## 判定

**回测与策略语义层面:可以替代。** 逐项净收益与逐笔事实在冻结基线上零漂移,
测试全绿,生产闭包的分层栅栏成立。

**实盘切换层面:可以先 paper,不宜直接 live。** 理由见 §4——实盘等价性是
"构造性"的(纯改名 + 入口闭包断言),没有回放式证据可用。

## §1 净收益 parity(闸门原文)

`python tools/replay_acceptance.py`,2026-10-23:

| 场景｜段 | n | 净收益 | Δ | 逐笔 |
|---|---:|---:|---:|---|
| explore｜dev | 816 | +38.0407 | +0.0000 | 一致 |
| explore｜test | 346 | +195.3545 | +0.0000 | 一致 |
| explore｜full | 1153 | +281.0042 | +0.0000 | 一致 |
| book4｜dev | 976 | +176.3601 | +0.0000 | 一致 |
| book4｜test | 462 | +156.4641 | +0.0000 | 一致 |
| book4｜full | 1440 | +380.0750 | +0.0000 | 一致 |

24 个「场景 × 段 × 周期」子行(6 个场景×段 × 4 个周期)全部 Δ = 0.0000,
6 个主行的逐笔摘要也全部一致;非零 Δ 出现次数为 0。
容量口径 `admission_full_cost`(生产口径)不变。

## §2 逐笔摘要那条"红"是怎么回事(重要)

首轮闸门报「逐笔不同」而净收益 Δ=0。定位结论:**基线工件过期,不是回归**。

- 存下的摘要 `4ebc52fb…` 是在事件/路由词表去版本化**之前**抓的(当时键是 `E1`/`E3`),
  仓库里没有任何一版代码能产出它;
- 用基线自己的提交 `725cc28` 在隔离 worktree 复算 → `e7604c2f…`,与 HEAD **完全相同**。

⇒ 交易事实从未变化,变的只是命名。摘要对 `event_id`/`route`/`reason_exit` 字符串
敏感,任何改名都必须显式 `--capture` 重抓;该约束已写进 `TRADE_DIGEST_FIELDS`
注释与基线 payload(`schema: 2` + `note_digest`)。

## §3 替代前必须闭合的工程项(本轮已闭合)

| 缺口 | 后果 | 处置 |
|---|---|---|
| DOGE 帧未入库 | book4 基线在新克隆上**不可复现**(`aggregate.load_tf` 直接 FileNotFoundError) | 8 帧 + manifest 记录 + 合约条目入库(与 BTC/ETH/SOL 同口径:2h/4h/12h/1d 由 1h 聚合,30m 由 15m 聚合且过 1h 锚定断言) |
| `data.save` 按平台写 CRLF | manifest 的 sha256 只对 Windows 工作区成立,换机 checkout 出 LF 后必然对不上(仓库里 1h 与 30m `anchored_to_1h` 的过期哈希就是这么来的) | 显式 `lineterminator="\n"`,与 `.gitattributes`/`aggregate._csv_bytes` 统一;1h 记录按实际字节校正 |
| 分页按页长终止 | Bitget 单页上限 1D=90、12H=180 均 < `PAGE_LIMIT=200` ⇒ 长历史被静默截断(实测 1D 2 年只拿到 90 天) | 改按「到区间下界 / 页不再推进」终止;实测 1D 90 → 810 行;新增 2 条回归用例 |

复核:manifest 每条 `sha256` == 盘上字节;`data/raw/*.csv` 全为 LF;`pytest -q`
全绿(重构分支自身用例 477 passed / 1 skipped)。

## §4 残余风险(替代后仍需盯)

1. **实盘等价性是构造性的**:`scripts/live_runner.py` 只改名(`strategy_v6→routing`、
   `backtest_v6→replay`)与配置路径(`config/v21.json→config/strategy.json`),
   行为等价由 import 闭包断言(`tests/test_layering.py`)保证,**没有**实盘回放证据。
   ⇒ 首次切换先 `--mode paper` 跑够样本(建议 ≥ 100 笔或 ≥ 2 周,以先到者为准),
   对账口径用 `scripts/live_status.py` 三方核对。
2. **上游数据瑕疵**:DOGE 5m 有一根零成交 bar(`ts=1784404500000`,OHLC 全等),
   `validate` 已如实记录,不影响触发与收益。
3. **manifest 里 `aggtrades/*`、`binance_vision/*` 条目无对应文件**(历史研究产物,
   目录未入库),不影响生产链与闸门。
4. **历史研究文档仍写 `config/v21.json`**(`.codeartsdoer/specs/…`、`scripts/tf_exp/report.py`
   的叙述文字):按"历史原文不改写"保留,不构成失效引用。
5. 仓库内 `research/rounds/` 归档与 `src/` 里的退役模块(由 `tests/test_layering.py`
   的 `RETIRED` 清单栅栏隔离)是**有意保留**的可复现性资产,不是残留垃圾。

## §5 替代方式

```bash
# 1) 门禁先跑(必须 Δ = 0.0000)
python tools/replay_acceptance.py
pytest -q

# 2) 合并
git checkout main && git merge --no-ff refactor/domain-model
```

合并时注意:`data/raw/` 新增 8 个 DOGE CSV(约 19 MB),与既有 32 个 CSV
同一策略入库;`.gitattributes` 的 `data/raw/*.csv text eol=lf` 保证跨机字节一致。

## §6 增量批次(2026-10-23):单链路收敛 D3 / D9 / D13 / D15

在 §1 的 Δ=0 基线上继续收口,只做**不改数字**的项(设计与逐项状态见
`reports/refactor_20261023/DESIGN.md` §11)。

| 改动 | 语义后果 | 证据 |
|---|---|---|
| `live_runner` 删 v20 分支;`cognition.engine` 改为守卫 | 旧链从"可选回退"变成"启动失败" | `tests/test_live_runtime.py` 4 条新断言:配置无引擎开关、必须有平衡箱、拒绝非 v21 持仓管理器、拒绝退役引擎 |
| `split_boundary` 移入 `split.py` | 生产闭包不再 import 555 行研究方法学 | `tests/test_layering.py`:闭包含 `split`、不含 `research` |
| 平衡箱三窗口参数化 | 注释与实现不再漂移 | `tests/test_structure.py`:默认 (1,12,24)、rotations 门限、破位边缘过期 |
| `floor_step` 公开 | 回放与实盘同名 | `tests/test_broker.py` |

复核(本机,同一冻结数据):

```
python -m pytest -q            → 524 passed, 1 skipped in 25.05s
python tools/replay_acceptance.py --quiet-prep
   explore|dev  n= 816  +38.0407   Δ +0.0000   逐笔一致
   explore|test n= 346  +195.3545  Δ +0.0000   逐笔一致
   explore|full n=1153  +281.0042  Δ +0.0000   逐笔一致
   book4  |dev  n= 976  +176.3601  Δ +0.0000   逐笔一致
   book4  |test n= 462  +156.4641  Δ +0.0000   逐笔一致
   book4  |full n=1440  +380.0750  Δ +0.0000   逐笔一致
   [PASS] 全部场景与品类净收益 ≥ 基线
```

24 个「场景 × 段 × 周期」子行同样全部 Δ = +0.0000。

**未闭项**(不阻塞合并,但不要当成已完成):D1 容量统一(改数字)、D2/D5 计数口径
(`go_events` 在 `ACCEPT/REJECT` 配置下恒 0,修它会改冻结 counters)、D4/D6/D7/D8
退役批次、D10 改名、D14 脚本归档。顺序建议见 DESIGN §11。

**实盘动作**:合并后首次启动仍按 §4.1 先 `--mode paper`;单链路化的行为等价性
来自"启动即校验 + 只有一条实现",不是回放证据。冒烟:

```bash
python scripts/live_paper_check.py
python scripts/live_runner.py --mode paper
```
