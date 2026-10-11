# Changes needed

## Outbox order is reversed
The outbox shows today's top-priority message (rank 1) at the bottom and rank 10 at the top.

**Cause:** the outbox query sorts by `o.id DESC` (newest first) in `takehome/web.py`. Drafts are inserted in rank order, so rank 1 gets the lowest id and lands at the bottom.

**Fix:** show messages in priority order, rank 1 at the top. The outbox doesn't store rank, so either:
- sort by `o.id ASC` (works because drafts are created in rank order), or
- store the rank on the outbox row at draft time and sort by it (more explicit, survives later batches).

## Remove "Views since contact" and "Unanswered reply" from the All quotes table
They repeat what the candidates' reason text already says, and they mislead on closed quotes (e.g. Q-1004 is accepted but shows "unanswered reply: yes").

**Change:** drop both columns from the All quotes table in `takehome/templates/index.html`. Display only: the policy still computes and uses both values, and the quote detail page still shows them.

## Put Excluded side by side with Follow-up candidates
Show the two tables next to each other, so "contact these" and "not these, and why" read together. Stack them on narrow screens. The candidates table has 7 columns, so trim columns (e.g. Tech) if it gets cramped.

## Move All quotes to its own page
Take the All quotes table off the main page and give it its own page (e.g. `/quotes`), linked from the header. Main page becomes the daily workflow only.

## Move "Generate drafts for today" below the candidates table, and hide it once used
- Put the button under the Follow-up candidates table instead of above it.
- Once today's drafts are generated, the button should disappear. Decide this on the server (hide it when there are no candidates left for today), not with JavaScript, so it stays hidden after a page reload and comes back when "now" moves to a new day.

## Changing the date and clicking Go should empty the outbox
When "now" moves to a different day, the outbox should start empty for that day.

**Caution: don't delete outbox rows.** Sent rows are the record of what went out, and their idempotency keys are what stop a double-send. Ways to get the effect safely:
- **Filter the view:** show only messages created on the "now" day. Older rows still exist; maybe link to "earlier messages".
- **Expire stale drafts:** when the day changes, mark unsent drafts/approved rows from earlier days `cancelled` (new status) instead of deleting them. Ties into "Cancelling stale drafts" in notes.md.

Decide which (or both) before building.

## Remove the yellow highlighting
It flags our own sends (not a data problem), only shows one direction of snapshot/event disagreement, and the side-by-side snapshot vs. derived columns already show the difference. Make the point in the README instead, with the Q-1019 example: the snapshot says last contact Aug 11, events say Aug 14, so the engine correctly keeps her in cooldown on Aug 17.

**Change:**
- `takehome/templates/index.html`: drop the `diff` classes on the status and last-outbound cells, and the "highlighted where they disagree" note in the All quotes heading.
- `takehome/templates/quote.html`: drop the `diff` classes in the Derived panel.
- `takehome/templates/base.html`: drop the `.diff` style and `--warn` color.
- `takehome/policy.py`: remove `QuoteState.contact_disagrees`, which nothing else uses.
- `instructions.md`: remove the "Yellow cells…" sentence.

## Allow retrying a failed send
The assignment says "retrying a failed send must never double-send", which assumes a retry exists. Right now a failed row is final, so the guarantee holds only because retrying is impossible. A reviewer could reasonably say "you didn't solve retry, you banned it."

**Change** (~15 min plus a test):
- A row that failed with a **provider error** gets a Retry action. It goes back through the same send path: same row, same idempotency key, guardrails re-checked.
- A row that was **blocked** by a guardrail (quote closed, customer in cooldown) stays final. Retrying it would be wrong.
- **Test:** provider fails, then succeeds on retry. Exactly one text sent, one `message_sent` event recorded.

**Watch out for:**
- Blocked and provider-error rows are both `failed` today; they only differ by the `last_error` prefix ("Blocked:" vs "Provider error:"). Tell them apart with a real field, not string matching.
- A failed row frees the customer's one-active-message slot, so another draft for the same phone may exist by the time of the retry. Moving the row back to an active status would then hit the `outbox_one_active_per_phone` index. Handle that as "blocked", not a crash.
- Nothing in the UI makes the fake provider fail, so the Retry button can't be shown by hand without a way to simulate an outage. The test is enough for grading.

## "Needs a reply" quotes get a template message
notes.md says tier 1 "goes to a person, not a template", but the engine drafts *"Thanks for your message… I'll get back to you shortly."* Once sent, that counts as our response, so the quote drops out of tier 1. The customer gets a holding reply and nothing tracks that a real answer is still owed.

**Change (pick one):**
- **Don't draft tier 1.** Show those quotes as "Reply personally" with no draft (cheapest, honest), or
- keep the draft and explain the choice in the README.

Either way, make the code and notes.md agree.

## "Gone cold" nudges every 3 days
Tier 4 has no quiet period of its own beyond the 72h cooldown, so a quote with no signal at all is nudged the moment the cooldown ends (e.g. Q-1020, Q-1002: "No contact in 3 days"), up to 3 times.

**Change (pick one):**
- require ~7 quiet days for tier 4 (new threshold in `Policy`, update the seed-ranking test), or
- keep it and defend the 3-day cadence in the README.

## Smaller fixes
- **Python version:** the code needs Python 3.11+ (`datetime.fromisoformat` can't parse a trailing "Z" before 3.11). Say so in the README.
- **Repo hygiene:** `.DS_Store` is committed despite `.gitignore`; remove it from git (`git rm --cached .DS_Store`). The assignment PDF is committed too; remove it if the repo will be public.
- **Stale docs:** fix the `schema.sql` lifecycle comment (`failed -> (retry) sending` doesn't exist) and delete the old "Log: schema setup" section in notes.md (refers to `followup/` paths). Decide whether `schedule.md` and `changes_needed.md` ship with the submission.
- **"Close it out?" ignores snapshot contacts:** the max-touches count only counts `message_sent` events, not the snapshot's `last_contact_at`. Minor; mention it in the README.
- **Replies have no content:** a reply could be "STOP" or "went with someone else". Add to the README's other-signals list; opt-outs are legally required in production.

When these are done, update `instructions.md` to match.
