# -*- coding: utf-8 -*-
"""兼容导出层：所有常量都由 settings.Settings 统一解析。

保留本模块是为了不破坏已有 import（server/client/service/danger/views/webui 等）。
新增代码应直接使用 settings；这里仅做只读转发。
"""
from __future__ import annotations

from .settings import env_bool, env_float, settings  # noqa: F401  兼容旧 import

LOG_LEVEL = settings.log_level
LOG_FILE = settings.log_file
LOG_FILE_WEBUI = settings.log_file_webui
LOG_FILE_BRIDGE = settings.log_file_bridge
LOG_FILE_MCP = settings.log_file_mcp
LOG_FORMAT = settings.log_format
LOG_MAX_BYTES = settings.log_max_bytes
LOG_BACKUP_COUNT = settings.log_backup_count

DEFAULT_LIST_LIMIT = settings.default_list_limit
MAX_LIST_LIMIT = settings.max_list_limit
EXCLUDED_DOMAINS = set(settings.excluded_domains)
EXCLUDED_DEVICE_KEYWORDS = set(settings.excluded_device_keywords)

SERVICE_CONFIRM_DELAY = settings.service_confirm_delay
SERVICE_CONFIRM_RETRIES = settings.service_confirm_retries
SERVICE_CONFIRM_INTERVAL = settings.service_confirm_interval
SERVICE_CONFIRM_TIMEOUT = settings.service_confirm_timeout

HA_STATE_WS_ENABLED = settings.ha_state_ws_enabled
HA_STATE_CACHE_TTL = settings.ha_state_cache_ttl
HA_SERVICES_CACHE_TTL = settings.ha_services_cache_ttl
HA_STATE_WS_MAX_FAILURES = settings.ha_state_ws_max_failures

DANGER_CONFIRM_ENABLED = settings.danger_confirm_enabled
DANGER_CONFIRM_TTL = settings.danger_confirm_ttl
DANGEROUS_DOMAINS = set(settings.dangerous_domains)
DANGEROUS_COVER_CLASSES = set(settings.dangerous_cover_classes)
DANGEROUS_SERVICE_DOMAINS = set(settings.dangerous_service_domains)

WEBUI_HOST = settings.webui_host
WEBUI_PORT = settings.webui_port
WEBUI_ALLOWED_HOSTS = set(settings.webui_allowed_hosts)
WEBUI_TOKEN = settings.webui_token
WEBUI_TOKEN_GENERATED = settings.webui_token_generated
WEBUI_CACHE_TTL = settings.webui_cache_ttl
CONTROL_CONCURRENCY = settings.control_concurrency
METRICS_ENABLED = settings.metrics_enabled
