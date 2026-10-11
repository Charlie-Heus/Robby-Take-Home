"""Follow-up policy: who to contact, and why, as of a given "now".

Two steps:
1. Hard filters remove quotes that must not be messaged (closed, cooldown, ...).
2. What's left is ranked by tier, then by amount within a tier.

Quote state is derived here from the snapshot plus events up to "now". Events
after "now" are ignored, so the engine can be run as of any point in time.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DEFAULT_NOW = "2026-08-17T13:00:00Z"  # 9am Eastern, the morning after the last seed event
ACTIVE_OUTBOX = ("draft", "approved", "sending")
CLOSED = ("accepted", "dismissed")


@dataclass(frozen=True)
class Policy:
    cooldown: timedelta = timedelta(hours=72)     # per phone, outbound messages only
    recent_view: timedelta = timedelta(hours=72)  # how recent a view must be to count as "hot"
    big_amount: int = 5000
    quiet: timedelta = timedelta(days=5)          # silence before a big quote counts as "quiet"
    min_age: timedelta = timedelta(hours=24)      # give a new quote a day before nudging
    max_age: timedelta = timedelta(days=30)
    max_touches: int = 3                          # outbound messages before we stop nudging
    daily_cap: int = 10


TIER_NAMES = {1: "Needs a reply", 2: "Hot", 3: "Big and quiet", 4: "Gone cold"}


@dataclass
class QuoteState:
    quote: dict
    status: str                    # snapshot status, or "accepted" if a quote_accepted event exists
    created_at: datetime
    last_outbound: datetime | None  # latest of snapshot last_contact_at and message_sent events
    outbound_count: int
    last_view: datetime | None
    views_since_outbound: int
    last_reply: datetime | None
    last_reply_channel: str | None
    unanswered_reply: bool         # customer replied after our last outbound message
    events: list = field(default_factory=list)

    @property
    def id(self):
        return self.quote["id"]

    @property
    def phone(self):
        return self.quote["customer_phone"]

    @property
    def amount(self):
        return self.quote["amount"]


@dataclass
class Candidate:
    state: QuoteState
    tier: int
    reason: str
    rank: int = 0
    over_cap: bool = False         # ranked below the daily cap; carried over to tomorrow

    @property
    def tier_name(self):
        return TIER_NAMES[self.tier]


@dataclass
class Excluded:
    state: QuoteState
    reason: str


@dataclass
class Followups:
    now: datetime
    candidates: list
    excluded: list
    states: dict


def parse_ts(value):
    return datetime.fromisoformat(value)


def format_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(delta):
    hours = int(delta.total_seconds() // 3600)
    return f"{hours}h" if hours < 48 else f"{hours // 24} days"


def derive_state(quote, events):
    """Combine one quote's snapshot with its events (already filtered to <= now)."""
    by_type = {}
    for e in sorted(events, key=lambda e: (e["timestamp"], e["event_id"])):
        by_type.setdefault(e["type"], []).append(e)

    outbound = [parse_ts(e["timestamp"]) for e in by_type.get("message_sent", [])
                if e["direction"] in (None, "outbound")]
    outbound_count = len(outbound)
    # The snapshot's last_contact_at is treated as outbound: it can only lengthen a cooldown.
    if quote["last_contact_at"]:
        outbound.append(parse_ts(quote["last_contact_at"]))
    last_outbound = max(outbound, default=None)

    status = "accepted" if by_type.get("quote_accepted") else quote["status"]

    views = [parse_ts(e["timestamp"]) for e in by_type.get("quote_viewed", [])]
    replies = by_type.get("customer_replied", [])
    last_reply = parse_ts(replies[-1]["timestamp"]) if replies else None

    return QuoteState(
        quote=quote,
        status=status,
        created_at=parse_ts(quote["created_at"]),
        last_outbound=last_outbound,
        outbound_count=outbound_count,
        last_view=max(views, default=None),
        views_since_outbound=sum(1 for v in views if last_outbound is None or v > last_outbound),
        last_reply=last_reply,
        last_reply_channel=replies[-1]["channel"] if replies else None,
        unanswered_reply=last_reply is not None and (last_outbound is None or last_reply > last_outbound),
        events=events,
    )


def load_states(conn, now):
    now_s = format_ts(now)
    quotes = [dict(r) for r in conn.execute("SELECT * FROM quotes WHERE created_at <= ? ORDER BY id", (now_s,))]
    events = {}
    for r in conn.execute("SELECT * FROM events WHERE timestamp <= ?", (now_s,)):
        events.setdefault(r["quote_id"], []).append(dict(r))
    for q in quotes:
        # A snapshot contact recorded after "now" hasn't happened yet from this point of view.
        if q["last_contact_at"] and q["last_contact_at"] > now_s:
            q["last_contact_at"] = None
    return {q["id"]: derive_state(q, events.get(q["id"], [])) for q in quotes}


