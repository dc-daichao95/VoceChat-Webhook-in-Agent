## 核心准则 (Core Guidelines)
作为参与本代码库开发的 AI 智能体，你必须遵循以下底线原则：

1. **先思考后编码 (Think Before Coding)**：不要臆测。明确说明假设。如果有疑问，停下来提问。
2. **简单优先 (Simplicity First)**：编写能解决问题的最少代码。不添加推测性的功能。
3. **外科手术式修改 (Surgical Changes)**：只修改必须修改的地方。不要在未经要求的情况下“改进”相邻的代码。
4. **目标导向执行 (Goal-Driven Execution)**：定义可验证的成功标准。将命令式任务转化为可验证的目标（如测试），循环执行直至成功。
5. **保证与上游持续集成 (Ensure Upstream Continuous Integration)**：**【核心狠心原则】** 任何新提出的方案、架构设计或代码变更，必须将“能够保持持续集成与平滑升级”作为第一考量。绝不能为了实现特性而采取破坏性、无法向后兼容或无法合并上游更新的飞线设计 (Hack)！
6. **中文交互 (Communicate in Chinese)**：**【强制约束】** 所有与人类的交互（包括对话回复、解释说明、提问澄清、状态汇报等）必须使用中文。代码、标识符、Git 提交信息等仍遵循各自既定的语言规范。

## 设计原则 (Design Principles)
1. **严禁直接写示例代码**：先阐述设计，经确认后再落地。
2. **配合 UML 图阐述**：通过 PlantUML 画 UML 图配合说明阐述设计逻辑。在界面设计环节，可以使用 PlantUML Salt 绘制线框图 (Wireframe) 进行前端设计 Demo。
3. **动静结合**：设计时注重动态的功能设计（时序/流程），以及静态的软件架构设计（模块/分层）。
4. **贴合本项目场景**：检视与设计必须考虑本项目的特殊性——VoceChat webhook 时序、WebDAV 轮询与条件 GET、消息去重与处理游标、防自我循环。

## 技术栈 (Stack)
- **运行平台**：**【约束】** 本项目优先考虑在 **Windows 主机（部分服务在 Docker 容器）** 上运行。所有代码、脚本与工具**可**兼容 Linux、macOS 或其他 CPU 架构（如 ARM）。
- **语言**：Python 3.8+
- **接收端 (receiver)**：FastAPI（Docker 部署于 NAS，只收 webhook + 过滤 + 落盘）。
- **大脑 (brain) 与工具**：`requests`（VoceChat bot API 出站发送、WebDAV over HTTPS 拉取）、`python-dotenv`（配置）。
- **存储**：文件系统（会话 JSONL、`seen_mids.json`、本机 `state.json`）；本项目当前不引入数据库。
- **测试**：`pytest`
- **报告/输出**：JSON、文本 (Text)。

## 目录结构 (Directory Structure)
```
AnsweringMachine/
├─ app/                      # receiver(部署到 NAS 的部分)
├─ brain/                    # 大脑(本机的部分)
├─ scripts/                  # 脚本目录(打包、自检、单轮运行器等)
├─ skill/                    # 供 agent 使用的大脑每轮操作手册(如 loop_prompt.md)
├─ build/                    # 构建产物目录(打包 tar.gz;已 gitignore)
├─ tests/                    # 测试目录,pytest:filters/storage/config/receiver/send/select/pull/factory
└─ docs/                     # specs/ 规格、plans/ 计划、文档目录
```

## 代码质量与风格 (Code Quality & Style)
本项目的代码必须达到高质量、高可读性并符合社区规范 (PEP 8)，智能体在提交任何代码前必须严格检查以下标准：

1. **命名规范**：
   - 变量、函数与模块名：使用小写字母及下划线 (`snake_case`)。变量命名必须具备高度可读性，能清晰表达其用途。
   - 类名：使用大驼峰式 (`PascalCase`)。
   - 常量与模块级不可变量：使用全大写及下划线 (`UPPER_SNAKE_CASE`)。
2. **长度与规模限制**：
   - 函数长度：单个函数不应超过 50 行。
   - 文件长度：单个文件不应超过 500 行。
3. **注释与文档**：
   - 对于非直观的业务逻辑、复杂的正则或算法，必须添加精确的行内注释解释“为什么(Why)”这样做，而不是“做了什么(What)”。
   - 所有公开的模块、类、方法和函数，都应包含简洁的 **docstring**（用途/约束/为什么）。
   - 避免无意义的自明注释。
