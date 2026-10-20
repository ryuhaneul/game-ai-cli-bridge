@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0CHECK-BRIDGE.ps1" -NoPause
set "CHECK_EXIT=%ERRORLEVEL%"
echo.
echo Exit code: %CHECK_EXIT%
pause
exit /b %CHECK_EXIT%
