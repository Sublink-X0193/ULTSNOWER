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

结果：23 个测试全部通过（最新复验见第 7.9 节）。

覆盖新增场景：

- token 续期、过期后仅靠活动订单显示在线。
- 登录/下单统计落库。
- 首启 `/setup` 配置向导与管理员密码保护。
- 管理员手动下单、换队、设备 stop 指令。
- 日维度订单分析输出。
- 管理员角色管理、operator 只读权限、最后一个 active owner 保护。

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

### 7.6 管理员权限模型补齐

新增后台“管理员”页面与接口：

- `GET /api/admin/admins`
- `POST /api/admin/admins`
- `PUT /api/admin/admins/{id}/role`
- `PUT /api/admin/admins/{id}/status`
- `PUT /api/admin/admins/{id}/password`
- `DELETE /api/admin/admins/{id}`

角色定义：

- `owner`：完整管理权限，可修改系统设置、客户余额/密码/状态、生成/删除卡密、设备直控、手动下单、备份恢复、管理员管理。
- `operator`：只读运营权限，可查看概览、订单、客户、设备、审计、分析报表，不能做状态变更。

保护规则：

- 禁止降级、禁用或删除最后一个 `active owner`。
- 重置密码、禁用、删除管理员时会清理该管理员后台 session。
- 管理员创建、角色变更、状态变更、密码重置、删除全部写入审计日志。

### 7.7 Owner-only 变更面收口

以下后台状态变更接口已经加 owner 权限：

- 系统设置、公告、装备配置。
- 客户创建、余额调整、冻结/解冻、改密码、删除。
- 充值卡生成/删除。
- 订单加减时、订单停止。
- 设备直控、手动下单、换队、备份恢复、Bridge API Key 配置。

### 7.8 在线与统计口径确认

当前在线口径已按要求固定为：

1. 客户登录 token 未过期；或
2. 客户有活动订单。

登录 token 不做高频心跳，只在客户访问需要认证的接口时低频滑动续期；超时后不再显示 token 在线，但活动订单仍会让客户显示在线。统计信息通过 `customer_activity_events` 持久化，能查询今日登录未下单客户、登录当时订单状态、下单客户数、下单小时数与排行。

### 7.9 最新验证

已执行：

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：23 passed。

## 8. 2026-06-01 测试期首次配置调整

- `MERCHANT_REQUIRE_BRIDGE_SETUP` 默认改为 `0`，测试/联调阶段不再强制全站跳转 `/setup`。
- 正式上线需要强制首启 API Key 时，设置：

```text
MERCHANT_REQUIRE_BRIDGE_SETUP=1
```

- `/setup` 页面保留，可手动打开。
- 首次配置页已合并“全局设置”与 Bridge API Key：
  - 前台名称显示
  - 默认机密局数/小时
  - 绝密局数/小时
  - 包夜时间限制
  - 隐私模式与跳过余额
  - ACE/白嫖检测
  - 维护模式与维护文案
  - 公告内容
  - 全局雷达/备注地址
  - 绝密最大配装价值、是否允许自定义配装
  - 中央 Bridge 地址 / Merchant Key / Merchant Secret
- 测试期 API Key 可留空，只保存全局设置；正式强制模式下仍要求填完整 Bridge Key/Secret。

最新验证：

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：24 passed。

## 9. 2026-06-01 管理员手动下单旧版 1:1 对齐

- 手动下单弹窗按旧版恢复控件和文案：
  - 组队码。
  - 混合模式选择：按机密下单 / 按绝密下单。
  - 时长（小时）最大 `9999`。
  - 时长（分钟）。
  - 限制局数。
  - 限制亏币（单位：万）。
  - 绝密配装区域。
  - 大红包默认配装 / 自定义配装。
  - 头部、护甲、胸挂、手枪、背包装备。
  - 配装总价展示。
- 保留旧版函数名与控件 ID，减少旧代码/操作习惯断裂：
  - `openManualOrderModal`
  - `closeManualOrderModal`
  - `autoCalculateRounds`
  - `toggleLoadoutCustom`
  - `calculateLoadoutCost`
- 后端兼容旧版 payload：
  - 非混合模式不传 `selected_mode` 时，根据设备模式自动推断机密/绝密。
  - 手动下单时长上限同步放宽到旧版控件范围。

最新验证：

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：26 passed。

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

## 9. 2026-06-01 上线安全与运维备份补强

### 9.1 管理端来源校验

- 管理端、setup、商户后台表单的 `POST/PUT/PATCH/DELETE` 增加 Origin/Referer 同源校验。
- 无 Origin 的非浏览器本地脚本仍可调用；带恶意 Origin 的浏览器跨站请求会被拒绝。
- 拒绝响应：`403 bad_origin`。

### 9.2 数据库备份与恢复

后台新增“备份恢复”页面，接口兼容旧版运维习惯：

- `GET /api/admin/backup`：列出当前数据库和历史备份。
- `POST /api/admin/backup`：立即创建 SQLite 在线备份。
- `GET /api/admin/backup/{name}`：下载备份文件。
- `POST /api/admin/backup/{name}/restore`：恢复备份。

安全策略：

- 备份/恢复需要管理员登录；创建/恢复要求 owner 权限。
- 恢复前自动创建 `merchant_pre_restore_*.sqlite` 备份。
- 备份文件名使用 `Path.name` 收敛，拒绝路径穿越。
- 备份创建审计先写入数据库，再执行 SQLite backup，使备份文件自身包含创建审计记录。
