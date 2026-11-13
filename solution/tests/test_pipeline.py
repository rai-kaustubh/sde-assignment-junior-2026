"""End-to-end tests: ingest, idempotency, incremental == all-at-once, PII, append-only."""

import csv
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, SCHEMAS_DIR, SOLUTION_DIR
from db import open_db
from export import write_reports
from ingest import ingest_drop

REPORT_FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]
GENERATOR = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"


def run_ingest(db: Path, *drops: Path):
    conn, protector = open_db(db)
    try:
        return [ingest_drop(conn, protector, d, SCHEMAS_DIR) for d in drops]
    finally:
        conn.close()


def run_report(db: Path, out: Path) -> dict:
    conn, _ = open_db(db)
    try:
        return write_reports(conn, SCHEMAS_DIR, out)
    finally:
        conn.close()


def read_reports(out: Path) -> dict[str, bytes]:
    return {name: (out / name).read_bytes() for name in REPORT_FILES}


@pytest.fixture(scope="module")
def data(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("generated")
    subprocess.run([sys.executable, str(GENERATOR), "--seed", "7", "--scale", "small", "--out", str(out)],
                   check=True, capture_output=True)
    return out


def test_reingesting_a_drop_changes_nothing(data, tmp_path):
    db = tmp_path / "t.db"
    run_ingest(db, data / "drop_1", data / "drop_2")
    run_report(db, tmp_path / "before")
    again = run_ingest(db, data / "drop_1", data / "drop_2")
    run_report(db, tmp_path / "after")
    assert all(s.skipped for s in again)
    assert read_reports(tmp_path / "before") == read_reports(tmp_path / "after")


def test_incremental_equals_all_at_once(data, tmp_path):
    run_ingest(tmp_path / "inc.db", data / "drop_1", data / "drop_2")
    run_report(tmp_path / "inc.db", tmp_path / "inc")

    merged = tmp_path / "merged_drop"  # both drops' files in one drop, filename order preserved
    for drop in (data / "drop_1", data / "drop_2"):
        for f in drop.rglob("*"):
            if f.is_file():
                target = merged / f.relative_to(drop)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(f, target)
    run_ingest(tmp_path / "bulk.db", merged)
    run_report(tmp_path / "bulk.db", tmp_path / "bulk")

    assert read_reports(tmp_path / "inc") == read_reports(tmp_path / "bulk")


def test_report_contract_shapes(data, tmp_path):
    run_ingest(tmp_path / "t.db", data / "drop_1", data / "drop_2")
    quality = run_report(tmp_path / "t.db", tmp_path / "out")
    assert set(quality) == {
        "lead_rows_total", "lead_rows_rejected", "rejections_by_reason", "duplicate_lead_rows_merged",
        "field_warnings", "events_total", "malformed_events", "duplicate_events_dropped",
        "orphan_events", "unknown_disposition_events",
    }
    assert quality["lead_rows_rejected"] == sum(quality["rejections_by_reason"].values())
    with open(tmp_path / "out" / "rejections.csv", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["workflow_id", "batch_id", "row_number", "reason"]
    assert len(rows) - 1 == quality["lead_rows_rejected"]
    with open(tmp_path / "out" / "lead_report.csv", newline="") as fh:
        lead_rows = list(csv.DictReader(fh))
    assert all(r["phone_masked"].startswith("+91******") and len(r["phone_masked"]) == 13 for r in lead_rows)
    assert json.loads((tmp_path / "out" / "data_quality.json").read_text()) == quality


def test_no_pii_in_the_database_file(data, tmp_path):
    from ingest_leads import preprocess_drop
    db = tmp_path / "t.db"
    run_ingest(db, data / "drop_1", data / "drop_2")
    raw = db.read_bytes()
    leads = preprocess_drop(data / "drop_1", SCHEMAS_DIR).leads
    assert leads
    for (_, phone), lead in list(leads.items())[:100]:
        assert phone.encode() not in raw and phone[3:].encode() not in raw       # +91XXXXXXXXXX and XXXXXXXXXX
        assert lead.fields["customer_name"].encode() not in raw


def test_events_ledger_is_append_only(data, tmp_path):
    db = tmp_path / "t.db"
    run_ingest(db, data / "drop_1")
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET call_id = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events")
    conn.close()


# --- small hand-built drops -------------------------------------------------------------

def make_drop(root: Path, name: str, leads: list[str], events: list[dict]) -> Path:
    drop = root / name
    (drop / "leads" / "motor_renewal").mkdir(parents=True)
    (drop / "events").mkdir()
    if leads:
        (drop / "leads" / "motor_renewal" / f"batch_{name}.csv").write_text(
            "customer_name,phone,policy_number,preferred_language\n" + "\n".join(leads) + "\n")
    (drop / "events" / f"events_{name}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return drop


def initiated(event_id, phone, call_id="call_1", when="2026-10-03 10:01:00"):
    return {"event_id": event_id, "event_type": "call.initiated", "workflow_id": "motor_renewal",
            "call_id": call_id, "occurred_at": when, "phone": phone, "payload": {}}


def test_event_that_arrives_before_its_lead_counts_once_the_lead_arrives(tmp_path):
    db = tmp_path / "t.db"
    d1 = make_drop(tmp_path, "one", [], [initiated("e1", "91-98765-43210")])
    d2 = make_drop(tmp_path, "two", ["Asha,9876543210,POL-1,"], [])
    run_ingest(db, d1)
    assert run_report(db, tmp_path / "r1")["orphan_events"] == 1
    run_ingest(db, d2)
    assert run_report(db, tmp_path / "r2")["orphan_events"] == 0
    daily = (tmp_path / "r2" / "daily_overview.csv").read_text().splitlines()
    assert daily[1] == "motor_renewal,2026-10-03,1,0,0.0000,0"   # naive timestamp is IST


def test_orphan_check_comes_before_unknown_disposition(tmp_path):
    db = tmp_path / "t.db"
    unknown = {"event_id": "e1", "event_type": "disposition.set", "workflow_id": "motor_renewal",
               "call_id": "call_1", "occurred_at": "2026-10-03T10:00:00Z", "phone": "9000000001",
               "payload": {"disposition_code": "NOT_IN_CATALOGUE"}}
    d1 = make_drop(tmp_path, "one", ["Asha,9876543210,POL-1,"], [unknown])      # phone is not a lead
    run_ingest(db, d1)
    quality = run_report(db, tmp_path / "r")
    assert (quality["orphan_events"], quality["unknown_disposition_events"]) == (1, 0)


def test_later_duplicate_lead_merges_only_non_null_values_and_keeps_batch(tmp_path):
    db = tmp_path / "t.db"
    d1 = make_drop(tmp_path, "one", ["Asha,9876543210,POL-1,hi"], [])
    d2 = make_drop(tmp_path, "two", ["Asha K,98765 43210,POL-2,"], [])           # blank language must not erase "hi"
    run_ingest(db, d1, d2)
    quality = run_report(db, tmp_path / "r")
    assert quality["duplicate_lead_rows_merged"] == 1
    lead = list(csv.DictReader(open(tmp_path / "r" / "lead_report.csv")))[0]
    assert (lead["external_ref"], lead["batch_id"]) == ("POL-2", "batch_one")    # last write wins; batch is permanent
    conn = sqlite3.connect(db)
    assert json.loads(conn.execute("SELECT attrs FROM leads").fetchone()[0])["preferred_language"] == "hi"
    conn.close()


def test_default_language_applies_after_merging(tmp_path):
    db = tmp_path / "t.db"
    d1 = make_drop(tmp_path, "one", ["Asha,9876543210,POL-1,"], [])               # null -> default "en" for now
    d2 = make_drop(tmp_path, "two", ["Asha,9876543210,POL-1,TE"], [])             # a real later value replaces it
    run_ingest(db, d1, d2)
    conn = sqlite3.connect(db)
    assert json.loads(conn.execute("SELECT attrs FROM leads").fetchone()[0])["preferred_language"] == "te"
    conn.close()
