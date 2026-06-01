# Central Device Control Bridge API 接口文档

> 当前版本：`SNOW_DEVICE_CONTROL_BRIDGE` / FastAPI `0.1.0`  
> 基准地址：`http://127.0.0.1:8010`（测试默认）  
> 代码来源：`src/device_control_bridge/app.py`、`src/device_control_bridge/service.py`  
> 更新时间：2026-06-01

## 1. 设计边界

中央 Bridge 只负责设备与控制链路，不保存客户账务：

- 设备注册、在线状态、运行态遥测、设备模式、是否启用、是否接单。
- control session 的创建、续约、释放、失联保护。
- command / command bundle 的排队、领取、ACK、取消。
- event 事件流，供商户服务器拉取恢复本地订单状态。

中央 Bridge 不保存：客户账号、客户余额、订单金额、退款、购买时长、订单到期时间。上述数据由商户服务器保存。

## 2. 鉴权与幂等

### 2.1 Merchant API HMAC 头

除 `/health` 与 `/api/agent/*` demo 接口外，`/api/merchant/v1/*` 全部需要 HMAC。

必带请求头：

| Header | 必填 | 说明 |
|---|---:|---|
| `X-Merchant-Key` | 是 | API Key ID，例如测试默认 `mk_test` |
| `X-Merchant-Timestamp` | 是 | Unix 秒；允许约 300 秒时间偏差 |
| `X-Merchant-Nonce` | 是 | 一次性随机串；10 分钟内防重放 |
| `X-Merchant-Body-SHA256` | 是 | 原始 body bytes 的 SHA256 hex；GET/空 body 用空字节 SHA256 |
| `X-Merchant-Signature` | 是 | HMAC-SHA256 hex |
| `Content-Type` | 写接口 | `application/json` |
| `X-Idempotency-Key` | 写接口必填 | POST/PUT/DELETE 等写接口必须提供 |

签名原文：

```text
METHOD
PATH
QUERY_STRING
TIMESTAMP
NONCE
BODY_SHA256
```

签名算法：

```text
hex(HMAC_SHA256(merchant_secret, canonical_string))
```

注意：`QUERY_STRING` 是 URL 中原始 query，不含 `?`。

### 2.2 Scope

| Scope | 用途 |
|---|---|
| `machines.read` | 读取设备、容量、能力 |
| `machines.control` | 创建设备、修改设备、创建控制会话、下发命令 |
| `sessions.read` | 读取 session、事件流 |
| `sessions.write` | 创建/续约/释放 session |
| `commands.read` | 查询命令、命令包 |
| `commands.write` | 下发/取消命令 |

测试默认 scope 通常包含以上全部。

### 2.3 幂等规则

写接口必须带 `X-Idempotency-Key`。

- 同一 `X-Merchant-Key + X-Idempotency-Key + 请求体 SHA256` 重放：返回首次响应。
- 同一幂等键但请求体不同：`409 idempotency_conflict`。

## 3. 接口总表

### 3.1 基础

| 方法 | 路径 | 鉴权 | 幂等 | 说明 |
|---|---|---|---|---|
| GET | `/health` | 否 | 否 | 健康检查 |

### 3.2 Merchant API：设备与容量

| 方法 | 路径 | Scope | 幂等 | 说明 |
|---|---|---|---|---|
| GET | `/api/merchant/v1/devices` | `machines.read` | 否 | 获取当前商户设备列表 |
| POST | `/api/merchant/v1/devices` | `machines.control` | 是 | 新增设备码/机器绑定 |
| PUT | `/api/merchant/v1/devices/{device_id}` | `machines.control` | 是 | 编辑设备信息 |
| PUT | `/api/merchant/v1/devices/{device_id}/mode` | `machines.control` | 是 | 切换模式：机密/混合/绝密 |
| PUT | `/api/merchant/v1/devices/{device_id}/toggle` | `machines.control` | 是 | 启用/禁用设备 |
| PUT | `/api/merchant/v1/devices/{device_id}/accept-orders` | `machines.control` | 是 | 停止/恢复接单 |
| DELETE | `/api/merchant/v1/devices/{device_id}` | `machines.control` | 是 | 删除设备；有活动 session 时拒绝 |
| GET | `/api/merchant/v1/devices/{device_id}/capabilities` | `machines.read` | 否 | 查询设备 capabilities |
| GET | `/api/merchant/v1/capacity` | `machines.read` | 否 | 查询可自动分配容量 |

