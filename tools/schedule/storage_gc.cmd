@echo off
rem ---------------------------------------------------------------------------
rem 存储治理(计划任务 MarketOperatorStorageGC 每日一次;看护也会按 gc_hours 顺带调用)。
rem 只清"可再生产物":日志转 gz、备份打 zip、缓存/临时文件删除;
rem 绝不删 .gz 审计归档、不删源数据/配置/研究冻结产物。
rem 退出码:0=预算内,1=30 天预测将越线,2=已超硬预算(需人工扩容)。
rem 明细写入 logs\storage_gc.log,增长曲线写入 reports\_storage_growth.json。
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\storage_gc.py --enforce >> logs\storage_gc.log 2>&1
exit /b %ERRORLEVEL%
