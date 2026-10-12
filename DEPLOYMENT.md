# PSike Meet Bot — Server Deployment Guide (Docker)

Step-by-step runbook for deploying this service on a server/VM with
Docker. For what the service *does* and the full config reference,
see [README.md](README.md). Server path used throughout:
`/opt/hypson/hypson-bot` — adjust if yours differs.

---

## Prerequisites

**On the server:**
- Docker + the compose plugin (`docker compose version` works)
- Network access to the PSike MySQL host (port 3306) and to Google

**On a LINUX machine with a screen (one-time):**
- The signed-in Chrome profile — Google sign-in requires a human +
  visible browser, so it can NEVER be produced on the server directly.
  It must be generated on Linux (a desktop VM, or a laptop's Linux
  install/WSL2 is NOT enough — WSL2 is still a Windows Chrome install).
  A profile signed in on Windows or macOS encrypts its cookies with
  DPAPI/Keychain and CANNOT be decrypted on the Linux server — see
  step 1.1 and "Verify the profile before shipping" below.

---

## 1. First-time deployment

### 1.1 Generate the signed-in browser profile (LINUX machine)

Must run on Linux — see the prerequisites note above.

```bash
cd hypson-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m scripts.setup_profile     # sign in as the host account, finish 2FA
```

This fills `./browser-profile/` (~15 MB). **This folder is a
credential** — treat it like a password file.

### 1.1a Verify the profile BEFORE shipping it anywhere

Run this after every `setup_profile` and before every deploy/rsync of
`browser-profile/` — it catches both failure modes this bot has hit in
production (wrong sub-profile, non-Linux profile) without needing to
launch a browser or wait for a real join attempt:

```bash
python -m scripts.verify_profile
```

It prints every Chrome sub-profile found, which one (if any) matches
`SUPORTE_EMAIL`, whether that sub-profile has real Google auth cookies,
and whether the profile dir was made on Windows/macOS (`os_crypt.encrypted_key`
present — fails on Linux). Exits non-zero unless a Linux-made, signed-in
match is found. Do not proceed to step 1.2 if it fails.

### 1.2 Copy the project to the server (LOCAL machine)

```bash
rsync -av \
  --exclude venv --exclude .git --exclude .env \
  --exclude bot-state.sqlite \
  ./  root@SERVER:/opt/hypson/hypson-bot/
```

This includes `browser-profile/`. The excludes matter:
- `.env` — the server keeps its OWN `.env` (different DB host, UID…)
- `venv/`, `bot-state.sqlite` — local-only artifacts

### 1.3 Create the server's `.env` (SERVER)

```bash
cd /opt/hypson/hypson-bot
cp .env.example .env
nano .env
```

The values that differ per environment:

| Variable | Set to |
|---|---|
| `DB_HOST` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | the PSike MySQL server, as reachable FROM this VM |
| `TZ` | the timezone sessions are scheduled in (e.g. `Asia/Kolkata`) |
| `DB_TIME_ZONE` | same zone as UTC offset (e.g. `+05:30`) — needed when the MySQL server itself runs in UTC |
| `HOST_UID` | uid that will own `browser-profile/` on this server (next step). `1000` is fine. |

### 1.4 Fix profile ownership (SERVER)

The container runs as a non-root user with uid = `HOST_UID`. If you
copied the files as root, the container cannot read them:

```bash
chown -R 1000:1000 browser-profile/     # must match HOST_UID in .env
chmod -R u+rwX browser-profile/
```

### 1.5 Build and start (SERVER)

```bash
docker compose up -d --build
docker compose logs -f
```

### 1.6 Verify — the three startup lines

A healthy start ALWAYS begins with exactly these lines, in this order:

```
✓ DB connected successfully — <host>:3306/psike (server NOW() = <local wall-clock time>)
✓ Browser profile found — /profile/browser-profile (N entries)
PSike Meet host auto-join starting. Poll every 30s. ...
```

Check each one:

1. **`✓ DB connected` missing entirely, logs start with "auto-join
   starting"** → the image was built from OLD code. Your `bot/` folder
   on the server is stale — redo step 1.2, then
   `docker compose up -d --build`.
