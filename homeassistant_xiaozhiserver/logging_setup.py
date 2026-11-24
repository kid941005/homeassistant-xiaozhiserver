# -*- coding: utf-8 -*-
"""统一日志配置：分进程文件、可选 JSON 结构化输出，WebUI 可读取全部日志文件。

- WebUI 与 MCP bridge 是两个进程，各自写自己的文件，避免同一 RotatingFileHandler 轮转竞争；
- 设置 LOG_SPLIT=false 可退回两进程共用一个文件（保留旧行为）；
- 日志目录不可写时自动回退到临时目录，并且不再刷屏 Python logging 的 Traceback；
- read_recent_logs 读取所有实际日志文件尾部，按时间排序后返回。
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler, WatchedFileHandler

from . import __version__
from .settings import settings

_CALL_ID = contextvars.ContextVar("call_id", default="-")
_VERSION_LOGGED = False
_EFFECTIVE_FILES: dict[str, str] = {}
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[,.]\d{3})? (\w+)\s+(?:v(\d+\.\d+\.\d+\S*)\s+)?(\S+) \[([^\]]*)\] (.*)$"
)


class _CallIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = _CALL_ID.get()
        return True


class _VersionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.version = __version__
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "version": getattr(record, "version", __version__),
            "logger": record.name,
            "call_id": getattr(record, "call_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _ConciseFileErrors:
    """文件 handler 写失败时只打印一条带时间戳的告警，不再输出整段 Traceback。"""

    def handleError(self, record: logging.LogRecord) -> None:  # pragma: no cover - 防御性
        if getattr(self, "_error_logged", False):
            return
        self._error_logged = True  # type: ignore[attr-defined]
        exc = sys.exc_info()[1]
        sys.stderr.write(
            "%s ERROR 日志文件不可写 %s：%s；后续文件日志暂停，请检查 LOG_FILE 目录权限\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), getattr(self, "baseFilename", "?"), exc)
        )


class _SafeRotatingFileHandler(_ConciseFileErrors, RotatingFileHandler):
    pass


class _SafeWatchedFileHandler(_ConciseFileErrors, WatchedFileHandler):
    pass


def _role_name(role: str | None = None) -> str:
    return (role or os.environ.get("LOG_ROLE") or "bridge").strip().lower()


def _probe_writable(path: str) -> bool:
    if not path:
        return False
    parent = os.path.dirname(path)
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.close(fd)
        return True
    except OSError:
        return False


def _effective_path(role: str | None = None) -> str:
    key = _role_name(role)
    if key in _EFFECTIVE_FILES:
        return _EFFECTIVE_FILES[key]

    preferred = settings.log_file_for(key)
    if _probe_writable(preferred):
        chosen = preferred
    else:
        fallback = os.path.join(
            tempfile.gettempdir(), "xiaozhi-logs", os.path.basename(preferred) or "homeassistant-xiaozhiserver.log"
        )
        chosen = fallback if _probe_writable(fallback) else ""
    _EFFECTIVE_FILES[key] = chosen
    return chosen


def log_file_path(role: str | None = None) -> str:
    """返回本进程（或指定角色）实际可写的日志文件路径；不可写时返回空串。"""
    return _effective_path(role)


def log_files() -> tuple[str, ...]:
    roles = ("webui", "bridge") if settings.log_split else ("bridge",)
    paths = [p for p in (_effective_path(role) for role in roles) if p]
    return tuple(dict.fromkeys(paths))


def _formatter() -> logging.Formatter:
    if settings.log_format == "json":
        return _JsonFormatter()
    return logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s v%(version)s %(name)s [%(call_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def setup_logging(role: str | None = None) -> None:
    """安装根 logger handler；不同进程写不同文件，重复调用只补缺失的 handler。"""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    # 清掉 third-party 或上一次 basicConfig 留下的 handler，避免同一条日志被无格式地重复输出。
    for handler in list(root.handlers):
        if not getattr(handler, "_homeassistant_xiaozhiserver", False) and not getattr(
            handler, "_homeassistant_xiaozhiserver_file", False
        ):
            root.removeHandler(handler)

    if not any(getattr(h, "_homeassistant_xiaozhiserver", False) for h in root.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(_formatter())
        stream.addFilter(_CallIdFilter())
        stream.addFilter(_VersionFilter())
        stream._homeassistant_xiaozhiserver = True  # type: ignore[attr-defined]
        root.addHandler(stream)

    if not any(getattr(h, "_homeassistant_xiaozhiserver_file", False) for h in root.handlers):
        path = log_file_path(role)
        preferred = settings.log_file_for(role)
        if path and path != preferred:
            logging.getLogger("homeassistant_xiaozhiserver.logging").warning(
                "日志路径不可写 %s，已回退到 %s", preferred, path
            )
        if path:
            try:
                if settings.log_max_bytes > 0:
                    handler = _SafeRotatingFileHandler(
                        path,
                        maxBytes=settings.log_max_bytes,
                        backupCount=settings.log_backup_count,
                        encoding="utf-8",
                        delay=True,
                    )
                else:
                    handler = _SafeWatchedFileHandler(path, encoding="utf-8", errors="replace", delay=True)
            except OSError as exc:
                logging.getLogger("homeassistant_xiaozhiserver.logging").warning(
                    "无法创建日志文件 %s：%s；仅输出到 stderr", path, exc
                )
            else:
                handler.setFormatter(_formatter())
                handler.addFilter(_CallIdFilter())
                handler.addFilter(_VersionFilter())
                handler._homeassistant_xiaozhiserver_file = True  # type: ignore[attr-defined]
                root.addHandler(handler)
        else:
            logging.getLogger("homeassistant_xiaozhiserver.logging").warning(
                "日志目录不可写，已仅输出到 stderr；请检查 LOG_FILE 目录权限或挂载"
            )

    global _VERSION_LOGGED
    if not _VERSION_LOGGED:
        logging.getLogger("homeassistant_xiaozhiserver").info(
            "version=%s role=%s log_file=%s",
            __version__,
            _role_name(role),
            log_file_path(role) or "stderr",
        )
        _VERSION_LOGGED = True


def _tail(path: str, max_bytes: int = 256 * 1024) -> list[str]:
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
        lines = lines[1:]
    if lines and not raw.endswith(b"\n"):
        lines = lines[:-1]
    return lines


def _parse_line(line: str) -> dict[str, str] | None:
    if line.startswith("{"):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        return {
            "time": str(obj.get("time", "")),
            "level": str(obj.get("level", "")).upper(),
            "version": str(obj.get("version", "")),
            "logger": str(obj.get("logger", "")),
            "call_id": str(obj.get("call_id", "-")),
            "message": str(obj.get("message", "")),
        }
    match = _LINE_RE.match(line)
    if not match:
        return None
    return {
        "time": match.group(1),
        "level": match.group(2).upper(),
        "version": match.group(3) or "",
        "logger": match.group(4),
        "call_id": match.group(5),
        "message": match.group(6),
    }


def read_recent_logs(limit: int = 200, level: str | None = None) -> list[dict[str, str]]:
    """读取全部实际日志文件尾部，合并后按时间返回最后 limit 条。"""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    wanted = (level or "").strip().upper()
    if wanted == "ALL":
        wanted = ""

    entries: list[dict[str, str]] = []
    for path in log_files():
        for raw_line in _tail(path):
            line = raw_line.rstrip("\r\n")
            parsed = _parse_line(line)
            if parsed:
                entries.append(parsed)
            elif entries and line and not line.startswith("{"):
                entries[-1]["message"] += "\n" + line
    entries.sort(key=lambda item: item.get("time", ""))
    if wanted:
        entries = [e for e in entries if e["level"] == wanted]
    return entries[-limit:]


def set_call_id(value: str):
    return _CALL_ID.set(value or "-")


def reset_call_id(token) -> None:
    _CALL_ID.reset(token)
