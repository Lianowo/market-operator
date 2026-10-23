@echo off
rem ---------------------------------------------------------------------------
rem 实盘 runner 直启入口(**不推荐**:没有看护、没有自动重启)。
rem 常态请用 tools\schedule\live_supervisor.cmd —— 它负责判活/重启/每日存储治理。
rem 本文件只用于排障时前台观察,或明确不需要看护的场景。
rem 单实例锁在 main() 里:重复启动会 fail-closed 退出,不会出现两个 runner 同时下单。
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\live_runner.py --mode live 1>>logs\live_stdout.log 2>>logs\live_stderr.log
exit /b %ERRORLEVEL%
