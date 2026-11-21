# -*- coding: utf-8 -*-
"""统一日志配置：带 call_id 的文本日志，并写入共享文件供 WebUI 查看。

为什么需要文件日志：
- WebUI 与 MCP bridge 是同一容器内的两个进程；
- 仅用 stderr 时，WebUI 只能看到自己的内存日志，看不到 bridge 与 MCP server 的日志；
- 两个进程都把日志追加到同一个文件，WebUI 读取文件尾部即可看到完整服务日志。
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
import tempfile
from logging.handlers import WatchedFileHandler

from . import __version__
from .config import LOG_FILE, LOG_LEVEL

_CALL_ID = contextvars.ContextVar("call_id", default="-")
_VERSION_LOGGED = False
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\w+)\s+(?:v(\d+\.\d+\.\d+\S*)\s+)?(\S+) \[([^\]]*)\] (.*)$"
)


class _CallIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = _CALL_ID.get()
        return True


class _VersionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.version = __version__
        return True


def log_file_path() -> str:
    """返回共享日志文件路径；LOG_FILE 为空时使用系统临时目录。"""
    return LOG_FILE or os.path.join(tempfile.gettempdir(), "homeassistant-xiaozhiserver.log")


def _formatter() -> logging.Formatter:
    return logging.Formatter(
        "%(asctime)s %(levelname)-5s v%(version)s %(name)s [%(call_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def setup_logging() -> None:
    """安装一次根 logger handler；重复调用只更新级别。"""
    level_name = (LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if not any(getattr(h, "_homeassistant_xiaozhiserver", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_formatter())
        handler.addFilter(_CallIdFilter())
        handler.addFilter(_VersionFilter())
        handler._homeassistant_xiaozhiserver = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    if not any(getattr(h, "_homeassistant_xiaozhiserver_file", False) for h in root.handlers):
        path = log_file_path()
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            handler = WatchedFileHandler(
                path,
                encoding="utf-8",
                errors="replace",
                delay=True,
            )
        except OSError as exc:
            logging.getLogger("homeassistant_xiaozhiserver.logging").warning(
                "无法创建日志文件 %s：%s", path, exc
            )
            return
        handler.setFormatter(_formatter())
        handler.addFilter(_CallIdFilter())
        handler.addFilter(_VersionFilter())
        handler._homeassistant_xiaozhiserver_file = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    global _VERSION_LOGGED
    if not _VERSION_LOGGED:
        logging.getLogger("homeassistant_xiaozhiserver").info("version=%s", __version__)
        _VERSION_LOGGED = True


def read_recent_logs(limit: int = 200, level: str | None = None) -> list[dict[str, str]]:
    """读取共享日志文件尾部，按行解析为结构化记录。

    多行异常堆栈会合并到上一条记录；无法解析的行直接跳过。
    """
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    wanted = (level or "").strip().upper()
    if wanted == "ALL":
        wanted = ""

    path = log_file_path()
    # 只读文件尾部固定窗口：日志无限增长时也不会把整个文件读进内存。
    max_bytes = 256 * 1024
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            raw = fh.read()
    except (FileNotFoundError, OSError):
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # 丢掉被截断的第一行
    if lines and not raw.endswith(b"\n"):
        lines = lines[:-1]  # 正在写入的半行不参与解析

    entries: list[dict[str, str]] = []
    for raw_line in lines[-max(limit * 4, 500):]:
        line = raw_line.rstrip("\r\n")
        match = _LINE_RE.match(line)
        if match:
            entries.append({
                "time": match.group(1),
                "level": match.group(2).upper(),
                "version": match.group(3) or "",
                "logger": match.group(4),
                "call_id": match.group(5),
                "message": match.group(6),
            })
        elif entries and line:
            entries[-1]["message"] += "\n" + line

    if wanted:
        entries = [e for e in entries if e["level"] == wanted]
    return entries[-limit:]


def set_call_id(value: str):
    return _CALL_ID.set(value or "-")


def reset_call_id(token) -> None:
    _CALL_ID.reset(token)
