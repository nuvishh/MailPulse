# 📬 MailPulse
_Know what happens after Send._


Know when someone opens your email. A tiny self-hosted email open tracker:
create a tracked email on the dashboard, get a unique tracking-pixel image URL,
insert it into your Gmail, and see opens on the dashboard.

No branding, no signature, no third party — your server, your data.

## How it works

1. You create a tracked email on the dashboard (recipient + subject).
2. You get a unique 1×1 invisible tracking-pixel image URL.
3. In Gmail compose: **Insert photo (🖼️) → Web address (URL) → paste the URL → Insert** (set size to Small).
4. When the recipient opens the mail, their mail app loads the image and the
   open is logged with timestamp, IP and device info.

## Deploy on Render (free)

1. Push this folder to a GitHub repo (e.g. `mail-tracker`).
2. Go to [render.com](https://render.com) → **New + → Web Service** → connect the repo.
   - Or use the `render.yaml` in this repo: **New + → Blueprint** → select the repo.
3. Render auto-generates a `DASHBOARD_PASSWORD` (see `render.yaml`). Find it under
   **Environment** after deploy — you'll need it to open the dashboard.
4. Deploy. Your public URL will look like `https://mail-tracker-xxxx.onrender.com`.

Open `https://<your-url>/` → log in with username `admin` and the generated password.

## Environment variables

| Variable             | Purpose                                              | Default        |
|----------------------|------------------------------------------------------|----------------|
| `DASHBOARD_PASSWORD` | Password for the dashboard (username is `admin`)     | _(none = open)_|
| `PORT`               | Port to listen on                                    | `5000`         |
| `DB_PATH`            | SQLite file location                                 | `./tracker.db` |

## Run locally

```bash
pip install -r requirements.txt
DASHBOARD_PASSWORD=secret python app.py
# open http://localhost:5000  (pixel URLs will use localhost — for real
# tracking you need the public Render URL)
```

## Notes & limitations

- **Image blocking:** if the recipient's mail app blocks images, the open won't
  register. Tracking is reliable in most cases but not 100%.
- **Gmail image proxy:** Gmail loads images through its own proxy; the first
  open is still recorded, but the IP you see will be Google's, not the
  recipient's. Timestamps remain accurate.
- **Apple Mail Privacy Protection** may pre-load images and cause false opens.
- **SQLite on Render free:** the database file lives on ephemeral disk — it
  survives restarts/sleeps but is wiped on redeploys. For permanent storage,
  move to a managed Postgres later.
- **Free-tier sleep:** Render's free tier sleeps after inactivity; the first
  pixel load after sleep may take ~30s (cold start). Usually fine for
  open tracking.
