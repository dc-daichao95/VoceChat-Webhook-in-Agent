# browser-use Skill 接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把 browser-use 以形态 A(`bu` CLI)接为 Cursor 可驱动的浏览器 Skill,不引入额外 LLM Key。

**Architecture:** 专用 uv Python 3.12 venv 装本地 `thrid-party/browser-use`;PowerShell 启动器拉起带 CDP 的专用 Chrome;PowerShell shim 把 Python 喂给 `bu`;项目级 Cursor Skill 教我如何使用。

**Tech Stack:** uv、Python 3.12、browser-use(`bu` CLI)、Chrome CDP、PowerShell。

关联 spec:`docs/superpowers/specs/2026-07-09-browser-use-skill-design.md`

---

### Task 1: 仓库忽略规则

**Files:** Modify `.gitignore`

- [ ] **Step 1:** 在 `.gitignore` 追加:
```
# browser-use skill (local third-party + dedicated env, not committed)
thrid-party/
.venv-browseruse/
.browser-use-profile/
```
- [ ] **Step 2:** 验证忽略生效
Run: `git check-ignore thrid-party .venv-browseruse .browser-use-profile`
Expected: 三行都回显(表示被忽略)。
- [ ] **Step 3:** Commit
```
git add .gitignore
git commit -m "chore: ignore browser-use local env (third-party, venv, profile)"
```

---

### Task 2: 专用 venv + 安装 browser-use

**Files:** 无源码改动(生成 `.venv-browseruse/`)

- [ ] **Step 1:** 建 venv
Run: `uv venv .venv-browseruse --python 3.12`
Expected: 生成 `.venv-browseruse\Scripts\`。
- [ ] **Step 2:** 安装本地 browser-use(可编辑)
Run: `uv pip install --python .venv-browseruse -e "./thrid-party/browser-use"`
Expected: 成功;若报缺 Windows core,追加 `uv pip install --python .venv-browseruse "browser-use[core]"`。
- [ ] **Step 3:** 校验 CLI
Run: `.venv-browseruse\Scripts\bu.exe --help`
Expected: 打印 CLI 用法(入口 `browser_use.cli:main`)。
- [ ] **Step 4:** 若 Step 2/3 在 Windows 失败且无法快速修复 → **停止并触发回退**(见 spec §9:改用库封装 `scripts/browse.py`),回到本计划前与用户确认。

---

### Task 3: Chrome 启动器 + bu 调用 shim

**Files:** Create `scripts/browser_chrome.ps1`、`scripts/bu_run.ps1`

- [ ] **Step 1:** `scripts/browser_chrome.ps1`
```powershell
# 以 CDP 远程调试启动专用 Chrome(独立 profile,不碰日常登录态)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { throw "未找到 Chrome:$chrome" }
$profile = Join-Path $repo ".browser-use-profile"
Start-Process $chrome -ArgumentList @("--remote-debugging-port=9222", "--user-data-dir=$profile")
Write-Output "[OK] Chrome 已启动,CDP: http://127.0.0.1:9222 profile: $profile"
```
- [ ] **Step 2:** `scripts/bu_run.ps1`（把 Python 从文件或 stdin 喂给 bu）
```powershell
# 用法: scripts/bu_run.ps1 -File task.py   或   "print(page_info())" | scripts/bu_run.ps1
param([string]$File)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$bu = Join-Path $repo ".venv-browseruse\Scripts\bu.exe"
if (-not (Test-Path $bu)) { throw "未找到 bu:$bu(先跑 Task 2 安装)" }
if ($File) { Get-Content -Raw $File | & $bu } else { $input | & $bu }
```
- [ ] **Step 3:** Commit
```
git add scripts/browser_chrome.ps1 scripts/bu_run.ps1
git commit -m "feat: add Chrome CDP launcher and bu stdin shim for Windows"
```

---

### Task 4: 冒烟验证(证明 Windows 可跑)

**Files:** 临时 `build/_bu_smoke.py`(用后删)

- [ ] **Step 1:** 启动 Chrome
Run: `powershell -File scripts/browser_chrome.ps1`
Expected: `[OK] Chrome 已启动`。
- [ ] **Step 2:** 写冒烟脚本 `build/_bu_smoke.py`
```python
new_tab("https://example.com")
wait_for_load()
print(page_info())
```
- [ ] **Step 3:** 执行
Run: `powershell -File scripts/bu_run.ps1 -File build/_bu_smoke.py`
Expected: 输出含 `example.com` / "Example Domain" 的页面信息。
- [ ] **Step 4:** 清理临时脚本
Run: `Remove-Item build/_bu_smoke.py`
- [ ] **Step 5:** 若冒烟失败 → 停止,触发 spec §9 回退并与用户确认;不要硬凑。

---

### Task 5: 编写并关联 Cursor Skill

**Files:** Create `.cursor/skills/browser-use/SKILL.md`、`reference.md`、`examples.md`

- [ ] **Step 1:** `SKILL.md`(frontmatter + 正文;省略 `disable-model-invocation` 以支持自动加载)
  - frontmatter:`name: browser-use`;第三人称 `description`,含触发词(浏览器/网页/截图/抓取/自动化/browse/scrape/screenshot)。
  - 正文分节:前置检查(venv/Chrome/daemon)、启动方式(`browser_chrome.ps1` + `bu_run.ps1`,Windows 用管道/文件而非 heredoc)、核心操作循环(截图→读像素→`click_at_xy`→再截图;导航后 `wait_for_load()`;`ensure_real_tab()`)、`/loop` 集成一句、安全规则(登录/支付先征询;专用 profile)、指向 `reference.md` / `examples.md`。
- [ ] **Step 2:** `reference.md`:连接模型、常见交互技巧清单、gotchas(摘自 `thrid-party/browser-use/skills/browser-use/SKILL.md` 的对应段,一层深)。
- [ ] **Step 3:** `examples.md`:2-3 个具体调用示例(打开页面并读标题、截图并点击、用 `js(...)` 抓取)。
- [ ] **Step 4:** 校验:`SKILL.md` < 500 行;引用一层深;`description` 含 WHAT+WHEN;无 Windows 反斜杠路径写进 skill 正文的相对路径处(用 `scripts/...`)。
- [ ] **Step 5:** Commit
```
git add .cursor/skills/browser-use
git commit -m "feat: add browser-use Cursor skill (Cursor-driven browser, no extra LLM key)"
```

---

### Task 6: 收尾

- [ ] **Step 1:** 提交 spec/plan 文档(若未提交)。
- [ ] **Step 2:** 用 finishing-a-development-branch:呈现合并/PR/保留/丢弃四选项。

---

## Self-Review

**Spec coverage:** §3 目录/忽略→Task 1;§4.2 安装→Task 2;§4.3/4.4 启动器+shim→Task 3;§7 冒烟→Task 4;§4.5 关联 skill→Task 5;§9 回退→Task 2/4 的失败分支。

**Placeholder scan:** 无 TBD;命令具体;临时文件即用即删。

**一致性:** venv 路径 `.venv-browseruse`、`bu.exe`、端口 9222、profile `.browser-use-profile` 全计划一致。

**风险:** Windows CLI 可跑性在 Task 2/4 设了硬验证门与回退,不硬凑。
