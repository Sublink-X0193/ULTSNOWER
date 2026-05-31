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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

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

    def truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "on", "y"}:
                return True
            if v in {"0", "false", "no", "off", "n", ""}:
                return False
        return bool(value)

    def normalize_admin_settings_payload(values: dict[str, Any]) -> dict[str, Any]:
        payload = dict(values or {})
        if "privacy_mode" in payload and "privacy_mode_enabled" not in payload:
            payload["privacy_mode_enabled"] = truthy(payload.get("privacy_mode"))
        if "maintenance_mode" in payload and "maintenance_mode_enabled" not in payload:
            payload["maintenance_mode_enabled"] = truthy(payload.get("maintenance_mode"))
        for key in ("night_time_check", "ace_enabled", "allow_custom_loadout", "announcement_enabled"):
            if key in payload:
                payload[key] = truthy(payload.get(key))
        return payload

    def admin_settings_view(st: dict[str, Any]) -> dict[str, Any]:
        out = dict(st)
        out["privacy_mode"] = "1" if truthy(st.get("privacy_mode_enabled")) else "0"
        out["maintenance_mode"] = "1" if truthy(st.get("maintenance_mode_enabled")) else "0"
        out["global_radar_url_editable"] = True
        out["system_name_placeholder"] = st.get("system_name") or "SNOW 自助下单"
        return out

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
            banner += f"<div style='background:#eef6ff;border:1px solid #9ec5fe;padding:10px;border-radius:8px'>公告：{_safe_notice_html(merchant_settings.get('announcement_text'))}</div>"
        if merchant_settings.get("global_radar_url"):
            url = _escape(merchant_settings.get("global_radar_url"))
            banner += f"<div style='background:#f8fafc;border:1px solid #cbd5e1;padding:10px;border-radius:8px;margin-top:8px'>全局备注地址：<span style='font-family:Consolas,monospace'>{url}</span></div>"
        if merchant_settings.get("maintenance_mode_enabled"):
            msg = _escape(merchant_settings.get("maintenance_message") or "暂时不能新下单。")
            banner += f"<div style='background:#fff3cd;border:1px solid #ffda6a;padding:10px;border-radius:8px;margin-top:8px'>维护模式已开启：{msg}</div>"
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
        system_name: str = Form("SNOW 自助下单"),
        privacy_mode_enabled: str | None = Form(None),
        maintenance_mode_enabled: str | None = Form(None),
        maintenance_message: str = Form(""),
        announcement_enabled: str | None = Form(None),
        announcement_text: str = Form(""),
        admin: dict[str, Any] = Depends(current_admin),
    ) -> RedirectResponse:
        service.update_settings(
            admin["id"],
            {
                "system_name": system_name,
                "privacy_mode_enabled": privacy_mode_enabled == "1",
                "maintenance_mode_enabled": maintenance_mode_enabled == "1",
                "maintenance_message": maintenance_message,
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
            "system_name": merchant_settings["system_name"],
            "default_limit_rounds": merchant_settings["default_limit_rounds"],
            "absolute_rounds_per_hour": merchant_settings["absolute_rounds_per_hour"],
            "night_time_check": merchant_settings["night_time_check"],
            "night_start_time": merchant_settings["night_start_time"],
            "night_end_time": merchant_settings["night_end_time"],
            "global_radar_url": merchant_settings["global_radar_url"],
            "privacy_mode_enabled": merchant_settings["privacy_mode_enabled"],
            "maintenance_mode_enabled": merchant_settings["maintenance_mode_enabled"],
            "maintenance_message": merchant_settings["maintenance_message"] if merchant_settings["maintenance_mode_enabled"] else "",
            "announcement_enabled": merchant_settings["announcement_enabled"],
            "announcement_text": merchant_settings["announcement_text"] if merchant_settings["announcement_enabled"] else "",
        })

    @app.get("/api/notice")
    def api_notice() -> dict[str, Any]:
        merchant_settings = service.get_settings()
        return json_ok(content=merchant_settings.get("announcement_text") if merchant_settings.get("announcement_enabled") else "")

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
        return json_ok(settings=admin_settings_view(service.get_settings()))

    @app.put("/api/admin/settings")
    def api_admin_put_settings(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(settings=admin_settings_view(service.update_settings(admin["id"], normalize_admin_settings_payload(body))))

    @app.post("/api/admin/settings")
    def api_admin_post_settings(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        payload = body.get("settings") if isinstance(body.get("settings"), dict) else body
        return json_ok(msg="保存成功", settings=admin_settings_view(service.update_settings(admin["id"], normalize_admin_settings_payload(payload))))

    @app.post("/api/admin/notice")
    def api_admin_notice(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        content = str(body.get("content") or "")
        service.update_settings(admin["id"], {"announcement_text": content, "announcement_enabled": bool(content.strip())})
        return json_ok(msg="保存成功")

    @app.get("/api/admin/equipment-config")
    def api_admin_equipment_config(_admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(**service.get_equipment_config())

    @app.post("/api/admin/equipment-config")
    def api_admin_equipment_config_save(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        service.update_equipment_config(admin["id"], body)
        return json_ok(msg="保存成功", **service.get_equipment_config())

    @app.get("/api/admin/cards")
    def api_admin_cards(keyword: str = "", status: str = "", type: str = "", _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:  # noqa: A002 - keep legacy query name
        cards = service.list_recharge_cards(keyword=keyword, status=status, card_type=type)
        return json_ok(cards=cards, total=len(cards))

    @app.post("/api/admin/cards/generate")
    def api_admin_cards_generate(body: dict[str, Any] = Body(...), _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        minutes = int(body.get("minutes") or 0)
        if minutes <= 0:
            minutes = int(body.get("hours") or 0) * 60 + int(body.get("days") or 0) * 24 * 60
        cards = service.generate_recharge_cards(
            count=int(body.get("count") or 1),
            minutes=minutes,
            rounds=int(body.get("rounds") or body.get("absolute_rounds") or 0),
            card_type=body.get("card_type") or "normal",
            mode=body.get("mode") or "machine",
            night_coin_loss=int(body.get("night_coin_loss") or 0),
        )
        return json_ok(msg="生成成功", cards=cards)

    @app.delete("/api/admin/cards/{code}")
    def api_admin_card_delete(code: str, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(**service.delete_recharge_card(code))

    @app.get("/api/admin/cards/export-unused")
    def api_admin_cards_export_unused(type: str = "", _admin: dict[str, Any] = Depends(current_admin)) -> Response:  # noqa: A002 - keep legacy query name
        return Response(
            service.export_unused_cards_csv(card_type=type),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=unused_cards_local.csv"},
        )

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


def _safe_notice_html(value: Any) -> str:
    """Allow admin-authored rich-text notice HTML while stripping executable parts."""
    import re

    html = str(value or "")
    html = re.sub(r"(?is)<\s*(script|style|iframe|object|embed|meta|link)[^>]*>.*?<\s*/\s*\1\s*>", "", html)
    html = re.sub(r"(?is)<\s*/?\s*(script|style|iframe|object|embed|meta|link)[^>]*>", "", html)
    html = re.sub(r"(?is)\s+on\w+\s*=\s*(['\"]).*?\1", "", html)
    html = re.sub(r"(?is)\s+on\w+\s*=\s*[^\s>]+", "", html)
    html = re.sub(r"(?i)javascript\s*:", "", html)
    return html


def _customer_dashboard_html(customer: dict[str, Any], current: dict[str, Any] | None, capacity: dict[str, Any], settings: dict[str, Any], banner: str) -> str:
    template = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__SYSTEM_NAME__ - 客户中心</title>
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
    .toast { position:fixed; right:22px; top:74px; z-index:80; min-width:240px; max-width:420px; padding:12px 14px; border-radius:12px; background:#111827; color:#fff; box-shadow:0 18px 38px rgba(15,23,42,.28); opacity:0; transform:translateY(-10px); pointer-events:none; transition:all .22s; font-size:13px; font-weight:700; }
    .toast.show { opacity:1; transform:translateY(0); }
    @media(max-width:900px){ .hero-row,.form-grid,.cards{grid-template-columns:1fr}.topbar{height:auto;padding:12px;align-items:flex-start;gap:10px}.topbar-right{flex-direction:column;align-items:flex-end}.content{padding:14px}.orders-table{display:block;overflow:auto} }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-logo">__SYSTEM_NAME__ · 客户中心</div>
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
  <div id="toast" class="toast"></div>
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
function toast(msg){
  const el=$('toast');
  el.textContent=msg;
  el.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer=setTimeout(()=>el.classList.remove('show'),2600);
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
    toast('下单已创建，等待设备 ready 后开始计时'); showTab('current'); await loadAll();
  } catch(e){ toast(e.message); }
}
async function redeem(){
  try { await api('/api/recharge/redeem',{method:'POST',body:JSON.stringify({code:$('rechargeCode').value})}); toast('充值成功，刷新页面查看余额'); setTimeout(()=>location.reload(),700); } catch(e){ toast(e.message); }
}
async function loadAll(){ await loadCurrent(); await loadHistory(); }
loadAll().catch(()=>{});
setInterval(loadCurrent, 15000);
</script>
</body>
</html>"""
    return (
        template.replace("__USERNAME__", _escape(customer.get("username")))
        .replace("__SYSTEM_NAME__", _escape(settings.get("system_name") or "SNOW 自助下单"))
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
    .badge-unused { background:#dcfce7; color:#166534; }
    .badge-used { background:#f3f4f6; color:#6b7280; }
    .badge-machine { background:#dbeafe; color:#1d4ed8; }
    .badge-absolute { background:#fef3c7; color:#92400e; }
    .badge-purple { background:#ede9fe; color:#6d28d9; }
    .empty-state { padding:32px; text-align:center; color:#9ca3af; background:#fff; border:1px dashed #d1d5db; border-radius:12px; }
    .hint { color:#6b7280; font-size:12px; line-height:1.7; }
    .settings-row { display:grid; grid-template-columns: 220px 1fr; gap:12px; padding:14px 0; border-bottom:1px solid #f3f4f6; align-items:flex-start; }
    .settings-row:last-child { border-bottom:0; }
    textarea { width:100%; min-height:120px; border:1px solid #d1d5db; border-radius:10px; padding:12px; font:inherit; resize:vertical; }
    .switch-line { display:flex; align-items:center; gap:8px; font-weight:600; }
    .settings-wrap { display:flex; flex-direction:column; gap:16px; align-items:flex-start; }
    .settings-card { width:100%; max-width:760px; background:#fff; border-radius:10px; padding:20px 24px; border:1px solid #e5e7eb; box-shadow:0 1px 3px rgba(0,0,0,.04); }
    .settings-card.narrow { max-width:640px; }
    .setting-card-title { font-size:15px; font-weight:700; margin-bottom:12px; color:#111827; }
    .notice-toolbar { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:0; padding:8px; background:#f9fafb; border:1px solid #e5e7eb; border-radius:6px 6px 0 0; }
    .notice-toolbar select { height:28px; padding:3px 6px; border:1px solid #d1d5db; border-radius:4px; font-size:13px; background:#fff; }
    .notice-toolbar input[type=color] { width:32px; height:28px; border:1px solid #d1d5db; border-radius:4px; padding:2px; cursor:pointer; background:#fff; }
    .notice-tool { padding:4px 10px; border:1px solid #d1d5db; border-radius:4px; background:#fff; color:#374151; cursor:pointer; font-size:13px; min-width:30px; }
    .notice-tool:hover { background:#f3f4f6; }
    .notice-sep { width:1px; background:#e5e7eb; margin:0 2px; display:inline-block; }
    .notice-editor { min-height:128px; border:1px solid #e5e7eb; border-top:none; border-radius:0 0 6px 6px; padding:12px; font-size:14px; line-height:1.7; outline:none; background:#fff; }
    .notice-editor:empty:before { content: attr(data-placeholder); color:#9ca3af; }
    .setting-actions { margin-top:10px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .maintenance-box { border:1px solid #fde68a; background:#fffbeb; padding:10px 12px; border-radius:8px; }
    .maintenance-box label { color:#b45309; font-weight:700; }
    .readonly-note { padding:10px 12px; border-radius:8px; background:#f8fafc; border:1px dashed #cbd5e1; color:#64748b; font-size:12px; line-height:1.7; }
    .mini-grid { display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:12px; }
    .card-list { display:flex; flex-direction:column; gap:6px; max-height:220px; overflow:auto; margin-top:8px; padding:8px; border:1px solid #e5e7eb; border-radius:8px; background:#f9fafb; }
    .card-item { display:flex; justify-content:space-between; gap:8px; align-items:center; padding:7px 9px; border:1px solid #e5e7eb; border-radius:7px; background:#fff; font-family:Consolas, "SFMono-Regular", monospace; }
    .config-input { width:86px; height:30px; border:1px solid #d1d5db; border-radius:7px; padding:0 8px; }
    .modal-mask { position: fixed; inset: 0; background: rgba(15,23,42,.45); display: none; align-items: center; justify-content: center; z-index: 1000; padding: 18px; }
    .modal-mask.show { display: flex; }
    .modal { width: min(560px, 96vw); max-height: 90vh; overflow: hidden; background: #fff; border-radius: 14px; box-shadow: 0 24px 80px rgba(15,23,42,.28); animation: modalIn .16s ease-out; }
    .modal.modal-wide { width: min(960px, 96vw); }
    @keyframes modalIn { from { transform: translateY(8px) scale(.98); opacity: 0 } to { transform: none; opacity: 1 } }
    .modal-head { padding: 16px 18px; border-bottom: 1px solid #e5e7eb; font-weight: 800; font-size: 16px; }
    .modal-body { padding: 16px 18px; max-height: 68vh; overflow: auto; }
    .modal-foot { padding: 12px 18px; border-top: 1px solid #e5e7eb; display: flex; justify-content: flex-end; gap: 8px; background: #f9fafb; }
    .field { margin-bottom: 12px; }
    .field label { display: block; font-size: 12px; color: #6b7280; margin-bottom: 6px; font-weight: 700; }
    .field input, .field select, .field textarea { width: 100%; height: 38px; border: 1px solid #d1d5db; border-radius: 8px; padding: 0 10px; font: inherit; background: #fff; }
    .field textarea { height: 120px; padding: 10px; }
    .field-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .info-box { padding: 10px 12px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; color: #1d4ed8; font-size: 13px; line-height: 1.6; }
    .toast { position: fixed; right: 24px; bottom: 24px; background: #111827; color: #fff; padding: 10px 14px; border-radius: 9px; box-shadow: 0 12px 30px rgba(0,0,0,.22); z-index: 1200; opacity: 0; transform: translateY(8px); transition: all .18s; }
    .toast.show { opacity: 1; transform: none; }
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
    <div class="nav-tab" data-tab="cards">充值卡</div>
    <div class="nav-tab" data-tab="equipment">装备配置</div>
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

    <div id="tab-cards" class="tab-panel">
      <div class="section-header">
        <span class="section-title">充值卡管理 / 印卡密</span>
        <div>
          <button class="btn-sm btn-gray" onclick="exportUnusedCards()">导出未使用卡密</button>
          <button class="btn-sm btn-primary" onclick="openGenCardModal()">+ 生成充值卡</button>
        </div>
      </div>
      <div class="toolbar">
        <input id="cardSearchInput" placeholder="搜索卡密 / 使用客户" onkeydown="if(event.key==='Enter')loadCards()">
        <select id="cardStatusFilter" onchange="loadCards()">
          <option value="">全部状态</option><option value="unused">未使用</option><option value="used">已使用</option>
        </select>
        <select id="cardTypeFilter" onchange="loadCards()">
          <option value="">全部类型</option><option value="normal">普通卡</option><option value="night">包夜卡</option>
        </select>
        <button class="btn-sm btn-gray" onclick="loadCards()">刷新/搜索</button>
      </div>
      <div id="cardsTable"></div>
    </div>

    <div id="tab-equipment" class="tab-panel">
      <div class="section-header">
        <span class="section-title">绝密装备配置</span>
        <button class="btn-sm btn-primary" onclick="saveEquipmentConfig()">保存装备配置</button>
      </div>
      <div class="panel" style="margin-bottom:14px">
        <div class="mini-grid">
          <label class="switch-line"><input type="checkbox" id="allowCustomLoadout"> 允许客户自定义配装</label>
          <label style="display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;">最大配装价值 <input type="number" id="maxLoadoutCost" min="0" class="config-input" value="65"> W</label>
        </div>
        <div class="hint" style="margin-top:8px">这些是拆分前全局装备配置迁移到商户端的本地参数；中央 Bridge/Agent 执行时消费商户订单里的配置，不在商户端直接控制设备。</div>
      </div>
      <div id="equipmentConfigTable"></div>
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

    <!-- ===== 系统设置 ===== -->
    <div id="tab-settings" class="tab-panel">
      <!-- 公告栏编辑 -->
      <div
        style="background:#fff;border-radius:10px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,0.04);max-width:760px;margin-bottom:16px;">
        <h3 style="font-size:15px;font-weight:600;margin-bottom:12px;">客户公告栏</h3>
        <div
          style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:0;padding:8px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px 6px 0 0;">
          <select id="noticeFontSize" onchange="noticeExec('fontSize',this.value)"
            style="padding:3px 6px;border:1px solid #d1d5db;border-radius:4px;font-size:13px;">
            <option value="">字号</option>
            <option value="1">极小</option>
            <option value="2">小</option>
            <option value="3">正常</option>
            <option value="4">大</option>
            <option value="5">很大</option>
            <option value="6">超大</option>
            <option value="7">极大</option>
          </select>
          <input type="color" id="noticeFontColor" onchange="noticeExec('foreColor',this.value)" title="字体颜色"
            style="width:32px;height:28px;border:1px solid #d1d5db;border-radius:4px;padding:2px;cursor:pointer;"
            value="#000000" />
          <button onclick="noticeExec('bold')" title="加粗"
            style="padding:4px 10px;border:1px solid #d1d5db;border-radius:4px;background:#fff;font-weight:700;cursor:pointer;">B</button>
          <button onclick="noticeExec('italic')" title="倾斜"
            style="padding:4px 10px;border:1px solid #d1d5db;border-radius:4px;background:#fff;font-style:italic;cursor:pointer;">I</button>
          <button onclick="noticeExec('underline')" title="下划线"
            style="padding:4px 10px;border:1px solid #d1d5db;border-radius:4px;background:#fff;text-decoration:underline;cursor:pointer;">U</button>
          <button onclick="noticeExec('strikeThrough')" title="删除线"
            style="padding:4px 10px;border:1px solid #d1d5db;border-radius:4px;background:#fff;text-decoration:line-through;cursor:pointer;">S</button>
          <span style="width:1px;background:#e5e7eb;margin:0 2px;display:inline-block;"></span>
          <button onclick="noticeExec('justifyLeft')" title="左对齐"
            style="padding:4px 8px;border:1px solid #d1d5db;border-radius:4px;background:#fff;cursor:pointer;">左</button>
          <button onclick="noticeExec('justifyCenter')" title="居中"
            style="padding:4px 8px;border:1px solid #d1d5db;border-radius:4px;background:#fff;cursor:pointer;">中</button>
          <button onclick="noticeExec('justifyRight')" title="右对齐"
            style="padding:4px 8px;border:1px solid #d1d5db;border-radius:4px;background:#fff;cursor:pointer;">右</button>
          <span style="width:1px;background:#e5e7eb;margin:0 2px;display:inline-block;"></span>
          <button onclick="noticeExec('removeFormat')" title="清除格式"
            style="padding:4px 10px;border:1px solid #d1d5db;border-radius:4px;background:#fff;cursor:pointer;color:#6b7280;">清除格式</button>
        </div>
        <div id="noticeEditor" contenteditable="true"
          style="min-height:120px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 6px 6px;padding:12px;font-size:14px;line-height:1.7;outline:none;">
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;align-items:center;">
          <button class="btn-sm btn-primary" onclick="saveNotice()">保存公告</button>
          <button class="btn-sm btn-gray"
            onclick="document.getElementById('noticeEditor').innerHTML=''">清空</button>
          <span id="noticeSaveResult" style="font-size:13px;color:#22c55e;"></span>
        </div>
      </div>

      <!-- 全局参数 -->
      <div
        style="background:#fff;border-radius:10px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,0.04);max-width:600px;">
        <h3 style="font-size:15px;font-weight:600;margin-bottom:16px;">全局参数</h3>
        <div class="field">
          <label>系统名称</label>
          <input type="text" id="settingSystemName" maxlength="32" placeholder="留空则使用默认名称" />
          <div class="hint">登录页与注册页显示的系统名称，留空时默认使用"管理员用户名前3位+电竞"</div>
        </div>
        <div class="field">
          <label>机密每小时局数</label>
          <input type="number" id="settingLimitRounds" min="1" value="4" />
          <div class="hint">机密模式下，每小时充值获得的局数</div>
        </div>
        <div class="field">
          <label>绝密每小时局数</label>
          <input type="number" id="settingAbsoluteRoundsPerHour" min="1" value="3" />
          <div class="hint">绝密模式下，每小时充值获得的局数</div>
        </div>
        <div class="field">
          <label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer;">
            <input type="checkbox" id="settingNightTimeCheck" style="width:auto;"
              onchange="toggleNightTimeRange()" />
            启用包夜卡登录时间限制
          </label>
          <div class="hint">关闭后包夜卡可在任意时间登录，用于测试</div>
        </div>
        <div class="field" id="nightTimeRangeField">
          <label>包夜卡可登录时段</label>
          <div style="display:flex;align-items:center;gap:8px;">
            <input type="time" id="settingNightStartTime" style="width:auto;" />
            <span style="color:#6b7280;">至次日</span>
            <input type="time" id="settingNightEndTime" style="width:auto;" />
          </div>
          <div class="hint">跨午夜时段，开始时间须晚于结束时间（如 22:50 至次日 06:10）</div>
        </div>
        <div class="field">
          <label>全局备注地址</label>
          <input type="text" id="settingGlobalRadarUrl" placeholder="http://8.148.233.14:5000/" />
          <div class="hint">如果设置，所有客户下单后显示此备注地址（优先级高于设备备注地址）</div>
        </div>
        <div class="field">
          <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
            <label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer;">
              <input type="checkbox" id="settingPrivacyMode" style="width:auto;" />
              隐私模式
            </label>
            <label style="display:inline-flex;align-items:center;gap:6px;">
              <span style="color:#6b7280;">低于</span>
              <input type="number" id="settingPrivacySkipBalance" min="0" step="1" value="0"
                style="width:90px;" />
              <span style="color:#6b7280;">W 哈夫币时跳过</span>
            </label>
          </div>
          <div class="hint">开启后客户看不到机器列表，只显示"机密下单"和"绝密下单"按钮，系统自动分配空闲机器；当机器哈夫币余额小于设置值时视为不可用机器，0 为不跳过</div>
          <div class="hint">自动分配采用排钟逻辑：同模式多台空闲时优先分配空闲时间最久的机器（从未跑过订单的机器最优先），优先专用机器，无可用专用机时再使用混合机器</div>
        </div>
        <div class="field">
          <label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer;">
            <input type="checkbox" id="settingAceEnabled" style="width:auto;" />
            下雪反作弊系统 XX-ACE（订单反白嫖）
          </label>
          <div class="hint">开启后，若客户在机器"已准备待进图"状态下主动结单，系统将开启2分钟检测窗口：机器下一状态若为"空闲"则订单正常；若进入"选择干员中"则判定白嫖，自动冻结该客户账号</div>
        </div>

        <div class="field" style="border:1px solid #fde68a;background:#fffbeb;padding:10px 12px;border-radius:8px;">
          <label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer;font-weight:600;color:#b45309;">
            <input type="checkbox" id="settingMaintenanceMode" style="width:auto;" />
            🛠 平台维护模式（开启后客户不能新下单）
          </label>
          <div class="hint">开启后该系统所有客户的"新下单"接口（含隐私模式）都会被拦截并显示下方维护文案。已下单运行中的订单、换机、结单、自助充值、查看历史不受影响。<b>管理员后台手动下单仍可正常使用</b>。</div>
          <label style="display:block;font-size:12px;color:#6b7280;margin-top:8px;">维护文案（向客户展示，留空使用默认）</label>
          <input type="text" id="settingMaintenanceMessage" maxlength="200" placeholder="例如：系统升级中，预计 22:00 恢复" />
        </div>

        <button class="btn-sm btn-primary" onclick="saveSettings()">保存设置</button>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <div id="genCardModal" class="modal-mask">
    <div class="modal modal-wide">
      <div class="modal-head">生成 / 印卡密</div>
      <div class="modal-body">
        <div class="field">
          <label>卡类型</label>
          <select id="cardType" onchange="toggleCardTypeFields()">
            <option value="normal">普通充值卡</option>
            <option value="night">包夜卡</option>
          </select>
        </div>
        <div id="normalCardFields">
          <div class="field">
            <label>模式</label>
            <select id="cardMode" onchange="updateCardEstimate()">
              <option value="machine">机密模式（默认局数）</option>
              <option value="absolute">绝密模式（绝密局数）</option>
              <option value="hybrid">混合模式</option>
            </select>
          </div>
          <div class="field-row">
            <div class="field"><label>小时</label><input id="cardHours" type="number" min="0" value="1" oninput="updateCardEstimate()"></div>
            <div class="field"><label>分钟</label><input id="cardMinutes" type="number" min="0" max="59" value="0" oninput="updateCardEstimate()"></div>
          </div>
          <div class="field-row">
            <div class="field"><label>生成张数</label><input id="cardCount" type="number" min="1" max="100" value="1"></div>
            <div class="field"><label>绝密局数（可覆盖）</label><input id="cardAbsoluteRounds" type="number" min="0" value="0"></div>
          </div>
          <div class="info-box" id="cardEstimate">预计：60 分钟 / 4 局</div>
        </div>
        <div id="nightCardFields" style="display:none">
          <div class="field-row">
            <div class="field"><label>包夜小时</label><input id="cardNightHours" type="number" min="0" value="8"></div>
            <div class="field"><label>包夜分钟</label><input id="cardNightMinutes" type="number" min="0" max="59" value="0"></div>
          </div>
          <div class="field-row">
            <div class="field"><label>生成张数</label><input id="cardCountNight" type="number" min="1" max="100" value="1"></div>
            <div class="field"><label>战损扣除</label>
              <select id="cardNightLossType" onchange="toggleNightLossFields()">
                <option value="rounds">扣局数</option>
                <option value="coins">扣哈夫币</option>
              </select>
            </div>
          </div>
          <div class="field-row">
            <div class="field" id="nightRoundsField"><label>战损局数</label><input id="cardNightRounds" type="number" min="0" value="0"></div>
            <div class="field" id="nightCoinsField" style="display:none"><label>战损哈夫币 W</label><input id="cardNightCoinLoss" type="number" min="0" value="0"></div>
          </div>
          <div class="hint">包夜卡继承“包夜卡登录时间限制”和开始/结束时间配置；卡密本身只存储时长与战损规则。</div>
        </div>
        <div id="generatedCards" style="display:none">
          <div class="section-header" style="margin:14px 0 8px"><span class="section-title">已生成卡密</span><button class="btn-sm btn-gray" onclick="copyGeneratedCards()">复制全部</button></div>
          <div id="cardListOutput" class="card-list"></div>
        </div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('genCardModal')">关闭</button><button id="submitGenCards" class="btn-sm btn-primary" onclick="submitGenCards()">生成</button></div>
    </div>
  </div>

  <div id="addUserModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">创建客户</div>
      <div class="modal-body">
        <div class="field"><label>用户名</label><input id="newUsername" placeholder="客户登录用户名"></div>
        <div class="field"><label>密码</label><input id="newPassword" value="123456" placeholder="客户登录密码"></div>
        <div class="field-row">
          <div class="field"><label>初始分钟</label><input id="newBalanceMinutes" type="number" min="0" value="0"></div>
          <div class="field"><label>初始局数</label><input id="newBalanceRounds" type="number" min="0" value="0"></div>
        </div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('addUserModal')">取消</button><button class="btn-sm btn-primary" onclick="submitCreateCustomer()">创建</button></div>
    </div>
  </div>

  <div id="changePwdModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">修改客户密码</div>
      <div class="modal-body">
        <input id="changePwdUserId" type="hidden">
        <div class="field"><label>用户名</label><input id="changePwdUsername" disabled style="background:#f9fafb"></div>
        <div class="field"><label>新密码</label><input id="changePwdNew" value="123456" placeholder="输入新密码"></div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('changePwdModal')">取消</button><button class="btn-sm btn-primary" onclick="submitResetPwd()">确认修改</button></div>
    </div>
  </div>

  <div id="changeBalanceModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">修改客户剩余时长</div>
      <div class="modal-body">
        <input id="changeBalUserId" type="hidden">
        <div class="info-box" id="changeBalInfo">--</div>
        <div class="field-row" style="margin-top:12px">
          <div class="field"><label>分钟余额</label><input id="changeBalanceMinutes" type="number" min="0" value="0"></div>
          <div class="field"><label>局数余额</label><input id="changeBalanceRounds" type="number" min="0" value="0"></div>
        </div>
        <div class="hint">直接填写调整后的余额。负数增减请在订单管理里用“加减时”。</div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('changeBalanceModal')">取消</button><button class="btn-sm btn-green" onclick="submitBalance()">保存</button></div>
    </div>
  </div>

  <div id="addTimeModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">订单加减时</div>
      <div class="modal-body">
        <input id="addTimeOrderId" type="hidden">
        <div class="info-box" id="addTimeInfo">--</div>
        <div class="field" style="margin-top:12px">
          <label>操作类型</label>
          <div style="display:flex;gap:16px;align-items:center">
            <label style="display:flex;gap:6px;align-items:center"><input type="radio" name="addTimeOp" value="add" checked> 加时</label>
            <label style="display:flex;gap:6px;align-items:center"><input type="radio" name="addTimeOp" value="sub"> 减时</label>
          </div>
        </div>
        <div class="field-row">
          <div class="field"><label>小时</label><input id="addTimeHours" type="number" min="0" max="24" value="0"></div>
          <div class="field"><label>分钟</label><input id="addTimeMinutes" type="number" min="0" max="59" value="30"></div>
        </div>
        <div class="hint">会直接修改商户本地订单的购买时长/结束时间；中央设备只接收最终 stop。</div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('addTimeModal')">取消</button><button class="btn-sm btn-green" onclick="submitAdjustOrder()">确认调整</button></div>
    </div>
  </div>

  <div id="userOrdersModal" class="modal-mask">
    <div class="modal modal-wide">
      <div class="modal-head" id="userOrdersTitle">客户历史订单</div>
      <div class="modal-body" id="userOrdersContent"></div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('userOrdersModal')">关闭</button></div>
    </div>
  </div>

  <div id="orderDetailModal" class="modal-mask">
    <div class="modal modal-wide">
      <div class="modal-head">订单详情</div>
      <div class="modal-body" id="orderDetailContent"></div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('orderDetailModal')">关闭</button></div>
    </div>
  </div>

  <div id="confirmModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head" id="confirmTitle">确认操作</div>
      <div class="modal-body"><div id="confirmText" class="info-box"></div></div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="resolveConfirm(false)">取消</button><button class="btn-sm btn-danger" id="confirmOkBtn" onclick="resolveConfirm(true)">确认</button></div>
    </div>
  </div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let _defaultLimitRounds = 4;
let _absoluteRoundsPerHour = 3;
let _equipmentRows = [];
async function api(path, opts={}) {
  const headers = {'Content-Type':'application/json', ...(opts.headers || {})};
  delete opts.headers;
  const res = await fetch(path, {credentials:'same-origin', headers, ...opts});
  const data = await res.json().catch(()=>({ok:false,error:'bad_json',message:'响应解析失败'}));
  if (!res.ok || data.ok === false) throw new Error(data.message || data.error || res.statusText);
  return data;
}
function openModal(id) { $(id).classList.add('show'); }
function closeModal(id) { $(id).classList.remove('show'); }
function toast(msg) {
  const el = $('toast'); el.textContent = msg; el.classList.add('show');
  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}
let __confirmResolver = null;
function appConfirm(title, text, okClass='btn-danger') {
  $('confirmTitle').textContent = title || '确认操作';
  $('confirmText').innerHTML = esc(text || '').replace(/\n/g, '<br>');
  $('confirmOkBtn').className = 'btn-sm ' + okClass;
  openModal('confirmModal');
  return new Promise(resolve => { __confirmResolver = resolve; });
}
function resolveConfirm(v) {
  closeModal('confirmModal');
  if (__confirmResolver) __confirmResolver(v);
  __confirmResolver = null;
}
document.querySelectorAll('.modal-mask').forEach(m => m.addEventListener('click', e => {
  if (e.target !== m) return;
  if (m.id === 'confirmModal') resolveConfirm(false);
  else m.classList.remove('show');
}));
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
  if (name === 'cards') loadCards();
  if (name === 'equipment') loadEquipmentConfig();
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
      <button class="btn-sm btn-green" onclick="editBalance(${c.id}, ${c.balance_minutes}, ${c.balance_rounds}, decodeURIComponent('${encodeURIComponent(c.username)}'))">调时长</button>
      <button class="btn-sm btn-gray" onclick="resetPwd(${c.id}, decodeURIComponent('${encodeURIComponent(c.username)}'))">改密码</button>
      <button class="btn-sm btn-amber" onclick="setCustomerStatus(${c.id}, '${c.status === 'active' ? 'frozen' : 'active'}')">${c.status === 'active' ? '冻结' : '解冻'}</button>
      <button class="btn-sm btn-purple" onclick="showCustomerOrders(${c.id}, decodeURIComponent('${encodeURIComponent(c.username)}'))">历史</button>
      <button class="btn-sm btn-danger" onclick="deleteCustomer(${c.id}, decodeURIComponent('${encodeURIComponent(c.username)}'))">删除</button>
    </td></tr>`).join('') + '</tbody></table>';
}
function createCustomer() {
  $('newUsername').value = '';
  $('newPassword').value = '123456';
  $('newBalanceMinutes').value = '0';
  $('newBalanceRounds').value = '0';
  openModal('addUserModal');
}
async function submitCreateCustomer() {
  const username = $('newUsername').value.trim();
  const password = $('newPassword').value.trim() || '123456';
  const balance_minutes = Number($('newBalanceMinutes').value || 0);
  const balance_rounds = Number($('newBalanceRounds').value || 0);
  if (!username) { toast('请填写用户名'); return; }
  try {
    await api('/api/admin/customers', {method:'POST', body:JSON.stringify({username,password,balance_minutes,balance_rounds})});
    closeModal('addUserModal'); toast('客户创建成功'); await loadCustomers(); await loadOverview();
  } catch(e) { toast(e.message); }
}
function editBalance(id, oldMinutes, oldRounds, username='') {
  $('changeBalUserId').value = id;
  $('changeBalInfo').textContent = `客户：${username || id}`;
  $('changeBalanceMinutes').value = oldMinutes || 0;
  $('changeBalanceRounds').value = oldRounds || 0;
  openModal('changeBalanceModal');
}
async function submitBalance() {
  const id = $('changeBalUserId').value;
  const body = {
    balance_minutes: Number($('changeBalanceMinutes').value || 0),
    balance_rounds: Number($('changeBalanceRounds').value || 0),
  };
  try {
    await api(`/api/admin/customers/${id}/balance`, {method:'PUT', body:JSON.stringify(body)});
    closeModal('changeBalanceModal'); toast('客户余额已更新'); await loadCustomers(); await loadOnline(); await loadOverview();
  } catch(e) { toast(e.message); }
}
function resetPwd(id, username='') {
  $('changePwdUserId').value = id;
  $('changePwdUsername').value = username || id;
  $('changePwdNew').value = '123456';
  openModal('changePwdModal');
}
async function submitResetPwd() {
  const id = $('changePwdUserId').value;
  const password = $('changePwdNew').value.trim();
  if (!password) { toast('请输入新密码'); return; }
  try {
    await api(`/api/admin/customers/${id}/password`, {method:'PUT', body:JSON.stringify({password})});
    closeModal('changePwdModal'); toast('密码已修改');
  } catch(e) { toast(e.message); }
}
async function setCustomerStatus(id, status) {
  const ok = await appConfirm(status === 'frozen' ? '冻结客户' : '解冻客户', status === 'frozen' ? '确认冻结该客户？冻结后会清理客户在线 session。' : '确认解冻该客户？', status === 'frozen' ? 'btn-amber' : 'btn-primary');
  if (!ok) return;
  try {
    await api(`/api/admin/customers/${id}/status`, {method:'PUT', body:JSON.stringify({status})});
    toast('客户状态已更新'); await loadCustomers(); await loadOnline(); await loadOverview();
  } catch(e) { toast(e.message); }
}
async function showCustomerOrders(id, name) {
  try {
    const d = await api('/api/admin/orders?customer_id=' + id);
    $('userOrdersTitle').textContent = `客户历史订单 · ${name || id}`;
    renderOrders(d.orders, 'userOrdersContent');
    openModal('userOrdersModal');
  } catch(e) { toast(e.message); }
}
async function deleteCustomer(id, name='') {
  const ok = await appConfirm('删除客户', `确定删除客户「${name || id}」？\n有进行中订单时会被后端拒绝。`, 'btn-danger');
  if (!ok) return;
  try {
    await api(`/api/admin/customers/${id}`, {method:'DELETE'});
    toast('客户已删除'); await loadCustomers(); await loadOnline(); await loadOverview();
  } catch(e) { toast(e.message); }
}

function cardTypeLabel(t) { return t === 'night' ? '包夜卡' : '普通卡'; }
function cardModeBadge(mode) {
  if (mode === 'absolute') return '<span class="badge badge-absolute">绝密</span>';
  if (mode === 'hybrid') return '<span class="badge badge-purple">混合</span>';
  return '<span class="badge badge-machine">机密</span>';
}
async function loadCards() {
  const q = $('cardSearchInput')?.value || '';
  const st = $('cardStatusFilter')?.value || '';
  const type = $('cardTypeFilter')?.value || '';
  const d = await api('/api/admin/cards?keyword=' + encodeURIComponent(q) + '&status=' + encodeURIComponent(st) + '&type=' + encodeURIComponent(type));
  renderCards(d.cards || []);
}
function renderCards(cards) {
  if (!cards.length) { $('cardsTable').innerHTML = '<div class="empty-state">暂无卡密，点击右上角生成。</div>'; return; }
  $('cardsTable').innerHTML = `<table class="data-table"><thead><tr>
    <th>卡密</th><th>类型/模式</th><th>时长</th><th>局数</th><th>战损</th><th>状态</th><th>使用客户</th><th>创建/使用时间</th><th>操作</th>
  </tr></thead><tbody>` + cards.map(c => {
    const used = !!c.used;
    return `<tr>
      <td style="font-family:Consolas,monospace"><b>${esc(c.card_code)}</b></td>
      <td>${cardModeBadge(c.mode)} <span class="badge ${c.card_type === 'night' ? 'badge-purple' : 'badge-machine'}">${cardTypeLabel(c.card_type)}</span></td>
      <td>${fmtMin(c.minutes)}</td>
      <td>${esc(c.rounds || c.absolute_rounds || 0)}</td>
      <td>${c.night_coin_loss ? esc(c.night_coin_loss) + ' W' : '-'}</td>
      <td><span class="badge ${used ? 'badge-used' : 'badge-unused'}">${used ? '已使用' : '未使用'}</span></td>
      <td>${esc(c.used_by_name || '-')}</td>
      <td>${fmtDate(c.created_at)}<br><span class="hint">${fmtDate(c.used_at)}</span></td>
      <td>${used ? '<span class="hint">不可删除</span>' : `<button class="btn-sm btn-danger" onclick="deleteCard(decodeURIComponent('${encodeURIComponent(c.card_code)}'))">删除</button>`}</td>
    </tr>`;
  }).join('') + '</tbody></table>';
}
function openGenCardModal() {
  $('cardType').value = 'normal';
  $('cardMode').value = 'machine';
  $('cardHours').value = '1';
  $('cardMinutes').value = '0';
  $('cardCount').value = '1';
  $('cardAbsoluteRounds').value = '0';
  $('cardNightHours').value = '8';
  $('cardNightMinutes').value = '0';
  $('cardCountNight').value = '1';
  $('cardNightRounds').value = '0';
  $('cardNightCoinLoss').value = '0';
  $('generatedCards').style.display = 'none';
  $('cardListOutput').innerHTML = '';
  toggleCardTypeFields();
  openModal('genCardModal');
}
function toggleCardTypeFields() {
  const isNight = $('cardType').value === 'night';
  $('normalCardFields').style.display = isNight ? 'none' : '';
  $('nightCardFields').style.display = isNight ? '' : 'none';
  updateCardEstimate();
  toggleNightLossFields();
}
function toggleNightLossFields() {
  const isCoins = $('cardNightLossType')?.value === 'coins';
  if ($('nightRoundsField')) $('nightRoundsField').style.display = isCoins ? 'none' : '';
  if ($('nightCoinsField')) $('nightCoinsField').style.display = isCoins ? '' : 'none';
}
function calculateCardRounds(totalMinutes, mode) {
  const perHour = mode === 'machine' ? Number(_defaultLimitRounds || 0) : Number(_absoluteRoundsPerHour || 0);
  if (!totalMinutes || !perHour) return 0;
  return Math.max(perHour, Math.floor((totalMinutes / 60) * perHour));
}
function updateCardEstimate() {
  if (!$('cardEstimate')) return;
  const minutes = Number($('cardHours').value || 0) * 60 + Number($('cardMinutes').value || 0);
  const mode = $('cardMode').value;
  const override = Number($('cardAbsoluteRounds').value || 0);
  const rounds = override > 0 ? override : calculateCardRounds(minutes, mode);
  $('cardEstimate').textContent = `预计：${fmtMin(minutes)} / ${rounds} 局（默认机密 ${_defaultLimitRounds} 局/时，绝密 ${_absoluteRoundsPerHour} 局/时）`;
}
async function submitGenCards() {
  const isNight = $('cardType').value === 'night';
  const payload = isNight ? {
    card_type: 'night',
    mode: 'machine',
    minutes: Number($('cardNightHours').value || 0) * 60 + Number($('cardNightMinutes').value || 0),
    count: Number($('cardCountNight').value || 1),
    rounds: $('cardNightLossType').value === 'rounds' ? Number($('cardNightRounds').value || 0) : 0,
    night_coin_loss: $('cardNightLossType').value === 'coins' ? Number($('cardNightCoinLoss').value || 0) : 0,
  } : {
    card_type: 'normal',
    mode: $('cardMode').value,
    minutes: Number($('cardHours').value || 0) * 60 + Number($('cardMinutes').value || 0),
    count: Number($('cardCount').value || 1),
    absolute_rounds: Number($('cardAbsoluteRounds').value || 0),
  };
  if (!payload.minutes) { toast('请填写卡密时长'); return; }
  $('submitGenCards').disabled = true;
  try {
    const d = await api('/api/admin/cards/generate', {method:'POST', body:JSON.stringify(payload)});
    const cards = d.cards || [];
    $('generatedCards').style.display = '';
    $('cardListOutput').innerHTML = cards.map(c => `<div class="card-item"><span>${esc(c.card_code)}</span><span>${fmtMin(c.minutes)} / ${esc(c.rounds || 0)}局</span></div>`).join('');
    toast(`已生成 ${cards.length} 张卡密`);
    await loadCards();
  } catch(e) { toast(e.message); }
  finally { $('submitGenCards').disabled = false; }
}
async function copyGeneratedCards() {
  const text = Array.from(document.querySelectorAll('#cardListOutput .card-item span:first-child')).map(x => x.textContent).join('\n');
  if (!text) { toast('没有可复制卡密'); return; }
  try { await navigator.clipboard.writeText(text); }
  catch(_e) {
    const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  toast('卡密已复制');
}
async function deleteCard(code) {
  const ok = await appConfirm('删除充值卡', `确定删除未使用卡密「${code}」？`, 'btn-danger');
  if (!ok) return;
  try {
    await api('/api/admin/cards/' + encodeURIComponent(code), {method:'DELETE'});
    toast('卡密已删除');
    await loadCards();
  } catch(e) { toast(e.message); }
}
function exportUnusedCards() {
  const type = $('cardTypeFilter')?.value || '';
  location.href = '/api/admin/cards/export-unused?type=' + encodeURIComponent(type);
}

async function loadEquipmentConfig() {
  const d = await api('/api/admin/equipment-config');
  _equipmentRows = d.equipment || [];
  $('allowCustomLoadout').checked = !!d.allow_custom_loadout;
  $('maxLoadoutCost').value = d.max_loadout_cost ?? 65;
  renderEquipmentConfig();
}
function renderEquipmentConfig() {
  if (!_equipmentRows.length) { $('equipmentConfigTable').innerHTML = '<div class="empty-state">暂无装备目录</div>'; return; }
  $('equipmentConfigTable').innerHTML = `<table class="data-table"><thead><tr>
    <th>分类</th><th>装备</th><th>价格/W</th><th>启用</th><th>客户端支持</th>
  </tr></thead><tbody>` + _equipmentRows.map((e, idx) => `<tr>
    <td>${esc(e.type_label || e.equipment_type)}</td>
    <td><b>${esc(e.equipment_name)}</b></td>
    <td><input class="config-input" type="number" min="0" value="${esc(e.price || 0)}" onchange="updateEquipmentPrice(${idx}, this.value)"></td>
    <td><label class="switch-line"><input type="checkbox" ${e.enabled ? 'checked' : ''} onchange="updateEquipmentEnabled(${idx}, this.checked)"> 启用</label></td>
    <td>${e.client_supported ? '<span class="badge badge-unused">支持</span>' : '<span class="badge badge-offline">不支持</span>'}</td>
  </tr>`).join('') + '</tbody></table>';
}
function updateEquipmentPrice(idx, value) { if (_equipmentRows[idx]) _equipmentRows[idx].price = Math.max(0, Number(value || 0)); }
function updateEquipmentEnabled(idx, checked) { if (_equipmentRows[idx]) _equipmentRows[idx].enabled = checked ? 1 : 0; }
async function saveEquipmentConfig() {
  try {
    const d = await api('/api/admin/equipment-config', {method:'POST', body:JSON.stringify({
      equipment: _equipmentRows,
      max_loadout_cost: Number($('maxLoadoutCost').value || 0),
      allow_custom_loadout: $('allowCustomLoadout').checked,
    })});
    _equipmentRows = d.equipment || _equipmentRows;
    toast('装备配置已保存');
  } catch(e) { toast(e.message); }
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
  try {
    const d = await api('/api/admin/orders/' + id);
    $('addTimeOrderId').value = id;
    $('addTimeInfo').innerHTML = `订单 #${d.order.id} · 客户 ${esc(d.order.customer_username || d.order.customer_id)} · 当前剩余 ${fmtMin(d.order.remaining_minutes)} · 购买 ${fmtMin(d.order.requested_minutes)}`;
    document.querySelector('input[name="addTimeOp"][value="add"]').checked = true;
    $('addTimeHours').value = 0;
    $('addTimeMinutes').value = 30;
    openModal('addTimeModal');
  } catch(e) { toast(e.message); }
}
async function submitAdjustOrder() {
  const id = $('addTimeOrderId').value;
  const sign = document.querySelector('input[name="addTimeOp"]:checked')?.value === 'sub' ? -1 : 1;
  const minutes = (Number($('addTimeHours').value || 0) * 60 + Number($('addTimeMinutes').value || 0)) * sign;
  if (!minutes) { toast('请填写调整时长'); return; }
  try {
    await api(`/api/admin/orders/${id}/add-time`, {method:'POST', body:JSON.stringify({add_minutes:minutes})});
    closeModal('addTimeModal'); toast('订单时长已调整'); await loadOrders(); await loadOverview();
  } catch(e) { toast(e.message); }
}
async function stopOrder(id) {
  const ok = await appConfirm('停止订单', '确认向中央下发 stop_current 并停止该订单？\n本地订单会进入 stopping，等待中央事件确认。', 'btn-danger');
  if (!ok) return;
  try {
    await api(`/api/admin/orders/${id}/stop`, {method:'POST', body:'{}'});
    toast('已下发停止命令'); await loadOrders(); await loadOverview();
  } catch(e) { toast(e.message); }
}
async function orderDetail(id) {
  const d = await api('/api/admin/orders/' + id);
  const o = d.order;
  $('orderDetailContent').innerHTML = `<table class="data-table"><tbody>
    <tr><th>ID</th><td>${o.id}</td><th>客户</th><td>${esc(o.customer_username || o.customer_id)}</td></tr>
    <tr><th>状态</th><td>${statusBadge(o.status)}</td><th>队伍码</th><td>${esc(o.team_code || '-')}</td></tr>
    <tr><th>购买</th><td>${fmtMin(o.requested_minutes)}</td><th>剩余</th><td>${fmtMin(o.remaining_minutes)}</td></tr>
    <tr><th>开始</th><td>${fmtDate(o.started_at)}</td><th>结束</th><td>${fmtDate(o.end_at)}</td></tr>
    <tr><th>设备</th><td>${esc(o.binding?.device_id || '-')}</td><th>Session</th><td style="font-family:monospace">${esc(o.control_session_id || '-')}</td></tr>
    <tr><th>本地订单号</th><td colspan="3" style="font-family:monospace">${esc(o.local_order_no)}</td></tr>
    <tr><th>失败原因</th><td colspan="3">${esc(o.fail_reason || '-')}</td></tr>
  </tbody></table>`;
  openModal('orderDetailModal');
}
function toggleNightTimeRange() {
  const enabled = document.getElementById('settingNightTimeCheck').checked;
  document.getElementById('nightTimeRangeField').style.display = enabled ? '' : 'none';
}
async function loadSettings() {
  try {
    const d = await api('/api/admin/settings');
    const s = d.settings || {};
    const sysNameInput = document.getElementById('settingSystemName');
    sysNameInput.value = s.system_name || '';
    if (s.system_name_placeholder) {
      sysNameInput.placeholder = '留空则使用默认：' + s.system_name_placeholder;
    }
    _defaultLimitRounds = parseInt(s.default_limit_rounds || '4') || 4;
    _absoluteRoundsPerHour = parseInt(s.absolute_rounds_per_hour || '3') || 3;
    document.getElementById('settingLimitRounds').value = _defaultLimitRounds;
    document.getElementById('settingAbsoluteRoundsPerHour').value = _absoluteRoundsPerHour;
    const nightEnabled = s.night_time_check !== '0' && s.night_time_check !== false;
    document.getElementById('settingNightTimeCheck').checked = nightEnabled;
    document.getElementById('settingNightStartTime').value = s.night_start_time || '22:50';
    document.getElementById('settingNightEndTime').value = s.night_end_time || '06:10';
    const globalRadarInput = document.getElementById('settingGlobalRadarUrl');
    if (s.global_radar_url_editable === false) {
      globalRadarInput.value = '暂无';
      globalRadarInput.readOnly = true;
      globalRadarInput.disabled = true;
      globalRadarInput.dataset.locked = '1';
    } else {
      globalRadarInput.value = s.global_radar_url || 'http://8.148.233.14:5000/';
      globalRadarInput.readOnly = false;
      globalRadarInput.disabled = false;
      globalRadarInput.dataset.locked = '';
    }
    document.getElementById('settingPrivacyMode').checked = s.privacy_mode === '1' || s.privacy_mode_enabled === true;
    document.getElementById('settingPrivacySkipBalance').value = parseInt(s.privacy_skip_balance || '0') || 0;
    document.getElementById('settingAceEnabled').checked = s.ace_enabled === '1' || s.ace_enabled === true;
    const maintCheckbox = document.getElementById('settingMaintenanceMode');
    if (maintCheckbox) maintCheckbox.checked = s.maintenance_mode === '1' || s.maintenance_mode_enabled === true;
    const maintMsgInput = document.getElementById('settingMaintenanceMessage');
    if (maintMsgInput) maintMsgInput.value = s.maintenance_message || '';
    document.getElementById('noticeEditor').innerHTML = s.announcement_text || '';
    toggleNightTimeRange();
    updateCardEstimate();
  } catch(e) { toast(e.message || '加载设置失败'); }
}
function noticeExec(cmd, val) {
  document.getElementById('noticeEditor').focus();
  document.execCommand(cmd, false, val || null);
}
async function saveNotice() {
  const content = document.getElementById('noticeEditor').innerHTML;
  const el = document.getElementById('noticeSaveResult');
  try {
    const data = await api('/api/admin/notice', {method:'POST', body:JSON.stringify({content})});
    el.textContent = data.msg || '保存成功';
    el.style.color = '#22c55e';
    toast('公告已保存');
  } catch(e) {
    el.textContent = e.message || '保存失败';
    el.style.color = '#ef4444';
    toast(e.message || '保存公告失败');
  }
  setTimeout(() => el.textContent = '', 3000);
}
async function saveSettings() {
  const rounds = document.getElementById('settingLimitRounds').value;
  const absoluteRoundsPerHour = document.getElementById('settingAbsoluteRoundsPerHour').value;
  const nightTimeCheck = document.getElementById('settingNightTimeCheck').checked ? '1' : '0';
  const nightStartTime = document.getElementById('settingNightStartTime').value || '22:50';
  const nightEndTime = document.getElementById('settingNightEndTime').value || '06:10';
  const globalRadarInput = document.getElementById('settingGlobalRadarUrl');
  const globalRadarLocked = globalRadarInput.dataset.locked === '1';
  const globalRadarUrl = globalRadarInput.value || '';
  const privacyMode = document.getElementById('settingPrivacyMode').checked ? '1' : '0';
  let privacySkipBalance = parseInt(document.getElementById('settingPrivacySkipBalance').value);
  if (isNaN(privacySkipBalance) || privacySkipBalance < 0) privacySkipBalance = 0;
  const aceEnabled = document.getElementById('settingAceEnabled').checked ? '1' : '0';
  const systemName = (document.getElementById('settingSystemName').value || '').trim();
  const maintCheckbox = document.getElementById('settingMaintenanceMode');
  const maintMsgInput = document.getElementById('settingMaintenanceMessage');
  const maintenanceMode = (maintCheckbox && maintCheckbox.checked) ? '1' : '0';
  const maintenanceMessage = maintMsgInput ? (maintMsgInput.value || '').trim() : '';
  try {
    const payload = {
      default_limit_rounds: rounds,
      absolute_rounds_per_hour: absoluteRoundsPerHour,
      night_time_check: nightTimeCheck,
      night_start_time: nightStartTime,
      night_end_time: nightEndTime,
      privacy_mode: privacyMode,
      privacy_skip_balance: String(privacySkipBalance),
      ace_enabled: aceEnabled,
      system_name: systemName,
      maintenance_mode: maintenanceMode,
      maintenance_message: maintenanceMessage,
    };
    if (!globalRadarLocked) payload.global_radar_url = globalRadarUrl;
    const data = await api('/api/admin/settings', {method:'POST', body:JSON.stringify(payload)});
    _defaultLimitRounds = parseInt(rounds) || _defaultLimitRounds;
    _absoluteRoundsPerHour = parseInt(absoluteRoundsPerHour) || _absoluteRoundsPerHour;
    toast(data.msg || '保存成功');
    await loadOverview();
  } catch(e) { toast(e.message || '网络错误'); }
}
async function loadAll() {
  await loadOverview();
  const [online, orders] = await Promise.all([api('/api/admin/customers?online_only=true'), api('/api/admin/orders')]);
  renderCustomers(online.customers.slice(0, 6), 'overviewOnline');
  renderOrders(orders.orders.filter(o => ['claiming_device','device_claimed','commanding','waiting_ready_timer','running','stopping'].includes(o.status)).slice(0, 6), 'overviewOrders');
}
$('settingNightTimeCheck')?.addEventListener('change', toggleNightTimeRange);
loadAll().then(loadSettings).catch(e => toast(e.message));
</script>
</body>
</html>"""
    return template.replace("__ADMIN__", _escape(admin.get("username"))).replace("__ROLE__", _escape(admin.get("role")))
