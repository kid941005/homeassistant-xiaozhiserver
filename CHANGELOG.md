# Changelog

本项目的所有重要变更记录在此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

发布说明与镜像信息见 [GitHub Releases](https://github.com/kid941005/homeassistant-xiaozhiserver/releases)。

## [Unreleased]

暂无计划中的变更。

## [1.15.0] - 2026-11-25

安全与健壮性修复版本，重点处理 WebUI 暴露面、控制结果语义、HA 异常响应与部署加固。

### 安全

- WebUI 登录改为服务端随机会话 Cookie（HttpOnly、7 天有效、重启失效），并清理过期会话。
- 登录表单限制 16KB、工具调试 JSON 限制 64KB，超限返回 413。
- `/readyz` 公开探针脱敏为仅返回 `{"ok": bool}`，不再泄漏 HA 地址或 bridge 细节。
- 所有响应增加 `Cache-Control: no-store`；CSP 增加 `frame-ancestors`、`base-uri`、`form-action`。
- `/metrics` 未知路径归并，避免任意路径成为指标标签；端点掩码覆盖 URL 路径中的长 token。
- WebUI 危险检查的并发数限制为 5。

### 变更

- `control_device` / `control_devices` / `call_service` 仅在 `confirmed`，或 `no_change` 且已验证时返回 `ok=true`。
- `mismatch` 返回 `code=not_verified`，`accepted_pending` 返回 `code=pending_confirmation`。
- 批量控制改为全部成功才 `ok=true`；部分失败返回 `ok=false`、`partial` 与“另有 N 个设备未成功”的播报。
- 失败结果不再携带 `action=RESPONSE`。
- MCP server 使用独立 `*.mcp.log`（`LOG_FILE_MCP`），避免与 bridge 进程共用轮转文件。

### 修复

- HA 返回 3xx 或状态/服务目录不是 JSON 数组时显式报错，不再静默返回空列表。
- 状态、服务、注册表请求增加单飞锁，避免 TTL 到期并发重复拉取或重复建立 WebSocket。
- `stop_cover` 等无预期动作按 HA 返回的实际状态判定成功。
- 区域注册表不可用时显式返回 `registry_unavailable`，不再静默返回空列表。
- 工具异常也计入 metrics。
- bridge 状态文件使用唯一临时名、`fsync` 后原子替换。

### 部署

- 新增 `healthcheck.py`，依次尝试 `WEBUI_HOST` 与 `127.0.0.1` 的 `/healthz`。
- compose 增加 `cap_drop: ALL`、`read_only: true` 与 `/tmp` tmpfs。
- 发布镜像同时构建 `linux/amd64` 与 `linux/arm64`。

### 测试

- 全量离线测试通过：e2e、去重、状态缓存、bridge 状态、确认窗口、环境变量一致性、日志回退、WebUI 冒烟。
- WebUI 冒烟测试与真实 `.env` 隔离，避免误操作真实 Home Assistant。

### 升级注意

- 旧 WebUI 会话 Cookie 失效，升级后需重新登录。
- `/readyz` 现在只返回 `{"ok": bool}`。
- 依赖旧 `ok` 语义的脚本需要调整，批量部分失败现在返回 `ok=false`。

## [1.14.3] - 2026-11-24

### 新增

- 新增 MCP bridge 实际连接状态：`mcp_pipe` 定期写入 `.bridge-status.json`，`/api/status` 与 WebUI 首页展示 bridge 连接。
- 配置 `MCP_ENDPOINT` 时，`/readyz` 同时校验 bridge 是否已连接。

### 变更

- `_entity_locks` 改为弱引用字典，避免实体锁无界增长。
- 移除 `LOG_SPLIT`，固定 WebUI 与 bridge 分文件日志。

## [1.14.2] - 2026-11-24

### 修复

- MCP bridge 子进程正常退出后按退避重连，避免忙循环。
- `control_device` / `control_devices` / `call_service` 在 `mismatch` / `accepted_pending` 时返回 `ok=false`。
- WebUI 裸服务名按目标实体域判定高风险服务。
- 拒绝 NaN/Infinity 数值。
- Docker healthcheck 跟随 `WEBUI_HOST`。
- 畸形 Origin 失败关闭，不再返回 500。
- WebUI 状态/设备缓存增加单飞锁，危险检查并发执行。

## [1.14.1] - 2026-11-24

### 修复

- 日志时间戳加入毫秒，默认 `TZ=Asia/Shanghai`。
- 日志目录不可写时自动回退到 `/tmp/xiaozhi-logs`，只输出一条带时间戳的告警，不再刷 Python logging Traceback。
- 清理重复 root handler。

### 文档

- 新增 `TZ` 容器变量与日志目录权限说明。

## [1.14.0] - 2026-11-23

### 变更

- 架构重构：抽出 `settings.py` 统一配置、`devices.py`（DeviceService）业务层、`webui_page.py` 页面渲染。
- `server.py` 只保留 7 个工具薄包装，去掉 MCP 私有 API 依赖。
- WebUI 新增 `WEBUI_TOKEN` 登录、状态/设备接口短缓存、批量控制并发上限。
- 新增分进程日志、`LOG_FORMAT=json`、`/metrics`。
- 依赖精确锁定、镜像 digest 固定、`.env` 一致性测试。

## [1.13.8] - 2026-11-23

### 变更

- 部署配置整理：`.env.example` 收敛为 3 个必填项并补充默认值说明。
- `docker-compose.yml` 支持 `PUID`/`PGID` 覆盖运行用户，补充日志目录权限步骤。
- README 同步 host 网络、日志目录与 `.env` 说明。

## [1.13.7] - 2026-11-22

### 修复

- 修复慢设备回读窗口被 `min_attempts` 提前截断。
- 修复 `compact` 设备分组丢设备名。
- 修复注册表瞬时失败清空旧缓存。
- WebUI 危险闸门失败关闭，`call_service` 目标选择器防绕过。

### 新增

- 补充真实事件流、慢设备回读与批量上限测试。
- 新增 `WEBUI_HOST` / `WEBUI_PORT`、事件流失败熔断、`set_brightness 0` 映射关闭、Origin 默认端口归一化、日志尾部全量解析。

## [1.13.6] - 2026-11-22

### 新增

- 新增 `HA state_changed` 事件流与状态/服务缓存。
- 新增自适应回读确认、批量 `control_devices`、`list_devices compact`。
- 新增小智侧 MCP 重连与 action 白名单、首次连接失败自动重试、状态流退避重置、同实体控制串行化。
- 新增 WebUI Host/Origin 白名单、安全响应头、64KB 请求体上限、`/healthz`/`/readyz`、生效配置展示。
- 新增日志轮转与 compose 日志卷；CI 增加 PR 测试与版本一致性校验。

### 修复

- 去重前校验灯具可用性，注册表不可用兜底。

## [1.13.5] - 2026-11-21

### 新增

- 同一设备下与灯具同名且唯一的开关自动去重，只保留灯具。
- 新增 `duplicate_switch` 错误与 `deduplicated_switch_count` / `ambiguous_switch_groups`。

## [1.13.4] - 2026-11-21

### 变更

- 默认隐藏门铃、自动化、热水壶、中枢网关、媒体五类设备。
- 新增 `EXCLUDED_DOMAINS` / `EXCLUDED_DEVICE_KEYWORDS`。

## [1.13.3] - 2026-11-21

### 修复

- 全面审计修复：输入校验、脚本/系统级服务确认、回读与重试修正、日志脱敏与尾部读取、WebUI 同源校验、bridge 重连与 SIGTERM、健康检查、CI 测试。

## [1.13.2] - 2026-11-21

### 新增

- WebUI 页头、日志面板与日志行显示版本号。
- 日志格式加入版本号；`/api/logs` 返回版本。

## [1.13.1] - 2026-11-21

### 修复

- 控制结果改为 `action=RESPONSE` + 中文 `response`，修复 `turn_off` 被播报。

### 新增

- WebUI 去重、设备总览与共享日志。

## [1.13.0] - 2026-11-21

### 变更

- WebUI 与 bridge 合并为单容器。
- WebUI 去掉密码与 `WEBUI_*` 变量，当时固定 8099（v1.13.7 起可用 `WEBUI_HOST` / `WEBUI_PORT` 配置），复用 `HA_URL` / `HA_TOKEN`。

## [1.12.0] - 2026-11-21

### 新增

- 新增可选 WebUI：状态页、工具调试台、配置只读。

## [1.11.1] - 2026-11-21

### 修复

- 修复 `list_devices` 无过滤时忽略 `DEFAULT_LIST_LIMIT` 的问题。

## [1.11.0] - 2026-11-20

### 变更

- `list_devices` 支持关键字别名、未知参数报错、默认只列可控设备。

## [1.10.1] - 2026-11-20

### 变更

- 清理旧「方式 B」MCP 配置与冗余。

## [1.10.0] - 2026-11-20

### 变更

- 仅保留 stdio + WebSocket bridge，移除 HTTP/SSE/8090/health/ready/uvicorn。

## [1.9.1] - 2026-11-20

### 修复

- 审计修复：环境变量回退、HA URL 校验、注册表按 id 收集、回读失败标记。

## [1.9.0] - 2026-11-20

### 新增

- 新增区域名称返回与 `area` 过滤。

## [1.8.0] - 2026-11-20

### 变更

- 升级 MCP Python SDK 2.3.0，增加结构化日志与 `call_id`。

## [1.7.0] - 2026-11-20

### 新增

- 新增小智 MCP 接入点支持（`mcp_pipe.py`、bridge 镜像与 compose）。

## [1.6.0] - 2026-11-19

### 变更

- 统一项目/镜像/容器命名为 `homeassistant-xiaozhiserver`，README 重写。

## [1.5.0] - 2026-11-18

### 变更

- 拆分为独立 Python 包，配置集中解析，属性裁剪，注册表重试。

## [1.4.0] - 2026-11-18

### 新增

- 危险设备二次确认（一次性令牌）。

## [1.3.0] - 2026-11-18

### 新增

- 控制后状态 + 属性四态回读校验。

## [1.2.0] - 2026-11-18

### 新增

- 可控/危险标记，按物理设备聚合。

## [1.1.0] - 2026-11-18

### 新增

- 新增 `list_services` 与 `call_service`。

## [1.0.0] - 2026-11-18

### 新增

- 初始版本：设备列表、状态查询、常用控制与 Docker 部署。
