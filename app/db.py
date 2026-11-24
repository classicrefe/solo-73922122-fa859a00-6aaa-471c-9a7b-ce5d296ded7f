"""SQLite connection management and schema initialisation.

The published serving index is versioned: every successful catalog change
creates a new ``index_versions`` row + its ``index_entries`` and flips the
``active_index_version`` meta pointer inside the *same* transaction as the
plate/refs edits.  If that transaction rolls back, the pointer still names the
previous complete index and the website / CSV download keep serving it.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from flask import current_app, g


def now_str() -> str:
    """Local-time timestamp used for audit columns."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plates (
    number     TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refs (
    plate_number TEXT NOT NULL REFERENCES plates(number) ON DELETE CASCADE,
    ref_number   TEXT NOT NULL,
    position     INTEGER NOT NULL,
    PRIMARY KEY (plate_number, position)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_refs_ref_number ON refs(ref_number);

CREATE TABLE IF NOT EXISTS plate_status (
    plate_number TEXT PRIMARY KEY REFERENCES plates(number) ON DELETE CASCADE,
    state        TEXT NOT NULL CHECK (state IN ('serving', 'suspended')),
    blocker      TEXT,
    witness      TEXT NOT NULL DEFAULT '[]',
    reason       TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_versions (
    version       INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT '',
    changed_json  TEXT NOT NULL DEFAULT '[]',
    scope_json    TEXT NOT NULL DEFAULT '[]',
    recomputed    INTEGER NOT NULL DEFAULT 0,
    serving_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS index_entries (
    version      INTEGER NOT NULL REFERENCES index_versions(version),
    plate_number TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    refs_text    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (version, plate_number)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_index_entries_lookup
    ON index_entries(version, plate_number);

CREATE TABLE IF NOT EXISTS change_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    action       TEXT NOT NULL,
    changed_json TEXT NOT NULL DEFAULT '[]',
    scope_json   TEXT NOT NULL DEFAULT '[]',
    detail       TEXT NOT NULL DEFAULT '',
    ok           INTEGER NOT NULL DEFAULT 1
);
"""


def connect(database: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(database))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    # Explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK in catalog operations.
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(database: str) -> None:
    """Create the schema and an initial empty active index version."""
    conn = connect(database)
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'active_index_version'"
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO index_versions (created_at, source) VALUES (?, ?)",
                (now_str(), "初始空索引"),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('active_index_version', ?)",
                (str(cur.lastrowid),),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def active_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'active_index_version'"
    ).fetchone()
    if row is None:  # pragma: no cover - init_db always creates the pointer
        raise RuntimeError("published index is not initialised")
    return int(row["value"])
