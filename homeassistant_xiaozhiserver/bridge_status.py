# -*- coding: utf-8 -*-
"""MCP bridge 运行状态的跨进程共享文件。

WebUI 与 bridge 是两个进程：bridge 每 5 秒把自己与 MCP 接入点的实际连接状态
原子写入日志目录下的 .bridge-status.json，WebUI 读取该文件展示真实连接状态。
文件只保存脱敏端点和状态字段，不包含 token。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

from .logging_setup import log_file_path

STATUS_FILENAME = ".bridge-status.json"
STALE_AFTER_SECONDS = 20.0


def status_path() -> str:
    """返回状态文件路径：优先放在实际 bridge 日志目录，回退到临时目录。"""
    try:
        bridge_log = log_file_path("bridge")
    except Exception:  # noqa: BLE001 - 日志初始化失败时仍允许使用临时目录
        bridge_log = ""
    directory = os.path.dirname(bridge_log) if bridge_log else os.path.join(
        tempfile.gettempdir(), "xiaozhi-logs"
    )
    return os.path.join(directory, STATUS_FILENAME)


def write_status(payload: dict[str, Any], path: str | None = None) -> str:
    """原子写入状态文件，自动补充 updated_at。返回实际写入路径。"""
    target = path or status_path()
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    data = dict(payload)
    data["updated_at"] = float(data.get("updated_at") or time.time())
    # 唯一临时名避免多进程/多线程同时写时互相覆盖；fsync 后再原子替换。
    tmp = "%s.%s.tmp" % (target, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


def read_status(path: str | None = None, now: float | None = None) -> dict[str, Any] | None:
    """读取状态文件并补充 age_seconds/stale；文件不存在或损坏时返回 None。"""
    target = path or status_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        updated_at = float(data.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0.0
    age = max(0.0, (time.time() if now is None else now) - updated_at)
    data["age_seconds"] = round(age, 1)
    data["stale"] = age > STALE_AFTER_SECONDS
    return data
