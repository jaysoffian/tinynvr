"""Retention management for recorded segments."""

import asyncio
import contextlib
import logging
import re
import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from tinynvr import db

logger = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def cleanup_old_segments(storage_path: str, retention_days: int) -> int:
    """Delete expired hour dirs and their corresponding DB rows.

    The disk layout is ``{storage}/YYYY-MM-DD/HH/<camera>/MM-SS.mp4``.
    An hour dir is "expired" when its wall-clock end
    (``hour_start + 1h``) is already older than ``now - retention_days``.
    Entire hour dirs are removed with ``rmtree`` — one call per hour per
    camera — then the DB rows for segments before the retention cutoff
    are dropped in a single ``DELETE``.

    The current and next wall-clock hour are never touched, so active
    recorders can't have their working directory yanked out from under
    them.

    Returns the number of DB rows deleted.
    """
    root = Path(storage_path)
    if not root.exists():
        return 0

    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=retention_days)

    deleted_hours = 0
    for date_dir in sorted(root.iterdir()):
        if not date_dir.is_dir() or not DATE_RE.match(date_dir.name):
            continue
        try:
            d = date.fromisoformat(date_dir.name)
        except ValueError:
            continue
        for hour_dir in sorted(date_dir.iterdir()):
            if not hour_dir.is_dir() or not hour_dir.name.isdigit():
                continue
            try:
                hour_ts = datetime.combine(
                    d,
                    time(int(hour_dir.name)),
                    tzinfo=UTC,
                )
            except ValueError:
                continue
            if hour_ts + timedelta(hours=1) <= cutoff:
                try:
                    shutil.rmtree(hour_dir)
                except OSError as exc:
                    logger.warning("Failed to rmtree %s: %s", hour_dir, exc)
                    continue
                deleted_hours += 1
                logger.debug("Removed expired hour dir: %s", hour_dir)
        # Drop the date dir if it's now empty.
        with contextlib.suppress(OSError):
            date_dir.rmdir()

    # Align the DB cutoff to the hour so row deletion stays in lockstep
    # with the rmtree gate above: a row is dropped iff its hour dir has
    # already been removed from disk.
    cutoff_hour = cutoff.replace(minute=0, second=0, microsecond=0)
    cutoff_utc = int(cutoff_hour.timestamp())
    deleted_rows = db.delete_segments_before(cutoff_utc)
    if deleted_hours or deleted_rows:
        logger.info(
            "Retention: removed %d hour dir(s), %d db row(s)",
            deleted_hours,
            deleted_rows,
        )
    return deleted_rows


async def retention_loop(storage_path: str, retention_days: int) -> None:
    """Run cleanup every hour."""
    while True:
        try:
            cleanup_old_segments(storage_path, retention_days)
        except Exception:
            logger.exception("Error during retention cleanup")
        await asyncio.sleep(3600)
