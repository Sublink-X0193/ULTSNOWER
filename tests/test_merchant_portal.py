from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from zoneinfo import ZoneInfo
from typing import Any

import pytest
from fastapi.testclient import TestClient

from merchant_portal_server.app import create_app
from merchant_portal_server.bridge_client import BridgeClientError
from merchant_portal_server.config import Settings
from merchant_portal_server.db import Database, iso, utcnow
from merchant_portal_server.service import MerchantError, MerchantService


class FakeBridge:
    def __init__(self, capacity: int = 3):
        self.lock = threading.Lock()
        self.idle = list(range(1, capacity + 1))
        self.sessions: dict[str, dict[str, Any]] = {}
        self.session_requests: list[dict[str, Any]] = []
        self.events_log: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.renew_calls: list[str] = []
        self.seq = 0

    def _event(self, event: str, session_id: str | None, *, command_id: str | None = None, device_id: int | None = None, device_epoch: int | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.seq += 1
        ev = {
            "id": f"evt_{self.seq}",
            "event_seq": self.seq,
            "event": event,
            "control_session_id": session_id,
            "command_id": command_id,
            "device_id": device_id,
            "device_epoch": device_epoch,
            "payload": payload or {},
            "created_at": iso(),
        }
        self.events_log.append(ev)
        return ev

    def get_capacity(self) -> dict[str, Any]:
        with self.lock:
            n = len(self.idle)
            return {"ok": True, "available": bool(n), "capacity_label": "many" if n >= 3 else ("few" if n else "full"), "idle_device_ids": list(self.idle)}

    def create_control_session(
        self,
        *,
        merchant_context_ref: str,
        idem: str,
        device_id: int | None = None,
        auto_assign: bool = True,
        technical_lease_ttl_seconds: int = 180,
        selection_policy: dict[str, Any] | None = None,
        purpose: str = "customer_control",
        expected_device_state: str = "idle",
        takeover_policy: str = "reject",
    ) -> dict[str, Any]:
        with self.lock:
            self.session_requests.append({
                "merchant_context_ref": merchant_context_ref,
                "idem": idem,
                "device_id": device_id,
                "auto_assign": auto_assign,
                "technical_lease_ttl_seconds": technical_lease_ttl_seconds,
                "selection_policy": dict(selection_policy or {}),
                "purpose": purpose,
                "expected_device_state": expected_device_state,
                "takeover_policy": takeover_policy,
            })
            if not self.idle:
                raise BridgeClientError("device_not_available", "没有可用设备", 409)
            if device_id is not None:
                if int(device_id) not in self.idle:
                    raise BridgeClientError("device_not_available", "没有可用设备", 409)
                self.idle.remove(int(device_id))
                did = int(device_id)
            else:
                did = self.idle.pop(0)
            sid = f"cs_{len(self.sessions)+1}"
            sess = {"control_session_id": sid, "device_id": did, "fencing_token": f"ft_{sid}", "status": "active", "device_epoch": 1, "merchant_context_ref": merchant_context_ref}
            self.sessions[sid] = sess
            self._event("control_session.created", sid, device_id=did, device_epoch=1, payload={"purpose": "customer_control"})
            return dict(sess)

    def queue_command_bundle(self, session_id: str, *, fencing_token: str, expected_device_epoch: int | None, team_code: str, quality: str, idem: str, ace_enabled: bool = False, max_rounds: int = 0, max_coin_loss: int = 0, loadout: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            if session_id not in self.sessions:
                raise BridgeClientError("not_found", "session missing", 404)
            bundle_id = f"bundle_{session_id}"
            out = []
            for action in ["set_loadout", "enter_team", "ready", "watch"]:
                if action == "watch":
                    params = {"ace_enabled": bool(ace_enabled), "ace_window_seconds": 120, "max_rounds": int(max_rounds or 0), "max_coin_loss_w": int(max_coin_loss or 0)}
                elif action == "set_loadout":
                    params = dict(loadout or {})
                else:
                    params = {}
                cmd = {"command_id": f"cmd_{len(self.commands)+1}", "control_session_id": session_id, "action": action, "params": params, "status": "queued", "bundle_id": bundle_id}
                self.commands.append(cmd)
                out.append(dict(cmd))
            return {"bundle_id": bundle_id, "commands": out}

    def queue_stop(self, session_id: str, *, fencing_token: str, idem: str, reason: str = "merchant_order_finished") -> dict[str, Any]:
        with self.lock:
            cmd = {"command_id": f"cmd_{len(self.commands)+1}", "control_session_id": session_id, "action": "stop_current", "status": "queued"}
            self.commands.append(cmd)
            self._event("command.queued", session_id, command_id=cmd["command_id"], device_id=self.sessions[session_id]["device_id"], payload={"action": "stop_current"})
            return dict(cmd)

    def queue_command(self, session_id: str, *, fencing_token: str, action: str, params: dict[str, Any] | None = None, expected_device_epoch: int | None = None, idem: str) -> dict[str, Any]:
        with self.lock:
            cmd = {"command_id": f"cmd_{len(self.commands)+1}", "control_session_id": session_id, "action": action, "params": dict(params or {}), "status": "queued"}
            self.commands.append(cmd)
            self._event("command.queued", session_id, command_id=cmd["command_id"], device_id=self.sessions[session_id]["device_id"], payload={"action": action})
            return dict(cmd)

    def renew_session(self, session_id: str, *, fencing_token: str, idem: str, ttl_seconds: int = 180) -> dict[str, Any]:
        with self.lock:
            self.renew_calls.append(session_id)
            if session_id not in self.sessions:
                raise BridgeClientError("not_found", "session missing", 404)
            return {"control_session_id": session_id, "status": "active"}

    def events(self, *, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        with self.lock:
            events = [e for e in self.events_log if e["event_seq"] > cursor][:limit]
            return {"ok": True, "events": [dict(e) for e in events], "next_cursor": events[-1]["event_seq"] if events else cursor}

    def session_state(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            if session_id not in self.sessions:
                raise BridgeClientError("not_found", "session missing", 404)
            return dict(self.sessions[session_id])

    def push_ready(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            sess = self.sessions[session_id]
            sess["device_epoch"] += 1
            return self._event("device.ready_for_customer_timer", session_id, device_id=sess["device_id"], device_epoch=sess["device_epoch"], payload={"basis": "watch_succeeded"})

    def push_stop_succeeded(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            sess = self.sessions[session_id]
            sess["device_epoch"] += 1
            ev = self._event("command.succeeded", session_id, command_id="cmd_stop", device_id=sess["device_id"], device_epoch=sess["device_epoch"], payload={"action": "stop_current"})
            if sess["device_id"] not in self.idle:
                self.idle.append(sess["device_id"])
            sess["status"] = "released"
            self._event("control_session.released", session_id, device_id=sess["device_id"], device_epoch=sess["device_epoch"] + 1, payload={"reason": "merchant_order_closed"})
            return ev

    def push_admin_takeover(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            sess = self.sessions[session_id]
            sess["device_epoch"] += 1
            sess["status"] = "force_taken_over"
            return self._event("admin.takeover", session_id, device_id=sess["device_id"], device_epoch=sess["device_epoch"], payload={"reason": "maintenance"})

    def push_expired(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            sess = self.sessions[session_id]
            sess["status"] = "expired"
            return self._event("control_session.expired", session_id, device_id=sess["device_id"], device_epoch=sess["device_epoch"] + 1, payload={"reason": "renew_timeout"})


@pytest.fixture()
def app_and_bridge(tmp_path):
    bridge = FakeBridge(capacity=3)
    app = create_app(db_path=tmp_path / "merchant.sqlite", bridge_client=bridge)
    return app, bridge


def register_and_login(client: TestClient, username: str = "alice") -> dict[str, Any]:
    client.get("/api/captcha")
    captcha = (client.cookies.get("merchant_register_captcha") or "").split(":", 1)[0]
    r = client.post("/api/register", json={"username": username, "password": "123456", "captcha": captcha})
    assert r.status_code == 200, r.text
    return r.json()["customer"]


def test_register_login_recharge(app_and_bridge):
    app, _bridge = app_and_bridge
    client = TestClient(app)
    captcha_svg = client.get("/api/captcha")
    assert captcha_svg.status_code == 200
    assert "LOCAL" not in captcha_svg.text
    bad = client.post("/api/register", json={"username": "captcha_bad", "password": "123456", "captcha": "BAD1"})
    assert bad.status_code == 400
    assert bad.json()["error"] == "bad_captcha"
    customer = register_and_login(client)
    app.state.service.add_recharge_card("CARD-100", minutes=100)
    r = client.post("/api/recharge/redeem", json={"code": "CARD-100"})
    assert r.status_code == 200, r.text
    assert r.json()["customer"]["balance_minutes"] == 100
    assert r.json()["customer"]["balance_machine_minutes"] == 100
    assert r.json()["customer"]["balance_absolute_minutes"] == 0
    assert client.get("/api/me").json()["customer"]["username"] == customer["username"]


def test_order_waits_for_ready_timer_before_running(app_and_bridge):
    app, bridge = app_and_bridge
    client = TestClient(app)
    register_and_login(client)
    app.state.service.add_recharge_card("CARD-30", minutes=30, mode="absolute")
    client.post("/api/recharge/redeem", json={"code": "CARD-30"})

    r = client.post("/api/orders", json={"requested_minutes": 10, "team_code": "JYG4545", "quality": "secret"}, headers={"X-Idempotency-Key": "order-1"})
    assert r.status_code == 200, r.text
    order = r.json()["order"]
    assert order["status"] == "waiting_ready_timer"
    assert order["started_at"] is None

    bridge.push_ready(order["binding"]["control_session_id"])
    poll = client.post("/internal/workers/events").json()
    assert poll["processed"] >= 1
    running = client.get("/api/orders/current").json()["order"]
    assert running["status"] == "running"
    assert running["started_at"]
    assert running["end_at"]


def test_order_expire_sends_stop_and_stop_success_finishes(app_and_bridge):
    app, bridge = app_and_bridge
    service: MerchantService = app.state.service
    client = TestClient(app)
    register_and_login(client)
    service.add_recharge_card("CARD-5", minutes=5)
    client.post("/api/recharge/redeem", json={"code": "CARD-5"})
    order = client.post("/api/orders", json={"requested_minutes": 1, "team_code": "ABC123"}).json()["order"]
    bridge.push_ready(order["binding"]["control_session_id"])
    client.post("/internal/workers/events")

    with app.state.db.connect() as con:
        con.execute("UPDATE local_orders SET end_at=? WHERE id=?", (iso(utcnow() - timedelta(seconds=1)), order["id"]))
    exp = client.post("/internal/workers/order-expire").json()
    assert exp["stop_sent"] == 1
    assert bridge.commands[-1]["action"] == "stop_current"

    bridge.push_stop_succeeded(order["binding"]["control_session_id"])
    client.post("/internal/workers/events")
    cur = client.get("/api/orders/current").json()["order"]
    assert cur is None
    hist = client.get("/api/orders/history").json()["orders"][0]
    assert hist["status"] == "finished"


def test_event_replay_does_not_double_refund(app_and_bridge):
    app, bridge = app_and_bridge
    client = TestClient(app)
    customer = register_and_login(client)
    app.state.service.add_recharge_card("CARD-20", minutes=20)
    client.post("/api/recharge/redeem", json={"code": "CARD-20"})
    order = client.post("/api/orders", json={"requested_minutes": 10, "team_code": "REPLAY"}).json()["order"]
    ev = bridge.push_admin_takeover(order["binding"]["control_session_id"])
    app.state.service.process_bridge_event(ev)
    app.state.service.process_bridge_event(ev)
    assert app.state.service.get_customer(customer["id"])["balance_minutes"] == 20
    hist = client.get("/api/orders/history").json()["orders"][0]
    assert hist["status"] == "interrupted_by_admin"


def test_disconnect_expired_event_compensates_order(app_and_bridge):
    app, bridge = app_and_bridge
    client = TestClient(app)
    customer = register_and_login(client)
    app.state.service.add_recharge_card("CARD-40", minutes=40)
    client.post("/api/recharge/redeem", json={"code": "CARD-40"})
    order = client.post("/api/orders", json={"requested_minutes": 15, "team_code": "DISC1"}).json()["order"]
    ev = bridge.push_expired(order["binding"]["control_session_id"])
    app.state.service.process_bridge_event(ev)
    assert app.state.service.get_customer(customer["id"])["balance_minutes"] == 40
    assert client.get("/api/orders/history").json()["orders"][0]["status"] == "interrupted_by_disconnect"


def test_same_customer_duplicate_click_reuses_active_order(tmp_path):
    bridge = FakeBridge(capacity=5)
    db = Database(tmp_path / "merchant.sqlite")
    service = MerchantService(db, bridge)
    customer = service.register_customer("dupe", "123456")
    service.add_recharge_card("DUP-100", minutes=100)
    service.redeem_card(customer["id"], "DUP-100")

    def place(i: int):
        return service.place_order(customer["id"], requested_minutes=10, team_code="DUP123", idempotency_key=f"click-{i}")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(place, range(20)))

    ids = {r["order"]["id"] for r in results}
    assert len(ids) == 1
    assert service.get_customer(customer["id"])["balance_minutes"] == 90


def test_50_concurrent_customers_compete_for_two_devices(tmp_path):
    bridge = FakeBridge(capacity=2)
    db = Database(tmp_path / "merchant.sqlite")
    service = MerchantService(db, bridge)
    customers = []
    for i in range(50):
        c = service.register_customer(f"u{i:02d}", "123456")
        service.add_recharge_card(f"CARD-{i}", minutes=10)
        service.redeem_card(c["id"], f"CARD-{i}")
        customers.append(c)

    def place(c):
        return service.place_order(c["id"], requested_minutes=10, team_code=f"TEAM{c['id']}")

    with ThreadPoolExecutor(max_workers=50) as pool:
        results = list(pool.map(place, customers))

    waiting = [r for r in results if r["order"]["status"] == "waiting_ready_timer"]
    failed = [r for r in results if r["order"]["status"] == "failed"]
    assert len(waiting) == 2
    assert len(failed) == 48
    balances = [service.get_customer(c["id"])["balance_minutes"] for c in customers]
    assert balances.count(0) == 2
    assert balances.count(10) == 48


def test_session_renew_worker(app_and_bridge):
    app, bridge = app_and_bridge
    client = TestClient(app)
    register_and_login(client)
    app.state.service.add_recharge_card("CARD-60", minutes=60)
    client.post("/api/recharge/redeem", json={"code": "CARD-60"})
    order = client.post("/api/orders", json={"requested_minutes": 10, "team_code": "RNW1"}).json()["order"]
    bridge.push_ready(order["binding"]["control_session_id"])
    client.post("/internal/workers/events")
    res = client.post("/internal/workers/session-renew").json()
    assert res["renewed"] == 1
    assert bridge.renew_calls == [order["binding"]["control_session_id"]]


def test_admin_settings_privacy_announcement_and_maintenance(app_and_bridge):
    app, _bridge = app_and_bridge
    client = TestClient(app)

    login = client.post("/api/admin/login", json={"username": "admin", "password": "admin123456"})
    assert login.status_code == 200, login.text
    saved = client.put(
        "/api/admin/settings",
        json={
            "system_name": "SNOW 商户自助",
            "default_limit_rounds": 5,
            "absolute_rounds_per_hour": 3,
            "night_time_check": True,
            "night_start_time": "22:50",
            "night_end_time": "06:10",
            "global_radar_url": "https://example.local/radar",
            "privacy_skip_balance": 12,
            "ace_enabled": True,
            "privacy_mode_enabled": True,
            "maintenance_mode_enabled": False,
            "announcement_enabled": True,
            "announcement_text": "今晚 22:00 维护",
        },
    )
    assert saved.status_code == 200, saved.text
    public_settings = client.get("/api/public/settings").json()["settings"]
    assert public_settings["system_name"] == "SNOW 商户自助"
    assert public_settings["privacy_mode_enabled"] is True
    assert public_settings["announcement_text"] == "今晚 22:00 维护"
    assert public_settings["global_radar_url"] == "https://example.local/radar"
    assert public_settings["night_start_time"] == "22:50"
    admin_settings = client.get("/api/admin/settings").json()["settings"]
    assert admin_settings["default_limit_rounds"] == 5
    assert admin_settings["absolute_rounds_per_hour"] == 3
    assert admin_settings["night_time_check"] is True
    assert admin_settings["global_radar_url"] == "https://example.local/radar"
    assert admin_settings["privacy_skip_balance"] == 12
    assert admin_settings["ace_enabled"] is True

    register_and_login(client, "privacy_user")
    app.state.service.add_recharge_card("PRIV-10", minutes=10)
    client.post("/api/recharge/redeem", json={"code": "PRIV-10"})
    order = client.post("/api/orders", json={"requested_minutes": 5, "team_code": "SECRETTEAM"}).json()["order"]
    assert order["team_code"] != "SECRETTEAM"
    assert order["team_code_masked"] is True
    assert "fencing_token" not in order["binding"]
    assert "merchant_context_ref" not in order["binding"]

    saved = client.put(
        "/api/admin/settings",
        json={
            "privacy_mode_enabled": True,
            "maintenance_mode_enabled": True,
            "maintenance_message": "系统升级中，预计 22:00 恢复",
            "announcement_enabled": True,
            "announcement_text": "维护中",
        },
    )
    assert saved.status_code == 200
    register_and_login(client, "blocked_user")
    app.state.service.add_recharge_card("MAINT-10", minutes=10)
    client.post("/api/recharge/redeem", json={"code": "MAINT-10"})
    blocked = client.post("/api/orders", json={"requested_minutes": 5, "team_code": "MAINT"})
    assert blocked.status_code == 503
    assert blocked.json()["error"] == "maintenance_mode"
    assert blocked.json()["message"] == "系统升级中，预计 22:00 恢复"


def test_admin_card_generation_listing_export_and_delete(app_and_bridge):
    app, _bridge = app_and_bridge
    client = TestClient(app)
    assert client.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200
    client.put("/api/admin/settings", json={"default_limit_rounds": 4, "absolute_rounds_per_hour": 3})

    gen = client.post("/api/admin/cards/generate", json={"mode": "absolute", "minutes": 120, "count": 2, "card_type": "normal"})
    assert gen.status_code == 200, gen.text
    cards = gen.json()["cards"]
    assert len(cards) == 2
    assert cards[0]["rounds"] == 6

    listed = client.get("/api/admin/cards?status=unused").json()["cards"]
    assert any(c["card_code"] == cards[0]["card_code"] for c in listed)

    export = client.get("/api/admin/cards/export-unused")
    assert export.status_code == 200
    assert cards[0]["card_code"] in export.text

    deleted = client.delete(f"/api/admin/cards/{cards[0]['card_code']}")
    assert deleted.status_code == 200, deleted.text
    listed_after = client.get("/api/admin/cards?status=unused").json()["cards"]
    assert not any(c["card_code"] == cards[0]["card_code"] for c in listed_after)


def test_admin_settings_legacy_ui_and_post_compatibility(app_and_bridge):
    app, _bridge = app_and_bridge
    client = TestClient(app)
    assert client.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200

    html = client.get("/merchant-admin").text
    assert '登录页与注册页显示的系统名称，留空时默认使用"管理员用户名前3位+电竞"' in html
    assert "自动分配采用排钟逻辑" in html
    assert "下雪反作弊系统 XX-ACE（订单反白嫖）" in html
    assert 'id="settingPrivacyMode"' in html
    assert 'id="settingMaintenanceMode"' in html
    assert 'id="nightTimeRangeField"' in html
    assert "系统设置" in html

    posted = client.post(
        "/api/admin/settings",
        json={
            "system_name": "旧版设置兼容",
            "privacy_mode": "0",
            "maintenance_mode": "1",
            "night_time_check": "0",
            "ace_enabled": "1",
            "default_limit_rounds": "6",
            "absolute_rounds_per_hour": "2",
            "global_radar_url": "http://8.148.233.14:5000/",
        },
    )
    assert posted.status_code == 200, posted.text
    settings = client.get("/api/admin/settings").json()["settings"]
    assert settings["system_name"] == "旧版设置兼容"
    assert settings["privacy_mode_enabled"] is False
    assert settings["privacy_mode"] == "0"
    assert settings["maintenance_mode_enabled"] is True
    assert settings["maintenance_mode"] == "1"
    assert settings["night_time_check"] is False
    assert settings["ace_enabled"] is True
    assert settings["default_limit_rounds"] == 6
    assert settings["absolute_rounds_per_hour"] == 2

    notice = client.post("/api/admin/notice", json={"content": "<b>公告</b>"})
    assert notice.status_code == 200
    assert client.get("/api/notice").json()["content"] == "<b>公告</b>"

    renamed = client.post("/api/admin/settings", json={"system_name": "七元电竞"})
    assert renamed.status_code == 200
    admin_html = client.get("/merchant-admin").text
    assert "七元电竞 · 商户管理后台" in admin_html
    assert "SNOW 商户服务器 · 管理后台" not in admin_html


def test_customer_usage_settings_are_merchant_owned_and_applied(app_and_bridge):
    app, bridge = app_and_bridge
    admin_client = TestClient(app)
    assert admin_client.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200
    saved = admin_client.post(
        "/api/admin/settings",
        json={
            "privacy_mode": "1",
            "privacy_skip_balance": "8",
            "ace_enabled": "1",
        },
    )
    assert saved.status_code == 200, saved.text

    user_client = TestClient(app)
    register_and_login(user_client, "policy_user")
    app.state.service.add_recharge_card("POLICY-60", minutes=60, mode="absolute")
    assert user_client.post("/api/recharge/redeem", json={"code": "POLICY-60"}).status_code == 200
    order = user_client.post("/api/orders", json={"requested_minutes": 10, "team_code": "POLICY", "quality": "secret"}).json()["order"]
    assert order["status"] == "waiting_ready_timer"

    policy = bridge.session_requests[-1]["selection_policy"]
    assert policy["source"] == "merchant_settings"
    assert policy["privacy_mode"] is True
    assert policy["privacy_skip_balance_w"] == 8
    assert policy["min_device_coin_balance"] == 80000
    assert policy["order_quality"] == "secret"
    assert any(cmd["action"] == "watch" and cmd["params"]["ace_enabled"] is True for cmd in bridge.commands)


def test_night_card_time_check_is_enforced_by_merchant_server(app_and_bridge):
    app, _bridge = app_and_bridge
    admin_client = TestClient(app)
    assert admin_client.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200
    local_now = utcnow().astimezone(ZoneInfo("Asia/Shanghai"))
    start = (local_now + timedelta(hours=2)).strftime("%H:%M")
    end = (local_now + timedelta(hours=3)).strftime("%H:%M")
    saved = admin_client.post(
        "/api/admin/settings",
        json={"night_time_check": "1", "night_start_time": start, "night_end_time": end},
    )
    assert saved.status_code == 200, saved.text

    user_client = TestClient(app)
    register_and_login(user_client, "night_user")
    app.state.service.add_recharge_card("NIGHT-BLOCK", minutes=480, card_type="night")
    blocked = user_client.post("/api/recharge/redeem", json={"code": "NIGHT-BLOCK"})
    assert blocked.status_code == 403
    assert blocked.json()["error"] == "night_time_not_allowed"

    saved = admin_client.post("/api/admin/settings", json={"night_time_check": "0"})
    assert saved.status_code == 200, saved.text
    app.state.service.add_recharge_card("NIGHT-LOGIN-OK", minutes=480, card_type="night")
    night_client = TestClient(app)
    logged = night_client.post("/api/night-login", json={"card_code": "NIGHT-LOGIN-OK"})
    assert logged.status_code == 200, logged.text
    assert logged.json()["role"] == "night_card"
    assert night_client.get("/api/balance").json()["role"] == "night_card"


def test_admin_equipment_config_roundtrip(app_and_bridge):
    app, _bridge = app_and_bridge
    client = TestClient(app)
    assert client.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200

    cfg = client.get("/api/admin/equipment-config")
    assert cfg.status_code == 200
    payload = cfg.json()
    equipment = payload["equipment"]
    assert any(e["equipment_name"] == "五级夜视头" for e in equipment)
    first = dict(equipment[0])
    first["price"] = 12
    first["enabled"] = 1

    saved = client.post("/api/admin/equipment-config", json={"equipment": [first], "max_loadout_cost": 80, "allow_custom_loadout": False})
    assert saved.status_code == 200, saved.text

    cfg2 = client.get("/api/admin/equipment-config").json()
    assert cfg2["max_loadout_cost"] == 80
    assert cfg2["allow_custom_loadout"] is False
    assert any(e["equipment_name"] == first["equipment_name"] and e["price"] == 12 and e["enabled"] == 1 for e in cfg2["equipment"])


def test_html_pages_escape_user_controlled_values(app_and_bridge):
    app, _bridge = app_and_bridge
    client = TestClient(app)
    register_and_login(client, "<b>evil</b>")
    app.state.service.add_recharge_card("ESC-10", minutes=10)
    client.post("/api/recharge/redeem", json={"code": "ESC-10"})
    client.post("/api/orders", json={"requested_minutes": 5, "team_code": "<TAG>"})

    home = client.get("/").text
    current = client.get("/orders/current").text
    history = client.get("/orders/history").text

    assert "<b>evil</b>" not in home
    assert "&lt;b&gt;evil&lt;/b&gt;" in home
    assert "<TAG>" not in current
    assert "&lt;TAG&gt;" in current
    assert "<TAG>" not in history


def test_admin_customer_and_order_management_surfaces(app_and_bridge):
    app, bridge = app_and_bridge
    admin_client = TestClient(app)
    assert admin_client.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200

    created = admin_client.post("/api/admin/customers", json={"username": "managed", "password": "123456", "balance_minutes": 60})
    assert created.status_code == 200, created.text
    customer = created.json()["customer"]
    listed = admin_client.get("/api/admin/customers").json()["customers"]
    assert any(c["username"] == "managed" and c["balance_minutes"] == 60 for c in listed)

    updated = admin_client.put(f"/api/admin/customers/{customer['id']}/balance", json={"delta_minutes": 30})
    assert updated.status_code == 200
    assert updated.json()["customer"]["balance_minutes"] == 90

    user_client = TestClient(app)
    assert user_client.post("/api/login", json={"username": "managed", "password": "123456"}).status_code == 200
    online = admin_client.get("/api/admin/customers?online_only=true").json()["customers"]
    assert any(c["id"] == customer["id"] and c["online"] for c in online)

    order = user_client.post("/api/orders", json={"requested_minutes": 10, "team_code": "MGD123"}).json()["order"]
    bridge.push_ready(order["binding"]["control_session_id"])
    user_client.post("/internal/workers/events")

    orders = admin_client.get("/api/admin/orders?status=running").json()["orders"]
    assert any(o["id"] == order["id"] and o["remaining_minutes"] > 0 for o in orders)

    adjusted = admin_client.post(f"/api/admin/orders/{order['id']}/add-time", json={"add_minutes": 15})
    assert adjusted.status_code == 200
    assert adjusted.json()["order"]["requested_minutes"] == 25

    html = admin_client.get("/merchant-admin").text
    assert "所有客户预览 / 账户管理" in html
    assert "目前在线客户预览" in html
    assert "订单管理 / 剩余时长显示修改" in html


def test_legacy_customer_login_portal_and_api_compatibility(app_and_bridge):
    app, bridge = app_and_bridge
    client = TestClient(app)

    login_html = client.get("/login").text
    assert "账号登录" in login_html
    assert "包夜卡登录" in login_html
    assert "/api/night-login" in login_html

    register_html = client.get("/register").text
    assert "验证码" in register_html
    assert "/api/register" in register_html

    client.get("/api/captcha")
    captcha = (client.cookies.get("merchant_register_captcha") or "").split(":", 1)[0]
    registered = client.post("/api/register", json={"username": "legacy_user", "password": "123456", "captcha": captcha})
    assert registered.status_code == 200, registered.text
    customer_html = client.get("/customer").text
    assert "设备列表" in customer_html
    assert "我的订单" in customer_html
    assert "卡密充值" in customer_html
    assert "/api/devices/status" in customer_html
    assert "/api/orders/mine" in customer_html

    app.state.service.add_recharge_card("LEGACY-CARD", minutes=30, rounds=2)
    recharge = client.post("/api/recharge", json={"card_code": "LEGACY-CARD"})
    assert recharge.status_code == 200, recharge.text
    assert recharge.json()["minutes"] == 30
    balance = client.get("/api/balance").json()
    assert balance["balance_machine"] == 30
    assert balance["balance_absolute"] == 0
    assert balance["balance_machine_rounds"] == 2
    assert balance["balance_absolute_rounds"] == 0

    app.state.service.add_recharge_card("LEGACY-ABS", minutes=45, rounds=3, mode="absolute")
    recharge_abs = client.post("/api/recharge", json={"card_code": "LEGACY-ABS"})
    assert recharge_abs.status_code == 200, recharge_abs.text
    balance2 = client.get("/api/balance").json()
    assert balance2["balance_machine"] == 30
    assert balance2["balance_absolute"] == 45
    assert balance2["balance_machine_rounds"] == 2
    assert balance2["balance_absolute_rounds"] == 3

    devices = client.get("/api/devices/status").json()
    assert devices["ok"] is True
    assert devices["devices"]
    assert devices["devices"][0]["work_status"] == "空闲"

    order = client.post("/api/order", json={"boss_name": "ABC1234", "mode": "machine"})
    assert order.status_code == 200, order.text
    payload = order.json()
    assert payload["run_minutes"] == 30
    assert payload["run_rounds"] == 2
    order_id = payload["order_id"]
    mine = client.get("/api/orders/mine").json()["orders"]
    assert any(o["id"] == order_id and o["boss_name"] == "ABC1234" and o["status"] == "running" for o in mine)

    rejoin = client.post(f"/api/order/{order_id}/rejoin", json={"boss_name": "DEF1234"})
    assert rejoin.status_code == 200, rejoin.text
    assert rejoin.json()["order"]["boss_name"] == "DEF1234"

    stopped = client.post(f"/api/order/{order_id}/stop")
    assert stopped.status_code == 200, stopped.text
    assert bridge.commands[-1]["action"] == "stop_current"


def test_customer_online_uses_token_or_active_order_and_records_activity(app_and_bridge):
    app, bridge = app_and_bridge
    admin = TestClient(app)
    assert admin.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200
    created = admin.post("/api/admin/customers", json={"username": "presence", "password": "123456", "balance_minutes": 60})
    customer = created.json()["customer"]

    user = TestClient(app)
    assert user.post("/api/login", json={"username": "presence", "password": "123456"}).status_code == 200
    with app.state.db.connect() as con:
        sid_row = con.execute("SELECT sid,expires_at FROM sessions WHERE customer_id=?", (customer["id"],)).fetchone()
        old_exp = sid_row["expires_at"]
        con.execute(
            "UPDATE sessions SET expires_at=?,last_seen_at=? WHERE sid=?",
            (iso(utcnow() + timedelta(seconds=60)), iso(utcnow() - timedelta(hours=2)), sid_row["sid"]),
        )
    assert user.get("/api/me").status_code == 200
    with app.state.db.connect() as con:
        renewed = con.execute("SELECT expires_at,last_seen_at FROM sessions WHERE sid=?", (sid_row["sid"],)).fetchone()
    assert renewed["expires_at"] > old_exp

    online = admin.get("/api/admin/customers?online_only=true").json()["customers"]
    assert any(c["id"] == customer["id"] and c["online"] and "token" in c["online_reason"] for c in online)

    order = user.post("/api/orders", json={"requested_minutes": 10, "team_code": "PRS123"}).json()["order"]
    bridge.push_ready(order["binding"]["control_session_id"])
    user.post("/internal/workers/events")
    with app.state.db.connect() as con:
        con.execute("UPDATE sessions SET expires_at=? WHERE customer_id=?", (iso(utcnow() - timedelta(seconds=1)), customer["id"]))
    online2 = admin.get("/api/admin/customers?online_only=true").json()["customers"]
    row = next(c for c in online2 if c["id"] == customer["id"])
    assert row["online"] is True
    assert row["online_reason"] == "order"

    stats = admin.get("/api/admin/activity-stats").json()["stats"]
    assert stats["login_customer_count"] == 1
    assert stats["order_customer_count"] == 1
    assert stats["order_minutes"] == 10


def test_setup_wizard_skipped_by_default_for_testing(tmp_path):
    app = create_app(db_path=tmp_path / "setup.sqlite")
    client = TestClient(app)
    root = client.get("/", follow_redirects=False)
    assert root.status_code in {200, 303}
    assert root.headers.get("location") != "/setup"
    status = client.get("/api/setup/status").json()
    assert status["setup_enforced"] is False
    assert status["setup_required"] is False
    setup_html = client.get("/setup").text
    assert "首次配置 Bridge API Key / 全局设置" in setup_html
    assert "测试期跳过 API Key" in setup_html
    saved = client.post(
        "/api/setup/bridge",
        json={
            "admin_username": "admin",
            "admin_password": "admin123456",
            "bridge_base_url": "http://127.0.0.1:8010",
            "settings": {"system_name": "七元电竞", "privacy_mode_enabled": True, "default_limit_rounds": 6},
        },
    )
    assert saved.status_code == 200, saved.text
    public_settings = client.get("/api/public/settings").json()["settings"]
    assert public_settings["system_name"] == "七元电竞"
    assert public_settings["privacy_mode_enabled"] is True
    assert client.get("/api/setup/status").json()["configured"] is False


def test_setup_wizard_bridge_config_requires_admin_password_when_enforced(tmp_path):
    app = create_app(db_path=tmp_path / "setup-enforced.sqlite", settings=Settings(require_bridge_setup=True))
    client = TestClient(app)
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 303
    assert root.headers["location"] == "/setup"
    blocked_api = client.post("/api/login", json={"username": "x", "password": "y"})
    assert blocked_api.status_code == 428
    assert blocked_api.json()["error"] == "setup_required"
    login = client.get("/merchant-admin/login", follow_redirects=False)
    assert login.status_code == 303
    assert login.headers["location"] == "/setup"
    setup_html = client.get("/setup").text
    assert "首次配置 Bridge API Key / 全局设置" in setup_html
    assert "前台名称显示" in setup_html
    assert "中央 Bridge 地址 / API Key 填入地址" in setup_html
    bad = client.post(
        "/api/setup/bridge",
        json={"admin_username": "admin", "admin_password": "bad", "bridge_base_url": "http://127.0.0.1:8010", "bridge_merchant_key": "mk_live", "bridge_merchant_secret": "secret-live"},
    )
    assert bad.status_code == 401
    ok = client.post(
        "/api/setup/bridge",
        json={
            "admin_username": "admin",
            "admin_password": "admin123456",
            "bridge_base_url": "http://127.0.0.1:8010",
            "bridge_merchant_key": "mk_live",
            "bridge_merchant_secret": "secret-live",
            "settings": {"system_name": "七元电竞", "maintenance_mode_enabled": True},
        },
    )
    assert ok.status_code == 200, ok.text
    status = client.get("/api/setup/status").json()
    assert status["configured"] is True
    assert status["setup_required"] is False
    assert status["bridge_merchant_secret_set"] is True
    public_settings = client.get("/api/public/settings").json()["settings"]
    assert public_settings["system_name"] == "七元电竞"
    assert public_settings["maintenance_mode_enabled"] is True


def test_admin_order_analytics_devices_and_manual_order(app_and_bridge):
    app, bridge = app_and_bridge
    admin = TestClient(app)
    assert admin.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200

    devices = admin.get("/api/admin/devices").json()["devices"]
    assert any(d["id"] == 1 for d in devices)

    manual = admin.post(
        "/api/admin/manual-order",
        json={
            "device_id": 1,
            "boss_name": "ADM1234",
            "run_minutes": 30,
            "selected_mode": "absolute",
            "max_rounds": 3,
            "max_coin_loss": 12,
            "loadout_type": "custom",
            "loadout_helmet": "五级夜视头",
            "loadout_total_cost": 120000,
        },
    )
    assert manual.status_code == 200, manual.text
    order = manual.json()["order"]
    assert order["status"] == "waiting_ready_timer"
    assert order["quality"] == "secret"
    assert order["order_options"]["max_coin_loss"] == 12
    assert bridge.session_requests[-1]["device_id"] == 1
    assert bridge.session_requests[-1]["purpose"] == "admin_manual_order"
    assert bridge.commands[-4]["action"] == "set_loadout"
    assert bridge.commands[-4]["params"]["loadout_type"] == "custom"
    assert bridge.commands[-1]["params"]["max_coin_loss_w"] == 12

    detail = admin.get(f"/api/admin/orders/{order['id']}/detail")
    assert detail.status_code == 200
    assert detail.json()["detail"]["id"] == order["id"]

    added = admin.post(f"/api/admin/add-time/{order['id']}", json={"minutes": 5})
    assert added.status_code == 200
    assert added.json()["order"]["requested_minutes"] == 35

    rejoin = admin.post(f"/api/admin/manual-rejoin/{order['id']}", json={"boss_name": "ADM4567"})
    assert rejoin.status_code == 200, rejoin.text
    assert rejoin.json()["order"]["team_code"] == "ADM4567"

    stop = admin.post("/api/admin/devices/1/command", json={"action": "stop_current"})
    assert stop.status_code == 200, stop.text
    assert bridge.commands[-1]["action"] == "stop_current"
    maint = admin.post("/api/admin/machines/2/restart")
    assert maint.status_code == 200, maint.text
    assert bridge.session_requests[-1]["purpose"] == "admin_device_maintenance"
    assert bridge.commands[-1]["action"] == "restart"
    logs = admin.get("/api/admin/audit-logs").json()["logs"]
    assert any(l["action"] == "manual_order_create" for l in logs)
    assert any(l["action"] == "device_command" for l in logs)
    assert any(l["action"] == "device_maintenance_command" for l in logs)

    analytics = admin.get("/api/admin/order-analytics?period=day").json()["analytics"]
    assert analytics["order_count"] >= 1
    assert analytics["requested_minutes"] >= 30
    assert analytics["daily_series"]
    assert analytics["customer_rank"]


def test_admin_manual_order_modal_matches_legacy_controls(app_and_bridge):
    app, _bridge = app_and_bridge
    admin = TestClient(app)
    assert admin.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200
    html = admin.get("/merchant-admin").text
    for snippet in [
        '<div class="modal-head">手动下单</div>',
        'id="manualDeviceInfo"',
        'id="manualBossName"',
        'id="manualHybridModeSection"',
        '时长（小时）',
        'max="9999"',
        '时长（分钟）',
        '限制局数（0表示不限制）',
        '限制亏币（单位：万，0表示不限制）',
        'id="loadoutSection"',
        'name="loadoutType"',
        '大红包默认配装',
        'id="adminCustomLoadoutOption"',
        'id="customLoadoutFields"',
        'id="loadoutHelmet"',
        'id="loadoutArmor"',
        'id="loadoutRig"',
        'id="loadoutPistol"',
        'id="loadoutBackpack"',
        'id="loadoutCostDisplay"',
        'id="manualOrderBtn"',
        'function openManualOrderModal',
        'function closeManualOrderModal',
        'function autoCalculateRounds',
        'function toggleLoadoutCustom',
        'function calculateLoadoutCost',
    ]:
        assert snippet in html


def test_admin_manual_order_infers_device_mode_like_legacy_payload(tmp_path):
    class AbsoluteDeviceBridge(FakeBridge):
        def list_devices(self) -> list[dict[str, Any]]:
            return [{"id": 1, "device_id": 1, "display_name": "1号机", "online": True, "control_state": "idle", "mode": "absolute"}]

    bridge = AbsoluteDeviceBridge(capacity=1)
    app = create_app(db_path=tmp_path / "manual-mode.sqlite", bridge_client=bridge)
    admin = TestClient(app)
    assert admin.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200
    manual = admin.post("/api/admin/manual-order", json={"device_id": 1, "boss_name": "ABS1234", "run_minutes": 1})
    assert manual.status_code == 200, manual.text
    assert manual.json()["order"]["quality"] == "secret"


def test_manual_order_same_device_concurrency_guard(tmp_path):
    bridge = FakeBridge(capacity=1)
    db = Database(tmp_path / "manual-guard.sqlite")
    service = MerchantService(db, bridge)
    service.ensure_default_admin("admin", "admin123456")
    admin = service.authenticate_admin("admin", "admin123456")

    def place(i: int):
        try:
            return ("ok", service.admin_manual_order(admin, device_id=1, requested_minutes=5, requested_rounds=0, team_code=f"MNL{i:03d}", quality="standard"))
        except MerchantError as e:
            return ("err", e.code)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(place, range(6)))

    ok = [r for r in results if r[0] == "ok"]
    err = [r for r in results if r[0] == "err"]
    assert len(ok) == 1
    assert len(err) == 5
    assert all(code in {"device_has_active_order", "manual_device_has_active_order"} for _, code in err)
    assert len(service.admin_list_orders(status="waiting_ready_timer")) == 1


def test_admin_origin_check_and_backup_roundtrip(app_and_bridge):
    app, _bridge = app_and_bridge
    admin = TestClient(app)
    assert admin.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200

    rejected = admin.post("/api/admin/backup", headers={"Origin": "http://evil.example"}, json={})
    assert rejected.status_code == 403
    assert rejected.json()["error"] == "bad_origin"

    ok = admin.post("/api/admin/backup", headers={"Origin": "http://testserver"}, json={})
    assert ok.status_code == 200, ok.text
    backup = ok.json()["backup"]
    assert backup["name"].endswith(".sqlite")

    listed = admin.get("/api/admin/backup").json()["backups"]
    assert any(b["name"] == backup["name"] for b in listed)

    downloaded = admin.get(f"/api/admin/backup/{backup['name']}")
    assert downloaded.status_code == 200
    assert len(downloaded.content) > 100

    restored = admin.post(f"/api/admin/backup/{backup['name']}/restore", headers={"Origin": "http://testserver"}, json={})
    assert restored.status_code == 200, restored.text
    assert restored.json()["pre_restore"]["name"].startswith("merchant_pre_restore_")

    logs = admin.get("/api/admin/audit-logs").json()["logs"]
    assert any(l["action"] == "backup_create" for l in logs)
    assert any(l["action"] == "backup_restore" for l in logs)


def test_admin_role_management_and_owner_only_mutations(app_and_bridge):
    app, _bridge = app_and_bridge
    owner = TestClient(app)
    assert owner.post("/api/admin/login", json={"username": "admin", "password": "admin123456"}).status_code == 200

    created = owner.post("/api/admin/admins", json={"username": "ops", "password": "ops123456", "role": "operator"})
    assert created.status_code == 200, created.text
    operator_id = created.json()["admin"]["id"]
    admins = owner.get("/api/admin/admins").json()["admins"]
    owner_id = next(a["id"] for a in admins if a["username"] == "admin")
    assert any(a["username"] == "ops" and a["role"] == "operator" for a in admins)
    assert "商户管理员" in owner.get("/merchant-admin").text

    operator = TestClient(app)
    assert operator.post("/api/admin/login", json={"username": "ops", "password": "ops123456"}).status_code == 200
    assert operator.get("/api/admin/customers").status_code == 200
    assert operator.get("/api/admin/orders").status_code == 200
    denied = operator.post("/api/admin/settings", json={"system_name": "bad"})
    assert denied.status_code == 403
    assert denied.json()["error"] == "permission_denied"
    assert operator.post("/api/admin/admins", json={"username": "bad", "password": "123456"}).status_code == 403
    assert operator.post("/api/admin/cards/generate", json={"minutes": 60, "count": 1}).status_code == 403

    last_owner_status = owner.put(f"/api/admin/admins/{owner_id}/status", json={"status": "disabled"})
    assert last_owner_status.status_code == 409
    assert last_owner_status.json()["error"] == "last_owner"
    last_owner_role = owner.put(f"/api/admin/admins/{owner_id}/role", json={"role": "operator"})
    assert last_owner_role.status_code == 409
    assert last_owner_role.json()["error"] == "last_owner"

    reset = owner.put(f"/api/admin/admins/{operator_id}/password", json={"password": "ops654321"})
    assert reset.status_code == 200, reset.text
    assert operator.get("/api/admin/customers").status_code == 401
    assert operator.post("/api/admin/login", json={"username": "ops", "password": "ops654321"}).status_code == 200

    disabled = owner.put(f"/api/admin/admins/{operator_id}/status", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    assert operator.get("/api/admin/customers").status_code == 401
    assert TestClient(app).post("/api/admin/login", json={"username": "ops", "password": "ops654321"}).status_code == 401

    logs = owner.get("/api/admin/audit-logs").json()["logs"]
    assert any(l["action"] == "admin_create" for l in logs)
    assert any(l["action"] == "admin_password_reset" for l in logs)
    assert any(l["action"] == "admin_status_update" for l in logs)
