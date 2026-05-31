from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .bridge_client import BridgeClient, BridgeClientError
from .config import Settings, load_settings
from .db import Database
from .service import MerchantError, MerchantService


def json_ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def json_fail(code: str, message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code, "message": message}, status_code=status_code)


def create_app(*, db_path: str | Path | None = None, bridge_client: Any | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(db_path or settings.db_path)
    bridge = bridge_client or BridgeClient(settings.bridge_base_url, settings.bridge_merchant_key, settings.bridge_merchant_secret)
    service = MerchantService(db, bridge, merchant_ref_secret=settings.merchant_ref_secret, session_ttl_seconds=settings.session_ttl_seconds)
    service.ensure_default_admin(settings.default_admin_username, settings.default_admin_password)

    async def background_loop() -> None:
        while True:
            await asyncio.to_thread(service.poll_events_once)
            await asyncio.to_thread(service.expire_orders_once)
            await asyncio.to_thread(service.renew_sessions_once)
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.enable_background_workers:
            app.state.worker_task = asyncio.create_task(background_loop())
        try:
            yield
        finally:
            task = app.state.worker_task
            if task:
                task.cancel()
            if hasattr(bridge, "close"):
                bridge.close()

    app = FastAPI(title="SNOW Merchant Portal Server", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan)
    app.state.db = db
    app.state.bridge = bridge
    app.state.service = service
    app.state.worker_task = None

    @app.exception_handler(MerchantError)
    async def merchant_error_handler(_request: Request, exc: MerchantError) -> JSONResponse:
        return json_fail(exc.code, exc.message, exc.status_code)

    @app.exception_handler(BridgeClientError)
    async def bridge_error_handler(_request: Request, exc: BridgeClientError) -> JSONResponse:
        return json_fail(exc.code, exc.message, exc.status_code)

    def current_customer(request: Request) -> dict[str, Any]:
        customer = service.customer_from_session(request.cookies.get("merchant_session"))
        if not customer:
            raise MerchantError("login_required", "请先登录", 401)
        return customer

    def maybe_customer(request: Request) -> dict[str, Any] | None:
        return service.customer_from_session(request.cookies.get("merchant_session"))

    def set_session_cookie(resp: Response, sid: str) -> None:
        resp.set_cookie("merchant_session", sid, httponly=True, samesite="lax", max_age=settings.session_ttl_seconds)

    def clear_session_cookie(resp: Response) -> None:
        resp.delete_cookie("merchant_session")

    def current_admin(request: Request) -> dict[str, Any]:
        admin = service.admin_from_session(request.cookies.get("merchant_admin_session"))
        if not admin:
            raise MerchantError("admin_login_required", "请先登录商户后台", 401)
        return admin

    def maybe_admin(request: Request) -> dict[str, Any] | None:
        return service.admin_from_session(request.cookies.get("merchant_admin_session"))

    def set_admin_cookie(resp: Response, sid: str) -> None:
        resp.set_cookie("merchant_admin_session", sid, httponly=True, samesite="lax", max_age=settings.session_ttl_seconds)

    def clear_admin_cookie(resp: Response) -> None:
        resp.delete_cookie("merchant_admin_session")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return json_ok(service="merchant_portal", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        customer = maybe_customer(request)
        if not customer:
            return HTMLResponse(_layout("欢迎", '<p>请 <a href="/login">登录</a> 或 <a href="/register">注册</a></p>'))
        merchant_settings = service.get_settings()
        current = service.public_order(service.current_order(customer["id"]), privacy_mode=bool(merchant_settings.get("privacy_mode_enabled")))
        try:
            capacity = service.bridge.get_capacity()
        except Exception as e:  # UI should keep rendering even when bridge is down
            capacity = {"available": False, "capacity_label": "unknown", "error": str(e)}
        banner = ""
        if merchant_settings.get("announcement_enabled") and merchant_settings.get("announcement_text"):
            banner += f"<div style='background:#eef6ff;border:1px solid #9ec5fe;padding:10px;border-radius:8px'>公告：{_escape(merchant_settings.get('announcement_text'))}</div>"
        if merchant_settings.get("maintenance_mode_enabled"):
            banner += "<div style='background:#fff3cd;border:1px solid #ffda6a;padding:10px;border-radius:8px;margin-top:8px'>维护模式已开启：暂时不能新下单。</div>"
        return HTMLResponse(_customer_dashboard_html(customer, current, capacity, merchant_settings, banner))

    @app.get("/register", response_class=HTMLResponse)
    def register_page() -> HTMLResponse:
        return HTMLResponse(_layout("注册", _form("/register", [("username", "用户名"), ("password", "密码", "password")], "注册")))

    @app.post("/register")
    def register_form(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        customer = service.register_customer(username, password)
        sid = service.create_session(customer["id"])
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(resp, sid)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return HTMLResponse(_layout("登录", _form("/login", [("username", "用户名"), ("password", "密码", "password")], "登录")))

    @app.post("/login")
    def login_form(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        customer = service.authenticate(username, password)
        sid = service.create_session(customer["id"])
        resp = RedirectResponse("/", status_code=303)
        set_session_cookie(resp, sid)
        return resp

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        service.delete_session(request.cookies.get("merchant_session") or "")
        resp = RedirectResponse("/login", status_code=303)
        clear_session_cookie(resp)
        return resp

    @app.get("/recharge", response_class=HTMLResponse)
    def recharge_page(_customer: dict[str, Any] = Depends(current_customer)) -> HTMLResponse:
        return HTMLResponse(_layout("卡密充值", _form("/recharge", [("code", "卡密")], "充值")))

    @app.post("/recharge")
    def recharge_form(code: str = Form(...), customer: dict[str, Any] = Depends(current_customer)) -> RedirectResponse:
        service.redeem_card(customer["id"], code)
        return RedirectResponse("/", status_code=303)

    @app.get("/orders/new", response_class=HTMLResponse)
    def new_order_page(_customer: dict[str, Any] = Depends(current_customer)) -> HTMLResponse:
        merchant_settings = service.get_settings()
        if merchant_settings.get("maintenance_mode_enabled"):
            return HTMLResponse(_layout("下单", "<p>维护模式已开启，暂时不能新下单。</p><p><a href='/'>返回</a></p>"))
        return HTMLResponse(_layout("下单", _form("/orders", [("requested_minutes", "分钟"), ("team_code", "队伍码"), ("quality", "配置/品质")], "下单")))

    @app.post("/orders")
    def order_form(request: Request, requested_minutes: int = Form(...), team_code: str = Form(...), quality: str = Form("standard"), customer: dict[str, Any] = Depends(current_customer)) -> RedirectResponse:
        service.place_order(customer["id"], requested_minutes=requested_minutes, team_code=team_code, quality=quality, idempotency_key=request.headers.get("X-Idempotency-Key"))
        return RedirectResponse("/orders/current", status_code=303)

    @app.get("/orders/current", response_class=HTMLResponse)
    def current_order_page(customer: dict[str, Any] = Depends(current_customer)) -> HTMLResponse:
        return HTMLResponse(_layout("当前订单", f"{_pre(service.public_order(service.current_order(customer['id'])) or '暂无当前订单')}<p><a href='/'>返回</a></p>"))

    @app.get("/orders/history", response_class=HTMLResponse)
    def history_page(customer: dict[str, Any] = Depends(current_customer)) -> HTMLResponse:
        return HTMLResponse(_layout("历史订单", _pre(service.public_orders(service.order_history(customer["id"]))) + "<p><a href='/'>返回</a></p>"))

    @app.get("/merchant-admin/login", response_class=HTMLResponse)
    def admin_login_page() -> HTMLResponse:
        return HTMLResponse(_layout("商户后台登录", _form("/merchant-admin/login", [("username", "管理员"), ("password", "密码", "password")], "登录")))

    @app.post("/merchant-admin/login")
    def admin_login_form(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        admin = service.authenticate_admin(username, password)
        sid = service.create_admin_session(admin["id"])
        resp = RedirectResponse("/merchant-admin", status_code=303)
        set_admin_cookie(resp, sid)
        return resp

    @app.post("/merchant-admin/logout")
    def admin_logout(request: Request) -> RedirectResponse:
        service.delete_admin_session(request.cookies.get("merchant_admin_session") or "")
        resp = RedirectResponse("/merchant-admin/login", status_code=303)
        clear_admin_cookie(resp)
        return resp

    @app.get("/merchant-admin", response_class=HTMLResponse)
    def admin_home(admin: dict[str, Any] = Depends(current_admin)) -> HTMLResponse:
        return HTMLResponse(_admin_dashboard_html(admin))

    @app.post("/merchant-admin/settings")
    def admin_settings_form(
        privacy_mode_enabled: str | None = Form(None),
        maintenance_mode_enabled: str | None = Form(None),
        announcement_enabled: str | None = Form(None),
        announcement_text: str = Form(""),
        admin: dict[str, Any] = Depends(current_admin),
    ) -> RedirectResponse:
        service.update_settings(
            admin["id"],
            {
                "privacy_mode_enabled": privacy_mode_enabled == "1",
                "maintenance_mode_enabled": maintenance_mode_enabled == "1",
                "announcement_enabled": announcement_enabled == "1",
                "announcement_text": announcement_text,
            },
        )
        return RedirectResponse("/merchant-admin", status_code=303)

    # JSON API
    @app.post("/api/register")
    def api_register(response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        customer = service.register_customer(body.get("username") or "", body.get("password") or "")
        sid = service.create_session(customer["id"])
        set_session_cookie(response, sid)
        return json_ok(customer=service.get_customer(customer["id"]))

    @app.post("/api/login")
    def api_login(response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        customer = service.authenticate(body.get("username") or "", body.get("password") or "")
        sid = service.create_session(customer["id"])
        set_session_cookie(response, sid)
        return json_ok(customer=customer)

    @app.post("/api/logout")
    def api_logout(request: Request, response: Response) -> dict[str, Any]:
        service.delete_session(request.cookies.get("merchant_session") or "")
        clear_session_cookie(response)
        return json_ok()

    @app.get("/api/me")
    def api_me(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(customer=service.get_customer(customer["id"]))

    @app.get("/api/public/settings")
    def api_public_settings() -> dict[str, Any]:
        merchant_settings = service.get_settings()
        return json_ok(settings={
            "privacy_mode_enabled": merchant_settings["privacy_mode_enabled"],
            "maintenance_mode_enabled": merchant_settings["maintenance_mode_enabled"],
            "announcement_enabled": merchant_settings["announcement_enabled"],
            "announcement_text": merchant_settings["announcement_text"] if merchant_settings["announcement_enabled"] else "",
        })

    @app.get("/api/capacity")
    def api_capacity(_customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(capacity=service.bridge.get_capacity())

    @app.post("/api/recharge/redeem")
    def api_recharge(body: dict[str, Any] = Body(...), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(**service.redeem_card(customer["id"], body.get("code") or ""))

    @app.post("/api/orders")
    def api_order(request: Request, body: dict[str, Any] = Body(...), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        result = service.place_order(
            customer["id"],
            requested_minutes=int(body.get("requested_minutes") or 0),
            team_code=body.get("team_code") or "",
            quality=body.get("quality") or "standard",
            idempotency_key=request.headers.get("X-Idempotency-Key"),
        )
        result = {**result, "order": service.public_order(result.get("order"))}
        return json_ok(**result)

    @app.get("/api/orders/current")
    def api_current_order(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(order=service.public_order(service.current_order(customer["id"])))

    @app.get("/api/orders/history")
    def api_history(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(orders=service.public_orders(service.order_history(customer["id"])))

    @app.post("/api/admin/login")
    def api_admin_login(response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        admin = service.authenticate_admin(body.get("username") or "", body.get("password") or "")
        sid = service.create_admin_session(admin["id"])
        set_admin_cookie(response, sid)
        return json_ok(admin=admin)

    @app.post("/api/admin/logout")
    def api_admin_logout(request: Request, response: Response) -> dict[str, Any]:
        service.delete_admin_session(request.cookies.get("merchant_admin_session") or "")
        clear_admin_cookie(response)
        return json_ok()

    @app.get("/api/admin/settings")
    def api_admin_get_settings(_admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(settings=service.get_settings())

    @app.put("/api/admin/settings")
    def api_admin_put_settings(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(settings=service.update_settings(admin["id"], body))

    @app.get("/api/admin/overview")
    def api_admin_overview(_admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(**service.admin_overview())

    @app.get("/api/admin/customers")
    def api_admin_customers(keyword: str = "", online_only: bool = False, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        customers = service.admin_list_customers(keyword=keyword, online_only=online_only)
        return json_ok(customers=customers, total=len(customers))

    @app.post("/api/admin/customers")
    def api_admin_customer_create(body: dict[str, Any] = Body(...), _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        customer = service.admin_create_customer(
            username=body.get("username") or "",
            password=body.get("password") or "123456",
            balance_minutes=int(body.get("balance_minutes") or 0),
            balance_rounds=int(body.get("balance_rounds") or 0),
            status=body.get("status") or "active",
        )
        return json_ok(customer=customer)

    @app.put("/api/admin/customers/{customer_id}/balance")
    def api_admin_customer_balance(customer_id: int, body: dict[str, Any] = Body(...), _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        customer = service.admin_update_customer_balance(
            customer_id,
            balance_minutes=body.get("balance_minutes") if "balance_minutes" in body else None,
            balance_rounds=body.get("balance_rounds") if "balance_rounds" in body else None,
            delta_minutes=body.get("delta_minutes") if "delta_minutes" in body else None,
            delta_rounds=body.get("delta_rounds") if "delta_rounds" in body else None,
        )
        return json_ok(customer=customer)

    @app.put("/api/admin/customers/{customer_id}/status")
    def api_admin_customer_status(customer_id: int, body: dict[str, Any] = Body(...), _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(customer=service.admin_set_customer_status(customer_id, body.get("status") or "active"))

    @app.put("/api/admin/customers/{customer_id}/password")
    def api_admin_customer_password(customer_id: int, body: dict[str, Any] = Body(...), _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(**service.admin_reset_customer_password(customer_id, body.get("password") or "123456"))

    @app.delete("/api/admin/customers/{customer_id}")
    def api_admin_customer_delete(customer_id: int, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(**service.admin_delete_customer(customer_id))

    @app.get("/api/admin/orders")
    def api_admin_orders(keyword: str = "", status: str = "", customer_id: int | None = None, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        orders = service.admin_list_orders(keyword=keyword, status=status, customer_id=customer_id)
        return json_ok(orders=orders, total=len(orders))

    @app.get("/api/admin/orders/{order_id}")
    def api_admin_order_detail(order_id: int, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(order=service.admin_get_order(order_id))

    @app.post("/api/admin/orders/{order_id}/add-time")
    def api_admin_order_add_time(order_id: int, body: dict[str, Any] = Body(...), _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(order=service.admin_adjust_order_time(order_id, add_minutes=int(body.get("add_minutes") or 0)))

    @app.post("/api/admin/orders/{order_id}/stop")
    def api_admin_order_stop(order_id: int, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(order=service.admin_stop_order(order_id))

    @app.post("/internal/workers/events")
    def run_events() -> dict[str, Any]:
        return json_ok(**service.poll_events_once())

    @app.post("/internal/workers/order-expire")
    def run_expire() -> dict[str, Any]:
        return json_ok(**service.expire_orders_once())

    @app.post("/internal/workers/session-renew")
    def run_renew() -> dict[str, Any]:
        return json_ok(**service.renew_sessions_once())

    @app.post("/internal/workers/recover")
    def run_recover() -> dict[str, Any]:
        return json_ok(**service.recover_sessions_once())

    return app


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
    <style>
    *{{box-sizing:border-box}} body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#f0f2f5;color:#1f2937;max-width:1080px;margin:32px auto;padding:0 20px;line-height:1.6}}
    a{{color:#3b82f6;text-decoration:none}} a:hover{{text-decoration:underline}}
    h1{{font-size:22px;margin:0 0 18px}} h2{{font-size:18px}}
    input,textarea,select{{padding:9px 11px;margin:6px 0;border:1px solid #d1d5db;border-radius:8px;background:#fff;font:inherit}}
    button{{padding:8px 14px;margin:6px 4px 6px 0;border:0;border-radius:8px;background:#3b82f6;color:#fff;cursor:pointer;font-weight:600}} button:hover{{background:#2563eb}}
    form{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:18px;box-shadow:0 1px 3px rgba(0,0,0,.04);margin:12px 0}}
    pre{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px;overflow:auto;box-shadow:0 1px 3px rgba(0,0,0,.04)}}
    </style>
    </head><body><h1>{title}</h1>{body}</body></html>"""


def _form(action: str, fields: list[tuple], submit: str) -> str:
    parts = [f'<form method="post" action="{action}">']
    for item in fields:
        name, label = item[0], item[1]
        typ = item[2] if len(item) > 2 else "text"
        parts.append(f'<label>{label}<input name="{name}" type="{typ}"></label><br>')
    parts.append(f'<button type="submit">{submit}</button></form><p><a href="/">返回</a></p>')
    return "".join(parts)


def _escape(value: Any) -> str:
    import html

    return html.escape("" if value is None else str(value), quote=True)


def _pre(value: Any) -> str:
    return "<pre>" + _escape(repr(value) if not isinstance(value, str) else value) + "</pre>"


def _customer_dashboard_html(customer: dict[str, Any], current: dict[str, Any] | None, capacity: dict[str, Any], settings: dict[str, Any], banner: str) -> str:
    template = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SNOW 自助下单 - 客户中心</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; background:linear-gradient(135deg,#f5f7fa 0%,#c3cfe2 100%); color:#1f2937; min-height:100vh; }
    .topbar { background:rgba(255,255,255,.9); backdrop-filter:blur(10px); border-bottom:1px solid rgba(229,231,235,.8); padding:0 24px; height:56px; display:flex; align-items:center; justify-content:space-between; box-shadow:0 2px 8px rgba(0,0,0,.05); position:sticky; top:0; z-index:10; }
    .topbar-logo { font-size:16px; font-weight:800; color:#3b82f6; }
    .topbar-right { display:flex; align-items:center; gap:12px; font-size:13px; }
    .balance-box { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .balance-item { display:flex; align-items:center; gap:4px; padding:4px 10px; border-radius:8px; font-size:12px; font-weight:700; }
    .balance-machine { background:#dbeafe; color:#1d4ed8; }
    .balance-absolute { background:#fce7f3; color:#be185d; }
    .btn-sm { padding:6px 14px; border-radius:7px; border:none; font-size:12px; cursor:pointer; font-weight:700; }
    .btn-primary { background:#3b82f6; color:#fff; } .btn-primary:hover{background:#2563eb}
    .btn-gray { background:#e5e7eb; color:#374151; } .btn-gray:hover{background:#d1d5db}
    .btn-danger { background:#ef4444; color:#fff; } .btn-danger:hover{background:#dc2626}
    .btn-green { background:#22c55e; color:#fff; } .btn-green:hover{background:#16a34a}
    .content { max-width:1200px; margin:0 auto; padding:20px 24px 48px; }
    .nav-tabs { display:flex; gap:8px; background:transparent; border-bottom:none; padding:0 0 16px; overflow:auto; }
    .nav-tab { padding:10px 20px; font-size:14px; cursor:pointer; color:#6b7280; background:#fff; border-radius:10px; border:1px solid #e5e7eb; transition:all .25s; font-weight:600; white-space:nowrap; }
    .nav-tab.active { color:#fff; background:#3b82f6; border-color:#3b82f6; box-shadow:0 8px 20px rgba(59,130,246,.25); }
    .tab-panel { display:none; } .tab-panel.active { display:block; }
    .hero { border-radius:26px 26px 26px 10px; padding:22px; background:radial-gradient(circle at 16% 18%,rgba(255,255,255,.95) 0,rgba(255,255,255,0) 34%),linear-gradient(135deg,#fff 0%,#eef6ff 48%,#e9f0ff 100%); border:1px solid rgba(59,130,246,.18); box-shadow:0 18px 42px rgba(37,99,235,.14); margin-bottom:16px; }
    .hero-row { display:grid; grid-template-columns:1.2fr .8fr; gap:18px; align-items:stretch; }
    .hero h2 { font-size:26px; margin-bottom:8px; color:#0f172a; }
    .hint { color:#6b7280; font-size:13px; line-height:1.7; }
    .cards { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }
    .card { background:#fff; border:1px solid #e5e7eb; border-radius:16px; padding:16px; box-shadow:0 10px 24px rgba(15,23,42,.06); }
    .card-title { color:#6b7280; font-size:12px; margin-bottom:8px; font-weight:700; }
    .card-value { font-size:24px; font-weight:800; color:#111827; }
    .form-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin:14px 0; }
    input,select { width:100%; height:40px; padding:0 12px; border:1px solid #d1d5db; border-radius:10px; background:#fff; font:inherit; }
    table.orders-table { width:100%; border-collapse:collapse; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 10px 24px rgba(15,23,42,.06); }
    .orders-table th,.orders-table td { padding:11px 13px; border-bottom:1px solid #f3f4f6; text-align:left; font-size:13px; }
    .orders-table th { background:#f9fafb; color:#6b7280; font-size:12px; }
    .badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:800; }
    .badge-ok { background:#dcfce7; color:#15803d; } .badge-warn { background:#fef3c7; color:#92400e; } .badge-info { background:#dbeafe; color:#1d4ed8; } .badge-gray { background:#f3f4f6; color:#6b7280; }
    .empty { padding:26px; text-align:center; color:#9ca3af; background:#fff; border:1px dashed #d1d5db; border-radius:14px; }
    .notice { margin-bottom:16px; }
    @media(max-width:900px){ .hero-row,.form-grid,.cards{grid-template-columns:1fr}.topbar{height:auto;padding:12px;align-items:flex-start;gap:10px}.topbar-right{flex-direction:column;align-items:flex-end}.content{padding:14px}.orders-table{display:block;overflow:auto} }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-logo">SNOW 自助下单 · 客户中心</div>
    <div class="topbar-right">
      <div class="balance-box">
        <span>__USERNAME__</span>
        <span class="balance-item balance-machine">剩余 __BALANCE__ 分钟</span>
        <span class="balance-item balance-absolute">局数 __ROUNDS__</span>
      </div>
      <form method="post" action="/logout"><button class="btn-sm btn-gray">退出</button></form>
    </div>
  </div>
  <div class="content">
    <div class="notice">__BANNER__</div>
    <div class="nav-tabs">
      <div class="nav-tab active" data-tab="home">下单中心</div>
      <div class="nav-tab" data-tab="current">当前订单</div>
      <div class="nav-tab" data-tab="recharge">卡密充值</div>
      <div class="nav-tab" data-tab="history">历史订单</div>
    </div>
    <div id="tab-home" class="tab-panel active">
      <div class="hero">
        <div class="hero-row">
          <div>
            <h2>选择套餐，等待设备准备完成后才开始计时</h2>
            <div class="hint">商户服务器只在收到中央 <b>device.ready_for_customer_timer</b> 后开始本地倒计时。维护模式开启时不会允许新下单。</div>
            <div class="form-grid">
              <label><span class="hint">购买分钟</span><input id="orderMinutes" type="number" value="60" min="1"></label>
              <label><span class="hint">队伍码</span><input id="teamCode" placeholder="例如 JYG4545"></label>
              <label><span class="hint">配置品质</span><select id="quality"><option value="standard">机密</option><option value="secret">绝密</option></select></label>
            </div>
            <button class="btn-sm btn-primary" onclick="placeOrder()">立即下单</button>
            <button class="btn-sm btn-gray" onclick="loadAll()">刷新状态</button>
          </div>
          <div class="cards">
            <div class="card"><div class="card-title">中央容量</div><div class="card-value">__CAPACITY__</div><div class="hint">available: __AVAILABLE__</div></div>
            <div class="card"><div class="card-title">当前订单</div><div class="card-value" id="currentStatusCard">-</div><div class="hint" id="currentRemainCard">等待刷新</div></div>
            <div class="card"><div class="card-title">计费边界</div><div class="card-value">Ready</div><div class="hint">不在 claim/bundle 阶段偷跑计时</div></div>
          </div>
        </div>
      </div>
    </div>
    <div id="tab-current" class="tab-panel"><div id="currentBox"></div></div>
    <div id="tab-recharge" class="tab-panel">
      <div class="hero">
        <h2>卡密充值</h2>
        <div class="form-grid" style="grid-template-columns:1fr auto">
          <input id="rechargeCode" placeholder="输入 TEST-60 / TEST-180 / TEST-600">
          <button class="btn-sm btn-green" onclick="redeem()">充值</button>
        </div>
      </div>
    </div>
    <div id="tab-history" class="tab-panel"><div id="historyBox"></div></div>
  </div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, opts={}) {
  const headers = {'Content-Type':'application/json', ...(opts.headers || {})};
  delete opts.headers;
  const res = await fetch(path, {credentials:'same-origin', headers, ...opts});
  const data = await res.json().catch(()=>({ok:false,error:'bad_json',message:'响应解析失败'}));
  if (!res.ok || data.ok === false) throw new Error(data.message || data.error || res.statusText);
  return data;
}
function fmtMin(m){m=Number(m||0);return m>=60?`${Math.floor(m/60)}时${m%60}分`:`${m}分`}
function remain(o){ if(!o||!o.end_at)return 0; return Math.max(0, Math.ceil((new Date(o.end_at)-new Date())/60000)); }
function badge(s){ const cls=s==='running'?'badge-ok':(s==='waiting_ready_timer'||s==='stopping'?'badge-warn':'badge-gray'); return `<span class="badge ${cls}">${esc(s||'-')}</span>`; }
function showTab(name){ document.querySelectorAll('.nav-tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===name)); document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id==='tab-'+name)); if(name==='current')loadCurrent(); if(name==='history')loadHistory(); }
document.querySelectorAll('.nav-tab').forEach(t=>t.addEventListener('click',()=>showTab(t.dataset.tab)));
async function loadCurrent(){
  const d=await api('/api/orders/current'); const o=d.order;
  $('currentStatusCard').textContent=o?o.status:'暂无';
  $('currentRemainCard').textContent=o&&o.end_at?`剩余 ${fmtMin(remain(o))}`:'收到 ready 事件后开始计时';
  $('currentBox').innerHTML = o ? `<table class="orders-table"><tbody>
    <tr><th>订单号</th><td>${esc(o.local_order_no)}</td></tr><tr><th>状态</th><td>${badge(o.status)}</td></tr>
    <tr><th>购买时长</th><td>${fmtMin(o.requested_minutes)}</td></tr><tr><th>剩余</th><td><b>${fmtMin(remain(o))}</b></td></tr>
    <tr><th>队伍码</th><td>${esc(o.team_code || '-')}</td></tr><tr><th>开始/结束</th><td>${esc(o.started_at||'-')} / ${esc(o.end_at||'-')}</td></tr>
  </tbody></table>` : '<div class="empty">暂无当前订单</div>';
}
async function loadHistory(){
  const d=await api('/api/orders/history'); const rows=d.orders||[];
  $('historyBox').innerHTML = rows.length ? `<table class="orders-table"><thead><tr><th>ID</th><th>队伍码</th><th>状态</th><th>购买</th><th>开始</th><th>完成</th></tr></thead><tbody>` + rows.map(o=>`<tr><td>${o.id}</td><td>${esc(o.team_code||'-')}</td><td>${badge(o.status)}</td><td>${fmtMin(o.requested_minutes)}</td><td>${esc(o.started_at||'-')}</td><td>${esc(o.finished_at||'-')}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">暂无历史订单</div>';
}
async function placeOrder(){
  try {
    const body={requested_minutes:Number($('orderMinutes').value||0),team_code:$('teamCode').value,quality:$('quality').value};
    await api('/api/orders',{method:'POST',headers:{'X-Idempotency-Key':'web-'+Date.now()},body:JSON.stringify(body)});
    alert('下单已创建，等待设备 ready 后开始计时'); showTab('current'); await loadAll();
  } catch(e){ alert(e.message); }
}
async function redeem(){
  try { await api('/api/recharge/redeem',{method:'POST',body:JSON.stringify({code:$('rechargeCode').value})}); alert('充值成功，刷新页面查看余额'); location.reload(); } catch(e){ alert(e.message); }
}
async function loadAll(){ await loadCurrent(); await loadHistory(); }
loadAll().catch(()=>{});
setInterval(loadCurrent, 15000);
</script>
</body>
</html>"""
    return (
        template.replace("__USERNAME__", _escape(customer.get("username")))
        .replace("__BALANCE__", _escape(customer.get("balance_minutes")))
        .replace("__ROUNDS__", _escape(customer.get("balance_rounds")))
        .replace("__CAPACITY__", _escape(capacity.get("capacity_label")))
        .replace("__AVAILABLE__", _escape(capacity.get("available")))
        .replace("__BANNER__", banner or "")
    )


def _admin_dashboard_html(admin: dict[str, Any]) -> str:
    template = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SNOW 商户后台</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; background: #f0f2f5; color: #1f2937; min-height: 100vh; }
    .topbar { background: #fff; border-bottom: 1px solid #e5e7eb; padding: 0 24px; height: 56px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 3px rgba(0,0,0,.04); position: sticky; top: 0; z-index: 10; }
    .topbar-logo { font-size: 16px; font-weight: 800; color: #6366f1; letter-spacing: .2px; }
    .topbar-right { display:flex; align-items:center; gap:10px; color:#6b7280; font-size:13px; }
    .nav-tabs { display:flex; gap:0; background:#fff; border-bottom:1px solid #e5e7eb; padding:0 24px; overflow:auto; position: sticky; top: 56px; z-index: 9; }
    .nav-tab { padding:12px 20px; font-size:14px; cursor:pointer; color:#6b7280; border-bottom:2px solid transparent; transition: all .2s; white-space:nowrap; }
    .nav-tab.active { color:#6366f1; border-bottom-color:#6366f1; font-weight:600; }
    .nav-tab:hover { color:#6366f1; }
    .content { max-width:1600px; margin:0 auto; padding:20px 24px 48px; }
    .tab-panel { display:none; }
    .tab-panel.active { display:block; }
    .section-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; gap:12px; flex-wrap:wrap; }
    .section-title { font-size:16px; font-weight:700; }
    .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
    .toolbar input,.toolbar select { height:32px; border:1px solid #d1d5db; border-radius:8px; padding:0 10px; background:#fff; }
    .btn-sm { padding:6px 11px; border-radius:7px; border:none; font-size:12px; cursor:pointer; font-weight:600; }
    .btn-primary { background:#3b82f6; color:#fff; } .btn-primary:hover{background:#2563eb}
    .btn-gray { background:#e5e7eb; color:#374151; } .btn-gray:hover{background:#d1d5db}
    .btn-danger { background:#ef4444; color:#fff; } .btn-danger:hover{background:#dc2626}
    .btn-green { background:#22c55e; color:#fff; } .btn-green:hover{background:#16a34a}
    .btn-purple { background:#7c3aed; color:#fff; } .btn-purple:hover{background:#6d28d9}
    .btn-amber { background:#f59e0b; color:#fff; } .btn-amber:hover{background:#d97706}
    .grid { display:grid; gap:14px; }
    .stats-grid { grid-template-columns: repeat(6, minmax(150px, 1fr)); }
    .stat-card { background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
    .stat-label { font-size:12px; color:#6b7280; margin-bottom:8px; }
    .stat-value { font-size:26px; font-weight:800; color:#111827; }
    .panel { background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
    table.data-table { width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.04); }
    .data-table th,.data-table td { padding:10px 12px; text-align:left; font-size:13px; border-bottom:1px solid #f3f4f6; vertical-align:middle; }
    .data-table th { background:#f9fafb; color:#6b7280; font-weight:600; font-size:12px; }
    .data-table tbody tr:hover { background:#f8fafc; }
    .badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; font-weight:700; }
    .badge-online { background:#dcfce7; color:#15803d; }
    .badge-offline { background:#f3f4f6; color:#6b7280; }
    .badge-running { background:#dbeafe; color:#1d4ed8; }
    .badge-waiting { background:#fef3c7; color:#92400e; }
    .badge-failed { background:#fee2e2; color:#b91c1c; }
    .badge-done { background:#ede9fe; color:#6d28d9; }
    .empty-state { padding:32px; text-align:center; color:#9ca3af; background:#fff; border:1px dashed #d1d5db; border-radius:12px; }
    .hint { color:#6b7280; font-size:12px; line-height:1.7; }
    .settings-row { display:grid; grid-template-columns: 220px 1fr; gap:12px; padding:14px 0; border-bottom:1px solid #f3f4f6; align-items:flex-start; }
    .settings-row:last-child { border-bottom:0; }
    textarea { width:100%; min-height:120px; border:1px solid #d1d5db; border-radius:10px; padding:12px; font:inherit; resize:vertical; }
    .switch-line { display:flex; align-items:center; gap:8px; font-weight:600; }
    @media (max-width: 980px) { .stats-grid { grid-template-columns: repeat(2, minmax(0,1fr)); } .content{padding:14px} .data-table{display:block;overflow:auto} .settings-row{grid-template-columns:1fr} }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-logo">SNOW 商户服务器 · 管理后台</div>
    <div class="topbar-right">
      <span>__ADMIN__ / __ROLE__</span>
      <button class="btn-sm btn-gray" onclick="location.href='/'">客户首页</button>
      <form method="post" action="/merchant-admin/logout" style="display:inline"><button class="btn-sm btn-danger">退出</button></form>
    </div>
  </div>
  <div class="nav-tabs">
    <div class="nav-tab active" data-tab="overview">今日总览</div>
    <div class="nav-tab" data-tab="online">在线客户</div>
    <div class="nav-tab" data-tab="customers">客户管理</div>
    <div class="nav-tab" data-tab="orders">订单管理</div>
    <div class="nav-tab" data-tab="settings">系统设置</div>
  </div>
  <div class="content">
    <div id="tab-overview" class="tab-panel active">
      <div class="section-header"><span class="section-title">运营概览</span><button class="btn-sm btn-primary" onclick="loadAll()">刷新</button></div>
      <div id="overviewCards" class="grid stats-grid"></div>
      <div class="grid" style="grid-template-columns: 1fr 1fr; margin-top:14px">
        <div class="panel"><div class="section-header"><span class="section-title">当前在线客户</span><button class="btn-sm btn-gray" onclick="showTab('online')">查看全部</button></div><div id="overviewOnline"></div></div>
        <div class="panel"><div class="section-header"><span class="section-title">进行中订单</span><button class="btn-sm btn-gray" onclick="showTab('orders')">订单管理</button></div><div id="overviewOrders"></div></div>
      </div>
    </div>

    <div id="tab-online" class="tab-panel">
      <div class="section-header"><span class="section-title">目前在线客户预览</span><button class="btn-sm btn-primary" onclick="loadOnline()">刷新在线</button></div>
      <div id="onlineTable"></div>
    </div>

    <div id="tab-customers" class="tab-panel">
      <div class="section-header"><span class="section-title">所有客户预览 / 账户管理</span><button class="btn-sm btn-primary" onclick="createCustomer()">+ 创建客户</button></div>
      <div class="toolbar">
        <input id="customerKeyword" placeholder="搜索用户名 / 状态 / 订单" onkeydown="if(event.key==='Enter')loadCustomers()">
        <button class="btn-sm btn-gray" onclick="loadCustomers()">搜索</button>
        <button class="btn-sm btn-green" onclick="loadCustomers('', true)">只看在线</button>
      </div>
      <div id="customersTable"></div>
    </div>

    <div id="tab-orders" class="tab-panel">
      <div class="section-header"><span class="section-title">订单管理 / 剩余时长显示修改</span><button class="btn-sm btn-primary" onclick="loadOrders()">刷新订单</button></div>
      <div class="toolbar">
        <input id="orderKeyword" placeholder="搜索客户 / 队伍码 / session" onkeydown="if(event.key==='Enter')loadOrders()">
        <select id="orderStatus" onchange="loadOrders()">
          <option value="">全部状态</option><option value="waiting_ready_timer">等待计时</option><option value="running">运行中</option><option value="stopping">停止中</option><option value="finished">已完成</option><option value="failed">失败</option><option value="interrupted_by_admin">管理员中断</option><option value="interrupted_by_disconnect">失联中断</option>
        </select>
        <button class="btn-sm btn-gray" onclick="loadOrders()">搜索</button>
      </div>
      <div id="ordersTable"></div>
    </div>

    <div id="tab-settings" class="tab-panel">
      <div class="section-header"><span class="section-title">系统设置</span><button class="btn-sm btn-primary" onclick="saveSettings()">保存设置</button></div>
      <div class="panel">
        <div class="settings-row">
          <div><b>客户公告栏</b><div class="hint">显示在客户首页，也通过 /api/public/settings 暴露给前端。</div></div>
          <div><label class="switch-line"><input id="announcementEnabled" type="checkbox"> 开启公告</label><textarea id="announcementText" placeholder="输入向客户展示的公告"></textarea></div>
        </div>
        <div class="settings-row">
          <div><b>隐私模式</b><div class="hint">开启后客户侧隐藏队伍码与 control session 细节，保留后台完整可见。</div></div>
          <div><label class="switch-line"><input id="privacyMode" type="checkbox"> 开启隐私模式</label></div>
        </div>
        <div class="settings-row">
          <div><b>平台维护模式</b><div class="hint">开启后客户不能新下单；已有订单、充值、历史查询不受影响。</div></div>
          <div><label class="switch-line"><input id="maintenanceMode" type="checkbox"> 开启维护模式</label></div>
        </div>
      </div>
    </div>
  </div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, opts={}) {
  const headers = {'Content-Type':'application/json', ...(opts.headers || {})};
  delete opts.headers;
  const res = await fetch(path, {credentials:'same-origin', headers, ...opts});
  const data = await res.json().catch(()=>({ok:false,error:'bad_json',message:'响应解析失败'}));
  if (!res.ok || data.ok === false) throw new Error(data.message || data.error || res.statusText);
  return data;
}
function statusBadge(status) {
  const cls = status === 'running' ? 'badge-running' : (status === 'waiting_ready_timer' || status === 'stopping' ? 'badge-waiting' : (status === 'finished' ? 'badge-done' : (String(status).includes('failed') || String(status).includes('interrupted') ? 'badge-failed' : 'badge-offline')));
  return `<span class="badge ${cls}">${esc(status || '-')}</span>`;
}
function onlineBadge(v) { return v ? '<span class="badge badge-online">在线</span>' : '<span class="badge badge-offline">离线</span>'; }
function fmtMin(m) { m = Number(m || 0); return m >= 60 ? `${Math.floor(m/60)}时${m%60}分` : `${m}分`; }
function fmtDate(s) { return s ? new Date(s).toLocaleString() : '-'; }
function showTab(name) {
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'online') loadOnline();
  if (name === 'customers') loadCustomers();
  if (name === 'orders') loadOrders();
  if (name === 'settings') loadSettings();
}
document.querySelectorAll('.nav-tab').forEach(t => t.addEventListener('click', () => showTab(t.dataset.tab)));

async function loadOverview() {
  const d = await api('/api/admin/overview');
  const cards = [
    ['客户总数', d.customer_count], ['在线客户', d.online_count], ['活动订单', d.active_order_count],
    ['运行中', d.running_count], ['已完成', d.finished_count], ['总剩余分钟', d.total_balance_minutes],
  ];
  $('overviewCards').innerHTML = cards.map(([k,v]) => `<div class="stat-card"><div class="stat-label">${k}</div><div class="stat-value">${esc(v)}</div></div>`).join('');
}
async function loadOnline(target='onlineTable') {
  const d = await api('/api/admin/customers?online_only=true');
  renderCustomers(d.customers, target);
}
async function loadCustomers(keyword, onlineOnly=false) {
  const q = keyword ?? $('customerKeyword')?.value ?? '';
  const d = await api('/api/admin/customers?keyword=' + encodeURIComponent(q) + '&online_only=' + (onlineOnly ? 'true':'false'));
  renderCustomers(d.customers, 'customersTable');
}
function renderCustomers(rows, target) {
  if (!rows.length) { $(target).innerHTML = '<div class="empty-state">暂无客户</div>'; return; }
  $(target).innerHTML = `<table class="data-table"><thead><tr>
    <th>ID</th><th>客户</th><th>在线</th><th>状态</th><th>剩余分钟</th><th>局数</th><th>当前订单</th><th>最后在线</th><th>操作</th>
  </tr></thead><tbody>` + rows.map(c => `<tr>
    <td>${c.id}</td><td><b>${esc(c.username)}</b></td><td>${onlineBadge(c.online)}</td><td>${statusBadge(c.status)}</td>
    <td><b>${fmtMin(c.balance_minutes)}</b></td><td>${esc(c.balance_rounds)}</td>
    <td>${c.active_order ? `${statusBadge(c.active_order.status)} 剩 ${fmtMin(c.active_order.remaining_minutes)}` : '-'}</td>
    <td>${fmtDate(c.last_seen_at)}</td>
    <td>
      <button class="btn-sm btn-green" onclick="editBalance(${c.id}, ${c.balance_minutes}, ${c.balance_rounds})">调时长</button>
      <button class="btn-sm btn-gray" onclick="resetPwd(${c.id})">改密码</button>
      <button class="btn-sm btn-amber" onclick="setCustomerStatus(${c.id}, '${c.status === 'active' ? 'frozen' : 'active'}')">${c.status === 'active' ? '冻结' : '解冻'}</button>
      <button class="btn-sm btn-purple" onclick="showCustomerOrders(${c.id}, '${esc(c.username)}')">历史</button>
    </td></tr>`).join('') + '</tbody></table>';
}
async function createCustomer() {
  const username = prompt('客户用户名'); if (!username) return;
  const password = prompt('客户密码', '123456') || '123456';
  const balance_minutes = Number(prompt('初始分钟', '0') || 0);
  await api('/api/admin/customers', {method:'POST', body:JSON.stringify({username,password,balance_minutes})});
  await loadCustomers();
}
async function editBalance(id, oldMinutes, oldRounds) {
  const mode = prompt('输入新的分钟余额；或输入 +30 / -15 做增减', String(oldMinutes));
  if (mode === null) return;
  const body = {};
  if (/^[+-]/.test(mode.trim())) body.delta_minutes = Number(mode);
  else body.balance_minutes = Number(mode);
  const rounds = prompt('新的局数余额（留空不改）', String(oldRounds ?? 0));
  if (rounds !== null && rounds !== '') body.balance_rounds = Number(rounds);
  await api(`/api/admin/customers/${id}/balance`, {method:'PUT', body:JSON.stringify(body)});
  await loadCustomers(); await loadOnline();
}
async function resetPwd(id) {
  const password = prompt('新密码', '123456'); if (!password) return;
  await api(`/api/admin/customers/${id}/password`, {method:'PUT', body:JSON.stringify({password})});
  alert('密码已修改');
}
async function setCustomerStatus(id, status) {
  if (!confirm(status === 'frozen' ? '确认冻结该客户？' : '确认解冻该客户？')) return;
  await api(`/api/admin/customers/${id}/status`, {method:'PUT', body:JSON.stringify({status})});
  await loadCustomers(); await loadOnline();
}
async function showCustomerOrders(id, name) {
  showTab('orders');
  const d = await api('/api/admin/orders?customer_id=' + id);
  renderOrders(d.orders);
  $('orderKeyword').value = name;
}
async function loadOrders() {
  const q = $('orderKeyword')?.value || '';
  const st = $('orderStatus')?.value || '';
  const d = await api('/api/admin/orders?keyword=' + encodeURIComponent(q) + '&status=' + encodeURIComponent(st));
  renderOrders(d.orders);
}
function renderOrders(rows, target='ordersTable') {
  if (!rows.length) { $(target).innerHTML = '<div class="empty-state">暂无订单</div>'; return; }
  $(target).innerHTML = `<table class="data-table"><thead><tr>
    <th>ID</th><th>客户</th><th>队伍码</th><th>状态</th><th>购买</th><th>剩余</th><th>开始</th><th>结束</th><th>设备/session</th><th>操作</th>
  </tr></thead><tbody>` + rows.map(o => `<tr>
    <td>${o.id}</td><td>${esc(o.customer_username || o.customer_id)}</td><td>${esc(o.team_code || '-')}</td>
    <td>${statusBadge(o.status)}</td><td>${fmtMin(o.requested_minutes)}</td><td><b>${fmtMin(o.remaining_minutes)}</b></td>
    <td>${fmtDate(o.started_at)}</td><td>${fmtDate(o.end_at)}</td><td>#${esc(o.binding?.device_id || o.device_id || '-')}<br><span class="hint">${esc(o.control_session_id || '-')}</span></td>
    <td>
      <button class="btn-sm btn-green" onclick="adjustOrder(${o.id})">加减时</button>
      <button class="btn-sm btn-gray" onclick="orderDetail(${o.id})">详情</button>
      ${['running','waiting_ready_timer','stopping'].includes(o.status) ? `<button class="btn-sm btn-danger" onclick="stopOrder(${o.id})">停止</button>` : ''}
    </td></tr>`).join('') + '</tbody></table>';
}
async function adjustOrder(id) {
  const add = Number(prompt('调整订单剩余时长（分钟，可填负数）', '30') || 0);
  if (!add) return;
  await api(`/api/admin/orders/${id}/add-time`, {method:'POST', body:JSON.stringify({add_minutes:add})});
  await loadOrders(); await loadOverview();
}
async function stopOrder(id) {
  if (!confirm('确认向中央下发 stop_current 并停止该订单？')) return;
  await api(`/api/admin/orders/${id}/stop`, {method:'POST', body:'{}'});
  await loadOrders();
}
async function orderDetail(id) {
  const d = await api('/api/admin/orders/' + id);
  alert(JSON.stringify(d.order, null, 2));
}
async function loadSettings() {
  const d = await api('/api/admin/settings');
  const s = d.settings;
  $('privacyMode').checked = !!s.privacy_mode_enabled;
  $('maintenanceMode').checked = !!s.maintenance_mode_enabled;
  $('announcementEnabled').checked = !!s.announcement_enabled;
  $('announcementText').value = s.announcement_text || '';
}
async function saveSettings() {
  await api('/api/admin/settings', {method:'PUT', body:JSON.stringify({
    privacy_mode_enabled: $('privacyMode').checked,
    maintenance_mode_enabled: $('maintenanceMode').checked,
    announcement_enabled: $('announcementEnabled').checked,
    announcement_text: $('announcementText').value
  })});
  alert('保存成功');
  await loadOverview();
}
async function loadAll() {
  await loadOverview();
  const [online, orders] = await Promise.all([api('/api/admin/customers?online_only=true'), api('/api/admin/orders')]);
  renderCustomers(online.customers.slice(0, 6), 'overviewOnline');
  renderOrders(orders.orders.filter(o => ['claiming_device','device_claimed','commanding','waiting_ready_timer','running','stopping'].includes(o.status)).slice(0, 6), 'overviewOrders');
}
loadAll().then(loadSettings).catch(e => alert(e.message));
</script>
</body>
</html>"""
    return template.replace("__ADMIN__", _escape(admin.get("username"))).replace("__ROLE__", _escape(admin.get("role")))
