# 白盒自查审计记录

审计时间：2026-06-01

## 本轮代码变更面

- 数据库：session last_seen、activity events、admin audit logs、订单/绑定索引。
- Service：低压力在线判定、token 滑动续期、登录/下单事件、订单分析、设备直控、手动订单、Bridge 配置。
- App/API：setup 向导、统计接口、订单分析接口、设备直控接口、后台 UI。
- BridgeClient：补齐 list_devices、扩展 create_control_session 参数。
- Tests：新增 token/统计、setup、设备直控/手动下单/分析覆盖。

## 安全检查

- [x] 管理后台接口全部依赖 `current_admin`。
- [x] 首次 `/setup` 保存 API Key 要求本地管理员密码。
- [x] Bridge Secret 不通过状态接口回显。
- [x] 设备指令 action 白名单限制，不允许任意字符串直通。
- [x] 手动下单仍走 Bridge fencing token 与 command queue。
- [x] 客户侧 privacy mode 仍会遮罩敏感 binding 字段。
- [x] 管理员敏感操作写入 `admin_audit_logs`。

## 并发/一致性检查

- [x] 创建订单、手动订单使用 `BEGIN IMMEDIATE`。
- [x] 客户活动订单唯一索引保留，重复点击复用/拒绝。
- [x] 同设备手动订单通过伪客户和设备活动订单检查避免重复。
- [x] Bridge 设备 claim 失败不会扣客户余额；客户订单原逻辑继续失败退款。
- [x] 在线统计不引入新心跳，不增加周期写压力。

## 功能检查

- [x] 客户 token 未过期显示在线。
- [x] token 过期但活动订单存在仍显示在线。
- [x] 今日登录未下单统计保存登录当时订单状态。
- [x] 日/周/月订单分析有汇总、每日柱状、状态/模式分布、排行。
- [x] 后台设备直控有设备列表、手动下单、停止、换队。
- [x] 首启 API Key 配置页面存在。

## 已执行验证

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：20 passed。

## 风险与处理意见

1. **中央 Bridge 当前未暴露独立 force_takeover HTTP 路由**  
   商户端没有伪造强制接管；只对本地持有的 active control session 下发指令。若要跨 session 强接管，应先补中央 audited takeover API。

2. **SQLite 写并发上限**  
   当前 100-200 在线统计压力可控，因为不做心跳写入；若未来订单写入和后台操作显著增加，建议迁移 PostgreSQL。

3. **生产配置**  
   需要部署前替换默认管理员密码、Bridge API Secret、MERCHANT_REF_SECRET，并启用 HTTPS。

## 2026-06-01 追加白盒自查

### 本次追加变更

- 增加 setup 全站拦截 middleware。
- 增加安全响应头与敏感 POST 限流。
- 增加 `manual_device_id` 和同设备活动手动订单唯一索引。
- 增加 owner 权限检查。
- 增加审计日志查询接口与后台页面。
- 增加设备直控维护按钮：切观战、重启备用、清理。

### 追加审计结论

- [x] 首启未配置时，`/` 会跳转 `/setup`，API 登录返回 `428 setup_required`。
- [x] `/api/setup/bridge` 仍要求本地管理员密码，并进一步校验 owner 角色。
- [x] 同设备并发手动下单被数据库唯一索引和业务检查双重保护。
- [x] 直控 action 仍在后端白名单内，前端新增按钮不会绕过白名单。
- [x] 敏感动作可从后台“审计日志”查看。
- [x] 登录/注册/setup POST 有轻量限流；不会影响已有测试场景。

### 追加验证

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：21 passed。

### 追加热修复审计

- 发现旧数据库启动时，`manual_device_id` 索引如果在迁移前创建，会因旧表缺列导致启动失败。
- 修复：将 `manual_device_id` 相关索引从初始 schema 脚本移入 `_migrate()`，保证先补列再建索引。
- 复验：`python -m compileall -q src tests` 与 `python -m pytest -q` 均通过，21 passed。

## 2026-06-01 旧版直控兼容追加审计

### 追加检查项

- [x] 手动下单弹窗恢复旧版组队码、混合模式、限制局数、限制亏币、绝密配装控件。
- [x] 手动下单 payload 中的 `max_rounds`、`max_coin_loss`、`loadout_*` 不丢失，写入 `order_options_json`。
- [x] `max_coin_loss` 进入 Bridge `watch` 命令参数；配装进入 `set_loadout` 命令参数。
- [x] 补齐旧版接口别名，减少旧前端/脚本调用断裂风险。
- [x] 空闲设备维护命令通过独立 `admin_device_maintenance` control session 下发，仍保留中央 fencing/command queue 边界。

### 验证

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：21 passed。

## 2026-06-01 上线安全/备份追加审计

### 追加检查项

- [x] 管理端状态变更请求带恶意 Origin 会被 `403 bad_origin` 拒绝。
- [x] 同源 Origin 正常允许，不影响后台正常 fetch。
- [x] 备份列表、创建、下载、恢复接口存在。
- [x] 备份创建/恢复要求 owner 权限。
- [x] 恢复前自动创建 pre_restore 备份。
- [x] 备份文件下载使用安全文件名解析，避免路径穿越。
- [x] 备份创建与恢复均进入审计日志。

### 验证

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：22 passed。

## 2026-06-01 管理员权限追加审计

### 追加变更

- 新增商户管理员管理 Service/API/UI。
- 新增 `owner` / `operator` 分权：operator 保留运营只读；owner 才能执行配置、客户余额、卡密、设备直控、手动下单、订单加减时、备份恢复、管理员管理。
- 禁止降级、禁用、删除最后一个 active owner。
- 重置密码、禁用、删除管理员会清理其后台 session。
- 管理员创建、角色修改、状态修改、密码重置、删除全部写入 `admin_audit_logs`。

### 白盒检查

- [x] `/api/admin/admins*` 写接口全部依赖 `current_admin + require_owner_admin`。
- [x] 系统设置、公告、装备配置、客户/卡密/订单状态变更均补充 owner 权限。
- [x] operator 可访问只读运营数据，无法提交状态变更。
- [x] 最后一个 active owner 保护在事务内检查，避免误锁死后台。
- [x] session 清理覆盖密码重置、禁用、删除，旧 cookie 立即失效。
- [x] UI 新增“管理员”页和创建/改密/角色/状态/删除控件；非 owner 仅显示只读提示。

### 验证

```text
python -m compileall -q src tests
python -m pytest -q
```

结果：23 passed。
