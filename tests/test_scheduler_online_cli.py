"""online_fetch CLI 参数、脱敏失败与进程退出契约测试。"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import online_fetch


def assert_safe_failure(captured, error_name, stage):
    """断言 stdout 是稳定失败 JSON，且 stderr 无 traceback。"""
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["status"] == "failed"
    assert result["evidence"] == []
    assert result["errors"] == [
        {"source": "cli", "stage": stage, "error": error_name}
    ]
    return result


@pytest.mark.parametrize("job_id", ("0", "-1"))
def test_online_fetch_cli_requires_positive_job_id(
    job_id, monkeypatch, capsys
):
    """显式 job-id 必须严格大于零，且无效时不得初始化或抓取。"""

    def forbidden(*args, **kwargs):
        raise AssertionError("runtime must not start")

    monkeypatch.setattr(online_fetch, "QueueDB", forbidden)
    monkeypatch.setattr(online_fetch, "gather_progressively", forbidden)

    with pytest.raises(SystemExit) as error:
        online_fetch.main(
            [
                "json",
                "https://example.com/data",
                "--job-id",
                job_id,
            ]
        )

    assert error.value.code == 2
    assert "positive" in capsys.readouterr().err


def test_online_fetch_cli_requires_owner_with_job_id(
    monkeypatch, capsys
):
    """持久化证据时必须显式提供 owner，且参数错误不得启动运行时。"""

    def forbidden(*args, **kwargs):
        raise AssertionError("runtime must not start")

    monkeypatch.setattr(online_fetch, "QueueDB", forbidden)
    monkeypatch.setattr(online_fetch, "gather_progressively", forbidden)

    with pytest.raises(SystemExit) as error:
        online_fetch.main(
            ["json", "https://example.com/data", "--job-id", "1"]
        )

    assert error.value.code == 2
    assert "owner" in capsys.readouterr().err


def test_online_fetch_cli_sanitizes_database_initialization_failure(
    monkeypatch, capsys
):
    """QueueDB 初始化异常必须转为稳定 JSON，不泄漏路径、key 或 URL。"""

    def broken_db(path):
        raise OSError("C:\\private\\queue.db api_key=db-secret")

    monkeypatch.setattr(online_fetch, "QueueDB", broken_db)
    code = online_fetch.main(
        [
            "json",
            "https://example.com/data?api_key=url-secret",
            "--job-id",
            "1",
            "--owner",
            "cursor",
            "--db",
            "C:\\private\\queue.db",
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert_safe_failure(captured, "OSError", "database")
    assert "private" not in captured.out
    assert "secret" not in captured.out


def test_online_fetch_cli_sanitizes_unknown_runtime_failure(
    monkeypatch, capsys
):
    """未知执行异常不得冒泡或把异常正文写入 stdout/stderr。"""

    def broken(*args, **kwargs):
        raise RuntimeError("https://private/path?key=top-secret")

    monkeypatch.setattr(online_fetch, "gather_progressively", broken)

    code = online_fetch.main(
        ["json", "https://example.com/data?key=url-secret"]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert_safe_failure(captured, "RuntimeError", "execute")
    assert "top-secret" not in captured.out + captured.err
    assert "url-secret" not in captured.out + captured.err


def test_online_fetch_cli_uses_strict_json_output(monkeypatch, capsys):
    """结果含 NaN 时必须拒绝该结果并输出有限的脱敏失败 JSON。"""

    def non_finite(*args, **kwargs):
        return {
            "status": "complete",
            "evidence": [{"data": float("nan")}],
            "errors": [],
            "fallback": None,
        }

    monkeypatch.setattr(online_fetch, "gather_progressively", non_finite)

    code = online_fetch.main(["json", "https://example.com/data"])

    captured = capsys.readouterr()
    assert code == 1
    assert_safe_failure(captured, "ValueError", "output")
    assert "NaN" not in captured.out


def test_online_fetch_subprocess_hides_database_failure_details(tmp_path):
    """真实 CLI 进程的 DB 初始化失败不得打印 traceback 或敏感参数。"""
    invalid_db = tmp_path / "private-db-secret"
    invalid_db.mkdir()
    script = Path(online_fetch.__file__).resolve()
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "json",
            "https://example.com/data?api_key=url-secret",
            "--job-id",
            "1",
            "--owner",
            "cursor",
            "--db",
            str(invalid_db),
        ],
        cwd=str(script.parent.parent),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["errors"] == [
        {
            "source": "cli",
            "stage": "database",
            "error": "OperationalError",
        }
    ]
    assert "private-db-secret" not in completed.stdout
    assert "url-secret" not in completed.stdout
