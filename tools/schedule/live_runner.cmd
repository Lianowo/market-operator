@echo off
rem ---------------------------------------------------------------------------
rem 实盘 runner 的启动入口(计划任务 MarketOperatorLive 调用;也可手动双击)。
rem 为什么用 cmd 包一层:任务动作直接是 cmd,只要 python 还活着任务状态就是
rem "Running" —— 任务计划程序据此判定实例存活,IgnoreNew + 重复触发才能做到
rem "在跑就不重复起、不在跑就 5 分钟内拉起"(2026-10-22 静默停损就是进程没了
rem 而无人察觉)。日志一律追加,不覆盖,便于事后复盘缺口。
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\live_runner.py --mode live 1>>logs\live_stdout.log 2>>logs\live_stderr.log
exit /b %ERRORLEVEL%
