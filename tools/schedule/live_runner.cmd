@echo off
rem ---------------------------------------------------------------------------
rem Direct runner entry -- NOT recommended: no supervisor, no auto restart.
rem Normal operation uses the supervisor (liveness check, restart budget, daily
rem storage gc). Keep this only for foreground debugging.
rem The singleton lock in main() fails closed, so two runners can never trade
rem together; if a runner is already live this script simply exits non-zero.
rem Console output goes to <log dir>\live_runner_console.log (NOT live_stdout.log:
rem that file belongs to the supervised runner and is opened exclusively).
rem <log dir> comes from paths.log_dir(): `logs` in legacy layout, `var\logs` in portable.
rem ---------------------------------------------------------------------------
setlocal
rem Force UTF-8 for redirected stdout/stderr (default cp936 would mix encodings inside one log file).
set PYTHONUTF8=1
cd /d "%~dp0..\.."
rem Resolve the log dir from paths (portable layout => var\logs). Hardcoding `logs`
rem would leave a stray empty `logs\` in portable layout AND send this log somewhere
rem the daily inventory never looks. Fallback `logs` only when python is unavailable.
set "LOGDIR="
for /f "usebackq delims=" %%d in (`python -c "import sys;sys.path.insert(0,'src');from market_operator import paths;print(paths.log_dir())" 2^>nul`) do set "LOGDIR=%%d"
if not defined LOGDIR set "LOGDIR=logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
if "%~1"=="" (
  python scripts\live_runner.py --mode live 1>>"%LOGDIR%\live_runner_console.log" 2>&1
) else (
  python scripts\live_runner.py %*
)
exit /b %ERRORLEVEL%
