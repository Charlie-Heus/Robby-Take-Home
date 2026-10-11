# Notes

## Decisions
- **Cooldown counts outbound messages only.** A customer reply does not start a cooldown; it flags the quote as "needs a human answer."
- **Our sends write `message_sent` back into the event store.** One source of truth for cooldown. Each outbox row carries an idempotency key so retries and re-runs can't double-send.
- **Stack:** Python 3.11+, SQLite, a small Flask page.
- **Events table accepts anything well-formed.** No CHECK on `type` and no FK on `quote_id`, so unknown types (e.g. `quote_sent`) and early events are stored, not dropped. Logic ignores what it doesn't understand.
- **Index on `quotes.customer_phone`** because the cooldown is per customer (phone), not per quote.
- **One in-flight outbox row per phone**, enforced by a partial unique index (not just app code). Covers customers with several quotes.
- **Quote status/last_contact_at are a snapshot.** Real state is derived from events at query time.
- **Ingest only stores, never derives state.** Out-of-order/late events need no special handling.
- **Repeated event_id with different content: keep the first, report a conflict.** No silent overwrite; one bad event doesn't fail the import.
- **Quote re-import overwrites, except:** a closed status (accepted/dismissed) never reopens, and `last_contact_at` keeps the later value. Skipping isn't an option: dismissals only arrive via the snapshot.
- **Moving "now" backwards can bypass send guardrails (accepted).** Guardrails only see events up to "now", so sending at an earlier "now" than a past send can text the same customer again. Demo-only: in production "now" is the real clock and can't go backwards.
- **Bad lines are skipped and reported**, not fatal. Timestamps normalized to UTC `...Z` (naive ones rejected); phones normalized to `+1XXXXXXXXXX` (US-only assumption). Sub-second precision is dropped.

## Revisit later
- **Cooldown vs. "Needs a reply" quotes.** The 72h cooldown currently applies to tier 1 too: if a customer replies 1h after our text, the engine won't draft an answer for another 71h. The assignment's "never message twice within cooldown" reads as absolute, but answering a customer's question arguably shouldn't count. For now that reply comes from a person outside the engine.
- **Crash recovery, for providers without idempotency keys.** Retry is only safe because the provider drops repeated keys. Without that: add a `sending` status (claim the row before calling the provider) and send unclear results (timeouts, a crash mid-send) to a person instead of retrying: at-most-once beats at-least-once for customer texts.
- **Delivery callbacks.** Ingest the real provider's delivered/failed callbacks as events.
- **Cancelling drafts whose quote closed, same day.** A draft whose quote is accepted later that day is only caught at send time (unsent drafts from earlier days already expire to `cancelled`). Discard could also use `cancelled` instead of deleting, so a discarded draft isn't re-created by the next "Generate drafts" the same day.
- **Editing draft text.** Especially "Needs a reply" drafts, which should be written by a person.

## Send flow (takehome/outbox.py), simple version
- `draft → approved → sent`. A guardrail stop → `blocked` (final). A provider error → `failed` (retryable). Discard deletes a draft.
- **Send re-checks guardrails** (quote closed? phone in cooldown?) inside the same transaction that records the send (`BEGIN IMMEDIATE`). `blocked` and `failed` both free the customer's one-active-message slot.
- **Only `approved` and `failed` rows send**, so double clicks, or retrying a sent or blocked row, are no-ops.
- **Retry = the same send on the same row, same idempotency key**, guardrails re-checked. Goes straight `failed → sent`, never back through an active status, so it can't collide with the one-active-per-phone index.
- **Retry is only safe because the provider honors idempotency keys** (the fake one does: a repeated key returns the original message id without texting again). That covers "it actually sent but the response timed out". Real providers that lack idempotency keys need the crash-recovery approach in Revisit later. Stated in README.
- **Idempotency key = quote + last outbound contact.** Re-running the engine can't create a second draft; after a send the anchor moves, so the next quiet period gets its own key. **An expiring row gives up its key** (renamed `<key>:expired:<id>`), so a rolled-over customer's new draft reuses the original key. Why: an earlier version put the draft day in the key, and a send that timed out after actually going out, then expired and was redrafted with a new key, texted the customer twice. Reusing the key lets the provider drop the repeat. Regression test: `test_timeout_then_rollover_does_not_double_send`.
- **The outbox is per day.** Drafts, approved and failed rows not sent by the end of their day expire to `cancelled` (kept, never deleted) the next time drafts are generated, and send refuses a row from an earlier day. Those customers roll over into the next day's candidates. Only today's unsent rows hold a customer's slot. The Today page shows only today's outbox; history stays on each quote's page.
- **A send writes a `message_sent` event** (`engine:<key>`), so the policy's cooldown picks it up automatically.
- **Daily cap counts today's drafts**, so clicking "Generate drafts" again doesn't refill it with the next 10.
- Fake provider logs to `sent_messages.jsonl` and reloads it on startup to remember sent keys. Reset DB clears it and rebuilds the tables (so schema changes apply). Sends use the UI's "now" as the clock. Approver is hard-coded "owner" (no auth).
- Templates per tier never mention that the customer viewed the quote (it reads as creepy).

## Ranking logic (takehome/policy.py)

**Step 1: hard filters.** A quote is never a candidate if:
- it's closed: snapshot status accepted/dismissed, or a `quote_accepted` event exists (later views don't reopen it)
- it was sent < 24h ago (give the customer a day)
- it's > 30 days old, or has had 3 outbound messages: surfaced as "close it out?" instead
- the customer (phone) already has an unsent message in today's outbox (draft/approved)
- the customer (phone) got an outbound message < 72h ago, on **any** of their quotes

**Step 2: tiers**, then amount (high → low) within a tier:
1. **Needs a reply:** customer replied after our last outbound. Should be answered by a person; gets a template draft for now (see judgment calls).
2. **Hot:** viewed in the last 72h with no outbound since. View count shows in the reason but doesn't reorder.
3. **Big and quiet:** amount ≥ $5k and no outbound in ≥ 5 days (or never).
4. **Gone cold:** everything else that passed the filters.

**Then:** one candidate per phone (a customer's other quotes wait), and only the top 10 are "today"; the rest carry over.

**Judgment calls**
- "Needs a reply" quotes still get a template draft (demo shortcut). Sending it counts as our reply, so the quote leaves tier 1 even though the real answer is still owed; in production the owner edits the draft first. Explained in README.
- "Gone cold" quotes are nudged as soon as the 72h cooldown ends (no extra quiet period). Kept as is.
- Intent beats size: a recent view outranks a quiet $22k quote. The big quote is still there tomorrow; the hot moment isn't.
- Tiers instead of a weighted score: explainable reasons, defensible order, easy to tune per shop.
- Snapshot `last_contact_at` counts as outbound. It can only lengthen a cooldown, never shorten one.
- Cooldown applies to tier 1 too. Answering a customer within 72h of our last text is blocked; that reply should go through a person outside the engine for now.
- "Now" is a parameter. Events (and snapshot contacts) after "now" are ignored. The snapshot *status* has no timestamp, so it applies at any "now".
- Default "now" = 2026-08-17 13:00Z (9am Eastern, the morning after the last seed event).
- All thresholds live in one `Policy` object.

**Other signals worth adding (README)**: views right after our message; view bursts (comparison shopping); reply channel; quiet hours / local time; opt-outs (STOP, legally required); quote expiry; job urgency; tech notes; repeat customers.
