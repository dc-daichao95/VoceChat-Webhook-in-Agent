# browser-use 示例

所有命令在仓库根运行;`$py = .venv-browseruse/Scripts/python.exe`。先确保 Chrome 已启动
(`powershell -File scripts/browser_chrome.ps1`)。

## 示例 1:打开页面并读标题

```
.venv-browseruse/Scripts/python.exe scripts/browse.py open "https://example.com"
```
输出:`{"title":"Example Domain","url":"https://example.com/"}`

## 示例 2:提取正文用于回答

```
.venv-browseruse/Scripts/python.exe scripts/browse.py open "https://news.ycombinator.com"
.venv-browseruse/Scripts/python.exe scripts/browse.py text --max-chars 2000
```
拿到正文后据此总结/回答;若在 /loop 中,用 `python send.py --target-uid <uid> --text -` 发回。

## 示例 3:定向 DOM 提取(js)

```
.venv-browseruse/Scripts/python.exe scripts/browse.py js "Array.from(document.querySelectorAll('.titleline a')).slice(0,5).map(a=>a.textContent)"
```
返回前 5 条标题的 JSON 数组。

## 示例 4:截图 → 读图 → 点击 → 复核

```
.venv-browseruse/Scripts/python.exe scripts/browse.py shot build/page.png
# 读取 build/page.png,确定目标像素坐标 (x, y)
.venv-browseruse/Scripts/python.exe scripts/browse.py click 480 300
.venv-browseruse/Scripts/python.exe scripts/browse.py shot build/after.png
```
点击后再截图确认页面变化。
