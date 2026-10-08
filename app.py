"""
Self-hosted email open tracker.

How it works:
  1. Create a tracked email on the dashboard (recipient + subject).
  2. You get a unique tracking-pixel image URL.
  3. Insert that image URL into your Gmail compose window
     (Insert photo -> Web address (URL) -> paste -> Insert).
  4. When the recipient opens the mail, their mail app loads the image
     and the open is logged with timestamp, IP and device info.

Routes:
  GET  /            dashboard (password protected, see DASHBOARD_PASSWORD)
  GET  /new         form to create a tracked email
  POST /new         creates it, shows the pixel snippet
  POST /delete/<id> deletes a tracked email and its opens
  GET  /p/<id>      the tracking pixel itself (PUBLIC - mail apps fetch this)
  GET  /health      health check
"""

import base64
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, g, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "") or os.urandom(32).hex()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "tracker.db"))
ADMIN_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# 1x1 transparent PNG
PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()
    db.close()


init_db()


def check_auth():
    if not ADMIN_PASSWORD:
        return True
    return session.get("authed", False)


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not check_auth():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    if ADMIN_PASSWORD and check_auth():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if ADMIN_PASSWORD and request.form.get("password", "") == ADMIN_PASSWORD:
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Wrong password. Try again."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def fmt_time(iso):
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ).strftime("%d %b %Y, %I:%M %p")


def describe_ua(ua):
    """Short human-friendly label for a User-Agent string."""
    ua = ua or ""
    low = ua.lower()
    if not ua:
        return "—"
    if "googleimageproxy" in low:
        return "Gmail (Google image proxy)"
    if "yahoo" in low and "proxy" in low:
        return "Yahoo Mail (proxy)"
    labels = []
    if any(m in low for m in ("iphone", "android", "mobile")):
        labels.append("Mobile")
    else:
        labels.append("Desktop")
    for name in ("Outlook", "Thunderbird", "Chrome", "Safari", "Firefox", "Edge"):
        if name.lower() in low:
            labels.append(name)
            break
    if "applewebkit" in low and "mail" in low:
        labels.append("Apple Mail")
    return " · ".join(labels)


@app.route("/")
@requires_auth
def dashboard():
    db = get_db()
    emails = db.execute("SELECT * FROM emails ORDER BY created_at DESC").fetchall()
    rows = []
    for e in emails:
        stats = db.execute(
            "SELECT COUNT(*) AS c, MIN(opened_at) AS first, MAX(opened_at) AS last"
            " FROM opens WHERE email_id = ?",
            (e["id"],),
        ).fetchone()
        rows.append(
            {
                "id": e["id"],
                "recipient": e["recipient"],
                "subject": e["subject"] or "—",
                "created": fmt_time(e["created_at"]),
                "opens": stats["c"],
                "first": fmt_time(stats["first"]),
                "last": fmt_time(stats["last"]),
                "opened": stats["c"] > 0,
                "pixel_url": url_for("pixel", tid=e["id"], _external=True),
            }
        )
    total = len(rows)
    opened_count = sum(1 for r in rows if r["opened"])
    open_rate = round(opened_count / total * 100) if total else 0
    return render_template(
        "dashboard.html",
        rows=rows,
        no_auth=not ADMIN_PASSWORD,
        total=total,
        opened_count=opened_count,
        open_rate=open_rate,
    )


@app.route("/email/<tid>")
@requires_auth
def email_detail(tid):
    db = get_db()
    e = db.execute("SELECT * FROM emails WHERE id = ?", (tid,)).fetchone()
    if not e:
        return redirect(url_for("dashboard"))
    opens = db.execute(
        "SELECT * FROM opens WHERE email_id = ? ORDER BY opened_at DESC", (tid,)
    ).fetchall()
    rows = [
        {
            "at": fmt_time(o["opened_at"]),
            "ip": o["ip"] or "—",
            "via": describe_ua(o["user_agent"]),
            "ua": o["user_agent"] or "—",
        }
        for o in opens
    ]
    return render_template(
        "email_detail.html",
        recipient=e["recipient"],
        subject=e["subject"] or "—",
        created=fmt_time(e["created_at"]),
        pixel_url=url_for("pixel", tid=tid, _external=True),
        opens=rows,
    )


@app.route("/new", methods=["GET", "POST"])
@requires_auth
def new_email():
    if request.method == "POST":
        recipient = request.form.get("recipient", "").strip()
        subject = request.form.get("subject", "").strip()
        if not recipient:
            return render_template("new.html", error="Recipient is required.")
        tid = uuid.uuid4().hex
        db = get_db()
        db.execute(
            "INSERT INTO emails (id, recipient, subject, created_at) VALUES (?, ?, ?, ?)",
            (tid, recipient, subject, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
        return render_template(
            "snippet.html",
            recipient=recipient,
            subject=subject or "—",
            pixel_url=url_for("pixel", tid=tid, _external=True),
        )
    return render_template("new.html")


@app.route("/delete/<tid>", methods=["POST"])
@requires_auth
def delete_email(tid):
    db = get_db()
    db.execute("DELETE FROM emails WHERE id = ?", (tid,))
    db.commit()
    return redirect(url_for("dashboard"))


@app.route("/p/<path:tid>")
def pixel(tid):
    # Allow an optional .png suffix (some mail clients like image-looking URLs)
    tid = tid[:-4] if tid.endswith(".png") else tid
    try:
        db = sqlite3.connect(DB_PATH)
        row = db.execute("SELECT id FROM emails WHERE id = ?", (tid,)).fetchone()
        if row:
            fwd = request.headers.get("X-Forwarded-For", "")
            ip = (fwd.split(",")[0].strip() if fwd else (request.remote_addr or ""))
            db.execute(
                "INSERT INTO opens (email_id, opened_at, ip, user_agent)"
                " VALUES (?, ?, ?, ?)",
                (
                    tid,
                    datetime.now(timezone.utc).isoformat(),
                    ip,
                    request.headers.get("User-Agent", "")[:300],
                ),
            )
            db.commit()
        db.close()
    except Exception:
        pass  # never break the pixel response
    resp = Response(PIXEL_PNG, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
