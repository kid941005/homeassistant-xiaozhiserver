# -*- coding: utf-8 -*-
"""统一设置：集中解析全部环境变量，供 server / client / webui / bridge 共享。

- 所有环境变量只在这里读取一次，config.py 仅作为兼容导出层；
- 非法值记录 warning 并回退默认值/边界值，不让进程在 import 阶段崩溃；
- 不读取外部文件，不包含任何业务逻辑。
"""
from __future__ import annotations

import logging
import math
import os
import secrets
import tempfile
from dataclasses import dataclass, field

log = logging.getLogger("homeassistant_xiaozhiserver.settings")

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


def _split_path(base: str, suffix: str) -> str:
    if base.endswith(".log"):
        return base[:-4] + suffix + ".log"
    return base + suffix + ".log"


@dataclass(frozen=True)
class Settings:
    log_level: str = "INFO"
    log_file: str = ""
    log_file_webui: str = ""
    log_file_bridge: str = ""
    log_format: str = "text"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3
    default_list_limit: int = 50
    max_list_limit: int = 500
    excluded_domains: frozenset[str] = field(default_factory=frozenset)
    excluded_device_keywords: frozenset[str] = field(default_factory=frozenset)
    service_confirm_delay: float = 0.15
    service_confirm_retries: int = 3
    service_confirm_interval: float = 0.15
    service_confirm_timeout: float = 4.0
    ha_state_ws_enabled: bool = True
    ha_state_cache_ttl: float = 1.0
    ha_services_cache_ttl: float = 300.0
    ha_state_ws_max_failures: int = 5
    danger_confirm_enabled: bool = True
    danger_confirm_ttl: float = 120.0
    dangerous_domains: frozenset[str] = field(default_factory=frozenset)
    dangerous_cover_classes: frozenset[str] = field(default_factory=frozenset)
    dangerous_service_domains: frozenset[str] = field(default_factory=frozenset)
    webui_host: str = "0.0.0.0"
    webui_port: int = 8099
    webui_allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    webui_token: str = ""
    webui_token_generated: bool = False
    webui_cache_ttl: float = 3.0
    control_concurrency: int = 5
    metrics_enabled: bool = True

    def log_file_for(self, role: str | None = None) -> str:
        role = (role or os.environ.get("LOG_ROLE") or "bridge").strip().lower()
        if role == "webui":
            return self.log_file_webui or self.log_file
        return self.log_file_bridge or self.log_file

    @property
    def log_files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            path for path in (self.log_file_for("webui"), self.log_file_for("bridge")) if path
        ))


