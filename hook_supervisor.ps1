<#
  Codex PreToolUse hook: anti-degradation supervisor gate.

  这是整套设计里唯一真正"外部"的一层：
    - 由 Codex 宿主执行，生产者(agent)无法跳过、无法改写调用时机
    - 决策权在 supervisor，本脚本只是传输层

  行为：
    BLOCKED   -> permissionDecision = deny，工具调用被真正拦下
    DEGRADED  -> 放行，但把介入指令注入 permissionDecisionReason
    其余      -> {} 完全透明，不干扰
    只读白名单 -> 即使在 BLOCKED 下也放行（status / rules verify），避免把人锁死
    监督层不可达 -> 放行 + 记录（不能让一个 bug 砖掉整个 Codex）

  始终只输出一个 JSON 对象并 exit 0。
#>
$ErrorActionPreference = 'Continue'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'

$hookDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfgPath = Join-Path $hookDir 'hook_config.json'
$logDir  = Join-Path $hookDir 'logs'
$log     = Join-Path $logDir 'hook.log'

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

# ── 诊断探针：无条件记录，确认脚本是否被真正执行 ──
try {
  $probeLog = Join-Path $env:TEMP 'ad-hook-probe.log'
  $probeMsg = ("{0} ENTER pid={1} script={2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"), $PID, $MyInvocation.MyCommand.Path)
  Add-Content -LiteralPath $probeLog -Value $probeMsg -Encoding utf8 -ErrorAction SilentlyContinue
} catch { }


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

# 只读命令白名单：BLOCKED 时依然放行，否则人无法查看状态
function Test-ReadOnlyCommand([string]$cmd) {
  if (-not $cmd) { return $false }
  # 去掉引号再匹配，避免 supervisor.py 与 "supervisor.py" 两种写法漏判
  $c = ($cmd -replace '"', '' -replace "'", '').Trim()
  # 2026-10-06 修洞：原来是对"整串命令"做 -match 子串匹配，于是
  #   rm -rf / ; python supervisor.py status
  # 这类拼接命令会因含白名单子串而被整体放行 —— 生产者可借此绕开 BLOCKED 硬阻断。
  # 现在要求整条命令"就是"一条只读调用（最多允许一个前置 cd）。
  $ro   = '(?i)^(&\s+)?(python(\.exe)?|py)\s+\S*supervisor\.py\s+(status|rules\s+verify|debt\s+list|maintenance\s+status)\s*$'
  if ($c -match $ro) { return $true }
  $cdro = '(?i)^(cd|chdir|set-location|sl)\s+[^;&|<>]+\s*;\s*(&\s+)?(python(\.exe)?|py)\s+\S*supervisor\.py\s+(status|rules\s+verify|debt\s+list|maintenance\s+status)\s*$'
  return ($c -match $cdro)
}

# 先剥掉 here-string / heredoc 正文。内联脚本文本里的关键词不是"真实命令"：
# 否则 `$x = @'...Set-Content...'@` 会被误判成写盘，并把它引用的文件名当成写目标
# （2026-10-06 复查发现：harness 里的关键词反复给 hook_supervisor.ps1 记账）。
# 按 PS 规则匹配：开引号必须紧跟换行，闭引号必须**行首** —— 否则正文里出现的
# @...@ 片段会让非贪婪匹配提前收尾，把正文尾部当成命令行（同一 bug 的第二层）。
function Get-CommandLine([string]$cmd) {
  if (-not $cmd) { return '' }
  $c = $cmd
  $c = [regex]::Replace($c, "(?s)@'[ \t]*\r?\n[\s\S]*?\r?\n'@", ' ')
  $c = [regex]::Replace($c, '(?s)@"[ \t]*\r?\n[\s\S]*?\r?\n"@', ' ')
  $c = [regex]::Replace($c, "(?s)<<-?\s*['""]?(\w+)['""]?\s*\r?\n[\s\S]*?\r?\n\1", ' ')
  return $c
}

