"""Read a lead CSV file into raw rows. No header mapping or validation here."""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Iterator

_SEPARATORS = re.compile(r"[_\s]+")


def normalize_header(header: str) -> str:
    """Lower-case, trim, and collapse every run of '_' or whitespace to one space."""
    return _SEPARATORS.sub(" ", header.lower().strip()).strip()


def normalize_phone(value: str) -> str | None:
    """Return the canonical '+91XXXXXXXXXX', or None if not a valid Indian mobile."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_DATE = re.compile(r"^(\d{1,2})([/-])(\d{1,2})\2(\d{4})$")  # both separators must match
_DMON_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")


def normalize_date(value: str) -> str | None:
    """Return 'YYYY-MM-DD', or None if invalid.

    Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY and DD-Mon-YYYY (month name
    case-insensitive). Day-first; impossible dates (31/02) are invalid.
    """
    if m := _ISO_DATE.match(value):
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    elif m := _DMY_DATE.match(value):
        day, month, year = int(m[1]), int(m[3]), int(m[4])
    elif m := _DMON_DATE.match(value):
        month = _MONTHS.get(m[2].lower())
        if month is None:
            return None
        day, year = int(m[1]), int(m[3])
    else:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


_DECIMAL = re.compile(r"^(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)$", re.IGNORECASE)


def normalize_decimal(value: str) -> Decimal | None:
    """Return the amount rounded to 2 places, or None if invalid.

    Optional prefix ₹ / Rs / Rs. / INR (case-insensitive, optional space);
    commas are thousands separators; negative values are invalid.
    """
    m = _DECIMAL.match(value)
    if not m:
        return None
    return Decimal(m[1].replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_enum(value: str, allowed: list[str]) -> str | None:
    """Return the lower-cased value if it matches `allowed` case-insensitively, else None."""
    lowered = value.lower()
    return lowered if lowered in {a.lower() for a in allowed} else None


def load_schema(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_header_map(schema: dict) -> dict[str, str]:
    """Map normalised header text -> schema field name (via name or aliases)."""
    header_map: dict[str, str] = {}
    for f in schema["fields"]:
        for key in [f["name"], *f.get("aliases", [])]:
            header_map.setdefault(normalize_header(key), f["name"])
    return header_map


def map_row_to_fields(row: dict[str, str], header_map: dict[str, str]) -> dict[str, str]:
    """Rename a row's normalised headers to schema field names. Unmatched columns are
    ignored; if two columns hit the same field, the leftmost wins."""
    fields: dict[str, str] = {}
    for header, value in row.items():
        name = header_map.get(header)
        if name is not None and name not in fields:
            fields[name] = value
    return fields


def rejection_reason(fields: dict[str, str], schema: dict) -> str | None:
    """Walk the schema fields in order; the first failure is the row's single reason.

    - required field missing (empty)      -> 'missing_required:<field_name>'
    - phone field present but invalid     -> 'invalid_phone'
    Returns None if the row is accepted.
    """
    for f in schema["fields"]:
        value = fields.get(f["name"], "")
        if not value:
            if f.get("required"):
                return f"missing_required:{f['name']}"
            continue
        if f["type"] == "phone" and normalize_phone(value) is None:
            return "invalid_phone"
    return None


def normalize_fields(fields: dict[str, str], schema: dict) -> tuple[dict[str, object | None], int]:
    """Normalise an accepted row's values; return (values, field_warning_count).

    - empty value                       -> None, no warning (missing optional field)
    - present but invalid               -> None, one warning per field
    Only optional fields can warn. Required fields are already checked by
    rejection_reason (missing -> reject), so they are never counted here.
    Call only for rows that were not rejected, so warnings never count rejected rows.
    """
    values: dict[str, object | None] = {}
    warnings = 0
    for f in schema["fields"]:
        raw = fields.get(f["name"], "")
        if not raw:
            values[f["name"]] = None
            continue
        kind = f["type"]
        if kind == "phone":
            value = normalize_phone(raw)
        elif kind == "date":
            value = normalize_date(raw)
        elif kind == "decimal":
            value = normalize_decimal(raw)
        elif kind == "enum":
            value = normalize_enum(raw, f["values"])
        else:  # string: any non-empty text is valid
            value = raw
        if value is None and not f.get("required"):  # warnings are for optional fields only
            warnings += 1
        values[f["name"]] = value
    return values, warnings


def apply_defaults(values: dict[str, object | None], schema: dict) -> dict[str, object | None]:
    """Fill fields that are still None with the schema's `default`, if it has one.

    Call on a lead's final values AFTER all its duplicate rows are merged, so a default
    never stands in for a real value that a later row could still supply.
    """
    result = dict(values)
    for f in schema["fields"]:
        if result.get(f["name"]) is None and "default" in f:
            result[f["name"]] = f["default"]
    return result


def read_lead_file(path: str | Path) -> Iterator[tuple[int, dict[str, str]]]:
    """Yield (row_number, row) for every non-blank data row.

    - row is keyed by the normalised header text; values are trimmed.
      An empty string means the value is missing.
    - Blank rows (every cell empty/whitespace) are skipped and not counted.
    - row_number counts non-blank data rows starting at 1 (header excluded).
    """
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        raw_headers = next(reader, None)
        if raw_headers is None:
            return
        headers = [normalize_header(h) for h in raw_headers]

        row_number = 0
        for cells in reader:
            if all(not c.strip() for c in cells):
                continue
            row_number += 1
            yield row_number, {h: c.strip() for h, c in zip(headers, cells)}
