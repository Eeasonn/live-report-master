@echo off
setlocal EnableExtensions
rem Stage the official Windows embeddable Python runtime for offline use.
rem This one-time preparation needs network access; the report skill itself
rem does not download anything at runtime.
set "DEST=%~dp0..\assets\python-runtime"
set "ARCHIVE=%TEMP%\ai-report-python-3.12.10-embed-amd64.zip"
set "URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
if not exist "%DEST%" mkdir "%DEST%"
where curl.exe >nul 2>nul
if errorlevel 1 (
  echo curl.exe is required. Windows 10 and later normally include it.
  exit /b 2
)
echo Downloading Python 3.12.10 embeddable runtime...
curl.exe --fail --location --retry 3 --output "%ARCHIVE%" "%URL%"
if errorlevel 1 (
  echo Download failed: %URL%
  exit /b 2
)
echo Extracting runtime to "%DEST%"...
tar.exe -xf "%ARCHIVE%" -C "%DEST%"
if errorlevel 1 (
  echo Extraction failed. Unzip the archive manually into "%DEST%".
  exit /b 2
)
del /q "%ARCHIVE%" >nul 2>nul
if not exist "%DEST%\python.exe" (
  echo python.exe was not found after extraction.
  exit /b 2
)
echo Offline Python runtime is ready.
exit /b 0