def _load() -> Settings:
    level = env_str("LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        log.warning("环境变量 LOG_LEVEL=%r 非法，使用 INFO", level)
        level = "INFO"
    base_file = env_str("LOG_FILE", "") or os.path.join(
        tempfile.gettempdir(), "homeassistant-xiaozhiserver.log"
    )
    webui_file = env_str("LOG_FILE_WEBUI", "") or _split_path(base_file, ".webui")
    bridge_file = env_str("LOG_FILE_BRIDGE", "") or _split_path(base_file, ".bridge")
    fmt = env_str("LOG_FORMAT", "text").lower()
    if fmt not in ("text", "json"):
        log.warning("环境变量 LOG_FORMAT=%r 非法，使用 text", fmt)
        fmt = "text"

    default_limit = env_int("DEFAULT_LIST_LIMIT", 50, 1, 10000)
    max_limit = env_int("MAX_LIST_LIMIT", 500, 1, 10000)
    if default_limit > max_limit:
        log.warning("DEFAULT_LIST_LIMIT=%s 大于 MAX_LIST_LIMIT=%s，按上限使用", default_limit, max_limit)
        default_limit = max_limit

    delay = env_float("SERVICE_CONFIRM_DELAY", 0.15, 0.0, 30.0)
    token = env_str("WEBUI_TOKEN", "")
    generated = False
    if not token:
        token = secrets.token_urlsafe(18)
        generated = True

    return Settings(
        log_level=level,
        log_file=base_file,
        log_file_webui=webui_file,
        log_file_bridge=bridge_file,
        log_format=fmt,
        log_max_bytes=env_int("LOG_MAX_BYTES", 5 * 1024 * 1024, 0, 1024 * 1024 * 1024),
        log_backup_count=env_int("LOG_BACKUP_COUNT", 3, 0, 50),
        default_list_limit=default_limit,
        max_list_limit=max_limit,
        excluded_domains=frozenset(env_set("EXCLUDED_DOMAINS", "automation,media_player")),
        excluded_device_keywords=frozenset(env_set("EXCLUDED_DEVICE_KEYWORDS", "门铃,水壶,中枢网关")),
        service_confirm_delay=delay,
        service_confirm_retries=env_int("SERVICE_CONFIRM_RETRIES", 3, 0, 20),
        service_confirm_interval=env_float("SERVICE_CONFIRM_INTERVAL", delay, 0.0, 30.0),
        service_confirm_timeout=env_float("SERVICE_CONFIRM_TIMEOUT", 4.0, 0.0, 300.0),
        ha_state_ws_enabled=env_bool("HA_STATE_WS_ENABLED", True),
        ha_state_cache_ttl=env_float("HA_STATE_CACHE_TTL", 1.0, 0.0, 60.0),
        ha_services_cache_ttl=env_float("HA_SERVICES_CACHE_TTL", 300.0, 0.0, 3600.0),
        ha_state_ws_max_failures=env_int("HA_STATE_WS_MAX_FAILURES", 5, 1, 100),
        danger_confirm_enabled=env_bool("DANGER_CONFIRM_ENABLED", True),
        danger_confirm_ttl=env_float("DANGER_CONFIRM_TTL", 120.0, 1.0, 86400.0),
        dangerous_domains=frozenset(env_set("DANGEROUS_DOMAINS", "lock,alarm_control_panel")),
        dangerous_cover_classes=frozenset(env_set("DANGEROUS_COVER_CLASSES", "garage,door,gate,window")),
        dangerous_service_domains=frozenset(env_set(
            "DANGEROUS_SERVICE_DOMAINS",
            "lock,alarm_control_panel,script,shell_command,python_script",
        )),
        webui_host=env_str("WEBUI_HOST", "0.0.0.0"),
        webui_port=env_int("WEBUI_PORT", 8099, 1, 65535),
        webui_allowed_hosts=frozenset(env_set("WEBUI_ALLOWED_HOSTS", "")),
        webui_token=token,
        webui_token_generated=generated,
        webui_cache_ttl=env_float("WEBUI_CACHE_TTL", 3.0, 0.0, 60.0),
        control_concurrency=env_int("CONTROL_CONCURRENCY", 5, 1, 20),
        metrics_enabled=env_bool("METRICS_ENABLED", True),
    )


settings = _load()

# 供 .env.example 一致性检查与文档使用：本程序会读取的全部环境变量。
APP_ENV_KEYS = (
    "LOG_LEVEL", "LOG_FILE", "LOG_FILE_WEBUI", "LOG_FILE_BRIDGE",
    "LOG_FORMAT", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT",
    "DEFAULT_LIST_LIMIT", "MAX_LIST_LIMIT",
    "EXCLUDED_DOMAINS", "EXCLUDED_DEVICE_KEYWORDS",
    "SERVICE_CONFIRM_DELAY", "SERVICE_CONFIRM_RETRIES", "SERVICE_CONFIRM_INTERVAL",
    "SERVICE_CONFIRM_TIMEOUT",
    "HA_STATE_WS_ENABLED", "HA_STATE_CACHE_TTL", "HA_SERVICES_CACHE_TTL",
    "HA_STATE_WS_MAX_FAILURES",
    "DANGER_CONFIRM_ENABLED", "DANGER_CONFIRM_TTL", "DANGEROUS_DOMAINS",
    "DANGEROUS_COVER_CLASSES", "DANGEROUS_SERVICE_DOMAINS",
    "WEBUI_HOST", "WEBUI_PORT", "WEBUI_ALLOWED_HOSTS", "WEBUI_TOKEN",
    "WEBUI_CACHE_TTL", "CONTROL_CONCURRENCY", "METRICS_ENABLED",
    "HA_URL", "HA_TOKEN", "HA_TIMEOUT", "HA_VERIFY_SSL", "HA_TRUST_ENV",
    "HA_WS_ENABLED", "HA_WS_URL", "HA_REGISTRY_TTL", "HA_REGISTRY_ERROR_TTL",
    "MCP_ENDPOINT",
)
