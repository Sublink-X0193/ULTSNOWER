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

    @app.get("/health")
    def health() -> dict[str, Any]:
        return json_ok(service="merchant_portal", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        customer = maybe_customer(request)
        if not customer:
            return HTMLResponse(_layout("欢迎", '<p>请 <a href="/login">登录</a> 或 <a href="/register">注册</a></p>'))
        current = service.current_order(customer["id"])
        try:
            capacity = service.bridge.get_capacity()
        except Exception as e:  # UI should keep rendering even when bridge is down
            capacity = {"available": False, "capacity_label": "unknown", "error": str(e)}
        body = f"""
        <h2>你好，{customer['username']}</h2>
        <p>分钟余额：<b>{customer['balance_minutes']}</b></p>
        <p>中央容量：<b>{capacity.get('capacity_label')}</b> / 可用：{capacity.get('available')}</p>
        <p><a href="/orders/new">下单</a> · <a href="/recharge">卡密充值</a> · <a href="/orders/current">当前订单</a> · <a href="/orders/history">历史订单</a></p>
        <form method="post" action="/logout"><button>登出</button></form>
        <pre>{current or '暂无当前订单'}</pre>
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
        return HTMLResponse(_layout("下单", _form("/orders", [("requested_minutes", "分钟"), ("team_code", "队伍码"), ("quality", "配置/品质")], "下单")))

    @app.post("/orders")
    def order_form(request: Request, requested_minutes: int = Form(...), team_code: str = Form(...), quality: str = Form("standard"), customer: dict[str, Any] = Depends(current_customer)) -> RedirectResponse:
        service.place_order(customer["id"], requested_minutes=requested_minutes, team_code=team_code, quality=quality, idempotency_key=request.headers.get("X-Idempotency-Key"))
        return RedirectResponse("/orders/current", status_code=303)

    @app.get("/orders/current", response_class=HTMLResponse)
    def current_order_page(customer: dict[str, Any] = Depends(current_customer)) -> HTMLResponse:
        return HTMLResponse(_layout("当前订单", f"<pre>{service.current_order(customer['id']) or '暂无当前订单'}</pre><p><a href='/'>返回</a></p>"))

    @app.get("/orders/history", response_class=HTMLResponse)
    def history_page(customer: dict[str, Any] = Depends(current_customer)) -> HTMLResponse:
        return HTMLResponse(_layout("历史订单", "<pre>" + repr(service.order_history(customer["id"])) + "</pre><p><a href='/'>返回</a></p>"))

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
        return json_ok(**result)

    @app.get("/api/orders/current")
    def api_current_order(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(order=service.current_order(customer["id"]))

    @app.get("/api/orders/history")
    def api_history(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(orders=service.order_history(customer["id"]))

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
