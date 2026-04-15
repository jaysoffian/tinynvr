"""SQLite-backed segment index.

One DB file at ``{storage}/tinynvr.db`` in WAL mode. The recorder is the sole
writer; FastAPI handlers and the retention loop are readers. Rows are inserted
after a segment is probed successfully. Unplayable segments are deleted from
disk and never get a row.
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

db_conn: sqlite3.Connection | None = None


def init_db(storage_path: str) -> None:
    """Open the connection, apply PRAGMAs, create schema if missing."""
    global db_conn  # noqa: PLW0603
    if db_conn is not None:
        return
    root = Path(storage_path)
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "tinynvr.db"
    db_conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,
        check_same_thread=False,
    )
    db_conn.execute("PRAGMA journal_mode=WAL")
    db_conn.execute("PRAGMA synchronous=NORMAL")
    db_conn.executescript("""
        CREATE TABLE IF NOT EXISTS segments (
          camera      TEXT    NOT NULL,
          start_utc   INTEGER NOT NULL,
          duration_ms INTEGER NOT NULL,
          size_bytes  INTEGER NOT NULL,
          PRIMARY KEY (camera, start_utc)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_segments_start ON segments(start_utc);
    """)
    logger.info("Opened segment index at %s", db_path)


def close_db() -> None:
    global db_conn  # noqa: PLW0603
    if db_conn is not None:
        db_conn.close()
        db_conn = None


def get_conn() -> sqlite3.Connection:
    if db_conn is None:
        msg = "db.init_db() has not been called"
        raise RuntimeError(msg)
    return db_conn


def insert_segment(
    camera: str,
    start_utc: int,
    duration_ms: int,
    size_bytes: int,
) -> None:
    """Insert or replace a segment row. Replace makes recovery idempotent."""
    get_conn().execute(
        "INSERT OR REPLACE INTO segments (camera, start_utc, duration_ms, size_bytes)"
        " VALUES (?, ?, ?, ?)",
        (camera, start_utc, duration_ms, size_bytes),
    )


def delete_segments_before(cutoff_utc: int) -> int:
    """Delete all rows with ``start_utc < cutoff_utc``. Returns rowcount."""
    cur = get_conn().execute(
        "DELETE FROM segments WHERE start_utc < ?",
        (cutoff_utc,),
    )
    return cur.rowcount


def earliest_start_utc() -> int | None:
    row = get_conn().execute("SELECT MIN(start_utc) FROM segments").fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def list_segments_for_day(
    camera: str,
    day_start_utc: int,
    day_end_utc: int,
) -> list[tuple[int, int, int]]:
    """Return ``(start_utc, duration_ms, size_bytes)`` rows for one UTC day."""
    cur = get_conn().execute(
        "SELECT start_utc, duration_ms, size_bytes FROM segments"
        " WHERE camera = ? AND start_utc >= ? AND start_utc < ?"
        " ORDER BY start_utc",
        (camera, day_start_utc, day_end_utc),
    )
    return list(cur.fetchall())


def list_segments_for_range(
    camera: str,
    start_utc: int,
    end_utc: int,
) -> list[tuple[int, int, int]]:
    """Return rows whose span overlaps ``[start_utc, end_utc)``."""
    cur = get_conn().execute(
        "SELECT start_utc, duration_ms, size_bytes FROM segments"
        " WHERE camera = ?"
        "   AND start_utc < ?"
        "   AND (start_utc * 1000 + duration_ms) > (? * 1000)"
        " ORDER BY start_utc",
        (camera, end_utc, start_utc),
    )
    return list(cur.fetchall())


def known_start_utcs(
    camera: str,
    day_start_utc: int,
    day_end_utc: int,
) -> set[int]:
    cur = get_conn().execute(
        "SELECT start_utc FROM segments"
        " WHERE camera = ? AND start_utc >= ? AND start_utc < ?",
        (camera, day_start_utc, day_end_utc),
    )
    return {int(row[0]) for row in cur.fetchall()}
