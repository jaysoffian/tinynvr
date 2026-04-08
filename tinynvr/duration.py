"""Sidecar .nfo files for segment metadata caching.

Each .mkv segment gets a .nfo JSON sidecar containing at minimum its
duration.  Written by the recorder when a segment completes, and
backfilled by the server on first access for any missing ones.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_DURATION = 3600.0  # 1 hour sanity cap


def nfo_path(mkv: Path) -> Path:
    """Return the .nfo sidecar path for an MKV file."""
    return mkv.with_suffix(".nfo")


def read_nfo(mkv: Path) -> dict[str, Any] | None:
    """Read sidecar metadata, or None if missing/invalid."""
    p = nfo_path(mkv)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError, OSError:
        return None


def read_duration(mkv: Path) -> float | None:
    """Read cached duration from sidecar, or None if missing."""
    nfo = read_nfo(mkv)
    if nfo is None:
        return None
    dur = nfo.get("duration_sec")
    if dur is None:
        return None
    return min(float(dur), _MAX_DURATION)


async def probe_and_write(mkv: Path) -> float | None:
    """Probe duration with ffprobe and write .nfo sidecar. Returns seconds."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(mkv),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return None
        info = json.loads(stdout)
        raw = info.get("format", {}).get("duration")
        if raw is None:
            return None
        dur = min(float(raw), _MAX_DURATION)
        nfo = {"duration_sec": round(dur, 3)}
        nfo_path(mkv).write_text(json.dumps(nfo) + "\n")
        return dur
    except TimeoutError, json.JSONDecodeError, ValueError, OSError:
        return None


async def ensure_durations(paths: list[Path]) -> dict[str, float]:
    """Return {filename: duration_sec} for all paths, probing any missing.

    Reads from .nfo sidecars where available, probes with ffprobe otherwise.
    Probes run concurrently with a concurrency limit.
    """
    results: dict[str, float] = {}
    to_probe: list[Path] = []

    for p in paths:
        dur = read_duration(p)
        if dur is not None:
            results[p.name] = dur
        else:
            to_probe.append(p)

    if not to_probe:
        return results

    sem = asyncio.Semaphore(8)

    async def _probe(p: Path) -> None:
        async with sem:
            dur = await probe_and_write(p)
            if dur is not None:
                results[p.name] = dur

    await asyncio.gather(*(_probe(p) for p in to_probe))
    return results
