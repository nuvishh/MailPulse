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
import csv
import io
import os
import sqlite3
import urllib.parse
import urllib.request
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
CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    email_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    target_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id TEXT NOT NULL,
    clicked_at TEXT NOT NULL,
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    FOREIGN KEY (link_id) REFERENCES links(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
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


def migrate():
    """Add newer columns to existing databases."""
    db = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in db.execute("PRAGMA table_info(emails)").fetchall()]
    if "followup_days" not in cols:
        db.execute("ALTER TABLE emails ADD COLUMN followup_days INTEGER NOT NULL DEFAULT 0")
    if "followup_sent_at" not in cols:
        db.execute("ALTER TABLE emails ADD COLUMN followup_sent_at TEXT DEFAULT NULL")
    db.commit()
    db.close()


init_db()
migrate()


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


def read_settings():
    """All key/value settings as a dict (works outside request context)."""
    db = sqlite3.connect(DB_PATH)
    try:
        rows = db.execute("SELECT key, value FROM settings").fetchall()
    except sqlite3.OperationalError:
        rows = []
    db.close()
    return {k: v for k, v in rows}


def get_setting(key, default=""):
    s = read_settings()
    return s.get(key, default) or default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def send_telegram(text):
    """Send a Telegram message. Returns (ok, info). Never raises."""
    token = get_setting("tg_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = get_setting("tg_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False, "Telegram not configured"
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return (resp.status == 200), "sent" if resp.status == 200 else "http %s" % resp.status
    except Exception as e:
        return False, str(e)[:200]


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else (request.remote_addr or ""))


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
    link_rows = []
    for l in db.execute("SELECT * FROM links WHERE email_id = ? ORDER BY created_at", (tid,)):
        cstats = db.execute(
            "SELECT COUNT(*) AS c, MAX(clicked_at) AS last FROM clicks WHERE link_id = ?",
            (l["id"],),
        ).fetchone()
        link_rows.append(
            {
                "label": l["label"],
                "target_url": l["target_url"],
                "track_url": url_for("click_link", lid=l["id"], _external=True),
                "clicks": cstats["c"],
                "last_click": fmt_time(cstats["last"]),
            }
        )
    return render_template(
        "email_detail.html",
        recipient=e["recipient"],
        subject=e["subject"] or "—",
        created=fmt_time(e["created_at"]),
        pixel_url=url_for("pixel", tid=tid, _external=True),
        opens=rows,
        links=link_rows,
        followup_days=e["followup_days"] or 0,
    )


@app.route("/c/<lid>")
def click_link(lid):
    target = None
    link_row = None
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        link_row = db.execute("SELECT * FROM links WHERE id = ?", (lid,)).fetchone()
        if link_row:
            target = link_row["target_url"]
            db.execute(
                "INSERT INTO clicks (link_id, clicked_at, ip, user_agent) VALUES (?, ?, ?, ?)",
                (lid, datetime.now(timezone.utc).isoformat(), client_ip(),
                 request.headers.get("User-Agent", "")[:300]),
            )
            db.commit()
        db.close()
    except Exception:
        pass
    if link_row:
        try:
            email_row = None
            db2 = sqlite3.connect(DB_PATH)
            db2.row_factory = sqlite3.Row
            email_row = db2.execute("SELECT * FROM emails WHERE id = ?", (link_row["email_id"],)).fetchone()
            db2.close()
            who = email_row["recipient"] if email_row else "?"
            send_telegram("🔗 Link clicked\n👤 %s\n🏷️ %s\n🔗 %s" % (who, link_row["label"], link_row["target_url"]))
        except Exception:
            pass
    if target:
        return redirect(target)
    return Response("Link not found", 404)


