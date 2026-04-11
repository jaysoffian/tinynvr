# CLAUDE.md

## Project overview

TinyNVR — lightweight self-hosted NVR. Records RTSP camera streams via ffmpeg
(no transcoding) and serves a web UI for synced multi-camera playback
with timeline scrubbing. See [DESIGN.md](DESIGN.md) for the full design
including storage layout, recording/playback pipelines, retention, and
a "Why not HLS?" rationale.

## Verification

- Run `prek run --all-files` — never invoke ruff manually.
- Run `make run` to start the dev server on port 8554.

## Python

- Always use `uv` to run Python and tools (never bare `python`).
- Always use `uv add`/`uv remove` to manage dependencies.

## Git

- Do NOT include Claude attribution in commit messages.
