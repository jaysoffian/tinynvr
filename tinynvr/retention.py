"""Retention management for recorded segments."""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_old_segments(storage_path: str, retention_days: int) -> int:
    """Delete .mp4 and .idx files older than retention_days.

    Segments are deleted by start-time (rolling window): an ``.mp4``
    whose ``YYYY-MM-DD_HH-MM-SS`` stem resolves to a UTC datetime older
    than ``now - retention_days`` is removed. Daily ``.idx`` files are
    deleted one full UTC day later, so an index outlives the last
    segment it references — stale in-flight entries are harmless
    because list_segments ignores missing files and download_range
    stat-checks before handing paths to ffmpeg.

    Returns the number of .mp4 files deleted.
    """
    root = Path(storage_path)
    if not root.exists():
        return 0

    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=retention_days)
    idx_cutoff = (now - timedelta(days=retention_days + 1)).date()
    deleted = 0

    for camera_dir in root.iterdir():
        if not camera_dir.is_dir():
            continue
        for entry in camera_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix == ".mp4":
                try:
                    ts = datetime.strptime(entry.stem, "%Y-%m-%d_%H-%M-%S").replace(
                        tzinfo=UTC
                    )
                except ValueError:
                    continue
                if ts < cutoff:
                    entry.unlink()
                    deleted += 1
                    logger.debug("Deleted old segment: %s", entry)
            elif entry.suffix == ".idx":
                try:
                    file_date = date.fromisoformat(entry.stem)
                except ValueError:
                    continue
                if file_date < idx_cutoff:
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
