from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads(value: str | bytes | None, default: Any = None) -> Any:
    if value in (None, b"", ""):
        return default
    try:
        return json.loads(value)  # type: ignore[arg-type]
    except Exception:
        return default


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA temp_store=MEMORY")
        if self.path != Path(":memory:"):
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        return con

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(SCHEMA_SQL)
            self._migrate(con)
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate(self, con: sqlite3.Connection) -> None:
        card_cols = {r["name"] for r in con.execute("PRAGMA table_info(recharge_cards)").fetchall()}
        card_migrations = {
            "code_plain": "ALTER TABLE recharge_cards ADD COLUMN code_plain TEXT",
            "card_type": "ALTER TABLE recharge_cards ADD COLUMN card_type TEXT NOT NULL DEFAULT 'normal'",
            "mode": "ALTER TABLE recharge_cards ADD COLUMN mode TEXT NOT NULL DEFAULT 'machine'",
            "night_coin_loss": "ALTER TABLE recharge_cards ADD COLUMN night_coin_loss INTEGER NOT NULL DEFAULT 0",
            "note": "ALTER TABLE recharge_cards ADD COLUMN note TEXT",
        }
        for col, sql in card_migrations.items():
            if col not in card_cols:
                con.execute(sql)
        customer_cols = {r["name"] for r in con.execute("PRAGMA table_info(customers)").fetchall()}
        customer_migrations = {
            "balance_machine_minutes": "ALTER TABLE customers ADD COLUMN balance_machine_minutes INTEGER NOT NULL DEFAULT 0",
            "balance_machine_rounds": "ALTER TABLE customers ADD COLUMN balance_machine_rounds INTEGER NOT NULL DEFAULT 0",
            "balance_absolute_minutes": "ALTER TABLE customers ADD COLUMN balance_absolute_minutes INTEGER NOT NULL DEFAULT 0",
            "balance_absolute_rounds": "ALTER TABLE customers ADD COLUMN balance_absolute_rounds INTEGER NOT NULL DEFAULT 0",
        }
        added_customer_balance_cols = False
        for col, sql in customer_migrations.items():
            if col not in customer_cols:
                con.execute(sql)
                added_customer_balance_cols = True
        if added_customer_balance_cols:
            # Existing installations only had one balance bucket. Preserve it
            # as machine balance instead of incorrectly showing it in both
            # machine and absolute buckets.
            con.execute(
                """UPDATE customers
                   SET balance_machine_minutes=balance_minutes,
                       balance_machine_rounds=balance_rounds,
                       balance_absolute_minutes=0,
                       balance_absolute_rounds=0
                   WHERE balance_machine_minutes=0
                     AND balance_absolute_minutes=0
                     AND (balance_minutes<>0 OR balance_rounds<>0)"""
            )
        session_cols = {r["name"] for r in con.execute("PRAGMA table_info(sessions)").fetchall()}
        if "last_seen_at" not in session_cols:
            con.execute("ALTER TABLE sessions ADD COLUMN last_seen_at TEXT")
            con.execute("UPDATE sessions SET last_seen_at=created_at WHERE last_seen_at IS NULL")
        order_cols = {r["name"] for r in con.execute("PRAGMA table_info(local_orders)").fetchall()}
        if "manual_device_id" not in order_cols:
            con.execute("ALTER TABLE local_orders ADD COLUMN manual_device_id INTEGER")
        if "order_options_json" not in order_cols:
            con.execute("ALTER TABLE local_orders ADD COLUMN order_options_json TEXT NOT NULL DEFAULT '{}'")
        con.execute("CREATE INDEX IF NOT EXISTS idx_local_orders_manual_device ON local_orders(manual_device_id, status)")
        con.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_manual_order_per_device
               ON local_orders(manual_device_id)
               WHERE manual_device_id IS NOT NULL
                 AND status IN ('created','paid','claiming_device','device_claimed','commanding','waiting_ready_timer','running','stopping','refunding')"""
        )
        # Index/table creation is idempotent in SCHEMA_SQL; keep migrations
        # column-only so older SQLite files can be opened safely.
        audit_cols = {r["name"] for r in con.execute("PRAGMA table_info(admin_audit_logs)").fetchall()}
        audit_migrations = {
            "actor_type": "ALTER TABLE admin_audit_logs ADD COLUMN actor_type TEXT NOT NULL DEFAULT 'admin'",
            "actor_id": "ALTER TABLE admin_audit_logs ADD COLUMN actor_id INTEGER",
            "actor_username": "ALTER TABLE admin_audit_logs ADD COLUMN actor_username TEXT",
        }
        added_audit_cols = False
        for col, sql in audit_migrations.items():
            if col not in audit_cols:
                con.execute(sql)
                added_audit_cols = True
        if added_audit_cols:
            con.execute(
                """UPDATE admin_audit_logs
                   SET actor_type=COALESCE(actor_type,'admin'),
                       actor_id=COALESCE(actor_id,admin_id),
                       actor_username=COALESCE(actor_username,admin_username)"""
            )
        con.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_actor_created ON admin_audit_logs(actor_type,actor_id,created_at)")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  balance_minutes INTEGER NOT NULL DEFAULT 0,
  balance_rounds INTEGER NOT NULL DEFAULT 0,
  balance_machine_minutes INTEGER NOT NULL DEFAULT 0,
  balance_machine_rounds INTEGER NOT NULL DEFAULT 0,
  balance_absolute_minutes INTEGER NOT NULL DEFAULT 0,
  balance_absolute_rounds INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  sid TEXT PRIMARY KEY,
  customer_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT,
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions(customer_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS merchant_admins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'owner',
  status TEXT NOT NULL DEFAULT 'active',
  last_login_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
  sid TEXT PRIMARY KEY,
  admin_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  role TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(admin_id) REFERENCES merchant_admins(id)
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON admin_sessions(admin_id);

CREATE TABLE IF NOT EXISTS merchant_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by_admin_id INTEGER,
  FOREIGN KEY(updated_by_admin_id) REFERENCES merchant_admins(id)
);

CREATE TABLE IF NOT EXISTS local_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL,
  package_id INTEGER,
  status TEXT NOT NULL,
  local_order_no TEXT NOT NULL UNIQUE,
  requested_minutes INTEGER NOT NULL DEFAULT 0,
  requested_rounds INTEGER NOT NULL DEFAULT 0,
  team_code TEXT,
  quality TEXT,
  manual_device_id INTEGER,
  order_options_json TEXT NOT NULL DEFAULT '{}',
  amount_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  end_at TEXT,
  finished_at TEXT,
  fail_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_local_orders_customer_id ON local_orders(customer_id, id);
CREATE INDEX IF NOT EXISTS idx_local_orders_status_end ON local_orders(status, end_at);
CREATE INDEX IF NOT EXISTS idx_local_orders_created_at ON local_orders(created_at);
CREATE INDEX IF NOT EXISTS idx_local_orders_customer_created ON local_orders(customer_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_order_per_customer
  ON local_orders(customer_id)
  WHERE status IN ('created','paid','claiming_device','device_claimed','commanding','waiting_ready_timer','running','stopping','refunding');

CREATE TABLE IF NOT EXISTS order_control_bindings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  local_order_id INTEGER NOT NULL UNIQUE,
  control_session_id TEXT NOT NULL UNIQUE,
  fencing_token TEXT NOT NULL,
  device_id INTEGER NOT NULL,
  merchant_context_ref TEXT NOT NULL UNIQUE,
  last_device_epoch INTEGER NOT NULL DEFAULT 0,
  last_command_id TEXT,
  ready_timer_received INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(local_order_id) REFERENCES local_orders(id)
);
CREATE INDEX IF NOT EXISTS idx_bindings_session ON order_control_bindings(control_session_id);
CREATE INDEX IF NOT EXISTS idx_bindings_device_status ON order_control_bindings(device_id,status);

CREATE TABLE IF NOT EXISTS bridge_events (
  event_id TEXT PRIMARY KEY,
  event_seq INTEGER NOT NULL,
  control_session_id TEXT,
  command_id TEXT,
  device_id INTEGER,
  event TEXT NOT NULL,
  device_epoch INTEGER,
  payload_json TEXT NOT NULL,
  processed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bridge_events_seq ON bridge_events(event_seq);
CREATE INDEX IF NOT EXISTS idx_bridge_events_session ON bridge_events(control_session_id, event_seq);

CREATE TABLE IF NOT EXISTS recharge_cards (
  code_hash TEXT PRIMARY KEY,
  code_plain TEXT,
  minutes INTEGER NOT NULL DEFAULT 0,
  rounds INTEGER NOT NULL DEFAULT 0,
  card_type TEXT NOT NULL DEFAULT 'normal',
  mode TEXT NOT NULL DEFAULT 'machine',
  night_coin_loss INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  status TEXT NOT NULL DEFAULT 'unused',
  used_by_customer_id INTEGER,
  used_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(used_by_customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS recharge_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL,
  code_hash TEXT NOT NULL,
  minutes INTEGER NOT NULL DEFAULT 0,
  rounds INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(customer_id, code_hash),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS refund_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  local_order_id INTEGER NOT NULL,
  customer_id INTEGER NOT NULL,
  minutes INTEGER NOT NULL DEFAULT 0,
  rounds INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(local_order_id, reason),
  FOREIGN KEY(local_order_id) REFERENCES local_orders(id),
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS customer_activity_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  customer_id INTEGER NOT NULL,
  username TEXT NOT NULL,
  order_id INTEGER,
  order_status TEXT,
  order_quality TEXT,
  order_minutes INTEGER NOT NULL DEFAULT 0,
  order_rounds INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  local_date TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(customer_id) REFERENCES customers(id),
  FOREIGN KEY(order_id) REFERENCES local_orders(id)
);
CREATE INDEX IF NOT EXISTS idx_customer_activity_date_type ON customer_activity_events(local_date,event_type);
CREATE INDEX IF NOT EXISTS idx_customer_activity_customer_date ON customer_activity_events(customer_id,local_date);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  admin_id INTEGER,
  admin_username TEXT,
  actor_type TEXT NOT NULL DEFAULT 'admin',
  actor_id INTEGER,
  actor_username TEXT,
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(admin_id) REFERENCES merchant_admins(id)
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_action_created ON admin_audit_logs(action,created_at);
CREATE INDEX IF NOT EXISTS idx_admin_audit_admin_created ON admin_audit_logs(admin_id,created_at);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  scope TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS app_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""
