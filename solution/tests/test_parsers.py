import pytest

from read_leads import (
    normalize_date, normalize_decimal, normalize_enum, normalize_header, normalize_phone,
    rejection_reason,
)


@pytest.mark.parametrize("raw, expected", [
    ("9876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("91-77187-50036", "+917718750036"),
    ("098765 43210", "+919876543210"),        # 11 digits starting with 0
    ("(98765) 43210", "+919876543210"),
    ("5876543210", None),                      # must start with 6-9
    ("98765", None),
    ("+9198765432101", None),                  # 13 digits
    ("0919876543210", None),                   # 13 digits
    ("+1 415 555 0134", None),
    ("abc", None),
])
def test_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),              # day-first
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),             # month name is case-insensitive
    ("05/06/2026", "2026-06-05"),              # 5 June, not 6 May
    ("29/02/2028", "2028-02-29"),              # leap year
    ("31/02/2026", None),                      # impossible dates
    ("29/02/2027", None),
    ("2026-13-01", None),
    ("01/12-2026", None),                      # mixed separators
    ("soon", None),
])
def test_date(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("27554", "27554.00"),
    ("₹27,554", "27554.00"),
    ("Rs. 500", "500.00"),
    ("rs500", "500.00"),
    ("INR 25301.4", "25301.40"),
    ("0.005", "0.01"),                         # rounds half up to 2 places
    ("-5", None),                              # must be >= 0
    ("N/A", None),
    ("twelve thousand", None),
    ("₹", None),
])
def test_decimal(raw, expected):
    got = normalize_decimal(raw)
    assert (str(got) if got is not None else None) == expected


def test_enum_is_case_insensitive_and_stored_lowercase():
    allowed = ["en", "hi", "ta"]
    assert normalize_enum("HI", allowed) == "hi"
    assert normalize_enum("Hindi", allowed) is None


def test_header_normalisation():
    assert normalize_header("  Mobile_No ") == "mobile no"
    assert normalize_header("POLICY__NO") == "policy no"
    assert normalize_header("Order\tID") == "order id"


SCHEMA = {"fields": [
    {"name": "customer_name", "type": "string", "required": True},
    {"name": "phone", "type": "phone", "required": True},
    {"name": "policy_number", "type": "string", "required": True},
    {"name": "policy_expiry", "type": "date", "required": False},
]}


@pytest.mark.parametrize("fields, expected", [
    ({"customer_name": "A", "phone": "9876543210", "policy_number": "P"}, None),
    ({"customer_name": "", "phone": "9876543210", "policy_number": "P"}, "missing_required:customer_name"),
    ({"customer_name": "A", "phone": "", "policy_number": "P"}, "missing_required:phone"),
    ({"customer_name": "A", "phone": "12345", "policy_number": "P"}, "invalid_phone"),
    # first failure in schema order wins: name before phone, phone before policy_number
    ({"customer_name": "", "phone": "12345", "policy_number": ""}, "missing_required:customer_name"),
    ({"customer_name": "A", "phone": "12345", "policy_number": ""}, "invalid_phone"),
    # a bad optional value is not a rejection
    ({"customer_name": "A", "phone": "9876543210", "policy_number": "P", "policy_expiry": "soon"}, None),
])
def test_rejection_reason(fields, expected):
    assert rejection_reason(fields, SCHEMA) == expected
