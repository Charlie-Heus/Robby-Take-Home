# How to use the app

## Start it
```bash
python3 -m venv .venv                      # first time only
.venv/bin/pip install -r requirements.txt  # first time only
.venv/bin/python -m takehome.web
```
Open http://127.0.0.1:5000

## Buttons (top bar)
- **Load seed data**: loads the quotes and events. Safe to click again; nothing gets duplicated.
- **Reset DB**: deletes everything so you can start over.
- **Now (UTC) + Go**: pretend it's a different date and time. Everything on the page updates to match.

## Main page
- **Follow-up candidates**: who to contact, best first. The top 10 are for today; greyed rows wait until tomorrow.
- **Generate drafts for today**: writes a message for each of today's top 10. Safe to click again.
- **Outbox**: the messages.
  - Draft: click **Approve** or **Discard**.
  - Approved: click **Send**.
  - Sent: done. It won't send twice.
  - Failed: shows why (e.g. the quote was accepted, or the customer was just texted).
- **Excluded**: quotes we won't message, and why.
- **All quotes**: every quote. Yellow cells are where the events disagree with quotes.json.

Click any quote id to open it.

## Quote page
- **Snapshot**: what quotes.json says.
- **Derived**: what the events say.
- **Events**: everything that happened, oldest first. Grey rows are after "now" and are ignored.
- **Outbox**: messages for this quote.

## Where "sent" messages go
Nothing is really texted. Sent messages are written to `sent_messages.jsonl`.

## Tests
```bash
.venv/bin/python -m pytest
```
