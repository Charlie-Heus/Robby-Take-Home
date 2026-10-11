# How to use the app

## Start it
```bash
python3 -m venv .venv                      # first time only
.venv/bin/pip install -r requirements.txt  # first time only
.venv/bin/python -m takehome.web
```
Open http://127.0.0.1:5001

## Top bar
- **Today / All quotes**: switch pages.
- **Load seed data**: loads the quotes and events. Safe to click again; nothing gets duplicated.
- **Reset DB**: deletes everything (including the sent-messages log) so you can start over.
- **Now (UTC) + Go**: pretend it's a different date and time. Everything on the page updates to match.

## Today page
- **Follow-up candidates** (left): who to contact, best first. The top 10 are for today; greyed rows wait until tomorrow.
- **Excluded** (right): quotes we won't message, and why.
- **Generate drafts for today** (under the candidates): writes a message for each of today's top 10. Disappears once used; comes back on a new day.
- **Today's outbox**: today's messages, most important first. Anything not sent by the end of the day expires, and those customers come back as candidates tomorrow.
  - Draft: click **Approve** or **Discard**.
  - Approved: click **Send**.
  - Sent: done. It won't send twice.
  - Blocked: not sent, and shows why (e.g. the quote was accepted, or the customer was just texted). Final.
  - Failed: the (fake) SMS service had an error. Click **Retry**. It never texts twice.

## All quotes page
Every quote: what quotes.json says next to what the events say.

Click any quote id to open it.

## Quote page
- **Snapshot**: what quotes.json says.
- **Derived**: what the events say.
- **Events**: everything that happened, oldest first. Grey rows are after "now" and are ignored.
- **Outbox**: every message for this quote, including expired ones.

## Where "sent" messages go
Nothing is really texted. Sent messages are written to `sent_messages.jsonl`.

## Tests
```bash
.venv/bin/python -m pytest
```
