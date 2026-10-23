@echo off
rem ---------------------------------------------------------------------------
rem 实盘看护(计划任务 MarketOperatorLiveSupervisor 开机/登录触发;也可手动双击)。
rem runner 由看护拉起:判活只认 runtime/live/heartbeat.json 的时间事实,
rem 不读进程表、不读端口、不看计划任务的"上次运行结果"(2026-10-22 掉线根因)。
rem 单实例锁:重复启动会立即退出;锁由内核释放,进程崩溃不会留下毒锁。
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\live_supervisor.py 1>>logs\supervisor.log 2>>logs\live_stderr.log
exit /b %ERRORLEVEL%
