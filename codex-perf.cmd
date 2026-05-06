@echo off
setlocal

set "ROOT_DIR=%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%ROOT_DIR%scripts\fix-codex-perf.py" stop -y
  if ERRORLEVEL 1 exit /b %ERRORLEVEL%
  py -3 "%ROOT_DIR%scripts\fix-codex-perf.py" status --quiet --exit-code
  if ERRORLEVEL 3 exit /b %ERRORLEVEL%
  if ERRORLEVEL 2 py -3 "%ROOT_DIR%scripts\fix-codex-perf.py" repair -y
  if ERRORLEVEL 1 exit /b %ERRORLEVEL%
  py -3 "%ROOT_DIR%scripts\codex-perf-launch.py" --no-measure %*
  exit /b %ERRORLEVEL%
)

python "%ROOT_DIR%scripts\fix-codex-perf.py" stop -y
if ERRORLEVEL 1 exit /b %ERRORLEVEL%
python "%ROOT_DIR%scripts\fix-codex-perf.py" status --quiet --exit-code
if ERRORLEVEL 3 exit /b %ERRORLEVEL%
if ERRORLEVEL 2 python "%ROOT_DIR%scripts\fix-codex-perf.py" repair -y
if ERRORLEVEL 1 exit /b %ERRORLEVEL%
python "%ROOT_DIR%scripts\codex-perf-launch.py" --no-measure %*
exit /b %ERRORLEVEL%
