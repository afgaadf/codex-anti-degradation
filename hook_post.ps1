<#
  Codex PostToolUse hook: tool error collection (upgrade 1 - wider detection).

  PreToolUse runs BEFORE a tool executes and cannot see the result, so "tool error
  rate" was previously an empty signal. This hook runs on PostToolUse, whose input
  carries tool_response (required by the local codex-cli post-tool-use.command.input
  schema), and decides whether the call actually failed.

  Behaviour: always emits {} - report only, never interferes. Gating stays in the
  PreToolUse hook. Any exception is swallowed so collection can never brick a turn.
#>
$ErrorActionPreference = 'Continue'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'

if ($PSScriptRoot) { $hookDir = $PSScriptRoot } else { $hookDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$cfgPath = Join-Path $hookDir 'hook_config.json'
$logDir  = Join-Path $hookDir 'logs'
$log     = Join-Path $logDir 'post.log'

$supHome = Join-Path $env:USERPROFILE '.codex\anti-degradation'
$pyExe = 'python'

if (Test-Path -LiteralPath $cfgPath) {
  try {
    $cfg = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
    if ($cfg.supervisor_home) { $supHome = [string]$cfg.supervisor_home }
    if ($cfg.python) { $pyExe = [string]$cfg.python }
  } catch { }
}
$supScript = Join-Path $supHome 'supervisor.py'

function Write-HookLog([string]$msg) {
  try {
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 256000) { Remove-Item $log -Force -ErrorAction SilentlyContinue }
    Add-Content -LiteralPath $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -ErrorAction SilentlyContinue
  } catch { }
}

# 必须按 UTF-8 写原始字节：控制台输出码页是 cp936 时（Windows PowerShell 5.1 + chcp 936），
# [Console]::Out.Write 会把中文编成 GBK，宿主按 UTF-8 读 => 乱码。
# 实测：DEGRADED/BLOCKED 的注入文本进入模型时变成 "���Ƿ��յȼ�"。
# 2026-10-06 修，与 README 六·五 的 stdin 根因同源（view_image_guard 已先修）。
function Emit([string]$json) {
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
  $out = [System.Console]::OpenStandardOutput()
  $out.Write($bytes, 0, $bytes.Length)
  $out.Flush()
}

# 显式按 UTF-8 写子进程 stdin。
# 默认 StreamWriter 用控制台代码页(cp936)写出，中文会被按 GBK 写，
# supervisor 按 UTF-8 读时即乱码（observe_parse_error）。
# 直接写 BaseStream 字节：PS5.1 没有 StandardInputEncoding 属性，这条路径两个宿主都一致。
function Write-Utf8Stdin($proc, [string]$text) {
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
  $bs = $proc.StandardInput.BaseStream
  $bs.Write($bytes, 0, $bytes.Length)
  $bs.Flush()
  $proc.StandardInput.Close()
}

# ── 读 stdin：必须按 UTF-8 解原始字节，不能用 [Console]::In ──
# 原因：本机 [Console]::InputEncoding = gb2312(cp936)。宿主写进来的是 UTF-8，
# 用 [Console]::In.ReadToEnd() 会按 GBK 错解中文，并在多字节边界吃掉/插入
# ASCII 结构字符（" \ 等），把合法 JSON 顶成非法 JSON（见 logs/stdin-fail-*）。
$script:HookStdinBytes = $null
$raw = ''
try {
  $stdinStream = [System.Console]::OpenStandardInput()
  $stdinBuf = New-Object System.IO.MemoryStream
  $stdinStream.CopyTo($stdinBuf)
  $script:HookStdinBytes = $stdinBuf.ToArray()
  $utf8 = New-Object System.Text.UTF8Encoding($false)
  $raw = $utf8.GetString($script:HookStdinBytes)
  if ($raw.Length -gt 0 -and $raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
} catch { $raw = '' }
if ([string]::IsNullOrWhiteSpace($raw)) { Emit '{}'; exit 0 }

function Write-ParseFailure([string]$rawText, [string]$source) {
  # 解析失败 = 监督层没看见这条事件。落盘原始片段 + 上报盲区计数，杜绝静默失败。
  try {
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $dump = Join-Path $logDir ("stdin-fail-{0}-{1}.txt" -f $source, $stamp)
    # 落盘“原始字节”而非二次解码后的字符串：取证必须所见即宿主所写。
    $bytes = $script:HookStdinBytes
    if ($null -eq $bytes) {
      if ($null -ne $rawText) { $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$rawText) }
      else { $bytes = [byte[]]@() }
    }
    $cap = 4096
    if ($bytes.Length -gt $cap) {
      $marker = [System.Text.Encoding]::UTF8.GetBytes("`n...[truncated; total $($bytes.Length) bytes]")
      $out = New-Object byte[] ($cap + $marker.Length)
      [System.Array]::Copy($bytes, 0, $out, 0, $cap)
      [System.Array]::Copy($marker, 0, $out, $cap, $marker.Length)
      $bytes = $out
    }
    [System.IO.File]::WriteAllBytes($dump, $bytes)
    Write-HookLog ("input parse failed; raw head -> " + (Split-Path -Leaf $dump))
    if (Test-Path -LiteralPath $supScript) {
      $sev = [ordered]@{ kind = 'parse_failure'; source = $source; hint = 'hook stdin is not valid JSON (likely truncated by host)' } | ConvertTo-Json -Compress -Depth 4
      $psi = New-Object System.Diagnostics.ProcessStartInfo
      $psi.FileName = $pyExe
      $psi.Arguments = '"{0}" observe' -f $supScript
      $psi.RedirectStandardInput = $true
      $psi.RedirectStandardOutput = $true
      $psi.RedirectStandardError = $true
      $psi.UseShellExecute = $false
      $psi.CreateNoWindow = $true
      $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
      $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
      $proc = [System.Diagnostics.Process]::Start($psi)
      Write-Utf8Stdin $proc $sev
      $null = $proc.StandardOutput.ReadToEnd()
      $null = $proc.StandardError.ReadToEnd()
      $proc.WaitForExit(20000) | Out-Null
    }
  } catch { }
}

