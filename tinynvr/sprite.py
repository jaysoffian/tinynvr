"""Sprite (scrub-preview tile strip) generation for closed segments.

A sprite is a single JPEG containing 6 tiles sampled across a 60s
segment, used for hover-preview thumbnails on the timeline. Sibling
file to the source MP4: ``MM-SS.mp4`` → ``MM-SS.jpg``. Retention's
``rmtree`` of the hour directory cleans these up automatically.
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def sprite_path_for(segment: Path) -> Path:
    return segment.with_suffix(".jpg")


def _build_args(segment: Path, sprite: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        # -skip_frame:v nokey is an INPUT option: only keyframes are
        # decoded into the filter graph. ~99% CPU savings vs. decoding
        # every frame. Must precede -i.
        "-skip_frame:v",
        "nokey",
        "-i",
        str(segment),
        "-vf",
        "fps=1/10,scale=400:-2,tile=6x1",
        "-an",
        "-sn",
        "-qscale:v",
        "2",
        "-frames:v",
        "1",
        "-update",
        "1",
        str(sprite),
    ]


async def generate_sprite(segment: Path) -> Path | None:
    """Run ffmpeg to produce the sprite JPEG for ``segment``.

    Returns the sprite path on success, ``None`` on failure. Failures
    are logged at WARNING and never raised — sprite generation is
    best-effort and must never affect indexing callers.
    """
    sprite = sprite_path_for(segment)
    args = _build_args(segment, sprite)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (TimeoutError, OSError) as exc:
        logger.warning("Sprite ffmpeg failed for %s: %s", segment.name, exc)
        return None
    if proc.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        logger.warning(
            "Sprite ffmpeg exit %d for %s: %s",
            proc.returncode,
            segment.name,
            " | ".join(tail),
        )
        return None
    if not sprite.is_file():
        logger.warning("Sprite ffmpeg succeeded but %s missing", sprite.name)
        return None
    return sprite
