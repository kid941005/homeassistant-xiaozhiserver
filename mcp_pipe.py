"""
小智 MCP 接入点桥接：WebSocket（工具端）<-> stdio MCP server。

用法：
    export MCP_ENDPOINT="ws://<宿主>:8004/mcp_endpoint/mcp/?token=<TOKEN>"
    python mcp_pipe.py server.py

Docker 中由 CMD 直接执行 python /app/mcp_pipe.py /app/server.py。
本进程只负责搬运 MCP JSON-RPC 消息，不解析业务协议。
"""

import asyncio
import logging
import os
import random
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import websockets
from dotenv import load_dotenv

load_dotenv()

try:
    from homeassistant_xiaozhiserver.logging_setup import setup_logging
    setup_logging()
except Exception:  # pragma: no cover - 独立运行时退回基础配置
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
try:
    from homeassistant_xiaozhiserver import __version__ as _bridge_version
except Exception:  # pragma: no cover - 独立运行时无包
    _bridge_version = "unknown"
try:
    from homeassistant_xiaozhiserver.bridge_status import write_status as _write_bridge_status
except Exception:  # pragma: no cover - 独立运行时无包
    _write_bridge_status = None
logger = logging.getLogger("MCP_PIPE")
logger.info("bridge version=%s", _bridge_version)

INITIAL_BACKOFF = 1
MAX_BACKOFF = 600
TERMINATE_TIMEOUT = 5
BRIDGE_STATUS_INTERVAL = 5.0


class MCPPipeClosed(RuntimeError):
    """MCP server 管道正常结束（进程退出或连接关闭），需要按断线退避重连。"""


def _safe_endpoint(raw: str) -> str:
    """日志脱敏：去掉 token/query 与 userinfo。"""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "(invalid)"
    if not parts.scheme or not parts.netloc:
        return "(invalid)"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _new_bridge_state() -> dict:
    return {
        "connected": False,
        "connected_at": None,
        "attempt": 0,
        "last_error": None,
        "server_pid": None,
    }


def _publish_bridge_status(uri: str, state: dict) -> None:
    """把当前状态写入共享文件；失败只记 debug，不影响桥接。"""
    if _write_bridge_status is None:
        return
    payload = {
        "pid": os.getpid(),
        "version": _bridge_version,
        "endpoint": _safe_endpoint(uri),
        "connected": bool(state.get("connected")),
        "connected_at": state.get("connected_at"),
        "attempt": int(state.get("attempt") or 0),
        "last_error": str(state.get("last_error") or ""),
        "server_pid": state.get("server_pid"),
    }
    try:
        _write_bridge_status(payload)
    except Exception:  # noqa: BLE001 - 状态文件失败不影响主流程
        logger.debug("写入 bridge 状态失败", exc_info=True)


async def _bridge_status_heartbeat(uri: str, state: dict) -> None:
    while True:
        _publish_bridge_status(uri, state)
        await asyncio.sleep(BRIDGE_STATUS_INTERVAL)


def build_server_command(script_path: str | None = None):
    """返回启动本地 MCP server 的命令与环境变量。"""
    if script_path is None:
        script_path = sys.argv[1] if len(sys.argv) >= 2 else "server.py"
    if not os.path.isfile(script_path):
        raise RuntimeError("MCP server 脚本不存在：" + script_path)
    return [sys.executable, script_path], os.environ.copy()


