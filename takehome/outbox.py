"""Draft -> approve -> send.

    draft --approve--> approved --send--> sent
      |                     |       +--guardrail--> blocked (final)
      |                     |       +--provider error--> failed --retry (send again)--> sent
      +--discard--> (deleted)

    draft / approved / failed rows left unsent at the end of their day --> cancelled

Send re-checks the guardrails inside the same transaction that records the
send, so nothing can change between the check and the write. A retry is the
same send on the same row with the same idempotency key.
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
    """Stands in for a real SMS API that supports idempotency keys.

    Each key is delivered at most once: a repeat returns the original message id
    without sending again. That is what makes retrying safe even when a previous
    attempt actually went out (e.g. the response timed out). Sent messages are
    optionally appended to a log file, which is also reloaded on startup.
    """

    def __init__(self, log_path=None, fail_with=None, fail_after_send=None):
        self.log_path = Path(log_path) if log_path else None
        self.fail_with = fail_with              # raised before sending, e.g. "carrier rejected"
        self.fail_after_send = fail_after_send  # raised after sending, e.g. "response timed out"
        self.sent = []                          # messages actually sent by this instance
        self._by_key = {}
        if self.log_path and self.log_path.exists():
            for line in self.log_path.read_text().splitlines():
                record = json.loads(line)
                self._by_key[record["idempotency_key"]] = record["id"]

    def reset(self):
        """Forget everything sent (demo reset only; a real provider can't be reset)."""
        self.sent.clear()
        self._by_key.clear()
        if self.log_path and self.log_path.exists():
            self.log_path.write_text("")

    def send(self, to, body, idempotency_key):
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        message_id = f"fake-{idempotency_key}"
        record = {"id": message_id, "to": to, "body": body, "idempotency_key": idempotency_key}
        self.sent.append(record)
        self._by_key[idempotency_key] = message_id
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        if self.fail_after_send:
            raise RuntimeError(self.fail_after_send)
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


# An expired row gives up its idempotency key (renamed to "<key>:expired:<id>"), so the
# customer's next draft reuses the original key. That matters when the expired row was a
# send that failed after actually going out (e.g. a timeout): the provider sees the same
# key again and doesn't text twice. For a draft that was never sent, reusing it is harmless.
_EXPIRE = ("status = 'cancelled', last_error = 'Not sent on ' || substr(created_at, 1, 10) || '; expired',"
           " idempotency_key = idempotency_key || ':expired:' || id")


def expire_stale(conn, now):
    """Cancel unsent messages from earlier days. Kept as a record, never deleted."""
    cur = conn.execute(
        f"UPDATE outbox SET {_EXPIRE} WHERE status IN ('draft', 'approved', 'failed') AND substr(created_at, 1, 10) < ?",
        (format_ts(now)[:10],),
    )
    return cur.rowcount


def draft_body(candidate):
    q = candidate.state.quote
    return TEMPLATES[candidate.tier].format(first=q["customer_name"].split()[0], tech=q["tech_name"])


def generate_drafts(conn, now=DEFAULT_NOW, policy=Policy()):
    """Create a draft for each of today's candidates. Safe to run repeatedly."""
    now = _as_dt(now)
    followups = build_followups(conn, now, policy)
    created = skipped = 0
    with conn:
        expire_stale(conn, now)  # frees yesterday's one-active-message-per-phone slots
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
    """Send (or retry) one message. Returns (outcome, detail); outcome is "sent", "blocked", "failed" or "skipped".

    Only 'approved' rows and 'failed' rows (provider error, retryable) are sent,
    so a second click or a retry on a sent or blocked row is a no-op. A retry
    reuses the row's idempotency key, so a provider that already delivered it
    won't deliver it again. BEGIN IMMEDIATE takes the write lock up front, so
    two concurrent sends of the same row can't both pass the check.
    """
    now = _as_dt(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM outbox WHERE id = ? AND status IN ('approved', 'failed')", (outbox_id,)).fetchone()
        if row is None:
            conn.rollback()
            return "skipped", "Nothing to send (already sent, blocked, expired, or never approved)"
        if row["created_at"][:10] != format_ts(now)[:10]:
            conn.execute(f"UPDATE outbox SET {_EXPIRE} WHERE id = ?", (outbox_id,))
            conn.commit()
            return "skipped", f"Drafted on {row['created_at'][:10]}; expired"

        blocker = send_blocker(load_states(conn, now), row["quote_id"], row["customer_phone"], now, policy)
        if blocker:
            conn.execute("UPDATE outbox SET status = 'blocked', last_error = ? WHERE id = ?", (blocker, outbox_id))
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
