<#
.SYNOPSIS
    实盘 runner 的 Windows 看护(计划任务注册 / 启停 / 心跳体检)。

.DESCRIPTION
    2026-10-22 的教训:进程静默消失 ⇒ 交易所侧止损成交、本地仍记持仓,
    而"谁没了"没有任何人知道 —— 只能等下次重启靠缺口补做追平。看护的三件事:
      ① 进程不存在 ⇒ 立即拉起(任务重复触发 + IgnoreNew,崩溃后 ≤N 分钟复位);
      ② 进程在但心跳停摆(state.json 的 saved_at_utc 不再前进)⇒ 记日志并告警;
      ③ register 把上面两条固化成一个可复现的任务动作,不再靠人手敲命令。

.PARAMETER Action
    register | start | stop | status | guard(默认 status)

.PARAMETER IntervalMinutes
    任务重复触发间隔(默认 5)⇒ 该值就是"崩溃后最长无保护时间"。

.PARAMETER MaxBeatAgeMinutes
    心跳超期阈值(默认 20)。runner 每步(约 15s)都会落盘 state.json,
    2H 周期 + 136 品种的补做轮次实测 < 1 分钟,20 分钟足以区分"卡死"与"忙"。

.PARAMETER RestartOnStale
    guard 检出心跳超期时结束进程(下个触发自动重启)。默认关闭:宁可漏重启,
    不可在补做中途杀掉进程 —— 交易所有保护单兜底,本地事实可以重放。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\live_watchdog.ps1 -Action status
#>
[CmdletBinding()]
param(
    [ValidateSet('register', 'start', 'stop', 'status', 'guard',
                 'gc', 'register-gc')]
    [string]$Action = 'status',
    [int]$IntervalMinutes = 3,
    [int]$MaxBeatAgeMinutes = 20,
    [switch]$RestartOnStale
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$StateFile = Join-Path $Repo 'runtime\live\state.json'
$CmdFile = Join-Path $Repo 'tools\schedule\live_runner.cmd'
$TaskPath = '\MarketOperator\'
$TaskName = 'MarketOperatorLive'
$LogFile = Join-Path $Repo 'logs\watchdog.log'

function Write-Log([string]$Msg) {
    $line = '{0} {1}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Msg
    Write-Host $line
    $dir = Split-Path -Parent $LogFile
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Get-RunnerProc {
    # 只认 "python ... live_runner.py ... --mode live" 的进程:
    # 别把回测/预演/paper 当成实盘在跑(否则看护会误判"已在运行")
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match 'live_runner\.py' -and $_.CommandLine -match '--mode\s+live' }
}

function Get-BeatAgeMinutes {
    # 心跳 = state.json 的 saved_at_utc(UTC ISO,带 +00:00);读不到返回 $null
    if (-not (Test-Path $StateFile)) { return $null }
    try {
        $st = Get-Content $StateFile -Raw -Encoding utf8 | ConvertFrom-Json
        if (-not $st.saved_at_utc) { return $null }
        $beat = [datetimeoffset]::Parse($st.saved_at_utc)
        return [math]::Round(([datetimeoffset]::UtcNow - $beat).TotalMinutes, 2)
    } catch { return $null }
}

function Show-Status {
    $procs = @(Get-RunnerProc)
    if ($procs.Count -eq 0) {
        Write-Host '进程: 未运行'
    } else {
        foreach ($p in $procs) {
            Write-Host ("进程: PID {0}" -f $p.ProcessId)
            Write-Host ("       {0}" -f $p.CommandLine)
        }
    }
    $age = Get-BeatAgeMinutes
    if ($null -eq $age) {
        Write-Host '心跳: 读不到 state.json / saved_at_utc'
    } else {
        $flag = if ($age -gt $MaxBeatAgeMinutes) { '  <== 超期' } else { '' }
        Write-Host ("心跳: saved_at_utc 距今 {0} 分钟{1}" -f $age, $flag)
    }
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host '计划任务: 未注册(可 -Action register)'
    } else {
        $info = Get-ScheduledTaskInfo -TaskPath $TaskPath -TaskName $TaskName
        Write-Host ("计划任务: {0}{1} 状态 {2} 上次 {3} 结果 0x{4:X}" -f $TaskPath, $TaskName,
            $task.State, $info.LastRunTime, $info.LastTaskResult)
    }
}

