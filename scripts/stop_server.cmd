@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "PACKAGE_DIR=%~dp0.."
rem Matches start_server.cmd: state lives outside the skill package (per-user).
set "STATE_DIR=%USERPROFILE%\.ai-html-report"
set "PID_FILE=%STATE_DIR%\server.pid"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--pid-file" (
  set "PID_FILE=%~2"
  shift
  shift
  goto parse
)
echo Unknown argument: %~1
exit /b 2

:parsed
if not exist "%PID_FILE%" (
  echo No pid file found; nothing to stop: "%PID_FILE%"
  exit /b 0
)
set /p REPORT_PID=<"%PID_FILE%"
if not defined REPORT_PID (
  del /q "%PID_FILE%" >nul 2>nul
  echo Empty pid file; removing it. Nothing to stop.
  exit /b 0
)

rem Refuse to kill an unrelated/reused PID: the process must actually be our
rem run_server.py. WMIC is removed on modern Windows 11, so use PowerShell's
rem CIM query as the primary check; wmic remains only as a legacy fallback.
set "IS_OURS="
powershell -NoProfile -Command "if ((Get-CimInstance Win32_Process -Filter 'ProcessId=%REPORT_PID%').CommandLine -like '*run_server.py*') { exit 0 } else { exit 1 }" >nul 2>nul
if not errorlevel 1 set "IS_OURS=1"
if not defined IS_OURS (
  wmic process where "ProcessId=%REPORT_PID%" get CommandLine /format:list >nul 2>nul
  if not errorlevel 1 (
    for /f "tokens=*" %%L in ('wmic process where "ProcessId=%REPORT_PID%" get CommandLine /value 2^>nul ^| findstr /i "run_server.py"') do set "IS_OURS=1"
  )
)
if not defined IS_OURS (
  echo PID %REPORT_PID% is not the AI report server; refusing to kill it.
  del /q "%PID_FILE%" >nul 2>nul
  exit /b 0
)

rem Graceful terminate (no /T to avoid killing an unrelated tree, no /F force).
taskkill /PID %REPORT_PID% >nul 2>nul
if not errorlevel 1 (
  rem Give it a moment to flush the pid file, then remove it.
  ping 127.0.0.1 -n 2 >nul
  if exist "%PID_FILE%" del /q "%PID_FILE%" >nul 2>nul
  echo Server stopped.
) else (
  echo Server process not running or already stopped.
)
exit /b 0
