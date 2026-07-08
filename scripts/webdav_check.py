#!/usr/bin/env python3
"""fnOS WebDAV 连通性 + 轮询成本探测脚本。

用途:在正式接入前,验证本机能否经 WebDAV 列目录 / 下载 / 上传 / 删除,
并实测"频繁轮询"的网络与存储开销,确认不会导致异常。

读取仓库根目录的 share.env(key=value):
    url=https://<nas-host>:<port>
    user=<fnOS 系统用户>
    passwd=<密码>

不打印密码;对自签名证书默认跳过校验(--verify 可开启严格校验)。

示例:
    python scripts/webdav_check.py                 # 列目录 + 下载首个文件 + 上传/删除测试
    python scripts/webdav_check.py --path /webhook_share/
    python scripts/webdav_check.py --no-write       # 只读,不做上传/删除
    python scripts/webdav_check.py --bench 20        # 连测 20 次 PROPFIND,给出轮询成本估算
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    sys.exit("缺少依赖 requests:请先 `pip install requests`")

import urllib3

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = REPO_ROOT / "share.env"
DAV = "{DAV:}"


def load_config(env_path: Path) -> dict:
    if not env_path.exists():
        sys.exit(f"找不到配置文件:{env_path}")
    cfg: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        cfg[key.strip()] = val.strip()
    for required in ("url", "user", "passwd"):
        if not cfg.get(required):
            sys.exit(f"share.env 缺少必填项:{required}")
    return cfg


class WebDAV:
    def __init__(self, base_url: str, user: str, passwd: str, verify: bool):
        self.base = base_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, passwd)
        self.session.verify = verify
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _url(self, path: str) -> str:
        return urljoin(self.base, path.lstrip("/"))

    def propfind(self, path: str = "/", depth: str = "1", timeout: float = 15) -> requests.Response:
        return self.session.request(
            "PROPFIND", self._url(path), headers={"Depth": depth}, timeout=timeout
        )

    def get(self, path: str, etag: str | None = None, timeout: float = 30) -> requests.Response:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        return self.session.get(self._url(path), headers=headers, timeout=timeout)

    def put(self, path: str, data: bytes, timeout: float = 30) -> requests.Response:
        return self.session.put(self._url(path), data=data, timeout=timeout)

    def delete(self, path: str, timeout: float = 15) -> requests.Response:
        return self.session.delete(self._url(path), timeout=timeout)


def parse_propfind(xml_text: str, base_path: str) -> list[dict]:
    entries: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries
    for resp in root.findall(f"{DAV}response"):
        href_el = resp.find(f"{DAV}href")
        if href_el is None or href_el.text is None:
            continue
        href = unquote(href_el.text)
        propstat = resp.find(f"{DAV}propstat")
        prop = propstat.find(f"{DAV}prop") if propstat is not None else None
        is_dir = False
        size = mtime = etag = ""
        if prop is not None:
            rtype = prop.find(f"{DAV}resourcetype")
            is_dir = rtype is not None and rtype.find(f"{DAV}collection") is not None
            for tag, key in (
                ("getcontentlength", "size"),
                ("getlastmodified", "mtime"),
                ("getetag", "etag"),
            ):
                el = prop.find(f"{DAV}{tag}")
                if el is not None and el.text:
                    val = el.text
                    if key == "size":
                        size = val
                    elif key == "mtime":
                        mtime = val
                    else:
                        etag = val
        name = href.rstrip("/").split("/")[-1]
        # 跳过目录自身条目
        if href.rstrip("/").endswith(base_path.rstrip("/")) and not name:
            continue
        entries.append(
            {"href": href, "name": name, "is_dir": is_dir, "size": size, "mtime": mtime, "etag": etag}
        )
    return entries


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def cmd_check(dav: WebDAV, path: str, do_write: bool, keep: bool) -> int:
    print(f"目标: {dav.base}  路径: {path}")
    print("=" * 60)

    # 1) PROPFIND 列目录
    t0 = time.perf_counter()
    r = dav.propfind(path, depth="1")
    dt = (time.perf_counter() - t0) * 1000
    print(f"[1] PROPFIND {path} -> HTTP {r.status_code}  ({dt:.0f} ms, {human(len(r.content))})")
    if r.status_code == 401:
        print("    [X] 401 未授权:share.env 的账号/密码被 fnOS 拒绝,或该账号无此目录权限。")
        return 2
    if r.status_code not in (207, 200):
        print(f"    [X] 非预期状态。响应片段: {r.text[:200]!r}")
        return 3
    entries = [e for e in parse_propfind(r.text, path) if e["name"]]
    files = [e for e in entries if not e["is_dir"]]
    dirs = [e for e in entries if e["is_dir"]]
    print(f"    [OK] 列出 {len(dirs)} 个目录, {len(files)} 个文件:")
    for e in entries[:20]:
        kind = "DIR " if e["is_dir"] else "FILE"
        sz = human(int(e["size"])) if e["size"].isdigit() else "-"
        print(f"      {kind} {e['name']:<32} {sz:>8}  etag={e['etag'] or '-'}")

    # 2) 下载首个文件 + 条件 GET 演示
    if files:
        target = files[0]
        rel = urlparse(target["href"]).path
        t0 = time.perf_counter()
        g = dav.get(rel)
        dt = (time.perf_counter() - t0) * 1000
        print(f"[2] GET {target['name']} -> HTTP {g.status_code}  ({dt:.0f} ms, {human(len(g.content))})")
        if g.ok:
            preview = g.content[:200].decode("utf-8", "replace").replace("\n", "\\n")
            print(f"    [OK] 内容预览: {preview!r}")
            etag = g.headers.get("ETag")
            if etag:
                g2 = dav.get(rel, etag=etag)
                print(
                    f"    条件 GET(If-None-Match)-> HTTP {g2.status_code} "
                    f"({'[OK] 304 未改动,零传输' if g2.status_code == 304 else '服务器不支持 304, 每次全量下载'})"
                )
    else:
        print("[2] 目录暂无文件可下载(你可先上传一个测试文件)。")

    # 3) 上传 + 删除
    if do_write:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        test_name = f"webdav_check_{ts}.txt"
        rel = path.rstrip("/") + "/" + test_name
        body = f"answeringmachine webdav check {ts}\n".encode()
        p = dav.put(rel, body)
        print(f"[3] PUT {test_name} -> HTTP {p.status_code}  ({'[OK] 写入成功' if p.status_code in (200,201,204) else '[X] 写入失败/无权限'})")
        if p.status_code in (200, 201, 204) and not keep:
            d = dav.delete(rel)
            print(f"    DELETE {test_name} -> HTTP {d.status_code}  ({'[OK] 已清理' if d.status_code in (200,204) else '[X] 删除失败,请手动清理'})")
    else:
        print("[3] --no-write:跳过上传/删除测试。")
    return 0


def cmd_roundtrip(dav: WebDAV, path: str) -> int:
    """上传 -> 读回校验 -> 删除,一次证明 PUT+GET+DELETE 往返。"""
    print(f"往返测试(PUT -> GET -> DELETE)于 {dav.base}{path.lstrip('/')}")
    print("=" * 60)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    name = f"roundtrip_{ts}.json"
    rel = path.rstrip("/") + "/" + name
    payload = f'{{"probe":"answeringmachine","ts":"{ts}"}}'.encode()

    p = dav.put(rel, payload)
    print(f"[PUT]    {name} -> HTTP {p.status_code}  {'[OK]' if p.status_code in (200,201,204) else '[X]'}")
    if p.status_code not in (200, 201, 204):
        return 3

    g = dav.get(rel)
    ok = g.ok and g.content == payload
    print(f"[GET]    {name} -> HTTP {g.status_code}  ({human(len(g.content))})  "
          f"{'[OK] 字节一致' if ok else '[X] 内容不一致或下载失败'}")

    d = dav.delete(rel)
    print(f"[DELETE] {name} -> HTTP {d.status_code}  {'[OK] 已清理' if d.status_code in (200,204) else '[X] 请手动清理'}")
    return 0 if ok else 4


def cmd_bench(dav: WebDAV, path: str, n: int) -> int:
    print(f"轮询成本探测: 连续 {n} 次 PROPFIND {path}")
    print("=" * 60)
    lat, total = [], 0
    first_status = None
    for i in range(n):
        t0 = time.perf_counter()
        r = dav.propfind(path, depth="1")
        lat.append((time.perf_counter() - t0) * 1000)
        total += len(r.content)
        first_status = first_status or r.status_code
        if r.status_code == 401:
            print("    [X] 401:凭证问题,先修好再测。")
            return 2
        time.sleep(0.2)
    lat.sort()
    avg = sum(lat) / len(lat)
    p50 = lat[len(lat) // 2]
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    per_poll = total / n
    print(f"    状态: HTTP {first_status}")
    print(f"    延迟: avg {avg:.0f}ms  p50 {p50:.0f}ms  p95 {p95:.0f}ms")
    print(f"    单次 PROPFIND 响应: {human(int(per_poll))}")
    for interval in (30, 60):
        per_day = 86400 / interval
        print(
            f"    若每 {interval}s 轮询一次 -> {per_day:.0f} 次/天, "
            f"约 {human(int(per_poll * per_day))}/天流量(仅列目录, 不含实际下载)"
        )
    print("    说明: 实际下载用条件 GET(If-None-Match), 文件未变则 304 零传输;")
    print("          稳态下几乎只有 PROPFIND 的开销, 对网络/存储压力极小。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="fnOS WebDAV 连通性 + 轮询成本探测")
    ap.add_argument("--env", default=str(DEFAULT_ENV), help="share.env 路径")
    ap.add_argument("--path", default="/", help="要操作的 WebDAV 路径(默认根 /)")
    ap.add_argument("--no-write", action="store_true", help="只读,跳过 PUT/DELETE")
    ap.add_argument("--keep", action="store_true", help="保留上传的测试文件(不删除)")
    ap.add_argument("--verify", action="store_true", help="严格校验 TLS 证书(默认跳过自签名)")
    ap.add_argument("--bench", type=int, metavar="N", help="连测 N 次 PROPFIND 估算轮询成本")
    ap.add_argument("--roundtrip", action="store_true", help="PUT->GET->DELETE 往返测试")
    args = ap.parse_args()

    cfg = load_config(Path(args.env))
    dav = WebDAV(cfg["url"], cfg["user"], cfg["passwd"], verify=args.verify)

    if args.bench:
        return cmd_bench(dav, args.path, args.bench)
    if args.roundtrip:
        return cmd_roundtrip(dav, args.path)
    return cmd_check(dav, args.path, do_write=not args.no_write, keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