### 3.3 Merchant API：事件与 Session

| 方法 | 路径 | Scope | 幂等 | 说明 |
|---|---|---|---|---|
| GET | `/api/merchant/v1/events?cursor=&limit=` | `sessions.read` | 否 | 拉取事件流 |
| GET | `/api/merchant/v1/events/{event_id}` | `sessions.read` | 否 | 查询单个事件 |
| GET | `/api/merchant/v1/control-sessions?status=&limit=` | `sessions.read` | 否 | 查询 session 列表 |
| GET | `/api/merchant/v1/control-sessions/by-ref/{merchant_context_ref}` | `sessions.read` | 否 | 按 opaque ref 恢复 session |
| POST | `/api/merchant/v1/control-sessions` | `sessions.write` + `machines.control` | 是 | 创建 control session |
| POST | `/api/merchant/v1/control-sessions/{session_id}/renew` | `sessions.write` | 是 | 续约技术租约 |
| GET | `/api/merchant/v1/control-sessions/{session_id}/state` | `sessions.read` | 否 | 查询 session + device 当前状态 |
| POST | `/api/merchant/v1/control-sessions/{session_id}/release` | `sessions.write` | 是 | 主动释放 session |

### 3.4 Merchant API：命令

| 方法 | 路径 | Scope | 幂等 | 说明 |
|---|---|---|---|---|
| POST | `/api/merchant/v1/control-sessions/{session_id}/commands` | `commands.write` + `machines.control` | 是 | 下发单条命令 |
| POST | `/api/merchant/v1/control-sessions/{session_id}/command-bundles` | `commands.write` + `machines.control` | 是 | 下发顺序命令包 |
| POST | `/api/merchant/v1/control-sessions/{session_id}/commands/{command_id}/cancel` | `commands.write` + `machines.control` | 是 | 取消命令 |
| GET | `/api/merchant/v1/control-sessions/{session_id}/bundles/{bundle_id}` | `commands.read` | 否 | 查询命令包状态 |
| GET | `/api/merchant/v1/commands/{command_id}` | `commands.read` | 否 | 查询命令状态 |
| POST | `/api/merchant/v1/webhook/test` | `sessions.read` | 是 | 测试用事件回显 |

### 3.5 Agent Demo API

