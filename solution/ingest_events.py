"""Orchestrator: read every event file in a drop and apply the event rules.

Rule 1 (malformed lines are counted and skipped) and rule 2 (an event_id already seen,
in this or any earlier drop, is counted and skipped) happen here.

Orphans and unknown dispositions are decided later, in report order (orphan first, then
unknown), over every event ingested so far against every lead so far, because an event's
lead may only arrive in a later drop. `link_events` does the linking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Collection, NamedTuple

from read_events import event_lead_key, parse_event_line


@dataclass
class EventResult:
    events_total: int = 0  # every non-empty line read, including malformed ones
    malformed: int = 0  # not valid JSON, or no event_id
    duplicates_dropped: int = 0  # event_id already seen in this or an earlier drop
    events: list[dict] = field(default_factory=list)  # well-formed, first-seen events, in file order


def ingest_events(drop_dir: str | Path, seen_ids: set[str] | None = None) -> EventResult:
    """Read <drop_dir>/events/*.jsonl in filename order, line by line.

    `seen_ids` holds every event_id accepted so far and is updated in place. Pass the
    same set to each drop so a duplicate in a later drop is still caught.
    """
    if seen_ids is None:
        seen_ids = set()
    result = EventResult()
    events_dir = Path(drop_dir) / "events"
    if not events_dir.is_dir():
        return result

    for jsonl_file in sorted(events_dir.glob("*.jsonl")):
        with open(jsonl_file, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue  # blank lines are not events and are not counted
                result.events_total += 1
                event = parse_event_line(line)
                if event is None:
                    result.malformed += 1
                    continue
                if event["event_id"] in seen_ids:
                    result.duplicates_dropped += 1
                    continue
                seen_ids.add(event["event_id"])
                result.events.append(event)

    return result


class LinkedEvent(NamedTuple):
    lead_key: tuple[str, str]  # (workflow_id, canonical phone) of the lead
    event: dict


def link_events(
    events: list[dict], lead_keys: Collection[tuple[str, str]]
) -> tuple[list[LinkedEvent], int]:
    """Link events to leads; return (linked events in input order, orphan count).

    - Most events link by (workflow_id, normalised phone) to an existing lead.
    - disposition.override has no phone: it links through (workflow_id, call_id) to the
      lead that the same call's phone-linked events point to.
    - Anything that can't be linked is an orphan: counted, and left out of the result.

    Call on ALL events and ALL leads ingested so far, so the result is the final state.
    """
    # Pass 1: which lead does each call belong to? Built from phone-linked events only.
    call_lead: dict[tuple[str, str], tuple[str, str]] = {}
    for event in events:
        key = event_lead_key(event)
        if key in lead_keys and event.get("call_id"):
            call_lead.setdefault((event["workflow_id"], event["call_id"]), key)

    # Pass 2: link every event; overrides go through call_lead.
    linked: list[LinkedEvent] = []
    orphans = 0
    for event in events:
        if event.get("event_type") == "disposition.override":
            key = call_lead.get((event.get("workflow_id"), event.get("call_id")))
        else:
            key = event_lead_key(event)
            if key not in lead_keys:
                key = None
        if key is None:
            orphans += 1
        else:
            linked.append(LinkedEvent(key, event))
    return linked, orphans
