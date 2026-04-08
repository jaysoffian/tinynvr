"""Retention management for recorded segments."""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_old_segments(storage_path: str, retention_days: int) -> int:
    """Delete .mkv files older than retention_days based on filename timestamp.

    Filenames are like 2026-04-08_02-52-34.mkv (flat, no date subdirectories).
    Returns the number of files deleted.
    """
    root = Path(storage_path)
    if not root.exists():
        return 0

    cutoff = (datetime.now(tz=UTC) - timedelta(days=retention_days)).date()
    deleted = 0

    for camera_dir in root.iterdir():
        if not camera_dir.is_dir():
            continue
        for segment in camera_dir.iterdir():
            if segment.suffix != ".mkv" or not segment.is_file():
                continue
            # Parse date from filename: 2026-04-08_02-52-34.mkv
            try:
                file_date = date.fromisoformat(segment.stem[:10])
            except ValueError:
                continue
            if file_date < cutoff:
                segment.unlink()
                # Also remove .nfo sidecar if present
                nfo = segment.with_suffix(".nfo")
                if nfo.exists():
                    nfo.unlink()
                deleted += 1
                logger.debug("Deleted old segment: %s", segment)

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
