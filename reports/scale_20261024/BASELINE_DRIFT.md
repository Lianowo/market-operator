# 与冻结基线的漂移归因(2026-10-24)

> 用途:回答「现在的 HEAD 相对 main / 冻结基线,同条件回测有没有退步」。
> 全部数字来自本机同数据、同命令的实测,不是引用。

## 结论(先说)

1. **相对 main(`14a22b5`)本身:没有退步,也没有变好 —— 逐位相同。**
   `refactor/domain-model` 相对 main 在策略/交易链上的净改动只有
   `price_only` 执行细 bar 出口(9c1f058)与 `pivot_exec`(f56b3c1),
   **两者默认关闭**;实测 HEAD 的 6 行净收益与合并 main 后的 8a63992 完全一致。
2. **相对 `tests/fixtures/replay_baseline.json`(冻结基线):全面低于基线,闸门 FAIL。**
   explore|dev +18.01 / test +70.46 / full +74.32;
   book4|dev +111.89 / **test −11.06** / full +62.94。
3. **漂移不是本分支造成的,是 main 的 `cfff3b7` 随合并进来的**:
   `opp_lock_max_giveback_r=0.25` 接进回放链 + `tighten_lock` 落位闸门。
   把这两项还原 ⇒ **Δ 精确回到 0.0000**(6 行数字逐位等于基线)。
4. 因此 `reports/refactor_20261023/ACCEPTANCE.md` §1/§6 的「Δ=0」在当前 HEAD
   **已经过期**(它成立到 `f4c0a7d` 为止),需要在合并 main 后重新冻结或重新说明。

## 一、方法

```bash
# 同一数据、同一命令、只换代码/配置
python tools/replay_acceptance.py --quiet-prep                 # 用 config/strategy.json
python tools/replay_acceptance.py --quiet-prep --config X.json # 换配置
```

隔离 worktree(`..\_mo89`)里 checkout 指定提交,避免污染工作区。
判据沿用 `docs/acceptance.md`:净收益 ≥ 基线(容差 1e-6),逐笔摘要只报告。

## 二、2×2 归因(explore / book4,1x 成本,300U 起点)

| 代码 | 配置 | explore(dev/test/full) | book4(dev/test/full) |
|---|---|---|---|
| 落位闸门(现行) | `giveback=0.25`(现行) | 864 **+18.01** / **+70.46** / **+74.32** | 1044 **+111.89** / **−11.06** / **+62.94** |
| 旧 `tighten_lock` | `giveback=0.25` | 864 +25.74 / +70.46 / +92.41 | 1045 +111.77 / −11.06 / +62.83 |
| 落位闸门(现行) | `giveback=0` | 820 +5.57 / +195.35 / +188.26 | 976 +178.22 / +156.46 / +377.53 |
| 旧 `tighten_lock` | `giveback=0` | 816 **+38.04** / **+195.35** / **+281.00** | 976 **+176.36** / **+156.46** / **+380.08** |

最后一行 = 冻结基线本身(Δ = 0.0000,逐笔摘要也一致);`n` 为 dev 段成交数。

拆开看,`giveback=0.25` 是主因,落位闸门是次因:

| 场景|段 | 基线 | `giveback` 单项 | 闸门再叠加 | 合计 Δ |
|---|---:|---:|---:|---:|
| explore\|dev | +38.04 | −12.30 | −7.74 | **−20.03** |
| explore\|test | +195.35 | −124.90 | 0.00 | **−124.90** |
| explore\|full | +281.00 | −188.59 | −18.09 | **−206.68** |
| book4\|dev | +176.36 | −64.59 | −0.11 | **−64.47** |
| book4\|test | +156.46 | −167.53 | 0.00 | **−167.53** |
| book4\|full | +380.08 | −317.25 | +0.11 | **−317.14** |

## 三、机制对上症状:被扣掉的是「锁利价落在现价身后」的虚拟利润

`book4|full` 按事件拆:

| 事件 | 基线 | HEAD | Δ |
|---|---:|---:|---:|
| LEVEL_REJECTED | +422.13 | +27.72 | **−394.41** |
| 其余事件合计 | −42.05 | +35.22 | +77.27 |

