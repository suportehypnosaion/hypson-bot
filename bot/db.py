"""
db.py — read-only access to PSike's sessions table.

The service does NOT write to PSike's DB. It only reads to figure out
which meetings are happening now (or about to). All service-specific
state (which meetings we've already joined) lives in the local
SQLite tracker, not in PSike.
"""

from typing import List, Dict, Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text

from . import config

# Quote the password — passwords with special chars (@, /, :) break
# URL parsing otherwise. quote_plus is safe even for "normal" passwords.
_pw = quote_plus(config.DB_PASSWORD)
_DATABASE_URL = (
    f"mysql+pymysql://{config.DB_USER}:{_pw}"
    f"@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}"
)

# pool_pre_ping reconnects automatically if the DB connection got
# dropped (idle timeout, restart, etc.) — important for a long-
# running process like this one.
#
# DB_TIME_ZONE: sessions are stored as naive wall-clock time in the
# scheduler's zone, and our query compares them against NOW(). If the
# MySQL server itself runs in a different zone (cloud VMs are usually
# UTC), NOW() ticks in the wrong wall-clock and nothing ever matches.
# Setting time_zone per-connection makes NOW() return the right zone
# without touching the server's global config.
# connect_timeout: without it, a firewall that silently DROPs packets
# (common between Docker and a LAN MySQL host) makes the very first
# query hang forever — the service looks "stuck" with no error at all.
# Better to fail fast with a clear exception we can log.
_connect_args = {"connect_timeout": 10}
if config.DB_TIME_ZONE:
    _connect_args["init_command"] = f"SET time_zone = '{config.DB_TIME_ZONE}'"

_engine = create_engine(
    _DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args=_connect_args,
)


def check_connection() -> str:
    """
    Startup probe: open a connection and run a trivial query.

    Returns the MySQL server's NOW() as a string — logged at startup
    so timezone mismatches (the #1 cause of "bot never joins") are
    visible at a glance. Raises on any connection failure; the caller
    logs the error so a wrong host/password/firewall is diagnosable
    from the container logs instead of hanging silently.
    """
    with _engine.connect() as conn:
        row = conn.execute(text("SELECT NOW()")).fetchone()
        return str(row[0])


def find_active_sessions() -> List[Dict]:
    """
    Return PSike sessions the host account should currently be inside.

    A session qualifies if ALL of these are true:
      - session_type = 'video_call'  (in-person/phone don't need a host)
      - meeting_url IS NOT NULL      (no Meet → nothing to join)
      - status IN ('scheduled', 'in_progress')
      - NOW() is between [session_date − JOIN_EARLY] and
        [session_date + duration + LEAVE_GRACE]

    Each returned dict has:
      session_id, session_date, duration_minutes, meeting_url, status

    Note on time: PSike's frontend sends session_date as the
    therapist's naive local wall-clock time ("2026-06-04T12:05:00",
    no timezone), and the backend stores it as-is. The MySQL server's
    NOW() runs in that same wall-clock domain (time_zone = SYSTEM),
    so all time comparisons happen INSIDE SQL against NOW(). Never
    compare against Python's utcnow() here — on an IST host that
    made every session look 5.5 h in the future and the service never
    joined anything.

    Note on enum case: SQLAlchemy's SqlEnum stores the enum NAMES
    ('SCHEDULED', 'VIDEO_CALL'). MySQL's default ci collation makes
    lowercase literals match anyway, but we use UPPER() so this never
    silently breaks on a case-sensitive collation.
    """
    sql = text("""
        SELECT
            session_id,
            session_date,
            duration_minutes,
            meeting_url,
            status
        FROM sessions
        WHERE UPPER(session_type) = 'VIDEO_CALL'
          AND meeting_url IS NOT NULL
          AND meeting_url != ''
          AND UPPER(status) IN ('SCHEDULED', 'IN_PROGRESS')
          AND session_date <= DATE_ADD(NOW(), INTERVAL :join_early SECOND)
          AND DATE_ADD(
                session_date,
                INTERVAL (COALESCE(duration_minutes, 60) * 60 + :leave_grace) SECOND
              ) >= NOW()
        ORDER BY session_date
    """)

    with _engine.connect() as conn:
        result = conn.execute(sql, {
            "join_early":  config.JOIN_EARLY_SECONDS,
            "leave_grace": config.LEAVE_GRACE_SECONDS,
        })
        return [dict(row._mapping) for row in result]


def get_session_state(session_id: int) -> Optional[Dict]:
    """
    Current status + meeting_url for one session, with NO time-window filter.

    A bot already inside a meeting polls this to notice that the session it
    joined was cancelled / no-showed / completed (status no longer
    scheduled/in_progress) or moved to a different meeting_url. Returns
    {status, meeting_url} or None if the row is gone.
    """
    sql = text("SELECT status, meeting_url FROM sessions WHERE session_id = :sid")
    with _engine.connect() as conn:
        row = conn.execute(sql, {"sid": session_id}).fetchone()
        return dict(row._mapping) if row else None
