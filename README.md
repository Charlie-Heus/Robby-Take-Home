# Robby follow-up engine

An MVP that looks at open home-service quotes, decides who to follow up with and why, drafts the messages, and sends them only after a person approves.

## Running it

Needs Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m takehome.web        # open http://127.0.0.1:5001
.venv/bin/python -m pytest              # run the tests
```

The app uses port 5001 because macOS's AirPlay Receiver uses 5000. In the app:

- **Load seed data** loads the quotes and events. Clicking it again duplicates nothing. **Reset DB** starts over.
- **Now (UTC) + Go** replays the engine as of any date and time. The default is Aug 17, 9am Eastern.
- **Today** shows the ranked candidates next to the excluded quotes and the reason for each. **Generate drafts for today** fills today's outbox. Then **Approve**, **Send**, or **Retry** a failed send.
- **All quotes** shows what `quotes.json` says next to what the events say. Click any quote id to see its full event history and messages.
- Nothing is really texted. "Sent" messages are written to `sent_messages.jsonl`.

## Architecture

Python 3.11+, SQLite and a small Flask app. Everything lives in the `takehome/` package:

| Piece | File | Job |
|---|---|---|
| Storage | `schema.sql`, `db.py` | Three tables: `quotes`, `events`, `outbox` |
| Ingest | `ingest.py` | Loads `seed/quotes.json` and `seed/events.jsonl` into the tables |
| Policy | `policy.py` | Works out each quote's real state as of a given "now", then picks and ranks who to contact |
| Outbox | `outbox.py` | Drafts messages, handles approve and send, and holds the fake SMS provider |
| UI | `web.py`, `templates/` | Local web page. Routes only call the functions above. |

The three tables:
- **`quotes`**: the quote snapshot as imported. Treated as a hint, not the truth.
- **`events`**: an append-only log of everything that happened: views, replies, messages, acceptances. Keyed on `event_id`.
- **`outbox`**: every message the engine drafts, with its status and an idempotency key.

How data flows:

```
seed files ──ingest──▶ quotes + events
                            │
                   policy (as of "now")
                            │
                 ranked candidates + reasons
                            │
                     generate drafts ──▶ outbox (draft)
                                            │ person approves
                                            ▼
                         send: re-check guardrails ──▶ SMS provider
                                            │
                       message_sent event written back into events
