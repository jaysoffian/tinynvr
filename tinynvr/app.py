"""FastAPI application for the TinyNVR web interface."""

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, PlainTextResponse, StreamingResponse

from tinynvr import db
from tinynvr.config import (
    Config,
    config_to_dict,
    load_config,
    save_config,
)
from tinynvr.recorder import RecordingManager
from tinynvr.retention import retention_loop
from tinynvr.sprite import sprite_path_for


def _configure_logging() -> None:
    """Emit DEBUG+ for our own loggers; leave third-party loggers alone."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    app_logger = logging.getLogger("tinynvr")
    app_logger.setLevel(logging.DEBUG)
    app_logger.addHandler(handler)
    app_logger.propagate = False


_configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown of recording and retention."""
    config = load_config()
    db.init_db(config.storage.path)
    manager = RecordingManager(config)
    retention_task = asyncio.create_task(
        retention_loop(config.storage.path, config.storage.retention_days),
    )

    app.state.config = config
    app.state.manager = manager
    app.state.config_lock = asyncio.Lock()

    await manager.start_all()
    logger.info("TinyNVR started with %d camera(s)", len(config.cameras))

    yield

    await manager.stop_all()
    retention_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await retention_task
    db.close_db()
    logger.info("TinyNVR shutdown complete")


app = FastAPI(title="TinyNVR", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------


_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
try:
    _VERSION = _VERSION_FILE.read_text().strip() or "dev"
except OSError:
    _VERSION = "dev"


@app.get("/api/config")
async def get_config(request: Request) -> dict:
    """Return current config as JSON."""
    return config_to_dict(request.app.state.config)


@app.get("/api/version")
async def get_version() -> dict:
    """Return the short git commit baked into the image at build time."""
    return {"commit": _VERSION}


# ---------------------------------------------------------------------------
# Debug log (opt-in diagnostic — frontend posts JSON event batches here when
# loaded with ?debug=1, server appends them as JSON lines to a file that can
# then be pulled back via GET). Remove this section once the gapless
# playback investigation is done.
# ---------------------------------------------------------------------------

_DEBUG_LOG_PATH = Path("/tmp/tinynvr-debug.log")


@app.post("/api/debug/log")
async def debug_log_write(request: Request) -> dict:
    """Append a batch of client-side debug events to the debug log file."""
    body = await request.json()
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="'events' must be a list")
    try:
        with _DEBUG_LOG_PATH.open("a") as f:
            for event in events:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.warning("debug log write failed: %s", exc)
        raise HTTPException(status_code=500, detail="debug log unavailable") from None
    return {"ok": True, "count": len(events)}


@app.get("/api/debug/log")
async def debug_log_read() -> PlainTextResponse:
    """Return the full debug log as plain text, or an empty body if absent."""
    try:
        content = _DEBUG_LOG_PATH.read_text()
    except OSError:
        content = ""
    return PlainTextResponse(content)


@app.delete("/api/debug/log")
async def debug_log_clear() -> dict:
    """Truncate the debug log."""
    try:
        _DEBUG_LOG_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("debug log clear failed: %s", exc)
        raise HTTPException(status_code=500, detail="debug log unavailable") from None
    return {"ok": True}


# ---------------------------------------------------------------------------
# Camera API
# ---------------------------------------------------------------------------


@app.get("/api/recordings/range")
async def recordings_range(request: Request) -> dict:
    """Return the earliest indexed recording date across all cameras.

    The date is a UTC ``YYYY-MM-DD`` string, or ``None`` if nothing is indexed.
    """
    _ = request  # kept for API symmetry
    start_utc = db.earliest_start_utc()
    if start_utc is None:
        return {"earliest": None}
    earliest = datetime.fromtimestamp(start_utc, tz=UTC).date().isoformat()
    return {"earliest": earliest}


@app.get("/api/cameras")
async def list_cameras(request: Request) -> list[dict]:
    """List cameras with status."""
    manager: RecordingManager = request.app.state.manager
    return list(manager.get_status().values())


@app.post("/api/cameras/{name}/enable")
async def enable_camera(name: str, request: Request) -> dict:
    """Enable a camera and start recording."""
    manager: RecordingManager = request.app.state.manager
    async with request.app.state.config_lock:
        try:
            await manager.enable_camera(name)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"Camera '{name}' not found",
            ) from None
    return manager.recorders[name].get_status()


@app.post("/api/cameras/{name}/disable")
async def disable_camera(name: str, request: Request) -> dict:
    """Disable a camera and stop recording."""
    manager: RecordingManager = request.app.state.manager
    async with request.app.state.config_lock:
        try:
            await manager.disable_camera(name)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail=f"Camera '{name}' not found",
            ) from None
    return manager.recorders[name].get_status()


# ---------------------------------------------------------------------------
# Segment API
# ---------------------------------------------------------------------------


@app.get("/api/cameras/{name}/segments")
async def list_segments(name: str, date_str: str, request: Request) -> list[dict]:
    """List segments for a camera on a given UTC date (date_str=YYYY-MM-DD)."""
    config: Config = request.app.state.config
    if name not in config.cameras:
        raise HTTPException(status_code=404, detail=f"Camera '{name}' not found")

    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format, expected YYYY-MM-DD",
        ) from None

    day_start = int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp())
    day_end = day_start + 86400

    rows = db.list_segments_for_day(name, day_start, day_end)

    segments = []
    for start_utc, duration_ms, size_bytes in rows:
        ts = datetime.fromtimestamp(start_utc, tz=UTC)
        segments.append(
            {
                "filename": ts.strftime("%Y-%m-%d_%H-%M-%S.mp4"),
                "start_time": ts.isoformat(),
                "size_bytes": size_bytes,
                "duration_sec": duration_ms / 1000.0,
            }
        )

    return segments


