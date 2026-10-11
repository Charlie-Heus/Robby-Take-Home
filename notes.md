# Notes

## Decisions
- **Cooldown counts outbound messages only.** A customer reply does not start a cooldown; it flags the quote as "needs a human answer."
- **Our sends write `message_sent` back into the event store.** One source of truth for cooldown. Each outbox row carries an idempotency key so retries and re-runs can't double-send.
- **Stack:** Python + SQLite + a small Flask/FastAPI page.
- **Events table accepts anything well-formed.** No CHECK on `type` and no FK on `quote_id`, so unknown types (e.g. `quote_sent`) and early events are stored, not dropped. Logic ignores what it doesn't understand.
- **Index on `quotes.customer_phone`** because the cooldown is per customer (phone), not per quote.
- **One in-flight outbox row per phone**, enforced by a partial unique index (not just app code). Covers customers with several quotes.
- **Quote status/last_contact_at are a snapshot.** Real state is derived from events at query time.
- **Ingest only stores, never derives state.** Out-of-order/late events need no special handling.
- **Repeated event_id with different content: keep the first, report a conflict.** No silent overwrite; one bad event doesn't fail the import.
- **Quote re-import overwrites, except:** a closed status (accepted/dismissed) never reopens, and `last_contact_at` keeps the later value. Skipping isn't an option: dismissals only arrive via the snapshot.
- **Bad lines are skipped and reported**, not fatal. Timestamps normalized to UTC `...Z` (naive ones rejected); phones normalized to `+1XXXXXXXXXX` (US-only assumption). Sub-second precision is dropped.

## Revisit later
- **Cooldown vs. "Needs a reply" quotes.** The 72h cooldown currently applies to tier 1 too: if a customer replies 1h after our text, the engine won't draft an answer for another 71h. The assignment's "never message twice within cooldown" reads as absolute, but answering a customer's question arguably shouldn't count. For now that reply comes from a person outside the engine.

## Ranking logic (takehome/policy.py)

**Step 1: hard filters.** A quote is never a candidate if:
- it's closed: snapshot status accepted/dismissed, or a `quote_accepted` event exists (later views don't reopen it)
- it was sent < 24h ago (give the customer a day)
- it's > 30 days old, or has had 3 outbound messages: surfaced as "close it out?" instead
- the customer (phone) already has a message in the outbox (draft/approved/sending)
- the customer (phone) got an outbound message < 72h ago, on **any** of their quotes

**Step 2: tiers**, then amount (high → low) within a tier:
1. **Needs a reply:** customer replied after our last outbound. Goes to a person, not a template.
2. **Hot:** viewed in the last 72h with no outbound since. View count shows in the reason but doesn't reorder.
3. **Big and quiet:** amount ≥ $5k and no outbound in ≥ 5 days (or never).
4. **Gone cold:** everything else that passed the filters.

