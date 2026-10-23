@echo off
rem -----------------------------------------------------------------------
rem Direct runner entry -- NOT recommended: no supervisor, no auto restart.
rem Normal operation uses tools\schedule\live_supervisor.cmd (liveness check,
rem restart budget, daily storage gc). Keep this only for foreground debugging.
rem Singleton lock in main() fails closed, so two runners can never trade together.
rem -----------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\live_runner.py --mode live 1>>logs\live_stdout.log 2>>logs\live_stderr.log
exit /b %ERRORLEVEL%