2. **`server NOW()` shows the wrong wall-clock time** → timezone
   mismatch; fix `DB_TIME_ZONE` / `TZ` in `.env` and
   `docker compose up -d` (sessions will never match otherwise).
3. **`✗ DB connection FAILED`** → the logged MySQL error tells you
   which: wrong host/credentials, or the VM's firewall blocks 3306.
   The bot retries every 30 s — fix `.env`, then `docker compose up -d`.
4. **`✗ Browser profile MISSING`** → the log says which case:
   missing folder (redo 1.2), permission denied (redo 1.4 — the
   message prints the exact `chown` command), or a wrongly-nested /
   empty copy (check `ls browser-profile/` shows `Default/`,
   `Local State`, … — not another `browser-profile/` folder).
   The bot retries every 30 s; fixing the folder on the host is
   picked up WITHOUT a restart.

---

## 2. Updating the code on the server

> The #1 deployment mistake: updating only some files, then rebuilding
> an image from stale code. ALWAYS re-run the rsync from step 1.2 —
> never copy individual files by hand.

```bash
# LOCAL: same rsync as step 1.2
rsync -av --exclude venv --exclude .git --exclude .env \
  --exclude bot-state.sqlite \
  ./  root@SERVER:/opt/hypson/hypson-bot/

# SERVER: rebuild (a plain restart does NOT pick up code or .env changes)
cd /opt/hypson/hypson-bot
docker compose up -d --build
docker compose logs -f      # confirm the three startup lines again
```

---

## 3. When Google cookies expire (~every few weeks)

Symptom in the logs: `profile is NOT signed in`.

```bash
# LINUX machine: sign in again
python -m scripts.setup_profile

# LINUX machine: verify BEFORE shipping (see step 1.1a) — must exit 0
python -m scripts.verify_profile

# LOCAL: ship only the profile
rsync -av browser-profile/ root@SERVER:/opt/hypson/hypson-bot/browser-profile/

# SERVER: restore ownership (rsync as root resets it), then restart
chown -R 1000:1000 /opt/hypson/hypson-bot/browser-profile
docker compose restart hypson-bot
```

---

## 4. Day-to-day commands (SERVER)

```bash
docker compose logs -f --tail 100   # live logs
docker compose up -d                # apply .env changes
docker compose up -d --build        # apply code changes
docker compose down                 # stop
docker compose down -v              # stop + forget which sessions were handled
docker image prune -f               # reclaim space from old image layers
```

---

## 5. Quick symptom → fix table

| Symptom in `docker compose logs` | Cause | Fix |
|---|---|---|
| No `✓` lines at all at startup | Server has old `bot/` code | Re-rsync everything (step 2) |
| `✗ DB connection FAILED ... No route to host` / timeout | Firewall, wrong `DB_HOST` | Fix `.env`; test from VM: `nc -zv <DB_HOST> 3306` |
| `✗ DB connection FAILED ... Access denied` | Wrong user/password, or MySQL user not allowed from this VM's IP | Fix `.env`; check MySQL grants |
| `FileNotFoundError: /profile/browser-profile` | Old `docker-compose.yml` without the volume mount | Re-rsync everything (step 2) |
| `PermissionError: /profile/browser-profile` or `✗ ... permission denied` | Profile owned by root, container user can't read it | `chown -R <HOST_UID> browser-profile/` (step 1.4) |
| `server NOW()` ≠ local wall-clock | Timezone mismatch | Set `DB_TIME_ZONE`/`TZ` in `.env`, `docker compose up -d` |
| Polls forever, never joins | No qualifying sessions, or timezone (above) | Session must be `video_call`, `scheduled`/`in_progress`, with `meeting_url` |
| `profile is NOT signed in` | Google cookies expired | Refresh the profile (step 3) |
| `Browser profile is not signed in as <email>` | No sub-profile in `browser-profile/` matches `SUPORTE_EMAIL`, or it has no Google auth cookies (often: profile was made on Windows/macOS, or the account landed in a Workspace-managed sub-profile like "Profile 1" that never got cookies) | Run `python -m scripts.verify_profile` to see exactly which sub-profile/cookie/os_crypt check failed, then redo step 1.1 (and 1.1a) ON A LINUX MACHINE |
