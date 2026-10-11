import json
import random
from pathlib import Path

import pytest

from takehome.db import connect, init_db
from takehome.ingest import ingest_events, ingest_quotes

SEED = Path(__file__).resolve().parent.parent / "seed"
EVENT_COLS = "event_id, type, quote_id, timestamp, channel, direction, source, raw"


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def seed_quotes():
    return json.loads((SEED / "quotes.json").read_text())


def seed_event_lines():
    return (SEED / "events.jsonl").read_text().splitlines()


def stored_events(conn):
    return [tuple(r) for r in conn.execute(f"SELECT {EVENT_COLS} FROM events ORDER BY event_id")]


def quote(**overrides):
    q = {
        "id": "Q-1", "customer_name": "A", "customer_phone": "+19175550001", "tech_name": "T",
        "amount": 100, "status": "open", "created_at": "2026-08-01T00:00:00Z",
        "last_contact_at": "2026-08-05T00:00:00Z",
    }
    return {**q, **overrides}


def event(**overrides):
    e = {"event_id": "e1", "type": "quote_viewed", "quote_id": "Q-1", "timestamp": "2026-08-10T12:00:00Z"}
    return json.dumps({**e, **overrides})


# --- re-runs and ordering ---------------------------------------------------------

def test_reingesting_seed_changes_nothing(conn):
    ingest_quotes(conn, seed_quotes())
    first = ingest_events(conn, seed_event_lines())
    events_after_first = stored_events(conn)
    quotes_after_first = [tuple(r) for r in conn.execute("SELECT * FROM quotes ORDER BY id")]

    again_q = ingest_quotes(conn, seed_quotes())
    again_e = ingest_events(conn, seed_event_lines())

    assert first["counts"]["inserted"] == 82 and first["counts"]["duplicate"] == 6
    assert again_q["counts"]["unchanged"] == 30
    assert again_e["counts"]["inserted"] == 0
    assert stored_events(conn) == events_after_first
    assert [tuple(r) for r in conn.execute("SELECT * FROM quotes ORDER BY id")] == quotes_after_first


def test_event_order_does_not_change_what_is_stored(conn):
    ingest_events(conn, seed_event_lines())

    shuffled = seed_event_lines()
    random.Random(7).shuffle(shuffled)
    other = connect(":memory:")
    init_db(other)
    ingest_events(other, shuffled)

    assert stored_events(other) == stored_events(conn)


# --- duplicates, conflicts, bad input -----------------------------------------------

def test_same_instant_in_another_format_is_a_duplicate(conn):
    report = ingest_events(conn, [
        event(timestamp="2026-08-10T12:00:00Z"),
        event(timestamp="2026-08-10T08:00:00-04:00"),
    ])
    assert report["counts"]["duplicate"] == 1
    assert report["conflicts"] == []


def test_conflicting_event_keeps_first_copy_and_is_reported(conn):
    report = ingest_events(conn, [event(type="quote_viewed"), event(type="quote_accepted")])

    assert report["conflicts"] == [(2, "e1")]
    assert conn.execute("SELECT type FROM events WHERE event_id = 'e1'").fetchone()[0] == "quote_viewed"


def test_bad_lines_are_rejected_without_stopping_the_import(conn):
    report = ingest_events(conn, [
        event(event_id="good1"),
        "{not json",
        event(event_id="no-tz", timestamp="2026-08-10 12:00:00"),
        json.dumps({"event_id": "no-quote", "type": "quote_viewed", "timestamp": "2026-08-10T12:00:00Z"}),
        event(event_id="good2", type="some_future_type", quote_id="Q-unknown"),
    ])

    assert [lineno for lineno, _ in report["rejected"]] == [2, 3, 4]
    assert [r[0] for r in conn.execute("SELECT event_id FROM events ORDER BY event_id")] == ["good1", "good2"]


# --- quote re-import rules ----------------------------------------------------------

@pytest.mark.parametrize("closed", ["accepted", "dismissed"])
def test_closed_quote_never_reopens(conn, closed):
    ingest_quotes(conn, [quote(status=closed)])
    ingest_quotes(conn, [quote(status="open")])

    assert conn.execute("SELECT status FROM quotes").fetchone()[0] == closed


def test_open_quote_can_close(conn):
    ingest_quotes(conn, [quote(status="open")])
    ingest_quotes(conn, [quote(status="dismissed")])

    assert conn.execute("SELECT status FROM quotes").fetchone()[0] == "dismissed"


@pytest.mark.parametrize("incoming", ["2026-08-02T00:00:00Z", None])
def test_last_contact_never_moves_backwards(conn, incoming):
    ingest_quotes(conn, [quote(last_contact_at="2026-08-05T00:00:00Z")])
    ingest_quotes(conn, [quote(last_contact_at=incoming)])

    assert conn.execute("SELECT last_contact_at FROM quotes").fetchone()[0] == "2026-08-05T00:00:00Z"


def test_phone_formats_normalize_to_one_customer(conn):
    ingest_quotes(conn, [
        quote(id="Q-1", customer_phone="+19175550001"),
        quote(id="Q-2", customer_phone="(917) 555-0001"),
        quote(id="Q-3", customer_phone="917.555.0001"),
    ])

    phones = {r[0] for r in conn.execute("SELECT customer_phone FROM quotes")}
    assert phones == {"+19175550001"}
