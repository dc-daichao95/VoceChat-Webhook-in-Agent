# 设计:把 browser-use 接为「Cursor 可驱动的浏览器 Skill」(形态 A)

- 日期:2026-07-09
- 状态:待评审
- 关联:`thrid-party/browser-use`(browser-use 本地克隆)、`Agent.md`

## 1. 背景与目标

让 **Cursor 会话(我,即本项目的"大脑")** 具备直接驱动浏览器的能力:开页、截图、点击、抓取、执行 JS。采用 browser-use 的**形态 A(Agent Skill / CLI,`bu`/`browser-use`)**——由我决策与操作,**不引入任何额外 LLM API Key、不产生额外调用费用**。该能力作为工作区可用的 Cursor Skill,并可在 `/loop` 中用于"需要上网才能回答"的 VoceChat 消息。

**不做**:不接入 browser-use 的自主 `Agent`(那需外部 LLM Key);不使用 cloud 浏览器(需 Key);不改动 receiver/brain 现有运行时逻辑。

## 2. 关键前置事实(已核实)

- `thrid-party/browser-use`:origin `https://github.com/browser-use/browser-use.git`,版本约 `0.13.4`;是一个**嵌套 git 仓库**,当前**未被外层仓库跟踪也未被忽略**。
- CLI 入口由该包提供:`browser-use = browser_use.cli:main`,别名 `bu` / `browser`;并含 `win32/AMD64` 的 `browser-use-core` 可选轮子。
- 本机:**Python 3.12 已装**(`py -V:3.12`;uv 亦自带 3.12.13),`uv 0.11.23` 可用,**Chrome 已装** `C:\Program Files\Google\Chrome\Application\chrome.exe`。默认解释器是 3.8,**不可**用于 browser-use(要求 ≥3.11)。
- 环境:Windows Server + RDP、无独立 GPU;PowerShell **无 heredoc**。

## 3. 目录与产物

```
AnsweringMachine/
├─ thrid-party/browser-use/         # browser-use 本地克隆(外层仓库忽略)
├─ .venv-browseruse/                # 专用 uv venv(Python 3.12;外层仓库忽略)
├─ .browser-use-profile/            # 专用 Chrome 用户数据目录(忽略;避免碰日常登录态)
├─ scripts/
│  ├─ browser_chrome.ps1            # 以 CDP 远程调试端口启动专用 Chrome
│  └─ bu_run.ps1                    # Windows 调用 shim:把 Python 喂给 bu 的 stdin
└─ .cursor/skills/browser-use/      # Cursor 项目级 Skill(随仓库共享)
   ├─ SKILL.md                      # 主说明(何时/如何用,操作循环,安全)
   ├─ reference.md                  # 细节:连接模型、交互技巧、gotchas(摘上游)
   └─ examples.md                   # 具体调用示例
```

> `.gitignore` 追加:`thrid-party/`、`.venv-browseruse/`、`.browser-use-profile/`。`.cursor/skills/browser-use/` **纳入**版本库(它是我们编写的技能)。

## 4. 完整流程(下载 → 安装 → 关联 skill)

### 4.1 下载 / 更新 third-party
- browser-use 已在 `thrid-party/browser-use`。流程仍记录其获取方式,便于换机/更新:
  - 首次获取:`git clone https://github.com/browser-use/browser-use.git thrid-party/browser-use`
  - 更新到最新稳定:在该子目录 `git fetch --tags && git checkout <tag>`(如 `0.13.4`)。
- 因它自带 `.git` 且体量大,外层仓库**忽略** `thrid-party/`(见 §3),不将其源码提交进 AnsweringMachine。

### 4.2 安装到专用 venv
- 建 venv:`uv venv .venv-browseruse --python 3.12`
- 安装(本地可编辑,带 Windows core):`uv pip install --python .venv-browseruse -e "./thrid-party/browser-use"`
  - 若缺 Windows core 依赖,补装:`uv pip install --python .venv-browseruse "browser-use[core]"`(或本地 extra)。
