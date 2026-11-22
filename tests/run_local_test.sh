#!/usr/bin/env bash
# 一键离线自测：启动 fake HA，再由 e2e 客户端以 stdio 启动本服务，结束后自动清理。
# 用法：export TEST_TOKEN=任意本地测试口令 && bash tests/run_local_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-python}
: "${TEST_TOKEN:?请先 export TEST_TOKEN=任意本地测试口令（模拟器与本服务共用）}"

export HA_URL=http://127.0.0.1:8123
export HA_TOKEN="$TEST_TOKEN"
export FAKE_HA_TOKEN="$TEST_TOKEN"
# 注册表走 WebSocket（fake 的 WS 端口默认 8124）
export HA_WS_URL=ws://127.0.0.1:8124/api/websocket
export FAKE_HA_WS_PORT=8124

FAKE=""
cleanup() {
  if [ -n "$FAKE" ]; then
    kill "$FAKE" 2>/dev/null || true
    wait "$FAKE" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# 依赖与端口预检：避免旧实例占端口导致 e2e 打在脏状态上。
if ! "$PY" -c "import websockets" >/dev/null 2>&1; then
  echo "缺少 websockets 依赖，请先 pip install -r requirements.txt" >&2
  exit 1
fi
if "$PY" - <<'PY'
import socket, sys
busy = []
for port in (8123, 8124):
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        pass
    else:
        busy.append(port)
    finally:
        s.close()
if busy:
    print("端口已被占用：" + ",".join(str(p) for p in busy))
    sys.exit(1)
PY
then
  :
else
  echo "127.0.0.1:8123/8124 已被占用，请先停止旧的 fake_ha.py（例如 pkill -f fake_ha.py）" >&2
  exit 1
fi

"$PY" tests/fake_ha.py & FAKE=$!
sleep 0.3
if ! kill -0 "$FAKE" 2>/dev/null; then
  wait "$FAKE" 2>/dev/null || true
  echo "fake HA 启动失败，请查看上方错误输出。" >&2
  exit 1
fi

# 等待 fake HA 的 HTTP 与 WebSocket 端口都可连接（最多 15s）；注册表依赖 WS
READY=0
for _ in $(seq 1 60); do
  if "$PY" - <<'PY' >/dev/null 2>&1
import socket
for port in (8123, 8124):
    s = socket.create_connection(("127.0.0.1", port), timeout=1)
    s.close()
PY
  then
    READY=1
    break
  fi
  sleep 0.25
done
if [ "$READY" != "1" ]; then
  echo "fake HA 未在预期时间内启动（127.0.0.1:8123 或 8124 不可达）" >&2
  exit 1
fi

echo "===== e2e (stdio) ====="
"$PY" tests/e2e_mcp_client.py

echo "===== dedupe unit ====="
"$PY" tests/dedupe_switch_light.py

echo "===== state cache unit ====="
"$PY" tests/state_cache.py

echo "===== confirm window unit ====="
"$PY" tests/confirm_window.py

echo "===== webui smoke ====="
"$PY" tests/webui_smoke.py
