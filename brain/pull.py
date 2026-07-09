# brain/pull.py
"""大脑侧 WebDAV 拉取:PROPFIND 列目录 + 基于 ETag 的条件 GET,把变化的会话增量下载到本机。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
import urllib3
from requests.auth import HTTPBasicAuth

DAV = "{DAV:}"


class WebDAVClient:
    """最小 WebDAV 客户端(PROPFIND/GET);verify 默认 False 以兼容 NAS 自签名证书。"""

    def __init__(self, base_url: str, user: str, passwd: str, verify: bool = False):
        self.base = base_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, passwd)
        self.session.verify = verify
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _url(self, path: str) -> str:
        return urljoin(self.base, path.lstrip("/"))

    def list_dir(self, path: str, timeout: float = 15) -> list:
        """PROPFIND Depth:1 列出目录条目(含 etag),供增量判断使用。"""
        r = self.session.request("PROPFIND", self._url(path), headers={"Depth": "1"}, timeout=timeout)
        r.raise_for_status()
        return parse_listing(r.text)

    def get(self, path: str, etag=None, timeout: float = 30) -> requests.Response:
        """条件 GET:带 If-None-Match 时未变返回 304,省带宽;调用方需自行判断状态码。"""
        headers = {"If-None-Match": etag} if etag else {}
        return self.session.get(self._url(path), headers=headers, timeout=timeout)


def parse_listing(xml_text: str) -> list:
    """解析 WebDAV PROPFIND 的 multistatus XML,提取每项的 href/name/is_dir/etag。"""
    entries = []
    root = ET.fromstring(xml_text)
    for resp in root.findall(f"{DAV}response"):
        href_el = resp.find(f"{DAV}href")
        if href_el is None or not href_el.text:
            continue
        href = unquote(href_el.text)
        prop = resp.find(f"{DAV}propstat/{DAV}prop")
        is_dir = False
        etag = ""
        if prop is not None:
            rtype = prop.find(f"{DAV}resourcetype")
            is_dir = rtype is not None and rtype.find(f"{DAV}collection") is not None
            et = prop.find(f"{DAV}getetag")
            if et is not None and et.text:
                etag = et.text
        name = href.rstrip("/").split("/")[-1]
        entries.append({"href": href, "name": name, "is_dir": is_dir, "etag": etag})
    return entries


def pull_conversations(client: WebDAVClient, remote_dir: str, state: dict, inbound_dir: str) -> dict:
    """列出 remote_dir 下的 *.jsonl,对 etag 变化的文件下载到 inbound_dir,并更新 state。"""
    convs = state.setdefault("conversations", {})
    Path(inbound_dir).mkdir(parents=True, exist_ok=True)
    for entry in client.list_dir(remote_dir):
        if entry["is_dir"] or not entry["name"].endswith(".jsonl"):
            continue
        conv_id = entry["name"][:-len(".jsonl")]
        known_etag = convs.get(conv_id, {}).get("etag")
        if entry["etag"] and entry["etag"] == known_etag:
            continue  # 未变,跳过下载
        resp = client.get(remote_dir.rstrip("/") + "/" + entry["name"], etag=known_etag)
        if resp.status_code == 304:
            continue
        resp.raise_for_status()
        (Path(inbound_dir) / entry["name"]).write_bytes(resp.content)
        convs.setdefault(conv_id, {})["etag"] = resp.headers.get("ETag", entry["etag"])
    return state