# "会改盘"的命令判定：只给真正写入/修改文件的命令记 rework，避免把只读调查算成返工。
# 原则：宁可漏判（少算一次返工），也不误判（把正常调查顶成 DEGRADED/BLOCKED）。
function Test-MutatingCommand([string]$cmd) {
  if (-not $cmd) { return $false }
  $c = Get-CommandLine $cmd
  $pats = @(
    '(?i)(^|[\s;&|(])(set-content|add-content|clear-content|out-file|tee-object|new-item|remove-item|move-item|copy-item|rename-item|set-itemproperty|new-itemproperty|remove-itemproperty|export-csv|export-clixml)(\s|$)',
    '(?i)(^|[\s;&|(])(rm|del|erase|rmdir|mkdir|mv|move|cp|copy|ren|rename|touch|truncate|dd|tee|chmod|chown|ln|install)(\s|$)',
    '(?i)sed\s+-\w*i',
    '(?i)perl\s+-\w*i',
    '(?<![\-\w=])>{1,2}\s*(?:"[^"]*[\\/\.][^"]*"|''[^'']*[\\/\.][^'']*''|[^\s"''|;&<>()]*[\\/][^\s"''|;&<>()]*|[^\s"''|;&<>()]+\.[A-Za-z0-9]+)',
    '(?i)(^|[\s;&|(])(apply_patch|patch)(\s|$)',
    '(?i)git\s+(apply|am|checkout|restore|reset|merge|rebase|cherry-pick|clean|mv|rm)\b',
    '(?i)((write_text|write_bytes|writealltext|writeallbytes|appendalltext)\s*\(|(?<!stdout)(?<!stderr)(?<!stdin)\.write\(|open\s*\([^)]*[''"]\s*[wa])'
  )
  foreach ($p in $pats) { if ($c -match $p) { return $true } }
  return $false
}

# 从命令里取"写目标"路径。优先级：写重定向 > 显式路径参数 > 写 cmdlet 位置参数 > 兜底。
# 只分析命令行本身（here-string 正文已剥掉）；兜底跳过 cd/set-location 的目标，
# 否则 "cd <dir>; ... > log" 会把 <dir> 当成写目标，反复查看目录就虚增返工。
# 只读"调查"命令判定（2026-10-06 第十四轮修）：让"反复跑同一条只读命令"不再计入
# repeat_command。刻意保守 —— 必须同时满足：
#   (1) Test-MutatingCommand 为假（命令里没有任何写盘关键词）
#   (2) 第一条子命令命中"只读白名单"（绝不写盘的 cmdlet/二进制）
# 宁可漏豁免（仍照常计分），也不误豁免（少算一次应有的扣分）。
function Test-ReadOnlyInvestigation([string]$cmd) {
  if (-not $cmd) { return $false }
  if (Test-MutatingCommand $cmd) { return $false }
  $c = Get-CommandLine $cmd
  $c = ($c -replace '"', '' -replace "'", '').Trim()
  if (-not $c) { return $false }
  # 允许一个前置 cd/set-location，再取第一条子命令（分号/&&/|| 之前）
  $c = [regex]::Replace($c, '(?i)^\s*(cd|chdir|set-location|sl)\s+[^;&|<>]+\s*;\s*', '')
  $first = ($c -split '[;&|]')[0].Trim()
  if (-not $first) { return $false }
  $ro = '(?i)^(&\s+)?(get-content|gc|get-childitem|gci|get-item|gi|get-itemproperty|gp|select-string|sls|test-path|resolve-path|get-command|gcm|get-help|get-date|get-location|gl|pwd|measure-object|compare-object|select-object|where-object|sort-object|group-object|format-table|ft|format-list|fl|out-string|convertfrom-json|convertfrom-csv|get-filehash|cat|ls|dir|type|head|tail|wc|which|where|rg|grep)\b'
  if ($first -match $ro) { return $true }
  if ($first -match '(?i)^(&\s+)?(python(\.exe)?|py)\s+\S*supervisor\.py\s+(status|rules\s+verify|debt\s+list|maintenance\s+status)\b') { return $true }
  if ($first -match '(?i)^(&\s+)?git\s+(status|log|diff|show|rev-parse|describe|blame|ls-files)\b') { return $true }
  return $false
}

function Get-TargetPath([string]$cmd) {
  if (-not $cmd) { return '' }
  $c = Get-CommandLine $cmd
  $val = '(?:"([^"]+)"|''([^'']+)''|([^\s"''|;&<>()]+))'
  $m = [regex]::Match($c, ('(?<![\-\w=])>{1,2}\s*' + $val))
  if ($m.Success) { foreach ($g in 1..3) { if ($m.Groups[$g].Success -and ($m.Groups[$g].Value -match '[\\/.]')) { return $m.Groups[$g].Value } } }
  $m = [regex]::Match($c, ('(?i)-(?:filepath|path|literalpath|destination|outfile)\s*' + $val))
  if ($m.Success) { foreach ($g in 1..3) { if ($m.Groups[$g].Success -and $m.Groups[$g].Value) { return $m.Groups[$g].Value } } }
  $m = [regex]::Match($c, ('(?i)(?:set-content|add-content|clear-content|out-file|new-item|remove-item|move-item|copy-item|rename-item|tee-object|export-csv|export-clixml)\s+(?!-)' + $val))
  if ($m.Success) { foreach ($g in 1..3) { if ($m.Groups[$g].Success -and $m.Groups[$g].Value) { return $m.Groups[$g].Value } } }
  $rx = '(?i)(?:[A-Za-z]:\\[^\s"''|;>]+|\\\\[^\s"''|;>]+|(?<![\w])/(?:[\w.\-]+/)+[\w.\-]+|[\w\-.]+\.(?:py|ps1|psm1|js|ts|tsx|json|md|txt|html|css|cs|go|rs|java|yml|yaml|toml|cfg|ini|csv|log))'
  foreach ($mm in [regex]::Matches($c, $rx)) {
    $before = $c.Substring(0, $mm.Index)
    if ($before -match '(?i)(^|[\s;&|(])(cd|chdir|set-location|sl)\s+[^\s;&|]*$') { continue }
    return ($mm.Value -replace '[\s,;)\]]+$', '')
  }
  return ''
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
  Write-ParseFailure $raw 'supervisor'
  Emit '{}'; exit 0
}

