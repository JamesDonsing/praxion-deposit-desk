"""SQLite storage. One file, its own schema, its own fabricated seed data."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'quote_signed'
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id TEXT NOT NULL REFERENCES quotes(id),
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,          -- exact PayPal invoice body that would be sent
    message TEXT NOT NULL,          -- exact customer message
    money TEXT NOT NULL,
    flags TEXT NOT NULL,
    agent_notes TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    hash TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending | approved | denied | superseded | sent
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL REFERENCES drafts(id),
    quote_id TEXT NOT NULL,
    decision TEXT NOT NULL,         -- approved | denied
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    draft_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    quote_id TEXT PRIMARY KEY,      -- one live deposit invoice per quote
    draft_id INTEGER NOT NULL,
    approval_id INTEGER NOT NULL,
    paypal_invoice_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    amount TEXT NOT NULL,
    payer_url TEXT NOT NULL DEFAULT '',
    qr_png BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,      -- PayPal event id: processed at most once
    event_type TEXT NOT NULL,
    resource_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- agent | human | paypal | system
    actor TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id TEXT NOT NULL,
    title TEXT NOT NULL,
    owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.lastrowid

    def tx(self):
        """Serialize a multi-statement critical section (send / webhook)."""
        return self.lock

    # convenience -------------------------------------------------------------
    def quote(self, quote_id: str) -> dict | None:
        row = self.one("SELECT data, stage FROM quotes WHERE id = ?", (quote_id,))
        if not row:
            return None
        data = json.loads(row["data"])
        data["stage"] = row["stage"]
        return data

    def quotes(self) -> list[dict]:
        out = []
        for row in self.q("SELECT data, stage FROM quotes ORDER BY id"):
            d = json.loads(row["data"])
            d["stage"] = row["stage"]
            out.append(d)
        return out

    def set_stage(self, quote_id: str, stage: str) -> None:
        self.x("UPDATE quotes SET stage = ? WHERE id = ?", (stage, quote_id))

    def log(self, quote_id: str, kind: str, actor: str, text: str) -> None:
        self.x("INSERT INTO activity (quote_id, kind, actor, text, created_at) VALUES (?,?,?,?,?)",
               (quote_id, kind, actor, text, now_iso()))

    def reset(self) -> None:
        with self.lock:
            for t in ("tasks", "activity", "webhook_events", "invoices", "approvals", "drafts", "quotes"):
                self.conn.execute(f"DELETE FROM {t}")
            self.conn.execute("DELETE FROM sqlite_sequence")
