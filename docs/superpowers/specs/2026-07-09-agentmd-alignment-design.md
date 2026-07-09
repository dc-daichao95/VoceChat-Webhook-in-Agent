# 设计:按 Agent.md 校正并对齐本项目(方案 A)

- 日期:2026-07-09
- 状态:待评审
- 关联:`Agent.md`(项目章程)、`docs/superpowers/specs/2026-07-08-vocechat-answering-machine-design.md`

## 1. 背景与目标

`Agent.md` 是本仓库的 AI 智能体开发章程,但其内容大量沿用了另一个「Rust + Web + 内核检视」项目的模板,与本项目的真实形态(**纯 Python 的 VoceChat 应答机器人**:FastAPI 接收器 + 本机 brain 经 WebDAV 拉取 + `send.py` 出站)存在明显偏差。若照字面对齐会引入 Rust/Web/DB/Excel 等无关内容,反而违背 `Agent.md` 自身的「简单优先 / 外科手术式修改 / YAGNI」原则。

目标:**先校正 `Agent.md` 使其贴合现实,再据此对项目做适用范围内的对齐与卫生整理**,并保证 `pytest` 全绿。

## 2. 范围

**In(本次要做):**
- 修订 `Agent.md`:技术栈、代码质量条款、目录结构、子智能体/技能清单校正为项目真实形态。
- 目录结构对齐:新增 `skill/`(迁入 `/loop` 操作手册)与 `build/`(打包产物),并同步更新引用。
- 代码卫生(外科式、测试兜底):修复当前 2 个失败测试(恢复测试隔离);为公开函数补 docstring。

**Out(明确不做):**
- 不引入 Rust / Web UI / 数据库 / Excel 报告等 `Agent.md` 模板残留的技术栈。
- 不改动任何业务逻辑与消息处理流程(过滤、拉取、发送、记账均不变)。
- 不重构无关代码,不动历史设计/计划文档中的既成记录(仅更新会误导「当前用法」的活引用)。

## 3. 设计 A —— 修订 Agent.md

以「删减不适用 + 映射到 Python」为原则,逐节校正:

- **技术栈**:语言改为 **Python 3.8+**;运行平台保留(Windows 主机 + 部分服务 Docker,跨平台兼容);Web 框架/模板/DB/报告等改为项目实际用到的 **FastAPI、`requests`、WebDAV、`python-dotenv`、`pytest`**;删除 Axum/Actix-Web、Tera/Askama、TailwindCSS、SeaORM/Diesel、SQLite、Excel、「内核代码/中断上下文」等无关表述。
- **设计原则**:保留「先设计后编码、配合 UML 阐述、动静结合」;删除「贴合内核场景」这类不适用项(或改为「贴合本项目场景:webhook 时序、WebDAV 轮询、去重与游标」)。
- **代码质量与风格**:命名规范对 Python 依然适用(`snake_case` / `PascalCase` / `UPPER_SNAKE_CASE`),保留;长度限制(函数 ≤50 行、文件 ≤500 行)保留;将 Rust 专属项映射为 Python 等价:
  - `Rustdoc ///` → **模块/函数 docstring**;
  - `thiserror/anyhow` 自定义错误 → **自定义异常类型 + 清晰上下文**;
  - `unwrap()/expect()`、`dead_code` 警告 → **避免裸 `except` / 未使用导入,优先 early-return + 异常处理**。
- **约定**:删除「类型与所有权(Rust 生命周期/借用/`clone`)」;保留「架构分层解耦、状态管理」并改写为 Python/FastAPI 语境。
- **子智能体目录**:裁剪为当前 Cursor 环境真实可用者(`@generalPurpose`、`@explore`、`@shell`、`@cursor-guide`、`@best-of-n-runner`、`@code-reviewer` 等);删除或标注为「规划中」不存在的角色(如 `@rust-reviewer`、`@frontend-code-reviewer`、`@opencode-integrator`、`@requirements-analyst` 等自定义角色)。
- **技能目录**:仅保留本环境确实存在/相关的技能;移除 `pencil-ui-design` 等与本项目无关项(除非确认存在)。

> 校正后 `Agent.md` 应能被后续开发直接采信,不再产生误导。

