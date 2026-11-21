# homeassistant-xiaozhiserver

> 把 **Home Assistant** 的智能家居设备封装成标准 **MCP（Model Context Protocol）** 工具，
> 供**小智（xiaozhi-server）**通过 **MCP 接入点**用自然语言查询与控制，支持 Docker 一键部署。

当前版本：**v1.13.2** · Python 3.12 · 传输：**stdio**（由 `mcp_pipe.py` 桥接小智 MCP 接入点 WebSocket）

---

## 目录

1. [功能特性](#1-功能特性)
2. [架构与数据流](#2-架构与数据流)
3. [目录结构](#3-目录结构)
4. [环境变量](#4-环境变量)
5. [快速开始](#5-快速开始)
6. [MCP 工具](#6-mcp-工具)
7. [接入小智后端（MCP 接入点）](#7-接入小智后端mcp-接入点)
8. [自然语言使用示例](#8-自然语言使用示例)
9. [错误处理与排查](#9-错误处理与排查)
10. [离线测试](#10-离线测试)
11. [设备规模与性能](#11-设备规模与性能)
12. [安全建议](#12-安全建议)
13. [版本记录](#13-版本记录)

---

## 1. 功能特性

| 能力 | 说明 |
|---|---|
| 六个 MCP 工具 | `list_devices`、`get_device_state`、`control_device`、`list_services`、`call_service`、`list_domains`，名称、描述与参数均面向自然语言路由设计 |
| 小智 MCP 接入点 | 只保留 **stdio + WebSocket bridge** 一种接入方式：`mcp_pipe.py` 主动连接 `mcp-endpoint-server` 的工具端地址，无需公网暴露本服务端口 |
| 设备发现与状态 | 支持按名称/entity_id 模糊查询，按 `domain`、`area` 过滤且条件可组合；状态查询默认裁剪为关键属性，避免上下文膨胀 |
| 常用控制 | 开关、亮度、音量、温度、色温、颜色、窗帘、门锁等常用动作，按设备 domain 自动映射到对应 HA 服务 |
| 能力自描述 | `list_services` 查询某类或某个设备支持哪些服务及参数字段，先看能力再调用 |
| 通用服务调用 | `call_service` 可执行任意 HA 服务，覆盖 `control_device` 未封装的能力 |
| 控制后回读校验 | 调用后轮询回读状态与属性，返回 `confirmed` / `no_change` / `mismatch` / `accepted_pending` 四态，避免静默假成功 |
| 危险设备二次确认 | 门锁、安防面板、车库门/门窗类 cover 首次调用只返回一次性 `confirm_token`，用户确认后再次调用才执行 |
| 可控与危险标记 | 每个实体带 `controllable`、`actions`、`dangerous`、`danger_reason`；`list_domains` 汇总危险实体 |
| 按物理设备聚合 | `group_by="device"` 通过 HA WebSocket 注册表把同一设备的多个实体合并展示；注册表不可用时自动降级为按实体展示 |
| 健壮错误处理 | 所有工具返回结构化 JSON；失败给 `code` / `message` / `hint`，不抛异常堆栈、不使服务崩溃 |
| 配置安全 | `HA_URL` / `HA_TOKEN` / `MCP_ENDPOINT` 只从环境变量读取，不写入镜像或代码 |
| 离线可测 | 自带 fake HA 模拟器与 stdio 端到端测试，无需真实 Home Assistant 即可验证全部工具 |
| WebUI | 随服务启动：运行状态、按区域/类型汇总的可调用设备总览、WebUI 与 bridge 共享的服务日志；页头、日志面板与每条日志均标注版本号；高级工具调试台与运行参数折叠展示；固定 `8099`，无独立密码，直接使用 `HA_URL` / `HA_TOKEN`，敏感值仅掩码展示 |

---

## 2. 架构与数据流

```
用户语音
  └─> 小智后端（ASR → LLM → function calling）
        └─> mcp-endpoint-server（MCP 接入点，工具端 WebSocket）
              └─> mcp_pipe.py（WebSocket ↔ stdio 桥接，主动连出）
                    └─> server.py（stdio MCP server）
                          ├─> Home Assistant REST API
                          │     ├─ GET  /api/states                 读取实体状态
                          │     ├─ GET  /api/services               读取服务目录
                          │     └─ POST /api/services/{domain}/{service}  执行控制
                          └─> Home Assistant WebSocket API
                                └─ /api/websocket（设备/实体注册表，用于按设备聚合）
                                      └─> 真实设备
```

一次典型调用：

1. 小智后端通过 MCP 接入点向桥接进程发送 `initialize` / `tools/list`，桥接进程转发给 stdio MCP server；
2. 小智后端把 6 个工具定义并入 LLM 的 function 列表；
3. 用户说话 → ASR 转文本 → LLM 选择工具与参数；
4. 小智后端 `tools/call` → 桥接进程 → MCP server → HA REST/WS → 结构化结果原路返回；
5. LLM 根据结果生成自然语言回复。

> 本服务不持久化业务数据。设备/实体注册表是进程内 TTL 缓存，危险确认令牌也是单进程内存，因此默认按**单实例**部署；多副本场景需要额外共享令牌或改用签名令牌方案。

---

## 3. 目录结构

```
homeassistant-xiaozhiserver/
├── homeassistant_xiaozhiserver/                          # Python 包（应用代码）
│   ├── __init__.py
│   ├── __main__.py                  # python -m homeassistant_xiaozhiserver 入口（stdio）
│   ├── config.py                    # 环境变量解析与运行常量（非法值告警并回退）
│   ├── logging_setup.py             # 统一日志格式、call_id 单次调用追踪、共享日志文件读取
│   ├── client.py                    # HA REST/WebSocket 客户端、鉴权与错误归一化
│   ├── service.py                   # 动作映射、状态/属性预期、回读校验
│   ├── danger.py                    # 危险设备判定、一次性确认令牌
│   ├── views.py                     # 实体摘要、可控标记、按设备聚合
│   ├── server.py                    # 六个 MCP 工具、stdio 进程入口
│   └── webui.py                     # WebUI：状态/设备总览/服务日志/折叠工具台（随服务启动）
├── server.py                        # 兼容入口（等价于 python -m homeassistant_xiaozhiserver）
├── mcp_pipe.py                      # 小智 MCP 接入点桥接（WebSocket ↔ stdio）
├── requirements.txt
├── Dockerfile                       # 单容器镜像（stdio MCP server + mcp_pipe + WebUI）
├── docker-entrypoint.sh             # 容器入口：WebUI 后台 + MCP bridge 前台
├── docker-compose.yml               # 单容器运行（GHCR 镜像，暴露 8099 WebUI）
├── .env.example
├── tests/
│   ├── fake_ha.py                   # 离线 HA 模拟器
│   ├── e2e_mcp_client.py            # stdio 端到端 MCP 客户端断言测试
│   ├── run_local_test.sh
│   └── webui_smoke.py               # WebUI 接口冒烟测试（无需密码）
└── README.md
```

模块职责：

| 模块 | 职责 |
|---|---|
| `config.py` | 集中解析环境变量；`env_str` / `env_bool` / `env_int` / `env_float` / `env_set`，非法值不会导致 import 崩溃 |
| `logging_setup.py` | 统一日志格式（含 `call_id` 与版本号 `vX.Y.Z`）；同时写 stderr 与共享日志文件，供 WebUI 读取最近日志；单次工具调用可串联工具入口、HA 请求与动作回读 |
| `client.py` | 封装 HA REST 与 WebSocket；统一 `HAError` 错误码；连接池复用；注册表 TTL 与失败短重试；DEBUG 级记录 HA 请求/响应 |
| `service.py` | 动作 → HA 服务映射；预期状态/属性推导；控制后四态回读校验 |
| `danger.py` | 判断危险设备；签发/消费一次性 `confirm_token`，绑定实体、服务与参数 |
| `views.py` | 实体视图、关键属性裁剪、可控性/危险标记、按物理设备聚合 |
| `server.py` | 六个 MCP 工具；固定 `stdio` 传输；进程入口 |
| `mcp_pipe.py` | 连接小智 MCP 接入点，启动 stdio MCP server，双向搬运 JSON-RPC，断线自动重连 |
| `webui.py` | WebUI（FastAPI）：运行状态、按区域/类型汇总的可调用设备、WebUI+bridge 服务日志、折叠的工具调试台与运行参数；页头与服务日志面板显示版本号；随服务启动，固定 `0.0.0.0:8099`，无独立密码 |

---

## 4. 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MCP_ENDPOINT` | ✅ | — | 小智 MCP 接入点的**工具端 WebSocket** 地址，形如 `ws://<宿主>:8004/mcp_endpoint/mcp/?token=<TOKEN>`；不是 `/mcp_endpoint/health?key=...` |
| `HA_URL` | ✅ | — | Home Assistant 地址，如 `https://your-ha.example:8123`（结尾不要带 `/`） |
| `HA_TOKEN` | ✅ | — | HA 长期访问令牌（个人资料 → 安全 → 长期访问令牌） |
| `HA_TIMEOUT` | | `10` | 访问 HA 的超时秒数 |
| `HA_VERIFY_SSL` | | `true` | 是否校验 HA 的 TLS 证书（自签名证书设 `false`） |
| `HA_TRUST_ENV` | | `false` | 是否读取宿主 HTTP(S)_PROXY/NO_PROXY；默认关闭，避免代理干扰直连 HA |
| `HA_WS_ENABLED` | | `true` | 是否用 WebSocket 读取设备/实体注册表以支持按设备聚合 |
| `HA_WS_URL` | | 由 `HA_URL` 推导 | HA WebSocket 地址，如 `ws://homeassistant:8123/api/websocket` |
| `HA_REGISTRY_TTL` | | `60` | 设备/实体注册表成功缓存秒数 |
| `HA_REGISTRY_ERROR_TTL` | | `10` | 注册表读取失败后的重试间隔，避免瞬时 WS 故障后长时间不重试 |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`；非法值告警并回退 `INFO` |
| `LOG_FILE` | | 系统临时目录下 `homeassistant-xiaozhiserver.log` | 共享日志文件路径；WebUI 与 bridge 都写入该文件，WebUI 从该文件读取最近日志 |
| `DEFAULT_LIST_LIMIT` | | `50` | `list_devices` 默认每页条数 |
| `MAX_LIST_LIMIT` | | `500` | `list_devices` 单次最大条数 |
| `SERVICE_CONFIRM_DELAY` | | `0.6` | 服务调用后回读前的等待秒数 |
| `SERVICE_CONFIRM_RETRIES` | | `1` | 回读的额外重试次数 |
| `SERVICE_CONFIRM_INTERVAL` | | 同 `SERVICE_CONFIRM_DELAY` | 回读轮询间隔秒数 |
| `SERVICE_CONFIRM_TIMEOUT` | | `4.0` | 回读确认总超时秒数 |
| `DANGER_CONFIRM_ENABLED` | | `true` | 是否对危险设备启用二次确认 |
| `DANGER_CONFIRM_TTL` | | `120` | 危险操作确认令牌有效期（秒），一次性使用 |
| `DANGEROUS_DOMAINS` | | `lock,alarm_control_panel` | 视为危险设备的 domain（逗号分隔） |
| `DANGEROUS_COVER_CLASSES` | | `garage,door,gate,window` | 视为危险 cover 的 device_class（逗号分隔） |
| `DANGEROUS_SERVICE_DOMAINS` | | `lock,alarm_control_panel,script,shell_command,python_script` | 即使目标实体不危险，这些服务域也要求二次确认（逗号分隔） |
> WebUI 随服务启动，固定监听 `0.0.0.0:8099`，无独立密码，直接复用 `HA_URL` / `HA_TOKEN`；不需要任何 `WEBUI_*` 环境变量。

> 数值类环境变量写错不会导致启动崩溃：`config.py` 会记录 warning 并回退默认值或边界值。
> 安全：`HA_TOKEN` / `MCP_ENDPOINT` 只通过环境变量注入，不写入镜像、代码或 Git。

---

## 5. 快速开始

### 5.1 准备 `.env`

```bash
cd homeassistant-xiaozhiserver
cp .env.example .env
# 编辑 .env，至少填写：
#   MCP_ENDPOINT=ws://<宿主>:8004/mcp_endpoint/mcp/?token=<TOKEN>
#   HA_URL=https://your-ha.example:8123
#   HA_TOKEN=你的HA长期访问令牌
```

### 5.2 Docker Compose（推荐）

`docker-compose.yml` 默认拉取已发布的 GHCR 镜像 `ghcr.io/kid941005/homeassistant-xiaozhiserver:latest`，无需本地构建：

> 同一镜像也会同步推送到 Docker Hub：`kid941005/homeassistant-xiaozhiserver:latest`，网络访问 Docker Hub 更快时可把 `image:` 换成该地址。

```bash
docker compose pull
docker compose up -d
docker compose logs -f homeassistant-xiaozhiserver
```

需要自己改代码构建时，把 `docker-compose.yml` 里的 `image:` 注释掉、启用其下的 `build:` 两行，再执行 `docker compose up -d --build`。

容器内会同时启动两个进程：WebUI（后台）和 MCP bridge（前台）。看到以下日志即表示桥接成功：

```text
2026-01-01 12:00:00 INFO  v1.13.2 MCP_PIPE [-] MCP接入点连接成功
2026-01-01 12:00:00 INFO  v1.13.2 MCP_PIPE [-] 已启动 MCP server：...
```

> **MCP bridge 本身不监听端口**，它主动连出到 `MCP_ENDPOINT`，不需要做入站映射。容器暴露的 `8099` 只给 WebUI 用：浏览器打开 `http://<宿主机IP>:8099/` 即可，无需登录。

### 5.3 本地开发（无 Docker）

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export MCP_ENDPOINT="ws://<宿主>:8004/mcp_endpoint/mcp/?token=<TOKEN>"
export HA_URL="https://your-ha.example:8123"
export HA_TOKEN="你的HA长期访问令牌"

python mcp_pipe.py server.py
```

也可以直接运行 stdio server（仅用于本地调试，不经过小智接入点）：

```bash
python -m homeassistant_xiaozhiserver
```

### 5.4 并入现有 compose（可选）

本仓库不包含 `deploy/` 目录；如果你的 NAS/服务器已有自己的 compose（例如 `deploy/docker-compose.yml`），把下面这个服务合并进去即可：

```yaml
  homeassistant-xiaozhiserver:
    image: ghcr.io/kid941005/homeassistant-xiaozhiserver:latest
    container_name: homeassistant-xiaozhiserver
    restart: unless-stopped
    env_file:
      - .env
    ports:
      - "8099:8099"
```

在**该 compose 所在目录**的 `.env` 中补充 `MCP_ENDPOINT` / `HA_URL` / `HA_TOKEN` 后执行：

```bash
docker compose pull homeassistant-xiaozhiserver
docker compose up -d homeassistant-xiaozhiserver
docker compose ps homeassistant-xiaozhiserver
docker compose logs -f homeassistant-xiaozhiserver
```

> bridge 主动连出到 `MCP_ENDPOINT`，与 `xiaozhi-server` 不需要同网络，也不需要为它做端口映射；`8099` 仅供 WebUI 访问。

### 5.5 WebUI

WebUI 与 MCP bridge 在**同一个容器**内启动：WebUI 后台运行，bridge 前台运行。页面按四块组织，避免重复展示：

- **运行状态**：版本、启动时间、运行时长、HA 连通性、MCP 接入点、运行形态；页头同时显示版本徽标，配置类信息不再单独重复一张表。
- **可调用设备**：按区域汇总，逐行显示设备、实体、类型（灯具/开关/空调/窗帘/门锁/媒体/风扇/扫地机/传感器等）、当前状态与可执行动作；危险设备带标记，支持关键字过滤和刷新。数据来自 `list_devices(group_by="device", controllable_only=true)`。
- **服务日志**：读取 WebUI 与 bridge 共用的日志文件，展示最近 200 条，支持级别筛选、手动刷新与 5 秒自动刷新；面板标题与每条日志均标注版本号，页面底部显示日志文件路径。
- **高级**：工具调试台与运行参数默认折叠在页面底部，需要时再展开。

WebUI 固定监听 `0.0.0.0:8099`，无独立密码，直接复用 `.env` 里的 `HA_URL` / `HA_TOKEN`，不需要任何 `WEBUI_*` 环境变量。容器启动后浏览器访问 `http://<宿主机IP>:8099/`（例如 `http://192.168.50.100:8099/`）即可。

本机直接运行（不使用 Docker）：

```bash
export HA_URL="https://your-ha.example:8123"
export HA_TOKEN="你的HA长期访问令牌"
python -m homeassistant_xiaozhiserver.webui
```

日志文件默认是系统临时目录下的 `homeassistant-xiaozhiserver.log`；可用 `LOG_FILE` 指定路径。容器内如需长期保留，建议把该路径挂载到卷，例如 `-v ./logs:/var/log/homeassistant-xiaozhiserver` 并设置 `LOG_FILE=/var/log/homeassistant-xiaozhiserver/app.log`。

安全说明：

- WebUI **没有登录口令**，请只绑定可信内网，不要直接暴露公网；远程访问优先走 SSH 隧道或带鉴权的反向代理。
- 页面与 `/api/status`、`/api/config` 只展示掩码后的 `HA_TOKEN` / `MCP_ENDPOINT` 令牌，不会回传明文。
- 工具调试台只允许调用六个白名单工具，参数必须是 JSON 对象。
- WebUI 对危险设备操作只返回 `dangerous_operation_blocked`，不执行、不回传 `confirm_token`；门锁/安防等操作请走小智语音确认。
- WebUI 对 POST/PUT/PATCH/DELETE 校验同源 `Origin`，降低 CSRF 风险；但它仍没有登录口令，不能替代访问控制。
- 服务日志只读取文件尾部固定窗口，日志文件很大时也不会把整个文件读入内存。
- 容器以非 root 用户（uid 10001）运行，并带 `HEALTHCHECK` 探测 `/api/status`。

---


## 6. MCP 工具

所有工具返回 **JSON**，统一结构为 `{"ok": true/false, ...}`。失败时 `ok=false` 并带 `code` / `message` / `hint`，不会抛出异常堆栈。

### 6.1 `list_devices` — 获取设备列表

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `keyword` | string | 否 | 名称、entity_id 或所属设备名关键字，模糊匹配（如「客厅」「灯」）；也可用等价别名 `query` / `q` / `search` / `name`（只传一个，传不同值会报 `invalid_argument`） |
| `domain` | string | 否 | 设备类型过滤：`light` / `switch` / `sensor` / `climate` / `cover` / `media_player` / `fan` / `vacuum` / `scene` 等 |
| `area` | string | 否 | 区域过滤：区域名称或 `area_id`，模糊匹配（如「客厅」「living_room」）；依赖 HA 区域注册表返回 `area_name`，按 `area_id` 过滤始终可用 |
| `controllable_only` | bool | 否 | 只返回可控制设备（自动排除 sensor 等只读实体）；**不传且无其他过滤条件时默认 true** |
| `group_by` | string | 否 | `entity`（默认，逐实体）或 `device`（按物理设备聚合） |
| `limit` | int | 否 | 每页条数，默认 `DEFAULT_LIST_LIMIT`（通常 50）、上限 `MAX_LIST_LIMIT`；**无过滤条件时同样使用 `DEFAULT_LIST_LIMIT`**；`limit<=0` 回退默认值，`offset<0` 视为 0 |
| `offset` | int | 否 | 起始偏移，默认 0 |

> `keyword`、`domain`、`area` 可任意组合，例如 `{"area":"客厅","domain":"light"}` 只返回客厅的灯。
>
> **默认收敛**：不传任何过滤条件时，默认只返回可控制设备且每页 `DEFAULT_LIST_LIMIT` 条（通常 50，响应里 `applied_defaults` 标示），避免一次把上百个实体塞给模型；需要全量时显式传 `controllable_only=false` 并配合 `limit`/`offset`。若 `has_more=true`，目标设备可能不在当前页，应先按 `keyword`/`area` 精确过滤。
>
> **未知参数不再静默忽略**：传入本工具不支持的参数会返回 `{"ok":false,"code":"invalid_argument","message":"不支持的参数：...；可用参数：..."}`，避免参数名写错却拿到全量数据。

返回（entity 模式）：`{ok, grouped_by:"entity", registry_available, area, total, count, offset, limit, has_more, next_offset, devices:[{entity_id,name,domain,state,unit,device_class,controllable,actions,controllable_reason,dangerous,danger_reason,device_id,device_name,area_id,area_name}], message}`。

返回（device 模式）：`{ok, grouped_by:"device", registry_available, area, total, count, offset, limit, has_more, next_offset, devices:[{device_id,name,manufacturer,model,area_id,area_name,controllable,dangerous,entity_count,entities:[...]}], message}`。

> `registry_available:false` 表示 HA WebSocket 设备/实体注册表不可用（令牌无权限、WS 不通等），此时自动降级为按实体展示，不影响状态查询与控制。区域注册表单独读取失败时只令 `area_name` 为 `null`，按 `area_id` 过滤仍可用。

### 6.2 `get_device_state` — 获取设备状态

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 是 | entity_id（如 `light.living_room`）或友好名称（如「客厅灯」）；服务端自动解析，多义时返回候选 |
| `include_all_attributes` | bool | 否 | 默认 `false` 只返回关键属性；需要完整 attributes 时传 `true` |

返回：`{ok, entity:{entity_id,name,domain,state,unit,attributes,attributes_truncated,attribute_count,controllable,actions,controllable_reason,dangerous,danger_reason,device_id,device_name,area_id,area_name,last_changed,last_updated}, message}`。

默认只返回亮度、色温、音量、温度、位置、电池等关键属性，避免媒体/摄像头等实体的巨大 attributes 占用上下文；`attributes_truncated=true` 表示还有更多属性。

### 6.3 `control_device` — 控制设备

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 是 | 目标设备：entity_id 或友好名称 |
| `action` | string | 是 | 动作，见下表 |
| `value` | number / string / array | 视动作 | 亮度/音量百分比（1-100）、温度（℃）、色温（K）；颜色支持 `#RRGGBB`、`"r,g,b"` 或 `[r,g,b]` |
| `confirm_token` | string | 危险设备时 | 危险设备二次确认令牌（首次调用返回，用户确认后再带此参数重试） |

通用动作：

| action | 作用 | 需要的 value |
|---|---|---|
| `turn_on` / `turn_off` | 开关；cover 自动映射 `open_cover` / `close_cover`，vacuum 映射 `start` / `stop` | — |
| `toggle` | 切换 | — |
| `open` / `close` / `stop` / `pause` / `continue` / `resume` / `play` | 按设备能力映射到对应服务（`resume` 为 `continue` 的别名） | — |
| `lock` / `unlock` | 上锁 / 解锁（lock） | — |
| `set_brightness` | 设置亮度（light） | 1-100 |
| `set_volume` | 设置音量（media_player） | 1-100 |
| `volume_mute` / `volume_unmute` | 静音 / 取消静音 | — |
| `set_temperature` | 设置温度（climate） | 摄氏度 |
| `set_color_temp` | 设置色温（light） | 开尔文 |
| `set_color` | 设置颜色（light） | `#RRGGBB`、`"r,g,b"` 或 `[r,g,b]` |

各设备类型内置动作：

| domain | 内置动作 |
|---|---|
| light | turn_on, turn_off, toggle, set_brightness, set_color_temp, set_color |
| switch / fan / input_boolean / automation | turn_on, turn_off, toggle |
| cover | open, close, stop, turn_on, turn_off |
| climate | turn_on, turn_off, set_temperature |
| media_player | turn_on, turn_off, play, pause, stop, continue, resume, set_volume, volume_mute, volume_unmute |
| vacuum | turn_on, turn_off, stop, pause, continue, resume |
| lock | lock, unlock |
| scene / script | turn_on |

返回：`{ok, entity_id, name, action, service, response_kind, status, expected_state, before, after, changed, verified, readback_errors, verification, message}`。

**控制后状态+属性回读校验**：服务调用后会在 `SERVICE_CONFIRM_TIMEOUT` 秒内轮询回读，既核对状态（如 turn_on→on），也核对属性（亮度、温度、音量、色温、颜色等）。`status` 为：

| status | 含义 |
|---|---|
| `confirmed` | 回读确认状态/属性已达到预期，且确有变化 |
| `no_change` | HA 已接受但无变化；`verified=true` 表示设备本来就是目标状态 |
| `mismatch` | 回读看到变化，但状态或属性与预期不符（设备能力/参数问题） |
| `accepted_pending` | HA 返回 null（新版 HA 常见），或回读状态失败；已下发但回读窗口内无法确认结果，`readback_errors` 给出失败错误码 |

`verification` 给出 `attempts`（回读次数）、`elapsed_ms`、`expectations`、`checks`、`mismatches`、`readback_errors`，便于判断是否真正生效，避免 HA「对不存在实体也返回 200」造成的静默假成功。

> 若设备类型本身不可控制（如 `sensor`），返回 `code:"not_controllable"` 并说明原因，不会误发服务。

> **危险设备二次确认（防误操作）**：`lock`、`alarm_control_panel` 以及 device_class 为 `garage` / `door` / `gate` / `window` 的 `cover` 默认视为危险设备。首次调用会返回 `code:"confirmation_required"`、`dangerous:true`、`requires_confirmation:true`、`confirm_token`、`expires_in` 与 `risk_reason`，**不会执行**；请先向用户明确确认动作，再带同一个 `confirm_token` 重试。令牌**一次性**、绑定「实体 + 服务 + 参数」，默认 `DANGER_CONFIRM_TTL=120` 秒有效；令牌无效/过期/操作不匹配分别返回 `invalid_confirm_token` / `confirm_token_expired` / `confirm_token_mismatch`。

### 6.4 `list_services` — 查询设备能力（服务目录）

让调用方先了解「某类/某个设备支持哪些服务、每个服务要什么参数」，再精确调用（能力自描述）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 否 | 设备 entity_id 或友好名称；给出时只返回该设备所属 domain 的服务 |
| `domain` | string | 否 | 直接指定设备类型（如 `light` / `climate` / `media_player`） |
| `keyword` | string | 否 | 按服务名或描述关键字过滤（如 `temperature`） |
| `detailed` | bool | 否 | 是否返回字段参数明细（指定 entity/domain 时默认 true，否则仅名称） |

返回：`{ok, domain, domain_count, service_count, domains:[{domain, service_count, services:[...]}], message}`。

`detailed=true` 时 `services` 为对象列表（`service` / `name` / `description` / `fields`），否则为服务名字符串列表。数据源为 HA `GET /api/services`。

### 6.5 `call_service` — 通用服务调用

执行 `control_device` 未封装的任意 HA 服务（如空调模式、风扇风速、自定义脚本）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `entity` | string | 是 | 目标设备：entity_id 或友好名称 |
| `service` | string | 是 | 服务名：`turn_on`（默认用设备 domain）或 `climate.set_hvac_mode`（显式带 domain 以它为准） |
| `data` | object | 否 | 额外参数 JSON，如 `{"temperature":26}`、`{"hvac_mode":"heat"}`、`{"rgb_color":[255,0,0]}`；不要包含 `entity_id` |
| `confirm_token` | string | 危险设备时 | 危险设备二次确认令牌，机制同 `control_device` |

返回：`{ok, entity_id, name, service, response_kind, status, expected_state, before, after, changed, verified, readback_errors, verification, message}`。

`status` 四态同 `control_device`：`confirmed` / `no_change` / `mismatch` / `accepted_pending`；`verification` 含 `attempts`、`elapsed_ms`、`checks`、`mismatches`、`readback_errors`。

> 危险设备保护与 `control_device` 相同：`lock` / `alarm_control_panel` 及车库门/门窗类 `cover` 的首次调用只返回 `confirmation_required` 与 `confirm_token`，确认后带 token 重试才执行。

### 6.6 `list_domains` — 获取设备类型

无参数。返回 `{ok, domains:{light:3,switch:2,...}, controllable_domains:["light","switch",...], dangerous_domains:["lock",...], dangerous_entities:[{entity_id,name,domain,state,reason}], dangerous_entity_count, message}`。

`dangerous_entities` 会识别 `lock` / `alarm_control_panel` 以及 `device_class=garage/door/gate/window` 的 cover，便于了解哪些具体实体操作需要二次确认。

---
## 7. 接入小智后端（MCP 接入点）

本服务只支持一种接入方式：**小智 MCP 接入点（工具端 WebSocket）**。不再提供 HTTP / SSE / streamable-http 直连，也不需要在智控台填写 `http://<host>:8090/mcp`。

### 7.1 原理

```
小智后端 ──> mcp-endpoint-server ──WebSocket──> mcp_pipe.py ──stdio──> server.py ──> Home Assistant
```

- `mcp_pipe.py` 是 WebSocket **客户端**，主动连接 `mcp-endpoint-server` 暴露的工具端地址；
- 连接建立后，它用 `stdio` 启动 `server.py`，并在 WebSocket 文本消息与子进程 stdin/stdout 之间原样搬运 MCP JSON-RPC；
- 因此 bridge 不需要监听端口，也不需要公网映射；只要它能出网访问 `MCP_ENDPOINT`，并且能访问 `HA_URL` 即可。

### 7.2 获取正确的工具端地址

在智控台「MCP 接入点」中复制**工具端地址**，形如：

```text
ws://<宿主>:8004/mcp_endpoint/mcp/?token=<TOKEN>
```

> ⚠️ `http://<宿主>:8004/mcp_endpoint/health?key=<KEY>` 是智控台参数里的健康检查地址，**不是**工具连接地址。`mcp_pipe.py` 只接受 `ws://` 或 `wss://`。

### 7.3 本机快速验证

```bash
cd homeassistant-xiaozhiserver
pip install -r requirements.txt

export MCP_ENDPOINT="ws://<宿主>:8004/mcp_endpoint/mcp/?token=<TOKEN>"
export HA_URL="https://your-ha.example:8123"
export HA_TOKEN="你的HA长期访问令牌"

python mcp_pipe.py server.py
```

成功日志特征：

```text
MCP_PIPE - 正在连接 MCP 接入点 ws://<宿主>:8004/mcp_endpoint/mcp/
MCP_PIPE - MCP接入点连接成功
MCP_PIPE - 已启动 MCP server：.../python .../server.py
```

日志里不会打印 token（query 已脱敏）。随后在智控台刷新 MCP 工具列表，应看到本服务的 6 个工具。

### 7.4 Docker Compose 常驻

```bash
cd homeassistant-xiaozhiserver
cp .env.example .env
# 填写 MCP_ENDPOINT / HA_URL / HA_TOKEN
docker compose pull
docker compose up -d
docker compose logs -f homeassistant-xiaozhiserver
```

容器配置了 `restart: unless-stopped`；进程内部还有指数退避重连（1s 起，最大 600s），`mcp-endpoint-server` 重启或网络抖动后会自动恢复。

### 7.5 网络连通

| 场景 | `MCP_ENDPOINT` 写法 |
|---|---|
| bridge 与 `mcp-endpoint-server` 在同一 compose 网络 | `ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=<TOKEN>` |
| 两者在不同机器/网络 | `ws://<mcp-endpoint-server 可达地址>:8004/mcp_endpoint/mcp/?token=<TOKEN>`，或经反向代理使用 `wss://` |
| `mcp-endpoint-server` 在公网 | `wss://<域名>/mcp_endpoint/mcp/?token=<TOKEN>`（建议 TLS） |

需要保证的只有两条：

1. bridge → `mcp-endpoint-server:8004` 可出网访问；
2. bridge → `HA_URL` 可访问。

不需要为 bridge 发布 8090，也不需要在路由器上做任何入站端口映射。

### 7.6 智控台核对

在智控台刷新 MCP 工具后，应看到：

| 工具 | 用途 |
|---|---|
| `list_devices` | 查询设备列表 |
| `get_device_state` | 查询单个设备状态 |
| `control_device` | 常用控制 |
| `list_services` | 查询设备支持的服务与参数 |
| `call_service` | 通用服务调用 |
| `list_domains` | 查询设备类型汇总 |

工具清单以智控台握手结果为准；本服务固定暴露 6 个工具。

**推荐在智控台的角色提示词里加一句硬约束**，可显著减少多余调用：

```
当用户明确点名某个设备要开/关时，直接调用 control_device(entity="设备中文名", action="turn_on"|"turn_off")，不要先调用 list_devices。
```

`list_devices` 的工具描述里已内置同样的约束；若智控台只能改系统提示词，把上面这句放进去即可。

### 7.7 控制结果的播报协议（避免 turn_off 被念出来）

小智 MCP 接入点会检查工具结果里是否有 `action` 字段，并把它当作小智内部的 `Action` 枚举名解析：

```python
# xiaozhi-server: core/providers/tools/mcp_endpoint/mcp_endpoint_executor.py
if "action" in resultJson:
    return ActionResponse(action=Action[resultJson["action"]], response=resultJson.get("response", ""))
```

合法枚举只有 `ERROR` / `NOTFOUND` / `NONE` / `RESPONSE` / `REQLLM` / `RECORD`。`turn_off`、`turn_on` 不是合法枚举，会抛 `KeyError` 并被转成 `Action.ERROR`，随后小智直接把 `str(e)`（也就是 `turn_off`）交给 TTS 播报。旧版 `control_device` 返回 `"action": "turn_off"`，就会出现“turn 什么 off”的播报。

本服务的约定：

| 场景 | 返回字段 | 小智行为 |
|---|---|---|
| 控制成功 | `"device_action": "turn_off"`、`"action": "RESPONSE"`、`"response": "电风扇已关闭。"` | 直接 TTS 播报 `response`，不再调用一次 LLM，路径更短 |
| 危险设备待确认 / 参数错误 / 设备不存在 | 不返回 `action`，保留 `code`、`confirm_token`、中文 `message` | 走 `REQLLM`，由 LLM 继续确认或解释 |
| 需要技术细节的客户端 | 读取 `device_action`、`service`、`status`、`verification` | `action` 只用于小智协议，不表示设备动作 |

`control_device` 的 `response` 只包含设备名和中文动作，不含 `turn_off`、`light.turn_off`、`entity_id` 或英文状态。`call_service` 对可控 domain 的常见控制服务（`turn_on`/`turn_off`/`set_temperature`/`set_hvac_mode`/`set_fan_mode` 等）同样返回 `action: "RESPONSE"` 与中文 `response`；脚本、场景、未知服务不返回 `action`，走 `REQLLM`，由 LLM 根据中文 `message` 组织回复。

`call_service` 的二次确认不只按目标实体判断：`script`、`shell_command`、`python_script`、`lock`、`alarm_control_panel` 等服务域，以及 `homeassistant.restart/stop/reload_*` 系统级服务，即使目标实体是普通灯/开关，也会先返回 `confirmation_required`，避免借普通实体执行脚本绕过确认。

设备名称能直接匹配时，模型应直接调用 `control_device`，不要先 `list_devices`；只有名称不明确、需要确认设备能力或设备不存在时才查列表/服务。这样一次语音控制只需要「LLM 决策 → 工具执行 → 直接播报」三步。

### 7.8 注意事项

- **只运行一个 bridge 实例**：同一 `MCP_ENDPOINT` 被多个 bridge 同时连接会产生重复工具/重复会话；需要高可用时应在 `mcp-endpoint-server` 侧做互斥或分片。
- `MCP_ENDPOINT` 含 token，只放 `.env` 或运行时环境变量，不要提交到 Git、不要写进镜像。
- 不要把 `/mcp_endpoint/health?key=...` 填进 `MCP_ENDPOINT`。
- 断线重连会重新启动 `server.py`；危险确认令牌是进程内内存，重连后旧令牌失效，需要重新发起原操作。
- 如果你的 xiaozhi-server 部署目录里还留有旧「方式 B」的 `.mcp_server_settings.json`，改用接入点后应清空为 `{"mcpServers": {}}`（或删除），否则它可能继续连接已移除的 HTTP MCP 地址，造成报错或工具重复。

---


## 8. 自然语言使用示例

| 用户说 | 调用工具 | 参数 | 返回（节选） |
|---|---|---|---|
| 家里有哪些设备？ | `list_devices` | `{}` | `{"ok":true,"total":6,"count":6,"has_more":false,"applied_defaults":{"controllable_only":true,"limit":50},"devices":[...]}`（默认只列可控设备） |
| 列出所有实体（含传感器） | `list_devices` | `{"controllable_only":false}` | `total=7` |
| 列出所有的灯 | `list_devices` | `{"domain":"light"}` | `total=1, devices=[{"name":"客厅灯","state":"off"}]` |
| 客厅都有什么设备？ | `list_devices` | `{"keyword":"客厅"}` | `total=3` |
| 客厅区域有哪些设备？ | `list_devices` | `{"area":"客厅"}` | `total=6`（区域匹配，含 light/switch 等） |
| 客厅有哪些灯？ | `list_devices` | `{"area":"客厅","domain":"light"}` | `total=1`，只返回客厅的 `light` 实体 |
| 哪些设备能控制？ | `list_devices` | `{"controllable_only":true}` | `total=6`（自动排除只读 sensor） |
| 按物理设备看看家里有什么 | `list_devices` | `{"group_by":"device"}` | `devices=[{"name":"客厅环境组","entity_count":2,"controllable":true,"entities":[...]}]` |
| 家里有哪些类型的设备？ | `list_domains` | `{}` | `{"domains":{"light":1,"switch":1,...}}` |
| 客厅灯现在开着吗？ | `get_device_state` | `{"entity":"客厅灯"}` | `{"ok":true,"entity":{"state":"off","attributes":{...}}}` |
| 把客厅灯亮度调到 50% | `control_device` | `{"entity":"客厅灯","action":"set_brightness","value":50}` | `{"ok":true,"device_action":"set_brightness","action":"RESPONSE","response":"客厅灯亮度已调到50%。","service":"light.turn_on","changed":true,"after":{"state":"on","brightness":127}}` |
| 关掉电风扇 | `control_device` | `{"entity":"电风扇","action":"turn_off"}` | `{"ok":true,"device_action":"turn_off","action":"RESPONSE","response":"电风扇已关闭。","service":"switch.turn_off","changed":true}` |
| 空调调到 26 度 | `control_device` | `{"entity":"空调","action":"set_temperature","value":26}` | `{"ok":true,"service":"climate.set_temperature",...}` |
| 电视音量调到 30 | `control_device` | `{"entity":"电视","action":"set_volume","value":30}` | `{"ok":true,"service":"media_player.volume_set",...}` |
| 打开窗帘 | `control_device` | `{"entity":"窗帘","action":"open"}` | `{"ok":true,"service":"cover.open_cover","changed":true,"after":{"state":"open"}}` |
| 打开大门门锁 | `control_device` | `{"entity":"大门门锁","action":"unlock"}` | `{"ok":false,"code":"confirmation_required","dangerous":true,"confirm_token":"...","expires_in":120}`（**未执行**） |
| （用户确认后）打开大门门锁 | `control_device` | `{"entity":"大门门锁","action":"unlock","confirm_token":"..."}` | `{"ok":true,"status":"confirmed","after":{"state":"unlocked"}}` |
| 空调能做什么操作？ | `list_services` | `{"entity":"空调"}` | `{"ok":true,"domain":"climate","service_count":4,"services":[{"service":"set_temperature","fields":[{"name":"temperature","required":true}]},...]}` |
| 空调设成制热模式 | `call_service` | `{"entity":"空调","service":"climate.set_hvac_mode","data":{"hvac_mode":"heat"}}` | `{"ok":true,"status":"confirmed","after":{"state":"heat"}}` |
| 把客厅灯切成暖光 | `call_service` | `{"entity":"客厅灯","service":"light.turn_on","data":{"color_temp_kelvin":2700}}` | `{"ok":true,"status":"confirmed","verified":true,"verification":{"attempts":1,...}}` |
| 把客厅灯调成红色 | `control_device` | `{"entity":"客厅灯","action":"set_color","value":"#ff0000"}` | `{"ok":true,"service":"light.turn_on","changed":true,"after":{"rgb_color":[255,0,0]}}` |
| 把客厅灯亮度调到 80（设备没调到） | `call_service` | `{"entity":"客厅灯","service":"light.turn_on","data":{"brightness_pct":80}}` | `{"ok":true,"status":"mismatch","verified":false,"verification":{"mismatches":[{"field":"brightness","expected":204,"actual":127}]}}` |

> `control_device` 成功时多返回 `device_action`（真实设备动作）与 `action: "RESPONSE"` + 中文 `response`；小智 MCP 接入点会直接播报 `response`，省掉一次 LLM 往返。危险确认、参数错误等分支不返回 `action`，交给 LLM 处理。详见 7.7。

失败示例（清晰提示，不崩溃）：

```json
// 设备不存在
{"ok": false, "code": "not_found", "message": "未找到设备『不存在设备』，无法控制。可先用 list_devices 查询。"}
// 动作不支持
{"ok": false, "code": "unsupported_action", "message": "『客厅灯』（light）不支持动作：fly。该类型可用动作：turn_on, turn_off, toggle, ..."}
// 设备不可控制（如传感器）
{"ok": false, "code": "not_controllable", "message": "『客厅温度』（sensor）不可控制：该类型无内置控制动作..."}
// 危险设备未二次确认
{"ok": false, "code": "confirmation_required", "dangerous": true, "confirm_token": "...", "expires_in": 120, "message": "『大门门锁』是危险设备..."}
```

---

## 9. 错误处理与排查

### 9.1 错误码

| code | 含义 | 处理建议 |
|---|---|---|
| `config_missing` | 缺少 `HA_URL` / `HA_TOKEN` | 补齐环境变量后重启；bridge 启动时会直接失败退出 |
| `config_invalid` | `HA_URL` 不是 `http://` / `https://` 开头或缺少主机名 | 修正 `HA_URL` 后重启 |
| `invalid_argument` | 工具收到未知/冲突参数 | 按返回的可用参数名重试 |
| `invalid_pagination` | `limit` / `offset` 不是整数 | 传正整数 `limit` 与非负整数 `offset` |
| `invalid_entity` | `entity_id` 不是 `domain.object_id` 格式 | 用 `list_devices` 查正确 entity_id，或直接传中文名称 |
| `invalid_response` | HA 返回了非预期 JSON 类型 | 检查 HA 版本/反向代理是否改写了响应 |
| `auth_failed` | 令牌无效/过期/无权限（HTTP 401/403） | 重新生成长期令牌，更新 `HA_TOKEN` |
| `unreachable` | 连不上 HA | 检查 `HA_URL`、网络连通、容器能否访问该地址 |
| `timeout` | 请求 HA 超时 | 增大 `HA_TIMEOUT` 或检查网络/HA 负载 |
| `upstream_error` | HA 5xx | 查看 HA 自身日志/健康状况 |
| `http_error` | HA 返回非 2xx（401/403/404/5xx 之外） | 查看 HA 日志与请求参数 |
| `network_error` | 连接/传输层错误（DNS、TLS、连接重置等） | 检查网络、代理、TLS 证书与 `HA_URL` |
| `not_found` | 实体/资源不存在 | 用 `list_devices` 查正确 entity_id |
| `not_controllable` | 设备类型无内置控制动作（如 sensor） | 只用 `get_device_state` 查询，或 `list_services` 后用 `call_service` |
| `ambiguous` | 名称匹配到多个设备 | 改用更精确名称或 entity_id |
| `missing_value` / `invalid_value` | 动作缺参数或取值越界 | 按工具描述传 0-100 等合法值（亮度/音量 0 表示关闭/静音） |
| `unsupported_action` | 动作不支持（含该设备类型不支持） | 使用返回的 `actions` 列表，或 `list_services` 后用 `call_service` |
| `invalid_service` | `call_service` 的 service 为空/格式错 | 用 `list_services` 查服务名，格式 `service` 或 `domain.service` |
| `invalid_data` | `data` 不是 JSON 对象、包含 `entity_id` 或字段值非法（如 `rgb_color` 分量越界） | 传键值对对象，如 `{"temperature":26}`；目标实体只用 `entity` 参数指定 |
| `invalid_group_by` | `group_by` 取值不支持 | 使用 `entity`（默认）或 `device` |
| `no_services` | 指定 domain 无可用服务 | 核对 domain 或 `list_services` 不带过滤查看 |
| `confirmation_required` | 危险设备首次调用，未带 `confirm_token`（未执行） | 向用户确认后带返回的 `confirm_token` 重试 |
| `invalid_confirm_token` | 令牌无效或已使用（一次性） | 重新发起原操作获取新令牌 |
| `confirm_token_expired` | 令牌超过 `DANGER_CONFIRM_TTL` | 使用新返回的 `confirm_token`（已自动刷新） |
| `confirm_token_mismatch` | 令牌与本次实体/服务/参数不匹配 | 重新发起原操作获取新令牌，勿改参数复用 |
| `ws_error` / `registry_error` | HA WebSocket 注册表读取失败 | 一般会降级为按实体展示；检查 WS 权限/网络，或设 `HA_WS_ENABLED=false` |
| `internal_error` | 未预期的内部错误 | 查看服务日志，确认 HA 版本与请求参数 |

### 9.2 排查步骤

1. **看 bridge 日志**：`docker logs -f homeassistant-xiaozhiserver`。
   - 先确认出现 `MCP接入点连接成功` 与 `已启动 MCP server`；
   - 若一直重连，检查 `MCP_ENDPOINT` 是否为工具端 `ws://` 地址、token 是否有效、bridge 能否访问该主机/端口。
2. **看容器状态**：`docker compose ps`。容器应保持 `Up`；反复重启通常是 `MCP_ENDPOINT` 未设置或格式错误（进程会直接退出）。
3. **看日志（带 call_id 与版本追踪）**：每条日志格式为 `时间 级别 v版本 logger [call_id] 消息`，`call_id` 可把一次工具调用的入口、HA 请求与回读串起来，版本号可区分容器内实际运行的镜像版本。
   ```bash
   docker logs -f homeassistant-xiaozhiserver 2>&1 | grep -E 'tool=|action |HA registry|MCP接入点'
   docker logs homeassistant-xiaozhiserver 2>&1 | grep '<call_id>'
   ```
   关键日志示例：
   - `tool=list_devices done ok=true elapsed_ms=...`（`start args` 仅在 `LOG_LEVEL=DEBUG` 输出，避免记录用户原话与敏感参数）
   - `action entity=light.living_room service=light.turn_on status=confirmed verified=True attempts=1 elapsed_ms=...`
   - `tool=control_device done ok=false code=not_found ...`
   - `confirm_token` 在日志中显示为 `***`；`HA_TOKEN` 启动日志只显示 `set`/`missing`。
   - 同一份日志也可在 WebUI 的“服务日志”区查看（支持级别筛选与 5 秒自动刷新，面板与每条日志显示版本号），无需反复执行 `docker logs`。
4. **单独验证 HA**（排除本服务问题）：
   ```bash
   curl -s -H "Authorization: Bearer <你的令牌>" https://your-ha.example:8123/api/states | head -c 300
   ```
   401 → 令牌问题；连接被拒 → 地址/网络问题。
5. **网络**：bridge 容器内访问 HA 注意
   - 同一 compose 网络用服务名（如 `http://homeassistant:8123`）；
   - 访问宿主机上的 HA 用 `http://host.docker.internal:8123`（Docker Desktop）；
   - Linux 下可加 `--add-host=host.docker.internal:host-gateway`。
6. **TLS**：自签名证书导致 `SSLError` 时设 `HA_VERIFY_SSL=false`（仅受信网络）。
7. **代理**：若宿主设置了 `HTTP_PROXY/NO_PROXY` 干扰直连，保持 `HA_TRUST_ENV=false`（默认）。
8. **按设备聚合不可用**：`list_devices(group_by="device")` 依赖 HA WebSocket `config/device_registry/list`。若返回 `registry_available:false`，会自动降级为按实体展示；可用 `HA_WS_URL` 指定地址，或设 `HA_WS_ENABLED=false` 明确关闭。
9. **控制回读不符（status=mismatch）**：说明 HA 接受了调用、状态也变了，但属性未达目标（如设备不支持该亮度/温度范围）。核对 `list_services` 的字段与设备能力，必要时用 `get_device_state` 复查。
10. **危险设备一直返回 `confirmation_required`**：这是防误操作设计，不是故障。把首次返回的 `confirm_token` 原样传入再次调用即可；若令牌过期，用新返回的 token。

> 已知 HA 行为：对**不存在的实体**调用 service 也可能返回 HTTP 200（响应为空）；新版 HA 对多数服务调用返回 **null**。
> 本服务在 `control_device` / `call_service` 后执行**状态+属性回读校验**：`response_kind`（states/empty/none）、`status`（confirmed/no_change/mismatch/accepted_pending）、`verified` 与 `verification`（checks/mismatches），避免误报成功。

---


## 10. 离线测试

提供模拟器与 stdio 端到端脚本，可在没有真实 HA 的情况下覆盖主要工具与关键分支（6 个工具入口、分页/过滤/聚合、控制回读、危险确认、WebUI 安全与示例）。测试口令由你自己指定（模拟器与本服务共用同一个值，**不要用真实 HA 令牌**）。

```bash
# 一键运行（推荐）
export TEST_TOKEN=任意本地测试口令
bash tests/run_local_test.sh
```

脚本会启动 fake HA（HTTP 8123 + WebSocket 8124），等待两个端口都可连接后运行端到端断言；e2e 客户端会以 `stdio` 子进程方式启动 `server.py`，结束后自动清理。随后运行 `tests/webui_smoke.py`，校验 WebUI 无需登录即可访问、状态、配置掩码、设备总览（`/api/devices`）、服务日志（`/api/logs`，含版本号）与工具白名单。整个测试不需要 `/ready` 或 8090 端口。

也可分两个终端手动运行：

```bash
# 终端 1：启动模拟 HA（令牌取自 FAKE_HA_TOKEN，缺省 devtoken）
export FAKE_HA_TOKEN=任意本地测试口令
python tests/fake_ha.py

# 终端 2：e2e 客户端自己启动 stdio MCP server（重跑前请重启终端 1 的模拟器，保证初始状态）
export HA_URL=http://127.0.0.1:8123
export HA_TOKEN=任意本地测试口令
export HA_WS_URL=ws://127.0.0.1:8124/api/websocket
python tests/e2e_mcp_client.py
```

`fake_ha.py` 特意复刻了「对不存在实体返回 200」的行为，并内置「延迟生效（*_delayed）」「只改状态不改属性（turn_on_ignore_brightness）」两种场景，用于验证回读确认与 mismatch 识别；并内置极简 WebSocket 注册表（默认 8124 端口）用于验证按设备聚合，未安装 websockets 时自动跳过。P3 还内置 `lock.front_door`（大门门锁）与 `lock.lock` / `lock.unlock` 服务，端到端脚本会验证「首次 confirmation_required → 带 token 执行成功 → 复用/错误 token 被拒 → 意图不匹配被拒」全流程。

---


## 11. 设备规模与性能

- `list_devices` 支持 `keyword`（别名 `query`/`q`/`search`/`name`）/ `domain` / `area` 过滤 + `limit` / `offset` 分页（默认 50、上限 500），返回 `has_more` / `next_offset` 便于续拉，避免实体过多撑爆上下文；
- 无过滤条件时 `list_devices` 默认 `controllable_only=true` 且每页 `DEFAULT_LIST_LIMIT` 条（通常 50，`applied_defaults` 标示）；`has_more=true` 时必须继续按 `keyword`/`area` 过滤而不是用当前页推断全量；未知参数返回 `invalid_argument` 而非静默忽略；
- `group_by="device"` 按物理设备聚合，`controllable_only=true` 只看可控设备，进一步压缩结果；
- `get_device_state` 默认只返回关键属性（亮度、色温、音量、温度、位置、电池等），完整属性需显式传 `include_all_attributes=true`；
- `list_services` 指定 entity/domain 时返回字段明细，未指定时默认只返回服务名，便于按需展开。

---
## 12. 安全建议

- `HA_TOKEN` / `MCP_ENDPOINT` 仅环境变量注入，不写入镜像/代码；`.env` 不要提交到 Git（已加入 `.gitignore`）。
- bridge 主动连出，**不需要对外暴露任何端口**；不要在路由器/防火墙上为它做入站映射。
- 容器以非 root 用户（uid 10001）运行。
- 危险设备二次确认 token 只用于**防误操作**，不是权限控制；真正的访问权限仍由 HA 长期令牌决定。
- 建议为 HA 使用专用长期令牌，并仅授予必要的设备控制权限。
- 若 `mcp-endpoint-server` 在公网，使用 `wss://` 并确保 token 不泄露；`mcp_pipe.py` 日志已对 query 脱敏。
- WebUI 无独立密码、固定 `8099`；请只绑定可信内网，不要直接暴露公网，远程访问优先走 SSH 隧道或带鉴权的反向代理；页面和 API 只展示掩码后的敏感值。WebUI 已拦截危险设备操作并校验同源写请求，但这只是纵深防御，不能替代网络隔离。

---


## 13. 版本记录

| 版本 | 主要变化 |
|---|---|
| 未发布（审计修复） | 输入校验（entity_id/服务名/HA 响应类型）、脚本与系统级服务纳入二次确认、回读字段与重试逻辑修正、区域优先级修正、日志递归脱敏与尾部读取、WebUI 同源校验与危险操作拦截、bridge 重连退避/WS 大小限制/SIGTERM、容器健康检查与双进程守护、CI 增加测试与版本一致性校验 |
| v1.13.0 | 简化部署：WebUI 与 MCP bridge 合并为单容器（`docker-entrypoint.sh` 同时启动两进程）；WebUI 去掉独立密码与全部 `WEBUI_*` 环境变量，固定监听 `0.0.0.0:8099`，直接复用 `HA_URL` / `HA_TOKEN`；compose 收敛为单服务；冒烟测试改为无需登录 |
| v1.13.2 | WebUI 页头、服务日志面板与每条日志显示版本号；日志格式改为 `时间 级别 v版本 logger [call_id] 消息`，`/api/logs` 返回 `version`；bridge 启动日志记录自身版本 |
| v1.13.1 | 控制结果改为 `action=RESPONSE` + 中文 `response`，修复小智把 `turn_off` 当 `Action` 枚举导致播报“turn off”的问题，并省掉控制后的一次 LLM 往返；`call_service` 常见控制服务同样直接播报；WebUI 去重运行状态/配置、新增按区域与类型展示的可调用设备列表、新增 WebUI+bridge 共享日志面板 |
| v1.12.0 | 新增可选 WebUI（默认关闭）：状态页、六工具调试台、配置只读；FastAPI + 服务端渲染，无前端构建；独立 compose profile，不改变 bridge 出站拓扑；新增 `tests/webui_smoke.py` |
| v1.11.1 | 修复 `list_devices` 无过滤时忽略 `DEFAULT_LIST_LIMIT`、固定按 20 条分页的问题（现按配置通常 50 条）；默认提示动态显示条数，`has_more=true` 时提示用 `keyword`/`area` 精确过滤；回归测试增加 `applied_limit_gt: 20` 断言，防止再次退化 |
| v1.11.0 | `list_devices` 易用性与防误用：支持 `query`/`q`/`search`/`name` 关键字别名；未知参数返回 `invalid_argument` 并列出可用参数（不再静默忽略）；无过滤条件时默认 `controllable_only=true` 且每页 20 条；工具描述内置「点名设备直接 control_device」硬约束 |
| v1.10.1 | 全项目冗余审计与清理：补齐 README 第 9 章标题、清理 xiaozhi-server 旧「方式 B」MCP 配置（`deploy/data/.mcp_server_settings.json` 置空）、更新 `deploy/.env` 服务名并补充 `MCP_ENDPOINT` 说明；代码层确认已无 HTTP/SSE/uvicorn/端口残留 |
| v1.10.0 | 仅保留方式 A：移除 HTTP / SSE / streamable-http、8090 端口、`/health` / `/ready`、uvicorn 与方式 B 配置；`Dockerfile` 统一为 stdio bridge 镜像；`mcp_pipe.py` 只支持 stdio 子进程；测试改为 stdio 客户端；compose 不再暴露端口 |
| v1.9.1 | 全面审计修复：`HA_VERIFY_SSL` 非法值回退默认、`HA_URL` 启动校验、WebSocket 注册表按 `result.id` 收集、回读失败标记 `accepted_pending` 并返回 `readback_errors`、分页/布尔参数健壮性、危险确认令牌容量上限、补齐 `resume` 动作与区域名回退 |
| v1.9.0 | 新增区域名称返回与区域过滤：`list_devices` 支持 `area`，实体/设备视图返回 `area_id`/`area_name`，可与 `domain`/`keyword` 组合（如 `area="客厅", domain="light"`）；区域注册表失败时仅降级 `area_name` |
| v1.8.0 | 升级 MCP Python SDK 至 2.3.0：FastMCP 改为 MCPServer，e2e 客户端改用 mcp.Client；新增带 `call_id` 的结构化日志、工具调用耗时/错误码/动作回读记录与敏感信息脱敏，便于快速排障 |
| v1.7.0 | 新增小智 MCP 接入点（工具端 WebSocket）支持：`mcp_pipe.py`、bridge 镜像与 compose；README 补充方式 A 接入说明 |
| v1.6.0 | 项目、Python 包、Docker 服务/镜像/容器统一更名为 `homeassistant-xiaozhiserver`；README 全面重写；部署 compose、工具说明与错误处理文档完善 |
| v1.5.0 | 拆分为独立 Python 包；配置集中解析；属性裁剪；注册表失败短重试；危险 cover 识别；测试断言与清理 |
| v1.4.0 | 危险设备二次确认（一次性令牌防误操作） |
| v1.3.0 | 控制后状态 + 属性四态回读校验（confirmed/no_change/mismatch/accepted_pending） |
| v1.2.0 | 可控标记、危险标记、按物理设备聚合（WebSocket 注册表，优雅降级） |
| v1.1.0 | 新增 `list_services` 服务自描述与通用 `call_service`；控制三态反馈 |
| v1.0.0 | 初始版本：设备列表、状态查询、常用控制与 Docker 部署 |
