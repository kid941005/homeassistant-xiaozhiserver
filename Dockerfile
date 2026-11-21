# homeassistant-xiaozhiserver — 小智 MCP 接入点 bridge 镜像
# 内容：stdio MCP server + WebSocket 桥接进程 + WebUI（8099）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY homeassistant_xiaozhiserver ./homeassistant_xiaozhiserver
COPY server.py ./server.py
COPY mcp_pipe.py ./mcp_pipe.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN useradd -m -u 10001 --no-log-init appuser && chmod 0755 /app/docker-entrypoint.sh
USER appuser

# HA_URL / HA_TOKEN / MCP_ENDPOINT 由运行时注入，不写入镜像
ENV HA_TIMEOUT=10 \
    HA_VERIFY_SSL=true \
    LOG_LEVEL=INFO

# WebUI 固定监听 8099
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/status', timeout=3)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