@app.route("/export")
@requires_auth
def export_csv():
    db = get_db()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["recipient", "subject", "event", "time_ist", "ip", "device", "link_label", "link_url"])
    for e in db.execute("SELECT * FROM emails ORDER BY created_at DESC"):
        for o in db.execute("SELECT * FROM opens WHERE email_id = ? ORDER BY opened_at", (e["id"],)):
            w.writerow([e["recipient"], e["subject"], "open", fmt_time(o["opened_at"]),
                        o["ip"], describe_ua(o["user_agent"]), "", ""])
        for l in db.execute("SELECT * FROM links WHERE email_id = ?", (e["id"],)):
            for c in db.execute("SELECT * FROM clicks WHERE link_id = ? ORDER BY clicked_at", (l["id"],)):
                w.writerow([e["recipient"], e["subject"], "click", fmt_time(c["clicked_at"]),
                            c["ip"], describe_ua(c["user_agent"]), l["label"], l["target_url"]])
    resp = Response(out.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=mailtracker-export.csv"
    return resp


@app.route("/settings", methods=["GET", "POST"])
@requires_auth
def settings_page():
    msg = None
    if request.method == "POST":
        set_setting("tg_bot_token", request.form.get("tg_bot_token", "").strip())
        set_setting("tg_chat_id", request.form.get("tg_chat_id", "").strip())
        if "test" in request.form:
            ok, info = send_telegram("✅ MailPulse test message — notifications are working!")
            msg = ("Test message sent!" if ok else "Failed: " + info)
        else:
            msg = "Settings saved."
    cron_url = url_for("check_followups", _external=True) + "?key=" + (ADMIN_PASSWORD or "SET_A_PASSWORD_FIRST")
    return render_template(
        "settings.html",
        tg_bot_token=get_setting("tg_bot_token"),
        tg_chat_id=get_setting("tg_chat_id"),
        msg=msg,
        cron_url=cron_url,
    )


@app.route("/api/check-followups")
def check_followups():
    # Protected by the dashboard password as a shared key (for external cron services)
    if not ADMIN_PASSWORD or request.args.get("key", "") != ADMIN_PASSWORD:
        return Response("forbidden", 403)
    now = datetime.now(timezone.utc)
    checked, reminded = 0, 0
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        for e in db.execute(
            "SELECT * FROM emails WHERE followup_days > 0 AND followup_sent_at IS NULL"
        ).fetchall():
            checked += 1
            created = datetime.fromisoformat(e["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            opens = db.execute("SELECT COUNT(*) AS c FROM opens WHERE email_id = ?", (e["id"],)).fetchone()["c"]
            if opens == 0 and (now - created).days >= (e["followup_days"] or 0):
                ok, _ = send_telegram(
                    "⏰ Follow-up: %s hasn't opened '%s' in %d days."
                    % (e["recipient"], e["subject"] or "your mail", e["followup_days"])
                )
                if ok:
                    db.execute("UPDATE emails SET followup_sent_at = ? WHERE id = ?",
                               (now.isoformat(), e["id"]))
                    reminded += 1
        db.commit()
        db.close()
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:200]}, 500
    return {"ok": True, "checked": checked, "reminded": reminded}


@app.route("/new", methods=["GET", "POST"])
@requires_auth
def new_email():
    if request.method == "POST":
        recipient = request.form.get("recipient", "").strip()
        subject = request.form.get("subject", "").strip()
        if not recipient:
            return render_template("new.html", error="Recipient is required.")
        try:
            followup_days = max(0, min(30, int(request.form.get("followup_days", "0") or 0)))
        except ValueError:
            followup_days = 0
        links = []
        for line in request.form.get("links", "").splitlines():
            if "|" in line:
                label, url = line.split("|", 1)
                label, url = label.strip(), url.strip()
                if url.startswith("http"):
                    links.append((label or url, url))
        tid = uuid.uuid4().hex
        db = get_db()
        db.execute(
            "INSERT INTO emails (id, recipient, subject, created_at, followup_days)"
            " VALUES (?, ?, ?, ?, ?)",
            (tid, recipient, subject, datetime.now(timezone.utc).isoformat(), followup_days),
        )
        link_rows = []
        for label, url in links:
            lid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO links (id, email_id, label, target_url, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (lid, tid, label, url, datetime.now(timezone.utc).isoformat()),
            )
            link_rows.append(
                {"label": label, "url": url, "track_url": url_for("click_link", lid=lid, _external=True)}
            )
        db.commit()
        return render_template(
            "snippet.html",
            recipient=recipient,
            subject=subject or "—",
            pixel_url=url_for("pixel", tid=tid, _external=True),
            links=link_rows,
            followup_days=followup_days,
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
    email_row = None
    notify = False
    try:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        email_row = db.execute("SELECT * FROM emails WHERE id = ?", (tid,)).fetchone()
        if email_row:
            last = db.execute(
                "SELECT MAX(opened_at) AS m, COUNT(*) AS c FROM opens WHERE email_id = ?",
                (tid,),
            ).fetchone()
            ua = request.headers.get("User-Agent", "")[:300]
            db.execute(
                "INSERT INTO opens (email_id, opened_at, ip, user_agent)"
                " VALUES (?, ?, ?, ?)",
                (tid, datetime.now(timezone.utc).isoformat(), client_ip(), ua),
            )
            db.commit()
            # Notify on first open, or if the previous open was >5 min ago (debounce)
            notify = True
            if last["m"]:
                last_dt = datetime.fromisoformat(last["m"])
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                gap = (datetime.now(timezone.utc) - last_dt).total_seconds()
                notify = gap > 300
            open_no = (last["c"] or 0) + 1
        db.close()
    except Exception:
        pass  # never break the pixel response
    if email_row and notify:
        try:
            subj = email_row["subject"] or "no subject"
            send_telegram(
                "📬 Mail opened #%d\n👤 %s\n✉️ %s\n🕒 %s"
                % (open_no, email_row["recipient"], subj, fmt_time(datetime.now(timezone.utc).isoformat()))
            )
        except Exception:
            pass
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
