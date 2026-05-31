# 商户服务器当前进度与上线优化记录

更新时间：2026-06-01

## 1. 已完成的拆分边界

- 商户服务器负责：客户账户、余额、充值卡、订单、购买时长、退款、客户/商户后台 UI。
- 中央 Bridge 负责：设备、Agent、control session、fencing token、command queue、event。
- 客户订单创建后只进入 `waiting_ready_timer`，只有收到中央事件 `device.ready_for_customer_timer` 后才写入 `started_at/end_at` 并开始本地计时。
- 订单到期由商户服务器主动下发 `stop_current`；中央仍保留技术租约和失联保护。

## 2. 本轮上线方向优化

### 2.1 在线统计改为低压力模型

不再使用高频心跳统计客户在线，避免 100-200 客户同时在线时给服务端制造无意义写压力。

在线定义：

1. 客户登录账户的 `merchant_session` token 未过期；或
2. 客户存在活动订单：`created/paid/claiming_device/device_claimed/commanding/waiting_ready_timer/running/stopping/refunding`。

实现要点：

- `sessions.last_seen_at` 只在认证请求触发且达到刷新阈值时更新。
- 过期 session 在读取在线列表、创建 session、认证 session 时顺手清理。
- 后台在线客户列表返回 `online_reason`：`token` / `order` / `token+order`。

### 2.2 客户 token 自动续期与超时

- 客户 session 使用滑动续期。
- 临近过期时自动延长 `expires_at`。
- 长时间未请求不会续期，到期后在线状态消失。
- 不额外增加心跳接口压力。

### 2.3 登录/下单统计落库

新增 `customer_activity_events`：

- 登录事件：记录客户、登录时间、登录当时订单状态。
- 下单事件：记录订单、模式、购买分钟、购买局数。
- 今日统计可回答：
  - 今天多少客户点击登录。
  - 其中多少客户登录后没下单。
  - 这些客户登录当时是什么订单状态。
  - 多少客户下单、下了多少小时。

后台新增 `今日登录 / 下单漏斗` 面板。

### 2.4 订单分析界面

后台新增 `订单分析`：

- 可选：日 / 周 / 月。
- 汇总：订单数、下单老板数、下单小时、完成小时、异常/失败单。
- 每日柱状报表。
- 状态分布、模式分布。
- 下单排行 TOP20。

接口：

- `GET /api/admin/order-analytics?period=day|week|month&date=YYYY-MM-DD`

### 2.5 首次启动 Bridge API Key 配置

新增首次配置向导：

- 当真实 BridgeClient 仍使用默认 `mk_test/secret` 且本地未保存 bridge 配置时，访问商户后台登录会跳转 `/setup`。
- `/setup` 要求输入本地管理员密码，避免未授权访客绑定自己的 API Key。
- 保存项写入本地数据库 `merchant_settings`：
  - `bridge_base_url`
  - `bridge_merchant_key`
  - `bridge_merchant_secret`
  - `bridge_configured`
- Secret 不在界面回显，只显示是否已设置。

接口：

- `GET /api/setup/status`
- `POST /api/setup/bridge`

### 2.6 设备直控与管理员手动下单

后台新增 `设备直控`：

- 设备列表来自中央 Bridge `GET /api/merchant/v1/devices`；不可用时降级到 capacity 的 idle device 列表。
- 空闲设备支持管理员手动下单。
- 运行中的本地订单支持：停止、换队、ready/watch 等维护指令。
- 所有直控仍通过 control session + fencing token + command queue，不直接绕过中央。

接口：

- `GET /api/admin/devices`
- `POST /api/admin/manual-order`
- `POST /api/admin/manual-rejoin/{order_id}`
- `POST /api/admin/devices/{device_id}/command`

## 3. 并发与调度策略

- SQLite 开启 WAL、busy timeout，写操作关键路径使用 `BEGIN IMMEDIATE`。
- 活动订单唯一约束：一个客户同一时间只能有一个活动订单，避免重复扣费。
- 手动订单使用 `admin_manual_device_{device_id}` 伪客户，避免不同设备的管理员订单互相阻塞，同时同一设备不能重复手动下单。
- 设备分配仍由中央 Bridge 按 control session 原子 claim，商户侧并发失败会进入失败/退款路径，不重复扣余额。
- 100-200 在线客户下，在线统计只查 indexed session/order，不做轮询写入。

## 4. 权限与审计

