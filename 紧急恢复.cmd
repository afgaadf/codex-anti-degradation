@echo off
chcp 65001>nul
setlocal EnableExtensions
title 管家 - 紧急恢复
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=utf-8"
if not exist "%ROOT%\supervisor.py" (
  echo 找不到 supervisor.py：%ROOT%\supervisor.py
  echo 按任意键关闭...
  pause >nul
  exit /b 2
)
echo.
echo   管家 - 紧急恢复
echo   --------------------------------
echo   把管家从任何状态拉回可用：清阻断、清维护模式、重置会话。
echo   只动运行状态，不改规则、不改信任、不改 hook 接线。
echo.
"%PY%" "%ROOT%\supervisor.py" rescue --by "%USERNAME%" --reason "桌面一键紧急恢复"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo   完成。管家已恢复可用。
) else (
  echo   恢复命令返回 %RC%，请把上面的输出发给维护者。
)
echo.
echo   按任意键关闭...
pause >nul
exit /b %RC%