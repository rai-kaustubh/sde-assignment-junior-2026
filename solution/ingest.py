"""Ingest: pre-process a drop, merge it with what is already stored, write it in one transaction."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from db import Protector
from ingest_events import ingest_events
from ingest_leads import Lead, preprocess_drop
from read_events import event_phone, parse_timestamp
from read_leads import apply_defaults, load_schema


@dataclass
class DropSummary:
    drop: str
    skipped: bool = False  # already ingested: nothing was changed
    lead_rows: int = 0
    rejected: int = 0
    leads_created: int = 0
    leads_merged_into_existing: int = 0
    events_read: int = 0
    events_stored: int = 0


def discover_drops(path: str | Path) -> list[Path]:
    """A drop directory itself, or a directory of drops ingested in natural name order."""
    path = Path(path)
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    natural = lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]
    return sorted((p for p in path.iterdir() if p.is_dir()), key=natural)


def fingerprint(drop_dir: Path) -> str:
    """Content hash of every file in the drop; the same files always give the same value."""
    digest = hashlib.sha256()
    for file in sorted(p for p in drop_dir.rglob("*") if p.is_file()):
        digest.update(file.relative_to(drop_dir).as_posix().encode())
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def _protect(fields: dict, schema: dict, protector: Protector) -> dict:
    """Storable form of a lead's fields: no phone (it has its own columns), PII fields
    hashed, decimals as strings."""
    stored: dict = {}
    for f in schema["fields"]:
        if f["type"] == "phone":
            continue
        value = fields.get(f["name"])
        if value is not None and f.get("pii"):
            value = protector.field(f["name"], str(value))
        elif isinstance(value, Decimal):
            value = str(value)
        stored[f["name"]] = value
    return stored


def _save_leads(
    conn: sqlite3.Connection, protector: Protector, schemas: dict, leads: dict[tuple[str, str], Lead]
) -> tuple[int, int]:
    """Merge this drop's leads into the table. Returns (created, merged into existing)."""
    new_rows, updates = [], []
    for (workflow_id, phone), lead in leads.items():
        schema = schemas[workflow_id]
        phone_hash = protector.phone(phone)
        incoming = _protect(lead.fields, schema, protector)
        existing = conn.execute(
            "SELECT attrs FROM leads WHERE workflow_id = ? AND phone_hash = ?", (workflow_id, phone_hash)
        ).fetchone()
        if existing is None:
            attrs = apply_defaults(incoming, schema)
            new_rows.append((workflow_id, phone_hash, phone[-4:], lead.batch_id, json.dumps(attrs, sort_keys=True)))
        else:  # the lead exists from an earlier drop: merge non-null values; batch_id stays
            attrs = json.loads(existing[0])
            attrs.update({k: v for k, v in incoming.items() if v is not None})
            attrs = apply_defaults(attrs, schema)
            updates.append((json.dumps(attrs, sort_keys=True), workflow_id, phone_hash))
    conn.executemany("INSERT INTO leads VALUES (?, ?, ?, ?, ?)", new_rows)
    conn.executemany("UPDATE leads SET attrs = ? WHERE workflow_id = ? AND phone_hash = ?", updates)
    return len(new_rows), len(updates)


def _event_row(event: dict, protector: Protector) -> tuple:
    text = lambda v: v if isinstance(v, str) else None
    moment = parse_timestamp(event.get("occurred_at"))
    phone = event_phone(event)
    payload = event.get("payload")
    return (
        event["event_id"], text(event.get("event_type")), text(event.get("workflow_id")),
        text(event.get("call_id")),
        moment.isoformat() if moment else text(event.get("occurred_at")),
        protector.phone(phone) if phone else None,
        json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True),
    )


def ingest_drop(
    conn: sqlite3.Connection, protector: Protector, drop_dir: str | Path, schemas_dir: str | Path
) -> DropSummary:
    """Ingest one drop. Re-ingesting a drop already stored changes nothing.

    Everything is written in a single transaction: a crash leaves the database as it was.
    """
    drop_dir = Path(drop_dir)
    summary = DropSummary(drop_dir.name)
    drop_id = fingerprint(drop_dir)
    if conn.execute("SELECT 1 FROM ingested_drops WHERE fingerprint = ?", (drop_id,)).fetchone():
        summary.skipped = True
        return summary

    leads_result = preprocess_drop(drop_dir, schemas_dir, apply_default_values=False)
    seen_ids = {row[0] for row in conn.execute("SELECT event_id FROM events")}
    events_result = ingest_events(drop_dir, seen_ids)
    schemas = {w: load_schema(Path(schemas_dir) / f"{w}.json") for w in {k[0] for k in leads_result.leads}}

    with conn:  # one transaction: commits on success, rolls back on any error
        created, merged_existing = _save_leads(conn, protector, schemas, leads_result.leads)
        conn.executemany(
            "INSERT INTO rejections VALUES (?, ?, ?, ?, ?)",
            [(drop_id, r.workflow_id, r.batch_id, r.row_number, r.reason) for r in leads_result.rejections],
        )
        conn.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            [_event_row(e, protector) for e in events_result.events],
        )
        conn.execute(
            "INSERT INTO ingested_drops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                drop_id, drop_dir.name, datetime.now(timezone.utc).isoformat(),
                leads_result.rows_total, leads_result.duplicates_merged + merged_existing,
                leads_result.field_warnings, events_result.events_total,
                events_result.malformed, events_result.duplicates_dropped,
            ),
        )

    summary.lead_rows, summary.rejected = leads_result.rows_total, len(leads_result.rejections)
    summary.leads_created, summary.leads_merged_into_existing = created, merged_existing
    summary.events_read, summary.events_stored = events_result.events_total, len(events_result.events)
    return summary
