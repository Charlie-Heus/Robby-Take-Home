"""Load quotes and events into SQLite.

Ingest only stores data. It never derives quote state, so out-of-order and
late events need no special handling here: state is computed from timestamps
at read time.
"""
import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from takehome.db import DEFAULT_DB, connect, init_db

ROOT = Path(__file__).resolve().parent.parent
CLOSED = ("accepted", "dismissed")
QUOTE_STATUSES = ("open",) + CLOSED
EVENT_FIELDS = ("type", "quote_id", "timestamp", "channel", "direction")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ts(value):
    """Return a canonical "YYYY-MM-DDTHH:MM:SSZ" UTC string.

    The schema relies on timestamps sorting lexically, so every stored value must
    share one format. Naive timestamps are rejected rather than guessed at.
    """
    if not isinstance(value, str):
        raise ValueError(f"timestamp is not a string: {value!r}")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"unparseable timestamp: {value!r}") from None
    if dt.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_phone(value):
    """Return a US number as +1XXXXXXXXXX. Cooldown keys on this, so formats must match."""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError(f"unrecognized phone number: {value!r}")
    return "+" + digits


def _required_str(record, field):
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or empty {field}")
    return value.strip()


# --- events ---------------------------------------------------------------------

def insert_event(conn, event, source="webhook"):
    """Store one event. Returns "inserted", "duplicate" or "conflict"; raises ValueError if invalid.

    Webhook events and the engine's own message_sent writes both go through here.
    A repeated event_id is ignored. If its content differs from the stored copy,
    the first copy is kept and the caller is told, so a person can look at it.
    """
    if not isinstance(event, dict):
        raise ValueError("event is not a JSON object")
    row = {
        "event_id": _required_str(event, "event_id"),
        "type": _required_str(event, "type"),
        "quote_id": _required_str(event, "quote_id"),
        "timestamp": normalize_ts(event.get("timestamp")),
        "channel": event.get("channel"),
        "direction": event.get("direction"),
    }

    cur = conn.execute(
        "INSERT OR IGNORE INTO events (event_id, type, quote_id, timestamp, channel, direction, source, raw, ingested_at)"
        " VALUES (:event_id, :type, :quote_id, :timestamp, :channel, :direction, :source, :raw, :ingested_at)",
        {**row, "source": source, "raw": json.dumps(event, sort_keys=True), "ingested_at": utc_now()},
    )
    if cur.rowcount == 1:
        return "inserted"

    stored = conn.execute(
        f"SELECT {', '.join(EVENT_FIELDS)} FROM events WHERE event_id = ?", (row["event_id"],)
    ).fetchone()
    same = all(stored[f] == row[f] for f in EVENT_FIELDS)
    return "duplicate" if same else "conflict"


def ingest_events(conn, lines):
    report = {"counts": Counter(), "rejected": [], "conflicts": []}
    with conn:
        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            report["counts"]["read"] += 1
            try:
                event = json.loads(line)
                outcome = insert_event(conn, event)
            except (json.JSONDecodeError, ValueError) as e:
                report["counts"]["rejected"] += 1
                report["rejected"].append((lineno, str(e)))
                continue
            report["counts"][outcome] += 1
            if outcome == "conflict":
                report["conflicts"].append((lineno, event["event_id"]))
    return report


# --- quotes ---------------------------------------------------------------------

def _clean_quote(q):
    if not isinstance(q, dict):
        raise ValueError("quote is not a JSON object")
    status = _required_str(q, "status")
    if status not in QUOTE_STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    amount = q.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
        raise ValueError(f"bad amount: {amount!r}")
    last_contact = q.get("last_contact_at")
    return {
        "id": _required_str(q, "id"),
        "customer_name": _required_str(q, "customer_name"),
        "customer_phone": normalize_phone(_required_str(q, "customer_phone")),
        "tech_name": _required_str(q, "tech_name"),
        "amount": int(amount),
        "status": status,
        "created_at": normalize_ts(q.get("created_at")),
        "last_contact_at": normalize_ts(last_contact) if last_contact is not None else None,
    }


def upsert_quote(conn, q):
    """Insert or update one quote snapshot. Returns "inserted", "updated" or "unchanged".

    The newest snapshot wins, with two exceptions:
    - a closed status (accepted/dismissed) is never replaced, so a stale export
      can't make a closed quote eligible for messages again;
    - last_contact_at keeps the later value, so it never moves backwards.
    """
    new = _clean_quote(q)
    old = conn.execute("SELECT * FROM quotes WHERE id = ?", (new["id"],)).fetchone()
    cols = list(new)

    if old is None:
        conn.execute(
            f"INSERT INTO quotes ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", new
        )
        return "inserted"

    merged = dict(new)
    if old["status"] in CLOSED:
        merged["status"] = old["status"]
    merged["last_contact_at"] = max(
        (t for t in (old["last_contact_at"], new["last_contact_at"]) if t), default=None
    )
    if all(old[c] == merged[c] for c in cols):
        return "unchanged"
    conn.execute(f"UPDATE quotes SET {', '.join(f'{c} = :{c}' for c in cols)} WHERE id = :id", merged)
    return "updated"


def ingest_quotes(conn, quotes):
    report = {"counts": Counter(), "rejected": []}
    if not isinstance(quotes, list):
        raise ValueError("quotes file must contain a JSON array")
    with conn:
        for i, q in enumerate(quotes):
            report["counts"]["read"] += 1
            try:
                outcome = upsert_quote(conn, q)
            except ValueError as e:
                report["counts"]["rejected"] += 1
                report["rejected"].append((q.get("id", f"#{i}") if isinstance(q, dict) else f"#{i}", str(e)))
                continue
            report["counts"][outcome] += 1
    return report


# --- CLI ------------------------------------------------------------------------

def _print_report(name, report):
    counts = ", ".join(f"{k}={v}" for k, v in report["counts"].items())
    print(f"{name}: {counts}")
    for where, why in report["rejected"]:
        print(f"  rejected {where}: {why}")
    for lineno, event_id in report.get("conflicts", []):
        print(f"  conflict line {lineno}: event_id {event_id} differs from stored copy; kept the first")


def main(argv=None):
    p = argparse.ArgumentParser(description="Load quotes and events into SQLite.")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--quotes", default=ROOT / "seed" / "quotes.json")
    p.add_argument("--events", default=ROOT / "seed" / "events.jsonl")
    args = p.parse_args(argv)

    conn = connect(args.db)
    init_db(conn)
    # Quotes first: outbox rows reference quotes. Events don't, so their order doesn't matter.
    _print_report("quotes", ingest_quotes(conn, json.loads(Path(args.quotes).read_text())))
    with open(args.events) as f:
        _print_report("events", ingest_events(conn, f))
    conn.close()


if __name__ == "__main__":
    main()
