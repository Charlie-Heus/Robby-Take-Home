# Schedule (3 hours)

| Time | Block | Done when |
|---|---|---|

| **0:00–0:15** | **Schema and project setup.** SQLite tables: `quotes`, `events` (keyed on `event_id`), `outbox` (unique idempotency key; status draft / approved / sending / sent / failed). | Schema is written and the project runs. |

| **0:15–0:45** | **Ingest and derived state.** Load seed files, drop duplicate events, compute each quote's state from timestamps (status, real last contact, last view, unanswered reply). | Ingesting twice gives the same DB. Shuffled input gives the same state. |

| **0:45–1:25** | **Policy and drafting.** For a given "now", list who to contact with a reason and score. Template drafts, one per reason (no LLM). | Aug 17 output checks out by hand against the known data problems. |

| **1:25–2:05** | **Approve → send flow and guardrails.** Send goes through a fake SMS interface. Re-check guardrails at send time: closed quote, per-phone cooldown, idempotency key already sent. | Running the engine twice, or retrying a failed send, never produces a second `message_sent`. |

| **2:05–2:30** | **Tests.** Duplicate/out-of-order events give the same state; shared phone respects cooldown; closed quotes never messaged; retries/re-runs idempotent; quote closes between approval and send. | Tests pass. |

| **2:30–2:45** | **Localhost page.** "Now" picker, candidates with reasons, approve/send buttons, outbox. Plain. | You can click through the whole flow. |

| **2:45–3:00** | **README.** How to run, policy and why, 50-shop paragraphs, next steps, where you stopped. | Then run `/export`. |

## Ground rules
- Protect the last 15 minutes for the README.
- Running late? Cut the UI first (a CLI is enough). Never cut the guardrail tests.
- Re-check guardrails at send time, not only when drafting.
- Log judgment calls in notes.md as you go.
