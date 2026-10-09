@echo off
setlocal EnableExtensions
set "REPORT_URL=%~1"
if /i "%~1"=="--url" set "REPORT_URL=%~2"
if not defined REPORT_URL (
  echo Usage: open_report.cmd ^<http://127.0.0.1:8765/reports/id^>
  exit /b 2
)
start "AI Report" "%REPORT_URL%"
exit /b 0
