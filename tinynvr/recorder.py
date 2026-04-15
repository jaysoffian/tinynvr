"""Camera recording via ffmpeg subprocesses."""

import asyncio
import contextlib
import logging
import os
import re
import signal
import time
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path

from asyncinotify import Inotify, Mask

from tinynvr import db
from tinynvr.config import CameraConfig, Config, StorageConfig, save_config
from tinynvr.probe import probe_duration, unlink_unplayable
from tinynvr.sprite import generate_sprite

logger = logging.getLogger(__name__)

SEGMENT_SECONDS = 60
SEGMENT_PATH_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"/(?P<hour>\d{2})"
    r"/(?P<camera>[^/]+)"
    r"/(?P<minute>\d{2})-(?P<second>\d{2})\.mp4$"
)


class CameraState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RECORDING = "recording"
    ERROR = "error"


_probe_duration = partial(probe_duration, max_duration=SEGMENT_SECONDS * 1.5)


def _parse_segment_path(
    path: Path,
    storage_root: Path,
) -> tuple[str, int] | None:
    """Parse ``{root}/YYYY-MM-DD/HH/<camera>/MM-SS.mp4`` → ``(camera, start_utc)``."""
    try:
        rel = path.relative_to(storage_root)
    except ValueError:
        return None
    m = SEGMENT_PATH_RE.match(rel.as_posix())
    if m is None:
        return None
    try:
        ts = datetime(
            year=int(m["year"]),
            month=int(m["month"]),
            day=int(m["day"]),
            hour=int(m["hour"]),
            minute=int(m["minute"]),
            second=int(m["second"]),
            tzinfo=UTC,
        )
    except ValueError:
        return None
    return m["camera"], int(ts.timestamp())


