from conftest import SCHEMAS_DIR
from derive import (
    COMPLETED, IN_PROGRESS_DIALED, IN_PROGRESS_TELEPHONY_FAILED, YET_TO_REACH_OUT,
    Call, build_calls, lead_bucket,
)
from ingest_events import LinkedEvent
from read_events import load_dispositions, parse_timestamp as ts
from reports import daily_overview_rows

W = "motor_renewal"
LEAD = (W, "+919876543210")
CATALOGUE = load_dispositions(SCHEMAS_DIR / "dispositions.csv")


def ev(event_id, event_type, call_id, when, **payload):
    return LinkedEvent(LEAD, {"event_id": event_id, "event_type": event_type, "workflow_id": W,
                              "call_id": call_id, "occurred_at": when, "payload": payload})


def effective(*events):
    return build_calls(list(events))[(W, "c1")].effective_disposition


def test_override_wins_even_when_older_than_the_set():
    assert effective(
        ev("1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("2", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="renewed"),
        ev("3", "disposition.override", "c1", "2026-10-01T09:00:00Z", disposition_code="wrong_number"),
    ) == "wrong_number"


def test_latest_override_wins():
    assert effective(
        ev("1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("2", "disposition.override", "c1", "2026-10-02T10:00:00Z", disposition_code="dnd"),
        ev("3", "disposition.override", "c1", "2026-10-01T10:00:00Z", disposition_code="renewed"),
    ) == "dnd"


def test_latest_set_is_by_occurred_at_not_arrival_order():
    assert effective(
        ev("1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("2", "disposition.set", "c1", "2026-10-01T10:09:00Z", disposition_code="callback_requested"),
        ev("3", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="renewed"),
    ) == "callback_requested"


def test_no_disposition_means_none():
    assert effective(ev("1", "call.initiated", "c1", "2026-10-01T10:00:00Z")) is None


def test_event_before_its_initiated_event_still_attaches():
    assert effective(
        ev("2", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="renewed"),
        ev("1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
    ) == "renewed"


def test_call_without_initiated_event_is_not_counted():
    assert build_calls([ev("1", "disposition.set", "c9", "2026-10-01T10:05:00Z", disposition_code="renewed")]) == {}


def test_connected_survives_a_later_call_ended():
    calls = build_calls([
        ev("1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("2", "call.connected", "c1", "2026-10-01T10:00:05Z"),
        ev("3", "call.ended", "c1", "2026-10-01T10:01:00Z"),
    ])
    assert calls[(W, "c1")].connected


def call(**kwargs):
    return Call(W, "x", LEAD, ts("2026-10-01T10:00:00Z"), **kwargs)


def test_bucket_order_and_rules():
    assert lead_bucket([], CATALOGUE) == YET_TO_REACH_OUT
    # terminal beats telephony_failed
    assert lead_bucket([call(telephony_failed=True, latest_set=(0, "dnd"))], CATALOGUE) == COMPLETED
    # a non-terminal disposition is not completed
    assert lead_bucket([call(latest_set=(0, "callback_requested"))], CATALOGUE) == IN_PROGRESS_DIALED
    # every call telephony-failed
    assert lead_bucket([call(telephony_failed=True), call(telephony_failed=True)], CATALOGUE) == IN_PROGRESS_TELEPHONY_FAILED
    # one failed, one plain: not "every call"
    assert lead_bucket([call(telephony_failed=True), call()], CATALOGUE) == IN_PROGRESS_DIALED
    # an override can turn a terminal call non-terminal, and the other way round
    assert lead_bucket([call(latest_set=(0, "renewed"), latest_override=(0, "callback_requested"))], CATALOGUE) == IN_PROGRESS_DIALED
    assert lead_bucket([call(latest_set=(0, "callback_requested"), latest_override=(0, "not_interested"))], CATALOGUE) == COMPLETED


def test_daily_overview_uses_ist_dates_and_distinct_success_leads():
    def c(call_id, lead, when, **kw):
        return (W, call_id), Call(W, call_id, (W, lead), ts(when), **kw)
    calls = dict([
        c("a", "L1", "2026-10-01T18:29:59Z", connected=True, latest_set=(0, "renewed")),  # 23:59:59 IST, 1 Oct
        c("b", "L1", "2026-10-01T18:30:00Z", connected=True, latest_set=(0, "renewed")),  # 00:00:00 IST, 2 Oct
        c("c", "L2", "2026-10-02T05:00:00Z", latest_set=(0, "renewed")),
        c("d", "L2", "2026-10-02T06:00:00Z", latest_set=(0, "renewed")),                  # same lead, counted once
        c("e", "L3", "2026-10-02T07:00:00Z", latest_set=(0, "renewed"), latest_override=(0, "not_interested")),
    ])
    assert daily_overview_rows(calls, CATALOGUE) == [
        [W, "2026-10-01", "1", "1", "1.0000", "1"],
        [W, "2026-10-02", "4", "1", "0.2500", "2"],
    ]
