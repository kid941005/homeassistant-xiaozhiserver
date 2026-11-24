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
import random
import re
import ssl
import time
import weakref
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import HA_STATE_WS_MAX_FAILURES, env_bool, env_float

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
        self._registry_error_ts: float = 0.0
        self._http: httpx.AsyncClient | None = None
        # 状态/服务缓存与长期 state_changed 事件流：减少每次工具调用拉全量 /api/states。
        self.state_ws_enabled = env_bool("HA_STATE_WS_ENABLED", True)
        self.state_cache_ttl = env_float("HA_STATE_CACHE_TTL", 1.0, 0.0, 60.0)
        self.services_ttl = env_float("HA_SERVICES_CACHE_TTL", 300.0, 0.0, 3600.0)
        self._state_entities: dict[str, dict] = {}
        self._states_cache: list[dict] | None = None
        self._states_ts: float = 0.0
        # 事件只标脏，读取时再一次性生成 list，避免每条 state_changed 都重建全量列表。
        self._states_dirty: bool = True
        self._services_cache: list[dict] | None = None
        self._services_ts: float = 0.0
        self._state_ws_task: asyncio.Task | None = None
        self._state_ws_ready = asyncio.Event()
        self._state_ws_stop = asyncio.Event()
        # 事件流失败熔断：连续失败 max_failures 次后暂停 60s，避免 HA 长期不可用时持续告警。
        self._state_ws_failures = 0
        self._state_ws_max_failures = HA_STATE_WS_MAX_FAILURES
        self._state_ws_retry_after = 0.0
        self._state_ws_established = False
        self._state_waiters: dict[str, list[tuple[asyncio.Future, Any]]] = {}
        self._entity_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
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
        """释放连接池与状态事件流（进程退出或测试收尾时调用）。"""
        self._state_ws_stop.set()
        if self._state_ws_task is not None:
            self._state_ws_task.cancel()
            try:
                await self._state_ws_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.debug("关闭 HA state stream 时出现异常", exc_info=True)
            self._state_ws_task = None
        self._state_ws_ready.clear()
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

    # ---------------- 状态缓存与事件流 ----------------
    def state_stream_ready(self) -> bool:
        """HA WebSocket state_changed 事件流是否已就绪。"""
        return self._state_ws_ready.is_set()

    def entity_lock(self, entity_id: str) -> asyncio.Lock:
        """同一实体的控制串行化，避免并发下发/回读互相覆盖。"""
        lock = self._entity_locks.get(entity_id)
        if lock is None:
            lock = asyncio.Lock()
            self._entity_locks[entity_id] = lock
        return lock

    def _ensure_state_stream(self) -> bool:
        """惰性启动长期状态事件流；事件流不可用时返回 False，调用方回退 REST。"""
        if not self.ws_enabled or not self.state_ws_enabled or websockets is None:
            return False
        if self._state_ws_task is not None and not self._state_ws_task.done():
            return True
        # 熔断期内直接回退 REST，由下一次工具调用在冷却结束后重新尝试。
        if time.monotonic() < self._state_ws_retry_after:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._state_ws_stop.clear()
        self._state_ws_failures = 0
        self._state_ws_task = loop.create_task(self._state_stream_loop())
        return True

    async def _fetch_states_rest(self) -> list[dict]:
        data = await self._request("GET", "/api/states")
        return data if isinstance(data, list) else []

    def _store_states(self, states: list[dict]) -> None:
        self._state_entities = {
            str(s.get("entity_id")): s
            for s in states
            if isinstance(s, dict) and s.get("entity_id")
        }
        self._states_dirty = True
        self._states_ts = time.time()

    def _states_snapshot(self) -> list[dict]:
        """按需生成全量 list：只在事件更新/失效后重建一次。"""
        if self._states_dirty or self._states_cache is None:
            self._states_cache = list(self._state_entities.values())
            self._states_dirty = False
        return self._states_cache

    def invalidate_states(self) -> None:
        """服务调用后让状态缓存失效；事件流就绪时保留内存快照，由事件增量更新。"""
        self._states_ts = 0.0
        self._states_dirty = True
        if not self._state_ws_ready.is_set():
            self._states_cache = None
            self._state_entities = {}

    def _notify_state(self, entity_id: str, state: dict | None) -> None:
        items = self._state_waiters.get(entity_id)
        if not items:
            return
        keep: list = []
        for fut, predicate in items:
            if fut.done():
                continue
            try:
                matched = bool(predicate(state))
            except Exception:  # noqa: BLE001 等待者断言异常不应拖垮事件流
                matched = False
            if matched:
                fut.set_result(state)
            else:
                keep.append((fut, predicate))
        if keep:
            self._state_waiters[entity_id] = keep
        else:
            self._state_waiters.pop(entity_id, None)

    def _fail_state_waiters(self) -> None:
        for items in list(self._state_waiters.values()):
            for fut, _predicate in items:
                if not fut.done():
                    fut.set_result(None)
        self._state_waiters.clear()

    async def wait_for_state(self, entity_id: str, predicate: Any = None,
                             timeout: float = 1.0) -> dict | None:
        """等待某个实体的下一次 state_changed。predicate 返回真才返回；
        超时、事件流不可用或断线返回 None，由调用方回退 REST 轮询。"""
        eid = str(entity_id or "").strip()
        if not self._ensure_state_stream() or not self._state_ws_ready.is_set():
            return None
        pred = predicate or (lambda _state: True)
        current = self._state_entities.get(eid)
        if isinstance(current, dict):
            try:
                if pred(current):
                    return current
            except Exception:  # noqa: BLE001
                pass
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._state_waiters.setdefault(eid, []).append((fut, pred))
        try:
            return await asyncio.wait_for(fut, timeout=max(0.01, float(timeout)))
        except asyncio.TimeoutError:
            return None
        finally:
            items = self._state_waiters.get(eid) or []
            kept = [(f, p) for f, p in items if f is not fut]
            if kept:
                self._state_waiters[eid] = kept
            else:
                self._state_waiters.pop(eid, None)

    async def _state_stream_loop(self) -> None:
        backoff = 1.0
        while not self._state_ws_stop.is_set():
            started = time.monotonic()
            self._state_ws_established = False
            try:
                await self._state_stream_connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("HA state stream 断开：%s", e)
            finally:
                self._state_ws_ready.clear()
                self._fail_state_waiters()
            if self._state_ws_stop.is_set():
                break
            # 只有“建立订阅并稳定运行 >=60s”才算成功；建立后立刻断开仍计为失败，
            # 避免 HA 反复接受后立刻关闭时形成 1s 级重连风暴。
            if self._state_ws_established and time.monotonic() - started >= 60.0:
                self._state_ws_failures = 0
                self._state_ws_retry_after = 0.0
                backoff = 1.0
            else:
                self._state_ws_failures += 1
                if self._state_ws_failures >= self._state_ws_max_failures:
                    self._state_ws_retry_after = time.monotonic() + 60.0
                    log.warning(
                        "HA state stream 连续 %s 次失败，暂停自动重连 60s，期间回退 REST 轮询",
                        self._state_ws_failures,
                    )
                    return
            # 连接稳定较久后重置退避，避免历史上多次失败导致后续重连仍等 30 秒。
            if time.monotonic() - started >= 60.0:
                backoff = 1.0
            await asyncio.sleep(backoff * (0.75 + random.random() * 0.5))
            backoff = min(backoff * 2.0, 30.0)

    async def _state_stream_connect(self) -> None:
        kwargs: dict = {
            "open_timeout": self.timeout,
            "close_timeout": 2,
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": 16 * 1024 * 1024,
        }
        if self.ws_url.startswith("wss://"):
            kwargs["ssl"] = self._ws_ssl()
        async with websockets.connect(self.ws_url, **kwargs) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.timeout))
            if hello.get("type") == "auth_required":
                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=self.timeout))
                if auth.get("type") != "auth_ok":
                    raise HAError("auth_failed", "Home Assistant WebSocket 鉴权失败：请检查 HA_TOKEN 是否有效。")
            elif hello.get("type") != "auth_ok":
                raise HAError("ws_error", "Home Assistant WebSocket 握手异常：" + str(hello.get("type")))
            await ws.send(json.dumps({"id": 10, "type": "subscribe_events", "event_type": "state_changed"}))
            deadline = time.monotonic() + max(self.timeout * 2, 5.0)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                if isinstance(msg, dict) and msg.get("type") == "result" and msg.get("id") == 10:
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        raise HAError("ws_error", "订阅 Home Assistant 状态事件失败：" + str(err.get("message") or err))
                    break
            # 先订阅再拉一次全量快照，保证事件流就绪时缓存完整，且不丢订阅后到达的事件。
            self._store_states(await self._fetch_states_rest())
            self._state_ws_established = True
            self._state_ws_ready.set()
            log.info("HA state stream 已连接，已订阅 state_changed（%s 个实体）", len(self._state_entities))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "event":
                    continue
                event = msg.get("event") or {}
                if event.get("event_type") != "state_changed":
                    continue
                data = event.get("data") or {}
                old_state = data.get("old_state")
                new_state = data.get("new_state")
                eid = (new_state or {}).get("entity_id") or (old_state or {}).get("entity_id")
                if not eid:
                    continue
                if isinstance(new_state, dict):
                    self._state_entities[eid] = new_state
                else:
                    self._state_entities.pop(eid, None)
                self._states_dirty = True
                self._states_ts = time.time()
                self._notify_state(eid, new_state)
            raise HAError("ws_closed", "Home Assistant 状态事件连接已关闭。")

    # ---------------- 上层 API ----------------
    async def get_states(self, force: bool = False) -> list:
        """返回全量实体状态；事件流就绪时直接读内存，否则按短 TTL 缓存，避免重复拉全量。"""
        self._ensure_state_stream()
        if self._state_ws_ready.is_set() and not force:
            return list(self._states_snapshot())
        now = time.time()
        if (not force and self._states_cache is not None
                and now - self._states_ts < self.state_cache_ttl):
            return list(self._states_snapshot())
        self._store_states(await self._fetch_states_rest())
        return list(self._states_snapshot())

    async def get_services(self, force: bool = False) -> list:
        """GET /api/services，返回服务目录（每个 domain 及其服务与参数字段），带 TTL 缓存。"""
        now = time.time()
        if (not force and self._services_cache is not None
                and now - self._services_ts < self.services_ttl):
            return list(self._services_cache)
        data = await self._request("GET", "/api/services")
        self._services_cache = data if isinstance(data, list) else []
        self._services_ts = now
        return list(self._services_cache)

    async def get_state(self, entity_id: str, force: bool = False) -> dict | None:
        eid = str(entity_id or "").strip()
        if not is_entity_id(eid):
            raise HAError("invalid_entity", "entity_id 格式不合法：" + eid + "。应为 domain.object_id，如 light.living_room。")
        self._ensure_state_stream()
        if self._state_ws_ready.is_set() and not force:
            cached = self._state_entities.get(eid)
            if isinstance(cached, dict):
                return cached
        try:
            data = await self._request("GET", "/api/states/" + eid)
        except HAError as e:
            if e.code == "not_found":
                return None
            raise
        if not isinstance(data, dict):
            raise HAError("invalid_response", "Home Assistant 返回的实体状态不是 JSON 对象。")
        self._state_entities[eid] = data
        if self._states_cache is not None:
            self._states_dirty = True
            self._states_ts = time.time()
        return data

    async def call_service(self, domain: str, service: str, data: dict) -> Any:
        dom = str(domain or "").strip()
        svc = str(service or "").strip()
        if not _SERVICE_NAME_RE.match(dom) or not _SERVICE_NAME_RE.match(svc):
            raise HAError("invalid_service", "服务名格式不合法：" + dom + "." + svc + "。只允许小写字母、数字与下划线。")
        result = await self._request("POST", "/api/services/" + dom + "/" + svc, json=data)
        if result is not None and not isinstance(result, (dict, list)):
            raise HAError("invalid_response", "Home Assistant 返回的服务调用结果不是 JSON 对象/数组。")
        self.invalidate_states()
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
        # 失败只短节流，且不清空仍可用的旧缓存；避免一次抖动让设备名/区域降级 60 秒。
        if (not force and self._registry_error_ts
                and now - self._registry_error_ts < self.registry_error_ttl):
            return self._registry_cache
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
        if data is None:
            # 失败时保留旧缓存，只记录失败时间做短节流。
            self._registry_error_ts = now
            if self._registry_cache is None:
                self._registry_ts = now
            return self._registry_cache
        self._registry_cache = data
        self._registry_ts = now
        self._registry_error_ts = 0.0
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

        返回 (entity, candidates, states)：
        - entity 非空：唯一命中
        - entity 为空且 candidates 非空：多个候选（歧义），需调用方消歧
        - 两者皆空：未找到
        - states 为按名称解析时已拉取的全量状态，供调用方复用；直接 entity_id 命中时为 None
        """
        ref = (ref or "").strip()
        if not ref:
            return None, [], None
        # 1) 直接当作 entity_id；只接受合法格式，避免把用户输入拼进 URL 路径
        if is_entity_id(ref):
            st = await self.get_state(ref)
            if st is not None:
                return st, [], None
        states = await self.get_states()
        low = ref.lower()

        def fname(s: dict) -> str:
            return str((s.get("attributes") or {}).get("friendly_name", ""))

        exact = [s for s in states if fname(s).lower() == low]
        if len(exact) == 1:
            return exact[0], [], states
        if len(exact) > 1:
            return None, exact, states
        contains = [s for s in states if low in fname(s).lower()]
        if len(contains) == 1:
            return contains[0], [], states
        if len(contains) > 1:
            return None, contains, states
        byid = [s for s in states if low in str(s.get("entity_id", "")).lower()]
        if len(byid) == 1:
            return byid[0], [], states
        return None, byid, states
