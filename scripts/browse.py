#!/usr/bin/env python3
"""由 Cursor 驱动的浏览器封装:经 CDP 控制已运行的 Chrome,进程干净退出。

背景:browser-use 的 `bu` CLI 在 Windows 下执行完不自退(harness 常驻),
故按 spec §9 回退到"库封装"——直接用 browser-use 依赖的 `cdp_use.CDPClient`
连接 scripts/browser_chrome.ps1 启动的 Chrome(CDP:9222),做无状态一次性操作。

必须用专用 venv 运行:.venv-browseruse\\Scripts\\python.exe scripts/browse.py <cmd> ...
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys

import requests
from cdp_use import CDPClient

CDP_HTTP = "http://127.0.0.1:9222"

# Windows 控制台默认 GBK,页面标题/文本可能含 emoji 或非 GBK 字符;强制 UTF-8 输出避免 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _list_pages() -> list:
    """列出 Chrome 的 page 类型 target;连不上则给出可操作的报错。"""
    try:
        r = requests.get(f"{CDP_HTTP}/json", timeout=10)
        r.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"[X] 连不上 Chrome CDP({CDP_HTTP}):{exc}\n    先运行:powershell -File scripts/browser_chrome.ps1")
    return [t for t in r.json() if t.get("type") == "page"]


def _page_ws(create_url: str | None = None) -> str:
    """取一个可用 page 的 webSocketDebuggerUrl;没有则新建一个标签页。"""
    pages = _list_pages()
    if not pages and create_url is not None:
        r = requests.put(f"{CDP_HTTP}/json/new?{create_url}", timeout=10)
        r.raise_for_status()
        return r.json()["webSocketDebuggerUrl"]
    if not pages:
        sys.exit("[X] 没有可用的浏览器标签页;请先 open 一个 URL。")
    return pages[0]["webSocketDebuggerUrl"]


async def _eval(client: CDPClient, expression: str) -> dict:
    """在页面上下文执行 JS 并按值返回(await Promise)。"""
    return await client.send_raw(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )


async def _wait_load(client: CDPClient, timeout_s: float = 20.0) -> None:
    """轮询 document.readyState 直到 complete 或超时(避免依赖事件时序)。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        res = await _eval(client, "document.readyState")
        if (res.get("result") or {}).get("value") == "complete":
            return
        await asyncio.sleep(0.3)


async def cmd_open(args) -> None:
    ws = _page_ws(create_url=args.url)
    async with CDPClient(ws) as client:
        await client.send_raw("Page.enable")
        await client.send_raw("Page.navigate", {"url": args.url})
        await _wait_load(client)
        res = await _eval(client, "JSON.stringify({title: document.title, url: location.href})")
        print((res.get("result") or {}).get("value", "{}"))


async def cmd_info(args) -> None:
    async with CDPClient(_page_ws()) as client:
        res = await _eval(client, "JSON.stringify({title: document.title, url: location.href})")
        print((res.get("result") or {}).get("value", "{}"))


async def cmd_text(args) -> None:
    async with CDPClient(_page_ws()) as client:
        res = await _eval(client, "document.body ? document.body.innerText : ''")
        text = (res.get("result") or {}).get("value", "") or ""
        print(text[: args.max_chars])


async def cmd_js(args) -> None:
    async with CDPClient(_page_ws()) as client:
        res = await _eval(client, args.expression)
        result = res.get("result") or {}
        print(json.dumps(result.get("value", result.get("description")), ensure_ascii=False))


async def cmd_shot(args) -> None:
    async with CDPClient(_page_ws()) as client:
        await client.send_raw("Page.enable")
        res = await client.send_raw("Page.captureScreenshot", {"format": "png"})
        data = res.get("data")
        if not data:
            sys.exit("[X] 截图失败:无数据")
        with open(args.path, "wb") as f:
            f.write(base64.b64decode(data))
        print(f"[OK] 截图已保存:{args.path}")


async def cmd_click(args) -> None:
    async with CDPClient(_page_ws()) as client:
        for event_type in ("mousePressed", "mouseReleased"):
            await client.send_raw(
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": args.x, "y": args.y, "button": "left", "clickCount": 1},
            )
        print(f"[OK] 已点击 ({args.x}, {args.y})")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cursor 驱动的 CDP 浏览器封装(连接 127.0.0.1:9222)")
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("open", help="导航到 URL 并等待加载")
    po.add_argument("url")
    po.set_defaults(func=cmd_open)

    sub.add_parser("info", help="打印当前页 title/url").set_defaults(func=cmd_info)

    pt = sub.add_parser("text", help="提取当前页可见文本")
    pt.add_argument("--max-chars", type=int, default=5000)
    pt.set_defaults(func=cmd_text)

    pj = sub.add_parser("js", help="在页面执行 JS 并按值返回")
    pj.add_argument("expression")
    pj.set_defaults(func=cmd_js)

    ps = sub.add_parser("shot", help="整页可视区域截图为 PNG")
    ps.add_argument("path")
    ps.set_defaults(func=cmd_shot)

    pc = sub.add_parser("click", help="在坐标处左键单击")
    pc.add_argument("x", type=int)
    pc.add_argument("y", type=int)
    pc.set_defaults(func=cmd_click)
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    asyncio.run(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
