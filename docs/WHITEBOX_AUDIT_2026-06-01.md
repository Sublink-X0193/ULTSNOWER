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
