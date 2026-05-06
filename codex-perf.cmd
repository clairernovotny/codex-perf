@echo off
setlocal

set "ROOT_DIR=%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  start "" /b py -3 "%ROOT_DIR%scripts\codex-perf-launch.py" %* >nul 2>nul
  exit /b 0
)

start "" /b python "%ROOT_DIR%scripts\codex-perf-launch.py" %* >nul 2>nul
exit /b 0
