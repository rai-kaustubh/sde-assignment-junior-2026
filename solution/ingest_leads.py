"""Orchestrator: read every lead file in a drop and pre-process each row.

Pre-processing = read file -> map headers to schema fields -> reject, or accept
and normalise values (invalid optional values become None + one field warning).
Accepted rows are then merged into leads by (workflow_id, phone), and defaults
fill fields still null after merging. Not done here: the database write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from read_leads import (
    apply_defaults,
    build_header_map,
    load_schema,
    map_row_to_fields,
    normalize_fields,
    read_lead_file,
    rejection_reason,
)


class Rejection(NamedTuple):
    workflow_id: str
    batch_id: str
    row_number: int
    reason: str


class AcceptedRow(NamedTuple):
    workflow_id: str
    batch_id: str
    row_number: int
    fields: dict[str, object | None]  # schema field name -> normalised value (None = missing/invalid)
    warnings: int  # optional fields that were present but invalid


@dataclass
class Lead:
    workflow_id: str
    phone: str  # canonical +91XXXXXXXXXX; identity is (workflow_id, phone)
    batch_id: str  # batch of the first valid row; never changes
    fields: dict[str, object | None]


@dataclass
class DropResult:
    rows_total: int = 0  # non-blank data rows read
    field_warnings: int = 0  # summed over accepted rows only
    duplicates_merged: int = 0  # accepted rows merged into an existing lead
    leads: dict[tuple[str, str], Lead] = field(default_factory=dict)
    accepted: list[AcceptedRow] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


def preprocess_drop(drop_dir: str | Path, schema_dir: str | Path) -> DropResult:
    """Pre-process all lead files under <drop_dir>/leads/<workflow_id>/*.csv.

    Workflows are visited in name order and files in filename order, so rows come
    out in the order the duplicate/merge rules need. workflow_id is the folder name,
    batch_id is the file stem.
    """
    drop_dir, schema_dir = Path(drop_dir), Path(schema_dir)
    result = DropResult()

    leads_dir = drop_dir / "leads"
    if not leads_dir.is_dir():
        return result

    for workflow_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
        workflow_id = workflow_dir.name
        schema = load_schema(schema_dir / f"{workflow_id}.json")
        header_map = build_header_map(schema)

        for csv_file in sorted(workflow_dir.glob("*.csv")):
            batch_id = csv_file.stem

            for row_number, row in read_lead_file(csv_file):
                result.rows_total += 1
                fields = map_row_to_fields(row, header_map)

                reason = rejection_reason(fields, schema)
                if reason is not None:
                    result.rejections.append(Rejection(workflow_id, batch_id, row_number, reason))
                else:
                    values, warnings = normalize_fields(fields, schema)
                    result.field_warnings += warnings
                    result.accepted.append(
                        AcceptedRow(workflow_id, batch_id, row_number, values, warnings)
                    )
                    _merge_row(result, workflow_id, batch_id, values)

        # All rows of this workflow are merged; only now fill still-null fields.
        for lead in result.leads.values():
            if lead.workflow_id == workflow_id:
                lead.fields = apply_defaults(lead.fields, schema)

    return result


def _merge_row(result: DropResult, workflow_id: str, batch_id: str, values: dict) -> None:
    """First valid row for (workflow_id, phone) creates the lead and fixes its batch_id.
    Each later one counts as a merged duplicate and overwrites only non-null values."""
    key = (workflow_id, values["phone"])
    lead = result.leads.get(key)
    if lead is None:
        result.leads[key] = Lead(workflow_id, values["phone"], batch_id, dict(values))
        return
    result.duplicates_merged += 1
    lead.fields.update({k: v for k, v in values.items() if v is not None})
