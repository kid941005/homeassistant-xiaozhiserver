# -*- coding: utf-8 -*-
"""集中解析环境变量与运行常量。

设计目标：
- 所有环境变量只在这里解析，避免 server.py / client.py 各自实现一套。
- 非法数值不直接让进程在 import 阶段崩溃：记录 warning 并回退默认值/边界值。
- 布尔值统一支持 1/true/yes/on/y 与 0/false/no/off/n；无法识别的值记录 warning 并回退默认值。
"""
from __future__ import annotations

import logging
import math
import os

log = logging.getLogger("homeassistant-xiaozhiserver.config")

_TRUE = ("1", "true", "yes", "on", "y")
_FALSE = ("0", "false", "no", "off", "n")


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    log.warning("环境变量 %s=%r 不是布尔值，使用默认值 %s", name, raw, default)
    return default


def env_int(name: str, default: int, minimum: int | None = None,
            maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        log.warning("环境变量 %s=%r 不是整数，使用默认值 %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("环境变量 %s=%s 小于最小值 %s，按 %s 使用", name, value, minimum, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        log.warning("环境变量 %s=%s 大于最大值 %s，按 %s 使用", name, value, maximum, maximum)
        value = maximum
    return value


def env_float(name: str, default: float, minimum: float | None = None,
              maximum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        log.warning("环境变量 %s=%r 不是数字，使用默认值 %s", name, raw, default)
        return default
    if not math.isfinite(value):
        log.warning("环境变量 %s=%r 不是有限数字，使用默认值 %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning("环境变量 %s=%s 小于最小值 %s，按 %s 使用", name, value, minimum, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        log.warning("环境变量 %s=%s 大于最大值 %s，按 %s 使用", name, value, maximum, maximum)
        value = maximum
    return value


def env_set(name: str, default: str) -> set[str]:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


# 日志（服务固定 stdio，不解析 HTTP 监听参数）
LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    log.warning("环境变量 LOG_LEVEL=%r 非法，使用 INFO", LOG_LEVEL)
    LOG_LEVEL = "INFO"

# 共享日志文件（WebUI 与 bridge 两个进程都写入，WebUI 读取尾部展示）
# 留空时使用系统临时目录，通常不需要设置。
LOG_FILE = env_str("LOG_FILE", "")

# 设备列表
DEFAULT_LIST_LIMIT = env_int("DEFAULT_LIST_LIMIT", 50, 1, 10000)
MAX_LIST_LIMIT = env_int("MAX_LIST_LIMIT", 500, 1, 10000)
if DEFAULT_LIST_LIMIT > MAX_LIST_LIMIT:
    log.warning("DEFAULT_LIST_LIMIT=%s 大于 MAX_LIST_LIMIT=%s，按上限使用", DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)
    DEFAULT_LIST_LIMIT = MAX_LIST_LIMIT

# 不向 MCP 暴露的设备类型/设备名称关键字（默认隐藏：门铃、自动化、热水壶、中枢网关、媒体）
# 设备名称关键字会同时匹配实体名、entity_id 与设备注册表名称。
EXCLUDED_DOMAINS = env_set("EXCLUDED_DOMAINS", "automation,media_player")
EXCLUDED_DEVICE_KEYWORDS = env_set("EXCLUDED_DEVICE_KEYWORDS", "门铃,水壶,中枢网关")

# 控制后回读
SERVICE_CONFIRM_DELAY = env_float("SERVICE_CONFIRM_DELAY", 0.6, 0.0, 30.0)
SERVICE_CONFIRM_RETRIES = env_int("SERVICE_CONFIRM_RETRIES", 1, 0, 20)
SERVICE_CONFIRM_INTERVAL = env_float("SERVICE_CONFIRM_INTERVAL", SERVICE_CONFIRM_DELAY, 0.0, 30.0)
SERVICE_CONFIRM_TIMEOUT = env_float("SERVICE_CONFIRM_TIMEOUT", 4.0, 0.0, 300.0)

# 危险设备二次确认
DANGER_CONFIRM_ENABLED = env_bool("DANGER_CONFIRM_ENABLED", True)
DANGER_CONFIRM_TTL = env_float("DANGER_CONFIRM_TTL", 120.0, 1.0, 86400.0)
DANGEROUS_DOMAINS = env_set("DANGEROUS_DOMAINS", "lock,alarm_control_panel")
DANGEROUS_COVER_CLASSES = env_set("DANGEROUS_COVER_CLASSES", "garage,door,gate,window")
# 这些服务域即使目标实体本身不危险，也可能执行脚本/系统级动作，同样需要二次确认。
DANGEROUS_SERVICE_DOMAINS = env_set(
    "DANGEROUS_SERVICE_DOMAINS",
    "lock,alarm_control_panel,script,shell_command,python_script",
)

# WebUI 固定监听 0.0.0.0:8099，无独立密码；不需要额外环境变量
