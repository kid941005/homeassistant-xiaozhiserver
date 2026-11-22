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

RUN useradd -m -u 10001 --no-log-init appuser \
    && chmod 0755 /app/docker-entrypoint.sh \
    && install -d -o appuser -g appuser /app/logs
USER appuser

# HA_URL / HA_TOKEN / MCP_ENDPOINT 由运行时注入，不写入镜像
ENV HA_TIMEOUT=10 \
    HA_VERIFY_SSL=true \
    LOG_LEVEL=INFO

# WebUI 默认监听 8099，可用 WEBUI_PORT 覆盖；EXPOSE 仅作默认声明。
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('WEBUI_PORT','8099'); urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=3)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
