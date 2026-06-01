from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import string
import time as time_mod
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from .bridge_client import BridgeClient, BridgeClientError
from .config import Settings, load_settings
from .db import Database, loads, parse_ts
from .service import MerchantError, MerchantService


def json_ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def json_fail(code: str, message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code, "message": message, "msg": message}, status_code=status_code)


def _stored_bridge_config(db: Database, settings: Settings) -> dict[str, str]:
    cfg = {
        "base_url": settings.bridge_base_url,
        "merchant_key": settings.bridge_merchant_key,
        "merchant_secret": settings.bridge_merchant_secret,
    }
    with db.connect() as con:
        rows = con.execute("SELECT key,value_json FROM merchant_settings WHERE key IN ('bridge_base_url','bridge_merchant_key','bridge_merchant_secret','bridge_configured')").fetchall()
    values = {str(r["key"]): loads(r["value_json"], "") for r in rows}
    if values.get("bridge_configured"):
        cfg["base_url"] = str(values.get("bridge_base_url") or cfg["base_url"])
        cfg["merchant_key"] = str(values.get("bridge_merchant_key") or cfg["merchant_key"])
        cfg["merchant_secret"] = str(values.get("bridge_merchant_secret") or cfg["merchant_secret"])
    return cfg


def create_app(*, db_path: str | Path | None = None, bridge_client: Any | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(db_path or settings.db_path)
    bridge_cfg = _stored_bridge_config(db, settings)
    bridge = bridge_client or BridgeClient(bridge_cfg["base_url"], bridge_cfg["merchant_key"], bridge_cfg["merchant_secret"])
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
    rate_buckets: dict[str, list[float]] = {}

    def setup_exempt_path(path: str) -> bool:
        return path in {"/setup", "/api/setup/status", "/api/setup/bridge", "/health", "/favicon.ico"} or path.startswith("/static/")

    def setup_required_effective() -> bool:
        return bool(settings.require_bridge_setup and service.bridge_setup_required())

    def too_many_attempts(key: str, *, limit: int = 40, window_seconds: int = 300) -> bool:
        now = time_mod.monotonic()
        rows = [t for t in rate_buckets.get(key, []) if now - t < window_seconds]
        if len(rows) >= limit:
            rate_buckets[key] = rows
            return True
        rows.append(now)
        rate_buckets[key] = rows
        return False

    def same_origin_request(request: Request) -> bool:
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if not origin:
            return True
        parsed = urlparse(origin)
        if not parsed.scheme or not parsed.netloc:
            return False
        return parsed.scheme == request.url.scheme and parsed.netloc == request.url.netloc

    @app.middleware("http")
    async def hardening_middleware(request: Request, call_next):
        path = request.url.path
        if setup_required_effective() and not setup_exempt_path(path):
            if path.startswith("/api/") or request.method.upper() != "GET":
                return json_fail("setup_required", "请先完成 Bridge API Key 首次配置", 428)
            return RedirectResponse("/setup", status_code=303)
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and (path.startswith("/api/admin/") or path.startswith("/merchant-admin") or path == "/api/setup/bridge"):
            if not same_origin_request(request):
                return json_fail("bad_origin", "请求来源不可信", 403)
        if request.method.upper() == "POST" and path in {"/api/login", "/api/night-login", "/api/register", "/api/admin/login", "/login", "/merchant-admin/login", "/api/setup/bridge"}:
            host = request.client.host if request.client else "unknown"
            if too_many_attempts(f"{host}:{path}", limit=60 if path == "/api/register" else 40, window_seconds=300):
                return json_fail("rate_limited", "请求过于频繁，请稍后再试", 429)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return response

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/static/favicon.svg", include_in_schema=False)
    def static_favicon_svg() -> Response:
        svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='#3b82f6'/><path d='M18 36c7-16 21-16 28 0M22 43h20' stroke='white' stroke-width='5' stroke-linecap='round' fill='none'/></svg>"
        return Response(svg, media_type="image/svg+xml")

    @app.exception_handler(MerchantError)
    async def merchant_error_handler(_request: Request, exc: MerchantError) -> JSONResponse:
        return json_fail(exc.code, exc.message, exc.status_code)

    @app.exception_handler(BridgeClientError)
    async def bridge_error_handler(_request: Request, exc: BridgeClientError) -> JSONResponse:
        return json_fail(exc.code, exc.message, exc.status_code)

    def current_customer(request: Request, response: Response) -> dict[str, Any]:
        customer = service.customer_from_session(request.cookies.get("merchant_session"))
        if not customer:
            raise MerchantError("login_required", "请先登录", 401)
        sid = request.cookies.get("merchant_session")
        if sid:
            set_session_cookie(response, sid)
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

    def require_owner_admin(admin: dict[str, Any]) -> dict[str, Any]:
        if str(admin.get("role") or "") != "owner":
            raise MerchantError("permission_denied", "该操作需要 owner 权限", 403)
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

    def captcha_signature(code: str) -> str:
        return hmac.new(settings.merchant_ref_secret.encode("utf-8"), code.upper().encode("utf-8"), hashlib.sha256).hexdigest()

    def make_captcha_cookie(code: str) -> str:
        return f"{code.upper()}:{captcha_signature(code)}"

    def verify_captcha(request: Request, submitted: Any) -> bool:
        code = str(submitted or "").strip().upper()
        raw = request.cookies.get("merchant_register_captcha") or ""
        if not code or ":" not in raw:
            return False
        expected, sig = raw.split(":", 1)
        expected = expected.strip().upper()
        return bool(expected and hmac.compare_digest(code, expected) and hmac.compare_digest(sig, captcha_signature(expected)))

    def admin_settings_view(st: dict[str, Any]) -> dict[str, Any]:
        out = dict(st)
        out["privacy_mode"] = "1" if truthy(st.get("privacy_mode_enabled")) else "0"
        out["maintenance_mode"] = "1" if truthy(st.get("maintenance_mode_enabled")) else "0"
        out["global_radar_url_editable"] = True
        out["system_name_placeholder"] = st.get("system_name") or "SNOW 自助下单"
        return out

    def setup_config_view() -> dict[str, Any]:
        cfg = service.bridge_config_view()
        raw_required = bool(cfg.get("setup_required"))
        cfg["setup_required_raw"] = raw_required
        cfg["setup_enforced"] = bool(settings.require_bridge_setup)
        cfg["setup_required"] = bool(settings.require_bridge_setup and raw_required)
        cfg["settings"] = admin_settings_view(service.get_settings())
        return cfg

    @app.get("/health")
    def health() -> dict[str, Any]:
        return json_ok(service="merchant_portal", version="0.1.0")

    def customer_home_html(customer: dict[str, Any]) -> HTMLResponse:
        merchant_settings = service.get_settings()
        return HTMLResponse(_legacy_customer_html(customer, merchant_settings))

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        customer = maybe_customer(request)
        if not customer:
            return RedirectResponse("/login", status_code=303)  # type: ignore[return-value]
        return customer_home_html(customer)

    @app.get("/customer", response_class=HTMLResponse)
    def customer_page(request: Request) -> HTMLResponse:
        customer = maybe_customer(request)
        if not customer:
            return RedirectResponse("/login", status_code=303)  # type: ignore[return-value]
        return customer_home_html(customer)

    @app.get("/register", response_class=HTMLResponse)
    def register_page() -> HTMLResponse:
        return HTMLResponse(_legacy_auth_html("register.html", service.get_settings()))

    @app.post("/register")
    def register_form(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        customer = service.register_customer(username, password)
        sid = service.create_session(customer["id"])
        resp = RedirectResponse("/customer", status_code=303)
        set_session_cookie(resp, sid)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return HTMLResponse(_legacy_auth_html("login.html", service.get_settings()))

    @app.post("/login")
    def login_form(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
        customer = service.authenticate(username, password)
        sid = service.create_session(customer["id"])
        service.record_customer_login(customer["id"], source="form_login")
        resp = RedirectResponse("/customer", status_code=303)
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
        if setup_required_effective():
            return RedirectResponse("/setup", status_code=303)  # type: ignore[return-value]
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
        return HTMLResponse(_admin_dashboard_html(admin, service.get_settings()))

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
        require_owner_admin(admin)
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

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request) -> HTMLResponse:
        admin = maybe_admin(request)
        cfg = setup_config_view()
        return HTMLResponse(_setup_html(cfg, require_admin_password=not bool(admin)))

    @app.get("/api/setup/status")
    def api_setup_status() -> dict[str, Any]:
        return json_ok(**setup_config_view())

    @app.post("/api/setup/bridge")
    def api_setup_bridge(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        admin = maybe_admin(request)
        if not admin:
            # First-run setup is still guarded by the local admin password so a
            # random LAN visitor cannot bind the merchant server to their key.
            admin = service.authenticate_admin(body.get("admin_username") or settings.default_admin_username, body.get("admin_password") or "")
        require_owner_admin(admin)
        raw_settings_payload = body.get("settings") if isinstance(body.get("settings"), dict) else body
        settings_payload = normalize_admin_settings_payload(raw_settings_payload)
        if settings_payload:
            service.update_settings(admin["id"], settings_payload)
        base_url = body.get("bridge_base_url") or body.get("base_url") or ""
        merchant_key = body.get("bridge_merchant_key") or body.get("merchant_key") or ""
        merchant_secret = body.get("bridge_merchant_secret") or body.get("merchant_secret") or ""
        has_bridge_credentials = bool(str(merchant_key or "").strip() or str(merchant_secret or "").strip())
        if has_bridge_credentials or setup_required_effective():
            cfg = service.update_bridge_config(
                admin,
                base_url=base_url,
                merchant_key=merchant_key,
                merchant_secret=merchant_secret,
            )
            if bridge_client is None:
                try:
                    if hasattr(service.bridge, "close"):
                        service.bridge.close()
                except Exception:
                    pass
                new_bridge = BridgeClient(cfg["bridge_base_url"], cfg["bridge_merchant_key"], merchant_secret)
                service.bridge = new_bridge
                app.state.bridge = new_bridge
        msg = "首次配置已保存" if not has_bridge_credentials else "全局设置与 Bridge API Key 已保存"
        return json_ok(msg=msg, bridge=setup_config_view(), settings=admin_settings_view(service.get_settings()), redirect="/merchant-admin/login")

    # JSON API
    @app.post("/api/register")
    def api_register(request: Request, response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not verify_captcha(request, body.get("captcha")):
            raise MerchantError("bad_captcha", "验证码错误或已过期", 400)
        customer = service.register_customer(body.get("username") or "", body.get("password") or "")
        sid = service.create_session(customer["id"])
        set_session_cookie(response, sid)
        response.delete_cookie("merchant_register_captcha")
        return json_ok(msg="注册成功", user_id=customer["id"], redirect="/customer", customer=service.get_customer(customer["id"]))

    @app.post("/api/login")
    def api_login(response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        customer = service.authenticate(body.get("username") or "", body.get("password") or "")
        sid = service.create_session(customer["id"])
        service.record_customer_login(customer["id"], source="api_login")
        set_session_cookie(response, sid)
        return json_ok(msg="登录成功", redirect="/customer", role="customer", customer=customer)

    @app.post("/api/logout")
    def api_logout(request: Request, response: Response) -> dict[str, Any]:
        service.delete_session(request.cookies.get("merchant_session") or "")
        clear_session_cookie(response)
        return json_ok(msg="已退出", redirect="/login")

    @app.post("/api/night-login")
    def api_night_login(response: Response, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        customer = service.night_login_card(body.get("card_code") or body.get("code") or "")
        sid = service.create_session(customer["id"])
        service.record_customer_login(customer["id"], source="night_login")
        set_session_cookie(response, sid)
        return json_ok(msg="登录成功", redirect="/customer", role="night_card", customer=customer)

    @app.get("/api/night-config")
    def api_night_config() -> dict[str, Any]:
        merchant_settings = service.get_settings()
        return json_ok(
            enabled=bool(merchant_settings.get("night_time_check")),
            start_time=merchant_settings.get("night_start_time") or "22:50",
            end_time=merchant_settings.get("night_end_time") or "06:10",
        )

    @app.get("/api/captcha")
    def api_captcha() -> Response:
        alphabet = string.ascii_uppercase + string.digits
        code = "".join(secrets.choice(alphabet) for _ in range(4))
        noise = "".join(f"<circle cx='{secrets.randbelow(120)}' cy='{secrets.randbelow(40)}' r='{1 + secrets.randbelow(3)}' fill='#bfdbfe' opacity='.65'/>" for _ in range(10))
        lines = "".join(f"<line x1='{secrets.randbelow(120)}' y1='{secrets.randbelow(40)}' x2='{secrets.randbelow(120)}' y2='{secrets.randbelow(40)}' stroke='#93c5fd' stroke-width='1' opacity='.55'/>" for _ in range(4))
        chars = "".join(
            f"<text x='{18 + i * 22}' y='{25 + secrets.randbelow(7)}' font-size='{20 + secrets.randbelow(4)}' font-family='Consolas,monospace' font-weight='700' fill='#1d4ed8' transform='rotate({secrets.randbelow(17)-8} {18 + i * 22} 22)'>{ch}</text>"
            for i, ch in enumerate(code)
        )
        svg = f"<svg xmlns='http://www.w3.org/2000/svg' width='120' height='40'><rect width='120' height='40' rx='8' fill='#f8fafc'/>{noise}{lines}{chars}</svg>"
        resp = Response(svg, media_type="image/svg+xml")
        resp.set_cookie("merchant_register_captcha", make_captcha_cookie(code), httponly=True, samesite="lax", max_age=300)
        return resp

    @app.get("/api/me")
    def api_me(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(customer=service.get_customer(customer["id"]))

    @app.get("/api/balance")
    def api_balance(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        fresh = service.get_customer(customer["id"])
        role = "night_card" if str(fresh.get("username") or "").startswith("night_") else "customer"
        return json_ok(
            user_id=fresh["id"],
            username=fresh["username"],
            role=role,
            tenant_id=0,
            balance_machine=int(fresh.get("balance_machine_minutes") or 0),
            balance_absolute=int(fresh.get("balance_absolute_minutes") or 0),
            balance_machine_rounds=int(fresh.get("balance_machine_rounds") or 0),
            balance_absolute_rounds=int(fresh.get("balance_absolute_rounds") or 0),
            balance_minutes=int(fresh.get("balance_minutes") or 0),
            balance_rounds=int(fresh.get("balance_rounds") or 0),
        )

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
    def api_capacity(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        status = _legacy_devices_status(service, customer)
        return json_ok(capacity=status["capacity"])

    @app.get("/api/devices/status")
    def api_devices_status(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(**_legacy_devices_status(service, customer))

    @app.get("/api/enabled-equipment")
    def api_enabled_equipment(_customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        cfg = service.get_equipment_config()
        equipment = [e for e in (cfg.get("equipment") or []) if int(e.get("enabled") or 0) == 1]
        return json_ok(
            equipment=equipment,
            supported_equipment=cfg.get("supported_equipment") or [],
            max_loadout_cost=cfg.get("max_loadout_cost", 65),
            allow_custom_loadout=cfg.get("allow_custom_loadout", True),
        )

    def legacy_place_order(request: Request, body: dict[str, Any], customer: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
        fresh = service.get_customer(customer["id"])
        mode = str(body.get("selected_mode") or body.get("mode") or "machine").strip().lower()
        if mode not in {"machine", "absolute"}:
            mode = "machine"
        available_minutes = int((fresh.get("balance_absolute_minutes") if mode == "absolute" else fresh.get("balance_machine_minutes")) or 0)
        available_rounds = int((fresh.get("balance_absolute_rounds") if mode == "absolute" else fresh.get("balance_machine_rounds")) or 0)
        minutes = int(body.get("run_minutes") or body.get("minutes") or body.get("requested_minutes") or available_minutes)
        rounds = int(body.get("run_rounds") or body.get("rounds") or body.get("max_rounds") or available_rounds)
        quality = "secret" if mode == "absolute" else "standard"
        team_code = body.get("boss_name") or body.get("team_code") or ""
        result = service.place_order(
            customer["id"],
            requested_minutes=minutes,
            requested_rounds=rounds,
            team_code=team_code,
            quality=quality,
            idempotency_key=request.headers.get("X-Idempotency-Key"),
        )
        order = result.get("order") or {}
        view = _legacy_order_view(order, service.get_settings())
        return json_ok(
            msg="订单创建成功",
            order_id=view["id"],
            run_minutes=view["run_minutes"],
            run_rounds=view["run_rounds"],
            max_rounds=view["max_rounds"],
            mode=mode,
            end_time=view.get("end_time"),
            end_time_ms=view.get("end_time_ms"),
            enhanced_radar_url=view.get("enhanced_radar_url", ""),
            native_radar_url=view.get("native_radar_url", ""),
            order=view,
            reused=bool(result.get("reused")),
        )

    @app.post("/api/order")
    def api_legacy_order(request: Request, body: dict[str, Any] = Body(...), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return legacy_place_order(request, body, customer, auto=False)

    @app.post("/api/order/auto")
    def api_legacy_order_auto(request: Request, body: dict[str, Any] = Body(...), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return legacy_place_order(request, body, customer, auto=True)

    @app.get("/api/orders/mine")
    def api_legacy_orders_mine(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        settings_view = service.get_settings()
        return json_ok(orders=[_legacy_order_view(o, settings_view) for o in service.order_history(customer["id"], limit=200)])

    @app.post("/api/order/{order_id}/stop")
    def api_legacy_order_stop(order_id: int, customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        stopped = service.customer_stop_order(order_id, customer["id"])
        return json_ok(msg="已下发结束指令，等待设备确认", detail="", order=_legacy_order_view(stopped, service.get_settings()))

    @app.post("/api/order/{order_id}/rejoin")
    def api_legacy_order_rejoin(order_id: int, body: dict[str, Any] = Body(default_factory=dict), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        order = service.customer_rejoin_order(order_id, customer["id"], body.get("boss_name") or body.get("team_code") or "")
        return json_ok(msg="已下发换队", order=_legacy_order_view(order, service.get_settings()))

    @app.post("/api/order/{order_id}/restart_backup")
    def api_legacy_restart_backup(order_id: int, customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        service.assert_customer_order(order_id, customer["id"])
        return json_ok(msg="备用电脑重启指令已记录")

    @app.post("/api/order/{order_id}/switch_spectate")
    def api_legacy_switch_spectate(order_id: int, customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        service.assert_customer_order(order_id, customer["id"])
        return json_ok(msg="已切换观战")

    @app.post("/api/order/{order_id}/switch-device")
    def api_legacy_switch_device(order_id: int, body: dict[str, Any] = Body(default_factory=dict), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        order = service.customer_rejoin_order(order_id, customer["id"], body.get("boss_name") or body.get("team_code") or "")
        return json_ok(msg="已记录换机请求；中央 Bridge 会按当前会话保护策略继续执行", order=_legacy_order_view(order, service.get_settings()))

    @app.post("/api/recharge/redeem")
    def api_recharge(body: dict[str, Any] = Body(...), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(**service.redeem_card(customer["id"], body.get("code") or ""))

    @app.post("/api/recharge")
    def api_legacy_recharge(body: dict[str, Any] = Body(...), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        result = service.redeem_card(customer["id"], body.get("card_code") or body.get("code") or "")
        return json_ok(msg=f"兑换成功：{result.get('minutes', 0)} 分钟 / {result.get('rounds', 0)} 战损", **result)

    @app.get("/api/recharge/orders")
    def api_legacy_recharge_orders(customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        return json_ok(orders=service.recharge_history(customer["id"]))

    @app.post("/api/orders")
    def api_order(request: Request, body: dict[str, Any] = Body(...), customer: dict[str, Any] = Depends(current_customer)) -> dict[str, Any]:
        result = service.place_order(
            customer["id"],
            requested_minutes=int(body.get("requested_minutes") or 0),
            requested_rounds=int(body.get("requested_rounds") or body.get("rounds") or 0),
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
        require_owner_admin(admin)
        return json_ok(settings=admin_settings_view(service.update_settings(admin["id"], normalize_admin_settings_payload(body))))

    @app.post("/api/admin/settings")
    def api_admin_post_settings(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        payload = body.get("settings") if isinstance(body.get("settings"), dict) else body
        return json_ok(msg="保存成功", settings=admin_settings_view(service.update_settings(admin["id"], normalize_admin_settings_payload(payload))))

    @app.post("/api/admin/notice")
    def api_admin_notice(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        content = str(body.get("content") or "")
        service.update_settings(admin["id"], {"announcement_text": content, "announcement_enabled": bool(content.strip())})
        return json_ok(msg="保存成功")

    @app.get("/api/admin/equipment-config")
    def api_admin_equipment_config(_admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(**service.get_equipment_config())

    @app.post("/api/admin/equipment-config")
    def api_admin_equipment_config_save(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        service.update_equipment_config(admin["id"], body)
        return json_ok(msg="保存成功", **service.get_equipment_config())

    @app.get("/api/admin/cards")
    def api_admin_cards(keyword: str = "", status: str = "", type: str = "", _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:  # noqa: A002 - keep legacy query name
        cards = service.list_recharge_cards(keyword=keyword, status=status, card_type=type)
        return json_ok(cards=cards, total=len(cards))

    @app.post("/api/admin/cards/generate")
    def api_admin_cards_generate(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
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
    def api_admin_card_delete(code: str, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
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

    @app.get("/api/admin/activity-stats")
    def api_admin_activity_stats(date: str = "", _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(stats=service.admin_activity_stats(local_date=date or None))

    @app.get("/api/admin/order-analytics")
    def api_admin_order_analytics(period: str = "day", date: str = "", _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(analytics=service.admin_order_analytics(period=period, date=date or None))

    @app.get("/api/admin/customers")
    def api_admin_customers(keyword: str = "", online_only: bool = False, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        customers = service.admin_list_customers(keyword=keyword, online_only=online_only)
        return json_ok(customers=customers, total=len(customers))

    @app.post("/api/admin/customers")
    def api_admin_customer_create(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        customer = service.admin_create_customer(
            username=body.get("username") or "",
            password=body.get("password") or "123456",
            balance_minutes=int(body.get("balance_minutes") or 0),
            balance_rounds=int(body.get("balance_rounds") or 0),
            status=body.get("status") or "active",
        )
        return json_ok(customer=customer)

    @app.put("/api/admin/customers/{customer_id}/balance")
    def api_admin_customer_balance(customer_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        customer = service.admin_update_customer_balance(
            customer_id,
            balance_minutes=body.get("balance_minutes") if "balance_minutes" in body else None,
            balance_rounds=body.get("balance_rounds") if "balance_rounds" in body else None,
            delta_minutes=body.get("delta_minutes") if "delta_minutes" in body else None,
            delta_rounds=body.get("delta_rounds") if "delta_rounds" in body else None,
        )
        return json_ok(customer=customer)

    @app.put("/api/admin/customers/{customer_id}/status")
    def api_admin_customer_status(customer_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(customer=service.admin_set_customer_status(customer_id, body.get("status") or "active"))

    @app.put("/api/admin/customers/{customer_id}/password")
    def api_admin_customer_password(customer_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(**service.admin_reset_customer_password(customer_id, body.get("password") or "123456"))

    @app.delete("/api/admin/customers/{customer_id}")
    def api_admin_customer_delete(customer_id: int, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(**service.admin_delete_customer(customer_id))

    @app.get("/api/admin/orders")
    def api_admin_orders(keyword: str = "", status: str = "", customer_id: int | None = None, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        orders = service.admin_list_orders(keyword=keyword, status=status, customer_id=customer_id)
        return json_ok(orders=orders, total=len(orders))

    @app.get("/api/admin/devices")
    def api_admin_devices(_admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        devices = service.admin_list_devices()
        return json_ok(devices=devices, total=len(devices))

    @app.post("/api/admin/devices")
    def api_admin_device_create(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        device = service.admin_create_device(
            admin,
            device_key=body.get("device_key") or body.get("machine_id") or "",
            device_name=body.get("device_name") or body.get("display_name") or "",
            mode=body.get("mode") or "machine",
            radar_url=body.get("radar_url") or "",
            watchdog_card=body.get("watchdog_card") or "",
        )
        return json_ok(msg="创建设备成功", device=device)

    @app.put("/api/admin/devices/{device_id}")
    def api_admin_device_update(device_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        device = service.admin_update_device(
            admin,
            device_id,
            device_key=body.get("device_key") if "device_key" in body else body.get("machine_id") if "machine_id" in body else None,
            device_name=body.get("device_name") if "device_name" in body else body.get("display_name") if "display_name" in body else None,
            mode=body.get("mode") if "mode" in body else None,
            radar_url=body.get("radar_url") if "radar_url" in body else None,
            watchdog_card=body.get("watchdog_card") if "watchdog_card" in body else None,
            enabled=truthy(body.get("enabled")) if "enabled" in body else None,
        )
        return json_ok(msg="保存成功", device=device)

    @app.put("/api/admin/devices/{device_id}/mode")
    def api_admin_device_mode(device_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="保存成功", device=service.admin_set_device_mode(admin, device_id, body.get("mode") or "machine"))

    @app.put("/api/admin/devices/{device_id}/toggle")
    def api_admin_device_toggle(device_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="保存成功", device=service.admin_set_device_enabled(admin, device_id, truthy(body.get("enabled"))))

    @app.delete("/api/admin/devices/{device_id}")
    def api_admin_device_delete(device_id: int, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="删除成功", **service.admin_delete_device(admin, device_id))

    @app.get("/api/admin/audit-logs")
    def api_admin_audit_logs(limit: int = 200, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        logs = service.admin_audit_logs(limit=limit)
        return json_ok(logs=logs, total=len(logs))

    @app.get("/api/admin/admins")
    def api_admin_admins(_admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        admins = service.admin_list_admins()
        return json_ok(admins=admins, total=len(admins))

    @app.post("/api/admin/admins")
    def api_admin_create_admin(body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        created = service.admin_create_admin(
            admin,
            username=body.get("username") or "",
            password=body.get("password") or "",
            role=body.get("role") or "operator",
            status=body.get("status") or "active",
        )
        return json_ok(msg="管理员已创建", admin=created)

    @app.put("/api/admin/admins/{admin_id}/role")
    def api_admin_set_admin_role(admin_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="管理员权限已更新", admin=service.admin_set_admin_role(admin, admin_id, body.get("role") or "operator"))

    @app.put("/api/admin/admins/{admin_id}/status")
    def api_admin_set_admin_status(admin_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="管理员状态已更新", admin=service.admin_set_admin_status(admin, admin_id, body.get("status") or "active"))

    @app.put("/api/admin/admins/{admin_id}/password")
    def api_admin_reset_admin_password(admin_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="管理员密码已重置", **service.admin_reset_admin_password(admin, admin_id, body.get("password") or ""))

    @app.delete("/api/admin/admins/{admin_id}")
    def api_admin_delete_admin(admin_id: int, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="管理员已删除", **service.admin_delete_admin(admin, admin_id))

    @app.get("/api/admin/backup")
    def api_admin_backup_list(_admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(**service.admin_list_backups())

    @app.post("/api/admin/backup")
    def api_admin_backup_create(admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="备份成功", backup=service.admin_create_backup(admin))

    @app.get("/api/admin/backup/{name}")
    def api_admin_backup_download(name: str, _admin: dict[str, Any] = Depends(current_admin)) -> FileResponse:
        path = service._resolve_backup_path(name)
        return FileResponse(str(path), media_type="application/octet-stream", filename=path.name)

    @app.post("/api/admin/backup/{name}/restore")
    def api_admin_backup_restore(name: str, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(msg="恢复成功；建议重启服务以确保所有连接重新打开", **service.admin_restore_backup(admin, name))

    @app.post("/api/admin/manual-order")
    def api_admin_manual_order(request: Request, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        device_id = int(body.get("device_id") or 0)
        mode = str(body.get("selected_mode") or body.get("mode") or "").strip().lower()
        if not mode and device_id:
            try:
                for d in service.admin_list_devices():
                    if int(d.get("id") or d.get("device_id") or 0) == device_id:
                        mode = str(d.get("mode") or d.get("device_mode") or d.get("quality") or "").strip().lower()
                        break
            except Exception:
                mode = ""
        if mode == "hybrid":
            mode = "machine"
        quality = body.get("quality") or ("secret" if mode in {"absolute", "secret", "绝密"} else "standard")
        order = service.admin_manual_order(
            admin,
            device_id=device_id,
            requested_minutes=int(body.get("run_minutes") or body.get("requested_minutes") or body.get("minutes") or 0),
            requested_rounds=int(body.get("run_rounds") or body.get("rounds") or body.get("max_rounds") or 0),
            max_coin_loss=int(body.get("max_coin_loss") or 0),
            team_code=body.get("boss_name") or body.get("team_code") or "",
            quality=quality,
            loadout=service._manual_loadout_from_payload(body),
        )
        view = _legacy_order_view(order, service.get_settings())
        return json_ok(msg="手动下单成功", order=order, order_id=order["id"], run_minutes=view["run_minutes"], run_rounds=view["run_rounds"], max_rounds=view["max_rounds"])

    @app.post("/api/admin/manual-rejoin/{order_id}")
    def api_admin_manual_rejoin(order_id: int, body: dict[str, Any] = Body(default_factory=dict), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        order = service.admin_rejoin_order(admin, order_id, body.get("boss_name") or body.get("team_code") or "")
        return json_ok(msg="管理员换队指令已下发", order=order)

    @app.post("/api/admin/devices/{device_id}/command")
    def api_admin_device_command(device_id: int, body: dict[str, Any] = Body(default_factory=dict), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        result = service.admin_device_command(admin, device_id, action=body.get("action") or "", params=body.get("params") if isinstance(body.get("params"), dict) else {})
        return json_ok(msg="设备指令已下发", **result)

    @app.post("/api/admin/devices/{device_id}/restart_backup")
    def api_admin_device_restart_backup(device_id: int, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        result = service.admin_device_command(admin, device_id, action="restart_backup", params={"operator": "merchant_admin"})
        return json_ok(msg="已发送备用机重启指令", **result)

    @app.post("/api/admin/machines/{device_key}/restart")
    def api_admin_machine_restart(device_key: str, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        result = service.admin_device_command(admin, service._resolve_admin_device_id(device_key), action="restart", params={"operator": "merchant_admin"})
        return json_ok(msg="已发送重启指令", **result)

    @app.post("/api/admin/machines/{device_key}/update")
    def api_admin_machine_update(device_key: str, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        result = service.admin_device_command(admin, service._resolve_admin_device_id(device_key), action="update", params={"operator": "merchant_admin"})
        return json_ok(msg="已发送更新指令", **result)

    @app.post("/api/admin/machines/{device_key}/collect_log")
    def api_admin_machine_collect_log(device_key: str, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        result = service.admin_device_command(admin, service._resolve_admin_device_id(device_key), action="collect_log", params={"operator": "merchant_admin"})
        return json_ok(msg="已发送日志回收指令", **result)

    @app.get("/api/admin/orders/{order_id}")
    def api_admin_order_detail(order_id: int, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        return json_ok(order=service.admin_get_order(order_id))

    @app.get("/api/admin/orders/{order_id}/detail")
    def api_admin_order_detail_legacy(order_id: int, _admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        order = service.admin_get_order(order_id)
        return json_ok(detail=order, order=order, matches=[], matches_summary={"count": 0})

    @app.post("/api/admin/orders/{order_id}/add-time")
    def api_admin_order_add_time(order_id: int, body: dict[str, Any] = Body(...), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        return json_ok(order=service.admin_adjust_order_time(order_id, add_minutes=int(body.get("add_minutes") or 0)))

    @app.post("/api/admin/add-time/{order_id}")
    def api_admin_add_time_legacy(order_id: int, body: dict[str, Any] = Body(default_factory=dict), admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
        add_minutes = int(body.get("add_minutes") or 0)
        if not add_minutes:
            sign = -1 if str(body.get("op") or body.get("operation") or "").lower() in {"sub", "minus", "subtract"} else 1
            add_minutes = sign * (int(body.get("hours") or 0) * 60 + int(body.get("minutes") or body.get("add_minutes_abs") or 0))
        return json_ok(order=service.admin_adjust_order_time(order_id, add_minutes=add_minutes), msg="订单时长已调整")

    @app.post("/api/admin/orders/{order_id}/stop")
    def api_admin_order_stop(order_id: int, admin: dict[str, Any] = Depends(current_admin)) -> dict[str, Any]:
        require_owner_admin(admin)
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


def _setup_html(cfg: dict[str, Any], *, require_admin_password: bool = True) -> str:
    st = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}

    def checked(key: str) -> str:
        value = st.get(key)
        truth = value is True or str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
        return "checked" if truth else ""

    admin_fields = """
      <label>管理员用户名<input id="adminUsername" value="admin" autocomplete="username"></label>
      <label>管理员密码<input id="adminPassword" type="password" autocomplete="current-password" placeholder="输入本地管理员密码"></label>
    """ if require_admin_password else "<div class='hint'>已登录管理员，会直接保存到本地配置。</div>"
    skip_button = "" if cfg.get("setup_enforced") else """<button class="btn-secondary" onclick="location.href='/merchant-admin/login'">测试期跳过 API Key，进入后台登录</button>"""
    setup_note = "正式模式：必须填入 Bridge API Key 后才能进入业务页面。" if cfg.get("setup_enforced") else "测试期间：当前不强制填写 API Key；可先保存全局设置，Bridge Key/Secret 留空。"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>首次配置 Bridge API Key / 全局设置</title>
    <style>
    *{{box-sizing:border-box}} body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:#eef2ff;min-height:100vh;margin:0;color:#111827;padding:28px}}
    .card{{width:min(980px,96vw);background:#fff;border-radius:18px;box-shadow:0 24px 80px rgba(30,64,175,.18);padding:28px;margin:0 auto}}
    h1{{margin:0 0 8px;font-size:22px}} h2{{font-size:16px;margin:18px 0 10px}} p{{color:#64748b;line-height:1.7}} label{{display:block;font-size:13px;font-weight:700;margin:10px 0 6px;color:#374151}}
    input,textarea,select{{width:100%;border:1px solid #cbd5e1;border-radius:10px;padding:0 12px;font:inherit;background:#fff}} input,select{{height:42px}} textarea{{min-height:84px;padding-top:10px;resize:vertical}}
    .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 16px}} .full{{grid-column:1/-1}} .switch{{display:flex;align-items:center;gap:8px;margin:10px 0;color:#374151;font-weight:700}} .switch input{{width:auto;height:auto}}
    .section{{border:1px solid #e5e7eb;border-radius:14px;padding:16px;margin-top:14px;background:#fbfdff}}
    button{{margin-top:18px;width:100%;height:44px;border:0;border-radius:10px;background:#2563eb;color:#fff;font-weight:800;cursor:pointer}} .btn-secondary{{background:#64748b}}
    .hint{{font-size:12px;color:#64748b;margin-top:8px;line-height:1.6}} .banner{{background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;padding:10px;border-radius:10px;margin:10px 0}} .ok{{background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;padding:10px;border-radius:10px;margin:10px 0}}
    .err{{background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;padding:10px;border-radius:10px;margin:10px 0}}
    @media(max-width:760px){{.grid{{grid-template-columns:1fr}} body{{padding:12px}}}}
    </style></head><body><div class="card">
      <h1>{_escape(st.get("system_name") or cfg.get("system_name") or "商户服务器")} · 首次配置 Bridge API Key / 全局设置</h1>
      <div class="banner">{_escape(setup_note)}</div>
      <div id="msg"></div>
      <div class="section">
        <h2>本地管理员验证</h2>
        {admin_fields}
      </div>
      <div class="section">
        <h2>中央 Bridge / API Key 地址</h2>
        <div class="grid">
          <div class="full"><label>中央 Bridge 地址 / API Key 填入地址<input id="bridgeBaseUrl" value="{_escape(cfg.get("bridge_base_url") or "http://127.0.0.1:8010")}" placeholder="http://127.0.0.1:8010"></label></div>
          <div><label>Merchant Key<input id="bridgeKey" value="{_escape(cfg.get("bridge_merchant_key") if cfg.get("bridge_merchant_key") != "mk_test" else "")}" placeholder="mk_xxx"></label></div>
          <div><label>Merchant Secret<input id="bridgeSecret" type="password" placeholder="中央生成的 Secret；测试期可留空"></label></div>
        </div>
        <div class="hint">Secret 保存后不会在界面回显；测试期留空时不会更新 Bridge 配置。</div>
      </div>
      <div class="section">
        <h2>全局设置</h2>
        <div class="grid">
          <div class="full"><label>前台名称显示<input id="settingSystemName" value="{_escape(st.get("system_name") or "")}" placeholder="例如：七元电竞"></label></div>
          <div><label>默认机密局数/小时<input id="settingLimitRounds" type="number" min="0" value="{_escape(st.get("default_limit_rounds") or 4)}"></label></div>
          <div><label>绝密局数/小时<input id="settingAbsoluteRoundsPerHour" type="number" min="0" value="{_escape(st.get("absolute_rounds_per_hour") or 3)}"></label></div>
          <div><label>包夜开始时间<input id="settingNightStartTime" type="time" value="{_escape(st.get("night_start_time") or "22:50")}"></label></div>
          <div><label>包夜结束时间<input id="settingNightEndTime" type="time" value="{_escape(st.get("night_end_time") or "06:10")}"></label></div>
          <div><label>隐私模式跳过余额阈值<input id="settingPrivacySkipBalance" type="number" min="0" value="{_escape(st.get("privacy_skip_balance") or 0)}"></label></div>
          <div><label>绝密最大配装价值 W<input id="settingMaxLoadoutCost" type="number" min="0" value="{_escape(st.get("max_loadout_cost") or 65)}"></label></div>
          <div class="full"><label>全局雷达/备注地址<input id="settingGlobalRadarUrl" value="{_escape(st.get("global_radar_url") or "")}" placeholder="可留空"></label></div>
          <div class="full"><label>维护文案<textarea id="settingMaintenanceMessage" placeholder="维护时展示给客户">{_escape(st.get("maintenance_message") or "")}</textarea></label></div>
          <div class="full"><label>公告内容<textarea id="settingAnnouncementText" placeholder="前台公告">{_escape(st.get("announcement_text") or "")}</textarea></label></div>
        </div>
        <label class="switch"><input id="settingNightTimeCheck" type="checkbox" {checked("night_time_check")}> 启用包夜卡登录时间限制</label>
        <label class="switch"><input id="settingPrivacyMode" type="checkbox" {checked("privacy_mode_enabled")}> 启用隐私模式</label>
        <label class="switch"><input id="settingAceEnabled" type="checkbox" {checked("ace_enabled")}> 启用 ACE/白嫖检测</label>
        <label class="switch"><input id="settingAllowCustomLoadout" type="checkbox" {checked("allow_custom_loadout")}> 允许客户自定义绝密配装</label>
        <label class="switch"><input id="settingMaintenanceMode" type="checkbox" {checked("maintenance_mode_enabled")}> 平台维护模式</label>
        <label class="switch"><input id="settingAnnouncementEnabled" type="checkbox" {checked("announcement_enabled")}> 启用公告栏</label>
      </div>
      <button onclick="save()">保存全局设置 / API Key</button>
      {skip_button}
    </div><script>
    const $ = id => document.getElementById(id);
    async function save(){{
      const payload={{
        admin_username: $('adminUsername')?.value || '',
        admin_password: $('adminPassword')?.value || '',
        bridge_base_url: $('bridgeBaseUrl').value.trim(),
        bridge_merchant_key: $('bridgeKey').value.trim(),
        bridge_merchant_secret: $('bridgeSecret').value.trim(),
        settings: {{
          system_name: $('settingSystemName').value.trim(),
          default_limit_rounds: Number($('settingLimitRounds').value || 0),
          absolute_rounds_per_hour: Number($('settingAbsoluteRoundsPerHour').value || 0),
          night_time_check: $('settingNightTimeCheck').checked,
          night_start_time: $('settingNightStartTime').value,
          night_end_time: $('settingNightEndTime').value,
          global_radar_url: $('settingGlobalRadarUrl').value.trim(),
          privacy_mode_enabled: $('settingPrivacyMode').checked,
          privacy_skip_balance: Number($('settingPrivacySkipBalance').value || 0),
          ace_enabled: $('settingAceEnabled').checked,
          maintenance_mode_enabled: $('settingMaintenanceMode').checked,
          maintenance_message: $('settingMaintenanceMessage').value,
          announcement_enabled: $('settingAnnouncementEnabled').checked,
          announcement_text: $('settingAnnouncementText').value,
          max_loadout_cost: Number($('settingMaxLoadoutCost').value || 0),
          allow_custom_loadout: $('settingAllowCustomLoadout').checked
        }}
      }};
      const msg=document.getElementById('msg'); msg.className=''; msg.textContent='保存中...';
      const res=await fetch('/api/setup/bridge',{{method:'POST',headers:{{'Content-Type':'application/json'}},credentials:'same-origin',body:JSON.stringify(payload)}});
      const data=await res.json().catch(()=>({{ok:false,message:'响应解析失败'}}));
      if(!res.ok||data.ok===false){{msg.className='err'; msg.textContent=data.message||data.error||'保存失败'; return;}}
      msg.className='ok'; msg.textContent=data.msg || '保存成功，正在跳转后台登录...';
      setTimeout(()=>location.href=data.redirect||'/merchant-admin/login',700);
    }}
    </script></body></html>"""


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


_LEGACY_TEMPLATE_DIR = Path(__file__).with_name("legacy")
_LEGACY_ACTIVE_STATUSES = {
    "created",
    "paid",
    "claiming_device",
    "device_claimed",
    "commanding",
    "waiting_ready_timer",
    "running",
    "stopping",
    "refunding",
}


def _legacy_template(name: str) -> str:
    return (_LEGACY_TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _legacy_system_name(settings: dict[str, Any]) -> str:
    return _escape(settings.get("system_name") or "SNOW 自助下单")


def _legacy_auth_html(name: str, settings: dict[str, Any]) -> str:
    system_name = _legacy_system_name(settings)
    html = _legacy_template(name)
    html = html.replace("粥粥宇电竞", system_name).replace("瑶光电竞", system_name)
    html = html.replace("const tenantId = 5782;", "const tenantId = 0;")
    html = html.replace("const tenantId = 0;", "const tenantId = 0;")
    return html


def _legacy_customer_html(customer: dict[str, Any], settings: dict[str, Any]) -> str:
    html = _legacy_auth_html("customer.html", settings)
    html = html.replace("const CURRENT_USER_ID = 5827;", f"const CURRENT_USER_ID = {int(customer.get('id') or 0)};")
    html = html.replace("const CURRENT_TENANT_ID = 0;", "const CURRENT_TENANT_ID = 0;")
    hidden = (
        f'<span id="serverEscapedUsername" style="display:none">{_escape(customer.get("username") or "")}</span>'
        f'<span id="serverEscapedBalance" style="display:none">{int(customer.get("balance_minutes") or 0)}</span>'
    )
    return html.replace("</body>", hidden + "\n</body>")


def _dt_to_epoch_ms(value: Any) -> int:
    dt = parse_ts(str(value)) if value else None
    return int(dt.timestamp() * 1000) if dt else 0


def _legacy_order_mode(order: dict[str, Any]) -> str:
    q = str(order.get("quality") or "").lower()
    if q in {"absolute", "secret", "绝密"}:
        return "absolute"
    return "machine"


def _legacy_order_status(status: Any) -> str:
    s = str(status or "")
    if s in _LEGACY_ACTIVE_STATUSES:
        return "running"
    if s in {"finished", "completed", "refunded"}:
        return "completed"
    return "failed"


def _legacy_order_view(order: dict[str, Any] | None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    order = dict(order or {})
    binding = order.get("binding") if isinstance(order.get("binding"), dict) else {}
    device_id = int((binding or {}).get("device_id") or 0)
    end_ms = _dt_to_epoch_ms(order.get("end_at"))
    started = parse_ts(str(order.get("started_at"))) if order.get("started_at") else None
    finished = parse_ts(str(order.get("finished_at"))) if order.get("finished_at") else None
    requested_minutes = int(order.get("requested_minutes") or 0)
    actual_minutes = requested_minutes
    if started and finished:
        actual_minutes = max(0, int((finished - started).total_seconds() // 60))
    mode = _legacy_order_mode(order)
    global_radar_url = str((settings or {}).get("global_radar_url") or "")
    return {
        "id": int(order.get("id") or 0),
        "device_id": device_id,
        "device_name": f"{device_id}号机" if device_id else "--",
        "boss_name": order.get("team_code") or "",
        "team_code": order.get("team_code") or "",
        "mode": mode,
        "status": _legacy_order_status(order.get("status")),
        "raw_status": order.get("status") or "",
        "run_minutes": requested_minutes,
        "run_rounds": int(order.get("requested_rounds") or 0),
        "max_rounds": int(order.get("requested_rounds") or 0),
        "actual_minutes": actual_minutes,
        "refund_minutes": int(order.get("refund_minutes") or 0),
        "refund_rounds": int(order.get("refund_rounds") or 0),
        "round_count": int(order.get("round_count") or 0),
        "created_at": order.get("created_at") or "",
        "started_at": order.get("started_at") or "",
        "finished_at": order.get("finished_at") or "",
        "end_time": int(end_ms / 1000) if end_ms else 0,
        "end_time_ms": end_ms,
        "remaining_seconds": int(order.get("remaining_seconds") or 0),
        "remaining_minutes": int(order.get("remaining_minutes") or 0),
        "enhanced_radar_url": global_radar_url,
        "native_radar_url": "",
        "device_work_status": "执行中",
        "watchdog": None,
    }


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _legacy_mode_label(mode: Any) -> str:
    mode_s = str(mode or "machine")
    if mode_s == "absolute":
        return "绝密"
    if mode_s == "hybrid":
        return "混合"
    return "机密"


def _legacy_epoch_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value / 1000) if value > 100000000000 else int(value)
    parsed = parse_ts(str(value)) if value else None
    return int(parsed.timestamp()) if parsed else 0


def _legacy_epoch_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value if value > 100000000000 else value * 1000)
    parsed = parse_ts(str(value)) if value else None
    return int(parsed.timestamp() * 1000) if parsed else 0


def _legacy_available_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out: list[dict[str, Any]] = []
    for d in devices:
        if d.get("enabled") is False:
            continue
        if not d.get("online"):
            continue
        if d.get("running_order_id"):
            continue
        if str(d.get("work_status") or "") != "空闲":
            continue
        cooldown = _to_int(d.get("cooldown_until_ms"), 0)
        if cooldown and cooldown > now_ms:
            continue
        out.append(d)
    return out


def _capacity_from_legacy_devices(devices: list[dict[str, Any]]) -> dict[str, Any]:
    enabled = [d for d in devices if d.get("enabled") is not False]
    idle = _legacy_available_devices(enabled)
    label = "many" if len(idle) >= 3 else ("few" if idle else "full")
    text = {"many": "空闲较多", "few": "空闲较少", "full": "满机"}[label]
    earliest = 0
    for d in enabled:
        end_ms = _to_int(d.get("end_time_ms"), 0)
        if end_ms > int(datetime.now(timezone.utc).timestamp() * 1000):
            earliest = end_ms if not earliest else min(earliest, end_ms)
    return {
        "available": bool(idle),
        "capacity_label": label,
        "capacity_text": text,
        "idle_count": len(idle),
        "total_count": len(enabled),
        "idle_device_ids": [int(d.get("id") or 0) for d in idle if int(d.get("id") or 0) > 0],
        "full_hint": "" if idle else ("距离最近下机还有一段时间" if earliest else "当前无空闲机器"),
        "earliest_end_time_ms": earliest,
    }


def _legacy_devices_status(service: Any, customer: dict[str, Any]) -> dict[str, Any]:
    settings = service.get_settings()
    devices: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    try:
        admin_devices = service.admin_list_devices()
    except Exception:
        admin_devices = []
    if admin_devices:
        for d in admin_devices:
            if d.get("enabled") is False:
                continue
            did = int(d.get("id") or d.get("device_id") or 0)
            if did <= 0:
                continue
            mode = d.get("mode") or d.get("device_mode") or "machine"
            work_status = str(d.get("work_status") or ("空闲" if d.get("online") else "离线"))
            end_ms = _to_int(d.get("end_time_ms"), 0) or _legacy_epoch_ms(d.get("end_time"))
            remaining_minutes = _to_int(d.get("remaining_minutes"), 0)
            remaining_seconds = _to_int(d.get("remaining_seconds"), remaining_minutes * 60)
            devices.append({
                "id": did,
                "name": d.get("device_name") or d.get("name") or f"{did}号机",
                "device_name": d.get("device_name") or d.get("name") or f"{did}号机",
                "device_key": d.get("device_key") or d.get("machine_id") or "",
                "machine_id": d.get("machine_id") or d.get("device_key") or "",
                "mode": mode,
                "mode_label": _legacy_mode_label(mode),
                "enabled": d.get("enabled") is not False,
                "work_status": work_status,
                "running_order_id": d.get("running_order_id"),
                "running_user_id": d.get("running_user_id"),
                "running_user": d.get("running_user") or d.get("active_customer") or "",
                "running_boss_name": d.get("running_boss_name") or d.get("team_code") or d.get("boss_name") or "",
                "remaining_minutes": remaining_minutes,
                "remaining_seconds": remaining_seconds,
                "end_time": _legacy_epoch_seconds(d.get("end_time")) or int(end_ms / 1000) if end_ms else 0,
                "end_time_ms": end_ms,
                "cooldown_until_ms": _to_int(d.get("cooldown_until_ms"), 0),
                "harvard": d.get("harvard") or "",
                "hfb_value": _to_int(d.get("hfb_value"), 0),
                "spectate_boss": d.get("spectate_boss") or d.get("boss_id") or "",
                "boss_id_debug": d.get("boss_id_debug") or "",
                "round_count": _to_int(d.get("round_count"), 0),
                "run_rounds": _to_int(d.get("run_rounds"), 0),
                "max_rounds": _to_int(d.get("max_rounds"), 0),
                "radar_url": d.get("radar_url") or settings.get("global_radar_url") or "",
                "online": bool(d.get("online")),
                "last_heartbeat_at": d.get("last_heartbeat_at") or "",
                "heartbeat_age_seconds": d.get("heartbeat_age_seconds"),
                "state": d.get("state") or d.get("agent_state") or ("idle" if work_status == "空闲" else work_status),
                "sub_state": d.get("sub_state") or "",
                "work_status_detail": d.get("work_status_detail") or work_status,
                "current_map": d.get("current_map") or "",
                "prison_stage": d.get("prison_stage") or "",
                "prison_stage_label": d.get("prison_stage_label") or "",
                "prison_point": d.get("prison_point") or "",
                "prison_action": d.get("prison_action") or "",
                "prison_score": d.get("prison_score"),
                "prison_match": d.get("prison_match") or "",
                "prison_region": d.get("prison_region") or "",
            })
        devices.sort(key=lambda d: (0 if d.get("running_user_id") == customer.get("id") else 1, 0 if d.get("work_status") == "空闲" else 1, int(d.get("id") or 0)))
        capacity = _capacity_from_legacy_devices(devices)
        return {
            "devices": devices,
            "capacity": capacity,
            "privacy_mode": bool(settings.get("privacy_mode_enabled")),
            "privacy_skip_balance": max(0, int(settings.get("privacy_skip_balance") or 0)),
            "maintenance_mode": bool(settings.get("maintenance_mode_enabled")),
            "maintenance_message": settings.get("maintenance_message") or "系统正在维护中，暂停接受新订单。请稍后再试。" if settings.get("maintenance_mode_enabled") else "",
            "server_time_ms": now_ms,
        }

    try:
        orders = service.admin_list_orders(limit=1000)
    except TypeError:
        orders = service.admin_list_orders()
    for order in orders:
        if str(order.get("status") or "") not in _LEGACY_ACTIVE_STATUSES:
            continue
        view = _legacy_order_view(order, settings)
        did = int(view.get("device_id") or 0)
        if did <= 0:
            continue
        used_ids.add(did)
        status = "游戏中" if str(order.get("status")) == "running" else "执行中"
        end_ms = int(view.get("end_time_ms") or 0)
        devices.append({
            "id": did,
            "name": f"{did}号机",
            "device_name": f"{did}号机",
            "mode": view["mode"],
            "mode_label": "绝密" if view["mode"] == "absolute" else "机密",
            "enabled": True,
            "work_status": status,
            "running_order_id": int(order.get("id") or 0),
            "running_user_id": int(order.get("customer_id") or 0),
            "running_user": order.get("customer_username") or "",
            "running_boss_name": order.get("team_code") or "",
            "remaining_minutes": int(order.get("remaining_minutes") or 0),
            "remaining_seconds": int(order.get("remaining_seconds") or 0),
            "end_time": int(end_ms / 1000) if end_ms else 0,
            "end_time_ms": end_ms,
            "cooldown_until_ms": 0,
            "harvard": "999W",
            "hfb_value": 9990000,
            "spectate_boss": order.get("team_code") or "",
            "round_count": 0,
            "run_rounds": int(order.get("requested_rounds") or 0),
            "max_rounds": int(order.get("requested_rounds") or 0),
            "radar_url": settings.get("global_radar_url") or "",
            "online": True,
            "state": "running",
            "sub_state": "",
            "work_status_detail": status,
        })

    idle_ids: list[int] = []
    try:
        cap = service.bridge.get_capacity()
        raw_ids = cap.get("idle_device_ids") or cap.get("idle_devices") or cap.get("device_ids") or []
        for item in raw_ids:
            try:
                idle_ids.append(int(item.get("id") if isinstance(item, dict) else item))
            except Exception:
                continue
        if not idle_ids and cap.get("available"):
            count = int(cap.get("idle_count") or cap.get("available_count") or cap.get("count") or 1)
            idle_ids = list(range(1, max(1, min(count, 20)) + 1))
    except Exception:
        idle_ids = []

    for did in idle_ids:
        if did in used_ids:
            continue
        devices.append({
            "id": did,
            "name": f"{did}号机",
            "device_name": f"{did}号机",
            "mode": "hybrid",
            "mode_label": "混合",
            "enabled": True,
            "work_status": "空闲",
            "running_order_id": None,
            "running_user_id": None,
            "running_user": "",
            "running_boss_name": "",
            "remaining_minutes": 0,
            "remaining_seconds": 0,
            "end_time": 0,
            "end_time_ms": 0,
            "cooldown_until_ms": 0,
            "harvard": "999W",
            "hfb_value": 9990000,
            "spectate_boss": "",
            "round_count": 0,
            "run_rounds": 0,
            "max_rounds": 0,
            "radar_url": settings.get("global_radar_url") or "",
            "online": True,
            "state": "idle",
            "sub_state": "",
            "work_status_detail": "空闲",
        })

    devices.sort(key=lambda d: (0 if d.get("running_user_id") == customer.get("id") else 1, 0 if d.get("work_status") == "空闲" else 1, int(d.get("id") or 0)))
    capacity = _capacity_from_legacy_devices(devices)
    return {
        "devices": devices,
        "capacity": capacity,
        "privacy_mode": bool(settings.get("privacy_mode_enabled")),
        "privacy_skip_balance": max(0, int(settings.get("privacy_skip_balance") or 0)),
        "maintenance_mode": bool(settings.get("maintenance_mode_enabled")),
        "maintenance_message": settings.get("maintenance_message") or "系统正在维护中，暂停接受新订单。请稍后再试。" if settings.get("maintenance_mode_enabled") else "",
        "server_time_ms": now_ms,
    }


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


def _admin_dashboard_html(admin: dict[str, Any], settings: dict[str, Any] | None = None) -> str:
    system_name = _escape((settings or {}).get("system_name") or "SNOW 自助下单")
    template = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>__SYSTEM_NAME__ 商户后台</title>
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
    .dropdown { position: relative; display: inline-block; }
    .dropdown-menu { display: none; position: absolute; right: 0; top: 100%; background:#fff; border:1px solid #e5e7eb; border-radius:8px; box-shadow:0 4px 12px rgba(0,0,0,.1); min-width:120px; z-index:50; padding:4px 0; }
    .dropdown-menu.up { top:auto; bottom:100%; box-shadow:0 -4px 12px rgba(0,0,0,.1); }
    .dropdown.open .dropdown-menu { display:block; }
    .dropdown-item { display:block; width:100%; padding:8px 14px; border:none; background:none; text-align:left; font-size:12px; cursor:pointer; color:#374151; white-space:nowrap; }
    .dropdown-item:hover { background:#f3f4f6; }
    .dropdown-item.text-danger { color:#ef4444; }
    .dropdown-item.text-danger:hover { background:#fef2f2; }
    .grid { display:grid; gap:14px; }
    .stats-grid { grid-template-columns: repeat(6, minmax(150px, 1fr)); }
    .stat-card { background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
    .stat-label { font-size:12px; color:#6b7280; margin-bottom:8px; }
    .stat-value { font-size:26px; font-weight:800; color:#111827; }
    .panel { background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
    table.data-table { width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.04); }
    #devicesTable, #devicesTable .data-table, #devicesTable .data-table tbody, #devicesTable .data-table tr, #devicesTable .data-table td { overflow: visible; }
    #devicesTable .dropdown-menu { z-index: 3000; }
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
    .bar-list { display:flex; flex-direction:column; gap:8px; margin-top:10px; }
    .bar-row { display:grid; grid-template-columns:110px 1fr 100px; gap:8px; align-items:center; font-size:12px; }
    .bar-track { height:10px; background:#e5e7eb; border-radius:999px; overflow:hidden; }
    .bar-fill { height:100%; background:linear-gradient(90deg,#3b82f6,#22c55e); border-radius:999px; min-width:2px; }
    .toast { position: fixed; right: 24px; bottom: 24px; background: #111827; color: #fff; padding: 10px 14px; border-radius: 9px; box-shadow: 0 12px 30px rgba(0,0,0,.22); z-index: 1200; opacity: 0; transform: translateY(8px); transition: all .18s; }
    .toast.show { opacity: 1; transform: none; }
    @media (max-width: 980px) { .stats-grid { grid-template-columns: repeat(2, minmax(0,1fr)); } .content{padding:14px} .data-table{display:block;overflow:auto} .settings-row{grid-template-columns:1fr} }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-logo">__SYSTEM_NAME__ · 商户管理后台</div>
    <div class="topbar-right">
      <span>__ADMIN__ / __ROLE__</span>
      <button class="btn-sm btn-gray" onclick="location.href='/'">客户首页</button>
      <form method="post" action="/merchant-admin/logout" style="display:inline"><button class="btn-sm btn-danger">退出</button></form>
    </div>
  </div>
  <div class="nav-tabs">
    <div class="nav-tab active" data-tab="overview">今日总览</div>
    <div class="nav-tab" data-tab="analytics">订单分析</div>
    <div class="nav-tab" data-tab="online">在线客户</div>
    <div class="nav-tab" data-tab="devices">设备直控</div>
    <div class="nav-tab" data-tab="customers">客户管理</div>
    <div class="nav-tab" data-tab="cards">充值卡</div>
    <div class="nav-tab" data-tab="equipment">装备配置</div>
    <div class="nav-tab" data-tab="orders">订单管理</div>
    <div class="nav-tab" data-tab="admins">管理员</div>
    <div class="nav-tab" data-tab="audit">审计日志</div>
    <div class="nav-tab" data-tab="backup">备份恢复</div>
    <div class="nav-tab" data-tab="settings">系统设置</div>
  </div>
  <div class="content">
    <div id="tab-overview" class="tab-panel active">
      <div class="section-header"><span class="section-title">运营概览</span><button class="btn-sm btn-primary" onclick="loadAll()">刷新</button></div>
      <div id="overviewCards" class="grid stats-grid"></div>
      <div class="panel" style="margin-top:14px">
        <div class="section-header"><span class="section-title">今日登录 / 下单漏斗</span><button class="btn-sm btn-gray" onclick="loadActivityStats()">刷新统计</button></div>
        <div id="activityStatsPanel"></div>
      </div>
      <div class="grid" style="grid-template-columns: 1fr 1fr; margin-top:14px">
        <div class="panel"><div class="section-header"><span class="section-title">当前在线客户</span><button class="btn-sm btn-gray" onclick="showTab('online')">查看全部</button></div><div id="overviewOnline"></div></div>
        <div class="panel"><div class="section-header"><span class="section-title">进行中订单</span><button class="btn-sm btn-gray" onclick="showTab('orders')">订单管理</button></div><div id="overviewOrders"></div></div>
      </div>
    </div>

    <div id="tab-analytics" class="tab-panel">
      <div class="section-header"><span class="section-title">订单分析 / 日周月报表</span><button class="btn-sm btn-primary" onclick="loadOrderAnalytics()">生成报表</button></div>
      <div class="toolbar">
        <select id="analyticsPeriod"><option value="day">按日</option><option value="week">按周</option><option value="month">按月</option></select>
        <input id="analyticsDate" type="date">
        <button class="btn-sm btn-gray" onclick="loadOrderAnalytics()">刷新</button>
      </div>
      <div id="analyticsPanel"></div>
    </div>

    <div id="tab-online" class="tab-panel">
      <div class="section-header"><span class="section-title">目前在线客户预览</span><button class="btn-sm btn-primary" onclick="loadOnline()">刷新在线</button></div>
      <div id="onlineTable"></div>
    </div>

    <div id="tab-devices" class="tab-panel">
      <div class="section-header">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span class="section-title">设备直控 / 管理员手动下单</span>
          <span id="deviceStatusBadge" style="font-size:14px;color:#6b7280;background:#f3f4f6;border-radius:20px;padding:4px 14px;display:inline-flex;align-items:center;gap:6px;"></span>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <label style="font-size:13px;color:#6b7280;display:inline-flex;align-items:center;gap:4px;">排序
            <select id="deviceSortMode" onchange="onDeviceSortChange()" style="padding:5px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;background:#fff;">
              <option value="name">机器号</option>
              <option value="idle_first">空闲优先</option>
            </select>
          </label>
          <button class="btn-sm btn-primary" onclick="openAddDeviceModal()">+ 添加设备</button>
          <button class="btn-sm btn-gray" onclick="loadDevicesAdmin(true)">🔄 刷新</button>
        </div>
      </div>
      <div class="hint" style="margin-bottom:10px">设备新增、机器模式切换、设备码绑定都通过中央 Bridge API Key 执行；直控仍走 control session + fencing token + command queue。</div>
      <div id="devicesTable"></div>
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

    <div id="tab-admins" class="tab-panel">
      <div class="section-header">
        <span class="section-title">商户管理员 / 权限管理</span>
        <div><button class="btn-sm btn-primary" onclick="openAdminModal()">+ 新建管理员</button><button class="btn-sm btn-gray" onclick="loadAdmins()">刷新</button></div>
      </div>
      <div class="hint" style="margin-bottom:10px">owner 可配置系统、客户余额、卡密、设备直控、备份恢复和管理员；operator 仅用于查看运营数据、客户、订单、设备和审计。</div>
      <div id="adminsTable"></div>
    </div>

    <div id="tab-audit" class="tab-panel">
      <div class="section-header"><span class="section-title">权限与敏感操作审计</span><button class="btn-sm btn-primary" onclick="loadAuditLogs()">刷新审计</button></div>
      <div class="hint" style="margin-bottom:10px">记录 Bridge API Key 配置、管理员手动下单、设备直控、管理员换队等敏感动作，便于上线后追责。</div>
      <div id="auditTable"></div>
    </div>

    <div id="tab-backup" class="tab-panel">
      <div class="section-header">
        <span class="section-title">数据库备份 / 恢复</span>
        <div><button class="btn-sm btn-green" onclick="createBackup()">立即备份</button><button class="btn-sm btn-primary" onclick="loadBackups()">刷新</button></div>
      </div>
      <div class="hint" style="margin-bottom:10px">上线运维安全：恢复前会自动创建 pre_restore 备份；恢复后建议重启服务，确保所有连接重新打开。</div>
      <div id="backupTable"></div>
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

  <div id="adminModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">新建管理员</div>
      <div class="modal-body">
        <div class="field"><label>用户名</label><input id="adminNewUsername" placeholder="后台登录用户名"></div>
        <div class="field"><label>密码</label><input id="adminNewPassword" value="123456" placeholder="至少 6 位"></div>
        <div class="field-row">
          <div class="field"><label>角色</label><select id="adminNewRole"><option value="operator">operator / 只读运营</option><option value="owner">owner / 完整管理</option></select></div>
          <div class="field"><label>状态</label><select id="adminNewStatus"><option value="active">active</option><option value="disabled">disabled</option></select></div>
        </div>
        <div class="hint">正式上线建议日常使用 operator 查看数据，owner 只用于配置、充值卡、客户余额、设备直控、备份恢复。</div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('adminModal')">取消</button><button class="btn-sm btn-primary" onclick="submitCreateAdmin()">创建</button></div>
    </div>
  </div>

  <div id="adminPwdModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">重置管理员密码</div>
      <div class="modal-body">
        <input id="adminPwdId" type="hidden">
        <div class="field"><label>管理员</label><input id="adminPwdUsername" disabled style="background:#f9fafb"></div>
        <div class="field"><label>新密码</label><input id="adminPwdNew" value="123456" placeholder="至少 6 位"></div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('adminPwdModal')">取消</button><button class="btn-sm btn-primary" onclick="submitResetAdminPwd()">确认重置</button></div>
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

  <div id="addDeviceModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head" id="deviceModalTitle">添加设备</div>
      <div class="modal-body">
        <input type="hidden" id="editDeviceId" value="" />
        <div class="field">
          <label>设备名称</label>
          <input type="text" id="deviceName" placeholder="例如：1号机器" />
        </div>
        <div class="field" id="fieldDeviceKey">
          <label>机器ID / 设备码</label>
          <input type="text" id="deviceKey" placeholder="粘贴客户端显示的机器ID" style="font-family:monospace;" />
          <div class="hint" id="cardKeyHint">从客户端界面复制机器ID粘贴到此处；保存会通过 Bridge API Key 写入中央。</div>
        </div>
        <div class="field">
          <label>运行模式</label>
          <select id="deviceMode">
            <option value="machine">机密</option>
            <option value="hybrid">混合</option>
            <option value="absolute">绝密</option>
          </select>
        </div>
        <div class="field">
          <label>备注网址</label>
          <input type="text" id="deviceRadarUrl" placeholder="例如：https://xxx.com/radar" />
        </div>
        <div class="field">
          <label>备用电脑的名刀卡号</label>
          <input type="text" id="deviceWatchdogCard" placeholder="留空表示不绑定" style="font-family:monospace;" />
        </div>
      </div>
      <div class="modal-foot">
        <button class="btn-sm btn-gray" onclick="closeAddDeviceModal()">取消</button>
        <button class="btn-sm btn-primary" onclick="submitDevice()" id="deviceSubmitBtn">提交</button>
      </div>
    </div>
  </div>

  <div id="manualOrderModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">手动下单</div>
      <div class="modal-body">
        <input type="hidden" id="manualDeviceId" value="" />
        <div class="field">
          <div class="info-box" id="manualDeviceInfo" style="padding:10px 12px;background:#eff6ff;border-radius:8px;font-size:13px;color:#1d4ed8;">--</div>
        </div>
        <div class="field">
          <label>组队码</label>
          <input type="text" id="manualBossName" placeholder="前3位大写字母+后4位数字（如 ABC1234）" />
        </div>
        <div class="field" id="manualHybridModeSection" style="display:none">
          <label>混合模式选择</label>
          <div style="display:flex;gap:10px;">
            <label style="display:flex;align-items:center;cursor:pointer;">
              <input type="radio" name="manualHybridMode" value="machine" checked onchange="updateManualOrderMode()" style="margin-right:5px;" />
              <span>按机密下单</span>
            </label>
            <label style="display:flex;align-items:center;cursor:pointer;">
              <input type="radio" name="manualHybridMode" value="absolute" onchange="updateManualOrderMode()" style="margin-right:5px;" />
              <span>按绝密下单</span>
            </label>
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>时长（小时）</label>
            <input type="number" id="manualHours" min="0" max="9999" value="1" onchange="autoCalculateRounds()" />
          </div>
          <div class="field">
            <label>时长（分钟）</label>
            <input type="number" id="manualMinutes" min="0" max="59" value="0" onchange="autoCalculateRounds()" />
          </div>
        </div>
        <div class="field">
          <label>限制局数（0表示不限制）</label>
          <input type="number" id="manualMaxRounds" min="0" value="0" oninput="this.value=this.value.replace(/[^0-9]/g,'')" />
        </div>
        <div class="field">
          <label>限制亏币（单位：万，0表示不限制）</label>
          <input type="number" id="manualMaxCoinLoss" min="0" value="0" oninput="this.value=this.value.replace(/[^0-9]/g,'')" />
        </div>
        <div id="loadoutSection" style="display:none;">
          <div class="field">
            <label>配装类型</label>
            <div style="display:flex;gap:10px;">
              <label style="display:flex;align-items:center;cursor:pointer;">
                <input type="radio" name="loadoutType" value="default" checked onchange="toggleLoadoutCustom()" style="margin-right:5px;" />
                <span>大红包默认配装</span>
              </label>
              <label style="display:flex;align-items:center;cursor:pointer;" id="adminCustomLoadoutOption">
                <input type="radio" name="loadoutType" value="custom" onchange="toggleLoadoutCustom()" style="margin-right:5px;" />
                <span>自定义配装</span>
              </label>
            </div>
          </div>
          <div id="customLoadoutFields" style="display:none;">
            <div class="field">
              <label>头部装备</label>
              <select id="loadoutHelmet" onchange="calculateLoadoutCost()"><option value="">不携带</option></select>
            </div>
            <div class="field">
              <label>护甲装备</label>
              <select id="loadoutArmor" onchange="calculateLoadoutCost()"><option value="">不携带</option></select>
            </div>
            <div class="field">
              <label>胸挂装备</label>
              <select id="loadoutRig" onchange="calculateLoadoutCost()"><option value="">不携带</option></select>
            </div>
            <div class="field">
              <label>手枪装备</label>
              <select id="loadoutPistol" onchange="calculateLoadoutCost()"><option value="">不携带</option></select>
            </div>
            <div class="field">
              <label>背包装备</label>
              <select id="loadoutBackpack" onchange="calculateLoadoutCost()"><option value="">不携带</option></select>
            </div>
            <div class="field">
              <div id="loadoutCostDisplay" style="padding:10px;background:#fef3c7;border-radius:6px;font-size:14px;font-weight:bold;color:#92400e;">
                配装总价：<span id="loadoutCostValue">0</span>元 / <span id="adminMaxCost">65</span>W
              </div>
            </div>
          </div>
        </div>
        <div class="hint" style="color:#d97706;">上机用户将显示为「组队码-手动」</div>
      </div>
      <div class="modal-foot">
        <button class="btn-sm btn-gray" onclick="closeManualOrderModal()">取消</button>
        <button class="btn-sm btn-green" onclick="submitManualOrder()" id="manualOrderBtn">确认下单</button>
      </div>
    </div>
  </div>

  <div id="adminRejoinModal" class="modal-mask">
    <div class="modal">
      <div class="modal-head">管理员换队</div>
      <div class="modal-body">
        <input id="rejoinOrderId" type="hidden">
        <div class="info-box" id="rejoinInfo">--</div>
        <div class="field"><label>新队伍码</label><input id="rejoinTeamCode" placeholder="例如 NEW1234"></div>
      </div>
      <div class="modal-foot"><button class="btn-sm btn-gray" onclick="closeModal('adminRejoinModal')">取消</button><button class="btn-sm btn-primary" onclick="submitAdminRejoin()">下发换队</button></div>
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
const CURRENT_ADMIN_ROLE = '__ROLE__';
const IS_OWNER = CURRENT_ADMIN_ROLE === 'owner';
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
async function copyText(v) {
  try { await navigator.clipboard.writeText(String(v || '')); toast('已复制'); }
  catch(e) { window.prompt('复制下面内容', String(v || '')); }
}
function showTab(name) {
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'online') loadOnline();
  if (name === 'analytics') loadOrderAnalytics();
  if (name === 'devices') loadDevicesAdmin();
  if (name === 'customers') loadCustomers();
  if (name === 'cards') loadCards();
  if (name === 'equipment') loadEquipmentConfig();
  if (name === 'orders') loadOrders();
  if (name === 'admins') loadAdmins();
  if (name === 'audit') loadAuditLogs();
  if (name === 'backup') loadBackups();
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
  await loadActivityStats();
}
async function loadActivityStats() {
  const d = await api('/api/admin/activity-stats');
  const s = d.stats || {};
  const statusText = (s.login_status_breakdown || []).map(x => `${esc(x.status)}：${esc(x.count)}`).join(' / ') || '-';
  const qualityText = (s.order_quality_breakdown || []).map(x => `${esc(x.quality)} ${esc(x.count)}单 ${esc(x.hours)}小时`).join(' / ') || '-';
  const noRows = (s.login_without_order_customers || []).slice(0, 20).map(x => `<tr><td>${esc(x.username)}</td><td>${esc(x.login_count)}</td><td>${fmtDate(x.last_login_at)}</td><td>${esc(x.order_status_at_login || 'none')}</td></tr>`).join('');
  $('activityStatsPanel').innerHTML = `
    <div class="grid stats-grid">
      <div class="stat-card"><div class="stat-label">今日登录客户</div><div class="stat-value">${esc(s.login_customer_count || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">登录未下单</div><div class="stat-value">${esc(s.login_without_order_count || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">今日下单客户</div><div class="stat-value">${esc(s.order_customer_count || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">今日订单数</div><div class="stat-value">${esc(s.order_count || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">今日下单小时</div><div class="stat-value">${esc(s.order_hours || 0)}</div></div>
    </div>
    <div class="hint" style="margin-top:8px">登录时订单状态：${statusText}<br>下单类型：${qualityText}</div>
    ${noRows ? `<table class="data-table" style="margin-top:10px"><thead><tr><th>登录未下单客户</th><th>登录次数</th><th>最后登录</th><th>登录当时订单状态</th></tr></thead><tbody>${noRows}</tbody></table>` : '<div class="empty-state" style="margin-top:10px">暂无登录未下单客户</div>'}
  `;
}
async function loadOrderAnalytics() {
  if (!$('analyticsDate').value) $('analyticsDate').value = new Date().toISOString().slice(0,10);
  const period = $('analyticsPeriod').value || 'day';
  const date = $('analyticsDate').value || '';
  const d = await api('/api/admin/order-analytics?period=' + encodeURIComponent(period) + '&date=' + encodeURIComponent(date));
  renderOrderAnalytics(d.analytics || {});
}
function renderOrderAnalytics(a) {
  const maxOrders = Math.max(1, ...(a.daily_series || []).map(x => Number(x.order_count || 0)));
  const bars = (a.daily_series || []).map(x => `<div class="bar-row"><div>${esc(x.date)}</div><div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, Number(x.order_count || 0) / maxOrders * 100)}%"></div></div><div>${esc(x.order_count)}单 / ${esc(x.hours)}h</div></div>`).join('');
  const ranks = (a.customer_rank || []).slice(0, 20).map((x, i) => `<tr><td>${i+1}</td><td>${esc(x.username)}</td><td>${esc(x.order_count)}</td><td>${esc(x.hours)}</td></tr>`).join('');
  const statusRows = (a.status_breakdown || []).map(x => `<span class="badge badge-offline" style="margin-right:6px">${esc(x.status)} ${esc(x.count)}</span>`).join('') || '-';
  const qualityRows = (a.quality_breakdown || []).map(x => `<span class="badge badge-machine" style="margin-right:6px">${esc(x.quality)} ${esc(x.order_count)}单 ${esc(x.hours)}h</span>`).join('') || '-';
  $('analyticsPanel').innerHTML = `
    <div class="grid stats-grid">
      <div class="stat-card"><div class="stat-label">订单数</div><div class="stat-value">${esc(a.order_count || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">下单老板数</div><div class="stat-value">${esc(a.customer_count || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">下单小时</div><div class="stat-value">${esc(a.requested_hours || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">完成小时</div><div class="stat-value">${esc(a.completed_hours || 0)}</div></div>
      <div class="stat-card"><div class="stat-label">异常/失败单</div><div class="stat-value">${esc(a.failed_order_count || 0)}</div></div>
    </div>
    <div class="panel" style="margin-top:14px"><div class="section-title">每日订单柱状图（${esc(a.start_date)} 至 ${esc(a.end_date)}）</div><div class="bar-list">${bars || '<div class="empty-state">暂无数据</div>'}</div></div>
    <div class="grid" style="grid-template-columns:1fr 1fr;margin-top:14px">
      <div class="panel"><div class="section-title">状态分布</div><div style="margin-top:10px">${statusRows}</div></div>
      <div class="panel"><div class="section-title">模式分布</div><div style="margin-top:10px">${qualityRows}</div></div>
    </div>
    <div class="panel" style="margin-top:14px"><div class="section-title">下单排行 TOP20</div>
      ${ranks ? `<table class="data-table" style="margin-top:10px"><thead><tr><th>#</th><th>老板/客户</th><th>订单数</th><th>小时</th></tr></thead><tbody>${ranks}</tbody></table>` : '<div class="empty-state">暂无排行</div>'}
    </div>`;
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

async function loadDevicesAdmin(forceRefresh=false) {
  const d = await api('/api/admin/devices' + (forceRefresh ? '?force_refresh=1' : ''));
  renderDevicesAdmin(d.devices || []);
}
function deviceModeBadge(mode, runningMode='') {
  mode = String(mode || 'machine'); runningMode = String(runningMode || '');
  if (mode === 'absolute') return '<span class="badge badge-absolute">绝密</span>';
  if (mode === 'hybrid') {
    if (runningMode === 'machine') return '<span class="badge badge-purple">混合-机密</span>';
    if (runningMode === 'absolute' || runningMode === 'secret') return '<span class="badge badge-purple">混合-绝密</span>';
    return '<span class="badge badge-purple">混合</span>';
  }
  return '<span class="badge badge-machine">机密</span>';
}
function workStatusBadge(status, cooldownMs=0) {
  status = String(status || '未知');
  const secLeft = cooldownMs ? Math.max(0, Math.ceil((Number(cooldownMs) - Date.now()) / 1000)) : 0;
  const label = status === '准备中' && secLeft > 0 ? `准备中 ${secLeft}s` : status;
  const running = ['配装中','游戏中','执行中','等待选图','等待进图','等待进图中','监控中','已准备待进图','选择干员中','地图载入中','丢包中','自雷中','观战中','已进队'];
  if (status === '空闲') return '<span class="badge badge-online">空闲</span>';
  if (status === '离线' || status === '不在线') return '<span class="badge badge-offline">不在线</span>';
  if (status === '已阵亡' || status === '进队异常' || status === '进队失败') return `<span class="badge badge-failed">${esc(label)}</span>`;
  if (status === '已结束') return `<span class="badge badge-used">${esc(label)}</span>`;
  if (status === '准备中' || status === '等待进房' || status === '等待救援') return `<span class="badge badge-waiting">${esc(label)}</span>`;
  if (running.includes(status)) return `<span class="badge badge-running">${esc(label)}</span>`;
  return `<span class="badge badge-used">${esc(label)}</span>`;
}
function formatRemainingHm(minutes) {
  const m = parseInt(minutes || 0);
  const h = Math.floor(m / 60), mm = m % 60;
  if (h > 0 && mm > 0) return `${h}时${mm}分`;
  if (h > 0) return `${h}时`;
  return `${mm}分`;
}
function parseHfbNumber(v) {
  const s = String(v || '').replace(/[,，\s]/g, '').toUpperCase();
  if (!s) return 0;
  const n = parseFloat(s.replace(/[WK万M]/g, '')) || 0;
  if (s.endsWith('M')) return n * 1000000;
  if (s.endsWith('W') || s.endsWith('万')) return n * 10000;
  if (s.endsWith('K')) return n * 1000;
  return parseFloat(s) || 0;
}
function formatHfb(v) {
  const raw = String(v || '');
  const n = parseHfbNumber(raw);
  if (!raw && !n) return {text:'--', low:false};
  if (raw) return {text: raw, low: n > 0 && n < 500000};
  if (n >= 10000) return {text:(n/10000).toFixed(1).replace(/\.0$/,'') + '万', low:n < 500000};
  return {text:String(n), low:n > 0 && n < 500000};
}
function deviceDetailHtml(d) {
  const detailMain = d.work_status_detail || d.sub_state || d.prison_stage_label || d.prison_stage || '';
  if (!detailMain && !d.prison_match && !d.prison_point && !d.prison_action && !d.current_map) return '<span class="hint">--</span>';
  const meta = [d.prison_action, d.prison_point, d.prison_match, d.prison_region, d.current_map].filter(Boolean).map(esc).join(' · ');
  return `<div style="line-height:1.5;min-width:120px;"><span class="badge badge-purple">${esc(detailMain || '详情')}</span>${meta ? `<div class="hint" style="max-width:190px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${meta}">${meta}</div>` : ''}</div>`;
}
function getDeviceSortMode() { return $('deviceSortMode')?.value || 'name'; }
function sortDevicesForAdmin(rows) {
  const items = (rows || []).slice();
  const byName = (a, b) => (parseInt(a.device_name || a.name || a.id) || Number(a.id || 0)) - (parseInt(b.device_name || b.name || b.id) || Number(b.id || 0));
  if (getDeviceSortMode() === 'idle_first') {
    const rank = d => (d.work_status === '空闲' ? 0 : (d.online ? 1 : 2));
    items.sort((a, b) => rank(a) - rank(b) || byName(a, b));
  } else {
    items.sort(byName);
  }
  return items;
}
function updateDeviceStatusBadge(rows) {
  const badge = $('deviceStatusBadge');
  if (!badge) return;
  const statusCount = {};
  (rows || []).forEach(d => { const s = d.work_status || (d.online ? '未知' : '离线'); statusCount[s] = (statusCount[s] || 0) + 1; });
  const colors = {'空闲':'#16a34a','已进队':'#2563eb','配装中':'#2563eb','游戏中':'#2563eb','执行中':'#2563eb','离线':'#ef4444','不在线':'#ef4444','已结束':'#6b7280','等待救援':'#d97706','进队异常':'#dc2626','进队失败':'#dc2626'};
  const parts = Object.entries(statusCount).map(([s,n]) => `<span style="color:${colors[s] || '#6b7280'};font-weight:600;">${esc(s)}</span> <span>${esc(n)}</span>`);
  const total = (rows || []).length;
  const enabled = (rows || []).filter(d => d.enabled !== false).length;
  const online = (rows || []).filter(d => d.online).length;
  parts.push(`<span style="color:#6b7280;">|</span> <span style="color:#374151;">在线</span> <b style="color:#3b82f6;">${online}/${total}</b>`);
  parts.push(`<span style="color:#374151;">启用</span> <b style="color:#3b82f6;">${enabled}/${total}</b>`);
  badge.innerHTML = parts.join('<span style="color:#d1d5db;">·</span>');
}
function onDeviceSortChange() { loadDevicesAdmin(); }
function renderDevicesAdmin(rows) {
  rows = sortDevicesForAdmin(rows || []);
  updateDeviceStatusBadge(rows);
  if (!rows.length) { $('devicesTable').innerHTML = '<div class="empty-state">暂无设备；请先在 /setup 配置 Bridge API Key，并确认中央 Bridge 已有 Agent 或点击添加设备。</div>'; return; }
  $('devicesTable').innerHTML = `<table class="data-table"><thead><tr>
    <th>ID</th><th>名称/设备码</th><th>模式</th><th>在线</th><th>工作状态</th><th>详情</th>
    <th>上机用户</th><th>剩余时长</th><th>预计结束</th><th>哈币</th><th>老板ID</th><th>已打局</th><th>已打币</th><th>版本</th><th>启用</th><th>操作</th>
  </tr></thead><tbody>` + rows.map(d => {
    const o = d.active_order;
    const devName = d.device_name || (d.id + '号机');
    const devKey = d.device_key || d.machine_id || '';
    const encName = encodeURIComponent(devName);
    const encKey = encodeURIComponent(devKey);
    const encRadar = encodeURIComponent(d.radar_url || '');
    const encWatchdog = encodeURIComponent(d.watchdog_card || '');
    const mode = d.mode || d.device_mode || 'machine';
    const nextMode = mode === 'machine' ? 'hybrid' : (mode === 'hybrid' ? 'absolute' : 'machine');
    const shortKey = devKey ? (devKey.length > 16 ? devKey.slice(0, 12) + '...' : devKey) : '未绑定机器ID';
    const status = d.work_status || (d.online ? ((o || d.running_order_id) ? '执行中' : '空闲') : '离线');
    const runUser = d.running_user || d.active_customer || (o ? (o.customer_username || o.customer_id) : '');
    const runBoss = d.running_boss_name || d.team_code || (o ? o.team_code : '');
    const userHtml = runUser ? `<span class="badge badge-running">${esc(runUser === 'admin' ? ((runBoss || '手动') + '-手动') : runUser)}</span>${runBoss ? `<div class="hint">组队码：${esc(runBoss)}</div>` : ''}` : '<span class="hint">--</span>';
    const rem = Number(d.remaining_minutes || (o ? o.remaining_minutes : 0) || 0);
    const hfb = formatHfb(d.harvard || d.hfb_value || '');
    const bossId = d.spectate_boss || d.boss_id || '--';
    const roundText = `${Number(d.round_count || 0)}${Number(d.max_rounds || 0) > 0 ? '/' + Number(d.max_rounds || 0) : '/无限制'}`;
    let coinText = '--';
    if (d.running_order_id || o) {
      const currentCoins = parseHfbNumber(d.harvard || d.hfb_value || '');
      const startCoins = Number(d.start_coins || 0);
      const coinLoss = startCoins > 0 && currentCoins > 0 ? Math.max(0, (startCoins - currentCoins) / 10000) : Number(d.actual_coin_loss || 0);
      const limit = Number(d.max_coin_loss || 0) > 0 ? Number(d.max_coin_loss || 0) + '万' : '无限制';
      coinText = `${coinLoss ? coinLoss.toFixed(1) : '0.0'}万/${limit}`;
    }
    // 操作控件按旧版 1:1：空闲只露出手动下单；运行中露出换队/加减时/结束/等待救援切换；
    // 其余维护动作统一收进“更多 ▴”下拉菜单。
    let actionBtns = '';
    if (status === '空闲' && !d.running_order_id) {
      actionBtns += `<button class="btn-sm btn-green" onclick="openManualOrderModal(${d.id}, decodeURIComponent('${encName}'), '${esc(mode)}')">手动下单</button>`;
    }
    if (d.running_order_id) {
      actionBtns += `<button class="btn-sm btn-purple" onclick="adminRejoin(${d.running_order_id})">换队</button>`;
      actionBtns += `<button class="btn-sm btn-primary" onclick="openAddTimeModal(${d.running_order_id}, decodeURIComponent('${encName}'), ${rem}, ${Number(d.max_rounds || 0)}, ${Number(d.round_count || 0)}, ${Number(d.max_coin_loss || 0)})">加减时</button>`;
      actionBtns += `<button class="btn-sm btn-danger" onclick="adminStopOrder(${d.running_order_id})">结束</button>`;
      if (status === '等待救援') {
        actionBtns += `<button class="btn-sm btn-amber" onclick="switchSpectate(${d.id}, ${d.running_order_id})">切换</button>`;
      }
    }
    if (d.radar_url && String(d.radar_url).trim() !== '') {
      actionBtns += `<button class="btn-sm btn-gray" onclick="copyOrOpenDeviceRadarUrl(this, decodeURIComponent('${encRadar}'))" title="单击复制 · 双击打开此设备的备注网址">📋 网址</button>`;
    }
    const modeCycleLabel = { machine: '切为机密', hybrid: '切为混合', absolute: '切为绝密' };
    const newModeLabel = modeCycleLabel[nextMode] || '切换模式';
    const wd = d.watchdog || {};
    let wdBtnAttr = '';
    let wdBtnTitle = '';
    if (!wd.bound && !d.watchdog_card) {
      wdBtnAttr = 'disabled style="opacity:0.5;cursor:not-allowed;"';
      wdBtnTitle = '未绑定名刀卡号';
    } else if (wd.bound && !wd.online) {
      wdBtnAttr = 'disabled style="opacity:0.5;cursor:not-allowed;"';
      wdBtnTitle = '名刀离线';
    } else {
      wdBtnTitle = wd.pending_restart ? '已发送重启指令，等待名刀执行' : '名刀在线';
    }
    actionBtns += `<div class="dropdown" onclick="toggleDropdown(event, this)">
        <button class="btn-sm btn-gray">更多 ▴</button>
        <div class="dropdown-menu">
            <button class="dropdown-item" onclick="restartDevice(decodeURIComponent('${encKey}'), decodeURIComponent('${encName}'))">异常重启</button>
            <button class="dropdown-item" onclick="restartBackupPC(${d.id}, decodeURIComponent('${encName}'))" ${wdBtnAttr} title="${esc(wdBtnTitle)}">重启备用电脑</button>
            <button class="dropdown-item" onclick="updateDevice(decodeURIComponent('${encKey}'), decodeURIComponent('${encName}'))">远程更新</button>
            <button class="dropdown-item" onclick="collectLog(decodeURIComponent('${encKey}'), decodeURIComponent('${encName}'))">回收日志</button>
            <button class="dropdown-item" onclick="switchMode(${d.id}, '${esc(nextMode)}')">${newModeLabel}</button>
            <button class="dropdown-item" onclick="toggleDevice(${d.id}, ${d.enabled ? 0 : 1})">${d.enabled ? '禁用设备' : '启用设备'}</button>
            <button class="dropdown-item" onclick="editDevice(${d.id}, decodeURIComponent('${encName}'), decodeURIComponent('${encKey}'), '${esc(mode)}', decodeURIComponent('${encRadar}'), decodeURIComponent('${encWatchdog}'))">编辑设备</button>
            <button class="dropdown-item text-danger" onclick="deleteDevice(${d.id})">删除设备</button>
        </div>
    </div>`;
    return `<tr>
      <td>${esc(d.id)}</td>
      <td><b>${esc(devName)}</b><div class="hint" style="font-family:monospace">${esc(shortKey)}</div></td>
      <td>${deviceModeBadge(mode, d.running_mode)}</td>
      <td>${onlineBadge(d.online)}</td>
      <td>${workStatusBadge(status, d.cooldown_until_ms || 0)}</td>
      <td>${deviceDetailHtml(d)}</td>
      <td>${userHtml}</td>
      <td>${(runUser || o || d.running_order_id) ? `<b>${rem}</b> 分钟<div class="hint">${formatRemainingHm(rem)}</div>` : '--'}</td>
      <td><span class="hint">${esc(d.estimated_end || (o ? o.end_at : '') || '--')}</span></td>
      <td>${hfb.text === '--' ? '--' : `<span${hfb.low ? ' style="color:#dc2626;font-weight:bold;"' : ''} title="原始值: ${esc(d.harvard || d.hfb_value || '')}">${esc(hfb.text)}</span>`}</td>
      <td style="font-family:monospace"><b>${esc(bossId)}</b></td>
      <td>${esc(roundText)}</td>
      <td>${esc(coinText)}</td>
      <td><span class="hint">${esc(d.script_ver || '--')}</span></td>
      <td>${d.enabled === false ? '<span class="badge badge-offline">禁用</span>' : '<span class="badge badge-online">启用</span>'}</td>
      <td style="display:flex;gap:4px;flex-wrap:wrap;min-width:260px;">${actionBtns}</td>
    </tr>`;
  }).join('') + '</tbody></table>';
}
async function loadDevices(forceRefresh=false) { await loadDevicesAdmin(); }
function openAddDeviceModal() {
  $('editDeviceId').value = '';
  $('deviceName').value = '';
  $('deviceKey').value = '';
  $('deviceMode').value = 'machine';
  $('deviceRadarUrl').value = '';
  $('deviceWatchdogCard').value = '';
  $('deviceModalTitle').textContent = '添加设备';
  $('cardKeyHint').textContent = '从客户端界面复制机器ID粘贴到此处；保存会通过 Bridge API Key 写入中央。';
  openModal('addDeviceModal');
}
function closeAddDeviceModal() { closeModal('addDeviceModal'); }
function editDevice(id, name, key, mode, radarUrl, watchdogCard) {
  $('editDeviceId').value = id;
  $('deviceName').value = name || '';
  $('deviceKey').value = key || '';
  $('deviceMode').value = mode || 'machine';
  $('deviceRadarUrl').value = radarUrl || '';
  $('deviceWatchdogCard').value = watchdogCard || '';
  $('deviceModalTitle').textContent = '编辑设备';
  $('cardKeyHint').textContent = '修改机器ID将更换绑定的客户端；保存会通过 Bridge API Key 写入中央。';
  openModal('addDeviceModal');
}
async function submitDevice() {
  const id = $('editDeviceId').value;
  const body = {
    device_name: $('deviceName').value.trim(),
    device_key: $('deviceKey').value.trim(),
    mode: $('deviceMode').value,
    radar_url: $('deviceRadarUrl').value.trim(),
    watchdog_card: $('deviceWatchdogCard').value.trim(),
  };
  if (!body.device_name) { toast('请填写设备名称'); return; }
  if (!body.device_key) { toast('请填写机器ID / 设备码'); return; }
  const btn = $('deviceSubmitBtn');
  btn.disabled = true; btn.textContent = '提交中...';
  try {
    const url = id ? `/api/admin/devices/${id}` : '/api/admin/devices';
    const method = id ? 'PUT' : 'POST';
    const data = await api(url, {method, body:JSON.stringify(body)});
    toast(data.msg || '操作成功');
    closeAddDeviceModal();
    await loadDevicesAdmin();
  } catch(e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = '提交'; }
}
async function switchMode(id, mode) {
  try {
    const d = await api(`/api/admin/devices/${id}/mode`, {method:'PUT', body:JSON.stringify({mode})});
    toast(d.msg || '模式已切换');
    await loadDevicesAdmin();
  } catch(e) { toast(e.message); }
}
async function toggleDevice(id, enabled) {
  try {
    const d = await api(`/api/admin/devices/${id}/toggle`, {method:'PUT', body:JSON.stringify({enabled: !!enabled})});
    toast(d.msg || '保存成功');
    await loadDevicesAdmin();
  } catch(e) { toast(e.message); }
}
async function deleteDevice(id) {
  const ok = await appConfirm('删除设备', '确定删除该设备？有活动 control session 时后端会拒绝。', 'btn-danger');
  if (!ok) return;
  try {
    await api(`/api/admin/devices/${id}`, {method:'DELETE'});
    toast('删除成功');
    await loadDevicesAdmin();
  } catch(e) { toast(e.message); }
}
const _radarBtnTimers = new WeakMap();
function copyOrOpenDeviceRadarUrl(btn, url) {
  if (!url) return;
  const existing = _radarBtnTimers.get(btn);
  if (existing) {
    clearTimeout(existing);
    _radarBtnTimers.delete(btn);
    try { window.open(url, '_blank'); }
    catch(e) { appAlert('打开失败：' + e.message); }
    return;
  }
  const t = setTimeout(() => {
    _radarBtnTimers.delete(btn);
    copyText(url);
  }, 280);
  _radarBtnTimers.set(btn, t);
}
function adminRejoin(orderId) { openAdminRejoin(orderId, ''); }
function adminStopOrder(orderId) { stopOrder(orderId); }
function openAddTimeModal(orderId, deviceName, remainMinutes, maxRounds, roundCount, maxCoinLoss) {
  $('addTimeOrderId').value = orderId;
  const remainRounds = Math.max(0, Number(maxRounds || 0) - Number(roundCount || 0));
  $('addTimeInfo').innerHTML =
    `设备: <b>${esc(deviceName)}</b> · 订单#${esc(orderId)}<br>` +
    `当前剩余时间: <b>${fmtMin(remainMinutes)}</b> · ` +
    `剩余局数: <b>${remainRounds}</b>(已打${Number(roundCount || 0)}/${Number(maxRounds || 0)}) · ` +
    `亏币上限: <b>${Number(maxCoinLoss || 0)}</b>万`;
  document.querySelector('input[name="addTimeOp"][value="add"]').checked = true;
  $('addTimeHours').value = 0;
  $('addTimeMinutes').value = 0;
  openModal('addTimeModal');
}
async function switchSpectate(deviceId, orderId) {
  const ok = await appConfirm('切换观战', '确定要切换观战目标？', 'btn-amber');
  if (!ok) return;
  try {
    await api(`/api/admin/devices/${deviceId}/command`, {method:'POST', body:JSON.stringify({action:'switch_spectate', params:{order_id:orderId, operator:'merchant_admin'}})});
    appAlert('已发送切换指令');
    await loadDevicesAdmin();
  } catch(e) { appAlert(e.message); }
}
function toggleDropdown(e, el) {
  e.stopPropagation();
  document.querySelectorAll('.dropdown.open').forEach(d => { if (d !== el) d.classList.remove('open'); });
  el.classList.toggle('open');
  if (el.classList.contains('open')) {
    const menu = el.querySelector('.dropdown-menu');
    if (menu) {
      menu.classList.remove('up');
      const rect = el.getBoundingClientRect();
      const menuHeight = menu.offsetHeight || 160;
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      if (spaceBelow < menuHeight && spaceAbove > spaceBelow) menu.classList.add('up');
    }
  }
}
document.addEventListener('click', () => {
  document.querySelectorAll('.dropdown.open').forEach(d => d.classList.remove('open'));
});
async function restartDevice(deviceKey, deviceName) {
  const ok = await appConfirm('异常重启', `确定异常重启设备 "${deviceName}" 吗？\n\n脚本将关闭并重新启动，启动后进入恢复脚本阶段。\n如果有正在运行的订单，服务器将重新下发。`, 'btn-amber');
  if (!ok) return;
  try {
    await api(`/api/admin/machines/${encodeURIComponent(deviceKey)}/restart`, {method:'POST', body:'{}'});
    appAlert('已发送重启指令，等待客户端执行');
    await loadDevicesAdmin();
  } catch(e) { appAlert(e.message); }
}
async function restartBackupPC(deviceId, deviceName) {
  const ok = await appConfirm('重启备用电脑', `确定重启设备 "${deviceName}" 的备用电脑吗？\n\n名刀收到指令后会立刻关闭并重新启动备用电脑，整个过程通常 30~60 秒。`, 'btn-amber');
  if (!ok) return;
  try {
    const data = await api(`/api/admin/devices/${deviceId}/restart_backup`, {method:'POST', body:'{}'});
    appAlert(data.msg || '已发送重启指令');
    await loadDevicesAdmin();
  } catch(e) { appAlert(e.message); }
}
async function updateDevice(deviceKey, deviceName) {
  const ok = await appConfirm('远程更新', `确定远程更新设备 "${deviceName}" 的脚本吗？\n\n更新期间该设备版本号将显示为红色。\n同一时间只能有一台设备在更新。`, 'btn-amber');
  if (!ok) return;
  try {
    await api(`/api/admin/machines/${encodeURIComponent(deviceKey)}/update`, {method:'POST', body:'{}'});
    appAlert('已发送更新指令，等待客户端执行');
    await loadDevicesAdmin();
  } catch(e) { appAlert(e.message); }
}
async function collectLog(deviceKey, deviceName) {
  const ok = await appConfirm('回收日志', `确定回收设备 "${deviceName}" 的运行日志吗？\n\n客户端将自动上传最新的日志文件到服务器。`, 'btn-primary');
  if (!ok) return;
  try {
    await api(`/api/admin/machines/${encodeURIComponent(deviceKey)}/collect_log`, {method:'POST', body:'{}'});
    appAlert('已发送日志回收指令，等待客户端上传');
    await loadDevicesAdmin();
  } catch(e) { appAlert(e.message); }
}
let _manualOrderMode = 'machine';
let _adminMaxLoadoutCost = 650000;
let _adminAllowCustomLoadout = true;
function getManualEffectiveMode() {
  if (_manualOrderMode === 'hybrid') {
    return document.querySelector('input[name="manualHybridMode"]:checked')?.value || 'machine';
  }
  return _manualOrderMode;
}
function validateBossName(name) { return /^[A-Z]{3}\\d{4}$/.test(String(name || '').trim()); }
function appAlert(msg) { toast(msg); }
function formatMinutes(m) { return fmtMin(m); }
function updateManualOrderMode() {
  const effectiveMode = getManualEffectiveMode();
  const loadoutSection = document.getElementById('loadoutSection');
  if (effectiveMode === 'absolute') {
    loadoutSection.style.display = 'block';
    document.querySelector('input[name="loadoutType"][value="default"]').checked = true;
    document.getElementById('customLoadoutFields').style.display = 'none';
    loadAdminEquipmentConfig().then(() => {
      document.querySelector('input[name="loadoutType"][value="default"]').checked = true;
      document.getElementById('customLoadoutFields').style.display = 'none';
      resetLoadoutSelections();
    });
  } else {
    loadoutSection.style.display = 'none';
  }
  autoCalculateRounds();
}
function openManualOrderModal(deviceId, deviceName, mode) {
  _manualOrderMode = mode || 'machine';
  document.getElementById('manualDeviceId').value = deviceId;
  const modeLabel = _manualOrderMode === 'machine' ? '机密' : (_manualOrderMode === 'hybrid' ? '混合' : '绝密');
  document.getElementById('manualDeviceInfo').innerHTML = `设备: <b>${esc(deviceName || deviceId)}</b> · 模式: <b>${modeLabel}</b>`;
  document.getElementById('manualBossName').value = '';
  document.getElementById('manualHours').value = 1;
  document.getElementById('manualMinutes').value = 0;
  document.getElementById('manualMaxRounds').value = 0;
  document.getElementById('manualMaxCoinLoss').value = 0;
  const hybridModeSection = document.getElementById('manualHybridModeSection');
  if (_manualOrderMode === 'hybrid') {
    hybridModeSection.style.display = 'block';
    document.querySelector('input[name="manualHybridMode"][value="machine"]').checked = true;
  } else {
    hybridModeSection.style.display = 'none';
  }
  updateManualOrderMode();
  document.getElementById('manualOrderModal').classList.add('show');
}
function openManualOrder(deviceId, name, mode='machine') { openManualOrderModal(deviceId, name, mode); }
async function loadAdminEquipmentConfig() {
  try {
    const d = await api('/api/admin/equipment-config');
    _adminMaxLoadoutCost = (Number(d.max_loadout_cost || 65)) * 10000;
    _adminAllowCustomLoadout = d.allow_custom_loadout !== false;
    const el = document.getElementById('adminMaxCost');
    if (el) el.textContent = Number(d.max_loadout_cost || 65);
    const customOptionLabel = document.getElementById('adminCustomLoadoutOption');
    if (customOptionLabel) customOptionLabel.style.display = _adminAllowCustomLoadout ? 'flex' : 'none';
    const map = {helmet:'loadoutHelmet', armor:'loadoutArmor', rig:'loadoutRig', pistol:'loadoutPistol', backpack:'loadoutBackpack'};
    Object.values(map).forEach(id => { $(id).innerHTML = '<option value="">不携带</option>'; });
    (d.equipment || []).filter(e => Number(e.enabled) === 1).forEach(e => {
      const sel = $(map[e.equipment_type]); if (!sel) return;
      const priceReal = Number(e.price || 0) * 10000;
      const opt = document.createElement('option');
      opt.value = `${e.equipment_name}:${priceReal}`;
      opt.textContent = `${e.equipment_name} (${Number(e.price || 0)}W)`;
      sel.appendChild(opt);
    });
  } catch(_e) {}
}
function toggleLoadoutCustom() {
  const selectedType = document.querySelector('input[name="loadoutType"]:checked')?.value || 'default';
  const isCustom = _adminAllowCustomLoadout && selectedType === 'custom';
  document.getElementById('customLoadoutFields').style.display = isCustom ? 'block' : 'none';
  if (isCustom) loadAdminEquipmentConfig().then(() => calculateLoadoutCost());
}
function toggleManualLoadoutCustom() { toggleLoadoutCustom(); }
function resetLoadoutSelections() {
  ['loadoutHelmet','loadoutArmor','loadoutRig','loadoutPistol','loadoutBackpack'].forEach(id => { if ($(id)) $(id).value = ''; });
  calculateLoadoutCost();
}
function calculateLoadoutCost() {
  let total = 0;
  ['loadoutHelmet','loadoutArmor','loadoutRig','loadoutPistol','loadoutBackpack'].forEach(id => {
    const v = $(id)?.value || '';
    if (v.includes(':')) total += Number(v.split(':')[1] || 0);
  });
  const costDisplay = document.getElementById('loadoutCostValue');
  if (costDisplay) costDisplay.textContent = (total / 10000).toFixed(1) + 'W';
  const costBox = document.getElementById('loadoutCostDisplay');
  if (costBox) {
    costBox.style.background = total > _adminMaxLoadoutCost ? '#fee2e2' : '#fef3c7';
    costBox.style.color = total > _adminMaxLoadoutCost ? '#991b1b' : '#92400e';
  }
  return total;
}
function calculateManualLoadoutCost() { return calculateLoadoutCost() / 10000; }
function autoCalculateRounds() {
  if (getManualEffectiveMode() !== 'absolute') return;
  const maxRoundsEl = document.getElementById('manualMaxRounds');
  if ((parseInt(maxRoundsEl.value) || 0) !== 0) return;
  const hours = parseInt(document.getElementById('manualHours').value) || 0;
  const minutes = parseInt(document.getElementById('manualMinutes').value) || 0;
  const totalMinutes = hours * 60 + minutes;
  if (totalMinutes > 0) maxRoundsEl.value = Math.max(_absoluteRoundsPerHour, Math.floor((totalMinutes / 60) * _absoluteRoundsPerHour));
}
function closeManualOrderModal() {
  document.getElementById('manualOrderModal').classList.remove('show');
}
async function submitManualOrder() {
  const deviceId = document.getElementById('manualDeviceId').value;
  const bossName = document.getElementById('manualBossName').value.trim().toUpperCase();
  const hours = parseInt(document.getElementById('manualHours').value) || 0;
  const minutes = parseInt(document.getElementById('manualMinutes').value) || 0;
  const runMinutes = hours * 60 + minutes;
  const maxRoundsInput = document.getElementById('manualMaxRounds').value.trim();
  const maxCoinLossInput = document.getElementById('manualMaxCoinLoss').value.trim();

  if (!bossName) { appAlert('请输入组队码'); return; }
  if (!validateBossName(bossName)) { appAlert('组队码格式错误：前3位为大写字母，后4位为数字（如 ABC1234）'); return; }
  if (runMinutes <= 0) { appAlert('请填写有效的运行时长'); return; }
  if (maxRoundsInput && !/^\\d+$/.test(maxRoundsInput)) { appAlert('限制局数必须为纯数字'); return; }
  if (maxCoinLossInput && !/^\\d+$/.test(maxCoinLossInput)) { appAlert('限制亏币必须为纯数字'); return; }

  const maxRounds = parseInt(maxRoundsInput) || 0;
  const maxCoinLoss = parseInt(maxCoinLossInput) || 0;
  const effectiveMode = getManualEffectiveMode();
  let loadoutType = 'default';
  let loadoutHelmet = '', loadoutArmor = '', loadoutRig = '', loadoutPistol = '', loadoutBackpack = '';
  let loadoutTotalCost = 0;
  if (effectiveMode === 'absolute') {
    loadoutType = document.querySelector('input[name="loadoutType"]:checked')?.value || 'default';
    if (!_adminAllowCustomLoadout) loadoutType = 'default';
    if (loadoutType === 'custom') {
      const pick = id => document.getElementById(id).value || '';
      const helmet = pick('loadoutHelmet'), armor = pick('loadoutArmor'), rig = pick('loadoutRig'), pistol = pick('loadoutPistol'), backpack = pick('loadoutBackpack');
      if (helmet) { loadoutHelmet = helmet.split(':')[0]; loadoutTotalCost += parseInt(helmet.split(':')[1]); }
      if (armor) { loadoutArmor = armor.split(':')[0]; loadoutTotalCost += parseInt(armor.split(':')[1]); }
      if (rig) { loadoutRig = rig.split(':')[0]; loadoutTotalCost += parseInt(rig.split(':')[1]); }
      if (pistol) { loadoutPistol = pistol.split(':')[0]; loadoutTotalCost += parseInt(pistol.split(':')[1]); }
      if (backpack) { loadoutBackpack = backpack.split(':')[0]; loadoutTotalCost += parseInt(backpack.split(':')[1]); }
      if (loadoutTotalCost > _adminMaxLoadoutCost) { appAlert(`配装总价不能超过${_adminMaxLoadoutCost / 10000}W`); return; }
    }
  }

  const btn = document.getElementById('manualOrderBtn');
  btn.disabled = true;
  btn.textContent = '提交中...';
  try {
    const data = await api('/api/admin/manual-order', {method:'POST', body:JSON.stringify({
      device_id: parseInt(deviceId),
      boss_name: bossName,
      selected_mode: _manualOrderMode === 'hybrid' ? effectiveMode : undefined,
      run_minutes: runMinutes,
      max_rounds: maxRounds,
      max_coin_loss: maxCoinLoss,
      loadout_type: loadoutType,
      loadout_helmet: loadoutHelmet,
      loadout_armor: loadoutArmor,
      loadout_rig: loadoutRig,
      loadout_pistol: loadoutPistol,
      loadout_backpack: loadoutBackpack,
      loadout_total_cost: loadoutTotalCost
    })});
    appAlert(`手动下单成功！运行时长: ${formatMinutes(data.run_minutes)}`);
    closeManualOrderModal();
    await loadDevicesAdmin(); await loadOrders(); await loadOverview();
  } catch(e) { appAlert(e.message || '网络错误'); }
  finally {
    btn.disabled = false;
    btn.textContent = '确认下单';
  }
}
function openAdminRejoin(orderId, oldTeam='') {
  $('rejoinOrderId').value = orderId;
  $('rejoinInfo').textContent = `订单 #${orderId}`;
  $('rejoinTeamCode').value = oldTeam || '';
  openModal('adminRejoinModal');
}
async function submitAdminRejoin() {
  const id = $('rejoinOrderId').value;
  try {
    await api('/api/admin/manual-rejoin/' + id, {method:'POST', body:JSON.stringify({boss_name:$('rejoinTeamCode').value.trim()})});
    closeModal('adminRejoinModal'); toast('换队指令已下发'); await loadDevicesAdmin(); await loadOrders();
  } catch(e) { toast(e.message); }
}
async function sendDeviceCommand(deviceId, action) {
  const ok = await appConfirm('设备直控确认', `确认向 ${deviceId} 号机下发 ${action} 指令？`, action === 'stop_current' ? 'btn-danger' : 'btn-primary');
  if (!ok) return;
  try {
    await api(`/api/admin/devices/${deviceId}/command`, {method:'POST', body:JSON.stringify({action, params:{operator:'merchant_admin'}})});
    toast('指令已下发'); await loadDevicesAdmin(); await loadOrders(); await loadOverview();
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
async function loadAdmins() {
  const d = await api('/api/admin/admins');
  const rows = d.admins || [];
  if (!rows.length) { $('adminsTable').innerHTML = '<div class="empty-state">暂无管理员</div>'; return; }
  $('adminsTable').innerHTML = `<table class="data-table"><thead><tr>
    <th>ID</th><th>用户名</th><th>角色</th><th>状态</th><th>最后登录</th><th>创建/更新</th><th>操作</th>
  </tr></thead><tbody>` + rows.map(a => {
    const encodedName = encodeURIComponent(a.username || '');
    const roleBadge = a.role === 'owner' ? '<span class="badge badge-purple">owner</span>' : '<span class="badge badge-waiting">operator</span>';
    const statusBadge = a.status === 'active' ? '<span class="badge badge-online">active</span>' : '<span class="badge badge-offline">disabled</span>';
    const actions = IS_OWNER ? `
      <button class="btn-sm btn-gray" onclick="openResetAdminPwd(${a.id}, decodeURIComponent('${encodedName}'))">改密码</button>
      <button class="btn-sm btn-primary" onclick="changeAdminRole(${a.id}, '${a.role === 'owner' ? 'operator' : 'owner'}')">${a.role === 'owner' ? '降为 operator' : '升为 owner'}</button>
      <button class="btn-sm ${a.status === 'active' ? 'btn-amber' : 'btn-green'}" onclick="changeAdminStatus(${a.id}, '${a.status === 'active' ? 'disabled' : 'active'}')">${a.status === 'active' ? '禁用' : '启用'}</button>
      <button class="btn-sm btn-danger" onclick="deleteAdmin(${a.id}, decodeURIComponent('${encodedName}'))">删除</button>` : '<span class="hint">仅 owner 可修改</span>';
    return `<tr>
      <td>${esc(a.id)}</td><td>${esc(a.username)}</td><td>${roleBadge}</td><td>${statusBadge}</td>
      <td>${fmtDate(a.last_login_at)}</td><td>${fmtDate(a.created_at)}<br><span class="hint">${fmtDate(a.updated_at)}</span></td><td>${actions}</td>
    </tr>`;
  }).join('') + '</tbody></table>';
}
function openAdminModal() {
  if (!IS_OWNER) { toast('仅 owner 可新建管理员'); return; }
  $('adminNewUsername').value = '';
  $('adminNewPassword').value = '123456';
  $('adminNewRole').value = 'operator';
  $('adminNewStatus').value = 'active';
  openModal('adminModal');
}
async function submitCreateAdmin() {
  try {
    await api('/api/admin/admins', {method:'POST', body:JSON.stringify({
      username: $('adminNewUsername').value.trim(),
      password: $('adminNewPassword').value,
      role: $('adminNewRole').value,
      status: $('adminNewStatus').value,
    })});
    closeModal('adminModal'); toast('管理员已创建'); await loadAdmins(); await loadAuditLogs();
  } catch(e) { toast(e.message); }
}
function openResetAdminPwd(id, name) {
  if (!IS_OWNER) { toast('仅 owner 可修改管理员密码'); return; }
  $('adminPwdId').value = id;
  $('adminPwdUsername').value = name || id;
  $('adminPwdNew').value = '123456';
  openModal('adminPwdModal');
}
async function submitResetAdminPwd() {
  try {
    await api('/api/admin/admins/' + $('adminPwdId').value + '/password', {method:'PUT', body:JSON.stringify({password:$('adminPwdNew').value})});
    closeModal('adminPwdModal'); toast('管理员密码已重置'); await loadAdmins(); await loadAuditLogs();
  } catch(e) { toast(e.message); }
}
async function changeAdminRole(id, role) {
  const ok = await appConfirm('修改管理员角色', `确认将管理员 #${id} 修改为 ${role}？`, role === 'owner' ? 'btn-primary' : 'btn-amber');
  if (!ok) return;
  try { await api('/api/admin/admins/' + id + '/role', {method:'PUT', body:JSON.stringify({role})}); toast('管理员角色已更新'); await loadAdmins(); await loadAuditLogs(); }
  catch(e) { toast(e.message); }
}
async function changeAdminStatus(id, status) {
  const ok = await appConfirm('修改管理员状态', `确认将管理员 #${id} 修改为 ${status}？`, status === 'active' ? 'btn-green' : 'btn-amber');
  if (!ok) return;
  try { await api('/api/admin/admins/' + id + '/status', {method:'PUT', body:JSON.stringify({status})}); toast('管理员状态已更新'); await loadAdmins(); await loadAuditLogs(); }
  catch(e) { toast(e.message); }
}
async function deleteAdmin(id, name) {
  const ok = await appConfirm('删除管理员', `确认删除管理员「${name || id}」？其后台会话会同时失效。`, 'btn-danger');
  if (!ok) return;
  try { await api('/api/admin/admins/' + id, {method:'DELETE'}); toast('管理员已删除'); await loadAdmins(); await loadAuditLogs(); }
  catch(e) { toast(e.message); }
}
async function loadAuditLogs() {
  const d = await api('/api/admin/audit-logs?limit=300');
  const rows = d.logs || [];
  if (!rows.length) { $('auditTable').innerHTML = '<div class="empty-state">暂无审计记录</div>'; return; }
  $('auditTable').innerHTML = `<table class="data-table"><thead><tr>
    <th>ID</th><th>时间</th><th>管理员</th><th>动作</th><th>资源</th><th>详情</th>
  </tr></thead><tbody>` + rows.map(l => `<tr>
    <td>${esc(l.id)}</td><td>${fmtDate(l.created_at)}</td><td>${esc(l.admin_username || '-')}</td>
    <td><span class="badge badge-purple">${esc(l.action)}</span></td><td>${esc(l.resource_type)} #${esc(l.resource_id || '-')}</td>
    <td><pre style="white-space:pre-wrap;margin:0;font-size:11px">${esc(JSON.stringify(l.metadata || {}, null, 2))}</pre></td>
  </tr>`).join('') + '</tbody></table>';
}
async function loadBackups() {
  const d = await api('/api/admin/backup');
  const rows = d.backups || [];
  if (!rows.length) { $('backupTable').innerHTML = '<div class="empty-state">暂无备份</div>'; return; }
  $('backupTable').innerHTML = `<table class="data-table"><thead><tr>
    <th>文件</th><th>大小</th><th>修改时间</th><th>类型</th><th>操作</th>
  </tr></thead><tbody>` + rows.map(b => `<tr>
    <td style="font-family:Consolas,monospace">${esc(b.name)}</td><td>${esc(b.size_kb)} KB</td><td>${fmtDate(b.modified_at)}</td>
    <td>${b.current ? '<span class="badge badge-online">当前数据库</span>' : '<span class="badge badge-purple">备份</span>'}</td>
    <td>${b.current ? '<span class="hint">不可下载当前活动库</span>' : `<a class="btn-sm btn-primary" href="/api/admin/backup/${encodeURIComponent(b.name)}">下载</a><button class="btn-sm btn-danger" onclick="restoreBackup(decodeURIComponent('${encodeURIComponent(b.name)}'))">恢复</button>`}</td>
  </tr>`).join('') + '</tbody></table>';
}
async function createBackup() {
  try { await api('/api/admin/backup', {method:'POST', body:'{}'}); toast('备份成功'); await loadBackups(); await loadAuditLogs(); }
  catch(e) { toast(e.message); }
}
async function restoreBackup(name) {
  const ok = await appConfirm('恢复数据库', `确定恢复备份「${name}」？\n系统会先创建 pre_restore 备份，恢复后建议重启服务。`, 'btn-danger');
  if (!ok) return;
  try { await api('/api/admin/backup/' + encodeURIComponent(name) + '/restore', {method:'POST', body:'{}'}); toast('恢复成功，请重启服务'); await loadBackups(); await loadAuditLogs(); }
  catch(e) { toast(e.message); }
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
    return template.replace("__SYSTEM_NAME__", system_name).replace("__ADMIN__", _escape(admin.get("username"))).replace("__ROLE__", _escape(admin.get("role")))
