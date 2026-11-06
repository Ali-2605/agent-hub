"""SQLite storage. One small file, plain tables, easy to inspect:

    sqlite3 ~/.config/agent-hub/hub.db '.tables'
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, task TEXT, project TEXT, kind TEXT, command TEXT, cwd TEXT,
    status TEXT, pid INTEGER, exit_code INTEGER, started REAL, ended REAL,
    progress REAL, progress_text TEXT, log_path TEXT, notify TEXT, meta TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, kind TEXT, source TEXT, session_id TEXT, job_id TEXT,
    title TEXT, body TEXT, options TEXT, allow_text INTEGER, status TEXT, answer TEXT,
    created REAL, answered_at REAL, tg_message_id INTEGER, meta TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, agent TEXT, cwd TEXT, title TEXT,
    agent_mode TEXT, override_mode TEXT, routing TEXT, pending_set_mode TEXT,
    status TEXT, last_event TEXT, last_message TEXT, last_seen REAL, created REAL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, ref TEXT, text TEXT
);
"""

JSON_COLS = {"command", "options", "meta"}


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:6]


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ---- generic helpers -------------------------------------------------
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for col in JSON_COLS & d.keys():
            if d[col]:
                d[col] = json.loads(d[col])
        return d

    def insert(self, table: str, data: dict) -> dict:
        data = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in data.items()}
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(data.values()))
        return self.get(table, data["id"])

    def update(self, table: str, id_: str, **fields: Any) -> dict | None:
        if fields:
            fields = {k: (json.dumps(v) if k in JSON_COLS and v is not None else v) for k, v in fields.items()}
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*fields.values(), id_])
        return self.get(table, id_)

    def get(self, table: str, id_: str) -> dict | None:
        return self._row(self.conn.execute(f"SELECT * FROM {table} WHERE id = ?", [id_]).fetchone())

    def find(self, table: str, id_prefix: str) -> dict | None:
        """Get by full id or unique prefix (so you can type /log a3f on the phone)."""
        exact = self.get(table, id_prefix)
        if exact:
            return exact
        rows = self.conn.execute(f"SELECT * FROM {table} WHERE id LIKE ? LIMIT 2", [id_prefix + "%"]).fetchall()
        return self._row(rows[0]) if len(rows) == 1 else None

    def query(self, sql: str, params: list | tuple = ()) -> list[dict]:
        return [self._row(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---- settings ----------------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", [key]).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [key, json.dumps(value)],
        )

    # ---- event log (searchable history) ------------------------------------
    def log_event(self, kind: str, text: str, ref: str = "") -> None:
        self.conn.execute("INSERT INTO events (ts, kind, ref, text) VALUES (?, ?, ?, ?)",
                          [time.time(), kind, ref, text[:4000]])
