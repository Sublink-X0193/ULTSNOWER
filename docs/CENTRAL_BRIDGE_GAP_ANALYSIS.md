# Central Bridge 与原版中央操作台接口缺口清单

> 对比基准：`C:\Users\WS\Documents\SNOWSERVER\snow_mock_server\app\main.py` 的原版中央/超管/Agent 相关接口。  
> 当前实现：`SNOW_DEVICE_CONTROL_BRIDGE` FastAPI Bridge。  
> 更新时间：2026-06-01

## 结论

当前中央 Bridge 已覆盖核心控制链路：设备列表/容量、control session、command、event、Agent heartbeat/claim/ack。  
相比原版中央操作台，缺口主要不是客户账务/订单类接口，而是 **中央超管、Agent 授权、旧 Agent 兼容、坐标库、屏幕墙、更新分发、全局运维监控**。

## 已覆盖或已有等价能力

| 原版能力 | 当前等价 |
|---|---|
| 商户设备 CRUD、模式切换、启用禁用 | `/api/merchant/v1/devices*` |
| 停止/恢复设备接单 | `/api/merchant/v1/devices/{device_id}/accept-orders`，原版没有独立接口，是拆分后新增 |
| 自动分配可用设备 | `/api/merchant/v1/capacity` + `POST /api/merchant/v1/control-sessions` |
| 下发进队/准备/观察/停止/重启/更新/回收日志等控制动作 | `POST /api/merchant/v1/control-sessions/{session_id}/commands` 或 `command-bundles` |
| Agent 取命令和 ACK | `/api/agent/commands/claim`、`/api/agent/commands/{command_id}/ack` |
| 老板 ID / HFB / 地图 / 状态等运行态展示 | 当前可通过 `/api/agent/heartbeat` 的 runtime 字段透传保存；但 OCR 图片上传识别接口未迁移 |

## 中央 Bridge 当前缺少的接口组

### 1. 中央超管控制台与登录/权限

当前 Bridge 没有原版 `/superadmin` 页面，也没有中央管理员登录态、中央管理员角色/权限、中央审计日志。

原版相关接口：

```text
GET  /superadmin
GET  /api/admin/system-stats
POST /api/admin/maintenance/run
POST /api/admin/ydocr_initial
```

### 2. 商户/租户管理与 Merchant API-Key 管理

Bridge 目前有 `merchant_api_keys` 表和 `create_api_key()` 内部方法，但没有 HTTP 管理接口；也没有原版超管创建/停用商户、编辑商户资料、权限、进入商户后台等接口。

原版相关接口：

```text
GET    /api/admin/tenants
POST   /api/admin/tenants
GET    /api/admin/tenants/{tid}
DELETE /api/admin/tenants/{tid}
POST   /api/admin/tenants/{tid}/note
GET    /api/admin/tenants/{tid}/permissions
POST   /api/admin/tenants/{tid}/permissions
POST   /api/admin/tenants/{tid}/profile
POST   /api/admin/tenants/{tid}/enter
POST   /api/admin/tenants/{tid}/recharge-config
GET    /api/admin/tenants/{tid}/recharge-config
POST   /api/admin/tenants/{tid}/global-radar-permission
```

拆分后建议中央新增而不是放商户侧：

```text
GET    /api/central/v1/tenants
POST   /api/central/v1/tenants
PUT    /api/central/v1/tenants/{tenant_id}
POST   /api/central/v1/tenants/{tenant_id}/disable
GET    /api/central/v1/tenants/{tenant_id}/api-keys
POST   /api/central/v1/tenants/{tenant_id}/api-keys
POST   /api/central/v1/api-keys/{key_id}/rotate
POST   /api/central/v1/api-keys/{key_id}/disable
```

### 3. Agent 授权码/激活码体系

当前 `/api/agent/*` 是 demo 接口，没有原版 Agent 授权码生命周期、机器绑定、到期、预分配商户、禁用/启用/解绑。

原版相关接口：

```text
GET  /api/admin/agent-licenses
POST /api/admin/agent-licenses/generate
POST /api/admin/agent-licenses/{code}/add-time
POST /api/admin/agent-licenses/{code}/disable
POST /api/admin/agent-licenses/{code}/enable
POST /api/admin/agent-licenses/{code}/unbind
POST /api/admin/agent-licenses/{code}/assign
POST /api/activate
POST /api/verify
POST /api/unbind
POST /api/comm_machine_id/upload
POST /api/comm_machine_id/download
```

### 4. Agent/机器签名与设备密钥管理

原版每台设备可查看/重置 `api_secret`，机器请求会验签、防重放。当前 Bridge Merchant API 有 HMAC，但 Agent demo 接口还没有机器级认证。

原版相关接口：

