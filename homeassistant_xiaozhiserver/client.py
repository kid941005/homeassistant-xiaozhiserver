# -*- coding: utf-8 -*-
"""Home Assistant REST API 客户端。

设计要点：
- 所有配置只从环境变量读取，绝不硬编码地址或令牌。
- 网络/鉴权/超时等异常统一转换为 HAError（带错误码 + 中文可读信息），
  由上层 MCP 工具捕获后转成结构化结果，保证服务不崩溃。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import env_bool, env_float

log = logging.getLogger("homeassistant_xiaozhiserver.client")

try:  # 设备/实体注册表需通过 HA WebSocket API 读取；缺失时降级为按实体展示
    import websockets
except Exception:  # noqa: BLE001
    websockets = None

_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def is_entity_id(value: object) -> bool:
    """entity_id 必须是 domain.object_id 形式，避免把用户输入拼进 URL 路径。"""
    return bool(_ENTITY_ID_RE.match(str(value or "").strip()))


class HAError(Exception):
    """带错误码与用户可读信息的统一异常。"""

    def __init__(self, code: str, message: str, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


class HAClient:
    def __init__(self, url: str | None = None, token: str | None = None,
                 timeout: float | None = None, verify_ssl: bool | None = None):
        self.base_url = (url if url is not None else os.environ.get("HA_URL") or "").rstrip("/")
        self.token = (token if token is not None else os.environ.get("HA_TOKEN") or "").strip()
        self.timeout = float(timeout) if timeout is not None else env_float("HA_TIMEOUT", 10.0, 0.1, 300.0)
        self.verify_ssl = env_bool("HA_VERIFY_SSL", True) if verify_ssl is None else verify_ssl
        # 默认不读取宿主的 HTTP_PROXY/NO_PROXY 等环境变量，避免代理干扰容器内直连 HA。
        self.trust_env = env_bool("HA_TRUST_ENV", False)
        # 设备/实体注册表：HA 只在 WebSocket API 暴露（REST 无此端点），用于“按设备聚合”。
        self.ws_url = (os.environ.get("HA_WS_URL") or "").strip() or self._derive_ws_url()
        self.ws_enabled = env_bool("HA_WS_ENABLED", True)
        self.registry_ttl = env_float("HA_REGISTRY_TTL", 60.0, 0.0, 3600.0)
        self.registry_error_ttl = env_float("HA_REGISTRY_ERROR_TTL", 10.0, 0.0, 3600.0)
        self._registry_cache: dict | None = None
        self._registry_ts: float = 0.0
        self._http: httpx.AsyncClient | None = None
        self._validate_config()

    def _validate_config(self) -> None:
        missing = []
        if not self.base_url:
            missing.append("HA_URL")
        if not self.token:
            missing.append("HA_TOKEN")
        if missing:
            raise HAError(
                "config_missing",
                "缺少必需的环境变量：" + ", ".join(missing) + "。请在 .env 或 docker run -e 中配置后重启服务。",
            )
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HAError(
                "config_invalid",
                "HA_URL 必须以 http:// 或 https:// 开头并包含主机名，当前为：" + self.base_url,
            )

    # ---------------- 底层 HTTP ----------------
    def _client(self) -> httpx.AsyncClient:
        """懒创建并复用 HTTP 连接池，避免每次调用都新建 TLS 连接。"""
        if self._http is None:
            headers = {
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            }
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
                trust_env=self.trust_env,
            )
        return self._http

    async def aclose(self) -> None:
        """释放连接池（进程退出或测试收尾时调用）。"""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        log.debug("HA request %s %s", method, path)
        try:
            client = self._client()
            resp = await client.request(method, path, json=json)
        except httpx.ConnectError as e:
            raise HAError(
                "unreachable",
                "无法连接 Home Assistant（" + self.base_url + "）。请检查 HA_URL 是否正确、HA 是否在运行、本服务能否访问该地址。",
                str(e),
            ) from e
        except httpx.TimeoutException as e:
            raise HAError(
                "timeout",
                "访问 Home Assistant 超时（" + str(int(self.timeout)) + "s）。可增大环境变量 HA_TIMEOUT，或检查网络/HA 负载。",
                str(e),
            ) from e
        except httpx.HTTPError as e:
            raise HAError("network_error", "访问 Home Assistant 发生网络错误：" + str(e), str(e)) from e

        log.debug("HA response %s %s -> %s", method, path, resp.status_code)
        if resp.status_code in (401, 403):
            raise HAError(
                "auth_failed",
                "Home Assistant 鉴权失败：令牌无效/已过期，或该令牌无权限。请重新生成长期访问令牌并更新 HA_TOKEN。",
                "HTTP " + str(resp.status_code),
            )
        if resp.status_code == 404:
            raise HAError("not_found", "请求的资源不存在（HTTP 404）。", method + " " + path)
        if resp.status_code >= 500:
            raise HAError("upstream_error", "Home Assistant 内部错误（HTTP " + str(resp.status_code) + "）。", resp.text[:300])
        if resp.status_code >= 400:
            raise HAError("http_error", "Home Assistant 返回错误（HTTP " + str(resp.status_code) + "）。", resp.text[:300])
        if resp.content:
            try:
                return resp.json()
            except Exception:
                return resp.text
        return None

    # ---------------- 上层 API ----------------
    async def get_states(self) -> list:
        data = await self._request("GET", "/api/states")
        return data if isinstance(data, list) else []

    async def get_services(self) -> list:
        """GET /api/services，返回服务目录（每个 domain 及其服务与参数字段）。"""
        data = await self._request("GET", "/api/services")
        return data if isinstance(data, list) else []

    async def get_state(self, entity_id: str) -> dict | None:
        eid = str(entity_id or "").strip()
        if not is_entity_id(eid):
            raise HAError("invalid_entity", "entity_id 格式不合法：" + eid + "。应为 domain.object_id，如 light.living_room。")
        try:
            data = await self._request("GET", "/api/states/" + eid)
        except HAError as e:
            if e.code == "not_found":
                return None
            raise
        if not isinstance(data, dict):
            raise HAError("invalid_response", "Home Assistant 返回的实体状态不是 JSON 对象。")
        return data

    async def call_service(self, domain: str, service: str, data: dict) -> Any:
        dom = str(domain or "").strip()
        svc = str(service or "").strip()
        if not _SERVICE_NAME_RE.match(dom) or not _SERVICE_NAME_RE.match(svc):
            raise HAError("invalid_service", "服务名格式不合法：" + dom + "." + svc + "。只允许小写字母、数字与下划线。")
        result = await self._request("POST", "/api/services/" + dom + "/" + svc, json=data)
        if result is not None and not isinstance(result, (dict, list)):
            raise HAError("invalid_response", "Home Assistant 返回的服务调用结果不是 JSON 对象/数组。")
        return result

    async def ping(self) -> bool:
        await self._request("GET", "/api/")
        return True

    # ---------------- 设备/实体注册表（WebSocket） ----------------
    def _derive_ws_url(self) -> str:
        u = self.base_url
        if u.startswith("https://"):
            u = "wss://" + u[len("https://"):]
        elif u.startswith("http://"):
            u = "ws://" + u[len("http://"):]
        return u.rstrip("/") + "/api/websocket"

    def _ws_ssl(self):
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def get_registries(self, force: bool = False) -> dict | None:
        """读取 HA 的设备、实体与区域注册表（带 TTL 缓存）。

        返回 {"devices": [...], "entities": [...], "areas": [...]}；WebSocket 不可用/无权限时返回 None，
        调用方应降级为按实体展示，不影响其它功能。区域注册表读取失败时不影响设备/实体注册表。
        """
        if not self.ws_enabled or websockets is None:
            return None
        now = time.time()
        age = now - self._registry_ts
        if not force and self._registry_cache is not None and age < self.registry_ttl:
            return self._registry_cache
        # 失败结果只短缓存，避免 WS 瞬时故障后 60 秒内都不再重试
        if (not force and self._registry_cache is None and self._registry_ts
                and age < self.registry_error_ttl):
            return None
        try:
            data = await self._ws_registries()
            log.debug("HA registry loaded: devices=%s entities=%s areas=%s",
                      len((data or {}).get("devices") or []),
                      len((data or {}).get("entities") or []),
                      len((data or {}).get("areas") or []))
        except HAError as e:
            log.warning("HA registry unavailable: %s (%s)", e.message, e.code)
            data = None
        except Exception as e:  # noqa: BLE001 注册表仅是增强信息，任何异常都降级
            log.warning("HA registry unavailable: %s", e)
            data = None
        self._registry_cache = data
        self._registry_ts = now
        return data

    async def _ws_registries(self) -> dict:
        kwargs: dict = {"open_timeout": self.timeout, "close_timeout": 2}
        if self.ws_url.startswith("wss://"):
            kwargs["ssl"] = self._ws_ssl()
        try:
            async with websockets.connect(self.ws_url, **kwargs) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.timeout))
                if hello.get("type") == "auth_required":
                    await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                    auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.timeout))
                    if auth.get("type") != "auth_ok":
                        raise HAError("auth_failed", "Home Assistant WebSocket 鉴权失败：请检查 HA_TOKEN 是否有效。")
                elif hello.get("type") != "auth_ok":
                    raise HAError("ws_error", "Home Assistant WebSocket 握手异常：" + str(hello.get("type")))
                cmds = [
                    (1, "config/device_registry/list", "devices"),
                    (2, "config/entity_registry/list", "entities"),
                    (3, "config/area_registry/list", "areas"),
                ]
                pending = {cid: (ctype, key) for cid, ctype, key in cmds}
                for cid, ctype, _ in cmds:
                    await ws.send(json.dumps({"id": cid, "type": ctype}))
                out: dict = {"devices": [], "entities": [], "areas": []}
                # 按消息 id 收集结果：HA 可能穿插 event 或其它响应，不能按消息条数硬收。
                deadline = time.monotonic() + max(self.timeout * 3, 5.0)
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                    if not isinstance(msg, dict) or msg.get("type") != "result":
                        continue
                    cid = msg.get("id")
                    if cid not in pending:
                        continue
                    _, key = pending.pop(cid)
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        text = str(err.get("message") or err)
                        # 区域注册表属于增强信息，失败时只降级区域名，不影响设备/实体。
                        if key == "areas":
                            log.warning("HA area registry unavailable: %s", text)
                            out["areas"] = []
                            continue
                        raise HAError("registry_error", "读取 Home Assistant 注册表失败：" + text)
                    out[key] = msg.get("result") or []
                return out
        except asyncio.TimeoutError as e:
            raise HAError("timeout", "连接 Home Assistant WebSocket 超时。", str(e)) from e
        except HAError:
            raise
        except Exception as e:  # noqa: BLE001
            raise HAError("ws_error", "连接 Home Assistant WebSocket 失败：" + str(e), str(e)) from e

    async def resolve_entity(self, ref: str) -> tuple:
        """把 entity_id 或友好名解析成一个实体。

        返回 (entity, candidates)：
        - entity 非空：唯一命中
        - entity 为空且 candidates 非空：多个候选（歧义），需调用方消歧
        - 两者皆空：未找到
        """
        ref = (ref or "").strip()
        if not ref:
            return None, []
        # 1) 直接当作 entity_id；只接受合法格式，避免把用户输入拼进 URL 路径
        if is_entity_id(ref):
            st = await self.get_state(ref)
            if st is not None:
                return st, []
        states = await self.get_states()
        low = ref.lower()

        def fname(s: dict) -> str:
            return str((s.get("attributes") or {}).get("friendly_name", ""))

        exact = [s for s in states if fname(s).lower() == low]
        if len(exact) == 1:
            return exact[0], []
        if len(exact) > 1:
            return None, exact
        contains = [s for s in states if low in fname(s).lower()]
        if len(contains) == 1:
            return contains[0], []
        if len(contains) > 1:
            return None, contains
        byid = [s for s in states if low in str(s.get("entity_id", "")).lower()]
        if len(byid) == 1:
            return byid[0], []
        return None, byid
