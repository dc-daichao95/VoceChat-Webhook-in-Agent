"""底层 reader 对真实本地 chunked/compressed 响应的边界测试。"""

import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from scheduler._online_http import ResponseTooLarge, read_body


class BoundaryHandler(BaseHTTPRequestHandler):
    """仅为底层 reader 提供确定性的本地传输编码响应。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        """禁止测试服务器向 stderr 输出请求信息。"""

    def _common_headers(self):
        self.send_header("Connection", "close")

    def do_GET(self):
        """按路径返回 chunked 或 gzip 正文。"""
        if self.path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self._common_headers()
            self.end_headers()
            for chunk in (b"abc", b"def"):
                self.wfile.write(b"3\r\n" + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        compressed = gzip.compress(b"A" * 128)
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(compressed)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(compressed)


@pytest.fixture
def local_reader_server():
    """启动仅供 read_body 使用的 loopback HTTP 服务。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BoundaryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _read_local(url, max_bytes):
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(url, stream=True, timeout=(1, 1))
        try:
            return read_body(
                response,
                max_bytes=max_bytes,
                deadline=time.monotonic() + 2,
                clock=time.monotonic,
            )
        finally:
            response.close()
    finally:
        session.close()


def test_reader_accepts_chunked_body_at_exact_limit(local_reader_server):
    """真实 chunked 解帧后的正文等于上限时应完整返回。"""
    assert _read_local(local_reader_server + "/chunked", 6) == b"abcdef"


def test_reader_rejects_chunked_body_over_limit(local_reader_server):
    """真实 chunked 解帧后的正文越过上限时应拒绝。"""
    with pytest.raises(ResponseTooLarge):
        _read_local(local_reader_server + "/chunked", 5)


def test_reader_limits_decompressed_body_not_wire_size(local_reader_server):
    """gzip 线传体积虽小，解压正文越过上限仍必须拒绝。"""
    with pytest.raises(ResponseTooLarge):
        _read_local(local_reader_server + "/compressed", 127)


def test_reader_accepts_decompressed_body_at_limit(local_reader_server):
    """gzip 解压正文恰好等于上限时应完整返回。"""
    assert _read_local(local_reader_server + "/compressed", 128) == b"A" * 128
