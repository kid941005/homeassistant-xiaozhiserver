# -*- coding: utf-8 -*-
"""极简进程内指标：HTTP 请求数/耗时、工具调用数/耗时，Prometheus 文本格式输出。"""
from __future__ import annotations

import threading
from collections import defaultdict


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._sums: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._sums[key] += value
            self._counts[key] += 1

    def record_tool(self, tool: str, ok: bool, elapsed: float, code: str = "") -> None:
        self.inc("ha_mcp_tool_calls_total", {"tool": tool, "ok": str(bool(ok)).lower(), "code": code or "ok"})
        self.observe("ha_mcp_tool_duration_seconds", elapsed, {"tool": tool})

    def record_http(self, method: str, path: str, status: int, elapsed: float) -> None:
        self.inc("ha_mcp_http_requests_total", {"method": method, "path": path, "status": str(status)})
        self.observe("ha_mcp_http_request_duration_seconds", elapsed, {"method": method, "path": path})

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = sorted(self._counters.items())
            sums = sorted(self._sums.items())
        for (name, labels), value in counters:
            suffix = _format_labels(labels)
            lines.append(f"{name}{suffix} {value:g}")
        for (name, labels), total in sums:
            suffix = _format_labels(labels)
            count = self._counts.get((name, labels), 0)
            lines.append(f"{name}_sum{suffix} {total:g}")
            lines.append(f"{name}_count{suffix} {count}")
        return "\n".join(lines) + ("\n" if lines else "")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


metrics = Metrics()
