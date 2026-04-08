# CLAUDE.md

## Project overview

Lightweight self-hosted NVR. Records RTSP camera streams via ffmpeg
(no transcoding) and serves a web UI for synced multi-camera playback
with timeline scrubbing. See [plan.md](plan.md) for full design.

## Verification

- Run `prek run --all-files` — never invoke ruff manually.
- Run `make run` to start the dev server on port 8554.

## Python

- Always use `uv` to run Python and tools (never bare `python`).
- Always use `uv add`/`uv remove` to manage dependencies.

## Git

- Do NOT include Claude attribution in commit messages.
