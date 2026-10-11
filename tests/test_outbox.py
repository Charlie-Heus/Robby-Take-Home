import itertools
import json

import pytest

from takehome.db import connect, init_db
from takehome.ingest import ingest_events, ingest_quotes
from takehome.outbox import FakeSmsProvider, approve, discard, generate_drafts, send
from takehome.policy import build_followups

NOW = "2026-08-17T13:00:00Z"
_ids = itertools.count()


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def provider():
    return FakeSmsProvider()


def add_quote(conn, id, phone="+19175550001", status="open", last_contact_at=None):
    ingest_quotes(conn, [{
        "id": id, "customer_name": "Pat Doe", "customer_phone": phone, "tech_name": "Brad", "amount": 1000,
        "status": status, "created_at": "2026-08-10T00:00:00Z", "last_contact_at": last_contact_at,
    }])


def add_event(conn, quote_id, type, timestamp, **extra):
    ingest_events(conn, [json.dumps(
        {"event_id": f"e{next(_ids)}", "type": type, "quote_id": quote_id, "timestamp": timestamp, **extra}
    )])


def outbox(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY id")]


def message_events(conn):
    return conn.execute("SELECT * FROM events WHERE type = 'message_sent'").fetchall()


def approved_draft(conn, quote_id="Q-1", **quote_kw):
    add_quote(conn, quote_id, **quote_kw)
    generate_drafts(conn, NOW)
    row = outbox(conn)[-1]
    assert approve(conn, row["id"], "owner", NOW)
    return row["id"]


# --- re-running never duplicates --------------------------------------------------------

def test_generating_drafts_twice_creates_them_once(conn):
    add_quote(conn, "Q-1", phone="+19175550001")
    add_quote(conn, "Q-2", phone="+19175550002")

    assert generate_drafts(conn, NOW) == {"created": 2, "skipped": 0}
    assert generate_drafts(conn, NOW) == {"created": 0, "skipped": 0}  # already in outbox -> not candidates
    assert len(outbox(conn)) == 2


def test_generating_again_does_not_refill_the_daily_cap(conn):
    for i in range(15):
        add_quote(conn, f"Q-{i}", phone=f"+1917555{i:04d}")

    assert generate_drafts(conn, NOW)["created"] == 10
    assert generate_drafts(conn, NOW)["created"] == 0                     # same day: cap used up
    assert generate_drafts(conn, "2026-08-18T13:00:00Z")["created"] == 10  # next day: unsent ones roll over


def test_sending_twice_sends_once(conn, provider):
    oid = approved_draft(conn)

    assert send(conn, oid, provider, NOW)[0] == "sent"
    assert send(conn, oid, provider, NOW)[0] == "skipped"
    assert len(provider.sent) == 1
    assert len(message_events(conn)) == 1


def test_after_sending_engine_does_not_draft_same_customer_again(conn, provider):
    oid = approved_draft(conn)
    send(conn, oid, provider, NOW)

    assert generate_drafts(conn, NOW)["created"] == 0               # same moment: cooldown
    assert generate_drafts(conn, "2026-08-18T13:00:00Z")["created"] == 0  # a day later: still cooldown


def test_unapproved_draft_cannot_be_sent(conn, provider):
    add_quote(conn, "Q-1")
    generate_drafts(conn, NOW)

    assert send(conn, outbox(conn)[0]["id"], provider, NOW)[0] == "skipped"
    assert provider.sent == []


# --- guardrails re-checked at send time ------------------------------------------------

def test_send_blocked_if_quote_accepted_after_approval(conn, provider):
    oid = approved_draft(conn)
    add_event(conn, "Q-1", "quote_accepted", "2026-08-17T12:30:00Z")

    outcome, detail = send(conn, oid, provider, NOW)

    assert outcome == "blocked" and "accepted" in detail
    assert provider.sent == []
    assert message_events(conn) == []
    assert outbox(conn)[0]["status"] == "blocked"


def test_send_blocked_if_customer_contacted_about_another_quote(conn, provider):
    oid = approved_draft(conn, "Q-1", phone="+19175550001")
    add_quote(conn, "Q-2", phone="+19175550001")
    add_event(conn, "Q-2", "message_sent", "2026-08-17T12:00:00Z", direction="outbound")  # e.g. tech texted by hand

    outcome, detail = send(conn, oid, provider, NOW)

    assert outcome == "blocked" and "about Q-2" in detail
    assert provider.sent == []


def test_blocked_row_frees_customer_for_future_drafts(conn, provider):
    oid = approved_draft(conn)
    add_event(conn, "Q-1", "message_sent", "2026-08-17T12:00:00Z", direction="outbound")
    send(conn, oid, provider, NOW)

    # The failed row no longer holds the one-active-message-per-phone slot.
    add_quote(conn, "Q-2", phone="+19175550001")
    assert generate_drafts(conn, "2026-08-21T13:00:00Z")["created"] == 1


def test_provider_error_marks_failed_without_recording_a_send(conn):
    oid = approved_draft(conn)

    outcome, _ = send(conn, oid, FakeSmsProvider(fail_with="carrier rejected"), NOW)

    assert outcome == "failed"
    assert message_events(conn) == []
    assert "carrier rejected" in outbox(conn)[0]["last_error"]


def test_discard_only_removes_drafts(conn, provider):
    oid = approved_draft(conn)
    assert not discard(conn, oid)  # approved, not a draft
    assert len(outbox(conn)) == 1


# --- retrying a failed send ---------------------------------------------------------

def test_retry_after_provider_error_sends_once(conn):
    oid = approved_draft(conn)
    provider = FakeSmsProvider(fail_with="carrier unavailable")
    assert send(conn, oid, provider, NOW)[0] == "failed"

    provider.fail_with = None
    assert send(conn, oid, provider, NOW)[0] == "sent"
    assert send(conn, oid, provider, NOW)[0] == "skipped"  # retrying a sent row does nothing

    assert len(provider.sent) == 1
    assert len(message_events(conn)) == 1
    assert outbox(conn)[0]["attempts"] == 2


def test_retry_after_send_that_actually_went_out_does_not_double_send(conn):
    # The provider delivered the text but the response timed out, so we recorded "failed".
    oid = approved_draft(conn)
    provider = FakeSmsProvider(fail_after_send="response timed out")
    assert send(conn, oid, provider, NOW)[0] == "failed"
    assert len(provider.sent) == 1

    provider.fail_after_send = None
    assert send(conn, oid, provider, NOW)[0] == "sent"

    assert len(provider.sent) == 1  # same idempotency key: provider didn't send again
    assert len(message_events(conn)) == 1


def test_blocked_send_cannot_be_retried(conn, provider):
    oid = approved_draft(conn)
    add_event(conn, "Q-1", "quote_accepted", "2026-08-17T12:30:00Z")
    assert send(conn, oid, provider, NOW)[0] == "blocked"

    assert send(conn, oid, provider, NOW)[0] == "skipped"
    assert provider.sent == []


def test_retry_rechecks_guardrails(conn):
    oid = approved_draft(conn)
    provider = FakeSmsProvider(fail_with="carrier unavailable")
    send(conn, oid, provider, NOW)
    add_event(conn, "Q-1", "quote_accepted", "2026-08-17T12:30:00Z")  # accepted before the retry

    provider.fail_with = None
    assert send(conn, oid, provider, NOW)[0] == "blocked"
    assert provider.sent == []


# --- day rollover ---------------------------------------------------------------------

TOMORROW = "2026-08-18T13:00:00Z"


def test_unsent_customers_roll_over_and_old_rows_expire(conn, provider):
    for i in range(4):
        add_quote(conn, f"Q-{i}", phone=f"+1917555{i:04d}")
    generate_drafts(conn, NOW)
    rows = outbox(conn)
    for r in rows[:2]:  # only message two of the four today
        approve(conn, r["id"], "owner", NOW)
        send(conn, r["id"], provider, NOW)

    f = build_followups(conn, TOMORROW)
    assert {c.state.id for c in f.candidates} == {rows[2]["quote_id"], rows[3]["quote_id"]}  # rolled over

    assert generate_drafts(conn, TOMORROW)["created"] == 2
    by_status = {}
    for r in outbox(conn):
        by_status.setdefault(r["status"], []).append(r["created_at"][:10])
    assert by_status == {"sent": ["2026-08-17"] * 2, "cancelled": ["2026-08-17"] * 2, "draft": ["2026-08-18"] * 2}


def test_yesterdays_approved_message_cannot_be_sent_today(conn, provider):
    oid = approved_draft(conn)

    outcome, _ = send(conn, oid, provider, TOMORROW)

    assert outcome == "skipped"
    assert provider.sent == []
    assert outbox(conn)[0]["status"] == "cancelled"


def test_rollover_still_never_double_sends(conn, provider):
    # Customer is messaged today; tomorrow they're in cooldown, so no new draft.
    oid = approved_draft(conn)
    send(conn, oid, provider, NOW)

    assert generate_drafts(conn, TOMORROW)["created"] == 0
    assert len(provider.sent) == 1


def test_timeout_then_rollover_does_not_double_send(conn):
    # Day 1: the text goes out but the provider times out, so we record "failed" and
    # no message_sent event. Nobody clicks Retry. Day 2: the row expires and the customer
    # rolls over. The new draft must reuse the old key, so the provider drops the repeat.
    oid = approved_draft(conn)
    provider = FakeSmsProvider(fail_after_send="response timed out")
    assert send(conn, oid, provider, NOW)[0] == "failed"
    provider.fail_after_send = None

    assert generate_drafts(conn, TOMORROW)["created"] == 1
    new_id = outbox(conn)[-1]["id"]
    approve(conn, new_id, "owner", TOMORROW)
    assert send(conn, new_id, provider, TOMORROW)[0] == "sent"

    assert len(provider.sent) == 1  # one text, not two
    assert len(message_events(conn)) == 1  # and cooldown now knows about it