try {
  $tool = [string]$ev.tool_name
  $ti   = $ev.tool_input

  $supEvent = [ordered]@{ kind = 'tool_call'; tool = $tool; session_id = [string]$ev.session_id }
  $isReadOnly = $false

  if ($tool -eq 'view_image') {
    $supEvent.kind = 'image'
  } elseif ($tool -in @('exec_command','Bash','bash','shell','sh')) {
    $cmd = ''
    if ($ti -and $ti.command) { $cmd = [string]$ti.command }
    $supEvent.kind = 'command'
    $supEvent.cmd = $cmd
    $isReadOnly = Test-ReadOnlyCommand $cmd
    if (Test-ReadOnlyInvestigation $cmd) { $supEvent.readonly = $true }
    # rework signal（2026-10-06 修）：只有"会改盘"的命令才附 path，表示对某路径的一次写入/修改。
    # 旧代码对任何命令都取首个疑似路径，于是反复 Get-Content/Get-ChildItem/Select-String
    # 同一文件（正常调查）也被计成返工，本会话被误判两次 DEGRADED、一次 BLOCKED。
    if (Test-MutatingCommand $cmd) {
      try {
        $tgt = Get-TargetPath $cmd
        if ($tgt) { $supEvent.path = $tgt; $supEvent.rework_kind = 'edit' }
      } catch { }
    }
  }

  if (-not (Test-Path -LiteralPath $supScript)) {
    Write-HookLog "supervisor not found at $supScript -> passthrough"
    Emit '{}'; exit 0
  }

  $eventJson = $supEvent | ConvertTo-Json -Compress -Depth 6
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
  # 事件走 stdin，避免命令行引号转义的脆弱性
  Write-Utf8Stdin $proc $eventJson
  $stdout = $proc.StandardOutput.ReadToEnd()
  $stderr = $proc.StandardError.ReadToEnd()
  $proc.WaitForExit(20000) | Out-Null

  if ([string]::IsNullOrWhiteSpace($stdout)) {
    Write-HookLog "supervisor produced no output; stderr=$stderr -> passthrough"
    Emit '{}'; exit 0
  }
  $dec = $stdout | ConvertFrom-Json
} catch {
  Write-HookLog "gate error: $($_.Exception.Message) -> passthrough"
  Emit '{}'; exit 0
}

try {
  $decision = [string]$dec.decision
  $level = [string]$dec.level
  $directive = [string]$dec.directive

  if ($isReadOnly) {
    Emit '{}'; exit 0
  }

  if ($decision -eq 'deny') {
    $reason = "SUPERVISOR BLOCKED (level=$level, score=$($dec.score))。"
    if ($directive) { $reason += "`n" + $directive }
    $reason += "`n需要人在 Codex 之外执行: python supervisor.py resume --by 人 --reason 理由"
    Write-HookLog "DENY tool=$tool level=$level score=$($dec.score)"
    $outObj = [ordered]@{
      hookSpecificOutput = [ordered]@{
        hookEventName = 'PreToolUse'
        permissionDecision = 'deny'
        permissionDecisionReason = $reason
      }
    }
    Emit ($outObj | ConvertTo-Json -Depth 6 -Compress)
    exit 0
  }

  if ($level -eq 'DEGRADED' -and $directive) {
    Write-HookLog "WARN tool=$tool score=$($dec.score)"
    $outObj = [ordered]@{
      hookSpecificOutput = [ordered]@{
        hookEventName = 'PreToolUse'
        permissionDecision = 'allow'
        permissionDecisionReason = $directive
      }
    }
    Emit ($outObj | ConvertTo-Json -Depth 6 -Compress)
    exit 0
  }

  Emit '{}'
  exit 0
} catch {
  Write-HookLog "emit error: $($_.Exception.Message) -> passthrough"
  Emit '{}'
  exit 0
}
