<#
  安装 anti-degradation supervisor

  用法：
    -Plan                              只打印计划与 digest，不改任何东西
    -Apply -ExpectedDigest <sha256>    按计划安装；若 hooks.json 已漂移则拒绝
    (直接执行，不带参数)                简化路径：备份后安装

  安全设计（吸收 @hasna/hooks 的 drift 防护做法）：
    - 修改 hooks.json 前必定备份
    - -Apply 要求 digest 匹配，避免"看过计划之后文件被改"的竞态
    - 合并而非覆盖：已有 hook 原样保留
    - 幂等：重复执行不写重复条目
    - 优先使用 pwsh(PS7)，避免 Windows PowerShell 5.1 的 UTF-8/BOM 陷阱
#>
param(
  [switch]$Plan,
  [switch]$Apply,
  [string]$ExpectedDigest,
  [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$src   = Split-Path -Parent $MyInvocation.MyCommand.Path
$codex = Join-Path $env:USERPROFILE '.codex'
$dest  = Join-Path $codex 'anti-degradation'
$hooks = Join-Path $codex 'hooks.json'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
# hook 统一经 run_hook.cmd 启动：它按顺序探测 pwsh 并回退 powershell.exe，
# 避免把 hooks.json 钉死在某条可能失效的 pwsh 路径上（WindowsApps MSIX 别名等）。
# Codex 用 Rust Command::new(argv[0]) 启动（无 shell，引号会被当成路径一部分），
# 所以这里用无空格的绝对路径 cmd.exe，把 run_hook.cmd 作为参数交给它。
$cmdExe   = Join-Path $env:SystemRoot 'System32\cmd.exe'
$launcher = Join-Path $dest 'run_hook.cmd'

function Get-FileDigest([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return '(none)' }
  return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
}

function Get-ShellExe {
  # 优先 pwsh (PS7)：正确读取 UTF-8，无 BOM 陷阱。
  # 顺序很重要：先选"稳定路径"，避免绑定到含版本哈希的临时目录。
  $stable = @(
    # 注意：绝不使用含空格的路径。Codex 用 Rust Command::new(argv[0]) 启动 hook，
    # 引号会被当成路径的一部分 -> WinError 2，hook 静默失效（见 Assert-NoSpaceInPath）。
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'),
    (Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe')
  )
  foreach ($c in $stable) {
    if (-not (Test-Path -LiteralPath $c)) { continue }
    if ($c -match ' ') {
      Write-Host "跳过（含空格，hook 启动器不可用）: $c" -ForegroundColor Yellow
      continue
    }
    return $c
  }
  $p = Get-Command pwsh -ErrorAction SilentlyContinue
  if ($p) {
    if ($p.Source -match ' ') {
      Write-Host "提示: 找到 pwsh 但路径含空格，不能作为 hook 启动器: $($p.Source)" -ForegroundColor Yellow
    } else {
      Write-Host "提示: 仅找到非标准路径的 pwsh: $($p.Source)" -ForegroundColor Yellow
      return $p.Source
    }
  }
  return "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
}


function Assert-NoSpaceInPath([string]$p, [string]$what) {
  # 路径含空格就必须加引号，而 Codex 用 Rust Command::new(argv[0]) 会把引号
  # 当作路径的一部分 -> WinError 2，hook 静默失败。这里直接拦下。
  if ($p -match ' ') { throw "PowerShell 路径含空格，会导致 hook 无法启动: $what = $p" }
}

$shellExe = Get-ShellExe
Assert-NoSpaceInPath $shellExe 'PowerShell'
$shellIsPwsh = ($shellExe -match 'pwsh')

function New-HookEntry([string]$mode) {
  return [ordered]@{
    matcher = '.*'
    hooks = @(
      [ordered]@{
        type = 'command'
        command = ('{0} /d /c {1} {2}' -f $cmdExe, $launcher, $mode)
        timeout = 30
      }
    )
  }
}

function New-SessionHookEntry([string]$mode) {
  return [ordered]@{
    hooks = @(
      [ordered]@{
        type = 'command'
        command = ('{0} /d /c {1} {2}' -f $cmdExe, $launcher, $mode)
        timeout = 30
      }
    )
  }
}

$sessionEvents = @('UserPromptSubmit','PreCompact','PostCompact','SessionStart','SessionEnd','SubagentStart','SubagentStop')

Write-Host "== anti-degradation supervisor ==" -ForegroundColor Cyan
Write-Host "source     : $src"
Write-Host "target     : $dest"
Write-Host "shell      : $shellExe"
Write-Host "launcher   : $launcher"
if (-not $shellIsPwsh) {
  Write-Host "警告: 未找到 pwsh(PS7)，将回退到 Windows PowerShell 5.1（存在 UTF-8 读取风险）" -ForegroundColor Yellow
}
Write-Host "hooks.json : $hooks"
Write-Host "digest     : $(Get-FileDigest $hooks)"
Write-Host ""

# ---------------- Uninstall ----------------
if ($Uninstall) {
  if (-not (Test-Path -LiteralPath $hooks)) { Write-Host "hooks.json 不存在"; exit 0 }
  Copy-Item -LiteralPath $hooks -Destination "$hooks.bak-$stamp" -Force
  $conf = Get-Content -LiteralPath $hooks -Raw | ConvertFrom-Json
  foreach ($evt in @('PreToolUse','PostToolUse') + $sessionEvents) {
    if (-not $conf.hooks.$evt) { continue }
    $kept = @()
    foreach ($entry in @($conf.hooks.$evt)) {
      $ours = $false
      foreach ($h in @($entry.hooks)) {
        if ($h.command -and $h.command -match '(run_hook\.cmd|hook_(supervisor|session|post)\.ps1)') { $ours = $true }
      }
      if (-not $ours -and $null -ne $entry) { $kept += $entry }
    }
    $conf.hooks.$evt = $kept
  }
  $conf | ConvertTo-Json -Depth 14 | Set-Content -LiteralPath $hooks -Encoding utf8
  Write-Host "已移除全部闸门条目。备份: hooks.json.bak-$stamp" -ForegroundColor Yellow
  Write-Host "监督层目录保留: $dest" -ForegroundColor Yellow
  exit 0
}

# ---------------- Plan ----------------
if ($Plan) {
  Write-Host "计划:" -ForegroundColor Green
  Write-Host "  1. 复制 supervisor.py / hook_supervisor.ps1 / hook_session.ps1 / hook_post.ps1 等到 $dest"
  Write-Host "  2. 备份 hooks.json"
  Write-Host "  3. 全部经 run_hook.cmd 启动（自动探测 pwsh / 回退 powershell.exe）"
  Write-Host "     PreToolUse       <- run_hook.cmd supervisor  (matcher: .*)"
  Write-Host "     PostToolUse      <- run_hook.cmd post        (matcher: .*)"
  foreach ($e in $sessionEvents) { Write-Host "     $($e.PadRight(16))<- run_hook.cmd session" }
  Write-Host "  4. 保留所有既有 hook 条目（如 view_image_guard）"
  Write-Host ""
  Write-Host "EXPECTED_DIGEST=$(Get-FileDigest $hooks)"
  Write-Host ""
  Write-Host "确认后执行:" -ForegroundColor Cyan
  Write-Host "  -Apply -ExpectedDigest $(Get-FileDigest $hooks)"
  exit 0
}

if ($Apply -and $ExpectedDigest) {
  $now = Get-FileDigest $hooks
  if ($now -ne $ExpectedDigest.ToLower()) {
    Write-Host "拒绝安装：hooks.json 已漂移" -ForegroundColor Red
    Write-Host "  期望: $($ExpectedDigest.ToLower())"
    Write-Host "  实际: $now"
    Write-Host "请重新运行 -Plan 获取最新计划。" -ForegroundColor Yellow
    exit 2
  }
  Write-Host "digest 校验通过" -ForegroundColor Green
}

# ---------------- Apply ----------------
if (-not (Test-Path -LiteralPath $dest)) { New-Item -ItemType Directory -Force -Path $dest | Out-Null }
foreach ($d in @('rules','rules\approvals','checks','ledger','state','logs')) {
  $p = Join-Path $dest $d
  if (-not (Test-Path -LiteralPath $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}

foreach ($f in @('supervisor.py','hook_supervisor.ps1','hook_session.ps1','hook_post.ps1','run_hook.cmd','README.md','run_tests.ps1')) {
  $s = Join-Path $src $f
  if (Test-Path -LiteralPath $s) { Copy-Item -LiteralPath $s -Destination $dest -Force }
}
$testsDest = Join-Path $dest 'tests'
if (-not (Test-Path -LiteralPath $testsDest)) { New-Item -ItemType Directory -Force -Path $testsDest | Out-Null }
foreach ($f in @('test_supervisor.py','test_hook.py','test_session_hook.py')) {
  $s = Join-Path $src "tests\$f"
  if (Test-Path -LiteralPath $s) { Copy-Item -LiteralPath $s -Destination $testsDest -Force }
}

$cfg = [ordered]@{ supervisor_home = $dest; python = 'python'; shell = $shellExe }
$cfg | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $dest 'hook_config.json') -Encoding utf8

$rulesTarget = Join-Path $dest 'rules\rules.json'
if (-not (Test-Path -LiteralPath $rulesTarget)) {
  Push-Location $dest
  & python (Join-Path $dest 'supervisor.py') init | Out-Null
  Pop-Location
  Write-Host "已初始化监督层规则" -ForegroundColor Green
} else {
  Write-Host "已存在规则，保留原阈值" -ForegroundColor Green
}

if (-not (Test-Path -LiteralPath $hooks)) {
  New-Item -ItemType Directory -Force -Path $codex | Out-Null
  '{"description":"Codex hooks","hooks":{}}' | Set-Content -LiteralPath $hooks -Encoding utf8
}
Copy-Item -LiteralPath $hooks -Destination "$hooks.bak-$stamp" -Force
Write-Host "已备份: hooks.json.bak-$stamp" -ForegroundColor Green

$conf = Get-Content -LiteralPath $hooks -Raw | ConvertFrom-Json
if (-not $conf.hooks) { $conf | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{}) -Force }

function Get-ExistingEntries($conf, [string]$evt) {
  # 注意: @($null) 在 PowerShell 里是"含一个 $null 的数组"，会导致写入 <NULL> 条目
  if ($null -eq $conf.hooks.$evt) { return @() }
  $raw = @($conf.hooks.$evt)
  return @($raw | Where-Object { $null -ne $_ })
}

function Test-AlreadyInstalled($conf, [string]$evt, [string]$mode) {
  # 幂等判定按 "run_hook.cmd <mode>" 识别，同时兼容早期直接调用 hook_*.ps1 的接线。
  if (-not $conf.hooks.$evt) { return $false }
  $needle = 'run_hook.cmd ' + $mode
  foreach ($entry in @($conf.hooks.$evt)) {
    foreach ($h in @($entry.hooks)) {
      $c = [string]$h.command
      if (-not $c) { continue }
      if ($c -match [regex]::Escape($needle)) { return $true }
      if ($mode -eq 'supervisor' -and $c -match 'hook_supervisor\.ps1') { return $true }
      if ($mode -eq 'post' -and $c -match 'hook_post\.ps1') { return $true }
      if ($mode -eq 'session' -and $c -match 'hook_session\.ps1') { return $true }
    }
  }
  return $false
}

# PreToolUse
if (-not (Test-Path -LiteralPath $hooks)) { }
if (Test-AlreadyInstalled $conf 'PreToolUse' 'hook_supervisor.ps1') {
  Write-Host "PreToolUse 闸门已存在，跳过" -ForegroundColor Green
} else {
  $existing = Get-ExistingEntries $conf 'PreToolUse'
  $conf.hooks | Add-Member -NotePropertyName PreToolUse -NotePropertyValue (@($existing) + @(New-HookEntry 'supervisor')) -Force
  Write-Host "已接入 PreToolUse 闸门" -ForegroundColor Green
}

# PostToolUse（① 工具报错采集）
if (Test-AlreadyInstalled $conf 'PostToolUse' 'hook_post.ps1') {
  Write-Host "PostToolUse 采集已存在，跳过" -ForegroundColor Green
} else {
  $existing = Get-ExistingEntries $conf 'PostToolUse'
  $conf.hooks | Add-Member -NotePropertyName PostToolUse -NotePropertyValue (@($existing) + @(New-HookEntry 'post')) -Force
  Write-Host "已接入 PostToolUse 采集" -ForegroundColor Green
}

# 会话级事件
foreach ($evt in $sessionEvents) {
  if (Test-AlreadyInstalled $conf $evt 'hook_session.ps1') {
    Write-Host "$evt 已存在，跳过" -ForegroundColor Green
  } else {
    $existing = Get-ExistingEntries $conf $evt
    $conf.hooks | Add-Member -NotePropertyName $evt -NotePropertyValue (@($existing) + @(New-SessionHookEntry 'session')) -Force
    Write-Host "已接入 $evt" -ForegroundColor Green
  }
}

$conf | ConvertTo-Json -Depth 14 | Set-Content -LiteralPath $hooks -Encoding utf8

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Cyan
$statusCmd = 'python "{0}\supervisor.py" status' -f $dest
$resumeCmd = 'python "{0}\supervisor.py" resume --by 你的名字 --reason 理由' -f $dest
Write-Host "查看状态: $statusCmd"
Write-Host "解除阻断: $resumeCmd"
Write-Host ""
Write-Host "注意: hooks 需要重启 Codex 才会生效，首次可能需要在界面上信任 hook。" -ForegroundColor Yellow
