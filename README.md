# ULTSNOWER

ULTSNOWER 是一个面向商户自助运营场景的 Web 服务，提供客户账户、余额、充值、订单、后台管理和运营配置能力。

本仓库以 MIT License 开源。详见：

- [`LICENSE`](LICENSE)
- [`CONTRIBUTING.md`](CONTRIBUTING.md)
- [`SECURITY.md`](SECURITY.md)

> 安全提醒：README 中出现的账号、密码和配置值都只用于本地开发示例。生产部署必须通过环境变量替换为强随机值。

## 功能

- 客户注册、登录与会话管理。
- 客户余额、充值卡、充值记录管理。
- 自助订单创建、当前订单与历史订单查询。
- 商户后台登录、客户管理、订单管理、卡密管理、公告与维护模式配置。
- 管理员角色、审计日志、备份与恢复。
- 本地 SQLite 数据库存储，适合 MVP、演示和小规模部署。

## 运行

```powershell
python -m pip install -e .
$env:MERCHANT_DB_PATH = "data\merchant.sqlite"
$env:MERCHANT_ADMIN_USERNAME = "admin"
$env:MERCHANT_ADMIN_PASSWORD = "change_me_before_production"
python -m merchant_portal_server
```

默认监听 `127.0.0.1:8020`。

完整环境变量示例见 [`.env.example`](.env.example)。当前程序不自动读取 `.env` 文件；请在 shell、进程管理器或部署平台中设置环境变量。

## 本地开发快速启动

初始化开发数据并启动服务：

```powershell
python tools\seed_dev_merchant.py
python -m merchant_portal_server
```

默认后台：

```text
http://127.0.0.1:8020/merchant-admin/login
admin / change_me_before_production
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
- `POST /api/orders` 创建订单。
- `GET /api/orders/current` 当前订单。
- `GET /api/orders/history` 历史订单。
- `GET /api/public/settings` 客户侧读取维护、公告与隐私状态。
- `POST /api/admin/login` 商户后台登录。
- `GET/PUT /api/admin/settings` 系统设置。
- `GET/POST /api/admin/customers` 客户列表与创建客户。
- `PUT /api/admin/customers/{id}/balance` 修改客户余额。
- `GET /api/admin/orders` 订单管理。
- `POST /api/admin/orders/{id}/add-time` 修改订单剩余时长。

## 初始化测试卡密

当前 MVP 没暴露后台制卡 UI。开发/测试可直接调用服务对象或写库：`recharge_cards.code_hash` 为卡密大写去空格后的 SHA-256。正式后台可在下一阶段接入。

## 商户后台默认账号

首次启动时，如果 `merchant_admins` 为空，会按环境变量创建一个 owner 管理员：

```text
MERCHANT_ADMIN_USERNAME=admin
MERCHANT_ADMIN_PASSWORD=change_me_before_production
MERCHANT_INTERNAL_WORKER_TOKEN=change-me-long-random-token
```

生产部署前请务必改掉默认密码和内部调用 token。

## 发布到 GitHub

```powershell
git status
git add .
git commit -m "Prepare open-source release"
git branch -M main
git remote add origin https://github.com/<your-org-or-user>/ULTSNOWER.git
git push -u origin main
```

如果远端仓库已存在，请先确认 `git remote -v`，再按实际仓库地址设置 `origin`。
