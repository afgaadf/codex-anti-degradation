<#
  Codex hook: 会话级降智信号采集 + 介入注入
  覆盖事件: UserPromptSubmit / PreCompact / PostCompact / SessionStart / SessionEnd / Stop

  这是补上的关键一环：
    - PreToolUse 看不到会话总长度，本 hook 通过 transcript_path 拿到
    - UserPromptSubmit 可以用 additionalContext 把警告注入回模型
    - PreCompact 是"上下文被填满"的硬证据，直接作为强降智信号

  设计原则同 PreToolUse 闸门：绝不阻断用户的正常提交流程，任何异常都放行 + 记日志。
#>
$ErrorActionPreference = 'Continue'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'

$hookDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfgPath = Join-Path $hookDir 'hook_config.json'
$logDir  = Join-Path $hookDir 'logs'
$log     = Join-Path $logDir 'session.log'

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


try {
  if ([string]::IsNullOrWhiteSpace($raw)) { Emit '{}'; exit 0 }
  $ev = $raw | ConvertFrom-Json
} catch {
  Write-ParseFailure $raw 'session'
  Emit '{}'; exit 0
}

$eventName = [string]$ev.hook_event_name
if ([string]::IsNullOrWhiteSpace($eventName)) { $eventName = 'UserPromptSubmit' }

# 估算上下文体积：transcript 文件大小是最直接的代理指标
$ctxChars = 0
$tp = [string]$ev.transcript_path
if ($tp -and (Test-Path -LiteralPath $tp)) {
  try { $ctxChars = (Get-Item -LiteralPath $tp).Length } catch { $ctxChars = 0 }
}

# Stop（2026-10-06 接线）：宿主侧"本轮结束"证据。只取元数据，不转发整条末消息（可能很大）。
$stopActive = $false; $hasMsg = $false; $msgLen = 0
if ($eventName -eq 'Stop') {
  $stopActive = [bool]$ev.stop_hook_active
  if ($null -ne $ev.last_assistant_message) { $hasMsg = $true; $msgLen = ([string]$ev.last_assistant_message).Length }
}

# 组装给 supervisor 的事件
$supEvents = @()
switch ($eventName) {
  'UserPromptSubmit' {
    $supEvents += [ordered]@{ kind = 'turn' }
    if ($ctxChars -gt 0) { $supEvents += [ordered]@{ kind = 'context'; chars = $ctxChars } }
  }
  'PreCompact'  { $supEvents += [ordered]@{ kind = 'compact'; phase = 'pre' } }
  'PostCompact' { $supEvents += [ordered]@{ kind = 'compact'; phase = 'post' } }
  # source 是官方 input schema 的 required 字段（startup/resume/clear/compact/fork），
  # 原来被丢弃；透传后可区分“新开线程 / 恢复 / 压缩后 / 清空”。2026-10-06 补。
  'SessionStart'{ $supEvents += [ordered]@{ kind = 'session_start'; source = [string]$ev.source } }
  'SessionEnd'  { $supEvents += [ordered]@{ kind = 'session_end' } }
  'SubagentStart'{ $supEvents += [ordered]@{ kind = 'subagent_start'; agent_id = [string]$ev.agent_id; agent_type = [string]$ev.agent_type } }
  'SubagentStop' { $supEvents += [ordered]@{ kind = 'subagent_stop';  agent_id = [string]$ev.agent_id; agent_type = [string]$ev.agent_type } }
  'Stop'        { $supEvents += [ordered]@{ kind = 'turn_end'; stop_hook_active = $stopActive; has_message = $hasMsg; msg_len = $msgLen } }
  default       { $supEvents += [ordered]@{ kind = 'turn' } }
}

$cx = [string]$ev.session_id
if ($cx) { foreach ($se in $supEvents) { if (-not $se.Contains('session_id')) { $se['session_id'] = $cx } } }

if (-not (Test-Path -LiteralPath $supScript)) {
  Write-HookLog "supervisor not found at $supScript -> passthrough"
  Emit '{}'; exit 0
}

