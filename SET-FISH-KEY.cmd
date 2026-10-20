@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0MANAGE-KEY.ps1" -Key fish -Action Set
set "KEY_EXIT=%ERRORLEVEL%"
echo.
if "%KEY_EXIT%"=="0" (
  echo SUCCESS: Fish Audio API key 설정 완료
) else (
  echo FAILED: Fish Audio API key 설정 실패
)
echo Exit code: %KEY_EXIT%
pause
exit /b %KEY_EXIT%
