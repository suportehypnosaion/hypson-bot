"""
config.py — load all settings from .env once, expose as module-level
constants. The service is small enough that a config class would be
overkill; constants are easier to import and reason about.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Missing required env var {name!r}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


# ─── PSike DB ──────────────────────────────────────────────────
DB_HOST     = _required("DB_HOST")
DB_PORT     = int(os.getenv("DB_PORT", "3306"))
DB_USER     = _required("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")  # blank ok for dev
DB_NAME     = _required("DB_NAME")
# Timezone for the SQL session (e.g. "+05:30"). session_date is naive
# wall-clock time in the SCHEDULER's zone; NOW() must tick in that
# same zone for the join-window query to match. Set this whenever the
# MySQL server's own time_zone (often UTC on cloud VMs) differs from
# the zone sessions are created in. Empty = use the server default.
DB_TIME_ZONE = os.getenv("DB_TIME_ZONE", "").strip()

# ─── Browser & state paths ─────────────────────────────────────
BROWSER_PROFILE_DIR = os.path.abspath(
    os.getenv("BROWSER_PROFILE_DIR", "./browser-profile")
)
TRACKER_DB_PATH = os.path.abspath(
    os.getenv("TRACKER_DB_PATH", "./bot-state.sqlite")
)

# ─── Timing ────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
JOIN_EARLY_SECONDS    = int(os.getenv("JOIN_EARLY_SECONDS", "60"))
LEAVE_GRACE_SECONDS   = int(os.getenv("LEAVE_GRACE_SECONDS", "300"))
MAX_JOIN_ATTEMPTS     = int(os.getenv("MAX_JOIN_ATTEMPTS", "3"))
# Once others have joined and then ALL left again, the host leaves
# after this many seconds alone (buffer covers brief reconnects).
# 0 disables — host then stays until the scheduled end as before.
ALONE_LEAVE_SECONDS   = int(os.getenv("ALONE_LEAVE_SECONDS", "120"))

# ─── Browser ───────────────────────────────────────────────────
# "chrome" = system Google Chrome (required: Google blocks sign-in
# on Playwright's bundled Chromium with "This browser or app may
# not be secure"). Set to "" to force bundled Chromium.
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "chrome")

# True = no visible window (Chrome's "new headless" mode). If Meet
# ever misbehaves headless (degraded UI, join refused), set to false
# and run under Xvfb on servers instead.
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in ("1", "true", "yes")

# Set by the Dockerfile. Inside a container Chrome needs --no-sandbox
# (the kernel privileges its sandbox wants aren't available) and
# --disable-dev-shm-usage (container /dev/shm is tiny by default and
# Chrome crashes when it fills up).
IN_DOCKER = os.getenv("DOCKER", "").strip().lower() in ("1", "true", "yes")

# ─── Identity ──────────────────────────────────────────────────
# The central Workspace host account. Comes from .env only — used in
# logs and setup instructions, never hardcoded anywhere in the repo.
SUPORTE_EMAIL = os.getenv("SUPORTE_EMAIL", "") or "the host account"
