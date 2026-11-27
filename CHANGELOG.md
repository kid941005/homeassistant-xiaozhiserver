# Changelog

本项目的所有重要变更记录在此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

发布说明与镜像信息见 [GitHub Releases](https://github.com/kid941005/homeassistant-xiaozhiserver/releases)。

## [Unreleased]

暂无计划中的变更。

## [1.15.15] - 2026-11-27

### 修复
- 危险 cover 英文安全词支持复数与空格/下划线写法：`Window Blinds`、`Window Curtains`、`Window Shades`、`Window Screens`、`Door Bell` 不再误判；`window blind garage`、`window door curtain` 仍判高风险。

### 测试
- 新增英文复数与空格安全写法（含 `Window Blind Door Bell`）不误判回归。

## [1.15.14] - 2026-11-27

### 修复
- 危险 cover 安全词识别改为基于 `friendly_name + entity_id` 合并文本：仅 entity_id 含 `window_blind`/`window_curtain`/`window_shade`/`window_screen` 时也不再误判；`window_blind_garage`、`window_door_curtain` 仍判高风险。
- 单设备 `control_device` / `call_service` 的实体解析抛出非 `HAError`（如 `RuntimeError`）时统一返回 `status=not_sent`、`code=state_unavailable`，不再误报 `internal_error`。

### 测试
- 新增仅 entity_id 的英文安全名称不误判、仅 entity_id 的混合强危险词仍判危，以及单设备解析非 `HAError` 映射 `state_unavailable/not_sent` 回归。

## [1.15.13] - 2026-11-27

### 修复
- 英文安全名称含 window 时不再误判为门窗/车库风险：`window blind`、`window curtain`、`window shade`、`window screen` 名称安全；但名称中仍含 garage/gate/door（如 `window blind garage`、`window door curtain`）继续按高风险处理。
- 批量 `control_devices` 的 `partial` 布尔值改为与 `status` 同源派生，修复 accepted_pending/mismatch 等已下发但无成功 voice 时 `status=partial` 却 `partial=false` 的矛盾。

### 测试
- 新增英文安全名称 window blind/curtain/shade/screen 不误判、混合强危险词仍判危，以及批量混合解析+accepted_pending 时 `status=partial` 且 `partial=true` 回归。

## [1.15.12] - 2026-11-27

### 修复
- 单设备 `control_device`/`call_service` 在 POST 已发出后返回 `invalid_response` 或其他发送后异常统一标记 `accepted_pending`/`pending_confirmation`/`may_be_dispatched`，并附 `last_error_code` 提示先回读，不再误报普通失败诱导重试。
- 危险 cover 安全词只抵消安全对象本体，不再整体否决同一名称中的危险词：`百叶窗式车库门`、`纱窗门`、`门磁式车库门`、`窗帘式车库门` 仍判为高风险。
- 批量多目标解析抛出非 `HAError` 异常时，错误项补 `status=not_sent`；顶层按是否有目标实际下发区分 `partial`/`not_sent`。
- 阶段一刷新后动作参数校验失败补 `status=not_sent`，与未下发语义一致。
- 过期确认令牌统一返回 `confirm_token_expired`，不再与无效令牌混淆。

### 测试
- 新增单设备 `control_device`/`call_service` invalid_response 映射 accepted_pending 且令牌消费、混合危险名称仍判危、过期令牌 `confirm_token_expired` 回归。

## [1.15.11] - 2026-11-27

### 修复
- 危险 cover 安全词排除同时作用于英文实体 ID 分支：`cover.living_room_window` 配合 百叶窗/纱窗/窗帘 等名称不再误报；新增 `blind`/`curtain`/`shade`/`screen` 安全词，仅 `screen door`（纱门）仍按高风险处理。

### 测试
- 新增英文 entity_id 含 window 的安全窗帘/纱窗、窗纱、投影幕不误判，以及纱门仍判危的回归用例。

## [1.15.10] - 2026-11-27

### 修复
- 确认令牌锁键统一 `strip` 规范化，空白变体并发请求共享同一全局锁；令牌锁仅对真实存在且长度合理的令牌生效，避免随机 token 导致锁表无界增长。
- 批量总时限在获取令牌锁之前开始计算，同令牌并发第二个请求不会在锁等待后获得额外预算；等待超时统一 `not_sent`/`batch_timeout`。
- 批量阶段一注册表读取、实体解析与重复开关上下文读取捕获非 `HAError` 异常，统一 `registry_unavailable`/`state_unavailable` 与 `not_sent`，避免冒泡 `internal_error`。
- 通用 `call_service` 在与 `HAClient` 相同的服务名语法预校验之后才进入危险确认，非法服务名不消费令牌。
- 危险 cover 中文名称恢复显式高危词 道闸/闸机/起落杆/闸杆/卷闸，并排除 百叶窗/纱窗，避免漏判与误判。

### 测试
- 新增空白变体同一令牌并发、注册表/实体解析 RuntimeError、非法服务名不消费令牌、道闸/闸机/起落杆正例与百叶窗/纱窗排除回归。

## [1.15.9] - 2026-11-27

第三轮对抗复核问题修复版本。

### 修复

- 批量阶段二 late-danger 且已有目标执行时，不再返回绑定整批的自动 confirm_token；该令牌立即消费并从响应中移除，消息改为提示仅对未执行目标重新发起确认，避免用旧令牌重试整批导致已执行目标重复执行。
- control_device/call_service 令牌只在 dispatch_state.dispatched 为真（真实开始 POST）时消费；锁等待取消、POST 前参数校验失败等尚未下发的路径保留令牌，符合全部 not_sent 语义。
- _consume_confirm_token 增加 expected_intent 校验，普通成功操作不再误消费其他危险操作遗留的令牌。
- 批量首次 get_states 与 _redundant_switch_context 第二次 get_states 的非 HAError 异常统一按 state_unavailable/not_sent 失败关闭，不再冒泡 internal_error。
- 批量阶段一刷新失败返回的 state_unavailable 补充 status=not_sent。
- 批量总时限在各阶段入口判定，deadline 已过期时不再用 max(0.01,..) 发起新操作。
- late-danger 部分结果保留 errors 与 deduplicated，避免部分失败信息丢失。
- 同一 confirm_token 的 control_device/call_service/control_devices 入口增加按令牌串行锁：并发请求只有一个能通过复核并执行，另一个因令牌已消费返回 invalid_confirm_token，避免一次性令牌被并发重放两次。
- 危险 cover 中文识别改为“门/窗”词根匹配并排除门铃/门磁/窗帘/窗纱/门帘，覆盖主卧窗、厨房门、阳台门等不写 device_class 的常见命名。

### 测试

- confirm_window 新增/加强：late-danger 部分结果不得返回可授权整批的令牌、重试整批不重复执行已执行目标；锁等待取消后令牌保留并可执行一次；invalid_response 与批量取消改用真实 call_service 边界而非伪造 dispatch_state；同一令牌并发批量只执行一次；中文门窗词根与门铃/窗帘等排除项；阶段一 RuntimeError 返回 state_unavailable/not_sent。

## [1.15.8] - 2026-11-27

第二轮最终对抗复核问题修复版本。

### 修复

- 通用 call_service 与 control_device 一致：初始缓存可能缺少危险属性时，非空 confirm_token 不再跳过锁内 fresh 危险复查，任意伪造令牌无法打开车库门/门锁；令牌校验通过后在真实下发边界统一消费。
- 有效令牌在锁内 fresh 刷新后设备已不再判定为危险时也会被消费，避免同一令牌在 TTL 内重放执行第二次。
- 批量阶段二发现 late-danger 时不再直接丢弃并发 gather 中其他已执行目标的副作用，返回确认请求同时附带 executed_entities/results 部分结果。
- 批量入口第二次 get_states（状态首次加载失败时由 _redundant_switch_context 触发）纳入 CONTROL_BATCH_TIMEOUT，超时返回 not_sent/batch_timeout。
- 批量阶段一 force 刷新遇到非 HAError（RuntimeError/TypeError 等）同样按 state_unavailable 失败关闭，不再冒泡 internal_error。
- 无 entity_lock 的后备执行路径也先 force 回源并在最新状态上做危险 precheck，不再直接使用旧缓存。
- 批量令牌消费契约明确：任一目标实际开始下发即消费令牌，全部 not_sent 时保留，避免部分执行后令牌重放。
- 危险 cover 中文关键词补充 入户门/庭院门/电动门/道闸/闸机。

### 测试

- confirm_window 新增：通用 call_service 任意非空令牌被 fresh 拒绝；同一有效令牌只能执行一次；批量双目标 late-danger 返回部分结果且已下发目标不丢失；批量 POST 后 invalid_response、control_device/call_service 取消、批量控制取消场景均消费令牌。

## [1.15.7] - 2026-11-27

最终对抗复核问题修复版本。

### 修复

- 危险确认令牌不再仅凭非空跳过锁内 fresh 复查：初始缓存看似安全时，force 刷新发现车库门/高风险设备仍会拒绝无效令牌并返回 invalid_confirm_token。
- 批量阶段一到阶段二之间的 late-danger 在实体锁内 fresh 状态重新判定；令牌只在整批全部通过后统一消费，避免被首台目标提前消费导致其余目标或 late-danger 失效。
- 批量确认令牌 intent 使用稳定的 batch/control_devices 前缀，输入顺序变化不再触发 confirm_token_mismatch。
- 批量总时限从入口开始计时，状态/注册表加载与实体解析也受 CONTROL_BATCH_TIMEOUT 约束，超时返回 not_sent/batch_timeout。
- force GET 不再覆盖请求期间到达的较新 WebSocket 事件（实体时间戳/generation 双重校验，必要时重试）。
- 锁内刷新失败统一返回 state_unavailable（含非 HAError 异常），带 confirm_token 也不降级；`_accepts_force` 改为签名 bind 判断。
- 危险 cover 中文关键词补充 前门/后门/侧门/卷帘门；批量超时分支补充 dispatch_state=may_be_dispatched。

### 测试

- 新增 confirm_window 回归：缓存未识别危险 + 任意非空令牌、批量 late-danger 均拒绝且不调用 call_service。

## [1.15.6] - 2026-11-27

对抗性复核问题修复版本。

### 修复

- 注册表读取返回 None（事件流禁用等）时不再按空映射放行，配置设备名排除关键字时 list_devices/list_domains 与实体解析均失败关闭。
- 实体锁内 force 刷新初始状态后重新执行危险判定，避免缓存缺少 device_class 时放行车库门等设备；刷新失败且需要危险复查时返回 state_unavailable，不再沿用旧状态下发。
- 批量控制改为两阶段：先对所有目标 force 刷新并整批完成危险判定，全部通过后才统一下发；带 confirm_token 时也先整批校验完整危险列表，避免部分设备已执行、另一台才要求确认而丢弃结果。
- HTTP POST 超时或网络中断（HAClient 的 timeout/network_error/upstream_error）统一映射为 post_timeout，批量与单设备返回 accepted_pending/dispatch_state=may_be_dispatched，不再误报为确定失败。
- 状态缓存 WS 快路径校验全量快照年龄，事件流静默失效后回源 REST，避免长期使用旧快照；在途 REST 全量快照带 generation 比对，不覆盖期间到达的事件更新；状态缓存时间统一使用 monotonic，避免系统时钟回拨导致缓存判定异常。
- cover 过渡态按动作方向判断：open_cover 仅接受 opening、close_cover 仅接受 closing，反向过渡不再当成功。
- climate 按能力位过滤 turn_on/turn_off/set_temperature/set_hvac_mode，避免暴露不支持动作。
- 批量超时通过 dispatch_state 区分 not_sent/batch_timeout 与可能已下发 accepted_pending/pending_confirmation。
- 数值参数显式拒绝 NaN/Infinity；华氏设备仅在缺少温度范围且值疑似摄氏度时拒绝；RGB 分量拒绝浮点与非有限值。
- 危险 cover 的英文关键词改为词边界匹配，doorbell 不再被 door 误判为高风险。
- WebUI 服务名内部空白分别规范化，scene. turn_on 不再能绕过危险闸门。
- call_service 的 data 目标选择器新增 entities，防止绕过 20 条上限与主实体危险判定。
- 登录限速改为表单解析成功且令牌确实错误时才占用配额，畸形/超大请求不再锁死同源用户。
- 删除 requires_confirmation 分支的重复工具指标；mcp_pipe 单行输出按 UTF-8 字节限制 16MiB。

### 安全

- MCP 主入口的 scene.turn_on/toggle、homeassistant.turn_on/toggle 指向 scene 也纳入共享危险确认，不再只依赖 WebUI 拦截。
- homeassistant.turn_on/turn_off/toggle 指向 script、lock、shell_command 等高风险目标域时同样判定为危险操作，不能借通用 domain 绕过脚本/门锁确认。

## [1.15.5] - 2026-11-27

安全边界、状态一致性与能力过滤修复版本。

### 修复

- `resolve_entity` 对合法但不存在的 `entity_id` 不再回退到名称模糊匹配，避免误控 friendly_name 恰好同名的实体；HA 返回 `not_found` 时直接视为未找到。
- 状态缓存按实体时间戳判断新鲜度：事件流就绪时仅在缓存年龄不超过 `HA_STATE_CACHE_TTL` 时直接命中，过期或 `invalidate_states` 后强制回源 REST。
- `call_service` 无论调用成败都在 `finally` 删除目标实体的内存快照与时间戳并失效全量缓存，避免 POST 已执行但回读旧状态造成假成功或漏报。
- 实体控制锁内先 `get_state(force=True)` 刷新初始状态，修复并发 toggle 基于旧状态判断预期的问题。
- 回读确认增加基于 `SERVICE_CONFIRM_TIMEOUT` 的硬截止，单次等待、休眠与状态读取均受剩余时间约束，慢设备不再远超超时时间。
- `cover` 的 `open_cover`/`close_cover` 在回读为 `opening`/`closing` 时返回 `accepted_pending`，慢速门不再被误判失败。
- 能力位修正：`fan.set_percentage` 使用 `SET_SPEED=1`，`vacuum` 使用 `START/STOP` 对应位，`climate` 仅在仅支持目标区间时不再暴露单温度动作。
- 注册表不可用时，`list_devices`/`get_device_state`/`list_domains` 在配置了设备名排除关键字时失败关闭并返回 `registry_unavailable`；重复开关状态读取失败返回 `state_unavailable`。
- 批量控制逐条解析的非预期异常被隔离，单个实体 HAError 不再中断整批；批量 `gather` 的 HAError 保留原错误码（`auth_failed`/`timeout` 等）而非统一降级为 `internal_error`。
- 危险 `cover` 在缺少 `device_class` 时按 entity_id/friendly_name 的 garage/gate/window/door/车库/大门/窗户关键词兜底判定。
- 数值校验补充 `brightness_pct`/`brightness`/`volume_level`/`position`/`percentage`/`tilt_position` 边界，华氏设备拒绝疑似摄氏值，RGB 分量拒绝浮点与布尔值。
- 消除 WebUI 工具调用指标的重复计数；`pipe_process_to_websocket` 拒绝超过 16MiB 的单行消息并断开重连。

### 安全

- WebUI 危险闸门支持 `call_service` 的裸服务名、`scene.turn_on`/`toggle`、`homeassistant.turn_on` 指向 scene，以及 `data.entity_id`/`entities` 候选，先去重再限 20 个目标。
- 登录失败限速改为锁内原子预留尝试，避免并发请求绕过阈值；`WEBUI_TOKEN` 比较改用 UTF-8 字节，非 ASCII 令牌不再触发 500。
- 指标 HTTP 方法标签归一为固定集合，未知方法记为 `OTHER`，避免 Prometheus 标签基数无界增长。
- CI 拆分 PR 构建与发布任务，PR 仅 `contents: read` 且不推送；发布任务最小权限；所有 Actions 固定提交 SHA；版本输入经正则校验，`latest` 仅在 tag 推送时追加。

## [1.15.4] - 2026-11-26

登录失败来源表上限修正版本。

### 修复

- 修复 `_record_login_failure` 新增来源后未再次裁剪、导致来源表可能短暂超过 `_MAX_LOGIN_SOURCES` 一项的问题；`tests/webui_smoke.py` 增加回归断言。

## [1.15.3] - 2026-11-26

性能、安全边界与可维护性修复版本。

### 修复

- 批量控制复用预加载状态：`resolve_entity` 命中传入的 states 时不再逐条请求 REST，降低批量控制 N+1 请求。
- 事件流状态缓存按实体时间戳判断新鲜度，超过 `HA_STATE_CACHE_TTL` 或落后时回源 REST；控制调用后清除目标实体时间戳，确保回读拿到新状态；`wait_for_state` 先注册等待者再检查当前值，消除事件丢失窗口。
- 能力过滤补齐：`supported_color_modes=[]` 时不再暴露亮度/色温/颜色动作；`vacuum` 按 `supported_features` 位过滤启动/停止/暂停等动作。
- 批量超时区分“未下发”（`not_sent`/`batch_timeout`）与“已下发待确认”（`accepted_pending`/`pending_confirmation`），MCP 工具文档同步。
- 即发即忘与无状态服务的回读预期修正；亮度百分比回读容差放宽到 5，减少不同灯具刻度造成的误报。
- 注册表不可用时危险设备检查失败关闭，批量入口复用一次注册表读取。

### 安全

- `/metrics` 移出公开路径，需会话 Cookie 或 `Authorization: Bearer <WEBUI_TOKEN>`。
- WebUI CSP 去掉 `unsafe-inline`，页面脚本与样式改用 nonce；模板按钮改为 `id` 绑定事件，移除内联 `onclick`。
- 登录失败来源表设置上限并定期清理，避免反向代理共享 IP 或大量来源造成内存增长；表单解析区分 415/413/400。

### 性能

- 指标落盘改为首次/2 秒/20 条节流，并每小时主动清理超过 24 小时的共享快照。
- `/metrics` 渲染放入 `asyncio.to_thread`，避免阻塞事件循环。
- WebUI 日志尾部读取按文件 `mtime/size` 缓存，未变化时不再重复解析 256KB。
- 信号处理改为设置停止标志并触发正常取消，确保 bridge 心跳取消与断开状态写入。

## [1.15.2] - 2026-11-25

指标、危险闸门与部署加固版本。

### 修复

- `/metrics` 合并 WebUI 进程与 stdio MCP server 子进程的指标（共享临时目录），工具调用指标不再因进程隔离丢失。
- WebUI 危险闸门按 `control_device`/`control_devices` 的 `action` 映射实际 HA 服务后再判定，`script.turn_on` 等不再因只传 action 而绕过；`scene.turn_on` 视为可能包含门锁动作的高风险操作。
- 批量控制超时区分“尚未下发”（`status=not_sent`、`code=batch_timeout`）与“已下发但未确认”（`accepted_pending`/`pending_confirmation`），不再对未下发指令播报已发送。
- 删除“无预期且 HA 返回空响应即视为 confirmed”的判定；`cover`/`valve` 的 `turn_on`/`turn_off` 按 `open`/`closed` 回读，bool 属性缺失不再当作 `expected=false` 匹配。
- 确认令牌意图对目标列表排序后再生成，实体顺序或批量顺序变化不再导致 `confirm_token_mismatch`。
- 注册表不可用且配置了设备关键词排除时，控制入口失败关闭并返回 `registry_unavailable`。
- `set_color_temp` 色温改为四舍五入，避免边界值差 1K。

### 安全

- WebUI 登录失败限速：同一来源 5 分钟内失败 5 次后返回 429 与 `Retry-After`；会话表上限 512，异常刷量时淘汰最早过期会话。
- 登录 Cookie 仅在 HTTPS 直连时加 `Secure`（反向代理场景由 uvicorn `proxy_headers` 处理）；登录成功/失败写入日志。
- 所有请求（含 401/403 提前返回）都计入 HTTP 指标。
- 容器健康检查改用 `/readyz`，覆盖 HA 与 bridge 就绪状态；`/readyz` 按 `HA_TIMEOUT` 探测 HA，避免冷启动慢请求被 3s 固定超时误判，`HEALTHCHECK` 超时放宽到 20s，`start-period` 60s。
- compose 增加 `pids_limit`、`mem_limit`、`cpus`，`/tmp` tmpfs 限制为 64MB；`.dockerignore` 排除日志目录。
- CI 在 pull request 上也构建镜像但不推送；新增 `tests/healthcheck_probe.py` 覆盖 `/readyz` 200/503 与候选主机顺序。

## [1.15.1] - 2026-11-25

控制链路语义、能力过滤与批量控制修复版本。

### 修复

- `call_service` 成功判定与 `control_device` 统一：仅 `confirmed`/`accepted`，或 `no_change` 且已验证时返回 `ok=true`；未验证的 `no_change` 返回 `code=not_verified`。
- `scene.turn_on` / `script.turn_on` / `automation.trigger` / `cover.stop_cover` 等无状态服务返回 `status=accepted`、`ok=true`，不再误判为 `accepted_pending`；无预期动作时提前结束回读。
- 能力过滤读取 `supported_features`、`supported_color_modes`、温度/色温范围等属性，避免向设备暴露不支持的动作。
- 新增 `set_percentage`、`set_position`、`set_hvac_mode`、`set_fan_mode`，并在下发前校验温度、色温、模式取值范围。
- 批量控制预加载状态与注册表、复用实体解析、重复目标去重；新增 `CONTROL_BATCH_TIMEOUT` 总时限，超时条目返回 `pending_confirmation`，失败播报包含设备名。
- 回读轮询在事件流未命中时降低 REST 回退频率（最多每 0.5 秒一次，最后一轮强制），减少 HA 压力。
- `stop` / `pause` / `continue` / `resume` 不再兜底到 `homeassistant.*`，不支持时返回 `unsupported_action`。
- `resolve_entity` 接受任意类型并按字符串处理，避免 WebUI 传数字时报 `internal_error`。

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
