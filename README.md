# PSike Meet — Host Auto-Join Service

Automatically joins every PSike video session as the clinic's central
Google Workspace host account (configured via `SUPORTE_EMAIL` in
`.env`). Because PSike's backend creates every Meet space with
`autoRecordingGeneration=ON` + `autoTranscriptionGeneration=ON`,
Google starts recording + transcribing the moment a user **with
recording privilege** is in the call — that's the host. Therapist and
patient join on their own; recording is already running.

This is a separate service from the PSike FastAPI backend. It reads
the same database (PSike's `sessions` table) but writes nothing back.

---

## How it works

Every `POLL_INTERVAL_SECONDS` (default 30 s) it queries PSike's DB for
video-call sessions whose join window is open. For each one it launches
**headless real Google Chrome** (signed in as the host via a persistent
profile), joins the Meet muted with camera off, and stays until the
session is over. No visible window, no display server needed.

### Session lifecycle

```
start + JOIN_EARLY_SECONDS·(−1)   host joins      (default: 1 min AFTER start)
                                  → recording + transcription auto-start
therapist & patient join/leave    host sits silently (mic & cam off)
everyone else gone                host leaves after ALONE_LEAVE_SECONDS (default 1 min)
                                  → meeting ends → artifacts finalize in the
                                    host account's Drive
```

Backstops: the host always leaves by `scheduled end + LEAVE_GRACE_SECONDS`,
and exits early if the meeting is ended for everyone / it gets removed.

### Behavior details

- **Join timing** — `JOIN_EARLY_SECONDS` is seconds *before* start;
  negative means *after* start (e.g. `-60` = 1 minute after).
- **Reschedule-aware** — the tracker stores each occurrence's
  `session_date`; if a session is rescheduled after the host attended,
  the new time is treated as a new occurrence and joined again.
- **Retries** — a failed join is retried on later polls, up to
  `MAX_JOIN_ATTEMPTS` total attempts.
- **Signed-out protection** — if the browser profile has lost its
  Google session, the service refuses to join (an anonymous guest does
  NOT trigger auto-recording) and logs exactly what to fix.
- **Concurrent sessions** — each join clones the browser profile to a
  throwaway directory, so overlapping meetings don't fight over it.
- **Time handling** — all schedule comparisons run inside SQL against
  the DB server's `NOW()` (PSike stores `session_date` as naive local
  wall-clock time). The container's `TZ` must match the DB server's
  timezone — set it in `.env`.

---

## Configuration — everything in `.env`

One file controls the whole service. See `.env.example` for the full
annotated list:

| Variable | Default | Meaning |
|---|---|---|
| `DB_HOST/PORT/USER/PASSWORD/NAME` | — | PSike MySQL (read-only use) |
| `POLL_INTERVAL_SECONDS` | 30 | DB poll frequency |
| `JOIN_EARLY_SECONDS` | -60 | join offset; negative = after start |
| `LEAVE_GRACE_SECONDS` | 300 | stay past scheduled end (backstop) |
| `ALONE_LEAVE_SECONDS` | 60 | leave N s after everyone else left (0 = off) |
| `MAX_JOIN_ATTEMPTS` | 3 | retries for failed joins |
| `BROWSER_CHANNEL` | chrome | real Chrome; `msedge` also works |
| `HEADLESS` | true | invisible browser |
| `SUPORTE_EMAIL` | — | the central host account |
| `TZ` | — | container timezone — MUST equal the DB server's |
| `HOST_UID` | 1000 | owner uid of `browser-profile/` (build-time) |

Apply changes with `docker compose up -d` — a plain `restart` does
**not** re-read `.env`.

---

## Deployment (Docker)

> **Deploying to a server?** Follow the full step-by-step runbook in
> [DEPLOYMENT.md](DEPLOYMENT.md) — it covers profile transfer,
> ownership, `.env` setup, startup verification, and a
> symptom → fix table. The section below is the short version.

The image is self-contained: Python, **real Google Chrome** (required —
Google blocks sign-in on plain Chromium/automation builds), and all
dependencies. The VM only needs Docker + network access to the DB and
to Google.

### 1. One-time: produce the signed-in browser profile