**Then:** one candidate per phone (a customer's other quotes wait), and only the top 10 are "today"; the rest carry over.

**Judgment calls**
- Intent beats size: a recent view outranks a quiet $22k quote. The big quote is still there tomorrow; the hot moment isn't.
- Tiers instead of a weighted score: explainable reasons, defensible order, easy to tune per shop.
- Snapshot `last_contact_at` counts as outbound. It can only lengthen a cooldown, never shorten one.
- Cooldown applies to tier 1 too. Answering a customer within 72h of our last text is blocked; that reply should go through a person outside the engine for now.
- "Now" is a parameter. Events (and snapshot contacts) after "now" are ignored. The snapshot *status* has no timestamp, so it applies at any "now".
- Default "now" = 2026-08-17 13:00Z (9am Eastern, the morning after the last seed event).
- All thresholds live in one `Policy` object.

**Other signals worth adding (README)**: views right after our message; view bursts (comparison shopping); reply channel; quiet hours / local time; opt-outs (STOP, legally required); quote expiry; job urgency; tech notes; repeat customers.

## Log: schema setup (6:51 PM)

### Check Python and SQLite versions
```
$ python3 --version && python3 -c "import sqlite3;print(sqlite3.sqlite_version)"
Python 3.13.5
3.49.1
```

### Files written
- `followup/schema.sql` (63 lines)
- `followup/db.py` (26 lines)
- `followup/__init__.py` (empty)
- `.gitignore` (`*.db`, `__pycache__/`, `.DS_Store`)

#### followup/schema.sql
```sql
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
--   draft -> approved -> sending -> sent
--                           \-> failed -> (retry) sending
-- A draft can also be discarded (deleted) before approval.
CREATE TABLE IF NOT EXISTS outbox (
    id                  INTEGER PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,  -- re-running the engine can't create a second row
    quote_id            TEXT NOT NULL REFERENCES quotes (id),
    customer_phone      TEXT NOT NULL,         -- copied at draft time; cooldown keys on this
    channel             TEXT NOT NULL DEFAULT 'sms',
    reason              TEXT NOT NULL,
    body                TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('draft', 'approved', 'sending', 'sent', 'failed')),
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
```

#### followup/db.py
```python
import sqlite3
import sys
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_DB = Path(__file__).resolve().parent.parent / "followup.db"


def connect(path=DEFAULT_DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA.read_text())


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB
    with connect(path) as conn:
        init_db(conn)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    print(f"initialized {path}: {', '.join(tables)}")
```

### Init schema twice and probe constraints
Checks that init runs, is safe to re-run, and that the constraints actually bite.
```
D=/private/tmp/claude-501/-Users-charlieheus-Desktop-Robby/cdb4d953-f023-4076-965f-df8bd7ae7299/scratchpad/t.db && rm -f $D && python3 -m followup.db $D && python3 -m followup.db $D && python3 - $D <<'PY'
import sys, sqlite3
from followup.db import connect
c=connect(sys.argv[1])
c.execute("insert into quotes values('Q-1','A','+1','T',100,'open','2026-08-01T00:00:00Z',null)")
c.execute("insert into quotes values('Q-2','A','+1','T',100,'open','2026-08-01T00:00:00Z',null)")
def ob(key,q,status): c.execute("insert into outbox(idempotency_key,quote_id,customer_phone,reason,body,status,created_at) values(?,?,?,?,?,?,?)",(key,q,'+1','r','b',status,'now'))
def tryit(label,f):
    try: f(); print("ALLOWED  ",label)
    except sqlite3.IntegrityError as e: print("REJECTED ",label,"-",e)
tryit("first draft", lambda: ob('k1','Q-1','draft'))
tryit("same idempotency key", lambda: ob('k1','Q-1','draft'))
tryit("2nd active draft, same phone, other quote", lambda: ob('k2','Q-2','draft'))
tryit("bad status", lambda: ob('k3','Q-2','queued'))
tryit("unknown quote", lambda: ob('k4','Q-404','sent'))
tryit("sent row alongside active draft", lambda: ob('k5','Q-2','sent'))
tryit("bad quote status", lambda: c.execute("insert into quotes values('Q-3','A','+1','T',1,'won','x',null)"))
PY
```
Output:
```
initialized .../scratchpad/t.db: quotes, events, outbox
initialized .../scratchpad/t.db: quotes, events, outbox
ALLOWED   first draft
REJECTED  same idempotency key - UNIQUE constraint failed: outbox.customer_phone
REJECTED  2nd active draft, same phone, other quote - UNIQUE constraint failed: outbox.customer_phone
REJECTED  bad status - CHECK constraint failed: status IN ('draft', 'approved', 'sending', 'sent', 'failed')
REJECTED  unknown quote - FOREIGN KEY constraint failed
ALLOWED   sent row alongside active draft
REJECTED  bad quote status - CHECK constraint failed: status IN ('open', 'accepted', 'dismissed')
```

### Verify idempotency key uniqueness alone
The duplicate-key insert above was caught by the per-phone index first, so the idempotency key was checked on its own:
```
python3 - <<'PY'
import sqlite3
from followup.db import connect, init_db
c=connect(":memory:"); init_db(c)
c.execute("insert into quotes values('Q-1','A','+1','T',100,'open','x',null)")
ins="insert into outbox(idempotency_key,quote_id,customer_phone,reason,body,status,created_at) values('k1','Q-1','+1','r','b','sent','now')"
c.execute(ins)
try: c.execute(ins); print("ALLOWED")
except sqlite3.IntegrityError as e: print("REJECTED -", e)
PY
```
Output:
```
REJECTED - UNIQUE constraint failed: outbox.idempotency_key
```

### Result
The schema is in place: the three tables are in `followup/schema.sql`, and `followup/db.py` creates them. Plain Python, no dependencies. To create the database:
```
python3 -m followup.db
```
That writes `followup.db` in the project folder (gitignored). Running it again is safe.
