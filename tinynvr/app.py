"""FastAPI application for the TinyNVR web interface."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from tinynvr.config import (
    Config,
    config_to_dict,
    load_config,
    save_config,
)
from tinynvr.recorder import RecordingManager
from tinynvr.retention import retention_loop

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown of recording and retention."""
    config = load_config()
    manager = RecordingManager(config)
    retention_task = asyncio.create_task(
        retention_loop(config.storage.path, config.storage.retention_days),
    )

    app.state.config = config
    app.state.manager = manager

    await manager.start_all()
    logger.info("TinyNVR started with %d camera(s)", len(config.cameras))

    yield

    await manager.stop_all()
    retention_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await retention_task
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


@app.get("/api/config")
async def get_config(request: Request) -> dict:
    """Return current config as JSON."""
    return config_to_dict(request.app.state.config)


# ---------------------------------------------------------------------------
# Camera API
# ---------------------------------------------------------------------------


@app.get("/api/recordings/range")
async def recordings_range(request: Request) -> dict:
    """Return the earliest recording date across all cameras."""
    config: Config = request.app.state.config
    storage = Path(config.storage.path)
    if not storage.is_dir():
        return {"earliest": None}

    earliest: str | None = None
    for camera_dir in storage.iterdir():
        if not camera_dir.is_dir():
            continue
        for segment in camera_dir.iterdir():
            if segment.suffix != ".mkv" or not segment.is_file():
                continue
            date_part = segment.stem[:10]
            if earliest is None or date_part < earliest:
                earliest = date_part
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
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format, expected YYYY-MM-DD",
        ) from None

    camera_dir = Path(config.storage.path) / name
    if not camera_dir.is_dir():
        return []

    prefix = date_str + "_"
    segments = []
    for segment_path in sorted(camera_dir.iterdir()):
        if not segment_path.name.startswith(prefix):
            continue
        if segment_path.suffix != ".mkv" or not segment_path.is_file():
            continue
        # Parse start_time from filename: 2026-04-08_02-52-34.mp4
        try:
            start_time = (
                datetime.strptime(segment_path.stem, "%Y-%m-%d_%H-%M-%S")
                .replace(tzinfo=UTC)
                .isoformat()
            )
        except ValueError:
            start_time = None

        segments.append(
            {
                "filename": segment_path.name,
                "start_time": start_time,
                "size_bytes": segment_path.stat().st_size,
            }
        )

    return segments


async def _wait_for_disconnect(request: Request) -> None:
    """Resolve when the client closes the connection."""
    while not await request.is_disconnected():
        await asyncio.sleep(0.5)


@app.get("/api/segments/{camera_name}/{filename}")
async def serve_segment(
    camera_name: str,
    filename: str,
    request: Request,
    download: bool = False,
) -> Response:
    """Serve a segment by remuxing MKV to fMP4 for browser playback."""
    config: Config = request.app.state.config
    storage = Path(config.storage.path).resolve()
    file_path = (storage / camera_name / filename).resolve()

    if not file_path.is_relative_to(storage) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Segment not found")

    # Remux MKV → fMP4: video copy, audio transcode to AAC (for pcm_mulaw etc.)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(file_path),
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

    # Race ffmpeg against client disconnect so we kill the process
    # if the browser moves to a different segment during scrubbing
    comm_task = asyncio.create_task(proc.communicate())
    disc_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {comm_task, disc_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        disc_task.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not comm_task.done():
            comm_task.cancel()

    if comm_task not in done:
        return Response(status_code=499)

    stdout, stderr = comm_task.result()

    if proc.returncode != 0:
        error = stderr.decode(errors="replace").strip()
        logger.warning("Remux failed for %s: %s", file_path, error)
        raise HTTPException(status_code=500, detail="Failed to remux segment")

    mp4_name = filename.replace(".mkv", ".mp4")
    disposition = "attachment" if download else "inline"
    return Response(
        content=stdout,
        media_type="video/mp4",
        headers={"Content-Disposition": f'{disposition}; filename="{mp4_name}"'},
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
    enabled: bool = body.get("enabled", False)

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

static_dir = Path("static").resolve()
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