$last = $null
foreach ($se in $supEvents) {
  try {
    $json = $se | ConvertTo-Json -Compress -Depth 6
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
    $stdout = $proc.StandardOutput.ReadToEnd()
    $proc.StandardError.ReadToEnd() | Out-Null
    $proc.WaitForExit(20000) | Out-Null
    if (-not [string]::IsNullOrWhiteSpace($stdout)) {
      $last = $stdout | ConvertFrom-Json
    }
  } catch {
    Write-HookLog "observe failed for $($se.kind): $($_.Exception.Message)"
  }
}

if (-not $last) { Emit '{}'; exit 0 }

try {
  $level = [string]$last.level
  $decision = [string]$last.decision
  $maint = $last.maintenance

  # 只在 UserPromptSubmit 上注入，其它事件保持透明
  $msg = ''
  if ($eventName -eq 'UserPromptSubmit') {
    if ($maint -and $maint.active) {
      $msg = "[SUPERVISOR 维护模式] 开发/维护临时放行至 $($maint.until)；由 $($maint.by) 启动。"
      if ($maint.reason) { $msg += "原因：$($maint.reason)" }
      if ($level -eq 'BLOCKED' -or $level -eq 'DEGRADED') {
        $msg += "`n底层评分仍为 $level (score=$($last.score))，但外部维护模式当前放行。"
      }
    } elseif ($level -eq 'DEGRADED' -or $level -eq 'BLOCKED') {
      $msg = "[SUPERVISOR] 降智风险等级=$level (score=$($last.score))。"
      if ($last.directive) { $msg += "`n" + [string]$last.directive }
      if ($ctxChars -gt 0) { $msg += "`n当前 transcript 体积: $([Math]::Round($ctxChars/1KB,1)) KB" }
      if ($level -eq 'BLOCKED') {
        $msg += "`n工具调用已被阻断，需人在 Codex 之外执行 resume 才能恢复。"
      }
    }
    # 监督者（独立程序）留下的"责令改正"要求 —— 注入给 AI
    try {
      $corrPath = [Environment]::GetEnvironmentVariable('SUPERVISOR_CORRECTIONS_PATH')
      if (-not $corrPath) { $corrPath = Join-Path $env:USERPROFILE '.codex\supervisor\corrections.json' }
      if (Test-Path -LiteralPath $corrPath) {
        $cj = Get-Content -LiteralPath $corrPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cj.open -and @($cj.open).Count -gt 0) {
          $viol = @(); $stand = @(); $n = 0
          foreach ($o in @($cj.open)) {
            $n++
            if ($n -gt 12) { break }
            if ($o.standing) {
              $stand += ("· 【{0}】{1}" -f [string]$o.rule, [string]$o.fix)
            } else {
              $viol += ("{0}. 【{1}】{2}（依据：{3}）" -f $viol.Count + 1, [string]$o.rule, [string]$o.fix, [string]$o.evidence)
            }
          }
          $parts = @()
          if ($viol.Count -gt 0) {
            $parts += ("[监督者·责令改正] 你上一轮违反了以下规矩，请立即纠正并在回复里说明怎么改的：`n" + ($viol -join "`n"))
          }
          if ($stand.Count -gt 0) {
            $parts += ("[监督者·常驻规矩（每轮都生效）]`n" + ($stand -join "`n"))
          }
          $cm = ($parts -join "`n")
          if ($cm) { if ($msg) { $msg = $msg + "`n" + $cm } else { $msg = $cm } }
        }
      }
    } catch { }
    if ($msg) {
      Write-HookLog "INJECT level=$level score=$($last.score) ctxKB=$([Math]::Round($ctxChars/1KB,1))"
      $outObj = [ordered]@{
        hookSpecificOutput = [ordered]@{
          hookEventName = $eventName
          additionalContext = $msg
        }
      }
      Emit ($outObj | ConvertTo-Json -Depth 6 -Compress)
      exit 0
    }
  }

  Emit '{}'
  exit 0
} catch {
  Write-HookLog "emit error: $($_.Exception.Message)"
  Emit '{}'
  exit 0
}
