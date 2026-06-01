# Merchant -> Central Bridge Integration

## HMAC

每个 Merchant API 请求都带：

```text
X-Merchant-Key
X-Merchant-Timestamp
X-Merchant-Nonce
X-Merchant-Body-SHA256
X-Merchant-Signature
X-Idempotency-Key   # 写接口必填
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

1. `GET /api/merchant/v1/capacity`
2. `POST /api/merchant/v1/control-sessions`
3. `POST /api/merchant/v1/control-sessions/{id}/command-bundles`
4. `GET /api/merchant/v1/events?cursor=...`
5. 收到 `device.ready_for_customer_timer` 后本地开始计时。
6. 本地到期：`POST /api/merchant/v1/control-sessions/{id}/commands` with `action=stop_current`。
7. 每 60 秒：`POST /api/merchant/v1/control-sessions/{id}/renew`。
8. 重启恢复：`GET /state` + events cursor 补偿。

## 传给中央的数据

可以传：

- `merchant_context_ref`：HMAC 后的 opaque 引用。
- `team_code`
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

完整接口表、请求/响应字段、事件、状态机见：[docs/CENTRAL_BRIDGE_API.md](CENTRAL_BRIDGE_API.md)。

中央 Bridge 与原版中央操作台的接口差异/缺口见：[docs/CENTRAL_BRIDGE_GAP_ANALYSIS.md](CENTRAL_BRIDGE_GAP_ANALYSIS.md)。

单开新会话继续补齐中央操作台能力时，直接使用：[`docs/GOAL_PROMPT_CENTRAL_BRIDGE_LEGACY_CONSOLE.md`](GOAL_PROMPT_CENTRAL_BRIDGE_LEGACY_CONSOLE.md)。
