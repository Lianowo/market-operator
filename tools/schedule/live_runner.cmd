@echo off
rem ---------------------------------------------------------------------------
rem Direct runner entry -- NOT recommended: no supervisor, no auto restart.
rem Normal operation uses the supervisor (liveness check, restart budget, daily
rem storage gc). Keep this only for foreground debugging.
rem The singleton lock in main() fails closed, so two runners can never trade
rem together; if a runner is already live this script simply exits non-zero.
rem Console output goes to logs\live_runner_console.log (NOT live_stdout.log:
rem that file belongs to the supervised runner and is opened exclusively).
rem ---------------------------------------------------------------------------
setlocal
rem Force UTF-8 for redirected stdout/stderr (default cp936 would mix encodings inside one log file).
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist logs mkdir logs
if "%~1"=="" (
  python scripts\live_runner.py --mode live 1>>logs\live_runner_console.log 2>&1
) else (
  python scripts\live_runner.py %*
)
exit /b %ERRORLEVEL%
