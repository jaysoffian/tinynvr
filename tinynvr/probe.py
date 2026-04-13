"""ffprobe wrapper used to measure segment durations."""

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_DURATION = 3600.0  # 1 hour sanity cap


async def probe_duration(segment: Path) -> float | None:
    """Probe a single file with ffprobe. Returns ``None`` on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            str(segment),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return None
        info = json.loads(stdout)
        raw = info.get("format", {}).get("duration")
        if raw is None:
            return None
        dur = min(float(raw), _MAX_DURATION)
        logger.debug("ffprobe %s: %.3fs", segment, dur)
    except (ValueError, OSError) as exc:
        logger.warning("ffprobe errored for %s: %s", segment, exc)
        return None
    else:
        return dur


def unlink_unplayable(segment: Path) -> None:
    """Remove a segment that ffprobe could not read."""
    try:
        segment.unlink()
    except OSError as exc:
        logger.warning("Failed to delete unplayable %s: %s", segment, exc)
        return
    logger.info("Deleted unplayable segment %s", segment)
