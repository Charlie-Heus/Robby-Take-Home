"""Local UI. Routes stay thin: they call takehome functions and render the result."""
import json
import os
from datetime import datetime, timezone

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from takehome import outbox as ob
from takehome.db import DEFAULT_DB, connect, init_db
from takehome.ingest import ROOT, ingest_events, ingest_quotes
from takehome.policy import DEFAULT_NOW, build_followups, format_ts, parse_ts

APPROVER = "owner"  # no auth in the MVP; a real app records the signed-in user


def create_app(db_path=None, provider=None):
    app = Flask(__name__)
    app.secret_key = "local-dev-only"
    app.config["DB"] = db_path or os.environ.get("TAKEHOME_DB", DEFAULT_DB)
    provider = provider or ob.FakeSmsProvider(log_path=ROOT / "sent_messages.jsonl")

    def db():
        conn = connect(app.config["DB"])
        init_db(conn)
        return conn

    def current_now():
        """The "now" the policy runs as of. Comes from ?now=, interpreted as UTC. Parsed once per request."""
        if "now" not in g:
            g.now = _parse_now()
        return g.now

    def _parse_now():
        raw = request.args.get("now")
        if not raw:
            return parse_ts(DEFAULT_NOW)
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            flash(f"Couldn't read now={raw!r}; using the default.", "error")
            return parse_ts(DEFAULT_NOW)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    @app.template_filter("ts")
    def ts_filter(value):
        if value is None:
            return "—"
        dt = parse_ts(value) if isinstance(value, str) else value
        return dt.strftime("%b %d %H:%M")

    @app.context_processor
    def globals_():
        now = current_now() if request.endpoint else parse_ts(DEFAULT_NOW)
        return {"now": now, "now_param": now.strftime("%Y-%m-%dT%H:%M")}

    @app.get("/")
    def index():
        conn = db()
        followups = build_followups(conn, current_now())
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("quotes", "events", "outbox")}
        # Today's messages only: unsent ones from earlier days have expired and those
        # customers are back in the candidates. Drafts are inserted in rank order, so id ASC = priority.
        messages = conn.execute(
            "SELECT o.*, q.customer_name FROM outbox o JOIN quotes q ON q.id = o.quote_id"
            " WHERE substr(o.created_at, 1, 10) = ? ORDER BY o.id", (format_ts(current_now())[:10],)).fetchall()
        conn.close()
        return render_template("index.html", f=followups, counts=counts, messages=messages)

    @app.get("/quotes")
    def all_quotes():
        conn = db()
        followups = build_followups(conn, current_now())
        conn.close()
        states = sorted(followups.states.values(), key=lambda s: s.id)
        return render_template("quotes.html", states=states)

    def back():
        return redirect(url_for("index", now=request.args.get("now")) + "#outbox")

    @app.post("/drafts")
    def generate():
        conn = db()
        r = ob.generate_drafts(conn, current_now())
        conn.close()
        flash(f"Drafts: {r['created']} created, {r['skipped']} already existed.", "info")
        return back()

    @app.post("/outbox/<int:oid>/approve")
    def approve(oid):
        conn = db()
        ok = ob.approve(conn, oid, APPROVER, current_now())
        conn.close()
        flash(f"Message {oid} approved." if ok else f"Message {oid} is not a draft; nothing changed.",
              "info" if ok else "error")
        return back()

    @app.post("/outbox/<int:oid>/discard")
    def discard(oid):
        conn = db()
        ok = ob.discard(conn, oid)
        conn.close()
        flash(f"Message {oid} discarded." if ok else f"Message {oid} is not a draft; nothing changed.",
              "info" if ok else "error")
        return back()

    @app.post("/outbox/<int:oid>/send")
    def send(oid):
        conn = db()
        outcome, detail = ob.send(conn, oid, provider, current_now())
        conn.close()
        # A successful send shows in the outbox row itself; only surface problems.
        if outcome != "sent":
            flash(f"Message {oid}: {outcome}. {detail}", "error")
        return back()

    @app.get("/quotes/<quote_id>")
    def quote_detail(quote_id):
        conn = db()
        quote = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if quote is None:
            abort(404)
        now = current_now()
        followups = build_followups(conn, now)
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE quote_id = ? ORDER BY timestamp, event_id", (quote_id,))]
        outbox = conn.execute("SELECT * FROM outbox WHERE quote_id = ? ORDER BY id", (quote_id,)).fetchall()
        conn.close()
        for e in events:
            e["after_now"] = e["timestamp"] > format_ts(now)
            e["raw_pretty"] = json.dumps(json.loads(e["raw"]), indent=2)
        candidate = next((c for c in followups.candidates if c.state.id == quote_id), None)
        excluded = next((x for x in followups.excluded if x.state.id == quote_id), None)
        return render_template(
            "quote.html", quote=quote, state=followups.states.get(quote_id),
            candidate=candidate, excluded=excluded, events=events, outbox=outbox,
        )

    @app.post("/ingest")
    def ingest():
        conn = db()
        q = ingest_quotes(conn, json.loads((ROOT / "seed" / "quotes.json").read_text()))
        with open(ROOT / "seed" / "events.jsonl") as fh:
            e = ingest_events(conn, fh)
        conn.close()
        for name, report in (("Quotes", q), ("Events", e)):
            flash(f"{name}: " + ", ".join(f"{k}={v}" for k, v in report["counts"].items()), "info")
            for where, why in report["rejected"]:
                flash(f"{name} rejected {where}: {why}", "error")
            for lineno, event_id in report.get("conflicts", []):
                flash(f"Conflict on line {lineno}: {event_id} differs from stored copy; kept the first", "error")
        return redirect(url_for("index", now=request.args.get("now")))

    @app.post("/reset")
    def reset():
        # Drop and recreate (not just DELETE), so schema changes take effect on a dev database.
        conn = connect(app.config["DB"])
        with conn:
            for table in ("outbox", "events", "quotes"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        init_db(conn)
        conn.close()
        provider.reset()
        flash("Database and fake SMS log cleared.", "info")
        return redirect(url_for("index", now=request.args.get("now")))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5001)  # 5000 is taken by AirPlay Receiver on macOS
