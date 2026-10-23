@echo off
rem -----------------------------------------------------------------------
rem Storage gc entry (Task Scheduler: \MarketOperator\MarketOperatorStorageGC,
rem daily 04:10; the supervisor also calls it every supervisor.gc_hours).
rem Deletes only reproducible artifacts: logs -> gz, backups -> zip, caches/tmp.
rem Never deletes .gz audit archives, raw data, config, or frozen research output.
rem Exit codes: 0 = within budget, 1 = 30d forecast overshoot, 2 = over budget.
rem Details: logs\storage_gc.log   Growth curve: reports\_storage_growth.json
rem -----------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\storage_gc.py --enforce >> logs\storage_gc.log 2>&1
exit /b %ERRORLEVEL%
