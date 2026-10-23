@echo off
rem ---------------------------------------------------------------------------
rem 存储治理的每日入口(计划任务 MarketOperatorStorageGC 调用)。
rem 只做"可再生产物"的清理:日志过期 gz、旧备份打 zip、缓存清理、
rem 已有 .gz 孪生体的原文件去重。源数据/配置/研究冻结产物一律不动。
rem 预算与动作全部写进 logs\storage_gc.log,便于事后审计。
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\storage_gc.py --apply --dedupe-gz >> logs\storage_gc.log 2>&1
exit /b %ERRORLEVEL%
