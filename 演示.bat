@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "启动.bat" (
  echo 缺少启动.bat，请确认演示.bat与启动.bat位于同一目录。
  pause
  exit /b 1
)
call "启动.bat" --demo %*
exit /b %ERRORLEVEL%