try { $ev = $raw | ConvertFrom-Json } catch { Write-ParseFailure $raw 'post'; Emit '{}'; exit 0 }

$tool = [string]$ev.tool_name
$resp = $ev.tool_response

$failed = $false
$why = ''
try {
  if ($null -ne $resp) {
    $names = @($resp.PSObject.Properties.Name)
    if (($names -contains 'is_error') -and ($resp.is_error -eq $true)) { $failed = $true; $why = 'is_error' }
    if ((-not $failed) -and ($names -contains 'success') -and ($resp.success -eq $false)) { $failed = $true; $why = 'success=false' }
    foreach ($k in @('exit_code','exitCode')) {
      if ((-not $failed) -and ($names -contains $k)) {
        $v = $resp.$k
        if ($null -ne $v) { if ([int]$v -ne 0) { $failed = $true; $why = ($k + '=' + $v) } }
      }
    }
    if ((-not $failed) -and ($names -contains 'error') -and ($null -ne $resp.error)) {
      if (("$($resp.error)").Trim() -ne '') { $failed = $true; $why = 'error field' }
    }
    if ((-not $failed) -and ($names -contains 'status')) {
      $st = ("$($resp.status)").ToLower()
      if (($st -eq 'error') -or ($st -eq 'failed') -or ($st -eq 'failure')) { $failed = $true; $why = ('status=' + $st) }
    }
  }
} catch { Write-HookLog "response inspect failed: $($_.Exception.Message)"; $failed = $false }

function Send-SupervisorEvent($obj) {
  if (-not (Test-Path -LiteralPath $supScript)) { Write-HookLog 'supervisor not found -> passthrough'; return $false }
  try {
    $json = $obj | ConvertTo-Json -Compress -Depth 6
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $pyExe
    $psi.Arguments = '"{0}" observe' -f $supScript
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $proc = [System.Diagnostics.Process]::Start($psi)
    Write-Utf8Stdin $proc $json
    $null = $proc.StandardOutput.ReadToEnd()
    $null = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit(20000) | Out-Null
    return $true
  } catch {
    Write-HookLog "observe failed: $($_.Exception.Message)"
    return $false
  }
}

if (-not $failed) {
  # 形状可见化（2026-10-06 线上查证）：官方测试与本机实测都表明，宿主把 tool_response
  # 作为"工具文本输出"的**字符串**发来，载荷里没有任何 exit_code/is_error/status 字段。
  # 也就是说：**无法判定成败**。这是"信号不可观测"，不是"没有错误"。
  # 按"宁可不报、也不假报"的原则不去猜文本；但必须让它可见 —— 否则 tool_errors=0 会被
  # 误读成"零错误"。每个 (session, 形状) 只上报一次，避免刷屏。
  $shape = if ($null -eq $resp) { 'null' } elseif ($resp -is [string]) { 'string' } else { 'object' }
  if ($shape -ne 'object') {
    try {
      $marker = Join-Path $logDir 'post_shape_seen.txt'
      $key = ([string]$ev.session_id) + '|' + $shape
      $prev = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { '' }
      if ($prev -ne $key) {
        Set-Content -LiteralPath $marker -Value $key -Encoding utf8
        Write-HookLog ("tool_response 形状不可判定: tool=" + $tool + " shape=" + $shape)
        [void](Send-SupervisorEvent ([ordered]@{ kind = 'tool_response_shape'; shape = $shape;
                   judgeable = $false; tool = $tool; session_id = [string]$ev.session_id }))
      }
    } catch { }
  }
  Emit '{}'
  exit 0
}

[void](Send-SupervisorEvent ([ordered]@{ kind = 'tool_error'; tool = $tool; debt_registered = $false;
         why = $why; session_id = [string]$ev.session_id }))
Write-HookLog ("TOOL_ERROR tool=" + $tool + " why=" + $why)
Emit '{}'
exit 0
