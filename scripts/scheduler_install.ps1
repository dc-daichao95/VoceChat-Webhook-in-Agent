<#
.SYNOPSIS
    在 Windows 任务计划程序中注册可靠调度器,登录时自启并在异常退出后自动重启。
.DESCRIPTION
    使用仓库内固定 Python 解释器与绝对工作目录运行 `python scripts/scheduler.py run`。
    单实例约束由任务的 MultipleInstances=IgnoreNew 与调度器自身的 PidFileLock 双重保证。
    幂等:重复运行不报错;任务已存在时需 -Force 才更新定义。支持 -WhatIf 干跑,不改动任何系统任务。
.PARAMETER Python
    Python 解释器路径。缺省优先使用仓库内 .venv\Scripts\python.exe,否则回退到 PATH 中的 python.exe。
.PARAMETER User
    运行任务的用户;缺省当前登录用户(不硬编码)。
.PARAMETER Force
    任务已存在时更新其定义,而非拒绝。
.EXAMPLE
    powershell -File scripts/scheduler_install.ps1 -WhatIf
    powershell -File scripts/scheduler_install.ps1
    powershell -File scripts/scheduler_install.ps1 -Force
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Python,
    [ValidateNotNullOrEmpty()]
    [string]$User = $env:USERNAME,
    [switch]$Force
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
    # 优先仓库内固定解释器,保证与开发环境依赖一致且路径可预测。
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return (Resolve-Path $venv).Path }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "未找到 python.exe;请用 -Python 指定仓库内解释器的绝对路径。"
}

$pythonPath = Resolve-PythonPath -Root $RepoRoot -Explicit $Python
# 相对脚本 + 绝对工作目录:参数稳定可读,工作目录钉死在仓库根。
$scriptArg = "scripts/scheduler.py run"

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument $scriptArg `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User

# IgnoreNew:已有实例运行时忽略新触发,杜绝多调度器并行。
# 失败后每 1 分钟重启,最多 999 次,近似"无限自愈"。ExecutionTimeLimit 0 表示常驻不限时。
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Write-Output "== 计划任务定义(intended) =="
Write-Output "TaskName       : $TaskName"
Write-Output "Python         : $pythonPath"
Write-Output "Argument       : $scriptArg"
Write-Output "WorkingDir     : $RepoRoot"
Write-Output "Trigger        : AtLogOn (User=$User)"
Write-Output "MultipleInst   : IgnoreNew"
Write-Output "Restart        : 每 1 分钟, 最多 999 次"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    # 默认幂等:重复安装不报错。任务已存在即视为成功;需要更新定义时显式加 -Force。
    Write-Output "[OK] 任务已存在,视为成功: $TaskName。使用 -Force 更新定义。"
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "注册/更新计划任务")) {
    # -Force 让 Register 覆盖同名任务,实现定义级幂等更新。
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "AnsweringMachine 可靠调度器(登录自启, 异常自愈, 单实例)" `
        -Force | Out-Null
    Write-Output "[OK] 已注册计划任务: $TaskName"
}

exit 0
