from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from merchant_portal_server.app import create_app
from merchant_portal_server.bridge_client import BridgeClientError
from merchant_portal_server.db import Database, iso, utcnow
from merchant_portal_server.service import MerchantService


class FakeBridge:
    def __init__(self, capacity: int = 3):
        self.lock = threading.Lock()
        self.idle = list(range(1, capacity + 1))
        self.sessions: dict[str, dict[str, Any]] = {}
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

    def create_control_session(self, *, merchant_context_ref: str, idem: str, device_id: int | None = None, auto_assign: bool = True, technical_lease_ttl_seconds: int = 180) -> dict[str, Any]:
        with self.lock:
            if not self.idle:
                raise BridgeClientError("device_not_available", "没有可用设备", 409)
            did = self.idle.pop(0)
            sid = f"cs_{len(self.sessions)+1}"
            sess = {"control_session_id": sid, "device_id": did, "fencing_token": f"ft_{sid}", "status": "active", "device_epoch": 1, "merchant_context_ref": merchant_context_ref}
            self.sessions[sid] = sess
            self._event("control_session.created", sid, device_id=did, device_epoch=1, payload={"purpose": "customer_control"})
            return dict(sess)

    def queue_command_bundle(self, session_id: str, *, fencing_token: str, expected_device_epoch: int | None, team_code: str, quality: str, idem: str) -> dict[str, Any]:
        with self.lock:
            if session_id not in self.sessions:
                raise BridgeClientError("not_found", "session missing", 404)
            bundle_id = f"bundle_{session_id}"
            out = []
            for action in ["set_loadout", "enter_team", "ready", "watch"]:
                cmd = {"command_id": f"cmd_{len(self.commands)+1}", "control_session_id": session_id, "action": action, "status": "queued", "bundle_id": bundle_id}
                self.commands.append(cmd)
                out.append(dict(cmd))
            return {"bundle_id": bundle_id, "commands": out}

    def queue_stop(self, session_id: str, *, fencing_token: str, idem: str, reason: str = "merchant_order_finished") -> dict[str, Any]:
        with self.lock:
            cmd = {"command_id": f"cmd_{len(self.commands)+1}", "control_session_id": session_id, "action": "stop_current", "status": "queued"}
            self.commands.append(cmd)
            self._event("command.queued", session_id, command_id=cmd["command_id"], device_id=self.sessions[session_id]["device_id"], payload={"action": "stop_current"})
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
    r = client.post("/api/register", json={"username": username, "password": "123456"})
    assert r.status_code == 200, r.text
    return r.json()["customer"]


def test_register_login_recharge(app_and_bridge):
    app, _bridge = app_and_bridge
    client = TestClient(app)
    customer = register_and_login(client)
    app.state.service.add_recharge_card("CARD-100", minutes=100)
    r = client.post("/api/recharge/redeem", json={"code": "CARD-100"})
    assert r.status_code == 200, r.text
    assert r.json()["customer"]["balance_minutes"] == 100
    assert client.get("/api/me").json()["customer"]["username"] == customer["username"]


def test_order_waits_for_ready_timer_before_running(app_and_bridge):
    app, bridge = app_and_bridge
    client = TestClient(app)
    register_and_login(client)
    app.state.service.add_recharge_card("CARD-30", minutes=30)
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
