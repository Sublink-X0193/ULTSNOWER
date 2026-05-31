from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


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
        cols = {r["name"] for r in con.execute("PRAGMA table_info(recharge_cards)").fetchall()}
        migrations = {
            "code_plain": "ALTER TABLE recharge_cards ADD COLUMN code_plain TEXT",
            "card_type": "ALTER TABLE recharge_cards ADD COLUMN card_type TEXT NOT NULL DEFAULT 'normal'",
            "mode": "ALTER TABLE recharge_cards ADD COLUMN mode TEXT NOT NULL DEFAULT 'machine'",
            "night_coin_loss": "ALTER TABLE recharge_cards ADD COLUMN night_coin_loss INTEGER NOT NULL DEFAULT 0",
            "note": "ALTER TABLE recharge_cards ADD COLUMN note TEXT",
        }
        for col, sql in migrations.items():
            if col not in cols:
                con.execute(sql)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  balance_minutes INTEGER NOT NULL DEFAULT 0,
  balance_rounds INTEGER NOT NULL DEFAULT 0,
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
  FOREIGN KEY(customer_id) REFERENCES customers(id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions(customer_id);

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
