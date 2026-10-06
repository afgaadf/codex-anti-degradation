# 一次性跑完全部验证
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$suites = @(
  @{ Name = 'supervisor 核心（闸门/规则锁/恢复）'; File = 'tests\test_supervisor.py' },
  @{ Name = 'PreToolUse 闸门（拦截/放行/白名单）'; File = 'tests\test_hook.py' },
  @{ Name = '会话级（上下文上报/压缩/注入）';     File = 'tests\test_session_hook.py' }
)

$failed = @()
foreach ($s in $suites) {
  Write-Host ""
  Write-Host "=== $($s.Name) ===" -ForegroundColor Cyan
  python (Join-Path $here $s.File)
  if ($LASTEXITCODE -ne 0) { $failed += $s.Name }
}

Write-Host ""
if ($failed.Count -eq 0) {
  Write-Host "全部通过" -ForegroundColor Green
  exit 0
} else {
  Write-Host "失败: $($failed -join ', ')" -ForegroundColor Red
  exit 1
}
