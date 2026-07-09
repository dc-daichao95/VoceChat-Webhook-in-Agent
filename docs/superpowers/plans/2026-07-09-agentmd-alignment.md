# Agent.md 校正与项目对齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按方案 A 校正 `Agent.md` 使其贴合纯 Python 项目,并做适用范围内的目录对齐与代码卫生,使 `pytest` 全绿。

**Architecture:** 纯文档 + 目录结构 + 测试基建 + docstring 层面的整理,不触碰 receiver/brain/send 运行时行为与数据流。

**Tech Stack:** Python 3.8+、pytest、python-dotenv、FastAPI、requests、PowerShell(打包脚本)。

关联 spec:`docs/superpowers/specs/2026-07-09-agentmd-alignment-design.md`

---

### Task 1: 修复测试隔离,恢复 pytest 全绿

**Files:**
- Create: `tests/conftest.py`
- Test: `tests/test_config.py::test_load_config_missing_bot_uid`、`tests/test_send.py::test_main_missing_env_returns_2`

- [ ] **Step 1: 先复现失败**

Run: `python -m pytest -q tests/test_config.py::test_load_config_missing_bot_uid tests/test_send.py::test_main_missing_env_returns_2`
Expected: 2 failed(前提是仓库根存在真实 `.env`)。

- [ ] **Step 2: 新增 conftest,打桩 load_dotenv 为 no-op(autouse)**

```python
# tests/conftest.py
"""全局测试隔离:阻止各模块加载仓库根真实 .env,保证测试不依赖运行环境。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    # load_config / send.main 会调用无参 load_dotenv() 自动读取 ./.env,
    # 这会污染 monkeypatch.delenv 后的环境。测试期一律打桩为 no-op。
    noop = lambda *args, **kwargs: False
    monkeypatch.setattr("app.config.load_dotenv", noop, raising=False)
    monkeypatch.setattr("send.load_dotenv", noop, raising=False)
```

- [ ] **Step 3: 运行全部测试**

Run: `python -m pytest -q`
Expected: 0 failed(29 passed 附近),无 `.env` 相关污染。

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test: isolate load_dotenv so suite ignores ambient .env"
```

---

### Task 2: 目录结构对齐(skill/ 与 build/)

**Files:**
- Move: `scripts/loop_prompt.md` → `skill/loop_prompt.md`
- Modify: `scripts/brain_cycle.py`(注释引用)、`README.md`、`scripts/package_receiver.ps1`、`.gitignore`

- [ ] **Step 1: 迁移 loop 手册到 skill/**

```bash
git mv scripts/loop_prompt.md skill/loop_prompt.md
```

- [ ] **Step 2: 更新 brain_cycle.py 注释引用**

把 `scripts/brain_cycle.py` 中 `见 scripts/loop_prompt.md` 改为 `见 skill/loop_prompt.md`。

- [ ] **Step 3: package_receiver.ps1 产物默认输出到 build/**

将默认参数改为:

```powershell
param(
    [string]$Output = "build/receiver-build-context.tar.gz"
)
```

并同步 `.SYNOPSIS`/`.EXAMPLE`/结尾提示里的产物路径描述(`receiver-build-context.tar.gz` → `build/receiver-build-context.tar.gz`)。

- [ ] **Step 4: .gitignore 增加 build/**

在 `# build artifacts` 段落追加一行 `build/`(保留原 `*.tar.gz`)。

- [ ] **Step 5: 更新 README 引用**

- 目录树:`scripts/loop_prompt.md` 一项移至 `skill/`;补 `build/` 说明。
- 运行说明:`执行 scripts/loop_prompt.md` → `执行 skill/loop_prompt.md`。
- 打包段:`生成 receiver-build-context.tar.gz` → `生成 build/receiver-build-context.tar.gz`。

- [ ] **Step 6: 兜底核查无遗留活引用**

Run: `git grep -n "scripts/loop_prompt.md"`
Expected: 仅 `docs/superpowers/plans|specs/2026-07-08-*`(历史记录,不改);无源码/README/活文档命中。

- [ ] **Step 7: 冒烟打包脚本 + 跑测试**

Run: `powershell -File scripts/package_receiver.ps1; python -m pytest -q`
Expected: `build/receiver-build-context.tar.gz` 生成且被忽略;测试仍 0 失败。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "chore: align dirs to Agent.md (skill/ loop prompt, build/ artifacts)"
```

---

### Task 3: 为公开函数补 docstring

**Files:**
- Modify: `app/config.py`、`app/filters.py`、`app/receiver.py`、`app/storage.py`、`brain/pull.py`、`brain/select.py`、`send.py`

- [ ] **Step 1: 逐文件补「解释 Why」的简洁 docstring**

规则:模块顶部 + 每个公开函数一句 docstring;只说用途/约束/为什么,不复述实现;不改签名与逻辑。示例(`send.py`):

```python
def build_url(server_url: str, *, uid=None, gid=None) -> str:
    """按目标类型拼接 VoceChat bot 发送端点;uid/gid 二选一,缺失即用法错误。"""
```

```python
def send_message(server_url, api_key, text, *, uid=None, gid=None, markdown=False, timeout=30):
    """向 VoceChat 发一条消息;markdown=True 时用 text/markdown,否则 text/plain。"""