Google sign-in needs a human and a visible browser, so it happens on a
machine with a screen (your laptop), never on the server:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m scripts.setup_profile     # sign in as the host account, finish 2FA
```

Cookies land in `./browser-profile/`. This folder **is a credential**
— it's git-ignored on purpose. Transfer it to the server manually:

```bash
rsync -av browser-profile/ user@SERVER:/opt/hypson-bot/browser-profile/
```

The server never sees a login screen or 2FA — the copied cookies *are*
the session.

### 2. On the server

```bash
cd /opt/hypson-bot
cp .env.example .env && nano .env    # DB creds, TZ, HOST_UID=$(id -u)
docker compose up -d --build
docker compose logs -f               # watch it join sessions
```

### Day-to-day commands

```bash
docker compose logs -f --tail 100   # live logs
docker compose up -d                # apply .env changes
docker compose up -d --build        # apply code changes
docker compose down                 # stop
docker compose down -v              # stop + wipe tracker memory
```

### When cookies expire (~every few weeks)

Logs will say `profile is NOT signed in`. Fix = repeat step 1:
sign in again on your laptop, `rsync` the profile to the server,
then `docker compose restart`.

---

## Testing end-to-end

1. In PSike, create a video-call session scheduled 2–3 minutes from now.
2. `docker compose logs -f` — about 1 minute after the scheduled start
   you'll see `→ Joining session N`, then `In meeting`.
3. Join the meeting yourself from any account. You should see the host
   as a participant, plus the red REC indicator and the
   "Transcribing" notice within seconds.
4. Leave the meeting. ~1 minute later the log shows the host leaving
   and `✓ Session N complete`.
5. After 5–30 minutes the recording, transcript and Gemini notes appear
   in the host account's Drive (Meet Recordings folder), and
   `POST /api/v1/sessions/<id>/transcript/fetch-from-drive` returns the
   transcript text.

---

## Troubleshooting

### "Missing required env var DB_HOST"
You skipped `cp .env.example .env`.

### Polls but never joins anything
No qualifying sessions: must be `session_type=video_call`,
status `scheduled`/`in_progress`, with a `meeting_url`. Time matching
happens in SQL against the DB's `NOW()`, so check the container `TZ`
matches the DB server's timezone if joins seem shifted.

### "profile is NOT signed in" in the logs
The profile has no (or expired) Google session. Re-run
`scripts/setup_profile.py` on a machine with a screen and copy
`browser-profile/` over again. Never let it join signed-out — an
anonymous participant doesn't trigger auto-recording.

### `Permission denied: /profile/browser-profile`
The container user's uid doesn't match the folder's owner. Set
`HOST_UID` in `.env` to `id -u` of the owner and
`docker compose up -d --build`. If you changed `HOST_UID` after the
first run, also wipe the old tracker volume once:
`docker compose down -v`.

### Joins but recording never starts
- Workspace admin: Apps → Google Workspace → Google Meet →
  Recording ON (and Transcripts ON). Requires Business Standard+.
- Confirm the participant tile shows the host account, not an
  anonymous guest (see signed-in issue above).

### "Camera not found" in logs/screenshots
Normal — the container has no camera/mic hardware. The host joins
with both switched off in software; recording is unaffected.

### A session failed and is never retried
After `MAX_JOIN_ATTEMPTS` failures a session is skipped permanently.
Force a retry:
```bash
docker exec hypson-bot python -c "
import sqlite3; c=sqlite3.connect('/data/bot-state.sqlite')
c.execute('DELETE FROM handled_sessions WHERE session_id=<ID>'); c.commit()"
```

### Meeting ends ~12 min after the host joins, nobody else came
That's Google ejecting an idle lone participant ("Are you still
there?"). Harmless — with real participants present it never happens.

---

## Folder layout

```
hypson-bot/
├── README.md
├── DEPLOYMENT.md              # server deployment runbook (Docker)
├── requirements.txt
├── Dockerfile                 # python-slim + real Google Chrome
├── docker-compose.yml         # volumes, shm_size, TZ
├── .env.example               # ALL config — copy to .env
├── bot/
│   ├── config.py              # env-loaded constants
│   ├── db.py                  # reads PSike's `sessions` (read-only)
│   ├── tracker.py             # SQLite: handled occurrences (+ reschedule detection)
│   ├── joiner.py              # Playwright: join/sit/leave one Meet
│   └── main.py                # scheduler loop
├── scripts/
│   └── setup_profile.py       # one-time interactive Google sign-in
├── browser-profile/           # signed-in Chrome profile — NEVER commit
└── bot-state.sqlite           # local venv runs only; Docker uses a volume
```
