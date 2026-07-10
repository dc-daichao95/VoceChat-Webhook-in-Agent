<#
.SYNOPSIS
    停止并注销可靠调度器计划任务。
.DESCRIPTION
    先 Stop-ScheduledTask 结束运行,再 Unregister-ScheduledTask 移除任务。
    默认需要交互确认(ConfirmImpact=High);-Force 可在非交互场景跳过确认。支持 -WhatIf 干跑。
.PARAMETER Force
    跳过确认直接注销。
.EXAMPLE
    powershell -File scripts/scheduler_uninstall.ps1
    powershell -File scripts/scheduler_uninstall.ps1 -Force
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$TaskName = "AnsweringMachineScheduler"
$RepoRoot = Split-Path -Parent $PSScriptRoot

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "[SKIP] 任务未安装,无需注销: $TaskName。工作目录 $RepoRoot"
    exit 0
}

if (-not ($Force -or $PSCmdlet.ShouldProcess($TaskName, "注销计划任务"))) {
    Write-Output "[ABORT] 未确认,取消注销。使用 -Force 可跳过确认。"
    exit 3
}

if ($task.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "[OK] 已注销计划任务: $TaskName"
exit 0
