"""Draft -> approve -> send.

    draft --approve--> approved --send--> sent
      |                            +--blocked or provider error--> failed
      +--discard--> (deleted)

Send re-checks the guardrails inside the same transaction that records the
send, so nothing can change between the check and the write.
"""
import json
from pathlib import Path

from takehome.ingest import insert_event
from takehome.policy import DEFAULT_NOW, Policy, build_followups, format_ts, load_states, parse_ts, send_blocker

TEMPLATES = {
    1: "Hi {first}, it's {tech}. Thanks for your message about your quote. I'll get back to you shortly.",
    2: "Hi {first}, it's {tech}. Just checking in on the quote I sent over. Happy to answer any questions.",
    3: "Hi {first}, it's {tech}. Following up on your quote. Is it still something you're considering? "
       "Happy to adjust scope or timing.",
    4: "Hi {first}, it's {tech}, checking in on your quote. Still interested? Just reply here.",
}


class FakeSmsProvider:
    """Stands in for a real SMS API. Records each message and optionally appends it to a log file."""

    def __init__(self, log_path=None, fail_with=None):
        self.log_path = Path(log_path) if log_path else None
        self.fail_with = fail_with  # set to an error message to simulate a provider rejection
        self.sent = []

    def send(self, to, body, idempotency_key):
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        message_id = f"fake-{len(self.sent) + 1}-{idempotency_key}"
        record = {"id": message_id, "to": to, "body": body, "idempotency_key": idempotency_key}
        self.sent.append(record)
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        return message_id


def _as_dt(now):
    return parse_ts(now) if isinstance(now, str) else now


def idempotency_key(state):
    """Same quote + same last outbound contact = the same follow-up.

    Re-running the engine on unchanged state produces the same key and is skipped.
    Once we send, last outbound moves, so the next quiet period gets its own key.
    """
    anchor = format_ts(state.last_outbound) if state.last_outbound else "none"
    return f"{state.id}:after:{anchor}"


def draft_body(candidate):
    q = candidate.state.quote
    return TEMPLATES[candidate.tier].format(first=q["customer_name"].split()[0], tech=q["tech_name"])


def generate_drafts(conn, now=DEFAULT_NOW, policy=Policy()):
    """Create a draft for each of today's candidates. Safe to run repeatedly."""
    now = _as_dt(now)
    followups = build_followups(conn, now, policy)
    created = skipped = 0
    with conn:
        for c in followups.candidates:
            if c.over_cap:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO outbox (idempotency_key, quote_id, customer_phone, reason, body, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'draft', ?)",
                (idempotency_key(c.state), c.state.id, c.state.phone,
                 f"{c.tier_name}: {c.reason}", draft_body(c), format_ts(now)),
            )
            if cur.rowcount:
                created += 1
            else:
                skipped += 1
    return {"created": created, "skipped": skipped}


def approve(conn, outbox_id, approved_by, now=DEFAULT_NOW):
    """Returns True if a draft was approved; False if the row isn't a draft (already approved, sent, ...)."""
    with conn:
        cur = conn.execute(
            "UPDATE outbox SET status = 'approved', approved_at = ?, approved_by = ? WHERE id = ? AND status = 'draft'",
            (format_ts(_as_dt(now)), approved_by, outbox_id),
        )
    return cur.rowcount == 1


def discard(conn, outbox_id):
    with conn:
        cur = conn.execute("DELETE FROM outbox WHERE id = ? AND status = 'draft'", (outbox_id,))
    return cur.rowcount == 1


def send(conn, outbox_id, provider, now=DEFAULT_NOW, policy=Policy()):
    """Send one approved message. Returns (outcome, detail); outcome is "sent", "blocked", "failed" or "skipped".

    Only rows in status 'approved' are sent, so a second click, a retry, or a
    re-run on a sent row is a no-op. BEGIN IMMEDIATE takes the write lock up
    front, so two concurrent sends of the same row can't both pass the check.
    """
    now = _as_dt(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM outbox WHERE id = ? AND status = 'approved'", (outbox_id,)).fetchone()
        if row is None:
            conn.rollback()
            return "skipped", "Not an approved message (already sent, failed, or never approved)"

        blocker = send_blocker(load_states(conn, now), row["quote_id"], row["customer_phone"], now, policy)
        if blocker:
            conn.execute("UPDATE outbox SET status = 'failed', last_error = ? WHERE id = ?",
                         (f"Blocked: {blocker}", outbox_id))
            conn.commit()
            return "blocked", blocker

        try:
            message_id = provider.send(row["customer_phone"], row["body"], row["idempotency_key"])
        except Exception as e:
            conn.execute("UPDATE outbox SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
                         (f"Provider error: {e}", outbox_id))
            conn.commit()
            return "failed", str(e)

        sent_at = format_ts(now)
        conn.execute(
            "UPDATE outbox SET status = 'sent', sent_at = ?, attempts = attempts + 1, provider_message_id = ? WHERE id = ?",
            (sent_at, message_id, outbox_id),
        )
        # Our own send goes into the event log, so cooldown has one source of truth.
        insert_event(conn, {
            "event_id": f"engine:{row['idempotency_key']}", "type": "message_sent", "quote_id": row["quote_id"],
            "timestamp": sent_at, "channel": row["channel"], "direction": "outbound",
        }, source="engine")
        conn.commit()
        return "sent", message_id
    except Exception:
        conn.rollback()
        raise
