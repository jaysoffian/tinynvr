"""FastAPI application for the TinyNVR web interface."""

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response, StreamingResponse

from tinynvr.config import (
    Config,
    config_to_dict,
    load_config,
    save_config,
)
from tinynvr.duration import read_index, read_indexes
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
    app.state.config_lock = asyncio.Lock()

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
    """Return the earliest indexed recording date across all cameras."""
    config: Config = request.app.state.config
    storage = Path(config.storage.path)
    if not storage.is_dir():
        return {"earliest": None}

    earliest: str | None = None
    for camera_dir in storage.iterdir():
        if not camera_dir.is_dir():
            continue
        for idx_file in camera_dir.glob("*.idx"):
            date_str = idx_file.stem
            if earliest is None or date_str < earliest:
                earliest = date_str
    if earliest is None:
        return {"earliest": None}
    return {"earliest": f"{earliest}T00:00:00+00:00"}


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
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid date format, expected YYYY-MM-DD",
        ) from None

    # The recorder owns the daily .idx file and is the sole writer.
    # Until the recorder has indexed a day, we return nothing for it.
    camera_dir = Path(config.storage.path) / name
    durations = read_index(camera_dir, date_str)

    segments = []
    for filename, duration_sec in sorted(durations.items()):
        try:
            start_time = (
                datetime.strptime(Path(filename).stem, "%Y-%m-%d_%H-%M-%S")
                .replace(tzinfo=UTC)
                .isoformat()
            )
        except ValueError:
            continue
        try:
            size_bytes = (camera_dir / filename).stat().st_size
        except OSError:
            continue
        segments.append(
            {
                "filename": filename,
                "start_time": start_time,
                "size_bytes": size_bytes,
                "duration_sec": duration_sec,
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

    # Sanitize filename for Content-Disposition header
    safe_name = re.sub(r"[^\w.\-]", "_", filename.replace(".mkv", ".mp4"))
    disposition = "attachment" if download else "inline"
    return Response(
        content=stdout,
        media_type="video/mp4",
        headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
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
    camera_dir = storage / name

    # Read indexes for the UTC dates spanning the range (plus one day before,
    # to catch a segment that started late on the prior day).
    dates: list[str] = []
    d = (start_dt - timedelta(days=1)).date()
    end_date = end_dt.date()
    while d <= end_date:
        dates.append(d.isoformat())
        d += timedelta(days=1)
    durations = read_indexes(camera_dir, dates)

    if not durations:
        raise HTTPException(status_code=404, detail="No segments found")

    # Find segments overlapping [start_dt, end_dt)
    matching: list[tuple[datetime, Path, float]] = []
    for filename, dur in sorted(durations.items()):
        if dur <= 0:
            continue
        p = camera_dir / filename
        if not p.resolve().is_relative_to(storage):
            continue
        try:
            ts = datetime.strptime(Path(filename).stem, "%Y-%m-%d_%H-%M-%S").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        seg_end = ts.timestamp() + dur
        if seg_end <= start_dt.timestamp():
            continue
        if ts.timestamp() >= end_dt.timestamp():
            continue
        matching.append((ts, p, dur))

    if not matching:
        raise HTTPException(
            status_code=404,
            detail="No segments overlap the requested range",
        )

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
