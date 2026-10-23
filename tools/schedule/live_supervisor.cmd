@echo off
rem -----------------------------------------------------------------------
rem Live supervisor entry (Task Scheduler: \MarketOperator\MarketOperatorLive,
rem repeats every 3 min; a second instance exits immediately via singleton lock).
rem Liveness is judged ONLY by runtime/live/heartbeat.json time facts -- never by
rem process lists, ports, or the task's last-run result (2026-10-22 outage root).
rem See README section 'live ops (supervisor & storage)' for the full rationale.
rem -----------------------------------------------------------------------
setlocal
cd /d "%~dp0..\.."
if not exist logs mkdir logs
python scripts\live_supervisor.py 1>>logs\supervisor.log 2>>logs\live_stderr.log
exit /b %ERRORLEVEL%
