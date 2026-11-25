# -*- coding: utf-8 -*-
"""healthcheck.py 单元测试：/readyz 200 视为健康，503 视为不健康。"""
from __future__ import annotations

import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import healthcheck  # noqa: E402 - 需先把仓库根加入 sys.path


class Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        body = b'{"ok":true}' if self.status == 200 else b'{"ok":false}'
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ANN002 - 静默测试输出
        return


def main() -> None:
    assert healthcheck.candidate_hosts() == ["127.0.0.1"]
    os.environ["WEBUI_HOST"] = "0.0.0.0"
    assert healthcheck.candidate_hosts() == ["127.0.0.1"]
    os.environ["WEBUI_HOST"] = "192.0.2.10"
    assert healthcheck.candidate_hosts() == ["127.0.0.1", "192.0.2.10"]
    print("[ok] 候选主机顺序")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    old_host = os.environ.get("WEBUI_HOST")
    old_port = os.environ.get("WEBUI_PORT")
    try:
        os.environ["WEBUI_HOST"] = "127.0.0.1"
        os.environ["WEBUI_PORT"] = str(server.server_address[1])
        Handler.status = 200
        assert healthcheck.main() == 0
        print("[ok] /readyz 200 返回健康")
        Handler.status = 503
        assert healthcheck.main() == 1
        print("[ok] /readyz 503 返回不健康")
    finally:
        if old_host is None:
            os.environ.pop("WEBUI_HOST", None)
        else:
            os.environ["WEBUI_HOST"] = old_host
        if old_port is None:
            os.environ.pop("WEBUI_PORT", None)
        else:
            os.environ["WEBUI_PORT"] = old_port
        server.shutdown()
        server.server_close()
    print("HEALTHCHECK OK")


if __name__ == "__main__":
    main()
