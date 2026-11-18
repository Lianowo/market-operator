@echo off
rem ---------------------------------------------------------------------------
rem RWA session-calendar learning entry (weekly).
rem Normal operation: Task Scheduler \MarketOperator\MarketOperatorLearnSessions
rem runs daily/weekly as  python.exe scripts\learn_sessions.py --days 21 --workers 6
rem (absolute interpreter path, working dir = repo root). Output:
rem   data/calendars/assignments.json  (runtime data, read live by sessions.py;
rem   no restart needed) and <log dir>\learn_sessions.log.
rem Manual/foreground use: pass args through, e.g.
rem   tools\schedule\learn_sessions.cmd --symbols AAPLUSDT,XAUUSDT --dry-run
rem ---------------------------------------------------------------------------
setlocal
rem Force UTF-8 for redirected stdout/stderr (default cp936 would mix encodings inside one log file).
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
cd /d "%~dp0..\.."
set "LOGDIR="
for /f "usebackq delims=" %%d in (`python -c "import sys;sys.path.insert(0,'src');from market_operator import paths;print(paths.log_dir())" 2^>nul`) do set "LOGDIR=%%d"
if not defined LOGDIR set "LOGDIR=logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if not "%~1"=="" goto :passthrough
python scripts\learn_sessions.py --days 21 --workers 6 1>>"%LOGDIR%\learn_sessions.log" 2>&1
exit /b %ERRORLEVEL%
:passthrough
python scripts\learn_sessions.py %*
exit /b %ERRORLEVEL%
