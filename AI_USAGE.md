# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
Claude Code (Claude Sonnet 5.5) in VS Code, working in the repository.

## What I used them for
- Reading the Assignment A README and proposing an approach.
- Writing the code in `solution/` step by step: the CSV reader and header normalisation, the
  phone/date/decimal/enum normalisers, the rejection rule, the event reader, the call-state and
  bucket derivation, the report rows, the SQLite layer, the CLI, and the tests.
- Running the code against the generated data and checking the numbers.
- Drafting `SUBMISSION.md`.

## What I wrote or decided myself
_Review and edit this section: it should only claim what is true for you._
- Chose to work one step at a time (leads, then schema, then events) rather than take a full plan.
- Proposed the in-memory-map-first idea for leads and the lead-to-latest-event-type map for events;
  both were then argued through and refined with the AI (see below).
- Decided the file split: reading and normalising helpers in `read_leads` / `read_events`, the
  main work in the `ingest_*` files.
- Decided which pieces to write, in which order, and what to leave out for later (for example,
  header-to-schema matching before the value parsers).
- Spotted that `leads` should not have a bucket column.

## Where the AI was wrong or unhelpful
- The first plan put a nullable `lead_id` on `events` to link orphans later. That needs an `UPDATE`,
  which the append-only trigger forbids. Found when walking through the schema; replaced by linking at
  report time.
- The first date parser accepted mixed separators such as `01/12-2026` (a regex allowing `/` or `-`
  on each side independently). Found by running a larger set of date test cases; fixed with a
  backreference so both separators match.
- A summary of sample output retyped one value wrongly (a policy number). Found by comparing against
  the raw tool output; the run itself was correct.
- The existing event ingest dropped unknown-disposition events before linking, which miscounts an
  event that is both an orphan and has an unknown code. Found by checking the README's order of
  checks against the data (1 of 19 events); fixed so unknown is decided after linking.
- My proposed lead-to-latest-event-type map could not hold per-call facts (connected, telephony
  failed, effective disposition); it was replaced with state keyed by call.

## How I verified AI-generated code
- Ran every function on hand-written cases, including edge cases (leap days, IST date boundaries,
  out-of-order events, override precedence).
- Cross-checked counts against independent computations on the generated data (row totals add up,
  per-call and per-lead totals agree, bucket counts sum to the lead count).
- 60 pytest tests, including: re-ingest changes nothing, ingesting two drops equals ingesting both
  at once byte for byte, the database file contains no phone or name, and the ledger rejects updates.
- Timed the large dataset (about 27 s in total).
