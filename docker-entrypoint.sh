#!/bin/sh
# 单容器入口：WebUI 后台运行，MCP WebSocket bridge 前台运行。
# 任一进程退出都会结束容器，交给 restart 策略重建；退出时最多等 10 秒再强杀。
set -eu

python -m homeassistant_xiaozhiserver.webui &
WEBUI_PID=$!

python /app/mcp_pipe.py /app/server.py &
BRIDGE_PID=$!

cleanup() {
    for pid in "$BRIDGE_PID" "$WEBUI_PID"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    i=0
    while [ "$i" -lt 10 ]; do
        alive=0
        for pid in "$BRIDGE_PID" "$WEBUI_PID"; do
            if kill -0 "$pid" 2>/dev/null; then
                alive=1
            fi
        done
        if [ "$alive" -eq 0 ]; then
            break
        fi
        i=$((i + 1))
        sleep 1
    done
    for pid in "$BRIDGE_PID" "$WEBUI_PID"; do
        kill -KILL "$pid" 2>/dev/null || true
    done
    wait "$BRIDGE_PID" 2>/dev/null || true
    wait "$WEBUI_PID" 2>/dev/null || true
}

trap 'cleanup; exit 143' TERM
trap 'cleanup; exit 130' INT

while kill -0 "$BRIDGE_PID" 2>/dev/null && kill -0 "$WEBUI_PID" 2>/dev/null; do
    sleep 2
done

STATUS=0
if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    wait "$BRIDGE_PID" || STATUS=$?
else
    wait "$WEBUI_PID" || STATUS=$?
fi
# 任一子进程异常退出都以非 0 结束，交给 restart 策略重建；0 会被视为正常结束。
if [ "$STATUS" -eq 0 ]; then
    STATUS=1
fi
cleanup
exit "$STATUS"
