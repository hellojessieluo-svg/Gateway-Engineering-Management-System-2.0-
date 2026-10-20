@echo off
rem ASCII only, on purpose: desktop shortcuts pass this path through COM,
rem which turns non-ASCII into "?".
setlocal
set "APP_DIR=%~dp0"
rem Fixed port so bookmarks keep working across restarts; override with GATE_PORT.
rem Extra args pass through: launch_gate_daemon.cmd adds --no-open.
if not defined GATE_PORT set "GATE_PORT=8790"
set "GATE_ARGS=%GATE_PORT% %*"

rem Runtime search order: GATE_PYTHON (a folder holding python.exe / pythonw.exe),
rem then the default per-user installs, then the py launcher / pythonw on PATH.
set "RUNTIME_1=%GATE_PYTHON%"
set "RUNTIME_2=%LOCALAPPDATA%\Programs\Python\Python314"
set "RUNTIME_3=%LOCALAPPDATA%\Programs\Python\Python313"
set "RUNTIME_4=%LOCALAPPDATA%\Programs\Python\Python312"

pushd "%APP_DIR%"

if defined GATE_PYTHON call :try "%RUNTIME_1%" && goto :ok
call :try "%RUNTIME_2%" && goto :ok
call :try "%RUNTIME_3%" && goto :ok
call :try "%RUNTIME_4%" && goto :ok
call :try_path && goto :ok

popd
echo Python runtime was not found. Install Python 3.12+ or set GATE_PYTHON.
pause
exit /b 1

:ok
popd
exit /b 0

:try
rem pythonw first: no console window. The page has a "Quit" button in the top
rem right, and launching again just reopens the tab of the instance already
rem running instead of starting a second server on the same ledger.
rem If it ever fails to start, look for server-error.log next to config.json
rem under %LOCALAPPDATA%\Gateway.
if exist "%~1\pythonw.exe" (
  start "" "%~1\pythonw.exe" -m gate_api.server %GATE_ARGS%
  exit /b 0
)
if exist "%~1\python.exe" (
  start "gateway" "%~1\python.exe" -m gate_api.server %GATE_ARGS%
  exit /b 0
)
exit /b 1

:try_path
where pyw >nul 2>nul && (
  start "" pyw -3 -m gate_api.server %GATE_ARGS%
  exit /b 0
)
where pythonw >nul 2>nul && (
  start "" pythonw -m gate_api.server %GATE_ARGS%
  exit /b 0
)
where py >nul 2>nul && (
  start "gateway" py -3 -m gate_api.server %GATE_ARGS%
  exit /b 0
)
exit /b 1
