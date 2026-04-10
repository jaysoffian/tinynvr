"""Camera recording via ffmpeg subprocesses."""

import asyncio
import contextlib
import logging
import os
import signal
import time
from enum import StrEnum
from pathlib import Path

from asyncinotify import Inotify, Mask

from tinynvr.config import CameraConfig, Config, StorageConfig, save_config
from tinynvr.duration import append_duration, validate_indexes

logger = logging.getLogger(__name__)


class CameraState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RECORDING = "recording"
    ERROR = "error"


class CameraRecorder:
    """Manages a single ffmpeg subprocess for one camera."""

    def __init__(
        self,
        name: str,
        camera: CameraConfig,
        storage: StorageConfig,
    ) -> None:
        self.name = name
        self.camera = camera
        self.storage = storage
        self.state = CameraState.STOPPED
        self.last_error: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None
        self._watcher_task: asyncio.Task | None = None
        self._should_run = False
        self._backoff = 1.0

    @property
    def output_dir(self) -> Path:
        return Path(self.storage.path) / self.name

    def _build_ffmpeg_args(self) -> list[str]:
        output_pattern = str(self.output_dir / "%Y-%m-%d_%H-%M-%S.mkv")
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-use_wallclock_as_timestamps",
            "1",
            "-timeout",
            "10000000",
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
            str(self.storage.segment_minutes * 60),
            "-segment_format",
            "matroska",
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
        self.output_dir.mkdir(parents=True, exist_ok=True)
        await validate_indexes(self.output_dir)
        self._watcher_task = asyncio.create_task(self._watcher_loop())
        self._monitor_task = asyncio.create_task(self._monitor())

    async def stop(self) -> None:
        """Stop recording this camera."""
        self._should_run = False
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None
        await self._kill_process()
        if self._watcher_task:
            # Give the watcher a tick to pick up the IN_CLOSE_WRITE for
            # ffmpeg's final segment and dispatch its append task before
            # we cancel. Any append already in flight is drained by the
            # watcher's CancelledError handler; anything missed is still
            # recovered by validate_indexes() on the next start().
            await asyncio.sleep(0.05)
            self._watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher_task
            self._watcher_task = None
        self.state = CameraState.STOPPED

    async def _spawn(self) -> None:
        """Spawn the ffmpeg process."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
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

    async def _index_segment(self, mkv: Path) -> None:
        try:
            await append_duration(self.output_dir, mkv)
        except Exception:
            logger.exception("Failed to index segment %s for %s", mkv.name, self.name)

    async def _watcher_loop(self) -> None:
        """Append each finished segment's duration to its daily .idx file.

        Driven by inotify ``IN_CLOSE_WRITE`` events, which fire whenever
        ffmpeg closes a segment file — whether via normal rotation or
        on process exit (clean, SIGTERM, SIGKILL, or crash).

        Each event dispatches a detached append task so the iterator
        isn't blocked on ffprobe and so :meth:`stop` can drain any
        in-flight appends before cancelling the watcher.
        """
        pending: set[asyncio.Task[None]] = set()
        try:
            with Inotify() as inotify:
                inotify.add_watch(self.output_dir, Mask.CLOSE_WRITE)
                async for event in inotify:
                    path = event.path
                    if path is None or path.suffix != ".mkv":
                        continue
                    task = asyncio.create_task(
                        self._index_segment(self.output_dir / path.name)
                    )
                    pending.add(task)
                    task.add_done_callback(pending.discard)
        except asyncio.CancelledError:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            raise
        except Exception:
            logger.exception("Inotify watcher for %s failed", self.name)

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
    """Manages all CameraRecorder instances."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.recorders: dict[str, CameraRecorder] = {}
        self._init_recorders()

    def _init_recorders(self) -> None:
        for name, camera in self.config.cameras.items():
            self.recorders[name] = CameraRecorder(
                name=name,
                camera=camera,
                storage=self.config.storage,
            )

    async def start_all(self) -> None:
        """Start recorders for all enabled cameras."""
        for name, recorder in self.recorders.items():
            if recorder.camera.enabled:
                logger.info("Starting recorder for %s", name)
                await recorder.start()

    async def stop_all(self) -> None:
        """Stop all recorders."""
        for recorder in self.recorders.values():
            await recorder.stop()

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
