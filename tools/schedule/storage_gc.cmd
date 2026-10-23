@echo off
rem ---------------------------------------------------------------------------
rem Storage gc entry (manual / foreground use).
rem Normal operation: Task Scheduler \MarketOperator\MarketOperatorStorageGC runs
rem daily 04:10 as  python.exe scripts\storage_gc.py --enforce  (absolute
rem interpreter path, working dir = repo root); the supervisor also calls gc every
rem supervisor.gc_hours = 24h and records rc + tail in logs\supervisor.jsonl.
rem Deletes only reproducible artifacts: logs -> gz, backups -> zip, caches/tmp.
rem Never deletes .gz audit archives, raw data, config, or frozen research output.
rem Exit codes: 0 = within budget, 1 = 30d forecast overshoot, 2 = over budget.
rem Details: logs\storage_gc.log   Growth curve: reports\_storage_growth.json
rem ---------------------------------------------------------------------------
setlocal
rem Force UTF-8 for redirected stdout/stderr (default cp936 would mix encodings inside one log file).
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist logs mkdir logs
if "%~1"=="" (
  python scripts\storage_gc.py --enforce 1>>logs\storage_gc.log 2>&1
) else (
  python scripts\storage_gc.py %*
)
exit /b %ERRORLEVEL%
