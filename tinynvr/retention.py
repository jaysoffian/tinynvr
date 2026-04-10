"""Retention management for recorded segments."""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_old_segments(storage_path: str, retention_days: int) -> int:
    """Delete .mkv and .idx files older than retention_days.

    Segments are named ``YYYY-MM-DD_HH-MM-SS.mkv`` and daily indexes are
    named ``YYYY-MM-DD.idx``.  Both are keyed by UTC date.  Returns the
    number of .mkv files deleted.
    """
    root = Path(storage_path)
    if not root.exists():
        return 0

    cutoff = (datetime.now(tz=UTC) - timedelta(days=retention_days)).date()
    deleted = 0

    for camera_dir in root.iterdir():
        if not camera_dir.is_dir():
            continue
        for entry in camera_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix == ".mkv":
                try:
                    file_date = date.fromisoformat(entry.stem[:10])
                except ValueError:
                    continue
                if file_date < cutoff:
                    entry.unlink()
                    deleted += 1
                    logger.debug("Deleted old segment: %s", entry)
            elif entry.suffix == ".idx":
                try:
                    file_date = date.fromisoformat(entry.stem)
                except ValueError:
                    continue
                if file_date < cutoff:
                    entry.unlink()
                    logger.debug("Deleted old index: %s", entry)

    if deleted > 0:
        logger.info("Retention cleanup: deleted %d segment(s)", deleted)
    return deleted


async def retention_loop(storage_path: str, retention_days: int) -> None:
    """Run cleanup every hour."""
    while True:
        try:
            cleanup_old_segments(storage_path, retention_days)
        except Exception:
            logger.exception("Error during retention cleanup")
        await asyncio.sleep(3600)