```

对 `load_config`、`filters` 的各纯函数、`receiver` 的路由处理、`storage` 的落盘函数、`pull` 的 WebDAV 方法、`select.select_pending` 同样各补一句。

- [ ] **Step 2: 复核文件长度未越界**

Run: `python -c "import pathlib; [print(len(p.read_text(encoding='utf-8').splitlines()), p) for p in pathlib.Path('.').rglob('*.py') if '__pycache__' not in str(p) and '.pytest_cache' not in str(p)]"`
Expected: 全部 < 500 行(`scripts/webdav_check.py` 补后仍 < 500)。

- [ ] **Step 3: 跑测试**

Run: `python -m pytest -q`
Expected: 0 failed(docstring 不影响行为)。

- [ ] **Step 4: Commit**

```bash
git add app brain send.py
git commit -m "docs: add module and public-function docstrings"
```

---

### Task 4: 修订 Agent.md 贴合本项目

**Files:**
- Modify: `Agent.md`

- [ ] **Step 1: 技术栈(§技术栈)改写为 Python-only**

- 语言:`Python 3.8+`(删除 Rust)。
- Web/模板/DB/报告:改为 `FastAPI、requests、WebDAV(python-dotenv 管理配置)`;删除 Axum/Actix-Web、Tera/Askama、TailwindCSS、SeaORM/Diesel、SQLite、Excel。
- 测试:`pytest`(删除 `cargo test`)。
- 保留:运行平台(Windows 主机 + Docker,跨平台兼容)。

- [ ] **Step 2: 设计原则(§设计原则)**

删除「贴合内核场景(内存分配/并发原语/中断上下文)」,改为「贴合本项目场景:webhook 时序、WebDAV 轮询、去重与游标」;保留 UML/动静结合/先设计后编码。

- [ ] **Step 3: 代码质量(§代码质量与风格)映射到 Python**

- 命名(snake_case/PascalCase/UPPER_SNAKE_CASE)、长度限制(函数 ≤50、文件 ≤500):保留。
- `Rustdoc ///` → 「模块/函数 docstring」。
- `thiserror/anyhow` → 「自定义异常类型 + 清晰上下文」。
- `unwrap()/expect()`、`dead_code` → 「避免裸/宽泛 `except`、删除未使用导入、优先 early-return」。

- [ ] **Step 4: 约定(§约定)**

删除「类型与所有权(Rust 生命周期/借用/clone)」;「状态管理」「架构分层」改写为 Python/FastAPI 语境(如 FastAPI 依赖注入/应用状态、业务逻辑与路由解耦)。

- [ ] **Step 5: 验证(§验证与完成)**

`cargo test 或 pytest` → 仅 `pytest`;其余(0 失败、覆盖率、可追溯性、架构验证)保留。

- [ ] **Step 6: 子智能体与技能目录**

- 子智能体:仅保留当前 Cursor 环境真实可用者(`@generalPurpose`、`@explore`、`@shell`、`@cursor-guide`、`@best-of-n-runner`、`@code-reviewer`);删除不存在的自定义角色(`@requirements-analyst`、`@feature-designer`、`@planner`、`@ui-designer`、`@developer`、`@tech-leader`、`@rust-reviewer`、`@frontend-code-reviewer`、`@qa`、`@opencode-integrator`),或将其整体降级为「(规划中,当前环境未提供)」一句说明,不逐条罗列失效角色。
- 技能:仅保留本环境确实存在者(如 `test-driven-development`、`writing-skills`、`brainstorming`、`writing-plans`、`executing-plans`、`subagent-driven-development`);删除 `git-commit-flow`、`plantuml-lint`、`pencil-ui-design` 等未提供项。

- [ ] **Step 7: 目录结构(§目录结构)**

确认与实际一致:`app/ brain/ scripts/ skill/ build/ tests/ docs/`(Task 2 已建 `skill/`、`build/`)。

- [ ] **Step 8: 通读一致性 + 跑测试**

人工通读 `Agent.md` 无 Rust/Web/DB 残留;`python -m pytest -q` 仍 0 失败。

- [ ] **Step 9: Commit**

```bash
git add Agent.md
git commit -m "docs: retarget Agent.md to the Python VoceChat bot (drop Rust/web/db template residue)"
```

---

## Self-Review

**Spec coverage:**
- spec §3(修订 Agent.md)→ Task 4(各小节)✅
- spec §4(skill//build/ 对齐 + 引用更新)→ Task 2 ✅
- spec §5.1(测试隔离)→ Task 1 ✅;§5.2(docstring)→ Task 3 ✅
- spec §7(验证 0 失败)→ Task 1/2/3/4 各自 pytest 步 ✅
- spec §8(引用兜底)→ Task 2 Step 6 ✅

**Placeholder scan:** 无 TBD/TODO;各代码步给出了具体内容或具体改写指令。

**Type consistency:** 打桩目标名 `app.config.load_dotenv` / `send.load_dotenv` 与源码 `from dotenv import load_dotenv` 的模块级名字一致;产物路径 `build/receiver-build-context.tar.gz` 在 Task 2 各步统一。

**顺序:** 先修测试得绿基线(Task 1)→ 结构(Task 2)→ docstring(Task 3)→ 文档改写(Task 4),每步独立可提交。
