# Submission

## Candidate
- **Name:** _fill in_
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** _fill in_

## How to run
```bash
# Tested on Python 3.13; the pipeline itself is standard library only
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python solution/cli.py ingest data/drop_1
python solution/cli.py ingest data/drop_2          # or: ingest data  (all drops, natural order)
python solution/cli.py report --out reports/
# options: --db pipeline.db (default), --schemas assignment-a-data/schemas (default)

pip install pytest && python -m pytest solution/tests     # 60 tests, ~2 s
```
Re-running `ingest` on a drop already stored prints "already ingested" and writes nothing.

## Design
Two steps. **Ingest** reads and cleans a drop, merges it with what is stored, and writes it in
one SQLite transaction. **Report** re-derives everything from the stored data, so reports always
reflect all drops ingested so far, however they were split.

```
ingest <drop>
  leads/<wf>/*.csv -> map headers -> reject | accept -> normalise values -> merge by (wf, phone)
                                                                                |
  events/*.jsonl  -> drop malformed -> drop duplicate event_id -----------------+-> one transaction
                                                                                      -> SQLite
report
  SQLite -> link events to leads (orphans) -> drop unknown dispositions
         -> per-call state -> buckets -> 5 files
```
Code layout: `read_leads.py` / `read_events.py` (reading and normalising helpers, no state),
`ingest_leads.py` / `ingest_events.py` (per-drop work), `ingest.py` (merge and write),
`derive.py` (calls, effective disposition, buckets), `reports.py` (report rows), `export.py`
(writes the files), `db.py` (schema, PII hashing), `cli.py`.

**Schema (SQLite)**
- `leads(workflow_id, phone_hash, phone_last4, batch_id, attrs JSON)`, primary key
  `(workflow_id, phone_hash)`: lead identity. `batch_id` is set once, by the first valid row.
- `events(event_id PK, event_type, workflow_id, call_id, occurred_at UTC, phone_hash, payload JSON)`:
  the append-only ledger. Triggers abort any `UPDATE` or `DELETE`.
- `rejections(fingerprint, workflow_id, batch_id, row_number, reason)`: positions and reasons only.
- `ingested_drops(fingerprint PK, counters...)`: idempotency, and the sums behind `data_quality.json`.

**Why events are not linked at write time.** An event may arrive before its lead, and a nullable
`lead_id` on the ledger would have to be updated later, which the append-only trigger forbids.
So linking is done at report time by joining on `(workflow_id, phone_hash)`, and overrides through
`call_id`. An event orphaned after `drop_1` links itself once its lead arrives in `drop_2`, and
orphan counts are always the final state. The same reasoning is why bucket is not a column: it
changes whenever events, overrides or leads arrive, so it is derived at report time.

**PII at rest.** `phone` and `customer_name` (the `pii: true` fields) are never stored in
plaintext. Phone is an HMAC-SHA256 under a secret key (used to match events to leads) plus the
last 4 digits (for the masked column). Other PII fields are HMAC hashes. The key is in
`<db>.key` (or `PIPELINE_PII_KEY`), not in the database; opening a populated database without its
key is an error. Event phones are hashed the same way, and rejections hold no values.