## 4. 设计 B —— 目录结构对齐

`Agent.md` §目录结构要求存在 `skill/` 与 `build/`。

- **`skill/`**:把 `scripts/loop_prompt.md` 迁移为 `skill/loop_prompt.md`(它是「供 agent 使用的大脑每轮操作手册」,语义上属于 skill 而非通用脚本)。
- **`build/`**:作为打包产物目录。`scripts/package_receiver.ps1` 默认输出改为 `build/receiver-build-context.tar.gz`;`.gitignore` 增加 `build/`(`*.tar.gz` 规则保留)。

**结构对比(相关部分):**

```
迁移前                              迁移后
scripts/loop_prompt.md         →    skill/loop_prompt.md
receiver-build-context.tar.gz  →    build/receiver-build-context.tar.gz
```

**需同步更新的活引用:**
- `scripts/brain_cycle.py`:注释中的 `scripts/loop_prompt.md` → `skill/loop_prompt.md`。
- `README.md`:目录树、运行说明中对 `scripts/loop_prompt.md` 与打包产物路径的引用。
- `scripts/package_receiver.ps1`:默认 `-Output` 与示例、`.SYNOPSIS`。
- (历史文档 `docs/superpowers/plans|specs/2026-07-08-*` 为既成记录,不回改。)

## 5. 设计 C —— 代码卫生(外科式)

### 5.1 修复测试隔离(最高优先级)

**问题**:`app/config.py::load_config()` 与 `send.py::main()` 调用无参 `load_dotenv()`,会自动加载仓库根真实 `.env`。测试用 `monkeypatch.delenv` 删除变量后又被 `.env` 塞回,导致:
- `tests/test_config.py::test_load_config_missing_bot_uid`(期望抛 `ValueError`,未抛);
- `tests/test_send.py::test_main_missing_env_returns_2`(期望返回 2,却真发请求)。

**方案**:新增 `tests/conftest.py`,提供一个 **autouse fixture**,将各模块引用的 `load_dotenv` 名字打桩为 no-op(`monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: False)` 与 `send.load_dotenv` 同理),确保测试**不受仓库根 `.env` 是否存在的影响**,恢复全局测试隔离。
- 只改测试基建(新增 `conftest.py`),不改动生产代码行为(生产环境仍正常加载 `.env`)。
- 完成后 `pytest -q` 必须 **0 失败**。

### 5.2 补充 docstring

为以下模块的公开函数补「解释 Why」的简洁 docstring(不改逻辑、不改签名):
- `app/config.py`(`load_config`)、`app/filters.py`、`app/receiver.py`、`app/storage.py`
- `brain/pull.py`、`brain/select.py`
- `send.py`(`build_url` / `send_message` / `main`)

保持精炼,避免自明式注释;遵守函数 ≤50 行、文件 ≤500 行(现状已满足,补 docstring 后仍需复核 `scripts/webdav_check.py` 242 行不越界)。

## 6. 架构与数据流影响

无。本次仅涉及**文档、目录结构、测试基建与注释**,不触碰 receiver/brain/send 的运行时行为与数据流(`conversations/*.jsonl`、`data/state.json`、seen_mids、条件 GET 等一律不变)。因此不引入新的 UML 时序/组件设计。

## 7. 测试与验证

- 迁移与改动后运行 `python -m pytest -q`,要求 **2 failed → 0 failed**(27+ passed)。
- 结构验证:`skill/loop_prompt.md`、`build/` 存在;`git grep loop_prompt` 无遗留的 `scripts/loop_prompt.md` 活引用(历史文档除外)。
- 打包脚本冒烟:`package_receiver.ps1` 能在 `build/` 下产出 tar.gz 且被 `.gitignore` 忽略。
- 可追溯性:每处改动对应本设计的某一节;无顺手重构。

## 8. 边界与风险

- **不破坏持续集成/向后兼容**(Agent.md 核心原则 #5):`.env` 加载在生产路径不变;仅测试打桩。目录迁移只动手册与产物,不动导入路径与包结构(`app/`、`brain/` 不变)。
- 风险点:遗漏某处对 `scripts/loop_prompt.md` 的引用 → 用 `git grep` 兜底核查。
