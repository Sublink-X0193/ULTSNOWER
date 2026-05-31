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
        body = f"""
        {banner}
        <h2>你好，{_escape(customer['username'])}</h2>
        <p>分钟余额：<b>{customer['balance_minutes']}</b></p>
        <p>中央容量：<b>{_escape(capacity.get('capacity_label'))}</b> / 可用：{_escape(capacity.get('available'))}</p>
        <p><a href="/orders/new">下单</a> · <a href="/recharge">卡密充值</a> · <a href="/orders/current">当前订单</a> · <a href="/orders/history">历史订单</a></p>
        <form method="post" action="/logout"><button>登出</button></form>
        {_pre(current or '暂无当前订单')}
        """
        return HTMLResponse(_layout("商户首页", body))

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
        merchant_settings = service.get_settings()
        body = f"""
        <p>管理员：{_escape(admin['username'])} / {_escape(admin['role'])}</p>
        <form method="post" action="/merchant-admin/settings">
          <label><input type="checkbox" name="privacy_mode_enabled" value="1" {'checked' if merchant_settings.get('privacy_mode_enabled') else ''}> 隐私模式：客户侧隐藏队伍码/控制会话细节</label><br>
          <label><input type="checkbox" name="maintenance_mode_enabled" value="1" {'checked' if merchant_settings.get('maintenance_mode_enabled') else ''}> 维护模式：禁止新下单</label><br>
          <label><input type="checkbox" name="announcement_enabled" value="1" {'checked' if merchant_settings.get('announcement_enabled') else ''}> 开启公告</label><br>
          <label>公告内容<br><textarea name="announcement_text" rows="6" cols="80">{_escape(merchant_settings.get('announcement_text') or '')}</textarea></label><br>
          <button type="submit">保存设置</button>
        </form>
        <form method="post" action="/merchant-admin/logout"><button>退出后台</button></form>
        """
        return HTMLResponse(_layout("商户后台配置", body))

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
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
    <style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:880px;margin:40px auto;padding:0 20px;line-height:1.6}}input,button{{padding:8px;margin:6px}}pre{{background:#f6f8fa;padding:12px;overflow:auto}}</style>
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

    return html.escape(str(value or ""), quote=True)


def _pre(value: Any) -> str:
    return "<pre>" + _escape(repr(value) if not isinstance(value, str) else value) + "</pre>"
