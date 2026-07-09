# browser-use 参考

## 命令一览

| 命令 | 作用 | 输出 |
|---|---|---|
| `open "<url>"` | 导航并等待 `document.readyState=complete`(超时 20s) | JSON:`{title, url}` |
| `info` | 当前页标题与地址 | JSON:`{title, url}` |
| `text [--max-chars N]` | 当前页可见正文(`document.body.innerText`,默认截断 5000) | 纯文本 |
| `js "<expr>"` | 在页面上下文执行 JS 并按值返回(await Promise) | JSON 值 |
| `shot <path.png>` | 可视区域截图 | 写文件,打印保存路径 |
| `click <x> <y>` | 在坐标处左键单击 | 确认信息 |

均以 `.venv-browseruse/Scripts/python.exe scripts/browse.py <cmd> ...` 运行,在仓库根执行。

## 工作原理

- `scripts/browse.py` 用 `cdp_use.CDPClient`(browser-use 的 CDP 依赖)连接 Chrome 的 page target
  websocket(取自 `http://127.0.0.1:9222/json`),发 `Page.*` / `Runtime.*` / `Input.*` CDP 命令。
- 无状态:每次调用连接→执行→关闭,进程干净退出(规避 `bu` CLI 在 Windows 常驻不退的问题)。
- 输出强制 UTF-8:页面标题/正文可能含 emoji 或非 GBK 字符,已 `reconfigure(encoding="utf-8")`。

## 为什么不用 `bu` CLI

`bu`(browser-harness)在 Windows 下:执行完 piped 代码后前台进程不自行退出(常驻线程/守护),
即便在代码尾追加 `os._exit(0)` 也无效(exec 在被转发的上下文)。因此改用直接 CDP 封装。
`bu` 及其安装仍保留在 `.venv-browseruse`,如需官方 CLI 可另行探索。

## 排障

- **连不上 CDP / 命令报错指向 9222**:Chrome 未启动或未开远程调试 →
  `powershell -File scripts/browser_chrome.ps1`;确认 `http://127.0.0.1:9222/json/version` 返回 200。
- **没有可用标签页**:先 `open <url>`(会在无标签页时新建)。
- **截图为空**:确认页面已加载(先 `open` 或 `info`)。
- **中文/emoji 乱码**:脚本已强制 UTF-8 输出;若从别处管道读取,确保读取端也按 UTF-8。

## 依赖与位置

- venv:`.venv-browseruse/`(Python 3.12;已 gitignore)。
- browser-use 源码:`thrid-party/browser-use/`(已 gitignore;origin `github.com/browser-use/browser-use`)。
- Chrome 专用 profile:`.browser-use-profile/`(已 gitignore)。
