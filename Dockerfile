# homeassistant-xiaozhiserver — 小智 MCP 接入点 bridge 镜像
# 内容：stdio MCP server + WebSocket 桥接进程 + WebUI（8099）。
# 基础镜像固定 digest，避免上游 tag 漂移；升级时显式更新 digest 并重跑测试。
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY homeassistant_xiaozhiserver ./homeassistant_xiaozhiserver
COPY server.py ./server.py
COPY mcp_pipe.py ./mcp_pipe.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY healthcheck.py /app/healthcheck.py

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

HEALTHCHECK --interval=30s --timeout=20s --start-period=60s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
