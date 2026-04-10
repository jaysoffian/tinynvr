"""Per-camera daily index files for segment durations.

Each camera directory contains one ``{YYYY-MM-DD}.idx`` per UTC day — a
simple append-only text file with one entry per line::

    2026-04-10_12-00-00.mkv: 600.0
    2026-04-10_12-10-00.mkv: 0

Failed probes are recorded as ``0`` so corrupt/incomplete segments
aren't re-probed.  When a filename appears more than once (recovery
case), the last entry wins.

The recorder runs :func:`validate_indexes` once at startup to catch
segments left behind by a prior run, then relies on an inotify
``IN_CLOSE_WRITE`` watch to call :func:`append_duration` each time
ffmpeg closes a completed segment.  The ``.idx`` files are never
re-read during runtime.
"""

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_DURATION = 3600.0  # 1 hour sanity cap


def index_path(camera_dir: Path, date_str: str) -> Path:
    """Return the daily index file path for a given UTC date."""
    return camera_dir / f"{date_str}.idx"


def _date_from_mkv(mkv: Path) -> str | None:
    """Extract YYYY-MM-DD from a segment filename."""
    stem = mkv.stem  # expects "YYYY-MM-DD_HH-MM-SS"
    if len(stem) < 10 or stem[4] != "-" or stem[7] != "-":
        return None
    return stem[:10]


def _parse_line(line: str) -> tuple[str, float]:
    """Parse one ``FILENAME: duration`` line."""
    name, _, val = line.partition(":")
    return name.strip(), float(val)


def read_index(camera_dir: Path, date_str: str) -> dict[str, float]:
    """Return ``{filename: duration_sec}`` from a daily index, or ``{}``.

    Last entry wins on duplicate filenames.
    """
    p = index_path(camera_dir, date_str)
    try:
        text = p.read_text()
    except OSError:
        return {}
    # Last entry wins on duplicate filenames
    return dict(_parse_line(line) for line in text.splitlines())


def read_indexes(camera_dir: Path, date_strs: list[str]) -> dict[str, float]:
    """Read multiple daily indexes and merge into one dict."""
    merged: dict[str, float] = {}
    for date_str in date_strs:
        merged.update(read_index(camera_dir, date_str))
    return merged


async def _probe_duration(mkv: Path) -> float:
    """Probe a single file with ffprobe. Returns 0.0 on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            str(mkv),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip().replace("\n", " | ")
            size = mkv.stat().st_size if mkv.exists() else -1
            logger.warning(
                "ffprobe failed for %s (rc=%d, size=%d): %s",
                mkv,
                proc.returncode,
                size,
                err or "<no stderr>",
            )
            return 0.0
        info = json.loads(stdout)
        raw = info.get("format", {}).get("duration")
        if raw is None:
            logger.warning("ffprobe for %s returned no duration", mkv)
            return 0.0
        dur = min(float(raw), _MAX_DURATION)
        logger.debug("ffprobe %s: %.3fs", mkv, dur)
        return dur
    except (ValueError, OSError) as exc:
        logger.warning("ffprobe errored for %s: %s", mkv, exc)
        return 0.0


def _append_entry(camera_dir: Path, filename: str, duration: float) -> None:
    """Append a single ``FILENAME: duration`` line to the matching daily index."""
    date_str = _date_from_mkv(Path(filename))
    if date_str is None:
        return
    with index_path(camera_dir, date_str).open("a") as f:
        f.write(f"{filename}: {round(duration, 3)}\n")


async def append_duration(camera_dir: Path, mkv: Path) -> float | None:
    """Probe one completed segment and append its duration to the daily index.

    Returns the probed duration in seconds, or ``None`` if the filename
    isn't a valid segment name.
    """
    if _date_from_mkv(mkv) is None:
        return None
    dur = await _probe_duration(mkv)
    _append_entry(camera_dir, mkv.name, dur)
    return dur


async def validate_indexes(camera_dir: Path) -> None:
    """Probe any segments missing from the daily index and append them.

    One-shot sweep intended to run at recorder start, before ffmpeg is
    launched, to catch segments left behind by a prior run (e.g. after
    a crash where the inotify event was lost).
    """
    if not camera_dir.is_dir():
        return

    by_date: dict[str, list[Path]] = {}
    for mkv in camera_dir.glob("*.mkv"):
        if not mkv.is_file():
            continue
        date_str = _date_from_mkv(mkv)
        if date_str is None:
            continue
        by_date.setdefault(date_str, []).append(mkv)

    if not by_date:
        return

    sem = asyncio.Semaphore(8)

    async def _probe(p: Path) -> tuple[str, float]:
        async with sem:
            return p.name, await _probe_duration(p)

    total = 0
    for date_str, mkvs in sorted(by_date.items()):
        known = read_index(camera_dir, date_str)
        to_probe = [m for m in mkvs if m.name not in known]
        if not to_probe:
            continue
        logger.info(
            "Indexing %d segment(s) for %s on %s",
            len(to_probe),
            camera_dir.name,
            date_str,
        )
        results = await asyncio.gather(*(_probe(p) for p in to_probe))
        with index_path(camera_dir, date_str).open("a") as f:
            for name, dur in results:
                f.write(f"{name}: {round(dur, 3)}\n")
                f.flush()
        total += len(results)

    if total:
        logger.info("Indexed %d backlog segment(s) for %s", total, camera_dir.name)
