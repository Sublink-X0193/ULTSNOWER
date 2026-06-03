from __future__ import annotations

import math
from html import escape as html_escape
from html.parser import HTMLParser
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
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


_ALLOWED_NOTICE_TAGS = {"b", "strong", "i", "em", "u", "p", "br", "ul", "ol", "li", "a", "div", "span"}
_VOID_NOTICE_TAGS = {"br"}


def normalize_public_http_url(value: Any, *, max_len: int = 300) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > max_len:
        raise MerchantError("bad_setting", "global_radar_url 最多 300 字")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise MerchantError("bad_setting", "global_radar_url 只允许 http:// 或 https:// 网址")
    return url


def _notice_href(value: Any) -> str:
    href = str(value or "").strip()
    if not href or len(href) > 500:
        return ""
    parsed = urlparse(href)
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return href
    return ""


class _NoticeSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        if tag_l not in _ALLOWED_NOTICE_TAGS:
            return
        if tag_l == "a":
            href = ""
            for name, value in attrs:
                if name.lower() == "href":
                    href = _notice_href(value)
                    break
            if href:
                self.out.append(f'<a href="{html_escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
            else:
                self.out.append("<a>")
            return
        self.out.append(f"<{tag_l}>")

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in _ALLOWED_NOTICE_TAGS and tag_l not in _VOID_NOTICE_TAGS:
            self.out.append(f"</{tag_l}>")

    def handle_data(self, data: str) -> None:
        self.out.append(html_escape(data, quote=False))


def sanitize_notice_html(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parser = _NoticeSanitizer()
    try:
        parser.feed(raw)
        parser.close()
        return "".join(parser.out)
    except Exception:
        return html_escape(raw, quote=False)


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
    def _bridge_error_message(self, e: BridgeClientError) -> str:
        """Translate central coordination errors into operator-safe merchant prompts."""
        if e.code in {"device_has_active_external_session", "device_has_active_control_session"}:
            return "中央提示该设备存在活动外部控制会话。请先等待订单结束/释放会话；如需强制维护，请在中央控制台使用 force=true。"
        if e.code == "idempotency_conflict":
            return "中央 Bridge 幂等键冲突：同一 X-Idempotency-Key 已用于不同请求。请刷新状态后重试，不要复用该幂等键提交不同内容。"
        if e.code == "idempotency_in_progress":
            return "中央 Bridge 正在处理相同幂等请求，请稍后用同一操作重试。"
        return e.message

    def _raise_bridge_error(self, e: BridgeClientError) -> None:
        raise MerchantError(e.code, self._bridge_error_message(e), e.status_code)

    def _get_state(self, con: sqlite3.Connection, key: str, default: str = "") -> str:
        row = con.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _set_state(self, con: sqlite3.Connection, key: str, value: str) -> None:
        con.execute(
            "INSERT INTO app_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, iso()),
        )

    def _put_setting_locked(self, con: sqlite3.Connection, key: str, value: Any, admin_id: int | None = None) -> None:
        con.execute(
            """INSERT INTO merchant_settings(key,value_json,updated_at,updated_by_admin_id)
               VALUES(?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at,updated_by_admin_id=excluded.updated_by_admin_id""",
            (key, dumps(value), iso(), admin_id),
        )

    def _log_audit_locked(
        self,
        con: sqlite3.Connection,
        *,
        actor_type: str,
        actor_id: Any = None,
        actor_username: str | None = None,
        action: str,
        resource_type: str,
        resource_id: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        actor_type = str(actor_type or "system").strip().lower()
        if actor_type not in {"admin", "customer", "system", "guest"}:
            actor_type = "system"
        admin_id = int(actor_id) if actor_type == "admin" and actor_id is not None else None
        admin_username = str(actor_username or "") if actor_type == "admin" and actor_username else None
        actor_id_i = int(actor_id) if actor_id is not None else None
        con.execute(
            """INSERT INTO admin_audit_logs(
                 admin_id,admin_username,actor_type,actor_id,actor_username,
                 action,resource_type,resource_id,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                admin_id,
                admin_username,
                actor_type,
                actor_id_i,
                str(actor_username or "") if actor_username is not None else None,
                action,
                resource_type,
                str(resource_id) if resource_id is not None else None,
                dumps(metadata or {}),
                iso(),
            ),
        )

    def _log_admin_action_locked(
        self,
        con: sqlite3.Connection,
        admin: dict[str, Any] | None,
        action: str,
        resource_type: str,
        resource_id: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._log_audit_locked(
            con,
            actor_type="admin" if admin else "system",
            actor_id=(admin.get("id") if admin else None),
            actor_username=(str(admin.get("username") or "") if admin else None),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )

    def _log_customer_action_locked(
        self,
        con: sqlite3.Connection,
        customer_id: int | None,
        username: str | None,
        action: str,
        resource_type: str,
        resource_id: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._log_audit_locked(
            con,
            actor_type="customer",
            actor_id=customer_id,
            actor_username=username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )

    def log_admin_action(self, admin: dict[str, Any] | None, action: str, resource_type: str, resource_id: Any = None, metadata: dict[str, Any] | None = None) -> None:
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, action, resource_type, resource_id, metadata)

    def log_customer_action(self, customer_id: int | None, username: str | None, action: str, resource_type: str, resource_id: Any = None, metadata: dict[str, Any] | None = None) -> None:
        with self.db.connect() as con:
            self._log_customer_action_locked(con, customer_id, username, action, resource_type, resource_id, metadata)

    def log_audit_event(self, actor_type: str, actor_id: Any, actor_username: str | None, action: str, resource_type: str, resource_id: Any = None, metadata: dict[str, Any] | None = None) -> None:
        with self.db.connect() as con:
            self._log_audit_locked(con, actor_type=actor_type, actor_id=actor_id, actor_username=actor_username, action=action, resource_type=resource_type, resource_id=resource_id, metadata=metadata)

    def admin_audit_logs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT * FROM admin_audit_logs
                   ORDER BY id DESC LIMIT ?""",
                (max(1, min(int(limit or 200), 1000)),),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["metadata"] = loads(item.pop("metadata_json", "{}"), {})
            item["actor_type"] = item.get("actor_type") or ("admin" if item.get("admin_id") is not None else "system")
            item["actor_id"] = item.get("actor_id") if item.get("actor_id") is not None else item.get("admin_id")
            item["actor_username"] = item.get("actor_username") or item.get("admin_username")
            out.append(item)
        return out

    def _backup_dir(self) -> Path:
        if self.db.path == Path(":memory:"):
            raise MerchantError("backup_unavailable", "内存数据库不支持备份", 409)
        d = self.db.path.parent / "db_backups"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _backup_meta(path: Path, *, current: bool = False) -> dict[str, Any]:
        st = path.stat()
        return {
            "name": path.name,
            "size": int(st.st_size),
            "size_kb": round(st.st_size / 1024, 1),
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=LOCAL_TZ).isoformat(),
            "current": current,
        }

    def _resolve_backup_path(self, name: str) -> Path:
        safe = Path(str(name or "")).name
        if not safe.endswith(".sqlite") and not safe.endswith(".db"):
            raise MerchantError("bad_backup_name", "备份文件名不合法", 400)
        path = self._backup_dir() / safe
        if not path.exists() or not path.is_file():
            raise MerchantError("not_found", "备份不存在", 404)
        return path

    def admin_list_backups(self) -> dict[str, Any]:
        if self.db.path == Path(":memory:"):
            return {"backups": [], "retention": 20}
        current = self._backup_meta(self.db.path, current=True) if self.db.path.exists() else None
        rows = [self._backup_meta(p) for p in sorted(self._backup_dir().glob("*.sqlite"), key=lambda x: x.stat().st_mtime, reverse=True)]
        if current:
            rows.insert(0, current)
        return {"backups": rows, "retention": 20}

    def _prune_backups(self, keep: int = 20) -> None:
        files = sorted(self._backup_dir().glob("*.sqlite"), key=lambda x: x.stat().st_mtime, reverse=True)
        for p in files[max(1, keep):]:
            try:
                p.unlink()
            except OSError:
                pass

    def admin_create_backup(self, admin: dict[str, Any] | None, *, label: str = "manual") -> dict[str, Any]:
        target = self._backup_dir() / f"merchant_{label}_{utcnow().strftime('%Y%m%d_%H%M%S')}.sqlite"
        # Write the audit entry before the backup so the backup artifact itself
        # contains evidence of who created it. If backup later fails, the
        # exception surfaces and the operator can retry.
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "backup_create", "backup", target.name, {"label": label})
        with self.db.connect() as src, sqlite3.connect(str(target)) as dst:
            src.backup(dst)
        self._prune_backups()
        return self._backup_meta(target)

    def admin_restore_backup(self, admin: dict[str, Any] | None, name: str) -> dict[str, Any]:
        src_path = self._resolve_backup_path(name)
        pre = self.admin_create_backup(admin, label="pre_restore")
        with sqlite3.connect(str(src_path)) as src, self.db.connect() as dst:
            src.backup(dst)
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "backup_restore", "backup", src_path.name, {"pre_restore": pre["name"]})
        return {"restored": self._backup_meta(src_path), "pre_restore": pre}

    def bridge_config_view(self) -> dict[str, Any]:
        settings = self.get_settings()
        with self.db.connect() as con:
            rows = {r["key"]: loads(r["value_json"], "") for r in con.execute("SELECT key,value_json FROM merchant_settings WHERE key LIKE 'bridge_%'").fetchall()}
        base_url = str(rows.get("bridge_base_url") or getattr(self.bridge, "base_url", "") or "")
        merchant_key = str(rows.get("bridge_merchant_key") or getattr(self.bridge, "merchant_key", "") or "")
        secret = str(rows.get("bridge_merchant_secret") or getattr(self.bridge, "merchant_secret", "") or "")
        configured = bool(rows.get("bridge_configured")) and bool(base_url and merchant_key and secret)
        return {
            "configured": configured,
            "setup_required": self.bridge_setup_required(),
            "bridge_base_url": base_url,
            "bridge_merchant_key": merchant_key,
            "bridge_merchant_secret_set": bool(secret),
            "bridge_api_prefix": str(getattr(self.bridge, "api_prefix", "") or ""),
            "bridge_auth_header_prefix": str(getattr(self.bridge, "auth_header_prefix", "") or ""),
            "system_name": settings.get("system_name") or "SNOW 自助下单",
        }

    def bridge_setup_required(self) -> bool:
        with self.db.connect() as con:
            rows = {r["key"]: loads(r["value_json"], "") for r in con.execute("SELECT key,value_json FROM merchant_settings WHERE key LIKE 'bridge_%'").fetchall()}
        configured = bool(rows.get("bridge_configured")) and bool(rows.get("bridge_base_url")) and bool(rows.get("bridge_merchant_key")) and bool(rows.get("bridge_merchant_secret"))
        if configured:
            return False
        key = str(getattr(self.bridge, "merchant_key", "") or "")
        secret = str(getattr(self.bridge, "merchant_secret", "") or "")
        # Tests and explicit custom clients should not be blocked; real default
        # mk_test/secret deployments should show the first-run setup wizard.
        if self.bridge.__class__.__name__ != "BridgeClient":
            return False
        return not (key and secret and (key != "mk_test" or secret != "secret"))

    def update_bridge_config(self, admin: dict[str, Any] | None, *, base_url: str, merchant_key: str, merchant_secret: str) -> dict[str, Any]:
        base_url = str(base_url or "").strip().rstrip("/")
        merchant_key = str(merchant_key or "").strip()
        merchant_secret = str(merchant_secret or "").strip()
        if not base_url.startswith(("http://", "https://")):
            raise MerchantError("bad_bridge_url", "中央 Bridge 地址必须以 http:// 或 https:// 开头")
        if len(merchant_key) < 3 or len(merchant_key) > 128:
            raise MerchantError("bad_bridge_key", "Merchant Key 长度不合法")
        if len(merchant_secret) < 6 or len(merchant_secret) > 512:
            raise MerchantError("bad_bridge_secret", "Merchant Secret 长度不合法")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                self._put_setting_locked(con, "bridge_base_url", base_url, int(admin["id"]) if admin else None)
                self._put_setting_locked(con, "bridge_merchant_key", merchant_key, int(admin["id"]) if admin else None)
                self._put_setting_locked(con, "bridge_merchant_secret", merchant_secret, int(admin["id"]) if admin else None)
                self._put_setting_locked(con, "bridge_configured", True, int(admin["id"]) if admin else None)
                self._log_admin_action_locked(con, admin, "bridge_config_update", "bridge", merchant_key, {"base_url": base_url})
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.bridge_config_view()

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
        out["order_options"] = loads(out.get("order_options_json"), {}) if isinstance(out.get("order_options_json"), str) else (out.get("order_options_json") or {})
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
                if con.execute("SELECT 1 FROM merchant_admins WHERE lower(username)=lower(?)", (username,)).fetchone():
                    raise MerchantError("bad_username", "此账户名不合法", 400)
                cur = con.execute(
                    """INSERT INTO customers(
                         username,password_hash,
                         balance_minutes,balance_rounds,
                         balance_machine_minutes,balance_machine_rounds,
                         balance_absolute_minutes,balance_absolute_rounds,
                         status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (username, hash_password(password), 0, 0, 0, 0, 0, 0, "active", now_s, now_s),
                )
                customer_id = int(cur.lastrowid)
                self._log_customer_action_locked(con, customer_id, username, "customer_register", "customer", customer_id, {"source": "register"})
                con.commit()
                return {"id": customer_id, "username": username, "balance_minutes": 0, "balance_rounds": 0}
            except sqlite3.IntegrityError:
                con.rollback()
                self.log_audit_event("customer", None, username, "customer_register_failed", "customer", None, {"reason": "username_exists"})
                raise MerchantError("username_exists", "用户名已存在", 409)
            except Exception:
                con.rollback()
                raise

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        username = str(username or "").strip()
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM customers WHERE username=? AND status='active'", (username,)).fetchone()
            if not row or not verify_password(str(password or ""), row["password_hash"]):
                self._log_audit_locked(
                    con,
                    actor_type="customer",
                    actor_id=int(row["id"]) if row else None,
                    actor_username=username,
                    action="customer_login_failed",
                    resource_type="auth",
                    resource_id=username or None,
                    metadata={"reason": "bad_credentials"},
                )
                raise MerchantError("bad_credentials", "用户名或密码错误", 401)
            return self.public_customer(dict(row))

    def create_session(self, customer_id: int) -> str:
        sid = secrets.token_urlsafe(32)
        now_s = iso()
        expires = iso(utcnow() + timedelta(seconds=self.session_ttl_seconds))
        with self.db.connect() as con:
            self._cleanup_expired_sessions_locked(con)
            row = con.execute("SELECT * FROM customers WHERE id=? AND status='active'", (customer_id,)).fetchone()
            if not row:
                raise MerchantError("not_found", "客户不存在", 404)
            con.execute("INSERT INTO sessions(sid,customer_id,username,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)", (sid, customer_id, row["username"], expires, now_s, now_s))
        return sid

    def _cleanup_expired_sessions_locked(self, con: sqlite3.Connection) -> int:
        cur = con.execute("DELETE FROM sessions WHERE expires_at<=?", (iso(),))
        return int(cur.rowcount or 0)

    def delete_session(self, sid: str) -> None:
        if not sid:
            return
        with self.db.connect() as con:
            row = con.execute("SELECT customer_id,username FROM sessions WHERE sid=?", (sid,)).fetchone()
            con.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            if row:
                self._log_customer_action_locked(con, int(row["customer_id"]), row["username"], "customer_logout", "session", None, {})

    def customer_from_session(self, sid: str | None, *, renew: bool = True) -> dict[str, Any] | None:
        if not sid:
            return None
        with self.db.connect() as con:
            row = con.execute(
                """SELECT c.*, s.expires_at AS session_expires_at, s.last_seen_at AS session_last_seen_at
                   FROM sessions s JOIN customers c ON c.id=s.customer_id
                   WHERE s.sid=? AND s.expires_at>? AND c.status='active'""",
                (sid, iso()),
            ).fetchone()
            if not row:
                con.execute("DELETE FROM sessions WHERE sid=? AND expires_at<=?", (sid, iso()))
                return None
            item = self.public_customer(dict(row))
            if renew:
                now_dt = utcnow()
                expires_at = parse_ts(row["session_expires_at"])
                last_seen_at = parse_ts(row["session_last_seen_at"])
                refresh_seen = (not last_seen_at) or (now_dt - last_seen_at).total_seconds() >= min(600, max(60, self.session_ttl_seconds // 8))
                refresh_expiry = (not expires_at) or (expires_at - now_dt).total_seconds() <= max(300, self.session_ttl_seconds // 2)
                if refresh_seen or refresh_expiry:
                    new_exp = iso(now_dt + timedelta(seconds=self.session_ttl_seconds)) if refresh_expiry else row["session_expires_at"]
                    con.execute("UPDATE sessions SET last_seen_at=?,expires_at=? WHERE sid=?", (iso(now_dt), new_exp, sid))
            return item

    @staticmethod
    def _mode_balance_columns(mode_or_quality: Any) -> tuple[str, str]:
        raw = str(mode_or_quality or "").strip().lower()
        if raw in {"absolute", "secret", "绝密"}:
            return "balance_absolute_minutes", "balance_absolute_rounds"
        return "balance_machine_minutes", "balance_machine_rounds"

    @staticmethod
    def _mode_from_quality(quality: Any) -> str:
        return "absolute" if str(quality or "").strip().lower() in {"absolute", "secret", "绝密"} else "machine"

    def _sync_customer_balance_locked(self, con: sqlite3.Connection, customer_id: int) -> None:
        con.execute(
            """UPDATE customers
               SET balance_minutes=balance_machine_minutes+balance_absolute_minutes,
                   balance_rounds=balance_machine_rounds+balance_absolute_rounds,
                   updated_at=?
               WHERE id=?""",
            (iso(), customer_id),
        )

    def public_customer(self, row: dict[str, Any]) -> dict[str, Any]:
        # New merchant split keeps machine/absolute balances independent.
        # Fallback handles rows from pre-migration tests or stale DB snapshots.
        machine_minutes = int(row.get("balance_machine_minutes") if row.get("balance_machine_minutes") is not None else row.get("balance_minutes") or 0)
        machine_rounds = int(row.get("balance_machine_rounds") if row.get("balance_machine_rounds") is not None else row.get("balance_rounds") or 0)
        absolute_minutes = int(row.get("balance_absolute_minutes") or 0)
        absolute_rounds = int(row.get("balance_absolute_rounds") or 0)
        return {
            "id": int(row["id"]),
            "username": row["username"],
            "balance_minutes": machine_minutes + absolute_minutes,
            "balance_rounds": machine_rounds + absolute_rounds,
            "balance_machine_minutes": machine_minutes,
            "balance_machine_rounds": machine_rounds,
            "balance_absolute_minutes": absolute_minutes,
            "balance_absolute_rounds": absolute_rounds,
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
            self._cleanup_expired_sessions_locked(con)
            customer_count = int(con.execute("SELECT COUNT(*) AS n FROM customers").fetchone()["n"])
            active_qmarks = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
            online_count = int(
                con.execute(
                    f"""SELECT COUNT(DISTINCT customer_id) AS n FROM (
                            SELECT customer_id FROM sessions WHERE expires_at>?
                            UNION
                            SELECT customer_id FROM local_orders WHERE status IN ({active_qmarks})
                        )""",
                    (now_s, *sorted(ACTIVE_ORDER_STATUSES)),
                ).fetchone()["n"]
            )
            active_order_count = int(
                con.execute(
                    f"SELECT COUNT(*) AS n FROM local_orders WHERE status IN ({active_qmarks})",
                    tuple(sorted(ACTIVE_ORDER_STATUSES)),
                ).fetchone()["n"]
            )
            running_count = int(con.execute("SELECT COUNT(*) AS n FROM local_orders WHERE status='running'").fetchone()["n"])
            total_balance_minutes = int(con.execute("SELECT COALESCE(SUM(balance_machine_minutes+balance_absolute_minutes),0) AS n FROM customers").fetchone()["n"] or 0)
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
            self._cleanup_expired_sessions_locked(con)
            online_rows = con.execute("SELECT customer_id, MAX(COALESCE(last_seen_at,created_at)) AS last_seen FROM sessions WHERE expires_at>? GROUP BY customer_id", (now_s,)).fetchall()
            online = {int(r["customer_id"]): r["last_seen"] for r in online_rows}
            rows = con.execute("SELECT * FROM customers ORDER BY id DESC LIMIT ?", (max(1, min(limit, 2000)),)).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                item = self.public_customer(dict(r))
                item["created_at"] = r["created_at"]
                item["updated_at"] = r["updated_at"]
                active = self._active_order_row(con, int(r["id"]))
                item["active_order"] = self._admin_order_view(self._order_with_binding(con, int(active["id"]))) if active else None
                item["active_order_status"] = item["active_order"]["status"] if item["active_order"] else ""
                item["active_order_remaining_minutes"] = item["active_order"]["remaining_minutes"] if item["active_order"] else 0
                token_online = int(r["id"]) in online
                order_online = bool(item["active_order"])
                item["online"] = token_online or order_online
                if token_online and order_online:
                    item["online_reason"] = "token+order"
                elif token_online:
                    item["online_reason"] = "token"
                elif order_online:
                    item["online_reason"] = "order"
                else:
                    item["online_reason"] = ""
                item["last_seen_at"] = online.get(int(r["id"])) or (item["active_order"]["updated_at"] if item["active_order"] else None)
                blob = dumps(item).lower()
                if online_only and not item["online"]:
                    continue
                if keyword_l and keyword_l not in blob:
                    continue
                out.append(item)
            return out

    def _activity_local_date(self, dt: datetime | None = None) -> str:
        return (dt or utcnow()).astimezone(LOCAL_TZ).date().isoformat()

    def _record_activity_locked(
        self,
        con: sqlite3.Connection,
        *,
        event_type: str,
        customer_id: int,
        username: str,
        order: dict[str, Any] | sqlite3.Row | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        order_d = dict(order) if order is not None else {}
        con.execute(
            """INSERT INTO customer_activity_events(
                 event_type, customer_id, username, order_id, order_status, order_quality,
                 order_minutes, order_rounds, metadata_json, local_date, created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_type,
                int(customer_id),
                username,
                int(order_d["id"]) if order_d.get("id") is not None else None,
                order_d.get("status") if order_d else "none",
                order_d.get("quality") if order_d else None,
                int(order_d.get("requested_minutes") or 0),
                int(order_d.get("requested_rounds") or 0),
                dumps(metadata or {}),
                self._activity_local_date(),
                iso(),
            ),
        )

    def record_customer_login(self, customer_id: int, *, source: str = "login") -> None:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
            if not row:
                return
            active = self._active_order_row(con, customer_id)
            self._record_activity_locked(
                con,
                event_type="login" if source != "night_login" else "night_login",
                customer_id=customer_id,
                username=row["username"],
                order=dict(active) if active else None,
                metadata={"source": source, "active_order_status_at_login": active["status"] if active else "none"},
            )
            action = "customer_night_login" if source == "night_login" else "customer_login"
            self._log_customer_action_locked(
                con,
                customer_id,
                row["username"],
                action,
                "auth",
                customer_id,
                {"source": source, "active_order_status_at_login": active["status"] if active else "none"},
            )

    def _record_order_activity(self, order_id: int, *, event_type: str = "order_created") -> None:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT o.*, c.username
                   FROM local_orders o JOIN customers c ON c.id=o.customer_id
                   WHERE o.id=?""",
                (order_id,),
            ).fetchone()
            if not row:
                return
            self._record_activity_locked(
                con,
                event_type=event_type,
                customer_id=int(row["customer_id"]),
                username=row["username"],
                order=dict(row),
                metadata={"mode": self._mode_from_quality(row["quality"])},
            )
            action = "customer_order_create" if event_type == "order_created" else f"customer_{event_type}"
            self._log_customer_action_locked(
                con,
                int(row["customer_id"]),
                row["username"],
                action,
                "order",
                int(row["id"]),
                {
                    "status": row["status"],
                    "quality": row["quality"],
                    "mode": self._mode_from_quality(row["quality"]),
                    "requested_minutes": int(row["requested_minutes"] or 0),
                    "requested_rounds": int(row["requested_rounds"] or 0),
                    "team_code": row["team_code"],
                },
            )

    def admin_activity_stats(self, *, local_date: str | None = None) -> dict[str, Any]:
        day = str(local_date or self._activity_local_date()).strip()[:10]
        with self.db.connect() as con:
            login_types = ("login", "night_login")
            login_customer_count = int(
                con.execute(
                    f"SELECT COUNT(DISTINCT customer_id) AS n FROM customer_activity_events WHERE local_date=? AND event_type IN ({','.join('?' for _ in login_types)})",
                    (day, *login_types),
                ).fetchone()["n"]
                or 0
            )
            order_customer_count = int(
                con.execute(
                    "SELECT COUNT(DISTINCT customer_id) AS n FROM customer_activity_events WHERE local_date=? AND event_type='order_created'",
                    (day,),
                ).fetchone()["n"]
                or 0
            )
            order_row = con.execute(
                """SELECT COUNT(*) AS order_count, COALESCE(SUM(order_minutes),0) AS minutes
                   FROM customer_activity_events
                   WHERE local_date=? AND event_type='order_created'""",
                (day,),
            ).fetchone()
            no_order_rows = con.execute(
                f"""SELECT l.customer_id, l.username, COUNT(*) AS login_count, MAX(l.created_at) AS last_login_at,
                           COALESCE((SELECT e.order_status
                                     FROM customer_activity_events e
                                     WHERE e.local_date=? AND e.customer_id=l.customer_id AND e.event_type IN ({','.join('?' for _ in login_types)})
                                     ORDER BY e.created_at DESC LIMIT 1), 'none') AS order_status_at_login
                    FROM customer_activity_events l
                    WHERE l.local_date=? AND l.event_type IN ({','.join('?' for _ in login_types)})
                      AND NOT EXISTS (
                        SELECT 1 FROM customer_activity_events o
                        WHERE o.local_date=? AND o.customer_id=l.customer_id AND o.event_type='order_created'
                      )
                    GROUP BY l.customer_id, l.username
                    ORDER BY last_login_at DESC
                    LIMIT 100""",
                (day, *login_types, day, *login_types, day),
            ).fetchall()
            status_rows = con.execute(
                f"""SELECT COALESCE(order_status,'none') AS status, COUNT(*) AS n
                    FROM customer_activity_events
                    WHERE local_date=? AND event_type IN ({','.join('?' for _ in login_types)})
                    GROUP BY COALESCE(order_status,'none')
                    ORDER BY n DESC""",
                (day, *login_types),
            ).fetchall()
            order_mode_rows = con.execute(
                """SELECT COALESCE(order_quality,'standard') AS quality, COUNT(*) AS n, COALESCE(SUM(order_minutes),0) AS minutes
                   FROM customer_activity_events
                   WHERE local_date=? AND event_type='order_created'
                   GROUP BY COALESCE(order_quality,'standard')
                   ORDER BY n DESC""",
                (day,),
            ).fetchall()
        order_minutes = int(order_row["minutes"] or 0)
        return {
            "local_date": day,
            "login_customer_count": login_customer_count,
            "login_without_order_count": len(no_order_rows),
            "order_customer_count": order_customer_count,
            "order_count": int(order_row["order_count"] or 0),
            "order_minutes": order_minutes,
            "order_hours": round(order_minutes / 60.0, 2),
            "login_status_breakdown": [{"status": r["status"], "count": int(r["n"] or 0)} for r in status_rows],
            "order_quality_breakdown": [{"quality": r["quality"], "count": int(r["n"] or 0), "minutes": int(r["minutes"] or 0), "hours": round(int(r["minutes"] or 0) / 60.0, 2)} for r in order_mode_rows],
            "login_without_order_customers": [
                {
                    "customer_id": int(r["customer_id"]),
                    "username": r["username"],
                    "login_count": int(r["login_count"] or 0),
                    "last_login_at": r["last_login_at"],
                    "order_status_at_login": r["order_status_at_login"] or "none",
                }
                for r in no_order_rows
            ],
        }

    def _analytics_period_bounds(self, *, period: str = "day", anchor: str | None = None) -> tuple[datetime, datetime, str]:
        period = str(period or "day").lower()
        if period not in {"day", "week", "month"}:
            raise MerchantError("bad_period", "period 必须是 day/week/month")
        try:
            base_date = datetime.fromisoformat(str(anchor or self._activity_local_date())[:10]).date()
        except Exception:
            raise MerchantError("bad_date", "日期格式必须是 YYYY-MM-DD")
        if period == "week":
            start_date = base_date - timedelta(days=base_date.weekday())
            days = 7
        elif period == "month":
            start_date = base_date.replace(day=1)
            next_month = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
            days = (next_month - start_date).days
        else:
            start_date = base_date
            days = 1
        start_local = datetime.combine(start_date, time.min, tzinfo=LOCAL_TZ)
        end_local = start_local + timedelta(days=days)
        return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC")), start_date.isoformat()

    def admin_order_analytics(self, *, period: str = "day", date: str | None = None) -> dict[str, Any]:
        start_utc, end_utc, start_label = self._analytics_period_bounds(period=period, anchor=date)
        start_iso, end_iso = iso(start_utc), iso(end_utc)
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT o.*, c.username AS customer_username
                   FROM local_orders o JOIN customers c ON c.id=o.customer_id
                   WHERE o.created_at>=? AND o.created_at<?
                   ORDER BY o.created_at ASC""",
                (start_iso, end_iso),
            ).fetchall()
        daily: dict[str, dict[str, Any]] = {}
        status: dict[str, int] = {}
        quality: dict[str, dict[str, int]] = {}
        ranks: dict[int, dict[str, Any]] = {}
        total_minutes = 0
        completed_minutes = 0
        failed_orders = 0
        for r in rows:
            d = dict(r)
            created = parse_ts(d.get("created_at")) or utcnow()
            local_day = created.astimezone(LOCAL_TZ).date().isoformat()
            bucket = daily.setdefault(local_day, {"date": local_day, "order_count": 0, "customer_ids": set(), "minutes": 0, "hours": 0.0})
            minutes = int(d.get("requested_minutes") or 0)
            total_minutes += minutes
            bucket["order_count"] += 1
            bucket["customer_ids"].add(int(d["customer_id"]))
            bucket["minutes"] += minutes
            st = str(d.get("status") or "unknown")
            status[st] = status.get(st, 0) + 1
            if st in {"failed", "interrupted_by_admin", "interrupted_by_disconnect"}:
                failed_orders += 1
            if st == "finished":
                completed_minutes += minutes
            q = str(d.get("quality") or "standard")
            qrow = quality.setdefault(q, {"order_count": 0, "minutes": 0})
            qrow["order_count"] += 1
            qrow["minutes"] += minutes
            cid = int(d["customer_id"])
            rank = ranks.setdefault(cid, {"customer_id": cid, "username": d.get("customer_username") or cid, "order_count": 0, "minutes": 0})
            rank["order_count"] += 1
            rank["minutes"] += minutes
        daily_series = []
        day_count = max(1, (end_utc.astimezone(LOCAL_TZ).date() - start_utc.astimezone(LOCAL_TZ).date()).days)
        first_local = start_utc.astimezone(LOCAL_TZ).date()
        for i in range(day_count):
            label = (first_local + timedelta(days=i)).isoformat()
            b = daily.get(label, {"date": label, "order_count": 0, "customer_ids": set(), "minutes": 0})
            minutes = int(b["minutes"] or 0)
            daily_series.append({
                "date": label,
                "order_count": int(b["order_count"] or 0),
                "customer_count": len(b["customer_ids"]),
                "minutes": minutes,
                "hours": round(minutes / 60.0, 2),
            })
        rank_rows = sorted(ranks.values(), key=lambda x: (-int(x["minutes"]), -int(x["order_count"]), str(x["username"])))[:50]
        for r in rank_rows:
            r["hours"] = round(int(r["minutes"]) / 60.0, 2)
        return {
            "period": period,
            "date": date or self._activity_local_date(),
            "start_date": start_label,
            "end_date": (end_utc.astimezone(LOCAL_TZ).date() - timedelta(days=1)).isoformat(),
            "order_count": len(rows),
            "customer_count": len({int(r["customer_id"]) for r in rows}),
            "requested_minutes": total_minutes,
            "requested_hours": round(total_minutes / 60.0, 2),
            "completed_minutes": completed_minutes,
            "completed_hours": round(completed_minutes / 60.0, 2),
            "failed_order_count": failed_orders,
            "daily_series": daily_series,
            "status_breakdown": [{"status": k, "count": v} for k, v in sorted(status.items(), key=lambda x: (-x[1], x[0]))],
            "quality_breakdown": [{"quality": k, "order_count": v["order_count"], "minutes": v["minutes"], "hours": round(v["minutes"] / 60.0, 2)} for k, v in sorted(quality.items(), key=lambda x: (-x[1]["minutes"], x[0]))],
            "customer_rank": rank_rows,
        }

    def admin_create_customer(self, *, username: str, password: str, balance_minutes: int = 0, balance_rounds: int = 0, status: str = "active") -> dict[str, Any]:
        customer = self.register_customer(username, password)
        status = status if status in {"active", "frozen"} else "active"
        with self.db.connect() as con:
            con.execute(
                """UPDATE customers
                   SET balance_minutes=?,balance_rounds=?,
                       balance_machine_minutes=?,balance_machine_rounds=?,
                       balance_absolute_minutes=0,balance_absolute_rounds=0,
                       status=?,updated_at=?
                   WHERE id=?""",
                (
                    max(0, int(balance_minutes or 0)),
                    max(0, int(balance_rounds or 0)),
                    max(0, int(balance_minutes or 0)),
                    max(0, int(balance_rounds or 0)),
                    status,
                    iso(),
                    customer["id"],
                ),
            )
        return self.get_customer(customer["id"])

    def admin_update_customer_balance(
        self,
        customer_id: int,
        *,
        balance_minutes: int | None = None,
        balance_rounds: int | None = None,
        balance_machine_minutes: int | None = None,
        balance_machine_rounds: int | None = None,
        balance_absolute_minutes: int | None = None,
        balance_absolute_rounds: int | None = None,
        delta_minutes: int | None = None,
        delta_rounds: int | None = None,
        delta_machine_minutes: int | None = None,
        delta_machine_rounds: int | None = None,
        delta_absolute_minutes: int | None = None,
        delta_absolute_rounds: int | None = None,
    ) -> dict[str, Any]:
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
                if not row:
                    raise MerchantError("not_found", "客户不存在", 404)
                machine_minutes = int(row["balance_machine_minutes"] or 0)
                machine_rounds = int(row["balance_machine_rounds"] or 0)
                absolute_minutes = int(row["balance_absolute_minutes"] or 0)
                absolute_rounds = int(row["balance_absolute_rounds"] or 0)
                # 兼容旧接口：balance_minutes / balance_rounds 仍表示“机密”余额。
                if balance_minutes is not None:
                    machine_minutes = int(balance_minutes)
                if balance_rounds is not None:
                    machine_rounds = int(balance_rounds)
                if balance_machine_minutes is not None:
                    machine_minutes = int(balance_machine_minutes)
                if balance_machine_rounds is not None:
                    machine_rounds = int(balance_machine_rounds)
                if balance_absolute_minutes is not None:
                    absolute_minutes = int(balance_absolute_minutes)
                if balance_absolute_rounds is not None:
                    absolute_rounds = int(balance_absolute_rounds)
                if delta_minutes is not None:
                    machine_minutes += int(delta_minutes)
                if delta_rounds is not None:
                    machine_rounds += int(delta_rounds)
                if delta_machine_minutes is not None:
                    machine_minutes += int(delta_machine_minutes)
                if delta_machine_rounds is not None:
                    machine_rounds += int(delta_machine_rounds)
                if delta_absolute_minutes is not None:
                    absolute_minutes += int(delta_absolute_minutes)
                if delta_absolute_rounds is not None:
                    absolute_rounds += int(delta_absolute_rounds)
                machine_minutes = max(0, machine_minutes)
                machine_rounds = max(0, machine_rounds)
                absolute_minutes = max(0, absolute_minutes)
                absolute_rounds = max(0, absolute_rounds)
                con.execute(
                    """UPDATE customers
                       SET balance_machine_minutes=?,balance_machine_rounds=?,
                           balance_absolute_minutes=?,balance_absolute_rounds=?
                       WHERE id=?""",
                    (machine_minutes, machine_rounds, absolute_minutes, absolute_rounds, customer_id),
                )
                self._sync_customer_balance_locked(con, customer_id)
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
                order_count = int(con.execute("SELECT COUNT(*) AS n FROM local_orders WHERE customer_id=?", (customer_id,)).fetchone()["n"])
                if order_count:
                    raise MerchantError("customer_has_orders", "客户已有订单记录，不能直接删除；可冻结账号保留账务记录", 409)
                con.execute("DELETE FROM sessions WHERE customer_id=?", (customer_id,))
                con.execute("DELETE FROM customer_activity_events WHERE customer_id=?", (customer_id,))
                con.execute("DELETE FROM recharge_records WHERE customer_id=?", (customer_id,))
                con.execute("UPDATE recharge_cards SET used_by_customer_id=NULL WHERE used_by_customer_id=?", (customer_id,))
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
                self._raise_bridge_error(e)
        with self.db.connect() as con:
            con.execute("UPDATE local_orders SET status='stopping',fail_reason='admin_stop',updated_at=? WHERE id=?", (iso(), order_id))
        return self.admin_get_order(order_id)

    def _ensure_manual_customer_locked(self, con: sqlite3.Connection, device_id: int) -> int:
        username = f"admin_manual_device_{int(device_id)}"
        row = con.execute("SELECT id FROM customers WHERE username=?", (username,)).fetchone()
        if row:
            return int(row["id"])
        now_s = iso()
        cur = con.execute(
            """INSERT INTO customers(username,password_hash,balance_minutes,balance_rounds,
                       balance_machine_minutes,balance_machine_rounds,balance_absolute_minutes,balance_absolute_rounds,
                       status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (username, hash_password(secrets.token_urlsafe(16)), 0, 0, 0, 0, 0, 0, "active", now_s, now_s),
        )
        return int(cur.lastrowid)

    def _active_order_by_device_locked(self, con: sqlite3.Connection, device_id: int) -> sqlite3.Row | None:
        qmarks = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
        return con.execute(
            f"""SELECT o.*, b.control_session_id, b.fencing_token, b.last_device_epoch, b.id AS binding_id
                FROM local_orders o LEFT JOIN order_control_bindings b ON b.local_order_id=o.id
                WHERE (b.device_id=? OR o.manual_device_id=?) AND o.status IN ({qmarks})
                ORDER BY o.id DESC LIMIT 1""",
            (int(device_id), int(device_id), *sorted(ACTIVE_ORDER_STATUSES)),
        ).fetchone()

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            if value in (None, ""):
                return default
            return int(float(value))
        except Exception:
            return default

    @staticmethod
    def _frontend_work_status(raw: Any) -> str:
        status = str(raw or "").strip()
        mapping = {
            "": "空闲",
            "idle": "空闲",
            "free": "空闲",
            "offline": "离线",
            "disconnected": "离线",
            "lost": "离线",
            "running": "执行中",
            "busy": "执行中",
            "active": "执行中",
            "claimed": "已占用",
            "commanding": "执行中",
            "team_entered": "已进队",
            "team": "已进队",
            "watching": "观战中",
        }
        return mapping.get(status, status)

    @staticmethod
    def _runtime_from_device(device: dict[str, Any]) -> dict[str, Any]:
        runtime = device.get("runtime") if isinstance(device.get("runtime"), dict) else {}
        merged = {**runtime, **{k: v for k, v in device.items() if v is not None}}
        return merged

    def _normalize_admin_device_row(self, d: dict[str, Any], active: dict[str, Any] | None) -> dict[str, Any]:
        runtime = self._runtime_from_device(d)
        did = int(d.get("id") or d.get("device_id") or 0)
        online = bool(d.get("online", True))
        mode = d.get("mode") or d.get("device_mode") or "machine"
        active_view = self._admin_order_view(active) if active else None
        raw_status = runtime.get("work_status") or runtime.get("state") or runtime.get("agent_state") or d.get("control_state") or "idle"
        work_status = self._frontend_work_status(raw_status)
        if not online:
            work_status = "离线"
        elif active and work_status in {"空闲", "离线", "已结束"}:
            work_status = "执行中"
        running_user = runtime.get("running_user") or (active.get("customer_username") if active else "")
        running_boss = runtime.get("running_boss_name") or runtime.get("boss_name") or runtime.get("team_code") or (active.get("team_code") if active else "")
        remaining_minutes = self._to_int(runtime.get("remaining_minutes"), 0)
        if active_view:
            remaining_minutes = int(active_view.get("remaining_minutes") or remaining_minutes or 0)
        boss_id = runtime.get("spectate_boss") or runtime.get("boss_id") or ""
        harvard = runtime.get("harvard") or runtime.get("hfb") or runtime.get("currency_balance") or runtime.get("hfb_value") or ""
        return {
            **d,
            "id": did,
            "device_id": did,
            "device_key": d.get("machine_id") or d.get("device_key") or d.get("machine") or str(did),
            "machine_id": d.get("machine_id") or d.get("device_key") or "",
            "device_name": d.get("display_name") or d.get("device_name") or f"{did}号机",
            "mode": mode,
            "device_mode": mode,
            "enabled": bool(d.get("enabled", True)),
            "accept_orders": bool(d.get("accept_orders", d.get("accepting_orders", d.get("order_enabled", True)))),
            "radar_url": d.get("radar_url") or runtime.get("radar_url") or "",
            "watchdog_card": d.get("watchdog_card") or runtime.get("watchdog_card") or "",
            "watchdog": runtime.get("watchdog") or {},
            "online": online,
            "control_state": (active.get("status") if active else None) or d.get("control_state") or d.get("state") or "idle",
            "agent_state": d.get("agent_state") or runtime.get("agent_state") or d.get("ui_state") or "",
            "ui_state": d.get("ui_state") or runtime.get("ui_state") or "",
            "active_order": active_view,
            "active_customer": active.get("customer_username") if active else "",
            "running_order_id": (active.get("id") if active else None) or self._to_int(runtime.get("running_order_id"), 0) or None,
            "running_user": running_user or "",
            "running_user_id": runtime.get("running_user_id") or (active.get("customer_id") if active else None),
            "running_boss_name": running_boss or "",
            "team_code": runtime.get("team_code") or running_boss or "",
            "boss_name": runtime.get("boss_name") or running_boss or "",
            "running_mode": runtime.get("running_mode") or (active.get("quality") if active else "") or "",
            "work_status": work_status,
            "work_status_detail": runtime.get("work_status_detail") or runtime.get("sub_state") or "",
            "sub_state": runtime.get("sub_state") or "",
            "remaining_minutes": remaining_minutes,
            "remaining_seconds": self._to_int(runtime.get("remaining_seconds"), remaining_minutes * 60),
            "end_time": runtime.get("end_time") or (active.get("end_at") if active else None),
            "end_time_ms": self._to_int(runtime.get("end_time_ms"), 0),
            "estimated_end": runtime.get("estimated_end") or (active.get("end_at") if active else "") or "",
            "cooldown_until_ms": self._to_int(runtime.get("cooldown_until_ms"), 0),
            "harvard": str(harvard or ""),
            "hfb_value": self._to_int(runtime.get("hfb_value") or runtime.get("currency_balance"), 0),
            "spectate_boss": str(boss_id or ""),
            "boss_id": str(boss_id or ""),
            "boss_id_debug": runtime.get("boss_id_debug") or "",
            "round_count": self._to_int(runtime.get("round_count"), 0),
            "run_rounds": self._to_int(runtime.get("run_rounds"), 0),
            "max_rounds": self._to_int(runtime.get("max_rounds") or (active.get("requested_rounds") if active else 0), 0),
            "start_coins": self._to_int(runtime.get("start_coins"), 0),
            "max_coin_loss": self._to_int(runtime.get("max_coin_loss"), 0),
            "actual_coin_loss": self._to_int(runtime.get("actual_coin_loss") or runtime.get("coin_loss"), 0),
            "script_ver": runtime.get("script_ver") or "",
            "updating": bool(runtime.get("updating")),
            "current_map": runtime.get("current_map") or "",
            "in_game": bool(runtime.get("in_game")),
            "last_heartbeat_at": d.get("last_heartbeat_at") or runtime.get("last_heartbeat_at") or "",
            "heartbeat_age_seconds": runtime.get("heartbeat_age_seconds"),
            "prison_stage": runtime.get("prison_stage") or "",
            "prison_stage_label": runtime.get("prison_stage_label") or "",
            "prison_point": runtime.get("prison_point") or "",
            "prison_action": runtime.get("prison_action") or "",
            "prison_score": runtime.get("prison_score"),
            "prison_match": runtime.get("prison_match") or "",
            "prison_region": runtime.get("prison_region") or "",
        }

    def admin_list_devices(self) -> list[dict[str, Any]]:
        bridge_devices: list[dict[str, Any]] = []
        if hasattr(self.bridge, "list_devices"):
            try:
                bridge_devices = [dict(d) for d in self.bridge.list_devices()]
            except BridgeClientError:
                bridge_devices = []
        if not bridge_devices and hasattr(self.bridge, "get_capacity"):
            try:
                cap = self.bridge.get_capacity()
                bridge_devices = [
                    {"id": int(did), "device_id": int(did), "display_name": f"{did}号机", "online": True, "control_state": "idle", "agent_state": "idle"}
                    for did in (cap.get("idle_device_ids") or [])
                ]
            except Exception:
                bridge_devices = []
        with self.db.connect() as con:
            qmarks = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
            rows = con.execute(
                f"""SELECT o.*, c.username AS customer_username,
                           COALESCE(b.device_id,o.manual_device_id) AS device_id,
                           b.control_session_id, b.last_device_epoch
                    FROM local_orders o
                    LEFT JOIN order_control_bindings b ON b.local_order_id=o.id
                    JOIN customers c ON c.id=o.customer_id
                    WHERE o.status IN ({qmarks}) AND COALESCE(b.device_id,o.manual_device_id) IS NOT NULL
                    ORDER BY o.id DESC""",
                tuple(sorted(ACTIVE_ORDER_STATUSES)),
            ).fetchall()
            active_by_device = {int(r["device_id"]): dict(r) for r in rows}
        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for d in bridge_devices:
            did = int(d.get("id") or d.get("device_id") or 0)
            if not did:
                continue
            seen.add(did)
            active = active_by_device.get(did)
            out.append(self._normalize_admin_device_row(d, active))
        for did, active in active_by_device.items():
            if did in seen:
                continue
            out.append(self._normalize_admin_device_row({
                "id": did,
                "device_id": did,
                "device_name": f"{did}号机",
                "online": True,
                "control_state": "busy",
                "agent_state": "",
                "ui_state": "",
            }, active))
        out.sort(key=lambda x: int(x.get("id") or 0))
        return out

    @staticmethod
    def _normalize_device_mode(mode: Any) -> str:
        mode_s = str(mode or "machine").strip().lower()
        return mode_s if mode_s in {"machine", "hybrid", "absolute"} else "machine"

    @staticmethod
    def _validate_device_key(value: Any) -> str:
        key = str(value or "").strip()
        if not key or len(key) > 128:
            raise MerchantError("bad_device_key", "机器ID不合法")
        if any(ch in key for ch in "\r\n\t<>\"'`"):
            raise MerchantError("bad_device_key", "机器ID含非法字符")
        return key

    @staticmethod
    def _validate_device_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name or len(name) > 128:
            raise MerchantError("bad_device_name", "设备名称不合法")
        return name

    def admin_create_device(self, admin: dict[str, Any] | None, *, device_key: str, device_name: str, mode: str = "machine", radar_url: str = "", watchdog_card: str = "", accept_orders: bool = True) -> dict[str, Any]:
        if not hasattr(self.bridge, "create_device"):
            raise MerchantError("bridge_unsupported", "中央 Bridge 当前不支持 API Key 设备新增，请先更新中央 Bridge", 501)
        device_key = self._validate_device_key(device_key)
        device_name = self._validate_device_name(device_name)
        mode = self._normalize_device_mode(mode)
        try:
            dev = self.bridge.create_device(machine_id=device_key, display_name=device_name, mode=mode, radar_url=str(radar_url or "").strip(), watchdog_card=str(watchdog_card or "").strip(), accept_orders=bool(accept_orders), idem=f"device-create:{device_key}:{iso()}")
        except BridgeClientError as e:
            self._raise_bridge_error(e)
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "device_create", "device", dev.get("id") or device_key, {"device_key": device_key, "device_name": device_name, "mode": mode})
        return dict(dev)

    def admin_update_device(self, admin: dict[str, Any] | None, device_id: int, *, device_key: str | None = None, device_name: str | None = None, mode: str | None = None, radar_url: str | None = None, watchdog_card: str | None = None, enabled: bool | None = None, accept_orders: bool | None = None) -> dict[str, Any]:
        if not hasattr(self.bridge, "update_device"):
            raise MerchantError("bridge_unsupported", "中央 Bridge 当前不支持 API Key 设备编辑，请先更新中央 Bridge", 501)
        kwargs: dict[str, Any] = {}
        if device_key is not None:
            kwargs["machine_id"] = self._validate_device_key(device_key)
        if device_name is not None:
            kwargs["display_name"] = self._validate_device_name(device_name)
        if mode is not None:
            kwargs["mode"] = self._normalize_device_mode(mode)
        if radar_url is not None:
            kwargs["radar_url"] = str(radar_url or "").strip()
        if watchdog_card is not None:
            kwargs["watchdog_card"] = str(watchdog_card or "").strip()
        if enabled is not None:
            kwargs["enabled"] = bool(enabled)
        if accept_orders is not None:
            kwargs["accept_orders"] = bool(accept_orders)
        try:
            dev = self.bridge.update_device(int(device_id), **kwargs, idem=f"device-update:{int(device_id)}:{iso()}")
        except BridgeClientError as e:
            self._raise_bridge_error(e)
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "device_update", "device", device_id, kwargs)
        return dict(dev)

    def admin_set_device_mode(self, admin: dict[str, Any] | None, device_id: int, mode: str) -> dict[str, Any]:
        if not hasattr(self.bridge, "set_device_mode"):
            raise MerchantError("bridge_unsupported", "中央 Bridge 当前不支持 API Key 机器模式切换，请先更新中央 Bridge", 501)
        mode = self._normalize_device_mode(mode)
        try:
            dev = self.bridge.set_device_mode(int(device_id), mode, idem=f"device-mode:{int(device_id)}:{mode}:{iso()}")
        except BridgeClientError as e:
            self._raise_bridge_error(e)
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "device_mode_update", "device", device_id, {"mode": mode})
        return dict(dev)

    def admin_set_device_enabled(self, admin: dict[str, Any] | None, device_id: int, enabled: bool) -> dict[str, Any]:
        if not hasattr(self.bridge, "set_device_enabled"):
            raise MerchantError("bridge_unsupported", "中央 Bridge 当前不支持 API Key 设备启停，请先更新中央 Bridge", 501)
        try:
            dev = self.bridge.set_device_enabled(int(device_id), bool(enabled), idem=f"device-toggle:{int(device_id)}:{int(bool(enabled))}:{iso()}")
        except BridgeClientError as e:
            self._raise_bridge_error(e)
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "device_toggle", "device", device_id, {"enabled": bool(enabled)})
        return dict(dev)

    def admin_set_device_accept_orders(self, admin: dict[str, Any] | None, device_id: int, accept_orders: bool) -> dict[str, Any]:
        if not hasattr(self.bridge, "set_device_accept_orders") and not hasattr(self.bridge, "update_device"):
            raise MerchantError("bridge_unsupported", "中央 Bridge 当前不支持 API Key 停止/恢复接单，请先更新中央 Bridge", 501)
        try:
            if hasattr(self.bridge, "set_device_accept_orders"):
                dev = self.bridge.set_device_accept_orders(int(device_id), bool(accept_orders), idem=f"device-accept-orders:{int(device_id)}:{int(bool(accept_orders))}:{iso()}")
            else:
                dev = self.bridge.update_device(int(device_id), accept_orders=bool(accept_orders), idem=f"device-accept-orders:{int(device_id)}:{int(bool(accept_orders))}:{iso()}")
        except BridgeClientError as e:
            self._raise_bridge_error(e)
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "device_accept_orders", "device", device_id, {"accept_orders": bool(accept_orders)})
        return dict(dev)

    def admin_delete_device(self, admin: dict[str, Any] | None, device_id: int) -> dict[str, Any]:
        if not hasattr(self.bridge, "delete_device"):
            raise MerchantError("bridge_unsupported", "中央 Bridge 当前不支持 API Key 设备删除，请先更新中央 Bridge", 501)
        try:
            res = self.bridge.delete_device(int(device_id), idem=f"device-delete:{int(device_id)}:{iso()}")
        except BridgeClientError as e:
            self._raise_bridge_error(e)
        res = {k: v for k, v in dict(res).items() if k not in {"ok", "msg"}}
        with self.db.connect() as con:
            self._log_admin_action_locked(con, admin, "device_delete", "device", device_id, {})
        return dict(res)

    def _resolve_admin_device_id(self, identifier: Any) -> int:
        raw = str(identifier or "").strip()
        if raw.isdigit():
            return int(raw)
        for d in self.admin_list_devices():
            keys = {
                str(d.get("machine_id") or ""),
                str(d.get("device_key") or ""),
                str(d.get("id") or ""),
                str(d.get("device_id") or ""),
                str(d.get("device_name") or ""),
            }
            if raw and raw in keys:
                return int(d.get("id") or d.get("device_id") or 0)
        raise MerchantError("not_found", "设备不存在", 404)

    @staticmethod
    def _manual_loadout_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
        loadout_type = str(payload.get("loadout_type") or "default").strip() or "default"
        items = {
            "helmet": str(payload.get("loadout_helmet") or "").strip(),
            "armor": str(payload.get("loadout_armor") or "").strip(),
            "rig": str(payload.get("loadout_rig") or "").strip(),
            "pistol": str(payload.get("loadout_pistol") or "").strip(),
            "backpack": str(payload.get("loadout_backpack") or "").strip(),
        }
        return {
            "loadout_type": loadout_type,
            "items": {k: v for k, v in items.items() if v},
            "total_cost": max(0, int(payload.get("loadout_total_cost") or 0)),
        }

    def admin_manual_order(
        self,
        admin: dict[str, Any] | None,
        *,
        device_id: int,
        requested_minutes: int,
        team_code: str,
        quality: str = "standard",
        requested_rounds: int = 0,
        max_coin_loss: int = 0,
        loadout: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        device_id = int(device_id or 0)
        requested_minutes = int(requested_minutes or 0)
        requested_rounds = max(0, int(requested_rounds or 0))
        max_coin_loss = max(0, int(max_coin_loss or 0))
        team_code = str(team_code or "").strip().upper()
        quality = str(quality or "standard").strip() or "standard"
        loadout = loadout or {"loadout_type": "default", "items": {}, "total_cost": 0}
        if device_id <= 0:
            raise MerchantError("bad_device", "请选择设备")
        if requested_minutes <= 0 or requested_minutes > (9999 * 60 + 59):
            raise MerchantError("bad_minutes", "手动下单分钟数不合法")
        if not (3 <= len(team_code) <= 32):
            raise MerchantError("bad_team_code", "队伍码长度不合法")
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                if self._active_order_by_device_locked(con, device_id):
                    raise MerchantError("device_has_active_order", "该设备已有商户本地进行中订单", 409)
                customer_id = self._ensure_manual_customer_locked(con, device_id)
                active = self._active_order_row(con, customer_id)
                if active:
                    raise MerchantError("manual_device_has_active_order", "该设备手动订单尚未结束", 409)
                order_no = self._new_order_no()
                options = {"admin_manual": True, "max_rounds": requested_rounds, "max_coin_loss": max_coin_loss, "loadout": loadout}
                cur = con.execute(
                    """INSERT INTO local_orders(customer_id,status,local_order_no,requested_minutes,requested_rounds,team_code,quality,manual_device_id,order_options_json,amount_cents,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (customer_id, "claiming_device", order_no, requested_minutes, requested_rounds, team_code, quality, device_id, dumps(options), 0, now_s, now_s),
                )
                order_id = int(cur.lastrowid)
                self._log_admin_action_locked(con, admin, "manual_order_create", "device", device_id, {"order_id": order_id, "minutes": requested_minutes, "team_code": team_code, "quality": quality})
                con.commit()
            except Exception:
                con.rollback()
                raise
        merchant_context_ref = opaque_merchant_ref(order_no, self.merchant_ref_secret)
        settings = self.get_settings()
        try:
            sess = self.bridge.create_control_session(
                merchant_context_ref=merchant_context_ref,
                idem=f"admin-manual-claim:{order_no}",
                device_id=device_id,
                auto_assign=False,
                selection_policy=self._bridge_selection_policy(settings, quality),
                purpose="admin_manual_order",
                expected_device_state="idle",
                takeover_policy="reject",
            )
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
                idem=f"admin-manual-bundle:{order_no}:v1",
                ace_enabled=bool(settings.get("ace_enabled")),
                max_rounds=requested_rounds,
                max_coin_loss=max_coin_loss,
                loadout=loadout,
            )
            commands = bundle.get("commands") or []
            last_command_id = commands[-1].get("command_id") if commands else None
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE local_orders SET status='waiting_ready_timer',updated_at=? WHERE id=?", (iso(), order_id))
                con.execute("UPDATE order_control_bindings SET last_command_id=?,updated_at=? WHERE local_order_id=?", (last_command_id, iso(), order_id))
                con.commit()
            self._record_order_activity(order_id, event_type="admin_manual_order")
            return self.admin_get_order(order_id)
        except BridgeClientError as e:
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                msg = self._bridge_error_message(e)
                con.execute("UPDATE local_orders SET status='failed',fail_reason=?,finished_at=?,updated_at=? WHERE id=?", (f"bridge:{e.code}:{msg}"[:500], iso(), iso(), order_id))
                self._log_admin_action_locked(con, admin, "manual_order_failed", "device", device_id, {"order_id": order_id, "error": e.code, "message": msg})
                con.commit()
            self._record_order_activity(order_id, event_type="admin_manual_order")
            self._raise_bridge_error(e)

    def admin_rejoin_order(self, admin: dict[str, Any] | None, order_id: int, team_code: str) -> dict[str, Any]:
        team_code = str(team_code or "").strip().upper()
        if team_code and not (3 <= len(team_code) <= 32):
            raise MerchantError("bad_team_code", "队伍码长度不合法")
        with self.db.connect() as con:
            row = con.execute(
                """SELECT o.*, b.control_session_id, b.fencing_token, b.last_device_epoch
                   FROM local_orders o LEFT JOIN order_control_bindings b ON b.local_order_id=o.id
                   WHERE o.id=?""",
                (order_id,),
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
                    params={"team_code": team_code, "clear_existing": True, "operator": "merchant_admin"},
                    expected_device_epoch=int(row["last_device_epoch"] or 0),
                    idem=f"admin-rejoin:{row['local_order_no']}:{team_code}",
                )
            except BridgeClientError as e:
                self._raise_bridge_error(e)
        if team_code:
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE local_orders SET team_code=?,updated_at=? WHERE id=?", (team_code, iso(), order_id))
                self._log_admin_action_locked(con, admin, "manual_rejoin", "order", order_id, {"team_code": team_code})
                con.commit()
        return self.admin_get_order(order_id)

    def admin_device_command(self, admin: dict[str, Any] | None, device_id: int, *, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        device_id = int(device_id or 0)
        action = str(action or "").strip()
        allowed = {"stop_current", "enter_team", "ready", "watch", "set_loadout", "restart_backup", "switch_spectate", "cleanup", "restart", "update", "collect_log"}
        maintenance_without_order = {"restart_backup", "cleanup", "restart", "update", "collect_log"}
        if action not in allowed:
            raise MerchantError("bad_command", "不允许的设备指令")
        with self.db.connect() as con:
            row = self._active_order_by_device_locked(con, device_id)
            if not row and action not in maintenance_without_order:
                raise MerchantError("no_active_control_session", "该设备没有商户本地活动控制会话；空闲设备请使用手动下单", 409)
        try:
            if not row:
                ref = f"admin-maint:{device_id}:{action}:{secrets.token_hex(6)}"
                sess = self.bridge.create_control_session(
                    merchant_context_ref=ref,
                    idem=f"admin-maint-claim:{device_id}:{action}:{secrets.token_hex(4)}",
                    device_id=device_id,
                    auto_assign=False,
                    purpose="admin_device_maintenance",
                    expected_device_state="idle",
                    takeover_policy="reject",
                )
                cmd = self.bridge.queue_command(
                    sess["control_session_id"],
                    fencing_token=sess["fencing_token"],
                    action=action,
                    params=params or {},
                    expected_device_epoch=int(sess.get("device_epoch") or 0),
                    idem=f"admin-maint-cmd:{device_id}:{action}:{secrets.token_hex(4)}",
                )
                with self.db.connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    self._log_admin_action_locked(con, admin, "device_maintenance_command", "device", device_id, {"action": action, "control_session_id": sess["control_session_id"]})
                    con.commit()
                return {"command": cmd, "control_session": sess, "order": None}
            if action == "stop_current":
                cmd = self.bridge.queue_stop(row["control_session_id"], fencing_token=row["fencing_token"], idem=f"admin-device-stop:{row['local_order_no']}:v1", reason="merchant_admin_device_stop")
                with self.db.connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    con.execute("UPDATE local_orders SET status='stopping',fail_reason='admin_device_stop',updated_at=? WHERE id=?", (iso(), int(row["id"])))
                    self._log_admin_action_locked(con, admin, "device_command", "device", device_id, {"action": action, "order_id": int(row["id"])})
                    con.commit()
                return {"command": cmd, "order": self.admin_get_order(int(row["id"]))}
            cmd = self.bridge.queue_command(
                row["control_session_id"],
                fencing_token=row["fencing_token"],
                action=action,
                params=params or {},
                expected_device_epoch=int(row["last_device_epoch"] or 0),
                idem=f"admin-device-cmd:{row['local_order_no']}:{action}:{secrets.token_hex(4)}",
            )
            with self.db.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                self._log_admin_action_locked(con, admin, "device_command", "device", device_id, {"action": action, "order_id": int(row["id"])})
                con.commit()
            return {"command": cmd, "order": self.admin_get_order(int(row["id"]))}
        except BridgeClientError as e:
            self._raise_bridge_error(e)

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
                self._raise_bridge_error(e)

        order_dict = dict(row)
        refund_minutes = self._remaining_minutes(order_dict)
        refund_rounds = int(order_dict.get("requested_rounds") or 0)
        refund_minutes_col, refund_rounds_col = self._mode_balance_columns(self._mode_from_quality(order_dict.get("quality")))
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
                        f"UPDATE customers SET {refund_minutes_col}={refund_minutes_col}+?,{refund_rounds_col}={refund_rounds_col}+?,updated_at=? WHERE id=?",
                        (refund_minutes, refund_rounds, now_s, customer_id),
                    )
                    self._sync_customer_balance_locked(con, customer_id)
                    con.execute(
                        "INSERT OR IGNORE INTO refund_records(local_order_id,customer_id,minutes,rounds,reason,created_at) VALUES(?,?,?,?,?,?)",
                        (order_id, customer_id, refund_minutes, refund_rounds, "customer_stop", now_s),
                    )
                con.execute(
                    "UPDATE local_orders SET status='stopping',fail_reason='customer_stop',updated_at=? WHERE id=?",
                    (now_s, order_id),
                )
                customer_row = con.execute("SELECT username FROM customers WHERE id=?", (customer_id,)).fetchone()
                self._log_customer_action_locked(
                    con,
                    customer_id,
                    customer_row["username"] if customer_row else None,
                    "customer_order_stop",
                    "order",
                    order_id,
                    {"refund_minutes": refund_minutes, "refund_rounds": refund_rounds},
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
                self._raise_bridge_error(e)
        if team_code:
            with self.db.connect() as con:
                con.execute("UPDATE local_orders SET team_code=?,updated_at=? WHERE id=?", (team_code, iso(), order_id))
                customer_row = con.execute("SELECT username FROM customers WHERE id=?", (customer_id,)).fetchone()
                self._log_customer_action_locked(
                    con,
                    customer_id,
                    customer_row["username"] if customer_row else None,
                    "customer_order_rejoin",
                    "order",
                    order_id,
                    {"team_code": team_code},
                )
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

    def configure_initial_owner_admin(self, *, username: str, password: str) -> dict[str, Any]:
        """Create or replace the bootstrap owner during first-run setup.

        create_app still seeds a local default owner so the app remains usable
        in development and tests. In enforced first-run setup, the setup page is
        the user's chance to replace that bootstrap account with their real
        local owner account before the site opens.
        """
        username = str(username or "").strip()
        if not username or len(username) > 64:
            raise MerchantError("bad_username", "管理员用户名不合法")
        if len(str(password or "")) < 4:
            raise MerchantError("bad_password", "管理员密码至少 4 位")
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM merchant_admins WHERE role='owner' ORDER BY id LIMIT 1").fetchone()
                if not row:
                    row = con.execute("SELECT * FROM merchant_admins ORDER BY id LIMIT 1").fetchone()
                existing = con.execute("SELECT * FROM merchant_admins WHERE username=?", (username,)).fetchone()
                if existing and row and int(existing["id"]) != int(row["id"]):
                    raise MerchantError("username_exists", "管理员用户名已存在", 409)
                if row:
                    admin_id = int(row["id"])
                    con.execute(
                        "UPDATE merchant_admins SET username=?,password_hash=?,role='owner',status='active',updated_at=? WHERE id=?",
                        (username, hash_password(password), now_s, admin_id),
                    )
                else:
                    cur = con.execute(
                        "INSERT INTO merchant_admins(username,password_hash,role,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (username, hash_password(password), "owner", "active", now_s, now_s),
                    )
                    admin_id = int(cur.lastrowid)
                fresh = con.execute("SELECT * FROM merchant_admins WHERE id=?", (admin_id,)).fetchone()
                self._log_admin_action_locked(con, self._admin_view(fresh), "initial_owner_configure", "admin", admin_id, {"username": username})
                con.commit()
                return self._admin_view(fresh)
            except MerchantError:
                con.rollback()
                raise
            except Exception:
                con.rollback()
                raise

    def authenticate_admin_optional(self, username: str, password: str) -> dict[str, Any] | None:
        """Authenticate an admin without recording a failed admin audit entry.

        Used by the shared /login customer/admin entry. A normal customer login
        attempt should not generate an admin-login-failed audit row simply
        because the username is not an admin username.
        """
        username = str(username or "").strip()
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM merchant_admins WHERE username=? AND status='active'", (username,)).fetchone()
            if not row or not verify_password(str(password or ""), row["password_hash"]):
                return None
            con.execute("UPDATE merchant_admins SET last_login_at=?,updated_at=? WHERE id=?", (iso(), iso(), int(row["id"])))
            self._log_admin_action_locked(con, dict(row), "admin_login", "auth", int(row["id"]), {"source": "shared_login"})
            return self.public_admin(dict(row))

    def authenticate_admin(self, username: str, password: str) -> dict[str, Any]:
        username = str(username or "").strip()
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM merchant_admins WHERE username=? AND status='active'", (username,)).fetchone()
            if not row or not verify_password(str(password or ""), row["password_hash"]):
                self._log_audit_locked(
                    con,
                    actor_type="admin",
                    actor_id=int(row["id"]) if row else None,
                    actor_username=username,
                    action="admin_login_failed",
                    resource_type="auth",
                    resource_id=username or None,
                    metadata={"reason": "bad_credentials"},
                )
                raise MerchantError("bad_credentials", "管理员用户名或密码错误", 401)
            con.execute("UPDATE merchant_admins SET last_login_at=?,updated_at=? WHERE id=?", (iso(), iso(), int(row["id"])))
            self._log_admin_action_locked(con, dict(row), "admin_login", "auth", int(row["id"]), {})
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
            row = con.execute("SELECT admin_id,username,role FROM admin_sessions WHERE sid=?", (sid,)).fetchone()
            con.execute("DELETE FROM admin_sessions WHERE sid=?", (sid,))
            if row:
                self._log_admin_action_locked(con, {"id": int(row["admin_id"]), "username": row["username"]}, "admin_logout", "auth", int(row["admin_id"]), {"role": row["role"]})

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

    def _admin_view(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        return {
            "id": int(d["id"]),
            "username": d["username"],
            "role": d.get("role") or "operator",
            "status": d.get("status") or "active",
            "last_login_at": d.get("last_login_at"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
        }

    @staticmethod
    def _normalize_admin_role(role: Any) -> str:
        role_s = str(role or "operator").strip().lower()
        if role_s in {"owner", "admin", "superadmin"}:
            return "owner"
        return "operator"

    def _active_owner_count_locked(self, con: sqlite3.Connection, *, exclude_admin_id: int | None = None) -> int:
        if exclude_admin_id is None:
            return int(con.execute("SELECT COUNT(*) AS n FROM merchant_admins WHERE role='owner' AND status='active'").fetchone()["n"] or 0)
        return int(con.execute("SELECT COUNT(*) AS n FROM merchant_admins WHERE role='owner' AND status='active' AND id<>?", (exclude_admin_id,)).fetchone()["n"] or 0)

    def admin_list_admins(self) -> list[dict[str, Any]]:
        with self.db.connect() as con:
            rows = con.execute("SELECT * FROM merchant_admins ORDER BY id").fetchall()
        return [self._admin_view(r) for r in rows]

    def admin_create_admin(self, actor: dict[str, Any] | None, *, username: str, password: str, role: str = "operator", status: str = "active") -> dict[str, Any]:
        username = str(username or "").strip()
        role = self._normalize_admin_role(role)
        status = str(status or "active").strip().lower()
        if not username or len(username) > 64:
            raise MerchantError("bad_username", "管理员用户名不合法")
        if len(str(password or "")) < 6:
            raise MerchantError("bad_password", "管理员密码至少 6 位")
        if status not in {"active", "disabled"}:
            raise MerchantError("bad_status", "管理员状态必须是 active 或 disabled")
        now_s = iso()
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute(
                    "INSERT INTO merchant_admins(username,password_hash,role,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (username, hash_password(password), role, status, now_s, now_s),
                )
                admin_id = int(cur.lastrowid)
                self._log_admin_action_locked(con, actor, "admin_create", "admin", admin_id, {"username": username, "role": role, "status": status})
                con.commit()
            except sqlite3.IntegrityError:
                con.rollback()
                raise MerchantError("username_exists", "管理员用户名已存在", 409)
            except Exception:
                con.rollback()
                raise
        return self._admin_view({"id": admin_id, "username": username, "role": role, "status": status, "created_at": now_s, "updated_at": now_s, "last_login_at": None})

    def admin_set_admin_role(self, actor: dict[str, Any] | None, admin_id: int, role: str) -> dict[str, Any]:
        role = self._normalize_admin_role(role)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM merchant_admins WHERE id=?", (admin_id,)).fetchone()
                if not row:
                    raise MerchantError("not_found", "管理员不存在", 404)
                if row["role"] == "owner" and role != "owner" and row["status"] == "active" and self._active_owner_count_locked(con, exclude_admin_id=admin_id) <= 0:
                    raise MerchantError("last_owner", "不能降级最后一个 active owner", 409)
                con.execute("UPDATE merchant_admins SET role=?,updated_at=? WHERE id=?", (role, iso(), admin_id))
                self._log_admin_action_locked(con, actor, "admin_role_update", "admin", admin_id, {"role": role})
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.admin_get_admin(admin_id)

    def admin_set_admin_status(self, actor: dict[str, Any] | None, admin_id: int, status: str) -> dict[str, Any]:
        status = str(status or "active").strip().lower()
        if status not in {"active", "disabled"}:
            raise MerchantError("bad_status", "管理员状态必须是 active 或 disabled")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM merchant_admins WHERE id=?", (admin_id,)).fetchone()
                if not row:
                    raise MerchantError("not_found", "管理员不存在", 404)
                if row["role"] == "owner" and status != "active" and row["status"] == "active" and self._active_owner_count_locked(con, exclude_admin_id=admin_id) <= 0:
                    raise MerchantError("last_owner", "不能禁用最后一个 active owner", 409)
                con.execute("UPDATE merchant_admins SET status=?,updated_at=? WHERE id=?", (status, iso(), admin_id))
                if status != "active":
                    con.execute("DELETE FROM admin_sessions WHERE admin_id=?", (admin_id,))
                self._log_admin_action_locked(con, actor, "admin_status_update", "admin", admin_id, {"status": status})
                con.commit()
            except Exception:
                con.rollback()
                raise
        return self.admin_get_admin(admin_id)

    def admin_reset_admin_password(self, actor: dict[str, Any] | None, admin_id: int, password: str) -> dict[str, Any]:
        if len(str(password or "")) < 6:
            raise MerchantError("bad_password", "管理员密码至少 6 位")
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute("UPDATE merchant_admins SET password_hash=?,updated_at=? WHERE id=?", (hash_password(password), iso(), admin_id))
                if cur.rowcount == 0:
                    raise MerchantError("not_found", "管理员不存在", 404)
                con.execute("DELETE FROM admin_sessions WHERE admin_id=?", (admin_id,))
                self._log_admin_action_locked(con, actor, "admin_password_reset", "admin", admin_id, {})
                con.commit()
            except Exception:
                con.rollback()
                raise
        return {"id": admin_id, "updated": True}

    def admin_delete_admin(self, actor: dict[str, Any] | None, admin_id: int) -> dict[str, Any]:
        if actor and int(actor.get("id") or 0) == int(admin_id):
            raise MerchantError("cannot_delete_self", "不能删除当前登录管理员", 409)
        with self.db.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM merchant_admins WHERE id=?", (admin_id,)).fetchone()
                if not row:
                    raise MerchantError("not_found", "管理员不存在", 404)
                if row["role"] == "owner" and row["status"] == "active" and self._active_owner_count_locked(con, exclude_admin_id=admin_id) <= 0:
                    raise MerchantError("last_owner", "不能删除最后一个 active owner", 409)
                con.execute("DELETE FROM admin_sessions WHERE admin_id=?", (admin_id,))
                con.execute("DELETE FROM merchant_admins WHERE id=?", (admin_id,))
                self._log_admin_action_locked(con, actor, "admin_delete", "admin", admin_id, {"username": row["username"], "role": row["role"]})
                con.commit()
            except Exception:
                con.rollback()
                raise
        return {"id": admin_id, "deleted": True}

    def admin_get_admin(self, admin_id: int) -> dict[str, Any]:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM merchant_admins WHERE id=?", (admin_id,)).fetchone()
            if not row:
                raise MerchantError("not_found", "管理员不存在", 404)
            return self._admin_view(row)

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
        out["announcement_text"] = sanitize_notice_html(out.get("announcement_text"))
        try:
            out["global_radar_url"] = normalize_public_http_url(out.get("global_radar_url")) if out.get("global_radar_url") else ""
        except MerchantError:
            out["global_radar_url"] = ""
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
                sanitized[key] = sanitize_notice_html(text)
            elif key == "global_radar_url":
                sanitized[key] = normalize_public_http_url(values.get(key)) if values.get(key) else ""
            elif key in {"maintenance_message", "night_start_time", "night_end_time"}:
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
                mode = card["mode"] or "machine"
                minutes_col, rounds_col = self._mode_balance_columns(mode)
                con.execute("UPDATE recharge_cards SET status='used',used_by_customer_id=?,used_at=? WHERE code_hash=?", (customer_id, now_s, code_hash))
                con.execute(
                    f"UPDATE customers SET {minutes_col}={minutes_col}+?,{rounds_col}={rounds_col}+?,updated_at=? WHERE id=?",
                    (minutes, rounds, now_s, customer_id),
                )
                self._sync_customer_balance_locked(con, customer_id)
                con.execute(
                    "INSERT INTO recharge_records(customer_id,code_hash,minutes,rounds,created_at) VALUES(?,?,?,?,?)",
                    (customer_id, code_hash, minutes, rounds, now_s),
                )
                self._log_customer_action_locked(
                    con,
                    customer_id,
                    customer["username"],
                    "customer_redeem_card",
                    "recharge_card",
                    card["code_plain"] or code_hash[:12],
                    {
                        "minutes": minutes,
                        "rounds": rounds,
                        "mode": mode,
                        "card_type": card["card_type"] or "normal",
                        "code_tail": str(code or "")[-4:],
                    },
                )
                con.commit()
                return {"minutes": minutes, "rounds": rounds, "mode": mode, "card_type": card["card_type"] or "normal", "customer": self.get_customer(customer_id)}
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
                mode = card["mode"] or "machine"
                machine_minutes = minutes if mode != "absolute" else 0
                machine_rounds = rounds if mode != "absolute" else 0
                absolute_minutes = minutes if mode == "absolute" else 0
                absolute_rounds = rounds if mode == "absolute" else 0
                cur = con.execute(
                    """INSERT INTO customers(
                         username,password_hash,
                         balance_minutes,balance_rounds,
                         balance_machine_minutes,balance_machine_rounds,
                         balance_absolute_minutes,balance_absolute_rounds,
                         status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        username,
                        hash_password(secrets.token_urlsafe(18)),
                        minutes,
                        rounds,
                        machine_minutes,
                        machine_rounds,
                        absolute_minutes,
                        absolute_rounds,
                        "active",
                        now_s,
                        now_s,
                    ),
                )
                customer_id = int(cur.lastrowid)
                con.execute("UPDATE recharge_cards SET status='used',used_by_customer_id=?,used_at=? WHERE code_hash=?", (customer_id, now_s, code_hash))
                con.execute(
                    "INSERT INTO recharge_records(customer_id,code_hash,minutes,rounds,created_at) VALUES(?,?,?,?,?)",
                    (customer_id, code_hash, minutes, rounds, now_s),
                )
                self._log_customer_action_locked(
                    con,
                    customer_id,
                    username,
                    "customer_night_card_redeem",
                    "recharge_card",
                    card["code_plain"] or code_hash[:12],
                    {
                        "minutes": minutes,
                        "rounds": rounds,
                        "mode": mode,
                        "card_type": card["card_type"] or "night",
                        "code_tail": code[-4:],
                    },
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
        balance_mode = self._mode_from_quality(quality)
        minutes_col, rounds_col = self._mode_balance_columns(balance_mode)
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
                    self._log_customer_action_locked(
                        con,
                        customer_id,
                        customer["username"],
                        "customer_order_reuse",
                        "order",
                        int(active["id"]),
                        {"status": active["status"], "team_code": active["team_code"]},
                    )
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
                if int(customer[minutes_col] or 0) < requested_minutes:
                    raise MerchantError("insufficient_balance", "分钟余额不足", 402)
                if requested_rounds and int(customer[rounds_col] or 0) < requested_rounds:
                    raise MerchantError("insufficient_rounds", "战损余额不足", 402)
                order_no = self._new_order_no()
                con.execute(
                    f"UPDATE customers SET {minutes_col}={minutes_col}-?,{rounds_col}={rounds_col}-?,updated_at=? WHERE id=?",
                    (requested_minutes, requested_rounds, now_s, customer_id),
                )
                self._sync_customer_balance_locked(con, customer_id)
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
                self._record_order_activity(order_id)
                return result
        except BridgeClientError as e:
            self._fail_and_refund_new_order(order_id, f"bridge:{e.code}:{self._bridge_error_message(e)}")
            with self.db.connect() as con:
                result = {"order": self._order_with_binding(con, order_id), "reused": False}
                if idempotency_key:
                    con.execute(
                        "INSERT OR REPLACE INTO idempotency_keys(scope,idempotency_key,request_hash,response_json,created_at) VALUES(?,?,?,?,?)",
                        (scope, idempotency_key, payload_hash, dumps(result), iso()),
                    )
            self._record_order_activity(order_id)
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
                    minutes_col, rounds_col = self._mode_balance_columns(self._mode_from_quality(order["quality"]))
                    con.execute(
                        f"UPDATE customers SET {minutes_col}={minutes_col}+?,{rounds_col}={rounds_col}+?,updated_at=? WHERE id=?",
                        (minutes, rounds, iso(), int(order["customer_id"])),
                    )
                    self._sync_customer_balance_locked(con, int(order["customer_id"]))
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
        control_session_id = ev.get("control_session_id") or ev.get("session_id")
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

    def _start_ready_timer_locked(self, con: sqlite3.Connection, order: dict[str, Any], binding: dict[str, Any]) -> None:
        order_id = int(order["id"])
        now_s = iso()
        if order["status"] in {"waiting_ready_timer", "commanding", "device_claimed", "claiming_device"} and not order.get("started_at"):
            start = utcnow()
            end = start + timedelta(minutes=int(order["requested_minutes"] or 0))
            con.execute(
                "UPDATE local_orders SET status='running',started_at=?,end_at=?,updated_at=? WHERE id=?",
                (iso(start), iso(end), now_s, order_id),
            )
            con.execute("UPDATE order_control_bindings SET ready_timer_received=1,updated_at=? WHERE id=?", (now_s, int(binding["id"])))

    def _command_action_for_event_locked(self, con: sqlite3.Connection, command_id: str | None, payload: dict[str, Any]) -> str:
        for src in (payload, payload.get("result") if isinstance(payload.get("result"), dict) else None):
            if isinstance(src, dict):
                action = src.get("action") or src.get("command_action")
                if action:
                    return str(action)
        if command_id:
            row = con.execute(
                """SELECT payload_json FROM bridge_events
                   WHERE command_id=? AND event='command.queued'
                   ORDER BY event_seq DESC, received_at DESC LIMIT 1""",
                (command_id,),
            ).fetchone()
            if row:
                queued = loads(row["payload_json"], {}) or {}
                action = queued.get("action")
                if action:
                    return str(action)
        return ""

    def _apply_event_locked(self, con: sqlite3.Connection, order: dict[str, Any], binding: dict[str, Any], event_name: str, payload: dict[str, Any], command_id: str | None, device_epoch: int | None) -> None:
        order_id = int(order["id"])
        now_s = iso()
        if event_name == "device.ready_for_customer_timer":
            self._start_ready_timer_locked(con, order, binding)
            return

        action = self._command_action_for_event_locked(con, command_id, payload)

        if event_name == "agent_job.done":
            # SNOWSERVER slim external API emits agent_job.done instead of the
            # older command.succeeded / device.ready_for_customer_timer events.
            # The last command in our start bundle is watch; once it is done the
            # customer timer can safely start.
            if action == "stop_current":
                if order["status"] in ACTIVE_ORDER_STATUSES | {"interrupted_by_disconnect", "interrupted_by_admin"}:
                    con.execute("UPDATE local_orders SET status='finished',finished_at=?,updated_at=? WHERE id=?", (now_s, now_s, order_id))
                    con.execute("UPDATE order_control_bindings SET status='released',last_command_id=COALESCE(?,last_command_id),updated_at=? WHERE id=?", (command_id, now_s, int(binding["id"])))
                return
            if action == "watch" or (command_id and command_id == binding.get("last_command_id")):
                self._start_ready_timer_locked(con, order, binding)
            return

        if event_name == "agent_job.failed":
            if action != "stop_current" and order["status"] != "running":
                self._interrupt_or_refund_locked(con, order, "failed", "agent_job_failed")
                con.execute("UPDATE order_control_bindings SET status='failed',last_command_id=COALESCE(?,last_command_id),updated_at=? WHERE id=?", (command_id, now_s, int(binding["id"])))
            return

        if event_name == "agent_job.requeued":
            return

        if event_name == "command.succeeded" and (payload.get("action") == "stop_current" or action == "stop_current"):
            if order["status"] in ACTIVE_ORDER_STATUSES | {"interrupted_by_disconnect", "interrupted_by_admin"}:
                con.execute("UPDATE local_orders SET status='finished',finished_at=?,updated_at=? WHERE id=?", (now_s, now_s, order_id))
                con.execute("UPDATE order_control_bindings SET status='released',last_command_id=COALESCE(?,last_command_id),updated_at=? WHERE id=?", (command_id, now_s, int(binding["id"])))
            return

        if event_name == "command.failed" and (payload.get("action") != "stop_current" and action != "stop_current"):
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

        if event_name == "control_session.interrupted":
            reason = str(payload.get("reason") or "admin_device_maintenance")
            self._interrupt_or_refund_locked(con, order, "interrupted_by_admin", reason)
            con.execute("UPDATE order_control_bindings SET status='interrupted',updated_at=? WHERE id=?", (now_s, int(binding["id"])))
            return

        if event_name == "admin.device_maintenance":
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
            minutes_col, rounds_col = self._mode_balance_columns(self._mode_from_quality(order.get("quality")))
            con.execute(
                f"UPDATE customers SET {minutes_col}={minutes_col}+?,{rounds_col}={rounds_col}+?,updated_at=? WHERE id=?",
                (minutes, rounds, now_s, customer_id),
            )
            self._sync_customer_balance_locked(con, customer_id)
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
            if status in {"expired", "force_taken_over", "revoked", "released", "interrupted"}:
                ev_name = (
                    "control_session.expired"
                    if status == "expired"
                    else ("control_session.interrupted" if status == "interrupted" else ("control_session.revoked" if status in {"force_taken_over", "revoked"} else "control_session.released"))
                )
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
