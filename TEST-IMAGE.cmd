@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0TEST-IMAGE.ps1"
set "TEST_EXIT=%ERRORLEVEL%"
echo.
echo Image test exit code: %TEST_EXIT%
echo Log file: LAST-IMAGE-TEST.log
if "%TEST_EXIT%"=="0" (
  echo Result: PASS
) else (
  echo Result: FAIL
)
pause
exit /b %TEST_EXIT%
