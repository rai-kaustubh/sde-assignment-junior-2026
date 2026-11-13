"""Derived facts: per-call state from linked events, and each lead's bucket.

Pure functions over already-linked events; no files, no database. The ledger is never
edited: overrides are applied here, when deriving the effective disposition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ingest_events import LinkedEvent
from read_events import parse_timestamp

Catalogue = dict[str, dict[str, tuple[bool, bool]]]  # workflow -> code -> (is_terminal, is_success)

YET_TO_REACH_OUT = "yet_to_reach_out"
COMPLETED = "completed"
IN_PROGRESS_TELEPHONY_FAILED = "in_progress_telephony_failed"
IN_PROGRESS_DIALED = "in_progress_dialed"


@dataclass
class Call:
    workflow_id: str
    call_id: str
    lead_key: tuple[str, str]
    initiated_at: datetime  # UTC; the call's date is the IST date of this
    connected: bool = False
    telephony_failed: bool = False
    latest_set: tuple[datetime, str] | None = None  # (occurred_at, code)
    latest_override: tuple[datetime, str] | None = None

    @property
    def effective_disposition(self) -> str | None:
        """Latest override by occurred_at; else latest disposition.set; else None."""
        chosen = self.latest_override or self.latest_set
        return chosen[1] if chosen else None


def _latest(current: tuple[datetime, str] | None, ts: datetime, code: str):
    """Keep the later occurred_at; on a tie the event seen later wins."""
    return (ts, code) if current is None or ts >= current[0] else current


def build_calls(linked: list[LinkedEvent]) -> dict[tuple[str, str], Call]:
    """Build per-call state, keyed by (workflow_id, call_id).

    A call exists only if its call.initiated event is present (and linked). Other events
    of a call with no initiated event are ignored. "Latest" is by occurred_at, so the
    arrival order of events does not matter.
    """
    calls: dict[tuple[str, str], Call] = {}

    for lead_key, event in linked:  # pass 1: create calls from call.initiated
        if event.get("event_type") != "call.initiated" or not event.get("call_id"):
            continue
        ts = parse_timestamp(event.get("occurred_at"))
        if ts is None:
            continue
        key = (event["workflow_id"], event["call_id"])
        call = calls.get(key)
        if call is None:
            calls[key] = Call(event["workflow_id"], event["call_id"], lead_key, ts)
        elif ts < call.initiated_at:
            call.initiated_at = ts

    for _, event in linked:  # pass 2: everything else attaches to an existing call
        call = calls.get((event.get("workflow_id"), event.get("call_id")))
        if call is None:
            continue
        kind = event.get("event_type")
        if kind == "call.connected":
            call.connected = True
        elif kind == "call.telephony_failed":
            call.telephony_failed = True
        elif kind in ("disposition.set", "disposition.override"):
            ts = parse_timestamp(event.get("occurred_at"))
            payload = event.get("payload")
            code = payload.get("disposition_code") if isinstance(payload, dict) else None
            if ts is None or not code:
                continue
            if kind == "disposition.set":
                call.latest_set = _latest(call.latest_set, ts, code)
            else:
                call.latest_override = _latest(call.latest_override, ts, code)
    return calls


def is_terminal(call: Call, catalogue: Catalogue) -> bool:
    code = call.effective_disposition
    return code is not None and catalogue.get(call.workflow_id, {}).get(code, (False, False))[0]


def is_success(call: Call, catalogue: Catalogue) -> bool:
    code = call.effective_disposition
    return code is not None and catalogue.get(call.workflow_id, {}).get(code, (False, False))[1]


def lead_bucket(calls: list[Call], catalogue: Catalogue) -> str:
    """First match wins: no calls; any terminal disposition; every call telephony-failed;
    otherwise dialed."""
    if not calls:
        return YET_TO_REACH_OUT
    if any(is_terminal(c, catalogue) for c in calls):
        return COMPLETED
    if all(c.telephony_failed for c in calls):
        return IN_PROGRESS_TELEPHONY_FAILED
    return IN_PROGRESS_DIALED


def assign_buckets(
    lead_keys: set[tuple[str, str]] | list[tuple[str, str]],
    calls: dict[tuple[str, str], Call],
    catalogue: Catalogue,
) -> dict[tuple[str, str], str]:
    """Bucket for every lead, including leads with no calls."""
    by_lead: dict[tuple[str, str], list[Call]] = {}
    for call in calls.values():
        by_lead.setdefault(call.lead_key, []).append(call)
    return {key: lead_bucket(by_lead.get(key, []), catalogue) for key in lead_keys}
