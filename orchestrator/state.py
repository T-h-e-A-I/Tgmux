"""SQLite state store + audit log (plan §10, §9.8)."""

import json
import sqlite3
import time
from typing import Any, Optional

from . import config

_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    slug            TEXT PRIMARY KEY,
    tmux_session    TEXT NOT NULL,
    local_path      TEXT NOT NULL,
    github_repo     TEXT,
    vercel_project  TEXT,
    dev_port        INTEGER,
    status          TEXT NOT NULL DEFAULT 'IDLE',
    paused          INTEGER NOT NULL DEFAULT 0,
    auth_mode       TEXT NOT NULL DEFAULT 'subscription',
    created_at      REAL,
    last_activity   REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.STATE_DB, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


# ---- agents ----

def add_agent(slug: str, tmux_session: str, local_path: str, dev_port: int,
              auth_mode: str = "subscription") -> None:
    now = time.time()
    db().execute(
        "INSERT OR REPLACE INTO agents "
        "(slug, tmux_session, local_path, dev_port, status, paused, auth_mode, created_at, last_activity) "
        "VALUES (?, ?, ?, ?, 'BUILDING', 0, ?, ?, ?)",
        (slug, tmux_session, local_path, dev_port, auth_mode, now, now),
    )
    db().commit()


def get_agent(slug: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM agents WHERE slug = ?", (slug,)).fetchone()


def all_agents() -> list[sqlite3.Row]:
    return db().execute("SELECT * FROM agents ORDER BY created_at").fetchall()


def set_field(slug: str, field: str, value: Any) -> None:
    assert field in {"github_repo", "vercel_project", "dev_port", "status",
                     "paused", "auth_mode", "last_activity"}
    db().execute(f"UPDATE agents SET {field} = ? WHERE slug = ?", (value, slug))
    db().commit()


def set_status(slug: str, status: str) -> None:
    db().execute("UPDATE agents SET status = ?, last_activity = ? WHERE slug = ?",
                 (status, time.time(), slug))
    db().commit()


def delete_agent(slug: str) -> None:
    db().execute("DELETE FROM agents WHERE slug = ?", (slug,))
    if get_active() == slug:
        set_active(None)
    db().commit()


def used_ports() -> set[int]:
    rows = db().execute("SELECT dev_port FROM agents WHERE dev_port IS NOT NULL").fetchall()
    return {r["dev_port"] for r in rows}


# ---- meta ----

def get_active() -> Optional[str]:
    row = db().execute("SELECT value FROM meta WHERE key = 'active_agent'").fetchone()
    return row["value"] if row and row["value"] else None


def set_active(slug: Optional[str]) -> None:
    db().execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('active_agent', ?)", (slug,))
    db().commit()


# ---- audit log (JSONL) ----

def audit(action: str, detail: str = "", slug: str = "") -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "action": action,
             "slug": slug, "detail": detail}
    with open(config.LOG_DIR / "audit.log", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