- 客户接口必须有客户 session。
- 商户后台接口必须有 admin session。
- 首次 Bridge 配置即使未登录也必须提供本地管理员密码。
- 新增 `admin_audit_logs`，记录 Bridge 配置更新、手动下单、设备指令、管理员换队等敏感动作。

## 5. 当前测试结果

已执行：

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：20 个测试全部通过。

覆盖新增场景：

- token 续期、过期后仅靠活动订单显示在线。
- 登录/下单统计落库。
- 首启 `/setup` 配置向导与管理员密码保护。
- 管理员手动下单、换队、设备 stop 指令。
- 日维度订单分析输出。

## 6. 后续上线前建议

- 生产环境必须替换默认管理员密码和 `MERCHANT_REF_SECRET`。
- 建议用反向代理启用 HTTPS，并给 `/merchant-admin`、`/setup` 做访问源限制或二次认证。
- 如果要支持“强制接管其他系统/中央超管 session”，应在中央 Bridge 暴露专门 audited takeover API；商户端不要伪造 admin_force 覆盖旧 session。
- 如 200+ 并发写入频繁，建议从 SQLite 平滑迁移 PostgreSQL；当前 100-200 在线统计与普通下单规模可用 WAL 支撑。

## 7. 2026-06-01 继续加固补充

### 7.1 首启配置入口扩大到全站

- 首启未配置 Bridge API Key 时，不只后台登录页，访问 `/`、客户登录/API 登录等业务入口也会被拦截。
- HTML GET 跳转 `/setup`；API/POST 返回 `428 setup_required`。
- 放行范围仅：`/setup`、`/api/setup/*`、`/health`、静态资源和 favicon。

### 7.2 安全响应头与登录限流

- 全站响应增加：
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: same-origin`
  - `Permissions-Policy` 禁用相机/麦克风/地理定位。
- 登录、注册、setup 保存等敏感 POST 增加轻量内存限流，防止上线后被撞库/刷接口。

### 7.3 设备手动下单并发锁

- `local_orders` 增加 `manual_device_id`。
- 增加唯一索引 `idx_one_live_manual_order_per_device`，保证同一设备同一时间只能有一个活动手动订单。
- 手动订单在 Bridge claim 前就写入 `manual_device_id`，堵住并发双击/多管理员同时手动下单造成的重复占用窗口。

### 7.4 权限与审计可视化

- Bridge 配置、设备直控、管理员手动下单、管理员换队要求 `owner` 权限。
- 后台新增“审计日志”页面，可查看敏感动作、资源、操作者和 metadata。
- 新增接口：`GET /api/admin/audit-logs`。

### 7.5 设备直控按钮补强

设备直控页在活动订单设备上补充旧版常见维护动作：

- 停止
- 准备
- 观战
- 切观战
- 重启备用
- 清理
- 换队

这些动作仍然只对商户本地持有的 active control session 生效，不跨 session 强制接管。

## 8. 2026-06-01 旧版直控/手动下单兼容补强

### 8.1 手动下单弹窗向旧版靠齐

后台手动下单弹窗补回旧版关键控件：

- 组队码格式校验：前三位大写字母 + 后四位数字。
- 混合模式选择：按机密 / 按绝密下单。
- 时长小时/分钟。
- 限制局数。
- 限制亏币（单位 W）。
- 绝密模式配装：默认配装 / 自定义配装。
- 自定义配装项：头部、护甲、胸挂、手枪、背包。
- 配装总价与最大配装价值校验。

### 8.2 旧版接口别名补齐

新增旧版兼容接口：

- `GET /api/admin/orders/{order_id}/detail`
- `POST /api/admin/add-time/{order_id}`
- `POST /api/admin/devices/{device_id}/restart_backup`
- `POST /api/admin/machines/{device_key}/restart`
- `POST /api/admin/machines/{device_key}/update`
- `POST /api/admin/machines/{device_key}/collect_log`

### 8.3 手动订单执行参数进入 Bridge command

- `max_rounds` / `max_coin_loss` 会进入 `watch` 命令参数。
- 配装信息会进入 `set_loadout` 命令参数。
- 本地订单新增 `order_options_json` 保存管理员手动下单选项，便于审计与复盘。

### 8.4 空闲设备维护命令

- 空闲设备也可以下发维护命令：异常重启、更新脚本、回收日志、重启备用、清理。
- 实现方式：商户端向中央 Bridge 创建 `admin_device_maintenance` control session，再下发维护 command。
- 不跨会话强制接管，不绕过中央 fencing/command queue。
