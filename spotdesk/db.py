"""SQLite helper — one database, WAL mode, dict rows, audit in one call.

Deliberately sqlite3-stdlib (no ORM): the desk has one writer process and < 5M rows;
the schema is Postgres-portable if multi-writer concurrency ever bites.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

log = logging.getLogger("spotdesk.db")

# The agent runs several monitor threads; SQLite is single-writer, so serialize writes
# in-process rather than relying on busy-timeout retries alone.
_WRITE_LOCK = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_schema() -> None:
    with _WRITE_LOCK:
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()
    log.info("schema ready at %s", config.DB_PATH)


@contextmanager
def connect():
    """Context-managed connection with dict-like rows. Commits on clean exit."""
    with _WRITE_LOCK:
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    """Run one statement; returns lastrowid (inserts) or rowcount context via cursor."""
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or cur.rowcount


def insert(table: str, values: dict) -> int:
    cols = ", ".join(values.keys())
    ph = ", ".join("?" for _ in values)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})",
                           tuple(values.values()))
        return cur.lastrowid


def update(table: str, values: dict, where: str, params: tuple = ()) -> int:
    sets = ", ".join(f"{k} = ?" for k in values)
    with connect() as conn:
        cur = conn.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                           tuple(values.values()) + params)
        return cur.rowcount


def audit(action: str, details: dict | None = None, actor: str = "agent") -> None:
    """Every decision logged. Non-optional: this is the debugging + compliance trail."""
    try:
        insert("audit_log", {"actor": actor, "action": action,
                             "details_json": json.dumps(details or {}, default=str)[:20000]})
    except Exception as e:  # audit must never take the pipeline down
        log.warning("audit write failed (%s): %s", action, e)


def j(value) -> str | None:
    """JSON-encode for a TEXT column (None stays None)."""
    return None if value is None else json.dumps(value, default=str)


def uj(raw, default=None):
    """JSON-decode a TEXT column, tolerating NULL/garbage."""
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
