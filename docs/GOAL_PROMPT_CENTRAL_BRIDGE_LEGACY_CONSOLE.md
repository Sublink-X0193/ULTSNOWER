# /goal：中央 Bridge 补齐原版中央操作台能力

> 用途：新开一个 Codex 会话时，直接把本文作为目标文档/提示词使用。  
> 主工作目录：`C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE`  
> 商户服务器目录：`C:\Users\WS\Documents\ULTSNOWSER`  
> 原版二合一服务器目录：`C:\Users\WS\Documents\SNOWSERVER`  
> 更新时间：2026-06-01

## 一句话目标

在 **中央 Bridge** 里补齐原版二合一服务器中属于“中央操作台 / Agent / 设备基础设施”的接口能力；能从原版复用的代码直接复用/拆出模块，不要重新设计；**屏幕墙不做**；客户、订单、余额、充值卡、全局商户设置继续留在商户服务器，不回迁中央。

## 已有背景文档

请先阅读：

```text
C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE\docs\CENTRAL_BRIDGE_API.md
C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE\docs\CENTRAL_BRIDGE_GAP_ANALYSIS.md
C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE\docs\DESIGN.md
C:\Users\WS\Documents\ULTSNOWSER\docs\BRIDGE_INTEGRATION.md
```

原版接口和可复用实现主要在：

```text
C:\Users\WS\Documents\SNOWSERVER\snow_mock_server\app\main.py
C:\Users\WS\Documents\SNOWSERVER\snow_mock_server\app\db.py
C:\Users\WS\Documents\SNOWSERVER\snow_mock_server\app\crypto_helper.py
```

当前 Bridge 主文件：

```text
C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE\src\device_control_bridge\app.py
C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE\src\device_control_bridge\service.py
C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE\src\device_control_bridge\db.py
```

## 硬约束

1. **屏幕墙不做**。不要实现下面这些：

   ```text
   POST      /api/admin/screen-wall/config
   GET       /api/admin/screen-wall/config
   POST      /api/upload/{cid}
   WEBSOCKET /ws
   GET       /api/clients
   DELETE    /api/clients/{cid}
   ```

2. **不要把商户业务迁回中央**。以下继续由商户服务器负责：
   - 客户注册/登录/token
   - 客户余额、分钟、局数、退款
   - 客户订单、手动下单、订单到期
   - 商户充值卡/卡密
   - 隐私模式、维护模式、公告、默认局数、起装配置、前台名称
   - 商户后台客户管理、订单管理、卡密管理

3. 中央 Bridge 只负责：
   - 租户/商户基础资料
   - Merchant API-Key
   - Agent 授权码/激活/绑定
   - 设备基础信息、设备密钥、机器签名
   - control session / command / event
   - 旧 Agent `/api/machine/*` 兼容
   - 坐标库 `/api/coordinates`
   - OCR 上传、日志上传
   - 更新分发
   - 中央超管运维、在线机器搜索、Agent job 查询、traffic 封禁

4. 旧接口能 1:1 兼容就 1:1 兼容，尤其是 Agent 客户端接口，优先保证旧 Agent 不需要改。

5. 不要用浏览器控件做常规验证；优先用 `pytest`、`curl`、`TestClient`、PowerShell。

## 当前 Bridge 已有能力

当前已有并保留：

```text
GET    /health
GET    /api/merchant/v1/devices
POST   /api/merchant/v1/devices
PUT    /api/merchant/v1/devices/{device_id}
PUT    /api/merchant/v1/devices/{device_id}/mode
PUT    /api/merchant/v1/devices/{device_id}/toggle
PUT    /api/merchant/v1/devices/{device_id}/accept-orders
DELETE /api/merchant/v1/devices/{device_id}
GET    /api/merchant/v1/capacity
GET    /api/merchant/v1/events
GET    /api/merchant/v1/events/{event_id}
GET    /api/merchant/v1/control-sessions
GET    /api/merchant/v1/control-sessions/by-ref/{merchant_context_ref}
POST   /api/merchant/v1/control-sessions
POST   /api/merchant/v1/control-sessions/{session_id}/renew
GET    /api/merchant/v1/control-sessions/{session_id}/state
POST   /api/merchant/v1/control-sessions/{session_id}/release
POST   /api/merchant/v1/control-sessions/{session_id}/commands
POST   /api/merchant/v1/control-sessions/{session_id}/command-bundles
POST   /api/merchant/v1/control-sessions/{session_id}/commands/{command_id}/cancel
GET    /api/merchant/v1/control-sessions/{session_id}/bundles/{bundle_id}
GET    /api/merchant/v1/commands/{command_id}
GET    /api/merchant/v1/devices/{device_id}/capabilities
POST   /api/merchant/v1/webhook/test
POST   /api/agent/heartbeat
POST   /api/agent/commands/claim
POST   /api/agent/commands/{command_id}/ack
```

