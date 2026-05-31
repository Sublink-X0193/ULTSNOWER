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
- command action 和技术参数。

不能传：

- customer id/name。
- customer balance。
- paid/refund amount。
- purchased minutes。
- order end time。
