"""SQLite storage: schema, PII protection, and loading stored state back for reports.

PII is never stored in plaintext:
  - phone  -> HMAC-SHA256 (matching key) + last 4 digits (for the masked report column)
  - other `pii: true` fields -> HMAC-SHA256 (one-way; no report needs them back)
The HMAC key lives in <db>.key (or env PIPELINE_PII_KEY), never in the database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path

from ingest_leads import Lead

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_drops (
    fingerprint                 TEXT PRIMARY KEY,   -- sha256 over the drop's files: re-ingest is a no-op
    drop_name                   TEXT NOT NULL,
    ingested_at                 TEXT NOT NULL,
    lead_rows_total             INTEGER NOT NULL,
    duplicate_lead_rows_merged  INTEGER NOT NULL,
    field_warnings              INTEGER NOT NULL,
    events_total                INTEGER NOT NULL,
    malformed_events            INTEGER NOT NULL,
    duplicate_events_dropped    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    workflow_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,                     -- HMAC of the canonical phone
    phone_last4  TEXT NOT NULL,
    batch_id     TEXT NOT NULL,                     -- file stem of the first valid row; never changes
    attrs        TEXT NOT NULL,                     -- JSON of the other fields (PII fields hashed)
    PRIMARY KEY (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS rejections (             -- no PII: positions and reasons only
    fingerprint  TEXT NOT NULL REFERENCES ingested_drops(fingerprint),
    workflow_id  TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    row_number   INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    PRIMARY KEY (fingerprint, workflow_id, batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS events (                 -- the append-only interaction ledger
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  TEXT,                              -- UTC ISO-8601 (raw text if it could not be parsed)
    phone_hash   TEXT,                              -- NULL for events without a phone (overrides)
    payload      TEXT NOT NULL                      -- JSON
);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
"""


class Protector:
    """Keyed one-way hashing for PII."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _digest(self, namespace: str, value: str) -> str:
        return hmac.new(self._key, f"{namespace}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()

    def phone(self, canonical_phone: str) -> str:
        return self._digest("phone", canonical_phone)

    def field(self, field_name: str, value: str) -> str:
        return self._digest(f"pii:{field_name}", value)


def _load_key(db_path: Path, has_data: bool) -> bytes:
    env_key = os.environ.get("PIPELINE_PII_KEY")
    if env_key:
        return bytes.fromhex(env_key)
    key_path = Path(str(db_path) + ".key")
    if key_path.exists():
        return bytes.fromhex(key_path.read_text().strip())
    if has_data:
        raise RuntimeError(
            f"{db_path} already has data but its key file {key_path} is missing: "
            "new hashes would not match the stored ones"
        )
    key = os.urandom(32)
    key_path.write_text(key.hex())
    key_path.chmod(0o600)
    return key


def open_db(db_path: str | Path) -> tuple[sqlite3.Connection, Protector]:
    db_path = Path(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    has_data = conn.execute("SELECT COUNT(*) FROM ingested_drops").fetchone()[0] > 0
    return conn, Protector(_load_key(db_path, has_data))


def load_leads(conn: sqlite3.Connection) -> tuple[dict[tuple[str, str], Lead], dict[tuple[str, str], str]]:
    """Leads keyed by (workflow_id, phone_hash), plus each lead's last 4 phone digits."""
    leads: dict[tuple[str, str], Lead] = {}
    last4: dict[tuple[str, str], str] = {}
    for workflow_id, phone_hash, phone_last4, batch_id, attrs in conn.execute(
        "SELECT workflow_id, phone_hash, phone_last4, batch_id, attrs FROM leads"
    ):
        key = (workflow_id, phone_hash)
        leads[key] = Lead(workflow_id, phone_hash, batch_id, json.loads(attrs))
        last4[key] = phone_last4
    return leads, last4


def load_events(conn: sqlite3.Connection) -> list[dict]:
    """All ledger events in insertion order (= file order across drops)."""
    return [
        {
            "event_id": event_id, "event_type": event_type, "workflow_id": workflow_id,
            "call_id": call_id, "occurred_at": occurred_at, "phone_hash": phone_hash,
            "payload": json.loads(payload),
        }
        for event_id, event_type, workflow_id, call_id, occurred_at, phone_hash, payload in conn.execute(
            "SELECT event_id, event_type, workflow_id, call_id, occurred_at, phone_hash, payload "
            "FROM events ORDER BY rowid"
        )
    ]