class SegmentWatcher:
    """Single application-wide inotify watcher for the storage tree.

    Inotify is per-directory, not recursive, so we walk the existing
    ``{storage}/YYYY-MM-DD/HH/<camera>/`` leaf dirs on start and add
    a ``CLOSE_WRITE`` watch on each. New leaf dirs are handed to
    :meth:`add_watch` by :meth:`CameraRecorder._dir_precreate_loop` as
    they are created. Retention's ``rmtree`` drops watches implicitly
    via ``IN_IGNORED``, which asyncinotify handles cleanly.
    """

    def __init__(
        self,
        storage_root: Path,
        dispatch,
    ) -> None:
        self.storage_root = storage_root
        self._dispatch = dispatch  # async (camera, start_utc, path) -> None
        self._inotify: Inotify | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._inotify is not None:
            return
        self._inotify = Inotify()
        if self.storage_root.is_dir():
            for leaf in self.storage_root.glob("*/*/*"):
                if leaf.is_dir():
                    self._add_watch_raw(leaf)
        self._task = asyncio.create_task(self._loop())

    def add_watch(self, leaf_dir: Path) -> None:
        if self._inotify is None:
            return
        self._add_watch_raw(leaf_dir)

    def _add_watch_raw(self, leaf_dir: Path) -> None:
        assert self._inotify is not None
        try:
            self._inotify.add_watch(leaf_dir, Mask.CLOSE_WRITE)
        except OSError as exc:
            logger.warning("Failed to add inotify watch on %s: %s", leaf_dir, exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._inotify is not None:
            self._inotify.close()
            self._inotify = None

    async def _loop(self) -> None:
        assert self._inotify is not None
        pending: set[asyncio.Task[None]] = set()
        try:
            async for event in self._inotify:
                path = event.path
                if path is None or path.suffix != ".mp4":
                    continue
                parsed = _parse_segment_path(path, self.storage_root)
                if parsed is None:
                    continue
                camera, start_utc = parsed
                task = asyncio.create_task(self._dispatch(camera, start_utc, path))
                pending.add(task)
                task.add_done_callback(pending.discard)
        except asyncio.CancelledError:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        except Exception:
            logger.exception("Segment watcher failed")


class CameraRecorder:
    """Manages a single ffmpeg subprocess for one camera."""

    def __init__(
        self,
        name: str,
        camera: CameraConfig,
        storage: StorageConfig,
        watcher: SegmentWatcher,
    ) -> None:
        self.name = name
        self.camera = camera
        self.storage = storage
        self._watcher = watcher
        self.state = CameraState.STOPPED
        self.last_error: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._precreate_task: asyncio.Task | None = None
        self._sprite_tasks: set[asyncio.Task] = set()
        self._should_run = False
        self._backoff = 1.0

    @property
    def storage_root(self) -> Path:
        return Path(self.storage.path)

    def _hour_dir(self, ts: datetime) -> Path:
        return (
            self.storage_root / ts.strftime("%Y-%m-%d") / ts.strftime("%H") / self.name
        )

    def _ensure_hour_dir(self, ts: datetime) -> Path:
        leaf = self._hour_dir(ts)
        leaf.mkdir(parents=True, exist_ok=True)
        return leaf

    def _build_ffmpeg_args(self) -> list[str]:
        output_pattern = str(
            self.storage_root / "%Y-%m-%d" / "%H" / self.name / "%M-%S.mp4"
        )
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-timeout",
            "30000000",
            "-i",
            self.camera.url,
            "-c",
            "copy",
            "-metadata",
            f"title={self.name}",
            "-f",
            "segment",
            "-reset_timestamps",
            "1",
            "-segment_time",
            str(SEGMENT_SECONDS),
            "-segment_format",
            "mp4",
            "-segment_format_options",
            "movflags=+faststart",
            "-segment_atclocktime",
            "1",
            "-strftime",
            "1",
            output_pattern,
        ]

    async def start(self) -> None:
        """Start recording this camera."""
        if self._should_run:
            return
        self._should_run = True
        self._backoff = 1.0
        # Pre-create the current and next hour dirs, and register
        # watches on them, before ffmpeg can try to open a file.
        now = datetime.now(tz=UTC)
        self._watcher.add_watch(self._ensure_hour_dir(now))
        self._watcher.add_watch(self._ensure_hour_dir(now + timedelta(hours=1)))
        await self._validate_segments()
        self._precreate_task = asyncio.create_task(self._dir_precreate_loop())
        self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        """Stop recording this camera."""
        self._should_run = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None
        await self._kill_process()
        if self._precreate_task is not None:
            self._precreate_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._precreate_task
            self._precreate_task = None
        self.state = CameraState.STOPPED

    async def _spawn(self) -> None:
        """Spawn the ffmpeg process."""
        now = datetime.now(tz=UTC)
        self._watcher.add_watch(self._ensure_hour_dir(now))
        self._watcher.add_watch(self._ensure_hour_dir(now + timedelta(hours=1)))
        args = self._build_ffmpeg_args()
        logger.info("Starting ffmpeg for %s", self.name)
        self.state = CameraState.STARTING
        env = {**os.environ, "TZ": "UTC"}
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self.state = CameraState.RECORDING

    async def _dir_precreate_loop(self) -> None:
        """Ensure current + next hour dirs exist, aligned to hour boundaries."""
        try:
            while self._should_run:
                now = datetime.now(tz=UTC)
                self._watcher.add_watch(self._ensure_hour_dir(now))
                next_ts = now + timedelta(hours=1)
                self._watcher.add_watch(self._ensure_hour_dir(next_ts))
                next_boundary = next_ts.replace(minute=0, second=0, microsecond=0)
                sleep_for = (next_boundary - now).total_seconds() - 30
                await asyncio.sleep(max(1.0, sleep_for))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dir precreate loop for %s failed", self.name)

    async def index_segment(self, start_utc: int, segment: Path) -> None:
        """Probe one closed segment and record it in the DB.

        Called from :class:`SegmentWatcher` when an ``IN_CLOSE_WRITE``
        event fires for a ``.mp4`` under this camera's tree.
        """
        try:
            dur = await _probe_duration(segment)
            if dur is None:
                unlink_unplayable(segment)
                return
            try:
                size = segment.stat().st_size
            except OSError as exc:
                logger.warning("stat failed for %s: %s", segment, exc)
                return
            db.insert_segment(
                camera=self.name,
                start_utc=start_utc,
                duration_ms=round(dur * 1000),
                size_bytes=size,
            )
            logger.info("Indexed %s for %s (%.1fs)", segment.name, self.name, dur)
            task = asyncio.create_task(generate_sprite(segment))
            self._sprite_tasks.add(task)
            task.add_done_callback(self._sprite_tasks.discard)
        except Exception:
            logger.exception(
                "Failed to index segment %s for %s", segment.name, self.name
            )

    async def _validate_segments(self) -> None:
        """Probe any on-disk segments missing from the DB.

        One-shot sweep at startup to catch segments left by a prior
        run where the inotify event was lost (e.g. crash, SIGKILL).
        """
        storage_root = self.storage_root
        if not storage_root.is_dir():
            return

        candidates: list[tuple[Path, int]] = []
        for p in storage_root.glob(f"*/*/{self.name}/*.mp4"):
            parsed = _parse_segment_path(p, storage_root)
            if parsed is None:
                continue
            candidates.append((p, parsed[1]))
        if not candidates:
            return

        known = db.known_start_utcs(
            self.name,
            day_start_utc=0,
            day_end_utc=2**31 - 1,
        )
        to_probe = [(p, u) for p, u in candidates if u not in known]
        if not to_probe:
            return

        logger.info(
            "Validating %d unindexed segment(s) for %s",
            len(to_probe),
            self.name,
        )
        sem = asyncio.Semaphore(8)

        async def _probe_one(p: Path, start_utc: int) -> None:
            async with sem:
                dur = await _probe_duration(p)
                if dur is None:
                    unlink_unplayable(p)
                    return
                try:
                    size = p.stat().st_size
                except OSError:
                    return
                db.insert_segment(
                    camera=self.name,
                    start_utc=start_utc,
                    duration_ms=round(dur * 1000),
                    size_bytes=size,
                )

        await asyncio.gather(*(_probe_one(p, u) for p, u in to_probe))

    async def _kill_process(self) -> None:
        """Send SIGTERM, wait up to 5s, then SIGKILL."""
        if self._process is None or self._process.returncode is not None:
            self._process = None
            return

        try:
            self._process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except TimeoutError, ProcessLookupError:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass
        self._process = None

    async def _monitor(self) -> None:
        """Watch the ffmpeg process and restart with exponential backoff."""
        while self._should_run:
            start_time = time.monotonic()
            try:
                await self._spawn()
                assert self._process is not None
                stderr_data = await self._process.stderr.read()  # type: ignore[union-attr]
                await self._process.wait()

                elapsed = time.monotonic() - start_time
                returncode = self._process.returncode

                if returncode != 0:
                    error_msg = stderr_data.decode(errors="replace").strip()
                    self.last_error = (
                        error_msg or f"ffmpeg exited with code {returncode}"
                    )
                    logger.warning(
                        "ffmpeg for %s exited with code %d: %s",
                        self.name,
                        returncode,
                        self.last_error,
                    )
                    self.state = CameraState.ERROR
                else:
                    logger.info("ffmpeg for %s exited cleanly", self.name)

                # Reset backoff if process ran successfully for >30s
                if elapsed > 30:
                    self._backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error managing ffmpeg for %s", self.name)
                self.state = CameraState.ERROR

            if not self._should_run:
                break

            logger.info(
                "Restarting ffmpeg for %s in %.0fs",
                self.name,
                self._backoff,
            )
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, 60.0)

    def get_status(self) -> dict:
        """Return status info for this camera."""
        return {
            "name": self.name,
            "enabled": self.camera.enabled,
            "state": self.state,
            "last_error": self.last_error,
        }