## Decisions & assumptions
| Decision | Why | Alternative considered |
|---|---|---|
| Flush to the database once per drop, in one transaction | Each `ingest` is its own process, merging needs earlier drops' leads, and a crash must leave nothing half-written | Hold everything in memory until all drops are read: breaks incremental runs |
| Idempotency by a content fingerprint of the drop | Same files => no-op, whatever the path or name | Track file names: a renamed copy would be re-ingested |
| Customer name is hashed, not encrypted | No report needs it back; keeps the pipeline standard-library only | Fernet encryption (`cryptography` is in requirements.txt but unused) if names must be recoverable |
| Orphan check before unknown-disposition check | README order: an unlinked event with an unknown code counts as an orphan only | Check unknown first: miscounts both |
| Unknown-disposition and orphan events stay in the ledger, but out of every report | Append-only ledger; they are excluded when deriving | Not storing them: loses the ability to link later |
| Per-call state keyed by `(workflow_id, call_id)`, "latest" by `occurred_at`, ties to the later event in file order | Events arrive out of order; `connected` must survive a later `call.ended` | One latest event type per lead: loses per-call facts |
| A call exists only if its `call.initiated` is present and linked; its date is the earliest initiated time in IST | README rule | |
| Defaults applied after merging, including across drops | A real later value must beat the default | Applying defaults per drop |
| Merged duplicates are counted per row, including the first row of a lead already stored by an earlier drop | Gives the same total as ingesting everything at once | |
| A required non-phone field that is present but invalid is not rejected | The README names only two rejection reasons; no schema here can hit it | Reject as missing_required |
| Header collisions: the leftmost matching column wins | Spec silent | |
| `rejections_by_reason` lists only reasons that occurred | Matches a counter of what was seen | Listing every possible reason with zeros |
| `batch_summary.csv` has a row for each batch with at least one lead | A batch whose rows were all rejected has no leads to summarise | Emit zero rows |
| `lead_report` ties on `external_ref` are broken by phone hash | Spec silent; none occur in the generated data | |
| Decimals accept lenient comma grouping (`1,23,456`) | Spec says commas are thousands separators and does not define grouping; Indian lakh format is common | Strict 3-digit groups |

## Failure modes considered
- **Bad input:** malformed JSON lines, a missing `event_id`, bad dates and amounts, blank rows,
  extra and renamed columns, BOM, short rows are all handled and counted.
- **Re-runs:** a repeated drop is a no-op. Duplicate `event_id`s across drops are dropped.
- **Crash mid-ingest:** one transaction per drop, so the database stays as it was.
- **Lost key file:** refused with a clear error rather than silently re-hashing.
- **Not handled:** two `ingest` processes at once (SQLite serialises writers but there is no
  queueing or retry); editing a drop file in place and re-running it is treated as a new drop, so
  its lead rows count as merged duplicates; reports re-derive from all stored data every time,
  holding all events in memory; the key file is not rotated.

## Testing
60 pytest tests (`solution/tests`):
- phone, date, decimal, enum, header normalisation, and the rejection order;
- effective disposition (override beats set, latest by `occurred_at`, out-of-order arrival), bucket rules, IST date boundaries;
- idempotent re-ingest, **incremental (drop_1 then drop_2) equals ingesting both at once, byte for byte**,
  an event that arrives before its lead, orphan-before-unknown order, duplicate-lead merge and defaults;
- the database file contains no phone or customer name, and the ledger rejects `UPDATE`/`DELETE`.

Not tested: concurrent writers, a corrupted database file, very large single rows. The expected
output of the official fresh seed is not available here, so correctness rests on the README rules
and the tests above; two README examples (connectivity `0.5625`, `last_call_at`
`2026-10-14T17:55:24+05:30`) appear in the output for seed 42.

## Results
- Seed 42, small: 2,057 lead rows (108 rejected, 1,892 leads, 57 merged duplicates), 8,745 event
  lines (8 malformed, 237 duplicates, 462 orphans, 18 unknown dispositions).
- Large (`--scale large`, ~144k lead rows, ~633k event lines), on this laptop: ingest `drop_1`
  7.8 s, `drop_2` 6.9 s, report 11.9 s. About 27 s in total (target: under 2 minutes). Re-ingesting a
  drop takes 0.2 s.
- Surprises in the data: one event was both an orphan and had an unknown disposition (the
  README's check order decides which counter it lands in); full language names such as `Hindi` and
  `tamil` show up in `preferred_language` and become warnings; overrides have no phone.

## If I had another day
1. Do the report aggregation in SQL so reports do not load every event into memory.
2. Make reports incremental (cache per-call state), instead of re-deriving from scratch.
3. Production at 100k leads/day: schedule `ingest` per drop from a queue or cron, with retries
   (safe because a drop is idempotent); keep the PII key in a secrets manager; move to
   PostgreSQL for concurrent writers; backfill by re-running `report`, or by replaying drops into a
   fresh database, since everything is derived from the ledger.
4. Alerting on counters (rejection rate, orphan rate) per drop.
