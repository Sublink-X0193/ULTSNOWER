from __future__ import annotations

import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .bridge_client import BridgeClientError
from .db import Database, dumps, iso, loads, parse_ts, utcnow
from .security import hash_card_code, hash_password, opaque_merchant_ref, request_hash, verify_password

ACTIVE_ORDER_STATUSES = {
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
RENEWABLE_STATUSES = {"claiming_device", "device_claimed", "commanding", "waiting_ready_timer", "running"}
DEFAULT_SETTINGS: dict[str, Any] = {
    "privacy_mode_enabled": False,
    "maintenance_mode_enabled": False,
    "announcement_enabled": False,
    "announcement_text": "",
}


class MerchantError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class OrderResult:
    order: dict[str, Any]
    reused: bool = False


class MerchantService:
    def __init__(self, db: Database, bridge_client: Any, *, merchant_ref_secret: str = "dev-secret", session_ttl_seconds: int = 86400):
        self.db = db
        self.bridge = bridge_client
        self.merchant_ref_secret = merchant_ref_secret
        self.session_ttl_seconds = session_ttl_seconds

    # ---------- utility ----------
    def _get_state(self, con: sqlite3.Connection, key: str, default: str = "") -> str:
        row = con.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _set_state(self, con: sqlite3.Connection, key: str, value: str) -> None:
        con.execute(
            "INSERT INTO app_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, iso()),
        )

    def _active_order_row(self, con: sqlite3.Connection, customer_id: int) -> sqlite3.Row | None:
        qmarks = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        return con.execute(
            f"SELECT * FROM local_orders WHERE customer_id=? AND status IN ({qmarks}) ORDER BY id DESC LIMIT 1",
            (customer_id, *sorted(ACTIVE_ORDER_STATUSES)),
        ).fetchone()

    def _order_with_binding(self, con: sqlite3.Connection, order_id: int) -> dict[str, Any]:
        order = dict(con.execute("SELECT * FROM local_orders WHERE id=?", (order_id,)).fetchone())
        binding = con.execute("SELECT * FROM order_control_bindings WHERE local_order_id=?", (order_id,)).fetchone()
        order["binding"] = dict(binding) if binding else None
        return order

    def _new_order_no(self) -> str:
        return "mo_" + utcnow().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(4)

    # ---------- auth/customers ----------
    def register_customer(self, username: str, password: str) -> dict[str, Any]:
        username = str(username or "").strip()
        if not (3 <= len(username) <= 64):
            raise MerchantError("bad_username", "用户名长度必须为 3-64")
        if len(str(password or "")) < 4:
            raise MerchantError("bad_password", "密码至少 4 位")
        now_s = iso()
        with self.db.connect() as con:
            try:
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute(
                    "INSERT INTO customers(username,password_hash,balance_minutes,balance_rounds,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (username, hash_password(password), 0, 0, "active", now_s, now_s),
                )
                con.commit()
                return {"id": int(cur.lastrowid), "username": username, "balance_minutes": 0, "balance_rounds": 0}
            except sqlite3.IntegrityError:
                con.rollback()
                raise MerchantError("username_exists", "用户名已存在", 409)
            except Exception:
                con.rollback()
                raise

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM customers WHERE username=? AND status='active'", (str(username or "").strip(),)).fetchone()
            if not row or not verify_password(str(password or ""), row["password_hash"]):
                raise MerchantError("bad_credentials", "用户名或密码错误", 401)
            return self.public_customer(dict(row))

    def create_session(self, customer_id: int) -> str:
        sid = secrets.token_urlsafe(32)
        now_s = iso()
        expires = iso(utcnow() + timedelta(seconds=self.session_ttl_seconds))
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM customers WHERE id=? AND status='active'", (customer_id,)).fetchone()
            if not row:
                raise MerchantError("not_found", "客户不存在", 404)
            con.execute("INSERT INTO sessions(sid,customer_id,username,expires_at,created_at) VALUES(?,?,?,?,?)", (sid, customer_id, row["username"], expires, now_s))
        return sid

    def delete_session(self, sid: str) -> None:
        if not sid:
            return
        with self.db.connect() as con:
            con.execute("DELETE FROM sessions WHERE sid=?", (sid,))

    def customer_from_session(self, sid: str | None) -> dict[str, Any] | None:
        if not sid:
            return None
        with self.db.connect() as con:
            row = con.execute(
                """SELECT c.* FROM sessions s JOIN customers c ON c.id=s.customer_id
                   WHERE s.sid=? AND s.expires_at>? AND c.status='active'""",
                (sid, iso()),
            ).fetchone()
            return self.public_customer(dict(row)) if row else None

    def public_customer(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "balance_minutes": int(row.get("balance_minutes") or 0),
            "balance_rounds": int(row.get("balance_rounds") or 0),
            "status": row.get("status") or "active",
        }

    def get_customer(self, customer_id: int) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            if not row:
                raise MerchantError("not_found", "客户不存在", 404)
            return self.public_customer(dict(row))

    # ---------- merchant admin / settings ----------
    def ensure_default_admin(self, username: str, password: str) -> None:
        username = str(username or "").strip()
        if not username or not password:
            return
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                count = int(con.execute("SELECT COUNT(*) AS n FROM merchant_admins").fetchone()["n"])
                if count == 0:
                    con.execute(
                        "INSERT INTO merchant_admins(username,password_hash,role,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (username, hash_password(password), "owner", "active", now_s, now_s),
                    )
                con.commit()
            except Exception:
                con.rollback()
                raise

    def authenticate_admin(self, username: str, password: str) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM merchant_admins WHERE username=? AND status='active'", (str(username or "").strip(),)).fetchone()
            if not row or not verify_password(str(password or ""), row["password_hash"]):
                raise MerchantError("bad_credentials", "管理员用户名或密码错误", 401)
            con.execute("UPDATE merchant_admins SET last_login_at=?,updated_at=? WHERE id=?", (iso(), iso(), int(row["id"])))
            return self.public_admin(dict(row))

    def create_admin_session(self, admin_id: int) -> str:
        sid = secrets.token_urlsafe(32)
        now_s = iso()
        expires = iso(utcnow() + timedelta(seconds=self.session_ttl_seconds))
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM merchant_admins WHERE id=? AND status='active'", (admin_id,)).fetchone()
            if not row:
                raise MerchantError("not_found", "管理员不存在", 404)
            con.execute(
                "INSERT INTO admin_sessions(sid,admin_id,username,role,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                (sid, admin_id, row["username"], row["role"], expires, now_s),
            )
        return sid

    def delete_admin_session(self, sid: str) -> None:
        if not sid:
            return
        with self.db.connect() as con:
            con.execute("DELETE FROM admin_sessions WHERE sid=?", (sid,))

    def admin_from_session(self, sid: str | None) -> dict[str, Any] | None:
        if not sid:
            return None
        with self.db.connect() as con:
            row = con.execute(
                """SELECT a.* FROM admin_sessions s JOIN merchant_admins a ON a.id=s.admin_id
                   WHERE s.sid=? AND s.expires_at>? AND a.status='active'""",
                (sid, iso()),
            ).fetchone()
            return self.public_admin(dict(row)) if row else None

    def public_admin(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"id": int(row["id"]), "username": row["username"], "role": row.get("role") or "admin", "status": row.get("status") or "active"}

    def get_settings(self) -> dict[str, Any]:
        with self.db.connect() as con:
            rows = con.execute("SELECT key,value_json FROM merchant_settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            out[str(r["key"])] = loads(r["value_json"], out.get(str(r["key"])))
        out["privacy_mode_enabled"] = bool(out.get("privacy_mode_enabled"))
        out["maintenance_mode_enabled"] = bool(out.get("maintenance_mode_enabled"))
        out["announcement_enabled"] = bool(out.get("announcement_enabled"))
        out["announcement_text"] = str(out.get("announcement_text") or "")
        return out

    def _settings_locked(self, con: sqlite3.Connection) -> dict[str, Any]:
        rows = con.execute("SELECT key,value_json FROM merchant_settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            out[str(r["key"])] = loads(r["value_json"], out.get(str(r["key"])))
        out["privacy_mode_enabled"] = bool(out.get("privacy_mode_enabled"))
        out["maintenance_mode_enabled"] = bool(out.get("maintenance_mode_enabled"))
        out["announcement_enabled"] = bool(out.get("announcement_enabled"))
        out["announcement_text"] = str(out.get("announcement_text") or "")
        return out

    def update_settings(self, admin_id: int, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS)
        sanitized: dict[str, Any] = {}
        for key in allowed:
            if key not in values:
                continue
            if key.endswith("_enabled"):
                sanitized[key] = bool(values.get(key))
            elif key == "announcement_text":
                text = str(values.get(key) or "").strip()
                if len(text) > 2000:
                    raise MerchantError("bad_announcement", "公告最多 2000 字")
                sanitized[key] = text
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                for key, value in sanitized.items():
                    con.execute(
                        """INSERT INTO merchant_settings(key,value_json,updated_at,updated_by_admin_id)
                           VALUES(?,?,?,?)
                           ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at,updated_by_admin_id=excluded.updated_by_admin_id""",
                        (key, dumps(value), now_s, admin_id),
                    )
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.get_settings()

    def public_order(self, order: dict[str, Any] | None, *, privacy_mode: bool | None = None) -> dict[str, Any] | None:
        if order is None:
            return None
        if privacy_mode is None:
            privacy_mode = bool(self.get_settings().get("privacy_mode_enabled"))
        out = dict(order)
        if privacy_mode and out.get("team_code"):
            out["team_code"] = self._mask(out.get("team_code"))
            out["team_code_masked"] = True
        binding = out.get("binding")
        if isinstance(binding, dict):
            safe_binding = {
                "control_session_id": binding.get("control_session_id"),
                "device_id": binding.get("device_id"),
                "ready_timer_received": binding.get("ready_timer_received"),
                "status": binding.get("status"),
            }
            if privacy_mode and safe_binding.get("control_session_id"):
                safe_binding["control_session_id"] = self._mask(safe_binding["control_session_id"])
            out["binding"] = safe_binding
        return out

    def public_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        privacy = bool(self.get_settings().get("privacy_mode_enabled"))
        return [self.public_order(o, privacy_mode=privacy) for o in orders if o is not None]

    def _mask(self, value: Any) -> str:
        s = str(value or "")
        if len(s) <= 4:
            return "*" * len(s)
        return s[:2] + "***" + s[-2:]

    # ---------- recharge ----------
    def add_recharge_card(self, code: str, *, minutes: int = 0, rounds: int = 0) -> None:
        if minutes <= 0 and rounds <= 0:
            raise MerchantError("bad_card", "卡密分钟或局数必须大于 0")
        with self.db.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO recharge_cards(code_hash,minutes,rounds,status,created_at) VALUES(?,?,?,?,?)",
                (hash_card_code(code), int(minutes), int(rounds), "unused", iso()),
            )

    def redeem_card(self, customer_id: int, code: str) -> dict[str, Any]:
        code_hash = hash_card_code(code)
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                customer = con.execute("SELECT * FROM customers WHERE id=? AND status='active'", (customer_id,)).fetchone()
                if not customer:
                    raise MerchantError("not_found", "客户不存在", 404)
                card = con.execute("SELECT * FROM recharge_cards WHERE code_hash=?", (code_hash,)).fetchone()
                if not card:
                    raise MerchantError("card_not_found", "卡密不存在", 404)
                if card["status"] != "unused":
                    raise MerchantError("card_used", "卡密已使用", 409)
                minutes = int(card["minutes"] or 0)
                rounds = int(card["rounds"] or 0)
                con.execute("UPDATE recharge_cards SET status='used',used_by_customer_id=?,used_at=? WHERE code_hash=?", (customer_id, now_s, code_hash))
                con.execute(
                    "UPDATE customers SET balance_minutes=balance_minutes+?,balance_rounds=balance_rounds+?,updated_at=? WHERE id=?",
                    (minutes, rounds, now_s, customer_id),
                )
                con.execute(
                    "INSERT INTO recharge_records(customer_id,code_hash,minutes,rounds,created_at) VALUES(?,?,?,?,?)",
                    (customer_id, code_hash, minutes, rounds, now_s),
                )
                con.commit()
                return {"minutes": minutes, "rounds": rounds, "customer": self.get_customer(customer_id)}
            except Exception:
                con.rollback()
                raise

    # ---------- orders ----------
    def place_order(self, customer_id: int, *, requested_minutes: int, team_code: str, quality: str = "standard", idempotency_key: str | None = None) -> dict[str, Any]:
        requested_minutes = int(requested_minutes or 0)
        team_code = str(team_code or "").strip().upper()
        quality = str(quality or "standard").strip() or "standard"
        if requested_minutes <= 0 or requested_minutes > 24 * 60:
            raise MerchantError("bad_minutes", "购买分钟数不合法")
        if not (3 <= len(team_code) <= 32):
            raise MerchantError("bad_team_code", "队伍码长度不合法")
        payload_hash = request_hash({"requested_minutes": requested_minutes, "team_code": team_code, "quality": quality})
        scope = f"order:create:{customer_id}"
        if idempotency_key:
            with self.db.connect() as con:
                idem = con.execute("SELECT * FROM idempotency_keys WHERE scope=? AND idempotency_key=?", (scope, idempotency_key)).fetchone()
                if idem:
                    if idem["request_hash"] != payload_hash:
                        raise MerchantError("idempotency_conflict", "同一幂等键请求体不一致", 409)
                    return loads(idem["response_json"], {})

        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                customer = con.execute("SELECT * FROM customers WHERE id=? AND status='active'", (customer_id,)).fetchone()
                if not customer:
                    raise MerchantError("not_found", "客户不存在", 404)
                active = self._active_order_row(con, customer_id)
                if active:
                    result = {"order": self._order_with_binding(con, int(active["id"])), "reused": True}
                    if idempotency_key:
                        con.execute(
                            "INSERT OR REPLACE INTO idempotency_keys(scope,idempotency_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                            (scope, idempotency_key, payload_hash, dumps(result), now_s),
                        )
                    con.commit()
                    return result
                settings = self._settings_locked(con)
                if settings.get("maintenance_mode_enabled"):
                    raise MerchantError("maintenance_mode", "商户维护模式已开启，暂时不能下单", 503)
                if int(customer["balance_minutes"] or 0) < requested_minutes:
                    raise MerchantError("insufficient_balance", "分钟余额不足", 402)
                order_no = self._new_order_no()
                con.execute("UPDATE customers SET balance_minutes=balance_minutes-?,updated_at=? WHERE id=?", (requested_minutes, now_s, customer_id))
                cur = con.execute(
                    """INSERT INTO local_orders(customer_id,status,local_order_no,requested_minutes,requested_rounds,team_code,quality,amount_cents,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (customer_id, "claiming_device", order_no, requested_minutes, 0, team_code, quality, 0, now_s, now_s),
                )
                order_id = int(cur.lastrowid)
                con.commit()
            except Exception:
                con.rollback()
                raise

        merchant_context_ref = opaque_merchant_ref(order_no, self.merchant_ref_secret)
        try:
            sess = self.bridge.create_control_session(merchant_context_ref=merchant_context_ref, idem=f"claim:{order_no}")
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE local_orders SET status='device_claimed',updated_at=? WHERE id=?", (iso(), order_id))
                con.execute(
                    """INSERT INTO order_control_bindings(local_order_id,control_session_id,fencing_token,device_id,merchant_context_ref,last_device_epoch,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (order_id, sess["control_session_id"], sess["fencing_token"], int(sess["device_id"]), merchant_context_ref, int(sess.get("device_epoch") or 0), "active", iso(), iso()),
                )
                con.commit()
            bundle = self.bridge.queue_command_bundle(
                sess["control_session_id"],
                fencing_token=sess["fencing_token"],
                expected_device_epoch=int(sess.get("device_epoch") or 0),
                team_code=team_code,
                quality=quality,
                idem=f"bundle:start:{order_no}:v1",
            )
            commands = bundle.get("commands") or []
            last_command_id = commands[-1].get("command_id") if commands else None
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE local_orders SET status='waiting_ready_timer',updated_at=? WHERE id=?", (iso(), order_id))
                con.execute("UPDATE order_control_bindings SET last_command_id=?,updated_at=? WHERE local_order_id=?", (last_command_id, iso(), order_id))
                result = {"order": self._order_with_binding(con, order_id), "reused": False}
                if idempotency_key:
                    con.execute(
                        "INSERT OR REPLACE INTO idempotency_keys(scope,idempotency_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                        (scope, idempotency_key, payload_hash, dumps(result), iso()),
                    )
                con.commit()
                return result
        except BridgeClientError as e:
            self._fail_and_refund_new_order(order_id, f"bridge:{e.code}:{e.message}")
            with self.db.connect() as con:
                result = {"order": self._order_with_binding(con, order_id), "reused": False}
                if idempotency_key:
                    con.execute(
                        "INSERT OR REPLACE INTO idempotency_keys(scope,idempotency_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                        (scope, idempotency_key, payload_hash, dumps(result), iso()),
                    )
                return result

    def _fail_and_refund_new_order(self, order_id: int, reason: str) -> None:
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                order = con.execute("SELECT * FROM local_orders WHERE id=?", (order_id,)).fetchone()
                if not order:
                    con.commit()
                    return
                if order["status"] not in {"failed", "refunded", "finished"}:
                    minutes = int(order["requested_minutes"] or 0)
                    con.execute("UPDATE customers SET balance_minutes=balance_minutes+?,updated_at=? WHERE id=?", (minutes, iso(), int(order["customer_id"])))
                    con.execute(
                        "INSERT OR IGNORE INTO refund_records(local_order_id,customer_id,minutes,rounds,reason,created_at) VALUES(?,?,?,?,?,?)",
                        (order_id, int(order["customer_id"]), minutes, 0, "bridge_claim_or_bundle_failed", iso()),
                    )
                    con.execute("UPDATE local_orders SET status='failed',fail_reason=?,finished_at=?,updated_at=? WHERE id=?", (reason[:500], iso(), iso(), order_id))
                con.commit()
            except Exception:
                con.rollback()
                raise

    def current_order(self, customer_id: int) -> dict[str, Any] | None:
        with self.db.connect() as con:
            active = self._active_order_row(con, customer_id)
            return self._order_with_binding(con, int(active["id"])) if active else None

    def order_history(self, customer_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as con:
            rows = con.execute("SELECT * FROM local_orders WHERE customer_id=? ORDER BY id DESC LIMIT ?", (customer_id, limit)).fetchall()
            return [self._order_with_binding(con, int(r["id"])) for r in rows]

    # ---------- events / workers ----------
    def poll_events_once(self, limit: int = 100) -> dict[str, Any]:
        with self.db.connect() as con:
            cursor = int(self._get_state(con, "bridge_event_cursor", "0") or 0)
        data = self.bridge.events(cursor=cursor, limit=limit)
        events = data.get("events") or []
        processed = 0
        max_seq = cursor
        for ev in events:
            res = self.process_bridge_event(ev)
            processed += 1 if res.get("processed") else 0
            max_seq = max(max_seq, int(ev.get("event_seq") or ev.get("seq") or 0))
        next_cursor = int(data.get("next_cursor") or max_seq)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._set_state(con, "bridge_event_cursor", str(max(next_cursor, max_seq)))
            con.commit()
        return {"fetched": len(events), "processed": processed, "cursor": max(next_cursor, max_seq)}

    def process_bridge_event(self, ev: dict[str, Any]) -> dict[str, Any]:
        event_id = str(ev.get("event_id") or ev.get("id") or "")
        if not event_id:
            raise MerchantError("bad_event", "event_id missing")
        event_name = str(ev.get("event") or "")
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else loads(ev.get("payload_json"), {})
        created_at = str(ev.get("created_at") or iso())
        received_at = iso()
        event_seq = int(ev.get("event_seq") or ev.get("seq") or 0)
        device_epoch = ev.get("device_epoch")
        device_epoch_i = int(device_epoch) if device_epoch is not None else None
        control_session_id = ev.get("control_session_id")
        command_id = ev.get("command_id")
        device_id = ev.get("device_id")

        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                existed = con.execute("SELECT processed FROM bridge_events WHERE event_id=?", (event_id,)).fetchone()
                if existed:
                    con.commit()
                    return {"inserted": False, "processed": False, "duplicate": True}
                con.execute(
                    """INSERT INTO bridge_events(event_id,event_seq,control_session_id,command_id,device_id,event,device_epoch,payload_json,processed,created_at,received_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, event_seq, control_session_id, command_id, device_id, event_name, device_epoch_i, dumps(payload or {}), 0, created_at, received_at),
                )
                binding = None
                order = None
                if control_session_id:
                    binding = con.execute("SELECT * FROM order_control_bindings WHERE control_session_id=?", (control_session_id,)).fetchone()
                    if binding:
                        order = con.execute("SELECT * FROM local_orders WHERE id=?", (int(binding["local_order_id"]),)).fetchone()
                if not binding or not order:
                    con.execute("UPDATE bridge_events SET processed=1 WHERE event_id=?", (event_id,))
                    con.commit()
                    return {"inserted": True, "processed": True, "matched": False}

                if device_epoch_i is not None and device_epoch_i < int(binding["last_device_epoch"] or 0):
                    con.execute("UPDATE bridge_events SET processed=1 WHERE event_id=?", (event_id,))
                    con.commit()
                    return {"inserted": True, "processed": True, "stale_epoch": True}
                if device_epoch_i is not None and device_epoch_i > int(binding["last_device_epoch"] or 0):
                    con.execute("UPDATE order_control_bindings SET last_device_epoch=?,updated_at=? WHERE id=?", (device_epoch_i, iso(), int(binding["id"])))

                self._apply_event_locked(con, dict(order), dict(binding), event_name, payload or {}, command_id, device_epoch_i)
                con.execute("UPDATE bridge_events SET processed=1 WHERE event_id=?", (event_id,))
                con.commit()
                return {"inserted": True, "processed": True, "matched": True}
            except Exception:
                con.rollback()
                raise

    def _apply_event_locked(self, con: sqlite3.Connection, order: dict[str, Any], binding: dict[str, Any], event_name: str, payload: dict[str, Any], command_id: str | None, device_epoch: int | None) -> None:
        order_id = int(order["id"])
        now_s = iso()
        if event_name == "device.ready_for_customer_timer":
            if order["status"] in {"waiting_ready_timer", "commanding", "device_claimed", "claiming_device"} and not order.get("started_at"):
                start = utcnow()
                end = start + timedelta(minutes=int(order["requested_minutes"] or 0))
                con.execute(
                    "UPDATE local_orders SET status='running',started_at=?,end_at=?,updated_at=? WHERE id=?",
                    (iso(start), iso(end), now_s, order_id),
                )
                con.execute("UPDATE order_control_bindings SET ready_timer_received=1,updated_at=? WHERE id=?", (now_s, int(binding["id"])))
            return

        if event_name == "command.succeeded" and payload.get("action") == "stop_current":
            if order["status"] in ACTIVE_ORDER_STATUSES | {"interrupted_by_disconnect", "interrupted_by_admin"}:
                con.execute("UPDATE local_orders SET status='finished',finished_at=?,updated_at=? WHERE id=?", (now_s, now_s, order_id))
                con.execute("UPDATE order_control_bindings SET status='released',last_command_id=COALESCE(?,last_command_id),updated_at=? WHERE id=?", (command_id, now_s, int(binding["id"])))
            return

        if event_name == "command.failed" and payload.get("action") != "stop_current":
            if order["status"] != "running":
                self._interrupt_or_refund_locked(con, order, "failed", "command_failed")
            return

        if event_name == "bundle.failed":
            if order["status"] != "running":
                self._interrupt_or_refund_locked(con, order, "failed", "bundle_failed")
            return

        if event_name in {"admin.takeover", "control_session.revoked"}:
            self._interrupt_or_refund_locked(con, order, "interrupted_by_admin", "admin_takeover")
            con.execute("UPDATE order_control_bindings SET status='revoked',updated_at=? WHERE id=?", (now_s, int(binding["id"])))
            return

        if event_name == "control_session.expired":
            self._interrupt_or_refund_locked(con, order, "interrupted_by_disconnect", "central_lost_30m")
            con.execute("UPDATE order_control_bindings SET status='expired',updated_at=? WHERE id=?", (now_s, int(binding["id"])))
            return

        if event_name == "control_session.released":
            if order["status"] == "stopping":
                con.execute("UPDATE local_orders SET status='finished',finished_at=?,updated_at=? WHERE id=?", (now_s, now_s, order_id))
            con.execute("UPDATE order_control_bindings SET status='released',updated_at=? WHERE id=?", (now_s, int(binding["id"])))
            return

    def _interrupt_or_refund_locked(self, con: sqlite3.Connection, order: dict[str, Any], target_status: str, reason: str) -> None:
        if order["status"] in {"finished", "refunded", "failed", "interrupted_by_admin", "interrupted_by_disconnect"}:
            return
        order_id = int(order["id"])
        customer_id = int(order["customer_id"])
        minutes = self._remaining_minutes(order)
        now_s = iso()
        if minutes > 0:
            con.execute("UPDATE customers SET balance_minutes=balance_minutes+?,updated_at=? WHERE id=?", (minutes, now_s, customer_id))
            con.execute(
                "INSERT OR IGNORE INTO refund_records(local_order_id,customer_id,minutes,rounds,reason,created_at) VALUES(?,?,?,?,?,?)",
                (order_id, customer_id, minutes, 0, reason, now_s),
            )
        con.execute("UPDATE local_orders SET status=?,fail_reason=?,finished_at=?,updated_at=? WHERE id=?", (target_status, reason, now_s, now_s, order_id))

    def _remaining_minutes(self, order: dict[str, Any]) -> int:
        requested = int(order.get("requested_minutes") or 0)
        if not order.get("started_at") or not order.get("end_at"):
            return requested
        end = parse_ts(order.get("end_at"))
        if not end:
            return 0
        seconds = max(0.0, (end - utcnow()).total_seconds())
        return min(requested, int(math.ceil(seconds / 60.0)))

    def expire_orders_once(self) -> dict[str, Any]:
        now_s = iso()
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT o.*, b.control_session_id, b.fencing_token, b.id AS binding_id
                   FROM local_orders o JOIN order_control_bindings b ON b.local_order_id=o.id
                   WHERE o.status='running' AND o.end_at IS NOT NULL AND o.end_at<=? AND b.status='active'""",
                (now_s,),
            ).fetchall()
            due = [dict(r) for r in rows]
        stopped = 0
        for row in due:
            order_no = row["local_order_no"]
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                fresh = con.execute("SELECT status FROM local_orders WHERE id=?", (int(row["id"]),)).fetchone()
                if not fresh or fresh["status"] != "running":
                    con.commit()
                    continue
                con.execute("UPDATE local_orders SET status='stopping',updated_at=? WHERE id=?", (iso(), int(row["id"])))
                con.commit()
            try:
                cmd = self.bridge.queue_stop(row["control_session_id"], fencing_token=row["fencing_token"], idem=f"stop:{order_no}:v1")
                with self.db.connect() as con:
                    con.execute("UPDATE order_control_bindings SET last_command_id=?,updated_at=? WHERE id=?", (cmd.get("command_id") or cmd.get("id"), iso(), int(row["binding_id"])))
                stopped += 1
            except BridgeClientError as e:
                with self.db.connect() as con:
                    con.execute("UPDATE local_orders SET status='running',fail_reason=?,updated_at=? WHERE id=?", (f"stop_failed:{e.code}", iso(), int(row["id"])))
        return {"due": len(due), "stop_sent": stopped}

    def renew_sessions_once(self) -> dict[str, Any]:
        with self.db.connect() as con:
            qmarks = ",".join("?" for _ in RENEWABLE_STATUSES)
            rows = con.execute(
                f"""SELECT o.local_order_no, b.control_session_id, b.fencing_token
                    FROM local_orders o JOIN order_control_bindings b ON b.local_order_id=o.id
                    WHERE o.status IN ({qmarks}) AND b.status='active'""",
                tuple(sorted(RENEWABLE_STATUSES)),
            ).fetchall()
        ok = 0
        failed = 0
        for r in rows:
            try:
                minute_bucket = int(utcnow().timestamp() // 60)
                self.bridge.renew_session(r["control_session_id"], fencing_token=r["fencing_token"], idem=f"renew:{r['local_order_no']}:{minute_bucket}")
                ok += 1
            except BridgeClientError:
                failed += 1
        return {"renewed": ok, "failed": failed}

    def recover_sessions_once(self) -> dict[str, Any]:
        checked = 0
        fixed = 0
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT o.id AS order_id, o.local_order_no, b.control_session_id
                   FROM local_orders o JOIN order_control_bindings b ON b.local_order_id=o.id
                   WHERE o.status IN ('claiming_device','device_claimed','commanding','waiting_ready_timer','running','stopping') AND b.status='active'"""
            ).fetchall()
        for r in rows:
            checked += 1
            try:
                state = self.bridge.session_state(r["control_session_id"])
            except BridgeClientError:
                continue
            status = state.get("status")
            if status in {"expired", "force_taken_over", "revoked", "released"}:
                ev_name = "control_session.expired" if status == "expired" else ("control_session.revoked" if status in {"force_taken_over", "revoked"} else "control_session.released")
                self.process_bridge_event(
                    {
                        "id": f"recovery:{r['control_session_id']}:{status}",
                        "event_seq": 0,
                        "event": ev_name,
                        "control_session_id": r["control_session_id"],
                        "payload": {"source": "recovery", "status": status},
                        "created_at": iso(),
                    }
                )
                fixed += 1
        poll = self.poll_events_once()
        return {"checked": checked, "fixed": fixed, "poll": poll}
