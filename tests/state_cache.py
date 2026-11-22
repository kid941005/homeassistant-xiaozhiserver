#!/usr/bin/env python3
"""状态与服务缓存的自测：不联网，用假 _request 统计 REST 调用次数。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant_xiaozhiserver.client import HAClient  # noqa: E402


def make_client() -> HAClient:
    ha = HAClient("http://127.0.0.1:8123", "token")
    ha.ws_enabled = False
    ha.state_ws_enabled = False
    ha.state_cache_ttl = 30.0
    ha.services_ttl = 30.0
    return ha


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print("[ok] " + label)


async def run() -> None:
    ha = make_client()
    calls = {"states": 0, "services": 0, "service": 0}
    states = [
        {"entity_id": "light.a", "state": "off", "attributes": {}},
        {"entity_id": "switch.b", "state": "on", "attributes": {}},
    ]

    async def fake_request(method: str, path: str, **kwargs):
        if path == "/api/states":
            calls["states"] += 1
            return [dict(s) for s in states]
        if path == "/api/services":
            calls["services"] += 1
            return [{"domain": "light", "services": {"turn_on": {}}}]
        if method == "POST":
            calls["service"] += 1
            return None
        raise AssertionError("unexpected path " + path)

    ha._request = fake_request  # type: ignore[method-assign]

    first = await ha.get_states()
    second = await ha.get_states()
    check(len(first) == 2 and len(second) == 2, "get_states 返回全部实体")
    check(calls["states"] == 1, "get_states 命中短 TTL 缓存，只请求一次 REST")
    await ha.get_states(force=True)
    check(calls["states"] == 2, "get_states(force=True) 绕过缓存")

    await ha.get_services()
    await ha.get_services()
    check(calls["services"] == 1, "get_services 命中缓存，只请求一次 REST")

    await ha.call_service("light", "turn_on", {"entity_id": "light.a"})
    check(calls["service"] == 1, "call_service 已下发")
    await ha.get_states()
    check(calls["states"] == 3, "服务调用后状态缓存失效，重新拉取")

    lock_a = ha.entity_lock("light.a")
    check(lock_a is ha.entity_lock("light.a"), "同一实体复用同一把控制锁")
    check(lock_a is not ha.entity_lock("light.b"), "不同实体使用不同控制锁")
    order = []

    async def locked_worker(name: str) -> None:
        async with ha.entity_lock("switch.b"):
            order.append(name + "-start")
            await asyncio.sleep(0.01)
            order.append(name + "-end")

    await asyncio.gather(locked_worker("one"), locked_worker("two"))
    check(
        order[0].endswith("start") and order[1].endswith("end")
        and order[2].endswith("start") and order[3].endswith("end"),
        "同一实体并发控制被锁串行化",
    )

    ha._state_ws_ready.set()
    ha._store_states([{"entity_id": "light.a", "state": "on", "attributes": {}}])
    before = calls["states"]
    cached = await ha.get_states()
    check(len(cached) == 1 and calls["states"] == before, "事件流就绪时 get_states 走内存缓存")

    await ha.aclose()

    # 真实 WS 事件流：本地起一个最小 HA WebSocket 服务端，验证订阅、事件解析、
    # 缓存落盘与 ready 置位，而不是手工 set() 绕过整条链路。
    async def ws_handler(ws) -> None:
        await ws.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(await ws.recv())
        check(auth.get("type") == "auth", "事件流先发 auth")
        await ws.send(json.dumps({"type": "auth_ok"}))
        sub = json.loads(await ws.recv())
        check(sub.get("type") == "subscribe_events" and sub.get("event_type") == "state_changed",
              "事件流订阅 state_changed")
        await ws.send(json.dumps({"id": 10, "type": "result", "success": True, "result": None}))
        await asyncio.sleep(0.1)
        await ws.send(json.dumps({
            "id": 11, "type": "event",
            "event": {"event_type": "state_changed", "data": {
                "entity_id": "light.a",
                "old_state": {"entity_id": "light.a", "state": "off", "attributes": {}},
                "new_state": {"entity_id": "light.a", "state": "on", "attributes": {}},
            }},
        }))
        await asyncio.sleep(2)

    server = await serve(ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    ha2 = make_client()
    ha2.ws_enabled = True
    ha2.state_ws_enabled = True
    ha2.ws_url = "ws://127.0.0.1:" + str(port)
    rest_calls = {"states": 0}

    async def fake_request2(method: str, path: str, **kwargs):
        if path == "/api/states":
            rest_calls["states"] += 1
            return [{"entity_id": "light.a", "state": "off", "attributes": {}}]
        raise AssertionError("unexpected path " + path)

    ha2._request = fake_request2  # type: ignore[method-assign]
    try:
        ha2._ensure_state_stream()
        await asyncio.wait_for(ha2._state_ws_ready.wait(), 3.0)
        check(rest_calls["states"] == 1, "事件流就绪前只拉一次全量快照")
        changed = await ha2.wait_for_state("light.a", lambda s: s.get("state") == "on", timeout=2.0)
        check(isinstance(changed, dict) and changed.get("state") == "on", "真实 state_changed 事件被解析")
        cached = await ha2.get_states()
        check(len(cached) == 1 and cached[0].get("state") == "on" and rest_calls["states"] == 1,
              "事件写入内存缓存且不再发 REST")
    finally:
        await ha2.aclose()
        server.close()
        await server.wait_closed()

    # 事件流连续失败熔断：避免 HA 长期不可用时每 30s 刷一次告警。
    ha3 = make_client()
    ha3.ws_enabled = True
    ha3.state_ws_enabled = True
    ha3.ws_url = "ws://127.0.0.1:1"
    ha3.timeout = 0.2
    ha3._state_ws_max_failures = 2
    ha3._ensure_state_stream()
    try:
        await asyncio.wait_for(ha3._state_ws_task, 10.0)
        check(ha3._state_ws_failures >= 2, "事件流连续失败计数")
        check(ha3._state_ws_retry_after > time.monotonic(), "事件流连续失败后进入熔断")
        check(ha3._ensure_state_stream() is False, "熔断期内不重启事件流")
    finally:
        await ha3.aclose()
    print("STATE CACHE OK")


if __name__ == "__main__":
    asyncio.run(run())
