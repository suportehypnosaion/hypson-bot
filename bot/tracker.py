"""
tracker.py — tiny local SQLite store for the service's own state.

Why local SQLite (not a column on PSike's sessions table):
  - Keeps the service decoupled from PSike — no schema changes needed
  - Can be wiped/redeployed without touching production DB
  - "Did we join session X?" is internal state; no one else on the
    PSike side needs to read it

The store records, for each session_id we've touched:
  status:    'joining' | 'joined' | 'left' | 'failed'
  joined_at: ISO timestamp
  left_at:   ISO timestamp (if applicable)
  attempts:  how many join attempts have been made
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from . import config


@contextmanager
def _conn():
    """Context manager that opens a SQLite connection and ensures the
    schema exists. SQLite handles concurrent reads/writes from one
    process fine; we open/close per-call for simplicity."""
    c = sqlite3.connect(config.TRACKER_DB_PATH, isolation_level=None)
    c.execute("""
        CREATE TABLE IF NOT EXISTS handled_sessions (
            session_id   INTEGER PRIMARY KEY,
            status       TEXT NOT NULL,
            joined_at    TEXT NOT NULL,
            left_at      TEXT,
            attempts     INTEGER NOT NULL DEFAULT 1,
            session_date TEXT
        )
    """)
    # Migrate pre-existing DBs that lack newer columns.
    cols = [r[1] for r in c.execute("PRAGMA table_info(handled_sessions)")]
    if "attempts" not in cols:
        c.execute(
            "ALTER TABLE handled_sessions "
            "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1"
        )
    if "session_date" not in cols:
        c.execute(
            "ALTER TABLE handled_sessions ADD COLUMN session_date TEXT"
        )
    if "meeting_url" not in cols:
        c.execute(
            "ALTER TABLE handled_sessions ADD COLUMN meeting_url TEXT"
        )
    try:
        yield c
    finally:
        c.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_already_handled(
    session_id: int, session_date: str = "", meeting_url: str = ""
) -> bool:
    """True if we've already started joining (or finished) this
    occurrence of the session.

    An occurrence is identified by session_date AND meeting_url (str of the
    DB values). If EITHER differs from what we stored, the session was
    RESCHEDULED (new start) or its meeting link CHANGED after we attended it
    — that's a brand-new occurrence and we must join again, so the bot also
    joins the changed meeting. (Stored NULL = row written by an older version;
    treated as matching to preserve its handled state.)

    'failed' sessions are retried (next poll cycle) until they've
    burned MAX_JOIN_ATTEMPTS attempts — Meet's UI is flaky enough
    that a single failed join is usually transient. After the cap,
    the session is permanently skipped; delete the row from
    bot-state.sqlite to force another try.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT status, attempts, session_date, meeting_url "
            "FROM handled_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return False
        status, attempts, stored_date, stored_url = row
        if stored_date is not None and session_date and stored_date != session_date:
            return False  # rescheduled — new occurrence, join again
        if stored_url is not None and meeting_url and stored_url != meeting_url:
            return False  # meeting link changed — new occurrence, join again
        if status == "failed" and attempts < config.MAX_JOIN_ATTEMPTS:
            return False  # eligible for retry
        return True


def mark_joining(
    session_id: int, session_date: str = "", meeting_url: str = ""
) -> None:
    """Insert (first attempt), bump the attempt counter (retry), or
    reset the counter (session was rescheduled to a new date OR its
    meeting link changed — either makes it a fresh occurrence)."""
    with _conn() as c:
        c.execute(
            "INSERT INTO handled_sessions "
            "(session_id, status, joined_at, attempts, session_date, meeting_url) "
            "VALUES (?, 'joining', ?, 1, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  status='joining', joined_at=excluded.joined_at, "
            "  attempts=CASE WHEN IFNULL(session_date,'') != IFNULL(excluded.session_date,'') "
            "                  OR IFNULL(meeting_url,'') != IFNULL(excluded.meeting_url,'') "
            "           THEN 1 ELSE attempts+1 END, "
            "  session_date=excluded.session_date, "
            "  meeting_url=excluded.meeting_url",
            (session_id, _now(), session_date, meeting_url),
        )


def mark_joined(session_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE handled_sessions SET status='joined' WHERE session_id=?",
            (session_id,),
        )


def mark_left(session_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE handled_sessions SET status='left', left_at=? "
            "WHERE session_id=?",
            (_now(), session_id),
        )


def mark_failed(session_id: int) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE handled_sessions SET status='failed', left_at=? "
            "WHERE session_id=?",
            (_now(), session_id),
        )


def get_status(session_id: int) -> Optional[str]:
    """Returns 'joining'|'joined'|'left'|'failed' or None if untouched."""
    with _conn() as c:
        row = c.execute(
            "SELECT status FROM handled_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return row[0] if row else None