按周期拆(book4|test,唯一由正转负的段):

| 周期 | 基线 | HEAD | Δ |
|---|---:|---:|---:|
| 2h | +7.54 | −61.34 | −68.88 |
| 4h | +100.09 | +31.22 | −68.87 |
| 12h | +50.24 | +27.46 | −22.78 |
| 1d | −1.40 | −8.40 | −7.00 |

`REJECT` 分支的锁利口径原本按 `entry + frac·(target−entry)` 落位,只在收盘裁决;
收盘价常年已经越过该位置 ⇒ 回测里「锁在现价身后」的止损仍被记为有效推进,
单一事件族贡献 +422。cfff3b7 要求锁利价必须①在当下可见价的保护侧②在 entry 的
盈利侧,否则不推进、不置 `reduced`;`giveback=0.25R` 再把锁价锚在现价而非计划行程。
两者一起把「回测里能锁、实盘锁不到」的那部分利润扣掉,数字因此塌下来。

**这不是本分支引入的回归,而是基线本身含有一段不可实盘的利润。**
但这也不代表可以照抄旧基线当作「main 的水位」:main 的现行行为就是 HEAD 这套。

## 四、本分支相对 main 的实际改动(逐项是否改数字)

| 改动 | 位置 | 默认 | 是否改数字 |
|---|---|---|---|
| `strategy_v6.py → routing.py`、`backtest_v6.py → replay.py` | src | — | 否(改名/搬迁) |
| 事件词表语义化(`E1..E8 →` 语义名) | events/routing/config | — | 否(摘要串变化,净值不变) |
| `capacity.py` 统一三套风险预算公式 | src | — | 否(`admission_full_cost` 不变) |
| `scenario.py` + `tools/replay_acceptance.py` | src/tools | — | 新增验收工具,不参与生产 |
| `split.py` 抽 `split_boundary` | src | — | 否 |
| `ArmConfig.exit_exec` / `exit_exec_trail` | campaign/position | **False** | 否(默认关) |
| `CampaignFrame.exec_pivots` + `ArmConfig.pivot_exec` | campaign/position | **False** | 否(默认关) |
| `price_only` 出口只走价格型分支 | position | 由上面开关驱动 | 否(关时同一路径) |

证据:HEAD 的 6 行净收益与 `8a63992`(合并 main 后、本分支全部实验开关落地前)
逐位相同;`f4c0a7d`(合并前)与 `89d64e4` 对冻结基线 Δ=0.0000。

## 五、待决策(需要人来定,不是工程问题)

1. **把 `opp_lock_max_giveback_r` 调回 0**:回测立刻回到旧水位,但等于把
   GOOGLUSDT 那类「锁利价已在现价身后」的实盘缺陷放回去 —— 不建议为数字这样做。
2. **接受新水位并重新冻结基线**(推荐):用 `--capture` 重抓 `replay_baseline.json`,
   同时改 `docs/acceptance.md`、`reports/refactor_20261023/ACCEPTANCE.md` 里的
   「Δ=0 / test 全正」表述,并把 v21 协议里 `book4|test +156` 一类结论标注为
   「旧锁利口径下的读数」。这是策略口径变更,必须显式记账,不能悄悄改。
3. 若只想让研究与实盘同口径,保留 0.25R;**不要**在研究脚本里偷偷设 0
   (`_diag_v21_backtest.py` 就因为不传 `arm` 而一直跑在 `opp_lock` 关闭的口径上)。

```bash
# 复核(约 2 分钟/次,两次 prep 各 45~65s)
python tools/replay_acceptance.py --quiet-prep                       # 现行 => FAIL
python -c "import json,pathlib;d=json.loads(pathlib.Path('config/strategy.json').read_text(encoding='utf-8'));d['arm_config']['opp_lock_max_giveback_r']=0.0;pathlib.Path('cfg0.json').write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8')"
python tools/replay_acceptance.py --quiet-prep --config cfg0.json    # 关 giveback => 仍 FAIL(闸门)
```
