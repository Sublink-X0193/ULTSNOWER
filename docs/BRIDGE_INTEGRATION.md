# Merchant -> Central Bridge Integration

## HMAC

每个 Merchant API 请求都带：

```text
X-External-Key
X-External-Timestamp
X-External-Nonce
X-External-Body-SHA256
X-External-Signature
X-Idempotency-Key   # 写接口必填
```

当前 `SNOWSERVER` 精简中央服务端默认路径前缀为 `/api/external/v1`，
本仓库 `BridgeClient` 默认使用 `X-External-*`。若联调旧独立 Bridge，
可设置：

```text
BRIDGE_API_PREFIX=/api/merchant/v1
BRIDGE_AUTH_HEADER_PREFIX=Merchant
```

签名原文：

```text
METHOD
PATH
QUERY_STRING
TIMESTAMP
NONCE
BODY_SHA256
```

本仓库实现见 `merchant_portal_server.bridge_client.BridgeClient`。

## 调用顺序

1. `GET /api/external/v1/capacity`
2. `POST /api/external/v1/control-sessions`
3. `POST /api/external/v1/control-sessions/{id}/command-bundles`
4. `GET /api/external/v1/events?cursor=...`
5. 收到 `device.ready_for_customer_timer`，或 `agent_job.done` 表示最后的 `watch` 命令完成后，本地开始计时。
6. 本地到期：`POST /api/external/v1/control-sessions/{id}/commands` with `action=stop_current`。
7. 每 60 秒：`POST /api/external/v1/control-sessions/{id}/renew`。
8. 重启恢复：`GET /state` + events cursor 补偿。

当前精简中央服务端对应路径为 `/api/external/v1/...`。事件兼容策略：

- `agent_job.done`
  - 根据同一 `command_id` 的 `command.queued.payload.action` 还原动作。
  - 如果完成的是启动命令包最后的 `watch` 命令，则等价于旧
    `device.ready_for_customer_timer`，本地开始计时。
  - 如果完成的是 `stop_current`，本地订单进入 `finished`。
- `agent_job.failed`
  - 非 `stop_current` 且订单尚未运行时，订单失败并退回剩余权益。
- `agent_job.requeued`
  - 只记录事件，不改变本地订单状态。
- `control_session.interrupted`
  - 视为中央/管理员维护中断，本地订单进入 `interrupted_by_admin`，
    并补偿剩余权益。
- `admin.device_maintenance`
  - 作为维护审计事件处理；具体订单状态以同批
    `control_session.interrupted` 为准。
- `device.created` / `device.updated` / `device.enabled` /
  `device.disabled` / `device.deleted`
  - 商户端会写入本地 `bridge_events` 去重流水；设备列表以实时
    `GET /api/external/v1/devices` 为准，不在本地维护影子设备表。

写接口幂等/冲突处理：

- 所有 POST/PUT/PATCH/DELETE 都必须带稳定的 `X-Idempotency-Key`。
- `idempotency_in_progress`：客户端用同一幂等键短暂退避重试。
- `idempotency_conflict`：不自动换 body 重试，提示刷新状态后重新发起。
- `device_has_active_external_session`：不在商户端强制覆盖，提示先释放会话；
  如确需维护，由中央控制台 `force=true` 中断并同步事件。
- `device_has_active_control_session`：外部设备写接口遇到活动控制会话时的
  409；处理策略同上。

## 设备管理写接口

商户后台设备管理现在对接精简中央服务端新增接口：

```text
POST   /api/external/v1/devices
PUT    /api/external/v1/devices/{device_id}
POST   /api/external/v1/devices/{device_id}/enable
POST   /api/external/v1/devices/{device_id}/disable
DELETE /api/external/v1/devices/{device_id}
```

字段映射：

- 新增：`machine_id`、`display_name`、`mode`、`enabled`、
  `accept_orders`、`radar_url`、`watchdog_card`。
- 编辑：只传变化字段；`accept_orders` 通过
  `PUT /api/external/v1/devices/{device_id}` 更新。
- 启停：商户端使用 `/enable` / `/disable`，不再使用旧
  `/toggle` 路径。
- 删除：`DELETE /devices/{id}`。默认不带 `force`，避免商户后台误中断
  活动订单；如确需强制维护，走中央控制台。

## 传给中央的数据

可以传：

- `merchant_context_ref`：HMAC 后的 opaque 引用。
- `team_code`：必须是 `^[A-Z]{3}\d{4}$`，即 3 位大写字母 + 4 位数字，例如 `ABC1234`；商户侧先校验，避免把无效队伍码打到中央。
- `quality/loadout_id`
- `selection_policy`：商户下单时的设备选择策略，只包含设备筛选条件，例如：
  - `order_quality`
  - `privacy_mode`
  - `privacy_skip_balance_w`
  - `min_device_coin_balance`
- command action 和技术参数。
- `watch` command 参数里的 `ace_enabled` / `ace_window_seconds`：这是商户订单策略下发给 Agent 执行观察，不是中央后台配置。

不能传：

- customer id/name。
- customer balance。
- paid/refund amount。
- purchased minutes。
- order end time。

## 商户配置边界

以下配置属于商户服务器，不属于中央控制台：

- `privacy_skip_balance`
  - 商户后台配置。
  - 下单创建 control session 时由商户服务器转换为 `selection_policy.min_device_coin_balance`。
  - 中央只按设备遥测/余额执行筛选；不保存客户余额，也不拥有这个配置。

- `ace_enabled`
  - 商户后台配置。
  - 下单下发 command bundle 时放到 `watch` 命令参数。
  - 中央/Agent 只负责上报和执行观察；是否启用由商户订单策略决定。

- `night_time_check`
  - 商户后台配置。
  - 包夜卡使用/充值时由商户服务器本地校验。
  - 不需要传给中央 Bridge。

## 当前接口文档

旧独立 Bridge 的完整接口表、请求/响应字段、事件、状态机见：[docs/CENTRAL_BRIDGE_API.md](CENTRAL_BRIDGE_API.md)。
当前精简中央服务端以运行中的 `/docs` OpenAPI 为准。

中央 Bridge 与原版中央操作台的接口差异/缺口见：[docs/CENTRAL_BRIDGE_GAP_ANALYSIS.md](CENTRAL_BRIDGE_GAP_ANALYSIS.md)。

单开新会话继续补齐中央操作台能力时，直接使用：[`docs/GOAL_PROMPT_CENTRAL_BRIDGE_LEGACY_CONSOLE.md`](GOAL_PROMPT_CENTRAL_BRIDGE_LEGACY_CONSOLE.md)。
