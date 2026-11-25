# -*- coding: utf-8 -*-
"""极简指标：HTTP 请求数/耗时、工具调用数/耗时，Prometheus 文本格式输出。

MCP 工具在 mcp_pipe 拉起的 stdio 子进程中执行，而 /metrics 由 WebUI 进程提供。
为避免工具指标只留在子进程内，LOG_ROLE=mcp 的进程会把指标快照写到共享目录，
render() 时再合并其他进程的快照。共享目录默认是系统临时目录下的
xiaozhi-metrics，可用 XIAOZHI_METRICS_DIR 覆盖（容器内建议挂到 /tmp）。
"""
from __future__ import annotations

import glob
import json
import os
import tempfile
import threading
import time
from collections import defaultdict

_SHARED_DIR_ENV = "XIAOZHI_METRICS_DIR"
_SHARED_MAX_AGE = 24 * 3600


def _shared_dir() -> str:
    raw = (os.environ.get(_SHARED_DIR_ENV) or "").strip()
    return raw or os.path.join(tempfile.gettempdir(), "xiaozhi-metrics")


def _shared_path(pid: int | None = None) -> str:
    return os.path.join(_shared_dir(), "metrics-" + str(pid or os.getpid()) + ".json")


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Metrics:
    def __init__(self, persist: bool | None = None) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._sums: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        if persist is None:
            persist = (os.environ.get("LOG_ROLE") or "").strip().lower() == "mcp"
        self._persist = bool(persist)

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
        self.flush()

    def record_http(self, method: str, path: str, status: int, elapsed: float) -> None:
        self.inc("ha_mcp_http_requests_total", {"method": method, "path": path, "status": str(status)})
        self.observe("ha_mcp_http_request_duration_seconds", elapsed, {"method": method, "path": path})

    def _snapshot(self) -> dict:
        with self._lock:
            return {
                "pid": os.getpid(),
                "updated_at": time.time(),
                "counters": [[name, list(labels), value] for (name, labels), value in self._counters.items()],
                "sums": [[name, list(labels), value] for (name, labels), value in self._sums.items()],
                "counts": [[name, list(labels), value] for (name, labels), value in self._counts.items()],
            }

    def flush(self) -> None:
        """把当前快照原子写入共享目录；非持久化进程或磁盘不可写时静默跳过。"""
        if not self._persist:
            return
        path = _shared_path()
        tmp = path + "." + str(time.time_ns()) + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._snapshot(), fh, ensure_ascii=False, separators=(",", ":"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _shared_snapshots(self) -> list[dict]:
        own = os.path.basename(_shared_path())
        out: list[dict] = []
        for path in glob.glob(os.path.join(_shared_dir(), "metrics-*.json")):
            if os.path.basename(path) == own:
                continue
            try:
                if time.time() - os.path.getmtime(path) > _SHARED_MAX_AGE:
                    os.unlink(path)
                    continue
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                out.append(data)
        return out

    @staticmethod
    def _merge_into(target: dict, rows: object) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            try:
                name, labels, value = row
                key = (str(name), tuple((str(k), str(v)) for k, v in labels))
                target[key] += float(value)
            except (TypeError, ValueError):
                continue

    def render(self) -> str:
        with self._lock:
            counters = defaultdict(float, self._counters)
            sums = defaultdict(float, self._sums)
            counts = defaultdict(int, self._counts)
        for data in self._shared_snapshots():
            self._merge_into(counters, data.get("counters"))
            self._merge_into(sums, data.get("sums"))
            self._merge_into(counts, data.get("counts"))
        lines: list[str] = []
        for (name, labels), value in sorted(counters.items()):
            lines.append(f"{name}{_format_labels(labels)} {value:g}")
        for (name, labels), total in sorted(sums.items()):
            suffix = _format_labels(labels)
            count = counts.get((name, labels), 0)
            lines.append(f"{name}_sum{suffix} {total:g}")
            lines.append(f"{name}_count{suffix} {count}")
        return "\n".join(lines) + ("\n" if lines else "")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


metrics = Metrics()