- 校验 CLI 就位:`.venv-browseruse\Scripts\bu.exe --help`(存在即成功)。
- 浏览器后端:优先复用已装 Chrome;如 browser-harness 需要自带浏览器,按其 `--doctor` 提示补齐。

### 4.3 启动专用 Chrome(CDP)
- `scripts/browser_chrome.ps1` 以固定端口 + 专用 profile 启动:
  - `& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="<repo>\.browser-use-profile"`
- 用专用 profile 的原因:**不复用**你 RDP 里日常登录的 Chrome 会话,避免误操作已登录账号(安全)。

### 4.4 Windows 调用 shim
- 官方用法是 bash heredoc:`bu <<'PY' ... PY`。PowerShell 无 heredoc,改为二选一(实现时实测定稿并写进 skill):
  - 管道喂 stdin:`Get-Content task.py -Raw | .venv-browseruse\Scripts\bu.exe`
  - 或临时文件参数:`.venv-browseruse\Scripts\bu.exe task.py`(若 CLI 接受文件参数)
- `scripts/bu_run.ps1` 封装上述细节:接收一段/一个文件的 Python,负责 `ensure_daemon` 由 CLI 处理,返回 stdout。

### 4.5 关联为 Cursor Skill
- 位置:**项目级** `.cursor/skills/browser-use/`(venv、profile、启动器均为本仓库相关;若需跨项目,改放 `~/.cursor/skills/browser-use/`)。
- `SKILL.md` frontmatter:`name: browser-use`;`description` 用第三人称并含触发词(浏览器/网页/截图/抓取/自动化/browse);**省略 `disable-model-invocation`**,以便"大脑自行判断需要上网时"自动加载(对应先前选定的 brain_auto)。
- 正文覆盖:前置检查(venv/Chrome/daemon)、Windows 调用方式、核心操作循环(截图→读像素→`click_at_xy`→再截图;导航后 `wait_for_load()`)、安全规则;细节以渐进式披露链到 `reference.md` / `examples.md`(一层深)。

## 5. 数据流

我(大脑)→ `bu_run.ps1` 调 `bu` 执行 Python → browser-harness daemon(`ensure_daemon`)→ Chrome(CDP:9222)→ 返回文本/截图 → 我据此决策或回答。`/loop` 场景:某条待回复消息需上网 → 触发本 skill 取信息 → 用 `send.py` 发回。

## 6. 错误处理

- daemon 连不上:`bu --doctor`;必要时重跑 `browser_chrome.ps1` 确保带 `--remote-debugging-port`。
- Chrome 未开远程调试:由启动器参数保证;若手动开的 Chrome,提示启用 `chrome://inspect/#remote-debugging`。
- 登录墙 / 支付 / 表单提交等敏感动作:**停下并征询用户**;已登录的 SSO 可自动用,但密码/MFA/账号选择必须停。
- Python 版本错用(3.8):所有调用固定走 `.venv-browseruse\Scripts\bu.exe`,不用系统 `python`。

## 7. 验证(冒烟)

1. `.venv-browseruse\Scripts\bu.exe --help` 正常。
2. 启动 `browser_chrome.ps1`。
3. 经 `bu_run.ps1` 执行:`new_tab("https://example.com"); print(page_info())` → 返回含 example.com 标题的信息即通过。
4. 截图:`capture_screenshot()` 能产出图片文件。
- 全程在 Windows 实测;若 CLI 在 Windows 无法运行,触发回退(§9)。

## 8. 安全与边界

- 专用 Chrome profile;CDP 仅本地(9222);不使用 cloud(无 Key)。
- 敏感操作先征询(见 §6)。
- 边界:不改 receiver/brain 运行时;不把 `thrid-party/` 源码提交进本仓库;不引入外部 LLM Key。

## 9. 风险与回退

- **主风险**:browser-use CLI(browser-harness)在 Windows 的可运行性未经安装实测。
- **回退**:若形态 A 的 CLI 在 Windows 跑不通,则退到"方案 2"——用 browser-use 库原语/CDP 写一个我驱动的薄封装 `scripts/browse.py`(`open/shot/click/extract` 子命令),skill 改为调用它。回退不改变对用户的接口语义(仍是"我驱动浏览器、无 Key")。
