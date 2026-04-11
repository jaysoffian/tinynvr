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

## Design constraints (do not re-litigate)

- Segments on disk are **non-fragmented MP4 with `moov` at front**
  (ffmpeg `-segment_format mp4 -segment_format_options
  movflags=+faststart`). This is load-bearing for byte-range
  scrubbing. Do not switch to fragmented MP4, HLS, or MSE — see
  [DESIGN.md](DESIGN.md) "Why not HLS?" for the full rationale and
  the specific regressions that killed the HLS branch.
- `SEGMENT_SECONDS` is fixed at 60 and intentionally not
  configurable — see README.md "Why 1-minute segments".
- Gapless playback is achieved via double-buffered `<video>` swap
  in `static/index.html`, not via any streaming protocol. See
  DESIGN.md "Playback pipeline".
