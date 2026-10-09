@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem Windows bootstrap for live-report-master. No admin rights required.
set "SKILL_DIR=%~dp0.."
set "PY_LAUNCH="
set "PY_ARGS="
where py >nul 2>nul
if not errorlevel 1 (
  set "PY_LAUNCH=py"
  set "PY_ARGS=-3"
)
if not defined PY_LAUNCH where python >nul 2>nul && set "PY_LAUNCH=python"
if not defined PY_LAUNCH if exist "%SKILL_DIR%\assets\python-runtime\python.exe" set "PY_LAUNCH=%SKILL_DIR%\assets\python-runtime\python.exe"
if not defined PY_LAUNCH if exist "%SKILL_DIR%\runtime\python\python.exe" set "PY_LAUNCH=%SKILL_DIR%\runtime\python\python.exe"
if defined PY_LAUNCH (
  echo Python found: %PY_LAUNCH% %PY_ARGS%
  "%PY_LAUNCH%" %PY_ARGS% --version
  exit /b 0
)

where winget >nul 2>nul
if errorlevel 1 (
  echo Python was not found and winget is unavailable.
  echo Install Python 3.10+ manually or place the offline runtime under:
  echo   "%SKILL_DIR%\assets\python-runtime"
  exit /b 2
)

echo Installing Python 3.12 for the current user...
winget install --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo Python installation failed. Check network access or use the offline package.
  exit /b 2
)
echo Python installed. Restart the Agent terminal and run the report build again.
exit /b 0