def phone_last_outbound(states):
    """Latest outbound contact per phone across all its quotes, as {phone: (when, quote_id)}."""
    phone_last = {}
    for s in states.values():
        if s.last_outbound and (s.phone not in phone_last or s.last_outbound > phone_last[s.phone][0]):
            phone_last[s.phone] = (s.last_outbound, s.id)
    return phone_last


def send_blocker(states, quote_id, phone, now, policy=Policy()):
    """Return why a message about this quote must not go out right now, or None.

    The two guardrails re-checked at send time: the quote is closed, or the
    customer (phone) is inside the cooldown window.
    """
    s = states.get(quote_id)
    if s is None:
        return "Quote not found"
    if s.status in CLOSED:
        return f"Quote is {s.status}"
    last, via = phone_last_outbound(states).get(phone, (None, None))
    if last and now - last < policy.cooldown:
        other = f" about {via}" if via != quote_id else ""
        return f"Customer contacted {ago(now - last)} ago{other}; cooldown until {format_ts(last + policy.cooldown)}"
    return None


def _exclusion(s, now, p, phone_last, in_flight):
    """Return why this quote must not be a candidate, or None."""
    if s.status in CLOSED:
        return f"Quote is {s.status}"
    if now - s.created_at < p.min_age:
        return f"Quote sent {ago(now - s.created_at)} ago; waiting a day before nudging"
    if now - s.created_at > p.max_age:
        return f"Quote is {ago(now - s.created_at)} old; close it out?"
    if s.outbound_count >= p.max_touches:
        return f"{s.outbound_count} follow-ups with no response; close it out?"
    if s.phone in in_flight:
        return f"A message for this customer is already in the outbox ({in_flight[s.phone]})"
    last, via = phone_last.get(s.phone, (None, None))
    if last and now - last < p.cooldown:
        other = f" about {via}" if via != s.id else ""
        return f"Customer contacted {ago(now - last)} ago{other}; cooldown until {format_ts(last + p.cooldown)}"
    return None


def _tier(s, now, p):
    if s.unanswered_reply:
        channel = f" by {s.last_reply_channel}" if s.last_reply_channel else ""
        return 1, f"Customer replied{channel} {ago(now - s.last_reply)} ago; no response since"

    if s.last_view and now - s.last_view <= p.recent_view and s.views_since_outbound:
        since = "since last contact" if s.last_outbound else "and never contacted"
        return 2, f"Viewed {s.views_since_outbound}x {since}, most recently {ago(now - s.last_view)} ago"

    quiet_for = now - (s.last_outbound or s.created_at)
    contact = (f"no contact in {ago(quiet_for)}" if s.last_outbound
               else f"never contacted ({ago(quiet_for)} since sent)")
    if s.amount >= p.big_amount and quiet_for >= p.quiet:
        return 3, f"${s.amount:,} quote, {contact}"
    return 4, contact[0].upper() + contact[1:]


def build_followups(conn, now=DEFAULT_NOW, policy=Policy()):
    now = parse_ts(now) if isinstance(now, str) else now
    states = load_states(conn, now)

    # Cooldown is per customer: the latest outbound to a phone across all its quotes.
    phone_last = phone_last_outbound(states)
    # Only today's unsent messages hold a customer. Unsent ones from earlier days have
    # expired, so those customers roll over into today's candidates.
    in_flight = {r["customer_phone"]: r["quote_id"] for r in conn.execute(
        f"SELECT customer_phone, quote_id FROM outbox WHERE status IN ({', '.join('?' * len(ACTIVE_OUTBOX))})"
        " AND substr(created_at, 1, 10) = ?",
        (*ACTIVE_OUTBOX, format_ts(now)[:10]),
    )}

    candidates, excluded = [], []
    for s in states.values():
        why_not = _exclusion(s, now, policy, phone_last, in_flight)
        if why_not:
            excluded.append(Excluded(s, why_not))
        else:
            candidates.append(Candidate(s, *_tier(s, now, policy)))

    candidates.sort(key=lambda c: (c.tier, -c.state.amount, c.state.id))

    # The daily cap counts messages already drafted today, so drafting doesn't refill it.
    drafted_today = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE substr(created_at, 1, 10) = ?", (format_ts(now)[:10],)
    ).fetchone()[0]
    slots = max(policy.daily_cap - drafted_today, 0)

    # One candidate per customer: a second quote on the same phone waits its turn.
    ranked, best_for_phone = [], {}
    for c in candidates:
        if c.state.phone in best_for_phone:
            excluded.append(Excluded(c.state, f"Another quote for this customer ranks higher ({best_for_phone[c.state.phone]})"))
            continue
        best_for_phone[c.state.phone] = c.state.id
        c.rank = len(ranked) + 1
        c.over_cap = c.rank > slots
        ranked.append(c)

    excluded.sort(key=lambda e: e.state.id)
    return Followups(now=now, candidates=ranked, excluded=excluded, states=states)
