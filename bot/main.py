"""
main.py — the scheduler loop.

Architecture:
  Every POLL_INTERVAL_SECONDS, ask PSike's DB which video_call sessions
  are happening now (or about to). For each one we haven't already
  handled in this service's tracker, kick off a detached background
  task that joins it as the host account. The task runs to completion
  independently of the scheduler — we don't await it — so the scheduler
  can keep polling and discover additional sessions that start mid-call.

  Concurrency: each session = one browser launch ≈ 500 MB RAM. For
  typical PSike use (1-3 concurrent sessions per clinic) this is fine
  on a 2-4 GB VM. Higher fan-out would need per-session resource
  budgeting.

To run:
  python -m bot.main

Stop with Ctrl-C — in-flight join tasks will be cancelled cleanly.
"""

import asyncio
import logging
import os
import signal
from typing import Set

from . import config
from .db import check_connection, find_active_sessions, get_session_state
from .joiner import join_meet
from .tracker import (
    is_already_handled,
    mark_joining,
    mark_joined,
    mark_left,
    mark_failed,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("psike-meet-bot")


# In-process set of OCCURRENCES currently being joined, keyed by
# (session_id, session_date, meeting_url). Without this, the scheduler could
# re-fire a join during the brief window between "create task" and
# "mark_joining writes to SQLite". Keying on the occurrence (not the id) lets
# the bot join a changed (relinked/rescheduled) occurrence while an earlier,
# still-occupied attendance of the same session is finishing.
_inflight: Set[str] = set()


def _occ_key(session: dict) -> str:
    """Identity for one occurrence of a session — a changed start time or
    meeting link yields a different key, so it's treated as a fresh join."""
    return (
        f"{session['session_id']}|"
        f"{session.get('session_date') or ''}|"
        f"{session.get('meeting_url') or ''}"
    )


def _make_recheck(session_id: int, joined_url: str):
    """Build the callable join_meet polls (in a worker thread) to learn whether
    the session changed while we're in the call. Returns True when the status
    became cancelled/no-show/completed (no longer scheduled/in_progress) or the
    meeting_url changed — i.e. time to consider leaving."""
    def _check() -> bool:
        st = get_session_state(session_id)
        if st is None:
            return True  # row deleted
        status = str(st.get("status") or "").upper()
        if status not in ("SCHEDULED", "IN_PROGRESS"):
            return True  # cancelled / no_show / completed
        return (st.get("meeting_url") or "") != joined_url
    return _check


async def handle_session(session: dict) -> None:
    sid          = session["session_id"]
    meeting_url  = session["meeting_url"]
    duration_min = session.get("duration_minutes") or 60
    # The occurrence key: same session_id + changed date OR changed meeting
    # link = a fresh occurrence to join (so the bot also joins the changed
    # meeting).
    date_key     = str(session.get("session_date") or "")
    occ_key      = _occ_key(session)

    if occ_key in _inflight or is_already_handled(sid, date_key, meeting_url):
        return
    _inflight.add(occ_key)

    try:
        logger.info(
            f"→ Joining session {sid} ({meeting_url}) for "
            f"~{duration_min} min as {config.SUPORTE_EMAIL}"
        )
        mark_joining(sid, date_key, meeting_url)

        # The scheduled session length. The bot stays through it and then keeps
        # staying as long as people are in the call, leaving once they ALL
        # leave; if nobody ever joins it closes at the scheduled end. (See
        # _stay_until_over.) No timezone math here — the old session_date vs
        # local-"now" comparison clamped the stay to a 60s floor.
        scheduled_seconds = duration_min * 60

        success = await join_meet(
            meeting_url,
            scheduled_seconds,
            on_joined=lambda: mark_joined(sid),
            recheck=_make_recheck(sid, meeting_url),
        )

        if success:
            mark_left(sid)
            logger.info(f"✓ Session {sid} complete — host left cleanly.")
        else:
            mark_failed(sid)
            logger.error(f"✗ Session {sid} failed.")
    except Exception:
        mark_failed(sid)
        logger.exception(f"Unhandled error for session {sid}")
    finally:
        _inflight.discard(occ_key)


async def wait_for_db(stop_event: asyncio.Event) -> bool:
    """
    Verify DB connectivity at startup, BEFORE the scheduler loop runs.

    On success logs a clear confirmation (including the server's NOW(),
    so timezone problems are visible immediately). On failure logs the
    full error and retries every POLL_INTERVAL_SECONDS — the container
    keeps running and recovers by itself once the DB is reachable.
    Returns False only if shutdown was requested while waiting.
    """
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        try:
            # Sync SQLAlchemy call — fine here, nothing else is
            # running on the loop yet and connect_timeout bounds it.
            server_now = check_connection()
            logger.info(
                f"✓ DB connected successfully — "
                f"{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME} "
                f"(server NOW() = {server_now})"
            )
            return True
        except Exception as e:
            logger.error(
                f"✗ DB connection FAILED (attempt {attempt}) — "
                f"{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}: {e}"
            )
            logger.error(
                f"  Retrying in {config.POLL_INTERVAL_SECONDS}s. Check "
                f"DB_HOST/DB_USER/DB_PASSWORD in .env and that the VM's "
                f"firewall allows this container to reach MySQL."
            )
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=config.POLL_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass  # retry
    return False


async def wait_for_profile(stop_event: asyncio.Event) -> bool:
    """
    Verify the signed-in Chrome profile exists at startup, right after
    the DB check. Same retry pattern as wait_for_db: ./browser-profile
    is a live bind mount, so if it's missing the operator can copy it
    onto the host and the service picks it up without a restart.

    "Valid" = the directory exists and contains Chrome's "Local State"
    file (or a "Default" subdir) — an empty or wrongly-nested copy
    (e.g. browser-profile/browser-profile/) fails this check, which is
    exactly the mistake we want to surface here instead of as a crash
    mid-join.
    """
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        d = config.BROWSER_PROFILE_DIR
        problem = None
        if not os.path.isdir(d):
            problem = "directory does not exist"
        else:
            try:
                entries = os.listdir(d)
            except PermissionError:
                # Classic VM mistake: profile copied as root, container
                # runs as botuser. isdir() above still works (it stats
                # via the readable parent), only listing fails.
                problem = (
                    f"permission denied — owned by another user? The "
                    f"container runs as uid {os.getuid()}; on the host "
                    f"run: chown -R {os.getuid()} browser-profile/"
                )
            else:
                if "Local State" not in entries and "Default" not in entries:
                    problem = (
                        "directory exists but doesn't look like a Chrome "
                        "profile (no 'Local State' file / 'Default' dir — "
                        "empty or wrongly-nested copy?)"
                    )

        if problem is None:
            logger.info(
                f"✓ Browser profile found — {d} ({len(entries)} entries)"
            )
            return True

        logger.error(
            f"✗ Browser profile MISSING (attempt {attempt}) — {d}: {problem}"
        )
        logger.error(
            f"  Retrying in {config.POLL_INTERVAL_SECONDS}s. Generate it "
            f"with `python -m scripts.setup_profile` on a machine with a "
            f"screen, then copy the browser-profile/ folder into this "
            f"service's directory on the host."
        )
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=config.POLL_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass  # retry
    return False


async def scheduler_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        f"PSike Meet host auto-join starting. Poll every "
        f"{config.POLL_INTERVAL_SECONDS}s. Profile: {config.BROWSER_PROFILE_DIR}"
    )
    while not stop_event.is_set():
        try:
            sessions = find_active_sessions()
            if sessions:
                sid_list = [s["session_id"] for s in sessions]
                logger.info(f"Active sessions: {sid_list}")

            for s in sessions:
                if _occ_key(s) in _inflight:
                    continue
                if is_already_handled(
                    s["session_id"],
                    str(s.get("session_date") or ""),
                    s.get("meeting_url") or "",
                ):
                    continue
                # Detached background task. handle_session manages its
                # own state — we don't need a reference to it here.
                asyncio.create_task(handle_session(s))
        except Exception:
            logger.exception("Scheduler poll failed (will retry next tick)")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=config.POLL_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass  # normal — sleep elapsed without stop signal


async def _main() -> None:
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        logger.info("Shutdown signal received — finishing in-flight tasks…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler in asyncio.
            # Ctrl-C still raises KeyboardInterrupt which works.
            pass

    # Prove the two startup dependencies first — clear ✓/✗ lines in
    # the logs — so a wrong host/password/firewall (DB) or a missing
    # Chrome profile copy (joins) is obvious on the VM instead of the
    # bot silently polling or crashing mid-join.
    if await wait_for_db(stop_event) and await wait_for_profile(stop_event):
        await scheduler_loop(stop_event)

    # Give in-flight joins a chance to leave cleanly. They have their
    # own timeouts inside; this is just a polite drain window.
    if _inflight:
        logger.info(
            f"Waiting up to 30s for {len(_inflight)} in-flight join(s) "
            f"to clean up…"
        )
        for _ in range(30):
            if not _inflight:
                break
            await asyncio.sleep(1)

    logger.info("Goodbye.")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
