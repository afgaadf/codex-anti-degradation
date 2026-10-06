Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$py = 'C:\Users\taich\AppData\Local\Programs\Python\Python312\python.exe'
$sup = 'C:\Users\taich\.codex\anti-degradation\supervisor.py'

$form = New-Object System.Windows.Forms.Form
$form.Text = '管家维护模式'
$form.Size = New-Object System.Drawing.Size(620, 430)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)

$title = New-Object System.Windows.Forms.Label
$title.Text = '管家维护模式'
$title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 16, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(24, 20)
$form.Controls.Add($title)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = '开发/维护用的外部限时开关：不常驻，到期自动恢复正常阻断。'
$hint.AutoSize = $true
$hint.Location = New-Object System.Drawing.Point(26, 62)
$form.Controls.Add($hint)

$reasonLabel = New-Object System.Windows.Forms.Label
$reasonLabel.Text = '维护原因：'
$reasonLabel.AutoSize = $true
$reasonLabel.Location = New-Object System.Drawing.Point(26, 100)
$form.Controls.Add($reasonLabel)

$reason = New-Object System.Windows.Forms.TextBox
$reason.Text = '外部维护'
$reason.Location = New-Object System.Drawing.Point(110, 96)
$reason.Size = New-Object System.Drawing.Size(460, 30)
$form.Controls.Add($reason)

$status = New-Object System.Windows.Forms.Label
$status.Text = '正在读取维护状态…'
$status.BorderStyle = 'FixedSingle'
$status.Location = New-Object System.Drawing.Point(26, 145)
$status.Size = New-Object System.Drawing.Size(544, 150)
$status.Padding = New-Object System.Windows.Forms.Padding(12)
$form.Controls.Add($status)

$on = New-Object System.Windows.Forms.Button
$on.Text = '进入维护模式（60 分钟）'
$on.Location = New-Object System.Drawing.Point(26, 315)
$on.Size = New-Object System.Drawing.Size(210, 42)
$form.Controls.Add($on)

$off = New-Object System.Windows.Forms.Button
$off.Text = '退出维护模式'
$off.Location = New-Object System.Drawing.Point(248, 315)
$off.Size = New-Object System.Drawing.Size(160, 42)
$form.Controls.Add($off)

$refresh = New-Object System.Windows.Forms.Button
$refresh.Text = '刷新状态'
$refresh.Location = New-Object System.Drawing.Point(420, 315)
$refresh.Size = New-Object System.Drawing.Size(150, 42)
$form.Controls.Add($refresh)

function Invoke-Maintenance([string[]]$Arguments) {
    $raw = & $py $sup @Arguments 2>&1 | Out-String
    try { return ($raw | ConvertFrom-Json) } catch { return [pscustomobject]@{ ok = $false; error = ('程序返回异常：' + $raw) } }
}

function Refresh-Status {
    $r = Invoke-Maintenance @('maintenance','status','--json')
    if ($r -and $r.ok -and $r.maintenance -and $r.maintenance.active) {
        $m = $r.maintenance
        $status.Text = "维护模式：已开启`r`n开启人：$($m.by)`r`n原因：$($m.reason)`r`n到期时间：$($m.until)`r`n剩余时间：约 $([Math]::Round($m.remaining_min,0)) 分钟"
    } else {
        $status.Text = '维护模式：未开启'
    }
}

$on.Add_Click({
    $why = $reason.Text.Trim()
    if (-not $why) { $why = '外部维护' }
    $r = Invoke-Maintenance @('maintenance','on','--by',$env:USERNAME,'--reason',$why,'--minutes','60','--json')
    if ($r -and $r.ok) {
        [void][System.Windows.Forms.MessageBox]::Show('维护模式已开启。', '管家维护模式', 'OK', 'Information')
    } else {
        [void][System.Windows.Forms.MessageBox]::Show(('开启失败：' + ($(if ($r.error) { $r.error } else { '未知原因' }))), '管家维护模式', 'OK', 'Error')
    }
    Refresh-Status
})

$off.Add_Click({
    $r = Invoke-Maintenance @('maintenance','off','--by',$env:USERNAME,'--reason','外部面板退出','--json')
    if ($r -and $r.ok) {
        [void][System.Windows.Forms.MessageBox]::Show('维护模式已关闭。', '管家维护模式', 'OK', 'Information')
    } else {
        [void][System.Windows.Forms.MessageBox]::Show(('关闭失败：' + ($(if ($r.error) { $r.error } else { '未知原因' }))), '管家维护模式', 'OK', 'Error')
    }
    Refresh-Status
})

$refresh.Add_Click({ Refresh-Status })
$form.Add_Shown({ Refresh-Status })
[void]$form.ShowDialog()