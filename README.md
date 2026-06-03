# SNOW Merchant Portal Server

独立商户客户服务器 MVP。它从原 `SNOWSERVER` 单体中拆出客户、余额、充值、订单、计时和退款职责；设备、Agent、control session、command、event 仍由中央 `SNOW_DEVICE_CONTROL_BRIDGE` 负责。

## 边界

- 商户服务器保存：客户、余额、充值卡、充值记录、本地订单、购买时长、退款/补偿记录、订单到中央 control session 的绑定。
- 中央 Bridge 保存：设备、control session、command queue、event log、30 分钟失联保护。
- 浏览器只访问本服务；中央 API key 只存在本服务后端配置里。
- 本地订单 **只在** `device.ready_for_customer_timer` 事件到达后开始计时。
- 本地订单到期后，本服务主动向中央下发 `stop_current`。

## 运行

```powershell
python -m pip install -e .
$env:MERCHANT_DB_PATH = "data\merchant.sqlite"
$env:BRIDGE_BASE_URL = "http://127.0.0.1:8010"
$env:BRIDGE_MERCHANT_KEY = "mk_test"
$env:BRIDGE_MERCHANT_SECRET = "secret"
$env:BRIDGE_API_PREFIX = "/api/external/v1"
$env:BRIDGE_AUTH_HEADER_PREFIX = "External"
$env:MERCHANT_ADMIN_USERNAME = "admin"
$env:MERCHANT_ADMIN_PASSWORD = "admin123456"
python -m merchant_portal_server
```

默认监听 `127.0.0.1:8020`。

> 对接当前 `SNOWSERVER` 精简中央服务端时使用默认 `/api/external/v1` +
> `X-External-*`。若仍联调旧独立 `SNOW_DEVICE_CONTROL_BRIDGE`，可改为
> `BRIDGE_API_PREFIX=/api/merchant/v1`、`BRIDGE_AUTH_HEADER_PREFIX=Merchant`。

## 本地联调快速启动

如果中央 Bridge 没有现成 API key/设备，可先启动带测试数据的 Bridge：

```powershell
python tools\run_seeded_bridge.py
```

另开一个终端初始化商户测试数据并启动商户服务器：

```powershell
python tools\seed_dev_merchant.py
python -m merchant_portal_server
```

默认后台：

```text
http://127.0.0.1:8020/merchant-admin/login
admin / admin123456
```

测试充值卡：

```text
TEST-60
TEST-180
TEST-600
```

## 测试

```powershell
python -m pytest -q
```

## 主要接口

- `POST /api/register` 注册客户。
- `POST /api/login` 登录，设置 HttpOnly session cookie。
- `POST /api/recharge/redeem` 卡密充值。
- `GET /api/capacity` 从中央 Bridge 读取容量。
- `POST /api/orders` 本地下单、扣余额、创建中央 control session 并下发启动 command bundle。
- `GET /api/orders/current` 当前订单。
- `GET /api/orders/history` 历史订单。
- `POST /internal/workers/events` 拉取并处理中央 events。
- `POST /internal/workers/order-expire` 本地到期订单主动 stop。
- `POST /internal/workers/session-renew` 续约 control session。
- `POST /internal/workers/recover` 重启恢复：查 session state + events cursor 补偿。
  - `/internal/*` 默认只允许本机调用；跨主机调度需设置 `MERCHANT_INTERNAL_WORKER_TOKEN` 并请求头携带 `X-Internal-Token`。
- `/merchant-admin/login` 商户管理员登录。
- `/merchant-admin` 商户后台配置页：
  - 隐私模式：客户侧隐藏队伍码和 control session 细节，不暴露 fencing token。
  - 维护模式：禁止新下单。
  - 公告：展示到客户首页并通过 public settings API 返回。
- `/merchant-admin` 现在是完整商户后台：
  - 今日总览：客户数、在线客户、活动订单、运行中订单、剩余分钟池。
  - 在线客户预览：当前 session 未过期的客户、当前订单、剩余订单时长。
  - 所有客户预览：创建客户、搜索客户、冻结/解冻、改密码、调分钟/局数余额。
  - 订单管理：订单列表、剩余时长显示、订单加减时、后台停止订单。
  - 系统设置：公告、隐私模式、维护模式。
  - 设备管理：对接中央 `/api/external/v1/devices` 新增/编辑/启用/停用/删除接口；活动会话存在时默认不强制覆盖。
- `GET /api/public/settings` 客户侧读取维护/公告/隐私状态。
- `POST /api/admin/login`、`GET/PUT /api/admin/settings` 商户后台 JSON API。
- `GET /api/admin/customers?online_only=true` 在线客户。
- `GET/POST /api/admin/customers` 全量客户与新建客户。
- `PUT /api/admin/customers/{id}/balance` 修改客户剩余分钟/局数。
- `GET /api/admin/orders` 订单管理。
- `POST /api/admin/orders/{id}/add-time` 修改运行订单剩余时长。

## 初始化测试卡密

当前 MVP 没暴露后台制卡 UI。开发/测试可直接调用服务对象或写库：`recharge_cards.code_hash` 为卡密大写去空格后的 SHA-256。正式后台可在下一阶段接入。

## 商户后台默认账号

首次启动时，如果 `merchant_admins` 为空，会按环境变量创建一个 owner 管理员：

```text
MERCHANT_ADMIN_USERNAME=admin
MERCHANT_ADMIN_PASSWORD=admin123456
# 可选：允许非本机 cron/调度器调用 /internal/workers/*
MERCHANT_INTERNAL_WORKER_TOKEN=change-me-long-random-token
```

生产部署前请务必改掉默认密码。