当前 demo Agent 接口未加机器侧鉴权，正式部署应增加机器密钥/签名。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/agent/heartbeat` | 无 | Agent 上报在线/状态/OCR/运行态 |
| POST | `/api/agent/commands/claim` | 无 | Agent 拉取待执行命令 |
| POST | `/api/agent/commands/{command_id}/ack` | 无 | Agent 回执命令结果 |

## 4. 设备接口

### 4.1 设备对象字段

`GET /api/merchant/v1/devices` 返回的设备对象包含 DB 字段和 runtime 展开字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | Bridge 内部设备 ID |
| `tenant_id` | int | 商户/租户 ID |
| `machine_id` | string | Agent 使用的机器 ID/设备码，唯一 |
| `display_name` | string | 后台显示名称 |
| `online` | int/bool | 是否在线 |
| `control_state` | string | `offline / idle / claimed / commanding / running / busy` 等 |
| `agent_state` | string | Agent 原始状态 |
| `ui_state` | string | UI 状态 |
| `mode` | string | `machine / hybrid / absolute` |
| `enabled` | int/bool | 是否启用设备配置 |
| `accept_orders` | int/bool | 是否允许客户自动接单 |
| `active_control_session_id` | string/null | 当前占用 session |
| `device_epoch` | int | 设备状态版本，命令前置条件使用 |
| `last_heartbeat_at` | string/null | 最近心跳时间 |
| `capabilities` | array | Agent 能力列表 |
| `runtime` | object | Agent 上报的运行态原文 |

常见 runtime 展开字段：

- `work_status`, `running_user`, `running_boss_name`, `team_code`
- `spectate_boss`, `boss_id`, `boss_id_debug`
- `remaining_minutes`, `remaining_seconds`, `end_time`, `end_time_ms`, `estimated_end`
- `harvard`, `hfb_value`, `round_count`, `max_rounds`
- `start_coins`, `max_coin_loss`, `actual_coin_loss`, `script_ver`
- `watchdog`, `watchdog_card`, `current_map`, `in_game`
- prison 相关：`prison_stage`, `prison_point`, `prison_score` 等

### 4.2 新增设备

```http
POST /api/merchant/v1/devices
```

请求：

```json
{
  "machine_id": "demo-online-idle",
  "display_name": "示例1 在线空闲",
  "mode": "hybrid",
  "radar_url": "https://radar.local/demo",
  "watchdog_card": "",
  "accept_orders": true
}
```

兼容字段：`device_key` 等同 `machine_id`，`device_name` 等同 `display_name`。

响应：

```json
{
  "ok": true,
  "msg": "创建设备成功",
  "device": { "id": 1, "machine_id": "demo-online-idle", "mode": "hybrid" }
}
```

### 4.3 编辑设备

```http
PUT /api/merchant/v1/devices/{device_id}
```

可传字段：

```json
{
  "machine_id": "new-machine-id",
  "display_name": "1号机",
  "mode": "machine",
  "enabled": true,
  "accept_orders": true,
  "radar_url": "",
  "watchdog_card": ""
}
```

### 4.4 模式/启用/接单快捷接口

```http
PUT /api/merchant/v1/devices/{device_id}/mode
{ "mode": "machine" }
```

```http
PUT /api/merchant/v1/devices/{device_id}/toggle
{ "enabled": false }
```

```http
PUT /api/merchant/v1/devices/{device_id}/accept-orders
{ "accept_orders": false }
```

`accept_orders=false` 的设备：

- 仍可在线、仍可展示、仍可被维护命令使用。
- 不参与客户自动分配。
- 不计入商户前台“可接单”容量。

### 4.5 容量

```http
GET /api/merchant/v1/capacity
```

当前实现只统计：

```text
enabled = true
accept_orders = true
online = true
control_state = idle
active_control_session_id IS NULL
```

响应：

```json
{
  "ok": true,
  "available": true,
  "capacity_label": "few",
  "idle_device_ids": [1]
}
```

`capacity_label`：

- `many`：空闲可接单设备数 >= 3
- `few`：有空闲但少于 3
- `full`：无可接单空闲设备

## 5. Control Session 接口

### 5.1 创建 Session

```http
POST /api/merchant/v1/control-sessions
```

请求：

```json
{
  "auto_assign": true,
  "device_id": null,
  "merchant_context_ref": "opaque-order-ref",
  "purpose": "customer_control",
  "technical_lease_ttl_seconds": 180,
  "expected_device_state": "idle",
  "takeover_policy": "reject",
  "selection_policy": {
    "order_quality": "secret",
    "privacy_mode": true,
    "min_device_coin_balance": 120000
  }
}
```

当前 Bridge 代码接收但暂未使用 `selection_policy` 深度筛选；商户侧仍可传，用于后续扩展。

响应：

```json
{
  "ok": true,
  "control_session": {
    "control_session_id": "cs_xxx",
    "device_id": 1,
    "fencing_token": "ft_xxx",
    "status": "active",
    "technical_lease_expires_at": "2026-06-01T00:00:00+00:00",
    "device_epoch": 3
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `merchant_context_ref` | 商户订单 opaque 引用；Bridge 只保存 hash |
| `fencing_token` | 后续续约、释放、下发命令必带 |
| `device_epoch` | 设备版本；用于命令防止旧状态下发 |
| `purpose` | `customer_control / manual_admin_order / admin_device_maintenance` 等 |
| `takeover_policy` | 默认 `reject`；`admin_force` 可用于强制接管逻辑 |

### 5.2 续约

```http
POST /api/merchant/v1/control-sessions/{session_id}/renew
```

请求：

```json
{
  "fencing_token": "ft_xxx",
  "technical_lease_ttl_seconds": 180
}
```

响应：

```json
{
  "ok": true,
  "control_session_id": "cs_xxx",
  "technical_lease_expires_at": "...",
  "device_epoch": 3
}
```

商户服务器建议每 60 秒续约一次。

### 5.3 查询状态

```http
GET /api/merchant/v1/control-sessions/{session_id}/state
```

响应：

```json
{
  "ok": true,
  "control_session": { "id": "cs_xxx", "status": "active" },
  "device": { "id": 1, "control_state": "running" }
}
```

### 5.4 按 merchant_context_ref 恢复

```http
GET /api/merchant/v1/control-sessions/by-ref/{merchant_context_ref}
```

用于商户服务重启后，按本地订单 opaque ref 找回 Bridge session。

### 5.5 主动释放

```http
POST /api/merchant/v1/control-sessions/{session_id}/release
```

请求：

```json
{
  "fencing_token": "ft_xxx",
  "release_reason": "merchant_order_closed"
}
```

释放后：

- session 状态变为 `released`。
- 未执行命令取消。
- 设备回到 `idle`。
- 产生 `control_session.released` 事件。

## 6. Command 接口

### 6.1 单条命令

```http
POST /api/merchant/v1/control-sessions/{session_id}/commands
```

请求：

```json
{
  "fencing_token": "ft_xxx",
  "action": "stop_current",
  "params": { "reason": "merchant_order_finished", "cleanup": true },
  "expected_device_epoch": 3,
  "expected_ui_state": null,
  "command_ttl_seconds": 30,
  "client_command_ref": "optional-ref"
}
```

响应：

```json
{
  "ok": true,
  "command": {
    "command_id": "cmd_xxx",
    "control_session_id": "cs_xxx",
    "device_id": 1,
    "seq": 5,
    "action": "stop_current",
    "status": "queued",
    "expires_at": "...",
    "bundle_id": null
  }
}
```

### 6.2 命令包

```http
POST /api/merchant/v1/control-sessions/{session_id}/command-bundles
```

请求：

```json
{
  "fencing_token": "ft_xxx",
  "mode": "sequential_stop_on_error",
  "expected_device_epoch": 3,
  "commands": [
    { "action": "set_loadout", "params": { "quality": "secret", "loadout_id": "default_secret" } },
    { "action": "enter_team", "params": { "team_code": "ABC1234", "clear_existing": true } },
    { "action": "ready", "params": {} },
    { "action": "watch", "params": { "ace_enabled": true, "ace_window_seconds": 120 } }
  ]
}
```

当前只支持 `sequential_stop_on_error`：上一条命令终态成功后，Agent 才会领取下一条；失败时取消后续命令并发 `bundle.failed`。

### 6.3 常用 action 约定

Bridge 不做强 schema 校验，Agent 根据 action 执行。当前商户端约定：

| action | 用途 | 常见 params |
|---|---|---|
| `set_loadout` | 设置配置/配装 | `quality`, `loadout_id`, `loadout_type`, `items`, `total_cost` |
| `enter_team` | 进队/换队 | `team_code`, `clear_existing`, `operator` |
| `rejoin` | 换队别名 | `team_code` |
| `ready` | 准备 | `{}` |
| `watch` | 开始观察 | `ace_enabled`, `ace_window_seconds`, `max_rounds`, `max_coin_loss_w` |
| `stop_current` | 结束当前订单/清理 | `reason`, `cleanup` |
| `cleanup` | 清理 | `reason` |
| `restart` | 异常重启 | `operator` |
| `restart_backup` | 重启备用电脑/名刀 | `operator` |
| `update` | 远程更新 | `operator` |
| `collect_log` | 回收日志 | `operator` |

### 6.4 查询/取消

```http
GET /api/merchant/v1/commands/{command_id}
```

```http
GET /api/merchant/v1/control-sessions/{session_id}/bundles/{bundle_id}
```

```http
POST /api/merchant/v1/control-sessions/{session_id}/commands/{command_id}/cancel
{
  "fencing_token": "ft_xxx",
  "reason": "merchant_cancel"
}
```

## 7. Event 事件流

### 7.1 拉取事件

```http
GET /api/merchant/v1/events?cursor=0&limit=100
```

响应：

```json
{
  "ok": true,
  "events": [
    {
      "id": "evt_xxx",
      "event_seq": 1,
      "event": "device.ready_for_customer_timer",
      "device_id": 1,
      "control_session_id": "cs_xxx",
      "command_id": null,
      "device_epoch": 4,
      "payload": { "basis": "watch_succeeded" },
      "created_at": "..."
    }
  ],
  "next_cursor": 1
}
```

`cursor` 使用上一轮返回的 `next_cursor`。

### 7.2 重要事件

| 事件 | 说明 | 商户侧动作 |
|---|---|---|
| `control_session.created` | session 创建 | 记录绑定 |
| `command.queued` | 命令入队 | 可展示等待执行 |
| `command.delivered` | Agent 已领取 | 可展示执行中 |
| `command.succeeded` | 命令成功 | 根据 action 更新订单/设备 |
| `command.failed` | 命令失败 | 标记异常或等待处理 |
| `command.canceled` | 命令取消 | 更新本地状态 |
| `bundle.failed` | 命令包失败，后续取消 | 订单失败/退款/人工处理 |
| `device.state_changed` | Agent 心跳状态变化 | 更新设备总览 |
| `device.ready_for_customer_timer` | 可以开始客户计时 | 商户本地订单开始计时 |
| `control_session.stale` | 技术租约未续，进入 stale | 尝试恢复/告警 |
| `control_session.expired` | 失联超过保护窗口 | 等待系统 stop/cleanup |
| `control_session.released` | session 已释放 | 订单结束/设备释放 |
| `control_session.revoked` | 被强制接管 | 本地标记中断 |
| `admin.takeover` | 管理员强制接管 | 审计/中断处理 |

## 8. Agent Demo 接口

### 8.1 心跳

```http
POST /api/agent/heartbeat
```

请求：

```json
{
  "machine_id": "demo-online-idle",
  "work_status": "已进队",
  "in_game": false,
  "running_user": "user1",
  "running_boss_name": "ABC1234",
  "team_code": "ABC1234",
  "boss_id": "BOSS7788",
  "boss_id_debug": "ocr-ok",
  "harvard": "88.5W",
  "round_count": 2,
  "max_rounds": 5,
  "script_ver": "v9.1"
}
```

说明：

- `machine_id` 或 `device_key` 用于定位设备。
- `boss_id` 和 `spectate_boss` 互为别名，Bridge 会自动补齐。
- `work_status=空闲/idle` 且无 active session 时，设备回到 `control_state=idle`。
- `work_status=离线/offline` 时，设备进入 `control_state=offline`。
- 非空闲状态且无 active session 时，设备显示为 `busy`。

响应：

```json
{
  "ok": true,
  "device_id": 1,
  "device_epoch": 2,
  "server_time": "..."
}
```

### 8.2 Agent 领取命令

```http
POST /api/agent/commands/claim
```

请求：

```json
{
  "machine_id": "demo-online-idle",
  "capacity": 1
}
```

响应：

```json
{
  "ok": true,
  "commands": [
    {
      "command_id": "cmd_xxx",
      "control_session_id": "cs_xxx",
      "seq": 1,
      "action": "enter_team",
      "params": { "team_code": "ABC1234" },
      "expires_at": "..."
    }
  ]
}
```

### 8.3 Agent ACK

```http
POST /api/agent/commands/{command_id}/ack
```

请求：

```json
{
  "machine_id": "demo-online-idle",
  "status": "succeeded",
  "message": "进入观看"
}
```

常见 `status`：

- `succeeded`
- `failed`
- `agent_rejected`
- `expired`
- `canceled`

ACK 副作用：

- `watch` 成功会发 `device.ready_for_customer_timer`，商户服务器收到后开始本地计时。
- `ready` 成功会让设备进入 `running`。
- `enter_team/rejoin` 成功会让设备进入 `team_entered`。
- `stop_current/cleanup` 成功会释放 session，设备回到 `idle`。

## 9. 状态机速查

### 9.1 control session status

| 状态 | 说明 |
|---|---|
| `creating` | 预留，当前创建后通常直接 active |
| `active` | 有效控制中 |
| `stale` | 租约过期但未达到失联保护窗口 |
| `expired` | 失联保护触发，系统排 stop_current |
| `released` | 正常释放 |
| `revoked` / `force_taken_over` | 被接管/撤销 |

### 9.2 command status

| 状态 | 说明 |
|---|---|
| `queued` | 已入队 |
| `delivered` | 已被 Agent 领取 |
| `accepted` / `executing` | 预留/执行中 |
| `succeeded` | 执行成功 |
| `failed` | 执行失败 |
| `expired` | 命令 TTL 过期 |
| `canceled` | 被取消 |
| `agent_rejected` | Agent 拒绝 |
| `superseded` | 被替代，预留 |

### 9.3 30 分钟失联保护

`BridgeService.expire_stale_sessions()` 逻辑：

1. active session 超过 `technical_lease_expires_at` 未 renew：变 `stale`，发 `control_session.stale`。
2. stale 超过 `lost_after_seconds`，默认 1800 秒：变 `expired`，发 `control_session.expired`。
3. 系统自动排 `stop_current`，params：

```json
{ "reason": "external_server_lost_30m", "cleanup": true }
```

Agent 执行成功后释放 session，发 `control_session.released`。

## 10. 常见错误码

| HTTP | error | 场景 |
|---:|---|---|
| 400 | `idempotency_required` | 写接口缺少 `X-Idempotency-Key` |
| 400 | `bad_machine_id` / `bad_display_name` | 设备参数不合法 |
| 400 | `bad_bundle_mode` | 命令包模式不支持 |
| 401 | `auth_required` | 缺少或无效 API Key |
| 401 | `bad_timestamp` | 时间戳无效/超窗 |
| 401 | `bad_signature` | body sha 或签名错误 |
| 401 | `nonce_replay` | nonce 重放 |
| 403 | `scope_denied` | scope 不足 |
| 404 | `not_found` | 设备/session/command/event 不存在 |
| 409 | `idempotency_conflict` | 同幂等键不同请求体 |
| 409 | `machine_id_exists` | 设备码重复 |
| 409 | `device_not_available` | 无可用设备 |
| 409 | `device_disabled` | 设备被禁用 |
| 409 | `device_not_accepting_orders` | 设备停止接单且用于 customer_control |
| 409 | `device_offline` | 设备离线 |
| 409 | `state_precondition_failed` | 状态前置条件不满足 |
| 409 | `device_busy` | 设备已有活动 session |
| 409 | `fencing_token_mismatch` | fencing token 不匹配 |
| 409 | `stale_device_epoch` | expected_device_epoch 已过期 |
| 409 | `session_expired` | session 非 active，不能下发普通命令 |
| 409 | `command_status_conflict` | 命令状态不可取消 |

## 11. 商户服务器标准调用链

1. 客户下单前：`GET /capacity` 或本地设备状态判断是否满机。
2. 创建本地订单并扣本地余额。
3. `POST /control-sessions`，`auto_assign=true`。
4. `POST /command-bundles`：`set_loadout -> enter_team -> ready -> watch`。
5. 轮询 `GET /events?cursor=...`。
6. 收到 `device.ready_for_customer_timer`：本地订单进入 running，开始本地计时。
7. 每 60 秒 `POST /renew`。
8. 本地到期/客户停止/管理员停止：`POST /commands action=stop_current`。
9. 收到 `command.succeeded action=stop_current` 或 `control_session.released`：本地订单结束/退款/清理。
10. 商户服务重启后：用本地 `merchant_context_ref` 调 `/control-sessions/by-ref/{ref}`，再用 events cursor 补偿。
