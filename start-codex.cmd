@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0START-BRIDGE.ps1" -Provider codex
set "BRIDGE_EXIT=%ERRORLEVEL%"
echo.
echo Bridge가 종료되었습니다.
echo Exit code: %BRIDGE_EXIT%
pause
exit /b %BRIDGE_EXIT%
