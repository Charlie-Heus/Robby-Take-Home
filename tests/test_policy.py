import itertools
import json
from pathlib import Path

import pytest

from takehome.db import connect, init_db
from takehome.ingest import ingest_events, ingest_quotes
from takehome.policy import build_followups

SEED = Path(__file__).resolve().parent.parent / "seed"
NOW = "2026-08-17T13:00:00Z"
_ids = itertools.count()


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def add_quote(conn, id, phone="+19175550001", amount=1000, status="open",
              created_at="2026-08-10T00:00:00Z", last_contact_at=None):
    ingest_quotes(conn, [{
        "id": id, "customer_name": "A", "customer_phone": phone, "tech_name": "T", "amount": amount,
        "status": status, "created_at": created_at, "last_contact_at": last_contact_at,
    }])


def add_event(conn, quote_id, type, timestamp, **extra):
    ingest_events(conn, [json.dumps(
        {"event_id": f"e{next(_ids)}", "type": type, "quote_id": quote_id, "timestamp": timestamp, **extra}
    )])


def result(conn, now=NOW):
    f = build_followups(conn, now)
    return {c.state.id: c for c in f.candidates}, {e.state.id: e.reason for e in f.excluded}


# --- hard filters ------------------------------------------------------------------

def test_closed_quotes_are_never_candidates(conn):
    add_quote(conn, "Q-snap-accepted", phone="+19175550001", status="accepted")
    add_quote(conn, "Q-snap-dismissed", phone="+19175550002", status="dismissed")
    add_quote(conn, "Q-event-accepted", phone="+19175550003")  # snapshot says open...
    add_event(conn, "Q-event-accepted", "quote_accepted", "2026-08-12T00:00:00Z")  # ...but it was accepted
    add_event(conn, "Q-event-accepted", "quote_viewed", "2026-08-16T00:00:00Z")    # a later view doesn't reopen it

    candidates, excluded = result(conn)
    assert candidates == {}
    assert set(excluded) == {"Q-snap-accepted", "Q-snap-dismissed", "Q-event-accepted"}


def test_cooldown_is_per_phone_across_quotes(conn):
    add_quote(conn, "Q-A", phone="+19175550001")
    add_quote(conn, "Q-B", phone="+19175550001")
    add_quote(conn, "Q-other", phone="+19175550002")
    add_event(conn, "Q-A", "message_sent", "2026-08-16T13:00:00Z", direction="outbound")

    candidates, excluded = result(conn)
    assert "Q-A" in excluded and "Q-B" in excluded  # texting about Q-A blocks Q-B
    assert "about Q-A" in excluded["Q-B"]
    assert "Q-other" in candidates


def test_cooldown_boundary(conn):
    add_quote(conn, "Q-1")
    add_event(conn, "Q-1", "message_sent", "2026-08-14T13:00:00Z", direction="outbound")

    assert "Q-1" in result(conn, "2026-08-17T12:59:59Z")[1]  # 1s short of 72h: blocked
    assert "Q-1" in result(conn, "2026-08-17T13:00:00Z")[0]  # exactly 72h: allowed


def test_message_event_counts_even_when_snapshot_says_never_contacted(conn):
    # Seed case Q-1005: last_contact_at is null but a message_sent event exists.
    add_quote(conn, "Q-1", last_contact_at=None)
    add_event(conn, "Q-1", "message_sent", "2026-08-16T00:00:00Z", direction="outbound")

    assert "cooldown" in result(conn)[1]["Q-1"]


def test_snapshot_last_contact_counts_toward_cooldown(conn):
    add_quote(conn, "Q-1", last_contact_at="2026-08-16T00:00:00Z")  # no matching event
    assert "cooldown" in result(conn)[1]["Q-1"]


def test_only_one_candidate_per_phone(conn):
    add_quote(conn, "Q-small", phone="+19175550001", amount=500)
    add_quote(conn, "Q-big", phone="+19175550001", amount=9000)

    candidates, excluded = result(conn)
    assert list(candidates) == ["Q-big"]
    assert "Q-big" in excluded["Q-small"]


def test_quote_with_message_in_outbox_is_excluded(conn):
    add_quote(conn, "Q-1")
    add_quote(conn, "Q-2")  # same phone
    conn.execute(
        "INSERT INTO outbox (idempotency_key, quote_id, customer_phone, reason, body, status, created_at)"
        " VALUES ('k', 'Q-1', '+19175550001', 'r', 'b', 'approved', ?)", (NOW,))

    _, excluded = result(conn)
    assert "already in the outbox" in excluded["Q-1"]
    assert "already in the outbox" in excluded["Q-2"]


# --- time travel ---------------------------------------------------------------------

def test_events_after_now_are_ignored(conn):
    add_quote(conn, "Q-1", last_contact_at="2026-08-16T12:00:00Z")
    add_event(conn, "Q-1", "quote_accepted", "2026-08-16T00:00:00Z")

    # As of Aug 15, the acceptance and the contact haven't happened yet.
    candidates, _ = result(conn, "2026-08-15T00:00:00Z")
    assert "Q-1" in candidates
    # As of Aug 17, they have.
    assert result(conn)[1]["Q-1"] == "Quote is accepted"


# --- tiers ---------------------------------------------------------------------------

def test_unanswered_reply_outranks_everything(conn):
    add_quote(conn, "Q-replied", phone="+19175550001", amount=400, last_contact_at="2026-08-11T00:00:00Z")
    add_event(conn, "Q-replied", "customer_replied", "2026-08-12T00:00:00Z", channel="sms")
    add_quote(conn, "Q-hot-big", phone="+19175550002", amount=22000)
    add_event(conn, "Q-hot-big", "quote_viewed", "2026-08-17T10:00:00Z")

    f = build_followups(conn, NOW)
    assert [(c.state.id, c.tier) for c in f.candidates] == [("Q-replied", 1), ("Q-hot-big", 2)]


def test_reply_followed_by_our_message_is_answered(conn):
    add_quote(conn, "Q-1")
    add_event(conn, "Q-1", "customer_replied", "2026-08-12T00:00:00Z", channel="sms")
    add_event(conn, "Q-1", "message_sent", "2026-08-12T03:00:00Z", direction="outbound")

    assert result(conn)[0]["Q-1"].tier != 1


def test_seed_ranking_as_of_aug_17(conn):
    ingest_quotes(conn, json.loads((SEED / "quotes.json").read_text()))
    ingest_events(conn, (SEED / "events.jsonl").read_text().splitlines())
    f = build_followups(conn, NOW)

    ids = [c.state.id for c in f.candidates]
    assert ids[:3] == ["Q-1016", "Q-1007", "Q-1015"]          # unanswered replies first
    assert {"Q-1004", "Q-1009", "Q-1017"}.isdisjoint(ids)     # closed
    assert "Q-1019" not in ids                                # in cooldown until 14:00
    assert not ({"Q-1003", "Q-1025"} <= set(ids))             # Karen gets one slot, not two
    assert sum(not c.over_cap for c in f.candidates) == 10