4. **防范复杂度**：
   - 函数应当短小精悍，遵循单一职责原则 (Single Responsibility Principle)。
   - 控制代码嵌套层级（禁止超过 4 层嵌套），尽量采用提早返回 (Early Return) 处理边界与错误，避免深层的 `if/else` 嵌套。
5. **容错与错误处理**：
   - 绝不使用裸 `except:` 或过于宽泛的 `except Exception` 吞掉错误，除非有明确理由（如接收器为避免 VoceChat 重试而始终回 200），并在注释中说明。
   - 优先使用清晰的内建异常或自定义异常类型传递错误上下文。向用户输出错误信息时提供可操作的建议。

## 约定 (Conventions)
- **类型注解 (Type Hints)**：公开函数尽量添加类型注解，提升可读性与工具支持；配合 `from __future__ import annotations`。
- **配置管理**：通过环境变量（可选 `.env` / `share.env`）加载配置，敏感凭据不入库、不打印到日志或会话输出。
- **架构分层**：严格保持核心逻辑（过滤/落盘/拉取/选择等纯逻辑）与 Web/CLI 层解耦。业务逻辑不应混入 FastAPI 路由处理器或命令行入口；纯函数优先，便于单测。

## 边界 (Boundaries)
- **范围限制**：绝对不要重构用户请求的直接范围之外的代码。
- **死代码与未用导入**：修改导致未使用的导入或变量时，必须删除它们，保持模块整洁。
- **基础设施**：除非特别指示，否则不要修改 CI/CD 管道 (`.github/workflows`) 或部署配置 (`Dockerfile` / `docker-compose.yml`) 的核心行为。

## 验证与完成 (Verification & Completion)
在完成任务前：
1. **测试**：必须运行 `pytest` 并确保 0 个失败。
2. **覆盖率**：新增功能必须被新测试覆盖；测试须相互独立、不依赖运行环境（如真实 `.env`）。
3. **可追溯性**：每一行更改都必须能直接追溯到用户的请求。严禁顺手牵羊式的重构 (No drive-by refactoring)。
4. **架构验证**：在合并或完成任务前，必须验证代码更改是否符合技术栈约定与分层架构（核心逻辑与 Web/CLI 层保持解耦）。绝不引入破坏现有架构模式的跨层依赖。

## 子智能体目录 (Subagents Directory)
本 Cursor 环境提供以下通用子智能体 (Subagents) 以协助不同类型的任务，请根据任务类型主动唤起：

- **@generalPurpose**：通用智能体。用于研究复杂问题、搜索代码或执行多步任务。当需要搜索关键字或文件但不确定能否快速定位时使用。
- **@explore**：只读代码库探索智能体。速度快，用于按模式查找文件、搜索关键字或了解代码结构机制。
- **@shell**：命令行执行专家。用于 Git 操作、命令执行及其他终端任务。
- **@cursor-guide**：Cursor 产品文档向导。了解 Cursor Desktop、IDE、CLI 机制或功能使用问题时调用。
- **@code-reviewer**：代码审查专家。在完成一个主要步骤后，对照计划与规范审查实现。
- **@best-of-n-runner**：在隔离的 Git 工作树中运行任务，用于并行实现尝试或隔离实验。

> 注：本项目未定义专属角色型子智能体（如需求分析、UI 设计、领域代码审查等）；如未来引入，请在此登记后再按名调用，避免调用不存在的角色。

## 技能目录 (Skills Directory)
本工作区为你提供以下与本项目相关的技能 (Skills)，遇到对应场景时主动调用（Superpowers 系列通过 Skill/Read 机制加载并严格执行）：

- **brainstorming**：任何创造性工作（新功能、修改行为）之前调用，先把想法澄清成设计。
- **writing-plans**：拿到 spec/需求后、动代码前调用，产出可逐条执行的实施计划。
- **executing-plans / subagent-driven-development**：按计划逐任务执行(内联或子智能体驱动)。
- **test-driven-development**：实现功能或修 Bug **之前**调用，强制 TDD。
- **systematic-debugging**：遇到 Bug、测试失败或异常行为时，先系统排查再提修复。
- **writing-skills**：创建/编辑/验证技能时调用。

> 注：仅登记本环境确实可用的技能；请勿引用未提供的技能。
