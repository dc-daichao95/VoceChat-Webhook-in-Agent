"""全局测试隔离:阻止各模块加载仓库根真实 .env,保证测试不依赖运行环境。"""
import sys
from pathlib import Path

import pytest

# scripts/ 下的模块(如 compact)可被测试直接 import。追加到末尾而非最前,
# 以免 scripts/scheduler.py 抢在仓库根前,遮蔽 scheduler 包(import scheduler)。
_REPO = str(Path(__file__).resolve().parent.parent)
_SCRIPTS = str(Path(__file__).resolve().parent.parent / "scripts")
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    # load_config / send.main 会调用无参 load_dotenv() 自动读取 ./.env,
    # 这会把 monkeypatch.delenv 删掉的变量重新塞回,破坏测试隔离。测试期一律打桩为 no-op。
    def _noop(*args, **kwargs):
        return False

    monkeypatch.setattr("app.config.load_dotenv", _noop, raising=False)
    monkeypatch.setattr("send.load_dotenv", _noop, raising=False)
