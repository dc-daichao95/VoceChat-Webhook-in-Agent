<#
.SYNOPSIS
    汇报调度器计划任务状态与调度器自身健康快照。
.DESCRIPTION
    打印 Get-ScheduledTask/Get-ScheduledTaskInfo 的运行状态、上次运行时间与结果,
    并调用 `python scripts/scheduler.py health` 输出健康 JSON(不含任何敏感字段)。
.PARAMETER Python
    Python 解释器路径;缺省优先仓库内 .venv,否则 PATH 中的 python.exe。
.EXAMPLE
    powershell -File scripts/scheduler_status.ps1
#>
[CmdletBinding()]
param(
    [string]$Python
)

$ErrorActionPreference = "Stop"

$TaskName = "AnsweringMachineScheduler"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Resolve-PythonPath {
    param([string]$Root, [string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "指定的 Python 不存在: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return (Resolve-Path $venv).Path }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "未找到 python.exe;请用 -Python 指定仓库内解释器的绝对路径。"
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "[ERROR] 任务未安装: $TaskName。请先运行 scheduler_install.ps1。"
    exit 2
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output "== 计划任务状态 =="
Write-Output "TaskName       : $TaskName"
Write-Output "State          : $($task.State)"
Write-Output "LastRunTime    : $($info.LastRunTime)"
Write-Output "LastTaskResult : $($info.LastTaskResult)"
Write-Output "NextRunTime    : $($info.NextRunTime)"
Write-Output "NumberOfMissedRuns : $($info.NumberOfMissedRuns)"

Write-Output ""
Write-Output "== 调度器健康快照 =="
$pythonPath = Resolve-PythonPath -Root $RepoRoot -Explicit $Python
Push-Location $RepoRoot
try {
    & $pythonPath scripts/scheduler.py health
    $healthExit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($healthExit -ne 0) {
    Write-Output "[WARN] 读取健康快照失败(exit $healthExit)。"
    exit 1
}

exit 0
