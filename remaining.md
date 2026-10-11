# Remaining before submission

Besides finishing the README.

## Required by the assignment
- Commit and push today's work (the retry changes aren't committed yet).
- Test a fresh clone: clone into a temp folder, follow the README, run the tests and click through the app.
- `/export` every Claude Code session, including the other session that worked on `changes_needed.md`.

## Decisions that affect correctness (from changes_needed.md)
- ~~**Needs a reply quotes:** stop drafting a template for them ("Reply personally"), or keep the draft and defend it in the README.~~ resolved: kept the template; README Judgments explains it
- ~~**Gone cold quotes:** add a ~7-day quiet period, or defend nudging every 3 days.~~ resolved: kept as is
- ~~**Changing "now" to a new day:** filter the outbox to that day, or cancel stale drafts. Never delete rows.~~ done (both: per-day outbox + unsent rows expire to `cancelled`)

## UI changes (from changes_needed.md) ✅ resolved
- ~~Show the outbox in priority order, rank 1 at the top.~~ resolved
- ~~Remove the "Views since contact" and "Unanswered reply" columns.~~ resolved
- ~~Put Excluded side by side with the candidates.~~ resolved
- ~~Move All quotes to its own page.~~ resolved
- ~~Move "Generate drafts" below the candidates and hide it once used.~~ resolved
- ~~Remove the yellow highlighting.~~ resolved

## Tidying
- ~~Remove `.DS_Store` from git.~~ done (untracked; still ignored)
- ~~Remove the assignment PDF if the repo is public.~~ n/a: repo is private, PDF kept
- ~~Delete the old "Log: schema setup" section from notes.md.~~ done
- ~~Decide whether `schedule.md`, `notes.md` and `changes_needed.md` go in the submission.~~ decided: fold all .md files into the README, then delete the others; submit only the README
- ~~Update `instructions.md` after the UI changes.~~ done

## README points to include (besides the main sections) ✅ completed
- ~~It needs Python 3.11+.~~ in README (Architecture)
- ~~The Q-1019 example (snapshot and events disagree, and the engine gets it right).~~ in README (Judgments)
- ~~"Close it out?" ignores contacts recorded only in the snapshot.~~ in README (Judgments)
- ~~Replies have no content, so a "STOP" can't be detected. Opt-outs are legally required.~~ in README (Judgments)
- ~~Moving "now" backwards can get past the guardrails. That's demo-only.~~ in README (Judgments)
- ~~What I'd build next: a ~7-day quiet period for "Gone cold" quotes. Today a quote with no signal is nudged as soon as the 72h cooldown ends (every 3 days, up to 3 times). It only affects what the engine suggests, not what the owner can send; any view or reply moves the quote to a higher tier without the wait.~~ dropped: not built, not included
