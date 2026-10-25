@echo off
rem Cross-Windows one-command bootstrap. Double-click or run:
rem   bootstrap.cmd            -> bootstrap
rem   bootstrap.cmd doctor     -> any `mo` subcommand
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE="
where py >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>&1
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo [mo] Python 3.11+ not found. Install from https://www.python.org/downloads/
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [mo] creating .venv ...
  %PYEXE% -m venv .venv
  if errorlevel 1 goto :fail
)
set "VPY=.venv\Scripts\python.exe"

"%VPY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo [mo] .venv uses Python ^< 3.11. Delete .venv and retry with Python 3.11+.
  exit /b 1
)

if not exist ".venv\.mo-ready" (
  echo [mo] installing project ...
  "%VPY%" -m pip install --upgrade pip
  if errorlevel 1 goto :fail
  "%VPY%" -m pip install -e .
  if errorlevel 1 goto :fail
  > ".venv\.mo-ready" echo ok
)

if "%~1"=="" (
  "%VPY%" scripts\mo.py bootstrap
) else (
  "%VPY%" scripts\mo.py %*
)
exit /b %errorlevel%

:fail
echo [mo] setup failed. See messages above.
exit /b 1
