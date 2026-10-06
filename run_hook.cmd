@echo off
rem ============================================================
rem  anti-degradation hook launcher
rem
rem  Why this exists:
rem    hooks.json used to point straight at one pwsh.exe path. If that
rem    path disappears (WindowsApps / MSIX AppExecLink aliases can fail
rem    to activate; versioned runtime dirs get cleaned up) the hook dies
rem    silently and the gate stops running. This launcher probes several
rem    pwsh locations, then falls back to Windows PowerShell 5.1.
rem
rem  Usage: run_hook.cmd <supervisor|session|post>
rem
rem  Note: all hook .ps1 files are UTF-8 *with BOM*, so PowerShell 5.1
rem        reads them correctly too.
rem ============================================================
setlocal

set "PSC="
set "MODE=%~1"

rem order matters: prefer stable, non-versioned paths first
if not defined PSC if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PSC=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not defined PSC if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe" set "PSC=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
if not defined PSC if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe" set "PSC=%LOCALAPPDATA%\Microsoft\WindowsApps\pwsh.exe"
if not defined PSC set "PSC=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if /I "%MODE%"=="session" (
  "%PSC%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0hook_session.ps1"
) else if /I "%MODE%"=="stop" (
  "%PSC%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0hook_session.ps1"
) else if /I "%MODE%"=="post" (
  "%PSC%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0hook_post.ps1"
) else (
  "%PSC%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0hook_supervisor.ps1"
)

exit /b %ERRORLEVEL%