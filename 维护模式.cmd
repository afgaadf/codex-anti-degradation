@echo off
chcp 65001>nul
setlocal EnableExtensions
set "ROOT=C:\Users\taich\.codex\anti-degradation"
set "PY=C:\Users\taich\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=utf-8"
if not exist "%ROOT%\supervisor.py" (
  echo 找不到 supervisor.py：%ROOT%\supervisor.py
  echo 按任意键关闭...
  pause >nul
  exit /b 2
)
if not "%~1"=="" goto arg
:menu
echo.
echo  管家维护模式（外部、限时、非常驻）
echo  1. 进入维护模式（60 分钟）
echo  2. 退出维护模式
echo  3. 查看维护状态
echo  4. 退出
echo.
choice /c 1234 /n /m "请选择 [1-4]："
if errorlevel 4 exit /b 0
if errorlevel 3 goto status
if errorlevel 2 goto off
if errorlevel 1 goto on
goto menu
:arg
if /i "%~1"=="on" goto on
if /i "%~1"=="off" goto off
if /i "%~1"=="status" goto status
echo 用法：维护模式.cmd [on ^| off ^| status]
echo 按任意键关闭...
pause >nul
exit /b 2
:on
set "REASON=%~2"
if "%REASON%"=="" set /p REASON=请输入维护原因：
if "%REASON%"=="" set "REASON=外部维护模式"
"%PY%" "%ROOT%\supervisor.py" maintenance on --by "%USERNAME%" --reason "%REASON%" --minutes 60 --human
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo 操作完成。) else (echo 操作失败，错误码 %RC%。)
echo 按任意键关闭...
pause >nul
exit /b %RC%
:off
set "REASON=%~2"
if "%REASON%"=="" set "REASON=外部退出维护"
"%PY%" "%ROOT%\supervisor.py" maintenance off --by "%USERNAME%" --reason "%REASON%" --human
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo 操作完成。) else (echo 操作失败，错误码 %RC%。)
echo 按任意键关闭...
pause >nul
exit /b %RC%
:status
"%PY%" "%ROOT%\supervisor.py" maintenance status --human
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo 操作完成。) else (echo 操作失败，错误码 %RC%。)
echo 按任意键关闭...
pause >nul
exit /b %RC%