"""契约测试:校验 Windows 任务计划生命周期脚本(Task 8)。

PowerShell 无法在通用 CI 上执行,因此这里只做可移植的静态校验:
脚本存在性、关键命令字符串、幂等标志、绝对路径解析、无明文凭据;
在本机有 PowerShell 时,额外做 AST 语法解析检查(不注册任何系统任务、
不启动任何常驻进程)。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
TASK_NAME = "AnsweringMachineScheduler"

LIFECYCLE_SCRIPTS = (
    "scheduler_install.ps1",
    "scheduler_start.ps1",
    "scheduler_stop.ps1",
    "scheduler_status.ps1",
    "scheduler_uninstall.ps1",
)

# 明文凭据/敏感字段黑名单;脚本一律通过环境变量或 .env 间接获取,不得内联。
FORBIDDEN_SECRET_TOKENS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
)


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("name", LIFECYCLE_SCRIPTS)
def test_script_file_exists(name: str) -> None:
    assert (SCRIPTS / name).is_file()


@pytest.mark.parametrize("name", LIFECYCLE_SCRIPTS)
def test_task_name_is_consistent(name: str) -> None:
    assert TASK_NAME in _read(name)


@pytest.mark.parametrize("name", LIFECYCLE_SCRIPTS)
def test_error_action_preference_is_strict(name: str) -> None:
    text = _read(name)
    assert '$ErrorActionPreference = "Stop"' in text


@pytest.mark.parametrize("name", LIFECYCLE_SCRIPTS)
def test_repo_root_resolved_from_script_root(name: str) -> None:
    # 用 $PSScriptRoot 派生绝对仓库根,避免依赖调用者的当前目录。
    assert "Split-Path -Parent $PSScriptRoot" in _read(name)


@pytest.mark.parametrize("name", LIFECYCLE_SCRIPTS)
def test_no_hardcoded_credentials(name: str) -> None:
    lowered = _read(name).lower()
    for token in FORBIDDEN_SECRET_TOKENS:
        assert token not in lowered, f"{name} 疑似内联敏感字段: {token}"


def test_install_action_runs_scheduler_run() -> None:
    text = _read("scheduler_install.ps1")
    assert "New-ScheduledTaskAction" in text
    assert "scripts/scheduler.py run" in text
    # 固定绝对工作目录 = 仓库根。
    assert "-WorkingDirectory" in text


def test_install_uses_logon_or_startup_trigger() -> None:
    text = _read("scheduler_install.ps1")
    assert "New-ScheduledTaskTrigger" in text
    assert ("-AtLogOn" in text) or ("-AtStartup" in text)


def test_install_configures_restart_and_single_instance() -> None:
    text = _read("scheduler_install.ps1")
    assert "-RestartCount" in text
    assert "-RestartInterval" in text
    # 禁止多实例并行(配合 PidFileLock)。
    assert "IgnoreNew" in text
    assert "MultipleInstances" in text


def test_install_is_idempotent_when_task_exists() -> None:
    text = _read("scheduler_install.ps1")
    assert "[switch]$Force" in text
    assert "Get-ScheduledTask" in text
    # 幂等更新任务定义。
    assert "Register-ScheduledTask" in text
    # 默认幂等:任务已存在且未 -Force 时视为成功并提示,不以错误码退出。
    assert "已存在" in text
    assert "-Force 更新定义" in text
    assert "exit 3" not in text


def test_install_supports_whatif_dry_run() -> None:
    assert "SupportsShouldProcess" in _read("scheduler_install.ps1")


def test_install_resolves_python_to_absolute_path() -> None:
    text = _read("scheduler_install.ps1")
    # 使用仓库内固定 Python 解释器并解析成绝对路径。
    assert "python.exe" in text
    assert "-Execute" in text


def test_start_uses_start_scheduled_task() -> None:
    assert "Start-ScheduledTask" in _read("scheduler_start.ps1")


def test_stop_is_graceful_and_waits_for_lock_release() -> None:
    text = _read("scheduler_stop.ps1")
    assert "Stop-ScheduledTask" in text
    # 优雅停止:停任务后等待 PidFileLock 文件释放。
    assert "scheduler.lock" in text


def test_stop_confirms_release_via_exclusive_open() -> None:
    text = _read("scheduler_stop.ps1")
    # 通过独占(无共享)打开锁文件来确认 PidFileLock 已释放。
    assert "[System.IO.File]::Open" in text
    assert '"None"' in text


def test_stop_lock_path_shares_single_source_with_scheduler() -> None:
    text = _read("scheduler_stop.ps1")
    # 锁路径不另行硬编码,复用调度器读取的 SCHEDULER_LOCK 环境变量作为单一来源。
    assert "SCHEDULER_LOCK" in text


def test_status_reports_task_state_and_health() -> None:
    text = _read("scheduler_status.ps1")
    assert "Get-ScheduledTask" in text
    assert "scripts/scheduler.py health" in text


def test_uninstall_stops_and_unregisters() -> None:
    text = _read("scheduler_uninstall.ps1")
    assert "Stop-ScheduledTask" in text
    assert "Unregister-ScheduledTask" in text
    # 除非 -Force,否则需显式确认。
    assert "[switch]$Force" in text


@pytest.mark.parametrize("name", LIFECYCLE_SCRIPTS)
def test_scripts_use_explicit_exit_codes(name: str) -> None:
    # start/stop/status/install/uninstall 都要有明确的退出码。
    assert "exit 0" in _read(name)


_POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(_POWERSHELL is None, reason="本机无 PowerShell,跳过 AST 语法解析")
@pytest.mark.parametrize("name", LIFECYCLE_SCRIPTS)
def test_powershell_syntax_parses(name: str) -> None:
    path = SCRIPTS / name
    command = (
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{path}', [ref]$null, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { "
        "$errors | ForEach-Object { Write-Output $_.Message }; exit 1 }; "
        "exit 0"
    )
    result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