def _terminate(process: subprocess.Popen, target: str) -> None:
    if process.poll() is not None:
        return
    logger.info("[%s] 正在停止 MCP server", target)
    process.terminate()
    try:
        process.wait(timeout=TERMINATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    logger.info("[%s] MCP server 已停止", target)


async def pipe_websocket_to_process(websocket, process, target: str) -> None:
    """WebSocket -> server stdin。"""
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            if not message.endswith("\n"):
                message += "\n"
            process.stdin.write(message)
            process.stdin.flush()
    except Exception as e:
        logger.error("[%s] WebSocket -> stdio 失败：%s", target, e)
        raise
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()


async def pipe_process_to_websocket(process, websocket, target: str) -> None:
    """server stdout -> WebSocket。"""
    try:
        while True:
            line = await asyncio.to_thread(process.stdout.readline)
            if not line:
                logger.info("[%s] MCP server stdout 已结束", target)
                return
            await websocket.send(line)
    except Exception as e:
        logger.error("[%s] stdio -> WebSocket 失败：%s", target, e)
        raise


async def pipe_process_stderr(process, target: str) -> None:
    """server stderr -> 当前进程 stderr（容器日志可见）。"""
    try:
        while True:
            line = await asyncio.to_thread(process.stderr.readline)
            if not line:
                return
            sys.stderr.write(line)
            sys.stderr.flush()
    except Exception as e:
        logger.error("[%s] 读取 MCP server stderr 失败：%s", target, e)
        raise


async def connect_to_server(uri: str, script_path: str, target: str, state: dict | None = None) -> None:
    """连接接入点并启动一次 MCP server；连接断开后由外层重连。"""
    state = state if state is not None else _new_bridge_state()
    logger.info("[%s] 正在连接 MCP 接入点 %s", target, _safe_endpoint(uri))
    async with websockets.connect(
        uri,
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
        open_timeout=15,
    ) as websocket:
        logger.info("[%s] MCP接入点连接成功", target)

        cmd, env = build_server_command(script_path)
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            text=True,
            bufsize=1,
            env=env,
        )
        logger.info("[%s] 已启动 MCP server：%s", target, " ".join(cmd))
        state.update(connected=True, connected_at=time.time(), last_error=None, server_pid=process.pid)
        _publish_bridge_status(uri, state)

        tasks = [
            asyncio.create_task(pipe_websocket_to_process(websocket, process, target)),
            asyncio.create_task(pipe_process_to_websocket(process, websocket, target)),
            asyncio.create_task(pipe_process_stderr(process, target)),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
            # 所有管道任务都正常结束（子进程退出或对端关闭连接）也必须按断线处理，
            # 否则 connect_with_retry 会立刻无退避重连，形成忙循环。
            raise MCPPipeClosed("MCP server 管道已结束（进程退出或连接关闭）")
        finally:
            state.update(connected=False, connected_at=None, server_pid=None)
            _publish_bridge_status(uri, state)
            _terminate(process, target)


async def connect_with_retry(uri: str, script_path: str, target: str) -> None:
    """无限重连；每次重连都会重新启动 MCP server。"""
    attempt = 0
    backoff = INITIAL_BACKOFF
    state = _new_bridge_state()
    heartbeat = asyncio.create_task(_bridge_status_heartbeat(uri, state))
    try:
        while True:
            # 在 try 外赋值，保证异常路径也能安全计算连接时长。
            started = time.monotonic()
            try:
                if attempt > 0:
                    logger.info("[%s] %ss 后进行第 %s 次重连", target, backoff, attempt)
                    await asyncio.sleep(backoff * (0.5 + random.random()))
                state["attempt"] = attempt
                await connect_to_server(uri, script_path, target, state)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 连接稳定运行过一段时间后再断开，重置退避，避免长期运行后从 600s 开始重连。
                if time.monotonic() - started > 60:
                    attempt = 0
                    backoff = INITIAL_BACKOFF
                attempt += 1
                state.update(connected=False, connected_at=None, server_pid=None)
                state["attempt"] = attempt
                state["last_error"] = type(e).__name__
                _publish_bridge_status(uri, state)
                logger.warning("[%s] 连接断开（第 %s 次）：%s", target, attempt, e)
                backoff = min(backoff * 2, MAX_BACKOFF)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        state.update(connected=False, connected_at=None, server_pid=None)
        _publish_bridge_status(uri, state)


def signal_handler(sig, frame) -> None:
    logger.info("收到退出信号，正在关闭...")
    sys.exit(0)


def main() -> None:
    endpoint = (os.environ.get("MCP_ENDPOINT") or "").strip()
    if not endpoint:
        logger.error("请设置 MCP_ENDPOINT 环境变量（小智 MCP 接入点的工具端 WebSocket 地址）")
        raise SystemExit(1)
    if not endpoint.startswith(("ws://", "wss://")):
        logger.error("MCP_ENDPOINT 必须以 ws:// 或 wss:// 开头，当前为：%s", _safe_endpoint(endpoint))
        raise SystemExit(1)

    script_path = sys.argv[1] if len(sys.argv) >= 2 else "server.py"
    if not os.path.isfile(script_path):
        logger.error("MCP server 脚本不存在：%s", script_path)
        raise SystemExit(1)
    target = os.path.basename(script_path)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(connect_with_retry(endpoint, script_path, target))
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except SystemExit:
        raise
    except Exception as e:
        logger.error("程序异常退出：%s", e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