def _segment_disk_path(storage: Path, camera_name: str, start_utc: int) -> Path:
    ts = datetime.fromtimestamp(start_utc, tz=UTC)
    return (
        storage
        / ts.strftime("%Y-%m-%d")
        / ts.strftime("%H")
        / camera_name
        / ts.strftime("%M-%S.mp4")
    )


@app.get("/api/segments/{camera_name}/{filename}")
async def serve_segment(
    camera_name: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Serve a segment MP4 or its sibling sprite JPEG statically.

    Segments are self-contained MP4 with the moov atom at the start,
    so Starlette's FileResponse handles Range: requests natively and
    Safari can byte-range seek into the middle of a segment. Sprite
    requests share this route because a separately-declared
    ``{filename}.jpg`` path would be shadowed by this ``{filename}``
    catch-all (FastAPI matches in declaration order).
    """
    config: Config = request.app.state.config
    storage = Path(config.storage.path).resolve()

    if filename.endswith(".jpg"):
        stem = filename.removesuffix(".jpg")
        media_type = "image/jpeg"
        is_sprite = True
    elif filename.endswith(".mp4"):
        stem = filename.removesuffix(".mp4")
        media_type = "video/mp4"
        is_sprite = False
    else:
        raise HTTPException(status_code=404, detail="Segment not found")

    try:
        ts = datetime.strptime(stem, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=404, detail="Segment not found") from None

    segment_path = _segment_disk_path(
        storage, camera_name, int(ts.timestamp())
    ).resolve()
    file_path = sprite_path_for(segment_path) if is_sprite else segment_path
    if not file_path.is_relative_to(storage) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Segment not found")

    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ---------------------------------------------------------------------------
# Range download API
# ---------------------------------------------------------------------------


@app.get("/api/cameras/{name}/download")
async def download_range(
    name: str,
    start: str,
    end: str,
    request: Request,
) -> StreamingResponse:
    """Download a time range as a single MP4 by concatenating segments."""
    config: Config = request.app.state.config
    if name not in config.cameras:
        raise HTTPException(status_code=404, detail=f"Camera '{name}' not found")

    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid datetime format, expected ISO 8601",
        ) from None

    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end must be after start")

    storage = Path(config.storage.path).resolve()

    rows = db.list_segments_for_range(
        name,
        int(start_dt.timestamp()),
        int(end_dt.timestamp()),
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No segments overlap the requested range",
        )

    matching: list[tuple[datetime, Path, float]] = []
    for start_utc, duration_ms, _size in rows:
        ts = datetime.fromtimestamp(start_utc, tz=UTC)
        p = _segment_disk_path(storage, name, start_utc)
        matching.append((ts, p, duration_ms / 1000.0))

    # Calculate trim offsets
    first_start = matching[0][0].timestamp()
    ss_offset = max(0.0, start_dt.timestamp() - first_start)

    # Total duration of all concatenated segments
    last_ts, _, last_dur = matching[-1]
    concat_end = last_ts.timestamp() + last_dur
    total_concat = concat_end - first_start
    # Requested duration, clamped to available footage
    requested_dur = end_dt.timestamp() - start_dt.timestamp()
    trim_dur = min(requested_dur, total_concat - ss_offset)

    # Build concat file list
    concat_fd, concat_path_str = tempfile.mkstemp(suffix=".txt", prefix="nvr-concat-")
    concat_file = Path(concat_path_str)
    os.close(concat_fd)

    with concat_file.open("w") as f:
        for _, p, _ in matching:
            f.write(f"file '{p}'\n")

    # Build a descriptive filename
    safe_start = re.sub(r"[^\w]", "-", start)
    safe_end = re.sub(r"[^\w]", "-", end)
    safe_name = re.sub(r"[^\w.\-]", "_", name)
    filename = f"{safe_name}_{safe_start}_to_{safe_end}.mp4"

    async def stream_ffmpeg() -> AsyncIterator[bytes]:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_path_str,
            "-ss",
            str(ss_offset),
            "-t",
            str(trim_dur),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
            await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            concat_file.unlink(missing_ok=True)

    return StreamingResponse(
        stream_ffmpeg(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Webhook API (HomeAssistant)
# ---------------------------------------------------------------------------


@app.post("/api/webhook/{name}")
async def webhook(name: str, request: Request) -> dict:
    """Toggle a camera on or off.

    Body: {"enabled": true|false}
    """
    manager: RecordingManager = request.app.state.manager
    if name not in manager.recorders:
        raise HTTPException(status_code=404, detail=f"Camera '{name}' not found")

    body = await request.json()
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=400,
            detail="Body must contain 'enabled' as a boolean",
        )

    async with request.app.state.config_lock:
        config: Config = request.app.state.config
        config.cameras[name].enabled = enabled
        if enabled:
            await manager.recorders[name].start()
        else:
            await manager.recorders[name].stop()
        save_config(config)

    action = "enabled" if enabled else "disabled"
    logger.info("Webhook: %s camera %s", action, name)

    return {"status": "ok", "action": action, "camera": name}


# ---------------------------------------------------------------------------
# Static files (must be last so API routes take priority)
# ---------------------------------------------------------------------------

static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
