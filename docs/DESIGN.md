# 独立商户服务器设计

本仓库是从 `C:\Users\WS\Documents\SNOWSERVER` 原单体拆出来的商户客户服务器。拆分原则：

```text
商户服务器：客户业务系统
中央 Bridge：设备控制系统
```

## 商户服务器职责

- 客户注册、登录、session cookie。
- 商户管理员登录、后台配置页。
- 客户余额：分钟/局数。
- 本地充值卡和充值记录。
- 本地订单、购买时长、退款/补偿。
- 对接中央 Bridge：HMAC、nonce、Idempotency-Key。
- 拉取/处理中央 events。
- 后台 worker：event polling、session renew、order expire、recovery。

## 中央 Bridge 职责

- 设备、Agent 心跳、命令领取和 ACK。
- control session、fencing token、command queue。
- 事件日志。
- 外部商户服务器失联 30 分钟后的 `stop_current + cleanup` 保护。

中央不得保存客户明文、余额、订单金额、购买时长、本地订单结束时间。

## 状态机

本地订单状态：

```text
created -> paid -> claiming_device -> device_claimed -> commanding -> waiting_ready_timer
waiting_ready_timer -> running -> stopping -> finished
waiting_ready_timer/commanding -> failed/refunded
running -> interrupted_by_admin / interrupted_by_disconnect
```

关键约束：

1. 创建 `control_session` 不开始计时。
2. 下发 bundle 不开始计时。
3. 只有处理到 `device.ready_for_customer_timer` 才写入 `started_at/end_at` 并进入 `running`。
4. 到期只进入 `stopping` 并下发 `stop_current`；收到 stop 成功事件后进入 `finished`。
5. 事件按 `event_id` 去重，按 `device_epoch` 过滤旧设备状态。

## 数据表

- `customers`
- `sessions`
- `merchant_admins`
- `admin_sessions`
- `merchant_settings`
- `local_orders`
- `order_control_bindings`
- `bridge_events`
- `recharge_cards`
- `recharge_records`
- `refund_records`
- `idempotency_keys`
- `app_state`

SQLite 使用 WAL，写路径使用 `BEGIN IMMEDIATE`，并通过 partial unique index 保证同一客户同一时间最多一个 live order。

## 并发策略

- 同一客户重复点击：返回现有 active order，不重复扣费。
- 写接口 `X-Idempotency-Key`：同键同 body 重放返回同响应；同键不同 body 返回冲突。
- 创建中央 session：`claim:{local_order_no}`。
- 下发启动 bundle：`bundle:start:{local_order_no}:v1`。
- 停止命令：`stop:{local_order_no}:v1`。
- 中央抢设备失败后本地订单失败并返还已扣分钟。

## 商户后台配置

后台入口：

```text
/merchant-admin/login
/merchant-admin
```

首次启动会用环境变量创建默认管理员：

```text
MERCHANT_ADMIN_USERNAME=admin
MERCHANT_ADMIN_PASSWORD=admin123456
```

支持配置：

- `privacy_mode_enabled`：隐私模式。客户侧订单响应会隐藏队伍码、移除 `fencing_token` 和 `merchant_context_ref`，并可遮罩 control session id。
- `maintenance_mode_enabled`：维护模式。已存在订单可继续展示/处理，新下单会返回 `maintenance_mode`。
- `announcement_enabled` / `announcement_text`：公告开关和内容。客户首页展示，`GET /api/public/settings` 返回给前端。

后台管理界面按原服务端的 `topbar + nav-tabs + data-table + badge` 风格重建，包含：

- 今日总览：客户总数、在线客户、活动订单、运行中订单、完成/异常订单、总剩余分钟。
- 目前在线客户预览：基于未过期 `sessions` 展示在线客户、当前订单、订单剩余时长。
- 所有客户预览：搜索、创建客户、冻结/解冻、重置密码、修改客户分钟/局数余额。
- 订单管理：按状态/关键字筛选订单，显示购买时长、剩余时长、设备/session，支持后台加减订单时长与停止订单。
- 系统设置：公告、隐私模式、维护模式。
