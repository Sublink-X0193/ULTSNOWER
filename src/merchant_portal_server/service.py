from __future__ import annotations

import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SETTINGS: dict[str, Any] = {
    "system_name": "SNOW 自助下单",
    "default_limit_rounds": 4,
    "absolute_rounds_per_hour": 3,
    "night_time_check": True,
    "night_start_time": "22:50",
    "night_end_time": "06:10",
    "global_radar_url": "",
    "privacy_mode_enabled": False,
    "privacy_skip_balance": 0,
    "ace_enabled": False,
    "maintenance_mode_enabled": False,
    "maintenance_message": "",
    "announcement_enabled": False,
    "announcement_text": "",
    "max_loadout_cost": 65,
    "allow_custom_loadout": True,
    "equipment_config": [],
}

V9_LOADOUT_CATALOG: dict[str, tuple[str, ...]] = {
    "helmet": ("五级夜视头", "五级听力头", "五级耐力头", "五级防爆头", "四级听力头"),
    "armor": ("五级重甲", "五级老赛甲", "四级重甲"),
    "rig": ("20格胸挂",),
    "pistol": ("满改左轮",),
    "backpack": ("30格金包",),
}
V9_LOADOUT_TYPE_LABELS = {
    "helmet": "头部装备",
    "armor": "护甲装备",
    "rig": "胸挂装备",
    "pistol": "手枪装备",
    "backpack": "背包装备",
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

    def _remaining_seconds_from_order(self, order: dict[str, Any]) -> int:
        end = parse_ts(order.get("end_at"))
        if not end:
            return 0
        return max(0, int((end - utcnow()).total_seconds()))

    def _admin_order_view(self, order: dict[str, Any]) -> dict[str, Any]:
        out = dict(order)
        out["remaining_seconds"] = self._remaining_seconds_from_order(out)
        out["remaining_minutes"] = int(math.ceil(out["remaining_seconds"] / 60.0)) if out["remaining_seconds"] else 0
        out["binding_status"] = (out.get("binding") or {}).get("status") if isinstance(out.get("binding"), dict) else None
        out["control_session_id"] = (out.get("binding") or {}).get("control_session_id") if isinstance(out.get("binding"), dict) else None
        return out

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

    def admin_overview(self) -> dict[str, Any]:
        now_s = iso()
        with self.db.connect() as con:
            customer_count = int(con.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"])
            online_count = int(con.execute("SELECT COUNT(DISTINCT customer_id) AS n FROM sessions WHERE expires_at>?", (now_s,)).fetchone()["n"])
            active_order_count = int(
                con.execute(
                    f"SELECT COUNT(*) AS n FROM local_orders WHERE status IN ({','.join('?' for _ in ACTIVE_ORDER_STATUSES)})",
                    tuple(sorted(ACTIVE_ORDER_STATUSES)),
                ).fetchone()["n"]
            )
            running_count = int(con.execute("SELECT COUNT(*) AS n FROM local_orders WHERE status='running'").fetchone()["n"])
            total_balance_minutes = int(con.execute("SELECT COALESCE(SUM(balance_minutes),0) AS n FROM customers").fetchone()["n"] or 0)
            finished_count = int(con.execute("SELECT COUNT(*) AS n FROM local_orders WHERE status='finished'").fetchone()["n"])
            interrupted_count = int(con.execute("SELECT COUNT(*) AS n FROM local_orders WHERE status LIKE 'interrupted_%' OR status='failed'").fetchone()["n"])
        return {
            "customer_count": customer_count,
            "online_count": online_count,
            "active_order_count": active_order_count,
            "running_count": running_count,
            "finished_count": finished_count,
            "interrupted_count": interrupted_count,
            "total_balance_minutes": total_balance_minutes,
            "settings": self.get_settings(),
        }

    def admin_list_customers(self, *, keyword: str = "", online_only: bool = False, limit: int = 500) -> list[dict[str, Any]]:
        keyword_l = str(keyword or "").strip().lower()
        now_s = iso()
        with self.db.connect() as con:
            online_rows = con.execute("SELECT customer_id, MAX(created_at) AS last_seen FROM sessions WHERE expires_at>? GROUP BY customer_id", (now_s,)).fetchall()
            online = {int(r["customer_id"]): r["last_seen"] for r in online_rows}
            rows = con.execute("SELECT * FROM customers ORDER BY id DESC LIMIT ?", (max(1, min(limit, 2000)),)).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                item = self.public_customer(dict(r))
                item["created_at"] = r["created_at"]
                item["updated_at"] = r["updated_at"]
                item["online"] = int(r["id"]) in online
                item["last_seen_at"] = online.get(int(r["id"]))
                active = self._active_order_row(con, int(r["id"]))
                item["active_order"] = self._admin_order_view(self._order_with_binding(con, int(active["id"]))) if active else None
                item["active_order_status"] = item["active_order"]["status"] if item["active_order"] else ""
                item["active_order_remaining_minutes"] = item["active_order"]["remaining_minutes"] if item["active_order"] else 0
                blob = dumps(item).lower()
                if online_only and not item["online"]:
                    continue
                if keyword_l and keyword_l not in blob:
                    continue
                out.append(item)
            return out

    def admin_create_customer(self, *, username: str, password: str, balance_minutes: int = 0, balance_rounds: int = 0, status: str = "active") -> dict[str, Any]:
        customer = self.register_customer(username, password)
        status = status if status in {"active", "frozen"} else "active"
        with self.db.connect() as con:
            con.execute(
                "UPDATE customers SET balance_minutes=?,balance_rounds=?,status=?,updated_at=? WHERE id=?",
                (max(0, int(balance_minutes or 0)), max(0, int(balance_rounds or 0)), status, iso(), customer["id"]),
            )
        return self.get_customer(customer["id"])

    def admin_update_customer_balance(self, customer_id: int, *, balance_minutes: int | None = None, balance_rounds: int | None = None, delta_minutes: int | None = None, delta_rounds: int | None = None) -> dict[str, Any]:
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
                if not row:
                    raise MerchantError("not_found", "客户不存在", 404)
                minutes = int(row["balance_minutes"] or 0)
                rounds = int(row["balance_rounds"] or 0)
                if balance_minutes is not None:
                    minutes = int(balance_minutes)
                if balance_rounds is not None:
                    rounds = int(balance_rounds)
                if delta_minutes is not None:
                    minutes += int(delta_minutes)
                if delta_rounds is not None:
                    rounds += int(delta_rounds)
                minutes = max(0, minutes)
                rounds = max(0, rounds)
                con.execute("UPDATE customers SET balance_minutes=?,balance_rounds=?,updated_at=? WHERE id=?", (minutes, rounds, iso(), customer_id))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.get_customer(customer_id)

    def admin_set_customer_status(self, customer_id: int, status: str) -> dict[str, Any]:
        status = str(status or "active")
        if status not in {"active", "frozen"}:
            raise MerchantError("bad_status", "客户状态必须是 active 或 frozen")
        with self.db.connect() as con:
            cur = con.execute("UPDATE customers SET status=?,updated_at=? WHERE id=?", (status, iso(), customer_id))
            if cur.rowcount == 0:
                raise MerchantError("not_found", "客户不存在", 404)
            if status != "active":
                con.execute("DELETE FROM sessions WHERE customer_id=?", (customer_id,))
        return self.get_customer(customer_id)

    def admin_reset_customer_password(self, customer_id: int, password: str) -> dict[str, Any]:
        if len(str(password or "")) < 4:
            raise MerchantError("bad_password", "密码至少 4 位")
        with self.db.connect() as con:
            cur = con.execute("UPDATE customers SET password_hash=?,updated_at=? WHERE id=?", (hash_password(password), iso(), customer_id))
            if cur.rowcount == 0:
                raise MerchantError("not_found", "客户不存在", 404)
        return {"id": customer_id, "updated": True}

    def admin_delete_customer(self, customer_id: int) -> dict[str, Any]:
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                active = self._active_order_row(con, customer_id)
                if active:
                    raise MerchantError("customer_has_active_order", "客户有进行中订单，不能删除", 409)
                cur = con.execute("DELETE FROM customers WHERE id=?", (customer_id,))
                if cur.rowcount == 0:
                    raise MerchantError("not_found", "客户不存在", 404)
                con.commit()
            except Exception:
                con.rollback()
                raise
        return {"id": customer_id, "deleted": True}

    def admin_list_orders(self, *, keyword: str = "", status: str = "", customer_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        keyword_l = str(keyword or "").strip().lower()
        with self.db.connect() as con:
            params: list[Any] = []
            where = "WHERE 1=1"
            if status:
                where += " AND o.status=?"
                params.append(status)
            if customer_id:
                where += " AND o.customer_id=?"
                params.append(int(customer_id))
            rows = con.execute(
                f"""SELECT o.*, c.username AS customer_username
                    FROM local_orders o JOIN customers c ON c.id=o.customer_id
                    {where}
                    ORDER BY o.id DESC LIMIT ?""",
                (*params, max(1, min(limit, 2000))),
            ).fetchall()
            out = []
            for r in rows:
                item = self._admin_order_view(self._order_with_binding(con, int(r["id"])))
                item["customer_username"] = r["customer_username"]
                if keyword_l and keyword_l not in dumps(item).lower():
                    continue
                out.append(item)
            return out

    def admin_get_order(self, order_id: int) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute("SELECT o.*, c.username AS customer_username FROM local_orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=?", (order_id,)).fetchone()
            if not row:
                raise MerchantError("not_found", "订单不存在", 404)
            item = self._admin_order_view(self._order_with_binding(con, order_id))
            item["customer_username"] = row["customer_username"]
            return item

    def admin_adjust_order_time(self, order_id: int, *, add_minutes: int) -> dict[str, Any]:
        add_minutes = int(add_minutes or 0)
        if add_minutes == 0:
            raise MerchantError("bad_minutes", "调整分钟不能为 0")
        if abs(add_minutes) > 24 * 60:
            raise MerchantError("bad_minutes", "单次调整不能超过 24 小时")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM local_orders WHERE id=?", (order_id,)).fetchone()
                if not row:
                    raise MerchantError("not_found", "订单不存在", 404)
                if row["status"] not in {"waiting_ready_timer", "running", "stopping"}:
                    raise MerchantError("bad_order_status", "只能调整等待/运行/停止中的订单", 409)
                requested = max(0, int(row["requested_minutes"] or 0) + add_minutes)
                end_at = row["end_at"]
                if row["end_at"]:
                    end = parse_ts(row["end_at"]) or utcnow()
                    end_at = iso(max(utcnow(), end + timedelta(minutes=add_minutes)))
                con.execute("UPDATE local_orders SET requested_minutes=?,end_at=?,updated_at=? WHERE id=?", (requested, end_at, iso(), order_id))
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.admin_get_order(order_id)

    def admin_stop_order(self, order_id: int) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT o.*, b.control_session_id, b.fencing_token
                   FROM local_orders o LEFT JOIN order_control_bindings b ON b.local_order_id=o.id
                   WHERE o.id=?""",
                (order_id,),
            ).fetchone()
            if not row:
                raise MerchantError("not_found", "订单不存在", 404)
            if row["status"] not in ACTIVE_ORDER_STATUSES:
                raise MerchantError("bad_order_status", "订单不在可停止状态", 409)
        if row["control_session_id"] and row["fencing_token"] and self.bridge:
            try:
                self.bridge.queue_stop(row["control_session_id"], fencing_token=row["fencing_token"], idem=f"admin-stop:{row['local_order_no']}:v1", reason="merchant_admin_stop")
            except BridgeClientError as e:
                raise MerchantError(e.code, e.message, e.status_code)
        with self.db.connect() as con:
            con.execute("UPDATE local_orders SET status='stopping',fail_reason='admin_stop',updated_at=? WHERE id=?", (iso(), order_id))
        return self.admin_get_order(order_id)

    def assert_customer_order(self, order_id: int, customer_id: int) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM local_orders WHERE id=? AND customer_id=?", (order_id, customer_id)).fetchone()
            if not row:
                raise MerchantError("not_found", "订单不存在", 404)
            return self._order_with_binding(con, order_id)

    def customer_stop_order(self, order_id: int, customer_id: int) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT o.*, b.control_session_id, b.fencing_token
                   FROM local_orders o LEFT JOIN order_control_bindings b ON b.local_order_id=o.id
                   WHERE o.id=? AND o.customer_id=?""",
                (order_id, customer_id),
            ).fetchone()
            if not row:
                raise MerchantError("not_found", "订单不存在", 404)
            if row["status"] not in ACTIVE_ORDER_STATUSES:
                raise MerchantError("bad_order_status", "订单不在可结束状态", 409)

        if row["control_session_id"] and row["fencing_token"] and self.bridge:
            try:
                self.bridge.queue_stop(row["control_session_id"], fencing_token=row["fencing_token"], idem=f"customer-stop:{row['local_order_no']}:v1", reason="merchant_customer_stop")
            except BridgeClientError as e:
                raise MerchantError(e.code, e.message, e.status_code)

        order_dict = dict(row)
        refund_minutes = self._remaining_minutes(order_dict)
        refund_rounds = int(order_dict.get("requested_rounds") or 0)
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                fresh = con.execute("SELECT * FROM local_orders WHERE id=? AND customer_id=?", (order_id, customer_id)).fetchone()
                if not fresh:
                    raise MerchantError("not_found", "订单不存在", 404)
                if fresh["status"] not in ACTIVE_ORDER_STATUSES:
                    raise MerchantError("bad_order_status", "订单不在可结束状态", 409)
                already_refunded = con.execute(
                    "SELECT 1 FROM refund_records WHERE local_order_id=? AND reason='customer_stop'",
                    (order_id,),
                ).fetchone()
                if not already_refunded and (refund_minutes > 0 or refund_rounds > 0):
                    con.execute(
                        "UPDATE customers SET balance_minutes=balance_minutes+?,balance_rounds=balance_rounds+?,updated_at=? WHERE id=?",
                        (refund_minutes, refund_rounds, now_s, customer_id),
                    )
                    con.execute(
                        "INSERT OR IGNORE INTO refund_records(local_order_id,customer_id,minutes,rounds,reason,created_at) VALUES(?,?,?,?,?,?)",
                        (order_id, customer_id, refund_minutes, refund_rounds, "customer_stop", now_s),
                    )
                con.execute(
                    "UPDATE local_orders SET status='stopping',fail_reason='customer_stop',updated_at=? WHERE id=?",
                    (now_s, order_id),
                )
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.admin_get_order(order_id)

    def customer_rejoin_order(self, order_id: int, customer_id: int, team_code: str) -> dict[str, Any]:
        team_code = str(team_code or "").strip().upper()
        if team_code and not (3 <= len(team_code) <= 32):
            raise MerchantError("bad_team_code", "队伍码长度不合法")
        with self.db.connect() as con:
            row = con.execute(
                """SELECT o.*, b.control_session_id, b.fencing_token, b.last_device_epoch
                   FROM local_orders o LEFT JOIN order_control_bindings b ON b.local_order_id=o.id
                   WHERE o.id=? AND o.customer_id=?""",
                (order_id, customer_id),
            ).fetchone()
            if not row:
                raise MerchantError("not_found", "订单不存在", 404)
            if row["status"] not in ACTIVE_ORDER_STATUSES:
                raise MerchantError("bad_order_status", "订单不在可换队状态", 409)
        if team_code and row["control_session_id"] and row["fencing_token"] and hasattr(self.bridge, "queue_command"):
            try:
                self.bridge.queue_command(
                    row["control_session_id"],
                    fencing_token=row["fencing_token"],
                    action="enter_team",
                    params={"team_code": team_code, "clear_existing": True},
                    expected_device_epoch=int(row["last_device_epoch"] or 0),
                    idem=f"customer-rejoin:{row['local_order_no']}:{team_code}",
                )
            except BridgeClientError as e:
                raise MerchantError(e.code, e.message, e.status_code)
        if team_code:
            with self.db.connect() as con:
                con.execute("UPDATE local_orders SET team_code=?,updated_at=? WHERE id=?", (team_code, iso(), order_id))
        return self.admin_get_order(order_id)

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

    @staticmethod
    def _bool_value(value: Any) -> bool:
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

    def get_settings(self) -> dict[str, Any]:
        with self.db.connect() as con:
            rows = con.execute("SELECT key,value_json FROM merchant_settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            out[str(r["key"])] = loads(r["value_json"], out.get(str(r["key"])))
        return self._normalize_settings(out)

    def _settings_locked(self, con: sqlite3.Connection) -> dict[str, Any]:
        rows = con.execute("SELECT key,value_json FROM merchant_settings").fetchall()
        out = dict(DEFAULT_SETTINGS)
        for r in rows:
            out[str(r["key"])] = loads(r["value_json"], out.get(str(r["key"])))
        return self._normalize_settings(out)

    def _normalize_settings(self, out: dict[str, Any]) -> dict[str, Any]:
        for key in ("privacy_mode_enabled", "maintenance_mode_enabled", "announcement_enabled", "night_time_check", "ace_enabled", "allow_custom_loadout"):
            out[key] = self._bool_value(out.get(key))
        for key, default in (("default_limit_rounds", 4), ("absolute_rounds_per_hour", 3), ("privacy_skip_balance", 0), ("max_loadout_cost", 65)):
            try:
                out[key] = max(0, int(out.get(key, default)))
            except Exception:
                out[key] = default
        for key in ("maintenance_message", "announcement_text", "system_name", "night_start_time", "night_end_time", "global_radar_url"):
            out[key] = str(out.get(key) or DEFAULT_SETTINGS.get(key) or "")
        out["system_name"] = out["system_name"] or "SNOW 自助下单"
        if not isinstance(out.get("equipment_config"), list):
            out["equipment_config"] = []
        return out

    def update_settings(self, admin_id: int, values: dict[str, Any]) -> dict[str, Any]:
        allowed = set(DEFAULT_SETTINGS)
        sanitized: dict[str, Any] = {}
        for key in allowed:
            if key not in values:
                continue
            if key.endswith("_enabled") or key in {"night_time_check", "ace_enabled", "allow_custom_loadout"}:
                sanitized[key] = self._bool_value(values.get(key))
            elif key in {"default_limit_rounds", "absolute_rounds_per_hour", "privacy_skip_balance", "max_loadout_cost"}:
                try:
                    sanitized[key] = max(0, int(values.get(key) or 0))
                except Exception:
                    raise MerchantError("bad_setting", f"{key} 必须是数字")
            elif key == "announcement_text":
                text = str(values.get(key) or "").strip()
                if len(text) > 4000:
                    raise MerchantError("bad_announcement", "公告最多 4000 字")
                sanitized[key] = text
            elif key in {"maintenance_message", "global_radar_url", "night_start_time", "night_end_time"}:
                text = str(values.get(key) or "").strip()
                if len(text) > 300:
                    raise MerchantError("bad_setting", f"{key} 最多 300 字")
                sanitized[key] = text
            elif key == "system_name":
                text = str(values.get(key) or "").strip()
                if len(text) > 32:
                    raise MerchantError("bad_system_name", "系统名称最多 32 字")
                sanitized[key] = text or "SNOW 自助下单"
            elif key == "equipment_config":
                if not isinstance(values.get(key), list):
                    raise MerchantError("bad_equipment", "装备配置必须是数组")
                sanitized[key] = self._normalize_equipment_list(values.get(key) or [])
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

    def _bridge_selection_policy(self, settings: dict[str, Any], quality: str) -> dict[str, Any]:
        skip_w = max(0, int(settings.get("privacy_skip_balance") or 0))
        return {
            "source": "merchant_settings",
            "order_quality": str(quality or "standard"),
            "privacy_mode": bool(settings.get("privacy_mode_enabled")),
            "privacy_skip_balance_w": skip_w,
            "min_device_coin_balance": skip_w * 10000,
        }

    @staticmethod
    def _parse_hhmm(value: Any, default: str) -> time:
        raw = str(value or default).strip()
        try:
            hh, mm = raw.split(":", 1)
            return time(max(0, min(23, int(hh))), max(0, min(59, int(mm))))
        except Exception:
            hh, mm = default.split(":", 1)
            return time(int(hh), int(mm))

    def _night_time_allowed(self, settings: dict[str, Any], *, now_local: datetime | None = None) -> bool:
        if not self._bool_value(settings.get("night_time_check")):
            return True
        current = (now_local or utcnow().astimezone(LOCAL_TZ)).time()
        start = self._parse_hhmm(settings.get("night_start_time"), "22:50")
        end = self._parse_hhmm(settings.get("night_end_time"), "06:10")
        if start == end:
            return True
        if start < end:
            return start <= current <= end
        return current >= start or current <= end

    # ---------- equipment config ----------
    def supported_equipment_catalog(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        idx = 1
        for typ, names in V9_LOADOUT_CATALOG.items():
            for sort_order, name in enumerate(names, 1):
                out.append({
                    "id": idx,
                    "equipment_type": typ,
                    "equipment_name": name,
                    "type_label": V9_LOADOUT_TYPE_LABELS.get(typ, typ),
                    "price": 0,
                    "enabled": 0,
                    "sort_order": sort_order,
                    "client_supported": True,
                })
                idx += 1
        return out

    def _normalize_equipment_list(self, rows: list[Any]) -> list[dict[str, Any]]:
        valid_names = {(typ, name) for typ, names in V9_LOADOUT_CATALOG.items() for name in names}
        out: list[dict[str, Any]] = []
        for i, item in enumerate(rows, 1):
            if not isinstance(item, dict):
                continue
            typ = str(item.get("equipment_type") or "").strip()
            name = str(item.get("equipment_name") or "").strip()
            if (typ, name) not in valid_names:
                continue
            out.append({
                "id": int(item.get("id") or i),
                "equipment_type": typ,
                "equipment_name": name,
                "type_label": V9_LOADOUT_TYPE_LABELS.get(typ, typ),
                "price": max(0, int(item.get("price") or 0)),
                "enabled": 1 if bool(item.get("enabled")) else 0,
                "sort_order": int(item.get("sort_order") or i),
                "client_supported": True,
            })
        return out

    def get_equipment_config(self) -> dict[str, Any]:
        st = self.get_settings()
        configured = self._normalize_equipment_list(st.get("equipment_config") or [])
        by_key = {(x["equipment_type"], x["equipment_name"]): x for x in configured}
        merged: list[dict[str, Any]] = []
        for base in self.supported_equipment_catalog():
            merged.append({**base, **by_key.get((base["equipment_type"], base["equipment_name"]), {})})
        return {
            "equipment": merged,
            "supported_equipment": self.supported_equipment_catalog(),
            "max_loadout_cost": int(st.get("max_loadout_cost") or 65),
            "allow_custom_loadout": bool(st.get("allow_custom_loadout")),
        }

    def update_equipment_config(self, admin_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.update_settings(admin_id, {
            "equipment_config": payload.get("equipment") or [],
            "max_loadout_cost": payload.get("max_loadout_cost", 65),
            "allow_custom_loadout": bool(payload.get("allow_custom_loadout", True)),
        })

    # ---------- recharge ----------
    def add_recharge_card(self, code: str, *, minutes: int = 0, rounds: int = 0, card_type: str = "normal", mode: str = "machine", night_coin_loss: int = 0) -> None:
        if minutes <= 0 and rounds <= 0:
            raise MerchantError("bad_card", "卡密分钟或局数必须大于 0")
        with self.db.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO recharge_cards(code_hash,code_plain,minutes,rounds,card_type,mode,night_coin_loss,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (hash_card_code(code), code, int(minutes), int(rounds), card_type or "normal", mode or "machine", int(night_coin_loss or 0), "unused", iso()),
            )

    def _calc_rounds(self, minutes: int, mode: str, card_type: str, explicit_rounds: int = 0) -> int:
        if explicit_rounds > 0:
            return int(explicit_rounds)
        if str(card_type or "normal") == "night":
            return 0
        st = self.get_settings()
        per_hour = int(st.get("default_limit_rounds") if mode == "machine" else st.get("absolute_rounds_per_hour") or 0)
        if minutes <= 0 or per_hour <= 0:
            return 0
        return max(per_hour, math.floor((minutes / 60) * per_hour))

    def generate_recharge_cards(self, *, count: int, minutes: int, rounds: int = 0, card_type: str = "normal", mode: str = "machine", night_coin_loss: int = 0) -> list[dict[str, Any]]:
        count = max(1, min(int(count or 1), 100))
        minutes = int(minutes or 0)
        if minutes <= 0:
            raise MerchantError("bad_card", "请填写时长")
        mode = mode if mode in {"machine", "absolute", "hybrid"} else "machine"
        card_type = "night" if card_type == "night" else "normal"
        rounds = self._calc_rounds(minutes, mode, card_type, int(rounds or 0))
        made = []
        for _ in range(count):
            code = "LOCAL-" + secrets.token_hex(4).upper()
            self.add_recharge_card(code, minutes=minutes, rounds=rounds, card_type=card_type, mode=mode, night_coin_loss=night_coin_loss)
            made.append({"card_code": code, "minutes": minutes, "rounds": rounds, "card_type": card_type, "mode": mode, "night_coin_loss": int(night_coin_loss or 0)})
        return made

    def list_recharge_cards(self, *, keyword: str = "", status: str = "", card_type: str = "", limit: int = 500) -> list[dict[str, Any]]:
        keyword_l = str(keyword or "").strip().lower()
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT rc.*, c.username AS used_by_name
                   FROM recharge_cards rc LEFT JOIN customers c ON c.id=rc.used_by_customer_id
                   ORDER BY rc.created_at DESC LIMIT ?""",
                (max(1, min(int(limit or 500), 2000)),),
            ).fetchall()
        out = []
        for r in rows:
            item = {
                "card_code": r["code_plain"] or self._mask(r["code_hash"]),
                "minutes": int(r["minutes"] or 0),
                "rounds": int(r["rounds"] or 0),
                "absolute_rounds": int(r["rounds"] or 0),
                "card_type": r["card_type"] or "normal",
                "mode": r["mode"] or "machine",
                "night_coin_loss": int(r["night_coin_loss"] or 0),
                "used": r["status"] != "unused",
                "status": r["status"],
                "used_by_name": r["used_by_name"],
                "used_at": r["used_at"],
                "created_at": r["created_at"],
            }
            if status and status != ("used" if item["used"] else "unused"):
                continue
            if card_type and item["card_type"] != card_type:
                continue
            if keyword_l and keyword_l not in dumps(item).lower():
                continue
            out.append(item)
        return out

    def delete_recharge_card(self, code: str) -> dict[str, Any]:
        code_hash = hash_card_code(code)
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM recharge_cards WHERE code_hash=?", (code_hash,)).fetchone()
            if not row:
                raise MerchantError("card_not_found", "卡密不存在", 404)
            if row["status"] != "unused":
                raise MerchantError("card_used", "已使用卡密不能删除", 409)
            con.execute("DELETE FROM recharge_cards WHERE code_hash=?", (code_hash,))
        return {"deleted": True, "card_code": code}

    def export_unused_cards_csv(self, *, card_type: str = "") -> str:
        import csv
        import io

        rows = self.list_recharge_cards(status="unused", card_type=card_type, limit=2000)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["card_code", "card_type", "mode", "minutes", "rounds", "created_at"])
        for c in rows:
            w.writerow([c["card_code"], c["card_type"], c["mode"], c["minutes"], c["rounds"], c["created_at"]])
        return out.getvalue()

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
                settings = self._settings_locked(con)
                if str(card["card_type"] or "normal") == "night" and not self._night_time_allowed(settings):
                    raise MerchantError(
                        "night_time_not_allowed",
                        f"包夜卡只能在 {settings.get('night_start_time') or '22:50'} 至次日 {settings.get('night_end_time') or '06:10'} 使用",
                        403,
                    )
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
                return {"minutes": minutes, "rounds": rounds, "mode": card["mode"] or "machine", "card_type": card["card_type"] or "normal", "customer": self.get_customer(customer_id)}
            except Exception:
                con.rollback()
                raise

    def night_login_card(self, code: str) -> dict[str, Any]:
        code = str(code or "").strip()
        if not code:
            raise MerchantError("bad_card", "请输入卡密")
        code_hash = hash_card_code(code)
        suffix = "".join(ch for ch in code[-6:] if ch.isalnum()).lower() or secrets.token_hex(3)
        username_base = f"night_{suffix}"
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                card = con.execute("SELECT * FROM recharge_cards WHERE code_hash=?", (code_hash,)).fetchone()
                if not card:
                    raise MerchantError("card_not_found", "卡密不存在", 404)
                if card["status"] != "unused":
                    raise MerchantError("card_used", "卡密已使用", 409)
                if str(card["card_type"] or "normal").lower() != "night":
                    raise MerchantError("not_night_card", "仅包夜卡可使用包夜入口登录", 400)
                settings = self._settings_locked(con)
                if not self._night_time_allowed(settings):
                    raise MerchantError(
                        "night_time_not_allowed",
                        f"包夜卡只能在 {settings.get('night_start_time') or '22:50'} 至次日 {settings.get('night_end_time') or '06:10'} 使用",
                        403,
                    )
                username = username_base
                for _ in range(10):
                    if not con.execute("SELECT 1 FROM customers WHERE username=?", (username,)).fetchone():
                        break
                    username = f"{username_base}_{secrets.token_hex(2)}"
                minutes = int(card["minutes"] or 0)
                rounds = int(card["rounds"] or 0)
                cur = con.execute(
                    "INSERT INTO customers(username,password_hash,balance_minutes,balance_rounds,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (username, hash_password(secrets.token_urlsafe(18)), minutes, rounds, "active", now_s, now_s),
                )
                customer_id = int(cur.lastrowid)
                con.execute("UPDATE recharge_cards SET status='used',used_by_customer_id=?,used_at=? WHERE code_hash=?", (customer_id, now_s, code_hash))
                con.execute(
                    "INSERT INTO recharge_records(customer_id,code_hash,minutes,rounds,created_at) VALUES(?,?,?,?,?)",
                    (customer_id, code_hash, minutes, rounds, now_s),
                )
                con.commit()
                return self.get_customer(customer_id)
            except Exception:
                con.rollback()
                raise

    def recharge_history(self, customer_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT rr.*, rc.code_plain, rc.mode, rc.card_type
                   FROM recharge_records rr LEFT JOIN recharge_cards rc ON rc.code_hash=rr.code_hash
                   WHERE rr.customer_id=?
                   ORDER BY rr.id DESC LIMIT ?""",
                (customer_id, max(1, min(int(limit or 200), 1000))),
            ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": int(r["id"]),
                "trade_no": f"CARD{int(r['id']):06d}",
                "status": "paid",
                "config_type": "card_redeem",
                "mode": r["mode"] or "machine",
                "card_type": r["card_type"] or "normal",
                "minutes": int(r["minutes"] or 0),
                "rounds": int(r["rounds"] or 0),
                "value_rounds": int(r["rounds"] or 0),
                "absolute_rounds": int(r["rounds"] or 0),
                "price": 0,
                "card_code": r["code_plain"] or self._mask(r["code_hash"]),
                "created_at": r["created_at"],
                "paid_at": r["created_at"],
            })
        return out

    # ---------- orders ----------
    def place_order(self, customer_id: int, *, requested_minutes: int, team_code: str, quality: str = "standard", requested_rounds: int = 0, idempotency_key: str | None = None) -> dict[str, Any]:
        requested_minutes = int(requested_minutes or 0)
        requested_rounds = max(0, int(requested_rounds or 0))
        team_code = str(team_code or "").strip().upper()
        quality = str(quality or "standard").strip() or "standard"
        if requested_minutes <= 0 or requested_minutes > 24 * 60:
            raise MerchantError("bad_minutes", "购买分钟数不合法")
        if requested_rounds > 10000:
            raise MerchantError("bad_rounds", "战损局数不合法")
        if not (3 <= len(team_code) <= 32):
            raise MerchantError("bad_team_code", "队伍码长度不合法")
        payload_hash = request_hash({"requested_minutes": requested_minutes, "requested_rounds": requested_rounds, "team_code": team_code, "quality": quality})
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
                    msg = settings.get("maintenance_message") or "商户维护模式已开启，暂时不能下单"
                    raise MerchantError("maintenance_mode", str(msg), 503)
                if int(customer["balance_minutes"] or 0) < requested_minutes:
                    raise MerchantError("insufficient_balance", "分钟余额不足", 402)
                if requested_rounds and int(customer["balance_rounds"] or 0) < requested_rounds:
                    raise MerchantError("insufficient_rounds", "战损余额不足", 402)
                order_no = self._new_order_no()
                con.execute(
                    "UPDATE customers SET balance_minutes=balance_minutes-?,balance_rounds=balance_rounds-?,updated_at=? WHERE id=?",
                    (requested_minutes, requested_rounds, now_s, customer_id),
                )
                cur = con.execute(
                    """INSERT INTO local_orders(customer_id,status,local_order_no,requested_minutes,requested_rounds,team_code,quality,amount_cents,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (customer_id, "claiming_device", order_no, requested_minutes, requested_rounds, team_code, quality, 0, now_s, now_s),
                )
                order_id = int(cur.lastrowid)
                con.commit()
            except Exception:
                con.rollback()
                raise

        merchant_context_ref = opaque_merchant_ref(order_no, self.merchant_ref_secret)
        selection_policy = self._bridge_selection_policy(settings, quality)
        try:
            sess = self.bridge.create_control_session(merchant_context_ref=merchant_context_ref, idem=f"claim:{order_no}", selection_policy=selection_policy)
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
                ace_enabled=bool(settings.get("ace_enabled")),
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
                    rounds = int(order["requested_rounds"] or 0)
                    con.execute("UPDATE customers SET balance_minutes=balance_minutes+?,balance_rounds=balance_rounds+?,updated_at=? WHERE id=?", (minutes, rounds, iso(), int(order["customer_id"])))
                    con.execute(
                        "INSERT OR IGNORE INTO refund_records(local_order_id,customer_id,minutes,rounds,reason,created_at) VALUES(?,?,?,?,?,?)",
                        (order_id, int(order["customer_id"]), minutes, rounds, "bridge_claim_or_bundle_failed", iso()),
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
        rounds = int(order.get("requested_rounds") or 0)
        now_s = iso()
        if minutes > 0 or rounds > 0:
            con.execute("UPDATE customers SET balance_minutes=balance_minutes+?,balance_rounds=balance_rounds+?,updated_at=? WHERE id=?", (minutes, rounds, now_s, customer_id))
            con.execute(
                "INSERT OR IGNORE INTO refund_records(local_order_id,customer_id,minutes,rounds,reason,created_at) VALUES(?,?,?,?,?,?)",
                (order_id, customer_id, minutes, rounds, reason, now_s),
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
