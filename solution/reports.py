"""Report calculations: daily_overview and lead_report rows.

Pure functions over derived state (calls, buckets, leads). They return rows as lists of
strings in the output-contract column order, already sorted; writing CSV files is a
separate step.
"""

from __future__ import annotations

from collections import defaultdict

from derive import Call, Catalogue, is_success
from ingest_leads import Lead
from read_events import to_ist

DAILY_OVERVIEW_HEADER = [
    "workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads",
]
LEAD_REPORT_HEADER = [
    "workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
    "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success",
]


def _bool(value: bool) -> str:
    return "true" if value else "false"


BATCH_SUMMARY_HEADER = [
    "workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
    "in_progress_telephony_failed", "in_progress_dialed", "completed",
]


def batch_summary_rows(
    leads: dict[tuple[str, str], Lead], buckets: dict[tuple[str, str], str]
) -> list[list[str]]:
    """One row per (workflow, batch) that has at least one lead, sorted by both.
    A lead belongs to the batch of its first valid row."""
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for key, lead in leads.items():
        counts[(lead.workflow_id, lead.batch_id)][buckets[key]] += 1
    return [
        [workflow_id, batch_id, str(sum(c.values())), str(c["yet_to_reach_out"]),
         str(c["in_progress_telephony_failed"]), str(c["in_progress_dialed"]), str(c["completed"])]
        for (workflow_id, batch_id), c in sorted(counts.items())
    ]


def daily_overview_rows(calls: dict[tuple[str, str], Call], catalogue: Catalogue) -> list[list[str]]:
    """One row per workflow per IST date that has at least one call, sorted by workflow, date.

    A call's date is the IST date of its call.initiated. success_leads counts distinct
    leads with at least one call that day whose effective disposition is a success.
    """
    total: dict[tuple[str, str], int] = defaultdict(int)
    connected: dict[tuple[str, str], int] = defaultdict(int)
    success_leads: dict[tuple[str, str], set] = defaultdict(set)

    for call in calls.values():
        key = (call.workflow_id, to_ist(call.initiated_at).date().isoformat())
        total[key] += 1
        connected[key] += call.connected
        if is_success(call, catalogue):
            success_leads[key].add(call.lead_key)

    return [
        [workflow_id, date, str(total[(workflow_id, date)]), str(connected[(workflow_id, date)]),
         f"{connected[(workflow_id, date)] / total[(workflow_id, date)]:.4f}",
         str(len(success_leads[(workflow_id, date)]))]
        for workflow_id, date in sorted(total)
    ]


def lead_report_rows(
    leads: dict[tuple[str, str], Lead],
    calls: dict[tuple[str, str], Call],
    buckets: dict[tuple[str, str], str],
    catalogue: Catalogue,
    reference_fields: dict[str, str],
    last4: dict[tuple[str, str], str] | None = None,
) -> list[list[str]]:
    """One row per lead, sorted by workflow_id, external_ref (ties broken by phone).

    `reference_fields` maps workflow_id -> name of the schema field marked "reference".
    `last4` maps lead key -> last 4 phone digits; needed when lead.phone is a hash.
    current_disposition is the effective disposition of the most recent call (by
    initiated time) that has one, not necessarily the most recent call.
    """
    by_lead: dict[tuple[str, str], list[Call]] = defaultdict(list)
    for call in calls.values():
        by_lead[call.lead_key].append(call)

    rows = []
    for key, lead in leads.items():
        lead_calls = sorted(by_lead.get(key, []), key=lambda c: c.initiated_at)
        last_call_at = (
            to_ist(lead_calls[-1].initiated_at).isoformat(timespec="seconds") if lead_calls else ""
        )
        current = next(
            (c.effective_disposition for c in reversed(lead_calls) if c.effective_disposition), ""
        )
        rows.append((
            lead.workflow_id,
            str(lead.fields[reference_fields[lead.workflow_id]]),
            lead.phone,
            [
                lead.workflow_id,
                str(lead.fields[reference_fields[lead.workflow_id]]),
                lead.batch_id,
                "+91******" + (last4[key] if last4 else lead.phone[-4:]),
                buckets[key],
                str(len(lead_calls)),
                str(sum(c.connected for c in lead_calls)),
                last_call_at,
                current,
                _bool(any(is_success(c, catalogue) for c in lead_calls)),
            ],
        ))
    rows.sort(key=lambda r: r[:3])
    return [r[3] for r in rows]