```text
GET  /api/admin/devices/{did}/secret
POST /api/admin/devices/{did}/secret/reset
```

建议中央新增：

```text
GET  /api/central/v1/devices/{device_id}/agent-secret
POST /api/central/v1/devices/{device_id}/agent-secret/rotate
```

并让 `/api/agent/*` 或兼容 `/api/machine/*` 强制机器签名。

### 5. 旧 Agent `/api/machine/*` 兼容层

当前 Bridge 只有新协议：

```text
POST /api/agent/heartbeat
POST /api/agent/commands/claim
POST /api/agent/commands/{command_id}/ack
```

原版旧 Agent 接口没有迁移。如果旧客户端不改，就需要兼容层：

```text
POST /api/machine/heartbeat
GET  /api/machine/task
POST /api/machine/jobs/claim
POST /api/machine/jobs/{job_id}/ack
POST /api/machine/jobs/{job_id}/nack
POST /api/machine/ack_stop
POST /api/machine/ack_rejoin
POST /api/machine/ack_restart
POST /api/machine/ack_update
POST /api/machine/ack_collect_log
POST /api/machine/ack_switch_spectate
POST /api/machine/ack_add_time
POST /api/machine/report_order_finished
POST /api/machine/update_limits
```

说明：当前 command 模型可以承载这些动作，但路由形状和响应字段不兼容原版。

### 6. OCR 图片上传识别与日志上传

当前 Bridge 可以通过 heartbeat 接收已经识别好的 runtime 字段，但没有原版图片上传后由服务端 OCR 的接口，也没有机器日志文件上传保存接口。

原版相关接口：

```text
POST /api/machine/hfb_upload
POST /api/machine/boss_id_upload
POST /api/machine/upload_log
```

### 7. 全局坐标库与客户端坐标下发

当前 Bridge 没有坐标库表、坐标校验、active 坐标切换，也没有客户端加密拉取坐标接口。

原版相关接口：

```text
GET    /api/admin/coordinate-sets
GET    /api/admin/coordinate-sets/{cid:int}
POST   /api/admin/coordinate-sets
POST   /api/admin/coordinate-sets/import-v9
PUT    /api/admin/coordinate-sets/{cid:int}
POST   /api/admin/coordinate-sets/{cid:int}/activate
DELETE /api/admin/coordinate-sets/{cid:int}
POST   /api/coordinates
```

### 8. 全局在线机器搜索与 Agent Job 运维视图

当前 Bridge 只有按商户 HMAC 隔离的设备列表，没有超管跨商户在线机器搜索、Agent job 队列视图。

原版相关接口：

```text
GET /api/admin/online_machines/search
GET /api/admin/agent-jobs
```

### 9. 屏幕墙/远程画面墙

当前 Bridge 没有屏幕墙上传、WebSocket 广播、客户端列表和屏幕墙配置。

原版相关接口：

```text
POST      /api/admin/screen-wall/config
GET       /api/admin/screen-wall/config
POST      /api/upload/{cid}
WEBSOCKET /ws
GET       /api/clients
DELETE    /api/clients/{cid}
```

### 10. 更新分发/下载

当前 Bridge 没有客户端检查更新和下载文件接口。

原版相关接口：

```text
GET /api/check_update
GET /download/{file_name}
```

### 11. 流量封禁/白名单

当前 Bridge 没有原版超管的 IP/地址封禁、白名单、非法访问记录接口。

原版相关接口：

```text
GET  /api/admin/traffic/stats
GET  /api/admin/traffic/blocked
GET  /api/admin/traffic/whitelisted
POST /api/admin/traffic/block
POST /api/admin/traffic/unblock
POST /api/admin/traffic/whitelist
POST /api/admin/traffic/unwhitelist
POST /api/admin/traffic/clear
GET  /api/admin/traffic/illegal/{addr}
```

## 不建议补到中央 Bridge 的原版接口

这些属于商户服务器职责，当前不算中央缺口：

- 客户注册/登录/余额/充值/订单：`/api/register`、`/api/login`、`/api/order*`、`/api/recharge*`。
- 商户后台客户、卡密、订单、全局设置、公告、装备价格：`/api/admin/users*`、`/api/admin/cards*`、`/api/admin/orders*`、`/api/admin/settings`、`/api/admin/notice`、`/api/admin/equipment-config`。
- 隐私模式、维护模式、包夜、默认局数、起装配置、前台名称：商户服务器本地配置，不应迁回中央。

## 建议优先级

1. **P0**：Agent 授权/机器签名、Agent license、API-Key 管理。
2. **P1**：旧 Agent `/api/machine/*` 兼容层、坐标库 `/api/coordinates`、日志上传。
3. **P2**：超管租户管理、在线机器搜索、Agent jobs 运维视图。
4. **P3**：屏幕墙、更新分发、流量封禁。