function Start-Runner {
    $procs = @(Get-RunnerProc)
    if ($procs.Count -gt 0) {
        Write-Log ("已在运行(PID {0}),不重复启动" -f ($procs.ProcessId -join ','))
        return
    }
    $logs = Join-Path $Repo 'logs'
    if (-not (Test-Path $logs)) { New-Item -ItemType Directory -Path $logs -Force | Out-Null }
    $p = Start-Process -FilePath 'python' `
        -ArgumentList 'scripts\live_runner.py', '--mode', 'live' `
        -WorkingDirectory $Repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs 'live_stdout.log') `
        -RedirectStandardError (Join-Path $logs 'live_stderr.log')
    Write-Log ("已启动 PID {0}(日志 logs\live_stdout.log / live_stderr.log)" -f $p.Id)
}

function Stop-Runner {
    $procs = @(Get-RunnerProc)
    if ($procs.Count -eq 0) { Write-Log '未运行,无需停止'; return }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force
        Write-Log ("已结束 PID {0}" -f $p.ProcessId)
    }
}

function Register-RunnerTask {
    if (-not (Test-Path $CmdFile)) { throw "找不到任务动作 $CmdFile" }
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "{0}"' -f $CmdFile)
    # Once + 长时长重复 = 常驻 + 自愈:在跑时 IgnoreNew 跳过,不在跑时立即拉起
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal `
        -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive -RunLevel Limited
    $desc = 'market-operator 实盘 runner 常驻:每 {0} 分钟触发一次,已在跑则跳过' -f $IntervalMinutes
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $action `
        -Trigger $trigger -Settings $settings -Principal $principal `
        -Description $desc -Force | Out-Null
    Write-Log ("已注册计划任务 {0}{1}(每 {2} 分钟触发,动作 {3})" -f $TaskPath, $TaskName,
        $IntervalMinutes, $CmdFile)
}

function Invoke-Guard {
    $procs = @(Get-RunnerProc)
    $age = Get-BeatAgeMinutes
    $stale = ($null -ne $age -and $age -gt $MaxBeatAgeMinutes)
    if ($procs.Count -eq 0) {
        Write-Log 'guard:进程不存在 → 拉起'
        Start-Runner
        return
    }
    if ($stale) {
        Write-Log ('guard:进程在(PID {0})但心跳已 {1} 分钟未更新(阈值 {2})' -f
            ($procs.ProcessId -join ','), $age, $MaxBeatAgeMinutes)
        if ($RestartOnStale) {
            Stop-Runner
            Start-Runner
            Write-Log 'guard:已按 -RestartOnStale 重启'
        } else {
            Write-Log 'guard:未启用 -RestartOnStale,仅记录(交易所有保护单兜底,不冒杀进程的风险)'
        }
        return
    }
    Write-Log ('guard:正常(PID {0},心跳 {1} 分钟)' -f ($procs.ProcessId -join ','), $age)
}

function Invoke-Gc {
    # 每日存储治理:只清可再生产物(见 scripts\storage_gc.py 头部原则)
    $gc = Join-Path $Repo 'scripts\storage_gc.py'
    if (-not (Test-Path $gc)) { throw "找不到 $gc" }
    $log = Join-Path $Repo 'logs\storage_gc.log'
    $out = & python $gc --apply --dedupe-gz 2>&1
    $out | Add-Content -Path $log -Encoding utf8
    $out | Select-Object -Last 12 | ForEach-Object { Write-Host $_ }
    Write-Log 'gc:存储治理完成(详见 logs\storage_gc.log)'
}

function Register-GcTask {
    $cmdFile = Join-Path $Repo 'tools\schedule\storage_gc.cmd'
    if (-not (Test-Path $cmdFile)) { throw "找不到任务动作 $cmdFile" }
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c "{0}"' -f $cmdFile)
    $trigger = New-ScheduledTaskTrigger -Daily -At '04:10'
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal `
        -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskPath $TaskPath -TaskName 'MarketOperatorStorageGC' `
        -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal `
        -Description 'market-operator 每日存储治理(日志/备份/缓存/无损去重;预算 35GB)' `
        -Force | Out-Null
    Write-Log ("已注册计划任务 {0}MarketOperatorStorageGC(每日 04:10)" -f $TaskPath)
}

switch ($Action) {
    'register' { Register-RunnerTask; Show-Status }
    'start' { Start-Runner; Show-Status }
    'stop' { Stop-Runner; Show-Status }
    'status' { Show-Status }
    'guard' { Invoke-Guard }
    'gc' { Invoke-Gc }
    'register-gc' { Register-GcTask }
}
