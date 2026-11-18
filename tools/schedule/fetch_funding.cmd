@echo off
rem ---------------------------------------------------------------------------
rem RWA funding-history refresh entry (weekly, incremental).
rem Normal operation: Task Scheduler \MarketOperator\MarketOperatorFunding runs
rem weekly as  python.exe scripts\fetch_funding.py --source bitget --all-rwa
rem   --days 90 --incremental   (absolute interpreter path, working dir = repo root).
rem Scope: only RWA contracts passing config/live.json's universe volume floor.
rem First run backfills 90 days per symbol; later runs only fetch the tail.
rem Output: data\raw\funding\{SYM}.csv (+ data\manifest.json) and
rem   <log dir>\fetch_funding.log.
rem Manual/foreground use: pass args through.
rem ---------------------------------------------------------------------------
setlocal
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
cd /d "%~dp0..\.."
set "LOGDIR="
for /f "usebackq delims=" %%d in (`python -c "import sys;sys.path.insert(0,'src');from market_operator import paths;print(paths.log_dir())" 2^>nul`) do set "LOGDIR=%%d"
if not defined LOGDIR set "LOGDIR=logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if "%~1"=="" (
  python scripts\fetch_funding.py --source bitget --all-rwa --days 90 --incremental 1>>"%LOGDIR%\fetch_funding.log" 2>&1
) else (
  python scripts\fetch_funding.py %*
)
exit /b %ERRORLEVEL%