```

Tests in `tests/` cover ingest, the policy and the send flow.

## How it works

1. **Load the data.** Quotes and events are read into the database. Duplicate events are ignored, bad lines are skipped and reported, and the file order doesn't matter.
2. **Work out what's really going on with each quote.** For a chosen "now", the engine looks at every event up to that moment: when we last contacted the customer, when they last viewed the quote, whether they replied and we never answered, and whether the quote was accepted.
3. **Rule out who must not be contacted.** Closed quotes, customers we texted in the last 72 hours (about any of their quotes), quotes sent less than a day ago, and quotes that are too old or have already had 3 follow-ups.
4. **Rank everyone else** into four groups, biggest amount first within each:
   1. **Needs a reply:** the customer replied and nobody answered.
   2. **Hot:** they viewed the quote in the last 3 days and we haven't contacted them since.
   3. **Big and quiet:** $5,000 or more and no contact in 5+ days.
   4. **Gone cold:** everyone else.

   Each customer appears once, and only the top 10 are for today. Every line says why it's there.
5. **Draft and approve.** One click writes a message for each of today's top 10. The owner approves or discards each one.
6. **Send safely.** Just before sending, the engine checks again that the quote is still open and the customer isn't in cooldown. If something changed, the message is blocked and the reason shown. A sent message is recorded as an event, so the cooldown picks it up immediately.
7. **Start fresh each day.** Messages not sent by the end of the day expire. Those customers come back as candidates the next day.

## Judgments

### Data and ingest

- **Events are the source of truth; `quotes.json` is a snapshot.**
  *Why:* the two disagree in both directions. For example, Q-1019's snapshot says the last contact was Aug 11, but the events show a message on Aug 14. Trusting the events correctly keeps that customer in cooldown on Aug 17.
- **Ingest only stores data; state is worked out when it's read.**
  *Why:* out-of-order and late events then need no special handling. A late event is just another row. It also means re-running ingest changes nothing.
- **A repeated `event_id` is ignored. If its content differs, the first copy is kept and the conflict is reported.**
  *Why:* overwriting silently could rewrite history, and failing the whole import over one bad event blocks all the good ones.
- **Unknown event types (like `quote_sent`) and events for unknown quotes are stored, not dropped.**
  *Why:* real webhook streams change. The logic ignores what it doesn't understand, but nothing is lost.
- **Bad lines are skipped and reported. Timestamps are converted to UTC, and phone numbers to `+1XXXXXXXXXX`.**
  *Why:* timestamps are compared as text, so one odd format would quietly put events in the wrong order. The phone number is how we identify a customer. Timestamps without a timezone are rejected rather than guessed at, and fractions of a second are dropped. Phone handling assumes US numbers.
- **On re-import, a closed quote never reopens, and `last_contact_at` never moves backwards.**
  *Why:* dismissals only arrive through the snapshot, so it has to be re-importable, but an old export mustn't make a closed quote eligible for messages again.

### Who to contact

- **A `quote_accepted` event closes a quote even if the snapshot still says "open", and later views never reopen it.**
  *Why:* either source can be the first to know. Q-1017 is accepted in the snapshot with no event, and in production an acceptance event can arrive before the next snapshot. Views keep coming after a quote closes (Q-1004 and Q-1009). Wrongly messaging a customer who already decided is worse than missing a rare reopen, which a person can handle.
- **New quotes wait 24 hours. Quotes over 30 days old, or with 3 follow-ups and no response, are surfaced as "close it out?" instead of nudged.**
  *Why:* the customer deserves a day to look before being chased, and past a point more nudges are noise. A person should decide whether the deal is dead.
- **The cooldown is per phone number, counts outbound messages only, and lasts 72 hours.**
  *Why:* one customer can have several quotes (Karen Nguyen has two on the same phone), and texting her about each would break the cooldown. A customer reply is a reason to answer, not a reason to wait.
- **The snapshot's `last_contact_at` counts as an outbound message.**
  *Why:* it's unclear whether it means outbound or any contact. Treating it as outbound can only make a cooldown longer, never shorter.
- **The cooldown applies to "Needs a reply" quotes too.**
  *Why:* the assignment says never message the same customer twice within the cooldown, with no exceptions. Answering a question arguably shouldn't count. That's an open question, and for now such a reply would come from a person outside the engine.
- **Hard filters first, then four ranked groups, rather than a weighted score.**
  *Why:* every candidate gets a reason the owner can act on ("she replied and we didn't answer"), the order is easy to defend, and each shop could tune it. A weighted score fitted to 30 quotes would be guesswork.
- **Interest beats size.** A recently viewed $850 quote ranks above a quiet $22,000 one.
  *Why:* the big quote will still be there tomorrow; the moment of interest won't.
- **Within "Hot", the number of views shows in the reason but doesn't change the order; amount does.**
  *Why:* one ordering rule across all groups is easier to explain and defend than a separate rule per group.
- **One candidate per customer, and a cap of 10 per day.** The cap counts messages already drafted that day.
  *Why:* the owner's problem today is scrolling 60 quotes, and 25 drafts would recreate it. Counting today's drafts stops a second click on "Generate" from adding 10 more.
- **"Now" is a parameter, and events after it are ignored.**
  *Why:* the seed data is from August, and replaying any point in time makes the policy easy to test and demo. The default is Aug 17, 9am Eastern, the morning after the last seed event. The snapshot's status has no timestamp, so it applies at any "now". Moving "now" backwards can get past the guardrails, but that's demo-only: in production "now" is the real clock.
- **Every threshold (cooldown, windows, $5,000 line, daily cap) lives in one `Policy` object.**
  *Why:* they're judgment calls a shop will want to change, so they belong in one place, not scattered through the code.
- **"Close it out?" counts only `message_sent` events, not the snapshot's `last_contact_at`.** After 3 follow-ups with no response, a quote stops being nudged.
  *Why:* the snapshot only gives one timestamp, so it can't say how many messages it stands for. The side effect is that a quote whose contacts exist only in the snapshot can be nudged more than 3 times in total. The cooldown still counts the snapshot contact.
- **A customer reply is treated as "they want an answer", whatever it says.**
  *Why:* the events carry no message text, so a reply of "STOP" or "went with someone else" looks the same as a question. In production, opt-outs (STOP) must be read and become a hard filter, because that's legally required.
- **Days are UTC days.**
  *Why:* a simplification. All seed customers share one area code. In production "today" should follow each shop's local timezone.

### Drafting and sending

- **Every message needs a person's approval, and the guardrails are checked again at send time, in the same database transaction that records the send.**
  *Why:* a lot can change between drafting and sending: the customer accepts, or a tech texts them by hand. Checking only when drafting would miss that.
- **At most one unsent message per customer, enforced by a database index.**
  *Why:* a rule in the database still holds if the application code has a bug.
- **Our own sends are written back into the event log as `message_sent` events.**
  *Why:* the cooldown then has one source of truth, whether a message came from the engine or from the shop's existing system.
- **The idempotency key is the quote plus our last contact with the customer. When an unsent message expires, it gives up its key, so the customer's next draft reuses it.**
  *Why:* re-running the engine on the same state produces the same key and is skipped. After a send, the last contact changes, so the next follow-up gets a new key. Releasing the key on expiry fixes a bug found in review: a send that timed out *after* actually going out, then expired and was redrafted under a new key, would have texted the customer twice. With the same key, the provider recognises the repeat.
- **"Blocked" and "failed" are separate statuses.** Blocked means a guardrail stopped the send, and it's final. Failed means the SMS provider errored, and it can be retried on the same row with the same key.
  *Why:* retrying a blocked message would be wrong, and a retry must never become a second, different message. Both statuses free the customer's one unsent-message slot. A retry goes straight from failed to sent without passing through an active status again, so it can't collide with that rule.
- **Retrying a send is only safe if the real SMS provider supports idempotency keys.**
  *Why:* if the provider already delivered a key, it should return the original message instead of texting again. That's what makes a retry safe when the first attempt actually went out but the response was lost. The fake provider behaves this way. With a provider that doesn't support idempotency keys, a retry after a timeout could text the customer twice. That would need crash recovery instead: mark the row `sending` before calling the provider, and have a person check unclear results rather than retrying.
- **The outbox works one day at a time.** Unsent messages expire at the end of their day and are marked `cancelled`, never deleted.
  *Why:* each morning starts from a fresh, ranked list instead of yesterday's leftovers. Keeping the rows preserves the record of what happened.
- **Discard deletes a draft.**
  *Why:* the simplest option. Two known side effects: a discarded draft comes back if you click "Generate" again the same day, and a draft whose quote closes later that day is only caught when someone tries to send it. Marking it `cancelled` instead of deleting it would fix the first.
- **The SMS provider is faked, sends use the app's "now" as the clock, and the approver is a hard-coded "owner" with no login.**
  *Why:* the assignment is about the decisions and the guardrails, not real texting, deployment or auth. The fake provider sits behind a small `send(to, body, idempotency_key)` interface, so a real one (such as Twilio) can replace it. In production, the provider's delivered and failed callbacks would come back in as events, and the approver would be the signed-in user.
- **"Needs a reply" quotes get a template draft too.**
  *Why:* it was simpler than writing real answers for a demo. The side effect is that sending it counts as our reply, so the quote leaves "Needs a reply" even though the real answer is still owed. In production the owner would edit the draft before approving, so what goes out *is* the answer.
- **Templates never mention that the customer viewed the quote.**
  *Why:* "I saw you looked at your quote" reads as creepy.

### Other signals worth adding

The seed data doesn't carry these, so the policy doesn't use them yet:

- **A view right after our message:** it landed, and they're thinking about it.
- **Bursts of views in a short time:** likely comparing quotes. Q-1012 got 3 views in 11 hours.
- **The channel they replied on:** someone who replies by email should get the follow-up by email.
- **Quiet hours in the customer's local time:** texting rules generally allow only 8am to 9pm.
- **Opt-outs (STOP):** a hard filter, required by law.
- **Quote expiry dates and job urgency:** a broken AC in August can't wait.
- **The tech's notes and repeat customers.**
