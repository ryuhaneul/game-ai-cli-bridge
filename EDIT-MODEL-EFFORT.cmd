@echo off
setlocal
chcp 65001 >nul
set "CONFIG=%~dp0bridge-config.json"
if not exist "%CONFIG%" (
  echo 설정파일을 찾지 못했습니다.
  pause
  exit /b 1
)
start "" notepad.exe "%CONFIG%"
if errorlevel 1 (
  echo 설정파일 열기 실패. Exit code: %ERRORLEVEL%
  pause
  exit /b %ERRORLEVEL%
)
exit /b 0
