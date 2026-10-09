@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "PACKAGE_DIR=%~dp0.."
rem Registry/data live outside the skill package (per-user) so running the server
rem never bloats the skill directory with a large indexed dataset.
set "PORT=8765"
set "STATE_DIR=%USERPROFILE%\.ai-html-report"
set "REGISTRY=%STATE_DIR%\registry.json"
set "DATA_DIR=%STATE_DIR%\data"
set "PID_FILE=%STATE_DIR%\server.pid"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--port" (
  set "PORT=%~2"
  shift
  shift
  goto parse
)
if /i "%~1"=="--registry" (
  set "REGISTRY=%~2"
  shift
  shift
  goto parse
)
if /i "%~1"=="--data-dir" (
  set "DATA_DIR=%~2"
  shift
  shift
  goto parse
)
if /i "%~1"=="--pid-file" (
  set "PID_FILE=%~2"
  shift
  shift
  goto parse
)
echo Unknown argument: %~1
exit /b 2

:parsed
set "PY_LAUNCH="
set "PY_ARGS="
where py >nul 2>nul
if not errorlevel 1 (
  set "PY_LAUNCH=py"
  set "PY_ARGS=-3"
)
if not defined PY_LAUNCH (
  where python >nul 2>nul
  if not errorlevel 1 set "PY_LAUNCH=python"
)
if not defined PY_LAUNCH if exist "%PACKAGE_DIR%\assets\python-runtime\python.exe" set "PY_LAUNCH=%PACKAGE_DIR%\assets\python-runtime\python.exe"
if not defined PY_LAUNCH if exist "%PACKAGE_DIR%\runtime\python\python.exe" set "PY_LAUNCH=%PACKAGE_DIR%\runtime\python\python.exe"
if not defined PY_LAUNCH (
  call "%~dp0bootstrap_windows.cmd"
  exit /b %errorlevel%
)

if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
for %%I in ("%REGISTRY%") do if not exist "%%~dpI" mkdir "%%~dpI"
for %%I in ("%PID_FILE%") do if not exist "%%~dpI" mkdir "%%~dpI"
start "AI Report Server" /b "%PY_LAUNCH%" %PY_ARGS% "%~dp0run_server.py" --host 0.0.0.0 --port "%PORT%" --registry "%REGISTRY%" --data-dir "%DATA_DIR%" --pid-file "%PID_FILE%" --quiet
timeout /t 1 /nobreak >nul
echo Server started on http://127.0.0.1:%PORT%
exit /b 0
