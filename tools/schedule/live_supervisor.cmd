@echo off
rem ---------------------------------------------------------------------------
rem Live supervisor entry (manual / foreground use).
rem Normal operation: Task Scheduler \MarketOperator\MarketOperatorLive repeats
rem every 3 min and calls the interpreter directly with an absolute path --
rem python.exe scripts\live_supervisor.py  (working dir = repo root).
rem Reason: in a Task Scheduler context `cmd.exe /c *.cmd` + log redirects can fail
rem at open time (the live runner owns logs/live_stdout.log and logs/live_stderr.log
rem exclusively), which kills cmd with exit code 0 and writes nothing at all.
rem A second supervisor exits immediately via the singleton lock, so triggering the
rem task repeatedly is exactly "start it if it is not running yet".
rem Liveness is judged ONLY by runtime/live/heartbeat.json time facts -- never by
rem process lists, ports, or the task's last-run result (2026-10-22 outage root).
rem See README section 'live ops (supervisor & storage)' for the full rationale.
rem ---------------------------------------------------------------------------
setlocal
rem Force UTF-8 for redirected stdout/stderr (default cp936 would mix encodings inside one log file).
set PYTHONUTF8=1
cd /d "%~dp0..\.."
if not exist logs mkdir logs
rem No redirection at all: the daemon mirrors its console lines into
rem logs/supervisor.log itself and records tracebacks as kind=crash. Any
rem redirect added here would collide with the open handle of the instance
rem that is already running, and cmd would die at open time with a cryptic
rem 'used by another process' and exit code 0 -- no log, no trace, no clue.
if "%~1"=="" (
  python scripts\live_supervisor.py
) else (
  python scripts\live_supervisor.py %*
)
exit /b %ERRORLEVEL%
