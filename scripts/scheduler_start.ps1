<#
.SYNOPSIS
    启动已注册的可靠调度器计划任务。
.DESCRIPTION
    仅触发任务运行;单实例并行由任务 MultipleInstances=IgnoreNew 与调度器 PidFileLock 双重拦截,
    重复调用不会产生第二个常驻进程。
.EXAMPLE
    powershell -File scripts/scheduler_start.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$TaskName = "AnsweringMachineScheduler"
$RepoRoot = Split-Path -Parent $PSScriptRoot

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "[ERROR] 任务未安装: $TaskName。请先运行 scheduler_install.ps1。"
    exit 2
}

if ($task.State -eq "Running") {
    Write-Output "[OK] 调度器已在运行($TaskName),无需重复启动。"
    exit 0
}

Start-ScheduledTask -TaskName $TaskName
Write-Output "[OK] 已启动计划任务: $TaskName (工作目录 $RepoRoot)"
exit 0
