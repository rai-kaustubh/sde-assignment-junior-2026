"""Build the five report files from everything stored so far.

Everything is derived at report time from the ledger and the leads, so the output always
reflects all drops ingested, however they were split.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from db import load_events, load_leads
from derive import assign_buckets, build_calls
from ingest_events import link_events
from read_events import is_unknown_disposition, load_dispositions
from read_leads import load_schema
from reports import (
    BATCH_SUMMARY_HEADER, DAILY_OVERVIEW_HEADER, LEAD_REPORT_HEADER,
    batch_summary_rows, daily_overview_rows, lead_report_rows,
)

REJECTIONS_HEADER = ["workflow_id", "batch_id", "row_number", "reason"]


def _write_csv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_reports(conn: sqlite3.Connection, schemas_dir: str | Path, out_dir: str | Path) -> dict:
    schemas_dir, out = Path(schemas_dir), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    catalogue = load_dispositions(schemas_dir / "dispositions.csv")
    leads, last4 = load_leads(conn)
    reference_fields = {
        w: next(f["name"] for f in load_schema(schemas_dir / f"{w}.json")["fields"] if f.get("reference"))
        for w in {lead.workflow_id for lead in leads.values()}
    }

    # Orphans first, then unknown dispositions (the README's order). Both stay out of the reports.
    linked, orphans = link_events(load_events(conn), set(leads))
    usable = [le for le in linked if not is_unknown_disposition(le.event, catalogue)]
    unknown = len(linked) - len(usable)

    calls = build_calls(usable)
    buckets = assign_buckets(set(leads), calls, catalogue)

    _write_csv(out / "batch_summary.csv", BATCH_SUMMARY_HEADER, batch_summary_rows(leads, buckets))
    _write_csv(out / "daily_overview.csv", DAILY_OVERVIEW_HEADER, daily_overview_rows(calls, catalogue))
    _write_csv(
        out / "lead_report.csv", LEAD_REPORT_HEADER,
        lead_report_rows(leads, calls, buckets, catalogue, reference_fields, last4),
    )
    _write_csv(
        out / "rejections.csv", REJECTIONS_HEADER,
        conn.execute(
            "SELECT workflow_id, batch_id, row_number, reason FROM rejections "
            "ORDER BY workflow_id, batch_id, row_number"
        ),
    )

    totals = conn.execute(
        "SELECT COALESCE(SUM(lead_rows_total), 0), COALESCE(SUM(duplicate_lead_rows_merged), 0), "
        "COALESCE(SUM(field_warnings), 0), COALESCE(SUM(events_total), 0), "
        "COALESCE(SUM(malformed_events), 0), COALESCE(SUM(duplicate_events_dropped), 0) FROM ingested_drops"
    ).fetchone()
    by_reason = dict(conn.execute("SELECT reason, COUNT(*) FROM rejections GROUP BY reason ORDER BY reason"))
    quality = {
        "lead_rows_total": totals[0],
        "lead_rows_rejected": sum(by_reason.values()),
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": totals[1],
        "field_warnings": totals[2],
        "events_total": totals[3],
        "malformed_events": totals[4],
        "duplicate_events_dropped": totals[5],
        "orphan_events": orphans,
        "unknown_disposition_events": unknown,
    }
    (out / "data_quality.json").write_text(json.dumps(quality, indent=2) + "\n", encoding="utf-8")
    return quality
