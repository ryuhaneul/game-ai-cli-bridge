@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0MANAGE-KEY.ps1" -Key openai -Action Remove
set "KEY_EXIT=%ERRORLEVEL%"
echo.
if "%KEY_EXIT%"=="0" (
  echo SUCCESS: 저장된 OpenAI embedding key 삭제 완료
) else (
  echo FAILED: OpenAI embedding key 삭제 실패
)
echo Exit code: %KEY_EXIT%
pause
exit /b %KEY_EXIT%
