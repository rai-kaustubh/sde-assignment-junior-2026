"""Event file helpers: reading JSON Lines, timestamps and phones. No linking or state."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from read_leads import normalize_phone

IST = timezone(timedelta(hours=5, minutes=30))


def parse_event_line(line: str) -> dict | None:
    """Parse one JSONL line. None if malformed: not valid JSON, not a JSON object,
    or no usable `event_id`."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or not event.get("event_id"):
        return None
    return event


def read_event_file(path: str | Path) -> Iterator[dict | None]:
    """Yield one item per non-empty line: the event dict, or None if the line is malformed.

    Empty/whitespace-only lines are skipped, so the number of items yielded is the
    file's `events_total` (malformed and duplicate lines included).
    """
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield parse_event_line(line)


def load_dispositions(path: str | Path) -> dict[str, dict[str, tuple[bool, bool]]]:
    """Read dispositions.csv into {workflow_id: {code: (is_terminal, is_success)}}."""
    catalogue: dict[str, dict[str, tuple[bool, bool]]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            catalogue.setdefault(row["workflow_id"].strip(), {})[row["code"].strip()] = (
                row["is_terminal"].strip().lower() == "true",
                row["is_success"].strip().lower() == "true",
            )
    return catalogue


def is_unknown_disposition(event: dict, catalogue: dict[str, dict[str, tuple[bool, bool]]]) -> bool:
    """True for a disposition.* event whose code is not in the workflow's catalogue."""
    if not str(event.get("event_type", "")).startswith("disposition."):
        return False
    payload = event.get("payload")
    code = payload.get("disposition_code") if isinstance(payload, dict) else None
    return code not in catalogue.get(event.get("workflow_id"), {})


def parse_timestamp(value: object) -> datetime | None:
    """Parse `...Z` (UTC), `...+05:30` (offset) or naive `YYYY-MM-DD HH:MM:SS` (IST).

    Returns a timezone-aware datetime in UTC, or None if it can't be parsed.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)  # naive means IST
    return parsed.astimezone(timezone.utc)


def to_ist(moment: datetime) -> datetime:
    """Convert an aware datetime to IST (reports use IST dates)."""
    return moment.astimezone(IST)


def event_phone(event: dict) -> str | None:
    """Canonical +91XXXXXXXXXX phone of an event, or None if absent/invalid."""
    phone = event.get("phone")
    return normalize_phone(phone) if isinstance(phone, str) else None


def event_lead_key(event: dict) -> tuple[str, str] | None:
    """(workflow_id, phone) an event links to, or None if it has no usable phone
    (e.g. disposition.override, which links through its call_id instead).

    Events loaded from the database carry `phone_hash` instead of a plaintext phone; then
    the hash is the key, matching leads keyed by (workflow_id, phone_hash)."""
    phone = event.get("phone_hash") if "phone_hash" in event else event_phone(event)
    workflow_id = event.get("workflow_id")
    return (workflow_id, phone) if phone and workflow_id else None
