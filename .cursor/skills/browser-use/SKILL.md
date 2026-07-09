---
name: browser-use
description: Drive a real Chrome browser from Cursor via CDP — navigate pages, screenshot, extract page text, run JS, and click by coordinates. Use when a task needs live web access: opening or scraping a website, taking a screenshot of a page, checking online content, or answering a question that requires up-to-date web info. Runs locally on Windows with no extra LLM/API key.
---

# browser-use (Cursor 驱动的浏览器)

由 Cursor 直接经 CDP 控制本地 Chrome:导航、截图、提取文本、执行 JS、按坐标点击。
**不使用自主 agent、不需要额外 LLM/API Key。** 环境:Windows + PowerShell,命令均在仓库根运行。

> 采用"库封装"(`scripts/browse.py`)而非 browser-use 的 `bu` CLI:后者在 Windows 下执行完进程不自退。原委见 reference.md。

## 前置检查

1. 专用 venv 已装 browser-use(一次性):
   `uv venv .venv-browseruse --python 3.12`
   `uv pip install --python .venv-browseruse -e ./thrid-party/browser-use`
2. 启动专用 Chrome(CDP:9222,独立 profile,不碰日常登录态):
   `powershell -File scripts/browser_chrome.ps1`
   自检:`http://127.0.0.1:9222/json/version` 返回 200 即就绪。

## 用法

用专用 venv 的 Python 调用;每条命令连接 Chrome、执行后**干净退出**:

```
.venv-browseruse/Scripts/python.exe scripts/browse.py open "<url>"
.venv-browseruse/Scripts/python.exe scripts/browse.py info
.venv-browseruse/Scripts/python.exe scripts/browse.py text [--max-chars N]
.venv-browseruse/Scripts/python.exe scripts/browse.py js "<expression>"
.venv-browseruse/Scripts/python.exe scripts/browse.py shot <path.png>
.venv-browseruse/Scripts/python.exe scripts/browse.py click <x> <y>
```

## 操作循环

- 导航:`open <url>`(等待加载完成,打印 title/url)。
- 理解页面:`shot out.png` 后读取该图片;或 `text` 取可读正文;或 `js` 做定向 DOM 提取。
- 点击:先 `shot` → 读像素坐标 → `click x y` → 再 `shot` 确认结果。

## /loop 集成

当 `/loop` 拉到的 VoceChat 消息需要"上网"才能回答时,用上述命令取信息,再用 `send.py` 发回。

## 安全

- 专用 Chrome profile(`.browser-use-profile/`),非你的日常登录态。
- 登录、支付、表单提交等敏感动作:**停下并征询用户**。
- CDP 仅本地监听(127.0.0.1:9222)。

## 更多

- 命令参考与排障:reference.md
- 具体示例:examples.md
