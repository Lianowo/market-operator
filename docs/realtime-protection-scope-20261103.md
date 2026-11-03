# 分支交付边界与清理记录（2026-11-03）

## 允许的生产变化

对照实盘基线 `bfb448d`，本分支只保留：

1. 15m 收盘价格保护、每轮 5s 新鲜报价推进、重启/长停顿重锚及仓位时钟清理。
2. 顺趋势第一根强收盘快速 ACCEPT；非趋势普通三根确认不变。
3. 两项功能必要的配置、回归测试与部署说明。

## 已撤销或移出的内容

- J/I 风险配比、复利阶梯调整和本分支新增实盘同方向容量门。
- 普通 ACCEPT/REJECT 的 regime 黑名单参数、路由及测试。
- balance-mode rotation 实验开关、band、目标路由及测试。
- 两根快速确认、允许非趋势快速的开关、研究回放 CLI。
- 本轮未跟踪报告、临时日志、调试脚本和原始回放产物。

基线原有回放/研究设施不受本次删除影响；只移除本轮新增实验，不删除共享行情或实盘运行文件。

## 可核验的不变量

- `D:/LiAn/Web3Code/market-operator-realtime/config/live.json` 的完整 `risk` 段与基线相同。
- `D:/LiAn/Web3Code/market-operator-realtime/config/strategy.json` 仅新增 `fast_accept_atr=0.5`；普通三根、风险/复利、白名单及所有其他配置与基线相同。
- `D:/LiAn/Web3Code/market-operator-realtime/src/market_operator/routing.py`、复利测试与通用 live runtime 测试恢复基线；既有 v21 测试只显式选择旧 bar 时钟以继续验证旧路径。
- live 轮询只新增价格保护，报价不触发快速开仓；事件层、触发层共享相同的趋势门。

## 研究结果与历史

风险审计、震荡研究和历史 drawdown 归因以文档保留，明确数据复用、网格口径和不能外推的边界。旧“J 已验收”“27% 是下限”“rotation 早期已盈利”不再作为结论。

原始研究脚本/结果、整理前源码快照、未推送提交 bundle 均在工作区外本地保存：

`D:/LiAn/Web3Code/market-operator-research-archive/20261103-realtime-cleanup/`。

归档不进本次提交。仅合并重写尚未推送的四个本地提交；已发布的 `107d4a3` 及之前历史保持不变，不执行强制推送。

## 非部署操作

本次只整理开发分支并提交，不重启、切分支或改动 `D:/LiAn/Web3Code/market-operator` 的实盘进程和运行数据。恢复旧风险预算是范围控制，不是风险/收益承诺。