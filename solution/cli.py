"""Command line entry point.

    python solution/cli.py ingest data/drop_1
    python solution/cli.py ingest data/drop_2
    python solution/cli.py report --out reports/

`ingest` also accepts a directory of drops (ingested in natural name order).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from db import open_db
from export import write_reports
from ingest import discover_drops, ingest_drop

DEFAULT_SCHEMAS = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="Interaction ledger pipeline")
    parser.add_argument("--db", default="pipeline.db", help="SQLite file (default: pipeline.db)")
    parser.add_argument("--schemas", default=str(DEFAULT_SCHEMAS), help="directory with workflow schemas and dispositions.csv")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="ingest a drop directory")
    ingest.add_argument("drop")
    report = sub.add_parser("report", help="write the five report files")
    report.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    conn, protector = open_db(args.db)
    try:
        if args.command == "ingest":
            drops = discover_drops(args.drop)
            if not drops:
                print(f"no drops found in {args.drop}", file=sys.stderr)
                return 1
            for drop in drops:
                s = ingest_drop(conn, protector, drop, args.schemas)
                if s.skipped:
                    print(f"{s.drop}: already ingested, nothing changed")
                else:
                    print(
                        f"{s.drop}: {s.lead_rows} lead rows ({s.rejected} rejected, {s.leads_created} new leads, "
                        f"{s.leads_merged_into_existing} merged into earlier leads); "
                        f"{s.events_read} event lines ({s.events_stored} stored)"
                    )
        else:
            write_reports(conn, args.schemas, args.out)
            print(f"reports written to {args.out}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