不要破坏这些接口和现有测试。

## 需要补齐的接口组

### P0-1：中央管理员、租户、Merchant API-Key

新增中央管理接口，路径可以使用新命名空间，避免和商户后台混淆：

```text
POST /api/central/v1/admin/login
POST /api/central/v1/admin/logout
GET  /api/central/v1/admin/me

GET    /api/central/v1/tenants
POST   /api/central/v1/tenants
GET    /api/central/v1/tenants/{tenant_id}
PUT    /api/central/v1/tenants/{tenant_id}
POST   /api/central/v1/tenants/{tenant_id}/enable
POST   /api/central/v1/tenants/{tenant_id}/disable

GET  /api/central/v1/tenants/{tenant_id}/api-keys
POST /api/central/v1/tenants/{tenant_id}/api-keys
POST /api/central/v1/api-keys/{key_id}/rotate
POST /api/central/v1/api-keys/{key_id}/enable
POST /api/central/v1/api-keys/{key_id}/disable
```

兼容原版超管租户能力时，可参考原版：

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
POST   /api/admin/tenants/{tid}/global-radar-permission
```

注意：`/api/admin/tenants/{tid}/recharge-config` 是商户支付/充值配置，拆分后不优先迁到中央。

### P0-2：Agent 授权码/激活码体系

迁移原版 license key 表和逻辑，兼容：

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

复用原版：

- `license_keys` 表结构
- `_license_duration_minutes`
- `_license_start_if_needed`
- `active_license_for`
- `CryptoHelper.decrypt/encrypt`
- 机器码绑定与预分配商户校验

### P0-3：机器签名与设备密钥

原版每台机器有 `api_secret`，机器请求验签、防重放。Bridge 需要迁移。

新增/兼容：

```text
GET  /api/central/v1/devices/{device_id}/agent-secret
POST /api/central/v1/devices/{device_id}/agent-secret/rotate
```

或兼容原路径：

```text
GET  /api/admin/devices/{did}/secret
POST /api/admin/devices/{did}/secret/reset
```

然后让这些接口支持机器级签名：

```text
/api/agent/*
/api/machine/*
/api/coordinates
/api/activate
/api/verify
```

### P1-1：旧 Agent `/api/machine/*` 兼容层

必须兼容旧 Agent：

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

实现策略：

- 不另造订单系统。
- 尽量把旧 task / command 映射到当前 Bridge 的 `commands`。
- 旧 heartbeat 写入 `devices.runtime_json`，继续透传：
  - `work_status`
  - `boss_id` / `spectate_boss`
  - `harvard` / `hfb_value`
  - `round_count`
  - `current_map`
  - `script_ver`
  - `sub_state`
  - `prison_stage`
  - `prison_point`
- 旧 ACK 产生当前 Bridge event：`command.succeeded` / `command.failed`。

### P1-2：坐标库和 `/api/coordinates`

迁移原版坐标库：

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

复用原版：

- `default_coordinates`
- `COORDINATE_REQUIRED_PATHS`
- `_validate_coordinates`
- `_coordinate_diff`
- `_active_coordinates`
- `_effective_coordinates_for_device`

要求：

- 客户端 `/api/coordinates` 返回 active 坐标。
- 支持原版加密 payload。
- 保留 V9 required path 校验。

### P2-1：OCR 上传和日志上传

迁移：

```text
POST /api/machine/hfb_upload
POST /api/machine/boss_id_upload
POST /api/machine/upload_log
```

实现要求：

- 先复用原版文件大小、Content-Type、magic bytes 校验。
- OCR 依赖如果可用，直接复用原版 `recognize_hfb_image` / `recognize_boss_id_image`。
- 如果依赖暂不可用，先实现接口、鉴权、文件保存、runtime 字段写入占位，但测试要明确标注 OCR provider fallback。
- `boss_id_upload` 成功后写入设备 runtime 的 `boss_id` / `spectate_boss`。
- `hfb_upload` 成功后写入 `harvard` / `hfb_value` / `currency_balance`。

### P2-2：中央在线机器搜索、Agent jobs、系统运维

迁移：

```text
GET /api/admin/online_machines/search
GET /api/admin/agent-jobs
GET /api/admin/system-stats
POST /api/admin/maintenance/run
POST /api/admin/ydocr_initial
```

说明：

- `/api/admin/online_machines/search` 应跨租户查询，只给中央超管。
- `/api/admin/agent-jobs` 用于查看旧 Agent job 兼容队列。
- `system-stats` 给中央操作台总览用。

### P2-3：更新分发

迁移：

```text
GET /api/check_update
GET /download/{file_name}
```

复用原版 `app_updates` 表/默认响应。

### P2-4：traffic 封禁/白名单

迁移：

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

复用原版：

- `_traffic_list`
- `_traffic_save`
- `_traffic_denied`

但注意不要误伤 Merchant HMAC 内部请求。

## 不做清单

明确不做：

```text
POST      /api/admin/screen-wall/config
GET       /api/admin/screen-wall/config
POST      /api/upload/{cid}
WEBSOCKET /ws
GET       /api/clients
DELETE    /api/clients/{cid}
```

暂不迁移到中央：

```text
/api/register
/api/login
/api/order*
/api/recharge*
/api/admin/users*
/api/admin/cards*
/api/admin/orders*
/api/admin/settings
/api/admin/notice
/api/admin/equipment-config
```

## 建议模块拆分

不要继续把所有东西塞进 `app.py`。建议拆成：

```text
src/device_control_bridge/admin_auth.py
src/device_control_bridge/central_admin_routes.py
src/device_control_bridge/tenant_routes.py
src/device_control_bridge/license_routes.py
src/device_control_bridge/agent_auth.py
src/device_control_bridge/agent_compat_routes.py
src/device_control_bridge/coordinate_routes.py
src/device_control_bridge/ocr_routes.py
src/device_control_bridge/update_routes.py
src/device_control_bridge/traffic_routes.py
src/device_control_bridge/legacy_crypto.py
```

`create_app()` 中注册各 route module。

## 数据库迁移建议

需要新增/迁移表：

```text
central_admins
central_admin_sessions
tenants 或扩展现有 tenant 概念
license_keys
machine_auth_nonces
agent_jobs
machine_commands
coordinate_sets
app_updates
traffic kv 或独立 traffic_rules / traffic_illegal_logs
uploaded_logs
```

现有表继续保留：

```text
merchant_api_keys
devices
control_sessions
commands
events
idempotency_keys
merchant_api_nonces
```

## 验收测试

必须补 pytest。至少覆盖：

1. Merchant API 现有 27 个接口不回归。
2. 中央超管登录成功/失败。
3. 创建 tenant 后能生成 Merchant API-Key。
4. disable API-Key 后 Merchant HMAC 请求被拒绝。
5. Agent license generate / activate / verify / unbind。
6. Agent license 绑定机器后，其他机器不能复用。
7. 机器签名正确通过，错误签名/重放 nonce 拒绝。
8. `/api/machine/heartbeat` 写入 runtime。
9. `/api/machine/task` 能领取当前 Bridge command。
10. `/api/machine/jobs/claim` / ack / nack 状态正确。
11. ack 后产生 `command.succeeded` 或 `command.failed` event。
12. 坐标库导入 V9、创建、激活、`/api/coordinates` 返回 active 坐标。
13. hfb/boss OCR 上传接口能鉴权、限流/限大小、写 runtime。
14. `/api/check_update` 返回默认版本或表内版本。
15. traffic block 后普通请求被拦，whitelist 可绕过。
16. 屏幕墙接口确认不存在或 404。

运行命令：

```powershell
cd C:\Users\WS\Documents\SNOW_DEVICE_CONTROL_BRIDGE
python -m compileall -q src
python -m pytest -q
```

同时商户服务器回归：

```powershell
cd C:\Users\WS\Documents\ULTSNOWSER
python -m compileall -q src
python -m pytest -q
```

## 完成标准

- 中央 Bridge 所有新增接口有测试。
- 原有 Bridge 接口和商户服务器测试不回归。
- `docs/CENTRAL_BRIDGE_API.md` 更新新增接口。
- `docs/CENTRAL_BRIDGE_GAP_ANALYSIS.md` 标记已完成/未做/故意不做。
- 每个阶段小提交，提交信息清楚。

建议提交顺序：

```text
1. Add central admin tenant api key management
2. Add agent license activation and machine auth
3. Add legacy machine API compatibility
4. Add coordinate set management
5. Add machine OCR upload and logs
6. Add central ops update traffic APIs
7. Update bridge API docs and gap status
```
