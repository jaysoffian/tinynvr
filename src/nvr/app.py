"""FastAPI application for the NVR web interface."""

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
from starlette.responses import FileResponse

from nvr.config import (
    Config,
    config_to_dict,
    load_config,
    save_config,
)
from nvr.recorder import RecordingManager
from nvr.retention import retention_loop

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
    logger.info("NVR started with %d camera(s)", len(config.cameras))

    yield

    await manager.stop_all()
    retention_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await retention_task
    logger.info("NVR shutdown complete")


app = FastAPI(title="NVR", lifespan=lifespan)

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
    """List segments for a camera on a given UTC date (date_str=YYYY-MM-DD).

    Filenames are flat: 2026-04-08_02-52-34.mp4
    """
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
        if segment_path.suffix != ".mp4" or not segment_path.is_file():
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


@app.get("/api/segments/{camera_name}/{filename}")
async def serve_segment(
    camera_name: str,
    filename: str,
    request: Request,
) -> FileResponse:
    """Serve a video segment file with Range header support."""
    config: Config = request.app.state.config
    storage = Path(config.storage.path).resolve()
    file_path = (storage / camera_name / filename).resolve()

    if not file_path.is_relative_to(storage) or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Segment not found")

    return FileResponse(
        path=file_path,
        media_type="video/mp4",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Webhook API (HomeAssistant)
# ---------------------------------------------------------------------------


@app.post("/api/webhook")
async def webhook(request: Request) -> dict:
    """Toggle cameras by name.

    Body: {"cameras": ["name1", ...], "enabled": true|false}
    """
    body = await request.json()
    camera_names: list[str] = body.get("cameras", [])
    enabled: bool = body.get("enabled", False)
    manager: RecordingManager = request.app.state.manager
    config: Config = request.app.state.config

    affected = []
    for name in camera_names:
        if name not in manager.recorders:
            continue
        affected.append(name)
        config.cameras[name].enabled = enabled
        if enabled:
            await manager.recorders[name].start()
        else:
            await manager.recorders[name].stop()

    if affected:
        save_config(config)

    action = "enabled" if enabled else "disabled"
    logger.info("Webhook: %s cameras: %s", action, affected)

    return {"status": "ok", "action": action, "cameras": affected}


# ---------------------------------------------------------------------------
# Static files (must be last so API routes take priority)
# ---------------------------------------------------------------------------

static_dir = Path("static").resolve()
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
