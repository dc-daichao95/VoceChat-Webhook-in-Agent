<#
.SYNOPSIS
    优雅停止可靠调度器计划任务,并等待其 PidFileLock 释放。
.DESCRIPTION
    调用 Stop-ScheduledTask 结束任务后,轮询任务状态直到不再 Running,并尝试独占打开
    PidFileLock 文件以确认锁已释放(能独占打开即代表常驻进程已退出、锁被 OS 回收)。
.PARAMETER LockPath
    PidFileLock 文件路径;缺省 data\scheduler.lock。
.PARAMETER TimeoutSeconds
    等待锁释放的最长秒数。
.EXAMPLE
    powershell -File scripts/scheduler_stop.ps1
#>
[CmdletBinding()]
param(
    [string]$LockPath,
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

$TaskName = "AnsweringMachineScheduler"
$RepoRoot = Split-Path -Parent $PSScriptRoot
# 锁路径单一来源:复用调度器读取的 SCHEDULER_LOCK 环境变量,缺省与 SchedulerConfig
# 的 data/scheduler.lock 默认一致,避免在不同脚本里各自硬编码。
if (-not $LockPath) {
    if ($env:SCHEDULER_LOCK) { $LockPath = $env:SCHEDULER_LOCK }
    else { $LockPath = Join-Path $RepoRoot "data\scheduler.lock" }
}

function Test-LockReleased {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $true }
    try {
        # 独占(无共享)打开成功 => PidFileLock 的字节区锁已释放,调度器确已退出。
        $stream = [System.IO.File]::Open($Path, "Open", "ReadWrite", "None")
        $stream.Close()
        return $true
    } catch {
        return $false
    }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "[ERROR] 任务未安装: $TaskName。"
    exit 2
}

Stop-ScheduledTask -TaskName $TaskName

# 单一总超时:在同一预算内轮询,直到任务退出 Running 且 PidFileLock 释放。
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$released = $false
while ((Get-Date) -lt $deadline) {
    $running = (Get-ScheduledTask -TaskName $TaskName).State -eq "Running"
    if (-not $running -and (Test-LockReleased -Path $LockPath)) {
        $released = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if ($released) {
    Write-Output "[OK] 调度器已停止,PidFileLock 已释放: $LockPath"
    exit 0
}

Write-Output "[WARN] 已发送停止命令,但在 $TimeoutSeconds 秒内未确认 PidFileLock 释放: $LockPath"
exit 1
