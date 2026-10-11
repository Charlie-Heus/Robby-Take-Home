-- All timestamps are ISO-8601 UTC strings ("2026-08-16T16:00:00Z"), so they sort lexically.

-- Snapshot of quotes as imported. Derived state (real last contact, last view,
-- unanswered reply) is computed from events, not stored here.
CREATE TABLE IF NOT EXISTS quotes (
    id              TEXT PRIMARY KEY,
    customer_name   TEXT NOT NULL,
    customer_phone  TEXT NOT NULL,
    tech_name       TEXT NOT NULL,
    amount          INTEGER NOT NULL,          -- whole dollars, as given
    status          TEXT NOT NULL CHECK (status IN ('open', 'accepted', 'dismissed')),
    created_at      TEXT NOT NULL,
    last_contact_at TEXT                       -- snapshot value; may disagree with events
);

-- Cooldown is per customer, and one phone can have several quotes.
CREATE INDEX IF NOT EXISTS quotes_phone ON quotes (customer_phone);

-- Append-only event log. Primary key on event_id makes re-ingesting a duplicate a no-op.
-- No CHECK on type and no foreign key on quote_id: unknown types and events for
-- not-yet-imported quotes are kept, not dropped. Order comes from timestamp, never rowid.
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    quote_id    TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    channel     TEXT,
    direction   TEXT,
    source      TEXT NOT NULL DEFAULT 'webhook' CHECK (source IN ('webhook', 'engine')),
    raw         TEXT NOT NULL,                 -- original JSON, for audit and replay
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_quote_ts ON events (quote_id, timestamp);

-- Messages the engine drafts and sends. Lifecycle:
--   draft -> approved -> sent
--                     -> blocked   a guardrail stopped it (final)
--                     -> failed    provider error; retry sends the same row, same key
-- A draft can also be discarded (deleted) before approval.
-- Drafts, approved and failed rows not sent by the end of their day become 'cancelled' (kept, not deleted).
-- 'sending' is reserved for crash recovery with providers that lack idempotency keys (not used yet).
CREATE TABLE IF NOT EXISTS outbox (
    id                  INTEGER PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,  -- re-running the engine can't create a second row
    quote_id            TEXT NOT NULL REFERENCES quotes (id),
    customer_phone      TEXT NOT NULL,         -- copied at draft time; cooldown keys on this
    channel             TEXT NOT NULL DEFAULT 'sms',
    reason              TEXT NOT NULL,
    body                TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'sending', 'sent', 'failed', 'blocked', 'cancelled')),
    created_at          TEXT NOT NULL,
    approved_at         TEXT,
    approved_by         TEXT,
    sent_at             TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    provider_message_id TEXT                   -- id returned by the SMS provider
);

-- At most one in-flight message per customer at a time, enforced by the database
-- rather than only by application code.
CREATE UNIQUE INDEX IF NOT EXISTS outbox_one_active_per_phone
    ON outbox (customer_phone)
    WHERE status IN ('draft', 'approved', 'sending');
