# -*- coding: utf-8 -*-
"""容器健康检查：探测 WebUI /readyz（HA 与 bridge 均就绪才返回 200）。

WEBUI_HOST 可能绑定 0.0.0.0、:: 或某个具体网卡地址；容器内最稳妥的是同时尝试
配置的主机与 127.0.0.1，任一返回 200 即视为健康。启动期依赖尚未就绪时 /readyz
返回 503，Dockerfile 的 start-period 已放宽到 60s。
"""
from __future__ import annotations

import os
import sys
import urllib.request


def candidate_hosts() -> list[str]:
    raw = (os.environ.get("WEBUI_HOST") or "").strip()
    # 先试回环地址：WEBUI_HOST 绑定具体网卡时回环通常也可用，且可避免先等外部地址超时。
    hosts = ["127.0.0.1"]
    if raw and raw not in ("0.0.0.0", "::", "[::]"):
        hosts.append(raw)
    seen: list[str] = []
    for host in hosts:
        if host not in seen:
            seen.append(host)
    return seen


def main() -> int:
    port = os.environ.get("WEBUI_PORT", "8099")
    for host in candidate_hosts():
        url_host = "[" + host + "]" if ":" in host else host
        url = "http://%s:%s/readyz" % (url_host, port)
        try:
            # /readyz 会按 HA_TIMEOUT 探测 HA，冷启动可能接近 10s；留出余量。
            with urllib.request.urlopen(url, timeout=12) as response:  # noqa: S310 - 固定本机探针
                if response.status == 200:
                    return 0
        except Exception:  # noqa: BLE001 - 逐个候选主机降级
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