class RecordingManager:
    """Manages all CameraRecorder instances and the shared SegmentWatcher."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.recorders: dict[str, CameraRecorder] = {}
        self.watcher = SegmentWatcher(
            Path(config.storage.path),
            self._dispatch_segment,
        )
        self._init_recorders()

    def _init_recorders(self) -> None:
        for name, camera in self.config.cameras.items():
            self.recorders[name] = CameraRecorder(
                name=name,
                camera=camera,
                storage=self.config.storage,
                watcher=self.watcher,
            )

    async def _dispatch_segment(
        self,
        camera_name: str,
        start_utc: int,
        path: Path,
    ) -> None:
        recorder = self.recorders.get(camera_name)
        if recorder is None:
            return
        await recorder.index_segment(start_utc, path)

    async def start_all(self) -> None:
        """Start recorders for all enabled cameras."""
        self.watcher.start()
        for name, recorder in self.recorders.items():
            if recorder.camera.enabled:
                logger.info("Starting recorder for %s", name)
                await recorder.start()

    async def stop_all(self) -> None:
        """Stop all recorders and the watcher."""
        for recorder in self.recorders.values():
            await recorder.stop()
        await self.watcher.stop()

    async def enable_camera(self, name: str) -> None:
        """Enable a camera and start recording."""
        if name not in self.recorders:
            msg = f"Unknown camera: {name}"
            raise KeyError(msg)
        self.config.cameras[name].enabled = True
        save_config(self.config)
        await self.recorders[name].start()

    async def disable_camera(self, name: str) -> None:
        """Disable a camera and stop recording."""
        if name not in self.recorders:
            msg = f"Unknown camera: {name}"
            raise KeyError(msg)
        self.config.cameras[name].enabled = False
        save_config(self.config)
        await self.recorders[name].stop()

    def get_status(self) -> dict[str, dict]:
        """Return status info for all cameras."""
        return {
            name: recorder.get_status() for name, recorder in self.recorders.items()
        }
